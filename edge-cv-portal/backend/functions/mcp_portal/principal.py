"""The calling principal of an MCP tool call (design §2 `principal.py` row).

`principal_from_access_token` turns the verified access token of
`CognitoAccessTokenVerifier` into a `Principal`. Its `user` dict is built
exactly as `shared_utils.get_user_from_event` builds it for the synthesized
backend event (`user_id=sub`, the username, the username as the `email`
fallback because an access token carries no `email` claim, and the
Claimed_Role `'Viewer'` because it carries no `custom:role`), so the
pre-dispatch RBAC check and the backing handler see the same user.

The principal's email comes from a per-container cache over
`cognito-idp:AdminGetUser`, keyed by `sub`; a failed lookup yields
`'unknown'` (WARNING) and is not cached. The response also carries the
account's `custom:role` attribute; this module deliberately reads only
`email` (D29 option (A)). Reading `custom:role` is option (C), Backlog 12.

`lookup_email` returns an `EmailLookup(email, failed)` tuple, so a lookup
that failed is told apart from an account without an email attribute (both
read `'unknown'`). `refresh_email` reads the caller's address from the pool
on the call, never from the cache, for the self-protection rule of pipeline
step 5b (spec: .kiro/specs/mcp-user-operations/, design A4).
"""
import collections
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

UNKNOWN = 'unknown'
EMAIL_CACHE_SIZE = 1024

# Patched by tests; created on first use so an import needs no region.
COGNITO = None
_COGNITO_LOCK = threading.Lock()
_EMAILS: 'collections.OrderedDict[str, str]' = collections.OrderedDict()
_EMAILS_LOCK = threading.Lock()


def _cognito():
    global COGNITO
    if COGNITO is None:
        with _COGNITO_LOCK:
            if COGNITO is None:
                import boto3
                from botocore.config import Config
                COGNITO = boto3.client('cognito-idp', config=Config(
                    connect_timeout=2, read_timeout=5,
                    retries={'total_max_attempts': 2}))
    return COGNITO


def _display_identity(email: Optional[str], username: Optional[str],
                      user_id: Optional[str]) -> str:
    """`shared_utils._display_identity`: the email, else the username, else
    the sub, never the literal 'unknown' when a real identifier exists."""
    for candidate in (email, username, user_id):
        if candidate and str(candidate).strip() and str(candidate).strip().lower() != UNKNOWN:
            return str(candidate).strip()
    return UNKNOWN


@dataclass(frozen=True)
class Principal:
    sub: str
    username: str
    email: str                      # from AdminGetUser, or 'unknown'
    claims: dict = field(repr=False)  # the verified payload, verbatim

    @property
    def user(self) -> dict:
        """The `get_user_from_event` dict of the synthesized event."""
        claims = self.claims
        user_id = claims.get('sub', UNKNOWN)
        username = claims.get('cognito:username', claims.get('username', UNKNOWN))
        return {
            'user_id': user_id,
            'email': _display_identity(claims.get('email'), username, user_id),
            'username': username,
            'role': claims.get('custom:role', 'Viewer'),
        }


class EmailLookup(NamedTuple):
    """The result of `lookup_email`. `failed` is True only when
    `AdminGetUser` raised on this call; `email` is then 'unknown' and the
    call cached nothing."""
    email: str      # the account's email attribute, or 'unknown'
    failed: bool


def lookup_email(sub: str, username: str, refresh: bool = False) -> EmailLookup:
    """The account's `email` attribute (cached per `sub`), or 'unknown'.

    With `refresh` the cache is not read: the User_Pool is asked on this
    call, and a successful answer rewrites the cache entry."""
    if not refresh:
        with _EMAILS_LOCK:
            if sub in _EMAILS:
                _EMAILS.move_to_end(sub)
                return EmailLookup(_EMAILS[sub], False)
    try:
        response = _cognito().admin_get_user(
            UserPoolId=os.environ['COGNITO_USER_POOL_ID'], Username=username)
        email = next((a.get('Value') for a in response.get('UserAttributes', ())
                      if a.get('Name') == 'email'), None)
    except Exception as error:  # noqa: BLE001 - attribution degrades, never fails
        logger.warning('Email lookup failed for sub %s: %s', sub, type(error).__name__)
        return EmailLookup(UNKNOWN, True)
    email = email if isinstance(email, str) and email.strip() else UNKNOWN
    with _EMAILS_LOCK:
        _EMAILS[sub] = email
        _EMAILS.move_to_end(sub)
        while len(_EMAILS) > EMAIL_CACHE_SIZE:
            _EMAILS.popitem(last=False)
    return EmailLookup(email, False)


def refresh_email(principal: Principal) -> EmailLookup:
    """The caller's address read from the User_Pool on this call (pipeline
    step 5b). The one caller of `refresh=True`; it lives here so a test
    patch of `lookup_email` covers step 1 and step 5b alike."""
    return lookup_email(principal.sub, principal.username, refresh=True)


def clear_email_cache() -> None:
    with _EMAILS_LOCK:
        _EMAILS.clear()


def principal_from_claims(claims: dict) -> Principal:
    """The `Principal` of verified ID-token claims, as the REST authorizer
    hands them to every handler (`requestContext.authorizer.claims`).

    Used by the Portal assistant (portal-ai-assistant design §2): the web
    UI's ID token carries `email`, so no `AdminGetUser` call is needed
    unless the claim is missing.
    """
    claims = dict(claims or {})
    sub = claims.get('sub') or UNKNOWN
    username = claims.get('cognito:username') or claims.get('username') or sub
    email = claims.get('email')
    if not (isinstance(email, str) and email.strip()):
        email = lookup_email(sub, username).email
    return Principal(sub=sub, username=username, email=email, claims=claims)


def principal_from_access_token(token) -> Principal:
    """The `Principal` of an `AccessToken` returned by the verifier (its
    `claims` hold the verified payload)."""
    claims = getattr(token, 'claims', None)
    if not isinstance(claims, dict):
        claims = {}
    sub = claims.get('sub') or getattr(token, 'subject', None) or UNKNOWN
    username = claims.get('username') or claims.get('cognito:username') or sub
    return Principal(sub=sub, username=username,
                     email=lookup_email(sub, username).email, claims=claims)
