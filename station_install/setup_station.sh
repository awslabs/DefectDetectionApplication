#!/bin/bash
# NOTE: Removed -e flag to allow proper error handling for Python 3.11 build
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

# Resolve AWS credentials into this shell's environment for tools that use the
# AWS SDK default provider chain (the Greengrass Java provisioner, and every
# `aws` CLI call below). Handles the two failure modes seen with AWS CLI v2:
#   1. `aws login --remote` / `aws sso login` sessions: the resolved session
#      lives in the CLI's token cache, not in static env vars, so materialize it
#      with `aws configure export-credentials`.
#   2. Running under sudo: this script requires root, but the operator usually
#      runs `aws login --remote` as their normal user, leaving root's ~/.aws
#      empty ("Unable to locate credentials"). Fall back to the invoking user's
#      context ($SUDO_USER) when root has no credentials.
# Drops AWS_CREDENTIAL_EXPIRATION so downstream SDKs treat the values as static
# (avoids the "refreshed credentials are still expired" botocore path). Returns
# 0 and exports AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN on success, 1 otherwise.
resolve_aws_credentials() {
    # Already provided in the environment (e.g. exported before invoking).
    if [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then
        return 0
    fi
    local creds=""
    # Try the current (root, under sudo) context first.
    creds=$(aws configure export-credentials --format env 2>/dev/null | grep -v AWS_CREDENTIAL_EXPIRATION)
    # Fall back to the invoking user's context — they likely hold the
    # `aws login --remote` session while root's ~/.aws is empty.
    if [ -z "$creds" ] && [ -n "${SUDO_USER:-}" ]; then
        creds=$(sudo -u "$SUDO_USER" -H aws configure export-credentials --format env 2>/dev/null | grep -v AWS_CREDENTIAL_EXPIRATION)
        if [ -n "$creds" ]; then
            echo "Resolved AWS credentials from invoking user '$SUDO_USER' (sudo context)."
        fi
    fi
    if [ -n "$creds" ]; then
        eval "$creds"
        export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
        return 0
    fi
    return 1
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
    
    # Python 3.11
    if ! check_command python3.11 && ! check_command /usr/local/bin/python3.11; then
        add_warning "Python 3.11 not found - will attempt to install"
    else
        echo "✓ Python 3.11 found"
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
  # Check for python3.11 specifically
  if command -v python3.11 >/dev/null 2>&1; then
    echo "Python 3.11 already installed, skipping build"
    return 0
  fi
  
  # Also check /usr/local/bin/python3.11 (where altinstall puts it)
  if [ -x /usr/local/bin/python3.11 ]; then
    echo "Python 3.11 already installed at /usr/local/bin/python3.11, skipping build"
    return 0
  fi
  
  echo "Installing Python 3.11 from source (no prebuilt deadsnakes package for this Ubuntu release/arch)."
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
  local build_dir="/tmp/python311_build"
  mkdir -p "$build_dir"
  cd "$build_dir"

  # Download Python 3.11 source code
  if [ ! -f "Python-3.11.9.tgz" ]; then
    echo "Downloading Python 3.11.9..."
    if ! run_cmd "wget https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz"; then
      add_error "Failed to download Python 3.11.9"
      cd "$current_dir"
      return 1
    fi
  fi
  
  # Extract if not already extracted
  if [ ! -d "Python-3.11.9" ]; then
    echo "Extracting Python source..."
    if ! run_cmd "tar -xf Python-3.11.9.tgz"; then
      add_error "Failed to extract Python source"
      cd "$current_dir"
      return 1
    fi
  fi
  
  cd Python-3.11.9

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
  if [ -x /usr/local/bin/python3.11 ]; then
    echo "✓ Python 3.11 installed successfully from source."
    /usr/local/bin/python3.11 --version
  else
    add_error "Python 3.11 installation failed - binary not found!"
    return 1
  fi
}

# Function to install from deadsnakes PPA
install_from_ppa() {
  echo "Ubuntu version is not 18.04. Installing Python 3.11 from the deadsnakes PPA."

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

  # Install Python 3.11
  if ! run_cmd "apt update"; then
    add_error "Failed to update package manager after adding PPA"
    return 1
  fi
  
  if ! run_cmd "apt install -y python3.11"; then
    add_error "Failed to install Python 3.11 from PPA"
    return 1
  fi
  
  run_cmd "apt install python3.11-venv -y" || add_warning "Failed to install python3.11-venv"

  echo "✓ Python 3.11 installed successfully from the deadsnakes PPA."
}

# Returns 0 if the argument looks like an AWS region (e.g. us-east-1,
# eu-west-2, ap-southeast-1, us-gov-west-1, cn-north-1). Used to catch the
# common mistake of passing the args in the wrong order — a bogus --aws-region
# makes Greengrass provisioning fail in confusing ways ("Unable to load
# credentials" / region errors) that look like an auth problem but aren't.
looks_like_region() {
    echo "$1" | grep -qE '^[a-z]{2}(-gov|-iso[a-z]?)?-[a-z]+-[0-9]+$'
}

# Require both arguments.
if [ $# -lt 2 ]; then
    echo "Usage: $0 <aws-region> <thing_name>"
    echo "Example: $0 us-east-1 dda_thing_1"
    exit 1
fi

SETUP_STARTED=1

aws_region="$1"
thing_name="$2"

# Guard against swapped arguments. The script signature is
# "<aws-region> <thing_name>", but operators frequently invoke it as
# "<thing_name> <aws-region>". Detect and correct that: if arg1 is not a valid
# region but arg2 is, swap them (with a warning); if neither is a region, stop
# with a clear error rather than sending a garbage --aws-region to Greengrass.
if ! looks_like_region "$aws_region"; then
    if looks_like_region "$thing_name"; then
        add_warning "Arguments appear swapped: '$aws_region' is not a valid AWS region but '$thing_name' is. Auto-correcting to <region> <thing_name>. Correct order is: $0 <aws-region> <thing_name>"
        local_tmp="$aws_region"
        aws_region="$thing_name"
        thing_name="$local_tmp"
    else
        add_error "'$aws_region' does not look like an AWS region (expected e.g. us-east-1). Usage: $0 <aws-region> <thing_name>"
        exit 1
    fi
fi

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

echo "▶ Installing Python 3.11..."
# The deadsnakes PPA has NO prebuilt python3.11 for Ubuntu 18.04 (bionic, JP4)
# or 20.04 (focal, JP5) on arm64 (Jetson) — it only ships arm64 packages for
# 22.04 (jammy, JP6). So build from source on 18.04/20.04 and use the PPA only
# on 22.04+ (with a source-build fallback if the PPA install fails, e.g. on an
# unexpected arch/release). In every case the SYSTEM python3 is left unchanged —
# apt and other OS tools depend on it and its C extensions (apt_pkg, etc.);
# python3.11 is installed alongside it.
case "$UBUNTU_VERSION" in
  18.04|20.04)
    echo "Detected Ubuntu $UBUNTU_VERSION - building Python 3.11 from source (deadsnakes has no prebuilt 3.11 for this release/arch)..."
    if ! install_from_source; then
      add_error "Python 3.11 installation from source failed"
    fi
    ;;
  *)
    echo "Detected Ubuntu $UBUNTU_VERSION - installing Python 3.11 from the deadsnakes PPA..."
    if ! install_from_ppa; then
      add_warning "deadsnakes PPA install failed - falling back to building Python 3.11 from source..."
      if ! install_from_source; then
        add_error "Python 3.11 installation failed (deadsnakes PPA and source build)"
      fi
    fi
    ;;
