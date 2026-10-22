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
"""The Edge MCP child's server (design §5 ``child/server.py``, §7;
Requirements 8.1, 8.2, 8.4, 8.5, 9.2, 9.7, 10.7, 10.8, 12.5, 15.3, 16.1).

``make_server(options)`` is the low-level ``Server`` (``dda-edge-mcp``,
``tools/list`` cache hint 60 s private); ``build_app(options)`` wraps it in
the SDK's Streamable HTTP app at ``/mcp`` (JSON responses, stateless) with
explicit transport security: DNS-rebinding protection on, ``Host`` limited
to ``127.0.0.1:<port>`` and ``localhost:<port>`` (421 otherwise), ``Origin``
limited to ``--allowed-origins`` (empty by default, so any ``Origin`` is
403), and the request body capped at the proxy's 1 MiB (413). ``/healthz``
answers the supervisor's readiness probe.

``on_call_tool`` runs the §7 pipeline:

1. ``resolve_principal`` with the caller's own ``Authorization`` header
   (``GET /local-auth/whoami``): the Backend_API decides; the child never
   acts on its own authority;
2. lookup (a deprecated alias runs its spec and says so); a spec outside
   ``DELIVERED_PHASES`` is an unknown tool (-32602); a spec whose feature is
   not enabled is ``not_available_on_device``; schema validation
   (``invalid_input`` naming the property); ``authz.decide``;
3. ``call_backend`` with the same credential and ``X-Request-ID``;
4. one ``mcp.access`` record per completion (``child/logging.emit``);
5. ``child/shaping.shape``.
"""
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Sequence

import anyio
import jsonschema
from mcp.server.caching import CacheHint
from mcp.server.lowlevel.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult, ListToolsResult, TextContent
from mcp_types import CLIENT_INFO_META_KEY
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_edge import CHILD_PORT, DELIVERED_PHASES, __version__
from mcp_edge.child import authz, catalog, dispatch, principal, shaping
from mcp_edge.child import logging as access_log
from mcp_edge.child.dispatch import ToolFailure

logger = logging.getLogger(__name__)

SERVER_NAME = 'dda-edge-mcp'
LOOPBACK_HOST = '127.0.0.1'
# The /mcp proxy's cap (endpoints/mcp_proxy.MCP_PROXY_MAX_BODY), so a direct
# loopback caller cannot send more than the proxy forwards.
MAX_REQUEST_BODY_BYTES = 1024 * 1024
TOOLS_LIST_TTL_MS = 60_000
MAX_CLIENT_LABEL_CHARS = 128
INSTRUCTIONS = (
    'Tools of one DDA LocalServer. Every call runs as the caller: the '
    'Backend_API authorizes it with the credential this connection carries. '
    'Read-only tools are listed for every principal; mutating tools need the '
    "device's mutating switch, a local session and the Operator role or above.")

# Patched by tests; one JSON line on stdout per completion.
emit = access_log.emit


@dataclass(frozen=True)
class Options:
    """The child's command-line options (``__main__.build_parser``)."""
    backend_url: str = 'https://127.0.0.1:5443'
    port: int = CHILD_PORT
    server_version: str = __version__
    allowed_origins: Sequence[str] = ()
    features: Sequence[str] = ()
    mutating_enabled: bool = False
    delivered_phases: Sequence[int] = field(default=DELIVERED_PHASES)


def options_from(args) -> Options:
    return Options(backend_url=args.backend_url, port=args.port,
                   server_version=args.server_version,
                   allowed_origins=tuple(args.allowed_origins),
                   features=tuple(args.features),
                   mutating_enabled=bool(args.mutating_enabled))


def transport_security(options: Options) -> TransportSecuritySettings:
    """Explicit settings: left None, the SDK's loopback defaults would admit
    any ``http://127.0.0.1:*`` / ``http://localhost:*`` Origin."""
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f'{LOOPBACK_HOST}:{options.port}', f'localhost:{options.port}'],
        allowed_origins=list(options.allowed_origins))


# ----------------------------------------------------------------- helpers

