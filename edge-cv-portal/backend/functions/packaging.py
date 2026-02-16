"""
Model packaging Lambda functions
Implements component packaging for Greengrass deployment
Based on DDA_Greengrass_Component_Creator.ipynb Phases 1-2

Simplified version that uses basic Python file operations instead of
complex storage management utilities.

Version: 2.0.0 - Simplified packaging without storage management utilities
"""
import json
import os
import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
import uuid
import tarfile
import tempfile
import shutil
import zipfile
from urllib.parse import urlparse
import yaml
import re

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, validate_required_fields,
    is_cross_account_setup, get_usecase_client, assume_usecase_role, get_usecase
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
sts = boto3.client('sts')
s3 = boto3.client('s3')

# Environment variables
TRAINING_JOBS_TABLE = os.environ.get('TRAINING_JOBS_TABLE')
USECASES_TABLE = os.environ.get('USECASES_TABLE')


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


def create_dda_manifest(trained_model_s3: str, model_type: str, s3_client) -> Tuple[Dict, str]:
    """
    Phase 1: Model Artifact Preparation
    Download trained model, extract config and manifest, create DDA-compatible manifest
    
    Args:
        trained_model_s3: S3 URI of the trained model
        model_type: Type of model (classification, segmentation, etc.)
        s3_client: boto3 S3 client (already configured with appropriate credentials)
    
    Returns:
        (dda_manifest_dict, model_filename)
    """
    temp_dir = None
    try:
        # Parse S3 URI
        parsed = urlparse(trained_model_s3)
        bucket = parsed.netloc
        key = parsed.path.lstrip('/')
        
        # Use the provided S3 client
        s3_usecase = s3_client
        
        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="dda_manifest_")
        
        # Download trained model artifact
        local_tar = os.path.join(temp_dir, 'model.tar.gz')
        logger.info(f"Downloading trained model from {trained_model_s3}")
        s3_usecase.download_file(bucket, key, local_tar)
        
        # Extract tar.gz
        extract_dir = os.path.join(temp_dir, 'extracted')
        os.makedirs(extract_dir, exist_ok=True)
        
        logger.info("Extracting trained model archive")
        with tarfile.open(local_tar, 'r:gz') as tar:
            tar.extractall(extract_dir)
        
        # Clean up the downloaded tar file
        os.remove(local_tar)
        
        # Read config.yaml for image dimensions
        config_path = os.path.join(extract_dir, 'config.yaml')
        if not os.path.exists(config_path):
            raise FileNotFoundError("config.yaml not found in trained model")
        
        with open(config_path, 'r') as f:
            config_data = yaml.safe_load(f)
        
        image_width = config_data['dataset']['image_width']
        image_height = config_data['dataset']['image_height']
        logger.info(f"Image dimensions: {image_width}x{image_height}")
        
        # Read export_artifacts/manifest.json
        manifest_path = os.path.join(extract_dir, 'export_artifacts', 'manifest.json')
        if not os.path.exists(manifest_path):
            raise FileNotFoundError("export_artifacts/manifest.json not found")
        
        with open(manifest_path, 'r') as f:
            original_manifest = json.load(f)
        
        # Find .pt model file
        export_artifacts_dir = os.path.join(extract_dir, 'export_artifacts')
        pt_file = None
        for file in os.listdir(export_artifacts_dir):
            if file.endswith('.pt'):
                pt_file = file
                break
        
        if not pt_file:
            raise FileNotFoundError("No .pt model file found in export_artifacts")
        
        logger.info(f"Found model file: {pt_file}")
        
        # Extract input_shape from original manifest
        input_shape = original_manifest.get('input_shape')
        if not input_shape:
            # Try to get from model_graph
            input_shape = original_manifest.get('model_graph', {}).get('stages', [{}])[0].get('input_shape')
        
        if not input_shape:
            raise ValueError("Could not find input_shape in manifest")
        
        # Create DDA-compatible manifest
        dda_manifest = {
            'model_graph': original_manifest['model_graph'],
            'compilable_models': [{
                'filename': pt_file,
                'data_input_config': {
                    'input': input_shape
                },
                'framework': 'PYTORCH'
            }],
            'dataset': {
                'image_width': image_width,
                'image_height': image_height
            }
        }
        
        logger.info("Created DDA-compatible manifest")
        
        return dda_manifest, pt_file
        
    except Exception as e:
        logger.error(f"Error creating DDA manifest: {str(e)}")
        raise
    finally:
        # Cleanup temp directory
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def package_component(training_id: str, target: str, compiled_model_s3: str, 
                     dda_manifest: Dict, s3_client, usecase: Dict) -> str:
    """
    Phase 2: Directory Structure Setup
    Download compiled model, organize directory structure, package as ZIP, upload to S3
    
    Args:
        training_id: Training job ID
        target: Compilation target (e.g., 'jetson-xavier')
        compiled_model_s3: S3 URI of the compiled model
        dda_manifest: DDA-compatible manifest dict
        s3_client: boto3 S3 client (already configured with appropriate credentials)
        usecase: Use case details dict
    
    Returns:
        S3 URI of packaged component
    """
    temp_dir = None
    try:
        # Parse compiled model S3 URI
        parsed = urlparse(compiled_model_s3)
        bucket = parsed.netloc
        key = parsed.path.lstrip('/')
        
        # Use the provided S3 client
        s3_usecase = s3_client
        
        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix=f"dda_package_{target}_")
        
        # Download compiled model artifact
        compiled_tar = os.path.join(temp_dir, 'compiled_model.tar.gz')
        logger.info(f"Downloading compiled model from {compiled_model_s3}")
        s3_usecase.download_file(bucket, key, compiled_tar)
        
        # Create directory structure
        model_artifacts_dir = os.path.join(temp_dir, 'model_artifacts')
        os.makedirs(model_artifacts_dir, exist_ok=True)
        
        # Determine model name from manifest
        model_name = dda_manifest['model_graph']['stages'][0]['type']
        compiled_model_dir = os.path.join(model_artifacts_dir, model_name)
        os.makedirs(compiled_model_dir, exist_ok=True)
        
        # Extract compiled model
        logger.info(f"Extracting compiled model for {target}")
        with tarfile.open(compiled_tar, 'r:gz') as tar:
            tar.extractall(compiled_model_dir)
        
        # Clean up the downloaded tar file
        os.remove(compiled_tar)
        
        # Write DDA manifest to model_artifacts/manifest.json
        manifest_path = os.path.join(model_artifacts_dir, 'manifest.json')
        with open(manifest_path, 'w') as f:
            json.dump(dda_manifest, f, indent=2)
        
        logger.info("Wrote DDA manifest to model_artifacts/manifest.json")
        
        # Generate unique UUID for component
        component_uuid = str(uuid.uuid4()).split('-')[-1]
        zip_filename = f"{component_uuid}_greengrass_model_component.zip"
        
        # Create ZIP archive
        zip_path = os.path.join(temp_dir, zip_filename)
        logger.info(f"Creating ZIP archive: {zip_filename}")
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(model_artifacts_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, model_artifacts_dir)
                    zipf.write(file_path, arcname)
        
        # Upload ZIP to S3
        s3_key = f"model_artifacts/model-{component_uuid}/{zip_filename}"
        s3_uri = f"s3://{usecase['s3_bucket']}/{s3_key}"
        
        logger.info(f"Uploading component package to {s3_uri}")
        s3_usecase.upload_file(zip_path, usecase['s3_bucket'], s3_key)
        
        logger.info(f"Component package uploaded successfully")
        
        return s3_uri
        
    except Exception as e:
        logger.error(f"Error packaging component: {str(e)}")
        raise
    finally:
        # Cleanup temp directory
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def package_components(event: Dict, context: Any) -> Dict:
    """
    Package compiled models as Greengrass components
    POST /api/v1/training/{training_id}/package
    
    Request body:
    {
        "targets": ["jetson-xavier", "x86_64-cpu"]  // Optional, defaults to all compiled targets
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
        requested_targets = body.get('targets')
        
        # Get training job details
        training_job = get_training_job_details(training_id)
        usecase_id = training_job['usecase_id']
        
        # Check user access (DataScientist role required)
        # Allow 'system' user for auto-triggered packaging
        if user_id != 'system' and not check_user_access(user_id, usecase_id, 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Check compilation jobs exist
        compilation_jobs = training_job.get('compilation_jobs', [])
        if not compilation_jobs:
            return create_response(400, {'error': 'No compilation jobs found. Run compilation first.'})
        
        # Filter to completed compilation jobs (case-insensitive check)
        completed_jobs = [j for j in compilation_jobs if j.get('status', '').upper() == 'COMPLETED']
        if not completed_jobs:
            return create_response(400, {'error': 'No completed compilation jobs found'})
        
        # Filter by requested targets if specified
        if requested_targets:
            completed_jobs = [j for j in completed_jobs if j['target'] in requested_targets]
            if not completed_jobs:
                return create_response(400, {
                    'error': f"No completed compilation jobs for requested targets: {requested_targets}"
                })
        
        # Get use case details
        usecase = get_usecase(usecase_id)
        
        # Create S3 client (handles both single-account and multi-account scenarios)
        s3_usecase = get_usecase_client(
            's3',
            usecase,
            session_name=f"pkg-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        )
        
        # Phase 1: Create DDA manifest (once for all targets)
        trained_model_s3 = training_job.get('artifact_s3')
        if not trained_model_s3:
            return create_response(400, {'error': 'Training job has no model artifact'})
        
        logger.info("Phase 1: Creating DDA-compatible manifest")
        dda_manifest, model_filename = create_dda_manifest(
            trained_model_s3,
            training_job.get('model_type', 'unknown'),
            s3_usecase
        )
        
        # Phase 2: Package each compiled model
        packaged_components = []
        
        for i, job in enumerate(completed_jobs):
            target = job['target']
            compiled_model_s3 = job.get('compiled_model_s3')
            
            if not compiled_model_s3:
                logger.warning(f"No compiled model S3 URI for target {target}, skipping")
                continue
            
            logger.info(f"Phase 2: Packaging component for target {target} ({i+1}/{len(completed_jobs)})")
            
            try:
                component_s3_uri = package_component(
                    training_id,
                    target,
                    compiled_model_s3,
                    dda_manifest,
                    s3_usecase,
                    usecase
                )
                
                packaged_components.append({
                    'target': target,
                    'component_package_s3': component_s3_uri,
                    'status': 'packaged'
                })
                
                logger.info(f"Successfully packaged component for {target}")
                
            except Exception as e:
                logger.error(f"Error packaging component for {target}: {str(e)}")
                packaged_components.append({
                    'target': target,
                    'status': 'failed',
                    'error': str(e)
                })
        
        # Update training job with packaged components
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        table.update_item(
            Key={'training_id': training_id},
            UpdateExpression='SET packaged_components = :components, updated_at = :updated',
            ExpressionAttributeValues={
                ':components': packaged_components,
                ':updated': timestamp
            }
        )
        
        # Log audit event
        log_audit_event(
            user_id=user_id,
            action='package_components',
            resource_type='training_job',
            resource_id=training_id,
            result='success',
            details={
                'targets': [c['target'] for c in packaged_components],
                'packaged_count': len([c for c in packaged_components if c['status'] == 'packaged'])
            }
        )
        
        success_count = len([c for c in packaged_components if c['status'] == 'packaged'])
        failed_count = len([c for c in packaged_components if c['status'] == 'failed'])
        
        logger.info(f"Packaging completed. Results: {success_count} successful, {failed_count} failed")
        
        # Trigger automatic component creation if packaging was successful and auto-triggered
        auto_triggered = body.get('auto_triggered', False)
        if success_count > 0 and auto_triggered:
            try:
                logger.info(f"Packaging completed successfully, triggering automatic component creation")
                
                # Trigger component creation by invoking the greengrass_publish Lambda
                lambda_client = boto3.client('lambda')
                greengrass_function_name = os.environ.get('GREENGRASS_PUBLISH_FUNCTION_NAME')
                
                if greengrass_function_name:
                    # Generate a component name and version based on the training job
                    model_name = training_job.get('model_name', 'model')
                    safe_model_name = re.sub(r'[^a-zA-Z0-9-]', '-', model_name.lower())
                    component_name = f"model-{safe_model_name}"
                    component_version = "1.0.0"  # Default version
                    
                    # Create a mock API Gateway event for the greengrass publish handler
                    greengrass_event = {
                        'httpMethod': 'POST',
                        'path': f'/api/v1/training/{training_id}/publish',
                        'pathParameters': {'id': training_id},
                        'body': json.dumps({
                            'component_name': component_name,
                            'component_version': component_version,
                            'friendly_name': model_name,
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
                    
                    # Invoke greengrass publish Lambda asynchronously
                    lambda_client.invoke(
                        FunctionName=greengrass_function_name,
                        InvocationType='Event',  # Async invocation
                        Payload=json.dumps(greengrass_event)
                    )
                    
                    logger.info(f"Triggered automatic component creation for training job {training_id}")
                else:
                    logger.warning("GREENGRASS_PUBLISH_FUNCTION_NAME not set, skipping automatic component creation")
                    
            except Exception as e:
                logger.error(f"Error triggering automatic component creation: {str(e)}")
                # Don't fail the packaging response if component creation trigger fails
        
        return create_response(200, {
            'training_id': training_id,
            'packaged_components': packaged_components,
            'message': f'Packaged {success_count} component(s) successfully',
            'component_creation_triggered': success_count > 0 and auto_triggered
        })
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return create_response(400, {'error': str(e)})
    except ClientError as e:
        logger.error(f"AWS error packaging components: {str(e)}")
        return create_response(500, {'error': f"Failed to package components: {str(e)}"})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler"""
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
        if http_method == 'POST' and '/package' in path:
            return package_components(event, context)
        else:
            return create_response(404, {'error': 'Not found'})
            
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})
