#!/bin/bash
# NOTE: Removed -e flag to allow proper error handling for Python 3.9 build
# We use explicit error checking instead

VERBOSE="${VERBOSE:-0}"
LOG_FILE="/tmp/setup-station-$(date +%s).log"
ERRORS=()
WARNINGS=()

# Get the Ubuntu release version
UBUNTU_VERSION=$(lsb_release -rs)

# Helper function to run commands with logging
run_cmd() {
    local cmd="$@"
    if [ "$VERBOSE" = "1" ]; then
        echo "[RUN] $cmd"
        eval "$cmd" | tee -a "$LOG_FILE"
    else
        echo "[RUN] $cmd"
        if ! eval "$cmd" >> "$LOG_FILE" 2>&1; then
            return 1
        fi
    fi
}

# Helper to add errors
add_error() {
    ERRORS+=("$1")
    echo "❌ $1" | tee -a "$LOG_FILE"
}

# Helper to add warnings
add_warning() {
    WARNINGS+=("$1")
    echo "⚠️  $1" | tee -a "$LOG_FILE"
}

# Check if command exists
check_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        return 1
    fi
    return 0
}

# Check version requirement
check_version() {
    local cmd="$1"
    local min_version="$2"
    local current_version
    
    current_version=$($cmd --version 2>&1 | head -n1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
    
    if [ -z "$current_version" ]; then
        return 1
    fi
    
    # Simple version comparison (major.minor)
    if [ "$(printf '%s\n' "$min_version" "$current_version" | sort -V | head -n1)" = "$min_version" ]; then
        return 0
    fi
    return 1
}

SETUP_STARTED=0

# Trap errors and show summary
trap 'show_summary' EXIT

show_summary() {
    # Only show summary if setup actually started
    if [ $SETUP_STARTED -eq 0 ]; then
        return
    fi
    
    echo ""
    echo "=========================================="
    if [ ${#ERRORS[@]} -eq 0 ] && [ ${#WARNINGS[@]} -eq 0 ]; then
        echo "✅ Setup completed successfully!"
    elif [ ${#ERRORS[@]} -eq 0 ]; then
        echo "✅ Setup completed with warnings"
    else
        echo "❌ Setup completed with ERRORS"
    fi
    echo "=========================================="
    
    if [ ${#ERRORS[@]} -gt 0 ]; then
        echo ""
        echo "ERRORS ENCOUNTERED:"
        printf '%s\n' "${ERRORS[@]}"
    fi
    
    if [ ${#WARNINGS[@]} -gt 0 ]; then
        echo ""
        echo "WARNINGS:"
        printf '%s\n' "${WARNINGS[@]}"
    fi
    
    echo ""
    echo "📋 Full log: $LOG_FILE"
    if [ "$VERBOSE" != "1" ] && [ ${#ERRORS[@]} -gt 0 ]; then
        echo "Run with VERBOSE=1 to see detailed output:"
        echo "  VERBOSE=1 $0 $@"
    fi
    echo ""
    
    if [ ${#ERRORS[@]} -gt 0 ]; then
        return 1
    fi
}

# Pre-flight checks
check_prerequisites() {
    echo "▶ Checking prerequisites..."
    
    # Check if running as root
    if [ "$EUID" -ne 0 ]; then
        add_error "This script must be run as root (use sudo)"
        return 1
    fi
    
    # Check Ubuntu version
    if ! command -v lsb_release >/dev/null 2>&1; then
        add_error "lsb_release not found - cannot determine Ubuntu version"
        return 1
    fi
    
    # Check internet connectivity
    if ! ping -c 1 8.8.8.8 >/dev/null 2>&1; then
        add_warning "No internet connectivity detected - some installations may fail"
    fi
    
    # Check disk space (need at least 2GB free)
    local free_space=$(df / | awk 'NR==2 {print $4}')
    if [ "$free_space" -lt 2097152 ]; then  # 2GB in KB
        add_error "Insufficient disk space (need at least 2GB free, have $(( free_space / 1024 / 1024 ))GB)"
        return 1
    fi
    
    echo "✓ Prerequisites check passed"
}

# Check mandatory dependencies
check_mandatory_deps() {
    echo ""
    echo "▶ Checking mandatory dependencies..."
    
    # Update package manager first
    if ! run_cmd "apt-get update"; then
        add_warning "Failed to update package manager"
    fi
    
    # Java
    if ! check_command java; then
        echo "Installing Java..."
        if ! run_cmd "apt-get install -y default-jdk"; then
            add_error "Failed to install Java"
            return 1
        else
            echo "✓ Java installed"
        fi
    else
        echo "✓ Java found"
    fi
    
    # curl
    if ! check_command curl; then
        echo "Installing curl..."
        if ! run_cmd "apt-get install -y curl"; then
            add_error "Failed to install curl"
            return 1
        else
            echo "✓ curl installed"
        fi
    else
        echo "✓ curl found"
    fi
    
    # unzip
    if ! check_command unzip; then
        echo "Installing unzip..."
        if ! run_cmd "apt-get install -y unzip"; then
            add_error "Failed to install unzip"
            return 1
        else
            echo "✓ unzip installed"
        fi
    else
        echo "✓ unzip found"
    fi
    
    # AWS CLI
    if ! check_command aws; then
        add_warning "AWS CLI not found - will attempt to install"
    else
        echo "✓ AWS CLI found"
    fi
    
    # Python 3.9
    if ! check_command python3.9 && ! check_command /usr/local/bin/python3.9; then
        add_warning "Python 3.9 not found - will attempt to install"
    else
        echo "✓ Python 3.9 found"
    fi
}

echo "=========================================="
echo "DDA Edge Device Setup"
echo "=========================================="
echo "Log file: $LOG_FILE"
echo ""

# Run prerequisite checks
if ! check_prerequisites; then
    exit 1
fi

# Check mandatory dependencies
check_mandatory_deps

# Function to install from source for Ubuntu 18.04
install_from_source() {
  # Check for python3.9 specifically
  if command -v python3.9 >/dev/null 2>&1; then
    echo "Python 3.9 already installed, skipping build"
    return 0
  fi
  
  # Also check /usr/local/bin/python3.9 (where altinstall puts it)
  if [ -x /usr/local/bin/python3.9 ]; then
    echo "Python 3.9 already installed at /usr/local/bin/python3.9, skipping build"
    return 0
  fi
  
  echo "Ubuntu version is 18.04. Installing Python 3.9 from source."
  echo "This will take approximately 10-15 minutes on ARM64..."

  # Install build dependencies
  echo "Installing build dependencies..."
  if ! run_cmd "apt update"; then
    add_error "Failed to update package manager"
    return 1
  fi
  
  if ! run_cmd "apt install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev libssl-dev libreadline-dev libffi-dev wget"; then
    add_error "Failed to install build dependencies"
    return 1
  fi

  # Save current directory
  local current_dir=$(pwd)
  
  # Create temp directory for build
  local build_dir="/tmp/python39_build"
  mkdir -p "$build_dir"
  cd "$build_dir"

  # Download Python 3.9 source code
  if [ ! -f "Python-3.9.18.tgz" ]; then
    echo "Downloading Python 3.9.18..."
    if ! run_cmd "wget https://www.python.org/ftp/python/3.9.18/Python-3.9.18.tgz"; then
      add_error "Failed to download Python 3.9.18"
      cd "$current_dir"
      return 1
    fi
  fi
  
  # Extract if not already extracted
  if [ ! -d "Python-3.9.18" ]; then
    echo "Extracting Python source..."
    if ! run_cmd "tar -xf Python-3.9.18.tgz"; then
      add_error "Failed to extract Python source"
      cd "$current_dir"
      return 1
    fi
  fi
  
  cd Python-3.9.18

  # Configure, compile, and install
  echo "Configuring Python build..."
  if ! run_cmd "./configure --enable-optimizations"; then
    add_error "Python configure failed"
    cd "$current_dir"
    return 1
  fi
  
  echo "Compiling Python (this takes ~10-15 minutes on ARM64)..."
  if ! run_cmd "make -j $(nproc)"; then
    add_error "Python compilation failed"
    cd "$current_dir"
    return 1
  fi
  
  echo "Installing Python..."
  if ! run_cmd "make altinstall"; then
    add_error "Python installation failed"
    cd "$current_dir"
    return 1
  fi

  # Return to original directory
  cd "$current_dir"
  
  # Verify installation
  if [ -x /usr/local/bin/python3.9 ]; then
    echo "✓ Python 3.9 installed successfully from source."
    /usr/local/bin/python3.9 --version
  else
    add_error "Python 3.9 installation failed - binary not found!"
    return 1
  fi
}

# Function to install from deadsnakes PPA
install_from_ppa() {
  echo "Ubuntu version is not 18.04. Installing Python 3.9 from the deadsnakes PPA."

  # Add the deadsnakes PPA
  if ! run_cmd "apt update"; then
    add_error "Failed to update package manager"
    return 1
  fi
  
  if ! run_cmd "apt install -y software-properties-common"; then
    add_error "Failed to install software-properties-common"
    return 1
  fi
  
  if ! run_cmd "add-apt-repository -y ppa:deadsnakes/ppa"; then
    add_error "Failed to add deadsnakes PPA"
    return 1
  fi

  # Install Python 3.9
  if ! run_cmd "apt update"; then
    add_error "Failed to update package manager after adding PPA"
    return 1
  fi
  
  if ! run_cmd "apt install -y python3.9"; then
    add_error "Failed to install Python 3.9 from PPA"
    return 1
  fi
  
  run_cmd "apt install python3.9-venv -y" || add_warning "Failed to install python3.9-venv"

  echo "✓ Python 3.9 installed successfully from the deadsnakes PPA."
}

# Check if region parameter is provided
if [ $# -eq 0 ]; then
    echo "Usage: $0 <aws-region> <thing_name>"
    echo "Example: $0 us-east-1 dda_thing_1"
    exit 1
fi

SETUP_STARTED=1

aws_region="$1"
thing_name="$2"
echo "Using AWS region: $aws_region"
echo "Using thing name: $thing_name"
echo ""

dda_root_folder="/aws_dda"
architecture=$(uname -m)
dda_greengrass_root_folder="${dda_root_folder}/greengrass/v2"
dda_image_capture_dir="${dda_root_folder}/image-capture"
dda_inference_result_dir="${dda_root_folder}/inference-results"
greengrass_version="2.12.0"

function isUserExists(){
 if id "$1" >/dev/null 2>&1 ; then
 # user exists
 return 0
 fi
 return 1
}

function isGroupExists(){
 if [ $(getent group "$1") ]; then
 # group exists
 return 0
 fi
 return 1
}

echo "▶ Setting up system users and groups..."

# DDA admin user/group
if ! isGroupExists dda_system_group; then
 run_cmd "groupadd dda_system_group" || add_warning "Failed to create dda_system_group"
fi

if ! isUserExists dda_system_user; then
 run_cmd "useradd dda_system_user -g dda_system_group" || add_warning "Failed to create dda_system_user"
 run_cmd "usermod -aG video dda_system_user" || add_warning "Failed to add dda_system_user to video group"
fi

# DDA customer user/group
if ! isGroupExists dda_admin_group; then
 run_cmd "groupadd dda_admin_group" || add_warning "Failed to create dda_admin_group"
fi

if ! isUserExists dda_admin_user; then
 run_cmd "useradd dda_admin_user -g dda_admin_group" || add_warning "Failed to create dda_admin_user"
fi

# Default device user
default_user=$(awk -F":" '/1000/ {print $1}' /etc/passwd)
run_cmd "usermod -aG dda_admin_group dda_system_user" || add_warning "Failed to add dda_system_user to dda_admin_group"
run_cmd "usermod -aG dda_admin_group ${default_user}" || add_warning "Failed to add default user to dda_admin_group"

echo "✓ Users and groups configured"
echo ""

echo "▶ Setting up DDA directories..."

# Setup DDA root folder
mkdir -p "${dda_root_folder}"
run_cmd "chgrp dda_system_group ${dda_root_folder}" || add_warning "Failed to set group on dda_root_folder"
run_cmd "chown dda_system_user ${dda_root_folder}" || add_warning "Failed to set owner on dda_root_folder"

# Setup DDA GGv2 folder
mkdir -p "${dda_greengrass_root_folder}"
run_cmd "chmod 755 ${dda_greengrass_root_folder}" || add_warning "Failed to set permissions on greengrass folder"

# Setup DDA image capture folder
mkdir -p "${dda_image_capture_dir}"
run_cmd "chgrp -R dda_admin_group ${dda_image_capture_dir}" || add_warning "Failed to set group on image capture folder"
run_cmd "chown -R dda_admin_user ${dda_image_capture_dir}" || add_warning "Failed to set owner on image capture folder"

# Setup DDA inference results folder
mkdir -p "${dda_inference_result_dir}"
run_cmd "chgrp -R dda_admin_group ${dda_inference_result_dir}" || add_warning "Failed to set group on inference results folder"
run_cmd "chown -R dda_admin_user ${dda_inference_result_dir}" || add_warning "Failed to set owner on inference results folder"

echo "✓ DDA directories configured"
echo ""

echo "▶ Installing additional system packages..."

if ! run_cmd "apt-get update"; then
    add_warning "Failed to update package manager"
fi

if ! run_cmd "apt-get install ca-certificates gnupg lsb-release zip -y"; then
    add_warning "Failed to install additional system packages"
fi

echo "✓ Additional system packages installed"
echo ""

echo "▶ Installing AWS CLI..."
if check_command aws; then
    echo "✓ AWS CLI already installed"
else
    if ! run_cmd "curl https://awscli.amazonaws.com/awscli-exe-linux-${architecture}.zip -o awscliv2.zip"; then
        add_error "Failed to download AWS CLI"
    elif ! run_cmd "unzip awscliv2.zip"; then
        add_error "Failed to extract AWS CLI"
    elif ! run_cmd "./aws/install"; then
        add_error "Failed to install AWS CLI"
    else
        echo "✓ AWS CLI installed successfully"
    fi
fi
echo ""

echo "▶ Installing Python 3.9..."
if [ "$UBUNTU_VERSION" = "18.04" ]; then
  echo "Detected Ubuntu 18.04 - building Python 3.9 from source..."
  if ! install_from_source; then
    add_error "Python 3.9 installation from source failed"
  fi
  
  # Set up alternatives for python3.9
  if [ -x /usr/local/bin/python3.9 ]; then
    run_cmd "update-alternatives --install /usr/local/bin/python3 python3 /usr/local/bin/python3.9 1" || add_warning "Failed to set python3 alternative"
    ln -sf /usr/local/bin/python3.9 /usr/bin/python3.9 2>/dev/null || add_warning "Failed to create python3.9 symlink"
  fi
else
  if ! install_from_ppa; then
    add_error "Python 3.9 installation from PPA failed"
  fi
  run_cmd "update-alternatives --install /usr/local/bin/python3 python3 /usr/bin/python3.9 1" || add_warning "Failed to set python3 alternative"
fi

if ! run_cmd "apt-get install python3-pip -y"; then
    add_error "Failed to install pip"
else
    echo "✓ pip installed"
fi

# Find python3.9 location
PYTHON39=""
if [ -x /usr/local/bin/python3.9 ]; then
  PYTHON39="/usr/local/bin/python3.9"
elif [ -x /usr/bin/python3.9 ]; then
  PYTHON39="/usr/bin/python3.9"
fi

if [ -n "$PYTHON39" ]; then
  echo "Using Python at: $PYTHON39"
  run_cmd "$PYTHON39 -m pip install --upgrade pip" || add_warning "Failed to upgrade pip"
  run_cmd "$PYTHON39 -m pip install --force-reinstall requests==2.32.3" || add_warning "Failed to install requests"
  run_cmd "$PYTHON39 -m pip install protobuf" || add_warning "Failed to install protobuf"
else
  add_warning "python3.9 not found. Using system python3 instead."
  run_cmd "python3 -m pip install --upgrade pip" || add_warning "Failed to upgrade pip"
  run_cmd "python3 -m pip install requests protobuf" || add_warning "Failed to install Python packages"
fi
echo ""

echo "▶ Installing GStreamer..."
if ! run_cmd "apt-get install -y libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-libav gstreamer1.0-tools gstreamer1.0-x gstreamer1.0-alsa gstreamer1.0-gl gstreamer1.0-gtk3 gstreamer1.0-qt5 gstreamer1.0-pulseaudio"; then
    add_error "Failed to install GStreamer"
else
    echo "✓ GStreamer installed"
fi
echo ""

echo "▶ Setting up Edge Manager Agent..."
mkdir -p "${dda_greengrass_root_folder}/em_agent/capture_data" \
 "${dda_greengrass_root_folder}/em_agent/local_data" \
 "${dda_greengrass_root_folder}/em_agent/config"

if [ -f "./edge_manager_agent_config.json" ]; then
    run_cmd "cp ./edge_manager_agent_config.json ${dda_greengrass_root_folder}/em_agent/config" || add_warning "Failed to copy Edge Manager Agent config"
    echo "✓ Edge Manager Agent config copied"
else
    add_warning "edge_manager_agent_config.json not found in current directory"
fi
echo ""

echo "▶ Installing Docker..."
if check_command docker; then
    echo "✓ Docker already installed"
else
    if ! run_cmd "mkdir -m 0755 -p /etc/apt/keyrings"; then
        add_warning "Failed to create keyrings directory"
    fi
    
    if ! run_cmd "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg"; then
        add_error "Failed to download Docker GPG key"
    elif ! run_cmd "echo 'deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable' | tee /etc/apt/sources.list.d/docker.list > /dev/null"; then
        add_error "Failed to add Docker repository"
    elif ! run_cmd "apt-get update"; then
        add_error "Failed to update package manager after adding Docker repo"
    elif ! run_cmd "apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin -y"; then
        add_error "Failed to install Docker packages"
    elif ! run_cmd "docker run hello-world"; then
        add_warning "Docker installed but hello-world test failed"
    else
        echo "✓ Docker installed successfully"
    fi
fi
echo ""

echo "▶ Installing Greengrass Core..."
if ! run_cmd "curl -s 'https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-${greengrass_version}.zip' > 'greengrass-${greengrass_version}.zip'"; then
    add_error "Failed to download Greengrass"
elif ! run_cmd "unzip greengrass-${greengrass_version}.zip -d GreengrassInstaller"; then
    add_error "Failed to extract Greengrass"
else
    run_cmd "rm greengrass-${greengrass_version}.zip" || add_warning "Failed to clean up Greengrass zip"
    
    if ! run_cmd "java -jar ./GreengrassInstaller/lib/Greengrass.jar --version"; then
        add_warning "Failed to verify Greengrass installation"
    else
        echo "✓ Greengrass Core downloaded and extracted"
    fi
fi
echo ""

echo "▶ Provisioning Greengrass Core Device..."
if ! run_cmd "java -Droot=/aws_dda/greengrass/v2 -Dlog.store=FILE -jar ./GreengrassInstaller/lib/Greengrass.jar --aws-region ${aws_region} --thing-name ${thing_name} --thing-group-name DDA_transition_EC2_Group --thing-policy-name GreengrassV2IoTThingPolicy --tes-role-name GreengrassV2TokenExchangeRole --tes-role-alias-name GreengrassCoreTokenExchangeRoleAlias --component-default-user ggc_user:ggc_group --setup-system-service true --provision true"; then
    add_error "Greengrass provisioning failed"
else
    echo "✓ Greengrass Core provisioned successfully"
fi
echo ""

echo "▶ Configuring Greengrass permissions..."
run_cmd "usermod -aG video ggc_user" || add_warning "Failed to add ggc_user to video group"
run_cmd "usermod -aG dda_system_group ggc_user" || add_warning "Failed to add ggc_user to dda_system_group"
run_cmd "usermod -aG ggc_group dda_system_user" || add_warning "Failed to add dda_system_user to ggc_group"

echo "▶ Copying certificates..."
run_cmd "cp ${dda_greengrass_root_folder}/thingCert.crt ${dda_greengrass_root_folder}/device.pem.crt" || add_warning "Failed to copy thing certificate"
run_cmd "cp ${dda_greengrass_root_folder}/privKey.key ${dda_greengrass_root_folder}/private.pem.key" || add_warning "Failed to copy private key"
run_cmd "cp ${dda_greengrass_root_folder}/rootCA.pem ${dda_greengrass_root_folder}/AmazonRootCA1.pem" || add_warning "Failed to copy root CA"

echo "▶ Tagging Greengrass Core Device..."
aws_account_id=$(aws sts get-caller-identity --query 'Account' --output text 2>/dev/null || echo "")
if [ -n "$aws_account_id" ]; then
    gg_core_arn="arn:aws:greengrass:${aws_region}:${aws_account_id}:coreDevices:${thing_name}"
    echo "Waiting for Greengrass Core Device to be registered..."
    sleep 10
    
    if run_cmd "aws greengrassv2 tag-resource --resource-arn $gg_core_arn --tags dda-portal:managed=true --region ${aws_region}"; then
        echo "✓ Greengrass Core Device tagged successfully"
    else
        add_warning "Could not tag Greengrass Core Device. Tag manually if needed."
    fi
else
    add_warning "Could not get AWS account ID for tagging"
fi
echo ""

echo "▶ Managing directory permissions..."
if [ -d $dda_root_folder ] ; then
    run_cmd "chown dda_system_user:dda_system_group $dda_root_folder" || add_warning "Failed to set ownership on dda_root_folder"
    run_cmd "chmod 775 $dda_root_folder" || add_warning "Failed to set permissions on dda_root_folder"
fi

dda_greengrass_dir="${dda_root_folder}/greengrass"
for directory in `find ${dda_root_folder}/ -maxdepth 1 -mindepth 1 -type d`
do
    if [ $directory != $dda_greengrass_dir ] ; then
        run_cmd "chown -R dda_admin_user:dda_admin_group $directory" || add_warning "Failed to set ownership on $directory"
        run_cmd "chmod -R 770 $directory" || add_warning "Failed to set permissions on $directory"
    fi
done

echo "✓ Directory permissions configured"
echo ""

echo "▶ Setting up CloudWatch Logs diagnostics..."
# Create a diagnostic script for troubleshooting CloudWatch logging
DIAG_SCRIPT="${dda_root_folder}/check-cloudwatch-logging.sh"
cat > "$DIAG_SCRIPT" << 'DIAG_EOF'
#!/bin/bash
# CloudWatch Logging Diagnostics Script
# Run this to check if device can upload logs to CloudWatch

set -e

AWS_REGION="${1:-us-east-1}"
DEVICE_NAME="${2:-}"

echo "🔍 CloudWatch Logging Diagnostics"
echo "=================================="
echo "Region: $AWS_REGION"
echo ""

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

check_mark() {
    echo -e "${GREEN}✅${NC} $1"
}

cross_mark() {
    echo -e "${RED}❌${NC} $1"
}

warning() {
    echo -e "${YELLOW}⚠️${NC} $1"
}

# Test 1: LogManager running
echo "Test 1: LogManager Component Status"
echo "-----------------------------------"
if ps aux | grep -i logmanager | grep -v grep > /dev/null; then
    check_mark "LogManager is running"
else
    cross_mark "LogManager is not running"
    warning "Check deployment includes aws.greengrass.LogManager component"
fi
echo ""

# Test 2: Local logs exist
echo "Test 2: Local Log Files"
echo "----------------------"
LOCAL_LOGS=$(ls -la /aws_dda/greengrass/v2/logs/*.log 2>/dev/null | wc -l)
if [ "$LOCAL_LOGS" -gt 0 ]; then
    check_mark "Local log files exist ($LOCAL_LOGS files)"
    ls -lh /aws_dda/greengrass/v2/logs/*.log | head -5
else
    cross_mark "No local log files found"
fi
echo ""

# Test 3: LogManager configuration
echo "Test 3: LogManager Configuration"
echo "--------------------------------"
if grep -A 50 'aws.greengrass.LogManager' /aws_dda/greengrass/v2/config/effectiveConfig.yaml | grep -q 'uploadToCloudWatch.*true'; then
    check_mark "uploadToCloudWatch is enabled"
else
    cross_mark "uploadToCloudWatch is not enabled or not found"
    warning "Check LogManager configuration in deployment"
fi
echo ""

# Test 4: Network connectivity to CloudWatch
echo "Test 4: Network Connectivity to CloudWatch"
echo "------------------------------------------"
CURL_RESULT=$(curl -s -o /dev/null -w '%{http_code}' https://logs.$AWS_REGION.amazonaws.com 2>/dev/null || echo "000")
if [ "$CURL_RESULT" != "000" ]; then
    check_mark "Network connectivity to CloudWatch: HTTP $CURL_RESULT"
else
    cross_mark "Cannot reach CloudWatch endpoint (https://logs.$AWS_REGION.amazonaws.com)"
    warning "Check device security group allows outbound HTTPS (port 443)"
fi
echo ""

# Test 5: DNS resolution
echo "Test 5: DNS Resolution"
echo "---------------------"
if nslookup logs.$AWS_REGION.amazonaws.com 8.8.8.8 2>&1 | grep -q 'Address'; then
    check_mark "DNS resolution working for logs.$AWS_REGION.amazonaws.com"
else
    cross_mark "DNS resolution failed for logs.$AWS_REGION.amazonaws.com"
fi
echo ""

# Test 6: LogManager upload activity
echo "Test 6: LogManager Upload Activity"
echo "---------------------------------"
if tail -100 /aws_dda/greengrass/v2/logs/aws.greengrass.LogManager.log 2>/dev/null | grep -i 'upload\|cloudwatch' > /dev/null; then
    check_mark "LogManager upload activity detected"
    tail -100 /aws_dda/greengrass/v2/logs/aws.greengrass.LogManager.log 2>/dev/null | grep -i 'upload\|cloudwatch' | tail -3
else
    warning "No recent upload activity in LogManager logs"
    warning "This could mean: (1) LogManager hasn't run yet, or (2) No logs to upload"
fi
echo ""

# Test 7: LogManager errors
echo "Test 7: LogManager Error Check"
echo "-----------------------------"
if tail -200 /aws_dda/greengrass/v2/logs/aws.greengrass.LogManager.log 2>/dev/null | grep -i 'error\|failed\|exception' > /dev/null; then
    cross_mark "Errors found in LogManager logs:"
    tail -200 /aws_dda/greengrass/v2/logs/aws.greengrass.LogManager.log 2>/dev/null | grep -i 'error\|failed\|exception' | tail -3
else
    check_mark "No errors in LogManager logs"
fi
echo ""

# Summary
echo "=================================="
echo "Diagnostic Summary"
echo "=================================="
echo ""
echo "If all tests pass:"
echo "  1. Wait 5 minutes for LogManager to upload logs"
echo "  2. Check CloudWatch Logs in AWS console"
echo "  3. Log groups should appear at: /aws/greengrass/GreengrassSystemComponent/$AWS_REGION/DEVICE_NAME"
echo ""
echo "If tests fail:"
echo "  1. Check device security group allows outbound HTTPS"
echo "  2. Verify device role has CloudWatch Logs permissions"
echo "  3. Check LogManager is included in deployment"
echo "  4. Review LogManager logs for specific errors"
echo ""
DIAG_EOF

chmod +x "$DIAG_SCRIPT"
echo "✓ Diagnostic script created at: $DIAG_SCRIPT"
echo "  Run: $DIAG_SCRIPT [region] to check CloudWatch logging"
echo ""

echo "=========================================="
echo "▶ Updating GreengrassV2TokenExchangeRole"
echo "=========================================="
echo ""

# Get AWS account ID for policy ARNs
aws_account_id=$(aws sts get-caller-identity --query 'Account' --output text 2>/dev/null || echo "")
if [ -z "$aws_account_id" ]; then
    add_warning "Could not get AWS account ID - skipping role policy updates"
else
    echo "AWS Account ID: $aws_account_id"
    echo ""
    
    # 1. Attach DDA Portal Component Access Policy (managed policy)
    echo "1. Attaching DDA Portal Component Access Policy..."
    DDA_POLICY_ARN="arn:aws:iam::${aws_account_id}:policy/DDAPortalComponentAccessPolicy"
    if aws iam get-policy --policy-arn "$DDA_POLICY_ARN" 2>/dev/null; then
        if run_cmd "aws iam attach-role-policy --role-name GreengrassV2TokenExchangeRole --policy-arn $DDA_POLICY_ARN"; then
            echo "   ✓ DDAPortalComponentAccessPolicy attached"
        else
            add_warning "Could not attach DDAPortalComponentAccessPolicy. Attach manually if needed."
        fi
    else
        add_warning "DDAPortalComponentAccessPolicy not found. Deploy UseCaseAccountStack first."
    fi
    echo ""
    
    # 2. Add S3 component access policy (inline policy)
    echo "2. Adding S3 component access policy..."
    if run_cmd "aws iam put-role-policy \
      --role-name GreengrassV2TokenExchangeRole \
      --policy-name GreengrassComponentS3Access \
      --policy-document '{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
          {
            \"Effect\": \"Allow\",
            \"Action\": [
              \"s3:GetObject\",
              \"s3:GetObjectVersion\"
            ],
            \"Resource\": [
              \"arn:aws:s3:::dda-component-*/*\",
              \"arn:aws:s3:::dda-component-us-east-1-*/*\"
            ]
          }
        ]
      }'"; then
        echo "   ✓ S3 component access policy attached"
    else
        add_warning "Could not attach S3 component access policy. Device may not be able to download components."
    fi
    echo ""
    
    # 3. Add CloudWatch Logs policy (inline policy)
    echo "3. Adding CloudWatch Logs policy..."
    if run_cmd "aws iam put-role-policy \
      --role-name GreengrassV2TokenExchangeRole \
      --policy-name CloudWatchLogsPolicy \
      --policy-document '{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
          {
            \"Effect\": \"Allow\",
            \"Action\": [
              \"logs:CreateLogGroup\",
              \"logs:CreateLogStream\",
              \"logs:PutLogEvents\",
              \"logs:DescribeLogStreams\"
            ],
            \"Resource\": \"arn:aws:logs:*:*:log-group:/aws/greengrass/*\"
          }
        ]
      }'"; then
        echo "   ✓ CloudWatch Logs policy attached"
    else
        add_warning "Could not attach CloudWatch Logs policy. Device may not be able to upload logs to CloudWatch."
    fi
    echo ""
    
    # 4. Verify all policies are attached
    echo "4. Verifying role policies..."
    ATTACHED_POLICIES=$(aws iam list-attached-role-policies --role-name GreengrassV2TokenExchangeRole --query 'AttachedPolicies[].PolicyName' --output text 2>/dev/null)
    INLINE_POLICIES=$(aws iam list-role-policies --role-name GreengrassV2TokenExchangeRole --query 'PolicyNames' --output text 2>/dev/null)
    
    echo "   Attached managed policies:"
    if [ -n "$ATTACHED_POLICIES" ]; then
        echo "$ATTACHED_POLICIES" | tr ' ' '\n' | sed 's/^/     - /'
    else
        echo "     (none)"
    fi
    
    echo "   Inline policies:"
    if [ -n "$INLINE_POLICIES" ]; then
        echo "$INLINE_POLICIES" | tr ' ' '\n' | sed 's/^/     - /'
    else
        echo "     (none)"
    fi
    echo ""
    
    echo "✓ GreengrassV2TokenExchangeRole updated successfully"
fi
echo ""
