/**
 * Static infrastructure assertions for Portal-managed stream camera
 * credentials and the stream feature floor (rtsp-rtmp-stream-cameras
 * tasks 9.1 and 9.2).
 *
 * Requirements covered:
 * - 5.9, 6.6: the Portal may write, withdraw, restore, and schedule
 *   deletion of the secrets under dda-portal/stream-camera-credentials/,
 *   and may never read one (no secretsmanager:GetSecretValue on either
 *   principal that writes them); it may write the device read grant as an
 *   inline policy on the devices' token-exchange role, and nothing broader.
 * - 9.7: the packaging and deployment Lambdas read the same feature-floor
 *   literal, empty (fail-closed) until supporting LocalServer builds exist.
 * - design component 7: the camera registry Lambda imports the shared
 *   Stream_URL rules from the workflow_core layer.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';
import { UseCaseAccountStack } from '../lib/usecase-account-stack';

const PORTAL_ACCOUNT = '222222222222';
const USECASE_ACCOUNT = '333333333333';
const REGION = 'us-east-1';

const SECRET_WRITE_ACTIONS = [
  'secretsmanager:CreateSecret',
  'secretsmanager:DeleteSecret',
  'secretsmanager:DescribeSecret',
  'secretsmanager:PutSecretValue',
  'secretsmanager:RestoreSecret',
  'secretsmanager:TagResource',
  'secretsmanager:UpdateSecretVersionStage',
];
const DEVICE_GRANT_ACTIONS = ['iam:GetRolePolicy', 'iam:PutRolePolicy'];
const FEATURE_FLOOR_ENV = 'WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS';

let computeTemplate: Template;
let usecaseTemplate: Template;

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
    trustedUseCaseAccountIds: ['111111111111'],
  });
  const usecase = new UseCaseAccountStack(app, 'UseCase', {
    env: { account: USECASE_ACCOUNT, region: REGION },
    portalAccountId: PORTAL_ACCOUNT,
    externalId: 'test-external-id',
  });
  computeTemplate = Template.fromStack(compute);
  usecaseTemplate = Template.fromStack(usecase);
}, 300_000);

const asList = (value: any): any[] =>
  value === undefined ? [] : Array.isArray(value) ? value : [value];

/** The single Lambda function with the given handler. */
function lambdaWithHandler(handler: string): any {
  const matches = Object.values(
    computeTemplate.findResources('AWS::Lambda::Function')
  ).filter((r: any) => r.Properties.Handler === handler);
  expect(matches).toHaveLength(1);
  return matches[0];
}

function workflowCoreLayerId(): string {
  const matches = Object.entries(
    computeTemplate.findResources('AWS::Lambda::LayerVersion')
  ).filter(([, r]: [string, any]) =>
    String(r.Properties.Description).startsWith('workflow_core shared package')
  );
  expect(matches).toHaveLength(1);
  return matches[0][0];
}

/** Every IAM statement granted to a role, across its default policy and
 *  any overflow managed policies (the compute stack minimizes policies, so
 *  statements may be merged or spread). */
function statementsOfRole(template: Template, roleLogicalId: string): any[] {
  const statements: any[] = [];
  for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    for (const resource of Object.values(template.findResources(type)) as any[]) {
      const roles = asList(resource.Properties.Roles);
      if (roles.some((ref: any) => ref.Ref === roleLogicalId)) {
        statements.push(...asList(resource.Properties.PolicyDocument.Statement));
      }
    }
  }
  return statements;
}

function roleOfFunction(fn: any): string {
  return fn.Properties.Role['Fn::GetAtt'][0];
}

/** The resources an Allow statement set grants `action` on. */
function resourcesGranting(statements: any[], action: string): any[] {
  return statements
    .filter((s) => s.Effect === 'Allow' && asList(s.Action).includes(action))
    .flatMap((s) => asList(s.Resource));
}

