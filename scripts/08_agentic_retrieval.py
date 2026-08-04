#!/usr/bin/env python3

import argparse
import json
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def run_agentic_retrieval(
    *,
    region: str,
    knowledge_base_id: str,
    query: str,
    document_id: str,
    max_results: int,
    max_iterations: int,
    output_path: Path,
) -> dict:
    client = boto3.client(
        "bedrock-agent-runtime",
        region_name=region,
        config=Config(
            retries={"total_max_attempts": 5, "mode": "adaptive"},
            connect_timeout=10,
            read_timeout=300,
        ),
    )

    if not hasattr(client, "agentic_retrieve_stream"):
        raise RuntimeError(
            f"boto3 {boto3.__version__} does not expose "
            "agentic_retrieve_stream; install and pin a newer validated SDK"
        )

    response = client.agentic_retrieve_stream(
        messages=[{"role": "user", "content": {"text": query}}],
        retrievers=[
            {
                "configuration": {
                    "knowledgeBase": {
                        "knowledgeBaseId": knowledge_base_id,
                        "retrievalOverrides": {
                            "maxNumberOfResults": max_results,
                            "filter": {
                                "equals": {
                                    "key": "document_id",
                                    "value": document_id,
                                }
                            },
                        },
                    }
                },
                "description": "AWS Well-Architected Games Industry Lens",
            }
        ],
        agenticRetrieveConfiguration={
            "foundationModelType": "MANAGED",
            "rerankingModelType": "MANAGED",
            "maxAgentIteration": max_iterations,
        },
        generateResponse=True,
        userContext={"userId": "managed-kb-e2e-test"},
    )

    event_count = 0
    answer_chunks = []
    final_result = None
    stream = response["stream"]

    try:
        with output_path.open("w", encoding="utf-8") as output:
            for event in stream:
                event_count += 1
                output.write(json.dumps(event, ensure_ascii=False, default=str))
                output.write("\n")

                if "responseEvent" in event:
                    answer_chunks.append(event["responseEvent"].get("text", ""))
                if "result" in event:
                    final_result = event["result"]
    finally:
        stream.close()

    return {
        "eventCount": event_count,
        "streamedAnswer": "".join(answer_chunks),
        "finalResult": final_result,
        "outputPath": str(output_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument(
        "--query",
        default="比较游戏发布日流量尖峰下的可靠性与成本优化措施，并说明权衡。",
    )
    parser.add_argument(
        "--document-id",
        default="aws-games-industry-lens-2026-07-31",
    )
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/20260803/tests/agentic-retrieval-events.ndjson"),
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = run_agentic_retrieval(
            region=args.region,
            knowledge_base_id=args.knowledge_base_id,
            query=args.query,
            document_id=args.document_id,
            max_results=args.max_results,
            max_iterations=args.max_iterations,
            output_path=args.output,
        )
    except (ClientError, RuntimeError) as error:
        print(f"Agentic retrieval failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
