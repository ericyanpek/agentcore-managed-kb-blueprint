#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

python_bin="${PYTHON_BIN:-python3}"
require_command "${python_bin}"
load_state

text_document_path="${SOURCE_DIR}/${TEXT_DOCUMENT_FILE}"
text_metadata_path="${SOURCE_DIR}/${TEXT_DOCUMENT_FILE}.metadata.json"

"${python_bin}" "${ROOT_DIR}/scripts/09_extract_pdf_to_markdown.py" \
  --input "${SOURCE_DIR}/${DOCUMENT_FILE}" \
  --output "${text_document_path}" \
  --report "${TEST_DIR}/pdf-to-markdown-report.json" \
  --source-url "${SOURCE_URL}" \
  --expected-pages 146 \
  --max-empty-pages 1 \
  --min-cjk-ratio 0.50

cp \
  "${ROOT_DIR}/config/${TEXT_DOCUMENT_FILE}.metadata.json" \
  "${text_metadata_path}"

printf 'Prepared text repair source: %s\n' "${text_document_path}"
printf 'Prepared metadata: %s\n' "${text_metadata_path}"
