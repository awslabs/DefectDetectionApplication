"""Result shaping of the Portal_MCP_Server (design §2 `shaping.py` row, D36,
§4 step 9, Property 9).

Order, for a backing response:

1. Use_Case binding (D36; revision 10, review-9 N5). On a 2xx of a spec with
   `bind_usecase_path`, `check_binding` resolves the dotted path in the body;
   an absent value, or one that is not the `usecase_id` the pre-dispatch
   check authorized, drops the body and returns `ToolFailure('not_found',
   'no such object in the named Use_Case', status=404)` with
   `binding_refused` set for the audit envelope. A backend 404 on such a
   spec returns the same failure, so the two results differ only in
   `request_id` (the real status goes to the log line and the envelope).
2. Any other status >= 300 -> `map_backend_status`.
3. Collections: a `limit` without a `param_map` entry is sliced here (the
   route returns everything), honouring `offset`; forwarded pagination is
   the route's own and is never cut.
4. `MAX_RESULT_BYTES`: a collection is cut from the end to fit, with
   `truncated: true`, `returned` and `total_hint`; a spec whose pagination
   is forwarded (`next_token` mapped) is not cut, because the route's token
   already points past the whole page, and returns `result_too_large`
   naming a smaller `limit`; a single object returns `result_too_large`.
5. `resource_id` from the well-known keys of a created object, or from the
   spec's `resource_id_path`.
"""
import json
import re
from dataclasses import dataclass
from typing import Optional

from mcp_portal.errors import ToolFailure, binding_failure, map_backend_status

MAX_RESULT_BYTES = 200_000
RESOURCE_ID_KEYS = ('deployment_id', 'training_id', 'job_id', 'model_id', 'workflow_id',
                    'compilation_job_name', 'build_id', 'run_id', 'package_id')
_SEGMENT_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?$')
_MISSING = object()


@dataclass
class Shaped:
    """The outcome of shaping: a success payload or a failure."""
    result: object = None
    resource_id: Optional[str] = None
    truncated: bool = False
    failure: Optional[ToolFailure] = None
    binding_refused: bool = False

    @property
    def structured(self) -> dict:
        content = {'result': self.result}
        if self.resource_id is not None:
            content['resource_id'] = self.resource_id
        if self.truncated:
            content['truncated'] = True
        return content

    def to_result(self, request_id: Optional[str]):
        if self.failure is not None:
            return self.failure.to_result(request_id)
        from mcp.types import CallToolResult, TextContent

        return CallToolResult(
            content=[TextContent(type='text', text=_dumps(self.result))],
            structured_content=self.structured)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def _size(value) -> int:
    return len(_dumps(value).encode('utf-8'))


def resolve(body, path: str):
    """The value at a dotted path (`a.b`, `items[0].id`), or `_MISSING`."""
    current = body
    for segment in path.split('.'):
        match = _SEGMENT_RE.match(segment)
        if match is None or not isinstance(current, dict) or match.group(1) not in current:
            return _MISSING
        current = current[match.group(1)]
        if match.group(2) is not None:
            index = int(match.group(2))
            if not isinstance(current, list) or index >= len(current):
                return _MISSING
            current = current[index]
    return current


def check_binding(spec, body, usecase_id: Optional[str]) -> Optional[ToolFailure]:
    """None when the object names the authorized Use_Case (D36)."""
    if not spec.bind_usecase_path:
        return None
    value = resolve(body, spec.bind_usecase_path)
    if value is _MISSING or not isinstance(value, str) or value != usecase_id:
        return binding_failure()
    return None


def _resource_id(spec, body) -> Optional[str]:
    if spec.resource_id_path:
        value = resolve(body, spec.resource_id_path)
        return str(value) if value is not _MISSING and value is not None else None
    if isinstance(body, dict):
        for key in RESOURCE_ID_KEYS:
            value = body.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                return str(value)
    return None


def _limit_default(spec) -> int:
    return spec.input_schema.get('properties', {}).get('limit', {}).get('default', 50)


def _collection(spec, body, args):
    """(container, key, items) of a collection body, or None."""
    if isinstance(body, list):
        return None, None, body
    if isinstance(body, dict):
        key = spec.collection_key
        if key and isinstance(body.get(key), list):
            return body, key, body[key]
    return None


def _rebuild(container, key, items, extra):
    if container is None:
        return {'items': items, **extra}
    rebuilt = dict(container)
    rebuilt[key] = items
    rebuilt.update(extra)
    return rebuilt


def shape(spec, args: dict, response) -> Shaped:
    """Shape one `BackendResponse` for the caller."""
    status, body = response.status, response.body_json
    if status >= 300:
        if spec.bind_usecase_path and status == 404:
            return Shaped(failure=binding_failure())
        return Shaped(failure=map_backend_status(status, body))
    refusal = check_binding(spec, body, args.get('usecase_id'))
    if refusal is not None:
        return Shaped(failure=refusal, binding_refused=True)
    resource_id = None if spec.read_only else _resource_id(spec, body)
    forwarded_cursor = any(c in spec.param_map for c in ('next_token', 'page'))
    found = _collection(spec, body, args) if spec.result_shape == 'collection' else None
    if found is None:
        if _size(body) > MAX_RESULT_BYTES:
            return Shaped(failure=ToolFailure(
                'result_too_large', f'the result exceeds {MAX_RESULT_BYTES} bytes'))
        return Shaped(result=body, resource_id=resource_id)
    container, key, items = found
    total = len(items)
    sliced = False
    if 'limit' in spec.input_schema.get('properties', {}) and 'limit' not in spec.param_map:
        offset = int(args.get('offset', 0) or 0)
        limit = int(args.get('limit', _limit_default(spec)))
        items = items[offset:offset + limit]
        sliced = True
    result = _rebuild(container, key, items, {})
    if _size(result) <= MAX_RESULT_BYTES:
        if sliced:
            result = _rebuild(container, key, items, {'returned': len(items), 'total_hint': total})
        return Shaped(result=result, resource_id=resource_id)
    if forwarded_cursor:
        limit = args.get('limit', _limit_default(spec))
        fits = max(1, _fitting(container, key, items) or 1)
        return Shaped(failure=ToolFailure(
            'result_too_large',
            f'the page exceeds {MAX_RESULT_BYTES} bytes; retry with limit <= '
            f'{min(fits, max(1, int(limit) - 1))}'))
    kept = _fitting(container, key, items)
    result = _rebuild(container, key, items[:kept],
                      {'truncated': True, 'returned': kept, 'total_hint': total})
    return Shaped(result=result, resource_id=resource_id, truncated=True)


def _fitting(container, key, items) -> int:
    """The largest prefix of `items` whose shaped result fits the bound."""
    low, high = 0, len(items)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = _rebuild(container, key, items[:middle],
                             {'truncated': True, 'returned': middle, 'total_hint': len(items)})
        if _size(candidate) <= MAX_RESULT_BYTES:
            low = middle
        else:
            high = middle - 1
    return low
