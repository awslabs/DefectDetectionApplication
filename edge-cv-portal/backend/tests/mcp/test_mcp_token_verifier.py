"""`CognitoAccessTokenVerifier` (`mcp_portal/token_verifier.py`).

Spec: .kiro/specs/mcp-servers/ (design.md "2. `mcp_portal` package"
`token_verifier.py` row, "Why no gateway authorizer", Error Handling; tasks.md
3.4). Requirements 2.1, 2.2, 2.3, 2.6, 2.7, 2.8, 16.2.

The `test_jwt_authorizer_verification.py` pattern: RSA key pairs generated at
test time, real RS256 signatures, an in-memory JWKS, nothing fetched over the
network. Every identifier below is visibly fake.
"""
import asyncio
import logging
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.provider import AccessToken
from mcp_portal import token_verifier as tv

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_FAKEPOOL1"
CLIENT_ID = "fake-mcp-client-id"
ORIGIN = "https://fakeapi000.execute-api.us-east-1.amazonaws.com"
SCOPE = f"{ORIGIN}/mcp/portal.tools"
AUDIENCES = (f"{ORIGIN}/mcp", f"{ORIGIN}/mcp/data", f"{ORIGIN}/mcp/models",
             f"{ORIGIN}/mcp/devices", f"{ORIGIN}/mcp/workflows",
             f"{ORIGIN}/mcp/admin")
KID = "fake-kid-1"

POOL_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwksClient:
    """`PyJWKClient` stand-in: the pool's public key for its `kid` only."""

    def __init__(self):
        self.calls = 0

    def get_signing_key_from_jwt(self, token):
        self.calls += 1
        header = jwt.get_unverified_header(token)
        if header.get("kid") != KID:
            raise jwt.PyJWKClientError("Unable to find a signing key")
        return jwt.PyJWK.from_dict(
            {**jwt.algorithms.RSAAlgorithm.to_jwk(POOL_KEY.public_key(),
                                                  as_dict=True),
             "kid": KID, "alg": "RS256", "use": "sig"})


def claims(**overrides):
    now = int(time.time())
    base = {"sub": str(uuid.uuid4()), "iss": ISSUER, "exp": now + 600,
            "iat": now - 5, "token_use": "access", "client_id": CLIENT_ID,
            "scope": f"openid {SCOPE}", "jti": str(uuid.uuid4()),
            "username": "fake-user"}
    for key, value in overrides.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = value
    return base


def sign(payload, key=POOL_KEY, kid=KID, algorithm="RS256"):
    return jwt.encode(payload, key, algorithm=algorithm, headers={"kid": kid})


@pytest.fixture
def verifier():
    tv.CURRENT_AUTH_FAILURE.set(None)
    return tv.CognitoAccessTokenVerifier(
        issuer=ISSUER, client_id=CLIENT_ID, required_scope=SCOPE,
        allowed_audiences=AUDIENCES, jwks_client=FakeJwksClient())


def verify(verifier, token):
    # The synchronous body, so CURRENT_AUTH_FAILURE is observed in this
    # context (asyncio.run would run the coroutine in a copy of it).
    return verifier.verify(token)


def test_verify_token_is_the_async_entry_point(verifier):
    async def both():
        accepted = await verifier.verify_token(sign(claims()))
        rejected = await verifier.verify_token(sign(claims(token_use="id")))
        return accepted, rejected, tv.CURRENT_AUTH_FAILURE.get()

    accepted, rejected, failure = asyncio.run(both())
    assert isinstance(accepted, AccessToken)
    assert rejected is None
    assert failure.reason == "token_use"


# ------------------------------------------------------------- acceptance

def test_valid_token_without_aud(verifier):
    payload = claims()
    token = sign(payload)
    result = verify(verifier, token)
    assert isinstance(result, AccessToken)
    assert result.client_id == CLIENT_ID
    assert result.scopes == ["openid", SCOPE]
    assert result.subject == payload["sub"]
    assert result.expires_at == payload["exp"]
    assert result.resource is None
    assert result.claims == payload
    assert tv.CURRENT_AUTH_FAILURE.get() is None


@pytest.mark.parametrize("aud", [AUDIENCES[0], [AUDIENCES[3]],
                                 ["https://elsewhere.example.com", AUDIENCES[2]]])
def test_valid_token_with_allowed_aud(verifier, aud):
    result = verify(verifier, sign(claims(aud=aud)))
    assert isinstance(result, AccessToken)
    expected = aud if isinstance(aud, str) else [a for a in aud if a in AUDIENCES][0]
    assert result.resource == expected


