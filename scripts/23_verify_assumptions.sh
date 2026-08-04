#!/usr/bin/env bash

# Verifies the two assumptions that decide the shape of the Markdown ingestion
# pipeline in scripts/22_incremental_ingest.sh. Both are unverified as of
# 2026-08-04 because the documented quotas do not answer them.
#
# A1 StartIngestionJob rate limit for managed knowledge bases
#   The Bedrock general quota page lists "(Knowledge Bases) StartIngestionJob
#   requests per second = 0.1, not adjustable" but publishes no managed
#   equivalent, while raising concurrent jobs per knowledge base from 1 to 50.
#   Method: submit BURST_COUNT jobs back to back and record which calls raise
#   ThrottlingException.
#   Reject A1 (no 0.1/s limit) when every call is accepted and the observed
#   interval is far below 10 s. Confirm A1 when calls are throttled at roughly
#   one per 10 s. Consequence: if rejected, the sequential gate in the AWS
#   auto-sync reference architecture is unnecessary on managed knowledge bases.
#
# A2 Whether a connector sync removes directly ingested documents
#   The direct ingestion page warns that documents ingested into an S3 data
#   source are not written back to the bucket and "aren't removed or
#   overwritten if you sync your data source" only when they also exist in S3.
#   This test covers the case the warning implies but does not state: a document
#   present in the index via IngestKnowledgeBaseDocuments and absent from the
#   S3 prefix.
#   Method: ingest a probe document directly, confirm it is retrievable, run a
#   full StartIngestionJob, then retrieve again.
#   Confirm A2 (sync deletes the probe) when the post-sync retrieval returns
#   nothing. Consequence: if confirmed, the fast path must always write to S3
#   first, and reconciliation cannot run against a partially populated prefix.
#
# Requires bedrock-agent control plane and bedrock-agent-runtime permissions.
# Run against a disposable data source. Do not point PROBE_DATA_SOURCE_ID at a
# production data source.

source "$(dirname "$0")/lib.sh"

require_command aws
require_command jq
load_state

: "${KB_ID:?Missing KB_ID in state}"
: "${PROBE_DATA_SOURCE_ID:?Set PROBE_DATA_SOURCE_ID to a disposable data source}"

burst_count="${BURST_COUNT:-3}"
report="${TEST_DIR}/assumption-verification.json"
probe_document_id="assumption-probe-a2"
probe_marker="ASSUMPTIONPROBEMARKER${RUN_ID}"

printf 'Verifying A1: StartIngestionJob rate limit (%s calls)\n' "${burst_count}"

a1_results="[]"
for attempt in $(seq 1 "${burst_count}"); do
  call_start="$(date -u +%s.%N)"
  if response="$(aws bedrock-agent start-ingestion-job \
    --knowledge-base-id "${KB_ID}" \
    --data-source-id "${PROBE_DATA_SOURCE_ID}" \
    --description "A1 burst ${attempt}" \
    --region "${AWS_REGION}" \
    --output json 2>&1)"; then
    outcome="ACCEPTED"
    detail="$(jq -r '.ingestionJob.ingestionJobId' <<< "${response}")"
  else
    outcome="REJECTED"
    detail="$(head -c 400 <<< "${response}" | tr '\n' ' ')"
  fi
  call_end="$(date -u +%s.%N)"
  printf '  call %s: %s\n' "${attempt}" "${outcome}"
  a1_results="$(jq \
    --argjson attempt "${attempt}" \
    --arg outcome "${outcome}" \
    --arg detail "${detail}" \
    --argjson elapsed "$(echo "${call_end} - ${call_start}" | bc)" \
    '. + [{attempt: $attempt, outcome: $outcome, detail: $detail, elapsedSeconds: $elapsed}]' \
    <<< "${a1_results}")"
done

a1_accepted="$(jq '[.[] | select(.outcome == "ACCEPTED")] | length' <<< "${a1_results}")"
a1_throttled="$(jq '[.[] | select(.detail | test("Throttling|TooManyRequests"))] | length' \
  <<< "${a1_results}")"
if [[ "${a1_accepted}" -eq "${burst_count}" ]]; then
  a1_verdict="REJECTED_no_0.1_rps_limit_observed"
elif [[ "${a1_throttled}" -gt 0 ]]; then
  a1_verdict="CONFIRMED_throttled"
else
  a1_verdict="INCONCLUSIVE_see_details"
fi
printf 'A1 verdict: %s\n' "${a1_verdict}"

printf 'Verifying A2: does a connector sync remove directly ingested documents\n'

