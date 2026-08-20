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

DATE_FIELDS = ("version_date", "effective_date", "expires_on")
FULL_DATE_PATTERN = re.compile(r"\A(\d{4})-?(\d{2})-?(\d{2})\Z")

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


def parse_date_field(name: str, raw_value: str, *, relative_path: Path) -> int:
    """Normalize a date field to YYYYMMDD.

    Rejecting anything else is deliberate. The previous behavior stripped
    non-digits and fell back to the raw string, so `2026-08` became 202608 and
    `unknown` stayed a string. That produced two different metadata types for
    one field across a corpus, which breaks Bedrock metadata filters, and made
    numeric range comparisons wrong because 202608 sorts below 20260801.
    """
    match = FULL_DATE_PATTERN.match(raw_value.strip())
    if match is None:
        raise ValueError(
            f"{name} must be a full date as YYYY-MM-DD or YYYYMMDD, "
            f"got {raw_value!r} in {relative_path}"
        )
    return int("".join(match.groups()))


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
    for optional_name in DATE_FIELDS:
        raw_value = front_matter.get(optional_name, "")
        if raw_value:
            values[optional_name] = parse_date_field(
                optional_name, raw_value, relative_path=relative_path
            )

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
        # utf-8-sig strips a leading BOM. Plain utf-8 keeps it as U+FEFF, which
        # silently defeats the front matter and heading patterns: the document
        # still processes, but with a filename-derived id and no governance
        # fields.
        markdown = source_path.read_text(encoding="utf-8-sig")
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
        sidecar = (
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        metadata_path.write_bytes(sidecar)
        if len(sidecar) > MAX_SIDECAR_BYTES:
            raise ValueError(f"metadata sidecar exceeds 10 KB: {metadata_path}")

        documents.append(
            {
                "documentId": document_id,
                "file": relative_path.as_posix(),
                "contentSha256": content_sha256,
                "metadataSha256": hashlib.sha256(sidecar).hexdigest(),
                "contentBytes": len(content_bytes),
                "sidecarBytes": len(sidecar),
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
