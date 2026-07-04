# Connect to an edge device (AWS IoT Secure Tunneling)

Connect a shell to a Greengrass core device with no inbound ports / public IP,
using AWS IoT Secure Tunneling. Useful for inspecting deployments and debugging
on-device model serving (e.g. confirming an ONNX/RF-DETR model loaded).

> The SSH login user is environment-specific — set `SSH_USER` below.
> - EC2 (Ubuntu AMI): `ubuntu`
> - Greengrass default: `ggc_user` (exists on every core; may need a login shell/key)
> - Jetson / on-prem: your device user

## Prerequisites (one-time per device)

1. **Deploy the Secure Tunneling component** to the device. Revise the device's
   deployment to add:
   - `aws.greengrass.SecureTunneling` (latest)
   Keep it alongside the existing components (LocalServer, model component).
   Do this when no other deployment is in progress.

2. **Device role**: the Greengrass Token Exchange role must allow the tunneling
   actions the component uses (subscribe/connect to the tunnel notifications).
   The AWS-managed component documents the exact permissions.

3. On the device, an SSH server must be listening on `127.0.0.1:22` and the
   chosen `SSH_USER` must be able to log in (key or password per your policy).

## Open a tunnel and connect (source side)

```bash
THING=jp5730ai-164v2
SSH_USER=ggc_user           # ubuntu on EC2; adjust per device

# 1) Open a tunnel for the SSH service; capture the source access token.
aws iot open-tunnel \
  --destination-config "thingName=${THING},services=SSH" \
  --region us-east-1

# Note the tunnelId and the SOURCE access token from the output.
```

Then bridge the tunnel to a local port with the AWS IoT **local proxy**, in
source mode.

> **Important:** the device end uses the **Greengrass Secure Tunneling
> component**, which speaks the **V1** protocol. The current local proxy (v3)
> must be started in source mode with `--destination-client-type V1`, or the
> connection will fail.

### Option A — Docker (recommended; no build, works on Windows, WSL, macOS, Linux)

AWS publishes a prebuilt local-proxy image, so there's nothing to compile:

```bash
# On Apple Silicon Macs, change amd64-latest -> arm64-latest.
# 1) Start the local proxy (keep this terminal open):
docker run --rm -it -p 5555:5555 \
  -e AWSIOT_TUNNEL_ACCESS_TOKEN="<SOURCE_TOKEN>" \
  public.ecr.aws/aws-iot-securetunneling-localproxy/ubuntu-bin:amd64-latest \
  --region us-east-1 -s 5555 -b 0.0.0.0 --destination-client-type V1

# 2) In a SECOND terminal, SSH to the device:
ssh -p 5555 ${SSH_USER}@localhost
```

The exact same `docker run` command works in WSL, PowerShell, macOS, and Linux
(`-b 0.0.0.0` + `-p 5555:5555` publishes the port so `ssh ...@localhost` reaches it).

### Option B — Native binary

`localproxy` is not an OS package; build it from source or use the prebuilt
binary image from
https://github.com/aws-samples/aws-iot-securetunneling-localproxy (Windows build
steps: `windows-localproxy-build.md`). Then:

```bash
export AWSIOT_TUNNEL_ACCESS_TOKEN="<SOURCE_TOKEN>"   # PowerShell: $env:AWSIOT_TUNNEL_ACCESS_TOKEN="<SOURCE_TOKEN>"
localproxy -r us-east-1 -s 5555 --destination-client-type V1
ssh -p 5555 ${SSH_USER}@localhost
```

The portal's **Remote Access** tab shows these exact commands with the token and
region filled in, plus a Copy button, after you click "Open SSH session".

Alternatively, use the **AWS IoT console → Manage → Tunnels → Create tunnel**
(select the thing, SSH service) for a guided, browser-initiated flow, then follow
the same local-proxy + SSH step.

## Verify the model deployment once connected

```bash
# Greengrass component logs (model component startup runs model_convertor.py):
sudo tail -n 200 /greengrass/v2/logs/model-rf-detr-seg-nano-jetson-xavier-jp5.log
# LocalServer / flask-app + Triton logs:
sudo tail -n 200 /greengrass/v2/logs/aws.edgeml.dda.LocalServer.arm64JP5.log
# Confirm the ONNX engine loaded model.onnx and the Triton model repo built.
```

## Cleanup

```bash
aws iot close-tunnel --tunnel-id <TUNNEL_ID> --region us-east-1
```

A future portal "Connect" button will wrap this into a browser terminal — see
docs/device-web-connect-spec.md.
