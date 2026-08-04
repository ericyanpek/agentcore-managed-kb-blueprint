#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

python_bin="${PYTHON_BIN:-python3}"
require_command "${python_bin}"
load_state

semantic_corpus_id="${SEMANTIC_CORPUS_ID:-aws-games-industry-lens-2026-07-31-semantic-v1}"
semantic_source_dir="${SOURCE_DIR}/semantic-chunks"
text_document_path="${SOURCE_DIR}/${TEXT_DOCUMENT_FILE}"

if [[ ! -f "${text_document_path}" ]]; then
  printf 'Text repair source is missing. Run scripts/09_prepare_text_repair.sh first.\n' >&2
  exit 1
fi

"${python_bin}" "${ROOT_DIR}/scripts/14_prepare_semantic_chunks.py" \
  --input "${text_document_path}" \
  --output-dir "${semantic_source_dir}" \
  --report "${TEST_DIR}/semantic-chunking-preparation-report.json" \
  --corpus-id "${semantic_corpus_id}" \
  --source-url "${SOURCE_URL}" \
  --start-page 8 \
  --end-page 141 \
  --target-chars 420 \
  --max-chars 600 \
  --min-chars 100

printf 'Prepared semantic corpus: %s\n' "${semantic_source_dir}"
