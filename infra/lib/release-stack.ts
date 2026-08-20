import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import { Construct } from 'constructs';
import { buildReleaseStateMachine, grantStateMachineBedrockPermissions } from './state-machine';

export interface ReleaseStackProps extends cdk.StackProps {
  readonly corpusId: string;
  readonly canonicalBucket: s3.IBucket;
  readonly registryBucket: s3.IBucket;
  readonly encryptionKey: kms.IKey;
  readonly stateMachineLogGroup: logs.ILogGroup;
  readonly knowledgeBaseId: string;
  readonly dataSourceId: string;
  readonly knowledgeBaseArn: string;
  readonly canonicalPrefix: string;
  readonly deletionRatioThreshold: number;
  /** Maximum poll attempts before the status-polling loop fails the release. @default 20 */
  readonly maxPollAttempts?: number;
}

/**
 * Release infrastructure: the DynamoDB audit table, the three gate Lambda
 * functions, and the publisher IAM role.
 *
 * The state machine itself is wired in the next stack (Task 12) — this stack
 * creates only the resources the state machine will consume.
 *
 * Design invariants:
 *   - The publisher role has NO direct path to the knowledge base. It may only
 *     upload objects to S3 and start a state machine execution. Every ingestion
 *     therefore passes through the gated state machine.
 *   - The check-gates Lambda receives NO AWS permissions because it performs
 *     only pure data transformation; granting it nothing is a deliberate
 *     architectural statement.
 */
export class ReleaseStack extends cdk.Stack {
  public readonly releaseTable: dynamodb.Table;
  public readonly verifyS3Function: lambda.Function;
  public readonly checkGatesFunction: lambda.Function;
  public readonly registryFunction: lambda.Function;
  public readonly publisherRole: iam.Role;
  public readonly stateMachine: sfn.StateMachine;
  public readonly deletionRatioThreshold: number;

  constructor(scope: Construct, id: string, props: ReleaseStackProps) {
    super(scope, id, props);

    this.deletionRatioThreshold = props.deletionRatioThreshold;

    // -------------------------------------------------------------------------
    // 1. DynamoDB release table
    // -------------------------------------------------------------------------
    this.releaseTable = new dynamodb.Table(this, 'ReleaseTable', {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.encryptionKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // -------------------------------------------------------------------------
    // 2. Shared Lambda code asset
    //    Allowlisted rather than denylisted. Excluding known-unwanted paths let
    //    config/test.env — a gitignored local file holding account identifiers —
    //    into the deployment package. Only the kbp package is needed at runtime.
    // -------------------------------------------------------------------------
    // The leading '.*' is needed alongside '*': the glob does not match
    // dot-prefixed entries, so .git and friends would otherwise survive.
    const codeAsset = lambda.Code.fromAsset('..', {
      exclude: ['*', '.*', '!kbp', '!kbp/**', '**/__pycache__'],
    });

    // -------------------------------------------------------------------------
    // 3. VerifyS3Function — S3 object and SHA verification (Gate A)
    // -------------------------------------------------------------------------
    this.verifyS3Function = new lambda.Function(this, 'VerifyS3Function', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'kbp.ingestion.handlers.verify_s3.handler',
      code: codeAsset,
      environment: {
        CANONICAL_BUCKET: props.canonicalBucket.bucketName,
      },
      description: `Gate A: S3 object verification for corpus ${props.corpusId}`,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
    });

    // Read-only on the canonical bucket to inspect objects, and on the registry
    // bucket because the gate reads the release manifest to learn which documents
    // this release publishes.
    props.canonicalBucket.grantRead(this.verifyS3Function);
    props.registryBucket.grantRead(this.verifyS3Function);
    props.encryptionKey.grantDecrypt(this.verifyS3Function);

    // -------------------------------------------------------------------------
    // 4. CheckGatesFunction — pure data transformation (Gates B/C/D)
    //    Intentionally receives ZERO AWS grants — it only evaluates data.
    // -------------------------------------------------------------------------
    this.checkGatesFunction = new lambda.Function(this, 'CheckGatesFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'kbp.ingestion.handlers.check_gates.handler',
      code: codeAsset,
      description: `Gates B/C/D: pure gate evaluation for corpus ${props.corpusId}`,
      timeout: cdk.Duration.seconds(10),
      memorySize: 128,
      // No environment variables needed; this function is pure data transformation.
    });

    // -------------------------------------------------------------------------
    // 5. RegistryFunction — DynamoDB state transitions
    // -------------------------------------------------------------------------
    this.registryFunction = new lambda.Function(this, 'RegistryFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'kbp.ingestion.handlers.registry_ops.handler',
      code: codeAsset,
      environment: {
        RELEASE_TABLE: this.releaseTable.tableName,
      },
      description: `Registry operations for corpus ${props.corpusId}`,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
    });

