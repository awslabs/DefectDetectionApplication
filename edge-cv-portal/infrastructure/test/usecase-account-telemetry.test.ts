/**
 * The use-case account template's workflow telemetry additions
 * (workflow-telemetry-dashboards design §6.1, §14.2; tasks.md 4.2).
 *
 * - TelemetryCrossAccountCondition gates DDATelemetryRuleErrorRole, its
 *   default policy, the /dda/telemetry/rule-errors log group and the
 *   dda_tel_ingest rule.
 * - The rule filters dda/telemetry/+/+/+ (no #), carries the §6.1 SQL and a
 *   role-less Lambda action on the Portal telemetry ingest alias of this account.
 * - The error role trusts iot.amazonaws.com conditioned on this account and
 *   the dda_tel_* rules, with three logs actions on the error group.
 * - DDAPortalAccessRole carries TelemetryLiveFrameExpiry exactly once,
 *   unconditionally, and the two shared Sids still exactly once each.
 *
 * Synth setup mirrors usecase-account-debug.test.ts (a concrete cross-account env).
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import {
  UseCaseAccountStack,
  TELEMETRY_CONVENTION_RULE_READ_SID,
  TELEMETRY_LIVE_FRAME_EXPIRY_SID,
  TELEMETRY_MEDIA_RETENTION_READ_SID,
} from '../lib/usecase-account-stack';

const PORTAL_ACCOUNT = '222222222222';
const USECASE_ACCOUNT = '333333333333';
const REGION = 'us-east-1';
const RULE_SQL =
  "SELECT encode(*, 'base64') AS p, topic() AS t, principal() AS pr, clientid() AS c, timestamp() AS r, newuuid() AS m FROM 'dda/telemetry/+/+/+'";

let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const stack = new UseCaseAccountStack(app, 'UseCase', {
    env: { account: USECASE_ACCOUNT, region: REGION },
    portalAccountId: PORTAL_ACCOUNT,
    externalId: 'test-external-id',
  });
  template = Template.fromStack(stack);
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

function roleNamed(roleName: string): [string, any] {
  const roles = Object.entries(template.findResources('AWS::IAM::Role')).filter(
    ([, r]: [string, any]) => r.Properties.RoleName === roleName,
  );
  expect(roles).toHaveLength(1);
  return roles[0] as [string, any];
}

function statementsOfRoleNamed(roleName: string): any[] {
  const [roleId] = roleNamed(roleName);
  const policies = Object.values(template.findResources('AWS::IAM::Policy')) as any[];
  return policies
    .filter((p) => (p.Properties.Roles ?? []).some((ref: any) => ref.Ref === roleId))
    .flatMap((p) => p.Properties.PolicyDocument?.Statement ?? []);
}

describe('the condition-gated telemetry resources', () => {
  test('DDATelemetryRuleErrorRole, its default policy, the error log group and the rule carry TelemetryCrossAccountCondition', () => {
    const conditions = template.toJSON().Conditions ?? {};
    expect(conditions.TelemetryCrossAccountCondition).toBeDefined();
    expect(flat(conditions.TelemetryCrossAccountCondition)).toContain('Fn::Not');
    expect(flat(conditions.TelemetryCrossAccountCondition)).toContain(PORTAL_ACCOUNT);

    const [roleId, role] = roleNamed('DDATelemetryRuleErrorRole');
    expect(role.Condition).toBe('TelemetryCrossAccountCondition');
    const policies = (Object.values(template.findResources('AWS::IAM::Policy')) as any[]).filter(
      (p) => (p.Properties.Roles ?? []).some((ref: any) => ref.Ref === roleId),
    );
    expect(policies).toHaveLength(1);
    expect(policies[0].Condition).toBe('TelemetryCrossAccountCondition');

    const groups = (Object.values(template.findResources('AWS::Logs::LogGroup')) as any[]).filter(
      (g) => g.Properties.LogGroupName === '/dda/telemetry/rule-errors',
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].Condition).toBe('TelemetryCrossAccountCondition');
    expect(groups[0].Properties.RetentionInDays).toBe(14);

    const rules = (Object.values(template.findResources('AWS::IoT::TopicRule')) as any[]).filter(
      (r) => r.Properties.RuleName === 'dda_tel_ingest',
    );
    expect(rules).toHaveLength(1);
    expect(rules[0].Condition).toBe('TelemetryCrossAccountCondition');
  });

  test('the debug and camera-shadow resources keep their own conditions', () => {
    const rules = Object.values(template.findResources('AWS::IoT::TopicRule')) as any[];
    expect(rules.find((r) => r.Properties.RuleName === 'dda_dbg_ingest').Condition).toBe('DebugCrossAccountCondition');
    expect(rules.find((r) => r.Properties.RuleName === 'dda_camera_registry_shadow_documents').Condition).toBe(
      'CameraShadowCrossAccountCondition',
    );
    expect(rules).toHaveLength(3);
  });
});

describe('the dda_tel_ingest rule', () => {
  test('filter without #, the §6.1 SQL, a role-less Lambda action on the Portal telemetry alias of this account, the error action', () => {
    const rule = (Object.values(template.findResources('AWS::IoT::TopicRule')) as any[]).find(
      (r) => r.Properties.RuleName === 'dda_tel_ingest',
    );
    const payload = rule.Properties.TopicRulePayload;
    expect(payload.Sql).toBe(RULE_SQL);
    expect(payload.Sql).toContain("FROM 'dda/telemetry/+/+/+'");
    expect(payload.Sql).not.toContain('#');
    expect(payload.AwsIotSqlVersion).toBe('2016-03-23');
    expect(payload.RuleDisabled).toBe(false);
    expect(payload.Actions).toHaveLength(1);
    expect(Object.keys(payload.Actions[0])).toEqual(['Lambda']);
    expect(Object.keys(payload.Actions[0].Lambda)).toEqual(['FunctionArn']);
    expect(payload.Actions[0].Lambda.FunctionArn).toBe(
      `arn:aws:lambda:${REGION}:${PORTAL_ACCOUNT}:function:dda-portal-telemetry-ingest:acct-${USECASE_ACCOUNT}`,
    );
    expect(payload.ErrorAction.CloudwatchLogs.LogGroupName).toBeDefined();
    expect(payload.ErrorAction.CloudwatchLogs.RoleArn['Fn::GetAtt'][1]).toBe('Arn');
  });
});

describe('DDATelemetryRuleErrorRole', () => {
  test('trusts iot.amazonaws.com conditioned on this account and the dda_tel_* rules; three logs actions on the error group', () => {
    const [, role] = roleNamed('DDATelemetryRuleErrorRole');
    const trust = role.Properties.AssumeRolePolicyDocument.Statement;
    expect(trust).toHaveLength(1);
    expect(trust[0].Principal.Service).toBe('iot.amazonaws.com');
    expect(trust[0].Condition.StringEquals['aws:SourceAccount']).toBe(USECASE_ACCOUNT);
    expect(trust[0].Condition.ArnLike['aws:SourceArn']).toBe(`arn:aws:iot:${REGION}:${USECASE_ACCOUNT}:rule/dda_tel_*`);
    const statements = statementsOfRoleNamed('DDATelemetryRuleErrorRole');
    expect(statements).toHaveLength(1);
    expect(statements[0].Sid).toBe('WriteTelemetryRuleErrors');
    expect(actionsOf(statements[0]).sort()).toEqual(['logs:CreateLogStream', 'logs:DescribeLogStreams', 'logs:PutLogEvents']);
    expect(resourcesOf(statements[0])).toEqual([
      `arn:aws:logs:${REGION}:${USECASE_ACCOUNT}:log-group:/dda/telemetry/rule-errors:*`,
    ]);
  });

  test('no role in the template holds lambda:InvokeFunction on the telemetry ingest function', () => {
    for (const policy of Object.values(template.findResources('AWS::IAM::Policy')) as any[]) {
      for (const statement of policy.Properties.PolicyDocument?.Statement ?? []) {
        if (actionsOf(statement).includes('lambda:InvokeFunction')) {
          expect(flat(resourcesOf(statement))).not.toContain('dda-portal-telemetry-ingest');
        }
      }
    }
    expect(Object.values(template.findResources('AWS::IAM::Role')).some(
      (r: any) => r.Properties.RoleName === 'DDATelemetryRuleRole',
    )).toBe(false);
  });
});

describe('DDAPortalAccessRole', () => {
  test('TelemetryLiveFrameExpiry exactly once, unconditional, on the live prefix of the default results bucket', () => {
    const statements = statementsOfRoleNamed('DDAPortalAccessRole');
    const expiry = statements.filter((s) => s.Sid === TELEMETRY_LIVE_FRAME_EXPIRY_SID);
    expect(expiry).toHaveLength(1);
    expect(expiry[0].Effect).toBe('Allow');
    expect(actionsOf(expiry[0])).toEqual(['s3:DeleteObject']);
    expect(resourcesOf(expiry[0])).toEqual([`arn:aws:s3:::dda-inference-results-${USECASE_ACCOUNT}/dda-telemetry/live/*`]);
    expect(expiry[0].Condition).toBeUndefined();
    const [roleId] = roleNamed('DDAPortalAccessRole');
    const carriers = (Object.values(template.findResources('AWS::IAM::Policy')) as any[]).filter(
      (p) => (p.Properties.Roles ?? []).some((ref: any) => ref.Ref === roleId)
        && flat(p.Properties.PolicyDocument).includes(TELEMETRY_LIVE_FRAME_EXPIRY_SID),
    );
    expect(carriers).toHaveLength(1);
    expect(carriers[0].Condition).toBeUndefined();
    // No other DeleteObject on the results bucket.
    const deletes = statements.filter((s) => actionsOf(s).includes('s3:DeleteObject')
      && flat(resourcesOf(s)).includes('dda-inference-results-'));
    expect(deletes).toHaveLength(1);
  });

  test('the two shared Sids are still exactly once each, as the debug plan wrote them', () => {
    const statements = statementsOfRoleNamed('DDAPortalAccessRole');
    const retention = statements.filter((s) => s.Sid === TELEMETRY_MEDIA_RETENTION_READ_SID);
    const rules = statements.filter((s) => s.Sid === TELEMETRY_CONVENTION_RULE_READ_SID);
    expect(retention).toHaveLength(1);
    expect(actionsOf(retention[0])).toEqual(['s3:GetLifecycleConfiguration', 's3:GetBucketVersioning']);
    expect(rules).toHaveLength(1);
    expect(resourcesOf(rules[0])).toEqual([
      `arn:aws:iot:*:${USECASE_ACCOUNT}:rule/dda_tel_ingest`,
      `arn:aws:iot:*:${USECASE_ACCOUNT}:rule/dda_dbg_ingest`,
    ]);
  });
});
