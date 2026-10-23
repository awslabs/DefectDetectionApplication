import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { portalRegistryEnforced } from './context-helpers';

/**
 * EdgeCVPortalMcpStack: the Portal_MCP_Server (mcp-servers design §1).
 *
 * One Lambda (`mcp_portal.handler.handler`) behind its own HTTP API serves
 * the aggregate `/mcp` endpoint and the five view endpoints, plus the
 * OAuth discovery documents, and dispatches every tool call to the existing
 * handler Lambda of the matching REST route. Authentication happens in the
 * Lambda (the MCP SDK's bearer middleware over Cognito access tokens of the
 * MCP app client created here), so every route uses HttpNoneAuthorizer: the
 * portal REST API's authorizer quota is full (see
 * portal-api-authorizer-count.test.ts) and the `.well-known` documents are
 * unauthenticated by RFC 9728 / RFC 8414.
 *
 * The only values imported from other stacks are update-stable: function
 * ARNs, table ARNs/names and the user pool. The shared layer is this stack's
 * own copy (D31): a LayerVersion ARN changes with its content, and an
 * imported one would block every later ComputeStack update.
 */

/** The 43 backend keys the catalog dispatches to (catalog_routes.json `backend`). */
export const MCP_COMPUTE_BACKEND_KEYS = [
  'devices', 'deviceLogs', 'deviceLogsAnalyzer', 'deployments', 'models', 'modelImport',
  'modelConverter', 'training', 'compilation', 'packaging', 'greengrassPublish', 'components',
  'componentConfiguration', 'sharedComponents', 'datasets', 'preLabeledDatasets',
  'dataManagement', 'captures', 'labeling', 'ddaLabeling', 'workflows', 'workflowValidation',
  'workflowPackaging', 'workflowTesting', 'workflowGenerator', 'useCases', 'userManagement',
  'userAdmin', 'auditLogs', 'cameraRegistry', 'cameraVideoPin', 'deviceRegistrations',
] as const;
export const MCP_NODE_DESIGNER_BACKEND_KEYS = [
  'pluginRecords', 'pluginImporter', 'nodeGenerator', 'pluginBuilds', 'pluginComponents',
  'pluginSimulator', 'customNodeTypes',
] as const;
export const MCP_BUILD_FLEET_BACKEND_KEYS = ['buildJobs', 'buildFleet', 'buildConfig'] as const;
export const MCP_SYNTHETIC_DATA_BACKEND_KEYS = ['syntheticData'] as const;
export const MCP_BACKEND_KEYS = [
  ...MCP_COMPUTE_BACKEND_KEYS,
  ...MCP_NODE_DESIGNER_BACKEND_KEYS,
  ...MCP_BUILD_FLEET_BACKEND_KEYS,
  ...MCP_SYNTHETIC_DATA_BACKEND_KEYS,
] as const;
export type McpBackendKey = (typeof MCP_BACKEND_KEYS)[number];

/** The handler functions of the four producing stacks, by backend key. */
export interface McpBackendSources {
  readonly computeHandlers: Record<string, lambda.IFunction>;
  readonly nodeDesigner: {
    readonly pluginRecordsHandler: lambda.IFunction;
    readonly pluginImporterHandler: lambda.IFunction;
    readonly nodeGeneratorHandler: lambda.IFunction;
    readonly pluginBuildsHandler: lambda.IFunction;
    readonly pluginComponentsHandler: lambda.IFunction;
    readonly pluginSimulatorHandler: lambda.IFunction;
    readonly customNodeTypesHandler: lambda.IFunction;
  };
  readonly buildFleet: {
    readonly buildJobsHandler: lambda.IFunction;
    readonly buildFleetHandler: lambda.IFunction;
    readonly buildConfigHandler: lambda.IFunction;
  };
  readonly syntheticData: { readonly syntheticDataHandler: lambda.IFunction };
}

/** `backendHandlers` from the four producing stacks; throws on a missing key. */
export function mcpBackendHandlers(sources: McpBackendSources): Record<McpBackendKey, lambda.IFunction> {
  const handlers: Record<string, lambda.IFunction> = {};
  for (const key of MCP_COMPUTE_BACKEND_KEYS) {
    const fn = sources.computeHandlers[key];
    if (!fn) {
      throw new Error(`ComputeStack.mcpBackendHandlers has no '${key}' entry`);
    }
    handlers[key] = fn;
  }
  const nd = sources.nodeDesigner;
  Object.assign(handlers, {
    pluginRecords: nd.pluginRecordsHandler,
    pluginImporter: nd.pluginImporterHandler,
    nodeGenerator: nd.nodeGeneratorHandler,
    pluginBuilds: nd.pluginBuildsHandler,
    pluginComponents: nd.pluginComponentsHandler,
    pluginSimulator: nd.pluginSimulatorHandler,
    customNodeTypes: nd.customNodeTypesHandler,
    buildJobs: sources.buildFleet.buildJobsHandler,
    buildFleet: sources.buildFleet.buildFleetHandler,
    buildConfig: sources.buildFleet.buildConfigHandler,
    syntheticData: sources.syntheticData.syntheticDataHandler,
  });
  return handlers as Record<McpBackendKey, lambda.IFunction>;
}

