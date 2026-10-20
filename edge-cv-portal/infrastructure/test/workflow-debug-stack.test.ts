/**
 * Static assertions on EdgeCVPortalWorkflowDebugStack (workflow-debug-mode
 * design §2, §6.1, §7.1, §9, §10, §13; tasks.md 4.1).
 *
 * Conventions follow synthetic-data-s3-permissions.test.ts: synthesize once
 * in beforeAll with a generous timeout (the functions and three layer assets
 * are staged at synth time), locate resources via template.findResources and
 * assert on raw CloudFormation properties. A second synth with
 * portalRegistryEnforced on covers the environment flag, a third with a
 * deployment value out of range covers the synth-time guard, and synths
 * without a usable `sharedAuthorizerId` prop cover the authorizer guard (the
 * stack attaches its routes to the portal API's existing Cognito authorizer,
 * whose id bin/app.ts passes from the CDK context, and creates none of its
 * own).
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { StorageStack } from '../lib/storage-stack';
import {
  DEBUG_DEPLOYMENT_VALUES,
  DEBUG_RULE_SQL,
  WorkflowDebugStack,
  WorkflowDebugStackProps,
  resolveDeploymentValue,
} from '../lib/workflow-debug-stack';

const TRUSTED_A = '222222222222';
const TRUSTED_B = '333333333333';
const INGEST_FUNCTION_NAME = 'dda-portal-workflow-debug-ingest';
const API_FUNCTION_NAME = 'dda-portal-workflow-debug-api';
const MAINTENANCE_FUNCTION_NAME = 'dda-portal-workflow-debug-maintenance';
/** The existing portal API authorizer the deploy passes as `-c sharedAuthorizerId` and bin/app.ts hands the stack (a fixture id). */
const SHARED_AUTHORIZER_ID = 'fixture0';

let template: Template;
let enforcedTemplate: Template;

/**
 * Synthesize the stack with the shared authorizer id as its prop, as
 * bin/app.ts passes it from the CDK context; a test overrides the prop to
 * exercise the synth guard.
 */
function buildStack(context?: { [key: string]: unknown }, override?: Partial<WorkflowDebugStackProps>): Template {
  const app = new cdk.App({ context });
  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');
  const props: WorkflowDebugStackProps = {
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    devicesTable: storage.devicesTable,
    deploymentsTable: storage.deploymentsTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    workflowsTable: storage.workflowsTable,
    workflowVersionsTable: storage.workflowVersionsTable,
    portalArtifactsBucket: storage.portalArtifactsBucket,
    userPool: new cognito.UserPool(deps, 'Pool'),
    trustedUseCaseAccountIds: [TRUSTED_A, TRUSTED_B],
    restApiId: 'testrestapiid',
    restApiRootResourceId: 'testrootresourceid',
    apiStageName: 'v1',
    sharedAuthorizerId: SHARED_AUTHORIZER_ID,
    ...override,
  };
  return Template.fromStack(new WorkflowDebugStack(app, 'WorkflowDebug', props));
}

beforeAll(() => {
  template = buildStack();
  enforcedTemplate = buildStack({ portalRegistryEnforced: 'true' });
}, 300_000);

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function flat(value: unknown): string {
  return JSON.stringify(value);
}

function functionsByName(): { [name: string]: [string, any] } {
  const result: { [name: string]: [string, any] } = {};
  for (const [logicalId, resource] of Object.entries(template.findResources('AWS::Lambda::Function'))) {
    const name = (resource as any).Properties.FunctionName;
    if (typeof name === 'string') result[name] = [logicalId, resource];
  }
  return result;
}

function roleLogicalIdOf(functionName: string): string {
  const [, fn] = functionsByName()[functionName];
  return fn.Properties.Role['Fn::GetAtt'][0];
}

