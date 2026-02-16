"""
Model compilation Lambda functions
Implements SageMaker Neo compilation job submission and monitoring
Based on DDA_SageMaker_Model_Training_and_Compilation.ipynb Step 8
"""
import json
import os
import logging
from typing import Dict, Any, Optional
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
import uuid
import tarfile
import tempfile
from urllib.parse import urlparse

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, validate_required_fields, create_s3_path_builder,
    is_cross_account_setup, get_usecase_client, assume_usecase_role, get_usecase
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
SETTINGS_TABLE = os.environ.get('SETTINGS_TABLE')

# Compilation target configurations
COMPILATION_TARGETS = {
    'jetson-xavier': {
        'os': 'LINUX',
        'arch': 'ARM64',
        'accelerator': 'NVIDIA',
        'compiler_options': json.dumps({
            'cuda-ver': '10.2',
            'gpu-code': 'sm_72',
            'trt-ver': '8.2.1',
            'max-workspace-size': '2147483648',
            'precision-mode': 'fp16',
            'jetson-platform': 'xavier'
        })
    },
    'x86_64-cpu': {
        'os': 'LINUX',
        'arch': 'X86_64',
        'accelerator': None,
        'compiler_options': None
    },
    'x86_64-cuda': {
        'os': 'LINUX',
        'arch': 'X86_64',
        'accelerator': 'NVIDIA',
        'compiler_options': json.dumps({
            'cuda-ver': '10.2',
            'gpu-code': 'sm_75',
            'trt-ver': '8.2.1',
            'max-workspace-size': '2147483648',
            'precision-mode': 'fp16'
        })
    },
    'arm64-cpu': {
        'os': 'LINUX',
        'arch': 'ARM64',
        'accelerator': None,
        'compiler_options': None
    }
}


def get_training_job_details(training_id: str) -> Dict:
    """Get training job details from DynamoDB"""
    try:
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        response = table.get_item(Key={'training_id': training_id})
        
        if 'Item' not in response:
            raise ValueError(f"Training job {training_id} not found")
        
        return response['Item']
    except Exception as e:
        logger.error(f"Error getting training job details: {str(e)}")
        raise


def extract_and_repackage_model(model_s3_uri: str, s3_client) -> tuple:
    """
    Extract mochi.pt from trained model and get input shape from mochi.json
    
    Args:
        model_s3_uri: S3 URI of the trained model artifact
        s3_client: boto3 S3 client (already configured with appropriate credentials)
    
    Returns:
        (repackaged_model_s3_uri, data_input_config)
    """
    try:
        # Parse S3 URI
        parsed = urlparse(model_s3_uri)
        bucket = parsed.netloc
        key = parsed.path.lstrip('/')
        
        # Use the provided S3 client
        s3_usecase = s3_client
        
        # Download model artifact to temp directory
        with tempfile.TemporaryDirectory() as temp_dir:
            local_tar = os.path.join(temp_dir, 'model.tar.gz')
            logger.info(f"Downloading model from {model_s3_uri}")
            s3_usecase.download_file(bucket, key, local_tar)
            
            # Extract tar.gz
            extract_dir = os.path.join(temp_dir, 'extracted')
            os.makedirs(extract_dir, exist_ok=True)
            
            with tarfile.open(local_tar, 'r:gz') as tar:
                tar.extractall(path=extract_dir)
            
            # Find mochi.pt file
            mochi_pt = None
            for root, dirs, files in os.walk(extract_dir):
                for file in files:
                    if file.endswith('.pt'):
                        mochi_pt = os.path.join(root, file)
                        break
                if mochi_pt:
                    break
            
            if not mochi_pt:
                raise FileNotFoundError("No .pt model file found in trained model artifact")
            
            logger.info(f"Found model file: {mochi_pt}")
            
            # Read mochi.json for input shape
            mochi_json_path = os.path.join(extract_dir, 'mochi.json')
            if not os.path.exists(mochi_json_path):
                raise FileNotFoundError("mochi.json not found in model artifact")
            
            with open(mochi_json_path, 'r') as f:
                mochi_data = json.load(f)
            
            input_shape = mochi_data['stages'][0]['input_shape']
            logger.info(f"Extracted input_shape: {input_shape}")
            
            # Build DataInputConfig
            data_input_config = json.dumps({'input_shape': input_shape})
            
            # Repackage just the .pt file
            repackaged_tar = os.path.join(temp_dir, 'model_for_compilation.tar.gz')
            with tarfile.open(repackaged_tar, 'w:gz') as tar:
                tar.add(mochi_pt, arcname=os.path.basename(mochi_pt))
            
            # Upload repackaged model
            repackaged_key = key.rsplit('/', 1)[0] + '/model_for_compilation.tar.gz'
            logger.info(f"Uploading repackaged model to s3://{bucket}/{repackaged_key}")
            s3_usecase.upload_file(repackaged_tar, bucket, repackaged_key)
            
            repackaged_s3_uri = f"s3://{bucket}/{repackaged_key}"
            
            return repackaged_s3_uri, data_input_config
            
    except Exception as e:
        logger.error(f"Error extracting and repackaging model: {str(e)}")
        raise


