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
    handle_error,
    get_usecase_region
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
        
        # Add output_manifest_s3_uri for completed jobs
        for job in jobs:
            if job.get('status') == 'Completed':
                # Use the manifest URI captured from SageMaker's response
                # If not available, use transformed manifest if available
                if not job.get('output_manifest_s3_uri'):
                    if job.get('is_transformed') and job.get('transformed_manifest_s3_uri'):
                        job['output_manifest_s3_uri'] = job['transformed_manifest_s3_uri']
        
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
        - dataset_prefix: Required. S3 prefix containing images to label
        - task_type: Required. ObjectDetection, Classification, or Segmentation
        - label_categories: Required. List of label names
        - workforce_arn: Required. WorkTeam ARN
        - instructions: Optional. Labeling instructions
        - num_workers_per_object: Optional. Default 1
        - task_time_limit: Optional. Default 600 seconds
        - mask_prefix: Optional. S3 prefix containing segmentation masks (for Segmentation task type)
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
        enable_automated_labeling = bool(body.get('enable_automated_labeling', False))
        task_time_limit = body.get('task_time_limit', 600)
        mask_prefix = body.get('mask_prefix')  # Optional for segmentation
        
        # Get use case details
        usecase = get_usecase(usecase_id)
        
        # Assume UseCase Account role for SageMaker
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            'create-labeling-job'
        )
        
        # Create SageMaker client with UseCase Account credentials
        sagemaker = boto3.client(
            'sagemaker',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Create S3 client for verification and manifest upload
        s3 = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Determine input data source (Data Account or UseCase Account)
        data_role_arn = usecase.get('data_account_role_arn')
        data_account_id = usecase.get('data_account_id')
        usecase_account_id = usecase.get('account_id')
        
        is_separate_data_account = (
            data_role_arn and 
            data_account_id and 
            data_account_id != usecase_account_id
        )
        
        if is_separate_data_account:
            logger.info(f"Using separate Data Account {data_account_id} for input data")
            data_external_id = usecase.get('data_account_external_id')
            if not data_external_id:
                return create_response(400, {
                    'error': 'data_account_external_id is required when using a separate Data Account.'
                })
            data_credentials = assume_usecase_role(
                data_role_arn,
                data_external_id,
                'labeling-data-access'
            )
            input_bucket = usecase.get('data_s3_bucket') or usecase.get('s3_bucket')
            
            s3_data = boto3.client(
                's3',
                aws_access_key_id=data_credentials['AccessKeyId'],
                aws_secret_access_key=data_credentials['SecretAccessKey'],
                aws_session_token=data_credentials['SessionToken']
            )
            s3_for_input = s3_data
        else:
            logger.info(f"Data Account is same as UseCase Account {usecase_account_id}")
            input_bucket = usecase.get('data_s3_bucket') or usecase['s3_bucket']
            s3_for_input = s3
        
        # Output bucket - must be in UseCase Account for SageMaker outputs
        output_bucket = usecase.get('s3_bucket')
        if not output_bucket:
            return create_response(400, {
                'error': 's3_bucket is required for SageMaker outputs.'
            })
        
        # Generate unique job ID
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        safe_job_name = job_name.replace('.', '-').replace('_', '-').replace(' ', '-')
        sagemaker_job_name = f"dda-{safe_job_name}-{uuid.uuid4().hex[:8]}"
        
        # Validate labeling job name length (SageMaker limit is 63 characters)
        if len(sagemaker_job_name) > 63:
            max_job_name_length = 63 - len("dda-") - len(uuid.uuid4().hex[:8]) - 1
            if max_job_name_length < 1:
                return create_response(400, {
                    'error': f"Job name '{job_name}' is too long. Please use a shorter job name (maximum 40 characters recommended)."
                })
            truncated_job_name = safe_job_name[:max_job_name_length]
            sagemaker_job_name = f"dda-{truncated_job_name}-{uuid.uuid4().hex[:8]}"
        
        logger.info(f"Generated labeling job name: {sagemaker_job_name}")
        
        # Step 1: List images from S3
        logger.info(f"Listing images from s3://{input_bucket}/{dataset_prefix}")
        images = list_images_from_s3(s3_for_input, input_bucket, dataset_prefix)
        
        if not images:
            return create_response(400, {'error': 'No images found in the specified prefix'})
        
        logger.info(f"Found {len(images)} images")
        
        # Step 2: For segmentation, discover masks if mask_prefix provided
        mask_map = {}
        if task_type == 'Segmentation' and mask_prefix:
            logger.info(f"Discovering masks in s3://{input_bucket}/{mask_prefix}")
            try:
                paginator = s3_for_input.get_paginator('list_objects_v2')
                pages = paginator.paginate(Bucket=input_bucket, Prefix=mask_prefix)
                
                mask_extensions = {'.png', '.jpg', '.jpeg'}
                for page in pages:
                    for obj in page.get('Contents', []):
                        mask_key = obj['Key']
                        mask_name = os.path.basename(mask_key)
                        mask_stem = os.path.splitext(mask_name)[0]
                        ext = os.path.splitext(mask_key)[1].lower()
                        
                        if ext in mask_extensions:
                            mask_map[mask_stem] = mask_key
                
                logger.info(f"Found {len(mask_map)} masks")
            except Exception as e:
                logger.warning(f"Error discovering masks: {str(e)}")
        
        # Step 3: Generate manifest file
        manifest_key = f"manifests/{job_id}.manifest"
        manifest_content = generate_manifest_with_masks(images, input_bucket, task_type, mask_map)
        
        # Step 4: Upload manifest to UseCase Account S3
        logger.info(f"Uploading manifest to s3://{output_bucket}/{manifest_key}")
        s3.put_object(
            Bucket=output_bucket,
            Key=manifest_key,
            Body=manifest_content,
            ContentType='application/x-ndjson'
        )
        
        manifest_s3_uri = f"s3://{output_bucket}/{manifest_key}"
        # Note: SageMaker Ground Truth appends the LabelingJobName to the S3OutputPath
        # So we set the base path, and SageMaker will create: {output_s3_uri}{sagemaker_job_name}/manifests/output/output.manifest
        output_s3_uri = f"s3://{output_bucket}/labeled/"
        
        # Step 5: Get Ground Truth execution role ARN
        ground_truth_role_arn = f"arn:aws:iam::{usecase['account_id']}:role/DDASageMakerExecutionRole"
        logger.info(f"Using Ground Truth role: {ground_truth_role_arn}")
        
        # Step 6: Create Ground Truth labeling job
        logger.info(f"Creating Ground Truth job: {sagemaker_job_name}")
        
        # Map task types to SageMaker built-in algorithm ARNs (for automated/active learning)
        region = get_usecase_region(usecase)
        task_type_arn_mapping = {
            'Classification': f'arn:aws:sagemaker:{region}:aws:labeling-job-algorithm/image-classification',
            'ObjectDetection': f'arn:aws:sagemaker:{region}:aws:labeling-job-algorithm/bounding-box',
            'Segmentation': f'arn:aws:sagemaker:{region}:aws:labeling-job-algorithm/semantic-segmentation'
        }
        
        # Built-in task types require PRE/ACS Lambdas (these make it a built-in
        # job rather than "Custom") plus a worker task template in S3.
        # NOTE: image classification & semantic segmentation use UiTemplateS3Uri,
        # NOT HumanTaskUiArn (that's only for NER / 3D point cloud / video frame).
        pre_lambda_arn, acs_lambda_arn = get_builtin_task_lambdas(task_type, region)
        if not pre_lambda_arn or not acs_lambda_arn:
            return create_response(400, {
                'error': f"Unsupported task type '{task_type}' or region '{region}' "
                         f"for built-in labeling. Supported task types: "
                         f"{', '.join(GROUND_TRUTH_TASK_FUNCTIONS.keys())}."
            })
        
        # Generate and upload the worker task template to the output bucket.
        template_key = f"templates/{job_id}.liquid.html"
        template_body = generate_worker_template(task_type, label_categories)
        logger.info(f"Uploading worker template to s3://{output_bucket}/{template_key}")
        s3.put_object(
            Bucket=output_bucket,
            Key=template_key,
            Body=template_body.encode('utf-8'),
            ContentType='text/html'
        )
        ui_template_s3_uri = f"s3://{output_bucket}/{template_key}"
        
        # Built-in image task types also require a label-category config JSON
        # (referenced via LabelCategoryConfigS3Uri) listing the categories.
        label_config_key = f"label-categories/{job_id}.json"
        label_config_body = generate_label_category_config(label_categories)
        logger.info(f"Uploading label category config to s3://{output_bucket}/{label_config_key}")
        s3.put_object(
            Bucket=output_bucket,
            Key=label_config_key,
            Body=label_config_body.encode('utf-8'),
            ContentType='application/json'
        )
        label_category_config_s3_uri = f"s3://{output_bucket}/{label_config_key}"
        
        human_task_config = {
            'WorkteamArn': workforce_arn,
            'TaskTitle': job_name,
            'TaskDescription': instructions or f"Label images for {job_name}",
            'NumberOfHumanWorkersPerDataObject': num_workers,
            'TaskTimeLimitInSeconds': task_time_limit,
            'TaskAvailabilityLifetimeInSeconds': 864000,  # 10 days
            'PreHumanTaskLambdaArn': pre_lambda_arn,
            'AnnotationConsolidationConfig': {
                'AnnotationConsolidationLambdaArn': acs_lambda_arn
            },
            'UiConfig': {
                'UiTemplateS3Uri': ui_template_s3_uri
            }
        }
        
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
            'LabelCategoryConfigS3Uri': label_category_config_s3_uri,
            'HumanTaskConfig': human_task_config,
            'Tags': [
                {'Key': 'UseCase', 'Value': usecase_id},
                {'Key': 'JobName', 'Value': job_name}
            ]
        }
        
        # Automated (active) learning is OPTIONAL and only valid for built-in
        # task types. Only attach it when the user explicitly enables it.
        if enable_automated_labeling:
            if task_type not in task_type_arn_mapping:
                return create_response(400, {
                    'error': f"Automated labeling isn't supported for task type '{task_type}'."
                })
            labeling_job_params['LabelingJobAlgorithmsConfig'] = {
                'LabelingJobAlgorithmSpecificationArn': task_type_arn_mapping[task_type]
            }
        
        sagemaker.create_labeling_job(**labeling_job_params)
        
        # Step 7: Store job metadata in DynamoDB
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
            elif 'WorkteamArn' in error_message or 'workteam' in error_message.lower():
                return create_response(400, {
                    'error': f'Invalid workteam. Ensure the workteam exists in SageMaker Ground Truth and is properly configured. Details: {error_message}'
                })
            elif 'ManifestS3Uri' in error_message or 'manifest' in error_message.lower():
                return create_response(400, {
                    'error': f'Failed to access the manifest file. Ensure the S3 bucket and prefix are correct and contain images. Details: {error_message}'
                })
            elif 'UiTemplateS3Uri' in error_message or 'template' in error_message.lower():
                return create_response(400, {
                    'error': f'Problem with the labeling UI template. Details: {error_message}'
                })
            else:
                return create_response(400, {'error': f"Validation error: {error_message}"})
        elif 'AccessDenied' in error_code or 'AccessDeniedException' in error_code:
            return create_response(403, {
                'error': 'Access denied. Please check that your SageMaker execution role has permissions to create labeling jobs and access the S3 buckets.'
            })
        elif 'ResourceLimitExceeded' in error_code:
            return create_response(429, {'error': 'Resource limit exceeded. Please try again later or contact support.'})
        elif 'EntityAlreadyExists' in error_code or 'already exists' in error_message.lower():
            return create_response(400, {
                'error': 'A labeling job with this name already exists. Please use a different job name.'
            })
        elif 'NoSuchEntity' in error_code or 'not found' in error_message.lower():
            return create_response(400, {
                'error': 'The specified workteam or resource was not found. Please verify the workteam exists in SageMaker Ground Truth.'
            })
        else:
            return create_response(500, {'error': f"Failed to create labeling job: {error_message}"})
    except Exception as e:
        logger.error(f"Unexpected error creating labeling job: {str(e)}", exc_info=True)
        error_str = str(e)
        
        # Provide helpful error messages for common issues
        if 'No images found' in error_str:
            return create_response(400, {
                'error': 'No images found in the specified S3 prefix. Please check that the prefix contains image files (jpg, png, bmp, tiff).'
            })
        elif 'workteam' in error_str.lower():
            return create_response(400, {
                'error': 'Workteam error. Please ensure the workteam is properly configured in SageMaker Ground Truth.'
            })
        elif 'bucket' in error_str.lower() or 's3' in error_str.lower():
            return create_response(400, {
                'error': 'S3 bucket error. Please check that the bucket exists and you have permission to access it.'
            })
        else:
            return create_response(500, {'error': f'Internal server error: {error_str}'})


