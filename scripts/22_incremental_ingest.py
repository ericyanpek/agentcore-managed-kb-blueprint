#!/usr/bin/env python3

"""Plan the two ingestion channels for a Markdown corpus change set.

The fast path batches changed documents for IngestKnowledgeBaseDocuments. The
reconciliation path is a single StartIngestionJob over the whole data source and
is the only channel that removes deleted documents.

Both channels are constrained by quotas whose managed-knowledge-base values are
partly unpublished, so the batch size and the throttle interval are inputs
rather than constants. See scripts/23_verify_assumptions.sh.
"""

import argparse
import json
import math
import sys
from pathlib import Path


def plan(args: argparse.Namespace) -> dict:
    report = json.loads(args.change_report.read_text(encoding="utf-8"))
    changes = report["changes"]
    upserts = changes["added"] + changes["modified"]
    deletions = changes["deleted"]

    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1")

    batches = [
        upserts[index : index + args.batch_size]
        for index in range(0, len(upserts), args.batch_size)
    ]
    fast_path_seconds = (
        max(0, len(batches) - 1) / args.direct_requests_per_second
        if args.direct_requests_per_second > 0
        else 0.0
    )
    reconciliation_required = bool(deletions) or args.always_reconcile
    reconciliation_seconds = args.throttle_interval_seconds if reconciliation_required else 0.0

    steps = []
    for position, batch in enumerate(batches, start=1):
        steps.append(
            {
                "channel": "direct",
                "api": "IngestKnowledgeBaseDocuments",
                "sequence": position,
                "documentCount": len(batch),
                "documentIds": [item["documentId"] for item in batch],
                "s3Keys": [
                    f"{args.s3_prefix.rstrip('/')}/{item['file']}" for item in batch
                ],
                "precondition": "objects and sidecars already uploaded to S3",
            }
        )
    if reconciliation_required:
        steps.append(
            {
                "channel": "reconciliation",
                "api": "StartIngestionJob",
                "sequence": len(batches) + 1,
                "documentCount": None,
                "reason": (
                    "deletions present"
                    if deletions
                    else "always-reconcile requested"
                ),
                "deletedDocumentIds": [item["documentId"] for item in deletions],
                "precondition": "deleted objects already removed from S3",
            }
        )

    deletion_ratio = (
        len(deletions) / report["documentCount"] if report["documentCount"] else 0.0
    )
    guardrails = []
    if deletion_ratio > args.deletion_protection_threshold:
        guardrails.append(
            f"deletion ratio {deletion_ratio:.1%} exceeds the configured "
            f"{args.deletion_protection_threshold:.0%} deletion protection threshold; "
            "the connector will skip the delete phase and the index will retain "
            "removed documents"
        )
    if not upserts and not reconciliation_required:
        guardrails.append("no changes detected; nothing to ingest")
    if args.assume_sync_removes_direct_documents and upserts:
        guardrails.append(
            "assumption A2 is treated as unverified-pessimistic: every fast-path "
            "document must exist in S3 before ingestion or a later reconciliation "
            "may drop it"
        )

    return {
        "corpusId": report["corpusId"],
        "changeCounts": report["changeCounts"],
        "quotaInputs": {
            "batchSize": args.batch_size,
            "directRequestsPerSecond": args.direct_requests_per_second,
            "throttleIntervalSeconds": args.throttle_interval_seconds,
            "deletionProtectionThreshold": args.deletion_protection_threshold,
            "assumeSyncRemovesDirectDocuments": (
                args.assume_sync_removes_direct_documents
            ),
        },
        "fastPathBatches": len(batches),
        "reconciliationRequired": reconciliation_required,
        "estimatedSeconds": math.ceil(fast_path_seconds + reconciliation_seconds),
        "guardrails": guardrails,
        "steps": steps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--change-report", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--s3-prefix", required=True)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Documents per IngestKnowledgeBaseDocuments request (managed quota: 10).",
    )
    parser.add_argument(
        "--direct-requests-per-second",
        type=float,
        default=20.0,
        help="Documented managed quota for IngestKnowledgeBaseDocuments.",
    )
    parser.add_argument(
        "--throttle-interval-seconds",
        type=float,
        default=10.0,
        help=(
            "Spacing assumed for StartIngestionJob. 10 s mirrors the documented "
            "0.1 rps for non-managed knowledge bases and is pessimistic until "
            "assumption A1 is measured."
        ),
    )
    parser.add_argument("--deletion-protection-threshold", type=float, default=0.5)
    parser.add_argument(
        "--always-reconcile",
        action="store_true",
        help="Run the reconciliation job even when no deletions were detected.",
    )
    parser.add_argument(
        "--assume-sync-removes-direct-documents",
        action="store_true",
        default=True,
        help="Pessimistic default for unverified assumption A2.",
    )
    args = parser.parse_args()

    try:
        result = plan(args)
        args.plan.parent.mkdir(parents=True, exist_ok=True)
        args.plan.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"Ingestion planning failed: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "steps"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
