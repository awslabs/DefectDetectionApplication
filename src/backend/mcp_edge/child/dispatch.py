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
"""The child's call to the Backend_API (design §5 ``child/dispatch.py``, §7
step 3, D32; Requirements 8.3, 8.6, 9.1, 9.7, 11.3).

``call_backend(spec, args, credential, request_id)`` sends one request to the
Backend_API route of ``spec`` with the caller's own credential, through one
module-level ``httpx2`` client from ``make_client(backend_url)``:

* exactly one attempt (``HTTPTransport(retries=0)``, D32) and a per-spec
  read timeout (``spec.timeout_s``), so a slow route is not cut off early;
* path parameters percent-encoded as one segment each, after the schema has
  refused ``/`` and the values ``.``/``..``; query parameters for GET routes;
  for body routes ``{**resolve_constants(spec.body_constants), **mapped}``
  (``NOW_EPOCH_S`` becomes ``int(time.time())``);
* ``edge_static_image_pin`` sends ``multipart/form-data``; its base64 is
  decoded (``validate=True``) before any request, and an undecodable value
  is ``invalid_input`` with nothing dispatched;
* ``credential_in_query`` specs (the five image routes of
  ``download_file.py``) send the credential as the ``token`` query
  parameter, which those routes read; every other spec sends
  ``Authorization: Bearer``. ``X-Request-ID`` is always sent;
* the URL is never formatted into a log record or an exception message:
  ``_safe_error`` names the spec, the exception class and the status only.

A read timeout or transport error on a mutating spec answers "the operation
may have been applied; call <status_tool> before retrying" with
``effect: "unknown"``, as the portal does. Backend statuses map to the
portal's category vocabulary (``map_backend_status``).
"""
import base64
import binascii
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Union
from urllib.parse import quote

import httpx2

from mcp_edge.child.catalog import NOW_EPOCH_S, SERVER_CONSUMED_ARGS  # noqa: F401 (re-export)

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 2.0
DEFAULT_READ_TIMEOUT_S = 60.0
WRITE_TIMEOUT_S = 10.0
POOL_TIMEOUT_S = 2.0

# The one spec whose body is multipart/form-data: (bytes argument, name argument).
MULTIPART_SPECS = {'edge_static_image_pin': ('image_base64', 'file_name')}

