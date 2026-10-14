/**
 * Static infrastructure assertions for the labeling-job-cleanup-work-stealing-
 * and-podium routes registered in dda-labeling-api-stack.ts (task 3.2).
 *
 * Requirements covered:
 * - 1.1/1.6 (Deletion_Route): DELETE /labeling/{id} attaches directly to the
 *   ApiGatewayStack-owned resource imported by id (the same imported ref the
 *   stop/review/rerun-prelabels sub-resources hang off), Cognito-authorized,
 *   proxying into DdaLabelingHandler (dda_labeling.py, where @rbac_check
 *   enforces MANAGE_LABELING_JOBS).
 * - 5.1 (Steal_Route): POST /labeler/jobs/{jobId}/steal exists with the same
 *   authorizer/integration posture as the shipped GET /labeler/jobs/{jobId}/next.
 * - 6.1 (Pool_Route): GET /labeler/jobs/{jobId}/pool likewise.
 * - No compute-stack diff: this feature adds zero handler environment keys,
 *   because the wiring the delete worker action and the labeler routes ride on
 *   (worker function name on the handler, artifacts bucket + labeling tables
 *   on the worker) already exists.
 *
 * Conventions follow workflow-manager-gaps-infra.test.ts /
 * llm-autolabel-prompt-tuning-infra.test.ts: synthesize the ComputeStack once
 * in beforeAll (default flag-off synth — no deployGroundedSamWorker context,
 * per the gsam-preview-infra.test.ts precedent), pull the DdaLabelingApi
 * nested-stack template via compute.node.findChild, locate resources with
 * template.findResources and assert on raw CloudFormation properties.
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
let labelingApiTemplate: Template;

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
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
  });

  computeTemplate = Template.fromStack(compute);
  labelingApiTemplate = Template.fromStack(
    compute.node.findChild('DdaLabelingApi') as cdk.NestedStack
  );
}, 300_000);

/** All AWS::ApiGateway::Resource entries of the DdaLabelingApi template. */
function apiResources(): Record<string, any> {
  return labelingApiTemplate.findResources('AWS::ApiGateway::Resource');
}

/** All AWS::ApiGateway::Method Properties of the DdaLabelingApi template. */
function apiMethodProps(): any[] {
  return Object.values(
    labelingApiTemplate.findResources('AWS::ApiGateway::Method')
  ).map((m: any) => m.Properties);
}

/**
 * Logical id of the single resource with `pathPart` under `parentId`.
 * `parentId === null` means "the parent is imported from outside this
 * template" (a nested-stack parameter ref — the portal API root or the
 * imported /labeling/{id} resource).
 */
function childOf(parentId: string | null, pathPart: string): string {
  const resources = apiResources();
  const matches = Object.entries(resources).filter(([, resource]) => {
    const props = (resource as any).Properties;
    if (props.PathPart !== pathPart) return false;
    const parentRef = props.ParentId?.Ref;
    const inTemplate = parentRef && resources[parentRef] !== undefined;
    return parentId === null ? !inTemplate : parentRef === parentId;
  });
  expect(matches).toHaveLength(1);
  return matches[0][0];
}

/** Non-OPTIONS method Properties attached to the given ResourceId ref. */
function methodsOnRef(resourceRef: string): any[] {
  return apiMethodProps()
    .filter((props) => props.ResourceId?.Ref === resourceRef)
    .filter((props) => props.HttpMethod !== 'OPTIONS');
}

/**
 * The nested-stack parameter ref carrying the imported /labeling/{id}
 * resource id (props.labelingJobResourceId), recovered from the stop /
 * review / rerun-prelabels sub-resources — the stop route's siblings all
 * hang off that one imported parent.
 */
function importedLabelingJobRef(): string {
  const resources = apiResources();
  const refs = ['stop', 'review', 'rerun-prelabels'].map((pathPart) => {
    const matches = Object.values(resources).filter((resource: any) => {
      const props = resource.Properties;
      if (props.PathPart !== pathPart) return false;
      const parentRef = props.ParentId?.Ref;
      // Parent not defined in this template => the imported resource.
      return parentRef !== undefined && resources[parentRef] === undefined;
    });
    expect(matches).toHaveLength(1);
    return (matches[0] as any).Properties.ParentId.Ref as string;
  });
  // One imported parent shared by all three siblings.
  expect(new Set(refs).size).toBe(1);
  return refs[0];
}

describe('DELETE /labeling/{id} — the Deletion_Route (Requirements 1.1, 1.6)', () => {
  test('a DELETE method attaches directly to the imported /labeling/{id} resource ref', () => {
    const importedRef = importedLabelingJobRef();
    const methods = methodsOnRef(importedRef);
    // Only the DELETE method attaches here — the resource (and its OPTIONS
    // preflight) is owned by the ApiGatewayStack.
    expect(methods.map((m) => m.HttpMethod)).toEqual(['DELETE']);
  });

  test('the DELETE method is Cognito-authorized and integrates with DdaLabelingHandler', () => {
    const importedRef = importedLabelingJobRef();
    const [deleteMethod] = methodsOnRef(importedRef).filter(
      (m) => m.HttpMethod === 'DELETE'
    );
    expect(deleteMethod).toBeDefined();
    expect(deleteMethod.AuthorizationType).toBe('COGNITO_USER_POOLS');
    expect(deleteMethod.AuthorizerId).toBeDefined();
    expect(deleteMethod.Integration.Type).toBe('AWS_PROXY');
    // dda_labeling.py owns request_job_deletion — the integration URI must
    // reference the DdaLabelingHandler function ARN parameter, not the
    // labeling.py handler the sibling stop route uses.
    expect(JSON.stringify(deleteMethod.Integration.Uri)).toContain(
      'DdaLabelingHandler'
    );

    // Contrast: the stop route on the sibling resource stays on labeling.py.
    const stopId = childOf(null, 'stop');
    const [stopMethod] = methodsOnRef(stopId);
    expect(stopMethod.HttpMethod).toBe('POST');
    expect(JSON.stringify(stopMethod.Integration.Uri)).not.toContain(
      'DdaLabelingHandler'
    );
  });
});

