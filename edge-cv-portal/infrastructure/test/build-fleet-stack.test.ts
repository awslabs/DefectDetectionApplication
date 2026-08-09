/**
 * Static infrastructure assertions for the BuildFleetStack
 * (portal-build-fleet-and-workflow-gates task 10.2).
 *
 * Covers the CDK half of the build fleet requirements:
 *
 * - The `/dda/portal-builds` log group retains build output for at least
 *   90 days, and the BuildJobs table expires items via the `ttl`
 *   attribute that build_jobs.py sets to a TTL of at least 90 days
 *   (Requirements 3.4, 4.4).
 * - All five EventBridge rules exist with the expected schedule/event
 *   patterns (1-minute dispatcher tick, dda.portal.builds phase events,
 *   EC2 instance state-change, spot interruption, SSM command status),
 *   captured in a stack snapshot.
 * - The handler IAM policies scope every mutating EC2/SSM action with the
 *   dda-build tag-namespace condition keys (aws:RequestTag /
 *   aws:ResourceTag / ec2:CreateAction / iam:PassedToService /
 *   ssm:resourceTag).
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as fs from 'fs';
import * as path from 'path';
import { BuildFleetStack } from '../lib/build-fleet-stack';

// Synthesized once: asset staging for the Lambda code/layer is expensive.
let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const deps = new cdk.Stack(app, 'Deps');
  const table = (id: string) =>
    new dynamodb.Table(deps, id, {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
    });

  const stack = new BuildFleetStack(app, 'BuildFleet', {
    userRolesTable: table('UserRoles'),
    auditLogTable: table('AuditLog'),
    settingsTable: table('Settings'),
    userPool: new cognito.UserPool(deps, 'Pool'),
    restApiId: 'testrestapi',
    restApiRootResourceId: 'testrootresource',
    apiStageName: 'v1',
  });

  template = Template.fromStack(stack);
}, 120_000);

/** Flattens a statement's Action / Resource entries to arrays. */
const asArray = (value: any): any[] =>
  value === undefined ? [] : Array.isArray(value) ? value : [value];

/** Serialized form of a resource entry (resolves Fn::Join token structure). */
const resourceText = (resource: any): string =>
  typeof resource === 'string' ? resource : JSON.stringify(resource);

/** Every IAM policy statement in the synthesized stack. */
function allPolicyStatements(): any[] {
  const statements: any[] = [];
  for (const policy of Object.values(
    template.findResources('AWS::IAM::Policy'),
  )) {
    statements.push(...(policy as any).Properties.PolicyDocument.Statement);
  }
  return statements;
}

/** All EventBridge rules in the synthesized stack, keyed by rule name. */
function rulesByName(): { [name: string]: any } {
  const byName: { [name: string]: any } = {};
  for (const resource of Object.values(
    template.findResources('AWS::Events::Rule'),
  )) {
    byName[(resource as any).Properties.Name] = resource;
  }
  return byName;
}

describe('build log retention and job TTL (Requirements 3.4, 4.4)', () => {
  test('/dda/portal-builds log group retains for at least 90 days', () => {
    const logGroups = Object.values(
      template.findResources('AWS::Logs::LogGroup'),
    );
    const buildLogGroup: any = logGroups.find(
      (lg: any) => lg.Properties.LogGroupName === '/dda/portal-builds',
    );
    expect(buildLogGroup).toBeDefined();
    expect(buildLogGroup.Properties.RetentionInDays).toBeGreaterThanOrEqual(90);
    // The build record must survive stack replacement.
    expect(buildLogGroup.DeletionPolicy).toBe('Retain');
  });

  test('BuildJobs table expires items via the ttl attribute', () => {
    const tables = Object.values(
      template.findResources('AWS::DynamoDB::Table'),
    );
    const jobsTable: any = tables.find(
      (t: any) => t.Properties.TableName === 'dda-portal-build-jobs',
    );
    expect(jobsTable).toBeDefined();
    expect(jobsTable.Properties.TimeToLiveSpecification).toEqual({
      AttributeName: 'ttl',
      Enabled: true,
    });
  });

  test('build_jobs.py writes a job TTL of at least 90 days', () => {
    // The TTL value itself is applied per item by the handler
    // (created_at + JOB_TTL_DAYS); pin the constant to the >= 90-day
    // retention floor here so the table TTL assertion above stays honest.
    const source = fs.readFileSync(
      path.join(__dirname, '../../backend/functions/build_jobs.py'),
      'utf8',
    );
    const match = source.match(/^JOB_TTL_DAYS\s*=\s*(\d+)\s*$/m);
    expect(match).not.toBeNull();
    expect(Number(match![1])).toBeGreaterThanOrEqual(90);
  });
});

