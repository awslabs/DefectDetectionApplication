/**
 * Static infrastructure assertions for the camera-registry-sync CDK
 * additions (camera-registry-sync task 4.2).
 *
 * Requirements covered:
 * - 1.1: the dda-portal-camera-registry table stores per-device Camera_Source
 *   items (PK device_id, item-type-prefixed SK) scoped to a Use_Case through
 *   the usecase-index GSI.
 * - 3.2: the edge->portal report path is wired end to end — the IoT topic
 *   rule in the use-case onboarding template forwards dda-camera-registry
 *   shadow documents events to the portal's dda-portal-camera-shadow-reports
 *   SQS queue (cross-account queue policy), which drives the
 *   CameraSyncHandler Lambda through an SQS event source; the
 *   CameraRegistryHandler serves the /devices/{id}/cameras route table.
 */
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';
import { UseCaseAccountStack } from '../lib/usecase-account-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';
const PORTAL_ACCOUNT = '222222222222';
const USECASE_ACCOUNT = '333333333333';
const REGION = 'us-east-1';

// Synthesized once: the ComputeStack stages Lambda/layer assets, which is
// expensive.
let storageTemplate: Template;
let computeTemplate: Template;
let cameraApiTemplate: Template;
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
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
  });

  // Concrete env so the rule's cross-account queue ARN/URL resolve to plain
  // strings (the onboarding stack derives them from region + portal account).
  const usecase = new UseCaseAccountStack(app, 'UseCase', {
    env: { account: USECASE_ACCOUNT, region: REGION },
    portalAccountId: PORTAL_ACCOUNT,
    externalId: 'test-external-id',
  });

  storageTemplate = Template.fromStack(storage);
  computeTemplate = Template.fromStack(compute);
  cameraApiTemplate = Template.fromStack(
    compute.node.findChild('CameraRegistryApi') as cdk.NestedStack
  );
  usecaseTemplate = Template.fromStack(usecase);
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

describe('camera registry table (Requirement 1.1)', () => {
  test('dda-portal-camera-registry schema: device_id PK, sk SK, on-demand, PITR, retained', () => {
    storageTemplate.hasResource('AWS::DynamoDB::Table', {
      DeletionPolicy: 'Retain',
      Properties: Match.objectLike({
        TableName: 'dda-portal-camera-registry',
        KeySchema: [
          { AttributeName: 'device_id', KeyType: 'HASH' },
          { AttributeName: 'sk', KeyType: 'RANGE' },
        ],
        BillingMode: 'PAY_PER_REQUEST',
        PointInTimeRecoverySpecification: {
          PointInTimeRecoveryEnabled: true,
        },
      }),
    });
  });

  test('usecase-index GSI partitions on usecase_id for Use_Case scoping', () => {
    const [, table] = findResource(
      storageTemplate,
      'AWS::DynamoDB::Table',
      (props) => props.TableName === 'dda-portal-camera-registry'
    );
    expect(table.Properties.GlobalSecondaryIndexes).toEqual([
      expect.objectContaining({
        IndexName: 'usecase-index',
        KeySchema: [{ AttributeName: 'usecase_id', KeyType: 'HASH' }],
      }),
    ]);
    expect(table.Properties.AttributeDefinitions).toEqual(
      expect.arrayContaining([
        { AttributeName: 'device_id', AttributeType: 'S' },
        { AttributeName: 'sk', AttributeType: 'S' },
        { AttributeName: 'usecase_id', AttributeType: 'S' },
      ])
    );
  });
});