esac

# A source build (make altinstall) installs to /usr/local/bin/python3.11. Expose
# it at /usr/bin/python3.11 (on PATH for DDA components) if it isn't already
# there. The deadsnakes package already installs /usr/bin/python3.11. Never
# touch the system python3.
if [ -x /usr/local/bin/python3.11 ] && [ ! -e /usr/bin/python3.11 ]; then
  ln -sf /usr/local/bin/python3.11 /usr/bin/python3.11 2>/dev/null || add_warning "Failed to create python3.11 symlink"
fi
echo "✓ Python 3.11 available as python3.11 (system python3 left unchanged)"

if ! run_cmd "apt-get install python3-pip -y"; then
    add_error "Failed to install pip"
else
    echo "✓ pip installed"
fi

# Find python3.11 location
PYTHON311=""
if [ -x /usr/local/bin/python3.11 ]; then
  PYTHON311="/usr/local/bin/python3.11"
elif [ -x /usr/bin/python3.11 ]; then
  PYTHON311="/usr/bin/python3.11"
fi

if [ -n "$PYTHON311" ]; then
  echo "Using Python at: $PYTHON311"
  run_cmd "$PYTHON311 -m pip install --upgrade pip" || add_warning "Failed to upgrade pip"
  run_cmd "$PYTHON311 -m pip install --force-reinstall requests==2.32.4" || add_warning "Failed to install requests"
  run_cmd "$PYTHON311 -m pip install protobuf" || add_warning "Failed to install protobuf"
