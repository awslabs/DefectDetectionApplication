"""
Custom JWT Authorizer for Edge CV Portal API Gateway
Provides flexible JWT validation with support for multiple identity providers
"""
import json
import logging
import os
import base64
import jwt
import requests
from typing import Dict, Any, Optional, Tuple
from functools import lru_cache
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _csv_env(name: str) -> Tuple[str, ...]:
    """Return an environment variable's comma-separated entries, trimmed, with
    empty entries dropped."""
    entries = (entry.strip() for entry in os.environ.get(name, '').split(','))
    return tuple(entry for entry in entries if entry)


# Configuration from environment variables, read once at import
COGNITO_USER_POOL_ID = os.environ.get('COGNITO_USER_POOL_ID')
COGNITO_REGION = os.environ.get('COGNITO_REGION', 'us-east-1')
# App client ids the Portal allows; a token's `aud` must name one of them.
ALLOWED_AUDIENCES = _csv_env('ALLOWED_AUDIENCES')
# Exact `https://` issuer strings of additional identity providers.
ISSUER_WHITELIST = _csv_env('ISSUER_WHITELIST')

# Cache for JWKS keys (1 hour TTL)
JWKS_CACHE_TTL = 3600


def _safe_event_metadata(event: Dict) -> Dict[str, Any]:
    """Return only non-sensitive authorizer-event fields for logging.

    An API Gateway authorizer event carries the bearer token in
    ``authorizationToken`` and/or ``headers.Authorization``. Logging the whole
    event writes the token verbatim into CloudWatch Logs, so this helper
    extracts ONLY the non-sensitive fields — ``methodArn`` (always) and
    ``requestContext.requestId`` when present — and NEVER references
    ``authorizationToken``, ``headers``, or any other token-bearing field.
    """
    metadata: Dict[str, Any] = {"methodArn": event.get("methodArn")}
    request_context = event.get("requestContext")
    if isinstance(request_context, dict) and request_context.get("requestId"):
        metadata["requestId"] = request_context["requestId"]
    return metadata


class AuthorizationError(Exception):
    """Custom exception for authorization errors"""
    pass


