#!/bin/bash
# DDA Portal - Cross-Account Role Setup Script
# Deploys IAM roles in UseCase or Data accounts

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

printf "${GREEN}========================================${NC}\n"
printf "${GREEN}DDA Portal - Account Role Setup${NC}\n"
printf "${GREEN}========================================${NC}\n"
echo ""

# Check directory
if [ ! -f "infrastructure/bin/usecase-account-app.ts" ]; then
    printf "${RED}Error: Must run from edge-cv-portal directory${NC}\n"
    exit 1
fi

# Check AWS CLI
if ! command -v aws &> /dev/null; then
    printf "${RED}Error: AWS CLI not found${NC}\n"
    exit 1
fi

# Get current account
printf "${BLUE}Checking AWS credentials...${NC}\n"
CURRENT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>&1)
if [ $? -ne 0 ] || ! [[ "$CURRENT_ACCOUNT" =~ ^[0-9]{12}$ ]]; then
    printf "${RED}Error: Could not get AWS account. Check credentials.${NC}\n"
    exit 1
fi
printf "Current AWS Account: ${GREEN}$CURRENT_ACCOUNT${NC}\n"
echo ""

# Role type selection
printf "${BLUE}What type of account is this?${NC}\n"
echo ""
echo "  1) UseCase Account"
echo "     - Where Greengrass devices and SageMaker training run"
echo "     - Full access for training, compilation, deployments"
echo ""
echo "  2) Data Account"
echo "     - Where training data (S3 buckets) is stored"
echo "     - Can be shared by multiple UseCase accounts"
echo ""
read -p "Enter choice [1-2]: " ROLE_TYPE

if [ "$ROLE_TYPE" != "1" ] && [ "$ROLE_TYPE" != "2" ]; then
    printf "${RED}Invalid choice${NC}\n"
    exit 1
fi

echo ""

# Get Portal Account ID
printf "${BLUE}Enter the Portal Account ID${NC}\n"
echo "(AWS account where DDA Portal is deployed)"
read -p "Portal Account ID: " PORTAL_ACCOUNT_ID

