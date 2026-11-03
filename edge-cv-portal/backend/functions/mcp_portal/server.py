"""The per-view MCP `Server`s of the Portal_MCP_Server (design §2 `server.py`
row, §4 "Request pipeline", D8; Requirements 1.2, 1.7, 5.3, 5.4, 6.2, 14.4,
15.3, 17.3).

`build_server(view)` returns a low-level `Server` named
`dda-portal-mcp/<view>` (`dda-portal-mcp` for the aggregate `/mcp`
endpoint, `view=None`) with the `tools/list` cache hint (300 s, private).
Its `on_call_tool` runs the §4 pipeline:

1. authenticate: the SDK's `get_access_token()`; None (only an in-memory
   client gets that far without a token) is `not_authenticated`;
2. lookup: the name or a `deprecated_alias`; an unknown name, or a spec
   whose phase is not in `DELIVERED_PHASES` (17.3, step 4's phase gate,
   applied here so an undelivered tool answers exactly like a nonexistent
   one), is the protocol error -32602;
3. validate: the input schema (`invalid_input` naming the property, never
   the value), then the destructive `confirm` rule (`confirmation_required`);
4. gate: a mutating spec with the switch off is `mutating_tools_disabled`;
5. authorize: `authz.authorize` (it writes the marked denial /
   unavailability entry);
6. dispatch: `dispatch.invoke` with the `mcp` block (`client_label(ctx)`);
7. audit: `shaping.shape` runs the D36 binding check first, then
   `audit.write_envelope`;
8. log: one `telemetry.log_tool_completion` line per completion;
9. shape: the `CallToolResult` (a refused body is `not_found`).

`portal_admin_whoami` is the one local spec: it is answered from the real
resolver and never dispatched.

Steps 3-9 live in `mcp_portal.pipeline.call_tool` (portal-ai-assistant
design §1), shared with the Portal assistant; this module authenticates,
looks the tool up and renders the `ToolOutcome` as a `CallToolResult`. The
helper names below (`find_spec`, `validation_failure`, `expected_confirm`,
`confirmation_failure`, `whoami`) are re-exported from the pipeline so the
tests and the catalog documentation keep one import path.
"""
import logging
import os
import uuid
from typing import Optional

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.caching import CacheHint
from mcp.server.lowlevel.server import Server
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult, ListToolsResult, TextContent
from mcp_types import CLIENT_INFO_META_KEY

from mcp_portal import DELIVERED_PHASES, VIEWS, __version__
from mcp_portal import catalog, pipeline
from mcp_portal.dispatch import McpContext
from mcp_portal.errors import ToolFailure
from mcp_portal.pipeline import (  # noqa: F401 - re-exported for tests and docs
    confirmation_failure, expected_confirm, find_spec, validation_failure, whoami)
from mcp_portal.principal import principal_from_access_token

logger = logging.getLogger(__name__)

SERVER_NAME = 'dda-portal-mcp'
TOOLS_LIST_TTL_MS = 300_000
MAX_CLIENT_LABEL_CHARS = 128
UNKNOWN = 'unknown'

VIEW_TEXT = {
    None: 'every area of the Portal',
    'data': 'datasets, captures, labeling and synthetic data',
    'models': 'models, training, compilation and packaging',
    'devices': 'devices, cameras, deployments and device logs',
    'workflows': 'workflows, their validation, testing and packaging',
    'admin': 'Use_Cases, users, roles and the audit log',
}


def server_version() -> str:
    return os.environ.get('MCP_SERVER_VERSION') or __version__


def mutating_enabled() -> bool:
    """The deploy-time switch: only a trimmed, lower-cased `true` is on."""
    return os.environ.get('MCP_MUTATING_TOOLS_ENABLED', 'false').strip().lower() == 'true'


def server_name(view: Optional[str]) -> str:
    return f'{SERVER_NAME}/{view}' if view else SERVER_NAME


def instructions_for(view: Optional[str]) -> str:
    return (f'Tools of the DDA Portal for {VIEW_TEXT[view]}. Every call runs as the '
            'signed-in Portal user under the same role checks as the Portal; '
            'Use_Case-scoped tools take usecase_id. Read-only tools are listed '
            'unless the deployment enables mutating tools.')


