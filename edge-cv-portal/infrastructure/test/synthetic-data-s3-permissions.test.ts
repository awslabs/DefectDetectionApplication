/**
 * Static infrastructure assertions for the synthetic-data-s3-permissions
 * bugfix (synthetic data handler role S3 data-plane grants).
 *
 * Two clearly separated suites:
 *
 * 1. Bug condition exploration (Property 1) — asserts the handler role
 *    carries the S3 data-plane statements the generation worker needs in
 *    single-account setups. EXPECTED TO FAIL on the unfixed stack (the role
 *    has no s3:* statement at all); the same tests validate the fix once it
 *    passes. Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3.
 *
 * 2. Preservation (Property 2) — captures the handler role's existing
 *    statement inventory and asserts, universally over EVERY statement in
 *    EVERY policy attached to the role, that no S3 control-plane action is
 *    granted. MUST PASS both before and after the fix.
 *    Requirements: 3.1, 3.2, 3.3.
 *
 * Conventions follow workflow-manager-gaps-infra.test.ts: synthesize once in
 * beforeAll with a generous timeout (Lambda/layer asset staging is
 * expensive), locate resources via template.findResources, assert on raw
 * CloudFormation properties.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { StorageStack } from '../lib/storage-stack';
import { SyntheticDataStack } from '../lib/synthetic-data-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';
const HANDLER_FUNCTION_NAME = 'dda-synthetic-data-handler';

// Synthesized once: the SyntheticDataStack stages the functions asset and
// three layer assets at synth time, which is expensive.
let syntheticTemplate: Template;

beforeAll(() => {
  const app = new cdk.App();

  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');

  const synthetic = new SyntheticDataStack(app, 'SyntheticData', {
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    trainingJobsTable: storage.trainingJobsTable,
    // The stack throws at synth time on an empty list — always pass one.
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
    userPool: new cognito.UserPool(deps, 'Pool'),
    restApiId: 'testrestapiid',
    restApiRootResourceId: 'testrootresourceid',
    apiStageName: 'prod',
  });

  syntheticTemplate = Template.fromStack(synthetic);
}, 300_000);

/** Logical id of the SyntheticDataHandlerRole AWS::IAM::Role resource. */
function handlerRoleLogicalId(): string {
  const matches = Object.keys(
    syntheticTemplate.findResources('AWS::IAM::Role')
  ).filter((logicalId) => logicalId.startsWith('SyntheticDataHandlerRole'));
  expect(matches).toHaveLength(1);
  return matches[0];
}

/**
 * Every IAM policy statement attached to the handler role: inline
 * AWS::IAM::Policy resources whose Roles reference the role's logical id
 * (plus AWS::IAM::ManagedPolicy for parity with the compute-stack tests,
 * in case CDK ever splits an oversized default policy).
 */
function handlerRoleStatements(): any[] {
  const roleId = handlerRoleLogicalId();
  const policies = [
    ...Object.values(syntheticTemplate.findResources('AWS::IAM::Policy')),
    ...Object.values(
      syntheticTemplate.findResources('AWS::IAM::ManagedPolicy')
    ),
  ] as any[];
  const statements = policies
    .filter((p) => p.Properties.Roles?.some((r: any) => r.Ref === roleId))
    .filter((p) => p.Properties.PolicyDocument?.Statement)
    .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
  expect(statements.length).toBeGreaterThan(0);
  return statements;
}

/** Normalize a statement's Action to a string array. */
function actionsOf(statement: any): string[] {
  return Array.isArray(statement.Action)
    ? statement.Action
    : [statement.Action];
}

/** Normalize a statement's Resource to an array (entries may be tokens). */
function resourcesOf(statement: any): any[] {
  return Array.isArray(statement.Resource)
    ? statement.Resource
    : [statement.Resource];
}

