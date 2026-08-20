"""Tests for the three Lambda handler modules.

All tests run offline — no AWS credentials required.

TDD order: tests were written before the implementation files existed. Each
test describes a requirement from the architecture: handlers are thin adapters
that read I/O, delegate to pure functions, and return the result.
"""

import io
import os

import pytest

# ---------------------------------------------------------------------------
# Fake S3 client for verify_s3 tests
# ---------------------------------------------------------------------------


class FakeClientError(Exception):
    """Minimal botocore ClientError shape."""

    def __init__(self, code: str):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class _FakeExceptions:
    ClientError = FakeClientError


class FakeS3Client:
    """Fake S3 client that serves pre-loaded head_object responses.

    `objects` maps key -> sha256 string. A key absent from the mapping raises
    NoSuchKey; a key mapped to None exists but carries no sha256 metadata, which
    is what an object written by something other than this pipeline looks like.
    Every requested key is recorded so a test can assert the prefix was applied.
    """

    exceptions = _FakeExceptions

    def __init__(self, objects: dict, *, error_code: str | None = None):
        self._objects = objects
        self._error_code = error_code
        self.requested_keys: list[str] = []

    def head_object(self, *, Bucket: str, Key: str):
        self.requested_keys.append(Key)
        if self._error_code:
            raise FakeClientError(self._error_code)
        if Key not in self._objects:
            raise FakeClientError("NoSuchKey")
        sha = self._objects[Key]
        return {"Metadata": {} if sha is None else {"sha256": sha}}


# ---------------------------------------------------------------------------
# verify_s3.evaluate
# ---------------------------------------------------------------------------


class TestVerifyS3Evaluate:
    """verify_s3.evaluate is a pure-function adapter callable without Lambda."""

    def _make_client(self, objects: dict) -> FakeS3Client:
        return FakeS3Client(objects)

    def test_missing_sidecar_fails(self):
        """A missing .metadata.json is reported in the missing list."""
        from kbp.ingestion.handlers import verify_s3

        client = self._make_client(
            {
                "corpus/2024/doc.md": "a" * 64,
                # .metadata.json intentionally absent
            }
        )
        upserts = [
            {
                "file": "2024/doc.md",
                "contentSha256": "a" * 64,
                "metadataSha256": "b" * 64,
            }
        ]
        result = verify_s3.evaluate(
            client=client,
            bucket="corpus",
            prefix="corpus",
            upserts=upserts,
            deletions=[],
        )

        assert result["passed"] is False
        assert "2024/doc.md.metadata.json" in result["missing"]

    def test_surviving_deletion_is_flagged(self):
        """An object that should have been deleted but still exists blocks the release."""
        from kbp.ingestion.handlers import verify_s3

        # The object to be deleted is still present in the bucket
        client = self._make_client({"corpus/gone.md": "c" * 64})
        result = verify_s3.evaluate(
            client=client,
            bucket="corpus",
            prefix="corpus",
            upserts=[],
            deletions=[{"file": "gone.md"}],
        )

        assert result["passed"] is False
        assert "gone.md" in result["surviving"]

    def test_consistent_state_passes(self):
        """All upserts present with correct SHAs and all deletions gone."""
        from kbp.ingestion.handlers import verify_s3

        client = self._make_client(
            {
                "corpus/doc.md": "a" * 64,
                "corpus/doc.md.metadata.json": "b" * 64,
                # gone.md is absent — correct for a deletion
            }
        )
        upserts = [
            {
                "file": "doc.md",
                "contentSha256": "a" * 64,
                "metadataSha256": "b" * 64,
            }
        ]
        result = verify_s3.evaluate(
            client=client,
            bucket="corpus",
            prefix="corpus",
            upserts=upserts,
            deletions=[{"file": "gone.md"}],
        )

        assert result["passed"] is True
        assert result["missing"] == []
        assert result["surviving"] == []

    def test_nested_files_are_looked_up_under_the_prefix(self):
        """A dropped prefix would query the wrong keys and report all missing."""
        from kbp.ingestion.handlers import verify_s3

        client = self._make_client(
            {
                "canonical/demo/security/overview.md": "a" * 64,
                "canonical/demo/security/overview.md.metadata.json": "b" * 64,
            }
        )

        result = verify_s3.evaluate(
            client=client,
            bucket="corpus",
            prefix="canonical/demo",
            upserts=[
                {
                    "file": "security/overview.md",
                    "contentSha256": "a" * 64,
                    "metadataSha256": "b" * 64,
                }
            ],
            deletions=[],
        )

        assert result["passed"] is True
        assert client.requested_keys == [
            "canonical/demo/security/overview.md",
            "canonical/demo/security/overview.md.metadata.json",
        ]

    def test_object_without_recorded_sha_is_reported_missing_not_crashed(self):
        """An object written outside this pipeline carries no sha256 metadata.

        Reading it unguarded raised KeyError, turning a gate failure into an
        unhandled Lambda error that named no object.
        """
        from kbp.ingestion.handlers import verify_s3

        client = self._make_client(
            {"corpus/doc.md": None, "corpus/doc.md.metadata.json": "b" * 64}
        )

        result = verify_s3.evaluate(
            client=client,
            bucket="corpus",
            prefix="corpus",
            upserts=[
                {
                    "file": "doc.md",
                    "contentSha256": "a" * 64,
                    "metadataSha256": "b" * 64,
                }
            ],
            deletions=[],
        )

        assert result["passed"] is False
        assert result["missing"] == ["doc.md"]

    def test_surviving_deletion_is_detected_without_sha_metadata(self):
        """Survival is about existence; a leftover object counts either way."""
        from kbp.ingestion.handlers import verify_s3

        client = self._make_client({"corpus/gone.md": None})

        result = verify_s3.evaluate(
            client=client,
            bucket="corpus",
            prefix="corpus",
            upserts=[],
            deletions=[{"file": "gone.md"}],
        )

        assert result["passed"] is False
        assert result["surviving"] == ["gone.md"]

    def test_access_denied_propagates_rather_than_reading_as_absent(self):
        """Reporting a permissions failure as a missing object would be a lie."""
        from kbp.ingestion.handlers import verify_s3

        client = FakeS3Client({}, error_code="AccessDenied")

        with pytest.raises(FakeClientError):
            verify_s3.evaluate(
                client=client,
                bucket="corpus",
                prefix="corpus",
                upserts=[
                    {
                        "file": "doc.md",
                        "contentSha256": "a" * 64,
                        "metadataSha256": "b" * 64,
                    }
                ],
                deletions=[],
            )


