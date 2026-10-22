"""`AuthFailureAuditMiddleware` (`mcp_portal/auth_audit.py`), unit cases.

Spec: .kiro/specs/mcp-servers/ (design.md "2. `mcp_portal` package"
`auth_audit.py` row, Data Models "Audit log entry" `authentication_failed`,
Error Handling (Portal) token rows; tasks.md 3.6). Requirements 4.7, 2.7.

A minimal Starlette app: the middleware outermost, then the SDK's
`AuthenticationMiddleware` with `BearerAuthBackend(CognitoAccessTokenVerifier)`,
and `RequireAuthMiddleware` on `/mcp/...`. The ASGI-transport and
middleware-order cases of `build_asgi_app()` are task 7.2's. Keys are
generated at test time; every identifier is visibly fake.
"""
import asyncio
import logging
import sys
import time
import uuid

import jwt
import pytest
from boto3.dynamodb.conditions import Attr
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.testclient import TestClient

from mcp.server.auth.middleware.bearer_auth import (BearerAuthBackend,
                                                    RequireAuthMiddleware)
from mcp_portal import auth_audit
from mcp_portal import token_verifier as tv

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_FAKEPOOL1"
CLIENT_ID = "fake-mcp-client-id"
ORIGIN = "https://fakeapi000.execute-api.us-east-1.amazonaws.com"
CANONICAL = f"{ORIGIN}/mcp"
SCOPE = f"{CANONICAL}/portal.tools"
AUDIENCES = (CANONICAL,) + tuple(f"{CANONICAL}/{v}" for v in
                                 ("data", "models", "devices", "workflows", "admin"))
KID = "fake-kid-1"
USER_AGENT = "fake-mcp-client/0.1"
SOURCE_IP = "203.0.113.9"

POOL_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
POOL_JWK = jwt.PyJWK.from_dict(
    {**jwt.algorithms.RSAAlgorithm.to_jwk(POOL_KEY.public_key(), as_dict=True),
     "kid": KID, "alg": "RS256", "use": "sig"})


class FakeJwksClient:
    def get_signing_key_from_jwt(self, _token):
        return POOL_JWK


def token(key=POOL_KEY, **overrides):
    now = int(time.time())
    payload = {"sub": str(uuid.uuid4()), "iss": ISSUER, "exp": now + 600,
               "iat": now - 5, "token_use": "access", "client_id": CLIENT_ID,
               "scope": SCOPE, "jti": str(uuid.uuid4())}
    payload.update(overrides)
    return payload["sub"], jwt.encode(payload, key, algorithm="RS256",
                                      headers={"kid": KID})


async def ok(scope, receive, send):
    await JSONResponse({"ok": True})(scope, receive, send)


def build_app(request_id):
    verifier = tv.CognitoAccessTokenVerifier(
        issuer=ISSUER, client_id=CLIENT_ID, required_scope=SCOPE,
        allowed_audiences=AUDIENCES, jwks_client=FakeJwksClient())
    app = Starlette(
        routes=[Mount("/mcp", app=RequireAuthMiddleware(ok, required_scopes=[]))],
        middleware=[
            Middleware(auth_audit.AuthFailureAuditMiddleware),
            Middleware(AuthenticationMiddleware,
                       backend=BearerAuthBackend(verifier)),
        ])
    event = {"version": "2.0", "requestContext": {
        "requestId": request_id,
        "http": {"method": "POST", "sourceIp": SOURCE_IP,
                 "userAgent": USER_AGENT}}}

    async def with_event(scope, receive, send):
        # Mangum passes the Lambda event in the scope under this key.
        scope["aws.event"] = event
        await app(scope, receive, send)
    return with_event


@pytest.fixture
def audit(aws_stack, monkeypatch):
    monkeypatch.setenv("MCP_CANONICAL_URL", CANONICAL)
    monkeypatch.delenv("MCP_SERVER_VERSION", raising=False)
    shared = sys.modules["shared_utils"]
    monkeypatch.setattr(shared, "_REQUEST_AUDIT_CONTEXT", None)

    def entries(sub):
        return aws_stack.tables.audit_log.scan(
            FilterExpression=Attr("user_id").eq(sub))["Items"]
    return entries


def post(path, bearer=None, request_id="fake-apigw-request-1"):
    client = TestClient(build_app(request_id))
    headers = {"User-Agent": USER_AGENT}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return client.post(path, headers=headers, json={})


# --------------------------------------------------- claim failures: written

