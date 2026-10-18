import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as iot from 'aws-cdk-lib/aws-iot';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as destinations from 'aws-cdk-lib/aws-lambda-destinations';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import { portalRegistryEnforced } from './context-helpers';
import * as crypto from 'crypto';
import * as path from 'path';

export interface WorkflowDebugStackProps extends cdk.StackProps {
  /** Portal tables the three functions read or write (StorageStack). */
  useCasesTable: dynamodb.Table;
  userRolesTable: dynamodb.Table;
  devicesTable: dynamodb.Table;
  deploymentsTable: dynamodb.Table;
  auditLogTable: dynamodb.Table;
  settingsTable: dynamodb.Table;
  workflowsTable: dynamodb.Table;
  workflowVersionsTable: dynamodb.Table;
  portalArtifactsBucket: s3.Bucket;
  /** Portal Cognito user pool (route authorizer). */
  userPool: cognito.IUserPool;
  /**
   * Trusted UseCase account ids: one ingest alias per account (plus the
   * Portal account) and the sts:AssumeRole targets, exactly as the other
   * stacks scope that grant. Non-empty at synth time.
   */
  trustedUseCaseAccountIds: string[];
  /** Accepted for parity with the sibling stacks; the data-plane grant is not wired here (design §9). */
  dataBucketAllowlist?: string[];
  /** Rest API id of the existing portal API (ComputeStack.api). */
  restApiId: string;
  /** Root resource id of the existing portal API. */
  restApiRootResourceId: string;
  /** Stage the portal API serves on (ApiGatewayStack deployOptions). */
  apiStageName: string;
}

/** A deployment value of design §10/§13: its context key, default and inclusive range. */
interface DeploymentValue {
  key: string;
  env: string;
  defaultValue: number;
  min: number;
  max: number;
}

/**
 * Deployment values (workflow-debug-mode design §10, §13), each read from
 * CDK context (`-c <key>=<n>`) and range-checked at synth. They reach the
 * functions as strings in `debugLambdaEnvironment`.
 */
export const DEBUG_DEPLOYMENT_VALUES: DeploymentValue[] = [
  { key: 'debugLogRetentionDays', env: 'DEBUG_LOG_RETENTION_DAYS', defaultValue: 14, min: 1, max: 90 },
  { key: 'debugMaxSessionsPerUsecase', env: 'DEBUG_MAX_SESSIONS_PER_USECASE', defaultValue: 4, min: 1, max: 20 },
  { key: 'debugMaxDevicesPerSession', env: 'DEBUG_MAX_DEVICES_PER_SESSION', defaultValue: 20, min: 1, max: 32 },
  { key: 'debugMaxMessagesPerSecond', env: 'DEBUG_MAX_MESSAGES_PER_SECOND', defaultValue: 10, min: 1, max: 20 },
  { key: 'debugMaxBytesPerSecond', env: 'DEBUG_MAX_BYTES_PER_SECOND', defaultValue: 262144, min: 32768, max: 1048576 },
  { key: 'debugIngestConcurrency', env: 'DEBUG_INGEST_CONCURRENCY', defaultValue: 20, min: 10, max: 200 },
  { key: 'debugGetLogEventsPerSecond', env: 'DEBUG_GET_LOG_EVENTS_PER_SECOND', defaultValue: 4, min: 1, max: 8 },
  { key: 'debugFilterLogEventsPerSecond', env: 'DEBUG_FILTER_LOG_EVENTS_PER_SECOND', defaultValue: 2, min: 1, max: 4 },
];

/** Resolve one deployment value from context; throw at synth when out of range. */
export function resolveDeploymentValue(spec: DeploymentValue, contextValue: unknown): number {
  if (contextValue === undefined || contextValue === null || contextValue === '') {
    return spec.defaultValue;
  }
  const value = typeof contextValue === 'number' ? contextValue : Number(String(contextValue).trim());
  if (!Number.isInteger(value) || value < spec.min || value > spec.max) {
    throw new Error(
      `${spec.key} must be an integer between ${spec.min} and ${spec.max} (got ${String(contextValue)})`,
    );
  }
  return value;
}

