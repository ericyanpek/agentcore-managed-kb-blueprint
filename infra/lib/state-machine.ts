/**
 * Release state machine — fail-closed by graph topology.
 *
 * The key invariant: a failed release leaves the previously active version
 * untouched. This holds because of the graph shape, not because code
 * remembers to check a return value:
 *
 *   - Every gate is a Choice whose non-passing branch leads to FailRelease.
 *   - No gate Choice has an edge that can reach PromoteRelease while skipping
 *     a later gate.
 *   - FailRelease writes FAILED status and never touches the active pointer.
 *
 * Nine steps
 * ----------
 * [1] ReadPointer           read active releaseId; empty change set exits early
 * [2] CreateReleaseRecord   write PREPARING, conditional on id not existing
 * [3] VerifyS3Consistency   Gate A — objects and sidecars match the manifest
 * [4] CheckDeletionRatio    Gate B — deletion fraction within threshold
 * [5] IngestBatches         Map, concurrency 1, ingest each batch
 * [6] DeleteBatches         Map, concurrency 1, delete each batch
 * [7] PollDocumentStatus    Gate C — poll until settled, bounded by maxPollAttempts
 * [8] SmokeRetrieve         Gate D — change is retrievable (or absent for delete-only)
 * [9] PromoteRelease        conditional write of pointer; old release → SUPERSEDED
 * [F] FailRelease           write FAILED and terminate; never touches pointer
 */

import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { Construct } from 'constructs';

// ─── props ───────────────────────────────────────────────────────────────────

export interface BuildReleaseStateMachineProps {
  /** Gate A: S3 object and SHA verification. */
  readonly verifyS3Function: lambda.IFunction;
  /** Gates B / C / D: pure data transformation. */
  readonly checkGatesFunction: lambda.IFunction;
  /** Registry operations: read pointer, create, advance, promote, fail. */
  readonly registryFunction: lambda.IFunction;
  /** CloudWatch log group for ALL-level execution history. */
  readonly logGroup: logs.ILogGroup;
  /** ARN of the managed knowledge base (used as IAM resource for Bedrock SDK tasks). */
  readonly knowledgeBaseArn: string;
  /** Fraction of deletions above which the release is blocked (0–1). */
  readonly deletionRatioThreshold: number;
  /** Maximum number of poll attempts before the release is failed. */
  readonly maxPollAttempts: number;
}

// ─── retry configuration shared between the two Bedrock SDK Map tasks ───────

const THROTTLE_RETRY: sfn.RetryProps = {
  errors: ['Bedrock.ThrottlingException', 'States.TaskFailed'],
  interval: cdk.Duration.seconds(2),
  backoffRate: 2,
  maxAttempts: 6,
};

/**
 * Fields every Pass state must carry forward.
 *
 * A Pass replaces the state entirely, so anything omitted here disappears for the
 * rest of the execution. Two states rebuild the state independently, and keeping
 * two hand-written lists in step failed: allowBulkDeletion was added to one and
 * not the other, and gate B died on a missing JSONPath mid-release.
 */
const FORWARDED_FIELDS = [
  'corpusId',
  'releaseId',
  'manifestS3Uri',
  'manifestS3VersionId',
  'canonicalPrefix',
  'knowledgeBaseId',
  'dataSourceId',
  'changeSet',
  'activeReleaseId',
  'ingestBatches',
  'deleteBatches',
  'deletedCount',
  'previousDocumentCount',
  'smokeQuery',
  'smokeExpectation',
  'smokeTarget',
  'ingestDocumentIds',
  'deleteDocumentIds',
  'allowBulkDeletion',
] as const;

function forwardedState(): Record<string, string> {
  return Object.fromEntries(
    FORWARDED_FIELDS.map((field) => [`${field}.$`, `$.${field}`]),
  );
}

// ─── factory ─────────────────────────────────────────────────────────────────

/**
 * Build and return the release state machine.
 *
 * The graph is assembled *backwards* from the terminal states so that every
 * `.next()` target already exists as a variable when it is referenced.
 */
