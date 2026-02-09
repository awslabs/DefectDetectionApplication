#!/bin/bash

# Build and publish Greengrass components using GDK
# This script builds components and publishes them to the Greengrass component repository

set -e

VERBOSE="${VERBOSE:-0}"
LOG_FILE="/tmp/gdk-build-$(date +%s).log"
ERRORS=()

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

# Build and publish component using gdk-config.json
echo "▶ Building component..."
if run_cmd "gdk component build"; then
    echo "✓ Build successful"
else
    echo "✗ Build failed"
    ERRORS+=("Failed to build component")
fi

echo ""
echo "▶ Publishing component..."
if run_cmd "gdk component publish"; then
    echo "✓ Publish successful"
else
    echo "✗ Publish failed"
    ERRORS+=("Failed to publish component")
fi
echo ""

if [ ${#ERRORS[@]} -eq 0 ]; then
    echo "✅ Component published successfully!"
fi
