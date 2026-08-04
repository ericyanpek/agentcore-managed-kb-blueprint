#!/usr/bin/env python3

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


PILLARS = (
    "卓越运营",
    "安全性",
    "可靠性",
    "性能效率",
    "成本优化",
    "可持续性与总结",
)
VARIANTS = ("no-metadata", "filter-metadata", "embedded-metadata")
RERANK_MODES = {
    "managed-rerank": "MANAGED",
    "no-rerank": "NONE",
}
SECTION_LABELS = {"实施指导", "实施步骤", "客户示例", "资源", "设计原则"}


@dataclass(frozen=True)
class Record:
    filename: str
    pillar: str
    topic: str
    question_id: str
    best_practice_id: str
    section_path: str
    subsection: str
    content: str


@dataclass(frozen=True)
class RetrievalCase:
    case_id: str
    category: str
    query: str
    expected_documents: tuple[str, ...]
    weak_label_type: str
    pillar: str
    metadata_signal_absent_rate: float
    runtime_filter: dict | None


NATURAL_CASES = (
    (
        "player-behavior-detection",
        "如何监控和审核玩家使用行为，并检测作弊、欺诈和其他滥用行为？",
        ("GAMESEC05-BP01", "GAMESEC05-BP02", "异常游戏内交易", "可疑通信行为"),
    ),
    (
        "matchmaking-bypass",
        "如何防止玩家绕过配对系统并未经授权加入游戏会话？",
        ("GAMESEC03-BP03", "服务器生成的票证", "玩家会话 ID", "配对服务"),
    ),
    (
        "fraud-detection",
        "游戏账号与交易欺诈检测有哪些策略？请覆盖异常登录、异常交易和虚拟经济。",
        ("欺诈检测", "登录尝试次数", "交易量", "虚拟经济"),
    ),
    (
        "account-takeover",
        "如何通过密码、多因素身份验证和风险场景控制降低玩家账户接管风险？",
        ("GAMESEC03-BP04", "GAMESEC03-BP05", "多重身份验证", "新的地理位置"),
    ),
    (
        "telemetry-analytics",
        "针对玩家行为进行数据分析有哪些最佳实践？如何采集、存储和分析遥测？",
        ("GAMEOPS06-BP01", "游戏遥测数据", "数据湖", "玩家留存率"),
    ),
    (
        "automated-detection",
        "如何使用 AWS 的机器学习能力自动发现作弊、欺诈和协调威胁？",
        ("GAMESEC06-BP02", "Lookout for Metrics", "SageMaker", "机器人网络"),
    ),
    (
        "abuse-response",
        "发现不良行为者和滥用行为后，应如何响应并处置相关账户？",
        ("GAMESEC07-BP01", "GAMESEC07-BP02", "事件响应计划", "封禁"),
    ),
    (
        "behavior-impact",
        "如何关联基础设施故障与玩家行为变化，并据此改进可靠性？",
        ("GAMEREL03-BP03", "玩家行为", "服务器指标", "异常终止"),
    ),
)


def metadata_attribute(payload: dict, name: str) -> str:
    value = payload.get("metadataAttributes", {}).get(name, {}).get("value", {})
    if value.get("type") == "STRING":
        return value.get("stringValue", "")
    return ""


def load_records(corpus_dir: Path) -> list[Record]:
    records = []
    for metadata_path in sorted(corpus_dir.glob("*.metadata.json")):
        filename = metadata_path.name.removesuffix(".metadata.json")
        content_path = corpus_dir / filename
        if not content_path.is_file():
            raise ValueError(f"missing content for sidecar: {metadata_path}")
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        section_path = metadata_attribute(payload, "section_path")
        subsection = section_path.rsplit(" / ", 1)[-1] if section_path else ""
        records.append(
            Record(
                filename=filename,
                pillar=metadata_attribute(payload, "pillar"),
                topic=metadata_attribute(payload, "topic"),
                question_id=metadata_attribute(payload, "question_id"),
                best_practice_id=metadata_attribute(payload, "best_practice_id"),
                section_path=section_path,
                subsection=subsection,
                content=content_path.read_text(encoding="utf-8"),
            )
        )
    if not records:
        raise ValueError("metadata corpus is empty")
    return records


