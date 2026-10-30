/**
 * The DDA permissions boundary (iam/portal-permissions-boundary-policy.json,
 * lib/permissions-boundary.ts).
 *
 * - With -c ddaPermissionsBoundary=<name> every IAM role of every stack
 *   (custom resource provider roles included) carries the boundary; without
 *   it no role does, so a deploy into an account bootstrapped without the
 *   boundary synthesizes exactly as before.
 * - The boundary allows every action the roles of the compute, node
 *   designer, test runner and build fleet stacks are granted, and every action
 *   of the four Portal execution policies (the execution role carries the
 *   boundary too); IAM and STS actions also on the resources they are granted on.
 * - The same holds for the real app: bin/app.ts synthesized through the CDK
 *   CLI with the IAM synth gate's fixture context and the boundary, every
 *   stack it instantiates included.
 * - bin/app.ts applies it after every stack exists.
 */
import * as cdk from 'aws-cdk-lib';
import { spawnSync } from 'child_process';
import * as os from 'os';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as fs from 'fs';
import * as path from 'path';
import { AuthStack } from '../lib/auth-stack';
import { BuildFleetStack } from '../lib/build-fleet-stack';
import { ComputeStack } from '../lib/compute-stack';
import { NodeDesignerStack } from '../lib/node-designer-stack';
import { applyDdaPermissionsBoundary, PERMISSIONS_BOUNDARY_CONTEXT_KEY } from '../lib/permissions-boundary';
import { StorageStack } from '../lib/storage-stack';
import { TestRunnerStack } from '../lib/test-runner-stack';

const IAM_DIR = path.join(__dirname, '..', '..', 'iam');
const BOUNDARY_NAME = 'dda-portal-permissions-boundary';
const ACCOUNT = '111111111111';
const REGION = 'us-east-1';

type Statement = { Sid?: string; Effect: string; Action?: string | string[]; NotAction?: string | string[];
  Resource?: unknown; NotResource?: unknown; Condition?: unknown };

const asArray = <T>(v: T | T[] | undefined): T[] => (v === undefined ? [] : Array.isArray(v) ? v : [v]);

/** IAM wildcard match (`*` any run, `?` one character), case-insensitive for actions. */
const matches = (pattern: string, value: string, ignoreCase = false): boolean => {
  const re = new RegExp('^' + pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*/g, '.*').replace(/\?/g, '.') + '$', ignoreCase ? 'i' : '');
  return re.test(value);
};

const filled = (file: string): { Statement: Statement[] } => JSON.parse(
  fs.readFileSync(path.join(IAM_DIR, file), 'utf-8').replace(/ACCOUNT_ID/g, ACCOUNT).replace(/REGION/g, REGION));

const boundary = filled('portal-permissions-boundary-policy.json');
const boundaryAllows = boundary.Statement.filter((s) => s.Effect === 'Allow');

/** The boundary allows `action` (on `resource`, when given as a literal ARN). */
const boundaryAllowsAction = (action: string, resource?: string): boolean =>
  boundaryAllows.some((s) => asArray(s.Action).some((p) => matches(p, action, true))
    && (resource === undefined
      || asArray(s.Resource as string | string[]).some((r) => matches(r, resource))));

/** A literal ARN for a statement resource, or undefined when it is a token the test cannot place. */
const literalResource = (r: unknown, roleArnByLogicalId: Map<string, string>): string | undefined => {
  if (typeof r === 'string') return r.replace('${AWS::AccountId}', ACCOUNT);
  const getAtt = (r as any)?.['Fn::GetAtt'];
  if (Array.isArray(getAtt) && getAtt[1] === 'Arn') return roleArnByLogicalId.get(getAtt[0]);
  const join = (r as any)?.['Fn::Join'];
  if (Array.isArray(join) && join[0] === '' && Array.isArray(join[1])) {
    const parts = join[1].map((p: any) => {
      if (typeof p === 'string') return p;
      if (p?.Ref === 'AWS::AccountId') return ACCOUNT;
      if (p?.Ref === 'AWS::Partition') return 'aws';
      if (p?.Ref === 'AWS::Region') return REGION;
      return undefined;
    });
    return parts.every((p: unknown) => typeof p === 'string') ? parts.join('') : undefined;
  }
  return undefined;
};

