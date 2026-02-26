"""
Manifest Validator and Transformer API

Provides endpoints to:
- Validate Ground Truth manifests (reuses existing validation logic)
- Transform to DDA format (reuses manifest_transformer)
- Fix common issues (timestamp colons, etc.)
- Compare before/after
"""

import json
import boto3
import re
from typing import Dict, List, Any
from urllib.parse import urlparse

# Import the existing transformer from shared layer
import sys
sys.path.append('/opt/python')
from manifest_transformer import (
    detect_ground_truth_attributes,
    transform_manifest_lines
)

s3 = boto3.client('s3')


def lambda_handler(event, context):
    """Main Lambda handler for manifest validation and transformation"""
    
    try:
        body = json.loads(event.get('body', '{}'))
        action = body.get('action')
        
        if action == 'validate':
            return validate_manifest(body)
        elif action == 'transform':
            return transform_manifest(body)
        elif action == 'fix_timestamps':
            return fix_timestamp_colons(body)
        elif action == 'validate_and_transform':
            return validate_and_transform(body)
        else:
            return error_response('Invalid action. Use: validate, transform, fix_timestamps, or validate_and_transform', 400)
            
    except Exception as e:
        print(f"Error in manifest_validator: {str(e)}")
        import traceback
        traceback.print_exc()
        return error_response(str(e), 500)


