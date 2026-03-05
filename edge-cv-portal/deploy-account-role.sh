#!/bin/bash

# Deploy cross-account role for UseCase or Data Account access
# Creates IAM role that allows Portal Account to access UseCase/Data Account resources

set -e

# Disable AWS CLI pager to prevent scrolling through JSON output
export AWS_PAGER=""

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
    echo "1) Single Account (for single-account setup in this account)"
    echo "2) UseCase Account (for training/compilation in separate account)"
    echo "3) Data Account (for data storage in separate account)"
    echo ""
    read -p "Enter option (1, 2, or 3): " OPTION
    
    case $OPTION in
        1)
            DEPLOYMENT_TYPE="single-account"
            ;;
        2)
            DEPLOYMENT_TYPE="usecase"
            ;;
        3)
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
    if [ "$DEPLOYMENT_TYPE" != "single-account" ] && [ "$DEPLOYMENT_TYPE" != "usecase" ] && [ "$DEPLOYMENT_TYPE" != "data" ]; then
        echo "Usage: $0 [single-account|usecase|data]"
        echo "Or run without arguments for interactive menu"
        exit 1
    fi
fi

echo ""
echo "Deployment Type: $DEPLOYMENT_TYPE"
echo ""

if [ "$DEPLOYMENT_TYPE" = "single-account" ]; then
    echo "=========================================="
    echo "Single-Account Setup - SageMaker Role"
    echo "=========================================="
    echo ""
    echo "This creates the DDASageMakerExecutionRole in your current account."
    echo "This role is used by SageMaker for training, compilation, and labeling jobs."
    echo ""
    
    CURRENT_ACCOUNT=$(aws sts get-caller-identity --query 'Account' --output text)
    echo "Creating role in account: $CURRENT_ACCOUNT"
    echo ""
    
    # Create trust policy for SageMaker
    # Get current region and map to SageMaker account ID
    CURRENT_REGION=$(aws configure get region || echo "us-east-1")
    
    # Map regions to SageMaker account IDs
    case $CURRENT_REGION in
        us-east-1)
            SAGEMAKER_ACCOUNT="X"
            ;;
        us-west-2)
            SAGEMAKER_ACCOUNT="X"
            ;;
        eu-west-1)
            SAGEMAKER_ACCOUNT="X"
            ;;
        eu-central-1)
            SAGEMAKER_ACCOUNT="X"
            ;;
        ap-northeast-1)
            SAGEMAKER_ACCOUNT="X"
            ;;
        ap-southeast-1)
            SAGEMAKER_ACCOUNT="X"
            ;;
        ap-southeast-2)
            SAGEMAKER_ACCOUNT="X"
            ;;
        *)
            # Default to us-east-1 if region not found
            SAGEMAKER_ACCOUNT='aws sts get-caller-identity --query Account --output text'
            echo sagemakeraccountid= $SAGEMAKER_ACCOUNT
            ;;
    esac
    
    TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "sagemaker.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    },
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::$SAGEMAKER_ACCOUNT:root"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
)
    
    # Create role
    if aws iam create-role \
        --role-name DDASageMakerExecutionRole \
        --assume-role-policy-document "$TRUST_POLICY" 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Created DDASageMakerExecutionRole"
    else
        ROLE_EXISTS=$(aws iam get-role --role-name DDASageMakerExecutionRole 2>/dev/null || echo "")
        if [ -n "$ROLE_EXISTS" ]; then
            echo -e "${YELLOW}⚠${NC} DDASageMakerExecutionRole already exists"
            echo "Updating trust policy with region-aware SageMaker account..."
            # Create a temporary file for the policy document
            TEMP_POLICY=$(mktemp)
            cat > "$TEMP_POLICY" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "sagemaker.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    },
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::$SAGEMAKER_ACCOUNT:root"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
            if aws iam update-assume-role-policy \
                --role-name DDASageMakerExecutionRole \
                --policy-document file://"$TEMP_POLICY"; then
                echo -e "${GREEN}✓${NC} Trust policy updated"
            else
                echo -e "${RED}✗ Failed to update trust policy${NC}"
            fi
            rm -f "$TEMP_POLICY"
        else
            echo -e "${RED}✗ Failed to create role. Check IAM permissions.${NC}"
            exit 1
        fi
    fi
    
    # Attach inline policies
    echo "Attaching policies..."
    
    # S3 Policy
    S3_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:GetBucketCors",
        "s3:PutBucketCors"
      ],
      "Resource": [
        "arn:aws:s3:::*",
        "arn:aws:s3:::*/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:ListBucket",
        "s3:GetBucketVersioning",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::sagemaker-*",
        "arn:aws:s3:::sagemaker-*/*"
      ]
    }
  ]
}
EOF
)
    
    aws iam put-role-policy \
        --role-name DDASageMakerExecutionRole \
        --policy-name S3Access \
        --policy-document "$S3_POLICY" 2>/dev/null && echo -e "${GREEN}✓${NC} S3 policy attached" || echo -e "${YELLOW}⚠${NC} Could not attach S3 policy"
    
    # CloudWatch Logs Policy
    LOGS_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "cloudwatch:PutMetricData",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "*"
    }
  ]
}
EOF
)
    
    aws iam put-role-policy \
        --role-name DDASageMakerExecutionRole \
        --policy-name CloudWatchLogs \
        --policy-document "$LOGS_POLICY" 2>/dev/null && echo -e "${GREEN}✓${NC} CloudWatch Logs policy attached" || echo -e "${YELLOW}⚠${NC} Could not attach CloudWatch Logs policy"
    
    # SageMaker Policy
    SAGEMAKER_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sagemaker:CreateTrainingJob",
        "sagemaker:DescribeTrainingJob",
        "sagemaker:StopTrainingJob",
        "sagemaker:ListTrainingJobs",
        "sagemaker:CreateCompilationJob",
        "sagemaker:DescribeCompilationJob",
        "sagemaker:StopCompilationJob",
        "sagemaker:ListCompilationJobs",
        "sagemaker:CreateLabelingJob",
        "sagemaker:DescribeLabelingJob",
        "sagemaker:ListLabelingJobs",
        "sagemaker:CreateModel",
        "sagemaker:DescribeModel",
        "sagemaker:DeleteModel",
        "sagemaker:ListModels"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "iam:PassRole",
        "iam:GetRole"
      ],
      "Resource": "arn:aws:iam::*:role/DDASageMakerExecutionRole"
    }
  ]
}
EOF
)
    
    aws iam put-role-policy \
        --role-name DDASageMakerExecutionRole \
        --policy-name SageMakerAccess \
        --policy-document "$SAGEMAKER_POLICY" 2>/dev/null && echo -e "${GREEN}✓${NC} SageMaker policy attached" || echo -e "${YELLOW}⚠${NC} Could not attach SageMaker policy"
    
    # PassRole Policy - allows role to be passed to SageMaker service
    PASS_ROLE_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::$CURRENT_ACCOUNT:role/DDASageMakerExecutionRole",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "sagemaker.amazonaws.com"
        }
      }
    }
  ]
}
EOF
)
    
    aws iam put-role-policy \
        --role-name DDASageMakerExecutionRole \
        --policy-name SageMakerPassRole \
        --policy-document "$PASS_ROLE_POLICY" 2>/dev/null && echo -e "${GREEN}✓${NC} PassRole policy attached" || echo -e "${YELLOW}⚠${NC} Could not attach PassRole policy"
    
    # ECR Policy
    ECR_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage"
      ],
      "Resource": "*"
    }
  ]
}
EOF
)
    
    aws iam put-role-policy \
        --role-name DDASageMakerExecutionRole \
        --policy-name ECRAccess \
        --policy-document "$ECR_POLICY" 2>/dev/null && echo -e "${GREEN}✓${NC} ECR policy attached" || echo -e "${YELLOW}⚠${NC} Could not attach ECR policy"
    
    # Create managed policy for Greengrass devices to access component artifacts
    echo ""
    echo "Creating managed policy for Greengrass devices..."
    
    GREENGRASS_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowPortalComponentBucketAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::dda-component-*",
        "arn:aws:s3:::dda-component-*/*"
      ]
    },
    {
      "Sid": "AllowDDABucketPatternAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetBucketLocation",
        "s3:HeadObject"
      ],
      "Resource": [
        "arn:aws:s3:::dda-*",
        "arn:aws:s3:::dda-*/*",
        "arn:aws:s3:::*-dda-*",
        "arn:aws:s3:::*-dda-*/*"
      ]
    },
    {
      "Sid": "AllowInferenceResultsUpload",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:PutObjectTagging",
        "s3:GetObject",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::dda-inference-results-*",
        "arn:aws:s3:::dda-inference-results-*/*"
      ]
    }
  ]
}
EOF
)
    
    if aws iam create-policy \
        --policy-name DDAPortalComponentAccessPolicy \
        --policy-document "$GREENGRASS_POLICY" 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Created DDAPortalComponentAccessPolicy managed policy"
    else
        POLICY_EXISTS=$(aws iam get-policy --policy-arn "arn:aws:iam::$(aws sts get-caller-identity --query 'Account' --output text):policy/DDAPortalComponentAccessPolicy" 2>/dev/null || echo "")
        if [ -n "$POLICY_EXISTS" ]; then
            echo -e "${YELLOW}⚠${NC} DDAPortalComponentAccessPolicy already exists"
        else
            echo -e "${RED}✗ Failed to create managed policy. Check IAM permissions.${NC}"
        fi
    fi
    
    # Associate Greengrass service role with this account
    # This is required for Greengrass CreateDeployment to interact with IoT Core
    # (e.g., creating IoT Jobs when deploying to individual things)
    echo ""
    echo "Setting up Greengrass service role..."
    
    # Check if a Greengrass service role is already associated
    EXISTING_GG_ROLE=$(aws greengrassv2 get-service-role-for-account --region "$CURRENT_REGION" 2>/dev/null || echo "")
    
    if echo "$EXISTING_GG_ROLE" | grep -q "roleArn"; then
        echo -e "${GREEN}✓${NC} Greengrass service role already associated"
        echo "  $(echo "$EXISTING_GG_ROLE" | grep -o '"roleArn": "[^"]*"')"
    else
        # Check if the Greengrass_ServiceRole exists
        if aws iam get-role --role-name Greengrass_ServiceRole 2>/dev/null > /dev/null; then
            echo -e "${YELLOW}⚠${NC} Greengrass_ServiceRole exists but is not associated"
        else
            echo "Creating Greengrass_ServiceRole..."
            GG_TRUST_POLICY=$(cat <<GGEOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "greengrass.amazonaws.com"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "$CURRENT_ACCOUNT"
        }
      }
    }
  ]
}
GGEOF
)
            if aws iam create-role \
                --role-name Greengrass_ServiceRole \
                --assume-role-policy-document "$GG_TRUST_POLICY" 2>/dev/null; then
                echo -e "${GREEN}✓${NC} Created Greengrass_ServiceRole"
            else
                echo -e "${RED}✗ Failed to create Greengrass_ServiceRole${NC}"
            fi
        fi
        
        # Attach the managed policy
        aws iam attach-role-policy \
            --role-name Greengrass_ServiceRole \
            --policy-arn arn:aws:iam::aws:policy/service-role/AWSGreengrassResourceAccessRolePolicy 2>/dev/null \
            && echo -e "${GREEN}✓${NC} Attached AWSGreengrassResourceAccessRolePolicy" \
            || echo -e "${YELLOW}⚠${NC} Policy may already be attached"
        
        # Wait for role propagation
        echo "Waiting for IAM role propagation..."
        sleep 10
        
        # Associate the role with the account
        GG_ROLE_ARN="arn:aws:iam::${CURRENT_ACCOUNT}:role/Greengrass_ServiceRole"
        if aws greengrassv2 associate-service-role-to-account \
            --role-arn "$GG_ROLE_ARN" \
            --region "$CURRENT_REGION" 2>/dev/null; then
            echo -e "${GREEN}✓${NC} Associated Greengrass service role with account in $CURRENT_REGION"
        else
            echo -e "${RED}✗ Failed to associate Greengrass service role${NC}"
            echo "  You may need to do this manually:"
            echo "  aws greengrassv2 associate-service-role-to-account --role-arn $GG_ROLE_ARN --region $CURRENT_REGION"
        fi
    fi
    
    echo ""
    echo -e "${GREEN}=========================================="
    echo "Single-Account Role Created Successfully!"
    echo "==========================================${NC}"
    echo ""
    echo "The following have been created:"
    echo "  • DDASageMakerExecutionRole - for SageMaker training/compilation/labeling"
    echo "  • DDAPortalComponentAccessPolicy - for Greengrass device access to model artifacts"
    echo "  • Greengrass_ServiceRole - for Greengrass to access IoT Core services"
    echo ""
    echo "Next steps:"
    echo "1. Attach the managed policy to your Greengrass device role:"
    echo "   aws iam attach-role-policy \\"
    echo "     --role-name GreengrassV2TokenExchangeRole \\"
    echo "     --policy-arn arn:aws:iam::$(aws sts get-caller-identity --query 'Account' --output text):policy/DDAPortalComponentAccessPolicy"
    echo ""
    echo "2. You can now create UseCases in the Portal."
    echo ""