def _printable(text: str) -> str:
    cleaned = ''.join(c for c in text if 32 <= ord(c) < 127)
    return cleaned[:MAX_CLIENT_LABEL_CHARS] or 'unknown'


def _label(info) -> Optional[str]:
    if info is None:
        return None
    if isinstance(info, dict):
        name, version = info.get('name'), info.get('version')
    else:
        name, version = getattr(info, 'name', None), getattr(info, 'version', None)
    if not name:
        return None
    return f'{name}/{version or "?"}'


def client_label(ctx) -> str:
    """The MCP client identity (the portal's rule, review-2 N4): the
    per-request ``_meta`` client info, else the handshake-era
    ``client_params.client_info``, else 'unknown'; printable ASCII, at most
    128 characters."""
    label = None
    meta = getattr(ctx, 'meta', None)
    try:
        if meta is not None:
            label = _label(meta.get(CLIENT_INFO_META_KEY) if hasattr(meta, 'get')
                           else getattr(meta, CLIENT_INFO_META_KEY, None))
    except Exception:  # noqa: BLE001 - a malformed _meta is just unknown
        label = None
    if label is None:
        try:
            params = ctx.session.client_params
            label = _label(params.client_info) if params is not None else None
        except Exception:  # noqa: BLE001
            label = None
    return _printable(label) if label else 'unknown'


def _header(ctx, name: str) -> Optional[str]:
    request = getattr(ctx, 'request', None)
    headers = getattr(request, 'headers', None)
    if headers is None:
        return None
    value = headers.get(name)
    return value if value else None


def validation_failure(spec, args) -> Optional[ToolFailure]:
    """``invalid_input`` naming the first failing property, or None. The
    message names the property and the failed keyword, never the value."""
    validator = jsonschema.Draft202012Validator(spec.input_schema)
    errors = sorted(validator.iter_errors(args), key=lambda e: (list(e.path), e.validator))
    if not errors:
        return None
    error = errors[0]
    if error.path:
        name = str(error.path[0])
    elif error.validator == 'required':
        missing = [r for r in error.validator_value if r not in (args or {})]
        name = missing[0] if missing else 'arguments'
    elif error.validator == 'additionalProperties':
        extra = [k for k in (args or {}) if k not in spec.input_schema.get('properties', {})]
        name = extra[0] if extra else 'arguments'
    else:
        name = 'arguments'
    if error.validator == 'required':
        return ToolFailure('invalid_input', f'{name} is required')
    if error.validator == 'additionalProperties':
        return ToolFailure('invalid_input', f'{name} is not an argument of {spec.name}')
    return ToolFailure('invalid_input', f'{name} fails {error.validator}')


PAGINATION_ARGS = ('limit', 'offset', 'page')


def with_forwarded_defaults(spec, args: dict) -> dict:
    """A forwarded pagination argument the caller left out travels with its
    schema default, so the route serves the page the schema describes
    (``edge_workflow_images_list`` without ``limit`` asks for
    ``maxResults=2&startingPoint=0``)."""
    properties = spec.input_schema.get('properties', {})
    filled = dict(args)
    for name in PAGINATION_ARGS:
        if name in spec.param_map and name not in filled and 'default' in properties.get(name, {}):
            filled[name] = properties[name]['default']
    return filled


def _outcome(failure: Optional[ToolFailure]) -> str:
    if failure is None:
        return 'success'
    if failure.category == 'not_authenticated':
        return 'not_authenticated'
    if failure.category in ('not_authorized', 'mutating_tools_disabled',
                            'confirmation_required'):
        return 'denied'
    return 'error'


# ------------------------------------------------------------------ server

