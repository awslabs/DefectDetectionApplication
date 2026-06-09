#!/bin/bash
# Test different methods to disable auto-exposure

echo "=== Testing Auto-Exposure Lock Methods ==="
echo ""

# First, check what properties are available
echo "Available nvarguscamerasrc properties:"
gst-inspect-1.0 nvarguscamerasrc 2>/dev/null | grep -i "lock\|ae\|exposure\|gain" | head -20
echo ""

# Test 1: Try aelock property (if available)
echo "Test 1: Using aelock=true (if supported)"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aelock=true \
    exposuretimerange="100000 100000" gainrange="1 1" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test_aelock_dark.jpg 2>&1 | grep -E "Setting|Invalid|Error|aelock" || echo "Completed"
echo ""

echo "Test 2: Using aelock=true with high exposure"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aelock=true \
    exposuretimerange="10000000 10000000" gainrange="1 1" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test_aelock_bright.jpg 2>&1 | grep -E "Setting|Invalid|Error|aelock" || echo "Completed"
echo ""

# Test 3: Try awblock property
echo "Test 3: Using awblock=true (if supported)"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    awblock=true aelock=true \
    exposuretimerange="100000 100000" gainrange="1 1" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test_awblock_dark.jpg 2>&1 | grep -E "Setting|Invalid|Error|lock" || echo "Completed"
echo ""

# Test 4: Try exposurecompensation=0
echo "Test 4: Using exposurecompensation=0 (if supported)"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    exposurecompensation=0 \
    exposuretimerange="100000 100000" gainrange="1 1" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test_expcomp_dark.jpg 2>&1 | grep -E "Setting|Invalid|Error|compensation" || echo "Completed"
echo ""

# Test 5: Gain-only test (no exposure setting)
echo "Test 5: Using ONLY gain (no exposure parameter) - Low gain"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    gainrange="1 1" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test_gain_only_low.jpg 2>&1 | grep -E "Setting|Invalid|Error" || echo "Completed"
echo ""

echo "Test 6: Using ONLY gain (no exposure parameter) - High gain"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    gainrange="10 10" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/test_gain_only_high.jpg 2>&1 | grep -E "Setting|Invalid|Error" || echo "Completed"
echo ""

echo "=== Test Complete ==="
echo ""
echo "Images created:"
ls -lh /tmp/test_*.jpg 2>/dev/null | tail -10
echo ""
echo "Compare these pairs:"
echo "1. aelock: test_aelock_dark.jpg vs test_aelock_bright.jpg"
echo "2. awblock: test_awblock_dark.jpg"
echo "3. gain-only: test_gain_only_low.jpg vs test_gain_only_high.jpg"
echo ""
echo "If gain-only images differ, we should focus on GAIN control instead of exposure."
