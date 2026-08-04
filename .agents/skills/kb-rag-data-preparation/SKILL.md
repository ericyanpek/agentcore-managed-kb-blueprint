---
name: kb-rag-data-preparation
description: Improve and govern knowledge-base and RAG data preparation from source extraction through parsing, structure-aware or semantic chunking, metadata design, ingestion, retrieval evaluation, release, update, and rollback. Use for PDF/HTML/Markdown/document corpora, chunking experiments, metadata filtering, ingestion quality gates, RAG regressions, or KB lifecycle governance.
---

# KB/RAG Data Preparation

## Purpose

Build a measured, reproducible data pipeline. Treat parsing, chunking, metadata,
embedding, retrieval, and governance as separately versioned decisions.

## Workflow

1. Inventory source formats, languages, tables/images, ownership, sensitivity,
   update frequency, and expected queries.
2. Preserve immutable originals and extract a canonical UTF-8 representation.
   Record source and canonical checksums.
3. Recover document structure before splitting. Prefer native headings,
   questions, procedures, tables, and code boundaries over inferred similarity.
4. Choose a chunking candidate using
   [decision-framework.md](references/decision-framework.md). Do not present
   fixed thresholds as universal defaults.
5. Define metadata before ingestion. Separate fields used for filtering,
   governance, provenance, and embedding. Read
   [metadata-governance.md](references/metadata-governance.md).
6. Run deterministic corpus checks:

   ```bash
   python3 scripts/profile_corpus.py <corpus-directory> --sidecar-policy required
   ```

7. Ingest each candidate through an isolated corpus, index, namespace, or data
   source. Never compare two strategies after changing both text and metadata
   unless the experiment explicitly studies the combined system.
8. Evaluate with representative, adversarial, exact-match, broad analytical,
   authorization, stale-content, and no-answer queries. Apply the gates in
   [evaluation-gates.md](references/evaluation-gates.md).
9. Publish only after quality, security, freshness, cost, and rollback gates
   pass. Retain the previous version until the new release is proven.
10. Monitor ingestion failures, retrieval quality, filter enforcement,
    freshness, latency, cost, and corpus drift.

## Experiment Rules

- State one hypothesis and one primary variable.
- Keep source bytes, query set, Top-K, reranker, filters, and scoring fixed.
- Use stable evidence labels or human judgments, not keyword overlap alone.
- Report per-query results and distributions, not only averages.
- Repeat latency-sensitive trials; a single request is not a performance claim.
- Record service version, region, model/index configuration, code revision,
  corpus checksum, and ingestion job evidence.
- Promote only when agreed primary metrics improve without violating
  authorization, provenance, no-answer, or latency gates.

## Managed Bedrock Variant

For Amazon Bedrock Managed Knowledge Bases, read
[bedrock-managed-kb.md](references/bedrock-managed-kb.md) before changing a data
source. In particular:

- S3 metadata is a same-folder `<document>.metadata.json` sidecar.
- `includeForEmbedding=false` keeps the field filterable without changing the
  embedding input.
- `includeForEmbedding=true` prepends the key/value to chunk text for embedding;
  it does not alter the returned raw chunk.
- Sidecar changes take effect only after a new ingestion job.
- Data-source parsing and chunking choices may require a new data source.

## Output Contract

Produce:

- a corpus manifest with source/canonical checksums and transformation version;
- a metadata dictionary with owner, type, allowed values, embedding policy,
  retention, and access semantics;
- preparation and ingestion quality reports;
- a versioned retrieval evaluation set and raw responses;
- an experiment report with hypothesis, controls, metrics, decision, and limits;
- a release/rollback record and operating owner.

## Attribution

This Skill adapts the strategy ladder and evaluation emphasis from the
MIT-licensed upstream `chunking-strategy` Skill. See
[upstream-attribution.md](references/upstream-attribution.md). The numeric
heuristics in that project are starting hypotheses, not acceptance criteria.
