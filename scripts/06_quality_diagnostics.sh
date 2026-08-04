#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

require_command aws
require_command jq
load_state

: "${KB_ID:?Missing KB_ID in state}"

query="游戏发布日流量尖峰下，如何平衡可靠性、容量与成本？"

base_config='{
  "managedSearchConfiguration": {
    "numberOfResults": 50,
    "rerankingModelType": "MANAGED",
    "filter": {
      "equals": {
        "key": "document_id",
        "value": "aws-games-industry-lens-2026-07-31"
      }
    }
  }
}'

image_config='{
  "managedSearchConfiguration": {
    "numberOfResults": 20,
    "rerankingModelType": "MANAGED",
    "filter": {
      "andAll": [
        {
          "equals": {
            "key": "document_id",
            "value": "aws-games-industry-lens-2026-07-31"
          }
        },
        {
          "equals": {
            "key": "_media_type",
            "value": "image"
          }
        }
      ]
    }
  }
}'

non_image_config='{
  "managedSearchConfiguration": {
    "numberOfResults": 20,
    "rerankingModelType": "MANAGED",
    "filter": {
      "andAll": [
        {
          "equals": {
            "key": "document_id",
            "value": "aws-games-industry-lens-2026-07-31"
          }
        },
        {
          "notEquals": {
            "key": "_media_type",
            "value": "image"
          }
        }
      ]
    }
  }
}'

retrieval_query="$(jq -n --arg text "${query}" '{text: $text, type: "TEXT"}')"

aws bedrock-agent-runtime retrieve \
  --knowledge-base-id "${KB_ID}" \
  --retrieval-query "${retrieval_query}" \
  --retrieval-configuration "${base_config}" \
  --region "${AWS_REGION}" \
  --output json > "${TEST_DIR}/diagnostic-all.json"

aws bedrock-agent-runtime retrieve \
  --knowledge-base-id "${KB_ID}" \
  --retrieval-query "${retrieval_query}" \
  --retrieval-configuration "${image_config}" \
  --region "${AWS_REGION}" \
  --output json > "${TEST_DIR}/diagnostic-image.json"

aws bedrock-agent-runtime retrieve \
  --knowledge-base-id "${KB_ID}" \
  --retrieval-query "${retrieval_query}" \
  --retrieval-configuration "${non_image_config}" \
  --region "${AWS_REGION}" \
  --output json > "${TEST_DIR}/diagnostic-non-image.json"

jq -n \
  --slurpfile all "${TEST_DIR}/diagnostic-all.json" \
  --slurpfile image "${TEST_DIR}/diagnostic-image.json" \
  --slurpfile non_image "${TEST_DIR}/diagnostic-non-image.json" \
  '{
    all: {
      resultCount: ($all[0].retrievalResults | length),
      topScore: $all[0].retrievalResults[0].score,
      mediaTypeCounts: (
        $all[0].retrievalResults |
        map(.metadata._media_type // "non-image") |
        group_by(.) |
        map({key: .[0], value: length}) |
        from_entries
      )
    },
    imageOnly: {
      resultCount: ($image[0].retrievalResults | length),
      topScore: ($image[0].retrievalResults[0].score // null),
      topPreview: ($image[0].retrievalResults[0].content.text // "" | .[0:400])
    },
    nonImage: {
      resultCount: ($non_image[0].retrievalResults | length),
      topScore: ($non_image[0].retrievalResults[0].score // null),
      topPreview: ($non_image[0].retrievalResults[0].content.text // "" | .[0:400])
    }
  }' > "${TEST_DIR}/quality-diagnostic-summary.json"

cat "${TEST_DIR}/quality-diagnostic-summary.json"
