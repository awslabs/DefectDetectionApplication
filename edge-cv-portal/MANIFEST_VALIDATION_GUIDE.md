# Manifest Validation Guide

## Overview

The Pre-Labeled Datasets feature includes comprehensive manifest validation that detects and reports issues to users before they attempt to create a dataset. This guide explains what validation checks are performed and how issues are displayed in the UI.

## Validation Checks

### 1. JSON Format Validation
- **What it checks**: Each line in the manifest file must be valid JSON
- **Error message**: `Line X: Invalid JSON - [error details]`
- **Example**: If a line is missing a closing brace or has invalid syntax

### 2. Required Fields
- **What it checks**: Each entry must have a `source-ref` field pointing to an S3 image
- **Error message**: `Entry X: Missing required field 'source-ref'`
- **Why it matters**: Ground Truth requires the source image reference to function

### 3. Label Column Detection
- **What it checks**: Manifest must have a label column (e.g., `cookie-classification`) with corresponding metadata (e.g., `cookie-classification-metadata`)
- **Error messages**:
  - `No label column found. Ground Truth manifest must have a label attribute (e.g., 'cookie-classification') with corresponding metadata (e.g., 'cookie-classification-metadata')`
  - `Missing label column in entries: 1, 2, 3` (if some entries are missing the label)
  - `Missing metadata column in entries: 1, 2, 3` (if some entries are missing the metadata)
- **Why it matters**: DDA requires standardized label columns for model training

### 4. S3 URI Validation
- **What it checks**: S3 URIs must be properly formatted
- **Issues detected**:
  - Duplicate `s3://` prefixes (e.g., `s3://s3://bucket/path`)
  - Double slashes in paths (e.g., `s3://bucket//path//image.jpg`)
  - Missing `s3://` prefix
- **Warning message**: `⚠️ Malformed S3 URIs detected (will be auto-corrected during transformation):`
- **Why it matters**: Malformed URIs prevent Ground Truth from accessing images. The system auto-corrects these during transformation.

### 5. Segmentation Mask Verification (for Segmentation Tasks)
- **What it checks**: For segmentation manifests, verifies that mask files referenced in the manifest are accessible in S3
- **Error message**: `Segmentation mask file not found: s3://bucket/path/mask.png`
- **Warning message**: `Could not verify segmentation mask file: s3://bucket/path/mask.png (error details)`
- **Why it matters**: Segmentation training requires both images and corresponding mask files. If masks are missing, training will fail.

### 6. Dataset Size Warning
- **What it checks**: Number of images in the dataset
- **Warning message**: `Dataset has fewer than 10 images, which may not be sufficient for training`
- **Why it matters**: Small datasets may not provide enough training data for good model performance

## UI Display

### Success State
When validation passes, users see:
- ✓ Green "Manifest Valid" alert
- Dataset statistics:
  - Total Images count
  - Task Type (classification, detection, segmentation)
  - Label distribution (e.g., "anomaly: 50, normal: 150")

### Error State
When validation fails, users see:
- ✗ Red "Validation Failed" alert with all errors listed
- A separate "⚠ Warnings" alert (if applicable) with warnings
- An "How to Fix" info alert with guidance on resolving common issues

### Example Error Display

```
✗ Validation Failed
Issues Found:
- No label column found. Ground Truth manifest must have a label attribute (e.g., 'cookie-classification') with corresponding metadata (e.g., 'cookie-classification-metadata')
- Entry 1: Invalid JSON - Expecting value: line 1 column 50 (char 49)

⚠ Warnings
- ⚠️ Malformed S3 URIs detected (will be auto-corrected during transformation):
- Entry 1: duplicate 's3://' prefix, double slashes in path - s3://s3://bucket//path//image.jpg

How to Fix
Your manifest file has issues that need to be resolved before creating a dataset:
- Missing label columns: Ensure your manifest has a label column (e.g., "cookie-classification") and its corresponding metadata column (e.g., "cookie-classification-metadata")
- Malformed S3 URIs: Check that S3 paths don't have duplicate "s3://" prefixes or double slashes. Example: "s3://bucket/path/image.jpg" (not "s3://s3://bucket//path//image.jpg")
- Invalid JSON: Ensure each line in the manifest is valid JSON
```

## Manifest Format Requirements