def get_labeling_job(job_id: str):
    """Get labeling job details and sync status from SageMaker."""
    try:
        response = labeling_jobs_table.get_item(Key={'job_id': job_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Labeling job not found'})
        
        job = response['Item']
        
        # Region for building console / worker-portal links
        try:
            region_for_links = get_usecase_region(get_usecase(job['usecase_id']))
        except Exception:
            region_for_links = os.environ.get('AWS_REGION', 'us-east-1')
        
        # Deep link to the labeling job in the SageMaker Ground Truth console.
        if job.get('sagemaker_job_name'):
            job['console_url'] = (
                f"https://{region_for_links}.console.aws.amazon.com/sagemaker/groundtruth"
                f"?region={region_for_links}#/labeling-jobs/details/{job['sagemaker_job_name']}"
            )
        
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
                
                # Resolve the worker portal sign-in URL from the workteam's
                # Cognito sub-domain so labelers know where to log in.
                try:
                    workteam_arn = (
                        sm_response.get('HumanTaskConfig', {}).get('WorkteamArn')
                        or job.get('workforce_arn')
                    )
                    if workteam_arn:
                        wt_name = workteam_arn.split('/')[-1]
                        wt_resp = sagemaker.describe_workteam(WorkteamName=wt_name)
                        sub_domain = wt_resp.get('Workteam', {}).get('SubDomain')
                        if sub_domain:
                            job['worker_portal_url'] = (
                                sub_domain if sub_domain.startswith('http')
                                else f"https://{sub_domain}"
                            )
                except Exception as e:
                    logger.warning(f"Could not resolve worker portal URL: {str(e)}")
                
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
        
        # Use the manifest URI captured from SageMaker's response
        output_manifest_uri = job.get('output_manifest_s3_uri')
        if not output_manifest_uri:
            return create_response(400, {
                'error': 'Output manifest URI not available. Job may still be processing.'
            })
        
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


def generate_manifest_with_masks(image_keys: List[str], bucket: str, task_type: str, mask_map: Dict[str, str]) -> str:
    """
    Generate Ground Truth manifest file in JSONL format.
    Supports both classification and segmentation tasks.
    
    For classification: Only includes source-ref
    For segmentation: Includes source-ref and mask references if masks are found
    
    Args:
        image_keys: List of S3 keys for images
        bucket: S3 bucket name
        task_type: Task type (Classification, ObjectDetection, or Segmentation)
        mask_map: Dict mapping image stem to mask S3 key (for segmentation)
    
    Returns:
        JSONL manifest content as string
    """
    manifest_lines = []
    
    for key in image_keys:
        # Extract image filename without extension
        image_name = os.path.basename(key)
        image_stem = os.path.splitext(image_name)[0]
        
        entry = {
            'source-ref': f"s3://{bucket}/{key}"
        }
        
        # For segmentation, add mask reference if available
        if task_type == 'Segmentation' and image_stem in mask_map:
            mask_key = mask_map[image_stem]
            entry['anomaly-mask-ref'] = f"s3://{bucket}/{mask_key}"
            entry['anomaly-mask-ref-metadata'] = {
                'internal-color-map': {
                    '0': {
                        'class-name': 'defect',
                        'hex-color': '#23A436'
                    }
                },
                'job-name': 'labeling-job/object-mask-ref',
                'human-annotated': 'yes',
                'type': 'groundtruth/semantic-segmentation'
            }
        
        manifest_lines.append(json.dumps(entry))
    
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


# AWS account that hosts the Ground Truth built-in PRE/ACS Lambda functions,
# keyed by region. Source: SageMaker API Reference (HumanTaskConfig /
# AnnotationConsolidationConfig).
GROUND_TRUTH_LAMBDA_ACCOUNTS = {
    'us-east-1': '432418664414',
    'us-east-2': '266458841044',
    'us-west-2': '081040173940',
    'ca-central-1': '918755190332',
    'eu-west-1': '568282634449',
    'eu-west-2': '487402164563',
    'eu-central-1': '203001061592',
    'ap-northeast-1': '477331159723',
    'ap-northeast-2': '845288260483',
    'ap-south-1': '565803892007',
    'ap-southeast-1': '377565633583',
    'ap-southeast-2': '454466003867',
}

# Built-in task type -> Ground Truth Lambda function name suffix.
GROUND_TRUTH_TASK_FUNCTIONS = {
    'Classification': 'ImageMultiClass',
    'ObjectDetection': 'BoundingBox',
    'Segmentation': 'SemanticSegmentation',
}


def get_builtin_task_lambdas(task_type: str, region: str):
    """Return (PreHumanTaskLambdaArn, AnnotationConsolidationLambdaArn) for a
    built-in Ground Truth task type, or (None, None) if unsupported/region
    unknown. These are required for built-in labeling jobs."""
    account = GROUND_TRUTH_LAMBDA_ACCOUNTS.get(region)
    func = GROUND_TRUTH_TASK_FUNCTIONS.get(task_type)
    if not account or not func:
        return None, None
    pre = f"arn:aws:lambda:{region}:{account}:function:PRE-{func}"
    acs = f"arn:aws:lambda:{region}:{account}:function:ACS-{func}"
    return pre, acs


def generate_label_category_config(label_categories) -> str:
    """Build the LabelCategoryConfigS3Uri JSON document required by built-in
    image task types (image classification / semantic segmentation)."""
    return json.dumps({
        'document-version': '2018-11-28',
        'labels': [{'label': c} for c in label_categories]
    })


def generate_worker_template(task_type: str, label_categories) -> str:
    """Build a Ground Truth Liquid worker-task template (HTML) for a built-in
    image task type. SageMaker requires a UiTemplateS3Uri for image
    classification / semantic segmentation (HumanTaskUiArn is only for NER,
    3D point cloud, and video frame jobs).

    Categories are bound from the label-category config via
    `task.input.labels` (the canonical Ground Truth pattern) rather than
    hardcoded, so the UI stays in sync with LabelCategoryConfigS3Uri.
    """
    if task_type == 'Segmentation':
        return """<script src="https://assets.crowd.aws/crowd-html-elements.js"></script>
<crowd-form>
  <crowd-semantic-segmentation
    name="crowd-semantic-segmentation"
    src="{{ task.input.taskObject | grant_read_access }}"
    header="Segment the image"
    labels="{{ task.input.labels | to_json | escape }}"
  >
    <full-instructions header="Segmentation instructions">
      Use the tools to label every pixel that belongs to each category.
    </full-instructions>
    <short-instructions>
      Paint each region with the matching category.
    </short-instructions>
  </crowd-semantic-segmentation>
</crowd-form>"""

    # Default: image (multi-class / single-label) classification
    return """<script src="https://assets.crowd.aws/crowd-html-elements.js"></script>
<crowd-form>
  <crowd-image-classifier
    name="crowd-image-classifier"
    src="{{ task.input.taskObject | grant_read_access }}"
    header="Classify the image"
    categories="{{ task.input.labels | to_json | escape }}"
  >
    <full-instructions header="Classification instructions">
      Choose the single category that best describes the image.
    </full-instructions>
    <short-instructions>
      Select the category that best describes the image.
    </short-instructions>
  </crowd-image-classifier>
</crowd-form>"""


def get_ui_template_arn(task_type: str, region: str = None) -> str:
    """Get AWS-provided UI template ARN for task type."""
    if not region:
        region = os.environ.get('AWS_REGION', 'us-east-1')
    
    templates = {
        'ObjectDetection': f"arn:aws:sagemaker:{region}:aws:labeling-job-template/BoundingBox",
        'Classification': f"arn:aws:sagemaker:{region}:aws:labeling-job-template/ImageMultiClass",
        'Segmentation': f"arn:aws:sagemaker:{region}:aws:labeling-job-template/SemanticSegmentation"
    }
    
    return templates.get(task_type, templates['ObjectDetection'])


def get_ui_template_s3_uri(task_type: str):
    """Get UI template S3 URI for task type.
    
    Returns None to use Ground Truth's built-in default templates.
    This avoids cross-account S3 access issues with AWS-managed templates.
    
    Ground Truth has built-in support for:
    - Image Classification (ImageMultiClass)
    - Bounding Box (BoundingBox)
    - Semantic Segmentation (SemanticSegmentation)
    
    These built-in templates don't require custom S3 URIs or cross-account access.
    """
    return None


def get_pre_human_task_lambda_arn(task_type: str, region: str = None) -> str:
    """Get pre-annotation Lambda ARN for task type."""
    if not region:
        region = os.environ.get('AWS_REGION', 'us-east-1')
    account = '432418664414'  # AWS-owned account for Ground Truth Lambdas
    
    lambdas = {
        'ObjectDetection': f"arn:aws:lambda:{region}:{account}:function:PRE-BoundingBox",
        'Classification': f"arn:aws:lambda:{region}:{account}:function:PRE-ImageMultiClass",
        'Segmentation': f"arn:aws:lambda:{region}:{account}:function:PRE-SemanticSegmentation"
    }
    
    return lambdas.get(task_type, lambdas['ObjectDetection'])


def get_annotation_consolidation_lambda_arn(task_type: str, region: str = None) -> str:
    """Get annotation consolidation Lambda ARN for task type."""
    if not region:
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
        
        logger.info(f"Found {len(workteams)} workteams for usecase {usecase_id}")
        
        return create_response(200, {
            'workteams': workteams,
            'count': len(workteams)
        })
        
    except Exception as e:
        logger.error(f"Error listing workteams: {str(e)}", exc_info=True)
        return handle_error(e, 'Failed to list workteams')



