#!/usr/bin/env python3

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path


SUPPORTED_SOURCE_SUFFIXES = {
    ".csv",
    ".doc",
    ".docx",
    ".html",
    ".md",
    ".pdf",
    ".txt",
    ".xls",
    ".xlsx",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_metadata(path: Path) -> tuple[int, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    attributes = payload.get("metadataAttributes")
    if not isinstance(attributes, dict) or not attributes:
        raise ValueError(f"{path}: metadataAttributes must be a non-empty object")
    embedded = 0
    for key, attribute in attributes.items():
        if not isinstance(attribute.get("includeForEmbedding"), bool):
            raise ValueError(f"{path}: {key} has invalid includeForEmbedding")
        value = attribute.get("value", {})
        value_type = value.get("type")
        expected_key = {
            "STRING": "stringValue",
            "NUMBER": "numberValue",
            "BOOLEAN": "booleanValue",
            "STRING_LIST": "stringListValue",
        }.get(value_type)
        if expected_key is None or expected_key not in value:
            raise ValueError(f"{path}: {key} has an invalid typed value")
        embedded += attribute["includeForEmbedding"]
    return len(attributes), embedded


def profile(directory: Path, sidecar_policy: str) -> dict:
    sources = sorted(
        path
        for path in directory.rglob("*")
        if path.is_file()
        and not path.name.endswith(".metadata.json")
        and path.name != "manifest.json"
        and path.suffix.lower() in SUPPORTED_SOURCE_SUFFIXES
    )
    if not sources:
        raise ValueError("no supported source documents found")

    sizes = [path.stat().st_size for path in sources]
    hashes = [sha256(path) for path in sources]
    sidecar_count = 0
    attribute_counts = []
    embedded_counts = []
    oversized_sidecars = []
    missing_sidecars = []
    for source in sources:
        sidecar = source.with_name(f"{source.name}.metadata.json")
        if not sidecar.exists():
            missing_sidecars.append(str(source.relative_to(directory)))
            continue
        sidecar_count += 1
        if sidecar.stat().st_size > 10 * 1024:
            oversized_sidecars.append(str(sidecar.relative_to(directory)))
        attributes, embedded = inspect_metadata(sidecar)
        attribute_counts.append(attributes)
        embedded_counts.append(embedded)

    if sidecar_policy == "required" and missing_sidecars:
        raise ValueError(f"{len(missing_sidecars)} documents are missing sidecars")
    if sidecar_policy == "forbidden" and sidecar_count:
        raise ValueError(f"{sidecar_count} documents unexpectedly have sidecars")

    return {
        "directory": str(directory),
        "sidecarPolicy": sidecar_policy,
        "documentCount": len(sources),
        "sidecarCount": sidecar_count,
        "missingSidecarCount": len(missing_sidecars),
        "missingSidecarExamples": missing_sidecars[:20],
        "oversizedSidecars": oversized_sidecars,
        "minimumDocumentBytes": min(sizes),
        "maximumDocumentBytes": max(sizes),
        "meanDocumentBytes": statistics.fmean(sizes),
        "duplicateContentCount": len(hashes) - len(set(hashes)),
        "meanMetadataAttributes": (
            statistics.fmean(attribute_counts) if attribute_counts else 0
        ),
        "meanEmbeddedMetadataAttributes": (
            statistics.fmean(embedded_counts) if embedded_counts else 0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--sidecar-policy",
        choices=("optional", "required", "forbidden"),
        default="optional",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = profile(args.directory, args.sidecar_policy)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Corpus profile failed: {error}", file=sys.stderr)
        return 1

    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