/** The §6.1 rule SQL: the device payload base64-encoded, the topic, the publishing identity and the delivery stamps. */
export const DEBUG_RULE_SQL =
  "SELECT encode(*, 'base64') AS p, topic() AS t, principal() AS pr, clientid() AS c, timestamp() AS r, newuuid() AS m FROM 'dda/debug/+/+/+'";

export const DEBUG_SESSIONS_TABLE_NAME = 'dda-portal-workflow-debug-sessions';
export const DEBUG_INGEST_FUNCTION_NAME = 'dda-portal-workflow-debug-ingest';
export const DEBUG_API_FUNCTION_NAME = 'dda-portal-workflow-debug-api';
export const DEBUG_MAINTENANCE_FUNCTION_NAME = 'dda-portal-workflow-debug-maintenance';
export const DEBUG_FAILURE_QUEUE_NAME = 'dda-portal-workflow-debug-failures';
export const DEBUG_RULE_ERROR_LOG_GROUP = '/dda/workflow-debug/rule-errors';
export const DEBUG_PORTAL_RULE_NAME = 'dda_dbg_ingest_portal';

/**
 * Workflow debug mode infrastructure (workflow-debug-mode design §2, §6, §7,
 * §9, §10). Self-contained like the SyntheticDataStack: it owns the sessions
 * table, the three Python 3.12 functions with their own shared, JWT and
 * workflow_core layers, the per-account ingest aliases with their async
 * invoke configuration and on-failure queue, the Portal-account IoT rule,
 * the maintenance schedule, the alarms, and registers the `/debug/*` routes
 * on the imported portal Rest API with its own Cognito authorizer, CORS
 * preflight and a route-salted CfnDeployment (RETAIN). The only shared-file
 * edit is the additive instantiation in bin/app.ts. It joins the IAM synth
 * gate with empty pre-feature baselines.
 */
export class WorkflowDebugStack extends cdk.Stack {
  public readonly sessionsTable: dynamodb.Table;
  public readonly ingestFunction: lambda.Function;
  public readonly apiFunction: lambda.Function;
  public readonly maintenanceFunction: lambda.Function;
  public readonly failureQueue: sqs.Queue;
  public readonly deploymentValues: { [env: string]: number } = {};

