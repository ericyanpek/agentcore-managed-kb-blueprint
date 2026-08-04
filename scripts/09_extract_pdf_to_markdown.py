#!/usr/bin/env python3

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from pypdf import PdfReader


CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
PAGE_FOOTER_PATTERN = re.compile(r"(?:^|\s)(?:[ivxlcdm]+|\d+)$", re.IGNORECASE)
REPEATED_HEADER = "游戏行业视角 AWS 白皮书"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_page_text(text: str, page_number: int) -> str:
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.splitlines()]

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if page_number > 1 and lines and lines[0].strip() == REPEATED_HEADER:
        lines.pop(0)

    if page_number > 1 and lines and PAGE_FOOTER_PATTERN.search(lines[-1].strip()):
        lines.pop()

    cleaned = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def extract_pdf(pdf_path: Path) -> tuple[list[str], dict]:
    reader = PdfReader(pdf_path)
    pages = []

    for page_number, page in enumerate(reader.pages, start=1):
        pages.append(clean_page_text(page.extract_text() or "", page_number))

    combined = "\n".join(pages)
    non_whitespace_count = len(re.findall(r"\S", combined))
    cjk_count = len(CJK_PATTERN.findall(combined))

    report = {
        "sourcePdf": str(pdf_path),
        "sourcePdfSha256": file_sha256(pdf_path),
        "pageCount": len(pages),
        "emptyPageCount": sum(not page for page in pages),
        "characterCount": len(combined),
        "nonWhitespaceCharacterCount": non_whitespace_count,
        "cjkCharacterCount": cjk_count,
        "cjkRatio": cjk_count / non_whitespace_count if non_whitespace_count else 0,
        "replacementCharacterCount": combined.count("\ufffd"),
    }
    return pages, report


def build_markdown(
    pages: list[str],
    *,
    title: str,
    source_url: str,
    source_sha256: str,
) -> str:
    output = [
        f"# {title}",
        "",
        f"- 原始文档：{source_url}",
        f"- 原始 PDF SHA-256：`{source_sha256}`",
        f"- PDF 页数：{len(pages)}",
        "- 转换说明：按 PDF 物理页提取 UTF-8 文本；每页保留显式页码标题。",
        "",
    ]

    for page_number, text in enumerate(pages, start=1):
        output.extend([f"## PDF 第 {page_number} 页", "", text or "[此页没有可提取文本]", ""])

    return "\n".join(output).rstrip() + "\n"


def validate_report(
    report: dict,
    *,
    expected_pages: int | None,
    max_empty_pages: int,
    min_cjk_ratio: float,
) -> None:
    failures = []
    if expected_pages is not None and report["pageCount"] != expected_pages:
        failures.append(
            f"expected {expected_pages} pages, extracted {report['pageCount']}"
        )
    if report["emptyPageCount"] > max_empty_pages:
        failures.append(
            f"empty pages {report['emptyPageCount']} exceed {max_empty_pages}"
        )
    if report["cjkRatio"] < min_cjk_ratio:
        failures.append(
            f"CJK ratio {report['cjkRatio']:.4f} is below {min_cjk_ratio:.4f}"
        )
    if report["replacementCharacterCount"]:
        failures.append(
            f"found {report['replacementCharacterCount']} replacement characters"
        )
    if failures:
        raise ValueError("; ".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--title",
        default="AWS Well-Architected Framework 游戏行业视角",
    )
    parser.add_argument(
        "--source-url",
        default=(
            "https://docs.aws.amazon.com/zh_cn/wellarchitected/latest/"
            "games-industry-lens/games-industry-lens.pdf"
        ),
    )
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument("--max-empty-pages", type=int, default=0)
    parser.add_argument("--min-cjk-ratio", type=float, default=0.50)
    args = parser.parse_args()

    try:
        pages, report = extract_pdf(args.input)
        validate_report(
            report,
            expected_pages=args.expected_pages,
            max_empty_pages=args.max_empty_pages,
            min_cjk_ratio=args.min_cjk_ratio,
        )
        markdown = build_markdown(
            pages,
            title=args.title,
            source_url=args.source_url,
            source_sha256=report["sourcePdfSha256"],
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(markdown, encoding="utf-8")
        report["outputMarkdown"] = str(args.output)
        report["outputMarkdownSha256"] = file_sha256(args.output)
        report["outputByteCount"] = args.output.stat().st_size
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError) as error:
        print(f"PDF extraction failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
