# Requirements Document

## Introduction

This feature adds Model Context Protocol (MCP) servers to the DefectDetectionApplication so that AI assistants and agents (Kiro and any other MCP-capable client) can operate the system through tools instead of hand-written REST calls: (A) MCP server(s) for the cloud Portal (edge-cv-portal: use cases, devices, deployments, models, datasets, labeling, training, compilation, workflows, components, builds, custom nodes, audit logs) and (B) an MCP server for the edge deployment (the on-device LocalServer: workflows and registrations, executions and results, cameras and image sources, streams, system health, feature and model configuration, text generation where available).

The owner's governing requirement is that both servers use the existing credential systems for access. On the Portal that means Amazon Cognito user-pool tokens validated by the Portal_Authorizer and authorization decided by the existing RBAC_Layer against the Portal_Identity_Registry, with every action recorded in the existing Audit_Log. On the edge that means the LocalServer's Request_Authorizer decision matrix: Local_Session_Tokens issued by the Local_Login endpoint against the Local_Credential_Cache that the Account_Sync_Service provisions from the Portal, plus the pre-existing Existing_Token_Auth bearer path. No new identity store, no new authentication scheme, no API keys in plaintext configuration, and no path on which RBAC or audit logging does not apply.

This document states what the MCP servers must do from the owner's perspective. Architectural choices (topology, transport, hosting, Python packaging, token mapping, phasing) are deliberately left open and are listed in "Open Design Decisions" for the design phase to resolve.

## Prior Work Check

Performed before writing, as the owner requested ("there may have been some spec work done in prep for this, please check first before starting over"):

- `.kiro/specs/mcp-servers/` did not exist. No directory under `.kiro/specs/` (155 specs) has a name suggesting MCP work.
- A text search for "MCP" / "Model Context Protocol" across `.kiro/`, `docs/`, the repository source tree (excluding `node_modules`, virtualenvs, `cdk.out`, `.git`), and the task-notes directories kept beside the repository found exactly one text hit: `.kiro/settings/mcp.json`. That file is Kiro *client* configuration for three third-party stdio servers (`awslabs.aws-documentation-mcp-server`, `awslabs.cdk-mcp-server`, `awslabs.aws-diagram-mcp-server`, launched with `uvx`, with `env`, `disabled`, and `autoApprove` keys). It contains no requirements, design notes, or decisions about MCP servers for this product. It is reused here only as the in-repo example of Kiro's stdio `mcp.json` shape (Requirement 15).
- The repository root has no task-notes directory, and the task-notes directories beside the repository contain no MCP task or notes directory. No git branch or commit message mentions MCP.

Conclusion: no prior MCP spec work exists; this spec starts from scratch. The following existing specs were read and are treated as the baseline this spec builds on (not re-specified here): `portal-user-manager` (Local_Login, Local_Session_Token, Request_Authorizer decision matrix, Account_Sync_Service), `portal-jwt-role-privilege-escalation` (Portal_Identity_Registry, `PORTAL_REGISTRY_ENFORCED`, durable audit attribution), `security-secrets-credentials-jwt-fixes` and `security-iam-authorization-fixes` (no secrets in logs or shell strings, least-privilege IAM, documented exceptions for intentional LAN binds), `station-quick-setup` (the Setup_Token and per-IP rate limiter are one-time device-provisioning mechanisms, not a reusable API credential), and `custom-node-designer` / `portal-user-manager` for document format.

## Research Baseline (verified 2026-10-18 against primary sources)

Recorded here because the acceptance criteria below depend on these facts; the design document will cite them with full references.

- MCP specification: the current protocol revision is **2026-07-28** (modelcontextprotocol.io/specification/versioning). Standard transports are **stdio** and **Streamable HTTP** (single MCP endpoint accepting POST; replies as a JSON object or a request-scoped SSE stream). The 2026-07-28 revision is stateless: no `initialize` handshake and no `Mcp-Session-Id`; every request carries its protocol version and client capabilities in `_meta`; servers must implement `server/discover`; every Streamable HTTP POST must carry an `MCP-Protocol-Version` header matching the body. Servers must validate the `Origin` header (403 on invalid), should bind local servers to 127.0.0.1, and should authenticate all connections. The legacy **HTTP+SSE transport is Deprecated** (since 2025-03-26, reclassified under SEP-2596). Backward compatibility with handshake-based revisions (2025-11-25 and earlier) is defined in the spec's Versioning section. Tool definitions carry `annotations` with `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` (schema 2026-07-28), which clients must treat as untrusted unless the server is trusted.
- MCP authorization (spec section "Authorization", 2026-07-28): authorization is optional; HTTP-based transports SHOULD conform; **stdio transports SHOULD NOT implement it and instead retrieve credentials from the environment**. Conforming HTTP servers act as OAuth 2.1 resource servers: they MUST implement **OAuth 2.0 Protected Resource Metadata (RFC 9728)**, return `401` with `WWW-Authenticate: Bearer resource_metadata="..."` (optionally `scope=`), accept tokens only in the `Authorization: Bearer` header (never in the URI), **MUST validate the token audience per RFC 8707** (clients MUST send the `resource` parameter), MUST NOT accept or pass through tokens issued for other resources, and return 401 for invalid/expired tokens and 403 for insufficient scope. Authorization servers must expose RFC 8414 or OpenID Connect Discovery metadata. Client registration is by Client ID Metadata Documents, pre-registration, or (deprecated) Dynamic Client Registration.
- Official Python SDK: package **`mcp`** on PyPI; latest stable **2.3.0** (released 2026-10-02) with a maintained 1.x line (**1.30.0**, 2026-09-07). Both require **Python >= 3.10**. Dependency floors that matter for this repo: 2.3.0 requires `pydantic>=2.12.0`, `httpx2>=2.10.0`, `jsonschema>=4.20.0`, `uvicorn>=0.31.1`, `starlette>=0.27`, `sse-starlette>=3.0.0`, `pyjwt[crypto]>=2.10.1`; 1.26.0-1.30.0 require `pydantic>=2.11.0,<3`, `httpx>=0.27.1`, `jsonschema>=4.20.0`, `uvicorn>=0.31.1`. The LocalServer pins `fastapi==0.109.2`, `uvicorn==0.23.2`, `pydantic==2.6.4`, `httpx==0.24.1`, `jsonschema==4.17.3` in `src/backend/requirements.txt`; every `mcp` release checked (1.0.0, 1.2.0, 1.6.0, 1.9.0, 1.12.0, 1.20.0, 1.25.0, 1.26.0, 2.3.0; the 1.0.0 floor is already `pydantic>=2.8.0`, `httpx>=0.27`) conflicts with at least the `pydantic` and `httpx` pins.
- Kiro MCP configuration (kiro.dev/docs/mcp/configuration): `mcp.json` at workspace level `.kiro/settings/mcp.json` or user level `~/.kiro/settings/mcp.json` (merged, workspace wins). Local servers use `command`, `args`, `env` (with `${VAR}` expansion), `disabled`, `autoApprove`, `disabledTools`. Remote servers use `url` plus optional `headers`, `oauth` (`clientId`, `clientSecret` (CLI only; the IDE supports public PKCE clients only), `redirectUri` (loopback `http://localhost|127.0.0.1:<port>/path`), `clientMetadataUrl`, `oauthScopes`). Kiro documents Cognito as a supported authorization server for pre-registered clients, requires exact RFC 8414 issuer matching between protected-resource metadata and the authorization-server metadata, and expects the MCP server to validate Bearer tokens against the provider's JWKS.

## Existing Credential and Capability Systems (as read from the code)

