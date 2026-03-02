#!/bin/bash
# Diagnose GStreamer installation and available camera elements

echo "=== GStreamer Diagnostics ==="
echo ""

echo "1. GStreamer version:"
gst-launch-1.0 --version
echo ""

echo "2. Check for Nvidia GStreamer plugins:"
dpkg -l | grep -i gstreamer | grep -i nv
echo ""

echo "3. Available video source elements:"
gst-inspect-1.0 | grep -i "video.*src\|camera\|argus"
echo ""

echo "4. Check if nvarguscamerasrc is available:"
if gst-inspect-1.0 nvarguscamerasrc &> /dev/null; then
    echo "✓ nvarguscamerasrc is available"
    gst-inspect-1.0 nvarguscamerasrc | head -30
else
    echo "✗ nvarguscamerasrc is NOT available"
    echo ""
    echo "This plugin should be provided by: gstreamer1.0-plugins-nvargus"
    echo "Or it may be part of: nvidia-l4t-gstreamer"
fi
echo ""

echo "5. Check for v4l2 video devices:"
ls -l /dev/video* 2>/dev/null || echo "No /dev/video* devices found"
echo ""

echo "6. Check for Argus camera:"
if [ -e /dev/video0 ]; then
    v4l2-ctl --list-devices 2>/dev/null || echo "v4l2-ctl not installed"
fi
echo ""

echo "7. Try to list camera capabilities:"
if command -v nvgstcapture-1.0 &> /dev/null; then
    echo "nvgstcapture-1.0 is available"
else
    echo "nvgstcapture-1.0 is not available"
fi
echo ""

echo "8. Check Jetson release info:"
if [ -f /etc/nv_tegra_release ]; then
    cat /etc/nv_tegra_release
else
    echo "Not a Jetson device or release file not found"
fi
echo ""

echo "=== Diagnostic Complete ==="
echo ""
echo "If nvarguscamerasrc is not available, you may need to:"
echo "1. Install: sudo apt-get install gstreamer1.0-plugins-nvargus"
echo "2. Or install: sudo apt-get install nvidia-l4t-gstreamer"
echo "3. Check if running inside Docker - nvarguscamerasrc may not work in containers"
