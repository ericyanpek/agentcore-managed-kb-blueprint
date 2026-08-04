#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

require_command curl
require_command pdfinfo
require_command shasum

document_path="${SOURCE_DIR}/${DOCUMENT_FILE}"

curl --fail --silent --show-error --location \
  "${SOURCE_URL}" \
  --output "${document_path}"

pdfinfo "${document_path}" > "${SOURCE_DIR}/pdfinfo.txt"
shasum -a 256 "${document_path}" > "${SOURCE_DIR}/sha256.txt"

cp \
  "${ROOT_DIR}/config/${DOCUMENT_FILE}.metadata.json" \
  "${SOURCE_DIR}/${DOCUMENT_FILE}.metadata.json"

printf 'Prepared source: %s\n' "${document_path}"
printf 'SHA-256: %s\n' "$(awk '{print $1}' "${SOURCE_DIR}/sha256.txt")"
printf 'Pages: %s\n' "$(awk -F: '/^Pages:/ {gsub(/^[ \t]+/, "", $2); print $2}' "${SOURCE_DIR}/pdfinfo.txt")"
