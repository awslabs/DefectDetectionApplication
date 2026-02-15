# DDA Sample Datasets

This directory contains sample datasets for training defect detection models with the Defect Detection Application (DDA).

## Sample Datasets Setup Guide

This guide covers setup for both Cookie and Alien datasets, including uploading to S3 and generating manifests.

### Overview

Each dataset includes helper scripts:
- **`upload_dataset.py`** - Uploads dataset images/masks to S3
- **`generate_manifest.py`** - Generates manifest files locally (classification and segmentation)

### Workflow

The typical workflow for each dataset is:

1. **Upload Dataset** - Use `upload_dataset.py` to upload images/masks to S3
2. **Generate Manifests** - Use `generate_manifest.py` to create manifest files locally
3. **Upload Manifests** - Manually upload generated manifests to S3
4. **Register in Portal** - Register the dataset in DDA Portal using the S3 manifest URI
5. **Transform Manifest** (if needed) - Portal automatically detects Ground Truth format and offers transformation
6. **Train Model** - Use the registered dataset to train a model

### Manifest Format Options

The `generate_manifest.py` script supports two manifest formats:

#### Ground Truth Format (Default)
- **Use Case**: Testing the transformation workflow in the portal
- **Attributes**: Job-specific names like `cookie-classification`, `cookie-segmentation-ref`
- **Transformation**: Portal will detect Ground Truth format and offer "Transform Manifest Now" button
- **Command**: `python3 generate_manifest.py s3://bucket/path/ --task both` (default format)

#### DDA Format
- **Use Case**: Ready for training without transformation
- **Attributes**: Standardized names like `anomaly-label`, `anomaly-mask-ref`
- **Transformation**: Not needed - manifests are production-ready
- **Command**: `python3 generate_manifest.py s3://bucket/path/ --task both --format dda`

### Manifest Transformation

When you register a Ground Truth format manifest in the portal:
1. Portal detects the Ground Truth format (job-specific attribute names)
2. A warning alert appears with "Transform Manifest Now" button
3. Click the button to transform the manifest to DDA format
4. Transformed manifest is automatically used for training

**Transformation Details**:
- Renames `{job-name}` → `anomaly-label`
- Renames `{job-name}-metadata` → `anomaly-label-metadata`
- Renames `{job-name}-ref` → `anomaly-mask-ref` (for segmentation)
- Renames `{job-name}-ref-metadata` → `anomaly-mask-ref-metadata` (for segmentation)
- Updates `job-name` fields in metadata to match new attribute names

### Cookie Dataset Setup

#### Dataset Structure

The Cookie Dataset is organized as follows:

```
cookie-dataset/
├── dataset-files/
│   ├── training-images/          # Training images (JPG)
│   │   ├── normal-1.jpg
│   │   ├── normal-2.jpg
│   │   ├── anomaly-1.jpg
│   │   └── anomaly-2.jpg
│   └── mask-images/              # Segmentation masks (PNG) - same filenames as images
│       ├── normal-1.png
│       ├── normal-2.png
│       ├── anomaly-1.png
│       └── anomaly-2.png
├── upload_dataset.py
└── generate_manifest.py
```

#### Upload and Manifest Generation

```bash
cd datasets/cookie-dataset

# Create bucket
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="dda-cookie-dataset-${ACCOUNT_ID}"
aws s3 mb s3://${BUCKET}

# Step 1: Upload dataset images AND masks to S3
# This uploads both training-images/ and mask-images/ directories
python3 upload_dataset.py s3://${BUCKET}/cookies/

# Verify upload
aws s3 ls s3://${BUCKET}/cookies/ --recursive

# Step 2: Generate manifests locally in Ground Truth format (default - for testing transformation)
python3 generate_manifest.py s3://${BUCKET}/cookies/ --task both

# Step 3: Upload manifests to S3
aws s3 cp dataset-files/manifests/train_class.manifest s3://${BUCKET}/cookies/dataset-files/manifests/
aws s3 cp dataset-files/manifests/train_segmentation.manifest s3://${BUCKET}/cookies/dataset-files/manifests/
```

