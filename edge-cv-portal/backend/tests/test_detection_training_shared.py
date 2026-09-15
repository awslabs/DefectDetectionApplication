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