else
  add_warning "python3.11 not found. Using system python3 instead."
  run_cmd "python3 -m pip install --upgrade pip" || add_warning "Failed to upgrade pip"
  run_cmd "python3 -m pip install requests protobuf" || add_warning "Failed to install Python packages"
fi

# The model component's Greengrass Startup/Shutdown lifecycle runs
#   python3 /aws_dda/model_convertor.py ...       (Startup)
#   python3 /aws_dda/convert_model_cleanup.py ... (Shutdown)
# on the HOST with the *system* python3 (NOT python3.11). Those scripts need
# protobuf >= 3.20 (generated model_config_pb2.py imports
# `google.protobuf.internal.builder`) AND requests (to call the LocalServer
# API). On JetPack 6 (Ubuntu 22.04) the system python3 is 3.10 and lacks both,
# so model deploys fail with:
#   ImportError: cannot import name 'builder' from 'google.protobuf.internal'
#   ModuleNotFoundError: No module named 'requests'
# Ensure the system python3 always has both, even when a separate python3.11 is
# present and used above.
if ! python3 -c "from google.protobuf.internal import builder" >/dev/null 2>&1; then
  echo "Installing protobuf>=3.20 for system python3 (model-conversion Startup script)..."
  run_cmd "python3 -m pip install --upgrade 'protobuf>=3.20,<5'" || \
    add_warning "Failed to install protobuf for system python3; model deploys may fail with the 'builder' ImportError."
else
  echo "✓ system python3 protobuf already supports the model-conversion script"
fi
if ! python3 -c "import requests" >/dev/null 2>&1; then
  echo "Installing requests for system python3 (model-conversion scripts)..."
  run_cmd "python3 -m pip install --upgrade requests" || \
    add_warning "Failed to install requests for system python3; model deploys may fail with 'No module named requests'."
else
  echo "✓ system python3 already has requests"
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

# Remove snap Docker if present — the snap bundles iptables 1.8.10 (nf_tables)
# which is incompatible with older kernels (4.9.x on JetPack 4.6.x / Ubuntu 18.04).
# It also conflicts with the apt docker.io package we need.
if snap list docker >/dev/null 2>&1; then
    echo "Removing snap Docker (incompatible with this kernel)..."
    run_cmd "snap stop docker" || true
    run_cmd "snap remove docker --purge" || run_cmd "snap remove docker" || add_warning "Failed to remove snap docker"
    # Also remove snap docker-compose if present
    if snap list docker-compose >/dev/null 2>&1; then
        run_cmd "snap remove docker-compose --purge" || run_cmd "snap remove docker-compose" || true
    fi
    # Clean up snap Docker state
    run_cmd "rm -rf /var/snap/docker" || true
    echo "✓ Snap Docker removed"
    # Clear the hash so bash doesn't cache the old snap docker path
    hash -r 2>/dev/null
