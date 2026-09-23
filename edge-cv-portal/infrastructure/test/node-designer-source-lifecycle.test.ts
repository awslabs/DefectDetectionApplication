/**
 * Infrastructure tests for the custom-node-source-lifecycle additions to the
 * NodeDesignerStack (task 9.4):
 *
 * - The arm64_jp7 build project follows the per-arch role scoping exactly
 *   (covered per-arch by node-designer-stack.test.ts via
 *   PLUGIN_BUILD_ARCHITECTURES; asserted here by name, Requirement 7.1).
 * - The git-sync CodeBuild project and its role: secretsmanager:GetSecretValue
 *   only on the dda-portal/git-connections/* ARN pattern, S3 read limited to
 *   plugin-sources/* + plugin-git-sync/*, write limited to plugin-git-sync/*,
 *   no Plugin_Library or staging path, no VpcConfig (Requirements 2.4, 9.5).
 * - The GitSyncHandler Lambda role has NO secretsmanager:GetSecretValue
 *   (Requirement 2.4) but can create/rotate/delete the connection secrets.
 * - The dda-portal-git-sync-results EventBridge rule targets the handler.
 * - The GitConnections / GitSyncOperations tables (keys, GSI, TTL).
 * - The new API routes are registered in the nested NodeDesignerApiStack.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { NodeDesignerStack } from '../lib/node-designer-stack';

jest.setTimeout(120_000);

let stack: NodeDesignerStack;
let template: Template;

beforeAll(() => {
  const app = new cdk.App({ context: { trustedUseCaseAccountIds: '111111111111' } });
  const deps = new cdk.Stack(app, 'Deps');
  const table = (id: string) =>
    new dynamodb.Table(deps, id, {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
    });
  stack = new NodeDesignerStack(app, 'NodeDesignerLifecycle', {
    portalArtifactsBucket: new s3.Bucket(deps, 'Artifacts'),
    useCasesTable: table('UseCases'),
    userRolesTable: table('UserRoles'),
    auditLogTable: table('AuditLog'),
    settingsTable: table('Settings'),
    workflowsTable: table('Workflows'),
    workflowVersionsTable: table('WorkflowVersions'),
    testDatasetsTable: table('TestDatasets'),
    trustedUseCaseAccountIds: ['111111111111'],
    userPool: new cognito.UserPool(deps, 'Pool'),
    restApiId: 'testrestapi',
    restApiRootResourceId: 'testrootresource',
    apiStageName: 'v1',
  });
  template = Template.fromStack(stack);
});

const asArray = (value: any): any[] =>
  value === undefined ? [] : Array.isArray(value) ? value : [value];
const text = (value: any): string =>
  typeof value === 'string' ? value : JSON.stringify(value);

function projectsByName(): { [name: string]: any } {
  const byName: { [name: string]: any } = {};
  for (const resource of Object.values(template.findResources('AWS::CodeBuild::Project'))) {
    byName[(resource as any).Properties.Name] = resource;
  }
  return byName;
}

/** Every IAM policy statement attached (inline or AWS::IAM::Policy) to a role
 * whose logical id starts with `rolePrefix`. */
function statementsOfRole(rolePrefix: string): any[] {
  const roles = template.findResources('AWS::IAM::Role');
  const roleIds = Object.keys(roles).filter((id) => id.startsWith(rolePrefix));
  expect(roleIds.length).toBe(1);
  const roleId = roleIds[0];
  const statements: any[] = [];
  for (const policy of Object.values(template.findResources('AWS::IAM::Policy'))) {
    const attached = asArray((policy as any).Properties.Roles).some(
      (r: any) => text(r).includes(roleId),
    );
    if (attached) {
      statements.push(...(policy as any).Properties.PolicyDocument.Statement);
    }
  }
  for (const inline of asArray((roles as any)[roleId].Properties.Policies)) {
    statements.push(...inline.PolicyDocument.Statement);
  }
  return statements;
}

