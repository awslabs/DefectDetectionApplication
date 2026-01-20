# YOLO Cookie Defect Detection - Complete Documentation

## Quick Links

- **Main Notebook**: `YOLO_Cookie_Defect_Detection.ipynb` - Complete end-to-end pipeline
- **Usage Guide**: `YOLO_NOTEBOOK_USAGE_GUIDE.md` - Detailed instructions and hyperparameter documentation
- **Training Script**: `yolo_training.py` - Custom PyTorch training script for SageMaker
- **Format Converter**: `yolo_format_converter.py` - Utilities for annotation conversion
- **Comparison Tools**: `yolo_comparison.py` - Model evaluation and visualization

## What This Project Does

This project trains and deploys YOLOv8 object detection models for cookie defect detection using Amazon SageMaker. It provides:

1. **Automated Dataset Preparation** - Downloads and converts the cookie dataset from Lookout for Vision format to YOLO format
2. **Flexible Model Training** - Supports 5 model sizes (nano to extra-large) and 2 tasks (detection and segmentation)
3. **Multi-Platform Compilation** - Optimizes models for Jetson Xavier GPU, x86_64 CPU, and ARM64 CPU
4. **Performance Validation** - Compares YOLO results with Lookout for Vision baseline

## Getting Started

### Prerequisites

1. **AWS Account** with SageMaker access
2. **SageMaker Notebook Instance** (ml.t3.medium or larger)
3. **IAM Role** with permissions for S3, SageMaker, and CloudWatch

### Installation

1. Clone or download this repository to your SageMaker notebook instance
2. Ensure all required files are present:
   - `YOLO_Cookie_Defect_Detection.ipynb`
   - `yolo_training.py`
   - `yolo_format_converter.py`
   - `yolo_comparison.py`
   - `test_yolo_notebook.py` (for testing)

3. Open `YOLO_Cookie_Defect_Detection.ipynb` in Jupyter

### Running the Notebook

1. **Read the Usage Guide** first: `YOLO_NOTEBOOK_USAGE_GUIDE.md`
2. **Execute cells sequentially** from top to bottom
3. **Monitor training jobs** using the provided monitoring cells
4. **Review results** in the model comparison section

**Total Runtime**: 1-2 hours (including training and compilation)

**Estimated Cost**: $1.00-2.00 per complete run

## Project Structure

```
.
├── YOLO_Cookie_Defect_Detection.ipynb  # Main notebook
├── YOLO_NOTEBOOK_USAGE_GUIDE.md        # Detailed usage instructions
├── YOLO_COOKIE_README.md               # This file
├── yolo_training.py                    # Custom training script
├── yolo_format_converter.py            # Format conversion utilities
├── yolo_comparison.py                  # Model comparison tools
├── test_yolo_notebook.py               # Unit and property tests
└── .kiro/specs/yolo-cookie-defect-detection/
    ├── requirements.md                 # Feature requirements
    ├── design.md                       # Design document
    └── tasks.md                        # Implementation tasks
```

## Key Features

### Model Types

- **YOLO Detection**: Fast bounding box detection (~20-50ms inference)
- **YOLO Segmentation**: Precise pixel-level masks (~50-100ms inference)

### Model Sizes

| Model | Parameters | Speed | Accuracy | Best For |
|-------|-----------|-------|----------|----------|
| yolov8n | 3.2M | Fastest | Good | Edge devices |
| yolov8s | 11.2M | Fast | Better | Balanced |
| yolov8m | 25.9M | Medium | High | Servers |
| yolov8l | 43.7M | Slow | Higher | High accuracy |
| yolov8x | 68.2M | Slowest | Highest | Maximum accuracy |

### Target Platforms

- **Jetson Xavier GPU**: NVIDIA edge device with CUDA acceleration
- **x86_64 CPU**: Standard Intel/AMD server processors
- **ARM64 CPU**: ARM-based edge devices (Raspberry Pi, AWS Graviton)

## Hyperparameter Quick Reference

### Common Configurations

**Edge Device (Real-Time)**:
```python
hyperparameters = {
    'model-size': 'yolov8n',
    'task': 'detect',
    'epochs': 50,
    'batch-size': 16,
    'img-size': 416,
}
```

**Server Deployment (Balanced)**:
```python
hyperparameters = {
    'model-size': 'yolov8s',
    'task': 'detect',
    'epochs': 100,
    'batch-size': 32,
    'img-size': 640,
}
```

**Quality Inspection (High Accuracy)**:
```python
hyperparameters = {
    'model-size': 'yolov8m',
    'task': 'segment',
    'epochs': 100,
    'batch-size': 16,
    'img-size': 1024,
}
```

## Workflow Overview

```
1. Environment Setup (5 min)
   ↓
2. Dataset Acquisition (5 min)
   ↓
3. Format Conversion (10 min)
   ↓
4. S3 Upload (5 min)
   ↓
5. Model Training (30-60 min)
   ↓
6. Model Preparation (5 min)
   ↓
7. Model Compilation (15-30 min)
   ↓
8. Model Comparison (10 min)
```

## Expected Results

### Performance Metrics