def make_server(options: Options, specs: Optional[Sequence] = None) -> Server:
    """The low-level ``Server`` of the child. ``specs`` replaces the catalog
    (tests only)."""
    specs = catalog.CATALOG if specs is None else tuple(specs)
    server_label = f'{SERVER_NAME}@{options.server_version}'

    async def on_list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(tools=catalog.tools_for(
            delivered_phases=options.delivered_phases,
            mutating_enabled=options.mutating_enabled,
            features=options.features, catalog=specs))

    async def on_call_tool(ctx, params) -> CallToolResult:
        started = time.monotonic()
        name = params.name
        args = dict(params.arguments or {})
        request_id = _header(ctx, 'x-request-id') or uuid.uuid4().hex
        authorization = _header(ctx, 'authorization')
        mcp_client = client_label(ctx)
        resolved = None
        status = None
        spec = catalog.find(name, specs)

        def finish(failure: Optional[ToolFailure], result: Optional[CallToolResult] = None):
            emit(access_log.build_record(
                tool=spec.name if spec is not None else name,
                outcome=_outcome(failure),
                principal=resolved if isinstance(resolved, principal.Principal) else None,
                request_id=request_id, mcp_server=server_label, mcp_client=mcp_client,
                category=failure.category if failure is not None else None,
                status=status, duration_ms=(time.monotonic() - started) * 1000))
            return failure.to_result(request_id) if failure is not None else result

        if spec is None or spec.phase not in options.delivered_phases:
            emit(access_log.build_record(
                tool=name, outcome='error', request_id=request_id,
                mcp_server=server_label, mcp_client=mcp_client, category='unknown_tool',
                duration_ms=(time.monotonic() - started) * 1000))
            raise MCPError(code=INVALID_PARAMS, message=f'Unknown tool: {name}')

        resolved = await anyio.to_thread.run_sync(
            principal.resolve_principal, authorization, request_id)
        if isinstance(resolved, ToolFailure):
            status = resolved.status
            return finish(resolved)
        if spec.feature is not None and spec.feature not in set(options.features):
            return finish(ToolFailure(
                'not_available_on_device',
                f'{spec.name} needs the {spec.feature} feature, which is not enabled on '
                'this device'))
        invalid = validation_failure(spec, args)
        if invalid is not None:
            logger.info('invalid_input for %s: %s', spec.name, invalid.message)
            return finish(invalid)
        decision = authz.decide(spec, resolved, options.mutating_enabled, args)
        if isinstance(decision, authz.Denied):
            return finish(ToolFailure(decision.category, decision.message))

        credential = dispatch.credential_from_header(authorization)
        args = with_forwarded_defaults(spec, args)
        response = await anyio.to_thread.run_sync(
            dispatch.call_backend, spec, args, credential, request_id)
        if isinstance(response, ToolFailure):
            status = response.status
            return finish(response)
        status = response.status
        shaped = shaping.shape(spec, args, response)
        if isinstance(shaped, ToolFailure):
            return finish(shaped)
        result = shaped.to_result(request_id)
        if name != spec.name:
            notice = (f'{name} is a deprecated alias of {spec.name}; removed in '
                      f'{spec.alias_removed_in}')
            result.structured_content = dict(result.structured_content or {},
                                             deprecation=notice)
            result.content.append(TextContent(type='text', text=notice))
        return finish(None, result)

    return Server(
        SERVER_NAME,
        version=options.server_version,
        instructions=INSTRUCTIONS,
        cache_hints={'tools/list': CacheHint(ttl_ms=TOOLS_LIST_TTL_MS, scope='private')},
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool)


async def healthz(_request: Request) -> JSONResponse:
    return JSONResponse({'status': 'ok'})


def build_app(options: Options, server: Optional[Server] = None,
              security: Optional[TransportSecuritySettings] = 'default'):
    """The child's Starlette app. ``security=None`` is the tests' control
    case (the SDK defaults); production always passes the explicit settings."""
    server = server if server is not None else make_server(options)
    settings = transport_security(options) if security == 'default' else security
    return server.streamable_http_app(
        streamable_http_path='/mcp',
        json_response=True,
        stateless_http=True,
        host=LOOPBACK_HOST,
        transport_security=settings,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
        custom_starlette_routes=[Route('/healthz', healthz, methods=['GET'])])


def build_server(args):
    """``__main__``'s entry: configure the Backend_API client, return the app."""
    options = options_from(args)
    dispatch.configure(options.backend_url)
    return build_app(options)
