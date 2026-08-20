/**
 * Tests for the fail-closed release state machine.
 *
 * The core invariant: every gate Choice routes its non-passing branch to
 * FailRelease and no gate Choice can reach PromoteRelease while skipping a
 * later gate. This must hold by graph topology, not by runtime guard code.
 *
 * DefinitionString extraction:  CDK emits the ASL as Fn::Join of literals and
 * CloudFormation tokens (Fn::GetAtt, Ref, etc.). We concatenate the literal
 * parts and replace token objects with a placeholder string so the result
 * remains valid JSON for parsing.
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { KnowledgeBaseStack } from '../lib/knowledge-base-stack';
import { ReleaseStack } from '../lib/release-stack';

const env = { account: '123456789012', region: 'us-east-1' };

// ─── helpers ────────────────────────────────────────────────────────────────

function buildStacks(corpusId = 'demo') {
  const app = new cdk.App();
  const foundation = new FoundationStack(app, 'TestFoundation', { env, corpusId });
  const knowledgeBase = new KnowledgeBaseStack(app, 'TestKnowledgeBase', {
    env,
    corpusId,
    canonicalBucket: foundation.canonicalBucket,
    encryptionKey: foundation.encryptionKey,
    canonicalPrefix: `canonical/${corpusId}`,
  });
  const release = new ReleaseStack(app, 'TestRelease', {
    env,
    corpusId,
    canonicalBucket: foundation.canonicalBucket,
    registryBucket: foundation.registryBucket,
    encryptionKey: foundation.encryptionKey,
    stateMachineLogGroup: foundation.stateMachineLogGroup,
    knowledgeBaseId: knowledgeBase.knowledgeBaseId,
    dataSourceId: knowledgeBase.dataSourceId,
    knowledgeBaseArn: knowledgeBase.knowledgeBaseArn,
    canonicalPrefix: `canonical/${corpusId}`,
    deletionRatioThreshold: 0.5,
  });
  return { app, foundation, knowledgeBase, release };
}

function synthTemplate(corpusId = 'demo'): Template {
  const { release } = buildStacks(corpusId);
  return Template.fromStack(release);
}

/**
 * Extract and parse the ASL definition from the synthesised template.
 *
 * CDK serialises DefinitionString as {"Fn::Join": ["", [literal, token, …]]}.
 * We replace every non-string element (a CloudFormation token object) with the
 * placeholder string "PLACEHOLDER" so the joined result is valid JSON.
 */
function extractDefinition(template: Template): Record<string, unknown> {
  const machines = template.findResources('AWS::StepFunctions::StateMachine');
  expect(Object.keys(machines).length).toBe(1);
  const sm = Object.values(machines)[0];

  const joinExpr = sm.Properties.DefinitionString;
  // CDK uses {"Fn::Join": ["", [...]]}
  expect(joinExpr).toHaveProperty('Fn::Join');
  const parts: unknown[] = joinExpr['Fn::Join'][1];

  // Token objects (Fn::GetAtt, Ref, etc.) appear *inside* JSON string values in
  // the surrounding literal — e.g.  "arn:" + {token} + ":states:::...".
  // Replacing with a bare word (no JSON quotes) keeps the surrounding string
  // value syntactically valid.
  const resolved = parts
    .map((p) => (typeof p === 'string' ? p : 'PLACEHOLDER'))
    .join('');

  return JSON.parse(resolved) as Record<string, unknown>;
}

type AslState = Record<string, unknown>;
type AslStates = Record<string, AslState>;

function getStates(def: Record<string, unknown>): AslStates {
  return def['States'] as AslStates;
}

/**
 * Collect all state names reachable from a Choice state's branches
 * (Choices array Next fields + Default field).
 */
function choiceNextStates(state: AslState): string[] {
  const nexts: string[] = [];
  const choices = (state['Choices'] as Array<Record<string, unknown>>) ?? [];
  for (const branch of choices) {
    if (typeof branch['Next'] === 'string') nexts.push(branch['Next'] as string);
  }
  if (typeof state['Default'] === 'string') nexts.push(state['Default'] as string);
  return nexts;
}

/**
 * Collect all states directly reachable from a given state (one hop).
 * Covers: Next, Catch[].Next, Choices[].Next, Default, Branches[].StartAt, Iterator.StartAt.
 */
