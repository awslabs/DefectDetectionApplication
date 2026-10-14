import json
import boto3
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional
from decimal import Decimal
import os
import sys

from botocore.exceptions import ClientError

# Import shared utilities
sys.path.append('/opt/python')
from shared_utils import (
    get_usecase, assume_usecase_role, create_response as shared_create_response, handle_error,
    get_user_from_event, check_user_access, log_audit_event, _display_identity,
    get_s3_client_for_bucket, parse_s3_uri, assert_bucket_allowed, allowed_s3_buckets,
    BucketConfigError, InvalidS3Uri,
)
from s3_browse_utils import browse_s3_bucket as browse_s3_bucket_util, filter_manifest_files
from manifest_transformer import detect_ground_truth_attributes

# Initialize AWS clients. There is no module-level S3 client: every S3 read
# goes through get_s3_client_for_bucket for the use case's configured bucket
# (use-case bucket configuration, design §3).
dynamodb = boto3.resource('dynamodb')


# Helper class to convert Decimal to int/float for JSON serialization
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super(DecimalEncoder, self).default(obj)

# Environment variables
USE_CASES_TABLE = os.environ.get('USE_CASES_TABLE', 'dda-portal-usecases')
PRE_LABELED_DATASETS_TABLE = os.environ.get('PRE_LABELED_DATASETS_TABLE', 'dda-portal-pre-labeled-datasets')


def lambda_handler(event, context):
    """Handle pre-labeled dataset operations"""
    try:
        method = event['httpMethod']
        path = event['path']
        
        print(f"Request: {method} {path}")
        
        # Handle CORS preflight requests
        if method == 'OPTIONS':
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
        
        # The authenticated caller, resolved once and handed to every route
        # (every route is authorized against the use case it acts on).
        user = get_user_from_event(event)
        
        if method == 'GET' and path == '/datasets/pre-labeled':
            return list_datasets(event, user)
        elif method == 'POST' and path == '/datasets/pre-labeled':
            return create_dataset(event, user)
        elif method == 'POST' and path == '/datasets/validate-manifest':
            return validate_manifest(event, user)
        elif method == 'GET' and '/datasets/pre-labeled/browse' in path:
            return browse_s3_bucket(event, user)
        elif method == 'GET' and '/datasets/pre-labeled/' in path:
            dataset_id = path.split('/')[-1]
            return get_dataset(dataset_id, user)
        elif method == 'DELETE' and '/datasets/pre-labeled/' in path:
            dataset_id = path.split('/')[-1]
            return delete_dataset(dataset_id, user)
        else:
            return create_response(405, {'error': 'Method not allowed'})
            
    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(500, {'error': 'Internal server error', 'details': str(e)})


