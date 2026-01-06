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
  portalArtifactsBucket: s3.Bucket;
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

      // Grant S3 permissions for portal artifacts bucket
      props.portalArtifactsBucket.grantReadWrite(role);

      // Grant SageMaker, Greengrass, CloudWatch Logs, and STS permissions
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
          'greengrass:CreateComponentVersion',
          'greengrass:DescribeComponent',
          'greengrass:ListComponents',
          'logs:GetLogEvents',
          'logs:DescribeLogStreams',
          'logs:FilterLogEvents',
          'sts:AssumeRole',
        ],
        resources: ['*'],
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
        ],
        resources: ['*'], // Restricted by assumed role in UseCase Account
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
    };

    // UseCases Lambda Handler
    const useCasesHandler = new lambda.Function(this, 'UseCasesHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'usecases.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: createLambdaRole('UseCases'),
      environment: {
        ...lambdaEnvironment,
        CODE_VERSION: '2024-12-12-fixed-shared-utils', // Force update with fixed shared_utils.py
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(30),
    });

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
        CODE_VERSION: '2025-01-04-shared-components',
      },
      layers: [sharedLayer],
      timeout: cdk.Duration.seconds(120), // 2 minutes for cross-account component creation
    });

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

    // API Gateway
    this.api = new apigateway.RestApi(this, 'EdgeCVPortalAPI', {
      restApiName: 'Edge CV Portal API',
      description: 'API for Edge CV Admin Portal',
      deployOptions: {
        stageName: 'v1',
        tracingEnabled: true,
        loggingLevel: apigateway.MethodLoggingLevel.INFO,
        dataTraceEnabled: true,
        metricsEnabled: true,
      },
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: [
          'Content-Type',
          'X-Amz-Date',
          'Authorization',
          'X-Api-Key',
          'X-Amz-Security-Token',
        ],
      },
    });

    // Cognito Authorizer
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'CognitoAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'EdgeCVPortalAuthorizer',
      identitySource: 'method.request.header.Authorization',
    });

    // JWT Lambda Authorizer (alternative to Cognito) - commented out for now
    // Uncomment and configure when needed for SSO integration
    /*
    const jwtAuthorizer = new apigateway.TokenAuthorizer(this, 'JwtAuthorizer', {
      handler: jwtAuthorizerHandler,
      authorizerName: 'EdgeCVPortalJwtAuthorizer',
      identitySource: 'method.request.header.Authorization',
      resultsCacheTtl: cdk.Duration.minutes(5),
    });
    */

    // Create Lambda integrations (this helps avoid circular dependencies)
    const useCasesIntegration = new apigateway.LambdaIntegration(useCasesHandler);
    const devicesIntegration = new apigateway.LambdaIntegration(devicesHandler);
    const deploymentsIntegration = new apigateway.LambdaIntegration(deploymentsHandler);
    const authIntegration = new apigateway.LambdaIntegration(authHandler);
    const userManagementIntegration = new apigateway.LambdaIntegration(userManagementHandler);
    const datasetsIntegration = new apigateway.LambdaIntegration(datasetsHandler);
    const preLabeledDatasetsIntegration = new apigateway.LambdaIntegration(preLabeledDatasetsHandler);
    const labelingIntegration = new apigateway.LambdaIntegration(labelingHandler);
    const trainingIntegration = new apigateway.LambdaIntegration(trainingHandler);
    const compilationIntegration = new apigateway.LambdaIntegration(compilationHandler);
    const packagingIntegration = new apigateway.LambdaIntegration(packagingHandler);
    const greengrassPublishIntegration = new apigateway.LambdaIntegration(greengrassPublishHandler);
    const componentsIntegration = new apigateway.LambdaIntegration(componentsHandler);
    const dataManagementIntegration = new apigateway.LambdaIntegration(dataManagementHandler);
    const sharedComponentsIntegration = new apigateway.LambdaIntegration(sharedComponentsHandler);

    // API Resources
    // Auth endpoints
    const authResource = this.api.root.addResource('auth');
    authResource.addResource('me').addMethod(
      'GET',
      authIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Token refresh endpoint (no authorization required as it uses refresh token)
    authResource.addResource('refresh').addMethod(
      'POST',
      authIntegration,
      {
        authorizationType: apigateway.AuthorizationType.NONE,
      }
    );

    // User Management endpoints
    const usersResource = this.api.root.addResource('users');
    usersResource.addMethod(
      'GET',
      userManagementIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Assign role endpoint
    const assignRoleResource = usersResource.addResource('assign-role');
    assignRoleResource.addMethod(
      'POST',
      userManagementIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // User-specific endpoints
    const userResource = usersResource.addResource('{user_id}');
    
    // User permissions endpoint
    const userPermissionsResource = userResource.addResource('permissions');
    userPermissionsResource.addMethod(
      'GET',
      userManagementIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // User roles endpoint
    const userRolesResource = userResource.addResource('roles');
    const userRoleResource = userRolesResource.addResource('{usecase_id}');
    userRoleResource.addMethod(
      'DELETE',
      userManagementIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Current user's use cases
    const meResource = usersResource.addResource('me');
    const myUsecasesResource = meResource.addResource('usecases');
    myUsecasesResource.addMethod(
      'GET',
      userManagementIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // UseCases endpoints
    const usecasesResource = this.api.root.addResource('usecases');
    usecasesResource.addMethod(
      'GET',
      useCasesIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    usecasesResource.addMethod(
      'POST',
      useCasesIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    const usecaseResource = usecasesResource.addResource('{id}');
    usecaseResource.addMethod(
      'GET',
      useCasesIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    usecaseResource.addMethod(
      'PUT',
      useCasesIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    usecaseResource.addMethod(
      'DELETE',
      useCasesIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Data Management endpoints (under usecases/{usecase_id}/data)
    const usecaseDataResource = usecaseResource.addResource('data');
    
    // Buckets endpoints
    const dataBucketsResource = usecaseDataResource.addResource('buckets');
    dataBucketsResource.addMethod(
      'GET',
      dataManagementIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    dataBucketsResource.addMethod(
      'POST',
      dataManagementIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Folders endpoints
    const dataFoldersResource = usecaseDataResource.addResource('folders');
    dataFoldersResource.addMethod(
      'GET',
      dataManagementIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    dataFoldersResource.addMethod(
      'POST',
      dataManagementIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Upload URL endpoint
    const dataUploadUrlResource = usecaseDataResource.addResource('upload-url');
    dataUploadUrlResource.addMethod(
      'POST',
      dataManagementIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Batch upload URLs endpoint
    const dataBatchUploadResource = usecaseDataResource.addResource('batch-upload-urls');
    dataBatchUploadResource.addMethod(
      'POST',
      dataManagementIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Configure data account endpoint
    const dataConfigureResource = usecaseDataResource.addResource('configure');
    dataConfigureResource.addMethod(
      'POST',
      dataManagementIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Datasets endpoints
    const datasetsResource = this.api.root.addResource('datasets');
    datasetsResource.addMethod(
      'GET',
      datasetsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    const datasetsCountResource = datasetsResource.addResource('count');
    datasetsCountResource.addMethod(
      'POST',
      datasetsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Pre-labeled datasets endpoints
    const preLabeledResource = datasetsResource.addResource('pre-labeled');
    preLabeledResource.addMethod(
      'GET',
      preLabeledDatasetsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    preLabeledResource.addMethod(
      'POST',
      preLabeledDatasetsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    const preLabeledItemResource = preLabeledResource.addResource('{id}');
    preLabeledItemResource.addMethod(
      'GET',
      preLabeledDatasetsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    preLabeledItemResource.addMethod(
      'DELETE',
      preLabeledDatasetsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Manifest validation endpoint
    const validateManifestResource = datasetsResource.addResource('validate-manifest');
    validateManifestResource.addMethod(
      'POST',
      preLabeledDatasetsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Image preview endpoint
    const previewResource = datasetsResource.addResource('preview');
    previewResource.addMethod(
      'GET',
      datasetsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Workteams endpoint (for listing available workteams)
    const workteamsResource = this.api.root.addResource('workteams');
    workteamsResource.addMethod(
      'GET',
      labelingIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Labeling endpoints
    const labelingResource = this.api.root.addResource('labeling');
    labelingResource.addMethod(
      'GET',
      labelingIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    labelingResource.addMethod(
      'POST',
      labelingIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    const labelingJobResource = labelingResource.addResource('{id}');
    labelingJobResource.addMethod(
      'GET',
      labelingIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    const labelingManifestResource = labelingJobResource.addResource('manifest');
    labelingManifestResource.addMethod(
      'GET',
      labelingIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Devices endpoints
    const devicesResource = this.api.root.addResource('devices');
    devicesResource.addMethod(
      'GET',
      devicesIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    const deviceResource = devicesResource.addResource('{id}');
    deviceResource.addMethod(
      'GET',
      devicesIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Deployments endpoints
    const deploymentsResource = this.api.root.addResource('deployments');
    deploymentsResource.addMethod(
      'GET',
      deploymentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    deploymentsResource.addMethod(
      'POST',
      deploymentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    const deploymentResource = deploymentsResource.addResource('{id}');
    deploymentResource.addMethod(
      'GET',
      deploymentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    deploymentResource.addMethod(
      'DELETE',
      deploymentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Training endpoints
    const trainingResource = this.api.root.addResource('training');
    trainingResource.addMethod(
      'GET',
      trainingIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    trainingResource.addMethod(
      'POST',
      trainingIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    const trainingJobResource = trainingResource.addResource('{id}');
    trainingJobResource.addMethod(
      'GET',
      trainingIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Training logs endpoint
    const trainingLogsResource = trainingJobResource.addResource('logs');
    trainingLogsResource.addMethod(
      'GET',
      trainingIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Training logs download endpoint
    const trainingLogsDownloadResource = trainingLogsResource.addResource('download');
    trainingLogsDownloadResource.addMethod(
      'GET',
      trainingIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Compilation endpoints
    const compileResource = trainingJobResource.addResource('compile');
    compileResource.addMethod(
      'POST',
      compilationIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    compileResource.addMethod(
      'GET',
      compilationIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Packaging endpoints
    const packageResource = trainingJobResource.addResource('package');
    packageResource.addMethod(
      'POST',
      packagingIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Greengrass publishing endpoints
    const publishResource = trainingJobResource.addResource('publish');
    publishResource.addMethod(
      'POST',
      greengrassPublishIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Components endpoints for Greengrass Component Browser
    const componentsResource = this.api.root.addResource('components');
    componentsResource.addMethod(
      'GET',
      componentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    componentsResource.addMethod(
      'POST',
      componentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    const componentResource = componentsResource.addResource('{id}');
    componentResource.addMethod(
      'GET',
      componentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );
    componentResource.addMethod(
      'DELETE',
      componentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    const componentVersionsResource = componentResource.addResource('versions');
    componentVersionsResource.addMethod(
      'GET',
      componentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Component discovery endpoint
    const discoverResource = componentsResource.addResource('discover');
    discoverResource.addMethod(
      'POST',
      componentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Shared Components endpoints for dda-LocalServer provisioning
    const sharedComponentsResource = this.api.root.addResource('shared-components');
    sharedComponentsResource.addMethod(
      'GET',
      sharedComponentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Available shared components from portal
    const availableSharedResource = sharedComponentsResource.addResource('available');
    availableSharedResource.addMethod(
      'GET',
      sharedComponentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    // Provision shared components to usecase
    const provisionSharedResource = sharedComponentsResource.addResource('provision');
    provisionSharedResource.addMethod(
      'POST',
      sharedComponentsIntegration,
      {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      }
    );

    this.apiUrl = this.api.url;

    // Outputs
    new cdk.CfnOutput(this, 'ApiUrl', {
      value: this.api.url,
      description: 'API Gateway URL',
      exportName: 'EdgeCVPortalApiUrl',
    });

    new cdk.CfnOutput(this, 'ApiId', {
      value: this.api.restApiId,
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
