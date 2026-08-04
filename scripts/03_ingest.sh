#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

require_command aws
require_command jq
load_state

: "${KB_ID:?Run scripts/02_provision.sh first}"

document_path="${SOURCE_DIR}/${DOCUMENT_FILE}"
metadata_path="${SOURCE_DIR}/${DOCUMENT_FILE}.metadata.json"

if [[ ! -f "${document_path}" || ! -f "${metadata_path}" ]]; then
  printf 'Source files are missing. Run scripts/01_prepare_source.sh first.\n' >&2
  exit 1
fi

aws s3 cp \
  "${document_path}" \
  "s3://${BUCKET_NAME}/${S3_PREFIX}/${DOCUMENT_FILE}" \
  --only-show-errors \
  --region "${AWS_REGION}"

aws s3 cp \
  "${metadata_path}" \
  "s3://${BUCKET_NAME}/${S3_PREFIX}/${DOCUMENT_FILE}.metadata.json" \
  --only-show-errors \
  --region "${AWS_REGION}"

aws s3api head-object \
  --bucket "${BUCKET_NAME}" \
  --key "${S3_PREFIX}/${DOCUMENT_FILE}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/s3-document-head.json"

aws s3api head-object \
  --bucket "${BUCKET_NAME}" \
  --key "${S3_PREFIX}/${DOCUMENT_FILE}.metadata.json" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/s3-metadata-head.json"

data_source_id="$(aws bedrock-agent list-data-sources \
  --knowledge-base-id "${KB_ID}" \
  --region "${AWS_REGION}" \
  --output json |
  jq -r --arg name "${DATA_SOURCE_NAME}" '.dataSourceSummaries[] | select(.name == $name) | .dataSourceId' |
  head -n 1)"

if [[ -z "${data_source_id}" ]]; then
  data_source_config="$(jq -n \
    --arg bucket "${BUCKET_NAME}" \
    --arg account "${AWS_ACCOUNT_ID}" \
    --arg prefix "${S3_PREFIX}/" \
    '{
      type: "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
      managedKnowledgeBaseConnectorConfiguration: {
        deletionProtectionConfiguration: {
          deletionProtectionStatus: "ENABLED",
          deletionProtectionThreshold: 50
        },
        mediaExtractionConfiguration: {
          imageExtractionConfiguration: {
            imageExtractionStatus: "ENABLED"
          }
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
    --name "${DATA_SOURCE_NAME}" \
    --description "Isolated S3 source for the AWS Games Industry Lens PDF" \
    --data-source-configuration "${data_source_config}" \
    --data-deletion-policy DELETE \
    --vector-ingestion-configuration "${vector_ingestion_config}" \
    --region "${AWS_REGION}" \
    --output json)"
  printf '%s\n' "${data_source_response}" > "${AWS_DIR}/create-data-source.json"
  data_source_id="$(jq -r '.dataSource.dataSourceId' <<< "${data_source_response}")"
fi

write_state DATA_SOURCE_ID "${data_source_id}"
wait_for_data_source "${KB_ID}" "${data_source_id}"

ingestion_response="$(aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${data_source_id}" \
  --description "Initial ingestion for ${DOCUMENT_FILE}" \
  --region "${AWS_REGION}" \
  --output json)"
printf '%s\n' "${ingestion_response}" > "${AWS_DIR}/start-ingestion-job.json"
ingestion_job_id="$(jq -r '.ingestionJob.ingestionJobId' <<< "${ingestion_response}")"
write_state INGESTION_JOB_ID "${ingestion_job_id}"

wait_for_ingestion "${KB_ID}" "${data_source_id}" "${ingestion_job_id}"

aws bedrock-agent get-data-source \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${data_source_id}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/data-source.json"

aws bedrock-agent get-ingestion-job \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${data_source_id}" \
  --ingestion-job-id "${ingestion_job_id}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/ingestion-job.json"

jq '.ingestionJob.statistics' "${AWS_DIR}/ingestion-job.json"
