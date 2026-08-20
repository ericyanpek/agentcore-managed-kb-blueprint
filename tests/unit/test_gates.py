import json

import pytest

from kbp.ingestion import gates


class TestDeletionRatio:
    """The denominator must be the pre-release document count.

    Using the post-release count makes a full deletion compute as 0%, which
    disables the guard entirely — this was a real defect in the previous
    implementation.
    """

    def test_full_deletion_is_reported_as_one_hundred_percent(self):
        result = gates.evaluate_deletion_ratio(
            deleted_count=50, previous_document_count=50, threshold=0.5
        )

        assert result["ratio"] == 1.0
        assert result["passed"] is False

    def test_ratio_below_threshold_passes(self):
        result = gates.evaluate_deletion_ratio(
            deleted_count=4, previous_document_count=50, threshold=0.5
        )

        assert result["ratio"] == pytest.approx(0.08)
        assert result["passed"] is True

    def test_ratio_exactly_at_threshold_passes(self):
        result = gates.evaluate_deletion_ratio(
            deleted_count=25, previous_document_count=50, threshold=0.5
        )

        assert result["ratio"] == 0.5
        assert result["passed"] is True

    def test_initial_release_has_no_denominator_and_passes(self):
        result = gates.evaluate_deletion_ratio(
            deleted_count=0, previous_document_count=0, threshold=0.5
        )

        assert result["ratio"] == 0.0
        assert result["passed"] is True

    def test_deleting_from_empty_corpus_is_inconsistent(self):
        with pytest.raises(ValueError, match="cannot delete"):
            gates.evaluate_deletion_ratio(
                deleted_count=3, previous_document_count=0, threshold=0.5
            )

    def test_override_allows_bulk_deletion(self):
        result = gates.evaluate_deletion_ratio(
            deleted_count=50,
            previous_document_count=50,
            threshold=0.5,
            allow_bulk_deletion=True,
        )

        assert result["ratio"] == 1.0
        assert result["passed"] is True
        assert result["overridden"] is True

    @pytest.mark.parametrize("threshold", [1.5, -0.1, 2.0])
    def test_threshold_outside_the_unit_interval_is_rejected(self, threshold):
        """A ratio can only be 0..1, so an out-of-range threshold is inert.

        1.5 as a typo for 0.15 would pass every deletion; a negative value would
        block every release. Both are silent, so reject the configuration.
        """
        with pytest.raises(ValueError, match="within \\[0, 1\\]"):
            gates.evaluate_deletion_ratio(
                deleted_count=1, previous_document_count=10, threshold=threshold
            )


class TestIngestTerminalStatus:
    """Only INDEXED signals full success.

    PARTIALLY_INDEXED means some chunks failed: the content is incomplete but
    the API reports no error. Treating it as success is silent data corruption.
    """

    @pytest.mark.parametrize(
        "status",
        [
            "PARTIALLY_INDEXED",
            "METADATA_PARTIALLY_INDEXED",
            "METADATA_UPDATE_FAILED",
            "FAILED",
            "IGNORED",
            "NOT_FOUND",
        ],
    )
    def test_non_indexed_terminal_states_are_failures(self, status):
        result = gates.evaluate_ingest_statuses(
            [{"identifier": "doc-1", "status": status}]
        )

        assert result["settled"] is True
        assert result["passed"] is False
        assert result["failures"] == [{"identifier": "doc-1", "status": status}]

    def test_all_indexed_passes(self):
        result = gates.evaluate_ingest_statuses(
            [
                {"identifier": "doc-1", "status": "INDEXED"},
                {"identifier": "doc-2", "status": "INDEXED"},
            ]
        )

        assert result["settled"] is True
        assert result["passed"] is True
        assert result["failures"] == []

    @pytest.mark.parametrize("status", ["PENDING", "STARTING", "IN_PROGRESS"])
    def test_in_flight_states_are_not_settled(self, status):
        result = gates.evaluate_ingest_statuses(
            [
                {"identifier": "doc-1", "status": "INDEXED"},
                {"identifier": "doc-2", "status": status},
            ]
        )

        assert result["settled"] is False
        assert result["passed"] is False
        assert result["pending"] == ["doc-2"]

    @pytest.mark.parametrize("status", ["DELETING", "DELETE_IN_PROGRESS"])
    def test_delete_phase_states_fail_an_upsert_instead_of_polling_on(self, status):
        """An upserted document reporting a delete state is contradictory.

        Classifying it as in-flight would spin the poller until its attempt
        budget ran out instead of surfacing the conflict.
        """
        result = gates.evaluate_ingest_statuses(
            [{"identifier": "doc-1", "status": status}]
        )

        assert result["settled"] is True
        assert result["passed"] is False

    def test_unknown_status_is_treated_as_failure_not_as_pending(self):
        """An unrecognized status must not make the poller spin forever."""
        result = gates.evaluate_ingest_statuses(
            [{"identifier": "doc-1", "status": "SOMETHING_NEW"}]
        )

        assert result["settled"] is True
        assert result["passed"] is False

    def test_empty_status_list_is_settled_and_passing(self):
        result = gates.evaluate_ingest_statuses([])

        assert result["settled"] is True
        assert result["passed"] is True

    def test_short_status_response_fails_instead_of_passing_vacuously(self):
        """A truncated response must not pass having observed nothing.

        Submitting five documents and receiving one status is evidence of a
        pagination or identifier bug, not of a successful release.
        """
        result = gates.evaluate_ingest_statuses(
            [{"identifier": "doc-1", "status": "INDEXED"}], expected_count=5
        )

        assert result["settled"] is True
        assert result["passed"] is False

    def test_matching_count_still_passes(self):
        result = gates.evaluate_ingest_statuses(
            [{"identifier": "doc-1", "status": "INDEXED"}], expected_count=1
        )

        assert result["passed"] is True


