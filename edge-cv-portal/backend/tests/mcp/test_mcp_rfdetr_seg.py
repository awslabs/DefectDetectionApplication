"""RF-DETR-Seg on the Portal MCP catalog (`mcp_portal/catalog.py`).
Spec: .kiro/specs/rfdetr-seg-support/ (Requirements 13.1, 13.2, 13.4, 15.2,
15.3; design §13; tasks.md 13.3; C8-C10).
The SDK's in-process `Client` against `build_server('models')` /
`build_server('data')` with the backing Lambdas faked by
`_portal_harness.FakeLambda`, as the protocol suite does:
* `portal_models_training_create` with `model_type = instance_segmentation`,
  `detection_arch = rf_detr_seg`, top-level `output_mode` and
  `hyperparameters.{rfdetr_seg_size, mask_threshold}` reaches a stubbed
  `training.handler` with every key in place (criterion-1 placement: the size
  and the mask threshold travel inside the opaque `hyperparameters` object).
* `portal_models_convert` with the identifiers plus top-level
  `mask_threshold` / `output_mode` reaches a stubbed `model_converter.handler`.
* A Seg convert call carrying `iou_threshold` receives the handler's 400 as a
  `ToolFailure`, and with the Seg_Switch OFF the handler's 403 reaches the
  caller as a `ToolFailure` with the disabled reason: both answered by the
  real `model_converter.handler` behind the fake Lambda (its request gates run
  before any AWS call), so the dispatcher is proven to forward the Lambda's
  own refusal - it reads no switch of its own.
* The schema mirror check: every new property is a key its route reads from
  the body (`_handler_ast`, the rule of `test_mcp_catalog_arguments.py`),
  the size is on neither schema, and no tool was added.
* `portal_data_captures_list` on an instance capture fixture returns the
  light page (`has_masks`, per-entry `mask_area`, `detections_truncated`,
  `jsonl_key`) and never `mask_rle` - the real `captures.handler` behind the
  fake Lambda, its S3 client faked.
The edge assertions (`edge_results_list`, `edge_result_get`,
`edge_execution_overlay_get`, `edge_workflow_images_list`, `edge_workflow_run`)
live in the device suite of task 10.7.
"""
import base64
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

import _handler_ast as ast_h
import _portal_harness as h
from mcp_portal import DELIVERED_PHASES, catalog
from mcp_portal.catalog import CATALOG

BY_NAME = {s.name: s for s in CATALOG}
TRAINING_CREATE = BY_NAME['portal_models_training_create']
CONVERT = BY_NAME['portal_models_convert']
CAPTURES_LIST = BY_NAME['portal_data_captures_list']
USECASE = 'uc-fake-1'
MANIFEST = 's3://example-bucket/datasets/instances/manifest.jsonl'
CHECKPOINT = 's3://example-bucket/models/rf-detr-seg-small.pth'
MCP_PORTAL_DIR = pathlib.Path(catalog.__file__).resolve().parent


@pytest.fixture
def portal(aws_stack, monkeypatch):
    """Every phase delivered and the mutating switch on (both tools are M)."""
    monkeypatch.delenv('MCP_SERVER_VERSION', raising=False)
    monkeypatch.delenv('PORTAL_REGISTRY_ENFORCED', raising=False)
    monkeypatch.delenv('PORTAL_RFDETR_SEG_ENABLED', raising=False)
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(shared.rbac_manager, 'has_permission', lambda *_a, **_k: True)
    fake = h.install(monkeypatch, phases=h.ALL_PHASES, mutating=True)
    fake.monkeypatch = monkeypatch
    return fake


@pytest.fixture
def model_converter(aws_stack):
    """The real converter handler, imported inside the moto mock."""
    sys.modules.pop('model_converter', None)
    import model_converter as module
    return module


def call(view, name, args):
    return h.in_memory(view, lambda client: client.call_tool(name, args), h.access_token())


def failure_of(result):
    assert result.is_error is True, result.structured_content
    content = result.structured_content
    assert set(content) >= {'category', 'message', 'request_id', 'status'}
    assert result.content[0].text == f"{content['category']}: {content['message']}"
    return content


def proxied(handler):
    """A `FakeLambda.answer` running a real Lambda handler on the dispatched event."""
    def answer(_arn, event):
        response = handler(event, None)
        return response['statusCode'], json.loads(response['body'])
    return answer


# ------------------------------------------------------- training create

