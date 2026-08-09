import * as cdk from 'aws-cdk-lib';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { Construct } from 'constructs';
import * as path from 'path';
import { NodeDesignerApiStack } from './node-designer-api-stack';

/**
 * The five plugin Target_Architectures (custom-node-designer requirements
 * glossary): plain x86_64, x86_64 with the NVIDIA GPU runtime, and the three
 * Jetson JetPack generations. One CodeBuild project per architecture, each
 * with its own custom build image (Requirements 3.1, 5.2).
 */
export const PLUGIN_BUILD_ARCHITECTURES = [
  'x86_64',
  'x86_64_nvidia',
  'arm64_jp4',
  'arm64_jp5',
  'arm64_jp6',
] as const;

export interface NodeDesignerStackProps extends cdk.StackProps {
  /** Portal artifacts bucket (plugin sources, Plugin_Library, sim results). */
  portalArtifactsBucket: s3.Bucket;
  useCasesTable: dynamodb.Table;
  userRolesTable: dynamodb.Table;
  auditLogTable: dynamodb.Table;
  settingsTable: dynamodb.Table;
  workflowsTable: dynamodb.Table;
  workflowVersionsTable: dynamodb.Table;
  testDatasetsTable: dynamodb.Table;
  /**
   * Trusted UseCase account IDs the plugin_components handler may assume
   * DDAPortalAccessRole into (Plugin_Component registration happens in the
   * Use_Case account Greengrass registry). Same context source and same
   * non-empty synth-time requirement as the ComputeStack.
   */
  trustedUseCaseAccountIds: string[];
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
 * Node_Designer infrastructure (custom-node-designer, task 6.1) following the
 * test-runner-stack patterns:
 *
 * - DynamoDB tables: PluginRecords, CustomNodeTypes, ModuleIndexCache (TTL),
 *   SimulationRuns, NodeGenSessions (TTL) per the design data model.
 * - The portal installation's asymmetric KMS signing key (ECDSA P-256,
 *   SIGN_VERIFY) used to sign Plugin_Artifact SHA-256 digests after successful
 *   builds and to verify them at packaging time (Requirements 3.3, 10.4).
 * - Five CodeBuild projects, one per Target_Architecture, each running a
 *   per-arch custom build image from the ECR repository created here:
 *   x86_64 (Ubuntu 22.04 / GStreamer 1.20, matching the test-sandbox image),
 *   x86_64_nvidia (same base plus the CUDA toolkit and NVIDIA GStreamer
 *   runtime headers), and arm64 JetPack 4/5/6 cross-build images pinning the
 *   DeepStream SDK version matching each JetPack release (Requirement 5.2).
 *   Every build runs in a fresh CodeBuild container with a role scoped to
 *   exactly the plugin-source and staging prefixes of that architecture and
 *   NO VpcConfig, so builds have no network path to portal internals
 *   (Requirement 3.2). Outbound internet stays available for source-declared
 *   dependency fetches during import builds.
 * - A lightweight "fetch" CodeBuild project that clones a public repository at
 *   a requested revision and syncs the tree to the plugin-sources prefix
 *   (Requirement 4.1 transport; orchestrated by plugin_importer.py).
 * - An EventBridge rule delivering CodeBuild build-state-change results to the
 *   plugin_builds.py handler (per-arch artifact/status recording, 3.4).
 * - The seven Node_Designer Lambda handlers. Their API Gateway routes are
 *   registered by the NodeDesignerApiStack nested under THIS stack against
 *   the imported portal Rest API (the API's own nested stack sits at the
 *   CloudFormation 500-resource limit, so routes get their own nested stack;
 *   nesting here keeps the cross-stack dependency one-directional:
 *   NodeDesignerStack depends on the ComputeStack API, never the reverse).
 *
 * - The Plugin_Simulator Step Functions state machine (task 8.2):
 *   Guard -> Prepare -> RunSandbox (Fargate, isolated subnet) -> Collect,
 *   running the existing test-sandbox image with HARNESS_MODE=simulate. The
 *   sandbox task role is limited to the run's plugin-simulations/... S3
 *   prefix — no Plugin_Library write path, no other Use_Case data
 *   (Requirement 7.2). A 5-minute execution timeout on the RunSandbox state
 *   stops the Fargate task; the record_timeout step marks the run
 *   failed-with-timeout while the incrementally flushed partial results in
 *   S3 stay untouched (Requirement 7.7).
 */
export class NodeDesignerStack extends cdk.Stack {
  public readonly pluginRecordsTable: dynamodb.Table;
  public readonly customNodeTypesTable: dynamodb.Table;
  public readonly moduleIndexCacheTable: dynamodb.Table;
  public readonly simulationRunsTable: dynamodb.Table;
  public readonly nodeGenSessionsTable: dynamodb.Table;
  /** Asymmetric ECDSA P-256 signing key for Plugin_Artifact signatures. */
  public readonly pluginSigningKey: kms.Key;
  /** ECR repository holding the per-arch plugin build images (tag = arch). */
  public readonly buildImageRepository: ecr.Repository;
  /** Per-Target_Architecture build projects, keyed by architecture. */
  public readonly buildProjects: { [arch: string]: codebuild.Project };
  /** Lightweight repository-fetch project used by plugin_importer.py. */
  public readonly fetchProject: codebuild.Project;
  /** Plugin_Simulator state machine (Guard -> Prepare -> RunSandbox -> Collect). */
  public readonly simulatorStateMachine: sfn.StateMachine;

  // Node_Designer Lambda handlers (routes registered in NodeDesignerApiStack).
  public readonly pluginRecordsHandler: lambda.Function;
  public readonly pluginImporterHandler: lambda.Function;
  public readonly nodeGeneratorHandler: lambda.Function;
  public readonly pluginBuildsHandler: lambda.Function;
  public readonly pluginComponentsHandler: lambda.Function;
  public readonly pluginSimulatorHandler: lambda.Function;
  public readonly customNodeTypesHandler: lambda.Function;

