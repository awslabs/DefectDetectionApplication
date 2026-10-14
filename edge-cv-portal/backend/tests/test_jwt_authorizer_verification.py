"""
Verification tests for the Portal JWT authorizer (security-scan-remediation-high,
R6). Every Allow decision of ``functions/jwt_authorizer.py`` must rest on a JWT
whose RS256 signature, expiry, issuer, audience and, for the user pool, token
type were checked against configuration; only the header's ``kid`` is read
before verification.

The tests go through ``handler()`` and check the returned policy. RSA key pairs
are generated at test time, JWTs are signed with real RS256, and
``get_jwks_keys`` is replaced by an in-memory JWKS, so nothing is fetched over
the network. Every identifier below is visibly fake.

# Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 5.3
"""
import ast
import base64
import functools
import hashlib
import hmac
import importlib.util
import itertools
import json
import logging
import os
import time

import pytest

# Some hosts carry PyJWT 1.x. importorskip's minversion can't be used here: it
# imports packaging.version, and conftest puts backend/functions, which holds
# the Portal's own packaging.py, first on sys.path.
jwt = pytest.importorskip("jwt")
if tuple(int(part) for part in jwt.__version__.split(".")[:2]) < (2, 8):
    pytest.skip(f"PyJWT {jwt.__version__} is older than 2.8", allow_module_level=True)

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    PublicFormat,
)
from hypothesis import assume, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from jwt.algorithms import RSAAlgorithm  # noqa: E402

_AUTHORIZER_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "functions", "jwt_authorizer.py")
)

REGION = "us-east-1"
POOL_ID = "us-east-1_FAKEPOOL1"
POOL_ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
POOL_JWKS_URL = f"{POOL_ISSUER}/.well-known/jwks.json"
CLIENT_ID = "fake-portal-client-id"
IDP_ISSUER = "https://idp.example.com"
IDP_JWKS_URL = f"{IDP_ISSUER}/.well-known/jwks.json"
METHOD_ARN = "arn:aws:execute-api:us-east-1:111122223333:fakeapi/prod/GET/usecases"

_LOADS = itertools.count()


@pytest.fixture(autouse=True)
def _restore_root_log_level(caplog):
    # The module sets the root logger to INFO at import; caplog restores it.
    caplog.set_level(logging.INFO)


