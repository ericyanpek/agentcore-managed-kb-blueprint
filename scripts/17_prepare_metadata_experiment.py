#!/usr/bin/env python3

import argparse
import hashlib
import json
import shutil
import statistics
import sys
from pathlib import Path


VARIANTS = ("no-metadata", "filter-metadata", "embedded-metadata")
EMBEDDED_FIELDS = {
    "title",
    "domain",
    "language",
    "pillar",
    "topic",
    "section_path",
    "question_id",
    "best_practice_id",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def metadata_value(value: str | int, *, include_for_embedding: bool) -> dict:
    if isinstance(value, int):
        typed_value = {"type": "NUMBER", "numberValue": value}
    else:
        typed_value = {"type": "STRING", "stringValue": value}
    return {
        "value": typed_value,
        "includeForEmbedding": include_for_embedding,
    }


def read_attribute(attributes: dict, name: str) -> str | int | None:
    attribute = attributes.get(name, {})
    value = attribute.get("value", {})
    value_type = value.get("type")
    if value_type == "STRING":
        return value.get("stringValue")
    if value_type == "NUMBER":
        return int(value["numberValue"])
    return None


def canonical_content(markdown: str) -> str:
    lines = markdown.splitlines()
    if len(lines) < 7 or not lines[0].startswith("# AWS Well-Architected"):
        raise ValueError("semantic chunk does not contain the expected generated header")
    source_line_index = next(
        (
            index
            for index, line in enumerate(lines[:10])
            if line.startswith("- 原始文档：")
        ),
        None,
    )
    if source_line_index is None:
        raise ValueError("semantic chunk is missing its generated source header")
    content = "\n".join(lines[source_line_index + 1 :]).strip()
    if not content:
        raise ValueError("semantic chunk is empty after removing the generated header")
    return content + "\n"


def build_metadata(
    *,
    source_attributes: dict,
    variant: str,
    chunk_id: str,
    content_sha256: str,
) -> dict:
    section_path = str(read_attribute(source_attributes, "section_path") or "")
    section_parts = [part.strip() for part in section_path.split("/") if part.strip()]
    values: dict[str, str | int] = {
        "document_id": chunk_id,
        "corpus_id": "aws-games-industry-lens-metadata-experiment-v1",
        "experiment_variant": variant,
        "title": str(
            read_attribute(source_attributes, "title")
            or "AWS Well-Architected Framework Games Industry Lens"
        ),
        "domain": str(read_attribute(source_attributes, "domain") or "games-industry"),
        "language": str(read_attribute(source_attributes, "language") or "zh-CN"),
        "classification": str(
            read_attribute(source_attributes, "classification") or "PUBLIC"
        ),
        "version_date": 20260731,
        "lifecycle_status": "ACTIVE",
        "owner": "aws-well-architected",
        "content_format": "canonical-prechunked-markdown",
        "pillar": section_parts[0] if section_parts else "",
        "topic": section_parts[1] if len(section_parts) > 1 else "",
        "section_path": section_path,
        "source_page_start": int(
            read_attribute(source_attributes, "source_page_start") or 0
        ),
        "source_page_end": int(
            read_attribute(source_attributes, "source_page_end") or 0
        ),
        "source_pdf_sha256": str(
            read_attribute(source_attributes, "source_pdf_sha256") or ""
        ),
        "content_sha256": content_sha256,
    }
    for optional_name in ("question_id", "best_practice_id"):
        optional_value = read_attribute(source_attributes, optional_name)
        if optional_value:
            values[optional_name] = str(optional_value)

    attributes = {
        name: metadata_value(
            value,
            include_for_embedding=(
                variant == "embedded-metadata" and name in EMBEDDED_FIELDS
            ),
        )
        for name, value in values.items()
        if value != ""
    }
    return {"metadataAttributes": attributes}


def prepare(args: argparse.Namespace) -> dict:
    source_manifest = json.loads(
        (args.source_dir / "manifest.json").read_text(encoding="utf-8")
    )
    if args.output_dir.exists():
        shutil.rmtree(args.output_dir)
    for variant in VARIANTS:
        (args.output_dir / variant).mkdir(parents=True)

    content_hashes: dict[str, dict[str, str]] = {variant: {} for variant in VARIANTS}
    metadata_sizes: dict[str, list[int]] = {
        variant: [] for variant in VARIANTS if variant != "no-metadata"
    }
    character_counts = []

    for item in source_manifest["chunks"]:
        filename = item["file"]
        source_path = args.source_dir / filename
        source_metadata_path = args.source_dir / f"{filename}.metadata.json"
        source_metadata = json.loads(source_metadata_path.read_text(encoding="utf-8"))
        source_attributes = source_metadata["metadataAttributes"]
        content = canonical_content(source_path.read_text(encoding="utf-8"))
        content_bytes = content.encode("utf-8")
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        character_counts.append(len(content.rstrip("\n")))

        for variant in VARIANTS:
            destination = args.output_dir / variant / filename
            destination.write_bytes(content_bytes)
            content_hashes[variant][filename] = file_sha256(destination)
            if variant == "no-metadata":
                continue

            metadata = build_metadata(
                source_attributes=source_attributes,
                variant=variant,
                chunk_id=item["chunkId"],
                content_sha256=content_sha256,
            )
            metadata_path = args.output_dir / variant / f"{filename}.metadata.json"
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            size = metadata_path.stat().st_size
            if size > 10 * 1024:
                raise ValueError(f"metadata sidecar exceeds 10 KB: {metadata_path}")
            metadata_sizes[variant].append(size)

    reference_hashes = content_hashes[VARIANTS[0]]
    byte_identical = all(
        hashes == reference_hashes for hashes in content_hashes.values()
    )
    if not byte_identical:
        raise ValueError("content differs across metadata experiment variants")

    report = {
        "experiment": "metadata-ab-v1",
        "sourceCorpusId": source_manifest["corpusId"],
        "variants": {
            "no-metadata": {
                "sidecarPolicy": "none",
                "includeForEmbedding": [],
            },
            "filter-metadata": {
                "sidecarPolicy": "full",
                "includeForEmbedding": [],
            },
            "embedded-metadata": {
                "sidecarPolicy": "full",
                "includeForEmbedding": sorted(EMBEDDED_FIELDS),
            },
        },
        "documentCountPerVariant": len(reference_hashes),
        "byteIdenticalContentAcrossVariants": byte_identical,
        "minimumContentCharacters": min(character_counts),
        "maximumContentCharacters": max(character_counts),
        "meanContentCharacters": statistics.fmean(character_counts),
        "metadataSidecarMaximumBytes": {
            variant: max(sizes) for variant, sizes in metadata_sizes.items()
        },
        "contentSetSha256": hashlib.sha256(
            "".join(
                f"{name}:{digest}\n"
                for name, digest in sorted(reference_hashes.items())
            ).encode()
        ).hexdigest(),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    try:
        report = prepare(args)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"Metadata experiment preparation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
