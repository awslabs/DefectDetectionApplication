"""
Shared vocabulary and pure helpers for portal-trained Object Detection models
(YOLO and RF-DETR).

Single source of truth for the three Lambdas that touch a detection training
record — `training.py` (create / status), `compilation.py` (Neo bypass) and
`packaging.py` (component build) — so they can never disagree about what a
Detection_Training_Job is, which hyperparameters it accepts, or what its
on-device manifest looks like.

Lives in the shared Lambda layer (mounted at /opt/python) alongside
`compilation_status.py`, `manifest_transformer.py`, etc.

Pure functions only — no boto3, no network I/O. The only filesystem access is
`build_sourcedir_tarball`, which tars a local directory the caller names; the
only DynamoDB access is `resolve_base_model`, which does one `get_item` on the
table the caller passes in.

Specs: .kiro/specs/portal-detection-training/ (design §Architecture) and
.kiro/specs/rfdetr-training-and-transfer-learning/ (design §Shared layer).
"""
import json
import os
import re
import tarfile
from typing import Any, Dict, List, Optional

# Re-export (rfdetr-training-and-transfer-learning task 7.1): the checkpoint
# classifier lives in its own shared-layer module (stdlib only, ~1,150 lines)
# and is reachable from here so callers have one import for everything
# detection-training. Sibling import, flat layer layout (/opt/python).
from checkpoint_probe import classify_checkpoint, FINE_TUNABLE_KINDS  # noqa: F401

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
MODEL_TYPE_OBJECT_DETECTION = 'object_detection'

# Detector families the portal can train. `detection_arch` on a request /
# record selects the entry point, the hyperparameter schema, the env the
# entry point reads and the on-device stage type + preprocessing.
DETECTION_ARCHES = ('yolo', 'rf_detr')

# On-device stage type / decoder selected by the manifest per arch. Must match
# model_converter.generate_dda_package's detection_stage_type and the
# OnnxRunner / Yolo|RfDetrDetectionPostProcessor in src/backend.
DETECTION_STAGE_TYPE = 'yolo_object_detection'
RFDETR_STAGE_TYPE = 'rf_detr_object_detection'
STAGE_TYPE_FOR_ARCH = {
    'yolo': DETECTION_STAGE_TYPE,
    'rf_detr': RFDETR_STAGE_TYPE,
}
DETECTION_ARCH = 'yolo'   # default arch (callers that omit detection_arch)

# The SageMaker script-mode entry points (datasets/detection_training/) and
# the files each needs as SIBLINGS at the sourcedir root. SageMaker's toolkit
# pip-installs exactly `requirements.txt`, so each arch's requirements file is
# renamed to that inside the tarball (see build_sourcedir_tarball).
DETECTION_ENTRY_POINT = 'train.py'
ENTRY_POINT_FOR_ARCH = {
    'yolo': 'train.py',
    'rf_detr': 'train_rfdetr.py',
}
REQUIREMENTS_FOR_ARCH = {
    'yolo': 'requirements.txt',
    'rf_detr': 'requirements-rfdetr.txt',
}
# Shared helpers module both entry points import as a sibling (created by the
# RF-DETR spec; bundled when present so older code dirs still tar fine).
DETECTION_SOURCEDIR_COMMON = '_common.py'
DETECTION_CONVERTER_FILES = (
    'manifest_to_detector_dataset.py',
    'dedupe_frames.py',
)
# The YOLO bundle as it appears inside sourcedir.tar.gz (without _common.py).
DETECTION_SOURCEDIR_FILES = (
    'train.py',
    'requirements.txt',
    'manifest_to_detector_dataset.py',
    'dedupe_frames.py',
)

# Fine-tunable checkpoint each trainer leaves in its Detection_Artifact
# (model.tar.gz); a later run extracts it as its base weights.
CHECKPOINT_MEMBER_FOR_ARCH = {
    'yolo': 'best.pt',
    'rf_detr': 'checkpoint_best_total.pth',
}

# RF-DETR Apache-2.0 detection checkpoints -> native square resolution.
# xlarge / 2xlarge are PML-licensed and deliberately excluded.
RFDETR_SIZES = {
    'nano': 384,
    'small': 512,
    'medium': 576,
    'large': 704,
}
# RF-DETR's input must be divisible by patch_size * num_windows for the
# variant; the current detection checkpoints (all four sizes above) use a
# block of 32 (rfdetr FAQ, "What input resolutions are allowed?"). The older
# "multiple of 56" rule belonged to the legacy RFDETRBase (14 * 4) and would
# reject every native size above. Pinned against the installed rfdetr by the
# entry point's own check (train_rfdetr.py).
RFDETR_RESOLUTION_STEP = 32
RFDETR_RESOLUTION_MIN = 224
RFDETR_RESOLUTION_MAX = 1120
# Decoder query slots for Nano..Large; the device keeps the top_k detections
# above score_threshold (set-based, no NMS).
RFDETR_TOP_K = 300