elif [ "$DEPLOYMENT_TYPE" = "usecase" ]; then
    echo "=========================================="
    echo "UseCase Account Setup (CDK)"
    echo "=========================================="
    echo ""
    echo "This deploys the DDA UseCase Account stack using AWS CDK."
    echo "It creates: IAM cross-account role, SageMaker execution role,"
    echo "Greengrass device policy, and inference results S3 bucket."
    echo ""
    
    # Get Portal Account ID
    read -p "Enter Portal Account ID: " PORTAL_ACCOUNT_ID
    
    if [ -z "$PORTAL_ACCOUNT_ID" ]; then
        echo -e "${RED}✗ Portal Account ID is required${NC}"
        exit 1
    fi
    
    # Generate or get External ID
    # Check for existing config file to reuse external ID on re-deploys
    CURRENT_ACCOUNT=$(aws sts get-caller-identity --query 'Account' --output text)
    EXISTING_CONFIG="usecase-account-${CURRENT_ACCOUNT}-config.txt"
    EXISTING_EXTERNAL_ID=""
    if [ -f "$EXISTING_CONFIG" ]; then
        EXISTING_EXTERNAL_ID=$(grep '^EXTERNAL_ID=' "$EXISTING_CONFIG" | cut -d'=' -f2)
    fi
    
    if [ -n "$EXISTING_EXTERNAL_ID" ]; then
        echo -e "${YELLOW}⚠ Found existing config: $EXISTING_CONFIG${NC}"
        echo -e "${YELLOW}  Existing External ID: ${EXISTING_EXTERNAL_ID:0:8}...${EXISTING_EXTERNAL_ID: -4}${NC}"
        read -p "Reuse existing External ID? (Y/n): " REUSE_ID
        if [ "$REUSE_ID" != "n" ] && [ "$REUSE_ID" != "N" ]; then
            EXTERNAL_ID="$EXISTING_EXTERNAL_ID"
            echo -e "${GREEN}✓ Reusing existing External ID${NC}"
        else
            read -p "Enter new External ID (leave blank to generate one): " EXTERNAL_ID
            if [ -z "$EXTERNAL_ID" ]; then
                EXTERNAL_ID=$(uuidgen 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "dda-$(date +%s)")
                echo -e "${BLUE}Generated new External ID: $EXTERNAL_ID${NC}"
                echo -e "${RED}⚠ WARNING: If you already registered this account in the portal, you MUST update the External ID there too!${NC}"
            fi
        fi
    else
        read -p "Enter External ID (leave blank to generate one): " EXTERNAL_ID
        if [ -z "$EXTERNAL_ID" ]; then
            EXTERNAL_ID=$(uuidgen 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "dda-$(date +%s)")
            echo -e "${BLUE}Generated External ID: $EXTERNAL_ID${NC}"
        fi
    fi
    
    echo ""
    echo "Deploying UseCase Account stack..."
    echo "Portal Account: $PORTAL_ACCOUNT_ID"
    echo "External ID: ${EXTERNAL_ID:0:8}...${EXTERNAL_ID: -4}"
    echo ""
    
    # Check CDK bootstrap
    BOOTSTRAP_VERSION=$(aws ssm get-parameter --name /cdk-bootstrap/hnb659fds/version --query 'Parameter.Value' --output text 2>/dev/null || echo "0")

    # Build CDK context args
    CDK_CONTEXT="-c portalAccountId=$PORTAL_ACCOUNT_ID -c externalId=$EXTERNAL_ID"
    if [ "$BOOTSTRAP_VERSION" = "0" ] || [ "$BOOTSTRAP_VERSION" -lt 21 ]; then
        echo -e "${YELLOW}⚠ CDK bootstrap is missing or outdated (version: $BOOTSTRAP_VERSION, need: 21+)${NC}"
        echo "Running cdk bootstrap..."
        cd infrastructure
        npx cdk bootstrap
        cd ..
    fi
    
    # Build CDK context args
    CDK_CONTEXT="-c portalAccountId=$PORTAL_ACCOUNT_ID -c externalId=$EXTERNAL_ID"
    
    # Deploy the stack
    cd infrastructure
    npx cdk deploy DDAPortalUseCaseAccountStack \
        -a "npx ts-node bin/usecase-account-app.ts" \
        $CDK_CONTEXT \
        --require-approval never
    cd ..
    
    # Get outputs
    CURRENT_ACCOUNT=$(aws sts get-caller-identity --query 'Account' --output text)
    ROLE_ARN="arn:aws:iam::${CURRENT_ACCOUNT}:role/DDAPortalAccessRole"
    
    # Associate Greengrass service role with this account
    # Required for Greengrass CreateDeployment to call IoT Core services
    # (e.g., creating IoT Jobs when deploying to individual things)
    echo ""
    echo "Setting up Greengrass service role..."
    
    CURRENT_REGION=$(aws configure get region || echo "us-east-1")
    EXISTING_GG_ROLE=$(aws greengrassv2 get-service-role-for-account --region "$CURRENT_REGION" 2>/dev/null || echo "")
    
    if echo "$EXISTING_GG_ROLE" | grep -q "roleArn"; then
        echo -e "${GREEN}✓${NC} Greengrass service role already associated"
        echo "  $(echo "$EXISTING_GG_ROLE" | grep -o '"roleArn": "[^"]*"')"
    else
        if aws iam get-role --role-name Greengrass_ServiceRole 2>/dev/null > /dev/null; then
            echo -e "${YELLOW}⚠${NC} Greengrass_ServiceRole exists but is not associated"
        else
            echo "Creating Greengrass_ServiceRole..."
            GG_TRUST_POLICY=$(cat <<GGEOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "greengrass.amazonaws.com"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "$CURRENT_ACCOUNT"
        }
      }
    }
  ]
}
GGEOF
)
            if aws iam create-role \
                --role-name Greengrass_ServiceRole \
                --assume-role-policy-document "$GG_TRUST_POLICY" 2>/dev/null; then
                echo -e "${GREEN}✓${NC} Created Greengrass_ServiceRole"
            else
                echo -e "${RED}✗ Failed to create Greengrass_ServiceRole${NC}"
            fi
        fi
        
        aws iam attach-role-policy \
            --role-name Greengrass_ServiceRole \
            --policy-arn arn:aws:iam::aws:policy/service-role/AWSGreengrassResourceAccessRolePolicy 2>/dev/null \
            && echo -e "${GREEN}✓${NC} Attached AWSGreengrassResourceAccessRolePolicy" \
            || echo -e "${YELLOW}⚠${NC} Policy may already be attached"
        
        echo "Waiting for IAM role propagation..."
        sleep 10
        
        GG_ROLE_ARN="arn:aws:iam::${CURRENT_ACCOUNT}:role/Greengrass_ServiceRole"
        if aws greengrassv2 associate-service-role-to-account \
            --role-arn "$GG_ROLE_ARN" \
            --region "$CURRENT_REGION" 2>/dev/null; then
            echo -e "${GREEN}✓${NC} Associated Greengrass service role with account in $CURRENT_REGION"
        else
            echo -e "${RED}✗ Failed to associate Greengrass service role${NC}"
            echo "  You may need to do this manually:"
            echo "  aws greengrassv2 associate-service-role-to-account --role-arn $GG_ROLE_ARN --region $CURRENT_REGION"
        fi
    fi
    
    # Save configuration
    CONFIG_FILE="usecase-account-${CURRENT_ACCOUNT}-config.txt"
    cat > "$CONFIG_FILE" << EOF