def balanced_select(candidates: list[dict], count: int) -> list[dict]:
    by_pillar = {
        pillar: sorted(
            (item for item in candidates if item["pillar"] == pillar),
            key=lambda item: (item["sortKey"], item["query"]),
        )
        for pillar in PILLARS
    }
    selected = []
    selected_keys = set()
    while len(selected) < count:
        made_progress = False
        for pillar in PILLARS:
            items = by_pillar[pillar]
            while items and items[0]["uniqueKey"] in selected_keys:
                items.pop(0)
            if items and len(selected) < count:
                item = items.pop(0)
                selected.append(item)
                selected_keys.add(item["uniqueKey"])
                made_progress = True
        if not made_progress:
            break
    if len(selected) != count:
        raise ValueError(
            f"could only select {len(selected)} balanced cases; expected {count}"
        )
    return selected


def natural_cases(records: list[Record]) -> list[RetrievalCase]:
    cases = []
    for case_id, query, markers in NATURAL_CASES:
        expected = sorted(
            record.filename
            for record in records
            if any(marker.casefold() in record.content.casefold() for marker in markers)
        )
        if not expected:
            raise ValueError(f"natural case has no weak-label documents: {case_id}")
        cases.append(
            RetrievalCase(
                case_id=f"natural-{case_id}",
                category="natural-business",
                query=query,
                expected_documents=tuple(expected),
                weak_label_type="content-evidence-marker",
                pillar="mixed",
                metadata_signal_absent_rate=0,
                runtime_filter=None,
            )
        )
    return cases


def section_candidates(records: list[Record]) -> list[dict]:
    grouped = defaultdict(list)
    for record in records:
        if record.section_path:
            grouped[record.section_path].append(record)

    candidates = []
    for section_path, section_records in grouped.items():
        first = section_records[0]
        if (
            first.pillar not in PILLARS
            or first.subsection not in SECTION_LABELS
            or not first.topic
            or len(first.topic) < 5
            or len(first.topic) > 70
        ):
            continue
        absent_count = sum(
            first.topic.casefold() not in record.content.casefold()
            and (
                not first.best_practice_id
                or first.best_practice_id.casefold() not in record.content.casefold()
            )
            for record in section_records
        )
        candidates.append(
            {
                "pillar": first.pillar,
                "topic": first.topic,
                "questionId": first.question_id,
                "bestPracticeId": first.best_practice_id,
                "subsection": first.subsection,
                "sectionPath": section_path,
                "expectedDocuments": sorted(
                    record.filename for record in section_records
                ),
                "metadataSignalAbsentRate": absent_count / len(section_records),
                "uniqueKey": section_path,
                "sortKey": section_path,
            }
        )
    return candidates


def generated_section_cases(
    records: list[Record],
    *,
    count_per_category: int,
) -> list[RetrievalCase]:
    candidates = section_candidates(records)
    control_candidates = []
    for item in candidates:
        if (
            item["bestPracticeId"]
            and item["subsection"] in {"实施步骤", "客户示例", "资源"}
            and item["metadataSignalAbsentRate"] >= 0.75
        ):
            control_candidates.append(
                {
                    **item,
                    "query": (
                        f"请定位 {item['bestPracticeId']} 的"
                        f"“{item['subsection']}”章节，并给出其中的具体内容。"
                    ),
                }
            )
    selected_control = balanced_select(control_candidates, count_per_category)
    used_sections = {item["sectionPath"] for item in selected_control}

    topic_candidates = []
    for item in candidates:
        if (
            item["sectionPath"] not in used_sections
            and item["subsection"] in {"实施指导", "客户示例", "资源", "设计原则"}
            and item["metadataSignalAbsentRate"] >= 0.75
        ):
            topic_candidates.append(
                {
                    **item,
                    "query": (
                        f"在“{item['topic']}”主题下，"
                        f"“{item['subsection']}”部分具体说明了什么？"
                    ),
                }
            )
    selected_topic = balanced_select(topic_candidates, count_per_category)

    cases = []
    for category, selected in (
        ("control-subsection", selected_control),
        ("topic-subsection", selected_topic),
    ):
        for index, item in enumerate(selected, start=1):
            cases.append(
                RetrievalCase(
                    case_id=f"{category}-{index:02d}",
                    category=category,
                    query=item["query"],
                    expected_documents=tuple(item["expectedDocuments"]),
                    weak_label_type="exact-section-path",
                    pillar=item["pillar"],
                    metadata_signal_absent_rate=item["metadataSignalAbsentRate"],
                    runtime_filter={
                        "equals": {
                            "key": (
                                "best_practice_id"
                                if category == "control-subsection"
                                else "topic"
                            ),
                            "value": (
                                item["bestPracticeId"]
                                if category == "control-subsection"
                                else item["topic"]
                            ),
                        }
                    },
                )
            )
    return cases


