#!/bin/bash
# create-edge-device-iam-role.sh - Create IAM role for DDA edge devices
#
# This script creates an IAM role with the necessary permissions for:
# - AWS Greengrass Core operations
# - IoT operations
# - S3 bucket access for component artifacts and inference uploads
# - CloudWatch logging
# - ECR access for container images
#
# Usage:
#   ./create-edge-device-iam-role.sh [OPTIONS]
#
# Options:
#   --role-name NAME         IAM role name (default: dda-edge-device-role)
#   --profile-name NAME      Instance profile name (default: dda-edge-device-role)
#   --region REGION          AWS region (default: us-east-1)
#   --dry-run                Show what would be created without creating
#   --help                   Show this help message

set -e

# Default values
ROLE_NAME="dda-edge-device-role"
PROFILE_NAME="dda-edge-device-role"
REGION="us-east-1"
DRY_RUN=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --role-name)
            ROLE_NAME="$2"
            shift 2
            ;;
        --profile-name)
            PROFILE_NAME="$2"
            shift 2
            ;;
        --region)
            REGION="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help)
            head -30 "$0" | tail -28
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "=============================================="
echo "DDA Edge Device IAM Role Creator"
echo "=============================================="
echo ""
echo "Configuration:"
echo "  Role Name:       $ROLE_NAME"
echo "  Profile Name:    $PROFILE_NAME"
echo "  Region:          $REGION"
echo ""

if [ "$DRY_RUN" = true ]; then
    echo "[DRY-RUN] Would create:"
    echo "  - IAM Role: $ROLE_NAME"
    echo "  - Instance Profile: $PROFILE_NAME"
    echo "  - Attach inline policy with Greengrass, IoT, S3, CloudWatch, and ECR permissions"
    exit 0
fi

# Create trust policy document
TRUST_POLICY=$(cat <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "ec2.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
)

# Create the IAM role
echo "Creating IAM role: $ROLE_NAME"
if aws iam get-role --role-name "$ROLE_NAME" &>/dev/null; then
    echo "Role already exists: $ROLE_NAME"
else
    aws iam create-role \
        --role-name "$ROLE_NAME" \
        --assume-role-policy-document "$TRUST_POLICY" \
        --description "Role for DDA edge devices with Greengrass and IoT permissions"
    echo "Created role: $ROLE_NAME"
fi

# Create inline policy with Greengrass, IoT, S3, CloudWatch, and ECR permissions
INLINE_POLICY=$(cat <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GreengrassPermissions",
      "Effect": "Allow",
      "Action": [
        "greengrass:*",
        "greengrassv2:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IoTPermissions",
      "Effect": "Allow",
      "Action": [
        "iot:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3Permissions",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:GetBucketVersioning",
        "s3:ListBucketVersions"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogsPermissions",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    },
    {
      "Sid": "CloudWatchMetricsPermissions",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:PutMetricData"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ECRPermissions",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "*"
    },
    {
      "Sid": "STSPermissions",
      "Effect": "Allow",
      "Action": [
        "sts:AssumeRole"
      ],
      "Resource": "arn:aws:iam::*:role/GreengrassV2TokenExchangeRole*"
    }
  ]
}
EOF
)

echo "Attaching inline policy with Greengrass, IoT, S3, CloudWatch, and ECR permissions..."
aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "dda-edge-device-policy" \
    --policy-document "$INLINE_POLICY"

# Create instance profile
echo "Creating instance profile: $PROFILE_NAME"
if aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" &>/dev/null; then
    echo "Instance profile already exists: $PROFILE_NAME"
else
    aws iam create-instance-profile \
        --instance-profile-name "$PROFILE_NAME"
    
    # Add role to instance profile
    aws iam add-role-to-instance-profile \
        --instance-profile-name "$PROFILE_NAME" \
        --role-name "$ROLE_NAME"
    
    echo "Created instance profile: $PROFILE_NAME"
fi

echo ""
echo "=============================================="
echo "IAM Role Setup Complete!"
echo "=============================================="
echo ""
echo "Role ARN: arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/$ROLE_NAME"
echo ""
echo "You can now launch edge devices with:"
echo "  ./launch-edge-device.sh --thing-name YOUR_DEVICE_NAME --key-name YOUR_KEY_NAME --iam-profile $PROFILE_NAME"
echo ""
echo "Permissions granted:"
echo "  ✓ AWS Greengrass Core operations"
echo "  ✓ AWS IoT operations"
echo "  ✓ S3 access for component artifacts and inference uploads"
echo "  ✓ CloudWatch logging and metrics"
echo "  ✓ ECR authentication for container images"
echo "  ✓ STS assume role for Greengrass Token Exchange"
echo ""
