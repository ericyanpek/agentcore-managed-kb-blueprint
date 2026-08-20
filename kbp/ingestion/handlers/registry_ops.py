"""Lambda handler for registry operations: DynamoDB state and pointer.

Dispatches on event["action"] and delegates to kbp.registry.store. Reads the
DynamoDB table name from the RELEASE_TABLE environment variable.
"""

import os

import boto3

from kbp.registry import store

# Built once so warm invocations reuse the connection pool.
_CLIENT = boto3.client("dynamodb")

_ACTIONS = frozenset(
    {"readPointer", "createRelease", "advanceStatus", "promote", "fail"}
)


GATE_RESULT_FIELDS = (
    ("s3Gate", "gate A (S3 consistency)"),
    ("deletionRatioGate", "gate B (deletion ratio)"),
    ("ingestStatusGate", "gate C (document status)"),
    ("smokeGate", "gate D (smoke retrieval)"),
)


def describe_failure(context: dict) -> str:
    """Summarize why a release failed, from the execution state.

    A thrown task leaves $.error; a gate that simply returned passed=false does
    not. Both have to produce a usable reason, because this runs on the one path
    that must never itself fail.
    """
    error = context.get("error")
    if error:
        return f"task error: {error.get('Error', 'unknown')}: {error.get('Cause', '')}"[
            :900
        ]

    for field, label in GATE_RESULT_FIELDS:
        result = (context.get(field) or {}).get("Payload") or context.get(field) or {}
        if result.get("passed") is False:
            detail = {
                key: value
                for key, value in result.items()
                if key in ("missing", "mismatched", "surviving", "failures", "ratio")
                and value
            }
            return f"{label} did not pass: {detail}"[:900]

    return "release failed without a recorded gate verdict"


def handler(event: dict, context) -> dict:
    """Dispatch registry operations to kbp.registry.store.

    Dispatches:
        readPointer    → read_active_release_id
        createRelease  → create_release
        advanceStatus  → advance_status
        promote        → promote_release
        fail           → fail_release
    """
    action = event["action"]
    # Validate before touching the environment so a bad action reports itself
    # rather than a missing variable.
    if action not in _ACTIONS:
        raise ValueError(f"unknown action: {action!r}")

    table_name = os.environ["RELEASE_TABLE"]
    client = _CLIENT

    if action == "readPointer":
        active_id = store.read_active_release_id(
            client,
            table_name=table_name,
            corpus_id=event["corpusId"],
        )
        return {"activeReleaseId": active_id}

    if action == "createRelease":
        store.create_release(
            client,
            table_name=table_name,
            corpus_id=event["corpusId"],
            release_id=event["releaseId"],
            manifest_s3_uri=event["manifestS3Uri"],
            manifest_s3_version_id=event["manifestS3VersionId"],
            parent_release_id=event.get("parentReleaseId"),
            execution_arn=event["executionArn"],
        )
        return {"status": "PREPARING"}

    if action == "advanceStatus":
        store.advance_status(
            client,
            table_name=table_name,
            corpus_id=event["corpusId"],
            release_id=event["releaseId"],
            status=event["status"],
        )
        return {"status": event["status"]}

    if action == "promote":
        store.promote_release(
            client,
            table_name=table_name,
            corpus_id=event["corpusId"],
            release_id=event["releaseId"],
            expected_previous_release_id=event.get("expectedPreviousReleaseId"),
        )
        return {"status": "ACTIVE"}

    if action == "fail":
        store.fail_release(
            client,
            table_name=table_name,
            corpus_id=event["corpusId"],
            release_id=event["releaseId"],
            reason=event.get("reason") or describe_failure(
                event.get("failureContext", {})
            ),
        )
        return {"status": "FAILED"}

    raise AssertionError(f"action {action!r} passed validation but has no branch")
