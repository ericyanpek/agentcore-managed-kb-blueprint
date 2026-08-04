#!/usr/bin/env python3

import argparse
import hashlib
import json
import re
import shutil
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path


PAGE_PATTERN = re.compile(r"^## PDF 第 (\d+) 页$", re.MULTILINE)
BEST_PRACTICE_PATTERN = re.compile(r"^(GAME[A-Z0-9]+-BP\d{2})\s+(.+)$")
QUESTION_PATTERN = re.compile(r"^(GAME[A-Z0-9]+)：(.+)$")
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
SENTENCE_PATTERN = re.compile(r"(?<=[。！？；.!?;])")
STRUCTURAL_LABELS = {
    "最佳实践",
    "客户示例",
    "实施指导",
    "实施步骤",
    "资源",
    "设计原则",
}


@dataclass
class SourceLine:
    page: int
    text: str


@dataclass
class SemanticSection:
    pillar: str
    topic: str
    question_id: str
    best_practice_id: str
    subsection: str
    lines: list[SourceLine] = field(default_factory=list)

    @property
    def page_start(self) -> int:
        return min(line.page for line in self.lines)

    @property
    def page_end(self) -> int:
        return max(line.page for line in self.lines)

    @property
    def section_path(self) -> str:
        parts = [
            self.pillar,
            self.topic,
            self.question_id,
            self.best_practice_id,
            self.subsection,
        ]
        return " / ".join(part for part in parts if part)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_pages(content: str) -> dict[int, str]:
    matches = list(PAGE_PATTERN.finditer(content))
    pages = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        pages[int(match.group(1))] = content[start:end].strip()
    return pages


def pillar_for_page(page: int) -> str:
    ranges = [
        (8, 30, "摘要、场景与定义"),
        (31, 53, "卓越运营"),
        (54, 84, "安全性"),
        (85, 94, "可靠性"),
        (95, 118, "性能效率"),
        (119, 132, "成本优化"),
        (133, 141, "可持续性与总结"),
    ]
    for start, end, pillar in ranges:
        if start <= page <= end:
            return pillar
    return "附录"


def is_structural_label(text: str) -> bool:
    return text in STRUCTURAL_LABELS


def join_wrapped_lines(lines: list[str]) -> str:
    output = ""
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if not output:
            output = text
            continue
        separator = ""
        if output[-1:].isascii() and output[-1:].isalnum():
            separator = " "
        if text[:1].isascii() and text[:1].isalnum():
            separator = " "
        output += separator + text
    return re.sub(r"[ \t]+", " ", output).strip()


def normalize_lines(lines: list[SourceLine]) -> list[str]:
    blocks = []
    current = []

    def flush() -> None:
        if current:
            blocks.append(join_wrapped_lines(current))
            current.clear()

    for source_line in lines:
        text = source_line.text.strip()
        if not text:
            flush()
            continue
        if (
            text.startswith("•")
            or BEST_PRACTICE_PATTERN.match(text)
            or QUESTION_PATTERN.match(text)
            or is_structural_label(text)
        ):
            flush()
            current.append(text)
            if text.startswith("•") or is_structural_label(text):
                flush()
            continue
        current.append(text)
    flush()
    return [block for block in blocks if block]


def build_sections(pages: dict[int, str], start_page: int, end_page: int) -> list[SemanticSection]:
    sections = []
    current: SemanticSection | None = None
    current_topic = ""
    current_question = ""
    current_best_practice = ""
    current_subsection = ""

    def ensure_section(page: int) -> SemanticSection:
        nonlocal current
        if current is None:
            current = SemanticSection(
                pillar=pillar_for_page(page),
                topic=current_topic,
                question_id=current_question,
                best_practice_id=current_best_practice,
                subsection=current_subsection,
            )
        return current

    def rotate(page: int) -> SemanticSection:
        nonlocal current
        if current is not None and current.lines:
            sections.append(current)
        current = SemanticSection(
            pillar=pillar_for_page(page),
            topic=current_topic,
            question_id=current_question,
            best_practice_id=current_best_practice,
            subsection=current_subsection,
        )
        return current

    for page in range(start_page, end_page + 1):
        page_text = pages.get(page, "")
        for raw_line in page_text.splitlines():
            text = raw_line.strip()
            if not text:
                ensure_section(page).lines.append(SourceLine(page, ""))
                continue

            question = QUESTION_PATTERN.match(text)
            best_practice = BEST_PRACTICE_PATTERN.match(text)
            if question:
                current_question = question.group(1)
                current_topic = question.group(2).strip()
                current_best_practice = ""
                current_subsection = "问题概述"
                rotate(page)
            elif best_practice and not text.startswith("•"):
                current_best_practice = best_practice.group(1)
                current_topic = best_practice.group(2).strip()
                current_subsection = "最佳实践正文"
                rotate(page)
            elif is_structural_label(text):
                current_subsection = text
                rotate(page)

            ensure_section(page).lines.append(SourceLine(page, text))

    if current is not None and current.lines:
        sections.append(current)
    return [section for section in sections if normalize_lines(section.lines)]