function directSuccessors(state: AslState): string[] {
  const nexts: string[] = [];
  if (typeof state['Next'] === 'string') nexts.push(state['Next'] as string);
  const catches = (state['Catch'] as Array<Record<string, unknown>>) ?? [];
  for (const c of catches) {
    if (typeof c['Next'] === 'string') nexts.push(c['Next'] as string);
  }
  const choices = (state['Choices'] as Array<Record<string, unknown>>) ?? [];
  for (const branch of choices) {
    if (typeof branch['Next'] === 'string') nexts.push(branch['Next'] as string);
  }
  if (typeof state['Default'] === 'string') nexts.push(state['Default'] as string);
  const branches = (state['Branches'] as Array<Record<string, unknown>>) ?? [];
  for (const branch of branches) {
    if (typeof branch['StartAt'] === 'string') nexts.push(branch['StartAt'] as string);
  }
  // Inline ItemProcessor
  const processor = state['ItemProcessor'] as Record<string, unknown> | undefined;
  if (processor && typeof processor['StartAt'] === 'string') nexts.push(processor['StartAt'] as string);
  return nexts;
}

/**
 * BFS from startState, returns the set of all reachable state names.
 */
function reachableFrom(states: AslStates, startState: string): Set<string> {
  const visited = new Set<string>();
  const queue = [startState];
  while (queue.length) {
    const curr = queue.shift()!;
    if (visited.has(curr)) continue;
    visited.add(curr);
    const state = states[curr];
    if (!state) continue;
    for (const next of directSuccessors(state)) {
      if (!visited.has(next)) queue.push(next);
    }
  }
  return visited;
}

// ─── tests ──────────────────────────────────────────────────────────────────

describe('ReleaseStateMachine – machine-level properties', () => {
  let template: Template;

  beforeAll(() => {
    template = synthTemplate();
  });

  test('exactly one state machine is created', () => {
    template.resourceCountIs('AWS::StepFunctions::StateMachine', 1);
  });

  test('state machine type is STANDARD', () => {
    template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
      StateMachineType: 'STANDARD',
    });
  });

  test('logging is enabled', () => {
    const machines = template.findResources('AWS::StepFunctions::StateMachine');
    const sm = Object.values(machines)[0];
    const logging = sm.Properties.LoggingConfiguration;
    expect(logging).toBeDefined();
    expect(logging.Level).toBe('ALL');
    expect(logging.IncludeExecutionData).toBe(true);
    expect(logging.Destinations).toBeDefined();
    expect(logging.Destinations.length).toBeGreaterThan(0);
  });

  test('X-Ray tracing is enabled', () => {
    const machines = template.findResources('AWS::StepFunctions::StateMachine');
    const sm = Object.values(machines)[0];
    expect(sm.Properties.TracingConfiguration?.Enabled).toBe(true);
  });
});

