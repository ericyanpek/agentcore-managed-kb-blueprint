# Managed KB 平台 Sprint 1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把本仓库从研究型脚本集合升级为平台基线：CDK 管理全部基础设施，远程 Release Registry 保存发布状态，Fail-closed 状态机保证发布失败不污染活动版本。

**Architecture:** 三个 CDK Stack（Foundation / KnowledgeBase / Release）按 stateful 与 stateless 切分。发布由 Step Functions Standard 编排，摄入、删除、终态查询、冒烟检索走 SDK 集成，仅 S3 校验、门禁判定、Registry 读写用 Lambda。所有判定逻辑是 `kbp/` 包中的纯函数，Lambda handler 只做 I/O 适配，因此门禁行为可在无 AWS 环境下测试。

**Tech Stack:** AWS CDK 2.265.0 (TypeScript)、Python 3.12、boto3 1.43.62、pytest、Jest、Step Functions Standard、DynamoDB、S3、KMS。

**Spec:** `docs/superpowers/specs/2026-08-17-managed-kb-platform-sprint1-design.md`

---

## 阅读须知

### 领域背景

Amazon Bedrock **Managed Knowledge Base** 托管解析、Embedding、索引与检索。你不能直接
读写它的向量索引，只能通过 API 提交文档并轮询状态。这带来两个后果，本计划的大部分设计
都是为了应对它们：

1. **摄入是异步的。** `IngestKnowledgeBaseDocuments` 返回 HTTP 202 只表示"请求已接受"，
   不表示文档已可检索。必须轮询 `GetKnowledgeBaseDocuments` 直到终态。
2. **索引是派生状态。** 事实源是 S3 与 Git；索引可以重建。所以"发布"的本质是让三者一致：
   S3 期望版本 == 已发布 Manifest 版本 == KB 已索引版本。

**Manifest** 是本项目的核心契约：一个 JSON 文件，记录某次发布包含哪些文档、每个文档内容
与 metadata 的 SHA-256。两次 Manifest 相比即得出 added/modified/deleted。

**Fail-closed** 指：任何门禁不通过时，系统停在原地且活动版本不受影响，而不是"记录告警后
继续"。本计划中它由状态机拓扑保证——门禁到 Promotion 不存在绕行路径。

### 关键陷阱（会导致静默数据损坏或直接崩溃）

| 陷阱 | 说明 |
| --- | --- |
| `DocumentStatus` 仅 `INDEXED` 是成功 | 共 12 个枚举值。`PARTIALLY_INDEXED` 表示部分分块失败——内容不完整但 API 不报错。判为成功等于静默数据损坏 |
| 顶层包不能叫 `platform` | 会遮蔽 Python 标准库；botocore 调 `platform.system()` 构造 User-Agent 时抛 `AttributeError`。本计划用 `kbp` |
| `clientToken` 字符集受限 | 33–256 字符，仅字母数字与连字符。下划线、点、斜杠非法。必须用 SHA-256 派生 |
| 删除比例分母是**发布前**总数 | 用发布后总数做分母时，全量删除会算成 0%，删除保护完全失效 |
| Managed KB 检索用 `managedSearchConfiguration` | 用 `vectorSearchConfiguration` 会静默返回零命中 |
| `ManagedKnowledgeBaseConfiguration` 是 createOnly | 改 embedding 配置会替换 KB 并丢失索引 |

### 开发环境准备

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest
```

本计划中所有 `pytest` 命令假定使用 `.venv/bin/pytest`。为简洁起见后文写作 `pytest`，
执行时请用 `.venv/bin/pytest` 或先激活虚拟环境。

沙箱 AWS 凭证需具备控制面权限。每个真实调用 AWS 的任务都会显式标注。

---

## 文件结构

实施顺序遵循依赖方向：先建可独立测试的纯函数，再建基础设施，最后接线。

| 文件 | 职责 |
| --- | --- |
| `kbp/__init__.py` | 包标记，空文件 |
| `kbp/preparation/corpus.py` | 扫描语料、质量门禁、生成 Manifest。自 `scripts/21` 迁入 |
| `kbp/preparation/diff.py` | 两份 Manifest 比对，输出 added/modified/deleted |
| `kbp/registry/manifest.py` | Manifest 数据结构、releaseId 与 clientToken 派生 |
| `kbp/registry/store.py` | DynamoDB 条件写、S3 Manifest 读写 |
| `kbp/ingestion/batching.py` | 变更集切分为批次、构造摄入 payload |
| `kbp/ingestion/gates.py` | 四道门禁的纯函数判定逻辑 |
| `kbp/ingestion/handlers/verify_s3.py` | 门禁 A 的 Lambda handler |
| `kbp/ingestion/handlers/check_gates.py` | 门禁 B/C/D 判定的 Lambda handler |
| `kbp/ingestion/handlers/registry_ops.py` | Registry 读写与 Promotion 的 Lambda handler |
| `kbp/probes/assumptions.py` | A1/A2 探针，重写 `scripts/23` |
| `cli/publish.py` | 本地发布入口 |
| `schemas/release-manifest.schema.json` | Manifest JSON Schema |
| `examples/corpus/` | 验收用固定小规模语料 |
| `infra/lib/foundation-stack.ts` | KMS、canonical 桶、registry 桶、日志组 |
| `infra/lib/knowledge-base-stack.ts` | KB service role、Managed KB、Data Source |
| `infra/lib/release-stack.ts` | DynamoDB、3 个 Lambda、状态机、publisher role |
| `infra/lib/state-machine.ts` | 状态机定义，从 release-stack 拆出以保持文件聚焦 |

**为什么状态机单独一个文件**：九步拓扑加重试策略会让 `release-stack.ts` 膨胀到难以在
一个上下文里通读。Stack 负责资源与权限，状态机负责流程，两者变更原因不同。

---

## Task 1: 建立 pytest 基础与 kbp 包骨架

**Files:**
- Create: `kbp/__init__.py`
- Create: `kbp/preparation/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/unit/test_package_layout.py`
- Modify: `requirements.txt`

这个任务先锁定"包名不遮蔽标准库"这一约束，用一个测试把它固定下来，避免后续任何人改回
`platform/`。

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_package_layout.py`：

```python
"""Guard the package layout invariants that would otherwise fail at runtime."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def test_kbp_package_is_importable():
    import kbp

    assert Path(kbp.__file__).parent == ROOT / "kbp"


def test_no_top_level_package_shadows_stdlib():
    """A top-level `platform` package breaks botocore's User-Agent construction.

    botocore calls platform.system(); if a local package shadows the stdlib
    module, every boto3 call from the repository root raises AttributeError.
    """
    shadowed = [
        name
        for name in ("platform", "types", "json", "io", "select", "code")
        if (ROOT / name / "__init__.py").exists()
    ]
    assert shadowed == []


def test_boto3_works_from_repository_root():
    result = subprocess.run(
        [sys.executable, "-c", "import boto3; boto3.session.Session()"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/unit/test_package_layout.py -v`

Expected: FAIL —— `ModuleNotFoundError: No module named 'kbp'`（pytest 未安装时先执行
下一步的依赖安装）。

- [ ] **Step 3: 建立包骨架与依赖**

创建 `kbp/__init__.py`（空文件）与 `kbp/preparation/__init__.py`（空文件），以及
`tests/unit/__init__.py`（空文件）。

修改 `requirements.txt`，追加 pytest：

```text
boto3==1.43.62
pypdf==6.10.2
pytest==8.4.2
```

安装：`.venv/bin/pip install -r requirements.txt`

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/unit/test_package_layout.py -v`

Expected: 3 passed。

- [ ] **Step 5: 提交**

```bash
git add kbp/__init__.py kbp/preparation/__init__.py tests/unit/__init__.py \
  tests/unit/test_package_layout.py requirements.txt
git commit -m "Add kbp package skeleton with layout guard tests

Lock in the constraint that no top-level package may shadow a stdlib
module, since a platform package would break botocore at import time."
```

---

## Task 2: 迁移语料准备逻辑到 kbp/preparation

**Files:**
- Create: `kbp/preparation/corpus.py`
- Create: `kbp/preparation/diff.py`
- Create: `tests/unit/test_corpus_preparation.py`
- Modify: `tests/test_data_preparation.py`
- Delete: `scripts/21_prepare_md_corpus.py`, `scripts/21_prepare_md_corpus.sh`

`scripts/21_prepare_md_corpus.py` 已经是"纯函数 + 薄 CLI"的形状，本任务是搬移加去掉
argparse 依赖，让函数签名接收显式参数而非 `argparse.Namespace`——后者使函数难以从
Lambda 调用。

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_corpus_preparation.py`：

```python
import json
from pathlib import Path

import pytest

from kbp.preparation import corpus, diff


def write_corpus(root: Path, documents: dict[str, str]) -> Path:
    source = root / "source"
    for relative_path, text in documents.items():
        target = source / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return source


def test_prepare_derives_domain_and_topic_from_directory_layout(tmp_path):
    source = write_corpus(
        tmp_path,
        {"security/anti-cheat/overview.md": "# Overview\n\nBody text.\n"},
    )

    manifest = corpus.prepare(
        source_dir=source,
        output_dir=tmp_path / "canonical",
        corpus_id="demo",
        embedded_fields=("title", "section_path", "domain", "topic"),
    )

    assert manifest["documentCount"] == 1
    sidecar = json.loads(
        (tmp_path / "canonical" / "security" / "anti-cheat" / "overview.md.metadata.json")
        .read_text(encoding="utf-8")
    )
    attributes = sidecar["metadataAttributes"]
    assert attributes["domain"]["value"]["stringValue"] == "security"
    assert attributes["topic"]["value"]["stringValue"] == "anti-cheat"


def test_governance_fields_never_participate_in_embedding(tmp_path):
    source = write_corpus(tmp_path, {"doc.md": "# Title\n\nBody.\n"})

    corpus.prepare(
        source_dir=source,
        output_dir=tmp_path / "canonical",
        corpus_id="demo",
        embedded_fields=("title", "section_path", "domain", "topic"),
    )

    sidecar = json.loads(
        (tmp_path / "canonical" / "doc.md.metadata.json").read_text(encoding="utf-8")
    )
    attributes = sidecar["metadataAttributes"]
    assert attributes["title"]["includeForEmbedding"] is True
    for governance_field in ("document_id", "classification", "content_sha256"):
        assert attributes[governance_field]["includeForEmbedding"] is False


@pytest.mark.parametrize(
    ("documents", "message"),
    [
        ({"empty.md": "---\ntitle: x\n---\n"}, "empty after front matter"),
        ({"broken.md": "# Title\n\nbad � char\n"}, "U+FFFD"),
    ],
)
def test_preparation_gates_reject_bad_documents(tmp_path, documents, message):
    source = write_corpus(tmp_path, documents)

    with pytest.raises(ValueError, match=message):
        corpus.prepare(
            source_dir=source,
            output_dir=tmp_path / "canonical",
            corpus_id="demo",
            embedded_fields=("title",),
        )


def test_duplicate_document_id_is_rejected(tmp_path):
    source = write_corpus(
        tmp_path,
        {
            "a.md": "---\ndocument_id: same\n---\n# A\n\nBody.\n",
            "b.md": "---\ndocument_id: same\n---\n# B\n\nBody.\n",
        },
    )

    with pytest.raises(ValueError, match="duplicate document id"):
        corpus.prepare(
            source_dir=source,
            output_dir=tmp_path / "canonical",
            corpus_id="demo",
            embedded_fields=("title",),
        )


def test_metadata_only_change_is_reported_as_modified(tmp_path):
    source = write_corpus(tmp_path, {"doc.md": "# Title\n\nBody.\n"})
    first = corpus.prepare(
        source_dir=source,
        output_dir=tmp_path / "v1",
        corpus_id="demo",
        embedded_fields=("title",),
    )

    (source / "doc.md").write_text(
        "---\nclassification: CONFIDENTIAL\n---\n# Title\n\nBody.\n",
        encoding="utf-8",
    )
    second = corpus.prepare(
        source_dir=source,
        output_dir=tmp_path / "v2",
        corpus_id="demo",
        embedded_fields=("title",),
    )

    changes = diff.diff_manifests(first, second)
    assert len(changes["modified"]) == 1
    assert changes["added"] == []
    assert changes["deleted"] == []


def test_initial_load_marks_every_document_as_added(tmp_path):
    source = write_corpus(
        tmp_path, {f"doc-{index}.md": f"# D{index}\n\nBody.\n" for index in range(3)}
    )
    manifest = corpus.prepare(
        source_dir=source,
        output_dir=tmp_path / "canonical",
        corpus_id="demo",
        embedded_fields=("title",),
    )

    changes = diff.diff_manifests(None, manifest)
    assert len(changes["added"]) == 3
    assert changes["modified"] == []
    assert changes["deleted"] == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/unit/test_corpus_preparation.py -v`

Expected: FAIL —— `ImportError: cannot import name 'corpus' from 'kbp.preparation'`。

- [ ] **Step 3: 实现 corpus.py**

创建 `kbp/preparation/corpus.py`。逻辑自 `scripts/21_prepare_md_corpus.py` 迁入，改动
两处：`prepare()` 接收显式关键字参数而非 `argparse.Namespace`；`diff_manifests` 移到
`diff.py`。

```python
"""Normalize an authored Markdown corpus into canonical objects and a manifest."""

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path

REPLACEMENT_CHARACTER = "�"
MAX_EXTRACTED_TEXT_BYTES = 30 * 1024 * 1024
MAX_SIDECAR_BYTES = 10 * 1024
FRONT_MATTER_PATTERN = re.compile(r"\A---\n(.*?)\n---\n", flags=re.DOTALL)
HEADING_PATTERN = re.compile(r"^#\s+(.+)$", flags=re.MULTILINE)

GOVERNANCE_FIELDS = frozenset(
    {
        "document_id",
        "corpus_id",
        "source_path",
        "content_format",
        "content_sha256",
        "classification",
        "owner",
        "language",
        "lifecycle_status",
    }
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_front_matter(markdown: str) -> tuple[dict[str, str], str]:
    match = FRONT_MATTER_PATTERN.match(markdown)
    if match is None:
        return {}, markdown
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            continue
        fields[key.strip()] = value.strip().strip("'\"")
    return fields, markdown[match.end() :]


def derive_document_id(relative_path: Path) -> str:
    slug = relative_path.with_suffix("").as_posix()
    slug = unicodedata.normalize("NFKC", slug).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a document id from {relative_path}")
    return slug


def derive_title(front_matter: dict[str, str], body: str, relative_path: Path) -> str:
    if front_matter.get("title"):
        return front_matter["title"]
    heading = HEADING_PATTERN.search(body)
    if heading:
        return heading.group(1).strip()
    return relative_path.stem


def metadata_value(value: str | int, *, include_for_embedding: bool) -> dict:
    if isinstance(value, int):
        typed_value = {"type": "NUMBER", "numberValue": value}
    else:
        typed_value = {"type": "STRING", "stringValue": value}
    return {"value": typed_value, "includeForEmbedding": include_for_embedding}


def build_metadata(
    *,
    document_id: str,
    corpus_id: str,
    front_matter: dict[str, str],
    title: str,
    relative_path: Path,
    content_sha256: str,
    embedded_fields: frozenset[str],
) -> dict:
    section_path = relative_path.parent.as_posix()
    if section_path == ".":
        section_path = ""
    section_parts = [part for part in section_path.split("/") if part]

    values: dict[str, str | int] = {
        "document_id": document_id,
        "corpus_id": corpus_id,
        "title": title,
        "source_path": relative_path.as_posix(),
        "content_format": "authored-markdown",
        "content_sha256": content_sha256,
        "classification": front_matter.get("classification", "INTERNAL"),
        "owner": front_matter.get("owner", ""),
        "language": front_matter.get("language", ""),
        "lifecycle_status": front_matter.get("lifecycle_status", "ACTIVE"),
        "section_path": section_path,
        "domain": section_parts[0] if section_parts else "",
        "topic": section_parts[1] if len(section_parts) > 1 else "",
    }
    for optional_name in ("version_date", "effective_date", "expires_on"):
        raw_value = front_matter.get(optional_name, "")
        if raw_value:
            digits = re.sub(r"[^0-9]", "", raw_value)
            values[optional_name] = int(digits) if digits else raw_value

    attributes = {
        name: metadata_value(
            value,
            include_for_embedding=name in embedded_fields
            and name not in GOVERNANCE_FIELDS,
        )
        for name, value in values.items()
        if value != ""
    }
    return {"metadataAttributes": attributes}


def assert_quality(*, relative_path: Path, body: str, content_bytes: bytes) -> None:
    if not body.strip():
        raise ValueError(f"document is empty after front matter: {relative_path}")
    if REPLACEMENT_CHARACTER in body:
        raise ValueError(f"document contains U+FFFD: {relative_path}")
    if len(content_bytes) > MAX_EXTRACTED_TEXT_BYTES:
        raise ValueError(
            f"document exceeds the 30 MB managed knowledge base limit: {relative_path}"
        )


def prepare(
    *,
    source_dir: Path,
    output_dir: Path,
    corpus_id: str,
    embedded_fields: Iterable[str],
) -> dict:
    source_paths = sorted(
        path
        for path in source_dir.rglob("*.md")
        if path.is_file() and not path.name.endswith(".metadata.json")
    )
    if not source_paths:
        raise ValueError(f"no Markdown documents found under {source_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    embedded = frozenset(field.strip() for field in embedded_fields if field.strip())

    documents = []
    seen_ids: dict[str, Path] = {}
    for source_path in source_paths:
        relative_path = source_path.relative_to(source_dir)
        markdown = source_path.read_text(encoding="utf-8")
        front_matter, body = parse_front_matter(markdown)
        content = body.strip() + "\n"
        content_bytes = content.encode("utf-8")
        assert_quality(
            relative_path=relative_path, body=body, content_bytes=content_bytes
        )

        document_id = front_matter.get("document_id") or derive_document_id(
            relative_path
        )
        if document_id in seen_ids:
            raise ValueError(
                f"duplicate document id {document_id}: "
                f"{seen_ids[document_id]} and {relative_path}"
            )
        seen_ids[document_id] = relative_path

        destination = output_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content_bytes)

        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        metadata = build_metadata(
            document_id=document_id,
            corpus_id=corpus_id,
            front_matter=front_matter,
            title=derive_title(front_matter, body, relative_path),
            relative_path=relative_path,
            content_sha256=content_sha256,
            embedded_fields=embedded,
        )
        metadata_path = destination.with_name(f"{destination.name}.metadata.json")
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        sidecar_bytes = metadata_path.stat().st_size
        if sidecar_bytes > MAX_SIDECAR_BYTES:
            raise ValueError(f"metadata sidecar exceeds 10 KB: {metadata_path}")

        documents.append(
            {
                "documentId": document_id,
                "file": relative_path.as_posix(),
                "contentSha256": content_sha256,
                "metadataSha256": file_sha256(metadata_path),
                "contentBytes": len(content_bytes),
                "sidecarBytes": sidecar_bytes,
            }
        )

    return {
        "corpusId": corpus_id,
        "contentFormat": "authored-markdown",
        "embeddedMetadataFields": sorted(embedded),
        "documentCount": len(documents),
        "totalContentBytes": sum(item["contentBytes"] for item in documents),
        "documents": documents,
        "corpusSha256": hashlib.sha256(
            "".join(
                f"{item['documentId']}:{item['contentSha256']}:{item['metadataSha256']}\n"
                for item in documents
            ).encode()
        ).hexdigest(),
    }
```

