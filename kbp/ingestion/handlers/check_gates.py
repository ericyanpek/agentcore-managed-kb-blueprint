"""Lambda handler for Gates B, C, D: status and ratio evaluations.

This module contains NO AWS access — it is pure data transformation and
dispatch. The gate functions in kbp.ingestion.gates own all pass/fail logic.

Identifier flattening
---------------------
The Bedrock status APIs return a nested identifier shape:
    {"identifier": {"s3": {"uri": "s3://..."}}, "status": "INDEXED"}
or
    {"identifier": {"custom": {"id": "..."}}, ...}

The gate functions expect `identifier` to be a plain string. Flattening that
nested structure is I/O adaptation, which is exactly what handlers are for.
"""

from kbp.ingestion import gates


def _flatten_identifier(identifier) -> str:
    """Flatten the API's nested identifier shape to a plain string.

    S3 identifiers carry the S3 URI; custom identifiers carry the custom id.
    If the value is already a string (pre-flattened or test-supplied), return
    it unchanged.

    Both casings are accepted: boto3 returns camelCase while a Step Functions SDK
    integration returns PascalCase, and this gate is reachable from either.
    """
    if isinstance(identifier, str):
        return identifier
    for s3_key, uri_key in (("s3", "uri"), ("S3", "Uri")):
        if s3_key in identifier:
            return identifier[s3_key][uri_key]
    for custom_key, id_key in (("custom", "id"), ("Custom", "Id")):
        if custom_key in identifier:
            return identifier[custom_key][id_key]
    raise ValueError(f"unrecognized identifier shape: {identifier!r}")


def _as_bool(value) -> bool:
    """Coerce a JSONPath-sourced flag to a real bool.

    Step Functions renders a boolean field read through JsonPath.stringAt as the
    string "true" or "false", and "false" is truthy. Anything unrecognized is
    rejected rather than guessed, since guessing wrong on this flag would permit
    a bulk deletion nobody authorized.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ValueError(f"expected a boolean or 'true'/'false', got {value!r}")


def _normalize_details(document_details: list[dict]) -> list[dict]:
    """Flatten nested identifiers and normalize key casing.

    A Step Functions SDK integration returns PascalCase keys where boto3 returns
    camelCase, and this gate is reachable from either.
    """
    return [
        {
            "identifier": _flatten_identifier(
                item.get("identifier", item.get("Identifier"))
            ),
            "status": item.get("status", item.get("Status")),
        }
        for item in document_details
    ]


def handler(event: dict, context) -> dict:
    """Dispatch to the appropriate gate function based on event['gate'].

    Dispatches:
        deletionRatio  → evaluate_deletion_ratio
        ingestStatus   → evaluate_ingest_statuses
        deleteStatus   → evaluate_delete_statuses
        smokeRetrieval → evaluate_smoke_retrieval
    """
    gate = event["gate"]

    if gate == "deletionRatio":
        return gates.evaluate_deletion_ratio(
            deleted_count=event["deletedCount"],
            previous_document_count=event["previousDocumentCount"],
            threshold=event["threshold"],
            allow_bulk_deletion=_as_bool(event.get("allowBulkDeletion", False)),
        )

    if gate == "ingestStatus":
        details = _normalize_details(event.get("documentDetails", []))
        kwargs = {}
        if "expectedCount" in event:
            kwargs["expected_count"] = event["expectedCount"]
        return gates.evaluate_ingest_statuses(details, **kwargs)

    if gate == "deleteStatus":
        details = _normalize_details(event.get("documentDetails", []))
        kwargs = {}
        if "expectedCount" in event:
            kwargs["expected_count"] = event["expectedCount"]
        return gates.evaluate_delete_statuses(details, **kwargs)

    if gate == "smokeRetrieval":
        return gates.evaluate_smoke_retrieval(
            expectation=event["expectation"],
            retrieved_document_ids=event["retrievedDocumentIds"],
            target=event["target"],
        )

    raise ValueError(f"unknown gate: {gate!r}")
