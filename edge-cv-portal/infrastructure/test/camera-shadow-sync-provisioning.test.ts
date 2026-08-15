/**
 * camera-shadow-sync-provisioning — Gap 2 bug condition exploration tests
 * (written BEFORE the fix; they MUST FAIL on the unfixed tree — the failures
 * are the executable counterexamples confirming the root cause, already
 * confirmed empirically in account 164152369890. After the fix they encode
 * the expected behavior and must pass.)
 *
 * Gap 2: the `dda_camera_registry_shadow_documents` IoT topic rule
 * (forwarding `$aws/things/+/shadow/name/dda-camera-registry/update/documents`
 * events to the `dda-portal-camera-shadow-reports` SQS queue) is defined only
 * in usecase-account-stack.ts, which is deployed only for cross-account
 * use-case onboarding. In the common single-account setup (portal account ==
 * use-case account) no UsecaseAccountStack exists, so the rule was never
 * created and shadow reports never reached the ingest queue/Lambda — the
 * portal showed "Never synced" with Cameras (0). Additionally, the
 * usecase-account-stack copies use unconditional fixed names
 * (`DDACameraShadowRuleRole`, `dda_camera_registry_shadow_documents`), so a
 * deploy into an account that already has the rule/role fails on create.
 *
 * Synth setup mirrors camera-registry-infra.test.ts (StorageStack +
 * ComputeStack + a cross-account UseCaseAccountStack); the existing test
 * files are untouched.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';
import { UseCaseAccountStack } from '../lib/usecase-account-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';
const PORTAL_ACCOUNT = '222222222222';
const USECASE_ACCOUNT = '333333333333';
const REGION = 'us-east-1';

const CAMERA_SHADOW_TOPIC =
  '$aws/things/+/shadow/name/dda-camera-registry/update/documents';

// Synthesized once: the ComputeStack stages Lambda/layer assets, which is
// expensive.
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
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
  });

  // Concrete cross-account env, mirroring camera-registry-infra.test.ts.
  const usecase = new UseCaseAccountStack(app, 'UseCase', {
    env: { account: USECASE_ACCOUNT, region: REGION },
    portalAccountId: PORTAL_ACCOUNT,
    externalId: 'test-external-id',
  });

  computeTemplate = Template.fromStack(compute);
  usecaseTemplate = Template.fromStack(usecase);
}, 300_000);

describe('exploration: bug condition (Gap 2 — single-account rule provisioning)', () => {
  /**
   * **Feature: camera-shadow-sync-provisioning, Property 3: Bug Condition —
   * Gap 2: ComputeStack provisions the camera shadow topic rule**
   *
   * Validates: Requirements 1.4
   *
   * Exploration case 4 — FAILS on unfixed code: the ComputeStack template's
   * only topic rule is `dda_user_accounts_shadow_documents`; with no
   * camera-registry rule, `dda-camera-registry` shadow documents events are
   * never forwarded and the `dda-portal-camera-shadow-reports` queue sits
   * with zero traffic (isBugCondition_Gap2 for every single-account deploy).
   */
  test('ComputeStack template contains a camera-registry shadow topic rule', () => {
    const rules = Object.values(
      computeTemplate.findResources('AWS::IoT::TopicRule')
    );
    const ruleNames = rules.map((r: any) => r.Properties.RuleName);
    const cameraRules = rules.filter((r: any) =>
      String(r.Properties.TopicRulePayload?.Sql ?? '').includes(
        CAMERA_SHADOW_TOPIC
      )
    );
    // Counterexample surface: which rules DO exist in the unfixed template.
    expect({ cameraRuleCount: cameraRules.length, ruleNames }).toEqual({
      cameraRuleCount: 1,
      ruleNames: expect.arrayContaining([
        expect.stringContaining('camera_registry_shadow_documents'),
      ]),
    });
  });

  /**
   * **Feature: camera-shadow-sync-provisioning, Property 4: Bug Condition —
   * Gap 2: no fixed-name collisions between the two definitions**
   *
   * Validates: Requirements 1.5
   *
   * Exploration case 5 — FAILS on unfixed code: the UseCaseAccountStack's
   * `DDACameraShadowRuleRole` role, its default policy, and the
   * `dda_camera_registry_shadow_documents` rule are unconditional fixed-name
   * resources, so deploying the stack into an account that already has the
   * rule/role (manually created today, ComputeStack-provisioned after the
   * fix) fails on create. Each must carry a CloudFormation `Condition`.
   */
  test('UseCaseAccountStack fixed-name camera shadow resources are condition-gated', () => {
    const roles = Object.entries(
      usecaseTemplate.findResources('AWS::IAM::Role')
    ).filter(
      ([, r]: [string, any]) =>
        r.Properties.RoleName === 'DDACameraShadowRuleRole'
    );
    expect(roles).toHaveLength(1);
    const [roleLogicalId, role] = roles[0] as [string, any];

    const policies = Object.values(
      usecaseTemplate.findResources('AWS::IAM::Policy')
    ).filter(
      (p: any) =>
        (p.Properties.Roles ?? []).some(
          (ref: any) => ref.Ref === roleLogicalId
        ) &&
        JSON.stringify(p.Properties.PolicyDocument).includes(
          'SendCameraShadowReports'
        )
    );
    expect(policies).toHaveLength(1);
    const policy = policies[0] as any;

    const rules = Object.values(
      usecaseTemplate.findResources('AWS::IoT::TopicRule')
    ).filter(
      (r: any) =>
        r.Properties.RuleName === 'dda_camera_registry_shadow_documents'
    );
    expect(rules).toHaveLength(1);
    const rule = rules[0] as any;

    // Counterexample surface: which of the three resources lack a Condition.
    expect({
      roleCondition: role.Condition,
      defaultPolicyCondition: policy.Condition,
      topicRuleCondition: rule.Condition,
    }).toEqual({
      roleCondition: expect.any(String),
      defaultPolicyCondition: expect.any(String),
      topicRuleCondition: expect.any(String),
    });
  });
});

