# AgentCore Managed Knowledge Base measured blueprint

**English** | [中文](README.md)

A reproducible blueprint written for architecture decisions. Instead of restating
what Managed Knowledge Base can do, it uses live testing to map the capability
boundaries, failure modes, and quota constraints, so selection and rollout
decisions rest on evidence. Every conclusion is labeled as an AWS documented
capability, an AWS recommendation, or a measurement from this project; negative
results are recorded alongside positive ones.

This workspace provisions and validates an Amazon Bedrock AgentCore Managed
Knowledge Base using the public AWS Well-Architected Games Industry Lens PDF.

See [RESULTS.md](docs/RESULTS.md) for the observed AWS resource IDs, ingestion
statistics, retrieval scores, Agentic Retrieval result, quality findings, and
governance recommendations.

See [KB_PLATFORM_SELECTION_GUIDE.md](docs/KB_PLATFORM_SELECTION_GUIDE.md) for the
cross-cloud, ISV, and self-built vector database selection framework.

See [AWS_KB_RAG_BEST_PRACTICES.md](docs/AWS_KB_RAG_BEST_PRACTICES.md) for AWS
official guidance on quality evaluation, knowledge updates, access governance,
retrieval optimization, performance, cost, and RAGOps release practices.

See [SEMANTIC_CHUNKING_EXPERIMENT.md](docs/SEMANTIC_CHUNKING_EXPERIMENT.md) for
the measured comparison of service-default fixed-size chunking and
structure-aware pre-chunking across evidence coverage, ranking, provenance,
and latency.

See [METADATA_EXPERIMENT.md](docs/METADATA_EXPERIMENT.md) for a byte-controlled
comparison of no metadata, filter-only metadata, and semantic metadata included
in embeddings, plus the storage, update, and governance model.

See [MD_CORPUS_PIPELINE.md](docs/MD_CORPUS_PIPELINE.md) for the enterprise
Markdown MVP pipeline: how managed and classic knowledge base quotas differ, how
the direct ingestion and reconciliation channels divide responsibility, how
manifest-based change detection works, and how to decide between multiple
knowledge bases and one knowledge base with many data sources.

See the project-owned
[KB/RAG Data Preparation Skill](.agents/skills/kb-rag-data-preparation/SKILL.md)
for the extraction, structure recovery, chunking, metadata, ingestion,
evaluation, release, and rollback workflow plus a corpus profiler.

See [SECURITY.md](SECURITY.md) for the repository publication policy.

The Chinese and English READMEs are maintained as synchronized documents.
Every README change must be applied to both language versions in the same
change. Run `python3 scripts/13_check_readme_sync.py` to validate bilingual
sections, command blocks, and links; GitHub CI also requires both READMEs to
change together.

## Local setup

The repository does not contain AWS credentials, account identifiers, resource
IDs, or raw runtime responses. Create the ignored local configuration and
environment before running the scripts:

```bash
cp config/test.env.example config/test.env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
aws sts get-caller-identity
```

Edit `config/test.env` for the target region and resource names. The account ID
and default globally unique bucket name are derived from the active AWS
identity during provisioning when those fields are left empty. If
`AWS_ACCOUNT_ID` is set, provisioning rejects a mismatch with the active
identity. AWS credentials remain in the normal AWS CLI credential chain; do
not put access keys in this repository.

## Execution order

```bash
./scripts/01_prepare_source.sh
./scripts/02_provision.sh
./scripts/03_ingest.sh
./scripts/04_test_retrieval.sh
./scripts/06_quality_diagnostics.sh
./scripts/07_verify.sh
```

`scripts/08_agentic_retrieval.py` contains the streaming Agentic Retrieval
equivalent for an SDK version that exposes `agentic_retrieve_stream`.

The Chinese PDF triggers a Managed KB Smart Parsing defect that drops most CJK
characters. The repair path pre-extracts UTF-8 Markdown and ingests it through
an isolated data source:

```bash
./scripts/09_prepare_text_repair.sh
./scripts/10_ingest_text_repair.sh
./scripts/11_test_text_repair.sh
```

The extraction quality gate requires 146 pages, no Unicode replacement
characters, no more than one image-only/empty-text page, and a CJK ratio of at
least 50%. The repaired document uses
`document_id=aws-games-industry-lens-2026-07-31-text-v1`. Applications must
filter on this value while the original diagnostic PDF data source remains
indexed.

Managed Embedding does not accept explicit semantic chunking. This repository
includes a structure-aware pre-chunking experiment that creates isolated
Markdown documents at question, best-practice, implementation-guidance, and
sentence boundaries, then compares that data source with the fixed-size
baseline.

```bash
./scripts/14_prepare_semantic_chunks.sh
./scripts/15_ingest_semantic_chunks.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/16_compare_semantic_chunking.sh
```

The experiment improved Top-10 evidence coverage and provenance but reduced
MRR. It therefore remains a canary corpus and does not replace the `text-v1`
baseline.