fi

if check_command docker && docker ps >/dev/null 2>&1; then
    # Check if docker compose V2 (plugin) is available
    if docker compose version >/dev/null 2>&1; then
        echo "✓ Docker already installed with Compose V2 plugin"
    else
        echo "⚠️  Docker found but Compose V2 plugin missing — installing plugin..."
        COMPOSE_ARCH=$(uname -m)
        case "$COMPOSE_ARCH" in
            aarch64) COMPOSE_ARCH="aarch64" ;;
            x86_64)  COMPOSE_ARCH="x86_64" ;;
        esac
        mkdir -p /usr/local/lib/docker/cli-plugins
        if run_cmd "curl -fsSL https://github.com/docker/compose/releases/download/v2.24.7/docker-compose-linux-${COMPOSE_ARCH} -o /usr/local/lib/docker/cli-plugins/docker-compose"; then
            run_cmd "chmod +x /usr/local/lib/docker/cli-plugins/docker-compose"
            if docker compose version >/dev/null 2>&1; then
                echo "✓ Docker Compose V2 plugin installed successfully"
            else
                add_error "Docker Compose V2 plugin installed but not working"
            fi
        else
            add_error "Failed to download Docker Compose V2 plugin"
        fi
    fi
else
    if [ "$UBUNTU_VERSION" = "18.04" ]; then
        # Ubuntu 18.04 / JetPack 4.6.x — NVIDIA ships its own Docker 19.03 with
        # nvidia-container-runtime pre-configured. docker-ce from Docker's repo
        # conflicts with NVIDIA's packages and breaks the daemon.
        # We must fully purge any docker-ce remnants before installing docker.io.
        echo "Ubuntu 18.04 / JetPack detected — using NVIDIA-provided Docker..."
        
        # Purge any conflicting docker-ce packages and config left from prior installs
        if dpkg -l docker-ce >/dev/null 2>&1 || dpkg -l docker-ce-cli >/dev/null 2>&1; then
            echo "Removing conflicting docker-ce packages..."
            run_cmd "systemctl stop docker.socket" || true
            run_cmd "systemctl stop docker" || true
            run_cmd "systemctl stop containerd" || true
            run_cmd "apt-get purge -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin" || true
            run_cmd "apt-get autoremove -y" || true
            # Clean up leftover config and state that prevent docker.io from starting
            run_cmd "rm -rf /var/lib/docker" || true
            run_cmd "rm -rf /var/lib/containerd" || true
            run_cmd "rm -f /etc/apt/sources.list.d/docker.list" || true
            run_cmd "rm -f /etc/apt/keyrings/docker.gpg" || true
            echo "✓ Conflicting docker-ce packages removed"
        fi
        
        # Install NVIDIA's Docker packages
        if ! run_cmd "apt-get update"; then
            add_warning "Failed to update package manager"
        fi
        if ! run_cmd "apt-get install -y docker.io containerd"; then
            add_error "Failed to install docker.io"
        else
            echo "✓ docker.io installed"
        fi
        
        # Install nvidia-container-runtime if not present (needed for GPU containers)
        if ! dpkg -l nvidia-container-runtime >/dev/null 2>&1; then
            run_cmd "apt-get install -y nvidia-container-runtime" || add_warning "nvidia-container-runtime not available — GPU containers may not work"
        fi
        
        # Install Compose V2 plugin manually (required for --profile flag)
        echo "Installing Docker Compose V2 plugin..."
        COMPOSE_ARCH=$(uname -m)
        case "$COMPOSE_ARCH" in
            aarch64) COMPOSE_ARCH="aarch64" ;;
            x86_64)  COMPOSE_ARCH="x86_64" ;;
        esac
        mkdir -p /usr/local/lib/docker/cli-plugins
        if run_cmd "curl -fsSL https://github.com/docker/compose/releases/download/v2.24.7/docker-compose-linux-${COMPOSE_ARCH} -o /usr/local/lib/docker/cli-plugins/docker-compose"; then
            run_cmd "chmod +x /usr/local/lib/docker/cli-plugins/docker-compose"
            echo "✓ Docker Compose V2 plugin installed"
        else
            add_error "Failed to download Docker Compose V2 plugin"
        fi
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
fi