describe('shadow-report queue and DLQ (Requirement 3.2)', () => {
  test('report queue redrives to the DLQ after 3 receives', () => {
    const [dlqId] = findResource(
      computeTemplate,
      'AWS::SQS::Queue',
      (props) => props.QueueName === 'dda-portal-camera-shadow-reports-dlq'
    );
    const [, queue] = findResource(
      computeTemplate,
      'AWS::SQS::Queue',
      (props) => props.QueueName === 'dda-portal-camera-shadow-reports'
    );
    expect(queue.Properties.RedrivePolicy).toEqual({
      deadLetterTargetArn: { 'Fn::GetAtt': [dlqId, 'Arn'] },
      maxReceiveCount: 3,
    });
    // Visibility timeout is a multiple of the 30 s consumer Lambda timeout.
    expect(queue.Properties.VisibilityTimeout).toBe(180);
  });

  test('DLQ retains dead-lettered reports for 14 days', () => {
    computeTemplate.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: 'dda-portal-camera-shadow-reports-dlq',
      MessageRetentionPeriod: 14 * 24 * 3600,
    });
  });

  test('cross-account queue policy admits SendMessage only from trusted use-case accounts and the portal account', () => {
    const [queueId] = findResource(
      computeTemplate,
      'AWS::SQS::Queue',
      (props) => props.QueueName === 'dda-portal-camera-shadow-reports'
    );
    const [, policy] = findResource(
      computeTemplate,
      'AWS::SQS::QueuePolicy',
      (props) =>
        JSON.stringify(props.Queues).includes(queueId) &&
        JSON.stringify(props.PolicyDocument).includes(
          'AllowUseCaseAccountIotRuleDelivery'
        )
    );
    const statement = policy.Properties.PolicyDocument.Statement.find(
      (s: any) => s.Sid === 'AllowUseCaseAccountIotRuleDelivery'
    );
    expect(statement).toMatchObject({
      Effect: 'Allow',
      Action: 'sqs:SendMessage',
      Condition: {
        StringEquals: {
          'aws:PrincipalAccount': [
            TRUSTED_USECASE_ACCOUNT,
            { Ref: 'AWS::AccountId' },
          ],
        },
      },
    });
    // The delivery grant is condition-scoped, not principal-scoped: the rule
    // role name in the use-case account is not knowable here.
    expect(statement.Principal).toEqual({ AWS: '*' });
  });
});

describe('camera sync and registry Lambdas (Requirement 3.2)', () => {
  test('CameraSyncHandler consumes the shadow-report queue with batch item failures reported', () => {
    const [handlerId, handler] = findResource(
      computeTemplate,
      'AWS::Lambda::Function',
      (props) => props.Handler === 'camera_sync.handler'
    );
    expect(handler.Properties.Runtime).toBe('python3.11');
    const [queueId] = findResource(
      computeTemplate,
      'AWS::SQS::Queue',
      (props) => props.QueueName === 'dda-portal-camera-shadow-reports'
    );
    computeTemplate.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      EventSourceArn: { 'Fn::GetAtt': [queueId, 'Arn'] },
      FunctionName: { Ref: handlerId },
      BatchSize: 10,
      FunctionResponseTypes: ['ReportBatchItemFailures'],
    });
  });

  test('CameraSyncHandler is wired to the registry table and the explicit DLQ dead-letter path', () => {
    const [, handler] = findResource(
      computeTemplate,
      'AWS::Lambda::Function',
      (props) => props.Handler === 'camera_sync.handler'
    );
    const env = handler.Properties.Environment.Variables;
    // Table name crosses stacks as an import; presence of the variable is
    // the wiring contract.
    expect(env.CAMERA_REGISTRY_TABLE).toBeDefined();
    expect(env.CAMERA_SHADOW_REPORT_DLQ_URL).toBeDefined();

    // The handler's role may SendMessage to the DLQ (explicit dead-lettering
    // of malformed reports without blocking the batch). The role carries so
    // many grants that CDK splits them into overflow managed policies, so
    // search inline and managed policies alike.
    const [dlqId] = findResource(
      computeTemplate,
      'AWS::SQS::Queue',
      (props) => props.QueueName === 'dda-portal-camera-shadow-reports-dlq'
    );
    const roleRef = handler.Properties.Role['Fn::GetAtt'][0];
    const policies = [
      ...Object.values(computeTemplate.findResources('AWS::IAM::Policy')),
      ...Object.values(
        computeTemplate.findResources('AWS::IAM::ManagedPolicy')
      ),
    ];
    const dlqSend = policies.some(
      (policy: any) =>
        (policy.Properties.Roles ?? []).some((r: any) => r.Ref === roleRef) &&
        policy.Properties.PolicyDocument.Statement.some(
          (s: any) =>
            JSON.stringify(s.Action).includes('sqs:SendMessage') &&
            JSON.stringify(s.Resource).includes(dlqId)
        )
    );
    expect(dlqSend).toBe(true);
  });

  test('CameraRegistryHandler exists for the cameras route table', () => {
    const [, handler] = findResource(
      computeTemplate,
      'AWS::Lambda::Function',
      (props) => props.Handler === 'camera_registry.handler'
    );
    expect(handler.Properties.Runtime).toBe('python3.11');
    expect(
      handler.Properties.Environment.Variables.CAMERA_REGISTRY_TABLE
    ).toBeDefined();
  });
});

