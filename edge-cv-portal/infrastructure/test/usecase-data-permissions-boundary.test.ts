/**
 * The UseCase and Data account permissions boundaries
 * (iam/usecase-permissions-boundary-policy.json, iam/data-permissions-boundary-policy.json),
 * put on every role by lib/permissions-boundary.ts in bin/usecase-account-app.ts
 * and bin/data-account-app.ts. The Portal Account's boundary is
 * permissions-boundary.test.ts.
 *
 * For each account:
 * - with -c ddaPermissionsBoundary=<name> every role of the stack carries the
 *   boundary, and without it none does;
 * - the boundary allows every action the stack's roles are granted, and every
 *   action of the account's execution policy (the execution role carries the
 *   boundary too); IAM and STS actions also on the resources they are granted on;
 * - its one Deny refuses a role without the same boundary;
 * - it grants no IAM user, access key, boundary removal, or change to the
 *   installer, an administrator or a boundary policy;
 * - the execution policy may put only this boundary on the stack's roles, and
 *   may read it;
 * - the app applies it before it synthesizes.
 */
import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';
import { DataAccountStack } from '../lib/data-account-stack';
import { applyDdaPermissionsBoundary, PERMISSIONS_BOUNDARY_CONTEXT_KEY } from '../lib/permissions-boundary';
import { UseCaseAccountStack } from '../lib/usecase-account-stack';

const IAM_DIR = path.join(__dirname, '..', '..', 'iam');
const ACCOUNT = '444444444444';
const PORTAL_ACCOUNT = '111111111111';
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

/**
 * Not an IAM service prefix: the Resource Groups Tagging API authorizes as
 * `tag:*`. An action under it grants nothing, so it needs no room in a boundary.
 */
const NOT_IAM_PREFIXES = new Set(['resourcegroupstaggingapi']);

interface AccountCase {
  kind: 'usecase' | 'data';
  boundaryName: string;
  boundaryFile: string;
  execFile: string;
  appFile: string;
  stackClass: string;
  build: (app: cdk.App) => cdk.Stack;
}

const env = { account: ACCOUNT, region: REGION };
const CASES: AccountCase[] = [
  {
    kind: 'usecase',
    boundaryName: 'dda-usecase-permissions-boundary',
    boundaryFile: 'usecase-permissions-boundary-policy.json',
    execFile: 'usecase-cfn-execution-policy.json',
    appFile: 'usecase-account-app.ts',
    stackClass: 'UseCaseAccountStack',
    build: (app) => new UseCaseAccountStack(app, 'DDAPortalUseCaseAccountStack', {
      portalAccountId: PORTAL_ACCOUNT, externalId: 'fixture-eid', env }),
  },
  {
    kind: 'data',
    boundaryName: 'dda-data-permissions-boundary',
    boundaryFile: 'data-permissions-boundary-policy.json',
    execFile: 'data-cfn-execution-policy.json',
    appFile: 'data-account-app.ts',
    stackClass: 'DataAccountStack',
    build: (app) => new DataAccountStack(app, 'DDAPortalDataAccountStack', {
      portalAccountId: PORTAL_ACCOUNT, usecaseAccountIds: [], externalId: 'fixture-eid',
      dataBucketNames: ['dda-test-data-bucket'], env }),
  },
];

/** The stack's template, synthesized with `context` and the boundary aspect. */
function synth(c: AccountCase, context: Record<string, string>): Record<string, any> {
  const app = new cdk.App({ context });
  const stack = c.build(app);
  applyDdaPermissionsBoundary(app);
  return app.synth().getStackByName(stack.stackName).template;
}

const rolesOf = (tpl: Record<string, any>) => Object.entries<any>(tpl.Resources ?? {})
  .filter(([, r]) => r.Type === 'AWS::IAM::Role').map(([id, r]) => ({ id, r }));