创建 `kbp/preparation/diff.py`：

```python
"""Compare two corpus manifests to derive the change set for a release."""


def diff_manifests(previous: dict | None, current: dict) -> dict:
    """Return added/modified/deleted document entries.

    `previous` is None for an initial load, in which case every document in
    `current` is reported as added.
    """
    previous_documents = {
        item["file"]: item for item in (previous or {}).get("documents", [])
    }
    current_documents = {item["file"]: item for item in current["documents"]}

    added = sorted(set(current_documents) - set(previous_documents))
    deleted = sorted(set(previous_documents) - set(current_documents))
    modified = sorted(
        name
        for name in set(current_documents) & set(previous_documents)
        if (
            current_documents[name]["contentSha256"]
            != previous_documents[name]["contentSha256"]
            or current_documents[name]["metadataSha256"]
            != previous_documents[name]["metadataSha256"]
        )
    )
    return {
        "added": [current_documents[name] for name in added],
        "modified": [current_documents[name] for name in modified],
        "deleted": [previous_documents[name] for name in deleted],
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/unit/test_corpus_preparation.py -v`

Expected: 7 passed（含参数化的 2 项）。

- [ ] **Step 5: 修正既有测试的加载路径**

`tests/test_data_preparation.py` 通过 `importlib` 按路径加载 `scripts/21`，该文件即将
删除。修改两处。

删除这两行：

```python
md_corpus = load_module("md_corpus", "scripts/21_prepare_md_corpus.py")
md_ingestion = load_module("md_ingestion", "scripts/22_incremental_ingest.py")
```

在 `ROOT` 定义之后、`load_module` 定义之前插入：

```python
sys.path.insert(0, str(ROOT))

from kbp.preparation import corpus as md_corpus  # noqa: E402
from kbp.preparation import diff as md_diff  # noqa: E402
```

该文件中 `md_corpus.prepare(argparse.Namespace(...))` 的三处调用改为关键字参数形式。
第一处（初始 50 篇）：

```python
            initial = md_corpus.prepare(
                source_dir=source,
                output_dir=root / "prepared-v1",
                corpus_id="enterprise-domain",
                embedded_fields=("title", "section_path", "domain", "topic"),
            )
            initial_changes = md_diff.diff_manifests(None, initial)
```

第二处（更新后）：

```python
            updated = md_corpus.prepare(
                source_dir=source,
                output_dir=root / "prepared-v2",
                corpus_id="enterprise-domain",
                embedded_fields=("title", "section_path", "domain", "topic"),
            )
            update_changes = md_diff.diff_manifests(initial, updated)
```

`md_ingestion.plan` 的两处调用与 `ingestion_plan_args` 静态方法暂时保留——`scripts/22`
在 Task 4 才迁移。本步只需让 `md_ingestion` 仍可加载，故保留其 `load_module` 行：

```python
md_ingestion = load_module("md_ingestion", "scripts/22_incremental_ingest.py")
```

- [ ] **Step 6: 运行全部测试**

Run: `pytest tests/ -v`

Expected: 全部 passed。既有的 `test_markdown_corpus_initial_load_and_five_file_update_are_incremental`
仍通过，证明迁移未改变行为。

- [ ] **Step 7: 删除已迁移脚本并提交**

```bash
git rm scripts/21_prepare_md_corpus.py scripts/21_prepare_md_corpus.sh
git add kbp/preparation/corpus.py kbp/preparation/diff.py \
  tests/unit/test_corpus_preparation.py tests/test_data_preparation.py
git commit -m "Move corpus preparation into the kbp package

Replace the argparse.Namespace parameter with explicit keyword arguments so
the same functions can be called from a Lambda handler, and separate manifest
diffing from preparation."
```

---

## Task 3: Manifest 契约、releaseId 与 clientToken 派生

**Files:**
- Create: `kbp/registry/__init__.py`
- Create: `kbp/registry/manifest.py`
- Create: `schemas/release-manifest.schema.json`
- Create: `tests/unit/test_manifest.py`

`clientToken` 的字符集约束是本任务的核心。直接拼接 `releaseId + "_batch_1"` 会被
`ValidationException` 拒绝——下划线非法。

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_manifest.py`：

```python
import json
import re
from pathlib import Path

import pytest

from kbp.registry import manifest

ROOT = Path(__file__).resolve().parent.parent.parent

CLIENT_TOKEN_PATTERN = re.compile(r"\A[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}\Z")


def test_release_id_is_derived_from_corpus_timestamp_and_hash():
    release_id = manifest.build_release_id(
        corpus_id="demo-corpus",
        timestamp="20260817T101500Z",
        corpus_sha256="abcdef1234567890" * 4,
    )

    assert release_id == "demo-corpus-20260817T101500Z-abcdef12"


@pytest.mark.parametrize(
    ("operation", "batch_index"),
    [("ingest", 0), ("ingest", 7), ("delete", 0), ("promote", 0)],
)
def test_client_token_satisfies_api_constraints(operation, batch_index):
    """clientToken must be 33-256 chars of alphanumerics and hyphens only.

    Underscores, dots and slashes are rejected by the API, so naive string
    concatenation of a releaseId fails at runtime.
    """
    token = manifest.build_client_token(
        release_id="demo-corpus-20260817T101500Z-abcdef12",
        operation=operation,
        batch_index=batch_index,
    )

    assert 33 <= len(token) <= 256
    assert CLIENT_TOKEN_PATTERN.match(token), token


def test_client_token_is_deterministic_for_retries():
    kwargs = {
        "release_id": "demo-20260817T101500Z-abcdef12",
        "operation": "ingest",
        "batch_index": 3,
    }
    assert manifest.build_client_token(**kwargs) == manifest.build_client_token(
        **kwargs
    )


def test_client_token_differs_across_batches_and_operations():
    base = {"release_id": "demo-20260817T101500Z-abcdef12"}
    tokens = {
        manifest.build_client_token(**base, operation=operation, batch_index=index)
        for operation in ("ingest", "delete")
        for index in range(3)
    }
    assert len(tokens) == 6


def test_release_manifest_matches_published_schema():
    schema = json.loads(
        (ROOT / "schemas" / "release-manifest.schema.json").read_text(encoding="utf-8")
    )
    document = manifest.build_release_manifest(
        release_id="demo-20260817T101500Z-abcdef12",
        parent_release_id=None,
        corpus_manifest={
            "corpusId": "demo",
            "corpusSha256": "a" * 64,
            "documentCount": 1,
            "documents": [
                {
                    "documentId": "doc",
                    "file": "doc.md",
                    "contentSha256": "b" * 64,
                    "metadataSha256": "c" * 64,
                }
            ],
        },
        change_counts={"added": 1, "modified": 0, "deleted": 0},
        source_commit="0" * 40,
    )

    for required_field in schema["required"]:
        assert required_field in document
    assert document["status"] == "CANDIDATE"
    assert document["parentReleaseId"] is None


def test_documents_carry_s3_version_id_slot_for_rollback():
    document = manifest.build_release_manifest(
        release_id="demo-20260817T101500Z-abcdef12",
        parent_release_id="demo-20260810T101500Z-99999999",
        corpus_manifest={
            "corpusId": "demo",
            "corpusSha256": "a" * 64,
            "documentCount": 1,
            "documents": [
                {
                    "documentId": "doc",
                    "file": "doc.md",
                    "contentSha256": "b" * 64,
                    "metadataSha256": "c" * 64,
                }
            ],
        },
        change_counts={"added": 0, "modified": 1, "deleted": 0},
        source_commit="0" * 40,
    )

    assert document["documents"][0]["s3VersionId"] is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/unit/test_manifest.py -v`

Expected: FAIL —— `ModuleNotFoundError: No module named 'kbp.registry'`。

- [ ] **Step 3: 实现 manifest.py 与 schema**

创建 `kbp/registry/__init__.py`（空文件）。

创建 `kbp/registry/manifest.py`：

```python
"""Release manifest construction and identifier derivation."""

import hashlib

CLIENT_TOKEN_LENGTH = 40


def build_release_id(*, corpus_id: str, timestamp: str, corpus_sha256: str) -> str:
    """Build a release identifier.

    The timestamp must already be compact (YYYYMMDDTHHMMSSZ); colons would be
    rejected downstream when the value is embedded in resource identifiers.
    """
    return f"{corpus_id}-{timestamp}-{corpus_sha256[:8]}"


def build_client_token(*, release_id: str, operation: str, batch_index: int) -> str:
    """Derive an idempotency token that satisfies the API character set.

    The API requires 33-256 characters matching [a-zA-Z0-9](-*[a-zA-Z0-9]){0,256},
    so underscores, dots and slashes are rejected. Hashing yields hexadecimal
    output of constant length, and is deterministic so a state machine retry of
    the same batch reuses the same token.
    """
    digest = hashlib.sha256(
        f"{release_id}|{operation}|{batch_index}".encode()
    ).hexdigest()
    return digest[:CLIENT_TOKEN_LENGTH]


def build_release_manifest(
    *,
    release_id: str,
    parent_release_id: str | None,
    corpus_manifest: dict,
    change_counts: dict,
    source_commit: str,
) -> dict:
    """Wrap a corpus manifest into an immutable release manifest.

    s3VersionId is reserved on each document so a later rollback can restore the
    exact object version this release published.
    """
    return {
        "releaseId": release_id,
        "parentReleaseId": parent_release_id,
        "corpusId": corpus_manifest["corpusId"],
        "corpusSha256": corpus_manifest["corpusSha256"],
        "sourceCommit": source_commit,
        "documentCount": corpus_manifest["documentCount"],
        "changeCounts": change_counts,
        "status": "CANDIDATE",
        "documents": [
            {
                "documentId": item["documentId"],
                "file": item["file"],
                "contentSha256": item["contentSha256"],
                "metadataSha256": item["metadataSha256"],
                "s3VersionId": item.get("s3VersionId"),
            }
            for item in corpus_manifest["documents"]
        ],
    }
```

创建 `schemas/release-manifest.schema.json`：

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/ericyanpek/agentcore-managed-kb/schemas/release-manifest.schema.json",
  "title": "Release manifest",
  "description": "Immutable record of the documents published by one release.",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "releaseId",
    "parentReleaseId",
    "corpusId",
    "corpusSha256",
    "sourceCommit",
    "documentCount",
    "changeCounts",
    "status",
    "documents"
  ],
  "properties": {
    "releaseId": {
      "type": "string",
      "pattern": "^[a-zA-Z0-9][a-zA-Z0-9-]{0,255}$"
    },
    "parentReleaseId": {
      "type": ["string", "null"],
      "description": "Null for the first release of a corpus."
    },
    "corpusId": { "type": "string", "minLength": 1 },
    "corpusSha256": { "type": "string", "pattern": "^[a-f0-9]{64}$" },
    "sourceCommit": { "type": "string", "minLength": 1 },
    "documentCount": { "type": "integer", "minimum": 0 },
    "changeCounts": {
      "type": "object",
      "additionalProperties": false,
      "required": ["added", "modified", "deleted"],
      "properties": {
        "added": { "type": "integer", "minimum": 0 },
        "modified": { "type": "integer", "minimum": 0 },
        "deleted": { "type": "integer", "minimum": 0 }
      }
    },
    "status": {
      "type": "string",
      "enum": ["CANDIDATE", "ACTIVE", "SUPERSEDED", "FAILED"]
    },
    "documents": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "documentId",
          "file",
          "contentSha256",
          "metadataSha256",
          "s3VersionId"
        ],
        "properties": {
          "documentId": { "type": "string", "minLength": 1 },
          "file": { "type": "string", "minLength": 1 },
          "contentSha256": { "type": "string", "pattern": "^[a-f0-9]{64}$" },
          "metadataSha256": { "type": "string", "pattern": "^[a-f0-9]{64}$" },
          "s3VersionId": {
            "type": ["string", "null"],
            "description": "Populated after upload; enables version-exact rollback."
          }
        }
      }
    }
  }
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/unit/test_manifest.py -v`

Expected: 10 passed（含参数化的 4 项）。

- [ ] **Step 5: 提交**

```bash
git add kbp/registry/__init__.py kbp/registry/manifest.py \
  schemas/release-manifest.schema.json tests/unit/test_manifest.py
git commit -m "Add release manifest contract and identifier derivation

Derive clientToken by hashing rather than concatenating identifiers, because
the API rejects underscores and only accepts 33-256 alphanumeric-or-hyphen
characters."
```

---

## Task 4: 批次切分与摄入 payload 构造

**Files:**
- Create: `kbp/ingestion/__init__.py`
- Create: `kbp/ingestion/batching.py`
- Create: `tests/unit/test_batching.py`
- Modify: `tests/test_data_preparation.py`
- Delete: `scripts/22_incremental_ingest.py`, `scripts/22_incremental_ingest.sh`

批次上限 10 是 API 硬约束（`KnowledgeBaseDocuments` 与 `DocumentIdentifiers` 列表
`max: 10`）。payload 必须显式携带 `metadata.type=S3_LOCATION` 指向 sidecar，这关闭
HANDOFF 第 4 项。

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_batching.py`：

```python
import pytest

from kbp.ingestion import batching


def document(name: str) -> dict:
    return {
        "documentId": name,
        "file": f"{name}.md",
        "contentSha256": "a" * 64,
        "metadataSha256": "b" * 64,
    }


@pytest.mark.parametrize(
    ("count", "expected_batches", "expected_last_size"),
    [(0, 0, None), (1, 1, 1), (10, 1, 10), (11, 2, 1), (25, 3, 5)],
)
def test_batches_respect_the_api_limit_of_ten(
    count, expected_batches, expected_last_size
):
    documents = [document(f"doc-{index}") for index in range(count)]

    batches = batching.split_batches(documents)

    assert len(batches) == expected_batches
    assert all(len(batch) <= batching.MAX_DOCUMENTS_PER_REQUEST for batch in batches)
    if expected_batches:
        assert len(batches[-1]) == expected_last_size


def test_ingest_payload_binds_the_metadata_sidecar_explicitly():
    payload = batching.build_ingest_payload(
        documents=[document("doc-1")],
        bucket="canonical-bucket",
        prefix="canonical/demo",
    )

    assert len(payload) == 1
    entry = payload[0]
    assert entry["content"]["dataSourceType"] == "S3"
    assert (
        entry["content"]["s3"]["s3Location"]["uri"]
        == "s3://canonical-bucket/canonical/demo/doc-1.md"
    )
    assert entry["metadata"]["type"] == "S3_LOCATION"
    assert (
        entry["metadata"]["s3Location"]["uri"]
        == "s3://canonical-bucket/canonical/demo/doc-1.md.metadata.json"
    )


