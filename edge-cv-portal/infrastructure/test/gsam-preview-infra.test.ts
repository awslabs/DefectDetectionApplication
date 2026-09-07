/**
 * Static infrastructure assertions for the grounded-sam prompt tuning
 * preview executor wiring in compute-stack.ts
 * (grounded-sam-prompt-tuning-preview task 3.2).
 *
 * Requirements covered:
 * - 8.1: with the Worker_Flag (`deployGroundedSamWorker`) set true, the
 *   ComputeStack sets GROUNDED_SAM_WORKER_FUNCTION_NAME on
 *   DdaLabelingHandler's environment and grants DdaLabelingHandler
 *   lambda:InvokeFunction on the Grounded_SAM_Worker, inside the existing
 *   gated block.
 * - 8.2: with the flag absent, DdaLabelingHandler's environment carries no
 *   GROUNDED_SAM_WORKER_FUNCTION_NAME entry and no grounded-sam worker
 *   resources exist — today's flag-off template.
 * - 8.3: with the flag set true, the existing DdaAutolabelWorker wiring
 *   (env + invoke grant) and the Grounded_SAM_Worker definition are
 *   unchanged by this feature.
 *
 * On flag-ON synthesis and Docker: grounded-sam-worker-infra.test.ts
 * deliberately avoided a flag-on synth on the assumption that
 * `DockerImageCode.fromImageAsset` performs the real (multi-GB) Docker
 * build at synth time. In the installed aws-cdk-lib (2.229.x),
 * DockerImageAsset construction only runs AssetStaging — it copies and
 * fingerprints the small source directory (backend/grounded-sam-worker,
 * ~136 KB; the model downloads happen inside the Docker build) and records
 * the asset with the stack synthesizer. The `docker build` itself is
 * performed by cdk-assets at deploy time, never during `Template.fromStack`.
 * A flag-on jest synth therefore adds zero Docker activity, which is what
 * makes the Requirement 8.1/8.3 assertions testable here.
 *
 * Conventions follow workflow-manager-gaps-infra.test.ts /
 * grounded-sam-worker-infra.test.ts: synthesize once in beforeAll with a
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

// Synthesized once each: the ComputeStack stages Lambda/layer assets at
// synth time, which is expensive, and this suite needs both flag states.
let flagOnTemplate: Template;
let flagOffTemplate: Template;

function synthComputeTemplate(context?: Record<string, string>): Template {
  const app = new cdk.App({ context });

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
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
  });

  return Template.fromStack(compute);
}

beforeAll(() => {
  // The CLI's `-c deployGroundedSamWorker=true` arrives as the string
  // 'true'; compute-stack.ts accepts `=== true || === 'true'`.
  flagOnTemplate = synthComputeTemplate({ deployGroundedSamWorker: 'true' });
  flagOffTemplate = synthComputeTemplate();
}, 900_000);

/** The single Lambda function in `template` with the given handler. */
function lambdaByHandler(template: Template, handler: string): [string, any] {
  const matches = Object.entries(
    template.findResources('AWS::Lambda::Function')
  ).filter(([, resource]) => (resource as any).Properties.Handler === handler);
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

/** The single DdaGroundedSamWorker function resource in `template`. */
function groundedSamWorkerFunction(template: Template): [string, any] {
  const matches = Object.entries(
    template.findResources('AWS::Lambda::Function')
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
 * workflow-manager-gaps-infra.test.ts precedent).
 */
function invokeStatementsOnWorker(
  template: Template,
  roleRef: string,
  workerLogicalId: string
): any[] {
  const policies = [
    ...Object.values(template.findResources('AWS::IAM::Policy')),
    ...Object.values(template.findResources('AWS::IAM::ManagedPolicy')),
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

describe('flag-on synth wires the preview executor to the worker (Requirements 8.1, 8.3)', () => {
  test('DdaLabelingHandler environment carries GROUNDED_SAM_WORKER_FUNCTION_NAME referencing the worker', () => {
    const [workerLogicalId] = groundedSamWorkerFunction(flagOnTemplate);
    const [, handler] = lambdaByHandler(flagOnTemplate, 'dda_labeling.handler');
    expect(
      handler.Properties.Environment.Variables.GROUNDED_SAM_WORKER_FUNCTION_NAME
    ).toEqual({ Ref: workerLogicalId });
  });

  test('DdaLabelingHandler role is granted lambda:InvokeFunction on the worker', () => {
    const [workerLogicalId] = groundedSamWorkerFunction(flagOnTemplate);
    const [, handler] = lambdaByHandler(flagOnTemplate, 'dda_labeling.handler');
    const statements = invokeStatementsOnWorker(
      flagOnTemplate,
      roleRefOf(handler),
      workerLogicalId
    );
    expect(statements.length).toBeGreaterThanOrEqual(1);
  });

  test('the existing DdaAutolabelWorker wiring is still present: env entry and invoke grant (Requirement 8.3)', () => {
    const [workerLogicalId] = groundedSamWorkerFunction(flagOnTemplate);
    const [, autolabel] = lambdaByHandler(
      flagOnTemplate,
      'dda_autolabel_worker.handler'
    );
    expect(
      autolabel.Properties.Environment.Variables
        .GROUNDED_SAM_WORKER_FUNCTION_NAME
    ).toEqual({ Ref: workerLogicalId });
    const statements = invokeStatementsOnWorker(
      flagOnTemplate,
      roleRefOf(autolabel),
      workerLogicalId
    );
    expect(statements.length).toBeGreaterThanOrEqual(1);
  });

  test('the Grounded_SAM_Worker definition keeps its shipped configuration (Requirement 8.3)', () => {
    const [, worker] = groundedSamWorkerFunction(flagOnTemplate);
    expect(worker.Properties.PackageType).toBe('Image');
    expect(worker.Properties.Architectures).toEqual(['x86_64']);
    expect(worker.Properties.MemorySize).toBe(10240);
    expect(worker.Properties.Timeout).toBe(300);
    // No threshold environment block: the handler's own defaults are the
    // intended values for this worker (grounded-sam-autolabel).
    expect(worker.Properties.Environment).toBeUndefined();
  });
});

describe("flag-off synth produces today's template (Requirement 8.2)", () => {
  test('no DdaGroundedSamWorker logical ids exist anywhere in the template', () => {
    const template = flagOffTemplate.toJSON();
    const workerIds = Object.keys(template.Resources ?? {}).filter(
      (logicalId) => logicalId.startsWith('DdaGroundedSamWorker')
    );
    expect(workerIds).toEqual([]);
  });

  test("DdaLabelingHandler's environment carries no GROUNDED_SAM_WORKER_FUNCTION_NAME entry", () => {
    const [, handler] = lambdaByHandler(
      flagOffTemplate,
      'dda_labeling.handler'
    );
    expect(
      handler.Properties.Environment.Variables.GROUNDED_SAM_WORKER_FUNCTION_NAME
    ).toBeUndefined();
  });

  test('no policy statement in the template references a grounded-sam worker (no new grant)', () => {
    const policies = [
      ...Object.values(flagOffTemplate.findResources('AWS::IAM::Policy')),
      ...Object.values(
        flagOffTemplate.findResources('AWS::IAM::ManagedPolicy')
      ),
    ] as any[];
    const referencingStatements = policies
      .filter((p) => p.Properties.PolicyDocument?.Statement)
      .flatMap((p) => p.Properties.PolicyDocument.Statement as any[])
      .filter((s) => JSON.stringify(s).includes('DdaGroundedSamWorker'));
    expect(referencingStatements).toEqual([]);
  });
});
