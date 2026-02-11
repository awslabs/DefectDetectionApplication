#!/bin/bash

# Deploy cross-account role for UseCase or Data Account access
# Creates IAM role that allows Portal Account to access UseCase/Data Account resources

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Check AWS CLI is available
if ! command -v aws &> /dev/null; then
    echo -e "${RED}✗ AWS CLI is not installed or not in PATH${NC}"
    echo "Please install AWS CLI: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    exit 1
fi

# Check AWS credentials are configured
if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}✗ AWS credentials are not configured${NC}"
    echo "Please configure AWS credentials using: aws configure"
    exit 1
fi

echo "=========================================="
echo "DDA Portal - Account Role Deployment"
echo "=========================================="
echo ""
echo -e "${BLUE}ℹ Current AWS Account: $(aws sts get-caller-identity --query 'Account' --output text)${NC}"
echo ""

# Show menu if no arguments provided
if [ $# -eq 0 ]; then
    echo "Select deployment type:"
    echo ""
    echo "1) UseCase Account (for training/compilation in separate account)"
    echo "2) Data Account (for data storage in separate account)"
    echo ""
    read -p "Enter option (1 or 2): " OPTION
    
    case $OPTION in
        1)
            DEPLOYMENT_TYPE="usecase"
            ;;
        2)
            DEPLOYMENT_TYPE="data"
            ;;
        *)
            echo -e "${RED}Invalid option${NC}"
            exit 1
            ;;
    esac
else
    # Support legacy command line arguments
    DEPLOYMENT_TYPE=${1:-}
    if [ "$DEPLOYMENT_TYPE" != "usecase" ] && [ "$DEPLOYMENT_TYPE" != "data" ]; then
        echo "Usage: $0 [usecase|data]"
        echo "Or run without arguments for interactive menu"
        exit 1
    fi
fi

echo ""
echo "Deployment Type: $DEPLOYMENT_TYPE"
echo ""

if [ "$DEPLOYMENT_TYPE" = "usecase" ]; then
    echo "=========================================="
    echo "UseCase Account Role Setup"
    echo "=========================================="
    echo ""
    echo "This creates a role in the UseCase Account that allows the Portal Account"
    echo "to access SageMaker, S3, and Greengrass resources for training and deployment."
    echo ""
    
    # Get Portal Account ID
    read -p "Enter Portal Account ID: " PORTAL_ACCOUNT_ID
    
    if [ -z "$PORTAL_ACCOUNT_ID" ]; then
        echo -e "${RED}✗ Portal Account ID is required${NC}"
        exit 1
    fi
    
    # Validate account ID format
    if ! [[ "$PORTAL_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
        echo -e "${RED}✗ Invalid account ID format (must be 12 digits)${NC}"
        exit 1
    fi
    
    # Check if Portal Account is different from current account
    CURRENT_ACCOUNT=$(aws sts get-caller-identity --query 'Account' --output text)
    if [ "$PORTAL_ACCOUNT_ID" = "$CURRENT_ACCOUNT" ]; then
        echo -e "${YELLOW}⚠ Warning: Portal Account ID is the same as current account${NC}"
        echo "This is a single-account setup. You don't need to deploy a cross-account role."
        echo "In the Portal, use: arn:aws:iam::$CURRENT_ACCOUNT:root as the Role ARN"
        read -p "Continue anyway? (y/n): " CONTINUE
        if [ "$CONTINUE" != "y" ] && [ "$CONTINUE" != "Y" ]; then
            echo "Cancelled."
            exit 0
        fi
    fi
    
    echo ""
    echo "Creating UseCase Account role..."
    echo "Portal Account: $PORTAL_ACCOUNT_ID"
    echo ""
    
    # Create trust policy for UseCase Account
    TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::$PORTAL_ACCOUNT_ID:root"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
)
    
    # Create role
    if aws iam create-role \
        --role-name DDAPortalUseCaseRole \
        --assume-role-policy-document "$TRUST_POLICY" 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Created DDAPortalUseCaseRole"
    else
        ROLE_EXISTS=$(aws iam get-role --role-name DDAPortalUseCaseRole 2>/dev/null || echo "")
        if [ -n "$ROLE_EXISTS" ]; then
            echo -e "${YELLOW}⚠${NC} DDAPortalUseCaseRole already exists"
            echo "Updating trust policy..."
            aws iam update-assume-role-policy-document \
                --role-name DDAPortalUseCaseRole \
                --policy-document "$TRUST_POLICY" 2>/dev/null || true
            echo -e "${GREEN}✓${NC} Trust policy updated"
        else
            echo -e "${RED}✗ Failed to create role. Check IAM permissions.${NC}"
            exit 1
        fi
    fi
    
    # Attach policies for UseCase Account
    echo "Attaching policies..."
    
    if aws iam attach-role-policy \
        --role-name DDAPortalUseCaseRole \
        --policy-arn arn:aws:iam::aws:policy/AmazonSageMakerFullAccess 2>/dev/null; then
        echo -e "${GREEN}✓${NC} SageMaker policy attached"
    else
        echo -e "${YELLOW}⚠${NC} Could not attach SageMaker policy (may already be attached)"
    fi
    
    if aws iam attach-role-policy \
        --role-name DDAPortalUseCaseRole \
        --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess 2>/dev/null; then
        echo -e "${GREEN}✓${NC} S3 policy attached"
    else
        echo -e "${YELLOW}⚠${NC} Could not attach S3 policy (may already be attached)"
    fi
    
    if aws iam attach-role-policy \
        --role-name DDAPortalUseCaseRole \
        --policy-arn arn:aws:iam::aws:policy/AWSGreengrassFullAccess 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Greengrass policy attached"
    else
        echo -e "${YELLOW}⚠${NC} Could not attach Greengrass policy (may already be attached)"
    fi
    
    # Get role ARN and external ID
    ROLE_ARN=$(aws iam get-role --role-name DDAPortalUseCaseRole --query 'Role.Arn' --output text)
    EXTERNAL_ID=$(openssl rand -hex 16)
    
    # Save configuration
    CONFIG_FILE="usecase-account-$(aws sts get-caller-identity --query 'Account' --output text)-config.txt"
    
    cat > "$CONFIG_FILE" << EOF
# DDA Portal - UseCase Account Configuration
# Generated: $(date)

# Portal Account ID (where Portal is deployed)
PORTAL_ACCOUNT_ID=$PORTAL_ACCOUNT_ID

# UseCase Account ID (this account)
USECASE_ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)

