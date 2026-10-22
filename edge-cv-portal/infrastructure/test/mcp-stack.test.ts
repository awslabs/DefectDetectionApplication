/**
 * EdgeCVPortalMcpStack assertions (mcp-servers design §1 and Testing
 * Strategy "Portal infrastructure"; tasks.md 8.3). Requirements 16.6, 6.1,
 * 6.5, 2.3, 5.4.
 *
 * Three fixtures:
 * - a unit app: a producer stack (user pool + hosted-UI domain, the three
 *   tables, 43 small functions standing in for the backing handlers) and
 *   the McpStack consuming it, so every value the McpStack imports is a real
 *   cross-stack reference;
 * - the real ComputeStack (synthesized once) feeding its 32
 *   `mcpBackendHandlers` to an McpStack, for the snapshot of those members,
 *   the absence of a public layer member and the exports it gains;
 * - the real app through the CDK CLI with `-c deployMcpServer=false`.
 *
 * Every account id and identifier is visibly fake.
 */
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as fc from 'fast-check';
import { ComputeStack } from '../lib/compute-stack';
import { mcpMutatingToolsEnabled } from '../lib/context-helpers';
import {
  MCP_BACKEND_FUNCTIONS_PARAMETER_NAME,
  MCP_BACKEND_KEYS,
  MCP_COMPUTE_BACKEND_KEYS,
  MCP_LOG_GROUP_NAME,
  McpBackendKey,
  McpStack,
  McpStackProps,
  mcpBackendHandlers,
  validateMcpAllowedOrigins,
} from '../lib/mcp-stack';
import { StorageStack } from '../lib/storage-stack';

const INFRA_DIR = path.join(__dirname, '..');
const ENDPOINT_KEYS = [
  'POST /mcp', 'GET /mcp', 'DELETE /mcp',
  'POST /mcp/{view}', 'GET /mcp/{view}', 'DELETE /mcp/{view}',
];
const ROUTE_KEYS = [
  ...ENDPOINT_KEYS,
  'GET /.well-known/oauth-protected-resource',
  'GET /.well-known/oauth-protected-resource/mcp',
  'GET /.well-known/oauth-protected-resource/mcp/{view}',
  'GET /.well-known/oauth-authorization-server',
];
const ENV = { account: '111111111111', region: 'us-east-1' };

const outdirs: string[] = [];
function tmpOutdir(prefix: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  outdirs.push(dir);
  return dir;
}
afterAll(() => {
  for (const dir of outdirs) fs.rmSync(dir, { recursive: true, force: true });
});

// ----------------------------------------------------------- unit fixture

interface UnitApp {
  mcp: McpStack;
  producer: cdk.Stack;
  template: Template;
}

function unitApp(overrides: Partial<McpStackProps> = {}): UnitApp {
  const app = new cdk.App({ outdir: tmpOutdir('mcp-stack-unit-') });
  const producer = new cdk.Stack(app, 'Producer', { env: ENV });
  const userPool = new cognito.UserPool(producer, 'Pool');
  const userPoolDomain = userPool.addDomain('Domain', {
    cognitoDomain: { domainPrefix: 'fake-portal-domain' },
  });
  const table = (id: string) =>
    new dynamodb.Table(producer, id, {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
    });
  const handlers = {} as Record<McpBackendKey, lambda.IFunction>;
  for (const key of MCP_BACKEND_KEYS) {
    handlers[key] = new lambda.Function(producer, `Fn-${key}`, {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context):\n    return {}\n'),
    });
  }
  const mcp = new McpStack(app, 'EdgeCVPortalMcpStack', {
    env: ENV,
    userPool,
    userPoolDomain,
    userRolesTable: table('UserRoles'),
    auditLogTable: table('AuditLog'),
    usecasesTable: table('UseCases'),
    backendHandlers: handlers,
    mcpMutatingToolsEnabled: 'false',
    mcpRedirectUris: [],
    mcpBuildSha: 'fake0000sha0',
    ...overrides,
  });
  return { mcp, producer, template: Template.fromStack(mcp) };
}

function resourcesOf(template: Template, type: string): Record<string, any> {
  return template.findResources(type);
}

function onlyResource(template: Template, type: string): any {
  const found = Object.values(resourcesOf(template, type));
  expect(found).toHaveLength(1);
  return found[0];
}