probe_payload="$(jq -n \
  --arg id "${probe_document_id}" \
  --arg data "# Assumption probe

This document exists only in the index. Marker ${probe_marker}." \
  '{documents: [{content: {dataSourceType: "CUSTOM", custom: {customDocumentIdentifier: {id: $id}, inlineContent: {textContent: {data: $data}, type: "TEXT"}, sourceType: "IN_LINE"}}}]}')"

a2_ingest_outcome="SKIPPED"
if ingest_response="$(aws bedrock-agent ingest-knowledge-base-documents \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${PROBE_DATA_SOURCE_ID}" \
  --cli-input-json "${probe_payload}" \
  --region "${AWS_REGION}" \
  --output json 2>&1)"; then
  a2_ingest_outcome="ACCEPTED"
  printf '%s\n' "${ingest_response}" > "${AWS_DIR}/a2-direct-ingest.json"
else
  a2_ingest_outcome="REJECTED: $(head -c 300 <<< "${ingest_response}" | tr '\n' ' ')"
fi
printf '  direct ingestion: %s\n' "${a2_ingest_outcome}"

retrieve_probe() {
  local label="$1"
  local hits
  aws bedrock-agent-runtime retrieve \
    --knowledge-base-id "${KB_ID}" \
    --retrieval-query "{\"text\": \"${probe_marker}\"}" \
    --retrieval-configuration '{"vectorSearchConfiguration": {"numberOfResults": 10}}' \
    --region "${AWS_REGION}" \
    --output json > "${TEST_DIR}/a2-retrieve-${label}.json" 2>/dev/null || {
    printf '0'
    return 0
  }
  hits="$(jq --arg marker "${probe_marker}" \
    '[.retrievalResults[]? | select(.content.text | contains($marker))] | length' \
    "${TEST_DIR}/a2-retrieve-${label}.json")"
  printf '%s' "${hits}"
}

sleep 30
a2_hits_before="$(retrieve_probe before-sync)"
printf '  probe retrievable before sync: %s\n' "${a2_hits_before}"

sync_response="$(aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${PROBE_DATA_SOURCE_ID}" \
  --description "A2 reconciliation sync" \
  --region "${AWS_REGION}" \
  --output json)"
sync_job_id="$(jq -r '.ingestionJob.ingestionJobId' <<< "${sync_response}")"
wait_for_ingestion "${KB_ID}" "${PROBE_DATA_SOURCE_ID}" "${sync_job_id}"

sleep 30
a2_hits_after="$(retrieve_probe after-sync)"
printf '  probe retrievable after sync: %s\n' "${a2_hits_after}"

if [[ "${a2_hits_before}" -eq 0 ]]; then
  a2_verdict="INCONCLUSIVE_probe_never_retrievable"
elif [[ "${a2_hits_after}" -eq 0 ]]; then
  a2_verdict="CONFIRMED_sync_removed_direct_document"
else
  a2_verdict="REJECTED_direct_document_survived_sync"
fi
printf 'A2 verdict: %s\n' "${a2_verdict}"

jq -n \
  --arg runId "${RUN_ID}" \
  --arg region "${AWS_REGION}" \
  --arg kbId "${KB_ID}" \
  --arg a1Verdict "${a1_verdict}" \
  --argjson a1Calls "${a1_results}" \
  --argjson a1Accepted "${a1_accepted}" \
  --argjson a1Throttled "${a1_throttled}" \
  --arg a2Verdict "${a2_verdict}" \
  --arg a2IngestOutcome "${a2_ingest_outcome}" \
  --argjson a2HitsBefore "${a2_hits_before}" \
  --argjson a2HitsAfter "${a2_hits_after}" \
  --arg a2SyncJobId "${sync_job_id}" \
  '{
    runId: $runId,
    region: $region,
    knowledgeBaseId: $kbId,
    assumptionA1: {
      question: "Does StartIngestionJob enforce 0.1 rps on managed knowledge bases?",
      verdict: $a1Verdict,
      acceptedCalls: $a1Accepted,
      throttledCalls: $a1Throttled,
      calls: $a1Calls
    },
    assumptionA2: {
      question: "Does a connector sync remove directly ingested documents absent from S3?",
      verdict: $a2Verdict,
      directIngestOutcome: $a2IngestOutcome,
      probeHitsBeforeSync: $a2HitsBefore,
      probeHitsAfterSync: $a2HitsAfter,
      reconciliationJobId: $a2SyncJobId
    }
  }' > "${report}"

printf 'Wrote %s\n' "${report}"
jq '{assumptionA1: .assumptionA1.verdict, assumptionA2: .assumptionA2.verdict}' "${report}"
