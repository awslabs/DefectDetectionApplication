/**
 * Static infrastructure assertions for the llm-model-token-and-image-sizing
 * wiring (task 12.3).
 *
 * Requirements covered:
 * - 1.8: the Model_Token_Limits bootstrap (LLM_MODEL_TOKEN_LIMITS) is
 *   delivered through the same per-model configuration mechanism as the
 *   Model_Image_Limit (LLM_MODEL_IMAGE_LIMITS): one context-derived string
 *   reaching exactly the three handlers that build or report `llm:` requests
 *   (DdaLabelingHandler preview, DdaAutolabelWorker labeling time,
 *   DataAccountsHandler model options).
 * - 6.6: preview and labeling time must emit byte-identical downscaled bytes
 *   for the same source and setting. The environmental precondition is one
 *   Pillow build: DdaLabelingHandler and DdaAutolabelWorker each carry two
 *   layers whose second is the same imagingLayer LayerVersion
 *   DdaLabelingWorker already attaches, and both get the 2048 MB allocation
 *   the resize path is sized for.
 * - 4.1: GET and PUT /data-accounts/{id}/token-limits exist on the
 *   data-accounts integration behind the stack's Cognito authorizer.
 *
 * Plus the non-regression half of the task: DdaLabelingWorker's and
 * SyntheticImagingLayer's definitions are unchanged, and the
 * DdaLabelingSelfInvokePolicy cycle guard is still a standalone iam.Policy
 * with grantInvoke(self) still absent.
 *
 * Conventions follow llm-autolabel-prompt-tuning-infra.test.ts /
 * workflow-manager-gaps-infra.test.ts: synthesize once in beforeAll with a
 * generous timeout (Lambda/layer asset staging is expensive), locate
 * resources via template.findResources, assert on raw CloudFormation
 * properties. The beforeAll synth of all four stacks is itself the synth
 * gate: a reintroduced dependency cycle or synth-time error fails every
 * test here.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';
import { SyntheticDataStack } from '../lib/synthetic-data-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';

/**
 * Context values exercising both derivation branches of the shared
 * string-passthrough / object-stringify / '{}' shape the two per-model
 * settings are built with (compute-stack.ts):
 * - llmModelTokenLimits as an object takes the JSON.stringify branch;
 * - llmModelImageLimits as a string takes the passthrough branch (the
 *   deliberate inner space would not survive a parse/re-stringify, so
 *   equality proves passthrough).
 * The same derived string must then reach every carrier (Req 1.8).
 */
const TOKEN_LIMITS_CONTEXT = { 'us.amazon.nova-pro-v1:0': 10000 };
const EXPECTED_TOKEN_LIMITS = JSON.stringify(TOKEN_LIMITS_CONTEXT);
const IMAGE_LIMITS_CONTEXT = '{"us.amazon.nova-pro-v1:0": 4}';

/** The three handlers that build or report `llm:` requests. */
const PER_MODEL_SETTINGS_CARRIERS = [
  'dda_labeling.handler', // Preview_API (DdaLabelingHandler)
  'dda_autolabel_worker.handler', // labeling time (DdaAutolabelWorker)
  'data_accounts.handler', // model options (DataAccountsHandler)
];

// Synthesized once: the ComputeStack stages Lambda/layer assets and runs the
// quick-setup bundle packaging script at synth time, which is expensive.
let computeTemplate: Template;
let apiTemplate: Template;
let syntheticTemplate: Template;

