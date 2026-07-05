"""
Model Converter Lambda functions
Auto-generates DDA-compatible metadata from raw PyTorch models
Enables easy BYOM by accepting just a .pt file and user-provided dimensions
"""
import json
import os
import logging
from typing import Dict, Any, List, Tuple, Optional
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
import uuid
import tarfile
import tempfile
import shutil
from urllib.parse import urlparse
import yaml

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, validate_required_fields
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
sts = boto3.client('sts')

# Environment variables
TRAINING_JOBS_TABLE = os.environ.get('TRAINING_JOBS_TABLE')
USECASES_TABLE = os.environ.get('USECASES_TABLE')

# Supported model types
MODEL_TYPES = {
    'classification': {
        'description': 'Image classification (binary or multi-class)',
        'output_format': '[batch, num_classes]'
    },
    'object_detection': {
        'description': 'Object detection (YOLO, SSD, etc.)',
        'output_format': '[batch, detections, attributes]'
    },
    'segmentation': {
        'description': 'Semantic segmentation',
        'output_format': '[batch, num_classes, height, width]'
    },
    'anomaly_detection': {
        'description': 'Anomaly detection (normal vs anomaly)',
        'output_format': '[batch, 2]'
    }
}


def assume_usecase_role(role_arn: str, external_id: str, session_name: str) -> Dict:
    """Assume cross-account role for UseCase Account access.

    Single-account setups store the account *root* ARN
    (arn:aws:iam::ACCOUNT_ID:root) as the "cross_account_role_arn" — that is not
    an assumable role, so attempting sts:AssumeRole on it fails with
    AccessDenied. In that case the Lambda's own execution role already has
    access to the (same-account) UseCase resources, so signal the caller to use
    the default credential chain instead of assuming a role.
    """
    if role_arn and role_arn.endswith(':root'):
        logger.info("Single-account setup (root ARN) — using Lambda execution role credentials")
        return {'is_default_credentials': True}
    try:
        response = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            ExternalId=external_id,
            DurationSeconds=3600
        )
        return response['Credentials']
    except ClientError as e:
        logger.error(f"Error assuming role {role_arn}: {str(e)}")
        raise


def make_usecase_s3_client(credentials: Dict):
    """Build an S3 client from assume_usecase_role() output.

    For single-account setups (is_default_credentials) use the Lambda's own
    credentials; otherwise use the assumed-role credentials.
    """
    if credentials.get('is_default_credentials'):
        return boto3.client('s3')
    return boto3.client(
        's3',
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken'],
    )


def get_usecase_details(usecase_id: str) -> Dict:
    """Get use case details from DynamoDB"""
    try:
        table = dynamodb.Table(USECASES_TABLE)
        response = table.get_item(Key={'usecase_id': usecase_id})
        
        if 'Item' not in response:
            raise ValueError(f"Use case {usecase_id} not found")
        
        return response['Item']
    except Exception as e:
        logger.error(f"Error getting use case details: {str(e)}")
        raise


# ── Dependency-free ONNX graph reader ──────────────────────────────────────
# We only need the input/output tensor shapes to auto-detect model attributes.
# Rather than ship the heavy onnx/onnxruntime packages in the Lambda, parse the
# few protobuf fields we need straight from the ModelProto wire format:
#   ModelProto.graph            = field 7  (message GraphProto)
#   GraphProto.input            = field 11 (repeated ValueInfoProto)
#   GraphProto.output           = field 12 (repeated ValueInfoProto)
#   ValueInfoProto.name         = field 1  (string)
#   ValueInfoProto.type         = field 2  (TypeProto)
#   TypeProto.tensor_type       = field 1  (Tensor)
#   Tensor.elem_type            = field 1  (varint)
#   Tensor.shape                = field 2  (TensorShapeProto)
#   TensorShapeProto.dim        = field 1  (repeated Dimension)
#   Dimension.dim_value         = field 1  (varint int64)
#   Dimension.dim_param         = field 2  (string; dynamic axis => unknown)
def _pb_read_varint(buf: bytes, i: int):
    shift = 0
    result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7


