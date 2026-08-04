# Evaluation Gates

## Preparation

- Source and canonical checksums recorded.
- Encoding valid; no unexpected replacement characters.
- Expected pages/sections/records present.
- Tables, lists, headings, and code preservation sampled.
- No empty, oversized, or pathological tiny chunks beyond approved exceptions.
- Metadata schema, pairing, size, type, and allowed-value checks pass.

## Retrieval

Measure at minimum:

- Hit Rate at K;
- Recall or evidence-marker coverage at K;
- MRR or nDCG for ranking;
- relevant and duplicate results per query;
- provenance and metadata completeness;
- p50/p95 latency over repeated trials;
- positive and negative authorization filters;
- no-answer behavior and stale-version exclusion.

Use human relevance labels for production decisions. Evidence strings are
useful deterministic regression signals but can miss semantically correct
answers and reward irrelevant keyword matches.

## Release

- Define primary metric and non-regression tolerances before execution.
- Fail closed on authorization-filter errors.
- Require complete provenance for user-visible citations.
- Document statistical limits for small query sets.
- Keep canary and prior corpus versions until online monitoring is stable.
- Roll back by routing retrieval to the prior corpus/data source, then diagnose
  extraction, metadata, chunking, embedding, retrieval, and generation
  separately.