function statementsOfRole(roleId: string): any[] {
  const policies = [
    ...Object.values(template.findResources('AWS::IAM::Policy')),
    ...Object.values(template.findResources('AWS::IAM::ManagedPolicy')),
  ] as any[];
  const fromPolicies = policies
    .filter((p) => p.Properties.Roles?.some((r: any) => r.Ref === roleId))
    .filter((p) => p.Properties.PolicyDocument?.Statement)
    .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
  const role = template.findResources('AWS::IAM::Role')[roleId] as any;
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

function allStatements(): any[] {
  const roles = Object.keys(template.findResources('AWS::IAM::Role'));
  return roles.flatMap((id) => statementsOfRole(id));
}

function statementsWithSid(roleId: string, sid: string): any[] {
  return statementsOfRole(roleId).filter((s) => s.Sid === sid);
}

const EXPECTED_ENV_KEYS = [
  'USECASES_TABLE', 'USER_ROLES_TABLE', 'DEVICES_TABLE', 'DEPLOYMENTS_TABLE', 'AUDIT_LOG_TABLE',
  'SETTINGS_TABLE', 'WORKFLOWS_TABLE', 'WORKFLOW_VERSIONS_TABLE', 'PORTAL_ARTIFACTS_BUCKET',
  'PORTAL_ACCOUNT_ID', 'USER_POOL_ID', 'COMPONENT_BUCKET_PREFIX', 'PORTAL_REGISTRY_ENFORCED',
  'WORKFLOW_DEBUG_SESSIONS_TABLE', 'WORKFLOW_DEBUG_FAILURE_QUEUE', 'DEBUG_PORTAL_RULE_NAME',
  'DEBUG_LOG_RETENTION_DAYS', 'DEBUG_MAX_SESSIONS_PER_USECASE', 'DEBUG_MAX_DEVICES_PER_SESSION',
  'DEBUG_MAX_MESSAGES_PER_SECOND', 'DEBUG_MAX_BYTES_PER_SECOND', 'DEBUG_INGEST_CONCURRENCY',
  'DEBUG_GET_LOG_EVENTS_PER_SECOND', 'DEBUG_FILTER_LOG_EVENTS_PER_SECOND',
];

// ---------------------------------------------------------------------------
// the rule and the aliases (design §6.1)
// ---------------------------------------------------------------------------
describe('the Portal-account rule dda_dbg_ingest_portal', () => {
  test('filter dda/debug/+/+/+ without #, the §6.1 SQL, a role-less Lambda action and an error action', () => {
    const rules = Object.values(template.findResources('AWS::IoT::TopicRule')) as any[];
    expect(rules).toHaveLength(1);
    const payload = rules[0].Properties.TopicRulePayload;
    expect(rules[0].Properties.RuleName).toBe('dda_dbg_ingest_portal');
    expect(payload.Sql).toBe(DEBUG_RULE_SQL);
    expect(payload.Sql).toContain("FROM 'dda/debug/+/+/+'");
    expect(payload.Sql).not.toContain('#');
    expect(payload.AwsIotSqlVersion).toBe('2016-03-23');
    expect(payload.Actions).toHaveLength(1);
    const action = payload.Actions[0];
    expect(Object.keys(action)).toEqual(['Lambda']);
    expect(Object.keys(action.Lambda)).toEqual(['FunctionArn']);
    // The action targets the Portal alias, not the unqualified function.
    const aliasIds = Object.keys(template.findResources('AWS::Lambda::Alias'));
    expect(aliasIds).toContain(action.Lambda.FunctionArn.Ref);
    expect(payload.ErrorAction.CloudwatchLogs.LogGroupName).toBeDefined();
    expect(payload.ErrorAction.CloudwatchLogs.RoleArn).toBeDefined();
  });

  test('the error role trusts iot.amazonaws.com conditioned on the account and the dda_dbg_* rules, with three logs actions', () => {
    const roles = Object.entries(template.findResources('AWS::IAM::Role')).filter(([id]) =>
      id.startsWith('WorkflowDebugRuleErrorRole'),
    );
    expect(roles).toHaveLength(1);
    const [roleId, role] = roles[0] as [string, any];
    const trust = role.Properties.AssumeRolePolicyDocument.Statement[0];
    expect(trust.Principal.Service).toBe('iot.amazonaws.com');
    expect(flat(trust.Condition.StringEquals['aws:SourceAccount'])).toContain('AWS::AccountId');
    expect(flat(trust.Condition.ArnLike['aws:SourceArn'])).toContain(':rule/dda_dbg_*');
    const statements = statementsOfRole(roleId);
    expect(statements).toHaveLength(1);
    expect(actionsOf(statements[0]).sort()).toEqual(
      ['logs:CreateLogStream', 'logs:DescribeLogStreams', 'logs:PutLogEvents'].sort(),
    );
    expect(flat(resourcesOf(statements[0]))).toContain('WorkflowDebugRuleErrorLogGroup');
    const groups = Object.values(template.findResources('AWS::Logs::LogGroup')) as any[];
    const errorGroup = groups.find((g) => g.Properties.LogGroupName === '/dda/workflow-debug/rule-errors');
    expect(errorGroup.Properties.RetentionInDays).toBe(14);
  });

  test('no role in the stack holds lambda:InvokeFunction on the ingest function or its aliases', () => {
    for (const statement of allStatements()) {
      if (!actionsOf(statement).includes('lambda:InvokeFunction')) continue;
      for (const resource of resourcesOf(statement)) {
        const text = flat(resource);
        expect(text).not.toContain('WorkflowDebugIngest');
        expect(text).not.toContain(INGEST_FUNCTION_NAME);
        expect(text).not.toContain('IngestAlias');
      }
    }
  });
});

describe('the ingest aliases', () => {
  test('one alias per trusted account and the Portal account, each with one permission statement', () => {
    const aliases = Object.entries(template.findResources('AWS::Lambda::Alias')) as [string, any][];
    const names = aliases.map(([, a]) => flat(a.Properties.Name));
    expect(aliases).toHaveLength(3);
    expect(names.filter((n) => n.includes(`acct-${TRUSTED_A}`))).toHaveLength(1);
    expect(names.filter((n) => n.includes(`acct-${TRUSTED_B}`))).toHaveLength(1);
    expect(names.filter((n) => n.includes('AWS::AccountId'))).toHaveLength(1);
    const permissions = Object.values(template.findResources('AWS::Lambda::Permission')) as any[];
    const iotPermissions = permissions.filter((p) => p.Properties.Principal === 'iot.amazonaws.com');
    expect(iotPermissions).toHaveLength(3);
    for (const [aliasId] of aliases) {
      const own = iotPermissions.filter((p) => p.Properties.FunctionName?.Ref === aliasId);
      expect(own).toHaveLength(1);
      expect(own[0].Properties.Action).toBe('lambda:InvokeFunction');
      expect(own[0].Properties.SourceAccount).toBeDefined();
      expect(flat(own[0].Properties.SourceArn)).toContain(':rule/dda_dbg_*');
      // The SourceArn names the same account as SourceAccount (a literal for
      // the trusted accounts, the AWS::AccountId pseudo parameter for the Portal).
      const sourceAccount = own[0].Properties.SourceAccount;
      if (typeof sourceAccount === 'string') {
        expect(flat(own[0].Properties.SourceArn)).toContain(`:${sourceAccount}:rule/dda_dbg_*`);
      } else {
        expect(flat(sourceAccount)).toContain('AWS::AccountId');
        expect(flat(own[0].Properties.SourceArn)).toContain('AWS::AccountId');
      }
    }
  });

  test('one EventInvokeConfig per alias with the retry, age and on-failure queue; none on $LATEST', () => {
    const aliasIds = Object.keys(template.findResources('AWS::Lambda::Alias'));
    const configs = Object.values(template.findResources('AWS::Lambda::EventInvokeConfig')) as any[];
    expect(configs).toHaveLength(aliasIds.length);
    const queues = Object.keys(template.findResources('AWS::SQS::Queue'));
    expect(queues).toHaveLength(1);
    const qualifiedAliases: string[] = [];
    for (const config of configs) {
      const qualifier = config.Properties.Qualifier;
      expect(qualifier).toBeDefined();
      expect(flat(qualifier)).not.toContain('$LATEST');
      // CDK writes the qualifier as the alias name taken from the alias ARN
      // (segment 7 of the colon-split Ref), so the config is bound to that alias.
      expect(qualifier['Fn::Select'][0]).toBe(7);
      const aliasRef = qualifier['Fn::Select'][1]['Fn::Split'][1].Ref;
      expect(aliasIds).toContain(aliasRef);
      qualifiedAliases.push(aliasRef);
      expect(config.Properties.FunctionName.Ref).toBeDefined();
      expect(config.Properties.MaximumRetryAttempts).toBe(2);
      expect(config.Properties.MaximumEventAgeInSeconds).toBe(3600);
      expect(config.Properties.DestinationConfig.OnFailure.Destination['Fn::GetAtt'][0]).toBe(queues[0]);
    }
    expect(qualifiedAliases.sort()).toEqual([...aliasIds].sort());
  });

  test('the failure queue keeps messages 14 days with managed encryption', () => {
    const queue = Object.values(template.findResources('AWS::SQS::Queue'))[0] as any;
    expect(queue.Properties.QueueName).toBe('dda-portal-workflow-debug-failures');
    expect(queue.Properties.MessageRetentionPeriod).toBe(14 * 24 * 3600);
    expect(queue.Properties.SqsManagedSseEnabled).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// functions, layers and environment (design §2)
// ---------------------------------------------------------------------------
describe('the three functions', () => {
  test('names, runtimes, sizes and the reserved concurrency of the ingest', () => {
    const fns = functionsByName();
    expect(Object.keys(fns).sort()).toEqual([API_FUNCTION_NAME, INGEST_FUNCTION_NAME, MAINTENANCE_FUNCTION_NAME].sort());
    const ingest = fns[INGEST_FUNCTION_NAME][1].Properties;
    expect(ingest.Runtime).toBe('python3.12');
    expect(ingest.Handler).toBe('workflow_debug_ingest.handler');
    expect(ingest.MemorySize).toBe(256);
    expect(ingest.Timeout).toBe(20);
    expect(ingest.ReservedConcurrentExecutions).toBe(20);
    const api = fns[API_FUNCTION_NAME][1].Properties;
    expect(api.Runtime).toBe('python3.12');
    expect(api.Handler).toBe('workflow_debug_api.handler');
    expect(api.Timeout).toBe(29);
    const maintenance = fns[MAINTENANCE_FUNCTION_NAME][1].Properties;
    expect(maintenance.Handler).toBe('workflow_debug_maintenance.handler');
    expect(maintenance.Runtime).toBe('python3.12');
  });

  test('the ingest carries the shared, JWT and workflow_core layers; the other two carry two layers', () => {
    const fns = functionsByName();
    const layers = template.findResources('AWS::Lambda::LayerVersion');
    const layerIdOf = (ref: any) => ref.Ref as string;
    const ingestLayers = fns[INGEST_FUNCTION_NAME][1].Properties.Layers.map(layerIdOf);
    expect(ingestLayers).toHaveLength(3);
    expect(ingestLayers.some((id: string) => id.startsWith('WorkflowDebugSharedLayer'))).toBe(true);
    expect(ingestLayers.some((id: string) => id.startsWith('WorkflowDebugJwtLayer'))).toBe(true);
    const coreId = ingestLayers.find((id: string) => id.startsWith('WorkflowDebugWorkflowCoreLayer'));
    expect(coreId).toBeDefined();
    expect((layers[coreId] as any).Properties.CompatibleRuntimes).toEqual(['python3.11', 'python3.12']);
    for (const name of [API_FUNCTION_NAME, MAINTENANCE_FUNCTION_NAME]) {
      const ids = fns[name][1].Properties.Layers.map(layerIdOf);
      expect(ids).toHaveLength(2);
      expect(ids.some((id: string) => id.startsWith('WorkflowDebugWorkflowCoreLayer'))).toBe(false);
    }
  });

  test('every environment key on every function, with the defaults as strings', () => {
    for (const [, [, fn]] of Object.entries(functionsByName())) {
      const env = fn.Properties.Environment.Variables;
      for (const key of EXPECTED_ENV_KEYS) {
        expect(env[key]).toBeDefined();
      }
      expect(env.COMPONENT_BUCKET_PREFIX).toBe('dda-component');
      expect(env.PORTAL_REGISTRY_ENFORCED).toBe('false');
      expect(flat(env.DEPLOYMENTS_TABLE)).toContain('Ref');
      expect(flat(env.WORKFLOW_DEBUG_SESSIONS_TABLE)).toContain('WorkflowDebugSessionsTable');
      expect(env.DEBUG_LOG_RETENTION_DAYS).toBe('14');
      expect(env.DEBUG_MAX_SESSIONS_PER_USECASE).toBe('4');
      expect(env.DEBUG_MAX_DEVICES_PER_SESSION).toBe('20');
      expect(env.DEBUG_MAX_MESSAGES_PER_SECOND).toBe('10');
      expect(env.DEBUG_MAX_BYTES_PER_SECOND).toBe('262144');
      expect(env.DEBUG_INGEST_CONCURRENCY).toBe('20');
      expect(env.DEBUG_GET_LOG_EVENTS_PER_SECOND).toBe('4');
      expect(env.DEBUG_FILTER_LOG_EVENTS_PER_SECOND).toBe('2');
      expect(env.DEBUG_PORTAL_RULE_NAME).toBe('dda_dbg_ingest_portal');
    }
  });

  test('PORTAL_REGISTRY_ENFORCED follows the context value on every function', () => {
    for (const resource of Object.values(enforcedTemplate.findResources('AWS::Lambda::Function')) as any[]) {
      if (typeof resource.Properties.FunctionName !== 'string') continue;
      expect(resource.Properties.Environment.Variables.PORTAL_REGISTRY_ENFORCED).toBe('true');
    }
  });
});

// ---------------------------------------------------------------------------
// IAM (design §9)
// ---------------------------------------------------------------------------
describe('the ingest role', () => {
  test('six logs actions on the two debug group patterns', () => {
    const [s] = statementsWithSid(roleLogicalIdOf(INGEST_FUNCTION_NAME), 'DebugLogGroupWrite');
    expect(actionsOf(s).sort()).toEqual([
      'logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:DescribeLogStreams',
      'logs:PutLogEvents', 'logs:PutRetentionPolicy', 'logs:TagResource',
    ].sort());
    const resources = resourcesOf(s).map(flat);
    expect(resources).toHaveLength(2);
    expect(resources.some((r) => r.includes(':log-group:/dda/workflow-debug/*"') || r.endsWith('/dda/workflow-debug/*"]}'))).toBe(true);
    expect(resources.some((r) => r.includes('/dda/workflow-debug/*:*'))).toBe(true);
  });

  test('sessions table reads and writes with the by-pair index, attribution reads, the certificate lookup, AssumeRole and the queue', () => {
    const roleId = roleLogicalIdOf(INGEST_FUNCTION_NAME);
    const [sessions] = statementsWithSid(roleId, 'DebugSessionsIngest');
    expect(actionsOf(sessions).sort()).toEqual(['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:UpdateItem']);
    expect(resourcesOf(sessions)).toHaveLength(2);
    expect(flat(resourcesOf(sessions))).toContain('/index/by-pair');
    expect(flat(resourcesOf(sessions))).not.toContain('/index/by-usecase');
    const [attribution] = statementsWithSid(roleId, 'DebugAttributionReads');
    expect(actionsOf(attribution)).toEqual(['dynamodb:GetItem']);
    expect(resourcesOf(attribution)).toHaveLength(3);
    const [identity] = statementsWithSid(roleId, 'DebugIdentityLookup');
    expect(actionsOf(identity)).toEqual(['iot:ListPrincipalThings']);
    expect(flat(resourcesOf(identity))).toContain(':cert/*');
    const [assume] = statementsWithSid(roleId, 'DebugAssumeUseCaseRole');
    expect(actionsOf(assume)).toEqual(['sts:AssumeRole']);
    expect(resourcesOf(assume).sort()).toEqual([
      `arn:aws:iam::${TRUSTED_A}:role/DDAPortalAccessRole`,
      `arn:aws:iam::${TRUSTED_B}:role/DDAPortalAccessRole`,
    ]);
    const sqs = statementsOfRole(roleId).filter((s) => actionsOf(s).includes('sqs:SendMessage'));
    expect(sqs.length).toBeGreaterThanOrEqual(1);
    const queues = Object.keys(template.findResources('AWS::SQS::Queue'));
    expect(flat(resourcesOf(sqs[0]))).toContain(queues[0]);
  });
});

describe('the API role', () => {
  test('five logs actions without DescribeLogStreams', () => {
    const [s] = statementsWithSid(roleLogicalIdOf(API_FUNCTION_NAME), 'DebugLogGroupPrepareAndRead');
    expect(actionsOf(s).sort()).toEqual([
      'logs:CreateLogGroup', 'logs:FilterLogEvents', 'logs:GetLogEvents', 'logs:PutRetentionPolicy', 'logs:TagResource',
    ].sort());
    for (const statement of statementsOfRole(roleLogicalIdOf(API_FUNCTION_NAME))) {
      expect(actionsOf(statement)).not.toContain('logs:DescribeLogStreams');
    }
  });

  test('exactly the six DynamoDB actions on the sessions table and Query on its two indexes', () => {
    const roleId = roleLogicalIdOf(API_FUNCTION_NAME);
    const [table] = statementsWithSid(roleId, 'DebugSessionsTable');
    expect(actionsOf(table).sort()).toEqual([
      'dynamodb:ConditionCheckItem', 'dynamodb:DeleteItem', 'dynamodb:GetItem',
      'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:UpdateItem',
    ]);
    expect(resourcesOf(table)).toHaveLength(1);
    expect(flat(resourcesOf(table))).toContain('WorkflowDebugSessionsTable');
    expect(flat(resourcesOf(table))).not.toContain('/index/');
    const [indexes] = statementsWithSid(roleId, 'DebugSessionsIndexes');
    expect(actionsOf(indexes)).toEqual(['dynamodb:Query']);
    const indexArns = resourcesOf(indexes).map(flat);
    expect(indexArns).toHaveLength(2);
    expect(indexArns.some((r) => r.includes('/index/by-usecase'))).toBe(true);
    expect(indexArns.some((r) => r.includes('/index/by-pair'))).toBe(true);
    for (const statement of statementsOfRole(roleId)) {
      for (const action of actionsOf(statement)) {
        expect(action).not.toBe('dynamodb:*');
        expect(action).not.toBe('dynamodb:BatchWriteItem');
      }
    }
  });

  test('GetItem and Scan only on Devices; the Deployments index Query; GetItem on UseCases, UserRoles, Workflows; PutItem on AuditLog', () => {
    const roleId = roleLogicalIdOf(API_FUNCTION_NAME);
    const [devices] = statementsWithSid(roleId, 'DebugDeviceRecords');
    expect(actionsOf(devices).sort()).toEqual(['dynamodb:GetItem', 'dynamodb:Scan']);
    expect(resourcesOf(devices)).toHaveLength(1);
    for (const statement of statementsOfRole(roleId)) {
      if (actionsOf(statement).includes('dynamodb:Scan')) {
        expect(statement.Sid).toBe('DebugDeviceRecords');
      }
    }
    const [deployments] = statementsWithSid(roleId, 'DebugDeploymentTargets');
    expect(actionsOf(deployments)).toEqual(['dynamodb:Query']);
    expect(flat(resourcesOf(deployments))).toContain('/index/usecase-deployments-index');
    const [reads] = statementsWithSid(roleId, 'DebugUseCaseAndRoleReads');
    expect(actionsOf(reads)).toEqual(['dynamodb:GetItem']);
    expect(resourcesOf(reads)).toHaveLength(2);
    const [workflows] = statementsWithSid(roleId, 'DebugWorkflowReads');
    expect(actionsOf(workflows)).toEqual(['dynamodb:GetItem']);
    const [audit] = statementsWithSid(roleId, 'DebugAuditWrite');
    expect(actionsOf(audit)).toEqual(['dynamodb:PutItem']);
  });

  test('shadow control, the image and retention S3 statements and AssumeRole', () => {
    const roleId = roleLogicalIdOf(API_FUNCTION_NAME);
    const [shadow] = statementsWithSid(roleId, 'DebugShadowControl');
    expect(actionsOf(shadow).sort()).toEqual(['iot:GetThingShadow', 'iot:UpdateThingShadow']);
    expect(flat(resourcesOf(shadow))).toContain(':thing/*');
    const [read] = statementsWithSid(roleId, 'DebugImageRead');
    expect(actionsOf(read)).toEqual(['s3:GetObject']);
    expect(flat(resourcesOf(read))).toContain('arn:aws:s3:::dda-inference-results-');
    expect(flat(resourcesOf(read))).toContain('/dda-telemetry/debug/*');
    const [list] = statementsWithSid(roleId, 'DebugImageList');
    expect(actionsOf(list)).toEqual(['s3:ListBucket']);
    expect(list.Condition).toEqual({ StringLike: { 's3:prefix': 'dda-telemetry/debug/*' } });
    const [retention] = statementsWithSid(roleId, 'DebugMediaRetentionRead');
    expect(actionsOf(retention).sort()).toEqual(['s3:GetBucketVersioning', 's3:GetLifecycleConfiguration']);
    const [assume] = statementsWithSid(roleId, 'DebugAssumeUseCaseRole');
    expect(resourcesOf(assume)).toHaveLength(2);
    for (const statement of statementsOfRole(roleId)) {
      for (const resource of resourcesOf(statement)) {
        const text = flat(resource);
        expect(text).not.toBe('"arn:aws:s3:::*"');
        expect(text).not.toBe('"arn:aws:s3:::*/*"');
      }
    }
  });
});

describe('the maintenance role', () => {
  test('no PutItem on the sessions table, no by-pair read, the recreate and clear statements', () => {
    const roleId = roleLogicalIdOf(MAINTENANCE_FUNCTION_NAME);
    const [table] = statementsWithSid(roleId, 'DebugSessionsMaintenance');
    expect(actionsOf(table).sort()).toEqual([
      'dynamodb:ConditionCheckItem', 'dynamodb:DeleteItem', 'dynamodb:GetItem',
      'dynamodb:Query', 'dynamodb:Scan', 'dynamodb:UpdateItem',
    ]);
    expect(actionsOf(table)).not.toContain('dynamodb:PutItem');
    const [index] = statementsWithSid(roleId, 'DebugSessionsIndexMaintenance');
    expect(flat(resourcesOf(index))).toContain('/index/by-usecase');
    expect(flat(statementsOfRole(roleId))).not.toContain('/index/by-pair');
    const [logsStatement] = statementsWithSid(roleId, 'DebugLogGroupRecreate');
    expect(actionsOf(logsStatement).sort()).toEqual(['logs:CreateLogGroup', 'logs:PutRetentionPolicy', 'logs:TagResource']);
    const [shadow] = statementsWithSid(roleId, 'DebugShadowClear');
    expect(actionsOf(shadow)).toEqual(['iot:UpdateThingShadow']);
    const [audit] = statementsWithSid(roleId, 'DebugAuditWrite');
    expect(actionsOf(audit)).toEqual(['dynamodb:PutItem']);
    expect(flat(resourcesOf(audit))).not.toContain('WorkflowDebugSessionsTable');
    const [assume] = statementsWithSid(roleId, 'DebugAssumeUseCaseRole');
    expect(actionsOf(assume)).toEqual(['sts:AssumeRole']);
  });
});

describe('every role', () => {
  test('no logs:DeleteLogStream anywhere, no wildcard administrative action', () => {
    for (const statement of allStatements()) {
      for (const action of actionsOf(statement)) {
        expect(action).not.toBe('logs:DeleteLogStream');
        expect(action).not.toBe('logs:DeleteLogGroup');
        expect(action).not.toBe('*');
        expect(action).not.toMatch(/:\*$/);
      }
    }
  });

  test('the schedule role may invoke the maintenance function only', () => {
    const roles = Object.entries(template.findResources('AWS::IAM::Role')).filter(([id]) =>
      id.startsWith('WorkflowDebugScheduleRole'),
    );
    expect(roles).toHaveLength(1);
    const [roleId, role] = roles[0] as [string, any];
    expect(role.Properties.AssumeRolePolicyDocument.Statement[0].Principal.Service).toBe('scheduler.amazonaws.com');
    const statements = statementsOfRole(roleId);
    expect(statements).toHaveLength(1);
    expect(actionsOf(statements[0])).toEqual(['lambda:InvokeFunction']);
    expect(resourcesOf(statements[0])).toHaveLength(1);
    expect(flat(resourcesOf(statements[0]))).toContain('WorkflowDebugMaintenance');
    const schedules = Object.values(template.findResources('AWS::Scheduler::Schedule')) as any[];
    expect(schedules).toHaveLength(1);
    expect(schedules[0].Properties.ScheduleExpression).toBe('rate(5 minutes)');
    expect(schedules[0].Properties.Target.RoleArn['Fn::GetAtt'][0]).toBe(roleId);
  });
});

// ---------------------------------------------------------------------------
// table, routes, deployment, alarms, deployment values
// ---------------------------------------------------------------------------
describe('the sessions table', () => {
  test('keys, the two indexes, TTL, PITR and on-demand billing', () => {
    const tables = Object.values(template.findResources('AWS::DynamoDB::Table')) as any[];
    const table = tables.find((t) => t.Properties.TableName === 'dda-portal-workflow-debug-sessions');
    expect(table).toBeDefined();
    expect(table.Properties.KeySchema).toEqual([
      { AttributeName: 'pk', KeyType: 'HASH' },
      { AttributeName: 'sk', KeyType: 'RANGE' },
    ]);
    expect(table.Properties.BillingMode).toBe('PAY_PER_REQUEST');
    expect(table.Properties.TimeToLiveSpecification).toEqual({ AttributeName: 'expires_at', Enabled: true });
    expect(table.Properties.PointInTimeRecoverySpecification.PointInTimeRecoveryEnabled).toBe(true);
    const indexes = table.Properties.GlobalSecondaryIndexes.map((g: any) => [g.IndexName, g.KeySchema]);
    expect(indexes).toEqual([
      ['by-usecase', [{ AttributeName: 'gsi_usecase', KeyType: 'HASH' }, { AttributeName: 'gsi_sort', KeyType: 'RANGE' }]],
      ['by-pair', [{ AttributeName: 'gsi_pair', KeyType: 'HASH' }, { AttributeName: 'gsi_sort', KeyType: 'RANGE' }]],
    ]);
    expect(table.DeletionPolicy).toBe('Retain');
  });
});

describe('the routes', () => {
  test('the six resources carry the expected methods; non-OPTIONS methods use the shared Cognito authorizer, OPTIONS are MOCK', () => {
    const methods = Object.values(template.findResources('AWS::ApiGateway::Method')) as any[];
    const nonOptions = methods.filter((m) => m.Properties.HttpMethod !== 'OPTIONS');
    const options = methods.filter((m) => m.Properties.HttpMethod === 'OPTIONS');
    expect(nonOptions).toHaveLength(7);
    for (const method of nonOptions) {
      expect(method.Properties.AuthorizationType).toBe('COGNITO_USER_POOLS');
      expect(method.Properties.AuthorizerId).toBe(SHARED_AUTHORIZER_ID);
      expect(method.Properties.Integration.Type).toBe('AWS_PROXY');
    }
    expect(options.length).toBeGreaterThanOrEqual(1);
    for (const method of options) {
      expect(method.Properties.Integration.Type).toBe('MOCK');
      expect(method.Properties.AuthorizationType).toBe('NONE');
    }
    const resources = Object.values(template.findResources('AWS::ApiGateway::Resource')) as any[];
    const parts = resources.map((r) => r.Properties.PathPart).sort();
    expect(parts).toEqual(['console-link', 'debug', 'images', 'log', 'sessions', 'sessions', 'usecases',
      '{execution_id}', '{session_id}', '{usecase_id}'].sort());
    const byMethod = nonOptions.map((m) => m.Properties.HttpMethod).sort();
    expect(byMethod).toEqual(['DELETE', 'GET', 'GET', 'GET', 'GET', 'GET', 'POST']);
  });

  test('the stack creates no authorizer: the API already has 10, the per-REST-API quota', () => {
    expect(Object.keys(template.findResources('AWS::ApiGateway::Authorizer'))).toHaveLength(0);
    expect(Object.keys(enforcedTemplate.findResources('AWS::ApiGateway::Authorizer'))).toHaveLength(0);
  });

  test('a missing, blank or malformed sharedAuthorizerId prop stops the synth', () => {
    expect(() => buildStack(undefined, { sharedAuthorizerId: undefined })).toThrow(/WorkflowDebugStack requires sharedAuthorizerId/);
    expect(() => buildStack(undefined, { sharedAuthorizerId: '' })).toThrow(/sharedAuthorizerId/);
    expect(() => buildStack(undefined, { sharedAuthorizerId: '  ' })).toThrow(/sharedAuthorizerId/);
    expect(() => buildStack(undefined, { sharedAuthorizerId: 'Fixture-0' })).toThrow(/lowercase letters and digits/);
    expect(() => buildStack(undefined, { sharedAuthorizerId: 'abc123\tdef456' })).toThrow(/lowercase letters and digits/);
    // The stack reads the prop, not the context: a context value alone does not satisfy it.
    expect(() => buildStack({ sharedAuthorizerId: 'fixture2' }, { sharedAuthorizerId: undefined }))
      .toThrow(/WorkflowDebugStack requires sharedAuthorizerId/);
    // A padded id is trimmed, not refused.
    const padded = buildStack(undefined, { sharedAuthorizerId: ' fixture1 ' });
    const methods = Object.values(padded.findResources('AWS::ApiGateway::Method')) as any[];
    for (const method of methods.filter((m) => m.Properties.HttpMethod !== 'OPTIONS')) {
      expect(method.Properties.AuthorizerId).toBe('fixture1');
    }
  }, 300_000);

  test('the route-salted deployment is retained and depends on every method of the route table', () => {
    const deployments = Object.entries(template.findResources('AWS::ApiGateway::Deployment')) as [string, any][];
    expect(deployments).toHaveLength(1);
    const [logicalId, deployment] = deployments[0];
    expect(logicalId).toMatch(/^WorkflowDebugDeployment[0-9a-f]{16}$/);
    expect(deployment.DeletionPolicy).toBe('Retain');
    expect(deployment.UpdateReplacePolicy).toBe('Retain');
    expect(deployment.Properties.StageName).toBe('v1');
    const methodIds = Object.keys(template.findResources('AWS::ApiGateway::Method'));
    expect(methodIds.length).toBeGreaterThanOrEqual(8);
    expect(deployment.DependsOn).toEqual(expect.arrayContaining(methodIds));
  });
});

describe('alarms', () => {
  test('IdentityUnavailable and the failure queue depth', () => {
    const alarms = Object.values(template.findResources('AWS::CloudWatch::Alarm')) as any[];
    expect(alarms).toHaveLength(2);
    const identity = alarms.find((a) => a.Properties.MetricName === 'IdentityUnavailable');
    expect(identity.Properties.Namespace).toBe('DDA/WorkflowDebug');
    expect(identity.Properties.Threshold).toBe(1);
    const queue = alarms.find((a) => a.Properties.MetricName === 'ApproximateNumberOfMessagesVisible');
    expect(queue.Properties.Namespace).toBe('AWS/SQS');
  });
});

describe('deployment values (design §10, §13)', () => {
  test('defaults and ranges', () => {
    const byKey = Object.fromEntries(DEBUG_DEPLOYMENT_VALUES.map((v) => [v.key, v]));
    expect(byKey.debugLogRetentionDays).toMatchObject({ defaultValue: 14, min: 1, max: 90 });
    expect(byKey.debugMaxSessionsPerUsecase).toMatchObject({ defaultValue: 4, min: 1, max: 20 });
    expect(byKey.debugMaxDevicesPerSession).toMatchObject({ defaultValue: 20, min: 1, max: 32 });
    expect(byKey.debugMaxMessagesPerSecond).toMatchObject({ defaultValue: 10, min: 1, max: 20 });
    expect(byKey.debugMaxBytesPerSecond).toMatchObject({ defaultValue: 262144, min: 32768, max: 1048576 });
    expect(byKey.debugIngestConcurrency).toMatchObject({ defaultValue: 20, min: 10, max: 200 });
    expect(byKey.debugGetLogEventsPerSecond).toMatchObject({ defaultValue: 4, min: 1, max: 8 });
    expect(byKey.debugFilterLogEventsPerSecond).toMatchObject({ defaultValue: 2, min: 1, max: 4 });
  });

  test('a value outside its range throws at synth; an in-range value reaches the environment', () => {
    expect(() => buildStack({ debugMaxDevicesPerSession: '33' })).toThrow(/debugMaxDevicesPerSession/);
    expect(() => buildStack({ debugIngestConcurrency: 9 })).toThrow(/debugIngestConcurrency/);
    expect(() => buildStack({ debugMaxBytesPerSecond: '32767' })).toThrow(/debugMaxBytesPerSecond/);
    expect(() => buildStack({ debugLogRetentionDays: 'ninety' })).toThrow(/debugLogRetentionDays/);
    expect(resolveDeploymentValue(DEBUG_DEPLOYMENT_VALUES[0], undefined)).toBe(14);
    expect(resolveDeploymentValue(DEBUG_DEPLOYMENT_VALUES[0], '30')).toBe(30);
    expect(() => resolveDeploymentValue(DEBUG_DEPLOYMENT_VALUES[0], 2.5)).toThrow();
    const tuned = buildStack({ debugMaxDevicesPerSession: '32', debugIngestConcurrency: '200' });
    const fns = Object.values(tuned.findResources('AWS::Lambda::Function')) as any[];
    const ingest = fns.find((f) => f.Properties.FunctionName === INGEST_FUNCTION_NAME);
    expect(ingest.Properties.ReservedConcurrentExecutions).toBe(200);
    expect(ingest.Properties.Environment.Variables.DEBUG_MAX_DEVICES_PER_SESSION).toBe('32');
  }, 300_000);

  test('an empty trusted account list is refused at synth', () => {
    const app = new cdk.App();
    const storage = new StorageStack(app, 'Storage');
    const deps = new cdk.Stack(app, 'Deps');
    expect(() => new WorkflowDebugStack(app, 'WorkflowDebug', {
      useCasesTable: storage.useCasesTable,
      userRolesTable: storage.userRolesTable,
      devicesTable: storage.devicesTable,
      deploymentsTable: storage.deploymentsTable,
      auditLogTable: storage.auditLogTable,
      settingsTable: storage.settingsTable,
      workflowsTable: storage.workflowsTable,
      workflowVersionsTable: storage.workflowVersionsTable,
      portalArtifactsBucket: storage.portalArtifactsBucket,
      userPool: new cognito.UserPool(deps, 'Pool'),
      trustedUseCaseAccountIds: [],
      restApiId: 'testrestapiid',
      restApiRootResourceId: 'testrootresourceid',
      apiStageName: 'v1',
      sharedAuthorizerId: SHARED_AUTHORIZER_ID,
    })).toThrow(/trustedUseCaseAccountIds/);
  });
});
