#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${root_dir}"

# Every content check runs through rg. Without it, `if rg ...` returns non-zero
# because the command is missing, which reads as "no sensitive content found" and
# the script reports success having scanned nothing.
if ! command -v rg >/dev/null 2>&1; then
  printf 'Repository safety check cannot run: ripgrep (rg) is not installed.\n' >&2
  exit 1
fi

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
# 123456789012 is the account id AWS uses throughout its own documentation, and
# CDK assertion tests must supply some account to synthesize. Excluding it keeps
# the check pointed at real identifiers instead of drowning in placeholders.
check_pattern \
  "standalone 12-digit AWS account ID" \
  '(^|[^[:alnum:]])(?!123456789012)[0-9]{12}([^[:alnum:]]|$)'
check_pattern \
  "account-specific AWS ARN" \
  'arn:(?:aws|aws-cn|aws-us-gov):[^:]+:[^:]*:(?!123456789012)[0-9]{12}:'
check_pattern \
  "absolute local user path" \
  '/(?:Users|home)/[A-Za-z0-9._-]+/'
# SECURITY.md forbids publishing live knowledge base, data source and ingestion
# job IDs, but nothing enforced it: an acceptance record reached the tree with two
# real ones. Such an ID is exactly ten uppercase alphanumerics, and an all-letter
# one is perfectly possible, so do not require a digit — an earlier version of
# this rule did and let a real ID through. Requiring surrounding quotes or backticks
# keeps identifier expressions like attrDataSourceId and placeholders like
# <data-source-id> out of the results; a scanner that cries wolf gets ignored.
# SUPERSEDED is a release status in this repository and happens to be exactly ten
# uppercase letters; any further all-caps ten-character constant must be excluded
# here too, or the scan turns into noise and stops being read.
check_pattern \
  "live Bedrock resource ID" \
  '["'"'"'`](?!SUPERSEDED)[A-Z0-9]{10}["'"'"'`]'

if ((failures)); then
  exit 1
fi

printf 'Repository safety check passed.\n'
