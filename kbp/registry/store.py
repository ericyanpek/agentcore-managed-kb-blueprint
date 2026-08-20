"""Release registry persistence: DynamoDB state and pointer, S3 manifests.

DynamoDB owns release status and the active pointer. S3 owns manifest content.
This split means a deleted table still leaves every published manifest
recoverable from the versioned bucket.
"""

VALID_STATUSES = frozenset(
    {"PREPARING", "INGESTING", "TESTING", "ACTIVE", "SUPERSEDED", "FAILED"}
)


class ConcurrentPromotionError(RuntimeError):
    """Raised when the active pointer moved while this release was in flight."""


def release_key(corpus_id: str, release_id: str) -> dict:
    return {"pk": {"S": f"CORPUS#{corpus_id}"}, "sk": {"S": f"RELEASE#{release_id}"}}


def pointer_key(corpus_id: str) -> dict:
    return {"pk": {"S": f"CORPUS#{corpus_id}"}, "sk": {"S": "POINTER"}}


def create_release(
    client,
    *,
    table_name: str,
    corpus_id: str,
    release_id: str,
    manifest_s3_uri: str,
    manifest_s3_version_id: str,
    parent_release_id: str | None,
    execution_arn: str,
) -> None:
    """Create the release record, refusing to overwrite an existing releaseId."""
    item = {
        **release_key(corpus_id, release_id),
        "corpusId": {"S": corpus_id},
        "releaseId": {"S": release_id},
        "status": {"S": "PREPARING"},
        "manifestS3Uri": {"S": manifest_s3_uri},
        "manifestS3VersionId": {"S": manifest_s3_version_id},
        "executionArn": {"S": execution_arn},
        "parentReleaseId": (
            {"S": parent_release_id} if parent_release_id else {"NULL": True}
        ),
    }
    client.put_item(
        TableName=table_name,
        Item=item,
        ConditionExpression="attribute_not_exists(pk)",
    )


def read_active_release_id(client, *, table_name: str, corpus_id: str) -> str | None:
    """Read the currently active releaseId, or None before the first release."""
    response = client.get_item(
        TableName=table_name, Key=pointer_key(corpus_id), ConsistentRead=True
    )
    item = response.get("Item")
    if not item:
        return None
    return item["activeReleaseId"]["S"]


def advance_status(
    client, *, table_name: str, corpus_id: str, release_id: str, status: str
) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown release status: {status}")
    client.update_item(
        TableName=table_name,
        Key=release_key(corpus_id, release_id),
        UpdateExpression="SET #status = :status",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": {"S": status}},
    )


def fail_release(
    client, *, table_name: str, corpus_id: str, release_id: str, reason: str
) -> None:
    """Mark a release FAILED. Deliberately never touches the pointer."""
    client.update_item(
        TableName=table_name,
        Key=release_key(corpus_id, release_id),
        UpdateExpression="SET #status = :status, failureReason = :reason",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": {"S": "FAILED"},
            ":reason": {"S": reason},
        },
    )


def promote_release(
    client,
    *,
    table_name: str,
    corpus_id: str,
    release_id: str,
    expected_previous_release_id: str | None,
) -> None:
    """Atomically make this release active.

    Three writes, ordered so that every intermediate state is safe to read:

    1. Mark this release ACTIVE. A reader following the pointer must never land
       on a record that is not yet active, so this precedes the pointer move.
    2. Move the pointer under a condition pinned to what the execution observed
       at start. A concurrent pipeline loses here and is told so.
    3. Mark the previous release SUPERSEDED, but only once the pointer actually
       moved. Doing it earlier would label the still-live release as superseded
       if the pointer write then failed.
    """
    advance_status(
        client,
        table_name=table_name,
        corpus_id=corpus_id,
        release_id=release_id,
        status="ACTIVE",
    )

    if expected_previous_release_id:
        condition = (
            "attribute_not_exists(activeReleaseId) OR activeReleaseId = :expected"
        )
        values = {
            ":active": {"S": release_id},
            ":expected": {"S": expected_previous_release_id},
        }
    else:
        condition = "attribute_not_exists(activeReleaseId)"
        values = {":active": {"S": release_id}}

    try:
        client.update_item(
            TableName=table_name,
            Key=pointer_key(corpus_id),
            UpdateExpression="SET activeReleaseId = :active",
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
        )
    except client.exceptions.ConditionalCheckFailedException as error:
        # Undo step 1 so a lost race does not leave a release marked ACTIVE that
        # nothing points to.
        fail_release(
            client,
            table_name=table_name,
            corpus_id=corpus_id,
            release_id=release_id,
            reason=(
                f"lost the promotion race; active pointer for {corpus_id} is no "
                f"longer {expected_previous_release_id}"
            ),
        )
        raise ConcurrentPromotionError(
            f"active pointer for {corpus_id} is no longer "
            f"{expected_previous_release_id}; another release won the race"
        ) from error

    if expected_previous_release_id:
        advance_status(
            client,
            table_name=table_name,
            corpus_id=corpus_id,
            release_id=expected_previous_release_id,
            status="SUPERSEDED",
        )
