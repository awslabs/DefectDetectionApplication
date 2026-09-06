/**
 * Static infrastructure assertions for the grounded-sam-autolabel worker
 * gating (task 5.2).
 *
 * Requirements covered:
 * - 5.2: with the Worker_Flag (`deployGroundedSamWorker`) absent, the
 *   ComputeStack defines no Grounded_SAM_Worker resources and
 *   DdaAutolabelWorker's environment carries no
 *   GROUNDED_SAM_WORKER_FUNCTION_NAME entry.
 * - 5.5: the existing DdaSamWorker definition stays behind its own
 *   `deploySamWorker` flag (also absent here, so also not synthesized) and
 *   DdaAutolabelWorker's own configuration — handler, runtime, timeout,
 *   memory, layers, and its always-present environment keys — is exactly
 *   the pre-feature configuration.
 *
 * Flag-ON synthesis is DELIBERATELY NOT tested here: setting
 * `deployGroundedSamWorker=true` (or `deploySamWorker=true`) makes
 * `DockerImageCode.fromImageAsset` perform a real Docker build at synth
 * time — a multi-gigabyte build downloading the Grounding DINO ONNX model,
 * its tokenizer, and the SAM archive. That is the same reason no
 * `deploySamWorker=true` jest test exists today. The gated deploy (task
 * 7.2) is the flag-on verification. This suite must add zero Docker
 * activity to the synth.
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
  // routine portal deployment (Req 5.2).
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

describe('default synth defines no gated worker resources (Requirement 5.2)', () => {
  test('no image-package Lambda function exists in the ComputeStack template', () => {
    // Both container-image workers (DdaGroundedSamWorker and DdaSamWorker)
    // are the only DockerImageFunction sources in the stack and both sit
    // behind default-OFF context flags, so a default synth must contain no
    // AWS::Lambda::Function with PackageType: Image.
    const imageFunctions = Object.entries(
      computeTemplate.findResources('AWS::Lambda::Function')
    ).filter(
      ([, resource]) => (resource as any).Properties.PackageType === 'Image'
    );
    expect(imageFunctions).toEqual([]);
  });

  test('no DdaGroundedSamWorker or DdaSamWorker logical ids exist anywhere in the template', () => {
    const template = computeTemplate.toJSON();
    const workerIds = Object.keys(template.Resources ?? {}).filter(
      (logicalId) =>
        logicalId.startsWith('DdaGroundedSamWorker') ||
        logicalId.startsWith('DdaSamWorker')
    );
    expect(workerIds).toEqual([]);
  });

  test("DdaAutolabelWorker's environment carries neither GROUNDED_SAM_WORKER_FUNCTION_NAME nor SAM_WORKER_FUNCTION_NAME", () => {
    const [, fn] = lambdaByHandler('dda_autolabel_worker.handler');
    const env = fn.Properties.Environment.Variables;
    expect(env.GROUNDED_SAM_WORKER_FUNCTION_NAME).toBeUndefined();
    expect(env.SAM_WORKER_FUNCTION_NAME).toBeUndefined();
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

  test('the environment carries exactly the pre-feature key set', () => {
    const [, fn] = lambdaByHandler('dda_autolabel_worker.handler');
    const env = fn.Properties.Environment.Variables;
    // lambdaEnvironment (the shared portal Lambda environment) plus the
    // three DdaAutolabelWorker-specific keys (CODE_VERSION and the two
    // per-model `llm:` settings). No key added, none removed: the
    // grounded-sam family's prompt inputs ride the job record, not the
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
