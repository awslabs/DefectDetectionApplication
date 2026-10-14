"""
Unit + property tests for the shared detection_training helpers
(portal-detection-training task 1.2).

Pure module — imported directly, no moto needed. conftest puts the shared
layer on sys.path.

# Validates: Requirements 2.2, 2.3, 2.4, 2.6, 3.2, 3.8, 3.9, 4.4, 5.3
"""
import io
import os
import tarfile
import tempfile

import pytest
from hypothesis import given, strategies as st

import detection_training as dt


# ---------------------------------------------------------------------------
# Fixtures: manifest entries
# ---------------------------------------------------------------------------

def dda_detection_entry(class_map=None, n_boxes=2):
    """Shape written by dda_manifest._serialize_object_detection."""
    class_map = class_map or {'0': 'blue_plate'}
    return {
        'source-ref': 's3://bucket/imts-plates-luggage/frame_0001.jpg',
        'bounding-box': {
            'image_size': [{'width': 2001, 'height': 2352, 'depth': 3}],
            'annotations': [
                {'class_id': 0, 'left': 10 * i, 'top': 20, 'width': 300, 'height': 280}
                for i in range(n_boxes)
            ],
        },
        'bounding-box-metadata': {
            'objects': [{'confidence': 1.0} for _ in range(n_boxes)],
            'class-map': class_map,
            'type': 'groundtruth/object-detection',
            'human-annotated': 'yes',
            'creation-date': '2026-09-12T00:00:00',
            'job-name': 'labeling-9cbcdb4c',
        },
    }


def gt_detection_entry():
    """A SageMaker Ground Truth BoundingBox job output (job-named attribute)."""
    return {
        'source-ref': 's3://bucket/frames/frame_0007.jpg',
        'plates-bbox': {
            'image_size': [{'width': 1920, 'height': 1080, 'depth': 3}],
            'annotations': [{'class_id': 1, 'left': 5, 'top': 5, 'width': 50, 'height': 50}],
        },
        'plates-bbox-metadata': {
            'objects': [{'confidence': 0.9}],
            'class-map': {'1': 'luggage', '0': 'plate'},
            'type': 'groundtruth/object-detection',
            'human-annotated': 'yes',
            'creation-date': '2026-09-12T00:00:00',
            'job-name': 'plates-bbox',
        },
    }


def dda_classification_entry():
    return {
        'source-ref': 's3://bucket/img.jpg',
        'anomaly-label': 1,
        'anomaly-label-metadata': {
            'class-name': 'anomaly', 'confidence': 1.0,
            'type': 'groundtruth/image-classification',
            'job-name': 'j', 'human-annotated': 'yes', 'creation-date': 'x',
        },
    }


# ---------------------------------------------------------------------------
# Record predicate (Req 4.4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('job,expected', [
    ({'model_type': 'object_detection', 'runtime': 'onnx'}, True),
    ({'model_type': 'object_detection', 'runtime': 'ONNX', 'source': 'trained'}, True),
    # runtime absent: an object_detection record whose artifact is a
    # TorchScript .pt (earlier specs' fixtures) still needs Neo / the export job.
    ({'model_type': 'object_detection'}, False),
    ({'model_type': 'object_detection', 'source': 'trained'}, False),
    ({'model_type': 'object_detection', 'runtime': 'dlr'}, False),
    ({'model_type': 'object_detection', 'runtime': 'onnx', 'source': 'imported'}, False),
    ({'model_type': 'classification', 'runtime': 'onnx'}, False),
    ({'model_type': 'segmentation', 'source': 'trained'}, False),
    ({}, False),
    (None, False),
])
def test_is_trained_detection_record(job, expected):
    assert dt.is_trained_detection_record(job) is expected


# ---------------------------------------------------------------------------
# Training image (Req 3.2)
# ---------------------------------------------------------------------------

def test_resolve_image_regional_default():
    uri = dt.resolve_detection_training_image('eu-west-1', None)
    assert uri == ('763104351884.dkr.ecr.eu-west-1.amazonaws.com/'
                   'pytorch-training:2.5.1-gpu-py311-cu124-ubuntu22.04-sagemaker')


def test_resolve_image_override_wins():
    assert dt.resolve_detection_training_image(
        'us-east-1', ' 123.dkr.ecr.us-east-1.amazonaws.com/custom:1 ') == \
        '123.dkr.ecr.us-east-1.amazonaws.com/custom:1'


def test_resolve_image_empty_override_uses_default():
    assert 'us-west-2' in dt.resolve_detection_training_image('us-west-2', '')


def test_resolve_image_requires_region_without_override():
    with pytest.raises(ValueError):
        dt.resolve_detection_training_image('', None)


# ---------------------------------------------------------------------------
# Hyperparameters (Req 3.8, 3.9)
# ---------------------------------------------------------------------------

def test_hyperparameter_defaults():
    assert dt.parse_detection_hyperparameters(None) == {
        'imgsz': 1280, 'epochs': 100, 'batch': 4, 'base_weights': 'yolo11s.pt',
        'patience': 30, 'score_threshold': 0.25, 'iou_threshold': 0.45, 'onnx_opset': 17,
    }


def test_hyperparameters_accept_string_numbers():
    parsed = dt.parse_detection_hyperparameters(
        {'imgsz': '640', 'epochs': '10', 'batch': 8.0, 'score_threshold': '0.3'})
    assert parsed['imgsz'] == 640 and isinstance(parsed['imgsz'], int)
    assert parsed['epochs'] == 10
    assert parsed['batch'] == 8
    assert parsed['score_threshold'] == 0.3


@pytest.mark.parametrize('raw,field', [
    ({'imgsz': 300}, 'imgsz'),           # below 320
    ({'imgsz': 1000}, 'imgsz'),          # not a multiple of 32
    ({'imgsz': 2080}, 'imgsz'),          # above 2048
    ({'imgsz': 'big'}, 'imgsz'),
    ({'imgsz': 640.5}, 'imgsz'),
    ({'imgsz': True}, 'imgsz'),
    ({'epochs': 0}, 'epochs'),
    ({'epochs': 1001}, 'epochs'),
    ({'batch': 0}, 'batch'),
    ({'batch': 65}, 'batch'),
    ({'patience': -1}, 'patience'),
    ({'onnx_opset': 9}, 'onnx_opset'),
    ({'base_weights': 'yolo11s'}, 'base_weights'),
    ({'base_weights': '../evil.pt'}, 'base_weights'),
    ({'base_weights': 'a b.pt'}, 'base_weights'),
    ({'score_threshold': 0}, 'score_threshold'),
    ({'score_threshold': 1}, 'score_threshold'),
    ({'score_threshold': 'x'}, 'score_threshold'),
    ({'iou_threshold': 1.5}, 'iou_threshold'),
])
def test_hyperparameter_violations_name_the_field(raw, field):
    with pytest.raises(ValueError) as exc:
        dt.parse_detection_hyperparameters(raw)
    assert field in str(exc.value)


def test_unknown_hyperparameter_rejected():
    with pytest.raises(ValueError) as exc:
        dt.parse_detection_hyperparameters({'classification_logic': 'seg_head'})
    assert 'classification_logic' in str(exc.value)


@given(
    imgsz=st.integers(min_value=10, max_value=64).map(lambda k: k * 32),
    epochs=st.integers(min_value=1, max_value=1000),
    batch=st.integers(min_value=1, max_value=64),
    patience=st.integers(min_value=0, max_value=1000),
    score=st.floats(min_value=0.01, max_value=0.99),
    iou=st.floats(min_value=0.01, max_value=0.99),
)
def test_valid_hyperparameters_round_trip(imgsz, epochs, batch, patience, score, iou):
    raw = {'imgsz': imgsz, 'epochs': epochs, 'batch': batch, 'patience': patience,
           'score_threshold': score, 'iou_threshold': iou}
    parsed = dt.parse_detection_hyperparameters(raw)
    for key, value in raw.items():
        assert parsed[key] == value
    # Idempotent: parsing the parsed dict changes nothing.
    assert dt.parse_detection_hyperparameters(parsed) == parsed
    env = dt.detection_job_environment('s3://b/m.manifest', parsed)
    assert env['IMGSZ'] == str(imgsz)
    assert all(isinstance(v, str) for v in env.values())
    assert 'IMAGES_S3' not in env


def test_job_environment_names():
    env = dt.detection_job_environment(
        's3://b/labeled/output.manifest', dt.parse_detection_hyperparameters(None))
    assert env == {
        'MANIFEST_S3': 's3://b/labeled/output.manifest', 'IMGSZ': '1280', 'EPOCHS': '100',
        'BATCH': '4', 'BASE_WEIGHTS': 'yolo11s.pt', 'PATIENCE': '30', 'ONNX_OPSET': '17',
    }


# ---------------------------------------------------------------------------
# Manifest validation (Req 2.2, 2.3, 2.4, 2.6)
# ---------------------------------------------------------------------------

def test_detect_bbox_attribute_dda_literal():
    assert dt.detect_bbox_attribute(dda_detection_entry()) == 'bounding-box'


def test_detect_bbox_attribute_gt_job_named():
    assert dt.detect_bbox_attribute(gt_detection_entry()) == 'plates-bbox'


def test_detect_bbox_attribute_none_for_classification():
    assert dt.detect_bbox_attribute(dda_classification_entry()) is None
    assert dt.detect_bbox_attribute({'source-ref': 'x'}) is None
    assert dt.detect_bbox_attribute('not a dict') is None


def test_validate_dda_detection_entry():
    result = dt.validate_detection_manifest_entry(
        dda_detection_entry({'1': 'luggage', '0': 'blue_plate'}))
    assert result['valid'], result['errors']
    assert result['attribute'] == 'bounding-box'
    # class-map sorted by integer id, not by string key order
    assert result['class_names'] == ['blue_plate', 'luggage']


def test_validate_gt_detection_entry():
    result = dt.validate_detection_manifest_entry(gt_detection_entry())
    assert result['valid'], result['errors']
    assert result['attribute'] == 'plates-bbox'
    assert result['class_names'] == ['plate', 'luggage']


def test_validate_classification_entry_names_the_problem_without_transformer_advice():
    result = dt.validate_detection_manifest_entry(dda_classification_entry())
    assert not result['valid']
    joined = ' '.join(result['errors'])
    assert 'anomaly-label' in joined
    assert 'bounding-box' in joined
    assert 'Transform' not in joined
    assert 'anomaly-label' in result['detected_attributes']


def test_validate_rejects_missing_class_map_and_annotations():
    entry = dda_detection_entry()
    del entry['bounding-box-metadata']['class-map']
    entry['bounding-box']['annotations'] = 'nope'
    result = dt.validate_detection_manifest_entry(entry)
    assert not result['valid']
    joined = ' '.join(result['errors'])
    assert 'class-map' in joined and 'annotations' in joined


def test_validate_rejects_bad_source_ref_and_non_object():
    entry = dda_detection_entry()
    entry['source-ref'] = 5
    assert not dt.validate_detection_manifest_entry(entry)['valid']
    assert not dt.validate_detection_manifest_entry(['list'])['valid']


def test_class_names_from_class_map_sorted_numerically():
    assert dt.class_names_from_class_map({'10': 'j', '2': 'b', '0': 'a'}) == ['a', 'b', 'j']


# ---------------------------------------------------------------------------
# Device manifest (Req 5.3)
# ---------------------------------------------------------------------------

def test_device_manifest_shape():
    m = dt.build_detection_device_manifest(
        image_width=1280, image_height=1280, num_classes=1, class_names=['blue_plate'],
        score_threshold=0.25, iou_threshold=0.45)
    assert m['runtime'] == 'onnx' and m['runtime_artifact'] == 'model.onnx'
    assert m['task'] == 'object_detection'
    stage = m['model_graph']['stages'][0]
    assert stage['type'] == 'yolo_object_detection'
    assert stage['input_shape'] == [1, 3, 1280, 1280]
    assert stage['output_shape'] == [1, 5, 8400]
    assert stage['image_range_scale'] is True and stage['normalize'] is False
    assert stage['threshold'] == 0.25 and stage['num_classes'] == 1
    assert m['preprocessing'] == {'resize': [1280, 1280], 'channel_order': 'RGB'}
    assert m['dataset'] == {'image_width': 1280, 'image_height': 1280}
    assert m['detection'] == {
        'layout': 'yolo', 'num_classes': 1, 'score_threshold': 0.25, 'network_input': 1280,
        'preserve_aspect': True, 'iou_threshold': 0.45, 'class_names': ['blue_plate'],
    }


def test_device_manifest_preserve_aspect_defaults_true_and_is_overridable():
    on = dt.build_detection_device_manifest(
        image_width=640, image_height=640, num_classes=2, class_names=None,
        score_threshold=0.3, iou_threshold=0.5)
    off = dt.build_detection_device_manifest(
        image_width=640, image_height=640, num_classes=2, class_names=None,
        score_threshold=0.3, iou_threshold=0.5, preserve_aspect=False)
    assert on['detection']['preserve_aspect'] is True
    assert off['detection']['preserve_aspect'] is False
    assert 'class_names' not in on['detection']


# ---------------------------------------------------------------------------
# Sourcedir (Req 3.3)
# ---------------------------------------------------------------------------

def test_build_sourcedir_tarball_flat_root():
    with tempfile.TemporaryDirectory() as td:
        code = os.path.join(td, 'code')
        os.makedirs(os.path.join(code, 'nested'))
        for name in dt.DETECTION_SOURCEDIR_FILES:
            with open(os.path.join(code, name), 'w') as fh:
                fh.write(f'# {name}\n')
        with open(os.path.join(code, '.hidden'), 'w') as fh:
            fh.write('x')
        out = os.path.join(td, 'sourcedir.tar.gz')
        names = dt.build_sourcedir_tarball(code, out)
        assert names == sorted(dt.DETECTION_SOURCEDIR_FILES)
        with tarfile.open(out, 'r:gz') as tar:
            members = sorted(m.name for m in tar.getmembers())
        assert members == sorted(dt.DETECTION_SOURCEDIR_FILES)
        assert all('/' not in n for n in members)


def test_build_sourcedir_tarball_requires_entry_point():
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, 'requirements.txt'), 'w') as fh:
            fh.write('ultralytics\n')
        with pytest.raises(FileNotFoundError):
            dt.build_sourcedir_tarball(td, os.path.join(td, 'o.tar.gz'))
        with pytest.raises(FileNotFoundError):
            dt.build_sourcedir_tarball(os.path.join(td, 'missing'), os.path.join(td, 'o.tar.gz'))


