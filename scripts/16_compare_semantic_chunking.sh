#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

python_bin="${PYTHON_BIN:-python3}"
require_command "${python_bin}"
load_state

: "${KB_ID:?Run scripts/02_provision.sh first}"
: "${TEXT_DOCUMENT_ID:?Run scripts/10_ingest_text_repair.sh first}"
: "${SEMANTIC_CORPUS_ID:?Run scripts/15_ingest_semantic_chunks.sh first}"

"${python_bin}" "${ROOT_DIR}/scripts/16_compare_semantic_chunking.py" \
  --region "${AWS_REGION}" \
  --knowledge-base-id "${KB_ID}" \
  --baseline-document-id "${TEXT_DOCUMENT_ID}" \
  --semantic-corpus-id "${SEMANTIC_CORPUS_ID}" \
  --number-of-results 10 \
  --output-dir "${TEST_DIR}"