def split_long_block(block: str, max_chars: int) -> list[str]:
    if len(block) <= max_chars:
        return [block]
    sentences = [part.strip() for part in SENTENCE_PATTERN.split(block) if part.strip()]
    if len(sentences) == 1:
        return [block[index : index + max_chars] for index in range(0, len(block), max_chars)]

    parts = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.extend(
                sentence[index : index + max_chars]
                for index in range(0, len(sentence), max_chars)
            )
        elif current and len(current) + len(sentence) > max_chars:
            parts.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        parts.append(current)
    return parts


def chunk_section(
    section: SemanticSection,
    *,
    target_chars: int,
    max_chars: int,
    min_chars: int,
) -> list[str]:
    blocks = []
    for block in normalize_lines(section.lines):
        blocks.extend(split_long_block(block, max_chars))

    chunks = []
    current = []
    current_length = 0
    for block in blocks:
        projected = current_length + len(block) + (1 if current else 0)
        if current and projected > target_chars:
            chunks.append("\n\n".join(current))
            current = [block]
            current_length = len(block)
        else:
            current.append(block)
            current_length = projected
    if current:
        chunks.append("\n\n".join(current))

    if len(chunks) > 1 and len(chunks[-1]) < min_chars:
        if len(chunks[-2]) + len(chunks[-1]) + 2 <= max_chars:
            chunks[-2] = chunks[-2] + "\n\n" + chunks[-1]
            chunks.pop()
    return chunks


def metadata_value(value: str | int, *, include_for_embedding: bool) -> dict:
    if isinstance(value, int):
        typed_value = {"type": "NUMBER", "numberValue": value}
    else:
        typed_value = {"type": "STRING", "stringValue": value}
    return {
        "value": typed_value,
        "includeForEmbedding": include_for_embedding,
    }


def build_metadata(
    *,
    chunk_id: str,
    corpus_id: str,
    section: SemanticSection,
    source_sha256: str,
) -> dict:
    attributes = {
        "document_id": metadata_value(chunk_id, include_for_embedding=False),
        "corpus_id": metadata_value(corpus_id, include_for_embedding=False),
        "experiment_variant": metadata_value(
            "structure-aware-semantic-v1",
            include_for_embedding=False,
        ),
        "title": metadata_value(
            "AWS Well-Architected Framework Games Industry Lens",
            include_for_embedding=True,
        ),
        "domain": metadata_value("games-industry", include_for_embedding=True),
        "language": metadata_value("zh-CN", include_for_embedding=True),
        "classification": metadata_value("PUBLIC", include_for_embedding=False),
        "content_format": metadata_value(
            "pre-chunked-semantic-markdown",
            include_for_embedding=False,
        ),
        "section_path": metadata_value(
            section.section_path,
            include_for_embedding=True,
        ),
        "source_page_start": metadata_value(
            section.page_start,
            include_for_embedding=False,
        ),
        "source_page_end": metadata_value(
            section.page_end,
            include_for_embedding=False,
        ),
        "source_pdf_sha256": metadata_value(
            source_sha256,
            include_for_embedding=False,
        ),
    }
    if section.question_id:
        attributes["question_id"] = metadata_value(
            section.question_id,
            include_for_embedding=True,
        )
    if section.best_practice_id:
        attributes["best_practice_id"] = metadata_value(
            section.best_practice_id,
            include_for_embedding=True,
        )
    return {"metadataAttributes": attributes}