  /**
   * Resolve availability zones at deploy time (Fn::GetAZs) instead of via a
   * synth-time context lookup, exactly like the TestRunnerStack: the portal
   * stacks synthesize without AWS credentials (e.g. `cdk synth` in CI) and a
   * context lookup for the simulator VPC would make credentials a new
   * mandatory requirement for that workflow.
   */
  public get availabilityZones(): string[] {
    return [
      cdk.Fn.select(0, cdk.Fn.getAzs()),
      cdk.Fn.select(1, cdk.Fn.getAzs()),
    ];
  }

  constructor(scope: Construct, id: string, props: NodeDesignerStackProps) {
    super(scope, id, props);

    // Same synth-time guard as the ComputeStack: never fall back to a
    // wildcard account for sts:AssumeRole.
    if (!props.trustedUseCaseAccountIds || props.trustedUseCaseAccountIds.length === 0) {
      throw new Error(
        'NodeDesignerStack requires a non-empty trustedUseCaseAccountIds list ' +
          '(pass -c trustedUseCaseAccountIds=<id>,<id>). Refusing to synth an ' +
          'sts:AssumeRole grant on a wildcard account.'
      );
    }

    const bucket = props.portalArtifactsBucket;

    // S3 prefixes per the design "S3 layout (portal artifacts bucket)".
    const PLUGIN_SOURCES_PREFIX = 'plugin-sources';
    const PLUGIN_STAGING_PREFIX = 'plugin-staging';
    const PLUGIN_LIBRARY_CUSTOM_PREFIX = 'workflow-plugins/custom';
    const PLUGIN_SIMULATIONS_PREFIX = 'plugin-simulations';

    // Fixed name of the Plugin_Simulator state machine so its ARN can be
    // composed for the plugin_simulator.py environment without a token
    // reference (the state machine invokes the same Lambda for its Guard/
    // Prepare/Collect steps, which would otherwise be a circular reference).
    const SIMULATOR_STATE_MACHINE_NAME = 'dda-plugin-simulator';
    const simulatorStateMachineArn =
      `arn:aws:states:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}` +
      `:stateMachine:${SIMULATOR_STATE_MACHINE_NAME}`;

    // ------------------------------------------------------------------
    // DynamoDB tables (design "Data Models" - new, additive)
    // ------------------------------------------------------------------

    // PluginRecords: plugin_id + version; versions are separate items so a new
    // version resets Lifecycle_State/review independently (9.13, 10.5).
    this.pluginRecordsTable = new dynamodb.Table(this, 'PluginRecordsTable', {
      tableName: 'dda-portal-plugin-records',
      partitionKey: {
        name: 'plugin_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'version',
        type: dynamodb.AttributeType.NUMBER,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.pluginRecordsTable.addGlobalSecondaryIndex({
      indexName: 'usecase-plugins-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'plugin_id',
        type: dynamodb.AttributeType.STRING,
      },
    });

    // CustomNodeTypes: node_type_id + version (declaration JSON per version;
    // prior versions retained, 14.1).
    this.customNodeTypesTable = new dynamodb.Table(this, 'CustomNodeTypesTable', {
      tableName: 'dda-portal-custom-node-types',
      partitionKey: {
        name: 'node_type_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'version',
        type: dynamodb.AttributeType.NUMBER,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.customNodeTypesTable.addGlobalSecondaryIndex({
      indexName: 'usecase-node-types-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'node_type_id',
        type: dynamodb.AttributeType.STRING,
      },
    });

    // ModuleIndexCache: single cache_key item ('gst-modules') with a 24 h TTL
    // (Requirement 6.4).
    this.moduleIndexCacheTable = new dynamodb.Table(this, 'ModuleIndexCacheTable', {
      tableName: 'dda-portal-module-index-cache',
      partitionKey: {
        name: 'cache_key',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // SimulationRuns (Plugin_Simulator run records, Requirement 7).
    this.simulationRunsTable = new dynamodb.Table(this, 'SimulationRunsTable', {
      tableName: 'dda-portal-simulation-runs',
      partitionKey: {
        name: 'run_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.simulationRunsTable.addGlobalSecondaryIndex({
      indexName: 'usecase-runs-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'started_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    // NodeGenSessions: TTL'd Bedrock scaffold-generation chat sessions
    // (mirrors the workflow chat sessions table).
    this.nodeGenSessionsTable = new dynamodb.Table(this, 'NodeGenSessionsTable', {
      tableName: 'dda-portal-node-gen-sessions',
      partitionKey: {
        name: 'session_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ------------------------------------------------------------------
    // KMS asymmetric signing key (ECDSA P-256) - Requirements 3.3, 10.4.
    // Sign() on the SHA-256 digest after a successful build; Verify() in the
    // Component_Packager before inclusion. The private key never leaves KMS.
    // ------------------------------------------------------------------
    this.pluginSigningKey = new kms.Key(this, 'PluginSigningKey', {
      alias: 'dda-portal/plugin-signing',
      description:
        'DDA portal Plugin_Artifact signing key (ECDSA P-256, sign/verify of SHA-256 digests)',
      keySpec: kms.KeySpec.ECC_NIST_P256,
      keyUsage: kms.KeyUsage.SIGN_VERIFY,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ------------------------------------------------------------------
    // Per-arch plugin build images. One ECR repository, tagged per
    // Target_Architecture (x86_64, x86_64_nvidia, arm64_jp4/jp5/jp6). The
    // images are built/pushed out of band (like the test-sandbox image):
    //   - x86_64:        Ubuntu 22.04 + GStreamer 1.20 (matches the sandbox)
    //   - x86_64_nvidia: same base + CUDA toolkit + NVIDIA GStreamer runtime
    //                    headers
    //   - arm64_jp4/5/6: JetPack cross-build images pinning the L4T +
    //                    DeepStream SDK version matching each JetPack release
    // The tag defaults to the architecture name; override the tag suffix via
    // the CDK context value `pluginBuildImageTag` (tag = `<arch>-<suffix>`).
    // ------------------------------------------------------------------
    this.buildImageRepository = new ecr.Repository(this, 'PluginBuildImageRepository', {
      repositoryName: 'dda-plugin-build',
      imageScanOnPush: true,
    });

    const imageTagSuffix: string = this.node.tryGetContext('pluginBuildImageTag') || '';
    const imageTagFor = (arch: string) =>
      imageTagSuffix ? `${arch}-${imageTagSuffix}` : arch;

    // ------------------------------------------------------------------
    // Per-arch CodeBuild projects (Requirements 3.1, 3.2, 5.2).
    //
    // Isolation (3.2): CodeBuild runs each build in a fresh container; the
    // per-project role below is scoped to exactly the plugin-sources read
    // prefix and this architecture's staging/Plugin_Library write prefixes -
    // no Use_Case account credentials, no other tables/buckets. No VpcConfig
    // is attached, so a build has no network route to portal internals;
    // outbound internet remains available for source-declared dependency
    // fetches during import builds.
    //
    // plugin_builds.py StartBuild()s with a sourceLocationOverride pointing at
    // the record's plugin-sources/{usecase}/{plugin}/{version}/ tree and the
    // environment variables consumed by the image's build entrypoint.
    // ------------------------------------------------------------------
    this.buildProjects = {};
    const buildProjectArns: string[] = [];

    for (const arch of PLUGIN_BUILD_ARCHITECTURES) {
      const role = new iam.Role(this, `PluginBuildRole${arch}`, {
        assumedBy: new iam.ServicePrincipal('codebuild.amazonaws.com'),
        description:
          `Plugin build role (${arch}): plugin-sources read + ${arch} staging/library write only`,
      });

      // Source read: the plugin source trees only.
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['s3:GetObject', 's3:GetObjectVersion'],
        resources: [`${bucket.bucketArn}/${PLUGIN_SOURCES_PREFIX}/*`],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['s3:ListBucket'],
        resources: [bucket.bucketArn],
        conditions: {
          StringLike: {
            's3:prefix': [
              `${PLUGIN_SOURCES_PREFIX}/*`,
              `${PLUGIN_STAGING_PREFIX}/*/${arch}/*`,
            ],
          },
        },
      }));

      // Staging write + promote to the per-Use_Case custom Plugin_Library
      // prefix for THIS architecture only (`{prefix}/{usecase}/{arch}/...`).
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['s3:PutObject', 's3:GetObject'],
        resources: [`${bucket.bucketArn}/${PLUGIN_STAGING_PREFIX}/*/${arch}/*`],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['s3:PutObject'],
        resources: [`${bucket.bucketArn}/${PLUGIN_LIBRARY_CUSTOM_PREFIX}/*/${arch}/*`],
      }));

      // Sign the SHA-256 digest of the built artifact (3.3).
      this.pluginSigningKey.grant(role, 'kms:Sign', 'kms:DescribeKey');

      // The role is passed to the project as withoutPolicyUpdates(): CDK's
      // automatic grant for an S3 source includes bucket-wide s3:List*,
      // which would exceed the exactly-prefix-scoped role required by 3.2.
      // The grants CDK would otherwise auto-add are declared explicitly:
      // CloudWatch Logs for this project's log group and image pull on the
      // build-image repository.
      const projectName = `dda-plugin-build-${arch}`;
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [
          `arn:aws:logs:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:log-group:/aws/codebuild/${projectName}`,
          `arn:aws:logs:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:log-group:/aws/codebuild/${projectName}:*`,
        ],
      }));
      this.buildImageRepository.grantPull(role);

      const project = new codebuild.Project(this, `PluginBuild${arch}`, {
        projectName,
        description:
          `Custom node plugin build for ${arch} (fresh container per build, ` +
          'prefix-scoped role, no VPC access to portal internals)',
        role: role.withoutPolicyUpdates(),
        source: codebuild.Source.s3({
          bucket,
          // Placeholder path; every StartBuild overrides the source location
          // with the plugin version's plugin-sources/... prefix.
          path: `${PLUGIN_SOURCES_PREFIX}/`,
        }),
        environment: {
          // The JetPack images are native arm64 builds from NVIDIA L4T
          // bases, so the arm64_jp* projects run on ARM CodeBuild fleets;
          // the x86_64 images run on the default Linux (x86_64) fleet.
          buildImage: arch.startsWith('arm64')
            ? codebuild.LinuxArmBuildImage.fromEcrRepository(
                this.buildImageRepository,
                imageTagFor(arch),
              )
            : codebuild.LinuxBuildImage.fromEcrRepository(
                this.buildImageRepository,
                imageTagFor(arch),
              ),
          computeType: codebuild.ComputeType.LARGE,
          privileged: false,
        },
        environmentVariables: {
          TARGET_ARCH: { value: arch },
          ARTIFACTS_BUCKET: { value: bucket.bucketName },
          PLUGIN_STAGING_PREFIX: { value: PLUGIN_STAGING_PREFIX },
          PLUGIN_LIBRARY_CUSTOM_PREFIX: { value: PLUGIN_LIBRARY_CUSTOM_PREFIX },
          SIGNING_KEY_ARN: { value: this.pluginSigningKey.keyArn },
        },
        buildSpec: codebuild.BuildSpec.fromObject({
          version: '0.2',
          phases: {
            build: {
              commands: [
                // The per-arch build image ships /usr/local/bin/dda-plugin-build:
                // meson/autotools configure + build for $TARGET_ARCH (honoring
                // the PLUGIN_TARGETS selection for plugin-set imports), upload
                // of the built .so(s) to s3://$ARTIFACTS_BUCKET/
                // $PLUGIN_STAGING_PREFIX/$USECASE_ID/$TARGET_ARCH/, a
                // best-effort kms sign of the SHA-256 digest with
                // $SIGNING_KEY_ARN (tolerant to absence; plugin_builds.py
                // re-signs the promoted artifact authoritatively), then
                // promotion to $PLUGIN_LIBRARY_CUSTOM_PREFIX/$USECASE_ID/
                // $TARGET_ARCH/$PLUGIN_NAME.so. USECASE_ID / PLUGIN_ID /
                // PLUGIN_VERSION / PLUGIN_NAME / PLUGIN_TARGETS arrive as
                // StartBuild environment overrides from plugin_builds.py.
                'dda-plugin-build',
              ],
            },
          },
        }),
        timeout: cdk.Duration.minutes(30),
      });

      this.buildProjects[arch] = project;
      buildProjectArns.push(project.projectArn);
    }

    // ------------------------------------------------------------------
    // Lightweight fetch project (repository import transport, Requirement 4.1).
    // Clones REPO_URL at REVISION (default branch when empty) and syncs the
    // tree (without .git) to the DEST_PREFIX under plugin-sources/. The role
    // can only write the plugin-sources prefix.
    // ------------------------------------------------------------------
    const fetchRole = new iam.Role(this, 'PluginFetchRole', {
      assumedBy: new iam.ServicePrincipal('codebuild.amazonaws.com'),
      description: 'Plugin repository fetch role (plugin-sources write only)',
    });
    fetchRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['s3:PutObject', 's3:GetObject'],
      resources: [`${bucket.bucketArn}/${PLUGIN_SOURCES_PREFIX}/*`],
    }));
    fetchRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['s3:ListBucket'],
      resources: [bucket.bucketArn],
      conditions: {
        StringLike: { 's3:prefix': [`${PLUGIN_SOURCES_PREFIX}/*`] },
      },
    }));

    this.fetchProject = new codebuild.Project(this, 'PluginFetchProject', {
      projectName: 'dda-plugin-fetch',
      description:
        'Clones a public plugin repository at a revision and syncs the tree to plugin-sources/',
      role: fetchRole,
      environment: {
        buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        computeType: codebuild.ComputeType.SMALL,
      },
      environmentVariables: {
        ARTIFACTS_BUCKET: { value: bucket.bucketName },
        // REPO_URL / REVISION / DEST_PREFIX arrive as StartBuild overrides.
        REPO_URL: { value: '' },
        REVISION: { value: '' },
        DEST_PREFIX: { value: '' },
      },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: {
          build: {
            commands: [
              'test -n "$REPO_URL" && test -n "$DEST_PREFIX"',
              'git clone "$REPO_URL" /tmp/repo',
              'if [ -n "$REVISION" ]; then git -C /tmp/repo checkout "$REVISION"; fi',
              'rm -rf /tmp/repo/.git',
              'aws s3 sync /tmp/repo/ "s3://$ARTIFACTS_BUCKET/$DEST_PREFIX/"',
            ],
          },
        },
      }),
      timeout: cdk.Duration.minutes(10),
    });

    // ------------------------------------------------------------------
    // Lambda layers. This stack builds its own layer versions from the same
    // assets as the ComputeStack (the TestRunnerStack does the same for
    // workflow_core) so no reference back into the ComputeStack exists -
    // the API route nested stack under the ApiGatewayStack references THESE
    // handlers, keeping the cross-stack dependency one-directional.
    // ------------------------------------------------------------------
    const sharedLayer = new lambda.LayerVersion(this, 'NodeDesignerSharedLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/shared')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'Shared utilities for the Node_Designer Lambda functions',
    });

    const workflowCoreLayer = new lambda.LayerVersion(this, 'NodeDesignerWorkflowCoreLayer', {
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
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'workflow_core shared package for the Node_Designer Lambda functions',
    });

    // ------------------------------------------------------------------
    // Node_Designer Lambda handlers.
    // Base role: the node-designer tables, the RBAC/audit tables, and the
    // portal artifacts bucket. Handler-specific grants are added per function.
    // ------------------------------------------------------------------
    const createHandlerRole = (name: string) => {
      const role = new iam.Role(this, `${name}Role`, {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        ],
      });

      this.pluginRecordsTable.grantReadWriteData(role);
      this.customNodeTypesTable.grantReadWriteData(role);
      this.moduleIndexCacheTable.grantReadWriteData(role);
      this.simulationRunsTable.grantReadWriteData(role);
      this.nodeGenSessionsTable.grantReadWriteData(role);

      props.useCasesTable.grantReadData(role);
      props.userRolesTable.grantReadData(role);
      props.settingsTable.grantReadData(role);
      props.auditLogTable.grantWriteData(role);

      bucket.grantReadWrite(role);

      return role;
    };

    const lambdaEnvironment: { [key: string]: string } = {
      PLUGIN_RECORDS_TABLE: this.pluginRecordsTable.tableName,
      CUSTOM_NODE_TYPES_TABLE: this.customNodeTypesTable.tableName,
      MODULE_INDEX_CACHE_TABLE: this.moduleIndexCacheTable.tableName,
      SIMULATION_RUNS_TABLE: this.simulationRunsTable.tableName,
      NODE_GEN_SESSIONS_TABLE: this.nodeGenSessionsTable.tableName,
      USECASES_TABLE: props.useCasesTable.tableName,
      USER_ROLES_TABLE: props.userRolesTable.tableName,
      AUDIT_LOG_TABLE: props.auditLogTable.tableName,
      SETTINGS_TABLE: props.settingsTable.tableName,
      WORKFLOWS_TABLE: props.workflowsTable.tableName,
      WORKFLOW_VERSIONS_TABLE: props.workflowVersionsTable.tableName,
      TEST_DATASETS_TABLE: props.testDatasetsTable.tableName,
      PORTAL_ARTIFACTS_BUCKET: bucket.bucketName,
      PLUGIN_SOURCES_PREFIX,
      PLUGIN_STAGING_PREFIX,
      PLUGIN_LIBRARY_CUSTOM_PREFIX,
      PLUGIN_SIMULATIONS_PREFIX,
      PLUGIN_SIGNING_KEY_ARN: this.pluginSigningKey.keyArn,
      PORTAL_ACCOUNT_ID: cdk.Aws.ACCOUNT_ID,
    };

    const buildProjectsJson = cdk.Stack.of(this).toJsonString(
      Object.fromEntries(
        PLUGIN_BUILD_ARCHITECTURES.map((arch) => [arch, this.buildProjects[arch].projectName]),
      ),
    );

    // plugin_records.py - Plugin_Record CRUD, lifecycle transitions with
    // guards, security review decisions, provenance display.
    this.pluginRecordsHandler = new lambda.Function(this, 'PluginRecordsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'plugin_records.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createHandlerRole('PluginRecords'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-10-plugin-records',
      },
      layers: [sharedLayer, workflowCoreLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // plugin_importer.py - repository import orchestration (fetch project) +
    // Module_Listing fetch/parse/cache with classification stamping.
    this.pluginImporterHandler = new lambda.Function(this, 'PluginImporterHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'plugin_importer.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createHandlerRole('PluginImporter'),
      environment: {
        ...lambdaEnvironment,
        FETCH_PROJECT_NAME: this.fetchProject.projectName,
        // The importer starts per-arch builds directly on the select-plugins
        // and adjust-revision (tree reuse) paths via
        // plugin_builds.start_queued_builds, which resolves project names
        // from BUILD_PROJECTS_JSON in ITS OWN Lambda environment - without
        // it every queued arch is skipped as "unconfigured" and sits queued
        // forever (imported-plugin-revision-adjustment-fix follow-up).
        BUILD_PROJECTS_JSON: buildProjectsJson,
        CODE_VERSION: '2026-02-10-plugin-importer',
      },
      layers: [sharedLayer, workflowCoreLayer],
      timeout: cdk.Duration.seconds(120),
    });
    this.pluginImporterHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['codebuild:StartBuild', 'codebuild:BatchGetBuilds'],
      // The fetch project plus the per-arch build projects (start_queued_builds
      // runs inside this Lambda on the select-plugins / adjust-revision paths).
      resources: [this.fetchProject.projectArn, ...buildProjectArns],
    }));

    // node_generator.py - Bedrock Converse scaffold-generation sessions
    // (mirrors workflow_generator.py: settings-table Bedrock_Configuration,
    // clamped <= 240 s timeout, forced tool use).
    this.nodeGeneratorHandler = new lambda.Function(this, 'NodeGeneratorHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'node_generator.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createHandlerRole('NodeGenerator'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-17-node-generator-async',
      },
      layers: [sharedLayer, workflowCoreLayer],
      // Bedrock invocation timeout is configurable up to 240s; leave headroom
      timeout: cdk.Duration.seconds(270),
    });
    // Async start/poll generation: the start routes Event-invoke this same
    // function (the worker path runs the Bedrock turn outside the API
    // Gateway 29 s integration cap), so the handler needs the self
    // lambda:InvokeFunction permission. The statement lives in a separate
    // policy (not the role's default policy) because the Function resource
    // depends on its role's default policy: referencing the function ARN
    // from there would be a dependency cycle.
    new iam.Policy(this, 'NodeGeneratorSelfInvokePolicy', {
      roles: [this.nodeGeneratorHandler.role as iam.Role],
      statements: [new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['lambda:InvokeFunction'],
        resources: [this.nodeGeneratorHandler.functionArn],
      })],
    });
    // Same Bedrock grant shape as the workflow generator: the model id is
    // runtime configuration, so cover foundation models + inference profiles.
    this.nodeGeneratorHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
      ],
      resources: [
        'arn:aws:bedrock:*::foundation-model/*',
        `arn:aws:bedrock:*:${cdk.Aws.ACCOUNT_ID}:inference-profile/*`,
      ],
    }));

    // plugin_components.py - automatic Plugin_Component packaging
    // (dda.plugin.{pluginId}) in the Use_Case account (Requirements 16.1,
    // 16.7). Registration happens through the assumed DDAPortalAccessRole in
    // the trusted Use_Case accounts, exactly like workflow packaging.
    this.pluginComponentsHandler = new lambda.Function(this, 'PluginComponentsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'plugin_components.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createHandlerRole('PluginComponents'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-10-plugin-components',
      },
      layers: [sharedLayer, workflowCoreLayer],
      timeout: cdk.Duration.seconds(300),
      memorySize: 1024,
    });
    this.pluginComponentsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['sts:AssumeRole'],
      resources: props.trustedUseCaseAccountIds.map(
        (accountId) => `arn:aws:iam::${accountId}:role/DDAPortalAccessRole`
      ),
    }));
    // Component artifacts stage to the Use_Case's data bucket
    // (plugins/staging/... then plugins/components/...) before Greengrass
    // registration. Data buckets are user-named and resolved at runtime, so
    // the object-level grant mirrors the ComputeStack data-plane pattern
    // (all buckets by default; the control plane is never granted). Same-
    // account Use_Cases exercise this role directly — cross-account ones go
    // through the assumed DDAPortalAccessRole above.
    this.pluginComponentsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:ListBucket'],
      resources: ['arn:aws:s3:::*'],
    }));
    // Same-account Use_Cases register the dda.plugin.* Greengrass component
    // version with this role directly (cross-account ones assume
    // DDAPortalAccessRole). Mirrors the ComputeStack packaging grant.
    this.pluginComponentsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'greengrass:CreateComponentVersion',
        'greengrass:DeleteComponent',
        'greengrass:DescribeComponent',
        'greengrass:GetComponent',
        'greengrass:ListComponents',
        'greengrass:ListComponentVersions',
        'greengrass:ListTagsForResource',
        'greengrass:TagResource',
      ],
      resources: ['*'],
    }));

    // plugin_builds.py - build orchestration + EventBridge result recording
    // (per-arch {s3Key, checksum, signature, buildStatus, logTail}); triggers
    // plugin_components.py when a version's builds settle with >= 1 success.
    this.pluginBuildsHandler = new lambda.Function(this, 'PluginBuildsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'plugin_builds.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createHandlerRole('PluginBuilds'),
      environment: {
        ...lambdaEnvironment,
        BUILD_PROJECTS_JSON: buildProjectsJson,
        PLUGIN_COMPONENTS_FUNCTION_NAME: this.pluginComponentsHandler.functionName,
        CODE_VERSION: '2026-02-10-plugin-builds',
      },
      layers: [sharedLayer, workflowCoreLayer],
      timeout: cdk.Duration.seconds(120),
    });
    this.pluginBuildsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['codebuild:StartBuild', 'codebuild:BatchGetBuilds'],
      resources: buildProjectArns,
    }));
    // Failed builds store the CloudWatch log tail on the Plugin_Record (3.4).
    this.pluginBuildsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['logs:GetLogEvents', 'logs:FilterLogEvents', 'logs:DescribeLogStreams'],
      resources: [
        `arn:aws:logs:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:log-group:/aws/codebuild/dda-plugin-build-*`,
        `arn:aws:logs:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:log-group:/aws/codebuild/dda-plugin-build-*:*`,
      ],
    }));
    // Prebuilt-binary uploads and post-build recording sign with the portal
    // key; result verification uses the public-key operations (3.3, 3.6).
    this.pluginSigningKey.grant(
      this.pluginBuildsHandler,
      'kms:Sign',
      'kms:Verify',
      'kms:GetPublicKey',
      'kms:DescribeKey',
    );
    this.pluginComponentsHandler.grantInvoke(this.pluginBuildsHandler);

    // plugin_simulator.py - simulator run start (x86_64-artifact guard, 409
    // describing the missing build, 7.5), status/results (7.3), and the
    // Guard/Prepare/Collect/record_timeout/record_failure steps invoked by
    // the Plugin_Simulator state machine below (task 8.2).
    this.pluginSimulatorHandler = new lambda.Function(this, 'PluginSimulatorHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'plugin_simulator.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createHandlerRole('PluginSimulator'),
      environment: {
        ...lambdaEnvironment,
        SIMULATOR_STATE_MACHINE_ARN: simulatorStateMachineArn,
        CODE_VERSION: '2026-02-14-plugin-simulator',
      },
      layers: [sharedLayer, workflowCoreLayer],
      timeout: cdk.Duration.seconds(120),
    });
    props.testDatasetsTable.grantReadData(this.pluginSimulatorHandler);
    // Start simulator executions and sync run status from them. The ARN is
    // the composed fixed-name one (see SIMULATOR_STATE_MACHINE_NAME above).
    this.pluginSimulatorHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['states:StartExecution'],
      resources: [simulatorStateMachineArn],
    }));
    this.pluginSimulatorHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['states:DescribeExecution', 'states:StopExecution'],
      resources: [
        `arn:aws:states:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}` +
          `:execution:${SIMULATOR_STATE_MACHINE_NAME}:*`,
      ],
    }));

    // custom_node_types.py - Custom_Node_Type registration, versioning,
    // deprecation, and reference-checked removal (WorkflowVersions scan).
    this.customNodeTypesHandler = new lambda.Function(this, 'CustomNodeTypesHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'custom_node_types.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createHandlerRole('CustomNodeTypes'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-10-custom-node-types',
      },
      layers: [sharedLayer, workflowCoreLayer],
      timeout: cdk.Duration.seconds(60),
    });
    props.workflowsTable.grantReadData(this.customNodeTypesHandler);
    props.workflowVersionsTable.grantReadWriteData(this.customNodeTypesHandler);
    // Removal deletes the plugin's Plugin_Component versions from the Use_Case
    // Greengrass registry (14.4) via the assumed DDAPortalAccessRole.
    this.customNodeTypesHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['sts:AssumeRole'],
      resources: props.trustedUseCaseAccountIds.map(
        (accountId) => `arn:aws:iam::${accountId}:role/DDAPortalAccessRole`
      ),
    }));

    // ------------------------------------------------------------------
    // EventBridge rule: CodeBuild build results -> plugin_builds.py
    // (idempotent on build id; per-arch artifact/status recording, 3.4).
    // ------------------------------------------------------------------
    const buildResultsRule = new events.Rule(this, 'PluginBuildResultsRule', {
      ruleName: 'dda-portal-plugin-build-results',
      description: 'Delivers plugin CodeBuild build results to plugin_builds.py',
      eventPattern: {
        source: ['aws.codebuild'],
        detailType: ['CodeBuild Build State Change'],
        detail: {
          'build-status': ['SUCCEEDED', 'FAILED', 'FAULT', 'STOPPED', 'TIMED_OUT'],
          'project-name': [
            ...PLUGIN_BUILD_ARCHITECTURES.map((arch) => `dda-plugin-build-${arch}`),
            'dda-plugin-fetch',
          ],
        },
      },
    });
    buildResultsRule.addTarget(new targets.LambdaFunction(this.pluginBuildsHandler));

    // ------------------------------------------------------------------
    // Plugin_Simulator state machine (task 8.2, Requirement 7):
    //   Guard -> Prepare -> RunSandbox (Fargate) -> Collect
    //
    // Isolation (7.2): the sandbox runs the existing test-sandbox image
    // (HARNESS_MODE=simulate, task 8.1) in an isolated subnet with no
    // NAT/internet gateway — the same network shape as the workflow test
    // sandbox. The task role below is limited to the run's S3 prefix
    // (plugin-simulations/...): read on the staged run inputs, write for the
    // incremental results flush. It has NO Plugin_Library write path, no
    // other Use_Case data, and no other portal tables or workloads.
    // ------------------------------------------------------------------
    const simulatorVpc = new ec2.Vpc(this, 'SimulatorSandboxVpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: 'simulator-isolated',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
    });
    simulatorVpc.addGatewayEndpoint('SimulatorS3Endpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });
    simulatorVpc.addInterfaceEndpoint('SimulatorEcrApiEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECR,
    });
    simulatorVpc.addInterfaceEndpoint('SimulatorEcrDockerEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
    });
    simulatorVpc.addInterfaceEndpoint('SimulatorCloudWatchLogsEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
    });

    // No inbound access at all; outbound only reaches the VPC endpoints
    // because the subnets are isolated.
    const simulatorSecurityGroup = new ec2.SecurityGroup(this, 'SimulatorSandboxSecurityGroup', {
      vpc: simulatorVpc,
      description: 'Plugin simulator sandbox Fargate tasks (no inbound; isolated subnets)',
      allowAllOutbound: true,
    });

    // The simulator reuses the workflow test sandbox image (built by the
    // workflow-manager task 11.3, pushed to the TestRunnerStack repository);
    // HARNESS_MODE=simulate dispatches to the single-plugin harness mode
    // (task 8.1). Same image tag context value as the test runner.
    const sandboxRepository = ecr.Repository.fromRepositoryName(
      this, 'SimulatorSandboxRepository', 'dda-workflow-test-sandbox',
    );
    const sandboxImageTag: string =
      this.node.tryGetContext('testSandboxImageTag') || 'latest';

    const simulatorCluster = new ecs.Cluster(this, 'SimulatorSandboxCluster', {
      vpc: simulatorVpc,
      clusterName: 'dda-plugin-simulator-sandbox',
    });

    const simulatorLogGroup = new logs.LogGroup(this, 'SimulatorSandboxLogGroup', {
      logGroupName: '/dda-portal/plugin-simulator-sandbox',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Task role (7.2): exactly the run-prefix data-plane access the simulate
    // harness needs. Read covers the staged run inputs (dataset copy and
    // plugin .so staged under plugin-simulations/... by the Prepare step);
    // write covers the incremental results/frames flush under the same
    // prefix. Deliberately NO workflow-plugins/** (Plugin_Library) write
    // path, no plugin-sources, no DynamoDB, no Greengrass/IoT.
    const simulatorTaskRole = new iam.Role(this, 'SimulatorSandboxTaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description:
        'Plugin simulator sandbox task role (plugin-simulations/ prefix only; '
        + 'no Plugin_Library write, no other Use_Case data)',
    });
    simulatorTaskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject', 's3:PutObject'],
      resources: [`${bucket.bucketArn}/${PLUGIN_SIMULATIONS_PREFIX}/*`],
    }));
    simulatorTaskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['s3:ListBucket'],
      resources: [bucket.bucketArn],
      conditions: {
        StringLike: { 's3:prefix': [`${PLUGIN_SIMULATIONS_PREFIX}/*`] },
      },
    }));

    const simulatorTaskDefinition = new ecs.FargateTaskDefinition(this, 'SimulatorTaskDefinition', {
      cpu: 2048,
      memoryLimitMiB: 8192,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.X86_64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
      taskRole: simulatorTaskRole,
    });

    const simulatorContainer = simulatorTaskDefinition.addContainer('SimulatorSandbox', {
      containerName: 'sandbox',
      image: ecs.ContainerImage.fromEcrRepository(sandboxRepository, sandboxImageTag),
      logging: ecs.LogDrivers.awsLogs({
        logGroup: simulatorLogGroup,
        streamPrefix: 'simulation-run',
      }),
      environment: {
        // Dispatches the sandbox entrypoint to the single-plugin simulate
        // harness (task 8.1) instead of the workflow test harness.
        HARNESS_MODE: 'simulate',
      },
    });

    // Guard/Prepare/Collect and the failure recorders run in
    // plugin_simulator.py (step dispatch on the 'step' payload field),
    // mirroring the workflow_test_steps.py pattern.
    const invokeSimulatorStep = (id: string, step: string, resultPath?: string) =>
      new tasks.LambdaInvoke(this, id, {
        lambdaFunction: this.pluginSimulatorHandler,
        payload: sfn.TaskInput.fromObject({
          step,
          'input.$': '$',
        }),
        payloadResponseOnly: true,
        resultPath: resultPath ?? sfn.JsonPath.DISCARD,
        retryOnServiceExceptions: true,
      });

    // Guard (7.5): refuse when the Plugin_Record version has no successful
    // x86_64 Plugin_Artifact. The start endpoint already rejects such runs
    // with a 409 before any execution exists; the in-machine guard re-checks
    // so a record changed between start and execution still cannot run.
    const guardFailed = new sfn.Fail(this, 'SimulationGuardFailed', {
      error: 'SimulationGuardFailed',
      cause: 'The Plugin_Record version has no successfully built x86_64 '
        + 'Plugin_Artifact; simulation requires a successful x86_64 build '
        + 'and the run was marked failed without executing',
    });
    const simulationTimedOut = new sfn.Fail(this, 'SimulationTimedOut', {
      error: 'SimulationTimedOut',
      cause: 'The simulation run exceeded the 5 minute limit; the sandbox '
        + 'task was stopped, the run marked failed-with-timeout, and the '
        + 'partial results flushed before termination retained',
    });
    const simulationFailed = new sfn.Fail(this, 'SimulationFailed', {
      error: 'SimulationFailed',
      cause: 'The simulation run failed; the run was marked failed and the '
        + 'partial results flushed before the failure retained',
    });

    const guard = invokeSimulatorStep('SimulationGuard', 'guard', '$.guard');
    const prepare = invokeSimulatorStep('SimulationPrepare', 'prepare', '$.prepare');
    const collect = invokeSimulatorStep('SimulationCollect', 'collect');

    // Timeout/failure recorders only update the SimulationRuns item; the
    // incrementally flushed results in S3 stay untouched (7.6, 7.7).
    const recordTimeout = invokeSimulatorStep('SimulationRecordTimeout', 'record_timeout');
    const recordSandboxFailure = invokeSimulatorStep('SimulationRecordFailure', 'record_failure');
    const recordInternalFailure = invokeSimulatorStep('SimulationRecordInternalFailure', 'record_failure');
    recordTimeout.next(simulationTimedOut);
    recordSandboxFailure.next(simulationFailed);
    recordInternalFailure.next(simulationFailed);

    // RunSandbox (7.2): HARNESS_MODE=simulate against the env contract of
    // test-sandbox/harness/simulate.py. The 5-minute task timeout makes Step
    // Functions stop the Fargate task on expiry (7.7).
    const runSimulation = new tasks.EcsRunTask(this, 'SimulationRunSandbox', {
      integrationPattern: sfn.IntegrationPattern.RUN_JOB,
      cluster: simulatorCluster,
      taskDefinition: simulatorTaskDefinition,
      launchTarget: new tasks.EcsFargateLaunchTarget({
        platformVersion: ecs.FargatePlatformVersion.LATEST,
      }),
      subnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      securityGroups: [simulatorSecurityGroup],
      assignPublicIp: false,
      containerOverrides: [
        {
          containerDefinition: simulatorContainer,
          environment: [
            { name: 'SIMULATION_RUN_ID', value: sfn.JsonPath.stringAt('$.run_id') },
            { name: 'ARTIFACTS_BUCKET', value: sfn.JsonPath.stringAt('$.artifacts_bucket') },
            {
              // Staged by the Prepare step under the run's prefix (the
              // Test_Dataset copy or the uploaded sample frames).
              name: 'DATASET_S3_PREFIX',
              value: sfn.JsonPath.stringAt('$.prepare.dataset_s3_prefix'),
            },
            { name: 'RESULTS_S3_KEY', value: sfn.JsonPath.stringAt('$.results_s3_key') },
            {
              // The plugin .so staged from the Plugin_Library into the run's
              // prefix by the Prepare step.
              name: 'PLUGIN_S3_KEY',
              value: sfn.JsonPath.stringAt('$.prepare.plugin_s3_key'),
            },
            { name: 'ELEMENT_FACTORY', value: sfn.JsonPath.stringAt('$.element_factory') },
            {
              // Declared parameter values for this run (7.4 re-runs pass
              // changed values). Pre-serialized by the start endpoint since
              // env values must be strings.
              name: 'ELEMENT_PARAMETERS',
              value: sfn.JsonPath.stringAt('$.parameters_json'),
            },
          ],
        },
      ],
      taskTimeout: sfn.Timeout.duration(cdk.Duration.minutes(5)),
      resultPath: sfn.JsonPath.DISCARD,
    });

    // Timeout stops the task and records failed-with-timeout (7.7); any
    // other sandbox failure records failed, retaining partials (7.6).
    runSimulation.addCatch(recordTimeout, {
      errors: ['States.Timeout'],
      resultPath: '$.errorInfo',
    });
    runSimulation.addCatch(recordSandboxFailure, {
      errors: ['States.ALL'],
      resultPath: '$.errorInfo',
    });

    // Unexpected guard/prepare/collect Lambda failures also mark the run
    // failed rather than leaving it stuck in 'running'.
    guard.addCatch(recordInternalFailure, {
      errors: ['States.ALL'],
      resultPath: '$.errorInfo',
    });
    prepare.addCatch(recordInternalFailure, {
      errors: ['States.ALL'],
      resultPath: '$.errorInfo',
    });
    collect.addCatch(recordInternalFailure, {
      errors: ['States.ALL'],
      resultPath: '$.errorInfo',
    });

    const simulatorDefinition = guard.next(
      new sfn.Choice(this, 'SimulationGuardPassed')
        .when(
          sfn.Condition.booleanEquals('$.guard.ok', true),
          prepare.next(
            runSimulation.next(
              collect.next(new sfn.Succeed(this, 'SimulationFinished')),
            ),
          ),
        )
        .otherwise(guardFailed),
    );

    this.simulatorStateMachine = new sfn.StateMachine(this, 'SimulatorStateMachine', {
      stateMachineName: SIMULATOR_STATE_MACHINE_NAME,
      definitionBody: sfn.DefinitionBody.fromChainable(simulatorDefinition),
      // Upper bound for the whole run; the 5-minute pipeline execution limit
      // (7.7) is enforced by the RunSandbox task timeout above.
      timeout: cdk.Duration.minutes(15),
      logs: {
        destination: new logs.LogGroup(this, 'SimulatorStateMachineLogGroup', {
          logGroupName: '/dda-portal/plugin-simulator',
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        level: sfn.LogLevel.ERROR,
      },
    });

    // ------------------------------------------------------------------
    // API Gateway routes: registered against the imported portal Rest API in
    // a nested stack (see class comment for the resource-limit rationale).
    // ------------------------------------------------------------------
    new NodeDesignerApiStack(this, 'NodeDesignerApi', {
      restApiId: props.restApiId,
      restApiRootResourceId: props.restApiRootResourceId,
      stageName: props.apiStageName,
      userPool: props.userPool,
      pluginRecordsHandler: this.pluginRecordsHandler,
      pluginImporterHandler: this.pluginImporterHandler,
      nodeGeneratorHandler: this.nodeGeneratorHandler,
      pluginBuildsHandler: this.pluginBuildsHandler,
      pluginComponentsHandler: this.pluginComponentsHandler,
      pluginSimulatorHandler: this.pluginSimulatorHandler,
      customNodeTypesHandler: this.customNodeTypesHandler,
    });

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new cdk.CfnOutput(this, 'PluginSigningKeyArn', {
      value: this.pluginSigningKey.keyArn,
      description: 'Plugin_Artifact signing key (ECDSA P-256) ARN',
    });
    new cdk.CfnOutput(this, 'PluginBuildImageRepositoryUri', {
      value: this.buildImageRepository.repositoryUri,
      description: 'ECR repository for the per-arch plugin build images (tag = architecture)',
    });
    new cdk.CfnOutput(this, 'PluginBuildProjects', {
      value: buildProjectsJson,
      description: 'Per-Target_Architecture plugin CodeBuild project names',
    });
    new cdk.CfnOutput(this, 'PluginFetchProjectName', {
      value: this.fetchProject.projectName,
      description: 'Lightweight repository-fetch CodeBuild project name',
    });
    new cdk.CfnOutput(this, 'SimulatorStateMachineArn', {
      value: this.simulatorStateMachine.stateMachineArn,
      description: 'Plugin_Simulator Step Functions state machine ARN',
    });
  }
}