@lru_cache(maxsize=10)
def get_jwks_keys(jwks_url: str) -> Dict:
    """
    Fetch and cache JWKS keys from identity provider
    
    Args:
        jwks_url: URL to fetch JWKS keys from
        
    Returns:
        Dictionary containing JWKS keys
    """
    try:
        logger.info(f"Fetching JWKS keys from: {jwks_url}")
        response = requests.get(jwks_url, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error fetching JWKS keys: {str(e)}")
        raise AuthorizationError(f"Failed to fetch JWKS keys: {str(e)}")


def get_cognito_jwks_url(user_pool_id: str, region: str) -> str:
    """Generate Cognito JWKS URL"""
    return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/jwks.json"


# The configured user pool's issuer, when a pool is configured
COGNITO_ISSUER = (
    f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
    if COGNITO_USER_POOL_ID else None
)


def _trusted_issuers() -> Tuple[Tuple[str, str], ...]:
    """The trusted issuers, in lookup order, as (issuer, jwks_url) pairs: the
    configured user pool first, then each `https://` ISSUER_WHITELIST entry."""
    issuers = []
    if COGNITO_ISSUER:
        issuers.append((COGNITO_ISSUER, get_cognito_jwks_url(COGNITO_USER_POOL_ID, COGNITO_REGION)))
    for issuer in ISSUER_WHITELIST:
        if not issuer.startswith('https://'):
            # Its signing keys would be fetched without TLS.
            logger.error(f"Ignoring ISSUER_WHITELIST entry that is not an https:// URL: {issuer}")
            continue
        issuers.append((issuer, f"{issuer}/.well-known/jwks.json"))
    return tuple(issuers)


TRUSTED_ISSUERS = _trusted_issuers()

if not ALLOWED_AUDIENCES or not TRUSTED_ISSUERS:
    logger.error(
        "JWT authorizer configuration is incomplete (ALLOWED_AUDIENCES and at least "
        "one trusted issuer are required): every request will be denied"
    )


def find_jwks_key(jwks: Dict, kid: str) -> Optional[Dict]:
    """
    Find specific key in JWKS by key ID
    
    Args:
        jwks: JWKS dictionary
        kid: Key ID to find
        
    Returns:
        Key dictionary if found, None otherwise
    """
    for key in jwks.get('keys', []):
        if key.get('kid') == kid:
            return key
    return None


def construct_rsa_key(jwks_key: Dict) -> str:
    """
    Construct RSA public key from JWKS key
    
    Args:
        jwks_key: JWKS key dictionary
        
    Returns:
        RSA public key in PEM format
    """
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.backends import default_backend
        
        # Decode base64url encoded values
        n = base64.urlsafe_b64decode(jwks_key['n'] + '==')  # Add padding
        e = base64.urlsafe_b64decode(jwks_key['e'] + '==')  # Add padding
        
        # Convert to integers
        n_int = int.from_bytes(n, byteorder='big')
        e_int = int.from_bytes(e, byteorder='big')
        
        # Create RSA public key
        public_key = rsa.RSAPublicNumbers(e_int, n_int).public_key(default_backend())
        
        # Serialize to PEM format
        pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        
        return pem.decode('utf-8')
        
    except Exception as e:
        logger.error(f"Error constructing RSA key: {str(e)}")
        raise AuthorizationError(f"Failed to construct RSA key: {str(e)}")


def validate_jwt_token(token: str) -> Dict[str, Any]:
    """
    Validate JWT token and extract claims

    Only the token header is read before verification, and only its key ID
    (kid), to select a signing key from the trusted issuers' JWKS. The claims
    are returned only after the signature, expiry, issuer, audience and, for
    the configured user pool, the token type have been checked against
    configuration.
    
    Args:
        token: JWT token string
        
    Returns:
        Dictionary containing validated claims
        
    Raises:
        AuthorizationError: If token validation fails
    """
    try:
        if not ALLOWED_AUDIENCES or not TRUSTED_ISSUERS:
            raise AuthorizationError("No allowed audience or trusted issuer is configured")

        # Only the header's key ID (kid) is read before verification, and only
        # to select the signing key. No claim is read until the token has been
        # verified against the configured issuer that owns that key.
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get('kid')
        if not isinstance(kid, str) or not kid:
            raise AuthorizationError("Token missing key ID (kid)")

        claims = None
        verified_issuer = None
        signature_error = None
        for issuer, jwks_url in TRUSTED_ISSUERS:
            try:
                jwks = get_jwks_keys(jwks_url)
            except AuthorizationError:
                # get_jwks_keys has logged the failure; try the next issuer
                continue
            jwks_key = find_jwks_key(jwks, kid)
            if not jwks_key:
                continue
            try:
                claims = jwt.decode(
                    token,
                    construct_rsa_key(jwks_key),
                    algorithms=['RS256'],
                    audience=list(ALLOWED_AUDIENCES),
                    issuer=issuer,  # the configured issuer that owns this key, never the token's iss
                    options={'require': ['exp', 'iss', 'aud', 'sub']},
                )
            except jwt.InvalidSignatureError as e:
                # Another trusted issuer may hold a key with the same kid
                signature_error = e
                continue
            verified_issuer = issuer
            break

        if claims is None:
            if signature_error is None:
                raise AuthorizationError(f"Key not found in any trusted JWKS: {kid}")
            raise signature_error

        # User pool ID tokens carry the app client id in `aud`; access tokens
        # are not accepted. Other issuers are checked by `aud` alone.
        if verified_issuer == COGNITO_ISSUER and claims.get('token_use') != 'id':
            raise AuthorizationError("Token is not an ID token")

        logger.info(f"Successfully validated token for user: {claims.get('sub', 'unknown')}")
        return claims
        
    except jwt.ExpiredSignatureError:
        raise AuthorizationError("Token has expired")
    except jwt.InvalidAudienceError:
        raise AuthorizationError("Invalid token audience")
    except jwt.InvalidIssuerError:
        raise AuthorizationError("Invalid token issuer")
    except jwt.InvalidSignatureError:
        raise AuthorizationError("Invalid token signature")
    except jwt.MissingRequiredClaimError as e:
        raise AuthorizationError(f"Token missing required claim: {e.claim}")
    except jwt.InvalidTokenError as e:
        raise AuthorizationError(f"Invalid token: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error validating token: {str(e)}")
        raise AuthorizationError(f"Token validation failed: {str(e)}")


def extract_token_from_event(event: Dict) -> str:
    """
    Extract JWT token from API Gateway event
    
    Args:
        event: API Gateway event
        
    Returns:
        JWT token string
        
    Raises:
        AuthorizationError: If token extraction fails
    """
    # Try Authorization header first
    auth_header = event.get('authorizationToken')
    if auth_header:
        if auth_header.startswith('Bearer '):
            return auth_header[7:]  # Remove 'Bearer ' prefix
        else:
            return auth_header
    
    # Try headers in request context
    headers = event.get('headers', {})
    auth_header = headers.get('Authorization') or headers.get('authorization')
    if auth_header:
        if auth_header.startswith('Bearer '):
            return auth_header[7:]
        else:
            return auth_header
    
    raise AuthorizationError("No authorization token found")


def generate_policy(principal_id: str, effect: str, resource: str, context: Optional[Dict] = None) -> Dict:
    """
    Generate IAM policy for API Gateway
    
    Args:
        principal_id: User identifier
        effect: Allow or Deny
        resource: Resource ARN
        context: Additional context to pass to Lambda
        
    Returns:
        IAM policy dictionary
    """
    policy = {
        'principalId': principal_id,
        'policyDocument': {
            'Version': '2012-10-17',
            'Statement': [
                {
                    'Action': 'execute-api:Invoke',
                    'Effect': effect,
                    'Resource': resource
                }
            ]
        }
    }
    
    if context:
        policy['context'] = context
    
    return policy


def handler(event, lambda_context):
    """
    Lambda authorizer handler for API Gateway
    
    Args:
        event: API Gateway authorizer event
        lambda_context: Lambda context
        
    Returns:
        IAM policy allowing or denying access
    """
    try:
        logger.info("JWT Authorizer invoked: %s", _safe_event_metadata(event))
        
        # Extract token from event
        token = extract_token_from_event(event)
        
        # Validate JWT token
        claims = validate_jwt_token(token)
        
        # Extract user information
        user_id = claims.get('sub', 'unknown')
        email = claims.get('email', 'unknown')
        username = claims.get('cognito:username', claims.get('preferred_username', 'unknown'))
        
        # Extract custom attributes (for Cognito)
        role = claims.get('custom:role', 'Viewer')
        groups = claims.get('custom:groups', '')
        
        # For non-Cognito tokens, try to extract role from groups or other claims
        if not role or role == 'Viewer':
            # Try standard OIDC groups claim
            token_groups = claims.get('groups', [])
            if isinstance(token_groups, list) and token_groups:
                # Map groups to roles (this should be configurable)
                group_role_mapping = {
                    'portal-admins': 'PortalAdmin',
                    'cv-data-scientists': 'DataScientist',
                    'cv-operators': 'Operator',
                    'cv-viewers': 'Viewer'
                }
                
                for group in token_groups:
                    if group in group_role_mapping:
                        role = group_role_mapping[group]
                        break
        
        # Create context to pass to Lambda functions
        auth_context = {
            'userId': user_id,
            'email': email,
            'username': username,
            'role': role,
            'groups': groups,
            'issuer': claims.get('iss', 'unknown'),
            'audience': claims.get('aud', 'unknown'),
            'tokenType': 'JWT'
        }
        
        # Generate allow policy
        policy = generate_policy(
            principal_id=user_id,
            effect='Allow',
            resource=event['methodArn'],
            context=auth_context
        )
        
        logger.info(f"Authorization successful for user: {user_id}")
        return policy
        
    except AuthorizationError as e:
        logger.warning(f"Authorization failed: {str(e)}")
        # Return deny policy
        return generate_policy(
            principal_id='unauthorized',
            effect='Deny',
            resource=event['methodArn']
        )
        
    except Exception as e:
        logger.error(f"Unexpected error in JWT authorizer: {str(e)}", exc_info=True)
        # Return deny policy for any unexpected errors
        return generate_policy(
            principal_id='error',
            effect='Deny',
            resource=event['methodArn']
        )