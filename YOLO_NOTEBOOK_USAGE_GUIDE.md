# YOLO Cookie Defect Detection - Usage Guide

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Quick Start](#quick-start)
4. [Hyperparameter Configuration](#hyperparameter-configuration)
5. [Model Selection Guide](#model-selection-guide)
6. [Troubleshooting](#troubleshooting)
7. [Model Comparison Interpretation](#model-comparison-interpretation)
8. [Cost Optimization](#cost-optimization)

## Overview

This notebook provides an end-to-end pipeline for training and deploying YOLOv8 models for cookie defect detection using Amazon SageMaker. The workflow includes:

- **Dataset Preparation**: Automatic download and conversion of the cookie dataset
- **Model Training**: Custom PyTorch training scripts with configurable hyperparameters
- **Multi-Platform Compilation**: Optimize models for Jetson Xavier GPU, x86_64 CPU, and ARM64 CPU
- **Performance Validation**: Compare YOLO results with Lookout for Vision baseline

### Key Features

- **Two Model Types**: Object detection (bounding boxes) and instance segmentation (pixel masks)
- **Five Model Sizes**: From nano (yolov8n) to extra-large (yolov8x)
- **Three Target Platforms**: GPU-accelerated edge devices, x86 servers, and ARM processors
- **Automated Monitoring**: Built-in status tracking for training and compilation jobs

## Prerequisites

### Required Permissions

Your SageMaker execution role must have:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::sagemaker-*/*",
        "arn:aws:s3:::sagemaker-*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "sagemaker:CreateTrainingJob",
        "sagemaker:DescribeTrainingJob",
        "sagemaker:CreateCompilationJob",
        "sagemaker:DescribeCompilationJob"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "arn:aws:logs:*:*:log-group:/aws/sagemaker/*"
    }
  ]
}
```

### Required Files

Ensure these files are in the same directory as the notebook:

1. **yolo_training.py** - Custom training script for YOLOv8
2. **yolo_format_converter.py** - Utilities for converting annotations
3. **yolo_comparison.py** - Model comparison and visualization functions

### Instance Requirements

- **Notebook Instance**: ml.t3.medium or larger (2 vCPU, 4 GB RAM minimum)
- **Training Instance**: ml.g4dn.4xlarge (GPU instance, automatically configured)
- **Compilation**: Serverless (no instance selection needed)

### Estimated Costs

| Component | Instance Type | Duration | Estimated Cost |
|-----------|--------------|----------|----------------|
| Notebook Instance | ml.t3.medium | 2 hours | $0.09 |
| Training Job | ml.g4dn.4xlarge | 30-60 min | $0.50-1.00 |
| Compilation (3 platforms) | Serverless | 15-30 min | $0.30-0.60 |
| S3 Storage | Standard | <1 GB | <$0.03/month |
| **Total** | | | **~$1.00-2.00** |

*Prices are approximate and vary by region. Check AWS pricing for your specific region.*

## Quick Start

### Step 1: Execute Setup Cells (Section 1)

Run cells 1.1 through 1.3 to:
- Import required libraries
- Initialize SageMaker session
- Create S3 folder structure

**Expected Output**: S3 paths displayed with ✅ confirmation

### Step 2: Acquire Dataset (Section 2)

Run cells 2.1 through 2.4 to:
- Clone the amazon-lookout-for-vision repository
- Extract the cookie dataset
- Validate dataset structure
- Clean up temporary files

**Expected Output**: 
- 63 training images found
- 33 mask images found
- Manifest files listed

### Step 3: Convert to YOLO Format (Section 3)

Run cells 3.1 through 3.5 to:
- Convert Lookout for Vision annotations to YOLO detection format
- Generate YOLO annotation files (.txt)
- Create data.yaml configuration

**Optional**: Run cells 3.6.1 through 3.6.4 for segmentation format conversion

**Expected Output**: 
- Annotation files created for each image
- data.yaml configuration displayed

### Step 4: Upload to S3 (Section 4)

Run cells 4.1 through 4.4 to:
- Upload training images to S3
- Upload YOLO annotations to S3
- Upload data.yaml configuration
- Verify all uploads completed

**Expected Output**: S3 URIs displayed with upload confirmation

### Step 5: Train Model (Section 5)

Run cells 5.1 through 5.4 to:
- Create PyTorch estimator with hyperparameters
- Configure input data channels
- Launch training job
- Monitor training progress

**Expected Duration**: 30-60 minutes depending on hyperparameters

**Expected Output**: 
- Training job ARN
- Progress indicators (dots)
- Trained model S3 URI upon completion

### Step 6: Prepare for Compilation (Section 6)

Run cells 6.1 through 6.3 to:
- Download trained model from S3
- Extract and prepare model artifacts
- Upload prepared model for compilation

**Expected Output**: Compilation-ready model S3 URI

### Step 7: Compile Models (Section 7)

Run cells 7.1, 7.2, and/or 7.3 to compile for:
- **7.1**: Jetson Xavier GPU (CUDA, TensorRT, FP16)
- **7.2**: x86_64 CPU (Intel/AMD servers)
- **7.3**: ARM64 CPU (Raspberry Pi, Graviton)

Then run cell 7.4 to monitor compilation progress.

**Expected Duration**: 5-10 minutes per platform

**Expected Output**: Compiled model S3 URI for each platform

### Step 8: Compare Models (Section 8)

Run cells 8.1 through 8.4 to:
- Load test images
- Run YOLO inference
- Calculate performance metrics
- Visualize results

**Expected Output**: 
- Detection visualizations
- Performance metrics (precision, recall, F1)
- Comparison with Lookout for Vision baseline

## Hyperparameter Configuration

### Model Size Selection

The `model-size` hyperparameter controls the YOLO model architecture:

| Model Size | Parameters | Speed | Accuracy | Use Case |
|------------|-----------|-------|----------|----------|
| **yolov8n** | 3.2M | Fastest | Good | Edge devices, real-time |
| **yolov8s** | 11.2M | Fast | Better | Balanced performance |
| **yolov8m** | 25.9M | Medium | High | Server deployment |
| **yolov8l** | 43.7M | Slow | Higher | High accuracy needs |
| **yolov8x** | 68.2M | Slowest | Highest | Maximum accuracy |

**Recommendation**: Start with `yolov8n` for edge deployment or `yolov8s` for server deployment.

### Task Type Selection

The `task` hyperparameter determines the model type:

| Task | Output | Precision | Speed | Use Case |
|------|--------|-----------|-------|----------|
| **detect** | Bounding boxes | Medium | Fast | Quick defect localization |
| **segment** | Pixel masks | High | Slower | Precise defect boundaries |

**Recommendation**: Use `detect` for real-time applications, `segment` for quality inspection.

### Training Hyperparameters

```python
hyperparameters = {
    'model-size': 'yolov8n',  # Model architecture
    'task': 'detect',          # Detection or segmentation
    'epochs': 50,              # Training iterations
    'batch-size': 16,          # Images per batch
    'img-size': 640,           # Input image size
}
```

#### Epochs

- **Range**: 10-300
- **Default**: 50
- **Impact**: More epochs = better accuracy but longer training time
- **Recommendations**:
  - Quick test: 10-20 epochs
  - Production: 50-100 epochs
  - Fine-tuning: 100-300 epochs

#### Batch Size

- **Range**: 1-64 (limited by GPU memory)
- **Default**: 16
- **Impact**: Larger batches = faster training but more memory
- **Recommendations**:
  - ml.g4dn.4xlarge: 16-32 (16 GB GPU memory)
  - ml.g4dn.8xlarge: 32-64 (32 GB GPU memory)
  - If out-of-memory errors occur, reduce batch size

#### Image Size

- **Range**: 320-1280 (multiples of 32)
- **Default**: 640
- **Impact**: Larger images = better accuracy but slower inference
- **Recommendations**:
  - Edge devices: 320-416
  - Balanced: 640
  - High accuracy: 1024-1280

### Instance Type Selection

For training jobs, you can modify the instance type:

```python
pytorch_estimator = PyTorch(
    # ... other parameters ...
    instance_type='ml.g4dn.4xlarge',  # Change this
    instance_count=1,
    volume_size=20,  # GB
)
```

| Instance Type | vCPU | GPU Memory | Cost/Hour | Use Case |
|---------------|------|------------|-----------|----------|
| ml.g4dn.xlarge | 4 | 16 GB | $0.736 | Small models, testing |
| ml.g4dn.2xlarge | 8 | 16 GB | $0.94 | Medium models |
| **ml.g4dn.4xlarge** | 16 | 16 GB | $1.505 | **Recommended** |
| ml.g4dn.8xlarge | 32 | 32 GB | $2.72 | Large models, big batches |

## Model Selection Guide

### Decision Tree

```
Start
  |
  ├─ Need real-time inference? (>30 FPS)
  |    ├─ Yes → Use yolov8n + detect + img-size 416
  |    └─ No → Continue
  |
  ├─ Need precise defect boundaries?
  |    ├─ Yes → Use segment task
  |    └─ No → Use detect task
  |
  ├─ Deploying to edge device?
  |    ├─ Yes → Use yolov8n or yolov8s
  |    └─ No → Use yolov8m or yolov8l
  |
  └─ Maximum accuracy required?
       ├─ Yes → Use yolov8x + segment + img-size 1024
       └─ No → Use yolov8s or yolov8m
```

### Common Configurations

#### Configuration 1: Edge Device (Real-Time)
```python
hyperparameters = {
    'model-size': 'yolov8n',
    'task': 'detect',
    'epochs': 50,
    'batch-size': 16,
    'img-size': 416,
}
# Compile for: Jetson Xavier GPU or ARM64 CPU
```
**Use Case**: Real-time defect detection on production line
**Performance**: ~50 FPS on Jetson Xavier, ~5 FPS on ARM64 CPU

#### Configuration 2: Server Deployment (Balanced)
```python
hyperparameters = {
    'model-size': 'yolov8s',
    'task': 'detect',
    'epochs': 100,
    'batch-size': 32,
    'img-size': 640,
}
# Compile for: x86_64 CPU
```
**Use Case**: Batch processing on cloud servers
**Performance**: ~20 FPS on x86_64 CPU

#### Configuration 3: Quality Inspection (High Accuracy)
```python
hyperparameters = {
    'model-size': 'yolov8m',
    'task': 'segment',
    'epochs': 100,
    'batch-size': 16,
    'img-size': 1024,
}
# Compile for: Jetson Xavier GPU or x86_64 CPU
```
**Use Case**: Detailed quality inspection with precise defect localization
**Performance**: ~10 FPS on Jetson Xavier, ~2 FPS on x86_64 CPU

## Troubleshooting

### Training Job Failures

#### Error: "ResourceLimitExceeded"
**Cause**: Insufficient GPU memory for batch size
**Solution**: Reduce `batch-size` from 16 to 8 or 4

#### Error: "ClientError: Training job failed"
**Cause**: Various reasons (check CloudWatch logs)
**Solution**: 
1. Click the CloudWatch logs link in the error message
2. Look for Python exceptions or CUDA errors
3. Common fixes:
   - Reduce batch size
   - Reduce image size
   - Check data.yaml path is correct

#### Error: "TimeoutError"
**Cause**: Training exceeded max_run time (2 hours)
**Solution**: Increase `max_run` parameter or reduce `epochs`

### Compilation Job Failures

#### Error: "Model input shape mismatch"
**Cause**: DataInputConfig doesn't match model's expected input
**Solution**: Verify the input shape in model metadata matches DataInputConfig

#### Error: "Framework version not supported"
**Cause**: PyTorch version incompatibility
**Solution**: Ensure training used PyTorch 2.0 (automatically configured)

#### Error: "Compiler options invalid"
**Cause**: Incompatible compiler options for target platform
**Solution**: 
- For Jetson Xavier: Verify CUDA 10.2, TensorRT 8.2.1 compatibility
- For CPU targets: Remove accelerator-specific options

### S3 Upload Failures

#### Error: "Access Denied"
**Cause**: Insufficient S3 permissions
**Solution**: Add S3 permissions to SageMaker execution role

#### Error: "NoSuchBucket"
**Cause**: S3 bucket doesn't exist
**Solution**: Verify `default_bucket` is correct and exists in your region

### Dataset Issues

#### Error: "No training images found"
**Cause**: Dataset extraction failed
**Solution**: 
1. Re-run dataset acquisition cells
2. Verify internet connectivity for GitHub clone
3. Check disk space (need ~500 MB)

## Model Comparison Interpretation

### Understanding the Metrics

#### Precision
- **Definition**: Of all predicted defects, how many were actually defects?
- **Formula**: TP / (TP + FP)
- **Range**: 0.0 to 1.0 (higher is better)
- **Interpretation**:
  - High precision (>0.9): Few false alarms
  - Low precision (<0.7): Many false positives

#### Recall
- **Definition**: Of all actual defects, how many did we detect?
- **Formula**: TP / (TP + FN)
- **Range**: 0.0 to 1.0 (higher is better)
- **Interpretation**:
  - High recall (>0.9): Few missed defects
  - Low recall (<0.7): Many false negatives

#### F1 Score
- **Definition**: Harmonic mean of precision and recall
- **Formula**: 2 * (Precision * Recall) / (Precision + Recall)
- **Range**: 0.0 to 1.0 (higher is better)
- **Interpretation**:
  - F1 > 0.9: Excellent performance
  - F1 0.7-0.9: Good performance
  - F1 < 0.7: Needs improvement

#### Inference Time
- **Definition**: Average time to process one image
- **Unit**: Milliseconds (ms)
- **Interpretation**:
  - <50 ms: Real-time capable (>20 FPS)
  - 50-200 ms: Near real-time (5-20 FPS)
  - >200 ms: Batch processing only (<5 FPS)

### YOLO vs Lookout for Vision

#### Expected Differences

| Metric | YOLO Detection | YOLO Segmentation | LFV Classification | LFV Segmentation |
|--------|----------------|-------------------|-------------------|------------------|
| **Precision** | 0.85-0.95 | 0.90-0.98 | 0.88-0.95 | 0.92-0.98 |
| **Recall** | 0.80-0.92 | 0.85-0.95 | 0.85-0.93 | 0.90-0.97 |
| **F1 Score** | 0.82-0.93 | 0.87-0.96 | 0.86-0.94 | 0.91-0.97 |
| **Inference Time** | 20-50 ms | 50-100 ms | 100-200 ms | 150-300 ms |

#### Interpretation Guide

**YOLO Detection vs LFV Classification**:
- YOLO provides bounding boxes, LFV provides binary classification
- YOLO is typically 2-4x faster
- LFV may have slightly higher accuracy for simple defects
- YOLO better for multiple defects per image

**YOLO Segmentation vs LFV Segmentation**:
- Both provide pixel-level masks
- YOLO is typically 1.5-2x faster
- LFV may have slightly better mask quality
- YOLO offers more deployment flexibility

### When to Choose YOLO

Choose YOLO when you need:
- **Faster inference** for real-time applications
- **Edge deployment** on resource-constrained devices
- **Multiple defect detection** in a single image
- **Flexible deployment** across different hardware platforms
- **Open-source solution** with community support

### When to Choose Lookout for Vision

Choose LFV when you need:
- **Managed service** with minimal setup
- **Highest accuracy** for critical applications
- **AWS integration** with other AWS services
- **No ML expertise** required for training
- **Automatic model updates** and improvements

## Cost Optimization

### Reduce Training Costs

1. **Use Spot Instances** (not shown in notebook, but available):
   ```python
   pytorch_estimator = PyTorch(
       # ... other parameters ...
       use_spot_instances=True,
       max_wait=7200,  # Maximum wait time for spot
   )
   ```
   **Savings**: Up to 70% on training costs

2. **Reduce Epochs for Testing**:
   - Use 10-20 epochs for initial experiments
   - Only use 50-100 epochs for final training

3. **Use Smaller Models**:
   - Start with yolov8n for testing
   - Only use larger models if accuracy is insufficient

### Reduce Compilation Costs

1. **Compile Only Needed Platforms**:
   - Skip platforms you won't deploy to
   - Compilation is charged per job

2. **Batch Compilations**:
   - Compile multiple models together
   - Reuse compiled models across deployments

### Reduce S3 Costs

1. **Clean Up After Experiments**:
   ```python
   # Delete S3 objects after testing
   import boto3
   s3 = boto3.resource('s3')
   bucket = s3.Bucket(default_bucket)
   bucket.objects.filter(Prefix=s3_prefix).delete()
   ```

2. **Use S3 Lifecycle Policies**:
   - Automatically delete old training artifacts
   - Move infrequently accessed models to Glacier

### Monitor Costs

1. **Set Up Billing Alerts**:
   - AWS Budgets: Set alerts for SageMaker spending
   - CloudWatch: Monitor training job costs

2. **Track Job Costs**:
   - Training: ~$0.50-1.00 per job
   - Compilation: ~$0.10-0.20 per job
   - Total for notebook: ~$1.00-2.00

## Additional Resources

### Documentation
- [YOLOv8 Documentation](https://docs.ultralytics.com/)
- [SageMaker Training Documentation](https://docs.aws.amazon.com/sagemaker/latest/dg/train-model.html)
- [SageMaker Neo Documentation](https://docs.aws.amazon.com/sagemaker/latest/dg/neo.html)

### Example Notebooks
- [SageMaker PyTorch Examples](https://github.com/aws/amazon-sagemaker-examples/tree/main/sagemaker-python-sdk/pytorch_cnn_cifar10)
- [SageMaker Neo Examples](https://github.com/aws/amazon-sagemaker-examples/tree/main/sagemaker_neo_compilation_jobs)

### Support
- [AWS SageMaker Forums](https://forums.aws.amazon.com/forum.jspa?forumID=285)
- [Ultralytics GitHub Issues](https://github.com/ultralytics/ultralytics/issues)
- [AWS Support](https://console.aws.amazon.com/support/)

## Conclusion

This notebook provides a complete pipeline for training and deploying YOLO models for defect detection. By following the hyperparameter guidelines and model selection recommendations, you can optimize for your specific use case - whether that's real-time edge deployment, high-accuracy quality inspection, or cost-effective batch processing.

For questions or issues, refer to the troubleshooting section or consult the additional resources listed above.
