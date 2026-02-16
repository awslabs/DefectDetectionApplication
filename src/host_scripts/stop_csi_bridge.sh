#!/bin/bash
#
# Stop Nvidia CSI Camera Bridge
#

PID_FILE="/tmp/nvidia_csi_bridge.pid"
FIFO_PATH="/tmp/nvidia_csi_fifo"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    echo "Stopping CSI camera bridge (PID: $PID)..."
    kill $PID 2>/dev/null
    rm -f "$PID_FILE"
    echo "Bridge stopped"
else
    echo "CSI camera bridge is not running"
fi

# Cleanup FIFO
rm -f "$FIFO_PATH"

# Kill any lingering gst-launch processes
pkill -f "gst-launch.*nvarguscamerasrc"
