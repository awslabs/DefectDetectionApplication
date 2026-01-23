import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

/**
 * Stack version - increment when making changes
 * Follow semver: MAJOR.MINOR.PATCH
 * - MAJOR: Breaking changes (role renames, permission removals)
 * - MINOR: New features (new permissions, new resources)
 * - PATCH: Bug fixes
 */
const STACK_VERSION = '1.3.0';

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
   * Enable cross-account EventBridge forwarding for SageMaker events.
   * When enabled, SageMaker training/compilation job state changes are forwarded
   * to the Portal Account's default event bus.
   * Default: true
   */
  enableEventBridgeForwarding?: boolean;

  /**
   * Name of the GDK component bucket in the Portal Account.
   * This bucket stores shared Greengrass component artifacts.
   * Format: dda-component-{region}-{portalAccountId}
   */
  componentBucketName?: string;

  /**
   * S3 bucket name for model artifacts in this UseCase Account.
   * This is the bucket where trained models and Greengrass component packages are stored.
   * Greengrass devices need access to download model components from this bucket.
   * The policy ALWAYS includes dda-* and *-dda-* patterns, plus this specific bucket if provided.
   */
  modelArtifactsBucket?: string;

  /**
   * AWS Account ID of the Data Account (if using separate data storage).
   * When provided, the SageMaker execution role will be granted permissions
   * to access S3 buckets in the Data Account for cross-account labeling and training.
   */
  dataAccountId?: string;

  /**
   * List of S3 bucket names in the Data Account that SageMaker should access.
   * Required when dataAccountId is provided.
   * Example: ['dda-cookie-bucket', 'dda-training-data']
   */
  dataAccountBuckets?: string[];
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
  public readonly greengrassDevicePolicy: iam.ManagedPolicy;
  public readonly inferenceResultsBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: UseCaseAccountStackProps) {
    super(scope, id, props);

    const portalAccountId = props.portalAccountId || cdk.Stack.of(this).account;
    const externalId = props.externalId;
    const region = cdk.Stack.of(this).region;
    const dataAccountId = props.dataAccountId;
    const dataAccountBuckets = props.dataAccountBuckets || [];
    
    // Component bucket name - defaults to Portal Account's GDK bucket
    const componentBucketName = props.componentBucketName || `dda-component-${region}-${portalAccountId}`;
    
    // Model artifacts bucket - optional specific bucket
    const modelArtifactsBucket = props.modelArtifactsBucket;

    // Build S3 resources list for model artifacts access
    // Always include dda-* and *-dda-* patterns, plus specific bucket if provided
    const modelArtifactResources = [
      'arn:aws:s3:::dda-*',
      'arn:aws:s3:::dda-*/*',
      'arn:aws:s3:::*-dda-*',
      'arn:aws:s3:::*-dda-*/*',
    ];
    
    // Add specific bucket if provided (in case it doesn't match the patterns)
    if (modelArtifactsBucket) {
      modelArtifactResources.push(`arn:aws:s3:::${modelArtifactsBucket}`);
      modelArtifactResources.push(`arn:aws:s3:::${modelArtifactsBucket}/*`);
    }

    // Create a managed policy for Greengrass devices to access component artifacts
    // This policy should be attached to GreengrassV2TokenExchangeRole during device provisioning
    // Uses pattern-based access to cover all DDA buckets without needing to specify each one
    this.greengrassDevicePolicy = new iam.ManagedPolicy(this, 'DDAGreengrassDevicePolicy', {
      managedPolicyName: 'DDAPortalComponentAccessPolicy',
      description: 'Allows Greengrass devices to download DDA component artifacts from Portal and UseCase S3 buckets',
      statements: [
        // Access to Portal Account's component bucket (for DDA LocalServer)
        new iam.PolicyStatement({
          sid: 'AllowPortalComponentBucketAccess',
          effect: iam.Effect.ALLOW,
          actions: ['s3:GetObject', 's3:GetBucketLocation'],
          resources: [
            `arn:aws:s3:::${componentBucketName}`,
            `arn:aws:s3:::${componentBucketName}/*`,
          ],
        }),
        // Access to DDA buckets (model artifacts, assets, etc.)
        // Covers: dda-assset-bucket, dda-data-bucket, my-dda-bucket, etc.
        // Plus any specific bucket provided via modelArtifactsBucket prop
        new iam.PolicyStatement({
          sid: 'AllowDDABucketPatternAccess',
          effect: iam.Effect.ALLOW,
          actions: ['s3:GetObject', 's3:GetBucketLocation', 's3:HeadObject'],
          resources: modelArtifactResources,
        }),
      ],
    });

    // Create S3 bucket for inference results
    // This bucket stores images and metadata uploaded from edge devices
    this.inferenceResultsBucket = new s3.Bucket(this, 'InferenceResultsBucket', {
      bucketName: `dda-inference-results-${this.account}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: false,
      lifecycleRules: [
        {
          id: 'DeleteOldInferenceResults',
          enabled: true,
          expiration: cdk.Duration.days(90), // Keep inference results for 90 days
        },
        {
          id: 'TransitionToIA',
          enabled: true,
          transitions: [
            {
              storageClass: s3.StorageClass.INFREQUENT_ACCESS,
              transitionAfter: cdk.Duration.days(30), // Move to IA after 30 days
            },
          ],
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN, // Don't delete bucket on stack deletion
    });

    // Add inference results upload permissions to Greengrass device policy
    this.greengrassDevicePolicy.addStatements(
      new iam.PolicyStatement({
        sid: 'AllowInferenceResultsUpload',
        effect: iam.Effect.ALLOW,
        actions: [
          's3:PutObject',
          's3:PutObjectTagging',
          's3:GetObject',
          's3:GetBucketLocation',
        ],
        resources: [
          this.inferenceResultsBucket.bucketArn,
          `${this.inferenceResultsBucket.bucketArn}/*`,
        ],
      })
    );

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

    // Cross-Account S3 Access for Data Account
    // This enables Ground Truth labeling jobs and SageMaker training to read data
    // from a separate Data Account's S3 buckets
    if (dataAccountId && dataAccountBuckets.length > 0) {
      // Build resource ARNs for Data Account buckets
      const dataAccountBucketArns: string[] = [];
      const dataAccountObjectArns: string[] = [];
      
      for (const bucketName of dataAccountBuckets) {
        dataAccountBucketArns.push(`arn:aws:s3:::${bucketName}`);
        dataAccountObjectArns.push(`arn:aws:s3:::${bucketName}/*`);
      }

      // Read access to Data Account buckets for training data and images
      this.groundTruthRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'CrossAccountDataBucketRead',
          effect: iam.Effect.ALLOW,
          actions: [
            's3:GetObject',
            's3:GetObjectVersion',
            's3:GetObjectTagging',
            's3:ListBucket',
            's3:GetBucketLocation',
          ],
          resources: [...dataAccountBucketArns, ...dataAccountObjectArns],
        })
      );

      // Write access to Data Account buckets for labeling output
      // Ground Truth needs to write annotation results back to S3
      this.groundTruthRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'CrossAccountDataBucketWrite',
          effect: iam.Effect.ALLOW,
          actions: [
            's3:PutObject',
            's3:PutObjectTagging',
          ],
          resources: dataAccountObjectArns,
        })
      );

      new cdk.CfnOutput(this, 'DataAccountId', {
        value: dataAccountId,
        description: 'Data Account ID for cross-account S3 access',
      });

      new cdk.CfnOutput(this, 'DataAccountBuckets', {
        value: dataAccountBuckets.join(','),
        description: 'Data Account S3 buckets accessible by SageMaker',
      });
    }

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

    // CloudWatch Logs for Greengrass components
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchLogsGreengrass',
        effect: iam.Effect.ALLOW,
        actions: [
          'logs:DescribeLogGroups',
          'logs:DescribeLogStreams',
          'logs:GetLogEvents',
          'logs:FilterLogEvents',
        ],
        resources: ['*'], // DescribeLogGroups requires wildcard resource
      })
    );

    // Inference Results S3 Access
    // Allow portal to read inference results uploaded by devices
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InferenceResultsAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetObject',
          's3:ListBucket',
          's3:GetBucketLocation',
        ],
        resources: [
          `arn:aws:s3:::dda-inference-results-${this.account}`,
          `arn:aws:s3:::dda-inference-results-${this.account}/*`,
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

    // Greengrass Deployments - use wildcard to cover all deployment ARN patterns
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'GreengrassDeploymentsV4',
        effect: iam.Effect.ALLOW,
        actions: [
          'greengrass:CreateDeployment',
          'greengrass:GetDeployment',
          'greengrass:ListDeployments',
          'greengrass:CancelDeployment',
          'greengrass:TagResource',
          'greengrass:UntagResource',
        ],
        resources: ['*'],
        conditions: {
          StringEquals: {
            'aws:ResourceAccount': this.account,
          },
        },
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

    // Cross-Account EventBridge Forwarding
    // Forward SageMaker events to Portal Account for real-time status updates
    const enableEventBridgeForwarding = props.enableEventBridgeForwarding !== false;
    
    if (enableEventBridgeForwarding && portalAccountId !== cdk.Stack.of(this).account) {
      // Create IAM role for EventBridge to send events cross-account
      const eventBridgeRole = new iam.Role(this, 'EventBridgeCrossAccountRole', {
        roleName: 'DDAEventBridgeCrossAccountRole',
        description: 'Role for EventBridge to forward SageMaker events to Portal Account',
        assumedBy: new iam.ServicePrincipal('events.amazonaws.com'),
      });

      // Allow putting events to Portal Account's default event bus
      eventBridgeRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['events:PutEvents'],
          resources: [`arn:aws:events:*:${portalAccountId}:event-bus/default`],
        })
      );

      // Target: Portal Account's default event bus
      const portalEventBus = events.EventBus.fromEventBusArn(
        this,
        'PortalEventBus',
        `arn:aws:events:${cdk.Stack.of(this).region}:${portalAccountId}:event-bus/default`
      );

      // Rule 1: Forward SageMaker Training Job State Changes
      const trainingStateChangeRule = new events.Rule(this, 'ForwardTrainingStateChange', {
        ruleName: 'dda-forward-training-state-change',
        description: 'Forward SageMaker training job state changes to Portal Account',
        eventPattern: {
          source: ['aws.sagemaker'],
          detailType: ['SageMaker Training Job State Change'],
          detail: {
            TrainingJobStatus: ['Completed', 'Failed', 'Stopped'],
          },
        },
      });

      trainingStateChangeRule.addTarget(
        new targets.EventBus(portalEventBus, {
          role: eventBridgeRole,
        })
      );

      // Rule 2: Forward SageMaker Compilation Job State Changes
      const compilationStateChangeRule = new events.Rule(this, 'ForwardCompilationStateChange', {
        ruleName: 'dda-forward-compilation-state-change',
        description: 'Forward SageMaker compilation job state changes to Portal Account',
        eventPattern: {
          source: ['aws.sagemaker'],
          detailType: ['SageMaker Compilation Job State Change'],
          detail: {
            CompilationJobStatus: ['COMPLETED', 'FAILED', 'STOPPED'],
          },
        },
      });

      compilationStateChangeRule.addTarget(
        new targets.EventBus(portalEventBus, {
          role: eventBridgeRole,
        })
      );

      new cdk.CfnOutput(this, 'EventBridgeForwardingEnabled', {
        value: 'true',
        description: 'Cross-account EventBridge forwarding is enabled',
      });
    }

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

    new cdk.CfnOutput(this, 'GreengrassDevicePolicyArn', {
      value: this.greengrassDevicePolicy.managedPolicyArn,
      description: 'ARN of the policy to attach to GreengrassV2TokenExchangeRole during device provisioning',
      exportName: 'DDAGreengrassDevicePolicyArn',
    });

    new cdk.CfnOutput(this, 'ComponentBucketName', {
      value: componentBucketName,
      description: 'Portal Account S3 bucket containing shared component artifacts',
    });

    new cdk.CfnOutput(this, 'ModelArtifactsBucketAccess', {
      value: modelArtifactsBucket 
        ? `${modelArtifactsBucket} + dda-* and *-dda-* patterns`
        : 'dda-* and *-dda-* patterns',
      description: 'S3 buckets Greengrass devices can access for model artifacts',
    });

    new cdk.CfnOutput(this, 'InferenceResultsBucketName', {
      value: this.inferenceResultsBucket.bucketName,
      description: 'S3 bucket for storing inference results from edge devices',
      exportName: 'DDAInferenceResultsBucketName',
    });

    new cdk.CfnOutput(this, 'InferenceResultsBucketArn', {
      value: this.inferenceResultsBucket.bucketArn,
      description: 'ARN of the inference results S3 bucket',
      exportName: 'DDAInferenceResultsBucketArn',
    });

    // Stack version for upgrade tracking
    new cdk.CfnOutput(this, 'StackVersion', {
      value: STACK_VERSION,
      description: 'Version of the UseCase Account stack',
      exportName: 'DDAUseCaseStackVersion',
    });
  }
}
