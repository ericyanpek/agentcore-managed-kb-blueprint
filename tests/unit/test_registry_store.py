import pytest

from kbp.registry import store


class FakeDynamoClient:
    """Records calls and lets a test force a conditional-check failure.

    Only writes carrying a ConditionExpression can fail this way, matching the
    real service: DynamoDB raises ConditionalCheckFailedException solely when a
    condition was supplied and evaluated false. A fake that failed unconditional
    writes too would push the production code into catching that exception around
    plain status updates, where it can never occur.
    """

    class exceptions:  # noqa: N801 - mirrors botocore client shape
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self, *, fail_condition: bool = False, existing_item: dict | None = None):
        self.fail_condition = fail_condition
        self.existing_item = existing_item
        self.calls: list[tuple[str, dict]] = []

    def _maybe_fail(self, kwargs):
        if self.fail_condition and "ConditionExpression" in kwargs:
            raise self.exceptions.ConditionalCheckFailedException("conditional failed")

    def put_item(self, **kwargs):
        self.calls.append(("put_item", kwargs))
        self._maybe_fail(kwargs)
        return {}

    def update_item(self, **kwargs):
        self.calls.append(("update_item", kwargs))
        self._maybe_fail(kwargs)
        return {}

    def get_item(self, **kwargs):
        self.calls.append(("get_item", kwargs))
        return {"Item": self.existing_item} if self.existing_item else {}


def test_keys_are_namespaced_to_allow_multiple_corpora_later():
    assert store.release_key("demo", "demo-20260817T101500Z-abcdef12") == {
        "pk": {"S": "CORPUS#demo"},
        "sk": {"S": "RELEASE#demo-20260817T101500Z-abcdef12"},
    }
    assert store.pointer_key("demo") == {
        "pk": {"S": "CORPUS#demo"},
        "sk": {"S": "POINTER"},
    }


def test_create_release_refuses_to_overwrite_an_existing_release_id():
    client = FakeDynamoClient()

    store.create_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        manifest_s3_uri="s3://registry/manifests/demo/r1.json",
        manifest_s3_version_id="v1",
        parent_release_id=None,
        execution_arn="arn:aws:states:us-east-1:1:execution:sm:exec",
    )

    _, kwargs = client.calls[0]
    assert kwargs["ConditionExpression"] == "attribute_not_exists(pk)"
    assert kwargs["Item"]["status"] == {"S": "PREPARING"}


def test_read_active_pointer_returns_none_for_first_release():
    client = FakeDynamoClient(existing_item=None)

    assert store.read_active_release_id(client, table_name="releases", corpus_id="demo") is None


def test_read_active_pointer_returns_current_release():
    client = FakeDynamoClient(
        existing_item={
            "pk": {"S": "CORPUS#demo"},
            "sk": {"S": "POINTER"},
            "activeReleaseId": {"S": "demo-20260810T101500Z-99999999"},
        }
    )

    assert (
        store.read_active_release_id(client, table_name="releases", corpus_id="demo")
        == "demo-20260810T101500Z-99999999"
    )


def test_promote_first_release_requires_absent_pointer():
    client = FakeDynamoClient()

    store.promote_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        expected_previous_release_id=None,
    )

    update_calls = [kwargs for name, kwargs in client.calls if name == "update_item"]
    pointer_call = next(
        kwargs for kwargs in update_calls if kwargs["Key"] == store.pointer_key("demo")
    )
    assert pointer_call["ConditionExpression"] == "attribute_not_exists(activeReleaseId)"


def test_promote_subsequent_release_pins_the_expected_previous_pointer():
    client = FakeDynamoClient()

    store.promote_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        expected_previous_release_id="demo-20260810T101500Z-99999999",
    )

    pointer_call = next(
        kwargs
        for name, kwargs in client.calls
        if name == "update_item" and kwargs["Key"] == store.pointer_key("demo")
    )
    assert (
        pointer_call["ConditionExpression"]
        == "attribute_not_exists(activeReleaseId) OR activeReleaseId = :expected"
    )
    assert pointer_call["ExpressionAttributeValues"][":expected"] == {
        "S": "demo-20260810T101500Z-99999999"
    }


