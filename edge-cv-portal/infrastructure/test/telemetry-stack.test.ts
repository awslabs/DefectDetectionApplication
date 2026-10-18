/**
 * Static assertions on EdgeCVPortalTelemetryStack (workflow-telemetry-dashboards
 * design §2.4, §6.1, §6.2, §7, §11.2, §12.3, §14.1, §15.2; tasks.md 4.1).
 *
 * Conventions follow workflow-debug-stack.test.ts: synthesize once in
 * beforeAll with a generous timeout (the functions and three layer assets are
 * staged at synth time), locate resources via template.findResources and
 * assert on raw CloudFormation properties. A second synth with
 * portalRegistryEnforced on covers the environment flag; a third with an
 * injected dataPlaneGrant covers the wired branch; out-of-range deployment
 * values throw at synth.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as contextHelpers from '../lib/context-helpers';
import { StorageStack } from '../lib/storage-stack';
import {
  DataPlaneGrant,
  TELEMETRY_API_RESOURCE_COUNT,
  TELEMETRY_DEPLOYMENT_VALUES,
  TELEMETRY_RULE_SQL,
  TelemetryStack,
  TelemetryStackProps,
  resolveTelemetryValue,
} from '../lib/telemetry-stack';

const TRUSTED_A = '222222222222';
const TRUSTED_B = '333333333333';
const INGEST = 'dda-portal-telemetry-ingest';
const ROLLUP = 'dda-portal-telemetry-rollup';
const API = 'dda-portal-telemetry-api';
const MAINTENANCE = 'dda-portal-telemetry-maintenance';
const FUNCTION_NAMES = [INGEST, ROLLUP, API, MAINTENANCE];

const helpersExportDataPlaneGrant =
  typeof (contextHelpers as unknown as { dataPlaneGrant?: unknown }).dataPlaneGrant === 'function';

const fakeGrant = (allowlist: string[]): DataPlaneGrant => {
  const buckets = allowlist.length ? allowlist : ['*'];
  return {
    objectLevel: buckets.map((b) => `arn:aws:s3:::${b}/*`),
    bucketLevel: buckets.map((b) => `arn:aws:s3:::${b}`),
    objectPrefix: (prefix: string) => buckets.map((b) => `arn:aws:s3:::${b}/${prefix}/*`),
    condition: { StringEquals: { 's3:ResourceTag/dda-portal:managed': 'true' } },
  };
};

let template: Template;
let enforcedTemplate: Template;
let wiredTemplate: Template;
let wiredStack: TelemetryStack;

function build(context?: { [key: string]: unknown }, override?: Partial<TelemetryStackProps>): TelemetryStack {
  const app = new cdk.App({ context });
  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');
  const props: TelemetryStackProps = {
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    devicesTable: storage.devicesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    workflowsTable: storage.workflowsTable,
    workflowVersionsTable: storage.workflowVersionsTable,
    portalArtifactsBucket: storage.portalArtifactsBucket,
    userPool: new cognito.UserPool(deps, 'Pool'),
    trustedUseCaseAccountIds: [TRUSTED_A, TRUSTED_B],
    dataBucketAllowlist: ['customer-data-bucket'],
    restApiId: 'testrestapiid',
    restApiRootResourceId: 'testrootresourceid',
    apiStageName: 'v1',
    ...override,
  };
  return new TelemetryStack(app, 'Telemetry', props);
}

beforeAll(() => {
  template = Template.fromStack(build());
  enforcedTemplate = Template.fromStack(build({ portalRegistryEnforced: 'true' }));
  wiredStack = build(undefined, { dataPlaneGrantOverride: fakeGrant });
  wiredTemplate = Template.fromStack(wiredStack);
}, 300_000);

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function flat(value: unknown): string {
  return JSON.stringify(value);
}

function functionsByName(tpl: Template = template): { [name: string]: [string, any] } {
  const result: { [name: string]: [string, any] } = {};
  for (const [logicalId, resource] of Object.entries(tpl.findResources('AWS::Lambda::Function'))) {
    const name = (resource as any).Properties.FunctionName;
    if (typeof name === 'string') result[name] = [logicalId, resource];
  }
  return result;
}

function roleLogicalIdOf(functionName: string, tpl: Template = template): string {
  const [, fn] = functionsByName(tpl)[functionName];
  return fn.Properties.Role['Fn::GetAtt'][0];
}

function statementsOfRole(roleId: string, tpl: Template = template): any[] {
  const policies = [
    ...Object.values(tpl.findResources('AWS::IAM::Policy')),
    ...Object.values(tpl.findResources('AWS::IAM::ManagedPolicy')),
  ] as any[];
  const fromPolicies = policies
    .filter((p) => p.Properties.Roles?.some((r: any) => r.Ref === roleId))
    .filter((p) => p.Properties.PolicyDocument?.Statement)
    .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
  const role = tpl.findResources('AWS::IAM::Role')[roleId] as any;
  const inline = ((role.Properties.Policies as any[]) ?? []).flatMap(
    (p: any) => p.PolicyDocument?.Statement ?? [],
  );
  return [...fromPolicies, ...inline];
}

function actionsOf(statement: any): string[] {
  return Array.isArray(statement.Action) ? statement.Action : [statement.Action];
}

function resourcesOf(statement: any): any[] {
  return Array.isArray(statement.Resource) ? statement.Resource : [statement.Resource];
}

function allStatements(tpl: Template = template): any[] {
  return Object.keys(tpl.findResources('AWS::IAM::Role')).flatMap((id) => statementsOfRole(id, tpl));
}

function sid(roleId: string, name: string, tpl: Template = template): any {
  const found = statementsOfRole(roleId, tpl).filter((s) => s.Sid === name);
  expect(found).toHaveLength(1);
  return found[0];
}

function tableNamed(name: string): any {
  const tables = Object.values(template.findResources('AWS::DynamoDB::Table')) as any[];
  const table = tables.find((t) => t.Properties.TableName === name);
  expect(table).toBeDefined();
  return table;
}

const EXPECTED_ENV_KEYS = [
  'USECASES_TABLE', 'USER_ROLES_TABLE', 'DEVICES_TABLE', 'AUDIT_LOG_TABLE', 'SETTINGS_TABLE', 'WORKFLOWS_TABLE',
  'WORKFLOW_VERSIONS_TABLE', 'PORTAL_ARTIFACTS_BUCKET', 'PORTAL_ACCOUNT_ID', 'USER_POOL_ID', 'COMPONENT_BUCKET_PREFIX',
  'PORTAL_REGISTRY_ENFORCED', 'TELEMETRY_SUMMARIES_TABLE', 'TELEMETRY_AGGREGATES_TABLE', 'TELEMETRY_EVENTS_TABLE',
  'TELEMETRY_CONFIG_TABLE', 'TELEMETRY_LAKE_BUCKET', 'TELEMETRY_FIREHOSE_SUMMARIES', 'TELEMETRY_FIREHOSE_EVENTS',
  'TELEMETRY_DATA_PLANE_GRANT', 'TELEMETRY_PORTAL_RULE_NAME', 'TELEMETRY_NOW_POLL_SECONDS', 'TELEMETRY_LIVE_MAX_LEASES',
  'TELEMETRY_LIVE_MAX_VIEW_MINUTES', 'TELEMETRY_CLIP_RETENTION_DAYS', 'TELEMETRY_LAKE_RETENTION_DAYS',
  'TELEMETRY_INGEST_CONCURRENCY',
];

// ---------------------------------------------------------------------------
// the rule and the aliases (design §6.1, §6.2)
// ---------------------------------------------------------------------------
describe('the Portal-account rule dda_tel_ingest_portal', () => {
  test('filter dda/telemetry/+/+/+ without #, the §6.1 SQL, a role-less Lambda action and an error action', () => {
    const rules = Object.values(template.findResources('AWS::IoT::TopicRule')) as any[];
    expect(rules).toHaveLength(1);
    const payload = rules[0].Properties.TopicRulePayload;
    expect(rules[0].Properties.RuleName).toBe('dda_tel_ingest_portal');
    expect(payload.Sql).toBe(TELEMETRY_RULE_SQL);
    expect(payload.Sql).toContain("FROM 'dda/telemetry/+/+/+'");
    expect(payload.Sql).not.toContain('#');
    expect(payload.AwsIotSqlVersion).toBe('2016-03-23');
    expect(payload.Actions).toHaveLength(1);
    const action = payload.Actions[0];
    expect(Object.keys(action)).toEqual(['Lambda']);
    expect(Object.keys(action.Lambda)).toEqual(['FunctionArn']);
    expect(Object.keys(template.findResources('AWS::Lambda::Alias'))).toContain(action.Lambda.FunctionArn.Ref);
    expect(payload.ErrorAction.CloudwatchLogs.LogGroupName).toBeDefined();
    expect(payload.ErrorAction.CloudwatchLogs.RoleArn).toBeDefined();
  });

  test('the error role trusts iot.amazonaws.com conditioned on the account and the dda_tel_* rules, with three logs actions', () => {
    const roles = Object.entries(template.findResources('AWS::IAM::Role')).filter(([id]) => id.startsWith('TelemetryRuleErrorRole'));
    expect(roles).toHaveLength(1);
    const [roleId, role] = roles[0] as [string, any];
    const trust = role.Properties.AssumeRolePolicyDocument.Statement[0];
    expect(trust.Principal.Service).toBe('iot.amazonaws.com');
    expect(flat(trust.Condition.StringEquals['aws:SourceAccount'])).toContain('AWS::AccountId');
    expect(flat(trust.Condition.ArnLike['aws:SourceArn'])).toContain(':rule/dda_tel_*');
    const statements = statementsOfRole(roleId);
    expect(statements).toHaveLength(1);
    expect(actionsOf(statements[0]).sort()).toEqual(['logs:CreateLogStream', 'logs:DescribeLogStreams', 'logs:PutLogEvents']);
    const groups = Object.values(template.findResources('AWS::Logs::LogGroup')) as any[];
    const errorGroup = groups.find((g) => g.Properties.LogGroupName === '/dda/telemetry/rule-errors');
    expect(errorGroup.Properties.RetentionInDays).toBe(14);
  });

  test('no role in the stack holds lambda:InvokeFunction on the ingest function or its aliases', () => {
    for (const statement of allStatements()) {
      if (!actionsOf(statement).includes('lambda:InvokeFunction')) continue;
      for (const resource of resourcesOf(statement)) {
        const text = flat(resource);
        expect(text).not.toContain('TelemetryIngest');
        expect(text).not.toContain(INGEST);
        expect(text).not.toContain('IngestAlias');
      }
    }
  });
});

describe('the ingest aliases', () => {
  test('one alias and one permission per trusted account and the Portal account', () => {
    const aliases = Object.entries(template.findResources('AWS::Lambda::Alias')) as [string, any][];
    const names = aliases.map(([, a]) => flat(a.Properties.Name));
    expect(aliases).toHaveLength(3);
    expect(names.filter((n) => n.includes(`acct-${TRUSTED_A}`))).toHaveLength(1);
    expect(names.filter((n) => n.includes(`acct-${TRUSTED_B}`))).toHaveLength(1);
    expect(names.filter((n) => n.includes('AWS::AccountId'))).toHaveLength(1);
    const permissions = (Object.values(template.findResources('AWS::Lambda::Permission')) as any[]).filter(
      (p) => p.Properties.Principal === 'iot.amazonaws.com',
    );
    expect(permissions).toHaveLength(3);
    for (const [aliasId] of aliases) {
      const own = permissions.filter((p) => p.Properties.FunctionName?.Ref === aliasId);
      expect(own).toHaveLength(1);
      expect(own[0].Properties.Action).toBe('lambda:InvokeFunction');
      expect(own[0].Properties.SourceAccount).toBeDefined();
      expect(flat(own[0].Properties.SourceArn)).toContain(':rule/dda_tel_*');
      const sourceAccount = own[0].Properties.SourceAccount;
      if (typeof sourceAccount === 'string') {
        expect(flat(own[0].Properties.SourceArn)).toContain(`:${sourceAccount}:rule/dda_tel_*`);
      } else {
        expect(flat(sourceAccount)).toContain('AWS::AccountId');
      }
    }
    // No permission on the unqualified function for iot.amazonaws.com.
    const fnIds = Object.keys(template.findResources('AWS::Lambda::Function'));
    expect(permissions.some((p) => fnIds.includes(p.Properties.FunctionName?.Ref))).toBe(false);
  });

  test('one EventInvokeConfig per alias with the retry, age and on-failure queue; none on $LATEST', () => {
    const aliasIds = Object.keys(template.findResources('AWS::Lambda::Alias'));
    const configs = Object.values(template.findResources('AWS::Lambda::EventInvokeConfig')) as any[];
    expect(configs).toHaveLength(aliasIds.length);
    const queues = Object.keys(template.findResources('AWS::SQS::Queue'));
    expect(queues).toHaveLength(1);
    const qualified: string[] = [];
    for (const config of configs) {
      const qualifier = config.Properties.Qualifier;
      expect(qualifier).toBeDefined();
      expect(flat(qualifier)).not.toContain('$LATEST');
      expect(qualifier['Fn::Select'][0]).toBe(7);
      const aliasRef = qualifier['Fn::Select'][1]['Fn::Split'][1].Ref;
      expect(aliasIds).toContain(aliasRef);
      qualified.push(aliasRef);
      expect(config.Properties.MaximumRetryAttempts).toBe(2);
      expect(config.Properties.MaximumEventAgeInSeconds).toBe(3600);
      expect(config.Properties.DestinationConfig.OnFailure.Destination['Fn::GetAtt'][0]).toBe(queues[0]);
    }
    expect(qualified.sort()).toEqual([...aliasIds].sort());
  });

  test('the failure queue keeps messages 14 days with managed encryption', () => {
    const queue = Object.values(template.findResources('AWS::SQS::Queue'))[0] as any;
    expect(queue.Properties.QueueName).toBe('dda-portal-telemetry-ingest-failures');
    expect(queue.Properties.MessageRetentionPeriod).toBe(14 * 24 * 3600);
    expect(queue.Properties.SqsManagedSseEnabled).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// functions, layers, environment, event sources (design §2.2, §2.4, §7.3)
// ---------------------------------------------------------------------------
describe('the four functions', () => {
  test('names, runtimes, sizes, the reserved concurrency of the ingest', () => {
    const fns = functionsByName();
    expect(Object.keys(fns).sort()).toEqual([...FUNCTION_NAMES].sort());
    const ingest = fns[INGEST][1].Properties;
    expect(ingest.Runtime).toBe('python3.12');
    expect(ingest.Handler).toBe('telemetry_ingest.handler');
    expect(ingest.MemorySize).toBe(256);
    expect(ingest.Timeout).toBe(20);
    expect(ingest.ReservedConcurrentExecutions).toBe(50);
    expect(fns[ROLLUP][1].Properties.Handler).toBe('telemetry_rollup.handler');
    expect(fns[API][1].Properties.Handler).toBe('telemetry_api.handler');
    expect(fns[API][1].Properties.Timeout).toBe(29);
    expect(fns[MAINTENANCE][1].Properties.Handler).toBe('telemetry_maintenance.handler');
    for (const name of FUNCTION_NAMES) expect(fns[name][1].Properties.Runtime).toBe('python3.12');
  });

  test('the ingest carries the shared, JWT and workflow_core layers; the other three carry two layers', () => {
    const fns = functionsByName();
    const layers = template.findResources('AWS::Lambda::LayerVersion');
    const ingestLayers = fns[INGEST][1].Properties.Layers.map((l: any) => l.Ref as string);
    expect(ingestLayers).toHaveLength(3);
    expect(ingestLayers.some((id: string) => id.startsWith('TelemetrySharedLayer'))).toBe(true);
    expect(ingestLayers.some((id: string) => id.startsWith('TelemetryJwtLayer'))).toBe(true);
    const coreId = ingestLayers.find((id: string) => id.startsWith('TelemetryWorkflowCoreLayer'));
    expect(coreId).toBeDefined();
    expect((layers[coreId] as any).Properties.CompatibleRuntimes).toEqual(['python3.11', 'python3.12']);
    for (const name of [ROLLUP, API, MAINTENANCE]) {
      const ids = fns[name][1].Properties.Layers.map((l: any) => l.Ref as string);
      expect(ids).toHaveLength(2);
      expect(ids.some((id: string) => id.startsWith('TelemetryWorkflowCoreLayer'))).toBe(false);
    }
  });

  test('every environment key on every function, with the defaults as strings', () => {
    for (const [, [, fn]] of Object.entries(functionsByName())) {
      const env = fn.Properties.Environment.Variables;
      for (const key of EXPECTED_ENV_KEYS) expect(env[key]).toBeDefined();
      expect(env.COMPONENT_BUCKET_PREFIX).toBe('dda-component');
      expect(env.PORTAL_REGISTRY_ENFORCED).toBe('false');
      expect(env.TELEMETRY_FIREHOSE_SUMMARIES).toBe('dda-portal-telemetry-summaries');
      expect(env.TELEMETRY_FIREHOSE_EVENTS).toBe('dda-portal-telemetry-events');
      expect(env.TELEMETRY_PORTAL_RULE_NAME).toBe('dda_tel_ingest_portal');
      expect(env.TELEMETRY_DATA_PLANE_GRANT).toBe(helpersExportDataPlaneGrant ? 'tag' : 'absent');
      expect(flat(env.TELEMETRY_SUMMARIES_TABLE)).toContain('TelemetrySummariesTable');
      expect(flat(env.TELEMETRY_CONFIG_TABLE)).toContain('TelemetryConfigTable');
      expect(flat(env.TELEMETRY_LAKE_BUCKET)).toContain('TelemetryLakeBucket');
      expect(env.TELEMETRY_NOW_POLL_SECONDS).toBe('5');
      expect(env.TELEMETRY_LIVE_MAX_LEASES).toBe('5');
      expect(env.TELEMETRY_LIVE_MAX_VIEW_MINUTES).toBe('30');
      expect(env.TELEMETRY_CLIP_RETENTION_DAYS).toBe('90');
      expect(env.TELEMETRY_LAKE_RETENTION_DAYS).toBe('400');
      expect(env.TELEMETRY_INGEST_CONCURRENCY).toBe('50');
    }
  });

  test('PORTAL_REGISTRY_ENFORCED follows the context value on every function', () => {
    const fns = functionsByName(enforcedTemplate);
    expect(Object.keys(fns)).toHaveLength(4);
    for (const [, [, fn]] of Object.entries(fns)) {
      expect(fn.Properties.Environment.Variables.PORTAL_REGISTRY_ENFORCED).toBe('true');
    }
  });

  test('the roll-up has two stream event sources with batch 100, a 60 s window, failure reporting, bisect, three retries and the queue', () => {
    const mappings = Object.values(template.findResources('AWS::Lambda::EventSourceMapping')) as any[];
    expect(mappings).toHaveLength(2);
    const rollupId = functionsByName()[ROLLUP][0];
    const tables = template.findResources('AWS::DynamoDB::Table');
    const streamed: string[] = [];
    for (const mapping of mappings) {
      expect(mapping.Properties.FunctionName.Ref).toBe(rollupId);
      expect(mapping.Properties.BatchSize).toBe(100);
      expect(mapping.Properties.MaximumBatchingWindowInSeconds).toBe(60);
      expect(mapping.Properties.MaximumBatchingWindowInSeconds).toBeGreaterThanOrEqual(1);
      expect(mapping.Properties.FunctionResponseTypes).toEqual(['ReportBatchItemFailures']);
      expect(mapping.Properties.BisectBatchOnFunctionError).toBe(true);
      expect(mapping.Properties.MaximumRetryAttempts).toBe(3);
      expect(mapping.Properties.StartingPosition).toBe('TRIM_HORIZON');
      const destination = mapping.Properties.DestinationConfig.OnFailure.Destination;
      expect(Object.keys(template.findResources('AWS::SQS::Queue'))).toContain(destination['Fn::GetAtt'][0]);
      const source = mapping.Properties.EventSourceArn['Fn::GetAtt'][0];
      expect(Object.keys(tables)).toContain(source);
      streamed.push((tables[source] as any).Properties.TableName);
    }
    expect(streamed.sort()).toEqual(['dda-portal-telemetry-events', 'dda-portal-telemetry-summaries']);
  });
});

// ---------------------------------------------------------------------------
// tables, the lake and the streams (design §7)
// ---------------------------------------------------------------------------
describe('the tables', () => {
  test('keys, TTLs, streams, GSIs, PITR and on-demand billing', () => {
    for (const name of ['dda-portal-telemetry-summaries', 'dda-portal-telemetry-events', 'dda-portal-telemetry-aggregates',
      'dda-portal-telemetry-config']) {
      const table = tableNamed(name);
      expect(table.Properties.KeySchema).toEqual([
        { AttributeName: 'pk', KeyType: 'HASH' },
        { AttributeName: 'sk', KeyType: 'RANGE' },
      ]);
      expect(table.Properties.BillingMode).toBe('PAY_PER_REQUEST');
      expect(table.Properties.TimeToLiveSpecification).toEqual({ AttributeName: 'expires_at', Enabled: true });
      expect(table.DeletionPolicy).toBe('Retain');
    }
    expect(tableNamed('dda-portal-telemetry-summaries').Properties.StreamSpecification).toEqual({ StreamViewType: 'NEW_IMAGE' });
    expect(tableNamed('dda-portal-telemetry-events').Properties.StreamSpecification).toEqual({ StreamViewType: 'NEW_IMAGE' });
    expect(tableNamed('dda-portal-telemetry-aggregates').Properties.StreamSpecification).toBeUndefined();
    expect(tableNamed('dda-portal-telemetry-config').Properties.StreamSpecification).toBeUndefined();
    const events = tableNamed('dda-portal-telemetry-events').Properties.GlobalSecondaryIndexes;
    expect(events).toEqual([{
      IndexName: 'by-workflow',
      KeySchema: [{ AttributeName: 'gsi1pk', KeyType: 'HASH' }, { AttributeName: 'gsi1sk', KeyType: 'RANGE' }],
      Projection: { ProjectionType: 'ALL' },
    }]);
    const config = tableNamed('dda-portal-telemetry-config');
    expect(config.Properties.PointInTimeRecoverySpecification.PointInTimeRecoveryEnabled).toBe(true);
    expect(config.Properties.GlobalSecondaryIndexes).toEqual([
      { IndexName: 'by-usecase', KeySchema: [{ AttributeName: 'gsi1pk', KeyType: 'HASH' }, { AttributeName: 'gsi1sk', KeyType: 'RANGE' }],
        Projection: { ProjectionType: 'ALL' } },
      { IndexName: 'by-type', KeySchema: [{ AttributeName: 'gsi2pk', KeyType: 'HASH' }, { AttributeName: 'gsi2sk', KeyType: 'RANGE' }],
        Projection: { ProjectionType: 'INCLUDE', NonKeyAttributes: ['enabled', 'account_id', 'region'] } },
    ]);
    expect(tableNamed('dda-portal-telemetry-summaries').Properties.GlobalSecondaryIndexes).toBeUndefined();
  });
});

describe('the lake bucket and the Firehose streams', () => {
  test('the lake bucket settings and its one-statement policy', () => {
    const buckets = Object.entries(template.findResources('AWS::S3::Bucket')) as [string, any][];
    expect(buckets).toHaveLength(1);
    const [bucketId, bucket] = buckets[0];
    expect(flat(bucket.Properties.BucketName)).toContain('edgecv-portal-telemetry-');
    expect(flat(bucket.Properties.BucketName)).toContain('AWS::AccountId');
    expect(flat(bucket.Properties.BucketName)).toContain('AWS::Region');
    expect(bucket.Properties.BucketEncryption.ServerSideEncryptionConfiguration[0].ServerSideEncryptionByDefault.SSEAlgorithm).toBe('AES256');
    expect(bucket.Properties.PublicAccessBlockConfiguration).toEqual({
      BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true,
    });
    expect(bucket.Properties.OwnershipControls.Rules[0].ObjectOwnership).toBe('BucketOwnerEnforced');
    expect(bucket.Properties.VersioningConfiguration).toBeUndefined();
    expect(bucket.DeletionPolicy).toBe('Retain');
    const rules = bucket.Properties.LifecycleConfiguration.Rules;
    expect(rules).toHaveLength(1);
    expect(rules[0].Prefix).toBe('raw/');
    expect(rules[0].ExpirationInDays).toBe(400);
    expect(rules[0].Status).toBe('Enabled');
    const policies = Object.values(template.findResources('AWS::S3::BucketPolicy')) as any[];
    expect(policies).toHaveLength(1);
    expect(policies[0].Properties.Bucket.Ref).toBe(bucketId);
    const statements = policies[0].Properties.PolicyDocument.Statement;
    expect(statements).toHaveLength(1);
    expect(statements[0].Effect).toBe('Deny');
    expect(statements[0].Action).toBe('s3:*');
    expect(statements[0].Condition).toEqual({ Bool: { 'aws:SecureTransport': 'false' } });
  });

  test('two Direct PUT streams with GZIP, 300 s / 64 MB buffering and the dated prefixes', () => {
    const streams = Object.values(template.findResources('AWS::KinesisFirehose::DeliveryStream')) as any[];
    expect(streams).toHaveLength(2);
    const byName = Object.fromEntries(streams.map((s) => [s.Properties.DeliveryStreamName, s]));
    expect(Object.keys(byName).sort()).toEqual(['dda-portal-telemetry-events', 'dda-portal-telemetry-summaries']);
    for (const [name, kind] of [['dda-portal-telemetry-summaries', 'summaries'], ['dda-portal-telemetry-events', 'events']]) {
      const s = byName[name];
      expect(s.Properties.DeliveryStreamType).toBe('DirectPut');
      const dest = s.Properties.ExtendedS3DestinationConfiguration;
      expect(dest.Prefix).toBe(`raw/${kind}/dt=!{timestamp:yyyy-MM-dd}/`);
      expect(dest.ErrorOutputPrefix.startsWith('errors/')).toBe(true);
      expect(dest.ErrorOutputPrefix).toContain('!{firehose:error-output-type}');
      expect(dest.BufferingHints).toEqual({ IntervalInSeconds: 300, SizeInMBs: 64 });
      expect(dest.CompressionFormat).toBe('GZIP');
      expect(flat(dest.BucketARN)).toContain('TelemetryLakeBucket');
      expect(flat(dest.RoleARN)).toContain('StreamRole');
      expect(dest.CloudWatchLoggingOptions.Enabled).toBe(true);
    }
  });

  test('the Firehose roles trust firehose.amazonaws.com with aws:SourceAccount and hold the lake grants only', () => {
    const roles = Object.entries(template.findResources('AWS::IAM::Role')).filter(([id]) => id.includes('StreamRole'));
    expect(roles).toHaveLength(2);
    for (const [roleId, role] of roles as [string, any][]) {
      const trust = role.Properties.AssumeRolePolicyDocument.Statement[0];
      expect(trust.Principal.Service).toBe('firehose.amazonaws.com');
      expect(flat(trust.Condition.StringEquals['aws:SourceAccount'])).toContain('AWS::AccountId');
      const statements = statementsOfRole(roleId);
      expect(statements).toHaveLength(3);
      const bucket = statements.find((s) => s.Sid === 'TelemetryLakeBucket');
      expect(actionsOf(bucket).sort()).toEqual(['s3:GetBucketLocation', 's3:ListBucket', 's3:ListBucketMultipartUploads']);
      const objects = statements.find((s) => s.Sid === 'TelemetryLakeObjects');
      expect(actionsOf(objects).sort()).toEqual(['s3:AbortMultipartUpload', 's3:PutObject']);
      const resources = resourcesOf(objects).map(flat);
      expect(resources).toHaveLength(2);
      expect(resources.some((r) => r.includes('/raw/*'))).toBe(true);
      expect(resources.some((r) => r.includes('/errors/*'))).toBe(true);
      expect(actionsOf(statements.find((s) => s.Sid === 'TelemetryFirehoseLogs'))).toEqual(['logs:PutLogEvents']);
    }
  });
});

// ---------------------------------------------------------------------------
// IAM (design §14.1)
// ---------------------------------------------------------------------------
describe('the ingest role', () => {
  test('store writes, config counters, attribution reads, the lake, the certificate lookup, AssumeRole and the queue', () => {
    const roleId = roleLogicalIdOf(INGEST);
    const writes = sid(roleId, 'TelemetryStoreWrites');
    expect(actionsOf(writes).sort()).toEqual(['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem']);
    expect(resourcesOf(writes).map(flat).join()).toMatch(/TelemetrySummariesTable.*TelemetryEventsTable|TelemetryEventsTable.*TelemetrySummariesTable/);
    const config = sid(roleId, 'TelemetryConfigCounters');
    expect(actionsOf(config).sort()).toEqual(['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem']);
    expect(flat(resourcesOf(config))).toContain('TelemetryConfigTable');
    const reads = sid(roleId, 'TelemetryAttributionReads');
    expect(actionsOf(reads)).toEqual(['dynamodb:GetItem']);
    expect(resourcesOf(reads)).toHaveLength(3);
    const lake = sid(roleId, 'TelemetryLakeWrite');
    expect(actionsOf(lake)).toEqual(['firehose:PutRecordBatch']);
    expect(resourcesOf(lake).map(flat).every((r) => r.includes(':deliverystream/dda-portal-telemetry-'))).toBe(true);
    expect(resourcesOf(lake)).toHaveLength(2);
    const identity = sid(roleId, 'TelemetryIdentityLookup');
    expect(actionsOf(identity)).toEqual(['iot:ListPrincipalThings']);
    expect(flat(resourcesOf(identity))).toContain(':cert/*');
    const assume = sid(roleId, 'TelemetryAssumeUseCaseRole');
    expect(actionsOf(assume)).toEqual(['sts:AssumeRole']);
    expect(resourcesOf(assume).sort()).toEqual([
      `arn:aws:iam::${TRUSTED_A}:role/DDAPortalAccessRole`,
      `arn:aws:iam::${TRUSTED_B}:role/DDAPortalAccessRole`,
    ]);
    const sqs = statementsOfRole(roleId).filter((s) => actionsOf(s).includes('sqs:SendMessage'));
    expect(sqs.length).toBeGreaterThanOrEqual(1);
    for (const s of sqs) expect(flat(resourcesOf(s))).toContain(Object.keys(template.findResources('AWS::SQS::Queue'))[0]);
    for (const s of statementsOfRole(roleId)) {
      expect(actionsOf(s)).not.toContain('dynamodb:Scan');
      expect(actionsOf(s)).not.toContain('dynamodb:DeleteItem');
    }
  });
});

describe('the roll-up role', () => {
  test('stream reads, Query on the two stores, PutItem and Query on the aggregates, the queue', () => {
    const roleId = roleLogicalIdOf(ROLLUP);
    const statements = statementsOfRole(roleId);
    const streams = statements.filter((s) => actionsOf(s).includes('dynamodb:GetRecords'));
    expect(streams).toHaveLength(2);
    for (const s of streams) {
      expect(actionsOf(s).sort()).toEqual(['dynamodb:DescribeStream', 'dynamodb:GetRecords', 'dynamodb:GetShardIterator']);
      expect(flat(resourcesOf(s))).toContain('StreamArn');
    }
    expect(statements.some((s) => actionsOf(s).includes('dynamodb:ListStreams'))).toBe(true);
    const reads = sid(roleId, 'TelemetryRollupReads');
    expect(actionsOf(reads)).toEqual(['dynamodb:Query']);
    expect(resourcesOf(reads)).toHaveLength(2);
    const aggregates = sid(roleId, 'TelemetryAggregates');
    expect(actionsOf(aggregates).sort()).toEqual(['dynamodb:PutItem', 'dynamodb:Query']);
    expect(flat(resourcesOf(aggregates))).toContain('TelemetryAggregatesTable');
    const sqs = statements.filter((s) => actionsOf(s).includes('sqs:SendMessage'));
    expect(sqs.length).toBeGreaterThanOrEqual(1);
    for (const s of statements) {
      expect(actionsOf(s)).not.toContain('dynamodb:UpdateItem');
      expect(actionsOf(s)).not.toContain('dynamodb:DeleteItem');
      expect(actionsOf(s)).not.toContain('firehose:PutRecordBatch');
    }
  });
});

describe('the API role', () => {
  test('the config table and its indexes, the three stores, the Portal tables and the audit write', () => {
    const roleId = roleLogicalIdOf(API);
    const config = sid(roleId, 'TelemetryConfigTable');
    expect(actionsOf(config).sort()).toEqual([
      'dynamodb:BatchGetItem', 'dynamodb:ConditionCheckItem', 'dynamodb:DeleteItem', 'dynamodb:GetItem',
      'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:UpdateItem',
    ]);
    expect(resourcesOf(config)).toHaveLength(1);
    const indexes = sid(roleId, 'TelemetryConfigIndexes');
    expect(actionsOf(indexes)).toEqual(['dynamodb:Query']);
    expect(resourcesOf(indexes).map(flat).some((r) => r.includes('/index/by-usecase'))).toBe(true);
    expect(resourcesOf(indexes).map(flat).some((r) => r.includes('/index/by-type'))).toBe(true);
    const stores = sid(roleId, 'TelemetryStoreReads');
    expect(actionsOf(stores).sort()).toEqual(['dynamodb:GetItem', 'dynamodb:Query']);
    expect(resourcesOf(stores)).toHaveLength(4);
    expect(flat(resourcesOf(stores))).toContain('/index/by-workflow');
    const reads = sid(roleId, 'TelemetryUseCaseAndRoleReads');
    expect(actionsOf(reads)).toEqual(['dynamodb:GetItem']);
    expect(resourcesOf(reads)).toHaveLength(2);
    const devices = sid(roleId, 'TelemetryDeviceRecords');
    expect(actionsOf(devices).sort()).toEqual(['dynamodb:GetItem', 'dynamodb:Scan']);
    expect(resourcesOf(devices)).toHaveLength(1);
    const workflows = sid(roleId, 'TelemetryWorkflowReads');
    expect(actionsOf(workflows).sort()).toEqual(['dynamodb:GetItem', 'dynamodb:Query']);
    expect(flat(resourcesOf(workflows))).toContain('/index/usecase-workflows-index');
    expect(actionsOf(sid(roleId, 'TelemetryAuditWrite'))).toEqual(['dynamodb:PutItem']);
    for (const s of statementsOfRole(roleId)) {
      if (actionsOf(s).includes('dynamodb:Scan')) expect(s.Sid).toBe('TelemetryDeviceRecords');
      for (const action of actionsOf(s)) {
        expect(action).not.toBe('dynamodb:*');
        expect(action).not.toBe('dynamodb:BatchWriteItem');
      }
    }
  });

  test('the portal-topology control-plane and media statements', () => {
    const roleId = roleLogicalIdOf(API);
    const shadow = sid(roleId, 'TelemetryShadowControl');
    expect(actionsOf(shadow).sort()).toEqual(['iot:GetThingShadow', 'iot:UpdateThingShadow']);
    expect(flat(resourcesOf(shadow))).toContain(':thing/*');
    const rule = sid(roleId, 'TelemetryPortalRuleRead');
    expect(actionsOf(rule)).toEqual(['iot:GetTopicRule']);
    expect(flat(resourcesOf(rule))).toContain(':rule/dda_tel_ingest_portal');
    const greengrass = sid(roleId, 'TelemetryCoreDevices');
    expect(actionsOf(greengrass).sort()).toEqual(['greengrass:GetCoreDevice', 'greengrass:ListCoreDevices', 'greengrass:ListTagsForResource']);
    expect(resourcesOf(greengrass)).toEqual(['*']);
    const read = sid(roleId, 'TelemetryMediaRead');
    expect(actionsOf(read)).toEqual(['s3:GetObject']);
    expect(flat(resourcesOf(read))).toContain('arn:aws:s3:::dda-inference-results-');
    expect(flat(resourcesOf(read))).toContain('/dda-telemetry/*');
    const list = sid(roleId, 'TelemetryMediaList');
    expect(actionsOf(list)).toEqual(['s3:ListBucket']);
    expect(list.Condition).toEqual({ StringLike: { 's3:prefix': 'dda-telemetry/*' } });
    const retention = sid(roleId, 'TelemetryMediaRetentionRead');
    expect(actionsOf(retention).sort()).toEqual(['s3:GetBucketVersioning', 's3:GetLifecycleConfiguration']);
    const assume = sid(roleId, 'TelemetryAssumeUseCaseRole');
    expect(resourcesOf(assume)).toHaveLength(2);
    for (const s of statementsOfRole(roleId)) {
      expect(actionsOf(s)).not.toContain('s3:DeleteObject');
      expect(actionsOf(s)).not.toContain('s3:PutObject');
    }
  });
});

describe('the maintenance role', () => {
  test('the config table and indexes, UseCases, Devices, the shadow write, GetCoreDevice, the live-frame sweep', () => {
    const roleId = roleLogicalIdOf(MAINTENANCE);
    const config = sid(roleId, 'TelemetryConfigMaintenance');
    expect(actionsOf(config).sort()).toEqual(['dynamodb:DeleteItem', 'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:UpdateItem']);
    expect(resourcesOf(sid(roleId, 'TelemetryConfigIndexesMaintenance'))).toHaveLength(2);
    expect(actionsOf(sid(roleId, 'TelemetryUseCaseRead'))).toEqual(['dynamodb:GetItem']);
    expect(actionsOf(sid(roleId, 'TelemetryDeviceRecordsMaintenance')).sort()).toEqual(['dynamodb:GetItem', 'dynamodb:Scan']);
    expect(actionsOf(sid(roleId, 'TelemetryShadowMaintenance'))).toEqual(['iot:UpdateThingShadow']);
    expect(actionsOf(sid(roleId, 'TelemetryCoreDeviceStatus'))).toEqual(['greengrass:GetCoreDevice']);
    const list = sid(roleId, 'TelemetryLiveFrameList');
    expect(actionsOf(list)).toEqual(['s3:ListBucket']);
    expect(list.Condition).toEqual({ StringLike: { 's3:prefix': 'dda-telemetry/live/*' } });
    const expiry = sid(roleId, 'TelemetryLiveFrameExpiry');
    expect(actionsOf(expiry)).toEqual(['s3:DeleteObject']);
    expect(flat(resourcesOf(expiry))).toContain('/dda-telemetry/live/*');
    expect(actionsOf(sid(roleId, 'TelemetryAssumeUseCaseRole'))).toEqual(['sts:AssumeRole']);
    for (const s of statementsOfRole(roleId)) {
      expect(actionsOf(s)).not.toContain('s3:GetObject');
      expect(actionsOf(s)).not.toContain('iot:GetThingShadow');
      expect(flat(resourcesOf(s))).not.toContain('TelemetrySummariesTable');
      expect(flat(resourcesOf(s))).not.toContain('TelemetryAggregatesTable');
    }
  });
});

describe('every role', () => {
  test('no S3 object action on arn:aws:s3:::*/* and no bucket action on arn:aws:s3:::* without the tag condition; no wildcard administrative action', () => {
    for (const tpl of [template, wiredTemplate]) {
      for (const statement of allStatements(tpl)) {
        const actions = actionsOf(statement);
        for (const action of actions) {
          expect(action).not.toBe('*');
          expect(action).not.toMatch(/:\*$/);
          expect(action).not.toBe('logs:DeleteLogGroup');
        }
        if (!actions.some((a) => a.startsWith('s3:'))) continue;
        for (const resource of resourcesOf(statement)) {
          const text = flat(resource);
          if (text === '"arn:aws:s3:::*/*"' || text === '"arn:aws:s3:::*"') {
            expect(statement.Condition).toBeDefined();
            expect(flat(statement.Condition)).toContain('dda-portal:managed');
          }
        }
      }
    }
  });

  test('the schedule role may invoke the maintenance function only, every 5 minutes', () => {
    const roles = Object.entries(template.findResources('AWS::IAM::Role')).filter(([id]) => id.startsWith('TelemetryScheduleRole'));
    expect(roles).toHaveLength(1);
    const [roleId, role] = roles[0] as [string, any];
    expect(role.Properties.AssumeRolePolicyDocument.Statement[0].Principal.Service).toBe('scheduler.amazonaws.com');
    const statements = statementsOfRole(roleId);
    expect(statements).toHaveLength(1);
    expect(actionsOf(statements[0])).toEqual(['lambda:InvokeFunction']);
    expect(resourcesOf(statements[0])).toHaveLength(1);
    expect(flat(resourcesOf(statements[0]))).toContain('TelemetryMaintenance');
    const schedules = Object.values(template.findResources('AWS::Scheduler::Schedule')) as any[];
    expect(schedules).toHaveLength(1);
    expect(schedules[0].Properties.ScheduleExpression).toBe('rate(5 minutes)');
    expect(schedules[0].Properties.Target.RoleArn['Fn::GetAtt'][0]).toBe(roleId);
  });
});

