# Nvidia CSI Camera Settings Fix

## Issues Fixed

### 1. Save Button Disabled
**Problem**: The Save button was disabled after changing gain/exposure values in the Edit Image Settings page.

**Root Cause**: The button was checking `props.cameraStatus !== CameraStatus.Connected` for all camera types, but Nvidia CSI cameras don't use the Arvis camera connection system.

**Solution**: Changed the condition to only check camera status for Arvis cameras:
```typescript
disabled={props.isArvisCamera && props.cameraStatus !== CameraStatus.Connected}
```

### 2. Gain/Exposure Not Affecting Image
**Problem**: Changing gain and exposure values in the UI didn't affect the captured image brightness.

**Root Cause**: 
- The UI exposure range was 1-150000, but nvarguscamerasrc expects nanoseconds (13000-683709000)
- The UI gain range was 1-100, but the IMX219 sensor supports 1.0-10.625
- Default values were too low (gain=1, exposure=500)

**Solution**:
- Updated exposure range to 13000-683709000 nanoseconds (matching sensor specs)
- Updated gain range to 1.0-10.625 (matching sensor specs)
- Changed default values to gain=2, exposure=100000 for better initial brightness
- Updated constraint text to clarify the units and ranges

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