# SageMaker PyTorch GPU Deep Learning Container that runs train.py. The DLC
# account is 763104351884 in every standard commercial region, so the region
# is substituted rather than pinned (docs/detection-training-gap.md §8).
DETECTION_TRAINING_IMAGE_ACCOUNT = '763104351884'
DETECTION_TRAINING_IMAGE_TAG = (
    'pytorch-training:2.5.1-gpu-py311-cu124-ubuntu22.04-sagemaker')

DETECTION_DEFAULTS: Dict[str, Any] = {
    'imgsz': 1280,
    'epochs': 100,
    'batch': 4,
    'base_weights': 'yolo11s.pt',
    'patience': 30,
    'score_threshold': 0.25,
    'iou_threshold': 0.45,
    'onnx_opset': 17,
}
# RF-DETR schema (Req 3.2 of the RF-DETR spec). `resolution: None` means "the
# size's native resolution". batch 4 x grad_accum 4 is the documented T4
# (ml.g4dn.xlarge) configuration; no IoU — DETR-family decoding is NMS-free.
RFDETR_DEFAULTS: Dict[str, Any] = {
    'rfdetr_size': 'small',
    'resolution': None,
    'epochs': 100,
    'batch': 4,
    'grad_accum': 4,
    'lr': 1e-4,
    'patience': 10,
    'score_threshold': 0.5,
    'onnx_opset': 17,
}
DEFAULTS_FOR_ARCH = {
    'yolo': DETECTION_DEFAULTS,
    'rf_detr': RFDETR_DEFAULTS,
}
DETECTION_DEFAULT_INSTANCE_TYPE = 'ml.g4dn.xlarge'
DETECTION_DEFAULT_MAX_RUNTIME_SECONDS = 10800
DETECTION_VOLUME_SIZE_GB = 60

# SageMaker MetricDefinitions that lift train.py's single
#   TEST METRICS: {"test_map50": 0.99, "test_map50_95": 0.91, ...}
# log line into FinalMetricDataList, so the portal can show mAP without
# parsing CloudWatch.
DETECTION_METRIC_DEFINITIONS = [
    {'Name': 'test:mAP50', 'Regex': r'"test_map50": ([0-9.eE+-]+)'},
    {'Name': 'test:mAP50-95', 'Regex': r'"test_map50_95": ([0-9.eE+-]+)'},
    {'Name': 'test:precision', 'Regex': r'"test_precision": ([0-9.eE+-]+)'},
    {'Name': 'test:recall', 'Regex': r'"test_recall": ([0-9.eE+-]+)'},
]

# Manifest attribute vocabulary (mirrors dda_manifest.py without importing it:
# that module has its own heavier dependencies).
DDA_BBOX_ATTRIBUTE = 'bounding-box'
GT_OBJECT_DETECTION_TYPE = 'groundtruth/object-detection'

_BASE_WEIGHTS_RE = re.compile(r'^[A-Za-z0-9._-]+\.pt$')


# ---------------------------------------------------------------------------
# Record predicate
# ---------------------------------------------------------------------------

def is_trained_detection_record(training_job: Dict) -> bool:
    """True for a portal-trained Object Detection record whose training job
    already exported ONNX (what training.py writes: model_type
    'object_detection' + runtime 'onnx').

    All three conditions matter:
    - `runtime == 'onnx'` distinguishes this path from an `object_detection`
      record whose artifact is a TorchScript .pt that still needs Neo / the
      'onnx' export pseudo-target (the shape earlier specs' fixtures model,
      and the shape a future non-YOLO trainer might write). Those carry no
      `runtime` key.
    - trained records write no `source` (models.py defaults it to 'trained');
    - imported models write `source='imported'` and are handled by the
      existing ONNX-import predicates, never here.
    """
    if not isinstance(training_job, dict):
        return False
    if training_job.get('model_type') != MODEL_TYPE_OBJECT_DETECTION:
        return False
    if str(training_job.get('runtime', '')).lower() != 'onnx':
        return False
    return training_job.get('source', 'trained') == 'trained'