Portal (`edge-cv-portal/`):
- The Portal_API is an API Gateway REST API (`infrastructure/lib/api-gateway-stack.ts`, stage `v1`) whose methods use a native Cognito User Pools authorizer (`apigateway.CognitoUserPoolsAuthorizer`, `identitySource: method.request.header.Authorization`). New route groups are added as nested stacks that import the REST API by id (`user-admin-api-stack.ts`, `camera-registry-api-stack.ts`, `node-designer-api-stack.ts`) because the main API stack sits at the CloudFormation 500-resource limit. The custom Lambda authorizer `backend/functions/jwt_authorizer.py` (RS256, JWKS, `aud` in `ALLOWED_AUDIENCES`, `token_use == 'id'` for the pool issuer, optional `ISSUER_WHITELIST`) is defined in `compute-stack.ts` but is not attached to any method.
- The frontend (`frontend/src/services/api.ts`) authenticates with the Cognito **ID token** (`Authorization: Bearer <idToken>`, refreshed through Amplify). The User_Pool (`auth-stack.ts`) allows only the SRP flow (`authFlows: { userSrp: true }`, no `USER_PASSWORD_AUTH`), has a hosted-UI domain with the authorization-code grant and `openid email profile` scopes, a public client (`generateSecret: false`), and `custom:role` / `custom:groups` attributes. Password policy: 12+ chars, mixed case, digits, symbols.
- Authorization inside Lambdas: `rbac_middleware.rbac_check([Permission...], usecase_param, allow_global)` and `super_user_only`, backed by `shared_utils.RBACManager` (`Role` = Viewer, Operator, DataScientist, UseCaseAdmin, PortalAdmin, DataLabeler; `Permission` enum incl. `view_*`, `create_deployments`, `restart_devices`, `workflow:*`, `node-designer:*`, `builds:*`, `view_audit_logs`, `manage_users`). Privilege comes from the Portal_Identity_Registry (`USER_ROLES_TABLE`, keyed `user_id`/`usecase_id`, `'global'` scope) when `PORTAL_REGISTRY_ENFORCED` is on; the JWT `custom:role` is Claimed_Role (descriptive only). Denials return 403 and write an `unauthorized_access` Audit_Log entry; registry outages return 500 and write `authorization_unavailable`.
- Audit logging: `shared_utils.log_audit_event(user_id, action, resource_type, resource_id, result, details, identity=attribution_from(event, user))` writes to `AUDIT_LOG_TABLE` with 90-day TTL and the Attribution_Fields (`username`, `email`, `source_ip`, `user_agent`, `identity_source`); `record_audit_event_strict` / `finalize_audit_event` implement audit-before-effect; `sanitize_audit_details` denylists password/verifier/hash/temp* keys. `audit_logs.py` serves `GET /audit-logs` to `view_audit_logs` holders.
- Rate limiting today is API Gateway stage throttling (per-method for Quick Setup routes) plus the Quick-Setup-only per-IP invalid-token limiter (`rate_limiter.py`). `token_service.py` issues one-time `dqs1.*` Setup_Tokens for device provisioning only.
- Lambdas run on `PYTHON_3_11` with the shared layer (`layers/shared/python/shared_utils.py`, `auth_middleware.py`, `user_roles_dao.py`) and the JWT layer (`layers/jwt/requirements.txt`).

Edge (`src/backend/`):
- The LocalServer backend is a **FastAPI** application served by uvicorn (`app.py`), not Flask (the compose service image is historically named `flask-app`). It listens on `0.0.0.0:5000` (plaintext) when `/aws_dda/authorization_settings.json` is absent and on `0.0.0.0:5443` with TLS (`device.pem.crt` / `private.pem.key`) when it is present. The container runs with `network_mode: host` and `privileged: true` (`src/docker-compose.yaml`), so any new listener is reachable on the device LAN. Images: JP5 `python3.11`, JP6 `python3.10`, JP7 `python3.11`, amd64/x86_64_nvidia `python3.11`.
- Authentication: `utils/auth.py:authorize_request` is attached to every router built by `endpoints/route/access_log_router.py:get_api_router()`; it applies the decision matrix (Local_Login_Configuration × Existing_Token_Auth): open access when both are off; Local_Session_Token (HMAC-SHA256 two-segment token, 12-hour lifetime, re-validated against the Local_Credential_Cache on every request so disabled accounts are revoked immediately, `local_auth/session_tokens.py`) when local login is enabled; remote OAuth introspection (`validate_remotely` with `clientId`/`clientSecret`/`introspectEndpoint` from the settings file) when Existing_Token_Auth is configured. Exempt paths: `/local-auth/login`, `/local-auth/status`, and `/static/*` (explicit exemptions in `utils/auth.py`), plus `/health` and the `/hmi` static mount, whose routers carry no auth dependency. `download_file.py` accepts the same credential as a query parameter for browser downloads.
- Login: `POST /local-auth/login` (`endpoints/local_auth.py`) verifies username/password against `/aws_dda/local_credential_cache.json` (PBKDF2 verifiers synced by `user_accounts_sync/agent.py` over the `dda-user-accounts` named shadow), applies lockout (5 failures → 15 minutes), and returns `{token, expiresAt, role, username}`. The Local_Session_Token payload carries `sub` and `role` (the synced Portal_Role), but **no LocalServer endpoint enforces roles today**: authentication only.
- Logging: per-request structured access log (`api.access`: method, path, status, client ip, duration, request id) with no principal attribution; no edge audit table.
- Capability surface the Edge_MCP_Server would wrap (routes in `endpoints/*.py` and `workflow_engine/api.py`): `/workflows`, `/workflows/{id}` (CRUD, `run`, `retry`, `images`, `capture-task`), `/workflows/registrations/*` (list, graph, executions, `trigger`, `continuous/pause|resume`), `/workflows/executions/*` (results, overlay, log, node-status, metadata), `/workflows/{id}/results*` (summary, list, reset, delete, patch, export), `/cameras*` (list, rescan, connect, disconnect, features), `/image-sources*` (CRUD, test-connection, stream-health, preview, capture), `/captured-images`, `/streams/*` (subscribe, frame, heartbeat, viewers, settings, capabilities), `/static-image-camera/pin`, `/static-video-camera/pin`, `/system-health`, `/dda-component-status`, `/greengrass-components`, `/snapshot`, `/station`, `/restart-dda`, `/feature-configurations*` (incl. model start/stop, gpu-status), `/input-configurations`, `/output-configurations`, `/text-generation/*` (vLLM images only), `/authorization-configurations`, `/health`.

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend behind API Gateway, DynamoDB storage) used to manage DDA use cases, models, data, labeling, training, components, deployments, devices, and workflows.
- **Portal_API**: The Portal's existing REST API (API Gateway REST API, stage `v1`) and its Lambda handlers under `edge-cv-portal/backend/functions/`.
- **Portal_Authorizer**: The existing Cognito User Pools authorizer on the Portal_API that rejects requests without a valid User_Pool token before any handler runs.
- **User_Pool**: The Amazon Cognito user pool `dda-portal-users` that stores Portal accounts and issues the JWT tokens the Portal accepts.
- **Portal_Role**: One of the defined Portal roles: PortalAdmin, UseCaseAdmin, DataScientist, Operator, Viewer, DataLabeler.
- **Permission**: An entry of the existing `Permission` enum in `shared_utils` (for example `view_devices`, `create_deployments`, `workflow:deploy`, `view_audit_logs`) that the RBAC_Layer grants per Portal_Role and Use_Case.
- **RBAC_Layer**: The existing authorization code path (`rbac_middleware.rbac_check` / `super_user_only` over `shared_utils.RBACManager`) that resolves a caller's effective Portal_Role from the Portal_Identity_Registry and decides whether a Permission is held within a Use_Case or at the `global` scope.
- **Portal_Identity_Registry**: The existing `USER_ROLES_TABLE` rows (keyed by Cognito `sub` and Use_Case or `global`) that grant Portal privilege when `PORTAL_REGISTRY_ENFORCED` is on; the token's `custom:role` is Claimed_Role only.
- **Use_Case**: The existing Portal tenancy unit (per-account production line) to which models, devices, workflows, and role assignments are scoped.
- **Audit_Log**: The existing Portal audit DynamoDB table and the `log_audit_event` / `record_audit_event_strict` writers, including the Attribution_Fields (username, email, source_ip, user_agent, identity_source).
- **LocalServer**: The Greengrass component (`aws.edgeml.dda.LocalServer.<arch>`) running on an edge device: the FastAPI Backend_API in the `flask-app` container plus the local web UI, embedding Triton and executing workflows.
- **Backend_API**: The LocalServer's FastAPI HTTP API on port 5000 (plaintext, authorization settings absent) or 5443 (TLS, authorization settings present).
- **Request_Authorizer**: The existing per-request dependency `utils/auth.py:authorize_request` that applies the (Local_Login_Configuration × Existing_Token_Auth) decision matrix to every Backend_API route.
- **Local_Login_Configuration**: The existing Greengrass component configuration key `LocalLoginEnabled` (default disabled) that turns Local_Login on or off per device.
- **Local_Credential_Cache**: The existing on-device store `/aws_dda/local_credential_cache.json` of synced account records (username, email, Portal_Role, enabled flag, PBKDF2 verifier).
- **Account_Sync_Service**: The existing Portal-to-device account delivery over the `dda-user-accounts` named IoT shadow (`user_admin.py` / `account_sync.py` on the Portal, `user_accounts_sync/agent.py` on the device).
- **Local_Session_Token**: The existing HMAC-signed 12-hour token issued by `POST /local-auth/login`, carrying `sub` (username) and `role` (synced Portal_Role).
- **Existing_Token_Auth**: The LocalServer's pre-existing bearer-token mechanism, enabled by the presence of `/aws_dda/authorization_settings.json`, validated by remote OAuth token introspection.
- **Access_Log**: The LocalServer's existing structured per-request log (`api.access`) and application log.
- **MCP**: The Model Context Protocol, current revision 2026-07-28, as published at modelcontextprotocol.io.
- **MCP_Client**: Any MCP-conforming client application (Kiro IDE/CLI, other assistants, test harnesses) that connects to an MCP server and invokes tools on behalf of a human user.
- **MCP_Transport**: One of the MCP standard transport bindings: stdio (client-launched subprocess) or Streamable HTTP (single HTTP endpoint).
- **Portal_MCP_Server**: The MCP server (or set of servers; see Open Design Decision 1) introduced by this feature that exposes Portal capabilities as MCP tools.
- **Edge_MCP_Server**: The MCP server introduced by this feature that exposes LocalServer capabilities as MCP tools on or alongside an edge device.
- **MCP_Tool**: A named, described, schema-typed operation exposed by an MCP server and invoked by an MCP_Client.
- **Read_Only_Tool**: An MCP_Tool whose invocation changes no Portal or device state (for example list devices, get workflow, read results).
- **Mutating_Tool**: An MCP_Tool whose invocation creates, modifies, deletes, deploys, starts, stops, restarts, or otherwise changes Portal or device state.
- **Tool_Catalog**: The authoritative list of MCP_Tools exposed by an MCP server, each with its classification (Read_Only_Tool or Mutating_Tool), the Permission(s) it requires, the existing Portal_API handler or Backend_API route it exercises, and its MCP annotations.
- **Principal**: The authenticated human user on whose behalf an MCP_Tool is invoked: a User_Pool account (Portal) or a synced account in the Local_Credential_Cache or an Existing_Token_Auth bearer (edge).
- **MCP_Audit_Marker**: Attributes on an Audit_Log or Access_Log entry that identify the entry as originating from an MCP tool invocation (at minimum: channel = MCP, tool name, MCP server identity, and the MCP_Client identity when supplied).
- **Protected_Resource_Metadata**: The RFC 9728 document an OAuth 2.1 resource server publishes at its `.well-known/oauth-protected-resource` location so MCP_Clients can discover the authorization server.
- **MCP_Config**: An MCP client configuration file (for Kiro: `.kiro/settings/mcp.json` or `~/.kiro/settings/mcp.json`) that tells the client how to launch or reach an MCP server.
- **Python_SDK**: The official Model Context Protocol Python package `mcp` on PyPI.
- **Security_Preservation_Gate**: The build-time test suite under `test/backend-test/security/preservation/` that pins hashes of `src/docker-compose.yaml`, the backend/frontend/edgemlsdk Dockerfiles, `src/backend/requirements.txt`, recipe variants, and `station_install/setup_station.sh` against baselines in `test/backend-test/security/baselines/` (steering: `.kiro/steering/builds.md`).
- **On_Device_Verification**: The steering rule that any change running on the edge device must be built, deployed (or hot-patched), and exercised end to end on real hardware of every affected architecture before it is committed.

