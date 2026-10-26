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
"""Properties 9 (edge clauses) and 19: result bounding and path hygiene.

**Feature: mcp-servers, Property 9: Result bounding**
**Feature: mcp-servers, Property 19: Edge result path hygiene**

Spec: .kiro/specs/mcp-servers/ (design.md Correctness Properties, Properties
9 and 19; §5 ``child/shaping.py``, §7 step 5, D34; tasks.md 11.10).
**Validates: Requirements 8.5, 1.6**

The shaping functions are pure; bodies are generated, and the oracles below
re-derive each expected outcome from the property text rather than from
``shaping``'s helpers.
"""
import base64
import copy
import json
import os
import posixpath
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_edge.child import shaping
from mcp_edge.child.catalog import CATALOG, PATH_KEY_RE
from mcp_edge.child.dispatch import BackendResponse, ToolFailure

SETTINGS = settings(max_examples=100, deadline=None)
BY_NAME = {s.name: s for s in CATALOG}
MAX = shaping.MAX_RESULT_BYTES
MIB2 = 2 * 1024 * 1024
MAGIC = {'image/jpeg': b'\xff\xd8\xff\xe0', 'image/png': b'\x89PNG\r\n\x1a\n',
         'image/gif': b'GIF89a', 'image/bmp': b'BM'}
DEVICE_ROOT = re.compile(r'^(file://)?/(aws_dda|tmp|opt|home|root|var|etc|usr|snapshot)/\S*$')
URL_SCHEME = re.compile(r'^(?!file:)[a-z][a-z0-9+.-]*://')


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def image_b64(size: int, mime: str = 'image/jpeg') -> str:
    """Base64 of an image-looking payload of `size` decoded bytes."""
    magic = MAGIC[mime]
    return b64(magic + b'\x00' * max(0, size - len(magic)))


def size_of(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                          default=str).encode('utf-8'))


def json_response(body) -> BackendResponse:
    return BackendResponse(200, 'application/json', json.dumps(body).encode('utf-8'))


# ======================================================== Property 9 (edge)

SLICED = [s for s in CATALOG if s.result_shape == 'collection'
          and 'limit' in s.input_schema['properties'] and 'limit' not in s.param_map]
IMAGE_SPECS = [s for s in CATALOG if s.image_field]
DROP_SPECS = [s for s in CATALOG if s.drop_fields]
PLAIN_OBJECT_SPECS = [s for s in CATALOG if s.result_shape == 'object' and not s.image_field
                      and not s.drop_fields and not s.text_tail_arg]

sizes = st.one_of(st.lists(st.integers(0, 30_000), max_size=60),
                  st.lists(st.integers(5_000, 30_000), min_size=10, max_size=60))


@SETTINGS
@given(data=st.data(), spec=st.sampled_from(SLICED), sizes=sizes)
def test_collection_is_a_bounded_contiguous_slice(data, spec, sizes):
    limit_schema = spec.input_schema['properties']['limit']
    limit = data.draw(st.integers(limit_schema['minimum'], limit_schema['maximum']))
    offset = data.draw(st.integers(0, len(sizes) + 5))
    items = [{'id': f'item-{i}', 'pad': 'x' * n} for i, n in enumerate(sizes)]
    body = {spec.collection_key: items} if spec.collection_key else items
    shaped = shaping.shape_json(spec, {'limit': limit, 'offset': offset}, body)
    assert not isinstance(shaped, ToolFailure)
    returned = shaped.result[spec.collection_key] if spec.collection_key else shaped.result
    window = items[offset:offset + limit]
    assert len(returned) <= limit
    assert returned == items[offset:offset + len(returned)]
    assert size_of(shaped.result) <= MAX
    assert shaped.truncated is (len(returned) < len(window))


