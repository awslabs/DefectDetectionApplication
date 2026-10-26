"""Lambda entry point of the Portal_MCP_Server (design §2 `handler.py` row,
Decision point 2 "Root-app composition", D26, D32; Requirements 7.1, 7.2,
7.3, 15.3).

Module level, built once per container: one `Server` per endpoint
(`SERVERS`; key None is the aggregate `/mcp`) and the access-token
verifier (`VERIFIER`).

`build_asgi_app()` composes a new root app per invocation, because a
`StreamableHTTPSessionManager.run()` can be entered once only: six fresh
session managers (JSON responses, stateless, explicit transport security:
`Host` limited to `MCP_API_HOST`, `Origin` to `MCP_ALLOWED_ORIGINS`, empty
by default), six exact `POST` routes under `RequireAuthMiddleware`, the
eight discovery routes, the root middleware in the order
`[AuthFailureAuditMiddleware, AuthenticationMiddleware, AuthContextMiddleware]`
(Starlette's first entry is the outermost wrapper; the audit wrapper must
enclose the verifier whose failure it records — review-4 M1), and an
`AsyncExitStack` lifespan that runs every manager.

`handler(event, context)` hands the dispatcher the Lambda context (the
remaining-time guard) and serves the event through Mangum with
`lifespan='auto'`. A Lambda container serves one invocation at a time, so
the module-level `dispatch.LAMBDA_CONTEXT` is sound.
"""
import contextlib
import os

from mangum import Mangum
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.streamable_http_manager import (StreamableHTTPASGIApp,
                                                StreamableHTTPSessionManager)
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route

from mcp_portal import VIEWS, discovery, dispatch
from mcp_portal.auth_audit import AuthFailureAuditMiddleware
from mcp_portal.server import build_server
from mcp_portal.token_verifier import CognitoAccessTokenVerifier

ENDPOINTS = (None,) + VIEWS

SERVERS = {view: build_server(view) for view in ENDPOINTS}
VERIFIER = CognitoAccessTokenVerifier()


def _csv(value) -> list:
    return [item.strip() for item in (value or '').split(',') if item.strip()]


def endpoint_path(view) -> str:
    return '/mcp' if view is None else f'/mcp/{view}'


def endpoint_url(view) -> str:
    return discovery.MCP_CANONICAL_URL + ('' if view is None else f'/{view}')


def transport_security() -> TransportSecuritySettings:
    """Explicit settings (left None, the SDK would admit loopback origins)."""
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[os.environ.get('MCP_API_HOST', '')],
        allowed_origins=_csv(os.environ.get('MCP_ALLOWED_ORIGINS')))


def build_asgi_app() -> Starlette:
    """The root app of one invocation (fresh session managers)."""
    security = transport_security()
    managers = []
    routes = []
    for view in ENDPOINTS:
        manager = StreamableHTTPSessionManager(app=SERVERS[view], json_response=True,
                                               stateless=True,
                                               security_settings=security)
        managers.append(manager)
        routes.append(Route(
            endpoint_path(view),
            endpoint=RequireAuthMiddleware(
                StreamableHTTPASGIApp(manager), [discovery.MCP_REQUIRED_SCOPE],
                build_resource_metadata_url(AnyHttpUrl(endpoint_url(view)))),
            methods=['POST']))
    routes += discovery.routes()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with contextlib.AsyncExitStack() as stack:
            for manager in managers:
                await stack.enter_async_context(manager.run())
            yield

    return Starlette(
        routes=routes,
        # Starlette: the first entry is the outermost wrapper; the audit
        # wrapper must enclose the verifier (review-4 M1).
        middleware=[
            Middleware(AuthFailureAuditMiddleware),
            Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(VERIFIER)),
            Middleware(AuthContextMiddleware),
        ],
        lifespan=lifespan)


def handler(event, context):
    dispatch.LAMBDA_CONTEXT = context
    return Mangum(build_asgi_app(), lifespan='auto')(event, context)
