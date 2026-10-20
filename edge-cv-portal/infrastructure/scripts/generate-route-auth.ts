/**
 * Writes edge-cv-portal/infrastructure/ROUTE_AUTH.md (portal-authorization-
 * and-iam-scoping, design I3 "Route inventory"; task 3.3).
 *
 *   npm run build && npx ts-node scripts/generate-route-auth.ts
 *
 * Constructs the six stacks of API_STACK_INVENTORY directly with fixture
 * props (the way the infra tests construct NodeDesignerStack and
 * BuildFleetStack: fixture strings for the imported REST API id, root
 * resource id and shared authorizer id), synthesizes them in memory, collects every
 * AWS::ApiGateway::Method of each stack and its nested stacks, partitions out
 * CORS preflight and renders the inventory. No AWS call, no context lookup,
 * nothing written besides ROUTE_AUTH.md (the synthesized assembly goes to a
 * temporary directory that is removed afterwards).
 *
 * test/route-auth-inventory.test.ts imports buildInventoryStacks() and
 * generateRouteAuthMarkdown() and compares the committed file with the output.
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { BuildFleetStack } from '../lib/build-fleet-stack';
import { ComputeStack } from '../lib/compute-stack';
import { NodeDesignerStack } from '../lib/node-designer-stack';
import {
  API_STACK_INVENTORY,
  CollectedMethod,
  collectInventoryMethods,
  renderRouteAuthMarkdown,
} from '../lib/route-auth-inventory';
import { StorageStack } from '../lib/storage-stack';
import { SyntheticDataStack } from '../lib/synthetic-data-stack';
import { TelemetryStack } from '../lib/telemetry-stack';
import { WorkflowDebugStack } from '../lib/workflow-debug-stack';

/** Path of the committed inventory document. */
export const ROUTE_AUTH_PATH = path.join(__dirname, '..', 'ROUTE_AUTH.md');

// Fixture values only; no account, API or resource of a live deployment.
const FIXTURE_TRUSTED_ACCOUNTS = ['1'.repeat(12)];
const FIXTURE_REST_API_ID = 'fixture-rest-api-id';
const FIXTURE_ROOT_RESOURCE_ID = 'fixture-root-resource-id';
const FIXTURE_STAGE_NAME = 'v1';
/** The portal API's existing Cognito authorizer the two gated stacks attach to (bin/app.ts `sharedAuthorizerId`). */
const FIXTURE_SHARED_AUTHORIZER_ID = 'fixture0';

export interface InventoryStacks {
  readonly app: cdk.App;
  /** The constructed stacks keyed by their API_STACK_INVENTORY stack name. */
  readonly stacks: Readonly<Record<string, cdk.Stack>>;
  /** Removes the temporary assembly directory. */
  readonly dispose: () => void;
}

