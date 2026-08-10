# Observability Evidence

> Copy this template into the ignored runtime evidence directory. Do not commit
> real account IDs, ARNs, resource IDs, request payloads, or customer data.

## Experiment

| Field | Value |
| --- | --- |
| Experiment ID | `<E00-E07>` |
| Test case | `<success-or-controlled-failure>` |
| UTC time range | `<start/end>` |
| Account reference | `<redacted-alias>` |
| Region | `<region>` |
| Environment | `<lab/staging/production>` |
| Operator role reference | `<redacted-role>` |
| Resource type | `<kb/runtime/memory/gateway/tool/application>` |
| Resource logical ID | `<logical-id>` |
| Request ID | `<request-id-or-not-available>` |
| Runtime session ID | `<session-id-or-not-available>` |
| Trace ID | `<trace-id-or-not-available>` |
| Corpus/release version | `<version-or-not-applicable>` |

## Expected Behavior

- Functional result:
- Expected telemetry:
- Expected controlled failure:
- Prohibited sensitive fields:

## Functional Result

- Status: `PASS | FAIL | GAP | N/A`
- API or workflow:
- Observed result:
- Error category:
- Evidence location:

## Metrics

- Status: `PASS | FAIL | GAP | N/A`
- Namespace:
- Metric names:
- Dimensions:
- Period/statistic:
- Observed value:
- Expected arrival delay:
- Dashboard/alarm:
- Evidence location:

## Logs

- Status: `PASS | FAIL | GAP | N/A`
- Log type: `service | application | ingestion | audit`
- Destination/log group or controlled prefix:
- Delivery configuration evidence:
- Retention:
- KMS:
- Successful event reference:
- Failed event reference:
- Delivery lag/error:
- Evidence location:

## Traces

- Status: `PASS | FAIL | GAP | N/A`
- Transaction Search enabled:
- Root span:
- Expected child spans:
- Missing links:
- Propagated correlation headers/IDs:
- Evidence location:

## Correlation

- Can the same request be found across applicable signals: `YES | NO | PARTIAL`
- Join keys:
- Explicit ID mappings:
- Unexplained orphan events:

## Data Governance

- Data classification:
- `payload_redacted`: `true | false`
- Secret/token scan:
- Prompt/tool/raw chunk treatment:
- IAM and query roles reviewed:
- Online retention:
- Long-term retention:
- Deletion/Legal Hold:

## Pipeline Health

- Delivery failure alarm:
- Throttling/retry evidence:
- Firehose backup/error records:
- Schema validation:
- Long-term table maintenance:

## Cost

- Model/token usage:
- CloudWatch ingest/query:
- Firehose delivery:
- S3 Tables storage/query:
- Estimated total:

## Conclusion

- Functional status:
- Observability readiness: `Metrics / Logs / Traces / ADOT`
- Control IDs:
- Gaps and risks:
- Owner:
- Next action and due date:
- Cleanup status:
