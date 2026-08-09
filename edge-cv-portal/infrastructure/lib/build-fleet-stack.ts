import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';
import * as crypto from 'crypto';
import * as path from 'path';

export interface BuildFleetStackProps extends cdk.StackProps {
  /** Shared RBAC table (permission checks in every build handler). */
  userRolesTable: dynamodb.Table;
  /** Shared Audit_Log table (build/fleet/config audit entries). */
  auditLogTable: dynamodb.Table;
  /** PortalSettings table (build_infrastructure_config item, Req 9.1). */
  settingsTable: dynamodb.Table;
  /** Portal Cognito user pool (route authorizer). */
  userPool: cognito.IUserPool;
  /** Rest API id of the existing portal API (ComputeStack.api). */
  restApiId: string;
  /** Root resource id of the existing portal API. */
  restApiRootResourceId: string;
  /** Stage the portal API serves on (ApiGatewayStack deployOptions). */
  apiStageName: string;
}

/**
 * Build fleet infrastructure (portal-build-fleet-and-workflow-gates,
 * task 10.1) following the node-designer-stack patterns:
 *
 * - DynamoDB tables: BuildJobs (status/server/request GSIs, 180-day TTL —
 *   above the 90-day retention floor of Req 3.4/4.7) and BuildServers
 *   (PAY_PER_REQUEST, PITR), per the design data models.
 * - The five build Lambdas (build_jobs, build_fleet, build_config,
 *   build_dispatcher, build_events) sharing the shared-utils layer and a
 *   common environment (table names, log group, event bus, SNS topic,
 *   instance profile, network placement).
 * - EventBridge: a 1-minute schedule driving the dispatcher tick (Req 3.1
 *   dispatch latency, 7.3 queue promotion, watchdogs) plus rules routing
 *   EC2 instance state-change, EC2 spot interruption, SSM command status,
 *   and the agent's custom `dda.portal.builds` phase events to
 *   build_events.py.
 * - CloudWatch Logs group `/dda/portal-builds` with SIX_MONTHS retention
 *   (>= the 90-day minimum of Req 3.4/4.4); the SSM agent commands stream
 *   build output here via CloudWatchOutputConfig.
 * - SNS topic `dda-portal-build-alerts` for orphaned-runner notifications
 *   (Req 3.9); Portal_Admins subscribe out of band.
 * - Scoped IAM: the Lambdas' EC2 mutating actions are condition-keyed to
 *   the `dda-build:*` tag namespace (RunInstances requires the request
 *   tag; Start/Stop/Terminate require the resource tag), SSM SendCommand
 *   is restricted to the AWS-RunShellScript document on dda-build-tagged
 *   instances, and iam:PassRole is limited to the build instance role.
 * - The build compute instance profile: a CDK-created extension of the
 *   manual launch script's `dda-build-role` (SSM core + the DDABuildPolicy
 *   Greengrass/IoT/S3/ECR-read permissions) extended with the ECR push
 *   actions, `events:PutEvents` on the default bus (agent phase events),
 *   and CloudWatch Logs — replacing launch-arm64-build-server.sh's inline
 *   role creation (design §10). Fixed physical names (`dda-build-role`)
 *   keep the handlers' defaults valid.
 * - A self-contained public-subnet VPC for build compute: instances get a
 *   public IP (outbound internet for git/docker/publish and the SSM
 *   endpoints) behind a security group with NO inbound rules — all access
 *   is IAM-audited SSM, no SSH key pair (design §2).
 * - API Gateway routes registered against the imported portal Rest API
 *   with a Cognito authorizer, exactly like the other portal route stacks
 *   (fresh deployment re-points the existing stage; logical id salted
 *   with the route table).
 */
export class BuildFleetStack extends cdk.Stack {
  public readonly buildJobsTable: dynamodb.Table;
  public readonly buildServersTable: dynamodb.Table;
  /** Build output log group `/dda/portal-builds` (Req 3.4, 4.4). */
  public readonly buildLogGroup: logs.LogGroup;
  /** `dda-portal-build-alerts` (orphaned-runner notifications, Req 3.9). */
  public readonly buildAlertTopic: sns.Topic;
  /** Instance role attached to every build server / ephemeral runner. */
  public readonly buildInstanceRole: iam.Role;