describe('labeler pool and steal routes (Requirements 5.1, 6.1)', () => {
  /** Resource ids of /labeler/jobs/{jobId}/{next,pool,steal}. */
  function labelerJobChildren(): {
    nextId: string;
    poolId: string;
    stealId: string;
  } {
    const labelerId = childOf(null, 'labeler');
    const jobsId = childOf(labelerId, 'jobs');
    const jobIdId = childOf(jobsId, '{jobId}');
    return {
      nextId: childOf(jobIdId, 'next'),
      poolId: childOf(jobIdId, 'pool'),
      stealId: childOf(jobIdId, 'steal'),
    };
  }

  test('GET /labeler/jobs/{jobId}/pool and POST /labeler/jobs/{jobId}/steal exist', () => {
    const { poolId, stealId } = labelerJobChildren();
    expect(methodsOnRef(poolId).map((m) => m.HttpMethod)).toEqual(['GET']);
    expect(methodsOnRef(stealId).map((m) => m.HttpMethod)).toEqual(['POST']);
  });

  test('pool and steal carry the exact authorizer/integration posture of /next', () => {
    labelingApiTemplate.hasResourceProperties('AWS::ApiGateway::Authorizer', {
      Type: 'COGNITO_USER_POOLS',
      Name: 'EdgeCVPortalDdaLabelingAuthorizer',
      IdentitySource: 'method.request.header.Authorization',
    });

    const { nextId, poolId, stealId } = labelerJobChildren();
    const [nextMethod] = methodsOnRef(nextId);
    expect(nextMethod.HttpMethod).toBe('GET');
    // /next is the shipped labeler-route posture the new routes must match.
    expect(nextMethod.AuthorizationType).toBe('COGNITO_USER_POOLS');
    expect(nextMethod.AuthorizerId).toBeDefined();
    expect(nextMethod.Integration.Type).toBe('AWS_PROXY');
    expect(JSON.stringify(nextMethod.Integration.Uri)).toContain(
      'DdaLabelingHandler'
    );

    for (const resourceId of [poolId, stealId]) {
      const [method] = methodsOnRef(resourceId);
      expect(method.AuthorizationType).toBe('COGNITO_USER_POOLS');
      // Same authorizer instance...
      expect(method.AuthorizerId).toEqual(nextMethod.AuthorizerId);
      // ...and the same DdaLabelingHandler proxy integration.
      expect(method.Integration.Type).toBe('AWS_PROXY');
      expect(method.Integration.Uri).toEqual(nextMethod.Integration.Uri);
    }
  });
});

describe('compute stack shows no diff from this feature', () => {
  /** The single Lambda function in the compute template with `handler`. */
  function lambdaByHandler(handler: string): any {
    const matches = Object.values(
      computeTemplate.findResources('AWS::Lambda::Function')
    ).filter((resource: any) => resource.Properties.Handler === handler);
    expect(matches).toHaveLength(1);
    return matches[0];
  }

  test('handler and worker environments carry no keys introduced by this spec', () => {
    for (const handler of [
      'dda_labeling.handler',
      'dda_labeling_worker.handler',
    ]) {
      const fn = lambdaByHandler(handler);
      const keys = Object.keys(fn.Properties.Environment.Variables);
      // Deletion, stealing and the podium ride existing wiring — the spec
      // introduces zero environment keys (design: "No compute-stack change").
      expect(keys.filter((k) => /DELET|STEAL|PODIUM/.test(k))).toEqual([]);
    }
  });

  test('the pre-existing wiring the feature rides on is already present', () => {
    // The Deletion_Route async-invokes the worker by the name already in the
    // handler environment; the worker already reaches the artifacts bucket
    // (Job_Artifact_Prefix deletion) and both labeling tables (task items,
    // job record).
    const handlerEnv = lambdaByHandler('dda_labeling.handler').Properties
      .Environment.Variables;
    expect(handlerEnv.DDA_LABELING_WORKER_FUNCTION_NAME).toBeDefined();
    expect(handlerEnv.PORTAL_ARTIFACTS_BUCKET).toBeDefined();

    const workerEnv = lambdaByHandler('dda_labeling_worker.handler').Properties
      .Environment.Variables;
    expect(workerEnv.PORTAL_ARTIFACTS_BUCKET).toBeDefined();
    expect(workerEnv.LABELING_TASKS_TABLE).toBeDefined();
    expect(workerEnv.LABELING_JOBS_TABLE).toBeDefined();
  });
});