# ---------------------------------------------------------------------------
# Training image
# ---------------------------------------------------------------------------

def resolve_detection_training_image(region: str, override: Optional[str] = None) -> str:
    """The DLC image URI: an explicit override wins, else the regional default."""
    if override and str(override).strip():
        return str(override).strip()
    if not region:
        raise ValueError('region is required to resolve the detection training image')
    return (f"{DETECTION_TRAINING_IMAGE_ACCOUNT}.dkr.ecr.{region}.amazonaws.com/"
            f"{DETECTION_TRAINING_IMAGE_TAG}")


# ---------------------------------------------------------------------------
# Arch
# ---------------------------------------------------------------------------

def normalize_detection_arch(arch: Optional[str]) -> str:
    """'yolo' | 'rf_detr' (default 'yolo'); ValueError for anything else.

    ValueError is what training.py already maps to HTTP 400 for bad
    hyperparameters, so an unknown `detection_arch` takes the same path.
    """
    if arch is None or (isinstance(arch, str) and not arch.strip()):
        return DETECTION_ARCH
    value = str(arch).strip().lower()
    if value not in DETECTION_ARCHES:
        raise ValueError(
            f"Invalid detection_arch '{arch}': expected one of {', '.join(DETECTION_ARCHES)}")
    return value


# ---------------------------------------------------------------------------
# Hyperparameters
# ---------------------------------------------------------------------------

def _as_int(raw: Dict, name: str, defaults: Dict[str, Any] = DETECTION_DEFAULTS) -> int:
    value = raw.get(name, defaults[name])
    if isinstance(value, bool):
        raise ValueError(f"Invalid hyperparameter '{name}': must be an integer")
    try:
        # Accept "100" and 100.0 (e.g. a JSON number decoded as float) but not "abc".
        if isinstance(value, str):
            value = value.strip()
        as_float = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid hyperparameter '{name}': must be an integer")
    if as_float != int(as_float):
        raise ValueError(f"Invalid hyperparameter '{name}': must be an integer")
    return int(as_float)


def _as_float(raw: Dict, name: str, defaults: Dict[str, Any] = DETECTION_DEFAULTS) -> float:
    value = raw.get(name, defaults[name])
    if isinstance(value, bool):
        raise ValueError(f"Invalid hyperparameter '{name}': must be a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid hyperparameter '{name}': must be a number")


def _reject_unknown(raw: Dict, defaults: Dict[str, Any]) -> None:
    unknown = sorted(set(raw) - set(defaults))
    if unknown:
        raise ValueError(
            f"Unknown detection hyperparameter(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(defaults))}")


def parse_detection_hyperparameters(raw: Optional[Dict], arch: str = DETECTION_ARCH) -> Dict[str, Any]:
    """Apply defaults and validate the user-tunable detection settings for `arch`.

    Raises ValueError naming the offending field (Requirement 3.8). Returns a
    dict with native types; parsing the result again is a no-op.

    - yolo:    {imgsz, epochs, batch, base_weights, patience, score_threshold,
                iou_threshold, onnx_opset} (unchanged from portal-detection-training)
    - rf_detr: {rfdetr_size, resolution, epochs, batch, grad_accum, lr, patience,
                score_threshold, onnx_opset}; `resolution` defaults to the
                size's native value (RFDETR_SIZES).
    """
    arch = normalize_detection_arch(arch)
    if arch == 'rf_detr':
        return _parse_rfdetr_hyperparameters(raw)
    return _parse_yolo_hyperparameters(raw)


