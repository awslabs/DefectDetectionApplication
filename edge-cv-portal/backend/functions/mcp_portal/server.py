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

`portal_admin_whoami` is the one local spec: it is answered here from the
real resolver and never dispatched.
"""
import dataclasses
import logging
import os
import time
import uuid
from typing import Optional

import anyio
import jsonschema
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.caching import CacheHint
from mcp.server.lowlevel.server import Server
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult, ListToolsResult, TextContent
from mcp_types import CLIENT_INFO_META_KEY

from mcp_portal import DELIVERED_PHASES, VIEWS, __version__
from mcp_portal import audit, authz, catalog, dispatch, shaping, telemetry
from mcp_portal.dispatch import BackendResponse, McpContext
from mcp_portal.errors import ToolFailure
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


def find_spec(name: str, specs) -> Optional[tuple]:
    """(spec, called_by_alias) for a tool name, or None."""
    for spec in specs:
        if spec.name == name:
            return spec, False
    for spec in specs:
        if spec.deprecated_alias and spec.deprecated_alias == name:
            return spec, True
    return None


def validation_failure(spec, args) -> Optional[ToolFailure]:
    """`invalid_input` naming the first failing property, or None. The
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


def _target_value(args: dict, target: str):
    if target.endswith('[0]'):
        value = args.get(target[:-3])
        return value[0] if isinstance(value, list) and value else None
    return args.get(target)


def expected_confirm(spec, args: dict) -> Optional[str]:
    """The value `confirm` must equal (D9; review-3 N2, review-9 N7)."""
    target = spec.confirm_target
    if isinstance(target, tuple):
        for branch, branch_target in zip(spec.input_schema.get('oneOf', ()), target):
            if all(name in args for name in branch.get('required', ())):
                return _target_value(args, branch_target)
        return None
    if target:
        return _target_value(args, target)
    return spec.confirm_literal


def confirmation_failure(spec, args: dict) -> Optional[ToolFailure]:
    if not spec.destructive:
        return None
    expected = expected_confirm(spec, args)
    confirm = args.get('confirm')
    if expected is None or confirm is None or str(confirm) != str(expected):
        return ToolFailure('confirmation_required',
                           f'{spec.name} is destructive; pass confirm={catalog.confirm_text(spec)}')
    return None


def whoami(principal):
    """`portal_admin_whoami`: the caller as the real resolver sees it.

    A registry read failure under `PORTAL_REGISTRY_ENFORCED` returns the
    `authorization_unavailable` tool failure (Error Handling,
    `RegistryUnavailable` row), so the call still gets its marked audit
    entry and its `mcp_tool` line.
    """
    import shared_utils  # the SharedLayer; imported where it is needed

    user = principal.user
    try:
        role = shared_utils.rbac_manager.get_user_role(principal.sub, 'global',
                                                       user_info=user)
        usecases = shared_utils.rbac_manager.get_accessible_usecases(principal.sub, user)
    except shared_utils.RegistryUnavailable as error:
        logger.error('Portal_Identity registry unavailable for MCP tool '
                     'portal_admin_whoami: %s', error, exc_info=True)
        return ToolFailure('authorization_unavailable',
                           'the Portal identity registry is unavailable; retry')
    body = {'user_id': principal.sub, 'username': principal.username,
            'email': principal.email, 'global_role': role.value if role else None,
            'usecases': sorted(usecases or [])}
    return BackendResponse(200, body, {}, None)


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
        started = time.monotonic()
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

        def log(tool, category, status):
            telemetry.log_tool_completion(
                principal=principal, tool=tool, view=view, category=category,
                status=status, duration_ms=(time.monotonic() - started) * 1000,
                request_id=request_id, client=mcp_client, server_version=server_version())

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

        def finish(failure: Optional[ToolFailure], dispatched: bool, status=None,
                   shaped=None, event=None, identity_source=None):
            outcome = audit.Outcome(
                dispatched=dispatched, category=failure.category if failure else None,
                binding_refused=bool(shaped is not None and shaped.binding_refused),
                args=args, usecase_id=usecase_id)
            ctx_for_audit = dataclasses.replace(mcp_ctx, identity_source=identity_source)
            audit.write_envelope(spec, principal, ctx_for_audit, outcome,
                                 (time.monotonic() - started) * 1000, status,
                                 shaped.resource_id if shaped is not None else None,
                                 mcp_event=event)
            log(spec.name, failure.category if failure else None, status)
            result = failure.to_result(request_id) if failure is not None \
                else shaped.to_result(request_id)
            if by_alias and failure is None:
                notice = (f'{name} is a deprecated alias of {spec.name}; removed in '
                          f'{spec.alias_removed_in}')
                result.structured_content = dict(result.structured_content or {},
                                                 deprecation=notice)
                result.content.append(TextContent(type='text', text=notice))
            return result

        # 3. validate
        failure = validation_failure(spec, args) or confirmation_failure(spec, args)
        if failure is not None:
            logger.info('%s for %s: %s', failure.category, spec.name, failure.message)
            return finish(failure, dispatched=False)
        # 4. gate
        if not spec.read_only and not mutating_enabled():
            return finish(ToolFailure('mutating_tools_disabled',
                                      f'{spec.name} is mutating and mutating tools are '
                                      'disabled on this deployment'), dispatched=False)
        # 5. authorize
        event = dispatch.build_backend_event(spec, args, principal, mcp_ctx) \
            if spec.backend else None
        decision = await anyio.to_thread.run_sync(
            lambda: authz.authorize(spec, principal, usecase_id, mcp_event=event))
        if isinstance(decision.decision, authz.Denied):
            log(spec.name, 'not_authorized', None)
            return ToolFailure('not_authorized',
                               f'{spec.name} requires one of {list(spec.permissions)} '
                               f'at scope {decision.scope}', status=403).to_result(request_id)
        if isinstance(decision.decision, authz.Unavailable):
            log(spec.name, 'authorization_unavailable', None)
            return ToolFailure('authorization_unavailable',
                               'the Portal identity registry is unavailable; retry'
                               ).to_result(request_id)
        identity_source = decision.identity_source
        # 6. dispatch
        if spec.backend is None:
            response = await anyio.to_thread.run_sync(whoami, principal)
            event = None
        else:
            event = dispatch.build_backend_event(
                spec, args, principal,
                dataclasses.replace(mcp_ctx, identity_source=identity_source))
            response = await anyio.to_thread.run_sync(dispatch.invoke, spec, event)
        if isinstance(response, ToolFailure):
            return finish(response, dispatched=True, status=None, event=event,
                          identity_source=identity_source)
        # 7-9. binding check and shaping, audit, log, result
        shaped = shaping.shape(spec, args, response)
        return finish(shaped.failure, dispatched=True, status=response.status,
                      shaped=shaped, event=event, identity_source=identity_source)

    return Server(
        server_name(view),
        version=server_version(),
        instructions=instructions_for(view),
        cache_hints={'tools/list': CacheHint(ttl_ms=TOOLS_LIST_TTL_MS, scope='private')},
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool)
