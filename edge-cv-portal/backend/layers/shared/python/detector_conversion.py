"""Checkpoint conversion for imported detectors (spec: detector-checkpoint-import).

Smart Import takes an ultralytics YOLO `.pt` or an RF-DETR `.pth`, converts it
to ONNX in a network-isolated SageMaker job (the Conversion_Job, which runs
datasets/detection_training/export_checkpoint.py on the Export_Image), and
publishes the result through packaging.package_trained_detection_component.
This module holds every pure piece of that pipeline so the three handler
modules that take part (model_converter, training / training_events,
packaging) share one implementation:

  * assess_checkpoint           -- classify_checkpoint output -> convertible? why not? pre-fill
  * validate_conversion_request -- the convert request's boundary validation
  * build_conversion_job_request / build_conversion_record
  * is_detector_conversion_record
  * plan_conversion_transition / plan_finalize_transition / apply_conversion_transition
  * validate_conversion_artifact -- the untrusted job output, checked before packaging

Security invariant: nothing here (or in any portal Lambda) deserializes a
checkpoint. Classification is classify_checkpoint's literal-only walk; the
unpickle happens only inside the isolated job; the job's output is parsed as
plain bytes (tar headers, protobuf fields, JSON).

Stdlib only (boto3 appears only as a lazily-inspected exception in
apply_conversion_transition), so the module imports in the portal's unit tests.
"""
import hashlib
import io
import json
import logging
import mmap
import os
import re
import tarfile
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (decided by the spike: docs/detector-checkpoint-import-spike.md)
# ---------------------------------------------------------------------------

#: Fleet_Floor_Runtime: the oldest onnxruntime a default packaging target runs
#: (JP5 GPU build, CPU / x86 images) -> ONNX opset <= 19, IR version <= 9.
FLEET_FLOOR_ORT = '1.16.3'
FLEET_MAX_OPSET = 19
FLEET_MAX_IR = 9
CONVERSION_OPSET = 17
ALLOWED_OP_DOMAINS = ('ai.onnx',)

#: Largest source checkpoint accepted by inspect / upload / convert (spike:
#: a 1 GiB checkpoint measured 27 s of download + sha256 + probe + sidecar
#: upload in a ModelConverter-shaped Lambda, against the 29 s API timeout;
#: 512 MiB costs ~14 s).
CHECKPOINT_SIZE_CAP = 512 << 20
#: Largest Conversion_Artifact (model.tar.gz) and members accepted from the job.
ARTIFACT_SIZE_CAP = 2 << 30
ONNX_MEMBER_CAP = 2 << 30
METADATA_MEMBER_CAP = 1 << 20

CONVERSION_INSTANCE_TYPE = 'ml.m5.xlarge'
CONVERSION_VOLUME_GB = 30
CONVERSION_MAX_RUNTIME_S = 1800
CONVERSION_CHANNEL = 'checkpoint'

CONVERSION_IN_PROGRESS = 'InProgress'
CONVERSION_FINALIZING = 'Finalizing'
CONVERSION_COMPLETED = 'Completed'
CONVERSION_FAILED = 'Failed'
CONVERSION_STATUSES = (CONVERSION_IN_PROGRESS, CONVERSION_FINALIZING,
                       CONVERSION_COMPLETED, CONVERSION_FAILED)
TERMINAL_CONVERSION_STATUSES = (CONVERSION_COMPLETED, CONVERSION_FAILED)
PROGRESS_FOR_STATUS = {
    CONVERSION_IN_PROGRESS: 10,
    CONVERSION_FINALIZING: 80,
    CONVERSION_COMPLETED: 100,
    CONVERSION_FAILED: 0,
}

MODEL_TYPE_OBJECT_DETECTION = 'object_detection'
CHECKPOINT_EXTENSIONS = ('.pt', '.pth')
UPLOAD_EXTENSIONS = ('.pt', '.pth', '.onnx')
INPUT_STEP = 32
INPUT_BOUNDS = {'yolo': (320, 2048), 'rf_detr': (224, 1120)}
DEFAULT_SCORE_THRESHOLD = {'yolo': 0.25, 'rf_detr': 0.5}
DEFAULT_IOU_THRESHOLD = 0.45
RFDETR_TOP_K = 300

# ultralytics: the only model class / head classes the device decodes.
YOLO_MODEL_CLASS = 'ultralytics.nn.tasks.DetectionModel'
YOLO_HEAD_PREFIX = 'ultralytics.nn.modules.head.'
# YOLO26 checkpoints carry `Detect`; YOLOv10's NMS-free `v10Detect` converts
# through its trained one-to-many branch (ultralytics `nms=None` export ->
# [1, 4+C, N]); spike: docs/detector-checkpoint-import-spike.md.
ACCEPTED_YOLO_HEADS = ('ultralytics.nn.modules.head.Detect', 'ultralytics.nn.modules.head.v10Detect')
REJECTED_ULTRALYTICS_MODELS = {
    'SegmentationModel': 'segmentation',
    'PoseModel': 'pose estimation',
    'OBBModel': 'oriented bounding boxes',
    'ClassificationModel': 'classification',
    'WorldModel': 'open-vocabulary detection (YOLO-World)',
    'YOLOEModel': 'open-vocabulary detection (YOLOE)',
    'RTDETRDetectionModel': 'ultralytics RT-DETR',
}
REJECTED_YOLO_HEAD_REASONS = {
    'Segment': 'the head is a segmentation head',
    'Pose': 'the head is a pose head',
    'OBB': 'the head is an oriented-box head',
    'Classify': 'the head is a classification head',
    'WorldDetect': 'the head is an open-vocabulary (YOLO-World) head',
    'YOLOEDetect': 'the head is an open-vocabulary (YOLOE) head',
    'RTDETRDecoder': 'the head is an RT-DETR decoder',
}

# RF-DETR: the Apache-2.0 detection sizes and their native resolutions.
RFDETR_SIZES = {'nano': 384, 'small': 512, 'medium': 576, 'large': 704}
RFDETR_CLASS_FOR_SIZE = {'nano': 'RFDETRNano', 'small': 'RFDETRSmall',
                         'medium': 'RFDETRMedium', 'large': 'RFDETRLarge'}
RFDETR_PML_CLASSES = ('RFDETRXLarge', 'RFDETR2XLarge', 'RFDETRSegXLarge', 'RFDETRSeg2XLarge')
RFDETR_LEGACY_CLASSES = ('RFDETRBase', 'RFDETRLargeDeprecated')
RFDETR_NATIVE_INPUTS = tuple(sorted(set(RFDETR_SIZES.values())))

REASON_TEXT = {
    'torchscript': ('this is a frozen TorchScript graph, not a training checkpoint; export ONNX '
                    'with the tool that produced it and import that ONNX'),
    'state_dict': ('this file is a bare state_dict (weights without a model definition); only '
                   'full ultralytics YOLO or RF-DETR training checkpoints can be converted'),
    'legacy_torch': ('this is a legacy torch file without a model definition; only full '
                     'ultralytics YOLO or RF-DETR training checkpoints can be converted'),
    'onnx': 'this file is already an ONNX graph; import it with ONNX output instead of converting it',
    'unknown': 'this is not a recognised ultralytics YOLO or RF-DETR checkpoint',
}


