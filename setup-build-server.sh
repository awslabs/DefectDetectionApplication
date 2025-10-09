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
sudo snap install docker-compose

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
python3.9 -m pip install awscli
python3.9 -m pip install git+https://github.com/aws-greengrass/aws-greengrass-gdk-cli.git

# Verify AWS CLI installation
aws --version

# Fix Docker permissions
sudo systemctl start docker
sudo usermod -aG docker $USER
sudo chmod 666 /var/run/docker.sock

# Verify Docker is working
docker ps

# Note: /aws_dda directories are only needed on deployment targets, not on build servers

echo "Build server setup complete!"
echo "To build the DDA component, run:"
echo "./gdk-compnent-build-and-publish.sh"