/** The five view endpoints beside the aggregate `/mcp` (mcp_portal.VIEWS). */
export const MCP_VIEWS = ['data', 'models', 'devices', 'workflows', 'admin'] as const;
/** The callback the Kiro clients use (Client configuration section). */
export const MCP_DEFAULT_REDIRECT_URI = 'http://localhost:7899/oauth/callback';
export const MCP_BACKEND_FUNCTIONS_PARAMETER_NAME = '/dda-portal/mcp/backend-functions';
export const MCP_LOG_GROUP_NAME = '/dda-portal/mcp-server';
export const MCP_SCOPE_NAME = 'portal.tools';
/** mcp_portal.__version__ */
export const MCP_SERVER_VERSION = '1.0.0';
/** Per-route throttle of the six MCP endpoint routes (N6). */
export const MCP_ENDPOINT_ROUTE_KEYS = [
  'POST /mcp', 'GET /mcp', 'DELETE /mcp',
  'POST /mcp/{view}', 'GET /mcp/{view}', 'DELETE /mcp/{view}',
];

/**
 * Validates the extra OAuth callback URLs of the MCP app client: each must be
 * `http://localhost[:port]/path` or an `https://` URL. Cognito accepts plain
 * HTTP for `localhost` only, so `127.0.0.1` or any other plain-HTTP host is
 * refused here at synth rather than by Cognito at deploy.
 */
export function validateMcpRedirectUris(uris: string[]): string[] {
  for (const uri of uris) {
    let parsed: URL;
    try {
      parsed = new URL(uri);
    } catch {
      throw new Error(`mcpRedirectUris: '${uri}' is not a URL`);
    }
    if (parsed.protocol === 'https:' && parsed.hostname) {
      continue;
    }
    if (parsed.protocol === 'http:' && parsed.hostname === 'localhost') {
      continue;
    }
    throw new Error(
      `mcpRedirectUris: '${uri}' must be http://localhost[:port]/path or https://... ` +
        '(Cognito accepts plain HTTP for localhost only)',
    );
  }
  return uris;
}

/**
 * Validates the allowed `Origin` values of the MCP endpoints: each must be a
 * bare origin, `http(s)://host[:port]` with no path (not even `/`), query or
 * fragment, because the transport security settings compare the `Origin`
 * header byte for byte.
 */
export function validateMcpAllowedOrigins(origins: string[]): string[] {
  for (const origin of origins) {
    let parsed: URL | undefined;
    try {
      parsed = new URL(origin);
    } catch {
      parsed = undefined;
    }
    if (!parsed || !['http:', 'https:'].includes(parsed.protocol) || parsed.origin !== origin) {
      throw new Error(
        `mcpAllowedOrigins: '${origin}' must be an origin, http(s)://host[:port] with no path`,
      );
    }
  }
  return origins;
}

export interface McpStackProps extends cdk.StackProps {
  readonly userPool: cognito.IUserPool;
  /** The hosted-UI domain (AuthStack.userPoolDomain). */
  readonly userPoolDomain: cognito.UserPoolDomain;
  readonly userRolesTable: dynamodb.ITable;
  readonly auditLogTable: dynamodb.ITable;
  readonly usecasesTable: dynamodb.ITable;
  readonly backendHandlers: Record<McpBackendKey, lambda.IFunction>;
  /** The SAML provider name when the pool federates (AuthStack.identityProvider). */
  readonly ssoIdentityProviderName?: string;
  /** 'true' | 'false' (context-helpers.mcpMutatingToolsEnabled). */
  readonly mcpMutatingToolsEnabled: string;
  /** Extra OAuth callback URLs beside MCP_DEFAULT_REDIRECT_URI. */
  readonly mcpRedirectUris: string[];
  /** Allowed `Origin` values; empty by default (no browser client). */
  readonly mcpAllowedOrigins?: string[];
  readonly mcpBuildSha: string;
}