# Ensure Docker daemon is enabled and running
echo "▶ Enabling and starting Docker service..."
run_cmd "systemctl enable docker" || add_warning "Failed to enable Docker service"
if ! run_cmd "systemctl start docker"; then
    add_error "Failed to start Docker daemon"
else
    echo "✓ Docker daemon is running"
fi
echo ""

# Ensure the iptables kernel modules Docker bridge networking needs are loaded.
#
# Newer Docker (as shipped on JetPack 6 / Ubuntu 22.04) sets up bridge networks
# with a "DIRECT ACCESS FILTERING" DROP rule in the iptables `raw` table. If the
# iptable_raw (and iptable_nat/iptable_filter) kernel modules aren't loaded,
# container startup fails with:
#   "iptables ... can't initialize iptables table `raw': Table does not exist
#    (do you need to insmod?)"
# Load them now and persist via /etc/modules-load.d so they survive reboots.
echo "▶ Ensuring Docker iptables kernel modules are loaded..."
DOCKER_IPT_MODULES="iptable_raw iptable_nat iptable_filter ip_tables br_netfilter"
IPT_PERSIST=/etc/modules-load.d/dda-docker-iptables.conf
RAW_MODULE_OK=0
: > /tmp/dda-ipt-modules.conf || true
for mod in $DOCKER_IPT_MODULES; do
    if run_cmd "modprobe $mod"; then
        echo "$mod" >> /tmp/dda-ipt-modules.conf
        echo "   ✓ loaded $mod"
        [ "$mod" = "iptable_raw" ] && RAW_MODULE_OK=1
    elif [ "$mod" = "iptable_raw" ]; then
        # Expected on stock JetPack 6 kernels (built without CONFIG_IP_NF_RAW).
        echo "   • iptable_raw not available in this kernel (handled below)"
    else
        add_warning "Could not load kernel module $mod (Docker bridge networking may fail on this host)."
    fi
done
# Persist for reboots (best-effort).
if [ -s /tmp/dda-ipt-modules.conf ]; then
    cp /tmp/dda-ipt-modules.conf "$IPT_PERSIST" 2>/dev/null || \
        add_warning "Could not write $IPT_PERSIST to persist iptables modules across reboots."
fi
rm -f /tmp/dda-ipt-modules.conf 2>/dev/null || true
# A fresh Docker daemon restart re-evaluates iptables now that modules are present.
run_cmd "systemctl restart docker" || add_warning "Failed to restart Docker after loading iptables modules"
echo ""

# Docker bridge-networking compatibility on JetPack 6.
#
# Stock JetPack 6 Tegra kernels are built WITHOUT CONFIG_IP_NF_RAW, so the
# iptables `raw` table does not exist and iptable_raw can't be loaded. Docker
# 28+ programs a DROP rule in that raw table for bridge networks with port
# mappings ("DIRECT ACCESS FILTERING"), so the DDA frontend container fails to
# start. NVIDIA's recommended fix (short of rebuilding the kernel) is to pin
# Docker to 27.5.1, which does not use the raw-table rule. We only downgrade
# when the raw table is genuinely unavailable AND Docker is >= 28.
if [ "$RAW_MODULE_OK" -eq 1 ] || iptables -t raw -L -n >/dev/null 2>&1; then
    echo "✓ iptables 'raw' table available — Docker bridge networking OK"
