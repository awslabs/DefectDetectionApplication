#!/bin/bash
# setup-build-server.sh - Script to set up DDA build environment

# Exit on error
set -e

echo "Setting up DDA build server environment..."

# Update system packages
sudo apt-get update
sudo apt-get install -y git docker.io docker-compose python3 python3-venv python3-pip zip

# Install AWS CLI
sudo snap install aws-cli --classic

# Verify AWS CLI installation
aws --version

# Fix Docker permissions
sudo systemctl start docker
sudo usermod -aG docker $USER
sudo chmod 666 /var/run/docker.sock

# Verify Docker is working
docker ps

# Note: /aws_dda directories are only needed on deployment targets, not on build servers

# Install GDK in a virtual environment
python3 -m venv ~/gdk-venv
source ~/gdk-venv/bin/activate
pip install git+https://github.com/aws-greengrass/aws-greengrass-gdk-cli.git
echo 'source ~/gdk-venv/bin/activate' >> ~/.bashrc

# Create symbolic link for GDK
sudo ln -sf ~/gdk-venv/bin/gdk /usr/local/bin/gdk

echo "Build server setup complete!"
echo "To build the DDA component, run:"
echo "  cd /path/to/DDA-OpenSource"
echo "  gdk component build"
echo "  gdk component publish"