describe('EventBridge wiring (Requirements 3.1, 3.5, 5.1, 6.9)', () => {
  test('all five build rules exist', () => {
    expect(Object.keys(rulesByName()).sort()).toEqual(
      [
        'dda-portal-build-dispatcher-tick',
        'dda-portal-build-phase-events',
        'dda-portal-build-instance-state',
        'dda-portal-build-spot-interruption',
        'dda-portal-build-ssm-command-status',
      ].sort(),
    );
  });

  test('dispatcher tick runs on a 1-minute schedule', () => {
    const rule = rulesByName()['dda-portal-build-dispatcher-tick'];
    expect(rule.Properties.ScheduleExpression).toBe('rate(1 minute)');
    expect(rule.Properties.Targets).toHaveLength(1);
  });

  test('agent phase events route dda.portal.builds to build_events', () => {
    const rule = rulesByName()['dda-portal-build-phase-events'];
    expect(rule.Properties.EventPattern.source).toEqual(['dda.portal.builds']);
  });

  test('EC2 state-change and spot interruption rules match aws.ec2', () => {
    const stateRule = rulesByName()['dda-portal-build-instance-state'];
    expect(stateRule.Properties.EventPattern).toEqual({
      source: ['aws.ec2'],
      'detail-type': ['EC2 Instance State-change Notification'],
    });

    const spotRule = rulesByName()['dda-portal-build-spot-interruption'];
    expect(spotRule.Properties.EventPattern).toEqual({
      source: ['aws.ec2'],
      'detail-type': ['EC2 Spot Instance Interruption Warning'],
    });
  });

  test('SSM command status rule matches ALL terminal command statuses (build-fleet-execution-failures Req 2.1)', () => {
    const rule = rulesByName()['dda-portal-build-ssm-command-status'];
    const pattern = rule.Properties.EventPattern;
    expect(pattern.source).toEqual(['aws.ssm']);
    // `Success` is included so a missing agent result after a
    // successful command can be settled; every pre-existing failure
    // status is preserved.
    expect(pattern.detail.status.sort()).toEqual(
      ['Cancelled', 'Failed', 'Success', 'TimedOut'].sort(),
    );
    for (const preserved of ['Failed', 'TimedOut', 'Cancelled']) {
      expect(pattern.detail.status).toContain(preserved);
    }
  });

  test('EventBridge rules stack snapshot', () => {
    // Full definition snapshot of the five rules: schedule, event
    // patterns, and Lambda target wiring.
    expect(rulesByName()).toMatchSnapshot();
  });
});

/** Policy statements attached to one handler role (logical-id fragment). */
function policyStatementsForRole(roleIdFragment: string): any[] {
  const statements: any[] = [];
  for (const policy of Object.values(
    template.findResources('AWS::IAM::Policy'),
  )) {
    const roles = (policy as any).Properties.Roles ?? [];
    const attached = roles.some((r: any) =>
      JSON.stringify(r).includes(roleIdFragment),
    );
    if (attached) {
      statements.push(...(policy as any).Properties.PolicyDocument.Statement);
    }
  }
  return statements;
}