function handlerOf(template: Template): any {
  const functions = resourcesOf(template, 'AWS::Lambda::Function');
  const handler = Object.values(functions).find(
    (fn: any) => fn.Properties.Handler === 'mcp_portal.handler.handler',
  );
  expect(handler).toBeDefined();
  return handler;
}

/** Every intrinsic replaced by a 64-character literal (the worst case of an
 * execute-api host, a table name or an id). */
function worstCase(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    const obj = value as Record<string, any>;
    if ('Fn::Join' in obj) {
      const [sep, parts] = obj['Fn::Join'];
      return (parts as unknown[]).map(worstCase).join(sep);
    }
    return 'x'.repeat(64);
  }
  return String(value);
}

function importValues(value: unknown, out: string[] = []): string[] {
  if (Array.isArray(value)) {
    value.forEach((item) => importValues(item, out));
  } else if (value && typeof value === 'object') {
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      if (key === 'Fn::ImportValue') {
        out.push(JSON.stringify(item));
      } else {
        importValues(item, out);
      }
    }
  }
  return out;
}

describe('EdgeCVPortalMcpStack (unit app)', () => {
  let unit: UnitApp;
  beforeAll(() => {
    unit = unitApp();
  });

  test('exactly the ten route keys, no $default route', () => {
    const routes = Object.values(resourcesOf(unit.template, 'AWS::ApiGatewayV2::Route'));
    const keys = routes.map((route: any) => route.Properties.RouteKey).sort();
    expect(keys).toEqual([...ROUTE_KEYS].sort());
    expect(keys).not.toContain('$default');
  });

  test('every route is AuthorizationType NONE on the one Lambda integration; no authorizer', () => {
    const integrations = resourcesOf(unit.template, 'AWS::ApiGatewayV2::Integration');
    const [integrationId] = Object.keys(integrations);
    expect(Object.keys(integrations)).toHaveLength(1);
    for (const route of Object.values(resourcesOf(unit.template, 'AWS::ApiGatewayV2::Route'))) {
      const props = (route as any).Properties;
      expect(props.AuthorizationType).toBe('NONE');
      expect(props.AuthorizerId).toBeUndefined();
      expect(props.Target).toEqual({
        'Fn::Join': ['', ['integrations/', { Ref: integrationId }]],
      });
    }
    unit.template.resourceCountIs('AWS::ApiGatewayV2::Authorizer', 0);
    unit.template.resourceCountIs('AWS::ApiGateway::Authorizer', 0);
  });

  test('the $default stage throttles 20/40 and the six endpoint routes 10/20', () => {
    const stage = onlyResource(unit.template, 'AWS::ApiGatewayV2::Stage');
    expect(stage.Properties.StageName).toBe('$default');
    expect(stage.Properties.DefaultRouteSettings).toMatchObject({
      ThrottlingRateLimit: 20,
      ThrottlingBurstLimit: 40,
    });
    expect(Object.keys(stage.Properties.RouteSettings).sort()).toEqual([...ENDPOINT_KEYS].sort());
    for (const settings of Object.values(stage.Properties.RouteSettings)) {
      expect(settings).toEqual({ ThrottlingRateLimit: 10, ThrottlingBurstLimit: 20 });
    }
  });

  test('the Lambda environment: keys, the issuer, the parameter name, no map, < 3,000 bytes', () => {
    const variables = handlerOf(unit.template).Properties.Environment.Variables;
    for (const key of [
      'MCP_CLIENT_ID', 'MCP_REQUIRED_SCOPE', 'MCP_ALLOWED_AUDIENCES', 'COGNITO_ISSUER',
      'COGNITO_HOSTED_UI_ORIGIN', 'MCP_AS_ISSUER', 'MCP_API_HOST',
      'MCP_BACKEND_FUNCTIONS_PARAMETER', 'MCP_ALLOWED_ORIGINS', 'MCP_MUTATING_TOOLS_ENABLED',
      'MCP_CANONICAL_URL', 'MCP_SERVER_VERSION', 'MCP_BUILD_SHA',
    ]) {
      expect(variables).toHaveProperty(key);
    }
    expect(variables).not.toHaveProperty('MCP_BACKEND_FUNCTIONS');
    expect(variables).not.toHaveProperty('MCP_DELIVERED_PHASES');
    expect(variables.MCP_BACKEND_FUNCTIONS_PARAMETER).toBe(MCP_BACKEND_FUNCTIONS_PARAMETER_NAME);
    // The ApiEndpoint attribute of the HTTP API, with no trailing slash.
    const apiId = Object.keys(resourcesOf(unit.template, 'AWS::ApiGatewayV2::Api'))[0];
    const issuer = variables.MCP_AS_ISSUER;
    // The attribute is https://<id>.execute-api.<region>.amazonaws.com with
    // no trailing slash; discovery.py validates the value at import.
    expect(issuer).toEqual({ 'Fn::GetAtt': [apiId, 'ApiEndpoint'] });
    expect(JSON.stringify(variables.MCP_CANONICAL_URL)).toContain('ApiEndpoint');
    expect(worstCase(variables.MCP_CANONICAL_URL).endsWith('/mcp')).toBe(true);
    expect(worstCase(variables.MCP_ALLOWED_AUDIENCES).split(',')).toHaveLength(6);
    expect(variables.MCP_ALLOWED_ORIGINS).toBe('');
    expect(variables.MCP_MUTATING_TOOLS_ENABLED).toBe('false');
    const serialized = JSON.stringify(
      Object.fromEntries(Object.entries(variables).map(([k, v]) => [k, worstCase(v)])),
    );
    expect(Buffer.byteLength(serialized, 'utf8')).toBeLessThan(3000);
  });

  test('the Advanced-tier dispatch-map parameter imports all 43 handler ARNs', () => {
    const parameter = onlyResource(unit.template, 'AWS::SSM::Parameter');
    expect(parameter.Properties).toMatchObject({
      Name: MCP_BACKEND_FUNCTIONS_PARAMETER_NAME,
      Type: 'String',
      Tier: 'Advanced',
    });
    expect(parameter.Properties.Value).toHaveProperty('Fn::Join');
    const imports = importValues(parameter.Properties.Value);
    expect(imports).toHaveLength(43);
    expect(new Set(imports).size).toBe(43);
  });

  test('the role: six inline statements, no wildcard, basic execution, < 9,000 characters', () => {
    const role = onlyResource(unit.template, 'AWS::IAM::Role');
    const policies = role.Properties.Policies;
    expect(policies).toHaveLength(1);
    const statements = policies[0].PolicyDocument.Statement;
    expect(statements).toHaveLength(6);
    const shapes = statements.map((s: any) => [s.Effect, [s.Action].flat().sort()]);
    expect(shapes).toEqual([
      ['Allow', ['lambda:InvokeFunction']],
      ['Allow', ['dynamodb:GetItem', 'dynamodb:Query']],
      ['Allow', ['dynamodb:Scan']],
      ['Allow', ['dynamodb:PutItem']],
      ['Allow', ['cognito-idp:AdminGetUser']],
      ['Allow', ['ssm:GetParameter']],
    ]);
    expect(importValues(statements[0].Resource)).toHaveLength(43);
    for (const statement of statements) {
      expect([statement.Action].flat()).not.toContain('*');
      for (const resource of [statement.Resource].flat()) {
        expect(resource).not.toBe('*');
      }
      expect(JSON.stringify(statement.Action)).not.toContain('ssm:DescribeParameters');
    }
    expect(JSON.stringify(policies[0].PolicyDocument).length).toBeLessThan(9000);
    expect(JSON.stringify(role.Properties.ManagedPolicyArns)).toContain(
      'service-role/AWSLambdaBasicExecutionRole',
    );
    unit.template.resourceCountIs('AWS::IAM::Policy', 0);
  });

  test('two own layers, none imported; timeout 29 s; 512 MB', () => {
    unit.template.resourceCountIs('AWS::Lambda::LayerVersion', 2);
    const layers = Object.keys(resourcesOf(unit.template, 'AWS::Lambda::LayerVersion'));
    expect(layers.some((id) => id.startsWith('McpSharedLayer'))).toBe(true);
    expect(layers.some((id) => id.startsWith('McpLayer'))).toBe(true);
    const handler = handlerOf(unit.template);
    expect(importValues(handler.Properties.Layers)).toEqual([]);
    expect(handler.Properties.Layers).toHaveLength(2);
    expect(handler.Properties.Timeout).toBe(29);
    expect(handler.Properties.MemorySize).toBe(512);
  });

  test('log group, timeout metric filter and alarm; no LogRetention custom resource', () => {
    const logGroups = resourcesOf(unit.template, 'AWS::Logs::LogGroup');
    expect(Object.keys(logGroups)).toHaveLength(1);
    const [logGroupId, logGroup] = Object.entries(logGroups)[0];
    expect((logGroup as any).Properties.LogGroupName).toBe(MCP_LOG_GROUP_NAME);
    expect(handlerOf(unit.template).Properties.LoggingConfig.LogGroup).toEqual({ Ref: logGroupId });
    const filter = onlyResource(unit.template, 'AWS::Logs::MetricFilter');
    expect(filter.Properties.FilterPattern).toBe('"Task timed out"');
    expect(filter.Properties.LogGroupName).toEqual({ Ref: logGroupId });
    const transformation = filter.Properties.MetricTransformations[0];
    const alarm = onlyResource(unit.template, 'AWS::CloudWatch::Alarm');
    expect(alarm.Properties.MetricName).toBe(transformation.MetricName);
    expect(alarm.Properties.Namespace).toBe(transformation.MetricNamespace);
    unit.template.resourceCountIs('Custom::LogRetention', 0);
  });

  test('the MCP app client: public, code grant, refresh-only explicit flows, no openid', () => {
    const client = onlyResource(unit.template, 'AWS::Cognito::UserPoolClient');
    expect(client.Properties.GenerateSecret).toBe(false);
    expect(client.Properties.AllowedOAuthFlows).toEqual(['code']);
    expect(client.Properties.AllowedOAuthFlowsUserPoolClient).toBe(true);
    expect(JSON.stringify(client.Properties.AllowedOAuthScopes)).not.toContain('openid');
    unit.template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      ExplicitAuthFlows: ['ALLOW_REFRESH_TOKEN_AUTH'],
    });
    unit.template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      ExplicitAuthFlows: Match.not(Match.arrayWith(['ALLOW_USER_SRP_AUTH'])),
    });
    for (const url of client.Properties.CallbackURLs) {
      expect(url).toMatch(/^(http:\/\/localhost(:\d+)?\/|https:\/\/)/);
    }
    expect(client.Properties.CallbackURLs).toContain('http://localhost:7899/oauth/callback');
  });

  test('the resource server and the client belong to the MCP stack, not the pool stack', () => {
    unit.template.resourceCountIs('AWS::Cognito::UserPoolResourceServer', 1);
    unit.template.resourceCountIs('AWS::Cognito::UserPoolClient', 1);
    const producer = Template.fromStack(unit.producer);
    producer.resourceCountIs('AWS::Cognito::UserPoolResourceServer', 0);
    producer.resourceCountIs('AWS::Cognito::UserPoolClient', 0);
  });

  test('the seven outputs', () => {
    expect(Object.keys(unit.template.toJSON().Outputs).sort()).toEqual([
      'McpAuthorizationServerMetadataUrl', 'McpOAuthScope', 'McpProtectedResourceMetadataUrl',
      'McpRedirectUri', 'McpServerUrl', 'McpUserPoolClientId', 'McpViewUrls',
    ]);
  });
});

