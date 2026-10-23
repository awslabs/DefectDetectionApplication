"""Test-only principal injection for the in-memory protocol suites.

Spec: .kiro/specs/mcp-servers/ (design.md Testing Strategy row "16.1
protocol level"; C4, review-3 N7; tasks.md 7.1).

The SDK's in-process `Client` does not go through HTTP authentication, so
`set_principal(access_token)` does nothing but set
`mcp.server.auth.middleware.auth_context.auth_context_var` (the very
`ContextVar` that `AuthContextMiddleware` sets on the HTTP path) to
`AuthenticatedUser(access_token)`, and resets it on exit. Enter it before
the `Client(...)` context so the server's task group inherits the context.
It lives under `tests/`, never in the shipped `mcp_portal` package, so no
production override path exists; `on_call_tool` reads the principal
through `get_access_token()` only.
"""
import contextlib

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser


@contextlib.contextmanager
def set_principal(access_token):
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)
