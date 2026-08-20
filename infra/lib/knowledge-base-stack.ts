import * as cdk from 'aws-cdk-lib';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface KnowledgeBaseStackProps extends cdk.StackProps {
  readonly corpusId: string;
  readonly canonicalBucket: s3.IBucket;
  readonly encryptionKey: kms.IKey;
  readonly canonicalPrefix: string;
}

/**
 * The managed knowledge base and its data source.
 *
 * Isolated in its own stack because ManagedKnowledgeBaseConfiguration is
 * create-only: changing the embedding configuration replaces the knowledge base
 * and discards the index. Keeping it separate lets the release stack be rebuilt
 * freely without risking indexed content.
 */
export class KnowledgeBaseStack extends cdk.Stack {
  public readonly knowledgeBaseId: string;
  public readonly dataSourceId: string;
  public readonly knowledgeBaseArn: string;

  constructor(scope: Construct, id: string, props: KnowledgeBaseStackProps) {
    super(scope, id, props);

    const serviceRole = new iam.Role(this, 'KnowledgeBaseServiceRole', {
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: {
            'aws:SourceArn': `arn:${this.partition}:bedrock:${this.region}:${this.account}:knowledge-base/*`,
          },
        },
      }),
      description: `Managed KB service role for corpus ${props.corpusId}`,
    });

    // Read-only, and narrowed to the prefix the data source actually indexes.
    serviceRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [
          props.canonicalBucket.arnForObjects(`${props.canonicalPrefix}/*`),
        ],
        conditions: { StringEquals: { 'aws:ResourceAccount': this.account } },
      }),
    );
    serviceRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [props.canonicalBucket.bucketArn],
        conditions: {
          StringEquals: { 'aws:ResourceAccount': this.account },
          'ForAnyValue:StringLike': {
            's3:prefix': [props.canonicalPrefix, `${props.canonicalPrefix}/*`],
          },
        },
      }),
    );
    props.encryptionKey.grantDecrypt(serviceRole);

    const knowledgeBase = new bedrock.CfnKnowledgeBase(this, 'KnowledgeBase', {
      name: `${props.corpusId}-managed-kb`,
      description: `Managed knowledge base for corpus ${props.corpusId}`,
      roleArn: serviceRole.roleArn,
      knowledgeBaseConfiguration: {
        type: 'MANAGED',
        managedKnowledgeBaseConfiguration: {
          embeddingModelType: 'MANAGED',
          serverSideEncryptionConfiguration: {
            kmsKeyArn: props.encryptionKey.keyArn,
          },
        },
      },
      tags: { Project: 'agentcore-managed-kb', CorpusId: props.corpusId },
    });
    knowledgeBase.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    // A MANAGED knowledge base rejects `type: 'S3'` with "Unsupported data source
    // type for MANAGED knowledge base type". It takes
    // MANAGED_KNOWLEDGE_BASE_CONNECTOR instead, where the bucket and prefixes live
    // inside connectorParameters as a JSON string rather than in s3Configuration.
    // The shape below matches a working data source read back from the service.
    //
    // The connector's deletion protection does NOT cover this pipeline. Per the
    // API reference it bounds "the maximum percentage of documents that a sync job
    // can delete", and on exceeding it "the sync skips its delete phase" — it only
    // applies to sync jobs, and it skips rather than fails. Releases here delete
    // through DeleteKnowledgeBaseDocuments, so gate B is the only control that
    // actually blocks an over-large deletion. This setting only guards a manual or
    // future reconciliation sync. Its unit is a percentage, hence 50.
    //
    // AWS::Bedrock::DataSource is not taggable, so cost allocation tags live on
    // the knowledge base and the buckets instead.
    const dataSource = new bedrock.CfnDataSource(this, 'DataSource', {
      knowledgeBaseId: knowledgeBase.attrKnowledgeBaseId,
      name: `${props.corpusId}-canonical-s3`,
      dataDeletionPolicy: 'RETAIN',
      dataSourceConfiguration: {
        type: 'MANAGED_KNOWLEDGE_BASE_CONNECTOR',
        managedKnowledgeBaseConnectorConfiguration: {
          deletionProtectionConfiguration: {
            deletionProtectionStatus: 'ENABLED',
            deletionProtectionThreshold: 50,
          },
          connectorParameters: {
            type: 'S3',
            version: '1',
            aclEnabled: false,
            connectionConfiguration: {
              bucketName: props.canonicalBucket.bucketName,
              bucketOwnerAccountId: this.account,
            },
            filterConfiguration: {
              inclusionPrefixes: [`${props.canonicalPrefix}/`],
              maxFileSizeInMegaBytes: '500',
            },
          },
        },
      },
    });
    dataSource.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    this.knowledgeBaseId = knowledgeBase.attrKnowledgeBaseId;
    this.knowledgeBaseArn = knowledgeBase.attrKnowledgeBaseArn;
    this.dataSourceId = dataSource.attrDataSourceId;

    new cdk.CfnOutput(this, 'KnowledgeBaseId', {
      value: this.knowledgeBaseId,
      exportName: `${this.stackName}-KnowledgeBaseId`,
    });
    new cdk.CfnOutput(this, 'DataSourceId', {
      value: this.dataSourceId,
      exportName: `${this.stackName}-DataSourceId`,
    });
  }
}
