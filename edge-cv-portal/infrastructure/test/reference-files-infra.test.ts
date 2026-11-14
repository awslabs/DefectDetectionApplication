/**
 * Static infrastructure assertions for reference files uploaded for Bedrock
 * Inference nodes (inspection-format tuning fix, design §9.13.12): AC-44.
 *
 * - StorageStack: the Portal artifacts bucket expires staged uploads under
 *   workflow-reference-staging/ after one day (current and noncurrent
 *   versions, incomplete multipart uploads), beside ExpireLabelingPreviews.
 * - WorkflowManagerGapsApiStack: GET and POST /workflows/{id}/reference-files
 *   and POST /workflows/{id}/reference-files/upload-url behind the Cognito
 *   authorizer with the Workflows handler's integration, and the stage
 *   deployment depending on the two resources, their three methods and two
 *   CORS preflights, so the deployment snapshot always includes them.
 * - ComputeStack: the Workflows handler gets 1024 MB for the PNG content
 *   check and keeps its runtime, timeout and layers.
 *
 * On the device's commit c299a6e1 there is no such rule, no method on
 * /workflows/{id}/reference-files (so the deployment cannot depend on one)
 * and no MemorySize on the Workflows handler.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';

// Synthesized once: the ComputeStack stages Lambda/layer assets, which is
// expensive.
let storageTemplate: Template;
let computeTemplate: Template;
let gapsTemplate: Template;

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
  gapsTemplate = Template.fromStack(
    compute.node.findChild('WorkflowManagerGapsApi') as cdk.NestedStack
  );
}, 300_000);

/** The Portal artifacts bucket's lifecycle rules, by id. */
function artifactsBucketRules(): Record<string, any> {
  const buckets = Object.values(storageTemplate.findResources('AWS::S3::Bucket')).filter(
    (bucket: any) =>
      JSON.stringify(bucket.Properties.BucketName ?? '').includes('dda-portal-artifacts-')
  ) as any[];
  expect(buckets).toHaveLength(1);
  const rules = buckets[0].Properties.LifecycleConfiguration?.Rules ?? [];
  return Object.fromEntries(rules.map((rule: any) => [rule.Id, rule]));
}

/** `{logicalId: path}` of the gaps stack's resources; a parent outside the
 * template is the imported resource of the given path. */
function resourcePaths(): Record<string, string> {
  const resources = gapsTemplate.findResources('AWS::ApiGateway::Resource');
  const imported: Record<string, string> = {};
  const pathOf = (logicalId: string): string => {
    const resource: any = resources[logicalId];
    const parentRef = resource.Properties.ParentId?.Ref;
    const prefix =
      parentRef && resources[parentRef]
        ? pathOf(parentRef)
        : resource.Properties.PathPart === '{job_id}'
          ? '/workflows/generate'
          : '/workflows/{id}';
    return `${prefix}/${resource.Properties.PathPart}`;
  };
  for (const logicalId of Object.keys(resources)) imported[logicalId] = pathOf(logicalId);
  return imported;
}

/** `[logicalId, properties]` of every method, keyed `METHOD /path`. */
function methodsByRoute(): Record<string, [string, any]> {
  const paths = resourcePaths();
  const methods: Record<string, [string, any]> = {};
  for (const [logicalId, method] of Object.entries(
    gapsTemplate.findResources('AWS::ApiGateway::Method')
  )) {
    const props = (method as any).Properties;
    methods[`${props.HttpMethod} ${paths[props.ResourceId.Ref]}`] = [logicalId, props];
  }
  return methods;
}

const NEW_ROUTES = [
  'GET /workflows/{id}/reference-files',
  'POST /workflows/{id}/reference-files',
  'POST /workflows/{id}/reference-files/upload-url',
];

describe('staged reference-file uploads expire (design §9.13.12 item 1)', () => {
  test('ExpireWorkflowReferenceStaging expires current, noncurrent and multipart staging after 1 day', () => {
    const rules = artifactsBucketRules();
    expect(rules.ExpireWorkflowReferenceStaging).toEqual({
      Id: 'ExpireWorkflowReferenceStaging',
      Prefix: 'workflow-reference-staging/',
      Status: 'Enabled',
      ExpirationInDays: 1,
      NoncurrentVersionExpiration: { NoncurrentDays: 1 },
      AbortIncompleteMultipartUpload: { DaysAfterInitiation: 1 },
    });
    expect(rules.ExpireLabelingPreviews).toMatchObject({
      Prefix: 'labeling-previews/',
      Status: 'Enabled',
      ExpirationInDays: 1,
    });
  });
});

describe('reference-file routes (design §9.13.12 item 2)', () => {
  test('the three routes use the Cognito authorizer and the Workflows integration', () => {
    const methods = methodsByRoute();
    const [, rename] = methods['PATCH /workflows/{id}/name'];
    for (const route of NEW_ROUTES) {
      expect(methods[route]).toBeDefined();
      const [, props] = methods[route];
      expect(props.AuthorizationType).toBe('COGNITO_USER_POOLS');
      expect(props.AuthorizerId).toEqual(rename.AuthorizerId);
      expect(props.Integration.Type).toBe('AWS_PROXY');
      // The rename route is served by the same workflows.py handler.
      expect(props.Integration.Uri).toEqual(rename.Integration.Uri);
    }
    for (const preflight of [
      'OPTIONS /workflows/{id}/reference-files',
      'OPTIONS /workflows/{id}/reference-files/upload-url',
    ]) {
      expect(methods[preflight]).toBeDefined();
      expect(methods[preflight][1].Integration.Type).toBe('MOCK');
    }
  });

  test('the stage deployment depends on the two resources, three methods and two preflights', () => {
    const paths = resourcePaths();
    const methods = methodsByRoute();
    const deployments = Object.values(
      gapsTemplate.findResources('AWS::ApiGateway::Deployment')
    ) as any[];
    expect(deployments).toHaveLength(1);
    const dependsOn: string[] = deployments[0].DependsOn ?? [];
    const resourceIds = Object.entries(paths)
      .filter(([, path]) => path.startsWith('/workflows/{id}/reference-files'))
      .map(([logicalId]) => logicalId);
    expect(resourceIds).toHaveLength(2);
    const methodIds = [
      ...NEW_ROUTES,
      'OPTIONS /workflows/{id}/reference-files',
      'OPTIONS /workflows/{id}/reference-files/upload-url',
    ].map((route) => methods[route]?.[0]);
    expect(methodIds.filter((id) => id === undefined)).toEqual([]);
    for (const logicalId of [...resourceIds, ...methodIds]) {
      expect(dependsOn).toContain(logicalId);
    }
  });
});

describe('Workflows handler memory (design §9.13.12 item 7)', () => {
  test('workflows.handler has MemorySize 1024 and keeps its runtime, timeout and layers', () => {
    const handlers = Object.values(computeTemplate.findResources('AWS::Lambda::Function')).filter(
      (fn: any) => fn.Properties.Handler === 'workflows.handler'
    ) as any[];
    expect(handlers).toHaveLength(1);
    const props = handlers[0].Properties;
    expect(props.MemorySize).toBe(1024);
    expect(props.Runtime).toBe('python3.11');
    expect(props.Timeout).toBe(60);
    expect(props.Layers).toHaveLength(2);
  });
});