// ---------------------------------------------------------------------------
// the data-plane grant branch (design §12.3)
// ---------------------------------------------------------------------------
describe('the dataPlaneGrant branch', () => {
  test('without lib/context-helpers exporting dataPlaneGrant: absent, and no statement beyond the by-ARN ones', () => {
    expect(helpersExportDataPlaneGrant).toBe(false);
    const stack = build();
    expect(stack.dataPlaneGrantWired).toBe(false);
    const tpl = Template.fromStack(stack);
    for (const roleId of [roleLogicalIdOf(API, tpl), roleLogicalIdOf(MAINTENANCE, tpl)]) {
      for (const s of statementsOfRole(roleId, tpl)) {
        expect(String(s.Sid ?? '')).not.toContain('DataPlane');
        for (const resource of resourcesOf(s)) {
          const text = flat(resource);
          if (text.includes('arn:aws:s3:::')) expect(text).toContain('dda-inference-results-');
        }
      }
    }
    const env = functionsByName(tpl)[API][1].Properties.Environment.Variables;
    expect(env.TELEMETRY_DATA_PLANE_GRANT).toBe('absent');
    const outputs = tpl.toJSON().Outputs;
    expect(outputs.TelemetryDataPlaneGrant.Value).toBe('absent');
  });

  test('with a dataPlaneGrant: the tag-conditioned statements and TELEMETRY_DATA_PLANE_GRANT=tag', () => {
    expect(wiredStack.dataPlaneGrantWired).toBe(true);
    const apiRole = roleLogicalIdOf(API, wiredTemplate);
    const read = sid(apiRole, 'TelemetryDataPlaneObjectRead', wiredTemplate);
    expect(actionsOf(read)).toEqual(['s3:GetObject']);
    expect(resourcesOf(read)).toEqual(['arn:aws:s3:::customer-data-bucket/*']);
    expect(read.Condition).toEqual({ StringEquals: { 's3:ResourceTag/dda-portal:managed': 'true' } });
    const list = sid(apiRole, 'TelemetryDataPlaneBucketList', wiredTemplate);
    expect(actionsOf(list)).toEqual(['s3:ListBucket']);
    expect(resourcesOf(list)).toEqual(['arn:aws:s3:::customer-data-bucket']);
    const maintenanceRole = roleLogicalIdOf(MAINTENANCE, wiredTemplate);
    const expiry = sid(maintenanceRole, 'TelemetryDataPlaneLiveFrameExpiry', wiredTemplate);
    expect(actionsOf(expiry)).toEqual(['s3:DeleteObject']);
    expect(resourcesOf(expiry)).toEqual(['arn:aws:s3:::customer-data-bucket/dda-telemetry/live/*']);
    expect(expiry.Condition).toBeDefined();
    sid(maintenanceRole, 'TelemetryDataPlaneBucketList', wiredTemplate);
    for (const name of FUNCTION_NAMES) {
      expect(functionsByName(wiredTemplate)[name][1].Properties.Environment.Variables.TELEMETRY_DATA_PLANE_GRANT).toBe('tag');
    }
  });
});

// ---------------------------------------------------------------------------
// routes, deployment, alarms, deployment values
// ---------------------------------------------------------------------------
describe('the routes', () => {
  test('18 resources (decision Q5), 18 methods; non-OPTIONS methods use the Cognito authorizer, OPTIONS are MOCK', () => {
    const resources = Object.values(template.findResources('AWS::ApiGateway::Resource')) as any[];
    expect(resources).toHaveLength(TELEMETRY_API_RESOURCE_COUNT);
    expect(TELEMETRY_API_RESOURCE_COUNT).toBe(18);
    expect(build().apiResourceCount).toBe(18);
    expect(resources.map((r) => r.Properties.PathPart).sort()).toEqual([
      'clips', 'dashboards', 'events', 'live', 'live-lease', 'media', 'now', 'rejections', 'rejections', 'schemas',
      'series', 'settings', 'telemetry', 'templates', 'usecases', '{clip_ref}', '{dashboard_id}', '{usecase_id}',
    ].sort());
    const methods = Object.values(template.findResources('AWS::ApiGateway::Method')) as any[];
    const nonOptions = methods.filter((m) => m.Properties.HttpMethod !== 'OPTIONS');
    const options = methods.filter((m) => m.Properties.HttpMethod === 'OPTIONS');
    expect(nonOptions).toHaveLength(18);
    expect(nonOptions.map((m) => m.Properties.HttpMethod).sort()).toEqual(
      ['DELETE', ...Array(13).fill('GET'), 'PATCH', 'POST', 'POST', 'PUT'],
    );
    for (const method of nonOptions) {
      expect(method.Properties.AuthorizationType).toBe('COGNITO_USER_POOLS');
      expect(method.Properties.AuthorizerId).toBeDefined();
      expect(method.Properties.Integration.Type).toBe('AWS_PROXY');
    }
    expect(options.length).toBeGreaterThanOrEqual(1);
    for (const method of options) {
      expect(method.Properties.Integration.Type).toBe('MOCK');
      expect(method.Properties.AuthorizationType).toBe('NONE');
    }
    const authorizers = Object.values(template.findResources('AWS::ApiGateway::Authorizer')) as any[];
    expect(authorizers).toHaveLength(1);
    expect(authorizers[0].Properties.Type).toBe('COGNITO_USER_POOLS');
  });

  test('the route-salted deployment is retained and depends on the authorizer', () => {
    const deployments = Object.entries(template.findResources('AWS::ApiGateway::Deployment')) as [string, any][];
    expect(deployments).toHaveLength(1);
    const [logicalId, deployment] = deployments[0];
    expect(logicalId).toMatch(/^TelemetryDeployment[0-9a-f]{16}$/);
    expect(deployment.DeletionPolicy).toBe('Retain');
    expect(deployment.UpdateReplacePolicy).toBe('Retain');
    expect(deployment.Properties.StageName).toBe('v1');
    expect(deployment.DependsOn).toEqual(expect.arrayContaining(Object.keys(template.findResources('AWS::ApiGateway::Authorizer'))));
  });
});

