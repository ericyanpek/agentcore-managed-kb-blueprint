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
    query: str,
    filter_key: str,
    filter_value: str,
    number_of_results: int,
) -> tuple[dict, float]:
    started = time.perf_counter()
    response = client.retrieve(
        knowledgeBaseId=knowledge_base_id,
        retrievalQuery={"text": query, "type": "TEXT"},
        retrievalConfiguration={
            "managedSearchConfiguration": {
                "numberOfResults": number_of_results,
                "rerankingModelType": "MANAGED",
                "filter": {
                    "equals": {
                        "key": filter_key,
                        "value": filter_value,
                    }
                },
            }
        },
    )
    return response, (time.perf_counter() - started) * 1000


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
    top_metadata = top_result.get("metadata", {})
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
        "topSourcePages": [
            {
                "start": item.get("metadata", {}).get("source_page_start"),
                "end": item.get("metadata", {}).get("source_page_end"),
            }
            for item in results[:3]
        ],
        "topSectionPath": top_metadata.get("section_path"),
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
        "cases": cases,
    }


def build_markdown(summary: dict) -> str:
    baseline = summary["variants"]["baseline"]
    semantic = summary["variants"]["semantic"]
    lines = [
        "# Semantic Chunking 对照实验",
        "",
        f"- 查询数：{summary['queryCount']}",
        f"- Top-K：{summary['numberOfResults']}",
        "- 检索配置：Managed Search + Managed Reranking",
        "",
        "| 指标 | Fixed Size 基线 | 语义预分块 | 差值 |",
        "| --- | ---: | ---: | ---: |",
    ]
    metrics = (
        ("Hit Rate", "hitRate", ".3f"),
        ("Mean Marker Coverage", "meanMarkerCoverage", ".3f"),
        ("MRR", "meanReciprocalRank", ".3f"),
        ("Mean Relevant Results", "meanRelevantResults", ".2f"),
        ("Mean Top Score", "meanTopScore", ".3f"),
        ("Mean Latency (ms)", "meanLatencyMs", ".1f"),
    )
    for label, key, format_spec in metrics:
        base_value = baseline[key]
        semantic_value = semantic[key]
        if base_value is None or semantic_value is None:
            lines.append(f"| {label} | - | - | - |")
            continue
        difference = semantic_value - base_value
        lines.append(
            f"| {label} | {base_value:{format_spec}} | "
            f"{semantic_value:{format_spec}} | {difference:+{format_spec}} |"
        )

    lines.extend(
        [
            "",
            "| 用例 | 基线覆盖率 | 语义覆盖率 | 基线首个相关排名 | 语义首个相关排名 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    baseline_cases = {item["caseId"]: item for item in baseline["cases"]}
    semantic_cases = {item["caseId"]: item for item in semantic["cases"]}
    for case_id in baseline_cases:
        base_case = baseline_cases[case_id]
        semantic_case = semantic_cases[case_id]
        lines.append(
            f"| {case_id} | {base_case['markerCoverage']:.2f} | "
            f"{semantic_case['markerCoverage']:.2f} | "
            f"{base_case['firstRelevantRank'] or '-'} | "
            f"{semantic_case['firstRelevantRank'] or '-'} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument(
        "--baseline-document-id",
        default="aws-games-industry-lens-2026-07-31-text-v1",
    )
    parser.add_argument(
        "--semantic-corpus-id",
        default="aws-games-industry-lens-2026-07-31-semantic-v1",
    )
    parser.add_argument("--number-of-results", type=int, default=10)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    client = create_client(args.region)
    variants = {
        "baseline": ("document_id", args.baseline_document_id),
        "semantic": ("corpus_id", args.semantic_corpus_id),
    }
    variant_summaries = {}
    try:
        for variant_name, (filter_key, filter_value) in variants.items():
            case_summaries = []
            for case in QUERY_CASES:
                response, latency_ms = retrieve(
                    client,
                    knowledge_base_id=args.knowledge_base_id,
                    query=case.query,
                    filter_key=filter_key,
                    filter_value=filter_value,
                    number_of_results=args.number_of_results,
                )
                raw_path = args.output_dir / (
                    f"semantic-chunking-{variant_name}-{case.case_id}.json"
                )
                raw_path.write_text(
                    json.dumps(response, ensure_ascii=False, indent=2, default=str)
                    + "\n",
                    encoding="utf-8",
                )
                case_summaries.append(score_response(case, response, latency_ms))
            variant_summaries[variant_name] = summarize_variant(
                variant_name,
                case_summaries,
            )
    except ClientError as error:
        print(f"Semantic chunking comparison failed: {error}", file=sys.stderr)
        return 1

    summary = {
        "queryCount": len(QUERY_CASES),
        "numberOfResults": args.number_of_results,
        "retrievalConfiguration": {
            "search": "MANAGED",
            "rerankingModelType": "MANAGED",
        },
        "variants": variant_summaries,
    }
    summary_path = args.output_dir / "semantic-chunking-comparison.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path = args.output_dir / "semantic-chunking-comparison.md"
    markdown_path.write_text(build_markdown(summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
