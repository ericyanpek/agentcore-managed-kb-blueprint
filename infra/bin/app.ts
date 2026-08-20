#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { FoundationStack } from '../lib/foundation-stack';
import { KnowledgeBaseStack } from '../lib/knowledge-base-stack';
import { ReleaseStack } from '../lib/release-stack';

const app = new cdk.App();

const corpusId = (app.node.tryGetContext('corpusId') as string | undefined) ?? 'demo';
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION,
};

const foundation = new FoundationStack(app, 'ManagedKbFoundation', {
  env,
  corpusId,
  terminationProtection: true,
  description: 'Stateful storage and encryption for the managed KB platform',
});

const knowledgeBase = new KnowledgeBaseStack(app, 'ManagedKbKnowledgeBase', {
  env,
  corpusId,
  canonicalBucket: foundation.canonicalBucket,
  encryptionKey: foundation.encryptionKey,
  canonicalPrefix: `canonical/${corpusId}`,
  terminationProtection: true,
  description: 'Managed knowledge base and its S3 data source (create-only configuration)',
});
knowledgeBase.addStackDependency(foundation);

const release = new ReleaseStack(app, 'ManagedKbRelease', {
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
  description: 'Release gating, audit table, and gate Lambda functions for the managed KB platform',
});
release.addStackDependency(foundation);
release.addStackDependency(knowledgeBase);

// cdk-nag v3: use CDK native Validations.of().acknowledge() instead of the
// removed NagSuppressions class.
//
// AwsSolutions-S1: S3 server access logging is suppressed for this reference
// implementation because object-level access is already audited through
// CloudTrail data events; a separate access log bucket would itself require a
// log bucket and would add no evidence beyond what CloudTrail already captures.
for (const bucket of [foundation.canonicalBucket, foundation.registryBucket]) {
  cdk.Validations.of(bucket).acknowledge({
    id: 'AwsSolutions-S1',
    reason:
      'Object-level access is audited through CloudTrail data events for this ' +
      'reference implementation; a separate access log bucket would itself need ' +
      'a log bucket and adds no evidence not already captured.',
  });
}

// AwsSolutions-IAM5: The s3:GetObject wildcard is bounded to the single canonical
// prefix the data source indexes. Enumerating object keys is impossible because
// the corpus changes with every release, and the s3:ListBucket statement carries
// a StringLike prefix condition restricting it to the same subtree.
//
// cdk-nag v3 only matches a granular id that embeds the resolved resource string,
// which for a cross-stack bucket reference is the producer stack's export name.
// The corpus prefix is interpolated so the acknowledgement holds for any corpusId;
// a fully literal id would fail synth for every corpus but one.
const canonicalObjectExport =
  `${foundation.stackName}:ExportsOutputFnGetAttCanonicalBucket707414CAArn6B3D6A5E`;
cdk.Validations.of(knowledgeBase).acknowledge({
  id: `AwsSolutions-IAM5[Resource::${canonicalObjectExport}/canonical/${corpusId}/*]`,
  reason:
    'The s3:GetObject wildcard is scoped to canonical/<corpusId>/*, the single ' +
    'prefix this data source indexes. Object key enumeration is impossible ' +
    'because the corpus evolves with every release.',
});

// Construct-Annotations: Cross-stack reference strength warning is expected for
// a reference implementation where stack ordering is managed explicitly via
// addStackDependency. Strong references (the default) protect the producer stack
// which is the correct safety posture here — the canonical bucket must not be
// deleted while the knowledge base stack depends on it.
cdk.Validations.of(knowledgeBase).acknowledge({
  id: 'Construct-Annotations::@aws-cdk/core:crossStackReferencesDefaultStrong',
  reason:
    'Strong cross-stack references are the intended behavior: the canonical bucket ' +
    'must not be deleted while the knowledge base stack depends on it. The default ' +
    'strong reference mode is the correct safety posture for this reference implementation.',
});

// --------------------------------------------------------------------------
// ReleaseStack cdk-nag acknowledgements
//
// All IDs below are stable across corpora: none of the bucket export names,
// KMS key export names, or action wildcards embed the corpusId. The existing
// test synthesizes three different corpora to guard against regressions.
// --------------------------------------------------------------------------