describe('arm64_jp7 build target (Requirement 7.1)', () => {
  test('the jp7 project exists on the ARM fleet with the shared entrypoint buildspec', () => {
    const project = projectsByName()['dda-plugin-build-arm64_jp7'];
    expect(project).toBeDefined();
    expect(project.Properties.Environment.Type).toBe('ARM_CONTAINER');
    expect(text(project.Properties.Environment.Image)).toContain('arm64_jp7');
    expect(project.Properties.Source.BuildSpec).toContain('dda-plugin-build');
    expect(project.Properties.VpcConfig).toBeUndefined();
  });

  test('the build-results rule and BUILD_PROJECTS_JSON include jp7', () => {
    const rule: any = Object.values(template.findResources('AWS::Events::Rule')).find(
      (r: any) => r.Properties.Name === 'dda-portal-plugin-build-results',
    );
    expect(rule.Properties.EventPattern.detail['project-name']).toContain(
      'dda-plugin-build-arm64_jp7',
    );
    const buildsHandler: any = Object.values(template.findResources('AWS::Lambda::Function')).find(
      (f: any) => f.Properties.Handler === 'plugin_builds.handler',
    );
    expect(text(buildsHandler.Properties.Environment.Variables.BUILD_PROJECTS_JSON)).toContain(
      'arm64_jp7',
    );
  });
});

describe('git-sync CodeBuild project (Requirements 2.4, 9.5)', () => {
  test('project shape: standard image, no VPC, runner buildspec, result upload', () => {
    const project = projectsByName()['dda-plugin-git-sync'];
    expect(project).toBeDefined();
    expect(project.Properties.VpcConfig).toBeUndefined();
    expect(project.Properties.Environment.ComputeType).toBe('BUILD_GENERAL1_SMALL');
    expect(project.Properties.Environment.Image).toBe('aws/codebuild/standard:7.0');
    expect(project.Properties.TimeoutInMinutes).toBe(15);
    expect(project.Properties.Source.Type).toBe('S3');
    expect(text(project.Properties.Source.Location)).toContain('plugin-git-sync/runner/');
    const buildSpec = project.Properties.Source.BuildSpec as string;
    expect(buildSpec).toContain('bash runner.sh');
    expect(buildSpec).toContain('$RESULT_KEY');
    // No token in project-level environment: only ARTIFACTS_BUCKET and
    // placeholders that StartBuild overrides.
    const envNames = project.Properties.Environment.EnvironmentVariables.map((v: any) => v.Name);
    expect(envNames).not.toContain('GIT_TOKEN');
  });

  test('runner role: secrets read scoped to the git-connections pattern only', () => {
    const statements = statementsOfRole('GitSyncRunnerRole');
    const secretStatements = statements.filter((s) =>
      asArray(s.Action).some((a: string) => a.startsWith('secretsmanager:')),
    );
    expect(secretStatements.length).toBe(1);
    expect(asArray(secretStatements[0].Action)).toEqual(['secretsmanager:GetSecretValue']);
    for (const resource of asArray(secretStatements[0].Resource)) {
      expect(text(resource)).toContain(':secret:dda-portal/git-connections/*');
    }
  });

  test('runner role: S3 access limited to plugin-sources read and plugin-git-sync read/write', () => {
    const statements = statementsOfRole('GitSyncRunnerRole');
    for (const statement of statements) {
      const s3Actions = asArray(statement.Action).filter((a: string) => a.startsWith('s3:'));
      if (s3Actions.length === 0) continue;
      const objectActions = s3Actions.filter((a: string) => !/^s3:List/.test(a));
      for (const resource of asArray(statement.Resource)) {
        const r = text(resource);
        if (objectActions.length > 0) {
          expect(r.includes('/plugin-sources/*') || r.includes('/plugin-git-sync/*')).toBe(true);
          if (objectActions.some((a: string) => a === 's3:PutObject' || a === 's3:DeleteObject')) {
            expect(r).toContain('/plugin-git-sync/*');
          }
        }
        expect(r).not.toContain('workflow-plugins/');
        expect(r).not.toContain('plugin-staging/');
      }
      const listActions = s3Actions.filter((a: string) => /^s3:List/.test(a));
      if (listActions.length > 0) {
        const prefixes = asArray(statement.Condition?.StringLike?.['s3:prefix']).sort();
        expect(prefixes).toEqual(['plugin-git-sync/*', 'plugin-sources/*']);
      }
    }
  });
});

