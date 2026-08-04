#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

require_command aws
require_command jq

aws sts get-caller-identity \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/caller-identity.json"

caller_account_id="$(jq -r '.Account' "${AWS_DIR}/caller-identity.json")"
if [[ -n "${AWS_ACCOUNT_ID:-}" && "${AWS_ACCOUNT_ID}" != "${caller_account_id}" ]]; then
  printf 'Configured AWS_ACCOUNT_ID does not match the active AWS identity.\n' >&2
  exit 1
fi

AWS_ACCOUNT_ID="${caller_account_id}"
BUCKET_NAME="${BUCKET_NAME:-${PROJECT_TAG}-${AWS_ACCOUNT_ID}-${AWS_REGION}}"
export AWS_ACCOUNT_ID BUCKET_NAME
write_state AWS_ACCOUNT_ID "${AWS_ACCOUNT_ID}"
write_state BUCKET_NAME "${BUCKET_NAME}"

if ! aws s3api head-bucket --bucket "${BUCKET_NAME}" 2>/dev/null; then
  create_bucket_args=(
    --bucket "${BUCKET_NAME}"
    --region "${AWS_REGION}"
    --output json
  )
  if [[ "${AWS_REGION}" != "us-east-1" ]]; then
    create_bucket_args+=(
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}"
    )
  fi
  aws s3api create-bucket "${create_bucket_args[@]}" \
    > "${AWS_DIR}/create-bucket.json"
fi

aws s3api put-public-access-block \
  --bucket "${BUCKET_NAME}" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-encryption \
  --bucket "${BUCKET_NAME}" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":false}]}'

aws s3api put-bucket-versioning \
  --bucket "${BUCKET_NAME}" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-tagging \
  --bucket "${BUCKET_NAME}" \
  --tagging "TagSet=[{Key=Project,Value=${PROJECT_TAG}},{Key=Environment,Value=test},{Key=ManagedBy,Value=codex-runbook}]"

lifecycle_json="$(jq -n '{
  Rules: [
    {
      ID: "abort-incomplete-multipart-uploads",
      Status: "Enabled",
      Filter: {Prefix: ""},
      AbortIncompleteMultipartUpload: {DaysAfterInitiation: 7}
    },
    {
      ID: "expire-old-noncurrent-versions",
      Status: "Enabled",
      Filter: {Prefix: ""},
      NoncurrentVersionExpiration: {NoncurrentDays: 30}
    }
  ]
}')"
aws s3api put-bucket-lifecycle-configuration \
  --bucket "${BUCKET_NAME}" \
  --lifecycle-configuration "${lifecycle_json}"

trust_policy="$(jq -n \
  --arg account "${AWS_ACCOUNT_ID}" \
  --arg region "${AWS_REGION}" \
  '{
    Version: "2012-10-17",
    Statement: [
      {
        Effect: "Allow",
        Principal: {Service: "bedrock.amazonaws.com"},
        Action: "sts:AssumeRole",
        Condition: {
          StringEquals: {"aws:SourceAccount": $account},
          ArnLike: {"AWS:SourceArn": ("arn:aws:bedrock:" + $region + ":" + $account + ":knowledge-base/*")}
        }
      }
    ]
  }')"

if ! aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  aws iam create-role \
    --role-name "${ROLE_NAME}" \
    --description "Least-privilege service role for the Games Industry Lens managed knowledge base test" \
    --assume-role-policy-document "${trust_policy}" \
    --tags \
      Key=Project,Value="${PROJECT_TAG}" \
      Key=Environment,Value=test \
      Key=ManagedBy,Value=codex-runbook \
    --output json > "${AWS_DIR}/create-role.json"
fi