function buildApp(context: Record<string, string>): cdk.Stack[] {
  const app = new cdk.App({ context: { trustedUseCaseAccountIds: '222222222222', ...context } });
  const env = { account: ACCOUNT, region: REGION };
  const authStack = new AuthStack(app, 'EdgeCVPortalAuthStack', { env });
  const storageStack = new StorageStack(app, 'EdgeCVPortalStorageStack', { env });
  const testRunnerStack = new TestRunnerStack(app, 'EdgeCVPortalTestRunnerStack', {
    env, testRunsTable: storageStack.testRunsTable, portalArtifactsBucket: storageStack.portalArtifactsBucket,
  });
  const computeStack = new ComputeStack(app, 'EdgeCVPortalComputeStack', {
    env,
    suppressTemplateIndentation: true,
    userPool: authStack.userPool,
    userPoolClientId: authStack.userPoolClient.userPoolClientId,
    useCasesTable: storageStack.useCasesTable,
    userRolesTable: storageStack.userRolesTable,
    devicesTable: storageStack.devicesTable,
    auditLogTable: storageStack.auditLogTable,
    trainingJobsTable: storageStack.trainingJobsTable,
    labelingJobsTable: storageStack.labelingJobsTable,
    labelingTeamsTable: storageStack.labelingTeamsTable,
    labelingTasksTable: storageStack.labelingTasksTable,
    preLabeledDatasetsTable: storageStack.preLabeledDatasetsTable,
    modelsTable: storageStack.modelsTable,
    deploymentsTable: storageStack.deploymentsTable,
    settingsTable: storageStack.settingsTable,
    componentsTable: storageStack.componentsTable,
    sharedComponentsTable: storageStack.sharedComponentsTable,
    dataAccountsTable: storageStack.dataAccountsTable,
    workflowsTable: storageStack.workflowsTable,
    workflowVersionsTable: storageStack.workflowVersionsTable,
    testDatasetsTable: storageStack.testDatasetsTable,
    testRunsTable: storageStack.testRunsTable,
    workflowChatSessionsTable: storageStack.workflowChatSessionsTable,
    cameraRegistryTable: storageStack.cameraRegistryTable,
    deviceRegistrationsTable: storageStack.deviceRegistrationsTable,
    portalArtifactsBucket: storageStack.portalArtifactsBucket,
    testRunStateMachine: testRunnerStack.stateMachine,
    trustedUseCaseAccountIds: ['222222222222'],
    dataBucketAllowlist: [],
  });
  const deps = new cdk.Stack(app, 'Deps', { env });
  const table = (id: string) =>
    new dynamodb.Table(deps, id, { partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING } });
  const userPool = new cognito.UserPool(deps, 'Pool');
  const api = { restApiId: 'testrestapi', restApiRootResourceId: 'testrootresource', apiStageName: 'v1' };
  const nodeDesigner = new NodeDesignerStack(app, 'EdgeCVPortalNodeDesignerStack', {
    env,
    portalArtifactsBucket: new s3.Bucket(deps, 'Artifacts'),
    useCasesTable: table('UseCases'),
    userRolesTable: table('UserRoles'),
    auditLogTable: table('AuditLog'),
    settingsTable: table('Settings'),
    workflowsTable: table('Workflows'),
    workflowVersionsTable: table('WorkflowVersions'),
    testDatasetsTable: table('TestDatasets'),
    trustedUseCaseAccountIds: ['222222222222'],
    userPool,
    ...api,
  });
  const buildFleet = new BuildFleetStack(app, 'EdgeCVPortalBuildFleetStack', {
    env,
    userRolesTable: table('BuildUserRoles'),
    auditLogTable: table('BuildAuditLog'),
    settingsTable: table('BuildSettings'),
    userPool,
    ...api,
  });
  applyDdaPermissionsBoundary(app);
  return [authStack, storageStack, testRunnerStack, computeStack, nodeDesigner, buildFleet];
}