class TestDeleteTerminalStatus:
    """Deletion is confirmed only by NOT_FOUND, not by the 202 response."""

    def test_not_found_confirms_deletion(self):
        result = gates.evaluate_delete_statuses(
            [{"identifier": "doc-1", "status": "NOT_FOUND"}]
        )

        assert result["settled"] is True
        assert result["passed"] is True

    @pytest.mark.parametrize(
        "status", ["INDEXED", "DELETING", "DELETE_IN_PROGRESS"]
    )
    def test_deletion_in_flight_is_not_settled(self, status):
        """INDEXED is in-flight here, not a failure.

        A document polled right after the delete call is issued still reports
        INDEXED. Treating that as terminal would fail a valid delete before the
        transition to DELETING propagates.
        """
        result = gates.evaluate_delete_statuses(
            [{"identifier": "doc-1", "status": status}]
        )

        assert result["settled"] is False
        assert result["passed"] is False

    @pytest.mark.parametrize(
        "status",
        [
            "FAILED",
            "IGNORED",
            "PARTIALLY_INDEXED",
            "METADATA_PARTIALLY_INDEXED",
            "METADATA_UPDATE_FAILED",
            "PENDING",
            "STARTING",
            "IN_PROGRESS",
        ],
    )
    def test_statuses_a_deletion_should_never_reach_are_failures(self, status):
        """A deleting document reports INDEXED, DELETING or NOT_FOUND.

        Anything else means the poll described some other operation, so settle as
        a failure rather than looping until the attempt budget runs out.
        """
        result = gates.evaluate_delete_statuses(
            [{"identifier": "doc-1", "status": status}]
        )

        assert result["settled"] is True
        assert result["passed"] is False


class TestS3Consistency:
    def test_missing_sidecar_fails(self):
        result = gates.evaluate_s3_consistency(
            expected_upserts=[
                {"file": "doc.md", "contentSha256": "a" * 64, "metadataSha256": "b" * 64}
            ],
            observed_objects={"doc.md": "a" * 64},
            expected_deletions=[],
            surviving_deletions=[],
        )

        assert result["passed"] is False
        assert "doc.md.metadata.json" in result["missing"]

    def test_content_hash_mismatch_fails(self):
        result = gates.evaluate_s3_consistency(
            expected_upserts=[
                {"file": "doc.md", "contentSha256": "a" * 64, "metadataSha256": "b" * 64}
            ],
            observed_objects={"doc.md": "z" * 64, "doc.md.metadata.json": "b" * 64},
            expected_deletions=[],
            surviving_deletions=[],
        )

        assert result["passed"] is False
        assert result["mismatched"] == ["doc.md"]

    def test_surviving_deleted_object_fails(self):
        """A failed S3 deletion must block promotion, not be ignored."""
        result = gates.evaluate_s3_consistency(
            expected_upserts=[],
            observed_objects={},
            expected_deletions=[{"file": "gone.md"}],
            surviving_deletions=["gone.md"],
        )

        assert result["passed"] is False
        assert result["surviving"] == ["gone.md"]

    def test_fully_consistent_state_passes(self):
        result = gates.evaluate_s3_consistency(
            expected_upserts=[
                {"file": "doc.md", "contentSha256": "a" * 64, "metadataSha256": "b" * 64}
            ],
            observed_objects={"doc.md": "a" * 64, "doc.md.metadata.json": "b" * 64},
            expected_deletions=[{"file": "gone.md"}],
            surviving_deletions=[],
        )

        assert result["passed"] is True

    def test_deleted_object_still_observed_fails_without_caller_help(self):
        """The gate cross-checks deletions itself rather than trusting the caller.

        A caller that computes surviving_deletions incorrectly would otherwise
        promote a release whose objects are still in the bucket.
        """
        result = gates.evaluate_s3_consistency(
            expected_upserts=[],
            observed_objects={"gone.md": "a" * 64},
            expected_deletions=[{"file": "gone.md"}],
            surviving_deletions=[],
        )

        assert result["passed"] is False
        assert result["surviving"] == ["gone.md"]

    def test_repeated_upsert_entries_do_not_duplicate_the_report(self):
        duplicated = {
            "file": "doc.md",
            "contentSha256": "a" * 64,
            "metadataSha256": "b" * 64,
        }

        result = gates.evaluate_s3_consistency(
            expected_upserts=[duplicated, duplicated],
            observed_objects={},
            expected_deletions=[],
            surviving_deletions=[],
        )

        assert result["missing"] == ["doc.md", "doc.md.metadata.json"]


