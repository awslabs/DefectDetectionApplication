"""
Data Management Lambda for Edge CV Portal
Handles S3 bucket creation, folder management, and file uploads
Supports both same-account and cross-account data storage
"""
import json
import os
import logging
import re
from typing import Dict, Any, Optional, List
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, validate_required_fields, get_usecase
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
sts = boto3.client('sts')

# Environment variables
USECASES_TABLE = os.environ.get('USECASES_TABLE')


def get_data_account_credentials(usecase: Dict) -> Optional[Dict]:
    """
    Get credentials for the data account.
    If data_account_role_arn is set, assume that role with the required external ID.
    Otherwise, use the regular usecase account role.
    """
    # Check if separate data account is configured
    data_role_arn = usecase.get('data_account_role_arn')
    
    if data_role_arn:
        # Use separate data account role - external ID is required for production
        external_id = usecase.get('data_account_external_id')
        if not external_id:
            raise ValueError(
                "data_account_external_id is required when using a separate Data Account. "
                "Please update the UseCase configuration with the external ID."
            )
        session_name = f"data-mgmt-{int(datetime.utcnow().timestamp())}"[:64]
    else:
        # Use regular usecase account role (this one requires external_id)
        data_role_arn = usecase['cross_account_role_arn']
        external_id = usecase['external_id']
        session_name = f"data-mgmt-{int(datetime.utcnow().timestamp())}"[:64]
    
    try:
        # Build assume role params
        assume_params = {
            'RoleArn': data_role_arn,
            'RoleSessionName': session_name,
            'DurationSeconds': 3600
        }
        
        # Only include ExternalId if it's set (some roles don't require it)
        if external_id:
            assume_params['ExternalId'] = external_id
            
        response = sts.assume_role(**assume_params)
        return response['Credentials']
    except ClientError as e:
        logger.error(f"Error assuming role {data_role_arn}: {str(e)}")
        logger.error(f"External ID provided: {'Yes' if external_id else 'No'}")
        raise


def get_data_bucket(usecase: Dict) -> str:
    """Get the data bucket name from usecase config."""
    return usecase.get('data_s3_bucket') or usecase.get('s3_bucket')


def create_s3_client(credentials: Dict) -> boto3.client:
    """Create S3 client with assumed credentials."""
    return boto3.client(
        's3',
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken']
    )


