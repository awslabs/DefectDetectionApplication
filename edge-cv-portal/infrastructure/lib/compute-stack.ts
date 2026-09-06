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
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import { Construct } from 'constructs';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import { execFileSync, execSync } from 'child_process';
import { ApiGatewayStack } from './api-gateway-stack';
import { CameraRegistryApiStack } from './camera-registry-api-stack';
import { UserAdminApiStack } from './user-admin-api-stack';
import { QuickSetupApiStack } from './quick-setup-api-stack';
import { DdaLabelingApiStack } from './dda-labeling-api-stack';
import { WorkflowManagerGapsApiStack } from './workflow-manager-gaps-api-stack';

export interface ComputeStackProps extends cdk.StackProps {
  userPool: cognito.UserPool;
  useCasesTable: dynamodb.Table;
  userRolesTable: dynamodb.Table;
  devicesTable: dynamodb.Table;
  auditLogTable: dynamodb.Table;
  trainingJobsTable: dynamodb.Table;
  labelingJobsTable: dynamodb.Table;
  /**
   * DDA Data Labeling tables (dda-data-labeling): teams
   * (dda-portal-labeling-teams) and per-image tasks
   * (dda-portal-labeling-tasks), created by the StorageStack (task 3.1).
   */
  labelingTeamsTable: dynamodb.Table;
  labelingTasksTable: dynamodb.Table;
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
  /**
   * DDA Data Labeling API handler (dda_labeling.py). Exported so the
   * DdaLabelingApiStack (task 14.2) can register the /labeling-teams*,
   * /labeler*, /labeling/{id}/stop and /labeling/{id}/review* routes
   * against it.
   */
  public readonly ddaLabelingHandler: lambda.Function;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    // Validate the trusted UseCase account list at synth time. An empty list
    // would otherwise produce an empty sts:AssumeRole resource list; the
    // design requires an explicit failure rather than any fallback to a
    // wildcard account (coupled to I1/I5).
    if (!props.trustedUseCaseAccountIds || props.trustedUseCaseAccountIds.length === 0) {
      throw new Error(
        'ComputeStack requires a non-empty trustedUseCaseAccountIds list ' +
          '(pass -c trustedUseCaseAccountIds=<id>,<id>, set the ' +
          'TRUSTED_USECASE_ACCOUNT_IDS environment variable, or the SSM ' +
          'parameter /dda-portal/trusted-usecase-account-ids). Refusing to ' +
          'synth an sts:AssumeRole grant on a wildcard account.'
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
      props.labelingTeamsTable.grantReadWriteData(role);
      props.labelingTasksTable.grantReadWriteData(role);
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
      //
      // greengrass:DeleteComponent backs the all-or-nothing rollback in
      // greengrass_publish.py: when a multi-target vLLM publish fails part way
      // through, the handler deletes only the component versions it created
      // itself seconds earlier in that same failed attempt, so no orphaned
      // version survives with no backing publish state. The action is confined
      // to the Greengrass components resource ARN (never a wildcard resource),
      // and the cross-account DDAPortalAccessRole in usecase-account-stack.ts
      // already grants exactly this action — adding it here closes an
      // inconsistency that only bit single-account setups rather than widening
      // the trust boundary (2.7, 2.8, 3.15).
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'greengrass:CreateComponentVersion',
          'greengrass:DeleteComponent',
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
      LABELING_TEAMS_TABLE: props.labelingTeamsTable.tableName,
      LABELING_TASKS_TABLE: props.labelingTasksTable.tableName,
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
      // blocks the JetPack variants. This map gives every lineage its own floor
      // (workflow support ships in current field builds, which sit well below
      // the legacy lineage numbers). This map MUST cover every
      // ARCH_TO_LOCAL_SERVER_COMPONENT key in workflow_packaging.py — the
      // backend coverage test (test_workflow_min_localserver_floor_coverage.py)
      // enforces it. Archs missing from the map resolve the safe '1.0.0'
      // per-lineage floor with a loud warning, never the cross-lineage scalar.
      // Keys are workflow_core arch ids.
      WORKFLOW_MIN_LOCAL_SERVER_VERSIONS: JSON.stringify({
        arm64_jp4: '1.0.0',
        arm64_jp5: '1.0.0',
        arm64_jp6: '1.0.0',
        arm64_jp7: '1.0.0',
        x86_64: '1.0.0',
        x86_64_nvidia: '1.0.0',
      }),
      COMPONENT_BUCKET_PREFIX: 'dda-component',
    };

    // Per-model Model_Image_Limit overrides for `llm:` auto-label requests
    // (llm-autolabel-prompt-tuning Req 7.1): a JSON object keyed by model
    // identifier, e.g. {"us.amazon.nova-pro-v1:0": 20, "tighter.model": 4}.
    // Set with -c llmModelImageLimits='{"model-id": 4}'; the default of {}
    // means every model resolves the shared default of 20. A missing,
    // non-integer or < 1 entry also resolves to the default in
    // resolve_model_image_limit, so configuration can never widen the bound
    // or drive it to zero images. Only the three handlers that build or
    // report `llm:` requests (DdaLabelingHandler preview, DdaAutolabelWorker
    // labeling time, DataAccountsHandler model options) carry this, so it is
    // deliberately not part of lambdaEnvironment.
    const llmModelImageLimitsContext =
      this.node.tryGetContext('llmModelImageLimits');
    const llmModelImageLimits =
      typeof llmModelImageLimitsContext === 'string'
        ? llmModelImageLimitsContext
        : llmModelImageLimitsContext
          ? JSON.stringify(llmModelImageLimitsContext)
          : '{}';

    // Per-model Model_Token_Limit bootstrap for `llm:` auto-label requests
    // (llm-model-token-and-image-sizing Req 1.8): a JSON object keyed by the
    // model identifier following the `llm:` prefix, e.g.
    // {"us.amazon.nova-pro-v1:0": 10000}. Set with
    // -c llmModelTokenLimits='{"model-id": 10000}'; the default of {} means
    // every model resolves the Model_Token_Limit_Default of 10000. A missing,
    // non-integer or out-of-range entry also resolves to that default in
    // resolve_token_budget, so configuration can never push a request past
    // Model_Token_Limit_Ceiling (128000) or down to zero tokens. Deliberately
    // built by the same string-passthrough / object-stringify / '{}' shape as
    // llmModelImageLimits, and carried by exactly the same three handlers that
    // build or report `llm:` requests (DdaLabelingHandler preview,
    // DdaAutolabelWorker labeling time, DataAccountsHandler model options), so
    // the delivery mechanism for both per-model settings is one mechanism
    // (Req 1.8) and the displayed budget equals the sent budget (Req 1.6).
    // It is a bootstrap only: the persisted llm_model_token_limits settings
    // item takes whole-mapping precedence over this value.
    const llmModelTokenLimitsContext =
      this.node.tryGetContext('llmModelTokenLimits');
    const llmModelTokenLimits =
      typeof llmModelTokenLimitsContext === 'string'
        ? llmModelTokenLimitsContext
        : llmModelTokenLimitsContext
          ? JSON.stringify(llmModelTokenLimitsContext)
          : '{}';

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
        // list_bedrock_model_options reports the resolved Model_Image_Limit
        // per option so the wizard's attach/omit hint uses the same source
        // the request paths do (llm-autolabel-prompt-tuning Req 7.1, 7.5).
        LLM_MODEL_IMAGE_LIMITS: llmModelImageLimits,
        // Same bootstrap for the resolved Model_Token_Limit reported beside
        // each option's image_limit, so the Effective_Token_Budget the wizard
        // pre-fills is resolved from the same source the preview and the
        // autolabel worker resolve it from
        // (llm-model-token-and-image-sizing Req 1.6, 1.8).
        LLM_MODEL_TOKEN_LIMITS: llmModelTokenLimits,
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

    // Portal-account IoT topic rule for the SINGLE-ACCOUNT topology (portal
    // account == use-case account): forwards every dda-camera-registry shadow
    // documents event to the shadow-report queue above. Cross-account use-case
    // accounts get the equivalent rule from the UseCaseAccountStack instead
    // (condition-gated there to the cross-account case). Deliberately distinct
    // rule name and CDK-generated role name so this never collides with the
    // fixed-name UseCaseAccountStack copies (or resources created manually
    // before this fix existed — see the camera-shadow-sync-provisioning spec
    // migration note).
    const cameraShadowRuleRole = new iam.Role(this, 'CameraShadowRuleRole', {
      assumedBy: new iam.ServicePrincipal('iot.amazonaws.com'),
      description:
        'Role for the portal-account dda-camera-registry shadow IoT topic rule ' +
        'to deliver shadow documents events to the DDA Portal shadow-report queue',
    });
    cameraShadowRuleRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SendCameraShadowReports',
      effect: iam.Effect.ALLOW,
      actions: ['sqs:SendMessage'],
      resources: [cameraShadowReportQueue.queueArn],
    }));

    new iot.CfnTopicRule(this, 'CameraRegistryShadowRule', {
      ruleName: 'dda_camera_registry_shadow_documents_portal',
      topicRulePayload: {
        // topic(3) is the thing name in $aws/things/{thing}/shadow/...
        sql: "SELECT *, topic(3) AS thing_name FROM '$aws/things/+/shadow/name/dda-camera-registry/update/documents'",
        awsIotSqlVersion: '2016-03-23',
        ruleDisabled: false,
        description:
          'Forward dda-camera-registry shadow documents events to the DDA ' +
          'Portal camera shadow-report queue (single-account topology)',
        actions: [
          {
            sqs: {
              queueUrl: cameraShadowReportQueue.queueUrl,
              roleArn: cameraShadowRuleRole.roleArn,
              useBase64: false,
            },
          },
        ],
      },
    });

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
    // finalize_audit_event (shared_utils.py) recovers the audit table's
    // (event_id, timestamp) range key with a dynamodb:Query before the
    // terminal update_item — the base createLambdaRole only grants
    // grantWriteData, so without this the deployed handler 500s after
    // every mutation's Cognito effect (user-manager-datalabeler-role,
    // design Decisions 3-4). Exactly the missing action, narrower than
    // grantReadData (no Scan/GetItem), scoped to this handler only.
    props.auditLogTable.grant(userAdminHandler, 'dynamodb:Query');
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
    // DDA Data Labeling (dda-data-labeling, task 14.1)
    // In-house labeling backend: dda_labeling.py (API), dda_labeling_worker.py
    // (async distribution / notifications / manifest generation),
    // dda_autolabel_worker.py (SQS consumer for Bedrock/SAM pre-labeling)
    // and the optional dda_sam_worker container-image Lambda.
    // Requirements: 6.5, 8.1, 12.8
    // ------------------------------------------------------------------

    // Pillow imaging layer for the labeling functions (mask rendering,
    // Image_Downscaler, dimension probes): bundled at synth time via CDK
    // asset bundling.
    //
    // BUGFIX (dda-imaging-layer-empty): this asset was previously the raw
    // backend/layers/imaging directory, relying on build.sh having been
    // run manually to create python/ before deploy — when it wasn't, an
    // EMPTY layer (only build.sh + requirements.txt) synthesized and
    // deployed cleanly, and the DDA labeling functions failed at runtime
    // with "No module named 'PIL'". Bundling makes synth itself produce
    // the populated layer: the local tryBundle path runs pip on the host
    // with the same manylinux wheel targeting as build.sh (fast, no Docker
    // needed); if it fails, CDK falls back to the Docker bundling image.
    // The bundling options mirror the SyntheticDataStack's
    // SyntheticImagingLayer byte-for-byte so both stacks stage one
    // identical asset (equal Content.S3Key). build.sh remains valid for
    // manual/standalone layer builds.
    const imagingLayerSourceDir = path.join(__dirname, '../../backend/layers/imaging');
    const imagingLayer = new lambda.LayerVersion(this, 'ImagingLayer', {
      code: lambda.Code.fromAsset(imagingLayerSourceDir, {
        bundling: {
          image: lambda.Runtime.PYTHON_3_11.bundlingImage,
          command: [
            'bash',
            '-c',
            'pip install -r requirements.txt -t /asset-output/python',
          ],
          local: {
            tryBundle(outputDir: string): boolean {
              try {
                // Same wheel targeting as build.sh: Pillow ships native
                // extensions, so force the manylinux wheel matching the
                // Lambda runtime (Python 3.11, x86_64) regardless of host.
                execSync(
                  [
                    'pip install',
                    `-r ${path.join(imagingLayerSourceDir, 'requirements.txt')}`,
                    `-t ${path.join(outputDir, 'python')}`,
                    '--platform manylinux2014_x86_64',
                    '--implementation cp',
                    '--python-version 3.11',
                    '--only-binary=:all:',
                  ].join(' '),
                  { stdio: ['ignore', 'pipe', 'pipe'] }
                );
                return true;
              } catch {
                // Fall back to the Docker bundling image.
                return false;
              }
            },
          },
        },
      }),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description:
        'Pillow imaging layer for DDA labeling mask rendering (built by ' +
        'backend/layers/imaging/build.sh)',
    });

    // Auto-label queue + DLQ (camera-shadow queue pattern). The visibility
    // timeout equals the consumer Lambda timeout (300 s) so a message is
    // never redelivered while its Bedrock/SAM inference is still running;
    // after 3 failed receives the message dead-letters.
    const ddaAutolabelDlq = new sqs.Queue(this, 'DdaAutolabelDLQ', {
      queueName: 'dda-portal-autolabel-queue-dlq',
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });

    const ddaAutolabelQueue = new sqs.Queue(this, 'DdaAutolabelQueue', {
      queueName: 'dda-portal-autolabel-queue',
      visibilityTimeout: cdk.Duration.seconds(300),
      retentionPeriod: cdk.Duration.days(4),
      enforceSSL: true,
      deadLetterQueue: {
        queue: ddaAutolabelDlq,
        maxReceiveCount: 3,
      },
    });

    // Async worker (dda_labeling_worker.py): task distribution/rebalancing,
    // SES notifications, and manifest generation (segmentation mask
    // rendering with Pillow is why it gets 2 GB / 900 s).
    const ddaLabelingWorker = new lambda.Function(this, 'DdaLabelingWorker', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'dda_labeling_worker.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('DdaLabelingWorker'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-27-dda-labeling',
        AUTOLABEL_QUEUE_URL: ddaAutolabelQueue.queueUrl,
        // Same verified SES sender the User_Manager uses; unset => the
        // worker records notifications_skipped=true and proceeds (Req 6.5).
        SES_SENDER_ADDRESS: sesSenderAddress.valueAsString,
        // Portal frontend domain for the labeler-workspace deep links in
        // notification emails (https://{PORTAL_DOMAIN}/labeler?job={job_id}).
        // Same optional cloudFrontDomain source the data-management handler
        // uses for CLOUDFRONT_DOMAIN; absent until the frontend stack exists.
        ...(props.cloudFrontDomain && { PORTAL_DOMAIN: props.cloudFrontDomain }),
      },
      layers: [sharedLayer, imagingLayer],
      timeout: cdk.Duration.seconds(900),
      memorySize: 2048,
    });

    // The distributor fans one auto-label message per image onto the queue.
    ddaAutolabelQueue.grantSendMessages(ddaLabelingWorker);

    // Assignment-notification emails (identity/* — the user_admin pattern:
    // SES authorizes SendEmail against the address or covering domain
    // identity of the Source address).
    ddaLabelingWorker.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['ses:SendEmail'],
      resources: [
        `arn:aws:ses:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:identity/*`,
      ],
    }));

    // API handler (dda_labeling.py): team management, DDA job creation
    // (delegated from labeling.py), labeler task APIs, admin review.
    const ddaLabelingHandler = new lambda.Function(this, 'DdaLabelingHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'dda_labeling.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('DdaLabeling'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-27-dda-labeling',
        DDA_LABELING_WORKER_FUNCTION_NAME: ddaLabelingWorker.functionName,
        // Per-model Model_Image_Limit for few-shot attachment in
        // Prompt_Tuning_Preview requests (llm-autolabel-prompt-tuning
        // Req 7.1) — the same value the autolabel worker resolves, so
        // preview and labeling time attach the same example subset.
        LLM_MODEL_IMAGE_LIMITS: llmModelImageLimits,
        // Per-model Model_Token_Limit bootstrap for the Effective_Token_Budget
        // resolved once at Preview_Run start; the same value the autolabel
        // worker reads, which is what makes the previewed and the labeling-time
        // maxTokens equal (llm-model-token-and-image-sizing Req 1.6, 1.8).
        LLM_MODEL_TOKEN_LIMITS: llmModelTokenLimits,
      },
      // The same imagingLayer LayerVersion DdaLabelingWorker attaches above —
      // one Pillow/libjpeg-turbo/zlib build shared by every function that runs
      // the Image_Downscaler, which is the environmental precondition for the
      // byte-identical downscale output the preview and the autolabel worker
      // must produce (llm-model-token-and-image-sizing Req 6.1, 6.6). Pillow is
      // imported lazily inside the re-encode path, so Downscale_Off pays
      // nothing for the attachment.
      layers: [sharedLayer, imagingLayer],
      // 900 s is a cap, not a reservation: the HTTP routes still answer in
      // well under a second, but the same function also runs the async
      // Prompt_Tuning_Preview executor, which is up to 5 sequential model
      // invocations at 120 s each plus S3 reads
      // (llm-autolabel-prompt-tuning Req 3.3).
      timeout: cdk.Duration.seconds(900),
      // Up from the 128 MB default: the Image_Downscaler accepts sources up to
      // Max_Source_Pixel_Count (100 M pixels), whose Pillow decode buffer plus
      // the resized buffer and the encoder working set peaks in the 400-500 MB
      // region, which 128 MB cannot hold at all. 2048 MB also sits above the
      // ~1769 MB point where Lambda allocates a full vCPU, and CPU is what
      // decides whether the 5 s Downscale_Duration_Bound is met; it matches
      // DdaLabelingWorker's allocation so there is one number for Pillow in
      // this stack (llm-model-token-and-image-sizing Req 6.11).
      memorySize: 2048,
    });
    this.ddaLabelingHandler = ddaLabelingHandler;

    // Job creation / membership changes async-invoke the worker
    // ({action: 'distribute'|'generate_manifest'}).
    ddaLabelingWorker.grantInvoke(ddaLabelingHandler);

    // Prompt_Tuning_Preview: POST /labeling-preview/runs validates and
    // returns 202, then async-invokes THIS function with
    // {action: 'execute_preview_run'} to run the samples past API Gateway's
    // 29 s integration bound (llm-autolabel-prompt-tuning Req 3.3). The
    // executor resolves its own name from context.function_name — an
    // environment variable would be a CloudFormation self-reference.
    //
    // The self-invoke grant CANNOT go through grantInvoke: that statement
    // lands in a policy owned by the role construct, and the Lambda function
    // depends on its role's whole subtree, so a policy referencing the
    // function's ARN closes a dependency cycle
    // (policy -> function -> policy) and the template becomes undeployable.
    // A standalone policy attached to the same role is a sibling of the
    // function instead of a child of the role, so the only edge left is
    // policy -> function.
    new iam.Policy(this, 'DdaLabelingSelfInvokePolicy', {
      roles: [ddaLabelingHandler.role!],
      statements: [
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['lambda:InvokeFunction'],
          resources: [
            ddaLabelingHandler.functionArn,
            `${ddaLabelingHandler.functionArn}:*`,
          ],
        }),
      ],
    });

    // Bedrock vision pre-labeling for preview runs, with the same
    // foundation-model + inference-profile resource scope the autolabel
    // worker uses (the model id is request configuration, not a fixed ARN).
    // Preview calls the identical shared invocation path the Auto_Labeler
    // does, so it needs the identical grant
    // (llm-autolabel-prompt-tuning Req 3.3).
    ddaLabelingHandler.addToRolePolicy(new iam.PolicyStatement({
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

    // labeling.py delegates DDA job creation in-process to
    // dda_labeling.create_dda_job (import, same Lambda), which async-invokes
    // the worker — so the existing LabelingHandler needs the same wiring.
    labelingHandler.addEnvironment(
      'DDA_LABELING_WORKER_FUNCTION_NAME',
      ddaLabelingWorker.functionName,
    );
    ddaLabelingWorker.grantInvoke(labelingHandler);

    // Team-member resolution (member identities/emails come from Cognito):
    // the handler validates the Data_Labeler role on add-member and the
    // worker resolves recipient emails for notifications.
    //
    // labelingHandler needs the same grant: POST /labeling runs there and
    // delegates in-process to create_dda_job, which re-resolves every team
    // member's Data_Labeler role from Cognito at creation time (Req 4.8).
    // Without it every member lookup fails closed and job creation is
    // rejected with "no members with the Data_Labeler role".
    for (const fn of [labelingHandler, ddaLabelingHandler, ddaLabelingWorker]) {
      fn.addToRolePolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'cognito-idp:ListUsers',
          'cognito-idp:AdminGetUser',
        ],
        resources: [props.userPool.userPoolArn],
      }));
    }

    // Auto-label SQS consumer (dda_autolabel_worker.py): Bedrock Converse /
    // SAM pre-labeling per image. Returns partial batch responses, hence
    // reportBatchItemFailures.
    const ddaAutolabelWorker = new lambda.Function(this, 'DdaAutolabelWorker', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'dda_autolabel_worker.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('DdaAutolabelWorker'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2026-02-27-dda-labeling',
        // Per-model Model_Image_Limit bounding few-shot example attachment
        // at labeling time (llm-autolabel-prompt-tuning Req 7.1); shared
        // with the preview path so both attach the same example subset.
        LLM_MODEL_IMAGE_LIMITS: llmModelImageLimits,
        // Per-model Model_Token_Limit bootstrap for jobs whose record carries
        // no valid Token_Budget_Selection; delivered through the same mechanism
        // as LLM_MODEL_IMAGE_LIMITS and read from the same persisted settings
        // item the preview reads (llm-model-token-and-image-sizing Req 1.6, 1.8).
        LLM_MODEL_TOKEN_LIMITS: llmModelTokenLimits,
      },
      // The same imagingLayer LayerVersion DdaLabelingWorker and
      // DdaLabelingHandler attach: one Pillow build across every function that
      // runs the Image_Downscaler, so the preview and labeling time emit
      // byte-identical downscaled bytes for the same source and setting
      // (llm-model-token-and-image-sizing Req 6.1, 6.6).
      layers: [sharedLayer, imagingLayer],
      timeout: cdk.Duration.seconds(300),
      // Up from the 128 MB default, for the same reason as
      // DdaLabelingHandler: a worst-case accepted source (100 M pixels) peaks
      // around 400-500 MB across the Pillow decode buffer, the resized buffer
      // and the encoder working set, and 2048 MB is above the ~1769 MB
      // full-vCPU threshold that the 5 s Downscale_Duration_Bound depends on
      // (llm-model-token-and-image-sizing Req 6.11). Negligible cost here:
      // these invocations are already dominated by a 120 s model call.
      memorySize: 2048,
    });

    ddaAutolabelWorker.addEventSource(new SqsEventSource(ddaAutolabelQueue, {
      batchSize: 5,
      reportBatchItemFailures: true,
    }));

    // Bedrock vision pre-labeling via the Converse API (authorized by
    // bedrock:InvokeModel). The model id is runtime job configuration, so
    // the grant covers foundation models and inference profiles rather than
    // a fixed model ARN (workflow-generator pattern).
    ddaAutolabelWorker.addToRolePolicy(new iam.PolicyStatement({
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

    // SAM worker (dda_sam_worker): container-image Lambda bundling a CPU
    // ONNX SAM variant (backend/sam-worker/Dockerfile). Building the image
    // downloads the model archive and produces a multi-GB Docker build, so
    // it is gated behind the `deploySamWorker` CDK context flag
    // (-c deploySamWorker=true; default OFF) — ordinary portal deployments
    // must not require Docker or the model download. When disabled,
    // SAM_WORKER_FUNCTION_NAME is simply absent from the autolabel worker
    // environment and SAM-model jobs report pre-label failures instead.
    const deploySamWorkerContext = this.node.tryGetContext('deploySamWorker');
    const deploySamWorker =
      deploySamWorkerContext === true || deploySamWorkerContext === 'true';
    if (deploySamWorker) {
      // The bundled model is overridable per deployment
      // (-c samModelArchiveUrl=<zip-with-onnx-exports>). The Dockerfile
      // default is MobileSAM (fast, coarse); sam_vit_b_01ec64.zip from
      // the same HuggingFace repo is a substantially better fit when
      // pre-label quality matters. ViT-L/ViT-H exports exceed the
      // autolabel worker's 120 s per-image invocation bound (Req 8.5)
      // on CPU and must not be deployed here.
      const samModelArchiveUrl =
        this.node.tryGetContext('samModelArchiveUrl') as string | undefined;

      // Image platform and function architecture are pinned together and
      // explicitly: with neither set, the image inherits the build host's
      // architecture while the function silently stays x86_64, so building
      // on an ARM host (e.g. the arm64 build server this repo ships)
      // produces a function that fails every invoke with
      // Runtime.InvalidEntrypoint / ProcessSpawnFailed. Pinning amd64
      // keeps the SAM worker consistent with every other Lambda in this
      // stack; on an ARM build host the image is produced through qemu
      // emulation, which is slower but correct.
      const ddaSamWorker = new lambda.DockerImageFunction(this, 'DdaSamWorker', {
        code: lambda.DockerImageCode.fromImageAsset(
          path.join(__dirname, '../../backend/sam-worker'),
          {
            platform: ecrAssets.Platform.LINUX_AMD64,
            ...(samModelArchiveUrl
              ? { buildArgs: { SAM_MODEL_ARCHIVE_URL: samModelArchiveUrl } }
              : {}),
          },
        ),
        architecture: lambda.Architecture.X86_64,
        description:
          'DDA labeling SAM pre-label worker (CPU ONNX SAM region proposals)',
        memorySize: 10240,
        timeout: cdk.Duration.seconds(300),
        // Automatic-mask-generation tuning (read by handler.py, all
        // env-var overridable). Denser prompt grid + stricter IoU keep
        // than the handler defaults (8 / 0.7): small-defect datasets
        // need the extra prompts to hit thin regions, and the stricter
        // keep discards the junk proposals a denser grid also produces.
        // The lower area floor admits thin structures (cracks/gaps)
        // that a 0.05% floor would drop.
        environment: {
          SAM_POINTS_PER_SIDE: '16',
          SAM_PRED_IOU_THRESHOLD: '0.88',
          SAM_MIN_AREA_FRACTION: '0.0002',
        },
      });

      // Synchronous invoke from the autolabel worker (bounded at 120 s
      // wall-clock in dda_autolabel_worker.py). The image is passed inline
      // (base64) or as a presigned URL, so the SAM worker needs no S3/table
      // grants of its own.
      ddaAutolabelWorker.addEnvironment(
        'SAM_WORKER_FUNCTION_NAME',
        ddaSamWorker.functionName,
      );
      ddaSamWorker.grantInvoke(ddaAutolabelWorker);
    }

    // Grounded-SAM worker (dda_grounded_sam_worker): container-image Lambda
    // bundling CPU ONNX Grounding DINO + a SAM mask model
    // (backend/grounded-sam-worker/Dockerfile). Building the image downloads
    // the DINO model, its tokenizer, and the SAM archive and produces a
    // multi-GB Docker build, so it is gated behind the
    // `deployGroundedSamWorker` CDK context flag
    // (-c deployGroundedSamWorker=true; default OFF) — ordinary portal
    // deployments must not require Docker or the model downloads. When
    // disabled, GROUNDED_SAM_WORKER_FUNCTION_NAME is simply absent from the
    // autolabel worker environment and grounded-sam jobs report pre-label
    // failures instead.
    const deployGroundedSamWorkerContext =
      this.node.tryGetContext('deployGroundedSamWorker');
    const deployGroundedSamWorker =
      deployGroundedSamWorkerContext === true ||
      deployGroundedSamWorkerContext === 'true';
    if (deployGroundedSamWorker) {
      // The baked models are overridable per deployment
      // (-c groundedSamDinoModelUrl=... / -c groundedSamDinoTokenizerUrl=...
      // / -c groundedSamModelArchiveUrl=...). Each build arg is passed only
      // when its context value is set, so the Dockerfile's pinned defaults
      // (grounding-dino-tiny ONNX, its tokenizer.json, and the MobileSAM
      // archive) stay in force otherwise.
      const dinoModelUrl =
        this.node.tryGetContext('groundedSamDinoModelUrl') as string | undefined;
      const dinoTokenizerUrl =
        this.node.tryGetContext('groundedSamDinoTokenizerUrl') as string | undefined;
      const samArchiveUrl =
        this.node.tryGetContext('groundedSamModelArchiveUrl') as string | undefined;

      // Image platform and function architecture are pinned together and
      // explicitly: with neither set, the image inherits the build host's
      // architecture while the function silently stays x86_64, so building
      // on an ARM host (e.g. the arm64 build server this repo ships)
      // produces a function that fails every invoke with
      // Runtime.InvalidEntrypoint / ProcessSpawnFailed. Pinning amd64
      // keeps the Grounded-SAM worker consistent with every other Lambda
      // in this stack; on an ARM build host the image is produced through
      // qemu emulation, which is slower but correct.
      const ddaGroundedSamWorker = new lambda.DockerImageFunction(
        this,
        'DdaGroundedSamWorker',
        {
          code: lambda.DockerImageCode.fromImageAsset(
            path.join(__dirname, '../../backend/grounded-sam-worker'),
            {
              platform: ecrAssets.Platform.LINUX_AMD64,
              buildArgs: {
                ...(dinoModelUrl
                  ? { GROUNDING_DINO_MODEL_URL: dinoModelUrl }
                  : {}),
                ...(dinoTokenizerUrl
                  ? { GROUNDING_DINO_TOKENIZER_URL: dinoTokenizerUrl }
                  : {}),
                ...(samArchiveUrl
                  ? { SAM_MODEL_ARCHIVE_URL: samArchiveUrl }
                  : {}),
              },
            },
          ),
          architecture: lambda.Architecture.X86_64,
          description:
            'DDA labeling Grounded-SAM pre-label worker (CPU ONNX Grounding DINO + SAM)',
          memorySize: 10240,
          timeout: cdk.Duration.seconds(300),
          // No threshold environment block: the handler's own defaults
          // (box 0.35, text 0.25, NMS IoU 0.8, max detections 20) are the
          // intended values for this worker — contrast DdaSamWorker, whose
          // tuned grid values differ from its handler defaults. Retuning
          // is a Lambda env-var change, not a redeploy.
        },
      );

      // Synchronous invoke from the autolabel worker (bounded at 240 s
      // wall-clock in dda_autolabel_worker.py — CPU Grounding DINO latency;
      // the sam family's 120 s bound is untouched). The image is passed as
      // a presigned URL, so the grounded-sam worker needs no S3/table
      // grants of its own.
      ddaAutolabelWorker.addEnvironment(
        'GROUNDED_SAM_WORKER_FUNCTION_NAME',
        ddaGroundedSamWorker.functionName,
      );
      ddaGroundedSamWorker.grantInvoke(ddaAutolabelWorker);
    }

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
    //
    // Fixed function name so the Lambda's environment can carry its own
    // function name (async submit/worker self-invocation,
    // workflow-manager-gaps) and the role can scope lambda:InvokeFunction to
    // exactly this function — a CloudFormation resource cannot Ref itself
    // from its own Environment, so the name must be a synth-time constant.
    // Same fixed-name pattern as the SyntheticDataStack handler.
    const WORKFLOW_GENERATOR_FUNCTION_NAME = 'dda-portal-workflow-generator';
    const workflowGeneratorFunctionArn =
      `arn:aws:lambda:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}` +
      `:function:${WORKFLOW_GENERATOR_FUNCTION_NAME}`;
    const workflowGeneratorHandler = new lambda.Function(this, 'WorkflowGeneratorHandler', {
      functionName: WORKFLOW_GENERATOR_FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'workflow_generator.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('WorkflowGenerator'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-25-workflow-generator',
        // Self-invocation target for the async generation worker
        // (submit_generation Event-invokes this same function with a
        // workflow_gen_worker payload; the handler falls back to
        // AWS_LAMBDA_FUNCTION_NAME when unset).
        WORKFLOW_GENERATOR_FUNCTION_NAME,
      },
      layers: [sharedLayer, workflowCoreLayer],
      // Bedrock invocation timeout is configurable up to 240s; leave headroom
      timeout: cdk.Duration.seconds(270),
    });

    // Async submit/poll generation (workflow-manager-gaps): grant the
    // generator role lambda:InvokeFunction on its own function so
    // submit_generation can Event-invoke the background worker (the
    // ddaLabelingWorker.grantInvoke pattern, self-directed). The grant uses
    // the fixed-name ARN rather than grantInvoke(self) because the Function
    // resource depends on its role's default policy — referencing the
    // function ARN token from there would be a dependency cycle (see
    // NodeGeneratorSelfInvokePolicy in node-designer-stack.ts).
    workflowGeneratorHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['lambda:InvokeFunction'],
      resources: [workflowGeneratorFunctionArn],
    }));

    // Never retry the async generation worker: an abnormally terminated
    // worker must surface as failed via the status endpoint's reaper
    // (GENERATION_ABNORMAL_TERMINATION) rather than being silently re-run
    // after the reaper already fired (Req 3.6). Synthesizes an
    // AWS::Lambda::EventInvokeConfig with MaximumRetryAttempts: 0.
    workflowGeneratorHandler.configureAsyncInvoke({
      retryAttempts: 0,
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

    // DDA data labeling routes (dda-data-labeling, task 14.2) in their own
    // nested stack for the same 500-resource-limit reason. /labeling-teams*
    // and /labeler* attach at the imported API root; /labeling/{id}/stop and
    // /labeling/{id}/review* attach under the ApiGatewayStack-owned
    // /labeling/{id} resource via its exported resource id. Requests are
    // JWT-authorized at the gateway; RBAC (Requirements 2.5, 3.7, 11.4) is
    // enforced in dda_labeling.py / labeling.py.
    const ddaLabelingApiStack = new DdaLabelingApiStack(this, 'DdaLabelingApi', {
      restApiId: apiGatewayStack.api.restApiId,
      restApiRootResourceId: apiGatewayStack.api.restApiRootResourceId,
      labelingJobResourceId: apiGatewayStack.labelingJobResourceId,
      // Must match ApiGatewayStack deployOptions.stageName.
      stageName: 'v1',
      userPool: props.userPool,
      ddaLabelingHandler,
      labelingHandler,
    });
    ddaLabelingApiStack.addDependency(apiGatewayStack);
    // Serialize the stage re-pointing deployments (all nested API stacks
    // deploy against the same 'v1' stage).
    ddaLabelingApiStack.addDependency(cameraRegistryApiStack);
    ddaLabelingApiStack.addDependency(userAdminApiStack);
    ddaLabelingApiStack.addDependency(quickSetupApiStack);

    // Workflow Manager gaps routes (workflow-manager-gaps, task 10.3) in
    // their own nested stack for the same 500-resource-limit reason.
    // GET /workflows/generate/{job_id} (generation job status) attaches
    // under the ApiGatewayStack-owned /workflows/generate resource and
    // PATCH /workflows/{id}/name (metadata-only rename) under
    // /workflows/{id}, both via their exported resource ids.
    const workflowManagerGapsApiStack = new WorkflowManagerGapsApiStack(
      this,
      'WorkflowManagerGapsApi',
      {
        restApiId: apiGatewayStack.api.restApiId,
        restApiRootResourceId: apiGatewayStack.api.restApiRootResourceId,
        workflowGenerateResourceId: apiGatewayStack.workflowGenerateResourceId,
        workflowResourceId: apiGatewayStack.workflowResourceId,
        // Must match ApiGatewayStack deployOptions.stageName.
        stageName: 'v1',
        userPool: props.userPool,
        workflowGeneratorHandler,
        workflowsHandler,
      },
    );
    workflowManagerGapsApiStack.addDependency(apiGatewayStack);
    // Serialize the stage re-pointing deployments (all nested API stacks
    // deploy against the same 'v1' stage).
    workflowManagerGapsApiStack.addDependency(cameraRegistryApiStack);
    workflowManagerGapsApiStack.addDependency(userAdminApiStack);
    workflowManagerGapsApiStack.addDependency(quickSetupApiStack);
    workflowManagerGapsApiStack.addDependency(ddaLabelingApiStack);

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