describe('ReleaseStateMachine – graph topology (fail-closed invariants)', () => {
  let def: Record<string, unknown>;
  let states: AslStates;

  beforeAll(() => {
    def = extractDefinition(synthTemplate());
    states = getStates(def);
  });

  test('definition parses and contains expected states', () => {
    const required = [
      'ReadPointer', 'MergePointer', 'IsChangeSetEmpty', 'NoChanges', 'CreateReleaseRecord',
      'VerifyS3Consistency', 'GateAChoice', 'CheckDeletionRatio', 'GateBChoice',
      'MarkIngesting', 'IngestBatches', 'DeleteBatches', 'WaitForSettlement',
      'GetDocumentStatuses', 'EvaluateIngestStatus', 'GateCChoice',
      'IncrementPollAttempt', 'MarkTesting', 'SmokeRetrieve', 'EvaluateSmoke',
      'GateDChoice', 'PromoteRelease', 'ReleaseSucceeded', 'FailRelease', 'ReleaseFailed',
    ];
    for (const name of required) {
      expect(states[name]).toBeDefined();
    }
  });

  test('GateAChoice has a branch that goes to FailRelease', () => {
    const choices = choiceNextStates(states['GateAChoice']);
    expect(choices).toContain('FailRelease');
  });

  test('GateBChoice has a branch that goes to FailRelease', () => {
    const choices = choiceNextStates(states['GateBChoice']);
    expect(choices).toContain('FailRelease');
  });

  test('GateCChoice has a branch that goes to FailRelease', () => {
    const choices = choiceNextStates(states['GateCChoice']);
    expect(choices).toContain('FailRelease');
  });

  test('GateDChoice has a branch that goes to FailRelease', () => {
    const choices = choiceNextStates(states['GateDChoice']);
    expect(choices).toContain('FailRelease');
  });

  test('GateAChoice cannot reach PromoteRelease directly (must go through later gates)', () => {
    // GateAChoice should not have PromoteRelease as a direct branch target.
    // It may only proceed to CheckDeletionRatio (GateBChoice path) or FailRelease.
    const directBranches = choiceNextStates(states['GateAChoice']);
    expect(directBranches).not.toContain('PromoteRelease');
  });

  test('GateBChoice cannot reach PromoteRelease directly', () => {
    const directBranches = choiceNextStates(states['GateBChoice']);
    expect(directBranches).not.toContain('PromoteRelease');
  });

  test('GateCChoice cannot reach PromoteRelease directly', () => {
    const directBranches = choiceNextStates(states['GateCChoice']);
    expect(directBranches).not.toContain('PromoteRelease');
  });

  test('PromoteRelease is reachable only from GateDChoice', () => {
    // Every state except GateDChoice must NOT have PromoteRelease as a direct successor.
    for (const [name, state] of Object.entries(states)) {
      if (name === 'GateDChoice') continue;
      const succs = directSuccessors(state);
      expect(succs).not.toContain('PromoteRelease');
    }
  });

  test('an empty change set reaches a Succeed state without creating a release record', () => {
    // ReadPointer → MergePointer → IsChangeSetEmpty → NoChanges (Succeed)
    // MergePointer is a Pass state that flattens the pointer result into the execution state.
    const readPointerSuccessors = directSuccessors(states['ReadPointer']);
    expect(readPointerSuccessors).toContain('MergePointer');

    const mergePointerSuccessors = directSuccessors(states['MergePointer']);
    expect(mergePointerSuccessors).toContain('IsChangeSetEmpty');

    const isEmptySuccessors = choiceNextStates(states['IsChangeSetEmpty']);
    expect(isEmptySuccessors).toContain('NoChanges');

    // NoChanges must be a Succeed state
    expect(states['NoChanges']['Type']).toBe('Succeed');

    // NoChanges must not reach CreateReleaseRecord
    const reachableFromNoChanges = reachableFrom(states, 'NoChanges');
    expect(reachableFromNoChanges).not.toContain('CreateReleaseRecord');
  });

  test('FailRelease terminates in a Fail state', () => {
    // FailRelease itself is a task that calls the registry Lambda with action=fail,
    // and it chains to ReleaseFailed which is a Fail state.
    const reachable = reachableFrom(states, 'FailRelease');
    const failStates = Array.from(reachable).filter(
      (name) => states[name]?.['Type'] === 'Fail',
    );
    expect(failStates.length).toBeGreaterThan(0);
  });

  test('FailRelease does not reach PromoteRelease', () => {
    const reachable = reachableFrom(states, 'FailRelease');
    expect(reachable).not.toContain('PromoteRelease');
  });

  test('poll loop has a bounded attempt check that routes to FailRelease', () => {
    // GateCChoice must have a branch that leads to FailRelease when max poll attempts exceeded.
    // This is the bound: when pollAttempt >= maxPollAttempts, go to FailRelease.
    const reachableFromGateC = reachableFrom(states, 'GateCChoice');
    expect(reachableFromGateC).toContain('FailRelease');

    // IncrementPollAttempt must be reachable from the poll loop
    // and must lead back toward GetDocumentStatuses (the loop-back path).
    expect(states['IncrementPollAttempt']).toBeDefined();
    const incrementSuccessors = directSuccessors(states['IncrementPollAttempt']);
    expect(incrementSuccessors).toContain('WaitForSettlement');
  });
});

describe('ReleaseStateMachine – Map state concurrency', () => {
  let states: AslStates;

  beforeAll(() => {
    const def = extractDefinition(synthTemplate());
    states = getStates(def);
  });

  test('IngestBatches Map uses maxConcurrency 1', () => {
    expect(states['IngestBatches']['Type']).toBe('Map');
    expect(states['IngestBatches']['MaxConcurrency']).toBe(1);
  });

  test('DeleteBatches Map uses maxConcurrency 1', () => {
    expect(states['DeleteBatches']['Type']).toBe('Map');
    expect(states['DeleteBatches']['MaxConcurrency']).toBe(1);
  });
});

