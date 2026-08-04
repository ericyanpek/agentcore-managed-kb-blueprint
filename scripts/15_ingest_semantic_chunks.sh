#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

require_command aws
require_command jq
load_state

: "${KB_ID:?Run scripts/02_provision.sh first}"

semantic_corpus_id="${SEMANTIC_CORPUS_ID:-aws-games-industry-lens-2026-07-31-semantic-v1}"
semantic_s3_prefix="${SEMANTIC_S3_PREFIX:-documents/games-industry-lens-semantic/2026-07-31-v1}"
semantic_data_source_name="${SEMANTIC_DATA_SOURCE_NAME:-}"
if [[ -z "${semantic_data_source_name}" ]]; then
  semantic_data_source_name="games-industry-lens-semantic-s3-${RUN_ID}"
fi
semantic_source_dir="${SOURCE_DIR}/semantic-chunks"

if [[ ! -f "${semantic_source_dir}/manifest.json" ]]; then
  printf 'Semantic corpus is missing. Run scripts/14_prepare_semantic_chunks.sh first.\n' >&2
  exit 1
fi

current_policy="$(aws iam get-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${ROLE_POLICY_NAME}" \
  --query 'PolicyDocument' \
  --output json)"

semantic_object_arn="arn:aws:s3:::${BUCKET_NAME}/${semantic_s3_prefix}/*"
updated_policy="$(jq \
  --arg prefix "${semantic_s3_prefix}" \
  --arg object_arn "${semantic_object_arn}" \
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
  ' <<< "${current_policy}")"

aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${ROLE_POLICY_NAME}" \
  --policy-document "${updated_policy}"

printf '%s\n' "${updated_policy}" \
  > "${AWS_DIR}/semantic-chunking-iam-role-policy.json"

aws s3 sync \
  "${semantic_source_dir}/" \
  "s3://${BUCKET_NAME}/${semantic_s3_prefix}/" \
  --exclude "manifest.json" \
  --only-show-errors \
  --region "${AWS_REGION}"

semantic_data_source_id="$(aws bedrock-agent list-data-sources \
  --knowledge-base-id "${KB_ID}" \
  --region "${AWS_REGION}" \
  --output json |
  jq -r --arg name "${semantic_data_source_name}" \
    '.dataSourceSummaries[] | select(.name == $name) | .dataSourceId' |
  head -n 1)"

if [[ -z "${semantic_data_source_id}" ]]; then
  data_source_config="$(jq -n \
    --arg bucket "${BUCKET_NAME}" \
    --arg account "${AWS_ACCOUNT_ID}" \
    --arg prefix "${semantic_s3_prefix}/" \
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

  data_source_response="$(aws bedrock-agent create-data-source \
    --knowledge-base-id "${KB_ID}" \
    --name "${semantic_data_source_name}" \
    --description "Structure-aware semantic chunks for the AWS Games Industry Lens" \
    --data-source-configuration "${data_source_config}" \
    --data-deletion-policy DELETE \
    --vector-ingestion-configuration "${vector_ingestion_config}" \
    --region "${AWS_REGION}" \
    --output json)"
  printf '%s\n' "${data_source_response}" \
    > "${AWS_DIR}/semantic-chunking-create-data-source.json"
  semantic_data_source_id="$(jq -r '.dataSource.dataSourceId' \
    <<< "${data_source_response}")"
fi

write_state SEMANTIC_DATA_SOURCE_ID "${semantic_data_source_id}"
write_state SEMANTIC_CORPUS_ID "${semantic_corpus_id}"
wait_for_data_source "${KB_ID}" "${semantic_data_source_id}"

ingestion_response="$(aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${semantic_data_source_id}" \
  --description "Structure-aware semantic chunking experiment ingestion" \
  --region "${AWS_REGION}" \
  --output json)"
printf '%s\n' "${ingestion_response}" \
  > "${AWS_DIR}/semantic-chunking-start-ingestion-job.json"
semantic_ingestion_job_id="$(jq -r '.ingestionJob.ingestionJobId' \
  <<< "${ingestion_response}")"
write_state SEMANTIC_INGESTION_JOB_ID "${semantic_ingestion_job_id}"

wait_for_ingestion \
  "${KB_ID}" \
  "${semantic_data_source_id}" \
  "${semantic_ingestion_job_id}"

aws bedrock-agent get-data-source \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${semantic_data_source_id}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/semantic-chunking-final-data-source.json"

aws bedrock-agent get-ingestion-job \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${semantic_data_source_id}" \
  --ingestion-job-id "${semantic_ingestion_job_id}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/semantic-chunking-ingestion-job.json"

jq '.ingestionJob.statistics' \
  "${AWS_DIR}/semantic-chunking-ingestion-job.json"
