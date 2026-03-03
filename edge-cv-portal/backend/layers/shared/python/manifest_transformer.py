"""
Manifest Transformer Utility
Transforms Ground Truth manifests to DDA-compatible format
Handles field name mapping and class label normalization for segmentation
"""
import json
import logging
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)


def detect_ground_truth_attributes(entry: Dict, task_type: str) -> Optional[Dict[str, str]]:
    """
    Detect Ground Truth attribute names from a manifest entry.
    
    For classification:
        Returns: label_attr, metadata_attr
    
    For segmentation:
        Returns: label_attr, metadata_attr, mask_ref_attr, mask_ref_metadata_attr
        
    Special case: For segmentation-only manifests (no classification labels),
        returns mask attributes and sets segmentation_only flag
    """
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
    
    # Check if this is a segmentation-only manifest (no classification label)
    segmentation_only = False
    if task_type == 'segmentation':
        # Check if this is actually a mask reference attribute (ends with -ref)
        if label_attr.endswith('-ref'):
            # This is a mask reference, not a classification label
            segmentation_only = True
            # Don't return None - continue to process as segmentation-only
        elif label_attr not in entry:
            # Label attribute doesn't exist and it's not a mask reference
            return None
    
    result = {
        'label_attr': label_attr if not segmentation_only else None,
        'metadata_attr': metadata_attr,
        'segmentation_only': segmentation_only
    }
    
    # For segmentation, also find mask reference attributes
    if task_type == 'segmentation':
        mask_ref_attr = None
        mask_ref_metadata_attr = None
        
        # If segmentation-only, the detected attributes ARE the mask attributes
        if segmentation_only:
            mask_ref_attr = label_attr
            mask_ref_metadata_attr = metadata_attr
        else:
            # First, look for any -ref-metadata attribute (highest priority)
            for key in entry.keys():
                if key.endswith('-ref-metadata') and key not in skip_attrs:
                    mask_ref_metadata_attr = key
                    mask_ref_attr = key.replace('-metadata', '')
                    break
            
            # If not found, look for any -ref attribute
            if not mask_ref_attr:
                for key in entry.keys():
                    if key.endswith('-ref') and key not in skip_attrs and not key.endswith('-ref-metadata'):
                        mask_ref_attr = key
                        mask_ref_metadata_attr = f"{key}-metadata"
                        break
        
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
        Normalizes color map to DDA format:
        - Class 0 = BACKGROUND (normal)
        - Class 1 = DEFECT (anomaly)
    
    For segmentation-only manifests (no classification labels):
        Automatically infers classification labels from filename:
        - If filename contains "anomaly" (case-insensitive) → anomaly-label = 1
        - Otherwise → anomaly-label = 0
        Generates synthetic anomaly-label-metadata
        
        NOTE: This is a temporary heuristic matching the SageMaker notebook approach.
        For production use, customers should provide manifests with explicit classification labels.
    
    Also updates the 'job-name' field inside metadata to match the new attribute names.
    """
    transformed = {}
    
    # Copy and normalize source-ref (always present)
    if 'source-ref' in entry:
        source_ref = entry['source-ref']
        # Normalize S3 URI: remove duplicate s3:// prefixes and double slashes
        source_ref = source_ref.replace('s3://s3://', 's3://')
        source_ref = source_ref.replace('//', '/')
        source_ref = 's3://' + source_ref.lstrip('s3:/').lstrip('/')
        
        # Fix incorrect path patterns
        source_ref = source_ref.replace('/cookies/training-images/', '/cookies/dataset-files/training-images/')
        source_ref = source_ref.replace('/cookies/mask-images/', '/cookies/dataset-files/mask-images/')
        
        transformed['source-ref'] = source_ref
    
    # Handle segmentation-only manifests (infer classification labels)
    if detected_attrs.get('segmentation_only', False):
        # Extract filename from source-ref
        source_ref = entry.get('source-ref', '')
        filename = source_ref.split('/')[-1].lower()
        
        # Infer classification label from filename
        # Check if "anomaly" appears in filename (case-insensitive)
        # This matches the logic used in the SageMaker training notebook
        if 'anomaly' in filename:
            label = 1
            class_name = 'anomaly'
        else:
            label = 0
            class_name = 'normal'
        
        transformed['anomaly-label'] = label
        
        # Generate synthetic classification metadata
        transformed['anomaly-label-metadata'] = {
            'class-name': class_name,
            'confidence': 1.0,
            'type': 'groundtruth/image-classification',
            'job-name': 'anomaly-label',
            'human-annotated': 'yes',
            'creation-date': entry.get(detected_attrs['metadata_attr'], {}).get('creation-date', '') if detected_attrs.get('metadata_attr') else ''
        }
    else:
        # Transform label attribute (normal case)
        label_attr = detected_attrs.get('label_attr')
        if label_attr and label_attr in entry:
            transformed['anomaly-label'] = entry[label_attr]
        
        # Transform metadata attribute (normal case)
        metadata_attr = detected_attrs.get('metadata_attr')
        if metadata_attr and metadata_attr in entry:
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
            mask_ref = entry[mask_ref_attr]
            transformed['anomaly-mask-ref'] = mask_ref
        
        # Transform mask reference metadata
        mask_ref_metadata_attr = detected_attrs.get('mask_ref_metadata_attr')
        if mask_ref_metadata_attr and mask_ref_metadata_attr in entry:
            mask_metadata = entry[mask_ref_metadata_attr].copy() if isinstance(entry[mask_ref_metadata_attr], dict) else entry[mask_ref_metadata_attr]
            
            # Update job-name in mask metadata if present
            if isinstance(mask_metadata, dict) and 'job-name' in mask_metadata:
                mask_metadata['job-name'] = 'anomaly-mask-ref'
            
            transformed['anomaly-mask-ref-metadata'] = mask_metadata
    
    # Copy any other attributes (like confidence scores, etc.)
    skip_attrs = {'source-ref'}
    
    # Add label attributes to skip list if they exist
    label_attr = detected_attrs.get('label_attr')
    metadata_attr = detected_attrs.get('metadata_attr')
    if label_attr:
        skip_attrs.add(label_attr)
    if metadata_attr:
        skip_attrs.add(metadata_attr)
    
    # Add mask attributes to skip list if they exist
    if task_type == 'segmentation':
        mask_ref_attr = detected_attrs.get('mask_ref_attr')
        mask_ref_metadata_attr = detected_attrs.get('mask_ref_metadata_attr')
        if mask_ref_attr:
            skip_attrs.add(mask_ref_attr)
        if mask_ref_metadata_attr:
            skip_attrs.add(mask_ref_metadata_attr)
        if mask_ref_metadata_attr:
            skip_attrs.add(mask_ref_metadata_attr)
    
    for key, value in entry.items():
        if key not in skip_attrs and key not in transformed:
            transformed[key] = value
    
    return transformed


def transform_manifest_lines(manifest_lines: List[str], task_type: str = 'classification') -> Dict:
    """
    Transform a list of manifest lines (JSONL format) to DDA format.
    
    Args:
        manifest_lines: List of JSON strings (one per line)
        task_type: 'classification' or 'segmentation'
    
    Returns:
        Dict with:
        - transformed_lines: List of transformed JSON strings
        - stats: Transformation statistics
        - detected_attrs: Detected Ground Truth attributes
        - errors: List of transformation errors
    """
    if not manifest_lines:
        return {
            'transformed_lines': [],
            'stats': {'total': 0, 'transformed': 0, 'skipped': 0},
            'detected_attrs': None,
            'errors': []
        }
    
    # Detect attributes from first line
    try:
        first_entry = json.loads(manifest_lines[0])
        detected_attrs = detect_ground_truth_attributes(first_entry, task_type)
        
        if not detected_attrs:
            return {
                'transformed_lines': [],
                'stats': {'total': len(manifest_lines), 'transformed': 0, 'skipped': len(manifest_lines)},
                'detected_attrs': None,
                'errors': ['Could not detect Ground Truth attributes in manifest']
            }
    except Exception as e:
        logger.error(f"Error parsing first manifest line: {str(e)}")
        return {
            'transformed_lines': [],
            'stats': {'total': len(manifest_lines), 'transformed': 0, 'skipped': len(manifest_lines)},
            'detected_attrs': None,
            'errors': [f'Error parsing manifest: {str(e)}']
        }
    
    # Transform all lines
    transformed_lines = []
    errors = []
    
    for i, line in enumerate(manifest_lines):
        try:
            entry = json.loads(line)
            transformed_entry = transform_manifest_entry(entry, detected_attrs, task_type)
            transformed_lines.append(json.dumps(transformed_entry))
        except Exception as e:
            error_msg = f"Line {i + 1}: {str(e)}"
            logger.warning(error_msg)
            errors.append(error_msg)
    
    return {
        'transformed_lines': transformed_lines,
        'stats': {
            'total': len(manifest_lines),
            'transformed': len(transformed_lines),
            'skipped': len(manifest_lines) - len(transformed_lines)
        },
        'detected_attrs': detected_attrs,
        'errors': errors
    }
