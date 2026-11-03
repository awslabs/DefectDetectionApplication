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
 * their templates, nested stacks included, grouped by the REST API each one
 * belongs to: every API's total must stay within the quota. An authorizer
 * whose RestApiId is a Ref to a RestApi in its own template belongs to that
 * API (the ApiGatewayStack's portal API, or a stack-owned API such as the
 * assistant's Regional one, portal-ai-assistant design D2); one that imports
 * its RestApiId attaches to the portal API, the only API the stacks share.
 * A stack that creates no authorizer of its own (the telemetry and debug
 * stacks attach to the existing one through `-c sharedAuthorizerId`, which
 * bin/app.ts hands them as the `sharedAuthorizerId` prop) adds nothing here;
 * a new stack that creates one on the portal API fails this test before it
 * fails a deploy.
 *
 * The stack set is a mirror of bin/app.ts (the app is a script that reads
 * SSM and calls app.synth() at import, so it cannot be imported). The mirror
 * is checked against the bin/app.ts source: every `new <Stack>(app, ...)` it
 * contains must be constructed here, so a stack added to the app without a
 * line in this file fails the test instead of escaping the count. This is
 * the app WITH the context value, so all twelve stacks are present (the MCP
 * stack's HTTP API has no authorizer at all, design §1); the real
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
import { ASSISTANT_API_NAME, AssistantStack } from '../lib/assistant-stack';
import { McpStack, mcpBackendHandlers } from '../lib/mcp-stack';
import { SyntheticDataStack } from '../lib/synthetic-data-stack';
import { TelemetryStack } from '../lib/telemetry-stack';
import { TestRunnerStack } from '../lib/test-runner-stack';
import { WorkflowDebugStack } from '../lib/workflow-debug-stack';

/** The API Gateway default quota for authorizers per REST API (Support-only increase). */
const AUTHORIZERS_PER_REST_API = 10;
/** The portal API's RestApi Name (lib/api-gateway-stack.ts); imported RestApiIds resolve to it. */
const PORTAL_API_NAME = 'Edge CV Portal API';
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
  'McpStack',
  'AssistantStack',
  'WorkflowDebugStack',
  'TelemetryStack',
  'FrontendStack',
];

/** Authorizer count per template, keyed by the stack's construct path. */
let authorizersByStack: Record<string, number>;
/** Authorizer count per REST API, keyed by the API's Name; nested by the owning stack's construct path. */
let authorizersByApi: Record<string, Record<string, number>>;

/**
 * The REST API an authorizer belongs to. A Ref to a RestApi in the same
 * template names that API; anything else (Fn::ImportValue / Fn::GetAtt of
 * the ComputeStack's export) is the portal API, the one API the stacks share.
 */
function restApiNameOf(template: Template, restApiId: unknown): string {
  if (restApiId && typeof restApiId === 'object' && 'Ref' in restApiId) {
    const local = template.findResources('AWS::ApiGateway::RestApi')[(restApiId as { Ref: string }).Ref];
    if (local) return String(local.Properties?.Name ?? (restApiId as { Ref: string }).Ref);
  }
  return PORTAL_API_NAME;
}

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
  const nodeDesignerStack = new NodeDesignerStack(app, 'EdgeCVPortalNodeDesignerStack', {
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
  const buildFleetStack = new BuildFleetStack(app, 'EdgeCVPortalBuildFleetStack', {
    userRolesTable: storageStack.userRolesTable,
    auditLogTable: storageStack.auditLogTable,
    settingsTable: storageStack.settingsTable,
    userPool: authStack.userPool,
    ...api,
  });
  const syntheticDataStack = new SyntheticDataStack(app, 'EdgeCVPortalSyntheticDataStack', {
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
  new McpStack(app, 'EdgeCVPortalMcpStack', {
    userPool: authStack.userPool,
    userPoolDomain: authStack.userPoolDomain!,
    userRolesTable: storageStack.userRolesTable,
    auditLogTable: storageStack.auditLogTable,
    usecasesTable: storageStack.useCasesTable,
    backendHandlers: mcpBackendHandlers({
      computeHandlers: computeStack.mcpBackendHandlers,
      nodeDesigner: nodeDesignerStack,
      buildFleet: buildFleetStack,
      syntheticData: syntheticDataStack,
    }),
    mcpMutatingToolsEnabled: 'false',
    mcpRedirectUris: [],
    mcpAllowedOrigins: [],
    mcpBuildSha: 'fixture',
  });
  // The Portal assistant's own Regional REST API and authorizer
  // (portal-ai-assistant design §10): counted per API below.
  new AssistantStack(app, 'EdgeCVPortalAssistantStack', {
    userPool: authStack.userPool,
    userRolesTable: storageStack.userRolesTable,
    auditLogTable: storageStack.auditLogTable,
    usecasesTable: storageStack.useCasesTable,
    settingsTable: storageStack.settingsTable,
    backendHandlers: mcpBackendHandlers({
      computeHandlers: computeStack.mcpBackendHandlers,
      nodeDesigner: nodeDesignerStack,
      buildFleet: buildFleetStack,
      syntheticData: syntheticDataStack,
    }),
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
  authorizersByApi = {};
  for (const construct of app.node.findAll()) {
    if (!cdk.Stack.isStack(construct)) continue;
    const template = Template.fromStack(construct);
    const authorizers = Object.values(template.findResources('AWS::ApiGateway::Authorizer'));
    authorizersByStack[construct.node.path] = authorizers.length;
    for (const authorizer of authorizers) {
      const apiName = restApiNameOf(template, authorizer.Properties?.RestApiId);
      const perStack = (authorizersByApi[apiName] ??= {});
      perStack[construct.node.path] = (perStack[construct.node.path] ?? 0) + 1;
    }
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

  test(`no REST API carries more than ${AUTHORIZERS_PER_REST_API} AWS::ApiGateway::Authorizer resources`, () => {
    // The count is not vacuous: the ApiGatewayStack's own authorizer is here.
    const portalTotal = Object.values(authorizersByApi[PORTAL_API_NAME] ?? {}).reduce((sum, c) => sum + c, 0);
    expect(portalTotal).toBeGreaterThan(0);
    for (const [apiName, perStack] of Object.entries(authorizersByApi)) {
      const total = Object.values(perStack).reduce((sum, count) => sum + count, 0);
      if (total > AUTHORIZERS_PER_REST_API) {
        const breakdown = Object.entries(perStack)
          .map(([stackPath, count]) => `  ${stackPath}: ${count}`)
          .join('\n');
        throw new Error(
          `${total} AWS::ApiGateway::Authorizer resources on the "${apiName}" REST API exceed the quota of ` +
            `${AUTHORIZERS_PER_REST_API} per REST API (Support-only increase); attach new routes to an ` +
            `existing authorizer instead (lib/context-helpers.ts sharedAuthorizerId).\n${breakdown}`,
        );
      }
      expect(total).toBeLessThanOrEqual(AUTHORIZERS_PER_REST_API);
    }
  });

  test('the telemetry and debug stacks create no authorizer of their own', () => {
    expect(authorizersByStack['EdgeCVPortalWorkflowDebugStack']).toBe(0);
    expect(authorizersByStack['EdgeCVPortalTelemetryStack']).toBe(0);
  });

  test("the assistant stack's one authorizer is on its own REST API, not the portal API", () => {
    expect(authorizersByStack['EdgeCVPortalAssistantStack']).toBe(1);
    expect(authorizersByApi[ASSISTANT_API_NAME]).toEqual({ EdgeCVPortalAssistantStack: 1 });
    expect(authorizersByApi[PORTAL_API_NAME]?.['EdgeCVPortalAssistantStack']).toBeUndefined();
  });
});
