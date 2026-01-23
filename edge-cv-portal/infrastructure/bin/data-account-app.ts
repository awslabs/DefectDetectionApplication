#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { DataAccountStack } from '../lib/data-account-stack';

/**
 * CDK App for deploying the Data Account access roles and bucket policies.
 * 
 * This is a standalone app that should be deployed in Data Accounts
 * to grant access for the Portal Account to manage S3 buckets and data.
 * 
 * UseCase Account access is configured automatically by the portal when
 * each UseCase is onboarded - no need to specify UseCase Account IDs here.
 * 
 * Usage:
 * 
 *   cdk deploy -a "npx ts-node bin/data-account-app.ts" \
 *     -c portalAccountId=111111111111 \
 *     -c externalId=your-uuid-here
 * 
 * Optional - specify bucket names to configure policies immediately:
 *   cdk deploy -a "npx ts-node bin/data-account-app.ts" \
 *     -c portalAccountId=111111111111 \
 *     -c externalId=your-uuid-here \
 *     -c dataBucketNames=dda-cookie-bucket,dda-training-data
 */

const app = new cdk.App();

// Get configuration from context
const portalAccountId = app.node.tryGetContext('portalAccountId');
const externalId = app.node.tryGetContext('externalId');
const dataBucketNamesStr = app.node.tryGetContext('dataBucketNames');

// Validate required parameters
if (!portalAccountId) {
  console.error('❌ ERROR: portalAccountId is required');
  console.error('   Usage: cdk deploy -a "npx ts-node bin/data-account-app.ts" -c portalAccountId=111111111111 -c externalId=your-uuid');
  process.exit(1);
}

if (!externalId) {
  console.error('❌ ERROR: externalId is required for production security');
  console.error('   Generate a UUID: uuidgen');
  console.error('   Usage: cdk deploy -a "npx ts-node bin/data-account-app.ts" -c portalAccountId=111111111111 -c externalId=your-uuid');
  console.error('');
  console.error('   ⚠️  IMPORTANT: Save this external ID securely!');
  console.error('   You will need to provide it when onboarding UseCases that use this Data Account.');
  process.exit(1);
}

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

console.log('📋 Data Account Stack Configuration:');
console.log(`   Portal Account:    ${portalAccountId}`);
console.log(`   External ID:       ${externalId.substring(0, 8)}...${externalId.substring(externalId.length - 4)} (masked)`);
console.log(`   Data Buckets:      ${dataBucketNames ? dataBucketNames.join(', ') : '(configured automatically during UseCase onboarding)'}`);
console.log('');
console.log('⚠️  IMPORTANT: Save the external ID securely!');
console.log('   You will need to provide it when onboarding UseCases that use this Data Account.');
console.log('');
console.log('ℹ️  NOTE: UseCase Account access and bucket policies will be automatically');
console.log('   configured by the portal when you onboard each UseCase.');
console.log('   No manual bucket configuration needed.');
console.log('');

new DataAccountStack(app, 'DDAPortalDataAccountStack', {
  portalAccountId,
  usecaseAccountIds: [], // Empty - portal configures access dynamically
  externalId,
  dataBucketNames,
  description: 'IAM roles and bucket policies for DDA Portal to access Data Account resources. UseCase access configured automatically.',
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});

app.synth();