_TOKEN_RE = re.compile(r'token=[^&\s]*', re.IGNORECASE)
_TRACEBACK_RE = re.compile(r'Traceback|File "')
# A credential ends where the next one starts ("Bearer", or a JWT's "eyJ"), so
# two adjacent ones are both removed.
_BEARER_RE = re.compile(r'Bearer\s+(?:(?!Bearer)[A-Za-z0-9._~+/=-])+', re.IGNORECASE)
_JWT_RE = re.compile(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.(?:(?!eyJ)[A-Za-z0-9_-])*')
MAX_MESSAGE_CHARS = 1000


# ----------------------------------------------------------------- results

class ToolFailure(Exception):
    """A tool call that ends in an error result (Data Models "Tool error
    result (both servers)")."""

    def __init__(self, category: str, message: str, status: Optional[int] = None,
                 effect: Optional[str] = None, status_tool: Optional[str] = None,
                 details: Optional[dict] = None):
        super().__init__(f'{category}: {message}')
        self.category = category
        self.message = message
        self.status = status
        self.effect = effect
        self.status_tool = status_tool
        self.details = dict(details or {})

    @property
    def text(self) -> str:
        return f'{self.category}: {self.message}'

    def structured(self, request_id: Optional[str]) -> dict:
        content = {'category': self.category, 'message': self.message,
                   'request_id': request_id, 'status': self.status}
        if self.effect is not None:
            content['effect'] = self.effect
        if self.status_tool is not None:
            content['status_tool'] = self.status_tool
        content.update(self.details)
        return content

    def to_result(self, request_id: Optional[str]):
        from mcp.types import CallToolResult, TextContent

        return CallToolResult(is_error=True,
                              content=[TextContent(type='text', text=self.text)],
                              structured_content=self.structured(request_id))


def effect_unknown_failure(spec, category: str, message: str) -> ToolFailure:
    if spec.read_only:
        return ToolFailure(category, message)
    return ToolFailure(category,
                       f'the operation may have been applied; call {spec.status_tool} '
                       'before retrying', effect='unknown', status_tool=spec.status_tool)


def category_for(status: int) -> str:
    if status in (400, 422):
        return 'invalid_input'
    if status == 401:
        return 'not_authenticated'
    if status == 403:
        return 'not_authorized'
    if status == 404:
        return 'not_found'
    if status == 409:
        return 'conflict'
    if status == 429:
        return 'throttled'
    if 400 <= status < 500:
        return 'invalid_input'
    return 'backend_failure'


def scrub(text, category: str) -> str:
    if not isinstance(text, str) or not text.strip() or _TRACEBACK_RE.search(text):
        return category
    text = _BEARER_RE.sub('[removed]', text)
    text = _JWT_RE.sub('[removed]', text)
    text = _TOKEN_RE.sub('token=[removed]', text)
    return text[:MAX_MESSAGE_CHARS]


def _error_text(body) -> Optional[str]:
    if isinstance(body, dict):
        for key in ('error', 'detail', 'message'):
            value = body.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                nested = value.get('message') or value.get('error')
                if isinstance(nested, str):
                    return nested
            if isinstance(value, list) and value and isinstance(value[0], dict):
                nested = value[0].get('msg')
                if isinstance(nested, str):
                    return nested
    if isinstance(body, str):
        return body
    return None


def map_backend_status(status: int, body) -> ToolFailure:
    category = category_for(int(status))
    return ToolFailure(category, scrub(_error_text(body), category), status=int(status))


@dataclass(frozen=True)
class BackendResponse:
    status: int
    content_type: str
    body: bytes = field(repr=False)

    @property
    def media_type(self) -> str:
        return self.content_type.split(';', 1)[0].strip().lower()

    def json(self):
        return json.loads(self.body.decode('utf-8'))

    def text(self) -> str:
        return self.body.decode('utf-8', errors='replace')


# ------------------------------------------------------------------ client

_CLIENT: Optional[httpx2.Client] = None
_CLIENT_LOCK = threading.Lock()


def make_client(backend_url: str) -> httpx2.Client:
    """The child's one Backend_API client. TLS verification is off because
    the backend is always ``https://127.0.0.1:5443`` (or plaintext loopback),
    whose certificate is the station's self-signed one; the healthcheck uses
    the same setting."""
    return httpx2.Client(
        base_url=backend_url,
        verify=False,  # nosec B501 - loopback Backend_API only (healthcheck.py precedent)
        timeout=httpx2.Timeout(connect=CONNECT_TIMEOUT_S, read=DEFAULT_READ_TIMEOUT_S,
                               write=WRITE_TIMEOUT_S, pool=POOL_TIMEOUT_S),
        transport=httpx2.HTTPTransport(retries=0, verify=False))  # nosec B501


def configure(backend_url: str) -> httpx2.Client:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            _CLIENT.close()
        _CLIENT = make_client(backend_url)
        return _CLIENT


def client() -> httpx2.Client:
    if _CLIENT is None:
        raise RuntimeError('mcp_edge.child.dispatch.configure() was not called')
    return _CLIENT


def set_client(value: Optional[httpx2.Client]) -> None:
    """Install a client (tests: an ``httpx2.MockTransport`` client)."""
    global _CLIENT
    with _CLIENT_LOCK:
        _CLIENT = value


# ----------------------------------------------------------------- request

def resolve_constants(constants: Dict[str, object]) -> Dict[str, object]:
    return {key: (int(time.time()) if value == NOW_EPOCH_S else value)
            for key, value in constants.items()}


def _query_value(value) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, separators=(',', ':'))


def _rename_items(spec, argument: str, value):
    renames = spec.item_param_map.get(argument)
    if not renames or not isinstance(value, list):
        return value
    return [{renames.get(k, k): v for k, v in item.items()} if isinstance(item, dict) else item
            for item in value]


@dataclass
class Request:
    method: str
    path: str
    params: Dict[str, str]
    headers: Dict[str, str]
    json: Optional[dict] = None
    files: Optional[dict] = None


