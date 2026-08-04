#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${root_dir}"

failures=0

check_pattern() {
  local description="$1"
  local pattern="$2"

  if rg \
    --hidden \
    --glob '!.git/**' \
    --line-number \
    --pcre2 \
    "${pattern}" \
    .; then
    printf 'Repository safety check failed: %s\n' "${description}" >&2
    failures=1
  fi
}

for path in config/test.env artifacts tmp .venv .venv-agentic; do
  if git ls-files --error-unmatch "${path}" >/dev/null 2>&1 ||
    git ls-files "${path}/**" | grep -q .; then
    printf 'Repository safety check failed: tracked local path %s\n' "${path}" >&2
    failures=1
  fi
done

check_pattern \
  "possible AWS access key ID" \
  '\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'
check_pattern \
  "standalone 12-digit AWS account ID" \
  '(^|[^[:alnum:]])[0-9]{12}([^[:alnum:]]|$)'
check_pattern \
  "account-specific AWS ARN" \
  'arn:(?:aws|aws-cn|aws-us-gov):[^:]+:[^:]*:[0-9]{12}:'
check_pattern \
  "absolute local user path" \
  '/(?:Users|home)/[A-Za-z0-9._-]+/'

if ((failures)); then
  exit 1
fi

printf 'Repository safety check passed.\n'
