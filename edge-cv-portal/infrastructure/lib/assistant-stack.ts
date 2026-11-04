/**
 * EdgeCVPortalAssistantStack: the Portal assistant (spec
 * .kiro/specs/portal-ai-assistant/, design §10, D2, D7, D8).
 *
 * One Regional REST API (`dda-portal-assistant`, stage v1) with its own
 * Cognito User Pools authorizer, two Python Lambdas (the API handler and the
 * asynchronous Turn worker), one DynamoDB table under a dedicated customer
 * managed key, an SSM parameter with the backend dispatch map (the same
 * handler ARNs the MCP server dispatches to), three layers (the shared
 * layer, the MCP SDK layer for jsonschema at the MCP server's pins, and the
 * user guide), and alarms without actions.
 *
 * The stack imports ARNs only and exports nothing. Removal is two steps:
 * `cdk destroy EdgeCVPortalAssistantStack`, then
 * `DEPLOY_ASSISTANT=false ./deploy-infrastructure.sh`
 * (scripts/portal-assistant-stack.sh guards the order). The table and the
 * key are retained on destroy.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { portalRegistryEnforced } from './context-helpers';
import { MCP_LAYER_SOURCE_DIR, mcpLayerBundling } from './mcp-layer';
import { MCP_BACKEND_KEYS, McpBackendKey } from './mcp-stack';

export const ASSISTANT_API_NAME = 'dda-portal-assistant';
export const ASSISTANT_STAGE_NAME = 'v1';
export const ASSISTANT_TABLE_NAME = 'dda-portal-assistant';
export const ASSISTANT_KEY_ALIAS = 'alias/dda-portal/assistant';
export const ASSISTANT_BACKEND_FUNCTIONS_PARAMETER_NAME = '/dda-portal/assistant/backend-functions';
export const ASSISTANT_API_LOG_GROUP_NAME = '/dda-portal/assistant-api';
export const ASSISTANT_WORKER_LOG_GROUP_NAME = '/dda-portal/assistant-worker';
export const ASSISTANT_METRIC_NAMESPACE = 'DDA/Assistant';
/** assistant.__version__ */
export const ASSISTANT_VERSION = '1.1.0';
export const GUIDE_SOURCE_DIR = path.join(__dirname, '../../../docs/user-guide');
/** The user guide files the guide layer ships: chapters and the index only. */
export const GUIDE_FILE_PATTERN = /^(README|[0-9]{2}-[a-z0-9-]+)\.md$/;

/** The 11 methods of the assistant API (design §6), as `METHOD /path`. */
export const ASSISTANT_ROUTES: ReadonlyArray<string> = [
  'GET /assistant/state',
  'GET /assistant/settings',
  'PUT /assistant/settings',
  'GET /assistant/conversations',
  'POST /assistant/conversations',
  'GET /assistant/conversations/{cid}',
  'DELETE /assistant/conversations/{cid}',
  'POST /assistant/conversations/{cid}/turns',
  'GET /assistant/conversations/{cid}/turns/{tid}',
  'POST /assistant/conversations/{cid}/turns/{tid}/confirmation',
  'POST /assistant/conversations/{cid}/turns/{tid}/stop',
];

export interface AssistantStackProps extends cdk.StackProps {
  readonly userPool: cognito.IUserPool;
  readonly userRolesTable: dynamodb.ITable;
  readonly auditLogTable: dynamodb.ITable;
  readonly usecasesTable: dynamodb.ITable;
  readonly settingsTable: dynamodb.ITable;
  readonly backendHandlers: Record<McpBackendKey, lambda.IFunction>;
  /** Shown to the model as the support contact (Requirement 4.4); optional. */
  readonly supportEmail?: string;
  /** Shown to the model as the documentation link (Requirement 4.4); optional. */
  readonly documentationUrl?: string;
}

/**
 * Bundling of the guide layer: copy the chapter files under `guide/` so they
 * mount at /opt/guide/*.md. Local first (plain file copies, no tool needed),
 * the Docker image as the fallback.
 */
