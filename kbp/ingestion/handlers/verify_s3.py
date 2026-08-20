"""Lambda handler for Gate A: S3 object and SHA verification.

`evaluate` is a directly unit-testable pure function that accepts an injected
S3 client. `handler` is the thin Lambda entry point that reads configuration
from environment variables and delegates immediately.
"""

import json
import os

import boto3

from kbp.ingestion import gates

# Built once so warm invocations reuse the connection pool.
_CLIENT = boto3.client("s3")


def _get_sha(*, client, bucket: str, key: str) -> str | None:
    """Return the recorded sha256 for a key, or None if it is not verifiably there.

    None covers both an absent object and one carrying no sha256, which happens
    when something other than this pipeline wrote it. Both are reported to the
    gate as missing rather than raised, so the release fails on the gate with a
    named object instead of on an unhandled KeyError.
    """
    try:
        response = client.head_object(Bucket=bucket, Key=key)
    except client.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return None
        raise
    return response.get("Metadata", {}).get("sha256")


def _object_exists(*, client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
    except client.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise
    return True


def evaluate(
    *,
    client,
    bucket: str,
    prefix: str,
    upserts: list[dict],
    deletions: list[dict],
) -> dict:
    """Check S3 objects against the manifest expectations.

    For each upsert, fetches the content object and its .metadata.json sidecar
    and records observed SHAs. For each deletion, checks whether the object
    still exists. Delegates all pass/fail logic to gates.evaluate_s3_consistency.
    """
    normalized_prefix = prefix.strip().strip("/")

    observed_objects: dict[str, str] = {}

    for item in upserts:
        content_key_rel = item["file"]
        sidecar_key_rel = f"{item['file']}.metadata.json"

        for rel_key in (content_key_rel, sidecar_key_rel):
            full_key = f"{normalized_prefix}/{rel_key}"
            sha = _get_sha(client=client, bucket=bucket, key=full_key)
            if sha is not None:
                observed_objects[rel_key] = sha

    # Survival is about existence, not content, so this must not go through the
    # SHA lookup: an object left behind without sha256 metadata is still left
    # behind.
    surviving = [
        item["file"]
        for item in deletions
        if _object_exists(
            client=client, bucket=bucket, key=f"{normalized_prefix}/{item['file']}"
        )
    ]

    return gates.evaluate_s3_consistency(
        expected_upserts=upserts,
        observed_objects=observed_objects,
        expected_deletions=deletions,
        surviving_deletions=surviving,
    )


def load_manifest_documents(client, *, manifest_s3_uri: str, version_id: str) -> list[dict]:
    """Read the release manifest and return its document entries.

    The manifest is the authoritative record of what a release publishes, so the
    gate reads the document list from there rather than from the execution input.
    Passing thirteen full document entries through Step Functions would work, but
    a larger corpus would approach the state payload limit.
    """
    without_scheme = manifest_s3_uri.removeprefix("s3://")
    bucket, _, key = without_scheme.partition("/")
    response = client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    return json.loads(response["Body"].read())["documents"]


def handler(event: dict, context) -> dict:
    """Lambda entry point for the S3 consistency gate.

    Reads the upsert set from the release manifest named in the event, and the
    deletion set from the event itself — deleted files are by definition absent
    from the manifest this release publishes.
    """
    upserts = load_manifest_documents(
        _CLIENT,
        manifest_s3_uri=event["manifestS3Uri"],
        version_id=event["manifestS3VersionId"],
    )

    return evaluate(
        client=_CLIENT,
        bucket=os.environ["CANONICAL_BUCKET"],
        prefix=event["canonicalPrefix"],
        upserts=upserts,
        deletions=event.get("deletions", []),
    )
