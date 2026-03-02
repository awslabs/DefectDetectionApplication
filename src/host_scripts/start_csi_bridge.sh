#!/bin/bash
#
# Nvidia CSI Camera Bridge
# Captures from nvarguscamerasrc on host and provides frames via named pipe
#

FIFO_PATH="/tmp/nvidia_csi_fifo"
PID_FILE="/tmp/nvidia_csi_bridge.pid"

# Function to cleanup on exit
cleanup() {
    echo "Stopping CSI camera bridge..."
    if [ -f "$PID_FILE" ]; then
        kill $(cat "$PID_FILE") 2>/dev/null
        rm -f "$PID_FILE"
    fi
    rm -f "$FIFO_PATH"
    exit 0
}

trap cleanup SIGINT SIGTERM EXIT

# Check if already running
if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
    echo "CSI camera bridge is already running (PID: $(cat $PID_FILE))"
    exit 0
fi

# Create named pipe
rm -f "$FIFO_PATH"
mkfifo "$FIFO_PATH"
chmod 666 "$FIFO_PATH"

echo "Starting Nvidia CSI camera bridge..."
echo "FIFO: $FIFO_PATH"

# Start GStreamer pipeline that writes to FIFO
# This runs continuously and writes JPEG frames to the pipe
while true; do
    gst-launch-1.0 -q \
        nvarguscamerasrc sensor_id=0 num-buffers=1 ! \
        'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
        nvvidconv ! \
        'video/x-raw,format=BGRx' ! \
        videoconvert ! \
        jpegenc idct-method=2 quality=100 ! \
        filesink location="$FIFO_PATH" 2>/dev/null
    
    # Small delay between captures
    sleep 0.1
done &

# Save PID
echo $! > "$PID_FILE"

echo "CSI camera bridge started (PID: $!)"
echo "Container can read from: $FIFO_PATH"

# Keep script running
wait
