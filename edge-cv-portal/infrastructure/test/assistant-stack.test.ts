/**
 * EdgeCVPortalAssistantStack (spec .kiro/specs/portal-ai-assistant/,
 * design §10; tasks.md 3.2, 3.5). Requirements 6.1, 12, 13.1, 13.3, 13.4.
 *
 * Synthesizes the stack against fixture producers (no bundling: the layer
 * assets keep their source hash) and asserts the resources, the Cognito
 * authorization of every method, the throttles, the two roles' exact
 * statements, the worker's invoke configuration, the outputs, and that the
 * stack exports nothing. `assistantEnabled` and the guide bundling are
 * covered too.
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as fc from 'fast-check';
import {
  ASSISTANT_API_NAME,
  ASSISTANT_BACKEND_FUNCTIONS_PARAMETER_NAME,
  ASSISTANT_KEY_ALIAS,
  ASSISTANT_ROUTES,
  ASSISTANT_TABLE_NAME,
  ASSISTANT_VERSION,
  AssistantStack,
  AssistantStackProps,
  GUIDE_SOURCE_DIR,
  guideLayerBundling,
} from '../lib/assistant-stack';
import { assistantEnabled, mcpServerEnabled } from '../lib/context-helpers';
import { MCP_BACKEND_KEYS, McpBackendKey } from '../lib/mcp-stack';

const ENV = { account: '111111111111', region: 'us-east-1' };
const NO_BUNDLING = { 'aws:cdk:bundling-stacks': [] as string[] };
const outdirs: string[] = [];

function tmpOutdir(prefix: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  outdirs.push(dir);
  return dir;
}

afterAll(() => {
  for (const dir of outdirs) fs.rmSync(dir, { recursive: true, force: true });
});

interface Unit {
  stack: AssistantStack;
  template: Template;
}

function unit(overrides: Partial<AssistantStackProps> = {}, context: Record<string, unknown> = {}): Unit {
  const app = new cdk.App({ outdir: tmpOutdir('assistant-stack-'), context: { ...NO_BUNDLING, ...context } });
  const producer = new cdk.Stack(app, 'Producer', { env: ENV });
  const userPool = new cognito.UserPool(producer, 'Pool');
  const table = (id: string) =>
    new dynamodb.Table(producer, id, { partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING } });
  const handlers = {} as Record<McpBackendKey, lambda.IFunction>;
  for (const key of MCP_BACKEND_KEYS) {
    handlers[key] = new lambda.Function(producer, `Fn-${key}`, {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context):\n    return {}\n'),
    });
  }
  const stack = new AssistantStack(app, 'EdgeCVPortalAssistantStack', {
    env: ENV,
    userPool,
    userRolesTable: table('UserRoles'),
    auditLogTable: table('AuditLog'),
    usecasesTable: table('UseCases'),
    settingsTable: table('Settings'),
    backendHandlers: handlers,
    ...overrides,
  });
  return { stack, template: Template.fromStack(stack) };
}

function resourcesOf(template: Template, type: string): Record<string, any> {
  return template.findResources(type);
}

function functionsByHandler(template: Template): Record<string, any> {
  const out: Record<string, any> = {};
  for (const resource of Object.values(resourcesOf(template, 'AWS::Lambda::Function'))) {
    out[(resource as any).Properties.Handler] = (resource as any).Properties;
  }
  return out;
}

function statementsOf(template: Template, roleIdPrefix: string): any[] {
  const policies = Object.entries(resourcesOf(template, 'AWS::IAM::Policy'))
    .filter(([id]) => id.startsWith(roleIdPrefix));
  const roles = Object.entries(resourcesOf(template, 'AWS::IAM::Role'))
    .filter(([id]) => id.startsWith(roleIdPrefix));
  const statements: any[] = [];
  for (const [, role] of roles) {
    for (const policy of (role as any).Properties.Policies ?? []) {
      statements.push(...policy.PolicyDocument.Statement);
    }
  }
  for (const [, policy] of policies) {
    statements.push(...(policy as any).Properties.PolicyDocument.Statement);
  }
  return statements;
}

describe('EdgeCVPortalAssistantStack resources', () => {
  let built: Unit;
  beforeAll(() => {
    built = unit();
  });

  test('one table under a dedicated rotating key, TTL and PITR on, both retained', () => {
    const { template } = built;
    template.resourceCountIs('AWS::DynamoDB::Table', 1);
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: ASSISTANT_TABLE_NAME,
      BillingMode: 'PAY_PER_REQUEST',
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      SSESpecification: Match.objectLike({ SSEEnabled: true, SSEType: 'KMS' }),
      KeySchema: [{ AttributeName: 'pk', KeyType: 'HASH' }, { AttributeName: 'sk', KeyType: 'RANGE' }],
    });
    template.hasResource('AWS::DynamoDB::Table', { DeletionPolicy: 'Retain' });
    template.resourceCountIs('AWS::KMS::Key', 1);
    template.hasResourceProperties('AWS::KMS::Key', { EnableKeyRotation: true });
    template.hasResource('AWS::KMS::Key', { DeletionPolicy: 'Retain' });
    template.hasResourceProperties('AWS::KMS::Alias', { AliasName: ASSISTANT_KEY_ALIAS });
  });

  test('the dispatch-map parameter holds the 43 backend ARNs in the advanced tier', () => {
    const { template } = built;
    const [parameter] = Object.values(resourcesOf(template, 'AWS::SSM::Parameter')) as any[];
    expect(parameter.Properties.Name).toBe(ASSISTANT_BACKEND_FUNCTIONS_PARAMETER_NAME);
    expect(parameter.Properties.Tier).toBe('Advanced');
    // The value is an Fn::Join of literal JSON fragments and 43 imported ARNs.
    const value = JSON.stringify(parameter.Properties.Value);
    for (const key of MCP_BACKEND_KEYS) {
      expect(value).toContain(`${key}`);
    }
    expect((value.match(/Fn::ImportValue/g) ?? []).length).toBe(MCP_BACKEND_KEYS.length);
  });

  test('three layers and two Python 3.11 functions with the designed sizing', () => {
    const { template } = built;
    template.resourceCountIs('AWS::Lambda::LayerVersion', 3);
    const functions = functionsByHandler(template);
    expect(Object.keys(functions).sort()).toEqual(['assistant.api.handler', 'assistant.worker.handler']);
    const worker = functions['assistant.worker.handler'];
    expect(worker.Runtime).toBe('python3.11');
    expect(worker.Timeout).toBe(300);
    expect(worker.MemorySize).toBe(1024);
    expect(worker.ReservedConcurrentExecutions).toBe(10);
    expect(worker.Layers).toHaveLength(3);
    expect(worker.Environment.Variables).toEqual(expect.objectContaining({
      MCP_BACKEND_FUNCTIONS_PARAMETER: ASSISTANT_BACKEND_FUNCTIONS_PARAMETER_NAME,
      ASSISTANT_GUIDE_DIR: '/opt/guide',
      ASSISTANT_VERSION,
      PORTAL_REGISTRY_ENFORCED: 'false',
    }));
    expect(worker.Environment.Variables.ASSISTANT_SUPPORT_EMAIL).toBeUndefined();
    const api = functions['assistant.api.handler'];
    expect(api.Timeout).toBe(29);
    expect(api.MemorySize).toBe(256);
    expect(api.Layers).toHaveLength(2);
    expect(api.Environment.Variables.ASSISTANT_WORKER_FUNCTION).toBeDefined();
    expect(api.Environment.Variables.MCP_BACKEND_FUNCTIONS_PARAMETER).toBeUndefined();
  });

  test('the worker is invoked asynchronously without retries', () => {
    built.template.hasResourceProperties('AWS::Lambda::EventInvokeConfig', {
      MaximumRetryAttempts: 0,
      MaximumEventAgeInSeconds: 120,
      Qualifier: '$LATEST',
    });
  });

  test('a Regional REST API with every method behind the Cognito authorizer', () => {
    const { template } = built;
    template.hasResourceProperties('AWS::ApiGateway::RestApi', {
      Name: ASSISTANT_API_NAME,
      EndpointConfiguration: { Types: ['REGIONAL'] },
    });
    template.resourceCountIs('AWS::ApiGateway::Authorizer', 1);
    template.hasResourceProperties('AWS::ApiGateway::Authorizer', {
      Type: 'COGNITO_USER_POOLS',
      IdentitySource: 'method.request.header.Authorization',
    });
    const methods = Object.values(resourcesOf(template, 'AWS::ApiGateway::Method')) as any[];
    const real = methods.filter((m) => m.Properties.HttpMethod !== 'OPTIONS');
    const preflight = methods.filter((m) => m.Properties.HttpMethod === 'OPTIONS');
    expect(real).toHaveLength(ASSISTANT_ROUTES.length);
    for (const method of real) {
      expect(method.Properties.AuthorizationType).toBe('COGNITO_USER_POOLS');
      expect(method.Properties.AuthorizerId).toBeDefined();
      expect(method.Properties.Integration.Type).toBe('AWS_PROXY');
    }
    for (const method of preflight) {
      expect(method.Properties.Integration.Type).toBe('MOCK');
    }
    const paths = Object.values(resourcesOf(template, 'AWS::ApiGateway::Resource')) as any[];
    const parts = paths.map((r) => r.Properties.PathPart).sort();
    expect(parts).toEqual(['assistant', 'confirmation', 'conversations', 'settings', 'state', 'stop',
      'turns', '{cid}', '{tid}']);
  });

  test('stage throttles: 20/40 overall and 10/20 on the poll method; CORS allows X-Portal-Build', () => {
    const { template } = built;
    template.hasResourceProperties('AWS::ApiGateway::Stage', {
      StageName: 'v1',
      MethodSettings: Match.arrayWith([
        Match.objectLike({ HttpMethod: '*', ResourcePath: '/*', ThrottlingRateLimit: 20,
          ThrottlingBurstLimit: 40, MetricsEnabled: true }),
        Match.objectLike({ HttpMethod: 'GET', ResourcePath: '/~1assistant~1conversations~1{cid}~1turns~1{tid}',
          ThrottlingRateLimit: 10, ThrottlingBurstLimit: 20 }),
      ]),
    });
    const methods = Object.values(resourcesOf(template, 'AWS::ApiGateway::Method')) as any[];
    const options = methods.find((m) => m.Properties.HttpMethod === 'OPTIONS');
    const headers = JSON.stringify(options.Properties.Integration.IntegrationResponses);
    expect(headers).toContain('X-Portal-Build');
  });

  test('the worker role: exact statements, Bedrock scoped as the Portal handlers, no wildcard action', () => {
    const statements = statementsOf(built.template, 'AssistantWorkerRole');
    const sids = statements.map((s) => s.Sid).sort();
    expect(sids).toEqual(['ApplyConfiguredGuardrail', 'AssistantKey', 'AssistantTable', 'InvokeBackingHandlers',
      'InvokeConfiguredModel', 'ListUseCasesForWhoami', 'ReadDispatchMap', 'ReadPortalIdentityRegistry',
      'ReadSettings', 'WriteAuditLog', 'WriteSettings']);
    for (const statement of statements) {
      const actions = ([] as string[]).concat(statement.Action);
      expect(actions).not.toContain('*');
      expect(actions.every((a) => !a.endsWith(':*'))).toBe(true);
      expect(statement.Resource).not.toBe('*');
    }
    const bedrock = statements.find((s) => s.Sid === 'InvokeConfiguredModel');
    expect(bedrock.Action).toEqual(['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream']);
    expect(JSON.stringify(bedrock.Resource)).toContain('arn:aws:bedrock:*::foundation-model/*');
    const invoke = statements.find((s) => s.Sid === 'InvokeBackingHandlers');
    expect(invoke.Resource).toHaveLength(MCP_BACKEND_KEYS.length);
    expect(statements.find((s) => s.Sid === 'AssistantKey').Action).toEqual(
      ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:DescribeKey']);
  });

  test('the API role: no Bedrock, no backing handlers, invokes the worker only', () => {
    const statements = statementsOf(built.template, 'AssistantApiRole');
    const sids = statements.map((s) => s.Sid).sort();
    expect(sids).toEqual(['AssistantKey', 'AssistantTable', 'InvokeWorker', 'ReadPortalIdentityRegistry',
      'ReadSettings', 'WriteAuditLog', 'WriteSettings']);
    const text = JSON.stringify(statements);
    expect(text).not.toContain('bedrock:');
    expect(text).not.toContain('ssm:');
    const invokeWorker = statements.find((s) => s.Sid === 'InvokeWorker').Resource;
    expect(([] as unknown[]).concat(invokeWorker)).toHaveLength(1);
  });

  test('four alarms without actions, three outputs, no exports', () => {
    const { template } = built;
    template.resourceCountIs('AWS::CloudWatch::Alarm', 4);
    for (const alarm of Object.values(resourcesOf(template, 'AWS::CloudWatch::Alarm')) as any[]) {
      expect(alarm.Properties.AlarmActions).toBeUndefined();
    }
    const outputs = template.toJSON().Outputs as Record<string, any>;
    // RestApi adds its own `<id>Endpoint` output; the three below are ours.
    const ours = Object.keys(outputs).filter((k) => !k.startsWith('AssistantApiEndpoint')).sort();
    expect(ours).toEqual(['AssistantApiUrl', 'AssistantTableName', 'AssistantWorkerFunctionName']);
    for (const output of Object.values(outputs)) {
      expect(output.Export).toBeUndefined();
    }
  });

  test('the backend map must be complete', () => {
    expect(() => {
      const app = new cdk.App({ outdir: tmpOutdir('assistant-stack-missing-'), context: NO_BUNDLING });
      const producer = new cdk.Stack(app, 'Producer', { env: ENV });
      const userPool = new cognito.UserPool(producer, 'Pool');
      const table = new dynamodb.Table(producer, 'T', { partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING } });
      new AssistantStack(app, 'EdgeCVPortalAssistantStack', {
        env: ENV, userPool, userRolesTable: table, auditLogTable: table, usecasesTable: table,
        settingsTable: table, backendHandlers: {} as Record<McpBackendKey, lambda.IFunction>,
      });
    }).toThrow(/backendHandlers is missing/);
  });

  test('optional support contact and documentation link reach the worker environment', () => {
    const { template } = unit({ supportEmail: 'help@example.com', documentationUrl: 'https://docs.example.com/dda' });
    const worker = functionsByHandler(template)['assistant.worker.handler'];
    expect(worker.Environment.Variables.ASSISTANT_SUPPORT_EMAIL).toBe('help@example.com');
    expect(worker.Environment.Variables.ASSISTANT_DOCUMENTATION_URL).toBe('https://docs.example.com/dda');
  });

  test('registry enforcement reaches both functions from the CDK context', () => {
    const { template } = unit({}, { portalRegistryEnforced: 'true' });
    for (const fn of Object.values(functionsByHandler(template))) {
      expect(fn.Environment.Variables.PORTAL_REGISTRY_ENFORCED).toBe('true');
    }
  });
});

describe('assistantEnabled', () => {
  test('is the mcpServerEnabled rule: only an explicit false turns the stack off', () => {
    expect(assistantEnabled(undefined)).toBe(true);
    expect(assistantEnabled('false')).toBe(false);
    expect(assistantEnabled(' FALSE ')).toBe(false);
    expect(assistantEnabled(false)).toBe(false);
    expect(assistantEnabled('0')).toBe(true);
    expect(assistantEnabled('no')).toBe(true);
    fc.assert(fc.property(fc.oneof(fc.string(), fc.boolean(), fc.constant(undefined)), (value) => {
      expect(assistantEnabled(value)).toBe(mcpServerEnabled(value));
    }));
  });
});

describe('guide layer bundling', () => {
  test('copies the chapters and the index under guide/ and nothing else', () => {
    const source = tmpOutdir('guide-src-');
    fs.writeFileSync(path.join(source, 'README.md'), '# index');
    fs.writeFileSync(path.join(source, '01-getting-started.md'), '# 1');
    fs.writeFileSync(path.join(source, 'build-pdf.sh'), 'echo');
    fs.mkdirSync(path.join(source, 'images'));
    const output = tmpOutdir('guide-out-');
    expect(guideLayerBundling(source).local!.tryBundle(output, {} as any)).toBe(true);
    expect(fs.readdirSync(path.join(output, 'guide')).sort()).toEqual(['01-getting-started.md', 'README.md']);
  });

  test('refuses an empty guide directory', () => {
    const source = tmpOutdir('guide-empty-');
    const output = tmpOutdir('guide-out2-');
    expect(() => guideLayerBundling(source).local!.tryBundle(output, {} as any)).toThrow(/no guide chapters/);
  });

  test('the repository guide directory has the index and at least 24 chapters', () => {
    const names = fs.readdirSync(GUIDE_SOURCE_DIR).filter((n) => /^[0-9]{2}-[a-z0-9-]+\.md$/.test(n));
    expect(names.length).toBeGreaterThanOrEqual(24);
    expect(fs.existsSync(path.join(GUIDE_SOURCE_DIR, 'README.md'))).toBe(true);
  });
});
