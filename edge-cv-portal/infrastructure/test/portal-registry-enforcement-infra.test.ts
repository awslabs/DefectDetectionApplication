/**
 * Static infrastructure (CDK) assertions for the
 * portal-jwt-role-privilege-escalation spec (task 6.3).
 *
 * The runtime half of this spec is proven in `edge-cv-portal/backend/tests`
 * against moto; the four claims below can only be proven against a
 * synthesized CloudFormation template, and each one is load-bearing for the
 * fix:
 *
 * 1. **App client auth flows (Requirement 5.1, task 6.1).** The portal app
 *    client exposes SRP (plus the refresh flow CDK always adds) and neither
 *    `ALLOW_USER_PASSWORD_AUTH` nor `ALLOW_ADMIN_USER_PASSWORD_AUTH` — the
 *    flows that let a password set out of band through
 *    `AdminSetUserPassword` be exchanged for portal tokens, which is the
 *    path the incident took.
 * 2. **The enforcement flag on every role-resolving handler (Requirement
 *    2.4, task 5.1).** `PORTAL_REGISTRY_ENFORCED` must reach *every* Lambda
 *    that resolves privilege through `shared_utils` — identified here by the
 *    handler carrying `USER_ROLES_TABLE`, i.e. the Portal_Identity registry
 *    — in all four stacks that hold such handlers (Compute, BuildFleet,
 *    NodeDesigner, SyntheticData). A handler left without it would keep
 *    granting from the `custom:role` claim after the flip, i.e. the
 *    escalation would survive in that route. Asserted in **both** modes: a
 *    default synth deploys `'false'` (the flip is deliberate and manual —
 *    task 5.2), `-c portalRegistryEnforced=true` deploys `'true'`, and the
 *    flag is the *only* difference between the two synths.
 * 3. **The User Manager's registry write grant (task 5.1).** `user_admin.py`
 *    is the registry's writer and needs Get/Query/Scan/Put/Update/Delete on
 *    `dda-portal-user-roles`: Query for the per-Use_Case row sweep on
 *    delete, Scan for the last-PortalAdmin count once enforcement is on.
 * 4. **The out-of-band-administration detection rule (Requirements 6.1,
 *    6.2, 6.3, task 6.2).** The EventBridge rule's pattern (the seven admin
 *    events, scoped to the portal pool), its User-Manager-role exclusion,
 *    and the five attribution fields on the SNS target.
 *
 * Conventions follow `user-admin-audit-grant.test.ts` /
 * `synthetic-data-s3-permissions.test.ts`: synthesize once in `beforeAll`
 * with a generous timeout (Lambda/layer asset staging is expensive), locate
 * resources with `template.findResources`, assert on raw CloudFormation
 * properties.
 *
 * NOTE (learned in task 6.1): `npm run build` must run before `npm test` in
 * this package — jest resolves `../lib/foo` to a stale compiled
 * `lib/foo.js` before `lib/foo.ts`, so an un-rebuilt tree tests the previous
 * source.
 *
 * Deliberately NOT asserted here (recorded in the task 6.3 outcome): the
 * event pattern's *matching* semantics. Deciding that a CloudTrail record
 * from a direct IAM user matches while one from the User Manager's assumed
 * role does not requires EventBridge's own matcher (`aws events
 * test-event-pattern`), which needs an AWS call; re-implementing
 * `anything-but`/`$or` semantics here would only assert my reading of them.
 * The structural assertions below pin exactly the shape that reading
 * produced, including the two-armed `$or` whose omission was the bug the
 * task-6.2 mutation testing caught.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { AuthStack } from '../lib/auth-stack';
import { BuildFleetStack } from '../lib/build-fleet-stack';
import { ComputeStack } from '../lib/compute-stack';
import { NodeDesignerStack } from '../lib/node-designer-stack';
import { StorageStack } from '../lib/storage-stack';
import { SyntheticDataStack } from '../lib/synthetic-data-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';

/** The Lambda environment variable under test. */
const FLAG = 'PORTAL_REGISTRY_ENFORCED';