data_access_policy="$(jq -n \
  --arg account "${AWS_ACCOUNT_ID}" \
  --arg bucket "${BUCKET_NAME}" \
  --arg prefix "${S3_PREFIX}" \
  '{
    Version: "2012-10-17",
    Statement: [
      {
        Sid: "ListOnlyTheManagedKBPrefix",
        Effect: "Allow",
        Action: ["s3:ListBucket"],
        Resource: [("arn:aws:s3:::" + $bucket)],
        Condition: {
          StringEquals: {"aws:ResourceAccount": $account},
          "ForAnyValue:StringLike": {
            "s3:prefix": [$prefix, ($prefix + "/*")]
          }
        }
      },
      {
        Sid: "ReadOnlyTheManagedKBPrefix",
        Effect: "Allow",
        Action: ["s3:GetObject"],
        Resource: [("arn:aws:s3:::" + $bucket + "/" + $prefix + "/*")],
        Condition: {
          StringEquals: {"aws:ResourceAccount": $account}
        }
      }
    ]
  }')"

aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${ROLE_POLICY_NAME}" \
  --policy-document "${data_access_policy}"

role_arn="$(aws iam get-role \
  --role-name "${ROLE_NAME}" \
  --query 'Role.Arn' \
  --output text)"

kb_id="$(aws bedrock-agent list-knowledge-bases \
  --region "${AWS_REGION}" \
  --output json |
  jq -r --arg name "${KB_NAME}" '.knowledgeBaseSummaries[] | select(.name == $name) | .knowledgeBaseId' |
  head -n 1)"

if [[ -z "${kb_id}" ]]; then
  kb_config='{"type":"MANAGED","managedKnowledgeBaseConfiguration":{"embeddingModelType":"MANAGED"}}'
  kb_response="$(aws bedrock-agent create-knowledge-base \
    --name "${KB_NAME}" \
    --description "Managed KB test for the AWS Well-Architected Games Industry Lens" \
    --role-arn "${role_arn}" \
    --knowledge-base-configuration "${kb_config}" \
    --tags "Project=${PROJECT_TAG},Environment=test,ManagedBy=codex-runbook" \
    --region "${AWS_REGION}" \
    --output json)"
  printf '%s\n' "${kb_response}" > "${AWS_DIR}/create-knowledge-base.json"
  kb_id="$(jq -r '.knowledgeBase.knowledgeBaseId' <<< "${kb_response}")"
fi

write_state KB_ID "${kb_id}"
wait_for_kb "${kb_id}"

kb_arn="$(aws bedrock-agent get-knowledge-base \
  --knowledge-base-id "${kb_id}" \
  --region "${AWS_REGION}" \
  --query 'knowledgeBase.knowledgeBaseArn' \
  --output text)"

scoped_trust_policy="$(jq -n \
  --arg account "${AWS_ACCOUNT_ID}" \
  --arg kb_arn "${kb_arn}" \
  '{
    Version: "2012-10-17",
    Statement: [
      {
        Effect: "Allow",
        Principal: {Service: "bedrock.amazonaws.com"},
        Action: "sts:AssumeRole",
        Condition: {
          StringEquals: {
            "aws:SourceAccount": $account,
            "AWS:SourceArn": $kb_arn
          }
        }
      }
    ]
  }')"

aws iam update-assume-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-document "${scoped_trust_policy}"

aws bedrock-agent get-knowledge-base \
  --knowledge-base-id "${kb_id}" \
  --region "${AWS_REGION}" \
  --output json > "${AWS_DIR}/knowledge-base.json"

aws s3api get-public-access-block \
  --bucket "${BUCKET_NAME}" \
  --output json > "${AWS_DIR}/s3-public-access-block.json"
aws s3api get-bucket-encryption \
  --bucket "${BUCKET_NAME}" \
  --output json > "${AWS_DIR}/s3-encryption.json"
aws s3api get-bucket-versioning \
  --bucket "${BUCKET_NAME}" \
  --output json > "${AWS_DIR}/s3-versioning.json"
aws iam get-role \
  --role-name "${ROLE_NAME}" \
  --output json > "${AWS_DIR}/iam-role.json"
aws iam get-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${ROLE_POLICY_NAME}" \
  --output json > "${AWS_DIR}/iam-role-policy.json"

printf 'Managed knowledge base ready: %s\n' "${kb_id}"
