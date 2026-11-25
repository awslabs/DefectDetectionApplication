#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""rfdetr-seg-support task 10.7 (Requirements 13.3, 13.4, 17.3; design §10
"Edge MCP", C8 / C9): the dict-wildcard path segment ``{}`` beside ``[]`` and
the five edge tools whose results carry the Detection_List.

``shaping.resolve_path`` fans ``name[]`` over a list's elements and, new,
``name{}`` over a dict's values (the live ``inferenceResult.detections`` is a
map keyed ``"0"``, ``"1"``, ...); a value that is not that kind of collection
yields nothing, and ``drop_fields`` records a path iff it resolved. Every tool
that returns the list - ``edge_results_list``, ``edge_result_get``,
``edge_execution_overlay_get``, ``edge_workflow_images_list`` and
``edge_workflow_run`` - drops ``mask_rle`` and keeps the boxes and
``mask_area``; ``edge_result_get`` on a row with 50 instance masks of ~50 KB
each returns a ``Shaped`` result (boxes, ``mask_area``, the extracted
``maskImage``), never ``result_too_large``, because ``drop_fields`` runs before
``bound`` and ``MAX_RESULT_BYTES`` is not raised.
"""
import base64
import copy

import pytest

from mcp_edge.child import shaping
from mcp_edge.child.catalog import CATALOG
from mcp_edge.child.dispatch import ToolFailure

BY_NAME = {s.name: s for s in CATALOG}

# A base64 PNG-looking payload for the extracted mask image.
MASK_IMAGE_B64 = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'\x00' * 64).decode('ascii')

# A ~50 KB Canonical_RLE string (1080p instance mask as text, design DD4).
BIG_RLE = ' '.join(str(1000 + (i % 97)) for i in range(10_000))
assert 45_000 < len(BIG_RLE) < 60_000


def _entry(index, rle=True):
    entry = {
        'class_index': str(index % 3),
        'class_label': ('scratch', 'dent', 'chip')[index % 3],
        'bounding_box': [10 * index, 20, 10 * index + 40, 60],
        'confidence': 0.9 - 0.001 * index,
        'mask_area': 1234 + index,
    }
    if rle:
        entry['mask_rle'] = '27 4 8 4 8 4 8 4 125'
    return entry


def _list_form(count, rle=True):
    return [_entry(i, rle) for i in range(count)]


def _map_form(count, rle=True):
    return {str(i): _entry(i, rle) for i in range(count)}


def _walk(node, key, found):
    """Every value stored under ``key`` anywhere in ``node``."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                found.append(value)
            _walk(value, key, found)
    elif isinstance(node, list):
        for value in node:
            _walk(value, key, found)
    return found


# ------------------------------------------------------------- the grammar

class TestPathGrammar:
    def test_parse_path_modes(self):
        assert shaping._parse_path('a') == [('a', None)]
        assert shaping._parse_path('a[]') == [('a', 'list')]
        assert shaping._parse_path('a{}') == [('a', 'dict')]
        assert shaping._parse_path('x[].y{}.z') == [('x', 'list'), ('y', 'dict'), ('z', None)]

    @pytest.mark.parametrize('path', ['a{]', 'a[}', 'a{}{}', 'a[]{}', '{}', 'a.{}', 'a{}}', 'a{ }'])
    def test_parse_path_rejects_malformed_wildcards(self, path):
        with pytest.raises(ValueError):
            shaping._parse_path(path)

    @pytest.mark.parametrize('path', [
        'results[].detections[].mask_rle',
        'detections[].mask_rle',
        'instances[].mask_rle',
        'images[].inferenceResult.detections{}.mask_rle',
        'inferenceResult.detections{}.mask_rle',
    ])
    def test_the_catalog_paths_parse(self, path):
        assert shaping._PATH_RE.match(path)
        shaping._parse_path(path)


class TestResolvePathDictWildcard:
    def test_dict_step_fans_over_values(self):
        body = {'a': {'k1': {'b': 1}, 'k2': {'b': 2}, 'k3': {'c': 3}}}
        targets = shaping.resolve_path(body, 'a{}.b')
        assert sorted(container[key] for container, key in targets) == [1, 2]
        assert all(key == 'b' for _, key in targets)

    def test_dict_leaf_yields_the_dict_and_every_key(self):
        body = {'a': {'k1': 1, 'k2': 2}}
        targets = shaping.resolve_path(body, 'a{}')
        assert all(container is body['a'] for container, _ in targets)
        assert sorted(key for _, key in targets) == ['k1', 'k2']

    def test_nested_list_then_dict(self):
        body = {'x': [{'y': {'0': {'z': 'a'}, '1': {'z': 'b'}}},
                      {'y': {'0': {'z': 'c'}}},
                      {'y': []}]}
        targets = shaping.resolve_path(body, 'x[].y{}.z')
        assert sorted(container[key] for container, key in targets) == ['a', 'b', 'c']

    @pytest.mark.parametrize('body', [
        {'a': [{'b': 1}, {'b': 2}]},     # a list under a dict step
        {'a': 'text'},
        {'a': 7},
        {'a': None},
        {'a': {'k': 'not a dict'}},      # the step after `{}` finds no dict
        {},
    ])
    def test_non_dict_value_yields_nothing(self, body):
        assert shaping.resolve_path(body, 'a{}.b') == []
        if not isinstance(body.get('a'), dict):
            assert shaping.resolve_path(body, 'a{}') == []

    def test_list_step_still_ignores_a_dict(self):
        assert shaping.resolve_path({'a': {'0': {'b': 1}}}, 'a[].b') == []
        assert shaping.resolve_path({'a': {'0': 1}}, 'a[]') == []


