"""
Lambda function for Ground Truth labeling job management.
Handles job creation, monitoring, and manifest generation.
"""

import json
import boto3
from botocore.exceptions import ClientError
import os
import uuid
import logging
from typing import Dict, List, Any
from datetime import datetime

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

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
    """Main Lambda handler for labeling operations."""
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        resource = event.get('resource', '')
        
        print(f"Handler invoked: {http_method} {path} (resource: {resource})")
        
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
        
        # Note: path includes stage (/v1/labeling), resource is the pattern (/labeling)
        if http_method == 'GET' and '/workteams' in path:
            return list_workteams(event)
        elif http_method == 'POST' and '/transform-manifest' in path:
            return transform_manifest(event)
        elif http_method == 'GET' and '/labeling' in path and '{id}' not in resource:
            return list_labeling_jobs(event)
        elif http_method == 'POST' and '/labeling' in path and '{id}' not in resource:
            return create_labeling_job(event)
        elif http_method == 'GET' and '/labeling/' in path and '{id}' in resource:
            # Extract job_id from path parameters
            job_id = event.get('pathParameters', {}).get('id', '')
            if 'manifest' in path:
                return get_manifest(job_id)
            return get_labeling_job(job_id)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        return handle_error(e, 'Labeling operation failed')


def list_labeling_jobs(event):
    """
    List labeling jobs for a use case.
    
    Query Parameters:
        - usecase_id: Required. Filter by use case
        - status: Optional. Filter by status (InProgress, Completed, Failed, Stopped)
    """
    try:
        params = event.get('queryStringParameters', {}) or {}
        usecase_id = params.get('usecase_id')
        status_filter = params.get('status')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Query DynamoDB for jobs
        response = labeling_jobs_table.query(
            IndexName='usecase-jobs-index',
            KeyConditionExpression='usecase_id = :usecase_id',
            ExpressionAttributeValues={':usecase_id': usecase_id}
        )
        
        jobs = response.get('Items', [])
        
        # Filter by status if provided
        if status_filter:
            jobs = [j for j in jobs if j.get('status') == status_filter]
        
        # Add output_manifest_s3_uri for completed jobs
        for job in jobs:
            if job.get('status') == 'Completed' and job.get('output_s3_uri'):
                # Ground Truth creates output in: {output_s3_uri}/{sagemaker_job_name}/manifests/output/output.manifest
                sagemaker_job_name = job.get('sagemaker_job_name', '')
                output_s3_uri = job['output_s3_uri']
                if not output_s3_uri.endswith('/'):
                    output_s3_uri += '/'
                
                # Construct the actual manifest path with SageMaker job name
                if sagemaker_job_name:
                    source_manifest_uri = f"{output_s3_uri}{sagemaker_job_name}/manifests/output/output.manifest"
                else:
                    # Fallback to old path if sagemaker_job_name is missing
                    source_manifest_uri = f"{output_s3_uri}manifests/output/output.manifest"
                
                # Prefer transformed manifest if available
                if job.get('is_transformed') and job.get('transformed_manifest_s3_uri'):
                    job['output_manifest_s3_uri'] = job['transformed_manifest_s3_uri']
                else:
                    job['output_manifest_s3_uri'] = source_manifest_uri
        
        # Sort by created_at descending
        jobs.sort(key=lambda x: x.get('created_at', 0), reverse=True)
        
        return create_response(200, {
            'jobs': jobs,
            'count': len(jobs)
        })
        
    except Exception as e:
        return handle_error(e, 'Failed to list labeling jobs')


