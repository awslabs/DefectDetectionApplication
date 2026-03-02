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
    echo '{"gain":4,"exposure":5000000}' > "$CONFIG_FILE"
    chmod 666 "$CONFIG_FILE"
    echo "Created default config file"
fi

echo "Starting Nvidia CSI continuous capture..."
echo "Capture directory: $CAPTURE_DIR"
echo "Config file: $CONFIG_FILE"

# Check if jq is available
if ! command -v jq &> /dev/null; then
    echo "ERROR: jq is not installed. Installing..."
    apt-get update -qq && apt-get install -y -qq jq || {
        echo "ERROR: Failed to install jq. Gain/exposure settings will not work."
        echo "Using default values: gain=4, exposure=5000000"
        GAIN=4
        EXPOSURE=5000000
        USE_JQ=false
    }
else
    USE_JQ=true
fi

# Function to read config
read_config() {
    if [ "$USE_JQ" = "true" ] && [ -f "$CONFIG_FILE" ]; then
        GAIN=$(jq -r '.gain // 4' "$CONFIG_FILE" 2>/dev/null || echo "4")
        EXPOSURE=$(jq -r '.exposure // 5000000' "$CONFIG_FILE" 2>/dev/null || echo "5000000")
        
        # Read crop settings
        CROP_TOP=$(jq -r '.crop.top // 0' "$CONFIG_FILE" 2>/dev/null || echo "0")
        CROP_BOTTOM=$(jq -r '.crop.bottom // 0' "$CONFIG_FILE" 2>/dev/null || echo "0")
        CROP_LEFT=$(jq -r '.crop.left // 0' "$CONFIG_FILE" 2>/dev/null || echo "0")
        CROP_RIGHT=$(jq -r '.crop.right // 0' "$CONFIG_FILE" 2>/dev/null || echo "0")
    else
        GAIN=4
        EXPOSURE=5000000
        CROP_TOP=0
        CROP_BOTTOM=0
        CROP_LEFT=0
        CROP_RIGHT=0
    fi
}

# Initial config read
read_config
LAST_GAIN=$GAIN
LAST_EXPOSURE=$EXPOSURE
LAST_CROP="$CROP_TOP,$CROP_BOTTOM,$CROP_LEFT,$CROP_RIGHT"

echo "Initial settings - Gain: $GAIN, Exposure: $EXPOSURE, Crop: top=$CROP_TOP bottom=$CROP_BOTTOM left=$CROP_LEFT right=$CROP_RIGHT (jq available: $USE_JQ)"

# Continuous capture loop
CAPTURE_COUNT=0
while true; do
    # Check if config has changed (only if jq is available)
    if [ "$USE_JQ" = "true" ]; then
        read_config
        CURRENT_CROP="$CROP_TOP,$CROP_BOTTOM,$CROP_LEFT,$CROP_RIGHT"
        if [ "$GAIN" != "$LAST_GAIN" ] || [ "$EXPOSURE" != "$LAST_EXPOSURE" ] || [ "$CURRENT_CROP" != "$LAST_CROP" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Settings updated - Gain: $GAIN, Exposure: $EXPOSURE, Crop: top=$CROP_TOP bottom=$CROP_BOTTOM left=$CROP_LEFT right=$CROP_RIGHT"
            LAST_GAIN=$GAIN
            LAST_EXPOSURE=$EXPOSURE
            LAST_CROP=$CURRENT_CROP
            CAPTURE_COUNT=0  # Reset counter to log next capture
        fi
    fi
    
    # Log every 50th capture to avoid spam
    if [ $((CAPTURE_COUNT % 50)) -eq 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Capturing with Gain=$GAIN, Exposure=$EXPOSURE (capture #$CAPTURE_COUNT)"
    fi
    CAPTURE_COUNT=$((CAPTURE_COUNT + 1))
    
    # Capture with current settings
    # nvarguscamerasrc parameters for manual exposure control:
    # - aeantibanding: Set to 0 to disable auto-exposure antibanding  
    # - wbmode: Set to 0 to disable auto white balance
    # - exposuretimerange: "min max" in nanoseconds (safe range: 50000 to 30000000)
    # - gainrange: "min max" for analog gain (range: 1.0 to 10.625)
    #
    # IMPORTANT: The extreme values (13000 and 683709000) are rejected as invalid
    # Use safe middle range values that the camera accepts
    
    # Build videocrop parameters if cropping is enabled
    CROP_PARAMS=""
    if [ "$CROP_TOP" -gt 0 ] || [ "$CROP_BOTTOM" -gt 0 ] || [ "$CROP_LEFT" -gt 0 ] || [ "$CROP_RIGHT" -gt 0 ]; then
        CROP_PARAMS="videocrop top=$CROP_TOP bottom=$CROP_BOTTOM left=$CROP_LEFT right=$CROP_RIGHT !"
    fi
    
    gst-launch-1.0 -q \
        nvarguscamerasrc sensor_id=0 num-buffers=1 \
        aeantibanding=0 \
        wbmode=0 \
        exposuretimerange="$EXPOSURE $EXPOSURE" \
        gainrange="$GAIN $GAIN" ! \
        'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
        nvvidconv ! \
        'video/x-raw,format=BGRx' ! \
        videoconvert ! \
        $CROP_PARAMS \
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
