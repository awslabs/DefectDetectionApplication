import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';

export interface AuthStackProps extends cdk.StackProps {
  readonly ssoEnabled?: boolean;
  readonly ssoMetadataUrl?: string;
  readonly ssoProviderName?: string;
  readonly domainPrefix?: string;
}

export class AuthStack extends cdk.Stack {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;
  public readonly userPoolDomain?: cognito.UserPoolDomain;
  public readonly identityProvider?: cognito.UserPoolIdentityProviderSaml;

  constructor(scope: Construct, id: string, props?: AuthStackProps) {
    super(scope, id, props);

    // Cognito User Pool
    this.userPool = new cognito.UserPool(this, 'DDAPortalUserPool', {
      userPoolName: 'dda-portal-users',
      selfSignUpEnabled: false,
      signInAliases: {
        email: true,
        username: true,
      },
      autoVerify: {
        email: true,
      },
      standardAttributes: {
        email: {
          required: true,
          mutable: true,
        },
        givenName: {
          required: true,
          mutable: true,
        },
        familyName: {
          required: true,
          mutable: true,
        },
      },
      customAttributes: {
        role: new cognito.StringAttribute({ mutable: true }),
        groups: new cognito.StringAttribute({ mutable: true }),
      },
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Create User Pool Domain for hosted UI
    const domainPrefix = props?.domainPrefix || `dda-portal-${cdk.Aws.ACCOUNT_ID}`;
    this.userPoolDomain = new cognito.UserPoolDomain(this, 'DDAPortalUserPoolDomain', {
      userPool: this.userPool,
      cognitoDomain: {
        domainPrefix: domainPrefix,
      },
    });

    // Add SAML Identity Provider if SSO is enabled
    if (props?.ssoEnabled && props?.ssoMetadataUrl) {
      this.identityProvider = new cognito.UserPoolIdentityProviderSaml(this, 'SamlProvider', {
        userPool: this.userPool,
        name: props.ssoProviderName || 'CustomerSSO',
        metadata: cognito.UserPoolIdentityProviderSamlMetadata.url(props.ssoMetadataUrl),
        attributeMapping: {
          email: cognito.ProviderAttribute.other('http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress'),
          givenName: cognito.ProviderAttribute.other('http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname'),
          familyName: cognito.ProviderAttribute.other('http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname'),
          custom: {
            groups: cognito.ProviderAttribute.other('http://schemas.xmlsoap.org/claims/Group'),
            role: cognito.ProviderAttribute.other('http://schemas.microsoft.com/ws/2008/06/identity/claims/role'),
          },
        },
      });
    }

    // User Pool Client
    this.userPoolClient = new cognito.UserPoolClient(this, 'DDAPortalUserPoolClient', {
      userPool: this.userPool,
      userPoolClientName: 'dda-portal-client',
      authFlows: {
        userPassword: true,
        userSrp: true,
        adminUserPassword: true,  // Enable ADMIN_NO_SRP_AUTH for testing
      },
      oAuth: {
        flows: {
          authorizationCodeGrant: true,
        },
        scopes: [
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.PROFILE,
        ],
        callbackUrls: [
          'http://localhost:3000',  // For local development
          // Production URLs will be added via environment variables
        ],
        logoutUrls: [
          'http://localhost:3000',  // For local development
          // Production URLs will be added via environment variables
        ],
      },
      generateSecret: false,
      preventUserExistenceErrors: true,
      supportedIdentityProviders: props?.ssoEnabled && this.identityProvider ? [
        cognito.UserPoolClientIdentityProvider.COGNITO,
        cognito.UserPoolClientIdentityProvider.custom(this.identityProvider.providerName),
      ] : [
        cognito.UserPoolClientIdentityProvider.COGNITO,
      ],
    });

    // Add dependency to ensure identity provider is created before client
    if (this.identityProvider) {
      this.userPoolClient.node.addDependency(this.identityProvider);
    }

    // Outputs
    new cdk.CfnOutput(this, 'UserPoolId', {
      value: this.userPool.userPoolId,
      description: 'Cognito User Pool ID',
      exportName: 'EdgeCVPortalUserPoolId',
    });

    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
      description: 'Cognito User Pool Client ID',
      exportName: 'EdgeCVPortalUserPoolClientId',
    });

    new cdk.CfnOutput(this, 'UserPoolArn', {
      value: this.userPool.userPoolArn,
      description: 'Cognito User Pool ARN',
    });

    new cdk.CfnOutput(this, 'UserPoolDomainUrl', {
      value: `https://${this.userPoolDomain.domainName}.auth.${cdk.Aws.REGION}.amazoncognito.com`,
      description: 'Cognito User Pool Domain URL',
      exportName: 'EdgeCVPortalUserPoolDomainUrl',
    });

    if (this.identityProvider) {
      new cdk.CfnOutput(this, 'SamlProviderName', {
        value: this.identityProvider.providerName,
        description: 'SAML Identity Provider Name',
      });
    }

    // Output configuration for frontend
    new cdk.CfnOutput(this, 'AuthConfig', {
      value: JSON.stringify({
        userPoolId: this.userPool.userPoolId,
        userPoolWebClientId: this.userPoolClient.userPoolClientId,
        region: cdk.Aws.REGION,
        domain: this.userPoolDomain.domainName,
        ssoEnabled: props?.ssoEnabled || false,
      }),
      description: 'Authentication configuration for frontend',
    });
  }
}