**Manifest Format Options**:
- `--format ground-truth` (default) - Ground Truth format with job-specific attribute names (for testing transformation in portal)
- `--format dda` - DDA format with standardized attribute names (ready for training without transformation)

**Example Commands**:
```bash
# Generate Ground Truth format (default - for testing transformation)
python3 generate_manifest.py s3://${BUCKET}/cookies/ --task both

# Generate DDA format (ready for training without transformation)
python3 generate_manifest.py s3://${BUCKET}/cookies/ --task both --format dda

# Classification only
python3 generate_manifest.py s3://${BUCKET}/cookies/ --task classification

# Segmentation only
python3 generate_manifest.py s3://${BUCKET}/cookies/ --task segmentation
```

**Output Locations**:
- Classification: `dataset-files/manifests/train_class.manifest`
- Segmentation: `dataset-files/manifests/train_segmentation.manifest`

**S3 Structure After Upload**:
```
s3://dda-cookie-dataset-ACCOUNT_ID/
└── cookies/
    ├── training-images/
    │   ├── normal-1.jpg
    │   ├── anomaly-1.jpg
    │   └── ...
    ├── mask-images/
    │   ├── normal-1.png
    │   ├── anomaly-1.png
    │   └── ...
    ├── train_class.manifest
    └── train_segmentation.manifest
```

### Alien Dataset Setup

#### Dataset Structure

The Alien Dataset is organized as follows:

```
alien-dataset/
├── dataset-files/
│   ├── training-images/          # Training images (JPG)
│   │   ├── normal-1.jpg
│   │   ├── anomaly-1.jpg
│   │   └── ...
│   └── mask-images/              # Segmentation masks (PNG) - optional for normal images
│       ├── anomaly-1.png
│       └── ...
├── upload_dataset.py
└── generate_manifest.py
```

#### Upload and Manifest Generation

```bash
cd datasets/alien-dataset

# Create bucket
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="dda-alien-dataset-${ACCOUNT_ID}"
aws s3 mb s3://${BUCKET}

# Step 1: Upload dataset images AND masks to S3
# This uploads both training-images/ and mask-images/ directories
python3 upload_dataset.py s3://${BUCKET}/aliens/

# Verify upload
aws s3 ls s3://${BUCKET}/aliens/ --recursive

# Step 2: Generate manifests locally (creates manifest files with mask references)
python3 generate_manifest.py s3://${BUCKET}/aliens/ --task both

# Step 3: Upload manifests to S3
aws s3 cp train_class.manifest s3://${BUCKET}/aliens/
aws s3 cp train_segmentation.manifest s3://${BUCKET}/aliens/
```

**Manifest Options**:
- `--task classification` - Classification manifest (images only)
- `--task segmentation` - Segmentation manifest (images with mask references; uses dummy masks for normal images)
- `--task both` - Both classification and segmentation manifests

**Output Locations**:
- Classification: `train_class.manifest`
- Segmentation: `train_segmentation.manifest`

**S3 Structure After Upload**:
```
s3://dda-alien-dataset-ACCOUNT_ID/
└── aliens/
    ├── training-images/
    │   ├── normal-1.jpg
    │   ├── anomaly-1.jpg
    │   └── ...
    ├── mask-images/
    │   ├── anomaly-1.png
    │   └── ...
    ├── train_class.manifest
    └── train_segmentation.manifest
```

### Important Notes