The metadata experiment ingests the same 479 canonical Markdown documents with
no sidecars, all `includeForEmbedding=false`, and selected semantic fields set
to `includeForEmbedding=true`:

```bash
./scripts/17_prepare_metadata_experiment.sh
./scripts/18_ingest_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/19_compare_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/20_expand_metadata_retrieval.sh
```

The expanded experiment covers 44 queries, two reranking modes, and 408
Retrieve calls. Unfiltered retrieval still showed no metadata-embedding gain.
Runtime filters increased MRR from 0.241 to 0.556 across the 36 filterable
queries and raised `best_practice_id` section lookup MRR to 1.000. A metadata
filter narrows the semantic candidate set but does not guarantee a result.
Authorization filters must fail closed, while deterministic document reads
should use S3 or the content system. The default is therefore complete
metadata, governance fields excluded from embeddings, and runtime filtering
on stable control identifiers.

An enterprise Markdown corpus uses a separate two-channel flow.
`05_sync_updates.sh` can only run one full sync over an entire data source and
has no change detection, throttling, targeted ingestion, or retry. The Markdown
flow instead diffs against a published manifest to derive added, modified, and
deleted sets, then applies upserts with `IngestKnowledgeBaseDocuments` and
absorbs deletions with `StartIngestionJob`. Set `DRY_RUN=1` to stop after
planning:

```bash
PYTHON_BIN=python3.12 ./scripts/21_prepare_md_corpus.sh
DRY_RUN=1 PYTHON_BIN=python3.12 ./scripts/22_incremental_ingest.sh
PYTHON_BIN=python3.12 ./scripts/22_incremental_ingest.sh
```

Two assumptions that shape this architecture are currently unverified: whether
`StartIngestionJob` enforces 0.1 rps on managed knowledge bases, and whether a
reconciliation sync removes directly ingested documents that exist only in the
index. The planner is configured pessimistically. Running the following script
in an environment with control plane permissions produces the evidence, and it
must point at a disposable data source:

```bash
PROBE_DATA_SOURCE_ID=<disposable-data-source-id> ./scripts/23_verify_assumptions.sh
```

Use `./scripts/05_sync_updates.sh` after changing the original PDF source. For
the repaired text source, select its data source explicitly:

```bash
source artifacts/20260803/state.env
TARGET_DATA_SOURCE_ID="${TEXT_DATA_SOURCE_ID}" ./scripts/05_sync_updates.sh
```

Cleanup is intentionally guarded:

```bash
CONFIRM_DESTROY=DELETE-games-industry-lens-managed-kb-20260803 \
  ./scripts/99_cleanup.sh
```

## Test design

- Knowledge base type: `MANAGED`
- Embedding: service-managed
- Reranker: service-managed
- Source: dedicated, private, versioned, SSE-S3 encrypted S3 bucket
- Connector: managed S3 connector restricted to one prefix
- Parsing: `SMART_PARSING`
- Image extraction: enabled for PDF visual content
- Chunking: service default, fixed size, 300 tokens, 20% overlap
- Deletion safeguard: block a sync that would delete more than 50% of indexed
  documents
- Data deletion policy: `DELETE`
- Retrieval: managed search, 10 candidates, managed reranking
- Generation baseline: `Retrieve` followed by Amazon Nova Lite `Converse`,
  1,200 maximum output tokens, temperature 0.1

The chunking strategy is immutable after data source creation. Managed
Knowledge Bases support default/fixed-size/no chunking in the product
documentation; semantic and hierarchical chunking are not supported.

On 2026-08-03, the `CreateDataSource` API rejected an explicit
`chunkingConfiguration` for a knowledge base using
`embeddingModelType=MANAGED`:

```text
A chunking strategy cannot be specified with a managed embedding model.
Omit chunkingConfiguration to use the default.
```

The executable configuration therefore omits `chunkingConfiguration` and uses
the managed service default of 300 tokens with 20% overlap. Use a custom
embedding model and a new data source if explicit chunk sizing is required.

## Artifact layout

All command responses and test evidence are stored under the ignored
`artifacts/<RUN_ID>/` directory:

- `source/`: downloaded PDF, SHA-256, PDF metadata, ingestion metadata
- `aws/`: resource descriptions, IAM evidence, ingestion job statistics
- `tests/`: raw retrieval and generation responses plus compact summaries
- `state.env`: generated non-secret resource identifiers

These files can still contain account IDs, IAM ARNs, bucket names, resource
IDs, source paths, and retrieved content. They are operational evidence, not
publishable fixtures. Run `./scripts/12_repository_safety_check.sh` before
staging a release.

The repair-specific evidence includes:

- `source/games-industry-lens.zh-CN.md`: page-marked UTF-8 text
- `tests/pdf-to-markdown-report.json`: local extraction quality metrics
- `tests/text-repair-retrieval-summary.json`: four filtered Retrieve checks
- `tests/text-repair-regression-summary.json`: ingestion and agentic regression
  summary
