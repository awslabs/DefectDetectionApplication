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
run_cmd "sudo apt-get install -y python3 python3-pip nodejs npm docker.io git curl" || add_warning "Some system packages failed to install"

echo "▶ Installing AWS CLI..."
if ! command -v aws >/dev/null 2>&1; then
    run_cmd "sudo pip3 install awscli" || add_warning "Failed to install AWS CLI"
else
    echo "✓ AWS CLI already installed"
fi

echo "▶ Installing AWS CDK..."
if ! command -v cdk >/dev/null 2>&1; then
    run_cmd "sudo npm install -g aws-cdk" || add_error "Failed to install AWS CDK"
else
    echo "✓ AWS CDK already installed"
fi

echo "▶ Installing AWS Greengrass Development Kit (GDK)..."
if ! command -v gdk >/dev/null 2>&1; then
    run_cmd "sudo python3 -m pip install git+https://github.com/aws-greengrass/aws-greengrass-gdk-cli.git@v1.6.2" || add_warning "Failed to install GDK CLI from GitHub"
else
    echo "✓ GDK CLI already installed"
fi

echo ""
echo "✓ Build server setup complete!"
