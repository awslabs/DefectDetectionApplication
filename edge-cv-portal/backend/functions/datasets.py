"""
Lambda function for dataset management operations.
Handles S3 dataset listing, manifest discovery, and validation.
"""

import json
import boto3
import os
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

s3_client = boto3.client('s3')


def handler(event, context):
    """Main Lambda handler for dataset operations."""
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
        
        if 'datasets' in path:
            if http_method == 'GET':
                if 'preview' in path:
                    return get_image_preview(event)
                else:
                    return list_datasets(event)
            elif http_method == 'POST':
                return count_images(event)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        return handle_error(e, 'Dataset operation failed')


def get_data_bucket_and_credentials(usecase):
    """
    Get the appropriate bucket and credentials for data access.
    Uses Data Account if configured, otherwise falls back to UseCase Account.
    """
    # Check if separate data account is configured
    data_role_arn = usecase.get('data_account_role_arn')
    
    if data_role_arn:
        # Use Data Account - external ID is required for production
        external_id = usecase.get('data_account_external_id')
        if not external_id:
            raise ValueError(
                "data_account_external_id is required when using a separate Data Account. "
                "Please update the UseCase configuration with the external ID."
            )
        credentials = assume_usecase_role(
            data_role_arn,
            external_id,
            'data-access'
        )
        bucket = usecase.get('data_s3_bucket') or usecase.get('s3_bucket')
        prefix = usecase.get('data_s3_prefix') or usecase.get('s3_prefix', '')
    else:
        # Use UseCase Account (this one requires external_id)
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            'data-access'
        )
        bucket = usecase['s3_bucket']
        prefix = usecase.get('s3_prefix', '')
    
    return bucket, prefix, credentials


def list_datasets(event):
    """
    List available S3 prefixes (datasets) in a UseCase Account.
    
    Query Parameters:
        - usecase_id: Required. The use case ID
        - prefix: Optional. Filter by prefix
        - max_depth: Optional. Maximum directory depth (default: 3)
    
    Returns:
        List of datasets with prefix, image count, and last modified date
    """
    try:
        params = event.get('queryStringParameters', {}) or {}
        usecase_id = params.get('usecase_id')
        filter_prefix = params.get('prefix', '')
        max_depth = int(params.get('max_depth', '3'))
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Get use case details
        usecase = get_usecase(usecase_id)
        
        # Get bucket and credentials (uses Data Account if configured)
        bucket, base_prefix, credentials = get_data_bucket_and_credentials(usecase)
        
        # Create S3 client with assumed credentials
        s3 = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Combine base prefix with filter
        search_prefix = f"{base_prefix}{filter_prefix}".strip('/')
        if search_prefix:
            search_prefix += '/'
        
        # Discover dataset prefixes
        datasets = discover_datasets(
            s3,
            bucket,
            search_prefix,
            max_depth
        )
        
        return create_response(200, {
            'datasets': datasets,
            'bucket': bucket,
            'base_prefix': base_prefix
        })
        
    except Exception as e:
        return handle_error(e, 'Failed to list datasets')


def discover_datasets(s3_client, bucket: str, prefix: str, max_depth: int) -> List[Dict[str, Any]]:
    """
    Discover dataset prefixes by scanning S3 for directories containing images.
    
    Args:
        s3_client: Boto3 S3 client with assumed credentials
        bucket: S3 bucket name
        prefix: Base prefix to search
        max_depth: Maximum directory depth to scan
    
    Returns:
        List of datasets with metadata
    """
    datasets = []
    visited_prefixes = set()
    
    # Common image extensions
    image_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'}
    
    def scan_prefix(current_prefix: str, depth: int):
        """Recursively scan prefixes for images."""
        if depth > max_depth or current_prefix in visited_prefixes:
            return
        
        visited_prefixes.add(current_prefix)
        
        try:
            # List objects with delimiter to get "folders"
            paginator = s3_client.get_paginator('list_objects_v2')
            pages = paginator.paginate(
                Bucket=bucket,
                Prefix=current_prefix,
                Delimiter='/'
            )
            
            image_count = 0
            last_modified = None
            has_subdirs = False
            
            for page in pages:
                # Count images in current prefix
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    ext = os.path.splitext(key)[1].lower()
                    
                    if ext in image_extensions:
                        image_count += 1
                        if last_modified is None or obj['LastModified'] > last_modified:
                            last_modified = obj['LastModified']
                
                # Check for subdirectories
                common_prefixes = page.get('CommonPrefixes', [])
                if common_prefixes:
                    has_subdirs = True
                    
                    # If current prefix has images, add it as a dataset
                    if image_count > 0:
                        datasets.append({
                            'prefix': current_prefix,
                            'image_count': image_count,
                            'last_modified': last_modified.isoformat() if last_modified else None,
                            'has_subdirectories': True
                        })
                    
                    # Recursively scan subdirectories
                    if depth < max_depth:
                        for common_prefix in common_prefixes:
                            subprefix = common_prefix['Prefix']
                            scan_prefix(subprefix, depth + 1)
            
            # If no subdirectories and has images, add as dataset
            if not has_subdirs and image_count > 0:
                datasets.append({
                    'prefix': current_prefix,
                    'image_count': image_count,
                    'last_modified': last_modified.isoformat() if last_modified else None,
                    'has_subdirectories': False
                })
                
        except Exception as e:
            print(f"Error scanning prefix {current_prefix}: {str(e)}")
    
    # Start scanning from base prefix
    scan_prefix(prefix, 0)
    
    # Sort by image count (descending) and prefix
    datasets.sort(key=lambda x: (-x['image_count'], x['prefix']))
    
    return datasets


