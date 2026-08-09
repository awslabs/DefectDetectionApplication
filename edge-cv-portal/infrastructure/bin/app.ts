#!/usr/bin/env node
import 'source-map-support/register';
import { execFileSync } from 'child_process';
import * as cdk from 'aws-cdk-lib';
import { AuthStack } from '../lib/auth-stack';
import { StorageStack } from '../lib/storage-stack';
import { ComputeStack } from '../lib/compute-stack';
import { TestRunnerStack } from '../lib/test-runner-stack';
import { NodeDesignerStack } from '../lib/node-designer-stack';
import { BuildFleetStack } from '../lib/build-fleet-stack';
import { FrontendStack } from '../lib/frontend-stack';

const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION || 'us-east-1',
};

// Authentication Stack
const authStack = new AuthStack(app, 'EdgeCVPortalAuthStack', {
  env,
  description: 'Authentication and authorization infrastructure for Edge CV Portal',
  ssoEnabled: process.env.SSO_ENABLED === 'true',
  ssoMetadataUrl: process.env.SSO_METADATA_URL,
  ssoProviderName: process.env.SSO_PROVIDER_NAME || 'CustomerSSO',
  domainPrefix: process.env.COGNITO_DOMAIN_PREFIX,
});

// Storage Stack (DynamoDB tables)
const storageStack = new StorageStack(app, 'EdgeCVPortalStorageStack', {
  env,
  description: 'Data storage infrastructure for Edge CV Portal',
});

// Test Runner Stack (Workflow_Test_Runner: Step Functions state machine +
// Fargate sandbox in an isolated subnet). The sandbox container image tag is
// configurable via `-c testSandboxImageTag=<tag>` (default: latest).
const testRunnerStack = new TestRunnerStack(app, 'EdgeCVPortalTestRunnerStack', {
  env,
  description: 'Workflow test-runner infrastructure (Step Functions + Fargate sandbox) for Edge CV Portal',
  testRunsTable: storageStack.testRunsTable,
  portalArtifactsBucket: storageStack.portalArtifactsBucket,
});