# DDA Portal - UseCase Account Configuration
# Generated: $(date)
# Deployed via: CDK Stack (DDAPortalUseCaseAccountStack)

PORTAL_ACCOUNT_ID=$PORTAL_ACCOUNT_ID
USECASE_ACCOUNT_ID=$CURRENT_ACCOUNT
ROLE_ARN=$ROLE_ARN
EXTERNAL_ID=$EXTERNAL_ID
SAGEMAKER_ROLE_ARN=arn:aws:iam::${CURRENT_ACCOUNT}:role/DDASageMakerExecutionRole

# Resources created:
# - DDAPortalAccessRole (cross-account access)
# - DDASageMakerExecutionRole (training/compilation)
# - DDAPortalComponentAccessPolicy (Greengrass device access)
# - dda-inference-results-${CURRENT_ACCOUNT} (inference results bucket)
EOF
    
    echo ""
    echo -e "${GREEN}=========================================="
    echo "UseCase Account Stack Deployed Successfully!"
    echo "==========================================${NC}"
    echo ""
    echo "Configuration saved to: $CONFIG_FILE"
    echo ""
    echo "Next steps:"
    echo "1. In the Portal, go to Settings → UseCases"
    echo "2. Click 'Add UseCase' and fill in:"
    echo "   - Account ID: $CURRENT_ACCOUNT"
    echo "   - Role ARN: $ROLE_ARN"
    echo "   - External ID: $EXTERNAL_ID"
    echo "   - SageMaker Role ARN: arn:aws:iam::${CURRENT_ACCOUNT}:role/DDASageMakerExecutionRole"
    echo ""
    
