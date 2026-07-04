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

Then bridge the tunnel to a local port with the AWS IoT **local proxy**
(`localproxy`), in source mode:

```bash
# Install the local proxy: https://github.com/aws-samples/aws-iot-securetunneling-localproxy
export AWSIOT_TUNNEL_ACCESS_TOKEN=<SOURCE_TOKEN>
localproxy -r us-east-1 -s 5555        # listen locally on 5555 -> device SSH

# In another shell:
ssh -p 5555 ${SSH_USER}@localhost
```

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
