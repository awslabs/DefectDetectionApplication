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
"""Result shaping of the child (design §5 ``child/shaping.py``, §7 step 5,
D34; Properties 9 and 19; Requirements 8.5, 8.6).

A 2xx JSON body passes, in this one order:

(a) ``drop_fields`` (root-relative dotted paths, ``[]`` = every array
    element) removed, ``dropped_fields`` recorded;
(b) ``extract_image(spec.image_field)``: a non-empty base64 string decoded
    and sniffed; <= 2 MiB -> one ``ImageContent`` (the original string, the
    sniffed MIME type, default ``image/jpeg``) and the field deleted; larger
    -> ``result_too_large {bytes, limit}``; undecodable -> ``backend_failure``;
    then ``spec.result_constant`` merged;
(c) ``drop_oversized_images``: any other string >= 16 KiB whose first
    decoded bytes are an image magic number -> ``"<dropped: <n> bytes
    image>"``, recorded in ``dropped_fields``;
(d) ``scrub_paths``: a path-like value under a ``PATH_KEY_RE`` key, and any
    single-token device-root value, replaced by its file name, recorded in
    ``scrubbed_fields``;
(e) collection slicing (specs whose route does not paginate) and the
    ``MAX_RESULT_BYTES`` bound.

``image/*`` bodies become one ``ImageContent`` (``image/jpg`` ->
``image/jpeg``) when <= 2 MiB. A ``text/plain`` body is cut, when the spec
sets ``text_tail_arg``, to the longest suffix of at most that many
characters whose JSON encoding fits ``MAX_RESULT_BYTES - 1 000`` bytes, then
wrapped as ``{"text": ...}``; the wrapper (also used for a non-JSON body) is
never walked. ``resource_id`` is a named body key (``captureId``,
``captureTaskId``, ``executionId``), the scalar body of a
``resource_id_from_body`` spec, or the spec's ``resource_id_path``.
"""
import base64
import binascii
import copy
import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

from mcp_edge.child.catalog import PATH_KEY_RE
from mcp_edge.child.dispatch import BackendResponse, ToolFailure, failure_for

MAX_RESULT_BYTES = 200_000
TEXT_MARGIN_BYTES = 1_000
MAX_IMAGE_BYTES = 2 * 1024 * 1024
OVERSIZED_IMAGE_CHARS = 16 * 1024
RESOURCE_ID_KEYS = ('captureId', 'captureTaskId', 'executionId')

DEVICE_ROOT_RE = re.compile(r'^(file://)?/(aws_dda|tmp|opt|home|root|var|etc|usr|snapshot)/\S*$')
URL_SCHEME_RE = re.compile(r'^(?!file:)[a-z][a-z0-9+.-]*://')
_PATH_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(\[\])?(\.[A-Za-z_][A-Za-z0-9_]*(\[\])?)*$')
_MISSING = object()

_MAGIC = (
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
    (b'BM', 'image/bmp'),
    (b'II*\x00', 'image/tiff'),
    (b'MM\x00*', 'image/tiff'),
)


# ----------------------------------------------------------------- outcome

@dataclass
class Shaped:
    result: object = None
    images: List[Tuple[str, str]] = field(default_factory=list)   # (base64, mime type)
    dropped_fields: List[str] = field(default_factory=list)
    scrubbed_fields: List[str] = field(default_factory=list)
    truncated: bool = False
    total_chars: Optional[int] = None
    resource_id: Optional[str] = None

    @property
    def structured(self) -> dict:
        content = {'result': self.result}
        if self.scrubbed_fields:
            content['scrubbed_fields'] = list(self.scrubbed_fields)
        if self.dropped_fields:
            content['dropped_fields'] = list(self.dropped_fields)
        if self.truncated:
            content['truncated'] = True
        if self.total_chars is not None:
            content['total_chars'] = self.total_chars
        if self.resource_id is not None:
            content['resource_id'] = self.resource_id
        return content

    def content(self) -> list:
        from mcp.types import ImageContent, TextContent

        items = [TextContent(type='text', text=dumps(self.result))]
        items.extend(ImageContent(type='image', data=data, mime_type=mime)
                     for data, mime in self.images)
        return items

    def to_result(self, request_id: Optional[str] = None):
        from mcp.types import CallToolResult

        return CallToolResult(content=self.content(), structured_content=self.structured)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def encoded_size(value) -> int:
    return len(dumps(value).encode('utf-8'))


