"""
Setup_Token service for Station Quick Setup.

Shared by both quick-setup Lambdas (`device_registrations.py` issues tokens,
`quick_setup.py` validates them). The token is an opaque, versioned wire
value:

    dqs1.{registration_id}.{secret}          secret = token_urlsafe(32)

Only a one-way SHA-256 hash of the *secret* is ever persisted (Req 3.6); the
raw token/secret never leaves this module except inside the Setup_Command
returned to the portal user.

Design references: station-quick-setup design "TokenService" component and
Correctness Properties 5-8. This module intentionally holds the
security-critical parsing/validation logic in one place and keeps it easy to
exercise in isolation (the registration loader is injectable).

Requirements: 3.1, 3.2, 3.4, 3.5, 3.6, 3.7
"""
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

import boto3

# Versioned, opaque-to-the-station token prefix.
TOKEN_PREFIX = "dqs1"

# Upper bound on token lifetime (Req 3.1): at most 90 minutes.
TOKEN_TTL_SECONDS = 90 * 60

# Number of "." separated fields in a well-formed token.
_TOKEN_PARTS = 3

# The registrations table is read to resolve the embedded registration id.
REGISTRATIONS_TABLE = os.environ.get("REGISTRATIONS_TABLE")

# Lazily created so importing this module never requires AWS configuration.
_dynamodb = None


def _table():
    """The registrations DynamoDB table resource (created on first use)."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(REGISTRATIONS_TABLE)


class ValidationResult(str, Enum):
    """Outcome of validating a presented Setup_Token.

    - VALID:        secret hash matches, token neither consumed nor expired.
    - EXPIRED:      secret hash matches but the token has passed its
                    expiration time (deliberately distinguishable so the
                    station can print a regenerate instruction, Req 3.3/7.5).
    - INVALID:      malformed token, unknown/deleted registration, wrong
                    secret (including a token superseded by regeneration), or
                    a consumed token. All collapse to this single result so
                    responses cannot distinguish these cases (Req 3.4, 3.5).
    - CHECK_FAILED: a security check could not be evaluated (e.g. a storage
                    error); the caller must reject rather than proceed
                    (Req 3.10 / 3.7 binding cannot be established).
    """

    VALID = "VALID"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"
    CHECK_FAILED = "CHECK_FAILED"


@dataclass(frozen=True)
class TokenValidation:
    """Result of :func:`validate_token`.

    ``registration`` is populated only for :data:`ValidationResult.VALID` and
    :data:`ValidationResult.EXPIRED`, where the caller legitimately needs the
    bound registration (Req 3.7 binding, and the pending->expired transition
    of Req 3.3). It is deliberately ``None`` for INVALID/CHECK_FAILED so no
    handler can leak the existence of a registration behind an invalid token.
    """

    result: ValidationResult
    registration: Optional[dict] = None


def _sha256_hex(value: str) -> str:
    """Lowercase hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_token(registration_id: str, now: Optional[int] = None):
    """Mint a new Setup_Token for a registration.

    Derives a 256-bit secret from a cryptographically secure random source
    (Req 3.2) via ``secrets.token_urlsafe(32)`` and sets the expiration to at
    most 90 minutes after ``now`` (Req 3.1).

    Returns ``(token, token_hash, expires_at)`` where ``token`` is the wire
    value handed to the operator, ``token_hash`` is the SHA-256 hex of the
    *secret* (the only thing persisted, Req 3.6), and ``expires_at`` is an
    epoch-second timestamp.
    """
    if now is None:
        now = int(time.time())
    secret = secrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}.{registration_id}.{secret}"
    return token, _sha256_hex(secret), int(now) + TOKEN_TTL_SECONDS


def parse_token(token: str):
    """Parse a wire token into ``(registration_id, secret)``.

    Returns ``None`` for anything that is not a well-formed
    ``dqs1.{registration_id}.{secret}`` value. The secret produced by
    ``token_urlsafe`` and a uuid4 registration id never contain ".", so a
    well-formed token splits into exactly three non-empty fields.
    """
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != _TOKEN_PARTS:
        return None
    prefix, registration_id, secret = parts
    if prefix != TOKEN_PREFIX or not registration_id or not secret:
        return None
    return registration_id, secret


def validate_token(
    token: str,
    now: int,
    load_registration: Optional[Callable[[str], Optional[dict]]] = None,
) -> TokenValidation:
    """Classify a presented Setup_Token without mutating any state.

    The registration is resolved by the id embedded in the token (a
    primary-key read), which keeps "unknown token" structurally identical to
    "wrong secret" — both fall through to the single INVALID result (Req 3.5).
    Only ``sha256(secret)`` is ever compared, using a constant-time
    comparison (Req 3.6).

    ``load_registration`` is an optional loader taking a registration id and
    returning the item dict (or ``None`` if absent); it defaults to a
    primary-key read of the registrations table. Any exception it raises is
    treated as an unevaluable check and yields CHECK_FAILED (Req 3.7 / 3.10),
    so the caller rejects rather than proceeding with an unverified token.

    This function does not apply the pending->expired transition of Req 3.3;
    that side effect belongs to the request pipeline, which uses the returned
    registration.
    """
    loader = load_registration or _load_registration

    parsed = parse_token(token)
    if parsed is None:
        # Malformed / wrong prefix — indistinguishable from other INVALIDs.
        return TokenValidation(ValidationResult.INVALID)

    registration_id, secret = parsed

    try:
        registration = loader(registration_id)
    except Exception:
        # Any storage/lookup failure: the check could not be evaluated.
        return TokenValidation(ValidationResult.CHECK_FAILED)

    if not registration:
        # Unknown or deleted registration.
        return TokenValidation(ValidationResult.INVALID)

    stored_hash = registration.get("token_hash")
    if not stored_hash:
        return TokenValidation(ValidationResult.INVALID)

    presented_hash = _sha256_hex(secret)
    if not hmac.compare_digest(presented_hash, str(stored_hash)):
        # Wrong secret, or a token superseded by regeneration (hash replaced).
        return TokenValidation(ValidationResult.INVALID)

    # Hash matches this registration's current token from here on.
    # A consumed token collapses to INVALID and must take priority over
    # expiry so a consumed-then-expired token stays indistinguishable from
    # other invalid tokens (Req 3.4 / Property 8).
    consumed_at = registration.get("consumed_at", 0) or 0
    if int(consumed_at) > 0:
        return TokenValidation(ValidationResult.INVALID)

    expires_at = int(registration.get("token_expires_at", 0) or 0)
    if now >= expires_at:
        return TokenValidation(ValidationResult.EXPIRED, registration)

    return TokenValidation(ValidationResult.VALID, registration)


def _load_registration(registration_id: str) -> Optional[dict]:
    """Default loader: primary-key read of the registrations table."""
    response = _table().get_item(Key={"registration_id": registration_id})
    return response.get("Item")
