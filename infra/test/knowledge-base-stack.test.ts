import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { KnowledgeBaseStack } from '../lib/knowledge-base-stack';

const env = { account: '123456789012', region: 'us-east-1' };

function synth(): Template {
  const app = new cdk.App();
  const foundation = new FoundationStack(app, 'TestFoundation', { env, corpusId: 'demo' });
  const stack = new KnowledgeBaseStack(app, 'TestKnowledgeBase', {
    env,
    corpusId: 'demo',
    canonicalBucket: foundation.canonicalBucket,
    encryptionKey: foundation.encryptionKey,
    canonicalPrefix: 'canonical/demo',
  });
  return Template.fromStack(stack);
}

describe('KnowledgeBaseStack', () => {
  test('creates a managed knowledge base with a managed embedding model', () => {
    synth().hasResourceProperties('AWS::Bedrock::KnowledgeBase', {
      KnowledgeBaseConfiguration: {
        Type: 'MANAGED',
        ManagedKnowledgeBaseConfiguration: {
          EmbeddingModelType: 'MANAGED',
        },
      },
    });
  });

  test('knowledge base is encrypted with the platform CMK', () => {
    synth().hasResourceProperties('AWS::Bedrock::KnowledgeBase', {
      KnowledgeBaseConfiguration: {
        ManagedKnowledgeBaseConfiguration: {
          ServerSideEncryptionConfiguration: {
            KmsKeyArn: Match.anyValue(),
          },
        },
      },
    });
  });

  test('data source retains data so index content survives stack changes', () => {
    synth().hasResourceProperties('AWS::Bedrock::DataSource', {
      DataDeletionPolicy: 'RETAIN',
    });
  });

  test('data source uses the connector type a managed knowledge base accepts', () => {
    // A MANAGED knowledge base rejects `Type: 'S3'` outright with "Unsupported
    // data source type for MANAGED knowledge base type", which only surfaced on a
    // real deploy. The connector carries the bucket and prefixes inside
    // connectorParameters instead of s3Configuration.
    synth().hasResourceProperties('AWS::Bedrock::DataSource', {
      DataSourceConfiguration: {
        Type: 'MANAGED_KNOWLEDGE_BASE_CONNECTOR',
      },
    });
  });

  test('the connector reads only the configured canonical prefix', () => {
    const template = synth();
    const sources = template.findResources('AWS::Bedrock::DataSource');
    const connector = Object.values(sources)[0].Properties.DataSourceConfiguration
      .ManagedKnowledgeBaseConnectorConfiguration;

    expect(connector.ConnectorParameters.type).toBe('S3');
    expect(connector.ConnectorParameters.filterConfiguration.inclusionPrefixes).toEqual(
      ['canonical/demo/'],
    );
    expect(connector.DeletionProtectionConfiguration.DeletionProtectionStatus).toBe(
      'ENABLED',
    );
  });

  test('service role grants read access scoped to the canonical prefix', () => {
    const template = synth();
    const policies = template.findResources('AWS::IAM::Policy');
    const statements = Object.values(policies).flatMap(
      (policy) => policy.Properties.PolicyDocument.Statement,
    );
    const getObject = statements.find((statement: { Action: string | string[] }) =>
      JSON.stringify(statement.Action).includes('s3:GetObject'),
    );
    expect(JSON.stringify(getObject.Resource)).toContain('canonical/demo/*');
  });

  test('service role does not grant write access to the canonical bucket', () => {
    const template = synth();
    const policies = template.findResources('AWS::IAM::Policy');
    const rendered = JSON.stringify(Object.values(policies));
    expect(rendered).not.toContain('s3:PutObject');
    expect(rendered).not.toContain('s3:DeleteObject');
  });

  test('trust policy is scoped to this account', () => {
    synth().hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Principal: { Service: 'bedrock.amazonaws.com' },
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                'aws:SourceAccount': '123456789012',
              }),
            }),
          }),
        ]),
      },
    });
  });
});

describe('cdk-nag acknowledgements', () => {
  test('the app synthesizes for any corpus id, not just the default', () => {
    // cdk-nag matches wildcard findings by an id embedding the resolved resource
    // string, which contains the corpus prefix. A hardcoded acknowledgement passes
    // for one corpus and fails synth for every other, so prove several work.
    const { execFileSync } = require('child_process');

    for (const corpusId of ['demo', 'prod', 'other-corpus']) {
      expect(() =>
        execFileSync(
          'npx',
          ['cdk', 'synth', '--all', '-c', `corpusId=${corpusId}`],
          {
            cwd: `${__dirname}/..`,
            stdio: 'pipe',
            env: {
              ...process.env,
              CDK_DEFAULT_ACCOUNT: '123456789012',
              CDK_DEFAULT_REGION: 'us-east-1',
            },
          },
        ),
      ).not.toThrow();
    }
  }, 180000);
});
