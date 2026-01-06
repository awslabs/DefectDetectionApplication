import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface DataAccountStackProps extends cdk.StackProps {
  /**
   * AWS Account ID of the Portal Account that will assume this role.
   */
  portalAccountId: string;

  /**
   * List of UseCase Account IDs that need to read data from this account.
   * SageMaker training jobs in these accounts will access S3 buckets here.
   */
  usecaseAccountIds: string[];

  /**
   * External ID for additional security when assuming the role.
   */
  externalId?: string;
}

/**
 * Stack to deploy in Data Accounts to grant access for:
 * 1. Portal Account - to manage S3 buckets and data
 * 2. UseCase Accounts - for SageMaker to read training data
 * 
 * This supports the architecture where training data is stored in a separate
 * Data Account that can be shared by multiple UseCase Accounts.
 * 
 * Deploy this stack in each Data Account.
 */
export class DataAccountStack extends cdk.Stack {
  public readonly portalAccessRole: iam.Role;
  public readonly sagemakerAccessRole: iam.Role;

  constructor(scope: Construct, id: string, props: DataAccountStackProps) {
    super(scope, id, props);

    const { portalAccountId, usecaseAccountIds, externalId } = props;

    // ===========================================
    // Role 1: Portal Access Role
    // ===========================================
    // Allows the Portal to manage S3 buckets and data in this account
    this.portalAccessRole = new iam.Role(this, 'DDAPortalDataAccessRole', {
      roleName: 'DDAPortalDataAccessRole',
      description: 'Role for DDA Portal to manage S3 data in this Data Account',
      assumedBy: new iam.AccountPrincipal(portalAccountId),
      externalIds: externalId ? [externalId] : undefined,
      maxSessionDuration: cdk.Duration.hours(12),
    });

    // S3 bucket management
    this.portalAccessRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'S3BucketManagement',
        effect: iam.Effect.ALLOW,
        actions: [
          's3:ListBucket',
          's3:GetBucketLocation',
          's3:GetBucketVersioning',
          's3:GetBucketTagging',
          's3:PutBucketTagging',
          's3:GetBucketCors',
          's3:PutBucketCors',
          's3:ListAllMyBuckets',
        ],
        resources: ['arn:aws:s3:::*'],
      })
    );

    // S3 object operations
    this.portalAccessRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'S3ObjectAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetObject',
          's3:PutObject',
          's3:DeleteObject',
          's3:GetObjectTagging',
          's3:PutObjectTagging',
        ],
        resources: ['arn:aws:s3:::*/*'],
      })
    );

    // Resource tagging API for discovering tagged buckets
    this.portalAccessRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ResourceTaggingAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'tag:GetResources',
          'tag:GetTagKeys',
          'tag:GetTagValues',
          'resourcegroupstaggingapi:GetResources',
        ],
        resources: ['*'],
      })
    );

    // ===========================================
    // Role 2: SageMaker Cross-Account Access Role
    // ===========================================
    // Allows SageMaker in UseCase Accounts to read training data
    
    // Use account-level principals - any role from these accounts can assume
    // The accounts themselves are trusted, not specific roles
    const allAccountIds = [...usecaseAccountIds, portalAccountId];

    this.sagemakerAccessRole = new iam.Role(this, 'DDASageMakerDataAccessRole', {
      roleName: 'DDASageMakerDataAccessRole',
      description: 'Role for SageMaker in UseCase Accounts to read training data',
      assumedBy: new iam.CompositePrincipal(
        ...allAccountIds.map(accountId => new iam.AccountPrincipal(accountId))
      ),
      maxSessionDuration: cdk.Duration.hours(12),
    });

    // Read-only S3 access for training data
    this.sagemakerAccessRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'S3ReadAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetObject',
          's3:GetObjectVersion',
          's3:ListBucket',
          's3:GetBucketLocation',
        ],
        resources: [
          'arn:aws:s3:::*',
          'arn:aws:s3:::*/*',
        ],
      })
    );

    // Write access for SageMaker outputs (model artifacts, checkpoints)
    this.sagemakerAccessRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'S3WriteAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          's3:PutObject',
          's3:DeleteObject',
        ],
        resources: [
          'arn:aws:s3:::*/sagemaker/*',
          'arn:aws:s3:::*/output/*',
          'arn:aws:s3:::*/models/*',
        ],
      })
    );

    // ===========================================
    // Outputs
    // ===========================================
    new cdk.CfnOutput(this, 'PortalAccessRoleArn', {
      value: this.portalAccessRole.roleArn,
      description: 'ARN of the Portal access role for data management',
      exportName: 'DDAPortalDataAccessRoleArn',
    });

    new cdk.CfnOutput(this, 'SageMakerAccessRoleArn', {
      value: this.sagemakerAccessRole.roleArn,
      description: 'ARN of the SageMaker access role for training data',
      exportName: 'DDASageMakerDataAccessRoleArn',
    });

    new cdk.CfnOutput(this, 'DataAccountId', {
      value: this.account,
      description: 'This Data Account ID',
    });

    new cdk.CfnOutput(this, 'PortalAccountId', {
      value: portalAccountId,
      description: 'Portal Account ID that can assume the portal role',
    });

    new cdk.CfnOutput(this, 'UseCaseAccountIds', {
      value: usecaseAccountIds.join(','),
      description: 'UseCase Account IDs that can assume the SageMaker role',
    });

    if (externalId) {
      new cdk.CfnOutput(this, 'ExternalId', {
        value: externalId,
        description: 'External ID for assuming the portal role',
      });
    }
  }
}
