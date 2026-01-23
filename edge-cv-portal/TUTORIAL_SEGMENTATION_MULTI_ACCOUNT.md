# Tutorial: Segmentation Model Training with Multi-Account Architecture

A complete end-to-end guide for training pixel-level defect segmentation models using the DDA Portal with multi-account architecture.

## Overview

This tutorial demonstrates how to:
- Set up Ground Truth for semantic segmentation labeling
- Label images with pixel-level masks
- Transform Ground Truth segmentation manifests to DDA format
- Train a segmentation model using AWS Marketplace algorithm
- Deploy the model to edge devices

**Architecture:**
- **Portal Account** (`164152369890`) - Hosts the DDA Portal
- **UseCase Account** (`198226511894`) - Runs SageMaker training, stores outputs
- **Data Account** (`814373574263`) - Stores training images (input data)

**S3 Bucket Strategy:**
- `dda-alien-bucket` (Data Account) - Training images
- `dda-alien-output` (UseCase Account) - SageMaker outputs, models, manifests

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    SEGMENTATION WORKFLOW                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  1. SETUP (Prerequisites)                                                   │
│     ├── Complete multi-account setup from TUTORIAL_MULTI_ACCOUNT_WORKFLOW   │
│     ├── Create Ground Truth workteam                                        │
│     └── Upload images to Data Account                                       │
│                                                                              │
│  2. SEGMENTATION LABELING                                                   │
│     ├── Create segmentation labeling job                                    │
│     ├── Paint pixel-level masks on defects                                  │
│     └── Wait for job completion                                             │
│                                                                              │
│  3. MANIFEST TRANSFORMATION                                                 │
│     ├── Download Ground Truth output manifest                               │
│     ├── Transform to DDA-compatible format                                  │
│     └── Verify transformation results                                       │
│                                                                              │
│  4. SEGMENTATION TRAINING                                                   │
│     ├── Create training job with segmentation model type                    │
│     ├── Monitor training progress                                           │
│     └── Review model metrics                                                │
│                                                                              │
│  5. DEPLOYMENT                                                              │
│     ├── Compile model for target architecture                               │
│     ├── Package as Greengrass component                                     │
│     └── Deploy to edge devices                                              │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Prerequisites

Before starting this tutorial, you must complete:

1. **Multi-Account Setup**: Follow [TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md](TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md) through Part 1 (One-Time Setup)
2. **Data Preparation**: Complete Part 2 (Data Preparation) to upload images
3. **Ground Truth Workteam**: Create a private workteam (Part 3, Step 3.1)

**Required Accounts:**
- Portal Account: `164152369890`
- UseCase Account: `198226511894`
- Data Account: `814373574263`

**Required Buckets:**
- `dda-alien-bucket` (Data Account) - Input images
- `dda-alien-output` (UseCase Account) - Outputs

---

## Part 1: Understanding Segmentation

### What is Semantic Segmentation?

Unlike classification (whole image) or object detection (bounding boxes), semantic segmentation identifies defects at the **pixel level**. Each pixel is classified as either:
- **Normal** (0) - No defect
- **Defect** (1) - Part of a defect

### When to Use Segmentation

Use segmentation when you need:
- **Precise defect boundaries** - Exact shape and size of defects
- **Multiple defects per image** - Detect all defect regions
- **Defect area measurement** - Calculate defect size in pixels
- **Defect localization** - Know exactly where defects are

### Segmentation vs Classification

| Feature | Classification | Segmentation |
|---------|---------------|--------------|
| Output | Whole image label | Pixel-level mask |
| Precision | Image-level | Pixel-level |
| Training Time | Faster (~30 min) | Slower (~1-2 hours) |
| Labeling Effort | Low (click label) | High (paint masks) |
| Use Case | "Is there a defect?" | "Where exactly is the defect?" |

---

## Part 2: Segmentation Labeling

### Step 2.1: Create Segmentation Labeling Job

1. In the DDA Portal, go to **Labeling** → **Create Labeling Job**
2. Fill in the form:

**Job Configuration:**
- Job Name: `alien-defect-segmentation`
- Task Type: **Segmentation** (Semantic Segmentation)

