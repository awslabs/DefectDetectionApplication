#!/bin/bash
#
# Nvidia CSI Camera Continuous Capture
# Runs on host and captures images to a directory that Docker can access
#

CAPTURE_DIR="/aws_dda/nvidia-csi-capture"
LATEST_IMAGE="$CAPTURE_DIR/latest.jpg"
TEMP_IMAGE="$CAPTURE_DIR/temp.jpg"

# Create capture directory
mkdir -p "$CAPTURE_DIR"
chmod 777 "$CAPTURE_DIR"

echo "Starting Nvidia CSI continuous capture..."
echo "Capture directory: $CAPTURE_DIR"

# Continuous capture loop
while true; do
    # Capture to temp file first
    gst-launch-1.0 -q \
        nvarguscamerasrc sensor_id=0 num-buffers=1 ! \
        'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
        nvvidconv ! \
        'video/x-raw,format=BGRx' ! \
        videoconvert ! \
        jpegenc idct-method=2 quality=100 ! \
        filesink location="$TEMP_IMAGE" 2>/dev/null
    
    # Atomically move to latest (prevents reading partial images)
    if [ -f "$TEMP_IMAGE" ]; then
        mv "$TEMP_IMAGE" "$LATEST_IMAGE"
        chmod 666 "$LATEST_IMAGE"
    fi
    
    # Capture rate: ~10 fps (adjust as needed)
    sleep 0.1
done