def create_response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Create HTTP response"""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
            'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
            'Access-Control-Max-Age': '86400'
        },
        'body': json.dumps(body, cls=DecimalEncoder)
    }


def _insufficient_permissions(user: Dict, usecase_id: Optional[str], resource_type: str,
                              resource_id: Optional[str],
                              required_role: Optional[str] = None) -> Dict[str, Any]:
    """The 403 every route answers when `check_user_access` fails, after one
    `unauthorized_access` audit row (design §7); nothing has been read from
    S3 or written to DynamoDB at this point."""
    details = {'usecase_id': usecase_id or ''}
    if required_role:
        details['required_role'] = required_role
    log_audit_event(
        user_id=user['user_id'], action='unauthorized_access',
        resource_type=resource_type, resource_id=resource_id or 'unknown',
        result='denied', details=details)
    return create_response(403, {'error': 'Insufficient permissions'})


def list_datasets(event, user: Dict):
    """List pre-labeled datasets for a use case"""
    try:
        query_params = event.get('queryStringParameters') or {}
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Membership of the use case named in the query
        if not check_user_access(user['user_id'], usecase_id):
            return _insufficient_permissions(user, usecase_id, 'pre_labeled_dataset', usecase_id)
        
        table = dynamodb.Table(PRE_LABELED_DATASETS_TABLE)
        response = table.query(
            IndexName='usecase-index',
            KeyConditionExpression='usecase_id = :usecase_id',
            ExpressionAttributeValues={':usecase_id': usecase_id}
        )
        
        datasets = response.get('Items', [])
        
        return create_response(200, {
            'datasets': datasets,
            'count': len(datasets)
        })
        
    except Exception as e:
        print(f"Error listing datasets: {str(e)}")
        return create_response(500, {'error': 'Failed to list datasets', 'details': str(e)})


def create_dataset(event, user: Dict):
    """Create a new pre-labeled dataset"""
    try:
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['name', 'usecase_id', 'manifest_s3_uri']
        for field in required_fields:
            if not body.get(field):
                return create_response(400, {'error': f'{field} is required'})
        
        usecase_id = body['usecase_id']
        
        # DataScientist on the use case, before any S3 or DynamoDB write
        if not check_user_access(user['user_id'], usecase_id, 'DataScientist'):
            return _insufficient_permissions(user, usecase_id, 'pre_labeled_dataset',
                                             usecase_id, 'DataScientist')
        
        # Bind the manifest bucket to the use case's configured buckets
        # before persisting (use-case bucket configuration, design §3)
        bucket, _key = parse_s3_uri(body['manifest_s3_uri'])
        usecase = get_usecase(usecase_id)
        assert_bucket_allowed(usecase, bucket, 'prelabeled-register',
                              actor=user, event=event)
        
        # Generate dataset ID
        dataset_id = str(uuid.uuid4())
        
        # Create dataset record
        dataset = {
            'dataset_id': dataset_id,
            'usecase_id': usecase_id,
            'name': body['name'],
            'description': body.get('description', ''),
            'manifest_s3_uri': body['manifest_s3_uri'],
            'task_type': body.get('task_type', 'unknown'),
            'label_attribute': body.get('label_attribute', ''),
            'image_count': body.get('image_count', 0),
            'label_stats': body.get('label_stats', {}),
            # The authenticated caller (email -> username -> sub), never a
            # body-supplied value.
            'created_by': _display_identity(user.get('email'), user.get('username'),
                                            user['user_id']),
            'created_at': int(datetime.utcnow().timestamp()),
            'updated_at': int(datetime.utcnow().timestamp())
        }
        
        # Save to DynamoDB
        table = dynamodb.Table(PRE_LABELED_DATASETS_TABLE)
        table.put_item(Item=dataset)
        
        return create_response(201, {
            'dataset': dataset,
            'message': 'Dataset created successfully'
        })
        
    except BucketConfigError as e:
        return create_response(400, e.to_body())
    except Exception as e:
        print(f"Error creating dataset: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(500, {'error': 'Failed to create dataset', 'details': str(e)})


def validate_manifest(event, user: Dict):
    """Validate a manifest file format"""
    try:
        body = json.loads(event.get('body', '{}'))
        manifest_s3_uri = body.get('manifest_s3_uri')
        usecase_id = body.get('usecase_id')
        
        if not manifest_s3_uri:
            return create_response(400, {'error': 'manifest_s3_uri is required'})
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # DataScientist on the use case, before any S3 call
        if not check_user_access(user['user_id'], usecase_id, 'DataScientist'):
            return _insufficient_permissions(user, usecase_id, 'pre_labeled_dataset',
                                             usecase_id, 'DataScientist')
        
        # Parse S3 URI (InvalidS3Uri -> 400 below)
        bucket, key = parse_s3_uri(manifest_s3_uri)
        
        # Get usecase to determine which account/role to use for S3 access
        usecase = get_usecase(usecase_id)
        
        # Bind the manifest bucket to the use case's configured buckets
        # before any STS or S3 call (use-case bucket configuration, design §3)
        assert_bucket_allowed(usecase, bucket, 'prelabeled-validate',
                              actor=user, event=event)
        
        # Get S3 client with correct credentials for the manifest bucket
        s3_client = get_s3_client_for_bucket(usecase, bucket, 'prelabeled-validate')
        
        # Validate manifest file
        validation_result = validate_manifest_file(bucket, key, s3_client, usecase=usecase)
        
        return create_response(200, validation_result)
        
    except BucketConfigError as e:
        return create_response(400, e.to_body())
    except Exception as e:
        print(f"Error validating manifest: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(500, {'error': 'Failed to validate manifest', 'details': str(e)})


def validate_manifest_file(bucket: str, key: str, s3_client, *, usecase: Dict) -> Dict[str, Any]:
    """Validate manifest file format and content.

    `s3_client` is the use case's client for `bucket` (required: there is no
    module-level fallback client); `usecase` decides which other buckets a
    manifest-internal reference (the segmentation mask probe) may be checked
    in — a reference outside the use case's configured buckets is skipped
    with a warning, never dereferenced (design §4).
    """
    if s3_client is None:
        raise ValueError('validate_manifest_file requires the S3 client for the manifest bucket')
    try:
        # Download and parse manifest
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read().decode('utf-8')
        
        lines = content.strip().split('\n')
        entries = []
        errors = []
        warnings = []
        
        # Parse each line
        for i, line in enumerate(lines):
            if not line.strip():
                continue
                
            try:
                entry = json.loads(line)
                entries.append(entry)
            except json.JSONDecodeError as e:
                errors.append(f"Line {i+1}: Invalid JSON - {str(e)}")
        
        if not entries:
            errors.append("No valid entries found in manifest")
            return {
                'valid': False,
                'errors': errors,
                'warnings': warnings,
                'stats': {
                    'sample_entries': []
                }
            }
        
        # Analyze entries
        stats = analyze_manifest_entries(entries)
        
        # Include sample entries for format detection
        stats['sample_entries'] = entries[:1]  # Include first entry for format detection
        
        # Validate required fields
        required_fields = ['source-ref']
        for i, entry in enumerate(entries[:10]):  # Check first 10 entries
            for field in required_fields:
                if field not in entry:
                    errors.append(f"Entry {i+1}: Missing required field '{field}'")
        
        # Validate Ground Truth format (detect label columns)
        if entries:
            first_entry = entries[0]
            label_attrs = [k for k in first_entry.keys() if k.endswith('-metadata') and not k.endswith('-ref-metadata')]
            
            if not label_attrs:
                # No classification label found - check if this is a valid segmentation-only manifest
                # Try to detect as segmentation manifest
                detected_attrs_seg = detect_ground_truth_attributes(first_entry, task_type='segmentation')
                
                if detected_attrs_seg and detected_attrs_seg.get('segmentation_only'):
                    # This is a valid segmentation-only manifest
                    warnings.append("Segmentation-only manifest detected. Classification labels will be automatically inferred during training (mask present = anomaly, no mask = normal).")
                else:
                    # Also try classification in case it's a different format
                    detected_attrs_cls = detect_ground_truth_attributes(first_entry, task_type='classification')
                    if detected_attrs_cls:
                        # Has classification but the detection logic didn't find it with the simple check
                        warnings.append("Manifest format detected. Will be transformed to DDA format during training.")
                    else:
                        errors.append("No label column found. Ground Truth manifest must have a label attribute (e.g., 'cookie-classification') with corresponding metadata (e.g., 'cookie-classification-metadata')")
            else:
                # Check if all entries have the same label attributes
                missing_label_entries = set()
                missing_metadata_entries = set()
                
                for i, entry in enumerate(entries[:10]):
                    for label_attr in label_attrs:
                        if label_attr not in entry:
                            missing_label_entries.add(i+1)
                        # Also check for the metadata
                        metadata_attr = label_attr
                        if metadata_attr not in entry:
                            missing_metadata_entries.add(i+1)
                
                # Report missing columns with entry numbers
                if missing_label_entries:
                    entry_list = ', '.join(map(str, sorted(missing_label_entries)))
                    errors.append(f"Missing label column in entries: {entry_list}")
                if missing_metadata_entries:
                    entry_list = ', '.join(map(str, sorted(missing_metadata_entries)))
                    errors.append(f"Missing metadata column in entries: {entry_list}")
        
        # Validate S3 URIs
        malformed_uris = []
        for i, entry in enumerate(entries[:10]):
            if 'source-ref' in entry:
                uri = entry['source-ref']
                issues = []
                
                # Check for common S3 URI issues
                if uri.startswith('s3://s3://'):
                    issues.append("duplicate 's3://' prefix")
                elif '//' in uri.replace('s3://', ''):
                    issues.append("double slashes in path")
                elif not uri.startswith('s3://'):
                    issues.append("missing 's3://' prefix")
                
                if issues:
                    malformed_uris.append(f"Entry {i+1}: {', '.join(issues)} - {uri}")
        
        if malformed_uris:
            warnings.append("⚠️ Malformed S3 URIs detected (will be auto-corrected during transformation):")
            warnings.extend(malformed_uris)
        
        # Check for common issues
        if stats['total_images'] < 10:
            warnings.append("Dataset has fewer than 10 images, which may not be sufficient for training")
        
        # For segmentation tasks, verify mask files are accessible
        if stats['task_type'] == 'segmentation' and entries:
            first_entry = entries[0]
            mask_ref_attrs = [k for k in first_entry.keys() if k.endswith('-ref') and not k.endswith('-ref-metadata')]
            
            if mask_ref_attrs:
                # Check if mask files are accessible
                mask_ref_attr = mask_ref_attrs[0]
                sample_mask_uri = first_entry.get(mask_ref_attr)
                
                if sample_mask_uri:
                    try:
                        mask_bucket, mask_key = parse_s3_uri(sample_mask_uri)
                    except InvalidS3Uri:
                        mask_bucket = mask_key = None
                        warnings.append("Segmentation mask reference is not a valid S3 URI; not checked")
                    if mask_bucket is not None:
                        try:
                            # The use case's client for the mask bucket: the
                            # manifest's own client when they match, the
                            # helper's for another configured bucket; a bucket
                            # outside the configured set is not probed.
                            if mask_bucket == bucket:
                                mask_s3 = s3_client
                            elif mask_bucket in allowed_s3_buckets(usecase):
                                mask_s3 = get_s3_client_for_bucket(
                                    usecase, mask_bucket, 'prelabeled-mask-ref')
                            else:
                                mask_s3 = None
                                warnings.append(
                                    f"Segmentation mask reference in bucket {mask_bucket} is not "
                                    f"configured for this use case; existence not checked")
                                print(f"Mask reference bucket {mask_bucket} is not configured for "
                                      f"use case {usecase.get('usecase_id')}; existence not checked")
                            if mask_s3 is not None:
                                # Try to access the mask file
                                mask_s3.head_object(Bucket=mask_bucket, Key=mask_key)
                                print(f"Verified segmentation mask file accessible: {sample_mask_uri}")
                        except ClientError as e:
                            if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                                errors.append(f"Segmentation mask file not found: {sample_mask_uri}")
                            else:
                                warnings.append(f"Could not verify segmentation mask file: {sample_mask_uri} ({str(e)})")
                        except Exception as e:
                            warnings.append(f"Could not verify segmentation mask file: {sample_mask_uri} ({str(e)})")
        
        return {
            'valid': len(errors) == 0,
            'errors': errors,
            'warnings': warnings,
            'stats': stats
        }
        
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return {
                'valid': False,
                'errors': [f"Manifest file not found: s3://{bucket}/{key}"],
                'warnings': [],
                'stats': {
                    'sample_entries': []
                }
            }
        return {
            'valid': False,
            'errors': [f"Failed to read manifest file: {str(e)}"],
            'warnings': [],
            'stats': {
                'sample_entries': []
            }
        }
    except Exception as e:
        return {
            'valid': False,
            'errors': [f"Failed to read manifest file: {str(e)}"],
            'warnings': [],
            'stats': {
                'sample_entries': []
            }
        }


def analyze_manifest_entries(entries: List[Dict]) -> Dict[str, Any]:
    """Analyze manifest entries to extract statistics"""
    total_images = len(entries)
    label_fields = set()
    label_distribution = {}
    task_type = 'unknown'
    
    # Analyze first few entries to determine structure
    for entry in entries[:10]:
        for key in entry.keys():
            # Skip metadata and source fields
            if key.endswith('-metadata') or key == 'source-ref':
                continue
            
            # Detect task type based on field naming patterns
            if key.endswith('-label'):
                # Classification: ends with -label (e.g., anomaly-label, cookie-classification)
                label_fields.add(key)
                task_type = 'classification'
            elif key.endswith('-bounding-box'):
                # Object Detection: ends with -bounding-box
                label_fields.add(key)
                task_type = 'detection'
            elif key.endswith('-mask-ref') or key.endswith('-ref'):
                # Segmentation: ends with -mask-ref or -ref (e.g., anomaly-mask-ref, cookie-segmentation-ref)
                label_fields.add(key)
                task_type = 'segmentation'
            elif key.endswith('-classification'):
                # Classification variant: ends with -classification (e.g., cookie-classification)
                label_fields.add(key)
                task_type = 'classification'
    
    # Count label distribution for classification tasks
    if task_type == 'classification' and label_fields:
        label_field = list(label_fields)[0]
        for entry in entries:
            if label_field in entry:
                label_value = entry[label_field]
                if isinstance(label_value, (int, float)):
                    # Convert numeric labels to strings
                    label_key = 'anomaly' if label_value == 1 else 'normal'
                else:
                    label_key = str(label_value)
                
                label_distribution[label_key] = label_distribution.get(label_key, 0) + 1
    
    return {
        'total_images': total_images,
        'task_type': task_type,
        'label_distribution': label_distribution,
        'sample_entries': entries[:3]  # First 3 entries as samples
    }


def get_dataset(dataset_id: str, user: Dict):
    """Get dataset details"""
    try:
        table = dynamodb.Table(PRE_LABELED_DATASETS_TABLE)
        response = table.get_item(Key={'dataset_id': dataset_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Dataset not found'})
        
        # Membership of the use case the record belongs to (404 first, so an
        # unknown id is not distinguishable from a forbidden one by order)
        record_usecase_id = response['Item'].get('usecase_id')
        if not check_user_access(user['user_id'], record_usecase_id):
            return _insufficient_permissions(user, record_usecase_id, 'pre_labeled_dataset',
                                             dataset_id)
        
        return create_response(200, {'dataset': response['Item']})
        
    except Exception as e:
        print(f"Error getting dataset: {str(e)}")
        return create_response(500, {'error': 'Failed to get dataset', 'details': str(e)})


def delete_dataset(dataset_id: str, user: Dict):
    """Delete a dataset"""
    try:
        table = dynamodb.Table(PRE_LABELED_DATASETS_TABLE)
        response = table.get_item(Key={'dataset_id': dataset_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Dataset not found'})
        
        # DataScientist on the record's use case, before the delete
        record_usecase_id = response['Item'].get('usecase_id')
        if not check_user_access(user['user_id'], record_usecase_id, 'DataScientist'):
            return _insufficient_permissions(user, record_usecase_id, 'pre_labeled_dataset',
                                             dataset_id, 'DataScientist')
        
        table.delete_item(Key={'dataset_id': dataset_id})
        
        return create_response(200, {'message': 'Dataset deleted successfully'})
        
    except Exception as e:
        print(f"Error deleting dataset: {str(e)}")
        return create_response(500, {'error': 'Failed to delete dataset', 'details': str(e)})



def browse_s3_bucket(event, user: Dict):
    """
    Browse S3 bucket contents for a use case.
    
    Query Parameters:
        - usecase_id: Required. The use case to browse
        - prefix: Optional. S3 prefix to browse (default: root)
        - delimiter: Optional. Delimiter for grouping (default: '/')
    
    Returns:
        - folders: List of folder prefixes
        - files: List of files with metadata
        - current_prefix: Current S3 prefix being browsed
    """
    try:
        query_params = event.get('queryStringParameters') or {}
        usecase_id = query_params.get('usecase_id')
        prefix = query_params.get('prefix', '')
        delimiter = query_params.get('delimiter', '/')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Membership of the use case named in the query, before any S3 call
        if not check_user_access(user['user_id'], usecase_id):
            return _insufficient_permissions(user, usecase_id, 'pre_labeled_dataset', usecase_id)
        
        # Use shared browse utility with manifest file filter
        result = browse_s3_bucket_util(
            usecase_id=usecase_id,
            prefix=prefix,
            delimiter=delimiter,
            file_filter=filter_manifest_files
        )
        
        return create_response(200, result)
        
    except ValueError as e:
        return create_response(400, {'error': str(e)})
    except Exception as e:
        print(f"Error browsing S3 bucket: {str(e)}")
        import traceback
        traceback.print_exc()
        return handle_error(e, 'Failed to browse S3 bucket')
