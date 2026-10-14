#!/bin/bash
# launch-arm64-build-server.sh - Launch an ARM64 build server for DDA component builds
#
# This script creates an EC2 instance similar to the existing ARM64 build server
# with configurable parameters for enterprise environments.
#
# Usage:
#   ./launch-arm64-build-server.sh [OPTIONS]
#
# Options:
#   --name NAME              Instance name tag (default: dda-arm64-build-server)
#   --instance-type TYPE     EC2 instance type (default: m6g.4xlarge)
#   --key-name KEY           SSH key pair name (REQUIRED)
#   --security-group-id SG   Security group ID (default: creates new one)
#   --subnet-id SUBNET       Subnet ID (default: uses default VPC)
#   --iam-profile PROFILE    IAM instance profile name (default: dda-build-role)
#   --volume-size SIZE       Root volume size in GB (default: 100)
#   --region REGION          AWS region (default: us-east-1)
#   --ami-id AMI             AMI ID (default: latest Ubuntu ARM64 AMI for --ubuntu-version)
#   --ubuntu-version VER     Ubuntu LTS version for the default AMI lookup:
#                            18.04 (default), 20.04, 22.04, or 24.04
#                            (use 24.04 for JP7 build servers; see README "JetPack 7
#                            (JP7) Build Server" section for provisioning steps)
#   --flavor FLAVOR          Ubuntu flavor: pro (Ubuntu Pro) or standard
#                            (default: standard)
#   --dry-run                Show what would be created without creating
#   --help                   Show this help message

set -e

# Default values (based on existing ARM64 build server i-05b71d1570d477769)
INSTANCE_NAME="dda-arm64-build-server"
INSTANCE_TYPE="m6g.4xlarge"
KEY_NAME=""
SECURITY_GROUP_ID=""
SUBNET_ID=""
IAM_PROFILE="dda-build-role"
VOLUME_SIZE=100
REGION="us-east-1"
AMI_ID=""
UBUNTU_VERSION="18.04"
UBUNTU_FLAVOR="standard"
DRY_RUN=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --name)
            INSTANCE_NAME="$2"
            shift 2
            ;;
        --instance-type)
            INSTANCE_TYPE="$2"
            shift 2
            ;;
        --key-name)
            KEY_NAME="$2"
            shift 2
            ;;
        --security-group-id)
            SECURITY_GROUP_ID="$2"
            shift 2
            ;;
        --subnet-id)
            SUBNET_ID="$2"
            shift 2
            ;;
        --iam-profile)
            IAM_PROFILE="$2"
            shift 2
            ;;
        --volume-size)
            VOLUME_SIZE="$2"
            shift 2
            ;;
        --region)
            REGION="$2"
            shift 2
            ;;
        --ami-id)
            AMI_ID="$2"
            shift 2
            ;;
        --ubuntu-version)
            UBUNTU_VERSION="$2"
            shift 2
            ;;
        --flavor)
            UBUNTU_FLAVOR="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help)
            head -27 "$0" | tail -26
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Validate the Ubuntu flavor immediately after argument parsing, before any
# AWS API call (ubuntu-pro-build-servers Req 5.5): only the exact values
# 'pro' and 'standard' are accepted — no silent fallback in either direction.
if [ "$UBUNTU_FLAVOR" != "pro" ] && [ "$UBUNTU_FLAVOR" != "standard" ]; then
    echo "Error: Unsupported --flavor '$UBUNTU_FLAVOR' (supported values: pro, standard)"
    exit 1
fi

# Validate required parameters
if [ -z "$KEY_NAME" ]; then
    echo "Error: --key-name is required"
    echo "Usage: $0 --key-name YOUR_KEY_NAME [OPTIONS]"
    echo "Run '$0 --help' for more options"
    exit 1
fi

echo "=============================================="
echo "DDA ARM64 Build Server Launcher"
echo "=============================================="
echo ""

# Create IAM role and instance profile if they don't exist
echo "Setting up IAM role and instance profile..."