# ---------------------------------------------------------------------------
# check_gates.handler
# ---------------------------------------------------------------------------


class TestCheckGatesDeletionRatio:
    def test_deletion_ratio_gate_passes_below_threshold(self):
        from kbp.ingestion.handlers import check_gates

        result = check_gates.handler(
            {
                "gate": "deletionRatio",
                "deletedCount": 4,
                "previousDocumentCount": 50,
                "threshold": 0.5,
            },
            None,
        )

        assert result["passed"] is True

    def test_deletion_ratio_gate_fails_above_threshold(self):
        from kbp.ingestion.handlers import check_gates

        result = check_gates.handler(
            {
                "gate": "deletionRatio",
                "deletedCount": 40,
                "previousDocumentCount": 50,
                "threshold": 0.5,
            },
            None,
        )

        assert result["passed"] is False

    def test_deletion_ratio_allows_bulk_when_flag_set(self):
        from kbp.ingestion.handlers import check_gates

        result = check_gates.handler(
            {
                "gate": "deletionRatio",
                "deletedCount": 50,
                "previousDocumentCount": 50,
                "threshold": 0.5,
                "allowBulkDeletion": True,
            },
            None,
        )

        assert result["passed"] is True
        assert result["overridden"] is True


class TestCheckGatesIngestStatus:
    def test_ingest_status_aggregates_and_reports_partial_failure(self):
        """PARTIALLY_INDEXED is a terminal failure, not a success."""
        from kbp.ingestion.handlers import check_gates

        result = check_gates.handler(
            {
                "gate": "ingestStatus",
                "documentDetails": [
                    {
                        "identifier": {"s3": {"uri": "s3://b/doc.md"}},
                        "status": "PARTIALLY_INDEXED",
                    }
                ],
            },
            None,
        )

        assert result["settled"] is True
        assert result["passed"] is False
        assert len(result["failures"]) == 1

    def test_ingest_status_flattens_nested_s3_identifier(self):
        """The API returns nested identifiers; the gate expects plain strings."""
        from kbp.ingestion.handlers import check_gates

        result = check_gates.handler(
            {
                "gate": "ingestStatus",
                "documentDetails": [
                    {
                        "identifier": {"s3": {"uri": "s3://bucket/doc.md"}},
                        "status": "PARTIALLY_INDEXED",
                    }
                ],
            },
            None,
        )

        # The failure entry must carry a string identifier, not a dict
        failure = result["failures"][0]
        assert isinstance(failure["identifier"], str)
        assert failure["identifier"] == "s3://bucket/doc.md"

    def test_ingest_status_flattens_custom_identifier(self):
        """Custom document identifiers are also flattened to a plain string."""
        from kbp.ingestion.handlers import check_gates

        result = check_gates.handler(
            {
                "gate": "ingestStatus",
                "documentDetails": [
                    {
                        "identifier": {"custom": {"id": "my-custom-doc"}},
                        "status": "FAILED",
                    }
                ],
            },
            None,
        )

        failure = result["failures"][0]
        assert isinstance(failure["identifier"], str)
        assert failure["identifier"] == "my-custom-doc"

    def test_ingest_status_passes_expected_count_through(self):
        """A short status response must fail when expectedCount is provided."""
        from kbp.ingestion.handlers import check_gates

        # Only 1 document detail returned, but expectedCount says 5
        result = check_gates.handler(
            {
                "gate": "ingestStatus",
                "expectedCount": 5,
                "documentDetails": [
                    {
                        "identifier": {"s3": {"uri": "s3://b/doc.md"}},
                        "status": "INDEXED",
                    }
                ],
            },
            None,
        )

        assert result["passed"] is False
        assert result["settled"] is True

    def test_ingest_status_all_indexed_passes(self):
        from kbp.ingestion.handlers import check_gates

        result = check_gates.handler(
            {
                "gate": "ingestStatus",
                "documentDetails": [
                    {
                        "identifier": {"s3": {"uri": "s3://b/doc.md"}},
                        "status": "INDEXED",
                    }
                ],
            },
            None,
        )

        assert result["passed"] is True


