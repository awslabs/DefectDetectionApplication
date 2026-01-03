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
from typing import Dict, Any, Optional
from functools import lru_cache
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration from environment variables
COGNITO_USER_POOL_ID = os.environ.get('COGNITO_USER_POOL_ID')
COGNITO_REGION = os.environ.get('COGNITO_REGION', 'us-east-1')
ALLOWED_AUDIENCES = os.environ.get('ALLOWED_AUDIENCES', '').split(',')
ISSUER_WHITELIST = os.environ.get('ISSUER_WHITELIST', '').split(',')

# Cache for JWKS keys (1 hour TTL)
JWKS_CACHE_TTL = 3600


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
        pem = public_key.serialize(
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
    
    Args:
        token: JWT token string
        
    Returns:
        Dictionary containing validated claims
        
    Raises:
        AuthorizationError: If token validation fails
    """
    try:
        # Decode header without verification to get key ID and issuer info
        unverified_header = jwt.get_unverified_header(token)
        unverified_payload = jwt.decode(token, options={"verify_signature": False})
        
        kid = unverified_header.get('kid')
        if not kid:
            raise AuthorizationError("Token missing key ID (kid)")
        
        issuer = unverified_payload.get('iss')
        if not issuer:
            raise AuthorizationError("Token missing issuer (iss)")
        
        # Determine JWKS URL based on issuer
        if COGNITO_USER_POOL_ID and f"cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}" in issuer:
            # Cognito token
            jwks_url = get_cognito_jwks_url(COGNITO_USER_POOL_ID, COGNITO_REGION)
        elif issuer in ISSUER_WHITELIST:
            # Custom identity provider
            jwks_url = f"{issuer}/.well-known/jwks.json"
        else:
            raise AuthorizationError(f"Untrusted issuer: {issuer}")
        
        # Fetch JWKS keys
        jwks = get_jwks_keys(jwks_url)
        
        # Find the specific key
        jwks_key = find_jwks_key(jwks, kid)
        if not jwks_key:
            raise AuthorizationError(f"Key not found in JWKS: {kid}")
        
        # Construct public key for verification
        public_key = construct_rsa_key(jwks_key)
        
        # Verify and decode token
        decoded_token = jwt.decode(
            token,
            public_key,
            algorithms=['RS256'],
            audience=ALLOWED_AUDIENCES if ALLOWED_AUDIENCES != [''] else None,
            issuer=issuer,
            options={
                'verify_exp': True,
                'verify_aud': bool(ALLOWED_AUDIENCES and ALLOWED_AUDIENCES != ['']),
                'verify_iss': True,
            }
        )
        
        logger.info(f"Successfully validated token for user: {decoded_token.get('sub', 'unknown')}")
        return decoded_token
        
    except jwt.ExpiredSignatureError:
        raise AuthorizationError("Token has expired")
    except jwt.InvalidAudienceError:
        raise AuthorizationError("Invalid token audience")
    except jwt.InvalidIssuerError:
        raise AuthorizationError("Invalid token issuer")
    except jwt.InvalidSignatureError:
        raise AuthorizationError("Invalid token signature")
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
        logger.info(f"JWT Authorizer invoked with event: {json.dumps(event, default=str)}")
        
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