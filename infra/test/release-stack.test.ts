import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { KnowledgeBaseStack } from '../lib/knowledge-base-stack';
import { ReleaseStack } from '../lib/release-stack';

const env = { account: '123456789012', region: 'us-east-1' };

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

function synth(corpusId = 'demo'): Template {
  const { release } = buildStacks(corpusId);
  return Template.fromStack(release);
}

describe('ReleaseStack – DynamoDB table', () => {
  test('table is encrypted with the platform CMK', () => {
    synth().hasResourceProperties('AWS::DynamoDB::Table', {
      SSESpecification: {
        KMSMasterKeyId: Match.anyValue(),
        SSEEnabled: true,
        SSEType: 'KMS',
      },
    });
  });

  test('table has point-in-time recovery enabled', () => {
    synth().hasResourceProperties('AWS::DynamoDB::Table', {
      PointInTimeRecoverySpecification: {
        PointInTimeRecoveryEnabled: true,
      },
    });
  });

  test('table has composite pk/sk key schema', () => {
    synth().hasResourceProperties('AWS::DynamoDB::Table', {
      KeySchema: Match.arrayWith([
        Match.objectLike({ AttributeName: 'pk', KeyType: 'HASH' }),
        Match.objectLike({ AttributeName: 'sk', KeyType: 'RANGE' }),
      ]),
    });
  });

  test('table uses on-demand billing', () => {
    synth().hasResourceProperties('AWS::DynamoDB::Table', {
      BillingMode: 'PAY_PER_REQUEST',
    });
  });

  test('table is retained on stack deletion', () => {
    const template = synth();
    const tables = template.findResources('AWS::DynamoDB::Table');
    for (const logicalId of Object.keys(tables)) {
      expect(tables[logicalId].DeletionPolicy).toBe('Retain');
    }
  });
});

describe('ReleaseStack – Lambda functions', () => {
  test('exactly three Lambda functions are created', () => {
    synth().resourceCountIs('AWS::Lambda::Function', 3);
  });

  test('verify-s3 function has read but not write on the canonical bucket', () => {
    // Scope the write-action check to the VerifyS3 function's policy only.
    // The publisher role legitimately has s3:PutObject, so checking all policies
    // would produce a false failure. We identify the verify-s3 policy by the
    // role logical ID extracted from the Lambda function itself.
    const template = synth();
    const policies = template.findResources('AWS::IAM::Policy');
    const functions = template.findResources('AWS::Lambda::Function');

    // Find the VerifyS3 Lambda function.
    const verifyEntry = Object.entries(functions).find(([, fn]) =>
      JSON.stringify(fn.Properties.Handler).includes('verify_s3'),
    );
    expect(verifyEntry).toBeDefined();
    const [, verifyFn] = verifyEntry!;
    const verifyRoleLogicalId = verifyFn.Properties.Role?.['Fn::GetAtt']?.[0];
    expect(verifyRoleLogicalId).toBeDefined();

    // Find the policy attached to the VerifyS3 role.
    const verifyPolicy = Object.values(policies).find((policy) => {
      const roles: unknown[] = policy.Properties.Roles ?? [];
      return roles.some((ref) => JSON.stringify(ref).includes(verifyRoleLogicalId));
    });
    expect(verifyPolicy).toBeDefined();

    // The VerifyS3 policy must grant s3:GetObject (read).
    const policyJson = JSON.stringify(verifyPolicy!.Properties.PolicyDocument);
    expect(policyJson).toContain('s3:GetObject');

    // The VerifyS3 policy must NOT grant any write actions.
    expect(policyJson).not.toContain('s3:PutObject');
    expect(policyJson).not.toContain('s3:DeleteObject');
  });

  test('registry function can write to the DynamoDB table', () => {
    const template = synth();
    const policies = template.findResources('AWS::IAM::Policy');
    const allStatements = Object.values(policies).flatMap(
      (policy) => policy.Properties.PolicyDocument.Statement,
    );
    const hasUpdateItem = allStatements.some((stmt: { Action: unknown }) =>
      JSON.stringify(stmt.Action).includes('dynamodb:UpdateItem'),
    );
    const hasPutItem = allStatements.some((stmt: { Action: unknown }) =>
      JSON.stringify(stmt.Action).includes('dynamodb:PutItem'),
    );
    expect(hasUpdateItem).toBe(true);
    expect(hasPutItem).toBe(true);
  });

  test('check-gates function has no AWS policy granting access', () => {
    // CheckGatesFunction is pure data transformation — it must not be granted
    // any AWS permissions. We verify this by finding all Lambda functions and
    // confirming the one whose handler points to check_gates has no associated
    // IAM::Policy beyond the basic execution role managed policy.
    //
    // Strategy: Lambda functions get a generated role. If CheckGatesFunction
    // were granted anything, a new AWS::IAM::Policy resource would be attached
    // to that role. We assert that the only policies in the template belong to
    // the other two functions (verify_s3 and registry_ops).
    const template = synth();

    // Collect all Lambda function logical IDs and their role refs.
    const functions = template.findResources('AWS::Lambda::Function');
    const policies = template.findResources('AWS::IAM::Policy');

    // Find the check_gates handler function.
    const checkGatesEntry = Object.entries(functions).find(([, fn]) =>
      JSON.stringify(fn.Properties.Handler).includes('check_gates'),
    );
    expect(checkGatesEntry).toBeDefined();
    const [checkGatesLogicalId, checkGatesFn] = checkGatesEntry!;

    // The role attached to check_gates.
    const checkGatesRoleRef = checkGatesFn.Properties.Role?.['Fn::GetAtt']?.[0];
    expect(checkGatesRoleRef).toBeDefined();

    // No AWS::IAM::Policy should reference the check_gates role.
    const policiesJson = JSON.stringify(Object.values(policies));
    // Each policy lists the roles it is attached to; check_gates role should not appear.
    const checkGatesRoleRefInPolicy = Object.values(policies).some((policy) => {
      const roles: unknown[] = policy.Properties.Roles ?? [];
      return roles.some((ref) => {
        const refStr = JSON.stringify(ref);
        return refStr.includes(checkGatesRoleRef) || refStr.includes(checkGatesLogicalId);
      });
    });

    expect(checkGatesRoleRefInPolicy).toBe(false);

    // Consume policiesJson to satisfy the linter (we use it as a sanity guard).
    expect(typeof policiesJson).toBe('string');
  });
});