def _parse_yolo_hyperparameters(raw: Optional[Dict]) -> Dict[str, Any]:
    raw = dict(raw or {})
    _reject_unknown(raw, DETECTION_DEFAULTS)

    imgsz = _as_int(raw, 'imgsz')
    if imgsz < 320 or imgsz > 2048 or imgsz % 32 != 0:
        raise ValueError(
            "Invalid hyperparameter 'imgsz': must be a multiple of 32 between 320 and 2048")

    epochs = _as_int(raw, 'epochs')
    if not 1 <= epochs <= 1000:
        raise ValueError("Invalid hyperparameter 'epochs': must be between 1 and 1000")

    batch = _as_int(raw, 'batch')
    if not 1 <= batch <= 64:
        raise ValueError("Invalid hyperparameter 'batch': must be between 1 and 64")

    patience = _as_int(raw, 'patience')
    if not 0 <= patience <= 1000:
        raise ValueError("Invalid hyperparameter 'patience': must be between 0 and 1000")

    onnx_opset = _as_int(raw, 'onnx_opset')
    if not 11 <= onnx_opset <= 20:
        raise ValueError("Invalid hyperparameter 'onnx_opset': must be between 11 and 20")

    base_weights = str(raw.get('base_weights', DETECTION_DEFAULTS['base_weights'])).strip()
    if not _BASE_WEIGHTS_RE.match(base_weights):
        raise ValueError(
            "Invalid hyperparameter 'base_weights': must be a .pt filename "
            "(letters, digits, '.', '_', '-')")

    score_threshold = _as_float(raw, 'score_threshold')
    if not 0.0 < score_threshold < 1.0:
        raise ValueError(
            "Invalid hyperparameter 'score_threshold': must be strictly between 0 and 1")

    iou_threshold = _as_float(raw, 'iou_threshold')
    if not 0.0 < iou_threshold < 1.0:
        raise ValueError(
            "Invalid hyperparameter 'iou_threshold': must be strictly between 0 and 1")

    return {
        'imgsz': imgsz,
        'epochs': epochs,
        'batch': batch,
        'base_weights': base_weights,
        'patience': patience,
        'score_threshold': score_threshold,
        'iou_threshold': iou_threshold,
        'onnx_opset': onnx_opset,
    }


def _parse_rfdetr_hyperparameters(raw: Optional[Dict]) -> Dict[str, Any]:
    raw = dict(raw or {})
    _reject_unknown(raw, RFDETR_DEFAULTS)
    d = RFDETR_DEFAULTS

    size_value = raw.get('rfdetr_size', d['rfdetr_size'])
    rfdetr_size = str(size_value).strip().lower() if isinstance(size_value, str) else None
    if rfdetr_size not in RFDETR_SIZES:
        raise ValueError(
            "Invalid hyperparameter 'rfdetr_size': must be one of "
            f"{', '.join(RFDETR_SIZES)}")

    # Absent / null -> the size's native resolution; anything else validated.
    if raw.get('resolution') is None:
        resolution = RFDETR_SIZES[rfdetr_size]
    else:
        resolution = _as_int(raw, 'resolution', d)
    if (resolution < RFDETR_RESOLUTION_MIN or resolution > RFDETR_RESOLUTION_MAX
            or resolution % RFDETR_RESOLUTION_STEP != 0):
        raise ValueError(
            f"Invalid hyperparameter 'resolution': must be a multiple of "
            f"{RFDETR_RESOLUTION_STEP} between {RFDETR_RESOLUTION_MIN} and {RFDETR_RESOLUTION_MAX}")

    epochs = _as_int(raw, 'epochs', d)
    if not 1 <= epochs <= 1000:
        raise ValueError("Invalid hyperparameter 'epochs': must be between 1 and 1000")

    batch = _as_int(raw, 'batch', d)
    if not 1 <= batch <= 64:
        raise ValueError("Invalid hyperparameter 'batch': must be between 1 and 64")

    grad_accum = _as_int(raw, 'grad_accum', d)
    if not 1 <= grad_accum <= 64:
        raise ValueError("Invalid hyperparameter 'grad_accum': must be between 1 and 64")

    lr = _as_float(raw, 'lr', d)
    if not 0.0 < lr < 1.0:
        raise ValueError("Invalid hyperparameter 'lr': must be strictly between 0 and 1")

    patience = _as_int(raw, 'patience', d)
    if not 0 <= patience <= 1000:
        raise ValueError("Invalid hyperparameter 'patience': must be between 0 and 1000")

    score_threshold = _as_float(raw, 'score_threshold', d)
    if not 0.0 < score_threshold < 1.0:
        raise ValueError(
            "Invalid hyperparameter 'score_threshold': must be strictly between 0 and 1")

    onnx_opset = _as_int(raw, 'onnx_opset', d)
    if not 11 <= onnx_opset <= 20:
        raise ValueError("Invalid hyperparameter 'onnx_opset': must be between 11 and 20")

    return {
        'rfdetr_size': rfdetr_size,
        'resolution': resolution,
        'epochs': epochs,
        'batch': batch,
        'grad_accum': grad_accum,
        'lr': lr,
        'patience': patience,
        'score_threshold': score_threshold,
        'onnx_opset': onnx_opset,
    }


