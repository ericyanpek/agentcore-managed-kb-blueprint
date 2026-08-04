#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

python_bin="${PYTHON_BIN:-python3}"
require_command "${python_bin}"
load_state

semantic_source_dir="${SOURCE_DIR}/semantic-chunks"
metadata_source_dir="${SOURCE_DIR}/metadata-experiment"

if [[ ! -f "${semantic_source_dir}/manifest.json" ]]; then
  printf 'Semantic corpus is missing. Run scripts/14_prepare_semantic_chunks.sh first.\n' >&2
  exit 1
fi

"${python_bin}" "${ROOT_DIR}/scripts/17_prepare_metadata_experiment.py" \
  --source-dir "${semantic_source_dir}" \
  --output-dir "${metadata_source_dir}" \
  --report "${TEST_DIR}/metadata-experiment-preparation-report.json"

printf 'Prepared metadata experiment corpus: %s\n' "${metadata_source_dir}"
