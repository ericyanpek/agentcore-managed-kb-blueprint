# Metadata Governance

## Storage Layers

1. **System of record**: source catalog, content management system, or a
   version-controlled manifest.
2. **Ingestion representation**: adjacent sidecar, inline attributes, or
   connector-native fields.
3. **Retrieval index**: copied fields attached to chunks for filtering,
   ranking, provenance, and response metadata.
4. **Audit evidence**: immutable ingestion reports, schema version, checksums,
   and release decision.

Do not treat the vector index as the only copy. It is derived state and must be
rebuildable.

## Field Classes

| Class | Examples | Embedding default |
| --- | --- | --- |
| Authorization | tenant, ACL, classification | false |
| Lifecycle | status, effective date, expiry, version | false |
| Provenance | source URI, checksum, page, chunk ID | false |
| Routing | product, region, language, document type | false |
| Semantic context | title, topic, section path, entities | test both |

Never embed secrets, personal identifiers, tenant IDs, ACLs, checksums, or
volatile timestamps. Embedding a field is a relevance decision, not a storage
requirement.

## Data Dictionary

For every field define:

- canonical key and data type;
- description and source owner;
- required/optional status and allowed values;
- filter operators and access-control meaning;
- `includeForEmbedding` policy;
- update trigger, retention, and deletion behavior;
- schema version and migration rule.

Use stable snake_case keys. Reject inconsistent types and normalize enums before
ingestion.

## Update Policy

1. Change source content and metadata in one reviewed release.
2. Increment content or schema version and calculate checksums.
3. Validate required fields, allowed values, sidecar pairing, and size limits.
4. Run ingestion and inspect failures/skips.
5. Verify returned metadata and positive/negative filters.
6. Run retrieval and authorization regressions.
7. Promote, retain prior evidence, and remove stale versions only after the
   rollback window.
