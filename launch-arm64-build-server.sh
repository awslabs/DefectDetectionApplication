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
#   --ami-id AMI             AMI ID (default: Ubuntu Pro 18.04 ARM64)
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

# Find Ubuntu Pro 18.04 ARM64 AMI if not specified
if [ -z "$AMI_ID" ]; then
    echo "Finding latest Ubuntu Pro 18.04 ARM64 AMI..."
    AMI_ID=$(aws ec2 describe-images \
        --region "$REGION" \
        --owners 099720109477 \
        --filters \
            "Name=name,Values=ubuntu-pro-server/images/hvm-ssd/ubuntu-bionic-18.04-arm64-pro-server-*" \
            "Name=architecture,Values=arm64" \
            "Name=state,Values=available" \
        --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
        --output text 2>/dev/null || echo "")
    
    if [ -z "$AMI_ID" ] || [ "$AMI_ID" == "None" ]; then
        # Fallback to standard Ubuntu 18.04 ARM64
        echo "Ubuntu Pro not found, trying standard Ubuntu 18.04 ARM64..."
        AMI_ID=$(aws ec2 describe-images \
            --region "$REGION" \
            --owners 099720109477 \
            --filters \
                "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-arm64-server-*" \
                "Name=architecture,Values=arm64" \
                "Name=state,Values=available" \
            --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
            --output text 2>/dev/null || echo "")
    fi
    
    if [ -z "$AMI_ID" ] || [ "$AMI_ID" == "None" ]; then
        echo "Error: Could not find Ubuntu 18.04 ARM64 AMI"
        echo "Please specify --ami-id manually"
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
echo "  Region:           $REGION"
echo ""

if [ "$DRY_RUN" = true ]; then
    echo "[DRY-RUN] Would launch instance with above configuration"
    exit 0
fi

# Build the run-instances command
RUN_CMD="aws ec2 run-instances \
    --region $REGION \
    --image-id $AMI_ID \
    --instance-type $INSTANCE_TYPE \
    --key-name $KEY_NAME \
    --security-group-ids $SECURITY_GROUP_ID \
    --iam-instance-profile Name=$IAM_PROFILE \
    --block-device-mappings DeviceName=/dev/sda1,Ebs={VolumeSize=$VOLUME_SIZE,VolumeType=gp3,DeleteOnTermination=true} \
    --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled \
    --tag-specifications ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME}] \
    --ebs-optimized \
    --query 'Instances[0].InstanceId' \
    --output text"

# Add subnet if specified
if [ -n "$SUBNET_ID" ]; then
    RUN_CMD="$RUN_CMD --subnet-id $SUBNET_ID"
fi

echo "Launching instance..."
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
echo "  git clone <your-repo>"
echo "  cd DefectDetectionApplication"
echo "  ./setup-build-server.sh"
echo ""
echo "Then build the ARM64 component:"
echo "  ./gdk-component-build-and-publish.sh"
echo ""
