/**
 * Infrastructure assertions for camera-override-binding-500.
 *
 * A workflow deployment that binds a camera by manual override is validated
 * in deployments.py (`_camera_node_descriptor` / `_override_errors`), which
 * imports `workflow_core` lazily, inside those functions. The
 * DeploymentsHandler attached only the SharedLayer, so in Lambda the import
 * raised ModuleNotFoundError and every override deployment answered 500. The
 * Python unit tests never saw it: their conftest puts the workflow_core layer
 * on sys.path for every module.
 *
 * Property 1 (bug condition): a function that can run a `workflow_core`
 * import has the package at run time.
 * - DeploymentsHandler attaches the SharedLayer and the stack's single
 *   WorkflowCoreLayer, the same Ref and order as WorkflowPackagingHandler,
 *   which runs the same code asset.
 * - General guard: every ComputeStack function whose handler module in
 *   backend/functions contains a `workflow_core` import, at module level or
 *   inside a function, attaches the WorkflowCoreLayer. The check reads the
 *   handler module itself, not what it imports.
 *
 * Conventions follow llm-model-token-and-image-sizing-infra.test.ts:
 * synthesize once in beforeAll with a generous timeout, then assert on raw
 * CloudFormation properties.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

const FUNCTIONS_DIR = path.join(__dirname, '../../backend/functions');

/**
 * A `workflow_core` import statement anywhere in a module: at module level,
 * or indented inside a function, as deployments.py does it. Comment lines
 * never match.
 */
const WORKFLOW_CORE_IMPORT =
  /^[ \t]*(?:from[ \t]+workflow_core(?:\.[\w.]+)?[ \t]+import\b|import[ \t]+workflow_core\b)/m;

// Synthesized once: the ComputeStack stages Lambda/layer assets and runs the
// quick-setup bundle packaging script at synth time, which is expensive.
let computeTemplate: Template;

beforeAll(() => {
  const app = new cdk.App();
  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');
  const userPool = new cognito.UserPool(deps, 'Pool');

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
    trustedUseCaseAccountIds: ['111111111111'],
  });

  computeTemplate = Template.fromStack(compute);
}, 300_000);

/** The single Lambda function in the compute template with `handler`. */
function lambdaByHandler(handler: string): [string, any] {
  const matches = Object.entries(
    computeTemplate.findResources('AWS::Lambda::Function')
  ).filter(([, resource]) => (resource as any).Properties.Handler === handler);
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

/** Logical ids of the LayerVersions attached to `fn`, in template order. */
function layerRefs(fn: any): string[] {
  return (fn.Properties.Layers ?? []).map((layer: any) => layer.Ref);
}

/** Logical id of the stack's single WorkflowCoreLayer LayerVersion. */
function workflowCoreLayerId(): string {
  const matches = Object.keys(
    computeTemplate.findResources('AWS::Lambda::LayerVersion')
  ).filter((logicalId) => logicalId.startsWith('WorkflowCoreLayer'));
  expect(matches).toHaveLength(1);
  return matches[0];
}

/**
 * Source of the handler's module in backend/functions, or undefined when the
 * handler does not name a module there (inline code, for example).
 * 'deployments.handler' reads backend/functions/deployments.py.
 */
function handlerModuleSource(handler: string): string | undefined {
  const lastDot = handler.lastIndexOf('.');
  if (lastDot <= 0) return undefined;
  const modulePath = handler.slice(0, lastDot).split('.').join(path.sep);
  const file = path.join(FUNCTIONS_DIR, `${modulePath}.py`);
  return fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : undefined;
}

describe('workflow_core at run time (camera-override-binding-500, Property 1)', () => {
  test('DeploymentsHandler attaches the SharedLayer and the same WorkflowCoreLayer as WorkflowPackagingHandler', () => {
    const [, packaging] = lambdaByHandler('workflow_packaging.handler');
    const packagingLayers = layerRefs(packaging);
    expect(packagingLayers).toHaveLength(2);
    expect(packagingLayers[0]).toMatch(/^SharedLayer/);
    expect(packagingLayers[1]).toBe(workflowCoreLayerId());

    // The identical Refs in the identical order: the override check runs
    // against the same /opt contents the packaging function already runs.
    const [, deployments] = lambdaByHandler('deployments.handler');
    expect(layerRefs(deployments)).toEqual(packagingLayers);
  });

  test('every function whose handler module imports workflow_core attaches the WorkflowCoreLayer', () => {
    const workflowCoreRef = workflowCoreLayerId();
    const importers: string[] = [];
    const missing: string[] = [];

    for (const [logicalId, resource] of Object.entries(
      computeTemplate.findResources('AWS::Lambda::Function')
    )) {
      const fn = resource as any;
      const handler = fn.Properties.Handler;
      // Container-image functions carry no Handler.
      if (typeof handler !== 'string') continue;
      const source = handlerModuleSource(handler);
      if (source === undefined || !WORKFLOW_CORE_IMPORT.test(source)) continue;
      importers.push(handler);
      if (!layerRefs(fn).includes(workflowCoreRef)) {
        missing.push(`${handler} (${logicalId})`);
      }
    }

    // Not vacuous: the scan finds the Workflow Manager handlers and the
    // deployments handler's lazy imports.
    expect(importers).toEqual(
      expect.arrayContaining([
        'workflows.handler',
        'workflow_packaging.handler',
        'deployments.handler',
      ])
    );
    expect(missing).toEqual([]);
  });
});
