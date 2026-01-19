#!/bin/bash
# NOTE: Removed -e flag to allow proper error handling for Python 3.9 build
# We use explicit error checking instead

# Get the Ubuntu release version
UBUNTU_VERSION=$(lsb_release -rs)

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
  apt update
  if ! apt install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev libssl-dev libreadline-dev libffi-dev wget; then
    echo "ERROR: Failed to install build dependencies"
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
    if ! wget https://www.python.org/ftp/python/3.9.18/Python-3.9.18.tgz; then
      echo "ERROR: Failed to download Python 3.9.18"
      cd "$current_dir"
      return 1
    fi
  fi
  
  # Extract if not already extracted
  if [ ! -d "Python-3.9.18" ]; then
    echo "Extracting Python source..."
    if ! tar -xf Python-3.9.18.tgz; then
      echo "ERROR: Failed to extract Python source"
      cd "$current_dir"
      return 1
    fi
  fi
  
  cd Python-3.9.18

  # Configure, compile, and install
  echo "Configuring Python build..."
  if ! ./configure --enable-optimizations; then
    echo "ERROR: Python configure failed"
    cd "$current_dir"
    return 1
  fi
  
  echo "Compiling Python (this takes ~10-15 minutes on ARM64)..."
  if ! make -j "$(nproc)"; then
    echo "ERROR: Python compilation failed"
    cd "$current_dir"
    return 1
  fi
  
  echo "Installing Python..."
  if ! make altinstall; then
    echo "ERROR: Python installation failed"
    cd "$current_dir"
    return 1
  fi

  # Return to original directory
  cd "$current_dir"
  
  # Verify installation
  if [ -x /usr/local/bin/python3.9 ]; then
    echo "Python 3.9 installed successfully from source."
    /usr/local/bin/python3.9 --version
  else
    echo "ERROR: Python 3.9 installation failed - binary not found!"
    return 1
  fi
}

# Function to install from deadsnakes PPA
install_from_ppa() {
  echo "Ubuntu version is not 18.04. Installing Python 3.9 from the deadsnakes PPA."

  # Add the deadsnakes PPA
  apt update
  if ! apt install -y software-properties-common; then
    echo "ERROR: Failed to install software-properties-common"
    return 1
  fi
  
  if ! add-apt-repository -y ppa:deadsnakes/ppa; then
    echo "ERROR: Failed to add deadsnakes PPA"
    return 1
  fi

  # Install Python 3.9
  apt update
  if ! apt install -y python3.9; then
    echo "ERROR: Failed to install Python 3.9 from PPA"
    return 1
  fi
  
  echo "Python 3.9 is available. Installing python3.9-venv package..."
  apt install python3.9-venv -y || true

  echo "Python 3.9 installed successfully from the deadsnakes PPA."
}

