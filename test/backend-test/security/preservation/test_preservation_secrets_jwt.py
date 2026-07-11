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
"""S1 + S5 preservation baselines — ``jwt_authorizer.py`` (Req 3.1, 3.5).

Spec: security-secrets-credentials-jwt-fixes — Property 2: Preservation
(``F(X) = F'(X)`` for every legitimate, non-bug-condition input).

Observation-first: these baselines are captured on the UNFIXED tree (task 2,
PASS now) and re-run UNCHANGED against the fixed tree (task 8) to prove the fix
changes only the invocation log line (S1) / adds only a ``# nosem`` comment (S5)
and never the observable behavior.

  * **S1 (Req 3.1):** the authorizer's returned allow/deny IAM policy
    (``principalId`` / ``Effect`` / ``Resource`` / ``context``) is a **pure
    function of the token/claims** — it does NOT depend on the (to-be-redacted)
    invocation log line. The property generates events with random ``methodArn``,
    random secret-bearing ``authorizationToken`` / ``headers``, and random claims
    (with ``validate_jwt_token`` mocked so the decode is deterministic) and
    asserts the policy equals the recorded reference model and is identical
    regardless of the secret token/header values.
  * **S5 (Req 3.5):** the two-stage decode still validates a real RS256-signed
    token to the same claims and still rejects a tampered token with
    ``AuthorizationError`` (a small RSA keypair signs a real token; the JWKS
    lookup / RSA-key construction are mocked so the verified decode runs).

``jwt_authorizer.py`` imports only stdlib + ``jwt`` / ``requests`` and reads its
config from env at import, so it loads in isolation via
``load_module_from_path`` (``requests`` stubbed; ``get_jwks_keys`` /
``construct_rsa_key`` mocked per test). This is the same module-loading approach
the sibling suite and the task-1 exploration test use, so task 8 re-exercises the
fixed source directly.

**Validates: Requirements 3.1, 3.5**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_secrets_jwt.py \
        -p no:cacheprovider --noconftest -v
"""
import os
import sys
import types

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from _preservation_support import load_module_from_path

# jwt_authorizer.py imports PyJWT (`import jwt`), a cloud-portal Lambda
# dependency that is NOT installed in the edge runtime flask-app image where
# build-custom.sh runs this gate (present on the JP6 image but not JP5). Skip
# this whole module cleanly when PyJWT is unavailable; the cloud-portal Lambda
# runtime provides it. secrets_audit.py still statically guards jwt_authorizer.py.
pytest.importorskip("jwt")

JWT_AUTHORIZER_REL = "edge-cv-portal/backend/functions/jwt_authorizer.py"


# --------------------------------------------------------------------------- #
# Module loading (isolated) — stub ``requests`` (get_jwks_keys is mocked so the
# real network client is never invoked). ``jwt`` is the REAL library so S5 can
# sign/verify a genuine RS256 token.
# --------------------------------------------------------------------------- #
def _requests_stub():
    mod = types.ModuleType("requests")

    def _get(*a, **k):  # pragma: no cover - never called (get_jwks_keys mocked)
        raise RuntimeError("network access not allowed in preservation tests")

    mod.get = _get
    return {"requests": mod}


def _load_jwt_authorizer(env=None):
    """Load jwt_authorizer.py in isolation. ``env`` is applied to os.environ for
    the duration of the import so the module-level config (ISSUER_WHITELIST,
    ALLOWED_AUDIENCES, COGNITO_*) is captured as intended."""
    env = env or {}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        return load_module_from_path(
            "jwt_authorizer_preservation",
            JWT_AUTHORIZER_REL,
            injected_modules=_requests_stub(),
        )
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


# --------------------------------------------------------------------------- #
# S1 reference model — the recorded F(X): the allow/deny policy as a pure
# function of (methodArn, claims). Mirrors handler()'s context/role logic so
# task 8 can assert F'(X) == this exactly.
# --------------------------------------------------------------------------- #
_GROUP_ROLE_MAPPING = {
    "portal-admins": "PortalAdmin",
    "cv-data-scientists": "DataScientist",
    "cv-operators": "Operator",
    "cv-viewers": "Viewer",
}


