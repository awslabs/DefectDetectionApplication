/**
 * The CloudFormation execution policies manage KMS keys and EC2 network
 * resources only when they carry dda-portal:managed=true
 * (iam/portal-cfn-execution-app-policy.json, -network-policy.json), so the
 * portal stacks must create every such resource with that tag.
 *
 * - tagExecPolicyManagedResources tags each governed type, and only those.
 * - The stacks that hold KMS keys or network resources (node designer, test
 *   runner, build fleet), tagged as bin/app.ts tags the app, carry the tag on
 *   every governed resource, and every other KMS or EC2 type they hold is one
 *   the policies authorize through a tagged parent.
 * - bin/app.ts applies the tags to the whole app.
 * - The policy files, MANAGED_TAG_TYPES in iam/setup-account.sh and the
 *   governed type list agree.
 */
import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as fs from 'fs';
import * as path from 'path';
import { BuildFleetStack } from '../lib/build-fleet-stack';
import {
  EXEC_POLICY_TAGGED_RESOURCE_TYPES,
  MANAGED_TAG_KEY,
  MANAGED_TAG_VALUE,
  tagExecPolicyManagedResources,
} from '../lib/managed-tag';
import { NodeDesignerStack } from '../lib/node-designer-stack';
import { TestRunnerStack } from '../lib/test-runner-stack';

const IAM_DIR = path.join(__dirname, '..', '..', 'iam');

/** KMS and EC2 types with no tags of their own; the policies authorize them through a tagged parent. */
const AUTHORIZED_THROUGH_PARENT = [
  'AWS::KMS::Alias',
  'AWS::EC2::SubnetRouteTableAssociation',
  'AWS::EC2::Route',
  'AWS::EC2::VPCGatewayAttachment',
  'AWS::EC2::SecurityGroupIngress',
  'AWS::EC2::SecurityGroupEgress',
];

type Resource = { Type: string; Properties?: { Tags?: { Key: string; Value: string }[] } };

const managedTag = (resource: Resource): string | undefined =>
  (resource.Properties?.Tags ?? []).find((t) => t.Key === MANAGED_TAG_KEY)?.Value;

const resourcesOf = (stack: cdk.Stack): Record<string, Resource> =>
  cdk.assertions.Template.fromStack(stack).toJSON().Resources ?? {};

describe('tagExecPolicyManagedResources', () => {
  test('tags each governed type and nothing else', () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'Fixture');
    const vpc = new ec2.Vpc(stack, 'Vpc', {
      maxAzs: 2,
      natGateways: 0,
      restrictDefaultSecurityGroup: false,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC },
        { name: 'isolated', subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      ],
    });
    vpc.addGatewayEndpoint('S3', { service: ec2.GatewayVpcEndpointAwsService.S3 });
    vpc.addInterfaceEndpoint('Logs', { service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS });
    new kms.Key(stack, 'Key', { alias: 'dda-portal/fixture' });
    new s3.Bucket(stack, 'Bucket');
    tagExecPolicyManagedResources(app);

    const resources = Object.values(resourcesOf(stack));
    for (const type of EXEC_POLICY_TAGGED_RESOURCE_TYPES) {
      const ofType = resources.filter((r) => r.Type === type);
      expect(ofType.length).toBeGreaterThan(0);
      for (const r of ofType) expect(managedTag(r)).toBe(MANAGED_TAG_VALUE);
    }
    const bucket = resources.find((r) => r.Type === 'AWS::S3::Bucket')!;
    expect(managedTag(bucket)).toBeUndefined();
  });
});

describe('portal stacks with KMS keys or network resources', () => {
  let stacks: cdk.Stack[];

  beforeAll(() => {
    const app = new cdk.App();
    const deps = new cdk.Stack(app, 'Deps');
    const table = (id: string) =>
      new dynamodb.Table(deps, id, { partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING } });
    const userPool = new cognito.UserPool(deps, 'Pool');
    const artifacts = new s3.Bucket(deps, 'Artifacts');
    const api = { restApiId: 'testrestapi', restApiRootResourceId: 'testrootresource', apiStageName: 'v1' };
    stacks = [
      new NodeDesignerStack(app, 'NodeDesigner', {
        portalArtifactsBucket: artifacts,
        useCasesTable: table('UseCases'),
        userRolesTable: table('UserRoles'),
        auditLogTable: table('AuditLog'),
        settingsTable: table('Settings'),
        workflowsTable: table('Workflows'),
        workflowVersionsTable: table('WorkflowVersions'),
        testDatasetsTable: table('TestDatasets'),
        trustedUseCaseAccountIds: ['111111111111'],
        userPool,
        ...api,
      }),
      new TestRunnerStack(app, 'TestRunner', {
        testRunsTable: table('TestRuns'),
        portalArtifactsBucket: artifacts,
      }),
      new BuildFleetStack(app, 'BuildFleet', {
        userRolesTable: table('BuildUserRoles'),
        auditLogTable: table('BuildAuditLog'),
        settingsTable: table('BuildSettings'),
        userPool,
        ...api,
      }),
    ];
    tagExecPolicyManagedResources(app);
  }, 300_000);

  test('every governed resource carries dda-portal:managed=true', () => {
    let governed = 0;
    for (const stack of stacks) {
      for (const [id, r] of Object.entries(resourcesOf(stack))) {
        if (!EXEC_POLICY_TAGGED_RESOURCE_TYPES.includes(r.Type)) continue;
        governed += 1;
        expect({ stack: stack.stackName, id, tag: managedTag(r) })
          .toEqual({ stack: stack.stackName, id, tag: MANAGED_TAG_VALUE });
      }
    }
    expect(governed).toBeGreaterThan(20);
  });

  test('every other KMS or EC2 type is authorized through a tagged parent', () => {
    for (const stack of stacks) {
      for (const [id, r] of Object.entries(resourcesOf(stack))) {
        if (!/^AWS::(KMS|EC2)::/.test(r.Type) || EXEC_POLICY_TAGGED_RESOURCE_TYPES.includes(r.Type)) continue;
        expect({ stack: stack.stackName, id, type: r.Type, known: AUTHORIZED_THROUGH_PARENT.includes(r.Type) })
          .toEqual({ stack: stack.stackName, id, type: r.Type, known: true });
      }
    }
  });
});

