/**
 * JWT authorizer audience wiring (security-scan-remediation-high, R6).
 *
 * ComputeStack passes its optional userPoolClientId prop to the custom JWT
 * authorizer as ALLOWED_AUDIENCES. Without the prop the value is '' and the
 * authorizer denies every request, so a missing value fails closed.
 *
 * Requirements covered: 6.3 (the audience is checked against the app clients
 * the Portal allows), 17.2.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack, ComputeStackProps } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

/** The synthesized environment of the jwt_authorizer.handler function. */
function authorizerEnvironment(userPoolClientId?: string): Record<string, unknown> {
  const app = new cdk.App();
  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');
  const props: ComputeStackProps = {
    userPool: new cognito.UserPool(deps, 'Pool'),
    ...(userPoolClientId === undefined ? {} : { userPoolClientId }),
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
    trustedUseCaseAccountIds: ['111111111111'],
  };
  const template = Template.fromStack(new ComputeStack(app, 'Compute', props));
  const functions = Object.values(
    template.findResources('AWS::Lambda::Function', {
      Properties: { Handler: 'jwt_authorizer.handler' },
    })
  ) as any[];
  expect(functions).toHaveLength(1);
  return functions[0].Properties.Environment.Variables;
}

describe('JWT authorizer ALLOWED_AUDIENCES wiring (Requirement 6.3)', () => {
  test('takes the app client id from the userPoolClientId prop', () => {
    const environment = authorizerEnvironment('test-client-id');
    expect(environment.ALLOWED_AUDIENCES).toBe('test-client-id');
    expect(environment.ISSUER_WHITELIST).toBe('');
  }, 300_000);

  test('is empty without the prop, so the authorizer denies every request', () => {
    expect(authorizerEnvironment().ALLOWED_AUDIENCES).toBe('');
  }, 300_000);
});
