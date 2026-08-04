# Amazon Bedrock Managed Knowledge Base Notes

## S3 Metadata

For a source object `chapter.md`, store metadata as
`chapter.md.metadata.json` in the same S3 folder. The sidecar uses
`metadataAttributes`, typed `value` objects, and `includeForEmbedding`. AWS
documents a 10 KB maximum per sidecar.

```json
{
  "metadataAttributes": {
    "classification": {
      "value": {"type": "STRING", "stringValue": "PUBLIC"},
      "includeForEmbedding": false
    }
  }
}
```

- `false`: the field is stored and filterable, but only chunk text is embedded.
- `true`: AWS concatenates the metadata key/value with chunk text for embedding.
  Returned chunk text remains raw content.

Source:
[Connect to Amazon S3 for your knowledge base](https://docs.aws.amazon.com/bedrock/latest/userguide/s3-data-source-connector.html).

## Lifecycle

- S3 and sidecars are rebuildable source state.
- The service-managed vector index is derived state.
- Uploading an object does not publish it to retrieval; run and monitor
  `StartIngestionJob`.
- A metadata-only change also requires ingestion.
- Verify the exact returned field type before relying on a filter.
- Enforce document authorization with runtime metadata filters. S3 caller
  permissions are not automatically inherited by retrieval callers.

## Controlled Metadata Experiment

Use identical document bytes in separate data sources:

1. no sidecar;
2. full sidecar, every field excluded from embeddings;
3. same sidecar, selected semantic fields included in embeddings.

Isolate each group with the service-generated `_data_source_id` filter. Compare
retrieval quality, latency, returned metadata completeness, positive filters,
and a missing-field negative control.
