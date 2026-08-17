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