def detection_job_environment(
    manifest_s3: str,
    params: Dict[str, Any],
    arch: str = DETECTION_ARCH,
    base_weights_s3: Optional[str] = None,
    base_weights_member: Optional[str] = None,
) -> Dict[str, str]:
    """The bare environment names the arch's entry point reads (all values strings).

    Thresholds are NOT passed: they only shape the device manifest (record ->
    packaging), never the training run. `BASE_WEIGHTS_S3` (and
    `BASE_WEIGHTS_MEMBER` when the URI is a tarball the entry point must
    extract the checkpoint from) are added only when a base model resolved
    to real weights — the published-checkpoint env is unchanged.
    """
    arch = normalize_detection_arch(arch)
    if arch == 'rf_detr':
        env = {
            'MANIFEST_S3': manifest_s3,
            'RFDETR_SIZE': str(params['rfdetr_size']),
            'RESOLUTION': str(params['resolution']),
            'EPOCHS': str(params['epochs']),
            'BATCH': str(params['batch']),
            'GRAD_ACCUM': str(params['grad_accum']),
            'LR': str(params['lr']),
            'PATIENCE': str(params['patience']),
            'ONNX_OPSET': str(params['onnx_opset']),
        }
    else:
        env = {
            'MANIFEST_S3': manifest_s3,
            'IMGSZ': str(params['imgsz']),
            'EPOCHS': str(params['epochs']),
            'BATCH': str(params['batch']),
            'BASE_WEIGHTS': str(params['base_weights']),
            'PATIENCE': str(params['patience']),
            'ONNX_OPSET': str(params['onnx_opset']),
        }
    if base_weights_s3:
        env['BASE_WEIGHTS_S3'] = str(base_weights_s3)
        if base_weights_member:
            env['BASE_WEIGHTS_MEMBER'] = str(base_weights_member)
    return env


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

def detect_bbox_attribute(entry: Dict) -> Optional[str]:
    """Name of the bounding-box label attribute in a manifest entry, or None.

    Accepts the DDA literal `bounding-box` and a Ground Truth job-named
    attribute whose `<name>-metadata.type` is groundtruth/object-detection.
    """
    if not isinstance(entry, dict):
        return None
    if isinstance(entry.get(DDA_BBOX_ATTRIBUTE), dict):
        return DDA_BBOX_ATTRIBUTE
    for key, value in entry.items():
        if not key.endswith('-metadata') or not isinstance(value, dict):
            continue
        if str(value.get('type', '')).lower() != GT_OBJECT_DETECTION_TYPE:
            continue
        base = key[:-len('-metadata')]
        if isinstance(entry.get(base), dict):
            return base
    return None


def class_names_from_class_map(class_map: Dict) -> List[str]:
    """Ordered class names from a GT/DDA `class-map` ({"0": "a", "1": "b"})."""
    ordered = []
    for key in sorted(class_map, key=lambda k: int(k)):
        ordered.append(str(class_map[key]))
    return ordered


def validate_detection_manifest_entry(entry: Any) -> Dict[str, Any]:
    """Validate the first line of a manifest as an Object Detection entry.

    Returns {'valid', 'errors', 'class_names', 'attribute', 'detected_attributes'}.
    Never suggests the Manifest Transformer: it has no ObjectDetection
    handling, so that advice would be a dead end (gap doc §5.2).
    """
    if not isinstance(entry, dict):
        return {'valid': False, 'errors': ['Manifest entry is not a JSON object'],
                'class_names': [], 'attribute': None, 'detected_attributes': []}

    detected = list(entry.keys())
    errors: List[str] = []

    if not isinstance(entry.get('source-ref'), str) or not entry['source-ref']:
        errors.append('source-ref must be a non-empty string (S3 URI)')

    attr = detect_bbox_attribute(entry)
    if attr is None:
        if 'anomaly-label' in entry:
            errors.append(
                'Manifest is a classification/segmentation manifest (anomaly-label) — '
                'Object Detection requires a bounding-box manifest with '
                "'bounding-box' / 'bounding-box-metadata' attributes")
        else:
            errors.append(
                "No bounding-box attribute found: expected 'bounding-box' with a "
                "'bounding-box-metadata' companion, or a Ground Truth attribute whose "
                f"metadata type is '{GT_OBJECT_DETECTION_TYPE}'")
        return {'valid': False, 'errors': errors, 'class_names': [],
                'attribute': None, 'detected_attributes': detected}

    bbox = entry.get(attr) or {}
    meta = entry.get(f'{attr}-metadata') or {}
    if not isinstance(meta, dict):
        errors.append(f"'{attr}-metadata' must be an object")
        meta = {}

    if not isinstance(bbox.get('annotations'), list):
        errors.append(f"'{attr}.annotations' must be a list")
    if not isinstance(bbox.get('image_size'), list) or not bbox.get('image_size'):
        errors.append(f"'{attr}.image_size' must be a non-empty list")

    class_map = meta.get('class-map')
    class_names: List[str] = []
    if not isinstance(class_map, dict) or not class_map:
        errors.append(f"'{attr}-metadata.class-map' must be a non-empty object")
    else:
        try:
            class_names = class_names_from_class_map(class_map)
        except (TypeError, ValueError):
            errors.append(f"'{attr}-metadata.class-map' keys must be integer class ids")

    return {
        'valid': not errors,
        'errors': errors,
        'class_names': class_names,
        'attribute': attr,
        'detected_attributes': detected,
    }


