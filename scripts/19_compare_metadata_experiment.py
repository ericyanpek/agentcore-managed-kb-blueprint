#!/usr/bin/env python3

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


EXPECTED_USER_METADATA = {
    "document_id",
    "corpus_id",
    "experiment_variant",
    "title",
    "domain",
    "language",
    "classification",
    "version_date",
    "lifecycle_status",
    "owner",
    "content_format",
    "pillar",
    "topic",
    "section_path",
    "source_page_start",
    "source_page_end",
    "source_pdf_sha256",
    "content_sha256",
}


@dataclass(frozen=True)
class QueryCase:
    case_id: str
    query: str
    evidence_markers: tuple[str, ...]


QUERY_CASES = (
    QueryCase(
        "player-behavior-detection",
        "如何监控和审核玩家使用行为，并检测作弊、欺诈和其他滥用行为？",
        ("GAMESEC05-BP01", "GAMESEC05-BP02", "异常游戏内交易", "可疑通信行为"),
    ),
    QueryCase(
        "matchmaking-bypass",
        "如何防止玩家绕过配对系统并未经授权加入游戏会话？",
        ("GAMESEC03-BP03", "服务器生成的票证", "玩家会话 ID", "配对服务"),
    ),
    QueryCase(
        "fraud-detection",
        "游戏账号与交易欺诈检测有哪些策略？请覆盖异常登录、异常交易和虚拟经济。",
        ("欺诈检测", "登录尝试次数", "交易量", "虚拟经济"),
    ),
    QueryCase(
        "account-takeover",
        "如何通过密码、多因素身份验证和风险场景控制降低玩家账户接管风险？",
        ("GAMESEC03-BP04", "GAMESEC03-BP05", "多重身份验证", "新的地理位置"),
    ),
    QueryCase(
        "telemetry-analytics",
        "针对玩家行为进行数据分析有哪些最佳实践？如何采集、存储和分析遥测？",
        ("GAMEOPS06-BP01", "游戏遥测数据", "数据湖", "玩家留存率"),
    ),
    QueryCase(
        "automated-detection",
        "如何使用 AWS 的机器学习能力自动发现作弊、欺诈和协调威胁？",
        ("GAMESEC06-BP02", "Lookout for Metrics", "SageMaker", "机器人网络"),
    ),
    QueryCase(
        "abuse-response",
        "发现不良行为者和滥用行为后，应如何响应并处置相关账户？",
        ("GAMESEC07-BP01", "GAMESEC07-BP02", "事件响应计划", "封禁"),
    ),
    QueryCase(
        "behavior-impact",
        "如何关联基础设施故障与玩家行为变化，并据此改进可靠性？",
        ("GAMEREL03-BP03", "玩家行为", "服务器指标", "异常终止"),
    ),
)


FILTER_CASES = (
    (
        "pillar",
        "欺诈检测与玩家行为监控有哪些最佳实践？",
        {"equals": {"key": "pillar", "value": "安全性"}},
        "pillar",
        "安全性",
    ),
    (
        "topic",
        "如何监控和分析玩家的使用行为？",
        {
            "equals": {
                "key": "topic",
                "value": "如何监控和分析游戏中玩家的使用行为？",
            }
        },
        "topic",
        "如何监控和分析游戏中玩家的使用行为？",
    ),
    (
        "best-practice",
        "应该收集哪些玩家行为数据来检测作弊和欺诈？",
        {
            "equals": {
                "key": "best_practice_id",
                "value": "GAMESEC05-BP01",
            }
        },
        "best_practice_id",
        "GAMESEC05-BP01",
    ),
    (
        "governance",
        "当前公开版本中关于玩家安全分析的要求是什么？",
        {
            "andAll": [
                {"equals": {"key": "classification", "value": "PUBLIC"}},
                {"equals": {"key": "lifecycle_status", "value": "ACTIVE"}},
                {
                    "greaterThanOrEquals": {
                        "key": "version_date",
                        "value": 20260731,
                    }
                },
            ]
        },
        "classification",
        "PUBLIC",
    ),
)


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


def combine_filters(data_source_id: str, extra_filter: dict | None = None) -> dict:
    data_source_filter = {
        "equals": {"key": "_data_source_id", "value": data_source_id}
    }
    if extra_filter is None:
        return data_source_filter
    return {"andAll": [data_source_filter, extra_filter]}