describe('EdgeCVPortalMcpStack context values', () => {
  test('mcpAllowedOrigins lands joined; an entry with a path or without a scheme fails synth', () => {
    const origins = ['https://tools.example.com', 'http://localhost:6274'];
    const { template } = unitApp({ mcpAllowedOrigins: origins });
    expect(handlerOf(template).Properties.Environment.Variables.MCP_ALLOWED_ORIGINS).toBe(
      origins.join(','),
    );
    for (const bad of ['https://tools.example.com/', 'https://tools.example.com/app',
      'tools.example.com', '//tools.example.com', 'ftp://tools.example.com']) {
      expect(() => validateMcpAllowedOrigins([bad])).toThrow(/mcpAllowedOrigins/);
    }
    expect(() => unitApp({ mcpAllowedOrigins: ['https://tools.example.com/app'] })).toThrow(
      /mcpAllowedOrigins/,
    );
  });

  test("mcpMutatingToolsEnabled lands as 'true'/'false' only (fast-check over context strings)", () => {
    fc.assert(
      fc.property(fc.oneof(fc.string(), fc.constantFrom(' TRUE ', 'yes', 'off', '')), (value) => {
        expect(['true', 'false']).toContain(mcpMutatingToolsEnabled(value));
      }),
      { numRuns: 100 },
    );
    for (const value of [' Yes ', 'maybe']) {
      const { template } = unitApp({ mcpMutatingToolsEnabled: mcpMutatingToolsEnabled(value) });
      expect(handlerOf(template).Properties.Environment.Variables.MCP_MUTATING_TOOLS_ENABLED).toBe(
        value === ' Yes ' ? 'true' : 'false',
      );
    }
  });
});

