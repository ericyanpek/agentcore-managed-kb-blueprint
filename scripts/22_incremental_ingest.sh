#!/usr/bin/env bash

# Executes the plan produced by 22_incremental_ingest.py.
#
# Order matters. S3 upload precedes direct ingestion because a document ingested
# only into the index is not written back to the bucket and a later
# reconciliation sync may drop it. Deletions are applied to S3 before the
# reconciliation job because the connector is the only channel that removes
# vectors.
#
# Set DRY_RUN=1 to produce the plan without calling any mutating API.

source "$(dirname "$0")/lib.sh"

python_bin="${PYTHON_BIN:-python3}"
require_command aws
require_command jq
require_command "${python_bin}"
load_state

: "${KB_ID:?Missing KB_ID in state}"
: "${MD_DATA_SOURCE_ID:?Set MD_DATA_SOURCE_ID to the Markdown data source}"
: "${BUCKET_NAME:?Missing BUCKET_NAME}"
: "${MD_S3_PREFIX:?Set MD_S3_PREFIX in config/test.env}"

md_output_dir="${SOURCE_DIR}/md-corpus"
change_report="${TEST_DIR}/md-corpus-preparation-report.json"
plan_file="${TEST_DIR}/md-ingestion-plan.json"
result_file="${TEST_DIR}/md-ingestion-result.json"
published_dir="${ARTIFACT_DIR}/published"
dry_run="${DRY_RUN:-0}"

if [[ ! -f "${change_report}" ]]; then
  printf 'Missing change report. Run scripts/21_prepare_md_corpus.sh first.\n' >&2
  exit 1
fi

reconcile_args=()
if [[ "${ALWAYS_RECONCILE:-0}" == "1" ]]; then
  reconcile_args=(--always-reconcile)
fi

"${python_bin}" "${ROOT_DIR}/scripts/22_incremental_ingest.py" \
  --change-report "${change_report}" \
  --plan "${plan_file}" \
  --s3-prefix "${MD_S3_PREFIX}" \
  --batch-size "${MD_DIRECT_BATCH_SIZE:-10}" \
  --throttle-interval-seconds "${MD_THROTTLE_INTERVAL_SECONDS:-10}" \
  --deletion-protection-threshold "${MD_DELETION_PROTECTION_THRESHOLD:-0.5}" \
  "${reconcile_args[@]}"

while IFS= read -r guardrail; do
  printf 'Guardrail: %s\n' "${guardrail}" >&2
done < <(jq -r '.guardrails[]' "${plan_file}")

if [[ "${dry_run}" == "1" ]]; then
  printf 'DRY_RUN=1, stopping after planning. Plan: %s\n' "${plan_file}"
  exit 0
fi

printf 'Uploading changed objects and sidecars to s3://%s/%s\n' \
  "${BUCKET_NAME}" "${MD_S3_PREFIX}"
while IFS= read -r relative_path; do
  aws s3 cp "${md_output_dir}/${relative_path}" \
    "s3://${BUCKET_NAME}/${MD_S3_PREFIX}/${relative_path}" \
    --region "${AWS_REGION}" --only-show-errors
  aws s3 cp "${md_output_dir}/${relative_path}.metadata.json" \
    "s3://${BUCKET_NAME}/${MD_S3_PREFIX}/${relative_path}.metadata.json" \
    --region "${AWS_REGION}" --only-show-errors
done < <(jq -r '.changes.added[].file, .changes.modified[].file' "${change_report}")

while IFS= read -r relative_path; do
  printf 'Removing s3://%s/%s/%s\n' "${BUCKET_NAME}" "${MD_S3_PREFIX}" "${relative_path}"
  aws s3 rm "s3://${BUCKET_NAME}/${MD_S3_PREFIX}/${relative_path}" \
    --region "${AWS_REGION}" --only-show-errors || true
  aws s3 rm "s3://${BUCKET_NAME}/${MD_S3_PREFIX}/${relative_path}.metadata.json" \
    --region "${AWS_REGION}" --only-show-errors || true
done < <(jq -r '.changes.deleted[].file' "${change_report}")

executed_steps="[]"
batch_total="$(jq '[.steps[] | select(.channel == "direct")] | length' "${plan_file}")"
batch_index=0