def retrieve(
    client,
    *,
    knowledge_base_id: str,
    query: str,
    data_source_id: str,
    number_of_results: int,
    extra_filter: dict | None = None,
) -> tuple[dict, float]:
    started = time.perf_counter()
    response = client.retrieve(
        knowledgeBaseId=knowledge_base_id,
        retrievalQuery={"text": query, "type": "TEXT"},
        retrievalConfiguration={
            "managedSearchConfiguration": {
                "numberOfResults": number_of_results,
                "rerankingModelType": "MANAGED",
                "filter": combine_filters(data_source_id, extra_filter),
            }
        },
    )
    return response, (time.perf_counter() - started) * 1000


def user_metadata(metadata: dict) -> dict:
    return {key: value for key, value in metadata.items() if not key.startswith("_")}


def score_response(case: QueryCase, response: dict, latency_ms: float) -> dict:
    results = response.get("retrievalResults", [])
    texts = [item.get("content", {}).get("text", "") for item in results]
    combined = "\n".join(texts)
    marker_hits = [
        marker for marker in case.evidence_markers if marker.casefold() in combined.casefold()
    ]
    relevant_ranks = [
        rank
        for rank, text in enumerate(texts, start=1)
        if any(marker.casefold() in text.casefold() for marker in case.evidence_markers)
    ]
    top_result = results[0] if results else {}
    metadata = user_metadata(top_result.get("metadata", {}))
    present_expected = EXPECTED_USER_METADATA.intersection(metadata)
    return {
        "caseId": case.case_id,
        "query": case.query,
        "resultCount": len(results),
        "latencyMs": latency_ms,
        "topScore": top_result.get("score"),
        "markerCoverage": len(marker_hits) / len(case.evidence_markers),
        "markerHits": marker_hits,
        "relevantResultCount": len(relevant_ranks),
        "firstRelevantRank": relevant_ranks[0] if relevant_ranks else None,
        "reciprocalRank": 1 / relevant_ranks[0] if relevant_ranks else 0,
        "hit": bool(relevant_ranks),
        "topUserMetadataCount": len(metadata),
        "topExpectedMetadataCoverage": (
            len(present_expected) / len(EXPECTED_USER_METADATA)
        ),
        "topPreview": top_result.get("content", {}).get("text", "")[:300],
    }


def summarize_variant(name: str, cases: list[dict]) -> dict:
    scores = [item["topScore"] for item in cases if item["topScore"] is not None]
    return {
        "variant": name,
        "caseCount": len(cases),
        "hitRate": statistics.fmean(item["hit"] for item in cases),
        "meanMarkerCoverage": statistics.fmean(
            item["markerCoverage"] for item in cases
        ),
        "meanReciprocalRank": statistics.fmean(
            item["reciprocalRank"] for item in cases
        ),
        "meanRelevantResults": statistics.fmean(
            item["relevantResultCount"] for item in cases
        ),
        "meanTopScore": statistics.fmean(scores) if scores else None,
        "meanLatencyMs": statistics.fmean(item["latencyMs"] for item in cases),
        "medianLatencyMs": statistics.median(item["latencyMs"] for item in cases),
        "meanExpectedMetadataCoverage": statistics.fmean(
            item["topExpectedMetadataCoverage"] for item in cases
        ),
        "cases": cases,
    }