export function guideLayerBundling(sourceDir: string = GUIDE_SOURCE_DIR): cdk.BundlingOptions {
  return {
    image: lambda.Runtime.PYTHON_3_11.bundlingImage,
    command: [
      'bash', '-c',
      `mkdir -p ${cdk.AssetStaging.BUNDLING_OUTPUT_DIR}/guide && ` +
        `cp ${cdk.AssetStaging.BUNDLING_INPUT_DIR}/*.md ${cdk.AssetStaging.BUNDLING_OUTPUT_DIR}/guide/`,
    ],
    local: {
      tryBundle(outputDir: string): boolean {
        const target = path.join(outputDir, 'guide');
        fs.mkdirSync(target, { recursive: true });
        const names = fs.readdirSync(sourceDir).filter((name) => GUIDE_FILE_PATTERN.test(name));
        if (names.length === 0) {
          throw new Error(`AssistantGuideLayer: no guide chapters found in ${sourceDir}`);
        }
        for (const name of names) {
          fs.copyFileSync(path.join(sourceDir, name), path.join(target, name));
        }
        return true;
      },
    },
  };
}

export class AssistantStack extends cdk.Stack {
  public readonly api: apigateway.RestApi;
  public readonly apiHandler: lambda.Function;
  public readonly worker: lambda.Function;
  public readonly table: dynamodb.Table;
  public readonly key: kms.Key;
  public readonly apiUrl: string;

