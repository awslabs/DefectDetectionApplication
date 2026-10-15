/**
 * Use-case bucket configuration IAM (portal-usecase-bucket-config, design
 * §6.1; Requirements 9.12, 15.5).
 *
 * POST /s3-buckets (usecases.py) creates a bucket for a use case, tags it in
 * the CreateBucket request and enables ABAC on it. The UseCases Lambda role
 * — and only that role — therefore carries exactly three statements:
 *
 *   - s3:CreateBucket on arn:aws:s3:::*
 *   - s3:TagResource on arn:aws:s3:::* conditioned on
 *     StringEquals aws:RequestTag/dda-portal:created-by = portal
 *   - s3:PutBucketABAC on arn:aws:s3:::*
 *
 * The createLambdaRole factory (shared by every Lambda role) grants
 * s3:CreateBucket on the portal artifacts bucket only, so no other role may
 * create an arbitrary bucket, tag one or enable ABAC on one.
 *
 * Conventions follow user-admin-audit-grant.test.ts /
 * synthetic-data-s3-permissions.test.ts: synthesize StorageStack +
 * ComputeStack once in beforeAll, locate resources via
 * template.findResources, assert on raw CloudFormation properties.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

// Fixture account id (twelve ones), assembled so no identifier-shaped literal
// lands in the source.
const TRUSTED_USECASE_ACCOUNT = '1'.repeat(12);
const ANY_BUCKET = 'arn:aws:s3:::*';
const CREATED_BY_CONDITION = {
  StringEquals: { 'aws:RequestTag/dda-portal:created-by': 'portal' },
};

let computeTemplate: Template;

beforeAll(() => {
  const app = new cdk.App();
  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');
  const compute = new ComputeStack(app, 'Compute', {
    userPool: new cognito.UserPool(deps, 'Pool'),
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    devicesTable: storage.devicesTable,
    auditLogTable: storage.auditLogTable,
    trainingJobsTable: storage.trainingJobsTable,
    labelingJobsTable: storage.labelingJobsTable,
    labelingTeamsTable: storage.labelingTeamsTable,
    labelingTasksTable: storage.labelingTasksTable,
    preLabeledDatasetsTable: storage.preLabeledDatasetsTable,
    modelsTable: storage.modelsTable,
    deploymentsTable: storage.deploymentsTable,
    settingsTable: storage.settingsTable,
    componentsTable: storage.componentsTable,
    sharedComponentsTable: storage.sharedComponentsTable,
    dataAccountsTable: storage.dataAccountsTable,
    workflowsTable: storage.workflowsTable,
    workflowVersionsTable: storage.workflowVersionsTable,
    testDatasetsTable: storage.testDatasetsTable,
    testRunsTable: storage.testRunsTable,
    workflowChatSessionsTable: storage.workflowChatSessionsTable,
    cameraRegistryTable: storage.cameraRegistryTable,
    deviceRegistrationsTable: storage.deviceRegistrationsTable,
    portalArtifactsBucket: storage.portalArtifactsBucket,
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
  });
  computeTemplate = Template.fromStack(compute);
}, 300_000);

/** Logical id of a createLambdaRole('<name>') role by its prefix. */
function roleLogicalId(prefix: string): string {
  const matches = Object.keys(
    computeTemplate.findResources('AWS::IAM::Role')
  ).filter((logicalId) => logicalId.startsWith(prefix));
  expect(matches).toHaveLength(1);
  return matches[0];
}

/**
 * Every IAM policy statement attached to the given role logical id: inline
 * AWS::IAM::Policy resources whose Roles reference the role, plus
 * AWS::IAM::ManagedPolicy (CDK splits oversized default policies into
 * overflow managed policies on this stack's roles).
 */