**Dataset Selection:**
- UseCase: `Manufacturing Line 1`
- S3 Dataset Prefix: `datasets/alien/`

The portal will discover images from both `normal/` and `anomaly/` folders in the Data Account bucket.

**Label Configuration:**
- Add label: `defect`
- Add label: `background` (optional, for clarity)

**Workforce:**
- Select your private workteam ARN

**Instructions (Optional):**
```
Paint over all defect areas using the "defect" label.
Be as precise as possible with the boundaries.
Leave normal areas unpainted (background).
```

3. Click **Create Labeling Job**

The portal will:
- Generate a manifest from images in `s3://dda-alien-bucket/datasets/alien/`
- Upload manifest to `s3://dda-alien-output/manifests/`
- Create Ground Truth segmentation job in UseCase Account
- Store job metadata in DynamoDB

### Step 2.2: Label Images with Pixel Masks

1. Team members receive email with labeling portal link
2. Open the Ground Truth labeling UI
3. For each image:
   - Use the **paint brush** tool to paint over defect areas
   - Select the `defect` label
   - Adjust brush size as needed for precision
   - Use **zoom** for fine details
   - Use **undo** if you make mistakes
4. Submit each annotation

**Labeling Tips:**
- Start with larger defects to get familiar with the tool
- Use smaller brush sizes for edges and fine details
- Zoom in to ensure accurate boundaries
- Paint all defect regions, even small ones
- Leave normal areas unpainted

### Step 2.3: Monitor Labeling Progress

In the Portal:
1. Go to **Labeling** → Click on your job
2. View progress:
   - Total images: 118
   - Labeled: (updates in real-time)
   - Remaining: (calculated)
3. Status: `InProgress`

**How Many Images to Label?**
- Minimum: 50 images (25 normal, 25 defect)
- Recommended: 80-100 images for better accuracy
- You can stop early if needed - Ground Truth saves progress

### Step 2.4: Stop Labeling Job

Once you have enough labeled images:

1. Go to AWS Console → SageMaker → Ground Truth → Labeling jobs
2. Select your job: `dda-alien-defect-segmentation-XXXXXXXX`
3. Click **Actions** → **Stop**
4. Confirm

Ground Truth will:
- Finalize the output manifest
- Save to: `s3://dda-alien-output/labeled/labeling-XXXXXXXX/dda-alien-defect-segmentation-XXXXXXXX/manifests/output/output.manifest`

---

## Part 3: Manifest Transformation

### Step 3.1: Understand Manifest Format

**Ground Truth Output Format:**
```json
{
  "source-ref": "s3://dda-alien-bucket/datasets/alien/anomaly/1.png",
  "dda-alien-defect-segmentation-XXXXXXXX": 0,
  "dda-alien-defect-segmentation-XXXXXXXX-metadata": {
    "class-name": "defect",
    "job-name": "labeling-job/dda-alien-defect-segmentation-XXXXXXXX",
    "confidence": 0.95,
    "type": "groundtruth/semantic-segmentation",
    "human-annotated": "yes",
    "creation-date": "2026-01-23T10:30:00.000000"
  },
  "dda-alien-defect-segmentation-XXXXXXXX-ref": "s3://dda-alien-output/labeled/.../masks/1.png",
  "dda-alien-defect-segmentation-XXXXXXXX-ref-metadata": {
    "internal-color-map": {
      "0": {"class-name": "BACKGROUND"},
      "1": {"class-name": "defect"}
    },
    "type": "groundtruth/semantic-segmentation",
    "job-name": "labeling-job/dda-alien-defect-segmentation-XXXXXXXX",
    "human-annotated": "yes",
    "creation-date": "2026-01-23T10:30:00.000000"
  }
}
```

**DDA Required Format:**
```json
{
  "source-ref": "s3://dda-alien-bucket/datasets/alien/anomaly/1.png",
  "anomaly-label": 0,
  "anomaly-label-metadata": {
    "class-name": "defect",
    "job-name": "anomaly-label",
    "confidence": 0.95,
    "type": "groundtruth/semantic-segmentation",
    "human-annotated": "yes",
    "creation-date": "2026-01-23T10:30:00.000000"
  },
  "anomaly-mask-ref": "s3://dda-alien-output/labeled/.../masks/1.png",
  "anomaly-mask-ref-metadata": {
    "internal-color-map": {
      "0": {"class-name": "BACKGROUND"},
      "1": {"class-name": "defect"}
    },
    "type": "groundtruth/semantic-segmentation",
    "job-name": "anomaly-mask-ref",
    "human-annotated": "yes",
    "creation-date": "2026-01-23T10:30:00.000000"
  }
}
```