## Requirements

### Requirement 1: Portal Capabilities Exposed as MCP Tools

**User Story:** As a Portal user working in an AI assistant, I want the Portal's capabilities available as MCP tools, so that I can inspect and operate use cases, devices, deployments, models, data, labeling, training, workflows, components, and builds from my assistant without hand-writing REST calls.

#### Acceptance Criteria

1. THE Portal_MCP_Server SHALL expose MCP_Tools covering each Portal functional domain served by the Portal_API: use cases, devices and device registrations, deployments and deployment preflight, device logs, models (registry, import, conversion), compilation, training, datasets and data management, labeling (Ground Truth and DDA labeling), workflows (definitions, validation, test runs, packaging), components and component configuration, build jobs and build fleet, custom node types and plugin records, synthetic data, camera registry, quick setup status, and audit logs.
2. WHEN an MCP_Client requests the tool list, THE Portal_MCP_Server SHALL return every MCP_Tool in its Tool_Catalog with a name, a human-readable description that states what the tool does and which Portal resources it touches, a JSON Schema for its inputs, and MCP annotations consistent with its classification (Read_Only_Tool: `readOnlyHint` true; Mutating_Tool: `readOnlyHint` false, with `destructiveHint` true for tools that delete, roll back, restart, or reboot).
3. THE Portal_MCP_Server SHALL implement every MCP_Tool by exercising the same Portal_API handler logic, data access, and validation that the Portal web UI uses for the equivalent action, and SHALL NOT introduce a data path, privilege, or side effect that the Portal_API does not already provide to a user of the same Portal_Role.
4. WHEN a Mutating_Tool is invoked, THE Portal_MCP_Server SHALL return the identifier of the created or modified resource (or the job identifier for long-running operations such as training, compilation, builds, packaging, and deployments) so that follow-up tools can observe its status.
5. WHEN an MCP_Tool invocation fails validation, authorization, or execution, THE Portal_MCP_Server SHALL return an MCP tool error whose message identifies the failing input or the reason category (not authorized, not found, invalid input, backend failure) without exposing stack traces, internal identifiers of other Use_Cases, or credential material.
6. WHEN a Read_Only_Tool returns a collection, THE Portal_MCP_Server SHALL support pagination or result limits equivalent to the Portal_API's so that no single tool result exceeds a design-specified size bound.
7. THE Portal_MCP_Server SHALL return the tool list in a deterministic order and SHALL include the cache metadata the MCP specification requires for list results.

### Requirement 2: Portal MCP Authentication Reuses the User_Pool

**User Story:** As a Portal administrator, I want every MCP tool invocation on the Portal to be performed as an authenticated Portal user via the existing Cognito user pool, so that MCP introduces no new credential type, no shared service account, and no second place where user identity is managed.

#### Acceptance Criteria

1. THE Portal_MCP_Server SHALL accept, as the only credential for invoking any MCP_Tool, a token issued by the User_Pool for the authenticated Principal, validated with the same checks the Portal_API applies (signature against the User_Pool's signing keys, expiry, issuer, and audience).
2. IF a tool invocation arrives without a credential, with an expired credential, with a credential whose signature does not verify, or with a credential issued by an issuer or for an audience the Portal does not accept, THEN THE Portal_MCP_Server SHALL reject the invocation with an authentication error and SHALL NOT execute the tool.
3. THE Portal_MCP_Server SHALL NOT create, store, or accept API keys, shared secrets, static bearer tokens, or any credential not issued by the User_Pool, and SHALL NOT embed a service identity that acts on behalf of users.
4. WHERE the Portal_MCP_Server is reachable over an HTTP-based MCP_Transport, THE Portal_MCP_Server SHALL conform to the MCP authorization specification for its protocol revision: publish Protected_Resource_Metadata naming the User_Pool as the authorization server, answer unauthenticated requests with HTTP 401 and a `WWW-Authenticate: Bearer resource_metadata="..."` challenge, accept tokens only in the `Authorization` header, and validate that each token was issued for the Portal_MCP_Server as the intended audience.
5. WHERE the Portal_MCP_Server runs locally over the stdio MCP_Transport, THE Portal_MCP_Server SHALL obtain the Principal's User_Pool token only through an authentication flow the User_Pool already allows (the SRP flow or the hosted-UI authorization-code flow with PKCE) or from a short-lived token supplied in the process environment at launch, and SHALL NOT read a long-lived password, refresh token, or token from a plaintext configuration file.
6. WHEN the Principal's token expires during an MCP session, THE Portal_MCP_Server SHALL fail subsequent tool invocations with an authentication error that tells the MCP_Client to re-authenticate (or, where the Principal's existing refresh flow is available to the server, refresh through that flow) and SHALL NOT continue to act under the expired identity.
7. THE Portal_MCP_Server SHALL NOT write token values, authorization headers, refresh tokens, or passwords to any log, audit entry, error message, or tool result.
8. WHEN a Principal's User_Pool account is disabled or deleted, THE Portal_MCP_Server SHALL stop honoring that Principal's tool invocations no later than the User_Pool stops issuing or accepting tokens for the account (that is, the MCP path adds no additional token lifetime beyond what the Portal already grants).

### Requirement 3: Portal MCP Authorization Reuses the RBAC Layer

**User Story:** As a Portal administrator, I want each MCP tool gated by the same roles and permissions as the equivalent portal action, so that an assistant can never do more than the user it acts for.

#### Acceptance Criteria

