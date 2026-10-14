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
"""Local_Session_Token issuance and validation (portal-user-manager, D5/D6).

Pure token functions over an injected clock and credential cache:

- ``issue_token``: compact HMAC-SHA256 signed token,
  ``base64url(payload JSON) + "." + base64url(HMAC-SHA256(secret, payload_b64))``
  with payload ``{sub, role, iat, exp: iat + 43200, jti}`` (12-hour validity,
  Requirement 8.2).
- ``validate_token``: rejects malformed structure, bad signatures
  (``hmac.compare_digest``), and expired tokens (``exp <= now``,
  Requirements 8.5, 8.7), then re-checks the credential cache so that an
  account marked disabled (or removed) by sync instantly invalidates every
  outstanding token for it (Requirement 8.6, design decision D6).
- ``get_or_create_secret``: 32 random bytes at
  ``/aws_dda/local_session_secret`` (mode ``0600``), regenerated when the
  file is missing or unreadable.

Only Python stdlib is used (``hmac``, ``hashlib``, ``secrets``, ``base64``,
``json``).
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union

DEFAULT_SECRET_PATH = "/aws_dda/local_session_secret"
SECRET_NUM_BYTES = 32
TOKEN_LIFETIME_SECONDS = 12 * 60 * 60  # 43200 — 12 hours (Requirement 8.2)


@dataclass(frozen=True)
class AuthError:
    """Validation failure. ``reason`` is for logging only — callers must
    return a uniform HTTP 401 regardless of the reason (Requirement 8.7)."""

    reason: str


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _sign(secret: bytes, payload_b64: str) -> bytes:
    return hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()


def issue_token(secret: bytes, username: str, role: str, now: int) -> str:
    """Issue a Local_Session_Token valid for 12 hours from ``now``.

    ``now`` is an injected epoch-seconds clock so callers (and tests)
    control time. Payload: ``{sub, role, iat, exp, jti}`` with
    ``exp = iat + 43200`` (Requirement 8.2).
    """
    payload = {
        "sub": username,
        "role": role,
        "iat": int(now),
        "exp": int(now) + TOKEN_LIFETIME_SECONDS,
        "jti": uuid.uuid4().hex,
    }
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature_b64 = _b64url_encode(_sign(secret, payload_b64))
    return payload_b64 + "." + signature_b64


def _lookup_account(cache: Any, username: str) -> Optional[Any]:
    """Find ``username`` in the injected credential cache.

    Accepts either an object exposing ``get_account(username)`` (the
    ``credential_cache`` module's store) or a plain mapping — either
    ``{username: account}`` directly or the cache-file shape
    ``{"accounts": {username: account}}``.
    """
    if hasattr(cache, "get_account"):
        return cache.get_account(username)
    if isinstance(cache, Mapping):
        accounts = cache.get("accounts")
        if not isinstance(accounts, Mapping):
            accounts = cache
        return accounts.get(username)
    return None


def _is_enabled(account: Any) -> bool:
    if isinstance(account, Mapping):
        return account.get("enabled") is True
    return getattr(account, "enabled", False) is True


def validate_token(
    secret: bytes, token: str, now: int, cache: Any
) -> Union[Any, AuthError]:
    """Validate a Local_Session_Token against ``secret``, ``now`` and ``cache``.

    Returns the account record from ``cache`` on success, or an
    :class:`AuthError` describing why validation failed. Rejects, in order:
    malformed structure, bad signature (constant-time comparison),
    ``exp <= now`` (Requirements 8.5, 8.7), then account absent or disabled
    in the credential cache (Requirement 8.6, D6 revocation-on-disable).
    """
    if not isinstance(token, str):
        return AuthError("malformed token: not a string")
    segments = token.split(".")
    if len(segments) != 2 or not segments[0] or not segments[1]:
        return AuthError("malformed token: expected two segments")
    payload_b64, signature_b64 = segments

    # Signature check first, before trusting any payload content.
    try:
        presented_signature = _b64url_decode(signature_b64)
    except (binascii.Error, ValueError):
        return AuthError("malformed token: signature not base64url")
    expected_signature = _sign(secret, payload_b64)
    if not hmac.compare_digest(presented_signature, expected_signature):
        return AuthError("invalid signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return AuthError("malformed token: payload not base64url JSON")
    if not isinstance(payload, dict):
        return AuthError("malformed token: payload is not an object")

    sub = payload.get("sub")
    exp = payload.get("exp")
    if not isinstance(sub, str) or not sub:
        return AuthError("malformed token: missing sub")
    if not isinstance(exp, int) or isinstance(exp, bool):
        return AuthError("malformed token: missing exp")
    if exp <= now:
        return AuthError("token expired")

    # Re-check the credential cache on every validation so a disabled flag
    # arriving by sync instantly revokes all outstanding tokens (8.6, D6).
    account = _lookup_account(cache, sub)
    if account is None:
        return AuthError("account not present in credential cache")
    if not _is_enabled(account):
        return AuthError("account disabled")
    return account


def get_or_create_secret(path: str = DEFAULT_SECRET_PATH) -> bytes:
    """Return the HMAC signing secret, creating it on first use.

    Reads ``path``; when the file is missing, unreadable, or does not hold
    exactly 32 bytes, generates 32 fresh random bytes
    (``secrets.token_bytes``) and writes them with file mode ``0600``.
    """
    try:
        with open(path, "rb") as secret_file:
            secret = secret_file.read()
        if len(secret) == SECRET_NUM_BYTES:
            return secret
    except OSError:
        pass

    secret = secrets.token_bytes(SECRET_NUM_BYTES)
    # O_TRUNC handles the regenerate-over-corrupt-file case; mode 0600 is
    # applied at creation so the secret is never world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return secret