describe('reconciliation least privilege (build-fleet-execution-failures Requirements 2.1, 2.5, 2.12, 3.5)', () => {
  const RECONCILIATION_READS = ['ssm:GetCommandInvocation', 'ssm:ListCommands'];

  test('the events consumer gets exactly the read actions required for invocation recovery', () => {
    const statements = policyStatementsForRole('BuildEventsRole');
    for (const action of RECONCILIATION_READS) {
      expect(
        statements.some((s) => asArray(s.Action).includes(action)),
      ).toBe(true);
    }
  });

  test('the dispatcher gets ListCommands for ambiguous-send recovery', () => {
    const statements = policyStatementsForRole('BuildDispatcherRole');
    for (const action of RECONCILIATION_READS) {
      expect(
        statements.some((s) => asArray(s.Action).includes(action)),
      ).toBe(true);
    }
  });

  test('the events consumer receives NO mutating EC2/SSM action', () => {
    // Least privilege (Req 2.12): the consumer reads invocation
    // evidence; it never sends commands, launches, or terminates.
    const forbidden = [
      'ssm:SendCommand',
      'ssm:CancelCommand',
      'ec2:RunInstances',
      'ec2:StartInstances',
      'ec2:StopInstances',
      'ec2:TerminateInstances',
    ];
    const statements = policyStatementsForRole('BuildEventsRole');
    for (const statement of statements) {
      for (const action of asArray(statement.Action)) {
        expect(forbidden).not.toContain(action);
      }
    }
  });

  test('the reconciliation read statements grant only read actions', () => {
    // The statement carrying GetCommandInvocation/ListCommands must not
    // smuggle a mutating action alongside the reads.
    const readStatements = allPolicyStatements().filter((s) =>
      asArray(s.Action).some((a: string) => RECONCILIATION_READS.includes(a)),
    );
    expect(readStatements.length).toBeGreaterThan(0);
    for (const statement of readStatements) {
      for (const action of asArray(statement.Action)) {
        expect(action).toMatch(
          /^ssm:(GetCommandInvocation|ListCommands|DescribeInstanceInformation)$/,
        );
      }
    }
  });

  test('the one-minute dispatcher schedule is unchanged', () => {
    const rule = rulesByName()['dda-portal-build-dispatcher-tick'];
    expect(rule.Properties.ScheduleExpression).toBe('rate(1 minute)');
  });
});