def question_cases(
    records: list[Record],
    *,
    count: int,
) -> list[RetrievalCase]:
    grouped = defaultdict(list)
    for record in records:
        if record.question_id:
            grouped[record.question_id].append(record)

    candidates = []
    for question_id, question_records in grouped.items():
        pillar = Counter(
            record.pillar for record in question_records if record.pillar in PILLARS
        ).most_common(1)
        topics = Counter(record.topic for record in question_records if record.topic)
        if not pillar or not topics:
            continue
        topic = topics.most_common(1)[0][0]
        if len(topic) < 5 or len(topic) > 70:
            continue
        candidates.append(
            {
                "pillar": pillar[0][0],
                "query": (
                    f"请查找 {question_id}“{topic}”对应的问题概述和最佳实践。"
                ),
                "expectedDocuments": sorted(
                    record.filename for record in question_records
                ),
                "metadataSignalAbsentRate": statistics.fmean(
                    question_id.casefold() not in record.content.casefold()
                    and topic.casefold() not in record.content.casefold()
                    for record in question_records
                ),
                "uniqueKey": question_id,
                "sortKey": question_id,
                "questionId": question_id,
            }
        )
    selected = balanced_select(candidates, count)
    return [
        RetrievalCase(
            case_id=f"question-lookup-{index:02d}",
            category="question-lookup",
            query=item["query"],
            expected_documents=tuple(item["expectedDocuments"]),
            weak_label_type="exact-question-id",
            pillar=item["pillar"],
            metadata_signal_absent_rate=item["metadataSignalAbsentRate"],
            runtime_filter={
                "equals": {
                    "key": "question_id",
                    "value": item["questionId"],
                }
            },
        )
        for index, item in enumerate(selected, start=1)
    ]


def build_query_set(records: list[Record], count_per_category: int) -> list[RetrievalCase]:
    cases = natural_cases(records)
    cases.extend(
        generated_section_cases(
            records,
            count_per_category=count_per_category,
        )
    )
    cases.extend(question_cases(records, count=count_per_category))
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("generated query set contains duplicate case IDs")
    return cases


