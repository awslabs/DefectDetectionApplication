#!/bin/bash
# Quick diagnostic script for Nvidia CSI capture service

echo "=== Nvidia CSI Capture Service Diagnostics ==="
echo ""

echo "1. Service Status:"
sudo systemctl status nvidia-csi-capture.service | head -20
echo ""

echo "2. Config File Contents:"
if [ -f /aws_dda/nvidia-csi-capture/config.json ]; then
    cat /aws_dda/nvidia-csi-capture/config.json
    echo ""
else
    echo "Config file not found!"
    echo ""
fi

echo "3. Latest Image Info:"
if [ -f /aws_dda/nvidia-csi-capture/latest.jpg ]; then
    ls -lh /aws_dda/nvidia-csi-capture/latest.jpg
    echo "Image size: $(stat -f%z /aws_dda/nvidia-csi-capture/latest.jpg 2>/dev/null || stat -c%s /aws_dda/nvidia-csi-capture/latest.jpg) bytes"
    echo ""
else
    echo "Latest image not found!"
    echo ""
fi

echo "4. Recent Service Logs (last 20 lines):"
sudo journalctl -u nvidia-csi-capture.service -n 20 --no-pager
echo ""

echo "5. Test: Update config and watch for changes"
echo "Current config:"
cat /aws_dda/nvidia-csi-capture/config.json
echo ""
echo "Writing test config (gain=5, exposure=300000)..."
echo '{"gain":5,"exposure":300000}' | sudo tee /aws_dda/nvidia-csi-capture/config.json > /dev/null
echo "Waiting 2 seconds..."
sleep 2
echo "Checking logs for update message..."
sudo journalctl -u nvidia-csi-capture.service -n 5 --no-pager | grep "Settings updated"
echo ""

echo "=== Diagnostic Complete ==="
echo ""
echo "To watch live logs: sudo journalctl -u nvidia-csi-capture.service -f"
echo "To restart service: sudo systemctl restart nvidia-csi-capture.service"
