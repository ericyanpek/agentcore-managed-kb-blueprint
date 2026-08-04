#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

require_command aws
require_command jq
load_state

: "${KB_ID:?Run scripts/02_provision.sh first}"
: "${TEXT_DOCUMENT_FILE:?Missing TEXT_DOCUMENT_FILE in local configuration}"
: "${TEXT_DOCUMENT_ID:?Missing TEXT_DOCUMENT_ID in local configuration}"
: "${TEXT_S3_PREFIX:?Missing TEXT_S3_PREFIX in local configuration}"
: "${TEXT_DATA_SOURCE_NAME:?Missing TEXT_DATA_SOURCE_NAME in local configuration}"

text_document_file="${TEXT_DOCUMENT_FILE}"
text_document_id="${TEXT_DOCUMENT_ID}"
text_s3_prefix="${TEXT_S3_PREFIX}"
text_data_source_name="${TEXT_DATA_SOURCE_NAME}"
text_document_path="${SOURCE_DIR}/${text_document_file}"
text_metadata_path="${SOURCE_DIR}/${text_document_file}.metadata.json"

if [[ ! -f "${text_document_path}" || ! -f "${text_metadata_path}" ]]; then
  printf 'Text source files are missing. Run the extraction step first.\n' >&2
  exit 1
fi

current_policy="$(aws iam get-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${ROLE_POLICY_NAME}" \
  --query 'PolicyDocument' \
  --output json)"

text_object_arn="arn:aws:s3:::${BUCKET_NAME}/${text_s3_prefix}/*"
updated_policy="$(jq \
  --arg prefix "${text_s3_prefix}" \
  --arg object_arn "${text_object_arn}" \
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

printf '%s\n' "${updated_policy}" > "${AWS_DIR}/text-repair-iam-role-policy.json"

aws s3 cp \
  "${text_document_path}" \
  "s3://${BUCKET_NAME}/${text_s3_prefix}/${text_document_file}" \
  --content-type "text/markdown; charset=utf-8" \
  --only-show-errors \
  --region "${AWS_REGION}"

aws s3 cp \
  "${text_metadata_path}" \
  "s3://${BUCKET_NAME}/${text_s3_prefix}/${text_document_file}.metadata.json" \
  --content-type "application/json" \
  --only-show-errors \
  --region "${AWS_REGION}"

aws s3api head-object \
  --bucket "${BUCKET_NAME}" \
  --key "${text_s3_prefix}/${text_document_file}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/text-repair-s3-document-head.json"

aws s3api head-object \
  --bucket "${BUCKET_NAME}" \
  --key "${text_s3_prefix}/${text_document_file}.metadata.json" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/text-repair-s3-metadata-head.json"

text_data_source_id="$(aws bedrock-agent list-data-sources \
  --knowledge-base-id "${KB_ID}" \
  --region "${AWS_REGION}" \
  --output json |
  jq -r --arg name "${text_data_source_name}" \
    '.dataSourceSummaries[] | select(.name == $name) | .dataSourceId' |
  head -n 1)"

if [[ -z "${text_data_source_id}" ]]; then
  data_source_config="$(jq -n \
    --arg bucket "${BUCKET_NAME}" \
    --arg account "${AWS_ACCOUNT_ID}" \
    --arg prefix "${text_s3_prefix}/" \
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
    --name "${text_data_source_name}" \
    --description "UTF-8 Markdown repair source for the AWS Games Industry Lens" \
    --data-source-configuration "${data_source_config}" \
    --data-deletion-policy DELETE \
    --vector-ingestion-configuration "${vector_ingestion_config}" \
    --region "${AWS_REGION}" \
    --output json)"
  printf '%s\n' "${data_source_response}" \
    > "${AWS_DIR}/text-repair-create-data-source.json"
  text_data_source_id="$(jq -r '.dataSource.dataSourceId' <<< "${data_source_response}")"
fi

write_state TEXT_DATA_SOURCE_ID "${text_data_source_id}"
write_state TEXT_DOCUMENT_ID "${text_document_id}"
wait_for_data_source "${KB_ID}" "${text_data_source_id}"

ingestion_response="$(aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${text_data_source_id}" \
  --description "Initial UTF-8 Markdown repair ingestion" \
  --region "${AWS_REGION}" \
  --output json)"
printf '%s\n' "${ingestion_response}" \
  > "${AWS_DIR}/text-repair-start-ingestion-job.json"
text_ingestion_job_id="$(jq -r '.ingestionJob.ingestionJobId' <<< "${ingestion_response}")"
write_state TEXT_INGESTION_JOB_ID "${text_ingestion_job_id}"

wait_for_ingestion "${KB_ID}" "${text_data_source_id}" "${text_ingestion_job_id}"

aws bedrock-agent get-data-source \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${text_data_source_id}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/text-repair-final-data-source.json"

aws bedrock-agent get-ingestion-job \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${text_data_source_id}" \
  --ingestion-job-id "${text_ingestion_job_id}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/text-repair-ingestion-job.json"

jq '.ingestionJob.statistics' "${AWS_DIR}/text-repair-ingestion-job.json"
