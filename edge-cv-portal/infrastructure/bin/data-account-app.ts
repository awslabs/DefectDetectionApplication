#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { DataAccountStack } from '../lib/data-account-stack';

/**
 * CDK App for deploying the Data Account access roles and bucket policies.
 * 
 * This is a standalone app that should be deployed in Data Accounts
 * to grant access for:
 * 1. Portal Account - to manage S3 buckets and data
 * 2. UseCase Accounts - for SageMaker to read training data directly
 * 
 * Usage:
 * 
 * Basic (roles only):
 *   cdk deploy -a "npx ts-node bin/data-account-app.ts" \
 *     -c portalAccountId=111111111111 \
 *     -c usecaseAccountIds=222222222222 \
 *     -c externalId=your-uuid-here
 * 
 * With bucket policies (recommended for SageMaker cross-account access):
 *   cdk deploy -a "npx ts-node bin/data-account-app.ts" \
 *     -c portalAccountId=111111111111 \
 *     -c usecaseAccountIds=222222222222 \
 *     -c externalId=your-uuid-here \
 *     -c dataBucketNames=dda-cookie-bucket,dda-training-data
 * 
 * Multiple UseCase Accounts (comma-separated):
 *   cdk deploy -a "npx ts-node bin/data-account-app.ts" \
 *     -c portalAccountId=111111111111 \
 *     -c usecaseAccountIds=222222222222,333333333333 \
 *     -c externalId=your-uuid-here \
 *     -c dataBucketNames=dda-cookie-bucket
 */

const app = new cdk.App();

// Get configuration from context
const portalAccountId = app.node.tryGetContext('portalAccountId');
const usecaseAccountIdsStr = app.node.tryGetContext('usecaseAccountIds');
const externalId = app.node.tryGetContext('externalId');
const dataBucketNamesStr = app.node.tryGetContext('dataBucketNames');

// Validate required parameters
if (!portalAccountId) {
  console.error('❌ ERROR: portalAccountId is required');
  console.error('   Usage: cdk deploy -a "npx ts-node bin/data-account-app.ts" -c portalAccountId=111111111111 -c usecaseAccountIds=222222222222 -c externalId=your-uuid');
  process.exit(1);
}

if (!usecaseAccountIdsStr) {
  console.error('❌ ERROR: usecaseAccountIds is required');
  console.error('   Usage: cdk deploy -a "npx ts-node bin/data-account-app.ts" -c portalAccountId=111111111111 -c usecaseAccountIds=222222222222 -c externalId=your-uuid');
  console.error('   For multiple accounts: -c usecaseAccountIds=222222222222,333333333333');
  process.exit(1);
}

if (!externalId) {
  console.error('❌ ERROR: externalId is required for production security');
  console.error('   Generate a UUID: uuidgen');
  console.error('   Usage: cdk deploy -a "npx ts-node bin/data-account-app.ts" -c portalAccountId=111111111111 -c usecaseAccountIds=222222222222 -c externalId=your-uuid');
  console.error('');
  console.error('   ⚠️  IMPORTANT: Save this external ID securely!');
  console.error('   You will need to provide it when onboarding UseCases that use this Data Account.');
  process.exit(1);
}

// Parse usecase account IDs (comma-separated)
const usecaseAccountIds = usecaseAccountIdsStr.split(',').map((id: string) => id.trim());

// Parse data bucket names (comma-separated, optional)
const dataBucketNames = dataBucketNamesStr 
  ? dataBucketNamesStr.split(',').map((name: string) => name.trim())
  : undefined;

// Validate account ID format
const accountIdRegex = /^\d{12}$/;
if (!accountIdRegex.test(portalAccountId)) {
  console.error(`❌ ERROR: Invalid portalAccountId format: ${portalAccountId}`);
  console.error('   AWS Account IDs must be 12 digits');
  process.exit(1);
}

for (const accountId of usecaseAccountIds) {
  if (!accountIdRegex.test(accountId)) {
    console.error(`❌ ERROR: Invalid usecaseAccountId format: ${accountId}`);
    console.error('   AWS Account IDs must be 12 digits');
    process.exit(1);
  }
}

console.log('📋 Data Account Stack Configuration:');
console.log(`   Portal Account:    ${portalAccountId}`);
console.log(`   UseCase Accounts:  ${usecaseAccountIds.join(', ')}`);
console.log(`   External ID:       ${externalId.substring(0, 8)}...${externalId.substring(externalId.length - 4)} (masked)`);
console.log(`   Data Buckets:      ${dataBucketNames ? dataBucketNames.join(', ') : '(automatic - configured during UseCase onboarding)'}`);
console.log('');
console.log('⚠️  IMPORTANT: Save the external ID securely!');
console.log('   You will need to provide it when onboarding UseCases that use this Data Account.');
console.log('');

if (!dataBucketNames) {
  console.log('ℹ️  NOTE: Bucket policies will be automatically configured when you onboard');
  console.log('   a UseCase in the portal. No manual bucket configuration needed.');
  console.log('');
}

new DataAccountStack(app, 'DDAPortalDataAccountStack', {
  portalAccountId,
  usecaseAccountIds,
  externalId,
  dataBucketNames,
  description: 'IAM roles and bucket policies for DDA Portal and SageMaker to access Data Account resources',
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});

app.synth();
