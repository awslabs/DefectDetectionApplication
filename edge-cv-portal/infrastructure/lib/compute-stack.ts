import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import * as path from 'path';
import { ApiGatewayStack } from './api-gateway-stack';

export interface ComputeStackProps extends cdk.StackProps {
  userPool: cognito.UserPool;
  useCasesTable: dynamodb.Table;
  userRolesTable: dynamodb.Table;
  devicesTable: dynamodb.Table;
  auditLogTable: dynamodb.Table;
  trainingJobsTable: dynamodb.Table;
  labelingJobsTable: dynamodb.Table;
  preLabeledDatasetsTable: dynamodb.Table;
  modelsTable: dynamodb.Table;
  deploymentsTable: dynamodb.Table;
  settingsTable: dynamodb.Table;
  componentsTable: dynamodb.Table;
  sharedComponentsTable: dynamodb.Table;
  dataAccountsTable: dynamodb.Table;
  portalArtifactsBucket: s3.Bucket;
  /**
   * CloudFront domain for the portal frontend.
   * Used to configure CORS on Data Account buckets during UseCase onboarding.
   */
  cloudFrontDomain?: string;
}

export class ComputeStack extends cdk.Stack {
  public readonly api: apigateway.RestApi;
  public readonly apiUrl: string;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    // Lambda Layer for shared utilities
    const sharedLayer = new lambda.LayerVersion(this, 'SharedLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/shared')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'Shared utilities for Edge CV Portal Lambda functions - v2024-12-12-fixed-syntax',
    });

    // Lambda Layer for JWT dependencies
    const jwtLayer = new lambda.LayerVersion(this, 'JwtLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/jwt')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'JWT validation dependencies (PyJWT, cryptography)',
    });

    // Base IAM Role for Lambda functions
    const createLambdaRole = (name: string) => {
      const role = new iam.Role(this, `${name}Role`, {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        ],
      });

      // Grant DynamoDB permissions
      props.useCasesTable.grantReadWriteData(role);
      props.userRolesTable.grantReadWriteData(role);
      props.devicesTable.grantReadWriteData(role);
      props.auditLogTable.grantWriteData(role);
      props.trainingJobsTable.grantReadWriteData(role);
      props.labelingJobsTable.grantReadWriteData(role);
      props.preLabeledDatasetsTable.grantReadWriteData(role);
      props.modelsTable.grantReadWriteData(role);
      props.deploymentsTable.grantReadWriteData(role);
      props.settingsTable.grantReadWriteData(role);
      props.componentsTable.grantReadWriteData(role);
      props.sharedComponentsTable.grantReadWriteData(role);
      props.dataAccountsTable.grantReadWriteData(role);

      // Grant S3 permissions for portal artifacts bucket
      props.portalArtifactsBucket.grantReadWrite(role);

      // Grant SageMaker, Greengrass, CloudWatch Logs, STS, and API Gateway permissions
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'sagemaker:CreateTrainingJob',
          'sagemaker:DescribeTrainingJob',
          'sagemaker:ListTrainingJobs',
          'sagemaker:CreateCompilationJob',
          'sagemaker:DescribeCompilationJob',
          'sagemaker:ListCompilationJobs',
          'sagemaker:CreateLabelingJob',
          'sagemaker:DescribeLabelingJob',
          'sagemaker:ListLabelingJobs',
          'sagemaker:StopLabelingJob',
          'sagemaker:ListWorkteams',
          'sagemaker:DescribeWorkteam',
          'sagemaker:AddTags',
          'greengrass:CreateComponentVersion',
          'greengrass:DescribeComponent',
          'greengrass:GetComponent',
          'greengrass:ListComponents',
          'greengrass:ListComponentVersions',
          'greengrass:ListCoreDevices',
          'greengrass:GetCoreDevice',
          'greengrass:ListInstalledComponents',
          'greengrass:ListEffectiveDeployments',
          'greengrass:ListTagsForResource',
          'greengrass:TagResource',
          'greengrass:ListDeployments',
          'greengrass:GetDeployment',
          'greengrass:CreateDeployment',
          'greengrass:CancelDeployment',
          'iot:DescribeThing',
          'iot:DescribeEndpoint',
          'iot:DescribeThingGroup',
          'iot:GetThingType',
          'iot:ListThings',
          'iot:ListThingsInThingGroup',
          'iot:ListThingGroups',
          'iot:CreateThingGroup',
          'iot:AddThingToThingGroup',
          'iot:RemoveThingFromThingGroup',
          'iot:CreateJob',
          'iot:DescribeJob',
          'iot:UpdateJob',
          'iot:GetJobDocument',
          'iot:ListJobs',
          'iot:CancelJob',
          'iot:GetThingShadow',
          'iot:UpdateThingShadow',
          'iot:DeleteThingShadow',
          'logs:GetLogEvents',
          'logs:DescribeLogStreams',
          'logs:DescribeLogGroups',
          'logs:FilterLogEvents',
          'sts:AssumeRole',
          'execute-api:Invoke',
          's3:GetBucketCors',
          's3:PutBucketCors',
        ],
        resources: ['*'],
      }));

      // Grant IAM PassRole permission for SageMaker execution role
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['iam:PassRole'],
        resources: ['*'],
        conditions: {
          StringEquals: {
            'iam:PassedToService': 'sagemaker.amazonaws.com',
          },
        },
      }));

      // Grant Cognito permissions for auth functions
      if (name === 'Auth') {
        role.addToPolicy(new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'cognito-idp:InitiateAuth',
            'cognito-idp:RespondToAuthChallenge',
            'cognito-idp:GetUser',
            'cognito-idp:AdminGetUser',
          ],
          resources: [props.userPool.userPoolArn],
        }));
      }

      // Grant S3 permissions for model artifact processing
      // Note: Cross-account S3 access is handled via assumed role
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetObject',
          's3:PutObject',
          's3:ListBucket',
          's3:ListAllMyBuckets',
          's3:CreateBucket',
          's3:GetBucketLocation',
          's3:GetBucketTagging',
        ],
        resources: ['*'], // Restricted by assumed role in UseCase Account
      }));

      // Grant Resource Groups Tagging API permissions for finding tagged buckets
      role.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'tag:GetResources',
        ],
        resources: ['*'],
      }));

      return role;
    };

    // Environment variables for Lambda functions
    const lambdaEnvironment = {
      USECASES_TABLE: props.useCasesTable.tableName,
      USER_ROLES_TABLE: props.userRolesTable.tableName,
      DEVICES_TABLE: props.devicesTable.tableName,
      AUDIT_LOG_TABLE: props.auditLogTable.tableName,
      TRAINING_JOBS_TABLE: props.trainingJobsTable.tableName,
      LABELING_JOBS_TABLE: props.labelingJobsTable.tableName,
      PRE_LABELED_DATASETS_TABLE: props.preLabeledDatasetsTable.tableName,
      MODELS_TABLE: props.modelsTable.tableName,
      DEPLOYMENTS_TABLE: props.deploymentsTable.tableName,
      SETTINGS_TABLE: props.settingsTable.tableName,
      COMPONENTS_TABLE: props.componentsTable.tableName,
      SHARED_COMPONENTS_TABLE: props.sharedComponentsTable.tableName,
      PORTAL_ARTIFACTS_BUCKET: props.portalArtifactsBucket.bucketName,
      PORTAL_ACCOUNT_ID: cdk.Aws.ACCOUNT_ID,
      USER_POOL_ID: props.userPool.userPoolId,
      // Shared component configuration - update DDA_LOCAL_SERVER_VERSION when publishing new component versions
      DDA_LOCAL_SERVER_VERSION: '1.0.63',
      COMPONENT_BUCKET_PREFIX: 'dda-component',
    };

    // UseCases Lambda Handler
    const useCasesHandler = new lambda.Function(this, 'UseCasesHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'usecases.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('UseCases'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-08-auto-cors', // Force update with auto CORS configuration
        // CloudFront domain for auto-configuring CORS on Data Account buckets
        ...(props.cloudFrontDomain && { CLOUDFRONT_DOMAIN: props.cloudFrontDomain }),
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(120), // Increased for shared component provisioning
    });

    // Grant UseCases Lambda permission to list S3 objects for artifact discovery during onboarding
    const componentBucketNameForUsecases = `dda-component-${cdk.Aws.REGION}-${cdk.Aws.ACCOUNT_ID}`;
    useCasesHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        's3:ListBucket',
        's3:GetBucketPolicy',
        's3:PutBucketPolicy',
      ],
      resources: [`arn:aws:s3:::${componentBucketNameForUsecases}`],
    }));

    // Grant UseCases Lambda permission to list Greengrass component versions for dynamic version discovery
    useCasesHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'greengrass:ListComponentVersions',
      ],
      resources: [`arn:aws:greengrass:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:components:aws.edgeml.dda.LocalServer.*`],
    }));

    // Grant UseCases Lambda permission to configure EventBridge for cross-account event forwarding
    useCasesHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'events:PutPermission',
        'events:RemovePermission',
        'events:DescribeEventBus',
      ],
      resources: [`arn:aws:events:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:event-bus/default`],
    }));

    // Devices Lambda Handler
    const devicesHandler = new lambda.Function(this, 'DevicesHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'devices.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Devices'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2024-12-12-fixed-shared-utils', // Force update with fixed shared_utils.py
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // Device Logs Lambda Handler
    const deviceLogsHandler = new lambda.Function(this, 'DeviceLogsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'device_logs.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('DeviceLogs'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-19-device-logs',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60), // Longer timeout for log fetching
    });

    // Deployments Lambda Handler
    const deploymentsHandler = new lambda.Function(this, 'DeploymentsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'deployments.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Deployments'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-04-deployments',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Auth Lambda Handler
    const authHandler = new lambda.Function(this, 'AuthHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'auth.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Auth'),
      environment: {
        ...lambdaEnvironment,
        USER_POOL_ID: props.userPool.userPoolId,
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // User Management Lambda Handler
    const userManagementHandler = new lambda.Function(this, 'UserManagementHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'user_management.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('UserManagement'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2024-12-12-fixed-shared-utils', // Force update with fixed shared_utils.py
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // JWT Authorizer Lambda Handler (alternative to Cognito authorizer)
    const jwtAuthorizerHandler = new lambda.Function(this, 'JwtAuthorizerHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'jwt_authorizer.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('JwtAuthorizer'),
      environment: {
        COGNITO_USER_POOL_ID: props.userPool.userPoolId,
        COGNITO_REGION: cdk.Aws.REGION,
        ALLOWED_AUDIENCES: '', // Configure based on your needs
        ISSUER_WHITELIST: '', // Configure based on your identity providers
      },
      layers: [jwtLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // Datasets Lambda Handler
    const datasetsHandler = new lambda.Function(this, 'DatasetsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'datasets.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Datasets'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Pre-Labeled Datasets Lambda Handler
    const preLabeledDatasetsHandler = new lambda.Function(this, 'PreLabeledDatasetsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'pre_labeled_datasets.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('PreLabeledDatasets'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Labeling Lambda Handler
    const labelingHandler = new lambda.Function(this, 'LabelingHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'labeling.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Labeling'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2024-12-08-v1', // Force update - fixed usecase-jobs-index
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Labeling Monitor Lambda Handler (for EventBridge)
    const labelingMonitorHandler = new lambda.Function(this, 'LabelingMonitorHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'labeling_monitor.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('LabelingMonitor'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // Training Lambda Handler
    const trainingHandler = new lambda.Function(this, 'TrainingHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'training.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Training'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2024-12-08-v1', // Force update - fixed job name validation
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60), // Longer timeout for SageMaker API calls
    });

    // Compilation Lambda Handler
    const compilationHandler = new lambda.Function(this, 'CompilationHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'compilation.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Compilation'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(300), // 5 minutes for model extraction and repackaging
      memorySize: 1024, // More memory for tar.gz extraction
      ephemeralStorageSize: cdk.Size.gibibytes(4), // 4GB temp storage for large models
    });

    // Packaging Lambda Handler
    const packagingHandler = new lambda.Function(this, 'PackagingHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'packaging.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Packaging'),
      environment: {
        ...lambdaEnvironment,
        STORAGE_WARNING_THRESHOLD: '6442450944', // 6GB in bytes
        STORAGE_CRITICAL_THRESHOLD: '7516192768', // 7GB in bytes
        ENABLE_STREAMING_PROCESSING: 'true',
        MAX_CONCURRENT_OPERATIONS: '2',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(900), // 15 minutes for large model processing
      memorySize: 3008, // Increased memory for better performance
      ephemeralStorageSize: cdk.Size.gibibytes(8), // 8GB temp storage for large models
    });

    // Greengrass Publishing Lambda Handler
    const greengrassPublishHandler = new lambda.Function(this, 'GreengrassPublishHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'greengrass_publish.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('GreengrassPublish'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(300), // 5 minutes for component creation and monitoring
    });

    // Update packaging handler environment with Greengrass function name
    packagingHandler.addEnvironment('GREENGRASS_PUBLISH_FUNCTION_NAME', greengrassPublishHandler.functionName);

    // Components Lambda Handler for Greengrass Component Browser
    const componentsHandler = new lambda.Function(this, 'ComponentsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'components.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Components'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60), // 1 minute for component discovery and management
    });

    // Data Management Lambda Handler for S3 bucket/folder management
    const dataManagementHandler = new lambda.Function(this, 'DataManagementHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'data_management.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('DataManagement'),
      environment: lambdaEnvironment,
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Shared Components Lambda Handler for dda-LocalServer provisioning
    const sharedComponentsHandler = new lambda.Function(this, 'SharedComponentsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'shared_components.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('SharedComponents'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-07-bucket-policy-update',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(120), // 2 minutes for cross-account component creation
    });

    // Data Accounts Lambda Handler for managing registered Data Accounts
    const dataAccountsHandler = new lambda.Function(this, 'DataAccountsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'data_accounts.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('DataAccounts'),
      environment: {
        ...lambdaEnvironment,
        DATA_ACCOUNTS_TABLE: props.dataAccountsTable.tableName,
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // Model Import Lambda Handler for BYOM (Bring Your Own Model)
    const modelImportHandler = new lambda.Function(this, 'ModelImportHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'model_import.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('ModelImport'),
      environment: {
        ...lambdaEnvironment,
        COMPILATION_FUNCTION_NAME: compilationHandler.functionName,
        CODE_VERSION: '2025-01-10-byom',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(300), // 5 minutes for model validation
      memorySize: 1024, // More memory for tar.gz extraction
      ephemeralStorageSize: cdk.Size.gibibytes(4), // 4GB temp storage for large models
    });

    // Grant Model Import Lambda permission to invoke compilation
    compilationHandler.grantInvoke(modelImportHandler);

    // Model Converter Lambda Handler for auto-generating DDA metadata
    const modelConverterHandler = new lambda.Function(this, 'ModelConverterHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'model_converter.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('ModelConverter'),
      environment: {
        ...lambdaEnvironment,
        MODEL_IMPORT_FUNCTION_NAME: modelImportHandler.functionName,
        CODE_VERSION: '2025-01-10-converter',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(600), // 10 minutes for large model conversion
      memorySize: 3008, // More memory for PyTorch model inspection
      ephemeralStorageSize: cdk.Size.gibibytes(8), // 8GB temp storage for large models
    });

    // Grant Model Converter Lambda permission to invoke Model Import
    modelImportHandler.grantInvoke(modelConverterHandler);

    // Models Registry Lambda Handler
    const modelsHandler = new lambda.Function(this, 'ModelsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'models.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('Models'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-19-models-registry',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // Audit Logs Lambda Handler
    const auditLogsHandler = new lambda.Function(this, 'AuditLogsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'audit_logs.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('AuditLogs'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2025-01-20-audit-logs',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

    // Grant Audit Logs Lambda read access to the audit log table
    props.auditLogTable.grantReadData(auditLogsHandler);

    // Grant SharedComponents Lambda permission to update the GDK component bucket policy
    // This is needed to add new usecase accounts to the bucket policy during onboarding
    const componentBucketName = `dda-component-${cdk.Aws.REGION}-${cdk.Aws.ACCOUNT_ID}`;
    sharedComponentsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        's3:GetBucketPolicy',
        's3:PutBucketPolicy',
      ],
      resources: [`arn:aws:s3:::${componentBucketName}`],
    }));

    // Grant permission to list S3 objects for artifact discovery
    sharedComponentsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        's3:ListBucket',
      ],
      resources: [`arn:aws:s3:::${componentBucketName}`],
    }));

    // Grant permission to list and get Greengrass component versions for dynamic version discovery
    // Note: GetComponent requires full ARN with :versions:X.Y.Z, so we need wildcard for versions too
    sharedComponentsHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'greengrass:ListComponentVersions',
        'greengrass:GetComponent',
      ],
      resources: [
        `arn:aws:greengrass:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:components:aws.edgeml.dda.LocalServer.*`,
        `arn:aws:greengrass:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:components:aws.edgeml.dda.LocalServer.*:versions:*`,
      ],
    }));

    // SNS Topic for training alerts
    const trainingAlertTopic = new sns.Topic(this, 'TrainingAlertTopic', {
      displayName: 'DDA Portal Training Alerts',
      topicName: 'dda-portal-training-alerts',
    });

    // Training Events Lambda Handler (for EventBridge)
    const trainingEventsHandler = new lambda.Function(this, 'TrainingEventsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'training_events.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('TrainingEvents'),
      environment: {
        ...lambdaEnvironment,
        ALERT_TOPIC_ARN: trainingAlertTopic.topicArn,
        COMPILATION_FUNCTION_NAME: compilationHandler.functionName,
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Grant SNS publish permissions
    trainingAlertTopic.grantPublish(trainingEventsHandler);
    
    // Grant permission to invoke compilation Lambda
    compilationHandler.grantInvoke(trainingEventsHandler);

    // Compilation Events Lambda Handler (for EventBridge)
    const compilationEventsHandler = new lambda.Function(this, 'CompilationEventsHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'compilation_events.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('CompilationEvents'),
      environment: {
        ...lambdaEnvironment,
        ALERT_TOPIC_ARN: trainingAlertTopic.topicArn,
        PACKAGING_FUNCTION_NAME: packagingHandler.functionName,
        GREENGRASS_PUBLISH_FUNCTION_NAME: greengrassPublishHandler.functionName,
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    // Grant SNS publish permissions
    trainingAlertTopic.grantPublish(compilationEventsHandler);
    
    // Grant permission to invoke packaging and Greengrass publish Lambdas
    packagingHandler.grantInvoke(compilationEventsHandler);
    greengrassPublishHandler.grantInvoke(compilationEventsHandler);
    greengrassPublishHandler.grantInvoke(packagingHandler);

    // EventBridge Rule for SageMaker Training Job State Changes
    const trainingStateChangeRule = new events.Rule(this, 'TrainingStateChangeRule', {
      ruleName: 'dda-portal-training-state-change',
      description: 'Capture SageMaker training job state changes',
      eventPattern: {
        source: ['aws.sagemaker'],
        detailType: ['SageMaker Training Job State Change'],
        detail: {
          TrainingJobStatus: ['Completed', 'Failed', 'Stopped'],
        },
      },
    });

    // Add Lambda as target for the EventBridge rule
    trainingStateChangeRule.addTarget(new targets.LambdaFunction(trainingEventsHandler));

    // EventBridge Rule for SageMaker Compilation Job State Changes
    const compilationStateChangeRule = new events.Rule(this, 'CompilationStateChangeRule', {
      ruleName: 'dda-portal-compilation-state-change',
      description: 'Capture SageMaker compilation job state changes',
      eventPattern: {
        source: ['aws.sagemaker'],
        detailType: ['SageMaker Compilation Job State Change'],
        detail: {
          CompilationJobStatus: ['Completed', 'Failed', 'Stopped'],
        },
      },
    });

    // Add Lambda as target for the compilation EventBridge rule
    compilationStateChangeRule.addTarget(new targets.LambdaFunction(compilationEventsHandler));

    // Enable SageMaker EventBridge integration
    // This ensures SageMaker sends events to EventBridge for training and compilation jobs
    const sagemakerEventBridgeRole = new iam.Role(this, 'SageMakerEventBridgeRole', {
      assumedBy: new iam.ServicePrincipal('sagemaker.amazonaws.com'),
      description: 'Role for SageMaker to send events to EventBridge',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonEventBridgeFullAccess'),
      ],
    });

    // Create EventBridge configuration for SageMaker
    // Note: This creates the IAM role, but SageMaker EventBridge integration
    // needs to be enabled at the account level. This can be done via:
    // 1. AWS Console: SageMaker → Settings → EventBridge
    // 2. AWS CLI: aws events put-rule --name sagemaker-events --event-pattern '{"source":["aws.sagemaker"]}'
    // 3. Or automatically via a custom resource (implemented below)

    // Custom resource to enable SageMaker EventBridge integration
    const enableSageMakerEventBridge = new lambda.Function(this, 'EnableSageMakerEventBridge', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromInline(`
import json
import boto3
import cfnresponse

def handler(event, context):
    """
    Custom resource to enable SageMaker EventBridge integration
    This ensures SageMaker sends training and compilation job events to EventBridge
    """
    try:
        if event['RequestType'] == 'Create' or event['RequestType'] == 'Update':
            # Create EventBridge client
            events_client = boto3.client('events')
            
            # Enable SageMaker events by creating a rule that captures all SageMaker events
            # This effectively enables SageMaker to send events to EventBridge
            rule_name = 'sagemaker-eventbridge-enabler'
            
            try:
                events_client.put_rule(
                    Name=rule_name,
                    EventPattern=json.dumps({
                        "source": ["aws.sagemaker"],
                        "detail-type": [
                            "SageMaker Training Job State Change",
                            "SageMaker Compilation Job State Change"
                        ]
                    }),
                    State='ENABLED',
                    Description='Enable SageMaker EventBridge integration for DDA Portal'
                )
                
                # The rule exists but has no targets - this is intentional
                # It just enables SageMaker to send events to EventBridge
                # Our actual processing rules (with targets) are defined separately
                
                print(f"Successfully enabled SageMaker EventBridge integration via rule: {rule_name}")
                cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                    'Message': 'SageMaker EventBridge integration enabled successfully',
                    'RuleName': rule_name
                })
                
            except Exception as e:
                if 'already exists' in str(e).lower():
                    print(f"Rule {rule_name} already exists - SageMaker EventBridge integration already enabled")
                    cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                        'Message': 'SageMaker EventBridge integration already enabled',
                        'RuleName': rule_name
                    })
                else:
                    raise e
                    
        elif event['RequestType'] == 'Delete':
            # Clean up the enabler rule on stack deletion
            events_client = boto3.client('events')
            rule_name = 'sagemaker-eventbridge-enabler'
            
            try:
                events_client.delete_rule(Name=rule_name)
                print(f"Deleted SageMaker EventBridge enabler rule: {rule_name}")
            except Exception as e:
                print(f"Could not delete rule {rule_name}: {e}")
                # Don't fail deletion if rule doesn't exist
            
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                'Message': 'SageMaker EventBridge integration cleanup completed'
            })
            
    except Exception as e:
        print(f"Error in SageMaker EventBridge enabler: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {
            'Message': f'Failed to configure SageMaker EventBridge integration: {str(e)}'
        })
      `),
      timeout: cdk.Duration.seconds(60),
    });

    // Grant permissions to the custom resource Lambda
    enableSageMakerEventBridge.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'events:PutRule',
        'events:DeleteRule',
        'events:DescribeRule',
        'events:ListRules',
      ],
      resources: ['*'],
    }));

    // Create the custom resource
    const sagemakerEventBridgeIntegration = new cdk.CustomResource(this, 'SageMakerEventBridgeIntegration', {
      serviceToken: enableSageMakerEventBridge.functionArn,
      properties: {
        // Force update when stack is updated
        Timestamp: Date.now().toString(),
      },
    });

    // Cross-Account EventBridge Permission
    // Allow UseCase Accounts to send events to this account's default event bus
    // This enables real-time status updates for training/compilation jobs running in UseCase Accounts
    const defaultEventBus = events.EventBus.fromEventBusName(this, 'DefaultEventBus', 'default');
    
    // Create a resource-based policy to allow cross-account event delivery
    // Note: You need to add each UseCase Account ID that should be allowed to send events
    // This can be done via AWS CLI or by adding account IDs to the useCaseAccountIds array
    const useCaseAccountIds = process.env.USECASE_ACCOUNT_IDS?.split(',') || [];
    
    if (useCaseAccountIds.length > 0) {
      // Create policy statement for each UseCase Account
      const eventBusPolicy = new events.CfnEventBusPolicy(this, 'CrossAccountEventBusPolicy', {
        eventBusName: 'default',
        statementId: 'AllowUseCaseAccountsToSendEvents',
        action: 'events:PutEvents',
        principal: '*',
        condition: {
          type: 'StringEquals',
          key: 'aws:PrincipalAccount',
          value: useCaseAccountIds.join(','),
        },
      });
    }

    // Output instructions for manual cross-account setup
    new cdk.CfnOutput(this, 'CrossAccountEventBridgeSetup', {
      value: `To enable cross-account EventBridge from UseCase Accounts, run in Portal Account:
aws events put-permission --event-bus-name default --action events:PutEvents --principal <USECASE_ACCOUNT_ID> --statement-id AllowUseCaseAccount<N>`,
      description: 'Instructions for enabling cross-account EventBridge',
    });

    // API Gateway in Nested Stack
    // Moving API Gateway to a nested stack solves the 500 resource limit
    const apiGatewayStack = new ApiGatewayStack(this, 'ApiGateway', {
      userPool: props.userPool,
      authHandler,
      userManagementHandler,
      useCasesHandler,
      devicesHandler,
      deviceLogsHandler,
      deploymentsHandler,
      dataManagementHandler,
      datasetsHandler,
      preLabeledDatasetsHandler,
      labelingHandler,
      trainingHandler,
      compilationHandler,
      packagingHandler,
      greengrassPublishHandler,
      modelsHandler,
      modelImportHandler,
      modelConverterHandler,
      componentsHandler,
      sharedComponentsHandler,
      dataAccountsHandler,
      auditLogsHandler,
      lambdaEnvironment,
      createLambdaRole,
      sharedLayer,
    });

    this.api = apiGatewayStack.api;
    this.apiUrl = apiGatewayStack.apiUrl;

    // Custom Resource to update UseCases Lambda environment variable with API Gateway ID
    // This avoids circular dependency by updating the Lambda AFTER both resources are created
    const updateLambdaEnv = new lambda.Function(this, 'UpdateLambdaEnv', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromInline(`
import json
import boto3
import cfnresponse

lambda_client = boto3.client('lambda')

def handler(event, context):
    try:
        if event['RequestType'] in ['Create', 'Update']:
            function_name = event['ResourceProperties']['FunctionName']
            api_gateway_id = event['ResourceProperties']['ApiGatewayId']
            
            # Get current environment variables
            response = lambda_client.get_function_configuration(FunctionName=function_name)
            env_vars = response.get('Environment', {}).get('Variables', {})
            
            # Add API_GATEWAY_ID
            env_vars['API_GATEWAY_ID'] = api_gateway_id
            
            # Update Lambda environment
            lambda_client.update_function_configuration(
                FunctionName=function_name,
                Environment={'Variables': env_vars}
            )
            
            print(f"Updated {function_name} with API_GATEWAY_ID={api_gateway_id}")
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                'Message': f'Updated Lambda environment'
            })
        else:
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
    except Exception as e:
        print(f"Error: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {
            'Message': str(e)
        })
      `),
      timeout: cdk.Duration.seconds(60),
    });

    // Grant permissions to update Lambda configuration
    updateLambdaEnv.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'lambda:GetFunctionConfiguration',
        'lambda:UpdateFunctionConfiguration',
      ],
      resources: [useCasesHandler.functionArn],
    }));

    // Create custom resource
    const lambdaEnvUpdater = new cdk.CustomResource(this, 'LambdaEnvUpdater', {
      serviceToken: updateLambdaEnv.functionArn,
      properties: {
        FunctionName: useCasesHandler.functionName,
        ApiGatewayId: apiGatewayStack.api.restApiId,
        // Force update when API changes
        Timestamp: Date.now().toString(),
      },
    });

    // Ensure custom resource runs after both Lambda and API Gateway are created
    lambdaEnvUpdater.node.addDependency(useCasesHandler);
    lambdaEnvUpdater.node.addDependency(apiGatewayStack);

    // Outputs
    new cdk.CfnOutput(this, 'ApiUrl', {
      value: apiGatewayStack.apiUrl,
      description: 'API Gateway URL',
      exportName: 'EdgeCVPortalApiUrl',
    });

    new cdk.CfnOutput(this, 'ApiId', {
      value: apiGatewayStack.api.restApiId,
      description: 'API Gateway ID',
    });

    new cdk.CfnOutput(this, 'TrainingAlertTopicArn', {
      value: trainingAlertTopic.topicArn,
      description: 'SNS Topic ARN for training alerts',
      exportName: 'EdgeCVPortalTrainingAlertTopicArn',
    });

    new cdk.CfnOutput(this, 'SageMakerEventBridgeStatus', {
      value: sagemakerEventBridgeIntegration.getAttString('Message'),
      description: 'SageMaker EventBridge integration status',
    });
  }
}
