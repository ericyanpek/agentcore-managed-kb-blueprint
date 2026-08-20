import * as cdk from 'aws-cdk-lib';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface FoundationStackProps extends cdk.StackProps {
  readonly corpusId: string;
}

/**
 * Stateful storage and encryption shared by the knowledge base and release
 * stacks. Retained on deletion so tearing down the platform never destroys
 * published content or audit evidence.
 */
export class FoundationStack extends cdk.Stack {
  public readonly encryptionKey: kms.Key;
  public readonly canonicalBucket: s3.Bucket;
  public readonly registryBucket: s3.Bucket;
  public readonly stateMachineLogGroup: logs.LogGroup;

  constructor(scope: Construct, id: string, props: FoundationStackProps) {
    super(scope, id, props);

    this.encryptionKey = new kms.Key(this, 'PlatformKey', {
      description: `Managed KB platform CMK for corpus ${props.corpusId}`,
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Canonical documents are republishable, so noncurrent versions expire.
    this.canonicalBucket = new s3.Bucket(this, 'CanonicalBucket', {
      versioned: true,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: this.encryptionKey,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        { abortIncompleteMultipartUploadAfter: cdk.Duration.days(7) },
        { noncurrentVersionExpiration: cdk.Duration.days(30) },
      ],
    });

    // Manifests are audit evidence, so no expiry rule is configured.
    this.registryBucket = new s3.Bucket(this, 'RegistryBucket', {
      versioned: true,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: this.encryptionKey,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        { abortIncompleteMultipartUploadAfter: cdk.Duration.days(7) },
      ],
    });

    this.stateMachineLogGroup = new logs.LogGroup(this, 'ReleaseStateMachineLogs', {
      retention: logs.RetentionDays.THREE_MONTHS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    new cdk.CfnOutput(this, 'CanonicalBucketName', {
      value: this.canonicalBucket.bucketName,
      exportName: `${this.stackName}-CanonicalBucketName`,
    });
    new cdk.CfnOutput(this, 'RegistryBucketName', {
      value: this.registryBucket.bucketName,
      exportName: `${this.stackName}-RegistryBucketName`,
    });
    new cdk.CfnOutput(this, 'EncryptionKeyArn', {
      value: this.encryptionKey.keyArn,
      exportName: `${this.stackName}-EncryptionKeyArn`,
    });
  }
}