# Check if region parameter is provided
if [ $# -eq 0 ]; then
    echo "Usage: $0 <aws-region> <thing_name>"
    
    echo "Example: $0 us-east-1 dda_thing_1"
    exit 1
fi

aws_region="$1"
thing_name="$2"
echo "Using AWS region: $aws_region"

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


# DDA admin user/group
if ! isGroupExists dda_system_group; then
 groupadd dda_system_group
fi


if ! isUserExists dda_system_user; then
 useradd dda_system_user -g dda_system_group
 usermod -aG video dda_system_user
fi


# DDA customer user/group
if ! isGroupExists dda_admin_group; then
 groupadd dda_admin_group
fi


if ! isUserExists dda_admin_user; then
 useradd dda_admin_user -g dda_admin_group
fi


# Default device user
default_user=$(awk -F":" '/1000/ {print $1}' /etc/passwd)
usermod -aG dda_admin_group dda_system_user
usermod -aG dda_admin_group "${default_user}"

# Setup DDA root folder
mkdir -p "${dda_root_folder}"
chgrp dda_system_group "${dda_root_folder}"
chown dda_system_user "${dda_root_folder}"


# Setup DDA GGv2 folder
mkdir -p "${dda_greengrass_root_folder}"
chmod 755 "${dda_greengrass_root_folder}"


# Setup DDA image capture folder
mkdir -p "${dda_image_capture_dir}"
chgrp -R dda_admin_group "${dda_image_capture_dir}"
chown -R dda_admin_user "${dda_image_capture_dir}"


# Setup DDA inference results folder
mkdir -p "${dda_inference_result_dir}"
chgrp -R dda_admin_group "${dda_inference_result_dir}"
chown -R dda_admin_user "${dda_inference_result_dir}"


echo "Downloading and installing Greengrass Core"


apt-get update
apt-get install curl ca-certificates gnupg lsb-release unzip zip -y


echo "Installing Java"
apt-get install default-jdk -y
java -version


# AWS CLI
if [ -x "$(command -v aws)" ]; then
 echo "AWS CLI already installed"
else
 echo "Installing AWS CLI"
 curl "https://awscli.amazonaws.com/awscli-exe-linux-${architecture}.zip" -o "awscliv2.zip"
 unzip awscliv2.zip
 ./aws/install
fi

# Setup Python
echo "Installing Python3.9"
# Add deadsnakes PPA for Python 3.9 on Ubuntu 24.04
#add-apt-repository ppa:deadsnakes/ppa -y
#apt-get update
#apt-get install python3.9 python3.9-dev python3.9-venv python3.9-distutils -y
if [ "$UBUNTU_VERSION" = "18.04" ]; then
  echo "Detected Ubuntu 18.04 - building Python 3.9 from source..."
  if ! install_from_source; then
    echo ""
    echo "=========================================="
    echo "ERROR: Python 3.9 installation FAILED!"
    echo "=========================================="
    echo "Please check the error messages above."
    echo "Common issues:"
    echo "  - Missing disk space (need ~2GB free)"
    echo "  - Missing build dependencies"
    echo "  - Network issues downloading source"
    echo ""
    exit 1
  fi
  
  # Set up alternatives for python3.9
  if [ -x /usr/local/bin/python3.9 ]; then
    update-alternatives --install /usr/local/bin/python3 python3 /usr/local/bin/python3.9 1 || true
    # Also create symlink in /usr/bin for compatibility
    ln -sf /usr/local/bin/python3.9 /usr/bin/python3.9 2>/dev/null || true
  fi
else #x86 where ubuntu version is not 18.04
  if ! install_from_ppa; then
    echo "ERROR: Python 3.9 installation from PPA failed!"
    exit 1
  fi
  update-alternatives --install /usr/local/bin/python3 python3 /usr/bin/python3.9 1 || true
fi
echo "Installing Pip"
apt-get install python3-pip -y

# Find python3.9 location
PYTHON39=""
if [ -x /usr/local/bin/python3.9 ]; then
  PYTHON39="/usr/local/bin/python3.9"
elif [ -x /usr/bin/python3.9 ]; then
  PYTHON39="/usr/bin/python3.9"
fi

if [ -n "$PYTHON39" ]; then
  echo "Using Python at: $PYTHON39"
  $PYTHON39 -m pip install --upgrade pip || true
  $PYTHON39 -m pip install --force-reinstall requests==2.32.3 || true
  $PYTHON39 -m pip install protobuf || true
else
  echo "WARNING: python3.9 not found. Using system python3 instead."
  echo "DDA components run in Docker, so this is not critical."
  # Use system python3 as fallback
  python3 -m pip install --upgrade pip || true
  python3 -m pip install requests protobuf || true
fi





# Setup Gstreamer
echo "Installing Gstreamer"
apt-get install -y libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
 gstreamer1.0-libav gstreamer1.0-tools gstreamer1.0-x gstreamer1.0-alsa gstreamer1.0-gl \
 gstreamer1.0-gtk3 gstreamer1.0-qt5 gstreamer1.0-pulseaudio


# Edge Manager Agent Config
mkdir -p "${dda_greengrass_root_folder}/em_agent/capture_data" \
 "${dda_greengrass_root_folder}/em_agent/local_data" \
 "${dda_greengrass_root_folder}/em_agent/config"
pwd
cp ./edge_manager_agent_config.json "${dda_greengrass_root_folder}/em_agent/config"
echo "Edge Manager Agent config copied"


# Setup Docker
if [ -x "$(command -v docker)" ]; then
 echo "Docker already installed"
else
    echo "Installing Docker"
    mkdir -m 0755 -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update
    apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin -y
    docker run hello-world
fi

# Setup Docker Compose as root user
sudo su - <<DOCKER_COMPOSE_INSTALL
if docker compose version; then
 echo "Docker Compose already installed"
else
 echo "Installing Docker Compose"
 DOCKER_CONFIG=${DOCKER_CONFIG:-\$HOME/.docker}
 mkdir -p \$DOCKER_CONFIG/cli-plugins
 curl -SL https://github.com/docker/compose/releases/download/v2.17.0/docker-compose-linux-${architecture} -o \$DOCKER_CONFIG/cli-plugins/docker-compose
 chmod +x \$DOCKER_CONFIG/cli-plugins/docker-compose
fi
DOCKER_COMPOSE_INSTALL


# Setup GreenGrass Core
echo "Downloading and installing Greengrass Core"
if ! curl -s "https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-${greengrass_version}.zip" > "greengrass-${greengrass_version}.zip"; then
    echo "ERROR: Failed to download Greengrass"
    exit 1
fi

if ! unzip "greengrass-${greengrass_version}.zip" -d GreengrassInstaller; then
    echo "ERROR: Failed to extract Greengrass"
    exit 1
fi
rm "greengrass-${greengrass_version}.zip"

java -jar ./GreengrassInstaller/lib/Greengrass.jar --version

# Create IoT thing
# Replace aws-region, thing-name, thing-group-name and etc with your desird value
# If it fails with "The role with name GreengrassV2TokenExchangeRole cannot be found", rerun the command
echo "Provisioning Greengrass Core Device..."
if ! java -Droot="/aws_dda/greengrass/v2" -Dlog.store=FILE -jar ./GreengrassInstaller/lib/Greengrass.jar \
    --aws-region ${aws_region} \
    --thing-name ${thing_name} \
    --thing-group-name DDA_transition_EC2_Group \
    --thing-policy-name GreengrassV2IoTThingPolicy \
    --tes-role-name GreengrassV2TokenExchangeRole \
    --tes-role-alias-name GreengrassCoreTokenExchangeRoleAlias \
    --component-default-user ggc_user:ggc_group \
    --setup-system-service true \
    --provision true; then
    echo "ERROR: Greengrass provisioning failed!"
    echo "If you see 'GreengrassV2TokenExchangeRole cannot be found', rerun the command."
    exit 1
fi

# Attach DDA Portal Component Access Policy to GreengrassV2TokenExchangeRole
# This allows the device to download component artifacts from the Portal Account's S3 bucket
echo "Attaching DDA Portal Component Access Policy to GreengrassV2TokenExchangeRole..."
DDA_POLICY_ARN="arn:aws:iam::$(aws sts get-caller-identity --query 'Account' --output text):policy/DDAPortalComponentAccessPolicy"
if aws iam get-policy --policy-arn "$DDA_POLICY_ARN" 2>/dev/null; then
    if aws iam attach-role-policy --role-name GreengrassV2TokenExchangeRole --policy-arn "$DDA_POLICY_ARN" 2>/dev/null; then
        echo "DDAPortalComponentAccessPolicy attached to GreengrassV2TokenExchangeRole"
    else
        echo "WARNING: Could not attach DDAPortalComponentAccessPolicy. You may need to attach it manually:"
        echo "  aws iam attach-role-policy --role-name GreengrassV2TokenExchangeRole --policy-arn \"$DDA_POLICY_ARN\""
    fi
else
    echo "WARNING: DDAPortalComponentAccessPolicy not found. Deploy UseCaseAccountStack first, then attach manually:"
    echo "  aws iam attach-role-policy --role-name GreengrassV2TokenExchangeRole --policy-arn \"$DDA_POLICY_ARN\""
fi

# Tag the Greengrass Core Device for portal discovery
# Note: We tag the Greengrass Core Device, not the IoT Thing
echo "Tagging Greengrass Core Device for portal discovery..."
# Get AWS account ID
aws_account_id=$(aws sts get-caller-identity --query 'Account' --output text 2>/dev/null || echo "")
if [ -n "$aws_account_id" ]; then
    # Construct the Greengrass Core Device ARN
    gg_core_arn="arn:aws:greengrass:${aws_region}:${aws_account_id}:coreDevices:${thing_name}"
    echo "Tagging Greengrass Core Device ARN: $gg_core_arn"
    
    # Wait a moment for Greengrass to register the core device
    echo "Waiting for Greengrass Core Device to be registered..."
    sleep 10
    
    # Tag the Greengrass Core Device
    if aws greengrassv2 tag-resource --resource-arn "$gg_core_arn" --tags "dda-portal:managed=true" --region ${aws_region} 2>/dev/null; then
        echo "Greengrass Core Device tagged with dda-portal:managed=true"
    else
        echo "WARNING: Could not tag Greengrass Core Device. The device may not appear in the portal."
        echo "Please tag manually after Greengrass is fully started:"
        echo "  aws greengrassv2 tag-resource --resource-arn \"$gg_core_arn\" --tags \"dda-portal:managed=true\" --region ${aws_region}"
    fi
else
    echo "Warning: Could not get AWS account ID for tagging"
fi

# Add ggc_user to a group that allows access to GPU and driver
usermod -aG video ggc_user
usermod -aG dda_system_group ggc_user
usermod -aG ggc_group dda_system_user

# Copy certificates
echo "Copying certificates"
cp "${dda_greengrass_root_folder}/thingCert.crt" "${dda_greengrass_root_folder}/device.pem.crt"
cp "${dda_greengrass_root_folder}/privKey.key" "${dda_greengrass_root_folder}/private.pem.key"
cp "${dda_greengrass_root_folder}/rootCA.pem" "${dda_greengrass_root_folder}/AmazonRootCA1.pem"

# Manage permissions for customer data
if [ -d $dda_root_folder ] ; then
    chown dda_system_user:dda_system_group $dda_root_folder
    chmod 775 $dda_root_folder
fi

# Manage permissions for DDA directories
dda_greengrass_dir="${dda_root_folder}/greengrass"
for directory in `find ${dda_root_folder}/ -maxdepth 1 -mindepth 1 -type d`
do
    # user directories
    if [ $directory != $dda_greengrass_dir ] ; then
        chown -R dda_admin_user:dda_admin_group $directory
        chmod -R 770 $directory
    fi
done

echo "Device bootstrapped successfully"