- `tests/agentic-player-behavior-analytics-summary.json`: player behavior
  analytics retrieval parameters, coverage assessment, and evidence links
- `tests/agentic-player-behavior-analytics-events.ndjson`: raw streaming events
  for the player behavior analytics query
- `tests/semantic-chunking-preparation-report.json`: semantic pre-chunking
  quality statistics
- `tests/semantic-chunking-comparison.json`: metrics and case-level results for
  the eight A/B retrieval tests
- `tests/metadata-experiment-preparation-report.json`: cross-variant content
  identity and sidecar gates
- `tests/metadata-experiment-comparison.json`: metadata quality and filter
  comparison
- `tests/metadata-expanded-query-set.json`: 44-query expanded retrieval set
- `tests/metadata-expanded-comparison.json`: metrics, categories, and paired
  comparisons for 408 calls
- `tests/metadata-expanded-comparison.md`: generated compact expanded report

## Storage and pipeline

The source PDF and its sidecar metadata file are normal S3 objects under the
configured prefix. The vector index, embeddings, parser execution, reranker,
and retrieval infrastructure are service-managed and are not exposed as a
customer S3 Vectors bucket or OpenSearch collection.

The authoritative metadata copy is an S3 sidecar adjacent to the source object,
using the source name plus `.metadata.json`. Ingestion copies fields into the
Managed KB index and attaches them to chunks. `includeForEmbedding` controls
only whether a field contributes to vector input, not whether it is stored or
filterable. A sidecar update requires an explicit new ingestion job.

The ingestion pipeline does not require Lambda, Step Functions, or a separate
ETL job. It is explicit at the control-plane boundary:

1. Upload or update source objects.
2. Call `StartIngestionJob`.
3. Poll `GetIngestionJob` to `COMPLETE` or `FAILED`.
4. Inspect document statistics and logs.

Each subsequent ingestion is incremental: the connector detects new, changed,
and deleted source content. Source versioning allows rollback of accidental
overwrites; the connector deletion safeguard prevents a large source deletion
from immediately removing most indexed content.

## Configuration decisions

Knowledge-base creation fixes:

- Managed versus self-managed storage.
- Managed versus custom embedding model.
- Optional KMS key for the managed vector store.

Data-source creation fixes:

- Connector type and connection parameters.
- Parsing strategy. Managed Knowledge Bases only support `SMART_PARSING`.
- Chunking strategy. With managed embedding, the current API forces the
  service default. With a configurable option, changing it requires a new data
  source.
- Media extraction for images, audio, and video.

Runtime retrieval can vary per request:

- `numberOfResults`: 1-100; larger values improve recall but increase latency.
- `rerankingModelType`: `MANAGED`, `CUSTOM`, or `NONE`.
- Metadata filters: exact, range, list, and logical operators.
- Guardrail configuration.
- User context.

Managed Knowledge Bases do not support the classic `RetrieveAndGenerate` API.
Use one of these supported patterns:

- `Retrieve` followed by `Converse` for full prompt and citation control. This
  repository executes this path.
- `AgenticRetrieveStream` for multi-step query planning, iterative retrieval,
  sufficiency evaluation, and streaming traces. This requires an SDK version
  that includes the 2026 API. This repository pins the validated SDK version
  in `requirements.txt`.

The `Converse` phase controls the generation model, prompt, `maxTokens`,
temperature, top-p, and citation format. `AgenticRetrieveStream` instead
controls the planning model, retriever list, maximum results, and agent
iteration limit.

`maxAgentIteration` is a ceiling, not a required number of rounds. Both repaired
agentic regressions completed speculative retrieval and planning with
`actions=[]`. Broad questions can therefore miss relevant sections even when
the index is healthy. Use targeted sub-queries or a higher result count for
coverage-sensitive evaluations.

## Update and governance policy

- Keep each security or ownership boundary in a separate data source or KB.
- Store `classification`, `document_id`, owner, version, and effective date as
  filterable metadata.
- Enforce authorization with metadata filters in the application. S3 source
  permissions are not automatically preserved at retrieval time.
- Treat source updates as reviewed releases: upload, checksum, sync, run
  retrieval regression tests, then promote.
- Enable CloudTrail data events for `Retrieve` and `AgenticRetrieveStream`;
  management events already cover KB and ingestion changes.
- Deliver ingestion logs to CloudWatch Logs or S3 and alarm on failed jobs.
- Use customer-managed KMS keys for regulated data and redact PII/PHI before
  ingestion. Guardrails do not sanitize raw retrieved references.
- Use least-privilege service roles scoped to the exact bucket prefix and
  tighten the trust policy to the exact KB ARN after creation.
- Run periodic stale-document, access-review, retrieval-quality, cost, and
  deletion-recovery checks.

## License

Project code and original documentation are available under the
[MIT License](LICENSE). The project-owned Skill preserves the copyright and
attribution of its referenced upstream methodology. AWS service names and
official documentation remain the property of their respective owners.
