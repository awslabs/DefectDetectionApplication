/**
 * Shared-components provisioning route and invoke (portal-authorization-and-
 * iam-scoping, design I3; task 3.1; Requirements 2.1, 5.1, 5.2).
 *
 *   - POST /shared-components/provision carries the Cognito authorizer like
 *     every sibling method;
 *   - the UseCases function reaches the SharedComponents function with a
 *     direct invoke: lambda:InvokeFunction on its role and
 *     SHARED_COMPONENTS_FUNCTION_NAME in its environment;
 *   - TRUSTED_USECASE_ACCOUNT_IDS on the UseCases function equals the
 *     deploy-time trusted list, joined;
 *   - the HTTP hop's support is gone: no role carries execute-api:Invoke and
 *     the API_GATEWAY_ID environment patch (UpdateLambdaEnv function and the
 *     LambdaEnvUpdater custom resource) is not synthesized.
 *
 * Conventions follow usecase-bucket-config-iam.test.ts: synthesize
 * StorageStack + ComputeStack once in beforeAll, read the parent template and
 * the ApiGateway nested stack's template, assert on raw CloudFormation
 * properties.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

// Fixture account ids, assembled so no identifier-shaped literal lands in the
// source.
const TRUSTED_ACCOUNTS = ['1'.repeat(12), '2'.repeat(12)];

let computeTemplate: Template;
let apiTemplate: Template;

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
    trustedUseCaseAccountIds: TRUSTED_ACCOUNTS,
  });
  computeTemplate = Template.fromStack(compute);
  apiTemplate = Template.fromStack(
    compute.node.findChild('ApiGateway') as cdk.NestedStack
  );
}, 300_000);

function functionByHandler(template: Template, handler: string): [string, any] {
  const matches = Object.entries(
    template.findResources('AWS::Lambda::Function')
  ).filter(([, fn]: [string, any]) => fn.Properties?.Handler === handler);
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

function roleLogicalId(prefix: string): string {
  const matches = Object.keys(
    computeTemplate.findResources('AWS::IAM::Role')
  ).filter((logicalId) => logicalId.startsWith(prefix));
  expect(matches).toHaveLength(1);
  return matches[0];
}

/** Every policy statement in a template (policies, managed policies, inline role policies). */
function allStatements(template: Template): Array<{ carrier: string; statement: any }> {
  const out: Array<{ carrier: string; statement: any }> = [];
  for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    for (const [id, res] of Object.entries(template.findResources(type)) as [string, any][]) {
      for (const statement of res.Properties?.PolicyDocument?.Statement ?? []) {
        out.push({ carrier: id, statement });
      }
    }
  }
  for (const [id, res] of Object.entries(template.findResources('AWS::IAM::Role')) as [string, any][]) {
    for (const policy of res.Properties?.Policies ?? []) {
      for (const statement of policy.PolicyDocument?.Statement ?? []) {
        out.push({ carrier: id, statement });
      }
    }
  }
  return out;
}

/** Statements attached to a role (by Roles reference). */
function roleStatements(roleId: string): any[] {
  return [
    ...Object.values(computeTemplate.findResources('AWS::IAM::Policy')),
    ...Object.values(computeTemplate.findResources('AWS::IAM::ManagedPolicy')),
  ]
    .filter((p: any) => p.Properties.Roles?.some((r: any) => r.Ref === roleId))
    .flatMap((p: any) => p.Properties.PolicyDocument?.Statement ?? []);
}

function asList(value: any): any[] {
  return Array.isArray(value) ? value : [value];
}