export class McpStack extends cdk.Stack {
  public readonly httpApi: apigwv2.HttpApi;
  public readonly handler: lambda.Function;
  public readonly userPoolClient: cognito.UserPoolClient;
  public readonly canonicalUrl: string;

  constructor(scope: Construct, id: string, props: McpStackProps) {
    super(scope, id, props);

    const missing = MCP_BACKEND_KEYS.filter((key) => !props.backendHandlers[key]);
    if (missing.length > 0) {
      throw new Error(`McpStack backendHandlers is missing ${missing.join(', ')}`);
    }
    const redirectUris = validateMcpRedirectUris(props.mcpRedirectUris);
    const allowedOrigins = validateMcpAllowedOrigins(props.mcpAllowedOrigins ?? []);
    const region = cdk.Stack.of(this).region;

    // ---------------------------------------------------------------- API
    // No corsPreflight: browser clients are out of scope and CORS would
    // weaken the Origin rule of the transport security settings.
    const httpApi = new apigwv2.HttpApi(this, 'McpHttpApi', {
      apiName: 'dda-portal-mcp',
      createDefaultStage: false,
    });
    this.httpApi = httpApi;
    const canonicalUrl = `${httpApi.apiEndpoint}/mcp`;
    this.canonicalUrl = canonicalUrl;
    const viewUrls = MCP_VIEWS.map((view) => `${canonicalUrl}/${view}`);
    const apiHost = cdk.Fn.select(2, cdk.Fn.split('/', httpApi.apiEndpoint));

    const stage = new apigwv2.HttpStage(this, 'McpDefaultStage', {
      httpApi,
      stageName: '$default',
      autoDeploy: true,
      throttle: { rateLimit: 20, burstLimit: 40 },
    });
    (stage.node.defaultChild as apigwv2.CfnStage).addPropertyOverride(
      'RouteSettings',
      Object.fromEntries(
        MCP_ENDPOINT_ROUTE_KEYS.map((key) => [key, { ThrottlingRateLimit: 10, ThrottlingBurstLimit: 20 }]),
      ),
    );

    // ------------------------------------------------------------ Cognito
    // In this stack's scope, not userPool.addResourceServer (which would
    // place it in AuthStack and create a cycle through the API id).
    const portalToolsScope = new cognito.ResourceServerScope({
      scopeName: MCP_SCOPE_NAME,
      scopeDescription: 'Invoke DDA portal MCP tools as the signed-in user',
    });
    const resourceServer = new cognito.UserPoolResourceServer(this, 'McpResourceServer', {
      userPool: props.userPool,
      identifier: canonicalUrl,
      userPoolResourceServerName: 'dda-portal-mcp',
      scopes: [portalToolsScope],
    });
    const supportedIdentityProviders = [cognito.UserPoolClientIdentityProvider.COGNITO];
    if (props.ssoIdentityProviderName) {
      supportedIdentityProviders.push(
        cognito.UserPoolClientIdentityProvider.custom(props.ssoIdentityProviderName),
      );
    }
    const userPoolClient = new cognito.UserPoolClient(this, 'McpUserPoolClient', {
      userPool: props.userPool,
      userPoolClientName: 'dda-portal-mcp-client',
      generateSecret: false,
      // An explicit false key is required: CDK omits ExplicitAuthFlows for
      // `{}` and Cognito then defaults to refresh + USER_SRP + CUSTOM. With
      // it, the template carries ['ALLOW_REFRESH_TOKEN_AUTH'] only, so the
      // hosted-UI code grant (PKCE) and the refresh grant are the only token
      // paths.
      authFlows: { userSrp: false },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        // No openid scope: Cognito issues no ID token to MCP clients.
        scopes: [cognito.OAuthScope.resourceServer(resourceServer, portalToolsScope)],
        callbackUrls: [MCP_DEFAULT_REDIRECT_URI, ...redirectUris],
      },
      preventUserExistenceErrors: true,
      accessTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
      supportedIdentityProviders,
    });
    this.userPoolClient = userPoolClient;
    const requiredScope = `${canonicalUrl}/${MCP_SCOPE_NAME}`;