def test_delete_identifiers_use_s3_uris():
    identifiers = batching.build_delete_identifiers(
        documents=[document("doc-1")],
        bucket="canonical-bucket",
        prefix="canonical/demo",
    )

    assert identifiers == [
        {
            "dataSourceType": "S3",
            "s3": {"uri": "s3://canonical-bucket/canonical/demo/doc-1.md"},
        }
    ]


def test_prefix_trailing_slash_does_not_produce_double_separator():
    payload = batching.build_ingest_payload(
        documents=[document("doc-1")],
        bucket="canonical-bucket",
        prefix="canonical/demo/",
    )

    assert "//" not in payload[0]["content"]["s3"]["s3Location"]["uri"].removeprefix(
        "s3://"
    )


def test_nested_paths_are_preserved_in_object_keys():
    nested = {
        "documentId": "security-anti-cheat-overview",
        "file": "security/anti-cheat/overview.md",
        "contentSha256": "a" * 64,
        "metadataSha256": "b" * 64,
    }

    payload = batching.build_ingest_payload(
        documents=[nested], bucket="bucket", prefix="canonical/demo"
    )

    assert (
        payload[0]["content"]["s3"]["s3Location"]["uri"]
        == "s3://bucket/canonical/demo/security/anti-cheat/overview.md"
    )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/unit/test_batching.py -v`

Expected: FAIL —— `ModuleNotFoundError: No module named 'kbp.ingestion'`。

- [ ] **Step 3: 实现 batching.py**

创建 `kbp/ingestion/__init__.py`（空文件）。

创建 `kbp/ingestion/batching.py`：

```python
"""Split a change set into API-sized batches and build request payloads."""

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
    return f"s3://{bucket}/{prefix.strip('/')}/{file}"


def build_ingest_payload(
    *, documents: list[dict], bucket: str, prefix: str
) -> list[dict]:
    """Build IngestKnowledgeBaseDocuments entries.

    The metadata sidecar is bound explicitly via S3_LOCATION; without it the
    service would index the document without its governance and filter
    attributes.
    """
    return [
        {
            "content": {
                "dataSourceType": "S3",
                "s3": {
                    "s3Location": {
                        "uri": _object_uri(
                            bucket=bucket, prefix=prefix, file=item["file"]
                        )
                    }
                },
            },
            "metadata": {
                "type": "S3_LOCATION",
                "s3Location": {
                    "uri": _object_uri(
                        bucket=bucket,
                        prefix=prefix,
                        file=f"{item['file']}.metadata.json",
                    )
                },
            },
        }
        for item in documents
    ]


def build_delete_identifiers(
    *, documents: list[dict], bucket: str, prefix: str
) -> list[dict]:
    """Build DocumentIdentifier entries for DeleteKnowledgeBaseDocuments."""
    return [
        {
            "dataSourceType": "S3",
            "s3": {
                "uri": _object_uri(bucket=bucket, prefix=prefix, file=item["file"])
            },
        }
        for item in documents
    ]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/unit/test_batching.py -v`

Expected: 9 passed（含参数化的 5 项）。

- [ ] **Step 5: 从既有测试中移除 scripts/22 依赖**

`scripts/22_incremental_ingest.py` 即将删除。编辑 `tests/test_data_preparation.py`：

删除这一行：

```python
md_ingestion = load_module("md_ingestion", "scripts/22_incremental_ingest.py")
```

删除 `ingestion_plan_args` 静态方法（整个方法块）。

在 `test_markdown_corpus_initial_load_and_five_file_update_are_incremental` 中，删除
两处 `initial_report` / `update_report` 的构造与 `md_ingestion.plan` 调用及其断言，改为
直接用 `kbp.ingestion.batching` 断言批次数。在文件顶部 import 区加入：

```python
from kbp.ingestion import batching  # noqa: E402
```

初始加载处的断言改为：

```python
            initial_changes = md_diff.diff_manifests(None, initial)
            self.assertEqual(len(initial_changes["added"]), 50)
            self.assertEqual(len(batching.split_batches(initial_changes["added"])), 5)
```

更新处的断言改为：

```python
            update_changes = md_diff.diff_manifests(initial, updated)
            self.assertEqual(len(update_changes["modified"]), 5)
            self.assertEqual(len(update_changes["added"]), 0)
            self.assertEqual(len(update_changes["deleted"]), 0)
            update_batches = batching.split_batches(update_changes["modified"])
            self.assertEqual(len(update_batches), 1)
            self.assertEqual(len(update_batches[0]), 5)
```

- [ ] **Step 6: 运行全部测试**

Run: `pytest tests/ -v`

Expected: 全部 passed。50 篇初始加载切 5 批、改 5 篇切 1 批的行为得以保留。

- [ ] **Step 7: 删除已迁移脚本并提交**

```bash
git rm scripts/22_incremental_ingest.py scripts/22_incremental_ingest.sh
git add kbp/ingestion/__init__.py kbp/ingestion/batching.py \
  tests/unit/test_batching.py tests/test_data_preparation.py
git commit -m "Move batch planning into the kbp package

Bind the metadata sidecar explicitly through S3_LOCATION so directly ingested
documents carry their governance and filter attributes."
```

---

## Task 5: 四道门禁的纯函数判定

**Files:**
- Create: `kbp/ingestion/gates.py`
- Create: `tests/unit/test_gates.py`

本任务是整个 Sprint 的正确性核心。四道门禁关闭 HANDOFF 第 5 节的多项缺陷，其中删除比例
分母与终态判定两处最容易写错。

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_gates.py`：

```python
import pytest

from kbp.ingestion import gates


class TestDeletionRatio:
    """The denominator must be the pre-release document count.

    Using the post-release count makes a full deletion compute as 0%, which
    disables the guard entirely — this was a real defect in the previous
    implementation.
    """

    def test_full_deletion_is_reported_as_one_hundred_percent(self):
        result = gates.evaluate_deletion_ratio(
            deleted_count=50, previous_document_count=50, threshold=0.5
        )

        assert result["ratio"] == 1.0
        assert result["passed"] is False

    def test_ratio_below_threshold_passes(self):
        result = gates.evaluate_deletion_ratio(
            deleted_count=4, previous_document_count=50, threshold=0.5
        )

        assert result["ratio"] == pytest.approx(0.08)
        assert result["passed"] is True

    def test_ratio_exactly_at_threshold_passes(self):
        result = gates.evaluate_deletion_ratio(
            deleted_count=25, previous_document_count=50, threshold=0.5
        )

        assert result["ratio"] == 0.5
        assert result["passed"] is True

    def test_initial_release_has_no_denominator_and_passes(self):
        result = gates.evaluate_deletion_ratio(
            deleted_count=0, previous_document_count=0, threshold=0.5
        )

        assert result["ratio"] == 0.0
        assert result["passed"] is True

    def test_deleting_from_empty_corpus_is_inconsistent(self):
        with pytest.raises(ValueError, match="cannot delete"):
            gates.evaluate_deletion_ratio(
                deleted_count=3, previous_document_count=0, threshold=0.5
            )

    def test_override_allows_bulk_deletion(self):
        result = gates.evaluate_deletion_ratio(
            deleted_count=50,
            previous_document_count=50,
            threshold=0.5,
            allow_bulk_deletion=True,
        )

        assert result["ratio"] == 1.0
        assert result["passed"] is True
        assert result["overridden"] is True


class TestIngestTerminalStatus:
    """Only INDEXED signals full success.

    PARTIALLY_INDEXED means some chunks failed: the content is incomplete but
    the API reports no error. Treating it as success is silent data corruption.
    """

    @pytest.mark.parametrize(
        "status",
        [
            "PARTIALLY_INDEXED",
            "METADATA_PARTIALLY_INDEXED",
            "METADATA_UPDATE_FAILED",
            "FAILED",
            "IGNORED",
            "NOT_FOUND",
        ],
    )
    def test_non_indexed_terminal_states_are_failures(self, status):
        result = gates.evaluate_ingest_statuses(
            [{"identifier": "doc-1", "status": status}]
        )

        assert result["settled"] is True
        assert result["passed"] is False
        assert result["failures"] == [{"identifier": "doc-1", "status": status}]

    def test_all_indexed_passes(self):
        result = gates.evaluate_ingest_statuses(
            [
                {"identifier": "doc-1", "status": "INDEXED"},
                {"identifier": "doc-2", "status": "INDEXED"},
            ]
        )

        assert result["settled"] is True
        assert result["passed"] is True
        assert result["failures"] == []

    @pytest.mark.parametrize(
        "status", ["PENDING", "STARTING", "IN_PROGRESS", "DELETING", "DELETE_IN_PROGRESS"]
    )
    def test_in_flight_states_are_not_settled(self, status):
        result = gates.evaluate_ingest_statuses(
            [
                {"identifier": "doc-1", "status": "INDEXED"},
                {"identifier": "doc-2", "status": status},
            ]
        )

        assert result["settled"] is False
        assert result["passed"] is False
        assert result["pending"] == ["doc-2"]

    def test_unknown_status_is_treated_as_failure_not_as_pending(self):
        """An unrecognized status must not make the poller spin forever."""
        result = gates.evaluate_ingest_statuses(
            [{"identifier": "doc-1", "status": "SOMETHING_NEW"}]
        )

        assert result["settled"] is True
        assert result["passed"] is False

    def test_empty_status_list_is_settled_and_passing(self):
        result = gates.evaluate_ingest_statuses([])

        assert result["settled"] is True
        assert result["passed"] is True


class TestDeleteTerminalStatus:
    """Deletion is confirmed only by NOT_FOUND, not by the 202 response."""

    def test_not_found_confirms_deletion(self):
        result = gates.evaluate_delete_statuses(
            [{"identifier": "doc-1", "status": "NOT_FOUND"}]
        )

        assert result["settled"] is True
        assert result["passed"] is True

    def test_still_indexed_document_is_a_failure(self):
        result = gates.evaluate_delete_statuses(
            [{"identifier": "doc-1", "status": "INDEXED"}]
        )

        assert result["settled"] is True
        assert result["passed"] is False

    @pytest.mark.parametrize("status", ["DELETING", "DELETE_IN_PROGRESS"])
    def test_deletion_in_flight_is_not_settled(self, status):
        result = gates.evaluate_delete_statuses(
            [{"identifier": "doc-1", "status": status}]
        )

        assert result["settled"] is False
        assert result["passed"] is False


class TestS3Consistency:
    def test_missing_sidecar_fails(self):
        result = gates.evaluate_s3_consistency(
            expected_upserts=[
                {"file": "doc.md", "contentSha256": "a" * 64, "metadataSha256": "b" * 64}
            ],
            observed_objects={"doc.md": "a" * 64},
            expected_deletions=[],
            surviving_deletions=[],
        )

        assert result["passed"] is False
        assert "doc.md.metadata.json" in result["missing"]

    def test_content_hash_mismatch_fails(self):
        result = gates.evaluate_s3_consistency(
            expected_upserts=[
                {"file": "doc.md", "contentSha256": "a" * 64, "metadataSha256": "b" * 64}
            ],
            observed_objects={"doc.md": "z" * 64, "doc.md.metadata.json": "b" * 64},
            expected_deletions=[],
            surviving_deletions=[],
        )

        assert result["passed"] is False
        assert result["mismatched"] == ["doc.md"]

    def test_surviving_deleted_object_fails(self):
        """A failed S3 deletion must block promotion, not be ignored."""
        result = gates.evaluate_s3_consistency(
            expected_upserts=[],
            observed_objects={},
            expected_deletions=[{"file": "gone.md"}],
            surviving_deletions=["gone.md"],
        )

        assert result["passed"] is False
        assert result["surviving"] == ["gone.md"]

    def test_fully_consistent_state_passes(self):
        result = gates.evaluate_s3_consistency(
            expected_upserts=[
                {"file": "doc.md", "contentSha256": "a" * 64, "metadataSha256": "b" * 64}
            ],
            observed_objects={"doc.md": "a" * 64, "doc.md.metadata.json": "b" * 64},
            expected_deletions=[{"file": "gone.md"}],
            surviving_deletions=[],
        )

        assert result["passed"] is True


class TestSmokeRetrieval:
    def test_upsert_smoke_requires_a_hit(self):
        result = gates.evaluate_smoke_retrieval(
            expectation="present", retrieved_document_ids=["doc-1"], target="doc-1"
        )

        assert result["passed"] is True

    def test_missing_upsert_hit_fails(self):
        result = gates.evaluate_smoke_retrieval(
            expectation="present", retrieved_document_ids=[], target="doc-1"
        )

        assert result["passed"] is False

    def test_deleted_document_must_not_be_retrievable(self):
        """A delete-only release verifies absence instead of presence."""
        result = gates.evaluate_smoke_retrieval(
            expectation="absent", retrieved_document_ids=[], target="gone"
        )

        assert result["passed"] is True

    def test_deleted_document_still_retrievable_fails(self):
        result = gates.evaluate_smoke_retrieval(
            expectation="absent", retrieved_document_ids=["gone"], target="gone"
        )

        assert result["passed"] is False


class TestChangeSetEmptiness:
    def test_empty_change_set_is_detected(self):
        assert gates.is_empty_change_set(
            {"added": [], "modified": [], "deleted": []}
        ) is True

    def test_any_change_makes_it_non_empty(self):
        assert gates.is_empty_change_set(
            {"added": [], "modified": [{"file": "a.md"}], "deleted": []}
        ) is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/unit/test_gates.py -v`

Expected: FAIL —— `ImportError: cannot import name 'gates' from 'kbp.ingestion'`。

- [ ] **Step 3: 实现 gates.py**

创建 `kbp/ingestion/gates.py`：

```python
"""Pure decision logic for the four release gates.

These functions take plain data and return plain data so they can be tested
without AWS. The Lambda handlers in kbp/ingestion/handlers are thin adapters
that fetch state and delegate here.
"""

INGEST_SUCCESS_STATUS = "INDEXED"
DELETE_SUCCESS_STATUS = "NOT_FOUND"

IN_FLIGHT_STATUSES = frozenset(
    {"PENDING", "STARTING", "IN_PROGRESS", "DELETING", "DELETE_IN_PROGRESS"}
)


def evaluate_deletion_ratio(
    *,
    deleted_count: int,
    previous_document_count: int,
    threshold: float,
    allow_bulk_deletion: bool = False,
) -> dict:
    """Evaluate the deletion guard against the pre-release document count.

    The denominator is deliberately the count before this release. Using the
    post-release count would make a full deletion compute as zero.
    """
    if previous_document_count == 0:
        if deleted_count:
            raise ValueError(
                f"cannot delete {deleted_count} documents from an empty corpus"
            )
        return {"ratio": 0.0, "passed": True, "overridden": False, "threshold": threshold}

    ratio = deleted_count / previous_document_count
    within_threshold = ratio <= threshold
    return {
        "ratio": ratio,
        "passed": within_threshold or allow_bulk_deletion,
        "overridden": bool(not within_threshold and allow_bulk_deletion),
        "threshold": threshold,
    }


def _partition_statuses(details: list[dict], success_status: str) -> dict:
    pending = [
        item["identifier"]
        for item in details
        if item["status"] in IN_FLIGHT_STATUSES
    ]
    failures = [
        {"identifier": item["identifier"], "status": item["status"]}
        for item in details
        if item["status"] not in IN_FLIGHT_STATUSES
        and item["status"] != success_status
    ]
    settled = not pending
    return {
        "settled": settled,
        "passed": settled and not failures,
        "pending": pending,
        "failures": failures,
    }


def evaluate_ingest_statuses(details: list[dict]) -> dict:
    """Aggregate document statuses for upserted documents.

    Only INDEXED counts as success. PARTIALLY_INDEXED and the METADATA_* failure
    variants are terminal-but-incomplete: the API reports no error while the
    indexed content is incomplete. An unrecognized status is treated as a
    failure rather than as in-flight, so the poller cannot spin forever.
    """
    return _partition_statuses(details, INGEST_SUCCESS_STATUS)


def evaluate_delete_statuses(details: list[dict]) -> dict:
    """Aggregate document statuses for deleted documents.

    Deletion is confirmed only when the document reports NOT_FOUND; the 202
    response from the delete call proves nothing about the index.
    """
    return _partition_statuses(details, DELETE_SUCCESS_STATUS)


def evaluate_s3_consistency(
    *,
    expected_upserts: list[dict],
    observed_objects: dict[str, str],
    expected_deletions: list[dict],
    surviving_deletions: list[str],
) -> dict:
    """Verify canonical objects match the manifest before ingestion.

    `observed_objects` maps object key suffix to its SHA-256. `surviving_deletions`
    lists files that should have been removed from S3 but are still present; a
    failed S3 deletion must block promotion rather than be ignored.
    """
    missing: list[str] = []
    mismatched: list[str] = []

    for item in expected_upserts:
        content_key = item["file"]
        sidecar_key = f"{item['file']}.metadata.json"

        if content_key not in observed_objects:
            missing.append(content_key)
        elif observed_objects[content_key] != item["contentSha256"]:
            mismatched.append(content_key)

        if sidecar_key not in observed_objects:
            missing.append(sidecar_key)
        elif observed_objects[sidecar_key] != item["metadataSha256"]:
            mismatched.append(sidecar_key)

    surviving = sorted(set(surviving_deletions))
    return {
        "passed": not (missing or mismatched or surviving),
        "missing": missing,
        "mismatched": mismatched,
        "surviving": surviving,
        "expectedDeletionCount": len(expected_deletions),
    }


def evaluate_smoke_retrieval(
    *, expectation: str, retrieved_document_ids: list[str], target: str
) -> dict:
    """Check a single smoke retrieval outcome.

    A delete-only release has no upserted document to smoke test, so it verifies
    absence instead: the removed document must no longer be retrievable.
    """
    if expectation not in ("present", "absent"):
        raise ValueError(f"unknown expectation: {expectation}")

    found = target in retrieved_document_ids
    passed = found if expectation == "present" else not found
    return {"passed": passed, "expectation": expectation, "target": target, "found": found}


def is_empty_change_set(changes: dict) -> bool:
    """Report whether a change set contains no work at all."""
    return not any(
        changes.get(key) for key in ("added", "modified", "deleted")
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/unit/test_gates.py -v`