/** Construct the six inventory stacks (and the storage/fixture stacks they need) in one app. */
export function buildInventoryStacks(): InventoryStacks {
  const outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'route-auth-'));
  const app = new cdk.App({ outdir });
  const storage = new StorageStack(app, 'EdgeCVPortalStorageStack');
  const fixtures = new cdk.Stack(app, 'RouteAuthFixtures');
  const userPool = new cognito.UserPool(fixtures, 'Pool');
  const compute = new ComputeStack(app, 'EdgeCVPortalComputeStack', {
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
    trustedUseCaseAccountIds: FIXTURE_TRUSTED_ACCOUNTS,
  });
  const nodeDesigner = new NodeDesignerStack(app, 'EdgeCVPortalNodeDesignerStack', {
    portalArtifactsBucket: storage.portalArtifactsBucket,
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    workflowsTable: storage.workflowsTable,
    workflowVersionsTable: storage.workflowVersionsTable,
    testDatasetsTable: storage.testDatasetsTable,
    trustedUseCaseAccountIds: FIXTURE_TRUSTED_ACCOUNTS,
    userPool,
    restApiId: FIXTURE_REST_API_ID,
    restApiRootResourceId: FIXTURE_ROOT_RESOURCE_ID,
    apiStageName: FIXTURE_STAGE_NAME,
  });
  const buildFleet = new BuildFleetStack(app, 'EdgeCVPortalBuildFleetStack', {
    userRolesTable: storage.userRolesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    userPool,
    restApiId: FIXTURE_REST_API_ID,
    restApiRootResourceId: FIXTURE_ROOT_RESOURCE_ID,
    apiStageName: FIXTURE_STAGE_NAME,
  });
  const syntheticData = new SyntheticDataStack(app, 'EdgeCVPortalSyntheticDataStack', {
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    trainingJobsTable: storage.trainingJobsTable,
    trustedUseCaseAccountIds: FIXTURE_TRUSTED_ACCOUNTS,
    userPool,
    restApiId: FIXTURE_REST_API_ID,
    restApiRootResourceId: FIXTURE_ROOT_RESOURCE_ID,
    apiStageName: FIXTURE_STAGE_NAME,
  });
  const workflowDebug = new WorkflowDebugStack(app, 'EdgeCVPortalWorkflowDebugStack', {
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    devicesTable: storage.devicesTable,
    deploymentsTable: storage.deploymentsTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    workflowsTable: storage.workflowsTable,
    workflowVersionsTable: storage.workflowVersionsTable,
    portalArtifactsBucket: storage.portalArtifactsBucket,
    userPool,
    trustedUseCaseAccountIds: FIXTURE_TRUSTED_ACCOUNTS,
    restApiId: FIXTURE_REST_API_ID,
    restApiRootResourceId: FIXTURE_ROOT_RESOURCE_ID,
    apiStageName: FIXTURE_STAGE_NAME,
    sharedAuthorizerId: FIXTURE_SHARED_AUTHORIZER_ID,
  });
  const telemetry = new TelemetryStack(app, 'EdgeCVPortalTelemetryStack', {
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    devicesTable: storage.devicesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    workflowsTable: storage.workflowsTable,
    workflowVersionsTable: storage.workflowVersionsTable,
    portalArtifactsBucket: storage.portalArtifactsBucket,
    userPool,
    trustedUseCaseAccountIds: FIXTURE_TRUSTED_ACCOUNTS,
    restApiId: FIXTURE_REST_API_ID,
    restApiRootResourceId: FIXTURE_ROOT_RESOURCE_ID,
    apiStageName: FIXTURE_STAGE_NAME,
    sharedAuthorizerId: FIXTURE_SHARED_AUTHORIZER_ID,
  });
  const stacks: Record<string, cdk.Stack> = {
    EdgeCVPortalComputeStack: compute,
    EdgeCVPortalNodeDesignerStack: nodeDesigner,
    EdgeCVPortalBuildFleetStack: buildFleet,
    EdgeCVPortalSyntheticDataStack: syntheticData,
    EdgeCVPortalWorkflowDebugStack: workflowDebug,
    EdgeCVPortalTelemetryStack: telemetry,
  };
  for (const entry of API_STACK_INVENTORY) {
    if (!stacks[entry.stackName]) {
      throw new Error(`generator does not construct inventory stack ${entry.stackName}`);
    }
  }
  return {
    app,
    stacks,
    dispose: () => fs.rmSync(outdir, { recursive: true, force: true }),
  };
}

/** Collect the methods of the inventory stacks and render ROUTE_AUTH.md. */
export function generateRouteAuthMarkdown(
  built: InventoryStacks = buildInventoryStacks(),
): { markdown: string; methods: CollectedMethod[] } {
  const methods = collectInventoryMethods(built.stacks);
  return { markdown: renderRouteAuthMarkdown(methods), methods };
}

if (require.main === module) {
  const built = buildInventoryStacks();
  try {
    const { markdown, methods } = generateRouteAuthMarkdown(built);
    fs.writeFileSync(ROUTE_AUTH_PATH, markdown);
    console.log(`Wrote ${ROUTE_AUTH_PATH} (${methods.length} synthesized methods)`);
  } finally {
    built.dispose();
  }
}