**Key Transformations:**
1. Rename label attribute: `{job-name}` → `anomaly-label`
2. Rename label metadata: `{job-name}-metadata` → `anomaly-label-metadata`
3. Rename mask reference: `{job-name}-ref` → `anomaly-mask-ref`
4. Rename mask metadata: `{job-name}-ref-metadata` → `anomaly-mask-ref-metadata`
5. Update `job-name` field in label metadata to `"anomaly-label"`
6. Update `job-name` field in mask metadata to `"anomaly-mask-ref"`

### Step 3.2: Find Output Manifest

In the UseCase Account (`198226511894`):

```bash
# List labeling job outputs
aws s3 ls s3://dda-alien-output/labeled/ --recursive | grep output.manifest

# Example output:
# 2026-01-23 10:45:31  36210 labeled/labeling-ab2b7dfd/dda-alien-defect-segmentation-9f5e8300/manifests/output/output.manifest
```

Copy the full S3 URI:
```
s3://dda-alien-output/labeled/labeling-ab2b7dfd/dda-alien-defect-segmentation-9f5e8300/manifests/output/output.manifest
```

### Step 3.3: Transform Manifest Using Portal

1. In the DDA Portal, go to **Labeling** → **Manifest Transformer**
2. Fill in the form:

**Source Manifest:**
- S3 URI: `s3://dda-alien-output/labeled/labeling-ab2b7dfd/dda-alien-defect-segmentation-9f5e8300/manifests/output/output.manifest`

**Task Type:**
- Select: **Segmentation**

**Output Manifest (Optional):**
- Leave blank to auto-generate: `output-dda.manifest` in same location
- Or specify custom location: `s3://dda-alien-output/manifests/alien-segmentation-dda.manifest`

3. Click **Transform Manifest**

The portal will:
- Download the Ground Truth manifest
- Auto-detect attribute names (e.g., `dda-alien-defect-segmentation-9f5e8300`)
- Transform all 5 required attributes:
  - Label value and metadata
  - Mask reference and metadata
- Update `job-name` fields in both metadata objects
- Upload transformed manifest
- Return statistics

**Expected Response:**
```json
{
  "message": "Manifest transformed successfully",
  "transformed_manifest_uri": "s3://dda-alien-output/labeled/.../output-dda.manifest",
  "stats": {
    "total_entries": 104,
    "transformed": 104,
    "skipped": 0,
    "errors": []
  },
  "detected_attributes": {
    "label_attr": "dda-alien-defect-segmentation-9f5e8300",
    "metadata_attr": "dda-alien-defect-segmentation-9f5e8300-metadata",
    "mask_ref_attr": "dda-alien-defect-segmentation-9f5e8300-ref",
    "mask_ref_metadata_attr": "dda-alien-defect-segmentation-9f5e8300-ref-metadata"
  },
  "dda_attributes": {
    "label": "anomaly-label",
    "metadata": "anomaly-label-metadata",
    "mask_ref": "anomaly-mask-ref",
    "mask_ref_metadata": "anomaly-mask-ref-metadata"
  },
  "sample_entry": { ... }
}
```

### Step 3.4: Verify Transformation

Download and inspect the transformed manifest:

```bash
# Download transformed manifest
aws s3 cp s3://dda-alien-output/labeled/.../output-dda.manifest ./output-dda.manifest

# Check first entry
head -1 output-dda.manifest | jq .
```

**Verify:**
- ✅ Has `anomaly-label` (not job-specific name)
- ✅ Has `anomaly-label-metadata` with `job-name: "anomaly-label"`
- ✅ Has `anomaly-mask-ref` (mask image S3 URI)
- ✅ Has `anomaly-mask-ref-metadata` with `job-name: "anomaly-mask-ref"`
- ✅ Has `source-ref` (original image S3 URI)

---

