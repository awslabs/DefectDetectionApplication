#!/bin/bash
#
# Nvidia CSI Camera Continuous Capture (persistent-pipeline supervisor)
# Runs on host and stages images to a directory that Docker can access
# Reads gain and exposure settings from config file
#
# ONE long-lived nvarguscamerasrc pipeline (a single persistent Argus
# session) replaces the previous per-frame single-shot capture loop.
# The supervisor polls config.json and restarts the pipeline exactly once
# per effective settings change; a pipeline death is logged loudly (stderr
# is NOT discarded — failures are visible in the service journal) and the
# pipeline is relaunched after a backoff. systemd Restart=always remains
# the outer supervisor for this script itself.
#

CAPTURE_DIR="/aws_dda/nvidia-csi-capture"
# Test override hook: the behavioral suites drive this real script against
# a temp directory with a stub gst-launch-1.0 on PATH.
CAPTURE_DIR="${CSI_CAPTURE_DIR:-$CAPTURE_DIR}"
LATEST_IMAGE="$CAPTURE_DIR/latest.jpg"
CONFIG_FILE="$CAPTURE_DIR/config.json"

# Tunable supervisor constants (env-overridable for tests)
FRAMERATE_NUM="${FRAMERATE_NUM:-2}"                 # staged-frame cadence target (fps)
CONFIG_POLL_INTERVAL="${CONFIG_POLL_INTERVAL:-1}"   # seconds between config.json polls
RESTART_BACKOFF="${RESTART_BACKOFF:-5}"             # seconds before relaunch after a pipeline failure
STAGE_PATTERN="$CAPTURE_DIR/stage_%05d.jpg"         # multifilesink staging pattern

# Create capture directory
mkdir -p "$CAPTURE_DIR"
chmod 777 "$CAPTURE_DIR"

# Create default config if it doesn't exist
if [ ! -f "$CONFIG_FILE" ]; then
    echo '{"gain":4,"exposure":5000000}' > "$CONFIG_FILE"
    chmod 666 "$CONFIG_FILE"
    echo "Created default config file"
fi

echo "Starting Nvidia CSI continuous capture (persistent pipeline supervisor)..."
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

# Build videocrop parameters if cropping is enabled
build_crop_params() {
    CROP_PARAMS=""
    if [ "$CROP_TOP" -gt 0 ] || [ "$CROP_BOTTOM" -gt 0 ] || [ "$CROP_LEFT" -gt 0 ] || [ "$CROP_RIGHT" -gt 0 ]; then
        CROP_PARAMS="videocrop top=$CROP_TOP bottom=$CROP_BOTTOM left=$CROP_LEFT right=$CROP_RIGHT !"
    fi
}

GST_PID=""

# Launch the ONE persistent pipeline (a single long-lived Argus session,
# no buffer-count limit). Backgrounded: the supervisor loop owns lifecycle.
#
# nvarguscamerasrc parameters for manual exposure control:
# - aeantibanding: Set to 0 to disable auto-exposure antibanding
# - wbmode: Set to 0 to disable auto white balance
# - exposuretimerange: "min max" in nanoseconds (safe range: 50000 to 30000000)
# - gainrange: "min max" for analog gain (range: 1.0 to 10.625)
#
# IMPORTANT: The extreme values (13000 and 683709000) are rejected as invalid
# Use safe middle range values that the camera accepts
launch_pipeline() {
    build_crop_params
    # Clear stale stage files so the stager only promotes frames produced
    # by the pipeline being launched (never pre-change frames)
    rm -f "$CAPTURE_DIR"/stage_*.jpg
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Launching persistent pipeline - Gain: $GAIN, Exposure: $EXPOSURE, Crop: top=$CROP_TOP bottom=$CROP_BOTTOM left=$CROP_LEFT right=$CROP_RIGHT"
    gst-launch-1.0 -e \
        nvarguscamerasrc sensor_id=0 \
        aeantibanding=0 \
        wbmode=0 \
        exposuretimerange="$EXPOSURE $EXPOSURE" \
        gainrange="$GAIN $GAIN" ! \
        'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
        nvvidconv ! \
        'video/x-raw,format=BGRx' ! \
        videoconvert ! \
        videorate drop-only=true ! "video/x-raw,framerate=${FRAMERATE_NUM}/1" ! \
        $CROP_PARAMS \
        jpegenc idct-method=2 quality=100 ! \
        multifilesink location="$STAGE_PATTERN" max-files=3 &
    GST_PID=$!
}

