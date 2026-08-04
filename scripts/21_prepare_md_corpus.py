#!/usr/bin/env python3

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path


REPLACEMENT_CHARACTER = "�"
MAX_EXTRACTED_TEXT_BYTES = 30 * 1024 * 1024
MAX_SIDECAR_BYTES = 10 * 1024
FRONT_MATTER_PATTERN = re.compile(r"\A---\n(.*?)\n---\n", flags=re.DOTALL)
HEADING_PATTERN = re.compile(r"^#\s+(.+)$", flags=re.MULTILINE)


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


def document_id(relative_path: Path) -> str:
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
    doc_id: str,
    corpus_id: str,
    front_matter: dict[str, str],
    title: str,
    relative_path: Path,
    content_sha256: str,
    embedded_fields: set[str],
) -> dict:
    section_path = relative_path.parent.as_posix()
    if section_path == ".":
        section_path = ""
    section_parts = [part for part in section_path.split("/") if part]

    values: dict[str, str | int] = {
        "document_id": doc_id,
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
            value, include_for_embedding=name in embedded_fields
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


def prepare(args: argparse.Namespace) -> dict:
    source_paths = sorted(
        path
        for path in args.source_dir.rglob("*.md")
        if path.is_file() and not path.name.endswith(".metadata.json")
    )
    if not source_paths:
        raise ValueError(f"no Markdown documents found under {args.source_dir}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    embedded_fields = {
        field.strip() for field in args.embedded_fields.split(",") if field.strip()
    }

    documents = []
    seen_ids: dict[str, Path] = {}
    for source_path in source_paths:
        relative_path = source_path.relative_to(args.source_dir)
        markdown = source_path.read_text(encoding="utf-8")
        front_matter, body = parse_front_matter(markdown)
        content = body.strip() + "\n"
        content_bytes = content.encode("utf-8")
        assert_quality(
            relative_path=relative_path, body=body, content_bytes=content_bytes
        )

        doc_id = front_matter.get("document_id") or document_id(relative_path)
        if doc_id in seen_ids:
            raise ValueError(
                f"duplicate document id {doc_id}: {seen_ids[doc_id]} and {relative_path}"
            )
        seen_ids[doc_id] = relative_path

        destination = args.output_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content_bytes)

        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        metadata = build_metadata(
            doc_id=doc_id,
            corpus_id=args.corpus_id,
            front_matter=front_matter,
            title=derive_title(front_matter, body, relative_path),
            relative_path=relative_path,
            content_sha256=content_sha256,
            embedded_fields=embedded_fields,
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
                "documentId": doc_id,
                "file": relative_path.as_posix(),
                "contentSha256": content_sha256,
                "metadataSha256": file_sha256(metadata_path),
                "contentBytes": len(content_bytes),
                "sidecarBytes": sidecar_bytes,
            }
        )

    manifest = {
        "corpusId": args.corpus_id,
        "contentFormat": "authored-markdown",
        "embeddedMetadataFields": sorted(embedded_fields),
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
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def diff_manifests(previous: dict, current: dict) -> dict:
    previous_documents = {item["file"]: item for item in previous.get("documents", [])}
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize an authored Markdown corpus and emit a change manifest."
    )
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--previous-manifest",
        type=Path,
        help="Manifest from the last published run; enables change detection.",
    )
    parser.add_argument(
        "--embedded-fields",
        default="title,section_path,domain,topic",
        help="Comma-separated metadata fields to include in embeddings.",
    )
    args = parser.parse_args()

    try:
        manifest = prepare(args)
        if args.previous_manifest and args.previous_manifest.is_file():
            previous = json.loads(args.previous_manifest.read_text(encoding="utf-8"))
            changes = diff_manifests(previous, manifest)
            baseline = "previous-manifest"
        else:
            changes = {
                "added": manifest["documents"],
                "modified": [],
                "deleted": [],
            }
            baseline = "initial-load"

        report = {
            "corpusId": manifest["corpusId"],
            "documentCount": manifest["documentCount"],
            "totalContentBytes": manifest["totalContentBytes"],
            "corpusSha256": manifest["corpusSha256"],
            "embeddedMetadataFields": manifest["embeddedMetadataFields"],
            "changeBaseline": baseline,
            "changeCounts": {
                name: len(items) for name, items in changes.items()
            },
            "changes": changes,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"Markdown corpus preparation failed: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "changes"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
