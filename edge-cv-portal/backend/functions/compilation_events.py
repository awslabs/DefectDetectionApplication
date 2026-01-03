"""
Compilation job event handler for EventBridge
Handles SageMaker compilation job state change events and triggers Greengrass component creation
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


def handle_compilation_state_change(event: Dict, context: Any) -> Dict:
    """
    Handle SageMaker Compilation Job State Change events from EventBridge
    
    Event pattern:
    {
      "source": ["aws.sagemaker"],
      "detail-type": ["SageMaker Compilation Job State Change"],
      "detail": {
        "CompilationJobStatus": ["Completed", "Failed", "Stopped"]
      }
    }
    """
    try:
        logger.info(f"Received compilation state change event: {json.dumps(event)}")
        
        # Extract event details
        detail = event.get('detail', {})
        compilation_job_name = detail.get('CompilationJobName')
        compilation_job_status = detail.get('CompilationJobStatus')
        compilation_job_arn = detail.get('CompilationJobArn')
        
        if not compilation_job_name:
            logger.error("No CompilationJobName in event")
            return {'statusCode': 400, 'body': 'Missing CompilationJobName'}
        
        logger.info(f"Compilation job {compilation_job_name} status: {compilation_job_status}")
        
        # Find training job in DynamoDB by scanning for compilation job name
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        
        # Scan all training jobs and filter in Python
        # DynamoDB's contains() doesn't work for searching within nested List of Maps
        training_job = None
        paginator_token = None
        
        while True:
            scan_params = {}
            if paginator_token:
                scan_params['ExclusiveStartKey'] = paginator_token
            
            response = table.scan(**scan_params)
            
            # Search through items for matching compilation job name
            for item in response.get('Items', []):
                compilation_jobs = item.get('compilation_jobs', [])
                for job in compilation_jobs:
                    if job.get('compilation_job_name') == compilation_job_name:
                        training_job = item
                        break
                if training_job:
                    break
            
            if training_job:
                break
            
            # Check for more pages
            paginator_token = response.get('LastEvaluatedKey')
            if not paginator_token:
                break
        
        if not training_job:
            logger.warning(f"Training job with compilation job {compilation_job_name} not found in DynamoDB")
            return {'statusCode': 404, 'body': 'Training job not found'}
        
        training_id = training_job['training_id']
        logger.info(f"Found training job {training_id} for compilation job {compilation_job_name}")
        
        # Update compilation job status in the training job record
        compilation_jobs = training_job.get('compilation_jobs', [])
        updated_jobs = []
        
        for job in compilation_jobs:
            if job.get('compilation_job_name') == compilation_job_name:
                # Normalize status to uppercase to match SageMaker API format
                # EventBridge sends 'Completed' but SageMaker API returns 'COMPLETED'
                job['status'] = compilation_job_status.upper() if compilation_job_status else compilation_job_status
                
                if compilation_job_status.upper() == 'COMPLETED':
                    # Add compiled model S3 URI from event
                    model_artifacts = detail.get('ModelArtifacts', {})
                    s3_model_artifacts = model_artifacts.get('S3ModelArtifacts')
                    if s3_model_artifacts:
                        job['compiled_model_s3'] = s3_model_artifacts
                elif compilation_job_status.upper() == 'FAILED':
                    failure_reason = detail.get('FailureReason', 'Unknown')
                    job['failure_reason'] = failure_reason
            
            updated_jobs.append(job)
        
        # Update DynamoDB with new compilation job status
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        table.update_item(
            Key={'training_id': training_id},
            UpdateExpression='SET compilation_jobs = :jobs, updated_at = :updated',
            ExpressionAttributeValues={
                ':jobs': updated_jobs,
                ':updated': timestamp
            }
        )
        
        logger.info(f"Updated compilation job {compilation_job_name} status to {compilation_job_status}")
        
        # Trigger packaging for completed compilation jobs
        if compilation_job_status.upper() == 'COMPLETED':
            try:
                # Check if all compilation jobs are completed before triggering packaging
                # Status is normalized to uppercase, so only check for 'COMPLETED'
                all_completed = all(
                    job.get('status', '').upper() == 'COMPLETED'
                    for job in updated_jobs
                )
                
                if all_completed:
                    logger.info(f"All compilation jobs completed for training {training_id}, triggering packaging")
                    
                    # Trigger packaging by invoking the packaging Lambda
                    lambda_client = boto3.client('lambda')
                    packaging_function_name = os.environ.get('PACKAGING_FUNCTION_NAME')
                    
                    if packaging_function_name:
                        # Create a mock API Gateway event for the packaging handler
                        packaging_event = {
                            'httpMethod': 'POST',
                            'path': f'/api/v1/training/{training_id}/package',
                            'pathParameters': {'id': training_id},
                            'body': json.dumps({
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
                        
                        # Invoke packaging Lambda asynchronously
                        lambda_client.invoke(
                            FunctionName=packaging_function_name,
                            InvocationType='Event',  # Async invocation
                            Payload=json.dumps(packaging_event)
                        )
                        
                        logger.info(f"Triggered automatic packaging for training job {training_id}")
                    else:
                        logger.warning("PACKAGING_FUNCTION_NAME not set, skipping automatic packaging")
                else:
                    logger.info(f"Compilation job {compilation_job_name} completed, but waiting for other compilation jobs to complete")
                    
            except Exception as e:
                logger.error(f"Error triggering automatic packaging: {str(e)}")
                # Don't fail the whole event processing if packaging trigger fails
        
        # Send SNS notification for failures
        if compilation_job_status and compilation_job_status.upper() == 'FAILED' and ALERT_TOPIC_ARN:
            try:
                failure_reason = detail.get('FailureReason', 'Unknown')
                
                message = {
                    'alert_type': 'compilation_job_failed',
                    'training_id': training_id,
                    'compilation_job_name': compilation_job_name,
                    'compilation_job_arn': compilation_job_arn,
                    'failure_reason': failure_reason,
                    'usecase_id': training_job.get('usecase_id'),
                    'model_name': training_job.get('model_name'),
                    'timestamp': timestamp
                }
                
                sns.publish(
                    TopicArn=ALERT_TOPIC_ARN,
                    Subject=f'Compilation Job Failed: {compilation_job_name}',
                    Message=json.dumps(message, indent=2)
                )
                
                logger.info(f"Sent failure alert for compilation job {compilation_job_name}")
                
            except Exception as e:
                logger.error(f"Error sending SNS notification: {str(e)}")
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'training_id': training_id,
                'compilation_job_name': compilation_job_name,
                'status': compilation_job_status.upper() if compilation_job_status else compilation_job_status,
                'message': 'Compilation job status updated',
                'packaging_triggered': compilation_job_status.upper() == 'COMPLETED' if compilation_job_status else False
            })
        }
        
    except Exception as e:
        logger.error(f"Error handling compilation state change: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler for EventBridge events"""
    return handle_compilation_state_change(event, context)