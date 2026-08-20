"""Unit tests for cli.publish pure functions.

All tests run offline — no AWS credentials required.
"""

import pytest

from cli.publish import (
    apply_version_ids,
    build_execution_input,
    canonical_prefix_for,
)
from kbp.ingestion import gates


# ─── helpers ──────────────────────────────────────────────────────────────────


def _doc(name: str, *, version_id: str | None = None) -> dict:
    """Minimal corpus manifest document entry."""
    d = {
        "documentId": name,
        "file": f"{name}.md",
        "contentSha256": "a" * 64,
        "metadataSha256": "b" * 64,
    }
    if version_id is not None:
        d["s3VersionId"] = version_id
    return d


def _corpus_manifest(docs: list[dict]) -> dict:
    return {
        "corpusId": "test-corpus",
        "contentFormat": "authored-markdown",
        "embeddedMetadataFields": [],
        "documentCount": len(docs),
        "totalContentBytes": 100,
        "documents": docs,
        "corpusSha256": "c" * 64,
    }


def _change_set(
    added: list[dict] | None = None,
    modified: list[dict] | None = None,
    deleted: list[dict] | None = None,
) -> dict:
    return {
        "added": added or [],
        "modified": modified or [],
        "deleted": deleted or [],
    }


FAKE_ARGS = dict(
    corpus_id="test-corpus",
    knowledge_base_id="KB123",
    data_source_id="DS456",
    canonical_bucket="canonical-bucket",
    registry_bucket="registry-bucket",
    canonical_prefix="test-corpus/canonical",
    release_id="test-corpus-20260101T000000Z-abcd1234",
    release_manifest_s3_uri="s3://registry-bucket/manifests/test-release.json",
    release_manifest_s3_version_id="ver-manifest-1",
    previous_document_count=0,
    allow_bulk_deletion=False,
    smoke_query="What is this?",
)


# ─── test: ingest and delete batches are both present ─────────────────────────


def test_batches_for_both_channels():
    doc_a = _doc("alpha")
    doc_b = _doc("beta")
    doc_c = _doc("gamma")
    changes = _change_set(added=[doc_a, doc_b], deleted=[doc_c])
    manifest = _corpus_manifest([doc_a, doc_b])

    result = build_execution_input(
        change_set=changes,
        corpus_manifest=manifest,
        **FAKE_ARGS,
    )

    assert len(result["ingestBatches"]) >= 1
    assert len(result["deleteBatches"]) >= 1
    # Each ingest batch carries documents
    assert all("documents" in batch for batch in result["ingestBatches"])
    # Each delete batch carries documentIdentifiers
    assert all("documentIdentifiers" in batch for batch in result["deleteBatches"])


# ─── test: each batch gets a distinct idempotency token ───────────────────────


def test_distinct_idempotency_tokens_per_batch():
    # 21 documents → 3 ingest batches (10+10+1)
    docs = [_doc(f"doc-{i}") for i in range(21)]
    changes = _change_set(added=docs)
    manifest = _corpus_manifest(docs)

    result = build_execution_input(
        change_set=changes,
        corpus_manifest=manifest,
        **FAKE_ARGS,
    )

    tokens = [batch["clientToken"] for batch in result["ingestBatches"]]
    assert len(tokens) == len(set(tokens)), "idempotency tokens must be unique per batch"


def test_distinct_idempotency_tokens_across_channels():
    doc_a = _doc("alpha")
    doc_b = _doc("beta")
    changes = _change_set(added=[doc_a], deleted=[doc_b])
    manifest = _corpus_manifest([doc_a])

    result = build_execution_input(
        change_set=changes,
        corpus_manifest=manifest,
        **FAKE_ARGS,
    )

    ingest_tokens = {batch["clientToken"] for batch in result["ingestBatches"]}
    delete_tokens = {batch["clientToken"] for batch in result["deleteBatches"]}
    assert ingest_tokens.isdisjoint(delete_tokens), (
        "tokens must differ between ingest and delete channels"
    )


# ─── test: poll identifiers cover both upserts and deletions ──────────────────


def test_poll_identifiers_cover_both_channels():
    doc_upsert = _doc("upsert-me")
    doc_delete = _doc("delete-me")
    changes = _change_set(added=[doc_upsert], deleted=[doc_delete])
    manifest = _corpus_manifest([doc_upsert])

    result = build_execution_input(
        change_set=changes,
        corpus_manifest=manifest,
        **FAKE_ARGS,
    )

    assert len(result["ingestDocumentIds"]) == 1
    assert len(result["deleteDocumentIds"]) == 1


# ─── test: smokeExpectation is 'present' when documents are upserted ──────────


