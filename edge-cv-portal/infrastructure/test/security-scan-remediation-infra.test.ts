/**
 * security-scan-remediation-high: R14 and R15 infrastructure invariants.
 *
 * One synth per stack serves every case in this file: the R14 SNS cases
 * (task 6) and the R14 SQS, R14 DynamoDB and R15 cases that tasks 7 to 9
 * add. StorageStack and ComputeStack are synthesized as in
 * user-admin-audit-grant.test.ts, and a cross-account UseCaseAccountStack as
 * in camera-shadow-sync-provisioning.test.ts.
 *
 * R14 SNS (design r14-sns-encryption): the training-alerts topic is
 * encrypted with the AWS managed SNS key. That key admits only principals of
 * this account that call through SNS, so these cases also pin that nothing
 * in the stack makes an AWS service a publisher of the topic (Requirement
 * 14.2).
 *
 * R14 SQS (design r14-sqs-encryption): the camera-shadow and account-sync-ack
 * pairs take SSE-SQS, because use-case accounts may send to them, and the
 * auto-label pair takes the AWS managed SQS key. That key admits only
 * principals of this account, so these cases also pin that every sender of
 * the auto-label pair is in this account (Requirements 14.3, 14.4).
 *
 * R14 DynamoDB (design r14-dynamodb-cmk): one retained customer managed key
 * with rotation serves both portal-account tables, and every principal of
 * the tables holds key access from a policy both tables depend on, so
 * CloudFormation attaches it before either table switches to the key
 * (Requirement 14.5). Names, point-in-time recovery and Retain stay
 * (Requirement 16.2).
 *
 * R15 (design r15-least-privilege-iam): the five flagged statements are
 * split. Only actions without resource-level permissions stay on '*', every
 * other action moves to the ARNs it uses at runtime, and every action each
 * role held stays granted (Requirements 15.1, 15.2, 15.4). The cases read a
 * role's grants across all of its policy carriers, so ComputeStack's policy
 * minimization doesn't affect them.
 *
 * Requirements covered: 14.1, 14.2, 14.3, 14.4, 14.5, 15.1, 15.2, 15.4, 16.1,
 * 16.2, 17.2.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';
import { UseCaseAccountStack } from '../lib/usecase-account-stack';
import { isDeepStrictEqual } from 'util';

const TRUSTED_USECASE_ACCOUNT = '111111111111';
const PORTAL_ACCOUNT = '222222222222';
const USECASE_ACCOUNT = '333333333333';
const REGION = 'us-east-1';

// Synthesized once: the ComputeStack stages Lambda/layer assets, which is
// expensive. nestedComputeTemplates holds ComputeStack's nested stacks,
// which deploy with it.
let computeTemplate: Template;
let nestedComputeTemplates: Template[];
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

  // Concrete cross-account env, mirroring camera-shadow-sync-provisioning.test.ts.
  const usecase = new UseCaseAccountStack(app, 'UseCase', {
    env: { account: USECASE_ACCOUNT, region: REGION },
    portalAccountId: PORTAL_ACCOUNT,
    externalId: 'test-external-id',
  });

  computeTemplate = Template.fromStack(compute);
  nestedComputeTemplates = compute.node
    .findAll()
    .filter((construct): construct is cdk.NestedStack =>
      cdk.NestedStack.isNestedStack(construct)
    )
    .map((nested) => Template.fromStack(nested));
  usecaseTemplate = Template.fromStack(usecase);
}, 300_000);

// ---------------------------------------------------------------------------
// R14 SNS: the training-alerts topic (design r14-sns-encryption, task 6)
// ---------------------------------------------------------------------------

const TRAINING_ALERTS_TOPIC = 'dda-portal-training-alerts';
const SERVICE_PUBLISHER_NOTE =
  'a reference outside a Lambda environment, an IAM policy document or the ' +
  'TrainingAlertTopicArn output can make an AWS service a publisher, and an ' +
  'AWS service publisher needs the customer managed key of design ' +
  'r14-sns-encryption, not alias/aws/sns (Requirement 14.2)';
const IAM_STATEMENT_TYPES = ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy', 'AWS::IAM::Role'];

/** Logical id of the training-alerts topic, found by its name. */
function trainingAlertTopicId(): string {
  const ids = Object.keys(
    computeTemplate.findResources('AWS::SNS::Topic', {
      Properties: { TopicName: TRAINING_ALERTS_TOPIC },
    })
  );
  expect(ids).toHaveLength(1);
  return ids[0];
}

/**
 * Template paths of every reference to `logicalId` under `node`: a Ref, an
 * Fn::GetAtt, or an Fn::Sub string naming it. DependsOn entries are plain
 * ids, not references, and aren't counted.
 */
function referencePaths(node: unknown, logicalId: string, path: string[] = []): string[][] {
  if (typeof node === 'string') {
    const named = node.includes('${' + logicalId + '}') || node.includes('${' + logicalId + '.');
    return named ? [path] : [];
  }
  if (Array.isArray(node)) {
    return node.flatMap((child, index) => referencePaths(child, logicalId, [...path, String(index)]));
  }
  if (node === null || typeof node !== 'object') {
    return [];
  }
  const record = node as Record<string, unknown>;
  const getAtt = record['Fn::GetAtt'];
  if (record.Ref === logicalId || (Array.isArray(getAtt) && getAtt[0] === logicalId)) {
    return [path];
  }
  return Object.entries(record).flatMap(([key, child]) =>
    referencePaths(child, logicalId, [...path, key])
  );
}

