# Edge MCP server (LocalServer)

The LocalServer component can serve a Model Context Protocol (MCP) endpoint, so
an AI assistant such as Kiro can read a station's state through the same
Backend_API the HMI uses. The MCP server is off by default. When it is on, the
Backend_API serves `POST /mcp` on its existing listeners and forwards each
request to a supervised child process bound to `127.0.0.1:5790`. Every tool
call is checked by the Backend_API's existing request authorizer with the
caller's own credential, and by an MCP-scoped role gate.

Phase 1 delivers read-only tools only: the server lists and accepts only the
Phase-1 rows of the tool table below. Mutating tools arrive in later releases.

The portal has its own MCP server; see the
[Administrator Guide: MCP server (AI assistants)](../edge-cv-portal/ADMIN_GUIDE.md#mcp-server-ai-assistants).
No stdio MCP package is distributed for either server: the edge server is
reached over HTTP at `/mcp`, and the portal's only connection form is remote
OAuth.

> **Known limitation: plaintext-only devices are tunnel-only.** On a device
> with local login enabled but Existing_Token_Auth not configured, the
> Backend_API listens on plaintext 5000 only, so MCP is tunnel-only there; LAN
> access requires the TLS listener (authorization settings present). On such a
> device `/mcp` answers a non-loopback peer with
> `403 mcp_loopback_only`; connect through a port-forward
> ([Connect to the device](#connect-to-the-device)).

## Enable it on a device

Set the component configuration through a deployment configuration merge on
the device's LocalServer component (`aws.edgeml.dda.LocalServer.<variant>`),
for example:

```json
{
  "configurationUpdate": {
    "merge": "{\"McpServerEnabled\":\"true\"}"
  }
}
```

- `McpServerEnabled` starts the MCP child and serves `/mcp`.
- `McpMutatingToolsEnabled` lets the server list and accept mutating tools
  (it requires `McpServerEnabled`). In Phase 1 no mutating tool is delivered,
  so this switch changes nothing yet.
- Only an explicit true value turns a switch on; a value the component cannot
  read means both switches are off.
- The LocalServer re-reads the configuration every 30 s. A change starts,
  stops or restarts the child; no component restart is needed.

### Component configuration keys

| Key | Recipe default | Meaning |
|---|---|---|
| `LocalLoginEnabled` | `"false"` | Local login with synced portal accounts (Local_Session_Token). Needed for the login helper below. |
| `McpServerEnabled` | `"false"` | Start the Edge MCP server child and serve `POST /mcp`. |
| `McpMutatingToolsEnabled` | `"false"` | List and accept mutating tools (requires `McpServerEnabled`). |
| `McpAllowedOrigins` | absent (empty) | Optional, in no recipe's `DefaultConfiguration`. Comma-separated `scheme://host[:port]` values accepted as an `Origin` header. Absent or empty means none: any request with an `Origin` header gets `403 invalid_origin`. Set it only if your client is shown to send an `Origin` header. |

Every recipe variant carries the first three keys in its
`DefaultConfiguration`.

## Connect to the device

| Form | URL | When |
|---|---|---|
| LAN (TLS) | `https://<device-lan-address>:5443/mcp` | Existing_Token_Auth is configured (`authorization_settings.json` present), so the TLS listener runs. |
| Tunnel | `http://127.0.0.1:<forwarded-port>/mcp` (plaintext device, port 5000 forwarded) or `https://localhost:<forwarded-port>/mcp` (TLS device, port 5443 forwarded) | Any device, and the only form on a plaintext device. The Backend_API runs one listener: 5443 TLS when Existing_Token_Auth is configured, otherwise plaintext 5000; forward that one. Forward the LocalServer port through SSH or AWS IoT Secure Tunneling: [Connect to an edge device: Forward the LocalServer port for MCP](connect-to-device.md#forward-the-localserver-port-for-mcp). |

The certificate on 5443 is the device's own certificate
(`/aws_dda/greengrass/v2/device.pem.crt`), which no public authority issued.
Add it to the client machine's trust store; the tunnel to a TLS device serves
the same certificate.

`/mcp` accepts `POST` only, rejects any request that carries an `Origin`
header (`403 invalid_origin`, unless `McpAllowedOrigins` lists it), refuses
bodies above 1 MiB (`413 payload_too_large`), and answers `503 mcp_disabled`
or `503 mcp_unavailable` when the child is off or not running.

## Sign in with a synced account

The edge server accepts the same credentials as the Backend_API. With local
login enabled, sign in with a synced portal account using the login helper:

```bash
eval "$(scripts/dda-edge-mcp-login.sh https://<device>:5443)"
```

- The helper prompts for the username and reads the password with `read -s`,
  so the password never appears in an argument or the environment.
- It prints one `export DDA_EDGE_TOKEN=<token>` line for `eval` and writes no
  file. The token lives 12 hours; when it expires the client sees `401`, and
  you run the helper again.
- For a self-issued certificate, pass `DDA_EDGE_CA_CERT=<the device
  certificate file>`; `DDA_EDGE_INSECURE=1` skips verification (the password
  then goes to whichever host answers at that address).

> **Shell history.** Run the helper under `eval` as shown. Pasting the printed
> `export` line by hand records the token in `~/.bash_history`. If that
> happens, delete the entry with `history -d <number>`, or set
> `HISTCONTROL=ignorespace` and start the pasted line with a space.

## Configure Kiro

```jsonc
// .kiro/settings/mcp.json (workspace) or ~/.kiro/settings/mcp.json (user)
{
  "mcpServers": {
    "dda-edge-line1": {
      "url": "https://<device-lan-address>:5443/mcp",     // TLS listener; or
      // "url": "http://127.0.0.1:5000/mcp",              // plaintext listener through a port-forward
      "headers": { "Authorization": "Bearer ${DDA_EDGE_TOKEN}" },
      "disabled": true,
      "autoApprove": ["edge_whoami", "edge_system_health_get", "edge_registrations_list"]
    }
  }
}
```

1. Add `DDA_EDGE_TOKEN` to Kiro's approved environment variables ("Mcp
   Approved Env Vars"). Kiro expands only approved `${VAR}` references.
2. Set the variable in the shell that launches Kiro with the login helper.
3. Set `disabled` to `false`.

No password, token or client secret is ever written to `mcp.json`. The
repository's `.kiro/settings/mcp.json` carries a disabled `dda-edge-example`
entry with placeholder values.

### Text generation duration

`edge_textgen_generate` may take up to 130 s (the route's own limit is
120 s). Kiro's MCP configuration documents no per-server tool-call timeout
key, so Kiro is expected to wait for the server's answer; a client that does
apply a per-call timeout must raise it to at least 130 s for this server, or
pass `max_tokens` to bound the generation.

## Role gating

The Backend_API first admits or refuses the request with its existing
decision matrix. The MCP server then applies its own role gate per tool:

| Principal | Read-only tools | Mutating tools | Destructive tools |
|---|---|---|---|
| Local_Session_Token, role Operator, UseCaseAdmin or PortalAdmin | allowed | allowed while `McpMutatingToolsEnabled` is true | also need `confirm` equal to the named target |
| Local_Session_Token, any other role (for example DataScientist or Viewer) | allowed | `not_authorized` | `not_authorized` |
| Existing_Token_Auth bearer (no portal role) | allowed | `not_authorized` | `not_authorized` |
| Anonymous (open-access device, loopback only) | allowed | `not_authorized` | `not_authorized` |

With `McpMutatingToolsEnabled` false, every mutating tool is unlisted and a
call answers `mutating_tools_disabled`.

### Open-access devices

On a device where neither local login nor Existing_Token_Auth is configured,
the Backend_API is open to its network today, and the MCP server keeps that
parity for reads. On such a device `/mcp` answers only loopback peers on the
plaintext listener, so the read-only tools are reachable by any process on
the device and by anyone with a port-forward to it. Mutating tools stay
unavailable there in every configuration, because they require a
Local_Session_Token with an Operator role or higher. Enable local login before
enabling MCP if the station's state should not be readable without a sign-in.

### Trust boundary

The MCP child is bound to loopback, which under `network_mode: host` is
reachable by any local process; it never acts on its own authority — every
call carries the caller's credential to the Backend_API, where the existing
authorizer decides — so a direct caller gains nothing it could not get from
the Backend_API itself, but its `/mcp` request gets no proxy `api.access`
record.

## Status and logs

`GET /mcp/status` (authenticated like every Backend_API route; also the
`edge_mcp_status_get` tool) reports the server's state:

```json
{"enabled": true, "mutatingToolsEnabled": false, "listening": true, "endpoint": "/mcp",
 "transport": "streamable-http", "protocolRevisions": ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"],
 "serverVersion": "1.0.0", "deliveredPhases": [1], "componentVersion": "<component version>",
 "features": ["vllm"], "restartCount": 0, "lastExitCode": null, "lastError": null}
```

A crashed child is restarted with backoff (1, 2, 4 … 60 s); `restartCount`,
`lastExitCode` and `lastError` show it. The Backend_API keeps serving while the
child is down.

Every tool completion writes one `mcp.access` record to the LocalServer log
(`event: "mcp tool"`, `channel: "mcp"`, tool, server and client versions,
`request_id`, principal, role, source, outcome, category, status,
`duration_ms`). The underlying route's `api.access` record shares the
`request_id`. The five image tools whose routes read the credential from the
query string (`edge_execution_output_image_get`,
`edge_execution_overlay_image_get`, `edge_execution_node_image_get`,
`edge_capture_input_image_get`, `edge_capture_output_image_get`) produce no
`api.access` record; correlate them through `mcp.access`.

## Tools

Classification is the text each tool's description ends with. Results are
shaped before they reach the client:

- Path-like fields (local file paths) come back as file names only; the result
  names them in `scrubbed_fields`, and fields a tool drops in `dropped_fields`.
- List tools take `limit` (50 by default, at most 200 per call) unless the
  table names a narrower bound.
- A result is bounded at 200 000 bytes of JSON; a list is cut to the items
  that fit (`truncated: true`), and a larger single object answers
  `result_too_large`.
- Video pin upload and results export are available in the HMI only.

Phase 1 is delivered; Phase 2 and Phase 3 rows are listed for reference and are
neither listed nor accepted by the server until their release.

| Tool | Phase | Classification | Result shaping and limits |
|---|---|---|---|
| `edge_camera_feature_bounds_get` | 1 | Read-only. | - |
| `edge_cameras_list` | 1 | Read-only. | - |
| `edge_capture_input_image_get` | 1 | Read-only. | returns the image; no `api.access` record (correlate through `mcp.access`) |
| `edge_capture_output_image_get` | 1 | Read-only. | returns the image; no `api.access` record (correlate through `mcp.access`) |
| `edge_execution_get` | 1 | Read-only. | - |
| `edge_execution_log_get` | 1 | Read-only. | returns the newest part of the Run_Log: at most `tail_chars` characters (default 150 000), fewer when their JSON encoding would exceed the result bound; `truncated` and `total_chars` say when it cut |
| `edge_execution_metadata_get` | 1 | Read-only. | - |
| `edge_execution_node_image_get` | 1 | Read-only. | returns the image; no `api.access` record (correlate through `mcp.access`) |
| `edge_execution_node_status_get` | 1 | Read-only. | - |
| `edge_execution_output_image_get` | 1 | Read-only. | returns the image; no `api.access` record (correlate through `mcp.access`) |
| `edge_execution_overlay_get` | 1 | Read-only. | returns `maskImage` as one image (`ImageContent`) |
| `edge_execution_overlay_image_get` | 1 | Read-only. | returns the image; no `api.access` record (correlate through `mcp.access`) |
| `edge_execution_results_get` | 1 | Read-only. | - |
| `edge_feature_configurations_list` | 1 | Read-only. | - |
| `edge_gpu_status_get` | 1 | Read-only. | - |
| `edge_image_source_configurations_get` | 1 | Read-only. | - |
| `edge_image_source_get` | 1 | Read-only. | - |
| `edge_image_source_preview` | 1 | Read-only. | returns `image` as one image (`ImageContent`) |
| `edge_image_source_stream_health_get` | 1 | Read-only. | - |
| `edge_image_source_test_connection` | 1 | Read-only. | returns `image` as one image (`ImageContent`) |
| `edge_image_sources_list` | 1 | Read-only. | - |
| `edge_input_configurations_list` | 1 | Read-only. | - |
| `edge_mcp_status_get` | 1 | Read-only. | - |
| `edge_output_configurations_list` | 1 | Read-only. | - |
| `edge_registration_continuous_status_get` | 1 | Read-only. | - |
| `edge_registration_executions_list` | 1 | Read-only. | `limit` 50 by default, at most 50 per call |
| `edge_registration_get` | 1 | Read-only. | drops `executions` |
| `edge_registration_graph_get` | 1 | Read-only. | - |
| `edge_registrations_list` | 1 | Read-only. | - |
| `edge_result_get` | 1 | Read-only. | returns `maskImage` as one image (`ImageContent`) |
| `edge_results_list` | 1 | Read-only. | drops `results[].maskImage`; `limit` 50 by default, at most 100 per call |
| `edge_results_summary_get` | 1 | Read-only. | - |
| `edge_static_image_pin_get` | 1 | Read-only. | - |
| `edge_static_video_pin_get` | 1 | Read-only. | - |
| `edge_stream_viewers_get` | 1 | Read-only. | - |
| `edge_streams_capabilities_get` | 1 | Read-only. | - |
| `edge_system_component_status_get` | 1 | Read-only. | - |
| `edge_system_greengrass_components_list` | 1 | Read-only. | - |
| `edge_system_health_get` | 1 | Read-only. | - |
| `edge_system_station_get` | 1 | Read-only. | drops `logoImage` |
| `edge_textgen_generate` | 1 | Read-only. | listed only when the `vllm` feature is present; may take up to 130 s (see "Text generation duration") |
| `edge_textgen_models_list` | 1 | Read-only. | listed only when the `vllm` feature is present |
| `edge_whoami` | 1 | Read-only. | - |
| `edge_workflow_capture_task_get` | 1 | Read-only. | - |
| `edge_workflow_get` | 1 | Read-only. | - |
| `edge_workflow_images_list` | 1 | Read-only. | drops `images[].image`, `images[].inferenceResult.mask_image`; `limit` 2 by default, at most 2 per call |
| `edge_workflows_list` | 1 | Read-only. | - |
| `edge_camera_connect` | 2 | MUTATING. | - |
| `edge_cameras_rescan` | 2 | MUTATING. | - |
| `edge_image_source_capture` | 2 | MUTATING. | returns `image` as one image (`ImageContent`); returns the captured frame, but no file identifier |
| `edge_results_summary_reset` | 2 | MUTATING, DESTRUCTIVE — requires confirm=workflow_id. | - |
| `edge_results_update` | 2 | MUTATING. | - |
| `edge_static_image_pin` | 2 | MUTATING. | accepts about 700 KiB of image at most; larger images are pinned in the HMI |
| `edge_static_image_unpin` | 2 | MUTATING, DESTRUCTIVE — requires confirm='static-image'. | - |
| `edge_static_video_unpin` | 2 | MUTATING, DESTRUCTIVE — requires confirm='static-video'. | - |
| `edge_system_snapshot_create` | 2 | MUTATING, DESTRUCTIVE — requires confirm='snapshot'. | - |
| `edge_camera_disconnect` | 3 | MUTATING, DESTRUCTIVE — requires confirm=camera_id. | - |
| `edge_camera_features_apply` | 3 | MUTATING. | - |
| `edge_image_source_add` | 3 | MUTATING. | takes the stream address as `stream_url` (RTSP/RTMP only; folder sources are HMI-only) |
| `edge_image_source_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=image_source_id. | - |
| `edge_image_source_update` | 3 | MUTATING. | takes the stream address as `stream_url` (RTSP/RTMP only; folder sources are HMI-only) |
| `edge_model_start` | 3 | MUTATING. | - |
| `edge_model_stop` | 3 | MUTATING, DESTRUCTIVE — requires confirm=model_name. | - |
| `edge_registration_continuous_pause` | 3 | MUTATING, DESTRUCTIVE — requires confirm=registration_id. | - |
| `edge_registration_continuous_resume` | 3 | MUTATING, DESTRUCTIVE — requires confirm=registration_id. | - |
| `edge_registration_trigger` | 3 | MUTATING. | - |
| `edge_result_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=capture_id. | - |
| `edge_system_restart_dda` | 3 | MUTATING, DESTRUCTIVE — requires confirm='restart-dda'. | - |
| `edge_workflow_create` | 3 | MUTATING. | - |
| `edge_workflow_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=workflow_id. | - |
| `edge_workflow_retry` | 3 | MUTATING, DESTRUCTIVE — requires confirm=workflow_id. | - |
| `edge_workflow_run` | 3 | MUTATING. | drops `inferenceResult.mask_image` |
| `edge_workflow_update` | 3 | MUTATING, DESTRUCTIVE — requires confirm=workflow_id. | - |
