# Amazon Bedrock Managed Knowledge Base: ingestion, tuning, retrieval, evaluation

**English** | [中文](README.md)

This repository answers four questions, each backed by measured numbers or
runnable code:

| # | Question | Where |
| --- | --- | --- |
| 1 | How knowledge gets into the KB, and how to do incremental updates correctly | [Ingestion pipeline](#3-ingestion-pipeline) · `kbp/preparation` `kbp/ingestion` |
| 2 | Which Managed KB parameters are tunable and which are immutable | [Parameter boundaries](#4-parameter-boundaries) |
| 3 | How to improve retrieval after indexing | [Retrieval tuning](#5-retrieval-tuning) · `docs/METADATA_EXPERIMENT.md` |
| 4 | What justifies shipping or rolling back a KB version | [Evaluation and release criteria](#6-evaluation-and-release-criteria) · `scripts/20` |

> **Responsibility boundary:** Managed KB owns parsing, embedding, indexing,
> storage and retrieval. The system of record, authorization, quality evaluation
> and the release decision remain with the caller. That boundary determines the
> division of labor in every section below.

The corpus is the AWS Well-Architected Games Industry Lens whitepaper; all
measurements were completed in 2026-08.

The infrastructure behind the release pipeline (CDK, Step Functions, the atomic
DynamoDB pointer) lives in [Platform implementation](#7-platform-implementation).
That is the vehicle for items 1 and 4, not the point of this repository.

## 1. Key findings

| Question | Finding |
| --- | --- |
| 50 Markdown files, 5 later modified — how many get updated? | All 50 are rescanned locally and compared against the manifest; only the 5 changed files are uploaded and ingested. |
| Can the official AWS PDF be ingested directly? | The Chinese PDF loses large amounts of CJK text under Smart Parsing; pre-extracting to UTF-8 Markdown restores it. |
| Is semantic chunking always better? | Structure-aware pre-chunking improves Top-10 evidence coverage and traceability but lowers MRR; kept as a canary, not as the baseline. |
| Does metadata improve recall? | No measurable gain from metadata in the embedding; a runtime filter raised MRR on 36 filterable queries from 0.241 to 0.556. |
| Does agentic retrieval always issue follow-up searches? | No. `maxAgentIteration` is a ceiling; `actions=[]` means the planner triggered no follow-up, not that something failed. |
| Can chunk size be customized under managed embedding? | Not currently — the API requires omitting `chunkingConfiguration` and uses the service default of 300 tokens / 20% overlap. |
| What justifies shipping a new KB version? | A paired retrieval comparison over a fixed golden set: ship only when all four metrics have a 95% CI lower bound above -0.02; roll back when any metric's CI lies entirely below zero. |

Full numbers, query cases and evidence are in the
[results report](docs/RESULTS.md).

## 2. Quick start

Prerequisites: Python 3.12, the AWS CLI, temporary AWS credentials for the target
region, and least-privilege permissions to create test resources. Runs incur AWS
charges; use a sandbox account with budget alarms first.

```bash
cp config/test.env.example config/test.env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
aws sts get-caller-identity
```

After editing the git-ignored `config/test.env`, run the baseline experiment:

```bash
./scripts/01_prepare_source.sh
cd infra && npm ci && npx cdk deploy --all --require-approval never && cd ..
./scripts/03_ingest.sh
./scripts/04_test_retrieval.sh
./scripts/06_quality_diagnostics.sh
./scripts/07_verify.sh
```

The baseline path creates test resources, ingests the raw PDF and verifies
retrieval. The streaming agentic retrieval implementation is in
`scripts/08_agentic_retrieval.py`.

## 3. Ingestion pipeline

### 3.1 Four layers of state — drop one and "is the new content live?" has no answer

```text
Git / CMS             system of record
  -> S3 Canonical     candidate copy the connector can see
  -> Published Manifest   approved release contract
  -> KB Index         derived index from the last successful ingestion
```

The consistency condition is that three of them agree:
`expected S3 version == manifest version == indexed KB version`. They may diverge
briefly during asynchronous ingestion, which is why **writing to S3 does not
prove the new content is retrievable** — the document terminal status must be
polled.

### 3.2 What "incremental" means: incremental cloud writes, not a skipped local scan

When 5 of 50 documents change, all 50 are still rescanned locally — that is the
only way to reliably detect deletions, duplicate `document_id` values and
metadata-only changes. Only the 5 changed documents are uploaded and ingested.

```bash
.venv/bin/python -m cli.publish \
  --source-dir examples/corpus \
  --corpus-id demo \
  --canonical-bucket <canonical-bucket> \
  --registry-bucket <registry-bucket> \
  --knowledge-base-id <knowledge-base-id> \
  --data-source-id <data-source-id> \
  --state-machine-arn <state-machine-arn> \
  --release-table <release-table> \
  --source-commit "$(git rev-parse HEAD)" \
  --dry-run
```

`kbp/preparation/corpus.py` handles preparation and the quality gates,
`kbp/preparation/diff.py` compares manifests to derive added/modified/deleted,
and `kbp/ingestion/batching.py` splits batches and binds sidecars. Drop
`--dry-run` to trigger a real execution.

### 3.3 Quality gates during preparation

Any failure rejects the release, because once these problems are indexed only a
rebuild fixes them:

| Gate | Why |
| --- | --- |
| Non-empty body | An empty document produces a meaningless vector |
| No `U+FFFD` | The Chinese PDF loses large amounts of CJK under Smart Parsing — see 3.4 |
| UTF-8 BOM stripped | A BOM invalidates the whole front matter block without erroring; `document_id` and governance fields silently revert to defaults |
| Complete date fields | `2026-08` becomes the number 202608, which sorts wrong against 8-digit dates, and mixing two types in one field breaks metadata filters |
| Unique `document_id` | Duplicate identifiers overwrite each other |
| ≤ 30 MB per document, ≤ 10 KB per sidecar | Managed KB quotas |

### 3.4 The Chinese PDF must be pre-extracted

After `SMART_PARSING`, non-image chunks of the original Chinese PDF lose large
numbers of CJK characters: the sidecar declares `language=zh-CN` while the system
field `_language_code` falls back to `en` everywhere. After pre-extraction to
UTF-8 Markdown, the top score across four targeted retrievals rose from
0.456-0.570 to 0.619-0.735 and `U+FFFD` dropped to zero.

```bash
./scripts/09_prepare_text_repair.sh
./scripts/10_ingest_text_repair.sh
./scripts/11_test_text_repair.sh
```

An enterprise Markdown corpus is already in the repaired state, so the
engineering weight sits not on parsing quality but on update orchestration and
release gates. See the
[enterprise Markdown pipeline](docs/MD_CORPUS_PIPELINE.md).

## 4. Parameter boundaries

Managed KB offers a much narrower tuning surface than Classic KB. Separating
"immutable after creation" from "tunable per retrieval" is a prerequisite for
both selection and tuning.

### 4.1 Decided at creation, immutable afterwards

| Parameter | Values | Cost of changing |
| --- | --- | --- |
| `KnowledgeBaseConfiguration` | `type: MANAGED` | **createOnly** — a change replaces the KB and loses the whole index |
| `embeddingModelType` | `MANAGED` / `CUSTOM` | Same |
| `serverSideEncryptionConfiguration.kmsKeyArn` | CMK ARN | Same |
| Data source `type` | Managed KB accepts only `MANAGED_KNOWLEDGE_BASE_CONNECTOR` | createOnly |
| Chunking strategy | Must omit `chunkingConfiguration` under managed embedding | createOnly |

**A data source with `type: 'S3'` is rejected outright by Managed KB**
(`Unsupported data source type for MANAGED knowledge base type`). The
CloudFormation schema permits the combination syntactically and the service
rejects it semantically — only a live deployment surfaces this. The bucket and
prefixes live inside the `connectorParameters` JSON document, not in
`s3Configuration`:

```json
{"type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
 "managedKnowledgeBaseConnectorConfiguration": {
   "connectorParameters": {
     "type": "S3", "version": "1",
     "connectionConfiguration": {"bucketName": "…"},
     "filterConfiguration": {"inclusionPrefixes": ["canonical/demo/"]}}}}
```

### 4.2 Chunking and embedding: almost no knobs

Managed embedding currently uses the service default of 300 tokens / 20% overlap,
and the API was measured to require omitting `chunkingConfiguration`. Two paths
remain if chunk control is needed: switch to `CUSTOM` embedding, or pre-chunk
**before** ingestion (see the
[semantic chunking experiment](docs/SEMANTIC_CHUNKING_EXPERIMENT.md), whose
conclusion is that the gain is only local).

Parsing likewise offers only `SMART_PARSING` with no alternative — which is
exactly why the Chinese PDF must be pre-extracted.

### 4.3 Deletion protection covers sync jobs only, not direct deletion

`deletionProtectionThreshold` is a **percentage** (0-100) meaning "the largest
share of indexed documents a single sync job may delete". Over the limit it
**skips the deletion phase** rather than failing.

Two consequences are easy to misread:

- It does not constrain `DeleteKnowledgeBaseDocuments`. A release pipeline that
  uses direct deletion is **not protected by it at all**.
- "Skip rather than fail" makes it a hint, not a block.

So the deletion-ratio gate in this repository
([section 6](#6-evaluation-and-release-criteria)) is the only control on the
direct deletion path — not a second line of defense.

### 4.4 Tunable per retrieval

| Parameter | Range | Effect |
| --- | --- | --- |
| `numberOfResults` | 1-100 | Recall coverage vs context cost |
| `rerankingModelType` | `MANAGED` / `CUSTOM` / `NONE` | See the measurements in [section 5](#5-retrieval-tuning) |
| `filter` | Metadata expression | Narrows the candidate set; the largest measured gain |
| `userContext` / guardrail | — | Authorization and content control |

Managed KB retrieval must use `managedSearchConfiguration`. Using
`vectorSearchConfiguration` **silently returns zero hits** without an error.

### 4.5 The three quotas most worth noting

| Quota | Managed KB | Difference from Classic |
| --- | ---: | --- |
| Concurrent ingestion jobs per KB | 50 | Classic allows 1 |
| Data sources per KB | 200 | Classic allows 5 |
| Documents per `IngestKnowledgeBaseDocuments` request | **10** | The user guide says 25, which applies to other KB types; submitting 11 is rejected server-side |

The Managed KB quota table **lists no `StartIngestionJob` rate limit**. Classic's
0.1 rps does not apply to Managed, and carrying it over causes unnecessary
serialization.

### 4.6 Responsibility boundary

```text
S3 / CMS / Git (system of record)
  -> Data preparation + metadata + approval
  -> Managed Connector / Direct Ingestion
  -> Managed parsing + embedding + index + retrieval
  -> Application / Gateway (authentication, authorization, generation, citations)
```

The authoritative copy of metadata belongs in a sidecar next to the system of
record; the vector index inside Managed KB is rebuildable derived state. Gateway
tool authorization, KB filters and end-user authorization are three distinct
layers of control and cannot substitute for one another.

## 5. Retrieval tuning

Everything below comes from a controlled comparison over the same corpus (479
byte-identical documents), 44 queries, 408 `Retrieve` calls and two reranking
modes. Full numbers are in the
[metadata comparison experiment](docs/METADATA_EXPERIMENT.md).

### 5.1 The largest gain: runtime metadata filters

| Technique | Effect |
| --- | --- |
| Metadata included in the embedding | **No measurable gain** (unfiltered recall) |
| Runtime filter | 36 filterable queries: MRR **0.241 → 0.556**, Recall@10 **0.078 → 0.342** |
| Locating a section by its stable control number | MRR reaches **1.000** |

That result drives metadata design directly: make the fields that can be filtered
at retrieval time (`domain`, `topic`, `section_path`) first-class citizens rather
than hoping metadata inside the embedding will help.

```bash
./scripts/17_prepare_metadata_experiment.sh
./scripts/18_ingest_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/19_compare_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/20_expand_metadata_retrieval.sh
```

### 5.2 Metadata field strategy

The defaults in `kbp/preparation/corpus.py` follow from those measurements:

- Governance and authorization fields (`document_id`, `classification`, `owner`,
  `lifecycle_status`, `content_sha256`, `source_path`) are always
  `includeForEmbedding=false`.
- Only `title`, `section_path`, `domain` and `topic` participate in the embedding.
- Directory hierarchy maps automatically onto `domain`/`topic`/`section_path`, so
  runtime filtering has stable business keys — the precondition for the gains in
  5.1.

Two safety boundaries must be enforced in the application layer: a filter only
narrows the candidate set and **does not guarantee a result**, and a permission
filter that matches nothing must fail closed. Any caller holding
`bedrock:Retrieve` can see all ingested content; S3 object permissions do not
automatically become retrieval-layer permissions.

### 5.3 Pre-chunking: a local gain, not enough to replace the baseline

Structure-aware pre-chunking improves Top-10 evidence coverage and traceability
but **lowers MRR**, so it is kept as a canary rather than the default. Design and
numbers are in the
[semantic chunking experiment](docs/SEMANTIC_CHUNKING_EXPERIMENT.md).

```bash
./scripts/14_prepare_semantic_chunks.sh
./scripts/15_ingest_semantic_chunks.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/16_compare_semantic_chunking.sh
```

### 5.4 Two traps that lead to misjudging coverage

**`actions=[]` is not a fault.** In agentic retrieval `maxAgentIteration` is a
ceiling, not a guarantee. In the measured run, planning returned `actions=[]` and
triggered no second retrieval, so a broad question missed cross-section content
that a targeted `Retrieve` does find. Coverage-sensitive evaluation needs targeted
sub-queries or a higher result count; a single broad prompt cannot settle it.

**"Not in the documents" is not "not in the corpus".** The first anti-fraud test
produced a well-worded "the documents do not support this" answer, and later
diagnostics proved the source document did contain the relevant content — the root
cause was Smart Parsing destroying the Chinese chunks. A grounded failure is
evidence about index state, never a conclusion about corpus coverage.

## 6. Evaluation and release criteria

The release decision answers one question: **is this KB version's retrieval
quality no worse than the previous one.** Neither a source diff nor a manifest
diff can answer it — Managed KB exposes no comparable underlying vector index, so
behavioral changes in chunking, embedding and ranking are observable only through
retrieval regression.

### 6.1 Three diffs, all required

| Diff | Answers | How it is computed |
| --- | --- | --- |
| Source diff | What the author changed | Git diff; object SHA-256 for PDFs |
| Release diff | Which documents to add, modify or delete | Compare content and metadata SHA-256 across manifests |
| **Retrieval diff** | **Which recall results the new version changed** | Paired comparison over a fixed golden set |

### 6.2 Decision metrics

`scripts/20_expand_metadata_retrieval.py` already implements the paired
comparison. Each metric reports a mean delta, a bootstrap 95% confidence interval
(5000 resamples, reproducible from a fixed seed) and per-case
improved / tied / regressed counts:

| Metric | Code field | What it captures |
| --- | --- | --- |
| Hit@1 | `hitAt1` | Whether the first result hits — matters most for direct question answering |
| MRR | `reciprocalRank` | Where the correct answer ranks |
| Recall@10 | `recallAt10` | Coverage — critical for questions needing several pieces of evidence |
| nDCG@10 | `nDcgAt10` | Overall ranking quality, weighing both hits and positions |

### 6.3 Ship or roll back

Use the paired comparison rather than absolute values: run the same query set
against both versions and look at the distribution of deltas.

| Situation | Criterion | Action |
| --- | --- | --- |
| No significant regression | All four metrics have a 95% CI lower bound above -0.02 | **Ship** |
| Significant regression | Any metric's 95% CI lies **entirely below zero** | **Roll back** |
| Inconclusive | The CI spans zero and the mean delta is below -0.02 | Enlarge the golden set and re-measure; do not release on this data |

**Why the CI and not just the mean delta:** at a sample size of 44 queries a mean
delta of -0.03 may be noise. Only a CI entirely below zero shows the regression is
systematic. Conversely, an apparent improvement is equally untrustworthy when the
CI spans zero.

The -0.02 threshold is a starting point for this repository's corpus size, not a
universal value. A larger golden set allows tightening it.

### 6.4 The full gate order before release

Destructive operations must come after the gates. This ordering is a lesson from
a real run: an early implementation deleted before gating, and an over-threshold
release had already removed 8 of 13 documents by the time the gate failed.

```text
1. review source-of-record change    (a Git PR is the approval trail)
2. preparation gates                 encoding, empty body, date type, unique id
3. deletion-ratio gate               <-- must precede any deletion
4. ingest                            direct channel for upserts, Delete API for removals
5. document terminal status          only INDEXED counts as success
6. retrieval diff                    paired comparison on the golden set
7. ACL regression                    zero cross-tenant leakage
8. manifest promotion                conditional write, atomic
```

Step 5 deserves separate emphasis: `DocumentStatus` has 12 values and **only
`INDEXED` is full success**. `PARTIALLY_INDEXED` means some chunks failed — the
content is incomplete while the API reports no error, so treating it as success is
silent data corruption.

### 6.5 Regression scope by change type

| Change | Required |
| --- | --- |
| A few documents modified | Full preparation checks; affected golden queries; global critical smoke; ACL |
| Metadata/ACL only | Filter positive and negative tests, cross-tenant leakage, field type and absence tests |
| Documents deleted | Stale-content exclusion, broken citations, deletion and permission regression |
| A full PDF updated | All golden queries related to that PDF; global smoke; parsing and citation sampling |
| Parser/chunking/embedding change | Full golden set, latency/cost, A/B and a rollback rehearsal |

### 6.6 Rollback

The manifest records an `s3VersionId` per document, so rollback is version-exact:
read the old manifest, restore the corresponding S3 versions, re-ingest, then
conditionally write the pointer back. The superseded version is retained as
`SUPERSEDED` rather than deleted, so the fallback stays fast during the
observation window.

## 7. Platform implementation

**This section is the vehicle for the six above, not the point of this
repository.** Readers who only want the ingestion, tuning, retrieval and
evaluation material can skip it. The reason it goes this far is that if the gate
order in 6.4 depended on developer discipline, someone would eventually bypass
it; encoded as state machine topology, it cannot be bypassed.

### 7.1 Three stacks and the release state machine

| Component | Location | Responsibility |
| --- | --- | --- |
| Foundation stack | `infra/lib/foundation-stack.ts` | Canonical bucket, registry bucket, release table |
| KB stack | `infra/lib/knowledge-base-stack.ts` | Managed KB and the connector data source |
| Release stack | `infra/lib/release-stack.ts` | Gate Lambdas and the state machine |
| State machine | `infra/lib/state-machine.ts` | Gate orchestration; any failure is fail-closed |
| Gate pure functions | `kbp/ingestion/gates.py` | All decision logic, unit-testable |
| Release CLI | `cli/publish.py` | Prepare, upload, start the execution |

**Fail-closed by topology, not by discipline:** the non-passing branch of every
Choice state points straight at a terminal Fail, with no "log a warning and
continue" path. Pointer advancement uses a DynamoDB conditional write whose
`expectedPreviousReleaseId` pins the pointer observed when the execution started;
the loser of a concurrent release is rejected at `PromoteRelease`.

Design trade-offs are recorded in the [ADR directory](docs/adr/), and the
end-to-end acceptance record is in
[`tests/integration/test_release_pipeline.md`](tests/integration/test_release_pipeline.md)
(four paths: normal publish, corrupted document blocked, over-threshold deletion
hard-failed, concurrent conditional write rejected).

### 7.2 Extended topics (moved off the main line)

None of this is required for the six sections above; read it as needed:

| Topic | Document |
| --- | --- |
| Selection: managed vs classic vs self-built | [KB platform selection](docs/KB_PLATFORM_SELECTION_GUIDE.md) |
| Official AWS RAG best practices, reviewed | [Best practices report](docs/AWS_KB_RAG_BEST_PRACTICES.md) |
| Enterprise governance and approval model | [Enterprise governance blueprint](docs/ENTERPRISE_GOVERNANCE_BLUEPRINT.md) |
| Control inventory | [Control baseline](docs/CONTROL_BASELINE.md) |
| Observability and the event model | [Observability blueprint](docs/OBSERVABILITY_BLUEPRINT.md) · [event schema](schemas/observability-event.schema.json) · [evidence template](experiments/observability-evidence.template.md) |
| Enterprise scenario experiment route | [Experiment directory](experiments/README.md) |
| Data preparation skill | [kb-rag-data-preparation](.agents/skills/kb-rag-data-preparation/SKILL.md) |
| Early-phase handoff record | [Handoff report](HANDOFF_REPORT.md) |

### 7.3 Official baseline

Reviewed on 2026-08-04; reconfirm against the target account and region before
going live:

- Announced regions include `us-east-1`, `us-west-2`, `eu-west-1`,
  `eu-central-1`, `eu-west-2`, `ap-southeast-2`, `ap-northeast-1` and
  `us-gov-west-1`.
- Default quotas include 10,000 KBs per account/region, 200 data sources per KB,
  50 concurrent ingestion jobs, 10 TB of raw data, 600 Retrieve calls per minute
  per KB, and 60 agentic Retrieve calls per minute per account.
- US region pricing examples: index storage `$5/GB-month`, standard Retrieve
  `$1/1,000 calls`, agentic Retrieve `$4/1,000 calls`; Retrieve calls made inside
  an agentic run are still billed separately.
- CloudFormation/CDK L1 supports `ManagedKnowledgeBaseConfiguration`.

Official sources, publication dates and pinned sample commits are in the
[AWS sample catalog](docs/AWS_SAMPLE_CATALOG.md).

### 7.4 Runtime evidence and safety

Run responses are stored under the git-ignored `artifacts/<RUN_ID>/` and may
contain account IDs, ARNs, bucket names, resource IDs, source paths or retrieved
content — none of which may be committed. Before publishing, run:

```bash
./scripts/12_repository_safety_check.sh
python3 scripts/13_check_readme_sync.py
```

The Chinese and English READMEs must change together; CI checks links, sections
and command blocks. The full policy is in [SECURITY.md](SECURITY.md).

## 8. Known limitations

- The raw Chinese PDF is unsuitable as a production corpus; use the repaired
  Markdown version.
- Semantic chunking shows only a local gain and has not met the bar to replace the
  baseline.
- Metadata filters narrow the candidate set substantially but replace neither
  authentication nor a guarantee that results are returned.
- Transaction Search, vended log delivery and application ADOT must each be
  verified per account, region and resource — checking a console page is not
  enough.
- **There is no independent drift detection:** a tampered S3 object is caught only
  when the next release's manifest happens to cover it, at gate A.
- The golden set currently holds 44 queries, which is a small sample; the -0.02
  threshold in 6.3 is correspondingly conservative.
- Managed KB creation was measured at roughly 24 minutes, far beyond the 2-5
  minutes the documentation states; CI timeouts should not be set to 5 minutes.
- This repository contains no AWS credentials, real account evidence or customer
  data, and it does not deploy production resources automatically.

## 9. Cleanup and license

The cleanup script requires explicit confirmation:

```bash
CONFIRM_DESTROY=DELETE-games-industry-lens-managed-kb-20260803 \
  ./scripts/99_cleanup.sh
```

Project code and original documentation are released under the
[MIT License](LICENSE). AWS service names and the content of official AWS
documentation remain the property of their respective owners.
