#!/bin/bash
# Note: Not using 'set -e' to allow proper error handling

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

printf "${GREEN}========================================${NC}\n"
printf "${GREEN}DDA Portal - Account Role Setup${NC}\n"
printf "${GREEN}========================================${NC}\n"
echo ""

# Check if we're in the right directory
if [ ! -f "infrastructure/bin/usecase-account-app.ts" ]; then
    printf "${RED}Error: Must run from edge-cv-portal directory${NC}\n"
    echo "Current directory: $(pwd)"
    echo "Looking for: infrastructure/bin/usecase-account-app.ts"
    exit 1
fi

# Check AWS CLI is installed
if ! command -v aws &> /dev/null; then
    printf "${RED}========================================${NC}\n"
    printf "${RED}ERROR: AWS CLI not found${NC}\n"
    printf "${RED}========================================${NC}\n"
    echo ""
    echo "The AWS CLI is required but not installed."
    echo ""
    echo "Install it from: https://aws.amazon.com/cli/"
    echo ""
    exit 1
fi

# Get current account ID
printf "${BLUE}Checking AWS credentials...${NC}\n"
AWS_OUTPUT=$(aws sts get-caller-identity --query Account --output text 2>&1)
AWS_EXIT_CODE=$?

if [ $AWS_EXIT_CODE -ne 0 ]; then
    printf "${RED}========================================${NC}\n"
    printf "${RED}ERROR: AWS credentials not configured${NC}\n"
    printf "${RED}========================================${NC}\n"
    echo ""
    echo "Could not get AWS account information."
    echo ""
    echo "AWS CLI error:"
    echo "  $AWS_OUTPUT"
    echo ""
    echo "Please configure AWS credentials using one of:"
    echo "  1. aws configure"
    echo "  2. aws sso login"
    echo "  3. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables"
    echo "  4. Use an IAM role (if running on EC2/ECS)"
    echo ""
    exit 1
fi

# Trim any whitespace and validate
CURRENT_ACCOUNT=$(echo "$AWS_OUTPUT" | tr -d '[:space:]')

if [ -z "$CURRENT_ACCOUNT" ] || ! [[ "$CURRENT_ACCOUNT" =~ ^[0-9]{12}$ ]]; then
    printf "${RED}========================================${NC}\n"
    printf "${RED}ERROR: Invalid AWS account response${NC}\n"
    printf "${RED}========================================${NC}\n"
    echo ""
    echo "Received unexpected response from AWS:"
    echo "  '$AWS_OUTPUT'"
    echo ""
    echo "Expected a 12-digit AWS account ID."
    echo "Please check your AWS credentials and try again."
    echo ""
    exit 1
fi

printf "Current AWS Account: ${GREEN}$CURRENT_ACCOUNT${NC}\n"
echo ""

# Ask for role type
printf "${BLUE}What type of role do you want to deploy?${NC}\n"
echo ""
echo "  1) UseCase Account Role"
echo "     - Full access for SageMaker training, compilation, Greengrass, IoT"
echo "     - Required for each UseCase account"
echo ""
echo "  2) Data Account Role (Optional)"
echo "     - S3 access only for data storage"
echo "     - Use when storing training data in a separate account"
echo ""
read -p "Enter choice [1-2]: " ROLE_TYPE

if [ "$ROLE_TYPE" = "1" ]; then
    ROLE_NAME="UseCase Account"
    STACK_NAME="DDAPortalUseCaseAccountStack"
    CONFIG_FILE="usecase-account-config.txt"
elif [ "$ROLE_TYPE" = "2" ]; then
    ROLE_NAME="Data Account"
    STACK_NAME="DDAPortalDataAccountStack"
    CONFIG_FILE="data-account-config.txt"
else
    printf "${RED}Invalid choice${NC}\n"
    exit 1
fi

echo ""
printf "${YELLOW}Deploying: $ROLE_NAME Role${NC}\n"
echo ""

# Ask for Portal Account ID (required - no default)
printf "${BLUE}Enter the Portal Account ID${NC}\n"
echo "(This is the AWS account where the DDA Portal is deployed)"
echo ""
read -p "Portal Account ID: " PORTAL_ACCOUNT_ID

if [ -z "$PORTAL_ACCOUNT_ID" ]; then
    printf "${RED}Error: Portal Account ID is required${NC}\n"
    exit 1
