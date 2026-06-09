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
 * 3. With additional bucket access (if bucket name doesn't match dda-* pattern):
 *    cdk deploy -a "npx ts-node bin/usecase-account-app.ts" \
 *      -c portalAccountId=111111111111 \
 *      -c externalId=your-uuid-here \
 *      -c modelArtifactsBucket=my-custom-bucket
 * 
 * The Greengrass device policy automatically grants access to:
 * - Portal Account's component bucket (dda-component-{region}-{portalAccountId})
 * - All buckets matching dda-* and *-dda-* patterns (for model artifacts)
 * - Plus any specific bucket provided via modelArtifactsBucket
 */

const app = new cdk.App();

// Get configuration from context
const portalAccountId = app.node.tryGetContext('portalAccountId');
const externalId = app.node.tryGetContext('externalId');
const modelArtifactsBucket = app.node.tryGetContext('modelArtifactsBucket');

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
  modelArtifactsBucket,
  description: 'IAM role for DDA Portal to access UseCase Account resources (v2 - with device management)',
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});

app.synth();
