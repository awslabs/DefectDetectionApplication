# Nvidia CSI Camera Setup for Docker

Due to Argus daemon limitations, nvarguscamerasrc cannot run inside Docker containers on Jetson devices. This setup uses a host service to capture images that the Docker container can read.

## Provisioning Prerequisite: CSI is Opt-In

CSI capture is **opt-in per device** (spec: `csi-nvargus-optional`). nvargus/CSI
ISP activity can poison `nvargus-daemon` into a degraded state where ALL new
CUDA context creation fails device-wide (kernel signature: `Can't map dma
attachment!` + NVRM `osCreateOsDescriptorFromFileHandle ... Error (89)`), so
devices without a CSI camera keep the daemon disabled.

To use a CSI camera, the device must be provisioned with the flag:

```bash
sudo ENABLE_CSI_CAMERA=1 bash setup_station.sh <aws-region> <thing_name>
```

This enables `nvargus-daemon` and writes the opt-in marker
`/aws_dda/system/csi_camera_optin`. The component installer gates on that
marker at every deployment:

- **Marker present**: `nvidia-csi-capture.service` is installed, enabled, and
  restarted as before.
- **Marker absent** (the default): every deployment disables and stops
  `nvidia-csi-capture.service`. Re-provision with `ENABLE_CSI_CAMERA=1` to
  opt in; re-provisioning without the flag clears the marker and opts the
  device back out.

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
- The service is a supervisor around **one long-lived `nvarguscamerasrc`
  pipeline** (a single persistent Argus session at 3264x2464 @ 21fps — never
  one session per frame; single-frame session churn is the driver-defect
  trigger pattern that degrades `nvargus-daemon`)
- The pipeline stages frames continuously via `multifilesink`; the supervisor
  promotes the newest complete frame to
  `/aws_dda/nvidia-csi-capture/latest.jpg` by atomic rename, so consumers
  never read a partial image
- The service reads gain, exposure, and crop settings from
  `/aws_dda/nvidia-csi-capture/config.json`
- When you adjust settings in the UI, the application updates this config
  file; the supervisor polls it (every 1s) and applies an effective change by
  restarting the pipeline exactly once with the new settings (subsequent
  staged frames reflect them)
- If the pipeline dies, the supervisor logs the failure visibly and relaunches
  it after a short backoff; systemd `Restart=always` remains the outer
  supervisor
- The Docker container reads `latest.jpg` when capturing/previewing

## Adjusting Camera Settings

Gain and exposure can be adjusted in the DDA web UI:

1. Navigate to your Nvidia CSI image source
2. Click "Edit image settings"
3. Adjust gain (1.0 - 10.625) and exposure (13000 - 683709000 nanoseconds)
4. The host service polls the config every second and applies an effective change through one supervised pipeline restart — new settings show up in staged frames within a few seconds

## Troubleshooting

If images aren't being captured:

```bash
# Check if the service is running
sudo systemctl status nvidia-csi-capture

# Restart the service
sudo systemctl restart nvidia-csi-capture

# Check for errors
sudo journalctl -u nvidia-csi-capture -n 50

# Test camera manually — ONE single-frame capture is fine for a smoke test,
# but do NOT run single-frame captures repeatedly (e.g. in a loop): per-frame
# Argus session churn is the driver-defect trigger pattern that degrades
# nvargus-daemon device-wide
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
