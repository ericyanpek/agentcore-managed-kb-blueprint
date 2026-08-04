#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

require_command aws
require_command jq
load_state

: "${KB_ID:?Run scripts/02_provision.sh first}"
: "${DATA_SOURCE_ID:?Run scripts/03_ingest.sh first}"

queries=(
  "根据 AWS 游戏行业框架，实时多人游戏应如何应对区域故障，并恢复玩家会话？"
  "游戏发布日或大型活动出现流量尖峰时，架构应如何扩缩容并控制成本？"
  "该指南对玩家数据保护、反作弊和安全事件响应提出了哪些建议？"
)

retrieval_config='{
  "managedSearchConfiguration": {
    "numberOfResults": 10,
    "rerankingModelType": "MANAGED"
  }
}'

filtered_retrieval_config='{
  "managedSearchConfiguration": {
    "numberOfResults": 10,
    "rerankingModelType": "MANAGED",
    "filter": {
      "equals": {
        "key": "document_id",
        "value": "aws-games-industry-lens-2026-07-31"
      }
    }
  }
}'

for i in "${!queries[@]}"; do
  test_number="$((i + 1))"
  query="${queries[$i]}"

  aws bedrock-agent-runtime retrieve \
    --knowledge-base-id "${KB_ID}" \
    --retrieval-query "$(jq -n --arg text "${query}" '{text: $text, type: "TEXT"}')" \
    --retrieval-configuration "${retrieval_config}" \
    --region "${AWS_REGION}" \
    --output json > "${TEST_DIR}/retrieve-${test_number}.json"

  jq '{
    resultCount: (.retrievalResults | length),
    results: [.retrievalResults[] | {
      score,
      location,
      metadata,
      textPreview: (.content.text // "" | .[0:240])
    }]
  }' "${TEST_DIR}/retrieve-${test_number}.json" \
    > "${TEST_DIR}/retrieve-${test_number}-summary.json"
done

aws bedrock-agent-runtime retrieve \
  --knowledge-base-id "${KB_ID}" \
  --retrieval-query "$(jq -n --arg text "${queries[0]}" '{text: $text, type: "TEXT"}')" \
  --retrieval-configuration "${filtered_retrieval_config}" \
  --region "${AWS_REGION}" \
  --output json > "${TEST_DIR}/retrieve-filtered.json"

for i in "${!queries[@]}"; do
  test_number="$((i + 1))"
  query="${queries[$i]}"
  retrieve_file="${TEST_DIR}/retrieve-${test_number}.json"

  context="$(jq -r '
    [
      .retrievalResults[0:5] |
      to_entries[] |
      "[S\(.key + 1)] page=\(.value.metadata._excerpt_page_number) uri=\(.value.metadata._source_uri)\n\(.value.content.text)"
    ] | join("\n\n")
  ' "${retrieve_file}")"

  prompt="$(printf \
    '请仅依据以下检索结果回答。若资料不足，请明确说明。使用中文，给出简洁的架构建议，并在每个关键结论后使用 [S1] 这样的编号标注来源。\n\n检索结果：\n%s\n\n问题：%s' \
    "${context}" \
    "${query}")"

  messages="$(jq -n --arg prompt "${prompt}" \
    '[{role: "user", content: [{text: $prompt}]}]')"

  system_prompt='[{"text":"你是 AWS 游戏行业架构审查助手。不得使用检索上下文之外的事实，并且不得伪造引用。"}]'

  aws bedrock-runtime converse \
    --model-id "${GENERATION_MODEL_ID}" \
    --system "${system_prompt}" \
    --messages "${messages}" \
    --inference-config '{"maxTokens":1200,"temperature":0.1,"topP":0.9}' \
    --region "${AWS_REGION}" \
    --output json > "${TEST_DIR}/converse-${test_number}.json"

  jq --slurpfile retrieval "${retrieve_file}" '{
    answer: ([.output.message.content[].text] | join("\n")),
    stopReason,
    usage,
    sourceMap: [
      $retrieval[0].retrievalResults[0:5] |
      to_entries[] |
      {
        citation: ("S" + ((.key + 1) | tostring)),
        score: .value.score,
        page: .value.metadata._excerpt_page_number,
        uri: .value.metadata._source_uri
      }
    ]
  }' "${TEST_DIR}/converse-${test_number}.json" \
    > "${TEST_DIR}/converse-${test_number}-summary.json"
done

jq -s '{
  tests: [.[] | {
    resultCount,
    topScore: (.results[0].score // null),
    lowestReturnedScore: (.results[-1].score // null)
  }]
}' "${TEST_DIR}"/retrieve-[1-3]-summary.json \
  > "${TEST_DIR}/retrieval-score-summary.json"

cat "${TEST_DIR}/retrieval-score-summary.json"