fi

# Validate it looks like an AWS account ID (12 digits)
if ! [[ "$PORTAL_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
    printf "${RED}Error: Invalid AWS Account ID format (must be 12 digits)${NC}\n"
    exit 1
fi

# For Data Account, also ask for UseCase Account ID (for SageMaker cross-account access)
USECASE_ACCOUNT_ID=""
if [ "$ROLE_TYPE" = "2" ]; then
    echo ""
    printf "${BLUE}Enter the UseCase Account ID${NC}\n"
    echo "(This is the AWS account where SageMaker training jobs will run)"
    echo "(SageMaker needs to read training data from this Data Account)"
    echo ""
    read -p "UseCase Account ID: " USECASE_ACCOUNT_ID

    if [ -z "$USECASE_ACCOUNT_ID" ]; then
        printf "${RED}Error: UseCase Account ID is required for Data Account setup${NC}\n"
        exit 1
    fi

    # Validate it looks like an AWS account ID (12 digits)
    if ! [[ "$USECASE_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
        printf "${RED}Error: Invalid AWS Account ID format (must be 12 digits)${NC}\n"
        exit 1
    fi
fi

echo ""
printf "${YELLOW}Configuration:${NC}\n"
printf "  Portal Account:     ${GREEN}$PORTAL_ACCOUNT_ID${NC}\n"
if [ "$ROLE_TYPE" = "2" ]; then
    printf "  UseCase Account:    ${GREEN}$USECASE_ACCOUNT_ID${NC} (SageMaker will run here)\n"
fi
printf "  Target Account:     ${GREEN}$CURRENT_ACCOUNT${NC} (where this role will be deployed)\n"
echo ""

# Determine if same-account or cross-account for display purposes
if [ "$PORTAL_ACCOUNT_ID" = "$CURRENT_ACCOUNT" ]; then
    printf "${YELLOW}Note: Same-account deployment (Portal and $ROLE_NAME in same account)${NC}\n"
else
    printf "${YELLOW}Note: Cross-account deployment${NC}\n"
fi

# Generate External ID
if command -v uuidgen &> /dev/null; then
    EXTERNAL_ID=$(uuidgen)
else
    # Fallback if uuidgen not available
    EXTERNAL_ID=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "$(date +%s)-$$")
fi

echo ""
printf "${YELLOW}Generated External ID: ${GREEN}$EXTERNAL_ID${NC}\n"
printf "${RED}⚠️  IMPORTANT: Save this External ID securely!${NC}\n"
echo ""

read -p "Press Enter to continue with deployment..."

echo ""
printf "${YELLOW}Deploying $ROLE_NAME role...${NC}\n"
echo ""

cd infrastructure

if [ "$ROLE_TYPE" = "1" ]; then
    # Deploy UseCase Account Stack (full permissions)
    cdk deploy -a "npx ts-node bin/usecase-account-app.ts" \
        -c portalAccountId=$PORTAL_ACCOUNT_ID \
        -c externalId=$EXTERNAL_ID \
        --require-approval never
else
    # Deploy Data Account Stack (S3 only permissions)
    cdk deploy -a "npx ts-node bin/usecase-account-app.ts" \
        -c portalAccountId=$PORTAL_ACCOUNT_ID \
        -c externalId=$EXTERNAL_ID \
        -c stackName=$STACK_NAME \
        -c dataAccountOnly=true \
        --require-approval never
fi

cd ..

echo ""
printf "${GREEN}✓ Deployment complete!${NC}\n"
echo ""

# Get outputs
ROLE_ARN=$(aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --query 'Stacks[0].Outputs[?OutputKey==`RoleArn`].OutputValue' \
    --output text 2>/dev/null || echo "")

SAGEMAKER_ROLE_ARN=""
if [ "$ROLE_TYPE" = "1" ]; then
    SAGEMAKER_ROLE_ARN=$(aws cloudformation describe-stacks \
        --stack-name $STACK_NAME \
        --query 'Stacks[0].Outputs[?OutputKey==`SageMakerExecutionRoleArn`].OutputValue' \
        --output text 2>/dev/null || echo "")
fi

printf "${BLUE}========================================${NC}\n"
printf "${BLUE}Use these values in the Portal:${NC}\n"
printf "${BLUE}========================================${NC}\n"
echo ""
printf "  Account ID:   ${GREEN}$CURRENT_ACCOUNT${NC}\n"
printf "  External ID:  ${GREEN}$EXTERNAL_ID${NC}\n"

if [ -n "$ROLE_ARN" ]; then
    printf "  Role ARN:     ${GREEN}$ROLE_ARN${NC}\n"
fi

if [ -n "$SAGEMAKER_ROLE_ARN" ]; then
    printf "  SageMaker Role ARN: ${GREEN}$SAGEMAKER_ROLE_ARN${NC}\n"
fi

echo ""

# Save to file
if [ "$ROLE_TYPE" = "1" ]; then
    cat > $CONFIG_FILE << EOF
UseCase Account Configuration
=============================
Account ID: $CURRENT_ACCOUNT
Portal Account ID: $PORTAL_ACCOUNT_ID
External ID: $EXTERNAL_ID
Role ARN: $ROLE_ARN
SageMaker Execution Role ARN: $SAGEMAKER_ROLE_ARN
Deployment Date: $(date)

Use in Portal Onboarding:
- AWS Account ID: $CURRENT_ACCOUNT
- Role ARN: $ROLE_ARN
- SageMaker Execution Role ARN: $SAGEMAKER_ROLE_ARN
- External ID: $EXTERNAL_ID
EOF
else
    cat > $CONFIG_FILE << EOF
Data Account Configuration
==========================
Data Account ID: $CURRENT_ACCOUNT
Portal Account ID: $PORTAL_ACCOUNT_ID
UseCase Account ID: $USECASE_ACCOUNT_ID
External ID: $EXTERNAL_ID
Role ARN: $ROLE_ARN
Deployment Date: $(date)

Use in Portal Onboarding (Advanced: Separate Data Account):
- Data Account ID: $CURRENT_ACCOUNT
- Data Account Role ARN: $ROLE_ARN
- Data Account External ID: $EXTERNAL_ID

Cross-Account Access:
- SageMaker in UseCase Account ($USECASE_ACCOUNT_ID) needs bucket policy to read from Data Account
- Run this script again and provide bucket name to set up bucket policy
EOF
fi

printf "${GREEN}Configuration saved to: $CONFIG_FILE${NC}\n"
echo ""

# S3 bucket tagging
printf "${YELLOW}⚠️  IMPORTANT: Tag your S3 buckets for portal access${NC}\n"
echo ""
echo "The portal uses tag-based access control. Tag each S3 bucket you want"
echo "the portal to access with the following command:"
echo ""
printf "${GREEN}aws s3api put-bucket-tagging --bucket YOUR_BUCKET_NAME --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]'${NC}\n"
echo ""
read -p "Enter S3 bucket name to tag now (or press Enter to skip): " BUCKET_NAME

if [ -n "$BUCKET_NAME" ]; then
    echo ""
    printf "${YELLOW}Tagging bucket: $BUCKET_NAME${NC}\n"
    if aws s3api put-bucket-tagging --bucket "$BUCKET_NAME" --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]' 2>/dev/null; then
        printf "${GREEN}✓ Bucket tagged successfully!${NC}\n"
    else
        printf "${RED}✗ Failed to tag bucket. You may need to tag it manually.${NC}\n"
    fi
    
    # Configure CORS for portal access (required for browser uploads)
    echo ""
    printf "${YELLOW}Configuring CORS for portal access...${NC}\n"
    
    # Ask for CloudFront domain
    read -p "Enter your Portal CloudFront domain (e.g., d3qeryypza4i9i.cloudfront.net): " CLOUDFRONT_DOMAIN
    
    if [ -n "$CLOUDFRONT_DOMAIN" ]; then
        # Create CORS configuration
        CORS_CONFIG=$(cat <<CORS
{
    "CORSRules": [
        {
            "AllowedHeaders": ["*"],
            "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
            "AllowedOrigins": ["https://${CLOUDFRONT_DOMAIN}"],
            "ExposeHeaders": ["ETag"],
            "MaxAgeSeconds": 3000
        }
    ]
}
CORS
)
        
        # Apply CORS configuration
        if printf '%s' "$CORS_CONFIG" | aws s3api put-bucket-cors --bucket "$BUCKET_NAME" --cors-configuration file:///dev/stdin 2>/dev/null; then
            printf "${GREEN}✓ CORS configured successfully!${NC}\n"
            printf "  Allowed origin: https://${CLOUDFRONT_DOMAIN}\n"
        else
            printf "${RED}✗ Failed to configure CORS. You may need to configure it manually.${NC}\n"
            echo ""
            echo "Manual CORS configuration:"
            echo "aws s3api put-bucket-cors --bucket $BUCKET_NAME --cors-configuration '<cors-json>'"
        fi
    else
        printf "${YELLOW}Skipped CORS configuration. You'll need to configure it manually for browser uploads.${NC}\n"
        echo ""
        echo "To configure CORS later, run:"
        echo "aws s3api put-bucket-cors --bucket $BUCKET_NAME --cors-configuration '{\"CORSRules\":[{\"AllowedHeaders\":[\"*\"],\"AllowedMethods\":[\"GET\",\"PUT\",\"POST\",\"HEAD\"],\"AllowedOrigins\":[\"https://YOUR_CLOUDFRONT_DOMAIN\"],\"ExposeHeaders\":[\"ETag\"],\"MaxAgeSeconds\":3000}]}'"
    fi
    
    # For Data Account, also set up bucket policy for cross-account SageMaker access
    if [ "$ROLE_TYPE" = "2" ] && [ -n "$USECASE_ACCOUNT_ID" ]; then
        echo ""
        printf "${YELLOW}Setting up cross-account bucket policy for SageMaker access...${NC}\n"
        echo ""
        
        # Create bucket policy JSON
        BUCKET_POLICY=$(cat <<POLICY
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowSageMakerFromUseCaseAccount",
            "Effect": "Allow",
            "Principal": {
                "AWS": "arn:aws:iam::${USECASE_ACCOUNT_ID}:root"
            },
            "Action": [
                "s3:GetObject",
                "s3:GetObjectVersion",
                "s3:ListBucket",
                "s3:GetBucketLocation"
            ],
            "Resource": [
                "arn:aws:s3:::${BUCKET_NAME}",
                "arn:aws:s3:::${BUCKET_NAME}/*"
            ],
            "Condition": {
                "StringLike": {
                    "aws:PrincipalArn": [
                        "arn:aws:iam::${USECASE_ACCOUNT_ID}:role/DDAPortalSageMakerExecutionRole",
                        "arn:aws:iam::${USECASE_ACCOUNT_ID}:role/*SageMaker*"
                    ]
                }
            }
        }
    ]
}
POLICY
)
        
        echo "Bucket policy to be applied:"
        echo "$BUCKET_POLICY" | head -20
        echo "..."
        echo ""
        read -p "Apply this bucket policy? [y/N]: " APPLY_POLICY
        
        if [ "$APPLY_POLICY" = "y" ] || [ "$APPLY_POLICY" = "Y" ]; then
            if aws s3api put-bucket-policy --bucket "$BUCKET_NAME" --policy "$BUCKET_POLICY" 2>/dev/null; then
                printf "${GREEN}✓ Bucket policy applied successfully!${NC}\n"
                printf "${GREEN}  SageMaker in UseCase Account ($USECASE_ACCOUNT_ID) can now read from this bucket.${NC}\n"
            else
                printf "${RED}✗ Failed to apply bucket policy.${NC}\n"
                echo ""
                echo "You can apply it manually with:"
                echo "aws s3api put-bucket-policy --bucket $BUCKET_NAME --policy '<policy-json>'"
            fi
        else
            printf "${YELLOW}Skipped bucket policy. You'll need to configure cross-account access manually.${NC}\n"
        fi
    fi
fi

echo ""
printf "${GREEN}========================================${NC}\n"
printf "${GREEN}$ROLE_NAME Setup Complete!${NC}\n"
printf "${GREEN}========================================${NC}\n"
echo ""

if [ "$ROLE_TYPE" = "1" ]; then
    echo "Next steps:"
    echo "  1. Go to the Portal and create a new UseCase"
    echo "  2. Enter the values shown above in the onboarding wizard"
    echo "  3. Tag any S3 buckets you want the portal to access"
else
    echo "Next steps:"
    echo "  1. Go to the Portal UseCase onboarding"
    echo "  2. In 'Configure S3 Storage', expand 'Advanced: Use Separate Data Account'"
    echo "  3. Enter the values shown above"
    echo "  4. Tag any S3 buckets in this account for portal access"
fi
echo ""
