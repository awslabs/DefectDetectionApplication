#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { UseCaseAccountStack } from '../lib/usecase-account-stack';

/**
 * CDK App for deploying the UseCase Account access role.
 * 
 * This is a standalone app that should be deployed in UseCase Accounts
 * to grant the Defect Detection Application (DDA) access to resources.
 * 
 * Usage:
 * 
 * 1. Same-account deployment (Portal and UseCase in same account):
 *    cdk deploy -a "npx ts-node bin/usecase-account-app.ts"
 * 
 * 2. Cross-account deployment:
 *    cdk deploy -a "npx ts-node bin/usecase-account-app.ts" \
 *      -c portalAccountId=111111111111 \
 *      -c externalId=your-uuid-here
 * 
 * 3. Custom S3 bucket prefix:
 *    cdk deploy -a "npx ts-node bin/usecase-account-app.ts" \
 *      -c s3BucketPrefix=my-custom-prefix-*
 */

const app = new cdk.App();

// Get configuration from context
const portalAccountId = app.node.tryGetContext('portalAccountId');
const externalId = app.node.tryGetContext('externalId');
const s3BucketPrefix = app.node.tryGetContext('s3BucketPrefix');

// Validate cross-account configuration
if (portalAccountId && !externalId) {
  console.warn(
    '⚠️  WARNING: portalAccountId provided without externalId. ' +
      'For cross-account access, an externalId is strongly recommended for security.'
  );
}

new UseCaseAccountStack(app, 'DDAPortalUseCaseAccountStack', {
  portalAccountId,
  externalId,
  s3BucketPrefix,
  description: 'IAM role for DDA Portal to access UseCase Account resources (v2 - with device management)',
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});

app.synth();