def start_compilation_job(event: Dict, context: Any) -> Dict:
    """
    Start SageMaker Neo compilation job
    POST /api/v1/training/{training_id}/compile
    
    Request body:
    {
        "targets": ["jetson-xavier", "x86_64-cpu", "x86_64-cuda", "arm64-cpu"],
        "auto_triggered": true  # Optional flag for auto-triggered compilations
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get path parameters
        training_id = event.get('pathParameters', {}).get('id')
        if not training_id:
            return create_response(400, {'error': 'training_id is required'})
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        targets = body.get('targets', ['x86_64-cpu'])
        auto_triggered = body.get('auto_triggered', False)
        
        # Validate targets
        invalid_targets = [t for t in targets if t not in COMPILATION_TARGETS]
        if invalid_targets:
            return create_response(400, {
                'error': f"Invalid targets: {', '.join(invalid_targets)}. Valid targets: {', '.join(COMPILATION_TARGETS.keys())}"
            })
        
        # Get training job details
        training_job = get_training_job_details(training_id)
        usecase_id = training_job['usecase_id']
        
        # Check user access (DataScientist role required) - skip for auto-triggered compilations
        if not auto_triggered and not check_user_access(user_id, usecase_id, 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Check training job status
        if training_job.get('status') != 'Completed':
            return create_response(400, {
                'error': f"Training job must be completed. Current status: {training_job.get('status')}"
            })
        
        # Get use case details
        usecase = get_usecase(usecase_id)
        
        # Create SageMaker and S3 clients (handles both single-account and multi-account scenarios)
        sagemaker_usecase = get_usecase_client(
            'sagemaker',
            usecase,
            session_name=f"comp-{int(datetime.utcnow().timestamp())}"
        )
        s3_usecase = get_usecase_client(
            's3',
            usecase,
            session_name=f"comp-s3-{int(datetime.utcnow().timestamp())}"
        )
        
        # Extract and repackage model
        model_artifact_s3 = training_job.get('artifact_s3')
        if not model_artifact_s3:
            return create_response(400, {'error': 'Training job has no model artifact'})
        
        logger.info(f"Extracting and repackaging model from {model_artifact_s3}")
        repackaged_model_s3, data_input_config = extract_and_repackage_model(
            model_artifact_s3,
            s3_usecase
        )
        
        # Create S3 path builder for consistent path generation
        path_builder = create_s3_path_builder(
            bucket=usecase['s3_bucket']
        )
        
        # Start compilation jobs for each target
        compilation_jobs = []
        
        for target in targets:
            target_config = COMPILATION_TARGETS[target]
            
            # Generate unique compilation job name
            # SageMaker compilation job names must be 63 characters or less
            # and follow pattern: [a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}
            safe_model_name = training_job['model_name'].replace('.', '-').replace('_', '-')
            # Remove any consecutive hyphens and ensure it doesn't start/end with hyphen
            safe_model_name = '-'.join(filter(None, safe_model_name.split('-')))
            
            # Create safe target name for SageMaker job naming
            target_name_mapping = {
                'jetson-xavier': 'jetson',
                'x86_64-cpu': 'x86cpu',
                'x86_64-cuda': 'x86cuda',
                'arm64-cpu': 'arm64cpu'
            }
            safe_target = target_name_mapping.get(target, target.replace('_', '').replace('-', ''))
            
            timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')  # Remove hyphens from timestamp
            compilation_job_name = f"{safe_model_name}-{safe_target}-{timestamp}"
            
            # Ensure job name meets SageMaker regex pattern: [a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}
            # Remove any invalid characters and ensure no consecutive hyphens
            import re
            compilation_job_name = re.sub(r'[^a-zA-Z0-9-]', '', compilation_job_name)
            compilation_job_name = re.sub(r'-+', '-', compilation_job_name)  # Replace multiple hyphens with single
            compilation_job_name = compilation_job_name.strip('-')  # Remove leading/trailing hyphens
            
            # Validate compilation job name length (SageMaker limit is 63 characters)
            if len(compilation_job_name) > 63:
                # Truncate model name to fit within limit
                max_model_name_length = 63 - len(safe_target) - len(timestamp) - 2  # -2 for the hyphens
                if max_model_name_length < 1:
                    logger.error(f"Cannot create compilation job name for target '{target}' - name would be too long")
                    continue  # Skip this target
                
                truncated_model_name = safe_model_name[:max_model_name_length]
                compilation_job_name = f"{truncated_model_name}-{safe_target}-{timestamp}"
                logger.warning(f"Model name truncated from '{safe_model_name}' to '{truncated_model_name}' for compilation job")
            
            logger.info(f"Generated compilation job name: {compilation_job_name} (length: {len(compilation_job_name)})")
            
            # Generate S3 output path using new structure
            compilation_output_uri = path_builder.get_compilation_output_uri(compilation_job_name, target)
            logger.info(f"Compilation output for {target} will be stored at: {compilation_output_uri}")
            
            # Build output config
            output_config = {
                'S3OutputLocation': compilation_output_uri,
                'TargetPlatform': {
                    'Os': target_config['os'],
                    'Arch': target_config['arch']
                }
            }
            
            # Add accelerator if specified
            if target_config['accelerator']:
                output_config['TargetPlatform']['Accelerator'] = target_config['accelerator']
            
            # Add compiler options if specified
            if target_config['compiler_options']:
                output_config['CompilerOptions'] = target_config['compiler_options']
            
            logger.info(f"Starting compilation job: {compilation_job_name} for target: {target}")
            
            # Get SageMaker execution role ARN
            sagemaker_role_arn = f"arn:aws:iam::{usecase['account_id']}:role/DDASageMakerExecutionRole"
            logger.info(f"Using SageMaker role: {sagemaker_role_arn}")
            
            # Create compilation job (without tags to avoid permission issues)
            compilation_response = sagemaker_usecase.create_compilation_job(
                CompilationJobName=compilation_job_name,
                RoleArn=sagemaker_role_arn,
                InputConfig={
                    'S3Uri': repackaged_model_s3,
                    'DataInputConfig': data_input_config,
                    'Framework': 'PYTORCH',
                    'FrameworkVersion': '1.8'
                },
                OutputConfig=output_config,
                StoppingCondition={
                    'MaxRuntimeInSeconds': 3600  # 1 hour
                }
            )
            
            compilation_jobs.append({
                'target': target,
                'compilation_job_name': compilation_job_name,
                'compilation_job_arn': compilation_response['CompilationJobArn'],
                'status': 'InProgress'
            })
        
        # Update training job with compilation info
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        table.update_item(
            Key={'training_id': training_id},
            UpdateExpression='SET compilation_jobs = :jobs, updated_at = :updated',
            ExpressionAttributeValues={
                ':jobs': compilation_jobs,
                ':updated': timestamp
            }
        )
        
        # Log audit event
        log_audit_event(
            user_id=user_id if not auto_triggered else 'system',
            action='start_compilation',
            resource_type='training_job',
            resource_id=training_id,
            result='success',
            details={
                'targets': targets,
                'compilation_jobs': [j['compilation_job_name'] for j in compilation_jobs],
                'auto_triggered': auto_triggered
            }
        )
        
        logger.info(f"Started {len(compilation_jobs)} compilation jobs for training {training_id}")
        
        return create_response(200, {
            'training_id': training_id,
            'compilation_jobs': compilation_jobs,
            'message': f'Started compilation for {len(targets)} target(s)'
        })
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return create_response(400, {'error': str(e)})
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_message = e.response.get('Error', {}).get('Message', str(e))
        
        logger.error(f"AWS error starting compilation: {error_code} - {error_message}")
        
        # Provide user-friendly error messages for common SageMaker validation errors
        if 'ValidationException' in error_code:
            if 'Member must have length less than' in error_message:
                if 'CompilationJobName' in error_message:
                    return create_response(400, {
                        'error': f"Compilation job name is too long. Please use a shorter model name (maximum 30 characters recommended)."
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
            return create_response(500, {'error': f"Failed to start compilation: {error_message}"})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def get_compilation_status(event: Dict, context: Any) -> Dict:
    """
    Get compilation job status
    GET /api/v1/training/{training_id}/compile
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get path parameters
        training_id = event.get('pathParameters', {}).get('id')
        if not training_id:
            return create_response(400, {'error': 'training_id is required'})
        
        # Get training job details
        training_job = get_training_job_details(training_id)
        usecase_id = training_job['usecase_id']
        
        # Check user access
        if not check_user_access(user_id, usecase_id):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        compilation_jobs = training_job.get('compilation_jobs', [])
        
        if not compilation_jobs:
            return create_response(404, {'error': 'No compilation jobs found for this training'})
        
        # Get use case details for cross-account access
        usecase = get_usecase(usecase_id)
        
        # Create SageMaker client (handles both single-account and multi-account scenarios)
        sagemaker_usecase = get_usecase_client(
            'sagemaker',
            usecase,
            session_name=f"stat-{int(datetime.utcnow().timestamp())}"
        )
        
        # Update status for each compilation job
        updated_jobs = []
        for job in compilation_jobs:
            try:
                response = sagemaker_usecase.describe_compilation_job(
                    CompilationJobName=job['compilation_job_name']
                )
                
                job_status = response['CompilationJobStatus']
                job['status'] = job_status
                
                if job_status == 'COMPLETED':
                    job['compiled_model_s3'] = response['ModelArtifacts']['S3ModelArtifacts']
                elif job_status == 'FAILED':
                    job['failure_reason'] = response.get('FailureReason', 'Unknown')
                
                updated_jobs.append(job)
                
            except ClientError as e:
                logger.error(f"Error getting compilation status for {job['compilation_job_name']}: {str(e)}")
                job['status'] = 'ERROR'
                job['error'] = str(e)
                updated_jobs.append(job)
        
        # Update DynamoDB with latest status
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        table.update_item(
            Key={'training_id': training_id},
            UpdateExpression='SET compilation_jobs = :jobs, updated_at = :updated',
            ExpressionAttributeValues={
                ':jobs': updated_jobs,
                ':updated': timestamp
            }
        )
        
        return create_response(200, {
            'training_id': training_id,
            'compilation_jobs': updated_jobs
        })
        
    except Exception as e:
        logger.error(f"Error getting compilation status: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler - routes to appropriate function"""
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        
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
        if http_method == 'POST' and '/compile' in path:
            return start_compilation_job(event, context)
        elif http_method == 'GET' and '/compile' in path:
            return get_compilation_status(event, context)
        else:
            return create_response(404, {'error': 'Not found'})
            
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})