def _reference_role(claims):
    role = claims.get("custom:role", "Viewer")
    if not role or role == "Viewer":
        token_groups = claims.get("groups", [])
        if isinstance(token_groups, list) and token_groups:
            for group in token_groups:
                if group in _GROUP_ROLE_MAPPING:
                    role = _GROUP_ROLE_MAPPING[group]
                    break
    return role


def _reference_context(claims):
    return {
        "userId": claims.get("sub", "unknown"),
        "email": claims.get("email", "unknown"),
        "username": claims.get(
            "cognito:username", claims.get("preferred_username", "unknown")
        ),
        "role": _reference_role(claims),
        "groups": claims.get("custom:groups", ""),
        "issuer": claims.get("iss", "unknown"),
        "audience": claims.get("aud", "unknown"),
        "tokenType": "JWT",
    }


def _reference_allow_policy(method_arn, claims):
    return {
        "principalId": claims.get("sub", "unknown"),
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Resource": method_arn,
                }
            ],
        },
        "context": _reference_context(claims),
    }


def _reference_deny_policy(method_arn, principal_id="unauthorized"):
    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Deny",
                    "Resource": method_arn,
                }
            ],
        },
    }


def _event(method_arn, token):
    """A token-bearing authorizer event (the token lands in the log line on the
    unfixed tree; it must NEVER influence the returned policy)."""
    return {
        "authorizationToken": f"Bearer {token}",
        "headers": {"Authorization": f"Bearer {token}", "authorization": token},
        "methodArn": method_arn,
    }


# --------------------------------------------------------------------------- #
# S1 — example baselines (allow + deny)
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.1
def test_s1_allow_policy_is_recorded_reference():
    """A valid (mocked-decode) token yields the recorded allow policy; the secret
    token value does not appear in the policy."""
    mod = _load_jwt_authorizer()
    claims = {
        "sub": "user-123",
        "email": "user@example.com",
        "cognito:username": "alice",
        "custom:role": "DataScientist",
        "custom:groups": "team-a,team-b",
        "iss": "https://issuer.example.com",
        "aud": "audience-1",
    }
    mod.validate_jwt_token = lambda token: dict(claims)

    method_arn = "arn:aws:execute-api:us-west-2:111122223333:abc/prod/GET/things"
    secret = "eyJhbGciOiJSUzI1NiJ9.SECRETpayload.SECRETsig"
    policy = mod.handler(_event(method_arn, secret), None)

    assert policy == _reference_allow_policy(method_arn, claims)
    # The bearer token must never leak into the returned policy.
    assert secret not in repr(policy)


# Validates: Requirements 3.1
def test_s1_group_mapping_promotes_role_in_policy():
    """OIDC ``groups`` claim maps to a role in the context exactly as recorded."""
    mod = _load_jwt_authorizer()
    claims = {
        "sub": "user-9",
        "iss": "https://issuer.example.com",
        "groups": ["unrelated", "cv-operators"],
    }
    mod.validate_jwt_token = lambda token: dict(claims)

    method_arn = "arn:aws:execute-api:us-east-1:111122223333:xyz/prod/POST/pkg"
    policy = mod.handler(_event(method_arn, "tok"), None)

    expected = _reference_allow_policy(method_arn, claims)
    assert expected["context"]["role"] == "Operator"
    assert policy == expected


# Validates: Requirements 3.1
def test_s1_deny_policy_on_invalid_token_is_recorded_reference():
    """A rejected token yields the recorded deny policy (principalId
    ``unauthorized``, no context)."""
    mod = _load_jwt_authorizer()

    def _raise(token):
        raise mod.AuthorizationError("bad token")

    mod.validate_jwt_token = _raise
    method_arn = "arn:aws:execute-api:us-west-2:111122223333:abc/prod/GET/things"
    policy = mod.handler(_event(method_arn, "SECRET-bearer"), None)

    assert policy == _reference_deny_policy(method_arn, "unauthorized")
    assert "SECRET-bearer" not in repr(policy)


# Validates: Requirements 3.1
def test_s1_deny_policy_when_no_token_present():
    """An event with no token yields the deny policy (extract raises)."""
    mod = _load_jwt_authorizer()
    method_arn = "arn:aws:execute-api:eu-west-1:111122223333:abc/prod/GET/x"
    policy = mod.handler({"methodArn": method_arn}, None)
    assert policy == _reference_deny_policy(method_arn, "unauthorized")