else
    DOCKER_VER=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
    DOCKER_MAJOR=$(echo "$DOCKER_VER" | cut -d. -f1)
    if echo "$DOCKER_MAJOR" | grep -qE '^[0-9]+$' && [ "$DOCKER_MAJOR" -ge 28 ]; then
        echo "▶ iptables 'raw' table unavailable and Docker $DOCKER_VER >= 28 — pinning Docker to 27.5.1 (JetPack 6 fix)..."
        if run_cmd "apt-get install -y --allow-downgrades docker-ce=5:27.5.1* docker-ce-cli=5:27.5.1*"; then
            echo "   ✓ Docker downgraded to 27.5.1"
            run_cmd "apt-mark hold docker-ce docker-ce-cli" || \
                add_warning "Could not 'apt-mark hold' Docker; a future apt upgrade may bump it back to 28+ and break bridge networking."
            run_cmd "systemctl restart docker" || add_warning "Failed to restart Docker after downgrade"
        else
            add_warning "Failed to pin Docker to 27.5.1. The DDA frontend container may fail to start on this JetPack 6 kernel. Fix manually: sudo apt-get install -y --allow-downgrades docker-ce=5:27.5.1* docker-ce-cli=5:27.5.1*"
        fi
    else
        echo "✓ Docker $DOCKER_VER does not require the iptables 'raw' table — no change needed"
    fi
fi
echo ""

# Register the NVIDIA Container Runtime with the Docker daemon (Jetson/aarch64).
#
# On Jetson/L4T the DDA LocalServer container gets GPU access via
# `runtime: nvidia` in docker-compose (the supported mechanism). That requires
# the `nvidia` runtime to be registered in /etc/docker/daemon.json. NVIDIA's
# JetPack 4.6 Docker pre-registers it, but a stock docker-ce install on
# JetPack 5 (Ubuntu 20.04) installs nvidia-container-runtime WITHOUT wiring it
# into the daemon, so containers fail to start with:
#   "unknown or invalid runtime name: nvidia"
# Registering it here (idempotent) fixes that. Without it, compose falls back to
# the unsupported --gpus/CDI hook and fails with:
#   "invoking the NVIDIA Container Runtime Hook directly ... is not supported."
ARCH_RAW=$(uname -m)
if [ "$ARCH_RAW" = "aarch64" ]; then
    echo "▶ Configuring NVIDIA Container Runtime for Docker (Jetson)..."
    if docker info 2>/dev/null | grep -qi "Runtimes:.*nvidia"; then
        echo "✓ nvidia runtime already registered with Docker"
    elif command -v nvidia-ctk >/dev/null 2>&1; then
        # Preferred: let the toolkit wire up the runtime and set it as default.
        if run_cmd "nvidia-ctk runtime configure --runtime=docker --set-as-default"; then
            run_cmd "systemctl restart docker" || add_warning "Failed to restart Docker after nvidia-ctk configure"
            echo "✓ nvidia runtime registered via nvidia-ctk"
        else
            add_warning "nvidia-ctk runtime configure failed — GPU containers may not start"
        fi
    elif command -v nvidia-container-runtime >/dev/null 2>&1; then
        # Fallback: merge the nvidia runtime into /etc/docker/daemon.json directly.
        echo "nvidia-ctk not found — registering nvidia runtime in daemon.json directly..."
        NVIDIA_RUNTIME_PATH=$(command -v nvidia-container-runtime)
        mkdir -p /etc/docker
        python3 - "$NVIDIA_RUNTIME_PATH" <<'PYEOF' || add_warning "Failed to update /etc/docker/daemon.json with nvidia runtime"
import json, os, sys
runtime_path = sys.argv[1]
path = "/etc/docker/daemon.json"
cfg = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            cfg = json.load(f) or {}
    except (ValueError, OSError):
        cfg = {}