  constructor(scope: Construct, id: string, props: AssistantStackProps) {
    super(scope, id, props);
    const missing = MCP_BACKEND_KEYS.filter((key) => !props.backendHandlers[key]);
    if (missing.length > 0) {
      throw new Error(`AssistantStack backendHandlers is missing ${missing.join(', ')}`);
    }
    const registryEnforced = portalRegistryEnforced(this.node.tryGetContext('portalRegistryEnforced'));

    // ------------------------------------------------------------ storage
    // A dedicated key: Conversation content (prompts, tool results) is a
    // different data class from the account tables (design D7).
    this.key = new kms.Key(this, 'AssistantKey', {
      alias: ASSISTANT_KEY_ALIAS,
      description: 'Portal assistant conversations table',
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    this.table = new dynamodb.Table(this, 'AssistantTable', {
      tableName: ASSISTANT_TABLE_NAME,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.key,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ------------------------------------------------------ dispatch map
    // The assistant's own copy of the MCP dispatch map (mcp-servers D30), so
    // the assistant works with deployMcpServer=false.
    const backendFunctionsParameter = new ssm.StringParameter(this, 'AssistantBackendFunctions', {
      parameterName: ASSISTANT_BACKEND_FUNCTIONS_PARAMETER_NAME,
      tier: ssm.ParameterTier.ADVANCED,
      description: 'Assistant backend key -> handler function ARN (read once per Lambda container)',
      stringValue: cdk.Stack.of(this).toJsonString(
        Object.fromEntries(
          MCP_BACKEND_KEYS.map((key) => [key, props.backendHandlers[key].functionArn]),
        ),
      ),
    });

    // ------------------------------------------------------------- layers
    const sharedLayer = new lambda.LayerVersion(this, 'AssistantSharedLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/shared')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'shared_utils for the assistant Lambdas (own copy, D31 pattern)',
    });
    const depsLayer = new lambda.LayerVersion(this, 'AssistantDepsLayer', {
      code: lambda.Code.fromAsset(MCP_LAYER_SOURCE_DIR, {
        bundling: mcpLayerBundling(),
        exclude: ['python'],
      }),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'jsonschema at the MCP server pins (backend/layers/mcp lock) for the assistant',
    });
    const guideLayer = new lambda.LayerVersion(this, 'AssistantGuideLayer', {
      code: lambda.Code.fromAsset(GUIDE_SOURCE_DIR, {
        bundling: guideLayerBundling(),
        exclude: ['images', 'images/**', '*.pdf', 'build-pdf.*', 'package*.json', 'node_modules', 'node_modules/**'],
      }),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'docs/user-guide chapters at /opt/guide (the assistant grounding, design D4)',
    });

    // -------------------------------------------------------------- roles
    const tableAccess = new iam.PolicyStatement({
      sid: 'AssistantTable',
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem',
        'dynamodb:Query', 'dynamodb:BatchWriteItem'],
      resources: [this.table.tableArn],
    });
    const keyAccess = new iam.PolicyStatement({
      sid: 'AssistantKey',
      actions: ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:DescribeKey'],
      resources: [this.key.keyArn],
    });
    const readSettings = new iam.PolicyStatement({
      sid: 'ReadSettings',
      actions: ['dynamodb:GetItem'],
      resources: [props.settingsTable.tableArn],
    });
    const writeSettings = new iam.PolicyStatement({
      sid: 'WriteSettings',
      actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem'],
      resources: [props.settingsTable.tableArn],
    });
    const readRegistry = new iam.PolicyStatement({
      sid: 'ReadPortalIdentityRegistry',
      actions: ['dynamodb:GetItem', 'dynamodb:Query'],
      resources: [props.userRolesTable.tableArn],
    });
    const scanUsecases = new iam.PolicyStatement({
      sid: 'ListUseCasesForWhoami',
      actions: ['dynamodb:Scan'],
      resources: [props.usecasesTable.tableArn],
    });
    const writeAudit = new iam.PolicyStatement({
      sid: 'WriteAuditLog',
      actions: ['dynamodb:PutItem'],
      resources: [props.auditLogTable.tableArn],
    });
    const basicExecution = iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole');

    const workerRole = new iam.Role(this, 'AssistantWorkerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Portal assistant Turn worker',
      managedPolicies: [basicExecution],
      inlinePolicies: {
        AssistantWorkerPolicy: new iam.PolicyDocument({
          statements: [
            tableAccess, keyAccess, readSettings, writeSettings, readRegistry, scanUsecases, writeAudit,
            new iam.PolicyStatement({
              sid: 'InvokeBackingHandlers',
              actions: ['lambda:InvokeFunction'],
              resources: MCP_BACKEND_KEYS.map((key) => props.backendHandlers[key].functionArn),
            }),
            new iam.PolicyStatement({
              sid: 'ReadDispatchMap',
              actions: ['ssm:GetParameter'],
              resources: [backendFunctionsParameter.parameterArn],
            }),
            // The same resource scope as the Portal's other Bedrock-backed
            // handlers (compute-stack.ts): the model id is runtime
            // configuration (Bedrock_Configuration), not a fixed ARN.
            new iam.PolicyStatement({
              sid: 'InvokeConfiguredModel',
              actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
              resources: [
                'arn:aws:bedrock:*::foundation-model/*',
                `arn:aws:bedrock:*:${cdk.Aws.ACCOUNT_ID}:inference-profile/*`,
              ],
            }),
            new iam.PolicyStatement({
              sid: 'ApplyConfiguredGuardrail',
              actions: ['bedrock:ApplyGuardrail'],
              resources: [`arn:aws:bedrock:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:guardrail/*`],
            }),
          ],
        }),
      },
    });

    // --------------------------------------------------------- log groups
    const workerLogGroup = new logs.LogGroup(this, 'AssistantWorkerLogGroup', {
      logGroupName: ASSISTANT_WORKER_LOG_GROUP_NAME,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const apiLogGroup = new logs.LogGroup(this, 'AssistantApiLogGroup', {
      logGroupName: ASSISTANT_API_LOG_GROUP_NAME,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ------------------------------------------------------------ worker
    const commonEnvironment: Record<string, string> = {
      ASSISTANT_TABLE: this.table.tableName,
      SETTINGS_TABLE: props.settingsTable.tableName,
      USER_ROLES_TABLE: props.userRolesTable.tableName,
      USECASES_TABLE: props.usecasesTable.tableName,
      AUDIT_LOG_TABLE: props.auditLogTable.tableName,
      PORTAL_REGISTRY_ENFORCED: registryEnforced,
      ASSISTANT_VERSION: ASSISTANT_VERSION,
      COGNITO_USER_POOL_ID: props.userPool.userPoolId,
    };
    this.worker = new lambda.Function(this, 'AssistantWorker', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'assistant.worker.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      layers: [sharedLayer, depsLayer, guideLayer],
      memorySize: 1024,
      // The Turn budget is 240 s of wall clock (design D8); the worker keeps
      // a 20 s reserve before the Lambda timeout.
      timeout: cdk.Duration.seconds(300),
      reservedConcurrentExecutions: 10,
      role: workerRole,
      logGroup: workerLogGroup,
      description: 'Portal assistant Turn worker (agent loop over the MCP tool pipeline)',
      environment: {
        ...commonEnvironment,
        // dispatch.py reads this variable name (shared with the MCP server).
        MCP_BACKEND_FUNCTIONS_PARAMETER: ASSISTANT_BACKEND_FUNCTIONS_PARAMETER_NAME,
        ASSISTANT_GUIDE_DIR: '/opt/guide',
        ASSISTANT_CLIENT_LABEL_PREFIX: 'dda-portal-frontend',
        ...(props.supportEmail ? { ASSISTANT_SUPPORT_EMAIL: props.supportEmail } : {}),
        ...(props.documentationUrl ? { ASSISTANT_DOCUMENTATION_URL: props.documentationUrl } : {}),
      },
    });
    // Asynchronous invocation without retries: the Turn record is the source
    // of truth and the API reaps a lost worker (design §8.4).
    new lambda.EventInvokeConfig(this, 'AssistantWorkerInvokeConfig', {
      function: this.worker,
      retryAttempts: 0,
      maxEventAge: cdk.Duration.minutes(2),
    });

    // --------------------------------------------------------------- api
    const apiRole = new iam.Role(this, 'AssistantApiRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Portal assistant API handler',
      managedPolicies: [basicExecution],
      inlinePolicies: {
        AssistantApiPolicy: new iam.PolicyDocument({
          statements: [
            tableAccess, keyAccess, readSettings, writeSettings, readRegistry, writeAudit,
            new iam.PolicyStatement({
              sid: 'InvokeWorker',
              actions: ['lambda:InvokeFunction'],
              resources: [this.worker.functionArn],
            }),
          ],
        }),
      },
    });
    this.apiHandler = new lambda.Function(this, 'AssistantApiHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'assistant.api.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      layers: [sharedLayer, depsLayer],
      memorySize: 256,
      timeout: cdk.Duration.seconds(29),
      role: apiRole,
      logGroup: apiLogGroup,
      description: 'Portal assistant API (conversations, turns, confirmations, settings)',
      environment: {
        ...commonEnvironment,
        ASSISTANT_WORKER_FUNCTION: this.worker.functionName,
        ASSISTANT_CLIENT_LABEL_PREFIX: 'dda-portal-frontend',
      },
    });

    // Regional: a later server-sent-events transport needs the 5-minute
    // stream idle timeout the edge-optimized main API lacks (design D2).
    this.api = new apigateway.RestApi(this, 'AssistantApi', {
      restApiName: ASSISTANT_API_NAME,
      description: 'Portal assistant API',
      endpointTypes: [apigateway.EndpointType.REGIONAL],
      deployOptions: {
        stageName: ASSISTANT_STAGE_NAME,
        metricsEnabled: true,
        throttlingRateLimit: 20,
        throttlingBurstLimit: 40,
        methodOptions: {
          // the poll route
          '/assistant/conversations/{cid}/turns/{tid}/GET': {
            throttlingRateLimit: 10,
            throttlingBurstLimit: 20,
          },
        },
      },
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: ['Content-Type', 'X-Amz-Date', 'Authorization', 'X-Api-Key',
          'X-Amz-Security-Token', 'X-Portal-Build'],
      },
    });
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'AssistantAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'AssistantAuthorizer',
      identitySource: 'method.request.header.Authorization',
    });
    const integration = new apigateway.LambdaIntegration(this.apiHandler, { proxy: true });
    const methodOptions: apigateway.MethodOptions = {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    };
    const assistant = this.api.root.addResource('assistant');
    const state = assistant.addResource('state');
    const settings = assistant.addResource('settings');
    const conversations = assistant.addResource('conversations');
    const conversation = conversations.addResource('{cid}');
    const turns = conversation.addResource('turns');
    const turn = turns.addResource('{tid}');
    const confirmation = turn.addResource('confirmation');
    const stop = turn.addResource('stop');
    const methods: Array<[apigateway.Resource, string]> = [
      [state, 'GET'], [settings, 'GET'], [settings, 'PUT'],
      [conversations, 'GET'], [conversations, 'POST'],
      [conversation, 'GET'], [conversation, 'DELETE'],
      [turns, 'POST'], [turn, 'GET'], [confirmation, 'POST'], [stop, 'POST'],
    ];
    for (const [resource, method] of methods) {
      resource.addMethod(method, integration, methodOptions);
    }
    this.apiUrl = this.api.url.replace(/\/$/, '');

    // ------------------------------------------------------------- alarms
    // No alarm actions: the operator attaches the portal's notification topic
    // (ADMIN_GUIDE "Portal assistant"), as for the MCP timeout alarm.
    const metric = (name: string, statistic = 'sum') =>
      new cloudwatch.Metric({
        namespace: ASSISTANT_METRIC_NAMESPACE, metricName: name, statistic,
        period: cdk.Duration.minutes(5),
      });
    new cloudwatch.Alarm(this, 'AssistantModelErrorsAlarm', {
      metric: metric('ModelErrors'),
      threshold: 5, evaluationPeriods: 3,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription: 'Portal assistant: sustained Bedrock model errors (5 or more in 15 minutes)',
    });
    new cloudwatch.Alarm(this, 'AssistantThrottledAlarm', {
      metric: metric('BedrockThrottled'),
      threshold: 5, evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription: 'Portal assistant: Bedrock throttling (5 or more Turns in 5 minutes)',
    });
    new cloudwatch.Alarm(this, 'AssistantWorkerErrorsAlarm', {
      metric: this.worker.metricErrors({ period: cdk.Duration.minutes(5), statistic: 'sum' }),
      threshold: 1, evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription: 'Portal assistant: the Turn worker failed (a Turn may be left for the lazy reap)',
    });
    const workerLostFilter = new logs.MetricFilter(this, 'AssistantWorkerLostFilter', {
      logGroup: apiLogGroup,
      filterPattern: logs.FilterPattern.literal('"worker_lost"'),
      metricNamespace: ASSISTANT_METRIC_NAMESPACE,
      metricName: 'WorkerLost',
      metricValue: '1',
    });
    new cloudwatch.Alarm(this, 'AssistantWorkerLostAlarm', {
      metric: workerLostFilter.metric({ statistic: 'sum', period: cdk.Duration.minutes(5) }),
      threshold: 1, evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription: 'Portal assistant: a Turn was reaped because its worker did not finish',
    });

    // ------------------------------------------------------------ outputs
    new cdk.CfnOutput(this, 'AssistantApiUrl', {
      value: this.apiUrl,
      description: 'Portal assistant API base URL (frontend config.json assistantApiUrl)',
    });
    new cdk.CfnOutput(this, 'AssistantTableName', {
      value: this.table.tableName,
      description: 'Portal assistant conversations table',
    });
    new cdk.CfnOutput(this, 'AssistantWorkerFunctionName', {
      value: this.worker.functionName,
      description: 'Portal assistant Turn worker',
    });
  }
}