def test_repo_entry_point_files_exist_for_bundling():
    """The CDK bundler copies exactly these four repo files; keep them present."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    expected = [
        'datasets/detection_training/train.py',
        'datasets/detection_training/requirements.txt',
        'datasets/manifest_to_detector_dataset.py',
        'datasets/dedupe_frames.py',
    ]
    for rel in expected:
        assert os.path.isfile(os.path.join(repo_root, rel)), rel


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def test_decode_final_metrics():
    assert dt.decode_final_metrics([
        {'MetricName': 'test:mAP50', 'Value': 0.995},
        {'MetricName': 'test:recall', 'Value': '1.0'},
        {'MetricName': 'bad', 'Value': None},
        {'Value': 3},
    ]) == {'test:mAP50': 0.995, 'test:recall': 1.0}
    assert dt.decode_final_metrics(None) == {}


# ===========================================================================
# RF-DETR + base model (rfdetr-training-and-transfer-learning task 3.2)
#
# Validates: Requirements 1.1, 3.2, 3.3, 3.4, 4.1, 4.2 of that spec. Every
# YOLO case above is a preservation pin and is untouched; the tests below
# only ADD the rf_detr branch and the base-model resolver.
# ===========================================================================

RFDETR_RESOLUTION_ERROR = (
    "Invalid hyperparameter 'resolution': must be a multiple of 32 between 224 and 1120")


# ---------------------------------------------------------------------------
# Arch
# ---------------------------------------------------------------------------

def test_detection_arch_vocabulary():
    assert dt.DETECTION_ARCHES == ('yolo', 'rf_detr')
    assert dt.ENTRY_POINT_FOR_ARCH == {'yolo': 'train.py', 'rf_detr': 'train_rfdetr.py'}
    assert dt.REQUIREMENTS_FOR_ARCH == {
        'yolo': 'requirements.txt', 'rf_detr': 'requirements-rfdetr.txt'}
    assert dt.CHECKPOINT_MEMBER_FOR_ARCH == {
        'yolo': 'best.pt', 'rf_detr': 'checkpoint_best_total.pth'}
    assert dt.RFDETR_SIZES == {'nano': 384, 'small': 512, 'medium': 576, 'large': 704}
    assert dt.RFDETR_TOP_K == 300
    assert dt.RFDETR_STAGE_TYPE == 'rf_detr_object_detection'


@pytest.mark.parametrize('raw,expected', [
    (None, 'yolo'), ('', 'yolo'), ('   ', 'yolo'),
    ('yolo', 'yolo'), ('YOLO', 'yolo'), (' rf_detr ', 'rf_detr'), ('RF_DETR', 'rf_detr'),
])
def test_normalize_detection_arch(raw, expected):
    assert dt.normalize_detection_arch(raw) == expected


@pytest.mark.parametrize('bad', ['detr', 'rfdetr', 'rf-detr', 'yolov8', 5])
def test_normalize_detection_arch_rejects_unknown(bad):
    with pytest.raises(ValueError) as exc:
        dt.normalize_detection_arch(bad)
    assert 'detection_arch' in str(exc.value)


def test_parse_hyperparameters_unknown_arch_rejected():
    with pytest.raises(ValueError) as exc:
        dt.parse_detection_hyperparameters({}, arch='detr')
    assert 'detection_arch' in str(exc.value)


# ---------------------------------------------------------------------------
# RF-DETR hyperparameter schema (Req 1.1, 3.2)
# ---------------------------------------------------------------------------

def test_rfdetr_hyperparameter_defaults():
    assert dt.parse_detection_hyperparameters(None, arch='rf_detr') == {
        'rfdetr_size': 'small', 'resolution': 512, 'epochs': 100, 'batch': 4,
        'grad_accum': 4, 'lr': 1e-4, 'patience': 10, 'score_threshold': 0.5,
        'onnx_opset': 17,
    }
    assert dt.parse_detection_hyperparameters({}, arch='rf_detr') == \
        dt.parse_detection_hyperparameters(None, arch='rf_detr')


def test_yolo_defaults_unchanged_with_explicit_arch():
    assert dt.parse_detection_hyperparameters(None, arch='yolo') == \
        dt.parse_detection_hyperparameters(None)


@pytest.mark.parametrize('size,native', sorted(dt.RFDETR_SIZES.items()))
def test_rfdetr_resolution_defaults_to_native_size(size, native):
    parsed = dt.parse_detection_hyperparameters({'rfdetr_size': size}, arch='rf_detr')
    assert parsed['rfdetr_size'] == size
    assert parsed['resolution'] == native
    # An explicit null behaves like "absent".
    assert dt.parse_detection_hyperparameters(
        {'rfdetr_size': size, 'resolution': None}, arch='rf_detr')['resolution'] == native


def test_rfdetr_explicit_resolution_respected():
    parsed = dt.parse_detection_hyperparameters(
        {'rfdetr_size': 'nano', 'resolution': 640}, arch='rf_detr')
    assert parsed['resolution'] == 640 and parsed['rfdetr_size'] == 'nano'


def test_rfdetr_size_is_case_insensitive():
    assert dt.parse_detection_hyperparameters(
        {'rfdetr_size': ' Medium '}, arch='rf_detr')['rfdetr_size'] == 'medium'


def test_rfdetr_hyperparameters_accept_string_numbers():
    parsed = dt.parse_detection_hyperparameters(
        {'resolution': '640', 'epochs': '10', 'batch': 8.0, 'grad_accum': '2',
         'lr': '0.0002', 'score_threshold': '0.3'}, arch='rf_detr')
    assert parsed['resolution'] == 640 and isinstance(parsed['resolution'], int)
    assert parsed['epochs'] == 10 and parsed['batch'] == 8 and parsed['grad_accum'] == 2
    assert parsed['lr'] == 0.0002 and parsed['score_threshold'] == 0.3


@pytest.mark.parametrize('resolution', [224, 512, 1120, 384, 576, 704, 640])
def test_rfdetr_resolution_accepts_multiples_of_32_in_range(resolution):
    parsed = dt.parse_detection_hyperparameters({'resolution': resolution}, arch='rf_detr')
    assert parsed['resolution'] == resolution


@pytest.mark.parametrize('resolution', [
    500,     # not a multiple of 32
    560,     # multiple of 56 (legacy RFDETRBase rule) but not of 32
    216,     # below 224
    192,     # multiple of 32 but below 224
    1152,    # multiple of 32 but above 1120
    0, -32,
])
def test_rfdetr_resolution_rule_is_multiple_of_32_in_224_1120(resolution):
    with pytest.raises(ValueError) as exc:
        dt.parse_detection_hyperparameters({'resolution': resolution}, arch='rf_detr')
    assert str(exc.value) == RFDETR_RESOLUTION_ERROR


@pytest.mark.parametrize('bad', ['big', 640.5, True])
def test_rfdetr_resolution_must_be_an_integer(bad):
    with pytest.raises(ValueError) as exc:
        dt.parse_detection_hyperparameters({'resolution': bad}, arch='rf_detr')
    assert "'resolution'" in str(exc.value)


@pytest.mark.parametrize('raw,field', [
    ({'epochs': 0}, 'epochs'),
    ({'epochs': 1001}, 'epochs'),
    ({'batch': 0}, 'batch'),
    ({'batch': 65}, 'batch'),
    ({'grad_accum': 0}, 'grad_accum'),
    ({'grad_accum': 65}, 'grad_accum'),
    ({'lr': 0}, 'lr'),
    ({'lr': 1}, 'lr'),
    ({'lr': -0.001}, 'lr'),
    ({'lr': 'fast'}, 'lr'),
    ({'patience': -1}, 'patience'),
    ({'patience': 1001}, 'patience'),
    ({'score_threshold': 0}, 'score_threshold'),
    ({'score_threshold': 1}, 'score_threshold'),
    ({'score_threshold': 'x'}, 'score_threshold'),
    ({'onnx_opset': 9}, 'onnx_opset'),
    ({'onnx_opset': 21}, 'onnx_opset'),
    ({'rfdetr_size': 'xlarge'}, 'rfdetr_size'),    # PML-licensed, excluded
    ({'rfdetr_size': '2xlarge'}, 'rfdetr_size'),
    ({'rfdetr_size': 'base'}, 'rfdetr_size'),
    ({'rfdetr_size': 5}, 'rfdetr_size'),
    ({'rfdetr_size': None}, 'rfdetr_size'),
])
def test_rfdetr_hyperparameter_violations_name_the_field(raw, field):
    with pytest.raises(ValueError) as exc:
        dt.parse_detection_hyperparameters(raw, arch='rf_detr')
    assert f"'{field}'" in str(exc.value)


@pytest.mark.parametrize('key', ['imgsz', 'iou_threshold', 'base_weights'])
def test_rfdetr_rejects_yolo_only_keys(key):
    with pytest.raises(ValueError) as exc:
        dt.parse_detection_hyperparameters({key: 640}, arch='rf_detr')
    assert key in str(exc.value)
    assert 'Unknown detection hyperparameter' in str(exc.value)


@pytest.mark.parametrize('key', ['rfdetr_size', 'resolution', 'grad_accum', 'lr'])
def test_yolo_rejects_rfdetr_only_keys(key):
    with pytest.raises(ValueError) as exc:
        dt.parse_detection_hyperparameters({key: 4})
    assert key in str(exc.value)


@given(
    size=st.sampled_from(sorted(dt.RFDETR_SIZES)),
    resolution=st.one_of(st.none(), st.integers(min_value=7, max_value=35).map(lambda k: k * 32)),
    epochs=st.integers(min_value=1, max_value=1000),
    batch=st.integers(min_value=1, max_value=64),
    grad_accum=st.integers(min_value=1, max_value=64),
    lr=st.floats(min_value=1e-6, max_value=0.5),
    patience=st.integers(min_value=0, max_value=1000),
    score=st.floats(min_value=0.01, max_value=0.99),
    opset=st.integers(min_value=11, max_value=20),
)
def test_rfdetr_valid_hyperparameters_round_trip(size, resolution, epochs, batch, grad_accum,
                                                 lr, patience, score, opset):
    raw = {'rfdetr_size': size, 'epochs': epochs, 'batch': batch, 'grad_accum': grad_accum,
           'lr': lr, 'patience': patience, 'score_threshold': score, 'onnx_opset': opset}
    if resolution is not None:
        raw['resolution'] = resolution
    parsed = dt.parse_detection_hyperparameters(raw, arch='rf_detr')
    for key, value in raw.items():
        assert parsed[key] == value
    assert parsed['resolution'] == (resolution if resolution is not None else dt.RFDETR_SIZES[size])
    assert set(parsed) == set(dt.RFDETR_DEFAULTS)
    # Idempotent: parsing the parsed dict changes nothing.
    assert dt.parse_detection_hyperparameters(parsed, arch='rf_detr') == parsed
    env = dt.detection_job_environment('s3://b/m.manifest', parsed, arch='rf_detr')
    assert env['RESOLUTION'] == str(parsed['resolution'])
    assert all(isinstance(v, str) for v in env.values())
    assert 'IMAGES_S3' not in env
    assert not any(k.startswith('IOU') for k in env)


# ---------------------------------------------------------------------------
# Per-arch job environment (Req 1.1, 3.2)
# ---------------------------------------------------------------------------

def test_rfdetr_job_environment_names():
    env = dt.detection_job_environment(
        's3://b/labeled/output.manifest',
        dt.parse_detection_hyperparameters(None, arch='rf_detr'), arch='rf_detr')
    # Thresholds shape only the device manifest, never the run: no
    # SCORE_THRESHOLD, and (RF-DETR being NMS-free) nothing IOU-related.
    assert env == {
        'MANIFEST_S3': 's3://b/labeled/output.manifest',
        'RFDETR_SIZE': 'small', 'RESOLUTION': '512', 'EPOCHS': '100', 'BATCH': '4',
        'GRAD_ACCUM': '4', 'LR': '0.0001', 'PATIENCE': '10', 'ONNX_OPSET': '17',
    }


def test_rfdetr_job_environment_reflects_size_and_resolution():
    env = dt.detection_job_environment(
        's3://b/m.manifest',
        dt.parse_detection_hyperparameters({'rfdetr_size': 'large'}, arch='rf_detr'),
        arch='rf_detr')
    assert env['RFDETR_SIZE'] == 'large' and env['RESOLUTION'] == '704'
    env = dt.detection_job_environment(
        's3://b/m.manifest',
        dt.parse_detection_hyperparameters(
            {'rfdetr_size': 'large', 'resolution': 640}, arch='rf_detr'),
        arch='rf_detr')
    assert env['RFDETR_SIZE'] == 'large' and env['RESOLUTION'] == '640'


def test_yolo_job_environment_unchanged_with_explicit_arch():
    params = dt.parse_detection_hyperparameters(None)
    assert dt.detection_job_environment('s3://b/m', params, arch='yolo') == \
        dt.detection_job_environment('s3://b/m', params)
    assert dt.detection_job_environment('s3://b/m', params, arch='yolo') == {
        'MANIFEST_S3': 's3://b/m', 'IMGSZ': '1280', 'EPOCHS': '100',
        'BATCH': '4', 'BASE_WEIGHTS': 'yolo11s.pt', 'PATIENCE': '30', 'ONNX_OPSET': '17',
    }


@pytest.mark.parametrize('arch', dt.DETECTION_ARCHES)
def test_job_environment_base_weights_only_when_passed(arch):
    params = dt.parse_detection_hyperparameters(None, arch=arch)
    plain = dt.detection_job_environment('s3://b/m', params, arch=arch)
    assert 'BASE_WEIGHTS_S3' not in plain and 'BASE_WEIGHTS_MEMBER' not in plain

    bare = dt.detection_job_environment(
        's3://b/m', params, arch=arch, base_weights_s3='s3://uc/ckpt/checkpoint.pth')
    assert bare == {**plain, 'BASE_WEIGHTS_S3': 's3://uc/ckpt/checkpoint.pth'}

    member = dt.CHECKPOINT_MEMBER_FOR_ARCH[arch]
    tarball = dt.detection_job_environment(
        's3://b/m', params, arch=arch,
        base_weights_s3='s3://uc/training/j/output/model.tar.gz', base_weights_member=member)
    assert tarball == {**plain, 'BASE_WEIGHTS_S3': 's3://uc/training/j/output/model.tar.gz',
                       'BASE_WEIGHTS_MEMBER': member}
    assert all(isinstance(v, str) for v in tarball.values())

    # A member without a URI is meaningless and is dropped; None / '' URIs add nothing.
    assert dt.detection_job_environment(
        's3://b/m', params, arch=arch, base_weights_member=member) == plain
    assert dt.detection_job_environment(
        's3://b/m', params, arch=arch, base_weights_s3='', base_weights_member=member) == plain


def test_job_environment_unknown_arch_rejected():
    with pytest.raises(ValueError):
        dt.detection_job_environment(
            's3://b/m', dt.parse_detection_hyperparameters(None), arch='detr')


# ---------------------------------------------------------------------------
# Per-arch sourcedir (Req 3.4)
# ---------------------------------------------------------------------------

_CODE_DIR_FILES = {
    'train.py': '# yolo entry point\n',
    'train_rfdetr.py': '# rf-detr entry point\n',
    'requirements.txt': 'ultralytics==8.3.0\n',
    'requirements-rfdetr.txt': 'rfdetr[onnxexport]==1.3.0\nonnxruntime\nnumpy<2\n',
    '_common.py': '# shared helpers\n',
    'manifest_to_detector_dataset.py': '# converter\n',
    'dedupe_frames.py': '# dedupe\n',
}


def _write_code_dir(root, omit=()):
    code = os.path.join(root, 'code')
    os.makedirs(code)
    for name, body in _CODE_DIR_FILES.items():
        if name in omit:
            continue
        with open(os.path.join(code, name), 'w') as fh:
            fh.write(body)
    return code


def _tar_contents(path):
    with tarfile.open(path, 'r:gz') as tar:
        return {m.name: tar.extractfile(m).read().decode() for m in tar.getmembers()}


def test_build_sourcedir_tarball_yolo_entry_point_bundle():
    with tempfile.TemporaryDirectory() as td:
        code = _write_code_dir(td)
        out = os.path.join(td, 'sourcedir.tar.gz')
        names = dt.build_sourcedir_tarball(code, out, entry_point='train.py')
        contents = _tar_contents(out)
        expected = {'train.py', 'requirements.txt', '_common.py',
                    'manifest_to_detector_dataset.py', 'dedupe_frames.py'}
        assert set(contents) == expected
        assert names == sorted(expected)
        assert contents['requirements.txt'] == _CODE_DIR_FILES['requirements.txt']
        assert contents['train.py'] == _CODE_DIR_FILES['train.py']
        assert 'train_rfdetr.py' not in contents and 'requirements-rfdetr.txt' not in contents
        assert all('/' not in n for n in contents)


def test_build_sourcedir_tarball_default_entry_point_is_yolo():
    with tempfile.TemporaryDirectory() as td:
        code = _write_code_dir(td)
        a = os.path.join(td, 'a.tar.gz')
        b = os.path.join(td, 'b.tar.gz')
        assert dt.build_sourcedir_tarball(code, a) == \
            dt.build_sourcedir_tarball(code, b, entry_point='train.py')
        assert _tar_contents(a) == _tar_contents(b)


def test_build_sourcedir_tarball_rfdetr_entry_point_bundle():
    with tempfile.TemporaryDirectory() as td:
        code = _write_code_dir(td)
        out = os.path.join(td, 'sourcedir.tar.gz')
        names = dt.build_sourcedir_tarball(code, out, entry_point='train_rfdetr.py')
        contents = _tar_contents(out)
        expected = {'train_rfdetr.py', 'requirements.txt', '_common.py',
                    'manifest_to_detector_dataset.py', 'dedupe_frames.py'}
        assert set(contents) == expected
        assert names == sorted(expected)
        # The SageMaker toolkit installs exactly `requirements.txt`, so the
        # RF-DETR pins travel under that name — and the YOLO pins do not travel.
        assert contents['requirements.txt'] == _CODE_DIR_FILES['requirements-rfdetr.txt']
        assert contents['train_rfdetr.py'] == _CODE_DIR_FILES['train_rfdetr.py']
        assert 'train.py' not in contents and 'requirements-rfdetr.txt' not in contents
        assert all('/' not in n for n in contents)


@pytest.mark.parametrize('entry_point', sorted(dt.ENTRY_POINT_FOR_ARCH.values()))
def test_build_sourcedir_tarball_tolerates_missing_common(entry_point):
    with tempfile.TemporaryDirectory() as td:
        code = _write_code_dir(td, omit=('_common.py',))
        out = os.path.join(td, 'sourcedir.tar.gz')
        names = dt.build_sourcedir_tarball(code, out, entry_point=entry_point)
        assert '_common.py' not in names
        assert set(names) == {entry_point, 'requirements.txt',
                              'manifest_to_detector_dataset.py', 'dedupe_frames.py'}


def test_build_sourcedir_tarball_unknown_entry_point():
    with tempfile.TemporaryDirectory() as td:
        code = _write_code_dir(td)
        with pytest.raises(ValueError) as exc:
            dt.build_sourcedir_tarball(code, os.path.join(td, 'o.tar.gz'), entry_point='train_detr.py')
        assert 'train_detr.py' in str(exc.value)
        assert not os.path.exists(os.path.join(td, 'o.tar.gz'))


@pytest.mark.parametrize('entry_point,missing', [
    ('train_rfdetr.py', 'train_rfdetr.py'),
    ('train_rfdetr.py', 'requirements-rfdetr.txt'),
    ('train.py', 'requirements.txt'),
    ('train_rfdetr.py', 'dedupe_frames.py'),
])
def test_build_sourcedir_tarball_missing_required_file(entry_point, missing):
    with tempfile.TemporaryDirectory() as td:
        code = _write_code_dir(td, omit=(missing,))
        with pytest.raises(FileNotFoundError) as exc:
            dt.build_sourcedir_tarball(code, os.path.join(td, 'o.tar.gz'), entry_point=entry_point)
        assert missing in str(exc.value)


# ---------------------------------------------------------------------------
# RF-DETR device manifest (Req 4.1)
# ---------------------------------------------------------------------------

def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


def test_rfdetr_device_manifest_shape():
    m = dt.build_detection_device_manifest(
        image_width=512, image_height=512, num_classes=2, class_names=['plate', 'luggage'],
        score_threshold=0.5, detection_arch='rf_detr')
    assert m['runtime'] == 'onnx' and m['runtime_artifact'] == 'model.onnx'
    assert m['task'] == 'object_detection'
    assert m['model_graph']['model_graph_type'] == 'single_stage_model_graph'
    stage = m['model_graph']['stages'][0]
    assert stage['type'] == 'rf_detr_object_detection'
    assert stage['input_shape'] == [1, 3, 512, 512] and m['input_shape'] == [1, 3, 512, 512]
    # ImageNet mean/std AFTER 0..1 scaling — the opposite of YOLO's normalize=False.
    assert stage['image_range_scale'] is True and stage['normalize'] is True
    assert stage['threshold'] == 0.5 and stage['num_classes'] == 2
    assert m['preprocessing'] == {'resize': [512, 512], 'channel_order': 'RGB'}
    assert m['dataset'] == {'image_width': 512, 'image_height': 512}
    assert m['detection'] == {
        'layout': 'rf_detr', 'num_classes': 2, 'score_threshold': 0.5, 'network_input': 512,
        'preserve_aspect': False, 'top_k': 300, 'class_names': ['plate', 'luggage'],
    }
    assert 'iou_threshold' not in set(_walk_keys(m))


def test_rfdetr_device_manifest_preserve_aspect_defaults_false_and_is_overridable():
    kwargs = dict(image_width=576, image_height=576, num_classes=1, class_names=None,
                  score_threshold=0.4, detection_arch='rf_detr')
    default = dt.build_detection_device_manifest(**kwargs)
    on = dt.build_detection_device_manifest(preserve_aspect=True, **kwargs)
    assert default['detection']['preserve_aspect'] is False
    assert on['detection']['preserve_aspect'] is True
    assert 'class_names' not in default['detection']


def test_rfdetr_device_manifest_top_k_overridable():
    m = dt.build_detection_device_manifest(
        image_width=384, image_height=384, num_classes=3, class_names=['a', 'b', 'c'],
        score_threshold=0.5, detection_arch='rf_detr', top_k=100)
    assert m['detection']['top_k'] == 100


def test_rfdetr_device_manifest_ignores_iou_threshold():
    """NMS is YOLO-only: an iou_threshold handed to the rf_detr branch never
    reaches the manifest (packaging may pass a record's value through)."""
    with_iou = dt.build_detection_device_manifest(
        image_width=512, image_height=512, num_classes=1, class_names=['x'],
        score_threshold=0.5, iou_threshold=0.45, detection_arch='rf_detr')
    without = dt.build_detection_device_manifest(
        image_width=512, image_height=512, num_classes=1, class_names=['x'],
        score_threshold=0.5, detection_arch='rf_detr')
    assert with_iou == without
    assert 'iou_threshold' not in set(_walk_keys(with_iou))


def test_yolo_device_manifest_requires_iou_threshold():
    with pytest.raises(ValueError) as exc:
        dt.build_detection_device_manifest(
            image_width=640, image_height=640, num_classes=1, class_names=['x'],
            score_threshold=0.25)
    assert 'iou_threshold' in str(exc.value)


def test_yolo_device_manifest_unchanged_with_explicit_arch():
    kwargs = dict(image_width=1280, image_height=1280, num_classes=1, class_names=['blue_plate'],
                  score_threshold=0.25, iou_threshold=0.45)
    explicit = dt.build_detection_device_manifest(detection_arch='yolo', **kwargs)
    assert explicit == dt.build_detection_device_manifest(**kwargs)
    assert explicit['model_graph']['stages'][0]['type'] == 'yolo_object_detection'
    assert explicit['model_graph']['stages'][0]['normalize'] is False
    assert explicit['detection']['iou_threshold'] == 0.45
    assert 'top_k' not in explicit['detection']


def test_device_manifest_unknown_arch_rejected():
    with pytest.raises(ValueError):
        dt.build_detection_device_manifest(
            image_width=640, image_height=640, num_classes=1, class_names=['x'],
            score_threshold=0.25, iou_threshold=0.45, detection_arch='detr')


@given(
    k=st.integers(min_value=7, max_value=35),
    n=st.integers(min_value=1, max_value=6),
    score=st.floats(min_value=0.01, max_value=0.99),
)
def test_rfdetr_device_manifest_property(k, n, score):
    names = [f"c{i}" for i in range(n)]
    m = dt.build_detection_device_manifest(
        image_width=k * 32, image_height=k * 32, num_classes=n, class_names=names,
        score_threshold=score, detection_arch='rf_detr')
    stage = m['model_graph']['stages'][0]
    assert stage['type'] == 'rf_detr_object_detection' and stage['normalize'] is True
    assert m['detection']['layout'] == 'rf_detr'
    assert m['detection']['top_k'] == 300
    assert m['detection']['preserve_aspect'] is False
    assert m['detection']['network_input'] == k * 32
    assert m['detection']['class_names'] == names
    assert 'iou_threshold' not in set(_walk_keys(m))


# ---------------------------------------------------------------------------
# Base model resolution (Req 6.3, 6.4, 6.5, 7)
# ---------------------------------------------------------------------------

UC = 'uc-1'
OTHER_UC = 'uc-2'


class _StubTable:
    """The one DynamoDB call resolve_base_model makes: get_item(Key={'training_id'})."""

    def __init__(self, items):
        self.items = {item['training_id']: item for item in items}
        self.calls = []

    def get_item(self, Key):
        self.calls.append(Key)
        item = self.items.get(Key['training_id'])
        return {'Item': item} if item is not None else {}


def _yolo_job(training_id='tj-yolo', status='Completed', usecase_id=UC, **overrides):
    record = {
        'training_id': training_id,
        'usecase_id': usecase_id,
        'status': status,
        'model_type': 'object_detection',
        'runtime': 'onnx',
        'model_name': 'blue-plate',
        'model_version': 2,
        'artifact_s3': f's3://{usecase_id}-bucket/models/training/{training_id}/output/model.tar.gz',
        'detection': {
            'detection_arch': 'yolo',
            'class_names': ['blue_plate'],
            'network_input_width': 1280,
        },
    }
    record.update(overrides)
    return record


def _rfdetr_job(training_id='tj-rfdetr', **overrides):
    record = _yolo_job(training_id=training_id, model_name='plates-detr', model_version=1)
    record['detection'] = {
        'detection_arch': 'rf_detr',
        'class_names': ['plate', 'luggage'],
        'network_input_width': 512,
    }
    record.update(overrides)
    return record


def _imported(training_id='imp-1', fine_tunable='default', **overrides):
    if fine_tunable == 'default':
        fine_tunable = {
            'arch': 'yolo', 'kind': 'ultralytics_checkpoint',
            'checkpoint_s3': f's3://{UC}-bucket/converted-models/imp/checkpoint.pt',
            'class_names': ['bolt'],
        }
    record = {
        'training_id': training_id,
        'usecase_id': UC,
        'status': 'Completed',
        'source': 'imported',
        'model_type': 'object_detection',
        'runtime': 'onnx',
        'model_name': 'yolo-world-blue-plate',
        'artifact_s3': f's3://{UC}-bucket/converted-models/imp/package.tar.gz',
        'metadata': {'fine_tunable': fine_tunable},
    }
    record.update(overrides)
    return record


def _resolve(kind, ref, arch, items, usecase_id=UC):
    table = _StubTable(items)
    return dt.resolve_base_model(kind, ref, arch, usecase_id, table), table


@pytest.mark.parametrize('arch', dt.DETECTION_ARCHES)
def test_resolve_base_model_published(arch):
    desc, table = _resolve('published', 'yolo11s.pt', arch, [_yolo_job()])
    assert desc == {
        'kind': 'published', 'ref': 'yolo11s.pt', 'weights_s3': None, 'member': None,
        'detection_arch': arch, 'class_names': None,
    }
    assert table.calls == []       # never touches the table


def test_resolve_base_model_defaults_to_published():
    desc, table = _resolve(None, None, 'yolo', [])
    assert desc['kind'] == 'published' and desc['weights_s3'] is None and desc['ref'] is None
    desc, _ = _resolve('', '  ', 'rf_detr', [])
    assert desc['kind'] == 'published' and desc['ref'] is None
    assert table.calls == []


def test_resolve_base_model_bad_kind():
    with pytest.raises(ValueError) as exc:
        _resolve('checkpoint', 'x', 'yolo', [])
    assert str(exc.value) == \
        "Invalid base_model.kind 'checkpoint': expected one of published, training_job, imported"


@pytest.mark.parametrize('kind', ['training_job', 'imported'])
@pytest.mark.parametrize('ref', [None, '', '   '])
def test_resolve_base_model_requires_ref(kind, ref):
    with pytest.raises(ValueError) as exc:
        _resolve(kind, ref, 'yolo', [_yolo_job()])
    assert str(exc.value) == f"base_model.ref is required when base_model.kind is '{kind}'"


def test_resolve_base_model_completed_yolo_job():
    job = _yolo_job()
    desc, table = _resolve('training_job', 'tj-yolo', 'yolo', [job])
    assert desc == {
        'kind': 'training_job', 'ref': 'tj-yolo',
        'weights_s3': job['artifact_s3'],      # the tarball; the entry point extracts...
        'member': 'best.pt',                   # ...this member from it
        'detection_arch': 'yolo',
        'class_names': ['blue_plate'],
    }
    assert table.calls == [{'training_id': 'tj-yolo'}]


def test_resolve_base_model_completed_rfdetr_job():
    job = _rfdetr_job()
    desc, _ = _resolve('training_job', 'tj-rfdetr', 'rf_detr', [job])
    assert desc['weights_s3'] == job['artifact_s3']
    assert desc['member'] == 'checkpoint_best_total.pth'
    assert desc['detection_arch'] == 'rf_detr'
    assert desc['class_names'] == ['plate', 'luggage']


def test_resolve_base_model_legacy_record_without_arch_is_yolo():
    """Records written before this spec carry no detection_arch: they are YOLO."""
    job = _yolo_job()
    del job['detection']['detection_arch']
    desc, _ = _resolve('training_job', 'tj-yolo', 'yolo', [job])
    assert desc['member'] == 'best.pt' and desc['weights_s3'] == job['artifact_s3']
    with pytest.raises(ValueError) as exc:
        _resolve('training_job', 'tj-yolo', 'rf_detr', [job])
    assert str(exc.value) == \
        "Base model blue-plate v2 is a yolo detector; cannot start an rf_detr run from it"


def test_resolve_base_model_strips_ref():
    desc, table = _resolve('training_job', '  tj-yolo ', 'yolo', [_yolo_job()])
    assert desc['ref'] == 'tj-yolo'


@pytest.mark.parametrize('kind', ['training_job', 'imported'])
def test_resolve_base_model_missing_and_other_use_case_share_one_message(kind):
    """No cross-tenant existence leak: another use case's job reads as 'not found'."""
    items = [_yolo_job(usecase_id=OTHER_UC), _imported(usecase_id=OTHER_UC)]
    ref = 'tj-yolo' if kind == 'training_job' else 'imp-1'
    with pytest.raises(ValueError) as other:
        _resolve(kind, ref, 'yolo', items)
    with pytest.raises(ValueError) as missing:
        _resolve(kind, 'tj-nope', 'yolo', items)
    assert str(other.value) == \
        f"Base model training job '{ref}' was not found in this use case"
    assert str(missing.value) == \
        "Base model training job 'tj-nope' was not found in this use case"


@pytest.mark.parametrize('status', ['InProgress', 'Failed', 'Stopped', ''])
def test_resolve_base_model_requires_completed(status):
    with pytest.raises(ValueError) as exc:
        _resolve('training_job', 'tj-yolo', 'yolo', [_yolo_job(status=status)])
    shown = status or 'unknown'
    assert str(exc.value) == (
        f"Base model blue-plate v2 is not Completed (status: {shown}); "
        "only a completed training job can be fine-tuned from")


def test_resolve_base_model_arch_mismatch_both_directions():
    with pytest.raises(ValueError) as exc:
        _resolve('training_job', 'tj-yolo', 'rf_detr', [_yolo_job()])
    assert str(exc.value) == \
        "Base model blue-plate v2 is a yolo detector; cannot start an rf_detr run from it"
    with pytest.raises(ValueError) as exc:
        _resolve('training_job', 'tj-rfdetr', 'yolo', [_rfdetr_job()])
    assert str(exc.value) == \
        "Base model plates-detr v1 is a rf_detr detector; cannot start a yolo run from it"


@pytest.mark.parametrize('record', [
    _yolo_job(model_type='classification', runtime='onnx'),        # LFV classifier
    _yolo_job(runtime=None),                                        # TorchScript detector
    _yolo_job(source='imported'),                                   # imported, not trained
])
def test_resolve_base_model_training_job_must_be_trained_detector(record):
    record = {k: v for k, v in record.items() if v is not None}
    with pytest.raises(ValueError) as exc:
        _resolve('training_job', 'tj-yolo', 'yolo', [record])
    assert str(exc.value) == "Base model blue-plate v2 is not a portal-trained object detector"


@pytest.mark.parametrize('artifact', [None, '', '   '])
def test_resolve_base_model_requires_artifact_naming_member(artifact):
    job = _yolo_job(artifact_s3=artifact)
    if artifact is None:
        del job['artifact_s3']
    with pytest.raises(ValueError) as exc:
        _resolve('training_job', 'tj-yolo', 'yolo', [job])
    assert str(exc.value) == \
        "Base model blue-plate v2 has no training artifact containing best.pt"
    job = _rfdetr_job(artifact_s3='')
    with pytest.raises(ValueError) as exc:
        _resolve('training_job', 'tj-rfdetr', 'rf_detr', [job])
    assert str(exc.value) == \
        "Base model plates-detr v1 has no training artifact containing checkpoint_best_total.pth"


def test_resolve_base_model_display_name_falls_back_to_ref():
    job = _yolo_job(status='InProgress')
    del job['model_name']
    del job['model_version']
    with pytest.raises(ValueError) as exc:
        _resolve('training_job', 'tj-yolo', 'yolo', [job])
    assert str(exc.value).startswith("Base model tj-yolo is not Completed")
    job = _yolo_job(status='InProgress')
    del job['model_version']
    with pytest.raises(ValueError) as exc:
        _resolve('training_job', 'tj-yolo', 'yolo', [job])
    assert str(exc.value).startswith("Base model blue-plate is not Completed")


def test_resolve_base_model_imported_with_fine_tunable_checkpoint():
    rec = _imported()
    desc, _ = _resolve('imported', 'imp-1', 'yolo', [rec])
    assert desc == {
        'kind': 'imported', 'ref': 'imp-1',
        'weights_s3': rec['metadata']['fine_tunable']['checkpoint_s3'],   # a bare file
        'member': None,
        'detection_arch': 'yolo',
        'class_names': ['bolt'],
    }


def test_resolve_base_model_imported_rfdetr_checkpoint():
    rec = _imported(fine_tunable={
        'arch': 'rf_detr', 'kind': 'rfdetr_checkpoint',
        'checkpoint_s3': 's3://uc-1-bucket/converted-models/imp/checkpoint.pth'})
    rec['metadata']['class_names'] = ['plate']
    desc, _ = _resolve('imported', 'imp-1', 'rf_detr', [rec])
    assert desc['weights_s3'] == 's3://uc-1-bucket/converted-models/imp/checkpoint.pth'
    assert desc['member'] is None and desc['detection_arch'] == 'rf_detr'
    # class_names fall back to metadata.class_names when fine_tunable has none.
    assert desc['class_names'] == ['plate']


@pytest.mark.parametrize('fine_tunable', [
    None,                                   # ONNX-only / TorchScript import
    {},                                     # marker without a checkpoint
    {'arch': 'yolo', 'kind': 'onnx'},       # classified but nothing to load
    {'arch': 'yolo', 'checkpoint_s3': ''},
])
def test_resolve_base_model_imported_without_checkpoint(fine_tunable):
    with pytest.raises(ValueError) as exc:
        _resolve('imported', 'imp-1', 'yolo', [_imported(fine_tunable=fine_tunable)])
    assert str(exc.value) == \
        "Imported model yolo-world-blue-plate has no fine-tunable checkpoint (ONNX/TorchScript)"


def test_resolve_base_model_imported_without_metadata_block():
    rec = _imported()
    del rec['metadata']
    with pytest.raises(ValueError) as exc:
        _resolve('imported', 'imp-1', 'yolo', [rec])
    assert 'has no fine-tunable checkpoint' in str(exc.value)


def test_resolve_base_model_imported_arch_mismatch():
    with pytest.raises(ValueError) as exc:
        _resolve('imported', 'imp-1', 'rf_detr', [_imported()])
    assert str(exc.value) == \
        "Base model yolo-world-blue-plate is a yolo model; cannot start an rf_detr run from it"
    rec = _imported(fine_tunable={'checkpoint_s3': 's3://uc-1-bucket/x.pt'})
    with pytest.raises(ValueError) as exc:
        _resolve('imported', 'imp-1', 'yolo', [rec])
    assert str(exc.value) == (
        "Base model yolo-world-blue-plate is a non-detection model; "
        "cannot start a yolo run from it")


def test_resolve_base_model_imported_kind_requires_imported_record():
    with pytest.raises(ValueError) as exc:
        _resolve('imported', 'tj-yolo', 'yolo', [_yolo_job()])
    assert str(exc.value) == "Base model blue-plate v2 is not an imported model"


def test_resolve_base_model_unknown_arch_rejected_before_lookup():
    table = _StubTable([_yolo_job()])
    with pytest.raises(ValueError):
        dt.resolve_base_model('training_job', 'tj-yolo', 'detr', UC, table)
    assert table.calls == []
