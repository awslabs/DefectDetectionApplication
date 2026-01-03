"""
Training job management Lambda functions
Implements SageMaker training job submission and monitoring
Based on DDA_SageMaker_Model_Training_and_Compilation.ipynb
"""
import json
import os
import logging
from typing import Dict, Any, Optional
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
import uuid

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, validate_required_fields, create_s3_path_builder
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
sagemaker = boto3.client('sagemaker')
sts = boto3.client('sts')
s3 = boto3.client('s3')

# Environment variables
TRAINING_JOBS_TABLE = os.environ.get('TRAINING_JOBS_TABLE')
USECASES_TABLE = os.environ.get('USECASES_TABLE')

# AWS Marketplace algorithm configuration
# Get algorithm ARN from environment variable
# This can be updated without code changes by updating the Lambda environment variable
MARKETPLACE_ALGORITHM_ARN = os.environ.get(
    'MARKETPLACE_ALGORITHM_ARN',
    'arn:aws:sagemaker:us-east-1:865070037744:algorithm/lfv-public-algorithm-2025-02-0-422b42886fa13174a28ac6ffbf8fc874'
)

logger.info(f"Using marketplace algorithm ARN: {MARKETPLACE_ALGORITHM_ARN}")


def assume_usecase_role(role_arn: str, external_id: str, session_name: str) -> Dict:
    """Assume cross-account role for UseCase Account access"""
    try:
        response = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            ExternalId=external_id,
            DurationSeconds=3600
        )
        return response['Credentials']
    except ClientError as e:
        logger.error(f"Error assuming role {role_arn}: {str(e)}")
        raise


def get_usecase_details(usecase_id: str) -> Dict:
    """Get use case details from DynamoDB"""
    try:
        table = dynamodb.Table(USECASES_TABLE)
        response = table.get_item(Key={'usecase_id': usecase_id})
        
        if 'Item' not in response:
            raise ValueError(f"Use case {usecase_id} not found")
        
        return response['Item']
    except Exception as e:
        logger.error(f"Error getting use case details: {str(e)}")
        raise