def build_request(spec, args: dict, credential: Optional[str],
                  request_id: str) -> Union[Request, ToolFailure]:
    """The Backend_API request of one call (pure, apart from the clock of
    ``NOW_EPOCH_S``)."""
    path_values, query, body = {}, {}, {}
    multipart = MULTIPART_SPECS.get(spec.name)
    for argument, value in args.items():
        if argument in SERVER_CONSUMED_ARGS and argument not in spec.param_map:
            continue
        if multipart and argument in multipart:
            continue
        target = spec.param_map.get(argument, argument)
        location = spec.location_of(argument)
        if location == 'path':
            path_values[target] = value
        elif location == 'query':
            query[target] = _query_value(value)
        else:
            body[target] = _rename_items(spec, argument, value)
    path = spec.resource
    for name, value in path_values.items():
        encoded = quote(str(value), safe='')
        path = path.replace('{' + name + '}', encoded).replace('{' + name + '+}', encoded)
    headers = {'X-Request-ID': request_id}
    if spec.credential_in_query:
        if credential:
            query['token'] = credential
    elif credential:
        headers['Authorization'] = f'Bearer {credential}'
    request = Request(spec.method.upper(), path, query, headers)
    if multipart:
        data_arg, name_arg = multipart
        try:
            image_bytes = base64.b64decode(args.get(data_arg, ''), validate=True)
        except (binascii.Error, ValueError):
            return ToolFailure('invalid_input', f'{data_arg} is not valid base64')
        request.files = {'file': (args.get(name_arg), image_bytes, 'application/octet-stream')}
        return request
    if request.method in ('POST', 'PUT', 'PATCH') or body:
        request.json = {**resolve_constants(spec.body_constants), **body}
    return request


def _safe_error(spec, error: Exception, status: Optional[int] = None) -> str:
    """Spec name, exception class and status only; never the URL."""
    text = f'{spec.name}: {type(error).__name__}'
    if status is not None:
        text += f' (status {status})'
    return _TOKEN_RE.sub('token=[removed]', text)


def call_backend(spec, args: dict, credential: Optional[str],
                 request_id: str) -> Union[BackendResponse, ToolFailure]:
    request = build_request(spec, args, credential, request_id)
    if isinstance(request, ToolFailure):
        return request
    timeout = httpx2.Timeout(connect=CONNECT_TIMEOUT_S, read=spec.timeout_s,
                             write=WRITE_TIMEOUT_S, pool=POOL_TIMEOUT_S)
    try:
        response = client().request(
            request.method, request.path, params=request.params or None,
            headers=request.headers, json=request.json, files=request.files,
            timeout=timeout)
    except httpx2.TimeoutException as error:
        logger.warning('Backend call timed out: %s', _safe_error(spec, error))
        if isinstance(error, (httpx2.ConnectTimeout, httpx2.PoolTimeout)):
            return effect_unknown_failure(spec, 'backend_failure', 'the Backend_API is unreachable')
        return effect_unknown_failure(spec, 'timeout', 'the backing operation timed out')
    except httpx2.HTTPError as error:
        logger.warning('Backend call failed: %s', _safe_error(spec, error))
        return effect_unknown_failure(spec, 'backend_failure', 'backing call failed')
    return BackendResponse(response.status_code, response.headers.get('content-type', ''),
                           response.content)


def credential_from_header(authorization: Optional[str]) -> Optional[str]:
    """The bearer credential of an ``Authorization`` header, or None."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(' ')
    if scheme.lower() != 'bearer' or not value.strip():
        return None
    return value.strip()


def failure_for(response: BackendResponse) -> ToolFailure:
    """The error result of a non-2xx Backend_API response."""
    try:
        body = response.json()
    except ValueError:
        body = response.text()
    return map_backend_status(response.status, body)


__all__ = ('BackendResponse', 'NOW_EPOCH_S', 'Request', 'ToolFailure', 'build_request',
           'call_backend', 'client', 'configure', 'credential_from_header',
           'effect_unknown_failure', 'failure_for', 'make_client', 'map_backend_status',
           'resolve_constants', 'set_client')