Expected: 全部 passed（约 30 项，含参数化）。

- [ ] **Step 5: 运行全部单元测试确认无回归**

Run: `pytest tests/ -v`

Expected: 全部 passed。

- [ ] **Step 6: 提交**

```bash
git add kbp/ingestion/gates.py tests/unit/test_gates.py
git commit -m "Add pure decision logic for the four release gates

Compute the deletion ratio against the pre-release document count, and treat
every terminal status other than INDEXED as a failure so partially indexed
content cannot reach promotion."
```

---

## Task 6: Registry 存储层与原子 Promotion

**Files:**
- Create: `kbp/registry/store.py`
- Create: `tests/unit/test_registry_store.py`

DynamoDB 条件写是并发安全的唯一保障。本任务用一个假 DynamoDB 客户端测试条件表达式的
构造与冲突处理——不引入 moto，因为要断言的是"传给 API 的参数正确"而非"DynamoDB 行为
正确"。

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_registry_store.py`：

```python
import pytest

from kbp.registry import store


class FakeDynamoClient:
    """Records calls and lets a test force a conditional-check failure."""

    class exceptions:  # noqa: N801 - mirrors botocore client shape
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self, *, fail_condition: bool = False, existing_item: dict | None = None):
        self.fail_condition = fail_condition
        self.existing_item = existing_item
        self.calls: list[tuple[str, dict]] = []

    def put_item(self, **kwargs):
        self.calls.append(("put_item", kwargs))
        if self.fail_condition:
            raise self.exceptions.ConditionalCheckFailedException("conditional failed")
        return {}

    def update_item(self, **kwargs):
        self.calls.append(("update_item", kwargs))
        if self.fail_condition:
            raise self.exceptions.ConditionalCheckFailedException("conditional failed")
        return {}

    def get_item(self, **kwargs):
        self.calls.append(("get_item", kwargs))
        return {"Item": self.existing_item} if self.existing_item else {}


def test_keys_are_namespaced_to_allow_multiple_corpora_later():
    assert store.release_key("demo", "demo-20260817T101500Z-abcdef12") == {
        "pk": {"S": "CORPUS#demo"},
        "sk": {"S": "RELEASE#demo-20260817T101500Z-abcdef12"},
    }
    assert store.pointer_key("demo") == {
        "pk": {"S": "CORPUS#demo"},
        "sk": {"S": "POINTER"},
    }


def test_create_release_refuses_to_overwrite_an_existing_release_id():
    client = FakeDynamoClient()

    store.create_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        manifest_s3_uri="s3://registry/manifests/demo/r1.json",
        manifest_s3_version_id="v1",
        parent_release_id=None,
        execution_arn="arn:aws:states:us-east-1:1:execution:sm:exec",
    )

    _, kwargs = client.calls[0]
    assert kwargs["ConditionExpression"] == "attribute_not_exists(pk)"
    assert kwargs["Item"]["status"] == {"S": "PREPARING"}


def test_read_active_pointer_returns_none_for_first_release():
    client = FakeDynamoClient(existing_item=None)

    assert store.read_active_release_id(client, table_name="releases", corpus_id="demo") is None


def test_read_active_pointer_returns_current_release():
    client = FakeDynamoClient(
        existing_item={
            "pk": {"S": "CORPUS#demo"},
            "sk": {"S": "POINTER"},
            "activeReleaseId": {"S": "demo-20260810T101500Z-99999999"},
        }
    )

    assert (
        store.read_active_release_id(client, table_name="releases", corpus_id="demo")
        == "demo-20260810T101500Z-99999999"
    )


def test_promote_first_release_requires_absent_pointer():
    client = FakeDynamoClient()

    store.promote_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        expected_previous_release_id=None,
    )

    update_calls = [kwargs for name, kwargs in client.calls if name == "update_item"]
    pointer_call = next(
        kwargs for kwargs in update_calls if kwargs["Key"] == store.pointer_key("demo")
    )
    assert pointer_call["ConditionExpression"] == "attribute_not_exists(activeReleaseId)"


def test_promote_subsequent_release_pins_the_expected_previous_pointer():
    client = FakeDynamoClient()

    store.promote_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        expected_previous_release_id="demo-20260810T101500Z-99999999",
    )

    pointer_call = next(
        kwargs
        for name, kwargs in client.calls
        if name == "update_item" and kwargs["Key"] == store.pointer_key("demo")
    )
    assert (
        pointer_call["ConditionExpression"]
        == "attribute_not_exists(activeReleaseId) OR activeReleaseId = :expected"
    )
    assert pointer_call["ExpressionAttributeValues"][":expected"] == {
        "S": "demo-20260810T101500Z-99999999"
    }


def test_concurrent_promotion_is_rejected_rather_than_silently_overwriting():
    client = FakeDynamoClient(fail_condition=True)

    with pytest.raises(store.ConcurrentPromotionError) as error:
        store.promote_release(
            client,
            table_name="releases",
            corpus_id="demo",
            release_id="demo-20260817T101500Z-abcdef12",
            expected_previous_release_id="demo-20260810T101500Z-99999999",
        )

    assert "demo-20260810T101500Z-99999999" in str(error.value)


def test_promotion_supersedes_the_previous_release_record():
    client = FakeDynamoClient()

    store.promote_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        expected_previous_release_id="demo-20260810T101500Z-99999999",
    )

    superseded = next(
        kwargs
        for name, kwargs in client.calls
        if name == "update_item"
        and kwargs["Key"]
        == store.release_key("demo", "demo-20260810T101500Z-99999999")
    )
    assert superseded["ExpressionAttributeValues"][":status"] == {"S": "SUPERSEDED"}


def test_pointer_is_updated_after_the_release_record_is_marked_active():
    """Ordering matters: a reader following the pointer must find an ACTIVE record."""
    client = FakeDynamoClient()

    store.promote_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        expected_previous_release_id=None,
    )

    keys_in_order = [kwargs["Key"] for _, kwargs in client.calls]
    active_index = keys_in_order.index(
        store.release_key("demo", "demo-20260817T101500Z-abcdef12")
    )
    pointer_index = keys_in_order.index(store.pointer_key("demo"))
    assert active_index < pointer_index


def test_fail_release_never_touches_the_pointer():
    client = FakeDynamoClient()

    store.fail_release(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        reason="gate A failed: sidecar missing",
    )

    touched_keys = [kwargs["Key"] for _, kwargs in client.calls]
    assert store.pointer_key("demo") not in touched_keys
    assert client.calls[0][1]["ExpressionAttributeValues"][":status"] == {"S": "FAILED"}


def test_advance_status_records_the_new_state():
    client = FakeDynamoClient()

    store.advance_status(
        client,
        table_name="releases",
        corpus_id="demo",
        release_id="demo-20260817T101500Z-abcdef12",
        status="INGESTING",
    )

    _, kwargs = client.calls[0]
    assert kwargs["ExpressionAttributeValues"][":status"] == {"S": "INGESTING"}


def test_unknown_status_is_rejected():
    client = FakeDynamoClient()

    with pytest.raises(ValueError, match="unknown release status"):
        store.advance_status(
            client,
            table_name="releases",
            corpus_id="demo",
            release_id="demo-20260817T101500Z-abcdef12",
            status="ALMOST_DONE",
        )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/unit/test_registry_store.py -v`

Expected: FAIL —— `ImportError: cannot import name 'store' from 'kbp.registry'`。

- [ ] **Step 3: 实现 store.py**

创建 `kbp/registry/store.py`：

```python
"""Release registry persistence: DynamoDB state and pointer, S3 manifests.

DynamoDB owns release status and the active pointer. S3 owns manifest content.
This split means a deleted table still leaves every published manifest
recoverable from the versioned bucket.
"""

VALID_STATUSES = frozenset(
    {"PREPARING", "INGESTING", "TESTING", "ACTIVE", "SUPERSEDED", "FAILED"}
)


class ConcurrentPromotionError(RuntimeError):
    """Raised when the active pointer moved while this release was in flight."""


def release_key(corpus_id: str, release_id: str) -> dict:
    return {"pk": {"S": f"CORPUS#{corpus_id}"}, "sk": {"S": f"RELEASE#{release_id}"}}


def pointer_key(corpus_id: str) -> dict:
    return {"pk": {"S": f"CORPUS#{corpus_id}"}, "sk": {"S": "POINTER"}}


def create_release(
    client,
    *,
    table_name: str,
    corpus_id: str,
    release_id: str,
    manifest_s3_uri: str,
    manifest_s3_version_id: str,
    parent_release_id: str | None,
    execution_arn: str,
) -> None:
    """Create the release record, refusing to overwrite an existing releaseId."""
    item = {
        **release_key(corpus_id, release_id),
        "corpusId": {"S": corpus_id},
        "releaseId": {"S": release_id},
        "status": {"S": "PREPARING"},
        "manifestS3Uri": {"S": manifest_s3_uri},
        "manifestS3VersionId": {"S": manifest_s3_version_id},
        "executionArn": {"S": execution_arn},
        "parentReleaseId": (
            {"S": parent_release_id} if parent_release_id else {"NULL": True}
        ),
    }
    client.put_item(
        TableName=table_name,
        Item=item,
        ConditionExpression="attribute_not_exists(pk)",
    )


def read_active_release_id(client, *, table_name: str, corpus_id: str) -> str | None:
    """Read the currently active releaseId, or None before the first release."""
    response = client.get_item(
        TableName=table_name, Key=pointer_key(corpus_id), ConsistentRead=True
    )
    item = response.get("Item")
    if not item:
        return None
    return item["activeReleaseId"]["S"]


def advance_status(
    client, *, table_name: str, corpus_id: str, release_id: str, status: str
) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown release status: {status}")
    client.update_item(
        TableName=table_name,
        Key=release_key(corpus_id, release_id),
        UpdateExpression="SET #status = :status",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": {"S": status}},
    )


def fail_release(
    client, *, table_name: str, corpus_id: str, release_id: str, reason: str
) -> None:
    """Mark a release FAILED. Deliberately never touches the pointer."""
    client.update_item(
        TableName=table_name,
        Key=release_key(corpus_id, release_id),
        UpdateExpression="SET #status = :status, failureReason = :reason",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": {"S": "FAILED"},
            ":reason": {"S": reason},
        },
    )


def promote_release(
    client,
    *,
    table_name: str,
    corpus_id: str,
    release_id: str,
    expected_previous_release_id: str | None,
) -> None:
    """Atomically make this release active.

    The release record is marked ACTIVE before the pointer moves, so a reader
    that follows the pointer always finds an ACTIVE record. The conditional
    write on the pointer rejects a concurrent pipeline instead of overwriting it.
    """
    advance_status(
        client,
        table_name=table_name,
        corpus_id=corpus_id,
        release_id=release_id,
        status="ACTIVE",
    )

    if expected_previous_release_id:
        advance_status(
            client,
            table_name=table_name,
            corpus_id=corpus_id,
            release_id=expected_previous_release_id,
            status="SUPERSEDED",
        )
        condition = (
            "attribute_not_exists(activeReleaseId) OR activeReleaseId = :expected"
        )
        values = {
            ":active": {"S": release_id},
            ":expected": {"S": expected_previous_release_id},
        }
    else:
        condition = "attribute_not_exists(activeReleaseId)"
        values = {":active": {"S": release_id}}

    try:
        client.update_item(
            TableName=table_name,
            Key=pointer_key(corpus_id),
            UpdateExpression="SET activeReleaseId = :active",
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
        )
    except client.exceptions.ConditionalCheckFailedException as error:
        raise ConcurrentPromotionError(
            f"active pointer for {corpus_id} is no longer "
            f"{expected_previous_release_id}; another release won the race"
        ) from error
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/unit/test_registry_store.py -v`

Expected: 12 passed。

- [ ] **Step 5: 提交**

```bash
git add kbp/registry/store.py tests/unit/test_registry_store.py
git commit -m "Add release registry persistence with atomic promotion

Pin the pointer update to the release the execution observed at start, so a
concurrent pipeline is rejected rather than silently overwritten, and mark the
release ACTIVE before moving the pointer so pointer followers never land on a
non-active record."
```

---

## Task 7: Lambda handlers

**Files:**
- Create: `kbp/ingestion/handlers/__init__.py`
- Create: `kbp/ingestion/handlers/verify_s3.py`
- Create: `kbp/ingestion/handlers/check_gates.py`
- Create: `kbp/ingestion/handlers/registry_ops.py`
- Create: `tests/unit/test_handlers.py`

handler 必须保持极薄：取事件、调 AWS、交纯函数、返回结果。任何判定逻辑出现在 handler
里就是设计错误——它会变成不可测的部分。

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_handlers.py`：

```python
import pytest

from kbp.ingestion.handlers import check_gates, registry_ops, verify_s3


class FakeS3Client:
    def __init__(self, objects: dict[str, str]):
        self.objects = objects

    def head_object(self, *, Bucket, Key):  # noqa: N803 - boto3 casing
        suffix = Key.split("canonical/demo/", 1)[-1]
        if suffix not in self.objects:
            raise self.exceptions.ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"Metadata": {"sha256": self.objects[suffix]}}

    class exceptions:  # noqa: N801
        class ClientError(Exception):
            def __init__(self, response, operation):
                super().__init__(operation)
                self.response = response


def test_verify_s3_handler_reports_missing_sidecar():
    client = FakeS3Client({"doc.md": "a" * 64})

    result = verify_s3.evaluate(
        client=client,
        bucket="canonical",
        prefix="canonical/demo",
        upserts=[
            {"file": "doc.md", "contentSha256": "a" * 64, "metadataSha256": "b" * 64}
        ],
        deletions=[],
    )

    assert result["passed"] is False
    assert "doc.md.metadata.json" in result["missing"]


def test_verify_s3_handler_flags_surviving_deletion():
    client = FakeS3Client({"gone.md": "a" * 64})

    result = verify_s3.evaluate(
        client=client,
        bucket="canonical",
        prefix="canonical/demo",
        upserts=[],
        deletions=[{"file": "gone.md"}],
    )

    assert result["passed"] is False
    assert result["surviving"] == ["gone.md"]


def test_verify_s3_handler_passes_on_consistent_state():
    client = FakeS3Client({"doc.md": "a" * 64, "doc.md.metadata.json": "b" * 64})

    result = verify_s3.evaluate(
        client=client,
        bucket="canonical",
        prefix="canonical/demo",
        upserts=[
            {"file": "doc.md", "contentSha256": "a" * 64, "metadataSha256": "b" * 64}
        ],
        deletions=[],
    )

    assert result["passed"] is True


def test_check_gates_deletion_ratio_uses_previous_count_from_event():
    event = {
        "gate": "deletionRatio",
        "deletedCount": 30,
        "previousDocumentCount": 50,
        "threshold": 0.5,
        "allowBulkDeletion": False,
    }

    result = check_gates.handler(event, None)

    assert result["passed"] is False
    assert result["ratio"] == pytest.approx(0.6)


def test_check_gates_aggregates_ingest_statuses():
    event = {
        "gate": "ingestStatus",
        "documentDetails": [
            {"identifier": {"s3": {"uri": "s3://b/doc-1.md"}}, "status": "INDEXED"},
            {
                "identifier": {"s3": {"uri": "s3://b/doc-2.md"}},
                "status": "PARTIALLY_INDEXED",
            },
        ],
    }

    result = check_gates.handler(event, None)

    assert result["settled"] is True
    assert result["passed"] is False
    assert result["failures"][0]["status"] == "PARTIALLY_INDEXED"


def test_check_gates_normalizes_s3_identifier_to_uri_string():
    """The SDK returns a nested identifier; gates work on plain strings."""
    event = {
        "gate": "ingestStatus",
        "documentDetails": [
            {"identifier": {"s3": {"uri": "s3://b/doc-1.md"}}, "status": "FAILED"}
        ],
    }

    result = check_gates.handler(event, None)

    assert result["failures"][0]["identifier"] == "s3://b/doc-1.md"


def test_check_gates_rejects_unknown_gate_name():
    with pytest.raises(ValueError, match="unknown gate"):
        check_gates.handler({"gate": "vibes"}, None)


def test_registry_ops_rejects_unknown_action():
    with pytest.raises(ValueError, match="unknown action"):
        registry_ops.handler({"action": "improvise"}, None)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/unit/test_handlers.py -v`