# Create IAM role
if ! aws iam get-role --role-name "$IAM_PROFILE" &>/dev/null; then
    echo "Creating IAM role: $IAM_PROFILE"
    aws iam create-role \
        --role-name "$IAM_PROFILE" \
        --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        }'
    
    # Attach AWS managed policy for SSM access
    aws iam attach-role-policy \
        --role-name "$IAM_PROFILE" \
        --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
    
    # Attach inline policy for build permissions.
    # Least-privilege note: IoT and S3 actions are scoped by resource where the
    # AWS IAM reference supports it (thing/dda-*, job/*, dda-component-* and
    # dda-inference-results-* buckets). Two actions cannot be scoped by resource
    # and are therefore isolated into their own statements on "Resource": "*":
    #   - IoTEndpointDiscovery  (iot:DescribeEndpoint)  -- unscopable per IAM ref
    #   - S3ListAllBuckets      (s3:ListAllMyBuckets)   -- unscopable per IAM ref
    aws iam put-role-policy \
        --role-name "$IAM_PROFILE" \
        --policy-name DDABuildPolicy \
        --policy-document '{
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "GreengrassPermissions",
                    "Effect": "Allow",
                    "Action": [
                        "greengrass:CreateComponentVersion",
                        "greengrass:DescribeComponent",
                        "greengrass:GetComponent",
                        "greengrass:ListComponents",
                        "greengrass:ListComponentVersions",
                        "greengrass:ListCoreDevices",
                        "greengrass:GetCoreDevice",
                        "greengrass:ListInstalledComponents",
                        "greengrass:ListTagsForResource",
                        "greengrass:TagResource",
                        "greengrass:ListDeployments",
                        "greengrass:GetDeployment",
                        "greengrass:CreateDeployment",
                        "greengrass:CancelDeployment"
                    ],
                    "Resource": "*"
                },
                {
                    "Sid": "IoTThingPermissions",
                    "Effect": "Allow",
                    "Action": [
                        "iot:DescribeThing",
                        "iot:CreateThing",
                        "iot:UpdateThingShadow",
                        "iot:AttachPolicy"
                    ],
                    "Resource": "arn:aws:iot:*:*:thing/dda-*"
                },
                {
                    "Sid": "IoTJobPermissions",
                    "Effect": "Allow",
                    "Action": ["iot:DescribeJob"],
                    "Resource": "arn:aws:iot:*:*:job/*"
                },
                {
                    "Sid": "IoTEndpointDiscovery",
                    "Effect": "Allow",
                    "Action": ["iot:DescribeEndpoint"],
                    "Resource": "*"
                },
                {
                    "Sid": "S3Permissions",
                    "Effect": "Allow",
                    "Action": [
                        "s3:CreateBucket",
                        "s3:GetBucketLocation",
                        "s3:PutBucketVersioning",
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:ListBucket",
                        "s3:DeleteObject",
                        "s3:GetBucketVersioning",
                        "s3:ListBucketVersions",
                        "s3:GetBucketPolicy",
                        "s3:PutBucketPolicy",
                        "s3:GetBucketAcl",
                        "s3:PutBucketAcl",
                        "s3:GetBucketTagging",
                        "s3:PutBucketTagging"
                    ],
                    "Resource": [
                        "arn:aws:s3:::dda-component-*",
                        "arn:aws:s3:::dda-component-*/*",
                        "arn:aws:s3:::dda-inference-results-*",
                        "arn:aws:s3:::dda-inference-results-*/*"
                    ]
                },
                {
                    "Sid": "S3ListAllBuckets",
                    "Effect": "Allow",
                    "Action": ["s3:ListAllMyBuckets"],
                    "Resource": "*"
                },
                {
                    "Sid": "EC2Permissions",
                    "Effect": "Allow",
                    "Action": [
                        "ec2:DescribeInstances",
                        "ec2:DescribeImages",
                        "ec2:DescribeSecurityGroups",
                        "ec2:DescribeSubnets",
                        "ec2:DescribeVpcs",
                        "ec2:DescribeKeyPairs",
                        "ec2:DescribeTags"
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
                    "Action": ["cloudwatch:PutMetricData"],
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
                }
            ]
        }'
    
    echo "IAM role created: $IAM_PROFILE"
else
    echo "IAM role already exists: $IAM_PROFILE"
fi

