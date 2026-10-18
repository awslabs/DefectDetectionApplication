/**
 * The use-case account template's workflow debug additions
 * (workflow-debug-mode design §6.1, §9; tasks.md 4.2;
 * workflow-telemetry-dashboards design §14.2 for the shared statements).
 *
 * - DebugCrossAccountCondition gates DDADebugRuleErrorRole, its default
 *   policy, the /dda/workflow-debug/rule-errors log group and the
 *   dda_dbg_ingest rule (the camera-shadow block's form).
 * - The rule filters dda/debug/+/+/+ (no #), carries the §6.1 SQL and a
 *   role-less Lambda action on the Portal ingest alias of this account.
 * - The error role trusts iot.amazonaws.com conditioned on this account and
 *   the dda_dbg_* rules, with three logs actions on the error group.
 * - DDAPortalAccessRole carries TelemetryMediaRetentionRead and
 *   TelemetryConventionRuleRead exactly once, and calling the helper again
 *   adds nothing.
 *
 * Synth setup mirrors camera-shadow-sync-provisioning.test.ts (a concrete
 * cross-account env).
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import {
  UseCaseAccountStack,
  addTelemetrySharedStatements,
  TELEMETRY_CONVENTION_RULE_READ_SID,
  TELEMETRY_MEDIA_RETENTION_READ_SID,
} from '../lib/usecase-account-stack';

const PORTAL_ACCOUNT = '222222222222';
const USECASE_ACCOUNT = '333333333333';
const REGION = 'us-east-1';
const RULE_SQL =
  "SELECT encode(*, 'base64') AS p, topic() AS t, principal() AS pr, clientid() AS c, timestamp() AS r, newuuid() AS m FROM 'dda/debug/+/+/+'";

let template: Template;
let twiceTemplate: Template;

function build(callHelperAgain: boolean): Template {
  const app = new cdk.App();
  const stack = new UseCaseAccountStack(app, 'UseCase', {
    env: { account: USECASE_ACCOUNT, region: REGION },
    portalAccountId: PORTAL_ACCOUNT,
    externalId: 'test-external-id',
  });
  if (callHelperAgain) {
    addTelemetrySharedStatements(stack.role, stack.account);
    addTelemetrySharedStatements(stack.role, stack.account);
  }
  return Template.fromStack(stack);
}

beforeAll(() => {
  template = build(false);
  twiceTemplate = build(true);
}, 300_000);

function flat(value: unknown): string {
  return JSON.stringify(value);
}

function actionsOf(statement: any): string[] {
  return Array.isArray(statement.Action) ? statement.Action : [statement.Action];
}

function resourcesOf(statement: any): any[] {
  return Array.isArray(statement.Resource) ? statement.Resource : [statement.Resource];
}

/** Every statement of every policy attached to the role with the given RoleName. */
function statementsOfRoleNamed(tpl: Template, roleName: string): any[] {
  const roles = Object.entries(tpl.findResources('AWS::IAM::Role')).filter(
    ([, r]: [string, any]) => r.Properties.RoleName === roleName,
  );
  expect(roles).toHaveLength(1);
  const [roleId] = roles[0];
  const policies = Object.values(tpl.findResources('AWS::IAM::Policy')) as any[];
  return policies
    .filter((p) => (p.Properties.Roles ?? []).some((ref: any) => ref.Ref === roleId))
    .flatMap((p) => p.Properties.PolicyDocument?.Statement ?? []);
}

