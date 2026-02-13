# Manifest Generation Guide

This guide explains how to generate DDA-compatible manifest files for both Cookie and Alien datasets, supporting classification and segmentation tasks.

## Overview

Manifest files are JSONL (JSON Lines) format files that describe your dataset to the DDA training pipeline. Each line is a JSON object containing:
- Image S3 URI
- Label (0 for normal, 1 for anomaly)
- Label metadata
- (Optional) Segmentation mask S3 URI and metadata

## Cookie Dataset

### Directory Structure

```
datasets/cookie-dataset/
├── dataset-files/
│   ├── training-images/     # 63 training images
│   ├── mask-images/         # 32 segmentation masks
│   └── manifests/           # Generated manifest files
├── test-images/             # Test images
├── generate_manifest.py      # Manifest generator
└── upload_dataset.py         # S3 upload script
```

### Generate Classification Manifest

Classification manifest includes only images (no masks):

```bash
cd datasets/cookie-dataset

# Generate classification manifest
python3 generate_manifest.py s3://dda-cookie-dataset-123456789/cookies/ --task classification

# Output: dataset-files/manifests/train_class.manifest
```

**Manifest Format:**
```json
{
  "source-ref": "s3://bucket/cookies/training-images/anomaly-1.jpg",
  "anomaly-label": 1,
  "anomaly-label-metadata": {
    "job-name": "anomaly-label",
    "class-name": "anomaly",
    "human-annotated": "yes",
    "creation-date": "2024-01-15T10:30:45.123456Z",
    "type": "groundtruth/image-classification"
  }
}
```

### Generate Segmentation Manifest

Segmentation manifest includes images with pixel-level masks:

```bash
cd datasets/cookie-dataset

# Generate segmentation manifest
python3 generate_manifest.py s3://dda-cookie-dataset-123456789/cookies/ --task segmentation

# Output: dataset-files/manifests/train_segmentation.manifest
```

**Manifest Format:**
```json
{
  "source-ref": "s3://bucket/cookies/training-images/anomaly-1.jpg",
  "anomaly-label": 1,
  "anomaly-label-metadata": {
    "job-name": "anomaly-label",
    "class-name": "anomaly",
    "human-annotated": "yes",
    "creation-date": "2024-01-15T10:30:45.123456Z",
    "type": "groundtruth/image-classification"
  },
  "anomaly-mask-ref": "s3://bucket/cookies/mask-images/anomaly-1.png",
  "anomaly-mask-ref-metadata": {
    "internal-color-map": {
      "0": {
        "class-name": "cracked",
        "hex-color": "#23A436"
      }
    },
    "job-name": "labeling-job/object-mask-ref",
    "human-annotated": "yes",
    "creation-date": "2024-01-15T10:30:45.123456Z",
    "type": "groundtruth/semantic-segmentation"
  }
}
```

### Generate Both Manifests

```bash
cd datasets/cookie-dataset

# Generate both classification and segmentation manifests
python3 generate_manifest.py s3://dda-cookie-dataset-123456789/cookies/ --task both

# Output:
# - dataset-files/manifests/train_class.manifest
# - dataset-files/manifests/train_segmentation.manifest
```

## Alien Dataset

### Directory Structure

```
datasets/alien-dataset/
├── normal/                  # Normal alien images
├── anomaly/                 # Anomalous alien images
├── masks/                   # (Optional) Segmentation masks
├── generate_manifest.py     # Manifest generator
└── upload_dataset.py        # S3 upload script
```

### Generate Classification Manifest

```bash
cd datasets/alien-dataset

# Generate classification manifest
python3 generate_manifest.py s3://dda-alien-dataset-123456789/aliens/ --task classification

# Output: train_class.manifest
```

**Manifest Format:**
```json
{
  "source-ref": "s3://bucket/aliens/normal/alien-1.jpg",
  "anomaly-label": 0,
  "anomaly-label-metadata": {
    "job-name": "anomaly-label",
    "class-name": "normal",
    "human-annotated": "yes",
    "creation-date": "2024-01-15T10:30:45.123456Z",
    "type": "groundtruth/image-classification"
  }
}
```

### Generate Segmentation Manifest

For Alien dataset, segmentation uses dummy masks for normal images and actual masks for anomalies (if available):

```bash
cd datasets/alien-dataset

# Generate segmentation manifest
python3 generate_manifest.py s3://dda-alien-dataset-123456789/aliens/ --task segmentation

# Output: train_segmentation.manifest
```

**Manifest Format (Normal Image with Dummy Mask):**
```json
{
  "source-ref": "s3://bucket/aliens/normal/alien-1.jpg",
  "anomaly-label": 0,
  "anomaly-label-metadata": {
    "job-name": "anomaly-label",
    "class-name": "normal",
    "human-annotated": "yes",
    "creation-date": "2024-01-15T10:30:45.123456Z",
    "type": "groundtruth/image-classification"
  },
  "anomaly-mask-ref": "s3://bucket/aliens/masks/dummy_normal_mask.png",
  "anomaly-mask-ref-metadata": {
    "internal-color-map": {
      "0": {
        "class-name": "BACKGROUND",
        "hex-color": "#ffffff"
      }
    },
    "job-name": "labeling-job/dummy-mask",
    "human-annotated": "yes",
    "creation-date": "2024-01-15T10:30:45.123456Z",
    "type": "groundtruth/semantic-segmentation"
  }
}
```