export function buildReleaseStateMachine(
  scope: Construct,
  id: string,
  props: BuildReleaseStateMachineProps,
): sfn.StateMachine {

  // ── Terminal states ────────────────────────────────────────────────────────

  const releaseFailed = new sfn.Fail(scope, 'ReleaseFailed', {
    comment: 'Release has failed; the active pointer is unchanged.',
  });

  const releaseSucceeded = new sfn.Succeed(scope, 'ReleaseSucceeded', {
    comment: 'Release is now active.',
  });

  const noChanges = new sfn.Succeed(scope, 'NoChanges', {
    comment: 'Change set is empty — nothing to release.',
  });

  // ── [F] FailRelease ────────────────────────────────────────────────────────
  // Write FAILED status. Never touches the active pointer.

  // The whole execution state is forwarded rather than a formatted string.
  // `$.error` exists only when a task threw; a gate that merely returned
  // passed=false leaves it absent, and referencing it unconditionally made the
  // failure path itself fail — the one path that must never break.
  const failReleaseTask = new tasks.LambdaInvoke(scope, 'FailRelease', {
    lambdaFunction: props.registryFunction,
    payload: sfn.TaskInput.fromObject({
      action: 'fail',
      corpusId: sfn.JsonPath.stringAt('$.corpusId'),
      releaseId: sfn.JsonPath.stringAt('$.releaseId'),
      failureContext: sfn.TaskInput.fromJsonPathAt('$').value,
    }),
    resultPath: '$.failResult',
    retryOnServiceExceptions: false,
  });
  failReleaseTask.next(releaseFailed);

  // Helper: adds a catch-to-FailRelease on a TaskStateBase.
  function withCatch<T extends sfn.TaskStateBase>(task: T): T {
    task.addCatch(failReleaseTask, {
      errors: ['States.ALL'],
      resultPath: '$.error',
    });
    return task;
  }

  // Helper: adds a catch-to-FailRelease on a Map state.
  function withMapCatch(mapState: sfn.Map): sfn.Map {
    mapState.addCatch(failReleaseTask, {
      errors: ['States.ALL'],
      resultPath: '$.error',
    });
    return mapState;
  }

  // ── [9] PromoteRelease ─────────────────────────────────────────────────────

  const promoteRelease = withCatch(
    new tasks.LambdaInvoke(scope, 'PromoteRelease', {
      lambdaFunction: props.registryFunction,
      payload: sfn.TaskInput.fromObject({
        action: 'promote',
        corpusId: sfn.JsonPath.stringAt('$.corpusId'),
        releaseId: sfn.JsonPath.stringAt('$.releaseId'),
        expectedPreviousReleaseId: sfn.JsonPath.stringAt('$.activeReleaseId'),
      }),
      resultPath: '$.promoteResult',
      retryOnServiceExceptions: false,
    }),
  );
  promoteRelease.next(releaseSucceeded);

  // ── Gate D: SmokeRetrieve & EvaluateSmoke ─────────────────────────────────
  // [8a] Retrieve one document from the managed KB using the runtime API.
  // Uses ManagedSearchConfiguration — VectorSearchConfiguration silently
  // returns zero hits against a managed knowledge base.

  const smokeRetrieve = withCatch(
    new tasks.CallAwsService(scope, 'SmokeRetrieve', {
      service: 'bedrockagentruntime',
      action: 'retrieve',
      parameters: {
        KnowledgeBaseId: sfn.JsonPath.stringAt('$.knowledgeBaseId'),
        RetrievalQuery: {
          Text: sfn.JsonPath.stringAt('$.smokeQuery'),
        },
        RetrievalConfiguration: {
          ManagedSearchConfiguration: {},
        },
      },
      iamResources: [props.knowledgeBaseArn],
      iamAction: 'bedrock:Retrieve',
      resultSelector: {
        // A managed knowledge base reports Location.Type CUSTOM even for documents
        // that came from S3, so the identifier lives under CustomDocumentLocation.Id
        // rather than S3Location.Uri. Reading the S3 shape yielded an empty list and
        // the smoke gate could never pass.
        'retrievedDocumentIds.$':
          '$.RetrievalResults[*].Location.CustomDocumentLocation.Id',
      },
      resultPath: '$.smokeResult',
    }),
  );

  // [8b] Evaluate the retrieval result (pure gate function).
  const evaluateSmoke = withCatch(
    new tasks.LambdaInvoke(scope, 'EvaluateSmoke', {
      lambdaFunction: props.checkGatesFunction,
      payload: sfn.TaskInput.fromObject({
        gate: 'smokeRetrieval',
        expectation: sfn.JsonPath.stringAt('$.smokeExpectation'),
        retrievedDocumentIds: sfn.JsonPath.listAt('$.smokeResult.retrievedDocumentIds'),
        target: sfn.JsonPath.stringAt('$.smokeTarget'),
      }),
      resultPath: '$.smokeGate',
      retryOnServiceExceptions: false,
    }),
  );

  // [8c] Gate D choice: passed → PromoteRelease, else → FailRelease.
  const gateDChoice = new sfn.Choice(scope, 'GateDChoice', {
    comment: 'Gate D — smoke retrieval passed?',
  });
  gateDChoice
    .when(
      sfn.Condition.booleanEquals('$.smokeGate.Payload.passed', true),
      promoteRelease,
    )
    .otherwise(failReleaseTask);

  smokeRetrieve.next(evaluateSmoke);
  evaluateSmoke.next(gateDChoice);

  // ── MarkTesting status ─────────────────────────────────────────────────────

  const markTesting = withCatch(
    new tasks.LambdaInvoke(scope, 'MarkTesting', {
      lambdaFunction: props.registryFunction,
      payload: sfn.TaskInput.fromObject({
        action: 'advanceStatus',
        corpusId: sfn.JsonPath.stringAt('$.corpusId'),
        releaseId: sfn.JsonPath.stringAt('$.releaseId'),
        status: 'TESTING',
      }),
      resultPath: '$.markTestingResult',
      retryOnServiceExceptions: false,
    }),
  );
  markTesting.next(smokeRetrieve);

  // ── Gate C: PollDocumentStatus loop ───────────────────────────────────────
  // Polls until all documents are settled, bounded by maxPollAttempts.

  // [7a] Increment poll attempt counter (Pass state using States.MathAdd).
  const incrementPollAttempt = new sfn.Pass(scope, 'IncrementPollAttempt', {
    parameters: {
      ...forwardedState(),
      // No '.$' suffix here: mathAdd already returns a JSONPath expression and
      // CDK appends the suffix itself. Writing it produced 'pollAttempt.$.$',
      // which never increments, so the loop ran to the execution timeout instead
      // of failing at the attempt limit.
      pollAttempt: sfn.JsonPath.mathAdd(
        sfn.JsonPath.numberAt('$.pollAttempt'),
        1,
      ),
    },
  });

  // [7b] Wait before polling.
  const waitForSettlement = new sfn.Wait(scope, 'WaitForSettlement', {
    time: sfn.WaitTime.duration(cdk.Duration.seconds(15)),
  });

  // [7c] Call Bedrock to get document statuses for upserted documents.
  const getDocumentStatuses = withCatch(
    new tasks.CallAwsService(scope, 'GetDocumentStatuses', {
      service: 'bedrockagent',
      action: 'getKnowledgeBaseDocuments',
      parameters: {
        KnowledgeBaseId: sfn.JsonPath.stringAt('$.knowledgeBaseId'),
        DataSourceId: sfn.JsonPath.stringAt('$.dataSourceId'),
        DocumentIdentifiers: sfn.JsonPath.listAt('$.ingestDocumentIds'),
      },
      iamResources: [props.knowledgeBaseArn],
      iamAction: 'bedrock:GetKnowledgeBaseDocuments',
      resultSelector: {
        'documentDetails.$': '$.DocumentDetails',
      },
      resultPath: '$.statusResult',
    }),
  );

  // [7d] Evaluate the document statuses (pure gate function).
  const evaluateIngestStatus = withCatch(
    new tasks.LambdaInvoke(scope, 'EvaluateIngestStatus', {
      lambdaFunction: props.checkGatesFunction,
      payload: sfn.TaskInput.fromObject({
        gate: 'ingestStatus',
        documentDetails: sfn.JsonPath.listAt('$.statusResult.documentDetails'),
        // States.ArrayLength, not '.length': the latter is not JSONPath and fails
        // at runtime with "JSONPath could not be found in the input".
        expectedCount: sfn.JsonPath.arrayLength(
          sfn.JsonPath.listAt('$.ingestDocumentIds'),
        ),
      }),
      resultPath: '$.ingestStatusGate',
      retryOnServiceExceptions: false,
    }),
  );

  // [7e] Gate C choice:
  //   - settled && passed → MarkTesting
  //   - settled && !passed → FailRelease
  //   - !settled && pollAttempt < maxPollAttempts → IncrementPollAttempt
  //   - !settled && pollAttempt >= maxPollAttempts → FailRelease (bounded)
  const gateCChoice = new sfn.Choice(scope, 'GateCChoice', {
    comment: 'Gate C — ingest statuses settled and passed?',
  });
  gateCChoice
    .when(
      sfn.Condition.and(
        sfn.Condition.booleanEquals('$.ingestStatusGate.Payload.settled', true),
        sfn.Condition.booleanEquals('$.ingestStatusGate.Payload.passed', true),
      ),
      markTesting,
    )
    .when(
      sfn.Condition.booleanEquals('$.ingestStatusGate.Payload.settled', true),
      // settled but not passed → fail
      failReleaseTask,
    )
    .when(
      // not settled — check the attempt bound
      sfn.Condition.numberLessThan('$.pollAttempt', props.maxPollAttempts),
      incrementPollAttempt,
    )
    .otherwise(failReleaseTask); // exceeded max attempts

  waitForSettlement.next(getDocumentStatuses);
  getDocumentStatuses.next(evaluateIngestStatus);
  evaluateIngestStatus.next(gateCChoice);
  incrementPollAttempt.next(waitForSettlement);

  // ── [6] DeleteBatches Map ─────────────────────────────────────────────────

  // ── Fail states for Map item processors ───────────────────────────────────
  // Tasks inside a Map's itemProcessor must catch to states within that same
  // processor graph. We use dedicated Fail states here (not the outer
  // FailRelease task) to avoid CDK graph traversal crossing the Map boundary.
  // The Map state itself has addCatch(failReleaseTask) in the outer graph, so
  // any iteration failure propagates to FailRelease correctly.

  const deleteItemFail = new sfn.Fail(scope, 'DeleteItemFailed', {
    comment: 'A single document deletion failed; the Map state will catch this.',
  });

  const deleteOneDocument = new tasks.CallAwsService(
    scope,
    'DeleteOneDocument',
    {
      service: 'bedrockagent',
      action: 'deleteKnowledgeBaseDocuments',
      // Inline item processors receive the item as `$`; see IngestOneBatch below.
      parameters: {
        KnowledgeBaseId: sfn.JsonPath.stringAt('$.knowledgeBaseId'),
        DataSourceId: sfn.JsonPath.stringAt('$.dataSourceId'),
        ClientToken: sfn.JsonPath.stringAt('$.clientToken'),
        DocumentIdentifiers: sfn.JsonPath.listAt('$.documentIdentifiers'),
      },
      iamResources: [props.knowledgeBaseArn],
      iamAction: 'bedrock:DeleteKnowledgeBaseDocuments',
      resultPath: '$.deleteResult',
    },
  );
  deleteOneDocument.addRetry(THROTTLE_RETRY);
  deleteOneDocument.addCatch(deleteItemFail, {
    errors: ['States.ALL'],
    resultPath: '$.error',
  });

  const deleteBatches = withMapCatch(
    new sfn.Map(scope, 'DeleteBatches', {
      maxConcurrency: 1,
      itemsPath: '$.deleteBatches',
      resultPath: '$.deleteBatchResults',
    }),
  );
  deleteBatches.itemProcessor(deleteOneDocument);
  deleteBatches.next(waitForSettlement);

  // ── [5] IngestBatches Map ─────────────────────────────────────────────────

  const ingestItemFail = new sfn.Fail(scope, 'IngestItemFailed', {
    comment: 'A single document ingestion failed; the Map state will catch this.',
  });

  const ingestOneBatch = new tasks.CallAwsService(scope, 'IngestOneBatch', {
    service: 'bedrockagent',
    action: 'ingestKnowledgeBaseDocuments',
    // An inline item processor receives the item itself as `$`, so the fields are
    // read directly. `$$.Map.Item.Value` resolves against the execution context
    // instead and fails at runtime with "JSONPath could not be found in the input".
    parameters: {
      KnowledgeBaseId: sfn.JsonPath.stringAt('$.knowledgeBaseId'),
      DataSourceId: sfn.JsonPath.stringAt('$.dataSourceId'),
      ClientToken: sfn.JsonPath.stringAt('$.clientToken'),
      Documents: sfn.JsonPath.listAt('$.documents'),
    },
    iamResources: [props.knowledgeBaseArn],
    // The service authorizes this call as bedrock:StartIngestionJob, not under an
    // action matching the API name. A 403 naming StartIngestionJob is the only
    // way this surfaces; nothing at synth time knows the mapping.
    iamAction: 'bedrock:StartIngestionJob',
    resultPath: '$.ingestResult',
  });
  ingestOneBatch.addRetry(THROTTLE_RETRY);
  ingestOneBatch.addCatch(ingestItemFail, {
    errors: ['States.ALL'],
    resultPath: '$.error',
  });

  const ingestBatches = withMapCatch(
    new sfn.Map(scope, 'IngestBatches', {
      maxConcurrency: 1,
      itemsPath: '$.ingestBatches',
      resultPath: '$.ingestBatchResults',
    }),
  );
  ingestBatches.itemProcessor(ingestOneBatch);
  ingestBatches.next(deleteBatches);

  // ── MarkIngesting status ───────────────────────────────────────────────────

  const markIngesting = withCatch(
    new tasks.LambdaInvoke(scope, 'MarkIngesting', {
      lambdaFunction: props.registryFunction,
      payload: sfn.TaskInput.fromObject({
        action: 'advanceStatus',
        corpusId: sfn.JsonPath.stringAt('$.corpusId'),
        releaseId: sfn.JsonPath.stringAt('$.releaseId'),
        status: 'INGESTING',
      }),
      resultPath: '$.markIngestingResult',
      retryOnServiceExceptions: false,
    }),
  );
  markIngesting.next(ingestBatches);

  // ── Gate B: CheckDeletionRatio ────────────────────────────────────────────
  // [4a] Invoke the gate function.
  const checkDeletionRatio = withCatch(
    new tasks.LambdaInvoke(scope, 'CheckDeletionRatio', {
      lambdaFunction: props.checkGatesFunction,
      payload: sfn.TaskInput.fromObject({
        gate: 'deletionRatio',
        deletedCount: sfn.JsonPath.numberAt('$.deletedCount'),
        previousDocumentCount: sfn.JsonPath.numberAt('$.previousDocumentCount'),
        threshold: props.deletionRatioThreshold,
        // Read from the execution input. Hardcoding false meant an operator who
        // passed --allow-bulk-deletion got past the client-side guard, which then
        // deleted the objects, only for this gate to refuse the release anyway.
        allowBulkDeletion: sfn.JsonPath.stringAt('$.allowBulkDeletion'),
      }),
      resultPath: '$.deletionRatioGate',
      retryOnServiceExceptions: false,
    }),
  );

  // [4b] Gate B choice.
  const gateBChoice = new sfn.Choice(scope, 'GateBChoice', {
    comment: 'Gate B — deletion ratio within threshold?',
  });
  gateBChoice
    .when(
      sfn.Condition.booleanEquals('$.deletionRatioGate.Payload.passed', true),
      markIngesting,
    )
    .otherwise(failReleaseTask);

  checkDeletionRatio.next(gateBChoice);

  // ── Gate A: VerifyS3Consistency ───────────────────────────────────────────
  // [3a] Invoke the S3 verification function.
  const verifyS3Consistency = withCatch(
    new tasks.LambdaInvoke(scope, 'VerifyS3Consistency', {
      lambdaFunction: props.verifyS3Function,
      payload: sfn.TaskInput.fromObject({
        manifestS3Uri: sfn.JsonPath.stringAt('$.manifestS3Uri'),
        manifestS3VersionId: sfn.JsonPath.stringAt('$.manifestS3VersionId'),
        canonicalPrefix: sfn.JsonPath.stringAt('$.canonicalPrefix'),
        // The upsert set comes from the manifest, but deleted files are by
        // definition absent from it, so they travel in the payload. The list
        // carries only file names, so it stays small.
        deletions: sfn.JsonPath.listAt('$.changeSet.deleted'),
      }),
      resultPath: '$.s3Gate',
      retryOnServiceExceptions: false,
    }),
  );

  // [3b] Gate A choice.
  const gateAChoice = new sfn.Choice(scope, 'GateAChoice', {
    comment: 'Gate A — S3 objects consistent with manifest?',
  });
  gateAChoice
    .when(
      sfn.Condition.booleanEquals('$.s3Gate.Payload.passed', true),
      checkDeletionRatio,
    )
    .otherwise(failReleaseTask);

  verifyS3Consistency.next(gateAChoice);

  // ── [2] CreateReleaseRecord ───────────────────────────────────────────────

  const createReleaseRecord = withCatch(
    new tasks.LambdaInvoke(scope, 'CreateReleaseRecord', {
      lambdaFunction: props.registryFunction,
      payload: sfn.TaskInput.fromObject({
        action: 'createRelease',
        corpusId: sfn.JsonPath.stringAt('$.corpusId'),
        releaseId: sfn.JsonPath.stringAt('$.releaseId'),
        manifestS3Uri: sfn.JsonPath.stringAt('$.manifestS3Uri'),
        manifestS3VersionId: sfn.JsonPath.stringAt('$.manifestS3VersionId'),
        parentReleaseId: sfn.JsonPath.stringAt('$.activeReleaseId'),
        executionArn: sfn.JsonPath.executionId,
      }),
      resultPath: '$.createResult',
      retryOnServiceExceptions: false,
    }),
  );
  createReleaseRecord.next(verifyS3Consistency);

  // ── [1] ReadPointer + IsChangeSetEmpty ───────────────────────────────────

  const isChangeSetEmpty = new sfn.Choice(scope, 'IsChangeSetEmpty', {
    comment: 'Is the change set empty? If so, exit immediately.',
  });
  isChangeSetEmpty
    .when(
      sfn.Condition.and(
        sfn.Condition.isPresent('$.changeSet'),
        sfn.Condition.or(
          sfn.Condition.isPresent('$.changeSet.added'),
          sfn.Condition.isPresent('$.changeSet.modified'),
          sfn.Condition.isPresent('$.changeSet.deleted'),
        ),
      ),
      createReleaseRecord,
    )
    .otherwise(noChanges);

  const readPointer = withCatch(
    new tasks.LambdaInvoke(scope, 'ReadPointer', {
      lambdaFunction: props.registryFunction,
      payload: sfn.TaskInput.fromObject({
        action: 'readPointer',
        corpusId: sfn.JsonPath.stringAt('$.corpusId'),
      }),
      resultSelector: {
        'activeReleaseId.$': '$.Payload.activeReleaseId',
      },
      resultPath: '$.pointerResult',
      retryOnServiceExceptions: false,
    }),
  );

  // Merge pointer result into the execution state. activeReleaseId comes from the
  // ReadPointer result rather than being forwarded, so it overrides the shared set.
  const mergePointer = new sfn.Pass(scope, 'MergePointer', {
    parameters: {
      ...forwardedState(),
      'activeReleaseId.$': '$.pointerResult.activeReleaseId',
      'pollAttempt': 0,
    },
  });

  readPointer.next(mergePointer);
  mergePointer.next(isChangeSetEmpty);

  // ── State machine ─────────────────────────────────────────────────────────

  return new sfn.StateMachine(scope, id, {
    definitionBody: sfn.DefinitionBody.fromChainable(readPointer),
    stateMachineType: sfn.StateMachineType.STANDARD,
    timeout: cdk.Duration.hours(2),
    logs: {
      destination: props.logGroup,
      level: sfn.LogLevel.ALL,
      includeExecutionData: true,
    },
    tracingEnabled: true,
    comment: 'Fail-closed release state machine for managed knowledge base',
  });
}