def query_set_payload(cases: list[RetrievalCase], content_set_sha256: str) -> dict:
    payload = {
        "schemaVersion": 1,
        "contentSetSha256": content_set_sha256,
        "caseCount": len(cases),
        "categories": dict(Counter(case.category for case in cases)),
        "cases": [
            {
                "caseId": case.case_id,
                "category": case.category,
                "query": case.query,
                "expectedDocuments": list(case.expected_documents),
                "weakLabelType": case.weak_label_type,
                "pillar": case.pillar,
                "metadataSignalAbsentRate": case.metadata_signal_absent_rate,
                "runtimeFilter": case.runtime_filter,
            }
            for case in cases
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["querySetSha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def create_client(region: str):
    return boto3.client(
        "bedrock-agent-runtime",
        region_name=region,
        config=Config(
            retries={"total_max_attempts": 5, "mode": "adaptive"},
            connect_timeout=10,
            read_timeout=120,
        ),
    )


def retrieve(
    client,
    *,
    knowledge_base_id: str,
    data_source_id: str,
    query: str,
    number_of_results: int,
    reranking_model_type: str,
    extra_filter: dict | None = None,
) -> tuple[dict, float]:
    data_source_filter = {
        "equals": {
            "key": "_data_source_id",
            "value": data_source_id,
        }
    }
    retrieval_filter = (
        data_source_filter
        if extra_filter is None
        else {"andAll": [data_source_filter, extra_filter]}
    )
    started = time.perf_counter()
    response = client.retrieve(
        knowledgeBaseId=knowledge_base_id,
        retrievalQuery={"text": query, "type": "TEXT"},
        retrievalConfiguration={
            "managedSearchConfiguration": {
                "numberOfResults": number_of_results,
                "rerankingModelType": reranking_model_type,
                "filter": retrieval_filter,
            }
        },
    )
    return response, (time.perf_counter() - started) * 1000


def ndcg_at_k(relevant: list[bool], target_count: int, k: int) -> float:
    dcg = sum(
        (1 / math.log2(rank + 1)) if is_relevant else 0
        for rank, is_relevant in enumerate(relevant[:k], start=1)
    )
    ideal_count = min(target_count, k)
    idcg = sum(1 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return dcg / idcg if idcg else 0


def score_response(
    case: RetrievalCase,
    response: dict,
    latency_ms: float,
    number_of_results: int,
) -> dict:
    results = response.get("retrievalResults", [])
    titles = [
        item.get("metadata", {}).get("_document_title", "") for item in results
    ]
    expected = set(case.expected_documents)
    relevant = [title in expected for title in titles]
    relevant_ranks = [
        rank for rank, is_relevant in enumerate(relevant, start=1) if is_relevant
    ]
    unique_relevant = expected.intersection(titles)
    top_result = results[0] if results else {}
    return {
        "caseId": case.case_id,
        "category": case.category,
        "query": case.query,
        "weakLabelType": case.weak_label_type,
        "pillar": case.pillar,
        "metadataSignalAbsentRate": case.metadata_signal_absent_rate,
        "expectedDocumentCount": len(expected),
        "resultCount": len(results),
        "latencyMs": latency_ms,
        "topScore": top_result.get("score"),
        "hitAt1": bool(relevant[:1] and relevant[0]),
        "hitAt3": any(relevant[:3]),
        "hitAt10": any(relevant[:number_of_results]),
        "firstRelevantRank": relevant_ranks[0] if relevant_ranks else None,
        "reciprocalRank": 1 / relevant_ranks[0] if relevant_ranks else 0,
        "recallAt10": len(unique_relevant) / len(expected),
        "nDcgAt10": ndcg_at_k(relevant, len(expected), number_of_results),
        "relevantResultCount": sum(relevant),
        "duplicateResultCount": len(titles) - len(set(titles)),
        "retrievedDocumentTitles": titles,
        "topPreview": top_result.get("content", {}).get("text", "")[:240],
    }


def summarize_cases(cases: list[dict]) -> dict:
    top_scores = [case["topScore"] for case in cases if case["topScore"] is not None]
    return {
        "caseCount": len(cases),
        "hitRateAt1": statistics.fmean(case["hitAt1"] for case in cases),
        "hitRateAt3": statistics.fmean(case["hitAt3"] for case in cases),
        "hitRateAt10": statistics.fmean(case["hitAt10"] for case in cases),
        "meanReciprocalRank": statistics.fmean(
            case["reciprocalRank"] for case in cases
        ),
        "meanRecallAt10": statistics.fmean(case["recallAt10"] for case in cases),
        "meanNdcgAt10": statistics.fmean(case["nDcgAt10"] for case in cases),
        "meanRelevantResultsAt10": statistics.fmean(
            case["relevantResultCount"] for case in cases
        ),
        "meanDuplicateResultsAt10": statistics.fmean(
            case["duplicateResultCount"] for case in cases
        ),
        "meanTopScore": statistics.fmean(top_scores) if top_scores else None,
        "meanLatencyMs": statistics.fmean(case["latencyMs"] for case in cases),
        "medianLatencyMs": statistics.median(case["latencyMs"] for case in cases),
    }


def summarize_variant(name: str, cases: list[dict]) -> dict:
    categories = sorted({case["category"] for case in cases})
    return {
        "variant": name,
        **summarize_cases(cases),
        "categories": {
            category: summarize_cases(
                [case for case in cases if case["category"] == category]
            )
            for category in categories
        },
        "cases": cases,
    }


def percentile(sorted_values: list[float], probability: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return (
        sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction
    )


def paired_metric(
    baseline_cases: list[dict],
    experiment_cases: list[dict],
    metric: str,
    *,
    seed: int,
    bootstrap_samples: int = 5000,
) -> dict:
    baseline = {case["caseId"]: case[metric] for case in baseline_cases}
    experiment = {case["caseId"]: case[metric] for case in experiment_cases}
    if baseline.keys() != experiment.keys():
        raise ValueError(f"case mismatch for paired metric: {metric}")
    deltas = [
        experiment[case_id] - baseline[case_id] for case_id in sorted(baseline)
    ]
    rng = random.Random(seed)
    sampled_means = sorted(
        statistics.fmean(rng.choice(deltas) for _ in deltas)
        for _ in range(bootstrap_samples)
    )
    tolerance = 1e-12
    return {
        "metric": metric,
        "meanDelta": statistics.fmean(deltas),
        "bootstrap95Ci": [
            percentile(sampled_means, 0.025),
            percentile(sampled_means, 0.975),
        ],
        "improvedCaseCount": sum(delta > tolerance for delta in deltas),
        "tiedCaseCount": sum(abs(delta) <= tolerance for delta in deltas),
        "regressedCaseCount": sum(delta < -tolerance for delta in deltas),
    }


def paired_comparison(baseline: dict, experiment: dict, seed: int) -> dict:
    metrics = ("hitAt1", "reciprocalRank", "recallAt10", "nDcgAt10")
    return {
        metric: paired_metric(
            baseline["cases"],
            experiment["cases"],
            metric,
            seed=seed + index,
        )
        for index, metric in enumerate(metrics)
    }


def format_delta(value: float) -> str:
    return f"{value:+.3f}"


def build_markdown(summary: dict) -> str:
    lines = [
        "# Metadata 扩展召回实验",
        "",
        f"- Query Set：`{summary['querySetSha256']}`",
        f"- 查询数：{summary['queryCount']}",
        f"- Top-K：{summary['numberOfResults']}",
        "- 变体隔离：系统 `_data_source_id`",
        "",
    ]
    for mode_name, mode in summary["modes"].items():
        lines.extend(
            [
                f"## {mode_name}",
                "",
                "| Variant | Hit@1 | Hit@10 | MRR | Recall@10 | nDCG@10 | Relevant@10 |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for variant_name in VARIANTS:
            variant = mode["variants"][variant_name]
            lines.append(
                f"| {variant_name} | {variant['hitRateAt1']:.3f} | "
                f"{variant['hitRateAt10']:.3f} | "
                f"{variant['meanReciprocalRank']:.3f} | "
                f"{variant['meanRecallAt10']:.3f} | "
                f"{variant['meanNdcgAt10']:.3f} | "
                f"{variant['meanRelevantResultsAt10']:.2f} |"
            )

        comparison = mode["comparisons"]["no-metadata-to-embedded"]
        lines.extend(
            [
                "",
                "| Embedded 相对 No Metadata | 均值差 | 95% Bootstrap CI | 改善/持平/退化 |",
                "| --- | ---: | --- | ---: |",
            ]
        )
        for label, metric in (
            ("Hit@1", "hitAt1"),
            ("MRR", "reciprocalRank"),
            ("Recall@10", "recallAt10"),
            ("nDCG@10", "nDcgAt10"),
        ):
            result = comparison[metric]
            ci = result["bootstrap95Ci"]
            lines.append(
                f"| {label} | {format_delta(result['meanDelta'])} | "
                f"[{ci[0]:+.3f}, {ci[1]:+.3f}] | "
                f"{result['improvedCaseCount']}/"
                f"{result['tiedCaseCount']}/"
                f"{result['regressedCaseCount']} |"
            )

        lines.extend(
            [
                "",
                "| Category | No Metadata MRR | Embedded MRR | Delta | "
                "No Metadata Recall@10 | Embedded Recall@10 | Delta |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        baseline_categories = mode["variants"]["no-metadata"]["categories"]
        embedded_categories = mode["variants"]["embedded-metadata"]["categories"]
        for category in sorted(baseline_categories):
            baseline = baseline_categories[category]
            embedded = embedded_categories[category]
            lines.append(
                f"| {category} | {baseline['meanReciprocalRank']:.3f} | "
                f"{embedded['meanReciprocalRank']:.3f} | "
                f"{format_delta(embedded['meanReciprocalRank'] - baseline['meanReciprocalRank'])} | "
                f"{baseline['meanRecallAt10']:.3f} | "
                f"{embedded['meanRecallAt10']:.3f} | "
                f"{format_delta(embedded['meanRecallAt10'] - baseline['meanRecallAt10'])} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Runtime Metadata Filter",
            "",
            "仅统计 36 条可从 Query 确定 `best_practice_id`、`topic` 或 "
            "`question_id` 的生成用例。",
            "",
            "| Mode | Unfiltered MRR | Filtered MRR | Delta | "
            "Unfiltered Recall@10 | Filtered Recall@10 | Delta |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for mode_name, variants in summary["runtimeFilterEvaluation"].items():
        for variant_name, evaluation in variants.items():
            baseline = evaluation["unfiltered"]
            filtered = evaluation["filtered"]
            lines.append(
                f"| {mode_name} / {variant_name} | "
                f"{baseline['meanReciprocalRank']:.3f} | "
                f"{filtered['meanReciprocalRank']:.3f} | "
                f"{format_delta(filtered['meanReciprocalRank'] - baseline['meanReciprocalRank'])} | "
                f"{baseline['meanRecallAt10']:.3f} | "
                f"{filtered['meanRecallAt10']:.3f} | "
                f"{format_delta(filtered['meanRecallAt10'] - baseline['meanRecallAt10'])} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--no-metadata-data-source-id", required=True)
    parser.add_argument("--filter-metadata-data-source-id", required=True)
    parser.add_argument("--embedded-metadata-data-source-id", required=True)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--preparation-report", type=Path, required=True)
    parser.add_argument("--number-of-results", type=int, default=10)
    parser.add_argument("--count-per-generated-category", type=int, default=12)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generate-only", action="store_true")
    parser.add_argument("--runtime-filter-only", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        records = load_records(args.corpus_dir)
        preparation = json.loads(
            args.preparation_report.read_text(encoding="utf-8")
        )
        cases = build_query_set(records, args.count_per_generated_category)
        query_payload = query_set_payload(cases, preparation["contentSetSha256"])
        query_set_path = args.output_dir / "metadata-expanded-query-set.json"
        query_set_path.write_text(
            json.dumps(query_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if args.generate_only:
            print(json.dumps(query_payload, ensure_ascii=False, indent=2))
            return 0

        data_source_ids = {
            "no-metadata": args.no_metadata_data_source_id,
            "filter-metadata": args.filter_metadata_data_source_id,
            "embedded-metadata": args.embedded_metadata_data_source_id,
        }
        client = create_client(args.region)
        summary_path = args.output_dir / "metadata-expanded-comparison.json"
        if args.runtime_filter_only:
            existing_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if existing_summary.get("querySetSha256") != query_payload["querySetSha256"]:
                raise ValueError(
                    "existing summary query set does not match the generated query set"
                )
            modes = existing_summary["modes"]
        else:
            modes = {}
            for mode_index, (mode_name, reranking_type) in enumerate(
                RERANK_MODES.items()
            ):
                scored_by_variant = {variant: [] for variant in VARIANTS}
                for case in cases:
                    for variant in VARIANTS:
                        response, latency_ms = retrieve(
                            client,
                            knowledge_base_id=args.knowledge_base_id,
                            data_source_id=data_source_ids[variant],
                            query=case.query,
                            number_of_results=args.number_of_results,
                            reranking_model_type=reranking_type,
                        )
                        raw_path = args.output_dir / (
                            f"metadata-expanded-{mode_name}-{variant}-{case.case_id}.json"
                        )
                        raw_path.write_text(
                            json.dumps(
                                response,
                                ensure_ascii=False,
                                indent=2,
                                default=str,
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                        scored_by_variant[variant].append(
                            score_response(
                                case,
                                response,
                                latency_ms,
                                args.number_of_results,
                            )
                        )

                variant_summaries = {
                    variant: summarize_variant(
                        variant,
                        scored_by_variant[variant],
                    )
                    for variant in VARIANTS
                }
                modes[mode_name] = {
                    "rerankingModelType": reranking_type,
                    "variants": variant_summaries,
                    "comparisons": {
                        "no-metadata-to-embedded": paired_comparison(
                            variant_summaries["no-metadata"],
                            variant_summaries["embedded-metadata"],
                            seed=20260804 + mode_index * 100,
                        ),
                        "filter-metadata-to-embedded": paired_comparison(
                            variant_summaries["filter-metadata"],
                            variant_summaries["embedded-metadata"],
                            seed=20260854 + mode_index * 100,
                        ),
                    },
                }

        filterable_cases = [case for case in cases if case.runtime_filter is not None]
        runtime_filter_evaluation = {}
        for mode_index, (mode_name, reranking_type) in enumerate(
            RERANK_MODES.items()
        ):
            runtime_filter_evaluation[mode_name] = {}
            for variant_index, variant in enumerate(
                ("filter-metadata", "embedded-metadata")
            ):
                filtered_cases = []
                for case in filterable_cases:
                    response, latency_ms = retrieve(
                        client,
                        knowledge_base_id=args.knowledge_base_id,
                        data_source_id=data_source_ids[variant],
                        query=case.query,
                        number_of_results=args.number_of_results,
                        reranking_model_type=reranking_type,
                        extra_filter=case.runtime_filter,
                    )
                    raw_path = args.output_dir / (
                        f"metadata-runtime-filter-{mode_name}-{variant}-{case.case_id}.json"
                    )
                    raw_path.write_text(
                        json.dumps(
                            response,
                            ensure_ascii=False,
                            indent=2,
                            default=str,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    filtered_cases.append(
                        score_response(
                            case,
                            response,
                            latency_ms,
                            args.number_of_results,
                        )
                    )

                unfiltered_cases = [
                    case
                    for case in modes[mode_name]["variants"][variant]["cases"]
                    if case["category"] != "natural-business"
                ]
                unfiltered_summary = summarize_variant(
                    f"{variant}-unfiltered",
                    unfiltered_cases,
                )
                filtered_summary = summarize_variant(
                    f"{variant}-with-runtime-filter",
                    filtered_cases,
                )
                runtime_filter_evaluation[mode_name][variant] = {
                    "unfiltered": unfiltered_summary,
                    "filtered": filtered_summary,
                    "comparison": paired_comparison(
                        unfiltered_summary,
                        filtered_summary,
                        seed=20260904 + mode_index * 100 + variant_index * 10,
                    ),
                }
    except (ClientError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"Expanded metadata retrieval experiment failed: {error}", file=sys.stderr)
        return 1

    summary = {
        "querySetSha256": query_payload["querySetSha256"],
        "contentSetSha256": query_payload["contentSetSha256"],
        "queryCount": len(cases),
        "categories": query_payload["categories"],
        "numberOfResults": args.number_of_results,
        "retrievalConfiguration": {
            "search": "MANAGED",
            "variantIsolation": "_data_source_id",
            "modes": RERANK_MODES,
        },
        "modes": modes,
        "runtimeFilterEvaluation": runtime_filter_evaluation,
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path = args.output_dir / "metadata-expanded-comparison.md"
    markdown_path.write_text(
        build_markdown(summary),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "querySetSha256": summary["querySetSha256"],
                "queryCount": summary["queryCount"],
                "summaryPath": str(summary_path),
                "markdownPath": str(markdown_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
