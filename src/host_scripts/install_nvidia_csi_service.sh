#!/bin/bash
#
# Install Nvidia CSI Camera Capture Service
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="nvidia-csi-capture.service"
CAPTURE_SCRIPT="nvidia_csi_capture.sh"

echo "Installing Nvidia CSI camera capture service..."

# Install jq if not present (needed for reading config)
if ! command -v jq &> /dev/null; then
    echo "Installing jq..."
    apt-get update -qq && apt-get install -y -qq jq
fi

# Copy capture script to system location
mkdir -p /aws_dda/system
cp "$SCRIPT_DIR/$CAPTURE_SCRIPT" /aws_dda/system/
chmod +x /aws_dda/system/$CAPTURE_SCRIPT

# Install systemd service
cp "$SCRIPT_DIR/$SERVICE_FILE" /etc/systemd/system/
systemctl daemon-reload
systemctl enable $SERVICE_FILE
systemctl restart $SERVICE_FILE

echo "Service installed and started"
echo "Check status with: sudo systemctl status nvidia-csi-capture"
echo "View logs with: sudo journalctl -u nvidia-csi-capture -f"
