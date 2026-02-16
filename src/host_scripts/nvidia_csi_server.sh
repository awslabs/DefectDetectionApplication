#!/bin/bash
#
# Nvidia CSI Camera GStreamer Server
# This script runs on the host and streams camera frames via TCP
# The Docker container connects to this stream
#

PORT=5000

echo "Starting Nvidia CSI camera server on port $PORT..."
echo "Camera will stream at 3264x2464 @ 21fps"

# Kill any existing server
pkill -f "gst-launch.*nvarguscamerasrc.*tcpserversink"

# Start the GStreamer server
gst-launch-1.0 -v \
  nvarguscamerasrc sensor_id=0 ! \
  'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
  nvvidconv ! \
  'video/x-raw,format=I420' ! \
  videoconvert ! \
  'video/x-raw,format=I420' ! \
  tcpserversink host=127.0.0.1 port=$PORT sync=false &

echo "Server started. Container can connect to tcp://127.0.0.1:$PORT"