# ------------------------------------------------------------------ paths

def _parse_path(path: str) -> List[Tuple[str, bool]]:
    if not _PATH_RE.match(path):
        raise ValueError(f'not a dotted path: {path!r}')
    return [(part[:-2], True) if part.endswith('[]') else (part, False)
            for part in path.split('.')]


def resolve_path(body, path: str) -> list:
    """Every ``(container, key)`` the path names (``[]`` fans out)."""
    found = []

    def walk(node, steps):
        name, each = steps[0]
        if not isinstance(node, dict) or name not in node:
            return
        if len(steps) == 1:
            if each:
                if isinstance(node[name], list):
                    found.extend((node[name], i) for i in range(len(node[name])))
            else:
                found.append((node, name))
            return
        child = node[name]
        if each:
            for item in child if isinstance(child, list) else ():
                walk(item, steps[1:])
        else:
            walk(child, steps[1:])

    walk(body, _parse_path(path))
    return found


def drop_fields(body, paths) -> Tuple[object, List[str]]:
    dropped = []
    for path in paths:
        targets = resolve_path(body, path)
        if not targets:
            continue
        for container, key in sorted(targets, key=lambda t: t[1] if isinstance(t[1], int) else 0,
                                     reverse=True):
            del container[key]
        dropped.append(path)
    return body, dropped


# ----------------------------------------------------------------- images

def sniff(data: bytes) -> Optional[str]:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _strip_data_uri(value: str) -> str:
    if value.startswith('data:') and ',' in value[:100]:
        return value.split(',', 1)[1]
    return value


def extract_image(body, image_field: str) -> Union[Tuple[object, Optional[Tuple[str, str]]],
                                                   ToolFailure]:
    targets = resolve_path(body, image_field)
    if not targets:
        return body, None
    container, key = targets[0]
    value = container[key]
    if not isinstance(value, str) or not value:
        return body, None
    data = _strip_data_uri(value)
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return ToolFailure('backend_failure', f'{image_field} is not valid base64')
    if len(decoded) > MAX_IMAGE_BYTES:
        return ToolFailure('result_too_large', 'the image exceeds 2 MiB',
                           details={'bytes': len(decoded), 'limit': MAX_IMAGE_BYTES})
    del container[key]
    # the original base64 string (a `data:` URI prefix, if any, removed)
    return body, (data, sniff(decoded) or 'image/jpeg')


def _looks_like_image(value: str) -> bool:
    head = _strip_data_uri(value)[:16]
    try:
        return sniff(base64.b64decode(head + '=' * (-len(head) % 4))) is not None
    except (binascii.Error, ValueError):
        return False


def drop_oversized_images(body) -> Tuple[object, List[str]]:
    dropped: List[str] = []

    def walk(node, path):
        if isinstance(node, dict):
            for key in list(node):
                value = node[key]
                child = f'{path}.{key}' if path else key
                if isinstance(value, str):
                    if len(value) >= OVERSIZED_IMAGE_CHARS and _looks_like_image(value):
                        node[key] = f'<dropped: {len(value)} bytes image>'
                        if child not in dropped:
                            dropped.append(child)
                else:
                    walk(value, child)
        elif isinstance(node, list):
            child = f'{path}[]'
            for index, value in enumerate(node):
                if isinstance(value, str):
                    if len(value) >= OVERSIZED_IMAGE_CHARS and _looks_like_image(value):
                        node[index] = f'<dropped: {len(value)} bytes image>'
                        if child not in dropped:
                            dropped.append(child)
                else:
                    walk(value, child)

    walk(body, '')
    return body, dropped