def test_environment_configuration(monkeypatch):
    monkeypatch.setenv("COGNITO_ISSUER", ISSUER)
    monkeypatch.setenv("MCP_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("MCP_REQUIRED_SCOPE", SCOPE)
    monkeypatch.setenv("MCP_ALLOWED_AUDIENCES", ",".join(AUDIENCES))
    configured = tv.CognitoAccessTokenVerifier()
    assert configured.issuer == ISSUER
    assert configured.client_id == CLIENT_ID
    assert configured.required_scope == SCOPE
    assert configured.allowed_audiences == AUDIENCES
    assert isinstance(configured.jwks_client, jwt.PyJWKClient)
    assert configured.jwks_client.uri == f"{ISSUER}/.well-known/jwks.json"


# ------------------------------------------------- rejection, nothing recorded

@pytest.mark.parametrize("token", [
    "", "not-a-jwt", "a.b.c",
    sign(claims(), key=OTHER_KEY),                 # bad signature
    sign(claims(), kid="unknown-kid"),             # key not in the pool JWKS
    sign(claims(iss="https://cognito-idp.us-east-1.amazonaws.com/other")),
    sign(claims(iss=ISSUER + "/")),
    sign(claims(jti=None)),                        # a required claim missing
    sign(claims(scope=None)),
])
def test_rejected_without_a_recorded_failure(verifier, token):
    assert verify(verifier, token) is None
    assert tv.CURRENT_AUTH_FAILURE.get() is None


def test_hs256_token_rejected(verifier):
    token = jwt.encode(claims(), "shared-secret-value-of-sufficient-length!",
                       algorithm="HS256", headers={"kid": KID})
    assert verify(verifier, token) is None
    assert tv.CURRENT_AUTH_FAILURE.get() is None


# ------------------------------------- rejection after signature and issuer

@pytest.mark.parametrize("overrides,reason", [
    ({"exp": int(time.time()) - 1}, "expired"),
    ({"exp": "not-a-number"}, "expired"),
    ({"token_use": "id"}, "token_use"),
    ({"client_id": "another-client"}, "client_id"),
    ({"scope": "openid profile"}, "scope"),
    ({"scope": f"{SCOPE}x"}, "scope"),
    ({"aud": "https://elsewhere.example.com/mcp"}, "audience"),
    ({"aud": ["https://elsewhere.example.com/mcp"]}, "audience"),
    ({"aud": []}, "audience"),
])
def test_claim_failure_recorded_with_sub(verifier, overrides, reason):
    payload = claims(**overrides)
    assert verify(verifier, sign(payload)) is None
    failure = tv.CURRENT_AUTH_FAILURE.get()
    assert failure == tv.AuthFailure(sub=payload["sub"], reason=reason,
                                     client_id=payload["client_id"],
                                     token_use=payload["token_use"])


class UnreachableJwksClient:
    """`PyJWKClient` stand-in whose key-set fetch fails, as `fetch_data` does
    on a URL error, a timeout or an HTTP exception."""

    def get_signing_key_from_jwt(self, token):
        raise jwt.PyJWKClientConnectionError(
            "Fail to fetch data from the url, err: timed out")


def test_jwks_fetch_failure_logged_at_error_with_kid(caplog):
    tv.CURRENT_AUTH_FAILURE.set(None)
    unreachable = tv.CognitoAccessTokenVerifier(
        issuer=ISSUER, client_id=CLIENT_ID, required_scope=SCOPE,
        allowed_audiences=AUDIENCES, jwks_client=UnreachableJwksClient())
    caplog.set_level(logging.DEBUG, logger=tv.logger.name)
    token = sign(claims())
    assert verify(unreachable, token) is None
    assert tv.CURRENT_AUTH_FAILURE.get() is None
    records = [r for r in caplog.records if r.name == tv.logger.name]
    assert [r.levelno for r in records] == [logging.ERROR]
    assert KID in records[0].getMessage()
    assert token not in caplog.text
    for segment in token.split("."):
        assert segment not in caplog.text


def test_unknown_kid_stays_at_info(verifier, caplog):
    caplog.set_level(logging.DEBUG, logger=tv.logger.name)
    assert verify(verifier, sign(claims(), kid="unknown-kid")) is None
    records = [r for r in caplog.records if r.name == tv.logger.name]
    assert [r.levelno for r in records] == [logging.INFO]


def test_token_never_logged(verifier, caplog):
    caplog.set_level(logging.DEBUG)
    tokens = [sign(claims(token_use="id")), sign(claims(), key=OTHER_KEY),
              sign(claims())]
    for token in tokens:
        verify(verifier, token)
    for token in tokens:
        assert token not in caplog.text
        assert token.split(".")[2] not in caplog.text


# ---------------------------------------- through the SDK bearer backend

def _app(verifier):
    async def whoami(request: Request):
        user = request.user
        return JSONResponse({"authenticated": user.is_authenticated})

    return Starlette(routes=[Route("/mcp", whoami, methods=["GET"])],
                     middleware=[Middleware(AuthenticationMiddleware,
                                            backend=BearerAuthBackend(verifier))])


def test_bearer_header_authenticates(verifier):
    client = TestClient(_app(verifier))
    response = client.get("/mcp", headers={"Authorization": f"Bearer {sign(claims())}"})
    assert response.json() == {"authenticated": True}


def test_no_token_is_unauthenticated(verifier):
    client = TestClient(_app(verifier))
    assert client.get("/mcp").json() == {"authenticated": False}
    assert verifier.jwks_client.calls == 0


def test_access_token_query_parameter_ignored(verifier):
    client = TestClient(_app(verifier))
    response = client.get("/mcp", params={"access_token": sign(claims())})
    assert response.json() == {"authenticated": False}
    assert verifier.jwks_client.calls == 0