@SETTINGS
@given(spec=st.sampled_from(IMAGE_SPECS), mime=st.sampled_from(sorted(MAGIC)),
       size=st.one_of(st.integers(1, 4_096), st.integers(MIB2 - 8, MIB2 + 8),
                      st.sampled_from((MIB2 // 2, 3 * MIB2))),
       data_uri=st.booleans(), extra=st.text(alphabet='abcdef ', max_size=20))
def test_image_field_is_extracted_and_never_counts_toward_the_bound(spec, mime, size,
                                                                     data_uri, extra):
    encoded = image_b64(size, mime)
    value = f'data:{mime};base64,{encoded}' if data_uri else encoded
    body = {spec.image_field: value, 'note': extra}
    shaped = shaping.shape(spec, {}, json_response(body))
    if size > MIB2:
        assert isinstance(shaped, ToolFailure) and shaped.category == 'result_too_large'
        assert shaped.details == {'bytes': size, 'limit': MIB2}
        return
    assert not isinstance(shaped, ToolFailure)
    assert shaped.images == [(encoded, mime)]
    assert spec.image_field not in shaped.result
    expected = {'note': extra}
    if spec.result_constant is not None:
        expected.update(spec.result_constant)
    assert shaped.result == expected
    result = shaped.to_result('r')
    images = [c for c in result.content if c.type == 'image']
    assert len(images) == 1 and images[0].data == encoded and images[0].mime_type == mime


def _resolves(body, path):
    """True when the dotted path (`[]` = every element) names something."""
    nodes = [body]
    for part in path.split('.'):
        each = part.endswith('[]')
        name = part[:-2] if each else part
        nxt = []
        for node in nodes:
            if isinstance(node, dict) and name in node:
                value = node[name]
                if each:
                    if isinstance(value, list):
                        nxt.extend(value)
                else:
                    nxt.append(value)
        nodes = nxt
        if each and not nodes:
            return False
    if path.endswith('[]'):
        return bool(nodes)
    return bool(nodes)


def _fill(path, present):
    """A body in which `path` resolves (`present`) or not."""
    parts = path.split('.')
    if not present:
        return {}
    def build(index):
        part = parts[index]
        each = part.endswith('[]')
        name = part[:-2] if each else part
        leaf = index == len(parts) - 1
        # Fresh values per element: a parsed JSON body never shares objects.
        make = (lambda: 'v') if leaf else (lambda: build(index + 1))
        return {name: [make(), make()] if each else make()}
    return build(0)


def _merge(a, b):
    for key, value in b.items():
        if key in a and isinstance(a[key], dict) and isinstance(value, dict):
            _merge(a[key], value)
        elif key in a and isinstance(a[key], list) and isinstance(value, list):
            for left, right in zip(a[key], value):
                if isinstance(left, dict) and isinstance(right, dict):
                    _merge(left, right)
        else:
            a[key] = value
    return a


@SETTINGS
@given(data=st.data(), spec=st.sampled_from(DROP_SPECS))
def test_drop_fields_resolve_to_nothing_and_are_listed_iff_present(data, spec):
    present = {path: data.draw(st.booleans(), label=path) for path in spec.drop_fields}
    body = {'keep': 'kept'}
    for path, flag in present.items():
        _merge(body, _fill(path, flag))
    original = copy.deepcopy(body)
    shaped = shaping.shape_json(spec, {}, body)
    assert not isinstance(shaped, ToolFailure)
    for path in spec.drop_fields:
        assert not _resolves(shaped.result, path), path
    assert shaped.dropped_fields == [p for p in spec.drop_fields if _resolves(original, p)]
    assert shaped.result['keep'] == 'kept'


@st.composite
def guarded_bodies(draw):
    """A nested body with one >= 16 KiB image string at a random path, plus
    strings the guard must leave alone."""
    big = image_b64(draw(st.integers(12_300, 40_000)), draw(st.sampled_from(sorted(MAGIC))))
    small = image_b64(draw(st.integers(10, 12_000)))
    plain_b64 = b64(b'\x00' * draw(st.integers(12_300, 30_000)))
    depth = draw(st.integers(0, 3))
    in_array = draw(st.booleans())
    keys = [draw(st.sampled_from(('alpha', 'beta', 'gamma', 'delta'))) for _ in range(depth)]
    leaf = draw(st.sampled_from(('blob', 'thumb', 'data')))
    holder = {leaf: big, 'small': small, 'raw': plain_b64, 'name': 'n'}
    node = [holder, {'name': 'second'}] if in_array else holder
    for key in reversed(keys):
        node = {key: node, 'sibling': 'text'}
    body = node if isinstance(node, dict) else {'items': node}
    path = '.'.join(keys + (['items'] if not keys and in_array else []))
    if in_array:
        path = (path + '[]') if path else 'items[]'
    path = f'{path}.{leaf}' if path else leaf
    return body, path, len(big)


@SETTINGS
@given(spec=st.sampled_from(PLAIN_OBJECT_SPECS), case=guarded_bodies())
def test_oversized_image_guard(spec, case):
    body, path, length = case
    original = copy.deepcopy(body)
    shaped = shaping.shape_json(spec, {}, body)
    assert not isinstance(shaped, ToolFailure)
    assert path in shaped.dropped_fields
    expected = copy.deepcopy(original)

    def replace(node, parts):
        head, rest = parts[0], parts[1:]
        if head.endswith('[]'):
            children = [c for c in node[head[:-2]] if isinstance(c, dict)]
        elif not rest:
            if head in node:
                node[head] = f'<dropped: {length} bytes image>'
            return
        else:
            children = [node[head]]
        for child in children:
            if rest:
                replace(child, rest)

    replace(expected, path.split('.'))
    assert shaped.result == expected
    assert shaped.dropped_fields == [path]


# ---------------------------------------------------- fixed cases (a)-(g)

def _jpeg(size):
    return image_b64(size, 'image/jpeg')


def test_fixed_a_station_logo_is_dropped():
    logo = 'data:image/png;base64,' + image_b64(300_000, 'image/png')
    shaped = shaping.shape(BY_NAME['edge_system_station_get'], {},
                           json_response({'name': 's', 'logoImage': logo}))
    assert shaped.result == {'name': 's'} and shaped.dropped_fields == ['logoImage']


def test_fixed_b_workflow_images_fields_are_dropped():
    body = {'images': [{'image': _jpeg(225_000),
                        'inferenceResult': {'mask_image': image_b64(150_000, 'image/png'),
                                            'inference_result': 'Anomaly'}}]}
    shaped = shaping.shape(BY_NAME['edge_workflow_images_list'], {}, json_response(body))
    assert shaped.dropped_fields == ['images[].image', 'images[].inferenceResult.mask_image']
    assert shaped.result == {'images': [{'inferenceResult': {'inference_result': 'Anomaly'}}]}
    assert shaped.images == []


def test_fixed_c_results_list_mask_images_are_dropped():
    body = {'total': 1, 'page': 1, 'size': 50,
            'results': [{'captureId': 'c1', 'maskImage': image_b64(225_000, 'image/png')}]}
    shaped = shaping.shape(BY_NAME['edge_results_list'], {}, json_response(body))
    assert shaped.dropped_fields == ['results[].maskImage']
    assert shaped.result['results'][0]['captureId'] == 'c1'


def test_fixed_d_result_get_mask_is_one_image():
    mask = image_b64(5_000, 'image/png')
    shaped = shaping.shape(BY_NAME['edge_result_get'], {},
                           json_response({'captureId': 'c1', 'maskImage': mask}))
    assert shaped.images == [(mask, 'image/png')] and shaped.result == {'captureId': 'c1'}


def test_fixed_e_workflow_run_image_is_dropped_by_the_guard():
    image = _jpeg(450_000)
    body = {'captureId': 'c1', 'image': image, 'inferenceResult': {'inference_result': 'Normal'}}
    shaped = shaping.shape(BY_NAME['edge_workflow_run'], {}, json_response(body))
    assert shaped.dropped_fields == ['image']
    assert shaped.result['image'] == f'<dropped: {len(image)} bytes image>'
    assert shaped.images == [] and shaped.resource_id == 'c1'


def test_fixed_f_capture_is_one_image_and_the_constant():
    image = _jpeg(450_000)
    shaped = shaping.shape(BY_NAME['edge_image_source_capture'], {},
                           json_response({'image': image}))
    assert shaped.images == [(image, 'image/jpeg')] and shaped.result == {'captured': True}


def test_fixed_g_registration_executions_are_dropped():
    executions = [{'executionId': f'e{i}', 'pad': 'p' * 280} for i in range(3000)]
    shaped = shaping.shape(BY_NAME['edge_registration_get'], {},
                           json_response({'registrationId': 'r', 'executions': executions}))
    assert not isinstance(shaped, ToolFailure)
    assert 'executions' not in shaped.result and shaped.dropped_fields == ['executions']


def test_guard_leaves_small_images_non_images_and_the_image_field():
    small = _jpeg(2_000)
    raw = b64(b'\x00' * 20_000)
    shaped = shaping.shape_json(BY_NAME['edge_system_health_get'] if 'edge_system_health_get'
                                in BY_NAME else PLAIN_OBJECT_SPECS[0], {},
                                {'small': small, 'raw': raw})
    assert shaped.result == {'small': small, 'raw': raw} and shaped.dropped_fields == []


def test_without_an_image_field_the_guard_drops_the_image():
    """The same 600 KB body through a spec with no image_field: the
    oversized-image guard (D34(d)) replaces it, so it is never
    result_too_large on account of that field."""
    image = _jpeg(450_000)
    shaped = shaping.shape_json(PLAIN_OBJECT_SPECS[0], {}, {'image': image})
    assert shaped.dropped_fields == ['image']
    assert shaped.result == {'image': f'<dropped: {len(image)} bytes image>'}


# =============================================================== Property 19

PATH_KEYS = ('inferenceFilePath', 'imageDataFilePath', 'inputImageFilePath', 'archivePath',
             'artifactPath', 'captureIdPath', 'outputDir', 'location', 'captureLocation',
             'imageFileName', 'snapshotPath', 'workDir', 'exportFolder')
ORDINARY_KEYS = ('name', 'endpoint', 'message', 'id', 'status', 'url', 'note')
segment = st.from_regex(r'[A-Za-z0-9_.-]{1,12}', fullmatch=True)
roots = st.sampled_from(('aws_dda', 'tmp', 'opt', 'home', 'root', 'var', 'etc', 'usr',
                         'snapshot'))
values = st.one_of(
    st.builds(lambda r, parts, slash: '/' + r + '/' + '/'.join(parts) + ('/' if slash else ''),
              roots, st.lists(segment, min_size=0, max_size=3), st.booleans()),
    st.builds(lambda r, parts: 'file:///' + r + '/' + '/'.join(parts), roots,
              st.lists(segment, min_size=1, max_size=3)),
    st.builds(lambda parts: '/'.join(parts), st.lists(segment, min_size=2, max_size=3)),
    segment,
    st.builds(lambda scheme, host, path: f'{scheme}://{host}/{path}',
              st.sampled_from(('rtsp', 'https', 'http', 'rtmp')), segment, segment),
    st.text(alphabet='abc /:\n', max_size=20),
    st.builds(lambda r, p: f'/{r}/{p} is locked', roots, segment),
    st.just('/mcp'))
json_bodies = st.recursive(
    st.one_of(values, st.integers(), st.booleans(), st.none()),
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(st.sampled_from(PATH_KEYS + ORDINARY_KEYS), inner, max_size=4)),
    max_leaves=12)


def _file_name(value: str) -> str:
    stripped = value[len('file://'):] if value.startswith('file://') else value
    return posixpath.basename(stripped.rstrip('/'))


def _path_like(value: str) -> bool:
    return '/' in value and not URL_SCHEME.match(value)


def _walk(node, path='', key=None):
    """(path, key, value) of every string value, with the key that governs it
    (a list's items are governed by the list's key)."""
    if isinstance(node, dict):
        for child_key, value in node.items():
            child = f'{path}.{child_key}' if path else child_key
            if isinstance(value, str):
                yield child, child_key, value
            else:
                yield from _walk(value, child, child_key)
    elif isinstance(node, list):
        child = f'{path}[]'
        for value in node:
            if isinstance(value, str):
                yield child, key, value
            else:
                yield from _walk(value, child, key)


def _strings_in_order(node):
    return list(_walk(node))


@SETTINGS
@given(body=json_bodies)
def test_edge_result_path_hygiene(body):
    original = copy.deepcopy(body)
    out, scrubbed = shaping.scrub_paths(copy.deepcopy(body))
    if isinstance(original, str):
        expected = _file_name(original) if DEVICE_ROOT.match(original) else original
        assert out == expected
        return
    before, after = _strings_in_order(original), _strings_in_order(out)
    assert [p for p, _, _ in before] == [p for p, _, _ in after]
    changed = []
    for (path, key, old), (_, _, new) in zip(before, after):
        governed = key is not None and PATH_KEY_RE.match(key)
        if governed:
            assert not _path_like(new), (path, new)
        assert not DEVICE_ROOT.match(new), (path, new)
        if new != old:
            assert new == _file_name(old), (path, old, new)
            if path not in changed:
                changed.append(path)
        if URL_SCHEME.match(old) or (not governed and not DEVICE_ROOT.match(old)):
            assert new == old, (path, old)
    assert scrubbed == changed
    # Idempotent.
    again, rescrubbed = shaping.scrub_paths(copy.deepcopy(out))
    assert again == out and rescrubbed == []


@pytest.mark.parametrize('body,result,fields', [
    ({'archivePath': 'snapshotfile/snapshot-s-2026-10-19-08-15-00.tar.gz'},
     {'archivePath': 'snapshot-s-2026-10-19-08-15-00.tar.gz'}, ['archivePath']),
    ({'images': [{'inferenceFilePath': '/aws_dda/inference-results/wf/0f3c.jsonl'}]},
     {'images': [{'inferenceFilePath': '0f3c.jsonl'}]}, ['images[].inferenceFilePath']),
    ({'imageFileName': 'img_001.jpg'}, {'imageFileName': 'img_001.jpg'}, []),
    ({'location': 'rtsp://10.0.0.9:554/live'}, {'location': 'rtsp://10.0.0.9:554/live'}, []),
    ({'location': '/aws_dda/image-capture/folder-src'}, {'location': 'folder-src'},
     ['location']),
    ({'message': '/aws_dda/system/db.sqlite is locked'},
     {'message': '/aws_dda/system/db.sqlite is locked'}, []),
    ({'message': '/aws_dda/system/db.sqlite'}, {'message': 'db.sqlite'}, ['message']),
    ({'endpoint': '/mcp'}, {'endpoint': '/mcp'}, []),
])
def test_path_hygiene_fixed_cases(body, result, fields):
    out, scrubbed = shaping.scrub_paths(copy.deepcopy(body))
    assert out == result and scrubbed == fields


@pytest.mark.parametrize('content_type,raw', [
    ('text/plain', b'/aws_dda/x: started\nline 2'),
    ('application/json', b'/aws_dda/x not json'),
])
def test_text_wrapper_is_returned_byte_identical(content_type, raw):
    spec = BY_NAME['edge_execution_log_get'] if content_type == 'text/plain' \
        else PLAIN_OBJECT_SPECS[0]
    shaped = shaping.shape(spec, {}, BackendResponse(200, content_type, raw))
    assert shaped.result == {'text': raw.decode()} and shaped.scrubbed_fields == []


def test_result_constant_passes_through_the_scrub():
    """Revision 9 / review-8 N1 order: extraction, then the constant, then the scrub."""
    import dataclasses

    spec = dataclasses.replace(BY_NAME['edge_image_source_capture'],
                               result_constant={'captured': True,
                                                'where': '/aws_dda/system/a.txt'})
    shaped = shaping.shape_json(spec, {}, {'image': _jpeg(1_000), 'note': '/aws_dda/x/y.txt'})
    assert shaped.result == {'captured': True, 'where': 'a.txt', 'note': 'y.txt'}
    assert os.path.basename('/aws_dda/x/y.txt') == 'y.txt'