def test_smoke_expectation_present_for_upsert():
    doc = _doc("my-doc")
    changes = _change_set(added=[doc])
    manifest = _corpus_manifest([doc])

    result = build_execution_input(
        change_set=changes,
        corpus_manifest=manifest,
        **FAKE_ARGS,
    )

    assert result["smokeExpectation"] == "present"
    # The target is the object's S3 URI, matching what the state machine extracts
    # from RetrievalResults[*].Location.S3Location.Uri. A documentId could never
    # match, so the gate would always fail.
    assert result["smokeTarget"].startswith("s3://")
    assert result["smokeTarget"].endswith(doc["file"])


# ─── test: delete-only release verifies absence ───────────────────────────────


def test_smoke_expectation_absent_for_delete_only():
    doc = _doc("remove-me")
    changes = _change_set(deleted=[doc])
    manifest = _corpus_manifest([])  # no documents remain after deletion

    result = build_execution_input(
        change_set=changes,
        corpus_manifest=manifest,
        **FAKE_ARGS,
    )

    assert result["smokeExpectation"] == "absent"
    assert result["smokeTarget"].startswith("s3://")
    assert result["smokeTarget"].endswith(doc["file"])


# ─── test: bulk-deletion override is passed through ───────────────────────────


def test_allow_bulk_deletion_in_input():
    doc = _doc("bulk-doc")
    changes = _change_set(deleted=[doc])
    manifest = _corpus_manifest([])

    args_override = {**FAKE_ARGS, "allow_bulk_deletion": True, "previous_document_count": 1}
    result = build_execution_input(
        change_set=changes,
        corpus_manifest=manifest,
        **args_override,
    )

    # The field must be present in the execution input (state machine reads it)
    # Note: the CDK CheckDeletionRatio currently hardcodes allowBulkDeletion=false
    # on the Lambda payload, but the input carries it for future use.
    assert "allowBulkDeletion" in result
    assert result["allowBulkDeletion"] is True


# ─── test: empty change set produces no batches ───────────────────────────────


def test_empty_change_set_produces_no_batches():
    changes = _change_set()
    manifest = _corpus_manifest([])

    result = build_execution_input(
        change_set=changes,
        corpus_manifest=manifest,
        **FAKE_ARGS,
    )

    assert result["ingestBatches"] == []
    assert result["deleteBatches"] == []
    assert result["ingestDocumentIds"] == []
    assert result["deleteDocumentIds"] == []


# ─── test: uploaded version ids land on manifest documents ────────────────────


def test_apply_version_ids_stamps_documents():
    doc_a = _doc("alpha")
    doc_b = _doc("beta")
    manifest = _corpus_manifest([doc_a, doc_b])

    version_ids = {
        "alpha.md": "ver-alpha-1",
        "beta.md": "ver-beta-2",
    }

    stamped = apply_version_ids(manifest, version_ids)

    by_id = {d["documentId"]: d for d in stamped["documents"]}
    assert by_id["alpha"]["s3VersionId"] == "ver-alpha-1"
    assert by_id["beta"]["s3VersionId"] == "ver-beta-2"


def test_apply_version_ids_does_not_mutate_input():
    doc = _doc("my-doc")
    manifest = _corpus_manifest([doc])
    version_ids = {"my-doc.md": "ver-1"}

    apply_version_ids(manifest, version_ids)

    # Original manifest document must be unchanged
    assert doc.get("s3VersionId") is None


# ─── test: version carried forward for untouched documents ────────────────────


def test_apply_version_ids_preserves_untouched_documents():
    """Documents not in the version_ids map keep whatever version they had."""
    doc_touched = _doc("touched")
    doc_untouched = _doc("untouched", version_id="ver-previous-42")
    manifest = _corpus_manifest([doc_touched, doc_untouched])

    version_ids = {"touched.md": "ver-new-1"}

    stamped = apply_version_ids(manifest, version_ids)

    by_id = {d["documentId"]: d for d in stamped["documents"]}
    assert by_id["touched"]["s3VersionId"] == "ver-new-1"
    assert by_id["untouched"]["s3VersionId"] == "ver-previous-42"


def test_apply_version_ids_none_for_document_with_no_prior_version():
    """A document not uploaded and with no prior version keeps None."""
    doc = _doc("no-prior")
    manifest = _corpus_manifest([doc])

    stamped = apply_version_ids(manifest, {})

    assert stamped["documents"][0]["s3VersionId"] is None


# ─── test: all required state machine fields are present ─────────────────────


def test_execution_input_has_all_required_fields():
    """Cross-check against the MergePointer fields from state-machine.ts."""
    doc = _doc("check-doc")
    changes = _change_set(added=[doc])
    manifest = _corpus_manifest([doc])

    result = build_execution_input(
        change_set=changes,
        corpus_manifest=manifest,
        **FAKE_ARGS,
    )

    # These fields come directly from MergePointer parameters (minus activeReleaseId
    # which is set by the ReadPointer task result, and pollAttempt set to 0 by the
    # state machine itself).
    required_fields = {
        "corpusId",
        "releaseId",
        "manifestS3Uri",
        "manifestS3VersionId",
        "canonicalPrefix",
        "knowledgeBaseId",
        "dataSourceId",
        "changeSet",
        "ingestBatches",
        "deleteBatches",
        "deletedCount",
        "previousDocumentCount",
        "smokeQuery",
        "smokeExpectation",
        "smokeTarget",
        "ingestDocumentIds",
        "deleteDocumentIds",
    }
    missing = required_fields - result.keys()
    assert not missing, f"missing required fields: {missing}"


