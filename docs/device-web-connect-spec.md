# Spec: Web-based "Connect to Device" (remote shell) for edge devices

Status: Proposal / future work
Owners: DDA edge team

## 1. Goal

Let a portal user open an interactive shell to a Greengrass edge device from the
browser — similar to the **EC2 Instance Connect / SSM Session Manager** console
experience — without needing the device on the local network, a public IP, or
pre-shared SSH keys. Primary use: inspect deployments, read logs, and debug
on-device model serving (e.g. verify an ONNX/RF-DETR model loaded and is
producing inference) directly from the portal.

## 2. Why

Today, connecting to a device to debug requires out-of-band SSH access, which is
awkward for NAT'd / field devices and inconsistent across environments (EC2
Ubuntu, on-prem Jetson, etc.). A one-click browser terminal removes that
friction and keeps the workflow inside the portal.

## 3. Underlying mechanism — AWS IoT Secure Tunneling

Greengrass devices can be reached over **AWS IoT Secure Tunneling**, which
brokers an SSH (or arbitrary TCP) connection to a device with no inbound ports:

- Device runs the **`aws.greengrass.SecureTunneling`** managed component, which
  starts the AWS IoT local proxy in *destination* mode and connects the tunnel
  to a local service (e.g. `SSH` on `127.0.0.1:22`).
- The portal (source side) calls `iot:OpenTunnel` to create a tunnel and gets a
  short-lived **source access token**.
- A source local-proxy (or a browser websocket client) connects with that token;
  traffic is bridged to the device's SSH.

## 4. Configuration surface (user-settable)

The SSH **login user** varies by environment and must be configurable per device
(or per use case), not hardcoded:

- **EC2 (Ubuntu AMI):** `ubuntu`
- **Jetson / on-prem:** device-specific (`nvidia`, a custom user, etc.)
- **Greengrass default:** `ggc_user` exists on every core (the user Greengrass
  runs components as) and is a reasonable **default**, though it may lack a login
  shell/keys on some images.

Store a per-device `ssh_user` setting (default `ggc_user`) with an override field
in the connect dialog. Optionally a per-use-case default.

## 5. Portal UX (future)

- Device detail page → **Connect** button.
- Opens a browser terminal (e.g. xterm.js) in a panel/modal.
- Backend: a Lambda opens the tunnel (`iot:OpenTunnel` with
  `destinationConfig.thingName`, `services=[SSH]`), returns the source token; a
  websocket bridge (API Gateway websocket + local-proxy, or a small ECS/Lambda
  proxy) relays the browser <-> tunnel.
- Show session status, allow disconnect, auto-expire the tunnel.

## 6. Backend / infra work (future)

- `aws.greengrass.SecureTunneling` added to device deployments (opt-in per
  device or a portal toggle).
- Device Greengrass **Token Exchange Service** role: allow the tunnel
  notify/connect actions the component needs; portal Lambda role: `iot:OpenTunnel`,
  `iot:DescribeTunnel`, `iot:CloseTunnel`.
- Websocket relay for the browser terminal (the non-trivial piece — the AWS local
  proxy is a native binary; a browser client must speak the IoT tunneling
  websocket protocol directly, or we proxy through a server-side local-proxy).
- Per-device `ssh_user` setting in the devices table + settings UI.
- Audit-log connect/disconnect events.

## 7. Open questions

1. Browser terminal transport: server-side local-proxy relay vs. a
   browser-native IoT tunneling websocket client.
2. Auth mapping: portal identity -> device SSH user/key. Do we provision an
   ephemeral key per session, or rely on an existing device user?
3. Session limits, timeouts, and concurrency per device.
4. Non-SSH services (e.g. direct access to the flask-app/Triton port for
   inspection) — Secure Tunneling supports multiple services.

## 8. Near-term (manual) alternative

Until the web screen exists, connect via Secure Tunneling + CLI/console SSH — see
docs/connect-to-device.md.
