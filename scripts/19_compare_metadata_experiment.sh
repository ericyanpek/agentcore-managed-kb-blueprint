#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

python_bin="${PYTHON_BIN:-python3}"
require_command "${python_bin}"
load_state

: "${KB_ID:?Run scripts/02_provision.sh first}"
: "${METADATA_NONE_DATA_SOURCE_ID:?Run scripts/18_ingest_metadata_experiment.sh first}"
: "${METADATA_FILTER_DATA_SOURCE_ID:?Run scripts/18_ingest_metadata_experiment.sh first}"
: "${METADATA_EMBEDDED_DATA_SOURCE_ID:?Run scripts/18_ingest_metadata_experiment.sh first}"

"${python_bin}" "${ROOT_DIR}/scripts/19_compare_metadata_experiment.py" \
  --region "${AWS_REGION}" \
  --knowledge-base-id "${KB_ID}" \
  --no-metadata-data-source-id "${METADATA_NONE_DATA_SOURCE_ID}" \
  --filter-metadata-data-source-id "${METADATA_FILTER_DATA_SOURCE_ID}" \
  --embedded-metadata-data-source-id "${METADATA_EMBEDDED_DATA_SOURCE_ID}" \
  --number-of-results 10 \
  --output-dir "${TEST_DIR}"