# ---------------------------------------------------------------------------
# Device manifest
# ---------------------------------------------------------------------------

def build_detection_device_manifest(
    *,
    image_width: int,
    image_height: int,
    num_classes: int,
    class_names: Optional[List[str]],
    score_threshold: float,
    iou_threshold: Optional[float] = None,
    preserve_aspect: Optional[bool] = None,
    detection_arch: str = DETECTION_ARCH,
    top_k: int = RFDETR_TOP_K,
) -> Dict[str, Any]:
    """The on-device manifest.json for a portal-trained ONNX detector.

    Byte-for-byte the shape model_converter.generate_dda_package writes for
    (export_format='onnx', model_type='object_detection', detection_arch=...)
    — pinned by tests/test_detection_manifest_parity.py — plus the top-level
    `dataset` block packaging.package_onnx_component merges in from
    config.yaml for imported packages. Kept as a copy rather than an import:
    model_converter.py is a handler module with boto3 clients at import time
    and is preservation-tracked.

    Per arch:
    - yolo:    stage `yolo_object_detection`, `normalize: False` (0..1 scaling
               only), detection block with `iou_threshold` (NMS).
               `preserve_aspect` defaults to True because train.py fine-tunes
               letterboxed (rect=False); serving squashed would silently cost
               ~1.35x mean confidence (docs/detection-training-gap.md §7).
    - rf_detr: stage `rf_detr_object_detection`, `normalize: True` (ImageNet
               mean/std after 0..1 scaling), detection block with `top_k` and
               NO `iou_threshold` (set-based decoding). `preserve_aspect`
               defaults to False because RF-DETR trains on an aspect-destroying
               square resize — letterboxing would be the same geometry
               mismatch in the other direction.

    `output_shape` is nominal for both arches ([1, C+4, 8400], exactly what
    Smart Import writes; the device does not read it) — packaging replaces it
    with the real exported shape recorded in training_metadata.json.
    """
    arch = normalize_detection_arch(detection_arch)
    if preserve_aspect is None:
        preserve_aspect = arch == 'yolo'
    if arch != 'rf_detr' and iou_threshold is None:
        raise ValueError("iou_threshold is required for a yolo detection manifest")

    image_width = int(image_width)
    image_height = int(image_height)
    num_classes = int(num_classes)
    input_shape = [1, 3, image_height, image_width]
    output_shape = [1, num_classes + 4, 8400]

    stage = {
        'type': STAGE_TYPE_FOR_ARCH[arch],
        'input_shape': input_shape,
        'output_shape': output_shape,
        'image_width': image_width,
        'image_height': image_height,
        'image_range_scale': True,
        'normalize': arch == 'rf_detr',
        'threshold': float(score_threshold),
        'num_classes': num_classes,
    }
    detection: Dict[str, Any] = {
        'layout': arch,
        'num_classes': num_classes,
        'score_threshold': float(score_threshold),
        'network_input': image_width,
        'preserve_aspect': bool(preserve_aspect),
    }
    if arch == 'rf_detr':
        detection['top_k'] = int(top_k)
    else:
        detection['iou_threshold'] = float(iou_threshold)
    manifest: Dict[str, Any] = {
        'runtime': 'onnx',
        'runtime_artifact': 'model.onnx',
        'model_graph': {
            'model_graph_type': 'single_stage_model_graph',
            'stages': [stage],
        },
        'input_shape': input_shape,
        'preprocessing': {
            'resize': [image_width, image_height],
            'channel_order': 'RGB',
        },
        'task': MODEL_TYPE_OBJECT_DETECTION,
        'detection': detection,
        'dataset': {
            'image_width': image_width,
            'image_height': image_height,
        },
    }
    if class_names:
        manifest['detection']['class_names'] = [str(c) for c in class_names]
    return manifest


