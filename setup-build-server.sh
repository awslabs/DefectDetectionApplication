#!/bin/bash

# Setup build server for DDA development
# Installs dependencies and configures the build environment

set -e

VERBOSE="${VERBOSE:-0}"
LOG_FILE="/tmp/setup-build-$(date +%s).log"
ERRORS=()
WARNINGS=()

# Helper function to run commands with logging
run_cmd() {
    local cmd="$@"
    if [ "$VERBOSE" = "1" ]; then
        echo "[RUN] $cmd"
        eval "$cmd" | tee -a "$LOG_FILE"
    else
        echo "[RUN] $cmd"
        if ! eval "$cmd" >> "$LOG_FILE" 2>&1; then
            ERRORS+=("Failed: $cmd")
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

# Trap errors and show summary
trap 'show_summary' EXIT

show_summary() {
    echo ""
    echo "=========================================="
    if [ ${#ERRORS[@]} -eq 0 ] && [ ${#WARNINGS[@]} -eq 0 ]; then
        echo "✅ Build server setup complete!"
    elif [ ${#ERRORS[@]} -eq 0 ]; then
        echo "✅ Build server setup complete (with warnings)"
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
        echo "  VERBOSE=1 $0"
    fi
    
    if [ ${#ERRORS[@]} -eq 0 ] && [ ${#WARNINGS[@]} -eq 0 ]; then
        echo ""
        echo "Next steps:"
        echo "  1. Verify Docker access: docker ps"
        echo "  2. Build component: ./gdk-component-build-and-publish.sh"
    fi
    
    echo ""
    
    if [ ${#ERRORS[@]} -gt 0 ]; then
        return 1
    fi
}

echo "Setting up build server..."
echo "Log file: $LOG_FILE"
echo ""

echo "▶ Updating package manager..."
run_cmd "sudo apt-get update" || true

echo "▶ Installing system dependencies..."
run_cmd "sudo apt-get install -y python3 python3-pip nodejs npm docker.io docker-compose git curl" || add_warning "Some system packages failed to install"

echo "▶ Starting Docker daemon..."
if run_cmd "sudo systemctl start docker"; then
    echo "✓ Docker daemon started"
else
    add_error "Failed to start Docker daemon"
fi

echo "▶ Configuring Docker permissions..."
# Create docker group if it doesn't exist
if ! getent group docker > /dev/null; then
    if run_cmd "sudo groupadd docker"; then
        echo "✓ Created docker group"
    else
        add_error "Failed to create docker group"
    fi
else
    echo "✓ Docker group already exists"
fi

# Add user to docker group
if run_cmd "sudo usermod -aG docker $USER"; then
    echo "✓ Added $USER to docker group"
else
    add_error "Failed to add user to docker group"
fi

# Fix Docker socket permissions
if run_cmd "sudo chmod 666 /var/run/docker.sock"; then
    echo "✓ Fixed Docker socket permissions"
else
    add_warning "Could not fix Docker socket permissions"
fi

# Reload systemd daemon
if run_cmd "sudo systemctl daemon-reload"; then
    echo "✓ Reloaded systemd daemon"
else
    add_warning "Could not reload systemd daemon"
fi

echo "▶ Verifying Docker installation..."
if sudo docker ps > /dev/null 2>&1; then
    echo "✓ Docker is accessible"
else
    add_warning "Docker may not be fully accessible (may need to restart or log in again)"
fi

echo "▶ Installing Python 3.9..."
if ! command -v python3.9 >/dev/null 2>&1; then
    UBUNTU_VERSION=$(lsb_release -rs)
    if [ "$UBUNTU_VERSION" = "18.04" ]; then
        echo "  Building Python 3.9 from source (Ubuntu 18.04)..."
        run_cmd "sudo apt-get install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev libssl-dev libreadline-dev libffi-dev wget" || add_warning "Failed to install build dependencies"
        
        if [ ! -d /tmp/Python-3.9.16 ]; then
            run_cmd "cd /tmp && wget https://www.python.org/ftp/python/3.9.16/Python-3.9.16.tgz" || add_error "Failed to download Python 3.9"
            run_cmd "cd /tmp && tar -xf Python-3.9.16.tgz" || add_error "Failed to extract Python 3.9"
        fi
        
        run_cmd "cd /tmp/Python-3.9.16 && ./configure --enable-optimizations" || add_error "Failed to configure Python 3.9"
        run_cmd "cd /tmp/Python-3.9.16 && make -j 8" || add_error "Failed to build Python 3.9"
        run_cmd "cd /tmp/Python-3.9.16 && sudo make altinstall" || add_error "Failed to install Python 3.9"
    else
        echo "  Installing Python 3.9 from PPA..."
        run_cmd "sudo add-apt-repository ppa:deadsnakes/ppa -y" || add_warning "Failed to add deadsnakes PPA"
        run_cmd "sudo apt-get update" || add_warning "Failed to update package manager"
        run_cmd "sudo apt-get install -y python3.9 python3.9-venv python3.9-dev" || add_error "Failed to install Python 3.9"
    fi
    
    # Set as default
    run_cmd "sudo update-alternatives --install /usr/local/bin/python3 python3 /usr/local/bin/python3.9 1" || add_warning "Failed to set Python 3.9 as default"
    echo "✓ Python 3.9 installed"
else
    echo "✓ Python 3.9 already installed"
fi

echo "▶ Configuring PATH..."
if ! grep -q 'export PATH="$HOME/.local/bin:$PATH"' ~/.bashrc; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
    echo "✓ Added ~/.local/bin to PATH"
else
    echo "✓ PATH already configured"
fi
export PATH="$HOME/.local/bin:$PATH"

echo "▶ Installing AWS CLI..."
if ! command -v aws >/dev/null 2>&1; then
    run_cmd "sudo pip3 install awscli" || add_warning "Failed to install AWS CLI"
else
    echo "✓ AWS CLI already installed"
fi

echo "▶ Installing AWS CDK (optional for build server)..."
if ! command -v cdk >/dev/null 2>&1; then
    run_cmd "sudo npm install -g aws-cdk" || add_warning "Failed to install AWS CDK (optional - not required for component builds)"
else
    echo "✓ AWS CDK already installed"
fi

echo "▶ Installing AWS Greengrass Development Kit (GDK)..."
if ! command -v gdk >/dev/null 2>&1; then
    run_cmd "sudo python3 -m pip install git+https://github.com/aws-greengrass/aws-greengrass-gdk-cli.git@v1.6.2" || add_warning "Failed to install GDK CLI from GitHub"
else
    echo "✓ GDK CLI already installed"
fi

echo "▶ Building edgemlsdk Docker image..."
if [ -d "src/edgemlsdk" ]; then
    run_cmd "docker build -t edgemlsdk:latest src/edgemlsdk/" || add_warning "Failed to build edgemlsdk Docker image"
    echo "✓ edgemlsdk Docker image built"
else
    add_warning "src/edgemlsdk directory not found - skipping Docker image build"
fi

echo ""
echo "✓ Build server setup complete!"
