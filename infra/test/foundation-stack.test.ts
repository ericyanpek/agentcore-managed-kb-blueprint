import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';

function synth(): Template {
  const app = new cdk.App();
  const stack = new FoundationStack(app, 'TestFoundation', {
    env: { account: '123456789012', region: 'us-east-1' },
    corpusId: 'demo',
  });
  return Template.fromStack(stack);
}

describe('FoundationStack', () => {
  test('creates a customer managed key with rotation enabled', () => {
    synth().hasResourceProperties('AWS::KMS::Key', {
      EnableKeyRotation: true,
    });
  });

  test('both buckets are versioned and encrypted with the CMK', () => {
    const template = synth();
    template.resourceCountIs('AWS::S3::Bucket', 2);
    template.allResourcesProperties('AWS::S3::Bucket', {
      VersioningConfiguration: { Status: 'Enabled' },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          {
            ServerSideEncryptionByDefault: {
              SSEAlgorithm: 'aws:kms',
              KMSMasterKeyID: Match.anyValue(),
            },
          },
        ],
      },
    });
  });

  test('both buckets block all public access', () => {
    synth().allResourcesProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  test('stateful resources are retained on stack deletion', () => {
    const template = synth();
    for (const type of ['AWS::S3::Bucket', 'AWS::KMS::Key']) {
      const resources = template.findResources(type);
      for (const logicalId of Object.keys(resources)) {
        expect(resources[logicalId].DeletionPolicy).toBe('Retain');
      }
    }
  });

  test('canonical bucket expires noncurrent versions but registry bucket does not', () => {
    const template = synth();
    const buckets = template.findResources('AWS::S3::Bucket');
    const lifecycles = Object.values(buckets).map(
      (bucket) => bucket.Properties?.LifecycleConfiguration,
    );
    const withExpiry = lifecycles.filter((config) =>
      JSON.stringify(config ?? {}).includes('NoncurrentVersionExpiration'),
    );
    expect(withExpiry).toHaveLength(1);
  });

  test('exposes bucket names and key arn for dependent stacks', () => {
    const app = new cdk.App();
    const stack = new FoundationStack(app, 'TestFoundation', {
      env: { account: '123456789012', region: 'us-east-1' },
      corpusId: 'demo',
    });
    expect(stack.canonicalBucket).toBeDefined();
    expect(stack.registryBucket).toBeDefined();
    expect(stack.encryptionKey).toBeDefined();
  });
});
