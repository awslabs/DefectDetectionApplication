#!/bin/bash
# setup-build-server.sh - Script to set up DDA build environment

# Exit on error
set -e

echo "Setting up DDA build server environment..."

# Update system packages
sudo apt-get update
sudo apt-get install -y git python3-venv zip software-properties-common

# Install Docker and docker-compose via snap
sudo snap install docker
GROUP_NAME=docker
if getent group "$GROUP_NAME" > /dev/null; then
    echo "Group '$GROUP_NAME' already exists."
else
    echo "Group '$GROUP_NAME' does not exist. Creating it..."
    # Create the group
    sudo groupadd "$GROUP_NAME"
    if [ $? -eq 0 ]; then
        echo "Group '$GROUP_NAME' created successfully."
    else
        echo "Error: Failed to create group '$GROUP_NAME'."
        exit 1
    fi
fi
sudo usermod -aG docker $USER
source ~/.bashrc
# Install Python 3.9
UBUNTU_VERSION=$(lsb_release -rs)
if [ "$UBUNTU_VERSION" = "18.04" ]; then
  # Install from source for 18.04
  sudo apt-get install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev libssl-dev libreadline-dev libffi-dev wget
  cd /tmp
  wget https://www.python.org/ftp/python/3.9.16/Python-3.9.16.tgz
  tar -xf Python-3.9.16.tgz
  cd Python-3.9.16
  ./configure --enable-optimizations
  make -j 8
  sudo make altinstall
  sudo update-alternatives --install /usr/local/bin/python3 python3 /usr/local/bin/python3.9 1
else
  # Install from PPA for other versions
  sudo add-apt-repository ppa:deadsnakes/ppa -y
  sudo apt-get update
  sudo apt-get install -y python3.9 python3.9-venv python3.9-dev
  sudo update-alternatives --install /usr/local/bin/python3 python3 /usr/bin/python3.9 1
fi

echo "Installing Pip"
sudo apt-get install python3-pip -y
python3.9 -m pip install --upgrade pip
python3.9 -m pip install --force-reinstall requests==2.32.3
python3.9 -m pip install protobuf


# Install AWS CLI v2 and GDK
python3.9 -m pip install git+https://github.com/aws-greengrass/aws-greengrass-gdk-cli.git
sudo snap install aws-cli --classic
# Add ~/.local/bin to PATH for GDK
if ! grep -q 'export PATH="$HOME/.local/bin:$PATH"' ~/.bashrc; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
fi
export PATH="$HOME/.local/bin:$PATH"

# Verify AWS CLI installation
aws --version

# Fix Docker permissions
sudo usermod -aG docker $USER
sudo chmod 666 /var/run/docker.sock
sudo systemctl daemon-reload
# Verify Docker is working
sudo docker ps

# Note: /aws_dda directories are only needed on deployment targets, not on build servers

echo "Build server setup complete!"
echo "To build the DDA component, run:"
echo "./gdk-component-build-and-publish.sh"