// ------------------------------------------------------ ComputeStack fixture

describe('ComputeStack members the McpStack consumes', () => {
  let compute: ComputeStack;
  let computeTemplate: Template;
  let mcpTemplate: Template;
  beforeAll(() => {
    const app = new cdk.App({ outdir: tmpOutdir('mcp-stack-compute-') });
    const storage = new StorageStack(app, 'Storage');
    const deps = new cdk.Stack(app, 'Deps');
    const userPool = new cognito.UserPool(deps, 'Pool');
    const userPoolDomain = userPool.addDomain('Domain', {
      cognitoDomain: { domainPrefix: 'fake-portal-domain' },
    });
    compute = new ComputeStack(app, 'Compute', {
      userPool,
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
      trustedUseCaseAccountIds: ['111111111111'],
    });
    const fn = (id: string) =>
      new lambda.Function(deps, id, {
        runtime: lambda.Runtime.PYTHON_3_11,
        handler: 'index.handler',
        code: lambda.Code.fromInline('def handler(event, context):\n    return {}\n'),
      });
    const mcp = new McpStack(app, 'EdgeCVPortalMcpStack', {
      userPool,
      userPoolDomain,
      userRolesTable: storage.userRolesTable,
      auditLogTable: storage.auditLogTable,
      usecasesTable: storage.useCasesTable,
      backendHandlers: mcpBackendHandlers({
        computeHandlers: compute.mcpBackendHandlers,
        nodeDesigner: {
          pluginRecordsHandler: fn('PluginRecords'),
          pluginImporterHandler: fn('PluginImporter'),
          nodeGeneratorHandler: fn('NodeGenerator'),
          pluginBuildsHandler: fn('PluginBuilds'),
          pluginComponentsHandler: fn('PluginComponents'),
          pluginSimulatorHandler: fn('PluginSimulator'),
          customNodeTypesHandler: fn('CustomNodeTypes'),
        },
        buildFleet: {
          buildJobsHandler: fn('BuildJobs'),
          buildFleetHandler: fn('BuildFleet'),
          buildConfigHandler: fn('BuildConfig'),
        },
        syntheticData: { syntheticDataHandler: fn('SyntheticData') },
      }),
      mcpMutatingToolsEnabled: 'false',
      mcpRedirectUris: [],
      mcpBuildSha: 'fake0000sha0',
    });
    computeTemplate = Template.fromStack(compute);
    mcpTemplate = Template.fromStack(mcp);
  }, 900_000);

  test('mcpBackendHandlers pins all 32 keys and the component-configuration handler', () => {
    expect(Object.keys(compute.mcpBackendHandlers).sort()).toEqual(
      [...MCP_COMPUTE_BACKEND_KEYS].sort(),
    );
    expect(Object.keys(compute.mcpBackendHandlers)).toHaveLength(32);
    const apiGateway = compute.node.findChild('ApiGateway') as cdk.NestedStack;
    expect(compute.mcpBackendHandlers.componentConfiguration).toBe(
      (apiGateway as any).componentConfigurationHandler,
    );
    expect(compute.mcpBackendHandlers.deviceLogsAnalyzer).toBeDefined();
    // Each key names a distinct function construct.
    const paths = Object.values(compute.mcpBackendHandlers).map((f) => f.node.path);
    expect(new Set(paths).size).toBe(32);
    expect(paths.every((p) => p.startsWith('Compute/'))).toBe(true);
  });

  test('ComputeStack exposes no public layer member', () => {
    expect('sharedLayer' in compute).toBe(false);
    for (const value of Object.values(compute)) {
      expect(value instanceof lambda.LayerVersion).toBe(false);
    }
  });

  test('every ComputeStack export the McpStack imports resolves to a function ARN', () => {
    const imported = new Set(
      importValues(mcpTemplate.toJSON()).map((item) => JSON.parse(item) as string),
    );
    const outputs = computeTemplate.toJSON().Outputs as Record<string, any>;
    const resources = computeTemplate.toJSON().Resources as Record<string, any>;
    const exported = Object.values(outputs).filter(
      (output) => output.Export && imported.has(output.Export.Name),
    );
    // 31 handlers of the ComputeStack itself plus the component-configuration
    // handler of its nested API stack.
    expect(exported.length).toBe(32);
    const apiGateway = compute.node.findChild('ApiGateway') as cdk.NestedStack;
    const nestedOutputs = Template.fromStack(apiGateway).toJSON().Outputs as Record<string, any>;
    const nestedResources = Template.fromStack(apiGateway).toJSON().Resources as Record<string, any>;
    for (const output of exported) {
      const [logicalId, attribute] = output.Value['Fn::GetAtt'];
      if (attribute === 'Arn') {
        expect(resources[logicalId].Type).toBe('AWS::Lambda::Function');
      } else {
        expect(resources[logicalId].Type).toBe('AWS::CloudFormation::Stack');
        const nested = nestedOutputs[attribute.replace(/^Outputs\./, '')];
        const [nestedId, nestedAttribute] = nested.Value['Fn::GetAtt'];
        expect(nestedAttribute).toBe('Arn');
        expect(nestedResources[nestedId].Type).toBe('AWS::Lambda::Function');
      }
      expect(JSON.stringify(output.Value)).not.toMatch(/Layer/);
    }
  });
});