- **`upload_dataset.py` uploads BOTH images and masks** - The script automatically uploads both `training-images/` and `mask-images/` directories to S3
- **`generate_manifest.py` does NOT upload to S3** - It only generates manifest files locally. You must manually upload them using `aws s3 cp`
- **Manifest URIs** - Use the S3 path to the manifest file when registering in the portal (e.g., `s3://bucket/cookies/train_segmentation.manifest`)
- **AWS Credentials** - Both scripts use default AWS credentials. Configure with `aws configure` or set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` environment variables

### Segmentation Masks

For segmentation tasks, masks are PNG files with pixel colors representing different classes:

- **Mask Filenames** - Must match image filenames (e.g., `anomaly-1.jpg` → `anomaly-1.png`)
- **Pixel Colors** - Each color represents a class:
  - `#23A436` (green) - Defect/anomaly region
  - `#FFFFFF` (white) - Background/normal region
- **Color Map** - Defined in manifest metadata for Ground Truth interpretation

**Example Manifest Entry with Mask**:
```json
{
  "source-ref": "s3://bucket/cookies/training-images/anomaly-1.jpg",
  "anomaly-label": 1,
  "anomaly-label-metadata": {
    "job-name": "anomaly-label",
    "class-name": "anomaly",
    "human-annotated": "yes",
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
    "type": "groundtruth/semantic-segmentation"
  }
}
```

### Portal Registration and Training

For complete instructions on registering datasets in the portal and training models, see [QUICK_START_DATASET.md](../QUICK_START_DATASET.md).

## Cookie Dataset

The Cookie Dataset is a pre-labeled dataset containing images of cookies with normal and defective examples, along with pixel-level segmentation masks for defect localization.

### Dataset Contents

- **training-images/** - 63 training images (32 anomalies, 31 normal)
- **mask-images/** - Pixel-level segmentation masks for anomalies
- **manifests/template.manifest** - Template manifest file with S3 path placeholders
- **test-images/** - Test images for validation
- **dummy_anomaly_mask.png** - Example mask for normal images


#### 3. Register in DDA Portal

For **Classification** tasks:
1. Go to **Data Management** → **Pre-Labeled Datasets**
2. Click **Register Dataset**
3. Fill in:
   - **Dataset Name**: "Cookie Defect Detection"
   - **S3 Manifest URI**: `s3://dda-cookie-dataset-ACCOUNT_ID/cookies/train_class.manifest`
   - **Description**: "Cookie dataset for classification"
4. Click **Register**

For **Segmentation** tasks:
1. Go to **Data Management** → **Pre-Labeled Datasets**
2. Click **Register Dataset**
3. Fill in:
   - **Dataset Name**: "Cookie Defect Detection (Segmentation)"
   - **S3 Manifest URI**: `s3://dda-cookie-dataset-ACCOUNT_ID/cookies/train_segmentation.manifest`
   - **Description**: "Cookie dataset with pixel-level segmentation masks"
4. Click **Register**

#### 4. Train Model

1. Go to **Training** → **Create Training Job**
2. Select **Pre-Labeled Dataset** as source
3. Choose your registered dataset from dropdown
4. Configure training parameters
5. Click **Start Training**

### Upload Script Options

```bash
# Standard upload
python3 upload_dataset.py s3://my-bucket/cookies/

# Dry run (shows what would be uploaded)
python3 upload_dataset.py s3://my-bucket/cookies/ --dry-run
```

### Troubleshooting

**Error: "AWS credentials not found"**
- Configure AWS credentials: `aws configure`
- Or set environment variables: `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`

**Error: "S3 bucket not found"**
- Verify bucket exists: `aws s3 ls s3://your-bucket-name`
- Create bucket if needed: `aws s3 mb s3://your-bucket-name`

**Error: "Access Denied"**
- Verify your AWS credentials have S3 permissions
- Check bucket policy allows your IAM user/role

### Dataset Statistics

| Metric | Value |
|--------|-------|
| Total Images | 63 |
| Anomalies | 32 |
| Normal | 31 |
| Image Format | JPG |
| Mask Format | PNG |
| Segmentation | Yes (pixel-level masks) |

### Source

This dataset is based on the Cookie Dataset from the [amazon-lookout-for-vision](https://github.com/aws-samples/amazon-lookout-for-vision) repository.

### License

This dataset is provided under the Apache License 2.0. See the LICENSE file in the root directory.