/** Every template of the stacks and their nested stacks. */
const templates = (stacks: cdk.Stack[]): Record<string, any> => {
  const assembly = (stacks[0].node.root as cdk.App).synth();
  const out: Record<string, any> = {};
  for (const artifact of assembly.stacks) out[artifact.stackName] = artifact.template;
  for (const file of fs.readdirSync(assembly.directory)) {
    if (file.endsWith('.nested.template.json')) {
      out[file] = JSON.parse(fs.readFileSync(path.join(assembly.directory, file), 'utf-8'));
    }
  }
  return out;
};

const roles = (tpl: Record<string, any>) => Object.entries(tpl)
  .flatMap(([name, t]) => Object.entries<any>(t.Resources ?? {})
    .filter(([, r]) => r.Type === 'AWS::IAM::Role').map(([id, r]) => ({ name, id, r })));

/**
 * Every action the roles and policies of these templates allow that the
 * boundary does not; IAM and STS actions also on the literal resources they
 * are granted on.
 */
function outsideBoundary(tpls: Record<string, any>): string[] {
  const missing = new Set<string>();
  for (const tpl of Object.values(tpls)) {
    const resources: Record<string, any> = tpl.Resources ?? {};
    const roleArn = new Map<string, string>();
    for (const [id, r] of Object.entries<any>(resources)) {
      if (r.Type !== 'AWS::IAM::Role') continue;
      const roleName = r.Properties?.RoleName;
      roleArn.set(id, `arn:aws:iam::${ACCOUNT}:role/${typeof roleName === 'string' ? roleName : 'EdgeCVPortalGenerated'}`);
    }
    const statements: Statement[] = [];
    for (const r of Object.values<any>(resources)) {
      if (r.Type === 'AWS::IAM::Role') {
        for (const p of asArray(r.Properties?.Policies)) statements.push(...(p as any).PolicyDocument.Statement);
      } else if (r.Type === 'AWS::IAM::Policy' || r.Type === 'AWS::IAM::ManagedPolicy') {
        statements.push(...r.Properties.PolicyDocument.Statement);
      }
    }
    for (const s of statements.filter((x) => x.Effect === 'Allow')) {
      if (s.NotAction !== undefined) missing.add(`NotAction ${JSON.stringify(s.NotAction)}`);
      for (const action of asArray(s.Action)) {
        if (!boundaryAllowsAction(action)) { missing.add(action); continue; }
        if (!/^(iam|sts):/i.test(action)) continue;
        for (const r of asArray(s.Resource)) {
          const arn = literalResource(r, roleArn);
          if (arn !== undefined && arn !== '*' && !boundaryAllowsAction(action, arn)) missing.add(`${action} on ${arn}`);
        }
      }
    }
  }
  return [...missing].sort();
}

describe('applyDdaPermissionsBoundary', () => {
  let withBoundary: Record<string, any>;
  let without: Record<string, any>;

  beforeAll(() => {
    withBoundary = templates(buildApp({ [PERMISSIONS_BOUNDARY_CONTEXT_KEY]: BOUNDARY_NAME }));
    without = templates(buildApp({}));
  }, 600_000);

  test('every role of every stack carries the boundary when the context names it', () => {
    const all = roles(withBoundary);
    expect(all.length).toBeGreaterThan(60);
    for (const { name, id, r } of all) {
      expect({ name, id, boundary: JSON.stringify(r.Properties?.PermissionsBoundary) }).toEqual({
        name, id, boundary: JSON.stringify({ 'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' },
          `:iam::${ACCOUNT}:policy/${BOUNDARY_NAME}`]] }) });
    }
  });

  test('no role carries a boundary without the context', () => {
    for (const { name, id, r } of roles(without)) {
      expect({ name, id, boundary: r.Properties?.PermissionsBoundary }).toEqual({ name, id, boundary: undefined });
    }
  });

  test('the boundary allows every action the roles are granted', () => {
    expect(outsideBoundary(withBoundary)).toEqual([]);
  });
});

