"""Property test: access-token acceptance.

**Feature: mcp-servers, Property 1: Access-token acceptance**

Spec: .kiro/specs/mcp-servers/ (design.md "2. `mcp_portal` package"
`token_verifier.py` row, Error Handling (Portal), Correctness Property 1;
tasks.md 3.5). Validates: Requirements 2.1, 2.2, 2.4, 4.7.

For any RS256 token signed by the test pool key (or another key) with
arbitrary `token_use`, `client_id`, `scope` string, optional `aud` (string
or list), `exp` around now and issuer string, `verify` returns an
`AccessToken` iff the signature verifies, `iss` equals the configured issuer
exactly, `exp` is in the future, `token_use == 'access'`, `client_id` is the
MCP client id, the required scope is among the space-separated scopes and
`aud` is absent or intersects the allowed endpoint set; and
`CURRENT_AUTH_FAILURE` holds the token's `sub` exactly when the signature
and issuer verified but a later check failed.

Keys are generated at test time and the JWKS is in memory; every identifier
is visibly fake. `exp` offsets keep at least 5 s away from "now" so the
oracle and the verifier cannot disagree across a clock tick.
"""
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from hypothesis import given, settings
from hypothesis import strategies as st

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
POOL_JWK = jwt.PyJWK.from_dict(
    {**jwt.algorithms.RSAAlgorithm.to_jwk(POOL_KEY.public_key(), as_dict=True),
     "kid": KID, "alg": "RS256", "use": "sig"})


class FakeJwksClient:
    """`PyJWKClient` stand-in: the pool's public key for every token."""

    def get_signing_key_from_jwt(self, _token):
        return POOL_JWK


VERIFIER = tv.CognitoAccessTokenVerifier(
    issuer=ISSUER, client_id=CLIENT_ID, required_scope=SCOPE,
    allowed_audiences=AUDIENCES, jwks_client=FakeJwksClient())

_OTHER_URLS = st.sampled_from([f"{ORIGIN}/other", f"{ORIGIN}/mcp/", ORIGIN,
                               "https://elsewhere.example.com/mcp"])
_AUD_VALUE = st.one_of(st.sampled_from(AUDIENCES), _OTHER_URLS)
_AUD = st.one_of(st.none(), _AUD_VALUE,
                 st.lists(_AUD_VALUE, min_size=0, max_size=3))
_SCOPE_WORD = st.one_of(st.sampled_from([SCOPE, "openid", "profile",
                                         f"{SCOPE}x", f"{ORIGIN}/mcp"]),
                        st.from_regex(r"\A[a-z./:]{1,12}\Z", fullmatch=True))
_SCOPE = st.lists(_SCOPE_WORD, min_size=0, max_size=4).map(" ".join)
_ISSUER = st.sampled_from([ISSUER, ISSUER + "/", ISSUER.upper(),
                           "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHER0001"])
_EXP_OFFSET = st.one_of(st.integers(min_value=-3600, max_value=-5),
                        st.integers(min_value=5, max_value=3600))


@settings(max_examples=100, deadline=None)
@given(token_use=st.sampled_from(["access", "id", "other"]),
       client_id=st.sampled_from([CLIENT_ID, "fake-web-client-id", ""]),
       scope=_SCOPE, aud=_AUD, exp_offset=_EXP_OFFSET, issuer=_ISSUER,
       pool_signed=st.booleans())
def test_access_token_acceptance(token_use, client_id, scope, aud, exp_offset,
                                 issuer, pool_signed):
    now = int(time.time())
    sub = str(uuid.uuid4())
    payload = {"sub": sub, "iss": issuer, "exp": now + exp_offset,
               "iat": now - 5, "token_use": token_use, "client_id": client_id,
               "scope": scope, "jti": str(uuid.uuid4())}
    if aud is not None:
        payload["aud"] = aud
    token = jwt.encode(payload, POOL_KEY if pool_signed else OTHER_KEY,
                       algorithm="RS256", headers={"kid": KID})

    tv.CURRENT_AUTH_FAILURE.set(None)
    result = VERIFIER.verify(token)
    failure = tv.CURRENT_AUTH_FAILURE.get()

    signature_and_issuer = pool_signed and issuer == ISSUER
    audiences = [aud] if isinstance(aud, str) else (aud or [])
    claims_ok = (exp_offset > 0
                 and token_use == "access"
                 and client_id == CLIENT_ID
                 and SCOPE in scope.split()
                 and (aud is None or any(a in AUDIENCES for a in audiences)))

    accepted = signature_and_issuer and claims_ok
    assert isinstance(result, AccessToken) is accepted
    assert (result is None) is (not accepted)
    if accepted:
        assert result.subject == sub
        assert result.client_id == CLIENT_ID
        assert SCOPE in result.scopes

    if signature_and_issuer and not claims_ok:
        assert failure is not None and failure.sub == sub
    else:
        assert failure is None