class TestCheckGatesDeleteStatus:
    def test_delete_status_passes_expected_count_through(self):
        """A short delete-status response must fail when expectedCount is given."""
        from kbp.ingestion.handlers import check_gates

        result = check_gates.handler(
            {
                "gate": "deleteStatus",
                "expectedCount": 3,
                "documentDetails": [
                    {
                        "identifier": {"s3": {"uri": "s3://b/doc.md"}},
                        "status": "NOT_FOUND",
                    }
                ],
            },
            None,
        )

        assert result["passed"] is False

    def test_delete_status_not_found_passes(self):
        from kbp.ingestion.handlers import check_gates

        result = check_gates.handler(
            {
                "gate": "deleteStatus",
                "documentDetails": [
                    {
                        "identifier": {"s3": {"uri": "s3://b/doc.md"}},
                        "status": "NOT_FOUND",
                    }
                ],
            },
            None,
        )

        assert result["passed"] is True


class TestCheckGatesSmokeRetrieval:
    def test_smoke_retrieval_present_hit(self):
        from kbp.ingestion.handlers import check_gates

        result = check_gates.handler(
            {
                "gate": "smokeRetrieval",
                "expectation": "present",
                "retrievedDocumentIds": ["doc-1"],
                "target": "doc-1",
            },
            None,
        )

        assert result["passed"] is True

    def test_smoke_retrieval_absent_success(self):
        from kbp.ingestion.handlers import check_gates

        result = check_gates.handler(
            {
                "gate": "smokeRetrieval",
                "expectation": "absent",
                "retrievedDocumentIds": [],
                "target": "removed-doc",
            },
            None,
        )

        assert result["passed"] is True


class TestCheckGatesUnknown:
    def test_unknown_gate_raises_value_error(self):
        from kbp.ingestion.handlers import check_gates

        with pytest.raises(ValueError, match="unknown gate"):
            check_gates.handler({"gate": "nonexistentGate"}, None)