describe('cdk synth of bin/app.ts with the boundary context', () => {
  // The real app through the CDK CLI, as deploy-infrastructure.sh runs it, with
  // the IAM synth gate's fixture context (portal-app-with-shared-authorizer.test.ts)
  // so the context-gated stacks are in it too. Ambient credentials are stripped
  // so the CLI cannot replace the fixture account.
  let outdir: string;
  let status: number | null;
  let stderr: string;
  let synthesized: Record<string, any>;
  let stackNames: string[];

  beforeAll(() => {
    outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'permissions-boundary-app-'));
    const env: NodeJS.ProcessEnv = { ...process.env };
    for (const key of ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE',
      'AWS_DEFAULT_PROFILE', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI', 'AWS_CONTAINER_CREDENTIALS_FULL_URI',
      'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_ARN']) {
      delete env[key];
    }
    env.CDK_DEFAULT_ACCOUNT = ACCOUNT;
    env.CDK_DEFAULT_REGION = REGION;
    env.AWS_SHARED_CREDENTIALS_FILE = path.join(outdir, 'no-such-credentials');
    env.AWS_CONFIG_FILE = path.join(outdir, 'no-such-config');
    env.AWS_EC2_METADATA_DISABLED = 'true';
    const result = spawnSync(process.execPath, [require.resolve('aws-cdk/bin/cdk'), 'synth', '--quiet',
      '--output', outdir,
      '--context', `portalAccountId=${ACCOUNT}`,
      '--context', 'externalId=fixture-eid',
      '--context', 'trustedUseCaseAccountIds=222222222222,333333333333',
      '--context', 'portalRegistryEnforced=false',
      '--context', 'sharedAuthorizerId=fixture0',
      '--context', `${PERMISSIONS_BOUNDARY_CONTEXT_KEY}=${BOUNDARY_NAME}`,
    ], { cwd: path.join(__dirname, '..'), env, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    status = result.status;
    stderr = result.stderr ?? '';
    synthesized = {};
    stackNames = [];
    if (status === 0) {
      for (const file of fs.readdirSync(outdir)) {
        if (!file.endsWith('.template.json')) continue;
        synthesized[file] = JSON.parse(fs.readFileSync(path.join(outdir, file), 'utf-8'));
        if (!file.endsWith('.nested.template.json')) stackNames.push(file.replace('.template.json', ''));
      }
    }
  }, 900_000);

  afterAll(() => {
    if (outdir) fs.rmSync(outdir, { recursive: true, force: true });
  });

  test('synthesizes every stack bin/app.ts instantiates', () => {
    if (status !== 0) throw new Error(`cdk synth exited ${status}\n${stderr.slice(-4000)}`);
    const source = fs.readFileSync(path.join(__dirname, '..', 'bin', 'app.ts'), 'utf-8');
    const expected = Array.from(source.matchAll(/new\s+(\w+Stack)\s*\(\s*app\s*,/g), (m) => `EdgeCVPortal${m[1]}`);
    expect(expected.length).toBeGreaterThanOrEqual(11);
    expect(stackNames.sort()).toEqual(expected.sort());
  });

  test('every role of every stack and nested stack carries the boundary', () => {
    const all = roles(synthesized);
    expect(all.length).toBeGreaterThan(90);
    for (const { name, id, r } of all) {
      expect({ name, id, boundary: JSON.stringify(r.Properties?.PermissionsBoundary ?? null) })
        .toEqual({ name, id, boundary: expect.stringContaining(`:policy/${BOUNDARY_NAME}`) });
    }
  });

  test('the boundary allows every action the roles of the whole app are granted', () => {
    expect(Object.keys(synthesized).length).toBeGreaterThan(11);
    expect(outsideBoundary(synthesized)).toEqual([]);
  });
});