describe('GitSyncHandler Lambda (Requirement 2.4)', () => {
  test('exists with the git-sync environment and can start the runner', () => {
    const fn: any = Object.values(template.findResources('AWS::Lambda::Function')).find(
      (f: any) => f.Properties.Handler === 'git_sync.handler',
    );
    expect(fn).toBeDefined();
    expect(fn.Properties.Timeout).toBe(120);
    const vars = fn.Properties.Environment.Variables;
    expect(vars.GIT_SECRET_PREFIX).toBe('dda-portal/git-connections');
    expect(vars.PLUGIN_GIT_SYNC_PREFIX).toBe('plugin-git-sync');
    expect(vars.GIT_CONNECTIONS_TABLE).toBeDefined();
    expect(vars.GIT_SYNC_OPERATIONS_TABLE).toBeDefined();
    expect(text(vars.GIT_SYNC_PROJECT_NAME)).toContain('GitSyncProject');

    const statements = statementsOfRole('GitSyncRunnerRole');
    // (the runner role) — the handler role is asserted next
    expect(statements.length).toBeGreaterThan(0);
  });

  test('handler role manages secrets but never reads them', () => {
    const roles = template.findResources('AWS::IAM::Role');
    const handlerRoleId = Object.keys(roles).find(
      (id) => id.startsWith('GitSyncRole') && !id.startsWith('GitSyncRunnerRole'),
    );
    expect(handlerRoleId).toBeDefined();
    const statements: any[] = [];
    for (const policy of Object.values(template.findResources('AWS::IAM::Policy'))) {
      const attached = asArray((policy as any).Properties.Roles).some(
        (r: any) => text(r).includes(handlerRoleId as string),
      );
      if (attached) statements.push(...(policy as any).Properties.PolicyDocument.Statement);
    }
    const secretActions = statements
      .flatMap((s) => asArray(s.Action))
      .filter((a: string) => a.startsWith('secretsmanager:'));
    expect(secretActions.sort()).toEqual([
      'secretsmanager:CreateSecret',
      'secretsmanager:DeleteSecret',
      'secretsmanager:DescribeSecret',
      'secretsmanager:PutSecretValue',
      'secretsmanager:TagResource',
    ]);
    expect(secretActions).not.toContain('secretsmanager:GetSecretValue');
    const secretStatement = statements.find((s) =>
      asArray(s.Action).includes('secretsmanager:CreateSecret'),
    );
    for (const resource of asArray(secretStatement.Resource)) {
      expect(text(resource)).toContain(':secret:dda-portal/git-connections/*');
    }
    const startBuild = statements.find((s) => asArray(s.Action).includes('codebuild:StartBuild'));
    expect(text(startBuild.Resource)).toContain('GitSyncProject');
    expect(text(startBuild.Resource)).not.toContain('PluginBuild');
  });

  test('results rule targets the handler for the git-sync project only', () => {
    const rule: any = Object.values(template.findResources('AWS::Events::Rule')).find(
      (r: any) => r.Properties.Name === 'dda-portal-git-sync-results',
    );
    expect(rule).toBeDefined();
    expect(rule.Properties.EventPattern.detail['project-name']).toEqual(['dda-plugin-git-sync']);
    expect(rule.Properties.EventPattern.detail['build-status'].sort()).toEqual(
      ['FAILED', 'FAULT', 'STOPPED', 'SUCCEEDED', 'TIMED_OUT'].sort(),
    );
    expect(text(rule.Properties.Targets[0].Arn)).toContain('GitSyncHandler');
    // The plugin build rule does not carry the git-sync project.
    const buildRule: any = Object.values(template.findResources('AWS::Events::Rule')).find(
      (r: any) => r.Properties.Name === 'dda-portal-plugin-build-results',
    );
    expect(buildRule.Properties.EventPattern.detail['project-name']).not.toContain(
      'dda-plugin-git-sync',
    );
  });
});

