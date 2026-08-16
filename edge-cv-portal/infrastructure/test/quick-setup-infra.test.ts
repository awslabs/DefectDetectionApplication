/**
 * Static infrastructure assertions for the station-quick-setup CDK additions
 * (station-quick-setup task 8.4).
 *
 * Requirements covered:
 * - 3.9: every token-authenticated /quick-setup/* method is registered with
 *   AuthorizationType.NONE (validated in-handler) and carries method-level
 *   throttling (~10 rps / burst 20) as API Gateway defense-in-depth.
 * - 4.4: the Setup_Bundle is packaged at deploy time (BucketDeployment under
 *   quick-setup/current/) and the resulting bundle key + bootstrap key are
 *   baked into the quick_setup Lambda environment so only the most recently
 *   deployed bundle is ever served.
 * - 4.5: the bundle/bootstrap SHA-256 checksums computed over the exact
 *   uploaded bytes are wired into the Lambda environments (the quick_setup
 *   handler serves the bundle checksum; the device_registrations handler
 *   embeds the bootstrap checksum in the Setup_Command).
 */
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';

// Synthesized once: the ComputeStack stages Lambda/layer assets and runs the
// bundle packaging script at synth time, which is expensive.
let storageTemplate: Template;
let computeTemplate: Template;
let quickSetupApiTemplate: Template;
let apiGatewayTemplate: Template;

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
  quickSetupApiTemplate = Template.fromStack(
    compute.node.findChild('QuickSetupApi') as cdk.NestedStack
  );
  // The v1 stage (and therefore the /quick-setup/* method-level throttling) is
  // owned by the ApiGateway nested stack — a CfnDeployment re-pointing an
  // existing stage cannot carry a StageDescription.
  apiGatewayTemplate = Template.fromStack(
    compute.node.findChild('ApiGateway') as cdk.NestedStack
  );
}, 300_000);