describe('the boundary and the execution policies', () => {
  test('the boundary allows every action of the four Portal execution policies', () => {
    const missing: string[] = [];
    for (const file of ['portal-cfn-execution-iam-policy.json', 'portal-cfn-execution-app-policy.json',
      'portal-cfn-execution-platform-policy.json', 'portal-cfn-execution-network-policy.json']) {
      for (const s of filled(file).Statement.filter((x) => x.Effect === 'Allow')) {
        for (const action of asArray(s.Action)) {
          if (!boundaryAllowsAction(action)) { missing.push(`${file}: ${action}`); continue; }
          if (!action.startsWith('iam:')) continue;
          for (const r of asArray(s.Resource as string | string[])) {
            if (r !== '*' && !boundaryAllowsAction(action, r)) missing.push(`${file}: ${action} on ${r}`);
          }
        }
      }
    }
    expect(missing).toEqual([]);
  });

  test('a role can be created or given a boundary only with this boundary, the device role excepted', () => {
    const deny = boundary.Statement.filter((s) => s.Effect === 'Deny');
    expect(deny).toEqual([{
      Sid: 'NoRoleWithoutThisBoundary',
      Effect: 'Deny',
      Action: ['iam:CreateRole', 'iam:PutRolePermissionsBoundary'],
      NotResource: `arn:aws:iam::${ACCOUNT}:role/GreengrassV2TokenExchangeRole`,
      Condition: { StringNotEquals: { 'iam:PermissionsBoundary': `arn:aws:iam::${ACCOUNT}:policy/${BOUNDARY_NAME}` } },
    }]);
  });

  test('the execution role can give the portal roles this boundary, and no other', () => {
    // CloudFormation adds the boundary to a role an earlier deploy created with
    // iam:PutRolePermissionsBoundary, so an upgrade needs it in the execution policy.
    const grants = filled('portal-cfn-execution-iam-policy.json').Statement.filter((s) => s.Effect === 'Allow'
      && asArray(s.Action).some((a) => matches(a, 'iam:PutRolePermissionsBoundary', true)));
    expect(grants).toEqual([{
      Sid: 'DdaRolesGetOnlyTheDdaBoundary',
      Effect: 'Allow',
      Action: 'iam:PutRolePermissionsBoundary',
      Resource: [`arn:aws:iam::${ACCOUNT}:role/EdgeCVPortal*`, `arn:aws:iam::${ACCOUNT}:role/DDAStationProvisioningRole`,
        `arn:aws:iam::${ACCOUNT}:role/dda-build-role`],
      Condition: { StringEquals: { 'iam:PermissionsBoundary': `arn:aws:iam::${ACCOUNT}:policy/${BOUNDARY_NAME}` } },
    }]);
    // CloudFormation also reads the boundary policy when it sets it on a role.
    expect(filled('portal-cfn-execution-iam-policy.json').Statement.filter((s) => s.Sid === 'ReadTheDdaBoundary'))
      .toEqual([{ Sid: 'ReadTheDdaBoundary', Effect: 'Allow', Action: 'iam:GetPolicy',
        Resource: `arn:aws:iam::${ACCOUNT}:policy/${BOUNDARY_NAME}` }]);
  });

  test('the boundary grants no IAM user, access key, boundary removal or policy change outside DDA', () => {
    for (const action of ['iam:CreateUser', 'iam:CreateAccessKey', 'iam:CreateLoginProfile',
      'iam:DeleteRolePermissionsBoundary', 'iam:PutUserPolicy', 'organizations:LeaveOrganization']) {
      expect({ action, allowed: boundaryAllowsAction(action) }).toEqual({ action, allowed: false });
    }
    for (const [action, arn] of [
      ['iam:CreatePolicyVersion', `arn:aws:iam::${ACCOUNT}:policy/${BOUNDARY_NAME}`],
      ['iam:CreatePolicyVersion', `arn:aws:iam::${ACCOUNT}:policy/dda-installer-portal`],
      ['iam:PutRolePolicy', `arn:aws:iam::${ACCOUNT}:role/DDAInstaller`],
      ['iam:AttachRolePolicy', `arn:aws:iam::${ACCOUNT}:role/Admin`],
      ['iam:PassRole', `arn:aws:iam::${ACCOUNT}:role/Admin`],
    ]) {
      expect({ action, arn, allowed: boundaryAllowsAction(action, arn) }).toEqual({ action, arn, allowed: false });
    }
  });

  test('bin/app.ts applies the boundary after every stack and before it synthesizes', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'bin', 'app.ts'), 'utf-8');
    const call = source.indexOf('applyDdaPermissionsBoundary(app);');
    expect(call).toBeGreaterThan(source.lastIndexOf('new FrontendStack('));
    expect(call).toBeLessThan(source.indexOf('app.synth();'));
  });

  test('an invalid policy name is refused', () => {
    const app = new cdk.App({ context: { [PERMISSIONS_BOUNDARY_CONTEXT_KEY]: 'not a name' } });
    new cdk.Stack(app, 'S');
    expect(() => applyDdaPermissionsBoundary(app)).toThrow(/must be an IAM policy name/);
  });
});