SEG_TRAINING_ARGS = {
    'usecase_id': USECASE, 'model_name': 'seg-model', 'model_version': '1',
    'model_type': 'instance_segmentation', 'detection_arch': 'rf_detr_seg',
    'dataset_manifest_s3': MANIFEST, 'output_mode': 'semantic',
    'hyperparameters': {'rfdetr_seg_size': 'small', 'mask_threshold': 0.4,
                        'epochs': 5},
    'class_names': ['scratch', 'dent'],
}


def test_training_create_reaches_the_handler_with_the_seg_keys_in_place(portal):
    seen = {}

    def training_handler(_arn, event):
        seen['event'] = event
        return 201, {'job_id': 'fake-training-1', 'status': 'Pending'}

    portal.answer = training_handler
    result = call('models', 'portal_models_training_create', SEG_TRAINING_ARGS)
    assert result.is_error is False, result.structured_content
    [(arn, event)] = portal.calls
    assert arn == h.backend_arn('training')
    assert event['httpMethod'] == 'POST' and event['path'] == '/training'
    body = json.loads(event['body'])
    assert body['model_type'] == 'instance_segmentation'
    assert body['detection_arch'] == 'rf_detr_seg'
    assert body['output_mode'] == 'semantic'                    # top level (Req 5.5)
    assert body['hyperparameters'] == {'rfdetr_seg_size': 'small', 'mask_threshold': 0.4,
                                       'epochs': 5}
    assert 'rfdetr_seg_size' not in body and 'mask_threshold' not in body
    assert body['usecase_id'] == USECASE
    assert body['dataset_manifest_s3'] == MANIFEST
    assert result.structured_content['result'] == {'job_id': 'fake-training-1',
                                                   'status': 'Pending'}


@pytest.mark.parametrize('field, value', [
    ('model_type', 'instance-segmentation'),
    ('detection_arch', 'rf_detr_segmentation'),
    ('output_mode', 'panoptic'),
])
def test_training_create_refuses_values_outside_the_enums_before_dispatch(portal, field, value):
    content = failure_of(call('models', 'portal_models_training_create',
                              {**SEG_TRAINING_ARGS, field: value}))
    assert content['category'] == 'invalid_input' and field in content['message']
    assert portal.calls == []


# ---------------------------------------------------------------- convert

SEG_CONVERT_ARGS = {
    'usecase_id': USECASE, 'model_s3_uri': CHECKPOINT, 'model_name': 'seg-import',
    'model_type': 'instance_segmentation', 'detection_arch': 'rf_detr_seg',
    'image_width': 384, 'image_height': 384, 'export_format': 'onnx',
    'class_names': ['scratch', 'dent'],
    'score_threshold': 0.5, 'mask_threshold': 0.6, 'output_mode': 'instance',
}


def test_convert_reaches_the_handler_with_the_seg_fields_at_the_top_level(portal):
    portal.answer = lambda _arn, _event: (200, {'conversion_id': 'fake-conversion-1'})
    result = call('models', 'portal_models_convert', SEG_CONVERT_ARGS)
    assert result.is_error is False, result.structured_content
    [(arn, event)] = portal.calls
    assert arn == h.backend_arn('modelConverter')
    assert event['httpMethod'] == 'POST' and event['path'] == '/models/convert'
    body = json.loads(event['body'])
    assert body['model_type'] == 'instance_segmentation'
    assert body['detection_arch'] == 'rf_detr_seg'
    assert body['mask_threshold'] == 0.6 and body['output_mode'] == 'instance'
    assert body['score_threshold'] == 0.5
    assert 'iou_threshold' not in body
    assert 'rfdetr_seg_size' not in body            # the size is inferred from the checkpoint
    assert body['usecase_id'] == USECASE


def test_convert_with_iou_threshold_receives_the_handlers_400(portal, model_converter):
    portal.answer = proxied(model_converter.handler)
    content = failure_of(call('models', 'portal_models_convert',
                              {**SEG_CONVERT_ARGS, 'iou_threshold': 0.5}))
    assert content['category'] == 'invalid_input' and content['status'] == 400
    assert 'iou_threshold' in content['message'] and 'RF-DETR-Seg' in content['message']
    assert len(portal.calls) == 1                    # refused by the handler, not the server


def test_switch_off_403_reaches_the_caller_with_the_disabled_reason(portal, model_converter):
    portal.monkeypatch.setenv('PORTAL_RFDETR_SEG_ENABLED', 'false')
    portal.answer = proxied(model_converter.handler)
    content = failure_of(call('models', 'portal_models_convert', SEG_CONVERT_ARGS))
    assert content['category'] == 'not_authorized' and content['status'] == 403
    assert content['message'] == model_converter.SEG_DISABLED_REASON
    assert len(portal.calls) == 1                    # the Lambda was invoked: no dispatcher read