# Role ARN (use this in Portal when creating UseCase)
ROLE_ARN=$ROLE_ARN

# External ID (use this in Portal when creating UseCase)
EXTERNAL_ID=$EXTERNAL_ID

# SageMaker Execution Role (optional, for training)
SAGEMAKER_ROLE_ARN=$(aws iam get-role --role-name DDAPortalUseCaseRole --query 'Role.Arn' --output text)
EOF
    
    echo ""
    echo -e "${GREEN}=========================================="
    echo "UseCase Account Role Created Successfully!"
    echo "==========================================${NC}"
    echo ""
    echo "Configuration saved to: $CONFIG_FILE"
    echo ""
    echo "Next steps:"
    echo "1. Copy the configuration file to your Portal Account"
    echo "2. In the Portal, go to Settings → UseCases"
    echo "3. Click 'Add UseCase' and fill in:"
    echo "   - Account ID: $(aws sts get-caller-identity --query 'Account' --output text)"
    echo "   - Role ARN: $ROLE_ARN"
    echo "   - External ID: $EXTERNAL_ID"
    echo ""
    
elif [ "$DEPLOYMENT_TYPE" = "data" ]; then
    echo "=========================================="
    echo "Data Account Role Setup"
    echo "=========================================="
    echo ""
    echo "This creates a role in the Data Account that allows the Portal Account"
    echo "to access S3 buckets for training data storage."
    echo ""
    
    # Get Portal Account ID
    read -p "Enter Portal Account ID: " PORTAL_ACCOUNT_ID
    
    if [ -z "$PORTAL_ACCOUNT_ID" ]; then
        echo -e "${RED}✗ Portal Account ID is required${NC}"
        exit 1
    fi
    
    # Validate account ID format
    if ! [[ "$PORTAL_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
        echo -e "${RED}✗ Invalid account ID format (must be 12 digits)${NC}"
        exit 1
    fi
    
    # Check if Portal Account is different from current account
    CURRENT_ACCOUNT=$(aws sts get-caller-identity --query 'Account' --output text)
    if [ "$PORTAL_ACCOUNT_ID" = "$CURRENT_ACCOUNT" ]; then
        echo -e "${YELLOW}⚠ Warning: Portal Account ID is the same as current account${NC}"
        echo "This is a single-account setup. You don't need to deploy a cross-account role."
        echo "In the Portal, use the same account for both UseCase and Data Account."
        read -p "Continue anyway? (y/n): " CONTINUE
        if [ "$CONTINUE" != "y" ] && [ "$CONTINUE" != "Y" ]; then
            echo "Cancelled."
            exit 0
        fi
    fi
    
    echo ""
    echo "Creating Data Account role..."
    echo "Portal Account: $PORTAL_ACCOUNT_ID"
    echo ""
    
    # Create trust policy for Data Account
    TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::$PORTAL_ACCOUNT_ID:root"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
)
    
    # Create role
    if aws iam create-role \
        --role-name DDAPortalDataAccessRole \
        --assume-role-policy-document "$TRUST_POLICY" 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Created DDAPortalDataAccessRole"
    else
        ROLE_EXISTS=$(aws iam get-role --role-name DDAPortalDataAccessRole 2>/dev/null || echo "")
        if [ -n "$ROLE_EXISTS" ]; then
            echo -e "${YELLOW}⚠${NC} DDAPortalDataAccessRole already exists"
            echo "Updating trust policy..."
            aws iam update-assume-role-policy-document \
                --role-name DDAPortalDataAccessRole \
                --policy-document "$TRUST_POLICY" 2>/dev/null || true
            echo -e "${GREEN}✓${NC} Trust policy updated"
        else
            echo -e "${RED}✗ Failed to create role. Check IAM permissions.${NC}"
            exit 1
        fi
    fi
    
    # Attach policies for Data Account
    echo "Attaching policies..."
    
    if aws iam attach-role-policy \
        --role-name DDAPortalDataAccessRole \
        --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess 2>/dev/null; then
        echo -e "${GREEN}✓${NC} S3 policy attached"
    else
        echo -e "${YELLOW}⚠${NC} Could not attach S3 policy (may already be attached)"
    fi
    
    if aws iam attach-role-policy \
        --role-name DDAPortalDataAccessRole \
        --policy-arn arn:aws:iam::aws:policy/CloudWatchLogsReadOnlyAccess 2>/dev/null; then
        echo -e "${GREEN}✓${NC} CloudWatch Logs policy attached"
    else
        echo -e "${YELLOW}⚠${NC} Could not attach CloudWatch Logs policy (may already be attached)"
    fi
    
    # Get role ARN and external ID
    ROLE_ARN=$(aws iam get-role --role-name DDAPortalDataAccessRole --query 'Role.Arn' --output text)
    EXTERNAL_ID=$(openssl rand -hex 16)
    
    # Save configuration
    CONFIG_FILE="data-account-$(aws sts get-caller-identity --query 'Account' --output text)-config.txt"
    
    cat > "$CONFIG_FILE" << EOF
# DDA Portal - Data Account Configuration
# Generated: $(date)

# Portal Account ID (where Portal is deployed)
PORTAL_ACCOUNT_ID=$PORTAL_ACCOUNT_ID

# Data Account ID (this account)
DATA_ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)

# Role ARN (use this in Portal when configuring Data Account)
ROLE_ARN=$ROLE_ARN

# External ID (use this in Portal when configuring Data Account)
EXTERNAL_ID=$EXTERNAL_ID
EOF
    
    echo ""
    echo -e "${GREEN}=========================================="
    echo "Data Account Role Created Successfully!"
    echo "==========================================${NC}"
    echo ""
    echo "Configuration saved to: $CONFIG_FILE"
    echo ""
    echo "Next steps:"
    echo "1. Copy the configuration file to your Portal Account"
    echo "2. In the Portal, go to Settings → Data Accounts"
    echo "3. Click 'Add Data Account' and fill in:"
    echo "   - Account ID: $(aws sts get-caller-identity --query 'Account' --output text)"
    echo "   - Role ARN: $ROLE_ARN"
    echo "   - External ID: $EXTERNAL_ID"
    echo ""
fi
