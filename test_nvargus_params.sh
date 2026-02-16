#!/bin/bash
# Test script to verify nvarguscamerasrc parameters
# Run this on the Jetson device to see available properties

echo "=== Available nvarguscamerasrc properties ==="
gst-inspect-1.0 nvarguscamerasrc | grep -A 2 "gain\|exposure\|ae\|awb"

echo ""
echo "=== Testing different parameter combinations ==="

# Test 1: Default (auto everything)
echo "Test 1: Default settings"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test1_default.jpg 2>&1 | head -20

# Test 2: With gainrange and exposuretimerange
echo ""
echo "Test 2: Manual gain=5, exposure=200000"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    gainrange="5 5" exposuretimerange="200000 200000" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test2_manual.jpg 2>&1 | head -20

# Test 3: With aeantibanding and wbmode
echo ""
echo "Test 3: With aeantibanding=0 wbmode=0"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    gainrange="5 5" exposuretimerange="200000 200000" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test3_manual_ae_wb.jpg 2>&1 | head -20

# Test 4: Very low exposure
echo ""
echo "Test 4: Low exposure=13000 (darkest)"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    gainrange="1 1" exposuretimerange="13000 13000" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test4_dark.jpg 2>&1 | head -20

# Test 5: Very high exposure
echo ""
echo "Test 5: High exposure=683709000 (brightest)"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    gainrange="10 10" exposuretimerange="683709000 683709000" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test5_bright.jpg 2>&1 | head -20

echo ""
echo "=== Test complete. Check images in /tmp/ ==="
ls -lh /tmp/test*.jpg