class TestDropFieldsDictWildcard:
    def test_drops_and_records_iff_present(self):
        body = {'keep': 1, 'detections': {'0': {'mask_rle': 'x', 'mask_area': 5},
                                          '1': {'mask_area': 6}}}
        shaped, dropped = shaping.drop_fields(copy.deepcopy(body), ('detections{}.mask_rle',))
        assert dropped == ['detections{}.mask_rle']
        assert shaped == {'keep': 1, 'detections': {'0': {'mask_area': 5}, '1': {'mask_area': 6}}}
        # Nothing resolves on the shaped body: not recorded a second time.
        again, dropped_again = shaping.drop_fields(shaped, ('detections{}.mask_rle',))
        assert dropped_again == [] and again == shaped

    def test_absent_map_records_nothing(self):
        body = {'detections': {}}
        _, dropped = shaping.drop_fields(body, ('detections{}.mask_rle',))
        assert dropped == []
        _, dropped = shaping.drop_fields({'detections': [{'mask_rle': 'x'}]}, ('detections{}.mask_rle',))
        assert dropped == []

    def test_dict_leaf_deletes_every_value(self):
        body = {'m': {'a': 1, 'b': 2}, 'keep': 'kept'}
        shaped, dropped = shaping.drop_fields(body, ('m{}',))
        assert shaped == {'m': {}, 'keep': 'kept'} and dropped == ['m{}']


# ---------------------------------------------------------- the five tools

def _shape(name, body):
    shaped = shaping.shape_json(BY_NAME[name], {}, body)
    assert not isinstance(shaped, ToolFailure), shaped
    return shaped


class TestEdgeResultsList:
    def test_list_form_rows_lose_mask_rle_keep_boxes(self):
        body = {'results': [
            {'captureId': 'c1', 'maskImage': MASK_IMAGE_B64, 'detections': _list_form(3)},
            {'captureId': 'c2', 'maskImage': None, 'detections': None},
            {'captureId': 'c3', 'detections': _list_form(2, rle=False)},
        ], 'total': 3}
        shaped = _shape('edge_results_list', body)
        assert _walk(shaped.result, 'mask_rle', []) == []
        assert len(_walk(shaped.result, 'mask_area', [])) == 5
        assert len(_walk(shaped.result, 'bounding_box', [])) == 5
        assert shaped.result['results'][1]['detections'] is None
        assert shaped.dropped_fields == ['results[].maskImage', 'results[].detections[].mask_rle']

    def test_rows_without_masks_record_only_today_s_path(self):
        body = {'results': [{'captureId': 'c1', 'maskImage': 'x', 'detections': _list_form(2, rle=False)},
                            {'captureId': 'c2', 'maskImage': 'y'}], 'total': 2}
        shaped = _shape('edge_results_list', body)
        assert shaped.dropped_fields == ['results[].maskImage']


class TestEdgeResultGet:
    def test_drops_mask_rle_and_extracts_the_mask_image(self):
        body = {'captureId': 'c1', 'workflowId': 'w', 'prediction': 'Detection',
                'maskImage': MASK_IMAGE_B64, 'maskBackground': {'class-name': 'background'},
                'detections': _list_form(4)}
        shaped = _shape('edge_result_get', body)
        assert _walk(shaped.result, 'mask_rle', []) == []
        assert 'maskImage' not in shaped.result
        assert len(shaped.images) == 1 and shaped.images[0][1] == 'image/png'
        assert [d['mask_area'] for d in shaped.result['detections']] == [1234, 1235, 1236, 1237]
        assert all(len(d['bounding_box']) == 4 for d in shaped.result['detections'])
        assert shaped.dropped_fields == ['detections[].mask_rle']
        assert shaped.resource_id == 'c1'

    def test_fifty_large_masks_never_result_too_large(self):
        detections = [dict(_entry(i), mask_rle=BIG_RLE) for i in range(50)]
        body = {'captureId': 'c1', 'workflowId': 'w', 'prediction': 'Detection',
                'maskImage': MASK_IMAGE_B64, 'maskBackground': None, 'detections': detections}
        assert shaping.encoded_size(body) > 10 * shaping.MAX_RESULT_BYTES
        shaped = _shape('edge_result_get', body)
        assert isinstance(shaped, shaping.Shaped)
        assert shaped.truncated is False
        assert len(shaped.result['detections']) == 50
        assert _walk(shaped.result, 'mask_rle', []) == []
        assert all('mask_area' in d and 'bounding_box' in d for d in shaped.result['detections'])
        assert len(shaped.images) == 1
        assert shaping.encoded_size(shaped.result) <= shaping.MAX_RESULT_BYTES

    def test_max_result_bytes_is_not_raised(self):
        assert shaping.MAX_RESULT_BYTES == 200_000

    def test_plain_detection_row_is_unchanged_apart_from_the_image(self):
        body = {'captureId': 'c1', 'maskImage': None, 'detections': _list_form(2, rle=False)}
        shaped = _shape('edge_result_get', body)
        assert shaped.result['detections'] == _list_form(2, rle=False)
        assert shaped.dropped_fields == []