| Metric | YOLO Detection | YOLO Segmentation | LFV Classification |
|--------|----------------|-------------------|-------------------|
| Precision | 0.85-0.95 | 0.90-0.98 | 0.88-0.95 |
| Recall | 0.80-0.92 | 0.85-0.95 | 0.85-0.93 |
| F1 Score | 0.82-0.93 | 0.87-0.96 | 0.86-0.94 |
| Inference Time | 20-50 ms | 50-100 ms | 100-200 ms |

### Model Artifacts

After completion, you'll have:

1. **Trained Model** (S3): PyTorch model weights (.pt file)
2. **Compiled Models** (S3): Optimized models for each target platform
3. **Training Metrics** (CloudWatch): Loss curves, accuracy metrics
4. **Comparison Results** (Notebook): Visualizations and performance tables

## Troubleshooting

### Common Issues

**Training Job Fails**:
- Check CloudWatch logs for detailed error messages
- Reduce batch size if out-of-memory errors occur
- Verify data.yaml configuration is correct

**Compilation Job Fails**:
- Verify model input shape matches DataInputConfig
- Check PyTorch version compatibility (2.0 required)
- Ensure compiler options match target platform

**S3 Upload Fails**:
- Verify IAM role has S3 permissions
- Check S3 bucket exists and is in the same region
- Ensure sufficient disk space for uploads

**Dataset Not Found**:
- Re-run dataset acquisition cells
- Verify internet connectivity for GitHub clone
- Check disk space (need ~500 MB)

For detailed troubleshooting, see `YOLO_NOTEBOOK_USAGE_GUIDE.md`.

## Testing

The project includes comprehensive tests:

```bash
# Run all tests
pytest test_yolo_notebook.py -v

# Run only unit tests
pytest test_yolo_notebook.py -v -k "not property"

# Run only property-based tests
pytest test_yolo_notebook.py -v -k "property"
```

### Test Coverage

- **Unit Tests**: Specific examples and edge cases
- **Property Tests**: Universal correctness properties (100+ iterations each)
- **Integration Tests**: End-to-end notebook execution

## Cost Optimization

### Reduce Costs

1. **Use Spot Instances**: Save up to 70% on training costs
2. **Reduce Epochs**: Use 10-20 epochs for testing, 50-100 for production
3. **Compile Only Needed Platforms**: Skip platforms you won't deploy to
4. **Clean Up S3**: Delete old artifacts after experimentation

### Monitor Costs

- Set up AWS Budgets for SageMaker spending alerts
- Track individual job costs in SageMaker console
- Expected total cost: ~$1.00-2.00 per complete run

## Additional Resources

### Documentation

- [YOLOv8 Official Docs](https://docs.ultralytics.com/)
- [SageMaker Training Guide](https://docs.aws.amazon.com/sagemaker/latest/dg/train-model.html)
- [SageMaker Neo Guide](https://docs.aws.amazon.com/sagemaker/latest/dg/neo.html)
- [Lookout for Vision](https://docs.aws.amazon.com/lookout-for-vision/)

### Example Code

- [SageMaker PyTorch Examples](https://github.com/aws/amazon-sagemaker-examples/tree/main/sagemaker-python-sdk/pytorch_cnn_cifar10)
- [YOLOv8 Training Examples](https://github.com/ultralytics/ultralytics/tree/main/examples)

### Support

- [AWS SageMaker Forums](https://forums.aws.amazon.com/forum.jspa?forumID=285)
- [Ultralytics GitHub](https://github.com/ultralytics/ultralytics/issues)
- [AWS Support Center](https://console.aws.amazon.com/support/)

## Contributing

This project was developed following the spec-driven development methodology:

1. **Requirements** (`.kiro/specs/yolo-cookie-defect-detection/requirements.md`)
2. **Design** (`.kiro/specs/yolo-cookie-defect-detection/design.md`)
3. **Tasks** (`.kiro/specs/yolo-cookie-defect-detection/tasks.md`)

All requirements are validated through property-based testing using Hypothesis.

## License

This project uses the cookie dataset from the [amazon-lookout-for-vision](https://github.com/aws-samples/amazon-lookout-for-vision) repository, which is licensed under the MIT-0 License.

YOLOv8 is provided by Ultralytics under the AGPL-3.0 license.

## Acknowledgments

- **Dataset**: Amazon Lookout for Vision cookie dataset
- **Model**: YOLOv8 by Ultralytics
- **Platform**: Amazon SageMaker
- **Testing**: Hypothesis property-based testing framework

## Next Steps

After completing this notebook:

1. **Deploy Models**: Use SageMaker endpoints or edge deployment
2. **Fine-Tune**: Adjust hyperparameters based on results
3. **Scale Up**: Train on larger datasets or custom data
4. **Integrate**: Connect to production systems or IoT devices
5. **Monitor**: Set up CloudWatch alarms for model performance

For detailed deployment instructions, refer to:
- [SageMaker Deployment Guide](https://docs.aws.amazon.com/sagemaker/latest/dg/deploy-model.html)
- [SageMaker Edge Manager](https://docs.aws.amazon.com/sagemaker/latest/dg/edge.html)

## Contact

For questions or issues:
1. Check the troubleshooting section in `YOLO_NOTEBOOK_USAGE_GUIDE.md`
2. Review the design document for implementation details
3. Consult AWS SageMaker documentation
4. Open an issue in the project repository

---

**Happy Training! 🚀**
