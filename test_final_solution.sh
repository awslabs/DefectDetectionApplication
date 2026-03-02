#!/bin/bash
# Final test: Use moderate exposure with varying gain
# Based on findings that low exposure + low gain = failure

echo "=== Final Solution Test: Moderate Exposure + Variable Gain ==="
echo ""

# Strategy: Keep exposure at a moderate value, vary gain for brightness control
FIXED_EXPOSURE=5000000  # 5ms - middle of working range

echo "Using FIXED exposure: ${FIXED_EXPOSURE}ns (5ms)"
echo "Varying GAIN for brightness control"
echo ""

echo "Test 1: Low brightness (gain=1)"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    exposuretimerange="$FIXED_EXPOSURE $FIXED_EXPOSURE" \
    gainrange="1 1" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/final_dark.jpg 2>&1 | grep -E "Setting|Error|Failed" || echo "✓ Success"
echo ""

echo "Test 2: Medium-low brightness (gain=2)"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    exposuretimerange="$FIXED_EXPOSURE $FIXED_EXPOSURE" \
    gainrange="2 2" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/final_medium_low.jpg 2>&1 | grep -E "Setting|Error|Failed" || echo "✓ Success"
echo ""

echo "Test 3: Medium brightness (gain=4)"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    exposuretimerange="$FIXED_EXPOSURE $FIXED_EXPOSURE" \
    gainrange="4 4" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/final_medium.jpg 2>&1 | grep -E "Setting|Error|Failed" || echo "✓ Success"
echo ""

echo "Test 4: Medium-high brightness (gain=6)"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    exposuretimerange="$FIXED_EXPOSURE $FIXED_EXPOSURE" \
    gainrange="6 6" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/final_medium_high.jpg 2>&1 | grep -E "Setting|Error|Failed" || echo "✓ Success"
echo ""

echo "Test 5: High brightness (gain=8)"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    exposuretimerange="$FIXED_EXPOSURE $FIXED_EXPOSURE" \
    gainrange="8 8" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/final_high.jpg 2>&1 | grep -E "Setting|Error|Failed" || echo "✓ Success"
echo ""

echo "Test 6: Very high brightness (gain=10)"
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 \
    aeantibanding=0 wbmode=0 \
    exposuretimerange="$FIXED_EXPOSURE $FIXED_EXPOSURE" \
    gainrange="10 10" ! \
    'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! \
    nvvidconv ! jpegenc ! filesink location=/tmp/final_very_high.jpg 2>&1 | grep -E "Setting|Error|Failed" || echo "✓ Success"
echo ""

echo "=== Test Complete ==="
echo ""
echo "Images created:"
ls -lh /tmp/final_*.jpg 2>/dev/null
echo ""
echo "Brightness progression (should increase):"
echo "  1. final_dark.jpg         (gain=1)"
echo "  2. final_medium_low.jpg   (gain=2)"
echo "  3. final_medium.jpg       (gain=4)"
echo "  4. final_medium_high.jpg  (gain=6)"
echo "  5. final_high.jpg         (gain=8)"
echo "  6. final_very_high.jpg    (gain=10)"
echo ""
echo "If all 6 images have different brightness levels, this is the SOLUTION!"
echo "We'll use fixed exposure=5000000ns and vary gain (1-10) for brightness control."