describe('POST /shared-components/provision', () => {
  test('is Cognito-authorized like every sibling method', () => {
    const resources = apiTemplate.findResources('AWS::ApiGateway::Resource');
    const sharedComponents = Object.entries(resources).find(
      ([, r]: [string, any]) => r.Properties.PathPart === 'shared-components'
    );
    expect(sharedComponents).toBeDefined();
    const provision = Object.entries(resources).filter(
      ([, r]: [string, any]) =>
        r.Properties.PathPart === 'provision' &&
        r.Properties.ParentId?.Ref === sharedComponents![0]
    );
    expect(provision).toHaveLength(1);
    const methods = Object.values(apiTemplate.findResources('AWS::ApiGateway::Method'))
      .map((m: any) => m.Properties)
      .filter((p: any) => p.ResourceId?.Ref === provision[0][0] && p.HttpMethod === 'POST');
    expect(methods).toHaveLength(1);
    expect(methods[0].AuthorizationType).toBe('COGNITO_USER_POOLS');
    expect(methods[0].AuthorizerId).toBeDefined();
  });

  test('every non-preflight /shared-components method carries the authorizer', () => {
    const resources = apiTemplate.findResources('AWS::ApiGateway::Resource');
    const ids = new Set<string>();
    const root = Object.entries(resources).find(
      ([, r]: [string, any]) => r.Properties.PathPart === 'shared-components'
    )![0];
    ids.add(root);
    for (const [id, r] of Object.entries(resources) as [string, any][]) {
      if (r.Properties.ParentId?.Ref === root) ids.add(id);
    }
    const methods = Object.values(apiTemplate.findResources('AWS::ApiGateway::Method'))
      .map((m: any) => m.Properties)
      .filter((p: any) => ids.has(p.ResourceId?.Ref) && p.HttpMethod !== 'OPTIONS');
    expect(methods.length).toBeGreaterThanOrEqual(5);
    for (const method of methods) {
      expect(method.AuthorizationType).toBe('COGNITO_USER_POOLS');
    }
  });
});

describe('UseCases -> SharedComponents direct invoke', () => {
  test('the UseCases role may invoke the SharedComponents function', () => {
    const [sharedComponentsId] = functionByHandler(computeTemplate, 'shared_components.handler');
    const invokes = roleStatements(roleLogicalId('UseCasesRole')).filter(
      (s) =>
        s.Effect === 'Allow' &&
        asList(s.Action).includes('lambda:InvokeFunction') &&
        asList(s.Resource).some(
          (r: any) => JSON.stringify(r) === JSON.stringify({ 'Fn::GetAtt': [sharedComponentsId, 'Arn'] })
        )
    );
    expect(invokes).toHaveLength(1);
  });

  test('the UseCases function names the SharedComponents function and the trusted list', () => {
    const [sharedComponentsId] = functionByHandler(computeTemplate, 'shared_components.handler');
    const [, useCases] = functionByHandler(computeTemplate, 'usecases.handler');
    const variables = useCases.Properties.Environment.Variables;
    expect(variables.SHARED_COMPONENTS_FUNCTION_NAME).toEqual({ Ref: sharedComponentsId });
    expect(variables.TRUSTED_USECASE_ACCOUNT_IDS).toBe(TRUSTED_ACCOUNTS.join(','));
    expect(variables.API_GATEWAY_ID).toBeUndefined();
    expect(variables.PORTAL_API_URL).toBeUndefined();
  });

  test('every handler of the compute stack carries the trusted list', () => {
    const functions = Object.values(computeTemplate.findResources('AWS::Lambda::Function')) as any[];
    const withPortalEnvironment = functions.filter(
      (fn) => fn.Properties?.Environment?.Variables?.PORTAL_ACCOUNT_ID !== undefined
    );
    expect(withPortalEnvironment.length).toBeGreaterThan(10);
    for (const fn of withPortalEnvironment) {
      expect(fn.Properties.Environment.Variables.TRUSTED_USECASE_ACCOUNT_IDS).toBe(
        TRUSTED_ACCOUNTS.join(',')
      );
    }
  });
});

describe('the HTTP hop support is retired', () => {
  test('no role carries execute-api:Invoke', () => {
    const offenders = [...allStatements(computeTemplate), ...allStatements(apiTemplate)]
      .filter(({ statement }) =>
        asList(statement.Action).some(
          (a: any) => typeof a === 'string' && a.toLowerCase().startsWith('execute-api:')
        )
      )
      .map(({ carrier }) => carrier);
    expect(offenders).toEqual([]);
  });

  test('the API_GATEWAY_ID environment patch is not synthesized', () => {
    const ids = Object.keys(computeTemplate.toJSON().Resources);
    expect(ids.filter((id) => id.startsWith('UpdateLambdaEnv') || id.startsWith('LambdaEnvUpdater'))).toEqual([]);
    expect(computeTemplate.findResources('AWS::CloudFormation::CustomResource')).not.toHaveProperty(
      'LambdaEnvUpdater'
    );
  });
});
