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

# Generate External ID (will be overridden if existing config found)
if command -v uuidgen &> /dev/null; then
    EXTERNAL_ID=$(uuidgen)
else
    EXTERNAL_ID=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "ext-$(date +%s)")
fi

if [ "$ROLE_TYPE" = "1" ]; then
    # ========================================
    # UseCase Account Setup
    # ========================================
    
    # Check for existing UseCase External ID (account-specific file first, then generic)
    USECASE_EXTERNAL_ID=""
    if [ -f "usecase-account-${CURRENT_ACCOUNT}-config.txt" ]; then
        USECASE_EXTERNAL_ID=$(grep "External ID:" "usecase-account-${CURRENT_ACCOUNT}-config.txt" | head -1 | awk '{print $NF}')
        printf "${BLUE}Found account-specific config: usecase-account-${CURRENT_ACCOUNT}-config.txt${NC}\n"
    elif [ -f "usecase-account-config.txt" ]; then
        # Check if the generic config is for this account
        CONFIG_ACCOUNT=$(grep "Account ID:" usecase-account-config.txt | head -1 | awk '{print $NF}')
        if [ "$CONFIG_ACCOUNT" = "$CURRENT_ACCOUNT" ]; then
            USECASE_EXTERNAL_ID=$(grep "External ID:" usecase-account-config.txt | head -1 | awk '{print $NF}')
        fi
    fi
    
    if [ -n "$USECASE_EXTERNAL_ID" ]; then
        echo ""
        printf "${YELLOW}Found existing UseCase External ID: ${GREEN}$USECASE_EXTERNAL_ID${NC}\n"
        read -p "Use existing External ID? [Y/n]: " USE_EXISTING
        if [ "$USE_EXISTING" != "n" ] && [ "$USE_EXISTING" != "N" ]; then
            EXTERNAL_ID="$USECASE_EXTERNAL_ID"
            printf "Using existing External ID: ${GREEN}$EXTERNAL_ID${NC}\n"
        else
            printf "${YELLOW}WARNING: Using new External ID. You will need to update the UseCase in the portal!${NC}\n"
        fi
    fi
    
    # Ask for additional bucket (optional - dda-* pattern is always included)
    echo ""
    printf "${BLUE}Additional S3 bucket for model artifacts (optional)${NC}\n"
    echo "The policy automatically includes access to buckets matching 'dda-*' and '*-dda-*' patterns."
    echo "If your bucket doesn't match these patterns, enter it here."
    read -p "Additional bucket name [press Enter to skip]: " MODEL_ARTIFACTS_BUCKET
    
    echo ""
    printf "${YELLOW}Deploying UseCase Account Role...${NC}\n"
    printf "  Portal Account:         ${GREEN}$PORTAL_ACCOUNT_ID${NC}\n"
    printf "  Target Account:         ${GREEN}$CURRENT_ACCOUNT${NC}\n"
    printf "  External ID:            ${GREEN}$EXTERNAL_ID${NC}\n"
    if [ -n "$MODEL_ARTIFACTS_BUCKET" ]; then
        printf "  Model Artifacts Access: ${GREEN}dda-* and *-dda-* + $MODEL_ARTIFACTS_BUCKET${NC}\n"
    else
        printf "  Model Artifacts Access: ${GREEN}dda-* and *-dda-* buckets${NC}\n"
    fi
    echo ""
    
    read -p "Press Enter to continue..."
    
    cd infrastructure
    
    # Build CDK command
    CDK_CMD="cdk deploy -a \"npx ts-node bin/usecase-account-app.ts\" -c portalAccountId=$PORTAL_ACCOUNT_ID -c externalId=$EXTERNAL_ID"
    if [ -n "$MODEL_ARTIFACTS_BUCKET" ]; then
        CDK_CMD="$CDK_CMD -c modelArtifactsBucket=$MODEL_ARTIFACTS_BUCKET"
    fi
    CDK_CMD="$CDK_CMD --require-approval never"
    
    eval $CDK_CMD
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
    
    # Save config with account ID in filename for multi-account support
    CONFIG_FILE="usecase-account-${CURRENT_ACCOUNT}-config.txt"
    if [ -n "$MODEL_ARTIFACTS_BUCKET" ]; then
        BUCKET_ACCESS="dda-* and *-dda-* + $MODEL_ARTIFACTS_BUCKET"
    else
        BUCKET_ACCESS="dda-* and *-dda-* (automatic)"
    fi
    cat > "$CONFIG_FILE" << EOF
UseCase Account Configuration
=============================
Account ID: $CURRENT_ACCOUNT
Portal Account ID: $PORTAL_ACCOUNT_ID
External ID: $EXTERNAL_ID
Role ARN: $ROLE_ARN
SageMaker Execution Role ARN: $SAGEMAKER_ROLE_ARN
Model Artifacts Access: $BUCKET_ACCESS
Deployment Date: $(date)
EOF
    printf "Configuration saved to: ${GREEN}$CONFIG_FILE${NC}\n"