elif [ "$DEPLOYMENT_TYPE" = "data" ]; then
    echo "=========================================="
    echo "Data Account Setup (CDK)"
    echo "=========================================="
    echo ""
    echo "This deploys the DDA Data Account stack using AWS CDK."
    echo "It creates: IAM cross-account role for Portal and UseCase accounts"
    echo "to access S3 buckets for training data storage."
    echo ""
    
    # Get Portal Account ID
    read -p "Enter Portal Account ID: " PORTAL_ACCOUNT_ID
    
    if [ -z "$PORTAL_ACCOUNT_ID" ]; then
        echo -e "${RED}✗ Portal Account ID is required${NC}"
        exit 1
    fi
    
    # Generate or get External ID
    # Check for existing config file to reuse external ID on re-deploys
    CURRENT_ACCOUNT=$(aws sts get-caller-identity --query 'Account' --output text)
    EXISTING_CONFIG="data-account-${CURRENT_ACCOUNT}-config.txt"
    EXISTING_EXTERNAL_ID=""
    if [ -f "$EXISTING_CONFIG" ]; then
        EXISTING_EXTERNAL_ID=$(grep '^EXTERNAL_ID=' "$EXISTING_CONFIG" | cut -d'=' -f2)
    fi
    
    if [ -n "$EXISTING_EXTERNAL_ID" ]; then
        echo -e "${YELLOW}⚠ Found existing config: $EXISTING_CONFIG${NC}"
        echo -e "${YELLOW}  Existing External ID: ${EXISTING_EXTERNAL_ID:0:8}...${EXISTING_EXTERNAL_ID: -4}${NC}"
        read -p "Reuse existing External ID? (Y/n): " REUSE_ID
        if [ "$REUSE_ID" != "n" ] && [ "$REUSE_ID" != "N" ]; then
            EXTERNAL_ID="$EXISTING_EXTERNAL_ID"
            echo -e "${GREEN}✓ Reusing existing External ID${NC}"
        else
            read -p "Enter new External ID (leave blank to generate one): " EXTERNAL_ID
            if [ -z "$EXTERNAL_ID" ]; then
                EXTERNAL_ID=$(uuidgen 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "dda-$(date +%s)")
                echo -e "${BLUE}Generated new External ID: $EXTERNAL_ID${NC}"
                echo -e "${RED}⚠ WARNING: If you already registered this account in the portal, you MUST update the External ID there too!${NC}"
            fi
        fi
    else
        read -p "Enter External ID (leave blank to generate one): " EXTERNAL_ID
        if [ -z "$EXTERNAL_ID" ]; then
            EXTERNAL_ID=$(uuidgen 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "dda-$(date +%s)")
            echo -e "${BLUE}Generated External ID: $EXTERNAL_ID${NC}"
        fi
    fi
    
    # Optional: data bucket names
    read -p "Enter data bucket names (comma-separated, leave blank for auto-config): " DATA_BUCKETS
    
    echo ""
    echo "Deploying Data Account stack..."
    echo "Portal Account: $PORTAL_ACCOUNT_ID"
    echo "External ID: ${EXTERNAL_ID:0:8}...${EXTERNAL_ID: -4}"
    echo ""
    
    # Check CDK bootstrap
    BOOTSTRAP_VERSION=$(aws ssm get-parameter --name /cdk-bootstrap/hnb659fds/version --query 'Parameter.Value' --output text 2>/dev/null || echo "0")
    if [ "$BOOTSTRAP_VERSION" = "0" ] || [ "$BOOTSTRAP_VERSION" -lt 21 ]; then
        echo -e "${YELLOW}⚠ CDK bootstrap is missing or outdated (version: $BOOTSTRAP_VERSION, need: 21+)${NC}"
        echo "Running cdk bootstrap..."
        cd infrastructure
        npx cdk bootstrap
        cd ..
    fi
    
    # Build CDK context args
    CDK_CONTEXT="-c portalAccountId=$PORTAL_ACCOUNT_ID -c externalId=$EXTERNAL_ID"
    if [ -n "$DATA_BUCKETS" ]; then
        CDK_CONTEXT="$CDK_CONTEXT -c dataBucketNames=$DATA_BUCKETS"
    fi
    
    # Deploy the stack
    cd infrastructure
    npx cdk deploy DDAPortalDataAccountStack \
        -a "npx ts-node bin/data-account-app.ts" \
        $CDK_CONTEXT \
        --require-approval never
    cd ..
    
    # Get outputs
    CURRENT_ACCOUNT=$(aws sts get-caller-identity --query 'Account' --output text)
    ROLE_ARN="arn:aws:iam::${CURRENT_ACCOUNT}:role/DDAPortalDataAccessRole"
    
    # Save configuration
    CONFIG_FILE="data-account-${CURRENT_ACCOUNT}-config.txt"
    cat > "$CONFIG_FILE" << EOF
# DDA Portal - Data Account Configuration
# Generated: $(date)
# Deployed via: CDK Stack (DDAPortalDataAccountStack)

PORTAL_ACCOUNT_ID=$PORTAL_ACCOUNT_ID
DATA_ACCOUNT_ID=$CURRENT_ACCOUNT
ROLE_ARN=$ROLE_ARN
EXTERNAL_ID=$EXTERNAL_ID
EOF
    
    echo ""
    echo -e "${GREEN}=========================================="
    echo "Data Account Stack Deployed Successfully!"
    echo "==========================================${NC}"
    echo ""
    echo "Configuration saved to: $CONFIG_FILE"
    echo ""
    echo "Next steps:"
    echo "1. In the Portal, go to Settings → Data Accounts"
    echo "2. Click 'Add Data Account' and fill in:"
    echo "   - Account ID: $CURRENT_ACCOUNT"
    echo "   - Role ARN: $ROLE_ARN"
    echo "   - External ID: $EXTERNAL_ID"
    echo ""
fi
