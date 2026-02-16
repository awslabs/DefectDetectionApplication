# Nvidia CSI Camera Settings Fix

## Issues Fixed

### 1. Save Button Disabled ✓ FIXED
**Problem**: The Save button was disabled after changing gain/exposure values in the Edit Image Settings page.

**Root Cause**: The button was checking camera connection status for all camera types, but Nvidia CSI cameras don't use the Arvis camera connection system.

**Solution**: 
- Changed the condition to only check camera status for Arvis cameras
- Added form validation state check for all camera types
- Save button now enabled when form is valid for Nvidia CSI cameras

### 2. Gain/Exposure Not Affecting Image ⚠️ IN PROGRESS
**Problem**: Changing gain and exposure values in the UI doesn't reliably affect the captured image brightness.

**Status**: Config file is being updated correctly (verified with jq), but nvarguscamerasrc may not be applying the settings.

**Possible Root Causes**:
1. Auto-exposure (AE) is enabled by default and overriding manual settings
2. nvarguscamerasrc parameter format may be incorrect
3. Camera firmware may have limitations on manual control

**Current Solution Attempt**:
- Added `aeantibanding=0` parameter to disable auto-exposure antibanding
- Using correct parameter format: `exposuretimerange="$EXPOSURE $EXPOSURE"` and `gainrange="$GAIN $GAIN"`
- Added debug logging to track when settings change

**Next Steps for Testing**:
1. Run `check_csi_service.sh` to verify service is reading config changes
2. Run `test_nvargus_params.sh` to test different parameter combinations
3. Check service logs: `sudo journalctl -u nvidia-csi-capture.service -f`
4. Try extreme values (very dark vs very bright) to see if any change occurs

## Files Modified

### Frontend
- `src/frontend/src/components/image-settings/constants.ts`
  - GAIN_MAX: 100 → 10.625
  - EXPOSURE_MIN: 1 → 13000
  - EXPOSURE_MAX: 150000 → 683709000

- `src/frontend/src/components/image-settings/EditImageSettingsPage.tsx`
  - Save button now only checks camera status for Arvis cameras

- `src/frontend/src/components/image-settings/details-input/EditImageSettingsInput.tsx`
  - Updated constraint text to clarify units and ranges

### Backend
- `src/backend/gstreamer/pipeline_builder.py`
  - Default gain: 1 → 2
  - Default exposure: 500 → 100000

- `src/backend/utils/config/default_camera_configurations.json`
  - Added default gain and exposure values for Nvidia CSI

### Host Scripts
- `src/host_scripts/nvidia_csi_capture.sh`
  - Updated all default values to gain=2, exposure=100000

## Camera Specifications (IMX219)
- Resolution: 3264x2464 @ 21fps
- Analog Gain Range: 1.0 to 10.625
- Exposure Range: 13000 to 683709000 nanoseconds (0.013ms to 683.7ms)

## Testing
After deploying these changes:
1. Create a new Nvidia CSI image source or edit existing one
2. Go to Edit Image Settings
3. Adjust gain (1.0-10.625) and exposure (13000-683709000)
4. Save button should be enabled
5. Image brightness should change according to the settings
6. Settings are written to `/aws_dda/nvidia-csi-capture/config.json`
7. Host service reads config and applies to nvarguscamerasrc
