# Bring Your Own Model (BYOM) Guide

This guide explains how to package pre-trained PyTorch models from various platforms for import into the Edge CV Portal.

## Table of Contents

- [Overview](#overview)
- [Required File Structure](#required-file-structure)
- [File Specifications](#file-specifications)
- [Examples](#examples)
  - [YOLOv10 from Databricks](#example-1-yolov10-from-databricks)
  - [ResNet Classification from SageMaker](#example-2-resnet-classification-from-sagemaker)
  - [EfficientNet from Azure ML](#example-3-efficientnet-from-azure-ml)
  - [Custom Anomaly Detection from Local Training](#example-4-custom-anomaly-detection-from-local-training)
  - [Segmentation Model from Hugging Face](#example-5-segmentation-model-from-hugging-face)
- [Export Scripts](#export-scripts)
- [Validation Checklist](#validation-checklist)
- [Troubleshooting](#troubleshooting)

---

## Overview

The Edge CV Portal supports importing pre-trained PyTorch models through the BYOM (Bring Your Own Model) feature. This allows you to:

1. Train models on any platform (Databricks, SageMaker, Azure ML, local, etc.)
2. Package them in the DDA-compatible format
3. Import into the portal for compilation and edge deployment

**Supported Framework:** PyTorch 1.8+

**Supported Compilation Targets:**
- `x86_64-cpu` - Intel/AMD 64-bit processors
- `x86_64-cuda` - NVIDIA GPU on x86_64
- `arm64-cpu` - ARM 64-bit processors (Raspberry Pi, etc.)
- `jetson-xavier` - NVIDIA Jetson Xavier

---

## Required File Structure

```
model.tar.gz
├── config.yaml                    # Image dimensions and dataset info
├── mochi.json                     # Model graph definition
└── export_artifacts/
    ├── manifest.json              # Compilation metadata
    └── <model_name>.pt            # PyTorch model file
```

---

## File Specifications

### 1. config.yaml

Contains image dimensions used during training.

```yaml
dataset:
  image_width: <int>    # Required: Input image width
  image_height: <int>   # Required: Input image height
  # Optional fields
  num_classes: <int>
  class_names: [...]
```

**Validation Rules:**
- `image_width` and `image_height` must be positive integers
- Values must match `input_shape[3]` (width) and `input_shape[2]` (height) in mochi.json

### 2. mochi.json

Defines the model graph and input/output shapes.

```json
{
  "stages": [
    {
      "type": "<model_type>",           // Required: e.g., "classification", "object_detection"
      "input_shape": [B, C, H, W],      // Required: [batch, channels, height, width]
      "output_shape": [...]             // Optional: Model output shape
    }
  ],
  "model_info": {                       // Optional metadata
    "name": "...",
    "version": "...",
    "framework": "pytorch"
  }
}
```

**Input Shape Format:** `[batch_size, channels, height, width]`
- Example: `[1, 3, 640, 640]` for a 640x640 RGB image

### 3. export_artifacts/manifest.json

Contains compilation metadata and preprocessing info.

```json
{
  "model_graph": {
    "stages": [
      {
        "type": "<model_type>",
        "input_shape": [B, C, H, W]
      }
    ]
  },
  "input_shape": [B, C, H, W],
  "compilable_models": [
    {
      "filename": "<model_file>.pt",
      "data_input_config": {
        "input": [B, C, H, W]
      },
      "framework": "PYTORCH"
    }
  ],
  "preprocessing": { ... },    // Optional
  "postprocessing": { ... }    // Optional
}
```

### 4. export_artifacts/<model_name>.pt

The PyTorch model file. Can be:
- A state dict (`model.state_dict()`)
- A traced model (`torch.jit.trace()`)
- A scripted model (`torch.jit.script()`)

**Requirements:**
- Must be compatible with PyTorch 1.8
- Single `.pt` file in the `export_artifacts/` directory

---

## Examples

### Example 1: YOLOv10 from Databricks

**Use Case:** Object detection for manufacturing defects

**Directory Structure:**
```
yolov10-defect-detector.tar.gz
├── config.yaml
├── mochi.json
└── export_artifacts/
    ├── manifest.json
    └── yolov10_defect.pt
```

**config.yaml:**
```yaml
dataset:
  image_width: 640
  image_height: 640
  num_classes: 5
  class_names:
    - scratch
    - dent
    - crack
    - stain
    - good
```

**mochi.json:**
```json
{
  "stages": [
    {
      "type": "yolov10_object_detection",
      "input_shape": [1, 3, 640, 640],
      "output_shape": [1, 84, 8400],
      "num_classes": 5,
      "confidence_threshold": 0.25,
      "nms_threshold": 0.45
    }
  ],
  "model_info": {
    "name": "YOLOv10-Defect-Detector",
    "version": "1.0.0",
    "framework": "pytorch",
    "trained_on": "databricks"
  }
}
```

**export_artifacts/manifest.json:**
```json
{
  "model_graph": {
    "stages": [
      {
        "type": "yolov10_object_detection",
        "input_shape": [1, 3, 640, 640],
        "output_shape": [1, 84, 8400]
      }
    ]
  },
  "input_shape": [1, 3, 640, 640],
  "compilable_models": [
    {
      "filename": "yolov10_defect.pt",
      "data_input_config": {
        "input": [1, 3, 640, 640]
      },
      "framework": "PYTORCH"
    }
  ],
  "preprocessing": {
    "resize": [640, 640],
    "normalize": {
      "mean": [0.0, 0.0, 0.0],
      "std": [1.0, 1.0, 1.0]
    },
    "channel_order": "RGB"
  },
  "postprocessing": {
    "type": "yolo_nms",
    "confidence_threshold": 0.25,
    "nms_threshold": 0.45
  }
}
```

---

### Example 2: ResNet Classification from SageMaker

**Use Case:** Binary classification (good/defect) for quality inspection

**Directory Structure:**
```
resnet50-quality-classifier.tar.gz
├── config.yaml
├── mochi.json
└── export_artifacts/
    ├── manifest.json
    └── resnet50_classifier.pt
```

**config.yaml:**
```yaml
dataset:
  image_width: 224
  image_height: 224
  num_classes: 2
  class_names:
    - good
    - defect
```

**mochi.json:**
```json
{
  "stages": [
    {
      "type": "classification",
      "input_shape": [1, 3, 224, 224],
      "output_shape": [1, 2],
      "num_classes": 2
    }
  ],
  "model_info": {
    "name": "ResNet50-Quality-Classifier",
    "version": "1.0.0",
    "framework": "pytorch",
    "architecture": "resnet50",
    "trained_on": "sagemaker"
  }
}
```

**export_artifacts/manifest.json:**
```json
{
  "model_graph": {
    "stages": [
      {
        "type": "classification",
        "input_shape": [1, 3, 224, 224],
        "output_shape": [1, 2]
      }
    ]
  },
  "input_shape": [1, 3, 224, 224],
  "compilable_models": [
    {
      "filename": "resnet50_classifier.pt",
      "data_input_config": {
        "input": [1, 3, 224, 224]
      },
      "framework": "PYTORCH"
    }
  ],
  "preprocessing": {
    "resize": [224, 224],
    "normalize": {
      "mean": [0.485, 0.456, 0.406],
      "std": [0.229, 0.224, 0.225]
    },
    "channel_order": "RGB"
  },
  "postprocessing": {
    "type": "softmax",
    "threshold": 0.5
  }
}
```

---

### Example 3: EfficientNet from Azure ML

**Use Case:** Multi-class classification for product categorization

**Directory Structure:**
```
efficientnet-product-classifier.tar.gz
├── config.yaml
├── mochi.json
└── export_artifacts/
    ├── manifest.json
    └── efficientnet_b0.pt
```

**config.yaml:**
```yaml
dataset:
  image_width: 256
  image_height: 256
  num_classes: 10
  class_names:
    - product_a
    - product_b
    - product_c
    - product_d
    - product_e
    - product_f
    - product_g
    - product_h
    - product_i
    - product_j
```

**mochi.json:**
```json
{
  "stages": [
    {
      "type": "multi_class_classification",
      "input_shape": [1, 3, 256, 256],
      "output_shape": [1, 10],
      "num_classes": 10
    }
  ],
  "model_info": {
    "name": "EfficientNet-Product-Classifier",
    "version": "2.0.0",
    "framework": "pytorch",
    "architecture": "efficientnet_b0",
    "trained_on": "azure_ml"
  }
}
```

**export_artifacts/manifest.json:**
```json
{
  "model_graph": {
    "stages": [
      {
        "type": "multi_class_classification",
        "input_shape": [1, 3, 256, 256],
        "output_shape": [1, 10]
      }
    ]
  },
  "input_shape": [1, 3, 256, 256],
  "compilable_models": [
    {
      "filename": "efficientnet_b0.pt",
      "data_input_config": {
        "input": [1, 3, 256, 256]
      },
      "framework": "PYTORCH"
    }
  ],
  "preprocessing": {
    "resize": [256, 256],
    "normalize": {
      "mean": [0.485, 0.456, 0.406],
      "std": [0.229, 0.224, 0.225]
    },
    "channel_order": "RGB"
  },
  "postprocessing": {
    "type": "argmax"
  }
}
```

---

### Example 4: Custom Anomaly Detection from Local Training

**Use Case:** Anomaly detection for surface inspection (similar to Lookout for Vision)

**Directory Structure:**
```
anomaly-detector.tar.gz
├── config.yaml
├── mochi.json
└── export_artifacts/
    ├── manifest.json
    └── anomaly_model.pt
```

**config.yaml:**
```yaml
dataset:
  image_width: 224
  image_height: 224
  num_classes: 2
  class_names:
    - normal
    - anomaly
```

**mochi.json:**
```json
{
  "stages": [
    {
      "type": "anomaly_detection",
      "input_shape": [1, 3, 224, 224],
      "output_shape": [1, 2],
      "anomaly_threshold": 0.5
    }
  ],
  "model_info": {
    "name": "Surface-Anomaly-Detector",
    "version": "1.0.0",
    "framework": "pytorch",
    "architecture": "custom_autoencoder",
    "trained_on": "local"
  }
}
```

**export_artifacts/manifest.json:**
```json
{
  "model_graph": {
    "stages": [
      {
        "type": "anomaly_detection",
        "input_shape": [1, 3, 224, 224],
        "output_shape": [1, 2]
      }
    ]
  },
  "input_shape": [1, 3, 224, 224],
  "compilable_models": [
    {
      "filename": "anomaly_model.pt",
      "data_input_config": {
        "input": [1, 3, 224, 224]
      },
      "framework": "PYTORCH"
    }
  ],
  "preprocessing": {
    "resize": [224, 224],
    "normalize": {
      "mean": [0.5, 0.5, 0.5],
      "std": [0.5, 0.5, 0.5]
    },
    "channel_order": "RGB"
  },
  "postprocessing": {
    "type": "anomaly_score",
    "threshold": 0.5
  }
}
```

---

### Example 5: Segmentation Model from Hugging Face

**Use Case:** Semantic segmentation for defect localization

**Directory Structure:**
```
segmentation-model.tar.gz
├── config.yaml
├── mochi.json
└── export_artifacts/
    ├── manifest.json
    └── segformer_b0.pt
```

**config.yaml:**
```yaml
dataset:
  image_width: 512
  image_height: 512
  num_classes: 3
  class_names:
    - background
    - defect_type_a
    - defect_type_b
```

**mochi.json:**
```json
{
  "stages": [
    {
      "type": "semantic_segmentation",
      "input_shape": [1, 3, 512, 512],
      "output_shape": [1, 3, 512, 512],
      "num_classes": 3
    }
  ],
  "model_info": {
    "name": "SegFormer-Defect-Segmentation",
    "version": "1.0.0",
    "framework": "pytorch",
    "architecture": "segformer_b0",
    "trained_on": "huggingface",
    "source": "nvidia/segformer-b0-finetuned-ade-512-512"
  }
}
```

**export_artifacts/manifest.json:**
```json
{
  "model_graph": {
    "stages": [
      {
        "type": "semantic_segmentation",
        "input_shape": [1, 3, 512, 512],
        "output_shape": [1, 3, 512, 512]
      }
    ]
  },
  "input_shape": [1, 3, 512, 512],
  "compilable_models": [
    {
      "filename": "segformer_b0.pt",
      "data_input_config": {
        "input": [1, 3, 512, 512]
      },
      "framework": "PYTORCH"
    }
  ],
  "preprocessing": {
    "resize": [512, 512],
    "normalize": {
      "mean": [0.485, 0.456, 0.406],
      "std": [0.229, 0.224, 0.225]
    },
    "channel_order": "RGB"
  },
  "postprocessing": {
    "type": "argmax_per_pixel"
  }
}
```

---

## Export Scripts

### Generic Export Script

```python
import torch
import yaml
import json
import tarfile
import os
import shutil
from typing import List, Optional, Tuple

def export_model_for_dda(
    model: torch.nn.Module,
    output_path: str,
    model_name: str,
    model_type: str,
    input_shape: Tuple[int, int, int, int],
    output_shape: Optional[List[int]] = None,
    num_classes: Optional[int] = None,
    class_names: Optional[List[str]] = None,
    preprocessing: Optional[dict] = None,
    postprocessing: Optional[dict] = None,
    metadata: Optional[dict] = None
):
    """
    Export a PyTorch model to DDA-compatible format.
    
    Args:
        model: PyTorch model (nn.Module)
        output_path: Path for output tar.gz file
        model_name: Name for the model file
        model_type: Type of model (e.g., "classification", "object_detection")
        input_shape: Input shape as (batch, channels, height, width)
        output_shape: Optional output shape
        num_classes: Number of classes
        class_names: List of class names
        preprocessing: Preprocessing configuration
        postprocessing: Postprocessing configuration
        metadata: Additional metadata
    """
    
    batch, channels, height, width = input_shape
    
    # Create temp directory
    temp_dir = f"/tmp/dda_export_{model_name}"
    export_dir = os.path.join(temp_dir, "export_artifacts")
    os.makedirs(export_dir, exist_ok=True)
    
    # 1. Create config.yaml
    config = {
        "dataset": {
            "image_width": width,
            "image_height": height,
        }
    }
    if num_classes:
        config["dataset"]["num_classes"] = num_classes
    if class_names:
        config["dataset"]["class_names"] = class_names
    
    with open(os.path.join(temp_dir, "config.yaml"), "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    
    # 2. Create mochi.json
    stage = {
        "type": model_type,
        "input_shape": list(input_shape),
    }
    if output_shape:
        stage["output_shape"] = output_shape
    if num_classes:
        stage["num_classes"] = num_classes
    
    mochi = {
        "stages": [stage],
        "model_info": {
            "name": model_name,
            "version": "1.0.0",
            "framework": "pytorch",
            **(metadata or {})
        }
    }
    
    with open(os.path.join(temp_dir, "mochi.json"), "w") as f:
        json.dump(mochi, f, indent=2)
    
    # 3. Create manifest.json
    pt_filename = f"{model_name}.pt"
    manifest = {
        "model_graph": {
            "stages": [stage]
        },
        "input_shape": list(input_shape),
        "compilable_models": [
            {
                "filename": pt_filename,
                "data_input_config": {
                    "input": list(input_shape)
                },
                "framework": "PYTORCH"
            }
        ]
    }
    if preprocessing:
        manifest["preprocessing"] = preprocessing
    if postprocessing:
        manifest["postprocessing"] = postprocessing
    
    with open(os.path.join(export_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    
    # 4. Save model
    model.eval()
    torch.save(model.state_dict(), os.path.join(export_dir, pt_filename))
    
    # 5. Create tar.gz
    with tarfile.open(output_path, "w:gz") as tar:
        for item in os.listdir(temp_dir):
            tar.add(os.path.join(temp_dir, item), arcname=item)
    
    # Cleanup
    shutil.rmtree(temp_dir)
    
    print(f"✅ Model exported to: {output_path}")
    return output_path


# Example usage:
# export_model_for_dda(
#     model=my_model,
#     output_path="my-model.tar.gz",
#     model_name="my_classifier",
#     model_type="classification",
#     input_shape=(1, 3, 224, 224),
#     output_shape=[1, 10],
#     num_classes=10,
#     class_names=["class_0", "class_1", ...],
#     preprocessing={
#         "resize": [224, 224],
#         "normalize": {"mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225]}
#     }
# )
```

### Databricks Export Script

```python
# Run in Databricks notebook

import torch
import mlflow

def export_databricks_model(
    run_id: str,
    model_artifact_path: str,
    output_path: str,
    **kwargs
):
    """Export a model from MLflow to DDA format."""
    
    # Load model from MLflow
    model_uri = f"runs:/{run_id}/{model_artifact_path}"
    model = mlflow.pytorch.load_model(model_uri)
    
    # Export using generic function
    export_model_for_dda(model=model, output_path=output_path, **kwargs)
    
    # Upload to S3
    dbutils.fs.cp(f"file:{output_path}", f"s3://my-bucket/models/{os.path.basename(output_path)}")

# Usage:
# export_databricks_model(
#     run_id="abc123",
#     model_artifact_path="model",
#     output_path="/tmp/my-model.tar.gz",
#     model_name="yolov10_defect",
#     model_type="object_detection",
#     input_shape=(1, 3, 640, 640)
# )
```

### SageMaker Export Script

```python
# Run in SageMaker notebook

import sagemaker
import boto3

def export_sagemaker_model(
    model_data_s3: str,
    output_s3: str,
    **kwargs
):
    """Export a SageMaker model to DDA format."""
    
    # Download model from S3
    s3 = boto3.client('s3')
    bucket, key = model_data_s3.replace("s3://", "").split("/", 1)
    
    local_model = "/tmp/model.tar.gz"
    s3.download_file(bucket, key, local_model)
    
    # Extract and load model
    import tarfile
    with tarfile.open(local_model, "r:gz") as tar:
        tar.extractall("/tmp/model")
    
    model = torch.load("/tmp/model/model.pth")
    
    # Export to DDA format
    output_local = "/tmp/dda-model.tar.gz"
    export_model_for_dda(model=model, output_path=output_local, **kwargs)
    
    # Upload to S3
    out_bucket, out_key = output_s3.replace("s3://", "").split("/", 1)
    s3.upload_file(output_local, out_bucket, out_key)
```

---

## Validation Checklist

Before importing your model, verify:

- [ ] **File Structure**
  - [ ] `config.yaml` exists at root
  - [ ] `mochi.json` exists at root
  - [ ] `export_artifacts/manifest.json` exists
  - [ ] Single `.pt` file in `export_artifacts/`

- [ ] **config.yaml**
  - [ ] Contains `dataset.image_width` (positive integer)
  - [ ] Contains `dataset.image_height` (positive integer)

- [ ] **mochi.json**
  - [ ] Contains `stages` array with at least one stage
  - [ ] First stage has `type` field
  - [ ] First stage has `input_shape` as `[B, C, H, W]`

- [ ] **manifest.json**
  - [ ] Contains `model_graph` with stages
  - [ ] Contains `input_shape` array
  - [ ] Contains `compilable_models` with filename

- [ ] **Dimension Matching**
  - [ ] `config.yaml` width == `input_shape[3]`
  - [ ] `config.yaml` height == `input_shape[2]`

- [ ] **Model File**
  - [ ] PyTorch 1.8 compatible
  - [ ] Can be loaded with `torch.load()`

---

## Troubleshooting

### Common Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `config.yaml missing 'dataset' section` | Missing or malformed config.yaml | Ensure `dataset:` key exists with `image_width` and `image_height` |
| `Dimension mismatch` | config.yaml dimensions don't match input_shape | Ensure width/height match input_shape[3]/input_shape[2] |
| `No .pt model file found` | Missing PyTorch model | Add `.pt` file to `export_artifacts/` |
| `Invalid input_shape` | Wrong format | Use `[batch, channels, height, width]` format |
| `Model validation failed` | Various | Check all files exist and are valid JSON/YAML |

### Testing Locally

```python
import tarfile
import yaml
import json
import os

def validate_dda_package(tar_path: str):
    """Validate a DDA model package locally."""
    
    with tarfile.open(tar_path, "r:gz") as tar:
        members = tar.getnames()
        
        # Check required files
        required = ["config.yaml", "mochi.json", "export_artifacts/manifest.json"]
        for req in required:
            if req not in members:
                print(f"❌ Missing: {req}")
                return False
        
        # Check for .pt file
        pt_files = [m for m in members if m.endswith(".pt")]
        if not pt_files:
            print("❌ No .pt file found")
            return False
        
        # Extract and validate
        tar.extractall("/tmp/validate")
        
        # Validate config.yaml
        with open("/tmp/validate/config.yaml") as f:
            config = yaml.safe_load(f)
        
        width = config.get("dataset", {}).get("image_width")
        height = config.get("dataset", {}).get("image_height")
        
        if not width or not height:
            print("❌ Missing image dimensions in config.yaml")
            return False
        
        # Validate mochi.json
        with open("/tmp/validate/mochi.json") as f:
            mochi = json.load(f)
        
        input_shape = mochi.get("stages", [{}])[0].get("input_shape")
        if not input_shape or len(input_shape) != 4:
            print("❌ Invalid input_shape in mochi.json")
            return False
        
        # Check dimension match
        if width != input_shape[3] or height != input_shape[2]:
            print(f"❌ Dimension mismatch: config={width}x{height}, shape={input_shape}")
            return False
        
        print("✅ Package is valid!")
        print(f"   Input shape: {input_shape}")
        print(f"   Model file: {pt_files[0]}")
        return True

# Usage:
# validate_dda_package("my-model.tar.gz")
```

---

## Support

For questions or issues with BYOM:

1. Check the [Troubleshooting](#troubleshooting) section
2. Validate your package locally before uploading
3. Use the portal's "Validate Model" feature before importing
4. Contact your administrator for platform-specific guidance