def test_concurrent_promotion_is_rejected_rather_than_silently_overwriting():
    client = FakeDynamoClient(fail_condition=True)

    with pytest.raises(store.ConcurrentPromotionError) as error:
        store.promote_release(
            client,
            table_name="releases",
            corpus_id="demo",
            release_id="demo-20260817T101500Z-abcdef12",
            expected_previous_release_id="demo-20260810T101500Z-99999999",
        )

    assert "demo-20260810T101500Z-99999999" in str(error.value)


def test_losing_the_race_does_not_leave_an_orphan_active_record():
    """The ACTIVE mark precedes the pointer write, so a loss must undo it.

    Otherwise a release stays ACTIVE with nothing pointing at it, which reads as
    a successful publish that never took effect.
    """
    client = FakeDynamoClient(fail_condition=True)

    with pytest.raises(store.ConcurrentPromotionError):
        store.promote_release(
            client,
            table_name="releases",
            corpus_id="demo",
            release_id="demo-20260817T101500Z-abcdef12",
            expected_previous_release_id="demo-20260810T101500Z-99999999",
        )

    statuses = [
        kwargs["ExpressionAttributeValues"][":status"]["S"]
        for name, kwargs in client.calls
        if name == "update_item"
        and kwargs["Key"]
        == store.release_key("demo", "demo-20260817T101500Z-abcdef12")
    ]
    assert statuses == ["ACTIVE", "FAILED"]


def test_losing_the_race_leaves_the_previous_release_untouched():
    """The winner is still serving traffic, so it must not be marked SUPERSEDED."""
    client = FakeDynamoClient(fail_condition=True)

    with pytest.raises(store.ConcurrentPromotionError):
        store.promote_release(
            client,
            table_name="releases",
            corpus_id="demo",
            release_id="demo-20260817T101500Z-abcdef12",
            expected_previous_release_id="demo-20260810T101500Z-99999999",
        )

    previous_key = store.release_key("demo", "demo-20260810T101500Z-99999999")
    assert [kwargs for _, kwargs in client.calls if kwargs.get("Key") == previous_key] == []


def test_previous_release_is_superseded_only_after_the_pointer_moves():
    client = FakeDynamoClient()

    store.promote_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        expected_previous_release_id="demo-20260810T101500Z-99999999",
    )

    keys_in_order = [kwargs["Key"] for _, kwargs in client.calls]
    pointer_index = keys_in_order.index(store.pointer_key("demo"))
    superseded_index = keys_in_order.index(
        store.release_key("demo", "demo-20260810T101500Z-99999999")
    )
    assert pointer_index < superseded_index


def test_promotion_supersedes_the_previous_release_record():
    client = FakeDynamoClient()

    store.promote_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        expected_previous_release_id="demo-20260810T101500Z-99999999",
    )

    superseded = next(
        kwargs
        for name, kwargs in client.calls
        if name == "update_item"
        and kwargs["Key"]
        == store.release_key("demo", "demo-20260810T101500Z-99999999")
    )
    assert superseded["ExpressionAttributeValues"][":status"] == {"S": "SUPERSEDED"}


def test_pointer_is_updated_after_the_release_record_is_marked_active():
    """Ordering matters: a reader following the pointer must find an ACTIVE record."""
    client = FakeDynamoClient()

    store.promote_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        expected_previous_release_id=None,
    )

    keys_in_order = [kwargs["Key"] for _, kwargs in client.calls]
    active_index = keys_in_order.index(
        store.release_key("demo", "demo-20260817T101500Z-abcdef12")
    )
    pointer_index = keys_in_order.index(store.pointer_key("demo"))
    assert active_index < pointer_index


def test_fail_release_never_touches_the_pointer():
    client = FakeDynamoClient()

    store.fail_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        reason="gate A failed: sidecar missing",
    )

    touched_keys = [kwargs["Key"] for _, kwargs in client.calls]
    assert store.pointer_key("demo") not in touched_keys
    assert client.calls[0][1]["ExpressionAttributeValues"][":status"] == {"S": "FAILED"}


def test_advance_status_records_the_new_state():
    client = FakeDynamoClient()

    store.advance_status(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        status="INGESTING",
    )

    _, kwargs = client.calls[0]
    assert kwargs["ExpressionAttributeValues"][":status"] == {"S": "INGESTING"}


def test_unknown_status_is_rejected():
    client = FakeDynamoClient()

    with pytest.raises(ValueError, match="unknown release status"):
        store.advance_status(
            client,
            table_name="releases",
            corpus_id="demo",
            release_id="demo-20260817T101500Z-abcdef12",
            status="ALMOST_DONE",
        )