runtimes = cfg.setdefault("runtimes", {})
runtimes["nvidia"] = {"path": runtime_path, "runtimeArgs": []}
# Make nvidia the default runtime so `runtime: nvidia` and plain runs both work.
cfg.setdefault("default-runtime", "nvidia")
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("Updated /etc/docker/daemon.json")
PYEOF
        run_cmd "systemctl restart docker" || add_warning "Failed to restart Docker after daemon.json update"
        if docker info 2>/dev/null | grep -qi "Runtimes:.*nvidia"; then
            echo "✓ nvidia runtime registered via daemon.json"
        else
            add_warning "nvidia runtime still not registered — GPU containers may not start"
        fi
    else
        add_warning "Neither nvidia-ctk nor nvidia-container-runtime found — GPU containers will not start. Install nvidia-container-runtime."
    fi
    echo ""
fi

echo "▶ Installing Greengrass Core..."
if [ -f "greengrass-${greengrass_version}.zip" ] && [ -d "GreengrassInstaller" ]; then
    echo "✓ Greengrass already downloaded and extracted, skipping"
else
    if [ ! -f "greengrass-${greengrass_version}.zip" ]; then
        if ! run_cmd "curl -s 'https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-${greengrass_version}.zip' > 'greengrass-${greengrass_version}.zip'"; then
            add_error "Failed to download Greengrass"
        fi
    fi
    
    if [ -f "greengrass-${greengrass_version}.zip" ]; then
        rm -rf GreengrassInstaller
        if ! run_cmd "unzip -o greengrass-${greengrass_version}.zip -d GreengrassInstaller"; then
            add_error "Failed to extract Greengrass"
        else
            echo "✓ Greengrass Core downloaded and extracted"
        fi
    fi
fi
echo ""

echo "▶ Provisioning Greengrass Core Device..."
# The Greengrass provisioner is a Java app that uses the AWS Java SDK's default
# credential provider chain. That chain cannot read the AWS CLI's credential
# sources in all contexts (SSO cache, `aws login` session, some IMDS setups),
# which fails with:
#   SdkClientException: Unable to load credentials from any of the providers...
# Materialize the active credentials via the CLI and pass them to Java both as
# environment variables and as -Daws.* system properties so the SDK always sees
# them. (Fix ported from the jp5v2 branch; lost during the merge/rebase.)
# Resolve credentials (handles `aws login --remote` sessions and the sudo/root
# context mismatch) and export them so BOTH the Java provisioner and every
# subsequent `aws` CLI call in this script (account id lookup, tagging, role
# policy updates) can authenticate.
resolve_aws_credentials || true

