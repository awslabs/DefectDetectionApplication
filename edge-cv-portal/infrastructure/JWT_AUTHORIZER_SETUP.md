# JWT Authorizer Setup Guide

This document describes the JWT authorizer implementation for the Edge CV Portal API Gateway.

## Overview

The Edge CV Portal supports two authentication methods:

1. **Cognito User Pools Authorizer** (Default) - Uses AWS Cognito for JWT validation
2. **Custom JWT Authorizer** (Alternative) - Custom Lambda function for flexible JWT validation

## JWT Authorizer Features

### Supported Identity Providers

- AWS Cognito User Pools
- Any OIDC-compliant identity provider (Okta, Azure AD, Auth0, etc.)
- Custom identity providers with JWKS endpoints

### Configuration

The JWT authorizer is configured via environment variables:

```typescript
environment: {
  COGNITO_USER_POOL_ID: props.userPool.userPoolId,
  COGNITO_REGION: cdk.Aws.REGION,
  ALLOWED_AUDIENCES: '', // Comma-separated list of allowed audiences
  ISSUER_WHITELIST: '', // Comma-separated list of trusted issuers
}
```

### Token Validation Process

1. Extract JWT token from Authorization header
2. Decode token header to get Key ID (kid) and issuer
3. Determine JWKS URL based on issuer
4. Fetch and cache JWKS keys
5. Validate token signature using RSA public key
6. Verify token claims (expiration, audience, issuer)
7. Extract user information and role mappings
8. Generate IAM policy for API Gateway

### Role Mapping

The authorizer supports role mapping from identity provider groups:

```python
group_role_mapping = {
    'portal-admins': 'PortalAdmin',
    'cv-data-scientists': 'DataScientist',
    'cv-operators': 'Operator',
    'cv-viewers': 'Viewer'
}
```

### Caching

- JWKS keys are cached for 1 hour to improve performance
- Authorization results are cached by API Gateway for 5 minutes

## Switching Between Authorizers

### Using Cognito Authorizer (Default)

```typescript
usecasesResource.addMethod(
  'GET',
  useCasesIntegration,
  {
    authorizer,  // Cognito authorizer
    authorizationType: apigateway.AuthorizationType.COGNITO,
  }
);
```

### Using JWT Authorizer

```typescript
usecasesResource.addMethod(
  'GET',
  useCasesIntegration,
  {
    authorizer: jwtAuthorizer,  // JWT Lambda authorizer
    authorizationType: apigateway.AuthorizationType.CUSTOM,
  }
);
```

## Token Refresh Implementation

The auth handler now includes a token refresh endpoint:

```
POST /api/v1/auth/refresh
Content-Type: application/json

{
  "refresh_token": "...",
  "client_id": "..."
}
```

Response:
```json
{
  "access_token": "...",
  "id_token": "...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

## Dependencies

The JWT authorizer requires additional Python packages:

- `PyJWT==2.8.0` - JWT token handling
- `cryptography==41.0.7` - RSA key operations
- `requests==2.31.0` - JWKS fetching

These are packaged in a separate Lambda layer (`JwtLayer`).

## Building the JWT Layer

To build the JWT dependencies layer:

```bash
cd edge-cv-portal/backend/layers/jwt
./build.sh
```

This will install the required packages in the `python/` directory.

## Error Handling

The JWT authorizer handles various error conditions:

- **Invalid token format**: Returns Deny policy
- **Expired tokens**: Returns Deny policy  
- **Invalid signature**: Returns Deny policy
- **Untrusted issuer**: Returns Deny policy
- **Missing JWKS keys**: Returns Deny policy
- **Network errors**: Returns Deny policy

All errors are logged for debugging purposes.

## Security Considerations

1. **JWKS Caching**: Keys are cached to prevent excessive requests to identity providers
2. **Issuer Validation**: Only whitelisted issuers are accepted
3. **Audience Validation**: Tokens must have valid audience claims
4. **Signature Verification**: All tokens are cryptographically verified
5. **Error Handling**: Detailed errors are logged but not exposed to clients

## Monitoring

The JWT authorizer integrates with CloudWatch for monitoring:

- Authorization success/failure metrics
- Token validation latency
- JWKS fetch errors
- Cache hit/miss rates

## Testing

To test the JWT authorizer:

1. Deploy the infrastructure with JWT authorizer enabled
2. Obtain a valid JWT token from your identity provider
3. Make API requests with the token in the Authorization header:

```bash
curl -H "Authorization: Bearer <jwt_token>" \
     https://api.example.com/api/v1/usecases
```

## Troubleshooting

### Common Issues

1. **"Key not found in JWKS"**: The token's key ID doesn't match any keys in the JWKS
2. **"Untrusted issuer"**: The token issuer is not in the whitelist
3. **"Invalid token signature"**: The token signature verification failed
4. **"Token has expired"**: The token's exp claim is in the past

### Debug Logging

Enable debug logging by setting the Lambda log level to DEBUG:

```python
logger.setLevel(logging.DEBUG)
```

This will provide detailed information about token validation steps.