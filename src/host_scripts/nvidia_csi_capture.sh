#!/bin/bash
#
# Nvidia CSI Camera Continuous Capture
# Runs on host and captures images to a directory that Docker can access
# Reads gain and exposure settings from config file
#

CAPTURE_DIR="/aws_dda/nvidia-csi-capture"
LATEST_IMAGE="$CAPTURE_DIR/latest.jpg"
TEMP_IMAGE="$CAPTURE_DIR/temp.jpg"
CONFIG_FILE="$CAPTURE_DIR/config.json"

# Create capture directory
mkdir -p "$CAPTURE_DIR"
chmod 777 "$CAPTURE_DIR"

# Create default config if it doesn't exist
if [ ! -f "$CONFIG_FILE" ]; then
    echo '{"gain":1,"exposure":500}' > "$CONFIG_FILE"
    chmod 666 "$CONFIG_FILE"
fi

echo "Starting Nvidia CSI continuous capture..."
echo "Capture directory: $CAPTURE_DIR"
echo "Config file: $CONFIG_FILE"

# Function to read config
read_config() {
    if [ -f "$CONFIG_FILE" ]; then
        GAIN=$(jq -r '.gain // 1' "$CONFIG_FILE" 2>/dev/null || echo "1")
        EXPOSURE=$(jq -r '.exposure // 500' "$CONFIG_FILE" 2>/dev/null || echo "500")
    else
        GAIN=1
        EXPOSURE=500
    fi
}

# Initial config read
read_config
LAST_GAIN=$GAIN
LAST_EXPOSURE=$EXPOSURE

echo "Initial settings - Gain: $GAIN, Exposure: $EXPOSURE"

# Continuous capture loop
while true; do
    # Check if config has changed
    read_config
    if [ "$GAIN" != "$LAST_GAIN" ] || [ "$EXPOSURE" != "$LAST_EXPOSURE" ]; then
        echo "Settings updated - Gain: $GAIN, Exposure: $EXPOSURE"
        LAST_GAIN=$GAIN
        LAST_EXPOSURE=$EXPOSURE
    fi
    
    # Capture with current settings
    # Note: nvarguscamerasrc uses different parameter names
    # gain is "gainrange" and exposure is "exposuretimerange"
    # Values are in format "min max" but we use same value for both
    gst-launch-1.0 -q \
        nvarguscamerasrc sensor_id=0 num-buffers=1 \
        gainrange="$GAIN $GAIN" \
        exposuretimerange="$EXPOSURE $EXPOSURE" ! \
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
