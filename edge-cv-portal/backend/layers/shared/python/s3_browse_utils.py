"""
Shared S3 browsing utilities for portal Lambda functions.

Provides reusable functions for browsing S3 bucket contents with support for:
- Folder navigation with breadcrumbs
- File type detection (manifest, image, file)
- Cross-account S3 access
- Pagination and filtering
"""

import boto3
from typing import Dict, List, Any, Optional, Callable
from shared_utils import assume_usecase_role, get_usecase, get_s3_client_for_bucket


def browse_s3_bucket(
    usecase_id: str,
    prefix: str = '',
    delimiter: str = '/',
    file_filter: Optional[Callable[[Dict], bool]] = None
) -> Dict[str, Any]:
    """
    Browse S3 bucket contents for a use case.
    
    Args:
        usecase_id: The use case ID to browse
        prefix: S3 prefix to browse (default: root)
        delimiter: Delimiter for grouping (default: '/')
        file_filter: Optional function to filter files. Takes file dict, returns bool.
    
    Returns:
        Dictionary with:
        - bucket: S3 bucket name
        - current_prefix: Current S3 prefix being browsed
        - breadcrumbs: List of breadcrumb navigation items
        - folders: List of folder items
        - files: List of file items
        - folder_count: Number of folders
        - file_count: Number of files
    
    Raises:
        ValueError: If usecase_id is invalid or bucket not configured
        Exception: If S3 access fails
    """
    # Get use case details
    usecase = get_usecase(usecase_id)
    
    # Determine which bucket to browse
    # Try data_s3_bucket first (for separate data account), then fall back to s3_bucket
    target_bucket = usecase.get('data_s3_bucket') or usecase.get('s3_bucket')
    
    if not target_bucket:
        raise ValueError('No S3 bucket configured for this use case')
    
    # Get S3 client with correct credentials for the target bucket
    s3_client = get_s3_client_for_bucket(usecase, target_bucket, 'browse-s3-bucket')
    
    # List objects in bucket
    folders = []
    files = []
    
    paginator = s3_client.get_paginator('list_objects_v2')
    pages = paginator.paginate(
        Bucket=target_bucket,
        Prefix=prefix,
        Delimiter=delimiter
    )
    
    # Collect common prefixes (folders)
    for page in pages:
        for common_prefix in page.get('CommonPrefixes', []):
            folder_name = common_prefix['Prefix'].rstrip('/').split('/')[-1]
            folders.append({
                'name': folder_name,
                'prefix': common_prefix['Prefix'],
                'type': 'folder'
            })
        
        # Collect files
        for obj in page.get('Contents', []):
            key = obj['Key']
            # Skip if it's the prefix itself
            if key == prefix:
                continue
            
            file_name = key.split('/')[-1]
            file_size = obj['Size']
            last_modified = obj['LastModified'].isoformat() if 'LastModified' in obj else None
            
            # Determine file type
            file_type = detect_file_type(file_name)
            
            file_item = {
                'name': file_name,
                'key': key,
                'size': file_size,
                'size_mb': round(file_size / (1024 * 1024), 2),
                'last_modified': last_modified,
                'type': file_type,
                's3_uri': f's3://{target_bucket}/{key}'
            }
            
            # Apply filter if provided
            if file_filter is None or file_filter(file_item):
                files.append(file_item)
    
    # Sort folders and files by name
    folders.sort(key=lambda x: x['name'].lower())
    files.sort(key=lambda x: x['name'].lower())
    
    # Generate breadcrumb navigation
    breadcrumbs = []
    if prefix:
        breadcrumbs.append({'name': 'root', 'prefix': ''})
        parts = prefix.rstrip('/').split('/')
        current = ''
        for part in parts:
            if part:
                current += part + '/'
                breadcrumbs.append({'name': part, 'prefix': current})
    else:
        breadcrumbs.append({'name': 'root', 'prefix': ''})
    
    return {
        'bucket': target_bucket,
        'current_prefix': prefix,
        'breadcrumbs': breadcrumbs,
        'folders': folders,
        'files': files,
        'folder_count': len(folders),
        'file_count': len(files)
    }


def detect_file_type(filename: str) -> str:
    """
    Detect file type based on extension.
    
    Args:
        filename: The filename to analyze
    
    Returns:
        File type: 'manifest', 'image', or 'file'
    """
    lower = filename.lower()
    if lower.endswith(('.manifest', '.jsonl')) or 'manifest' in lower:
        return 'manifest'
    elif lower.endswith(('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif')):
        return 'image'
    else:
        return 'file'


def filter_manifest_files(file_item: Dict) -> bool:
    """Filter function to show only manifest files."""
    return file_item['type'] == 'manifest'


def filter_image_files(file_item: Dict) -> bool:
    """Filter function to show only image files."""
    return file_item['type'] == 'image'


def filter_by_extension(extensions: List[str]) -> Callable[[Dict], bool]:
    """
    Create a filter function for specific file extensions.
    
    Args:
        extensions: List of extensions to include (e.g., ['.json', '.jsonl'])
    
    Returns:
        Filter function
    """
    def filter_func(file_item: Dict) -> bool:
        filename = file_item['name'].lower()
        return any(filename.endswith(ext.lower()) for ext in extensions)
    
    return filter_func