// Compute Stack (Lambda functions, API Gateway)
// Note: cloudFrontDomain is optional and can be set after initial deployment
// to enable automatic CORS configuration on Data Account buckets
const cloudFrontDomain = app.node.tryGetContext('cloudFrontDomain');
// Trusted UseCase account IDs that portal Lambdas may assume DDAPortalAccessRole
// into. Resolved (in priority order) from:
//   1. CDK context   `-c trustedUseCaseAccountIds=<id>,<id>`
//   2. env           `TRUSTED_USECASE_ACCOUNT_IDS` (forwarded by deploy-portal.sh)
//   3. SSM parameter `/dda-portal/trusted-usecase-account-ids`
// A comma-separated string; absence from all three yields an empty list, which
// the ComputeStack constructor rejects at synth time (safe default — no
// wildcard/self-account fallback). The env and SSM sources make the one-command
// deploy flow and the ComputeStack error's advertised SSM fallback actually work
// without editing the orchestrated deploy scripts.
function resolveTrustedUseCaseAccountIds(): string {
  const fromContext = app.node.tryGetContext('trustedUseCaseAccountIds');
  if (fromContext) {
    return String(fromContext);
  }
  const fromEnv = process.env.TRUSTED_USECASE_ACCOUNT_IDS;
  if (fromEnv && fromEnv.trim().length > 0) {
    return fromEnv;
  }
  // SSM fallback: the parameter the ComputeStack error message advertises.
  // Read synchronously via the AWS CLI (already required by the deploy scripts);
  // any failure (parameter absent, CLI/creds unavailable) leaves the list empty
  // so the ComputeStack surfaces its explicit, actionable error.
  try {
    const region =
      process.env.CDK_DEFAULT_REGION ||
      process.env.AWS_REGION ||
      process.env.AWS_DEFAULT_REGION ||
      'us-east-1';
    const value = execFileSync(
      'aws',
      [
        'ssm',
        'get-parameter',
        '--name',
        '/dda-portal/trusted-usecase-account-ids',
        '--query',
        'Parameter.Value',
        '--output',
        'text',
        '--region',
        region,
      ],
      { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'] }
    ).trim();
    if (value && value !== 'None') {
      return value;
    }
  } catch {
    // fall through to empty
  }
  return '';
}
const trustedUseCaseAccountIds: string[] = resolveTrustedUseCaseAccountIds()
  .split(',')
  .map((id: string) => id.trim())
  .filter((id: string) => id.length > 0);
// Optional allowlist of data buckets the portal Lambda roles may access on the
// S3 data plane. Comma-separated bucket names or ARNs via CDK context
// (`-c dataBucketAllowlist=bucket-a,bucket-b`). Empty/unset => all buckets
// (arn:aws:s3:::*) on the data plane (default; control-plane never granted).
const dataBucketAllowlist: string[] = (app.node.tryGetContext('dataBucketAllowlist') || '')
  .split(',')
  .map((b: string) => b.trim())
  .filter((b: string) => b.length > 0);
const computeStack = new ComputeStack(app, 'EdgeCVPortalComputeStack', {
  env,
  description: 'Compute and API infrastructure for Edge CV Portal',
  // The rendered template is near CloudFormation's hard 1 MB limit; emitting
  // it without JSON indentation keeps it well under (CDK's recommended fix).
  suppressTemplateIndentation: true,
  userPool: authStack.userPool,
  useCasesTable: storageStack.useCasesTable,
  userRolesTable: storageStack.userRolesTable,
  devicesTable: storageStack.devicesTable,
  auditLogTable: storageStack.auditLogTable,
  trainingJobsTable: storageStack.trainingJobsTable,
  labelingJobsTable: storageStack.labelingJobsTable,
  preLabeledDatasetsTable: storageStack.preLabeledDatasetsTable,
  modelsTable: storageStack.modelsTable,
  deploymentsTable: storageStack.deploymentsTable,
  settingsTable: storageStack.settingsTable,
  componentsTable: storageStack.componentsTable,
  sharedComponentsTable: storageStack.sharedComponentsTable,
  dataAccountsTable: storageStack.dataAccountsTable,
  workflowsTable: storageStack.workflowsTable,
  workflowVersionsTable: storageStack.workflowVersionsTable,
  testDatasetsTable: storageStack.testDatasetsTable,
  testRunsTable: storageStack.testRunsTable,
  workflowChatSessionsTable: storageStack.workflowChatSessionsTable,
  cameraRegistryTable: storageStack.cameraRegistryTable,
  deviceRegistrationsTable: storageStack.deviceRegistrationsTable,
  portalArtifactsBucket: storageStack.portalArtifactsBucket,
  testRunStateMachine: testRunnerStack.stateMachine,
  cloudFrontDomain,
  trustedUseCaseAccountIds,
  dataBucketAllowlist,
});

// Node Designer Stack (custom-node-designer: PluginRecords/CustomNodeTypes/
// ModuleIndexCache/SimulationRuns/NodeGenSessions tables, the plugin-artifact
// KMS signing key, per-architecture plugin CodeBuild projects + the
// repository-fetch project, EventBridge build-result delivery, the seven
// Node_Designer Lambda handlers, and their API routes registered against the
// ComputeStack API in a nested stack. The per-arch build image tag suffix is
// configurable via `-c pluginBuildImageTag=<suffix>`.
const nodeDesignerStack = new NodeDesignerStack(app, 'EdgeCVPortalNodeDesignerStack', {
  env,
  description: 'Custom node designer infrastructure (plugin builds, signing, simulator data, API) for Edge CV Portal',
  portalArtifactsBucket: storageStack.portalArtifactsBucket,
  useCasesTable: storageStack.useCasesTable,
  userRolesTable: storageStack.userRolesTable,
  auditLogTable: storageStack.auditLogTable,
  settingsTable: storageStack.settingsTable,
  workflowsTable: storageStack.workflowsTable,
  workflowVersionsTable: storageStack.workflowVersionsTable,
  testDatasetsTable: storageStack.testDatasetsTable,
  trustedUseCaseAccountIds,
  userPool: authStack.userPool,
  restApiId: computeStack.api.restApiId,
  restApiRootResourceId: computeStack.api.restApiRootResourceId,
  // Must match ApiGatewayStack deployOptions.stageName.
  apiStageName: 'v1',
});

// Build Fleet Stack (portal-build-fleet-and-workflow-gates: BuildJobs/
// BuildServers tables, the five build Lambdas, the 1-minute dispatcher
// schedule + build event rules, the /dda/portal-builds log group, the
// dda-portal-build-alerts SNS topic, the extended dda-build-role instance
// profile, and the /builds, /build-servers, /build-config routes registered
// against the ComputeStack API. The bootstrap repo URL is configurable via
// `-c buildRepoUrl=<url>`.
const buildFleetStack = new BuildFleetStack(app, 'EdgeCVPortalBuildFleetStack', {
  env,
  description: 'Portal build fleet infrastructure (build jobs, dedicated/ephemeral build compute, dispatcher, API) for Edge CV Portal',
  userRolesTable: storageStack.userRolesTable,
  auditLogTable: storageStack.auditLogTable,
  settingsTable: storageStack.settingsTable,
  userPool: authStack.userPool,
  restApiId: computeStack.api.restApiId,
  restApiRootResourceId: computeStack.api.restApiRootResourceId,
  // Must match ApiGatewayStack deployOptions.stageName.
  apiStageName: 'v1',
});

// Frontend Stack (CloudFront, S3)
const frontendStack = new FrontendStack(app, 'EdgeCVPortalFrontendStack', {
  env,
  description: 'Frontend hosting infrastructure for Edge CV Portal',
  apiUrl: computeStack.apiUrl,
  userPoolId: authStack.userPool.userPoolId,
  userPoolClientId: authStack.userPoolClient.userPoolClientId,
});

app.synth();
