#!/usr/bin/env bash

source "$(dirname "$0")/lib.sh"

python_bin="${PYTHON_BIN:-python3}"
require_command "${python_bin}"
load_state

: "${MD_CORPUS_SOURCE_DIR:?Set MD_CORPUS_SOURCE_DIR to the authored Markdown tree}"
: "${MD_CORPUS_ID:?Set MD_CORPUS_ID in config/test.env}"

md_output_dir="${SOURCE_DIR}/md-corpus"
published_manifest="${ARTIFACT_DIR}/published/md-corpus-manifest.json"

previous_manifest_args=()
if [[ -f "${published_manifest}" ]]; then
  previous_manifest_args=(--previous-manifest "${published_manifest}")
  printf 'Change detection baseline: %s\n' "${published_manifest}"
else
  printf 'No published manifest found. Treating this run as an initial load.\n'
fi

"${python_bin}" "${ROOT_DIR}/scripts/21_prepare_md_corpus.py" \
  --source-dir "${MD_CORPUS_SOURCE_DIR}" \
  --output-dir "${md_output_dir}" \
  --corpus-id "${MD_CORPUS_ID}" \
  --report "${TEST_DIR}/md-corpus-preparation-report.json" \
  --embedded-fields "${MD_EMBEDDED_METADATA_FIELDS:-title,section_path,domain,topic}" \
  "${previous_manifest_args[@]}"

printf 'Prepared Markdown corpus: %s\n' "${md_output_dir}"
