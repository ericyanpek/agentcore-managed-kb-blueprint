"""Split a change set into API-sized batches and build request payloads."""

# Ten, confirmed against the service: submitting eleven returns "The number of
# documents (11) exceeds the maximum allowed (10) for MANAGED knowledge base type".
# The user guide's figure of 25 applies to other knowledge base types, so do not
# raise this on the strength of that page alone.
MAX_DOCUMENTS_PER_REQUEST = 10


def split_batches(
    documents: list[dict], *, size: int = MAX_DOCUMENTS_PER_REQUEST
) -> list[list[dict]]:
    """Split documents into batches no larger than the API limit."""
    if size < 1 or size > MAX_DOCUMENTS_PER_REQUEST:
        raise ValueError(
            f"batch size must be between 1 and {MAX_DOCUMENTS_PER_REQUEST}, got {size}"
        )
    return [
        documents[index : index + size] for index in range(0, len(documents), size)
    ]


def _object_uri(*, bucket: str, prefix: str, file: str) -> str:
    normalized = prefix.strip().strip("/")
    if not normalized:
        # An empty prefix would yield s3://bucket//file, which addresses a key
        # literally named "/file". The URI stays syntactically valid, so the
        # service would ingest the wrong object rather than reject the request.
        raise ValueError(f"prefix must name a non-empty key prefix, got {prefix!r}")
    return f"s3://{bucket}/{normalized}/{file}"


def build_ingest_payload(
    *, documents: list[dict], bucket: str, prefix: str
) -> list[dict]:
    """Build IngestKnowledgeBaseDocuments entries.

    Field names are PascalCase because these entries are passed through a Step
    Functions SDK integration, which requires PascalCase parameters and rejects
    the camelCase that boto3 accepts: "The field \"content\" is not supported by
    Step Functions. Did you mean 'Content'?"

    The metadata sidecar is bound explicitly via S3_LOCATION; without it the
    service would index the document without its governance and filter
    attributes.
    """
    return [
        {
            "Content": {
                "DataSourceType": "S3",
                "S3": {
                    "S3Location": {
                        "Uri": _object_uri(
                            bucket=bucket, prefix=prefix, file=item["file"]
                        )
                    }
                },
            },
            "Metadata": {
                "Type": "S3_LOCATION",
                "S3Location": {
                    "Uri": _object_uri(
                        bucket=bucket,
                        prefix=prefix,
                        file=f"{item['file']}.metadata.json",
                    )
                },
            },
        }
        for item in documents
    ]


def build_document_identifiers(
    *, documents: list[dict], bucket: str, prefix: str
) -> list[dict]:
    """Build DocumentIdentifier entries.

    Used both to delete documents and to poll their status, since both APIs take
    the same identifier shape. PascalCase for the same reason as the ingest
    payload: these travel through a Step Functions SDK integration.
    """
    return [
        {
            "DataSourceType": "S3",
            "S3": {
                "Uri": _object_uri(bucket=bucket, prefix=prefix, file=item["file"])
            },
        }
        for item in documents
    ]
