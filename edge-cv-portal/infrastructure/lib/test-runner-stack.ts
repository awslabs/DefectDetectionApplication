import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { Construct } from 'constructs';
import * as path from 'path';

export interface TestRunnerStackProps extends cdk.StackProps {
  /** TestRuns DynamoDB table (status/failure updates from the state machine). */
  testRunsTable: dynamodb.Table;
  /**
   * Portal artifacts bucket holding workflow definitions, test datasets,
   * compiled documents, and per-node test results under workflows/... prefixes.
   */
  portalArtifactsBucket: s3.Bucket;
}

/**
 * Workflow_Test_Runner infrastructure (Workflow Manager, task 11.2).
 *
 * Step Functions state machine
 *   Validate -> Compile (x86_64, simulation=true) -> RunSandbox (Fargate)
 *   -> CollectResults
 * (design section 10, Requirements 12.4, 12.12) with:
 *
 * - Validation/compilation errors short-circuiting to Fail states after the
 *   step Lambda records per-node/connection error records and marks the
 *   TestRuns item failed - the pipeline is never executed (12.4, 12.12).
 * - A 10-minute timeout on the sandbox task state; on timeout Step Functions
 *   stops the Fargate task and a handler marks the run failed-with-timeout,
 *   retaining the partial per-node results the harness already flushed to
 *   S3 (12.13).
 * - The sandbox running in an isolated subnet with no NAT/internet gateway
 *   and therefore no route to device networks; AWS access is limited to
 *   S3/DynamoDB gateway endpoints plus the ECR/CloudWatch Logs interface
 *   endpoints needed to start the task. Neither the task role nor the step
 *   Lambda has any Greengrass permission, so a test run cannot create
 *   Greengrass resources or deliver artifacts to devices (12.9).
 *
 * The sandbox container image (task 11.3) is pushed to the ECR repository
 * created here; the tag is configurable via the CDK context value
 * `testSandboxImageTag` (default `latest`).
 */
export class TestRunnerStack extends cdk.Stack {
  /** The Workflow_Test_Runner state machine workflow_testing.py starts. */
  public readonly stateMachine: sfn.StateMachine;
  /** ECR repository the sandbox image (task 11.3) is pushed to. */
  public readonly sandboxRepository: ecr.Repository;

  /**
   * Resolve availability zones at deploy time (Fn::GetAZs) instead of via a
   * synth-time context lookup. The pre-existing portal stacks synthesize
   * without AWS credentials (e.g. `cdk synth` in CI); a context lookup here
   * would make credentials a new mandatory requirement for that workflow
   * (backward compatibility, Requirement 13.2).
   */
  public get availabilityZones(): string[] {
    return [
      cdk.Fn.select(0, cdk.Fn.getAzs()),
      cdk.Fn.select(1, cdk.Fn.getAzs()),
    ];
  }

  constructor(scope: Construct, id: string, props: TestRunnerStackProps) {
    super(scope, id, props);

    const sandboxImageTag: string =
      this.node.tryGetContext('testSandboxImageTag') || 'latest';

    // ------------------------------------------------------------------
    // Isolated network (12.9): no internet/NAT gateway, so no route to
    // device networks exists. Gateway endpoints cover S3 + DynamoDB data
    // access; interface endpoints let Fargate pull the image and ship logs.
    // ------------------------------------------------------------------
    const vpc = new ec2.Vpc(this, 'TestSandboxVpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: 'sandbox-isolated',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
    });