def _pb_fields(buf: bytes):
    """Yield (field_number, wire_type, value) for a protobuf message. value is
    an int for varint/fixed wire types, or a bytes slice for length-delimited."""
    i = 0
    n = len(buf)
    while i < n:
        key, i = _pb_read_varint(buf, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:      # varint
            val, i = _pb_read_varint(buf, i)
            yield fn, wt, val
        elif wt == 2:    # length-delimited
            ln, i = _pb_read_varint(buf, i)
            yield fn, wt, buf[i:i + ln]
            i += ln
        elif wt == 1:    # 64-bit
            yield fn, wt, buf[i:i + 8]
            i += 8
        elif wt == 5:    # 32-bit
            yield fn, wt, buf[i:i + 4]
            i += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wt}")


def _pb_first(buf: bytes, field: int):
    for fn, _wt, val in _pb_fields(buf):
        if fn == field:
            return val
    return None


def _pb_all(buf: bytes, field: int):
    return [val for fn, _wt, val in _pb_fields(buf) if fn == field]


def _onnx_value_info_shape(vi_bytes: bytes):
    """Return (name, [dims]) for a ValueInfoProto. A dim is an int (static) or
    None (dynamic/unknown)."""
    name = None
    dims = []
    name_raw = _pb_first(vi_bytes, 1)
    if isinstance(name_raw, (bytes, bytearray)):
        name = name_raw.decode('utf-8', 'replace')
    type_proto = _pb_first(vi_bytes, 2)
    if type_proto is None:
        return name, dims
    tensor = _pb_first(type_proto, 1)   # TypeProto.tensor_type
    if tensor is None:
        return name, dims
    shape = _pb_first(tensor, 2)        # Tensor.shape
    if shape is None:
        return name, dims
    for dim_bytes in _pb_all(shape, 1):  # repeated Dimension
        dim_value = None
        for fn, wt, val in _pb_fields(dim_bytes):
            if fn == 1 and wt == 0:      # dim_value (static)
                dim_value = int(val)
            elif fn == 2:                # dim_param (dynamic axis) => unknown
                dim_value = None
        dims.append(dim_value)
    return name, dims


