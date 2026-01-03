"""
Lambda function for Ground Truth labeling job management.
Handles job creation, monitoring, and manifest generation.
"""

import json
import boto3
import os
import uuid
from typing import Dict, List, Any
from datetime import datetime

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
        
        # Assume UseCase Account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            'create-labeling-job'
        )
        
        # Create clients with assumed credentials
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
        
        bucket = usecase['s3_bucket']
        
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
        
        # Step 1: List images from S3
        print(f"Listing images from s3://{bucket}/{dataset_prefix}")
        images = list_images_from_s3(s3, bucket, dataset_prefix)
        
        if not images:
            return create_response(400, {'error': 'No images found in the specified prefix'})
        
        print(f"Found {len(images)} images")
        
        # Step 2: Generate manifest file
        manifest_key = f"manifests/{job_id}.manifest"
        manifest_content = generate_manifest(images, bucket)
        
        # Step 3: Upload manifest to S3
        print(f"Uploading manifest to s3://{bucket}/{manifest_key}")
        s3.put_object(
            Bucket=bucket,
            Key=manifest_key,
            Body=manifest_content,
            ContentType='application/x-ndjson'
        )
        
        manifest_s3_uri = f"s3://{bucket}/{manifest_key}"
        output_s3_uri = f"s3://{bucket}/labeled/{job_id}/"
        
        # Step 4: Get Ground Truth execution role ARN
        # This role should exist in the UseCase Account
        # Always use DDASageMakerExecutionRole (don't trust old usecase records)
        ground_truth_role_arn = f"arn:aws:iam::{usecase['account_id']}:role/DDASageMakerExecutionRole"
        print(f"Using Ground Truth role: {ground_truth_role_arn}")
        
        # Step 5: Create UI template in S3
        ui_template_content = create_ui_template(task_type, label_categories)
        ui_template_key = f"ui-templates/{job_id}-template.liquid"
        
        s3.put_object(
            Bucket=bucket,
            Key=ui_template_key,
            Body=ui_template_content,
            ContentType='text/html'
        )
        
        ui_template_s3_uri = f"s3://{bucket}/{ui_template_key}"
        
        # Step 6: Create label category config
        label_category_config = create_label_category_config(label_categories)
        label_config_key = f"manifests/{job_id}-label-categories.json"
        
        s3.put_object(
            Bucket=bucket,
            Key=label_config_key,
            Body=json.dumps(label_category_config),
            ContentType='application/json'
        )
        
        label_config_s3_uri = f"s3://{bucket}/{label_config_key}"
        
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
    """Get labeling job details."""
    try:
        response = labeling_jobs_table.get_item(Key={'job_id': job_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Labeling job not found'})
        
        job = response['Item']
        
        # Optionally fetch latest status from SageMaker
        # This would require assuming the role again
        
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
        'Classification': ['Image', 'Classification', 'Multi-class'],
        'Segmentation': ['Image', 'Segmentation', 'Semantic Segmentation']
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
