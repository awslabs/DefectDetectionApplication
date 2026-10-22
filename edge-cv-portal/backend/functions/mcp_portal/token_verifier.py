"""Cognito access-token verification for the Portal_MCP_Server.

`CognitoAccessTokenVerifier` is the SDK `TokenVerifier` behind
`BearerAuthBackend`: the single validator of the MCP HTTP API, which has no
gateway authorizer (design "Why no gateway authorizer"). It accepts an
access token of the existing user pool only when the RS256 signature
verifies against the pool's JWKS, `iss` equals `COGNITO_ISSUER`, the token
has not expired, `token_use == 'access'`, `client_id` is the MCP app client,
the required scope is granted, and `aud` (when present) names one of the
MCP endpoint URLs. Any failure returns None, and the SDK answers 401.

When the signature and the issuer verified but a later claim check failed,
the failure is recorded in `CURRENT_AUTH_FAILURE` with the token's `sub`, so
`AuthFailureAuditMiddleware` can write the marked `authentication_failed`
audit entry (Requirement 4.7). Tokens that are unsigned, unparseable or from
another issuer record nothing: they name no verifiable principal.

The token itself is never logged; only `sub`, the failure class and (when
the pool's key set cannot be fetched) the header's `kid` are.
"""
import contextvars
import logging
import os
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)

REQUIRED_CLAIMS = ['exp', 'iat', 'iss', 'sub', 'token_use', 'client_id',
                   'scope', 'jti']


@dataclass(frozen=True)
class AuthFailure:
    """A claim-level rejection of a token whose signature and issuer verified."""
    sub: str
    reason: str
    client_id: Optional[str] = None


# Request-scoped: reset by AuthFailureAuditMiddleware at the start of each
# request, read by it when the response is a 401.
CURRENT_AUTH_FAILURE: contextvars.ContextVar = contextvars.ContextVar(
    'CURRENT_AUTH_FAILURE', default=None)


def _csv(value: Optional[str]) -> tuple:
    return tuple(part.strip() for part in (value or '').split(',') if part.strip())


def _unverified_kid(token: str) -> Optional[str]:
    """The `kid` of the token's header, or None when the header is unreadable."""
    try:
        kid = jwt.get_unverified_header(token).get('kid')
    except Exception:  # noqa: BLE001 - only used for the log line
        return None
    return kid if isinstance(kid, str) else None


class CognitoAccessTokenVerifier(TokenVerifier):
    """Verify Cognito access tokens issued to the MCP app client."""

    def __init__(self, issuer: Optional[str] = None,
                 client_id: Optional[str] = None,
                 required_scope: Optional[str] = None,
                 allowed_audiences: Optional[Iterable[str]] = None,
                 jwks_client=None):
        self.issuer = issuer if issuer is not None \
            else os.environ.get('COGNITO_ISSUER', '')
        self.client_id = client_id if client_id is not None \
            else os.environ.get('MCP_CLIENT_ID', '')
        self.required_scope = required_scope if required_scope is not None \
            else os.environ.get('MCP_REQUIRED_SCOPE', '')
        self.allowed_audiences = tuple(allowed_audiences) \
            if allowed_audiences is not None \
            else _csv(os.environ.get('MCP_ALLOWED_AUDIENCES'))
        # Keys are cached per container; constructing the client fetches nothing.
        self.jwks_client = jwks_client if jwks_client is not None \
            else jwt.PyJWKClient(f'{self.issuer}/.well-known/jwks.json',
                                 cache_keys=True)

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        return self.verify(token)

    def verify(self, token: str) -> Optional[AccessToken]:
        """Synchronous body of `verify_token`."""
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            # Signature, issuer and the required claims. `exp` is checked
            # below rather than by PyJWT so that an expired token whose
            # signature and issuer verified is classified as such.
            payload = jwt.decode(
                token, signing_key.key, algorithms=['RS256'],
                issuer=self.issuer,
                options={'verify_aud': False, 'verify_exp': False,
                         'require': REQUIRED_CLAIMS})
        except jwt.PyJWKClientConnectionError:
            # The pool's key set could not be fetched (transient): every
            # token is a 401 until it can, so this is logged at ERROR with
            # the header's kid. Nothing is recorded for the audit entry.
            logger.error('MCP access token rejected: JWKS fetch failed '
                         'for kid %s', _unverified_kid(token))
            return None
        except Exception as error:  # noqa: BLE001 - any decode failure is a 401
            logger.info('MCP access token rejected before claim checks: %s',
                        type(error).__name__)
            return None

        sub = str(payload.get('sub'))
        client_id = payload.get('client_id')
        reason = self._claim_failure(payload)
        if reason is not None:
            CURRENT_AUTH_FAILURE.set(AuthFailure(
                sub=sub, reason=reason,
                client_id=client_id if isinstance(client_id, str) else None))
            logger.info('MCP access token rejected for sub %s: %s', sub, reason)
            return None

        scopes = str(payload['scope']).split()
        return AccessToken(
            token=token, client_id=client_id, scopes=scopes,
            resource=self._matching_audience(payload.get('aud')),
            expires_at=int(payload['exp']), subject=sub, claims=payload)

    def _claim_failure(self, payload: dict) -> Optional[str]:
        """The first failed claim check, or None when all pass."""
        try:
            expired = int(payload['exp']) <= int(time.time())
        except (TypeError, ValueError):
            return 'expired'
        if expired:
            return 'expired'
        if payload.get('token_use') != 'access':
            return 'token_use'
        if payload.get('client_id') != self.client_id:
            return 'client_id'
        scope = payload.get('scope')
        if not isinstance(scope, str) or self.required_scope not in scope.split():
            return 'scope'
        if 'aud' in payload and self._matching_audience(payload['aud']) is None:
            return 'audience'
        return None

    def _matching_audience(self, aud) -> Optional[str]:
        """The first `aud` value that is an allowed endpoint URL, or None."""
        if aud is None:
            return None
        values = [aud] if isinstance(aud, str) else aud
        if not isinstance(values, (list, tuple)):
            return None
        for value in values:
            if isinstance(value, str) and value in self.allowed_audiences:
                return value
        return None