def validate_manifest(body: Dict) -> Dict:
    """Validate a Ground Truth manifest - reuses existing validation logic"""
    
    manifest_s3_path = body.get('manifestPath')
    region = body.get('region', 'us-east-1')
    task_type = body.get('taskType', 'segmentation')
    
    if not manifest_s3_path:
        return error_response('manifestPath is required', 400)
    
    try:
        # Download manifest
        s3_client = boto3.client('s3', region_name=region)
        parsed = urlparse(manifest_s3_path)
        bucket = parsed.netloc
        key = parsed.path.lstrip('/')
        
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
                errors.append({
                    'type': 'parse_error',
                    'severity': 'error',
                    'message': f"Line {i+1}: Invalid JSON - {str(e)}"
                })
        
        if not entries:
            errors.append({
                'type': 'empty_manifest',
                'severity': 'error',
                'message': 'No valid entries found in manifest'
            })
            return success_response({
                'valid': False,
                'issues': errors,
                'warnings': warnings,
                'stats': {}
            })
        
        # Analyze entries
        stats = {
            'total_entries': len(entries),
            'entries_with_masks': 0,
            'entries_with_labels': 0,
            'unique_labels': set(),
            'timestamp_colon_issues': 0
        }
        
        # Detect attributes from first entry
        detected_attrs = detect_ground_truth_attributes(entries[0], task_type)
        
        if not detected_attrs:
            errors.append({
                'type': 'no_attributes',
                'severity': 'error',
                'message': 'Could not detect Ground Truth attributes in manifest',
                'fix': 'Ensure manifest has proper Ground Truth format with label and metadata fields'
            })
        else:
            # Check for segmentation-only manifests
            if detected_attrs.get('segmentation_only'):
                warnings.append({
                    'type': 'segmentation_only',
                    'severity': 'info',
                    'message': 'Segmentation-only manifest detected (no classification labels)',
                    'fix': 'DDA will automatically infer classification labels during training'
                })
            
            # Count entries with masks and labels
            mask_ref_attr = detected_attrs.get('mask_ref_attr')
            label_attr = detected_attrs.get('label_attr')
            
            for entry in entries:
                if mask_ref_attr and mask_ref_attr in entry:
                    stats['entries_with_masks'] += 1
                    
                    # Check for timestamp colons
                    mask_path = entry[mask_ref_attr]
                    if re.search(r'T\d{2}:\d{2}:\d{2}\.', mask_path):
                        stats['timestamp_colon_issues'] += 1
                
                if label_attr and label_attr in entry:
                    stats['entries_with_labels'] += 1
                    label_value = entry[label_attr]
                    if isinstance(label_value, (int, str)):
                        stats['unique_labels'].add(str(label_value))
        
        # Add timestamp colon issue if found
        if stats['timestamp_colon_issues'] > 0:
            errors.append({
                'type': 'timestamp_colons',
                'severity': 'error',
                'message': f"Found {stats['timestamp_colon_issues']} mask filenames with colons in timestamps (Ground Truth bug)",
                'example': 'T21:16:30 should be T211630',
                'fix': 'Use fix_timestamps action to remove colons from timestamps'
            })
        
        # Convert set to list for JSON serialization
        stats['unique_labels'] = sorted(list(stats['unique_labels']))
        
        # Determine if transformation is needed
        needs_transformation = detected_attrs and (
            detected_attrs.get('segmentation_only') or 
            detected_attrs.get('label_attr') != 'anomaly-label'
        )
        
        return success_response({
            'valid': len(errors) == 0,
            'issues': errors,
            'warnings': warnings,
            'stats': stats,
            'manifest_type': 'segmentation_only' if detected_attrs and detected_attrs.get('segmentation_only') else 'classification_and_segmentation',
            'needs_transformation': needs_transformation,
            'detected_attributes': {
                'label': detected_attrs.get('label_attr') if detected_attrs else None,
                'mask': detected_attrs.get('mask_ref_attr') if detected_attrs else None,
                'metadata': detected_attrs.get('metadata_attr') if detected_attrs else None
            }
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return error_response(f'Validation failed: {str(e)}', 500)


def transform_manifest(body: Dict) -> Dict:
    """Transform Ground Truth manifest to DDA format - reuses manifest_transformer"""
    
    manifest_s3_path = body.get('manifestPath')
    region = body.get('region', 'us-east-1')
    output_path = body.get('outputPath')  # Optional
    task_type = body.get('taskType', 'segmentation')
    
    if not manifest_s3_path:
        return error_response('manifestPath is required', 400)
    
    try:
        # Download manifest
        s3_client = boto3.client('s3', region_name=region)
        parsed = urlparse(manifest_s3_path)
        bucket = parsed.netloc
        key = parsed.path.lstrip('/')
        
        response = s3_client.get_object(Bucket=bucket, Key=key)
        manifest_content = response['Body'].read().decode('utf-8')
        
        # Parse original entries
        manifest_lines = manifest_content.strip().split('\n')
        original_entries = [json.loads(line) for line in manifest_lines if line.strip()]
        
        # Use the existing transform_manifest_lines function
        transformation_result = transform_manifest_lines(manifest_lines, task_type=task_type)
        
        transformed_entries = [
            json.loads(line) for line in transformation_result['transformed_lines'] if line.strip()
        ]
        
        # Create comparison (first 3 entries)
        comparison = {
            'before': original_entries[:3],
            'after': transformed_entries[:3],
            'total_entries': len(original_entries),
            'stats': transformation_result.get('stats', {})
        }
        
        # Optionally save to S3
        saved_path = None
        if output_path:
            output_parsed = urlparse(output_path)
            output_bucket = output_parsed.netloc
            output_key = output_parsed.path.lstrip('/')
            
            transformed_content = '\n'.join(transformation_result['transformed_lines'])
            s3_client.put_object(
                Bucket=output_bucket,
                Key=output_key,
                Body=transformed_content.encode('utf-8')
            )
            saved_path = output_path
        
        return success_response({
            'transformed': True,
            'comparison': comparison,
            'saved_path': saved_path,
            'errors': transformation_result.get('errors', []),
            'warnings': []
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return error_response(f'Transformation failed: {str(e)}', 500)


def fix_timestamp_colons(body: Dict) -> Dict:
    """Fix timestamp colons in mask filenames (Ground Truth bug)"""
    
    manifest_s3_path = body.get('manifestPath')
    region = body.get('region', 'us-east-1')
    output_path = body.get('outputPath')  # Optional
    
    if not manifest_s3_path:
        return error_response('manifestPath is required', 400)
    
    try:
        # Download manifest
        s3_client = boto3.client('s3', region_name=region)
        parsed = urlparse(manifest_s3_path)
        bucket = parsed.netloc
        key = parsed.path.lstrip('/')
        
        response = s3_client.get_object(Bucket=bucket, Key=key)
        manifest_content = response['Body'].read().decode('utf-8')
        
        # Fix colons using regex
        # Pattern: T21:16:30 -> T211630
        fixed_content = re.sub(
            r'T(\d{2}):(\d{2}):(\d{2})\.',
            r'T\1\2\3.',
            manifest_content
        )
        
        # Count changes
        original_lines = manifest_content.strip().split('\n')
        fixed_lines = fixed_content.strip().split('\n')
        changes = sum(1 for o, f in zip(original_lines, fixed_lines) if o != f)
        
        # Parse for comparison
        original_entries = [json.loads(line) for line in original_lines if line.strip()]
        fixed_entries = [json.loads(line) for line in fixed_lines if line.strip()]
        
        # Save if output path provided, otherwise save back to original
        saved_path = None
        if changes > 0:
            if output_path:
                output_parsed = urlparse(output_path)
                output_bucket = output_parsed.netloc
                output_key = output_parsed.path.lstrip('/')
                
                s3_client.put_object(
                    Bucket=output_bucket,
                    Key=output_key,
                    Body=fixed_content.encode('utf-8')
                )
                saved_path = output_path
            else:
                # Save back to original location
                s3_client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=fixed_content.encode('utf-8')
                )
                saved_path = manifest_s3_path
        
        return success_response({
            'fixed': changes > 0,
            'changes_made': changes,
            'total_entries': len(original_entries),
            'saved_path': saved_path,
            'comparison': {
                'before': original_entries[0] if original_entries else None,
                'after': fixed_entries[0] if fixed_entries else None
            }
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return error_response(f'Fix failed: {str(e)}', 500)


def validate_and_transform(body: Dict) -> Dict:
    """Complete workflow: validate, fix issues, and transform"""
    
    manifest_s3_path = body.get('manifestPath')
    region = body.get('region', 'us-east-1')
    auto_fix = body.get('autoFix', True)
    
    if not manifest_s3_path:
        return error_response('manifestPath is required', 400)
    
    try:
        # Step 1: Validate
        validation_result = validate_manifest(body)
        validation_data = json.loads(validation_result['body'])
        
        if not validation_data.get('valid') and auto_fix:
            # Step 2: Fix timestamp issues if present
            has_timestamp_issues = any(
                issue['type'] == 'timestamp_colons' 
                for issue in validation_data.get('issues', [])
            )
            
            if has_timestamp_issues:
                fix_result = fix_timestamp_colons(body)
                fix_data = json.loads(fix_result['body'])
                
                # Re-validate after fix
                validation_result = validate_manifest(body)
                validation_data = json.loads(validation_result['body'])
        
        # Step 3: Transform if needed
        transform_result = None
        if validation_data.get('needs_transformation'):
            transform_result = transform_manifest(body)
        
        return success_response({
            'validation': validation_data,
            'transformation': json.loads(transform_result['body']) if transform_result else None,
            'ready_for_training': validation_data.get('valid', False)
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return error_response(f'Workflow failed: {str(e)}', 500)


def success_response(data: Dict) -> Dict:
    """Create success response"""
    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(data)
    }


def error_response(message: str, status_code: int = 500) -> Dict:
    """Create error response"""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps({'error': message})
    }