  // Build fleet Lambda handlers.
  public readonly buildJobsHandler: lambda.Function;
  public readonly buildFleetHandler: lambda.Function;
  public readonly buildConfigHandler: lambda.Function;
  public readonly buildDispatcherHandler: lambda.Function;
  public readonly buildEventsHandler: lambda.Function;

  /**
   * Resolve availability zones at deploy time (Fn::GetAZs) instead of via
   * a synth-time context lookup, exactly like the NodeDesignerStack: the
   * portal stacks synthesize without AWS credentials (e.g. `cdk synth` in
   * CI) and a context lookup for the build VPC would make credentials a
   * new mandatory requirement for that workflow.
   */
  public get availabilityZones(): string[] {
    return [
      cdk.Fn.select(0, cdk.Fn.getAzs()),
      cdk.Fn.select(1, cdk.Fn.getAzs()),
    ];
  }

  constructor(scope: Construct, id: string, props: BuildFleetStackProps) {
    super(scope, id, props);

    // Names the handlers default to (build_dispatcher.py / build_fleet.py
    // environment contract).
    const BUILD_LOG_GROUP_NAME = '/dda/portal-builds';
    const BUILD_ALERT_TOPIC_NAME = 'dda-portal-build-alerts';
    const BUILD_INSTANCE_PROFILE_NAME = 'dda-build-role';
    // Agent phase events (scripts/portal-build-agent.sh) publish to the
    // DEFAULT bus: the aws.ec2 / aws.ssm service events consumed by
    // build_events.py are only delivered there, so one bus serves all
    // four rules.
    const BUILD_EVENT_BUS = 'default';
    // Source repository cloned by the server/runner user-data bootstrap;
    // overridable via `-c buildRepoUrl=<url>` for forks.
    const buildRepoUrl: string =
      this.node.tryGetContext('buildRepoUrl') ||
      'https://github.com/awslabs/DefectDetectionApplication';

    // ------------------------------------------------------------------
    // DynamoDB tables (design "Data Models")
    // ------------------------------------------------------------------

    // BuildJobs: one item per Build_Job; GSIs back the status sweep, the
    // per-server queue (7.3 promotion orders by created_at), and the
    // request chain (1.3 sequential dispatch). The `ttl` attribute is 180
    // days from creation (build_jobs.py JOB_TTL_DAYS) — comfortably above
    // the 90-day retention floor (Req 3.4, 4.7).
    this.buildJobsTable = new dynamodb.Table(this, 'BuildJobsTable', {
      tableName: 'dda-portal-build-jobs',
      partitionKey: {
        name: 'build_job_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.buildJobsTable.addGlobalSecondaryIndex({
      indexName: 'status-index',
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.NUMBER },
    });
    this.buildJobsTable.addGlobalSecondaryIndex({
      indexName: 'server-index',
      partitionKey: { name: 'server_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.NUMBER },
    });
    this.buildJobsTable.addGlobalSecondaryIndex({
      indexName: 'request-index',
      partitionKey: { name: 'request_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'request_order', type: dynamodb.AttributeType.NUMBER },
    });

    // BuildServers: the Dedicated_Build_Server fleet registry, including
    // the running_build_job_id serialization allocation lock (Req 7.1).
    this.buildServersTable = new dynamodb.Table(this, 'BuildServersTable', {
      tableName: 'dda-portal-build-servers',
      partitionKey: {
        name: 'server_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ------------------------------------------------------------------
    // Build log group and alert topic
    // ------------------------------------------------------------------

    // Every agent SendCommand streams stdout/stderr here via
    // CloudWatchOutputConfig (stream {command_id}/{instance_id}/...);
    // SIX_MONTHS retention matches the 180-day job TTL and satisfies the
    // >= 90 day floor (Req 3.4, 4.4). RETAIN: logs must outlive any stack
    // replacement (they are the durable build record).
    this.buildLogGroup = new logs.LogGroup(this, 'BuildLogGroup', {
      logGroupName: BUILD_LOG_GROUP_NAME,
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Orphaned-runner notifications (termination retries exhausted after
    // 1 h, Req 3.9). Portal_Admin subscriptions are managed out of band.
    this.buildAlertTopic = new sns.Topic(this, 'BuildAlertTopic', {
      topicName: BUILD_ALERT_TOPIC_NAME,
      displayName: 'DDA portal build alerts (orphaned runners)',
    });

    // ------------------------------------------------------------------
    // Build compute network: self-contained VPC with public subnets (the
    // builds need outbound internet for the git clone, docker pulls, and
    // Greengrass/ECR publishing, and SSM connects over the public
    // endpoints). The security group has NO inbound rules — combined with
    // "no key pair" in the RunInstances calls, all access is SSM
    // (design §2). No NAT gateways: zero idle network cost.
    // ------------------------------------------------------------------
    const buildVpc = new ec2.Vpc(this, 'BuildVpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: 'build-public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
      ],
    });

    const buildSecurityGroup = new ec2.SecurityGroup(this, 'BuildSecurityGroup', {
      vpc: buildVpc,
      description:
        'DDA build compute (dedicated servers + ephemeral runners): no inbound rules, SSM only',
      allowAllOutbound: true,
    });

    // ------------------------------------------------------------------
    // Build instance role + profile: the extended dda-build-role
    // (design §10). CDK creation replaces launch-arm64-build-server.sh's
    // inline `aws iam create-role`; the fixed physical names match the
    // handlers' BUILD_INSTANCE_PROFILE_NAME default. Base permissions are
    // the manual script's DDABuildPolicy; the extension adds the ECR push
    // actions, events:PutEvents (agent phase events, design §5), and the
    // CloudWatch Logs actions were already present in the base policy.
    // ------------------------------------------------------------------
    this.buildInstanceRole = new iam.Role(this, 'BuildInstanceRole', {
      roleName: BUILD_INSTANCE_PROFILE_NAME,
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      description:
        'DDA build server / ephemeral runner role (SSM core + Greengrass/ECR publish + build events)',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
      ],
    });

    // DDABuildPolicy (launch-arm64-build-server.sh), statement for
    // statement. Unscopable actions stay isolated on Resource "*" exactly
    // as the script documents.
    this.buildInstanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'GreengrassPermissions',
      effect: iam.Effect.ALLOW,
      actions: [
        'greengrass:CreateComponentVersion',
        'greengrass:DescribeComponent',
        'greengrass:GetComponent',
        'greengrass:ListComponents',
        'greengrass:ListComponentVersions',
        'greengrass:ListCoreDevices',
        'greengrass:GetCoreDevice',
        'greengrass:ListInstalledComponents',
        'greengrass:ListTagsForResource',
        'greengrass:TagResource',
        'greengrass:ListDeployments',
        'greengrass:GetDeployment',
        'greengrass:CreateDeployment',
        'greengrass:CancelDeployment',
      ],
      resources: ['*'],
    }));
    this.buildInstanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'IoTThingPermissions',
      effect: iam.Effect.ALLOW,
      actions: [
        'iot:DescribeThing',
        'iot:CreateThing',
        'iot:UpdateThingShadow',
        'iot:AttachPolicy',
      ],
      resources: ['arn:aws:iot:*:*:thing/dda-*'],
    }));
    this.buildInstanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'IoTJobPermissions',
      effect: iam.Effect.ALLOW,
      actions: ['iot:DescribeJob'],
      resources: ['arn:aws:iot:*:*:job/*'],
    }));
    this.buildInstanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'IoTEndpointDiscovery',
      effect: iam.Effect.ALLOW,
      actions: ['iot:DescribeEndpoint'],
      resources: ['*'],
    }));
    this.buildInstanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'S3Permissions',
      effect: iam.Effect.ALLOW,
      actions: [
        's3:CreateBucket',
        's3:GetBucketLocation',
        's3:PutBucketVersioning',
        's3:GetObject',
        's3:PutObject',
        's3:ListBucket',
        's3:DeleteObject',
        's3:GetBucketVersioning',
        's3:ListBucketVersions',
        's3:GetBucketPolicy',
        's3:PutBucketPolicy',
        's3:GetBucketAcl',
        's3:PutBucketAcl',
        's3:GetBucketTagging',
        's3:PutBucketTagging',
      ],
      resources: [
        'arn:aws:s3:::dda-component-*',
        'arn:aws:s3:::dda-component-*/*',
        'arn:aws:s3:::dda-inference-results-*',
        'arn:aws:s3:::dda-inference-results-*/*',
      ],
    }));
    this.buildInstanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'S3ListAllBuckets',
      effect: iam.Effect.ALLOW,
      actions: ['s3:ListAllMyBuckets'],
      resources: ['*'],
    }));
    this.buildInstanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'EC2DescribePermissions',
      effect: iam.Effect.ALLOW,
      actions: [
        'ec2:DescribeInstances',
        'ec2:DescribeImages',
        'ec2:DescribeSecurityGroups',
        'ec2:DescribeSubnets',
        'ec2:DescribeVpcs',
        'ec2:DescribeKeyPairs',
        'ec2:DescribeTags',
      ],
      resources: ['*'],
    }));
    this.buildInstanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CloudWatchLogsPermissions',
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:CreateLogGroup',
        'logs:CreateLogStream',
        'logs:PutLogEvents',
        'logs:DescribeLogStreams',
      ],
      resources: ['arn:aws:logs:*:*:*'],
    }));
    this.buildInstanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CloudWatchMetricsPermissions',
      effect: iam.Effect.ALLOW,
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
    }));
    // ECR: base read actions plus the push/create extension (>2 GB build
    // artifacts publish container images, design §5 credentials note).
    this.buildInstanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ECRPermissions',
      effect: iam.Effect.ALLOW,
      actions: [
        'ecr:GetAuthorizationToken',
        'ecr:BatchGetImage',
        'ecr:GetDownloadUrlForLayer',
        'ecr:BatchCheckLayerAvailability',
        'ecr:PutImage',
        'ecr:InitiateLayerUpload',
        'ecr:UploadLayerPart',
        'ecr:CompleteLayerUpload',
        'ecr:CreateRepository',
        'ecr:DescribeRepositories',
      ],
      resources: ['*'],
    }));
    // Agent phase events (phase=building/publishing/succeeded/failed) go
    // to the default bus for the dda.portal.builds rule below.
    this.buildInstanceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BuildPhaseEvents',
      effect: iam.Effect.ALLOW,
      actions: ['events:PutEvents'],
      resources: [
        `arn:aws:events:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:event-bus/${BUILD_EVENT_BUS}`,
      ],
    }));

    const buildInstanceProfile = new iam.CfnInstanceProfile(this, 'BuildInstanceProfile', {
      instanceProfileName: BUILD_INSTANCE_PROFILE_NAME,
      roles: [this.buildInstanceRole.roleName],
    });

    // ------------------------------------------------------------------
    // Lambda layer: this stack builds its own shared-utils layer version
    // from the same asset as the ComputeStack (the NodeDesignerStack does
    // the same), keeping the cross-stack dependency one-directional.
    // ------------------------------------------------------------------
    const sharedLayer = new lambda.LayerVersion(this, 'BuildFleetSharedLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/shared')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'Shared utilities for the build fleet Lambda functions',
    });

    // ------------------------------------------------------------------
    // Common IAM + environment for the five handlers.
    // ------------------------------------------------------------------
    const createHandlerRole = (name: string) => {
      const role = new iam.Role(this, `${name}Role`, {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        ],
      });
      this.buildJobsTable.grantReadWriteData(role);
      this.buildServersTable.grantReadWriteData(role);
      props.userRolesTable.grantReadData(role);
      props.auditLogTable.grantWriteData(role);
      props.settingsTable.grantReadData(role);
      return role;
    };

    // Environment contract of the handlers ("Environment variables
    // (build-fleet-stack.ts lambdaEnvironment)" in each module).
    const lambdaEnvironment: { [key: string]: string } = {
      BUILD_JOBS_TABLE: this.buildJobsTable.tableName,
      BUILD_SERVERS_TABLE: this.buildServersTable.tableName,
      SETTINGS_TABLE: props.settingsTable.tableName,
      USER_ROLES_TABLE: props.userRolesTable.tableName,
      AUDIT_LOG_TABLE: props.auditLogTable.tableName,
      BUILD_LOG_GROUP: BUILD_LOG_GROUP_NAME,
      BUILD_EVENT_BUS,
      BUILD_ALERT_TOPIC_ARN: this.buildAlertTopic.topicArn,
      BUILD_INSTANCE_PROFILE_NAME,
      BUILD_INSTANCE_PROFILE_ARN: buildInstanceProfile.attrArn,
      BUILD_SECURITY_GROUP_ID: buildSecurityGroup.securityGroupId,
      BUILD_SUBNET_ID: buildVpc.publicSubnets[0].subnetId,
      BUILD_REPO_URL: buildRepoUrl,
    };

    // --------------------------------------------------------- IAM shapes

    // EC2 tag namespace scoping every mutating EC2 action (design §10).
    const TAG_FLEET = 'dda-build:fleet';
    const TAG_EPHEMERAL = 'dda-build:ephemeral';

    const ec2InstanceArn = `arn:aws:ec2:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:instance/*`;
    // RunInstances touches untaggable/request-scoped resources besides the
    // instance; those get their own statement without a tag condition.
    const runInstancesAncillaryArns = [
      `arn:aws:ec2:${cdk.Aws.REGION}::image/*`,
      `arn:aws:ec2:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:volume/*`,
      `arn:aws:ec2:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:network-interface/*`,
      `arn:aws:ec2:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:security-group/*`,
      `arn:aws:ec2:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:subnet/*`,
    ];

    /** RunInstances allowed only when the launch tags the instance with
     * the given dda-build:* tag (aws:RequestTag), plus CreateTags scoped
     * to the RunInstances action and the ancillary resource statement. */
    const grantRunInstances = (role: iam.Role, requestTagKey: string) => {
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ec2:RunInstances'],
        resources: [ec2InstanceArn],
        conditions: {
          StringEquals: { [`aws:RequestTag/${requestTagKey}`]: 'true' },
        },
      }));
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ec2:RunInstances'],
        resources: runInstancesAncillaryArns,
      }));
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ec2:CreateTags'],
        resources: [ec2InstanceArn],
        conditions: {
          StringEquals: { 'ec2:CreateAction': 'RunInstances' },
        },
      }));
      // The hardened launch profile (extended dda-build-role) is attached
      // at RunInstances time.
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['iam:PassRole'],
        resources: [this.buildInstanceRole.roleArn],
        conditions: {
          StringEquals: { 'iam:PassedToService': 'ec2.amazonaws.com' },
        },
      }));
    };

    /** Lifecycle actions permitted only on dda-build-tagged instances
     * (aws:ResourceTag condition key). */
    const grantInstanceLifecycle = (
      role: iam.Role, actions: string[], resourceTagKey: string,
    ) => {
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions,
        resources: [ec2InstanceArn],
        conditions: {
          StringEquals: { [`aws:ResourceTag/${resourceTagKey}`]: 'true' },
        },
      }));
    };

    /** Describe* calls have no resource-level scoping in IAM. */
    const grantEc2Describe = (role: iam.Role) => {
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ec2:DescribeInstances', 'ec2:DescribeImages'],
        resources: ['*'],
      }));
    };

    /** SSM SendCommand restricted to AWS-RunShellScript on instances
     * carrying one of the dda-build:* tags; invocation reads and the
     * managed-instance ping have no resource-level scoping. */
    const grantSsmCommands = (role: iam.Role) => {
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ssm:SendCommand'],
        resources: [`arn:aws:ssm:${cdk.Aws.REGION}::document/AWS-RunShellScript`],
      }));
      for (const tagKey of [TAG_FLEET, TAG_EPHEMERAL]) {
        role.addToPolicy(new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ssm:SendCommand'],
          resources: [ec2InstanceArn],
          conditions: {
            StringEquals: { [`ssm:resourceTag/${tagKey}`]: 'true' },
          },
        }));
      }
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetCommandInvocation', 'ssm:DescribeInstanceInformation'],
        resources: ['*'],
      }));
    };

    /** Read-only invocation/command recovery for terminal reconciliation
     * (build-fleet-execution-failures Req 2.1, 2.5, 2.12): the event
     * consumer retrieves the final invocation, and the dispatcher's
     * scheduled tick additionally recovers ambiguous sends through a
     * recent-command lookup. GetCommandInvocation and ListCommands
     * support no resource-level scoping; no mutating action is granted
     * here. This is service execution wiring, not user RBAC. */
    const grantSsmReconciliationReads = (role: iam.Role) => {
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetCommandInvocation', 'ssm:ListCommands'],
        resources: ['*'],
      }));
    };

    /** Public canonical Ubuntu 22.04 AMI SSM parameters (AMI resolution
     * in build_fleet.py / build_dispatcher.py). */
    const grantAmiParameterRead = (role: iam.Role) => {
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetParameter'],
        resources: [
          `arn:aws:ssm:${cdk.Aws.REGION}::parameter/aws/service/canonical/*`,
        ],
      }));
    };

    // ------------------------------------------------------------------
    // Lambda handlers
    // ------------------------------------------------------------------
    const functionsAsset = lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions'));

    // build_dispatcher.py — async on-submit invoke + 1-minute schedule;
    // executes the build_planner decisions (dedicated dispatch, ephemeral
    // provisioning, watchdogs, sweeps). Created first so build_jobs can
    // carry its function name.
    const dispatcherRole = createHandlerRole('BuildDispatcher');
    grantRunInstances(dispatcherRole, TAG_EPHEMERAL);
    // The dispatcher terminates ephemeral runners (terminal-status
    // watchdog + partial-provisioning cleanup, Req 3.2, 3.7, 3.9).
    grantInstanceLifecycle(dispatcherRole, ['ec2:TerminateInstances', 'ec2:StopInstances'], TAG_EPHEMERAL);
    grantEc2Describe(dispatcherRole);
    grantSsmCommands(dispatcherRole);
    // Scheduled command reconciliation + ambiguous-send recovery
    // (build-fleet-execution-failures Req 2.5, 2.7).
    grantSsmReconciliationReads(dispatcherRole);
    grantAmiParameterRead(dispatcherRole);
    this.buildDispatcherHandler = new lambda.Function(this, 'BuildDispatcherHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'build_dispatcher.handler',
      code: functionsAsset,
      role: dispatcherRole,
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-21-build-dispatcher',
      },
      layers: [sharedLayer],
      // A tick runs several synchronous SSM verifications (60 s window
      // each); overlapping ticks stay safe through the conditional-update
      // transitions and the DynamoDB allocation lock.
      timeout: cdk.Duration.seconds(300),
      memorySize: 256,
    });
    this.buildAlertTopic.grantPublish(this.buildDispatcherHandler);

    // build_jobs.py — submit/list/get/logs/cancel/retry (Req 1, 4).
    const jobsRole = createHandlerRole('BuildJobs');
    // Cancel of a running job: SSM stop command + pgrep confirmation.
    grantSsmCommands(jobsRole);
    // GET /builds/{id}/logs pages the job's CloudWatch stream (Req 4.4).
    jobsRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['logs:GetLogEvents', 'logs:DescribeLogStreams', 'logs:FilterLogEvents'],
      resources: [
        this.buildLogGroup.logGroupArn,
        `${this.buildLogGroup.logGroupArn}:*`,
      ],
    }));
    this.buildJobsHandler = new lambda.Function(this, 'BuildJobsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'build_jobs.handler',
      code: functionsAsset,
      role: jobsRole,
      environment: {
        ...lambdaEnvironment,
        BUILD_DISPATCHER_FUNCTION_NAME: this.buildDispatcherHandler.functionName,
        CODE_VERSION: '2026-02-21-build-jobs',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(120),
    });
    // Immediate dispatch on submit: async Event invoke of the dispatcher
    // (ephemeral provisioning starts well within 60 s, Req 3.1).
    this.buildDispatcherHandler.grantInvoke(this.buildJobsHandler);

    // build_fleet.py — Dedicated_Build_Server lifecycle (Req 6): launch
    // with the hardened profile (Req 6.5), start/stop/terminate with
    // validate_fleet_action, live DescribeInstances reconciliation.
    const fleetRole = createHandlerRole('BuildFleet');
    grantRunInstances(fleetRole, TAG_FLEET);
    grantInstanceLifecycle(
      fleetRole,
      ['ec2:StartInstances', 'ec2:StopInstances', 'ec2:TerminateInstances'],
      TAG_FLEET,
    );
    grantEc2Describe(fleetRole);
    grantAmiParameterRead(fleetRole);
    this.buildFleetHandler = new lambda.Function(this, 'BuildFleetHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'build_fleet.handler',
      code: functionsAsset,
      role: fleetRole,
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-21-build-fleet',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(120),
    });

    // build_config.py — build_infrastructure_config read/update in the
    // PortalSettings table (Req 9.1); updates are PortalAdmin-only and
    // audited per applied change.
    const configRole = createHandlerRole('BuildConfig');
    props.settingsTable.grantReadWriteData(configRole);
    this.buildConfigHandler = new lambda.Function(this, 'BuildConfigHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'build_config.handler',
      code: functionsAsset,
      role: configRole,
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-21-build-config',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // build_events.py — EventBridge consumer (agent phase events, EC2
    // state changes, spot interruptions, SSM command status). DynamoDB
    // conditional updates plus the READ-ONLY GetCommandInvocation
    // retrieval for terminal command reconciliation
    // (build-fleet-execution-failures Req 2.1, 2.12): no mutating
    // EC2/SSM action is granted to this consumer.
    const eventsRole = createHandlerRole('BuildEvents');
    grantSsmReconciliationReads(eventsRole);
    this.buildEventsHandler = new lambda.Function(this, 'BuildEventsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'build_events.handler',
      code: functionsAsset,
      role: eventsRole,
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-21-build-events',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // ------------------------------------------------------------------
    // EventBridge wiring
    // ------------------------------------------------------------------

    // 1-minute dispatcher tick: bounds every "within 5 minutes"
    // requirement (7.3 promotion, 7.6 re-verification, watchdogs).
    new events.Rule(this, 'BuildDispatcherSchedule', {
      ruleName: 'dda-portal-build-dispatcher-tick',
      description: 'Runs the build dispatcher tick every minute',
      schedule: events.Schedule.rate(cdk.Duration.minutes(1)),
      targets: [new targets.LambdaFunction(this.buildDispatcherHandler)],
    });

    // Agent phase events (scripts/portal-build-agent.sh PutEvents).
    new events.Rule(this, 'BuildPhaseEventsRule', {
      ruleName: 'dda-portal-build-phase-events',
      description: 'Delivers dda.portal.builds agent phase events to build_events.py',
      eventPattern: {
        source: ['dda.portal.builds'],
      },
      targets: [new targets.LambdaFunction(this.buildEventsHandler)],
    });

    // EC2 instance state changes: fleet lifecycle reconciliation
    // (Req 6.2, 6.3, 6.9) and instance-loss interruption for jobs
    // (Req 3.5). build_events ignores instances it does not track.
    new events.Rule(this, 'BuildInstanceStateChangeRule', {
      ruleName: 'dda-portal-build-instance-state',
      description: 'Delivers EC2 instance state-change notifications to build_events.py',
      eventPattern: {
        source: ['aws.ec2'],
        detailType: ['EC2 Instance State-change Notification'],
      },
      targets: [new targets.LambdaFunction(this.buildEventsHandler)],
    });

    // Spot interruption warnings → interrupted status + retry (Req 3.5).
    new events.Rule(this, 'BuildSpotInterruptionRule', {
      ruleName: 'dda-portal-build-spot-interruption',
      description: 'Delivers EC2 spot interruption warnings to build_events.py',
      eventPattern: {
        source: ['aws.ec2'],
        detailType: ['EC2 Spot Instance Interruption Warning'],
      },
      targets: [new targets.LambdaFunction(this.buildEventsHandler)],
    });

    // SSM command status: every TERMINAL agent command status is routed
    // to the reconciliation path (build-fleet-execution-failures
    // Req 2.1) — `Success` is included so a successful command whose
    // agent result never arrives can be settled to AGENT_RESULT_MISSING;
    // all pre-existing failure statuses are preserved.
    new events.Rule(this, 'BuildSsmCommandStatusRule', {
      ruleName: 'dda-portal-build-ssm-command-status',
      description: 'Delivers SSM command status changes to build_events.py',
      eventPattern: {
        source: ['aws.ssm'],
        detailType: [
          'EC2 Command Status-change Notification',
          'EC2 Command Invocation Status-change Notification',
        ],
        detail: {
          status: ['Success', 'Failed', 'TimedOut', 'Cancelled'],
        },
      },
      targets: [new targets.LambdaFunction(this.buildEventsHandler)],
    });

    // ------------------------------------------------------------------
    // API Gateway routes against the imported portal Rest API (same
    // pattern as NodeDesignerApiStack / UserAdminApiStack: authorizers are
    // per-Rest-API resources, so this stack attaches its own Cognito
    // authorizer instance; a fresh deployment re-points the stage).
    // ------------------------------------------------------------------
    const api = apigateway.RestApi.fromRestApiAttributes(this, 'PortalApi', {
      restApiId: props.restApiId,
      rootResourceId: props.restApiRootResourceId,
    });

    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'BuildFleetAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'EdgeCVPortalBuildFleetAuthorizer',
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

    // allowTestInvoke: false — one AWS::Lambda::Permission per method
    // (same resource-count economy as the other portal route stacks).
    const jobsIntegration = new apigateway.LambdaIntegration(this.buildJobsHandler, { allowTestInvoke: false });
    const fleetIntegration = new apigateway.LambdaIntegration(this.buildFleetHandler, { allowTestInvoke: false });
    const configIntegration = new apigateway.LambdaIntegration(this.buildConfigHandler, { allowTestInvoke: false });

    const methods: apigateway.Method[] = [];
    const addMethod = (
      resource: apigateway.IResource,
      httpMethod: string,
      integration: apigateway.LambdaIntegration,
    ) => {
      methods.push(
        resource.addMethod(httpMethod, integration, {
          authorizer,
          authorizationType: apigateway.AuthorizationType.COGNITO,
        }),
      );
    };

    // /builds — submit + history (build_jobs.py).
    const buildsResource = api.root.addResource('builds', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(buildsResource, 'POST', jobsIntegration);
    addMethod(buildsResource, 'GET', jobsIntegration);

    // /builds/{id} — detail, logs, cancel, retry.
    const buildResource = buildsResource.addResource('{id}');
    addMethod(buildResource, 'GET', jobsIntegration);
    addMethod(buildResource.addResource('logs'), 'GET', jobsIntegration);
    addMethod(buildResource.addResource('cancel'), 'POST', jobsIntegration);
    addMethod(buildResource.addResource('retry'), 'POST', jobsIntegration);

    // /build-servers — fleet list/launch (build_fleet.py).
    const serversResource = api.root.addResource('build-servers', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(serversResource, 'GET', fleetIntegration);
    addMethod(serversResource, 'POST', fleetIntegration);

    // /build-servers/{id} — terminate (DELETE with confirm-name echo),
    // start, stop.
    const serverResource = serversResource.addResource('{id}');
    addMethod(serverResource, 'DELETE', fleetIntegration);
    addMethod(serverResource.addResource('start'), 'POST', fleetIntegration);
    addMethod(serverResource.addResource('stop'), 'POST', fleetIntegration);

    // /build-config — effective config read / PortalAdmin update
    // (build_config.py, Req 9.1).
    const configResource = api.root.addResource('build-config', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(configResource, 'GET', configIntegration);
    addMethod(configResource, 'PUT', configIntegration);

    // /build-branches — branch discovery for the submission form's
    // repository field (build_jobs.list_build_branches, guarded by the
    // builds read boundary; build-source-selection Req 3.1, 3.4).
    const branchesResource = api.root.addResource('build-branches', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(branchesResource, 'GET', jobsIntegration);

    // Deployment re-pointing the existing stage so the routes above go
    // live; logical id salted with the route table so any route change
    // rolls a new deployment (same pattern as NodeDesignerApiStack).
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

    const deployment = new apigateway.CfnDeployment(this, 'BuildFleetDeployment', {
      restApiId: props.restApiId,
      stageName: props.apiStageName,
      description: 'Build fleet routes deployment (portal-build-fleet-and-workflow-gates)',
    });
    deployment.overrideLogicalId(`BuildFleetDeployment${routeSalt}`);
    // Every resource/method (including the CORS preflight OPTIONS methods)
    // and the authorizer must exist before the deployment snapshot.
    deployment.node.addDependency(authorizer);
    deployment.node.addDependency(buildsResource);
    deployment.node.addDependency(serversResource);
    deployment.node.addDependency(configResource);
    deployment.node.addDependency(branchesResource);

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new cdk.CfnOutput(this, 'BuildJobsTableName', {
      value: this.buildJobsTable.tableName,
      description: 'BuildJobs DynamoDB table',
    });
    new cdk.CfnOutput(this, 'BuildServersTableName', {
      value: this.buildServersTable.tableName,
      description: 'BuildServers DynamoDB table',
    });
    new cdk.CfnOutput(this, 'BuildLogGroupName', {
      value: this.buildLogGroup.logGroupName,
      description: 'CloudWatch Logs group for build output (>= 90-day retention)',
    });
    new cdk.CfnOutput(this, 'BuildAlertTopicArn', {
      value: this.buildAlertTopic.topicArn,
      description: 'SNS topic for build alerts (orphaned runners)',
    });
    new cdk.CfnOutput(this, 'BuildInstanceProfileArn', {
      value: buildInstanceProfile.attrArn,
      description: 'Instance profile attached to build servers and ephemeral runners',
    });
    new cdk.CfnOutput(this, 'BuildSecurityGroupId', {
      value: buildSecurityGroup.securityGroupId,
      description: 'No-inbound security group for build compute',
    });
  }
}