def create_training_job(event: Dict, context: Any) -> Dict:
    """
    Create a new SageMaker training job
    POST /api/v1/training
    
    Request body:
    {
        "usecase_id": "string",
        "model_name": "string",
        "model_version": "string",
        "model_type": "classification" | "segmentation" | "classification-robust" | "segmentation-robust",
        "dataset_manifest_s3": "s3://bucket/path/manifest.json",
        "instance_type": "ml.g4dn.2xlarge",
        "max_runtime_seconds": 7200,
        "hyperparameters": {},  // optional
        "auto_compile": true,  // optional, default false
        "compilation_targets": ["x86_64", "aarch64", "jetson"]  // optional, required if auto_compile is true
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['usecase_id', 'model_name', 'model_version', 'model_type', 'dataset_manifest_s3']
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        usecase_id = body['usecase_id']
        model_name = body['model_name'].strip()
        model_version = body['model_version'].strip()
        model_type = body['model_type']
        dataset_manifest_s3 = body['dataset_manifest_s3'].strip()
        instance_type = body.get('instance_type', 'ml.g4dn.2xlarge')
        # Set default max runtime based on model type - segmentation takes longer but should still be reasonable
        default_max_runtime = 7200 if model_type in ['segmentation', 'segmentation-robust'] else 3600  # 2 hours for segmentation, 1 hour for classification
        max_runtime = body.get('max_runtime_seconds', default_max_runtime)
        hyperparameters = body.get('hyperparameters', {})
        auto_compile = body.get('auto_compile', False)
        compilation_targets = body.get('compilation_targets', [])
        
        # Validate model type
        valid_model_types = ['classification', 'segmentation', 'classification-robust', 'segmentation-robust']
        if model_type not in valid_model_types:
            return create_response(400, {
                'error': f"Invalid model_type. Must be one of: {', '.join(valid_model_types)}"
            })
        
        # Check user access (DataScientist role required)
        if not check_user_access(user_id, usecase_id, 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Get use case details
        usecase = get_usecase_details(usecase_id)
        
        # Assume cross-account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            f"training-{user_id}-{int(datetime.utcnow().timestamp())}"
        )
        
        # Create SageMaker client with assumed role
        sagemaker_usecase = boto3.client(
            'sagemaker',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Generate unique training job name
        # SageMaker job names can only contain alphanumeric and hyphens
        # SageMaker training job names must be 63 characters or less
        training_id = str(uuid.uuid4())
        safe_model_name = model_name.replace('.', '-').replace('_', '-')
        timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
        training_job_name = f"{safe_model_name}-{timestamp}"
        
        # Validate training job name length (SageMaker limit is 63 characters)
        if len(training_job_name) > 63:
            # Truncate model name to fit within limit
            max_model_name_length = 63 - len(timestamp) - 1  # -1 for the hyphen
            if max_model_name_length < 1:
                return create_response(400, {
                    'error': f"Model name '{model_name}' is too long. Please use a shorter model name (maximum {max_model_name_length + len(timestamp) + 1 - 15} characters)."
                })
            
            truncated_model_name = safe_model_name[:max_model_name_length]
            training_job_name = f"{truncated_model_name}-{timestamp}"
            logger.warning(f"Model name truncated from '{safe_model_name}' to '{truncated_model_name}' to fit SageMaker limits")
        
        logger.info(f"Generated training job name: {training_job_name} (length: {len(training_job_name)})")
        
        # Set attribute names based on model type
        if model_type in ['classification', 'classification-robust']:
            attribute_names = ['source-ref', 'anomaly-label-metadata', 'anomaly-label']
        else:  # segmentation
            attribute_names = [
                'source-ref',
                'anomaly-label-metadata',
                'anomaly-label',
                'anomaly-mask-ref-metadata',
                'anomaly-mask-ref'
            ]
        
        # Build hyperparameters
        training_hyperparameters = {
            'ModelType': model_type,
            'TrainingInputDataAttributeNames': ','.join(attribute_names),
            'TestInputDataAttributeNames': ','.join(attribute_names)
        }
        training_hyperparameters.update(hyperparameters)
        
        # Create S3 path builder for consistent path generation
        path_builder = create_s3_path_builder(
            bucket=usecase['s3_bucket'],
            prefix=usecase.get('s3_prefix', '')
        )
        
        # Generate S3 output path using new structure
        training_output_path = path_builder.get_training_output_uri(training_job_name)
        logger.info(f"Training output will be stored at: {training_output_path}")
        
        # Create training job
        logger.info(f"Creating training job: {training_job_name} with algorithm: {MARKETPLACE_ALGORITHM_ARN}")
        
        training_response = sagemaker_usecase.create_training_job(
            TrainingJobName=training_job_name,
            HyperParameters=training_hyperparameters,
            AlgorithmSpecification={
                'AlgorithmName': MARKETPLACE_ALGORITHM_ARN,
                'TrainingInputMode': 'File',
                'EnableSageMakerMetricsTimeSeries': False
            },
            RoleArn=usecase['sagemaker_execution_role_arn'],
            InputDataConfig=[
                {
                    'ChannelName': 'training',
                    'DataSource': {
                        'S3DataSource': {
                            'S3DataType': 'AugmentedManifestFile',
                            'S3Uri': dataset_manifest_s3,
                            'S3DataDistributionType': 'ShardedByS3Key',
                            'AttributeNames': attribute_names
                        }
                    },
                    'CompressionType': 'None',
                    'RecordWrapperType': 'RecordIO',
                    'InputMode': 'Pipe'
                }
            ],
            OutputDataConfig={
                'S3OutputPath': training_output_path
            },
            ResourceConfig={
                'InstanceType': instance_type,
                'InstanceCount': 1,
                'VolumeSizeInGB': 20
            },
            StoppingCondition={
                'MaxRuntimeInSeconds': max_runtime
            },
            EnableNetworkIsolation=True,
            Tags=[
                {'Key': 'UseCase', 'Value': usecase_id},
                {'Key': 'ModelName', 'Value': model_name},
                {'Key': 'ModelVersion', 'Value': model_version},
                {'Key': 'CreatedBy', 'Value': user_id}
            ]
        )
        
        training_job_arn = training_response['TrainingJobArn']
        
        # Store training job metadata in DynamoDB
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        training_item = {
            'training_id': training_id,
            'usecase_id': usecase_id,
            'model_name': model_name,
            'model_version': model_version,
            'model_type': model_type,
            'dataset_manifest_s3': dataset_manifest_s3,
            'algorithm_uri': MARKETPLACE_ALGORITHM_ARN,
            'hyperparameters': training_hyperparameters,
            'instance_type': instance_type,
            'training_job_name': training_job_name,
            'training_job_arn': training_job_arn,
            'status': 'InProgress',
            'progress': 10,  # Initial progress when job is created
            'created_by': user['email'],
            'created_at': timestamp,
            'updated_at': timestamp,
            'auto_compile': auto_compile,
            'compilation_targets': compilation_targets
        }
        
        table.put_item(Item=training_item)
        
        # Log audit event
        log_audit_event(
            user_id=user_id,
            action='create_training_job',
            resource_type='training_job',
            resource_id=training_id,
            result='success',
            details={
                'model_name': model_name,
                'model_version': model_version,
                'model_type': model_type,
                'training_job_name': training_job_name
            }
        )
        
        logger.info(f"Training job created successfully: {training_id}")
        
        return create_response(201, {
            'training_id': training_id,
            'training_job_name': training_job_name,
            'training_job_arn': training_job_arn,
            'status': 'InProgress',
            'message': 'Training job created successfully'
        })
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return create_response(400, {'error': str(e)})
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_message = e.response.get('Error', {}).get('Message', str(e))
        
        logger.error(f"AWS error creating training job: {error_code} - {error_message}")
        
        # Provide user-friendly error messages for common SageMaker validation errors
        if 'ValidationException' in error_code:
            if 'Member must have length less than' in error_message:
                if 'TrainingJobName' in error_message:
                    return create_response(400, {
                        'error': f"Training job name '{training_job_name}' is too long. Please use a shorter model name (maximum 40 characters recommended)."
                    })
                else:
                    # Replace generic "Member" with more specific field names
                    user_friendly_message = error_message.replace('Member must have length less than', 'Field must have length less than')
                    return create_response(400, {'error': user_friendly_message})
            elif 'length greater than' in error_message.lower():
                return create_response(400, {'error': error_message.replace('Member', 'Field')})
            else:
                return create_response(400, {'error': f"Validation error: {error_message}"})
        elif 'AccessDenied' in error_code:
            return create_response(403, {'error': 'Access denied. Please check your permissions for this use case.'})
        elif 'ResourceLimitExceeded' in error_code:
            return create_response(429, {'error': 'Resource limit exceeded. Please try again later or contact support.'})
        else:
            return create_response(500, {'error': f"Failed to create training job: {error_message}"})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def list_training_jobs(event: Dict, context: Any) -> Dict:
    """
    List training jobs for a use case
    GET /api/v1/training?usecase_id=xxx
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get query parameters
        params = event.get('queryStringParameters', {}) or {}
        usecase_id = params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Check user access
        if not check_user_access(user_id, usecase_id):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Query DynamoDB
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        response = table.query(
            IndexName='usecase-training-index',
            KeyConditionExpression='usecase_id = :usecase_id',
            ExpressionAttributeValues={':usecase_id': usecase_id},
            ScanIndexForward=False  # Sort by created_at descending
        )
        
        jobs = response.get('Items', [])
        
        return create_response(200, {
            'jobs': jobs,
            'count': len(jobs)
        })
        
    except Exception as e:
        logger.error(f"Error listing training jobs: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def get_training_job(event: Dict, context: Any) -> Dict:
    """
    Get training job details and sync status from SageMaker
    GET /api/v1/training/{training_id}
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get path parameters
        training_id = event.get('pathParameters', {}).get('id')
        
        if not training_id:
            return create_response(400, {'error': 'training_id is required'})
        
        # Get training job from DynamoDB
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        response = table.get_item(Key={'training_id': training_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Training job not found'})
        
        job = response['Item']
        
        # Check user access
        if not check_user_access(user_id, job['usecase_id']):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Ensure progress field exists for backward compatibility
        if 'progress' not in job:
            # Set progress based on current status
            if job.get('status') == 'Completed':
                job['progress'] = 100
            elif job.get('status') == 'InProgress':
                job['progress'] = 50
            elif job.get('status') in ['Failed', 'Stopped']:
                job['progress'] = 0
            else:
                job['progress'] = 0
        
        # If job is in progress, sync status from SageMaker
        if job.get('status') == 'InProgress':
            try:
                usecase = get_usecase_details(job['usecase_id'])
                credentials = assume_usecase_role(
                    usecase['cross_account_role_arn'],
                    usecase['external_id'],
                    f"training-status-{user_id}-{int(datetime.utcnow().timestamp())}"
                )
                
                sagemaker_usecase = boto3.client(
                    'sagemaker',
                    aws_access_key_id=credentials['AccessKeyId'],
                    aws_secret_access_key=credentials['SecretAccessKey'],
                    aws_session_token=credentials['SessionToken']
                )
                
                sm_response = sagemaker_usecase.describe_training_job(
                    TrainingJobName=job['training_job_name']
                )
                
                status = sm_response['TrainingJobStatus']
                timestamp = int(datetime.utcnow().timestamp() * 1000)
                
                # Update DynamoDB if status changed
                if status != job.get('status'):
                    # Calculate progress based on status
                    progress = 0
                    if status == 'InProgress':
                        progress = 50  # Assume 50% when in progress (no detailed progress from SageMaker)
                    elif status == 'Completed':
                        progress = 100
                    elif status in ['Failed', 'Stopped']:
                        progress = 0  # Reset to 0 for failed/stopped jobs
                    
                    update_expr = 'SET #status = :status, updated_at = :updated, progress = :progress'
                    expr_values = {
                        ':status': status,
                        ':updated': timestamp,
                        ':progress': progress
                    }
                    expr_names = {'#status': 'status'}
                    
                    if status == 'Completed':
                        update_expr += ', artifact_s3 = :artifact, completed_at = :completed'
                        expr_values[':artifact'] = sm_response['ModelArtifacts']['S3ModelArtifacts']
                        expr_values[':completed'] = timestamp
                    elif status == 'Failed':
                        update_expr += ', failure_reason = :reason'
                        expr_values[':reason'] = sm_response.get('FailureReason', 'Unknown')
                    
                    table.update_item(
                        Key={'training_id': training_id},
                        UpdateExpression=update_expr,
                        ExpressionAttributeValues=expr_values,
                        ExpressionAttributeNames=expr_names
                    )
                    
                    job['status'] = status
                    job['updated_at'] = timestamp
                    job['progress'] = progress
                    if status == 'Completed':
                        job['artifact_s3'] = sm_response['ModelArtifacts']['S3ModelArtifacts']
                        job['completed_at'] = timestamp
                    elif status == 'Failed':
                        job['failure_reason'] = sm_response.get('FailureReason', 'Unknown')
                
            except Exception as e:
                logger.error(f"Error syncing training status: {str(e)}")
                # Continue with cached status from DynamoDB
        
        return create_response(200, job)
        
    except Exception as e:
        logger.error(f"Error getting training job: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def get_training_logs(event: Dict, context: Any) -> Dict:
    """
    Get training job CloudWatch logs
    GET /api/v1/training/{training_id}/logs?limit=100&nextToken=xxx
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get path parameters
        training_id = event.get('pathParameters', {}).get('id')
        if not training_id:
            return create_response(400, {'error': 'training_id is required'})
        
        # Get query parameters
        params = event.get('queryStringParameters', {}) or {}
        limit = int(params.get('limit', 100))
        next_forward_token = params.get('nextForwardToken')
        next_backward_token = params.get('nextBackwardToken')
        
        # Get training job from DynamoDB
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        response = table.get_item(Key={'training_id': training_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Training job not found'})
        
        job = response['Item']
        
        # Check user access
        if not check_user_access(user_id, job['usecase_id']):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Get use case details for cross-account access
        usecase = get_usecase_details(job['usecase_id'])
        
        # Assume cross-account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            f"training-logs-{user_id}-{int(datetime.utcnow().timestamp())}"
        )
        
        # Create CloudWatch Logs client with assumed role
        logs_client = boto3.client(
            'logs',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # CloudWatch log group for SageMaker training jobs
        log_group = '/aws/sagemaker/TrainingJobs'
        
        try:
            # Find the log stream for this training job
            # SageMaker creates log streams with format: {job_name}/algo-1-{timestamp}
            try:
                streams_response = logs_client.describe_log_streams(
                    logGroupName=log_group,
                    logStreamNamePrefix=job['training_job_name'],
                    orderBy='LogStreamName',
                    descending=True,
                    limit=1
                )
            except logs_client.exceptions.ResourceNotFoundException:
                # Log group doesn't exist yet
                return create_response(200, {
                    'training_id': training_id,
                    'training_job_name': job['training_job_name'],
                    'logs': [],
                    'message': 'No logs available yet. Training may not have started.'
                })
            
            if not streams_response.get('logStreams'):
                # No log stream found yet
                return create_response(200, {
                    'training_id': training_id,
                    'training_job_name': job['training_job_name'],
                    'logs': [],
                    'message': 'No logs available yet. Training may not have started.'
                })
            
            log_stream = streams_response['logStreams'][0]['logStreamName']
            
            # Get log events
            # CloudWatch Logs pagination:
            # - startFromHead=True: reads from beginning, use nextForwardToken to continue
            # - startFromHead=False: reads from end, use nextBackwardToken to go back
            log_params = {
                'logGroupName': log_group,
                'logStreamName': log_stream,
                'limit': min(limit, 10000),  # CloudWatch max is 10000
            }
            
            # Determine pagination direction and token
            if next_forward_token:
                # Continue reading forward (chronologically)
                log_params['nextToken'] = next_forward_token
                log_params['startFromHead'] = True
            elif next_backward_token:
                # Continue reading backward (reverse chronologically)
                log_params['nextToken'] = next_backward_token
                log_params['startFromHead'] = False
            else:
                # Initial request - start from the beginning
                log_params['startFromHead'] = True
            
            log_response = logs_client.get_log_events(**log_params)
            
            # Format log events
            log_events = []
            for event in log_response.get('events', []):
                log_events.append({
                    'timestamp': event['timestamp'],
                    'message': event['message'],
                    'ingestionTime': event.get('ingestionTime')
                })
            
            return create_response(200, {
                'training_id': training_id,
                'training_job_name': job['training_job_name'],
                'logs': log_events,
                'nextForwardToken': log_response.get('nextForwardToken'),
                'nextBackwardToken': log_response.get('nextBackwardToken')
            })
            
        except logs_client.exceptions.ResourceNotFoundException:
            # Log stream doesn't exist yet or training hasn't started
            return create_response(200, {
                'training_id': training_id,
                'training_job_name': job['training_job_name'],
                'logs': [],
                'message': 'No logs available yet. Training may not have started.'
            })
        
    except Exception as e:
        logger.error(f"Error getting training logs: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def download_training_logs(event: Dict, context: Any) -> Dict:
    """
    Download all training job CloudWatch logs as a text file
    GET /api/v1/training/{training_id}/logs/download
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get path parameters
        training_id = event.get('pathParameters', {}).get('id')
        if not training_id:
            return create_response(400, {'error': 'training_id is required'})
        
        # Get training job from DynamoDB
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        response = table.get_item(Key={'training_id': training_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Training job not found'})
        
        job = response['Item']
        
        # Check user access
        if not check_user_access(user_id, job['usecase_id']):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Get use case details for cross-account access
        usecase = get_usecase_details(job['usecase_id'])
        
        # Assume cross-account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            f"training-logs-download-{user_id}-{int(datetime.utcnow().timestamp())}"
        )
        
        # Create CloudWatch Logs client with assumed role
        logs_client = boto3.client(
            'logs',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # CloudWatch log group for SageMaker training jobs
        log_group = '/aws/sagemaker/TrainingJobs'
        
        try:
            # Find the log stream for this training job
            try:
                streams_response = logs_client.describe_log_streams(
                    logGroupName=log_group,
                    logStreamNamePrefix=job['training_job_name'],
                    orderBy='LogStreamName',
                    descending=True,
                    limit=1
                )
            except logs_client.exceptions.ResourceNotFoundException:
                return create_response(404, {
                    'error': 'No logs available yet. Training may not have started.'
                })
            
            if not streams_response.get('logStreams'):
                return create_response(404, {
                    'error': 'No logs available yet. Training may not have started.'
                })
            
            log_stream = streams_response['logStreams'][0]['logStreamName']
            
            # Fetch all log events by paginating through the stream
            all_logs = []
            next_token = None
            
            while True:
                log_params = {
                    'logGroupName': log_group,
                    'logStreamName': log_stream,
                    'startFromHead': True,
                    'limit': 10000  # Max allowed by CloudWatch
                }
                
                if next_token:
                    log_params['nextToken'] = next_token
                
                log_response = logs_client.get_log_events(**log_params)
                events = log_response.get('events', [])
                
                if not events:
                    break
                
                all_logs.extend(events)
                
                # Check if there are more logs
                new_token = log_response.get('nextForwardToken')
                if new_token == next_token:
                    # No more logs available
                    break
                next_token = new_token
            
            # Format logs as text
            log_text_lines = []
            log_text_lines.append(f"Training Job: {job['training_job_name']}")
            log_text_lines.append(f"Training ID: {training_id}")
            log_text_lines.append(f"Model: {job.get('model_name', 'N/A')} v{job.get('model_version', 'N/A')}")
            log_text_lines.append(f"Downloaded: {datetime.utcnow().isoformat()}Z")
            log_text_lines.append(f"Total Log Events: {len(all_logs)}")
            log_text_lines.append("=" * 80)
            log_text_lines.append("")
            
            for event in all_logs:
                timestamp = datetime.fromtimestamp(event['timestamp'] / 1000).isoformat()
                log_text_lines.append(f"[{timestamp}] {event['message']}")
            
            log_text = '\n'.join(log_text_lines)
            
            # Return as downloadable text file
            return {
                'statusCode': 200,
                'headers': {
                    'Content-Type': 'text/plain',
                    'Content-Disposition': f'attachment; filename="training-logs-{job["training_job_name"]}.txt"',
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': log_text
            }
            
        except logs_client.exceptions.ResourceNotFoundException:
            return create_response(404, {
                'error': 'No logs available yet. Training may not have started.'
            })
        
    except Exception as e:
        logger.error(f"Error downloading training logs: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler - routes to appropriate function"""
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        resource = event.get('resource', '')
        
        logger.info(f"Handler invoked: {http_method} {path} (resource: {resource})")
        
        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }
        
        # Route to appropriate handler
        # Note: path includes stage (/v1/training), resource is the pattern (/training)
        if http_method == 'POST' and '/training' in path and '{id}' not in resource:
            return create_training_job(event, context)
        elif http_method == 'GET' and '/training' in path and '{id}' not in resource:
            return list_training_jobs(event, context)
        elif http_method == 'GET' and '/logs/download' in path:
            return download_training_logs(event, context)
        elif http_method == 'GET' and '/logs' in path:
            return get_training_logs(event, context)
        elif http_method == 'GET' and '/training/' in path and '{id}' in resource:
            return get_training_job(event, context)
        else:
            return create_response(404, {'error': 'Not found'})
            
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})
