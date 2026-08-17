/**
 * Bug condition exploration test for the user-manager-datalabeler-role
 * bugfix (defect 1.4): the UserAdmin Lambda role must be allowed to
 * `dynamodb:Query` the audit-log table — `finalize_audit_event`
 * (shared_utils.py) recovers the (event_id, timestamp) range key with a
 * Query, but `createLambdaRole('UserAdmin')` only receives
 * `auditLogTable.grantWriteData` (verified live AccessDeniedException on
 * UserAdminRole2557D264, request id 79dc2dd8-2e10-486a-a0e6-aec5ef805d37).
 *
 * Property 1: Bug Condition — Audit Finalize Permitted.
 * **Validates: Requirements 1.4 (bug condition) / 2.4 (expected behavior
 * once fixed)**
 *
 * EXPECTED TO FAIL on the unfixed stack (the role's audit-table action
 * set is the write-only grantWriteData set — no Query). The same test
 * validates the fix once compute-stack.ts adds
 * `props.auditLogTable.grant(userAdminHandler, 'dynamodb:Query')`
 * (task 3.3).
 *
 * Conventions follow synthetic-data-s3-permissions.test.ts /
 * camera-registry-infra.test.ts: synthesize StorageStack + ComputeStack
 * once in beforeAll with a generous timeout, locate resources via
 * template.findResources, assert on raw CloudFormation properties.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';

// Synthesized once: the ComputeStack stages Lambda/layer assets, which is
// expensive.
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

/** Logical id of the UserAdmin role (createLambdaRole('UserAdmin')). */
function userAdminRoleLogicalId(): string {
  const matches = Object.keys(
    computeTemplate.findResources('AWS::IAM::Role')
  ).filter((logicalId) => logicalId.startsWith('UserAdminRole'));
  expect(matches).toHaveLength(1);
  return matches[0];
}

/**
 * Every IAM policy statement attached to the UserAdmin role: inline
 * AWS::IAM::Policy resources whose Roles reference the role's logical id,
 * plus AWS::IAM::ManagedPolicy (CDK splits oversized default policies
 * into overflow managed policies on this stack's roles).
 */
function userAdminRoleStatements(): any[] {
  const roleId = userAdminRoleLogicalId();
  const policies = [
    ...Object.values(computeTemplate.findResources('AWS::IAM::Policy')),
    ...Object.values(
      computeTemplate.findResources('AWS::IAM::ManagedPolicy')
    ),
  ] as any[];
  const statements = policies
    .filter((p) => p.Properties.Roles?.some((r: any) => r.Ref === roleId))
    .filter((p) => p.Properties.PolicyDocument?.Statement)
    .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
  expect(statements.length).toBeGreaterThan(0);
  return statements;
}

