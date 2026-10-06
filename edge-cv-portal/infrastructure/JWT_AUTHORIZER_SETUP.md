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
  ALLOWED_AUDIENCES: props.userPoolClientId ?? '', // Required: comma-separated app client ids
  ISSUER_WHITELIST: '', // Comma-separated exact https:// issuer strings
}
```

- `ALLOWED_AUDIENCES` is required. CDK wires it from the user-pool client id: `bin/app.ts` passes `authStack.userPoolClient.userPoolClientId` to ComputeStack as `userPoolClientId`. A token's `aud` must name one of the listed ids. If the list is empty, the authorizer denies every request. When you add a custom identity provider, add its client ids here too.
- `ISSUER_WHITELIST` entries are exact `https://` issuer strings, compared by string equality. Each issuer's keys are fetched from `<issuer>/.well-known/jwks.json`. An entry that doesn't start with `https://` is ignored and logged at ERROR.
- Tokens from the configured user pool must be ID tokens (`token_use` is `id`). Access tokens are denied.

### Token Validation Process

1. Extract JWT token from Authorization header
2. Read only the token header before verification, and use its Key ID (kid) only to select a signing key
3. Fetch and cache the JWKS of each trusted issuer in turn (the configured user pool first, then each `ISSUER_WHITELIST` entry) and look up the kid
4. Validate the RS256 signature with that issuer's public key
5. Verify the claims against configuration: `exp`, `iss`, `aud` and `sub` are required, `iss` must equal the issuer that owns the key, `aud` must be in `ALLOWED_AUDIENCES`, and user-pool tokens must be ID tokens
6. Extract user information and role mappings
7. Generate IAM policy for API Gateway

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
2. **Issuer Validation**: Only the configured user pool and the exact `https://` issuers in `ISSUER_WHITELIST` are trusted, and a token's `iss` must equal the issuer whose key verified it
3. **Audience Validation**: `ALLOWED_AUDIENCES` is required, and a token's `aud` must name one of its app client ids. User-pool tokens must be ID tokens, so access tokens are denied
4. **Signature Verification**: All tokens are cryptographically verified with RS256. Only the header is read before verification, and only its `kid`, to select the key
5. **Required Claims**: Tokens without `exp`, `iss`, `aud` or `sub` are denied
6. **Error Handling**: Detailed errors are logged but not exposed to clients

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

1. **"Key not found in any trusted JWKS"**: The token's key ID doesn't match any key of the trusted issuers
2. **"Invalid token issuer"**: The token's `iss` isn't the issuer whose key verified it
3. **"Invalid token signature"**: The token signature verification failed
4. **"Token has expired"**: The token's exp claim is in the past
5. **"Invalid token audience"** or **"Token missing required claim: aud"**: The token's `aud` isn't in `ALLOWED_AUDIENCES`, or it has none. A user-pool access token lands here, because it carries `client_id` and no `aud`; send the ID token
6. **"Token is not an ID token"**: A user-pool token has an allowed `aud`, but its `token_use` isn't `id`

### Debug Logging

Enable debug logging by setting the Lambda log level to DEBUG:

```python
logger.setLevel(logging.DEBUG)
```

This will provide detailed information about token validation steps.