# ---------------------------------------------------------------------------
# Sourcedir
# ---------------------------------------------------------------------------

_REQUIREMENTS_FOR_ENTRY_POINT = {
    ENTRY_POINT_FOR_ARCH[a]: REQUIREMENTS_FOR_ARCH[a] for a in DETECTION_ARCHES
}


def build_sourcedir_tarball(
    code_dir: str,
    out_path: str,
    entry_point: str = DETECTION_ENTRY_POINT,
) -> List[str]:
    """Tar one arch's sourcedir FLAT at the archive root; returns the member names.

    Members: the named entry point, ITS requirements file renamed to
    `requirements.txt` (the SageMaker toolkit installs exactly that name), the
    two converter files, and `_common.py` when `code_dir` has one. Nothing
    else — each job's sourcedir stays minimal (Req 3.4) and the other arch's
    heavyweight pins never get installed.

    Everything is flat: train*.py resolves manifest_to_detector_dataset.py and
    _common.py as siblings and the converter imports dedupe_frames from its
    own directory; SageMaker extracts sourcedir.tar.gz flat, so nesting would
    break dataset conversion minutes into a GPU job (see
    datasets/detection_training/build_sourcedir.sh). A missing required file
    is a FileNotFoundError here, for the same reason.
    """
    if not os.path.isdir(code_dir):
        raise FileNotFoundError(
            f"Detection training code directory not found: {code_dir}")
    entry_point = str(entry_point)
    requirements_src = _REQUIREMENTS_FOR_ENTRY_POINT.get(entry_point)
    if requirements_src is None:
        raise ValueError(
            f"Unknown detection training entry point '{entry_point}': expected one of "
            f"{', '.join(sorted(_REQUIREMENTS_FOR_ENTRY_POINT))}")
    entry = os.path.join(code_dir, entry_point)
    if not os.path.isfile(entry):
        raise FileNotFoundError(
            f"Detection training entry point missing: {entry}")

    # (source filename in code_dir, name inside the tarball)
    members = [(entry_point, entry_point), (requirements_src, 'requirements.txt')]
    members += [(name, name) for name in DETECTION_CONVERTER_FILES]
    if os.path.isfile(os.path.join(code_dir, DETECTION_SOURCEDIR_COMMON)):
        members.append((DETECTION_SOURCEDIR_COMMON, DETECTION_SOURCEDIR_COMMON))
    for src, _arcname in members:
        if not os.path.isfile(os.path.join(code_dir, src)):
            raise FileNotFoundError(
                f"Detection training sourcedir file missing: {os.path.join(code_dir, src)}")

    members.sort(key=lambda m: m[1])
    with tarfile.open(out_path, 'w:gz') as tar:
        for src, arcname in members:
            tar.add(os.path.join(code_dir, src), arcname=arcname)
    return [arcname for _src, arcname in members]


# ---------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------

BASE_MODEL_KINDS = ('published', 'training_job', 'imported')

_ARTICLE_FOR_ARCH = {'yolo': 'a', 'rf_detr': 'an'}


def _record_display_name(record: Dict, fallback: str) -> str:
    name = record.get('model_name')
    if not name:
        return fallback
    version = record.get('model_version')
    return f"{name} v{version}" if version else str(name)


