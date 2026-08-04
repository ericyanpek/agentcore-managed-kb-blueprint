#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

require_command aws
require_command jq
load_state

: "${KB_ID:?Run scripts/02_provision.sh first}"

metadata_source_dir="${SOURCE_DIR}/metadata-experiment"
metadata_prefix_root="${METADATA_S3_PREFIX_ROOT:-documents/games-industry-lens-metadata/2026-07-31-v1}"

if [[ ! -f "${TEST_DIR}/metadata-experiment-preparation-report.json" ]]; then
  printf 'Metadata experiment corpus is missing. Run scripts/17_prepare_metadata_experiment.sh first.\n' >&2
  exit 1
fi

variants=("no-metadata" "filter-metadata" "embedded-metadata")
state_keys=(
  "METADATA_NONE_DATA_SOURCE_ID"
  "METADATA_FILTER_DATA_SOURCE_ID"
  "METADATA_EMBEDDED_DATA_SOURCE_ID"
)
ingestion_state_keys=(
  "METADATA_NONE_INGESTION_JOB_ID"
  "METADATA_FILTER_INGESTION_JOB_ID"
  "METADATA_EMBEDDED_INGESTION_JOB_ID"
)
data_source_names=(
  "${METADATA_NONE_DATA_SOURCE_NAME:-games-lens-metadata-none-${RUN_ID}}"
  "${METADATA_FILTER_DATA_SOURCE_NAME:-games-lens-metadata-filter-${RUN_ID}}"
  "${METADATA_EMBEDDED_DATA_SOURCE_NAME:-games-lens-metadata-embedded-${RUN_ID}}"
)

current_policy="$(aws iam get-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${ROLE_POLICY_NAME}" \
  --query 'PolicyDocument' \
  --output json)"

updated_policy="${current_policy}"
for variant in "${variants[@]}"; do
  prefix="${metadata_prefix_root}/${variant}"
  object_arn="arn:aws:s3:::${BUCKET_NAME}/${prefix}/*"
  updated_policy="$(jq \
    --arg prefix "${prefix}" \
    --arg object_arn "${object_arn}" \
    '
      .Statement |= map(
        if .Sid == "ListOnlyTheManagedKBPrefix" then
          .Condition["ForAnyValue:StringLike"]["s3:prefix"] |=
            (. + [$prefix, ($prefix + "/*")] | unique)
        elif .Sid == "ReadOnlyTheManagedKBPrefix" then
          .Resource |= (. + [$object_arn] | unique)
        else
          .
        end
      )
    ' <<< "${updated_policy}")"
done

aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${ROLE_POLICY_NAME}" \
  --policy-document "${updated_policy}"
printf '%s\n' "${updated_policy}" > "${AWS_DIR}/metadata-experiment-iam-role-policy.json"

for index in "${!variants[@]}"; do
  variant="${variants[$index]}"
  prefix="${metadata_prefix_root}/${variant}"
  aws s3 sync \
    "${metadata_source_dir}/${variant}/" \
    "s3://${BUCKET_NAME}/${prefix}/" \
    --only-show-errors \
    --region "${AWS_REGION}"

  data_source_name="${data_source_names[$index]}"
  data_source_id="$(aws bedrock-agent list-data-sources \
    --knowledge-base-id "${KB_ID}" \
    --region "${AWS_REGION}" \
    --output json |
    jq -r --arg name "${data_source_name}" \
      '.dataSourceSummaries[] | select(.name == $name) | .dataSourceId' |
    head -n 1)"

  if [[ -z "${data_source_id}" ]]; then
    data_source_config="$(jq -n \
      --arg bucket "${BUCKET_NAME}" \
      --arg account "${AWS_ACCOUNT_ID}" \
      --arg prefix "${prefix}/" \
      '{
        type: "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
        managedKnowledgeBaseConnectorConfiguration: {
          deletionProtectionConfiguration: {
            deletionProtectionStatus: "ENABLED",
            deletionProtectionThreshold: 50
          },
          connectorParameters: {
            type: "S3",
            version: "1",
            connectionConfiguration: {
              bucketName: $bucket,
              bucketOwnerAccountId: $account
            },
            filterConfiguration: {
              inclusionPrefixes: [$prefix]
            }
          }
        }
      }')"
    vector_ingestion_config='{
      "parsingConfiguration": {
        "parsingStrategy": "SMART_PARSING"
      }
    }'
    create_response="$(aws bedrock-agent create-data-source \
      --knowledge-base-id "${KB_ID}" \
      --name "${data_source_name}" \
      --description "Controlled metadata experiment: ${variant}" \
      --data-source-configuration "${data_source_config}" \
      --data-deletion-policy DELETE \
      --vector-ingestion-configuration "${vector_ingestion_config}" \
      --region "${AWS_REGION}" \
      --output json)"
    printf '%s\n' "${create_response}" \
      > "${AWS_DIR}/metadata-experiment-${variant}-create-data-source.json"
    data_source_id="$(jq -r '.dataSource.dataSourceId' <<< "${create_response}")"
  fi

  write_state "${state_keys[$index]}" "${data_source_id}"
  wait_for_data_source "${KB_ID}" "${data_source_id}"

  ingestion_response="$(aws bedrock-agent start-ingestion-job \
    --knowledge-base-id "${KB_ID}" \
    --data-source-id "${data_source_id}" \
    --description "Controlled metadata experiment ingestion: ${variant}" \
    --region "${AWS_REGION}" \
    --output json)"
  printf '%s\n' "${ingestion_response}" \
    > "${AWS_DIR}/metadata-experiment-${variant}-start-ingestion-job.json"
  ingestion_job_id="$(jq -r '.ingestionJob.ingestionJobId' \
    <<< "${ingestion_response}")"
  write_state "${ingestion_state_keys[$index]}" "${ingestion_job_id}"

  wait_for_ingestion "${KB_ID}" "${data_source_id}" "${ingestion_job_id}"
  aws bedrock-agent get-ingestion-job \
    --knowledge-base-id "${KB_ID}" \
    --data-source-id "${data_source_id}" \
    --ingestion-job-id "${ingestion_job_id}" \
    --region "${AWS_REGION}" \
    --output json > "${AWS_DIR}/metadata-experiment-${variant}-ingestion-job.json"

  printf '%s: ' "${variant}"
  jq -c '.ingestionJob.statistics' \
    "${AWS_DIR}/metadata-experiment-${variant}-ingestion-job.json"
done