class TestEdgeExecutionOverlayGet:
    def test_instances_lose_mask_rle(self):
        body = {'maskImage': MASK_IMAGE_B64, 'maskBackground': {'class-name': 'background'},
                'instances': _list_form(3), 'detectionsTruncated': False}
        shaped = _shape('edge_execution_overlay_get', body)
        assert _walk(shaped.result, 'mask_rle', []) == []
        assert len(shaped.result['instances']) == 3
        assert shaped.result['detectionsTruncated'] is False
        assert len(shaped.images) == 1
        assert shaped.dropped_fields == ['instances[].mask_rle']

    def test_null_instances_record_nothing(self):
        body = {'maskImage': None, 'maskBackground': None, 'instances': None, 'detectionsTruncated': None}
        shaped = _shape('edge_execution_overlay_get', body)
        assert shaped.result == body and shaped.dropped_fields == []


class TestLiveMapFormTools:
    def _live_result(self, count, rle=True):
        return {'confidence': 0.9, 'inference_result': 'Detection', 'anomaly_score': 0.9,
                'anomaly_threshold': 1.0, 'detections': _map_form(count, rle), 'detection_count': count,
                'mask_image': MASK_IMAGE_B64}

    def test_edge_workflow_images_list_drops_map_values_mask_rle(self):
        body = {'images': [{'captureId': 'c1', 'image': 'x', 'inferenceResult': self._live_result(2)},
                           {'captureId': 'c2', 'image': 'y', 'inferenceResult': self._live_result(1)}]}
        shaped = _shape('edge_workflow_images_list', body)
        assert _walk(shaped.result, 'mask_rle', []) == []
        assert _walk(shaped.result, 'mask_image', []) == []
        kept = shaped.result['images'][0]['inferenceResult']['detections']
        assert set(kept) == {'0', '1'} and all('mask_area' in v and 'bounding_box' in v for v in kept.values())
        assert shaped.dropped_fields == ['images[].image', 'images[].inferenceResult.mask_image',
                                         'images[].inferenceResult.detections{}.mask_rle']

    def test_edge_workflow_images_list_plain_detection_keeps_today_s_list(self):
        body = {'images': [{'captureId': 'c1', 'image': 'x', 'inferenceResult': self._live_result(2, rle=False)}]}
        shaped = _shape('edge_workflow_images_list', body)
        assert shaped.dropped_fields == ['images[].image', 'images[].inferenceResult.mask_image']

    def test_edge_workflow_run_drops_map_values_mask_rle(self):
        body = {'captureId': 'c1', 'inferenceResult': self._live_result(3), 'processingTime': 12}
        shaped = _shape('edge_workflow_run', body)
        assert _walk(shaped.result, 'mask_rle', []) == []
        assert 'mask_image' not in shaped.result['inferenceResult']
        assert set(shaped.result['inferenceResult']['detections']) == {'0', '1', '2'}
        assert shaped.dropped_fields == ['inferenceResult.mask_image', 'inferenceResult.detections{}.mask_rle']

    def test_edge_workflow_run_plain_detection_keeps_today_s_list(self):
        body = {'captureId': 'c1', 'inferenceResult': self._live_result(1, rle=False)}
        shaped = _shape('edge_workflow_run', body)
        assert shaped.dropped_fields == ['inferenceResult.mask_image']
        assert shaped.result['inferenceResult']['detections'] == _map_form(1, rle=False)


def test_every_detection_list_tool_drops_mask_rle_and_nothing_else_moved():
    """The five tools' drop rules, by name, and the catalog count pins hold."""
    expected = {
        'edge_results_list': ('results[].maskImage', 'results[].detections[].mask_rle'),
        'edge_result_get': ('detections[].mask_rle',),
        'edge_execution_overlay_get': ('instances[].mask_rle',),
        'edge_workflow_images_list': ('images[].image', 'images[].inferenceResult.mask_image',
                                      'images[].inferenceResult.detections{}.mask_rle'),
        'edge_workflow_run': ('inferenceResult.mask_image', 'inferenceResult.detections{}.mask_rle'),
    }
    for name, paths in expected.items():
        assert BY_NAME[name].drop_fields == paths, name
    others = [s for s in CATALOG if s.name not in expected and any('mask_rle' in p for p in s.drop_fields)]
    assert others == []
    assert len(CATALOG) == 73
