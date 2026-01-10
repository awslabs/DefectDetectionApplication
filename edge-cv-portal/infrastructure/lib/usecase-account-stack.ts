import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface UseCaseAccountStackProps extends cdk.StackProps {
  /**
   * AWS Account ID of the Portal Account that will assume this role.
   * If not provided, allows same-account access.
   */
  portalAccountId?: string;

  /**
   * External ID for additional security when assuming the role.
   * Required for cross-account access.
   */
  externalId?: string;

  /**
   * S3 bucket prefix pattern for data access.
   * Default: '*-dda-*'
   */
  s3BucketPrefix?: string;
}

/**
 * Stack to deploy in UseCase Accounts to grant Defect Detection Application (DDA) access.
 * 
 * This stack creates an IAM role with all necessary permissions for:
 * - SageMaker training and compilation
 * - S3 data access
 * - Greengrass component management
 * - IoT device management
 * - CloudWatch logs access
 * 
 * Deploy this stack in each UseCase Account that needs to be managed by the DDA Portal.
 */
export class UseCaseAccountStack extends cdk.Stack {
  public readonly role: iam.Role;
  public readonly roleArn: string;
  public readonly groundTruthRole: iam.Role;

  constructor(scope: Construct, id: string, props: UseCaseAccountStackProps) {
    super(scope, id, props);

    const portalAccountId = props.portalAccountId || cdk.Stack.of(this).account;
    const externalId = props.externalId;
    const s3BucketPrefix = props.s3BucketPrefix || '*-dda-*';

    // Create SageMaker Execution Role
    // This role is used by SageMaker for training, compilation, and Ground Truth labeling jobs
    this.groundTruthRole = new iam.Role(this, 'DDASageMakerExecutionRole', {
      roleName: 'DDASageMakerExecutionRole',
      description: 'Execution role for SageMaker training, compilation, and labeling jobs',
      assumedBy: new iam.ServicePrincipal('sagemaker.amazonaws.com'),
    });

    // Ground Truth S3 access - allow all buckets for flexibility
    this.groundTruthRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetObject',
          's3:PutObject',
          's3:DeleteObject',
          's3:ListBucket',
          's3:GetBucketLocation',
          's3:GetBucketCors',
          's3:PutBucketCors',
        ],
        resources: [
          'arn:aws:s3:::*',
          'arn:aws:s3:::*/*',
        ],
      })
    );

    // Ground Truth CloudWatch Logs
    this.groundTruthRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'cloudwatch:PutMetricData',
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents',
          'logs:DescribeLogStreams',
        ],
        resources: ['*'],
      })
    );

    // SageMaker permissions for training, compilation, and labeling
    this.groundTruthRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          // Training job permissions
          'sagemaker:CreateTrainingJob',
          'sagemaker:DescribeTrainingJob',
          'sagemaker:StopTrainingJob',
          'sagemaker:ListTrainingJobs',
          // Compilation job permissions
          'sagemaker:CreateCompilationJob',
          'sagemaker:DescribeCompilationJob',
          'sagemaker:StopCompilationJob',
          'sagemaker:ListCompilationJobs',
          // Labeling job permissions
          'sagemaker:DescribeLabelingJob',
          'sagemaker:ListLabelingJobs',
          // Model permissions
          'sagemaker:CreateModel',
          'sagemaker:DescribeModel',
          'sagemaker:DeleteModel',
          'sagemaker:ListModels',
        ],
        resources: ['*'],
      })
    );

    // Ground Truth ECR access for custom containers
    this.groundTruthRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'ecr:GetAuthorizationToken',
          'ecr:BatchCheckLayerAvailability',
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchGetImage',
        ],
        resources: ['*'],
      })
    );

    // Create the cross-account (or same-account) role
    this.role = new iam.Role(this, 'DDAPortalAccessRole', {
      roleName: 'DDAPortalAccessRole',
      description: 'Role for Defect Detection Application (DDA) to access UseCase Account resources',
      assumedBy: new iam.AccountPrincipal(portalAccountId),
      externalIds: externalId ? [externalId] : undefined,
      maxSessionDuration: cdk.Duration.hours(12),
    });

    // SageMaker Training permissions
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SageMakerTraining',
        effect: iam.Effect.ALLOW,
        actions: [
          'sagemaker:CreateTrainingJob',
          'sagemaker:DescribeTrainingJob',
          'sagemaker:StopTrainingJob',
          'sagemaker:ListTrainingJobs',
          'sagemaker:AddTags',
        ],
        resources: [`arn:aws:sagemaker:*:${this.account}:training-job/*`],
      })
    );

    // SageMaker Compilation permissions
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SageMakerCompilation',
        effect: iam.Effect.ALLOW,
        actions: [
          'sagemaker:CreateCompilationJob',
          'sagemaker:DescribeCompilationJob',
          'sagemaker:ListCompilationJobs',
          'sagemaker:StopCompilationJob',
        ],
        resources: [`arn:aws:sagemaker:*:${this.account}:compilation-job/*`],
      })
    );

    // SageMaker Algorithm access (AWS Marketplace)
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SageMakerAlgorithm',
        effect: iam.Effect.ALLOW,
        actions: ['sagemaker:DescribeAlgorithm'],
        resources: [
          'arn:aws:sagemaker:*:865070037744:algorithm/computer-vision-defect-detection',
        ],
      })
    );

    // Ground Truth Labeling permissions
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'GroundTruthLabelingV2',
        effect: iam.Effect.ALLOW,
        actions: [
          'sagemaker:AddTags',
          'sagemaker:CreateLabelingJob',
          'sagemaker:DescribeLabelingJob',
          'sagemaker:ListLabelingJobs',
          'sagemaker:StopLabelingJob',
          'sagemaker:ListWorkteams',
        ],
        resources: [`arn:aws:sagemaker:*:${this.account}:labeling-job/*`],
      })
    );

    // Ground Truth Workteam permissions (ListWorkteams requires wildcard resource)
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'GroundTruthWorkteams',
        effect: iam.Effect.ALLOW,
        actions: ['sagemaker:ListWorkteams'],
        resources: ['*'],
      })
    );

    // S3 Data Access - Tag-based access for flexibility
    // Buckets must be tagged with 'dda-portal:managed' = 'true'
    // Resource Groups Tagging API - used to find buckets and Greengrass components with dda-portal:managed tag
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ResourceTaggingAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'tag:GetResources',
          'tag:GetTagKeys',
          'tag:GetTagValues',
          'resourcegroupstaggingapi:GetResources',
          'resourcegroupstaggingapi:GetTagKeys',
          'resourcegroupstaggingapi:GetTagValues',
        ],
        resources: ['*'],
      })
    );

    // Bucket-level operations
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'S3BucketAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          's3:ListBucket',
          's3:GetBucketLocation',
          's3:GetBucketVersioning',
          's3:GetBucketTagging',
          's3:CreateBucket',
          's3:PutBucketVersioning',
          's3:PutBucketEncryption',
          's3:PutBucketTagging',
        ],
        resources: ['arn:aws:s3:::*'],
      })
    );

    // Object-level operations
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'S3ObjectAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetObject',
          's3:PutObject',
          's3:DeleteObject',
        ],
        resources: ['arn:aws:s3:::*/*'],
      })
    );

    // SageMaker-managed buckets (always allowed)
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'S3SageMakerAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetObject',
          's3:PutObject',
          's3:DeleteObject',
          's3:ListBucket',
          's3:GetBucketLocation',
        ],
        resources: [
          'arn:aws:s3:::sagemaker-*',
          'arn:aws:s3:::sagemaker-*/*',
        ],
      })
    );

    // CloudWatch Logs
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchLogs',
        effect: iam.Effect.ALLOW,
        actions: [
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents',
          'logs:GetLogEvents',
          'logs:DescribeLogStreams',
          'logs:FilterLogEvents',
        ],
        resources: [
          `arn:aws:logs:*:${this.account}:log-group:/aws/sagemaker/*`,
          `arn:aws:logs:*:${this.account}:log-group:/aws/lambda/*`,
        ],
      })
    );

    // Greengrass Components
    // Allows creating model components and shared components (dda-LocalServer)
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'GreengrassComponents',
        effect: iam.Effect.ALLOW,
        actions: [
          'greengrass:CreateComponentVersion',
          'greengrass:DescribeComponent',
          'greengrass:DeleteComponent',
          'greengrass:ListComponents',
          'greengrass:ListComponentVersions',
          'greengrass:GetComponent',
          'greengrass:GetComponentVersionArtifact',
          'greengrass:TagResource',
          'greengrass:UntagResource',
          'greengrass:ListTagsForResource',
        ],
        resources: [`arn:aws:greengrass:*:${this.account}:components:*`],
      })
    );

    // Greengrass Deployments
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'GreengrassDeployments',
        effect: iam.Effect.ALLOW,
        actions: [
          'greengrass:CreateDeployment',
          'greengrass:GetDeployment',
          'greengrass:ListDeployments',
          'greengrass:CancelDeployment',
        ],
        resources: [`arn:aws:greengrass:*:${this.account}:deployments:*`],
      })
    );

    // Greengrass Core Devices
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'GreengrassCoreDevices',
        effect: iam.Effect.ALLOW,
        actions: [
          'greengrass:ListCoreDevices',
          'greengrass:GetCoreDevice',
          'greengrass:ListInstalledComponents',
          'greengrass:ListEffectiveDeployments',
          'greengrass:ListTagsForResource',
        ],
        resources: ['*'],
      })
    );

    // IoT Things
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'IoTThings',
        effect: iam.Effect.ALLOW,
        actions: [
          'iot:DescribeThing',
          'iot:ListThings',
          'iot:UpdateThing',
          'iot:ListThingGroupsForThing',
          'iot:ListTagsForResource',
        ],
        resources: [`arn:aws:iot:*:${this.account}:thing/*`],
      })
    );

    // IoT Shadows
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'IoTShadows',
        effect: iam.Effect.ALLOW,
        actions: [
          'iot:GetThingShadow',
          'iot:UpdateThingShadow',
          'iot:DeleteThingShadow',
        ],
        resources: [`arn:aws:iot:*:${this.account}:thing/*`],
      })
    );

    // IoT Jobs
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'IoTJobs',
        effect: iam.Effect.ALLOW,
        actions: ['iot:CreateJob', 'iot:DescribeJob', 'iot:CancelJob', 'iot:ListJobs'],
        resources: [`arn:aws:iot:*:${this.account}:job/*`],
      })
    );

    // IAM PassRole for SageMaker and Greengrass
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'IAMPassRole',
        effect: iam.Effect.ALLOW,
        actions: ['iam:PassRole'],
        resources: [
          `arn:aws:iam::${this.account}:role/*SageMaker*`,
          `arn:aws:iam::${this.account}:role/*Greengrass*`,
          `arn:aws:iam::${this.account}:role/*GroundTruth*`,
          `arn:aws:iam::${this.account}:role/DDASageMakerExecutionRole`,
        ],
        conditions: {
          StringEquals: {
            'iam:PassedToService': ['sagemaker.amazonaws.com', 'greengrass.amazonaws.com'],
          },
        },
      })
    );

    // ECR Access for container images
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ECRAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'ecr:GetAuthorizationToken',
          'ecr:BatchCheckLayerAvailability',
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchGetImage',
        ],
        resources: ['*'],
      })
    );

    this.roleArn = this.role.roleArn;

    // Outputs
    new cdk.CfnOutput(this, 'RoleArn', {
      value: this.role.roleArn,
      description: 'ARN of the DDA Portal access role',
      exportName: 'DDAPortalAccessRoleArn',
    });

    new cdk.CfnOutput(this, 'RoleName', {
      value: this.role.roleName,
      description: 'Name of the DDA Portal access role',
    });

    if (externalId) {
      new cdk.CfnOutput(this, 'ExternalId', {
        value: externalId,
        description: 'External ID for assuming the role',
      });
    }

    new cdk.CfnOutput(this, 'PortalAccountId', {
      value: portalAccountId,
      description: 'Portal Account ID that can assume this role',
    });

    new cdk.CfnOutput(this, 'SageMakerExecutionRoleArn', {
      value: this.groundTruthRole.roleArn,
      description: 'ARN of the SageMaker execution role',
      exportName: 'DDASageMakerExecutionRoleArn',
    });
  }
}