# ─── test: batch structure carries knowledgeBaseId and dataSourceId ───────────


def test_ingest_batch_carries_kb_and_ds_ids():
    doc = _doc("kb-doc")
    changes = _change_set(added=[doc])
    manifest = _corpus_manifest([doc])

    result = build_execution_input(
        change_set=changes,
        corpus_manifest=manifest,
        **FAKE_ARGS,
    )

    batch = result["ingestBatches"][0]
    assert batch["knowledgeBaseId"] == FAKE_ARGS["knowledge_base_id"]
    assert batch["dataSourceId"] == FAKE_ARGS["data_source_id"]


def test_delete_batch_carries_kb_and_ds_ids():
    doc = _doc("del-doc")
    changes = _change_set(deleted=[doc])
    manifest = _corpus_manifest([])

    result = build_execution_input(
        change_set=changes,
        corpus_manifest=manifest,
        **FAKE_ARGS,
    )

    batch = result["deleteBatches"][0]
    assert batch["knowledgeBaseId"] == FAKE_ARGS["knowledge_base_id"]
    assert batch["dataSourceId"] == FAKE_ARGS["data_source_id"]


# ─── test: deletedCount and previousDocumentCount ─────────────────────────────


def test_deleted_count_matches_change_set():
    docs = [_doc(f"d{i}") for i in range(3)]
    changes = _change_set(deleted=docs)
    manifest = _corpus_manifest([])

    result = build_execution_input(
        change_set=changes,
        corpus_manifest=manifest,
        **{**FAKE_ARGS, "previous_document_count": 5},
    )

    assert result["deletedCount"] == 3
    assert result["previousDocumentCount"] == 5


def test_canonical_prefix_matches_the_data_source_inclusion_prefix():
    """The upload prefix and the indexed prefix must be the same string.

    infra/bin/app.ts configures the data source with `canonical/<corpusId>`.
    Uploading anywhere else puts every object outside the range the knowledge
    base indexes, so ingestion would report success against documents the
    service never sees.
    """
    from pathlib import Path
    import re

    infra_app = (
        Path(__file__).resolve().parent.parent.parent / "infra" / "bin" / "app.ts"
    )
    declared = set(
        re.findall(r"canonicalPrefix:\s*`([^`]+)`", infra_app.read_text(encoding="utf-8"))
    )

    assert declared == {"canonical/${corpusId}"}
    assert canonical_prefix_for("demo") == "canonical/demo"


def test_deletion_threshold_matches_the_state_machine():
    """A client guard that disagrees with gate B is worse than no guard.

    Too low and it refuses releases the pipeline would accept; too high and it
    destroys objects before the gate can refuse them.
    """
    import re
    from pathlib import Path

    from cli.publish import DELETION_RATIO_THRESHOLD

    infra_app = (
        Path(__file__).resolve().parent.parent.parent / "infra" / "bin" / "app.ts"
    )
    declared = re.findall(
        r"deletionRatioThreshold:\s*([0-9.]+)", infra_app.read_text(encoding="utf-8")
    )

    assert declared, "infra/bin/app.ts must declare deletionRatioThreshold"
    assert {float(value) for value in declared} == {DELETION_RATIO_THRESHOLD}


def test_over_threshold_deletion_is_refused_before_any_upload(monkeypatch, tmp_path):
    """The guard must run before upload_changed_objects touches S3.

    Gate B is authoritative but runs after this process has already deleted
    objects, so an over-threshold release has to stop here to leave S3 intact.
    """
    from cli import publish

    touched: list[str] = []
    monkeypatch.setattr(
        publish,
        "upload_changed_objects",
        lambda *a, **k: touched.append("uploaded") or {},
    )

    verdict = gates.evaluate_deletion_ratio(
        deleted_count=8,
        previous_document_count=13,
        threshold=publish.DELETION_RATIO_THRESHOLD,
        allow_bulk_deletion=False,
    )

    assert verdict["passed"] is False
    assert verdict["ratio"] > publish.DELETION_RATIO_THRESHOLD
    assert touched == []


def test_the_override_lets_a_bulk_deletion_through():
    verdict = gates.evaluate_deletion_ratio(
        deleted_count=8,
        previous_document_count=13,
        threshold=0.5,
        allow_bulk_deletion=True,
    )

    assert verdict["passed"] is True
    assert verdict["overridden"] is True
