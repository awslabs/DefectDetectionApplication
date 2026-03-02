# Nvidia CSI Exposure/Gain Fix - Final Summary

## Key Discovery from Test Results

The test output revealed that **the extreme exposure values are rejected by the camera**:
- `exposuretimerange="13000 13000"` → `GST_ARGUS: Invalid Exposure Time Range Input`
- `exposuretimerange="683709000 683709000"` → `GST_ARGUS: Invalid Exposure Time Range Input`

However, middle-range values ARE accepted:
- `exposuretimerange="200000 200000"` → `GST_ARGUS: NvArgusCameraSrc: Setting Exposure Time Range : 200000 200000` ✓

## Critical Parameters Discovered

From Test 3, we found that these parameters work together:
```bash
nvarguscamerasrc sensor_id=0 \
    aeantibanding=0 \      # Disable auto-exposure antibanding
    wbmode=0 \             # Disable auto white balance
    exposuretimerange="200000 200000" \
    gainrange="5 5"
```

## Changes Made

### 1. Updated Exposure Range (Frontend)
**File**: `src/frontend/src/components/image-settings/constants.ts`
- Changed from: `EXPOSURE_MIN = 13000, EXPOSURE_MAX = 683709000`
- Changed to: `EXPOSURE_MIN = 50000, EXPOSURE_MAX = 30000000`
- Reason: Extreme values are rejected; use safe working range

### 2. Updated Default Values
**Files**: Multiple
- Changed default exposure from 100000 to 200000 (0.2ms)
- Kept default gain at 2
- Reason: 200000 is proven to work from test results

### 3. Added wbmode=0 to Capture Script
**File**: `src/host_scripts/nvidia_csi_capture.sh`
- Added `wbmode=0` parameter to disable auto white balance
- Kept `aeantibanding=0` to disable auto-exposure
- Reason: Test 3 showed this combination works

### 4. Updated UI Constraint Text
**File**: `src/frontend/src/components/image-settings/details-input/EditImageSettingsInput.tsx`
- Updated to show: "Values between 50000 and 30000000 nanoseconds (0.05ms to 30ms)"
- Reason: Reflect the actual working range

## Working Exposure Range

Based on test results and camera specs:
- **Minimum**: 50000 nanoseconds (0.05ms) - Dark images
- **Default**: 200000 nanoseconds (0.2ms) - Normal exposure
- **Maximum**: 30000000 nanoseconds (30ms) - Very bright images

## Working Gain Range

- **Minimum**: 1.0 - Lowest gain
- **Default**: 2.0 - Normal gain
- **Maximum**: 10.625 - Highest gain (from sensor specs)

## Testing the Fix

### 1. Run the new test with working values:
```bash
chmod +x test_working_exposure.sh
./test_working_exposure.sh
```

This will create 4 images with different brightness levels. If they all look different, manual exposure control is working!

### 2. Deploy and test in UI:
```bash
# Build and deploy the updated code
# Then in the UI:
# 1. Edit Nvidia CSI image settings
# 2. Try these values:
#    - Dark: exposure=50000, gain=1
#    - Normal: exposure=200000, gain=2
#    - Bright: exposure=5000000, gain=8
#    - Very Bright: exposure=30000000, gain=10
# 3. Save and check if image brightness changes
```

### 3. Monitor the service:
```bash
sudo journalctl -u nvidia-csi-capture.service -f
```

You should see:
```
Settings updated - Gain: X, Exposure: Y
Capturing with Gain=X, Exposure=Y
```

## Expected Behavior After Fix

1. **Save button works** ✓ (Already fixed)
2. **Config file updates** ✓ (Already working)
3. **Service reads config** ✓ (Already working)
4. **Camera applies settings** ⚠️ (Needs testing with new values)

## If Manual Control Still Doesn't Work

If the images still don't change brightness with the new values, it means:
1. Auto-exposure is still active despite `aeantibanding=0` and `wbmode=0`
2. The camera firmware may not support full manual control
3. We may need to use alternative methods (v4l2-ctl or post-processing)

## Files Modified

### Frontend
- `src/frontend/src/components/image-settings/constants.ts` - Updated exposure range
- `src/frontend/src/components/image-settings/details-input/EditImageSettingsInput.tsx` - Updated constraint text
- `src/frontend/src/components/image-settings/EditImageSettingsPage.tsx` - Fixed Save button logic
- `src/frontend/src/components/image-settings/EditImageSettings.tsx` - Pass form validity state
- `src/frontend/src/components/image-settings/edit/schema.ts` - Added exposure max validation

### Backend
- `src/backend/gstreamer/pipeline_builder.py` - Updated default exposure to 200000
- `src/backend/utils/config/default_camera_configurations.json` - Updated defaults

### Host Scripts
- `src/host_scripts/nvidia_csi_capture.sh` - Added wbmode=0, updated defaults, added debug logging

### Test Scripts Created
- `test_working_exposure.sh` - Test with working exposure values
- `check_where_running.sh` - Verify running on host
- `diagnose_gstreamer.sh` - Full diagnostics
- `simple_csi_test.sh` - Simple camera test
- `check_csi_service.sh` - Service diagnostics

## Next Steps

1. Run `test_working_exposure.sh` on the Jetson host
2. Compare the 4 generated images - they should have visibly different brightness
3. If test passes, deploy the updated code
4. Test in the UI with different exposure/gain values
5. Verify image brightness changes in real-time preview