def inspect_onnx_model(model_path: str) -> Dict:
    """Parse an ONNX model's input/output tensor shapes and infer model
    attributes (input size, task/architecture, num_classes) for UI pre-fill.

    Detection heuristics from output tensor shapes:
      * 1 output  -> YOLO detection ([1, 4+C, N] or [1, N, 4+C]); C = the
        non-anchor dim minus 4.
      * 2 outputs -> RF-DETR detection (boxes [1,Q,4] + logits [1,Q,C]).
      * 3 outputs -> RF-DETR instance segmentation (adds a 4-D mask tensor).
      * 2-D single output [1, C] -> classification (C classes).
    """
    info: Dict[str, Any] = {'type': 'onnx', 'architecture_hints': []}
    try:
        with open(model_path, 'rb') as f:
            model_bytes = f.read()
        graph = _pb_first(model_bytes, 7)  # ModelProto.graph
        if graph is None:
            info['architecture_hints'].append('Could not read ONNX graph.')
            return info

        inputs = [_onnx_value_info_shape(v) for v in _pb_all(graph, 11)]
        outputs = [_onnx_value_info_shape(v) for v in _pb_all(graph, 12)]
        # Exclude initializer-backed inputs (weights): real inputs usually the
        # first entry. Use the first graph input as the data input.
        in_shapes = [dims for _n, dims in inputs if dims]
        out_shapes = [dims for _n, dims in outputs if dims]
        info['input_shapes'] = in_shapes
        info['output_shapes'] = out_shapes
        info['num_outputs'] = len(out_shapes)

        # Input NCHW -> channels/H/W (dynamic dims come back as None).
        if in_shapes:
            d = in_shapes[0]
            if len(d) == 4:
                info['input_channels'] = d[1]
                info['input_height'] = d[2]
                info['input_width'] = d[3]

        def last_dim(shape):
            return shape[-1] if shape else None

        n_out = len(out_shapes)
        if n_out == 2:
            # RF-DETR detection: one tensor ends in 4 (boxes), other is logits.
            boxes = next((s for s in out_shapes if last_dim(s) == 4), None)
            logits = next((s for s in out_shapes if s is not boxes), None)
            info['suggested_type'] = 'object_detection'
            info['detection_arch'] = 'rf_detr'
            if logits is not None and last_dim(logits):
                info['num_classes'] = int(last_dim(logits))
            info['architecture_hints'].append(
                'RF-DETR-style detection: 2 output tensors (boxes + logits), NMS-free.')
        elif n_out >= 3:
            # RF-DETR instance segmentation: boxes + logits + mask tensor(s).
            logits = next((s for s in out_shapes if s and len(s) == 3 and last_dim(s) != 4), None)
            info['suggested_type'] = 'segmentation'
            info['detection_arch'] = 'rf_detr'
            if logits is not None and last_dim(logits):
                info['num_classes'] = int(last_dim(logits))
            info['architecture_hints'].append(
                'RF-DETR-style instance segmentation: 3 output tensors '
                '(boxes + logits + masks).')
        elif n_out == 1:
            s = out_shapes[0]
            if s and len(s) == 3:
                # YOLO detection [1, 4+C, N] or [1, N, 4+C]; anchors is the
                # larger dim, channels the smaller.
                a, b = s[1], s[2]
                if a and b:
                    ch = min(a, b)
                    info['suggested_type'] = 'object_detection'
                    info['detection_arch'] = 'yolo'
                    info['num_classes'] = int(ch - 4)
                    info['architecture_hints'].append(
                        'YOLO-style detection: single output tensor, NMS decode.')
            elif s and len(s) == 2 and s[1]:
                info['suggested_type'] = 'classification'
                info['num_classes'] = int(s[1])
                info['architecture_hints'].append(
                    'Classification: single [batch, num_classes] output.')

        if not info['architecture_hints']:
            info['architecture_hints'].append(
                'ONNX model — could not infer task from output shapes; set the '
                'model type manually.')
        return info
    except Exception as e:  # noqa: BLE001 - inspection must never hard-fail
        logger.error(f"Error parsing ONNX model: {str(e)}")
        return {
            'type': 'onnx',
            'architecture_hints': [
                'ONNX model — shape inspection failed; set attributes manually.'],
        }


def inspect_pytorch_model(model_path: str) -> Dict:
    """
    Inspect a PyTorch model file to extract metadata.
    Returns detected information about the model.
    """
    try:
        import torch
        
        # Load the model
        model_data = torch.load(model_path, map_location='cpu')
        
        info = {
            'type': 'unknown',
            'is_state_dict': False,
            'is_jit': False,
            'is_full_model': False,
            'layers': [],
            'input_channels': None,
            'num_classes': None,
            'architecture_hints': []
        }
        
        # Check if it's a JIT model
        if hasattr(model_data, 'graph'):
            info['is_jit'] = True
            info['type'] = 'jit_model'
            return info
        
        # Check if it's a state dict
        if isinstance(model_data, dict):
            # Could be a state dict or a checkpoint
            if 'model' in model_data:
                # Checkpoint format (common in YOLO, etc.)
                state_dict = model_data.get('model', {})
                if hasattr(state_dict, 'state_dict'):
                    state_dict = state_dict.state_dict()
                info['is_checkpoint'] = True
            elif 'state_dict' in model_data:
                state_dict = model_data['state_dict']
                info['is_checkpoint'] = True
            else:
                # Assume it's a raw state dict
                state_dict = model_data
                info['is_state_dict'] = True
            
            # Analyze layer names
            layer_names = list(state_dict.keys()) if isinstance(state_dict, dict) else []
            info['layers'] = layer_names[:20]  # First 20 layers
            info['total_layers'] = len(layer_names)
            
            # Try to detect architecture from layer names
            layer_str = ' '.join(layer_names).lower()
            
            # Detect common architectures
            if 'yolo' in layer_str or 'detect' in layer_str:
                info['architecture_hints'].append('YOLO-like object detection')
                info['suggested_type'] = 'object_detection'
            elif 'classifier' in layer_str or 'fc' in layer_str:
                info['architecture_hints'].append('Classification network')
                info['suggested_type'] = 'classification'
            elif 'decoder' in layer_str and 'encoder' in layer_str:
                info['architecture_hints'].append('Encoder-Decoder (segmentation)')
                info['suggested_type'] = 'segmentation'
            elif 'resnet' in layer_str:
                info['architecture_hints'].append('ResNet architecture')
                info['suggested_type'] = 'classification'
            elif 'efficientnet' in layer_str:
                info['architecture_hints'].append('EfficientNet architecture')
                info['suggested_type'] = 'classification'
            elif 'vit' in layer_str or 'transformer' in layer_str:
                info['architecture_hints'].append('Vision Transformer')
                info['suggested_type'] = 'classification'
            
            # Try to detect input channels from first conv layer
            for name, param in state_dict.items() if isinstance(state_dict, dict) else []:
                if 'conv' in name.lower() and 'weight' in name.lower():
                    if hasattr(param, 'shape') and len(param.shape) == 4:
                        info['input_channels'] = param.shape[1]
                        break
            
            # Try to detect num_classes from last layer
            for name in reversed(layer_names):
                if 'fc' in name.lower() or 'classifier' in name.lower() or 'head' in name.lower():
                    if 'weight' in name.lower():
                        param = state_dict.get(name)
                        if hasattr(param, 'shape') and len(param.shape) == 2:
                            info['num_classes'] = param.shape[0]
                            break
        
        else:
            # Full model object
            info['is_full_model'] = True
            info['type'] = 'full_model'
        
        return info
        
    except Exception as e:
        logger.error(f"Error inspecting model: {str(e)}")
        return {
            'type': 'unknown',
            'error': str(e),
            'architecture_hints': ['Could not inspect model']
        }


