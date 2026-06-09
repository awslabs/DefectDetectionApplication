# How to Test Nvidia CSI Camera Exposure/Gain Settings

## IMPORTANT: Run Tests on HOST, Not in Docker!

The `nvarguscamerasrc` GStreamer element **ONLY works on the Jetson host**, not inside Docker containers. You'll get the error "no element nvarguscamerasrc" if you try to run it in Docker.

## Step-by-Step Testing

### 1. Exit Docker (if you're in it)
```bash
exit
```

### 2. Navigate to the project directory on the host
```bash
cd /path/to/DefectDetectionApplication
```

### 3. Run the environment check
```bash
chmod +x check_where_running.sh
./check_where_running.sh
```

This will tell you:
- If you're on the host or in Docker
- If nvarguscamerasrc is installed
- If the capture service is installed

### 4. Install missing packages (if needed)
If nvarguscamerasrc is not available:
```bash
sudo apt-get update
sudo apt-get install -y gstreamer1.0-plugins-nvargus nvidia-l4t-gstreamer gstreamer1.0-tools
```

### 5. Run the simple camera test
```bash
chmod +x simple_csi_test.sh
./simple_csi_test.sh
```

This will capture 3 images:
- `/tmp/csi_test_default.jpg` - Default settings
- `/tmp/csi_test_dark.jpg` - Low exposure/gain (should be dark)
- `/tmp/csi_test_bright.jpg` - High exposure/gain (should be bright)

**Compare the images**: If the dark and bright images look the same, the camera is ignoring manual settings (auto-exposure is overriding).

### 6. Check the capture service
```bash
chmod +x check_csi_service.sh
./check_csi_service.sh
```

This will:
- Show service status
- Display current config
- Test config updates
- Show recent logs

### 7. Watch service logs in real-time
```bash
sudo journalctl -u nvidia-csi-capture.service -f
```

Then in the DDA UI:
1. Go to Edit Image Settings for your Nvidia CSI camera
2. Change exposure or gain values
3. Click Save
4. Watch the logs - you should see "Settings updated - Gain: X, Exposure: Y"

### 8. Run full diagnostics (if issues persist)
```bash
chmod +x diagnose_gstreamer.sh
./diagnose_gstreamer.sh
```

## Expected Results

### If Everything Works:
- `simple_csi_test.sh` creates 3 images with different brightness levels
- Service logs show "Settings updated" when you change values in UI
- Image preview in UI changes brightness when you adjust exposure/gain

### If Exposure/Gain Don't Work:
- All 3 test images have similar brightness (auto-exposure is active)
- Service logs show settings updated, but image doesn't change
- This means nvarguscamerasrc is ignoring the manual parameters

## Next Steps if Manual Control Doesn't Work

If the camera ignores manual exposure/gain settings, we may need to:

1. **Try different nvarguscamerasrc properties**:
   - Check available properties: `gst-inspect-1.0 nvarguscamerasrc`
   - Look for `aelock`, `awblock`, or other AE control properties

2. **Use v4l2-ctl to control camera directly**:
   ```bash
   v4l2-ctl -d /dev/video0 --list-ctrls
   v4l2-ctl -d /dev/video0 --set-ctrl=exposure_absolute=200
   ```

3. **Check Jetson forums** for IMX219-specific manual exposure control

4. **Consider alternative approach**: Use post-processing to adjust brightness instead of camera settings

## Files Created for Testing

- `check_where_running.sh` - Verify you're on host, not Docker
- `diagnose_gstreamer.sh` - Full GStreamer diagnostics
- `simple_csi_test.sh` - Quick camera test with 3 exposure levels
- `check_csi_service.sh` - Service status and config test
- `test_nvargus_params.sh` - Comprehensive parameter testing (advanced)

## Common Issues

### "no element nvarguscamerasrc"
- You're running inside Docker - exit and run on host
- OR nvarguscamerasrc is not installed - install packages above

### "Could not get EGL display connection"
- You're running inside Docker - this confirms nvarguscamerasrc needs host access

### Service not found
- Run installation: `sudo bash src/host_scripts/install_nvidia_csi_service.sh`

### Config updates but image doesn't change
- Camera has auto-exposure enabled that overrides manual settings
- Need to find correct parameters to disable AE
- May need alternative approach
