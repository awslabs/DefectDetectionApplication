# SSO Integration Setup Guide

This guide explains how to configure Single Sign-On (SSO) integration with the Edge CV Portal using SAML 2.0 or OIDC.

## Overview

The Portal supports flexible authentication options:
1. **Cognito User Pool only** (default) - Users authenticate directly with Cognito
2. **Cognito + SAML Federation** - Users authenticate via company SSO (Okta, Azure AD, etc.)
3. **Cognito + OIDC Federation** - Users authenticate via OIDC-compliant identity providers

## Environment Variables

Configure these environment variables before deploying the auth stack:

```bash
# SSO Configuration
export SSO_ENABLED=true                                    # Enable SSO integration
export SSO_METADATA_URL=https://your-idp.com/metadata.xml  # SAML metadata URL
export SSO_PROVIDER_NAME=YourCompanySSO                   # Display name for SSO provider
export COGNITO_DOMAIN_PREFIX=your-company-dda-portal      # Unique domain prefix

# Optional: Frontend URLs (will be configured automatically if not set)
export FRONTEND_URL=https://your-domain.com               # Production frontend URL
```

## Supported Identity Providers

### Okta
```bash
export SSO_ENABLED=true
export SSO_METADATA_URL=https://your-org.okta.com/app/abc123/sso/saml/metadata
export SSO_PROVIDER_NAME=Okta
```

### Azure Active Directory
```bash
export SSO_ENABLED=true
export SSO_METADATA_URL=https://login.microsoftonline.com/your-tenant-id/federationmetadata/2007-06/federationmetadata.xml
export SSO_PROVIDER_NAME=AzureAD
```

### Google Workspace
```bash
export SSO_ENABLED=true
export SSO_METADATA_URL=https://accounts.google.com/o/saml2/idp?idpid=your-idp-id
export SSO_PROVIDER_NAME=GoogleWorkspace
```

## SAML Attribute Mapping

The Portal expects these SAML attributes from your identity provider:

| Portal Attribute | SAML Claim | Description |
|------------------|------------|-------------|
| email | http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress | User email address |
| given_name | http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname | First name |
| family_name | http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname | Last name |
| groups | http://schemas.xmlsoap.org/claims/Group | Group memberships |
| role | http://schemas.microsoft.com/ws/2008/06/identity/claims/role | User role |

## Role Mapping

Configure your identity provider to send group/role information that maps to Portal roles:

| IdP Group/Role | Portal Role | Permissions |
|----------------|-------------|-------------|
| portal-admins | PortalAdmin | Full access to all use cases |
| cv-data-scientists | DataScientist | Labeling, training, model registry |
| cv-operators | Operator | Deployments, device management |
| cv-viewers | Viewer | Read-only access |

## Deployment Steps

1. **Configure Environment Variables**
   ```bash
   export SSO_ENABLED=true
   export SSO_METADATA_URL=https://your-idp.com/metadata.xml
   export SSO_PROVIDER_NAME=YourCompanySSO
   export COGNITO_DOMAIN_PREFIX=your-company-dda-portal
   ```

2. **Deploy Auth Stack**
   ```bash
   cd infrastructure
   npm run build
   cdk deploy EdgeCVPortalAuthStack
   ```

3. **Configure Identity Provider**
   - Add the Cognito SAML endpoint as a service provider in your IdP
   - Configure attribute mappings as shown above
   - Set up group/role assignments for users

4. **Update Frontend Configuration**
   - The auth stack outputs will include the necessary configuration
   - Update your frontend deployment with the new auth settings

## Testing SSO Integration

1. **Verify Deployment**
   ```bash
   # Check that the identity provider was created
   aws cognito-idp list-identity-providers --user-pool-id <your-pool-id>
   ```

2. **Test Login Flow**
   - Navigate to the Cognito hosted UI
   - Click on your SSO provider
   - Complete authentication with your company credentials
   - Verify that user attributes are mapped correctly

3. **Verify Role Mapping**
   - Check that group memberships are correctly mapped to Portal roles
   - Test access controls with different user roles

## Troubleshooting

### Common Issues

1. **SAML Metadata URL not accessible**
   - Ensure the metadata URL is publicly accessible
   - Check firewall rules and network connectivity

2. **Attribute mapping issues**
   - Verify that your IdP sends the expected SAML attributes
   - Check attribute names match the configuration

3. **Domain prefix conflicts**
   - Cognito domain prefixes must be globally unique
   - Try a different prefix if deployment fails

4. **Role mapping not working**
   - Verify group/role claims are included in SAML assertion
   - Check that attribute mapping configuration is correct

### Debug Commands

```bash
# Check Cognito configuration
aws cognito-idp describe-user-pool --user-pool-id <pool-id>

# List identity providers
aws cognito-idp list-identity-providers --user-pool-id <pool-id>

# Check user attributes after login
aws cognito-idp admin-get-user --user-pool-id <pool-id> --username <username>
```

## Security Considerations

1. **SAML Signing**
   - Ensure SAML assertions are signed by your IdP
   - Verify certificate validity and rotation procedures

2. **Attribute Validation**
   - Validate that group/role attributes cannot be spoofed
   - Implement proper access controls in your IdP

3. **Session Management**
   - Configure appropriate session timeouts
   - Implement proper logout procedures

4. **Audit Logging**
   - Monitor authentication events in CloudTrail
   - Set up alerts for failed authentication attempts

## Next Steps

After SSO is configured:
1. Deploy the remaining infrastructure stacks
2. Configure use case assignments for users
3. Set up role-based access controls
4. Test the complete authentication flow