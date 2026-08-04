#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

require_command aws
require_command jq
load_state

expected_confirmation="DELETE-${KB_NAME}"
if [[ "${CONFIRM_DESTROY:-}" != "${expected_confirmation}" ]]; then
  printf 'Refusing cleanup. Set CONFIRM_DESTROY=%s to continue.\n' "${expected_confirmation}" >&2
  exit 1
fi

if [[ -n "${KB_ID:-}" ]]; then
  for data_source_id in \
    "${METADATA_EMBEDDED_DATA_SOURCE_ID:-}" \
    "${METADATA_FILTER_DATA_SOURCE_ID:-}" \
    "${METADATA_NONE_DATA_SOURCE_ID:-}" \
    "${SEMANTIC_DATA_SOURCE_ID:-}" \
    "${TEXT_DATA_SOURCE_ID:-}" \
    "${DATA_SOURCE_ID:-}"; do
    if [[ -z "${data_source_id}" ]]; then
      continue
    fi

    aws bedrock-agent delete-data-source \
      --knowledge-base-id "${KB_ID}" \
      --data-source-id "${data_source_id}" \
      --region "${AWS_REGION}"

    for _attempt in {1..60}; do
      if ! aws bedrock-agent get-data-source \
        --knowledge-base-id "${KB_ID}" \
        --data-source-id "${data_source_id}" \
        --region "${AWS_REGION}" >/dev/null 2>&1; then
        break
      fi
      sleep 10
    done
  done
fi

if [[ -n "${KB_ID:-}" ]]; then
  aws bedrock-agent delete-knowledge-base \
    --knowledge-base-id "${KB_ID}" \
    --region "${AWS_REGION}"

  for _attempt in {1..60}; do
    if ! aws bedrock-agent get-knowledge-base \
      --knowledge-base-id "${KB_ID}" \
      --region "${AWS_REGION}" >/dev/null 2>&1; then
      break
    fi
    sleep 10
  done
fi

while :; do
  versioned_objects="$(aws s3api list-object-versions \
    --bucket "${BUCKET_NAME}" \
    --region "${AWS_REGION}" \
    --output json |
    jq '{
      Objects: (
        ([.Versions[]? | {Key, VersionId}] +
         [.DeleteMarkers[]? | {Key, VersionId}])[0:1000]
      ),
      Quiet: true
    }')"

  if [[ "$(jq '.Objects | length' <<< "${versioned_objects}")" -eq 0 ]]; then
    break
  fi

  aws s3api delete-objects \
    --bucket "${BUCKET_NAME}" \
    --delete "${versioned_objects}" \
    --region "${AWS_REGION}"
done

aws s3api delete-bucket --bucket "${BUCKET_NAME}" --region "${AWS_REGION}"
aws iam delete-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${ROLE_POLICY_NAME}"
aws iam delete-role --role-name "${ROLE_NAME}"