describe('IAM scoping condition keys (design §10)', () => {
  test.each([
    ['dda-build:ephemeral'], // dispatcher provisions ephemeral runners
    ['dda-build:fleet'], // fleet handler launches dedicated servers
  ])('RunInstances requires the aws:RequestTag/%s launch tag', (tagKey) => {
    const statements = allPolicyStatements().filter((s) =>
      asArray(s.Action).includes('ec2:RunInstances'),
    );
    expect(statements.length).toBeGreaterThan(0);
    const tagged = statements.some(
      (s) =>
        s.Condition?.StringEquals?.[`aws:RequestTag/${tagKey}`] === 'true' &&
        asArray(s.Resource).some((r) =>
          resourceText(r).includes(':instance/'),
        ),
    );
    expect(tagged).toBe(true);
  });

  test('every RunInstances grant on instances is request-tag conditioned', () => {
    // Ancillary RunInstances resources (image/volume/eni/sg/subnet) carry
    // no tag condition by design; the instance ARN statements all must.
    const statements = allPolicyStatements().filter(
      (s) =>
        asArray(s.Action).includes('ec2:RunInstances') &&
        asArray(s.Resource).some((r) => resourceText(r).includes(':instance/')),
    );
    expect(statements.length).toBeGreaterThan(0);
    for (const statement of statements) {
      const requestTags = Object.keys(
        statement.Condition?.StringEquals ?? {},
      ).filter((key) => key.startsWith('aws:RequestTag/dda-build:'));
      expect(requestTags.length).toBeGreaterThan(0);
    }
  });

  test('instance lifecycle actions are resource-tag conditioned', () => {
    const lifecycleActions = [
      'ec2:StartInstances',
      'ec2:StopInstances',
      'ec2:TerminateInstances',
    ];
    const statements = allPolicyStatements().filter((s) =>
      asArray(s.Action).some((a: string) => lifecycleActions.includes(a)),
    );
    expect(statements.length).toBeGreaterThan(0);
    for (const statement of statements) {
      const resourceTags = Object.keys(
        statement.Condition?.StringEquals ?? {},
      ).filter((key) => key.startsWith('aws:ResourceTag/dda-build:'));
      expect(resourceTags.length).toBeGreaterThan(0);
    }
    // Both tag namespaces are covered (dispatcher: ephemeral; fleet: fleet).
    const conditionKeys = statements.flatMap((s) =>
      Object.keys(s.Condition?.StringEquals ?? {}),
    );
    expect(conditionKeys).toContain('aws:ResourceTag/dda-build:ephemeral');
    expect(conditionKeys).toContain('aws:ResourceTag/dda-build:fleet');
  });

  test('CreateTags is confined to the RunInstances launch action', () => {
    const statements = allPolicyStatements().filter((s) =>
      asArray(s.Action).includes('ec2:CreateTags'),
    );
    expect(statements.length).toBeGreaterThan(0);
    for (const statement of statements) {
      expect(statement.Condition?.StringEquals?.['ec2:CreateAction']).toBe(
        'RunInstances',
      );
    }
  });

  test('iam:PassRole is limited to EC2 and the build instance role', () => {
    const statements = allPolicyStatements().filter((s) =>
      asArray(s.Action).includes('iam:PassRole'),
    );
    expect(statements.length).toBeGreaterThan(0);
    for (const statement of statements) {
      expect(
        statement.Condition?.StringEquals?.['iam:PassedToService'],
      ).toBe('ec2.amazonaws.com');
      // The grant targets the stack's build instance role (GetAtt Arn).
      expect(resourceText(statement.Resource)).toContain('BuildInstanceRole');
    }
  });

  test('ssm:SendCommand on instances requires a dda-build resource tag', () => {
    const instanceSends = allPolicyStatements().filter(
      (s) =>
        asArray(s.Action).includes('ssm:SendCommand') &&
        asArray(s.Resource).some((r) => resourceText(r).includes(':instance/')),
    );
    expect(instanceSends.length).toBeGreaterThan(0);
    for (const statement of instanceSends) {
      const tagConditions = Object.keys(
        statement.Condition?.StringEquals ?? {},
      ).filter((key) => key.startsWith('ssm:resourceTag/dda-build:'));
      expect(tagConditions.length).toBeGreaterThan(0);
    }
    // Both tag namespaces are reachable by SendCommand (agent dispatch on
    // dedicated servers and ephemeral runners alike).
    const conditionKeys = instanceSends.flatMap((s) =>
      Object.keys(s.Condition?.StringEquals ?? {}),
    );
    expect(conditionKeys).toContain('ssm:resourceTag/dda-build:fleet');
    expect(conditionKeys).toContain('ssm:resourceTag/dda-build:ephemeral');
  });

  test('non-instance ssm:SendCommand grants target only AWS-RunShellScript', () => {
    const documentSends = allPolicyStatements().filter(
      (s) =>
        asArray(s.Action).includes('ssm:SendCommand') &&
        asArray(s.Resource).every(
          (r) => !resourceText(r).includes(':instance/'),
        ),
    );
    expect(documentSends.length).toBeGreaterThan(0);
    for (const statement of documentSends) {
      for (const resource of asArray(statement.Resource)) {
        expect(resourceText(resource)).toContain(
          'document/AWS-RunShellScript',
        );
      }
    }
  });
});