// AwsSolutions-IAM4: All three Lambda functions use the AWS managed
// AWSLambdaBasicExecutionRole to publish CloudWatch Logs. Replacing it with
// an inline equivalent would provide no security improvement for a reference
// implementation where log retention is enforced separately.
//
// cdk.Validations.of().acknowledge() rejects IDs containing more than one '::'
// because qualifyId() splits on '::' and throws if parts.length > 2. The
// IAM4 finding embeds '<AWS::Partition>' which produces three '::'s. We bypass
// the helper by writing the acknowledgement directly into node metadata using
// the same key that cdk-nag's isAcknowledged() reads. This is an intentional
// workaround for the mismatch between how CDK generates the finding ID and
// how Validations.acknowledge() validates it.
const iam4Id = 'AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]';
const iam4Reason = 'AWSLambdaBasicExecutionRole grants only cloudwatch:PutLogEvents and ' +
  'logs:CreateLogGroup/Stream. Replacing it with a custom inline policy ' +
  'provides no security benefit for this reference implementation.';
for (const fn of [release.verifyS3Function, release.checkGatesFunction, release.registryFunction]) {
  fn.node.addMetadata(cdk.Validations.ACKNOWLEDGED_RULES_METADATA_KEY, { [iam4Id]: iam4Reason });
}

// AwsSolutions-L1: Python 3.12 is the highest non-preview runtime currently
// available for AWS Lambda. cdk-nag fires this rule whenever the runtime is
// not the very latest; the rule will need revisiting when 3.13 graduates from
// preview. Pinning to an explicit version rather than PYTHON_LATEST avoids
// silent breakage when the default changes mid-deployment.
for (const fn of [release.verifyS3Function, release.checkGatesFunction, release.registryFunction]) {
  cdk.Validations.of(fn).acknowledge({
    id: 'AwsSolutions::AwsSolutions-L1',
    reason:
      'Python 3.12 is the latest stable (non-preview) Lambda runtime. The ' +
      'runtime is pinned explicitly to prevent silent breakage on default ' +
      'runtime upgrades; this acknowledgement should be revisited when 3.13 ' +
      'becomes generally available.',
  });
}

// AwsSolutions-IAM5 for the VerifyS3Function's grantRead on the canonical bucket.
// The s3:GetObject*, s3:GetBucket*, and s3:List* wildcards are generated by the
// CDK grantRead helper. They are bounded to the canonical bucket ARN and its
// objects; no other bucket is in scope.
const canonicalBucketExport =
  `${foundation.stackName}:ExportsOutputFnGetAttCanonicalBucket707414CAArn6B3D6A5E`;
for (const id of [
  'AwsSolutions-IAM5[Action::s3:GetBucket*]',
  'AwsSolutions-IAM5[Action::s3:GetObject*]',
  'AwsSolutions-IAM5[Action::s3:List*]',
  `AwsSolutions-IAM5[Resource::${canonicalBucketExport}/*]`,
]) {
  cdk.Validations.of(release.verifyS3Function).acknowledge({
    id,
    reason:
      'grantRead generates s3:GetObject*, s3:GetBucket*, and s3:List* bounded ' +
      'to the canonical bucket only. The VerifyS3 function is read-only by ' +
      'design; the wildcards are the minimum set the CDK helper produces.',
  });
}

// AwsSolutions-IAM5 for the RegistryFunction's KMS grants.
// kms:GenerateDataKey* and kms:ReEncrypt* are emitted by grantEncryptDecrypt
// which is the minimum grant required for DynamoDB CMK encryption.
for (const id of [
  'AwsSolutions-IAM5[Action::kms:GenerateDataKey*]',
  'AwsSolutions-IAM5[Action::kms:ReEncrypt*]',
]) {
  cdk.Validations.of(release.registryFunction).acknowledge({
    id,
    reason:
      'grantEncryptDecrypt on the platform CMK requires kms:GenerateDataKey* ' +
      'and kms:ReEncrypt* to support DynamoDB envelope encryption. These are ' +
      'the minimum actions the CDK grant helper produces.',
  });
}

// AwsSolutions-IAM5 for the PublisherRole's S3 and KMS grants.
// The publisher needs read/write on both buckets; grantReadWrite generates the
// wildcarded action set and resource /* suffix. No Bedrock permissions are
// granted, so the publisher cannot bypass the state machine gate.
const registryBucketExport =
  `${foundation.stackName}:ExportsOutputFnGetAttRegistryBucket37EB318DArn707F3B68`;