1. THE Tool_Catalog SHALL declare, for every MCP_Tool, the Permission(s) and scope (Use_Case or `global`) required to invoke it, and THE Portal_MCP_Server SHALL evaluate that declaration through the RBAC_Layer for every invocation.
2. THE Portal_MCP_Server SHALL resolve the Principal's effective Portal_Role exactly as the Portal_API does, including Portal_Identity_Registry enforcement when `PORTAL_REGISTRY_ENFORCED` is on and treatment of the token's `custom:role` as Claimed_Role only.
3. IF a Principal invokes an MCP_Tool without holding a required Permission in the targeted Use_Case (or at the `global` scope for non-Use_Case tools), THEN THE Portal_MCP_Server SHALL deny the invocation with an authorization error, SHALL perform no part of the operation, and SHALL record an `unauthorized_access` Audit_Log entry carrying the MCP_Audit_Marker.
4. IF the Portal_Identity_Registry cannot be read during an authorization check, THEN THE Portal_MCP_Server SHALL fail the invocation with an availability error (not an authorization denial and not a silent downgrade to Viewer) and SHALL record an `authorization_unavailable` Audit_Log entry, matching the Portal_API's behavior.
5. WHEN a Principal holds the Viewer Portal_Role only, THE Portal_MCP_Server SHALL permit only Read_Only_Tools whose Permissions the Viewer role holds, and SHALL deny every Mutating_Tool.
6. WHEN a Principal holds the DataLabeler Portal_Role only, THE Portal_MCP_Server SHALL permit only the labeler self-service tools (Permission `labeling:tasks-self`) and SHALL deny all other MCP_Tools.
7. THE Portal_MCP_Server SHALL restrict MCP_Tools that read audit logs, manage users, manage settings, or approve plugin security reviews to the Portal_Roles that hold those Permissions today (UseCaseAdmin within its own Use_Case and PortalAdmin for `view_audit_logs`; PortalAdmin only for `manage_users`, `manage_settings`, and `node-designer:security-review`).
8. WHEN an MCP_Client requests the tool list, THE Portal_MCP_Server MAY omit tools the Principal cannot invoke; THE Portal_MCP_Server SHALL in all cases enforce authorization at invocation time regardless of what the list contained.
9. THE Portal_MCP_Server SHALL NOT provide any tool, parameter, or mode that elevates the Principal's effective Portal_Role, assumes a different Principal, or invokes a Portal_API handler with a synthesized authorizer context.

### Requirement 4: Portal MCP Tool Invocations Are Audited

**User Story:** As a Portal administrator, I want every MCP tool invocation recorded in the existing audit log with a marker that shows it came through MCP, so that I can trace what assistants did on whose behalf and distinguish it from browser activity.

#### Acceptance Criteria