def test_switch_on_convert_passes_the_handlers_seg_gates(portal, model_converter):
    # With the switch on (absent) the real handler accepts the Seg request
    # past its gates; the first thing after them is the membership check,
    # answered 403 'Insufficient permissions' by the empty moto registry -
    # a different text from the disabled reason, so the OFF case above is
    # proven to be the switch and not the access check.
    portal.answer = proxied(model_converter.handler)
    content = failure_of(call('models', 'portal_models_convert', SEG_CONVERT_ARGS))
    assert content['status'] == 403
    assert content['message'] == 'Insufficient permissions'


def test_the_dispatcher_reads_no_seg_switch():
    for path in sorted(MCP_PORTAL_DIR.glob('*.py')):
        assert 'PORTAL_RFDETR_SEG_ENABLED' not in path.read_text(encoding='utf-8'), path.name


# ---------------------------------------------------------- schema mirror

def route_body_keys(spec) -> set:
    uni = ast_h.universe()
    start = uni.find_function(spec.handler_module, spec.handler_function)
    assert start is not None, (spec.handler_module, spec.handler_function)
    reads, _required, _groups, _graph = ast_h.ReadCollector(uni).collect(start)
    return {r.key for r in reads if r.kind in (ast_h.QUERY, ast_h.BODY)}


def test_convert_schema_mirrors_the_handler():
    props = CONVERT.input_schema['properties']
    assert props['model_type']['enum'] == ['classification', 'object_detection',
                                           'segmentation', 'anomaly_detection',
                                           'instance_segmentation']
    assert props['detection_arch']['enum'] == ['yolo', 'rf_detr', 'rf_detr_seg']
    assert props['mask_threshold'] == {'type': 'number', 'minimum': 0, 'maximum': 1}
    assert props['output_mode']['enum'] == ['instance', 'semantic']
    assert 'rfdetr_seg_size' not in props
    reads = route_body_keys(CONVERT)
    assert {'mask_threshold', 'output_mode', 'detection_arch', 'model_type',
            'iou_threshold', 'score_threshold'} <= reads


def test_training_create_schema_mirrors_the_handler():
    props = TRAINING_CREATE.input_schema['properties']
    assert props['model_type']['enum'] == ['classification', 'segmentation',
                                           'classification-robust', 'segmentation-robust',
                                           'object_detection', 'instance_segmentation']
    assert props['detection_arch']['enum'] == ['yolo', 'rf_detr', 'rf_detr_seg']
    assert props['output_mode']['enum'] == ['instance', 'semantic']
    assert props['hyperparameters']['type'] == 'object'
    # The size and the mask threshold are hyperparameters the arch parser
    # validates, not body keys the route reads: rule 1 of argument_problems.
    assert 'rfdetr_seg_size' not in props and 'mask_threshold' not in props
    reads = route_body_keys(TRAINING_CREATE)
    assert {'output_mode', 'detection_arch', 'model_type', 'hyperparameters'} <= reads
    assert 'rfdetr_seg_size' not in reads and 'mask_threshold' not in reads


def test_no_tool_added_and_the_types_tool_is_untouched():
    # The three pins of Req 13.2, as their own suites compute them
    # (test_mcp_protocol.py:108, test_mcp_catalog_classification.py:136,
    # test_mcp_guardrails.py:63), which pass unmodified.
    assert len({s.name for s in CATALOG if s.phase in DELIVERED_PHASES and s.read_only}) == 89
    assert len([s for s in CATALOG if s.read_only and s.backend is not None]) == 88
    assert len([s for s in CATALOG if s.destructive]) == 31
    types_get = BY_NAME['portal_models_types_get']
    assert types_get.input_schema.get('properties', {}) == {}
    assert types_get.permissions == ('view_models',)


# ------------------------------------------------------------ captures

RLE_A = '0 5 2 2 2 5'
RLE_B = '5 2 2 2 5'
PREFIX = 'dev'
DEVICE = 'thing-1'


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode('utf-8')).decode('utf-8')


def instance_jsonl(entries, detections_truncated=False) -> bytes:
    """One Capture_Metadata line in the Marshal shape: the summary and the
    detections block as base64 aux outputs."""
    summary = {'Inference result': 'Detection', 'Detection_count': len(entries)}
    block = {'detections': {str(i): e for i, e in enumerate(entries)}}
    if detections_truncated:
        block['detections_truncated'] = True
    metadata = {'deviceFleetAuxiliaryOutputs': [
        {'observedContentType': 'json', 'data': _b64(summary)},
        {'observedContentType': 'json_with_base64_encoding', 'data': _b64(block)},
    ]}
    return (json.dumps(metadata) + '\n').encode('utf-8')