JAVA_CRED_PROPS=""
if [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then
    JAVA_CRED_PROPS="-Daws.accessKeyId=$AWS_ACCESS_KEY_ID -Daws.secretAccessKey=$AWS_SECRET_ACCESS_KEY"
    if [ -n "${AWS_SESSION_TOKEN:-}" ]; then
        JAVA_CRED_PROPS="$JAVA_CRED_PROPS -Daws.sessionToken=$AWS_SESSION_TOKEN"
    fi
else
    add_warning "No AWS credentials resolved for Greengrass provisioning - provisioning will likely fail. Authenticate first (e.g. 'aws login --remote' or 'aws sso login'), then verify with 'aws sts get-caller-identity'. NOTE: this script runs as root via sudo; if you authenticated as your normal user, credentials are read from that user (\$SUDO_USER) automatically, but only if 'sudo -u <you> aws sts get-caller-identity' works."
fi

if ! AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" AWS_SESSION_TOKEN="$AWS_SESSION_TOKEN" \
    run_cmd "java -Droot=/aws_dda/greengrass/v2 -Dlog.store=FILE $JAVA_CRED_PROPS -jar ./GreengrassInstaller/lib/Greengrass.jar --aws-region ${aws_region} --thing-name ${thing_name} --thing-group-name DDA_transition_EC2_Group --thing-policy-name GreengrassV2IoTThingPolicy --tes-role-name GreengrassV2TokenExchangeRole --tes-role-alias-name GreengrassCoreTokenExchangeRoleAlias --component-default-user ggc_user:ggc_group --setup-system-service true --provision true"; then
    add_error "Greengrass provisioning failed"
else
    echo "✓ Greengrass Core provisioned successfully"
fi
echo ""

echo "▶ Configuring Greengrass permissions..."
run_cmd "usermod -aG video ggc_user" || add_warning "Failed to add ggc_user to video group"
run_cmd "usermod -aG docker ggc_user" || add_warning "Failed to add ggc_user to docker group"
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

echo "▶ Installing OpenCV for application health reporting..."
# The Application Health Overview page reads the OpenCV version from the LFV
# edge-agent venv site-packages (see get_opencv_version_from_lfv in
# src/backend/endpoints/system.py, which appends this path to sys.path and looks
# up the 'opencv_python_headless' distribution). The LocalServer backend runs in
# a container with /aws_dda bind-mounted, so this venv path is the only
# station-writable location the backend can actually read. Pre-seed
# opencv-python-headless there so the health page reports a version by default
# instead of "Not found".
lfv_agent_component="aws.iot.lookoutvision.EdgeAgent"
lfv_venv_site_packages="${dda_greengrass_root_folder}/work/${lfv_agent_component}/env/lib/python3.11/site-packages"

if [ -z "$PYTHON311" ]; then
    add_warning "python3.11 not found - skipping OpenCV install for health reporting"
else
    if run_cmd "mkdir -p ${lfv_venv_site_packages}"; then
        if run_cmd "$PYTHON311 -m pip install --target ${lfv_venv_site_packages} --upgrade opencv-python-headless"; then
            echo "✓ OpenCV (opencv-python-headless) installed for health reporting"
            # Make readable by the Greengrass component user that runs the LFV
            # agent / LocalServer (created during provisioning above).
            if isUserExists ggc_user; then
                run_cmd "chown -R ggc_user:ggc_group ${dda_greengrass_root_folder}/work/${lfv_agent_component}" \
                    || add_warning "Failed to set ownership on LFV agent venv path"
            fi
        else
            add_warning "Failed to install opencv-python-headless - health page may show OpenCV as 'Not found'"
        fi
    else
        add_warning "Failed to create LFV agent venv path for OpenCV install"
    fi
fi
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

    # 3.5 Add ECR access policy (inline policy)
    # Required for Docker-based components (e.g. aws.edgeml.dda.LocalServer) whose
    # artifacts are published to ECR. Without ecr:GetAuthorizationToken the device
    # fails with GET_ECR_CREDENTIAL_ERROR / "Failed to get auth token for docker login".
    # ecr:GetAuthorizationToken does not support resource scoping and must use "*".
    echo "3.5 Adding ECR access policy..."
    if run_cmd "aws iam put-role-policy \
      --role-name GreengrassV2TokenExchangeRole \
      --policy-name ECRComponentAccess \
      --policy-document '{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
          {
            \"Sid\": \"AllowEcrAuthToken\",
            \"Effect\": \"Allow\",
            \"Action\": [
              \"ecr:GetAuthorizationToken\"
            ],
            \"Resource\": \"*\"
          },
          {
            \"Sid\": \"AllowEcrImagePull\",
            \"Effect\": \"Allow\",
            \"Action\": [
              \"ecr:BatchGetImage\",
              \"ecr:GetDownloadUrlForLayer\",
              \"ecr:BatchCheckLayerAvailability\"
            ],
            \"Resource\": \"arn:aws:ecr:*:${aws_account_id}:repository/dda/*\"
          }
        ]
      }'"; then
        echo "   ✓ ECR access policy attached"
    else
        add_warning "Could not attach ECR access policy. Device may not be able to pull Docker-based components from ECR."
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