describe('ReleaseStateMachine – retry configuration', () => {
  let states: AslStates;

  beforeAll(() => {
    const def = extractDefinition(synthTemplate());
    states = getStates(def);
  });

  /**
   * Find retry entries that target throttling/task-failure errors with
   * at least 6 attempts and exponential backoff (backoffRate >= 2).
   */
  function hasThrottlingRetry(state: AslState): boolean {
    const retries = (state['Retry'] as Array<Record<string, unknown>>) ?? [];
    return retries.some((r) => {
      const errs = (r['ErrorEquals'] as string[]) ?? [];
      const hasThrottle =
        errs.includes('Bedrock.ThrottlingException') ||
        errs.includes('States.TaskFailed');
      const atLeastSix = ((r['MaxAttempts'] as number) ?? 0) >= 6;
      const exponential = ((r['BackoffRate'] as number) ?? 1) >= 2;
      return hasThrottle && atLeastSix && exponential;
    });
  }

  test('ingest task inside IngestBatches has throttling retry with >= 6 attempts', () => {
    // The retry is on the task inside the ItemProcessor, not on the Map state itself.
    // We retrieve the processor's StartAt and check the actual task.
    const ingestMap = states['IngestBatches'];
    const processor = ingestMap['ItemProcessor'] as Record<string, unknown>;
    const processorStates = processor['States'] as AslStates;
    const startAt = processor['StartAt'] as string;
    const ingestTask = processorStates[startAt];
    expect(hasThrottlingRetry(ingestTask)).toBe(true);
  });

  test('delete task inside DeleteBatches has throttling retry with >= 6 attempts', () => {
    const deleteMap = states['DeleteBatches'];
    const processor = deleteMap['ItemProcessor'] as Record<string, unknown>;
    const processorStates = processor['States'] as AslStates;
    const startAt = processor['StartAt'] as string;
    const deleteTask = processorStates[startAt];
    expect(hasThrottlingRetry(deleteTask)).toBe(true);
  });
});

describe('ReleaseStateMachine – smoke retrieval', () => {
  let states: AslStates;

  beforeAll(() => {
    const def = extractDefinition(synthTemplate());
    states = getStates(def);
  });

  test('SmokeRetrieve uses ManagedSearchConfiguration, not VectorSearchConfiguration', () => {
    const smokeState = states['SmokeRetrieve'];
    const stateJson = JSON.stringify(smokeState);
    expect(stateJson).toContain('ManagedSearchConfiguration');
    expect(stateJson).not.toContain('VectorSearchConfiguration');
  });
});

describe('ReleaseStateMachine – publisher role permissions', () => {
  let template: Template;

  beforeAll(() => {
    template = synthTemplate();
  });

  test('publisher role has states:StartExecution', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    const allStatements = Object.values(policies).flatMap(
      (p) => p.Properties.PolicyDocument.Statement,
    );
    const hasStartExecution = allStatements.some((stmt: { Action: unknown }) =>
      JSON.stringify(stmt.Action).includes('states:StartExecution'),
    );
    expect(hasStartExecution).toBe(true);
  });

  test('publisher role still has no bedrock: permission', () => {
    const roles = template.findResources('AWS::IAM::Role');
    const policies = template.findResources('AWS::IAM::Policy');

    const publisherRoleEntries = Object.entries(roles).filter(([, role]) => {
      const statements = role.Properties.AssumeRolePolicyDocument?.Statement ?? [];
      return statements.some((stmt: { Principal: unknown }) => {
        const principalStr = JSON.stringify(stmt.Principal);
        return principalStr.includes(':root') || principalStr.includes('iam::');
      });
    });
    expect(publisherRoleEntries.length).toBeGreaterThan(0);

    for (const [publisherRoleLogicalId] of publisherRoleEntries) {
      const attachedPolicies = Object.values(policies).filter((policy) => {
        const attachedRoles: unknown[] = policy.Properties.Roles ?? [];
        return attachedRoles.some((ref) =>
          JSON.stringify(ref).includes(publisherRoleLogicalId),
        );
      });

      for (const policy of attachedPolicies) {
        const policyJson = JSON.stringify(policy.Properties.PolicyDocument);
        expect(policyJson.toLowerCase()).not.toContain('bedrock:');
      }
    }
  });
});

describe('ReleaseStateMachine – multi-corpus synthesis', () => {
  test.each(['demo', 'prod', 'other-corpus'])(
    'synthesises cleanly for corpusId=%s',
    (corpusId) => {
      expect(() => synthTemplate(corpusId)).not.toThrow();
      const template = synthTemplate(corpusId);
      template.resourceCountIs('AWS::StepFunctions::StateMachine', 1);
    },
  );
});