describe('wiring and policy files', () => {
  const policy = (name: string) => JSON.parse(fs.readFileSync(path.join(IAM_DIR, name), 'utf-8'));
  const statement = (doc: any, sid: string) => doc.Statement.find((s: any) => s.Sid === sid);
  const asArray = (v: any): string[] => (Array.isArray(v) ? v : [v]);

  test('bin/app.ts tags the whole app before it synthesizes', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'bin', 'app.ts'), 'utf-8');
    const call = source.indexOf('tagExecPolicyManagedResources(app);');
    expect(call).toBeGreaterThan(0);
    expect(call).toBeLessThan(source.indexOf('app.synth();'));
  });

  test('setup-account.sh tags exactly the governed types of an earlier deploy', () => {
    const script = fs.readFileSync(path.join(IAM_DIR, 'setup-account.sh'), 'utf-8');
    const match = script.match(/MANAGED_TAG_TYPES=\(([^)]*)\)/);
    expect(match).not.toBeNull();
    expect(match![1].trim().split(/\s+/).sort()).toEqual([...EXEC_POLICY_TAGGED_RESOURCE_TYPES].sort());
  });

  test('the network policy creates only tagged resources and tags only at creation or when tagged', () => {
    const doc = policy('portal-cfn-execution-network-policy.json');
    const requestTagged = ['CreateTaggedVpcsAndGateways', 'CreateTaggedVpcChildren'];
    const createActions = requestTagged.flatMap((sid) => asArray(statement(doc, sid).Action));
    for (const sid of requestTagged) {
      expect(statement(doc, sid).Condition).toEqual({
        StringEquals: { [`aws:RequestTag/${MANAGED_TAG_KEY}`]: MANAGED_TAG_VALUE } });
    }
    const atCreation = statement(doc, 'TagOnlyAtCreation');
    expect(atCreation.Action).toBe('ec2:CreateTags');
    expect([...atCreation.Condition.StringEquals['ec2:CreateAction']].sort())
      .toEqual(createActions.map((a) => a.replace('ec2:', '')).sort());
    const manage = statement(doc, 'ManageTaggedNetworking');
    expect(manage.Condition).toEqual({
      StringEquals: { [`aws:ResourceTag/${MANAGED_TAG_KEY}`]: MANAGED_TAG_VALUE } });
    // Every EC2 action outside these statements is a read or a rule of a tagged group.
    for (const s of doc.Statement) {
      if (s.Condition) continue;
      expect(s.Sid).toBe('RulesOfTaggedSecurityGroups');
      expect(asArray(s.Resource)).toEqual(['arn:aws:ec2:REGION:ACCOUNT_ID:security-group-rule/*']);
    }
  });

  test('KMS key management needs the tag on the key, and new keys must carry it', () => {
    const doc = policy('portal-cfn-execution-app-policy.json');
    for (const sid of ['DdaKeys', 'EncryptDdaTablesWithDdaKeys']) {
      expect(statement(doc, sid).Condition.StringEquals[`aws:ResourceTag/${MANAGED_TAG_KEY}`]).toBe(MANAGED_TAG_VALUE);
    }
    for (const sid of ['CreateTaggedKeys', 'TagKeysAtCreation']) {
      expect(statement(doc, sid).Condition).toEqual({
        StringEquals: { [`aws:RequestTag/${MANAGED_TAG_KEY}`]: MANAGED_TAG_VALUE } });
    }
    // No other statement grants a KMS action on keys (listing aliases is not one).
    const keyStatements = doc.Statement.filter((s: any) =>
      asArray(s.Resource).some((r) => r.includes(':key/') || r === '*')
      && asArray(s.Action).some((a) => a.startsWith('kms:') && a !== 'kms:ListAliases'));
    expect(keyStatements.map((s: any) => s.Sid).sort()).toEqual(
      ['CreateTaggedKeys', 'DdaKeys', 'EncryptDdaTablesWithDdaKeys', 'TagKeysAtCreation', 'UserPoolKeysThroughCognito'].sort());
    // The platform policy no longer carries the network statement.
    expect(policy('portal-cfn-execution-platform-policy.json').Statement
      .some((s: any) => asArray(s.Action).some((a) => a.startsWith('ec2:')))).toBe(false);
  });
});
