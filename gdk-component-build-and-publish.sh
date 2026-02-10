#!/bin/bash

# Build and publish Greengrass components using GDK
# This script builds components and publishes them to the Greengrass component repository

set -e

VERBOSE="${VERBOSE:-0}"
LOG_FILE="${LOG_FILE:-/tmp/gdk-build-$(date +%s).log}"
ERRORS=()

# Export LOG_FILE so build-custom.sh can use it
export LOG_FILE

# Spinner animation frames
SPINNER=( '⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏' )

# Helper function to show progress with task name
show_progress() {
    local task="$1"
    local pid=$2
    local i=0
    
    while kill -0 $pid 2>/dev/null; do
        printf "\r  ${SPINNER[$((i % ${#SPINNER[@]}))]]} $task"
        ((i++))
        sleep 0.1
    done
    wait $pid
    local exit_code=$?
    printf "\r  ✓ $task\n"
    return $exit_code
}

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

# Trap errors and show summary
trap 'show_error_summary' EXIT

show_error_summary() {
    if [ ${#ERRORS[@]} -gt 0 ]; then
        echo ""
        echo "❌ ERRORS ENCOUNTERED:"
        printf '%s\n' "${ERRORS[@]}"
        echo ""
        echo "📋 Full log: $LOG_FILE"
        echo "Run with VERBOSE=1 to see detailed output:"
        echo "  VERBOSE=1 $0"
        return 1
    fi
}

echo "Building and publishing Greengrass components..."
echo "Log file: $LOG_FILE"
echo ""

# Get architecture and determine recipe file
echo "▶ Detecting system architecture..."
ARCH=$(uname -m)
case $ARCH in
    x86_64)
        RECIPE_FILE="recipe-amd64.yaml"
        COMPONENT_NAME="aws.edgeml.dda.LocalServer.amd64"
        echo "✓ Architecture: x86_64 (amd64)"
        ;;
    aarch64)
        RECIPE_FILE="recipe-arm64.yaml"
        COMPONENT_NAME="aws.edgeml.dda.LocalServer.arm64"
        echo "✓ Architecture: aarch64 (arm64)"
        ;;
    *)
        ERRORS+=("Unsupported architecture: $ARCH")
        echo "❌ Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

echo "  Component name: $COMPONENT_NAME"
echo "  Recipe file: $RECIPE_FILE"
echo ""

# Verify recipe file exists
echo "▶ Verifying recipe file..."
if [ ! -f "$RECIPE_FILE" ]; then
    ERRORS+=("Recipe file not found: $RECIPE_FILE")
    echo "❌ Recipe file not found: $RECIPE_FILE"
    exit 1
fi
echo "✓ Recipe file found"
echo ""

# Copy recipe to root
echo "▶ Preparing recipe..."
cp "$RECIPE_FILE" recipe.yaml
echo "✓ Recipe copied to recipe.yaml"
echo ""

# Create gdk-config.json with architecture-specific component name
echo "▶ Generating GDK configuration..."
cat > gdk-config.json << EOF
{
  "component": {
    "${COMPONENT_NAME}": {
      "author": "Amazon",
      "version": "NEXT_PATCH",
      "build": {
        "build_system": "custom",
        "custom_build_command": [
          "bash",
          "build-custom.sh",
          "${COMPONENT_NAME}",
          "NEXT_PATCH"
        ]
      },
      "publish": {
        "bucket": "dda-component",
        "region": "us-east-1"
      }
    }
  },
  "gdk_version": "1.0.0"
}
EOF
echo "✓ GDK configuration generated"
echo ""

# Clean GDK cache and build directories
echo "▶ Cleaning build directories..."
rm -rf greengrass-build/ 2>/dev/null || true
rm -rf .gdk/ 2>/dev/null || true
echo "✓ Build directories cleaned"
echo ""

# Build and publish component using gdk-config.json
echo "▶ Building component..."
if [ "$VERBOSE" = "1" ]; then
    # In verbose mode, show all output
    if run_cmd "gdk component build"; then
        echo "✓ Build successful"
    else
        echo "✗ Build failed"
        ERRORS+=("Failed to build component")
    fi
else
    # In normal mode, show animated spinner with task name
    (gdk component build >> "$LOG_FILE" 2>&1) &
    if show_progress "Building Docker images..." $!; then
        echo "✓ Build successful"
    else
        echo "✗ Build failed"
        ERRORS+=("Failed to build component")
    fi
fi

echo ""
echo "▶ Publishing component..."
if [ "$VERBOSE" = "1" ]; then
    # In verbose mode, show all output
    if run_cmd "gdk component publish"; then
        echo "✓ Publish successful"
    else
        echo "✗ Publish failed"
        ERRORS+=("Failed to publish component")
    fi
else
    # In normal mode, show animated spinner with task name
    (gdk component publish >> "$LOG_FILE" 2>&1) &
    if show_progress "Uploading to AWS..." $!; then
        echo "✓ Publish successful"
    else
        echo "✗ Publish failed"
        ERRORS+=("Failed to publish component")
    fi
fi
echo ""

if [ ${#ERRORS[@]} -eq 0 ]; then
    echo "✅ Component ${COMPONENT_NAME} built and published successfully!"
fi