describe('the condition-gated debug resources', () => {
  test('DDADebugRuleErrorRole, its default policy, the error log group and the rule carry DebugCrossAccountCondition', () => {
    const conditions = template.toJSON().Conditions ?? {};
    expect(conditions.DebugCrossAccountCondition).toBeDefined();
    expect(flat(conditions.DebugCrossAccountCondition)).toContain('Fn::Not');
    expect(flat(conditions.DebugCrossAccountCondition)).toContain(PORTAL_ACCOUNT);

    const roles = Object.entries(template.findResources('AWS::IAM::Role')).filter(
      ([, r]: [string, any]) => r.Properties.RoleName === 'DDADebugRuleErrorRole',
    );
    expect(roles).toHaveLength(1);
    const [roleId, role] = roles[0] as [string, any];
    expect(role.Condition).toBe('DebugCrossAccountCondition');

    const policies = Object.values(template.findResources('AWS::IAM::Policy')).filter(
      (p: any) => (p.Properties.Roles ?? []).some((ref: any) => ref.Ref === roleId),
    ) as any[];
    expect(policies).toHaveLength(1);
    expect(policies[0].Condition).toBe('DebugCrossAccountCondition');

    const groups = Object.values(template.findResources('AWS::Logs::LogGroup')).filter(
      (g: any) => g.Properties.LogGroupName === '/dda/workflow-debug/rule-errors',
    ) as any[];
    expect(groups).toHaveLength(1);
    expect(groups[0].Condition).toBe('DebugCrossAccountCondition');
    expect(groups[0].Properties.RetentionInDays).toBe(14);

    const rules = Object.values(template.findResources('AWS::IoT::TopicRule')).filter(
      (r: any) => r.Properties.RuleName === 'dda_dbg_ingest',
    ) as any[];
    expect(rules).toHaveLength(1);
    expect(rules[0].Condition).toBe('DebugCrossAccountCondition');
  });

  test('the camera-shadow resources keep their own condition', () => {
    const rules = Object.values(template.findResources('AWS::IoT::TopicRule')).filter(
      (r: any) => r.Properties.RuleName === 'dda_camera_registry_shadow_documents',
    ) as any[];
    expect(rules).toHaveLength(1);
    expect(rules[0].Condition).toBe('CameraShadowCrossAccountCondition');
  });
});

describe('the dda_dbg_ingest rule', () => {
  test('filter without #, the §6.1 SQL, a role-less Lambda action on the Portal alias of this account, the error action', () => {
    const rule = (Object.values(template.findResources('AWS::IoT::TopicRule')) as any[]).find(
      (r) => r.Properties.RuleName === 'dda_dbg_ingest',
    );
    const payload = rule.Properties.TopicRulePayload;
    expect(payload.Sql).toBe(RULE_SQL);
    expect(payload.Sql).toContain("FROM 'dda/debug/+/+/+'");
    expect(payload.Sql).not.toContain('#');
    expect(payload.AwsIotSqlVersion).toBe('2016-03-23');
    expect(payload.Actions).toHaveLength(1);
    expect(Object.keys(payload.Actions[0])).toEqual(['Lambda']);
    expect(Object.keys(payload.Actions[0].Lambda)).toEqual(['FunctionArn']);
    expect(payload.Actions[0].Lambda.FunctionArn).toBe(
      `arn:aws:lambda:${REGION}:${PORTAL_ACCOUNT}:function:dda-portal-workflow-debug-ingest:acct-${USECASE_ACCOUNT}`,
    );
    expect(payload.ErrorAction.CloudwatchLogs.LogGroupName).toBeDefined();
    expect(payload.ErrorAction.CloudwatchLogs.RoleArn['Fn::GetAtt'][1]).toBe('Arn');
  });
});

describe('DDADebugRuleErrorRole', () => {
  test('trusts iot.amazonaws.com conditioned on this account and the dda_dbg_* rules; three logs actions on the error group', () => {
    const role = (Object.values(template.findResources('AWS::IAM::Role')) as any[]).find(
      (r) => r.Properties.RoleName === 'DDADebugRuleErrorRole',
    );
    const trust = role.Properties.AssumeRolePolicyDocument.Statement;
    expect(trust).toHaveLength(1);
    expect(trust[0].Principal.Service).toBe('iot.amazonaws.com');
    expect(trust[0].Condition.StringEquals['aws:SourceAccount']).toBe(USECASE_ACCOUNT);
    expect(trust[0].Condition.ArnLike['aws:SourceArn']).toBe(`arn:aws:iot:${REGION}:${USECASE_ACCOUNT}:rule/dda_dbg_*`);
    const statements = statementsOfRoleNamed(template, 'DDADebugRuleErrorRole');
    expect(statements).toHaveLength(1);
    expect(statements[0].Sid).toBe('WriteDebugRuleErrors');
    expect(actionsOf(statements[0]).sort()).toEqual(
      ['logs:CreateLogStream', 'logs:DescribeLogStreams', 'logs:PutLogEvents'].sort(),
    );
    expect(resourcesOf(statements[0])).toEqual([
      `arn:aws:logs:${REGION}:${USECASE_ACCOUNT}:log-group:/dda/workflow-debug/rule-errors:*`,
    ]);
  });

  test('no role in the template holds lambda:InvokeFunction on the ingest function', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
    for (const policy of policies) {
      for (const statement of policy.Properties.PolicyDocument?.Statement ?? []) {
        if (actionsOf(statement).includes('lambda:InvokeFunction')) {
          expect(flat(resourcesOf(statement))).not.toContain('dda-portal-workflow-debug-ingest');
        }
      }
    }
  });
});