# Create instance profile
if ! aws iam get-instance-profile --instance-profile-name "$IAM_PROFILE" &>/dev/null; then
    echo "Creating instance profile: $IAM_PROFILE"
    aws iam create-instance-profile --instance-profile-name "$IAM_PROFILE"
    
    # Add role to instance profile
    aws iam add-role-to-instance-profile \
        --instance-profile-name "$IAM_PROFILE" \
        --role-name "$IAM_PROFILE"
    
    # Wait for instance profile to be ready (with retry logic)
    echo "Waiting for instance profile to propagate..."
    for i in {1..30}; do
        if aws iam get-instance-profile --instance-profile-name "$IAM_PROFILE" &>/dev/null; then
            echo "Instance profile ready"
            break
        fi
        if [ $i -eq 30 ]; then
            echo "Warning: Instance profile may not be fully propagated yet, continuing anyway..."
        fi
        sleep 1
    done
    
    # Extra wait for IAM eventual consistency - EC2 needs time to see the new profile
    echo "Waiting 30 seconds for IAM propagation to EC2..."
    sleep 30
    
    echo "Instance profile created: $IAM_PROFILE"
else
    echo "Instance profile already exists: $IAM_PROFILE"
fi

echo ""

# Find the Ubuntu ARM64 AMI for the requested release if not specified.
# 18.04 (bionic) remains the default for the existing JP4/JP5/JP6 flow;
# --ubuntu-version 24.04 (noble) provisions a JetPack 7 (JP7) build server
# (see the README "JetPack 7 (JP7) Build Server" section).
if [ -z "$AMI_ID" ]; then
    case "$UBUNTU_VERSION" in
        18.04) UBUNTU_CODENAME="bionic"; UBUNTU_SSD_PATH="hvm-ssd" ;;
        20.04) UBUNTU_CODENAME="focal";  UBUNTU_SSD_PATH="hvm-ssd" ;;
        22.04) UBUNTU_CODENAME="jammy";  UBUNTU_SSD_PATH="hvm-ssd" ;;
        24.04) UBUNTU_CODENAME="noble";  UBUNTU_SSD_PATH="hvm-ssd-gp3" ;;
        *)
            echo "Error: Unsupported --ubuntu-version '$UBUNTU_VERSION' (supported: 18.04, 20.04, 22.04, 24.04)"
            echo "Alternatively, specify --ami-id manually"
            exit 1
            ;;
    esac

    # Single flavor-selected DescribeImages query (ubuntu-pro-build-servers
    # Req 5.2, 5.3, 5.4): the selected flavor's name pattern is the only one
    # queried — a failed lookup exits nonzero without ever querying the
    # other flavor (no silent fallback in either direction).
    if [ "$UBUNTU_FLAVOR" = "pro" ]; then
        NAME_FILTER="ubuntu-pro-server/images/${UBUNTU_SSD_PATH}/ubuntu-${UBUNTU_CODENAME}-${UBUNTU_VERSION}-arm64-pro-server-*"
    else
        NAME_FILTER="ubuntu/images/${UBUNTU_SSD_PATH}/ubuntu-${UBUNTU_CODENAME}-${UBUNTU_VERSION}-arm64-server-*"
    fi

    echo "Finding latest Ubuntu ${UBUNTU_FLAVOR} ${UBUNTU_VERSION} ARM64 AMI..."
    AMI_ID=$(aws ec2 describe-images \
        --region "$REGION" \
        --owners 099720109477 \
        --filters \
            "Name=name,Values=${NAME_FILTER}" \
            "Name=architecture,Values=arm64" \
            "Name=state,Values=available" \
        --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
        --output text 2>/dev/null || echo "")

    if [ -z "$AMI_ID" ] || [ "$AMI_ID" == "None" ]; then
        echo "Error: Could not find an Ubuntu ${UBUNTU_FLAVOR} ${UBUNTU_VERSION} ARM64 AMI"
        echo "Specify --ami-id manually, or check the flavor/release combination"
        exit 1
    fi
fi

echo "AMI ID: $AMI_ID"

