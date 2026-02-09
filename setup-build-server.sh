#!/bin/bash

# Setup build server for DDA development
# Installs dependencies and configures the build environment

set -e

echo "Setting up build server..."

# Install system dependencies
sudo apt-get update
sudo apt-get install -y \
    python3 \
    python3-pip \
    nodejs \
    npm \
    docker.io \
    git

# Install AWS CLI
pip3 install awscli

# Install CDK
npm install -g aws-cdk

# Install GDK for Greengrass component development
pip3 install aws-greengrass-core-sdk

echo "Build server setup complete!"