Expected: FAIL —— `ModuleNotFoundError: No module named 'kbp.ingestion.handlers'`。

- [ ] **Step 3: 实现三个 handler**

创建 `kbp/ingestion/handlers/__init__.py`（空文件）。

创建 `kbp/ingestion/handlers/verify_s3.py`：

```python
"""Gate A: verify canonical objects match the manifest before ingestion."""

import os

import boto3

from kbp.ingestion import gates


def _object_sha256(client, *, bucket: str, prefix: str, file: str) -> str | None:
    """Return the recorded SHA-256 for an object, or None when it is absent."""
    try:
        response = client.head_object(Bucket=bucket, Key=f"{prefix.strip('/')}/{file}")
    except client.exceptions.ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return None
        raise
    return response["Metadata"]["sha256"]


def evaluate(*, client, bucket: str, prefix: str, upserts: list, deletions: list) -> dict:
    observed = {}
    for item in upserts:
        for file in (item["file"], f"{item['file']}.metadata.json"):
            digest = _object_sha256(client, bucket=bucket, prefix=prefix, file=file)
            if digest is not None:
                observed[file] = digest

    surviving = [
        item["file"]
        for item in deletions
        if _object_sha256(client, bucket=bucket, prefix=prefix, file=item["file"])
        is not None
    ]

    return gates.evaluate_s3_consistency(
        expected_upserts=upserts,
        observed_objects=observed,
        expected_deletions=deletions,
        surviving_deletions=surviving,
    )


def handler(event, _context):
    return evaluate(
        client=boto3.client("s3"),
        bucket=os.environ["CANONICAL_BUCKET"],
        prefix=event["prefix"],
        upserts=event["upserts"],
        deletions=event["deletions"],
    )
```

创建 `kbp/ingestion/handlers/check_gates.py`：

```python
"""Gates B, C and D: evaluate release gates from state machine input."""

from kbp.ingestion import gates


def _identifier_to_string(identifier: dict | str) -> str:
    """Flatten the SDK's nested document identifier into a plain string."""
    if isinstance(identifier, str):
        return identifier
    if "s3" in identifier:
        return identifier["s3"]["uri"]
    return identifier["custom"]["id"]


def _normalize_details(details: list[dict]) -> list[dict]:
    return [
        {
            "identifier": _identifier_to_string(item["identifier"]),
            "status": item["status"],
        }
        for item in details
    ]


def handler(event, _context):
    gate = event.get("gate")

    if gate == "deletionRatio":
        return gates.evaluate_deletion_ratio(
            deleted_count=event["deletedCount"],
            previous_document_count=event["previousDocumentCount"],
            threshold=event["threshold"],
            allow_bulk_deletion=event.get("allowBulkDeletion", False),
        )

    if gate == "ingestStatus":
        return gates.evaluate_ingest_statuses(
            _normalize_details(event["documentDetails"])
        )

    if gate == "deleteStatus":
        return gates.evaluate_delete_statuses(
            _normalize_details(event["documentDetails"])
        )

    if gate == "smokeRetrieval":
        return gates.evaluate_smoke_retrieval(
            expectation=event["expectation"],
            retrieved_document_ids=event["retrievedDocumentIds"],
            target=event["target"],
        )

    raise ValueError(f"unknown gate: {gate}")
```

创建 `kbp/ingestion/handlers/registry_ops.py`：

```python
"""Registry reads, status transitions and atomic promotion."""

import os

import boto3

from kbp.registry import store


def handler(event, _context):
    client = boto3.client("dynamodb")
    table_name = os.environ["RELEASE_TABLE"]
    action = event.get("action")
    corpus_id = event.get("corpusId")

    if action == "readPointer":
        return {
            "activeReleaseId": store.read_active_release_id(
                client, table_name=table_name, corpus_id=corpus_id
            )
        }

    if action == "createRelease":
        store.create_release(
            client,
            table_name=table_name,
            corpus_id=corpus_id,
            release_id=event["releaseId"],
            manifest_s3_uri=event["manifestS3Uri"],
            manifest_s3_version_id=event["manifestS3VersionId"],
            parent_release_id=event.get("parentReleaseId"),
            execution_arn=event["executionArn"],
        )
        return {"status": "PREPARING"}

    if action == "advanceStatus":
        store.advance_status(
            client,
            table_name=table_name,
            corpus_id=corpus_id,
            release_id=event["releaseId"],
            status=event["status"],
        )
        return {"status": event["status"]}

    if action == "promote":
        store.promote_release(
            client,
            table_name=table_name,
            corpus_id=corpus_id,
            release_id=event["releaseId"],
            expected_previous_release_id=event.get("expectedPreviousReleaseId"),
        )
        return {"status": "ACTIVE"}

    if action == "fail":
        store.fail_release(
            client,
            table_name=table_name,
            corpus_id=corpus_id,
            release_id=event["releaseId"],
            reason=event["reason"],
        )
        return {"status": "FAILED"}

    raise ValueError(f"unknown action: {action}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/unit/test_handlers.py -v`

Expected: 8 passed。

- [ ] **Step 5: 提交**

```bash
git add kbp/ingestion/handlers tests/unit/test_handlers.py
git commit -m "Add Lambda handlers as thin adapters over the gate functions

Keep every decision in the pure functions so handler code stays limited to
fetching state and shaping payloads, and flatten the SDK's nested document
identifier at the boundary."
```

---

## Task 8: A1/A2 假设探针（调用真实 AWS）

**Files:**
- Create: `kbp/probes/__init__.py`
- Create: `kbp/probes/assumptions.py`
- Create: `tests/unit/test_probes.py`
- Create: `docs/adr/ADR-006-assumption-probe-results.md`
- Delete: `scripts/23_verify_assumptions.sh`

**本任务调用真实 AWS 并产生费用。** 需要一个已存在的 Managed KB。若 CDK Stack 尚未部署，
可先跳到 Task 9–11 部署基础设施，再回到本任务——但探针必须在 Task 13 状态机接线**之前**
完成，因为结论会写入 ADR 并影响后续对账通道设计。

现有 `scripts/23_verify_assumptions.sh` 有三处错误使其结果不可用：A1 对同一个 Data
Source 连续提交 Job（混淆并发限制与速率限制）；A2 用 `CUSTOM` payload 测试 S3 型 Data
Source；查询用了 `vectorSearchConfiguration`（Managed KB 需要 `managedSearchConfiguration`，
错误被吞成零命中）。

- [ ] **Step 1: 写失败测试**

探针的可测部分是"结果如何判定假设成立"，这部分是纯函数。创建 `tests/unit/test_probes.py`：

```python
import pytest

from kbp.probes import assumptions


def test_a1_is_refuted_when_rapid_submissions_all_succeed():
    result = assumptions.interpret_a1(
        [
            {"intervalSeconds": 0.5, "throttled": False},
            {"intervalSeconds": 0.5, "throttled": False},
            {"intervalSeconds": 0.5, "throttled": False},
        ]
    )

    assert result["holds"] is False
    assert "no throttling" in result["conclusion"]


def test_a1_holds_when_submissions_are_throttled():
    result = assumptions.interpret_a1(
        [
            {"intervalSeconds": 0.5, "throttled": False},
            {"intervalSeconds": 0.5, "throttled": True},
            {"intervalSeconds": 0.5, "throttled": True},
        ]
    )

    assert result["holds"] is True
    assert result["throttledCount"] == 2


def test_a1_requires_distinct_data_sources_to_be_valid():
    """Submitting to one data source measures concurrency, not rate."""
    with pytest.raises(ValueError, match="distinct data sources"):
        assumptions.interpret_a1(
            [
                {"intervalSeconds": 0.5, "throttled": False, "dataSourceId": "ds-1"},
                {"intervalSeconds": 0.5, "throttled": False, "dataSourceId": "ds-1"},
            ],
            require_distinct_data_sources=True,
        )


def test_a2_holds_when_probe_document_disappears_after_sync():
    result = assumptions.interpret_a2(
        retrievable_before_sync=True, retrievable_after_sync=False
    )

    assert result["holds"] is True
    assert "removes" in result["conclusion"]


def test_a2_is_refuted_when_probe_document_survives_sync():
    result = assumptions.interpret_a2(
        retrievable_before_sync=True, retrievable_after_sync=True
    )

    assert result["holds"] is False


def test_a2_is_inconclusive_when_probe_never_became_retrievable():
    """Without a retrievable baseline the post-sync observation proves nothing."""
    result = assumptions.interpret_a2(
        retrievable_before_sync=False, retrievable_after_sync=False
    )

    assert result["holds"] is None
    assert "inconclusive" in result["conclusion"]


def test_managed_search_configuration_is_used_for_retrieval():
    """Managed KB requires managedSearchConfiguration; the vector variant
    silently returns zero hits and was a defect in the previous probe."""
    request = assumptions.build_probe_retrieve_request(
        knowledge_base_id="KB123", query="probe", document_id="probe-doc"
    )

    assert "managedSearchConfiguration" in request["retrievalConfiguration"]
    assert "vectorSearchConfiguration" not in request["retrievalConfiguration"]
    filter_clause = request["retrievalConfiguration"]["managedSearchConfiguration"][
        "filter"
    ]
    assert filter_clause == {
        "equals": {"key": "document_id", "value": "probe-doc"}
    }


def test_probe_ingest_payload_uses_s3_not_custom_for_s3_data_sources():
    """The previous probe sent a CUSTOM payload to an S3 data source."""
    payload = assumptions.build_probe_ingest_payload(
        bucket="bucket", key="outside-prefix/probe.md"
    )

    assert payload["content"]["dataSourceType"] == "S3"
    assert "custom" not in payload["content"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/unit/test_probes.py -v`

Expected: FAIL —— `ModuleNotFoundError: No module named 'kbp.probes'`。

- [ ] **Step 3: 实现 assumptions.py**

创建 `kbp/probes/__init__.py`（空文件）。

创建 `kbp/probes/assumptions.py`：

```python
"""Probes for the two unverified assumptions that shape ingestion design.

A1: does StartIngestionJob enforce 0.1 rps on managed knowledge bases?
A2: does a reconciliation sync remove documents that exist only in the index?

The interpret_* functions are pure so the decision rules can be tested without
AWS. The run_* functions perform the measurements.
"""

import time


def build_probe_retrieve_request(
    *, knowledge_base_id: str, query: str, document_id: str
) -> dict:
    """Build a Retrieve request scoped to one probe document.

    Managed knowledge bases require managedSearchConfiguration. Using
    vectorSearchConfiguration produces a silent zero-hit result rather than an
    error, which previously masked a broken probe.
    """
    return {
        "knowledgeBaseId": knowledge_base_id,
        "retrievalQuery": {"text": query},
        "retrievalConfiguration": {
            "managedSearchConfiguration": {
                "numberOfResults": 10,
                "filter": {"equals": {"key": "document_id", "value": document_id}},
            }
        },
    }


def build_probe_ingest_payload(*, bucket: str, key: str) -> dict:
    """Build an ingest entry for an S3-type data source.

    S3 data sources accept only S3 locations; inline CUSTOM content is rejected.
    """
    return {
        "content": {
            "dataSourceType": "S3",
            "s3": {"s3Location": {"uri": f"s3://{bucket}/{key}"}},
        }
    }


def interpret_a1(
    submissions: list[dict], *, require_distinct_data_sources: bool = False
) -> dict:
    """Decide whether A1 holds from a series of job submissions.

    Each submission must target a distinct data source; submitting repeatedly to
    one data source measures the per-data-source concurrency limit rather than
    the API rate limit, which is the flaw in the previous probe.
    """
    if require_distinct_data_sources:
        data_source_ids = [
            item["dataSourceId"] for item in submissions if "dataSourceId" in item
        ]
        if len(set(data_source_ids)) != len(data_source_ids):
            raise ValueError(
                "A1 requires distinct data sources per submission to isolate "
                "the rate limit from the concurrency limit"
            )

    throttled_count = sum(1 for item in submissions if item["throttled"])
    if throttled_count:
        return {
            "holds": True,
            "throttledCount": throttled_count,
            "conclusion": (
                f"{throttled_count} of {len(submissions)} submissions were throttled; "
                "a reconciliation channel must keep a rate limiter"
            ),
        }
    return {
        "holds": False,
        "throttledCount": 0,
        "conclusion": (
            f"no throttling across {len(submissions)} rapid submissions; "
            "the serial gate from the classic-KB reference architecture is "
            "unnecessary here"
        ),
    }


def interpret_a2(*, retrievable_before_sync: bool, retrievable_after_sync: bool) -> dict:
    """Decide whether A2 holds from probe retrievability before and after sync."""
    if not retrievable_before_sync:
        return {
            "holds": None,
            "conclusion": (
                "inconclusive: the probe document never became retrievable, so the "
                "post-sync observation carries no information"
            ),
        }
    if retrievable_after_sync:
        return {
            "holds": False,
            "conclusion": (
                "the probe survived the sync; a reconciliation job does not remove "
                "documents outside the inclusion prefix"
            ),
        }
    return {
        "holds": True,
        "conclusion": (
            "the sync removes index-only documents; writing to S3 before direct "
            "ingestion is a correctness requirement"
        ),
    }


def poll_document_status(
    client,
    *,
    knowledge_base_id: str,
    data_source_id: str,
    identifier: dict,
    max_attempts: int = 30,
    interval_seconds: float = 10.0,
) -> str:
    """Poll one document until it reaches a terminal status."""
    in_flight = {"PENDING", "STARTING", "IN_PROGRESS", "DELETING", "DELETE_IN_PROGRESS"}
    for _ in range(max_attempts):
        response = client.get_knowledge_base_documents(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
            documentIdentifiers=[identifier],
        )
        status = response["documentDetails"][0]["status"]
        if status not in in_flight:
            return status
        time.sleep(interval_seconds)
    return "TIMED_OUT"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/unit/test_probes.py -v`

Expected: 8 passed。

- [ ] **Step 5: 在沙箱执行探针并记录 ADR**

此步需要真实 AWS 凭证与一个可用的 Managed KB（可来自 Task 9–11 的部署）。

A1 测量步骤：创建 3 个一次性 Data Source（各指向一个空前缀），对每个提交
`StartIngestionJob`，间隔约 0.5 秒，记录是否 `ThrottlingException`，然后删除这些 Data
Source。

A2 测量步骤：向 canonical 桶中**位于 Data Source inclusion prefix 之外**的键写入一个探针
Markdown 与 sidecar；用 `build_probe_ingest_payload` 定向摄入；轮询至终态；用
`build_probe_retrieve_request` 确认可检索（记为 before）；对该 Data Source 执行
`StartIngestionJob` 并等其 `COMPLETE`；再次检索（记为 after）。

创建 `docs/adr/ADR-006-assumption-probe-results.md`，填入实测数字：

```markdown
# ADR-006: A1/A2 假设探针结论

- 状态：已接受
- 日期：<执行日期>
- 决策者：<执行者>

## 背景

两条假设影响摄入通道设计，此前无证据。旧探针 `scripts/23_verify_assumptions.sh` 的实现
有三处错误，其结果不可用于架构决策。

## 测量方法

A1：<记录 Data Source 数量、提交间隔、SDK 版本、Region>
A2：<记录探针对象键、inclusion prefix、摄入终态、两次检索结果>

## 结论

A1：<holds 或 refuted>，依据 <throttled 数量 / 总提交数>。
A2：<holds、refuted 或 inconclusive>，依据 <before/after 可检索性>。

## 影响

- A1 不改变 Sprint 1 状态机：本次摄入与删除均走 Direct API，未调用
  `StartIngestionJob`。结论用于后续对账通道是否需要限流器。
- A2 不改变门禁 A：无论结论如何，本次都强制"先写 S3 再定向摄入"。A2 成立时这是正确性
  要求；被推翻时它仍是防止 Manifest 与 canonical 桶漂移的一致性校验。

## 复现

`.venv/bin/python -m kbp.probes.assumptions --help`
```

- [ ] **Step 6: 删除旧探针并提交**

```bash
git rm scripts/23_verify_assumptions.sh
git add kbp/probes tests/unit/test_probes.py \
  docs/adr/ADR-006-assumption-probe-results.md
git commit -m "Rewrite the assumption probes and record their results

The previous probe measured per-data-source concurrency instead of the API rate
limit, sent a CUSTOM payload to an S3 data source, and queried with
vectorSearchConfiguration, which turns a broken probe into a silent zero-hit
result."
```

---

## Task 9: CDK 项目初始化与 FoundationStack