def generate_dda_package(
    model_path: str,
    model_name: str,
    model_type: str,
    image_width: int,
    image_height: int,
    num_classes: Optional[int] = None,
    class_names: Optional[List[str]] = None,
    output_path: str = None,
    export_format: str = 'pytorch',
    score_threshold: float = 0.25,
    iou_threshold: float = 0.45,
    detection_arch: str = 'yolo',
) -> str:
    """
    Generate a DDA-compatible package from a raw model file.
    Creates config.yaml, mochi.json, and manifest.json automatically.

    :param export_format: 'pytorch' (legacy .pt / DLR path) or 'onnx'. For
        'onnx' the package is written for the pluggable ONNX Runtime engine
        (manifest runtime="onnx", artifact model.onnx) and, for detection
        models, the object-detection task path (see
        docs/multi-runtime-inference.md). 'pytorch' preserves the original
        behavior.
    :param score_threshold/iou_threshold: detection decode thresholds (only used
        for object_detection).
    :param detection_arch: object-detection decoder family — 'yolo' (single
        tensor, NMS) or 'rf_detr' (DETR-family, two tensors, NMS-free).
    """
    temp_dir = None
    is_onnx = str(export_format).lower() == 'onnx'
    is_detection = model_type == 'object_detection'
    detection_arch = str(detection_arch or 'yolo').lower()
    # RF-DETR instance-segmentation ONNX -> rendered as a semantic mask through
    # the existing anomaly-localization path (colored mask + overlay + color map).
    is_rf_detr_seg = is_onnx and model_type == 'segmentation' and detection_arch == 'rf_detr'
    # Map the detection architecture to the on-device stage type / decoder.
    detection_stage_type = (
        'rf_detr_object_detection' if detection_arch == 'rf_detr'
        else 'yolo_object_detection'
    )

    try:
        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="dda_convert_")
        export_dir = os.path.join(temp_dir, "export_artifacts")
        os.makedirs(export_dir, exist_ok=True)
        
        # Determine input shape (assume RGB)
        input_shape = [1, 3, image_height, image_width]
        
        # Determine output shape based on model type
        if model_type == 'classification':
            output_shape = [1, num_classes or 2]
        elif model_type == 'object_detection':
            # YOLO-style output
            output_shape = [1, (num_classes or 80) + 4, 8400]
        elif model_type == 'segmentation':
            output_shape = [1, num_classes or 2, image_height, image_width]
        elif model_type == 'anomaly_detection':
            output_shape = [1, 2]
            num_classes = 2
        else:
            output_shape = [1, num_classes or 2]
        
        # 1. Create config.yaml
        config = {
            'dataset': {
                'image_width': image_width,
                'image_height': image_height
            }
        }
        if num_classes:
            config['dataset']['num_classes'] = num_classes
        if class_names:
            config['dataset']['class_names'] = class_names
        
        with open(os.path.join(temp_dir, 'config.yaml'), 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
        
        # 2. Create mochi.json
        mochi_stage_type = detection_stage_type if (is_onnx and is_detection) else model_type
        mochi = {
            'stages': [
                {
                    'type': mochi_stage_type,
                    'input_shape': input_shape,
                    'output_shape': output_shape
                }
            ],
            'model_info': {
                'name': model_name,
                'version': '1.0.0',
                'framework': 'pytorch',
                'auto_generated': True,
                'generated_at': datetime.utcnow().isoformat()
            }
        }
        if num_classes:
            mochi['stages'][0]['num_classes'] = num_classes
        
        with open(os.path.join(temp_dir, 'mochi.json'), 'w') as f:
            json.dump(mochi, f, indent=2)
        
        # 3. Create manifest.json
        if is_onnx:
            # ONNX package for the pluggable ONNX Runtime engine. For detection
            # models, wire the object-detection task path the device serving
            # code (Phases B/C) reads.
            artifact_filename = "model.onnx"
            # Map the user-facing model_type to the device stage type and graph.
            if is_detection:
                stage_type = detection_stage_type
            elif is_rf_detr_seg:
                stage_type = 'rf_detr_semantic_segmentation'
            else:
                stage_type = model_type
            # Preprocessing differs by architecture:
            #  - YOLO: 0..1 scaling only (image_range_scale), NO ImageNet
            #    mean/std normalization.
            #  - RF-DETR (DETR-family, detection AND segmentation): 0..1 scaling
            #    THEN ImageNet mean/std normalization, i.e. (pixel/255 - mean)/std.
            #    BasicPreProcessor applies the ImageNet MEAN/STD when
            #    normalize=True; omitting it yields garbage output.
            detection_normalize = (is_detection and detection_arch == 'rf_detr') or is_rf_detr_seg
            stage = {
                "type": stage_type,
                "input_shape": input_shape,
                "output_shape": output_shape,
                "image_width": image_width,
                "image_height": image_height,
                "image_range_scale": True,
                "normalize": detection_normalize,
                "threshold": score_threshold,
            }
            if num_classes:
                stage["num_classes"] = num_classes
            manifest = {
                "runtime": "onnx",
                "runtime_artifact": artifact_filename,
                "model_graph": {
                    "model_graph_type": "single_stage_model_graph",
                    "stages": [stage],
                },
                "input_shape": input_shape,
                "preprocessing": {
                    "resize": [image_width, image_height],
                    "channel_order": "RGB",
                },
            }
            if is_rf_detr_seg:
                # Semantic segmentation via the anomaly-localization path: the
                # task stays "anomaly" (default) so the base model emits the
                # colored mask + overlay + per-class color map. pixel_level_classes
                # (index 0 = background) enables localization and provides the
                # class name / color-map labels.
                seg_num_classes = int(num_classes or 91)  # RF-DETR-seg-nano COCO default
                if class_names and len(class_names) >= seg_num_classes:
                    class_labels = list(class_names[:seg_num_classes])
                else:
                    class_labels = [f"class_{i}" for i in range(seg_num_classes)]
                manifest["model_graph"]["pixel_level_classes"] = {
                    "names": ["background"] + class_labels,
                    "normal_ids": [0],
                }
                # Seg decoder config (read from the stage by the post-processor).
                stage["detection"] = {
                    "layout": "rf_detr",
                    "num_classes": seg_num_classes,
                    "score_threshold": score_threshold,
                    "mask_threshold": 0.5,
                    "network_input": image_width,
                }
            elif is_detection:
                manifest["task"] = "object_detection"
                manifest["detection"] = {
                    "layout": detection_arch,  # 'yolo' | 'rf_detr' (decoder family)
                    "num_classes": num_classes or 80,
                    "score_threshold": score_threshold,
                    "network_input": image_width,
                }
                # NMS is YOLO-only; DETR-family is set-based (top-k, no NMS).
                if detection_arch == 'rf_detr':
                    manifest["detection"]["top_k"] = 300
                else:
                    manifest["detection"]["iou_threshold"] = iou_threshold
                if class_names:
                    manifest["detection"]["class_names"] = class_names
        else:
            # Legacy PyTorch/DLR package (unchanged behavior).
            pt_filename = f"{model_name}.pt"
            artifact_filename = pt_filename
            manifest = {
                'model_graph': {
                    'stages': [
                        {
                            'type': model_type,
                            'input_shape': input_shape,
                            'output_shape': output_shape
                        }
                    ]
                },
                'input_shape': input_shape,
                'compilable_models': [
                    {
                        'filename': pt_filename,
                        'data_input_config': {
                            'input': input_shape
                        },
                        'framework': 'PYTORCH'
                    }
                ],
                'preprocessing': {
                    'resize': [image_width, image_height],
                    'normalize': {
                        'mean': [0.485, 0.456, 0.406],
                        'std': [0.229, 0.224, 0.225]
                    },
                    'channel_order': 'RGB'
                }
            }
        
        with open(os.path.join(export_dir, 'manifest.json'), 'w') as f:
            json.dump(manifest, f, indent=2)
        
        # 4. Copy the model file (flat in the export dir). This is the imported
        # intermediate package; the final on-device stage-subdir layout is
        # applied later by packaging.package_onnx_component when the deployable
        # Greengrass component ZIP is built (it nests model.onnx under the
        # manifest's stage_type). Keeping it flat here lets that consumer locate
        # the .onnx by a top-level scan.
        shutil.copy(model_path, os.path.join(export_dir, artifact_filename))
        
        # 5. Create tar.gz archive
        if not output_path:
            output_path = os.path.join(temp_dir, f"{model_name}.tar.gz")
        
        with tarfile.open(output_path, 'w:gz') as tar:
            for item in os.listdir(temp_dir):
                item_path = os.path.join(temp_dir, item)
                if item != os.path.basename(output_path):  # Don't include the output file itself
                    tar.add(item_path, arcname=item)
        
        logger.info(f"Generated DDA package: {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Error generating DDA package: {str(e)}")
        raise
    finally:
        # Don't cleanup if output_path is in temp_dir
        pass


def convert_model(event: Dict, context: Any) -> Dict:
    """
    Convert a raw PyTorch model to DDA-compatible format.
    POST /api/v1/models/convert
    
    Request body:
    {
        "usecase_id": "string",
        "model_s3_uri": "s3://bucket/path/model.pt",  // Raw .pt file
        "model_name": "string",
        "model_type": "classification" | "object_detection" | "segmentation" | "anomaly_detection",
        "image_width": 224,
        "image_height": 224,
        "num_classes": 10,  // optional
        "class_names": ["class1", "class2"],  // optional
        "auto_import": true  // optional, auto-import after conversion
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['usecase_id', 'model_s3_uri', 'model_name', 'model_type', 'image_width', 'image_height']
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        usecase_id = body['usecase_id']
        model_s3_uri = body['model_s3_uri'].strip()
        model_name = body['model_name'].strip()
        model_type = body['model_type']
        image_width = int(body['image_width'])
        image_height = int(body['image_height'])
        num_classes = body.get('num_classes')
        class_names = body.get('class_names')
        auto_import = body.get('auto_import', False)
        # 'pytorch' (legacy .pt/DLR) or 'onnx' (pluggable ONNX Runtime engine).
        export_format = str(body.get('export_format', 'pytorch')).lower()
        score_threshold = float(body.get('score_threshold', 0.25))
        iou_threshold = float(body.get('iou_threshold', 0.45))
        # Detection decoder family: 'yolo' (default) or 'rf_detr'.
        detection_arch = str(body.get('detection_arch', 'yolo')).lower()
        
        # Validate model type
        if model_type not in MODEL_TYPES:
            return create_response(400, {
                'error': f"Invalid model_type. Must be one of: {', '.join(MODEL_TYPES.keys())}"
            })
        
        # Validate dimensions
        if image_width <= 0 or image_height <= 0:
            return create_response(400, {'error': 'Image dimensions must be positive integers'})
        
        # Check user access (DataScientist role required)
        if not check_user_access(user_id, usecase_id, 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Validate S3 URI format
        if not model_s3_uri.startswith('s3://'):
            return create_response(400, {
                'error': 'Invalid model_s3_uri. Must be an S3 URI (s3://bucket/path/model.pt)'
            })
        
        # Get use case details
        usecase = get_usecase_details(usecase_id)
        
        # Assume cross-account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            f"convert-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        )

        # Create S3 client (assumed role for multi-account, Lambda role for single-account)
        s3_client = make_usecase_s3_client(credentials)
        
        # Parse S3 URI
        parsed = urlparse(model_s3_uri)
        source_bucket = parsed.netloc
        source_key = parsed.path.lstrip('/')
        
        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="model_convert_")
        
        try:
            # Download the model file. Use an extension matching the source so
            # ONNX packages carry model.onnx and the torch inspector is skipped.
            is_onnx = export_format == 'onnx'
            local_name = 'model.onnx' if is_onnx else 'model.pt'
            local_model = os.path.join(temp_dir, local_name)
            logger.info(f"Downloading model from {model_s3_uri}")
            s3_client.download_file(source_bucket, source_key, local_model)

            # Inspect the model (PyTorch only; ONNX is opaque to the torch
            # inspector and doesn't need it).
            if is_onnx:
                model_info = {'type': 'onnx', 'architecture_hints': ['ONNX model']}
            else:
                logger.info("Inspecting model...")
                model_info = inspect_pytorch_model(local_model)
            
            # Generate DDA package
            logger.info("Generating DDA-compatible package...")
            safe_model_name = model_name.replace(' ', '_').replace('-', '_').lower()
            output_tar = os.path.join(temp_dir, f"{safe_model_name}.tar.gz")
            
            generate_dda_package(
                model_path=local_model,
                model_name=safe_model_name,
                model_type=model_type,
                image_width=image_width,
                image_height=image_height,
                num_classes=num_classes,
                class_names=class_names,
                output_path=output_tar,
                export_format=export_format,
                score_threshold=score_threshold,
                iou_threshold=iou_threshold,
                detection_arch=detection_arch,
            )
            
            # Upload converted package to S3
            output_key = f"converted-models/{safe_model_name}-{uuid.uuid4().hex[:8]}.tar.gz"
            output_s3_uri = f"s3://{usecase['s3_bucket']}/{output_key}"
            
            logger.info(f"Uploading converted model to {output_s3_uri}")
            s3_client.upload_file(output_tar, usecase['s3_bucket'], output_key)
            
            # Log audit event
            log_audit_event(
                user_id=user_id,
                action='convert_model',
                resource_type='model',
                resource_id=safe_model_name,
                result='success',
                details={
                    'source_uri': model_s3_uri,
                    'output_uri': output_s3_uri,
                    'model_type': model_type,
                    'dimensions': f"{image_width}x{image_height}"
                }
            )
            
            result = {
                'converted_model_s3_uri': output_s3_uri,
                'model_name': safe_model_name,
                'model_type': model_type,
                'input_shape': [1, 3, image_height, image_width],
                'model_info': model_info,
                'message': 'Model converted successfully'
            }
            
            # Auto-import if requested
            if auto_import:
                # Invoke model import Lambda
                lambda_client = boto3.client('lambda')
                import_function_name = os.environ.get('MODEL_IMPORT_FUNCTION_NAME')
                
                if import_function_name:
                    import_event = {
                        'httpMethod': 'POST',
                        'path': '/api/v1/models/import',
                        'body': json.dumps({
                            'usecase_id': usecase_id,
                            'model_name': model_name,
                            'model_version': '1.0.0',
                            'model_s3_uri': output_s3_uri,
                            'description': f'Auto-converted from {model_s3_uri}'
                        }),
                        'requestContext': {
                            'authorizer': {
                                'claims': {
                                    'sub': user_id,
                                    'email': user['email'],
                                    'cognito:username': user.get('username', user_id)
                                }
                            }
                        }
                    }
                    
                    # Invoke synchronously to get result
                    response = lambda_client.invoke(
                        FunctionName=import_function_name,
                        InvocationType='RequestResponse',
                        Payload=json.dumps(import_event)
                    )
                    
                    import_result = json.loads(response['Payload'].read())
                    if import_result.get('statusCode') == 201:
                        import_body = json.loads(import_result.get('body', '{}'))
                        result['import_result'] = import_body
                        result['training_id'] = import_body.get('training_id')
                        result['message'] = 'Model converted and imported successfully'
                    else:
                        result['import_error'] = 'Auto-import failed'
            
            return create_response(200, result)
            
        finally:
            # Cleanup temp directory
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return create_response(400, {'error': str(e)})
    except ClientError as e:
        logger.error(f"AWS error: {str(e)}")
        return create_response(500, {'error': f"Failed to convert model: {str(e)}"})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def inspect_model_endpoint(event: Dict, context: Any) -> Dict:
    """
    Inspect a PyTorch model file to detect its architecture.
    POST /api/v1/models/inspect
    
    Request body:
    {
        "usecase_id": "string",
        "model_s3_uri": "s3://bucket/path/model.pt"
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['usecase_id', 'model_s3_uri']
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        usecase_id = body['usecase_id']
        model_s3_uri = body['model_s3_uri'].strip()
        
        # Check user access
        if not check_user_access(user_id, usecase_id):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Get use case details
        usecase = get_usecase_details(usecase_id)
        
        # Assume cross-account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            f"inspect-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        )

        # Create S3 client (assumed role for multi-account, Lambda role for single-account)
        s3_client = make_usecase_s3_client(credentials)
        
        # Parse S3 URI
        parsed = urlparse(model_s3_uri)
        bucket = parsed.netloc
        key = parsed.path.lstrip('/')

        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="model_inspect_")

        # ONNX models are opaque to the PyTorch inspector (torch.load). Parse
        # the ONNX graph's input/output shapes to auto-detect attributes
        # (input size, task, detection architecture, num_classes) for UI
        # pre-fill instead of the misleading "Could not inspect model".
        is_onnx = key.lower().endswith('.onnx')

        try:
            # Download the model file (extension-matched so the torch path only
            # sees real .pt files).
            local_model = os.path.join(temp_dir, 'model.onnx' if is_onnx else 'model.pt')
            logger.info(f"Downloading model from {model_s3_uri}")
            s3_client.download_file(bucket, key, local_model)

            # Inspect the model
            if is_onnx:
                model_info = inspect_onnx_model(local_model)
            else:
                model_info = inspect_pytorch_model(local_model)

            return create_response(200, {
                'model_s3_uri': model_s3_uri,
                'inspection_result': model_info,
                'supported_model_types': MODEL_TYPES
            })
            
        finally:
            # Cleanup
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        
    except Exception as e:
        logger.error(f"Error inspecting model: {str(e)}")
        return create_response(500, {'error': f"Failed to inspect model: {str(e)}"})


def get_supported_types(event: Dict, context: Any) -> Dict:
    """
    Get supported model types for conversion.
    GET /api/v1/models/types
    """
    return create_response(200, {
        'model_types': MODEL_TYPES,
        'common_dimensions': {
            'classification': [224, 256, 299, 384, 512],
            'object_detection': [320, 416, 512, 640, 1280],
            'segmentation': [256, 512, 768, 1024],
            'anomaly_detection': [224, 256, 512]
        },
        'supported_frameworks': ['PYTORCH'],
        'framework_versions': ['1.8', '1.9', '1.10', '1.11', '1.12', '1.13', '2.0']
    })


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler - routes to appropriate function"""
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        
        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }
        
        # Route to appropriate handler
        if http_method == 'POST' and '/models/convert' in path:
            return convert_model(event, context)
        elif http_method == 'POST' and '/models/inspect' in path:
            return inspect_model_endpoint(event, context)
        elif http_method == 'GET' and '/models/types' in path:
            return get_supported_types(event, context)
        else:
            return create_response(404, {'error': 'Not found'})
            
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})
