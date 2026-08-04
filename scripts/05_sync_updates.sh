#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

require_command aws
require_command jq
load_state

: "${KB_ID:?Missing KB_ID in state}"
: "${DATA_SOURCE_ID:?Missing DATA_SOURCE_ID in state}"

target_data_source_id="${TARGET_DATA_SOURCE_ID:-${DATA_SOURCE_ID}}"

ingestion_response="$(aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${target_data_source_id}" \
  --description "Incremental sync $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --region "${AWS_REGION}" \
  --output json)"

ingestion_job_id="$(jq -r '.ingestionJob.ingestionJobId' <<< "${ingestion_response}")"
printf '%s\n' "${ingestion_response}" \
  > "${AWS_DIR}/start-ingestion-job-${ingestion_job_id}.json"

wait_for_ingestion "${KB_ID}" "${target_data_source_id}" "${ingestion_job_id}"

aws bedrock-agent get-ingestion-job \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${target_data_source_id}" \
  --ingestion-job-id "${ingestion_job_id}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/ingestion-job-${ingestion_job_id}.json"

jq '.ingestionJob.statistics' "${AWS_DIR}/ingestion-job-${ingestion_job_id}.json"