function portalAccessRole(): [string, any] {
  const matches = Object.entries(
    usecaseTemplate.findResources('AWS::IAM::Role')
  ).filter(([, r]: [string, any]) => r.Properties.RoleName === 'DDAPortalAccessRole');
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

const noSecretReads = (statements: any[]) => {
  for (const statement of statements) {
    for (const action of asList(statement.Action)) {
      expect(action).not.toBe('secretsmanager:GetSecretValue');
      expect(action).not.toBe('secretsmanager:*');
      expect(action).not.toBe('*');
    }
  }
};

describe('use-case account role (Requirements 5.9, 6.6)', () => {
  test('StreamCameraCredentialWrite grants exactly the write actions on the credential prefix', () => {
    const [roleId] = portalAccessRole();
    const statements = statementsOfRole(usecaseTemplate, roleId);
    const statement = statements.find((s) => s.Sid === 'StreamCameraCredentialWrite');
    expect(statement).toBeDefined();
    expect(statement.Effect).toBe('Allow');
    expect([...asList(statement.Action)].sort()).toEqual(SECRET_WRITE_ACTIONS);
    expect(asList(statement.Resource)).toEqual([
      `arn:aws:secretsmanager:*:${USECASE_ACCOUNT}:secret:dda-portal/stream-camera-credentials/*`,
    ]);
    expect(statement.Condition).toBeUndefined();
  });

  test('the role can write inline policies on the token-exchange role only', () => {
    const [roleId] = portalAccessRole();
    const statements = statementsOfRole(usecaseTemplate, roleId);
    for (const action of DEVICE_GRANT_ACTIONS) {
      expect(resourcesGranting(statements, action)).toEqual([
        `arn:aws:iam::${USECASE_ACCOUNT}:role/GreengrassV2TokenExchangeRole`,
      ]);
    }
    for (const action of ['iam:AttachRolePolicy', 'iam:CreateRole', 'iam:DeleteRolePolicy']) {
      expect(resourcesGranting(statements, action)).toEqual([]);
    }
  });

  test('the Portal can never read a stream camera credential back', () => {
    const [roleId] = portalAccessRole();
    noSecretReads(statementsOfRole(usecaseTemplate, roleId));
  });

  test('the stack version records the new permissions', () => {
    usecaseTemplate.hasOutput('StackVersion', { Value: '1.7.0' });
  });
});

describe('camera registry Lambda (single-account setups, design component 7)', () => {
  test('carries the workflow_core layer for the shared Stream_URL rules', () => {
    const fn = lambdaWithHandler('camera_registry.handler');
    expect(asList(fn.Properties.Layers)).toContainEqual({ Ref: workflowCoreLayerId() });
  });

  test('its role holds the same write-only credential grant and device read grant', () => {
    const fn = lambdaWithHandler('camera_registry.handler');
    const statements = statementsOfRole(computeTemplate, roleOfFunction(fn));
    const prefix = {
      'Fn::Join': ['', [
        'arn:aws:secretsmanager:*:', { Ref: 'AWS::AccountId' },
        ':secret:dda-portal/stream-camera-credentials/*',
      ]],
    };
    for (const action of SECRET_WRITE_ACTIONS) {
      expect(resourcesGranting(statements, action)).toEqual([prefix]);
    }
    const tesRole = {
      'Fn::Join': ['', [
        'arn:aws:iam::', { Ref: 'AWS::AccountId' },
        ':role/GreengrassV2TokenExchangeRole',
      ]],
    };
    for (const action of DEVICE_GRANT_ACTIONS) {
      expect(resourcesGranting(statements, action)).toEqual([tesRole]);
    }
    noSecretReads(statements);
  });

  test('camera_registry.py imports only the stdlib-only stream_url from workflow_core', () => {
    // The layer's jsonschema dependency carries a cp311-only native module
    // and this Lambda runs Python 3.12 (compute-stack.ts, WorkflowCoreLayer).
    const source = fs.readFileSync(
      path.join(__dirname, '../../backend/functions/camera_registry.py'), 'utf-8'
    );
    const imports = source.match(/^\s*(from|import)\s+workflow_core[\w.]*/gm) ?? [];
    expect(imports.length).toBeGreaterThan(0);
    for (const line of imports) {
      expect(line.trim()).toBe('from workflow_core.stream_url');
    }
  });
});

describe('stream feature floor (Requirement 9.7)', () => {
  // The first LocalServer builds verified on hardware with the feature (spec
  // task 26.3). arm64_cpu has no verified build, so it stays out and fails
  // closed with STREAM_CAMERAS_UNSUPPORTED_ARCH.
  const VERIFIED_FLOOR = {
    arm64_jp5: '1.0.51',
    arm64_jp6: '1.0.74',
    arm64_jp7: '1.0.52',
    x86_64: '1.0.47',
    x86_64_nvidia: '1.0.47',
  };
  test.each([['workflow_packaging.handler'], ['deployments.handler']])(
    '%s reads the verified feature floor, without arm64_cpu', (handler) => {
      const fn = lambdaWithHandler(handler);
      const floor = JSON.parse(fn.Properties.Environment.Variables[FEATURE_FLOOR_ENV]);
      expect(floor).toEqual(VERIFIED_FLOOR);
      expect(floor).not.toHaveProperty('arm64_cpu');
    }
  );

  test('the deployments Lambda carries workflow_core for override validation', () => {
    const fn = lambdaWithHandler('deployments.handler');
    expect(asList(fn.Properties.Layers)).toContainEqual({ Ref: workflowCoreLayerId() });
  });
});