def is_valid_bucket_name(name: str) -> bool:
    """Validate S3 bucket name according to AWS rules."""
    # Must be 3-63 characters
    if len(name) < 3 or len(name) > 63:
        return False
    
    # Must start and end with letter or number
    if not re.match(r'^[a-z0-9]', name) or not re.search(r'[a-z0-9]$', name):
        return False
    
    # Can only contain lowercase letters, numbers, hyphens, and periods
    if not re.match(r'^[a-z0-9][a-z0-9.\-]*[a-z0-9]$', name):
        return False
    
    # Cannot contain consecutive periods or hyphens
    if '..' in name or '--' in name:
        return False
    
    # Cannot be formatted as IP address
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', name):
        return False
    
    return True


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler for data management operations."""
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
        
        # Route requests
        if '/buckets' in path:
            if http_method == 'GET':
                return list_buckets(event)
            elif http_method == 'POST':
                return create_bucket(event)
        elif '/folders' in path:
            if http_method == 'GET':
                return list_folders(event)
            elif http_method == 'POST':
                return create_folder(event)
        elif '/upload-url' in path and http_method == 'POST':
            return get_upload_url(event)
        elif '/batch-upload-urls' in path and http_method == 'POST':
            return get_batch_upload_urls(event)
        elif '/configure' in path and http_method == 'POST':
            return configure_data_account(event)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})



def list_buckets(event: Dict) -> Dict:
    """
    List S3 buckets in the data account that have the dda-portal:managed tag.
    GET /api/v1/usecases/{usecase_id}/data/buckets
    
    Only returns buckets tagged with dda-portal:managed=true
    """
    try:
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get usecase_id from path
        path_params = event.get('pathParameters', {}) or {}
        usecase_id = path_params.get('id') or path_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Check access
        if not check_user_access(user_id, usecase_id, user_info=user):
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        
        # Determine which account we're querying
        # Priority: data_account_id (if separate data account) → account_id (usecase account)
        data_role_arn = usecase.get('data_account_role_arn')
        if data_role_arn:
            # Separate data account is configured
            target_account = usecase.get('data_account_id', data_role_arn.split(':')[4])
            logger.info(f"Querying Data Account {target_account} using data_account_role_arn")
        else:
            # No separate data account - use the usecase account
            target_account = usecase.get('account_id', 'unknown')
            logger.info(f"No data_account_role_arn configured, using UseCase Account {target_account}")
        
        # Get credentials and create resource tagging client
        credentials = get_data_account_credentials(usecase)
        
        # Use Resource Groups Tagging API to find tagged buckets
        tagging_client = boto3.client(
            'resourcegroupstaggingapi',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        s3 = create_s3_client(credentials)
        
        # Find buckets with dda-portal:managed tag
        buckets = []
        paginator = tagging_client.get_paginator('get_resources')
        
        for page in paginator.paginate(
            TagFilters=[
                {
                    'Key': 'dda-portal:managed',
                    'Values': ['true']
                }
            ],
            ResourceTypeFilters=['s3:bucket']
        ):
            for resource in page.get('ResourceTagMappingList', []):
                # ARN format: arn:aws:s3:::bucket-name
                arn = resource['ResourceARN']
                bucket_name = arn.split(':::')[-1]
                
                try:
                    # Get bucket location
                    location = s3.get_bucket_location(Bucket=bucket_name)
                    region = location.get('LocationConstraint') or 'us-east-1'
                    
                    # Get bucket tags for additional info
                    tags = {tag['Key']: tag['Value'] for tag in resource.get('Tags', [])}
                    
                    buckets.append({
                        'name': bucket_name,
                        'region': region,
                        'tags': tags
                    })
                except ClientError as e:
                    logger.warning(f"Could not get details for bucket {bucket_name}: {str(e)}")
                    # Still include the bucket but with limited info
                    buckets.append({
                        'name': bucket_name,
                        'region': 'unknown',
                        'tags': {tag['Key']: tag['Value'] for tag in resource.get('Tags', [])}
                    })
        
        # Also include the configured data bucket if it exists
        current_bucket = get_data_bucket(usecase)
        if current_bucket:
            # Check if it's already in the list
            bucket_names = [b['name'] for b in buckets]
            if current_bucket not in bucket_names:
                try:
                    location = s3.get_bucket_location(Bucket=current_bucket)
                    region = location.get('LocationConstraint') or 'us-east-1'
                    buckets.insert(0, {
                        'name': current_bucket,
                        'region': region,
                        'tags': {},
                        'is_configured': True
                    })
                except ClientError:
                    pass
        
        log_audit_event(
            user_id, 'list_buckets', 'data_management', usecase_id,
            'success', {'bucket_count': len(buckets), 'target_account': target_account}
        )
        
        return create_response(200, {
            'buckets': buckets,
            'current_data_bucket': current_bucket,
            'target_account': target_account,
            'has_data_account_role': bool(data_role_arn),
            'message': f"Querying account {target_account}. Only showing buckets tagged with dda-portal:managed=true"
        })
        
    except ClientError as e:
        logger.error(f"AWS error listing buckets: {str(e)}")
        return create_response(500, {'error': f"Failed to list buckets: {str(e)}"})
    except Exception as e:
        logger.error(f"Error listing buckets: {str(e)}")
        return create_response(500, {'error': 'Failed to list buckets'})



def create_bucket(event: Dict) -> Dict:
    """
    Create a new S3 bucket in the data account.
    POST /api/v1/usecases/{usecase_id}/data/buckets
    
    NOTE: This endpoint is disabled. Buckets must be created before UseCase onboarding
    and tagged with dda-portal:managed=true to be visible in the portal.
    """
    return create_response(400, {
        'error': 'Bucket creation via portal is not supported. Please create the bucket in AWS Console before onboarding the UseCase, then tag it with dda-portal:managed=true.',
        'instructions': [
            '1. Create bucket in AWS Console: aws s3 mb s3://your-bucket-name',
            '2. Tag the bucket: aws s3api put-bucket-tagging --bucket your-bucket-name --tagging \'TagSet=[{Key=dda-portal:managed,Value=true}]\'',
            '3. The bucket will appear in the portal after tagging'
        ]
    })


def list_folders(event: Dict) -> Dict:
    """
    List folders and files in a bucket prefix.
    GET /api/v1/usecases/{usecase_id}/data/folders?bucket={bucket}&prefix={prefix}
    """
    try:
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get usecase_id from path
        path_params = event.get('pathParameters', {}) or {}
        usecase_id = path_params.get('id') or path_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Check access
        if not check_user_access(user_id, usecase_id, user_info=user):
            return create_response(403, {'error': 'Access denied'})
        
        # Get query parameters
        query_params = event.get('queryStringParameters', {}) or {}
        bucket = query_params.get('bucket')
        prefix = query_params.get('prefix', '')
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        
        # Use default bucket if not specified
        if not bucket:
            bucket = get_data_bucket(usecase)
        
        if not bucket:
            return create_response(400, {'error': 'bucket is required'})
        
        # Ensure prefix ends with / if not empty
        if prefix and not prefix.endswith('/'):
            prefix += '/'
        
        # Get credentials and create S3 client
        credentials = get_data_account_credentials(usecase)
        s3 = create_s3_client(credentials)
        
        # List objects with delimiter to get folders
        response = s3.list_objects_v2(
            Bucket=bucket,
            Prefix=prefix,
            Delimiter='/'
        )
        
        folders = []
        files = []
        
        # Get folders (common prefixes)
        for cp in response.get('CommonPrefixes', []):
            folder_path = cp['Prefix']
            folder_name = folder_path[len(prefix):].rstrip('/')
            folders.append({
                'name': folder_name,
                'path': folder_path
            })
        
        # Get files (contents)
        for obj in response.get('Contents', []):
            key = obj['Key']
            # Skip the prefix itself (folder marker)
            if key == prefix:
                continue
            
            filename = key[len(prefix):]
            # Skip if it's a subfolder marker
            if '/' in filename:
                continue
            
            files.append({
                'name': filename,
                'key': key,
                'size': obj['Size'],
                'last_modified': obj['LastModified'].isoformat()
            })
        
        return create_response(200, {
            'bucket': bucket,
            'prefix': prefix,
            'folders': folders,
            'files': files,
            'is_truncated': response.get('IsTruncated', False)
        })
        
    except ClientError as e:
        logger.error(f"AWS error listing folders: {str(e)}")
        return create_response(500, {'error': f"Failed to list folders: {str(e)}"})
    except Exception as e:
        logger.error(f"Error listing folders: {str(e)}")
        return create_response(500, {'error': 'Failed to list folders'})



def create_folder(event: Dict) -> Dict:
    """
    Create a new folder in the bucket.
    POST /api/v1/usecases/{usecase_id}/data/folders
    """
    try:
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get usecase_id from path
        path_params = event.get('pathParameters', {}) or {}
        usecase_id = path_params.get('id') or path_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Check access
        if not check_user_access(user_id, usecase_id, 'DataScientist', user_info=user):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        bucket = body.get('bucket')
        folder_path = body.get('folder_path')
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        
        # Use default bucket if not specified
        if not bucket:
            bucket = get_data_bucket(usecase)
        
        if not bucket or not folder_path:
            return create_response(400, {'error': 'bucket and folder_path are required'})
        
        # Ensure folder path ends with /
        if not folder_path.endswith('/'):
            folder_path += '/'
        
        # Get credentials and create S3 client
        credentials = get_data_account_credentials(usecase)
        s3 = create_s3_client(credentials)
        
        # Create folder (empty object with trailing /)
        s3.put_object(Bucket=bucket, Key=folder_path, Body='')
        
        log_audit_event(
            user_id, 'create_folder', 'data_management', usecase_id,
            'success', {'bucket': bucket, 'folder_path': folder_path}
        )
        
        return create_response(201, {
            'bucket': bucket,
            'folder_path': folder_path,
            'created': True
        })
        
    except ClientError as e:
        logger.error(f"AWS error creating folder: {str(e)}")
        return create_response(500, {'error': f"Failed to create folder: {str(e)}"})
    except Exception as e:
        logger.error(f"Error creating folder: {str(e)}")
        return create_response(500, {'error': 'Failed to create folder'})



def get_upload_url(event: Dict) -> Dict:
    """
    Get a presigned URL for uploading a file.
    POST /api/v1/usecases/{usecase_id}/data/upload-url
    """
    try:
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get usecase_id from path
        path_params = event.get('pathParameters', {}) or {}
        usecase_id = path_params.get('id') or path_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Check access
        if not check_user_access(user_id, usecase_id, 'DataScientist', user_info=user):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        bucket = body.get('bucket')
        key = body.get('key')
        content_type = body.get('content_type', 'application/octet-stream')
        expires_in = body.get('expires_in', 3600)  # Default 1 hour
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        
        # Use default bucket if not specified
        if not bucket:
            bucket = get_data_bucket(usecase)
        
        if not bucket or not key:
            return create_response(400, {'error': 'bucket and key are required'})
        
        # Get credentials and create S3 client
        credentials = get_data_account_credentials(usecase)
        s3 = create_s3_client(credentials)
        
        # Generate presigned URL for PUT
        upload_url = s3.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': bucket,
                'Key': key,
                'ContentType': content_type
            },
            ExpiresIn=expires_in
        )
        
        return create_response(200, {
            'upload_url': upload_url,
            'bucket': bucket,
            'key': key,
            'expires_in': expires_in
        })
        
    except ClientError as e:
        logger.error(f"AWS error generating upload URL: {str(e)}")
        return create_response(500, {'error': f"Failed to generate upload URL: {str(e)}"})
    except Exception as e:
        logger.error(f"Error generating upload URL: {str(e)}")
        return create_response(500, {'error': 'Failed to generate upload URL'})



def get_batch_upload_urls(event: Dict) -> Dict:
    """
    Get presigned URLs for uploading multiple files.
    POST /api/v1/usecases/{usecase_id}/data/batch-upload-urls
    """
    try:
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get usecase_id from path
        path_params = event.get('pathParameters', {}) or {}
        usecase_id = path_params.get('id') or path_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Check access
        if not check_user_access(user_id, usecase_id, 'DataScientist', user_info=user):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        bucket = body.get('bucket')
        prefix = body.get('prefix', '')
        files = body.get('files', [])
        expires_in = body.get('expires_in', 3600)
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        
        # Use default bucket if not specified
        if not bucket:
            bucket = get_data_bucket(usecase)
        
        if not bucket or not files:
            return create_response(400, {'error': 'bucket and files are required'})
        
        # Limit batch size
        if len(files) > 100:
            return create_response(400, {'error': 'Maximum 100 files per batch'})
        
        # Ensure prefix ends with / if not empty
        if prefix and not prefix.endswith('/'):
            prefix += '/'
        
        # Get credentials and create S3 client
        credentials = get_data_account_credentials(usecase)
        s3 = create_s3_client(credentials)
        
        # Generate presigned URLs for each file
        uploads = []
        for file_info in files:
            filename = file_info.get('filename')
            content_type = file_info.get('content_type', 'application/octet-stream')
            
            if not filename:
                continue
            
            key = f"{prefix}{filename}"
            
            try:
                upload_url = s3.generate_presigned_url(
                    'put_object',
                    Params={
                        'Bucket': bucket,
                        'Key': key,
                        'ContentType': content_type
                    },
                    ExpiresIn=expires_in
                )
                
                uploads.append({
                    'filename': filename,
                    'key': key,
                    'upload_url': upload_url,
                    'content_type': content_type
                })
            except Exception as e:
                logger.warning(f"Failed to generate URL for {filename}: {str(e)}")
                uploads.append({
                    'filename': filename,
                    'error': str(e)
                })
        
        log_audit_event(
            user_id, 'batch_upload_urls', 'data_management', usecase_id,
            'success', {'bucket': bucket, 'file_count': len(uploads)}
        )
        
        return create_response(200, {
            'bucket': bucket,
            'prefix': prefix,
            'uploads': uploads,
            'expires_in': expires_in
        })
        
    except ClientError as e:
        logger.error(f"AWS error generating batch upload URLs: {str(e)}")
        return create_response(500, {'error': f"Failed to generate upload URLs: {str(e)}"})
    except Exception as e:
        logger.error(f"Error generating batch upload URLs: {str(e)}")
        return create_response(500, {'error': 'Failed to generate upload URLs'})



def configure_data_account(event: Dict) -> Dict:
    """
    Configure data account settings for a usecase.
    POST /api/v1/usecases/{usecase_id}/data/configure
    """
    try:
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get usecase_id from path
        path_params = event.get('pathParameters', {}) or {}
        usecase_id = path_params.get('id') or path_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Check access (require UseCaseAdmin or higher)
        if not check_user_access(user_id, usecase_id, 'UseCaseAdmin', user_info=user):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        # Update usecase with data account settings
        table = dynamodb.Table(USECASES_TABLE)
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        update_expr = "SET updated_at = :updated"
        expr_values = {':updated': timestamp}
        
        # Optional data account fields
        data_fields = [
            'data_account_id',
            'data_account_role_arn', 
            'data_account_external_id',
            'data_s3_bucket',
            'data_s3_prefix'
        ]
        
        for field in data_fields:
            if field in body:
                update_expr += f", {field} = :{field}"
                expr_values[f":{field}"] = body[field]
        
        table.update_item(
            Key={'usecase_id': usecase_id},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values
        )
        
        log_audit_event(
            user_id, 'configure_data_account', 'data_management', usecase_id,
            'success', {'updated_fields': list(body.keys())}
        )
        
        return create_response(200, {
            'message': 'Data account configuration updated',
            'usecase_id': usecase_id
        })
        
    except ClientError as e:
        logger.error(f"AWS error configuring data account: {str(e)}")
        return create_response(500, {'error': f"Failed to configure data account: {str(e)}"})
    except Exception as e:
        logger.error(f"Error configuring data account: {str(e)}")
        return create_response(500, {'error': 'Failed to configure data account'})