if ! [[ "$PORTAL_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
    printf "${RED}Error: Invalid AWS Account ID (must be 12 digits)${NC}\n"
    exit 1
fi

# Generate External ID
if command -v uuidgen &> /dev/null; then
    EXTERNAL_ID=$(uuidgen)
else
    EXTERNAL_ID=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "ext-$(date +%s)")
fi

if [ "$ROLE_TYPE" = "1" ]; then
    # ========================================
    # UseCase Account Setup
    # ========================================
    echo ""
    printf "${YELLOW}Deploying UseCase Account Role...${NC}\n"
    printf "  Portal Account: ${GREEN}$PORTAL_ACCOUNT_ID${NC}\n"
    printf "  Target Account: ${GREEN}$CURRENT_ACCOUNT${NC}\n"
    printf "  External ID:    ${GREEN}$EXTERNAL_ID${NC}\n"
    echo ""
    
    read -p "Press Enter to continue..."
    
    cd infrastructure
    cdk deploy -a "npx ts-node bin/usecase-account-app.ts" \
        -c portalAccountId=$PORTAL_ACCOUNT_ID \
        -c externalId=$EXTERNAL_ID \
        --require-approval never
    cd ..
    
    # Get outputs
    ROLE_ARN=$(aws cloudformation describe-stacks \
        --stack-name DDAPortalUseCaseAccountStack \
        --query 'Stacks[0].Outputs[?OutputKey==`RoleArn`].OutputValue' \
        --output text 2>/dev/null || echo "")
    
    SAGEMAKER_ROLE_ARN=$(aws cloudformation describe-stacks \
        --stack-name DDAPortalUseCaseAccountStack \
        --query 'Stacks[0].Outputs[?OutputKey==`SageMakerExecutionRoleArn`].OutputValue' \
        --output text 2>/dev/null || echo "")
    
    echo ""
    printf "${GREEN}========================================${NC}\n"
    printf "${GREEN}UseCase Account Setup Complete!${NC}\n"
    printf "${GREEN}========================================${NC}\n"
    echo ""
    printf "Use these values in Portal onboarding:\n"
    printf "  Account ID:              ${GREEN}$CURRENT_ACCOUNT${NC}\n"
    printf "  Role ARN:                ${GREEN}$ROLE_ARN${NC}\n"
    printf "  SageMaker Role ARN:      ${GREEN}$SAGEMAKER_ROLE_ARN${NC}\n"
    printf "  External ID:             ${GREEN}$EXTERNAL_ID${NC}\n"
    echo ""
    
    # Save config
    cat > usecase-account-config.txt << EOF
UseCase Account Configuration
=============================
Account ID: $CURRENT_ACCOUNT
Portal Account ID: $PORTAL_ACCOUNT_ID
External ID: $EXTERNAL_ID
Role ARN: $ROLE_ARN
SageMaker Execution Role ARN: $SAGEMAKER_ROLE_ARN
Deployment Date: $(date)
EOF
    printf "Configuration saved to: ${GREEN}usecase-account-config.txt${NC}\n"

else
    # ========================================
    # Data Account Setup
    # ========================================
    echo ""
    printf "${BLUE}Enter UseCase Account ID(s)${NC}\n"
    echo "(Accounts where SageMaker training will run)"
    echo "(For multiple accounts, separate with commas: 111111111111,222222222222)"
    read -p "UseCase Account ID(s): " USECASE_ACCOUNT_IDS
    
    if [ -z "$USECASE_ACCOUNT_IDS" ]; then
        printf "${RED}Error: At least one UseCase Account ID is required${NC}\n"
        exit 1
    fi
    
    echo ""
    printf "${YELLOW}Deploying Data Account Roles...${NC}\n"
    printf "  Portal Account:   ${GREEN}$PORTAL_ACCOUNT_ID${NC}\n"
    printf "  UseCase Accounts: ${GREEN}$USECASE_ACCOUNT_IDS${NC}\n"
    printf "  Data Account:     ${GREEN}$CURRENT_ACCOUNT${NC}\n"
    printf "  External ID:      ${GREEN}$EXTERNAL_ID${NC}\n"
    echo ""
    
    read -p "Press Enter to continue..."
    
    cd infrastructure
    cdk deploy -a "npx ts-node bin/data-account-app.ts" \
        -c portalAccountId=$PORTAL_ACCOUNT_ID \
        -c usecaseAccountIds=$USECASE_ACCOUNT_IDS \
        -c externalId=$EXTERNAL_ID \
        --require-approval never
    cd ..
    
    # Get outputs
    PORTAL_ROLE_ARN=$(aws cloudformation describe-stacks \
        --stack-name DDAPortalDataAccountStack \
        --query 'Stacks[0].Outputs[?OutputKey==`PortalAccessRoleArn`].OutputValue' \
        --output text 2>/dev/null || echo "")
    
    SAGEMAKER_ROLE_ARN=$(aws cloudformation describe-stacks \
        --stack-name DDAPortalDataAccountStack \
        --query 'Stacks[0].Outputs[?OutputKey==`SageMakerAccessRoleArn`].OutputValue' \
        --output text 2>/dev/null || echo "")
    
    echo ""
    printf "${GREEN}========================================${NC}\n"
    printf "${GREEN}Data Account Setup Complete!${NC}\n"
    printf "${GREEN}========================================${NC}\n"
    echo ""
    printf "Use these values in Portal onboarding:\n"
    printf "  Data Account ID:         ${GREEN}$CURRENT_ACCOUNT${NC}\n"
    printf "  Portal Access Role ARN:  ${GREEN}$PORTAL_ROLE_ARN${NC}\n"
    printf "  SageMaker Access Role:   ${GREEN}$SAGEMAKER_ROLE_ARN${NC}\n"
    printf "  External ID:             ${GREEN}$EXTERNAL_ID${NC}\n"
    echo ""
    
    # Save config
    cat > data-account-config.txt << EOF
Data Account Configuration
==========================
Data Account ID: $CURRENT_ACCOUNT
Portal Account ID: $PORTAL_ACCOUNT_ID
UseCase Account IDs: $USECASE_ACCOUNT_IDS
External ID: $EXTERNAL_ID
Portal Access Role ARN: $PORTAL_ROLE_ARN
SageMaker Access Role ARN: $SAGEMAKER_ROLE_ARN
Deployment Date: $(date)
EOF
    printf "Configuration saved to: ${GREEN}data-account-config.txt${NC}\n"
fi

echo ""
printf "${YELLOW}Next: Tag S3 buckets for portal access${NC}\n"
echo "aws s3api put-bucket-tagging --bucket YOUR_BUCKET --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]'"
echo ""
