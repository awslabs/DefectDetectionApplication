"""Property 9: Result bounding (portal clauses).

**Feature: mcp-servers, Property 9: Result bounding**

Spec: .kiro/specs/mcp-servers/ (design.md Correctness Properties, Property 9;
§2 `shaping.py` row, §4 step 9, Error Handling "Result shaping"; tasks.md
6.7). **Validates: Requirements 1.6**

For any collection and `limit`/`offset`, the shaped result of a
dispatcher-sliced collection has at most `limit` items, is a contiguous slice
starting at `offset`, serializes to at most `MAX_RESULT_BYTES`, and is marked
`truncated` iff items were dropped for size; a forwarded page is never cut
(whole, or `result_too_large`); a single object over the bound is
`result_too_large`.
"""
import json

from hypothesis import event, given, settings
from hypothesis import strategies as st

from mcp_portal import catalog as cat
from mcp_portal import shaping
from mcp_portal.dispatch import BackendResponse

SETTINGS = settings(max_examples=100, deadline=None)
MAX = shaping.MAX_RESULT_BYTES

COLLECTIONS = [s for s in cat.CATALOG if s.result_shape == 'collection']
SLICED = [s for s in COLLECTIONS if 'limit' in s.input_schema['properties']
          and 'limit' not in s.param_map]
FORWARDED = [s for s in COLLECTIONS
             if any(c in s.param_map for c in ('next_token', 'page'))]
OBJECTS = [s for s in cat.CATALOG if s.result_shape == 'object' and s.backend is not None
           and not s.bind_usecase_path]


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                          default=str).encode('utf-8'))


def _items(sizes):
    return [{'id': f'item-{i}', 'pad': 'x' * n} for i, n in enumerate(sizes)]


def _body(spec, items):
    return {spec.collection_key: items, 'count': len(items)} if spec.collection_key else items


def _returned(spec, result):
    return result[spec.collection_key] if spec.collection_key else result['items']


# Small collections, and large ones whose items overflow the bound together.
sizes = st.one_of(st.lists(st.integers(0, 30_000), max_size=60),
                  st.lists(st.integers(5_000, 30_000), min_size=10, max_size=60))


@SETTINGS
@given(data=st.data(), spec=st.sampled_from(SLICED), sizes=sizes)
def test_dispatcher_sliced_collection(data, spec, sizes):
    limit_schema = spec.input_schema['properties']['limit']
    limit = data.draw(st.integers(limit_schema['minimum'], limit_schema['maximum']))
    offset = data.draw(st.integers(0, len(sizes) + 5))
    items = _items(sizes)
    args = {'usecase_id': 'uc', 'limit': limit, 'offset': offset}
    shaped = shaping.shape(spec, args, BackendResponse(200, _body(spec, items)))
    assert shaped.failure is None
    returned = _returned(spec, shaped.result)
    window = items[offset:offset + limit]
    assert len(returned) <= limit
    assert returned == items[offset:offset + len(returned)]
    assert _size(shaped.result) <= MAX
    dropped = len(returned) < len(window)
    event('truncated' if dropped else 'whole window')
    assert shaped.truncated is dropped
    assert bool(shaped.result.get('truncated')) is dropped
    if dropped:
        # Cut from the end only as far as the bound requires.
        assert len(returned) == len(window) or _size(
            {**shaped.result, ('items' if not spec.collection_key else spec.collection_key):
             window[:len(returned) + 1]}) > MAX
    rendered = shaped.to_result('r')
    assert rendered.structured_content.get('truncated', False) is dropped


@SETTINGS
@given(data=st.data(), spec=st.sampled_from(FORWARDED), sizes=sizes)
def test_forwarded_page_is_never_cut(data, spec, sizes):
    limit_schema = spec.input_schema['properties'].get('limit')
    args = {'usecase_id': 'uc'}
    if limit_schema:
        args['limit'] = data.draw(st.integers(limit_schema['minimum'],
                                              limit_schema['maximum']))
    items = _items(sizes)
    body = _body(spec, items)
    if isinstance(body, dict):
        body['nextToken'] = 'cursor'
    shaped = shaping.shape(spec, args, BackendResponse(200, body))
    event('result_too_large' if shaped.failure is not None else 'whole page')
    if shaped.failure is not None:
        assert shaped.failure.category == 'result_too_large'
        assert 'limit <=' in shaped.failure.message
        assert _size(body) > MAX
    else:
        assert _returned(spec, shaped.result) == items
        assert not shaped.truncated and 'truncated' not in shaped.result
        assert _size(shaped.result) <= MAX


@SETTINGS
@given(spec=st.sampled_from(OBJECTS), size=st.integers(0, 2 * MAX))
def test_single_object_bound(spec, size):
    body = {'name': 'object', 'blob': 'z' * size}
    shaped = shaping.shape(spec, {'usecase_id': 'uc'}, BackendResponse(200, body))
    event('over' if _size(body) > MAX else 'within')
    if _size(body) > MAX:
        assert shaped.failure is not None and shaped.failure.category == 'result_too_large'
    else:
        assert shaped.failure is None and shaped.result == body and not shaped.truncated