    // Grant read/write on the table, plus KMS encrypt/decrypt.
    this.releaseTable.grantReadWriteData(this.registryFunction);
    props.encryptionKey.grantEncryptDecrypt(this.registryFunction);

    // -------------------------------------------------------------------------
    // 6. Publisher role
    //    Assumable by the AWS account principal (CDK CLI / CI pipeline).
    //    Grants read/write on both buckets and KMS encrypt/decrypt.
    //    Deliberately has NO Bedrock permissions so the publisher cannot bypass
    //    the gated state machine and trigger ingestion directly.
    // -------------------------------------------------------------------------
    this.publisherRole = new iam.Role(this, 'PublisherRole', {
      assumedBy: new iam.AccountRootPrincipal(),
      description: `Publisher role for corpus ${props.corpusId} - no direct KB access`,
    });

    props.canonicalBucket.grantReadWrite(this.publisherRole);
    props.registryBucket.grantReadWrite(this.publisherRole);
    props.encryptionKey.grantEncryptDecrypt(this.publisherRole);
    // -------------------------------------------------------------------------
    // 7. State machine (Task 12)
    // -------------------------------------------------------------------------
    this.stateMachine = buildReleaseStateMachine(this, 'ReleaseStateMachine', {
      verifyS3Function: this.verifyS3Function,
      checkGatesFunction: this.checkGatesFunction,
      registryFunction: this.registryFunction,
      logGroup: props.stateMachineLogGroup,
      knowledgeBaseArn: props.knowledgeBaseArn,
      deletionRatioThreshold: props.deletionRatioThreshold,
      maxPollAttempts: props.maxPollAttempts ?? 20,
    });

    // Grant Bedrock SDK integration permissions to the state machine role.
    grantStateMachineBedrockPermissions(this.stateMachine, props.knowledgeBaseArn);

    // Ingestion both reads CMK-encrypted canonical objects and writes into the
    // CMK-encrypted index, so decrypt alone is not enough — the service also calls
    // GenerateDataKey. Each missing action surfaces only as a ValidationException
    // wrapping an authorization failure on a live execution.
    props.encryptionKey.grantEncryptDecrypt(this.stateMachine);

    // Allow the publisher to start and monitor executions.
    this.stateMachine.grantStartExecution(this.publisherRole);
    this.stateMachine.grantRead(this.publisherRole);

    // -------------------------------------------------------------------------
    // 8. CloudFormation outputs
    // -------------------------------------------------------------------------
    new cdk.CfnOutput(this, 'ReleaseTableName', {
      value: this.releaseTable.tableName,
      exportName: `${this.stackName}-ReleaseTableName`,
    });
    new cdk.CfnOutput(this, 'PublisherRoleArn', {
      value: this.publisherRole.roleArn,
      exportName: `${this.stackName}-PublisherRoleArn`,
    });
    new cdk.CfnOutput(this, 'StateMachineArn', {
      value: this.stateMachine.stateMachineArn,
      exportName: `${this.stackName}-StateMachineArn`,
    });

  }
}