/** Normalize a statement's Action to an array. */
function actionsOf(statement: any): any[] {
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

/**
 * True when a resource entry references the audit-log table: a
 * cross-stack Fn::ImportValue of the StorageStack AuditLogTable export
 * (the table lives in StorageStack; ComputeStack imports its ARN).
 */
function referencesAuditLogTable(resource: any): boolean {
  return JSON.stringify(resource).includes('AuditLogTable');
}

// ---------------------------------------------------------------------------
// Property 1: Bug Condition — UserAdmin role may Query the audit table
// (defect 1.4).
//
// EXPECTED TO FAIL on the unfixed stack: the only audit-table statement on
// the role is the grantWriteData action set (BatchWriteItem / PutItem /
// UpdateItem / DeleteItem / DescribeTable) — no dynamodb:Query.
// ---------------------------------------------------------------------------
describe('Property 1: bug condition exploration — UserAdmin audit-table Query grant (defect 1.4)', () => {
  test('some Allow statement on the UserAdmin role carries dynamodb:Query with the audit-log table in its resources', () => {
    const statements = userAdminRoleStatements();

    const auditQueryStatements = statements.filter(
      (s) =>
        s.Effect === 'Allow' &&
        actionsOf(s).some(
          (a) => typeof a === 'string' && a === 'dynamodb:Query'
        ) &&
        resourcesOf(s).some(referencesAuditLogTable)
    );

    // Diagnostic counterexample on the unfixed stack: log the audit-table
    // statements' action sets (write-only, no Query) so the failure output
    // records the exact counterexample.
    const auditStatementActionSets = statements
      .filter((s) => resourcesOf(s).some(referencesAuditLogTable))
      .map((s) => actionsOf(s).filter((a) => typeof a === 'string'));
    if (auditQueryStatements.length === 0) {
      console.log(
        'COUNTEREXAMPLE — UserAdmin audit-table statement action sets ' +
          '(no dynamodb:Query): ' +
          JSON.stringify(auditStatementActionSets)
      );
    }

    expect(auditQueryStatements.length).toBeGreaterThanOrEqual(1);
  });
});

// ---------------------------------------------------------------------------
// Property 2: Preservation — grant pins (task 2, observation-first).
//
// These cases PASS on the UNFIXED stack and must keep passing after the
// fix: the UserAdmin role's EXISTING statements (audit-table write action
// set, cognito-idp admin actions on the portal pool, ses:SendEmail) are
// unchanged, and base createLambdaRole roles (sampled: Devices,
// Deployments) never gain an audit-table Query — after the fix ONLY the
// UserAdmin role does (Requirement 3.6, design Decision 4).
//
// **Validates: Requirements 3.6**
// ---------------------------------------------------------------------------

/** Logical id of a createLambdaRole('<name>') role by its prefix. */
function roleLogicalId(prefix: string): string {
  const matches = Object.keys(
    computeTemplate.findResources('AWS::IAM::Role')
  ).filter((logicalId) => logicalId.startsWith(prefix));
  expect(matches).toHaveLength(1);
  return matches[0];
}

/** Every IAM policy statement attached to the given role logical id. */
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

describe('Property 2: preservation — UserAdmin existing statements and base-role audit scope (Requirement 3.6)', () => {
  /** The grantWriteData action set observed on the unfixed stack (the
   * task-1 counterexample record). */
  const AUDIT_WRITE_ACTIONS = [
    'dynamodb:BatchWriteItem',
    'dynamodb:PutItem',
    'dynamodb:UpdateItem',
    'dynamodb:DeleteItem',
    'dynamodb:DescribeTable',
  ];

  /** The cognito-idp admin actions granted ONLY to user_admin.py
   * (compute-stack.ts userAdminHandler addToRolePolicy). */
  const COGNITO_ADMIN_ACTIONS = [
    'cognito-idp:AdminSetUserPassword',
    'cognito-idp:AdminUpdateUserAttributes',
    'cognito-idp:ListUsers',
    'cognito-idp:AdminGetUser',
    'cognito-idp:AdminCreateUser',
    'cognito-idp:AdminEnableUser',
    'cognito-idp:AdminDisableUser',
    'cognito-idp:AdminDeleteUser',
  ];

  test('the UserAdmin role keeps its audit-table write action set (grantWriteData)', () => {
    const statements = userAdminRoleStatements();

    const auditWriteStatements = statements.filter(
      (s) =>
        s.Effect === 'Allow' &&
        resourcesOf(s).some(referencesAuditLogTable) &&
        AUDIT_WRITE_ACTIONS.every((action) =>
          actionsOf(s).includes(action)
        )
    );

    expect(auditWriteStatements.length).toBeGreaterThanOrEqual(1);
  });

  test('the UserAdmin role keeps its cognito-idp admin actions', () => {
    const statements = userAdminRoleStatements();

    const cognitoStatements = statements.filter(
      (s) =>
        s.Effect === 'Allow' &&
        COGNITO_ADMIN_ACTIONS.every((action) =>
          actionsOf(s).includes(action)
        )
    );

    expect(cognitoStatements.length).toBeGreaterThanOrEqual(1);
  });

  test('the UserAdmin role keeps its ses:SendEmail grant', () => {
    const statements = userAdminRoleStatements();

    const sesStatements = statements.filter(
      (s) => s.Effect === 'Allow' && actionsOf(s).includes('ses:SendEmail')
    );

    expect(sesStatements.length).toBeGreaterThanOrEqual(1);
  });

  test.each(['DevicesRole', 'DeploymentsRole'])(
    'base createLambdaRole role %s carries NO audit-table dynamodb:Query',
    (rolePrefix) => {
      const statements = roleStatements(roleLogicalId(rolePrefix));
      expect(statements.length).toBeGreaterThan(0);

      const auditQueryStatements = statements.filter(
        (s) =>
          s.Effect === 'Allow' &&
          actionsOf(s).includes('dynamodb:Query') &&
          resourcesOf(s).some(referencesAuditLogTable)
      );

      expect(auditQueryStatements).toHaveLength(0);
    }
  );
});

// ---------------------------------------------------------------------------
// Property 4: Fix Checking — Scoped Audit Query Grant in the Synthesized
// Template (task 4). Runs on the FIXED stack: the Query grant statement's
// resources explicitly name the audit-log table; the grant is scoped to
// exactly the UserAdmin role among the sampled roles; and the UserAdmin
// role's pre-existing statements (audit write set, cognito-idp set,
// ses:SendEmail) are present unchanged.
//
// **Validates: Requirements 2.4, 3.6**
// ---------------------------------------------------------------------------
describe('Property 4: fix check — scoped audit-table Query grant (Requirements 2.4, 3.6)', () => {
  /** Same pinned sets as the preservation block (kept local so the
   * preservation describe stays byte-identical). */
  const AUDIT_WRITE_ACTIONS = [
    'dynamodb:BatchWriteItem',
    'dynamodb:PutItem',
    'dynamodb:UpdateItem',
    'dynamodb:DeleteItem',
    'dynamodb:DescribeTable',
  ];
  const COGNITO_ADMIN_ACTIONS = [
    'cognito-idp:AdminSetUserPassword',
    'cognito-idp:AdminUpdateUserAttributes',
    'cognito-idp:ListUsers',
    'cognito-idp:AdminGetUser',
    'cognito-idp:AdminCreateUser',
    'cognito-idp:AdminEnableUser',
    'cognito-idp:AdminDisableUser',
    'cognito-idp:AdminDeleteUser',
  ];

  /** Allow statements on the given role that carry dynamodb:Query with
   * the audit-log table among their resources. */
  function auditQueryStatementsOf(roleId: string): any[] {
    return roleStatements(roleId).filter(
      (s) =>
        s.Effect === 'Allow' &&
        actionsOf(s).includes('dynamodb:Query') &&
        resourcesOf(s).some(referencesAuditLogTable)
    );
  }

  test("the Query grant statement's resources name the audit-log table (every resource, not just some)", () => {
    // The `auditLogTable.grant(userAdminHandler, 'dynamodb:Query')` fix
    // produces a statement whose action set is exactly dynamodb:Query and
    // whose resources ALL reference the audit-log table (the table ARN
    // import and any derived index ARN) — nothing broader.
    const queryOnlyStatements = userAdminRoleStatements().filter(
      (s) =>
        s.Effect === 'Allow' &&
        actionsOf(s).filter((a) => typeof a === 'string').length === 1 &&
        actionsOf(s).includes('dynamodb:Query')
    );

    expect(queryOnlyStatements.length).toBeGreaterThanOrEqual(1);
    for (const statement of queryOnlyStatements) {
      for (const resource of resourcesOf(statement)) {
        expect(referencesAuditLogTable(resource)).toBe(true);
      }
    }
  });

  test('exactly the UserAdmin role among the sampled roles carries audit-table dynamodb:Query (Decision 4 scope)', () => {
    const sampled: Array<[string, string]> = [
      ['UserAdminRole', userAdminRoleLogicalId()],
      ['DevicesRole', roleLogicalId('DevicesRole')],
      ['DeploymentsRole', roleLogicalId('DeploymentsRole')],
    ];

    const rolesWithAuditQuery = sampled
      .filter(([, roleId]) => auditQueryStatementsOf(roleId).length > 0)
      .map(([name]) => name);

    expect(rolesWithAuditQuery).toEqual(['UserAdminRole']);
  });

  test("the UserAdmin role's pre-existing statements are present unchanged (audit write set, cognito-idp set, ses:SendEmail)", () => {
    const statements = userAdminRoleStatements();

    // grantWriteData audit-table statement intact.
    expect(
      statements.filter(
        (s) =>
          s.Effect === 'Allow' &&
          resourcesOf(s).some(referencesAuditLogTable) &&
          AUDIT_WRITE_ACTIONS.every((action) =>
            actionsOf(s).includes(action)
          )
      ).length
    ).toBeGreaterThanOrEqual(1);

    // cognito-idp admin action set intact.
    expect(
      statements.filter(
        (s) =>
          s.Effect === 'Allow' &&
          COGNITO_ADMIN_ACTIONS.every((action) =>
            actionsOf(s).includes(action)
          )
      ).length
    ).toBeGreaterThanOrEqual(1);

    // ses:SendEmail intact.
    expect(
      statements.filter(
        (s) => s.Effect === 'Allow' && actionsOf(s).includes('ses:SendEmail')
      ).length
    ).toBeGreaterThanOrEqual(1);
  });
});
