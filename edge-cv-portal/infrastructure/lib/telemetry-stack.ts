import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as iot from 'aws-cdk-lib/aws-iot';
import * as firehose from 'aws-cdk-lib/aws-kinesisfirehose';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as destinations from 'aws-cdk-lib/aws-lambda-destinations';
import * as eventsources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import * as contextHelpers from './context-helpers';
import { portalRegistryEnforced, sharedAuthorizerId } from './context-helpers';
import * as crypto from 'crypto';
import * as path from 'path';

/**
 * The shape of the data-plane grant helper the authorization specification
 * adds to lib/context-helpers (workflow-telemetry-dashboards design §12.3,
 * N15). While `lib/context-helpers` does not export `dataPlaneGrant`, the
 * stack grants nothing on use-case buckets beyond the by-ARN statements on
 * the default Results_Bucket and sets TELEMETRY_DATA_PLANE_GRANT=absent.
 */
export interface DataPlaneGrant {
  /** Object-level resources (`arn:aws:s3:::<bucket>/*`) the tag condition applies to. */
  objectLevel: string[];
  /** Bucket-level resources (`arn:aws:s3:::<bucket>`) the tag condition applies to. */
  bucketLevel: string[];
  /** Object resources under a key prefix (`arn:aws:s3:::<bucket>/<prefix>/*`). */
  objectPrefix(prefix: string): string[];
  /** The ABAC condition every statement carries (the `dda-portal:managed` tag). */
  condition: { [operator: string]: { [key: string]: string } };
}

export interface TelemetryStackProps extends cdk.StackProps {
  /** Portal tables the four functions read or write (StorageStack). */
  useCasesTable: dynamodb.Table;
  userRolesTable: dynamodb.Table;
  devicesTable: dynamodb.Table;
  auditLogTable: dynamodb.Table;
  settingsTable: dynamodb.Table;
  workflowsTable: dynamodb.Table;
  workflowVersionsTable: dynamodb.Table;
  portalArtifactsBucket: s3.Bucket;
  /** Portal Cognito user pool (USER_POOL_ID in the function environment). */
  userPool: cognito.IUserPool;
  /**
   * Trusted UseCase account ids: one ingest alias per account (plus the
   * Portal account) and the sts:AssumeRole targets. Non-empty at synth time.
   */
  trustedUseCaseAccountIds: string[];
  /** The data-bucket allowlist the SyntheticDataStack takes (N15); used only once dataPlaneGrant exists. */
  dataBucketAllowlist?: string[];
  /** Rest API id of the existing portal API (ComputeStack.api). */
  restApiId: string;
  /** Root resource id of the existing portal API. */
  restApiRootResourceId: string;
  /** Stage the portal API serves on (ApiGatewayStack deployOptions). */
  apiStageName: string;
  /**
   * Id of the portal API's existing Cognito authorizer every `/telemetry/*`
   * method attaches to (the CDK context value `sharedAuthorizerId`, which
   * bin/app.ts passes through and deploy-infrastructure.sh resolves from the
   * deployed API). Required and validated at synth: the stack creates no
   * authorizer of its own.
   */
  sharedAuthorizerId: string;
  /** Test seam: the data-plane grant to wire instead of the lib/context-helpers export. */
  dataPlaneGrantOverride?: ((allowlist: string[]) => DataPlaneGrant) | null;
}

/** A deployment value of design §15.2: its context key, environment name, default and inclusive range. */
export interface TelemetryDeploymentValue {
  key: string;
  env: string;
  defaultValue: number;
  min: number;
  max: number;
}

/**
 * Deployment values (workflow-telemetry-dashboards design §15.2), each read
 * from CDK context (`-c <key>=<n>`) and range-checked at synth. They reach the
 * functions as strings in `telemetryLambdaEnvironment`; the frontend reads
 * telemetryNowPollSeconds and the cadence table from GET /telemetry/schemas.
 */
export const TELEMETRY_DEPLOYMENT_VALUES: TelemetryDeploymentValue[] = [
  { key: 'telemetryNowPollSeconds', env: 'TELEMETRY_NOW_POLL_SECONDS', defaultValue: 5, min: 5, max: 30 },
  { key: 'telemetryLiveMaxLeases', env: 'TELEMETRY_LIVE_MAX_LEASES', defaultValue: 5, min: 1, max: 20 },
  { key: 'telemetryLiveMaxViewMinutes', env: 'TELEMETRY_LIVE_MAX_VIEW_MINUTES', defaultValue: 30, min: 5, max: 240 },
  { key: 'telemetryClipRetentionDays', env: 'TELEMETRY_CLIP_RETENTION_DAYS', defaultValue: 90, min: 7, max: 365 },
  { key: 'telemetryLakeRetentionDays', env: 'TELEMETRY_LAKE_RETENTION_DAYS', defaultValue: 400, min: 30, max: 3650 },
  { key: 'telemetryIngestConcurrency', env: 'TELEMETRY_INGEST_CONCURRENCY', defaultValue: 50, min: 10, max: 500 },
];