@pytest.mark.parametrize("overrides,reason", [
    ({"exp": int(time.time()) - 60}, "expired"),
    ({"client_id": "fake-web-client-id"}, "client_id"),
    ({"scope": "openid"}, "scope"),
    ({"token_use": "id"}, "token_use"),
    ({"aud": f"{ORIGIN}/elsewhere"}, "audience"),
])
def test_claim_failure_writes_one_marked_entry(audit, overrides, reason):
    sub, bearer = token(**overrides)
    response = post("/mcp/devices", bearer, request_id=f"req-{reason}")
    assert response.status_code == 401
    items = audit(sub)
    assert len(items) == 1
    item = items[0]
    assert item["action"] == "authentication_failed"
    assert item["resource_type"] == "mcp_server"
    assert item["resource_id"] == f"{CANONICAL}/devices"
    assert item["result"] == "denied"
    expected_details = {"reason": reason,
                        "client_id": overrides.get("client_id", CLIENT_ID),
                        "token_use": overrides.get("token_use", "access")}
    assert item["details"] == expected_details
    assert item["channel"] == "mcp"
    assert item["mcp_tool"] == "-"
    assert item["mcp_server"] == "dda-portal-mcp/devices@1.0.0"
    assert item["mcp_client"] == USER_AGENT
    assert item["mcp_request_id"] == f"req-{reason}"
    assert item["source_ip"] == SOURCE_IP
    assert item["user_agent"] == USER_AGENT
    assert bearer not in str(item)


def test_aggregate_endpoint_names_the_aggregate_server(audit):
    sub, bearer = token(client_id="fake-web-client-id")
    assert post("/mcp", bearer).status_code == 401
    (item,) = audit(sub)
    assert item["resource_id"] == CANONICAL
    assert item["mcp_server"] == "dda-portal-mcp@1.0.0"


# ------------------------------------------------- nothing verifiable: none

def test_bad_signature_writes_nothing(audit):
    sub, bearer = token(key=OTHER_KEY, exp=int(time.time()) - 60)
    assert post("/mcp/admin", bearer).status_code == 401
    assert audit(sub) == []


def test_wrong_issuer_writes_nothing(audit):
    sub, bearer = token(iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHER0001")
    assert post("/mcp/admin", bearer).status_code == 401
    assert audit(sub) == []


def test_valid_token_passes_and_writes_nothing(audit):
    sub, bearer = token()
    response = post("/mcp/data", bearer)
    assert response.status_code == 200
    assert audit(sub) == []


def test_no_token_and_query_token_write_nothing(audit):
    sub, bearer = token(client_id="fake-web-client-id")
    client = TestClient(build_app("req-query"))
    response = client.post(f"/mcp/data?access_token={bearer}", json={})
    assert response.status_code == 401
    assert audit(sub) == []


# ------------------------------------------------------- failure handling

def test_audit_write_failure_is_swallowed_and_logged(audit, monkeypatch, caplog):
    shared = sys.modules["shared_utils"]

    def broken(**_kwargs):
        raise RuntimeError("table unavailable")
    monkeypatch.setattr(shared, "log_audit_event", broken)
    sub, bearer = token(token_use="id")
    with caplog.at_level(logging.ERROR, logger="mcp_portal.auth_audit"):
        response = post("/mcp/models", bearer)
    assert response.status_code == 401
    errors = [r for r in caplog.records if r.levelno == logging.ERROR
              and r.name == "mcp_portal.auth_audit"]
    assert len(errors) == 1
    assert sub in errors[0].getMessage()
    assert bearer not in caplog.text


def _run(middleware_app, path="/mcp/data"):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": path, "headers": [],
             "aws.event": {"requestContext": {"requestId": "r", "http": {}}}}
    asyncio.run(middleware_app(scope, receive, send))
    return sent


def test_stale_failure_is_reset_at_request_start(monkeypatch):
    calls = []
    monkeypatch.setattr(auth_audit, "_write", lambda *a: calls.append(a))

    async def unauthenticated(scope, receive, send):
        await JSONResponse({}, status_code=401)(scope, receive, send)

    tv.CURRENT_AUTH_FAILURE.set(tv.AuthFailure(sub="stale-sub", reason="expired"))
    try:
        _run(auth_audit.AuthFailureAuditMiddleware(unauthenticated))
    finally:
        tv.CURRENT_AUTH_FAILURE.set(None)
    assert calls == []


@pytest.mark.parametrize("status", [200, 403, 500])
def test_recorded_failure_without_401_writes_nothing(monkeypatch, status):
    calls = []
    monkeypatch.setattr(auth_audit, "_write", lambda *a: calls.append(a))

    async def inner(scope, receive, send):
        tv.CURRENT_AUTH_FAILURE.set(tv.AuthFailure(sub="s-1", reason="scope"))
        await JSONResponse({}, status_code=status)(scope, receive, send)

    _run(auth_audit.AuthFailureAuditMiddleware(inner))
    assert calls == []


def test_recorded_failure_with_401_writes_once(monkeypatch):
    calls = []
    monkeypatch.setattr(auth_audit, "_write", lambda *a: calls.append(a))

    async def inner(scope, receive, send):
        tv.CURRENT_AUTH_FAILURE.set(tv.AuthFailure(sub="s-2", reason="scope"))
        await JSONResponse({}, status_code=401)(scope, receive, send)

    sent = _run(auth_audit.AuthFailureAuditMiddleware(inner))
    assert len(calls) == 1 and calls[0][1].sub == "s-2"
    assert sent[0]["status"] == 401


def test_non_http_scopes_pass_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    async def go():
        await auth_audit.AuthFailureAuditMiddleware(inner)(
            {"type": "lifespan"}, None, None)
    asyncio.run(go())
    assert seen == ["lifespan"]
