"""
Training job event handler for EventBridge
Handles SageMaker training job state change events
"""
import json
import os
import logging
from typing import Dict, Any
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
sns = boto3.client('sns')

# Environment variables
TRAINING_JOBS_TABLE = os.environ.get('TRAINING_JOBS_TABLE')
ALERT_TOPIC_ARN = os.environ.get('ALERT_TOPIC_ARN', '')


def handle_training_state_change(event: Dict, context: Any) -> Dict:
    """
    Handle SageMaker Training Job State Change events from EventBridge
    
    Event pattern:
    {
      "source": ["aws.sagemaker"],
      "detail-type": ["SageMaker Training Job State Change"],
      "detail": {
        "TrainingJobStatus": ["Completed", "Failed", "Stopped"]
      }
    }
    """
    try:
        logger.info(f"Received training state change event: {json.dumps(event)}")
        
        # Extract event details
        detail = event.get('detail', {})
        training_job_name = detail.get('TrainingJobName')
        training_job_status = detail.get('TrainingJobStatus')
        training_job_arn = detail.get('TrainingJobArn')
        
        if not training_job_name:
            logger.error("No TrainingJobName in event")
            return {'statusCode': 400, 'body': 'Missing TrainingJobName'}
        
        logger.info(f"Training job {training_job_name} status: {training_job_status}")
        
        # Find training job in DynamoDB by training_job_name
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        
        # Query by training_job_name (need to scan since it's not the primary key)
        response = table.scan(
            FilterExpression='training_job_name = :name',
            ExpressionAttributeValues={':name': training_job_name}
        )
        
        items = response.get('Items', [])
        if not items:
            logger.warning(f"Training job {training_job_name} not found in DynamoDB")
            return {'statusCode': 404, 'body': 'Training job not found'}
        
        training_job = items[0]
        training_id = training_job['training_id']
        
        # Update training job status in DynamoDB
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        # Set progress based on training job status
        progress = 10  # Default for InProgress
        if training_job_status == 'Completed':
            progress = 100
        elif training_job_status == 'Failed' or training_job_status == 'Stopped':
            progress = 0  # Reset progress for failed/stopped jobs
        elif training_job_status == 'InProgress':
            progress = 50  # Mid-progress for in-progress jobs
        
        update_expr = 'SET #status = :status, progress = :progress, updated_at = :updated'
        expr_values = {
            ':status': training_job_status,
            ':progress': progress,
            ':updated': timestamp
        }
        expr_names = {'#status': 'status'}
        
        # Add completion timestamp and artifact for completed jobs
        if training_job_status == 'Completed':
            model_artifacts = detail.get('ModelArtifacts', {})
            s3_model_artifacts = model_artifacts.get('S3ModelArtifacts')
            
            if s3_model_artifacts:
                update_expr += ', artifact_s3 = :artifact, completed_at = :completed'
                expr_values[':artifact'] = s3_model_artifacts
                expr_values[':completed'] = timestamp
        
        # Add failure reason for failed jobs
        elif training_job_status == 'Failed':
            failure_reason = detail.get('FailureReason', 'Unknown')
            update_expr += ', failure_reason = :reason'
            expr_values[':reason'] = failure_reason
        
        # Update DynamoDB
        table.update_item(
            Key={'training_id': training_id},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
            ExpressionAttributeNames=expr_names
        )
        
        logger.info(f"Updated training job {training_id} status to {training_job_status} with progress {progress}%")
        
        # Trigger automatic compilation for completed training jobs
        if training_job_status == 'Completed':
            try:
                # Check if auto-compilation is enabled for this training job
                auto_compile = training_job.get('auto_compile', False)
                compilation_targets = training_job.get('compilation_targets', [])
                
                if not auto_compile:
                    logger.info(f"Auto-compilation disabled for training job {training_id}, skipping")
                elif not compilation_targets:
                    logger.warning(f"No compilation targets specified for training job {training_id}, skipping auto-compilation")
                else:
                    logger.info(f"Training job {training_id} completed successfully, triggering compilation for targets: {compilation_targets}")
                    
                    # Trigger compilation by invoking the compilation Lambda
                    lambda_client = boto3.client('lambda')
                    compilation_function_name = os.environ.get('COMPILATION_FUNCTION_NAME')
                    
                    if compilation_function_name:
                        # Map frontend target names to backend target names
                        target_mapping = {
                            'x86_64': 'x86_64-cpu',
                            'aarch64': 'arm64-cpu', 
                            'jetson': 'jetson-xavier'
                        }
                        
                        # Convert frontend targets to backend targets
                        backend_targets = []
                        for target in compilation_targets:
                            backend_target = target_mapping.get(target, target)
                            backend_targets.append(backend_target)
                        
                        # Create a mock API Gateway event for the compilation handler
                        compilation_event = {
                            'httpMethod': 'POST',
                            'path': f'/api/v1/training/{training_id}/compile',
                            'pathParameters': {'id': training_id},
                            'body': json.dumps({
                                'targets': backend_targets,
                                'auto_triggered': True
                            }),
                            'requestContext': {
                                'authorizer': {
                                    'claims': {
                                        'sub': 'system',
                                        'email': 'system@edgecv.com',
                                        'cognito:username': 'system'
                                    }
                                }
                            }
                        }
                        
                        # Invoke compilation Lambda asynchronously
                        lambda_client.invoke(
                            FunctionName=compilation_function_name,
                            InvocationType='Event',  # Async invocation
                            Payload=json.dumps(compilation_event)
                        )
                        
                        logger.info(f"Triggered automatic compilation for training job {training_id} with targets: {backend_targets}")
                    else:
                        logger.warning("COMPILATION_FUNCTION_NAME not set, skipping automatic compilation")
                    
            except Exception as e:
                logger.error(f"Error triggering automatic compilation: {str(e)}")
                # Don't fail the whole event processing if compilation trigger fails
        
        # Send SNS notification for failures
        if training_job_status == 'Failed' and ALERT_TOPIC_ARN:
            try:
                failure_reason = detail.get('FailureReason', 'Unknown')
                
                message = {
                    'alert_type': 'training_job_failed',
                    'training_id': training_id,
                    'training_job_name': training_job_name,
                    'training_job_arn': training_job_arn,
                    'failure_reason': failure_reason,
                    'usecase_id': training_job.get('usecase_id'),
                    'model_name': training_job.get('model_name'),
                    'timestamp': timestamp
                }
                
                sns.publish(
                    TopicArn=ALERT_TOPIC_ARN,
                    Subject=f'Training Job Failed: {training_job_name}',
                    Message=json.dumps(message, indent=2)
                )
                
                logger.info(f"Sent failure alert for training job {training_id}")
                
            except Exception as e:
                logger.error(f"Error sending SNS notification: {str(e)}")
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'training_id': training_id,
                'status': training_job_status,
                'message': 'Training job status updated',
                'compilation_triggered': training_job_status == 'Completed'
            })
        }
        
    except Exception as e:
        logger.error(f"Error handling training state change: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler for EventBridge events"""
    return handle_training_state_change(event, context)