# Create security group if not specified
if [ -z "$SECURITY_GROUP_ID" ]; then
    echo "Creating security group for build server..."
    
    # Get default VPC
    VPC_ID=$(aws ec2 describe-vpcs \
        --region "$REGION" \
        --filters "Name=isDefault,Values=true" \
        --query 'Vpcs[0].VpcId' \
        --output text)
    
    if [ -z "$VPC_ID" ] || [ "$VPC_ID" == "None" ]; then
        echo "Error: No default VPC found. Please specify --security-group-id"
        exit 1
    fi
    
    SG_NAME="dda-build-server-sg-$(date +%Y%m%d%H%M%S)"
    
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY-RUN] Would create security group: $SG_NAME"
        SECURITY_GROUP_ID="sg-dryrun"
    else
        SECURITY_GROUP_ID=$(aws ec2 create-security-group \
            --region "$REGION" \
            --group-name "$SG_NAME" \
            --description "Security group for DDA ARM64 build server" \
            --vpc-id "$VPC_ID" \
            --query 'GroupId' \
            --output text)
        
        # Add SSH access (restrict to your IP in production)
        aws ec2 authorize-security-group-ingress \
            --region "$REGION" \
            --group-id "$SECURITY_GROUP_ID" \
            --protocol tcp \
            --port 22 \
            --cidr 0.0.0.0/0
        
        echo "Created security group: $SECURITY_GROUP_ID"
        echo "WARNING: SSH is open to 0.0.0.0/0. Restrict this in production!"
    fi
fi

echo ""
echo "Configuration:"
echo "  Instance Name:    $INSTANCE_NAME"
echo "  Instance Type:    $INSTANCE_TYPE"
echo "  AMI ID:           $AMI_ID"
echo "  Key Pair:         $KEY_NAME"
echo "  Security Group:   $SECURITY_GROUP_ID"
echo "  Subnet:           ${SUBNET_ID:-default}"
echo "  IAM Profile:      $IAM_PROFILE"
echo "  Volume Size:      ${VOLUME_SIZE}GB"
echo "  Ubuntu Version:   ${UBUNTU_VERSION}"
echo "  Ubuntu Flavor:    ${UBUNTU_FLAVOR}"
echo "  Region:           $REGION"
echo ""

if [ "$DRY_RUN" = true ]; then
    echo "[DRY-RUN] Would launch instance with above configuration"
    exit 0
fi

# Build the run-instances command with proper JSON formatting
echo "Launching instance..."

# Build command with optional IAM profile
RUN_CMD="aws ec2 run-instances \
    --region $REGION \
    --image-id $AMI_ID \
    --instance-type $INSTANCE_TYPE \
    --key-name $KEY_NAME \
    --security-group-ids $SECURITY_GROUP_ID \
    --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=$VOLUME_SIZE,VolumeType=gp3,DeleteOnTermination=true}' \
    --metadata-options 'HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled' \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME}]' \
    --ebs-optimized"

# Add IAM profile if specified and exists
if [ -n "$IAM_PROFILE" ]; then
    RUN_CMD="$RUN_CMD --iam-instance-profile Name=$IAM_PROFILE"
fi

# Add subnet if specified
if [ -n "$SUBNET_ID" ]; then
    RUN_CMD="$RUN_CMD --subnet-id $SUBNET_ID"
fi

RUN_CMD="$RUN_CMD --query 'Instances[0].InstanceId' --output text"

INSTANCE_ID=$(eval $RUN_CMD)

echo "Instance launched: $INSTANCE_ID"
echo ""
echo "Waiting for instance to be running..."
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

# Get public IP
PUBLIC_IP=$(aws ec2 describe-instances \
    --region "$REGION" \
    --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' \
    --output text)

echo ""
echo "=============================================="
echo "Instance Ready!"
echo "=============================================="
echo ""
echo "Instance ID:  $INSTANCE_ID"
echo "Public IP:    $PUBLIC_IP"
echo ""
echo "Connect with:"
echo "  ssh -i ~/.ssh/${KEY_NAME}.pem ubuntu@$PUBLIC_IP"
echo ""
echo "After connecting, set up the build environment:"
echo "  git clone https://github.com/awslabs/DefectDetectionApplication"
echo "  cd DefectDetectionApplication"
echo "  ./setup-build-server.sh"
echo ""
if [ "$UBUNTU_VERSION" = "24.04" ]; then
    echo "Ubuntu 24.04 (JP7 build server): follow the README section"
    echo "'JetPack 7 (JP7) Build Server (Ubuntu 24.04 arm64)' for the noble-specific"
    echo "prerequisites (Docker Engine + buildx and the docker-compose command shim),"
    echo "then build the JP7 component:"
    echo "  ./gdk-component-build-and-publish.sh aarch64 7"
else
    echo "Then build the ARM64 component:"
    echo "  ./gdk-component-build-and-publish.sh"
fi
echo ""