/** A literal ARN for a statement resource, or undefined when it is a token the test cannot place. */
const literalResource = (r: unknown, roleArnByLogicalId: Map<string, string>): string | undefined => {
  if (typeof r === 'string') return r;
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

describe.each(CASES)('the $kind account boundary', (c) => {
  const boundary = filled(c.boundaryFile);
  const allows = boundary.Statement.filter((s) => s.Effect === 'Allow');
  const boundaryArn = `arn:aws:iam::${ACCOUNT}:policy/${c.boundaryName}`;

  /** The boundary allows `action` (on `resource`, when given as a literal ARN). */
  const allowsAction = (action: string, resource?: string): boolean =>
    allows.some((s) => asArray(s.Action).some((p) => matches(p, action, true))
      && (resource === undefined || asArray(s.Resource as string | string[]).some((r) => matches(r, resource))));

  /** The grants of `statements` that the boundary does not cover. */
  const outside = (statements: Statement[], roleArn: Map<string, string>): string[] => {
    const missing = new Set<string>();
    for (const s of statements.filter((x) => x.Effect === 'Allow')) {
      if (s.NotAction !== undefined) missing.add(`NotAction ${JSON.stringify(s.NotAction)}`);
      for (const action of asArray(s.Action)) {
        if (NOT_IAM_PREFIXES.has(action.split(':')[0].toLowerCase())) continue;
        if (!allowsAction(action)) { missing.add(action); continue; }
        if (!/^(iam|sts):/i.test(action)) continue;
        for (const r of asArray(s.Resource)) {
          const arn = literalResource(r, roleArn);
          if (arn !== undefined && arn !== '*' && !allowsAction(action, arn)) missing.add(`${action} on ${arn}`);
        }
      }
    }
    return [...missing].sort();
  };

  let withBoundary: Record<string, any>;
  let without: Record<string, any>;
  beforeAll(() => {
    withBoundary = synth(c, { [PERMISSIONS_BOUNDARY_CONTEXT_KEY]: c.boundaryName });
    without = synth(c, {});
  });

  test('every role of the stack carries the boundary when the context names it, and none without', () => {
    const roles = rolesOf(withBoundary);
    expect(roles.length).toBeGreaterThanOrEqual(c.kind === 'usecase' ? 6 : 2);
    for (const { id, r } of roles) {
      expect({ id, boundary: JSON.stringify(r.Properties?.PermissionsBoundary) }).toEqual({
        id, boundary: JSON.stringify({ 'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' },
          `:iam::${ACCOUNT}:policy/${c.boundaryName}`]] }) });
    }
    for (const { id, r } of rolesOf(without)) {
      expect({ id, boundary: r.Properties?.PermissionsBoundary }).toEqual({ id, boundary: undefined });
    }
  });

  test('every role has a fixed name the boundary lets the execution role create', () => {
    for (const { id, r } of rolesOf(withBoundary)) {
      const name = r.Properties?.RoleName;
      expect({ id, name: typeof name }).toEqual({ id, name: 'string' });
      expect({ name, allowed: allowsAction('iam:CreateRole', `arn:aws:iam::${ACCOUNT}:role/${name}`) })
        .toEqual({ name, allowed: true });
    }
  });

  test('the boundary allows every action the roles of the stack are granted', () => {
    const resources: Record<string, any> = withBoundary.Resources ?? {};
    const roleArn = new Map<string, string>();
    const statements: Statement[] = [];
    for (const [id, r] of Object.entries<any>(resources)) {
      if (r.Type === 'AWS::IAM::Role') {
        roleArn.set(id, `arn:aws:iam::${ACCOUNT}:role/${r.Properties.RoleName}`);
        for (const p of asArray(r.Properties?.Policies)) statements.push(...(p as any).PolicyDocument.Statement);
      } else if (r.Type === 'AWS::IAM::Policy' || r.Type === 'AWS::IAM::ManagedPolicy') {
        statements.push(...r.Properties.PolicyDocument.Statement);
      }
    }
    expect(statements.length).toBeGreaterThan(c.kind === 'usecase' ? 20 : 3);
    expect(outside(statements, roleArn)).toEqual([]);
  });

  test('the boundary allows every action of the execution policy', () => {
    expect(outside(filled(c.execFile).Statement, new Map())).toEqual([]);
  });

  test('a role can be created or given a boundary only with this boundary', () => {
    const deny = boundary.Statement.filter((s) => s.Effect === 'Deny');
    const scope = c.kind === 'usecase'
      // Devices' token exchange role, which the Greengrass installer creates without one.
      ? { NotResource: `arn:aws:iam::${ACCOUNT}:role/GreengrassV2TokenExchangeRole` }
      : { Resource: '*' };
    expect(deny).toEqual([{
      Sid: 'NoRoleWithoutThisBoundary',
      Effect: 'Deny',
      Action: ['iam:CreateRole', 'iam:PutRolePermissionsBoundary'],
      ...scope,
      Condition: { StringNotEquals: { 'iam:PermissionsBoundary': boundaryArn } },
    }]);
  });

  test('the boundary grants no IAM user, access key, boundary removal or change outside DDA', () => {
    for (const action of ['iam:CreateUser', 'iam:CreateAccessKey', 'iam:CreateLoginProfile',
      'iam:DeleteRolePermissionsBoundary', 'iam:PutUserPolicy', 'iam:CreateServiceLinkedRole',
      'organizations:LeaveOrganization', 'lambda:CreateFunction', 'ec2:RunInstances', 'kms:ScheduleKeyDeletion']) {
      expect({ action, allowed: allowsAction(action) }).toEqual({ action, allowed: false });
    }
    for (const [action, arn] of [
      ['iam:CreatePolicyVersion', boundaryArn],
      ['iam:CreatePolicyVersion', `arn:aws:iam::${ACCOUNT}:policy/dda-installer-${c.kind}`],
      ['iam:CreatePolicyVersion', `arn:aws:iam::${ACCOUNT}:policy/dda-cfn-exec-${c.kind}`],
      ['iam:CreatePolicyVersion', `arn:aws:iam::${ACCOUNT}:policy/dda-sagemaker-execution-boundary`],
      ['iam:PutRolePolicy', `arn:aws:iam::${ACCOUNT}:role/DDAInstaller`],
      ['iam:AttachRolePolicy', `arn:aws:iam::${ACCOUNT}:role/Admin`],
      ['iam:UpdateAssumeRolePolicy', `arn:aws:iam::${ACCOUNT}:role/cdk-hnb659fds-cfn-exec-role-${ACCOUNT}-${REGION}`],
      ['iam:PassRole', `arn:aws:iam::${ACCOUNT}:role/Admin`],
      ['sts:AssumeRole', `arn:aws:iam::${ACCOUNT}:role/DDAInstaller`],
    ]) {
      expect({ action, arn, allowed: allowsAction(action, arn) }).toEqual({ action, arn, allowed: false });
    }
  });

  test('the execution role can give the stack roles this boundary, and no other, and read it', () => {
    const exec = filled(c.execFile).Statement;
    const roleArns = rolesOf(withBoundary).map(({ r }) => `arn:aws:iam::${ACCOUNT}:role/${r.Properties.RoleName}`);
    const grants = exec.filter((s) => s.Effect === 'Allow'
      && asArray(s.Action).some((a) => matches(a, 'iam:PutRolePermissionsBoundary', true)));
    expect(grants).toEqual([{
      Sid: 'DdaRolesGetOnlyTheDdaBoundary',
      Effect: 'Allow',
      Action: 'iam:PutRolePermissionsBoundary',
      Resource: expect.arrayContaining(roleArns),
      Condition: { StringEquals: { 'iam:PermissionsBoundary': boundaryArn } },
    }]);
    expect(asArray(grants[0].Resource as string[]).length).toBe(roleArns.length);
    expect(exec.filter((s) => s.Sid === 'ReadTheDdaBoundary'))
      .toEqual([{ Sid: 'ReadTheDdaBoundary', Effect: 'Allow', Action: 'iam:GetPolicy', Resource: boundaryArn }]);
  });

  test(`bin/${c.appFile} applies the boundary after the stack and before it synthesizes`, () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'bin', c.appFile), 'utf-8');
    const call = source.indexOf('applyDdaPermissionsBoundary(app);');
    expect(call).toBeGreaterThan(source.indexOf(`new ${c.stackClass}(`));
    expect(call).toBeLessThan(source.indexOf('app.synth();'));
  });
});