stop_pipeline() {
    if [ -n "$GST_PID" ] && kill -0 "$GST_PID" 2>/dev/null; then
        kill -TERM "$GST_PID" 2>/dev/null
        wait "$GST_PID" 2>/dev/null
    fi
    GST_PID=""
}

# Promote the newest COMPLETE stage file to latest.jpg. multifilesink may
# still be writing the newest file, so pick the newest-but-one (stage index
# N is complete once N+1 exists). Atomic mv on the same filesystem preserves
# the never-a-partial-read consumer contract exactly as before.
stage_frames() {
    local candidate
    candidate=$(ls -1t "$CAPTURE_DIR"/stage_*.jpg 2>/dev/null | sed -n '2p')
    if [ -n "$candidate" ] && [ -s "$candidate" ]; then
        mv -f "$candidate" "$LATEST_IMAGE"
        chmod 666 "$LATEST_IMAGE"
    fi
}

trap 'stop_pipeline' EXIT
trap 'exit 0' TERM INT

# Initial config read
read_config
LAST_GAIN=$GAIN
LAST_EXPOSURE=$EXPOSURE
LAST_CROP="$CROP_TOP,$CROP_BOTTOM,$CROP_LEFT,$CROP_RIGHT"

echo "Initial settings - Gain: $GAIN, Exposure: $EXPOSURE, Crop: top=$CROP_TOP bottom=$CROP_BOTTOM left=$CROP_LEFT right=$CROP_RIGHT (jq available: $USE_JQ)"

launch_pipeline

# Supervisor loop: stage frames, poll config.json every
# CONFIG_POLL_INTERVAL, restart the pipeline exactly once per effective
# settings change, relaunch with RESTART_BACKOFF when the pipeline dies.
while true; do
    sleep "$CONFIG_POLL_INTERVAL"

    stage_frames

    # Check if config has changed (only if jq is available); an effective
    # change costs exactly ONE Argus session restart; no-op rewrites of
    # identical values never restart anything
    if [ "$USE_JQ" = "true" ]; then
        read_config
        CURRENT_CROP="$CROP_TOP,$CROP_BOTTOM,$CROP_LEFT,$CROP_RIGHT"
        if [ "$GAIN" != "$LAST_GAIN" ] || [ "$EXPOSURE" != "$LAST_EXPOSURE" ] || [ "$CURRENT_CROP" != "$LAST_CROP" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Settings updated - Gain: $GAIN, Exposure: $EXPOSURE, Crop: top=$CROP_TOP bottom=$CROP_BOTTOM left=$CROP_LEFT right=$CROP_RIGHT - restarting pipeline with new settings"
            LAST_GAIN=$GAIN
            LAST_EXPOSURE=$EXPOSURE
            LAST_CROP=$CURRENT_CROP
            stop_pipeline
            launch_pipeline
            continue
        fi
    fi

    # Pipeline death: log LOUDLY (visible in the service journal), back
    # off, relaunch (one Argus session per recovery)
    if ! kill -0 "$GST_PID" 2>/dev/null; then
        wait "$GST_PID" 2>/dev/null
        GST_RC=$?
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: capture pipeline died (pid $GST_PID, exit status $GST_RC) - relaunching in ${RESTART_BACKOFF}s" >&2
        sleep "$RESTART_BACKOFF"
        launch_pipeline
    fi
done
