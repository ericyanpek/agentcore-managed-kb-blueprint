import pytest

from kbp.ingestion import batching


def document(name: str) -> dict:
    return {
        "documentId": name,
        "file": f"{name}.md",
        "contentSha256": "a" * 64,
        "metadataSha256": "b" * 64,
    }


@pytest.mark.parametrize(
    ("count", "expected_batches", "expected_last_size"),
    [(0, 0, None), (1, 1, 1), (10, 1, 10), (11, 2, 1), (25, 3, 5)],
)
def test_batches_respect_the_api_limit_of_ten(
    count, expected_batches, expected_last_size
):
    documents = [document(f"doc-{index}") for index in range(count)]

    batches = batching.split_batches(documents)

    assert len(batches) == expected_batches
    assert all(len(batch) <= batching.MAX_DOCUMENTS_PER_REQUEST for batch in batches)
    if expected_batches:
        assert len(batches[-1]) == expected_last_size


def test_ingest_payload_binds_the_metadata_sidecar_explicitly():
    payload = batching.build_ingest_payload(
        documents=[document("doc-1")],
        bucket="canonical-bucket",
        prefix="canonical/demo",
    )

    assert len(payload) == 1
    entry = payload[0]
    assert entry["Content"]["DataSourceType"] == "S3"
    assert (
        entry["Content"]["S3"]["S3Location"]["Uri"]
        == "s3://canonical-bucket/canonical/demo/doc-1.md"
    )
    assert entry["Metadata"]["Type"] == "S3_LOCATION"
    assert (
        entry["Metadata"]["S3Location"]["Uri"]
        == "s3://canonical-bucket/canonical/demo/doc-1.md.metadata.json"
    )


def test_document_identifiers_use_s3_uris():
    identifiers = batching.build_document_identifiers(
        documents=[document("doc-1")],
        bucket="canonical-bucket",
        prefix="canonical/demo",
    )

    assert identifiers == [
        {
            "DataSourceType": "S3",
            "S3": {"Uri": "s3://canonical-bucket/canonical/demo/doc-1.md"},
        }
    ]


@pytest.mark.parametrize("prefix", ["canonical/demo/", "/canonical/demo", "canonical/demo"])
def test_surrounding_slashes_are_normalized_away(prefix):
    payload = batching.build_ingest_payload(
        documents=[document("doc-1")],
        bucket="canonical-bucket",
        prefix=prefix,
    )

    assert (
        payload[0]["Content"]["S3"]["S3Location"]["Uri"]
        == "s3://canonical-bucket/canonical/demo/doc-1.md"
    )


@pytest.mark.parametrize("prefix", ["", "/", "//", "   "])
def test_empty_prefix_is_rejected(prefix):
    """An empty prefix yields s3://bucket//file, addressing a key named "/file".

    That URI stays syntactically valid, so the service would ingest the wrong
    object instead of rejecting the request.
    """
    with pytest.raises(ValueError, match="non-empty key prefix"):
        batching.build_ingest_payload(
            documents=[document("doc-1")], bucket="canonical-bucket", prefix=prefix
        )


def test_nested_paths_are_preserved_in_object_keys():
    nested = {
        "documentId": "security-anti-cheat-overview",
        "file": "security/anti-cheat/overview.md",
        "contentSha256": "a" * 64,
        "metadataSha256": "b" * 64,
    }

    payload = batching.build_ingest_payload(
        documents=[nested], bucket="bucket", prefix="canonical/demo"
    )

    assert (
        payload[0]["Content"]["S3"]["S3Location"]["Uri"]
        == "s3://bucket/canonical/demo/security/anti-cheat/overview.md"
    )


def test_payload_keys_are_pascal_case_for_step_functions():
    """A Step Functions SDK integration rejects camelCase parameter names.

    boto3 accepts `content`, but the integration replies "The field \"content\" is
    not supported by Step Functions. Did you mean 'Content'?" and fails the
    execution at runtime — something no synth-time check catches.
    """
    payload = batching.build_ingest_payload(
        documents=[document("doc-1")], bucket="b", prefix="p"
    )
    identifiers = batching.build_document_identifiers(
        documents=[document("doc-1")], bucket="b", prefix="p"
    )

    def keys(value):
        if isinstance(value, dict):
            return set(value) | {k for v in value.values() for k in keys(v)}
        if isinstance(value, list):
            return {k for item in value for k in keys(item)}
        return set()

    for key in keys(payload) | keys(identifiers):
        assert key[0].isupper(), f"{key} must be PascalCase for the SDK integration"
