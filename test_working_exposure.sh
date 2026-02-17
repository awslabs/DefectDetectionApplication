#!/bin/bash
# Test nvarguscamerasrc with WORKING exposure values
# Based on test results showing extreme values are rejected

echo "=== Testing Nvidia CSI with Working Exposure Values ==="
echo ""

# Check if running in Docker
if [ -f /.dockerenv ]; then
    echo "ERROR: Running inside Docker. Exit and run on host."
    exit 1
fi

# Test with safe exposure values that the camera accepts
echo "Test 1: Low exposure (50000ns = 0.05ms) - Should be DARK"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    exposuretimerange="50000 50000" gainrange="1 1" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test_dark.jpg 2>&1 | grep -E "Setting|Invalid|Error" || echo "Capture completed"
echo ""

echo "Test 2: Medium exposure (200000ns = 0.2ms) - Should be NORMAL"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    exposuretimerange="200000 200000" gainrange="2 2" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test_normal.jpg 2>&1 | grep -E "Setting|Invalid|Error" || echo "Capture completed"
echo ""

echo "Test 3: High exposure (5000000ns = 5ms) - Should be BRIGHT"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    exposuretimerange="5000000 5000000" gainrange="8 8" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test_bright.jpg 2>&1 | grep -E "Setting|Invalid|Error" || echo "Capture completed"
echo ""

echo "Test 4: Very high exposure (30000000ns = 30ms) - Should be VERY BRIGHT"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    exposuretimerange="30000000 30000000" gainrange="10 10" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test_very_bright.jpg 2>&1 | grep -E "Setting|Invalid|Error" || echo "Capture completed"
echo ""

echo "=== Test Complete ==="
echo ""
echo "Images created:"
ls -lh /tmp/test_*.jpg 2>/dev/null || echo "No images created - check errors above"
echo ""
echo "Compare brightness levels:"
echo "  Dark:        /tmp/test_dark.jpg (50000ns, gain=1)"
echo "  Normal:      /tmp/test_normal.jpg (200000ns, gain=2)"
echo "  Bright:      /tmp/test_bright.jpg (5000000ns, gain=8)"
echo "  Very Bright: /tmp/test_very_bright.jpg (30000000ns, gain=10)"
echo ""
echo "If all images have different brightness, manual exposure control is WORKING!"
echo "If they all look the same, auto-exposure is still overriding the settings."