def count_images(event):
    """
    Count images in a specific S3 prefix.
    
    Request Body:
        - usecase_id: Required. The use case ID
        - prefix: Required. The S3 prefix to count
    
    Returns:
        Image count and sample image keys
    """
    try:
        body = json.loads(event.get('body', '{}'))
        usecase_id = body.get('usecase_id')
        prefix = body.get('prefix')
        
        if not usecase_id or not prefix:
            return create_response(400, {
                'error': 'usecase_id and prefix are required'
            })
        
        # Get use case details
        usecase = get_usecase(usecase_id)
        
        # Get bucket and credentials (uses Data Account if configured)
        bucket, _, credentials = get_data_bucket_and_credentials(usecase)
        
        # Create S3 client with assumed credentials
        s3 = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Count images
        image_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'}
        image_count = 0
        sample_images = []
        
        paginator = s3.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
        
        for page in pages:
            for obj in page.get('Contents', []):
                key = obj['Key']
                ext = os.path.splitext(key)[1].lower()
                
                if ext in image_extensions:
                    image_count += 1
                    
                    # Collect first 5 images as samples
                    if len(sample_images) < 5:
                        sample_images.append({
                            'key': key,
                            'size': obj['Size'],
                            'last_modified': obj['LastModified'].isoformat()
                        })
        
        return create_response(200, {
            'prefix': prefix,
            'image_count': image_count,
            'sample_images': sample_images,
            'bucket': bucket
        })
        
    except Exception as e:
        return handle_error(e, 'Failed to count images')


def get_image_preview(event):
    """
    Generate presigned URLs for image preview.
    
    Query Parameters:
        - usecase_id: Required. The use case ID
        - prefix: Required. The S3 prefix to preview
        - limit: Optional. Number of images to preview (default: 8, max: 20)
    
    Returns:
        List of presigned URLs for image preview
    """
    try:
        params = event.get('queryStringParameters', {}) or {}
        usecase_id = params.get('usecase_id')
        prefix = params.get('prefix')
        limit = min(int(params.get('limit', '8')), 20)  # Max 20 images
        
        if not usecase_id or not prefix:
            return create_response(400, {
                'error': 'usecase_id and prefix are required'
            })
        
        # Get use case details
        usecase = get_usecase(usecase_id)
        
        # Get bucket and credentials (uses Data Account if configured)
        bucket, _, credentials = get_data_bucket_and_credentials(usecase)
        
        # Create S3 client with assumed credentials
        s3 = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Find image files
        image_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'}
        image_keys = []
        
        paginator = s3.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
        
        for page in pages:
            for obj in page.get('Contents', []):
                key = obj['Key']
                ext = os.path.splitext(key)[1].lower()
                
                if ext in image_extensions:
                    image_keys.append({
                        'key': key,
                        'size': obj['Size'],
                        'last_modified': obj['LastModified'].isoformat()
                    })
                    
                    # Stop when we have enough images
                    if len(image_keys) >= limit:
                        break
            
            if len(image_keys) >= limit:
                break
        
        # Generate presigned URLs (valid for 30 minutes)
        preview_images = []
        for image in image_keys:
            try:
                presigned_url = s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': bucket, 'Key': image['key']},
                    ExpiresIn=1800  # 30 minutes
                )
                
                preview_images.append({
                    'key': image['key'],
                    'filename': os.path.basename(image['key']),
                    'size': image['size'],
                    'last_modified': image['last_modified'],
                    'presigned_url': presigned_url
                })
            except Exception as e:
                print(f"Failed to generate presigned URL for {image['key']}: {str(e)}")
                continue
        
        return create_response(200, {
            'prefix': prefix,
            'bucket': bucket,
            'total_found': len(image_keys),
            'images': preview_images,
            'expires_in_seconds': 1800
        })
        
    except Exception as e:
        return handle_error(e, 'Failed to generate image preview')
