# Chunking Decision Framework

## Selection Order

1. **No chunking**: already atomic records, FAQs, or intentionally pre-chunked
   content that fits the embedding input.
2. **Structure-aware**: Markdown/HTML headings, manuals, policies, procedures,
   source code, tables, or documents with stable semantic labels.
3. **Recursive boundary-aware**: prose with useful paragraph and sentence
   boundaries but weak higher-level structure.
4. **Embedding-based semantic boundaries**: long prose with real topic shifts
   that structure rules cannot recover.
5. **Contextual or late chunking**: high-value retrieval where measured gains
   justify model calls, larger embedding contexts, latency, and cost.

## Parameter Method

- Derive candidate sizes from the embedding model limit and expected answer
  evidence span; do not divide the context window by a fixed constant.
- Use overlap only when evidence crosses boundaries. Duplication can inflate
  index size and crowd Top-K with near-identical chunks.
- Preserve headings, lists, tables, code blocks, and citations as atomic units
  where possible.
- Put section context in metadata first. Add it to embeddings only through an
  explicit A/B test.
- Test at least a precision-oriented and a context-oriented candidate.

## Failure Diagnosis

| Symptom | Likely cause | Candidate action |
| --- | --- | --- |
| Relevant document absent | recall/query/index issue | increase candidate pool, improve query set, inspect extraction |
| Right document, wrong passage | chunks too broad or weak boundaries | reduce size or add structure boundaries |
| Fragment lacks explanation | chunks too narrow | add parent context or enlarge semantic unit |
| Repeated near-duplicates | excessive overlap | reduce overlap and deduplicate |
| Exact IDs rank poorly | semantic-only retrieval | add hybrid search or carefully embedded labels |
| Filtered results empty | missing/type-mismatched metadata | validate sidecars and ingestion evidence |

Embedding similarity thresholds and chunk sizes are corpus-specific
hyperparameters. Select them from retrieval evaluation, not intuition alone.
