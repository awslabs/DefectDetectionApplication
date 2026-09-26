/**
 * Static infrastructure assertions for detector-checkpoint-import tasks 4.1
 * and 4.2.
 *
 * - The DetectorExportRepository: `dda-detector-export`, scan-on-push,
 *   IMMUTABLE tags, RETAIN.
 * - Its repository policy grants pull only, to concrete
 *   DDASageMakerExecutionRole ARNs of the trusted use-case accounts.
 * - `-c detectorExportImage` reaches ModelConverterHandler's
 *   DETECTOR_EXPORT_IMAGE, and is '' when unset. A value not pinned by digest
 *   fails the synth.
 * - TrainingHandler and TrainingEventsHandler carry PACKAGING_FUNCTION_NAME
 *   and may invoke PackagingHandler (the finalize invoke, Requirement 7.5).
 * - POST /models/upload-url -> ModelConverterHandler behind the Cognito
 *   authorizer, in api-gateway-stack.ts and in its duplicate api-model-stack.ts.
 *
 * Conventions follow llm-model-token-and-image-sizing-infra.test.ts: each
 * ComputeStack is synthesized once in beforeAll, and assertions run on the raw
 * CloudFormation properties.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as path from 'path';
import { ApiModelStack } from '../lib/api-model-stack';
import { ComputeStack } from '../lib/compute-stack';
import {
  DETECTOR_EXPORT_IMAGE_SSM_PARAMETER,
  detectorExportImage,
  detectorExportImageDefault,
} from '../lib/context-helpers';
import { StorageStack } from '../lib/storage-stack';

const TRUSTED = ['111111111111', '222222222222'];
const IMAGE =
  '164152369890.dkr.ecr.us-east-1.amazonaws.com/dda-detector-export@sha256:' +
  '0814885ea1ee171e06f1231f4ed9fba1aa740557b1bf0d0946df6d8c1a3bcdab';

function synthCompute(context: Record<string, unknown>) {
  const app = new cdk.App({ context });
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
    trustedUseCaseAccountIds: TRUSTED,
  });
  return {
    compute: Template.fromStack(compute),
    api: Template.fromStack(compute.node.findChild('ApiGateway') as cdk.NestedStack),
  };
}

let withImage: { compute: Template; api: Template };
let withoutImage: { compute: Template; api: Template };
let apiModel: Template;

beforeAll(() => {
  withImage = synthCompute({ detectorExportImage: `  ${IMAGE}  ` });
  withoutImage = synthCompute({});

  // api-model-stack.ts is a duplicate route table that no stack instantiates
  // today; synthesize it in isolation so the duplicate cannot drift.
  const app = new cdk.App();
  const parent = new cdk.Stack(app, 'Parent');
  const api = new apigateway.RestApi(parent, 'Api');
  const pool = new cognito.UserPool(parent, 'Pool');
  const authorizer = new apigateway.CognitoUserPoolsAuthorizer(parent, 'Authorizer', {
    cognitoUserPools: [pool],
  });
  const fn = (id: string) =>
    new lambda.Function(parent, id, {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(e, c):\n    return {}'),
    });
  new ApiModelStack(parent, 'ApiModel', {
    api,
    authorizer,
    trainingHandler: fn('Training'),
    compilationHandler: fn('Compilation'),
    packagingHandler: fn('Packaging'),
    greengrassPublishHandler: fn('GreengrassPublish'),
    modelsHandler: fn('Models'),
    modelImportHandler: fn('ModelImport'),
    modelConverterHandler: fn('ModelConverterHandler'),
    componentsHandler: fn('Components'),
    sharedComponentsHandler: fn('SharedComponents'),
    lambdaEnvironment: {},
    createLambdaRole: (name: string) =>
      new iam.Role(parent, `${name}Role`, {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      }),
    sharedLayer: new lambda.LayerVersion(parent, 'Shared', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/shared')),
    }),
  });
  // Its routes hang off the parent's RestApi, so they synthesize into the
  // parent template (unlike ApiGatewayStack, which owns its RestApi).
  apiModel = Template.fromStack(parent);
}, 600_000);

function lambdaByHandler(template: Template, handler: string): [string, any] {
  const matches = Object.entries(template.findResources('AWS::Lambda::Function')).filter(
    ([, resource]) => (resource as any).Properties.Handler === handler,
  );
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

describe('DetectorExportRepository (Requirement 5.5)', () => {
  test('is dda-detector-export with scan-on-push, immutable tags and RETAIN', () => {
    const repos = Object.entries(withImage.compute.findResources('AWS::ECR::Repository')).filter(
      ([logicalId]) => logicalId.startsWith('DetectorExportRepository'),
    );
    expect(repos).toHaveLength(1);
    const [, repo] = repos[0] as [string, any];
    expect(repo.Properties.RepositoryName).toBe('dda-detector-export');
    expect(repo.Properties.ImageScanningConfiguration).toEqual({ ScanOnPush: true });
    expect(repo.Properties.ImageTagMutability).toBe('IMMUTABLE');
    expect(repo.DeletionPolicy).toBe('Retain');
    expect(repo.UpdateReplacePolicy).toBe('Retain');
  });

  test('its policy grants pull only, to concrete trusted-account DDASageMakerExecutionRole ARNs', () => {
    const [, repo] = Object.entries(withImage.compute.findResources('AWS::ECR::Repository')).find(
      ([logicalId]) => logicalId.startsWith('DetectorExportRepository'),
    ) as [string, any];
    const statements = repo.Properties.RepositoryPolicyText.Statement;
    expect(statements).toHaveLength(1);
    const [statement] = statements;
    expect(statement.Effect).toBe('Allow');
    expect(statement.Sid).toBe('UseCaseSageMakerExecutionRolePull');
    expect([...statement.Action].sort()).toEqual([
      'ecr:BatchCheckLayerAvailability',
      'ecr:BatchGetImage',
      'ecr:GetDownloadUrlForLayer',
    ]);
    const principals = ([] as any[]).concat(statement.Principal.AWS).sort();
    expect(principals).toEqual(
      TRUSTED.map((id) => `arn:aws:iam::${id}:role/DDASageMakerExecutionRole`).sort(),
    );
    for (const arn of principals as string[]) {
      expect(arn).not.toContain('*');
    }
    expect(JSON.stringify(statement)).not.toMatch(/ecr:(Put|Delete|Initiate|Upload|Complete|Set)/);
  });

  test('the repository URI is exported for build-and-push.sh', () => {
    expect(Object.keys(withImage.compute.findOutputs('*'))).toEqual(
      expect.arrayContaining([expect.stringMatching(/^DetectorExportRepositoryUri/)]),
    );
  });
});

describe('DETECTOR_EXPORT_IMAGE (Requirement 4.8)', () => {
  test('carries the trimmed -c detectorExportImage value', () => {
    const [, fn] = lambdaByHandler(withImage.compute, 'model_converter.handler');
    expect(fn.Properties.Environment.Variables.DETECTOR_EXPORT_IMAGE).toBe(IMAGE);
  });

  test('is empty when the context is unset (conversion reported as not configured)', () => {
    const [, fn] = lambdaByHandler(withoutImage.compute, 'model_converter.handler');
    expect(fn.Properties.Environment.Variables.DETECTOR_EXPORT_IMAGE).toBe('');
  });

  test('only the converter carries it', () => {
    const carriers = Object.values(withImage.compute.findResources('AWS::Lambda::Function')).filter(
      (fn: any) => fn.Properties.Environment?.Variables?.DETECTOR_EXPORT_IMAGE !== undefined,
    );
    expect(carriers).toHaveLength(1);
  });

  test('a value that is not pinned by digest fails the synth', () => {
    expect(detectorExportImage(undefined)).toBe('');
    expect(detectorExportImage('   ')).toBe('');
    expect(detectorExportImage(` ${IMAGE}\n`)).toBe(IMAGE);
    for (const bad of [
      '164152369890.dkr.ecr.us-east-1.amazonaws.com/dda-detector-export:latest',
      '164152369890.dkr.ecr.us-east-1.amazonaws.com/dda-detector-export@sha256:abc',
      'docker.io/library/python@sha256:' + 'a'.repeat(64),
      'public.ecr.aws/x/y@sha256:' + 'a'.repeat(64),
      `${IMAGE} extra`,
      42,
    ]) {
      expect(() => detectorExportImage(bad)).toThrow(/detectorExportImage/);
    }
    expect(() => synthCompute({ detectorExportImage: 'repo:latest' })).toThrow(/pinned by digest/);
  }, 300_000);
});

describe('detectorExportImage default for flag-less deploys (task 10)', () => {
  const OTHER = IMAGE.replace(/[0-9a-f]{64}$/, 'b'.repeat(64));
  const ssmReturning = (value: string) => {
    const reads: string[] = [];
    const read = (name: string) => {
      reads.push(name);
      return value;
    };
    return { read, reads };
  };

  test('env DETECTOR_EXPORT_IMAGE wins and SSM is not read', () => {
    const ssm = ssmReturning(OTHER);
    expect(detectorExportImageDefault({ DETECTOR_EXPORT_IMAGE: ` ${IMAGE} ` }, ssm.read)).toBe(IMAGE);
    expect(ssm.reads).toEqual([]);
  });

  test('otherwise the SSM parameter', () => {
    const ssm = ssmReturning(`${OTHER}\n`);
    expect(detectorExportImageDefault({}, ssm.read)).toBe(OTHER);
    expect(ssm.reads).toEqual([DETECTOR_EXPORT_IMAGE_SSM_PARAMETER]);
    expect(DETECTOR_EXPORT_IMAGE_SSM_PARAMETER).toBe('/dda-portal/detector-export-image');
  });

  test('no value, "None", or a failed read gives no default', () => {
    expect(detectorExportImageDefault({ DETECTOR_EXPORT_IMAGE: '  ' }, ssmReturning('').read)).toBeUndefined();
    expect(detectorExportImageDefault({}, ssmReturning('None').read)).toBeUndefined();
    expect(
      detectorExportImageDefault({}, () => {
        throw new Error('ParameterNotFound');
      }),
    ).toBeUndefined();
  });

  test('App-props context is only a default: -c (even blank) overrides it, and it is still validated', () => {
    const resolved = (cliContext: Record<string, unknown> | undefined) => {
      // cdk.App reads CLI -c values from CDK_CONTEXT_JSON; props context is primed first.
      const saved = process.env.CDK_CONTEXT_JSON;
      if (cliContext) process.env.CDK_CONTEXT_JSON = JSON.stringify(cliContext);
      else delete process.env.CDK_CONTEXT_JSON;
      try {
        const app = new cdk.App({ context: { detectorExportImage: IMAGE } });
        return detectorExportImage(app.node.tryGetContext('detectorExportImage'));
      } finally {
        if (saved === undefined) delete process.env.CDK_CONTEXT_JSON;
        else process.env.CDK_CONTEXT_JSON = saved;
      }
    };
    expect(resolved(undefined)).toBe(IMAGE);
    expect(resolved({ detectorExportImage: OTHER })).toBe(OTHER);
    expect(resolved({ detectorExportImage: '' })).toBe('');
    expect(() => detectorExportImage(detectorExportImageDefault({}, () => 'repo:latest'))).toThrow(
      /pinned by digest/,
    );
  });
});

describe('finalize invoke wiring (Requirement 7.5)', () => {
  const packagingRef = (template: Template) =>
    lambdaByHandler(template, 'packaging.handler')[0];

  for (const handler of ['training.handler', 'training_events.handler']) {
    test(`${handler} carries PACKAGING_FUNCTION_NAME = PackagingHandler`, () => {
      const [, fn] = lambdaByHandler(withImage.compute, handler);
      expect(fn.Properties.Environment.Variables.PACKAGING_FUNCTION_NAME).toEqual({
        Ref: packagingRef(withImage.compute),
      });
    });

    test(`${handler} may invoke PackagingHandler`, () => {
      const [, fn] = lambdaByHandler(withImage.compute, handler);
      const roleId = fn.Properties.Role['Fn::GetAtt'][0];
      const packagingId = packagingRef(withImage.compute);
      // Large default policies overflow into AWS::IAM::ManagedPolicy resources.
      const policies = [
        ...Object.values(withImage.compute.findResources('AWS::IAM::Policy')),
        ...Object.values(withImage.compute.findResources('AWS::IAM::ManagedPolicy')),
      ];
      const grants = policies
        .filter((policy: any) => (policy.Properties.Roles ?? []).some((r: any) => r.Ref === roleId))
        .flatMap((policy: any) => policy.Properties.PolicyDocument.Statement)
        .filter((s: any) => ([] as any[]).concat(s.Action).includes('lambda:InvokeFunction'))
        .flatMap((s: any) => ([] as any[]).concat(s.Resource));
      expect(JSON.stringify(grants)).toContain(packagingId);
    });
  }
});

describe('POST /models/upload-url (Requirement 3.1)', () => {
  function uploadUrlMethods(template: Template): any[] {
    const resources = template.findResources('AWS::ApiGateway::Resource');
    // The root-level /models (its parent is the API's RootResourceId, not an
    // in-template resource such as /data-accounts/{id}).
    const models = Object.entries(resources).filter(
      ([, r]) =>
        (r as any).Properties.PathPart === 'models' &&
        (r as any).Properties.ParentId?.['Fn::GetAtt']?.[1] === 'RootResourceId',
    );
    expect(models).toHaveLength(1);
    const uploads = Object.entries(resources).filter(
      ([, r]) =>
        (r as any).Properties.PathPart === 'upload-url' &&
        (r as any).Properties.ParentId?.Ref === models[0][0],
    );
    expect(uploads).toHaveLength(1);
    return Object.values(template.findResources('AWS::ApiGateway::Method'))
      .map((m: any) => m.Properties)
      .filter((p) => p.ResourceId?.Ref === uploads[0][0] && p.HttpMethod !== 'OPTIONS');
  }

  for (const [name, get] of [
    ['api-gateway-stack.ts', () => withImage.api],
    ['api-model-stack.ts', () => apiModel],
  ] as Array<[string, () => Template]>) {
    test(`${name}: one Cognito-authorized POST proxied to ModelConverterHandler`, () => {
      const methods = uploadUrlMethods(get());
      expect(methods).toHaveLength(1);
      const [method] = methods;
      expect(method.HttpMethod).toBe('POST');
      expect(method.AuthorizationType).toBe('COGNITO_USER_POOLS');
      expect(method.AuthorizerId).toBeDefined();
      expect(method.Integration.Type).toBe('AWS_PROXY');
      expect(JSON.stringify(method.Integration.Uri)).toContain('ModelConverterHandler');
    });
  }
});