/**
 * The registry table's environment variable. A handler that carries it is a
 * handler that resolves roles through `shared_utils` — the population the
 * flag must cover.
 */
const REGISTRY_TABLE_ENV = 'USER_ROLES_TABLE';

/** The four stacks holding role-resolving handlers. */
type StackName = 'Compute' | 'BuildFleet' | 'NodeDesigner' | 'SyntheticData';

/**
 * Lower bounds on the number of role-resolving handlers per stack, observed
 * on the synthesized templates (Compute 41, BuildFleet 5, NodeDesigner 7,
 * SyntheticData 1 = 54 handlers).
 *
 * These are lower bounds, not equalities, on purpose: the universally
 * quantified assertions below are the actual property ("every handler
 * carrying USER_ROLES_TABLE carries the flag"), and the bounds exist only to
 * keep those assertions from passing vacuously if a stack stopped
 * synthesizing handlers. Pinning exact counts would make every unrelated
 * spec that adds a portal handler fail here for no security reason.
 */
const MIN_ROLE_RESOLVING_HANDLERS: Record<StackName, number> = {
  Compute: 41,
  BuildFleet: 5,
  NodeDesigner: 7,
  SyntheticData: 1,
};

/** Templates of the four stacks, per enforcement mode. */
const templates: Record<'off' | 'on', Partial<Record<StackName, Template>>> = {
  off: {},
  on: {},
};

/** AuthStack templates: default portal deploy and the SSO variant. */
let authTemplate: Template;
let authSsoTemplate: Template;

/**
 * Synthesizes all four role-resolving stacks in one app, so the expensive
 * asset staging happens once per mode rather than once per stack.
 *
 * `contextValue === undefined` models a plain `cdk deploy` (no
 * `-c portalRegistryEnforced=...`), which is what
 * `deploy-infrastructure.sh` runs when `PORTAL_REGISTRY_ENFORCED` is unset.
 */