def evaluate_filter(
    client,
    *,
    knowledge_base_id: str,
    data_source_id: str,
    variant: str,
    case: tuple,
    number_of_results: int,
    output_dir: Path,
) -> dict:
    case_id, query, metadata_filter, expected_key, expected_value = case
    response, latency_ms = retrieve(
        client,
        knowledge_base_id=knowledge_base_id,
        query=query,
        data_source_id=data_source_id,
        number_of_results=number_of_results,
        extra_filter=metadata_filter,
    )
    (output_dir / f"metadata-filter-{variant}-{case_id}.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    results = response.get("retrievalResults", [])
    matching = sum(
        item.get("metadata", {}).get(expected_key) == expected_value for item in results
    )
    return {
        "caseId": case_id,
        "resultCount": len(results),
        "matchingResultCount": matching,
        "allResultsMatch": bool(results) and matching == len(results),
        "latencyMs": latency_ms,
    }


def build_markdown(summary: dict) -> str:
    lines = [
        "# Metadata 对照实验",
        "",
        f"- 查询数：{summary['queryCount']}",
        f"- Top-K：{summary['numberOfResults']}",
        "- 内容控制：三组 Markdown 字节完全一致",
        "- 隔离方式：系统字段 `_data_source_id`",
        "",
        "| 变体 | Hit Rate | Marker Coverage | MRR | Relevant@10 | Top Score | Latency (ms) | Metadata Coverage |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ("no-metadata", "filter-metadata", "embedded-metadata"):
        variant = summary["variants"][name]
        top_score = (
            f"{variant['meanTopScore']:.3f}"
            if variant["meanTopScore"] is not None
            else "-"
        )
        lines.append(
            f"| {name} | {variant['hitRate']:.3f} | "
            f"{variant['meanMarkerCoverage']:.3f} | "
            f"{variant['meanReciprocalRank']:.3f} | "
            f"{variant['meanRelevantResults']:.2f} | {top_score} | "
            f"{variant['meanLatencyMs']:.1f} | "
            f"{variant['meanExpectedMetadataCoverage']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Filter 验证",
            "",
            "| 变体 | 用例 | 结果数 | 全部满足 Filter |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for variant, cases in summary["filterTests"].items():
        for case in cases:
            lines.append(
                f"| {variant} | {case['caseId']} | {case['resultCount']} | "
                f"{'是' if case['allResultsMatch'] else '否'} |"
            )
    negative = summary["missingMetadataNegativeControl"]
    lines.extend(
        [
            "",
            "## 负对照",
            "",
            "无 Metadata 组叠加 `classification=PUBLIC` Filter 后应返回 0 条："
            f"实测 {negative['resultCount']} 条。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--no-metadata-data-source-id", required=True)
    parser.add_argument("--filter-metadata-data-source-id", required=True)
    parser.add_argument("--embedded-metadata-data-source-id", required=True)
    parser.add_argument("--number-of-results", type=int, default=10)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    data_source_ids = {
        "no-metadata": args.no_metadata_data_source_id,
        "filter-metadata": args.filter_metadata_data_source_id,
        "embedded-metadata": args.embedded_metadata_data_source_id,
    }
    client = create_client(args.region)
    variant_summaries = {}
    filter_tests = {}
    try:
        for variant, data_source_id in data_source_ids.items():
            cases = []
            for case in QUERY_CASES:
                response, latency_ms = retrieve(
                    client,
                    knowledge_base_id=args.knowledge_base_id,
                    query=case.query,
                    data_source_id=data_source_id,
                    number_of_results=args.number_of_results,
                )
                (args.output_dir / f"metadata-{variant}-{case.case_id}.json").write_text(
                    json.dumps(response, ensure_ascii=False, indent=2, default=str)
                    + "\n",
                    encoding="utf-8",
                )
                cases.append(score_response(case, response, latency_ms))
            variant_summaries[variant] = summarize_variant(variant, cases)

        for variant in ("filter-metadata", "embedded-metadata"):
            filter_tests[variant] = [
                evaluate_filter(
                    client,
                    knowledge_base_id=args.knowledge_base_id,
                    data_source_id=data_source_ids[variant],
                    variant=variant,
                    case=case,
                    number_of_results=args.number_of_results,
                    output_dir=args.output_dir,
                )
                for case in FILTER_CASES
            ]

        negative_response, negative_latency = retrieve(
            client,
            knowledge_base_id=args.knowledge_base_id,
            query="玩家安全和欺诈检测",
            data_source_id=data_source_ids["no-metadata"],
            number_of_results=args.number_of_results,
            extra_filter={
                "equals": {"key": "classification", "value": "PUBLIC"}
            },
        )
    except ClientError as error:
        print(f"Metadata experiment comparison failed: {error}", file=sys.stderr)
        return 1

    summary = {
        "queryCount": len(QUERY_CASES),
        "numberOfResults": args.number_of_results,
        "retrievalConfiguration": {
            "search": "MANAGED",
            "rerankingModelType": "MANAGED",
            "variantIsolation": "_data_source_id",
        },
        "variants": variant_summaries,
        "filterTests": filter_tests,
        "missingMetadataNegativeControl": {
            "filter": {"classification": "PUBLIC"},
            "resultCount": len(negative_response.get("retrievalResults", [])),
            "latencyMs": negative_latency,
        },
    }
    (args.output_dir / "metadata-experiment-comparison.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "metadata-experiment-comparison.md").write_text(
        build_markdown(summary),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