**Files:**
- Create: `infra/package.json`
- Create: `infra/tsconfig.json`
- Create: `infra/jest.config.js`
- Create: `infra/cdk.json`
- Create: `infra/bin/app.ts`
- Create: `infra/lib/foundation-stack.ts`
- Create: `infra/test/foundation-stack.test.ts`
- Modify: `.gitignore`

版本组合已核实兼容：`ts-jest@29` 要求 `typescript <7`，而 TypeScript latest 是 7.x，
故锁定 `typescript@5.9.3`。`cdk-nag@3` 要求 `constructs ^10.5.1`。

- [ ] **Step 1: 建立 CDK 项目骨架**

创建 `infra/package.json`：

```json
{
  "name": "agentcore-managed-kb-infra",
  "version": "0.1.0",
  "private": true,
  "bin": { "infra": "bin/app.js" },
  "scripts": {
    "build": "tsc",
    "test": "jest",
    "synth": "cdk synth",
    "cdk": "cdk"
  },
  "devDependencies": {
    "@types/jest": "30.0.0",
    "@types/node": "24.3.0",
    "aws-cdk": "2.1137.0",
    "jest": "30.4.2",
    "ts-jest": "29.4.12",
    "ts-node": "10.9.2",
    "typescript": "5.9.3"
  },
  "dependencies": {
    "aws-cdk-lib": "2.265.0",
    "cdk-nag": "3.0.2",
    "constructs": "10.8.1"
  }
}
```

创建 `infra/tsconfig.json`：

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "commonjs",
    "lib": ["ES2022"],
    "strict": true,
    "noImplicitAny": true,
    "strictNullChecks": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "declaration": true,
    "inlineSourceMap": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "typeRoots": ["./node_modules/@types"]
  },
  "exclude": ["node_modules", "cdk.out"]
}
```

创建 `infra/jest.config.js`：

```javascript
module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/test'],
  testMatch: ['**/*.test.ts'],
  transform: { '^.+\\.tsx?$': 'ts-jest' },
};
```

创建 `infra/cdk.json`：

```json
{
  "app": "npx ts-node --prefer-ts-exts bin/app.ts",
  "watch": { "exclude": ["README.md", "cdk*.json", "**/*.d.ts", "**/*.js", "tsconfig.json", "package*.json", "yarn.lock", "node_modules", "test"] },
  "context": {
    "@aws-cdk/aws-iam:minimizePolicies": true,
    "@aws-cdk/core:checkSecretUsage": true,
    "@aws-cdk/core:enablePartitionLiterals": true
  }
}
```

修改 `.gitignore`，在末尾追加：

```text

# CDK build output
infra/node_modules/
infra/cdk.out/
infra/*.js
infra/*.d.ts
!infra/jest.config.js
```

安装依赖：`cd infra && npm install`

- [ ] **Step 2: 写失败测试**

创建 `infra/test/foundation-stack.test.ts`：

```typescript
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';

function synth(): Template {
  const app = new cdk.App();
  const stack = new FoundationStack(app, 'TestFoundation', {
    env: { account: '123456789012', region: 'us-east-1' },
    corpusId: 'demo',
  });
  return Template.fromStack(stack);
}

describe('FoundationStack', () => {
  test('creates a customer managed key with rotation enabled', () => {
    synth().hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: true,
    });
  });

  test('both buckets are versioned and encrypted with the CMK', () => {
    const template = synth();
    template.resourceCountIs('AWS::S3::Bucket', 2);
    template.allResourcesProperties('AWS::S3::Bucket', {
      VersioningConfiguration: { Status: 'Enabled' },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          {
            ServerSideEncryptionByDefault: {
              SSEAlgorithm: 'aws:kms',
              KMSMasterKeyID: Match.anyValue(),
            },
          },
        ],
      },
    });
  });

  test('both buckets block all public access', () => {
    synth().allResourcesProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  test('stateful resources are retained on stack deletion', () => {
    const template = synth();
    for (const type of ['AWS::S3::Bucket', 'AWS::KMS::Key']) {
      const resources = template.findResources(type);
      for (const logicalId of Object.keys(resources)) {
        expect(resources[logicalId].DeletionPolicy).toBe('Retain');
      }
    }
  });

  test('canonical bucket expires noncurrent versions but registry bucket does not', () => {
    const template = synth();
    const buckets = template.findResources('AWS::S3::Bucket');
    const lifecycles = Object.values(buckets).map(
      (bucket) => bucket.Properties?.LifecycleConfiguration,
    );
    const withExpiry = lifecycles.filter((config) =>
      JSON.stringify(config ?? {}).includes('NoncurrentVersionExpiration'),
    );
    expect(withExpiry).toHaveLength(1);
  });

  test('exposes bucket names and key arn for dependent stacks', () => {
    const app = new cdk.App();
    const stack = new FoundationStack(app, 'TestFoundation', {
      env: { account: '123456789012', region: 'us-east-1' },
      corpusId: 'demo',
    });
    expect(stack.canonicalBucket).toBeDefined();
    expect(stack.registryBucket).toBeDefined();
    expect(stack.encryptionKey).toBeDefined();
  });
});
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd infra && npx jest test/foundation-stack.test.ts`

Expected: FAIL —— `Cannot find module '../lib/foundation-stack'`。

- [ ] **Step 4: 实现 FoundationStack**

创建 `infra/lib/foundation-stack.ts`：

```typescript
import * as cdk from 'aws-cdk-lib';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface FoundationStackProps extends cdk.StackProps {
  readonly corpusId: string;
}

/**
 * Stateful storage and encryption shared by the knowledge base and release
 * stacks. Retained on deletion so tearing down the platform never destroys
 * published content or audit evidence.
 */
export class FoundationStack extends cdk.Stack {
  public readonly encryptionKey: kms.Key;
  public readonly canonicalBucket: s3.Bucket;
  public readonly registryBucket: s3.Bucket;
  public readonly stateMachineLogGroup: logs.LogGroup;

  constructor(scope: Construct, id: string, props: FoundationStackProps) {
    super(scope, id, props);

    this.encryptionKey = new kms.Key(this, 'PlatformKey', {
      description: `Managed KB platform CMK for corpus ${props.corpusId}`,
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Canonical documents are republishable, so noncurrent versions expire.
    this.canonicalBucket = new s3.Bucket(this, 'CanonicalBucket', {
      versioned: true,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: this.encryptionKey,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        { abortIncompleteMultipartUploadAfter: cdk.Duration.days(7) },
        { noncurrentVersionExpiration: cdk.Duration.days(30) },
      ],
    });

    // Manifests are audit evidence, so no expiry rule is configured.
    this.registryBucket = new s3.Bucket(this, 'RegistryBucket', {
      versioned: true,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: this.encryptionKey,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        { abortIncompleteMultipartUploadAfter: cdk.Duration.days(7) },
      ],
    });

    this.stateMachineLogGroup = new logs.LogGroup(this, 'ReleaseStateMachineLogs', {
      retention: logs.RetentionDays.THREE_MONTHS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    new cdk.CfnOutput(this, 'CanonicalBucketName', {
      value: this.canonicalBucket.bucketName,
      exportName: `${this.stackName}-CanonicalBucketName`,
    });
    new cdk.CfnOutput(this, 'RegistryBucketName', {
      value: this.registryBucket.bucketName,
      exportName: `${this.stackName}-RegistryBucketName`,
    });
    new cdk.CfnOutput(this, 'EncryptionKeyArn', {
      value: this.encryptionKey.keyArn,
      exportName: `${this.stackName}-EncryptionKeyArn`,
    });
  }
}
```

创建 `infra/bin/app.ts`（此时只挂 FoundationStack，后续任务追加）：

```typescript
#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { FoundationStack } from '../lib/foundation-stack';

const app = new cdk.App();

const corpusId = app.node.tryGetContext('corpusId') ?? 'demo';
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION,
};

const foundation = new FoundationStack(app, 'ManagedKbFoundation', {
  env,
  corpusId,
  terminationProtection: true,
  description: 'Stateful storage and encryption for the managed KB platform',
});

void foundation;

cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd infra && npx jest test/foundation-stack.test.ts`

Expected: 6 passed。

- [ ] **Step 6: 处理 cdk-nag 告警**

Run: `cd infra && npx cdk synth ManagedKbFoundation 2>&1 | head -40`

`AwsSolutions-S1`（服务器访问日志）会对两个桶告警。为它们添加抑制项并附理由——在
`bin/app.ts` 的 `cdk.Aspects` 之前插入：

```typescript
import { NagSuppressions } from 'cdk-nag';

NagSuppressions.addStackSuppressions(foundation, [
  {
    id: 'AwsSolutions-S1',
    reason:
      'Object-level access is audited through CloudTrail data events for this ' +
      'reference implementation; a separate access log bucket would itself need ' +
      'a log bucket and adds no evidence not already captured.',
  },
]);
```

重新执行 synth，确认无未抑制告警。若出现其他告警，逐条添加抑制项并写明理由，或修正配置。

- [ ] **Step 7: 提交**

```bash
cd .. && git add infra .gitignore
git commit -m "Add CDK foundation stack for storage and encryption

Separate the canonical and registry buckets because their lifecycles differ:
canonical objects are republishable and expire noncurrent versions, while
manifests are audit evidence and are kept indefinitely."
```

---

## Task 10: KnowledgeBaseStack

**Files:**
- Create: `infra/lib/knowledge-base-stack.ts`
- Create: `infra/test/knowledge-base-stack.test.ts`
- Modify: `infra/bin/app.ts`
- Delete: `scripts/02_provision.sh`

`ManagedKnowledgeBaseConfiguration` 是 createOnly——改 embedding 配置会替换 KB 并丢失
索引。这是本 Stack 单独存在并开启终止保护的原因。

- [ ] **Step 1: 写失败测试**

创建 `infra/test/knowledge-base-stack.test.ts`：

```typescript
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { KnowledgeBaseStack } from '../lib/knowledge-base-stack';

const env = { account: '123456789012', region: 'us-east-1' };

function synth(): Template {
  const app = new cdk.App();
  const foundation = new FoundationStack(app, 'TestFoundation', { env, corpusId: 'demo' });
  const stack = new KnowledgeBaseStack(app, 'TestKnowledgeBase', {
    env,
    corpusId: 'demo',
    canonicalBucket: foundation.canonicalBucket,
    encryptionKey: foundation.encryptionKey,
    canonicalPrefix: 'canonical/demo',
  });
  return Template.fromStack(stack);
}

describe('KnowledgeBaseStack', () => {
  test('creates a managed knowledge base with a managed embedding model', () => {
    synth().hasResourceProperties('AWS::Bedrock::KnowledgeBase', {
      KnowledgeBaseConfiguration: {
        Type: 'MANAGED',
        ManagedKnowledgeBaseConfiguration: {
          EmbeddingModelType: 'MANAGED',
        },
      },
    });
  });

  test('knowledge base is encrypted with the platform CMK', () => {
    synth().hasResourceProperties('AWS::Bedrock::KnowledgeBase', {
      KnowledgeBaseConfiguration: {
        ManagedKnowledgeBaseConfiguration: {
          ServerSideEncryptionConfiguration: {
            KmsKeyArn: Match.anyValue(),
          },
        },
      },
    });
  });

  test('data source retains data so index content survives stack changes', () => {
    synth().hasResourceProperties('AWS::Bedrock::DataSource', {
      DataDeletionPolicy: 'RETAIN',
    });
  });

  test('data source reads only the configured canonical prefix', () => {
    synth().hasResourceProperties('AWS::Bedrock::DataSource', {
      DataSourceConfiguration: {
        Type: 'S3',
        S3Configuration: {
          InclusionPrefixes: ['canonical/demo'],
        },
      },
    });
  });

  test('service role grants read access scoped to the canonical prefix', () => {
    const template = synth();
    const policies = template.findResources('AWS::IAM::Policy');
    const statements = Object.values(policies).flatMap(
      (policy) => policy.Properties.PolicyDocument.Statement,
    );
    const getObject = statements.find((statement: { Action: string | string[] }) =>
      JSON.stringify(statement.Action).includes('s3:GetObject'),
    );
    expect(JSON.stringify(getObject.Resource)).toContain('canonical/demo/*');
  });

  test('service role does not grant write access to the canonical bucket', () => {
    const template = synth();
    const policies = template.findResources('AWS::IAM::Policy');
    const rendered = JSON.stringify(Object.values(policies));
    expect(rendered).not.toContain('s3:PutObject');
    expect(rendered).not.toContain('s3:DeleteObject');
  });

  test('trust policy is scoped to this account', () => {
    synth().hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Principal: { Service: 'bedrock.amazonaws.com' },
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                'aws:SourceAccount': '123456789012',
              }),
            }),
          }),
        ]),
      },
    });
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd infra && npx jest test/knowledge-base-stack.test.ts`

Expected: FAIL —— `Cannot find module '../lib/knowledge-base-stack'`。

- [ ] **Step 3: 实现 KnowledgeBaseStack**

创建 `infra/lib/knowledge-base-stack.ts`：

```typescript
import * as cdk from 'aws-cdk-lib';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface KnowledgeBaseStackProps extends cdk.StackProps {
  readonly corpusId: string;
  readonly canonicalBucket: s3.IBucket;
  readonly encryptionKey: kms.IKey;
  readonly canonicalPrefix: string;
}

/**
 * The managed knowledge base and its data source.
 *
 * Isolated in its own stack because ManagedKnowledgeBaseConfiguration is
 * create-only: changing the embedding configuration replaces the knowledge base
 * and discards the index. Keeping it separate lets the release stack be rebuilt
 * freely without risking indexed content.
 */
export class KnowledgeBaseStack extends cdk.Stack {
  public readonly knowledgeBaseId: string;
  public readonly dataSourceId: string;
  public readonly knowledgeBaseArn: string;

  constructor(scope: Construct, id: string, props: KnowledgeBaseStackProps) {
    super(scope, id, props);

    const serviceRole = new iam.Role(this, 'KnowledgeBaseServiceRole', {
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: {
            'aws:SourceArn': `arn:${this.partition}:bedrock:${this.region}:${this.account}:knowledge-base/*`,
          },
        },
      }),
      description: `Managed KB service role for corpus ${props.corpusId}`,
    });

    // Read-only, and narrowed to the prefix the data source actually indexes.
    serviceRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [
          props.canonicalBucket.arnForObjects(`${props.canonicalPrefix}/*`),
        ],
        conditions: { StringEquals: { 'aws:ResourceAccount': this.account } },
      }),
    );
    serviceRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [props.canonicalBucket.bucketArn],
        conditions: {
          StringEquals: { 'aws:ResourceAccount': this.account },
          'ForAnyValue:StringLike': {
            's3:prefix': [props.canonicalPrefix, `${props.canonicalPrefix}/*`],
          },
        },
      }),
    );
    props.encryptionKey.grantDecrypt(serviceRole);

    const knowledgeBase = new bedrock.CfnKnowledgeBase(this, 'KnowledgeBase', {
      name: `${props.corpusId}-managed-kb`,
      description: `Managed knowledge base for corpus ${props.corpusId}`,
      roleArn: serviceRole.roleArn,
      knowledgeBaseConfiguration: {
        type: 'MANAGED',
        managedKnowledgeBaseConfiguration: {
          embeddingModelType: 'MANAGED',
          serverSideEncryptionConfiguration: {
            kmsKeyArn: props.encryptionKey.keyArn,
          },
        },
      },
      tags: { Project: 'agentcore-managed-kb', CorpusId: props.corpusId },
    });
    knowledgeBase.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    // AWS::Bedrock::DataSource is not taggable, so cost allocation tags live on
    // the knowledge base and the buckets instead.
    const dataSource = new bedrock.CfnDataSource(this, 'DataSource', {
      knowledgeBaseId: knowledgeBase.attrKnowledgeBaseId,
      name: `${props.corpusId}-canonical-s3`,
      dataDeletionPolicy: 'RETAIN',
      dataSourceConfiguration: {
        type: 'S3',
        s3Configuration: {
          bucketArn: props.canonicalBucket.bucketArn,
          inclusionPrefixes: [props.canonicalPrefix],
        },
      },
    });
    dataSource.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    this.knowledgeBaseId = knowledgeBase.attrKnowledgeBaseId;
    this.knowledgeBaseArn = knowledgeBase.attrKnowledgeBaseArn;
    this.dataSourceId = dataSource.attrDataSourceId;

    new cdk.CfnOutput(this, 'KnowledgeBaseId', {
      value: this.knowledgeBaseId,
      exportName: `${this.stackName}-KnowledgeBaseId`,
    });
    new cdk.CfnOutput(this, 'DataSourceId', {
      value: this.dataSourceId,
      exportName: `${this.stackName}-DataSourceId`,
    });
  }
}
```

修改 `infra/bin/app.ts`，在 `foundation` 之后、`void foundation;` 之前插入：

```typescript
const knowledgeBase = new KnowledgeBaseStack(app, 'ManagedKbKnowledgeBase', {
  env,
  corpusId,
  canonicalBucket: foundation.canonicalBucket,
  encryptionKey: foundation.encryptionKey,
  canonicalPrefix: `canonical/${corpusId}`,
  terminationProtection: true,
  description: 'Managed knowledge base and data source',
});
knowledgeBase.addDependency(foundation);

