#!/bin/bash
# Check if we're running inside Docker or on host

echo "=== Environment Check ==="
echo ""

if [ -f /.dockerenv ]; then
    echo "⚠️  RUNNING INSIDE DOCKER CONTAINER"
    echo ""
    echo "nvarguscamerasrc CANNOT run inside Docker!"
    echo "You must run this test on the HOST system."
    echo ""
    echo "Exit the container and run this script on the Jetson host."
else
    echo "✓ Running on HOST system"
    echo ""
    
    # Check if service is installed
    if systemctl list-unit-files | grep -q nvidia-csi-capture.service; then
        echo "✓ nvidia-csi-capture.service is installed"
        echo ""
        echo "Service status:"
        systemctl status nvidia-csi-capture.service --no-pager | head -10
    else
        echo "✗ nvidia-csi-capture.service is NOT installed"
        echo ""
        echo "Run the installation script:"
        echo "  sudo bash src/host_scripts/install_nvidia_csi_service.sh"
    fi
fi
echo ""

echo "Current user: $(whoami)"
echo "Current directory: $(pwd)"
echo ""

# Check for nvarguscamerasrc
echo "Checking for nvarguscamerasrc..."
if gst-inspect-1.0 nvarguscamerasrc &> /dev/null; then
    echo "✓ nvarguscamerasrc is available"
else
    echo "✗ nvarguscamerasrc is NOT available"
    echo ""
    echo "Install required packages:"
    echo "  sudo apt-get update"
    echo "  sudo apt-get install gstreamer1.0-plugins-nvargus nvidia-l4t-gstreamer"
fi