for (const id of [
  'AwsSolutions-IAM5[Action::s3:GetBucket*]',
  'AwsSolutions-IAM5[Action::s3:GetObject*]',
  'AwsSolutions-IAM5[Action::s3:List*]',
  'AwsSolutions-IAM5[Action::s3:Abort*]',
  'AwsSolutions-IAM5[Action::s3:DeleteObject*]',
  'AwsSolutions-IAM5[Action::kms:GenerateDataKey*]',
  'AwsSolutions-IAM5[Action::kms:ReEncrypt*]',
  `AwsSolutions-IAM5[Resource::${canonicalBucketExport}/*]`,
  `AwsSolutions-IAM5[Resource::${registryBucketExport}/*]`,
]) {
  cdk.Validations.of(release.publisherRole).acknowledge({
    id,
    reason:
      'grantReadWrite on the canonical and registry buckets requires the full ' +
      'CDK-generated action set including wildcarded variants and the /* resource ' +
      'suffix. The publisher role carries no Bedrock permissions, so it cannot ' +
      'trigger ingestion directly — every release must pass through the gated ' +
      'state machine.',
  });
}

// Construct-Annotations for the release stack cross-stack references.
// Same rationale as the knowledge-base stack: strong references are the
// correct safety posture when the producer stacks must not be deleted while
// this stack depends on them.
cdk.Validations.of(release).acknowledge({
  id: 'Construct-Annotations::@aws-cdk/core:crossStackReferencesDefaultStrong',
  reason:
    'Strong cross-stack references protect the foundation stack from deletion ' +
    'while the release stack depends on it. This is the correct safety posture ' +
    'for a reference implementation.',
});

// --------------------------------------------------------------------------
// Task 12: state machine cdk-nag acknowledgements
// --------------------------------------------------------------------------

// AwsSolutions-IAM5 for the PublisherRole's grantStartExecution on the state
// machine. grantStartExecution adds a states:GetExecutionHistory / DescribeExecution
// wildcard resource for the execution namespace, and grantRead adds states:* on *.
// Both are standard CDK grant patterns that cannot be narrowed further without
// duplicating the IAM logic.
//
// The execution resource ARN embeds Fn::Select/Fn::Split intrinsics that
// contain '::' tokens; acknowledge() rejects IDs with more than one '::'.
// We bypass it by writing the metadata directly, following the same precedent
// established for IAM4 above.
// Derived from the stack rather than written out: a literal would embed the
// deploying account and region, which both leaks the account id into the
// repository and pins the acknowledgement to one deployment target.
const smExecutionWildcardId =
  `AwsSolutions-IAM5[Resource::arn:${release.partition}:states:${release.region}:` +
  `${release.account}:execution:` +
  '{"Fn::Select":[6,{"Fn::Split":[":",{"Ref":"ReleaseStateMachineAD2BA208"}]}]}:*]';
const smGrantReason =
  'grantStartExecution and grantRead on the release state machine produce a ' +
  'wildcard execution resource ARN and a states:* resource of * respectively. ' +
  'These are the minimum grants the CDK Step Functions grant helpers produce; ' +
  'they scope the publisher to this single state machine only.';
release.publisherRole.node.addMetadata(
  cdk.Validations.ACKNOWLEDGED_RULES_METADATA_KEY,
  { [smExecutionWildcardId]: smGrantReason },
);

cdk.Validations.of(release.publisherRole).acknowledge({
  id: 'AwsSolutions-IAM5[Resource::*]',
  reason: smGrantReason,
});

// AwsSolutions-IAM5 for the ReleaseStateMachine's role policy.
// The Lambda invoke grants add :* to Lambda ARNs (for alias/version invocation).
// The CloudWatch Logs delivery policy requires Resource:* because the Logs
// delivery APIs do not support resource-level permissions.
// All IDs below are stable across corpora because they embed CDK logical IDs
// derived from the fixed construct names ReleaseStateMachine, VerifyS3Function,
// CheckGatesFunction, and RegistryFunction.
for (const id of [
  'AwsSolutions-IAM5[Resource::*]',
  'AwsSolutions-IAM5[Resource::<VerifyS3Function02302B91.Arn>:*]',
  'AwsSolutions-IAM5[Resource::<CheckGatesFunction11013662.Arn>:*]',
  'AwsSolutions-IAM5[Resource::<RegistryFunctionA9FB1E26.Arn>:*]',
]) {
  cdk.Validations.of(release.stateMachine).acknowledge({
    id,
    reason:
      'The state machine role requires lambda:InvokeFunction on :* for each ' +
      'gate Lambda (the :* suffix supports alias/version invocation, which the ' +
      'CDK LambdaInvoke task adds automatically). The Resource:* statement is ' +
      'required for CloudWatch Logs delivery APIs that do not support ' +
      'resource-level permissions.',
  });
}

// cdk-nag v3: register as an IPolicyValidationPlugin, not an IAspect.
cdk.Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));