# ------------------------------------------------------------------ scrub

def _file_name(value: str) -> str:
    stripped = value[len('file://'):] if value.startswith('file://') else value
    return posixpath.basename(stripped.rstrip('/'))


def _path_like(value: str) -> bool:
    return '/' in value and not URL_SCHEME_RE.match(value)


def _scrubbed(key: Optional[str], value: str) -> Optional[str]:
    if DEVICE_ROOT_RE.match(value):
        return _file_name(value)
    if key is not None and PATH_KEY_RE.match(key) and _path_like(value):
        return _file_name(value)
    return None


def scrub_paths(body) -> Tuple[object, List[str]]:
    """Property 19. Idempotent; returns the body (scrubbed in place) and the
    key paths that changed."""
    scrubbed: List[str] = []

    def note(path):
        if path not in scrubbed:
            scrubbed.append(path)

    def walk(node, path, key):
        if isinstance(node, dict):
            for child_key in list(node):
                value = node[child_key]
                child = f'{path}.{child_key}' if path else child_key
                if isinstance(value, str):
                    replacement = _scrubbed(child_key, value)
                    if replacement is not None and replacement != value:
                        node[child_key] = replacement
                        note(child)
                else:
                    walk(value, child, child_key)
        elif isinstance(node, list):
            child = f'{path}[]'
            for index, value in enumerate(node):
                if isinstance(value, str):
                    replacement = _scrubbed(key, value)
                    if replacement is not None and replacement != value:
                        node[index] = replacement
                        note(child)
                else:
                    walk(value, child, key)

    if isinstance(body, str):
        replacement = _scrubbed(None, body)
        if replacement is not None and replacement != body:
            return replacement, ['']
        return body, []
    walk(body, '', None)
    return body, scrubbed


# ------------------------------------------------------------ collections

def _items(spec, body):
    if spec.collection_key is None:
        return (None, body) if isinstance(body, list) else None
    if isinstance(body, dict) and isinstance(body.get(spec.collection_key), list):
        return body, body[spec.collection_key]
    return None


def _with_items(spec, container, items):
    if container is None:
        return items
    rebuilt = dict(container)
    rebuilt[spec.collection_key] = items
    return rebuilt


def _limit_default(spec) -> int:
    return spec.input_schema.get('properties', {}).get('limit', {}).get('default', 50)


def bound(spec, args: dict, body) -> Union[Tuple[object, bool], ToolFailure]:
    """(e): dispatcher slicing and ``MAX_RESULT_BYTES``."""
    found = _items(spec, body) if spec.result_shape == 'collection' else None
    if found is None:
        if encoded_size(body) > MAX_RESULT_BYTES:
            return ToolFailure('result_too_large', f'the result exceeds {MAX_RESULT_BYTES} bytes')
        return body, False
    container, items = found
    props = spec.input_schema.get('properties', {})
    if 'limit' in props and 'limit' not in spec.param_map:
        offset = int(args.get('offset', 0) or 0)
        items = items[offset:offset + int(args.get('limit', _limit_default(spec)))]
    result = _with_items(spec, container, items)
    if encoded_size(result) <= MAX_RESULT_BYTES:
        return result, False
    low, high = 0, len(items)
    while low < high:
        middle = (low + high + 1) // 2
        if encoded_size(_with_items(spec, container, items[:middle])) <= MAX_RESULT_BYTES:
            low = middle
        else:
            high = middle - 1
    return _with_items(spec, container, items[:low]), True


# ------------------------------------------------------------------- text