describe('the shared statements on DDAPortalAccessRole', () => {
  function sharedStatements(tpl: Template) {
    const statements = statementsOfRoleNamed(tpl, 'DDAPortalAccessRole');
    return {
      retention: statements.filter((s) => s.Sid === TELEMETRY_MEDIA_RETENTION_READ_SID),
      rules: statements.filter((s) => s.Sid === TELEMETRY_CONVENTION_RULE_READ_SID),
    };
  }

  test('TelemetryMediaRetentionRead and TelemetryConventionRuleRead exactly once, with the exact actions and resources, unconditional', () => {
    const { retention, rules } = sharedStatements(template);
    expect(retention).toHaveLength(1);
    expect(retention[0].Effect).toBe('Allow');
    expect(actionsOf(retention[0])).toEqual(['s3:GetLifecycleConfiguration', 's3:GetBucketVersioning']);
    expect(resourcesOf(retention[0])).toEqual([`arn:aws:s3:::dda-inference-results-${USECASE_ACCOUNT}`]);
    expect(retention[0].Condition).toBeUndefined();
    expect(rules).toHaveLength(1);
    expect(rules[0].Effect).toBe('Allow');
    expect(actionsOf(rules[0])).toEqual(['iot:GetTopicRule']);
    expect(resourcesOf(rules[0])).toEqual([
      `arn:aws:iot:*:${USECASE_ACCOUNT}:rule/dda_tel_ingest`,
      `arn:aws:iot:*:${USECASE_ACCOUNT}:rule/dda_dbg_ingest`,
    ]);
    expect(rules[0].Condition).toBeUndefined();
    // The carrier policy itself is unconditional (not gated like the rule's role).
    const roles = Object.entries(template.findResources('AWS::IAM::Role')).filter(
      ([, r]: [string, any]) => r.Properties.RoleName === 'DDAPortalAccessRole',
    );
    const [roleId] = roles[0];
    const carriers = (Object.values(template.findResources('AWS::IAM::Policy')) as any[]).filter(
      (p) => (p.Properties.Roles ?? []).some((ref: any) => ref.Ref === roleId)
        && flat(p.Properties.PolicyDocument).includes(TELEMETRY_MEDIA_RETENTION_READ_SID),
    );
    expect(carriers).toHaveLength(1);
    expect(carriers[0].Condition).toBeUndefined();
  });

  test('calling the helper again adds nothing', () => {
    const once = sharedStatements(template);
    const twice = sharedStatements(twiceTemplate);
    expect(twice.retention).toHaveLength(1);
    expect(twice.rules).toHaveLength(1);
    expect(twice.retention).toEqual(once.retention);
    expect(twice.rules).toEqual(once.rules);
    expect(statementsOfRoleNamed(twiceTemplate, 'DDAPortalAccessRole')).toHaveLength(
      statementsOfRoleNamed(template, 'DDAPortalAccessRole').length,
    );
  });

  test('the pre-existing InferenceResultsAccess statement is unchanged beside the new one', () => {
    const statements = statementsOfRoleNamed(template, 'DDAPortalAccessRole');
    const inference = statements.filter((s) => s.Sid === 'InferenceResultsAccess');
    expect(inference).toHaveLength(1);
    expect(actionsOf(inference[0]).sort()).toEqual(['s3:GetBucketLocation', 's3:GetObject', 's3:ListBucket']);
  });
});
