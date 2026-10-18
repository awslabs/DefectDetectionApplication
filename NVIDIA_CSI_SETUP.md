# Nvidia CSI Camera Setup for Docker

Due to Argus daemon limitations, nvarguscamerasrc cannot run inside Docker containers on Jetson devices. This setup uses a host service to capture images that the Docker container can read.

## Provisioning Prerequisite: CSI is Opt-In

CSI capture is **opt-in per device** (spec: `csi-nvargus-optional`). nvargus/CSI
ISP activity can poison `nvargus-daemon` into a degraded state where ALL new
CUDA context creation fails device-wide (kernel signature: `Can't map dma
attachment!` + NVRM `osCreateOsDescriptorFromFileHandle ... Error (89)`), so
devices without a CSI camera keep the daemon disabled.

**Add Device** has no CSI opt-in: its setup command runs `setup_station.sh`
without the flag, which disables `nvargus-daemon` and clears the opt-in marker.
To use a CSI camera, provision the device manually with the flag (manual path,
see [station_install/README.md](station_install/README.md#manual-provisioning-not-the-normal-path)):

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
  device back out. A later Add Device setup on the device also opts it out.

A manual `setup_station.sh` run does not record the device's Target
Architecture. After it, open the device in the portal and set **Target
Architecture (DDA)** on **Overview** with **Edit**
([README 7.5](README.md#75-verify-the-device)). vLLM and node-plugin
deployments are rejected until it is set.

## Installation

No manual install step is needed. The JetPack LocalServer components
(`aws.edgeml.dda.LocalServer.arm64JP5`, `arm64JP6` and `arm64JP7`) run
`install_nvidia_csi_service.sh` in their install step, and it installs the
capture service only when the opt-in marker is present.

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
- The pipeline uses manual exposure and gain:
  `aeantibanding=0 wbmode=0 exposuretimerange="$EXPOSURE $EXPOSURE" gainrange="$GAIN $GAIN"`
  (`src/host_scripts/nvidia_csi_capture.sh`), with the values from the config
  file below
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