while IFS= read -r step; do
  batch_index=$((batch_index + 1))
  payload="$(jq -c \
    --arg bucket "${BUCKET_NAME}" \
    '{documents: [.s3Keys[] | {content: {dataSourceType: "S3", s3: {s3Location: {uri: ("s3://" + $bucket + "/" + .)}}}}]}' \
    <<< "${step}")"

  attempt=1
  max_attempts="${MD_MAX_ATTEMPTS:-4}"
  while true; do
    if response="$(aws bedrock-agent ingest-knowledge-base-documents \
      --knowledge-base-id "${KB_ID}" \
      --data-source-id "${MD_DATA_SOURCE_ID}" \
      --cli-input-json "${payload}" \
      --region "${AWS_REGION}" \
      --output json 2>&1)"; then
      printf 'Direct batch %s/%s accepted\n' "${batch_index}" "${batch_total}"
      executed_steps="$(jq \
        --argjson sequence "${batch_index}" \
        --argjson attempts "${attempt}" \
        '. + [{channel: "direct", sequence: $sequence, outcome: "ACCEPTED", attempts: $attempts}]' \
        <<< "${executed_steps}")"
      break
    fi

    if [[ "${attempt}" -ge "${max_attempts}" ]]; then
      printf 'Direct batch %s failed after %s attempts\n' "${batch_index}" "${attempt}" >&2
      executed_steps="$(jq \
        --argjson sequence "${batch_index}" \
        --argjson attempts "${attempt}" \
        --arg detail "$(head -c 400 <<< "${response}" | tr '\n' ' ')" \
        '. + [{channel: "direct", sequence: $sequence, outcome: "FAILED", attempts: $attempts, detail: $detail}]' \
        <<< "${executed_steps}")"
      printf '%s\n' "${executed_steps}" > "${TEST_DIR}/md-ingestion-partial.json"
      exit 1
    fi

    backoff=$((2 ** attempt))
    printf 'Direct batch %s attempt %s failed, retrying in %ss\n' \
      "${batch_index}" "${attempt}" "${backoff}" >&2
    sleep "${backoff}"
    attempt=$((attempt + 1))
  done
done < <(jq -c '.steps[] | select(.channel == "direct")' "${plan_file}")

reconciliation_job_id="none"
if [[ "$(jq -r '.reconciliationRequired' "${plan_file}")" == "true" ]]; then
  printf 'Running reconciliation sync\n'
  sync_response="$(aws bedrock-agent start-ingestion-job \
    --knowledge-base-id "${KB_ID}" \
    --data-source-id "${MD_DATA_SOURCE_ID}" \
    --description "Markdown reconciliation ${RUN_ID}" \
    --region "${AWS_REGION}" \
    --output json)"
  reconciliation_job_id="$(jq -r '.ingestionJob.ingestionJobId' <<< "${sync_response}")"
  wait_for_ingestion "${KB_ID}" "${MD_DATA_SOURCE_ID}" "${reconciliation_job_id}"
  aws bedrock-agent get-ingestion-job \
    --knowledge-base-id "${KB_ID}" \
    --data-source-id "${MD_DATA_SOURCE_ID}" \
    --ingestion-job-id "${reconciliation_job_id}" \
    --region "${AWS_REGION}" \
    --output json > "${AWS_DIR}/md-reconciliation-${reconciliation_job_id}.json"
  jq '.ingestionJob.statistics' \
    "${AWS_DIR}/md-reconciliation-${reconciliation_job_id}.json"
fi

mkdir -p "${published_dir}"
cp "${md_output_dir}/manifest.json" "${published_dir}/md-corpus-manifest.json"

jq -n \
  --arg runId "${RUN_ID}" \
  --arg corpusId "$(jq -r '.corpusId' "${change_report}")" \
  --argjson changeCounts "$(jq '.changeCounts' "${change_report}")" \
  --argjson executedSteps "${executed_steps}" \
  --arg reconciliationJobId "${reconciliation_job_id}" \
  --argjson guardrails "$(jq '.guardrails' "${plan_file}")" \
  '{
    runId: $runId,
    corpusId: $corpusId,
    changeCounts: $changeCounts,
    directBatches: $executedSteps,
    reconciliationJobId: $reconciliationJobId,
    guardrails: $guardrails,
    publishedManifest: "artifacts/<RUN_ID>/published/md-corpus-manifest.json"
  }' > "${result_file}"

printf 'Wrote %s\n' "${result_file}"
printf 'Published manifest promoted. Next run will diff against it.\n'
