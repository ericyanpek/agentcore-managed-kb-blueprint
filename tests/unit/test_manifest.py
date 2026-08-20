import json
import re
from pathlib import Path

import jsonschema
import pytest

from kbp.registry import manifest

ROOT = Path(__file__).resolve().parent.parent.parent

CLIENT_TOKEN_PATTERN = re.compile(r"\A[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}\Z")

SCHEMA = json.loads(
    (ROOT / "schemas" / "release-manifest.schema.json").read_text(encoding="utf-8")
)


def corpus_manifest(document_count: int = 1) -> dict:
    return {
        "corpusId": "demo",
        "corpusSha256": "a" * 64,
        "documentCount": document_count,
        "documents": [
            {
                "documentId": f"doc-{index}",
                "file": f"doc-{index}.md",
                "contentSha256": "b" * 64,
                "metadataSha256": "c" * 64,
            }
            for index in range(document_count)
        ],
    }


def test_release_id_is_derived_from_corpus_timestamp_and_hash():
    release_id = manifest.build_release_id(
        corpus_id="demo-corpus",
        timestamp="20260817T101500Z",
        corpus_sha256="abcdef1234567890" * 4,
    )

    assert release_id == "demo-corpus-20260817T101500Z-abcdef12"


@pytest.mark.parametrize(
    ("operation", "batch_index"),
    [("ingest", 0), ("ingest", 7), ("delete", 0), ("promote", 0)],
)
def test_client_token_satisfies_api_constraints(operation, batch_index):
    """clientToken must be 33-256 chars of alphanumerics and hyphens only.

    Underscores, dots and slashes are rejected by the API, so naive string
    concatenation of a releaseId fails at runtime.
    """
    token = manifest.build_client_token(
        release_id="demo-corpus-20260817T101500Z-abcdef12",
        operation=operation,
        batch_index=batch_index,
    )

    assert 33 <= len(token) <= 256
    assert CLIENT_TOKEN_PATTERN.match(token), token


def test_client_token_is_deterministic_for_retries():
    kwargs = {
        "release_id": "demo-20260817T101500Z-abcdef12",
        "operation": "ingest",
        "batch_index": 3,
    }
    assert manifest.build_client_token(**kwargs) == manifest.build_client_token(
        **kwargs
    )


def test_client_token_differs_across_batches_and_operations():
    base = {"release_id": "demo-20260817T101500Z-abcdef12"}
    tokens = {
        manifest.build_client_token(**base, operation=operation, batch_index=index)
        for operation in ("ingest", "delete")
        for index in range(3)
    }
    assert len(tokens) == 6


def test_release_manifest_matches_published_schema():
    """Validate against the schema itself, not merely that keys are present.

    Checking key presence would miss a field emitted with the wrong type, which
    is exactly the kind of drift the published contract exists to prevent.
    """
    document = manifest.build_release_manifest(
        release_id="demo-20260817T101500Z-abcdef12",
        parent_release_id=None,
        corpus_manifest=corpus_manifest(),
        change_counts={"added": 1, "modified": 0, "deleted": 0},
        source_commit="0" * 40,
    )

    jsonschema.validate(document, SCHEMA)
    assert document["status"] == "CANDIDATE"
    assert document["parentReleaseId"] is None


def test_documents_carry_s3_version_id_slot_for_rollback():
    document = manifest.build_release_manifest(
        release_id="demo-20260817T101500Z-abcdef12",
        parent_release_id="demo-20260810T101500Z-99999999",
        corpus_manifest=corpus_manifest(),
        change_counts={"added": 0, "modified": 1, "deleted": 0},
        source_commit="0" * 40,
    )

    jsonschema.validate(document, SCHEMA)
    assert document["documents"][0]["s3VersionId"] is None


@pytest.mark.parametrize("corpus_id", ["prod_corpus", "my corpus", "a/b", "-leading"])
def test_corpus_id_outside_the_allowed_character_set_is_rejected(corpus_id):
    """An unvalidated corpus id yields a releaseId its own schema rejects.

    The value also becomes a Step Functions execution name, so an illegal
    character would otherwise surface only at StartExecution time.
    """
    with pytest.raises(ValueError, match="alphanumerics and hyphens"):
        manifest.build_release_id(
            corpus_id=corpus_id,
            timestamp="20260817T101500Z",
            corpus_sha256="a" * 64,
        )


def test_release_id_over_the_execution_name_limit_is_rejected():
    with pytest.raises(ValueError, match="execution name limit"):
        manifest.build_release_id(
            corpus_id="x" * 60,
            timestamp="20260817T101500Z",
            corpus_sha256="a" * 64,
        )


def test_derived_release_id_always_satisfies_the_published_pattern():
    release_id = manifest.build_release_id(
        corpus_id="demo-corpus",
        timestamp="20260817T101500Z",
        corpus_sha256="a" * 64,
    )

    document = manifest.build_release_manifest(
        release_id=release_id,
        parent_release_id=None,
        corpus_manifest=corpus_manifest(),
        change_counts={"added": 1, "modified": 0, "deleted": 0},
        source_commit="0" * 40,
    )

    jsonschema.validate(document, SCHEMA)
    assert len(release_id) <= manifest.MAX_RELEASE_ID_LENGTH


def test_document_count_is_taken_from_the_documents_actually_carried():
    """Rollback trusts documentCount, so it must not contradict the array."""
    inconsistent = corpus_manifest(document_count=2)
    inconsistent["documentCount"] = 5

    document = manifest.build_release_manifest(
        release_id="demo-20260817T101500Z-abcdef12",
        parent_release_id=None,
        corpus_manifest=inconsistent,
        change_counts={"added": 2, "modified": 0, "deleted": 0},
        source_commit="0" * 40,
    )

    assert document["documentCount"] == 2