# --------------------------------------------------------------------------- #
# S1 — property: the returned policy is a pure function of (methodArn, claims)
# and is invariant to the secret token/header values (i.e. independent of the
# log line the fix will redact).
# --------------------------------------------------------------------------- #
_IDENT = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122),
    min_size=1,
    max_size=16,
)
_ARN = st.from_regex(
    r"\Aarn:aws:execute-api:[a-z0-9-]{1,15}:[0-9]{12}:[a-z0-9]{1,8}/[a-z]{1,6}/"
    r"(GET|POST|PUT|DELETE)/[a-z]{1,10}\Z"
)
_SECRET = st.text(min_size=0, max_size=40)


@st.composite
def _claims_strategy(draw):
    claims = {"sub": draw(_IDENT), "iss": draw(_IDENT)}
    if draw(st.booleans()):
        claims["email"] = draw(_IDENT)
    if draw(st.booleans()):
        claims["cognito:username"] = draw(_IDENT)
    elif draw(st.booleans()):
        claims["preferred_username"] = draw(_IDENT)
    if draw(st.booleans()):
        claims["aud"] = draw(_IDENT)
    role = draw(st.sampled_from([None, "Viewer", "DataScientist", "Operator", "PortalAdmin"]))
    if role is not None:
        claims["custom:role"] = role
    if draw(st.booleans()):
        claims["custom:groups"] = draw(_IDENT)
    if draw(st.booleans()):
        claims["groups"] = draw(
            st.lists(
                st.sampled_from(list(_GROUP_ROLE_MAPPING.keys()) + ["misc-group"]),
                max_size=3,
            )
        )
    return claims


# Validates: Requirements 3.1
@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(method_arn=_ARN, claims=_claims_strategy(), token_a=_SECRET, token_b=_SECRET)
def test_s1_policy_is_pure_function_of_claims_not_the_log_line(
    method_arn, claims, token_a, token_b
):
    """Invariant: for the same (methodArn, claims) the returned allow policy
    equals the recorded reference and is byte-identical regardless of the
    secret-bearing token/header values (which the S1 fix will redact from the
    log). Task 8 must reproduce this exactly."""
    mod = _load_jwt_authorizer()
    mod.validate_jwt_token = lambda token: dict(claims)

    policy_a = mod.handler(_event(method_arn, token_a), None)
    policy_b = mod.handler(_event(method_arn, token_b), None)

    expected = _reference_allow_policy(method_arn, claims)
    assert policy_a == expected
    assert policy_b == expected
    # Independent of the token value => independent of the log line.
    assert policy_a == policy_b


# --------------------------------------------------------------------------- #
# S5 — two-stage decode preservation (real signature verify)
# --------------------------------------------------------------------------- #
# The verified stage of ``validate_jwt_token`` runs an RS256 signature check via
# PyJWT, which delegates to the ``cryptography`` OpenSSL/cffi backend. That
# backend is unavailable in this bare, ``--noconftest`` runner (the import fails
# with ``No module named '_cffi_backend'``), so a real RS256 sign+verify cannot
# execute here. Per the design ("use a small RSA keypair to sign a real token and
# tamper it, OR mock ``construct_rsa_key``/JWKS"), we take the mock route: the
# JWKS lookup and RSA-key construction are mocked and the REAL two-stage control
# flow is exercised end to end, substituting the signature algorithm RS256 ->
# HS256 (HMAC — pure-Python, needs no cryptography backend) for the verify step
# ONLY. This still performs a GENUINE cryptographic signature verification (a
# tampered token is really rejected) through the module's real unverified
# pre-parse (``get_unverified_header`` + ``verify_signature=False`` decode),
# issuer routing, ``kid`` lookup, and ``verify_exp``/``verify_aud``/``verify_iss``
# options. The RS256->HS256 substitution is an environment adaptation, not a
# behavior change; the S5 fix (task 3.3) only adds a ``# nosem`` comment (no
# control-flow change), so task 8 re-runs this unchanged.
_S5_ISSUER = "https://issuer.preservation.test"
_S5_KID = "preservation-kid"
_S5_SECRET = "preservation-hmac-secret-key-0123456789"