describe('alarms', () => {
  test('IdentityUnavailable, the failure queue depth and the roll-up iterator age', () => {
    const alarms = Object.values(template.findResources('AWS::CloudWatch::Alarm')) as any[];
    expect(alarms).toHaveLength(3);
    const identity = alarms.find((a) => a.Properties.MetricName === 'IdentityUnavailable');
    expect(identity.Properties.Namespace).toBe('DDA/Telemetry');
    expect(identity.Properties.Threshold).toBe(1);
    const queue = alarms.find((a) => a.Properties.MetricName === 'ApproximateNumberOfMessagesVisible');
    expect(queue.Properties.Namespace).toBe('AWS/SQS');
    const iterator = alarms.find((a) => a.Properties.MetricName === 'IteratorAge');
    expect(iterator.Properties.Namespace).toBe('AWS/Lambda');
    expect(iterator.Properties.Threshold).toBe(300000);
  });
});

describe('deployment values (design §15.2)', () => {
  test('defaults and ranges', () => {
    const byKey = Object.fromEntries(TELEMETRY_DEPLOYMENT_VALUES.map((v) => [v.key, v]));
    expect(byKey.telemetryNowPollSeconds).toMatchObject({ defaultValue: 5, min: 5, max: 30 });
    expect(byKey.telemetryLiveMaxLeases).toMatchObject({ defaultValue: 5, min: 1, max: 20 });
    expect(byKey.telemetryLiveMaxViewMinutes).toMatchObject({ defaultValue: 30, min: 5, max: 240 });
    expect(byKey.telemetryClipRetentionDays).toMatchObject({ defaultValue: 90, min: 7, max: 365 });
    expect(byKey.telemetryLakeRetentionDays).toMatchObject({ defaultValue: 400, min: 30, max: 3650 });
    expect(byKey.telemetryIngestConcurrency).toMatchObject({ defaultValue: 50, min: 10, max: 500 });
  });

  test('a value outside its range throws at synth; an in-range value reaches the environment and the resources', () => {
    expect(() => build({ telemetryLiveMaxLeases: '21' })).toThrow(/telemetryLiveMaxLeases/);
    expect(() => build({ telemetryIngestConcurrency: 9 })).toThrow(/telemetryIngestConcurrency/);
    expect(() => build({ telemetryNowPollSeconds: '4' })).toThrow(/telemetryNowPollSeconds/);
    expect(() => build({ telemetryLakeRetentionDays: 'forever' })).toThrow(/telemetryLakeRetentionDays/);
    expect(() => build({ telemetryClipRetentionDays: '366' })).toThrow(/telemetryClipRetentionDays/);
    expect(resolveTelemetryValue(TELEMETRY_DEPLOYMENT_VALUES[0], undefined)).toBe(5);
    expect(resolveTelemetryValue(TELEMETRY_DEPLOYMENT_VALUES[0], '30')).toBe(30);
    expect(() => resolveTelemetryValue(TELEMETRY_DEPLOYMENT_VALUES[0], 2.5)).toThrow();
    const tuned = Template.fromStack(build({ telemetryIngestConcurrency: '120', telemetryLakeRetentionDays: '30', telemetryLiveMaxLeases: '20' }));
    const ingest = functionsByName(tuned)[INGEST][1].Properties;
    expect(ingest.ReservedConcurrentExecutions).toBe(120);
    expect(ingest.Environment.Variables.TELEMETRY_LIVE_MAX_LEASES).toBe('20');
    const bucket = Object.values(tuned.findResources('AWS::S3::Bucket'))[0] as any;
    expect(bucket.Properties.LifecycleConfiguration.Rules[0].ExpirationInDays).toBe(30);
  }, 300_000);

  test('an empty trusted account list is refused at synth', () => {
    expect(() => build(undefined, { trustedUseCaseAccountIds: [] })).toThrow(/trustedUseCaseAccountIds/);
  });
});