class TestSmokeRetrieval:
    def test_upsert_smoke_requires_a_hit(self):
        result = gates.evaluate_smoke_retrieval(
            expectation="present", retrieved_document_ids=["doc-1"], target="doc-1"
        )

        assert result["passed"] is True

    def test_missing_upsert_hit_fails(self):
        result = gates.evaluate_smoke_retrieval(
            expectation="present", retrieved_document_ids=[], target="doc-1"
        )

        assert result["passed"] is False

    def test_deleted_document_must_not_be_retrievable(self):
        """A delete-only release verifies absence instead of presence."""
        result = gates.evaluate_smoke_retrieval(
            expectation="absent", retrieved_document_ids=[], target="gone"
        )

        assert result["passed"] is True

    def test_deleted_document_still_retrievable_fails(self):
        result = gates.evaluate_smoke_retrieval(
            expectation="absent", retrieved_document_ids=["gone"], target="gone"
        )

        assert result["passed"] is False


class TestChangeSetEmptiness:
    def test_empty_change_set_is_detected(self):
        assert gates.is_empty_change_set(
            {"added": [], "modified": [], "deleted": []}
        ) is True

    def test_any_change_makes_it_non_empty(self):
        assert gates.is_empty_change_set(
            {"added": [], "modified": [{"file": "a.md"}], "deleted": []}
        ) is False

    def test_absent_keys_do_not_raise(self):
        assert gates.is_empty_change_set({"added": []}) is True


class TestWireContract:
    """The state machine reads these keys by JSONPath.

    Renaming one would break the release pipeline at runtime while every
    behavioral test above still passed, so pin the key sets here.
    """

    def test_deletion_ratio_keys(self):
        result = gates.evaluate_deletion_ratio(
            deleted_count=1, previous_document_count=10, threshold=0.5
        )

        assert set(result) == {"ratio", "passed", "overridden", "threshold"}

    def test_status_evaluation_keys(self):
        for evaluate in (gates.evaluate_ingest_statuses, gates.evaluate_delete_statuses):
            result = evaluate([{"identifier": "doc-1", "status": "NOT_FOUND"}])

            assert set(result) == {"settled", "passed", "pending", "failures"}

    def test_s3_consistency_keys(self):
        result = gates.evaluate_s3_consistency(
            expected_upserts=[],
            observed_objects={},
            expected_deletions=[],
            surviving_deletions=[],
        )

        assert set(result) == {
            "passed",
            "missing",
            "mismatched",
            "surviving",
            "expectedDeletionCount",
        }

    def test_smoke_retrieval_keys(self):
        result = gates.evaluate_smoke_retrieval(
            expectation="present", retrieved_document_ids=["doc-1"], target="doc-1"
        )

        assert set(result) == {"passed", "expectation", "target", "found"}

    def test_every_returned_value_is_json_serializable(self):
        """Sets would break the Lambda response serialization."""
        results = [
            gates.evaluate_deletion_ratio(
                deleted_count=1, previous_document_count=10, threshold=0.5
            ),
            gates.evaluate_ingest_statuses([{"identifier": "d", "status": "INDEXED"}]),
            gates.evaluate_s3_consistency(
                expected_upserts=[],
                observed_objects={},
                expected_deletions=[{"file": "gone.md"}],
                surviving_deletions=["gone.md"],
            ),
            gates.evaluate_smoke_retrieval(
                expectation="absent", retrieved_document_ids=[], target="gone"
            ),
        ]

        for result in results:
            json.dumps(result)


class TestBulkDeletionOverrideTyping:
    """The override must be a real bool, never a coerced string.

    Step Functions renders a boolean read through JsonPath.stringAt as the string
    "false", which is truthy. Coercing it would permit exactly the bulk deletion
    the caller meant to forbid — a silent authorization bypass.
    """

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
    def test_non_boolean_override_is_rejected(self, value):
        with pytest.raises(ValueError, match="must be a bool"):
            gates.evaluate_deletion_ratio(
                deleted_count=8,
                previous_document_count=13,
                threshold=0.5,
                allow_bulk_deletion=value,
            )

    def test_handler_parses_the_stringified_flag(self):
        from kbp.ingestion.handlers import check_gates

        for sent, expected in (("false", False), ("true", True), (False, False)):
            result = check_gates.handler(
                {
                    "gate": "deletionRatio",
                    "deletedCount": 8,
                    "previousDocumentCount": 13,
                    "threshold": 0.5,
                    "allowBulkDeletion": sent,
                },
                None,
            )
            assert result["passed"] is expected, f"{sent!r} should give {expected}"

    def test_handler_rejects_an_unrecognized_flag(self):
        from kbp.ingestion.handlers import check_gates

        with pytest.raises(ValueError, match="expected a boolean"):
            check_gates.handler(
                {
                    "gate": "deletionRatio",
                    "deletedCount": 8,
                    "previousDocumentCount": 13,
                    "threshold": 0.5,
                    "allowBulkDeletion": "yes",
                },
                None,
            )