function synthesizeStacks(
  mode: 'off' | 'on',
  contextValue?: string,
): void {
  const app = new cdk.App({
    context: contextValue === undefined ? {} : { portalRegistryEnforced: contextValue },
  });

  // The two modes are synthesized into two SEPARATE apps with IDENTICAL
  // stack ids, so the resulting templates are directly comparable (the
  // "nothing but the flag changed" assertion below compares them verbatim;
  // mode-dependent ids would show up as cross-stack import differences).
  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');
  const userPool = new cognito.UserPool(deps, 'Pool');
  const table = (id: string) =>
    new dynamodb.Table(deps, id, {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
    });

  const compute = new ComputeStack(app, 'Compute', {
    userPool,
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

  const buildFleet = new BuildFleetStack(app, 'BuildFleet', {
    userRolesTable: table('UserRoles'),
    auditLogTable: table('AuditLog'),
    settingsTable: table('Settings'),
    userPool,
    restApiId: 'testrestapi',
    restApiRootResourceId: 'testrootresource',
    apiStageName: 'v1',
  });

  const nodeDesigner = new NodeDesignerStack(app, 'NodeDesigner', {
    portalArtifactsBucket: new s3.Bucket(deps, 'Artifacts'),
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    workflowsTable: storage.workflowsTable,
    workflowVersionsTable: storage.workflowVersionsTable,
    testDatasetsTable: storage.testDatasetsTable,
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
    userPool,
    restApiId: 'testrestapi',
    restApiRootResourceId: 'testrootresource',
    apiStageName: 'v1',
  });

  const syntheticData = new SyntheticDataStack(app, 'SyntheticData', {
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    trainingJobsTable: storage.trainingJobsTable,
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
    userPool,
    restApiId: 'testrestapi',
    restApiRootResourceId: 'testrootresource',
    apiStageName: 'v1',
  });

  templates[mode].Compute = Template.fromStack(compute);
  templates[mode].BuildFleet = Template.fromStack(buildFleet);
  templates[mode].NodeDesigner = Template.fromStack(nodeDesigner);
  templates[mode].SyntheticData = Template.fromStack(syntheticData);
}

beforeAll(() => {
  // Mode 'off': no context value at all — a plain deploy.
  synthesizeStacks('off');
  // Mode 'on': the explicit affirmative an operator passes at the flip.
  synthesizeStacks('on', 'true');

  // Each AuthStack gets its own App: `Template.fromStack` synthesizes the
  // whole app, and CDK forbids modifying an app's construct tree afterwards.
  authTemplate = Template.fromStack(new AuthStack(new cdk.App(), 'Auth'));
  authSsoTemplate = Template.fromStack(
    new AuthStack(new cdk.App(), 'AuthSso', {
      ssoEnabled: true,
      ssoMetadataUrl: 'https://example.com/metadata.xml',
      ssoProviderName: 'ExampleSSO',
    }),
  );
}, 900_000);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Every Lambda function in a template, keyed by logical id. */
function lambdaFunctions(template: Template): Record<string, any> {
  return template.findResources('AWS::Lambda::Function') as Record<string, any>;
}

/** A function resource's environment variables ({} when it declares none). */
function envOf(fn: any): Record<string, string> {
  return (fn.Properties?.Environment?.Variables ?? {}) as Record<string, string>;
}

/**
 * Logical ids of the functions that resolve roles through `shared_utils`,
 * i.e. those carrying the Portal_Identity registry table.
 */
function roleResolvingFunctionIds(template: Template): string[] {
  return Object.entries(lambdaFunctions(template))
    .filter(([, fn]) => REGISTRY_TABLE_ENV in envOf(fn))
    .map(([logicalId]) => logicalId)
    .sort();
}

/** Normalize a statement's Action / Resource entry to an array. */
function asArray(value: any): any[] {
  return value === undefined ? [] : Array.isArray(value) ? value : [value];
}

/** True when a policy resource entry references the given table logical id. */
function referencesTable(resource: any, tableLogicalIdFragment: string): boolean {
  return JSON.stringify(resource).includes(tableLogicalIdFragment);
}

/** Logical id of a `createLambdaRole('<name>')` role, by prefix. */
function roleLogicalId(template: Template, prefix: string): string {
  const matches = Object.keys(template.findResources('AWS::IAM::Role')).filter(
    (logicalId) => logicalId.startsWith(prefix),
  );
  expect(matches).toHaveLength(1);
  return matches[0];
}

/**
 * Every IAM policy statement attached to a role: inline `AWS::IAM::Policy`
 * resources plus the `AWS::IAM::ManagedPolicy` overflow policies CDK creates
 * when a default policy grows past the inline size limit.
 */
function statementsOfRole(template: Template, roleLogicalIdValue: string): any[] {
  const policies = [
    ...Object.values(template.findResources('AWS::IAM::Policy')),
    ...Object.values(template.findResources('AWS::IAM::ManagedPolicy')),
  ] as any[];
  const statements = policies
    .filter((p) => p.Properties.Roles?.some((r: any) => r.Ref === roleLogicalIdValue))
    .filter((p) => p.Properties.PolicyDocument?.Statement)
    .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
  expect(statements.length).toBeGreaterThan(0);
  return statements;
}

// ---------------------------------------------------------------------------
// Requirement 5.1 — the portal app client offers SRP only (task 6.1)
// ---------------------------------------------------------------------------
describe('Requirement 5.1: the portal app client exposes no password-based auth flow', () => {
  /**
   * Flows that let an out-of-band `AdminSetUserPassword` password be
   * exchanged for portal tokens without SRP. `userPassword: true` synthesizes
   * the first, `adminUserPassword: true` the second.
   */
  const FORBIDDEN_FLOWS = [
    'ALLOW_USER_PASSWORD_AUTH',
    'ALLOW_ADMIN_USER_PASSWORD_AUTH',
  ];

  /** The only client the AuthStack creates. */
  function portalClient(template: Template): any {
    const clients = Object.values(
      template.findResources('AWS::Cognito::UserPoolClient'),
    ) as any[];
    expect(clients).toHaveLength(1);
    return clients[0];
  }

  test.each<[string, () => Template]>([
    ['default portal deploy', () => authTemplate],
    ['SSO-enabled deploy', () => authSsoTemplate],
  ])(
    '%s: ExplicitAuthFlows is exactly SRP + refresh',
    (_label, getTemplate) => {
      const client = portalClient(getTemplate());
      expect([...client.Properties.ExplicitAuthFlows].sort()).toEqual([
        'ALLOW_REFRESH_TOKEN_AUTH',
        'ALLOW_USER_SRP_AUTH',
      ]);
    },
  );

  test.each<[string, () => Template]>([
    ['default portal deploy', () => authTemplate],
    ['SSO-enabled deploy', () => authSsoTemplate],
  ])(
    '%s: neither password-based flow is present',
    (_label, getTemplate) => {
      const flows: string[] = portalClient(getTemplate()).Properties
        .ExplicitAuthFlows;
      for (const forbidden of FORBIDDEN_FLOWS) {
        expect(flows).not.toContain(forbidden);
      }
    },
  );

  test('the refresh-token flow stays available (backend/functions/auth.py uses REFRESH_TOKEN_AUTH)', () => {
    // Preservation: the portal refreshes its own tokens with
    // AuthFlow='REFRESH_TOKEN_AUTH'. CDK always appends
    // ALLOW_REFRESH_TOKEN_AUTH, so removing the password flows cannot break
    // it — pinned here so a future narrowing has to notice.
    expect(portalClient(authTemplate).Properties.ExplicitAuthFlows).toContain(
      'ALLOW_REFRESH_TOKEN_AUTH',
    );
  });

  test('the client is still the portal client on the portal pool, with the custom:role attribute declared', () => {
    // Non-vacuity: the assertions above are about *the portal's* client and
    // the pool whose custom:role claim the incident abused.
    const client = portalClient(authTemplate);
    expect(client.Properties.ClientName).toBe('dda-portal-client');

    const pools = Object.values(
      authTemplate.findResources('AWS::Cognito::UserPool'),
    ) as any[];
    expect(pools).toHaveLength(1);
    expect(pools[0].Properties.UserPoolName).toBe('dda-portal-users');
    const schemaNames = (pools[0].Properties.Schema as any[]).map(
      (attribute) => attribute.Name,
    );
    expect(schemaNames).toContain('role');
  });
});

// ---------------------------------------------------------------------------
// Requirement 2.4 — the enforcement flag reaches every role-resolving handler
// (task 5.1), default OFF, and changes nothing else
// ---------------------------------------------------------------------------
describe('Requirement 2.4: PORTAL_REGISTRY_ENFORCED on every role-resolving handler', () => {
  const STACKS: StackName[] = [
    'Compute',
    'BuildFleet',
    'NodeDesigner',
    'SyntheticData',
  ];

  describe.each<['off' | 'on', string]>([
    ['off', 'false'],
    ['on', 'true'],
  ])('mode %s', (mode, expectedValue) => {
    test.each(STACKS)(
      `%s: every handler carrying ${REGISTRY_TABLE_ENV} carries ${FLAG}='${expectedValue}'`,
      (stackName) => {
        const template = templates[mode][stackName]!;
        const ids = roleResolvingFunctionIds(template);

        // Non-vacuity: the stack really does synthesize role-resolving
        // handlers (see MIN_ROLE_RESOLVING_HANDLERS).
        expect(ids.length).toBeGreaterThanOrEqual(
          MIN_ROLE_RESOLVING_HANDLERS[stackName],
        );

        const offenders = ids.filter(
          (id) => envOf(lambdaFunctions(template)[id])[FLAG] !== expectedValue,
        );
        expect(offenders).toEqual([]);
      },
    );

    test.each(STACKS)(
      `%s: no function declares ${FLAG} with any other value`,
      (stackName) => {
        const template = templates[mode][stackName]!;
        const values = new Set(
          Object.values(lambdaFunctions(template))
            .map((fn) => envOf(fn)[FLAG])
            .filter((value) => value !== undefined),
        );
        expect([...values]).toEqual([expectedValue]);
      },
    );
  });

  test.each(STACKS)(
    '%s: a plain deploy (no portalRegistryEnforced context) is enforcement-OFF',
    (stackName) => {
      // The flip is deliberate and manual (task 5.2): until the registry is
      // backfilled, enforcement on locks out every user including the
      // bootstrap `admin`, so an operator who forgets the context value must
      // get the safe value.
      const template = templates.off[stackName]!;
      for (const id of roleResolvingFunctionIds(template)) {
        expect(envOf(lambdaFunctions(template)[id])[FLAG]).toBe('false');
      }
    },
  );

  test.each(STACKS)(
    '%s: the flag is the ONLY difference between the two synths (no handler added, removed or otherwise changed)',
    (stackName) => {
      const off = lambdaFunctions(templates.off[stackName]!);
      const on = lambdaFunctions(templates.on[stackName]!);

      expect(Object.keys(on).sort()).toEqual(Object.keys(off).sort());

      for (const id of Object.keys(off).sort()) {
        const strip = (fn: any) => {
          const clone = JSON.parse(JSON.stringify(fn));
          if (clone.Properties?.Environment?.Variables) {
            delete clone.Properties.Environment.Variables[FLAG];
          }
          return clone;
        };
        expect(strip(on[id])).toEqual(strip(off[id]));
      }

      // ...and the flag is present on exactly the same set of handlers.
      const withFlag = (fns: Record<string, any>) =>
        Object.entries(fns)
          .filter(([, fn]) => FLAG in envOf(fn))
          .map(([id]) => id)
          .sort();
      expect(withFlag(on)).toEqual(withFlag(off));
    },
  );
});

// ---------------------------------------------------------------------------
// The User Manager's registry write grant (task 5.1 / Requirement 3)
// ---------------------------------------------------------------------------
describe('The User Manager role can read AND write the Portal_Identity registry', () => {
  /**
   * What `user_admin.py` needs on `dda-portal-user-roles`:
   * GetItem (resolve a row), Query (the per-Use_Case row sweep on delete),
   * Scan (the last-PortalAdmin count under enforcement), and
   * PutItem/UpdateItem/DeleteItem for create / role change / disable-enable /
   * delete. `grantReadWriteData` covers all six.
   */
  const REQUIRED_ACTIONS = [
    'dynamodb:GetItem',
    'dynamodb:Query',
    'dynamodb:Scan',
    'dynamodb:PutItem',
    'dynamodb:UpdateItem',
    'dynamodb:DeleteItem',
  ];

  test('the UserAdmin role holds every registry action on the user-roles table', () => {
    const template = templates.off.Compute!;
    const statements = statementsOfRole(
      template,
      roleLogicalId(template, 'UserAdminRole'),
    );

    const registryStatements = statements.filter(
      (s) =>
        s.Effect === 'Allow' &&
        asArray(s.Resource).some((r) => referencesTable(r, 'UserRolesTable')),
    );
    expect(registryStatements.length).toBeGreaterThanOrEqual(1);

    const granted = new Set(
      registryStatements.flatMap((s) =>
        asArray(s.Action).filter((a) => typeof a === 'string'),
      ),
    );
    for (const action of REQUIRED_ACTIONS) {
      expect([...granted]).toContain(action);
    }
  });

  test('the UserAdminHandler function actually runs as that role', () => {
    // Non-vacuity: the grant above must belong to the role the handler uses,
    // and that same role is the one the detection rule excludes below.
    const template = templates.off.Compute!;
    const userAdminRoleId = roleLogicalId(template, 'UserAdminRole');
    const handlers = Object.entries(lambdaFunctions(template)).filter(
      ([, fn]) => fn.Properties.Handler === 'user_admin.handler',
    );
    expect(handlers).toHaveLength(1);
    expect(handlers[0][1].Properties.Role).toEqual({
      'Fn::GetAtt': [userAdminRoleId, 'Arn'],
    });
  });
});

// ---------------------------------------------------------------------------
// Requirement 6 — detection of Cognito administration outside the portal
// (task 6.2)
// ---------------------------------------------------------------------------
describe('Requirement 6: out-of-band Cognito administration is detected', () => {
  /** The seven pool mutations Requirement 6.1 enumerates. */
  const WATCHED_EVENTS = [
    'AdminAddUserToGroup',
    'AdminCreateUser',
    'AdminDeleteUser',
    'AdminDisableUser',
    'AdminEnableUser',
    'AdminSetUserPassword',
    'AdminUpdateUserAttributes',
  ];

  const RULE_NAME = 'dda-portal-cognito-admin-activity';
  const TOPIC_NAME = 'dda-portal-cognito-admin-alerts';

  /** The detection rule resource (exactly one). */
  function rule(): any {
    const matches = Object.values(
      templates.off.Compute!.findResources('AWS::Events::Rule'),
    ).filter((r: any) => r.Properties.Name === RULE_NAME) as any[];
    expect(matches).toHaveLength(1);
    return matches[0];
  }

  /** The alert topic resource (exactly one), with its logical id. */
  function topic(): [string, any] {
    const matches = Object.entries(
      templates.off.Compute!.findResources('AWS::SNS::Topic'),
    ).filter(([, t]: [string, any]) => t.Properties.TopicName === TOPIC_NAME) as Array<
      [string, any]
    >;
    expect(matches).toHaveLength(1);
    return matches[0];
  }

  test('the alert topic exists', () => {
    const [, resource] = topic();
    expect(resource.Properties.DisplayName).toBe(
      'DDA portal: Cognito pool administration outside the portal',
    );
  });

  test('Requirement 6.1: the rule matches CloudTrail Cognito admin API calls', () => {
    const pattern = rule().Properties.EventPattern;
    expect(pattern.source).toEqual(['aws.cognito-idp']);
    expect(pattern['detail-type']).toEqual(['AWS API Call via CloudTrail']);
    expect(pattern.detail.eventSource).toEqual(['cognito-idp.amazonaws.com']);
  });

  test('Requirement 6.1: exactly the seven watched admin events are matched', () => {
    const pattern = rule().Properties.EventPattern;
    expect([...pattern.detail.eventName].sort()).toEqual(WATCHED_EVENTS);
  });

  test('Requirement 6.1: the rule is scoped to the portal user pool', () => {
    // Several pools exist in a portal account; matching them all would be
    // noise, and matching the wrong one would miss the incident entirely.
    const pattern = rule().Properties.EventPattern;
    const poolIds = pattern.detail.requestParameters.userPoolId;
    expect(poolIds).toHaveLength(1);
    // Cross-stack: the pool is a ComputeStack prop, so the id arrives as a
    // CloudFormation parameter/import reference rather than a literal.
    expect(typeof poolIds[0]).not.toBe('string');
    expect(JSON.stringify(poolIds[0])).toMatch(/Ref|ImportValue|Fn::/);
  });

  test('Requirement 6.3: the portal User Manager role is excluded, in both caller shapes', () => {
    const template = templates.off.Compute!;
    const userAdminRoleId = roleLogicalId(template, 'UserAdminRole');
    const pattern = rule().Properties.EventPattern;

    const arms: any[] = pattern.detail.$or;
    expect(Array.isArray(arms)).toBe(true);
    expect(arms).toHaveLength(2);

    // Arm 1 — every caller that is not an assumed role (IAM user, root,
    // federated). Needed because EventBridge's `anything-but` only matches
    // when the field is PRESENT, and such callers have no
    // sessionContext.sessionIssuer at all: without this arm the direct-IAM
    // -user case (the shape the incident's actor could take) would be
    // silently ignored.
    const typeArm = arms.find((arm) => arm?.userIdentity?.type !== undefined);
    expect(typeArm).toBeDefined();
    expect(typeArm.userIdentity.type).toEqual([
      { 'anything-but': ['AssumedRole'] },
    ]);

    // Arm 2 — every assumed role whose session issuer is not the User
    // Manager's execution role (Requirement 6.3: the portal's own
    // administration raises no signal).
    const issuerArm = arms.find(
      (arm) => arm?.userIdentity?.sessionContext !== undefined,
    );
    expect(issuerArm).toBeDefined();
    const issuerArn =
      issuerArm.userIdentity.sessionContext.sessionIssuer.arn;
    expect(issuerArn).toHaveLength(1);
    expect(Object.keys(issuerArn[0])).toEqual(['anything-but']);
    expect(issuerArn[0]['anything-but']).toEqual([
      { 'Fn::GetAtt': [userAdminRoleId, 'Arn'] },
    ]);
  });

  test('Requirement 6.2: the signal carries event name, caller ARN, source IP, user agent and event time', () => {
    const [topicLogicalId] = topic();
    const targets: any[] = rule().Properties.Targets;
    expect(targets).toHaveLength(1);

    const target = targets[0];
    expect(target.Arn).toEqual({ Ref: topicLogicalId });

    // CDK derives the placeholder names from the JSON paths
    // (`$.detail.eventName` -> `detail-eventName`), so the contract to pin is
    // the set of PATHS the message is built from, plus each one actually
    // being interpolated into the template under its generated name.
    const paths: Record<string, string> = target.InputTransformer.InputPathsMap;
    const template: string = target.InputTransformer.InputTemplate;

    const REQUIRED_PATHS = [
      '$.detail.eventName', // the event name
      '$.detail.userIdentity.arn', // the caller ARN
      '$.detail.sourceIPAddress', // the source IP
      '$.detail.userAgent', // the user agent
      '$.detail.eventTime', // the event time
    ];

    for (const path of REQUIRED_PATHS) {
      const placeholders = Object.entries(paths)
        .filter(([, declaredPath]) => declaredPath === path)
        .map(([placeholder]) => placeholder);
      // Declared exactly once...
      expect(placeholders).toHaveLength(1);
      // ...and actually interpolated into the delivered message.
      expect(template).toContain(`<${placeholders[0]}>`);
    }

    // Every placeholder the template interpolates is declared, so none
    // renders as a literal `<name>` in the delivered message.
    const interpolated = [...template.matchAll(/<([A-Za-z0-9_-]+)>/g)].map(
      (match) => match[1],
    );
    expect(interpolated.length).toBeGreaterThanOrEqual(REQUIRED_PATHS.length);
    for (const placeholder of interpolated) {
      expect(Object.keys(paths)).toContain(placeholder);
    }
  });

  test('EventBridge may publish to the alert topic', () => {
    const [topicLogicalId] = topic();
    const policies = Object.values(
      templates.off.Compute!.findResources('AWS::SNS::TopicPolicy'),
    ) as any[];

    const publishStatements = policies
      .filter((p) =>
        asArray(p.Properties.Topics).some((t: any) => t?.Ref === topicLogicalId),
      )
      .flatMap((p) => p.Properties.PolicyDocument.Statement as any[])
      .filter(
        (s) =>
          s.Effect === 'Allow' &&
          asArray(s.Action).includes('sns:Publish') &&
          JSON.stringify(s.Principal ?? {}).includes('events.amazonaws.com'),
      );

    expect(publishStatements.length).toBeGreaterThanOrEqual(1);
  });

  test('the topic ARN is exported for operators to subscribe to', () => {
    const outputs = templates.off.Compute!.findOutputs('*');
    expect(Object.keys(outputs)).toContain('CognitoAdminActivityTopicArn');
  });

  test('the detection rule is enabled and identical in both enforcement modes', () => {
    // Detection is independent of the enforcement flag: it must be live
    // before, during and after the flip.
    const ruleOf = (mode: 'off' | 'on') => {
      const matches = Object.values(
        templates[mode].Compute!.findResources('AWS::Events::Rule'),
      ).filter((r: any) => r.Properties.Name === RULE_NAME) as any[];
      expect(matches).toHaveLength(1);
      return matches[0];
    };
    expect(ruleOf('off').Properties.State).toBe('ENABLED');
    expect(ruleOf('on')).toEqual(ruleOf('off'));
  });
});
