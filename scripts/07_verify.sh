#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

require_command aws
require_command jq
load_state

: "${KB_ID:?Missing KB_ID in state}"
: "${DATA_SOURCE_ID:?Missing DATA_SOURCE_ID in state}"

aws bedrock-agent get-knowledge-base \
  --knowledge-base-id "${KB_ID}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/final-knowledge-base.json"

aws bedrock-agent get-data-source \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${DATA_SOURCE_ID}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/final-data-source.json"

aws bedrock-agent list-ingestion-jobs \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${DATA_SOURCE_ID}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/final-ingestion-jobs.json"

aws s3api list-objects-v2 \
  --bucket "${BUCKET_NAME}" \
  --prefix "${S3_PREFIX}/" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/final-s3-objects.json"

aws s3api list-object-versions \
  --bucket "${BUCKET_NAME}" \
  --prefix "${S3_PREFIX}/" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/final-s3-object-versions.json"

aws s3api get-public-access-block \
  --bucket "${BUCKET_NAME}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/final-s3-public-access-block.json"

aws s3api get-bucket-encryption \
  --bucket "${BUCKET_NAME}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/final-s3-encryption.json"

aws s3api get-bucket-versioning \
  --bucket "${BUCKET_NAME}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/final-s3-versioning.json"

aws s3api get-bucket-lifecycle-configuration \
  --bucket "${BUCKET_NAME}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/final-s3-lifecycle.json"

aws iam get-role \
  --role-name "${ROLE_NAME}" \
  --output json > "${AWS_DIR}/final-iam-role.json"

aws iam get-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${ROLE_POLICY_NAME}" \
  --output json > "${AWS_DIR}/final-iam-role-policy.json"

jq -n \
  --slurpfile kb "${AWS_DIR}/final-knowledge-base.json" \
  --slurpfile ds "${AWS_DIR}/final-data-source.json" \
  --slurpfile jobs "${AWS_DIR}/final-ingestion-jobs.json" \
  --slurpfile objects "${AWS_DIR}/final-s3-objects.json" \
  --slurpfile versions "${AWS_DIR}/final-s3-object-versions.json" \
  --slurpfile role "${AWS_DIR}/final-iam-role.json" \
  '{
    knowledgeBase: {
      id: $kb[0].knowledgeBase.knowledgeBaseId,
      status: $kb[0].knowledgeBase.status,
      type: $kb[0].knowledgeBase.knowledgeBaseConfiguration.type,
      embeddingModelType: $kb[0].knowledgeBase.knowledgeBaseConfiguration.managedKnowledgeBaseConfiguration.embeddingModelType
    },
    dataSource: {
      id: $ds[0].dataSource.dataSourceId,
      status: $ds[0].dataSource.status,
      type: $ds[0].dataSource.dataSourceConfiguration.type,
      parsingStrategy: $ds[0].dataSource.vectorIngestionConfiguration.parsingConfiguration.parsingStrategy,
      deletionPolicy: $ds[0].dataSource.dataDeletionPolicy
    },
    ingestion: {
      latestStatus: $jobs[0].ingestionJobSummaries[0].status,
      jobCount: ($jobs[0].ingestionJobSummaries | length)
    },
    sourceStorage: {
      currentObjectCount: ($objects[0].Contents | length),
      versionCount: ($versions[0].Versions | length),
      deleteMarkerCount: ($versions[0].DeleteMarkers // [] | length)
    },
    trustPolicySourceArn: $role[0].Role.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals."AWS:SourceArn"
  }' > "${AWS_DIR}/final-verification-summary.json"

cat "${AWS_DIR}/final-verification-summary.json"