def instance_entry(index, label, box, conf, rle, area):
    return {'class_index': str(index), 'class_label': label, 'bounding_box': box,
            'confidence': conf, 'mask_rle': rle, 'mask_area': area}


class FakeS3:
    """The subset of the S3 client `captures.py` uses, over `{key: bytes}`."""
    def __init__(self, objects):
        self._objects = dict(objects)

    def get_paginator(self, name):
        assert name == 'list_objects_v2'
        keys = sorted(self._objects)
        return SimpleNamespace(paginate=lambda Bucket=None, Prefix=None, **_: iter(
            [{'Contents': [{'Key': k} for k in keys if Prefix is None or k.startswith(Prefix)]}]))

    def get_object(self, Bucket=None, Key=None, **_):
        return {'Body': SimpleNamespace(read=lambda: self._objects[Key])}

    def generate_presigned_url(self, _op, Params=None, ExpiresIn=None):
        return f"https://signed.example/{Params['Key']}"


@pytest.fixture
def captures_mod(aws_stack, monkeypatch):
    """The real captures module inside the moto mock, its cross-account
    plumbing and membership check stubbed (the gate itself is covered by
    tests/test_captures_authz.py), its S3 client the fake above."""
    sys.modules.pop('captures', None)
    import captures as module
    base = f'{PREFIX}/{DEVICE}/cap-a'
    objects = {
        f'{base}.jsonl': instance_jsonl([
            instance_entry(0, 'scratch', [1, 1, 3, 3], 0.91, RLE_A, 12),
            instance_entry(1, 'dent', [2, 2, 4, 4], 0.64, RLE_B, 4)]),
        f'{base}.jpg': b'src', f'{base}.overlay.jpg': b'ovl', f'{base}.mask.png': b'png',
    }
    monkeypatch.setattr(module, 'check_user_access', lambda *_a, **_k: True)
    monkeypatch.setattr(module, 'get_usecase', lambda usecase_id: {
        'usecase_id': usecase_id, 'account_id': '123456789012',
        'cross_account_role_arn': 'fake-usecase-role', 'external_id': 'x'})
    monkeypatch.setattr(module, 'assume_usecase_role', lambda *_a, **_k: {
        'AccessKeyId': 'k', 'SecretAccessKey': 's', 'SessionToken': 't'})
    monkeypatch.setattr(module, 'boto3', SimpleNamespace(client=lambda *a, **k: FakeS3(objects)))
    return module


def test_captures_list_returns_the_light_page_without_mask_rle(portal, captures_mod):
    portal.answer = proxied(captures_mod.handler)
    result = call('data', 'portal_data_captures_list',
                  {'usecase_id': USECASE, 'prefix': PREFIX, 'device_id': DEVICE})
    assert result.is_error is False, result.structured_content
    [(arn, event)] = portal.calls
    assert arn == h.backend_arn('captures')
    assert event['httpMethod'] == 'GET' and event['resource'] == '/captures'
    assert event['queryStringParameters'] == {'usecase_id': USECASE, 'prefix': PREFIX,
                                              'device_id': DEVICE}
    page = result.structured_content['result']
    assert page['total_found'] == 1
    [cap] = page['captures']
    assert cap['has_masks'] is True
    assert cap['detections_truncated'] is False
    assert cap['jsonl_key'] == f'{PREFIX}/{DEVICE}/cap-a.jsonl'
    assert cap['detection_count'] == 2
    assert [d['mask_area'] for d in cap['detections']] == [12, 4]
    for det in cap['detections']:
        assert set(det) == {'class_index', 'class_label', 'bounding_box', 'confidence',
                            'mask_area'}
    assert 'mask_rle' not in json.dumps(page)
    assert cap['mask_url'] == f'https://signed.example/{PREFIX}/{DEVICE}/cap-a.mask.png'


def test_captures_list_has_no_include_masks_argument(portal, captures_mod):
    # The per-record RLE is the detail request's (`key` + `include_masks`,
    # task 11.1), which the MCP tool does not offer: the page is always light.
    assert 'include_masks' not in CAPTURES_LIST.input_schema['properties']
    assert 'key' not in CAPTURES_LIST.input_schema['properties']
    content = failure_of(call('data', 'portal_data_captures_list',
                              {'usecase_id': USECASE, 'prefix': PREFIX, 'include_masks': True}))
    assert content['category'] == 'invalid_input'
    assert content['message'] == 'include_masks is not an argument of portal_data_captures_list'
    assert portal.calls == []