    // ------------------------------------------------------------- layers
    const mcpSharedLayer = new lambda.LayerVersion(this, 'McpSharedLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/shared')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description:
        'shared_utils for the MCP Lambda - same asset as ComputeStack SharedLayer ' +
        '(NodeDesignerStack/BuildFleetStack/SyntheticDataStack pattern)',
    });
    const mcpLayer = new lambda.LayerVersion(this, 'McpLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/mcp')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'mcp==2.3.0 and mangum==0.22.0 for the Portal_MCP_Server',
    });

    // ------------------------------------------------------ dispatch map
    // Backend key -> function ARN, read once per Lambda container (D30). An
    // SSM parameter, not an environment variable: 43 ARNs are about 5.5 KB,
    // more than Lambda's 4 KB environment quota; the advanced tier holds 8 KB.
    const backendFunctionsParameter = new ssm.StringParameter(this, 'McpBackendFunctions', {
      parameterName: MCP_BACKEND_FUNCTIONS_PARAMETER_NAME,
      tier: ssm.ParameterTier.ADVANCED,
      description: 'MCP backend key -> handler function ARN (read once per Lambda container)',
      stringValue: cdk.Stack.of(this).toJsonString(
        Object.fromEntries(
          MCP_BACKEND_KEYS.map((key) => [key, props.backendHandlers[key].functionArn]),
        ),
      ),
    });

    // --------------------------------------------------------------- role
    // A dedicated role with exactly six inline statements and no wildcard
    // action or resource. The SSM read is an explicit statement:
    // StringParameter.grantRead would add ssm:DescribeParameters on '*'.
    const role = new iam.Role(this, 'McpPortalRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Portal_MCP_Server Lambda role',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
      inlinePolicies: {
        McpPortalPolicy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              sid: 'InvokeBackingHandlers',
              actions: ['lambda:InvokeFunction'],
              resources: MCP_BACKEND_KEYS.map((key) => props.backendHandlers[key].functionArn),
            }),
            new iam.PolicyStatement({
              sid: 'ReadPortalIdentityRegistry',
              actions: ['dynamodb:GetItem', 'dynamodb:Query'],
              resources: [props.userRolesTable.tableArn],
            }),
            new iam.PolicyStatement({
              sid: 'ListUseCasesForWhoami',
              actions: ['dynamodb:Scan'],
              resources: [props.usecasesTable.tableArn],
            }),
            new iam.PolicyStatement({
              sid: 'WriteAuditLog',
              actions: ['dynamodb:PutItem'],
              resources: [props.auditLogTable.tableArn],
            }),
            new iam.PolicyStatement({
              sid: 'ReadPrincipalEmail',
              actions: ['cognito-idp:AdminGetUser'],
              resources: [props.userPool.userPoolArn],
            }),
            new iam.PolicyStatement({
              sid: 'ReadDispatchMap',
              actions: ['ssm:GetParameter'],
              resources: [backendFunctionsParameter.parameterArn],
            }),
          ],
        }),
      },
    });

    // ---------------------------------------------------------- logging
    // An explicit log group (not logRetention, which adds a custom-resource
    // Lambda), with a metric filter and alarm on the runtime's own timeout
    // line: the only trace of a call the function could not answer.
    const logGroup = new logs.LogGroup(this, 'McpPortalHandlerLogGroup', {
      logGroupName: MCP_LOG_GROUP_NAME,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const timeoutFilter = new logs.MetricFilter(this, 'McpTimeoutMetricFilter', {
      logGroup,
      filterPattern: logs.FilterPattern.literal('"Task timed out"'),
      metricNamespace: 'DdaPortalMcp',
      metricName: 'LambdaTimeouts',
      metricValue: '1',
    });
    // No alarm action: the operator attaches the portal's notification topic.
    new cloudwatch.Alarm(this, 'McpTimeoutAlarm', {
      metric: timeoutFilter.metric({ statistic: 'sum', period: cdk.Duration.minutes(5) }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription: 'The Portal_MCP_Server Lambda reached its own timeout',
    });

    // ------------------------------------------------------------ handler
    const allowedAudiences = [canonicalUrl, ...viewUrls];
    const hostedUiOrigin =
      `https://${props.userPoolDomain.domainName}.auth.${cdk.Aws.REGION}.amazoncognito.com`;
    const handler = new lambda.Function(this, 'McpPortalHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'mcp_portal.handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      layers: [mcpSharedLayer, mcpLayer],
      memorySize: 512,
      // 29 s: under the HTTP API's 30 s integration limit. The dispatcher's
      // 24 s remaining-time guard and 2 s + 20 s invoke budget are sized
      // against it (D32).
      timeout: cdk.Duration.seconds(29),
      role,
      logGroup,
      description: 'Portal_MCP_Server (Model Context Protocol endpoint of the DDA Portal)',
      environment: {
        USECASES_TABLE: props.usecasesTable.tableName,
        USER_ROLES_TABLE: props.userRolesTable.tableName,
        AUDIT_LOG_TABLE: props.auditLogTable.tableName,
        PORTAL_REGISTRY_ENFORCED: portalRegistryEnforced(
          this.node.tryGetContext('portalRegistryEnforced'),
        ),
        COGNITO_USER_POOL_ID: props.userPool.userPoolId,
        COGNITO_REGION: region,
        COGNITO_ISSUER: `https://cognito-idp.${region}.amazonaws.com/${props.userPool.userPoolId}`,
        COGNITO_HOSTED_UI_ORIGIN: hostedUiOrigin,
        // The ApiEndpoint attribute: https://<id>.execute-api.<region>.amazonaws.com,
        // no trailing slash; the PRMs' authorization_servers[0] and the
        // facade's issuer (D27). discovery.py validates it at import.
        MCP_AS_ISSUER: httpApi.apiEndpoint,
        MCP_CANONICAL_URL: canonicalUrl,
        MCP_API_HOST: apiHost,
        MCP_ALLOWED_AUDIENCES: allowedAudiences.join(','),
        MCP_CLIENT_ID: userPoolClient.userPoolClientId,
        MCP_REQUIRED_SCOPE: requiredScope,
        MCP_MUTATING_TOOLS_ENABLED: props.mcpMutatingToolsEnabled,
        MCP_ALLOWED_ORIGINS: allowedOrigins.join(','),
        // The parameter name only; the map is never an environment variable.
        MCP_BACKEND_FUNCTIONS_PARAMETER: MCP_BACKEND_FUNCTIONS_PARAMETER_NAME,
        MCP_BUILD_SHA: props.mcpBuildSha,
        MCP_SERVER_VERSION: MCP_SERVER_VERSION,
      },
    });
    this.handler = handler;

    // ------------------------------------------------------------- routes
    // Every route uses HttpNoneAuthorizer: the Lambda authenticates (the
    // SDK's bearer middleware answers 401 with the RFC 9728 challenge), and
    // the four .well-known GET shapes are unauthenticated by RFC 9728 /
    // RFC 8414. No $default route.
    const integration = new HttpLambdaIntegration('McpIntegration', handler, {
      payloadFormatVersion: apigwv2.PayloadFormatVersion.VERSION_2_0,
    });
    const none = new apigwv2.HttpNoneAuthorizer();
    const endpointMethods = [apigwv2.HttpMethod.POST, apigwv2.HttpMethod.GET, apigwv2.HttpMethod.DELETE];
    for (const routePath of ['/mcp', '/mcp/{view}']) {
      httpApi.addRoutes({ path: routePath, methods: endpointMethods, integration, authorizer: none });
    }
    for (const routePath of [
      '/.well-known/oauth-protected-resource',
      '/.well-known/oauth-protected-resource/mcp',
      '/.well-known/oauth-protected-resource/mcp/{view}',
      '/.well-known/oauth-authorization-server',
    ]) {
      httpApi.addRoutes({ path: routePath, methods: [apigwv2.HttpMethod.GET], integration, authorizer: none });
    }

    // ------------------------------------------------------------ outputs
    new cdk.CfnOutput(this, 'McpServerUrl', {
      value: canonicalUrl,
      description: 'Portal_MCP_Server canonical endpoint (aggregate view)',
    });
    new cdk.CfnOutput(this, 'McpViewUrls', {
      value: cdk.Fn.join(',', viewUrls),
      description: 'Portal_MCP_Server view endpoints (data, models, devices, workflows, admin)',
    });
    new cdk.CfnOutput(this, 'McpUserPoolClientId', {
      value: userPoolClient.userPoolClientId,
      description: 'Cognito app client id of the MCP clients (public, code grant with PKCE)',
    });
    new cdk.CfnOutput(this, 'McpOAuthScope', {
      value: requiredScope,
      description: 'OAuth scope the MCP clients request',
    });
    new cdk.CfnOutput(this, 'McpRedirectUri', {
      value: MCP_DEFAULT_REDIRECT_URI,
      description: 'OAuth callback URL of the Kiro clients',
    });
    new cdk.CfnOutput(this, 'McpProtectedResourceMetadataUrl', {
      value: `${httpApi.apiEndpoint}/.well-known/oauth-protected-resource`,
      description: 'Protected Resource Metadata (RFC 9728), root form',
    });
    new cdk.CfnOutput(this, 'McpAuthorizationServerMetadataUrl', {
      value: `${httpApi.apiEndpoint}/.well-known/oauth-authorization-server`,
      description: 'Authorization Server Metadata facade (RFC 8414)',
    });
  }
}
