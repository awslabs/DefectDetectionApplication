"""
Lambda function for monitoring Ground Truth labeling jobs.
Polls SageMaker for job status and updates DynamoDB.
Can be triggered by EventBridge schedule or manually.
"""

import json
import boto3
import os
from typing import Dict, Any
from datetime import datetime
from decimal import Decimal

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    get_usecase,
    assume_usecase_role,
    create_response,
    handle_error
)

dynamodb = boto3.resource('dynamodb')
labeling_jobs_table = dynamodb.Table(os.environ.get('LABELING_JOBS_TABLE', 'LabelingJobs'))


def handler(event, context):
    """
    Main Lambda handler for labeling job monitoring.
    
    Can be triggered by:
    1. EventBridge schedule (every 5 minutes) - monitors all InProgress jobs
    2. API Gateway - monitors specific job
    3. EventBridge rule for SageMaker events
    """
    try:
        # Check if this is an API Gateway request
        if event.get('httpMethod'):
            return handle_api_request(event)
        
        # Check if this is a SageMaker event from EventBridge
        if event.get('source') == 'aws.sagemaker':
            return handle_sagemaker_event(event)
        
        # Otherwise, this is a scheduled monitoring run
        return monitor_all_jobs()
        
    except Exception as e:
        print(f"Error in labeling monitor: {str(e)}")
        return handle_error(e, 'Labeling monitoring failed')


def handle_api_request(event):
    """Handle API Gateway request to monitor a specific job."""
    try:
        path = event.get('path', '')
        job_id = path.split('/')[-2]  # /labeling/{job_id}/status
        
        result = monitor_job(job_id)
        
        if result:
            return create_response(200, {'job': result})
        else:
            return create_response(404, {'error': 'Job not found'})
            
    except Exception as e:
        return handle_error(e, 'Failed to monitor job')


def handle_sagemaker_event(event):
    """
    Handle SageMaker labeling job state change event from EventBridge.
    
    Event structure:
    {
        "source": "aws.sagemaker",
        "detail-type": "SageMaker Labeling Job State Change",
        "detail": {
            "LabelingJobName": "dda-labeling-abc123",
            "LabelingJobStatus": "Completed",
            ...
        }
    }
    """
    try:
        detail = event.get('detail', {})
        sagemaker_job_name = detail.get('LabelingJobName')
        status = detail.get('LabelingJobStatus')
        
        if not sagemaker_job_name:
            print("No LabelingJobName in event")
            return {'statusCode': 200}
        
        # Find job in DynamoDB by sagemaker_job_name
        response = labeling_jobs_table.scan(
            FilterExpression='sagemaker_job_name = :name',
            ExpressionAttributeValues={':name': sagemaker_job_name}
        )
        
        items = response.get('Items', [])
        if not items:
            print(f"Job {sagemaker_job_name} not found in DynamoDB")
            return {'statusCode': 200}
        
        job = items[0]
        job_id = job['job_id']
        
        print(f"Updating job {job_id} status to {status}")
        
        # Update job status
        monitor_job(job_id, force_update=True)
        
        return {'statusCode': 200}
        
    except Exception as e:
        print(f"Error handling SageMaker event: {str(e)}")
        return {'statusCode': 500}


def monitor_all_jobs():
    """Monitor all InProgress labeling jobs."""
    try:
        # Scan for all InProgress jobs
        response = labeling_jobs_table.scan(
            FilterExpression='#status = :status',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={':status': 'InProgress'}
        )
        
        jobs = response.get('Items', [])
        print(f"Found {len(jobs)} InProgress jobs to monitor")
        
        updated_count = 0
        for job in jobs:
            try:
                result = monitor_job(job['job_id'])
                if result:
                    updated_count += 1
            except Exception as e:
                print(f"Error monitoring job {job['job_id']}: {str(e)}")
                continue
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'monitored': len(jobs),
                'updated': updated_count
            })
        }
        
    except Exception as e:
        print(f"Error monitoring all jobs: {str(e)}")
        return {'statusCode': 500}


