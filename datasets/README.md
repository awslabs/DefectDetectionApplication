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
5. **Train Model** - Use the registered dataset to train a model

### Cookie Dataset Setup

```bash
cd datasets/cookie-dataset

# Create bucket
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="dda-cookie-dataset-${ACCOUNT_ID}"
aws s3 mb s3://${BUCKET}

# Step 1: Upload dataset images and masks
python3 upload_dataset.py s3://${BUCKET}/cookies/

# Step 2: Generate manifests locally
python3 generate_manifest.py s3://${BUCKET}/cookies/ --task both

# Step 3: Upload manifests to S3
aws s3 cp train_class.manifest s3://${BUCKET}/cookies/
aws s3 cp train_segmentation.manifest s3://${BUCKET}/cookies/
```

**Manifest Options**:
- `--task classification` - Images only (no masks)
- `--task segmentation` - Images with pixel-level masks
- `--task both` - Both classification and segmentation manifests

**Output Locations**:
- Classification: `train_class.manifest`
- Segmentation: `train_segmentation.manifest`

### Alien Dataset Setup

```bash
cd datasets/alien-dataset

# Create bucket
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="dda-alien-dataset-${ACCOUNT_ID}"
aws s3 mb s3://${BUCKET}

# Step 1: Upload dataset images
python3 upload_dataset.py s3://${BUCKET}/aliens/

# Step 2: Generate manifests locally
python3 generate_manifest.py s3://${BUCKET}/aliens/ --task both

# Step 3: Upload manifests to S3
aws s3 cp train_class.manifest s3://${BUCKET}/aliens/
aws s3 cp train_segmentation.manifest s3://${BUCKET}/aliens/
```

**Manifest Options**:
- `--task classification` - Images only
- `--task segmentation` - Images with dummy masks for normal, actual masks for anomalies
- `--task both` - Both classification and segmentation manifests

**Output Locations**:
- Classification: `train_class.manifest`
- Segmentation: `train_segmentation.manifest`

### Important Notes

- **`generate_manifest.py` does NOT upload to S3** - It only generates manifest files locally. You must manually upload them using `aws s3 cp`.
- **Manifest URIs** - Use the S3 path to the manifest file when registering in the portal (e.g., `s3://bucket/cookies/manifests/train_class.manifest`)
- **AWS Credentials** - Both scripts use default AWS credentials. Configure with `aws configure` or set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` environment variables.

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

1. Go to **Data Management** → **Pre-Labeled Datasets**
2. Click **Register Dataset**
3. Fill in:
   - **Dataset Name**: "Cookie Defect Detection"
   - **S3 Manifest URI**: `s3://dda-cookie-dataset-ACCOUNT_ID/cookies/manifests/train.manifest`
   - **Description**: "Cookie dataset with segmentation masks for defect detection"
4. Click **Register**

#### 4. Train Model

1. Go to **Training** → **Create Training Job**
2. Select **Pre-Labeled Dataset** as source
3. Choose "Cookie Defect Detection" from dropdown
4. Configure training parameters
5. Click **Start Training**

### Manifest Format

The dataset uses the DDA manifest format with segmentation support:

```json
{
  "source-ref": "s3://bucket/cookies/training-images/anomaly-1.jpg",
  "anomaly-label-metadata": {
    "job-name": "anomaly-label",
    "class-name": "anomaly",
    "human-annotated": "yes",
    "type": "groundtruth/image-classification"
  },
  "anomaly-label": 1,
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
  },
  "anomaly-mask-ref": "s3://bucket/cookies/mask-images/anomaly-1.png"
}
```

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