/**
 * Grant the state machine role permissions needed for the SDK integrations.
 * Called by ReleaseStack after the machine is created and roles are resolved.
 */
export function grantStateMachineBedrockPermissions(
  stateMachine: sfn.StateMachine,
  knowledgeBaseArn: string,
): void {
  // Direct ingestion needs bedrock:StartIngestionJob even though no ingestion job
  // is started: a live execution returned "not authorized to perform:
  // bedrock:StartIngestionJob" for IngestKnowledgeBaseDocuments. The five document
  // actions below are exactly the set the "Prerequisites for direct ingestion"
  // guide lists, plus Retrieve for the smoke gate.
  stateMachine.addToRolePolicy(
    new iam.PolicyStatement({
      actions: [
        'bedrock:StartIngestionJob',
        'bedrock:IngestKnowledgeBaseDocuments',
        'bedrock:GetKnowledgeBaseDocuments',
        'bedrock:ListKnowledgeBaseDocuments',
        'bedrock:DeleteKnowledgeBaseDocuments',
        'bedrock:Retrieve',
      ],
      resources: [knowledgeBaseArn],
    }),
  );
  // CloudWatch Logs permissions needed for ALL-level logging.
  stateMachine.addToRolePolicy(
    new iam.PolicyStatement({
      actions: [
        'logs:CreateLogDelivery',
        'logs:CreateLogGroup',
        'logs:CreateLogStream',
        'logs:DescribeLogGroups',
        'logs:DescribeLogStreams',
        'logs:DescribeResourcePolicies',
        'logs:GetLogDelivery',
        'logs:ListLogDeliveries',
        'logs:PutLogEvents',
        'logs:PutResourcePolicy',
        'logs:UpdateLogDelivery',
      ],
      resources: ['*'],
    }),
  );
}
