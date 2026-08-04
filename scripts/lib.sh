#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/config/test.env}"

if [[ ! -f "${ENV_FILE}" ]]; then
  printf 'Missing environment file: %s\n' "${ENV_FILE}" >&2
  exit 1
fi

set -a
# shellcheck source=/dev/null
source "${ENV_FILE}"
set +a

ARTIFACT_DIR="${ROOT_DIR}/artifacts/${RUN_ID}"
SOURCE_DIR="${ARTIFACT_DIR}/source"
AWS_DIR="${ARTIFACT_DIR}/aws"
TEST_DIR="${ARTIFACT_DIR}/tests"
STATE_FILE="${ARTIFACT_DIR}/state.env"

mkdir -p "${SOURCE_DIR}" "${AWS_DIR}" "${TEST_DIR}"

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'Required command not found: %s\n' "$1" >&2
    exit 1
  }
}

load_state() {
  if [[ -f "${STATE_FILE}" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${STATE_FILE}"
    set +a
  fi
}

write_state() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"

  if [[ -f "${STATE_FILE}" ]]; then
    awk -F= -v key="${key}" '$1 != key { print }' "${STATE_FILE}" > "${tmp}"
  fi
  printf '%s=%q\n' "${key}" "${value}" >> "${tmp}"
  mv "${tmp}" "${STATE_FILE}"
}

wait_for_kb() {
  local kb_id="$1"
  local status
  local _attempt

  for _attempt in {1..60}; do
    status="$(aws bedrock-agent get-knowledge-base \
      --knowledge-base-id "${kb_id}" \
      --region "${AWS_REGION}" \
      --query 'knowledgeBase.status' \
      --output text)"
    printf 'Knowledge base status: %s\n' "${status}"
    case "${status}" in
      ACTIVE)
        return 0
        ;;
      FAILED|DELETE_UNSUCCESSFUL)
        return 1
        ;;
    esac
    sleep 10
  done

  printf 'Timed out waiting for knowledge base %s\n' "${kb_id}" >&2
  return 1
}

wait_for_data_source() {
  local kb_id="$1"
  local data_source_id="$2"
  local status
  local _attempt

  for _attempt in {1..60}; do
    status="$(aws bedrock-agent get-data-source \
      --knowledge-base-id "${kb_id}" \
      --data-source-id "${data_source_id}" \
      --region "${AWS_REGION}" \
      --query 'dataSource.status' \
      --output text)"
    printf 'Data source status: %s\n' "${status}"
    case "${status}" in
      AVAILABLE)
        return 0
        ;;
      FAILED|DELETE_UNSUCCESSFUL)
        return 1
        ;;
    esac
    sleep 10
  done

  printf 'Timed out waiting for data source %s\n' "${data_source_id}" >&2
  return 1
}

wait_for_ingestion() {
  local kb_id="$1"
  local data_source_id="$2"
  local ingestion_job_id="$3"
  local status
  local _attempt

  for _attempt in {1..120}; do
    status="$(aws bedrock-agent get-ingestion-job \
      --knowledge-base-id "${kb_id}" \
      --data-source-id "${data_source_id}" \
      --ingestion-job-id "${ingestion_job_id}" \
      --region "${AWS_REGION}" \
      --query 'ingestionJob.status' \
      --output text)"
    printf 'Ingestion job status: %s\n' "${status}"
    case "${status}" in
      COMPLETE)
        return 0
        ;;
      FAILED|STOPPED)
        return 1
        ;;
    esac
    sleep 10
  done

  printf 'Timed out waiting for ingestion job %s\n' "${ingestion_job_id}" >&2
  return 1
}