  constructor(scope: Construct, id: string, props: WorkflowDebugStackProps) {
    super(scope, id, props);

    // Same synth-time guard as the ComputeStack: never fall back to a
    // wildcard account for sts:AssumeRole or the alias scheme.
    if (!props.trustedUseCaseAccountIds || props.trustedUseCaseAccountIds.length === 0) {
      throw new Error(
        'WorkflowDebugStack requires a non-empty trustedUseCaseAccountIds list ' +
          '(pass -c trustedUseCaseAccountIds=<id>,<id>). Refusing to synth an ' +
          'sts:AssumeRole grant on a wildcard account.',
      );
    }

    const region = cdk.Aws.REGION;
    const account = cdk.Aws.ACCOUNT_ID;

    // ------------------------------------------------------------------
    // Deployment values (design §10, §13): range-checked at synth.
    // ------------------------------------------------------------------
    for (const spec of DEBUG_DEPLOYMENT_VALUES) {
      this.deploymentValues[spec.env] = resolveDeploymentValue(spec, this.node.tryGetContext(spec.key));
    }

    // ------------------------------------------------------------------
    // Sessions table (design §7.1): pk/sk, TTL expires_at, PITR, the
    // by-usecase and by-pair indexes.
    // ------------------------------------------------------------------
    this.sessionsTable = new dynamodb.Table(this, 'WorkflowDebugSessionsTable', {
      tableName: DEBUG_SESSIONS_TABLE_NAME,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expires_at',
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    this.sessionsTable.addGlobalSecondaryIndex({
      indexName: 'by-usecase',
      partitionKey: { name: 'gsi_usecase', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'gsi_sort', type: dynamodb.AttributeType.STRING },
    });
    this.sessionsTable.addGlobalSecondaryIndex({
      indexName: 'by-pair',
      partitionKey: { name: 'gsi_pair', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'gsi_sort', type: dynamodb.AttributeType.STRING },
    });

    // ------------------------------------------------------------------
    // Layers (design §2): own shared and JWT layers on all three functions,
    // and an own workflow_core layer on the ingest only, for
    // workflow_core.stream_url (the redaction base rule). The exclude list
    // is the ComputeStack's.
    // ------------------------------------------------------------------
    const sharedLayer = new lambda.LayerVersion(this, 'WorkflowDebugSharedLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/shared')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11, lambda.Runtime.PYTHON_3_12],
      description: 'Shared utilities for the workflow debug functions',
    });
    const jwtLayer = new lambda.LayerVersion(this, 'WorkflowDebugJwtLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/jwt')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11, lambda.Runtime.PYTHON_3_12],
      description: 'JWT dependencies for the workflow debug functions',
    });
    const workflowCoreLayer = new lambda.LayerVersion(this, 'WorkflowDebugWorkflowCoreLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/workflow_core'), {
        exclude: [
          'tests',
          'tests/**',
          '.hypothesis',
          '.hypothesis/**',
          '.pytest_cache',
          '.pytest_cache/**',
          '**/__pycache__',
          '**/__pycache__/**',
          'build.sh',
          'pyproject.toml',
          'requirements.txt',
          'README.md',
        ],
      }),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11, lambda.Runtime.PYTHON_3_12],
      description:
        'workflow_core.stream_url for the ingest redaction pass (stdlib-only module; nothing else may be imported on 3.12)',
    });

    // ------------------------------------------------------------------
    // The on-failure queue of the asynchronous ingest invocations.
    // ------------------------------------------------------------------
    this.failureQueue = new sqs.Queue(this, 'WorkflowDebugFailureQueue', {
      queueName: DEBUG_FAILURE_QUEUE_NAME,
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });

    // ------------------------------------------------------------------
    // Environment (design §2): the ComputeStack key names plus the sessions
    // table and every deployment value as a string.
    // ------------------------------------------------------------------
    const debugLambdaEnvironment: { [key: string]: string } = {
      USECASES_TABLE: props.useCasesTable.tableName,
      USER_ROLES_TABLE: props.userRolesTable.tableName,
      DEVICES_TABLE: props.devicesTable.tableName,
      DEPLOYMENTS_TABLE: props.deploymentsTable.tableName,
      AUDIT_LOG_TABLE: props.auditLogTable.tableName,
      SETTINGS_TABLE: props.settingsTable.tableName,
      WORKFLOWS_TABLE: props.workflowsTable.tableName,
      WORKFLOW_VERSIONS_TABLE: props.workflowVersionsTable.tableName,
      PORTAL_ARTIFACTS_BUCKET: props.portalArtifactsBucket.bucketName,
      PORTAL_ACCOUNT_ID: account,
      USER_POOL_ID: props.userPool.userPoolId,
      // The ComputeStack's value; jest compares them.
      COMPONENT_BUCKET_PREFIX: 'dda-component',
      PORTAL_REGISTRY_ENFORCED: portalRegistryEnforced(this.node.tryGetContext('portalRegistryEnforced')),
      WORKFLOW_DEBUG_SESSIONS_TABLE: this.sessionsTable.tableName,
      WORKFLOW_DEBUG_FAILURE_QUEUE: this.failureQueue.queueName,
      DEBUG_PORTAL_RULE_NAME,
    };
    for (const spec of DEBUG_DEPLOYMENT_VALUES) {
      debugLambdaEnvironment[spec.env] = String(this.deploymentValues[spec.env]);
    }

    // ------------------------------------------------------------------
    // Roles (design §9), one per function, each statement written out.
    // ------------------------------------------------------------------
    const sessionsTableArn = this.sessionsTable.tableArn;
    const byUsecaseIndexArn = `${sessionsTableArn}/index/by-usecase`;
    const byPairIndexArn = `${sessionsTableArn}/index/by-pair`;
    const debugLogGroupPatterns = [
      `arn:aws:logs:${region}:${account}:log-group:/dda/workflow-debug/*`,
      `arn:aws:logs:${region}:${account}:log-group:/dda/workflow-debug/*:*`,
    ];
    const assumeRoleResources = props.trustedUseCaseAccountIds.map(
      (id) => `arn:aws:iam::${id}:role/DDAPortalAccessRole`,
    );
    const resultsBucketArn = `arn:aws:s3:::dda-inference-results-${account}`;

    const newRole = (id: string, description: string) =>
      new iam.Role(this, id, {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        description,
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        ],
      });

    // Ingest role.
    const ingestRole = newRole('WorkflowDebugIngestRole', 'Execution role of the workflow debug ingest function');
    ingestRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugLogGroupWrite',
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:CreateLogStream',
        'logs:DescribeLogStreams',
        'logs:PutLogEvents',
        'logs:CreateLogGroup',
        'logs:PutRetentionPolicy',
        'logs:TagResource',
      ],
      resources: debugLogGroupPatterns,
    }));
    ingestRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugSessionsIngest',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:Query'],
      resources: [sessionsTableArn, byPairIndexArn],
    }));
    ingestRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugAttributionReads',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [props.useCasesTable.tableArn, props.devicesTable.tableArn, props.workflowsTable.tableArn],
    }));
    ingestRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugIdentityLookup',
      effect: iam.Effect.ALLOW,
      actions: ['iot:ListPrincipalThings'],
      resources: [`arn:aws:iot:${region}:${account}:cert/*`],
    }));
    ingestRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugAssumeUseCaseRole',
      effect: iam.Effect.ALLOW,
      actions: ['sts:AssumeRole'],
      resources: assumeRoleResources,
    }));

    // API role.
    const apiRole = newRole('WorkflowDebugApiRole', 'Execution role of the workflow debug API function');
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugLogGroupPrepareAndRead',
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:CreateLogGroup',
        'logs:PutRetentionPolicy',
        'logs:TagResource',
        'logs:GetLogEvents',
        'logs:FilterLogEvents',
      ],
      resources: debugLogGroupPatterns,
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugSessionsTable',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:DeleteItem',
        'dynamodb:Query',
        'dynamodb:ConditionCheckItem',
      ],
      resources: [sessionsTableArn],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugSessionsIndexes',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [byUsecaseIndexArn, byPairIndexArn],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugUseCaseAndRoleReads',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [props.useCasesTable.tableArn, props.userRolesTable.tableArn],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugDeviceRecords',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem', 'dynamodb:Scan'],
      resources: [props.devicesTable.tableArn],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugDeploymentTargets',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [`${props.deploymentsTable.tableArn}/index/usecase-deployments-index`],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugWorkflowReads',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [props.workflowsTable.tableArn],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugAuditWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:PutItem'],
      resources: [props.auditLogTable.tableArn],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugShadowControl',
      effect: iam.Effect.ALLOW,
      actions: ['iot:UpdateThingShadow', 'iot:GetThingShadow'],
      resources: [`arn:aws:iot:${region}:${account}:thing/*`],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugImageRead',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject'],
      resources: [`${resultsBucketArn}/dda-telemetry/debug/*`],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugImageList',
      effect: iam.Effect.ALLOW,
      actions: ['s3:ListBucket'],
      resources: [resultsBucketArn],
      conditions: { StringLike: { 's3:prefix': 'dda-telemetry/debug/*' } },
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugMediaRetentionRead',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetLifecycleConfiguration', 's3:GetBucketVersioning'],
      resources: [resultsBucketArn],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugAssumeUseCaseRole',
      effect: iam.Effect.ALLOW,
      actions: ['sts:AssumeRole'],
      resources: assumeRoleResources,
    }));

    // Maintenance role. The Scan on the sessions table is how the function
    // finds the live sessions: the by-usecase index needs a use case id to
    // query, and no table this role may read lists the use cases that hold
    // sessions (the table is small: sessions, counters, markers).
    const maintenanceRole = newRole(
      'WorkflowDebugMaintenanceRole',
      'Execution role of the workflow debug maintenance function',
    );
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugSessionsMaintenance',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:UpdateItem',
        'dynamodb:DeleteItem',
        'dynamodb:Query',
        'dynamodb:Scan',
        'dynamodb:ConditionCheckItem',
      ],
      resources: [sessionsTableArn],
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugSessionsIndexMaintenance',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [byUsecaseIndexArn],
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugUseCaseRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [props.useCasesTable.tableArn],
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugLogGroupRecreate',
      effect: iam.Effect.ALLOW,
      actions: ['logs:CreateLogGroup', 'logs:PutRetentionPolicy', 'logs:TagResource'],
      resources: debugLogGroupPatterns,
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugShadowClear',
      effect: iam.Effect.ALLOW,
      actions: ['iot:UpdateThingShadow'],
      resources: [`arn:aws:iot:${region}:${account}:thing/*`],
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugAuditWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:PutItem'],
      resources: [props.auditLogTable.tableArn],
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DebugAssumeUseCaseRole',
      effect: iam.Effect.ALLOW,
      actions: ['sts:AssumeRole'],
      resources: assumeRoleResources,
    }));

    // ------------------------------------------------------------------
    // The three functions (Python 3.12, the functions directory as the code
    // asset, fixed names so no environment refers to another's ARN).
    // ------------------------------------------------------------------
    const codeAsset = lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions'));

    this.ingestFunction = new lambda.Function(this, 'WorkflowDebugIngest', {
      functionName: DEBUG_INGEST_FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'workflow_debug_ingest.handler',
      code: codeAsset,
      role: ingestRole,
      environment: debugLambdaEnvironment,
      layers: [sharedLayer, jwtLayer, workflowCoreLayer],
      memorySize: 256,
      timeout: cdk.Duration.seconds(20),
      reservedConcurrentExecutions: this.deploymentValues.DEBUG_INGEST_CONCURRENCY,
      description: 'Workflow debug ingest: the Lambda action target of the dda_dbg_ingest rules',
    });

    this.apiFunction = new lambda.Function(this, 'WorkflowDebugApi', {
      functionName: DEBUG_API_FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'workflow_debug_api.handler',
      code: codeAsset,
      role: apiRole,
      environment: debugLambdaEnvironment,
      layers: [sharedLayer, jwtLayer],
      memorySize: 512,
      timeout: cdk.Duration.seconds(29),
      description: 'Workflow debug API: the /debug/* routes',
    });

    this.maintenanceFunction = new lambda.Function(this, 'WorkflowDebugMaintenance', {
      functionName: DEBUG_MAINTENANCE_FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'workflow_debug_maintenance.handler',
      code: codeAsset,
      role: maintenanceRole,
      environment: debugLambdaEnvironment,
      layers: [sharedLayer, jwtLayer],
      memorySize: 256,
      timeout: cdk.Duration.minutes(4),
      description: 'Workflow debug maintenance: session expiry, shadow retries, log group recreation (every 5 minutes)',
    });

    // ------------------------------------------------------------------
    // Ingest aliases (design §6.1): one per trusted account and the Portal
    // account, each with the resource-based statement for iot.amazonaws.com
    // conditioned on that account's dda_dbg_* rules, and the asynchronous
    // invocation settings (Lambda stores them per qualifier, and the rules
    // invoke the aliases).
    // ------------------------------------------------------------------
    const aliasAccounts: Array<{ id: string; accountId: string }> = props.trustedUseCaseAccountIds.map(
      (accountId) => ({ id: `IngestAlias${accountId}`, accountId }),
    );
    const portalAccountResolved = !cdk.Token.isUnresolved(this.account) ? this.account : undefined;
    if (portalAccountResolved === undefined || !props.trustedUseCaseAccountIds.includes(portalAccountResolved)) {
      aliasAccounts.push({ id: 'IngestAliasPortal', accountId: account });
    }
    let portalAlias: lambda.Alias | undefined;
    for (const entry of aliasAccounts) {
      const alias = new lambda.Alias(this, entry.id, {
        aliasName: `acct-${entry.accountId}`,
        version: this.ingestFunction.currentVersion,
      });
      alias.addPermission('IotRuleInvoke', {
        principal: new iam.ServicePrincipal('iot.amazonaws.com'),
        action: 'lambda:InvokeFunction',
        sourceAccount: entry.accountId,
        sourceArn: `arn:aws:iot:${region}:${entry.accountId}:rule/dda_dbg_*`,
      });
      alias.configureAsyncInvoke({
        retryAttempts: 2,
        maxEventAge: cdk.Duration.hours(1),
        onFailure: new destinations.SqsDestination(this.failureQueue),
      });
      if (entry.id === 'IngestAliasPortal' || entry.accountId === portalAccountResolved) {
        portalAlias = alias;
      }
    }
    if (portalAlias === undefined) {
      // Unreachable: the Portal account is always in the list above.
      throw new Error('WorkflowDebugStack: no ingest alias for the Portal account');
    }

    // ------------------------------------------------------------------
    // The Portal-account rule (design §6.1): a role-less Lambda action on
    // the Portal alias and a CloudWatch Logs error action through its own
    // error role on /dda/workflow-debug/rule-errors (14 days).
    // ------------------------------------------------------------------
    const ruleErrorLogGroup = new logs.LogGroup(this, 'WorkflowDebugRuleErrorLogGroup', {
      logGroupName: DEBUG_RULE_ERROR_LOG_GROUP,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const ruleErrorRole = new iam.Role(this, 'WorkflowDebugRuleErrorRole', {
      description: 'Error-action role of the dda_dbg_ingest_portal rule (writes rule errors to CloudWatch Logs)',
      assumedBy: new iam.ServicePrincipal('iot.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': account },
          ArnLike: { 'aws:SourceArn': `arn:aws:iot:${region}:${account}:rule/dda_dbg_*` },
        },
      }),
    });
    ruleErrorRole.addToPolicy(new iam.PolicyStatement({
      sid: 'WriteDebugRuleErrors',
      effect: iam.Effect.ALLOW,
      actions: ['logs:CreateLogStream', 'logs:DescribeLogStreams', 'logs:PutLogEvents'],
      resources: [ruleErrorLogGroup.logGroupArn],
    }));

    new iot.CfnTopicRule(this, 'WorkflowDebugIngestRule', {
      ruleName: DEBUG_PORTAL_RULE_NAME,
      topicRulePayload: {
        sql: DEBUG_RULE_SQL,
        awsIotSqlVersion: '2016-03-23',
        ruleDisabled: false,
        description: 'Deliver dda/debug records of the Portal-account use cases to the workflow debug ingest alias',
        actions: [
          {
            lambda: { functionArn: portalAlias.functionArn },
          },
        ],
        errorAction: {
          cloudwatchLogs: {
            logGroupName: ruleErrorLogGroup.logGroupName,
            roleArn: ruleErrorRole.roleArn,
          },
        },
      },
    });

    // ------------------------------------------------------------------
    // Maintenance schedule (design §7.4): EventBridge Scheduler every
    // 5 minutes through a role that may invoke the maintenance function only.
    // ------------------------------------------------------------------
    const scheduleRole = new iam.Role(this, 'WorkflowDebugScheduleRole', {
      description: 'Lets EventBridge Scheduler invoke the workflow debug maintenance function',
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com', {
        conditions: { StringEquals: { 'aws:SourceAccount': account } },
      }),
    });
    scheduleRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeDebugMaintenance',
      effect: iam.Effect.ALLOW,
      actions: ['lambda:InvokeFunction'],
      resources: [this.maintenanceFunction.functionArn],
    }));
    new scheduler.CfnSchedule(this, 'WorkflowDebugMaintenanceSchedule', {
      name: 'dda-portal-workflow-debug-maintenance',
      description: 'Workflow debug maintenance every 5 minutes',
      scheduleExpression: 'rate(5 minutes)',
      flexibleTimeWindow: { mode: 'OFF' },
      state: 'ENABLED',
      target: {
        arn: this.maintenanceFunction.functionArn,
        roleArn: scheduleRole.roleArn,
        retryPolicy: { maximumRetryAttempts: 0, maximumEventAgeInSeconds: 60 },
      },
    });

    // ------------------------------------------------------------------
    // Alarms (design §6.2 step 8): terminal identity denials, and messages
    // in the on-failure queue.
    // ------------------------------------------------------------------
    new cloudwatch.Alarm(this, 'WorkflowDebugIdentityUnavailableAlarm', {
      alarmName: 'dda-portal-workflow-debug-identity-unavailable',
      alarmDescription: 'The workflow debug ingest could not resolve device identities (AccessDenied on ListPrincipalThings)',
      metric: new cloudwatch.Metric({
        namespace: 'DDA/WorkflowDebug',
        metricName: 'IdentityUnavailable',
        statistic: 'Sum',
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    new cloudwatch.Alarm(this, 'WorkflowDebugFailureQueueAlarm', {
      alarmName: 'dda-portal-workflow-debug-failures-queued',
      alarmDescription: 'Workflow debug messages landed in the on-failure queue (ingest invocations exhausted their retries)',
      metric: this.failureQueue.metricApproximateNumberOfMessagesVisible({
        statistic: 'Maximum',
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ------------------------------------------------------------------
    // API routes on the shared portal API (the SyntheticDataStack pattern).
    // ------------------------------------------------------------------
    const api = apigateway.RestApi.fromRestApiAttributes(this, 'PortalApi', {
      restApiId: props.restApiId,
      rootResourceId: props.restApiRootResourceId,
    });

    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'WorkflowDebugAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'EdgeCVPortalWorkflowDebugAuthorizer',
      identitySource: 'method.request.header.Authorization',
    });

    const corsOptions: apigateway.CorsOptions = {
      allowOrigins: apigateway.Cors.ALL_ORIGINS,
      allowMethods: apigateway.Cors.ALL_METHODS,
      allowHeaders: [
        'Content-Type',
        'X-Amz-Date',
        'Authorization',
        'X-Api-Key',
        'X-Amz-Security-Token',
      ],
    };

    const integration = new apigateway.LambdaIntegration(this.apiFunction, {
      allowTestInvoke: false,
    });

    const methods: apigateway.Method[] = [];
    const addMethod = (resource: apigateway.IResource, httpMethod: string) => {
      methods.push(
        resource.addMethod(httpMethod, integration, {
          authorizer,
          authorizationType: apigateway.AuthorizationType.COGNITO,
        }),
      );
    };

    // Route matrix (design §7.2):
    //   POST   /debug/usecases/{usecase_id}/sessions
    //   GET    /debug/usecases/{usecase_id}/sessions
    //   GET    /debug/usecases/{usecase_id}/log
    //   GET    /debug/sessions/{session_id}
    //   DELETE /debug/sessions/{session_id}
    //   GET    /debug/sessions/{session_id}/images/{execution_id}
    //   GET    /debug/sessions/{session_id}/console-link
    const debugResource = api.root.addResource('debug', {
      defaultCorsPreflightOptions: corsOptions,
    });
    const usecaseResource = debugResource.addResource('usecases').addResource('{usecase_id}');
    const usecaseSessions = usecaseResource.addResource('sessions');
    addMethod(usecaseSessions, 'POST');
    addMethod(usecaseSessions, 'GET');
    addMethod(usecaseResource.addResource('log'), 'GET');
    const sessionResource = debugResource.addResource('sessions').addResource('{session_id}');
    addMethod(sessionResource, 'GET');
    addMethod(sessionResource, 'DELETE');
    addMethod(sessionResource.addResource('images').addResource('{execution_id}'), 'GET');
    addMethod(sessionResource.addResource('console-link'), 'GET');

    // Deployment re-pointing the existing stage so the routes above go
    // live; the logical id is salted with the route table (the
    // SyntheticDataStack pattern).
    const routeSalt = crypto
      .createHash('sha256')
      .update(
        methods
          .map((m) => `${m.httpMethod} ${m.resource.path}`)
          .sort()
          .join('\n'),
      )
      .digest('hex')
      .slice(0, 16);

    const deployment = new apigateway.CfnDeployment(this, 'WorkflowDebugDeployment', {
      restApiId: props.restApiId,
      stageName: props.apiStageName,
      description: 'Workflow debug routes deployment (workflow-debug-mode)',
    });
    deployment.overrideLogicalId(`WorkflowDebugDeployment${routeSalt}`);
    deployment.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    deployment.node.addDependency(authorizer);
    deployment.node.addDependency(debugResource);

    new cdk.CfnOutput(this, 'WorkflowDebugSessionsTableName', {
      value: this.sessionsTable.tableName,
      description: 'Workflow debug sessions table',
    });
    new cdk.CfnOutput(this, 'WorkflowDebugFailureQueueArn', {
      value: this.failureQueue.queueArn,
      description: 'On-failure queue of the workflow debug ingest aliases',
    });
  }
}