/** Resolve one deployment value from context; throw at synth when out of range. */
export function resolveTelemetryValue(spec: TelemetryDeploymentValue, contextValue: unknown): number {
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
export const TELEMETRY_RULE_SQL =
  "SELECT encode(*, 'base64') AS p, topic() AS t, principal() AS pr, clientid() AS c, timestamp() AS r, newuuid() AS m FROM 'dda/telemetry/+/+/+'";

export const TELEMETRY_SUMMARIES_TABLE_NAME = 'dda-portal-telemetry-summaries';
export const TELEMETRY_EVENTS_TABLE_NAME = 'dda-portal-telemetry-events';
export const TELEMETRY_AGGREGATES_TABLE_NAME = 'dda-portal-telemetry-aggregates';
export const TELEMETRY_CONFIG_TABLE_NAME = 'dda-portal-telemetry-config';
export const TELEMETRY_INGEST_FUNCTION_NAME = 'dda-portal-telemetry-ingest';
export const TELEMETRY_ROLLUP_FUNCTION_NAME = 'dda-portal-telemetry-rollup';
export const TELEMETRY_API_FUNCTION_NAME = 'dda-portal-telemetry-api';
export const TELEMETRY_MAINTENANCE_FUNCTION_NAME = 'dda-portal-telemetry-maintenance';
export const TELEMETRY_FAILURE_QUEUE_NAME = 'dda-portal-telemetry-ingest-failures';
export const TELEMETRY_RULE_ERROR_LOG_GROUP = '/dda/telemetry/rule-errors';
export const TELEMETRY_PORTAL_RULE_NAME = 'dda_tel_ingest_portal';
export const TELEMETRY_FIREHOSE_SUMMARIES = 'dda-portal-telemetry-summaries';
export const TELEMETRY_FIREHOSE_EVENTS = 'dda-portal-telemetry-events';
/** The 18 API resources of design §11.2 (decision Q5; RB-1 counts the live API). */
export const TELEMETRY_API_RESOURCE_COUNT = 18;

/**
 * Workflow telemetry dashboards infrastructure (workflow-telemetry-dashboards
 * design §2, §6, §7, §11, §14, §15). Self-contained like the SyntheticDataStack
 * and the WorkflowDebugStack: it owns the four tables, the lake bucket, the
 * two Firehose streams, the four Python 3.12 functions with their own shared,
 * JWT and workflow_core layers, the per-account ingest aliases with their async
 * invoke configuration and on-failure queue, the Portal-account IoT rule, the
 * maintenance schedule, the alarms, and registers the `/telemetry/*` routes on
 * the imported portal Rest API with CORS preflight and a route-salted
 * CfnDeployment (RETAIN). The routes attach to the portal API's existing
 * Cognito authorizer, whose id is the `sharedAuthorizerId` prop (bin/app.ts
 * passes the CDK context value of that name and instantiates this stack
 * only when it is present; context-helpers.sharedAuthorizerId validates it):
 * API Gateway allows 10 authorizers per REST API and the portal API carries
 * one per route-registering stack already, so this stack creates none. The
 * only shared-file edit is the instantiation in bin/app.ts. It joins the
 * IAM synth gate with empty pre-feature baselines.
 */
export class TelemetryStack extends cdk.Stack {
  public readonly summariesTable: dynamodb.Table;
  public readonly eventsTable: dynamodb.Table;
  public readonly aggregatesTable: dynamodb.Table;
  public readonly configTable: dynamodb.Table;
  public readonly lakeBucket: s3.Bucket;
  public readonly ingestFunction: lambda.Function;
  public readonly rollupFunction: lambda.Function;
  public readonly apiFunction: lambda.Function;
  public readonly maintenanceFunction: lambda.Function;
  public readonly failureQueue: sqs.Queue;
  public readonly deploymentValues: { [env: string]: number } = {};
  public readonly dataPlaneGrantWired: boolean;
  public readonly apiResourceCount: number;
  /** The existing portal API authorizer every `/telemetry/*` method uses (the `sharedAuthorizerId` prop). */
  public readonly sharedAuthorizerId: string;

  constructor(scope: Construct, id: string, props: TelemetryStackProps) {
    super(scope, id, props);

    if (!props.trustedUseCaseAccountIds || props.trustedUseCaseAccountIds.length === 0) {
      throw new Error(
        'TelemetryStack requires a non-empty trustedUseCaseAccountIds list ' +
          '(pass -c trustedUseCaseAccountIds=<id>,<id>). Refusing to synth an ' +
          'sts:AssumeRole grant on a wildcard account.',
      );
    }

    // The existing authorizer the routes attach to. bin/app.ts constructs
    // this stack only with a context value present; a blank or malformed
    // prop still stops the synth here, before any asset is staged.
    this.sharedAuthorizerId = sharedAuthorizerId(props.sharedAuthorizerId, 'TelemetryStack');

    const region = cdk.Aws.REGION;
    const account = cdk.Aws.ACCOUNT_ID;

    // ------------------------------------------------------------------
    // Deployment values (design §15.2): range-checked at synth.
    // ------------------------------------------------------------------
    for (const spec of TELEMETRY_DEPLOYMENT_VALUES) {
      this.deploymentValues[spec.env] = resolveTelemetryValue(spec, this.node.tryGetContext(spec.key));
    }

    // ------------------------------------------------------------------
    // The data-plane grant (design §12.3): wired only when lib/context-helpers
    // exports dataPlaneGrant (or a test injects one).
    // ------------------------------------------------------------------
    const helperGrant = (contextHelpers as unknown as { dataPlaneGrant?: (allowlist: string[]) => DataPlaneGrant }).dataPlaneGrant;
    const grantFactory =
      props.dataPlaneGrantOverride === undefined ? helperGrant : props.dataPlaneGrantOverride ?? undefined;
    const dataPlaneGrant = typeof grantFactory === 'function' ? grantFactory(props.dataBucketAllowlist ?? []) : undefined;
    this.dataPlaneGrantWired = dataPlaneGrant !== undefined;

    // ------------------------------------------------------------------
    // Tables (design §7.1-§7.4): on demand, TTL expires_at, RETAIN.
    // ------------------------------------------------------------------
    const table = (id: string, name: string, extra: Partial<dynamodb.TableProps> = {}) =>
      new dynamodb.Table(this, id, {
        tableName: name,
        partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
        sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        timeToLiveAttribute: 'expires_at',
        removalPolicy: cdk.RemovalPolicy.RETAIN,
        ...extra,
      });
    this.summariesTable = table('TelemetrySummariesTable', TELEMETRY_SUMMARIES_TABLE_NAME, {
      stream: dynamodb.StreamViewType.NEW_IMAGE,
    });
    this.eventsTable = table('TelemetryEventsTable', TELEMETRY_EVENTS_TABLE_NAME, {
      stream: dynamodb.StreamViewType.NEW_IMAGE,
    });
    this.eventsTable.addGlobalSecondaryIndex({
      indexName: 'by-workflow',
      partitionKey: { name: 'gsi1pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'gsi1sk', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.aggregatesTable = table('TelemetryAggregatesTable', TELEMETRY_AGGREGATES_TABLE_NAME);
    this.configTable = table('TelemetryConfigTable', TELEMETRY_CONFIG_TABLE_NAME, {
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
    });
    this.configTable.addGlobalSecondaryIndex({
      indexName: 'by-usecase',
      partitionKey: { name: 'gsi1pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'gsi1sk', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.configTable.addGlobalSecondaryIndex({
      indexName: 'by-type',
      partitionKey: { name: 'gsi2pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'gsi2sk', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['enabled', 'account_id', 'region'],
    });

    // ------------------------------------------------------------------
    // The lake bucket (design §7.5): SSE-S3, Block Public Access, enforceSSL
    // (the one bucket-policy statement), object ownership enforced, no
    // versioning, RETAIN, lifecycle on raw/.
    // ------------------------------------------------------------------
    this.lakeBucket = new s3.Bucket(this, 'TelemetryLakeBucket', {
      bucketName: `edgecv-portal-telemetry-${account}-${region}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      versioned: false,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          id: 'TelemetryLakeRetention',
          prefix: 'raw/',
          enabled: true,
          expiration: cdk.Duration.days(this.deploymentValues.TELEMETRY_LAKE_RETENTION_DAYS),
        },
      ],
    });

    // ------------------------------------------------------------------
    // The two Direct PUT Firehose streams (design §7.5): GZIP, 300 s / 64 MB,
    // dated prefixes, error prefix under errors/, roles with aws:SourceAccount.
    // ------------------------------------------------------------------
    const firehoseLogGroup = new logs.LogGroup(this, 'TelemetryFirehoseLogGroup', {
      logGroupName: '/aws/kinesisfirehose/dda-portal-telemetry',
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const stream = (id: string, name: string, kind: 'summaries' | 'events') => {
      const role = new iam.Role(this, `${id}Role`, {
        description: `Delivery role of the ${name} Firehose stream (writes the telemetry lake)`,
        assumedBy: new iam.ServicePrincipal('firehose.amazonaws.com', {
          conditions: { StringEquals: { 'aws:SourceAccount': account } },
        }),
      });
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'TelemetryLakeBucket',
        effect: iam.Effect.ALLOW,
        actions: ['s3:GetBucketLocation', 's3:ListBucket', 's3:ListBucketMultipartUploads'],
        resources: [this.lakeBucket.bucketArn],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'TelemetryLakeObjects',
        effect: iam.Effect.ALLOW,
        actions: ['s3:PutObject', 's3:AbortMultipartUpload'],
        resources: [`${this.lakeBucket.bucketArn}/raw/*`, `${this.lakeBucket.bucketArn}/errors/*`],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'TelemetryFirehoseLogs',
        effect: iam.Effect.ALLOW,
        actions: ['logs:PutLogEvents'],
        resources: [`${firehoseLogGroup.logGroupArn}:*`],
      }));
      const logStream = new logs.LogStream(this, `${id}LogStream`, {
        logGroup: firehoseLogGroup,
        logStreamName: `${kind}-delivery`,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });
      const delivery = new firehose.CfnDeliveryStream(this, id, {
        deliveryStreamName: name,
        deliveryStreamType: 'DirectPut',
        extendedS3DestinationConfiguration: {
          bucketArn: this.lakeBucket.bucketArn,
          roleArn: role.roleArn,
          prefix: `raw/${kind}/dt=!{timestamp:yyyy-MM-dd}/`,
          errorOutputPrefix: `errors/${kind}/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/`,
          bufferingHints: { intervalInSeconds: 300, sizeInMBs: 64 },
          compressionFormat: 'GZIP',
          cloudWatchLoggingOptions: {
            enabled: true,
            logGroupName: firehoseLogGroup.logGroupName,
            logStreamName: logStream.logStreamName,
          },
        },
      });
      delivery.node.addDependency(role);
      return delivery;
    };
    stream('TelemetrySummariesStream', TELEMETRY_FIREHOSE_SUMMARIES, 'summaries');
    stream('TelemetryEventsStream', TELEMETRY_FIREHOSE_EVENTS, 'events');
    const streamArns = [TELEMETRY_FIREHOSE_SUMMARIES, TELEMETRY_FIREHOSE_EVENTS].map(
      (name) => `arn:aws:firehose:${region}:${account}:deliverystream/${name}`,
    );

    // ------------------------------------------------------------------
    // Layers (design §2.4): own shared and JWT layers on all four functions,
    // and an own workflow_core layer on the ingest only (workflow_core.stream_url).
    // ------------------------------------------------------------------
    const sharedLayer = new lambda.LayerVersion(this, 'TelemetrySharedLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/shared')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11, lambda.Runtime.PYTHON_3_12],
      description: 'Shared utilities for the workflow telemetry functions',
    });
    const jwtLayer = new lambda.LayerVersion(this, 'TelemetryJwtLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/jwt')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11, lambda.Runtime.PYTHON_3_12],
      description: 'JWT dependencies for the workflow telemetry functions',
    });
    const workflowCoreLayer = new lambda.LayerVersion(this, 'TelemetryWorkflowCoreLayer', {
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
    // The on-failure queue of the asynchronous ingest invocations and the
    // roll-up event sources (14 days).
    // ------------------------------------------------------------------
    this.failureQueue = new sqs.Queue(this, 'TelemetryFailureQueue', {
      queueName: TELEMETRY_FAILURE_QUEUE_NAME,
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });

    // ------------------------------------------------------------------
    // Environment (design §2.4).
    // ------------------------------------------------------------------
    const telemetryLambdaEnvironment: { [key: string]: string } = {
      USECASES_TABLE: props.useCasesTable.tableName,
      USER_ROLES_TABLE: props.userRolesTable.tableName,
      DEVICES_TABLE: props.devicesTable.tableName,
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
      TELEMETRY_SUMMARIES_TABLE: this.summariesTable.tableName,
      TELEMETRY_AGGREGATES_TABLE: this.aggregatesTable.tableName,
      TELEMETRY_EVENTS_TABLE: this.eventsTable.tableName,
      TELEMETRY_CONFIG_TABLE: this.configTable.tableName,
      TELEMETRY_LAKE_BUCKET: this.lakeBucket.bucketName,
      TELEMETRY_FIREHOSE_SUMMARIES,
      TELEMETRY_FIREHOSE_EVENTS,
      TELEMETRY_DATA_PLANE_GRANT: this.dataPlaneGrantWired ? 'tag' : 'absent',
      TELEMETRY_PORTAL_RULE_NAME,
    };
    for (const spec of TELEMETRY_DEPLOYMENT_VALUES) {
      telemetryLambdaEnvironment[spec.env] = String(this.deploymentValues[spec.env]);
    }

    // ------------------------------------------------------------------
    // Roles (design §14.1), one per function, each statement written out.
    // ------------------------------------------------------------------
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

    // Ingest role. GetItem on the summaries table is the two-pointer read of
    // the per-message gap estimate (§6.5); the design's list names the writes.
    const ingestRole = newRole('TelemetryIngestRole', 'Execution role of the workflow telemetry ingest function');
    ingestRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryStoreWrites',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:GetItem'],
      resources: [this.summariesTable.tableArn, this.eventsTable.tableArn],
    }));
    ingestRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryConfigCounters',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:UpdateItem', 'dynamodb:PutItem', 'dynamodb:GetItem'],
      resources: [this.configTable.tableArn],
    }));
    ingestRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryAttributionReads',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [props.useCasesTable.tableArn, props.devicesTable.tableArn, props.workflowsTable.tableArn],
    }));
    ingestRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryLakeWrite',
      effect: iam.Effect.ALLOW,
      actions: ['firehose:PutRecordBatch'],
      resources: streamArns,
    }));
    ingestRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryIdentityLookup',
      effect: iam.Effect.ALLOW,
      actions: ['iot:ListPrincipalThings'],
      resources: [`arn:aws:iot:${region}:${account}:cert/*`],
    }));
    ingestRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryAssumeUseCaseRole',
      effect: iam.Effect.ALLOW,
      actions: ['sts:AssumeRole'],
      resources: assumeRoleResources,
    }));

    // Roll-up role (the stream read grants come from the event sources).
    const rollupRole = newRole('TelemetryRollupRole', 'Execution role of the workflow telemetry roll-up function');
    rollupRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryRollupReads',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [this.summariesTable.tableArn, this.eventsTable.tableArn],
    }));
    rollupRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryAggregates',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:PutItem', 'dynamodb:Query'],
      resources: [this.aggregatesTable.tableArn],
    }));

    // API role.
    const apiRole = newRole('TelemetryApiRole', 'Execution role of the workflow telemetry API function');
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryConfigTable',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:DeleteItem',
        'dynamodb:ConditionCheckItem',
        'dynamodb:BatchGetItem',
        'dynamodb:Query',
      ],
      resources: [this.configTable.tableArn],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryConfigIndexes',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [`${this.configTable.tableArn}/index/by-usecase`, `${this.configTable.tableArn}/index/by-type`],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryStoreReads',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query', 'dynamodb:GetItem'],
      resources: [
        this.summariesTable.tableArn,
        this.aggregatesTable.tableArn,
        this.eventsTable.tableArn,
        `${this.eventsTable.tableArn}/index/by-workflow`,
      ],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryUseCaseAndRoleReads',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [props.useCasesTable.tableArn, props.userRolesTable.tableArn],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryDeviceRecords',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem', 'dynamodb:Scan'],
      resources: [props.devicesTable.tableArn],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryWorkflowReads',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem', 'dynamodb:Query'],
      resources: [props.workflowsTable.tableArn, `${props.workflowsTable.tableArn}/index/usecase-workflows-index`],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryAuditWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:PutItem'],
      resources: [props.auditLogTable.tableArn],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryAssumeUseCaseRole',
      effect: iam.Effect.ALLOW,
      actions: ['sts:AssumeRole'],
      resources: assumeRoleResources,
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryShadowControl',
      effect: iam.Effect.ALLOW,
      actions: ['iot:UpdateThingShadow', 'iot:GetThingShadow'],
      resources: [`arn:aws:iot:${region}:${account}:thing/*`],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryPortalRuleRead',
      effect: iam.Effect.ALLOW,
      actions: ['iot:GetTopicRule'],
      resources: [`arn:aws:iot:${region}:${account}:rule/${TELEMETRY_PORTAL_RULE_NAME}`],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryCoreDevices',
      effect: iam.Effect.ALLOW,
      actions: ['greengrass:GetCoreDevice', 'greengrass:ListCoreDevices', 'greengrass:ListTagsForResource'],
      resources: ['*'],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryMediaRead',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject'],
      resources: [`${resultsBucketArn}/dda-telemetry/*`],
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryMediaList',
      effect: iam.Effect.ALLOW,
      actions: ['s3:ListBucket'],
      resources: [resultsBucketArn],
      conditions: { StringLike: { 's3:prefix': 'dda-telemetry/*' } },
    }));
    apiRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryMediaRetentionRead',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetLifecycleConfiguration', 's3:GetBucketVersioning'],
      resources: [resultsBucketArn],
    }));

    // Maintenance role.
    const maintenanceRole = newRole(
      'TelemetryMaintenanceRole',
      'Execution role of the workflow telemetry maintenance function',
    );
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryConfigMaintenance',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem', 'dynamodb:Query'],
      resources: [this.configTable.tableArn],
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryConfigIndexesMaintenance',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [`${this.configTable.tableArn}/index/by-usecase`, `${this.configTable.tableArn}/index/by-type`],
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryUseCaseRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [props.useCasesTable.tableArn],
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryDeviceRecordsMaintenance',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem', 'dynamodb:Scan'],
      resources: [props.devicesTable.tableArn],
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryAssumeUseCaseRole',
      effect: iam.Effect.ALLOW,
      actions: ['sts:AssumeRole'],
      resources: assumeRoleResources,
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryShadowMaintenance',
      effect: iam.Effect.ALLOW,
      actions: ['iot:UpdateThingShadow'],
      resources: [`arn:aws:iot:${region}:${account}:thing/*`],
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryCoreDeviceStatus',
      effect: iam.Effect.ALLOW,
      actions: ['greengrass:GetCoreDevice'],
      resources: ['*'],
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryLiveFrameList',
      effect: iam.Effect.ALLOW,
      actions: ['s3:ListBucket'],
      resources: [resultsBucketArn],
      conditions: { StringLike: { 's3:prefix': 'dda-telemetry/live/*' } },
    }));
    maintenanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'TelemetryLiveFrameExpiry',
      effect: iam.Effect.ALLOW,
      actions: ['s3:DeleteObject'],
      resources: [`${resultsBucketArn}/dda-telemetry/live/*`],
    }));

    // The data-plane grant statements (design §12.3), once wired.
    if (dataPlaneGrant !== undefined) {
      apiRole.addToPolicy(new iam.PolicyStatement({
        sid: 'TelemetryDataPlaneObjectRead',
        effect: iam.Effect.ALLOW,
        actions: ['s3:GetObject'],
        resources: dataPlaneGrant.objectLevel,
        conditions: dataPlaneGrant.condition,
      }));
      apiRole.addToPolicy(new iam.PolicyStatement({
        sid: 'TelemetryDataPlaneBucketList',
        effect: iam.Effect.ALLOW,
        actions: ['s3:ListBucket'],
        resources: dataPlaneGrant.bucketLevel,
        conditions: dataPlaneGrant.condition,
      }));
      maintenanceRole.addToPolicy(new iam.PolicyStatement({
        sid: 'TelemetryDataPlaneBucketList',
        effect: iam.Effect.ALLOW,
        actions: ['s3:ListBucket'],
        resources: dataPlaneGrant.bucketLevel,
        conditions: dataPlaneGrant.condition,
      }));
      maintenanceRole.addToPolicy(new iam.PolicyStatement({
        sid: 'TelemetryDataPlaneLiveFrameExpiry',
        effect: iam.Effect.ALLOW,
        actions: ['s3:DeleteObject'],
        resources: dataPlaneGrant.objectPrefix('dda-telemetry/live'),
        conditions: dataPlaneGrant.condition,
      }));
    }

    // ------------------------------------------------------------------
    // The four functions (Python 3.12, the functions directory as the code
    // asset, fixed names so no environment refers to another's ARN).
    // ------------------------------------------------------------------
    const codeAsset = lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions'));

    this.ingestFunction = new lambda.Function(this, 'TelemetryIngest', {
      functionName: TELEMETRY_INGEST_FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'telemetry_ingest.handler',
      code: codeAsset,
      role: ingestRole,
      environment: telemetryLambdaEnvironment,
      layers: [sharedLayer, jwtLayer, workflowCoreLayer],
      memorySize: 256,
      timeout: cdk.Duration.seconds(20),
      reservedConcurrentExecutions: this.deploymentValues.TELEMETRY_INGEST_CONCURRENCY,
      description: 'Workflow telemetry ingest: the Lambda action target of the dda_tel_ingest rules',
    });

    this.rollupFunction = new lambda.Function(this, 'TelemetryRollup', {
      functionName: TELEMETRY_ROLLUP_FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'telemetry_rollup.handler',
      code: codeAsset,
      role: rollupRole,
      environment: telemetryLambdaEnvironment,
      layers: [sharedLayer, jwtLayer],
      memorySize: 512,
      timeout: cdk.Duration.minutes(2),
      description: 'Workflow telemetry roll-up: minute, hour and day aggregates from the summaries and events streams',
    });
    for (const source of [this.summariesTable, this.eventsTable]) {
      this.rollupFunction.addEventSource(
        new eventsources.DynamoEventSource(source, {
          startingPosition: lambda.StartingPosition.TRIM_HORIZON,
          batchSize: 100,
          maxBatchingWindow: cdk.Duration.seconds(60),
          reportBatchItemFailures: true,
          bisectBatchOnError: true,
          retryAttempts: 3,
          onFailure: new eventsources.SqsDlq(this.failureQueue),
        }),
      );
    }

    this.apiFunction = new lambda.Function(this, 'TelemetryApi', {
      functionName: TELEMETRY_API_FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'telemetry_api.handler',
      code: codeAsset,
      role: apiRole,
      environment: telemetryLambdaEnvironment,
      layers: [sharedLayer, jwtLayer],
      memorySize: 512,
      timeout: cdk.Duration.seconds(29),
      description: 'Workflow telemetry API: the /telemetry/* routes',
    });

    this.maintenanceFunction = new lambda.Function(this, 'TelemetryMaintenance', {
      functionName: TELEMETRY_MAINTENANCE_FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'telemetry_maintenance.handler',
      code: codeAsset,
      role: maintenanceRole,
      environment: telemetryLambdaEnvironment,
      layers: [sharedLayer, jwtLayer],
      memorySize: 256,
      timeout: cdk.Duration.minutes(4),
      description: 'Workflow telemetry maintenance: lease expiry, shadow re-sync, live-frame sweep (every 5 minutes)',
    });

    // ------------------------------------------------------------------
    // Ingest aliases (design §6.2): one per trusted account and the Portal
    // account, each with the resource-based statement for iot.amazonaws.com
    // conditioned on that account's dda_tel_* rules, and the asynchronous
    // invocation settings on the alias (Lambda stores them per qualifier).
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
        sourceArn: `arn:aws:iot:${region}:${entry.accountId}:rule/dda_tel_*`,
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
      throw new Error('TelemetryStack: no ingest alias for the Portal account');
    }

    // ------------------------------------------------------------------
    // The Portal-account rule (design §6.1): a role-less Lambda action on the
    // Portal alias and a CloudWatch Logs error action through its own error
    // role on /dda/telemetry/rule-errors (14 days).
    // ------------------------------------------------------------------
    const ruleErrorLogGroup = new logs.LogGroup(this, 'TelemetryRuleErrorLogGroup', {
      logGroupName: TELEMETRY_RULE_ERROR_LOG_GROUP,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const ruleErrorRole = new iam.Role(this, 'TelemetryRuleErrorRole', {
      description: 'Error-action role of the dda_tel_ingest_portal rule (writes rule errors to CloudWatch Logs)',
      assumedBy: new iam.ServicePrincipal('iot.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': account },
          ArnLike: { 'aws:SourceArn': `arn:aws:iot:${region}:${account}:rule/dda_tel_*` },
        },
      }),
    });
    ruleErrorRole.addToPolicy(new iam.PolicyStatement({
      sid: 'WriteTelemetryRuleErrors',
      effect: iam.Effect.ALLOW,
      actions: ['logs:CreateLogStream', 'logs:DescribeLogStreams', 'logs:PutLogEvents'],
      resources: [ruleErrorLogGroup.logGroupArn],
    }));

    new iot.CfnTopicRule(this, 'TelemetryIngestRule', {
      ruleName: TELEMETRY_PORTAL_RULE_NAME,
      topicRulePayload: {
        sql: TELEMETRY_RULE_SQL,
        awsIotSqlVersion: '2016-03-23',
        ruleDisabled: false,
        description: 'Deliver dda/telemetry messages of the Portal-account use cases to the workflow telemetry ingest alias',
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
    // Maintenance schedule (design §2.2): EventBridge Scheduler every
    // 5 minutes through a role that may invoke the maintenance function only.
    // ------------------------------------------------------------------
    const scheduleRole = new iam.Role(this, 'TelemetryScheduleRole', {
      description: 'Lets EventBridge Scheduler invoke the workflow telemetry maintenance function',
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com', {
        conditions: { StringEquals: { 'aws:SourceAccount': account } },
      }),
    });
    scheduleRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeTelemetryMaintenance',
      effect: iam.Effect.ALLOW,
      actions: ['lambda:InvokeFunction'],
      resources: [this.maintenanceFunction.functionArn],
    }));
    new scheduler.CfnSchedule(this, 'TelemetryMaintenanceSchedule', {
      name: 'dda-portal-telemetry-maintenance',
      description: 'Workflow telemetry maintenance every 5 minutes',
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
    // Alarms (design §6.5): terminal identity denials, the on-failure queue
    // depth and the roll-up iterator age.
    // ------------------------------------------------------------------
    new cloudwatch.Alarm(this, 'TelemetryIdentityUnavailableAlarm', {
      alarmName: 'dda-portal-telemetry-identity-unavailable',
      alarmDescription: 'The workflow telemetry ingest could not resolve device identities (AccessDenied on ListPrincipalThings)',
      metric: new cloudwatch.Metric({
        namespace: 'DDA/Telemetry',
        metricName: 'IdentityUnavailable',
        statistic: 'Sum',
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    new cloudwatch.Alarm(this, 'TelemetryFailureQueueAlarm', {
      alarmName: 'dda-portal-telemetry-failures-queued',
      alarmDescription: 'Workflow telemetry messages landed in the on-failure queue (ingest or roll-up retries exhausted)',
      metric: this.failureQueue.metricApproximateNumberOfMessagesVisible({
        statistic: 'Maximum',
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    new cloudwatch.Alarm(this, 'TelemetryRollupIteratorAgeAlarm', {
      alarmName: 'dda-portal-telemetry-rollup-lag',
      alarmDescription: 'The workflow telemetry roll-up is falling behind its streams (iterator age over 5 minutes)',
      metric: this.rollupFunction.metric('IteratorAge', {
        statistic: 'Maximum',
        period: cdk.Duration.minutes(5),
      }),
      threshold: 5 * 60 * 1000,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ------------------------------------------------------------------
    // API routes on the shared portal API (the SyntheticDataStack pattern).
    // ------------------------------------------------------------------
    const api = apigateway.RestApi.fromRestApiAttributes(this, 'PortalApi', {
      restApiId: props.restApiId,
      rootResourceId: props.restApiRootResourceId,
    });

    // The portal API's existing Cognito authorizer, by id (resolved above).
    // No AWS::ApiGateway::Authorizer is created here: the API carries one per
    // route-registering stack already and the quota is 10 per REST API, a
    // limit only AWS Support raises (the 2026-10-18 first deploy rolled back
    // on it). The existing authorizer validates the same user pool's tokens
    // from the same Authorization header, so the handlers read the same
    // claims as before.
    const authorizer: apigateway.IAuthorizer = {
      authorizerId: this.sharedAuthorizerId,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    };

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
    let resourceCount = 0;
    const addResource = (parent: apigateway.IResource, part: string, options?: apigateway.ResourceOptions) => {
      resourceCount += 1;
      return parent.addResource(part, options);
    };

    // Route matrix (design §11.2), 18 resources:
    //   GET        /telemetry/schemas
    //   GET, PUT   /telemetry/usecases/{usecase_id}/settings
    //   GET        /telemetry/usecases/{usecase_id}/templates
    //   GET        /telemetry/usecases/{usecase_id}/rejections
    //   GET        /telemetry/rejections
    //   GET, POST  /telemetry/dashboards
    //   GET, PATCH, DELETE /telemetry/dashboards/{dashboard_id}
    //   GET        /telemetry/dashboards/{dashboard_id}/{now,series,events,clips,live}
    //   GET        /telemetry/dashboards/{dashboard_id}/clips/{clip_ref}/media
    //   POST       /telemetry/dashboards/{dashboard_id}/live-lease
    const telemetryResource = addResource(api.root, 'telemetry', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(addResource(telemetryResource, 'schemas'), 'GET');
    const usecaseResource = addResource(addResource(telemetryResource, 'usecases'), '{usecase_id}');
    const settingsResource = addResource(usecaseResource, 'settings');
    addMethod(settingsResource, 'GET');
    addMethod(settingsResource, 'PUT');
    addMethod(addResource(usecaseResource, 'templates'), 'GET');
    addMethod(addResource(usecaseResource, 'rejections'), 'GET');
    addMethod(addResource(telemetryResource, 'rejections'), 'GET');
    const dashboardsResource = addResource(telemetryResource, 'dashboards');
    addMethod(dashboardsResource, 'GET');
    addMethod(dashboardsResource, 'POST');
    const dashboardResource = addResource(dashboardsResource, '{dashboard_id}');
    addMethod(dashboardResource, 'GET');
    addMethod(dashboardResource, 'PATCH');
    addMethod(dashboardResource, 'DELETE');
    addMethod(addResource(dashboardResource, 'now'), 'GET');
    addMethod(addResource(dashboardResource, 'series'), 'GET');
    addMethod(addResource(dashboardResource, 'events'), 'GET');
    const clipsResource = addResource(dashboardResource, 'clips');
    addMethod(clipsResource, 'GET');
    addMethod(addResource(addResource(clipsResource, '{clip_ref}'), 'media'), 'GET');
    addMethod(addResource(dashboardResource, 'live-lease'), 'POST');
    addMethod(addResource(dashboardResource, 'live'), 'GET');
    this.apiResourceCount = resourceCount;
    if (resourceCount !== TELEMETRY_API_RESOURCE_COUNT) {
      throw new Error(`TelemetryStack registers ${resourceCount} API resources; design §11.2 counts ${TELEMETRY_API_RESOURCE_COUNT}`);
    }

    // Deployment re-pointing the existing stage so the routes above go
    // live; the logical id is salted with the route table.
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

    const deployment = new apigateway.CfnDeployment(this, 'TelemetryDeployment', {
      restApiId: props.restApiId,
      stageName: props.apiStageName,
      description: 'Workflow telemetry routes deployment (workflow-telemetry-dashboards)',
    });
    deployment.overrideLogicalId(`TelemetryDeployment${routeSalt}`);
    deployment.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    // The /telemetry subtree holds every resource and method of the route table.
    deployment.node.addDependency(telemetryResource);

    new cdk.CfnOutput(this, 'TelemetryLakeBucketName', {
      value: this.lakeBucket.bucketName,
      description: 'Workflow telemetry lake bucket',
    });
    new cdk.CfnOutput(this, 'TelemetryFailureQueueArn', {
      value: this.failureQueue.queueArn,
      description: 'On-failure queue of the workflow telemetry ingest aliases and roll-up event sources',
    });
    new cdk.CfnOutput(this, 'TelemetryDataPlaneGrant', {
      value: this.dataPlaneGrantWired ? 'tag' : 'absent',
      description: 'Whether the use-case bucket data-plane grant is wired (design §12.3)',
    });
  }
}
