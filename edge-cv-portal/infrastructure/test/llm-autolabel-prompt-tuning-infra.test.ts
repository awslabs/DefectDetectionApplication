/**
 * Static infrastructure assertions for the Prompt_Tuning_Preview wiring
 * (llm-autolabel-prompt-tuning task 7.4).
 *
 * Requirements covered:
 * - 3.3: preview runs on DdaLabelingHandler itself — it needs
 *   bedrock:InvokeModel, a timeout covering the whole per-run bound (up to 5
 *   sequential 120 s model invocations plus S3 reads) and permission to
 *   async-invoke itself as the run executor.
 * - 7.1: the per-model Model_Image_Limit configuration (LLM_MODEL_IMAGE_LIMITS)
 *   reaches every function that resolves it, so preview, labeling time and the
 *   wizard hint read one source.
 * - 8.1: both Preview_API routes exist behind the Cognito authorizer.
 * - 1.6 / 3.5 / 8.8 (storage side): preview run state is reaped by tasks-table
 *   TTL and result payloads expire out of the artifacts bucket, so nothing a
 *   preview writes is durable pipeline state.
 */
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';

/** Per-run executor bound: 5 Sample_Images x 120 s plus the 60 s of slack the
 *  lock TTL (`min(sample_count * 120 + 60, 900)`) allows for. */
const PER_RUN_BOUND_SECONDS = 5 * 120 + 60;

// Synthesized once: the ComputeStack stages Lambda/layer assets and runs the
// quick-setup bundle packaging script at synth time, which is expensive.
let storageTemplate: Template;
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

  storageTemplate = Template.fromStack(storage);
  computeTemplate = Template.fromStack(compute);
  labelingApiTemplate = Template.fromStack(
    compute.node.findChild('DdaLabelingApi') as cdk.NestedStack
  );
}, 300_000);