describe('poll counter wiring', () => {
  test('the increment produces a single JSONPath suffix', () => {
    // mathAdd already yields a JSONPath expression and CDK appends '.$' itself.
    // Writing the suffix by hand produced 'pollAttempt.$.$', a key Step Functions
    // never assigns, so the counter stayed at zero and the loop ran until the
    // execution timeout instead of failing at the attempt limit.
    const states = getStates(extractDefinition(synthTemplate()));

    // toHaveProperty treats '.' as a path separator, so compare keys directly.
    const parameters = (states.IncrementPollAttempt as any).Parameters;

    expect(JSON.stringify(states)).not.toContain('pollAttempt.$.$');
    expect(Object.keys(parameters)).toContain('pollAttempt.$');
    expect(parameters['pollAttempt.$']).toBe('States.MathAdd($.pollAttempt, 1)');
  });

  test('the loop bound routes to FailRelease rather than looping forever', () => {
    const gateC = getStates(extractDefinition(synthTemplate())).GateCChoice as any;
    const bounded = gateC.Choices.find((choice: any) =>
      JSON.stringify(choice).includes('pollAttempt'),
    );

    expect(bounded).toBeDefined();
    expect(gateC.Default).toBe('FailRelease');
  });
});

describe('map item processors', () => {
  test('read the item directly rather than through the map context object', () => {
    // An inline item processor receives each item as `$`. Referencing
    // `$$.Map.Item.Value` resolves against the execution context instead and
    // fails at runtime with "JSONPath could not be found in the input" — a
    // failure no synth-time check catches.
    const rendered = JSON.stringify(extractDefinition(synthTemplate()));

    expect(rendered).not.toContain('$$.Map.Item.Value');
  });

  test('each batch task reads the fields the publish CLI puts on a batch', () => {
    const states = getStates(extractDefinition(synthTemplate()));

    for (const [mapName, taskName, listField] of [
      ['IngestBatches', 'IngestOneBatch', 'Documents.$'],
      ['DeleteBatches', 'DeleteOneDocument', 'DocumentIdentifiers.$'],
    ] as [string, string, string][]) {
      const task = (states[mapName] as any).ItemProcessor.States[taskName];
      expect(task.Parameters['KnowledgeBaseId.$']).toBe('$.knowledgeBaseId');
      expect(task.Parameters['DataSourceId.$']).toBe('$.dataSourceId');
      expect(task.Parameters['ClientToken.$']).toBe('$.clientToken');
      expect(task.Parameters[listField]).toMatch(/^\$\./);
    }
  });
});

describe('state forwarding', () => {
  test('both Pass states carry the same field set', () => {
    // A Pass replaces the state entirely, so a field missing from one of these
    // disappears for the rest of the execution. Two hand-maintained lists drifted
    // apart once already: allowBulkDeletion reached MergePointer but not
    // IncrementPollAttempt, and gate B died on a missing JSONPath mid-release.
    const states = getStates(extractDefinition(synthTemplate()));

    // pollAttempt is excluded: each state sets it rather than forwarding it —
    // MergePointer initializes it to 0, IncrementPollAttempt adds one.
    const fields = (name: string) =>
      new Set(
        Object.keys((states[name] as any).Parameters)
          .map((key) => (key.endsWith('.$') ? key.slice(0, -2) : key))
          .filter((field) => field !== 'pollAttempt'),
      );

    expect(fields('IncrementPollAttempt')).toEqual(fields('MergePointer'));
  });

  test('every field a later state reads is forwarded', () => {
    const def = extractDefinition(synthTemplate());
    const states = getStates(def);
    const forwarded = new Set(
      Object.keys((states.MergePointer as any).Parameters).map((key) =>
        key.endsWith('.$') ? key.slice(0, -2) : key,
      ),
    );

    // Top-level execution fields referenced anywhere in the graph.
    const referenced = new Set(
      // The negative lookbehind skips $$.Execution.Id and similar, which read the
      // context object rather than the execution state.
      [...JSON.stringify(def).matchAll(/(?<!\$)\$\.([a-zA-Z][a-zA-Z0-9]*)/g)].map(
        (match) => match[1],
      ),
    );

    // Fields produced by task results rather than forwarded from the input.
    const produced = new Set([
      'pointerResult', 's3Gate', 'deletionRatioGate', 'ingestStatusGate',
      'smokeGate', 'smokeResult', 'statusResult', 'error', 'Payload',
      'createResult', 'failResult', 'promoteResult', 'markIngestingResult',
      'markTestingResult', 'ingestBatchResults', 'deleteBatchResults',
      'ingestResult', 'deleteResult', 'documentIdentifiers', 'documents',
      'clientToken', 'RetrievalResults', 'DocumentDetails',
    ]);

    const missing = [...referenced].filter(
      (field) => !forwarded.has(field) && !produced.has(field),
    );
    expect(missing).toEqual([]);
  });
});