### Generate Both Manifests

```bash
cd datasets/alien-dataset

# Generate both classification and segmentation manifests
python3 generate_manifest.py s3://dda-alien-dataset-123456789/aliens/ --task both

# Output:
# - train_class.manifest
# - train_segmentation.manifest
```

## Complete Workflow

### Step 1: Upload Dataset to S3

```bash
# Cookie dataset
cd datasets/cookie-dataset
python3 upload_dataset.py s3://dda-cookie-dataset-123456789/cookies/

# Alien dataset
cd ../alien-dataset
python3 upload_dataset.py s3://dda-alien-dataset-123456789/aliens/
```

### Step 2: Generate Manifests

```bash
# Cookie dataset - both classification and segmentation
cd ../cookie-dataset
python3 generate_manifest.py s3://dda-cookie-dataset-123456789/cookies/ --task both

# Alien dataset - both classification and segmentation
cd ../alien-dataset
python3 generate_manifest.py s3://dda-alien-dataset-123456789/aliens/ --task both
```

### Step 3: Upload Manifests to S3

```bash
# Cookie dataset
cd ../cookie-dataset
aws s3 cp dataset-files/manifests/train_class.manifest \
  s3://dda-cookie-dataset-123456789/cookies/manifests/

aws s3 cp dataset-files/manifests/train_segmentation.manifest \
  s3://dda-cookie-dataset-123456789/cookies/manifests/

# Alien dataset
cd ../alien-dataset
aws s3 cp train_class.manifest \
  s3://dda-alien-dataset-123456789/aliens/

aws s3 cp train_segmentation.manifest \
  s3://dda-alien-dataset-123456789/aliens/
```

### Step 4: Register in DDA Portal

1. Go to **Data Management** → **Pre-Labeled Datasets**
2. Click **Register Dataset**
3. Fill in the form:
   - **Dataset Name**: e.g., "Cookie Classification" or "Alien Segmentation"
   - **S3 Manifest URI**: Path to your manifest file
   - **Description**: Brief description of the dataset
4. Click **Register**

### Step 5: Create Training Job

1. Go to **Training** → **Create Training Job**
2. Select **Pre-Labeled Dataset** as source
3. Choose your registered dataset
4. Configure training parameters
5. Click **Start Training**

## Manifest File Locations

### Cookie Dataset

| Task | Local Path | S3 Path |
|------|-----------|---------|
| Classification | `dataset-files/manifests/train_class.manifest` | `s3://bucket/cookies/manifests/train_class.manifest` |
| Segmentation | `dataset-files/manifests/train_segmentation.manifest` | `s3://bucket/cookies/manifests/train_segmentation.manifest` |

### Alien Dataset

| Task | Local Path | S3 Path |
|------|-----------|---------|
| Classification | `train_class.manifest` | `s3://bucket/aliens/train_class.manifest` |
| Segmentation | `train_segmentation.manifest` | `s3://bucket/aliens/train_segmentation.manifest` |

## Troubleshooting

### Error: "No images found"

**Cause**: Images not in expected directories

**Solution**:
```bash
# Verify directory structure
ls -la datasets/cookie-dataset/dataset-files/training-images/
ls -la datasets/alien-dataset/normal/
ls -la datasets/alien-dataset/anomaly/
```

### Error: "Mask not found"

**Cause**: Segmentation mask missing for an image

**Solution**:
- For Cookie: Ensure mask exists in `mask-images/` with same name as image
- For Alien: Script uses dummy masks if actual masks don't exist

### Manifest has 0 entries

**Cause**: No images found or wrong file format

**Solution**:
```bash
# Check image files
find datasets/cookie-dataset/dataset-files/training-images/ -type f
find datasets/alien-dataset/normal/ -type f
find datasets/alien-dataset/anomaly/ -type f

# Verify file extensions are .jpg, .jpeg, or .png
```

## Advanced Usage

### Custom Manifest Output Path

The scripts save manifests to default locations. To use custom paths:

1. Generate manifest as usual
2. Copy to desired location:
   ```bash
   cp dataset-files/manifests/train_class.manifest my-custom-manifest.jsonl
   ```

### Verify Manifest Format

```bash
# Check first entry
head -1 dataset-files/manifests/train_class.manifest | python3 -m json.tool

# Count entries
wc -l dataset-files/manifests/train_class.manifest

# Validate JSON format
python3 -c "
import json
with open('dataset-files/manifests/train_class.manifest') as f:
    for i, line in enumerate(f):
        try:
            json.loads(line)
        except json.JSONDecodeError as e:
            print(f'Error on line {i+1}: {e}')
"
```

## Next Steps

- [DATASET_SETUP_INSTRUCTIONS.md](DATASET_SETUP_INSTRUCTIONS.md) - Complete setup guide
- [QUICK_START_DATASET.md](QUICK_START_DATASET.md) - Quick reference
- [README.md](README.md) - Main DDA documentation
