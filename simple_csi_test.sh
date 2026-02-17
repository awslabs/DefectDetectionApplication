#!/bin/bash
# Simple test to verify CSI camera and nvarguscamerasrc work
# Run this on the Jetson HOST (not in Docker)

echo "=== Simple CSI Camera Test ==="
echo ""

# Check if running in Docker
if [ -f /.dockerenv ]; then
    echo "ERROR: You are running inside Docker!"
    echo "nvarguscamerasrc does not work in Docker containers."
    echo "Please exit the container and run this on the Jetson host."
    exit 1
fi

# Check if nvarguscamerasrc exists
if ! gst-inspect-1.0 nvarguscamerasrc &> /dev/null; then
    echo "ERROR: nvarguscamerasrc not found!"
    echo ""
    echo "Install required packages:"
    echo "  sudo apt-get update"
    echo "  sudo apt-get install -y gstreamer1.0-plugins-nvargus nvidia-l4t-gstreamer"
    exit 1
fi

echo "✓ nvarguscamerasrc is available"
echo ""

# Test 1: Basic capture with default settings
echo "Test 1: Capturing with default settings..."
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/csi_test_default.jpg

if [ -f /tmp/csi_test_default.jpg ]; then
    echo "✓ Success! Image saved to /tmp/csi_test_default.jpg"
    ls -lh /tmp/csi_test_default.jpg
else
    echo "✗ Failed to capture image"
    exit 1
fi
echo ""

# Test 2: Capture with low exposure (should be dark)
echo "Test 2: Capturing with LOW exposure (13000ns) and LOW gain (1)..."
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    exposuretimerange="13000 13000" gainrange="1 1" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/csi_test_dark.jpg

if [ -f /tmp/csi_test_dark.jpg ]; then
    echo "✓ Success! Image saved to /tmp/csi_test_dark.jpg"
    ls -lh /tmp/csi_test_dark.jpg
else
    echo "✗ Failed to capture image"
fi
echo ""

# Test 3: Capture with high exposure (should be bright)
echo "Test 3: Capturing with HIGH exposure (500000ns) and HIGH gain (8)..."
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    exposuretimerange="500000 500000" gainrange="8 8" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/csi_test_bright.jpg

if [ -f /tmp/csi_test_bright.jpg ]; then
    echo "✓ Success! Image saved to /tmp/csi_test_bright.jpg"
    ls -lh /tmp/csi_test_bright.jpg
else
    echo "✗ Failed to capture image"
fi
echo ""

echo "=== Test Complete ==="
echo ""
echo "Compare the images:"
echo "  Default:  /tmp/csi_test_default.jpg"
echo "  Dark:     /tmp/csi_test_dark.jpg"
echo "  Bright:   /tmp/csi_test_bright.jpg"
echo ""
echo "If the dark and bright images look the same, the camera may be"
echo "ignoring manual exposure/gain settings (auto-exposure is active)."