/** The single resource of the given type whose properties match `predicate`. */
function findResource(
  template: Template,
  type: string,
  predicate: (props: any) => boolean
): [string, any] {
  const matches = Object.entries(template.findResources(type)).filter(
    ([, resource]) => predicate((resource as any).Properties)
  );
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

describe('device-registrations table (Requirement 1.1)', () => {
  test('dda-portal-device-registrations schema: registration_id PK, on-demand, TTL, PITR, retained', () => {
    storageTemplate.hasResource('AWS::DynamoDB::Table', {
      DeletionPolicy: 'Retain',
      Properties: Match.objectLike({
        TableName: 'dda-portal-device-registrations',
        KeySchema: [{ AttributeName: 'registration_id', KeyType: 'HASH' }],
        BillingMode: 'PAY_PER_REQUEST',
        // TTL attribute auto-expires the RATELIMIT# counter items (Req 3.9).
        TimeToLiveSpecification: {
          AttributeName: 'ttl',
          Enabled: true,
        },
        PointInTimeRecoverySpecification: {
          PointInTimeRecoveryEnabled: true,
        },
      }),
    });
  });

  test('usecase-device-index GSI partitions on usecase_id, sorts by device_name', () => {
    const [, table] = findResource(
      storageTemplate,
      'AWS::DynamoDB::Table',
      (props) => props.TableName === 'dda-portal-device-registrations'
    );
    expect(table.Properties.GlobalSecondaryIndexes).toEqual([
      expect.objectContaining({
        IndexName: 'usecase-device-index',
        KeySchema: [
          { AttributeName: 'usecase_id', KeyType: 'HASH' },
          { AttributeName: 'device_name', KeyType: 'RANGE' },
        ],
      }),
    ]);
    expect(table.Properties.AttributeDefinitions).toEqual(
      expect.arrayContaining([
        { AttributeName: 'registration_id', AttributeType: 'S' },
        { AttributeName: 'usecase_id', AttributeType: 'S' },
        { AttributeName: 'device_name', AttributeType: 'S' },
      ])
    );
  });
});

describe('Setup_Bundle packaging and checksum wiring (Requirements 4.4, 4.5)', () => {
  test('the Setup_Bundle is deployed to quick-setup/current/ at deploy time', () => {
    // s3deploy.BucketDeployment renders as a Custom::CDKBucketDeployment
    // resource; the destination prefix is the atomic per-deploy location so a
    // failed deploy leaves prior artifacts in place (Req 4.4).
    computeTemplate.hasResourceProperties('Custom::CDKBucketDeployment', {
      DestinationBucketKeyPrefix: 'quick-setup/current/',
    });
  });

  test('device_registrations Lambda carries the table name and bootstrap checksum', () => {
    const [, handler] = findResource(
      computeTemplate,
      'AWS::Lambda::Function',
      (props) => props.Handler === 'device_registrations.handler'
    );
    expect(handler.Properties.Runtime).toBe('python3.11');
    const env = handler.Properties.Environment.Variables;
    expect(env.REGISTRATIONS_TABLE).toBeDefined();
    // The bootstrap checksum is embedded in the generated Setup_Command so the
    // station verifies the bootstrap it downloads (Req 4.5, integrity anchor).
    expect(typeof env.QUICK_SETUP_BOOTSTRAP_SHA256).toBe('string');
    expect(env.QUICK_SETUP_BOOTSTRAP_SHA256).toMatch(/^[0-9a-f]{64}$/);
  });

  test('quick_setup Lambda carries the bundle/bootstrap keys and both checksums', () => {
    const [, handler] = findResource(
      computeTemplate,
      'AWS::Lambda::Function',
      (props) => props.Handler === 'quick_setup.handler'
    );
    expect(handler.Properties.Runtime).toBe('python3.11');
    const env = handler.Properties.Environment.Variables;
    expect(env.REGISTRATIONS_TABLE).toBeDefined();
    // Artifact keys baked from this deployment's bundle asset so only the most
    // recently deployed bundle is ever served (Req 4.4).
    expect(env.QUICK_SETUP_BUNDLE_KEY).toBe('quick-setup/current/setup-bundle.tar.gz');
    expect(env.QUICK_SETUP_BOOTSTRAP_KEY).toBe('quick-setup/current/bootstrap.sh');
    // Checksums computed over the exact uploaded bytes (Req 4.5).
    expect(env.QUICK_SETUP_BUNDLE_SHA256).toMatch(/^[0-9a-f]{64}$/);
    expect(env.QUICK_SETUP_BOOTSTRAP_SHA256).toMatch(/^[0-9a-f]{64}$/);
    // The bundle and bootstrap are distinct artifacts with distinct digests.
    expect(env.QUICK_SETUP_BUNDLE_SHA256).not.toBe(env.QUICK_SETUP_BOOTSTRAP_SHA256);
  });

  test('both Lambdas agree on the bootstrap checksum served vs. embedded', () => {
    const [, regHandler] = findResource(
      computeTemplate,
      'AWS::Lambda::Function',
      (props) => props.Handler === 'device_registrations.handler'
    );
    const [, qsHandler] = findResource(
      computeTemplate,
      'AWS::Lambda::Function',
      (props) => props.Handler === 'quick_setup.handler'
    );
    // The checksum the Setup_Command embeds (device_registrations) must equal
    // the checksum for the bootstrap the quick_setup handler serves, or the
    // station's integrity check would always fail (Req 4.5 chain).
    expect(regHandler.Properties.Environment.Variables.QUICK_SETUP_BOOTSTRAP_SHA256).toBe(
      qsHandler.Properties.Environment.Variables.QUICK_SETUP_BOOTSTRAP_SHA256
    );
  });
});

describe('same-account station provisioning role', () => {
  test('DDAStationProvisioningRole exists and is assumable via sts:AssumeRole', () => {
    const [, role] = findResource(
      computeTemplate,
      'AWS::IAM::Role',
      (props) => props.RoleName === 'DDAStationProvisioningRole'
    );
    const statements = role.Properties.AssumeRolePolicyDocument.Statement;
    expect(
      statements.some(
        (s: any) => s.Effect === 'Allow' && s.Action === 'sts:AssumeRole'
      )
    ).toBe(true);
  });

  test('quick_setup Lambda is wired with the provisioning role ARN', () => {
    const [, handler] = findResource(
      computeTemplate,
      'AWS::Lambda::Function',
      (props) => props.Handler === 'quick_setup.handler'
    );
    const arn =
      handler.Properties.Environment.Variables.QUICK_SETUP_PROVISIONING_ROLE_ARN;
    expect(arn).toBeDefined();
    // ACCOUNT_ID renders as a pseudo-parameter token, so match the fixed
    // role-name suffix in the rendered value.
    expect(JSON.stringify(arn)).toContain('role/DDAStationProvisioningRole');
  });
});

describe('quick-setup API routes (Requirement 3.9)', () => {
  const resources = () =>
    quickSetupApiTemplate.findResources('AWS::ApiGateway::Resource');

  /** Full path for a resource logical id (parents outside the nested stack
   * template are the imported RestApi root). */
  function pathOf(logicalId: string): string {
    const resource: any = resources()[logicalId];
    const parentRef = resource.Properties.ParentId?.Ref;
    const prefix =
      parentRef && resources()[parentRef] ? pathOf(parentRef) : '';
    return `${prefix}/${resource.Properties.PathPart}`;
  }

  /** `METHOD /path` for every non-OPTIONS method in the nested stack. */
  function methodProps(): Array<{ path: string; props: any }> {
    return Object.values(
      quickSetupApiTemplate.findResources('AWS::ApiGateway::Method')
    )
      .map((m: any) => m.Properties)
      .filter((props) => props.HttpMethod !== 'OPTIONS')
      .map((props) => ({ path: pathOf(props.ResourceId.Ref), props }));
  }

  test('exactly the four /quick-setup/* token routes are registered', () => {
    const quickSetupRoutes = methodProps()
      .filter((m) => m.path.startsWith('/quick-setup/'))
      .map((m) => `${m.props.HttpMethod} ${m.path}`)
      .sort();
    expect(quickSetupRoutes).toEqual(
      [
        'GET /quick-setup/bootstrap',
        'POST /quick-setup/bundle',
        'POST /quick-setup/credentials',
        'POST /quick-setup/status',
      ].sort()
    );
  });

  test('every /quick-setup/* method uses AuthorizationType.NONE', () => {
    const quickSetupMethods = methodProps().filter((m) =>
      m.path.startsWith('/quick-setup/')
    );
    expect(quickSetupMethods.length).toBe(4);
    for (const { props } of quickSetupMethods) {
      expect(props.AuthorizationType).toBe('NONE');
      // No Cognito authorizer is attached to the token routes.
      expect(props.AuthorizerId).toBeUndefined();
      // Lambda proxy integration into the quick_setup handler.
      expect(props.Integration.Type).toBe('AWS_PROXY');
    }
  });

  test('the JWT /device-registrations routes remain Cognito-authorized', () => {
    const registrationMethods = methodProps().filter((m) =>
      m.path.startsWith('/device-registrations')
    );
    // POST + GET on /device-registrations, GET thing-groups, DELETE {id},
    // POST {id}/command.
    expect(registrationMethods.length).toBe(5);
    for (const { props } of registrationMethods) {
      expect(props.AuthorizationType).toBe('COGNITO_USER_POOLS');
      expect(props.AuthorizerId).toBeDefined();
    }
  });

  test('the /quick-setup/* methods carry ~10 rps / burst 20 throttling', () => {
    // Method-level throttling is applied on the stage OWNER (the ApiGateway
    // nested stack's v1 Stage MethodSettings), not on the QuickSetupApi
    // deployment: a CfnDeployment re-pointing an already-existing stage cannot
    // carry a StageDescription (Req 3.9 defense-in-depth).
    const [, stage] = findResource(
      apiGatewayTemplate,
      'AWS::ApiGateway::Stage',
      (props) => Array.isArray(props.MethodSettings)
    );
    const settings: any[] = stage.Properties.MethodSettings;
    // CloudFormation MethodSettings escape internal '/' as '~1'
    // (e.g. '/~1quick-setup~1bundle'); unescape for comparison.
    const unescape = (p: string) => p.replace(/~1/g, '/').replace(/^\/+/, '/');
    const quickSetupSettings = settings.filter((s) =>
      unescape(s.ResourcePath ?? '').startsWith('/quick-setup/')
    );
    expect(
      quickSetupSettings
        .map((s) => `${s.HttpMethod} ${unescape(s.ResourcePath)}`)
        .sort()
    ).toEqual(
      [
        'GET /quick-setup/bootstrap',
        'POST /quick-setup/bundle',
        'POST /quick-setup/credentials',
        'POST /quick-setup/status',
      ].sort()
    );
    for (const s of quickSetupSettings) {
      expect(s.ThrottlingRateLimit).toBe(10);
      expect(s.ThrottlingBurstLimit).toBe(20);
    }
  });
});
