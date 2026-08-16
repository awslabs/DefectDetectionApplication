#!/bin/bash
#
# Install Nvidia CSI Camera Capture Service
#
# Self-gated on the provisioning-time opt-in marker (spec:
# csi-nvargus-optional, Decision 1): the recipes invoke this script
# unconditionally on every arm64 deployment, so the script itself converges
# the device to the marker's state — marker absent -> capture service
# disabled+inactive; marker present -> capture service refreshed and running.
# The Error(89) watchdog is installed on ALL Jetson targets regardless of the
# marker (Decision 2): detection is journal-based and cannot false-positive
# on a healthy device, and the degraded state can arise from ANY nvargus
# contact, not just the capture service.
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="nvidia-csi-capture.service"
CAPTURE_SCRIPT="nvidia_csi_capture.sh"

# Env-overridable for the host-side behavioral test suite only (the tests
# cannot create the real marker path on a dev host); every production caller
# leaves it unset, so the default is identical to the hardcoded path.
CSI_OPTIN_MARKER="${CSI_OPTIN_MARKER:-/aws_dda/system/csi_camera_optin}"
WATCHDOG_SCRIPT="nvargus_error89_watchdog.sh"
WATCHDOG_SERVICE="nvargus-error89-watchdog.service"
WATCHDOG_TIMER="nvargus-error89-watchdog.timer"

# --- Error(89) watchdog: installed on ALL Jetson targets (opt-in or not) ---
# Detection is journal-based and cannot false-positive on a healthy device;
# the degraded state can arise from ANY nvargus contact, not just the capture
# service (spec: csi-nvargus-optional, Decision 2).
WATCHDOG_ARTIFACTS_PRESENT=true
for artifact in "$WATCHDOG_SCRIPT" "$WATCHDOG_SERVICE" "$WATCHDOG_TIMER"; do
    if [ ! -f "$SCRIPT_DIR/$artifact" ]; then
        echo "WARNING: watchdog artifact $artifact not found in $SCRIPT_DIR — skipping watchdog install"
        WATCHDOG_ARTIFACTS_PRESENT=false
    fi
done
if [ "$WATCHDOG_ARTIFACTS_PRESENT" = true ]; then
    echo "Installing nvargus Error(89) watchdog..."
    mkdir -p /aws_dda/system
    cp "$SCRIPT_DIR/$WATCHDOG_SCRIPT" /aws_dda/system/
    chmod +x /aws_dda/system/$WATCHDOG_SCRIPT
    cp "$SCRIPT_DIR/$WATCHDOG_SERVICE" /etc/systemd/system/
    cp "$SCRIPT_DIR/$WATCHDOG_TIMER" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now nvargus-error89-watchdog.timer
    echo "nvargus Error(89) watchdog timer enabled"
fi

# --- CSI capture service: gated on the provisioning-time opt-in marker ---
if [ ! -f "$CSI_OPTIN_MARKER" ]; then
    echo "CSI camera not opted in ($CSI_OPTIN_MARKER absent) — ensuring capture service is off"
    systemctl disable --now nvidia-csi-capture.service 2>/dev/null || true
    echo "nvidia-csi-capture.service disabled (provision with ENABLE_CSI_CAMERA=1 to opt in)"
    exit 0
fi

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