/**
 * Preservation tests (written BEFORE the fix, observation-first): the values
 * asserted below were observed on the UNFIXED tree and must hold on BOTH the
 * unfixed and fixed trees. The Gap 2 fix only ADDS a ComputeStack rule + role
 * and gates the UseCaseAccountStack copies behind a deploy-time condition —
 * every resource property recorded here is outside those additions.
 */
describe('preservation: infrastructure unchanged outside the additions (Property 6)', () => {
  const PORTAL_QUEUE_ARN = `arn:aws:sqs:${REGION}:${PORTAL_ACCOUNT}:dda-portal-camera-shadow-reports`;
  const PORTAL_QUEUE_URL = `https://sqs.${REGION}.amazonaws.com/${PORTAL_ACCOUNT}/dda-portal-camera-shadow-reports`;

  /**
   * **Feature: camera-shadow-sync-provisioning, Property 6: Preservation —
   * infrastructure unchanged outside the additions**
   *
   * Validates: Requirements 3.3
   *
   * Cross-account usecase synth preservation: with portalAccountId ≠ account,
   * the `DDACameraShadowRuleRole` role name, its `SendCameraShadowReports`
   * statement (sqs:SendMessage scoped to the portal queue ARN), the
   * `dda_camera_registry_shadow_documents` rule name, its SQL, and the queue
   * ARN/URL target equal their pre-fix values.
   */
  test('cross-account UseCaseAccountStack camera shadow role/policy/rule keep their pre-fix values', () => {
    // Role: fixed name, assumable by IoT.
    const roles = Object.entries(
      usecaseTemplate.findResources('AWS::IAM::Role')
    ).filter(
      ([, r]: [string, any]) =>
        r.Properties.RoleName === 'DDACameraShadowRuleRole'
    );
    expect(roles).toHaveLength(1);
    const [roleLogicalId, role] = roles[0] as [string, any];
    expect(role.Properties.AssumeRolePolicyDocument.Statement).toEqual([
      {
        Action: 'sts:AssumeRole',
        Effect: 'Allow',
        Principal: { Service: 'iot.amazonaws.com' },
      },
    ]);

    // Default policy: exactly the SendCameraShadowReports statement scoped
    // to the portal account's queue ARN.
    const policies = Object.values(
      usecaseTemplate.findResources('AWS::IAM::Policy')
    ).filter((p: any) =>
      (p.Properties.Roles ?? []).some((ref: any) => ref.Ref === roleLogicalId)
    );
    expect(policies).toHaveLength(1);
    expect(
      (policies[0] as any).Properties.PolicyDocument.Statement
    ).toEqual([
      {
        Sid: 'SendCameraShadowReports',
        Action: 'sqs:SendMessage',
        Effect: 'Allow',
        Resource: PORTAL_QUEUE_ARN,
      },
    ]);

    // Topic rule: fixed name, exact SQL, exact queue URL/role target.
    const rules = Object.values(
      usecaseTemplate.findResources('AWS::IoT::TopicRule')
    ).filter(
      (r: any) =>
        r.Properties.RuleName === 'dda_camera_registry_shadow_documents'
    );
    expect(rules).toHaveLength(1);
    const payload = (rules[0] as any).Properties.TopicRulePayload;
    expect(payload.Sql).toBe(
      `SELECT *, topic(3) AS thing_name FROM '${CAMERA_SHADOW_TOPIC}'`
    );
    expect(payload.AwsIotSqlVersion).toBe('2016-03-23');
    expect(payload.RuleDisabled).toBe(false);
    expect(payload.Actions).toEqual([
      {
        Sqs: {
          QueueUrl: PORTAL_QUEUE_URL,
          RoleArn: { 'Fn::GetAtt': [roleLogicalId, 'Arn'] },
          UseBase64: false,
        },
      },
    ]);

    // The queue-ARN output keeps its pre-fix value (stays unconditional).
    usecaseTemplate.hasOutput('CameraShadowReportQueueArn', {
      Value: PORTAL_QUEUE_ARN,
    });
  });

  /**
   * **Feature: camera-shadow-sync-provisioning, Property 6: Preservation —
   * infrastructure unchanged outside the additions**
   *
   * Validates: Requirements 3.4
   *
   * ComputeStack sibling preservation: the `UserAccountsShadowRule`
   * (rule name `dda_user_accounts_shadow_documents`, SQL, ack-queue target
   * through its SendAccountSyncAcks role) is unchanged.
   */
  test('ComputeStack UserAccountsShadowRule is unchanged', () => {
    const rules = Object.values(
      computeTemplate.findResources('AWS::IoT::TopicRule')
    ).filter(
      (r: any) =>
        r.Properties.RuleName === 'dda_user_accounts_shadow_documents'
    );
    expect(rules).toHaveLength(1);
    const payload = (rules[0] as any).Properties.TopicRulePayload;
    expect(payload.Sql).toBe(
      "SELECT *, topic(3) AS thing_name FROM " +
        "'$aws/things/+/shadow/name/dda-user-accounts/update/documents'"
    );
    expect(payload.AwsIotSqlVersion).toBe('2016-03-23');
    expect(payload.RuleDisabled).toBe(false);
    expect(payload.Actions).toHaveLength(1);
    const sqsAction = payload.Actions[0].Sqs;
    expect(sqsAction.UseBase64).toBe(false);

    // Queue target: the account-sync ack queue.
    const ackQueues = Object.entries(
      computeTemplate.findResources('AWS::SQS::Queue')
    ).filter(
      ([, q]: [string, any]) =>
        q.Properties.QueueName === 'dda-portal-account-sync-acks'
    );
    expect(ackQueues).toHaveLength(1);
    const [ackQueueLogicalId] = ackQueues[0];
    expect(sqsAction.QueueUrl).toEqual({ Ref: ackQueueLogicalId });

    // Rule role: assumable by IoT, SendAccountSyncAcks scoped to the ack
    // queue ARN.
    const ruleRoleLogicalId = sqsAction.RoleArn['Fn::GetAtt'][0];
    const rolePolicies = Object.values(
      computeTemplate.findResources('AWS::IAM::Policy')
    ).filter((p: any) =>
      (p.Properties.Roles ?? []).some(
        (ref: any) => ref.Ref === ruleRoleLogicalId
      )
    );
    expect(rolePolicies).toHaveLength(1);
    expect(
      (rolePolicies[0] as any).Properties.PolicyDocument.Statement
    ).toEqual([
      {
        Sid: 'SendAccountSyncAcks',
        Action: 'sqs:SendMessage',
        Effect: 'Allow',
        Resource: { 'Fn::GetAtt': [ackQueueLogicalId, 'Arn'] },
      },
    ]);
  });

  /**
   * **Feature: camera-shadow-sync-provisioning, Property 6: Preservation —
   * infrastructure unchanged outside the additions**
   *
   * Validates: Requirements 3.4
   *
   * ComputeStack sibling preservation: the camera shadow report queue, its
   * DLQ, and the cross-account queue policy properties are unchanged — the
   * fix only references the existing queue construct from the new rule.
   */
  test('ComputeStack camera shadow report queue/DLQ/queue-policy are unchanged', () => {
    // DLQ.
    const dlqs = Object.entries(
      computeTemplate.findResources('AWS::SQS::Queue')
    ).filter(
      ([, q]: [string, any]) =>
        q.Properties.QueueName === 'dda-portal-camera-shadow-reports-dlq'
    );
    expect(dlqs).toHaveLength(1);
    const [dlqLogicalId, dlq] = dlqs[0] as [string, any];
    expect(dlq.Properties.MessageRetentionPeriod).toBe(1209600); // 14 days

    // Report queue with its redrive policy.
    const queues = Object.entries(
      computeTemplate.findResources('AWS::SQS::Queue')
    ).filter(
      ([, q]: [string, any]) =>
        q.Properties.QueueName === 'dda-portal-camera-shadow-reports'
    );
    expect(queues).toHaveLength(1);
    const [queueLogicalId, queue] = queues[0] as [string, any];
    expect(queue.Properties.VisibilityTimeout).toBe(180);
    expect(queue.Properties.MessageRetentionPeriod).toBe(345600); // 4 days
    expect(queue.Properties.RedrivePolicy).toEqual({
      deadLetterTargetArn: { 'Fn::GetAtt': [dlqLogicalId, 'Arn'] },
      maxReceiveCount: 3,
    });

    // Queue policy: the enforceSSL deny plus the cross-account/same-account
    // IoT-rule delivery allow (trusted use-case accounts + portal account).
    const queuePolicies = Object.values(
      computeTemplate.findResources('AWS::SQS::QueuePolicy')
    ).filter((p: any) =>
      (p.Properties.Queues ?? []).some(
        (ref: any) => ref.Ref === queueLogicalId
      )
    );
    expect(queuePolicies).toHaveLength(1);
    const statements = (queuePolicies[0] as any).Properties.PolicyDocument
      .Statement;
    expect(statements).toHaveLength(2);

    const denies = statements.filter((s: any) => s.Effect === 'Deny');
    expect(denies).toHaveLength(1);
    expect(denies[0].Action).toBe('sqs:*');
    expect(denies[0].Condition).toEqual({
      Bool: { 'aws:SecureTransport': 'false' },
    });

    const allows = statements.filter(
      (s: any) => s.Sid === 'AllowUseCaseAccountIotRuleDelivery'
    );
    expect(allows).toEqual([
      {
        Sid: 'AllowUseCaseAccountIotRuleDelivery',
        Action: 'sqs:SendMessage',
        Effect: 'Allow',
        Principal: { AWS: '*' },
        Resource: { 'Fn::GetAtt': [queueLogicalId, 'Arn'] },
        Condition: {
          StringEquals: {
            'aws:PrincipalAccount': [
              TRUSTED_USECASE_ACCOUNT,
              { Ref: 'AWS::AccountId' },
            ],
          },
        },
      },
    ]);
  });
});