def create_labeling_job(event):
    """
    Create a new Ground Truth labeling job.
    
    Request Body:
        - usecase_id: Required
        - job_name: Required
        - dataset_prefix: Required. S3 prefix containing images
        - task_type: Required. ObjectDetection, Classification, or Segmentation
        - label_categories: Required. List of label names
        - workforce_arn: Required. WorkTeam ARN
        - instructions: Optional. Labeling instructions
        - num_workers_per_object: Optional. Default 1
        - task_time_limit: Optional. Default 600 seconds
    """
    try:
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['usecase_id', 'job_name', 'dataset_prefix', 'task_type', 
                          'label_categories', 'workforce_arn']
        for field in required_fields:
            if not body.get(field):
                return create_response(400, {'error': f'{field} is required'})
        
        # Validate label category order for anomaly detection
        # First category should be "normal" (index 0), anomalies should be index 1+
        label_categories = body['label_categories']
        if label_categories and len(label_categories) > 0:
            first_label = label_categories[0].lower()
            # Check if first label looks like an anomaly/defect
            anomaly_keywords = ['defect', 'anomaly', 'scratch', 'dent', 'crack', 'damage', 'fault', 'error']
            if any(keyword in first_label for keyword in anomaly_keywords):
                logger.warning(f"Label order may be incorrect. First label '{label_categories[0]}' appears to be an anomaly type. "
                             f"For DDA models, the first label (index 0) should be 'normal' or 'good', and anomalies should be index 1+.")
                # Return a warning but allow the job to proceed
                # Users may have valid reasons for this order
        
        usecase_id = body['usecase_id']
        job_name = body['job_name']
        dataset_prefix = body['dataset_prefix']
        task_type = body['task_type']
        label_categories = body['label_categories']
        workforce_arn = body['workforce_arn']
        instructions = body.get('instructions', '')
        num_workers = body.get('num_workers_per_object', 1)
        task_time_limit = body.get('task_time_limit', 600)
        
        # Get use case details
        usecase = get_usecase(usecase_id)
        
        # Assume UseCase Account role for SageMaker and output
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            'create-labeling-job'
        )
        
        # Create clients with UseCase Account credentials
        s3 = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        sagemaker = boto3.client(
            'sagemaker',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Determine input data source (Data Account or UseCase Account)
        # Input images come from Data Account if configured and different from UseCase Account
        data_role_arn = usecase.get('data_account_role_arn')
        data_account_id = usecase.get('data_account_id')
        usecase_account_id = usecase.get('account_id')
        
        # Check if Data Account is different from UseCase Account
        is_separate_data_account = (
            data_role_arn and 
            data_account_id and 
            data_account_id != usecase_account_id
        )
        
        if is_separate_data_account:
            # Use separate Data Account for input data - external ID is required for production
            print(f"Using separate Data Account {data_account_id} for input data")
            data_external_id = usecase.get('data_account_external_id')
            if not data_external_id:
                return create_response(400, {
                    'error': 'data_account_external_id is required when using a separate Data Account. '
                             'Please update the UseCase configuration with the external ID.'
                })
            data_credentials = assume_usecase_role(
                data_role_arn,
                data_external_id,
                'labeling-data-access'
            )
            input_bucket = usecase.get('data_s3_bucket') or usecase.get('s3_bucket')
            input_prefix = usecase.get('data_s3_prefix', '')
            
            # S3 client for reading input data from Data Account
            s3_data = boto3.client(
                's3',
                aws_access_key_id=data_credentials['AccessKeyId'],
                aws_secret_access_key=data_credentials['SecretAccessKey'],
                aws_session_token=data_credentials['SessionToken']
            )
            s3_for_input = s3_data
        else:
            # Data Account is same as UseCase Account - use same credentials
            print(f"Data Account is same as UseCase Account {usecase_account_id}")
            input_bucket = usecase.get('data_s3_bucket') or usecase['s3_bucket']
            input_prefix = usecase.get('data_s3_prefix', usecase.get('s3_prefix', ''))
            s3_for_input = s3
        
        # Output bucket - must be in UseCase Account for SageMaker outputs
        output_bucket = usecase.get('s3_bucket')
        
        if not output_bucket:
            return create_response(400, {
                'error': 's3_bucket is required for SageMaker outputs. Please configure an S3 bucket in the UseCase Account for storing labeling job outputs, manifests, and models.'
            })
        
        # Generate unique job ID
        # SageMaker labeling job names must be 63 characters or less
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        safe_job_name = job_name.replace('.', '-').replace('_', '-').replace(' ', '-')
        sagemaker_job_name = f"dda-{safe_job_name}-{uuid.uuid4().hex[:8]}"
        
        # Validate labeling job name length (SageMaker limit is 63 characters)
        if len(sagemaker_job_name) > 63:
            # Truncate job name to fit within limit
            max_job_name_length = 63 - len("dda-") - len(uuid.uuid4().hex[:8]) - 1  # -1 for the hyphen
            if max_job_name_length < 1:
                return create_response(400, {
                    'error': f"Job name '{job_name}' is too long. Please use a shorter job name (maximum 40 characters recommended)."
                })
            
            truncated_job_name = safe_job_name[:max_job_name_length]
            sagemaker_job_name = f"dda-{truncated_job_name}-{uuid.uuid4().hex[:8]}"
            logger.warning(f"Job name truncated from '{safe_job_name}' to '{truncated_job_name}' to fit SageMaker limits")
        
        logger.info(f"Generated labeling job name: {sagemaker_job_name} (length: {len(sagemaker_job_name)})")
        
        # Step 1: List images from S3 (from Data Account if configured)
        print(f"Listing images from s3://{input_bucket}/{dataset_prefix}")
        images = list_images_from_s3(s3_for_input, input_bucket, dataset_prefix)
        
        if not images:
            return create_response(400, {'error': 'No images found in the specified prefix'})
        
        print(f"Found {len(images)} images")
        
        # Step 2: Generate manifest file
        # Manifest references the input bucket (Data Account if configured)
        manifest_key = f"manifests/{job_id}.manifest"
        manifest_content = generate_manifest(images, input_bucket)
        
        # Step 3: Upload manifest to UseCase Account S3 (where SageMaker runs)
        print(f"Uploading manifest to s3://{output_bucket}/{manifest_key}")
        s3.put_object(
            Bucket=output_bucket,
            Key=manifest_key,
            Body=manifest_content,
            ContentType='application/x-ndjson'
        )
        
        manifest_s3_uri = f"s3://{output_bucket}/{manifest_key}"
        output_s3_uri = f"s3://{output_bucket}/labeled/{job_id}/"
        
        # Step 4: Get Ground Truth execution role ARN
        # This role should exist in the UseCase Account
        # Always use DDASageMakerExecutionRole (don't trust old usecase records)
        ground_truth_role_arn = f"arn:aws:iam::{usecase['account_id']}:role/DDASageMakerExecutionRole"
        print(f"Using Ground Truth role: {ground_truth_role_arn}")
        
        # Step 5: Create UI template in S3 (UseCase Account)
        ui_template_content = create_ui_template(task_type, label_categories)
        ui_template_key = f"ui-templates/{job_id}-template.liquid"
        
        s3.put_object(
            Bucket=output_bucket,
            Key=ui_template_key,
            Body=ui_template_content,
            ContentType='text/html'
        )
        
        ui_template_s3_uri = f"s3://{output_bucket}/{ui_template_key}"
        
        # Step 6: Create label category config
        label_category_config = create_label_category_config(label_categories)
        label_config_key = f"manifests/{job_id}-label-categories.json"
        
        s3.put_object(
            Bucket=output_bucket,
            Key=label_config_key,
            Body=json.dumps(label_category_config),
            ContentType='application/json'
        )
        
        label_config_s3_uri = f"s3://{output_bucket}/{label_config_key}"
        
        # Step 7: Create Ground Truth labeling job
        print(f"Creating Ground Truth job: {sagemaker_job_name}")
        
        labeling_job_params = {
            'LabelingJobName': sagemaker_job_name,
            'LabelAttributeName': get_label_attribute_name(task_type),
            'InputConfig': {
                'DataSource': {
                    'S3DataSource': {
                        'ManifestS3Uri': manifest_s3_uri
                    }
                }
            },
            'OutputConfig': {
                'S3OutputPath': output_s3_uri
            },
            'RoleArn': ground_truth_role_arn,
            'LabelCategoryConfigS3Uri': label_config_s3_uri,
            'HumanTaskConfig': {
                'WorkteamArn': workforce_arn,
                'UiConfig': {
                    'UiTemplateS3Uri': ui_template_s3_uri
                },
                'PreHumanTaskLambdaArn': get_pre_human_task_lambda_arn(task_type),
                'TaskTitle': job_name,
                'TaskDescription': instructions or f"Label images for {job_name}",
                'NumberOfHumanWorkersPerDataObject': num_workers,
                'TaskTimeLimitInSeconds': task_time_limit,
                'TaskAvailabilityLifetimeInSeconds': 864000,  # 10 days
                'TaskKeywords': get_task_keywords(task_type),
                'AnnotationConsolidationConfig': {
                    'AnnotationConsolidationLambdaArn': get_annotation_consolidation_lambda_arn(task_type)
                }
            },
            'Tags': [
                {'Key': 'UseCase', 'Value': usecase_id},
                {'Key': 'JobName', 'Value': job_name}
            ]
        }
        
        sagemaker.create_labeling_job(**labeling_job_params)
        
        # Step 8: Store job metadata in DynamoDB
        now = int(datetime.utcnow().timestamp())
        job_item = {
            'job_id': job_id,
            'usecase_id': usecase_id,
            'job_name': job_name,
            'sagemaker_job_name': sagemaker_job_name,
            'status': 'InProgress',
            'task_type': task_type,
            'dataset_prefix': dataset_prefix,
            'image_count': len(images),
            'label_categories': label_categories,
            'manifest_s3_uri': manifest_s3_uri,
            'output_s3_uri': output_s3_uri,
            'workforce_arn': workforce_arn,
            'created_at': now,
            'updated_at': now,
            'created_by': event.get('requestContext', {}).get('authorizer', {}).get('claims', {}).get('sub', 'unknown')
        }
        
        labeling_jobs_table.put_item(Item=job_item)
        
        return create_response(201, {
            'job_id': job_id,
            'sagemaker_job_name': sagemaker_job_name,
            'status': 'InProgress',
            'message': 'Labeling job created successfully'
        })
        
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_message = e.response.get('Error', {}).get('Message', str(e))
        
        logger.error(f"AWS error creating labeling job: {error_code} - {error_message}")
        
        # Provide user-friendly error messages for common SageMaker validation errors
        if 'ValidationException' in error_code:
            if 'Member must have length less than' in error_message:
                if 'LabelingJobName' in error_message:
                    return create_response(400, {
                        'error': f"Labeling job name is too long. Please use a shorter job name (maximum 40 characters recommended)."
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
            return create_response(500, {'error': f"Failed to create labeling job: {error_message}"})
    except Exception as e:
        logger.error(f"Unexpected error creating labeling job: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def get_labeling_job(job_id: str):
    """Get labeling job details and sync status from SageMaker."""
    try:
        response = labeling_jobs_table.get_item(Key={'job_id': job_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Labeling job not found'})
        
        job = response['Item']
        
        # Sync latest status from SageMaker Ground Truth
        if job.get('sagemaker_job_name'):
            try:
                usecase = get_usecase(job['usecase_id'])
                credentials = assume_usecase_role(
                    usecase['cross_account_role_arn'],
                    usecase['external_id'],
                    'labeling-status-sync'
                )
                
                sagemaker = boto3.client(
                    'sagemaker',
                    aws_access_key_id=credentials['AccessKeyId'],
                    aws_secret_access_key=credentials['SecretAccessKey'],
                    aws_session_token=credentials['SessionToken']
                )
                
                sm_response = sagemaker.describe_labeling_job(
                    LabelingJobName=job['sagemaker_job_name']
                )
                
                status = sm_response['LabelingJobStatus']
                timestamp = int(datetime.utcnow().timestamp())
                
                # Update DynamoDB if status changed
                if status != job.get('status'):
                    logger.info(f"Labeling job status changed: {job.get('status')} -> {status}")
                    
                    # Get labeled object count
                    labeled_count = sm_response.get('LabelCounters', {}).get('HumanLabeled', 0)
                    total_count = sm_response.get('LabelCounters', {}).get('TotalLabeled', 0)
                    
                    # Calculate progress
                    progress_percent = 0
                    if job.get('image_count', 0) > 0:
                        progress_percent = int((labeled_count / job['image_count']) * 100)
                    
                    update_expr = 'SET #status = :status, updated_at = :updated, labeled_objects = :labeled, progress_percent = :progress'
                    expr_values = {
                        ':status': status,
                        ':updated': timestamp,
                        ':labeled': labeled_count,
                        ':progress': progress_percent
                    }
                    expr_names = {'#status': 'status'}
                    
                    if status == 'Completed':
                        update_expr += ', completed_at = :completed'
                        expr_values[':completed'] = timestamp
                        
                        # Get the actual output manifest URI from Ground Truth
                        if 'LabelingJobOutput' in sm_response and 'OutputDatasetS3Uri' in sm_response['LabelingJobOutput']:
                            output_manifest_uri = sm_response['LabelingJobOutput']['OutputDatasetS3Uri']
                            update_expr += ', output_manifest_s3_uri = :output_manifest'
                            expr_values[':output_manifest'] = output_manifest_uri
                            logger.info(f"Captured output manifest URI: {output_manifest_uri}")
                    elif status == 'Failed':
                        failure_reason = sm_response.get('FailureReason', 'Unknown')
                        update_expr += ', failure_reason = :reason'
                        expr_values[':reason'] = failure_reason
                    
                    labeling_jobs_table.update_item(
                        Key={'job_id': job_id},
                        UpdateExpression=update_expr,
                        ExpressionAttributeValues=expr_values,
                        ExpressionAttributeNames=expr_names
                    )
                    
                    # Update job dict for response
                    job['status'] = status
                    job['updated_at'] = timestamp
                    job['labeled_objects'] = labeled_count
                    job['progress_percent'] = progress_percent
                    if status == 'Completed':
                        job['completed_at'] = timestamp
                    elif status == 'Failed':
                        job['failure_reason'] = sm_response.get('FailureReason', 'Unknown')
                
            except Exception as e:
                logger.error(f"Error syncing labeling status: {str(e)}")
                # Continue with cached status from DynamoDB
        
        return create_response(200, {'job': job})
        
    except Exception as e:
        return handle_error(e, 'Failed to get labeling job')


def get_manifest(job_id: str):
    """Get the output manifest URL for a completed labeling job."""
    try:
        response = labeling_jobs_table.get_item(Key={'job_id': job_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Labeling job not found'})
        
        job = response['Item']
        
        if job['status'] != 'Completed':
            return create_response(400, {
                'error': 'Job is not completed yet',
                'status': job['status']
            })
        
        # Generate presigned URL for manifest download
        output_manifest_uri = f"{job['output_s3_uri']}manifests/output/output.manifest"
        
        return create_response(200, {
            'manifest_uri': output_manifest_uri,
            'job_id': job_id
        })
        
    except Exception as e:
        return handle_error(e, 'Failed to get manifest')


# Helper functions

def list_images_from_s3(s3_client, bucket: str, prefix: str) -> List[str]:
    """List all image files from S3 prefix."""
    images = []
    image_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'}
    
    paginator = s3_client.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
    
    for page in pages:
        for obj in page.get('Contents', []):
            key = obj['Key']
            ext = os.path.splitext(key)[1].lower()
            if ext in image_extensions:
                images.append(key)
    
    return images


def generate_manifest(image_keys: List[str], bucket: str) -> str:
    """
    Generate Ground Truth manifest file in JSONL format.
    Each line is a JSON object with source-ref pointing to an image.
    """
    manifest_lines = []
    
    for key in image_keys:
        manifest_lines.append(json.dumps({
            'source-ref': f"s3://{bucket}/{key}"
        }))
    
    return '\n'.join(manifest_lines)


def create_label_category_config(categories: List[str]) -> Dict:
    """Create label category configuration for Ground Truth."""
    return {
        'document-version': '2018-11-28',
        'labels': [{'label': cat} for cat in categories]
    }


def create_ui_template(task_type: str, label_categories: List[str]) -> str:
    """
    Create a Liquid UI template for Ground Truth labeling.
    Returns the template content as a string.
    """
    if task_type == 'ObjectDetection':
        # Bounding box template
        template = '''
<script src="https://assets.crowd.aws/crowd-html-elements.js"></script>

<crowd-form>
  <crowd-bounding-box
    name="boundingBox"
    src="{{ task.input.taskObject | grant_read_access }}"
    header="Draw bounding boxes around objects"
    labels="{{ task.input.labels | to_json | escape }}"
  >
    <full-instructions header="Bounding Box Instructions">
      <p>Draw tight bounding boxes around all instances of the specified objects.</p>
      <p>Make sure the boxes are as tight as possible around the objects.</p>
    </full-instructions>
    
    <short-instructions>
      Draw bounding boxes around the objects in the image.
    </short-instructions>
  </crowd-bounding-box>
</crowd-form>
'''
    elif task_type == 'Classification':
        # Image classification template
        labels_html = '\n'.join([f'        <crowd-radio-button name="{cat}" value="{cat}">{cat}</crowd-radio-button>' 
                                 for cat in label_categories])
        template = f'''
<script src="https://assets.crowd.aws/crowd-html-elements.js"></script>

<crowd-form>
  <crowd-image-classifier
    name="classification"
    src="{{{{ task.input.taskObject | grant_read_access }}}}"
    header="Select the category that best describes this image"
    categories="{{{{ task.input.labels | to_json | escape }}}}"
  >
    <full-instructions header="Classification Instructions">
      <p>Select the category that best matches the content of the image.</p>
    </full-instructions>
    
    <short-instructions>
      Select the appropriate category for this image.
    </short-instructions>
  </crowd-image-classifier>
</crowd-form>
'''
    elif task_type == 'Segmentation':
        # Semantic segmentation template
        template = '''
<script src="https://assets.crowd.aws/crowd-html-elements.js"></script>

<crowd-form>
  <crowd-semantic-segmentation
    name="segmentation"
    src="{{ task.input.taskObject | grant_read_access }}"
    header="Paint the objects in the image"
    labels="{{ task.input.labels | to_json | escape }}"
  >
    <full-instructions header="Segmentation Instructions">
      <p>Paint over all instances of the specified objects using the appropriate label.</p>
      <p>Be as precise as possible with the boundaries.</p>
    </full-instructions>
    
    <short-instructions>
      Paint the objects in the image using the provided labels.
    </short-instructions>
  </crowd-semantic-segmentation>
</crowd-form>
'''
    else:
        # Default template
        template = '''
<script src="https://assets.crowd.aws/crowd-html-elements.js"></script>

<crowd-form>
  <crowd-bounding-box
    name="boundingBox"
    src="{{ task.input.taskObject | grant_read_access }}"
    header="Label the image"
    labels="{{ task.input.labels | to_json | escape }}"
  >
    <full-instructions header="Instructions">
      <p>Follow the labeling instructions provided.</p>
    </full-instructions>
    
    <short-instructions>
      Label the image as instructed.
    </short-instructions>
  </crowd-bounding-box>
</crowd-form>
'''
    
    return template.strip()


def get_label_attribute_name(task_type: str) -> str:
    """Get the label attribute name based on task type."""
    mapping = {
        'ObjectDetection': 'bounding-box',
        'Classification': 'class',
        'Segmentation': 'semantic-segmentation-ref'  # Must end with '-ref' for Segmentation
    }
    return mapping.get(task_type, 'label')


def get_ui_template_arn(task_type: str) -> str:
    """Get AWS-provided UI template ARN for task type."""
    region = os.environ.get('AWS_REGION', 'us-east-1')
    
    templates = {
        'ObjectDetection': f"arn:aws:sagemaker:{region}:aws:labeling-job-template/BoundingBox",
        'Classification': f"arn:aws:sagemaker:{region}:aws:labeling-job-template/ImageMultiClass",
        'Segmentation': f"arn:aws:sagemaker:{region}:aws:labeling-job-template/SemanticSegmentation"
    }
    
    return templates.get(task_type, templates['ObjectDetection'])


def get_pre_human_task_lambda_arn(task_type: str) -> str:
    """Get pre-annotation Lambda ARN for task type."""
    region = os.environ.get('AWS_REGION', 'us-east-1')
    account = '432418664414'  # AWS-owned account for Ground Truth Lambdas
    
    lambdas = {
        'ObjectDetection': f"arn:aws:lambda:{region}:{account}:function:PRE-BoundingBox",
        'Classification': f"arn:aws:lambda:{region}:{account}:function:PRE-ImageMultiClass",
        'Segmentation': f"arn:aws:lambda:{region}:{account}:function:PRE-SemanticSegmentation"
    }
    
    return lambdas.get(task_type, lambdas['ObjectDetection'])


def get_annotation_consolidation_lambda_arn(task_type: str) -> str:
    """Get annotation consolidation Lambda ARN for task type."""
    region = os.environ.get('AWS_REGION', 'us-east-1')
    account = '432418664414'  # AWS-owned account for Ground Truth Lambdas
    
    lambdas = {
        'ObjectDetection': f"arn:aws:lambda:{region}:{account}:function:ACS-BoundingBox",
        'Classification': f"arn:aws:lambda:{region}:{account}:function:ACS-ImageMultiClass",
        'Segmentation': f"arn:aws:lambda:{region}:{account}:function:ACS-SemanticSegmentation"
    }
    
    return lambdas.get(task_type, lambdas['ObjectDetection'])


def get_task_keywords(task_type: str) -> List[str]:
    """Get task keywords for Ground Truth built-in task types."""
    keywords = {
        'ObjectDetection': ['Image', 'Object Detection', 'Bounding Box'],
        'Classification': ['Image', 'Classification', 'Multiclass'],
        'Segmentation': ['Image', 'Segmentation', 'Semantic']
    }
    
    return keywords.get(task_type, ['Image', 'Labeling'])



def list_workteams(event):
    """
    List available workteams for a use case.
    
    Query Parameters:
        - usecase_id: Required. The use case to list workteams for
    """
    try:
        params = event.get('queryStringParameters', {}) or {}
        usecase_id = params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Get use case details
        usecase = get_usecase(usecase_id)
        
        # Assume UseCase Account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            'list-workteams'
        )
        
        # Create SageMaker client with assumed credentials
        sagemaker = boto3.client(
            'sagemaker',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # List workteams
        workteams = []
        paginator = sagemaker.get_paginator('list_workteams')
        
        for page in paginator.paginate():
            for workteam in page.get('Workteams', []):
                workteams.append({
                    'name': workteam['WorkteamName'],
                    'arn': workteam['WorkteamArn'],
                    'description': workteam.get('Description', ''),
                    'member_count': len(workteam.get('MemberDefinitions', []))
                })
        
        return create_response(200, {
            'workteams': workteams,
            'count': len(workteams)
        })
        
    except Exception as e:
        return handle_error(e, 'Failed to list workteams')


def transform_manifest(event):
    """
    Transform Ground Truth manifest to DDA-compatible format.
    
    Ground Truth creates manifests with job-specific attribute names like:
    - "my-labeling-job" (label value)
    - "my-labeling-job-metadata" (metadata)
    
    DDA model requires standardized names:
    - "anomaly-label" (label value)
    - "anomaly-label-metadata" (metadata)
    
    Request Body:
        - usecase_id: Required. UseCase for S3 access
        - source_manifest_uri: Required. S3 URI of Ground Truth output manifest
        - output_manifest_uri: Optional. S3 URI for transformed manifest (defaults to same location with -dda suffix)
        - task_type: Optional. "classification" or "segmentation" (default: "classification")
    
    Returns:
        - transformed_manifest_uri: S3 URI of the DDA-compatible manifest
        - stats: Statistics about the transformation
    """
    try:
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        if not body.get('usecase_id'):
            return create_response(400, {'error': 'usecase_id is required'})
        if not body.get('source_manifest_uri'):
            return create_response(400, {'error': 'source_manifest_uri is required'})
        
        usecase_id = body['usecase_id']
        source_manifest_uri = body['source_manifest_uri']
        output_manifest_uri = body.get('output_manifest_uri')
        task_type = body.get('task_type', 'classification')
        
        # Parse S3 URI
        if not source_manifest_uri.startswith('s3://'):
            return create_response(400, {'error': 'source_manifest_uri must be an S3 URI (s3://bucket/key)'})
        
        source_parts = source_manifest_uri.replace('s3://', '').split('/', 1)
        if len(source_parts) != 2:
            return create_response(400, {'error': 'Invalid S3 URI format'})
        
        source_bucket = source_parts[0]
        source_key = source_parts[1]
        
        # Get use case details
        usecase = get_usecase(usecase_id)
        
        # Assume UseCase Account role for S3 access
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            'transform-manifest'
        )
        
        # Create S3 client with assumed credentials
        s3 = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Download source manifest
        logger.info(f"Downloading manifest from {source_manifest_uri}")
        try:
            response = s3.get_object(Bucket=source_bucket, Key=source_key)
            manifest_content = response['Body'].read().decode('utf-8')
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                return create_response(404, {'error': 'Source manifest not found'})
            raise
        
        # Parse manifest (JSONL format - one JSON object per line)
        manifest_lines = manifest_content.strip().split('\n')
        if not manifest_lines:
            return create_response(400, {'error': 'Source manifest is empty'})
        
        # Detect Ground Truth attribute names from first line
        first_entry = json.loads(manifest_lines[0])
        detected_attrs = detect_ground_truth_attributes(first_entry, task_type)
        
        if not detected_attrs:
            return create_response(400, {
                'error': 'Could not detect Ground Truth attribute names in manifest. '
                         'Make sure this is a valid Ground Truth output manifest.'
            })
        
        logger.info(f"Detected Ground Truth attributes: {detected_attrs}")
        
        # Transform manifest
        transformed_lines = []
        stats = {
            'total_entries': len(manifest_lines),
            'transformed': 0,
            'skipped': 0,
            'errors': []
        }
        
        for i, line in enumerate(manifest_lines):
            try:
                entry = json.loads(line)
                transformed_entry = transform_manifest_entry(entry, detected_attrs, task_type)
                transformed_lines.append(json.dumps(transformed_entry))
                stats['transformed'] += 1
            except Exception as e:
                logger.warning(f"Error transforming line {i+1}: {str(e)}")
                stats['skipped'] += 1
                stats['errors'].append(f"Line {i+1}: {str(e)}")
        
        if stats['transformed'] == 0:
            return create_response(400, {
                'error': 'No entries could be transformed',
                'stats': stats
            })
        
        # Generate output manifest content
        transformed_content = '\n'.join(transformed_lines)
        
        # Determine output location
        if not output_manifest_uri:
            # Default: same location with -dda suffix
            base_key = source_key.rsplit('.', 1)[0]
            output_key = f"{base_key}-dda.manifest"
            output_bucket = source_bucket
            output_manifest_uri = f"s3://{output_bucket}/{output_key}"
        else:
            # Parse provided output URI
            output_parts = output_manifest_uri.replace('s3://', '').split('/', 1)
            if len(output_parts) != 2:
                return create_response(400, {'error': 'Invalid output_manifest_uri format'})
            output_bucket = output_parts[0]
            output_key = output_parts[1]
        
        # Upload transformed manifest
        logger.info(f"Uploading transformed manifest to {output_manifest_uri}")
        s3.put_object(
            Bucket=output_bucket,
            Key=output_key,
            Body=transformed_content,
            ContentType='application/x-ndjson'
        )
        
        # Update labeling job record if this transformation is for a labeling job
        # Check if source manifest matches a labeling job's output manifest pattern
        try:
            # Query labeling jobs to find matching job
            labeling_jobs_response = labeling_jobs_table.query(
                IndexName='usecase-jobs-index',
                KeyConditionExpression='usecase_id = :usecase_id',
                ExpressionAttributeValues={':usecase_id': usecase_id}
            )
            
            for job in labeling_jobs_response.get('Items', []):
                job_output_uri = job.get('output_s3_uri', '')
                # Check if source manifest is from this job's output
                if job_output_uri and source_manifest_uri.startswith(job_output_uri):
                    # Update job record with transformed manifest info
                    timestamp = int(datetime.utcnow().timestamp())
                    labeling_jobs_table.update_item(
                        Key={'job_id': job['job_id']},
                        UpdateExpression='SET transformed_manifest_s3_uri = :transformed_uri, is_transformed = :is_transformed, transformed_at = :transformed_at',
                        ExpressionAttributeValues={
                            ':transformed_uri': output_manifest_uri,
                            ':is_transformed': True,
                            ':transformed_at': timestamp
                        }
                    )
                    logger.info(f"Updated labeling job {job['job_id']} with transformed manifest URI")
                    break
        except Exception as e:
            logger.warning(f"Could not update labeling job record: {str(e)}")
            # Continue anyway - transformation succeeded
        
        # Generate sample entry for user reference
        sample_entry = json.loads(transformed_lines[0]) if transformed_lines else None
        
        return create_response(200, {
            'message': 'Manifest transformed successfully',
            'transformed_manifest_uri': output_manifest_uri,
            'stats': stats,
            'detected_attributes': detected_attrs,
            'dda_attributes': {
                'label': 'anomaly-label',
                'metadata': 'anomaly-label-metadata'
            },
            'sample_entry': sample_entry
        })
        
    except Exception as e:
        logger.error(f"Error transforming manifest: {str(e)}")
        return handle_error(e, 'Failed to transform manifest')


def detect_ground_truth_attributes(entry: Dict, task_type: str) -> Dict[str, str]:
    """
    Detect Ground Truth attribute names from a manifest entry.
    
    For classification:
        Returns: label_attr, metadata_attr
    
    For segmentation:
        Returns: label_attr, metadata_attr, mask_ref_attr, mask_ref_metadata_attr
    """
    # Skip known DDA attributes
    skip_attrs = {
        'source-ref', 
        'anomaly-label', 
        'anomaly-label-metadata',
        'anomaly-mask-ref',
        'anomaly-mask-ref-metadata'
    }
    
    # Find metadata attribute (ends with -metadata)
    metadata_attr = None
    for key in entry.keys():
        if key.endswith('-metadata') and key not in skip_attrs:
            metadata_attr = key
            break
    
    if not metadata_attr:
        return None
    
    # Derive label attribute (remove -metadata suffix)
    label_attr = metadata_attr.replace('-metadata', '')
    
    # Verify label attribute exists
    if label_attr not in entry:
        return None
    
    result = {
        'label_attr': label_attr,
        'metadata_attr': metadata_attr
    }
    
    # For segmentation, also find mask reference attributes
    if task_type == 'segmentation':
        # Look for -ref and -ref-metadata attributes
        mask_ref_attr = None
        mask_ref_metadata_attr = None
        
        for key in entry.keys():
            if key.endswith('-ref-metadata') and key not in skip_attrs:
                mask_ref_metadata_attr = key
                mask_ref_attr = key.replace('-metadata', '')
                break
            elif key.endswith('-ref') and key not in skip_attrs and not key.endswith('-ref-metadata'):
                # Found a -ref attribute, derive metadata
                mask_ref_attr = key
                mask_ref_metadata_attr = f"{key}-metadata"
        
        # Verify mask attributes exist
        if mask_ref_attr and mask_ref_attr in entry:
            result['mask_ref_attr'] = mask_ref_attr
            if mask_ref_metadata_attr and mask_ref_metadata_attr in entry:
                result['mask_ref_metadata_attr'] = mask_ref_metadata_attr
    
    return result


def transform_manifest_entry(entry: Dict, detected_attrs: Dict, task_type: str) -> Dict:
    """
    Transform a single manifest entry to DDA format.
    
    For classification:
        Renames: label_attr -> 'anomaly-label', metadata_attr -> 'anomaly-label-metadata'
    
    For segmentation:
        Renames: label_attr -> 'anomaly-label', metadata_attr -> 'anomaly-label-metadata'
                 mask_ref_attr -> 'anomaly-mask-ref', mask_ref_metadata_attr -> 'anomaly-mask-ref-metadata'
    
    Also updates the 'job-name' field inside metadata to match 'anomaly-label'
    """
    transformed = {}
    
    # Copy source-ref (always present)
    if 'source-ref' in entry:
        transformed['source-ref'] = entry['source-ref']
    
    # Transform label attribute
    label_attr = detected_attrs['label_attr']
    if label_attr in entry:
        transformed['anomaly-label'] = entry[label_attr]
    
    # Transform metadata attribute
    metadata_attr = detected_attrs['metadata_attr']
    if metadata_attr in entry:
        metadata = entry[metadata_attr].copy() if isinstance(entry[metadata_attr], dict) else entry[metadata_attr]
        
        # Update job-name inside metadata to match the new attribute name
        if isinstance(metadata, dict) and 'job-name' in metadata:
            metadata['job-name'] = 'anomaly-label'
        
        transformed['anomaly-label-metadata'] = metadata
    
    # For segmentation, transform mask attributes
    if task_type == 'segmentation':
        # Transform mask reference
        mask_ref_attr = detected_attrs.get('mask_ref_attr')
        if mask_ref_attr and mask_ref_attr in entry:
            transformed['anomaly-mask-ref'] = entry[mask_ref_attr]
        
        # Transform mask reference metadata
        mask_ref_metadata_attr = detected_attrs.get('mask_ref_metadata_attr')
        if mask_ref_metadata_attr and mask_ref_metadata_attr in entry:
            mask_metadata = entry[mask_ref_metadata_attr].copy() if isinstance(entry[mask_ref_metadata_attr], dict) else entry[mask_ref_metadata_attr]
            
            # Update job-name in mask metadata if present
            if isinstance(mask_metadata, dict) and 'job-name' in mask_metadata:
                mask_metadata['job-name'] = 'anomaly-mask-ref'
            
            transformed['anomaly-mask-ref-metadata'] = mask_metadata
    
    # Copy any other attributes (like confidence scores, etc.)
    skip_attrs = {
        'source-ref', 
        label_attr, 
        metadata_attr,
        detected_attrs.get('mask_ref_attr'),
        detected_attrs.get('mask_ref_metadata_attr')
    }
    
    for key, value in entry.items():
        if key not in skip_attrs and key not in transformed:
            transformed[key] = value
    
    return transformed