/** The single Lambda function in the compute template with `handler`. */
function lambdaByHandler(handler: string): [string, any] {
  const matches = Object.entries(
    computeTemplate.findResources('AWS::Lambda::Function')
  ).filter(([, resource]) => (resource as any).Properties.Handler === handler);
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

/**
 * Every IAM statement attached to the role of `fn`. A role carrying many
 * grants overflows the inline-policy size limit and CDK splits it into
 * managed policies, so both resource types are searched.
 */
function statementsForRole(fn: any): any[] {
  const roleRef = fn.Properties.Role['Fn::GetAtt'][0];
  const policies = [
    ...Object.values(computeTemplate.findResources('AWS::IAM::Policy')),
    ...Object.values(computeTemplate.findResources('AWS::IAM::ManagedPolicy')),
  ] as any[];
  return policies
    .filter((p) => (p.Properties.Roles ?? []).some((r: any) => r.Ref === roleRef))
    .filter((p) => p.Properties.PolicyDocument?.Statement)
    .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
}

const asArray = (value: any): any[] =>
  Array.isArray(value) ? value : [value];

describe('DdaLabelingHandler runs Prompt_Tuning_Preview (Requirements 3.3, 7.1)', () => {
  test('timeout covers the whole per-run executor bound', () => {
    const [, fn] = lambdaByHandler('dda_labeling.handler');
    expect(fn.Properties.Timeout).toBeGreaterThanOrEqual(PER_RUN_BOUND_SECONDS);
    // Lambda's hard maximum — a larger value would not synthesize.
    expect(fn.Properties.Timeout).toBeLessThanOrEqual(900);
  });

  test('role may invoke Bedrock models over the foundation-model and inference-profile scope', () => {
    const [, fn] = lambdaByHandler('dda_labeling.handler');
    const invokeModel = statementsForRole(fn).filter((s) =>
      asArray(s.Action).includes('bedrock:InvokeModel')
    );
    expect(invokeModel.length).toBeGreaterThanOrEqual(1);

    const actions = invokeModel.flatMap((s) => asArray(s.Action));
    expect(actions).toContain('bedrock:InvokeModelWithResponseStream');

    const resources = invokeModel.flatMap((s) =>
      asArray(s.Resource).map((r) => JSON.stringify(r))
    );
    expect(
      resources.some((r) => r.includes('foundation-model/*'))
    ).toBe(true);
    expect(
      resources.some((r) => r.includes('inference-profile/*'))
    ).toBe(true);
  });

  test('role may async-invoke its own function as the preview executor', () => {
    const [logicalId, fn] = lambdaByHandler('dda_labeling.handler');
    const selfInvoke = statementsForRole(fn).filter((s) => {
      if (!asArray(s.Action).includes('lambda:InvokeFunction')) return false;
      return asArray(s.Resource).some((r) =>
        JSON.stringify(r).includes(logicalId)
      );
    });
    expect(selfInvoke.length).toBeGreaterThanOrEqual(1);
    expect(logicalId).toContain('DdaLabelingHandler');
  });

  test('LLM_MODEL_IMAGE_LIMITS reaches the preview, labeling and model-options paths', () => {
    for (const handler of [
      'dda_labeling.handler',
      'dda_autolabel_worker.handler',
      'data_accounts.handler',
    ]) {
      const [, fn] = lambdaByHandler(handler);
      // Default with no `llmModelImageLimits` context: every model resolves
      // to MODEL_IMAGE_LIMIT_DEFAULT, never to zero or unbounded.
      expect(fn.Properties.Environment.Variables.LLM_MODEL_IMAGE_LIMITS).toBe(
        '{}'
      );
    }
  });
});

describe('Preview_API routes (Requirement 8.1)', () => {
  /** Resource ids of /labeling-preview, /runs and /{runId} in the nested stack. */
  function previewResourceIds(): {
    previewId: string;
    runsId: string;
    runIdId: string;
  } {
    const resources = labelingApiTemplate.findResources(
      'AWS::ApiGateway::Resource'
    );
    const childOf = (parentId: string | null, pathPart: string): string => {
      const matches = Object.entries(resources).filter(([, resource]) => {
        const props = (resource as any).Properties;
        if (props.PathPart !== pathPart) return false;
        const parentRef = props.ParentId?.Ref;
        // A ParentId that is not a resource in this template is the imported
        // API root (passed in as a nested-stack parameter).
        const inTemplate = parentRef && resources[parentRef] !== undefined;
        return parentId === null ? !inTemplate : parentRef === parentId;
      });
      expect(matches).toHaveLength(1);
      return matches[0][0];
    };
    const previewId = childOf(null, 'labeling-preview');
    const runsId = childOf(previewId, 'runs');
    return { previewId, runsId, runIdId: childOf(runsId, '{runId}') };
  }

  test('POST /labeling-preview/runs and GET /labeling-preview/runs/{runId} exist', () => {
    const { runsId, runIdId } = previewResourceIds();
    const methodsOn = (resourceId: string): string[] =>
      Object.values(labelingApiTemplate.findResources('AWS::ApiGateway::Method'))
        .map((m: any) => m.Properties)
        .filter((props) => props.ResourceId?.Ref === resourceId)
        .filter((props) => props.HttpMethod !== 'OPTIONS')
        .map((props) => props.HttpMethod)
        .sort();

    expect(methodsOn(runsId)).toEqual(['POST']);
    expect(methodsOn(runIdId)).toEqual(['GET']);
  });

  test('both preview routes sit behind the Cognito authorizer on the labeling handler', () => {
    const { runsId, runIdId } = previewResourceIds();
    labelingApiTemplate.hasResourceProperties('AWS::ApiGateway::Authorizer', {
      Type: 'COGNITO_USER_POOLS',
      Name: 'EdgeCVPortalDdaLabelingAuthorizer',
      IdentitySource: 'method.request.header.Authorization',
    });

    const previewMethods = Object.values(
      labelingApiTemplate.findResources('AWS::ApiGateway::Method')
    )
      .map((m: any) => m.Properties)
      .filter((props) =>
        [runsId, runIdId].includes(props.ResourceId?.Ref)
      )
      .filter((props) => props.HttpMethod !== 'OPTIONS');
    expect(previewMethods).toHaveLength(2);
    for (const props of previewMethods) {
      expect(props.AuthorizationType).toBe('COGNITO_USER_POOLS');
      expect(props.AuthorizerId).toBeDefined();
      // Lambda proxy integration into DdaLabelingHandler (imported into the
      // nested stack as a parameter reference).
      expect(props.Integration.Type).toBe('AWS_PROXY');
    }
  });
});

describe('preview state and payload expiry (Requirements 1.6, 3.5, 8.8)', () => {
  test('labeling tasks table has TTL enabled on the ttl attribute', () => {
    storageTemplate.hasResourceProperties(
      'AWS::DynamoDB::Table',
      Match.objectLike({
        TableName: 'dda-portal-labeling-tasks',
        TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
      })
    );
  });

  test('artifacts bucket expires labeling-previews/ payloads after a day', () => {
    const buckets = Object.entries(
      storageTemplate.findResources('AWS::S3::Bucket')
    ).filter(([logicalId]) => logicalId.startsWith('PortalArtifactsBucket'));
    expect(buckets).toHaveLength(1);

    const rules = (buckets[0][1] as any).Properties.LifecycleConfiguration
      .Rules as any[];
    const previewRule = rules.find(
      (rule) => rule.Id === 'ExpireLabelingPreviews'
    );
    expect(previewRule).toMatchObject({
      Prefix: 'labeling-previews/',
      Status: 'Enabled',
      ExpirationInDays: 1,
      NoncurrentVersionExpiration: { NoncurrentDays: 1 },
    });
  });
});
