"""Pure decision logic for the four release gates.

These functions take plain data and return plain data so they can be tested
without AWS. The Lambda handlers in kbp/ingestion/handlers are thin adapters
that fetch state and delegate here.
"""

INGEST_SUCCESS_STATUS = "INDEXED"
DELETE_SUCCESS_STATUS = "NOT_FOUND"

INGEST_IN_FLIGHT_STATUSES = frozenset({"PENDING", "STARTING", "IN_PROGRESS"})

# INDEXED counts as in-flight while deleting: a document polled right after the
# delete call is issued still reports INDEXED, and treating that as terminal
# would fail a valid delete before the transition to DELETING propagates.
DELETE_IN_FLIGHT_STATUSES = frozenset(
    {"INDEXED", "DELETING", "DELETE_IN_PROGRESS"}
)


def evaluate_deletion_ratio(
    *,
    deleted_count: int,
    previous_document_count: int,
    threshold: float,
    allow_bulk_deletion: bool = False,
) -> dict:
    """Evaluate the deletion guard against the pre-release document count.

    The denominator is deliberately the count before this release. Using the
    post-release count would make a full deletion compute as zero.
    """
    # A threshold outside [0, 1] silently disables the guard in one direction or
    # blocks every release in the other. DELETION_THRESHOLD=1.5 as a typo for
    # 0.15 would let a 90% deletion through.
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be within [0, 1], got {threshold}")

    # Reject a non-boolean override rather than coercing it. A JSONPath-sourced
    # value arrives as the string "false", which is truthy, so coercion would
    # permit exactly the deletion the caller meant to forbid.
    if not isinstance(allow_bulk_deletion, bool):
        raise ValueError(
            "allow_bulk_deletion must be a bool, got "
            f"{type(allow_bulk_deletion).__name__} {allow_bulk_deletion!r}"
        )

    if previous_document_count == 0:
        if deleted_count:
            raise ValueError(
                f"cannot delete {deleted_count} documents from an empty corpus"
            )
        return {"ratio": 0.0, "passed": True, "overridden": False, "threshold": threshold}

    ratio = deleted_count / previous_document_count
    within_threshold = ratio <= threshold
    return {
        "ratio": ratio,
        "passed": within_threshold or allow_bulk_deletion,
        "overridden": bool(not within_threshold and allow_bulk_deletion),
        "threshold": threshold,
    }


def _partition_statuses(
    details: list[dict],
    *,
    success_status: str,
    in_flight_statuses: frozenset[str],
    expected_count: int | None,
) -> dict:
    pending = [
        item["identifier"]
        for item in details
        if item["status"] in in_flight_statuses
    ]
    failures = [
        {"identifier": item["identifier"], "status": item["status"]}
        for item in details
        if item["status"] not in in_flight_statuses
        and item["status"] != success_status
    ]

    # Without this the gate passes vacuously: a truncated or empty status
    # response would report success having observed no document at all.
    if expected_count is not None and len(details) != expected_count:
        return {
            "settled": True,
            "passed": False,
            "pending": [],
            "failures": [
                {
                    "identifier": "*",
                    "status": f"expected {expected_count} statuses, got {len(details)}",
                }
            ],
        }

    settled = not pending
    return {
        "settled": settled,
        "passed": settled and not failures,
        "pending": pending,
        "failures": failures,
    }


def evaluate_ingest_statuses(
    details: list[dict], *, expected_count: int | None = None
) -> dict:
    """Aggregate document statuses for upserted documents.

    Only INDEXED counts as success. PARTIALLY_INDEXED and the METADATA_* failure
    variants are terminal-but-incomplete: the API reports no error while the
    indexed content is incomplete. An unrecognized status is treated as a
    failure rather than as in-flight, so the poller cannot spin forever.

    Pass expected_count to reject a response that does not describe every
    document submitted.
    """
    return _partition_statuses(
        details,
        success_status=INGEST_SUCCESS_STATUS,
        in_flight_statuses=INGEST_IN_FLIGHT_STATUSES,
        expected_count=expected_count,
    )


def evaluate_delete_statuses(
    details: list[dict], *, expected_count: int | None = None
) -> dict:
    """Aggregate document statuses for deleted documents.

    Deletion is confirmed only when the document reports NOT_FOUND; the 202
    response from the delete call proves nothing about the index.

    Pass expected_count to reject a response that does not describe every
    document submitted.
    """
    return _partition_statuses(
        details,
        success_status=DELETE_SUCCESS_STATUS,
        in_flight_statuses=DELETE_IN_FLIGHT_STATUSES,
        expected_count=expected_count,
    )


def evaluate_s3_consistency(
    *,
    expected_upserts: list[dict],
    observed_objects: dict[str, str],
    expected_deletions: list[dict],
    surviving_deletions: list[str],
) -> dict:
    """Verify canonical objects match the manifest before ingestion.

    `observed_objects` maps object key suffix to its SHA-256. `surviving_deletions`
    lists files the caller found still present in S3; anything in
    `expected_deletions` that also appears in `observed_objects` is added to that
    set here. A failed S3 deletion must block promotion rather than be ignored.
    """
    missing: set[str] = set()
    mismatched: set[str] = set()

    for item in expected_upserts:
        content_key = item["file"]
        sidecar_key = f"{item['file']}.metadata.json"

        if content_key not in observed_objects:
            missing.add(content_key)
        elif observed_objects[content_key] != item["contentSha256"]:
            mismatched.add(content_key)

        if sidecar_key not in observed_objects:
            missing.add(sidecar_key)
        elif observed_objects[sidecar_key] != item["metadataSha256"]:
            mismatched.add(sidecar_key)

    surviving = set(surviving_deletions)
    surviving.update(
        item["file"] for item in expected_deletions if item["file"] in observed_objects
    )
    # Sorted lists rather than sets: this value is serialized to JSON for the
    # state machine, and duplicates would only confuse the failure report.
    return {
        "passed": not (missing or mismatched or surviving),
        "missing": sorted(missing),
        "mismatched": sorted(mismatched),
        "surviving": sorted(surviving),
        "expectedDeletionCount": len(expected_deletions),
    }


def evaluate_smoke_retrieval(
    *, expectation: str, retrieved_document_ids: list[str], target: str
) -> dict:
    """Check a single smoke retrieval outcome.

    A delete-only release has no upserted document to smoke test, so it verifies
    absence instead: the removed document must no longer be retrievable.
    """
    if expectation not in ("present", "absent"):
        raise ValueError(f"unknown expectation: {expectation}")

    found = target in retrieved_document_ids
    passed = found if expectation == "present" else not found
    return {"passed": passed, "expectation": expectation, "target": target, "found": found}


def is_empty_change_set(changes: dict) -> bool:
    """Report whether a change set contains no work at all."""
    return not any(
        changes.get(key) for key in ("added", "modified", "deleted")
    )