1. WHEN an MCP_Tool completes (success or failure), THE Portal_MCP_Server SHALL ensure an Audit_Log entry exists for the action using the existing audit schema (user_id, action, resource_type, resource_id, result, details, timestamp) and the Attribution_Fields derived from the MCP request (username, email, source IP where the transport provides one, the MCP_Client's identity string in place of a browser user agent, identity_source).
2. THE Audit_Log entry for an MCP-originated action SHALL carry the MCP_Audit_Marker: at minimum a channel value identifying MCP, the MCP_Tool name, the MCP server identity and version, and the MCP_Client name and version when the client supplied them.
3. WHEN a Mutating_Tool exercises a Portal_API handler that already writes its own Audit_Log entry, THE Portal_MCP_Server SHALL ensure that entry carries the MCP_Audit_Marker rather than writing a second, unmarked entry for the same action.
4. WHEN a Mutating_Tool exercises a Portal_API path that uses audit-before-effect (`record_audit_event_strict`), THE Portal_MCP_Server SHALL preserve that ordering: IF the audit entry cannot be recorded, THEN the operation SHALL NOT be applied and the tool SHALL report the failure.
5. THE Portal_MCP_Server SHALL apply the existing audit details sanitization (denylist of password, verifier, hash, and temp* keys) to every detail it records and SHALL NOT record tool arguments that contain credential material.
6. WHEN a PortalAdmin queries the Audit_Log through the Portal (web UI, `GET /audit-logs`, or an audit MCP_Tool), THE Portal SHALL allow filtering or identification of MCP-originated entries by the MCP_Audit_Marker.
7. THE Portal_MCP_Server SHALL record denied invocations (Requirement 3.3), authentication failures that reach the server with a parseable Principal, and availability failures (Requirement 3.4) in the Audit_Log with the MCP_Audit_Marker.

### Requirement 5: Portal Mutating-Tool Safety

**User Story:** As a Portal administrator, I want state-changing MCP tools clearly classified and safeguarded, so that an assistant acting on a misunderstanding cannot silently deploy, delete, restart, or reboot production resources.

#### Acceptance Criteria

1. THE Tool_Catalog SHALL classify every MCP_Tool as a Read_Only_Tool or a Mutating_Tool, and THE Portal_MCP_Server SHALL expose that classification through MCP tool annotations and in the tool description text.
2. THE Portal_MCP_Server SHALL classify as Mutating_Tools with `destructiveHint` true, at minimum: deleting any resource, rolling back a deployment, restarting or rebooting a device, deleting models or datasets, resetting results, disabling or deleting accounts, and promoting or demoting plugin lifecycle states.
3. WHERE a Mutating_Tool is classified destructive, THE Portal_MCP_Server SHALL require an explicit confirmation input in the tool's arguments (for example a `confirm` flag that names the target resource) and SHALL reject invocations that omit it, so that an MCP_Client cannot perform the action in a single unconsidered step.
4. THE Portal_MCP_Server SHALL provide a server-level setting, controlled by the Portal deployment (CDK context or configuration, not by the MCP_Client), that disables all Mutating_Tools; WHILE that setting is active, THE Portal_MCP_Server SHALL omit Mutating_Tools from the tool list and SHALL reject their invocation.
5. THE Portal_MCP_Server SHALL NOT expose as MCP_Tools any operation that handles raw credential material (setting or resetting passwords, issuing temporary passwords, reading credential verifiers, generating Setup_Tokens, or minting provisioning credentials).
6. WHEN a Mutating_Tool targets a resource in a Use_Case, THE Portal_MCP_Server SHALL require the Use_Case identifier as an explicit tool argument and SHALL NOT infer it from a previous invocation.
7. THE Portal_MCP_Server SHALL apply the Portal_API's existing input validation and guardrails (for example the last-PortalAdmin guard, lifecycle-state gates for custom node deployment, deployment preflight checks) unchanged when the equivalent action is invoked through MCP.

### Requirement 6: Portal MCP Resource Protection and Operational Limits

**User Story:** As a Portal operator, I want MCP traffic bounded and observable, so that a runaway assistant cannot exhaust the portal, incur unbounded AWS cost, or go unnoticed.

#### Acceptance Criteria

1. WHERE the Portal_MCP_Server is reachable over an HTTP-based MCP_Transport, THE Portal SHALL apply request throttling to the MCP endpoint with rate and burst limits set in the Portal infrastructure (as the Quick Setup routes do today), and WHEN limits are exceeded THE Portal SHALL reject requests with the standard throttling response.
2. THE Portal_MCP_Server SHALL complete each tool invocation within the Portal_API's existing handler timeout bounds or return a job identifier for the caller to poll; THE Portal_MCP_Server SHALL NOT block an MCP request on long-running cloud operations (training, compilation, builds, packaging, deployments).
3. THE Portal_MCP_Server SHALL emit the same CloudWatch metrics and structured logs as other Portal Lambda functions (invocation count, errors, duration) and SHALL log, per tool invocation, the Principal identifier, tool name, outcome, and duration without credential material.
4. IF the Portal_MCP_Server is unavailable or misconfigured, THEN THE Portal web UI and the Portal_API SHALL continue to operate unchanged (the MCP server is an additive consumer of the Portal's logic, not a dependency of it).
5. THE Portal_MCP_Server SHALL run under an IAM role that grants only the permissions of the Portal_API handlers whose logic it exercises, created through the existing per-function role pattern, with no wildcard actions.

### Requirement 7: Portal MCP Protocol Compliance and Client Compatibility

**User Story:** As a user of Kiro or another MCP client, I want the Portal MCP server to follow the current MCP specification and remain usable from the clients I already have, so that I do not need custom client code.

#### Acceptance Criteria

1. THE Portal_MCP_Server SHALL implement the MCP protocol revision current at design approval (2026-07-28 as of this document) using the official Python_SDK, including `server/discover`, per-request protocol metadata, and the result fields the revision requires.
2. WHEN an MCP_Client that implements an earlier supported protocol revision connects, THE Portal_MCP_Server SHALL interoperate according to the specification's backward-compatibility rules for the revisions the design declares supported, and SHALL reject unsupported revisions with the specification's protocol-version error.
3. WHERE the Portal_MCP_Server uses Streamable HTTP, THE Portal_MCP_Server SHALL expose a single MCP endpoint accepting POST, SHALL validate the `Origin` header and respond 403 to invalid origins, SHALL require the `MCP-Protocol-Version` header, and SHALL NOT implement the deprecated HTTP+SSE transport.
4. THE Portal_MCP_Server SHALL be connectable from Kiro using a documented MCP_Config entry that contains no secret values (remote form with `url` and OAuth settings, or local form with `command`/`args`/`env` where `env` references variables rather than literal secrets).
5. WHEN Kiro connects to the Portal_MCP_Server over HTTP with OAuth, THE Portal's Protected_Resource_Metadata and the authorization server metadata SHALL identify the issuer with exactly matching strings so that Kiro's issuer check succeeds.

### Requirement 8: LocalServer Capabilities Exposed as MCP Tools

**User Story:** As an operator or engineer working at or near an edge device, I want the device's LocalServer capabilities available as MCP tools, so that an assistant can check health, inspect workflows and results, drive captures, and (with permission) control workflows on the device.

#### Acceptance Criteria

1. THE Edge_MCP_Server SHALL expose MCP_Tools covering each LocalServer capability area served by the Backend_API: system health and component status, station and snapshot information, workflows and workflow registrations (definitions, graphs, executions, results, overlays, logs, node status), inference results (summary, listing, export), cameras and image sources (listing, connection state, stream health, preview and capture), streams capabilities, static image and video pins, feature configurations and model start/stop status, GPU status, input and output configurations, and text generation where the device image provides it.
2. WHEN an MCP_Client requests the tool list, THE Edge_MCP_Server SHALL return every MCP_Tool in its Tool_Catalog with name, description, input JSON Schema, and annotations consistent with its classification (Read_Only_Tool or Mutating_Tool, with `destructiveHint` true for restart, delete, reset, pause, unpin, and disconnect tools).
3. THE Edge_MCP_Server SHALL implement every MCP_Tool by exercising the same Backend_API route logic and validation the LocalServer web UI and HMI use, and SHALL NOT reach any route without the Request_Authorizer deciding the call, or introduce a data path the Backend_API does not already provide.
4. WHEN a tool depends on a capability the device image lacks (for example text generation on a non-vLLM image), THE Edge_MCP_Server SHALL omit the tool from the list or return a clear "not available on this device" error, matching the Backend_API's conditional router registration.
5. WHEN a Read_Only_Tool returns image or binary content (frames, overlays, snapshots), THE Edge_MCP_Server SHALL return it as MCP image or resource content with size bounds defined by the design, and SHALL never return file-system paths that would let a client read arbitrary device files.
6. WHEN an MCP_Tool invocation fails, THE Edge_MCP_Server SHALL return an MCP tool error that identifies the reason category without exposing credential material or stack traces.

### Requirement 9: Edge MCP Authentication Reuses local_auth and the Account Sync

**User Story:** As a site administrator, I want the edge MCP server to accept exactly the credentials the LocalServer already accepts (synced portal accounts via local login, or the existing bearer token mechanism), so that enabling MCP on a device adds no new user store and no new password to manage.

#### Acceptance Criteria

1. THE Edge_MCP_Server SHALL authorize each tool invocation using the Request_Authorizer's decision matrix with the same credentials the Backend_API accepts: a valid Local_Session_Token while Local_Login_Configuration is enabled, a valid Existing_Token_Auth bearer token while Existing_Token_Auth is configured, and no credential only in the (disabled, not configured) open-access row.
2. THE Edge_MCP_Server SHALL NOT introduce any credential type, user store, password file, API key, or token issuer beyond the Local_Credential_Cache (provisioned by the Account_Sync_Service), the Local_Session_Token issuer, and Existing_Token_Auth.
3. WHEN a Principal authenticates to the Edge_MCP_Server with a username and password, THE Edge_MCP_Server SHALL obtain the Local_Session_Token through the existing `POST /local-auth/login` logic (including its uniform failure response and lockout) and SHALL NOT verify passwords through any other path.
4. WHILE Local_Login_Configuration is enabled, WHEN the Principal's synced account is marked disabled in the Local_Credential_Cache, THE Edge_MCP_Server SHALL reject subsequent tool invocations for that Principal immediately, inheriting the Request_Authorizer's per-request re-validation.
5. WHEN a Local_Session_Token presented to the Edge_MCP_Server expires, THE Edge_MCP_Server SHALL reject further invocations with an authentication error that tells the MCP_Client to log in again, and SHALL NOT extend, re-issue, or cache a replacement token without a new login.
6. THE Edge_MCP_Server SHALL NOT read a password or long-lived token from a plaintext configuration file, MCP_Config, or recipe; WHERE the stdio MCP_Transport is used, credentials SHALL be supplied through the process environment or an interactive login at launch only.
7. THE Edge_MCP_Server SHALL NOT write passwords, tokens, or authorization headers to the Access_Log, application log, tool results, or error messages.
8. WHERE Existing_Token_Auth is configured, THE Edge_MCP_Server SHALL validate bearer tokens through the existing remote introspection path unchanged and SHALL NOT read or modify `/aws_dda/authorization_settings.json` beyond the existing presence check and settings read.

### Requirement 10: Edge MCP Tool Classification and Role Gating

**User Story:** As a site administrator, I want state-changing edge tools restricted to the roles that hold device-control permissions in the portal, using the role already carried in the synced account, so that a Viewer's assistant can observe a device but not restart it or change its workflows.

#### Acceptance Criteria

1. THE Edge_MCP_Server's Tool_Catalog SHALL classify every MCP_Tool as a Read_Only_Tool or a Mutating_Tool and SHALL classify as destructive, at minimum: restarting DDA, deleting workflows or results, resetting result summaries, pausing or resuming continuous registrations, disconnecting cameras, deleting image sources or captured images, unpinning static media, and stopping models.
2. WHEN a Principal authenticated by a Local_Session_Token invokes a Mutating_Tool, THE Edge_MCP_Server SHALL permit the invocation only if the Portal_Role carried in the token is one that holds device-control Permissions in the Portal (Operator, UseCaseAdmin, PortalAdmin), and SHALL deny it for Viewer, DataScientist, and DataLabeler with an authorization error.
3. WHEN a Principal authenticated by a Local_Session_Token invokes a Read_Only_Tool, THE Edge_MCP_Server SHALL permit the invocation for every enabled synced Portal_Role.
4. THE Edge_MCP_Server SHALL take the Principal's Portal_Role only from the validated Local_Session_Token (and thereby from the synced account record), and SHALL NOT accept a role supplied as a tool argument, header, or configuration value.
5. WHERE the Principal is authenticated by Existing_Token_Auth (which carries no Portal_Role) or the open-access row applies, THE Edge_MCP_Server SHALL apply the Mutating_Tool policy the design specifies for role-less Principals (Open Design Decision 5), and that policy SHALL be no more permissive than the Backend_API grants the same request today.
6. WHERE a Mutating_Tool is classified destructive, THE Edge_MCP_Server SHALL require an explicit confirmation argument naming the target and SHALL reject invocations that omit it.
7. THE Edge_MCP_Server SHALL provide a device-level setting, controlled through the LocalServer's existing Greengrass component configuration mechanism (as `LocalLoginEnabled` is), that disables all Mutating_Tools; WHILE active, THE Edge_MCP_Server SHALL omit them from the tool list and reject their invocation.
8. THE Edge_MCP_Server SHALL NOT expose as MCP_Tools the login endpoint itself, the authorization-configuration endpoint's secret fields, or any operation that reads the Local_Credential_Cache, the session secret, or the authorization settings file.

### Requirement 11: Edge MCP Tool Invocations Are Logged

**User Story:** As a site administrator, I want every edge MCP tool invocation recorded with who did it and what tool was used, so that device activity driven by assistants is traceable alongside the existing access log.

#### Acceptance Criteria

1. WHEN an MCP_Tool completes on the device (success, failure, or denial), THE Edge_MCP_Server SHALL write a structured Access_Log entry containing the MCP_Audit_Marker (channel = MCP, tool name, Edge_MCP_Server version, MCP_Client identity when supplied), the Principal's username (or an "anonymous" marker in the open-access row), the Portal_Role when known, the outcome, the duration, and the request id.
2. THE Edge_MCP_Server SHALL log denied Mutating_Tool invocations (Requirement 10) and authentication failures at warning level with the Principal's username only, never the credential.
3. WHEN an MCP_Tool exercises a Backend_API route that already produces an Access_Log entry, THE Edge_MCP_Server SHALL correlate the two entries with a shared request id so that the MCP origin is attributable to the underlying route call.
4. THE Edge_MCP_Server SHALL keep its logging failure-safe: IF a log entry cannot be written, THEN THE Edge_MCP_Server SHALL NOT fail or alter the tool invocation's result (matching the Access_Log's existing "logging must never fail the request" rule), and SHALL NOT crash the LocalServer.

### Requirement 12: Edge MCP Server Enablement, Containment, and Device Exposure

**User Story:** As a site administrator, I want the edge MCP server off by default, enabled per device through the existing component configuration, and incapable of affecting the LocalServer's core inspection functions, so that adding it carries no risk to production lines.

#### Acceptance Criteria

1. THE Edge_MCP_Server SHALL be disabled by default; WHERE its enablement setting is absent, unreadable, or not a recognized true value, THE LocalServer SHALL behave exactly as it does today (no MCP listener, no MCP process, no new log lines beyond a single startup diagnostic).
2. WHEN the Edge_MCP_Server's enablement setting is changed in the LocalServer's Greengrass component configuration, THE LocalServer SHALL apply the change without reinstalling the component, within the same bound the Local_Login_Configuration honors today (60 seconds).
3. IF the Edge_MCP_Server fails to start, crashes, or exhausts a resource, THEN THE LocalServer's Backend_API, workflow engine, Triton, camera handling, web UI, and `/health` result SHALL continue unchanged (the same containment the vLLM runtime and sync agents have today).
4. WHERE the Edge_MCP_Server listens on a network port, THE Edge_MCP_Server SHALL bind only to the loopback interface unless the device's authentication is active (Local_Login_Configuration enabled or Existing_Token_Auth configured), and WHERE it is reachable beyond loopback it SHALL use TLS with the same device certificate the Backend_API uses on port 5443.
5. WHERE the Edge_MCP_Server listens over Streamable HTTP, THE Edge_MCP_Server SHALL validate the `Origin` header and reject invalid origins with 403 (DNS-rebinding protection), per the MCP specification.
6. THE Edge_MCP_Server SHALL run on every LocalServer architecture the product ships (arm64 JP5 with Python 3.11, arm64 JP6 with Python 3.10, arm64 JP7 with Python 3.11, amd64 and x86_64 NVIDIA with Python 3.11) or SHALL be explicitly marked unavailable for an architecture in the design with the owner's agreement; it SHALL NOT be silently absent.
7. THE Edge_MCP_Server SHALL NOT change the Backend_API's existing listener ports, TLS selection, CORS behavior, route set, or the Request_Authorizer's decision matrix.
8. WHEN the Edge_MCP_Server is enabled, THE LocalServer SHALL report the MCP server's state (enabled, listening endpoint or transport, version) through an authenticated Backend_API status surface so that operators and the Portal can see it.

### Requirement 13: Edge Build, Dependency, and Verification Constraints

**User Story:** As the repository owner, I want the edge MCP work to respect the build steering rules, so that it does not break the component build gate or ship untested on-device behavior.

#### Acceptance Criteria

1. IF the design changes `src/docker-compose.yaml`, any backend Dockerfile variant (`Dockerfile`, `Dockerfile.jp5`, `Dockerfile.jp6`, `Dockerfile.jp7`, `Dockerfile.x86_64_nvidia`), `src/backend/requirements.txt`, a recipe variant, or `station_install/setup_station.sh`, THEN the tasks SHALL update the corresponding Security_Preservation_Gate baselines in `test/backend-test/security/baselines/` in the same change and SHALL run the preservation suite in the flask-app container before any component build.
2. THE design SHALL state how the Python_SDK's dependency floors (see Research Baseline) are reconciled with the LocalServer's pinned `pydantic`, `httpx`, `jsonschema`, `uvicorn`, and `fastapi` versions without breaking the Backend_API, Triton client, or vLLM runtime on any architecture; the chosen approach is Open Design Decision 3.
3. THE Edge_MCP_Server SHALL be built and verified on real hardware for every architecture it ships on (JP5 and JP6 at minimum, JP7 and amd64 where available) before the change is committed, exercising login, tool listing, at least one Read_Only_Tool and one Mutating_Tool per capability area, and confirming the backend stays healthy (no crash loop, no container restart) for a sustained period, per On_Device_Verification.
4. THE Edge_MCP_Server SHALL NOT require a build of JP5, JP6, or JP7 component variants to run concurrently, and its tasks SHALL respect the one-variant-at-a-time build rule.
5. WHEN the Edge_MCP_Server adds Python dependencies, THE tasks SHALL pin exact versions, SHALL confirm wheel availability for arm64 on the JP5 (Ubuntu 20.04 base), JP6 (Ubuntu 22.04 base), and JP7 (Ubuntu 24.04 base) images, and SHALL confirm the dependencies install for offline/air-gapped devices (no runtime pip installs).

### Requirement 14: Consistent Tool Semantics Across Portal and Edge

**User Story:** As a user who works with both the portal and devices from one assistant, I want the two MCP servers to follow the same naming, classification, error, and confirmation conventions, so that I can move between them without relearning.

#### Acceptance Criteria

1. THE Portal_MCP_Server and THE Edge_MCP_Server SHALL use one documented tool-naming convention (a stable prefix per server or domain, lowercase, and the character set the MCP specification allows), one input-naming convention for shared concepts (use case id, device id, workflow id, registration id, execution id), and one error-category vocabulary.
2. THE Portal_MCP_Server and THE Edge_MCP_Server SHALL apply the same classification rules for Read_Only_Tool, Mutating_Tool, and destructive, and the same confirmation-argument convention for destructive tools.
3. WHEN both servers expose a tool over the same concept (for example a workflow registration visible in the Portal and running on a device), THE Tool_Catalogs SHALL use the same identifiers for that concept so results from one server can be used as inputs to the other.
4. THE Tool_Catalogs SHALL be versioned with the server release, and WHEN a tool is renamed or removed, THE server SHALL keep the previous name available for at least one release with a deprecation note in its description.

### Requirement 15: Packaging, Client Configuration, and Documentation

**User Story:** As a Portal administrator or site engineer, I want clear, secret-free instructions to connect Kiro (and other MCP clients) to the portal and edge MCP servers, so that teams can adopt them without guesswork.

#### Acceptance Criteria

1. THE repository SHALL provide example MCP_Config entries for Kiro for each supported connection form of each server (remote `url` form with OAuth settings; local stdio form with `command`, `args`, and `env`), and no example SHALL contain a literal token, password, client secret, or API key (environment references and OAuth client identifiers only).
2. THE repository's existing MCP_Config at `.kiro/settings/mcp.json` SHALL be left functional for its current third-party servers; any addition for this feature SHALL be additive and SHALL default to `disabled: true` until a developer opts in.
3. THE Portal_MCP_Server and THE Edge_MCP_Server SHALL each report a version (aligned to the Portal release and the LocalServer component version respectively) in the server identity they return to MCP_Clients.
4. THE documentation SHALL be updated in the places administrators already read: `edge-cv-portal/ADMIN_GUIDE.md` (enabling the Portal_MCP_Server, OAuth client registration in the User_Pool for MCP clients, disabling mutating tools, reading MCP audit entries), `README.md` and `docs/` (enabling the Edge_MCP_Server per device, connecting over the LAN or an IoT Secure Tunnel, logging in with a synced account), and the LocalServer component configuration reference (new configuration keys and defaults).
5. THE documentation SHALL state the protocol revision(s) supported, the MCP_Client versions tested (Kiro IDE and CLI at minimum), and the Python_SDK version used, with dates.
6. WHERE the Portal_MCP_Server is distributed as a locally runnable package (stdio form), THE package SHALL be installable with a pinned version from the repository (or a published artifact the design names) and SHALL declare its minimum Python version.

### Requirement 16: Testing and Verification Strategy Coverage

**User Story:** As the repository owner, I want the MCP servers covered by automated tests at the protocol, authentication, authorization, and audit levels plus on-device verification, so that a regression in any of these gates is caught before release.

#### Acceptance Criteria

1. THE test suite SHALL include protocol-level tests that connect an official Python_SDK client to each server, list tools, invoke at least one Read_Only_Tool and one Mutating_Tool, and verify the protocol revision negotiation and error shapes.
2. THE test suite SHALL include authentication negative tests for each server: no credential, expired credential, invalid signature, wrong issuer, wrong audience (Portal), token for a disabled account (edge), and credential presented in a URI query parameter (rejected on HTTP transports).
3. THE test suite SHALL include authorization tests that, for every MCP_Tool in each Tool_Catalog, assert the allowed Portal_Roles succeed and every other Portal_Role is denied with an authorization error and (Portal) an `unauthorized_access` Audit_Log entry carrying the MCP_Audit_Marker; the Portal tests SHALL run with `PORTAL_REGISTRY_ENFORCED` on and off.
4. THE test suite SHALL include audit tests asserting that every Mutating_Tool produces exactly one Audit_Log entry with the MCP_Audit_Marker and no credential material in details, and that audit-before-effect paths abort when the audit write fails.
5. THE test suite SHALL include tests that the destructive-tool confirmation argument is required and that the mutating-tools-disabled setting removes and rejects Mutating_Tools on both servers.
6. THE Portal infrastructure tests (`edge-cv-portal/infrastructure/test/`) SHALL cover any new CDK stack or resources: authorizer attachment on every MCP method, throttling settings, least-privilege IAM, and the absence of unauthenticated methods.
7. THE edge tests SHALL include host-runnable unit tests under `test/backend-test/` for the Edge_MCP_Server's authorization, role gating, logging, and enablement logic, and an on-hardware harness stage under `test/on-hardware/harness/stages/` that exercises the Edge_MCP_Server on a live device through its documented transport.
8. THE Security_Preservation_Gate suite SHALL pass with updated baselines for every tracked file the change touches, and the change SHALL NOT weaken or delete any preservation test.
9. THE tests SHALL cover the backward-compatibility behavior the design declares for earlier MCP protocol revisions (Requirement 7.2) with at least one earlier-revision client interaction per server.

### Requirement 17: Phased Delivery

**User Story:** As the repository owner, I want the MCP servers delivered in phases that each stand on their own, starting with read-only access, so that value lands early and the riskier state-changing tools arrive only once the authentication, authorization, and audit foundations are proven.

#### Acceptance Criteria

1. THE design SHALL define a first phase (MVP) in which both servers expose Read_Only_Tools only, with authentication, authorization, audit logging with the MCP_Audit_Marker, client configuration examples, documentation, and the full test coverage of Requirement 16 for those tools.
2. THE design SHALL define subsequent phase(s) that add Mutating_Tools by domain, each phase shippable independently, with the destructive-tool safeguards of Requirements 5 and 10 in place before the first Mutating_Tool ships.
3. WHILE a later phase is not yet delivered, THE servers SHALL NOT list or accept the tools of that phase (no partially implemented tools), and the documentation SHALL state which phase each tool belongs to.
4. THE phasing SHALL NOT drop any capability area named in Requirements 1 and 8; capability areas deferred to a later phase SHALL be listed explicitly with the phase that delivers them.

## Non-Negotiable Constraints

These apply to every requirement above and to the design and tasks that follow. They restate the owner's instruction ("use the existing credential system for access") in testable form.

- **C1 Reuse existing credential systems only.** Portal: User_Pool tokens validated like the Portal_API, authorization through the RBAC_Layer and Portal_Identity_Registry. Edge: the Request_Authorizer decision matrix with Local_Session_Tokens (accounts provisioned by the Account_Sync_Service) and Existing_Token_Auth. No other authentication scheme may be added for MCP.
- **C2 No new identity stores.** No new user table, account file, password store, or token issuer on either side. The Local_Credential_Cache and the User_Pool remain the only account sources.
- **C3 No API keys or plaintext secrets in configuration.** No static API keys, shared secrets, passwords, refresh tokens, or bearer tokens in `mcp.json`, recipes, docker-compose, environment files committed to the repository, or documentation examples.
- **C4 RBAC on every call.** Every MCP_Tool invocation is authorized by the existing permission model for the Principal it acts for; no synthesized authorizer context, no service identity acting for users, no role elevation.
- **C5 Audit on every action.** Every Portal MCP action is recorded in the Audit_Log with the MCP_Audit_Marker; every edge MCP action is recorded in the Access_Log with the MCP_Audit_Marker; audit-before-effect ordering is preserved where the Portal uses it.
- **C6 Steering compliance.** Edits to Security_Preservation_Gate-tracked files carry baseline updates in the same change; on-device features are verified on real hardware of each affected architecture before commit; component builds run one variant at a time; no portal deploy while a component build runs.
- **C7 Additive and reversible.** With the MCP servers disabled, the Portal and the LocalServer behave exactly as today; existing REST routes, ports, and the Request_Authorizer matrix are unchanged.
- **C8 Security posture.** No secrets in logs or shell strings, least-privilege IAM with no wildcard actions, and documented in-code exceptions for any intentional non-loopback bind, consistent with `security-secrets-credentials-jwt-fixes` and `security-iam-authorization-fixes`.

## Open Design Decisions

The design phase must resolve each item below and record the choice with reasoning. This document deliberately does not choose. Each item states the user need behind it and the facts the choice must respect.

### Decision 1: Portal MCP topology (one server vs. several domain-scoped servers)

User need: assistants should find the right tool quickly without being overwhelmed, and administrators should be able to grant or disable access per domain. Facts: Requirement 1 names roughly twenty functional domains with hundreds of underlying routes; MCP clients load every listed tool into model context; Kiro supports per-server `autoApprove` and `disabledTools`; a single server simplifies authentication and client configuration, while domain-scoped servers (for example data/labeling, models/training, devices/deployments, workflows/components, admin/audit) map naturally to Portal_Roles. The design must decide the number of servers, the domain grouping, and whether a single deployable can expose several server identities.

### Decision 2: Portal MCP hosting and transport (remote Streamable HTTP behind API Gateway + Cognito vs. local stdio calling the Portal_API vs. both)

User need: users want to connect from Kiro IDE/CLI with minimal setup; administrators want the server deployed and secured with the Portal, not installed per laptop. Facts: a remote server must meet the MCP authorization rules (Protected_Resource_Metadata, RFC 8707 audience validation, 401 challenges); the Portal_API's authorizer accepts Cognito **ID tokens** whose `aud` is the app client id and the frontend uses ID tokens, whereas the MCP specification expects access tokens scoped to the MCP server resource, so the design must decide how the audience requirement is met with the existing User_Pool (for example a dedicated app client and resource server scopes for MCP, or a documented and justified deviation); the existing User_Pool client is a public SRP + authorization-code client with loopback callbacks limited to `http://localhost:3000`, Cognito does not support Dynamic Client Registration, and Kiro supports pre-registered client ids with loopback redirect URIs and PKCE (IDE: public clients only); the API Gateway main stack is at the 500-resource limit, so new routes follow the nested-stack pattern; Lambda-hosted Streamable HTTP must work within API Gateway's request/response and SSE constraints, or the design must choose another host (for example a container service) and justify it; a stdio server running on the user's machine needs a way to obtain the user's token through an allowed User_Pool flow (SRP or hosted-UI PKCE) and to refresh it, and the MCP specification says stdio servers take credentials from the environment. The design must choose the transport(s), the hosting service, the token audience approach, and how Kiro IDE and CLI each authenticate.

### Decision 3: Edge MCP hosting and transport, and Python version/package constraints (FastAPI route in the Backend_API process vs. sidecar container vs. stdio process on the device)

User need: operators want the edge MCP server to work on every device they own (JP5, JP6, JP7, amd64), on LAN or through an IoT Secure Tunnel, without destabilizing the LocalServer. Facts: the Backend_API is FastAPI on uvicorn (not Flask) with pinned `fastapi==0.109.2`, `uvicorn==0.23.2`, `pydantic==2.6.4`, `httpx==0.24.1`, `jsonschema==4.17.3`; every published Python_SDK release requires newer `pydantic` and `httpx` (and from 1.12 newer `jsonschema`, from 1.20 newer `uvicorn`), so mounting the SDK in-process requires dependency bumps that trip the Security_Preservation_Gate and risk Triton/vLLM/edgemlsdk compatibility; the SDK supports Python 3.10 (JP6) and 3.11 (JP5/JP7/amd64); a sidecar container or separate virtualenv isolates dependencies but adds a docker-compose service (gate-tracked), image size, and a second process to health-check; a stdio process on the device is reachable only via SSH/tunnel and still needs the SDK installed; the container runs in host network mode, so any new port is LAN-exposed; the 2026-07-28 Streamable HTTP binding is stateless, which suits a thin HTTP proxy in front of the Backend_API. The design must choose the process model, the transport(s), the port and TLS behavior (Requirement 12.4), how the Request_Authorizer is invoked (in-process dependency vs. forwarding the credential to the Backend_API over loopback), and the dependency isolation approach, and must list the gate baselines each choice touches.

### Decision 4: Tool surface and safety (read-only vs. mutating classification, RBAC gating, audit logging with the MCP_Audit_Marker)

User need: users want enough tools to be useful; administrators want to know exactly which tools can change production state and to see those actions in the audit trail. Facts: the Portal_API handlers already carry `rbac_check` permission declarations that can be inventoried into the Tool_Catalog; several handlers write their own Audit_Log entries (the marker must be injected, not duplicated); the Backend_API has no role enforcement today, so edge role gating (Requirement 10) is new enforcement using the role already in the Local_Session_Token; MCP annotations are advisory and untrusted by clients, so server-side confirmation arguments are needed for destructive tools; Kiro's `autoApprove` lets users turn off client-side prompts. The design must produce the full Tool_Catalog for both servers (name, classification, Permission(s), underlying route, annotations, phase), the confirmation convention, and the exact Audit_Log/Access_Log field layout of the MCP_Audit_Marker.

### Decision 5: Authorization mapping (Cognito claims to RBAC roles; Local_Session_Tokens to roles; rate limiting; token and session expiry)

User need: users expect the same access they have in the web UI; administrators need predictable behavior when tokens expire or accounts change. Facts: Portal privilege comes from the Portal_Identity_Registry when `PORTAL_REGISTRY_ENFORCED` is on and `custom:role` is Claimed_Role only; the `jwt_authorizer.py` Lambda (unattached today) maps OIDC `groups` to roles for non-Cognito issuers and could be relevant if the design uses a custom authorizer for the MCP endpoint; Cognito ID tokens carry the pool client's default one-hour validity (`auth-stack.ts` does not override `idTokenValidity`) and the Portal refreshes them through Amplify or `POST /auth/refresh`; Local_Session_Tokens live 12 hours and are revoked on account disable; Existing_Token_Auth bearer tokens carry no Portal_Role; the Portal has stage-level throttling only, and the Quick Setup limiter is endpoint-specific. The design must specify: how an MCP request's token becomes a Principal and effective role on each side; the Mutating_Tool policy for role-less edge Principals (Requirement 10.5); MCP endpoint throttling values and whether per-Principal limits are needed; the token-expiry and refresh behavior for each transport; and the handling of mid-session account disablement.

### Decision 6: Packaging, distribution, and client configuration (mcp.json examples, versioning, documentation updates)

User need: a developer or operator should be able to connect Kiro in minutes using copy-paste configuration that contains no secrets. Facts: Kiro reads `.kiro/settings/mcp.json` and `~/.kiro/settings/mcp.json`, supports `url` + `oauth` (pre-registered `clientId`, loopback `redirectUri`, `oauthScopes`) for remote servers and `command`/`args`/`env` with `${VAR}` expansion for stdio servers; the repository already ships a workspace `mcp.json` for third-party servers; the Portal is deployed by `deploy-portal.sh` with CDK context, and the LocalServer by Greengrass component versions. The design must specify the distribution unit for each server (Lambda/nested stack, container image, Python package, or files inside the LocalServer image), the version string sources, the Kiro `mcp.json` examples for IDE and CLI, the Cognito app-client registration steps an administrator performs for MCP clients, and the exact documentation files to update.

### Decision 7: Testing strategy (protocol-level, authentication negative, RBAC denial, on-device verification, CDK tests)

User need: the owner needs confidence that authentication, authorization, and audit cannot regress silently, and that the edge server works on real hardware. Facts: Portal backend tests live in `edge-cv-portal/backend/tests/` (pytest, 446 files), CDK tests in `edge-cv-portal/infrastructure/test/` (jest with snapshots), edge unit tests in `test/backend-test/` (pytest in the flask-app container, `local_auth/` tests exist), the on-hardware harness in `test/on-hardware/harness/` drives a live device through port 5000 with restoration guarantees, and the Security_Preservation_Gate runs in the component build. The design must map each acceptance criterion of Requirement 16 to a test location and mechanism (including how an official Python_SDK client is driven in CI, how the earlier-revision compatibility test is constructed, and how the harness stage logs in with a synced account without storing a password).

### Decision 8: Phasing (MVP read-only vs. later mutating tools)

User need: deliver useful read access early; add state-changing power only after the security foundations are exercised in practice. Facts: Requirement 17 fixes the MVP as read-only on both servers with full auth/audit/test coverage; mutating tools need the confirmation convention, the disable switch, and (edge) role gating in place first; some domains (deployments, workflows, device control) are far more consequential than others (datasets, labeling). The design must define the phase boundaries by domain and server, the entry criteria for each phase (including on-device verification evidence), and which Requirement 1 and 8 capability areas land in which phase.

## Flagged Items (kept, not dropped; the design must address)

- **F1 Open-access edge devices.** On a device where neither Local_Login nor Existing_Token_Auth is configured, the Backend_API is intentionally open on the LAN today. Requirement 9.1 preserves parity, so the Edge_MCP_Server would also be open there. Requirement 12.1 (off by default) and 12.4 (loopback-only binding unless authentication is active) are the mitigations stated here; the design should consider whether Mutating_Tools must be unavailable in the open-access row regardless of configuration, and document the residual risk in `docs/`.
- **F2 ID-token vs. access-token audience.** The Portal's authorizers accept ID tokens whose audience is the app client id; the MCP authorization specification requires access tokens whose audience is the MCP server resource. Satisfying both "reuse the existing User_Pool" and "conform to MCP authorization" may require a dedicated Cognito app client or resource server for MCP, which is still the existing identity system but is a configuration addition to `auth-stack.ts`. Any deviation from the MCP authorization rules must be explicit and justified in the design.
- **F3 Edge dependency pins.** No published Python_SDK release installs against the LocalServer's current pins. Whatever Decision 3 chooses will touch at least one Security_Preservation_Gate-tracked file (requirements.txt, a Dockerfile, or docker-compose.yaml) or add a new isolated runtime; baselines must be updated in the same change (C6).
- **F4 Edge role enforcement is new.** The Backend_API authenticates but does not authorize by role today. Requirement 10 introduces role gating for MCP tools using the role already in the Local_Session_Token. This is consistent with "reuse existing credentials" (no new claims, no new store) but is new enforcement logic; the design must keep it MCP-scoped so existing Backend_API routes and the HMI remain unchanged (C7).
- **F5 Audit volume and cost.** Read_Only_Tools invoked by an assistant can be high-frequency. Requirement 4 asks for audit entries for every tool completion; the Portal's Audit_Log has a 90-day TTL and no GSI for channel filtering today. The design must decide whether read-only invocations are audited individually, sampled, or logged to CloudWatch only (with the owner's agreement if any Requirement 4 entry is relaxed), and how Requirement 4.6 filtering is served.
- **F6 Lambda and Streamable HTTP.** API Gateway REST APIs impose a 29-second integration timeout and buffered responses, which constrains the SSE response streams the Streamable HTTP binding allows. The design must confirm the chosen host can honor the binding for the tool durations expected, or choose a host that can.
- **F7 The `jwt_authorizer.py` Lambda is unattached.** It exists in `compute-stack.ts` but no API method uses it; the live authorizers are native Cognito User Pools authorizers. If the design relies on custom token validation for the MCP endpoint (for example to accept access tokens with an MCP audience), it must say whether this Lambda is reused, extended, or left alone, and must not silently change behavior for existing routes.
- **F8 Edge frontend and HMI unaffected.** The LocalServer web UI and the Quality Station HMI consume the Backend_API with Local_Session_Tokens; nothing in this feature may change their login or token handling.

## Out of Scope

- Changing how the Portal web UI or the LocalServer web UI/HMI authenticate users, or adding new sign-in methods, identity providers, or user attributes beyond the configuration additions the design justifies under F2.
- Building an MCP *client* into the Portal or the LocalServer (for example letting the Portal's Bedrock-backed code assist call external MCP servers).
- Exposing Portal-internal or device-internal secrets through any tool (credential verifiers, authorization settings, session secrets, Setup_Tokens, provisioning credentials, SES configuration).
- New Portal or LocalServer capabilities that do not already exist as REST routes; MCP tools wrap existing capabilities only.
- Multi-device fan-out tools on the Edge_MCP_Server (one Edge_MCP_Server serves one device); fleet-wide operations remain Portal tools.
- Replacing the deprecated HTTP+SSE transport compatibility for clients that only speak it.
- Changes to the Account_Sync_Service, Local_Login, lockout, or Local_Session_Token format and lifetime.
- Any implementation, infrastructure change, Dockerfile/recipe/docker-compose edit, build, deploy, or git commit in this spec-authoring run.


