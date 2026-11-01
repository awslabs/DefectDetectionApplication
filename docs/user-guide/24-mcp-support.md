# 24. MCP support

**Who:** every signed-in Portal user; each tool needs the same permission as the matching Portal page, so an assistant can read only what you can read in the web UI. **Video:** [Module 14: MCP support](https://youtu.be/0vL4eq0mcgM). **Reference:** [Administrator Guide, MCP server (AI assistants)](../../edge-cv-portal/ADMIN_GUIDE.md#mcp-server-ai-assistants), [Edge MCP server](../edge-mcp-server.md).

The Model Context Protocol (MCP) lets an AI assistant such as Kiro work with DDA through tools: "list the devices of the cookies use case", "show the last training job's logs", "which workflows are deployed to this station". The assistant calls the tools as you, with the credentials you already have, and every call goes through the same role and permission checks as the Portal. In this release every tool is read-only: an assistant can look things up, but it cannot create, change, deploy or delete anything.

## 24.1 The two MCP servers

| Server | Where it runs | Availability | What it offers |
|--------|---------------|--------------|----------------|
| **Portal MCP server** | In the cloud, deployed with the Portal | On by default | Read-only tools across data, models, devices, workflows and administration, grouped into views ([24.4](#244-what-the-tools-can-read)). |
| **Edge MCP server** | On an edge device, inside the LocalServer | Off by default, enabled per device | Read-only tools for the station's state ([24.6](#246-the-edge-mcp-server)). |

## 24.2 Connect Kiro to the Portal MCP server

Ask your PortalAdmin for three values from the Portal's deployment: the **server URL**, the **client id** and the **OAuth scope**. None of them is a secret. Then add a server entry to `.kiro/settings/mcp.json` in your workspace, or to `~/.kiro/settings/mcp.json` for every workspace:

```jsonc
{
  "mcpServers": {
    "dda-portal": {
      "url": "https://<api-id>.execute-api.<region>.amazonaws.com/mcp",
      "oauth": {
        "clientId": "<client-id>",
        "redirectUri": "http://localhost:7899/oauth/callback",
        "oauthScopes": ["https://<api-id>.execute-api.<region>.amazonaws.com/mcp/portal.tools"]
      },
      "disabled": false
    }
  }
}
```

| Field | What to enter |
|-------|---------------|
| **url** | The server URL, ending in `/mcp` for every tool, or a view URL ([24.4](#244-what-the-tools-can-read)) for one area's tools. |
| **clientId** | The client id from your PortalAdmin. |
| **redirectUri** | `http://localhost:7899/oauth/callback`, exactly as shown. |
| **oauthScopes** | The OAuth scope from your PortalAdmin: the server URL followed by `/portal.tools`. |

No password, token or client secret goes into `mcp.json`. The same entry works for Kiro IDE and Kiro CLI. To use a view as well, add it as a second server entry with its own name and the view URL; Kiro signs each entry in on its own.

## 24.3 Sign in

1. Enable the server entry in Kiro (or start Kiro CLI). Kiro opens the Portal's sign-in page in your browser.
2. Sign in with your Portal username and password, as on the Portal itself.
3. The browser returns to Kiro, and the server's tools appear in Kiro's MCP panel.

- Kiro keeps the sign-in and renews it by itself for up to 30 days; after that it asks you to sign in again. When a Portal account is disabled, its tool calls stop working within the hour.
- Only one Kiro window or Kiro CLI session can run the sign-in at a time, because the sign-in uses local port 7899. Sign in from one and let the others reuse it.
- Kiro CLI gives the browser sign-in one minute. If it times out, retry.
- Kiro asks you to approve the first call of a tool, even when the tool is listed under `autoApprove` in the server entry.

Other sign-in messages are explained in the [Administrator Guide, Troubleshooting MCP sign-in](../../edge-cv-portal/ADMIN_GUIDE.md#troubleshooting-mcp-sign-in).

## 24.4 What the tools can read

The server URL ending in `/mcp` offers every tool. Each view URL offers one area's tools, which keeps the list short when you work in one area:

| View | URL ending | What the tools read | Read-only tools |
|------|------------|---------------------|-----------------|
| **data** | `/mcp/data` | Buckets, datasets and folders, captures, labeling jobs and teams, your own labeling tasks, pre-labeled datasets and synthetic-data sessions. | 23 |
| **models** | `/mcp/models` | Models, training jobs and their logs, compilations, model types and format checks. | 11 |
| **devices** | `/mcp/devices` | Devices, their cameras, components, deployments, logs, registrations and pinned images and videos. | 19 |
| **workflows** | `/mcp/workflows` | Workflows and versions, test datasets and runs, custom nodes and plugins, builds and build servers. | 26 |
| **admin** | `/mcp/admin` | Use cases, users and their permissions, edge-sync devices, the audit log, and `portal_admin_whoami`. | 8 |

- Tool names start with `portal_` and the view, then say what they read, for example `portal_devices_list`, `portal_models_training_logs_get` or `portal_workflows_test_runs_list`.
- `portal_admin_whoami` works for every signed-in user and shows the account the server signed you in as, your account-level role and the use cases you can reach. Try it first after signing in.
- Tools that read inside a use case take the use case id, so name the use case in your request ("in the cookies use case …"). The assistant can find the id with `portal_admin_usecases_list`.
- Most list tools return 50 items by default and at most 200 per call.
- A tool answers **not authorized** when your role does not allow the same read in the Portal. If a tool is refused although the Portal page works for you, ask your PortalAdmin; some account-wide tools depend on how your role is recorded ([Administrator Guide, Roles and the registry-enforcement flag](../../edge-cv-portal/ADMIN_GUIDE.md#roles-and-the-registry-enforcement-flag)).

Tools that create, change or delete are planned for later releases; the server neither lists nor accepts them today. The full tool list, with the permission each tool needs, is in the [Administrator Guide, MCP tools](../../edge-cv-portal/ADMIN_GUIDE.md#mcp-tools).

## 24.5 MCP calls in the Audit Logs

Every Portal MCP tool call is recorded in the audit trail, like the actions you take in the web UI. In **Audit Logs** ([21](21-audit-logs.md)), set **Channel** to **MCP** to see only those entries; **Web & API** shows the rest.

| Field | MCP entry |
|-------|-----------|
| **Action** | `mcp_tool_call` for one tool call; `authentication_failed` for a sign-in token the server refused (for example an expired one). |
| **Result** | `success`, `failure`, or `denied` when the call was refused. |
| **Details** | The tool name, the use case, how long the call took, and the ids the tool was called with. |

Calls to the edge MCP server are logged on the device, not in the Portal's Audit Logs.

## 24.6 The edge MCP server

Each edge device can also serve its own MCP server from the LocalServer, so an assistant can read that station's state: cameras and image sources, workflows and their runs, results and images, and the system's health. It is off until it is enabled for the device through a deployment configuration change. It offers read-only tools only.

- Connect to it on the device's local network, or through a port forward to the device ([Connect to an edge device](../connect-to-device.md#forward-the-localserver-port-for-mcp)).
- On a device with local login enabled, sign in with your Portal account synced to the device ([20.3](20-user-manager.md#203-edge-account-sync)), through the login helper the edge guide describes.
- Every call is checked by the device with your own credential, as in the device web UI ([22](22-device-web-ui.md)).

Enabling the server, the connection addresses, the Kiro configuration and the tool list are in [Edge MCP server](../edge-mcp-server.md).