void knowledgeBase;
```

并在顶部加入 import：

```typescript
import { KnowledgeBaseStack } from '../lib/knowledge-base-stack';
```

删除 `void foundation;` 一行（不再需要，foundation 已被引用）。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd infra && npx jest test/knowledge-base-stack.test.ts`

Expected: 7 passed。

- [ ] **Step 5: 校验 synth 与 nag**

Run: `cd infra && npx cdk synth ManagedKbKnowledgeBase 2>&1 | tail -30`

Expected: 成功合成。若 `AwsSolutions-IAM5` 因 `canonical/demo/*` 通配告警，添加抑制项：

```typescript
NagSuppressions.addStackSuppressions(knowledgeBase, [
  {
    id: 'AwsSolutions-IAM5',
    reason:
      'The wildcard is bounded to the single canonical prefix the data source ' +
      'indexes; enumerating object keys is impossible because the corpus changes ' +
      'with every release.',
  },
]);
```

- [ ] **Step 6: 删除被取代的 provisioning 脚本并提交**

`scripts/02_provision.sh` 与本 Stack 功能重叠，保留两者会产生两个 provisioning 真相。

```bash
cd .. && git rm scripts/02_provision.sh
git add infra
git commit -m "Add CDK knowledge base stack and retire the provisioning script

Isolate the knowledge base in its own stack with termination protection,
because its managed configuration is create-only and a replacement would
discard the index."
```

README 中英文对该脚本的引用在 Task 15 统一更新。

---

## Task 11: ReleaseStack —— DynamoDB 与三个 Lambda

**Files:**
- Create: `infra/lib/release-stack.ts`
- Create: `infra/test/release-stack.test.ts`
- Modify: `infra/bin/app.ts`

本任务只建资源与权限，状态机在 Task 12 接线。Lambda 代码打包自仓库根的 `kbp/` 目录。

- [ ] **Step 1: 写失败测试**

创建 `infra/test/release-stack.test.ts`：

```typescript
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { ReleaseStack } from '../lib/release-stack';

const env = { account: '123456789012', region: 'us-east-1' };

function synth(): Template {
  const app = new cdk.App();
  const foundation = new FoundationStack(app, 'TestFoundation', { env, corpusId: 'demo' });
  const stack = new ReleaseStack(app, 'TestRelease', {
    env,
    corpusId: 'demo',
    canonicalBucket: foundation.canonicalBucket,
    registryBucket: foundation.registryBucket,
    encryptionKey: foundation.encryptionKey,
    stateMachineLogGroup: foundation.stateMachineLogGroup,
    knowledgeBaseId: 'KB123456',
    dataSourceId: 'DS123456',
    knowledgeBaseArn: `arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KB123456`,
    canonicalPrefix: 'canonical/demo',
    deletionRatioThreshold: 0.5,
  });
  return Template.fromStack(stack);
}

describe('ReleaseStack', () => {
  test('release table is encrypted with the CMK and has PITR enabled', () => {
    synth().hasResourceProperties('AWS::DynamoDB::Table', {
      SSESpecification: { SSEEnabled: true, SSEType: 'KMS' },
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });

  test('release table uses a composite key that allows multiple corpora later', () => {
    synth().hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: [
        { AttributeName: 'pk', KeyType: 'HASH' },
        { AttributeName: 'sk', KeyType: 'RANGE' },
      ],
    });
  });

  test('release table is retained on stack deletion', () => {
    const tables = synth().findResources('AWS::DynamoDB::Table');
    for (const logicalId of Object.keys(tables)) {
      expect(tables[logicalId].DeletionPolicy).toBe('Retain');
    }
  });

  test('creates exactly three lambda functions', () => {
    synth().resourceCountIs('AWS::Lambda::Function', 3);
  });

  test('gate evaluation lambda has no aws permissions because it is pure', () => {
    const template = synth();
    const policies = template.findResources('AWS::IAM::Policy');
    const rendered = JSON.stringify(Object.values(policies));
    // The pure gate function needs no S3, DynamoDB or Bedrock access.
    expect(rendered).not.toContain('CheckGatesFunctionServiceRoleDefaultPolicy');
  });

  test('verify-s3 lambda is granted read but not write on the canonical bucket', () => {
    const template = synth();
    const policies = template.findResources('AWS::IAM::Policy');
    const rendered = JSON.stringify(Object.values(policies));
    expect(rendered).toContain('s3:GetObject');
    expect(rendered).not.toContain('s3:DeleteObject');
  });

  test('registry lambda can write the release table', () => {
    const template = synth();
    const policies = template.findResources('AWS::IAM::Policy');
    const rendered = JSON.stringify(Object.values(policies));
    expect(rendered).toContain('dynamodb:UpdateItem');
    expect(rendered).toContain('dynamodb:PutItem');
  });

  test('publisher role can start executions but cannot call bedrock directly', () => {
    const template = synth();
    const policies = template.findResources('AWS::IAM::Policy');
    const rendered = JSON.stringify(Object.values(policies));
    expect(rendered).toContain('states:StartExecution');
    const publisherPolicies = Object.values(policies).filter((policy) =>
      JSON.stringify(policy).includes('states:StartExecution'),
    );
    expect(JSON.stringify(publisherPolicies)).not.toContain('bedrock:Retrieve');
  });

  test('deletion ratio threshold is surfaced to the state machine', () => {
    const app = new cdk.App();
    const foundation = new FoundationStack(app, 'TestFoundation', { env, corpusId: 'demo' });
    const stack = new ReleaseStack(app, 'TestRelease', {
      env,
      corpusId: 'demo',
      canonicalBucket: foundation.canonicalBucket,
      registryBucket: foundation.registryBucket,
      encryptionKey: foundation.encryptionKey,
      stateMachineLogGroup: foundation.stateMachineLogGroup,
      knowledgeBaseId: 'KB123456',
      dataSourceId: 'DS123456',
      knowledgeBaseArn: 'arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KB123456',
      canonicalPrefix: 'canonical/demo',
      deletionRatioThreshold: 0.25,
    });
    expect(stack.deletionRatioThreshold).toBe(0.25);
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd infra && npx jest test/release-stack.test.ts`

Expected: FAIL —— `Cannot find module '../lib/release-stack'`。

- [ ] **Step 3: 实现 ReleaseStack**

创建 `infra/lib/release-stack.ts`：

```typescript
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface ReleaseStackProps extends cdk.StackProps {
  readonly corpusId: string;
  readonly canonicalBucket: s3.IBucket;
  readonly registryBucket: s3.IBucket;
  readonly encryptionKey: kms.IKey;
  readonly stateMachineLogGroup: logs.ILogGroup;
  readonly knowledgeBaseId: string;
  readonly dataSourceId: string;
  readonly knowledgeBaseArn: string;
  readonly canonicalPrefix: string;
  readonly deletionRatioThreshold: number;
}

const KBP_ROOT = path.join(__dirname, '..', '..');

/**
 * Stateless release orchestration. Safe to destroy and redeploy because it owns
 * no indexed content; the release table is nonetheless retained so audit history
 * survives an accidental teardown.
 */
export class ReleaseStack extends cdk.Stack {
  public readonly releaseTable: dynamodb.Table;
  public readonly verifyS3Function: lambda.Function;
  public readonly checkGatesFunction: lambda.Function;
  public readonly registryFunction: lambda.Function;
  public readonly publisherRole: iam.Role;
  public readonly deletionRatioThreshold: number;

  constructor(scope: Construct, id: string, props: ReleaseStackProps) {
    super(scope, id, props);

    this.deletionRatioThreshold = props.deletionRatioThreshold;

    this.releaseTable = new dynamodb.Table(this, 'ReleaseTable', {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.encryptionKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const code = lambda.Code.fromAsset(KBP_ROOT, {
      exclude: [
        'infra',
        'artifacts',
        'tmp',
        'docs',
        'experiments',
        'scripts',
        'tests',
        '.git',
        '.venv*',
        '**/__pycache__',
      ],
    });

    const commonProps = {
      runtime: lambda.Runtime.PYTHON_3_12,
      code,
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      logRetention: logs.RetentionDays.THREE_MONTHS,
    };

    this.verifyS3Function = new lambda.Function(this, 'VerifyS3Function', {
      ...commonProps,
      handler: 'kbp.ingestion.handlers.verify_s3.handler',
      description: 'Gate A: canonical objects match the manifest',
      environment: { CANONICAL_BUCKET: props.canonicalBucket.bucketName },
    });
    props.canonicalBucket.grantRead(this.verifyS3Function);
    props.encryptionKey.grantDecrypt(this.verifyS3Function);

    // Pure decision logic: deliberately granted no AWS permissions at all.
    this.checkGatesFunction = new lambda.Function(this, 'CheckGatesFunction', {
      ...commonProps,
      handler: 'kbp.ingestion.handlers.check_gates.handler',
      description: 'Gates B, C and D: pure decision logic',
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
    });

    this.registryFunction = new lambda.Function(this, 'RegistryFunction', {
      ...commonProps,
      handler: 'kbp.ingestion.handlers.registry_ops.handler',
      description: 'Release registry state transitions and atomic promotion',
      environment: { RELEASE_TABLE: this.releaseTable.tableName },
    });
    this.releaseTable.grantReadWriteData(this.registryFunction);
    props.encryptionKey.grantEncryptDecrypt(this.registryFunction);

    // The publisher only starts executions; it has no direct path to the
    // knowledge base, so all ingestion flows through the gated state machine.
    this.publisherRole = new iam.Role(this, 'PublisherRole', {
      assumedBy: new iam.AccountPrincipal(this.account),
      description: `Starts release executions for corpus ${props.corpusId}`,
    });
    props.canonicalBucket.grantReadWrite(this.publisherRole);
    props.registryBucket.grantReadWrite(this.publisherRole);
    props.encryptionKey.grantEncryptDecrypt(this.publisherRole);

    new cdk.CfnOutput(this, 'ReleaseTableName', {
      value: this.releaseTable.tableName,
      exportName: `${this.stackName}-ReleaseTableName`,
    });
    new cdk.CfnOutput(this, 'PublisherRoleArn', {
      value: this.publisherRole.roleArn,
      exportName: `${this.stackName}-PublisherRoleArn`,
    });
  }
}
```

修改 `infra/bin/app.ts`，在 `knowledgeBase` 之后插入：

```typescript
const release = new ReleaseStack(app, 'ManagedKbRelease', {
  env,
  corpusId,
  canonicalBucket: foundation.canonicalBucket,
  registryBucket: foundation.registryBucket,
  encryptionKey: foundation.encryptionKey,
  stateMachineLogGroup: foundation.stateMachineLogGroup,
  knowledgeBaseId: knowledgeBase.knowledgeBaseId,
  dataSourceId: knowledgeBase.dataSourceId,
  knowledgeBaseArn: knowledgeBase.knowledgeBaseArn,
  canonicalPrefix: `canonical/${corpusId}`,
  deletionRatioThreshold: 0.5,
  description: 'Release orchestration, registry and gates',
});
release.addDependency(knowledgeBase);

void release;
```

并加入 import：

```typescript
import { ReleaseStack } from '../lib/release-stack';
```

删除 `void knowledgeBase;` 一行。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd infra && npx jest test/release-stack.test.ts`

Expected: 9 passed。

- [ ] **Step 5: 提交**

```bash
cd .. && git add infra
git commit -m "Add CDK release stack with registry table and gate functions

Grant the gate evaluation function no AWS permissions at all, since it only
transforms data, and give the publisher role no direct path to the knowledge
base so every ingestion must pass through the gated state machine."
```

---

## Task 12: 状态机接线（九步拓扑）

**Files:**
- Create: `infra/lib/state-machine.ts`
- Create: `infra/test/state-machine.test.ts`
- Modify: `infra/lib/release-stack.ts`

拓扑已通过 `ValidateStateMachineDefinition` 校验，包括轮询计数器与全部 Catch 分支。本任务
的验收核心是：**每一道门禁到 `FailRelease` 都存在一条边，且没有任何门禁能绕过而到达
`PromoteRelease`**。

- [ ] **Step 1: 写失败测试**

创建 `infra/test/state-machine.test.ts`：

```typescript
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { ReleaseStack } from '../lib/release-stack';

const env = { account: '123456789012', region: 'us-east-1' };

function definition(): Record<string, any> {
  const app = new cdk.App();
  const foundation = new FoundationStack(app, 'TestFoundation', { env, corpusId: 'demo' });
  const stack = new ReleaseStack(app, 'TestRelease', {
    env,
    corpusId: 'demo',
    canonicalBucket: foundation.canonicalBucket,
    registryBucket: foundation.registryBucket,
    encryptionKey: foundation.encryptionKey,
    stateMachineLogGroup: foundation.stateMachineLogGroup,
    knowledgeBaseId: 'KB123456',
    dataSourceId: 'DS123456',
    knowledgeBaseArn: 'arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KB123456',
    canonicalPrefix: 'canonical/demo',
    deletionRatioThreshold: 0.5,
  });
  const template = Template.fromStack(stack);
  const machines = template.findResources('AWS::StepFunctions::StateMachine');
  const raw = Object.values(machines)[0].Properties.DefinitionString;
  // The definition is a Fn::Join of literals and token references; concatenate
  // the literal parts so state names and transitions can be asserted.
  const joined = raw['Fn::Join'][1]
    .map((part: unknown) => (typeof part === 'string' ? part : '"TOKEN"'))
    .join('');
  return JSON.parse(joined);
}

