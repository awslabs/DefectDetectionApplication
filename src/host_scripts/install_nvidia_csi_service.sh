#!/bin/bash
#
# Install Nvidia CSI Camera Capture Service
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="nvidia-csi-capture.service"
CAPTURE_SCRIPT="nvidia_csi_capture.sh"

echo "Installing Nvidia CSI camera capture service..."

# Copy capture script to system location
sudo mkdir -p /aws_dda/system
sudo cp "$SCRIPT_DIR/$CAPTURE_SCRIPT" /aws_dda/system/
sudo chmod +x /aws_dda/system/$CAPTURE_SCRIPT

# Install systemd service
sudo cp "$SCRIPT_DIR/$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_FILE
sudo systemctl start $SERVICE_FILE

echo "Service installed and started"
echo "Check status with: sudo systemctl status nvidia-csi-capture"
echo "View logs with: sudo journalctl -u nvidia-csi-capture -f"
