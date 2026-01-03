#!/bin/bash

# Deploy Authentication Stack for Edge CV Portal
# Usage: ./deploy-auth.sh [--sso] [--profile aws-profile]

set -e

# Default values
SSO_ENABLED=false
AWS_PROFILE=""
REGION="us-east-1"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --sso)
      SSO_ENABLED=true
      shift
      ;;
    --profile)
      AWS_PROFILE="$2"
      shift 2
      ;;
    --region)
      REGION="$2"
      shift 2
      ;;
    --help)
      echo "Usage: $0 [--sso] [--profile aws-profile] [--region region]"
      echo ""
      echo "Options:"
      echo "  --sso              Enable SSO integration (requires SSO_* environment variables)"
      echo "  --profile PROFILE  AWS CLI profile to use"
      echo "  --region REGION    AWS region (default: us-east-1)"
      echo "  --help             Show this help message"
      echo ""
      echo "SSO Environment Variables (required when --sso is used):"
      echo "  SSO_METADATA_URL      SAML metadata URL from your identity provider"
      echo "  SSO_PROVIDER_NAME     Display name for your SSO provider (optional)"
      echo "  COGNITO_DOMAIN_PREFIX Unique domain prefix for Cognito (optional)"
      echo ""
      echo "Examples:"
      echo "  $0                                    # Deploy without SSO"
      echo "  $0 --sso --profile production        # Deploy with SSO using production profile"
      echo "  $0 --region us-west-2                # Deploy to us-west-2 region"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo "Use --help for usage information"
      exit 1
      ;;
  esac
done

# Set AWS profile if provided
if [[ -n "$AWS_PROFILE" ]]; then
  export AWS_PROFILE="$AWS_PROFILE"
  echo "Using AWS profile: $AWS_PROFILE"
fi

# Set region
export CDK_DEFAULT_REGION="$REGION"
echo "Deploying to region: $REGION"

# Validate SSO configuration if enabled
if [[ "$SSO_ENABLED" == "true" ]]; then
  echo "SSO integration enabled"
  
  if [[ -z "$SSO_METADATA_URL" ]]; then
    echo "Error: SSO_METADATA_URL environment variable is required when SSO is enabled"
    echo "Please set: export SSO_METADATA_URL=https://your-idp.com/metadata.xml"
    exit 1
  fi
  
  echo "SSO Metadata URL: $SSO_METADATA_URL"
  echo "SSO Provider Name: ${SSO_PROVIDER_NAME:-CustomerSSO}"
  echo "Cognito Domain Prefix: ${COGNITO_DOMAIN_PREFIX:-auto-generated}"
  
  export SSO_ENABLED=true
else
  echo "SSO integration disabled - using Cognito User Pool only"
  export SSO_ENABLED=false
fi

# Check if CDK is installed
if ! command -v cdk &> /dev/null; then
  echo "Error: AWS CDK is not installed"
  echo "Please install it with: npm install -g aws-cdk"
  exit 1
fi

# Check if we're in the right directory
if [[ ! -f "cdk.json" ]]; then
  echo "Error: cdk.json not found. Please run this script from the infrastructure directory"
  exit 1
fi

# Install dependencies
echo "Installing dependencies..."
npm install

# Build the project
echo "Building CDK project..."
npm run build

# Bootstrap CDK if needed (only for first-time deployment)
echo "Checking CDK bootstrap status..."
if ! aws cloudformation describe-stacks --stack-name CDKToolkit --region "$REGION" &>/dev/null; then
  echo "CDK not bootstrapped in this region. Bootstrapping..."
  cdk bootstrap --region "$REGION"
else
  echo "CDK already bootstrapped in this region"
fi

# Deploy the auth stack
echo "Deploying EdgeCVPortalAuthStack..."
cdk deploy EdgeCVPortalAuthStack --require-approval never

# Display outputs
echo ""
echo "Deployment completed successfully!"
echo ""
echo "Auth Stack Outputs:"
aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalAuthStack \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table

echo ""
if [[ "$SSO_ENABLED" == "true" ]]; then
  echo "Next steps for SSO configuration:"
  echo "1. Configure your identity provider with the Cognito SAML endpoint"
  echo "2. Set up attribute mappings as documented in SSO_SETUP.md"
  echo "3. Test the authentication flow"
  echo "4. Deploy the remaining infrastructure stacks"
else
  echo "Next steps:"
  echo "1. Create test users in the Cognito User Pool"
  echo "2. Deploy the remaining infrastructure stacks"
  echo "3. Configure user roles and use case assignments"
fi

echo ""
echo "For detailed SSO setup instructions, see: SSO_SETUP.md"