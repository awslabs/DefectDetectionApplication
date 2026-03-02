# Nvidia CSI Camera Setup for Docker

Due to Argus daemon limitations, nvarguscamerasrc cannot run inside Docker containers on Jetson devices. This setup uses a host service to capture images that the Docker container can read.

## Installation

On your Jetson device, run:

```bash
# After deploying the component, install the capture service
cd /greengrass/v2/packages/artifacts-unarchived/aws.edgeml.dda.LocalServer.arm64/*/aws.edgeml.dda.LocalServer.arm64-aarch64/custom-build/aws.edgeml.dda.LocalServer.arm64/host_scripts

sudo bash install_nvidia_csi_service.sh
```

## Verify Service

```bash
# Check service status
sudo systemctl status nvidia-csi-capture

# View logs
sudo journalctl -u nvidia-csi-capture -f

# Check if images are being captured
ls -lh /aws_dda/nvidia-csi-capture/
```

## Usage

1. In the DDA web UI, create a new image source
2. Select "Nvidia CSI" as the type
3. The application will read from the continuously captured images

## How It Works

- A systemd service runs on the host (outside Docker)
- It continuously captures from nvarguscamerasrc at 3264x2464 @ 21fps
- The service reads gain and exposure settings from `/aws_dda/nvidia-csi-capture/config.json`
- When you adjust gain/exposure in the UI, the application updates this config file
- The host service picks up the changes and applies them to the next capture
- Images are saved to `/aws_dda/nvidia-csi-capture/latest.jpg`
- The Docker container reads this file when capturing/previewing
- The file is atomically updated to prevent reading partial images

## Adjusting Camera Settings

Gain and exposure can be adjusted in the DDA web UI:

1. Navigate to your Nvidia CSI image source
2. Click "Edit image settings"
3. Adjust gain (1.0 - 10.625) and exposure (13000 - 683709000 nanoseconds)
4. The host service will apply the new settings within ~100ms

## Troubleshooting

If images aren't being captured:

```bash
# Check if the service is running
sudo systemctl status nvidia-csi-capture

# Restart the service
sudo systemctl restart nvidia-csi-capture

# Check for errors
sudo journalctl -u nvidia-csi-capture -n 50

# Test camera manually
gst-launch-1.0 nvarguscamerasrc sensor_id=0 num-buffers=1 ! 'video/x-raw(memory:NVMM),width=3264,height=2464,framerate=21/1' ! nvvidconv ! 'video/x-raw,format=BGRx' ! videoconvert ! jpegenc ! filesink location=/tmp/test.jpg
```

## Uninstall

```bash
sudo systemctl stop nvidia-csi-capture
sudo systemctl disable nvidia-csi-capture
sudo rm /etc/systemd/system/nvidia-csi-capture.service
sudo rm /aws_dda/system/nvidia_csi_capture.sh
sudo systemctl daemon-reload
```