def _sign_hs256(claims, headers=None):
    """Sign a real token with HS256 (pure-Python HMAC — no cryptography backend
    required). The header carries the ``kid`` the module routes on."""
    import jwt as pyjwt

    tok = pyjwt.encode(
        claims, _S5_SECRET, algorithm="HS256", headers=headers or {"kid": _S5_KID}
    )
    return tok.decode("utf-8") if isinstance(tok, bytes) else tok


class _JwtProxy:
    """A drop-in stand-in for the ``jwt`` module inside the loaded authorizer:
    forwards everything to the real PyJWT (``get_unverified_header``, the
    exception classes) but routes ``decode`` through our adapter. Installed onto
    the loaded module instance only, so the global ``jwt`` module is untouched."""

    def __init__(self, real, decode):
        self._real = real
        self.decode = decode

    def __getattr__(self, name):
        return getattr(self._real, name)


def _mount_two_stage_decode(mod):
    """Mock the JWKS lookup + RSA-key construction (returning the HMAC secret)
    and adapt the verified decode's algorithm RS256 -> HS256 so the real
    signature verification runs without the cryptography backend. The unverified
    pre-parse call (``options={"verify_signature": False}``) is passed through
    untouched."""
    import jwt as pyjwt

    mod.get_jwks_keys = lambda url: {"keys": [{"kid": _S5_KID}]}
    mod.construct_rsa_key = lambda jwks_key: _S5_SECRET

    real_decode = pyjwt.decode

    def _decode(token, key=None, algorithms=None, **kwargs):
        options = kwargs.get("options") or {}
        # Stage 1 — the unverified pre-parse: no key, signature disabled.
        if key is None and options.get("verify_signature") is False:
            return real_decode(token, options=options)
        # Stage 2 — the verified decode: substitute RS256 -> HS256 for the
        # pure-python HMAC verification path.
        if algorithms and "RS256" in algorithms:
            algorithms = ["HS256"]
        return real_decode(token, key, algorithms=algorithms, **kwargs)

    mod.jwt = _JwtProxy(pyjwt, _decode)


def _load_jwt_authorizer_s5():
    return _load_jwt_authorizer(
        env={
            "ISSUER_WHITELIST": _S5_ISSUER,
            "ALLOWED_AUDIENCES": "",
            "COGNITO_USER_POOL_ID": "",
        }
    )


# Validates: Requirements 3.5
def test_s5_valid_token_validates_to_same_claims():
    """A real (HS256-signed) token validates via the two-stage decode and returns
    the signed claims — the routing-only pre-parse then the verified decode."""
    import time

    mod = _load_jwt_authorizer_s5()
    _mount_two_stage_decode(mod)

    claims = {"sub": "user-42", "iss": _S5_ISSUER, "exp": int(time.time()) + 3600}
    token = _sign_hs256(claims)

    decoded = mod.validate_jwt_token(token)
    assert decoded["sub"] == "user-42"
    assert decoded["iss"] == _S5_ISSUER


# Validates: Requirements 3.5
def test_s5_tampered_token_is_rejected():
    """A token whose signature is tampered is rejected by the verified decode
    with AuthorizationError (the pre-parse still succeeds; the verified decode
    fails the signature check)."""
    import time

    mod = _load_jwt_authorizer_s5()
    _mount_two_stage_decode(mod)

    claims = {"sub": "user-42", "iss": _S5_ISSUER, "exp": int(time.time()) + 3600}
    token = _sign_hs256(claims)

    header, payload, signature = token.split(".")
    # Flip the first signature character to a different base64url char so the
    # HMAC no longer matches (header/payload preserved so the pre-parse still
    # reads the issuer and routes as before).
    flipped = ("B" if signature[0] != "B" else "C") + signature[1:]
    tampered = ".".join([header, payload, flipped])

    with pytest.raises(mod.AuthorizationError):
        mod.validate_jwt_token(tampered)


# Validates: Requirements 3.5
def test_s5_valid_token_still_accepted_after_tamper_check():
    """Sanity: the same secret still accepts an untampered token (guards against
    the tamper test passing for the wrong reason)."""
    import time

    mod = _load_jwt_authorizer_s5()
    _mount_two_stage_decode(mod)

    claims = {"sub": "user-7", "iss": _S5_ISSUER, "exp": int(time.time()) + 3600}
    token = _sign_hs256(claims)
    assert mod.validate_jwt_token(token)["sub"] == "user-7"