else
    # ========================================
    # Data Account Setup
    # ========================================
    
    # Check for existing Data Account External ID (account-specific file first, then generic)
    DATA_EXTERNAL_ID=""
    if [ -f "data-account-${CURRENT_ACCOUNT}-config.txt" ]; then
        DATA_EXTERNAL_ID=$(grep "External ID:" "data-account-${CURRENT_ACCOUNT}-config.txt" | head -1 | awk '{print $NF}')
        printf "${BLUE}Found account-specific config: data-account-${CURRENT_ACCOUNT}-config.txt${NC}\n"
    elif [ -f "data-account-config.txt" ]; then
        # Check if the generic config is for this account
        CONFIG_ACCOUNT=$(grep "Data Account ID:" data-account-config.txt | head -1 | awk '{print $NF}')
        if [ "$CONFIG_ACCOUNT" = "$CURRENT_ACCOUNT" ]; then
            DATA_EXTERNAL_ID=$(grep "External ID:" data-account-config.txt | head -1 | awk '{print $NF}')
        fi
    fi
    
    if [ -n "$DATA_EXTERNAL_ID" ]; then
        echo ""
        printf "${YELLOW}Found existing Data Account External ID: ${GREEN}$DATA_EXTERNAL_ID${NC}\n"
        read -p "Use existing External ID? [Y/n]: " USE_EXISTING
        if [ "$USE_EXISTING" != "n" ] && [ "$USE_EXISTING" != "N" ]; then
            EXTERNAL_ID="$DATA_EXTERNAL_ID"
            printf "Using existing External ID: ${GREEN}$EXTERNAL_ID${NC}\n"
        else
            printf "${YELLOW}WARNING: Using new External ID. You will need to update the Data Account in the portal!${NC}\n"
        fi
    fi
    
    echo ""
    printf "${YELLOW}Deploying Data Account Roles...${NC}\n"
    printf "  Portal Account:   ${GREEN}$PORTAL_ACCOUNT_ID${NC}\n"
    printf "  Data Account:     ${GREEN}$CURRENT_ACCOUNT${NC}\n"
    printf "  External ID:      ${GREEN}$EXTERNAL_ID${NC}\n"
    echo ""
    printf "${BLUE}NOTE: Bucket policies will be configured automatically by the portal${NC}\n"
    printf "${BLUE}      when you onboard each UseCase. No manual configuration needed.${NC}\n"
    echo ""
    
    read -p "Press Enter to continue..."
    
    cd infrastructure
    cdk deploy -a "npx ts-node bin/data-account-app.ts" \
        -c portalAccountId=$PORTAL_ACCOUNT_ID \
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
    
    # Save config with account ID in filename for multi-account support
    CONFIG_FILE="data-account-${CURRENT_ACCOUNT}-config.txt"
    cat > "$CONFIG_FILE" << EOF
Data Account Configuration
==========================
Data Account ID: $CURRENT_ACCOUNT
Portal Account ID: $PORTAL_ACCOUNT_ID
External ID: $EXTERNAL_ID
Portal Access Role ARN: $PORTAL_ROLE_ARN
SageMaker Access Role ARN: $SAGEMAKER_ROLE_ARN
Deployment Date: $(date)

NOTE: Bucket policies will be automatically configured by the portal
      when you onboard each UseCase. No manual bucket configuration needed.
EOF
    printf "Configuration saved to: ${GREEN}$CONFIG_FILE${NC}\n"
    
    # Prompt to register in portal for dropdown feature
    echo ""
    printf "${BLUE}========================================${NC}\n"
    printf "${BLUE}Enable Dropdown Feature (Recommended)${NC}\n"
    printf "${BLUE}========================================${NC}\n"
    echo ""
    echo "To use the dropdown feature in UseCase onboarding:"
    echo ""
    printf "  1. Log in to portal as ${GREEN}PortalAdmin${NC}\n"
    printf "  2. Go to ${GREEN}Settings → Data Accounts${NC}\n"
    printf "  3. Click ${GREEN}'Add Data Account'${NC}\n"
    printf "  4. Upload ${GREEN}$CONFIG_FILE${NC}\n"
    printf "  5. Fill in:\n"
    printf "     - Name: ${GREEN}Production Data Account${NC}\n"
    printf "     - Default Bucket: ${GREEN}your-bucket-name${NC}\n"
    printf "     - Region: ${GREEN}us-east-1${NC}\n"
    printf "  6. Click ${GREEN}'Register'${NC}\n"
    echo ""
    printf "Then when creating UseCases:\n"
    printf "  → Select 'Separate Data Account'\n"
    printf "  → Choose from dropdown\n"
    printf "  → All fields auto-filled!\n"
    echo ""
    printf "${YELLOW}Skip this step if you prefer manual entry${NC}\n"
fi

echo ""
printf "${YELLOW}Next: Tag S3 buckets for portal access${NC}\n"
echo "aws s3api put-bucket-tagging --bucket YOUR_BUCKET --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]'"
echo ""
