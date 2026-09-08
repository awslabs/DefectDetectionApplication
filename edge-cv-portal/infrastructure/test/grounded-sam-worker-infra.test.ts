/**
 * Static infrastructure assertions for the grounded-sam-autolabel worker
 * gating (originally grounded-sam-autolabel task 5.2; default synth
 * rebaselined by portal-deploy-flag-hardening task 4.1, whose Requirement
 * 6.1 declares this amendment).
 *
 * The `deployGroundedSamWorker` context flag now defaults ON
 * (portal-deploy-flag-hardening Requirement 1): four flag-less deploys each
 * deleted the live DdaGroundedSamWorker under the old default-OFF gate, so
 * the default (no-context) synth asserted here is the WITH-worker case —
 * the exact shape of a routine flag-less portal deployment. Only an
 * explicit false context omits the worker (that shape is pinned by
 * gsam-preview-infra.test.ts's flag-OFF suite).
 *
 * Requirements covered:
 * - portal-deploy-flag-hardening 1.1/1.6/1.7: the default synth defines the
 *   Grounded_SAM_Worker with its shipped configuration (image package,
 *   x86_64, 10240 MB, 300 s, no environment block) and the complete
 *   Worker_Wiring: GROUNDED_SAM_WORKER_FUNCTION_NAME plus an invoke grant
 *   on BOTH DdaAutolabelWorker and DdaLabelingHandler.
 * - portal-deploy-flag-hardening 5.2: the sibling DdaSamWorker stays behind
 *   its own `deploySamWorker` flag (still default OFF and absent here, so
 *   still not synthesized) and DdaAutolabelWorker's environment still
 *   carries no SAM_WORKER_FUNCTION_NAME entry.
 * - grounded-sam-autolabel 5.5: DdaAutolabelWorker's own configuration —
 *   handler, runtime, timeout, memory, layers, and its static environment
 *   values — is exactly the pre-feature configuration; its exact
 *   environment key set gains exactly one key,
 *   GROUNDED_SAM_WORKER_FUNCTION_NAME.
 *
 * On flag-on synthesis and Docker: this header previously claimed that a
 * flag-on synth performs a real multi-gigabyte Docker build at synth time.
 * That claim was wrong, as established by gsam-preview-infra.test.ts
 * (which synthesizes flag-ON under jest): `DockerImageCode.fromImageAsset`
 * only runs AssetStaging at synth time — it copies and fingerprints the
 * small backend/grounded-sam-worker source directory (~136 KB); the
 * `docker build` itself is performed by cdk-assets at deploy time, never
 * during `Template.fromStack`. The worker joining the default synth
 * therefore adds zero Docker activity to this suite.
 *
 * Conventions follow llm-model-token-and-image-sizing-infra.test.ts /
 * workflow-manager-gaps-infra.test.ts: synthesize once in beforeAll with a
 * generous timeout (Lambda/layer asset staging is expensive), locate
 * resources via template.findResources, assert on raw CloudFormation
 * properties.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';

// Synthesized once: the ComputeStack stages Lambda/layer assets and runs the
// quick-setup bundle packaging script at synth time, which is expensive.
let computeTemplate: Template;

beforeAll(() => {
  // Default synth: NO context at all — in particular neither
  // deployGroundedSamWorker nor deploySamWorker — the exact shape of a
  // routine portal deployment. Under portal-deploy-flag-hardening this is
  // the WITH-worker shape: the flag defaults ON and only an explicit false
  // omits the worker (Req 1.1).
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
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
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

/** The single DdaGroundedSamWorker function resource in the template. */
function groundedSamWorkerFunction(): [string, any] {
  const matches = Object.entries(
    computeTemplate.findResources('AWS::Lambda::Function')
  ).filter(([logicalId]) => logicalId.startsWith('DdaGroundedSamWorker'));
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

/**
 * All IAM policy statements attached to `roleRef` (an AWS::IAM::Role
 * logical id) that allow lambda:InvokeFunction on a resource referencing
 * `workerLogicalId`. The portal Lambda roles' default policies exceed the
 * inline-policy size limit, so CDK splits them into overflow
 * AWS::IAM::ManagedPolicy resources — search both types (the
 * gsam-preview-infra.test.ts precedent).
 */
function invokeStatementsOnWorker(
  roleRef: string,
  workerLogicalId: string
): any[] {
  const policies = [
    ...Object.values(computeTemplate.findResources('AWS::IAM::Policy')),
    ...Object.values(computeTemplate.findResources('AWS::IAM::ManagedPolicy')),
  ] as any[];
  return policies
    .filter((p) => p.Properties.Roles?.some((r: any) => r.Ref === roleRef))
    .filter((p) => p.Properties.PolicyDocument?.Statement)
    .flatMap((p) => p.Properties.PolicyDocument.Statement as any[])
    .filter((s) => {
      if (s.Effect !== 'Allow') return false;
      const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
      if (!actions.includes('lambda:InvokeFunction')) return false;
      const resources = Array.isArray(s.Resource) ? s.Resource : [s.Resource];
      return resources.some((r: any) =>
        JSON.stringify(r).includes(workerLogicalId)
      );
    });
}

/** The logical id of the role a function resource executes as. */
function roleRefOf(fn: any): string {
  const roleRef = fn.Properties.Role['Fn::GetAtt']?.[0];
  expect(roleRef).toBeDefined();
  return roleRef;
}

describe('default synth defines the grounded-sam worker and its wiring (portal-deploy-flag-hardening Requirement 1.1)', () => {
  test('exactly one image-package Lambda function exists: DdaGroundedSamWorker with its shipped configuration', () => {
    // DdaGroundedSamWorker now defaults ON (portal-deploy-flag-hardening);
    // the sibling DdaSamWorker stays behind its own default-OFF
    // `deploySamWorker` flag (absent here), so a default synth contains
    // exactly one AWS::Lambda::Function with PackageType: Image — the
    // grounded-sam worker, with its shipped configuration unchanged
    // (Req 1.6).
    const imageFunctions = Object.entries(
      computeTemplate.findResources('AWS::Lambda::Function')
    ).filter(
      ([, resource]) => (resource as any).Properties.PackageType === 'Image'
    );
    expect(imageFunctions).toHaveLength(1);
    const [logicalId, worker] = imageFunctions[0] as [string, any];
    expect(logicalId).toMatch(/^DdaGroundedSamWorker/);
    expect(worker.Properties.PackageType).toBe('Image');
    expect(worker.Properties.Architectures).toEqual(['x86_64']);
    expect(worker.Properties.MemorySize).toBe(10240);
    expect(worker.Properties.Timeout).toBe(300);
    // No threshold environment block: the handler's own defaults are the
    // intended values for this worker (grounded-sam-autolabel).
    expect(worker.Properties.Environment).toBeUndefined();
  });

  test('DdaGroundedSamWorker logical ids are present; DdaSamWorker logical ids are still absent', () => {
    const template = computeTemplate.toJSON();
    const logicalIds = Object.keys(template.Resources ?? {});
    expect(
      logicalIds.filter((logicalId) =>
        logicalId.startsWith('DdaGroundedSamWorker')
      )
    ).not.toEqual([]);
    expect(
      logicalIds.filter((logicalId) => logicalId.startsWith('DdaSamWorker'))
    ).toEqual([]);
  });

  test("DdaAutolabelWorker and DdaLabelingHandler carry GROUNDED_SAM_WORKER_FUNCTION_NAME referencing the worker; SAM_WORKER_FUNCTION_NAME is still absent", () => {
    const [workerLogicalId] = groundedSamWorkerFunction();
    const [, autolabel] = lambdaByHandler('dda_autolabel_worker.handler');
    expect(
      autolabel.Properties.Environment.Variables
        .GROUNDED_SAM_WORKER_FUNCTION_NAME
    ).toEqual({ Ref: workerLogicalId });
    expect(
      autolabel.Properties.Environment.Variables.SAM_WORKER_FUNCTION_NAME
    ).toBeUndefined();
    const [, labeling] = lambdaByHandler('dda_labeling.handler');
    expect(
      labeling.Properties.Environment.Variables
        .GROUNDED_SAM_WORKER_FUNCTION_NAME
    ).toEqual({ Ref: workerLogicalId });
  });

  test('both Worker_Wiring invoke grants are present: the DdaAutolabelWorker and DdaLabelingHandler roles may invoke the worker (Requirement 1.7)', () => {
    const [workerLogicalId] = groundedSamWorkerFunction();
    const [, autolabel] = lambdaByHandler('dda_autolabel_worker.handler');
    const [, labeling] = lambdaByHandler('dda_labeling.handler');
    expect(
      invokeStatementsOnWorker(roleRefOf(autolabel), workerLogicalId).length
    ).toBeGreaterThanOrEqual(1);
    expect(
      invokeStatementsOnWorker(roleRefOf(labeling), workerLogicalId).length
    ).toBeGreaterThanOrEqual(1);
  });
});

describe('DdaAutolabelWorker keeps its pre-feature configuration (Requirement 5.5)', () => {
  test('handler, runtime, timeout and memory are unchanged', () => {
    const [, fn] = lambdaByHandler('dda_autolabel_worker.handler');
    expect(fn.Properties.Handler).toBe('dda_autolabel_worker.handler');
    expect(fn.Properties.Runtime).toBe('python3.11');
    expect(fn.Properties.Timeout).toBe(300);
    // 2048 MB per llm-model-token-and-image-sizing Req 6.11 (the
    // Image_Downscaler allocation) — this feature must not move it.
    expect(fn.Properties.MemorySize).toBe(2048);
  });

  test('the two layers are unchanged: SharedLayer then the ImagingLayer Pillow build', () => {
    const [, fn] = lambdaByHandler('dda_autolabel_worker.handler');
    const layers = (fn.Properties.Layers ?? []).map(
      (layer: any) => layer.Ref
    );
    expect(layers).toHaveLength(2);
    expect(layers[0]).toMatch(/^SharedLayer/);
    expect(layers[1]).toMatch(/^ImagingLayer/);
  });

  test('the environment carries exactly the pre-feature key set plus GROUNDED_SAM_WORKER_FUNCTION_NAME', () => {
    const [, fn] = lambdaByHandler('dda_autolabel_worker.handler');
    const env = fn.Properties.Environment.Variables;
    // lambdaEnvironment (the shared portal Lambda environment) plus the
    // three DdaAutolabelWorker-specific keys (CODE_VERSION and the two
    // per-model `llm:` settings), plus exactly one key gained by the
    // default-ON Worker_Flag (portal-deploy-flag-hardening Req 6.1):
    // GROUNDED_SAM_WORKER_FUNCTION_NAME. No other key added, none removed:
    // the grounded-sam family's prompt inputs ride the job record, not the
    // environment.
    expect(Object.keys(env).sort()).toEqual(
      [
        'AUDIT_LOG_TABLE',
        'CAMERA_REGISTRY_TABLE',
        'CODE_VERSION',
        'COMPONENTS_TABLE',
        'COMPONENT_BUCKET_PREFIX',
        'DDA_LOCAL_SERVER_VERSION',
        'DEPLOYMENTS_TABLE',
        'DEVICES_TABLE',
        'GROUNDED_SAM_WORKER_FUNCTION_NAME',
        'LABELING_JOBS_TABLE',
        'LABELING_TASKS_TABLE',
        'LABELING_TEAMS_TABLE',
        'LLM_MODEL_IMAGE_LIMITS',
        'LLM_MODEL_TOKEN_LIMITS',
        'MODELS_TABLE',
        'PORTAL_ACCOUNT_ID',
        'PORTAL_ARTIFACTS_BUCKET',
        'PRE_LABELED_DATASETS_TABLE',
        'SETTINGS_TABLE',
        'SHARED_COMPONENTS_TABLE',
        'TEST_DATASETS_TABLE',
        'TEST_RUNS_TABLE',
        'TRAINING_JOBS_TABLE',
        'USECASES_TABLE',
        'USER_POOL_ID',
        'USER_ROLES_TABLE',
        'WORKFLOWS_S3_PREFIX',
        'WORKFLOWS_TABLE',
        'WORKFLOW_CHAT_SESSIONS_TABLE',
        'WORKFLOW_MIN_LOCAL_SERVER_VERSIONS',
        'WORKFLOW_VERSIONS_TABLE',
      ].sort()
    );
  });

  test('the static environment values are unchanged', () => {
    const [, fn] = lambdaByHandler('dda_autolabel_worker.handler');
    const env = fn.Properties.Environment.Variables;
    expect(env.CODE_VERSION).toBe('2026-02-27-dda-labeling');
    // Default synth (no llmModelImageLimits/llmModelTokenLimits context):
    // both per-model settings resolve their '{}' default.
    expect(env.LLM_MODEL_IMAGE_LIMITS).toBe('{}');
    expect(env.LLM_MODEL_TOKEN_LIMITS).toBe('{}');
    expect(env.WORKFLOWS_S3_PREFIX).toBe('workflows');
    expect(env.DDA_LOCAL_SERVER_VERSION).toBe('1.0.63');
    expect(env.COMPONENT_BUCKET_PREFIX).toBe('dda-component');
    expect(env.WORKFLOW_MIN_LOCAL_SERVER_VERSIONS).toBe(
      JSON.stringify({
        arm64_jp4: '1.0.0',
        arm64_jp5: '1.0.0',
        arm64_jp6: '1.0.0',
        arm64_jp7: '1.0.0',
        x86_64: '1.0.0',
        x86_64_nvidia: '1.0.0',
      })
    );
  });
});