describe('ReleaseStack – publisher role', () => {
  test('publisher role has no bedrock: permission of any kind', () => {
    // This assertion is the critical safety check: the publisher must have no
    // direct path to the knowledge base. Only S3 uploads and state machine starts
    // are allowed; every ingestion must pass through the gated state machine.
    //
    // We search ALL inline policies attached to roles whose trust policy allows
    // the account principal to assume them (i.e. the publisher role). We do NOT
    // search the whole template, because the bedrock service role and knowledge
    // base stack legitimately carry bedrock actions — we only care that the
    // publisher role has none.
    //
    // Implementation: collect all AWS::IAM::Policy resources and all
    // AWS::IAM::Role resources. Find the role with an account-principal trust;
    // then find all policies attached to that role and assert none contain
    // 'bedrock:'.
    const template = synth();

    const roles = template.findResources('AWS::IAM::Role');
    const policies = template.findResources('AWS::IAM::Policy');

    // Locate the publisher role: its trust policy allows the account root principal.
    const publisherRoleEntries = Object.entries(roles).filter(([, role]) => {
      const statements = role.Properties.AssumeRolePolicyDocument?.Statement ?? [];
      return statements.some((stmt: { Principal: unknown }) => {
        const principalStr = JSON.stringify(stmt.Principal);
        // Account root ARN looks like arn:aws:iam::123456789012:root
        return principalStr.includes(':root') || principalStr.includes('iam::');
      });
    });
    expect(publisherRoleEntries.length).toBeGreaterThan(0);

    // For each publisher role, find all attached policies and check for bedrock.
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

  test('publisher role can write to the canonical and registry buckets', () => {
    const template = synth();
    const policies = template.findResources('AWS::IAM::Policy');
    const allStatements = Object.values(policies).flatMap(
      (p) => p.Properties.PolicyDocument.Statement,
    );
    const hasPutObject = allStatements.some((stmt: { Action: unknown }) =>
      JSON.stringify(stmt.Action).includes('s3:PutObject'),
    );
    expect(hasPutObject).toBe(true);
  });
});

describe('ReleaseStack – exposed properties', () => {
  test('deletionRatioThreshold is surfaced on the stack for the state machine', () => {
    const { release } = buildStacks('demo');
    expect(release.deletionRatioThreshold).toBe(0.5);
  });

  test('all required fields are exposed as public readonly properties', () => {
    const { release } = buildStacks('demo');
    expect(release.releaseTable).toBeDefined();
    expect(release.verifyS3Function).toBeDefined();
    expect(release.checkGatesFunction).toBeDefined();
    expect(release.registryFunction).toBeDefined();
    expect(release.publisherRole).toBeDefined();
  });
});

describe('ReleaseStack – CloudFormation outputs', () => {
  test('exports the table name', () => {
    const template = synth();
    const outputs = template.findOutputs('*');
    const tableNameOutput = Object.values(outputs).find((o) =>
      JSON.stringify(o.Export ?? {}).includes('ReleaseTableName'),
    );
    expect(tableNameOutput).toBeDefined();
  });

  test('exports the publisher role ARN', () => {
    const template = synth();
    const outputs = template.findOutputs('*');
    const roleArnOutput = Object.values(outputs).find((o) =>
      JSON.stringify(o.Export ?? {}).includes('PublisherRoleArn'),
    );
    expect(roleArnOutput).toBeDefined();
  });
});

describe('lambda asset contents', () => {
  test('the bundle carries only the kbp package', () => {
    // A denylist previously let config/test.env — a gitignored local file holding
    // account identifiers — into the deployment package. Assert the allowlist so
    // that regression is visible rather than silent.
    const fs = require('fs');
    const os = require('os');
    const path = require('path');
    const { execFileSync } = require('child_process');

    // Synthesize into a scratch directory so an asset left by an earlier run
    // cannot stand in for the current bundle.
    const cdkOut = fs.mkdtempSync(path.join(os.tmpdir(), 'kb-asset-'));

    execFileSync('npx', ['cdk', 'synth', 'ManagedKbRelease', '--output', cdkOut], {
      cwd: path.join(__dirname, '..'),
      stdio: 'pipe',
      env: {
        ...process.env,
        CDK_DEFAULT_ACCOUNT: '123456789012',
        CDK_DEFAULT_REGION: 'us-east-1',
      },
    });

    const assets = fs
      .readdirSync(cdkOut)
      .filter((name: string) => name.startsWith('asset.'))
      .filter((name: string) => fs.existsSync(path.join(cdkOut, name, 'kbp')));

    expect(assets).toHaveLength(1);
    expect(fs.readdirSync(path.join(cdkOut, assets[0]))).toEqual(['kbp']);
  }, 120000);
});
