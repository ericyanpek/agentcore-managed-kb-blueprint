# AgentCore Managed Knowledge Base governance and test blueprint

**English** | [中文](README.md)

A reproducible project for AWS Solutions Architects, AI platform teams, and RAG
engineers. It uses the AWS Well-Architected Games Industry Lens to evaluate
Amazon Bedrock AgentCore Managed Knowledge Base data preparation, ingestion,
retrieval, Agentic Retrieval, metadata, updates, and enterprise governance.

> **Core position:** Managed KB operates parsing, embeddings, indexing, storage,
> and retrieval infrastructure. Customers still own the system of record, final
> authorization, quality evaluation, release governance, and generation layer.

The repository provides three types of assets:

- **Measured evidence:** reproducible parameters, positive results, failures,
  and negative findings.
- **Engineering tools:** PDF repair, chunking, metadata, incremental ingestion,
  and regression testing.
- **Governance blueprint:** control baseline, experiment route, observability,
  and release gates.

## 1. Key findings

| Question | Finding |
| --- | --- |
| If 5 of 50 Markdown files change, how many are updated? | The local pipeline rescans all 50 and compares manifests; S3 and direct ingestion process only the 5 changed files. |
| Can the AWS PDF be ingested directly? | Smart Parsing dropped most CJK text from the Chinese PDF. Pre-extracting UTF-8 Markdown repaired it. |
| Is semantic chunking always better? | Structure-aware pre-chunking improved Top-10 evidence coverage and provenance but reduced MRR. It remains a canary. |
| Does metadata improve retrieval? | Metadata embeddings showed no measured gain. Runtime filters raised MRR from 0.241 to 0.556 across 36 filterable queries. |
| Does Agentic Retrieval always run another search? | No. `maxAgentIteration` is a ceiling; `actions=[]` means the planner chose no follow-up action, not that the system failed. |
| Can managed embeddings use a custom chunk size? | The tested API required omitting `chunkingConfiguration` and using the 300-token, 20%-overlap service default. |
| Is the update pipeline production-ready? | No. It is a research MVP without final-state polling, targeted deletion, or atomic manifest promotion. |

See the [measured results](docs/RESULTS.md) for full metrics, queries, and
evidence.

## 2. Quick start

Prerequisites: Python 3.12, AWS CLI, temporary AWS credentials for the target
Region, and least-privilege permission to create lab resources. These commands
incur AWS charges; use a sandbox account and budget alarms.

```bash
cp config/test.env.example config/test.env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
aws sts get-caller-identity
```

Edit the ignored `config/test.env`, then run the baseline experiment:

```bash
./scripts/01_prepare_source.sh
./scripts/02_provision.sh
./scripts/03_ingest.sh
./scripts/04_test_retrieval.sh
./scripts/06_quality_diagnostics.sh
./scripts/07_verify.sh
```

The baseline creates lab resources, ingests the original PDF, and tests
retrieval. The streaming Agentic Retrieval implementation is
`scripts/08_agentic_retrieval.py`.

## 3. Reproduce experiments

### 3.1 Repair the Chinese PDF

Pre-extract page-marked UTF-8 Markdown and ingest it through an isolated data
source:

```bash
./scripts/09_prepare_text_repair.sh
./scripts/10_ingest_text_repair.sh
./scripts/11_test_text_repair.sh
```

Quality gates require 146 pages, no Unicode replacement characters, no more
than one empty-text page, and a CJK character ratio of at least 50%.

### 3.2 Structure-aware chunking

```bash
./scripts/14_prepare_semantic_chunks.sh
./scripts/15_ingest_semantic_chunks.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/16_compare_semantic_chunking.sh
```

See the [semantic chunking experiment](docs/SEMANTIC_CHUNKING_EXPERIMENT.md)
for the design and results.

### 3.3 Metadata comparison

```bash
./scripts/17_prepare_metadata_experiment.sh
./scripts/18_ingest_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/19_compare_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/20_expand_metadata_retrieval.sh
```

The test covers 44 queries, two reranking modes, and 408 Retrieve calls. See
the [metadata experiment](docs/METADATA_EXPERIMENT.md) for results and field
governance.

### 3.4 Incremental enterprise Markdown ingestion

```bash
PYTHON_BIN=python3.12 ./scripts/21_prepare_md_corpus.sh
DRY_RUN=1 PYTHON_BIN=python3.12 ./scripts/22_incremental_ingest.sh
PYTHON_BIN=python3.12 ./scripts/22_incremental_ingest.sh
```

This flow derives added, modified, and deleted sets from a manifest and
separates direct ingestion from connector reconciliation. See the
[enterprise Markdown pipeline](docs/MD_CORPUS_PIPELINE.md) for current
production gaps.

## 4. Architecture and configuration boundaries

### At knowledge-base creation

- Choose managed or self-managed storage.
- Choose a managed or custom embedding model.
- Optionally select a KMS key for the managed vector store.

### At data-source creation

- Fix the connector, connection parameters, and deletion policy.
- Managed KB supports only `SMART_PARSING`.
- Chunking is immutable after creation; managed embeddings currently use the
  service default.
- Configure media extraction for images, audio, and video as needed.

### For each retrieval request

- `numberOfResults`: 1-100.
- `rerankingModelType`: `MANAGED`, `CUSTOM`, or `NONE`.
- Metadata filters, guardrails, and user context.
- Use `Retrieve` followed by `Converse`, or `AgenticRetrieveStream`.

