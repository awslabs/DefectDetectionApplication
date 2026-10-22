# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Protocol-level suite of the Edge MCP child (``mcp_edge/child/server.py``).

Spec: .kiro/specs/mcp-servers/ (design.md §5 ``child/server.py``, §7, D34,
D35; Testing Strategy rows "16.1 protocol level" and "12.5 / child transport
security"; tasks.md 11.11). Requirements 8.1, 8.2, 8.4, 8.5, 9.2, 9.7, 10.7,
12.5, 15.3, 16.1.

The SDK's in-process ``Client`` (and, where the caller's ``Authorization``
header matters, the real Starlette app over ``httpx2.ASGITransport``)
against the child's ``Server``, with the Backend_API faked by
``_child_harness.FakeBackend``.
"""
import base64
import dataclasses
import json
import logging
import sys
import time

import pytest
from mcp.shared.exceptions import MCPError

import _child_harness as h
from mcp_edge.child import catalog, dispatch, server
from mcp_edge.child.__main__ import configure_logging
from mcp_edge.child.catalog import CATALOG, PATH_KEY_RE

BY_NAME = {s.name: s for s in CATALOG}
MUTATING = h.options(delivered_phases=h.ALL_PHASES, mutating_enabled=True)
MAX_RESULT_BYTES = 200_000


@pytest.fixture
def backend():
    fake = h.FakeBackend().install()
    yield fake
    dispatch.set_client(None)


def call(name, args=None, opts=None, specs=None, records=None):
    return h.in_memory(lambda client: client.call_tool(name, args or {}), opts, specs, records)


def text_of(result):
    return [c for c in result.content if c.type == 'text']


def images_of(result):
    return [c for c in result.content if c.type == 'image']


# ------------------------------------------------------------- listing

def test_discover_and_list(backend):
    async def go(client):
        first = await client.list_tools()
        second = await client.list_tools(cache_mode='refresh')
        return client.server_info, client.instructions, first, second

    info, instructions, first, second = h.in_memory(go)
    assert info.name == 'dda-edge-mcp' and info.version == '1.0.0'
    assert instructions == server.INSTRUCTIONS
    names = [t.name for t in first.tools]
    assert names == sorted(names) == [t.name for t in second.tools]
    assert first.ttl_ms == 60_000 and first.cache_scope == 'private'
    expected = {s.name for s in CATALOG if s.phase == 1 and s.read_only and s.feature is None}
    assert set(names) == expected
    for tool in first.tools:
        spec = BY_NAME[tool.name]
        assert tool.description == catalog.render_description(spec)
        assert tool.description.endswith(catalog.classification_text(spec))
        assert f'Touches: {spec.resource_type} on this device.' in tool.description
        assert tool.annotations.read_only_hint is spec.read_only
        assert tool.annotations.destructive_hint is spec.destructive
        assert tool.annotations.idempotent_hint is spec.idempotent
        assert tool.annotations.open_world_hint is False


def test_vllm_tools_are_listed_only_with_the_feature(backend):
    def names(opts):
        return {t.name for t in h.in_memory(lambda c: c.list_tools(), opts).tools}

    without, with_feature = names(h.options()), names(h.options(features=('vllm',)))
    vllm = {s.name for s in CATALOG if s.feature == 'vllm' and s.phase == 1}
    assert vllm and not vllm & without and vllm <= with_feature
    assert len(with_feature) == 47


def test_not_available_on_device_without_the_feature(backend):
    result = call('edge_textgen_models_list')
    assert result.is_error and result.structured_content['category'] == 'not_available_on_device'
    assert backend.requests == []


def test_alias_is_listed_and_runs_its_spec(backend):
    backend.routes[('GET', '/system/station')] = h.json_answer({'name': 's'})
    spec = dataclasses.replace(BY_NAME['edge_system_station_get'],
                               deprecated_alias='edge_station_get', alias_removed_in='2.0.0')
    specs = [spec if s.name == spec.name else s for s in CATALOG]
    listed = h.in_memory(lambda c: c.list_tools(), specs=specs).tools
    alias = [t for t in listed if t.name == 'edge_station_get']
    assert len(alias) == 1 and any(t.name == spec.name for t in listed)
    assert alias[0].description.startswith(
        'Deprecated alias of edge_system_station_get; removed in 2.0.0.')
    result = call('edge_station_get', specs=specs)
    assert not result.is_error and result.structured_content['result'] == {'name': 's'}
    assert 'deprecated alias' in result.structured_content['deprecation']


def test_unknown_and_undelivered_tools_are_protocol_errors(backend):
    async def go(client, name):
        try:
            await client.call_tool(name, {'registration_id': 'r1'})
        except MCPError as error:
            return error
        return None

    for name in ('edge_no_such_tool', 'edge_registration_trigger'):
        error = h.in_memory(lambda client: go(client, name))
        assert isinstance(error, MCPError) and error.error.code == -32602, name
    assert backend.requests == []


# ------------------------------------------------------------ RO calls

def test_registrations_list_scrubs_the_artifact_path(backend):
    backend.routes[('GET', '/workflows/registrations')] = h.json_answer(
        [{'registrationId': 'r1', 'artifactPath': '/aws_dda/registrations/r1/art.tar.gz'}])
    records = []
    result = call('edge_registrations_list', records=records)
    assert result.structured_content['result'] == [{'registrationId': 'r1',
                                                    'artifactPath': 'art.tar.gz'}]
    assert result.structured_content['scrubbed_fields'] == ['[].artifactPath']
    [record] = records
    assert record['outcome'] == 'success' and record['principal'] == 'op1'
    assert record['tool'] == 'edge_registrations_list' and record['status'] == 200


def test_preview_returns_exactly_one_image(backend):
    image = h.jpeg_b64(450_000)
    backend.routes[('POST', '/image-sources/src1/preview')] = h.json_answer({'image': image})
    result = call('edge_image_source_preview', {'image_source_id': 'src1'})
    assert not result.is_error
    images = images_of(result)
    assert len(images) == 1 and images[0].mime_type == 'image/jpeg' and images[0].data == image
    assert result.structured_content['result'] == {}


def test_the_same_body_without_an_image_field_is_dropped_by_the_guard(backend):
    """Control case of the preview call. A spec with image_field=None does not
    return the image; the oversized-image guard (D34(d)) replaces it rather
    than failing the call with result_too_large."""
    image = h.jpeg_b64(450_000)
    backend.routes[('POST', '/image-sources/src1/preview')] = h.json_answer({'image': image})
    spec = dataclasses.replace(BY_NAME['edge_image_source_preview'], image_field=None)
    specs = [spec if s.name == spec.name else s for s in CATALOG]
    result = call('edge_image_source_preview', {'image_source_id': 'src1'}, specs=specs)
    assert images_of(result) == []
    assert result.structured_content['result'] == {
        'image': f'<dropped: {len(image)} bytes image>'}
    assert result.structured_content['dropped_fields'] == ['image']


def test_overlay_returns_the_mask_and_the_background(backend):
    mask = h.png_b64(4_000)
    backend.routes[('GET', '/workflows/executions/e1/overlay')] = h.json_answer(
        {'maskImage': mask, 'maskBackground': {'width': 2, 'height': 2}})
    result = call('edge_execution_overlay_get', {'execution_id': 'e1'})
    [image] = images_of(result)
    assert image.mime_type == 'image/png'
    assert result.structured_content['result'] == {'maskBackground': {'width': 2, 'height': 2}}


def test_workflow_images_drop_frames_and_scrub_paths(backend):
    def answer(request):
        items = [{'captureId': f'c{i}', 'image': h.jpeg_b64(225_000),
                  'inferenceFilePath': f'/aws_dda/inference-results/wf/c{i}.jsonl'}
                 for i in range(2)]
        return h.json_answer({'images': items, 'nextStartingPoint': 2})

    backend.routes[('GET', '/workflows/wf/images')] = answer
    result = call('edge_workflow_images_list', {'workflow_id': 'wf'})
    assert images_of(result) == []
    content = result.structured_content
    assert content['dropped_fields'] == ['images[].image']
    assert [i['inferenceFilePath'] for i in content['result']['images']] == ['c0.jsonl',
                                                                             'c1.jsonl']
    params = backend.requests[-1].url.params
    assert params['maxResults'] == '2' and params['startingPoint'] == '0'


def test_workflow_images_limit_above_the_route_bound_is_refused(backend):
    result = call('edge_workflow_images_list', {'workflow_id': 'wf', 'limit': 3})
    assert result.structured_content['category'] == 'invalid_input'
    assert 'limit' in result.structured_content['message']
    assert backend.requests == []


def test_station_logo_is_dropped(backend):
    logo = 'data:image/png;base64,' + h.png_b64(300_000)
    backend.routes[('GET', '/system/station')] = h.json_answer({'name': 's', 'logoImage': logo})
    content = call('edge_system_station_get').structured_content
    assert content['result'] == {'name': 's'} and content['dropped_fields'] == ['logoImage']


def test_results_list_query_and_mask_drop(backend):
    def answer(request):
        results = [{'captureId': f'c{i}', 'maskImage': h.png_b64(225_000)} for i in range(12)]
        return h.json_answer({'total': 12, 'page': 2, 'size': 20, 'results': results})

    backend.routes[('GET', '/workflows/wf/results')] = answer
    result = call('edge_results_list', {'workflow_id': 'wf', 'page': 2, 'limit': 20,
                                        'prediction': 'Anomaly'})
    params = dict(backend.requests[-1].url.params)
    assert params == {'page': '2', 'size': '20', 'prediction': 'Anomaly'}
    content = result.structured_content
    assert len(content['result']['results']) == 12
    assert content['dropped_fields'] == ['results[].maskImage']


def test_result_get_mask_is_an_image(backend):
    mask = h.png_b64(6_000)
    backend.routes[('GET', '/workflows/wf/results/c1')] = h.json_answer(
        {'captureId': 'c1', 'maskImage': mask})
    result = call('edge_result_get', {'workflow_id': 'wf', 'capture_id': 'c1'})
    [image] = images_of(result)
    assert image.mime_type == 'image/png' and result.structured_content['result'] == {
        'captureId': 'c1'}


def test_registration_executions_are_dropped(backend):
    executions = [{'executionId': f'e{i}', 'pad': 'p' * 280} for i in range(3000)]
    backend.routes[('GET', '/workflows/registrations/r1')] = h.json_answer(
        {'registrationId': 'r1', 'executions': executions})
    content = call('edge_registration_get', {'registration_id': 'r1'}).structured_content
    assert content['dropped_fields'] == ['executions'] and 'executions' not in content['result']


def test_generic_image_field_is_replaced_by_the_guard(backend):
    image = h.jpeg_b64(20 * 1024)
    assert len(image) == 27308
    backend.routes[('GET', '/system/station')] = h.json_answer({'name': 's', 'banner': image})
    content = call('edge_system_station_get').structured_content
    assert content['result']['banner'] == '<dropped: 27308 bytes image>'
    assert content['dropped_fields'] == ['banner']


def test_per_spec_read_timeout(backend):
    backend.routes['*'] = h.json_answer({'text': 'ok'})
    call('edge_textgen_generate', {'model_name': 'm1', 'prompt': 'hi'},
         h.options(features=('vllm',)))
    call('edge_system_station_get')
    assert backend.timeouts == [('/text-generation/m1/generate', 130.0),
                                ('/system/station', 60.0)]


def test_textgen_generate_body(backend):
    backend.routes['*'] = h.json_answer({'text': 'ok'})
    call('edge_textgen_generate', {'model_name': 'm1', 'prompt': 'hi', 'max_tokens': 8},
         h.options(features=('vllm',)))
    assert backend.body_of() == {'prompt': 'hi', 'max_tokens': 8}


def test_execution_log_is_returned_unscrubbed(backend):
    backend.routes[('GET', '/workflows/executions/e1/log')] = lambda request: \
        __import__('httpx2').Response(200, text='/aws_dda/x: started\nline 2',
                                      headers={'content-type': 'text/plain'})
    content = call('edge_execution_log_get', {'execution_id': 'e1'}).structured_content
    assert content['result'] == {'text': '/aws_dda/x: started\nline 2'}
    assert 'scrubbed_fields' not in content


@pytest.mark.parametrize('length,tail,expected,truncated', [
    (900_000, None, 150_000, True), (900_000, 2_000, 2_000, True), (10_000, None, 10_000, False)])
def test_execution_log_tail(backend, length, tail, expected, truncated):
    import httpx2

    text = ''.join(chr(97 + i % 26) for i in range(length))
    backend.routes[('GET', '/workflows/executions/e1/log')] = httpx2.Response(
        200, text=text, headers={'content-type': 'text/plain'})
    args = {'execution_id': 'e1'}
    if tail is not None:
        args['tail_chars'] = tail
    content = call('edge_execution_log_get', args).structured_content
    assert content['result']['text'] == text[-expected:]
    assert content.get('truncated', False) is truncated
    if truncated:
        assert content['total_chars'] == length


@pytest.mark.parametrize('char', ['"\n', '\ufffd'])
def test_execution_log_tail_fits_the_encoded_bound(backend, char):
    import httpx2

    text = char * 150_000
    backend.routes[('GET', '/workflows/executions/e1/log')] = httpx2.Response(
        200, text=text, headers={'content-type': 'text/plain; charset=utf-8'})
    result = call('edge_execution_log_get', {'execution_id': 'e1'})
    assert not result.is_error
    content = result.structured_content
    assert content['truncated'] is True
    assert len(json.dumps({'text': content['result']['text']}).encode()) <= \
        MAX_RESULT_BYTES - 1_000


@pytest.mark.parametrize('capture_id', ['../../../cameras/X/disconnect', '..'])
def test_path_traversal_values_are_refused(backend, capture_id):
    result = call('edge_result_get', {'workflow_id': 'wf', 'capture_id': capture_id})
    assert result.structured_content['category'] == 'invalid_input'
    assert backend.requests == []


def test_colon_value_is_one_encoded_segment(backend):
    call('edge_result_get', {'workflow_id': 'wf', 'capture_id': 'a:b'})
    assert backend.requests[-1].url.raw_path.decode().endswith('/results/a%3Ab')


def test_no_spec_exposes_a_path_like_property():
    for spec in CATALOG:
        for name in spec.input_schema['properties']:
            assert not PATH_KEY_RE.match(name), (spec.name, name)
    assert BY_NAME['edge_image_source_add'].param_map['stream_url'] == 'location'


# ------------------------------------------------------- mutating calls

def test_registration_trigger_sends_no_body(backend):
    backend.routes['*'] = h.json_answer({'executionId': 'x1'})
    result = call('edge_registration_trigger', {'registration_id': 'r1'}, MUTATING)
    assert not result.is_error
    request = backend.requests[-1]
    assert request.method == 'POST' and request.url.path == \
        '/workflows/registrations/r1/trigger'
    assert request.content in (b'', b'{}')


def test_snapshot_create_gates_and_scrub(backend):
    backend.routes[('GET', '/snapshot')] = h.json_answer(
        {'archivePath': 'snapshotfile/snapshot-s-2026-10-19-08-15-00.tar.gz'})
    result = call('edge_system_snapshot_create', {'confirm': 'snapshot'}, MUTATING)
    content = result.structured_content
    assert content['result']['archivePath'] == 'snapshot-s-2026-10-19-08-15-00.tar.gz'
    assert content['scrubbed_fields'] == ['archivePath']
    missing = call('edge_system_snapshot_create', {}, MUTATING).structured_content
    assert missing['category'] == 'confirmation_required'
    disabled = call('edge_system_snapshot_create', {'confirm': 'snapshot'},
                    h.options(delivered_phases=h.ALL_PHASES)).structured_content
    assert disabled['category'] == 'mutating_tools_disabled'
    backend.principal = h.ANONYMOUS
    anonymous = call('edge_system_snapshot_create', {'confirm': 'snapshot'},
                     MUTATING).structured_content
    assert anonymous['category'] == 'not_authorized'
    assert len(backend.requests) == 1


def test_workflow_run_bodies_and_resource_id(backend):
    backend.routes['*'] = h.json_answer({'captureTaskId': 'task-1'})
    first = call('edge_workflow_run', {'workflow_id': 'wf'}, MUTATING)
    assert backend.body_of() == {'returnImageString': False}
    call('edge_workflow_run', {'workflow_id': 'wf', 'capture_image_count': 3,
                               'capture_time_interval': 1}, MUTATING)
    assert backend.body_of() == {'returnImageString': False, 'captureImageCount': 3,
                                 'captureTimeInterval': 1}
    assert first.structured_content['resource_id'] == 'task-1'


def test_image_source_capture(backend):
    image = h.jpeg_b64(450_000)
    backend.routes['*'] = h.json_answer({'image': image})
    result = call('edge_image_source_capture', {'image_source_id': 'src1'}, MUTATING)
    [content_image] = images_of(result)
    assert content_image.mime_type == 'image/jpeg'
    assert result.structured_content['result'] == {'captured': True}
    refused = call('edge_image_source_capture', {'image_source_id': 'src1',
                                                 'file_prefix': 'x/y'}, MUTATING)
    assert refused.structured_content['category'] == 'invalid_input'
    assert 'file_prefix' in refused.structured_content['message']


def test_static_image_pin_multipart(backend):
    raw = b'\xff\xd8\xff\xe0' + b'\x01' * 100_000
    backend.routes['*'] = h.json_answer({'pinned': True})
    call('edge_static_image_pin', {'image_base64': base64.b64encode(raw).decode(),
                                   'file_name': 'probe.jpg'}, MUTATING)
    request = backend.requests[-1]
    assert request.headers['content-type'].startswith('multipart/form-data')
    body = request.content
    assert body.count(b'Content-Disposition') == 1
    assert b'name="file"; filename="probe.jpg"' in body and raw in body


def test_static_image_pin_refusals(backend):
    too_long = call('edge_static_image_pin', {'image_base64': 'A' * 960_001,
                                              'file_name': 'x.jpg'}, MUTATING)
    assert too_long.structured_content['category'] == 'invalid_input'
    undecodable = call('edge_static_image_pin', {'image_base64': 'QUJD=A',
                                                 'file_name': 'x.jpg'}, MUTATING)
    assert undecodable.structured_content == {
        'category': 'invalid_input', 'message': 'image_base64 is not valid base64',
        'request_id': undecodable.structured_content['request_id'], 'status': None}
    # The Input-validation pattern ^[A-Za-z0-9+/=]+$ refuses these at the
    # schema, before any decode or request.
    for value in ('', 'QUJD\nRA==', 'QUJD-_=='):
        refused = call('edge_static_image_pin', {'image_base64': value,
                                                 'file_name': 'x.jpg'}, MUTATING)
        assert refused.structured_content['category'] == 'invalid_input'
        assert 'image_base64' in refused.structured_content['message']
        assert refused.structured_content['message'] != 'image_base64 is not valid base64'
    assert backend.requests == []


def test_results_summary_reset_time(backend):
    backend.routes['*'] = h.json_answer({'ok': True})
    before = int(time.time())
    call('edge_results_summary_reset', {'workflow_id': 'wf', 'confirm': 'wf'}, MUTATING)
    sent = backend.body_of()['resetTime']
    assert isinstance(sent, int) and before - 2 <= sent <= int(time.time()) + 2
    call('edge_results_summary_reset', {'workflow_id': 'wf', 'confirm': 'wf',
                                        'reset_time': 1_700_000_000}, MUTATING)
    assert backend.body_of() == {'resetTime': 1_700_000_000}


def test_image_source_add_maps_the_stream_url(backend):
    backend.routes['*'] = h.json_answer({'imageSourceId': 's1'})
    call('edge_image_source_add', {'type': 'RTSP', 'name': 'cam',
                                   'stream_url': 'rtsp://10.0.0.9:554/live'}, MUTATING)
    body = backend.body_of()
    assert body['location'] == 'rtsp://10.0.0.9:554/live' and 'stream_url' not in body
    refused = call('edge_image_source_add', {'type': 'RTSP', 'name': 'cam',
                                             'stream_url': '/aws_dda/folder'}, MUTATING)
    assert refused.structured_content['category'] == 'invalid_input'
    assert 'stream_url' in refused.structured_content['message']


def test_results_update_renames_item_fields(backend):
    backend.routes['*'] = h.json_answer({'updated': 1})
    call('edge_results_update', {'workflow_id': 'wf', 'inference_results': [
        {'capture_id': 'c1', 'human_classification': 'Anomaly'}]}, MUTATING)
    assert backend.body_of() == {'inferenceResults': [{'captureId': 'c1',
                                                       'humanClassification': 'Anomaly'}]}
    refused = call('edge_results_update', {'workflow_id': 'wf', 'inference_results': [
        {'human_classification': 'Anomaly'}]}, MUTATING)
    assert refused.structured_content['category'] == 'invalid_input'


def test_workflow_create_takes_the_id_from_a_bare_body(backend):
    backend.routes['*'] = h.json_answer('a1b2c3d4')
    result = call('edge_workflow_create', h.minimal_args(BY_NAME['edge_workflow_create']),
                  MUTATING)
    assert result.structured_content['resource_id'] == 'a1b2c3d4'


# ------------------------------------------------------------ failures

def test_error_shapes(backend):
    backend.routes[('GET', '/system/station')] = h.json_answer(
        {'detail': 'Traceback (most recent call last): File "x"'}, status=500)
    result = call('edge_system_station_get')
    assert result.is_error and text_of(result)[0].text == 'backend_failure: backend_failure'
    assert set(result.structured_content) == {'category', 'message', 'request_id', 'status'}
    assert result.structured_content['status'] == 500


def test_whoami_401_is_not_authenticated_and_nothing_is_dispatched(backend):
    backend.principal = 401
    records = []
    result = call('edge_system_station_get', records=records)
    assert result.structured_content['category'] == 'not_authenticated'
    assert backend.requests == []
    assert records[0]['outcome'] == 'not_authenticated' and records[0]['level'] == 'warning'


def test_mutating_timeout_names_the_status_tool(backend):
    import httpx2

    def stall(request):
        raise httpx2.ReadTimeout('stalled', request=request)

    backend.routes['*'] = stall
    spec = BY_NAME['edge_registration_trigger']
    content = call(spec.name, {'registration_id': 'r1'}, MUTATING).structured_content
    assert content['category'] == 'timeout' and content['effect'] == 'unknown'
    assert spec.status_tool and content['status_tool'] == spec.status_tool
    assert spec.status_tool in content['message']


# ----------------------------------------------- credential in the query

CREDENTIAL = 'local-session-credential-value-0123456789'


def _image_route(backend):
    import httpx2

    backend.routes[('GET', '/workflows/executions/e1/output-image')] = httpx2.Response(
        200, content=b'\xff\xd8\xff\xe0' + b'\x00' * 100, headers={'content-type': 'image/jpg'})


def test_credential_in_query_tool(backend):
    _image_route(backend)
    result = h.over_http(lambda c: c.call_tool('edge_execution_output_image_get',
                                               {'execution_id': 'e1'}),
                         headers={'Authorization': f'Bearer {CREDENTIAL}'})
    [image] = images_of(result)
    assert image.mime_type == 'image/jpeg'
    request = backend.requests[-1]
    assert request.url.params['token'] == CREDENTIAL
    assert 'authorization' not in request.headers
    assert backend.whoami_requests[-1].headers['authorization'] == f'Bearer {CREDENTIAL}'


def _never_logged(backend, caplog, capsys):
    capture = []

    class Capture(logging.Handler):
        def emit(self, record):
            capture.append(record)

    handler = Capture(level=logging.DEBUG)
    logging.getLogger().addHandler(handler)
    try:
        _image_route(backend)
        with caplog.at_level(logging.DEBUG):
            h.over_http(lambda c: c.call_tool('edge_execution_output_image_get',
                                              {'execution_id': 'e1'}),
                        headers={'Authorization': f'Bearer {CREDENTIAL}'})
    finally:
        logging.getLogger().removeHandler(handler)
    assert backend.requests[-1].url.params['token'] == CREDENTIAL
    for record in capture + list(caplog.records):
        assert CREDENTIAL not in record.getMessage(), record.name
        assert CREDENTIAL not in str(record.args)
    out = capsys.readouterr()
    assert CREDENTIAL not in out.err and CREDENTIAL not in out.out


def test_credential_in_query_never_logged(backend, caplog, capsys):
    configure_logging()
    _never_logged(backend, caplog, capsys)


def test_credential_in_query_never_logged_after_basic_config(backend, caplog, capsys):
    logging.basicConfig(level=logging.INFO, force=True)
    configure_logging()
    assert logging.getLogger().level == logging.WARNING
    _never_logged(backend, caplog, capsys)


# ---------------------------------------------------- transport security

def test_child_rejects_origin_and_foreign_host(backend):
    headers, body = h.captured_tools_list()
    app = server.build_app(h.options())
    plain = dict(headers)
    statuses = h.raw_requests(app, [
        ('POST', '/mcp', plain, body),
        ('POST', '/mcp', dict(plain, Origin='http://127.0.0.1:5000'), body),
        ('POST', '/mcp', dict(plain, Origin='http://localhost:5790'), body),
        ('POST', '/mcp', dict(plain, Host='10.0.0.5:5790'), body),
        ('POST', '/mcp', dict(plain, Host='localhost:5790'), body),
        ('POST', '/mcp', plain, b' ' * (1024 * 1024 + 1)),
    ])
    assert statuses == [200, 403, 403, 421, 200, 413]
    # Control: the SDK's own loopback defaults would accept both origins.
    control = server.build_app(h.options(), security=None)
    assert h.raw_requests(control, [
        ('POST', '/mcp', dict(plain, Origin='http://127.0.0.1:5000'), body),
        ('POST', '/mcp', dict(plain, Origin='http://localhost:5790'), body),
    ]) == [200, 200]


def test_direct_caller_without_a_credential_is_not_authenticated(backend):
    backend.principal = 401
    result = h.over_http(lambda c: c.call_tool('edge_system_station_get', {}))
    assert result.structured_content['category'] == 'not_authenticated'
    assert 'authorization' not in backend.whoami_requests[-1].headers
    assert backend.requests == []


def test_healthz(backend):
    app = server.build_app(h.options())
    assert h.raw_requests(app, [('GET', '/healthz', {}, None)]) == [200]


def test_request_id_header_is_forwarded(backend):
    backend.routes['*'] = h.json_answer({'name': 's'})
    h.over_http(lambda c: c.call_tool('edge_system_station_get', {}),
                headers={'X-Request-ID': 'corr-42'})
    assert backend.requests[-1].headers['x-request-id'] == 'corr-42'
    assert backend.whoami_requests[-1].headers['x-request-id'] == 'corr-42'


def test_child_package_has_no_backend_imports():
    loaded = [name for name in sys.modules
              if name.split('.')[0] in ('utils', 'local_auth', 'dao')]
    assert loaded == []