def resolve_base_model(
    kind: Optional[str],
    ref: Optional[str],
    arch: str,
    usecase_id: str,
    training_jobs_table: Any,
) -> Dict[str, Any]:
    """Resolve a request's `base_model = {kind, ref}` to a Base_Model_Descriptor.

    Returns `{kind, ref, weights_s3, member, detection_arch, class_names}`:
    - published:    weights_s3 None — the entry point uses the arch's published
                    COCO checkpoint (YOLO: the `base_weights` hyperparameter;
                    RF-DETR: the size's packaged weights). `ref` is echoed.
    - training_job: `ref` is a training_id in THIS use case whose record is a
                    Completed portal-trained detector of the same arch.
                    weights_s3 is its Detection_Artifact (model.tar.gz) and
                    `member` the checkpoint inside it (best.pt /
                    checkpoint_best_total.pth) — the entry point extracts it,
                    the Lambda never downloads a multi-hundred-MB artifact.
    - imported:     `ref` is an imported record in this use case whose
                    `metadata.fine_tunable` (Req 7) names a bare checkpoint
                    (`checkpoint_s3`) of the same arch; `member` is None.

    Raises ValueError with the user-facing message (training.py -> HTTP 400,
    before any S3 upload or SageMaker call) for every Req 6.4 condition: bad
    kind, unknown / other-use-case ref (one message — no cross-tenant
    existence leak), not Completed, not a detector, arch mismatch, no
    artifact for the checkpoint member, import without a fine-tunable
    checkpoint. Side-effect free apart from one `get_item`.
    """
    arch = normalize_detection_arch(arch)
    kind_value = str(kind or 'published').strip().lower()
    if kind_value not in BASE_MODEL_KINDS:
        raise ValueError(
            f"Invalid base_model.kind '{kind}': expected one of {', '.join(BASE_MODEL_KINDS)}")

    if kind_value == 'published':
        return {
            'kind': 'published',
            'ref': str(ref).strip() if ref is not None and str(ref).strip() else None,
            'weights_s3': None,
            'member': None,
            'detection_arch': arch,
            'class_names': None,
        }

    ref_value = str(ref).strip() if ref is not None else ''
    if not ref_value:
        raise ValueError(f"base_model.ref is required when base_model.kind is '{kind_value}'")

    response = training_jobs_table.get_item(Key={'training_id': ref_value}) or {}
    record = response.get('Item')
    if not isinstance(record, dict) or record.get('usecase_id') != usecase_id:
        raise ValueError(
            f"Base model training job '{ref_value}' was not found in this use case")
    name = _record_display_name(record, ref_value)
    det = record.get('detection') or {}
    if not isinstance(det, dict):
        det = {}

    if kind_value == 'training_job':
        if not is_trained_detection_record(record):
            raise ValueError(
                f"Base model {name} is not a portal-trained object detector")
        status = str(record.get('status', ''))
        if status != 'Completed':
            raise ValueError(
                f"Base model {name} is not Completed (status: {status or 'unknown'}); "
                "only a completed training job can be fine-tuned from")
        base_arch = str(det.get('detection_arch') or DETECTION_ARCH).lower()
        if base_arch != arch:
            raise ValueError(
                f"Base model {name} is a {base_arch} detector; cannot start "
                f"{_ARTICLE_FOR_ARCH[arch]} {arch} run from it")
        member = CHECKPOINT_MEMBER_FOR_ARCH[arch]
        artifact_s3 = record.get('artifact_s3')
        if not artifact_s3 or not str(artifact_s3).strip():
            raise ValueError(
                f"Base model {name} has no training artifact containing {member}")
        return {
            'kind': 'training_job',
            'ref': ref_value,
            'weights_s3': str(artifact_s3).strip(),
            'member': member,
            'detection_arch': arch,
            'class_names': [str(c) for c in (det.get('class_names') or [])],
        }

    # imported
    if record.get('source') != 'imported':
        raise ValueError(f"Base model {name} is not an imported model")
    metadata = record.get('metadata') or {}
    fine_tunable = metadata.get('fine_tunable') if isinstance(metadata, dict) else None
    if not isinstance(fine_tunable, dict) or not fine_tunable.get('checkpoint_s3'):
        raise ValueError(
            f"Imported model {name} has no fine-tunable checkpoint (ONNX/TorchScript)")
    base_arch = str(fine_tunable.get('arch') or '').lower()
    if base_arch != arch:
        raise ValueError(
            f"Base model {name} is a {base_arch or 'non-detection'} model; cannot start "
            f"{_ARTICLE_FOR_ARCH[arch]} {arch} run from it")
    class_names = (fine_tunable.get('class_names') or metadata.get('class_names')
                   or det.get('class_names') or [])
    return {
        'kind': 'imported',
        'ref': ref_value,
        'weights_s3': str(fine_tunable['checkpoint_s3']).strip(),
        'member': None,
        'detection_arch': arch,
        'class_names': [str(c) for c in class_names],
    }


def decode_final_metrics(final_metric_data_list: Optional[List[Dict]]) -> Dict[str, float]:
    """FinalMetricDataList -> {'test:mAP50': 0.99, ...} (floats)."""
    metrics: Dict[str, float] = {}
    for item in final_metric_data_list or []:
        name = item.get('MetricName')
        value = item.get('Value')
        if not name or value is None:
            continue
        try:
            metrics[str(name)] = float(value)
        except (TypeError, ValueError):
            continue
    return metrics


def manifest_to_json(manifest: Dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2)
