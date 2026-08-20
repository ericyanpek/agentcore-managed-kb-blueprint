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
        ({"broken.md": "# Title\n\nbad � char\n"}, "U\\+FFFD"),
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


def test_byte_order_mark_does_not_suppress_front_matter(tmp_path):
    """A BOM must not silently defeat front matter parsing.

    Plain utf-8 decoding keeps the BOM as U+FEFF, which stops the front matter
    and heading patterns from matching. The document still processes, so the
    failure is silent: the id falls back to the filename and governance fields
    revert to their defaults.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "doc.md").write_bytes(
        b"\xef\xbb\xbf---\ndocument_id: custom-id\nclassification: PUBLIC\n---\n"
        b"# My Report\n\nBody.\n"
    )

    manifest = corpus.prepare(
        source_dir=source,
        output_dir=tmp_path / "canonical",
        corpus_id="demo",
        embedded_fields=("title",),
    )

    assert manifest["documents"][0]["documentId"] == "custom-id"
    attributes = json.loads(
        (tmp_path / "canonical" / "doc.md.metadata.json").read_text(encoding="utf-8")
    )["metadataAttributes"]
    assert attributes["classification"]["value"]["stringValue"] == "PUBLIC"
    assert attributes["title"]["value"]["stringValue"] == "My Report"


@pytest.mark.parametrize("raw_value", ["2026-08-01", "20260801"])
def test_full_dates_normalize_to_one_numeric_form(tmp_path, raw_value):
    source = write_corpus(
        tmp_path, {"doc.md": f"---\nversion_date: {raw_value}\n---\n# T\n\nBody.\n"}
    )

    corpus.prepare(
        source_dir=source,
        output_dir=tmp_path / "canonical",
        corpus_id="demo",
        embedded_fields=("title",),
    )

    attributes = json.loads(
        (tmp_path / "canonical" / "doc.md.metadata.json").read_text(encoding="utf-8")
    )["metadataAttributes"]
    assert attributes["version_date"]["value"] == {
        "type": "NUMBER",
        "numberValue": 20260801,
    }


@pytest.mark.parametrize("raw_value", ["unknown", "2026-08", "Q1 2026", "2026"])
def test_partial_or_unparseable_dates_are_rejected(tmp_path, raw_value):
    """Mixed types on one field break metadata filters, so reject at preparation.

    A partial date is worse than an error: `2026-08` would become 202608, which
    compares below every YYYYMMDD value in the same field.
    """
    source = write_corpus(
        tmp_path, {"doc.md": f"---\nversion_date: {raw_value}\n---\n# T\n\nBody.\n"}
    )

    with pytest.raises(ValueError, match="must be a full date"):
        corpus.prepare(
            source_dir=source,
            output_dir=tmp_path / "canonical",
            corpus_id="demo",
            embedded_fields=("title",),
        )


def test_corpus_sha256_is_stable_and_sensitive(tmp_path):
    source = write_corpus(tmp_path, {"doc.md": "# Title\n\nBody.\n"})
    kwargs = {"corpus_id": "demo", "embedded_fields": ("title",)}

    first = corpus.prepare(source_dir=source, output_dir=tmp_path / "a", **kwargs)
    repeated = corpus.prepare(source_dir=source, output_dir=tmp_path / "b", **kwargs)
    assert first["corpusSha256"] == repeated["corpusSha256"]

    (source / "doc.md").write_text("# Title\n\nChanged.\n", encoding="utf-8")
    changed = corpus.prepare(source_dir=source, output_dir=tmp_path / "c", **kwargs)
    assert changed["corpusSha256"] != first["corpusSha256"]