def tail_text(text: str, max_chars: int) -> Tuple[str, bool]:
    """The longest suffix of at most ``max_chars`` characters whose
    ``json.dumps({"text": ...})`` fits ``MAX_RESULT_BYTES - 1 000`` bytes."""
    budget = MAX_RESULT_BYTES - TEXT_MARGIN_BYTES
    candidate = text[-max_chars:] if max_chars < len(text) else text
    if len(json.dumps({'text': candidate}).encode('utf-8')) <= budget:
        return candidate, len(candidate) < len(text)
    low, high = 0, len(candidate)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps({'text': candidate[-middle:] if middle else ''}).encode('utf-8')) <= budget:
            low = middle
        else:
            high = middle - 1
    return (candidate[-low:] if low else ''), True


# ------------------------------------------------------------------ shape

def _resource_id(spec, body) -> Optional[str]:
    if spec.resource_id_from_body:
        return str(body) if isinstance(body, (str, int)) and not isinstance(body, bool) else None
    if spec.resource_id_path:
        targets = resolve_path(body, spec.resource_id_path) if isinstance(body, dict) else []
        if targets:
            container, key = targets[0]
            return str(container[key])
        return None
    if isinstance(body, dict):
        for key in RESOURCE_ID_KEYS:
            value = body.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                return str(value)
    return None


def shape_json(spec, args: dict, body) -> Union[Shaped, ToolFailure]:
    """(a)-(e) on a parsed 2xx JSON body."""
    shaped = Shaped()
    body = copy.deepcopy(body)
    body, shaped.dropped_fields = drop_fields(body, spec.drop_fields)
    if spec.image_field:
        extracted = extract_image(body, spec.image_field)
        if isinstance(extracted, ToolFailure):
            return extracted
        body, image = extracted
        if image is not None:
            shaped.images.append(image)
        if spec.result_constant is not None:
            if isinstance(body, dict):
                body.update(copy.deepcopy(spec.result_constant))
            else:
                body = copy.deepcopy(spec.result_constant)
    body, oversized = drop_oversized_images(body)
    shaped.dropped_fields.extend(p for p in oversized if p not in shaped.dropped_fields)
    shaped.resource_id = _resource_id(spec, body)
    if spec.scrub_paths:
        body, shaped.scrubbed_fields = scrub_paths(body)
    bounded = bound(spec, args, body)
    if isinstance(bounded, ToolFailure):
        return bounded
    shaped.result, shaped.truncated = bounded
    return shaped


def shape(spec, args: dict, response: BackendResponse) -> Union[Shaped, ToolFailure]:
    """Shape one Backend_API response for the caller."""
    if response.status >= 300:
        return failure_for(response)
    media = response.media_type
    if media.startswith('image/'):
        if len(response.body) > MAX_IMAGE_BYTES:
            return ToolFailure('result_too_large', 'the image exceeds 2 MiB',
                               details={'bytes': len(response.body), 'limit': MAX_IMAGE_BYTES})
        mime = 'image/jpeg' if media == 'image/jpg' else media
        return Shaped(result={'mime_type': mime, 'bytes': len(response.body)},
                      images=[(base64.b64encode(response.body).decode('ascii'), mime)])
    if media == 'text/plain':
        text = response.text()
        shaped = Shaped()
        if spec.text_tail_arg:
            default = spec.input_schema.get('properties', {}).get(
                spec.text_tail_arg, {}).get('default', len(text))
            text_out, cut = tail_text(text, int(args.get(spec.text_tail_arg, default)))
            if cut:
                shaped.truncated, shaped.total_chars = True, len(text)
            shaped.result = {'text': text_out}
            return shaped
        if encoded_size({'text': text}) > MAX_RESULT_BYTES:
            return ToolFailure('result_too_large', f'the result exceeds {MAX_RESULT_BYTES} bytes')
        shaped.result = {'text': text}
        return shaped
    if not response.body:
        return Shaped(result=None)
    try:
        body = response.json()
    except ValueError:
        text = response.text()
        if encoded_size({'text': text}) > MAX_RESULT_BYTES:
            return ToolFailure('result_too_large', f'the result exceeds {MAX_RESULT_BYTES} bytes')
        return Shaped(result={'text': text})
    return shape_json(spec, args, body)