describe('release state machine topology', () => {
  test('is a STANDARD state machine with logging enabled', () => {
    const app = new cdk.App();
    const foundation = new FoundationStack(app, 'TestFoundation', { env, corpusId: 'demo' });
    const stack = new ReleaseStack(app, 'TestRelease', {
      env,
      corpusId: 'demo',
      canonicalBucket: foundation.canonicalBucket,
      registryBucket: foundation.registryBucket,
      encryptionKey: foundation.encryptionKey,
      stateMachineLogGroup: foundation.stateMachineLogGroup,
      knowledgeBaseId: 'KB123456',
      dataSourceId: 'DS123456',
      knowledgeBaseArn: 'arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KB123456',
      canonicalPrefix: 'canonical/demo',
      deletionRatioThreshold: 0.5,
    });
    Template.fromStack(stack).hasResourceProperties(
      'AWS::StepFunctions::StateMachine',
      { StateMachineType: 'STANDARD' },
    );
  });

  test('every gate has a transition to FailRelease', () => {
    const states = definition().States;
    for (const gateChoice of [
      'GateAChoice',
      'GateBChoice',
      'GateCChoice',
      'GateDChoice',
    ]) {
      const rendered = JSON.stringify(states[gateChoice]);
      expect(rendered).toContain('FailRelease');
    }
  });

  test('no gate choice can reach PromoteRelease directly', () => {
    const states = definition().States;
    for (const gateChoice of ['GateAChoice', 'GateBChoice', 'GateCChoice']) {
      expect(JSON.stringify(states[gateChoice])).not.toContain('PromoteRelease');
    }
  });

  test('PromoteRelease is reachable only from the last gate', () => {
    const states = definition().States;
    const predecessors = Object.entries(states)
      .filter(([name, state]) =>
        name !== 'GateDChoice' && JSON.stringify(state).includes('"PromoteRelease"'),
      )
      .map(([name]) => name);
    expect(predecessors).toEqual([]);
  });

  test('an empty change set succeeds without creating a release record', () => {
    const states = definition().States;
    expect(states.IsChangeSetEmpty.Default).toBe('CreateReleaseRecord');
    const emptyBranch = states.IsChangeSetEmpty.Choices[0].Next;
    expect(states[emptyBranch].Type).toBe('Succeed');
  });

  test('ingest and delete batches run with concurrency one', () => {
    const states = definition().States;
    expect(states.IngestBatches.MaxConcurrency).toBe(1);
    expect(states.DeleteBatches.MaxConcurrency).toBe(1);
  });

  test('throttling is retried with exponential backoff', () => {
    const states = definition().States;
    const retry = states.IngestBatches.Iterator.States.IngestBatch.Retry[0];
    expect(retry.BackoffRate).toBe(2);
    expect(retry.MaxAttempts).toBeGreaterThanOrEqual(6);
  });

  test('polling has a bounded attempt count so it cannot spin forever', () => {
    const states = definition().States;
    const timeoutChoice = states.GateCChoice.Choices.find((choice: any) =>
      JSON.stringify(choice).includes('NumericGreaterThanEquals'),
    );
    expect(timeoutChoice.Next).toBe('FailRelease');
  });

  test('the failure path terminates in a Fail state', () => {
    const states = definition().States;
    expect(states.FailRelease.Next).toBe('ReleaseFailed');
    expect(states.ReleaseFailed.Type).toBe('Fail');
  });

  test('uses managed search configuration for the smoke retrieval', () => {
    const rendered = JSON.stringify(definition().States.SmokeRetrieve);
    expect(rendered).toContain('ManagedSearchConfiguration');
    expect(rendered).not.toContain('VectorSearchConfiguration');
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd infra && npx jest test/state-machine.test.ts`

Expected: FAIL —— 找不到 `AWS::StepFunctions::StateMachine` 资源（ReleaseStack 尚未创建
状态机）。

- [ ] **Step 3: 实现状态机**

创建 `infra/lib/state-machine.ts`。使用 `sfn.DefinitionBody.fromChainable` 构建，逐个门禁
显式连接失败分支。

```typescript
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { Construct } from 'constructs';

export interface ReleaseStateMachineProps {
  readonly verifyS3Function: lambda.IFunction;
  readonly checkGatesFunction: lambda.IFunction;
  readonly registryFunction: lambda.IFunction;
  readonly logGroup: logs.ILogGroup;
  readonly knowledgeBaseArn: string;
  readonly deletionRatioThreshold: number;
  readonly maxPollAttempts: number;
}

const THROTTLE_RETRY: sfn.RetryProps = {
  errors: ['Bedrock.ThrottlingException', 'States.TaskFailed'],
  interval: cdk.Duration.seconds(2),
  backoffRate: 2,
  maxAttempts: 6,
};

/**
 * The fail-closed release pipeline.
 *
 * Fail-closed is enforced by topology rather than by code discipline: each gate
 * is a Choice state whose non-passing branch leads to FailRelease, and no gate
 * has an edge that skips a later gate to reach PromoteRelease.
 */
export function buildReleaseStateMachine(
  scope: Construct,
  id: string,
  props: ReleaseStateMachineProps,
): sfn.StateMachine {
  const registryCall = (name: string, payload: Record<string, unknown>) =>
    new tasks.LambdaInvoke(scope, name, {
      lambdaFunction: props.registryFunction,
      payload: sfn.TaskInput.fromObject(payload),
      resultPath: sfn.JsonPath.DISCARD,
    });

  const failRelease = new tasks.LambdaInvoke(scope, 'FailRelease', {
    lambdaFunction: props.registryFunction,
    payload: sfn.TaskInput.fromObject({
      action: 'fail',
      corpusId: sfn.JsonPath.stringAt('$.corpusId'),
      releaseId: sfn.JsonPath.stringAt('$.releaseId'),
      reason: sfn.JsonPath.stringAt('$.failureReason'),
    }),
    resultPath: sfn.JsonPath.DISCARD,
  }).next(
    new sfn.Fail(scope, 'ReleaseFailed', {
      error: 'ReleaseGateFailed',
      cause: 'A release gate failed; the active pointer was not modified',
    }),
  );

  const catchToFail: sfn.CatchProps = {
    errors: ['States.ALL'],
    resultPath: '$.error',
  };

  const promoteRelease = new tasks.LambdaInvoke(scope, 'PromoteRelease', {
    lambdaFunction: props.registryFunction,
    payload: sfn.TaskInput.fromObject({
      action: 'promote',
      corpusId: sfn.JsonPath.stringAt('$.corpusId'),
      releaseId: sfn.JsonPath.stringAt('$.releaseId'),
      expectedPreviousReleaseId: sfn.JsonPath.stringAt('$.pointer.activeReleaseId'),
    }),
    resultPath: sfn.JsonPath.DISCARD,
  })
    .addCatch(failRelease, catchToFail)
    .next(new sfn.Succeed(scope, 'ReleaseSucceeded'));

  const gateD = new sfn.Choice(scope, 'GateDChoice')
    .when(sfn.Condition.booleanEquals('$.gateD.passed', true), promoteRelease)
    .otherwise(failRelease);

  const evaluateSmoke = new tasks.LambdaInvoke(scope, 'EvaluateSmoke', {
    lambdaFunction: props.checkGatesFunction,
    payload: sfn.TaskInput.fromObject({
      gate: 'smokeRetrieval',
      expectation: sfn.JsonPath.stringAt('$.smokeExpectation'),
      retrievedDocumentIds: sfn.JsonPath.listAt('$.smokeDocumentIds'),
      target: sfn.JsonPath.stringAt('$.smokeTarget'),
    }),
    resultSelector: { passed: sfn.JsonPath.booleanAt('$.Payload.passed') },
    resultPath: '$.gateD',
  })
    .addCatch(failRelease, catchToFail)
    .next(gateD);

  const smokeRetrieve = new tasks.CallAwsService(scope, 'SmokeRetrieve', {
    service: 'bedrockagentruntime',
    action: 'retrieve',
    parameters: {
      KnowledgeBaseId: sfn.JsonPath.stringAt('$.knowledgeBaseId'),
      RetrievalQuery: { Text: sfn.JsonPath.stringAt('$.smokeQuery') },
      RetrievalConfiguration: {
        ManagedSearchConfiguration: { NumberOfResults: 10 },
      },
    },
    iamResources: [props.knowledgeBaseArn],
    iamAction: 'bedrock:Retrieve',
    resultPath: '$.smoke',
  })
    .addCatch(failRelease, catchToFail)
    .next(evaluateSmoke);

  const markTesting = registryCall('MarkTesting', {
    action: 'advanceStatus',
    corpusId: sfn.JsonPath.stringAt('$.corpusId'),
    releaseId: sfn.JsonPath.stringAt('$.releaseId'),
    status: 'TESTING',
  }).next(smokeRetrieve);

  const waitForSettlement = new sfn.Wait(scope, 'WaitForSettlement', {
    time: sfn.WaitTime.duration(cdk.Duration.seconds(15)),
  });

  const incrementPollAttempt = new sfn.Pass(scope, 'IncrementPollAttempt', {
    parameters: {
      'pollAttempt.$': 'States.MathAdd($.pollAttempt, 1)',
    },
    resultPath: '$.pollAttemptHolder',
  }).next(waitForSettlement);

  const gateC = new sfn.Choice(scope, 'GateCChoice')
    .when(
      sfn.Condition.and(
        sfn.Condition.booleanEquals('$.gateC.settled', true),
        sfn.Condition.booleanEquals('$.gateC.passed', true),
      ),
      markTesting,
    )
    .when(
      sfn.Condition.and(
        sfn.Condition.booleanEquals('$.gateC.settled', true),
        sfn.Condition.booleanEquals('$.gateC.passed', false),
      ),
      failRelease,
    )
    .when(
      sfn.Condition.numberGreaterThanEquals('$.pollAttempt', props.maxPollAttempts),
      failRelease,
    )
    .otherwise(incrementPollAttempt);

  const evaluateIngestStatus = new tasks.LambdaInvoke(scope, 'EvaluateIngestStatus', {
    lambdaFunction: props.checkGatesFunction,
    payload: sfn.TaskInput.fromObject({
      gate: 'ingestStatus',
      documentDetails: sfn.JsonPath.listAt('$.polled.documentDetails'),
    }),
    resultSelector: {
      settled: sfn.JsonPath.booleanAt('$.Payload.settled'),
      passed: sfn.JsonPath.booleanAt('$.Payload.passed'),
      failures: sfn.JsonPath.listAt('$.Payload.failures'),
    },
    resultPath: '$.gateC',
  })
    .addCatch(failRelease, catchToFail)
    .next(gateC);

  const getDocumentStatuses = new tasks.CallAwsService(scope, 'GetDocumentStatuses', {
    service: 'bedrockagent',
    action: 'getKnowledgeBaseDocuments',
    parameters: {
      KnowledgeBaseId: sfn.JsonPath.stringAt('$.knowledgeBaseId'),
      DataSourceId: sfn.JsonPath.stringAt('$.dataSourceId'),
      DocumentIdentifiers: sfn.JsonPath.listAt('$.pollIdentifiers'),
    },
    iamResources: [props.knowledgeBaseArn],
    iamAction: 'bedrock:GetKnowledgeBaseDocuments',
    resultSelector: {
      documentDetails: sfn.JsonPath.listAt('$.DocumentDetails'),
    },
    resultPath: '$.polled',
  })
    .addCatch(failRelease, catchToFail)
    .next(evaluateIngestStatus);

  waitForSettlement.next(getDocumentStatuses);

  const deleteBatches = new sfn.Map(scope, 'DeleteBatches', {
    itemsPath: '$.deleteBatches',
    maxConcurrency: 1,
    resultPath: sfn.JsonPath.DISCARD,
  });
  deleteBatches.itemProcessor(
    new tasks.CallAwsService(scope, 'DeleteBatch', {
      service: 'bedrockagent',
      action: 'deleteKnowledgeBaseDocuments',
      parameters: {
        KnowledgeBaseId: sfn.JsonPath.stringAt('$.knowledgeBaseId'),
        DataSourceId: sfn.JsonPath.stringAt('$.dataSourceId'),
        ClientToken: sfn.JsonPath.stringAt('$.clientToken'),
        DocumentIdentifiers: sfn.JsonPath.listAt('$.identifiers'),
      },
      iamResources: [props.knowledgeBaseArn],
      iamAction: 'bedrock:DeleteKnowledgeBaseDocuments',
    }).addRetry(THROTTLE_RETRY),
  );
  deleteBatches.addCatch(failRelease, catchToFail);
  deleteBatches.next(waitForSettlement);

  const ingestBatches = new sfn.Map(scope, 'IngestBatches', {
    itemsPath: '$.ingestBatches',
    maxConcurrency: 1,
    resultPath: sfn.JsonPath.DISCARD,
  });
  ingestBatches.itemProcessor(
    new tasks.CallAwsService(scope, 'IngestBatch', {
      service: 'bedrockagent',
      action: 'ingestKnowledgeBaseDocuments',
      parameters: {
        KnowledgeBaseId: sfn.JsonPath.stringAt('$.knowledgeBaseId'),
        DataSourceId: sfn.JsonPath.stringAt('$.dataSourceId'),
        ClientToken: sfn.JsonPath.stringAt('$.clientToken'),
        Documents: sfn.JsonPath.listAt('$.documents'),
      },
      iamResources: [props.knowledgeBaseArn],
      iamAction: 'bedrock:IngestKnowledgeBaseDocuments',
    }).addRetry(THROTTLE_RETRY),
  );
  ingestBatches.addCatch(failRelease, catchToFail);
  ingestBatches.next(deleteBatches);

  const markIngesting = registryCall('MarkIngesting', {
    action: 'advanceStatus',
    corpusId: sfn.JsonPath.stringAt('$.corpusId'),
    releaseId: sfn.JsonPath.stringAt('$.releaseId'),
    status: 'INGESTING',
  }).next(ingestBatches);

  const gateB = new sfn.Choice(scope, 'GateBChoice')
    .when(sfn.Condition.booleanEquals('$.gateB.passed', true), markIngesting)
    .otherwise(failRelease);

  const checkDeletionRatio = new tasks.LambdaInvoke(scope, 'CheckDeletionRatio', {
    lambdaFunction: props.checkGatesFunction,
    payload: sfn.TaskInput.fromObject({
      gate: 'deletionRatio',
      deletedCount: sfn.JsonPath.numberAt('$.changeCounts.deleted'),
      previousDocumentCount: sfn.JsonPath.numberAt('$.previousDocumentCount'),
      threshold: props.deletionRatioThreshold,
      allowBulkDeletion: sfn.JsonPath.stringAt('$.allowBulkDeletion'),
    }),
    resultSelector: {
      passed: sfn.JsonPath.booleanAt('$.Payload.passed'),
      ratio: sfn.JsonPath.numberAt('$.Payload.ratio'),
    },
    resultPath: '$.gateB',
  })
    .addCatch(failRelease, catchToFail)
    .next(gateB);

  const gateA = new sfn.Choice(scope, 'GateAChoice')
    .when(sfn.Condition.booleanEquals('$.gateA.passed', true), checkDeletionRatio)
    .otherwise(failRelease);

  const verifyS3 = new tasks.LambdaInvoke(scope, 'VerifyS3Consistency', {
    lambdaFunction: props.verifyS3Function,
    payload: sfn.TaskInput.fromObject({
      prefix: sfn.JsonPath.stringAt('$.prefix'),
      upserts: sfn.JsonPath.listAt('$.upserts'),
      deletions: sfn.JsonPath.listAt('$.deletions'),
    }),
    resultSelector: {
      passed: sfn.JsonPath.booleanAt('$.Payload.passed'),
      missing: sfn.JsonPath.listAt('$.Payload.missing'),
      mismatched: sfn.JsonPath.listAt('$.Payload.mismatched'),
      surviving: sfn.JsonPath.listAt('$.Payload.surviving'),
    },
    resultPath: '$.gateA',
  })
    .addCatch(failRelease, catchToFail)
    .next(gateA);

  const createReleaseRecord = new tasks.LambdaInvoke(scope, 'CreateReleaseRecord', {
    lambdaFunction: props.registryFunction,
    payload: sfn.TaskInput.fromObject({
      action: 'createRelease',
      corpusId: sfn.JsonPath.stringAt('$.corpusId'),
      releaseId: sfn.JsonPath.stringAt('$.releaseId'),
      manifestS3Uri: sfn.JsonPath.stringAt('$.manifestS3Uri'),
      manifestS3VersionId: sfn.JsonPath.stringAt('$.manifestS3VersionId'),
      parentReleaseId: sfn.JsonPath.stringAt('$.pointer.activeReleaseId'),
      executionArn: sfn.JsonPath.stringAt('$$.Execution.Id'),
    }),
    resultPath: sfn.JsonPath.DISCARD,
  })
    .addCatch(failRelease, catchToFail)
    .next(verifyS3);

  // An empty change set exits before a release record exists, so no FAILED or
  // ACTIVE record is written for a no-op publish.
  const isChangeSetEmpty = new sfn.Choice(scope, 'IsChangeSetEmpty')
    .when(
      sfn.Condition.and(
        sfn.Condition.numberEquals('$.changeCounts.added', 0),
        sfn.Condition.numberEquals('$.changeCounts.modified', 0),
        sfn.Condition.numberEquals('$.changeCounts.deleted', 0),
      ),
      new sfn.Succeed(scope, 'NoChanges'),
    )
    .otherwise(createReleaseRecord);

  const readPointer = new tasks.LambdaInvoke(scope, 'ReadPointer', {
    lambdaFunction: props.registryFunction,
    payload: sfn.TaskInput.fromObject({
      action: 'readPointer',
      corpusId: sfn.JsonPath.stringAt('$.corpusId'),
    }),
    resultSelector: {
      activeReleaseId: sfn.JsonPath.stringAt('$.Payload.activeReleaseId'),
    },
    resultPath: '$.pointer',
  }).next(isChangeSetEmpty);

  return new sfn.StateMachine(scope, id, {
    stateMachineType: sfn.StateMachineType.STANDARD,
    definitionBody: sfn.DefinitionBody.fromChainable(readPointer),
    timeout: cdk.Duration.hours(2),
    logs: {
      destination: props.logGroup,
      level: sfn.LogLevel.ALL,
      includeExecutionData: true,
    },
    tracingEnabled: true,
  });
}
```

修改 `infra/lib/release-stack.ts`：在文件顶部加入 import：

```typescript
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import { buildReleaseStateMachine } from './state-machine';
```

在类中加入公开字段声明：

```typescript
  public readonly stateMachine: sfn.StateMachine;
```

在 `publisherRole` 创建之后、`CfnOutput` 之前插入：

```typescript
    this.stateMachine = buildReleaseStateMachine(this, 'ReleaseStateMachine', {
      verifyS3Function: this.verifyS3Function,
      checkGatesFunction: this.checkGatesFunction,
      registryFunction: this.registryFunction,
      logGroup: props.stateMachineLogGroup,
      knowledgeBaseArn: props.knowledgeBaseArn,
      deletionRatioThreshold: props.deletionRatioThreshold,
      maxPollAttempts: 60,
    });
    this.stateMachine.grantStartExecution(this.publisherRole);
    this.stateMachine.grantRead(this.publisherRole);
```

并追加输出：

```typescript
    new cdk.CfnOutput(this, 'StateMachineArn', {
      value: this.stateMachine.stateMachineArn,
      exportName: `${this.stackName}-StateMachineArn`,
    });
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd infra && npx jest`

Expected: 全部 passed（foundation 6 + knowledge-base 7 + release 9 + state-machine 10）。

- [ ] **Step 5: 校验合成结果**

Run: `cd infra && npx cdk synth ManagedKbRelease > /tmp/release-synth.yaml && echo OK`

Expected: OK。若 `AwsSolutions-IAM4`（Lambda 使用 AWS 托管的基础执行策略）或
`AwsSolutions-SF1`/`SF2` 告警，前者添加抑制项并说明使用托管基础执行策略的理由，后两者本
实现已启用全量日志与 X-Ray，应当不触发。

- [ ] **Step 6: 提交**

```bash
cd .. && git add infra
git commit -m "Wire the fail-closed release state machine

Route every gate's non-passing branch to FailRelease and give no gate an edge
that reaches promotion early, so fail-closed holds by topology rather than by
remembering to check a return value. Bound the status poller so an unexpected
document state cannot spin forever."
```