class TestCheckGatesNoBoto3:
    def test_check_gates_module_does_not_import_boto3(self):
        """check_gates must be pure transformation — no AWS access at all.

        Checks the module's imports rather than a substring of its source, so a
        comment mentioning boto3 does not fail the test while an actual import
        would.
        """
        import ast
        import inspect

        from kbp.ingestion.handlers import check_gates

        tree = ast.parse(inspect.getsource(check_gates))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert "boto3" not in imported, "check_gates.py must not import boto3"
        assert "botocore" not in imported


# ---------------------------------------------------------------------------
# registry_ops.handler
# ---------------------------------------------------------------------------


class TestRegistryOpsUnknownAction:
    def test_unknown_action_raises_value_error(self):
        from kbp.ingestion.handlers import registry_ops

        with pytest.raises(ValueError, match="unknown action"):
            registry_ops.handler({"action": "doSomethingUnknown"}, None)

    def test_action_is_validated_before_the_environment_is_read(self, monkeypatch):
        """A bad action should name itself, not surface as a config error."""
        from kbp.ingestion.handlers import registry_ops

        monkeypatch.delenv("RELEASE_TABLE", raising=False)

        with pytest.raises(ValueError, match="unknown action"):
            registry_ops.handler({"action": "nope"}, None)


class TestRegistryOpsDispatch:
    """Cover the event-to-keyword translation for every real action.

    The store functions have their own tests, but those never see the event dict.
    A misspelled event key would reach production uncaught without these.
    """

    @pytest.fixture
    def recorder(self, monkeypatch):
        from kbp.ingestion.handlers import registry_ops

        calls: list[tuple[str, dict]] = []

        def record(name, result=None):
            def fake(client, **kwargs):
                calls.append((name, kwargs))
                return result

            return fake

        monkeypatch.setenv("RELEASE_TABLE", "releases")
        monkeypatch.setattr(
            registry_ops.store,
            "read_active_release_id",
            record("read_active_release_id", "demo-20260810T101500Z-99999999"),
        )
        for name in ("create_release", "advance_status", "promote_release", "fail_release"):
            monkeypatch.setattr(registry_ops.store, name, record(name))
        return calls

    def test_read_pointer_returns_the_active_release(self, recorder):
        from kbp.ingestion.handlers import registry_ops

        result = registry_ops.handler(
            {"action": "readPointer", "corpusId": "demo"}, None
        )

        assert result == {"activeReleaseId": "demo-20260810T101500Z-99999999"}
        assert recorder[0][1] == {"table_name": "releases", "corpus_id": "demo"}

    def test_create_release_forwards_every_manifest_field(self, recorder):
        from kbp.ingestion.handlers import registry_ops

        result = registry_ops.handler(
            {
                "action": "createRelease",
                "corpusId": "demo",
                "releaseId": "demo-20260817T101500Z-abcdef12",
                "manifestS3Uri": "s3://registry/manifests/demo/r1.json",
                "manifestS3VersionId": "v1",
                "parentReleaseId": None,
                "executionArn": "arn:aws:states:us-east-1:1:execution:sm:exec",
            },
            None,
        )

        assert result == {"status": "PREPARING"}
        name, kwargs = recorder[0]
        assert name == "create_release"
        assert kwargs == {
            "table_name": "releases",
            "corpus_id": "demo",
            "release_id": "demo-20260817T101500Z-abcdef12",
            "manifest_s3_uri": "s3://registry/manifests/demo/r1.json",
            "manifest_s3_version_id": "v1",
            "parent_release_id": None,
            "execution_arn": "arn:aws:states:us-east-1:1:execution:sm:exec",
        }

    def test_advance_status_echoes_the_requested_status(self, recorder):
        from kbp.ingestion.handlers import registry_ops

        result = registry_ops.handler(
            {
                "action": "advanceStatus",
                "corpusId": "demo",
                "releaseId": "demo-20260817T101500Z-abcdef12",
                "status": "INGESTING",
            },
            None,
        )

        assert result == {"status": "INGESTING"}
        assert recorder[0][1]["status"] == "INGESTING"

    def test_promote_forwards_the_expected_previous_pointer(self, recorder):
        from kbp.ingestion.handlers import registry_ops

        result = registry_ops.handler(
            {
                "action": "promote",
                "corpusId": "demo",
                "releaseId": "demo-20260817T101500Z-abcdef12",
                "expectedPreviousReleaseId": "demo-20260810T101500Z-99999999",
            },
            None,
        )

        assert result == {"status": "ACTIVE"}
        assert (
            recorder[0][1]["expected_previous_release_id"]
            == "demo-20260810T101500Z-99999999"
        )

    def test_promote_treats_an_absent_previous_pointer_as_first_release(self, recorder):
        from kbp.ingestion.handlers import registry_ops

        registry_ops.handler(
            {
                "action": "promote",
                "corpusId": "demo",
                "releaseId": "demo-20260817T101500Z-abcdef12",
            },
            None,
        )

        assert recorder[0][1]["expected_previous_release_id"] is None

    def test_fail_forwards_the_reason(self, recorder):
        from kbp.ingestion.handlers import registry_ops

        result = registry_ops.handler(
            {
                "action": "fail",
                "corpusId": "demo",
                "releaseId": "demo-20260817T101500Z-abcdef12",
                "reason": "gate A failed: sidecar missing",
            },
            None,
        )

        assert result == {"status": "FAILED"}
        assert recorder[0][1]["reason"] == "gate A failed: sidecar missing"

    def test_concurrent_promotion_error_reaches_the_state_machine(self, monkeypatch):
        """The state machine must see this error to route to its failure branch."""
        from kbp.ingestion.handlers import registry_ops
        from kbp.registry import store

        monkeypatch.setenv("RELEASE_TABLE", "releases")

        def raise_conflict(client, **kwargs):
            raise store.ConcurrentPromotionError("another release won")

        monkeypatch.setattr(registry_ops.store, "promote_release", raise_conflict)

        with pytest.raises(store.ConcurrentPromotionError):
            registry_ops.handler(
                {
                    "action": "promote",
                    "corpusId": "demo",
                    "releaseId": "demo-20260817T101500Z-abcdef12",
                },
                None,
            )