// --------------------------------------------------- the real app, opted out

describe('cdk synth of bin/app.ts with -c deployMcpServer=false', () => {
  let status: number | null = null;
  let stderr = '';
  let stacks: string[] = [];
  let outdir: string;
  beforeAll(() => {
    outdir = tmpOutdir('mcp-stack-optout-');
    const env: NodeJS.ProcessEnv = { ...process.env };
    for (const key of [
      'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
      'AWS_PROFILE', 'AWS_DEFAULT_PROFILE',
      'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI', 'AWS_CONTAINER_CREDENTIALS_FULL_URI',
      'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_ARN',
    ]) {
      delete env[key];
    }
    env.CDK_DEFAULT_ACCOUNT = '111111111111';
    env.CDK_DEFAULT_REGION = 'us-east-1';
    env.AWS_SHARED_CREDENTIALS_FILE = path.join(outdir, 'no-such-credentials');
    env.AWS_CONFIG_FILE = path.join(outdir, 'no-such-config');
    env.AWS_EC2_METADATA_DISABLED = 'true';
    const cdkBin = require.resolve('aws-cdk/bin/cdk');
    const result = spawnSync(
      process.execPath,
      [cdkBin, 'synth', '--quiet', '--output', outdir,
        '--context', 'portalAccountId=111111111111',
        '--context', 'externalId=fixture-eid',
        '--context', 'trustedUseCaseAccountIds=222222222222,333333333333',
        '--context', 'portalRegistryEnforced=false',
        '--context', 'deployMcpServer=false'],
      { cwd: INFRA_DIR, env, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 },
    );
    status = result.status;
    stderr = result.stderr ?? '';
    const manifestPath = path.join(outdir, 'manifest.json');
    if (fs.existsSync(manifestPath)) {
      const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
      stacks = Object.entries(manifest.artifacts ?? {})
        .filter(([, artifact]: [string, any]) => artifact.type === 'aws:cloudformation:stack')
        .map(([id]) => id);
    }
  }, 900_000);

  test('synthesizes no MCP stack and no MCP resource', () => {
    if (status !== 0) {
      throw new Error(`cdk synth exited ${status}\n${stderr.slice(-4000)}`);
    }
    expect(stacks.length).toBeGreaterThan(0);
    expect(stacks).not.toContain('EdgeCVPortalMcpStack');
    for (const stack of stacks) {
      const body = fs.readFileSync(path.join(outdir, `${stack}.template.json`), 'utf8');
      expect(body).not.toContain('mcp_portal.handler.handler');
      expect(body).not.toContain(MCP_BACKEND_FUNCTIONS_PARAMETER_NAME);
      expect(body).not.toContain('AWS::ApiGatewayV2::Api');
    }
  });
});
