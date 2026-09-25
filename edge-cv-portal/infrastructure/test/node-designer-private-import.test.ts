/**
 * Infrastructure tests for private-repo-plugin-import (task 6.2):
 *
 * - The dda-plugin-fetch role gains secretsmanager:GetSecretValue on exactly
 *   the dda-portal/git-connections/* ARN pattern and nothing else new
 *   (Requirement 2.3); its S3 access is still plugin-sources/* only.
 * - The PluginImporterHandler role can read the GitConnections table and has
 *   NO secretsmanager:GetSecretValue (Requirement 2.1).
 * - The fetch project stays NO_SOURCE, its buildspec carries the inlined
 *   fetch.sh runner (askpass block, subdirectory guard, shallow clone,
 *   result.json) and declares the new environment variables with empty
 *   defaults - the token is never a project-level variable (Requirement 2.2).
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { NodeDesignerStack } from '../lib/node-designer-stack';

jest.setTimeout(120_000);

let template: Template;

beforeAll(() => {
  const app = new cdk.App({ context: { trustedUseCaseAccountIds: '111111111111' } });
  const deps = new cdk.Stack(app, 'Deps');
  const table = (id: string) =>
    new dynamodb.Table(deps, id, {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
    });
  const stack = new NodeDesignerStack(app, 'NodeDesignerPrivateImport', {
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

function projectNamed(name: string): any {
  const project = Object.values(template.findResources('AWS::CodeBuild::Project')).find(
    (r: any) => r.Properties.Name === name,
  );
  expect(project).toBeDefined();
  return project;
}

function statementsOfRole(rolePrefix: string): any[] {
  const roles = template.findResources('AWS::IAM::Role');
  const roleIds = Object.keys(roles).filter((id) => id.startsWith(rolePrefix));
  expect(roleIds.length).toBe(1);
  const roleId = roleIds[0];
  const statements: any[] = [];
  // Inline DefaultPolicy statements plus the OverflowPolicy managed
  // policies CDK spills them into once a role's inline policy grows
  // past the size limit (search both carrier types).
  for (const policy of [
    ...Object.values(template.findResources('AWS::IAM::Policy')),
    ...Object.values(template.findResources('AWS::IAM::ManagedPolicy')),
  ]) {
    const attached = asArray((policy as any).Properties.Roles).some((r: any) =>
      text(r).includes(roleId),
    );
    if (attached) statements.push(...(policy as any).Properties.PolicyDocument.Statement);
  }
  for (const inline of asArray((roles as any)[roleId].Properties.Policies)) {
    statements.push(...inline.PolicyDocument.Statement);
  }
  return statements;
}

const actionsOf = (statements: any[]): string[] =>
  statements.flatMap((s) => asArray(s.Action));

describe('fetch role (Requirement 2.3)', () => {
  test('may read exactly the Git connection secrets', () => {
    const statements = statementsOfRole('PluginFetchRole');
    const secretStatements = statements.filter((s) =>
      asArray(s.Action).some((a: string) => a.startsWith('secretsmanager:')),
    );
    expect(secretStatements).toHaveLength(1);
    expect(asArray(secretStatements[0].Action)).toEqual(['secretsmanager:GetSecretValue']);
    const resources = asArray(secretStatements[0].Resource).map(text);
    expect(resources).toHaveLength(1);
    expect(resources[0]).toContain(':secret:dda-portal/git-connections/*');
  });

  test('S3 access is unchanged: plugin-sources/* only', () => {
    const statements = statementsOfRole('PluginFetchRole');
    const s3Statements = statements.filter((s) =>
      asArray(s.Action).some((a: string) => a.startsWith('s3:')),
    );
    for (const statement of s3Statements) {
      const resources = asArray(statement.Resource).map(text);
      const prefixes = asArray(statement.Condition?.StringLike?.['s3:prefix']).map(text);
      const scoped = [...resources, ...prefixes].join(' ');
      // Either an object statement on plugin-sources/* or the bucket-level
      // ListBucket conditioned on plugin-sources/*.
      expect(scoped).toContain('plugin-sources/*');
      expect(scoped).not.toContain('plugin-git-sync');
      expect(scoped).not.toContain('workflow-plugins');
    }
    expect(actionsOf(statements)).not.toContain('logs:GetLogEvents');
  });
});

describe('importer Lambda role (Requirement 2.1)', () => {
  test('reads the GitConnections table but can never read a secret', () => {
    const statements = statementsOfRole('PluginImporterRole');
    const actions = actionsOf(statements);
    expect(actions.some((a) => a.startsWith('secretsmanager:'))).toBe(false);
    const tableReads = statements.filter(
      (s) =>
        asArray(s.Action).includes('dynamodb:GetItem') &&
        asArray(s.Resource).some((r: any) => text(r).includes('GitConnectionsTable')),
    );
    expect(tableReads.length).toBeGreaterThanOrEqual(1);
    for (const statement of tableReads) {
      // Read-only on the connections table: no writes granted to the importer.
      expect(asArray(statement.Action)).not.toContain('dynamodb:PutItem');
      expect(asArray(statement.Action)).not.toContain('dynamodb:UpdateItem');
      expect(asArray(statement.Action)).not.toContain('dynamodb:DeleteItem');
    }
  });
});

describe('dda-plugin-fetch project (Requirements 1.5, 1.6, 1.8, 2.2, 6.2)', () => {
  test('stays NO_SOURCE with the inlined runner as its buildspec', () => {
    const project = projectNamed('dda-plugin-fetch');
    expect(project.Properties.Source.Type).toBe('NO_SOURCE');
    expect(project.Properties.VpcConfig).toBeUndefined();
    // CDK serialises the buildspec object as JSON.
    const spec = JSON.parse(project.Properties.Source.BuildSpec as string);
    expect(spec.env.shell).toBe('bash');
    const commands: string[] = spec.phases.build.commands;
    // The pre-flight guard the original buildspec had, still first; the
    // runner script is the only other command.
    expect(commands[0]).toBe('test -n "$REPO_URL" && test -n "$DEST_PREFIX"');
    expect(commands).toHaveLength(2);
    const buildSpec = commands[1];
    expect(buildSpec.startsWith('#!/usr/bin/env bash')).toBe(true);
    // Runner content: askpass credential, prompt-free git, branch/shallow
    // clone, subdirectory guard, and the result document.
    expect(buildSpec).toContain('GIT_ASKPASS');
    expect(buildSpec).toContain('GIT_TERMINAL_PROMPT=0');
    expect(buildSpec).toContain('--depth 1');
    expect(buildSpec).toContain('--branch "$REPO_BRANCH"');
    expect(buildSpec).toContain('PATH_NOT_FOUND');
    expect(buildSpec).toContain('INVALID_SUBDIR');
    expect(buildSpec).toContain('result.json');
    expect(buildSpec).toContain('aws s3 sync "$SRC_DIR/"');
    // The token is only ever referenced as an environment variable.
    expect(buildSpec).not.toMatch(/ghp_|glpat-/);
  });

  test('declares the new variables with empty defaults and no token', () => {
    const project = projectNamed('dda-plugin-fetch');
    const vars: { [name: string]: any } = {};
    for (const v of project.Properties.Environment.EnvironmentVariables) {
      vars[v.Name] = v;
    }
    for (const name of ['REPO_URL', 'REVISION', 'DEST_PREFIX', 'REPO_BRANCH', 'REPO_SUBDIR', 'SHALLOW', 'RESULT_KEY']) {
      expect(vars[name]).toBeDefined();
      expect(vars[name].Value).toBe('');
      expect(vars[name].Type ?? 'PLAINTEXT').toBe('PLAINTEXT');
    }
    expect(vars.GIT_USERNAME.Value).toBe('x-access-token');
    // GIT_TOKEN arrives only as a StartBuild override, never project-level.
    expect(vars.GIT_TOKEN).toBeUndefined();
    expect(
      project.Properties.Environment.EnvironmentVariables.some((v: any) => v.Type === 'SECRETS_MANAGER'),
    ).toBe(false);
  });
});
