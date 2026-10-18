/**
 * Regression guard for the API Gateway authorizer quota.
 *
 * Every portal stack that registers routes does so on the one REST API the
 * ComputeStack's nested ApiGatewayStack owns, and until 2026-10-18 each of
 * them created its own Cognito authorizer there. API Gateway allows 10
 * authorizers per REST API, a quota that only AWS Support raises, and the
 * portal API had reached it; the first deploy of EdgeCVPortalWorkflowDebugStack
 * rolled back at its AWS::ApiGateway::Authorizer with "Maximum number of
 * Authorizers for this API has been reached". No stack-level test can see
 * that, so this one synthesizes every stack bin/app.ts instantiates in one
 * app and counts the AWS::ApiGateway::Authorizer resources across all of
 * their templates, nested stacks included: the total must stay within the
 * quota. A stack that creates no authorizer of its own (the telemetry and
 * debug stacks attach to the existing one through `-c sharedAuthorizerId`,
 * which bin/app.ts hands them as the `sharedAuthorizerId` prop) adds nothing
 * here; a new stack that creates one fails this test before it fails a
 * deploy.
 *
 * The stack set is a mirror of bin/app.ts (the app is a script that reads
 * SSM and calls app.synth() at import, so it cannot be imported). The mirror
 * is checked against the bin/app.ts source: every `new <Stack>(app, ...)` it
 * contains must be constructed here, so a stack added to the app without a
 * line in this file fails the test instead of escaping the count. This is
 * the app WITH the context value, so all ten stacks are present; the real
 * app without it (the two stacks skipped, every other stack synthesizing)
 * is portal-app-without-shared-authorizer.test.ts.
 *
 * Conventions follow portal-registry-enforcement-infra.test.ts and
 * security-scan-remediation-infra.test.ts: one synth in beforeAll with a
 * generous timeout (the ComputeStack stages every Lambda and layer asset),
 * nested stacks found with cdk.NestedStack.isNestedStack, assertions on raw
 * CloudFormation resources.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AuthStack } from '../lib/auth-stack';
import { BuildFleetStack } from '../lib/build-fleet-stack';
import { ComputeStack } from '../lib/compute-stack';
import { FrontendStack } from '../lib/frontend-stack';
import { NodeDesignerStack } from '../lib/node-designer-stack';
import { StorageStack } from '../lib/storage-stack';
import { SyntheticDataStack } from '../lib/synthetic-data-stack';
import { TelemetryStack } from '../lib/telemetry-stack';
import { TestRunnerStack } from '../lib/test-runner-stack';
import { WorkflowDebugStack } from '../lib/workflow-debug-stack';

/** The API Gateway default quota for authorizers per REST API (Support-only increase). */
const AUTHORIZERS_PER_REST_API = 10;
const TRUSTED_USECASE_ACCOUNT_IDS = ['222222222222', '333333333333'];
/** The existing portal API authorizer the deploy passes as `-c sharedAuthorizerId` and bin/app.ts hands the two stacks (a fixture id). */
const SHARED_AUTHORIZER_ID = 'fixture0';
const APP_SOURCE = path.join(__dirname, '..', 'bin', 'app.ts');

/** Stack classes bin/app.ts instantiates, in its order; the test constructs exactly these. */
const MIRRORED_STACKS = [
  'AuthStack',
  'StorageStack',
  'TestRunnerStack',
  'ComputeStack',
  'NodeDesignerStack',
  'BuildFleetStack',
  'SyntheticDataStack',
  'WorkflowDebugStack',
  'TelemetryStack',
  'FrontendStack',
];

/** Authorizer count per template, keyed by the stack's construct path. */
let authorizersByStack: Record<string, number>;

