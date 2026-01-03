#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AuthStack } from '../lib/auth-stack';
import { StorageStack } from '../lib/storage-stack';
import { ComputeStack } from '../lib/compute-stack';
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

// Compute Stack (Lambda functions, API Gateway)
const computeStack = new ComputeStack(app, 'EdgeCVPortalComputeStack', {
  env,
  description: 'Compute and API infrastructure for Edge CV Portal',
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
