#!/bin/bash
# =============================================================================
# DDA Edge Device Launcher
# Launches an EC2 instance in the UseCase account and sets up Greengrass
# =============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default values
REGION="us-east-1"
INSTANCE_TYPE_ARM="m6g.xlarge"
INSTANCE_TYPE_X86="m5.xlarge"
THING_NAME=""
ARCHITECTURE="arm64"  # arm64 or x86_64
KEY_NAME=""
SECURITY_GROUP=""
SUBNET_ID=""
GITHUB_REPO=""
IAM_PROFILE="dda-edge-device-role"  # Default IAM instance profile
SSH_CIDR=""  # SSH CIDR must be explicitly specified (no default open access)

# AMI IDs (Ubuntu)
AMI_ARM64_US_EAST_1="ami-0c13dec58913b948c"  # Ubuntu 18.04 ARM64
AMI_X86_US_EAST_1="ami-0c7217cdde317cfec"    # Ubuntu 22.04 x86_64

# EBS Volume Configuration
EBS_VOLUME_SIZE=30  # GB - enough for Docker images and Greengrass artifacts
EBS_VOLUME_TYPE="gp3"

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Launch an EC2 instance configured as a DDA edge device with Greengrass"
    echo ""
    echo "Required Options:"
    echo "  -n, --thing-name NAME     IoT Thing name for the device (e.g., dda-edge-1)"
    echo "  -k, --key-name NAME       EC2 key pair name for SSH access"
    echo "  -c, --cidr CIDR           CIDR block for all access (SSH, 3000, 5000, 3443) (REQUIRED)"
    echo "                            Use 'auto' to auto-detect your current IP"
    echo "                            Or specify CIDR (e.g., 10.0.0.0/8, 203.0.113.0/24)"
    echo ""
    echo "Optional Options:"
    echo "  -a, --arch ARCH           Architecture: arm64 (default) or x86_64"
    echo "  -r, --region REGION       AWS region (default: us-east-1)"
    echo "  -s, --security-group SG   Security group ID (creates new if not specified)"
    echo "  -u, --subnet SUBNET       Subnet ID (uses default VPC if not specified)"
    echo "  -t, --instance-type TYPE  Override instance type"
    echo "  -v, --volume-size SIZE    EBS volume size in GB (default: 30)"
    echo "  -i, --iam-profile PROFILE IAM instance profile name (optional, for EC2-based Greengrass)"
    echo "  -g, --github-repo URL     GitHub repo URL for setup files"
    echo "  -h, --help                Show this help message"
    echo ""
    echo "Examples:"
    echo "  # Launch with auto-detected current IP (all ports restricted)"
    echo "  $0 -n dda-edge-1 -k my-key-pair -c auto"
    echo ""
    echo "  # Launch with specific CIDR"
    echo "  $0 -n dda-edge-1 -k my-key-pair -c 203.0.113.0/24"
    echo ""
    echo "  # Launch with specific IP"
    echo "  $0 -n dda-edge-1 -k my-key-pair -c 203.0.113.45/32"
    echo ""
    echo "  # Launch x86_64 device"
    echo "  $0 -n dda-x86-edge-1 -k my-key-pair -c auto -a x86_64"
    exit 1
}

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -n|--thing-name)
            THING_NAME="$2"
            shift 2
            ;;
        -k|--key-name)
            KEY_NAME="$2"
            shift 2
            ;;
        -a|--arch)
            ARCHITECTURE="$2"
            shift 2
            ;;
        -r|--region)
            REGION="$2"
            shift 2
            ;;
        -s|--security-group)
            SECURITY_GROUP="$2"
            shift 2
            ;;
        -u|--subnet)
            SUBNET_ID="$2"
            shift 2
            ;;
        -t|--instance-type)
            INSTANCE_TYPE_OVERRIDE="$2"
            shift 2
            ;;
        -v|--volume-size)
            EBS_VOLUME_SIZE="$2"
            shift 2
            ;;
        -i|--iam-profile)
            IAM_PROFILE="$2"
            shift 2
            ;;
        -c|--cidr)
            SSH_CIDR="$2"
            shift 2
            ;;
        -g|--github-repo)
            GITHUB_REPO="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            log_error "Unknown option: $1"
            usage
            ;;
    esac
