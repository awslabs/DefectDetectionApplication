import * as cdk from 'aws-cdk-lib';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

export interface TrainingWorkflowStackProps extends cdk.StackProps {
  trainingJobsTable: dynamodb.Table;
  modelsTable: dynamodb.Table;
  settingsTable: dynamodb.Table;
  useCasesTable: dynamodb.Table;
  trainingHandler: lambda.Function;
  compilationHandler: lambda.Function;
  packagingHandler: lambda.Function;
  greengrassPublishHandler: lambda.Function;
}

export class TrainingWorkflowStack extends cdk.Stack {
  public readonly stateMachine: sfn.StateMachine;

  constructor(scope: Construct, id: string, props: TrainingWorkflowStackProps) {
    super(scope, id, props);

    // SNS Topic for notifications
    const alertTopic = new sns.Topic(this, 'TrainingAlertTopic', {
      displayName: 'Training Pipeline Alerts',
    });

    // Lambda functions for workflow steps
    const assumeRoleFunction = new lambda.Function(this, 'AssumeUseCaseRole', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromInline(`
import json
import boto3

sts = boto3.client('sts')

def handler(event, context):
    """Assume cross-account role for use case"""
    role_arn = event['usecase']['cross_account_role_arn']
    external_id = event['usecase']['external_id']
    
    response = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName='EdgeCVPortalTraining',
        ExternalId=external_id,
        DurationSeconds=3600
    )
    
    return {
        'credentials': {
            'AccessKeyId': response['Credentials']['AccessKeyId'],
            'SecretAccessKey': response['Credentials']['SecretAccessKey'],
            'SessionToken': response['Credentials']['SessionToken']
        },
        'usecase': event['usecase'],
        'training_config': event['training_config']
    }
      `),
      timeout: cdk.Duration.seconds(30),
    });

    assumeRoleFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['sts:AssumeRole'],
      resources: ['*'], // Will be scoped to specific roles with ExternalId
    }));

    const startTrainingFunction = new lambda.Function(this, 'StartTraining', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromInline(`
import json
import boto3
from datetime import datetime

def handler(event, context):
    """Start SageMaker training job"""
    credentials = event['credentials']
    training_config = event['training_config']
    
    # Create SageMaker client with assumed role credentials
    sagemaker = boto3.client(
        'sagemaker',
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken']
    )
    
    training_job_name = f"edge-cv-{training_config['model_name']}-{int(datetime.now().timestamp())}"
    
    response = sagemaker.create_training_job(
        TrainingJobName=training_job_name,
        AlgorithmSpecification={
            'TrainingImage': training_config['algorithm_uri'],
            'TrainingInputMode': 'File'
        },
        RoleArn=training_config['sagemaker_role_arn'],
        InputDataConfig=[{
            'ChannelName': 'training',
            'DataSource': {
                'S3DataSource': {
                    'S3DataType': 'S3Prefix',
                    'S3Uri': training_config['dataset_s3_uri'],
                    'S3DataDistributionType': 'FullyReplicated'
                }
            }
        }],
        OutputDataConfig={
            'S3OutputPath': training_config['output_s3_path']
        },
        ResourceConfig={
            'InstanceType': training_config['instance_type'],
            'InstanceCount': 1,
            'VolumeSizeInGB': 30
        },
        StoppingCondition={
            'MaxRuntimeInSeconds': 86400  # 24 hours
        },
        HyperParameters=training_config.get('hyperparameters', {})
    )
    
    return {
        'training_job_name': training_job_name,
        'training_job_arn': response['TrainingJobArn'],
        'credentials': credentials,
        'training_config': training_config
    }
      `),
      timeout: cdk.Duration.minutes(1),
      environment: {
        TRAINING_JOBS_TABLE: props.trainingJobsTable.tableName,
      },
    });

    props.trainingJobsTable.grantReadWriteData(startTrainingFunction);

    const checkTrainingStatusFunction = new lambda.Function(this, 'CheckTrainingStatus', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromInline(`
import json
import boto3

def handler(event, context):
    """Check SageMaker training job status"""
    credentials = event['credentials']
    training_job_name = event['training_job_name']
    
    sagemaker = boto3.client(
        'sagemaker',
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken']
    )
    
    response = sagemaker.describe_training_job(
        TrainingJobName=training_job_name
    )
    
    return {
        'status': response['TrainingJobStatus'],
        'model_artifacts': response.get('ModelArtifacts', {}).get('S3ModelArtifacts'),
        'training_job_name': training_job_name,
        'credentials': credentials,
        'training_config': event['training_config']
    }
      `),
      timeout: cdk.Duration.seconds(30),
    });

    const startCompilationFunction = new lambda.Function(this, 'StartCompilation', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromInline(`
import json
import boto3
from datetime import datetime

def handler(event, context):
    """Start SageMaker Neo compilation for a target"""
    credentials = event['credentials']
    model_artifacts = event['model_artifacts']
    target = event['target']
    training_config = event['training_config']
    
    sagemaker = boto3.client(
        'sagemaker',
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken']
    )
    
    compilation_job_name = f"edge-cv-compile-{target['name']}-{int(datetime.now().timestamp())}"
    
    response = sagemaker.create_compilation_job(
        CompilationJobName=compilation_job_name,
        RoleArn=training_config['sagemaker_role_arn'],
        InputConfig={
            'S3Uri': model_artifacts,
            'DataInputConfig': training_config.get('data_shape', '{"input":[1,3,224,224]}'),
            'Framework': 'PYTORCH',
            'FrameworkVersion': '1.13'
        },
        OutputConfig={
            'S3OutputLocation': f"{training_config['output_s3_path']}/compiled/{target['name']}",
            'TargetPlatform': {
                'Arch': target['arch'].upper(),
                'Os': 'LINUX'
            }
        },
        StoppingCondition={
            'MaxRuntimeInSeconds': 900
        }
    )
    
    return {
        'compilation_job_name': compilation_job_name,
        'compilation_job_arn': response['CompilationJobArn'],
        'target': target,
        'credentials': credentials,
        'training_config': training_config
    }
      `),
      timeout: cdk.Duration.minutes(1),
    });

    const publishComponentFunction = new lambda.Function(this, 'PublishComponent', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.handler',
      code: lambda.Code.fromInline(`
import json
import boto3
from datetime import datetime

def handler(event, context):
    """Publish Greengrass component with compiled model"""
    credentials = event['credentials']
    compiled_model_uri = event['compiled_model_uri']
    target = event['target']
    training_config = event['training_config']
    
    greengrass = boto3.client(
        'greengrassv2',
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken']
    )
    
    component_name = f"com.edgecv.{training_config['model_name']}.{target['name']}"
    component_version = training_config.get('model_version', '1.0.0')
    
    recipe = {
        'RecipeFormatVersion': '2020-01-25',
        'ComponentName': component_name,
        'ComponentVersion': component_version,
        'ComponentDescription': f"Edge CV model {training_config['model_name']} for {target['name']}",
        'ComponentPublisher': 'Edge CV Portal',
        'ComponentConfiguration': {
            'DefaultConfiguration': {
                'ModelName': training_config['model_name'],
                'TargetPlatform': target['name']
            }
        },
        'Manifests': [{
            'Platform': {
                'os': 'linux',
                'architecture': target['arch']
            },
            'Lifecycle': {
                'Startup': {
                    'Script': 'python3 {artifacts:path}/model_convertor.py'
                },
                'Shutdown': {
                    'Script': 'python3 {artifacts:path}/convert_model_cleanup.py'
                }
            },
            'Artifacts': [{
                'URI': compiled_model_uri,
                'Unarchive': 'ZIP',
                'Permission': {
                    'Read': 'OWNER',
                    'Execute': 'OWNER'
                }
            }]
        }]
    }
    
    response = greengrass.create_component_version(
        inlineRecipe=json.dumps(recipe).encode('utf-8')
    )
    
    return {
        'component_arn': response['arn'],
        'component_name': component_name,
        'component_version': component_version,
        'target': target
    }
      `),
      timeout: cdk.Duration.minutes(2),
    });

    props.modelsTable.grantReadWriteData(publishComponentFunction);

    // Note: The actual workflow uses EventBridge for training completion
    // This Step Functions workflow is for manual orchestration if needed
    
    // Step 1: Wait for training completion (triggered by EventBridge)
    const waitForTraining = new sfn.Pass(this, 'Training Completed', {
      comment: 'Training job completed via EventBridge event',
    });

    // Step 2: Start compilation for all targets
    const startCompilationTask = new tasks.LambdaInvoke(this, 'Start Compilation', {
      lambdaFunction: props.compilationHandler,
      payload: sfn.TaskInput.fromObject({
        pathParameters: {
          training_id: sfn.JsonPath.stringAt('$.training_id'),
        },
        body: sfn.JsonPath.stringAt('$.compilation_config'),
        httpMethod: 'POST',
        path: '/api/v1/training/$/compile',
      }),
      outputPath: '$.Payload',
    });

    // Step 3: Wait for compilation to complete
    const waitForCompilation = new sfn.Wait(this, 'Wait for Compilation', {
      time: sfn.WaitTime.duration(cdk.Duration.minutes(10)),
    });

    // Step 4: Check compilation status
    const checkCompilationTask = new tasks.LambdaInvoke(this, 'Check Compilation Status', {
      lambdaFunction: props.compilationHandler,
      payload: sfn.TaskInput.fromObject({
        pathParameters: {
          training_id: sfn.JsonPath.stringAt('$.training_id'),
        },
        httpMethod: 'GET',
        path: '/api/v1/training/$/compile',
      }),
      outputPath: '$.Payload',
    });

    // Step 5: Check if compilation is complete
    const isCompilationComplete = new sfn.Choice(this, 'Compilation Complete?')
      .when(
        sfn.Condition.stringMatches('$.body', '*COMPLETED*'),
        new sfn.Pass(this, 'Compilation Succeeded')
      )
      .otherwise(waitForCompilation);

    waitForCompilation.next(checkCompilationTask);
    checkCompilationTask.next(isCompilationComplete);

    // Step 6: Package components
    const packageComponentsTask = new tasks.LambdaInvoke(this, 'Package Components', {
      lambdaFunction: props.packagingHandler,
      payload: sfn.TaskInput.fromObject({
        pathParameters: {
          training_id: sfn.JsonPath.stringAt('$.training_id'),
        },
        body: sfn.JsonPath.stringAt('$.packaging_config'),
        httpMethod: 'POST',
        path: '/api/v1/training/$/package',
      }),
      outputPath: '$.Payload',
    });

    // Step 7: Publish to Greengrass
    const publishToGreengrassTask = new tasks.LambdaInvoke(this, 'Publish to Greengrass', {
      lambdaFunction: props.greengrassPublishHandler,
      payload: sfn.TaskInput.fromObject({
        pathParameters: {
          training_id: sfn.JsonPath.stringAt('$.training_id'),
        },
        body: sfn.JsonPath.stringAt('$.publish_config'),
        httpMethod: 'POST',
        path: '/api/v1/training/$/publish',
      }),
      outputPath: '$.Payload',
    });

    // Step 8: Send success notification
    const sendSuccessNotification = new tasks.SnsPublish(this, 'Send Success Notification', {
      topic: alertTopic,
      message: sfn.TaskInput.fromObject({
        default: 'Training pipeline completed successfully',
        training_id: sfn.JsonPath.stringAt('$.training_id'),
        component_name: sfn.JsonPath.stringAt('$.component_name'),
        component_version: sfn.JsonPath.stringAt('$.component_version'),
      }),
      subject: 'Edge CV Training Pipeline - Success',
    });

    // Step 9: Send failure notification
    const sendFailureNotification = new tasks.SnsPublish(this, 'Send Failure Notification', {
      topic: alertTopic,
      message: sfn.TaskInput.fromObject({
        default: 'Training pipeline failed',
        error: sfn.JsonPath.stringAt('$.error'),
        training_id: sfn.JsonPath.stringAt('$.training_id'),
      }),
      subject: 'Edge CV Training Pipeline - Failed',
    });

    // Define the complete workflow
    const definition = waitForTraining
      .next(startCompilationTask)
      .next(waitForCompilation)
      .next(checkCompilationTask)
      .next(isCompilationComplete)
      .next(packageComponentsTask)
      .next(publishToGreengrassTask)
      .next(sendSuccessNotification);

    // Create the state machine
    this.stateMachine = new sfn.StateMachine(this, 'TrainingWorkflow', {
      stateMachineName: 'dda-portal-training-workflow',
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      timeout: cdk.Duration.hours(6),
      tracingEnabled: true,
    });

    // Outputs
    new cdk.CfnOutput(this, 'StateMachineArn', {
      value: this.stateMachine.stateMachineArn,
      description: 'Training Workflow State Machine ARN',
    });

    new cdk.CfnOutput(this, 'AlertTopicArn', {
      value: alertTopic.topicArn,
      description: 'Training Alert SNS Topic ARN',
    });
  }
}