describe('camera registry API routes (Requirements 1.1, 3.2)', () => {
  /** `METHOD /devices/{id}/...` for every non-OPTIONS method in the stack. */
  function routes(): string[] {
    const resources = cameraApiTemplate.findResources(
      'AWS::ApiGateway::Resource'
    );
    const pathOf = (logicalId: string): string => {
      const resource: any = resources[logicalId];
      const parentRef = resource.Properties.ParentId?.Ref;
      // A parent outside this template is the imported /devices/{id} resource.
      const prefix =
        parentRef && resources[parentRef]
          ? pathOf(parentRef)
          : '/devices/{id}';
      return `${prefix}/${resource.Properties.PathPart}`;
    };
    return Object.values(
      cameraApiTemplate.findResources('AWS::ApiGateway::Method')
    )
      .map((m: any) => m.Properties)
      .filter((props) => props.HttpMethod !== 'OPTIONS')
      .map((props) => `${props.HttpMethod} ${pathOf(props.ResourceId.Ref)}`)
      .sort();
  }

  test('exactly the seven Camera_Registry routes are registered', () => {
    expect(routes()).toEqual(
      [
        'GET /devices/{id}/cameras',
        'POST /devices/{id}/cameras',
        'PUT /devices/{id}/cameras/{csid}',
        'DELETE /devices/{id}/cameras/{csid}',
        'GET /devices/{id}/cameras/conflicts',
        'POST /devices/{id}/cameras/conflicts/{cid}/reapply',
        'POST /devices/{id}/cameras/refresh',
      ].sort()
    );
  });

  test('every route requires the Cognito authorizer', () => {
    cameraApiTemplate.hasResourceProperties('AWS::ApiGateway::Authorizer', {
      Type: 'COGNITO_USER_POOLS',
      Name: 'EdgeCVPortalCameraRegistryAuthorizer',
      IdentitySource: 'method.request.header.Authorization',
    });
    const methods = Object.values(
      cameraApiTemplate.findResources('AWS::ApiGateway::Method')
    )
      .map((m: any) => m.Properties)
      .filter((props) => props.HttpMethod !== 'OPTIONS');
    expect(methods.length).toBe(7);
    for (const props of methods) {
      expect(props.AuthorizationType).toBe('COGNITO_USER_POOLS');
      expect(props.AuthorizerId).toBeDefined();
      // Lambda proxy integration into the CameraRegistryHandler (imported
      // into the nested stack as a parameter reference).
      expect(props.Integration.Type).toBe('AWS_PROXY');
    }
  });

  test('a route-salted deployment re-points the v1 stage', () => {
    const deployments = cameraApiTemplate.findResources(
      'AWS::ApiGateway::Deployment'
    );
    const ids = Object.keys(deployments);
    expect(ids).toHaveLength(1);
    // Logical id is salted with the route table so route changes roll a new
    // deployment.
    expect(ids[0]).toMatch(/^CameraRegistryDeployment[0-9a-f]{16}$/);
    expect((deployments[ids[0]] as any).Properties.StageName).toBe('v1');
  });
});

describe('use-case onboarding IoT rule (Requirement 3.2)', () => {
  const queueArn = `arn:aws:sqs:${REGION}:${PORTAL_ACCOUNT}:dda-portal-camera-shadow-reports`;
  const queueUrl = `https://sqs.${REGION}.amazonaws.com/${PORTAL_ACCOUNT}/dda-portal-camera-shadow-reports`;

  test('dda_camera_registry_shadow_documents forwards shadow documents events to the portal queue', () => {
    const [, rule] = findResource(
      usecaseTemplate,
      'AWS::IoT::TopicRule',
      (props) => props.RuleName === 'dda_camera_registry_shadow_documents'
    );
    const payload = rule.Properties.TopicRulePayload;
    expect(payload.Sql).toBe(
      "SELECT *, topic(3) AS thing_name FROM '$aws/things/+/shadow/name/dda-camera-registry/update/documents'"
    );
    expect(payload.AwsIotSqlVersion).toBe('2016-03-23');
    expect(payload.RuleDisabled).toBe(false);
    expect(payload.Actions).toHaveLength(1);
    expect(payload.Actions[0].Sqs).toMatchObject({
      QueueUrl: queueUrl,
      UseBase64: false,
    });
    // Delivery runs under the rule role defined in the same template.
    expect(
      payload.Actions[0].Sqs.RoleArn['Fn::GetAtt'][0]
    ).toMatch(/^CameraShadowRuleRole/);
  });

  test('rule role is assumable by IoT and scoped to SendMessage on the portal queue', () => {
    usecaseTemplate.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'DDACameraShadowRuleRole',
      AssumeRolePolicyDocument: Match.objectLike({
        Statement: [
          Match.objectLike({
            Action: 'sts:AssumeRole',
            Effect: 'Allow',
            Principal: { Service: 'iot.amazonaws.com' },
          }),
        ],
      }),
    });
    usecaseTemplate.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'SendCameraShadowReports',
            Effect: 'Allow',
            Action: 'sqs:SendMessage',
            Resource: queueArn,
          }),
        ]),
      }),
    });
  });
});