class TestVerifyS3HandlerContract:
    """The handler must accept exactly what the state machine sends it.

    A field-name mismatch here passed every unit test and only surfaced on a real
    execution, because the tests exercised `evaluate` while the state machine calls
    `handler`. These tests assert the wire contract itself.
    """

    def _state_machine_payload(self) -> dict:
        """Mirror the payload built in infra/lib/state-machine.ts for Gate A."""
        return {
            "manifestS3Uri": "s3://registry/manifests/demo-r1.json",
            "manifestS3VersionId": "v1",
            "canonicalPrefix": "canonical/demo",
            "deletions": [],
        }

    def test_handler_accepts_the_state_machine_payload(self, monkeypatch):
        import json

        from kbp.ingestion.handlers import verify_s3

        manifest = {
            "documents": [
                {
                    "file": "doc.md",
                    "contentSha256": "a" * 64,
                    "metadataSha256": "b" * 64,
                }
            ]
        }

        class FakeClient:
            exceptions = _FakeExceptions

            def get_object(self, *, Bucket, Key, VersionId):
                assert (Bucket, Key, VersionId) == (
                    "registry",
                    "manifests/demo-r1.json",
                    "v1",
                )
                return {"Body": io.BytesIO(json.dumps(manifest).encode())}

            def head_object(self, *, Bucket, Key):
                shas = {
                    "canonical/demo/doc.md": "a" * 64,
                    "canonical/demo/doc.md.metadata.json": "b" * 64,
                }
                if Key not in shas:
                    raise FakeClientError("NoSuchKey")
                return {"Metadata": {"sha256": shas[Key]}}

        monkeypatch.setenv("CANONICAL_BUCKET", "canonical")
        monkeypatch.setattr(verify_s3, "_CLIENT", FakeClient())

        result = verify_s3.handler(self._state_machine_payload(), None)

        assert result["passed"] is True

    def test_payload_field_names_match_the_state_machine_definition(self):
        """Guard against the two sides drifting apart again."""
        import re
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent.parent
            / "infra"
            / "lib"
            / "state-machine.ts"
        ).read_text(encoding="utf-8")

        gate_a = source[source.index("'VerifyS3Consistency'") :]
        payload = gate_a[
            gate_a.index("fromObject({") : gate_a.index("resultPath")
        ]
        sent = set(re.findall(r"^\s+([a-zA-Z0-9]+):\s", payload, re.M))

        assert sent == set(self._state_machine_payload())