// ---------------------------------------------------------------------------
// Property 1: Bug Condition — Handler Role Carries S3 Data-Plane Grants
//
// EXPECTED TO FAIL on the unfixed stack (no s3:* statement exists on the
// role). These tests encode the expected behavior and validate the fix.
// ---------------------------------------------------------------------------
describe('Property 1: bug condition exploration — S3 data-plane grants (Requirements 1.1, 1.2, 1.3, 2.1, 2.2, 2.3)', () => {
  test('handler role allows s3:GetObject and s3:PutObject at the object level (default allowlist => arn:aws:s3:::*/*)', () => {
    const objectLevel = handlerRoleStatements().filter((s) => {
      const actions = actionsOf(s);
      return (
        s.Effect === 'Allow' &&
        actions.includes('s3:GetObject') &&
        actions.includes('s3:PutObject')
      );
    });
    expect(objectLevel.length).toBeGreaterThanOrEqual(1);
    // Default/empty allowlist: object-level resources cover all buckets.
    expect(
      objectLevel.some((s) =>
        resourcesOf(s).some(
          (r: any) => JSON.stringify(r).includes('arn:aws:s3:::*/*')
        )
      )
    ).toBe(true);
  });

  test('handler role allows s3:ListBucket, s3:GetBucketLocation, s3:GetBucketTagging at the bucket level (default allowlist => arn:aws:s3:::*)', () => {
    const bucketLevel = handlerRoleStatements().filter((s) => {
      const actions = actionsOf(s);
      return (
        s.Effect === 'Allow' &&
        actions.includes('s3:ListBucket') &&
        actions.includes('s3:GetBucketLocation') &&
        actions.includes('s3:GetBucketTagging')
      );
    });
    expect(bucketLevel.length).toBeGreaterThanOrEqual(1);
    // Default/empty allowlist: bucket-level resources cover all buckets
    // (exactly arn:aws:s3:::* — not the object-level */* form).
    expect(
      bucketLevel.some((s) =>
        resourcesOf(s).some((r: any) => {
          const flat = JSON.stringify(r);
          return flat.includes('arn:aws:s3:::*') && !flat.includes('*/*');
        })
      )
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Property 2: Preservation — Existing Grants and Non-Buggy Paths Unchanged
//
// MUST PASS on the unfixed stack (baseline inventory) and keep passing after
// the fix (no regressions).
// ---------------------------------------------------------------------------
describe('Property 2: preservation — existing grants and no S3 control plane (Requirements 3.1, 3.2, 3.3)', () => {
  test('handler role keeps its existing statement inventory (Requirements 3.1, 3.3)', () => {
    const statements = handlerRoleStatements();

    // Bedrock InvokeModel on foundation models + inference profiles.
    const bedrockInvoke = statements.filter((s) =>
      actionsOf(s).includes('bedrock:InvokeModel')
    );
    expect(bedrockInvoke.length).toBeGreaterThanOrEqual(1);
    expect(
      bedrockInvoke.some((s) => {
        const flat = JSON.stringify(resourcesOf(s));
        return (
          flat.includes('arn:aws:bedrock:*::foundation-model/*') &&
          flat.includes(':inference-profile/*')
        );
      })
    ).toBe(true);

    // Bedrock ListFoundationModels (list action, resource '*').
    expect(
      statements.some((s) =>
        actionsOf(s).includes('bedrock:ListFoundationModels')
      )
    ).toBe(true);

    // STS AssumeRole scoped to DDAPortalAccessRole in the trusted account
    // (cross-account access path, Requirement 3.3).
    const assumeRole = statements.filter((s) =>
      actionsOf(s).includes('sts:AssumeRole')
    );
    expect(assumeRole.length).toBeGreaterThanOrEqual(1);
    expect(
      assumeRole.some((s) =>
        resourcesOf(s).some((r: any) =>
          JSON.stringify(r).includes(
            `arn:aws:iam::${TRUSTED_USECASE_ACCOUNT}:role/DDAPortalAccessRole`
          )
        )
      )
    ).toBe(true);

    // Lambda self-invoke scoped to the fixed handler function ARN.
    const selfInvoke = statements.filter((s) =>
      actionsOf(s).includes('lambda:InvokeFunction')
    );
    expect(selfInvoke.length).toBeGreaterThanOrEqual(1);
    expect(
      selfInvoke.some((s) =>
        resourcesOf(s).some((r: any) =>
          JSON.stringify(r).includes(`:function:${HANDLER_FUNCTION_NAME}`)
        )
      )
    ).toBe(true);

    // SageMaker training-job actions on training-job resources.
    const sagemaker = statements.filter((s) => {
      const actions = actionsOf(s);
      return (
        actions.includes('sagemaker:CreateTrainingJob') &&
        actions.includes('sagemaker:DescribeTrainingJob') &&
        actions.includes('sagemaker:ListTrainingJobs') &&
        actions.includes('sagemaker:AddTags')
      );
    });
    expect(sagemaker.length).toBeGreaterThanOrEqual(1);
    expect(
      sagemaker.some((s) =>
        resourcesOf(s).some((r: any) =>
          JSON.stringify(r).includes('arn:aws:sagemaker:*:*:training-job/*')
        )
      )
    ).toBe(true);

    // iam:PassRole restricted to SageMaker via the PassedToService condition.
    const passRole = statements.filter((s) =>
      actionsOf(s).includes('iam:PassRole')
    );
    expect(passRole.length).toBeGreaterThanOrEqual(1);
    expect(
      passRole.some(
        (s) =>
          s.Condition?.StringEquals?.['iam:PassedToService'] ===
          'sagemaker.amazonaws.com'
      )
    ).toBe(true);
  });

  test('no statement anywhere on the handler role grants an S3 control-plane action (Requirement 3.2)', () => {
    // Universal quantification over EVERY statement in EVERY policy attached
    // to the handler role: the synthesized template is deterministic, so
    // this exhaustive assertion is the all-inputs guarantee. Must hold both
    // before AND after the fix (the fix adds data-plane grants only).
    const controlPlanePatterns = [
      /^s3:PutBucketPolicy$/i,
      /^s3:DeleteBucketPolicy$/i,
      /^s3:PutBucketAcl$/i,
      /^s3:PutObjectAcl$/i,
      /^s3:DeleteBucket$/i,
      /^s3:CreateBucket$/i,
      /^s3:PutBucketTagging$/i,
      /^s3:PutEncryptionConfiguration$/i,
      /^s3:PutBucketPublicAccessBlock$/i,
      /^s3:PutLifecycleConfiguration$/i,
      /^s3:PutBucketVersioning$/i,
      /^s3:PutBucketCORS$/i,
      /^s3:PutBucketWebsite$/i,
      /^s3:PutBucketLogging$/i,
      /^s3:PutBucketNotification$/i,
      /^s3:PutReplicationConfiguration$/i,
      /^s3:PutAccelerateConfiguration$/i,
      /^s3:PutBucketOwnershipControls$/i,
      /^s3:PutBucketRequestPayment$/i,
      /^s3:PutBucketObjectLockConfiguration$/i,
    ];

    for (const statement of handlerRoleStatements()) {
      for (const action of actionsOf(statement)) {
        if (typeof action !== 'string') continue;
        for (const pattern of controlPlanePatterns) {
          expect(action).not.toMatch(pattern);
        }
        // Broad wildcards that would implicitly include the control plane.
        expect(action).not.toBe('s3:*');
        expect(action).not.toBe('*');
      }
    }
  });
});

// ---------------------------------------------------------------------------
// Fix checking (Requirement 2.4): allowlist-scoping — a non-empty
// dataBucketAllowlist restricts the data-plane grant to exactly those
// buckets, with name-or-ARN normalization matching the compute stack's.
// Requires a second synth (separate app) with the allowlist prop set.
// ---------------------------------------------------------------------------
describe('Fix checking: dataBucketAllowlist scopes the data-plane grant (Requirements 2.4, 2.5)', () => {
  let allowlistTemplate: Template;

  beforeAll(() => {
    const app = new cdk.App();

    const storage = new StorageStack(app, 'Storage');
    const deps = new cdk.Stack(app, 'Deps');

    const synthetic = new SyntheticDataStack(app, 'SyntheticData', {
      useCasesTable: storage.useCasesTable,
      userRolesTable: storage.userRolesTable,
      auditLogTable: storage.auditLogTable,
      settingsTable: storage.settingsTable,
      trainingJobsTable: storage.trainingJobsTable,
      trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
      // One bare name and one full ARN: both normalize to canonical ARNs.
      dataBucketAllowlist: ['bucket-a', 'arn:aws:s3:::bucket-b'],
      userPool: new cognito.UserPool(deps, 'Pool'),
      restApiId: 'testrestapiid',
      restApiRootResourceId: 'testrootresourceid',
      apiStageName: 'prod',
    });

    allowlistTemplate = Template.fromStack(synthetic);
  }, 300_000);

  /** All statements attached to the handler role in the allowlist synth. */
  function allowlistHandlerRoleStatements(): any[] {
    const matches = Object.keys(
      allowlistTemplate.findResources('AWS::IAM::Role')
    ).filter((logicalId) => logicalId.startsWith('SyntheticDataHandlerRole'));
    expect(matches).toHaveLength(1);
    const roleId = matches[0];
    const policies = [
      ...Object.values(allowlistTemplate.findResources('AWS::IAM::Policy')),
      ...Object.values(
        allowlistTemplate.findResources('AWS::IAM::ManagedPolicy')
      ),
    ] as any[];
    const statements = policies
      .filter((p) => p.Properties.Roles?.some((r: any) => r.Ref === roleId))
      .filter((p) => p.Properties.PolicyDocument?.Statement)
      .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
    expect(statements.length).toBeGreaterThan(0);
    return statements;
  }

  test('bucket-level statement is scoped to exactly the allowlisted bucket ARNs, with no Condition', () => {
    const bucketLevel = allowlistHandlerRoleStatements().filter((s) => {
      const actions = actionsOf(s);
      return (
        s.Effect === 'Allow' &&
        actions.includes('s3:ListBucket') &&
        actions.includes('s3:GetBucketLocation') &&
        actions.includes('s3:GetBucketTagging')
      );
    });
    expect(bucketLevel).toHaveLength(1);
    expect(resourcesOf(bucketLevel[0]).sort()).toEqual([
      'arn:aws:s3:::bucket-a',
      'arn:aws:s3:::bucket-b',
    ]);
    expect(bucketLevel[0].Condition).toBeUndefined();
  });

  test('object-level statement is scoped to exactly the allowlisted buckets\' /* forms, with no Condition', () => {
    const objectLevel = allowlistHandlerRoleStatements().filter((s) => {
      const actions = actionsOf(s);
      return (
        s.Effect === 'Allow' &&
        actions.includes('s3:GetObject') &&
        actions.includes('s3:PutObject')
      );
    });
    expect(objectLevel).toHaveLength(1);
    expect(resourcesOf(objectLevel[0]).sort()).toEqual([
      'arn:aws:s3:::bucket-a/*',
      'arn:aws:s3:::bucket-b/*',
    ]);
    expect(objectLevel[0].Condition).toBeUndefined();
  });
});