def _load_authorizer(jwks_by_url, pool_id=POOL_ID, audiences=CLIENT_ID, whitelist=""):
    """Load jwt_authorizer.py afresh with this configuration, serving each JWKS
    URL from ``jwks_by_url``; any other URL fails like a network error."""
    env = {
        "COGNITO_USER_POOL_ID": pool_id,
        "COGNITO_REGION": REGION,
        "ALLOWED_AUDIENCES": audiences,
        "ISSUER_WHITELIST": whitelist,
    }
    saved = {name: os.environ.get(name) for name in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(
            f"jwt_authorizer_verification_{next(_LOADS)}", _AUTHORIZER_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for name, prev in saved.items():
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev

    fetched = []

    def _fake_get_jwks_keys(url):
        fetched.append(url)
        if url not in jwks_by_url:
            raise mod.AuthorizationError(f"Failed to fetch JWKS keys: {url}")
        return jwks_by_url[url]

    mod.get_jwks_keys = _fake_get_jwks_keys
    mod.fetched_jwks_urls = fetched
    return mod


@functools.lru_cache(maxsize=None)
def _private_key(name):
    """An RSA key pair generated at test time, one per name."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(name, kid):
    """The public JWK of key ``name``, published under ``kid``."""
    jwk = json.loads(RSAAlgorithm.to_jwk(_private_key(name).public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return jwk


def _jwks(*keys):
    return {"keys": list(keys)}


def _pool_authorizer(audiences=CLIENT_ID):
    """The deployed shape: the user pool and the one allowed app client."""
    return _load_authorizer({POOL_JWKS_URL: _jwks(_jwk("pool", "pool-kid-1"))},
                            audiences=audiences)


def _id_claims(changes=None, drop=()):
    """Claims of a valid user-pool ID token for the allowed app client, with
    ``changes`` applied and the ``drop`` claims removed."""
    now = int(time.time())
    claims = {
        "sub": "fake-user-0001",
        "iss": POOL_ISSUER,
        "aud": CLIENT_ID,
        "token_use": "id",
        "email": "fake.user@example.com",
        "cognito:username": "fake-user",
        "custom:role": "DataScientist",
        "custom:groups": "fake-team-a",
        "iat": now,
        "exp": now + 900,
    }
    claims.update(changes or {})
    for name in drop:
        claims.pop(name, None)
    return claims


def _idp_claims(changes=None):
    """Claims of a valid JWT from the whitelisted identity provider."""
    now = int(time.time())
    claims = {"sub": "fake-idp-user", "iss": IDP_ISSUER, "aud": CLIENT_ID,
              "groups": ["cv-operators"], "iat": now, "exp": now + 900}
    claims.update(changes or {})
    return claims


def _sign(claims, key_name="pool", kid="pool-kid-1"):
    """A compact JWT signed with RS256 by key ``key_name``."""
    return jwt.encode(claims, _private_key(key_name), algorithm="RS256",
                      headers={"kid": kid})


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _payload(compact):
    return json.loads(_b64url_decode(compact.split(".")[1]))


def _with_payload(compact, claims):
    """``compact`` with its payload re-encoded from ``claims``, under the
    original header and signature."""
    header, _, signature = compact.split(".")
    body = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    return ".".join((header, body, signature))


def _flip_signature_byte(compact):
    header, body, signature = compact.split(".")
    raw = bytearray(_b64url_decode(signature))
    raw[0] ^= 0x01
    return ".".join((header, body, _b64url(bytes(raw))))


def _unsigned(claims, kid="pool-kid-1"):
    """An ``alg: none`` JWT with an empty signature."""
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT", "kid": kid}).encode())
    return f"{header}.{_b64url(json.dumps(claims).encode())}."


def _hs256(claims, kid="pool-kid-1"):
    """An HS256 JWT keyed with the pool's public key PEM: the
    algorithm-confusion shape an RS256 verifier must refuse."""
    pem = _private_key("pool").public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    signing_input = f"{header}.{_b64url(json.dumps(claims).encode())}"
    mac = hmac.new(pem, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(mac)}"


def _event(compact):
    return {"authorizationToken": f"Bearer {compact}", "methodArn": METHOD_ARN}


def _effect(policy):
    return policy["policyDocument"]["Statement"][0]["Effect"]


def _assert_denied(policy):
    assert policy["principalId"] == "unauthorized"
    assert _effect(policy) == "Deny"
    assert "context" not in policy


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def _errors(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


# --------------------------------------------------------------------------- #
# Accepted
# --------------------------------------------------------------------------- #
def test_valid_pool_id_jwt_is_allowed_with_its_claims_as_context():
    mod = _pool_authorizer()
    claims = _id_claims()

    policy = mod.handler(_event(_sign(claims)), None)

    assert policy == {
        "principalId": claims["sub"],
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke", "Effect": "Allow",
                           "Resource": METHOD_ARN}],
        },
        "context": {
            "userId": claims["sub"], "email": claims["email"],
            "username": claims["cognito:username"], "role": claims["custom:role"],
            "groups": claims["custom:groups"], "issuer": POOL_ISSUER,
            "audience": CLIENT_ID, "tokenType": "JWT",
        },
    }
    assert mod.fetched_jwks_urls == [POOL_JWKS_URL]


def _pool_and_idp_authorizer(pool_kid="pool-kid-1", idp_kid="idp-kid-1"):
    return _load_authorizer({POOL_JWKS_URL: _jwks(_jwk("pool", pool_kid)),
                             IDP_JWKS_URL: _jwks(_jwk("idp", idp_kid))},
                            whitelist=f" {IDP_ISSUER} ,")


def test_valid_whitelisted_issuer_jwt_with_allowed_aud_is_allowed():
    mod = _pool_and_idp_authorizer()

    policy = mod.handler(_event(_sign(_idp_claims(), "idp", "idp-kid-1")), None)

    assert _effect(policy) == "Allow"
    assert policy["principalId"] == "fake-idp-user"
    assert policy["context"]["issuer"] == IDP_ISSUER
    assert policy["context"]["role"] == "Operator"
    assert mod.TRUSTED_ISSUERS == ((POOL_ISSUER, POOL_JWKS_URL), (IDP_ISSUER, IDP_JWKS_URL))


def test_issuer_whose_jwks_fetch_fails_is_skipped():
    # The pool's JWKS can't be fetched; the next trusted issuer still verifies.
    mod = _load_authorizer({IDP_JWKS_URL: _jwks(_jwk("idp", "idp-kid-1"))},
                           whitelist=IDP_ISSUER)

    policy = mod.handler(_event(_sign(_idp_claims(), "idp", "idp-kid-1")), None)

    assert _effect(policy) == "Allow"
    assert mod.fetched_jwks_urls == [POOL_JWKS_URL, IDP_JWKS_URL]


# --------------------------------------------------------------------------- #
# Denied: the deployed shape (user pool + one app client)
# --------------------------------------------------------------------------- #
_OTHER_POOL_ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/us-east-1_OTHERPOOL"
_SUBSTRING_ISSUER = f"https://attacker.example.com/cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"


def _role_changed_after_signing():
    compact = _sign(_id_claims())
    return _with_payload(compact, {**_payload(compact), "custom:role": "PortalAdmin"})


_POOL_DENIALS = [
    pytest.param(lambda: _flip_signature_byte(_sign(_id_claims())),
                 "Invalid token signature", id="flipped-signature-byte"),
    pytest.param(_role_changed_after_signing, "Invalid token signature",
                 id="role-changed-after-signing"),
    pytest.param(lambda: _sign(_id_claims({"exp": int(time.time()) - 60,
                                           "iat": int(time.time()) - 960})),
                 "Token has expired", id="expired"),
    pytest.param(lambda: _sign(_id_claims(drop=("exp",))),
                 "Token missing required claim: exp", id="no-exp"),
    pytest.param(lambda: _sign(_id_claims({"iss": _OTHER_POOL_ISSUER})),
                 "Invalid token issuer", id="iss-of-another-pool"),
    pytest.param(lambda: _sign(_id_claims({"iss": _SUBSTRING_ISSUER})),
                 "Invalid token issuer", id="iss-contains-pool-path"),
    pytest.param(lambda: _sign(_id_claims({"aud": "fake-other-client-id"})),
                 "Invalid token audience", id="wrong-aud"),
    pytest.param(lambda: _sign(_id_claims(drop=("aud",))),
                 "Token missing required claim: aud", id="no-aud"),
    pytest.param(lambda: _sign(_id_claims(drop=("sub",))),
                 "Token missing required claim: sub", id="no-sub"),
    pytest.param(lambda: _sign(_id_claims({"token_use": "access", "client_id": CLIENT_ID},
                                          drop=("aud",))),
                 "Token missing required claim: aud", id="access-jwt-allowed-client-id"),
    pytest.param(lambda: _sign(_id_claims({"token_use": "access"})),
                 "Token is not an ID token", id="access-jwt-with-allowed-aud"),
    pytest.param(lambda: _unsigned(_id_claims()), "Invalid token: ", id="alg-none"),
    pytest.param(lambda: _hs256(_id_claims()), "Invalid token: ", id="hs256"),
]


@pytest.mark.parametrize("build, reason", _POOL_DENIALS)
def test_pool_jwt_failing_a_check_is_denied(build, reason, caplog):
    mod = _pool_authorizer()

    policy = mod.handler(_event(build()), None)

    _assert_denied(policy)
    assert any(reason in line for line in _warnings(caplog)), _warnings(caplog)


def test_whitelisted_iss_on_a_kid_only_the_pool_publishes_is_denied(caplog):
    mod = _pool_and_idp_authorizer()

    policy = mod.handler(_event(_sign(_idp_claims(), "pool", "pool-kid-1")), None)

    _assert_denied(policy)
    assert any("Invalid token issuer" in line for line in _warnings(caplog))


def test_kid_collision_tries_each_trusted_issuer_holding_the_kid(caplog):
    mod = _pool_and_idp_authorizer(pool_kid="shared-kid", idp_kid="shared-kid")

    allowed = mod.handler(_event(_sign(_idp_claims(), "idp", "shared-kid")), None)
    assert _effect(allowed) == "Allow"
    assert allowed["principalId"] == "fake-idp-user"

    # Signed by neither issuer's key: both verifications fail on the signature.
    denied = mod.handler(_event(_sign(_idp_claims(), "stranger", "shared-kid")), None)
    _assert_denied(denied)
    assert any("Invalid token signature" in line for line in _warnings(caplog))


def test_unknown_kid_is_denied(caplog):
    mod = _pool_authorizer()

    policy = mod.handler(_event(_sign(_id_claims(), "pool", "fake-unknown-kid")), None)

    _assert_denied(policy)
    assert any("Key not found in any trusted JWKS" in line for line in _warnings(caplog))


# --------------------------------------------------------------------------- #
# Denied: configuration
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("audiences", ["", " , "])
def test_empty_allowed_audiences_denies_every_request(audiences, caplog):
    mod = _pool_authorizer(audiences=audiences)

    assert mod.ALLOWED_AUDIENCES == ()
    assert any("every request will be denied" in line for line in _errors(caplog))
    _assert_denied(mod.handler(_event(_sign(_id_claims())), None))
    assert mod.fetched_jwks_urls == []


def test_http_issuer_whitelist_entry_is_ignored(caplog):
    http_issuer = "http://idp.example.com"
    mod = _load_authorizer(
        {POOL_JWKS_URL: _jwks(_jwk("pool", "pool-kid-1")),
         f"{http_issuer}/.well-known/jwks.json": _jwks(_jwk("idp", "idp-kid-1"))},
        whitelist=http_issuer)

    assert mod.TRUSTED_ISSUERS == ((POOL_ISSUER, POOL_JWKS_URL),)
    assert any(http_issuer in line for line in _errors(caplog))
    policy = mod.handler(_event(_sign(_idp_claims({"iss": http_issuer}), "idp", "idp-kid-1")), None)
    _assert_denied(policy)
    assert mod.fetched_jwks_urls == [POOL_JWKS_URL]


# --------------------------------------------------------------------------- #
# Static: no decode call with signature verification turned off
# --------------------------------------------------------------------------- #
def _is_false(node):
    return isinstance(node, ast.Constant) and node.value is False


def test_module_has_no_decode_with_signature_verification_disabled():
    with open(_AUTHORIZER_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    decode_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "decode" and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "jwt"
    ]
    assert decode_calls, "expected the verified jwt.decode call"

    disabled = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            disabled += [node.lineno for key, value in zip(node.keys, node.values)
                         if isinstance(key, ast.Constant)
                         and key.value == "verify_signature" and _is_false(value)]
        elif (isinstance(node, ast.keyword) and node.arg == "verify_signature"
              and _is_false(node.value)):
            disabled.append(node.value.lineno)
    assert not disabled, f"signature verification disabled at line(s) {disabled}"

    for call in decode_calls:
        algorithms = [kw.value for kw in call.keywords if kw.arg == "algorithms"]
        assert len(algorithms) == 1 and ast.literal_eval(algorithms[0]) == ["RS256"]


# --------------------------------------------------------------------------- #
# Property 1: the authorizer allows only fully verified tokens
# --------------------------------------------------------------------------- #
_CLAIM_NAMES = tuple(sorted(_id_claims()))
_CLAIM_VALUES = st.one_of(
    st.none(), st.booleans(), st.integers(min_value=-(2 ** 53), max_value=2 ** 53),
    st.text(max_size=24), st.lists(st.text(max_size=8), max_size=3),
)


@st.composite
def _claim_edits(draw):
    """Claims replaced or added, and claims removed."""
    changes = draw(st.dictionaries(
        st.one_of(st.sampled_from(_CLAIM_NAMES), st.text(min_size=1, max_size=12)),
        _CLAIM_VALUES, max_size=4))
    drop = draw(st.lists(st.sampled_from(_CLAIM_NAMES), max_size=3, unique=True))
    return changes, drop


# Feature: security-scan-remediation-high, Property 1: The authorizer allows only
# fully verified tokens. Validates: Requirements 6.1, 6.2, 6.3, 6.4
# Runs at Hypothesis's own default example count, taken from its built-in
# "default" profile, because the conftest's portal-fast profile lowers it to 25.
@settings(max_examples=settings.get_profile("default").max_examples, deadline=None)
@given(edit=_claim_edits())
def test_property_changed_claims_under_the_original_signature_are_denied(edit):
    changes, drop = edit
    mod = _pool_authorizer()
    compact = _sign(_id_claims())
    assert _effect(mod.handler(_event(compact), None)) == "Allow"

    original = _payload(compact)
    edited = {**original, **changes}
    for name in drop:
        edited.pop(name, None)
    assume(edited != original)

    _assert_denied(mod.handler(_event(_with_payload(compact, edited)), None))
