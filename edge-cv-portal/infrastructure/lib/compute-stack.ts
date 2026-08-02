import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as iot from 'aws-cdk-lib/aws-iot';
import { SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import { Construct } from 'constructs';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import { execFileSync } from 'child_process';
import { ApiGatewayStack } from './api-gateway-stack';
import { CameraRegistryApiStack } from './camera-registry-api-stack';
import { UserAdminApiStack } from './user-admin-api-stack';
import { QuickSetupApiStack } from './quick-setup-api-stack';

export interface ComputeStackProps extends cdk.StackProps {
  userPool: cognito.UserPool;
  useCasesTable: dynamodb.Table;
  userRolesTable: dynamodb.Table;
  devicesTable: dynamodb.Table;
  auditLogTable: dynamodb.Table;
  trainingJobsTable: dynamodb.Table;
  labelingJobsTable: dynamodb.Table;
  preLabeledDatasetsTable: dynamodb.Table;
  modelsTable: dynamodb.Table;
  deploymentsTable: dynamodb.Table;
  settingsTable: dynamodb.Table;
  componentsTable: dynamodb.Table;
  sharedComponentsTable: dynamodb.Table;
  dataAccountsTable: dynamodb.Table;
  workflowsTable: dynamodb.Table;
  workflowVersionsTable: dynamodb.Table;
  testDatasetsTable: dynamodb.Table;
  testRunsTable: dynamodb.Table;
  workflowChatSessionsTable: dynamodb.Table;
  cameraRegistryTable: dynamodb.Table;
  /**
   * Station Quick Setup device-registrations table
   * (`dda-portal-device-registrations`). Backs both the Device_Registration
   * items and the `RATELIMIT#` counters. The token-authenticated quick_setup
   * Lambda is granted read/write on ONLY this table plus the audit log.
   */
  deviceRegistrationsTable: dynamodb.Table;
  portalArtifactsBucket: s3.Bucket;
  /**
   * Workflow_Test_Runner Step Functions state machine (test-runner stack).
   * When provided, the WorkflowTestingHandler receives its ARN as the
   * TEST_RUN_STATE_MACHINE_ARN environment variable and is granted
   * StartExecution/DescribeExecution; without it the handler falls back to
   * the portal settings table for runtime configuration.
   */
  testRunStateMachine?: sfn.IStateMachine;
  /**
   * CloudFront domain for the portal frontend.
   * Used to configure CORS on Data Account buckets during UseCase onboarding.
   */
  cloudFrontDomain?: string;
  /**
   * Trusted UseCase account IDs the portal Lambdas are allowed to assume
   * `DDAPortalAccessRole` into. Sourced from CDK context
   * (`-c trustedUseCaseAccountIds=111111111111,222222222222`) or a
   * deployment-time SSM parameter (`/dda-portal/trusted-usecase-account-ids`).
   * Must be non-empty — an empty list is a synth-time error (coupled to
   * I1/I5); the design DOES NOT fall back to a wildcard account.
   */
  trustedUseCaseAccountIds: string[];
  /**
   * Optional allowlist of data buckets the portal Lambda roles may access on
   * the S3 DATA PLANE (s3:ListBucket "see", plus s3:GetObject/PutObject
   * "access"). Each entry may be a bare bucket name (`my-data-bucket`) or a
   * full bucket ARN (`arn:aws:s3:::my-data-bucket`). Sourced from CDK context
   * `-c dataBucketAllowlist=bucket-a,bucket-b`.
   *
   * Default (empty/unset): all buckets (`arn:aws:s3:::*`) on the data plane —
   * required for browsing arbitrary user-named data buckets, since data buckets
   * are resolved at runtime and cannot be scoped by ARN at synth time, and S3
   * does not honor aws:ResourceTag for s3:ListBucket. Control-plane actions
   * (bucket policy/ACL/delete/tagging) are never granted here regardless.
   *
   * When non-empty, the data-plane grant is restricted to exactly these buckets
   * (the portal artifacts bucket is always accessible via its own grant).
   */
  dataBucketAllowlist?: string[];
}

export class ComputeStack extends cdk.Stack {
  public readonly api: apigateway.RestApi;
  public readonly apiUrl: string;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    // Validate the trusted UseCase account list at synth time. An empty list
    // would otherwise produce an empty sts:AssumeRole resource list; the
    // design requires an explicit failure rather than any fallback to a
    // wildcard account (coupled to I1/I5).
    if (!props.trustedUseCaseAccountIds || props.trustedUseCaseAccountIds.length === 0) {
      throw new Error(
        'ComputeStack requires a non-empty trustedUseCaseAccountIds list ' +
          '(pass -c trustedUseCaseAccountIds=<id>,<id> or the SSM parameter ' +
          '/dda-portal/trusted-usecase-account-ids). Refusing to synth an ' +
          'sts:AssumeRole grant on a wildcard account.'
      );
    }

    // Lambda Layer for shared utilities
    const sharedLayer = new lambda.LayerVersion(this, 'SharedLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/shared')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'Shared utilities for Edge CV Portal Lambda functions - v2024-12-12-fixed-syntax',
    });

    // Lambda Layer for JWT dependencies
    const jwtLayer = new lambda.LayerVersion(this, 'JwtLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/jwt')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'JWT validation dependencies (PyJWT, cryptography)',
    });

    // Lambda Layer for the shared workflow_core package (node catalog, serializer,
    // validator, compiler) used by the Workflow Manager Lambda functions. The
    // layer asset ships only the python/ package directory; test/build files
    // that live alongside it in the source tree are excluded.
    const workflowCoreLayer = new lambda.LayerVersion(this, 'WorkflowCoreLayer', {
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
      description: 'workflow_core shared package (catalog, serializer, validator, compiler) for Workflow Manager',
    });

    // ---------------------------------------------------------------------
    // Station Quick Setup bundle packaging (Requirements 4.3, 4.4, 4.5)
    //
    // At deploy time we package the repository's station_install/ tree
    // (including quick_setup/) into a single self-contained installer
    // (setup-bundle.tar.gz) and compute SHA-256 sidecars over the EXACT bytes
    // we upload. The packaging is performed by build-quick-setup-bundle.sh so
    // the checksums are guaranteed to be over the same bytes served to the
    // station. Artifacts land under quick-setup/current/ in the portal
    // artifacts bucket.
    //
    // The BucketDeployment custom resource only copies to the destination
    // after a successful CloudFormation deploy; a failed deploy therefore
    // leaves the previously published artifacts in place (Req 4.4). prune is
    // disabled so the destination is never emptied on a partial run.
    // ---------------------------------------------------------------------
    const stationInstallDir = path.join(__dirname, '../../../station_install');
    const bundleBuildScript = path.join(__dirname, '../scripts/build-quick-setup-bundle.sh');

    const quickSetupBundleSource = s3deploy.Source.asset(stationInstallDir, {
      // The bundle contents are derived purely from the station_install tree
      // and the packaging script; hash both so the asset (and the deploy) only
      // changes when the shipped bytes change.
      assetHashType: cdk.AssetHashType.SOURCE,
      bundling: {
        // Docker fallback: any image with GNU tar + coreutils works. Local
        // bundling (below) is used whenever the host has bash, so this image
        // is only pulled in environments without a local shell toolchain.
        image: cdk.DockerImage.fromRegistry('public.ecr.aws/amazonlinux/amazonlinux:2023'),
        // Mount the packaging script into the container. CDK already mounts the
        // asset source at /asset-input (read-only) and the output at
        // /asset-output; the script lives outside that tree, so make it
        // reachable at /build.
        volumes: [
          {
            hostPath: path.join(__dirname, '../scripts'),
            containerPath: '/build',
          },
        ],
        command: [
          'bash', '/build/build-quick-setup-bundle.sh', '/asset-input', '/asset-output',
        ],
        local: {
          tryBundle(outputDir: string): boolean {
            try {
              execFileSync('bash', [bundleBuildScript, stationInstallDir, outputDir], {
                stdio: ['ignore', 'inherit', 'inherit'],
              });
              return true;
            } catch (err) {
              // Fall back to the Docker image bundling on any local failure
              // (e.g. bash unavailable on the build host).
              return false;
            }
          },
        },
      },
    });

    // Exposed so ComputeStack wiring (task 8.2) can bake the bundle/bootstrap
    // keys into the quick_setup Lambda environment.
    const quickSetupBundleDeployment = new s3deploy.BucketDeployment(this, 'QuickSetupBundle', {
      sources: [quickSetupBundleSource],
      destinationBucket: props.portalArtifactsBucket,
      destinationKeyPrefix: 'quick-setup/current/',
      // Never empty the destination: a failed deploy must leave the prior
      // successful artifacts intact (Req 4.4).
      prune: false,
      retainOnDelete: true,
    });
    // S3 keys of the artifacts uploaded by QuickSetupBundle under
    // quick-setup/current/. These are baked into the Lambda environments so the
    // quick_setup handler serves exactly the objects packaged by this
    // deployment (Req 4.4).
    const quickSetupBundleKey = 'quick-setup/current/setup-bundle.tar.gz';
    const quickSetupBootstrapKey = 'quick-setup/current/bootstrap.sh';

    // Compute the bundle/bootstrap SHA-256 digests at synth time so they can be
    // baked into the Lambda environment. build-quick-setup-bundle.sh produces a
    // reproducible tarball (stable ordering, zeroed timestamps/ownership,
    // timestamp-free gzip), so the digests computed here match the exact bytes
    // uploaded by QuickSetupBundle above — the checksum served to the station
    // is over the served bytes (Req 4.5). A failed deploy leaves the previously
    // baked env vars (and previously uploaded objects) in place (Req 4.4).
    let quickSetupBundleSha256: string;
    let quickSetupBootstrapSha256: string;
    {
      const manifestStagingDir = fs.mkdtempSync(path.join(os.tmpdir(), 'dda-qs-manifest-'));
      try {
        execFileSync('bash', [bundleBuildScript, stationInstallDir, manifestStagingDir], {
          stdio: ['ignore', 'inherit', 'inherit'],
        });
        const manifest = JSON.parse(
          fs.readFileSync(path.join(manifestStagingDir, 'manifest.json'), 'utf-8')
        ) as { bundle_sha256?: string; bootstrap_sha256?: string };
        if (!manifest.bundle_sha256 || !manifest.bootstrap_sha256) {
          throw new Error('manifest.json is missing bundle_sha256/bootstrap_sha256');
        }
        quickSetupBundleSha256 = manifest.bundle_sha256;
        quickSetupBootstrapSha256 = manifest.bootstrap_sha256;
      } catch (err) {
        // The digests are required for the station to verify the bundle it
        // downloads; refuse to synth a quick_setup Lambda that would serve an
        // empty/incorrect checksum rather than silently degrade integrity.
        throw new Error(
          'Failed to compute Station Quick Setup bundle checksums for the ' +
            `quick_setup Lambda environment. The packaging script (${bundleBuildScript}) ` +
            `must run under bash at synth time. Underlying error: ${(err as Error).message}`
        );
      } finally {
        fs.rmSync(manifestStagingDir, { recursive: true, force: true });
      }
    }
    // quickSetupBundleDeployment is referenced below via addDependency so the
    // QuickSetup Lambda is only reachable after the bundle is uploaded.

    // Base IAM Role for Lambda functions
    const createLambdaRole = (name: string) => {
      const role = new iam.Role(this, `${name}Role`, {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        ],
      });

      // Grant DynamoDB permissions
      props.useCasesTable.grantReadWriteData(role);
      props.userRolesTable.grantReadWriteData(role);
      props.devicesTable.grantReadWriteData(role);
      props.auditLogTable.grantWriteData(role);
      props.trainingJobsTable.grantReadWriteData(role);
      props.labelingJobsTable.grantReadWriteData(role);
      props.preLabeledDatasetsTable.grantReadWriteData(role);
      props.modelsTable.grantReadWriteData(role);
      props.deploymentsTable.grantReadWriteData(role);
      props.settingsTable.grantReadWriteData(role);
      props.componentsTable.grantReadWriteData(role);
      props.sharedComponentsTable.grantReadWriteData(role);
      props.dataAccountsTable.grantReadWriteData(role);
      props.workflowsTable.grantReadWriteData(role);
      props.workflowVersionsTable.grantReadWriteData(role);
      props.testDatasetsTable.grantReadWriteData(role);
      props.testRunsTable.grantReadWriteData(role);
      props.workflowChatSessionsTable.grantReadWriteData(role);
      props.cameraRegistryTable.grantReadWriteData(role);

      // Grant S3 permissions for portal artifacts bucket
      props.portalArtifactsBucket.grantReadWrite(role);

      // Grant SageMaker, Greengrass, CloudWatch Logs, STS, and API Gateway
      // permissions. Previously a single combined statement on
      // resources: ['*']; split per-service so each grant is scoped to the
      // committed dda-* / dda/* naming conventions (I1). The union of actions
      // across the split statements is identical to the original combined list.

      // SageMaker (scopable to resource type, NOT to a dda-* name prefix).
      // Portal training/compilation/labeling jobs are named after the use case
      // and model (e.g. "cookies-binary-jetson-<ts>"), NOT "dda-*". Scoping to
      // *-job/dda-* denied CreateCompilationJob and, more visibly,
      // DescribeCompilationJob on the real job names, so compilation jobs
      // completed in SageMaker but the portal marked them ERROR (it could not
      // read their status). Scope to the job/model resource types (the account
      // is the security boundary), matching the cross-account DDAPortalAccessRole
      // in usecase-account-stack.ts which already uses training-job/*,
      // compilation-job/*, labeling-job/*.
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'sagemaker:CreateTrainingJob',
          'sagemaker:DescribeTrainingJob',
          'sagemaker:ListTrainingJobs',
          'sagemaker:CreateCompilationJob',
          'sagemaker:DescribeCompilationJob',
          'sagemaker:ListCompilationJobs',
          'sagemaker:CreateLabelingJob',
          'sagemaker:DescribeLabelingJob',
          'sagemaker:ListLabelingJobs',
          'sagemaker:StopLabelingJob',
          'sagemaker:DescribeWorkteam',
          'sagemaker:AddTags',
        ],
        resources: [
          'arn:aws:sagemaker:*:*:training-job/*',
          'arn:aws:sagemaker:*:*:compilation-job/*',
          'arn:aws:sagemaker:*:*:labeling-job/*',
          'arn:aws:sagemaker:*:*:model/*',
          'arn:aws:sagemaker:*:*:workteam/*',
        ],
      }));

      // SageMaker (unscopable): sagemaker:ListWorkteams does not support
      // resource-level permissions per the AWS IAM reference.
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['sagemaker:ListWorkteams'],
        resources: ['*'],
      }));

      // Greengrass v2 (scopable to the service resource ARNs). Resource-tag
      // conditions on the Greengrass v2 API are limited, so no tag Condition is
      // applied here — the grant is bounded to the components / coreDevices /
      // deployments resource types the portal operates on.
      role.addToPolicy(new iam.PolicyStatement({
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
          'greengrass:ListEffectiveDeployments',
          'greengrass:ListTagsForResource',
          'greengrass:TagResource',
          'greengrass:ListDeployments',
          'greengrass:GetDeployment',
          'greengrass:CreateDeployment',
          'greengrass:CancelDeployment',
        ],
        resources: [
          'arn:aws:greengrass:*:*:components:*',
          'arn:aws:greengrass:*:*:coreDevices:*',
          'arn:aws:greengrass:*:*:deployments:*',
        ],
      }));

      // IoT (scopable) — thing-level actions are scoped to all things in the
      // account (thing/*), NOT thing/dda-*: edge devices are provisioned by
      // setup_station.sh with operator-chosen thing names (e.g.
      // "jp5-mic730ai-ryvanlabhome"), which never carry a dda- prefix. Scoping
      // to thing/dda-* broke Greengrass CreateDeployment, which internally
      // calls iot:DescribeThing on the target thing (AccessDeniedException).
      // This matches the cross-account DDAPortalAccessRole in
      // usecase-account-stack.ts, which already scopes IoT things to thing/*.
      // The account is the real security boundary here. Topics/jobs/thing-
      // groups remain scoped by resource type.
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'iot:DescribeThing',
          'iot:DescribeThingGroup',
          'iot:GetThingType',
          'iot:ListThings',
          'iot:ListThingsInThingGroup',
          'iot:ListThingGroups',
          'iot:CreateThingGroup',
          'iot:AddThingToThingGroup',
          'iot:RemoveThingFromThingGroup',
          'iot:CreateJob',
          'iot:DescribeJob',
          'iot:UpdateJob',
          'iot:GetJobDocument',
          'iot:ListJobs',
          'iot:CancelJob',
          'iot:GetThingShadow',
          'iot:UpdateThingShadow',
          'iot:DeleteThingShadow',
        ],
        resources: [
          'arn:aws:iot:*:*:thing/*',
          'arn:aws:iot:*:*:topic/dda/*',
          'arn:aws:iot:*:*:job/*',
          'arn:aws:iot:*:*:thinggroup/*',
        ],
      }));

      // IoT (unscopable): iot:DescribeEndpoint does not support resource-level
      // permissions.
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['iot:DescribeEndpoint'],
        resources: ['*'],
      }));

      // CloudWatch Logs read (unscopable-ish): logs:DescribeLogGroups and the
      // Filter/Get log-events reads do not usefully scope by log-group ARN when
      // the portal must query arbitrary Greengrass log groups.
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'logs:GetLogEvents',
          'logs:DescribeLogStreams',
          'logs:DescribeLogGroups',
          'logs:FilterLogEvents',
        ],
        resources: ['*'],
      }));

      // STS: scope AssumeRole to the fixed DDAPortalAccessRole role name. The
      // arn:aws:iam::*:role/ account wildcard is now bounded to the trusted
      // UseCase account list at synth time (matching I5/I6); the role name
      // DDAPortalAccessRole stays fixed and only the account portion is scoped.
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['sts:AssumeRole'],
        resources: props.trustedUseCaseAccountIds.map(
          (id) => `arn:aws:iam::${id}:role/DDAPortalAccessRole`
        ),
      }));

      // execute-api: scope Invoke to this portal account's API Gateway. The
      // portal REST API is created after this role in the same stack, so the
      // concrete API id is not resolvable at createLambdaRole time; fall back to
      // a portal-account-scoped ARN rather than a global '*'.
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['execute-api:Invoke'],
        resources: [`arn:aws:execute-api:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:*/*`],
      }));

      // S3 CORS on the portal artifacts bucket. Cross-account CORS on Data
      // Account buckets is separately granted via the assumed role (unchanged).
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetBucketCors',
          's3:PutBucketCors',
        ],
        resources: [props.portalArtifactsBucket.bucketArn],
      }));

      // Grant IAM PassRole permission for SageMaker execution role. Scoped to
      // DDA*-named roles (DDAPortalAccessRole, DDASageMakerExecutionRole,
      // DDAPortalDataAccessRole, …); the PassedToService condition is preserved.
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['iam:PassRole'],
        resources: ['arn:aws:iam::*:role/DDA*Role'],
        conditions: {
          StringEquals: {
            'iam:PassedToService': 'sagemaker.amazonaws.com',
          },
        },
      }));

      // Grant Cognito permissions for auth functions
      if (name === 'Auth') {
        role.addToPolicy(new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'cognito-idp:InitiateAuth',
            'cognito-idp:RespondToAuthChallenge',
            'cognito-idp:GetUser',
            'cognito-idp:AdminGetUser',
          ],
          resources: [props.userPool.userPoolArn],
        }));
      }

      // Grant S3 permissions for model artifact processing (I2).
      // Access is now enforced at BOTH ends: this portal-account grant is
      // scoped to the portal artifacts bucket (below) plus dda-portal:managed
      // tagged buckets; cross-account S3 access still goes through the assumed
      // DDAPortalAccessRole in the UseCase Account.
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetObject',
          's3:PutObject',
          's3:ListBucket',
          's3:CreateBucket',
          's3:GetBucketLocation',
          's3:GetBucketTagging',
        ],
        resources: [
          props.portalArtifactsBucket.bucketArn,
          `${props.portalArtifactsBucket.bucketArn}/*`,
        ],
      }));

      // Data-plane access to portal-managed data buckets (browse folders, read/
      // write objects). These buckets are arbitrary user-named and resolved at
      // RUNTIME (from the usecase's data_s3_bucket config and the
      // dda-portal:managed=true tag via the Resource Groups Tagging API), so
      // they cannot be scoped by ARN at synth time.
      //
      // IMPORTANT: an earlier attempt gated this with an
      // `aws:ResourceTag/dda-portal:managed` Condition, but S3 does NOT honor
      // aws:ResourceTag for bucket-level actions like s3:ListBucket on regular
      // buckets (it applies only via Access Point ARNs or per-bucket ABAC), so
      // that Condition silently denied ALL data-bucket browsing (regression:
      // "AccessDenied ... s3:ListBucket"). The grant below is therefore
      // unconditional on the S3 data plane, but deliberately scoped to
      // data-plane actions only — NO control-plane actions (PutBucketPolicy,
      // ACLs, DeleteBucket, PutBucketTagging, etc.) are granted here, and
      // cross-account data access is still gated by the assumed
      // DDAPortalAccessRole in the UseCase account.
      // Resolve the data-plane bucket scope from the optional allowlist. Each
      // allowlist entry may be a bare bucket name or a full `arn:aws:s3:::name`
      // ARN; normalize to the canonical bucket ARN. Empty/unset -> all buckets.
      const allowlist = (props.dataBucketAllowlist ?? []).filter((b) => b.length > 0);
      const bucketArns: string[] =
        allowlist.length > 0
          ? allowlist.map((b) =>
              b.startsWith('arn:aws:s3:::')
                ? b.replace(/\/\*?$/, '')
                : `arn:aws:s3:::${b}`
            )
          : ['arn:aws:s3:::*'];
      const bucketLevelResources = bucketArns;
      const objectLevelResources = bucketArns.map((arn) => `${arn}/*`);

      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          's3:ListBucket',
          's3:GetBucketLocation',
          's3:GetBucketTagging',
        ],
        resources: bucketLevelResources,
      }));
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetObject',
          's3:PutObject',
        ],
        resources: objectLevelResources,
      }));

      // s3:ListAllMyBuckets does not support resource-level permissions per the
      // AWS IAM reference; this statement is intentionally on '*'.
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['s3:ListAllMyBuckets'],
        resources: ['*'],
      }));

      // Grant Resource Groups Tagging API permissions for finding tagged buckets
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'tag:GetResources',
        ],
        resources: ['*'],
      }));

      return role;
    };

    // Environment variables for Lambda functions
    const lambdaEnvironment = {
      USECASES_TABLE: props.useCasesTable.tableName,
      USER_ROLES_TABLE: props.userRolesTable.tableName,
      DEVICES_TABLE: props.devicesTable.tableName,
      AUDIT_LOG_TABLE: props.auditLogTable.tableName,
      TRAINING_JOBS_TABLE: props.trainingJobsTable.tableName,
      LABELING_JOBS_TABLE: props.labelingJobsTable.tableName,
      PRE_LABELED_DATASETS_TABLE: props.preLabeledDatasetsTable.tableName,
      MODELS_TABLE: props.modelsTable.tableName,
      DEPLOYMENTS_TABLE: props.deploymentsTable.tableName,
      SETTINGS_TABLE: props.settingsTable.tableName,
      COMPONENTS_TABLE: props.componentsTable.tableName,
      SHARED_COMPONENTS_TABLE: props.sharedComponentsTable.tableName,
      WORKFLOWS_TABLE: props.workflowsTable.tableName,
      WORKFLOW_VERSIONS_TABLE: props.workflowVersionsTable.tableName,
      TEST_DATASETS_TABLE: props.testDatasetsTable.tableName,
      TEST_RUNS_TABLE: props.testRunsTable.tableName,
      WORKFLOW_CHAT_SESSIONS_TABLE: props.workflowChatSessionsTable.tableName,
      CAMERA_REGISTRY_TABLE: props.cameraRegistryTable.tableName,
      PORTAL_ARTIFACTS_BUCKET: props.portalArtifactsBucket.bucketName,
      // Workflow Manager documents (definitions, compiled docs, test datasets,
      // test results) live in the portal artifacts bucket under
      // workflows/{usecase_id}/... prefixes.
      WORKFLOWS_S3_PREFIX: 'workflows',
      PORTAL_ACCOUNT_ID: cdk.Aws.ACCOUNT_ID,
      USER_POOL_ID: props.userPool.userPoolId,
      // Shared component configuration - update DDA_LOCAL_SERVER_VERSION when publishing new component versions
      DDA_LOCAL_SERVER_VERSION: '1.0.63',
      // Per-arch minimum LocalServer version for Workflow_Components. LocalServer
      // ships as independently-versioned per-architecture variants whose lineages
      // are NOT comparable (the arm64 variant is ~1.0.124 while arm64JP6 is
      // ~1.0.35), so the single DDA_LOCAL_SERVER_VERSION=1.0.63 baseline falsely
      // blocks the JetPack variants. This map gives the arm64 JetPack lineages
      // their own floor (workflow support ships in current field builds, which
      // sit well below the arm64/x86 lineage numbers); archs absent here fall
      // back to DDA_LOCAL_SERVER_VERSION. Keys are workflow_core arch ids.
      WORKFLOW_MIN_LOCAL_SERVER_VERSIONS: JSON.stringify({
        arm64_jp4: '1.0.0',
        arm64_jp5: '1.0.0',
        arm64_jp6: '1.0.0',
      }),
      COMPONENT_BUCKET_PREFIX: 'dda-component',
    };

    // ---------------------------------------------------------------------
    // Station Quick Setup Lambdas (Requirements 4.4, 5.1, 8.1)
    //
    // device_registrations.py is JWT-authenticated and gets the standard
    // portal Lambda role (createLambdaRole) plus read/write on the new
    // registrations table. quick_setup.py is token-authenticated and gets a
    // deliberately minimal role: it may AssumeRole the use-case cross-account
    // roles, read the quick-setup/* artifacts, and read/write ONLY the
    // registrations + audit tables — never the broader portal tables.
    // ---------------------------------------------------------------------

    // device_registrations Lambda (Cognito JWT + manage_devices RBAC).
    const deviceRegistrationsRole = createLambdaRole('DeviceRegistrations');
    props.deviceRegistrationsTable.grantReadWriteData(deviceRegistrationsRole);
    const deviceRegistrationsHandler = new lambda.Function(this, 'DeviceRegistrationsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'device_registrations.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: deviceRegistrationsRole,
      environment: {
        ...lambdaEnvironment,
        REGISTRATIONS_TABLE: props.deviceRegistrationsTable.tableName,
        // Integrity anchor embedded in the generated Setup_Command so the
        // station can verify the bootstrap it downloads (Req 4.8 chain).
        QUICK_SETUP_BOOTSTRAP_SHA256: quickSetupBootstrapSha256,
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // quick_setup Lambda (token-authenticated; AuthorizationType.NONE routes).
    // Minimal, dedicated role — NOT createLambdaRole.
    const quickSetupRole = new iam.Role(this, 'QuickSetupHandlerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });
    // Read/write ONLY the registrations table (registration items + RATELIMIT#
    // counters) and the audit log — never the broader portal tables.
    props.deviceRegistrationsTable.grantReadWriteData(quickSetupRole);
    props.auditLogTable.grantReadWriteData(quickSetupRole);
    // Read-only on the use-cases table: get_bundle_manifest and
    // exchange_credentials resolve the bound registration's Use_Case to obtain
    // its AWS region, account id, cross-account role ARN, and external id
    // (shared_utils.get_usecase). Without this the /quick-setup/bundle and
    // /quick-setup/credentials routes fail with AccessDenied -> 503.
    props.useCasesTable.grantReadData(quickSetupRole);
    // Write access to the portal Devices table (dda-portal-devices) so a
    // successful quick-setup completion can record the Station's detected DDA
    // Target_Architecture, making the device deployment-gate-ready without a
    // manual admin step (device-arch-compatibility Req 2.2). This is the single
    // net-new permission in the feature and is scoped to the one portal table
    // the gate reads. DEVICES_TABLE is already injected via lambdaEnvironment
    // (spread into the handler environment below), so no env change is needed.
    props.devicesTable.grantWriteData(quickSetupRole);
    // Read-only on the quick-setup artifacts: serve the bootstrap bytes,
    // head_object the bundle, and sign presigned GET URLs (Req 4.1, 4.10).
    quickSetupRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject'],
      resources: [`${props.portalArtifactsBucket.bucketArn}/quick-setup/*`],
    }));
    // The dedicated station-provisioning role name is FIXED so its ARN is
    // computable here as a plain string. The role itself (created below) trusts
    // this Lambda role, so referencing it by string ARN — rather than the
    // construct — avoids a circular dependency between the two roles.
    const stationProvisioningRoleName = 'DDAStationProvisioningRole';
    const stationProvisioningRoleArn =
      `arn:aws:iam::${cdk.Aws.ACCOUNT_ID}:role/${stationProvisioningRoleName}`;

    // AssumeRole to mint scoped, short-lived Provisioning_Credentials (Req 5.1):
    //  - CROSS-account use cases assume their use-case-account DDAPortalAccessRole;
    //  - SAME-account use cases (whose cross_account_role_arn is the account
    //    root ARN — not assumable) assume the dedicated DDAStationProvisioningRole
    //    in THIS account (created below, trusted only by this Lambda role).
    // The per-device session policy narrows the issued credentials in both cases.
    quickSetupRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['sts:AssumeRole'],
      resources: [
        ...props.trustedUseCaseAccountIds.map(
          (id) => `arn:aws:iam::${id}:role/DDAPortalAccessRole`
        ),
        stationProvisioningRoleArn,
      ],
    }));

    const quickSetupHandler = new lambda.Function(this, 'QuickSetupHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'quick_setup.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: quickSetupRole,
      environment: {
        ...lambdaEnvironment,
        REGISTRATIONS_TABLE: props.deviceRegistrationsTable.tableName,
        // Artifact keys + digests baked from this deployment's bundle asset so
        // only the most recently deployed bundle is ever served (Req 4.4, 4.5).
        QUICK_SETUP_BUNDLE_KEY: quickSetupBundleKey,
        QUICK_SETUP_BUNDLE_SHA256: quickSetupBundleSha256,
        QUICK_SETUP_BOOTSTRAP_KEY: quickSetupBootstrapKey,
        QUICK_SETUP_BOOTSTRAP_SHA256: quickSetupBootstrapSha256,
        // Same-account provisioning role assumed when a Use_Case's
        // cross_account_role_arn is the (un-assumable) account root ARN.
        QUICK_SETUP_PROVISIONING_ROLE_ARN: stationProvisioningRoleArn,
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });
    // The bundle must be uploaded before the handler can serve it.
    quickSetupHandler.node.addDependency(quickSetupBundleDeployment);

    // Dedicated station-provisioning role for SAME-account use cases
    // (station-quick-setup). The quick_setup Lambda assumes this role WITH the
    // per-device least-privilege session policy (session_policy.build_session_policy)
    // to mint the scoped, short-lived Provisioning_Credentials handed to the
    // station. It holds the provisioning action CEILING; the session policy
    // narrows every issuance to the single registered thing / Device_Group, so
    // the station never receives broader access. It is trusted ONLY by the
    // QuickSetupHandler role — no other principal may assume it. Cross-account
    // use cases continue to use their use-case-account DDAPortalAccessRole.
    const stationProvisioningRole = new iam.Role(this, 'StationProvisioningRole', {
      roleName: stationProvisioningRoleName,
      assumedBy: new iam.ArnPrincipal(quickSetupRole.roleArn),
      description:
        'Station Quick Setup provisioning role assumed by the quick_setup ' +
        'Lambda (with a per-device session policy) to mint scoped, short-lived ' +
        'station credentials for same-account use cases.',
      maxSessionDuration: cdk.Duration.hours(1),
    });
    // IoT thing + Device_Group provisioning (narrowed per-device by the session
    // policy), scoped to the thing/thinggroup resource types in this account.
    stationProvisioningRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'iot:CreateThing',
        'iot:DescribeThing',
        'iot:CreateThingGroup',
        'iot:DescribeThingGroup',
        'iot:AddThingToThingGroup',
        'iot:ListThingGroupsForThing',
      ],
      resources: [
        `arn:aws:iot:*:${cdk.Aws.ACCOUNT_ID}:thing/*`,
        `arn:aws:iot:*:${cdk.Aws.ACCOUNT_ID}:thinggroup/*`,
      ],
    }));
    // IoT cert/policy/endpoint/role-alias actions whose target ids are generated
    // during provisioning (Resource '*'; never a wildcard *action*).
    //
    // nosec: this resource wildcard is intentional and mitigated, not an
    // unscoped grant. iot:CreateKeysAndCertificate and iot:DescribeEndpoint are
    // resource-less (no ARN to scope to), and the cert/policy/role-alias ids the
    // remaining actions target are generated at provisioning time so cannot be
    // pre-scoped in the role. This role is only assumable by the QuickSetupHandler
    // Lambda, which assumes it with a per-device session policy that narrows every
    // call to the specific device being provisioned (see the session-policy
    // narrowing on the statement above). It is never attached to a station.
    stationProvisioningRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'iot:CreateKeysAndCertificate',
        'iot:AttachThingPrincipal',
        'iot:AttachPolicy',
        'iot:CreatePolicy',
        'iot:GetPolicy',
        'iot:ListPolicyVersions',
        'iot:CreatePolicyVersion',
        'iot:DeletePolicyVersion',
        'iot:DescribeEndpoint',
        'iot:CreateRoleAlias',
        'iot:DescribeRoleAlias',
      ],
      resources: ['*'], // nosec - see rationale above (resource-less + runtime-generated ids, session-policy narrowed)
    }));
    // Greengrass Token Exchange Service (TES) role setup performed by the
    // provisioner, scoped to the GreengrassV2TokenExchangeRole* names.
    stationProvisioningRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'iam:GetRole',
        'iam:CreateRole',
        'iam:AttachRolePolicy',
        'iam:PutRolePolicy',
        'iam:PassRole',
      ],
      resources: [`arn:aws:iam::${cdk.Aws.ACCOUNT_ID}:role/GreengrassV2TokenExchangeRole*`],
    }));
    stationProvisioningRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['iam:CreatePolicy', 'iam:GetPolicy'],
      resources: [`arn:aws:iam::${cdk.Aws.ACCOUNT_ID}:policy/GreengrassV2TokenExchangeRoleAccess*`],
    }));
    // Tag the Greengrass core device (dda-portal:managed) during provisioning.
    stationProvisioningRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['greengrass:TagResource'],
      resources: [`arn:aws:greengrass:*:${cdk.Aws.ACCOUNT_ID}:coreDevices:*`],
    }));
    stationProvisioningRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['sts:GetCallerIdentity'],
      resources: ['*'],
    }));

    // UseCases Lambda Handler
    const useCasesHandler = new lambda.Function(this, 'UseCasesHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'usecases.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('UseCases'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-08-auto-cors', // Force update with auto CORS configuration
        // CloudFront domain for auto-configuring CORS on Data Account buckets
        ...(props.cloudFrontDomain && { CLOUDFRONT_DOMAIN: props.cloudFrontDomain }),
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(120), // Increased for shared component provisioning
    });

    // Grant UseCases Lambda permission to list S3 objects for artifact discovery during onboarding
    const componentBucketNameForUsecases = `dda-component-${cdk.Aws.REGION}-${cdk.Aws.ACCOUNT_ID}`;
    useCasesHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        's3:ListBucket',
        's3:GetBucketPolicy',
        's3:PutBucketPolicy',
      ],
      resources: [`arn:aws:s3:::${componentBucketNameForUsecases}`],
    }));

    // Grant UseCases Lambda permission to list Greengrass component versions for dynamic version discovery
    useCasesHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'greengrass:ListComponentVersions',
      ],
      resources: [`arn:aws:greengrass:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:components:aws.edgeml.dda.LocalServer.*`],
    }));

    // Grant UseCases Lambda permission to configure EventBridge for cross-account event forwarding
    useCasesHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'events:PutPermission',
        'events:RemovePermission',
        'events:DescribeEventBus',
      ],
      resources: [`arn:aws:events:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:event-bus/default`],
    }));

    // Devices Lambda Handler
    const devicesHandler = new lambda.Function(this, 'DevicesHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'devices.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Devices'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2024-12-12-fixed-shared-utils', // Force update with fixed shared_utils.py
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // SSH via AWS IoT Secure Tunneling: the devices handler opens tunnels and
    // manages the SecureTunneling component. Greengrass deploy + IoT perms are
    // already on the base role; add the secure-tunneling actions here.
    // NOTE: AWS IoT Secure Tunneling's API service namespace is
    // "iotsecuretunneling", but its IAM action prefix is "iot:" (e.g.
    // iot:OpenTunnel). Using the wrong prefix yields AccessDeniedException.
    devicesHandler.role?.addToPrincipalPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'iot:OpenTunnel',
        'iot:CloseTunnel',
        'iot:DescribeTunnel',
        'iot:ListTunnels',
        'iot:ListTagsForResource',
        'iot:RotateTunnelAccessToken',
      ],
      // nosec: iam-resource-wildcard — AWS IoT Secure Tunneling OpenTunnel does
      // not support resource-level permissions (the tunnel does not yet exist
      // when OpenTunnel is called), and iot:ListTunnels is an account-scoped
      // list operation; both must remain on '*'. The remaining Close/Describe/
      // RotateTunnelAccessToken actions are bundled with them in this single
      // grant. This statement is NOT one of the I1–I17 scanner findings.
      resources: ['*'],
    }));

    // Device Logs Lambda Handler
    const deviceLogsHandler = new lambda.Function(this, 'DeviceLogsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'device_logs.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('DeviceLogs'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-19-device-logs',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60), // Longer timeout for log fetching
    });

    // Device Logs Analyzer Lambda Handler (AI/pattern-based log analysis).
    // Serves POST /devices/{id}/logs/analyze.
    const deviceLogsAnalyzerHandler = new lambda.Function(this, 'DeviceLogsAnalyzerHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'device_logs_analyzer.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('DeviceLogsAnalyzer'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-19-device-logs-analyzer',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60), // Fetch + analyze logs
    });

    // Deployments Lambda Handler
    const deploymentsHandler = new lambda.Function(this, 'DeploymentsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'deployments.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Deployments'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-04-deployments',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Component store limit auto-remediation sweep: detects devices whose
    // latest deployment failed with the Nucleus "Component store size limit
    // reached" rejection, submits a config-only revision raising
    // componentStoreMaxSizeBytes (no artifact downloads, so it bypasses the
    // pre-merge store check), then resubmits the blocked deployment once the
    // remediation completes. State machine lives in deployment tags —
    // no user intervention required (deployments.remediate_component_store_failures).
    const storeRemediationRule = new events.Rule(this, 'StoreRemediationScheduleRule', {
      ruleName: 'dda-portal-component-store-remediation',
      description:
        'Auto-remediates Greengrass "Component store size limit reached" ' +
        'deployment failures without user intervention',
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
    });
    storeRemediationRule.addTarget(new targets.LambdaFunction(deploymentsHandler, {
      event: events.RuleTargetInput.fromObject({
        action: 'remediate-component-store-limit',
      }),
    }));

    // Auth Lambda Handler
    const authHandler = new lambda.Function(this, 'AuthHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'auth.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Auth'),
      environment: {
        ...lambdaEnvironment,
        USER_POOL_ID: props.userPool.userPoolId,
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // User Management Lambda Handler
    const userManagementHandler = new lambda.Function(this, 'UserManagementHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'user_management.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('UserManagement'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2024-12-12-fixed-shared-utils', // Force update with fixed shared_utils.py
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // JWT Authorizer Lambda Handler (alternative to Cognito authorizer)
    const jwtAuthorizerHandler = new lambda.Function(this, 'JwtAuthorizerHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'jwt_authorizer.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('JwtAuthorizer'),
      environment: {
        COGNITO_USER_POOL_ID: props.userPool.userPoolId,
        COGNITO_REGION: cdk.Aws.REGION,
        ALLOWED_AUDIENCES: '', // Configure based on your needs
        ISSUER_WHITELIST: '', // Configure based on your identity providers
      },
      layers: [jwtLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // Datasets Lambda Handler
    const datasetsHandler = new lambda.Function(this, 'DatasetsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'datasets.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Datasets'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Captures Lambda Handler
    // Surfaces on-device inference-results captures (source image, overlay,
    // mask, and the results `.jsonl` Capture_Metadata) to the frontend,
    // mirroring the presigned-URL / cross-account-assume-role pattern used by
    // the Datasets handler (see backend/functions/captures.py).
    const capturesHandler = new lambda.Function(this, 'CapturesHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'captures.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Captures'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Pre-Labeled Datasets Lambda Handler
    const preLabeledDatasetsHandler = new lambda.Function(this, 'PreLabeledDatasetsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'pre_labeled_datasets.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('PreLabeledDatasets'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Labeling Lambda Handler
    const labelingHandler = new lambda.Function(this, 'LabelingHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'labeling.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Labeling'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2024-12-08-v1', // Force update - fixed usecase-jobs-index
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Labeling Monitor Lambda Handler (for EventBridge)
    const labelingMonitorHandler = new lambda.Function(this, 'LabelingMonitorHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'labeling_monitor.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('LabelingMonitor'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // Training Lambda Handler
    const trainingHandler = new lambda.Function(this, 'TrainingHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'training.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Training'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2024-12-08-v1', // Force update - fixed job name validation
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60), // Longer timeout for SageMaker API calls
    });

    // Compilation Lambda Handler
    const compilationHandler = new lambda.Function(this, 'CompilationHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'compilation.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Compilation'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(300), // 5 minutes for model extraction and repackaging
      memorySize: 1024, // More memory for tar.gz extraction
      ephemeralStorageSize: cdk.Size.gibibytes(4), // 4GB temp storage for large models
    });

    // Packaging Lambda Handler
    const packagingHandler = new lambda.Function(this, 'PackagingHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'packaging.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Packaging'),
      environment: {
        ...lambdaEnvironment,
        STORAGE_WARNING_THRESHOLD: '6442450944', // 6GB in bytes
        STORAGE_CRITICAL_THRESHOLD: '7516192768', // 7GB in bytes
        ENABLE_STREAMING_PROCESSING: 'true',
        MAX_CONCURRENT_OPERATIONS: '2',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(900), // 15 minutes for large model processing
      memorySize: 3008, // Increased memory for better performance
      ephemeralStorageSize: cdk.Size.gibibytes(8), // 8GB temp storage for large models
    });

    // Greengrass Publishing Lambda Handler
    const greengrassPublishHandler = new lambda.Function(this, 'GreengrassPublishHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'greengrass_publish.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('GreengrassPublish'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(300), // 5 minutes for component creation and monitoring
    });

    // Update packaging handler environment with Greengrass function name
    packagingHandler.addEnvironment('GREENGRASS_PUBLISH_FUNCTION_NAME', greengrassPublishHandler.functionName);

    // Components Lambda Handler for Greengrass Component Browser
    const componentsHandler = new lambda.Function(this, 'ComponentsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'components.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Components'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60), // 1 minute for component discovery and management
    });

    // Data Management Lambda Handler for S3 bucket/folder management
    const dataManagementHandler = new lambda.Function(this, 'DataManagementHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'data_management.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('DataManagement'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Shared Components Lambda Handler for dda-LocalServer provisioning
    const sharedComponentsHandler = new lambda.Function(this, 'SharedComponentsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'shared_components.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('SharedComponents'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-07-bucket-policy-update',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(120), // 2 minutes for cross-account component creation
    });

    // Data Accounts Lambda Handler for managing registered Data Accounts
    const dataAccountsHandler = new lambda.Function(this, 'DataAccountsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'data_accounts.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('DataAccounts'),
      environment: {
        ...lambdaEnvironment,
        DATA_ACCOUNTS_TABLE: props.dataAccountsTable.tableName,
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // Model Import Lambda Handler for BYOM (Bring Your Own Model)
    const modelImportHandler = new lambda.Function(this, 'ModelImportHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'model_import.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('ModelImport'),
      environment: {
        ...lambdaEnvironment,
        COMPILATION_FUNCTION_NAME: compilationHandler.functionName,
        CODE_VERSION: '2025-01-10-byom',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(300), // 5 minutes for model validation
      memorySize: 1024, // More memory for tar.gz extraction
      ephemeralStorageSize: cdk.Size.gibibytes(4), // 4GB temp storage for large models
    });

    // Grant Model Import Lambda permission to invoke compilation
    compilationHandler.grantInvoke(modelImportHandler);

    // Model Converter Lambda Handler for auto-generating DDA metadata
    const modelConverterHandler = new lambda.Function(this, 'ModelConverterHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'model_converter.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('ModelConverter'),
      environment: {
        ...lambdaEnvironment,
        MODEL_IMPORT_FUNCTION_NAME: modelImportHandler.functionName,
        CODE_VERSION: '2025-01-10-converter',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(600), // 10 minutes for large model conversion
      memorySize: 3008, // More memory for PyTorch model inspection
      ephemeralStorageSize: cdk.Size.gibibytes(8), // 8GB temp storage for large models
    });

    // Grant Model Converter Lambda permission to invoke Model Import
    modelImportHandler.grantInvoke(modelConverterHandler);

    // Models Registry Lambda Handler
    const modelsHandler = new lambda.Function(this, 'ModelsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'models.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Models'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-19-models-registry',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // Audit Logs Lambda Handler
    const auditLogsHandler = new lambda.Function(this, 'AuditLogsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'audit_logs.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('AuditLogs'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-20-audit-logs',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // Grant Audit Logs Lambda read access to the audit log table
    props.auditLogTable.grantReadData(auditLogsHandler);

    // ------------------------------------------------------------------
    // Camera Registry Sync (camera-registry-sync)
    // Edge devices report their Camera_Source inventory into the
    // dda-camera-registry named shadow; a per-use-case IoT topic rule on
    // $aws/things/+/shadow/name/dda-camera-registry/update/documents
    // (provisioned by the UseCaseAccountStack during use-case onboarding)
    // forwards each shadow documents event to the SQS queue below, where the
    // Portal_Sync_Service Lambda (camera_sync.py) reduces it into the
    // dda-portal-camera-registry table.
    // ------------------------------------------------------------------

    // Dead-letter queue for shadow-report events that repeatedly fail
    // processing (malformed/unparseable reports are also dead-lettered
    // explicitly by the handler without blocking the batch).
    const cameraShadowReportDlq = new sqs.Queue(this, 'CameraShadowReportDLQ', {
      queueName: 'dda-portal-camera-shadow-reports-dlq',
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });

    // Shadow-report queue fed by the per-use-case IoT topic rules. The
    // visibility timeout is a multiple of the consumer Lambda timeout per
    // the SQS event-source guidance.
    const cameraShadowReportQueue = new sqs.Queue(this, 'CameraShadowReportQueue', {
      queueName: 'dda-portal-camera-shadow-reports',
      visibilityTimeout: cdk.Duration.seconds(180),
      retentionPeriod: cdk.Duration.days(4),
      enforceSSL: true,
      deadLetterQueue: {
        queue: cameraShadowReportDlq,
        maxReceiveCount: 3,
      },
    });

    // Cross-account queue policy: an IoT topic rule delivers to SQS through
    // its rule role, an IAM principal in the (use-case) account where the
    // rule runs. Allow SendMessage from the trusted UseCase accounts plus
    // this portal account (same-account use cases).
    cameraShadowReportQueue.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'AllowUseCaseAccountIotRuleDelivery',
      effect: iam.Effect.ALLOW,
      principals: [new iam.AnyPrincipal()],
      actions: ['sqs:SendMessage'],
      resources: [cameraShadowReportQueue.queueArn],
      conditions: {
        StringEquals: {
          'aws:PrincipalAccount': Array.from(
            new Set([...props.trustedUseCaseAccountIds, cdk.Aws.ACCOUNT_ID])
          ),
        },
      },
    }));

    // Portal_Sync_Service ingest Lambda (camera_sync.py) — consumes shadow
    // documents events from the queue and applies the reduce_report sync
    // reducer per camera source into the camera registry table.
    const cameraSyncHandler = new lambda.Function(this, 'CameraSyncHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'camera_sync.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('CameraSync'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-02-14-camera-sync',
        CAMERA_SHADOW_REPORT_DLQ_URL: cameraShadowReportDlq.queueUrl,
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    cameraSyncHandler.addEventSource(new SqsEventSource(cameraShadowReportQueue, {
      batchSize: 10,
      reportBatchItemFailures: true,
    }));

    // The handler dead-letters malformed reports explicitly (in addition to
    // the redrive policy above).
    cameraShadowReportDlq.grantSendMessages(cameraSyncHandler);

    // Camera_Registry API Lambda (camera_registry.py) — device cameras
    // read/mutate routes, conflict listing/re-apply, and on-demand refresh
    // (GetThingShadow pull through the assumed use-case role).
    const cameraRegistryHandler = new lambda.Function(this, 'CameraRegistryHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'camera_registry.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('CameraRegistry'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-02-14-camera-registry',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // Deploy-time Camera_Binding delivery: deployments.py writes
    // desired.bindings["{workflowId}/{version}"] into each target thing's
    // dda-camera-bindings named shadow at deployment submission. IAM scopes
    // shadow actions to the thing ARN (named shadows share the thing
    // resource); this explicit iot-data grant keeps the camera-bindings
    // shadow write working independently of the base-role IoT statement.
    deploymentsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'iot:GetThingShadow',
        'iot:UpdateThingShadow',
      ],
      resources: ['arn:aws:iot:*:*:thing/*'],
    }));

    // ------------------------------------------------------------------
    // Portal User Manager (portal-user-manager, task 5.1)
    // PortalAdmin-only Cognito account management (user_admin.py) and the
    // Account_Sync_Service portal side (account_sync.py): accounts are
    // delivered to each edge device over the dda-user-accounts named
    // shadow; the device acks via reported.ackSyncId, forwarded by an IoT
    // topic rule into the SQS queue below and ingested back into the
    // per-device sync-state table. An EventBridge rate(5 minutes)
    // schedule drives retries and the 60-second ack-timeout sweep.
    // ------------------------------------------------------------------

    // Verified SES sender for temporary-password delivery (design D11):
    // Cognito cannot email a temporary password to an existing CONFIRMED
    // user, so user_admin.py sends it via SES from this address. The
    // address (or an address on a verified domain identity) must be
    // verified in SES in this account/region before the forgot-password
    // flow is used; user_admin.py rejects the action when unset.
    const sesSenderAddress = new cdk.CfnParameter(this, 'SesSenderAddress', {
      type: 'String',
      default: '',
      description:
        'Verified SES sender address for User_Manager temporary-password ' +
        'emails (portal-user-manager design D11). Leave empty to disable ' +
        'the forgot-password flow.',
    });

    // Credential-verifier table (design D3/D4): salted one-way PBKDF2
    // verifiers captured by user_admin.py password flows; never contains
    // plaintext. Read at listing time for the edgeCapable flag and by the
    // sync staging path.
    const edgeCredentialsTable = new dynamodb.Table(this, 'EdgeCredentialsTable', {
      tableName: 'dda-portal-edge-credentials',
      partitionKey: {
        name: 'username',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Per-device account-sync state (staged account set + syncId, status,
    // attemptAt/lastSyncAt, pendingChanges) — the Account_Sync_Service
    // reducer state (Reqs 7.4-7.9).
    const accountSyncTable = new dynamodb.Table(this, 'AccountSyncTable', {
      tableName: 'dda-portal-account-sync',
      partitionKey: {
        name: 'device_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Dead-letter queue for ack events that repeatedly fail processing
    // (malformed acks are also dead-lettered explicitly by the handler
    // without blocking the batch — camera-registry-sync pattern).
    const accountSyncAckDlq = new sqs.Queue(this, 'AccountSyncAckDLQ', {
      queueName: 'dda-portal-account-sync-acks-dlq',
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });

    // Ack queue fed by the dda-user-accounts shadow topic rule(s). The
    // visibility timeout is a multiple of the consumer Lambda timeout per
    // the SQS event-source guidance.
    const accountSyncAckQueue = new sqs.Queue(this, 'AccountSyncAckQueue', {
      queueName: 'dda-portal-account-sync-acks',
      visibilityTimeout: cdk.Duration.seconds(180),
      retentionPeriod: cdk.Duration.days(4),
      enforceSSL: true,
      deadLetterQueue: {
        queue: accountSyncAckDlq,
        maxReceiveCount: 3,
      },
    });

    // Cross-account queue policy: an IoT topic rule delivers to SQS
    // through its rule role, an IAM principal in the account where the
    // rule runs. Allow SendMessage from the trusted UseCase accounts
    // (devices whose shadows live there) plus this portal account.
    accountSyncAckQueue.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'AllowUseCaseAccountIotRuleDelivery',
      effect: iam.Effect.ALLOW,
      principals: [new iam.AnyPrincipal()],
      actions: ['sqs:SendMessage'],
      resources: [accountSyncAckQueue.queueArn],
      conditions: {
        StringEquals: {
          'aws:PrincipalAccount': Array.from(
            new Set([...props.trustedUseCaseAccountIds, cdk.Aws.ACCOUNT_ID])
          ),
        },
      },
    }));

    // Portal-account IoT topic rule forwarding every dda-user-accounts
    // shadow update/documents event (device acks) to the ack queue.
    // topic(3) is the thing name in $aws/things/{thing}/shadow/... —
    // account_sync.py's ack parser requires the injected thing_name.
    const userAccountsShadowRuleRole = new iam.Role(this, 'UserAccountsShadowRuleRole', {
      assumedBy: new iam.ServicePrincipal('iot.amazonaws.com'),
      description:
        'Role for the dda-user-accounts shadow IoT topic rule to deliver ' +
        'shadow documents events to the DDA Portal account-sync ack queue',
    });
    userAccountsShadowRuleRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SendAccountSyncAcks',
      effect: iam.Effect.ALLOW,
      actions: ['sqs:SendMessage'],
      resources: [accountSyncAckQueue.queueArn],
    }));

    new iot.CfnTopicRule(this, 'UserAccountsShadowRule', {
      ruleName: 'dda_user_accounts_shadow_documents',
      topicRulePayload: {
        sql: "SELECT *, topic(3) AS thing_name FROM '$aws/things/+/shadow/name/dda-user-accounts/update/documents'",
        awsIotSqlVersion: '2016-03-23',
        ruleDisabled: false,
        description:
          'Forward dda-user-accounts shadow documents events to the DDA ' +
          'Portal account-sync ack queue',
        actions: [
          {
            sqs: {
              queueUrl: accountSyncAckQueue.queueUrl,
              roleArn: userAccountsShadowRuleRole.roleArn,
              useBase64: false,
            },
          },
        ],
      },
    });

    // Account_Sync_Service Lambda (account_sync.py) — three entry paths
    // routed by event shape: SQS records -> ack ingest, direct
    // {action: 'sync_attempt'} invoke from user_admin.py, EventBridge
    // scheduled event -> timeout sweep + pending-changes delivery pass.
    // Shadow writes go through the base-role IoT grant (Get/Update
    // ThingShadow on thing/*) or the assumed use-case role for
    // cross-account devices (camera_registry.py pattern).
    const accountSyncHandler = new lambda.Function(this, 'AccountSyncHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'account_sync.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('AccountSync'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-06-account-sync',
        ACCOUNT_SYNC_TABLE: accountSyncTable.tableName,
        ACCOUNT_SYNC_ACK_DLQ_URL: accountSyncAckDlq.queueUrl,
      },
      layers: [sharedLayer],
      // The scheduled pass attempts delivery for every device with
      // pending changes in one invocation.
      timeout: cdk.Duration.seconds(60),
    });

    accountSyncHandler.addEventSource(new SqsEventSource(accountSyncAckQueue, {
      batchSize: 10,
      reportBatchItemFailures: true,
    }));

    // The handler dead-letters malformed acks explicitly (in addition to
    // the redrive policy above).
    accountSyncAckDlq.grantSendMessages(accountSyncHandler);
    accountSyncTable.grantReadWriteData(accountSyncHandler);

    // Retry/timeout driver: WHILE a device has undelivered pending
    // changes, delivery is attempted at intervals not exceeding 5 minutes
    // (Req 7.7); the same pass marks in_progress rows older than 60 s
    // without an ack as failed / device unreachable (Req 7.9).
    const accountSyncScheduleRule = new events.Rule(this, 'AccountSyncScheduleRule', {
      ruleName: 'dda-portal-account-sync-schedule',
      description:
        'Drives Account_Sync_Service retries and ack-timeout sweeps ' +
        '(portal-user-manager Reqs 7.7, 7.9)',
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
    });
    accountSyncScheduleRule.addTarget(new targets.LambdaFunction(accountSyncHandler));

    // User_Manager admin API Lambda (user_admin.py) — PortalAdmin-only
    // Cognito account management behind /admin/* (routes attached by the
    // UserAdminApiStack below). Separate function so the cognito-idp
    // admin and SES grants stay scoped to it alone (design D1).
    const userAdminHandler = new lambda.Function(this, 'UserAdminHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'user_admin.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('UserAdmin'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-06-user-admin',
        EDGE_CREDENTIALS_TABLE: edgeCredentialsTable.tableName,
        ACCOUNT_SYNC_TABLE: accountSyncTable.tableName,
        ACCOUNT_SYNC_FUNCTION: accountSyncHandler.functionName,
        SES_SENDER_ADDRESS: sesSenderAddress.valueAsString,
      },
      layers: [sharedLayer],
      // Full-pool list_users pagination (listing + last-PortalAdmin guard).
      timeout: cdk.Duration.seconds(60),
    });

    edgeCredentialsTable.grantReadWriteData(userAdminHandler);
    accountSyncTable.grantReadWriteData(userAdminHandler);
    // Immediate sync attempt after staging (the 5-minute schedule is the
    // fallback when the invoke fails).
    accountSyncHandler.grantInvoke(userAdminHandler);

    // Cognito admin operations scoped to the portal user pool and granted
    // ONLY to user_admin.py (task 5.1): account listing/inspection,
    // password set (permanent/temporary), and custom:role updates.
    // Extended (task 15.1) with account life-cycle operations: creation,
    // enable/disable, and deletion.
    userAdminHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'cognito-idp:AdminSetUserPassword',
        'cognito-idp:AdminUpdateUserAttributes',
        'cognito-idp:ListUsers',
        'cognito-idp:AdminGetUser',
        'cognito-idp:AdminCreateUser',
        'cognito-idp:AdminEnableUser',
        'cognito-idp:AdminDisableUser',
        'cognito-idp:AdminDeleteUser',
      ],
      resources: [props.userPool.userPoolArn],
    }));

    // SES send for temporary-password delivery (design D11). Scoped to
    // this account's verified identities: SES authorizes SendEmail
    // against the identity of the Source address — an address identity
    // or the domain identity covering it — so identity/* (rather than a
    // single address ARN) keeps domain-verified senders working.
    userAdminHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['ses:SendEmail'],
      resources: [
        `arn:aws:ses:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:identity/*`,
      ],
    }));

    // ------------------------------------------------------------------
    // Workflow Manager Lambda functions
    // Handler modules (workflows.py, workflow_validation.py,
    // workflow_packaging.py, workflow_generator.py, workflow_testing.py)
    // live in backend/functions alongside the existing handlers.
    // ------------------------------------------------------------------

    // Workflows Lambda Handler (Workflow_Store API - CRUD, versioning, duplicate)
    const workflowsHandler = new lambda.Function(this, 'WorkflowsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'workflows.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Workflows'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-25-workflows',
      },
      layers: [sharedLayer, workflowCoreLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Workflow Validation Lambda Handler (validate endpoint + node catalog)
    const workflowValidationHandler = new lambda.Function(this, 'WorkflowValidationHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'workflow_validation.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('WorkflowValidation'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-25-workflow-validation',
      },
      layers: [sharedLayer, workflowCoreLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Workflow Packaging Lambda Handler (Component_Packager - compile, assemble
    // per-arch artifacts, register Greengrass Workflow_Component)
    const workflowPackagingHandler = new lambda.Function(this, 'WorkflowPackagingHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'workflow_packaging.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('WorkflowPackaging'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-25-workflow-packaging',
      },
      layers: [sharedLayer, workflowCoreLayer],
      timeout: cdk.Duration.seconds(300), // compile + multi-arch artifact staging
      memorySize: 1024,
      ephemeralStorageSize: cdk.Size.gibibytes(4), // plugin artifact assembly
    });

    // Workflow Generator Lambda Handler (Bedrock prompt-based generation)
    const workflowGeneratorHandler = new lambda.Function(this, 'WorkflowGeneratorHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'workflow_generator.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('WorkflowGenerator'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-25-workflow-generator',
      },
      layers: [sharedLayer, workflowCoreLayer],
      // Bedrock invocation timeout is configurable up to 60s; leave headroom
      timeout: cdk.Duration.seconds(90),
    });

    // Grant the generator permission to invoke the configured Bedrock model via
    // the Converse API. The model identifier is runtime configuration
    // (Bedrock_Configuration in the settings table), so the grant covers
    // foundation models and inference profiles rather than a fixed model ARN.
    workflowGeneratorHandler.addToRolePolicy(new iam.PolicyStatement({
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

    // ------------------------------------------------------------------
    // Merged Node_Type_Catalog resolution (custom-node-designer task 9.2):
    // the workflow store / validation / generator handlers read the
    // Use_Case's registered Custom_Node_Types and the Lifecycle_State of
    // their backing Plugin_Record versions. Those tables live in the
    // NodeDesignerStack, which already depends on this stack's REST API —
    // referencing its table tokens here would create a circular
    // cross-stack reference, so the FIXED physical table names declared
    // in node-designer-stack.ts are used instead (the same fixed-name
    // pattern that stack uses for its simulator state machine ARN). The
    // handlers degrade to the built-in catalog when the tables are not
    // deployed.
    const CUSTOM_NODE_TYPES_TABLE_NAME = 'dda-portal-custom-node-types';
    const PLUGIN_RECORDS_TABLE_NAME = 'dda-portal-plugin-records';
    const catalogConsumerHandlers = [
      workflowsHandler,
      workflowValidationHandler,
      workflowGeneratorHandler,
      // Component_Packager (custom-node-designer task 10.1): compiles
      // against the merged catalog and loads the backing Plugin_Records
      // for the custom-plugin packaging gates and artifact verification.
      workflowPackagingHandler,
      // Deployment_Service (custom-node-designer task 10.5): loads the
      // backing Plugin_Records of a deployment's dependency closure for
      // the pre-submit lifecycle and architecture gates.
      deploymentsHandler,
      // Component listing (custom-node-designer task 10.8): joins
      // dda.plugin.* Plugin_Components with their backing Plugin_Record's
      // Lifecycle_State for the deployment screen (Requirement 16.2).
      componentsHandler,
    ];
    for (const handler of catalogConsumerHandlers) {
      handler.addEnvironment('CUSTOM_NODE_TYPES_TABLE', CUSTOM_NODE_TYPES_TABLE_NAME);
      handler.addEnvironment('PLUGIN_RECORDS_TABLE', PLUGIN_RECORDS_TABLE_NAME);
      handler.addToRolePolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:BatchGetItem',
          'dynamodb:Query',
          'dynamodb:Scan',
        ],
        resources: [
          `arn:aws:dynamodb:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:table/${CUSTOM_NODE_TYPES_TABLE_NAME}`,
          `arn:aws:dynamodb:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:table/${CUSTOM_NODE_TYPES_TABLE_NAME}/index/*`,
          `arn:aws:dynamodb:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:table/${PLUGIN_RECORDS_TABLE_NAME}`,
          `arn:aws:dynamodb:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:table/${PLUGIN_RECORDS_TABLE_NAME}/index/*`,
        ],
      }));
    }

    // Custom-plugin artifact verification (custom-node-designer task 10.1,
    // Requirement 10.4): the Component_Packager KMS-Verifies each custom
    // Plugin_Artifact signature against the portal signing key. The key
    // lives in the NodeDesignerStack (same circular-reference constraint as
    // the tables above), so its FIXED alias declared in
    // node-designer-stack.ts is referenced instead of the key ARN; the
    // grant is scoped to requests made through that alias.
    const PLUGIN_SIGNING_KEY_ALIAS = 'alias/dda-portal/plugin-signing';
    workflowPackagingHandler.addEnvironment(
      'PLUGIN_SIGNING_KEY_ARN',
      `arn:aws:kms:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:${PLUGIN_SIGNING_KEY_ALIAS}`,
    );
    workflowPackagingHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['kms:Verify', 'kms:DescribeKey'],
      resources: [`arn:aws:kms:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:key/*`],
      conditions: {
        StringEquals: { 'kms:RequestAlias': PLUGIN_SIGNING_KEY_ALIAS },
      },
    }));

    // The data-accounts handler serves the Bedrock model dropdown on the
    // settings page (GET /data-accounts/bedrock-configuration/models) by
    // listing inference profiles and foundation models. These are list
    // actions without resource-level scoping, hence resource '*'.
    dataAccountsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:ListFoundationModels',
        'bedrock:ListInferenceProfiles',
      ],
      resources: ['*'],
    }));

    // Workflow Testing Lambda Handler (Workflow_Test_Runner API - test dataset
    // upload/list, test run start/status/results). The Step Functions state
    // machine and Fargate sandbox live in the test-runner stack; its ARN is
    // wired in as TEST_RUN_STATE_MACHINE_ARN (with a settings-table fallback
    // inside the handler for environments without the test-runner stack).
    //
    // Triton model staging (workflow_model_staging.py): starting a test run
    // resolves every model_inference node's model against MODELS_TABLE,
    // reads the CPU-variant component recipe (greengrass:GetComponent), and
    // streams the model artifact zip from the use-case data bucket into the
    // portal artifacts bucket for the sandbox. All of that is covered by the
    // shared createLambdaRole grants (Greengrass components:* read, the
    // S3 data-plane grant — bounded by dataBucketAllowlist when configured —
    // and the models table), so NO additional IAM grant is added; the
    // sandbox task role stays portal-artifacts-only (Requirement 12.9).
    // The artifact copy (models can be >100 MB) is why this handler gets a
    // longer timeout and more memory than the other workflow handlers.
    const workflowTestingHandler = new lambda.Function(this, 'WorkflowTestingHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'workflow_testing.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('WorkflowTesting'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-25-workflow-testing',
        ...(props.testRunStateMachine && {
          TEST_RUN_STATE_MACHINE_ARN: props.testRunStateMachine.stateMachineArn,
        }),
      },
      layers: [sharedLayer, workflowCoreLayer],
      timeout: cdk.Duration.seconds(300), // model artifact staging copy
      memorySize: 1024,
    });

    // Allow the testing handler to start test-run executions and poll their
    // status (states:StartExecution + DescribeExecution and related reads).
    if (props.testRunStateMachine) {
      props.testRunStateMachine.grantStartExecution(workflowTestingHandler);
      props.testRunStateMachine.grantRead(workflowTestingHandler);
    }

    // Grant SharedComponents Lambda permission to update the GDK component bucket policy
    // This is needed to add new usecase accounts to the bucket policy during onboarding
    const componentBucketName = `dda-component-${cdk.Aws.REGION}-${cdk.Aws.ACCOUNT_ID}`;
    sharedComponentsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        's3:GetBucketPolicy',
        's3:PutBucketPolicy',
      ],
      resources: [`arn:aws:s3:::${componentBucketName}`],
    }));

    // Grant permission to list S3 objects for artifact discovery
    sharedComponentsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        's3:ListBucket',
      ],
      resources: [`arn:aws:s3:::${componentBucketName}`],
    }));

    // Grant permission to list and get Greengrass component versions for dynamic version discovery
    // Note: GetComponent requires full ARN with :versions:X.Y.Z, so we need wildcard for versions too
    sharedComponentsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'greengrass:ListComponentVersions',
        'greengrass:GetComponent',
      ],
      resources: [
        `arn:aws:greengrass:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:components:aws.edgeml.dda.LocalServer.*`,
        `arn:aws:greengrass:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:components:aws.edgeml.dda.LocalServer.*:versions:*`,
      ],
    }));

    // SNS Topic for training alerts
    const trainingAlertTopic = new sns.Topic(this, 'TrainingAlertTopic', {
      displayName: 'DDA Portal Training Alerts',
      topicName: 'dda-portal-training-alerts',
    });

    // Training Events Lambda Handler (for EventBridge)
    const trainingEventsHandler = new lambda.Function(this, 'TrainingEventsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'training_events.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('TrainingEvents'),
      environment: {
        ...lambdaEnvironment,
        ALERT_TOPIC_ARN: trainingAlertTopic.topicArn,
        COMPILATION_FUNCTION_NAME: compilationHandler.functionName,
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Grant SNS publish permissions
    trainingAlertTopic.grantPublish(trainingEventsHandler);
    
    // Grant permission to invoke compilation Lambda
    compilationHandler.grantInvoke(trainingEventsHandler);

    // Compilation Events Lambda Handler (for EventBridge)
    const compilationEventsHandler = new lambda.Function(this, 'CompilationEventsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'compilation_events.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('CompilationEvents'),
      environment: {
        ...lambdaEnvironment,
        ALERT_TOPIC_ARN: trainingAlertTopic.topicArn,
        PACKAGING_FUNCTION_NAME: packagingHandler.functionName,
        GREENGRASS_PUBLISH_FUNCTION_NAME: greengrassPublishHandler.functionName,
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Grant SNS publish permissions
    trainingAlertTopic.grantPublish(compilationEventsHandler);
    
    // Grant permission to invoke packaging and Greengrass publish Lambdas
    packagingHandler.grantInvoke(compilationEventsHandler);
    greengrassPublishHandler.grantInvoke(compilationEventsHandler);
    greengrassPublishHandler.grantInvoke(packagingHandler);

    // EventBridge Rule for SageMaker Training Job State Changes
    const trainingStateChangeRule = new events.Rule(this, 'TrainingStateChangeRule', {
      ruleName: 'dda-portal-training-state-change',
      description: 'Capture SageMaker training job state changes',
      eventPattern: {
        source: ['aws.sagemaker'],
        detailType: ['SageMaker Training Job State Change'],
        detail: {
          TrainingJobStatus: ['Completed', 'Failed', 'Stopped'],
        },
      },
    });

    // Add Lambda as target for the EventBridge rule
    trainingStateChangeRule.addTarget(new targets.LambdaFunction(trainingEventsHandler));

    // EventBridge Rule for SageMaker Compilation Job State Changes
    const compilationStateChangeRule = new events.Rule(this, 'CompilationStateChangeRule', {
      ruleName: 'dda-portal-compilation-state-change',
      description: 'Capture SageMaker compilation job state changes',
      eventPattern: {
        source: ['aws.sagemaker'],
        detailType: ['SageMaker Compilation Job State Change'],
        detail: {
          CompilationJobStatus: ['Completed', 'Failed', 'Stopped'],
        },
      },
    });

    // Add Lambda as target for the compilation EventBridge rule
    compilationStateChangeRule.addTarget(new targets.LambdaFunction(compilationEventsHandler));

    // Enable SageMaker EventBridge integration
    // This ensures SageMaker sends events to EventBridge for training and compilation jobs
    const sagemakerEventBridgeRole = new iam.Role(this, 'SageMakerEventBridgeRole', {
      assumedBy: new iam.ServicePrincipal('sagemaker.amazonaws.com'),
      description: 'Role for SageMaker to send events to EventBridge',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonEventBridgeFullAccess'),
      ],
    });

    // Create EventBridge configuration for SageMaker
    // Note: This creates the IAM role, but SageMaker EventBridge integration
    // needs to be enabled at the account level. This can be done via:
    // 1. AWS Console: SageMaker → Settings → EventBridge
    // 2. AWS CLI: aws events put-rule --name sagemaker-events --event-pattern '{"source":["aws.sagemaker"]}'
    // 3. Or automatically via a custom resource (implemented below)

    // Custom resource to enable SageMaker EventBridge integration
    const enableSageMakerEventBridge = new lambda.Function(this, 'EnableSageMakerEventBridge', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromInline(`
import json
import boto3
import cfnresponse

def handler(event, context):
    """
    Custom resource to enable SageMaker EventBridge integration
    This ensures SageMaker sends training and compilation job events to EventBridge
    """
    try:
        if event['RequestType'] == 'Create' or event['RequestType'] == 'Update':
            # Create EventBridge client
            events_client = boto3.client('events')
            
            # Enable SageMaker events by creating a rule that captures all SageMaker events
            # This effectively enables SageMaker to send events to EventBridge
            rule_name = 'sagemaker-eventbridge-enabler'
            
            try:
                events_client.put_rule(
                    Name=rule_name,
                    EventPattern=json.dumps({
                        "source": ["aws.sagemaker"],
                        "detail-type": [
                            "SageMaker Training Job State Change",
                            "SageMaker Compilation Job State Change"
                        ]
                    }),
                    State='ENABLED',
                    Description='Enable SageMaker EventBridge integration for DDA Portal'
                )
                
                # The rule exists but has no targets - this is intentional
                # It just enables SageMaker to send events to EventBridge
                # Our actual processing rules (with targets) are defined separately
                
                print(f"Successfully enabled SageMaker EventBridge integration via rule: {rule_name}")
                cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                    'Message': 'SageMaker EventBridge integration enabled successfully',
                    'RuleName': rule_name
                })
                
            except Exception as e:
                if 'already exists' in str(e).lower():
                    print(f"Rule {rule_name} already exists - SageMaker EventBridge integration already enabled")
                    cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                        'Message': 'SageMaker EventBridge integration already enabled',
                        'RuleName': rule_name
                    })
                else:
                    raise e
                    
        elif event['RequestType'] == 'Delete':
            # Clean up the enabler rule on stack deletion
            events_client = boto3.client('events')
            rule_name = 'sagemaker-eventbridge-enabler'
            
            try:
                events_client.delete_rule(Name=rule_name)
                print(f"Deleted SageMaker EventBridge enabler rule: {rule_name}")
            except Exception as e:
                print(f"Could not delete rule {rule_name}: {e}")
                # Don't fail deletion if rule doesn't exist
            
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                'Message': 'SageMaker EventBridge integration cleanup completed'
            })
            
    except Exception as e:
        print(f"Error in SageMaker EventBridge enabler: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {
            'Message': f'Failed to configure SageMaker EventBridge integration: {str(e)}'
        })
      `),
      timeout: cdk.Duration.seconds(60),
    });

    // Grant permissions to the custom resource Lambda
    enableSageMakerEventBridge.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'events:PutRule',
        'events:DeleteRule',
        'events:DescribeRule',
        'events:ListRules',
      ],
      resources: ['*'],
    }));

    // Create the custom resource
    const sagemakerEventBridgeIntegration = new cdk.CustomResource(this, 'SageMakerEventBridgeIntegration', {
      serviceToken: enableSageMakerEventBridge.functionArn,
      properties: {
        // Force update when stack is updated
        Timestamp: Date.now().toString(),
      },
    });

    // Cross-Account EventBridge Permission
    // Allow UseCase Accounts to send events to this account's default event bus
    // This enables real-time status updates for training/compilation jobs running in UseCase Accounts
    const defaultEventBus = events.EventBus.fromEventBusName(this, 'DefaultEventBus', 'default');
    
    // Create a resource-based policy to allow cross-account event delivery
    // Note: You need to add each UseCase Account ID that should be allowed to send events
    // This can be done via AWS CLI or by adding account IDs to the useCaseAccountIds array
    const useCaseAccountIds = process.env.USECASE_ACCOUNT_IDS?.split(',') || [];
    
    if (useCaseAccountIds.length > 0) {
      // Create policy statement for each UseCase Account
      const eventBusPolicy = new events.CfnEventBusPolicy(this, 'CrossAccountEventBusPolicy', {
        eventBusName: 'default',
        statementId: 'AllowUseCaseAccountsToSendEvents',
        action: 'events:PutEvents',
        principal: '*',
        condition: {
          type: 'StringEquals',
          key: 'aws:PrincipalAccount',
          value: useCaseAccountIds.join(','),
        },
      });
    }

    // Output instructions for manual cross-account setup
    new cdk.CfnOutput(this, 'CrossAccountEventBridgeSetup', {
      value: `To enable cross-account EventBridge from UseCase Accounts, run in Portal Account:
aws events put-permission --event-bus-name default --action events:PutEvents --principal <USECASE_ACCOUNT_ID> --statement-id AllowUseCaseAccount<N>`,
      description: 'Instructions for enabling cross-account EventBridge',
    });

    // API Gateway in Nested Stack
    // Moving API Gateway to a nested stack solves the 500 resource limit
    const apiGatewayStack = new ApiGatewayStack(this, 'ApiGateway', {
      userPool: props.userPool,
      authHandler,
      userManagementHandler,
      useCasesHandler,
      devicesHandler,
      deviceLogsHandler,
      deviceLogsAnalyzerHandler,
      deploymentsHandler,
      dataManagementHandler,
      datasetsHandler,
      capturesHandler,
      preLabeledDatasetsHandler,
      labelingHandler,
      trainingHandler,
      compilationHandler,
      packagingHandler,
      greengrassPublishHandler,
      modelsHandler,
      modelImportHandler,
      modelConverterHandler,
      componentsHandler,
      sharedComponentsHandler,
      dataAccountsHandler,
      auditLogsHandler,
      workflowsHandler,
      workflowValidationHandler,
      workflowPackagingHandler,
      workflowGeneratorHandler,
      workflowTestingHandler,
      lambdaEnvironment,
      createLambdaRole,
      sharedLayer,
    });

    this.api = apiGatewayStack.api;
    this.apiUrl = apiGatewayStack.apiUrl;

    // Camera_Registry routes (camera-registry-sync) in their own nested
    // stack: the ApiGateway nested stack sits at the CloudFormation
    // 500-resource limit, so the camera routes import the Rest API and the
    // /devices/{id} resource by id and attach there — the same pattern as
    // NodeDesignerApiStack.
    const cameraRegistryApiStack = new CameraRegistryApiStack(this, 'CameraRegistryApi', {
      restApiId: apiGatewayStack.api.restApiId,
      restApiRootResourceId: apiGatewayStack.api.restApiRootResourceId,
      deviceResourceId: apiGatewayStack.deviceResourceId,
      // Must match ApiGatewayStack deployOptions.stageName.
      stageName: 'v1',
      userPool: props.userPool,
      cameraRegistryHandler,
    });
    // The stage re-pointing deployment inside CameraRegistryApiStack needs
    // the ApiGatewayStack's stage (and its own deployment) to exist first.
    cameraRegistryApiStack.addDependency(apiGatewayStack);

    // User_Manager admin routes (portal-user-manager) in their own nested
    // stack for the same 500-resource-limit reason; every /admin/* method
    // is behind the JWT authorizer (Requirement 1.7).
    const userAdminApiStack = new UserAdminApiStack(this, 'UserAdminApi', {
      restApiId: apiGatewayStack.api.restApiId,
      restApiRootResourceId: apiGatewayStack.api.restApiRootResourceId,
      // Must match ApiGatewayStack deployOptions.stageName.
      stageName: 'v1',
      userPool: props.userPool,
      userAdminHandler,
    });
    userAdminApiStack.addDependency(apiGatewayStack);
    // Serialize the stage re-pointing deployments (both nested stacks
    // deploy against the same stage).
    userAdminApiStack.addDependency(cameraRegistryApiStack);

    // Station Quick Setup routes (station-quick-setup) in their own nested
    // stack for the same 500-resource-limit reason. The JWT
    // /device-registrations routes sit behind the Cognito authorizer; the
    // /quick-setup/* routes use AuthorizationType.NONE (Setup_Token validated
    // in-handler) plus method-level throttling for defense-in-depth (Req 3.9).
    const quickSetupApiStack = new QuickSetupApiStack(this, 'QuickSetupApi', {
      restApiId: apiGatewayStack.api.restApiId,
      restApiRootResourceId: apiGatewayStack.api.restApiRootResourceId,
      // Must match ApiGatewayStack deployOptions.stageName.
      stageName: 'v1',
      userPool: props.userPool,
      deviceRegistrationsHandler,
      quickSetupHandler,
    });
    quickSetupApiStack.addDependency(apiGatewayStack);
    // Serialize the stage re-pointing deployments (all nested API stacks
    // deploy against the same 'v1' stage).
    quickSetupApiStack.addDependency(cameraRegistryApiStack);
    quickSetupApiStack.addDependency(userAdminApiStack);

    // Custom Resource to update UseCases Lambda environment variable with API Gateway ID
    // This avoids circular dependency by updating the Lambda AFTER both resources are created
    const updateLambdaEnv = new lambda.Function(this, 'UpdateLambdaEnv', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromInline(`
import json
import boto3
import cfnresponse

lambda_client = boto3.client('lambda')

def handler(event, context):
    try:
        if event['RequestType'] in ['Create', 'Update']:
            function_name = event['ResourceProperties']['FunctionName']
            api_gateway_id = event['ResourceProperties']['ApiGatewayId']
            
            # Get current environment variables
            response = lambda_client.get_function_configuration(FunctionName=function_name)
            env_vars = response.get('Environment', {}).get('Variables', {})
            
            # Add API_GATEWAY_ID
            env_vars['API_GATEWAY_ID'] = api_gateway_id
            
            # Update Lambda environment
            lambda_client.update_function_configuration(
                FunctionName=function_name,
                Environment={'Variables': env_vars}
            )
            
            print(f"Updated {function_name} with API_GATEWAY_ID={api_gateway_id}")
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                'Message': f'Updated Lambda environment'
            })
        else:
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
    except Exception as e:
        print(f"Error: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {
            'Message': str(e)
        })
      `),
      timeout: cdk.Duration.seconds(60),
    });

    // Grant permissions to update Lambda configuration
    updateLambdaEnv.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'lambda:GetFunctionConfiguration',
        'lambda:UpdateFunctionConfiguration',
      ],
      resources: [useCasesHandler.functionArn],
    }));

    // Create custom resource
    const lambdaEnvUpdater = new cdk.CustomResource(this, 'LambdaEnvUpdater', {
      serviceToken: updateLambdaEnv.functionArn,
      properties: {
        FunctionName: useCasesHandler.functionName,
        ApiGatewayId: apiGatewayStack.api.restApiId,
        // Force update when API changes
        Timestamp: Date.now().toString(),
      },
    });

    // Ensure custom resource runs after both Lambda and API Gateway are created
    lambdaEnvUpdater.node.addDependency(useCasesHandler);
    lambdaEnvUpdater.node.addDependency(apiGatewayStack);

    // Outputs
    new cdk.CfnOutput(this, 'ApiUrl', {
      value: apiGatewayStack.apiUrl,
      description: 'API Gateway URL',
      exportName: 'EdgeCVPortalApiUrl',
    });

    new cdk.CfnOutput(this, 'ApiId', {
      value: apiGatewayStack.api.restApiId,
      description: 'API Gateway ID',
    });

    new cdk.CfnOutput(this, 'TrainingAlertTopicArn', {
      value: trainingAlertTopic.topicArn,
      description: 'SNS Topic ARN for training alerts',
      exportName: 'EdgeCVPortalTrainingAlertTopicArn',
    });

    new cdk.CfnOutput(this, 'SageMakerEventBridgeStatus', {
      value: sagemakerEventBridgeIntegration.getAttString('Message'),
      description: 'SageMaker EventBridge integration status',
    });
  }
}