### Data and responsibility boundaries

```text
S3 / CMS / Git (system of record)
  -> Data preparation + metadata + approval
  -> Managed Connector / Direct Ingestion
  -> Managed parsing + embedding + index + retrieval
  -> Application / Gateway (authentication, authorization, generation, citations)
```

Keep authoritative metadata in source-adjacent sidecars or the content system;
the Managed KB vector index is derived and rebuildable state. Gateway tool
authorization, KB filtering, and end-user authorization are separate controls
and cannot replace one another.

## 5. Governance and documentation

### Decisions and evidence

| Document | Purpose |
| --- | --- |
| [Measured results](docs/RESULTS.md) | Ingestion, retrieval, Agentic Retrieval, quality issues, and governance findings |
| [Platform selection guide](docs/KB_PLATFORM_SELECTION_GUIDE.md) | AWS, Azure, GCP, ISV, and self-built vector database comparison |
| [AWS KB/RAG best practices](docs/AWS_KB_RAG_BEST_PRACTICES.md) | Quality, updates, access, performance, cost, and RAGOps |
| [AWS official sample catalog](docs/AWS_SAMPLE_CATALOG.md) | Pinned sample SHAs, capability mapping, and production gaps |

### Enterprise governance

| Document | Purpose |
| --- | --- |
| [Enterprise governance blueprint](docs/ENTERPRISE_GOVERNANCE_BLUEPRINT.md) | Account, Region, tenant, IAM, network, data, and Gateway contracts |
| [Minimum control baseline](docs/CONTROL_BASELINE.md) | `MUST/SHOULD` controls, evidence, risk tiers, exceptions, and release gates |
| [Enterprise experiment route](experiments/README.md) | E00-E07 positive/negative tests, cost, cleanup, and ADRs |
| [Observability blueprint](docs/OBSERVABILITY_BLUEPRINT.md) | Metrics, logs, traces, ADOT, Transaction Search, and long-term analytics |
| [Handoff report](HANDOFF_REPORT.md) | Unverified assumptions, cross-service alignment, and follow-up work |

### Reusable assets

| Asset | Purpose |
| --- | --- |
| [KB/RAG data preparation skill](.agents/skills/kb-rag-data-preparation/SKILL.md) | Parsing, chunking, metadata, ingestion, evaluation, release, and rollback |
| [Observability evidence template](experiments/observability-evidence.template.md) | Success/failure and three-signal evidence for every experiment |
| [Observability event schema](schemas/observability-event.schema.json) | Contract for normalized redacted analytics events |
| [Security and publication policy](SECURITY.md) | GitHub publication boundary and redaction requirements |

## 6. Runtime evidence and safety

Runtime responses are stored under the ignored `artifacts/<RUN_ID>/` path:

| Path | Contents |
| --- | --- |
| `source/` | Source material, checksums, canonical Markdown, and metadata |
| `aws/` | Resource descriptions, IAM evidence, and ingestion jobs |
| `tests/` | Retrieval events, comparisons, and compact reports |
| `state.env` | Non-secret resource identifiers generated for the run |

Evidence can still contain account IDs, ARNs, bucket names, resource IDs,
source paths, or retrieved content. Do not commit it. Before publication, run:

```bash
./scripts/12_repository_safety_check.sh
python3 scripts/13_check_readme_sync.py
```

The Chinese and English READMEs must change together. CI checks links, sections,
and command blocks.

## 7. Current official baseline

The following information was reviewed on 2026-08-04 and must be revalidated
for the target account and Region before rollout:

- Published Regions include `us-east-1`, `us-west-2`, `eu-west-1`,
  `eu-central-1`, `eu-west-2`, `ap-southeast-2`, `ap-northeast-1`, and
  `us-gov-west-1`.
- Default quotas include 10,000 KBs per account/Region, 200 data sources per KB,
  50 concurrent ingestion jobs, 10 TB raw data, 600 Retrieve requests per
  minute per KB, and 60 Agentic Retrieve requests per minute per account.
- US Region price examples are `$5/GB-month` for index storage,
  `$1/1,000 calls` for Standard Retrieve, and `$4/1,000 calls` for Agentic
  Retrieve. Agentic internal Retrieve calls are billed separately.
- CloudFormation and CDK L1 support `ManagedKnowledgeBaseConfiguration`.

See the [AWS official sample catalog](docs/AWS_SAMPLE_CATALOG.md) for official
sources, release dates, and pinned sample commits.

## 8. Known limitations

- The original Chinese PDF is not suitable as the current production corpus;
  use the repaired Markdown version.
- Semantic chunking has local benefits but has not passed the release gate to
  replace the baseline.
- Metadata filters narrow candidates but cannot replace authentication or
  guarantee a result.
- `scripts/21` through `23` remain research implementations, not a production
  release state machine.
- Transaction Search, vended log delivery, and application ADOT must be
  verified separately for each account, Region, and resource. A console page
  is not sufficient evidence.
- This repository contains no AWS credentials, real account evidence, or
  customer data, and it does not automatically deploy production resources.

## 9. Cleanup and license

Cleanup requires explicit confirmation:

```bash
CONFIRM_DESTROY=DELETE-games-industry-lens-managed-kb-20260803 \
  ./scripts/99_cleanup.sh
```

Project code and original documentation use the [MIT License](LICENSE). AWS
service names and official documentation remain the property of their
respective owners.