## Part 4: Segmentation Training

### Step 4.1: Create Training Job

1. In the DDA Portal, go to **Training** → **Create Training Job**
2. Fill in the form:

**Model Configuration:**
- Model Name: `alien-defect-segmentation`
- Model Version: `1.0.0`
- Model Source: **AWS Marketplace** (default)
- Model Type: **Segmentation** (or **Segmentation-Robust** for better accuracy)

**Dataset:**
- Dataset Manifest S3 URI: `s3://dda-alien-output/labeled/.../output-dda.manifest`

**Training Configuration:**
- Instance Type: `ml.p3.2xlarge` (GPU recommended)
- Max Runtime: `7200` seconds (2 hours - segmentation takes longer)

**Auto-Compilation (Optional):**
- Enable: ✅ Auto-compile after training
- Targets: `arm64-cpu` (for ARM devices)

3. Click **Start Training**

The portal will:
- **Validate manifest format** - Check for all 5 required attributes
- Assume UseCase Account role
- Create SageMaker training job with segmentation hyperparameters
- Store job metadata in DynamoDB

**Manifest Validation:**

The portal automatically validates that your manifest has:
- `source-ref` - Image S3 URI
- `anomaly-label` - Label value
- `anomaly-label-metadata` - Label metadata
- `anomaly-mask-ref` - Mask image S3 URI
- `anomaly-mask-ref-metadata` - Mask metadata

If validation fails, you'll see:
```json
{
  "error": "Manifest validation failed",
  "details": [
    "Missing required attributes: anomaly-mask-ref, anomaly-mask-ref-metadata"
  ],
  "suggestion": "Use the Manifest Transformer tool to convert your Ground Truth manifest to DDA-compatible format"
}
```

### Step 4.2: Training Job Configuration

The portal automatically configures the training job with:

**Hyperparameters:**
```python
{
  "ModelType": "segmentation",  # or "segmentation-robust"
  "TrainingInputDataAttributeNames": "source-ref,anomaly-label-metadata,anomaly-label,anomaly-mask-ref-metadata,anomaly-mask-ref",
  "TestInputDataAttributeNames": "source-ref,anomaly-label-metadata,anomaly-label,anomaly-mask-ref-metadata,anomaly-mask-ref"
}
```

**Input Data Config:**
```python
{
  "ChannelName": "training",
  "DataSource": {
    "S3DataSource": {
      "S3DataType": "AugmentedManifestFile",
      "S3Uri": "s3://dda-alien-output/labeled/.../output-dda.manifest",
      "AttributeNames": [
        "source-ref",
        "anomaly-label-metadata",
        "anomaly-label",
        "anomaly-mask-ref-metadata",
        "anomaly-mask-ref"
      ]
    }
  }
}
```

**Critical Requirements:**
- All 5 attributes must match in THREE places:
  1. Manifest file attribute names
  2. `TrainingInputDataAttributeNames` hyperparameter
  3. `AttributeNames` in InputDataConfig
- The `job-name` field in metadata must match attribute names:
  - `anomaly-label-metadata` → `job-name: "anomaly-label"`
  - `anomaly-mask-ref-metadata` → `job-name: "anomaly-mask-ref"`

### Step 4.3: Monitor Training

1. Go to **Training** → Click on your job
2. View real-time status:
   - Status: `InProgress` → `Completed`
   - Progress: 0% → 100%
   - Training logs (CloudWatch)
   - Metrics: loss, accuracy, IoU (Intersection over Union)

**Expected Timeline:**
- Job creation: ~1 minute
- Instance provisioning: ~5 minutes
- Training: ~1-2 hours (depends on dataset size)
- Model upload: ~2 minutes
- Total: ~1.5-2.5 hours

### Step 4.4: Review Model Metrics

After training completes:

1. View final metrics in the Training Detail page:
   - **IoU (Intersection over Union)**: Measures mask overlap accuracy
   - **Pixel Accuracy**: Percentage of correctly classified pixels
   - **Loss**: Training loss (lower is better)

2. Check model artifacts:
   ```bash
   aws s3 ls s3://dda-alien-output/sagemaker/training/alien-defect-segmentation-1.0.0/output/
   ```