beforeAll(() => {
  const app = new cdk.App({
    context: {
      trustedUseCaseAccountIds: TRUSTED_USECASE_ACCOUNT_IDS.join(','),
    },
  });

  // The wiring below mirrors bin/app.ts (props and order); the stack ids are
  // the deployed names so the breakdown reads like the account.
  const authStack = new AuthStack(app, 'EdgeCVPortalAuthStack');
  const storageStack = new StorageStack(app, 'EdgeCVPortalStorageStack');
  const testRunnerStack = new TestRunnerStack(app, 'EdgeCVPortalTestRunnerStack', {
    testRunsTable: storageStack.testRunsTable,
    portalArtifactsBucket: storageStack.portalArtifactsBucket,
  });
  const computeStack = new ComputeStack(app, 'EdgeCVPortalComputeStack', {
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
    trustedUseCaseAccountIds: TRUSTED_USECASE_ACCOUNT_IDS,
    dataBucketAllowlist: [],
  });
  const api = {
    restApiId: computeStack.api.restApiId,
    restApiRootResourceId: computeStack.api.restApiRootResourceId,
    apiStageName: 'v1',
  };
  new NodeDesignerStack(app, 'EdgeCVPortalNodeDesignerStack', {
    portalArtifactsBucket: storageStack.portalArtifactsBucket,
    useCasesTable: storageStack.useCasesTable,
    userRolesTable: storageStack.userRolesTable,
    auditLogTable: storageStack.auditLogTable,
    settingsTable: storageStack.settingsTable,
    workflowsTable: storageStack.workflowsTable,
    workflowVersionsTable: storageStack.workflowVersionsTable,
    testDatasetsTable: storageStack.testDatasetsTable,
    trustedUseCaseAccountIds: TRUSTED_USECASE_ACCOUNT_IDS,
    userPool: authStack.userPool,
    ...api,
  });
  new BuildFleetStack(app, 'EdgeCVPortalBuildFleetStack', {
    userRolesTable: storageStack.userRolesTable,
    auditLogTable: storageStack.auditLogTable,
    settingsTable: storageStack.settingsTable,
    userPool: authStack.userPool,
    ...api,
  });
  new SyntheticDataStack(app, 'EdgeCVPortalSyntheticDataStack', {
    useCasesTable: storageStack.useCasesTable,
    userRolesTable: storageStack.userRolesTable,
    auditLogTable: storageStack.auditLogTable,
    settingsTable: storageStack.settingsTable,
    trainingJobsTable: storageStack.trainingJobsTable,
    trustedUseCaseAccountIds: TRUSTED_USECASE_ACCOUNT_IDS,
    dataBucketAllowlist: [],
    userPool: authStack.userPool,
    ...api,
  });
  new WorkflowDebugStack(app, 'EdgeCVPortalWorkflowDebugStack', {
    useCasesTable: storageStack.useCasesTable,
    userRolesTable: storageStack.userRolesTable,
    devicesTable: storageStack.devicesTable,
    deploymentsTable: storageStack.deploymentsTable,
    auditLogTable: storageStack.auditLogTable,
    settingsTable: storageStack.settingsTable,
    workflowsTable: storageStack.workflowsTable,
    workflowVersionsTable: storageStack.workflowVersionsTable,
    portalArtifactsBucket: storageStack.portalArtifactsBucket,
    userPool: authStack.userPool,
    trustedUseCaseAccountIds: TRUSTED_USECASE_ACCOUNT_IDS,
    dataBucketAllowlist: [],
    ...api,
    sharedAuthorizerId: SHARED_AUTHORIZER_ID,
  });
  new TelemetryStack(app, 'EdgeCVPortalTelemetryStack', {
    useCasesTable: storageStack.useCasesTable,
    userRolesTable: storageStack.userRolesTable,
    devicesTable: storageStack.devicesTable,
    auditLogTable: storageStack.auditLogTable,
    settingsTable: storageStack.settingsTable,
    workflowsTable: storageStack.workflowsTable,
    workflowVersionsTable: storageStack.workflowVersionsTable,
    portalArtifactsBucket: storageStack.portalArtifactsBucket,
    userPool: authStack.userPool,
    trustedUseCaseAccountIds: TRUSTED_USECASE_ACCOUNT_IDS,
    dataBucketAllowlist: [],
    ...api,
    sharedAuthorizerId: SHARED_AUTHORIZER_ID,
  });
  new FrontendStack(app, 'EdgeCVPortalFrontendStack', {
    apiUrl: computeStack.apiUrl,
    userPoolId: authStack.userPool.userPoolId,
    userPoolClientId: authStack.userPoolClient.userPoolClientId,
  });

  // Every stack in the app, nested ones included: a nested stack's template
  // is its own artifact, so its authorizers are not in the parent's.
  authorizersByStack = {};
  for (const construct of app.node.findAll()) {
    if (!cdk.Stack.isStack(construct)) continue;
    const template = Template.fromStack(construct);
    authorizersByStack[construct.node.path] = Object.keys(
      template.findResources('AWS::ApiGateway::Authorizer'),
    ).length;
  }
}, 900_000);

describe('authorizers per REST API across the whole app', () => {
  test('the mirror constructs every stack bin/app.ts instantiates', () => {
    const source = fs.readFileSync(APP_SOURCE, 'utf8');
    const inApp = Array.from(source.matchAll(/new\s+(\w+Stack)\s*\(\s*app\s*,/g), (m) => m[1]);
    expect(inApp.length).toBeGreaterThan(0);
    expect([...inApp].sort()).toEqual([...MIRRORED_STACKS].sort());
    // Each mirrored class produced a synthesized stack.
    const synthesized = Object.keys(authorizersByStack);
    for (const stackClass of MIRRORED_STACKS) {
      const deployedName = `EdgeCVPortal${stackClass}`;
      expect(synthesized).toContain(deployedName);
    }
  });

  test(`the app creates at most ${AUTHORIZERS_PER_REST_API} AWS::ApiGateway::Authorizer resources in total`, () => {
    const total = Object.values(authorizersByStack).reduce((sum, count) => sum + count, 0);
    // The count is not vacuous: the ApiGatewayStack's own authorizer is here.
    expect(total).toBeGreaterThan(0);
    if (total > AUTHORIZERS_PER_REST_API) {
      const breakdown = Object.entries(authorizersByStack)
        .filter(([, count]) => count > 0)
        .map(([stackPath, count]) => `  ${stackPath}: ${count}`)
        .join('\n');
      throw new Error(
        `${total} AWS::ApiGateway::Authorizer resources across the app exceed the quota of ` +
          `${AUTHORIZERS_PER_REST_API} per REST API (Support-only increase); attach new routes to an ` +
          `existing authorizer instead (lib/context-helpers.ts sharedAuthorizerId).\n${breakdown}`,
      );
    }
    expect(total).toBeLessThanOrEqual(AUTHORIZERS_PER_REST_API);
  });

  test('the telemetry and debug stacks create no authorizer of their own', () => {
    expect(authorizersByStack['EdgeCVPortalWorkflowDebugStack']).toBe(0);
    expect(authorizersByStack['EdgeCVPortalTelemetryStack']).toBe(0);
  });
});