class ConversionValidationError(ValueError):
    """A Conversion_Artifact failed validation. `rule` names the violated
    Requirement 8 rule; str(self) is the user-facing failure reason."""

    def __init__(self, rule: str, detail: str):
        self.rule = rule
        self.detail = detail
        super().__init__(f"Conversion output rejected ({rule}): {detail}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _plain(value: Any) -> Any:
    """Decimal -> int / float, recursively (DynamoDB round-trips)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def to_dynamo(value: Any) -> Any:
    """float -> Decimal, recursively (DynamoDB rejects Python floats)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    return value


def _short(qualname: Optional[str]) -> str:
    return str(qualname or '').rsplit('.', 1)[-1]


def is_checkpoint_key(key: str) -> bool:
    return str(key or '').lower().endswith(CHECKPOINT_EXTENSIONS)


def _valid_input(arch: str, value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    low, high = INPUT_BOUNDS[arch]
    return value if (value % INPUT_STEP == 0 and low <= value <= high) else None


# ---------------------------------------------------------------------------
# Pre-flight assessment (Requirement 2)
# ---------------------------------------------------------------------------

def _rfdetr_size_from_evidence(evidence: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(size, reason_if_rejected) from what the probe read without unpickling.

    RF-DETR converts only at its size's native resolution (spike decision:
    rfdetr 1.10.1 re-derives positional_encoding_size from an explicit
    `resolution`, so a non-native export no longer loads the checkpoint's
    positional grid). A size of None means "unknown here": the job infers it
    from the state_dict and still enforces the native resolution."""
    model_name = evidence.get('model_name')
    if model_name in RFDETR_PML_CLASSES:
        return None, (f'{model_name} is a PML-licensed RF-DETR size; only the Apache-2.0 sizes '
                      f'({", ".join(RFDETR_SIZES)}) can be converted')
    if isinstance(model_name, str) and ('seg' in model_name.lower() or 'keypoint' in model_name.lower()):
        return None, f'{model_name} is not an RF-DETR detection model; only detection converts'
    if model_name in RFDETR_LEGACY_CLASSES:
        return None, (f'{model_name} is a legacy RF-DETR model; only the nano/small/medium/large '
                      f'checkpoints of rfdetr >= 1.3 can be converted')
    picks = evidence.get('args_picks') or {}
    encoder = picks.get('encoder')
    if encoder and encoder != 'dinov2_windowed_small':
        return None, (f'RF-DETR encoder {encoder!r} is a legacy/unsupported variant; only the '
                      f'Apache-2.0 nano/small/medium/large checkpoints can be converted')
    resolution = picks.get('resolution')
    size = next((s for s, cls in RFDETR_CLASS_FOR_SIZE.items() if model_name == cls), None)
    if size is None:
        size = next((s for s, native in RFDETR_SIZES.items() if resolution == native), None)
    if isinstance(resolution, int) and not isinstance(resolution, bool):
        if size is not None and resolution != RFDETR_SIZES[size]:
            return size, (f'the checkpoint was trained at {resolution}px; RF-DETR {size} converts only '
                          f'at its native {RFDETR_SIZES[size]}px')
        if size is None:
            return None, (f'the checkpoint was trained at {resolution}px, which is not a native RF-DETR '
                          f'resolution ({", ".join(f"{s} {r}" for s, r in RFDETR_SIZES.items())})')
    return size, None


def assess_checkpoint(probe: Dict[str, Any], *, conversion_available: bool = True,
                      unavailable_reason: Optional[str] = None) -> Dict[str, Any]:
    """classify_checkpoint output -> the Requirement 2 `checkpoint` block.

    Pure. `convertible` is a pre-flight verdict only; the Conversion_Job
    re-checks everything with the real libraries (design D9).
    """
    probe = probe if isinstance(probe, dict) else {}
    evidence = probe.get('evidence') if isinstance(probe.get('evidence'), dict) else {}
    kind = probe.get('kind') or 'unknown'
    num_classes = probe.get('num_classes')
    if isinstance(num_classes, bool) or not isinstance(num_classes, int):
        num_classes = None
    raw_names = probe.get('class_names')
    class_names = [str(n) for n in raw_names] if isinstance(raw_names, list) else None
    out: Dict[str, Any] = {
        'kind': kind,
        'arch': probe.get('arch'),
        'task': None,
        'model_class': None,
        'head_classes': [],
        'num_classes': num_classes if isinstance(num_classes, int) else None,
        'class_names': class_names,
        'train_input_size': None,
        'framework': None,
        'framework_version': None,
        'rfdetr_size': None,
        'convertible': False,
        'reasons': [],
    }
    reasons: List[str] = out['reasons']

    if kind == 'ultralytics_checkpoint':
        out['arch'] = 'yolo'
        out['framework'] = 'ultralytics'
        out['framework_version'] = evidence.get('version')
        out['model_class'] = evidence.get('model_class')
        globals_ = evidence.get('pickle_globals') or []
        heads = sorted({g for g in globals_ if isinstance(g, str) and g.startswith(YOLO_HEAD_PREFIX)})
        out['head_classes'] = heads
        train_args = evidence.get('train_args') if isinstance(evidence.get('train_args'), dict) else {}
        task = train_args.get('task')
        out['task'] = task if isinstance(task, str) else ('detect' if out['model_class'] == YOLO_MODEL_CLASS else None)
        out['train_input_size'] = _valid_input('yolo', train_args.get('imgsz'))

        model_short = _short(out['model_class'])
        if out['model_class'] != YOLO_MODEL_CLASS:
            if model_short in REJECTED_ULTRALYTICS_MODELS:
                reasons.append(f'this ultralytics checkpoint is a {REJECTED_ULTRALYTICS_MODELS[model_short]} '
                               f'model ({model_short}); only object detection converts')
            else:
                reasons.append(f'ultralytics model class {out["model_class"] or "(unknown)"} is not '
                               f'{YOLO_MODEL_CLASS}; only object detection converts')
        if isinstance(task, str) and task != 'detect':
            reasons.append(f'the checkpoint was trained for the {task!r} task; only detect converts')
        rejected_heads = [h for h in heads if h not in ACCEPTED_YOLO_HEADS]
        for head in rejected_heads:
            reasons.append(REJECTED_YOLO_HEAD_REASONS.get(
                _short(head), f'detection head {_short(head)} is not supported'))
        if not heads:
            reasons.append('no ultralytics detection head found in the checkpoint')
        if not isinstance(num_classes, int) or num_classes < 1:
            reasons.append('the class count could not be read from the checkpoint')
        elif class_names is None or len(class_names) != num_classes:
            reasons.append('the class names could not be read from the checkpoint')
    elif kind == 'rfdetr_checkpoint':
        out['arch'] = 'rf_detr'
        out['framework'] = 'rfdetr'
        out['model_class'] = evidence.get('model_name')
        size, rejection = _rfdetr_size_from_evidence(evidence)
        out['rfdetr_size'] = size
        picks = evidence.get('args_picks') or {}
        resolution = _valid_input('rf_detr', picks.get('resolution'))
        out['train_input_size'] = resolution or (RFDETR_SIZES[size] if size else None)
        out['task'] = 'detect'
        if rejection:
            reasons.append(rejection)
        if not isinstance(num_classes, int) or num_classes < 1:
            reasons.append('the class count could not be read from the RF-DETR head')
    else:
        reasons.append(REASON_TEXT.get(kind, REASON_TEXT['unknown']))

    if not reasons and not conversion_available:
        reasons.append(unavailable_reason or 'Checkpoint conversion is not configured on this portal')
    out['convertible'] = not reasons
    return out


def assessment_prefill(assessment: Dict[str, Any]) -> Dict[str, Any]:
    """The existing inspection_result fields the Smart Import page pre-fills
    from (Requirement 2.5)."""
    size = assessment.get('train_input_size') if assessment.get('convertible') else None
    hints = [f"{'Ultralytics YOLO' if assessment.get('arch') == 'yolo' else 'RF-DETR'} "
             f"checkpoint" if assessment.get('arch') else 'PyTorch file']
    hints += [f'Not convertible: {r}' for r in assessment.get('reasons') or []]
    out = {
        'type': 'checkpoint',
        'architecture_hints': hints,
        'num_classes': assessment.get('num_classes'),
        'class_names': assessment.get('class_names'),
        'input_width': size,
        'input_height': size,
    }
    if assessment.get('convertible'):
        out['suggested_type'] = MODEL_TYPE_OBJECT_DETECTION
        out['detection_arch'] = assessment.get('arch')
    return out


# ---------------------------------------------------------------------------
# Request validation (Requirement 4.3-4.5)
# ---------------------------------------------------------------------------

def _number(body: Dict[str, Any], name: str, default: Optional[float]) -> Optional[float]:
    value = body.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"'{name}' must be a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be a number")


def validate_conversion_request(body: Dict[str, Any], assessment: Dict[str, Any]) -> Dict[str, Any]:
    """Boundary validation of a convert request against the server-side
    assessment. Returns ConversionParams; raises ValueError (HTTP 400) naming
    the field. Nothing may be created before this passes."""
    if not assessment.get('convertible'):
        raise ValueError('Checkpoint cannot be converted to ONNX: ' +
                         '; '.join(assessment.get('reasons') or ['unknown reason']))
    arch = assessment['arch']
    model_type = body.get('model_type') or MODEL_TYPE_OBJECT_DETECTION
    if model_type != MODEL_TYPE_OBJECT_DETECTION:
        raise ValueError(f"'model_type' must be '{MODEL_TYPE_OBJECT_DETECTION}' for a checkpoint "
                         f"conversion; got {model_type!r}")
    requested_arch = body.get('detection_arch')
    if requested_arch not in (None, '') and str(requested_arch).lower() != arch:
        raise ValueError(f"'detection_arch' {requested_arch!r} contradicts the checkpoint, which is "
                         f"{'an ultralytics YOLO' if arch == 'yolo' else 'an RF-DETR'} checkpoint")

    width, height = body.get('image_width'), body.get('image_height')
    if width is None and height is None:
        width = height = assessment.get('train_input_size')
    if width is None or height is None:
        raise ValueError("'image_width' and 'image_height' (the network input) are required")
    try:
        width_i, height_i = int(width), int(height)
    except (TypeError, ValueError):
        raise ValueError("'image_width' and 'image_height' must be integers")
    if width_i != height_i:
        raise ValueError(f"the network input must be square; got {width_i}x{height_i}")
    low, high = INPUT_BOUNDS[arch]
    if width_i % INPUT_STEP or not low <= width_i <= high:
        raise ValueError(f"the network input must be a multiple of {INPUT_STEP} between {low} and "
                         f"{high} for {'YOLO' if arch == 'yolo' else 'RF-DETR'}; got {width_i}")
    if arch == 'rf_detr':
        # Native resolution only (spike decision; see _rfdetr_size_from_evidence).
        known = assessment.get('rfdetr_size')
        allowed = (RFDETR_SIZES[known],) if known in RFDETR_SIZES else RFDETR_NATIVE_INPUTS
        if width_i not in allowed:
            raise ValueError(
                f"RF-DETR converts at its size's native resolution "
                f"({'%s: %d' % (known, allowed[0]) if known in RFDETR_SIZES else ', '.join(map(str, allowed))}); "
                f"got {width_i}")

    num_classes = assessment.get('num_classes')
    requested_nc = body.get('num_classes')
    if requested_nc not in (None, '') and int(requested_nc) != num_classes:
        raise ValueError(f"'num_classes' {requested_nc} contradicts the checkpoint's {num_classes}")
    names = body.get('class_names')
    if names in (None, []):
        names = assessment.get('class_names')
        if not names:
            raise ValueError(f"'class_names' is required: the checkpoint stores no class names; "
                             f"supply exactly {num_classes}")
    if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() for n in names):
        raise ValueError("'class_names' must be a list of non-empty strings")
    names = [n.strip() for n in names]
    if len(names) != num_classes:
        raise ValueError(f"'class_names' must list exactly {num_classes} classes (the checkpoint's "
                         f"head); got {len(names)}. Names may be renamed but not added or removed")

    score = _number(body, 'score_threshold', DEFAULT_SCORE_THRESHOLD[arch])
    if not 0.0 < score < 1.0:
        raise ValueError("'score_threshold' must be strictly between 0 and 1")
    iou = _number(body, 'iou_threshold', DEFAULT_IOU_THRESHOLD if arch == 'yolo' else None)
    if arch == 'yolo':
        if iou is None or not 0.0 < iou < 1.0:
            raise ValueError("'iou_threshold' must be strictly between 0 and 1")
    elif body.get('iou_threshold') is not None:
        raise ValueError("'iou_threshold' does not apply to RF-DETR (set-based decoding, no NMS)")

    derived_aspect = arch == 'yolo'
    requested_aspect = body.get('preserve_aspect')
    if requested_aspect is not None and bool(requested_aspect) != derived_aspect:
        raise ValueError(
            f"'preserve_aspect' must be {str(derived_aspect).lower()} for "
            f"{'YOLO (ultralytics trains letterboxed)' if arch == 'yolo' else 'RF-DETR (trained on a square resize)'}")

    size = assessment.get('rfdetr_size')
    requested_size = body.get('rfdetr_size')
    if arch == 'rf_detr' and requested_size not in (None, ''):
        requested_size = str(requested_size).lower()
        if requested_size not in RFDETR_SIZES:
            raise ValueError(f"'rfdetr_size' must be one of {', '.join(RFDETR_SIZES)}")
        if size and requested_size != size:
            raise ValueError(f"'rfdetr_size' {requested_size!r} contradicts the checkpoint ({size})")
        size = requested_size

    return {
        'arch': arch,
        'network_input': width_i,
        'num_classes': num_classes,
        'class_names': names,
        'score_threshold': score,
        'iou_threshold': iou if arch == 'yolo' else None,
        'top_k': RFDETR_TOP_K if arch == 'rf_detr' else None,
        'preserve_aspect': derived_aspect,
        'onnx_opset': CONVERSION_OPSET,
        'rfdetr_size': size if arch == 'rf_detr' else None,
    }


# ---------------------------------------------------------------------------
# Job request + record (Requirements 4.6, 5.1-5.4)
# ---------------------------------------------------------------------------

def conversion_job_name(safe_model_name: str, timestamp: str) -> str:
    """`<name>-cnv-<timestamp>`, SageMaker-legal (alnum + '-'), <= 63 chars."""
    stem = re.sub(r'[^A-Za-z0-9-]+', '-', str(safe_model_name)).strip('-') or 'model'
    suffix = f"-cnv-{re.sub(r'[^0-9]', '', str(timestamp))}"
    return re.sub(r'-+', '-', f"{stem[:63 - len(suffix)]}{suffix}").strip('-')


def image_region(image_uri: str) -> Optional[str]:
    """The region of an ECR image URI (`<acct>.dkr.ecr.<region>.amazonaws.com/...`)."""
    m = re.match(r'^\d{12}\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com(\.cn)?/', str(image_uri or ''))
    return m.group(1) if m else None


def conversion_unavailable_reason(image_uri: Optional[str], usecase_region: Optional[str]) -> Optional[str]:
    """None when conversion can run for this use case; otherwise the 503 text (Req 4.8)."""
    if not image_uri or not str(image_uri).strip():
        return 'Checkpoint conversion is not configured on this portal (no detector export image)'
    region = image_region(image_uri)
    if region is None:
        return f'Checkpoint conversion is misconfigured: {image_uri!r} is not an ECR image URI'
    if usecase_region and region != usecase_region:
        return (f'Checkpoint conversion is not configured for region {usecase_region} '
                f'(the detector export image is in {region})')
    return None


def build_conversion_job_request(*, job_name: str, image_uri: str, role_arn: str,
                                 input_prefix_s3: str, output_s3: str, params: Dict[str, Any],
                                 source_sha256: str, tags: Sequence[Dict[str, str]] = ()) -> Dict[str, Any]:
    """create_training_job kwargs for one Conversion_Job (pure).

    EnableNetworkIsolation=True, the code baked into the image (no
    sagemaker_program / sagemaker_submit_directory / requirements.txt), and a
    single input channel on the sidecar PREFIX with its trailing '/' -- without
    it S3 prefix matching would also pull `<name>-<hex>.tar.gz` siblings.
    """
    if not str(input_prefix_s3).startswith('s3://') or not str(input_prefix_s3).endswith('/'):
        raise ValueError('input_prefix_s3 must be an s3:// prefix ending in /')
    if not re.fullmatch(r'[0-9a-f]{64}', str(source_sha256)):
        raise ValueError('source_sha256 must be a 64-character hex digest')
    environment = {
        'DETECTION_ARCH': params['arch'],
        'NETWORK_INPUT': str(params['network_input']),
        'EXPECTED_NUM_CLASSES': str(params['num_classes']),
        'EXPECTED_SHA256': source_sha256,
        'ONNX_OPSET': str(params.get('onnx_opset') or CONVERSION_OPSET),
    }
    if params.get('rfdetr_size'):
        environment['RFDETR_SIZE'] = params['rfdetr_size']
    return {
        'TrainingJobName': job_name,
        'RoleArn': role_arn,
        'AlgorithmSpecification': {'TrainingImage': image_uri, 'TrainingInputMode': 'File'},
        'InputDataConfig': [{
            'ChannelName': CONVERSION_CHANNEL,
            'DataSource': {'S3DataSource': {
                'S3DataType': 'S3Prefix',
                'S3Uri': input_prefix_s3,
                'S3DataDistributionType': 'FullyReplicated',
            }},
            'InputMode': 'File',
        }],
        'OutputDataConfig': {'S3OutputPath': output_s3},
        'ResourceConfig': {
            'InstanceType': CONVERSION_INSTANCE_TYPE,
            'InstanceCount': 1,
            'VolumeSizeInGB': CONVERSION_VOLUME_GB,
        },
        'StoppingCondition': {'MaxRuntimeInSeconds': CONVERSION_MAX_RUNTIME_S},
        'EnableNetworkIsolation': True,
        'Environment': environment,
        'Tags': [dict(t) for t in tags],
    }


def build_conversion_record(*, training_id: str, usecase_id: str, model_name: str,
                            model_version: str, created_by: str, params: Dict[str, Any],
                            assessment: Dict[str, Any], fine_tunable: Optional[Dict[str, Any]],
                            job_name: str, job_arn: str, image_uri: str, source_s3: str,
                            source_sha256: str, source_bytes: int, model_file: str,
                            now_ms: int) -> Dict[str, Any]:
    """The Conversion_Record (Requirement 4.6), DynamoDB-ready (pure)."""
    arch = params['arch']
    size = params['network_input']
    detection: Dict[str, Any] = {
        'detection_arch': arch,
        'network_input_width': size,
        'network_input_height': size,
        'class_names': list(params['class_names']),
        'num_classes': params['num_classes'],
        'score_threshold': params['score_threshold'],
        'preserve_aspect': params['preserve_aspect'],
        'onnx_opset': params['onnx_opset'],
    }
    if arch == 'rf_detr':
        detection['top_k'] = params.get('top_k') or RFDETR_TOP_K
        detection['resolution'] = size
        if params.get('rfdetr_size'):
            detection['rfdetr_size'] = params['rfdetr_size']
    else:
        detection['iou_threshold'] = params['iou_threshold']
        detection['imgsz'] = size
    framework_version = assessment.get('framework_version')
    metadata = {
        'framework': 'PYTORCH',
        'framework_version': (f"{assessment.get('framework')} {framework_version}"
                              if framework_version else (assessment.get('framework') or 'PYTORCH')),
        'model_file': model_file,
        'pt_file': model_file,
        'model_type': MODEL_TYPE_OBJECT_DETECTION,
        'image_width': size,
        'image_height': size,
        'input_shape': [1, 3, size, size],
        'class_names': list(params['class_names']),
        'num_classes': params['num_classes'],
        'fine_tunable': fine_tunable,
        'checkpoint_kind': assessment.get('kind'),
    }
    record = {
        'training_id': training_id,
        'usecase_id': usecase_id,
        'model_name': model_name,
        'model_version': model_version,
        'model_type': MODEL_TYPE_OBJECT_DETECTION,
        'source': 'imported',
        'runtime': 'onnx',
        'status': 'InProgress',
        'progress': PROGRESS_FOR_STATUS[CONVERSION_IN_PROGRESS],
        'training_job_name': job_name,
        'training_job_arn': job_arn,
        'instance_type': CONVERSION_INSTANCE_TYPE,
        'algorithm_uri': image_uri,
        'created_by': created_by,
        'created_at': now_ms,
        'updated_at': now_ms,
        'auto_compile': False,
        'detection': detection,
        'metadata': metadata,
        'conversion': {
            'status': CONVERSION_IN_PROGRESS,
            'job_name': job_name,
            'export_image': image_uri,
            'source_s3': source_s3,
            'source_sha256': source_sha256,
            'source_bytes': int(source_bytes),
            'source_framework': assessment.get('framework'),
            'source_framework_version': framework_version,
            'started_at': now_ms,
        },
    }
    return to_dynamo(record)


def is_detector_conversion_record(record: Any) -> bool:
    """True for a Conversion_Record (Requirement 9.1). Never true for a
    portal-trained record (no `source`) or a plain ONNX / PyTorch import (no
    `conversion` block)."""
    return (isinstance(record, dict)
            and record.get('source') == 'imported'
            and record.get('model_type') == MODEL_TYPE_OBJECT_DETECTION
            and str(record.get('runtime', '')).lower() == 'onnx'
            and isinstance(record.get('conversion'), dict))


def conversion_status(record: Dict[str, Any]) -> Optional[str]:
    conv = record.get('conversion') if isinstance(record, dict) else None
    return conv.get('status') if isinstance(conv, dict) else None


# ---------------------------------------------------------------------------
# Lifecycle reducer (Requirement 7)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Transition:
    """One conditional Conversion_Status move. `set_fields` are top-level
    attributes; `conversion_fields` land under `conversion.*`."""

    from_status: str
    to_status: str
    set_fields: Dict[str, Any] = field(default_factory=dict)
    conversion_fields: Dict[str, Any] = field(default_factory=dict)
    invoke_finalize: bool = False


def plan_conversion_transition(record: Dict[str, Any], sm_status: Optional[str],
                               sm_failure_reason: Optional[str] = None,
                               sm_artifact_s3: Optional[str] = None,
                               now_ms: int = 0) -> Optional[Transition]:
    """What a SageMaker job state means for a Conversion_Record (pure).

    Only InProgress moves on SageMaker events: Finalizing belongs to the
    packaging finalize, and Completed / Failed are terminal (Req 7.1).
    """
    current = conversion_status(record)
    if current != CONVERSION_IN_PROGRESS:
        return None
    if sm_status == 'Completed':
        if not sm_artifact_s3:
            return _failed(current, 'Conversion job completed without an artifact', now_ms)
        return Transition(
            from_status=current, to_status=CONVERSION_FINALIZING,
            set_fields={'status': 'InProgress',
                        'progress': PROGRESS_FOR_STATUS[CONVERSION_FINALIZING],
                        'artifact_s3': sm_artifact_s3, 'updated_at': now_ms},
            conversion_fields={'finalizing_at': now_ms},
            invoke_finalize=True)
    if sm_status in ('Failed', 'Stopped'):
        reason = sm_failure_reason or (
            'Conversion job was stopped' if sm_status == 'Stopped' else 'Conversion job failed')
        return _failed(current, reason, now_ms)
    return None


def _failed(from_status: str, reason: str, now_ms: int) -> Transition:
    return Transition(
        from_status=from_status, to_status=CONVERSION_FAILED,
        set_fields={'status': 'Failed', 'progress': PROGRESS_FOR_STATUS[CONVERSION_FAILED],
                    'failure_reason': str(reason), 'updated_at': now_ms},
        conversion_fields={'failed_at': now_ms})


def plan_finalize_transition(*, success: bool, now_ms: int, failure_reason: Optional[str] = None,
                             packaged_components: Optional[List[Dict[str, Any]]] = None,
                             onnx_sha256: Optional[str] = None,
                             onnx_summary: Optional[Dict[str, Any]] = None) -> Transition:
    """Finalizing -> Completed / Failed, written by the packaging finalize (pure)."""
    if not success:
        return _failed(CONVERSION_FINALIZING, failure_reason or 'Conversion output rejected', now_ms)
    return Transition(
        from_status=CONVERSION_FINALIZING, to_status=CONVERSION_COMPLETED,
        set_fields={'status': 'Completed', 'progress': PROGRESS_FOR_STATUS[CONVERSION_COMPLETED],
                    'packaged_components': packaged_components or [],
                    'completed_at': now_ms, 'updated_at': now_ms},
        conversion_fields={'completed_at': now_ms, 'onnx_sha256': onnx_sha256,
                           'onnx_summary': onnx_summary or {}})


def transition_update_kwargs(training_id: str, transition: Transition) -> Dict[str, Any]:
    """The conditional update_item kwargs for a transition (pure)."""
    names = {'#conv': 'conversion', '#cst': 'status'}
    values: Dict[str, Any] = {':from': transition.from_status, ':to': transition.to_status}
    sets = ['#conv.#cst = :to']
    for i, (key, value) in enumerate(sorted(transition.set_fields.items())):
        names[f'#s{i}'] = key
        values[f':s{i}'] = to_dynamo(value)
        sets.append(f'#s{i} = :s{i}')
    for i, (key, value) in enumerate(sorted(transition.conversion_fields.items())):
        names[f'#c{i}'] = key
        values[f':c{i}'] = to_dynamo(value)
        sets.append(f'#conv.#c{i} = :c{i}')
    return {
        'Key': {'training_id': training_id},
        'UpdateExpression': 'SET ' + ', '.join(sets),
        'ConditionExpression': '#conv.#cst = :from',
        'ExpressionAttributeNames': names,
        'ExpressionAttributeValues': values,
    }


def apply_conversion_transition(table: Any, training_id: str, transition: Transition) -> bool:
    """Write a transition iff conversion.status still equals its `from_status`.
    Returns False (no-op) when another writer got there first (Req 7.5, 7.6)."""
    try:
        table.update_item(**transition_update_kwargs(training_id, transition))
        return True
    except Exception as e:  # noqa: BLE001 - only the lost race is swallowed
        code = (getattr(e, 'response', None) or {}).get('Error', {}).get('Code')
        if code == 'ConditionalCheckFailedException':
            return False
        raise


def build_finalize_event(training_id: str) -> Dict[str, Any]:
    """The API-Gateway-shaped Packaging invoke that finalizes a conversion
    (the `_trigger_component_creation` system-caller pattern)."""
    return {
        'httpMethod': 'POST',
        'path': f'/api/v1/training/{training_id}/package',
        'pathParameters': {'id': training_id},
        'body': json.dumps({'finalize_conversion': True, 'auto_triggered': True}),
        'requestContext': {'authorizer': {'claims': {
            'sub': 'system', 'email': 'system@example.com', 'cognito:username': 'system'}}},
    }


def invoke_finalize(lambda_client: Any, function_name: Optional[str], training_id: str) -> bool:
    """Asynchronously invoke the Packaging Lambda to finalize (best effort; a
    lost invoke leaves the record Finalizing, and the Package action retries)."""
    if not function_name:
        return False
    lambda_client.invoke(FunctionName=function_name, InvocationType='Event',
                         Payload=json.dumps(build_finalize_event(training_id)))
    return True


def reconcile_conversion(table: Any, record: Dict[str, Any], sm_status: Optional[str],
                         sm_failure_reason: Optional[str], sm_artifact_s3: Optional[str],
                         now_ms: int, lambda_client: Any = None,
                         packaging_function: Optional[str] = None) -> Optional[Transition]:
    """The one entry point both status writers call for a Conversion_Record:
    plan -> conditional write -> (winner only) finalize invoke. Returns the
    applied transition, or None when nothing changed."""
    transition = plan_conversion_transition(record, sm_status, sm_failure_reason,
                                            sm_artifact_s3, now_ms)
    if transition is None:
        return None
    if not apply_conversion_transition(table, record['training_id'], transition):
        return None
    if transition.invoke_finalize and lambda_client is not None:
        try:
            if not invoke_finalize(lambda_client, packaging_function, record['training_id']):
                logger.warning('No packaging function configured; %s stays Finalizing until '
                               'the Package action finalizes it', record['training_id'])
        except Exception as e:  # noqa: BLE001 - the claim stands; Package retries (Req 7.9)
            logger.warning('Finalize invoke for %s failed (%s); the record stays Finalizing '
                           'until the Package action finalizes it', record['training_id'], e)
    return transition


def apply_transition_to_record(record: Dict[str, Any], transition: Transition) -> Dict[str, Any]:
    """The record as it reads after `transition` (for responses; pure)."""
    updated = dict(record)
    updated.update(transition.set_fields)
    conv = dict(updated.get('conversion') or {})
    conv.update(transition.conversion_fields)
    conv['status'] = transition.to_status
    updated['conversion'] = conv
    return updated


# ---------------------------------------------------------------------------
# Untrusted artifact validation (Requirement 8)
# ---------------------------------------------------------------------------

ONNX_MEMBER = 'model.onnx'
METADATA_MEMBER = 'training_metadata.json'
_ARTIFACT_MEMBERS = (ONNX_MEMBER, METADATA_MEMBER)
_ONNX_FLOAT = 1  # TensorProto.DataType.FLOAT
_ONNX_TYPE_NAMES = {1: 'FLOAT', 2: 'UINT8', 3: 'INT8', 4: 'UINT16', 5: 'INT16', 6: 'INT32',
                    7: 'INT64', 8: 'STRING', 9: 'BOOL', 10: 'FLOAT16', 11: 'DOUBLE',
                    12: 'UINT32', 13: 'UINT64', 16: 'BFLOAT16'}


@dataclass
class ValidatedArtifact:
    onnx_path: str
    onnx_sha256: str
    metadata: Dict[str, Any]
    summary: Dict[str, Any]


def _normalise_member(name: str) -> str:
    while name.startswith('./'):
        name = name[2:]
    return name


def extract_artifact_members(tar_path: str, workdir: str,
                             onnx_cap: int = ONNX_MEMBER_CAP,
                             metadata_cap: int = METADATA_MEMBER_CAP) -> Dict[str, Tuple[str, str]]:
    """Read ONLY the two expected regular-file members out of the job's
    tarball, rejecting anything else (Req 8.2). Never extractall: a member that
    is a symlink / hard link / device, an absolute path, a '..' component, a
    duplicate, an oversize member or any extra name fails the conversion.
    Returns {name: (path, sha256)}."""
    caps = {ONNX_MEMBER: onnx_cap, METADATA_MEMBER: metadata_cap}
    found: Dict[str, Tuple[str, str]] = {}
    try:
        with tarfile.open(tar_path, mode='r:*') as tar:
            for member in tar:
                raw = member.name
                if raw.startswith('/') or os.path.isabs(raw):
                    raise ConversionValidationError('tar-member', f'absolute path {raw!r}')
                name = _normalise_member(raw)
                if '..' in name.split('/'):
                    raise ConversionValidationError('tar-member', f"'..' in member {raw!r}")
                if name in ('', '.') and member.isdir():
                    continue  # the root directory entry itself
                if member.issym() or member.islnk():
                    raise ConversionValidationError(
                        'tar-member', f'link member {raw!r} -> {member.linkname!r}')
                if not member.isreg():
                    raise ConversionValidationError('tar-member', f'non-regular member {raw!r}')
                if name not in _ARTIFACT_MEMBERS:
                    raise ConversionValidationError('tar-member', f'unexpected member {raw!r}')
                if name in found:
                    raise ConversionValidationError('tar-member', f'duplicate member {raw!r}')
                if member.size > caps[name]:
                    raise ConversionValidationError(
                        'size', f'{name} is {member.size} bytes; the limit is {caps[name]}')
                source = tar.extractfile(member)
                if source is None:
                    raise ConversionValidationError('tar-member', f'unreadable member {raw!r}')
                dest = os.path.join(workdir, name)
                digest = hashlib.sha256()
                written = 0
                with source, open(dest, 'wb') as out:
                    for chunk in iter(lambda: source.read(1 << 20), b''):
                        written += len(chunk)
                        if written > caps[name]:
                            raise ConversionValidationError('size', f'{name} exceeds {caps[name]} bytes')
                        digest.update(chunk)
                        out.write(chunk)
                found[name] = (dest, digest.hexdigest())
    except ConversionValidationError:
        raise
    except (tarfile.TarError, OSError, EOFError) as e:
        raise ConversionValidationError('tar', f'unreadable artifact: {e}')
    missing = [m for m in _ARTIFACT_MEMBERS if m not in found]
    if missing:
        raise ConversionValidationError('tar-member', f'missing member(s) {missing}')
    return found


# --- torch-free ONNX reader (protobuf wire format over an mmap) -------------

def _varint(buf, i: int) -> Tuple[int, int]:
    shift = value = 0
    while True:
        if i >= len(buf):
            raise ValueError('truncated varint')
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7
        if shift > 70:
            raise ValueError('varint too long')


def _fields(buf, start: int = 0, end: Optional[int] = None) -> Iterable[Tuple[int, int, Any]]:
    """Yield (field_number, wire_type, value) over buf[start:end]. Length-
    delimited values come back as (offset, length) so large payloads (raw
    tensor data) are skipped without copying."""
    i = start
    end = len(buf) if end is None else end
    while i < end:
        tag, i = _varint(buf, i)
        fnum, wt = tag >> 3, tag & 7
        if wt == 0:
            value, i = _varint(buf, i)
            yield fnum, wt, value
        elif wt == 2:
            length, i = _varint(buf, i)
            if i + length > end:
                raise ValueError('truncated field')
            yield fnum, wt, (i, length)
            i += length
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        else:
            raise ValueError(f'unsupported wire type {wt}')


def _string(buf, span: Tuple[int, int]) -> str:
    off, length = span
    return bytes(buf[off:off + length]).decode('utf-8', 'replace')


def _value_info(buf, span: Tuple[int, int]) -> Dict[str, Any]:
    off, length = span
    info: Dict[str, Any] = {'name': None, 'dtype': None, 'shape': None}
    for fnum, wt, value in _fields(buf, off, off + length):
        if fnum == 1 and wt == 2:
            info['name'] = _string(buf, value)
        elif fnum == 2 and wt == 2:  # TypeProto
            t_off, t_len = value
            for f2, w2, v2 in _fields(buf, t_off, t_off + t_len):
                if f2 != 1 or w2 != 2:  # tensor_type only
                    if w2 == 2:
                        info['dtype'] = f'non-tensor({f2})'
                    continue
                tt_off, tt_len = v2
                for f3, w3, v3 in _fields(buf, tt_off, tt_off + tt_len):
                    if f3 == 1 and w3 == 0:
                        info['dtype'] = _ONNX_TYPE_NAMES.get(v3, f'type{v3}')
                    elif f3 == 2 and w3 == 2:
                        dims: List[Any] = []
                        s_off, s_len = v3
                        for f4, w4, v4 in _fields(buf, s_off, s_off + s_len):
                            if f4 != 1 or w4 != 2:
                                continue
                            dim_value: Any = None
                            d_off, d_len = v4
                            for f5, w5, v5 in _fields(buf, d_off, d_off + d_len):
                                if f5 == 1 and w5 == 0:
                                    dim_value = v5
                                elif f5 == 2 and w5 == 2:
                                    dim_value = _string(buf, v5)
                            dims.append(dim_value)
                        info['shape'] = dims
    return info


def _tensor_is_external(buf, span: Tuple[int, int]) -> Tuple[Optional[str], bool]:
    """(name, external?) of a TensorProto: data_location (14) == EXTERNAL or
    any external_data (13) entry."""
    off, length = span
    name, external = None, False
    for fnum, wt, value in _fields(buf, off, off + length):
        if fnum == 8 and wt == 2:
            name = _string(buf, value)
        elif fnum == 14 and wt == 0 and value == 1:
            external = True
        elif fnum == 13 and wt == 2:
            external = True
    return name, external


def _scan_graph(buf, span: Tuple[int, int], acc: Dict[str, Any], depth: int = 0) -> None:
    """Walk a GraphProto: node domains (recursing into subgraph attributes),
    initializers' external-data use, and (top level) inputs / outputs."""
    if depth > 16:
        raise ValueError('subgraphs nested too deeply')
    off, length = span
    for fnum, wt, value in _fields(buf, off, off + length):
        if wt != 2:
            continue
        if fnum == 1:  # NodeProto
            n_off, n_len = value
            domain = ''
            for f2, w2, v2 in _fields(buf, n_off, n_off + n_len):
                if f2 == 7 and w2 == 2:
                    domain = _string(buf, v2)
                elif f2 == 5 and w2 == 2:  # AttributeProto
                    a_off, a_len = v2
                    for f3, w3, v3 in _fields(buf, a_off, a_off + a_len):
                        if w3 != 2:
                            continue
                        if f3 in (6, 11):  # g / graphs
                            _scan_graph(buf, v3, acc, depth + 1)
                        elif f3 in (5, 10):  # t / tensors
                            t_name, ext = _tensor_is_external(buf, v3)
                            if ext:
                                acc['external_data'].append(t_name or '(attribute tensor)')
            acc['domains'].add(domain or 'ai.onnx')
        elif fnum == 5:  # initializer
            t_name, ext = _tensor_is_external(buf, value)
            if ext:
                acc['external_data'].append(t_name or '(initializer)')
            if depth == 0 and t_name:
                acc['initializer_names'].add(t_name)
        elif fnum == 15:  # sparse_initializer
            s_off, s_len = value
            for f2, w2, v2 in _fields(buf, s_off, s_off + s_len):
                if f2 in (1, 2) and w2 == 2:
                    t_name, ext = _tensor_is_external(buf, v2)
                    if ext:
                        acc['external_data'].append(t_name or '(sparse initializer)')
        elif depth == 0 and fnum == 11:
            acc['inputs'].append(_value_info(buf, value))
        elif depth == 0 and fnum == 12:
            acc['outputs'].append(_value_info(buf, value))


def read_onnx_structure(path: str) -> Dict[str, Any]:
    """ir_version, opsets, operator domains, local functions, external-data
    use and graph I/O of an ONNX file, from an mmap (weights are skipped by
    length, so memory stays proportional to graph metadata). Raises
    ConversionValidationError when the bytes are not a ModelProto."""
    acc: Dict[str, Any] = {'ir_version': None, 'opsets': {}, 'domains': set(), 'functions': 0,
                           'external_data': [], 'inputs': [], 'outputs': [],
                           'initializer_names': set(), 'graphs': 0}
    try:
        with open(path, 'rb') as fh:
            size = os.fstat(fh.fileno()).st_size
            if size == 0:
                raise ConversionValidationError('onnx', 'model.onnx is empty')
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                buf = memoryview(mm)
                try:
                    for fnum, wt, value in _fields(buf):
                        if fnum == 1 and wt == 0:
                            acc['ir_version'] = value
                        elif fnum == 7 and wt == 2:
                            acc['graphs'] += 1
                            _scan_graph(buf, value, acc)
                        elif fnum == 8 and wt == 2:
                            o_off, o_len = value
                            domain, version = '', None
                            for f2, w2, v2 in _fields(buf, o_off, o_off + o_len):
                                if f2 == 1 and w2 == 2:
                                    domain = _string(buf, v2)
                                elif f2 == 2 and w2 == 0:
                                    version = v2
                            acc['opsets'][domain or 'ai.onnx'] = version
                        elif fnum == 25 and wt == 2:
                            acc['functions'] += 1
                finally:
                    buf.release()
    except ConversionValidationError:
        raise
    except (ValueError, IndexError, OSError) as e:
        raise ConversionValidationError('onnx', f'model.onnx is not a readable ONNX ModelProto: {e}')
    if acc['ir_version'] is None or acc['graphs'] != 1:
        raise ConversionValidationError('onnx', 'model.onnx is not an ONNX ModelProto (no IR version / graph)')
    # Graph inputs listed as initializers are weights, not feeds.
    acc['inputs'] = [i for i in acc['inputs'] if i['name'] not in acc['initializer_names']]
    acc['domains'] = sorted(acc['domains'])
    acc.pop('initializer_names')
    acc.pop('graphs')
    return acc


def _static(shape: Any) -> bool:
    return isinstance(shape, list) and all(isinstance(d, int) and d > 0 for d in shape)


def check_onnx_against_record(structure: Dict[str, Any], arch: str, network_input: int,
                              num_classes: int) -> Dict[str, Any]:
    """Requirement 8.3 on a parsed structure (pure). Returns the summary kept
    on the record; raises ConversionValidationError naming the rule."""
    if structure['ir_version'] > FLEET_MAX_IR:
        raise ConversionValidationError(
            'ir-version', f'IR version {structure["ir_version"]} > {FLEET_MAX_IR} '
                          f'(onnxruntime {FLEET_FLOOR_ORT} on JP5 / CPU images cannot load it)')
    opset = structure['opsets'].get('ai.onnx')
    if opset is None or opset > FLEET_MAX_OPSET:
        raise ConversionValidationError(
            'opset', f'default-domain opset {opset} > {FLEET_MAX_OPSET} (onnxruntime {FLEET_FLOOR_ORT})')
    bad = [d for d in structure['domains'] if d not in ALLOWED_OP_DOMAINS]
    bad += [d for d in structure['opsets'] if d not in ALLOWED_OP_DOMAINS]
    if bad:
        raise ConversionValidationError('op-domain', f'non-default operator domain(s) {sorted(set(bad))}')
    if structure['functions']:
        raise ConversionValidationError('op-domain', f'{structure["functions"]} local function(s)')
    if structure['external_data']:
        raise ConversionValidationError('external-data',
                                        f'external-data tensor(s) {structure["external_data"][:3]}')
    inputs = structure['inputs']
    if len(inputs) != 1:
        raise ConversionValidationError('input', f'expected exactly 1 graph input, got {len(inputs)}')
    expected_input = [1, 3, network_input, network_input]
    if inputs[0]['dtype'] != 'FLOAT' or inputs[0]['shape'] != expected_input:
        raise ConversionValidationError(
            'input', f'graph input {inputs[0]["name"]} is {inputs[0]["dtype"]} {inputs[0]["shape"]}; '
                     f'expected FLOAT {expected_input}')
    outputs = structure['outputs']
    quoted = ', '.join(f'{o["name"]}={o["shape"]}:{o["dtype"]}' for o in outputs)
    expected_count = 1 if arch == 'yolo' else 2
    if len(outputs) != expected_count:
        contract = (f'[1, {num_classes + 4}, N]' if arch == 'yolo'
                    else f'[1, Q, 4] + [1, Q, {num_classes + 1}]')
        raise ConversionValidationError(
            'output', f'expected exactly {expected_count} output{"s" if expected_count > 1 else ""} '
                      f'{contract}; got {len(outputs)}: [{quoted}]')
    for o in outputs:
        if o['dtype'] != 'FLOAT' or not _static(o['shape']) or len(o['shape']) != 3 or o['shape'][0] != 1:
            raise ConversionValidationError(
                'output', f'outputs must be static FLOAT rank-3 batch-1 tensors; got [{quoted}]')
    summary: Dict[str, Any] = {
        'ir_version': structure['ir_version'],
        'opset': opset,
        'input': expected_input,
        'outputs': [list(o['shape']) for o in outputs],
        'output_names': [o['name'] for o in outputs],
    }
    if arch == 'yolo':
        channels = num_classes + 4
        _b, a, b = outputs[0]['shape']
        if not ((a == channels and b > channels) or (b == channels and a > channels)):
            raise ConversionValidationError(
                'output', f'output {outputs[0]["shape"]} is not [1, {channels}, N] with N > {channels} '
                          f'(num_classes {num_classes} + 4); an embedded-NMS or one-to-one export is '
                          f'[1, K, 6] and a segmentation head adds mask channels')
        summary['anchors'] = max(a, b)
    else:
        shapes = [o['shape'] for o in outputs]
        boxes = next((s for s in shapes if s[2] == 4), None)
        logits = next((s for s in shapes if s is not boxes), None)
        if boxes is None or logits is None or boxes[1] != logits[1] or logits[2] != num_classes + 1:
            raise ConversionValidationError(
                'output', f'outputs [{quoted}] are not [1, Q, 4] + [1, Q, {num_classes + 1}] '
                          f'(num_classes {num_classes} + 1 background slot)')
        summary['top_k'] = boxes[1]
    return summary


def check_metadata_against_record(metadata: Any, arch: str, network_input: int, num_classes: int,
                                  onnx_sha256: str, summary: Dict[str, Any]) -> None:
    """Requirement 8.4 / 8.5: the job's own claims must agree with the record
    and with the graph itself (pure)."""
    if not isinstance(metadata, dict):
        raise ConversionValidationError('metadata', 'training_metadata.json is not a JSON object')
    if metadata.get('detection_arch') != arch:
        raise ConversionValidationError(
            'metadata', f'detection_arch {metadata.get("detection_arch")!r} != record {arch!r}')
    size_key = 'imgsz' if arch == 'yolo' else 'resolution'
    if _plain(metadata.get(size_key)) != network_input:
        raise ConversionValidationError(
            'metadata', f'{size_key} {metadata.get(size_key)!r} != record network input {network_input}')
    if _plain(metadata.get('num_classes')) != num_classes:
        raise ConversionValidationError(
            'metadata', f'num_classes {metadata.get("num_classes")!r} != record {num_classes}')
    if metadata.get('onnx_sha256') != onnx_sha256:
        raise ConversionValidationError(
            'sha256', f'metadata onnx_sha256 {metadata.get("onnx_sha256")!r} != model.onnx {onnx_sha256}')
    if arch == 'yolo':
        if _plain(metadata.get('onnx_output_shape')) != summary['outputs'][0]:
            raise ConversionValidationError(
                'metadata', f'onnx_output_shape {metadata.get("onnx_output_shape")!r} != graph '
                            f'{summary["outputs"][0]}')
    else:
        claimed = sorted(_plain(metadata.get('onnx_output_shapes')) or [])
        if claimed != sorted(summary['outputs']):
            raise ConversionValidationError(
                'metadata', f'onnx_output_shapes {metadata.get("onnx_output_shapes")!r} != graph '
                            f'{summary["outputs"]}')
        if _plain(metadata.get('top_k')) != summary['top_k']:
            raise ConversionValidationError(
                'metadata', f'top_k {metadata.get("top_k")!r} != graph queries {summary["top_k"]}')


def validate_conversion_artifact(tar_path: str, record: Dict[str, Any], workdir: str) -> ValidatedArtifact:
    """The whole Requirement 8 gate for one Conversion_Artifact (pure: local
    files only). The record is the source of truth for arch, input size and
    class count."""
    det = _plain(record.get('detection') or {})
    arch = det.get('detection_arch')
    network_input = det.get('network_input_width')
    num_classes = det.get('num_classes')
    if arch not in INPUT_BOUNDS or not isinstance(network_input, int) or not isinstance(num_classes, int):
        raise ConversionValidationError('record', 'the record carries no usable detection fields')
    size = os.path.getsize(tar_path)
    if size > ARTIFACT_SIZE_CAP:
        raise ConversionValidationError('size', f'artifact is {size} bytes; the limit is {ARTIFACT_SIZE_CAP}')
    members = extract_artifact_members(tar_path, workdir)
    onnx_path, onnx_sha = members[ONNX_MEMBER]
    meta_path, _meta_sha = members[METADATA_MEMBER]
    structure = read_onnx_structure(onnx_path)
    summary = check_onnx_against_record(structure, arch, network_input, num_classes)
    try:
        with open(meta_path, 'r', encoding='utf-8') as fh:
            metadata = json.load(fh)
    except (OSError, ValueError) as e:
        raise ConversionValidationError('metadata', f'training_metadata.json is not valid JSON: {e}')
    check_metadata_against_record(metadata, arch, network_input, num_classes, onnx_sha, summary)
    summary.update(_informational_claims(metadata))
    summary['onnx_bytes'] = os.path.getsize(onnx_path)
    return ValidatedArtifact(onnx_path=onnx_path, onnx_sha256=onnx_sha, metadata=metadata,
                             summary=summary)


def _informational_claims(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """The job's self-reported, display-only facts (exporter, fleet-floor
    runtime, Parity_Check maxima), reduced to bounded strings and finite
    numbers before they reach the record: the metadata is untrusted."""
    def text(value: Any, limit: int = 120) -> Optional[str]:
        return str(value)[:limit] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None

    def number(value: Any) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if value == value and abs(value) != float('inf') else None

    floor = metadata.get('fleet_floor') if isinstance(metadata.get('fleet_floor'), dict) else {}
    parity = metadata.get('parity') if isinstance(metadata.get('parity'), dict) else {}
    max_abs = parity.get('max_abs') if isinstance(parity.get('max_abs'), dict) else {}
    runtimes = parity.get('runtimes') if isinstance(parity.get('runtimes'), dict) else {}
    return {
        'exporter': text(metadata.get('exporter')),
        'fleet_floor_onnxruntime': text(floor.get('onnxruntime'), 32),
        'parity_max_abs': {k: number(max_abs.get(k)) for k in ('box_max_abs', 'score_max_abs')},
        'parity_runtimes': sorted(t for t in (text(k, 48) for k in list(runtimes)[:4]) if t),
    }