describe('git sync tables', () => {
  test('GitConnections and GitSyncOperations with GSIs and TTL', () => {
    const tables = Object.values(template.findResources('AWS::DynamoDB::Table')) as any[];
    const connections = tables.find((t) => t.Properties.TableName === 'dda-portal-git-connections');
    expect(connections).toBeDefined();
    expect(connections.Properties.KeySchema).toEqual([
      { AttributeName: 'connection_id', KeyType: 'HASH' },
    ]);
    expect(connections.Properties.GlobalSecondaryIndexes[0].IndexName).toBe(
      'usecase-connections-index',
    );
    expect(connections.Properties.PointInTimeRecoverySpecification.PointInTimeRecoveryEnabled).toBe(true);

    const operations = tables.find((t) => t.Properties.TableName === 'dda-portal-git-sync-operations');
    expect(operations).toBeDefined();
    expect(operations.Properties.KeySchema).toEqual([
      { AttributeName: 'operation_id', KeyType: 'HASH' },
    ]);
    expect(operations.Properties.TimeToLiveSpecification).toEqual({
      AttributeName: 'ttl',
      Enabled: true,
    });
    const gsi = operations.Properties.GlobalSecondaryIndexes[0];
    expect(gsi.IndexName).toBe('plugin-operations-index');
    expect(gsi.KeySchema).toEqual([
      { AttributeName: 'plugin_id', KeyType: 'HASH' },
      { AttributeName: 'started_at', KeyType: 'RANGE' },
    ]);
  });
});

describe('API routes (nested NodeDesignerApiStack)', () => {
  test('the new resources and methods are registered', () => {
    const nested = stack.node.findChild('NodeDesignerApi') as cdk.NestedStack;
    const apiTemplate = Template.fromStack(nested);
    const resources = Object.values(apiTemplate.findResources('AWS::ApiGateway::Resource')) as any[];
    const parts = resources.map((r) => r.Properties.PathPart);
    for (const part of ['new-version', 'architectures', 'git', 'push', 'pull', 'operations',
                        'git-connections', '{cid}', 'verify', 'git-sync-operations', '{opId}']) {
      expect(parts).toContain(part);
    }
    // Every Cognito-authorized method as "METHOD <path part>" of its resource.
    const resourceById: { [id: string]: string } = {};
    for (const [id, r] of Object.entries(apiTemplate.findResources('AWS::ApiGateway::Resource'))) {
      resourceById[id] = (r as any).Properties.PathPart;
    }
    const methods = Object.values(apiTemplate.findResources('AWS::ApiGateway::Method')) as any[];
    const pairs = methods
      .filter((m) => m.Properties.AuthorizationType === 'COGNITO_USER_POOLS')
      .map((m) => `${m.Properties.HttpMethod} ${resourceById[m.Properties.ResourceId?.Ref] ?? '?'}`);
    for (const expected of [
      'POST new-version', 'POST architectures',
      'PUT git', 'DELETE git', 'POST push', 'POST pull', 'GET operations',
      'GET git-connections', 'POST git-connections',
      'GET {cid}', 'PUT {cid}', 'DELETE {cid}', 'POST verify',
      'GET {opId}',
    ]) {
      expect(pairs).toContain(expected);
    }
    // 14 new Cognito-authorized methods on top of the pre-existing 30.
    expect(pairs.length).toBe(30 + 14);
  });
});
