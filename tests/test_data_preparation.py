import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


extract_pdf = load_module("extract_pdf", "scripts/09_extract_pdf_to_markdown.py")
semantic = load_module("semantic", "scripts/14_prepare_semantic_chunks.py")
metadata = load_module("metadata", "scripts/17_prepare_metadata_experiment.py")
compare_semantic = load_module(
    "compare_semantic",
    "scripts/16_compare_semantic_chunking.py",
)
expanded = load_module("expanded", "scripts/20_expand_metadata_retrieval.py")
profiler = load_module(
    "profiler",
    ".agents/skills/kb-rag-data-preparation/scripts/profile_corpus.py",
)


class DataPreparationTests(unittest.TestCase):
    def test_pdf_page_cleanup_removes_repeated_header_and_footer(self):
        source = "游戏行业视角 AWS 白皮书\n\n正文内容\n\n12"
        self.assertEqual(extract_pdf.clean_page_text(source, 2), "正文内容")

    def test_metadata_embedding_policy_only_embeds_semantic_fields(self):
        source_attributes = {
            "section_path": metadata.metadata_value(
                "安全性 / 玩家行为 / GAMESEC05 / GAMESEC05-BP01 / 实施指导",
                include_for_embedding=True,
            ),
            "source_page_start": metadata.metadata_value(
                72,
                include_for_embedding=False,
            ),
            "source_page_end": metadata.metadata_value(
                73,
                include_for_embedding=False,
            ),
        }
        filter_only = metadata.build_metadata(
            source_attributes=source_attributes,
            variant="filter-metadata",
            chunk_id="chunk-1",
            content_sha256="abc",
        )["metadataAttributes"]
        embedded = metadata.build_metadata(
            source_attributes=source_attributes,
            variant="embedded-metadata",
            chunk_id="chunk-1",
            content_sha256="abc",
        )["metadataAttributes"]

        self.assertFalse(filter_only["topic"]["includeForEmbedding"])
        self.assertTrue(embedded["topic"]["includeForEmbedding"])
        self.assertFalse(embedded["classification"]["includeForEmbedding"])
        self.assertFalse(embedded["content_sha256"]["includeForEmbedding"])

    def test_empty_retrieval_scores_are_reported_without_failure(self):
        case = {
            "hit": False,
            "markerCoverage": 0.0,
            "reciprocalRank": 0.0,
            "relevantResultCount": 0,
            "topScore": None,
            "latencyMs": 1.0,
        }
        summary = compare_semantic.summarize_variant("empty", [case])
        self.assertIsNone(summary["meanTopScore"])

    def test_paired_metric_is_deterministic(self):
        baseline = [{"caseId": "a", "reciprocalRank": 0.0}]
        experiment = [{"caseId": "a", "reciprocalRank": 1.0}]
        result = expanded.paired_metric(
            baseline,
            experiment,
            "reciprocalRank",
            seed=7,
            bootstrap_samples=20,
        )
        self.assertEqual(result["meanDelta"], 1.0)
        self.assertEqual(result["bootstrap95Ci"], [1.0, 1.0])
        self.assertEqual(result["improvedCaseCount"], 1)

    def test_corpus_profiler_validates_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory)
            (corpus / "doc.md").write_text("content\n", encoding="utf-8")
            sidecar = {
                "metadataAttributes": {
                    "classification": {
                        "value": {"type": "STRING", "stringValue": "PUBLIC"},
                        "includeForEmbedding": False,
                    }
                }
            }
            (corpus / "doc.md.metadata.json").write_text(
                json.dumps(sidecar),
                encoding="utf-8",
            )
            report = profiler.profile(corpus, "required")
            self.assertEqual(report["documentCount"], 1)
            self.assertEqual(report["sidecarCount"], 1)
            self.assertEqual(report["duplicateContentCount"], 0)

    def test_long_blocks_respect_the_maximum_size(self):
        parts = semantic.split_long_block("甲" * 25, max_chars=10)
        self.assertEqual([len(part) for part in parts], [10, 10, 5])


if __name__ == "__main__":
    unittest.main()