    vpc.addGatewayEndpoint('S3Endpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });
    vpc.addGatewayEndpoint('DynamoDbEndpoint', {
      service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
    });
    vpc.addInterfaceEndpoint('EcrApiEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECR,
    });
    vpc.addInterfaceEndpoint('EcrDockerEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
    });
    vpc.addInterfaceEndpoint('CloudWatchLogsEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
    });

    // No inbound access at all; outbound only reaches the VPC endpoints
    // because the subnets are isolated.
    const sandboxSecurityGroup = new ec2.SecurityGroup(this, 'TestSandboxSecurityGroup', {
      vpc,
      description: 'Workflow test sandbox Fargate tasks (no inbound; isolated subnets)',
      allowAllOutbound: true,
    });

    // ------------------------------------------------------------------
    // ECS cluster + Fargate task definition for the sandbox container
    // (image built by task 11.3; GStreamer + DDA plugins + CPU Triton +
    // vendored workflow_core test harness).
    // ------------------------------------------------------------------
    this.sandboxRepository = new ecr.Repository(this, 'TestSandboxRepository', {
      repositoryName: 'dda-workflow-test-sandbox',
      imageScanOnPush: true,
    });

    const cluster = new ecs.Cluster(this, 'TestSandboxCluster', {
      vpc,
      clusterName: 'dda-workflow-test-sandbox',
    });

    const sandboxLogGroup = new logs.LogGroup(this, 'TestSandboxLogGroup', {
      logGroupName: '/dda-portal/workflow-test-sandbox',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Task role: exactly the data-plane access the harness needs - portal
    // artifacts S3 (dataset in, incremental results out) and the TestRuns
    // table. Deliberately NO Greengrass/IoT permissions (12.9).
    const sandboxTaskRole = new iam.Role(this, 'TestSandboxTaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'Workflow test sandbox task role (S3 + TestRuns only, no Greengrass)',
    });
    props.portalArtifactsBucket.grantReadWrite(sandboxTaskRole);
    props.testRunsTable.grantReadWriteData(sandboxTaskRole);

    const taskDefinition = new ecs.FargateTaskDefinition(this, 'TestSandboxTaskDefinition', {
      cpu: 2048,
      memoryLimitMiB: 8192,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.X86_64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
      taskRole: sandboxTaskRole,
    });

    const sandboxContainer = taskDefinition.addContainer('Sandbox', {
      containerName: 'sandbox',
      image: ecs.ContainerImage.fromEcrRepository(this.sandboxRepository, sandboxImageTag),
      logging: ecs.LogDrivers.awsLogs({
        logGroup: sandboxLogGroup,
        streamPrefix: 'test-run',
      }),
      environment: {
        TEST_RUNS_TABLE: props.testRunsTable.tableName,
        WORKFLOWS_S3_PREFIX: 'workflows',
      },
    });

    // ------------------------------------------------------------------
    // Step Lambda: Validate / Compile / CollectResults / failure recording.
    // Standalone handler that only needs workflow_core - not the shared
    // utilities layer - so its role stays minimal.
    // ------------------------------------------------------------------
    const workflowCoreLayer = new lambda.LayerVersion(this, 'TestRunnerWorkflowCoreLayer', {
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
      description: 'workflow_core shared package for the Workflow_Test_Runner step Lambda',
    });

    const stepsRole = new iam.Role(this, 'TestRunStepsRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
      description: 'Workflow test-run step Lambda role (S3 + TestRuns only, no Greengrass)',
    });
    props.portalArtifactsBucket.grantReadWrite(stepsRole);
    props.testRunsTable.grantReadWriteData(stepsRole);

    const stepsFunction = new lambda.Function(this, 'TestRunStepsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'workflow_test_steps.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: stepsRole,
      environment: {
        TEST_RUNS_TABLE: props.testRunsTable.tableName,
        PORTAL_ARTIFACTS_BUCKET: props.portalArtifactsBucket.bucketName,
        CODE_VERSION: '2025-01-25-workflow-test-steps',
      },
      layers: [workflowCoreLayer],
      timeout: cdk.Duration.seconds(120),
      memorySize: 512,
    });

    // ------------------------------------------------------------------
    // State machine: Validate -> Compile -> RunSandbox -> CollectResults
    // ------------------------------------------------------------------
    const invokeStep = (id: string, step: string, resultPath?: string) =>
      new tasks.LambdaInvoke(this, id, {
        lambdaFunction: stepsFunction,
        payload: sfn.TaskInput.fromObject({
          step,
          'input.$': '$',
        }),
        payloadResponseOnly: true,
        resultPath: resultPath ?? sfn.JsonPath.DISCARD,
        retryOnServiceExceptions: true,
      });

    // Validation/compilation error short-circuits (12.4, 12.12): the step
    // Lambda has already written the per-node/connection error records and
    // marked the TestRuns item failed before these Fail states are reached.
    const validationFailed = new sfn.Fail(this, 'ValidationFailed', {
      error: 'WorkflowValidationFailed',
      cause: 'Workflow validation reported errors; each error was recorded with its '
        + 'node/connection identifier and the pipeline was not executed',
    });
    const compilationFailed = new sfn.Fail(this, 'CompilationFailed', {
      error: 'WorkflowCompilationFailed',
      cause: 'Workflow compilation reported errors; each error was recorded with its '
        + 'node/connection identifier and the pipeline was not executed',
    });
    const timedOut = new sfn.Fail(this, 'TimedOut', {
      error: 'TestRunTimedOut',
      cause: 'Pipeline execution exceeded the 10 minute limit; the sandbox task was '
        + 'stopped, the run marked failed-with-timeout, and partial per-node '
        + 'results retained',
    });
    const sandboxFailed = new sfn.Fail(this, 'SandboxFailed', {
      error: 'TestRunFailed',
      cause: 'Test run execution failed; the run was marked failed and partial '
        + 'per-node results retained',
    });

    const validate = invokeStep('Validate', 'validate', '$.validation');
    const compile = invokeStep('Compile', 'compile', '$.compilation');
    const collectResults = invokeStep('CollectResults', 'collect', '$.collection');

    // Timeout/failure recorders keep partial results in S3 untouched and
    // only update the TestRuns item (12.10, 12.13).
    const recordTimeout = invokeStep('RecordTimeout', 'record_timeout');
    const recordSandboxFailure = invokeStep('RecordSandboxFailure', 'record_failure');
    const recordInternalFailure = invokeStep('RecordInternalFailure', 'record_failure');
    recordTimeout.next(timedOut);
    recordSandboxFailure.next(sandboxFailed);
    recordInternalFailure.next(sandboxFailed);

    // RunSandbox: the compiled pipeline executes in the Fargate sandbox
    // (12.5). The 10-minute task timeout makes Step Functions stop the
    // task on expiry (12.13).
    const runSandbox = new tasks.EcsRunTask(this, 'RunSandbox', {
      integrationPattern: sfn.IntegrationPattern.RUN_JOB,
      cluster,
      taskDefinition,
      launchTarget: new tasks.EcsFargateLaunchTarget({
        platformVersion: ecs.FargatePlatformVersion.LATEST,
      }),
      subnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      securityGroups: [sandboxSecurityGroup],
      assignPublicIp: false,
      containerOverrides: [
        {
          containerDefinition: sandboxContainer,
          environment: [
            { name: 'TEST_RUN_ID', value: sfn.JsonPath.stringAt('$.test_run_id') },
            { name: 'WORKFLOW_ID', value: sfn.JsonPath.stringAt('$.workflow_id') },
            { name: 'USECASE_ID', value: sfn.JsonPath.stringAt('$.usecase_id') },
            { name: 'ARTIFACTS_BUCKET', value: sfn.JsonPath.stringAt('$.artifacts_bucket') },
            { name: 'DATASET_S3_PREFIX', value: sfn.JsonPath.stringAt('$.dataset_s3_prefix') },
            { name: 'RESULTS_S3_KEY', value: sfn.JsonPath.stringAt('$.results_s3_key') },
            {
              name: 'COMPILED_DOCUMENT_S3_KEY',
              value: sfn.JsonPath.stringAt('$.compilation.compiled_s3_key'),
            },
            {
              // Simulated inference outcome for stubbed model inference
              // nodes (12.6). The start endpoint always pre-serializes it
              // to a JSON string field, since env values must be strings.
              name: 'SIMULATED_INFERENCE',
              value: sfn.JsonPath.stringAt('$.simulated_inference_json'),
            },
            {
              // Model staging manifest [{nodeId, modelName, s3Key}, ...]:
              // the Triton model artifacts workflow_testing.py copied into
              // the portal artifacts bucket under the run's prefix. The
              // harness unpacks them into the sandbox's Triton model
              // repository and runs real CPU inference for those nodes
              // (harness/model_staging.py). Always pre-serialized by the
              // start endpoint ("[]" when the workflow has no model
              // inference nodes). The staged copies live in the portal
              // artifacts bucket, so the task role's existing grant covers
              // them — no model-bucket or Greengrass access is added here
              // (12.9).
              name: 'STAGED_MODELS',
              value: sfn.JsonPath.stringAt('$.staged_models_json'),
            },
          ],
        },
      ],
      taskTimeout: sfn.Timeout.duration(cdk.Duration.minutes(10)),
      resultPath: sfn.JsonPath.DISCARD,
    });

    // Timeout stops the task and records failed-with-timeout (12.13);
    // any other sandbox failure records failed, retaining partials (12.10).
    runSandbox.addCatch(recordTimeout, {
      errors: ['States.Timeout'],
      resultPath: '$.errorInfo',
    });
    runSandbox.addCatch(recordSandboxFailure, {
      errors: ['States.ALL'],
      resultPath: '$.errorInfo',
    });

    // Unexpected validate/compile/collect Lambda failures also mark the
    // run failed rather than leaving it stuck in 'running'.
    validate.addCatch(recordInternalFailure, {
      errors: ['States.ALL'],
      resultPath: '$.errorInfo',
    });
    compile.addCatch(recordInternalFailure, {
      errors: ['States.ALL'],
      resultPath: '$.errorInfo',
    });
    collectResults.addCatch(recordInternalFailure, {
      errors: ['States.ALL'],
      resultPath: '$.errorInfo',
    });

    const definition = validate.next(
      new sfn.Choice(this, 'ValidationPassed')
        .when(
          sfn.Condition.booleanEquals('$.validation.ok', true),
          compile.next(
            new sfn.Choice(this, 'CompilationPassed')
              .when(
                sfn.Condition.booleanEquals('$.compilation.ok', true),
                runSandbox.next(
                  collectResults.next(new sfn.Succeed(this, 'TestRunFinished')),
                ),
              )
              .otherwise(compilationFailed),
          ),
        )
        .otherwise(validationFailed),
    );

    this.stateMachine = new sfn.StateMachine(this, 'TestRunStateMachine', {
      stateMachineName: 'dda-workflow-test-runner',
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      // Upper bound for the whole run; the 10-minute pipeline execution
      // limit (12.13) is enforced by the RunSandbox task timeout above.
      timeout: cdk.Duration.minutes(20),
      logs: {
        destination: new logs.LogGroup(this, 'TestRunStateMachineLogGroup', {
          logGroupName: '/dda-portal/workflow-test-runner',
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        level: sfn.LogLevel.ERROR,
      },
    });

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new cdk.CfnOutput(this, 'TestRunStateMachineArn', {
      value: this.stateMachine.stateMachineArn,
      description: 'Workflow_Test_Runner Step Functions state machine ARN',
    });
    new cdk.CfnOutput(this, 'TestSandboxRepositoryUri', {
      value: this.sandboxRepository.repositoryUri,
      description: 'ECR repository for the workflow test sandbox image (task 11.3)',
    });
  }
}
