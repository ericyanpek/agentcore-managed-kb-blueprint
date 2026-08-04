#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

require_command aws
require_command jq
load_state

: "${KB_ID:?Missing KB_ID in state}"
: "${TEXT_DOCUMENT_ID:?Run scripts/10_ingest_text_repair.sh first}"

queries=(
  "如何监控和审核玩家使用行为，并检测和应对滥用或不当行为？"
  "如何防止玩家绕过配对系统并未经授权加入游戏会话？"
  "游戏账号与交易欺诈检测有哪些直接支持的控制？请区分账号保护、异常行为检测、支付或虚拟经济欺诈以及未覆盖内容。"
  "玩家账户如何通过强密码、多因素身份验证和风险场景控制降低账户接管风险？"
)

retrieval_config="$(jq -n \
  --arg document_id "${TEXT_DOCUMENT_ID}" \
  '{
    managedSearchConfiguration: {
      numberOfResults: 10,
      rerankingModelType: "MANAGED",
      filter: {
        equals: {
          key: "document_id",
          value: $document_id
        }
      }
    }
  }')"

for i in "${!queries[@]}"; do
  test_number="$((i + 1))"
  retrieval_query="$(jq -n \
    --arg text "${queries[$i]}" \
    '{text: $text, type: "TEXT"}')"

  aws bedrock-agent-runtime retrieve \
    --knowledge-base-id "${KB_ID}" \
    --retrieval-query "${retrieval_query}" \
    --retrieval-configuration "${retrieval_config}" \
    --region "${AWS_REGION}" \
    --output json > "${TEST_DIR}/text-repair-retrieve-${test_number}.json"
done

jq -s '{
  tests: [
    to_entries[] |
    {
      testNumber: (.key + 1),
      resultCount: (.value.retrievalResults | length),
      topScore: (.value.retrievalResults[0].score // null),
      cjkResultCount: (
        [.value.retrievalResults[] |
          select((.content.text // "") | test("[一-龥]"))] |
        length
      ),
      replacementCharacterResultCount: (
        [.value.retrievalResults[] |
          select((.content.text // "") | contains("�"))] |
        length
      ),
      languageCodes: (
        [.value.retrievalResults[].metadata._language_code // "missing"] |
        unique
      ),
      topPreviews: [
        .value.retrievalResults[0:3][] |
        {
          score,
          sourcePage: .metadata._excerpt_page_number,
          text: ((.content.text // "") | .[0:500])
        }
      ]
    }
  ]
}' "${TEST_DIR}"/text-repair-retrieve-[1-4].json \
  > "${TEST_DIR}/text-repair-retrieval-summary.json"

if ! jq -e '
  all(
    .tests[];
    .resultCount > 0 and
    .cjkResultCount > 0 and
    .replacementCharacterResultCount == 0
  )
' "${TEST_DIR}/text-repair-retrieval-summary.json" >/dev/null; then
  printf 'Text repair retrieval quality gate failed.\n' >&2
  exit 1
fi

cat "${TEST_DIR}/text-repair-retrieval-summary.json"