beforeAll(() => {
  const app = new cdk.App({
    context: {
      llmModelTokenLimits: TOKEN_LIMITS_CONTEXT,
      llmModelImageLimits: IMAGE_LIMITS_CONTEXT,
    },
  });

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

  // The SyntheticDataStack bundles the same backend/layers/imaging directory
  // as its own LayerVersion; synthesized here to pin that its definition is
  // untouched by this feature.
  const synthetic = new SyntheticDataStack(app, 'SyntheticData', {
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    trainingJobsTable: storage.trainingJobsTable,
    // The stack throws at synth time on an empty list — always pass one.
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
    userPool,
    restApiId: 'testrestapiid',
    restApiRootResourceId: 'testrootresourceid',
    apiStageName: 'v1',
  });

  computeTemplate = Template.fromStack(compute);
  // The /data-accounts routes live in the ApiGateway nested stack.
  apiTemplate = Template.fromStack(
    compute.node.findChild('ApiGateway') as cdk.NestedStack
  );
  syntheticTemplate = Template.fromStack(synthetic);
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

const asArray = (value: any): any[] => (Array.isArray(value) ? value : [value]);

describe('shared imaging layer and memory for the Image_Downscaler (Requirement 6.6)', () => {
  test('DdaLabelingHandler and DdaAutolabelWorker each carry two layers, the second the same imagingLayer Ref as DdaLabelingWorker', () => {
    const [, worker] = lambdaByHandler('dda_labeling_worker.handler');
    const workerLayers = layerRefs(worker);
    expect(workerLayers).toHaveLength(2);

    // DdaLabelingWorker's second layer is the stack's single ImagingLayer
    // LayerVersion (the Pillow build) — not some new layer source.
    const imagingRef = workerLayers[1];
    expect(imagingRef).toMatch(/^ImagingLayer/);
    const imagingLayer =
      computeTemplate.findResources('AWS::Lambda::LayerVersion')[imagingRef];
    expect(imagingLayer).toBeDefined();
    expect((imagingLayer as any).Properties.Description).toBe(
      'Pillow imaging layer for DDA labeling mask rendering (built by ' +
        'backend/layers/imaging/build.sh)'
    );

    for (const handler of [
      'dda_labeling.handler',
      'dda_autolabel_worker.handler',
    ]) {
      const [, fn] = lambdaByHandler(handler);
      const layers = layerRefs(fn);
      expect(layers).toHaveLength(2);
      expect(layers[0]).toMatch(/^SharedLayer/);
      // The identical Ref, not merely "an" imaging layer: one Pillow build
      // across every function that runs the Image_Downscaler.
      expect(layers[1]).toBe(imagingRef);
    }
  });

  test('both request-path functions have MemorySize 2048', () => {
    for (const handler of [
      'dda_labeling.handler',
      'dda_autolabel_worker.handler',
    ]) {
      const [, fn] = lambdaByHandler(handler);
      expect(fn.Properties.MemorySize).toBe(2048);
    }
  });
});

describe('per-model settings delivery (Requirement 1.8)', () => {
  test('LLM_MODEL_TOKEN_LIMITS rides alongside LLM_MODEL_IMAGE_LIMITS on all three carriers, from the same context-derived strings', () => {
    for (const handler of PER_MODEL_SETTINGS_CARRIERS) {
      const [, fn] = lambdaByHandler(handler);
      const env = fn.Properties.Environment.Variables;
      // Object context takes the JSON.stringify branch.
      expect(env.LLM_MODEL_TOKEN_LIMITS).toBe(EXPECTED_TOKEN_LIMITS);
      // String context passes through byte-for-byte (inner space preserved).
      expect(env.LLM_MODEL_IMAGE_LIMITS).toBe(IMAGE_LIMITS_CONTEXT);
    }
  });

  test('DataAccountsHandler carries LLM_MODEL_TOKEN_LIMITS so the displayed budget resolves from the source the request paths use', () => {
    const [, fn] = lambdaByHandler('data_accounts.handler');
    expect(fn.Properties.Environment.Variables.LLM_MODEL_TOKEN_LIMITS).toBe(
      EXPECTED_TOKEN_LIMITS
    );
  });
});

describe('token-limits routes (Requirement 4.1)', () => {
  /** Resource ids of /data-accounts, /{id} and /token-limits. */
  function tokenLimitsResourceId(): string {
    const resources = apiTemplate.findResources('AWS::ApiGateway::Resource');
    const childOf = (parentId: string | null, pathPart: string): string => {
      const matches = Object.entries(resources).filter(([, resource]) => {
        const props = (resource as any).Properties;
        if (props.PathPart !== pathPart) return false;
        const parentRef = props.ParentId?.Ref;
        // Root children reference the Rest API's RootResourceId via
        // Fn::GetAtt, never a Ref to an in-template Resource.
        const inTemplate = parentRef && resources[parentRef] !== undefined;
        return parentId === null ? !inTemplate : parentRef === parentId;
      });
      expect(matches).toHaveLength(1);
      return matches[0][0];
    };
    const dataAccountsId = childOf(null, 'data-accounts');
    const idId = childOf(dataAccountsId, '{id}');
    return childOf(idId, 'token-limits');
  }

  /** Non-OPTIONS method properties on `resourceId`. */
  function methodsOn(resourceId: string): any[] {
    return Object.values(apiTemplate.findResources('AWS::ApiGateway::Method'))
      .map((method: any) => method.Properties)
      .filter((props) => props.ResourceId?.Ref === resourceId)
      .filter((props) => props.HttpMethod !== 'OPTIONS');
  }

  test('GET and PUT /data-accounts/{id}/token-limits exist', () => {
    const methods = methodsOn(tokenLimitsResourceId());
    expect(methods.map((props) => props.HttpMethod).sort()).toEqual([
      'GET',
      'PUT',
    ]);
  });

  test('both routes sit behind the stack Cognito authorizer on the data-accounts integration', () => {
    const authorizerIds = Object.entries(
      apiTemplate.findResources('AWS::ApiGateway::Authorizer')
    )
      .filter(
        ([, authorizer]) =>
          (authorizer as any).Properties.Type === 'COGNITO_USER_POOLS' &&
          (authorizer as any).Properties.Name === 'EdgeCVPortalAuthorizer'
      )
      .map(([logicalId]) => logicalId);
    expect(authorizerIds).toHaveLength(1);

    const methods = methodsOn(tokenLimitsResourceId());
    expect(methods).toHaveLength(2);
    for (const props of methods) {
      expect(props.AuthorizationType).toBe('COGNITO_USER_POOLS');
      expect(props.AuthorizerId?.Ref).toBe(authorizerIds[0]);
      // Lambda proxy integration into DataAccountsHandler (imported into the
      // nested stack as a parameter reference).
      expect(props.Integration.Type).toBe('AWS_PROXY');
      expect(JSON.stringify(props.Integration.Uri)).toContain(
        'DataAccountsHandler'
      );
    }
  });
});

describe('non-regression: definitions this feature must not touch', () => {
  test('DdaLabelingWorker is unchanged: same two layers, 900 s / 2048 MB, and no per-model env vars', () => {
    const [, worker] = lambdaByHandler('dda_labeling_worker.handler');
    expect(worker.Properties.Timeout).toBe(900);
    expect(worker.Properties.MemorySize).toBe(2048);

    const layers = layerRefs(worker);
    expect(layers).toHaveLength(2);
    expect(layers[0]).toMatch(/^SharedLayer/);
    expect(layers[1]).toMatch(/^ImagingLayer/);

    // The per-model settings are carried by exactly the three `llm:` request
    // handlers, deliberately not by lambdaEnvironment — DdaLabelingWorker
    // (distribution/notifications/manifests) gains neither.
    const env = worker.Properties.Environment.Variables;
    expect(env.LLM_MODEL_TOKEN_LIMITS).toBeUndefined();
    expect(env.LLM_MODEL_IMAGE_LIMITS).toBeUndefined();
    expect(env.AUTOLABEL_QUEUE_URL).toBeDefined();
  });

  test('SyntheticImagingLayer is unchanged and still bundles its own copy of the imaging asset', () => {
    const matches = Object.entries(
      syntheticTemplate.findResources('AWS::Lambda::LayerVersion')
    ).filter(([logicalId]) => logicalId.startsWith('SyntheticImagingLayer'));
    expect(matches).toHaveLength(1);

    const [, layer] = matches[0] as [string, any];
    expect(layer.Properties.CompatibleRuntimes).toEqual(['python3.11']);
    expect(layer.Properties.Description).toBe(
      'Pillow for synthetic preview image decode/diff (bbox_from_diff auto-annotation)'
    );

    // Same backend/layers/imaging source directory as the ComputeStack's
    // ImagingLayer (equal content-hash asset keys) — no new layer source was
    // introduced by attaching the compute layer more widely.
    const computeImaging = Object.entries(
      computeTemplate.findResources('AWS::Lambda::LayerVersion')
    ).filter(([logicalId]) => logicalId.startsWith('ImagingLayer'));
    expect(computeImaging).toHaveLength(1);
    expect(layer.Properties.Content.S3Key).toBe(
      (computeImaging[0][1] as any).Properties.Content.S3Key
    );
  });

  test('DdaLabelingSelfInvokePolicy is still a standalone iam.Policy carrying the self-invoke grant', () => {
    const [handlerLogicalId, fn] = lambdaByHandler('dda_labeling.handler');
    const roleRef = fn.Properties.Role['Fn::GetAtt'][0];

    const standalone = Object.entries(
      computeTemplate.findResources('AWS::IAM::Policy')
    ).filter(([logicalId]) =>
      logicalId.startsWith('DdaLabelingSelfInvokePolicy')
    );
    expect(standalone).toHaveLength(1);

    const [, policy] = standalone[0] as [string, any];
    // Attached to the handler's role...
    expect(
      (policy.Properties.Roles ?? []).some((role: any) => role.Ref === roleRef)
    ).toBe(true);
    // ...and carrying lambda:InvokeFunction on the handler's own ARN.
    const selfInvoke = (
      policy.Properties.PolicyDocument.Statement as any[]
    ).filter(
      (statement) =>
        asArray(statement.Action).includes('lambda:InvokeFunction') &&
        asArray(statement.Resource).some((resource) =>
          JSON.stringify(resource).includes(handlerLogicalId)
        )
    );
    expect(selfInvoke.length).toBeGreaterThanOrEqual(1);
  });

  test('grantInvoke(self) stays absent: no other policy on the role references the function (the dependency-cycle guard)', () => {
    const [handlerLogicalId, fn] = lambdaByHandler('dda_labeling.handler');
    const roleRef = fn.Properties.Role['Fn::GetAtt'][0];

    // Every policy attached to the role EXCEPT the standalone one. A role
    // carrying many grants overflows the inline-policy size limit and CDK
    // splits it into managed policies, so both resource types are searched.
    const selfInvokeElsewhere = [
      ...Object.entries(computeTemplate.findResources('AWS::IAM::Policy')),
      ...Object.entries(
        computeTemplate.findResources('AWS::IAM::ManagedPolicy')
      ),
    ]
      .filter(
        ([logicalId]) => !logicalId.startsWith('DdaLabelingSelfInvokePolicy')
      )
      .map(([, policy]) => policy as any)
      .filter((policy) =>
        (policy.Properties.Roles ?? []).some(
          (role: any) => role.Ref === roleRef
        )
      )
      .filter((policy) => policy.Properties.PolicyDocument?.Statement)
      .flatMap((policy) => policy.Properties.PolicyDocument.Statement as any[])
      .filter(
        (statement) =>
          asArray(statement.Action).includes('lambda:InvokeFunction') &&
          asArray(statement.Resource).some((resource) =>
            JSON.stringify(resource).includes(handlerLogicalId)
          )
      );
    expect(selfInvokeElsewhere).toHaveLength(0);
  });
});
