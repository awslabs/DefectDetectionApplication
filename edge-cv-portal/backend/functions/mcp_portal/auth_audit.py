"""Audit of rejected access tokens that name a verifiable principal (4.7).

`AuthFailureAuditMiddleware` is pure ASGI and is the OUTERMOST middleware of
`build_asgi_app()` (the first entry of its `middleware=` list; Starlette
wraps the list in reverse). It resets `CURRENT_AUTH_FAILURE` when a request
starts, lets the request run through `AuthenticationMiddleware` (where
`CognitoAccessTokenVerifier` may record a failure), and when the response
status is 401 and a failure with a `sub` was recorded, writes one marked
audit entry:

    action='authentication_failed', user_id=<sub>, resource_type='mcp_server',
    resource_id=<endpoint URL>, result='denied',
    details={reason, client_id, token_use},
    channel='mcp', mcp_tool='-', mcp_client=<User-Agent>,
    mcp_request_id=<API Gateway request id>

`sourceIp`/`userAgent` come from `scope['aws.event']['requestContext']['http']`
(the HTTP API payload v2 event Mangum passes through) and the request id
from `scope['aws.event']['requestContext']`. An audit-write failure is
swallowed and logged at ERROR; the 401 has already been sent.
"""
import logging
import os
from typing import Optional
from urllib.parse import urlsplit

from mcp_portal import VIEWS, __version__
from mcp_portal.token_verifier import CURRENT_AUTH_FAILURE

logger = logging.getLogger(__name__)

AUTH_FAILED_ACTION = 'authentication_failed'
RESOURCE_TYPE = 'mcp_server'
NO_TOOL = '-'
ENDPOINT_PREFIX = '/mcp'


def _str_or_none(value) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _endpoint(path: str) -> tuple:
    """(endpoint URL, view or None) for a request path."""
    canonical = os.environ.get('MCP_CANONICAL_URL', '')
    parts = urlsplit(canonical)
    origin = f'{parts.scheme}://{parts.netloc}' if parts.netloc else ''
    # Endpoint URLs never end with '/' (D27); `/mcp/` names `/mcp`.
    path = path.rstrip('/') or '/'
    view = None
    if path.startswith(ENDPOINT_PREFIX + '/'):
        candidate = path[len(ENDPOINT_PREFIX) + 1:].strip('/')
        view = candidate if candidate in VIEWS else None
    return f'{origin}{path}', view


def _server_label(view: Optional[str]) -> str:
    version = os.environ.get('MCP_SERVER_VERSION') or __version__
    name = f'dda-portal-mcp/{view}' if view else 'dda-portal-mcp'
    return f'{name}@{version}'


def build_entry(scope: dict, failure) -> dict:
    """The keyword arguments of the `log_audit_event` call for `failure`."""
    event = scope.get('aws.event') if isinstance(scope.get('aws.event'), dict) else {}
    request_context = event.get('requestContext') if isinstance(
        event.get('requestContext'), dict) else {}
    http = request_context.get('http') if isinstance(
        request_context.get('http'), dict) else {}
    endpoint_url, view = _endpoint(scope.get('path') or ENDPOINT_PREFIX)
    user_agent = _str_or_none(http.get('userAgent'))
    details = {'reason': failure.reason}
    if failure.client_id is not None:
        details['client_id'] = failure.client_id
    if failure.token_use is not None:
        details['token_use'] = failure.token_use
    identity = {
        'username': None,
        'email': None,
        'source_ip': _str_or_none(http.get('sourceIp')),
        'user_agent': user_agent,
        'identity_source': None,
        'channel': 'mcp',
        'mcp_tool': NO_TOOL,
        'mcp_server': _server_label(view),
        'mcp_client': user_agent,
        'mcp_request_id': _str_or_none(request_context.get('requestId')),
    }
    return {
        'user_id': failure.sub,
        'action': AUTH_FAILED_ACTION,
        'resource_type': RESOURCE_TYPE,
        'resource_id': endpoint_url,
        'result': 'denied',
        'details': details,
        'identity': identity,
    }


def _write(scope: dict, failure) -> None:
    try:
        import shared_utils  # the SharedLayer; imported where it is needed
        shared_utils.log_audit_event(**build_entry(scope, failure))
    except Exception as error:  # noqa: BLE001 - the 401 stands regardless
        logger.error('Could not write the authentication_failed audit entry '
                     'for sub %s: %s', failure.sub, type(error).__name__)


class AuthFailureAuditMiddleware:
    """Write the marked `authentication_failed` entry for a 401 with a `sub`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return
        reset_token = CURRENT_AUTH_FAILURE.set(None)
        status = {}

        async def send_and_observe(message):
            if message.get('type') == 'http.response.start':
                status['code'] = message.get('status')
            await send(message)

        try:
            await self.app(scope, receive, send_and_observe)
            failure = CURRENT_AUTH_FAILURE.get()
            if status.get('code') == 401 and failure is not None \
                    and getattr(failure, 'sub', None):
                _write(scope, failure)
        finally:
            CURRENT_AUTH_FAILURE.reset(reset_token)
