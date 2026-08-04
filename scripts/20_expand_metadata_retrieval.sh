#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

python_bin="${PYTHON_BIN:-python3}"
require_command "${python_bin}"
load_state

: "${KB_ID:?Run scripts/02_provision.sh first}"
: "${METADATA_NONE_DATA_SOURCE_ID:?Run scripts/18_ingest_metadata_experiment.sh first}"
: "${METADATA_FILTER_DATA_SOURCE_ID:?Run scripts/18_ingest_metadata_experiment.sh first}"
: "${METADATA_EMBEDDED_DATA_SOURCE_ID:?Run scripts/18_ingest_metadata_experiment.sh first}"

corpus_dir="${SOURCE_DIR}/metadata-experiment/filter-metadata"
preparation_report="${TEST_DIR}/metadata-experiment-preparation-report.json"

if [[ ! -d "${corpus_dir}" || ! -f "${preparation_report}" ]]; then
  printf 'Metadata experiment corpus is missing. Run scripts/17_prepare_metadata_experiment.sh first.\n' >&2
  exit 1
fi

extra_args=()
if [[ "${RUNTIME_FILTER_ONLY:-0}" == "1" ]]; then
  extra_args+=(--runtime-filter-only)
fi

"${python_bin}" "${ROOT_DIR}/scripts/20_expand_metadata_retrieval.py" \
  --region "${AWS_REGION}" \
  --knowledge-base-id "${KB_ID}" \
  --no-metadata-data-source-id "${METADATA_NONE_DATA_SOURCE_ID}" \
  --filter-metadata-data-source-id "${METADATA_FILTER_DATA_SOURCE_ID}" \
  --embedded-metadata-data-source-id "${METADATA_EMBEDDED_DATA_SOURCE_ID}" \
  --corpus-dir "${corpus_dir}" \
  --preparation-report "${preparation_report}" \
  --number-of-results 10 \
  --count-per-generated-category 12 \
  --output-dir "${TEST_DIR}" \
  "${extra_args[@]}"