3. Download model:
   ```bash
   aws s3 cp s3://dda-alien-output/sagemaker/training/alien-defect-segmentation-1.0.0/output/model.tar.gz ./
   ```

---

## Part 5: Compilation & Deployment

### Step 5.1: Compile Model

Follow the same process as classification models:

1. Go to **Training** → Select your completed job
2. Click **Compile** tab
3. Select target: `arm64-cpu` (for ARM64 devices)
4. Click **Start Compilation**

Compilation takes 5-15 minutes.

### Step 5.2: Package as Greengrass Component

1. Click **Package** tab
2. Click **Start Packaging**

This creates a Greengrass-compatible component package.

### Step 5.3: Publish to Greengrass

1. Click **Publish** tab
2. Enter:
   - Component Name: `com.example.alien-defect-segmentation`
   - Component Version: `1.0.0`
   - Friendly Name: `Alien Defect Segmentation`
3. Click **Publish**

### Step 5.4: Deploy to Edge Device

1. Go to **Deployments** → **Create Deployment**
2. Select device: `dda_edge_server_1`
3. Add component: `com.example.alien-defect-segmentation` (version `1.0.0`)
4. Click **Create Deployment**

Monitor deployment status until `SUCCEEDED`.

---

## Part 6: Testing Segmentation Model

### Step 6.1: Test Inference

SSH to your edge device and test:

```bash
# Test with a defect image
curl -X POST http://localhost:5000/predict \
  -F "image=@test_defect.jpg" \
  -o result.json

# View results
cat result.json | jq .
```

**Expected Response:**
```json
{
  "prediction": "anomaly",
  "confidence": 0.95,
  "mask_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA...",
  "defect_pixels": 1234,
  "total_pixels": 307200,
  "defect_percentage": 0.40,
  "bounding_boxes": [
    {
      "x": 120,
      "y": 80,
      "width": 50,
      "height": 30,
      "confidence": 0.95
    }
  ]
}
```

### Step 6.2: Visualize Segmentation Mask

The response includes a base64-encoded mask image. To visualize:

```python
import json
import base64
from PIL import Image
import io

# Load result
with open('result.json') as f:
    result = json.load(f)

# Decode mask
mask_data = result['mask_url'].split(',')[1]
mask_bytes = base64.b64decode(mask_data)
mask_image = Image.open(io.BytesIO(mask_bytes))

# Display
mask_image.show()

# Or save
mask_image.save('defect_mask.png')
```

The mask image shows:
- **Black pixels (0)**: Normal areas
- **White pixels (255)**: Defect areas

---

## Troubleshooting

### Manifest Validation Fails

**Error:** "Missing required attributes: anomaly-mask-ref, anomaly-mask-ref-metadata"

**Cause:** Manifest is not in DDA format or transformation failed.

**Fix:**
1. Verify you're using the **transformed** manifest (with `-dda` suffix)
2. Re-run manifest transformation with task_type: `segmentation`
3. Check transformation stats for errors

### Training Fails with "AttributeNames mismatch"

**Error:** "The AttributeNames in InputDataConfig do not match the manifest"

**Cause:** Manifest attributes don't match the expected names.

**Fix:**
1. Verify manifest has all 5 attributes:
   ```bash
   head -1 output-dda.manifest | jq 'keys'
   ```
2. Should see: `["source-ref", "anomaly-label", "anomaly-label-metadata", "anomaly-mask-ref", "anomaly-mask-ref-metadata"]`
3. If not, re-transform the manifest

### Training Takes Too Long

**Issue:** Training running for >3 hours

**Causes:**
- Large dataset (>200 images)
- High-resolution images (>1024x1024)
- Segmentation-Robust model (more complex)

**Solutions:**
1. Reduce dataset size for initial testing
2. Resize images to 512x512 or 768x768
3. Use `segmentation` instead of `segmentation-robust`
4. Increase `max_runtime_seconds` to 10800 (3 hours)

### Low Segmentation Accuracy

**Issue:** Model has low IoU or pixel accuracy

**Causes:**
- Insufficient training data
- Inconsistent labeling
- Imbalanced dataset (too many normal vs defect)