/**
 * Fix-checking tests (written AFTER the fix, task 3.7): assert the exact
 * shape of the Gap 2 fix on the synthesized templates. The "for any
 * synthesized template" quantification of Properties 3 and 4 is discharged
 * by synthesis determinism, matching the repo's established practice for
 * CDK properties.
 */
describe('fix checking: single-account rule provisioning and collision avoidance (Properties 3 and 4)', () => {
  /**
   * **Feature: camera-shadow-sync-provisioning, Property 3: Bug Condition —
   * Gap 2: ComputeStack provisions the camera shadow topic rule**
   *
   * Validates: Requirements 2.4, 2.6
   *
   * Exactly one IoT topic rule whose SQL is exactly the camera-registry
   * shadow documents select, awsIotSqlVersion 2016-03-23, enabled, with a
   * single action delivering to the dda-portal-camera-shadow-reports queue
   * (useBase64: false) through a role assumable by iot.amazonaws.com whose
   * policy allows sqs:SendMessage scoped to that queue's ARN.
   */
  test('ComputeStack has exactly one camera-registry shadow rule delivering to the report queue', () => {
    const rules = Object.values(
      computeTemplate.findResources('AWS::IoT::TopicRule')
    ).filter(
      (r: any) =>
        r.Properties.TopicRulePayload?.Sql ===
        `SELECT *, topic(3) AS thing_name FROM '${CAMERA_SHADOW_TOPIC}'`
    );
    expect(rules).toHaveLength(1);
    const payload = (rules[0] as any).Properties.TopicRulePayload;
    expect(payload.AwsIotSqlVersion).toBe('2016-03-23');
    expect(payload.RuleDisabled).toBe(false);
    expect(payload.Actions).toHaveLength(1);
    const sqsAction = payload.Actions[0].Sqs;
    expect(sqsAction.UseBase64).toBe(false);

    // Delivery target: the existing camera shadow report queue.
    const queues = Object.entries(
      computeTemplate.findResources('AWS::SQS::Queue')
    ).filter(
      ([, q]: [string, any]) =>
        q.Properties.QueueName === 'dda-portal-camera-shadow-reports'
    );
    expect(queues).toHaveLength(1);
    const [queueLogicalId] = queues[0];
    expect(sqsAction.QueueUrl).toEqual({ Ref: queueLogicalId });

    // Rule role: assumable by IoT, sqs:SendMessage scoped to the queue ARN.
    const ruleRoleLogicalId = sqsAction.RoleArn['Fn::GetAtt'][0];
    const role = computeTemplate.findResources('AWS::IAM::Role')[
      ruleRoleLogicalId
    ] as any;
    expect(role).toBeDefined();
    expect(role.Properties.AssumeRolePolicyDocument.Statement).toEqual([
      {
        Action: 'sts:AssumeRole',
        Effect: 'Allow',
        Principal: { Service: 'iot.amazonaws.com' },
      },
    ]);
    const policies = Object.values(
      computeTemplate.findResources('AWS::IAM::Policy')
    ).filter((p: any) =>
      (p.Properties.Roles ?? []).some(
        (ref: any) => ref.Ref === ruleRoleLogicalId
      )
    );
    expect(policies).toHaveLength(1);
    expect(
      (policies[0] as any).Properties.PolicyDocument.Statement
    ).toEqual([
      {
        Sid: 'SendCameraShadowReports',
        Action: 'sqs:SendMessage',
        Effect: 'Allow',
        Resource: { 'Fn::GetAtt': [queueLogicalId, 'Arn'] },
      },
    ]);
  });

  /**
   * **Feature: camera-shadow-sync-provisioning, Property 4: Bug Condition —
   * Gap 2: no fixed-name collisions between the two definitions**
   *
   * Validates: Requirements 2.5
   *
   * ComputeStack side: the new rule role carries NO RoleName property
   * (CDK-generated name — IAM role names are account-global, so a fixed
   * name would collide with the UseCaseAccountStack copy or the manually
   * created production role) and the rule is named
   * dda_camera_registry_shadow_documents_portal (distinct from the
   * usecase-account fixed name).
   */
  test('ComputeStack rule role is CDK-named and the rule name carries the _portal suffix', () => {
    const rules = Object.values(
      computeTemplate.findResources('AWS::IoT::TopicRule')
    ).filter((r: any) =>
      String(r.Properties.TopicRulePayload?.Sql ?? '').includes(
        CAMERA_SHADOW_TOPIC
      )
    );
    expect(rules).toHaveLength(1);
    const rule = rules[0] as any;
    expect(rule.Properties.RuleName).toBe(
      'dda_camera_registry_shadow_documents_portal'
    );

    const ruleRoleLogicalId =
      rule.Properties.TopicRulePayload.Actions[0].Sqs.RoleArn['Fn::GetAtt'][0];
    const role = computeTemplate.findResources('AWS::IAM::Role')[
      ruleRoleLogicalId
    ] as any;
    expect(role).toBeDefined();
    expect(role.Properties.RoleName).toBeUndefined();
  });

  /**
   * **Feature: camera-shadow-sync-provisioning, Property 4: Bug Condition —
   * Gap 2: no fixed-name collisions between the two definitions**
   *
   * Validates: Requirements 2.5
   *
   * UseCaseAccountStack side: the condition expression is
   * Fn::Not[Fn::Equals[portalAccountId, Ref AWS::AccountId]] and EXACTLY
   * the fixed-name role, its default policy, and the topic rule carry it —
   * so the fixed-name resources are created iff the use-case account
   * differs from the portal account. STACK_VERSION output is 1.6.0 (D9).
   */
  test('UseCaseAccountStack gates exactly the three fixed-name resources behind Not(Equals(portalAccountId, AWS::AccountId))', () => {
    const template = usecaseTemplate.toJSON();

    // Locate the three fixed-name resources.
    const roles = Object.entries(
      usecaseTemplate.findResources('AWS::IAM::Role')
    ).filter(
      ([, r]: [string, any]) =>
        r.Properties.RoleName === 'DDACameraShadowRuleRole'
    );
    expect(roles).toHaveLength(1);
    const [roleLogicalId, role] = roles[0] as [string, any];

    const policies = Object.entries(
      usecaseTemplate.findResources('AWS::IAM::Policy')
    ).filter(([, p]: [string, any]) =>
      (p.Properties.Roles ?? []).some((ref: any) => ref.Ref === roleLogicalId)
    );
    expect(policies).toHaveLength(1);
    const [policyLogicalId, policy] = policies[0] as [string, any];

    const topicRules = Object.entries(
      usecaseTemplate.findResources('AWS::IoT::TopicRule')
    ).filter(
      ([, r]: [string, any]) =>
        r.Properties.RuleName === 'dda_camera_registry_shadow_documents'
    );
    expect(topicRules).toHaveLength(1);
    const [ruleLogicalId, rule] = topicRules[0] as [string, any];

    // All three carry the same condition.
    const conditionName = role.Condition;
    expect(conditionName).toEqual(expect.any(String));
    expect(policy.Condition).toBe(conditionName);
    expect(rule.Condition).toBe(conditionName);

    // The condition expression: Not(Equals(portalAccountId, AWS::AccountId)).
    expect(template.Conditions[conditionName]).toEqual({
      'Fn::Not': [
        { 'Fn::Equals': [PORTAL_ACCOUNT, { Ref: 'AWS::AccountId' }] },
      ],
    });

    // EXACTLY those three resources carry it — nothing else is gated.
    const conditioned = Object.entries(template.Resources)
      .filter(([, r]: [string, any]) => r.Condition === conditionName)
      .map(([logicalId]) => logicalId)
      .sort();
    expect(conditioned).toEqual(
      [roleLogicalId, policyLogicalId, ruleLogicalId].sort()
    );

    // Version bump: 1.5.0 → 1.6.0 with the new conditional behavior.
    usecaseTemplate.hasOutput('StackVersion', { Value: '1.6.0' });
  });
});