def write_chunks(
    *,
    sections: list[SemanticSection],
    output_dir: Path,
    corpus_id: str,
    source_url: str,
    source_sha256: str,
    target_chars: int,
    max_chars: int,
    min_chars: int,
) -> tuple[list[dict], str]:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    manifest = []
    digest = hashlib.sha256()
    chunk_number = 0
    for section in sections:
        contents = chunk_section(
            section,
            target_chars=target_chars,
            max_chars=max_chars,
            min_chars=min_chars,
        )
        for section_part, content in enumerate(contents, start=1):
            chunk_number += 1
            chunk_id = f"{corpus_id}-chunk-{chunk_number:04d}"
            filename = f"chunk-{chunk_number:04d}.md"
            path = output_dir / filename
            header = [
                "# AWS Well-Architected Framework 游戏行业视角",
                "",
                f"- 章节路径：{section.section_path}",
                f"- 来源页码：PDF 第 {section.page_start}-{section.page_end} 页",
                f"- 语义块 ID：`{chunk_id}`",
                f"- 原始文档：{source_url}",
                "",
            ]
            markdown = "\n".join(header) + content.strip() + "\n"
            path.write_text(markdown, encoding="utf-8")

            metadata = build_metadata(
                chunk_id=chunk_id,
                corpus_id=corpus_id,
                section=section,
                source_sha256=source_sha256,
            )
            metadata_path = output_dir / f"{filename}.metadata.json"
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            digest.update(filename.encode())
            digest.update(markdown.encode())
            digest.update(metadata_path.read_bytes())
            manifest.append(
                {
                    "chunkId": chunk_id,
                    "file": filename,
                    "sectionPart": section_part,
                    "sectionPath": section.section_path,
                    "bestPracticeId": section.best_practice_id or None,
                    "sourcePageStart": section.page_start,
                    "sourcePageEnd": section.page_end,
                    "contentCharacterCount": len(content),
                    "outputCharacterCount": len(markdown),
                    "sha256": file_sha256(path),
                }
            )

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps({"corpusId": corpus_id, "chunks": manifest}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return manifest, digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--corpus-id",
        default="aws-games-industry-lens-2026-07-31-semantic-v1",
    )
    parser.add_argument(
        "--source-url",
        default=(
            "https://docs.aws.amazon.com/zh_cn/wellarchitected/latest/"
            "games-industry-lens/games-industry-lens.pdf"
        ),
    )
    parser.add_argument("--start-page", type=int, default=8)
    parser.add_argument("--end-page", type=int, default=141)
    parser.add_argument("--target-chars", type=int, default=420)
    parser.add_argument("--max-chars", type=int, default=600)
    parser.add_argument("--min-chars", type=int, default=100)
    args = parser.parse_args()

    try:
        content = args.input.read_text(encoding="utf-8")
        pages = parse_pages(content)
        source_sha256_match = re.search(
            r"原始 PDF SHA-256：`([0-9a-f]{64})`",
            content,
        )
        if not source_sha256_match:
            raise ValueError("input is missing the source PDF SHA-256")
        if args.start_page not in pages or args.end_page not in pages:
            raise ValueError("configured page range is not present in the input")

        sections = build_sections(pages, args.start_page, args.end_page)
        manifest, corpus_sha256 = write_chunks(
            sections=sections,
            output_dir=args.output_dir,
            corpus_id=args.corpus_id,
            source_url=args.source_url,
            source_sha256=source_sha256_match.group(1),
            target_chars=args.target_chars,
            max_chars=args.max_chars,
            min_chars=args.min_chars,
        )

        lengths = [item["contentCharacterCount"] for item in manifest]
        input_best_practices = set(
            re.findall(r"\bGAME[A-Z0-9]+-BP\d{2}\b", content)
        )
        output_best_practices = {
            item["bestPracticeId"]
            for item in manifest
            if item["bestPracticeId"]
        }
        missing_best_practices = sorted(input_best_practices - output_best_practices)
        output_content = "\n".join(
            (args.output_dir / item["file"]).read_text(encoding="utf-8")
            for item in manifest
        )
        non_whitespace = len(re.findall(r"\S", output_content))
        report = {
            "corpusId": args.corpus_id,
            "strategy": "structure-aware-semantic-v1",
            "sourceMarkdownSha256": file_sha256(args.input),
            "sourcePdfSha256": source_sha256_match.group(1),
            "includedPageRange": [args.start_page, args.end_page],
            "excludedPages": [
                page for page in sorted(pages) if page < args.start_page or page > args.end_page
            ],
            "targetCharacters": args.target_chars,
            "maximumCharacters": args.max_chars,
            "minimumCharacters": args.min_chars,
            "semanticSectionCount": len(sections),
            "chunkCount": len(manifest),
            "contentCharacterCount": sum(lengths),
            "minimumChunkCharacters": min(lengths),
            "maximumChunkCharacters": max(lengths),
            "meanChunkCharacters": statistics.fmean(lengths),
            "medianChunkCharacters": statistics.median(lengths),
            "undersizedChunkCount": sum(length < args.min_chars for length in lengths),
            "oversizedChunkCount": sum(length > args.max_chars for length in lengths),
            "bestPracticeCount": len(output_best_practices),
            "missingBestPractices": missing_best_practices,
            "replacementCharacterCount": output_content.count("\ufffd"),
            "cjkRatio": (
                len(CJK_PATTERN.findall(output_content)) / non_whitespace
                if non_whitespace
                else 0
            ),
            "corpusSha256": corpus_sha256,
        }
        if report["oversizedChunkCount"]:
            raise ValueError("semantic output contains oversized chunks")
        if report["replacementCharacterCount"]:
            raise ValueError("semantic output contains Unicode replacement characters")
        if missing_best_practices:
            raise ValueError(
                f"semantic output is missing {len(missing_best_practices)} best-practice IDs"
            )

        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError) as error:
        print(f"Semantic chunk preparation failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