### Classification Manifest (Image Classification)
```json
{"source-ref": "s3://bucket/path/image1.jpg", "cookie-classification": 1, "cookie-classification-metadata": {"job-name": "cookie-classification", "class-name": "anomaly", "human-annotated": "yes", "creation-date": "2026-02-15T11:52:59Z", "type": "groundtruth/image-classification"}}
{"source-ref": "s3://bucket/path/image2.jpg", "cookie-classification": 0, "cookie-classification-metadata": {"job-name": "cookie-classification", "class-name": "normal", "human-annotated": "yes", "creation-date": "2026-02-15T11:52:59Z", "type": "groundtruth/image-classification"}}
```

### Segmentation Manifest (Semantic Segmentation)
For segmentation, you need BOTH the manifest file AND the mask image files in S3:

```json
{"source-ref": "s3://bucket/path/image1.jpg", "cookie-segmentation-ref": "s3://bucket/masks/image1.png", "cookie-segmentation-ref-metadata": {"internal-color-map": {"0": {"class-name": "cracked", "hex-color": "#23A436"}}, "job-name": "cookie-segmentation-ref", "human-annotated": "yes", "creation-date": "2026-02-15T11:52:59Z", "type": "groundtruth/semantic-segmentation"}}
{"source-ref": "s3://bucket/path/image2.jpg", "cookie-segmentation-ref": "s3://bucket/masks/image2.png", "cookie-segmentation-ref-metadata": {"internal-color-map": {"0": {"class-name": "cracked", "hex-color": "#23A436"}}, "job-name": "cookie-segmentation-ref", "human-annotated": "yes", "creation-date": "2026-02-15T11:52:59Z", "type": "groundtruth/semantic-segmentation"}}
```

**Important for Segmentation:**
- Mask files must exist in S3 at the paths specified in `*-ref` fields
- Mask files are typically PNG images where pixel values represent class labels
- The `internal-color-map` in metadata defines which pixel values map to which classes
- Validation will verify that mask files are accessible before allowing dataset creation

### Hybrid Manifest (Classification + Segmentation)
You can have both classification and segmentation data in the same manifest:

```json
{"source-ref": "s3://bucket/path/image1.jpg", "cookie-classification": 1, "cookie-classification-metadata": {...}, "cookie-segmentation-ref": "s3://bucket/masks/image1.png", "cookie-segmentation-ref-metadata": {...}}
```

This allows the same dataset to be used for either classification or segmentation training.

### Common Issues to Avoid
1. **Duplicate S3 prefixes**: ❌ `s3://s3://bucket/path` → ✅ `s3://bucket/path`
2. **Double slashes**: ❌ `s3://bucket//path//image.jpg` → ✅ `s3://bucket/path/image.jpg`
3. **Missing label columns**: ❌ Only `source-ref` → ✅ `source-ref` + label + metadata
4. **Invalid JSON**: ❌ Missing quotes or commas → ✅ Valid JSON on each line
5. **Missing metadata**: ❌ Label without metadata → ✅ Both label and metadata present

## Auto-Correction During Transformation

When a manifest is transformed for use in training, the system automatically corrects:
- Duplicate `s3://` prefixes
- Double slashes in S3 paths
- Job names in metadata to match the standardized label names

This means warnings about malformed S3 URIs are not blocking issues - they'll be fixed automatically.

## Next Steps

1. **Validate your manifest**: Click "Validate Manifest" in the Pre-Labeled Datasets modal
2. **Review any errors**: Fix issues listed in the "Issues Found" section
3. **Check warnings**: Review warnings about malformed URIs (these will be auto-corrected)
4. **Create dataset**: Once validation passes, click "Create Dataset"
5. **Use in training**: The dataset will be available for use in the training workflow

## Troubleshooting

### "No label column found"
- **Cause**: Your manifest doesn't have a label attribute with metadata
- **Fix**: Add a label column (e.g., `"cookie-classification": 1`) and metadata column (e.g., `"cookie-classification-metadata": {...}`) to each entry

### "Missing label column in entries"
- **Cause**: Some entries are missing the label column that other entries have
- **Fix**: Ensure all entries have the same label columns

### "Malformed S3 URIs detected"
- **Cause**: S3 paths have duplicate prefixes or double slashes
- **Fix**: These will be auto-corrected during transformation, but you can also fix them manually in your manifest

### "Invalid JSON"
- **Cause**: A line in the manifest is not valid JSON
- **Fix**: Check the line number and ensure it has proper JSON syntax (quotes, commas, braces)

