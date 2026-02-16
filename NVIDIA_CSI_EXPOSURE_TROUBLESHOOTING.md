# Nvidia CSI Camera Exposure/Gain Troubleshooting

## Issue
The config file (`/aws_dda/nvidia-csi-capture/config.json`) is being updated correctly, but the exposure and gain settings don't reliably affect the captured image brightness.

## Root Cause Analysis

### Possible Causes:
1. **Auto-Exposure (AE) Override**: nvarguscamerasrc may have auto-exposure enabled by default, which overrides manual settings
2. **Parameter Format**: The gainrange/exposuretimerange parameters might not be in the correct format
3. **Timing Issue**: The service might be capturing too fast for settings to take effect
4. **Camera Firmware**: The IMX219 camera firmware might have limitations on manual control

## Diagnostic Steps

### Step 1: Check if config file is being updated
```bash
# Watch the config file for changes
watch -n 1 cat /aws_dda/nvidia-csi-capture/config.json
```

### Step 2: Check if service is reading the config
```bash
# Check service logs
sudo journalctl -u nvidia-csi-capture.service -f
```
You should see: "Settings updated - Gain: X, Exposure: Y" when you change settings in the UI.

### Step 3: Test nvarguscamerasrc parameters manually
Run the test script to verify which parameters work:
```bash
chmod +x test_nvargus_params.sh
./test_nvargus_params.sh
```

Compare the brightness of the generated test images in `/tmp/`.

### Step 4: Check available nvarguscamerasrc properties
```bash
gst-inspect-1.0 nvarguscamerasrc | grep -E "gain|exposure|ae|awb"
```

Look for properties like:
- `aeantibanding`: Auto-exposure antibanding mode
- `aelock`: Auto-exposure lock
- `awblock`: Auto white balance lock
- `exposuretimerange`: Exposure time range
- `gainrange`: Gain range

## Solutions to Try

### Solution 1: Disable Auto-Exposure (Current Implementation)
The capture script now uses `aeantibanding=0` to disable auto-exposure antibanding:
```bash
nvarguscamerasrc sensor_id=0 aeantibanding=0 \
    exposuretimerange="$EXPOSURE $EXPOSURE" \
    gainrange="$GAIN $GAIN"
```

### Solution 2: Use aelock and awblock (If Available)
If the camera supports AE lock, try:
```bash
nvarguscamerasrc sensor_id=0 aelock=true awblock=true \
    exposuretimerange="$EXPOSURE $EXPOSURE" \
    gainrange="$GAIN $GAIN"
```

### Solution 3: Increase Sleep Time Between Captures
The current script captures at ~10fps (sleep 0.1). Try increasing to 1 second:
```bash
# In nvidia_csi_capture.sh, change:
sleep 0.1
# to:
sleep 1
```

### Solution 4: Use v4l2-ctl for Manual Control
If nvarguscamerasrc doesn't support manual control, try using v4l2-ctl to set camera parameters:
```bash
# Set exposure (if supported)
v4l2-ctl -d /dev/video0 --set-ctrl=exposure_absolute=200

# Set gain (if supported)
v4l2-ctl -d /dev/video0 --set-ctrl=gain=5
```

Then capture with nvarguscamerasrc without parameters.

### Solution 5: Restart Service After Config Change
Add a mechanism to restart the capture service when config changes:
```bash
# After updating config in UI, restart service
sudo systemctl restart nvidia-csi-capture.service
```

## Testing Procedure

1. **Set very low exposure** (13000) and gain (1.0) in UI
   - Image should be very dark/black
   
2. **Set very high exposure** (683709000) and gain (10.625) in UI
   - Image should be very bright/overexposed
   
3. **Set medium values** (exposure=100000, gain=2)
   - Image should be normally exposed

If the image brightness doesn't change between these extremes, the camera is ignoring the manual settings.

## Current Script Configuration

The capture script (`src/host_scripts/nvidia_csi_capture.sh`) currently:
- Reads config every 0.1 seconds
- Uses `aeantibanding=0` to disable auto-exposure
- Sets `exposuretimerange="$EXPOSURE $EXPOSURE"`
- Sets `gainrange="$GAIN $GAIN"`

## Next Steps if Issue Persists

1. Run `test_nvargus_params.sh` to identify which parameters work
2. Check `gst-inspect-1.0 nvarguscamerasrc` output for available properties
3. Try alternative parameters based on camera capabilities
4. Consider using a different capture method (v4l2-ctl + nvarguscamerasrc)
5. Check Nvidia Jetson forums for IMX219-specific manual exposure control

## References
- [Nvidia Accelerated GStreamer User Guide](https://docs.nvidia.com/jetson/l4t/index.html#page/Tegra%20Linux%20Driver%20Package%20Development%20Guide/accelerated_gstreamer.html)
- [nvarguscamerasrc Properties](https://developer.ridgerun.com/wiki/index.php?title=Jetson_Nano/Gstreamer/Example_Pipelines/Saving_frames_to_disk)