/**
 * True for the places a topic reference may sit: a Lambda function's
 * environment, an IAM policy document, or the TrainingAlertTopicArn output.
 */
function allowedTopicReference(template: any, path: string[]): boolean {
  const [section, logicalId, ...rest] = path;
  const under = (...prefix: string[]): boolean => prefix.every((key, index) => rest[index] === key);
  if (section === 'Outputs') {
    return logicalId === 'TrainingAlertTopicArn';
  }
  const type = section === 'Resources' ? template.Resources[logicalId]?.Type : undefined;
  if (type === 'AWS::Lambda::Function') {
    return under('Properties', 'Environment');
  }
  if (type === 'AWS::IAM::Policy' || type === 'AWS::IAM::ManagedPolicy') {
    return under('Properties', 'PolicyDocument');
  }
  return false;
}

describe('R14 SNS: training-alerts topic encryption (Requirements 14.1, 14.2)', () => {
  test('the topic uses alias/aws/sns and keeps its logical id, name and display name', () => {
    expect(trainingAlertTopicId()).toBe('TrainingAlertTopic5C2CFA97');
    const topics = computeTemplate.findResources('AWS::SNS::Topic', {
      Properties: { TopicName: TRAINING_ALERTS_TOPIC },
    });
    expect(Object.values(topics).map((topic: any) => topic.Properties)).toEqual([
      {
        DisplayName: 'DDA Portal Training Alerts',
        KmsMasterKeyId: 'alias/aws/sns',
        TopicName: TRAINING_ALERTS_TOPIC,
      },
    ]);
  });

  test('every Ref to the topic sits in a Lambda environment, an IAM policy document or the stack output', () => {
    const template = computeTemplate.toJSON();
    const paths = referencePaths(template, trainingAlertTopicId());
    const sites = paths.map((path) => path.join('.'));
    // The walk reaches the known sites, so an empty result can't pass.
    expect(sites).toContain('Outputs.TrainingAlertTopicArn.Value');
    expect(sites.some((site) => site.endsWith('.Environment.Variables.ALERT_TOPIC_ARN'))).toBe(true);
    expect(sites.some((site) => site.includes('.Properties.PolicyDocument.'))).toBe(true);
    const unexpected = paths
      .filter((path) => !allowedTopicReference(template, path))
      .map((path) => `${path.join('.')}: ${SERVICE_PUBLISHER_NOTE}`);
    expect(unexpected).toEqual([]);
  });

  test('no IAM statement in the stack or its nested stacks names alias/aws/sns', () => {
    // The AWS managed key needs no KMS grant, and topic.masterKey stays
    // unset, so grantPublish adds no KMS statement.
    const templates = [computeTemplate, ...nestedComputeTemplates];
    expect(nestedComputeTemplates.length).toBeGreaterThan(0);
    const naming = templates.flatMap((template) =>
      Object.entries(template.toJSON().Resources ?? {})
        .filter(([, resource]: [string, any]) => IAM_STATEMENT_TYPES.includes(resource.Type))
        .filter(([, resource]: [string, any]) =>
          JSON.stringify(resource.Properties ?? {}).includes('alias/aws/sns')
        )
        .map(([logicalId]) => logicalId)
    );
    expect(naming).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// R14 SQS: the six flagged queues (design r14-sqs-encryption, task 7)
// ---------------------------------------------------------------------------

type QueueEncryptionProperty = 'SqsManagedSseEnabled' | 'KmsMasterKeyId';
const ENCRYPTION_VALUES: Record<QueueEncryptionProperty, unknown> = {
  SqsManagedSseEnabled: true,
  KmsMasterKeyId: 'alias/aws/sqs',
};
const DLQ_RETENTION_SECONDS = 14 * 24 * 3600;
const QUEUE_RETENTION_SECONDS = 4 * 24 * 3600;
const SAME_ACCOUNT_SENDER_NOTE =
  'the auto-label queues use alias/aws/sqs, which admits only principals of ' +
  'this account; a sender in another account, or an AWS service sender, ' +
  'needs SSE-SQS or a customer managed key (design r14-sqs-encryption, ' +
  'Requirements 14.3 and 14.4)';
const SERVICE_SENDER_TYPES = ['AWS::IoT::TopicRule', 'AWS::Events::Rule', 'AWS::SNS::Subscription'];

/** A flagged queue: its encryption property and the properties it keeps from 4a3f960. */
interface FlaggedQueue {
  queueName: string;
  logicalId: string;
  encryption: QueueEncryptionProperty;
  unchanged: Record<string, unknown>;
}

function redriveTo(dlqLogicalId: string): Record<string, unknown> {
  return { deadLetterTargetArn: { 'Fn::GetAtt': [dlqLogicalId, 'Arn'] }, maxReceiveCount: 3 };
}

const FLAGGED_QUEUES: FlaggedQueue[] = [
  {
    queueName: 'dda-portal-camera-shadow-reports-dlq',
    logicalId: 'CameraShadowReportDLQ50DB798A',
    encryption: 'SqsManagedSseEnabled',
    unchanged: { MessageRetentionPeriod: DLQ_RETENTION_SECONDS },
  },
  {
    queueName: 'dda-portal-camera-shadow-reports',
    logicalId: 'CameraShadowReportQueue78573A06',
    encryption: 'SqsManagedSseEnabled',
    unchanged: {
      MessageRetentionPeriod: QUEUE_RETENTION_SECONDS,
      VisibilityTimeout: 180,
      RedrivePolicy: redriveTo('CameraShadowReportDLQ50DB798A'),
    },
  },
  {
    queueName: 'dda-portal-account-sync-acks-dlq',
    logicalId: 'AccountSyncAckDLQ75DF4763',
    encryption: 'SqsManagedSseEnabled',
    unchanged: { MessageRetentionPeriod: DLQ_RETENTION_SECONDS },
  },
  {
    queueName: 'dda-portal-account-sync-acks',
    logicalId: 'AccountSyncAckQueue772F11D7',
    encryption: 'SqsManagedSseEnabled',
    unchanged: {
      MessageRetentionPeriod: QUEUE_RETENTION_SECONDS,
      VisibilityTimeout: 180,
      RedrivePolicy: redriveTo('AccountSyncAckDLQ75DF4763'),
    },
  },
  {
    queueName: 'dda-portal-autolabel-queue-dlq',
    logicalId: 'DdaAutolabelDLQB2573578',
    encryption: 'KmsMasterKeyId',
    unchanged: { MessageRetentionPeriod: DLQ_RETENTION_SECONDS },
  },
  {
    queueName: 'dda-portal-autolabel-queue',
    logicalId: 'DdaAutolabelQueue5780A6F7',
    encryption: 'KmsMasterKeyId',
    unchanged: {
      MessageRetentionPeriod: QUEUE_RETENTION_SECONDS,
      VisibilityTimeout: 300,
      RedrivePolicy: redriveTo('DdaAutolabelDLQB2573578'),
    },
  },
];
const AUTOLABEL_QUEUES = FLAGGED_QUEUES.filter((queue) => queue.encryption === 'KmsMasterKeyId');

// Sites the walk must reach for each auto-label queue, so an empty walk
// can't pass (suffixes of the dotted template path).
const KNOWN_AUTOLABEL_SITES: Record<string, string[]> = {
  DdaAutolabelDLQB2573578: [
    '.Properties.Queues.0',
    'DdaAutolabelQueue5780A6F7.Properties.RedrivePolicy.deadLetterTargetArn',
  ],
  DdaAutolabelQueue5780A6F7: [
    '.Properties.Queues.0',
    '.Properties.EventSourceArn',
    '.Environment.Variables.AUTOLABEL_QUEUE_URL',
  ],
};

/**
 * True for the places a reference to an auto-label queue may sit, all of
 * them used by principals of this account: its queue policy, the source
 * queue's redrive policy, a Lambda function's environment, an IAM policy
 * document, or a Lambda event source mapping.
 */
function allowedAutolabelQueueReference(template: any, path: string[]): boolean {
  const [section, logicalId, ...rest] = path;
  if (section !== 'Resources') {
    return false;
  }
  const under = (...prefix: string[]): boolean => prefix.every((key, index) => rest[index] === key);
  switch (template.Resources[logicalId]?.Type) {
    case 'AWS::SQS::QueuePolicy':
      return under('Properties', 'Queues') || under('Properties', 'PolicyDocument');
    case 'AWS::SQS::Queue':
      return under('Properties', 'RedrivePolicy');
    case 'AWS::Lambda::Function':
      return under('Properties', 'Environment');
    case 'AWS::IAM::Policy':
    case 'AWS::IAM::ManagedPolicy':
      return under('Properties', 'PolicyDocument');
    case 'AWS::Lambda::EventSourceMapping':
      return under('Properties', 'EventSourceArn');
    default:
      return false;
  }
}

/** Logical ids of the IAM resources in ComputeStack or its nested stacks whose properties name `text`. */
function iamResourcesNaming(text: string): string[] {
  return [computeTemplate, ...nestedComputeTemplates].flatMap((template) =>
    Object.entries(template.toJSON().Resources ?? {})
      .filter(([, resource]: [string, any]) => IAM_STATEMENT_TYPES.includes(resource.Type))
      .filter(([, resource]: [string, any]) => JSON.stringify(resource.Properties ?? {}).includes(text))
      .map(([logicalId]) => logicalId)
  );
}

describe('R14 SQS: queue encryption at rest (Requirements 14.3, 14.4)', () => {
  test.each(FLAGGED_QUEUES)(
    '$queueName has $encryption, not the other property, and keeps its other properties',
    ({ queueName, logicalId, encryption, unchanged }) => {
      const queues = computeTemplate.findResources('AWS::SQS::Queue', {
        Properties: { QueueName: queueName },
      });
      expect(Object.keys(queues)).toEqual([logicalId]);
      const properties = queues[logicalId].Properties;
      const other: QueueEncryptionProperty =
        encryption === 'KmsMasterKeyId' ? 'SqsManagedSseEnabled' : 'KmsMasterKeyId';
      expect(properties[encryption]).toEqual(ENCRYPTION_VALUES[encryption]);
      expect(properties).not.toHaveProperty(other);
      // The whole Properties object, so nothing else on the queue moved.
      expect(properties).toEqual({
        QueueName: queueName,
        ...unchanged,
        [encryption]: ENCRYPTION_VALUES[encryption],
      });
    }
  );

  test.each(AUTOLABEL_QUEUES)('$queueName: its queue policy holds only the enforceSSL deny', ({ logicalId }) => {
    const policies = Object.values(computeTemplate.findResources('AWS::SQS::QueuePolicy')).filter(
      (policy: any) => (policy.Properties.Queues ?? []).some((queue: any) => queue?.Ref === logicalId)
    );
    expect(policies).toHaveLength(1);
    const enforceSslDeny = {
      Action: 'sqs:*',
      Condition: { Bool: { 'aws:SecureTransport': 'false' } },
      Effect: 'Deny',
      Principal: { AWS: '*' },
      Resource: { 'Fn::GetAtt': [logicalId, 'Arn'] },
    };
    const statements: unknown[] = (policies[0] as any).Properties.PolicyDocument.Statement;
    const findings = statements.map((statement) =>
      isDeepStrictEqual(statement, enforceSslDeny)
        ? 'enforceSSL deny'
        : `${JSON.stringify(statement)}: ${SAME_ACCOUNT_SENDER_NOTE}`
    );
    expect(findings).toEqual(['enforceSSL deny']);
  });

  test.each(AUTOLABEL_QUEUES)(
    '$queueName: no IoT rule, events rule or SNS subscription references it, only same-account sites',
    ({ logicalId }) => {
      const template = computeTemplate.toJSON();
      const paths = referencePaths(template, logicalId);
      const sites = paths.map((path) => path.join('.'));
      // The walk reaches the known sites, so an empty result can't pass.
      for (const known of KNOWN_AUTOLABEL_SITES[logicalId]) {
        expect(sites.filter((site) => site.endsWith(known))).not.toHaveLength(0);
      }
      const serviceSenders = paths
        .filter(([section]) => section === 'Resources')
        .filter(([, id]) => SERVICE_SENDER_TYPES.includes(template.Resources[id]?.Type))
        .map((path) => `${path.join('.')}: ${SAME_ACCOUNT_SENDER_NOTE}`);
      expect(serviceSenders).toEqual([]);
      const unexpected = paths
        .filter((path) => !allowedAutolabelQueueReference(template, path))
        .map((path) => `${path.join('.')}: ${SAME_ACCOUNT_SENDER_NOTE}`);
      expect(unexpected).toEqual([]);
    }
  );

  test('no IAM statement in the stack or its nested stacks names alias/aws/sqs', () => {
    // The AWS managed key needs no KMS grant, and CDK adds none for it, so
    // grantSendMessages and the SqsEventSource add no KMS statement.
    expect(nestedComputeTemplates.length).toBeGreaterThan(0);
    expect(iamResourcesNaming('alias/aws/sqs')).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// R15: least-privilege IAM (design r15-least-privilege-iam, task 9)
// ---------------------------------------------------------------------------

type Grants = Record<string, string[]>;

// ComputeStack renders cdk.Aws.ACCOUNT_ID as this Ref (renderValue below).
const ACCOUNT_REF = '${AWS::AccountId}';
const iotArn = (resource: string): string => 'arn:aws:iot:*:' + ACCOUNT_REF + ':' + resource;
const STAR = ['*'];
const SETUP_STATION = path.join(__dirname, '..', '..', '..', 'station_install', 'setup_station.sh');

const asArray = (value: any): any[] => (Array.isArray(value) ? value : [value]);

/** A resource value as text: Fn::Join joined, Ref as ${Name}, Fn::GetAtt as ${Id.Attr}. */
function renderValue(value: any): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    if ('Ref' in value) return '${' + value.Ref + '}';
    if ('Fn::GetAtt' in value) return '${' + asArray(value['Fn::GetAtt']).join('.') + '}';
    if ('Fn::Join' in value) {
      const [separator, parts] = value['Fn::Join'];
      return parts.map(renderValue).join(separator);
    }
  }
  return JSON.stringify(value);
}

/** The logical id of the one AWS::IAM::Role whose id is `prefix` plus CDK's hash. */
function roleId(template: any, prefix: string): string {
  const pattern = new RegExp(`^${prefix}[0-9A-F]{8}$`);
  const ids = Object.keys(template.Resources).filter(
    (id) => template.Resources[id].Type === 'AWS::IAM::Role' && pattern.test(id)
  );
  expect(ids).toHaveLength(1);
  return ids[0];
}

/**
 * Every policy document that grants `role` its permissions: its inline
 * Policies, each AWS::IAM::Policy and AWS::IAM::ManagedPolicy that lists it
 * in Roles (the default policy and ComputeStack's overflow policies), and
 * each managed policy of the template that its ManagedPolicyArns names.
 */
function roleDocuments(template: any, role: string): any[] {
  const properties = template.Resources[role].Properties ?? {};
  const documents: any[] = (properties.Policies ?? []).map((policy: any) => policy.PolicyDocument);
  const managed = new Set(
    asArray(properties.ManagedPolicyArns ?? []).filter((arn) => arn && arn.Ref).map((arn) => arn.Ref)
  );
  for (const [id, resource] of Object.entries<any>(template.Resources)) {
    if (resource.Type !== 'AWS::IAM::Policy' && resource.Type !== 'AWS::IAM::ManagedPolicy') continue;
    const attached = asArray(resource.Properties.Roles ?? []).some((ref) => ref && ref.Ref === role);
    if (attached || managed.has(id)) documents.push(resource.Properties.PolicyDocument);
  }
  return documents;
}

/**
 * Maps each action `role` is granted to the sorted resources it is granted
 * on, across all of the role's policy documents, one action and one resource
 * at a time, so how ComputeStack's policy minimization groups or spreads the
 * statements doesn't matter. A conditioned grant keeps its condition
 * ("<resource> when <condition JSON>"), and a Deny is keyed "Deny <action>".
 * Task 8's invariant (8.4) reuses it.
 */
function roleGrants(template: any, role: string): Grants {
  const grants: Record<string, Set<string>> = {};
  for (const document of roleDocuments(template, role)) {
    for (const statement of document.Statement) {
      if ('NotAction' in statement || 'NotResource' in statement) {
        throw new Error(`${role}: roleGrants doesn't read NotAction or NotResource`);
      }
      const condition = statement.Condition ? ' when ' + JSON.stringify(statement.Condition) : '';
      for (const action of asArray(statement.Action)) {
        const key = statement.Effect === 'Allow' ? action : `${statement.Effect} ${action}`;
        if (!grants[key]) grants[key] = new Set<string>();
        for (const resource of asArray(statement.Resource)) grants[key].add(renderValue(resource) + condition);
      }
    }
  }
  return Object.fromEntries(Object.keys(grants).sort().map((action) => [action, [...grants[action]].sort()]));
}

const pick = (grants: Grants, actions: string[]): Grants =>
  Object.fromEntries(actions.map((action) => [action, grants[action]]));
const sortedGrants = (expected: Grants): Grants =>
  Object.fromEntries(Object.entries(expected).map(([action, resources]) => [action, [...resources].sort()]));

// R15 site 1: StationProvisioningRole's 11 provisioning actions, each on
// exactly the resources of the site 1 code.
const THING_POLICY = iotArn('policy/GreengrassV2IoTThingPolicy');
const TES_CERTIFICATE_POLICIES = iotArn('policy/GreengrassTESCertificatePolicy*');
const TES_ROLE_ALIAS = iotArn('rolealias/GreengrassCoreTokenExchangeRoleAlias');
const STATION_PROVISIONING_ACTIONS: Grants = {
  'iot:CreateKeysAndCertificate': STAR,
  'iot:AttachThingPrincipal': STAR,
  'iot:DescribeEndpoint': STAR,
  'iot:GetPolicy': [THING_POLICY, TES_CERTIFICATE_POLICIES],
  'iot:CreatePolicy': [THING_POLICY, TES_CERTIFICATE_POLICIES],
  'iot:ListPolicyVersions': [THING_POLICY],
  'iot:CreatePolicyVersion': [THING_POLICY],
  'iot:DeletePolicyVersion': [THING_POLICY],
  'iot:AttachPolicy': [iotArn('cert/*')],
  'iot:CreateRoleAlias': [TES_ROLE_ALIAS],
  'iot:DescribeRoleAlias': [TES_ROLE_ALIAS],
};
// The role's other grants, which R15 doesn't change: the thing statement,
// the two TES statements, the core-device tag and the caller-identity check.
const THING_RESOURCES = [iotArn('thing/*'), iotArn('thinggroup/*')];
const TES_ROLE = ['arn:aws:iam::' + ACCOUNT_REF + ':role/GreengrassV2TokenExchangeRole*'];
const TES_POLICY = ['arn:aws:iam::' + ACCOUNT_REF + ':policy/GreengrassV2TokenExchangeRoleAccess*'];
const STATION_PROVISIONING_OTHER_GRANTS: Grants = {
  ...Object.fromEntries(
    ['iot:CreateThing', 'iot:DescribeThing', 'iot:CreateThingGroup', 'iot:DescribeThingGroup',
      'iot:AddThingToThingGroup', 'iot:ListThingGroupsForThing'].map((action) => [action, THING_RESOURCES])
  ),
  ...Object.fromEntries(
    ['iam:GetRole', 'iam:CreateRole', 'iam:AttachRolePolicy', 'iam:PutRolePolicy', 'iam:PassRole'].map(
      (action) => [action, TES_ROLE]
    )
  ),
  'iam:CreatePolicy': TES_POLICY,
  'iam:GetPolicy': TES_POLICY,
  'greengrass:TagResource': ['arn:aws:greengrass:*:' + ACCOUNT_REF + ':coreDevices:*'],
  'sts:GetCallerIdentity': STAR,
};

describe('R15: least-privilege IAM (Requirements 15.1, 15.2, 15.4)', () => {
  test('StationProvisioningRole keeps its 11 provisioning actions, each on exactly the site 1 resources', () => {
    const template = computeTemplate.toJSON();
    const grants = roleGrants(template, roleId(template, 'StationProvisioningRole'));
    expect(pick(grants, Object.keys(STATION_PROVISIONING_ACTIONS))).toEqual(
      sortedGrants(STATION_PROVISIONING_ACTIONS)
    );
    // Only the three actions without resource-level permissions stay on '*'.
    expect(Object.keys(grants).filter((action) => action.startsWith('iot:') && grants[action].includes('*'))).toEqual(
      ['iot:AttachThingPrincipal', 'iot:CreateKeysAndCertificate', 'iot:DescribeEndpoint']
    );
  });

  test('StationProvisioningRole: the thing, TES, tag and caller-identity grants are unchanged, and nothing else is granted', () => {
    const template = computeTemplate.toJSON();
    expect(roleGrants(template, roleId(template, 'StationProvisioningRole'))).toEqual(
      sortedGrants({ ...STATION_PROVISIONING_ACTIONS, ...STATION_PROVISIONING_OTHER_GRANTS })
    );
  });

  test('the site 1 policy and role-alias ARNs name what setup_station.sh passes to the installer', () => {
    const script = fs.readFileSync(SETUP_STATION, 'utf8');
    const installer = script.split('\n').filter((line) => line.includes('Greengrass.jar') && line.includes('--provision true'));
    expect(installer).toHaveLength(1);
    const thingPolicy = installer[0].match(/--thing-policy-name (\S+)/)?.[1];
    const roleAlias = installer[0].match(/--tes-role-alias-name (\S+)/)?.[1];
    const repairedPolicy = script.match(/^gg_thing_policy="([^"]+)"$/m)?.[1];
    expect([thingPolicy, roleAlias, repairedPolicy]).toEqual([
      'GreengrassV2IoTThingPolicy',
      'GreengrassCoreTokenExchangeRoleAlias',
      'GreengrassV2IoTThingPolicy',
    ]);

    const template = computeTemplate.toJSON();
    const grants = roleGrants(template, roleId(template, 'StationProvisioningRole'));
    expect(grants['iot:CreatePolicy']).toContain(iotArn(`policy/${thingPolicy}`));
    for (const action of ['iot:ListPolicyVersions', 'iot:CreatePolicyVersion', 'iot:DeletePolicyVersion']) {
      expect(grants[action]).toEqual([iotArn(`policy/${repairedPolicy}`)]);
    }
    expect(grants['iot:CreateRoleAlias']).toEqual([iotArn(`rolealias/${roleAlias}`)]);
    // The installer names its TES certificate policy with this prefix followed
    // by the role-alias name, which the policy pattern has to cover.
    const tesPattern = grants['iot:CreatePolicy'].find((resource) => resource.endsWith('*'));
    expect(tesPattern).toBe(TES_CERTIFICATE_POLICIES);
    expect(iotArn(`policy/GreengrassTESCertificatePolicy${roleAlias}`).startsWith(tesPattern!.slice(0, -1))).toBe(true);
  });

  test('DevicesRole: OpenTunnel, ListTunnels and ListTagsForResource on *, the other tunnel actions on tunnel/* only', () => {
    const template = computeTemplate.toJSON();
    const grants = roleGrants(template, roleId(template, 'DevicesRole'));
    const tunnels = [iotArn('tunnel/*')];
    expect(
      pick(grants, ['iot:OpenTunnel', 'iot:ListTunnels', 'iot:ListTagsForResource',
        'iot:CloseTunnel', 'iot:DescribeTunnel', 'iot:RotateTunnelAccessToken'])
    ).toEqual({
      'iot:OpenTunnel': STAR,
      'iot:ListTunnels': STAR,
      'iot:ListTagsForResource': STAR,
      'iot:CloseTunnel': tunnels,
      'iot:DescribeTunnel': tunnels,
      'iot:RotateTunnelAccessToken': tunnels,
    });
  });

  test('the SageMaker EventBridge enabler: rule actions on its one rule, ListRules on *, and the rule name in its code', () => {
    const template = computeTemplate.toJSON();
    const role = roleId(template, 'EnableSageMakerEventBridgeServiceRole');
    const rule = 'arn:aws:events:${AWS::Region}:' + ACCOUNT_REF + ':rule/sagemaker-eventbridge-enabler';
    expect(roleGrants(template, role)).toEqual({
      'events:DeleteRule': [rule],
      'events:DescribeRule': [rule],
      'events:ListRules': STAR,
      'events:PutRule': [rule],
    });
    const functions = Object.entries<any>(template.Resources).filter(
      ([id, resource]) => resource.Type === 'AWS::Lambda::Function' && /^EnableSageMakerEventBridge[0-9A-F]{8}$/.test(id)
    );
    expect(functions).toHaveLength(1);
    const [, enabler] = functions[0];
    expect(enabler.Properties.Role).toEqual({ 'Fn::GetAtt': [role, 'Arn'] });
    const code: string = enabler.Properties.Code.ZipFile;
    expect(code.split("rule_name = 'sagemaker-eventbridge-enabler'").length - 1).toBe(2);
  });

  test('UseCaseAccountStack DDASageMakerExecutionRole: each site 4 action on exactly the site 4 resources', () => {
    const template = usecaseTemplate.toJSON();
    const grants = roleGrants(template, roleId(template, 'DDASageMakerExecutionRole'));
    const logGroups = [`arn:aws:logs:*:${USECASE_ACCOUNT}:log-group:/aws/sagemaker/*`];
    const jobsAndModels = ['training-job/*', 'compilation-job/*', 'labeling-job/*', 'model/*'].map(
      (resource) => `arn:aws:sagemaker:*:${USECASE_ACCOUNT}:${resource}`
    );
    const expected: Grants = { 'cloudwatch:PutMetricData': STAR };
    for (const action of ['CreateLogGroup', 'CreateLogStream', 'PutLogEvents', 'DescribeLogStreams']) {
      expected[`logs:${action}`] = logGroups;
    }
    for (const action of ['CreateTrainingJob', 'DescribeTrainingJob', 'StopTrainingJob', 'CreateCompilationJob',
      'DescribeCompilationJob', 'StopCompilationJob', 'DescribeLabelingJob', 'CreateModel', 'DescribeModel',
      'DeleteModel']) {
      expected[`sagemaker:${action}`] = jobsAndModels;
    }
    for (const action of ['ListTrainingJobs', 'ListCompilationJobs', 'ListLabelingJobs', 'ListModels']) {
      expected[`sagemaker:${action}`] = STAR;
    }
    // Every cloudwatch, logs and SageMaker grant of the role is a site 4 grant.
    expect(pick(grants, Object.keys(grants).filter((action) => /^(cloudwatch|logs|sagemaker):/.test(action)))).toEqual(
      sortedGrants(expected)
    );
  });
});

// ---------------------------------------------------------------------------
// R14 DynamoDB: the account tables (design r14-dynamodb-cmk, task 8)
// ---------------------------------------------------------------------------

// The two portal-account tables: CDK's logical id prefix, name and partition key.
const ACCOUNT_TABLES = [
  { prefix: 'EdgeCredentialsTable', name: 'dda-portal-edge-credentials', partitionKey: 'username' },
  { prefix: 'AccountSyncTable', name: 'dda-portal-account-sync', partitionKey: 'device_id' },
];
// Every principal of the tables (design "R14 DynamoDB current behavior").
const ACCOUNT_TABLE_PRINCIPALS = ['AccountSyncRole', 'DevicesRole', 'UserAdminRole'];
const ACCOUNT_TABLES_KEY_ACTIONS = ['kms:Decrypt', 'kms:DescribeKey', 'kms:Encrypt', 'kms:GenerateDataKey*',
  'kms:ReEncrypt*'];
// Task 6.3's topic key, the one other key this spec may add (owner-gated section 1).
const TOPIC_KEY = /^TrainingAlertTopicKey[0-9A-F]{8}$/;

/** The logical id of the one resource of `type` whose id is `prefix` plus CDK's hash. */
function logicalIdOf(template: any, type: string, prefix: string): string {
  const pattern = new RegExp(`^${prefix}[0-9A-F]{8}$`);
  const ids = Object.keys(template.Resources).filter(
    (id) => template.Resources[id].Type === type && pattern.test(id)
  );
  expect(ids).toHaveLength(1);
  return ids[0];
}

// A rendered DynamoDB ARN. Its partition slot may be a token such as
// ${AWS::Partition}, which holds colons: Table.fromTableName and formatArn
// render the partition, region and account that way in this env-less synth.
const ARN_SLOT = '(?:\\$\\{[^}]*\\}|[^:$])*';
const DYNAMODB_ARN = new RegExp(`^arn:${ARN_SLOT}:dynamodb:`);

/**
 * Whether a rendered grant resource can name the table: its ARN, index ARNs
 * or another of its attributes, '*', every DynamoDB resource, or a table
 * pattern that matches its name or names it by Ref. A condition only narrows
 * a grant, so roleGrants' " when {...}" suffix is dropped first.
 */
function reachesTable(resource: string, tableId: string, tableName: string): boolean {
  const target = resource.split(' when ')[0];
  if (target === '*' || target.startsWith('${' + tableId + '.')) return true;
  if (!DYNAMODB_ARN.test(target)) return false;
  if (target.endsWith(':*')) return true;
  const segment = /:table\/([^/]*)/.exec(target);
  if (!segment) return false;
  if (segment[1] === '${' + tableId + '}') return true;
  const glob = segment[1].split('*').map((part) => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*');
  return new RegExp(`^${glob}$`).test(tableName);
}

describe('R14 DynamoDB: one customer managed key for the account tables (Requirements 14.5, 16.2)', () => {
  test('one new KMS key, rotated and retained, with the default key policy and the alias alias/dda-portal/account-tables', () => {
    const template = computeTemplate.toJSON();
    const key = logicalIdOf(template, 'AWS::KMS::Key', 'AccountTablesKey');
    const ids = (type: string) => Object.keys(template.Resources).filter(
      (id) => template.Resources[id].Type === type && !TOPIC_KEY.test(id) && !id.startsWith('TrainingAlertTopicKeyAlias')
    );
    expect(ids('AWS::KMS::Key')).toEqual([key]);
    for (const nested of nestedComputeTemplates) nested.resourceCountIs('AWS::KMS::Key', 0);
    const resource = template.Resources[key];
    expect(resource.DeletionPolicy).toBe('Retain');
    expect(resource.UpdateReplacePolicy).toBe('Retain');
    expect(resource.Properties.EnableKeyRotation).toBe(true);
    expect(resource.Properties.Description).toContain('dda-portal-edge-credentials and dda-portal-account-sync');
    // CDK's default key policy: the account root holds kms:*, which delegates to IAM.
    expect(resource.Properties.KeyPolicy.Statement.map((statement: any) => ({
      ...statement, Principal: renderValue(statement.Principal.AWS),
    }))).toEqual([{ Action: 'kms:*', Effect: 'Allow', Principal: 'arn:${AWS::Partition}:iam::' + ACCOUNT_REF + ':root',
      Resource: '*' }]);
    expect(ids('AWS::KMS::Alias').map((alias) => template.Resources[alias].Properties)).toEqual([
      { AliasName: 'alias/dda-portal/account-tables', TargetKeyId: { 'Fn::GetAtt': [key, 'Arn'] } },
    ]);
  });

  test('both tables use the key, keep their names, keys, point-in-time recovery and Retain, and depend on AccountTablesKeyAccess', () => {
    const template = computeTemplate.toJSON();
    const key = logicalIdOf(template, 'AWS::KMS::Key', 'AccountTablesKey');
    const access = logicalIdOf(template, 'AWS::IAM::Policy', 'AccountTablesKeyAccess');
    for (const table of ACCOUNT_TABLES) {
      const resource = template.Resources[logicalIdOf(template, 'AWS::DynamoDB::Table', table.prefix)];
      // The whole property set: SSESpecification is the only new property.
      expect(resource.Properties).toEqual({
        TableName: table.name,
        KeySchema: [{ AttributeName: table.partitionKey, KeyType: 'HASH' }],
        AttributeDefinitions: [{ AttributeName: table.partitionKey, AttributeType: 'S' }],
        BillingMode: 'PAY_PER_REQUEST',
        PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
        SSESpecification: { SSEEnabled: true, SSEType: 'KMS', KMSMasterKeyId: { 'Fn::GetAtt': [key, 'Arn'] } },
      });
      expect(resource.DeletionPolicy).toBe('Retain');
      expect(resource.UpdateReplacePolicy).toBe('Retain');
      expect(asArray(resource.DependsOn ?? [])).toContain(access);
    }
  });

  // Feature: security-scan-remediation-high, Property 8: Every principal of the account tables can use their key
  // Validates: Requirement 14.5
  test('Property 8: every role with a dynamodb grant on either table holds kms:Decrypt on the key, from the policy the tables depend on', () => {
    const template = computeTemplate.toJSON();
    const keyArn = '${' + logicalIdOf(template, 'AWS::KMS::Key', 'AccountTablesKey') + '.Arn}';
    const tables = ACCOUNT_TABLES.map((table) => ({
      ...table, id: logicalIdOf(template, 'AWS::DynamoDB::Table', table.prefix),
    }));
    const grantsTable = (action: string, resource: string): boolean =>
      (action === '*' || action.startsWith('dynamodb:')) &&
      tables.some((table) => reachesTable(resource, table.id, table.name));
    const principals = Object.keys(template.Resources)
      .filter((id) => template.Resources[id].Type === 'AWS::IAM::Role')
      .filter((role) => Object.entries(roleGrants(template, role)).some(([action, resources]) =>
        resources.some((resource) => grantsTable(action, resource))));
    expect(principals.map((role) => role.replace(/[0-9A-F]{8}$/, '')).sort()).toEqual(ACCOUNT_TABLE_PRINCIPALS);
    // Every policy of the stack that grants either table attaches to those roles
    // alone, by Ref. A user, a group, a role imported by name, or a reference
    // from anywhere but those roles' ManagedPolicyArns (a nested stack, an
    // output) would give the tables a principal the role check can't see. A
    // NotAction or NotResource statement counts as a table grant.
    const strays = Object.entries<any>(template.Resources)
      .filter(([, resource]) => resource.Type === 'AWS::IAM::Policy' || resource.Type === 'AWS::IAM::ManagedPolicy')
      .filter(([, resource]) => resource.Properties.PolicyDocument.Statement.some((statement: any) =>
        statement.Effect === 'Allow' && ('NotAction' in statement || 'NotResource' in statement ||
          asArray(statement.Action).some((action: string) =>
            asArray(statement.Resource).some((value: any) => grantsTable(action, renderValue(value)))))))
      .flatMap(([id, resource]) => [
        ...asArray(resource.Properties.Roles ?? []).filter((role) => !principals.includes(role?.Ref))
          .map((role) => `${id}: role ${renderValue(role)}`),
        ...asArray(resource.Properties.Users ?? []).map((user) => `${id}: user ${renderValue(user)}`),
        ...asArray(resource.Properties.Groups ?? []).map((group) => `${id}: group ${renderValue(group)}`),
        ...referencePaths(template, id).filter(([section, holder, properties, key]) => !(section === 'Resources' &&
          principals.includes(holder) && properties === 'Properties' && key === 'ManagedPolicyArns'))
          .map((where) => `${id}: referenced at ${where.join('.')}`),
      ]);
    expect(strays).toEqual([]);
    for (const role of principals) {
      expect(roleGrants(template, role)['kms:Decrypt']).toContain(keyArn);
      // A Lambda role of this stack, so no cross-account principal uses either table.
      expect(template.Resources[role].Properties.AssumeRolePolicyDocument.Statement).toEqual([
        { Action: 'sts:AssumeRole', Effect: 'Allow', Principal: { Service: 'lambda.amazonaws.com' } },
      ]);
    }
    // The key access comes from the one policy both tables depend on (case above),
    // so every principal holds it before either table switches to the key.
    const access = template.Resources[logicalIdOf(template, 'AWS::IAM::Policy', 'AccountTablesKeyAccess')].Properties;
    expect(asArray(access.Roles).map((ref) => ref.Ref).sort()).toEqual([...principals].sort());
    expect(access.PolicyDocument.Statement.map((statement: any) => ({
      ...statement, Action: [...asArray(statement.Action)].sort(), Resource: asArray(statement.Resource).map(renderValue),
    }))).toEqual([{ Action: ACCOUNT_TABLES_KEY_ACTIONS, Effect: 'Allow', Resource: [keyArn] }]);
    // No nested stack's role is granted either table: none names them.
    for (const nested of nestedComputeTemplates) {
      const text = JSON.stringify(nested.toJSON());
      for (const table of tables) {
        expect(text).not.toContain(table.id);
        expect(text).not.toContain(table.name);
      }
    }
  });
});

// ---------------------------------------------------------------------------
// Deploy constraints (Requirement 16.1; design "R14 and R15 fixtures and
// deploy constraints")
// ---------------------------------------------------------------------------

describe('ComputeStack deploy limits (Requirement 16.1)', () => {
  test('the minified template stays under 1,000,000 bytes and 500 resources', () => {
    const template = computeTemplate.toJSON();
    expect(Buffer.byteLength(JSON.stringify(template), 'utf8')).toBeLessThan(1_000_000);
    expect(Object.keys(template.Resources).length).toBeLessThan(500);
  });
});
