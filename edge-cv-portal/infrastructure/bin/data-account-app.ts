#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { DataAccountStack } from '../lib/data-account-stack';

/**
 * CDK App for deploying the Data Account access roles.
 * 
 * This is a standalone app that should be deployed in Data Accounts
 * to grant access for:
 * 1. Portal Account - to manage S3 buckets and data
 * 2. UseCase Accounts - for SageMaker to read training data
 * 
 * Usage:
 * 
 * Single UseCase Account:
 *   cdk deploy -a "npx ts-node bin/data-account-app.ts" \
 *     -c portalAccountId=111111111111 \
 *     -c usecaseAccountIds=222222222222 \
 *     -c externalId=your-uuid-here
 * 
 * Multiple UseCase Accounts (comma-separated):
 *   cdk deploy -a "npx ts-node bin/data-account-app.ts" \
 *     -c portalAccountId=111111111111 \
 *     -c usecaseAccountIds=222222222222,333333333333,444444444444 \
 *     -c externalId=your-uuid-here
 */

const app = new cdk.App();

// Get configuration from context
const portalAccountId = app.node.tryGetContext('portalAccountId');
const usecaseAccountIdsStr = app.node.tryGetContext('usecaseAccountIds');
const externalId = app.node.tryGetContext('externalId');

// Validate required parameters
if (!portalAccountId) {
  console.error('❌ ERROR: portalAccountId is required');
  console.error('   Usage: cdk deploy -a "npx ts-node bin/data-account-app.ts" -c portalAccountId=111111111111 -c usecaseAccountIds=222222222222');
  process.exit(1);
}

if (!usecaseAccountIdsStr) {
  console.error('❌ ERROR: usecaseAccountIds is required');
  console.error('   Usage: cdk deploy -a "npx ts-node bin/data-account-app.ts" -c portalAccountId=111111111111 -c usecaseAccountIds=222222222222');
  console.error('   For multiple accounts: -c usecaseAccountIds=222222222222,333333333333');
  process.exit(1);
}

// Parse usecase account IDs (comma-separated)
const usecaseAccountIds = usecaseAccountIdsStr.split(',').map((id: string) => id.trim());

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
console.log(`   External ID:       ${externalId || '(none)'}`);
console.log('');

new DataAccountStack(app, 'DDAPortalDataAccountStack', {
  portalAccountId,
  usecaseAccountIds,
  externalId,
  description: 'IAM roles for DDA Portal and SageMaker to access Data Account resources',
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});

app.synth();