**Solutions:**
1. Label more images (aim for 100+)
2. Ensure consistent mask boundaries
3. Balance dataset: 50% normal, 50% defect
4. Use `segmentation-robust` model type
5. Review labeling quality - re-label if needed

---

## Multi-Account Considerations

### Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                      DATA FLOW DIAGRAM                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Data Account (814373574263)                                    │
│  ├── dda-alien-bucket/                                          │
│  │   └── datasets/alien/                                        │
│  │       ├── normal/*.png         (input images)                │
│  │       └── anomaly/*.png        (input images)                │
│  │                                                               │
│  │   [Cross-account access via DDAPortalDataAccessRole]         │
│  │                                                               │
│  ↓                                                               │
│                                                                  │
│  UseCase Account (198226511894)                                 │
│  ├── SageMaker Training Job                                     │
│  │   ├── Reads images from Data Account                         │
│  │   ├── Reads manifest from UseCase Account                    │
│  │   └── Writes outputs to UseCase Account                      │
│  │                                                               │
│  └── dda-alien-output/                                          │
│      ├── manifests/                (input manifests)            │
│      ├── labeled/                  (GT outputs + masks)         │
│      ├── sagemaker/training/       (model artifacts)            │
│      └── models/                   (compiled models)            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Security Best Practices

1. **Separate Data Account**: Keep training data isolated
2. **External IDs**: Always use unique external IDs for cross-account roles
3. **Least Privilege**: Data Account role only has read access to specific bucket
4. **Audit Logging**: All cross-account access is logged in CloudTrail

### Cost Optimization

**Data Account:**
- S3 storage: ~$0.023/GB/month
- Data transfer to UseCase Account: Free (same region)

**UseCase Account:**
- SageMaker training: ~$3.06/hour (ml.p3.2xlarge)
- S3 storage: ~$0.023/GB/month
- Data transfer: Free (same region)

**Estimated Costs:**
- Labeling: $0 (private workteam)
- Training (2 hours): ~$6.12
- Storage (10GB): ~$0.23/month
- Total per model: ~$6.35

---

## Summary

You've completed the full segmentation workflow:

1. ✅ Created segmentation labeling job in Ground Truth
2. ✅ Labeled images with pixel-level masks
3. ✅ Transformed Ground Truth manifest to DDA format (5 attributes)
4. ✅ Validated manifest format before training
5. ✅ Trained segmentation model with AWS Marketplace algorithm
6. ✅ Compiled and deployed to edge device
7. ✅ Tested pixel-level defect detection

**Key Differences from Classification:**
- **Labeling**: Paint masks vs click labels (more effort)
- **Manifest**: 5 attributes vs 3 attributes
- **Training**: 1-2 hours vs 30 minutes
- **Output**: Pixel mask vs image label
- **Accuracy**: Pixel-level precision vs image-level

**When to Use Segmentation:**
- Need exact defect boundaries
- Measure defect size/area
- Multiple defects per image
- Precise localization required

---

## Next Steps

- **Improve Accuracy**: Label more images, balance dataset
- **Fine-tune**: Adjust hyperparameters for better IoU
- **Scale Deployment**: Deploy to multiple devices
- **Monitor Production**: Track inference performance
- **Update Models**: Retrain with new data

---

## Related Documentation

- [TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md](TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md) - Multi-account setup
- [MANIFEST_VALIDATION_FEATURE.md](MANIFEST_VALIDATION_FEATURE.md) - Manifest validation details
- [DATA_ACCOUNTS_DEPLOYMENT.md](DATA_ACCOUNTS_DEPLOYMENT.md) - Data Account setup
- [ADMIN_GUIDE.md](ADMIN_GUIDE.md) - Portal administration
- [CHANGELOG.md](CHANGELOG.md) - Version history

---

## References

- [AWS Blog: Train Custom CV Defect Detection Model](https://aws.amazon.com/blogs/machine-learning/train-custom-computer-vision-defect-detection-model-using-amazon-sagemaker/)
- [SageMaker Ground Truth Documentation](https://docs.aws.amazon.com/sagemaker/latest/dg/sms.html)
- [Semantic Segmentation Labeling](https://docs.aws.amazon.com/sagemaker/latest/dg/sms-semantic-segmentation.html)
- [AWS Marketplace: Computer Vision Defect Detection](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6)
