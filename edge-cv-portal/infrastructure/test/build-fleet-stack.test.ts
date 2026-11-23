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
 * - The optional NAT gateway (CDK context buildFleetNatGateway) is off by
 *   default. On, it is one NAT gateway with its Elastic IP in the second
 *   public subnet, the first subnet's default route points at it, and
 *   nothing else in the template changes.
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

/** Synthesizes the BuildFleet stack with the given CDK context. */
function synthBuildFleet(context: Record<string, unknown> = {}): Template {
  const app = new cdk.App({ context });
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

  return Template.fromStack(stack);
}

beforeAll(() => {
  template = synthBuildFleet();
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

describe('host-settled gate read (ephemeral-runner-patch-reboot P0-A, P1)', () => {
  const ACTION = 'ssm:DescribeInstanceAssociationsStatus';

  test('the dispatcher role allows ssm:DescribeInstanceAssociationsStatus', () => {
    const granting = policyStatementsForRole('BuildDispatcherRole').filter(
      (s) => asArray(s.Action).includes(ACTION),
    );
    expect(granting.length).toBeGreaterThan(0);
    for (const statement of granting) {
      expect(statement.Effect).toBe('Allow');
      // A read-only grant on its own: nothing rides along with it.
      expect(asArray(statement.Action)).toEqual([ACTION]);
      expect(asArray(statement.Resource)).toEqual(['*']);
    }
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

describe('optional NAT gateway (CDK context buildFleetNatGateway, default off)', () => {
  // Both synthesized here rather than reusing `template`, which an earlier
  // test changes in place (it sorts an event pattern's status array), so the
  // comparison below sees only what the flag changes.
  let offTemplate: Template;
  let natTemplate: Template;

  beforeAll(() => {
    offTemplate = synthBuildFleet();
    natTemplate = synthBuildFleet({ buildFleetNatGateway: 'true' });
  }, 240_000);

  const resourcesOf = (t: Template): { [id: string]: any } => t.toJSON().Resources;
  const outputsOf = (t: Template): { [id: string]: any } => t.toJSON().Outputs ?? {};

  /** The logical ids of every resource of one type. */
  const idsOfType = (t: Template, type: string): string[] =>
    Object.keys(t.findResources(type)).sort();

  /** The logical id of the one resource of a type. */
  function onlyIdOfType(t: Template, type: string): string {
    const ids = idsOfType(t, type);
    expect(ids).toHaveLength(1);
    return ids[0];
  }

  /** The 0.0.0.0/0 route of public subnet 1 or 2, found through its route table. */
  function defaultRoute(t: Template, subnet: 1 | 2): [string, any] {
    const routes = Object.entries(t.findResources('AWS::EC2::Route')).filter(
      ([, route]: [string, any]) =>
        route.Properties.DestinationCidrBlock === '0.0.0.0/0' &&
        JSON.stringify(route.Properties.RouteTableId).includes(`buildpublicSubnet${subnet}`),
    );
    expect(routes).toHaveLength(1);
    return routes[0];
  }

  test('off by default: no NAT gateway or Elastic IP, both default routes use the internet gateway, no output', () => {
    expect(idsOfType(offTemplate, 'AWS::EC2::NatGateway')).toEqual([]);
    expect(idsOfType(offTemplate, 'AWS::EC2::EIP')).toEqual([]);
    const igw = onlyIdOfType(offTemplate, 'AWS::EC2::InternetGateway');
    for (const subnet of [1, 2] as const) {
      const [, route] = defaultRoute(offTemplate, subnet);
      expect(route.Properties.GatewayId).toEqual({ Ref: igw });
      expect(route.Properties.NatGatewayId).toBeUndefined();
    }
    expect(outputsOf(offTemplate).BuildNatGatewayId).toBeUndefined();
  });

  test('on: one NAT gateway with its own Elastic IP, in the second public subnet', () => {
    const eip = onlyIdOfType(natTemplate, 'AWS::EC2::EIP');
    const nat = onlyIdOfType(natTemplate, 'AWS::EC2::NatGateway');
    expect(resourcesOf(natTemplate)[eip].Properties.Domain).toBe('vpc');
    const subnet2 = idsOfType(natTemplate, 'AWS::EC2::Subnet').filter((id) =>
      id.includes('buildpublicSubnet2'),
    );
    expect(subnet2).toHaveLength(1);
    const natProperties = resourcesOf(natTemplate)[nat].Properties;
    expect(natProperties.SubnetId).toEqual({ Ref: subnet2[0] });
    expect(natProperties.AllocationId).toEqual({ 'Fn::GetAtt': [eip, 'AllocationId'] });
  });

  test("on: the first subnet's default route goes to the NAT gateway, the second keeps the internet gateway", () => {
    const nat = onlyIdOfType(natTemplate, 'AWS::EC2::NatGateway');
    const igw = onlyIdOfType(natTemplate, 'AWS::EC2::InternetGateway');
    const [, route1] = defaultRoute(natTemplate, 1);
    expect(route1.Properties.NatGatewayId).toEqual({ Ref: nat });
    expect(route1.Properties.GatewayId).toBeUndefined();
    const [, route2] = defaultRoute(natTemplate, 2);
    expect(route2.Properties.GatewayId).toEqual({ Ref: igw });
    expect(route2.Properties.NatGatewayId).toBeUndefined();
  });

  test('on: adds only the Elastic IP and the NAT gateway, and changes only the first default route', () => {
    const off = resourcesOf(offTemplate);
    const on = resourcesOf(natTemplate);
    const added = Object.keys(on).filter((id) => !(id in off)).sort();
    expect(added).toEqual(
      [onlyIdOfType(natTemplate, 'AWS::EC2::EIP'), onlyIdOfType(natTemplate, 'AWS::EC2::NatGateway')].sort(),
    );
    expect(Object.keys(off).filter((id) => !(id in on))).toEqual([]);
    // The same logical id, so the switch replaces that route in place.
    const [route1] = defaultRoute(offTemplate, 1);
    expect(defaultRoute(natTemplate, 1)[0]).toBe(route1);
    for (const id of Object.keys(off)) {
      if (id === route1) continue;
      expect({ id, resource: on[id] }).toEqual({ id, resource: off[id] });
    }
  });

  test('no inbound rule, with the gateway off or on', () => {
    for (const t of [offTemplate, natTemplate]) {
      expect(idsOfType(t, 'AWS::EC2::SecurityGroupIngress')).toEqual([]);
      for (const group of Object.values(t.findResources('AWS::EC2::SecurityGroup'))) {
        expect((group as any).Properties.SecurityGroupIngress).toBeUndefined();
      }
    }
  });

  test('on: the BuildNatGatewayId output names the NAT gateway and has no export', () => {
    const output = outputsOf(natTemplate).BuildNatGatewayId;
    expect(output.Value).toEqual({ Ref: onlyIdOfType(natTemplate, 'AWS::EC2::NatGateway') });
    expect(output.Export).toBeUndefined();
    expect(Object.keys(outputsOf(natTemplate)).sort()).toEqual(
      [...Object.keys(outputsOf(offTemplate)), 'BuildNatGatewayId'].sort(),
    );
  });
});