# ----------------------------------------------------------------- helpers

def _printable(text: str) -> str:
    cleaned = ''.join(c for c in text if 32 <= ord(c) < 127)
    return cleaned[:MAX_CLIENT_LABEL_CHARS] or UNKNOWN


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
    """The MCP client identity (review-2 N4; the same rule as the edge
    child): the per-request `_meta` client info, else the handshake-era
    `client_params.client_info`, else 'unknown'; printable ASCII, at most
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
    return _printable(label) if label else UNKNOWN


def _request_identity(ctx):
    """(source IP, user agent) of the HTTP request: the HTTP API event's
    `requestContext.http` when Mangum carries one, else the request itself."""
    request = getattr(ctx, 'request', None)
    scope = getattr(request, 'scope', None) or {}
    http = ((scope.get('aws.event') or {}).get('requestContext') or {}).get('http') or {}
    source_ip = http.get('sourceIp')
    user_agent = http.get('userAgent')
    if source_ip is None and scope.get('client'):
        source_ip = scope['client'][0]
    if user_agent is None and request is not None:
        user_agent = request.headers.get('user-agent')
    return source_ip, user_agent


# ------------------------------------------------------------------ server

def build_server(view: Optional[str], specs=None) -> Server:
    """The low-level `Server` of one endpoint (`view=None`: `/mcp`).
    `specs` replaces the catalog (tests only)."""
    if view is not None and view not in VIEWS:
        raise ValueError(f'unknown view: {view!r}')
    specs = catalog.CATALOG if specs is None else tuple(specs)
    label = f'{server_name(view)}@{server_version()}'

    async def on_list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(tools=catalog.tools_for_view(
            view, delivered_phases=DELIVERED_PHASES, mutating_enabled=mutating_enabled(),
            catalog=specs))

    async def on_call_tool(ctx, params) -> CallToolResult:
        name = params.name
        args = dict(params.arguments or {})
        request_id = str(ctx.request_id) if getattr(ctx, 'request_id', None) is not None \
            else uuid.uuid4().hex

        # 1. authenticate
        token = get_access_token()
        if token is None:
            return ToolFailure('not_authenticated', 'no verified access token').to_result(
                request_id)
        principal = await anyio.to_thread.run_sync(principal_from_access_token, token)
        mcp_client = client_label(ctx)

        # 2. lookup (and the phase gate of step 4)
        found = find_spec(name, specs)
        if found is None or found[0].phase not in DELIVERED_PHASES \
                or (view is not None and found[0].domain != view):
            logger.info('Unknown or undelivered tool %r on %s', name, label)
            raise MCPError(code=INVALID_PARAMS, message=f'Unknown tool: {name}')
        spec, by_alias = found

        source_ip, user_agent = _request_identity(ctx)
        usecase_id = args.get('usecase_id') if isinstance(args.get('usecase_id'), str) else None
        mcp_ctx = McpContext(request_id=request_id, tool=spec.name, server=label,
                             client=mcp_client, source_ip=source_ip, user_agent=user_agent,
                             principal_email=principal.email)

        outcome = await anyio.to_thread.run_sync(
            lambda: pipeline.call_tool(spec, args, principal, mcp_ctx,
                                       mutating_enabled=mutating_enabled(), view=view,
                                       server_version=server_version()))
        if outcome.failure is not None:
            return outcome.failure.to_result(request_id)
        result = outcome.shaped.to_result(request_id)
        if by_alias:
            notice = (f'{name} is a deprecated alias of {spec.name}; removed in '
                      f'{spec.alias_removed_in}')
            result.structured_content = dict(result.structured_content or {},
                                             deprecation=notice)
            result.content.append(TextContent(type='text', text=notice))
        return result

    return Server(
        server_name(view),
        version=server_version(),
        instructions=instructions_for(view),
        cache_hints={'tools/list': CacheHint(ttl_ms=TOOLS_LIST_TTL_MS, scope='private')},
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool)
