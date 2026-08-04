#!/usr/bin/env python3

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CHINESE_README = ROOT / "README.md"
ENGLISH_README = ROOT / "README.en.md"


def fenced_blocks(content: str) -> list[str]:
    return re.findall(r"^```[^\n]*\n.*?^```$", content, flags=re.MULTILINE | re.DOTALL)


def level_two_headings(content: str) -> list[str]:
    return re.findall(r"^## .+$", content, flags=re.MULTILINE)


def main() -> int:
    chinese = CHINESE_README.read_text(encoding="utf-8")
    english = ENGLISH_README.read_text(encoding="utf-8")
    failures = []

    required_files = [
        ROOT / "docs" / "RESULTS.md",
        ROOT / "docs" / "KB_PLATFORM_SELECTION_GUIDE.md",
        ROOT / "docs" / "AWS_KB_RAG_BEST_PRACTICES.md",
        ROOT / "docs" / "SEMANTIC_CHUNKING_EXPERIMENT.md",
        ROOT / "docs" / "METADATA_EXPERIMENT.md",
        ROOT / "docs" / "MD_CORPUS_PIPELINE.md",
        ROOT / ".agents" / "skills" / "kb-rag-data-preparation" / "SKILL.md",
        ROOT / "SECURITY.md",
        ROOT / "LICENSE",
    ]
    for path in required_files:
        if not path.is_file():
            failures.append(f"missing linked file: {path.relative_to(ROOT)}")

    required_links = [
        (chinese, "[English](README.en.md)", "Chinese README language link"),
        (english, "[中文](README.md)", "English README language link"),
        (chinese, "(docs/RESULTS.md)", "Chinese results link"),
        (english, "(docs/RESULTS.md)", "English results link"),
        (
            chinese,
            "(docs/KB_PLATFORM_SELECTION_GUIDE.md)",
            "Chinese selection guide link",
        ),
        (
            english,
            "(docs/KB_PLATFORM_SELECTION_GUIDE.md)",
            "English selection guide link",
        ),
        (
            chinese,
            "(docs/AWS_KB_RAG_BEST_PRACTICES.md)",
            "Chinese best-practices report link",
        ),
        (
            english,
            "(docs/AWS_KB_RAG_BEST_PRACTICES.md)",
            "English best-practices report link",
        ),
        (
            chinese,
            "(docs/SEMANTIC_CHUNKING_EXPERIMENT.md)",
            "Chinese semantic chunking report link",
        ),
        (
            english,
            "(docs/SEMANTIC_CHUNKING_EXPERIMENT.md)",
            "English semantic chunking report link",
        ),
        (
            chinese,
            "(docs/METADATA_EXPERIMENT.md)",
            "Chinese metadata experiment report link",
        ),
        (
            english,
            "(docs/METADATA_EXPERIMENT.md)",
            "English metadata experiment report link",
        ),
        (
            chinese,
            "(docs/MD_CORPUS_PIPELINE.md)",
            "Chinese Markdown corpus pipeline link",
        ),
        (
            english,
            "(docs/MD_CORPUS_PIPELINE.md)",
            "English Markdown corpus pipeline link",
        ),
        (
            chinese,
            "(.agents/skills/kb-rag-data-preparation/SKILL.md)",
            "Chinese project skill link",
        ),
        (
            english,
            "(.agents/skills/kb-rag-data-preparation/SKILL.md)",
            "English project skill link",
        ),
        (chinese, "(LICENSE)", "Chinese license link"),
        (english, "(LICENSE)", "English license link"),
    ]
    for content, expected, description in required_links:
        if expected not in content:
            failures.append(f"missing {description}: {expected}")

    chinese_headings = level_two_headings(chinese)
    english_headings = level_two_headings(english)
    if len(chinese_headings) != len(english_headings):
        failures.append(
            "README section counts differ: "
            f"Chinese={len(chinese_headings)}, English={len(english_headings)}"
        )

    chinese_blocks = fenced_blocks(chinese)
    english_blocks = fenced_blocks(english)
    if chinese_blocks != english_blocks:
        failures.append("README fenced command blocks differ")

    if failures:
        for failure in failures:
            print(f"README synchronization check failed: {failure}", file=sys.stderr)
        return 1

    print(
        "README synchronization check passed: "
        f"{len(chinese_headings)} sections, {len(chinese_blocks)} command blocks."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
