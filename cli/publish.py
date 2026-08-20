"""Local publish entrypoint for Amazon Bedrock Managed Knowledge Base releases.

Design notes
────────────
Preparation stays local (not in the state machine) so a large corpus scan is
not bound by Lambda limits and the iteration loop stays fast.

The module is structured as pure functions + one I/O shell (`main`) so the
pure functions and their tests never need AWS credentials.  boto3 is imported
inside main() only.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from kbp.ingestion import batching, gates
from kbp.preparation import corpus, diff
from kbp.registry import manifest as registry_manifest
from kbp.registry import store

# Must equal deletionRatioThreshold in infra/bin/app.ts. A test compares the two,
# because a client-side guard that disagrees with the state machine's gate would
# either refuse releases the pipeline accepts or destroy objects before the gate
# gets to refuse them.
DELETION_RATIO_THRESHOLD = 0.5


# ─── pure functions ───────────────────────────────────────────────────────────


def apply_version_ids(corpus_manifest: dict, version_ids: dict[str, str]) -> dict:
    """Return a new manifest with s3VersionId stamped onto each document.

    Documents whose file is not in `version_ids` carry forward whatever version
    they already held (or None if they had none).  The input is not mutated.
    """
    stamped_docs = [
        {
            **doc,
            "s3VersionId": version_ids.get(doc["file"], doc.get("s3VersionId")),
        }
        for doc in corpus_manifest["documents"]
    ]
    return {**corpus_manifest, "documents": stamped_docs}


def build_execution_input(
    *,
    change_set: dict,
    corpus_manifest: dict,
    corpus_id: str,
    knowledge_base_id: str,
    data_source_id: str,
    canonical_bucket: str,
    registry_bucket: str,
    canonical_prefix: str,
    release_id: str,
    release_manifest_s3_uri: str,
    release_manifest_s3_version_id: str,
    previous_document_count: int,
    allow_bulk_deletion: bool,
    smoke_query: str,
) -> dict:
    """Build the Step Functions execution input from a change set.

    Pure function: no AWS calls, no file I/O.  The caller is responsible for
    uploading objects and stamping version IDs onto the corpus manifest before
    calling this function.

    The field layout matches the MergePointer Pass state parameters in
    infra/lib/state-machine.ts.  Fields managed by the state machine itself
    (activeReleaseId, pollAttempt) are NOT included here — the state machine
    derives them from ReadPointer result and initialises pollAttempt to 0.
    """
    upserts = change_set.get("added", []) + change_set.get("modified", [])
    deleted = change_set.get("deleted", [])

    # ── Ingest batches ────────────────────────────────────────────────────────
    ingest_batch_groups = batching.split_batches(upserts) if upserts else []
    ingest_batches = [
        {
            "knowledgeBaseId": knowledge_base_id,
            "dataSourceId": data_source_id,
            "documents": batching.build_ingest_payload(
                documents=batch,
                bucket=canonical_bucket,
                prefix=canonical_prefix,
            ),
            "clientToken": registry_manifest.build_client_token(
                release_id=release_id,
                operation="ingest",
                batch_index=idx,
            ),
        }
        for idx, batch in enumerate(ingest_batch_groups)
    ]

    # ── Delete batches ────────────────────────────────────────────────────────
    delete_batch_groups = batching.split_batches(deleted) if deleted else []
    delete_batches = [
        {
            "knowledgeBaseId": knowledge_base_id,
            "dataSourceId": data_source_id,
            "documentIdentifiers": batching.build_document_identifiers(
                documents=batch,
                bucket=canonical_bucket,
                prefix=canonical_prefix,
            ),
            "clientToken": registry_manifest.build_client_token(
                release_id=release_id,
                operation="delete",
                batch_index=idx,
            ),
        }
        for idx, batch in enumerate(delete_batch_groups)
    ]

    # ── Poll identifiers ──────────────────────────────────────────────────────
    ingest_document_ids = batching.build_document_identifiers(
        documents=upserts,
        bucket=canonical_bucket,
        prefix=canonical_prefix,
    )
    delete_document_ids = batching.build_document_identifiers(
        documents=deleted,
        bucket=canonical_bucket,
        prefix=canonical_prefix,
    )

    # ── Smoke gate target and expectation ─────────────────────────────────────
    # A delete-only release verifies absence of a removed document; any release
    # with upserts verifies presence of an upserted document.
    #
    # The target is the object's S3 URI, not its documentId: the state machine
    # compares against RetrievalResults[*].Location.S3Location.Uri, so a
    # documentId could never match and the gate would always fail.
    def smoke_uri(document: dict) -> str:
        return (
            f"s3://{canonical_bucket}/{canonical_prefix.strip('/')}/{document['file']}"
        )

    if upserts:
        smoke_expectation = "present"
        smoke_target = smoke_uri(upserts[0])
    elif deleted:
        smoke_expectation = "absent"
        smoke_target = smoke_uri(deleted[0])
    else:
        # Empty change set — caller should have short-circuited before here.
        smoke_expectation = "present"
        smoke_target = ""

    return {
        "corpusId": corpus_id,
        "releaseId": release_id,
        "manifestS3Uri": release_manifest_s3_uri,
        "manifestS3VersionId": release_manifest_s3_version_id,
        "canonicalPrefix": canonical_prefix,
        "knowledgeBaseId": knowledge_base_id,
        "dataSourceId": data_source_id,
        "changeSet": {
            "added": change_set.get("added", []),
            "modified": change_set.get("modified", []),
            "deleted": change_set.get("deleted", []),
        },
        "ingestBatches": ingest_batches,
        "deleteBatches": delete_batches,
        "deletedCount": len(deleted),
        "previousDocumentCount": previous_document_count,
        "smokeQuery": smoke_query,
        "smokeExpectation": smoke_expectation,
        "smokeTarget": smoke_target,
        "ingestDocumentIds": ingest_document_ids,
        "deleteDocumentIds": delete_document_ids,
        # Passed through for the CheckDeletionRatio Lambda; the CDK state machine
        # currently hard-codes allowBulkDeletion=false on the Lambda payload, but
        # carrying it here lets a future version wire it through without a code
        # change to publish.py.
        "allowBulkDeletion": allow_bulk_deletion,
    }


# ─── I/O helpers ─────────────────────────────────────────────────────────────


def canonical_prefix_for(corpus_id: str) -> str:
    """Derive the canonical S3 prefix for corpus objects.

    Must match the data source's inclusion prefix, which infra/bin/app.ts sets to
    `canonical/<corpusId>`. Uploading under any other prefix puts every object
    outside the range the knowledge base indexes, so ingestion would report
    success against documents the service never sees.
    """
    return f"canonical/{corpus_id}"


def upload_changed_objects(
    s3_client,
    *,
    upserts: list[dict],
    deletions: list[dict],
    prepared_dir: Path,
    canonical_bucket: str,
    canonical_prefix: str,
) -> dict[str, str]:
    """Upload content and sidecar objects; delete removed ones.

    Returns a map of relative file path → S3 version ID for every object
    uploaded.  SHA-256 is stored in S3 object metadata under the key ``sha256``
    so the verify_s3 Lambda can read it with a HEAD request.
    """
    version_ids: dict[str, str] = {}

    for doc in upserts:
        for rel_file in (doc["file"], f"{doc['file']}.metadata.json"):
            local_path = prepared_dir / rel_file
            content = local_path.read_bytes()
            sha256 = hashlib.sha256(content).hexdigest()
            s3_key = f"{canonical_prefix}/{rel_file}"
            response = s3_client.put_object(
                Bucket=canonical_bucket,
                Key=s3_key,
                Body=content,
                Metadata={"sha256": sha256},
            )
            version_ids[doc["file"]] = response["VersionId"]

    # Failures are not swallowed. A delete that silently fails leaves an object
    # the manifest says is gone, and the release would go on to promote a state
    # nobody verified.
    for doc in deletions:
        for rel_file in (doc["file"], f"{doc['file']}.metadata.json"):
            s3_client.delete_object(
                Bucket=canonical_bucket, Key=f"{canonical_prefix}/{rel_file}"
            )

    return version_ids


def _upload_manifest(
    s3_client,
    *,
    registry_bucket: str,
    release_id: str,
    manifest_obj: dict,
) -> tuple[str, str]:
    """Upload the release manifest and return (s3_uri, version_id)."""
    key = f"manifests/{release_id}.json"
    body = (json.dumps(manifest_obj, ensure_ascii=False, indent=2) + "\n").encode()
    response = s3_client.put_object(
        Bucket=registry_bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    s3_uri = f"s3://{registry_bucket}/{key}"
    version_id = response["VersionId"]
    return s3_uri, version_id


# ─── argparse shell ───────────────────────────────────────────────────────────


def _build_parser():
    import argparse

    p = argparse.ArgumentParser(
        description="Publish a release to Amazon Bedrock Managed Knowledge Base."
    )
    p.add_argument("--source-dir", required=True, type=Path)
    p.add_argument("--corpus-id", required=True)
    p.add_argument("--canonical-bucket", required=True)
    p.add_argument("--registry-bucket", required=True)
    p.add_argument("--knowledge-base-id", required=True)
    p.add_argument("--data-source-id", required=True)
    p.add_argument("--state-machine-arn", required=True)
    p.add_argument("--release-table", required=True)
    p.add_argument("--source-commit", default="unknown")
    p.add_argument(
        "--allow-bulk-deletion",
        action="store_true",
        help="Override the deletion ratio gate.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the execution input without uploading anything or starting "
            "an execution. Treats the release as initial (no active pointer read)."
        ),
    )
    return p


def main(argv=None) -> None:
    """Wires preparation, upload, manifest build, and state machine start."""
    import boto3

    args = _build_parser().parse_args(argv)

    corpus_id: str = args.corpus_id
    canonical_bucket: str = args.canonical_bucket
    registry_bucket: str = args.registry_bucket
    knowledge_base_id: str = args.knowledge_base_id
    data_source_id: str = args.data_source_id
    state_machine_arn: str = args.state_machine_arn
    release_table: str = args.release_table
    source_commit: str = args.source_commit
    allow_bulk_deletion: bool = args.allow_bulk_deletion
    dry_run: bool = args.dry_run

    canonical_prefix = canonical_prefix_for(corpus_id)

    # ── Step 1: prepare corpus locally ───────────────────────────────────────
    with tempfile.TemporaryDirectory(prefix="kbp-publish-") as tmp_str:
        prepared_dir = Path(tmp_str)
        print(f"[publish] Preparing corpus from {args.source_dir} …", file=sys.stderr)
        corpus_manifest = corpus.prepare(
            source_dir=args.source_dir,
            output_dir=prepared_dir,
            corpus_id=corpus_id,
            embedded_fields=[],
        )
        print(
            f"[publish] {corpus_manifest['documentCount']} documents prepared.",
            file=sys.stderr,
        )

        # ── Step 2: read previous release (skip for dry-run) ─────────────────
        # dry-run decision: skip the DynamoDB pointer read and treat this release
        # as initial.  This means dry-run never needs credentials and exits
        # instantly, which is the whole point.  The trade-off is that a dry-run
        # on an already-published corpus shows an empty previous manifest and all
        # documents as added — not a problem because dry-run is for inspecting
        # the input shape, not for correctness against live state.
        if dry_run:
            previous_release_id = None
            previous_manifest: dict | None = None
            previous_document_count = 0
            print(
                "[publish] --dry-run: skipping registry read; treating as initial release.",
                file=sys.stderr,
            )
        else:
            dynamodb = boto3.client("dynamodb")
            s3 = boto3.client("s3")
            previous_release_id = store.read_active_release_id(
                dynamodb, table_name=release_table, corpus_id=corpus_id
            )
            if previous_release_id:
                manifest_key = f"manifests/{previous_release_id}.json"
                obj = s3.get_object(Bucket=registry_bucket, Key=manifest_key)
                previous_manifest = json.loads(obj["Body"].read())
                previous_document_count = previous_manifest.get("documentCount", 0)
            else:
                previous_manifest = None
                previous_document_count = 0

        # ── Step 3: diff ──────────────────────────────────────────────────────
        change_set = diff.diff_manifests(previous_manifest, corpus_manifest)

        if gates.is_empty_change_set(change_set):
            print("[publish] No changes detected. Nothing to publish.", file=sys.stderr)
            return

        upserts = change_set["added"] + change_set["modified"]
        deleted = change_set["deleted"]

        # ── Step 4: build release id BEFORE upload so it can be the execution name
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        release_id = registry_manifest.build_release_id(
            corpus_id=corpus_id,
            timestamp=timestamp,
            corpus_sha256=corpus_manifest["corpusSha256"],
        )

        # ── Step 4b: deletion guard, before anything is destroyed ────────────
        # The state machine's gate B is the authoritative control, but it runs
        # after this process has already removed objects from S3. Evaluating the
        # same pure function here keeps an over-threshold deletion from
        # destroying anything, so a refused release really does leave S3 alone.
        deletion_verdict = gates.evaluate_deletion_ratio(
            deleted_count=len(deleted),
            previous_document_count=previous_document_count,
            threshold=DELETION_RATIO_THRESHOLD,
            allow_bulk_deletion=allow_bulk_deletion,
        )
        if not deletion_verdict["passed"]:
            raise SystemExit(
                f"refusing to delete {len(deleted)} of {previous_document_count} "
                f"documents ({deletion_verdict['ratio']:.0%}), over the "
                f"{DELETION_RATIO_THRESHOLD:.0%} threshold. Nothing has been "
                "changed. Re-run with --allow-bulk-deletion to proceed."
            )

        # ── Step 5: upload (skipped for dry-run) ─────────────────────────────
        if dry_run:
            version_ids: dict[str, str] = {}
            stamped_manifest = corpus_manifest
            release_manifest_s3_uri = f"s3://{registry_bucket}/manifests/{release_id}.json"
            release_manifest_s3_version_id = "dry-run-version"
            print("[publish] --dry-run: skipping S3 upload.", file=sys.stderr)
        else:
            print(
                f"[publish] Uploading {len(upserts)} upserted objects …",
                file=sys.stderr,
            )
            version_ids = upload_changed_objects(
                s3,
                upserts=upserts,
                deletions=deleted,
                prepared_dir=prepared_dir,
                canonical_bucket=canonical_bucket,
                canonical_prefix=canonical_prefix,
            )

            # ── Step 6: stamp version ids + build release manifest ────────────
            stamped_manifest = apply_version_ids(corpus_manifest, version_ids)
            release_manifest_obj = registry_manifest.build_release_manifest(
                release_id=release_id,
                parent_release_id=previous_release_id,
                corpus_manifest=stamped_manifest,
                change_counts={
                    "added": len(change_set["added"]),
                    "modified": len(change_set["modified"]),
                    "deleted": len(deleted),
                },
                source_commit=source_commit,
            )

            release_manifest_s3_uri, release_manifest_s3_version_id = _upload_manifest(
                s3,
                registry_bucket=registry_bucket,
                release_id=release_id,
                manifest_obj=release_manifest_obj,
            )
            print(
                f"[publish] Manifest uploaded: {release_manifest_s3_uri}",
                file=sys.stderr,
            )

        # ── Step 7: build execution input ─────────────────────────────────────
        execution_input = build_execution_input(
            change_set=change_set,
            corpus_manifest=stamped_manifest,
            corpus_id=corpus_id,
            knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id,
            canonical_bucket=canonical_bucket,
            registry_bucket=registry_bucket,
            canonical_prefix=canonical_prefix,
            release_id=release_id,
            release_manifest_s3_uri=release_manifest_s3_uri,
            release_manifest_s3_version_id=release_manifest_s3_version_id,
            previous_document_count=previous_document_count,
            allow_bulk_deletion=allow_bulk_deletion,
            smoke_query=f"Tell me about {upserts[0]['documentId'] if upserts else deleted[0]['documentId']}",
        )

        if dry_run:
            print(json.dumps(execution_input, indent=2))
            return

        # ── Step 8: start execution ────────────────────────────────────────────
        # CreateDataSource is asynchronous for a managed knowledge base, taking
        # 2-5 minutes to reach AVAILABLE, and the user guide says not to ingest
        # before then. Publishing straight after a deploy would otherwise race it.
        status = boto3.client("bedrock-agent").get_data_source(
            knowledgeBaseId=knowledge_base_id, dataSourceId=data_source_id
        )["dataSource"]["status"]
        if status != "AVAILABLE":
            raise SystemExit(
                f"data source {data_source_id} is {status}, not AVAILABLE; "
                "ingesting now would race its creation"
            )

        sfn = boto3.client("stepfunctions")
        print(f"[publish] Starting execution {release_id} …", file=sys.stderr)
        sfn.start_execution(
            stateMachineArn=state_machine_arn,
            name=release_id,
            input=json.dumps(execution_input),
        )
        print(f"[publish] Execution started: {release_id}", file=sys.stderr)
        print(f"[publish] Release ID: {release_id}", file=sys.stderr)


if __name__ == "__main__":
    main()