done

# Validate required parameters
if [ -z "$THING_NAME" ]; then
    log_error "Thing name is required (-n or --thing-name)"
    usage
fi

if [ -z "$KEY_NAME" ]; then
    log_error "Key name is required (-k or --key-name)"
    usage
fi

if [ -z "$SSH_CIDR" ]; then
    log_error "SSH CIDR is required (-c or --cidr)"
    log_error ""
    log_error "Access must be explicitly restricted for security."
    log_error "Use one of:"
    log_error "  -c auto                    # Auto-detect your current IP"
    log_error "  -c 203.0.113.0/24          # Specify a CIDR block"
    log_error "  -c 203.0.113.45/32         # Specify a single IP"
    usage
fi

# Handle CIDR configuration
if [ "$SSH_CIDR" == "auto" ]; then
    # Try to get current IP
    CURRENT_IP=$(curl -s https://checkip.amazonaws.com 2>/dev/null || echo "")
    if [ -n "$CURRENT_IP" ]; then
        SSH_CIDR="${CURRENT_IP}/32"
        log_info "Auto-detected current IP for all access: $SSH_CIDR"
    else
        log_error "Could not auto-detect current IP"
        log_error "Specify CIDR manually with -c option"
        exit 1
    fi
fi

# Validate CIDR format (basic validation)
if ! [[ "$SSH_CIDR" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/[0-9]{1,2}$ ]]; then
    log_error "Invalid CIDR format: $SSH_CIDR"
    log_error "Expected format: 10.0.0.0/8 or 203.0.113.0/24"
    exit 1
fi

# Set architecture-specific values
if [ "$ARCHITECTURE" == "arm64" ]; then
    AMI_ID="$AMI_ARM64_US_EAST_1"
    INSTANCE_TYPE="${INSTANCE_TYPE_OVERRIDE:-$INSTANCE_TYPE_ARM}"
    log_info "Using ARM64 architecture"
elif [ "$ARCHITECTURE" == "x86_64" ]; then
    AMI_ID="$AMI_X86_US_EAST_1"
    INSTANCE_TYPE="${INSTANCE_TYPE_OVERRIDE:-$INSTANCE_TYPE_X86}"
    log_info "Using x86_64 architecture"
else
    log_error "Invalid architecture: $ARCHITECTURE. Use 'arm64' or 'x86_64'"
    exit 1
fi

log_info "Configuration:"
log_info "  Region: $REGION"
log_info "  Thing Name: $THING_NAME"
log_info "  Architecture: $ARCHITECTURE"
log_info "  Instance Type: $INSTANCE_TYPE"
log_info "  AMI: $AMI_ID"
log_info "  Key Pair: $KEY_NAME"
log_info "  Access CIDR (all ports): $SSH_CIDR"
log_info "  EBS Volume: ${EBS_VOLUME_SIZE}GB ($EBS_VOLUME_TYPE)"
log_info "  IAM Profile: $IAM_PROFILE"

# Check AWS CLI is configured
if ! aws sts get-caller-identity &>/dev/null; then
    log_error "AWS CLI not configured or no valid credentials"
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
log_info "AWS Account: $ACCOUNT_ID"

# Validate IAM instance profile if specified
if [ -n "$IAM_PROFILE" ]; then
    log_info "Validating IAM instance profile: $IAM_PROFILE"
    if ! aws iam get-instance-profile --instance-profile-name "$IAM_PROFILE" &>/dev/null; then
        log_error "WARNING: IAM instance profile '$IAM_PROFILE' not found"
        log_error "Device will launch without IAM role (suitable for non-EC2 edge devices)"
        IAM_PROFILE=""
    else
        log_info "IAM instance profile validated: $IAM_PROFILE"
    fi
else
    log_info "No IAM profile specified - device will launch without AWS IAM role"
fi

# Create or use security group
if [ -z "$SECURITY_GROUP" ]; then
    SG_NAME="dda-edge-device-sg"
    log_info "Checking for existing security group: $SG_NAME"
    
    SECURITY_GROUP=$(aws ec2 describe-security-groups \
        --filters "Name=group-name,Values=$SG_NAME" \
        --query 'SecurityGroups[0].GroupId' \
        --output text \
        --region "$REGION" 2>/dev/null || echo "None")
    
    if [ "$SECURITY_GROUP" == "None" ] || [ -z "$SECURITY_GROUP" ]; then
        log_info "Creating security group: $SG_NAME"
        SECURITY_GROUP=$(aws ec2 create-security-group \
            --group-name "$SG_NAME" \
            --description "Security group for DDA edge devices" \
            --region "$REGION" \
            --query 'GroupId' \
            --output text)
        
        # Allow SSH with restricted CIDR
        aws ec2 authorize-security-group-ingress \
            --group-id "$SECURITY_GROUP" \
            --protocol tcp \
            --port 22 \
            --cidr "$SSH_CIDR" \
            --region "$REGION"
        
        # Allow DDA Frontend (React UI on port 3000)
        aws ec2 authorize-security-group-ingress \
            --group-id "$SECURITY_GROUP" \
            --protocol tcp \
            --port 3000 \
            --cidr "$SSH_CIDR" \
            --region "$REGION"
        
        # Allow DDA Frontend HTTPS (port 3443)
        aws ec2 authorize-security-group-ingress \
            --group-id "$SECURITY_GROUP" \
            --protocol tcp \
            --port 3443 \
            --cidr "$SSH_CIDR" \
            --region "$REGION"
        
        # Allow DDA Backend API (Flask on port 5000)
        aws ec2 authorize-security-group-ingress \
            --group-id "$SECURITY_GROUP" \
            --protocol tcp \
            --port 5000 \
            --cidr "$SSH_CIDR" \
            --region "$REGION"
        
        # Allow HTTPS outbound (for Greengrass)
        aws ec2 authorize-security-group-egress \
            --group-id "$SECURITY_GROUP" \
            --protocol tcp \
            --port 443 \
            --cidr 0.0.0.0/0 \
            --region "$REGION" 2>/dev/null || true
        
        log_info "Created security group: $SECURITY_GROUP"
    else
        log_info "Using existing security group: $SECURITY_GROUP"
    fi
fi

# Get script directory for user data
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Create user data script
USER_DATA=$(cat <<EOF
#!/bin/bash
set -e

# Log everything
exec > >(tee /var/log/dda-setup.log) 2>&1
echo "Starting DDA edge device setup at \$(date)"

# Update system
apt-get update
apt-get install -y git curl unzip

# Create setup directory
mkdir -p /tmp/dda-setup
cd /tmp/dda-setup

# Download setup files from S3 or GitHub
# Option 1: If using GitHub
if [ -n "$GITHUB_REPO" ]; then
    git clone "$GITHUB_REPO" repo
    cp -r repo/station_install/* .
else
    # Default to official repo
    git clone https://github.com/awslabs/DefectDetectionApplication repo
    cp -r repo/station_install/* .
fi

# Option 2: Create setup files inline (fallback)
if [ ! -f setup_station.sh ]; then
    echo "Downloading setup script..."
    # Create edge_manager_agent_config.json
    cat > edge_manager_agent_config.json << 'EMCONFIG'
{"sagemaker_edge_core_capture_data_disk_path": "", "sagemaker_edge_core_device_fleet_name": "", "sagemaker_edge_core_capture_data_buffer_size": "", "sagemaker_edge_core_device_name": "", "sagemaker_edge_provider_provider": "", "sagemaker_edge_core_capture_data_batch_size": "", "sagemaker_edge_local_data_root_path": "", "sagemaker_edge_core_folder_prefix": "", "sagemaker_edge_core_region": "", "sagemaker_edge_core_capture_data_destination": "", "sagemaker_edge_provider_provider_path": "", "sagemaker_edge_provider_s3_bucket_name": "", "sagemaker_edge_log_verbose": "", "sagemaker_edge_core_root_certs_path": ""}
EMCONFIG

    # Signal that manual setup is needed
    echo "MANUAL_SETUP_REQUIRED" > /tmp/dda-setup-status
    echo "Setup files not found. Please SSH in and run setup_station.sh manually."
    exit 0
fi

# Run setup script
chmod +x setup_station.sh
./setup_station.sh $REGION $THING_NAME

echo "DDA edge device setup completed at \$(date)"
echo "SETUP_COMPLETE" > /tmp/dda-setup-status
EOF
)

# Build launch command
LAUNCH_CMD="aws ec2 run-instances \
    --image-id $AMI_ID \
    --instance-type $INSTANCE_TYPE \
    --key-name $KEY_NAME \
    --security-group-ids $SECURITY_GROUP \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=$THING_NAME},{Key=dda-portal:managed,Value=true},{Key=Architecture,Value=$ARCHITECTURE}]' \
    --user-data \"$USER_DATA\" \
    --region $REGION"

if [ -n "$SUBNET_ID" ]; then
    LAUNCH_CMD="$LAUNCH_CMD --subnet-id $SUBNET_ID"
fi

log_info "Launching EC2 instance..."

# Build launch command with optional IAM profile
RUN_CMD="aws ec2 run-instances \
    --image-id \"$AMI_ID\" \
    --instance-type \"$INSTANCE_TYPE\" \
    --key-name \"$KEY_NAME\" \
    --security-group-ids \"$SECURITY_GROUP\" \
    --block-device-mappings \"DeviceName=/dev/sda1,Ebs={VolumeSize=$EBS_VOLUME_SIZE,VolumeType=$EBS_VOLUME_TYPE,DeleteOnTermination=true}\" \
    --tag-specifications \"ResourceType=instance,Tags=[{Key=Name,Value=$THING_NAME},{Key=dda-portal:managed,Value=true},{Key=Architecture,Value=$ARCHITECTURE}]\" \
    --metadata-options 'HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled' \
    --region \"$REGION\" \
    --query 'Instances[0].InstanceId' \
    --output text"

# Add IAM profile if specified
if [ -n "$IAM_PROFILE" ]; then
    RUN_CMD="$RUN_CMD --iam-instance-profile Name=\"$IAM_PROFILE\""
fi

INSTANCE_ID=$(eval $RUN_CMD)

if [ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" == "None" ]; then
    log_error "Failed to launch EC2 instance"
    exit 1
fi

if [ -n "$SUBNET_ID" ]; then
    log_warn "Note: Subnet ID was specified but instance was launched in default VPC. To use a specific subnet, modify the launch command."
fi

log_info "Instance launched: $INSTANCE_ID"
log_info "Waiting for instance to be running..."

# Wait for instance to be running
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"

# Get public IP
PUBLIC_IP=$(aws ec2 describe-instances \
    --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' \
    --output text \
    --region "$REGION")

log_info "Instance is running!"
echo ""
echo "=============================================="
echo -e "${GREEN}Edge Device Launched Successfully!${NC}"
echo "=============================================="
echo ""
echo "Instance ID:    $INSTANCE_ID"
echo "Public IP:      $PUBLIC_IP"
echo "Thing Name:     $THING_NAME"
echo "Architecture:   $ARCHITECTURE"
echo "Access CIDR:    $SSH_CIDR"
echo ""
echo "SSH Command:"
echo "  ssh -i ~/.ssh/${KEY_NAME}.pem ubuntu@${PUBLIC_IP}"
echo ""
echo "Access URLs:"
echo "  Frontend:  https://${PUBLIC_IP}:3443"
echo "  API:       http://${PUBLIC_IP}:5000"
echo ""
echo "Next Steps:"
echo "  1. Wait 2-3 minutes for instance to initialize"
echo "  2. SSH into the instance"
echo "  3. Run the setup script manually:"
echo ""
echo "     cd /tmp"
echo "     git clone <your-repo-url> dda"
echo "     cd dda/station_install"
echo "     sudo ./setup_station.sh $REGION $THING_NAME"
echo ""
echo "  4. After setup, the device will appear in the portal"
echo ""
echo "To check setup logs (after SSH):"
echo "  cat /var/log/dda-setup.log"
echo ""
echo "To terminate this instance:"
echo "  aws ec2 terminate-instances --instance-ids $INSTANCE_ID --region $REGION"
echo ""