function roleStatements(roleId: string): any[] {
  const policies = [
    ...Object.values(computeTemplate.findResources('AWS::IAM::Policy')),
    ...Object.values(
      computeTemplate.findResources('AWS::IAM::ManagedPolicy')
    ),
  ] as any[];
  return policies
    .filter((p) => p.Properties.Roles?.some((r: any) => r.Ref === roleId))
    .filter((p) => p.Properties.PolicyDocument?.Statement)
    .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
}

function actionsOf(statement: any): any[] {
  return Array.isArray(statement.Action)
    ? statement.Action
    : [statement.Action];
}

function resourcesOf(statement: any): any[] {
  return Array.isArray(statement.Resource)
    ? statement.Resource
    : [statement.Resource];
}

/** Allow statements on the role carrying `action` on arn:aws:s3:::*. */
function anyBucketStatements(roleId: string, action: string): any[] {
  return roleStatements(roleId).filter(
    (s) =>
      s.Effect === 'Allow' &&
      actionsOf(s).includes(action) &&
      resourcesOf(s).includes(ANY_BUCKET)
  );
}

describe('UseCases role: use-case bucket configuration grants', () => {
  test('s3:CreateBucket on any bucket, unconditioned', () => {
    const statements = anyBucketStatements(
      roleLogicalId('UseCasesRole'),
      's3:CreateBucket'
    );
    expect(statements).toHaveLength(1);
    expect(statements[0].Condition).toBeUndefined();
  });

  test('s3:TagResource on any bucket, only while asserting the portal creation marker', () => {
    const statements = anyBucketStatements(
      roleLogicalId('UseCasesRole'),
      's3:TagResource'
    );
    expect(statements).toHaveLength(1);
    expect(statements[0].Condition).toEqual(CREATED_BY_CONDITION);
  });

  test('s3:PutBucketABAC (that spelling) on any bucket, unconditioned', () => {
    const statements = anyBucketStatements(
      roleLogicalId('UseCasesRole'),
      's3:PutBucketABAC'
    );
    expect(statements).toHaveLength(1);
    expect(statements[0].Condition).toBeUndefined();
    // One spelling throughout the stack: no case variant of the action.
    const variants = roleStatements(roleLogicalId('UseCasesRole'))
      .flatMap(actionsOf)
      .filter(
        (a) =>
          typeof a === 'string' &&
          a.toLowerCase() === 's3:putbucketabac' &&
          a !== 's3:PutBucketABAC'
      );
    expect(variants).toEqual([]);
  });

  test('no s3:PutBucketTagging anywhere on the UseCases role', () => {
    const actions = roleStatements(roleLogicalId('UseCasesRole'))
      .flatMap(actionsOf)
      .filter((a) => typeof a === 'string');
    expect(actions).not.toContain('s3:PutBucketTagging');
  });
});

describe('Every other Lambda role: none of the three grants', () => {
  test('only the UseCases role may create, tag or enable ABAC on an arbitrary bucket', () => {
    const useCasesRoleId = roleLogicalId('UseCasesRole');
    const otherRoleIds = Object.keys(
      computeTemplate.findResources('AWS::IAM::Role')
    ).filter((logicalId) => logicalId !== useCasesRoleId);
    expect(otherRoleIds.length).toBeGreaterThan(10);

    const offenders: string[] = [];
    for (const roleId of otherRoleIds) {
      for (const s of roleStatements(roleId)) {
        if (s.Effect !== 'Allow') continue;
        const actions = actionsOf(s).filter((a) => typeof a === 'string');
        const onAnyBucket = resourcesOf(s).includes(ANY_BUCKET);
        if (actions.includes('s3:CreateBucket') && onAnyBucket) {
          offenders.push(`${roleId}: s3:CreateBucket on ${ANY_BUCKET}`);
        }
        if (actions.includes('s3:TagResource')) {
          offenders.push(`${roleId}: s3:TagResource`);
        }
        if (actions.some((a) => a.toLowerCase() === 's3:putbucketabac')) {
          offenders.push(`${roleId}: s3:PutBucketABAC`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
