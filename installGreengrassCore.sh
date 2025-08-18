#!/bin/bash -e


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
apt-get install curl ca-certificates gnupg lsb-release unzip -y


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
echo "Installing Python3.8"
apt-get install python3.8 -y
apt-get install python3.8-venv -y
apt-get install libpython3.8-dev -y


echo "Installing Pip"
apt-get install python3-pip -y
python3.8 -m pip install --upgrade pip


# Install python3.9-venv for LFV Edge Agent
if command -v python3.9 &>/dev/null; then
 echo "Python 3.9 is available. Installing python3.9-venv package..."
 apt-get install python3.9-venv -y
 echo "python3.9-venv package installed successfully"
else
 echo "Python 3.9 is not available, skip install python3.9-venv"
fi


# Setup Gstreamer
echo "Installing Gstreamer"
apt-get install -y libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
 gstreamer1.0-libav gstreamer1.0-doc gstreamer1.0-tools gstreamer1.0-x gstreamer1.0-alsa gstreamer1.0-gl \
 gstreamer1.0-gtk3 gstreamer1.0-qt5 gstreamer1.0-pulseaudio


# Edge Manager Agent Config
mkdir -p "${dda_greengrass_root_folder}/em_agent/capture_data" \
 "${dda_greengrass_root_folder}/em_agent/local_data" \
 "${dda_greengrass_root_folder}/em_agent/config"
cp edge_manager_agent_config.json "${dda_greengrass_root_folder}/em_agent/config"


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
curl -s "https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-${greengrass_version}.zip" > "greengrass-${greengrass_version}.zip"

unzip "greengrass-${greengrass_version}.zip" -d GreengrassInstaller && rm "greengrass-${greengrass_version}.zip"

java -jar ./GreengrassInstaller/lib/Greengrass.jar --version

# Create IoT thing
# Replace aws-region, thing-name, thing-group-name and etc with your desird value
# If it fails with "The role with name GreengrassV2TokenExchangeRole cannot be found", rerun the command
java -Droot="/aws_dda/greengrass/v2" -Dlog.store=FILE   -jar ./GreengrassInstaller/lib/Greengrass.jar   --aws-region us-west-2   --thing-name DDA_EC2_ARM_c6g --thing-group-name DDA_transition_EC2_Group   --thing-policy-name GreengrassV2IoTThingPolicy   --tes-role-name GreengrassV2TokenExchangeRole   --tes-role-alias-name GreengrassCoreTokenExchangeRoleAlias   --component-default-user ggc_user:ggc_group   --setup-system-service true --provision true

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