def monitor_job(job_id: str, force_update: bool = False) -> Dict[str, Any]:
    """
    Monitor a specific labeling job and update DynamoDB.
    
    Args:
        job_id: The job ID
        force_update: Force update even if job is not InProgress
    
    Returns:
        Updated job dict or None if not found
    """
    try:
        # Get job from DynamoDB
        response = labeling_jobs_table.get_item(Key={'job_id': job_id})
        
        if 'Item' not in response:
            print(f"Job {job_id} not found")
            return None
        
        job = response['Item']
        
        # Skip if job is already in terminal state (unless force_update)
        if not force_update and job['status'] in ['Completed', 'Failed', 'Stopped']:
            print(f"Job {job_id} is already in terminal state: {job['status']}")
            return job
        
        # Get use case details
        usecase = get_usecase(job['usecase_id'])
        
        # Assume UseCase Account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            'monitor-labeling-job'
        )
        
        # Create SageMaker client with assumed credentials
        sagemaker = boto3.client(
            'sagemaker',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Describe labeling job
        sagemaker_job_name = job['sagemaker_job_name']
        print(f"Describing SageMaker job: {sagemaker_job_name}")
        
        job_details = sagemaker.describe_labeling_job(
            LabelingJobName=sagemaker_job_name
        )
        
        # Extract relevant information
        status = job_details['LabelingJobStatus']
        
        # Prepare update
        update_expr = 'SET #status = :status, updated_at = :updated_at'
        expr_attr_names = {'#status': 'status'}
        expr_attr_values = {
            ':status': status,
            ':updated_at': int(datetime.utcnow().timestamp())
        }
        
        # Add progress metrics if available
        if 'LabelCounters' in job_details:
            counters = job_details['LabelCounters']
            
            total = counters.get('TotalLabeled', 0) + counters.get('Unlabeled', 0)
            labeled = counters.get('TotalLabeled', 0)
            human_labeled = counters.get('HumanLabeled', 0)
            machine_labeled = counters.get('MachineLabeled', 0)
            failed = counters.get('FailedNonRetryableError', 0)
            
            update_expr += ', total_objects = :total, labeled_objects = :labeled'
            update_expr += ', human_labeled = :human, machine_labeled = :machine'
            update_expr += ', failed_objects = :failed'
            
            expr_attr_values.update({
                ':total': total,
                ':labeled': labeled,
                ':human': human_labeled,
                ':machine': machine_labeled,
                ':failed': failed
            })
            
            # Calculate progress percentage
            if total > 0:
                progress = int((labeled / total) * 100)
                update_expr += ', progress_percent = :progress'
                expr_attr_values[':progress'] = progress
        
        # Add completion time if job is complete
        if status in ['Completed', 'Failed', 'Stopped']:
            if 'LabelingJobOutput' in job_details:
                output_s3_uri = job_details['LabelingJobOutput'].get('OutputDatasetS3Uri')
                if output_s3_uri:
                    update_expr += ', output_manifest_s3_uri = :output_uri'
                    expr_attr_values[':output_uri'] = output_s3_uri
            
            if status == 'Completed':
                update_expr += ', completed_at = :completed_at'
                expr_attr_values[':completed_at'] = int(datetime.utcnow().timestamp())
            elif status == 'Failed':
                failure_reason = job_details.get('FailureReason', 'Unknown')
                update_expr += ', failure_reason = :failure_reason'
                expr_attr_values[':failure_reason'] = failure_reason
        
        # Update DynamoDB
        labeling_jobs_table.update_item(
            Key={'job_id': job_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_attr_names,
            ExpressionAttributeValues=expr_attr_values
        )
        
        print(f"Updated job {job_id} status to {status}")
        
        # Get updated job
        response = labeling_jobs_table.get_item(Key={'job_id': job_id})
        return response.get('Item')
        
    except Exception as e:
        print(f"Error monitoring job {job_id}: {str(e)}")
        raise


def convert_decimals(obj):
    """Convert Decimal objects to int/float for JSON serialization."""
    if isinstance(obj, list):
        return [convert_decimals(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    else:
        return obj
