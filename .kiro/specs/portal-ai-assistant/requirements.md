# Requirements Document

## Introduction

This feature adds an AI assistant to the DDA Portal web application (edge-cv-portal). The Assistant lives in a panel that is present on every signed-in page, can be collapsed to a small trigger where screen space is tight and expanded again, and does two things for the user:

1. **Guides.** It explains the page the user is on and the end-to-end path through the application (use case → data → labeling → training → models → workflows → builds → devices → deployments → operations), grounded in the Portal's own user guide, and tells the user what to do next and where.
2. **Acts.** When the user is stuck in the UI or does not want to use it, the Assistant performs the task through the Portal's MCP tool catalog, as the signed-in user, under the same role checks and audit trail as the Portal and the Portal MCP server. State-changing actions run only after the user confirms the exact action in the panel.

The governing requirement is that the Assistant is a *client* of the capabilities the `mcp-servers` spec delivered, not a second implementation of them. It uses the same tool catalog, the same validation, authorization, dispatch, shaping and audit pipeline, and the user's own Portal credential. It introduces no service identity, no second credential type, no tool that reaches Portal state outside the catalog, and no state change without the user's confirmation.

Architectural choices (where the backend runs, how responses stream to the browser, how the user guide is supplied to the model, how conversations are stored, which chat components are used) are deliberately left open and listed under "Open Design Decisions" for the design phase.

## Prior Work Check

Performed 2026-11-01 on the trunk (`origin/integration/all-specs` = `main` = `remediation` at `0861addb`):

- `.kiro/specs/` (170 directories) has no spec whose name or text describes a chat assistant, copilot or in-Portal help agent. The two name matches are `custom-node-code-assist` (a Bedrock-backed prompt-to-code panel scoped to the custom Python node editors) and `detection-guided-bedrock-inspection` (an inference feature). A text search for "chatbot", "chat assistant", "in-portal assistant" and "copilot" across `.kiro/specs/`, `docs/`, `README.md` and the Portal guides found nothing.
- The `mcp-servers` spec is complete (tasks.md: 145 ticked, 0 open; phases 1–3 released, `DELIVERED_PHASES = (1, 2, 3)` on both servers). Its Out of Scope section (requirements.md line 379) deferred exactly this feature: "Building an MCP *client* into the Portal or the LocalServer". This spec is that follow-up.
- The research notes from the paused 2026-11-01 attempt are in the task-notes directory kept beside the repository (`portal-ai-assistant/handoff.md`); the facts there were re-verified against `0861addb` and are restated below.

**Owner decisions (2026-11-03), binding on the design:** (1) no Assistant for DataLabeler-only users (1.6); (2) the panel is expanded until the user first collapses it, except on the builder route and overlay-width viewports (2.4); (3) Conversations are stored server-side per user (9.1); (4) the retention period is a PortalAdmin setting, default 30 days (9.3, 12.1); (5) the Actions_Switch defaults to on and is a PortalAdmin setting (10.4); (6) Destructive_Tools reach the Assistant only through an allow-list approved at Phase 2 sign-off (5.2, 15.2). Requirement 8.1 (progressive display) was offered for softening and was not softened; it stands, and Decision 2 must satisfy it.

The following existing specs are the baseline this spec builds on and are not re-specified here: `mcp-servers` (Tool_Catalog, Tool_Pipeline, MCP_Audit_Marker, `confirm` convention, mutating-tools switch, phases), `custom-node-code-assist` (Bedrock_Configuration reuse, Bedrock failure categories, surface-scoped permissions), `workflow-manager` (Bedrock_Configuration storage), `portal-jwt-role-privilege-escalation` (Portal_Identity_Registry, Claimed_Role), `portal-authorization-and-iam-scoping` (shared authorizer, route-auth inventory, IAM gates), `portal-session-expiry-return-to-page` (session-expiry behaviour of the web UI), `dda-data-labeling` (DataLabeler confinement to `/labeler`).

## Research Baseline (verified 2026-11-01 against primary sources)

- **API Gateway REST APIs stream responses** since 2025-11-19 (aws.amazon.com/about-aws/whats-new/2025/11/api-gateway-response-streaming-rest-apis/; docs.aws.amazon.com/apigateway/latest/developerguide/response-transfer-mode.html). Setting an `AWS_PROXY` or `HTTP_PROXY` integration's response transfer mode to `STREAM` lets a response stream for up to 15 minutes, lifts the 10 MB payload limit, and removes the 29-second integration timeout. Constraints as documented: idle timeout 5 minutes for Regional and private endpoints but **30 seconds for edge-optimized endpoints**; endpoint caching, content encoding and VTL response transformation are unavailable in `STREAM` mode; the first 10 MB stream unthrottled, the rest at 2 MB/s; a Lambda may keep running after the client disconnects. A Lambda proxy integration in `STREAM` mode is invoked through `InvokeWithResponseStream` (a different integration URI, `…/2021-11-15/functions/…/response-streaming-invocations`) and must emit the streaming output format (a metadata JSON, an 8-null-byte delimiter within the first 16 KB, then the payload) — docs.aws.amazon.com/apigateway/latest/developerguide/response-transfer-mode-lambda.html.
- **Lambda response streaming** is native to the Node.js managed runtimes only; "for other languages, including Python, you can use a custom runtime with a custom Runtime API integration to stream responses or use the Lambda Web Adapter" (docs.aws.amazon.com/lambda/latest/dg/configuration-response-streaming.html). Streamed payloads may reach 200 MB; the function is billed for its full duration even when the client disconnects. Response streaming is available in all commercial Regions since 2026-04.
- **Amazon Bedrock Converse API.** `Converse` and `ConverseStream` accept tool definitions (`toolConfig`) and return `toolUse` content blocks the caller answers with `toolResult` blocks; streaming delivers text and tool-use arguments incrementally (docs.aws.amazon.com/bedrock, Converse API; `ConverseStream` examples for Anthropic Claude). Prompt caching is supported in two forms, implicit (best-effort prefix reuse, no request changes) and explicit (model-specific cache checkpoints with a model-specific minimum prefix length; the prefix must stay byte-identical between requests) — docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html. The default model in this repository, Anthropic Claude Sonnet 4.5 through a cross-region inference profile, supports tool use and prompt caching. Bedrock Guardrails attach to a Converse request through `guardrailConfig` (to be confirmed by the design for the configured model).
- **Cloudscape.** The `@cloudscape-design/chat-components` package (npm) provides the conversation components (chat bubble, avatar, loading bar, support prompt group); the prompt input lives in `@cloudscape-design/components`. Cloudscape's generative-AI chat pattern (cloudscape.design/gen-ai/patterns/generative-ai-chat/, published 2024-11-20) prescribes: in side-panel or embedded surfaces, align every message to the same side and distinguish authors by avatar; keep the composer pinned and scroll only the message log; show loading and error states; wrap the log in `role="region"` with an `aria-label` and announce new messages through a visually hidden live region; placeholder text "Ask a question"; refer to AI output as "responses"; optional thumbs-up/down feedback, inline sources and a disclaimer.

## Existing Systems (as read from the code at `0861addb`)

Portal MCP server (`edge-cv-portal/backend/functions/mcp_portal/`):
- `catalog.py` holds 183 `ToolSpec` rows (89 read-only, 94 mutating, 28 of them `destructive`), each declaring its backing handler, HTTP method and resource, JSON input schema, `permissions` and `scope`, `classification`, `phase`, `confirm_target`/`confirm_literal` for destructive tools, `handler_audits`/`handler_audits_denials`, `status_tool`, `result_shape`, `limit_default`/`limit_max`. `DELIVERED_PHASES = (1, 2, 3)` (`__init__.py`). `tools_for_view()` lists tools by phase and the mutating switch, never by principal, sorted `(domain, name)`; `to_mcp_tool()` renders name, title, description, input schema and annotations. Rendered as JSON, the 183 definitions (name, description, input schema) are about 126 KB (≈ 31k tokens at 4 bytes per token); the 89 read-only ones about 50 KB (≈ 12k tokens); per domain: admin ≈ 2.4k, models ≈ 3.8k, devices ≈ 5.8k, data ≈ 8.5k, workflows ≈ 10.9k tokens.
- `server.py:build_server()` implements the invocation pipeline inside the `on_call_tool` closure: (1) authenticate — the SDK's `get_access_token()` then `principal_from_access_token`; (2) look the tool up and apply the phase gate; (3) `validation_failure` and `confirmation_failure` (the `confirm` argument must equal the target named by `confirm_target`, or the `confirm_literal`); (4) the mutating gate `mutating_enabled()` (environment `MCP_MUTATING_TOOLS_ENABLED`, from CDK context `mcpMutatingToolsEnabled`, default off); (5) `authz.authorize` through `shared_utils` RBAC with `Denied`/`Unavailable` outcomes and the denial audit entry; (6) `dispatch.build_backend_event` + `dispatch.invoke` — a Lambda-to-Lambda call carrying the verified claims and a non-spoofable `event['mcp']` marker; (7) `shaping.shape` with `MAX_RESULT_BYTES = 200_000`; (8) `audit.write_envelope`; (9) `telemetry.log_tool_completion`. Steps 2–9 are not a reusable function today.
- `principal.py:Principal(sub, username, email, claims)` is built from a verified **Cognito access token** of the MCP app client (`dda-portal-mcp-client`, hosted-UI code + PKCE only, scope `<url>/portal.tools`). The web UI holds **ID tokens** from the SRP flow (`frontend/src/services/api.ts`, `refreshIdToken`), and the REST API's Cognito authorizer accepts ID tokens only; the MCP design (D3) made this separation deliberate, so the browser cannot call `/mcp` with its own token. The MCP endpoint also answers any `Origin` header with 403 unless listed in `MCP_ALLOWED_ORIGINS` (D14).
- Error vocabulary (`errors.py`): `ToolFailure` categories `not_authenticated, not_authorized, authorization_unavailable, not_found, invalid_input, confirmation_required, conflict, throttled, timeout, backend_failure, mutating_tools_disabled, result_too_large`; `effect_unknown_failure` for a dispatched mutating tool whose outcome is unknown; `scrub` removes credential-like material from messages.
- `backend/tests/mcp/` (≈ 3,800 tests) re-derives catalog facts from the handler source by AST and fails on drift (memory `portal-mcp-catalog-mirrors-handlers`).

Audit (`layers/shared/python/shared_utils.py`, `functions/audit_logs.py`, `frontend/src/pages/AuditLogs.tsx`): every audit entry carries `channel` — `AUDIT_CHANNEL_API = 'api'` or `AUDIT_CHANNEL_MCP = 'mcp'` — and MCP entries carry `mcp_tool`, `mcp_server`, `mcp_client`, `mcp_request_id`. `GET /audit-logs` accepts `channel=api|mcp` only (`audit_logs.py:156–162`); the page's `CHANNEL_OPTIONS` are All / Web & API / MCP.

Bedrock (`functions/bedrock_common.py`, `functions/code_assist.py`, `functions/data_accounts.py`): `get_bedrock_configuration()` reads setting key `bedrock_configuration` from `SETTINGS_TABLE` (model id, region, max tokens, temperature, top_p, timeout ≤ 240 s; default model `us.anthropic.claude-sonnet-4-5-20250929-v1:0`), `get_bedrock_client`, `build_inference_config` (sends at most one of temperature/top_p). Six handlers call `client.converse`; none uses `converse_stream`; `code_assist.py` already passes a `toolConfig`. PortalAdmins edit the configuration in Settings (`BedrockConfigurationSettings.tsx`, gate `bedrock-config:write`). Lambda roles grant `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on `foundation-model/*` and the account's `inference-profile/*` (`compute-stack.ts`, `node-designer-stack.ts`).

Frontend shell (`frontend/src/`): `App.tsx` renders `/login` outside `ProtectedRoute`; every other route renders inside `ProtectedRoute → DataLabelerRedirect → Layout`. `Layout.tsx` is Cloudscape `TopNavigation` + `AppLayout` with `toolsHide` (the tools drawer is unused), `navigationWidth={200}`, side navigation collapsed by default on `/workflows/builder` (the user's explicit toggle wins for the session), a fixed 24 px build banner at the viewport bottom (`z-index 1000`), and the change-password modal. `AuthContext` exposes `{user_id, email, username, role}` with `role` from the ID token's `custom:role` (Claimed_Role); `UsecaseContext` keeps `dda-selected-usecase-id` in `localStorage`. Dependencies: `@cloudscape-design/components` ^3, React 18, react-router-dom 7, aws-amplify 6, `@tanstack/react-query`, vitest 4 (263 test files). Not present: `@cloudscape-design/chat-components`, any Markdown renderer or HTML sanitizer, any `EventSource`/`ReadableStream` consumer, any React error boundary. `config.ts` branding carries optional `documentationUrl` and `supportEmail`.

User guide (`docs/user-guide/`): 25 Markdown files, 26,444 words — `README.md` (section index, roles, and "The end-to-end path": eight ordered steps, each naming its Portal page and chapter) plus chapters 01–24, one per Portal page or surface (24 is MCP support). Screenshots live in `images/`; `build-pdf.sh` renders `DDA-User-Guide.pdf`.

API Gateway (`infrastructure/lib/api-gateway-stack.ts`; memory `portal-api-gateway-limits`): the Portal REST API has no `endpointTypes` set, so it is **edge-optimized** (CDK default). It holds 259 of 300 resources (Oct 2026) and all 10 permitted Cognito authorizers; new route groups attach to the shared `EdgeCVPortalAuthorizer` and live in nested or sibling stacks; the main nested `ApiGatewayStack` is near the 500-resource CloudFormation limit; `route-auth-inventory.test.ts` requires every API-registering stack to be inventoried. The MCP server runs behind its own HTTP API in `EdgeCVPortalMcpStack` with Lambda-side token validation; removing a stack whose exports another stack imports needs the two-step order documented in the MCP design (D33).

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend behind API Gateway, DynamoDB storage).
- **Portal_API**: The Portal's existing REST API (stage `v1`) and its Lambda handlers under `edge-cv-portal/backend/functions/`.
- **Assistant**: The capability introduced by this feature: the Assistant_Panel in the Portal frontend plus the Assistant_Backend that runs conversation turns.
- **Assistant_Panel**: The frontend component that shows a Conversation, accepts prompts, shows tool activity and Action_Confirmations, and can be collapsed to a Trigger or expanded.
- **Trigger**: The compact always-visible control that expands a collapsed Assistant_Panel.
- **Assistant_Backend**: The server-side component that authenticates the user, composes the model request, runs the Assistant_Model, executes tools through the Tool_Pipeline, stores Conversations and streams the response to the Assistant_Panel.
- **Assistant_Model**: The Amazon Bedrock model named by the Bedrock_Configuration, invoked through the Converse API with tool definitions.
- **Bedrock_Configuration**: The existing per-account Portal settings (setting key `bedrock_configuration`) identifying the Bedrock model id, region, max tokens, optional sampling parameters and timeout used by every Bedrock-backed Portal feature.
- **Conversation**: An ordered sequence of Turns between one Principal and the Assistant, with an identifier, a title, creation and last-activity times.
- **Turn**: One user prompt and everything the Assistant does in response to it: model calls, tool invocations, Action_Confirmations and the final response text.
- **Page_Context**: The information about the user's current place in the Portal that the Assistant_Panel sends with a prompt: the route path and its parameters, and the selected Use_Case identifier.
- **User_Guide**: The Markdown chapters under `docs/user-guide/` and their index `README.md`.
- **Journey**: The ordered end-to-end path through the application described in the User_Guide's "The end-to-end path" section (use case and team → data → labeling → training and models → workflows → LocalServer build → device and deployment → operation).
- **Journey_Step**: One numbered step of the Journey, with the Portal page where it is performed and the User_Guide chapter that explains it.
- **Tool_Catalog**: The Portal MCP server's authoritative tool list, `mcp_portal/catalog.py` (`CATALOG`), with each tool's classification, permissions, scope, phase, input schema and confirmation rule.
- **Tool_Pipeline**: The Portal MCP server's per-invocation steps after authentication: lookup and phase gate, validation and confirmation check, mutating gate, authorization, dispatch, result shaping, audit, telemetry.
- **Portal_MCP_Server**: The MCP server the `mcp-servers` spec delivered (`EdgeCVPortalMcpStack`), used by external clients such as Kiro.
- **Read_Only_Tool / Mutating_Tool / Destructive_Tool**: A Tool_Catalog tool classified read-only; classified mutating; classified mutating with `destructive` set (deletes, cancels, restarts, role removal and the like), which the catalog guards with a `confirm` argument.
- **Action_Confirmation**: The Assistant_Panel step that shows one proposed Mutating_Tool invocation (tool, every argument, Use_Case, classification) and asks the user to confirm or reject it.
- **Confirmation_Binding**: The server-side record that ties one Action_Confirmation to the exact tool name, arguments, Principal and Conversation, is single-use and expires.
- **Activity_Trace**: The visible record inside a Turn of each tool the Assistant invoked, its key arguments and its outcome category.
- **Principal**: The signed-in Portal user on whose behalf a Turn runs, derived from the verified session credential.
- **Session_Credential**: The Cognito ID token the Portal frontend already sends as `Authorization: Bearer` to the Portal_API.
- **Portal_Role**: One of PortalAdmin, UseCaseAdmin, DataScientist, Operator, Viewer, DataLabeler.
- **Permission**: An entry of the existing `Permission` enum in `shared_utils` that the RBAC_Layer grants per Portal_Role and Use_Case.
- **RBAC_Layer**: The existing authorization code (`shared_utils.RBACManager`, `rbac_check`, `check_user_access`) that resolves a Principal's effective Portal_Role from the Portal_Identity_Registry and decides whether a Permission is held.
- **Portal_Identity_Registry**: The existing `USER_ROLES_TABLE` rows that grant Portal privilege when `PORTAL_REGISTRY_ENFORCED` is on; the token's `custom:role` is Claimed_Role only.
- **Use_Case**: The existing Portal tenancy unit to which models, devices, workflows and role assignments are scoped.
- **Audit_Log**: The existing Portal audit table and its writers, including `channel` and the MCP marker attributes.
- **Assistant_Audit_Marker**: The attributes on an Audit_Log entry that identify it as written during an Assistant Turn and distinguish it from external MCP clients and from browser activity.
- **Assistant_Switch**: The deployment-level control that includes or excludes the Assistant from a Portal deployment.
- **Assistant_Runtime_Toggle**: The PortalAdmin-operated setting that turns the Assistant off and on in a deployed Portal without a deploy.
- **Actions_Switch**: The control that allows the Assistant to invoke Mutating_Tools at all; off means guidance and Read_Only_Tools only.
- **Turn_Budget**: The design-specified bounds on one Turn: model calls, tool invocations, output tokens and wall-clock time.

## Requirements

### Requirement 1: The Assistant Is Present on Every Signed-In Page

**User Story:** As a Portal user, I want the Assistant available wherever I am in the Portal, so that I can ask for help without leaving the page I am working on.

#### Acceptance Criteria

1. WHILE a user is signed in and viewing any route rendered inside the authenticated Layout, THE Portal SHALL render the Assistant_Panel (expanded) or its Trigger (collapsed) on that page.
2. THE Portal SHALL NOT render the Assistant_Panel, its Trigger, or any Assistant network request on the `/login` route or on any other unauthenticated view (sign-in, new-password, forgot-password).
3. WHEN the user navigates between authenticated routes, THE Assistant_Panel SHALL keep the current Conversation, its scroll position, any unsent draft, and its expanded or collapsed state.
4. IF the Assistant_Panel throws during rendering or the Assistant_Backend is unreachable, THEN THE Portal SHALL continue to render the page content unaffected and SHALL show the failure inside the Assistant_Panel area only.
5. WHILE the Assistant_Switch is off for the deployment, or the Assistant_Runtime_Toggle (Requirement 12) is off, THE Portal SHALL render no Assistant control and SHALL make no Assistant network request other than learning the toggle's state.
6. THE Portal SHALL NOT render the Assistant for a user whose only Portal_Role is DataLabeler (the `/labeler` workspace), and THE Assistant_Backend SHALL refuse Turns from such a user. (Owner decision 1: no Assistant for DataLabeler-only users.)

### Requirement 2: Hiding, Showing and Layout

**User Story:** As a Portal user working on a dense page such as the workflow builder, I want to hide the Assistant to a small control and bring it back when I need it, so that it never costs me screen space I need.

#### Acceptance Criteria

1. THE Assistant_Panel SHALL provide one control that collapses it to the Trigger and the Trigger SHALL provide one control that expands it, both operable by pointer and by keyboard, each with an accessible name.
2. WHILE collapsed, THE Trigger SHALL occupy no more than a single button-sized area, SHALL NOT cover any interactive page element, and SHALL leave the fixed build banner at the viewport bottom visible.
3. WHILE expanded on a viewport wide enough to show the page content at Cloudscape's default minimum content width beside the panel, THE Assistant_Panel SHALL be shown beside the page content without covering it; on narrower viewports THE Assistant_Panel SHALL be shown as an overlay that the user can dismiss with the collapse control or the Escape key.
4. THE Portal SHALL remember the expanded or collapsed state per browser and restore it on the next page load. WHILE the user has never toggled the Assistant_Panel in that browser, THE Assistant_Panel SHALL be expanded, so that a user's first signed-in page shows it, except on `/workflows/builder` routes and on viewports where it would be an overlay (2.3), where it SHALL be collapsed. (Owner decision 2: expanded first.)
5. WHEN the user toggles the Assistant_Panel, the user's choice SHALL win on every route, including `/workflows/builder`, until the user toggles it again, mirroring the side-navigation behaviour of `Layout.tsx`.
6. WHILE the Assistant_Panel is collapsed and a Turn completes or an Action_Confirmation is waiting, THE Trigger SHALL show an unread indicator until the panel is expanded.
7. WHILE expanded beside the page content, THE Assistant_Panel SHALL keep the prompt input pinned and scroll only the message log, and SHALL NOT change the page's own scroll position.
8. THE Assistant_Panel SHALL expose the message log as a labelled region and announce new Assistant responses through a visually hidden live region, and every control in it SHALL be reachable in a logical keyboard order.

### Requirement 3: Prompts, Conversation and Page Context

**User Story:** As a Portal user, I want to ask questions in plain language about the page I am on and continue the conversation as I move through the Portal, so that the Assistant understands where I am without me explaining it.

#### Acceptance Criteria

1. THE Assistant_Panel SHALL accept a prompt of 1 to 4,000 characters and SHALL reject an empty or whitespace-only prompt without contacting the Assistant_Backend, indicating that a prompt is required.
2. WHEN the user submits a prompt, THE Assistant_Panel SHALL send with it the Page_Context: the current route path and its parameters, and the selected Use_Case identifier (or none).
3. THE Assistant_Panel SHALL NOT send form field contents, page text, the Session_Credential value, or any other page data as part of the Page_Context.
4. WHEN a Turn runs, THE Assistant_Backend SHALL include the earlier Turns of the same Conversation up to a design-specified window, so that follow-up prompts resolve references to earlier responses and tool results.
5. THE Assistant_Panel SHALL provide a "New conversation" action that starts an empty Conversation without deleting the previous one.
6. WHILE a Conversation is empty, THE Assistant_Panel SHALL offer at most four suggested prompts relevant to the current page (derived from the page's User_Guide chapter and the Journey), and selecting one SHALL submit it.
7. WHEN rendering a response, THE Assistant_Panel SHALL render a safe subset of Markdown (paragraphs, lists, emphasis, inline code, code blocks, tables, links) and SHALL NOT render raw HTML or execute scripts from response text.
8. WHEN a response contains a link to a Portal route, THE Assistant_Panel SHALL navigate within the single-page application on activation (no full reload), and WHEN it contains a link to the User_Guide or the branding `documentationUrl`, THE Assistant_Panel SHALL open it in a new tab.
9. WHILE a Turn is in progress in a Conversation, THE Assistant_Panel SHALL NOT accept a second prompt submission in that Conversation.

### Requirement 4: Guidance Along the Journey

**User Story:** As a new Portal user, I want the Assistant to tell me what this page is for, what to fill in, and what to do next on the path to a running inspection station, so that I can get through the application without reading the whole guide.

#### Acceptance Criteria

1. WHEN a prompt asks about a page, a field, a value or a control, THE Assistant SHALL answer from the User_Guide chapter that documents that page, and SHALL cite the chapter section it used (section title and link).
2. WHEN a prompt asks what to do next, or how far along the user is, THE Assistant SHALL determine the user's position on the Journey from Portal state read through Read_Only_Tools (for the selected Use_Case: whether datasets, labeling jobs, trained models, workflows, built components, registered devices and deployments exist) and SHALL state the next Journey_Step, the Portal page where it is done and the User_Guide chapter that explains it.
3. WHEN a Journey_Step or action requires a Portal_Role or Permission the Principal does not hold, THE Assistant SHALL say which role is required (from the Tool_Catalog declarations and the User_Guide's roles section) and SHALL NOT attempt the action.
4. IF the User_Guide and the Tool_Catalog contain nothing that answers a question, THEN THE Assistant SHALL say that it does not know and SHALL point to the branding `documentationUrl` or `supportEmail` where configured, rather than inventing an answer.
5. THE Assistant SHALL confine its responses to operating the DDA Portal, its devices and the documented workflows, and SHALL decline unrelated requests in one sentence.
6. WHEN the Assistant describes where to find something in the Portal, THE Assistant SHALL use the navigation labels and page names the Portal displays.
7. THE Assistant_Backend SHALL supply the model with the User_Guide text that is current for the deployed Portal build (the chapters shipped with the same release), so that guidance never describes a page the deployment does not have.

### Requirement 5: Tasks Through the Tool Catalog

**User Story:** As a Portal user who is stuck in the UI or prefers to ask, I want the Assistant to look things up and carry out tasks for me, with a clear confirmation before anything changes, so that I stay in control of what happens to my use case.

#### Acceptance Criteria

1. THE Assistant SHALL perform every look-up and every task through tools of the Tool_Catalog, executed through the Tool_Pipeline (lookup and phase gate, validation and confirmation check, mutating gate, authorization, dispatch, shaping, audit, telemetry), and SHALL NOT reach Portal_API handlers, tables or AWS services by any other path.
2. THE set of tools the Assistant_Model may call SHALL be derived from the Tool_Catalog at request time: the delivered phases' Read_Only_Tools always; the delivered phases' Mutating_Tools only while the Actions_Switch is on; and, among those, Destructive_Tools only when they are on the allow-list the owner approves at Phase 2 sign-off (Requirement 15.2); the tool names, descriptions and input schemas given to the model SHALL be the catalog's own.
3. WHEN answering requires a Read_Only_Tool, THE Assistant SHALL invoke it without asking and SHALL record it in the Turn's Activity_Trace (tool title, key arguments, outcome category), visible to the user.
4. WHEN the Assistant_Model proposes a Mutating_Tool, THE Assistant_Backend SHALL suspend the Turn and THE Assistant_Panel SHALL show an Action_Confirmation listing the tool's title and description, every argument name and value, the Use_Case it applies to, and whether it is DESTRUCTIVE; THE Assistant_Backend SHALL invoke the tool only after the user confirms that Action_Confirmation.
5. THE Assistant_Backend SHALL create a Confirmation_Binding for each Action_Confirmation and SHALL invoke the tool only when the confirmation presented matches the binding's tool name, arguments, Principal and Conversation exactly; a binding SHALL be usable once and SHALL expire after a design-specified time of at most 15 minutes.
6. THE Assistant_Backend SHALL remove any `confirm` argument the Assistant_Model supplies and SHALL set `confirm` to the value the catalog's `confirm_target` or `confirm_literal` rule requires only from a matching, unexpired Confirmation_Binding.
7. WHEN the user rejects an Action_Confirmation, THE Assistant_Backend SHALL invoke nothing, SHALL tell the Assistant_Model the action was declined, and THE Assistant SHALL continue the Turn without proposing the same invocation again in that Turn.
8. WHEN a task needs more than one Mutating_Tool, THE Assistant SHALL present each invocation as its own Action_Confirmation; one confirmation SHALL authorize exactly one invocation.
9. WHEN a Mutating_Tool completes, THE Assistant SHALL report the identifier of the created or modified resource (or the job identifier) and, where the catalog names a `status_tool`, SHALL offer to check or SHALL check the status on request.
10. WHEN a Use_Case-scoped tool is invoked, THE Assistant SHALL use the selected Use_Case from the Page_Context unless the user named another Use_Case in the Conversation, and the Action_Confirmation SHALL name the Use_Case explicitly.
11. WHEN a tool result is a collection or exceeds what fits in a response, THE Assistant SHALL summarize it and offer the Portal page where the full list is shown, rather than reproduce it in full.
12. WHEN the Assistant can offer the Portal page where a task is done, THE Assistant SHALL offer that in-app link alongside, or instead of, performing the task.
13. WHILE the Actions_Switch is off, THE Assistant SHALL explain, when asked to change something, that it can only look things up on this deployment and SHALL point to the page where the user can do it.

### Requirement 6: Identity, Authorization and Audit Parity with MCP

**User Story:** As a Portal administrator, I want every Assistant action to run as the signed-in user, under the same permission checks and in the same audit log as the Portal and the MCP server, with a marker that shows the Assistant did it, so that the Assistant can never do more than the user and I can trace what it did.

#### Acceptance Criteria

1. THE Assistant_Backend SHALL authenticate every request with the Principal's Session_Credential through the Portal's existing Cognito authorization (the same validation the Portal_API applies), SHALL derive the Principal from the verified claims only, and SHALL NOT store, forward or re-issue the credential.
2. THE Assistant_Backend SHALL NOT hold or use a service identity, API key, shared secret or stored token to invoke tools; every tool invocation SHALL carry the Principal's verified identity into the Tool_Pipeline exactly as the Portal_MCP_Server does.
3. THE Assistant_Backend SHALL authorize every tool invocation through the RBAC_Layer with the same resolution as the Portal_MCP_Server (Portal_Identity_Registry enforcement when on; `custom:role` as Claimed_Role only); a denial SHALL invoke nothing and SHALL write an `unauthorized_access` Audit_Log entry carrying the Assistant_Audit_Marker; a registry outage SHALL fail the invocation with `authorization_unavailable`, not downgrade silently.
4. WHEN a tool completes through the Assistant, THE Audit_Log SHALL contain the same entries it would contain for the same completion through the Portal_MCP_Server, each carrying the Assistant_Audit_Marker, which SHALL distinguish the Assistant from external MCP clients and from browser activity and SHALL carry a correlation identifier for the Turn.
5. THE Audit Logs page SHALL let holders of `view_audit_logs` filter to Assistant entries, and `GET /audit-logs` SHALL accept the corresponding filter value.
6. THE Assistant_Backend SHALL ignore any identity, role or Use_Case-membership claim supplied by the client in the Page_Context or prompt; the Page_Context SHALL influence only which guide content is used and which Use_Case a tool defaults to.
7. THE Assistant SHALL NOT provide any tool, parameter or mode that elevates the Principal's effective Portal_Role or acts as another user.
8. IF the Session_Credential expires or is rejected during a Turn, THEN THE Assistant_Backend SHALL end the Turn with an authentication error, SHALL invoke no further tool, and THE Assistant_Panel SHALL present the Portal's standard session-expiry behaviour (return to the same page after sign-in) rather than retrying with a cached credential.
9. WHEN the Principal's account is disabled or deleted, THE Assistant SHALL stop acting for that Principal no later than the Portal_API stops accepting its Session_Credential.

### Requirement 7: Model Invocation

**User Story:** As a Portal administrator, I want the Assistant to use the Bedrock model I already configured for the Portal's other AI features, with its cost kept predictable, so that one configuration governs all Bedrock use in the account.

#### Acceptance Criteria

1. THE Assistant_Backend SHALL read the model id, region, max tokens, temperature, top_p and timeout from the Bedrock_Configuration in effect for each model call, with the same default-substitution, unset-parameter and temperature/top_p rules as the custom-node-code-assist feature (its Requirement 4), and SHALL invoke the model through the Converse API with tool definitions.
2. THE Assistant_Backend SHALL compose the system prompt, the User_Guide material and the tool definitions server-side; THE Assistant_Panel SHALL have no way to add to, replace or reorder them.
3. THE Assistant_Backend SHALL derive the tool definitions given to the model from the Tool_Catalog's own names, descriptions and input schemas, with no hand-maintained copy, and a test SHALL fail when the two differ.
4. THE Assistant_Backend SHALL order each model request with its static content first (system prompt, guide material, tool definitions) and the Conversation last, and WHERE the configured model supports explicit prompt caching, SHALL place cache checkpoints after the static content.
5. THE Assistant_Backend SHALL enforce a Turn_Budget — a maximum number of model calls and tool invocations per Turn, a maximum response length, and a maximum Turn duration — and WHEN a budget is reached, THE Assistant SHALL end the Turn with a response that says which limit was reached and what was done so far.
6. IF the configured model rejects tool definitions (does not support tool use), THEN THE Assistant SHALL operate in guidance-only mode for that deployment, THE Assistant_Panel SHALL state that actions are unavailable, and the Settings page SHALL show PortalAdmins which model is configured and that it lacks tool use.
7. WHERE a Bedrock Guardrail identifier is configured for the Assistant, THE Assistant_Backend SHALL apply it to every model call.

### Requirement 8: Response Delivery

**User Story:** As a Portal user, I want to see the Assistant working and to be able to stop it, so that a slow or wrong answer never leaves me waiting or guessing.

#### Acceptance Criteria

1. WHEN a prompt is submitted, THE Assistant_Panel SHALL show an in-progress state within 1 second and SHALL display the response text progressively as the Assistant_Model produces it, with each tool invocation appended to the Activity_Trace as it starts and completes.
2. WHILE a Turn is in progress, THE Assistant_Panel SHALL offer a Stop control; WHEN the user stops a Turn, THE Assistant_Backend SHALL invoke no further tool in that Turn, SHALL keep the response text produced so far marked as stopped, and SHALL leave a Mutating_Tool already dispatched to complete (its outcome is reported on the next Turn or through the status tool).
3. WHEN a Turn exceeds the Turn_Budget's duration, THE Assistant_Backend SHALL end it as in Requirement 7.5, and THE Assistant_Panel SHALL show the partial response rather than discard it.
4. WHILE a Turn is in progress, THE Portal SHALL remain fully usable: the user can navigate, use the page and collapse the Assistant_Panel, and the Turn continues.
5. IF the connection carrying a Turn's response breaks before the Turn ends, THEN THE Assistant_Panel SHALL show the text received so far marked as incomplete and SHALL let the user re-open the Conversation to see the Turn's stored final state.

### Requirement 9: Conversation Persistence and Privacy

**User Story:** As a Portal user, I want my conversations kept for me across pages, sessions and browsers, and kept private, so that I can pick up where I left off and nobody else reads what I asked.

#### Acceptance Criteria

1. THE Assistant_Backend SHALL store each Conversation server-side keyed by the Principal, and SHALL return, continue or delete a Conversation only for the Principal that owns it; no Portal_Role, including PortalAdmin, SHALL grant read access to another Principal's Conversations. (Owner decision 3: server-side storage.)
2. THE Assistant_Panel SHALL list the Principal's recent Conversations (title and last activity), let the user reopen one, and let the user delete one; deletion SHALL remove the Conversation's content immediately and SHALL leave Audit_Log entries untouched.
3. THE Assistant_Backend SHALL expire Conversations after a retention period of inactivity that a PortalAdmin sets in the Settings Assistant section (Requirement 12.1), within a design-specified range of at least 1 and at most 365 days, defaulting to 30 days; expiry SHALL delete the Conversation's content, and a changed period SHALL apply to existing Conversations no later than their next activity. (Owner decision 4: configurable.)
4. THE Assistant_Backend SHALL store Conversation content encrypted at rest with a key the design names, consistent with the Portal's encryption-at-rest choices for tables holding user data.
5. THE Assistant_Backend SHALL NOT write prompt text, response text, tool arguments or tool results to CloudWatch logs or to the Audit_Log; its logs and audit entries SHALL carry identifiers, tool names, categories, sizes, durations and token counts only.
6. THE Assistant_Backend SHALL bound a Conversation's stored size and Turn count (design-specified) and WHEN the bound is reached SHALL ask the user to start a new Conversation rather than drop earlier Turns silently.

### Requirement 10: Safety and Abuse Resistance

**User Story:** As a Portal administrator, I want the Assistant to be hard to misuse, by users and by content it reads, so that enabling it does not widen the Portal's attack surface.

#### Acceptance Criteria

1. THE Assistant_Backend SHALL treat tool results, User_Guide text and Page_Context as data: no instruction contained in them SHALL cause a Mutating_Tool to run without an Action_Confirmation, and the system prompt SHALL say so to the model.
2. THE Assistant_Backend SHALL expose no endpoint through which the client names a tool to run; tools SHALL run only when proposed by the Assistant_Model within a Turn and, for Mutating_Tools, confirmed through a Confirmation_Binding.
3. THE Assistant_Backend SHALL enforce per-Principal limits — concurrent Turns, Turns per hour, and model tokens per day — with design-specified defaults, and WHEN a limit is reached SHALL refuse the Turn with a message that names the limit and when it resets.
4. THE Actions_Switch SHALL default to on once Phase 2 is deployed, because every Mutating_Tool invocation is already gated by an Action_Confirmation; a PortalAdmin SHALL be able to turn it off and on in the Settings Assistant section (Requirement 12.1) without a deploy, and each change SHALL be recorded in the Audit_Log. (Owner decision 5: default on.)
5. THE Assistant SHALL inherit the Tool_Pipeline's result shaping and error scrubbing, so that credential-like material, other Use_Cases' identifiers and stack traces never reach the Assistant_Model or the user.
6. THE Assistant_Backend SHALL validate every client message against a schema (prompt length, Page_Context shape, Conversation and confirmation identifiers) and SHALL reject anything else with an input error before any model or tool call.

### Requirement 11: Failure Handling

**User Story:** As a Portal user, I want clear messages when the Assistant cannot answer or act, so that I know whether to retry, do it myself, or call an administrator.

#### Acceptance Criteria

1. IF a Bedrock call fails with throttling, authorization, model-access or model error, or does not complete within the configured timeout, THEN THE Assistant_Panel SHALL show which of these occurred (and the timeout value for a timeout) and SHALL keep the user's prompt available for resubmission.
2. IF a tool invocation fails, THEN THE Assistant SHALL report the Tool_Pipeline's failure category and message to the user (and to the model, so it can adapt), without stack traces or internal identifiers.
3. IF a dispatched Mutating_Tool's outcome is unknown (timeout after dispatch), THEN THE Assistant SHALL say that the effect is unknown and SHALL offer the status tool or the Portal page to verify, mirroring the Tool_Pipeline's effect-unknown behaviour.
4. IF the Assistant_Backend is unreachable or returns a server error, THEN THE Assistant_Panel SHALL show an unavailable state with a retry control, and the rest of the page SHALL be unaffected.
5. IF the Bedrock_Configuration storage is unreadable, THEN THE Assistant_Backend SHALL use the same default configuration as the other Bedrock-backed features and SHALL not fail the Turn because of the read failure.

### Requirement 12: Administrators Can Disable the Assistant in Settings

**User Story:** As a Portal administrator, I want to switch the Assistant off for everyone from the Settings page, and back on later, without a deploy, so that I can stop its use and its cost at once when I need to.

#### Acceptance Criteria

1. THE Settings page SHALL present an Assistant section to PortalAdmins only, containing the Assistant_Runtime_Toggle (on or off), the Actions_Switch (on or off; Phase 2), the Conversation retention period in days (editable, Requirement 9.3), and the Assistant's current state: the configured model and whether it supports tool use.
2. THE Assistant_Runtime_Toggle SHALL default to on in a deployment whose Assistant_Switch is on; the deploy-time Assistant_Switch stays the way to leave the Assistant's resources out of a deployment altogether (Requirement 13.1).
3. WHEN a PortalAdmin turns the Assistant_Runtime_Toggle off, THE Assistant_Backend SHALL refuse new Turns with a "disabled by an administrator" error, SHALL make no further model call and invoke no further tool in Turns already in progress, and SHALL make no Bedrock call while the toggle is off.
4. WHEN the Assistant_Runtime_Toggle is turned off, THE Portal SHALL stop rendering the Assistant_Panel and its Trigger for every signed-in user within 60 seconds, without requiring them to sign out or reload, and nothing else on the page SHALL change.
5. WHEN a PortalAdmin turns the Assistant_Runtime_Toggle back on, THE Assistant SHALL be available to users within 60 seconds, with their stored Conversations intact and the Actions_Switch at the value it had before.
6. THE Assistant_Backend SHALL gate writes of the Assistant_Runtime_Toggle, the Actions_Switch and the retention period, and reads of the Assistant section's details (model, Actions_Switch, retention), server-side by a PortalAdmin-only Permission, following the `bedrock-config:write` precedent, independent of what the Settings page shows; the on/off state alone SHALL be readable by every signed-in user, so that the Portal can honour Requirement 1.5.
7. WHEN the Assistant_Runtime_Toggle, the Actions_Switch or the retention period changes, THE Portal SHALL write an Audit_Log entry recording the acting user, the setting, the previous value and the new value.
8. THE Assistant_Runtime_Toggle SHALL NOT alter the Bedrock_Configuration or the Actions_Switch, and a Bedrock_Configuration change SHALL NOT alter the toggle, so that disabling the Assistant never affects the Portal's other Bedrock-backed features.

### Requirement 13: Operations and Documentation

**User Story:** As a Portal administrator, I want to deploy, watch and document the Assistant with the same tools as the rest of the Portal, and to know what it costs.

#### Acceptance Criteria

1. THE Assistant SHALL be deployed by the Portal's existing deploy scripts behind the Assistant_Switch (deploy-time; default on), and a deployment with the switch off SHALL contain no Assistant resources; removing the Assistant from a deployment that has it SHALL not be blocked by exports other stacks import.
2. THE Assistant_Backend SHALL publish metrics for Turns, model calls, tool invocations by outcome category, Action_Confirmations (confirmed, rejected, expired), Bedrock latency and token usage, and SHALL raise alarms on sustained model errors and on Bedrock throttling.
3. THE Assistant SHALL stay within the Portal API Gateway limits (resource headroom, authorizer quota, nested-stack resource limit) and the design SHALL state the resources it adds.
4. THE Assistant's IAM roles SHALL grant least privilege: Bedrock invocation on foundation models and the account's inference profiles only, invocation of the Tool_Pipeline's backing functions only where the design dispatches to them, and access to its own tables only; the repository's IAM synthesis gates SHALL pass.
5. THE documentation SHALL be updated: an ADMIN_GUIDE section (enabling, the Settings toggle, switches, retention, cost, troubleshooting), a new User_Guide chapter describing the Assistant (what it can see, what it can do, how confirmation works) that the Assistant itself is grounded on, a `COST_ESTIMATE.md` entry with a per-Turn token estimate, and a README pointer.
6. THE Assistant_Panel SHALL show a one-line disclaimer that responses are generated and should be checked before acting on them, placed where Cloudscape's chat pattern puts constraint text.

### Requirement 14: Testing and Verification

**User Story:** As the owner, I want confidence that the confirmation gate, the identity path and the audit marker cannot regress silently, and that the model-dependent behaviour is checked before each release.

#### Acceptance Criteria

1. Backend tests SHALL cover: tool definitions equal the Tool_Catalog's (7.3); a model-supplied `confirm` is removed and a tool with a confirmation rule never runs without a matching binding (5.5, 5.6); a binding is single-use and expires (5.5); client-supplied identity is ignored (6.6); every tool completion produces the Assistant_Audit_Marker (6.4); no prompt or result text reaches logs (9.5); the Turn_Budget and per-Principal limits (7.5, 10.3); Bedrock failure categories (11.1); the Assistant_Runtime_Toggle refuses new Turns, stops in-progress Turns, is PortalAdmin-gated and is audited (12.3, 12.6, 12.7).
2. Frontend tests SHALL cover: the panel or Trigger on every authenticated route and absent on `/login` (1.1, 1.2); collapse/expand state persistence and the builder-route default (2.4, 2.5); Conversation survives navigation (1.3); the Action_Confirmation shows every argument and the Use_Case (5.4); safe Markdown rendering (3.7); the render-failure isolation (1.4); the DataLabeler rule (1.6); the Settings section visible to PortalAdmins only and the panel disappearing when the toggle turns off (12.1, 12.4).
3. Infrastructure tests SHALL assert the Assistant stack's resources, the route-auth inventory, the authorizer count and the IAM gates (13.3, 13.4), and SHALL pass with the Assistant_Switch on and off.
4. The repository's property-based testing convention SHALL be applied to the Confirmation_Binding (match ⇔ identical tool, arguments, Principal, Conversation) and to the Markdown renderer (no raw HTML survives).
5. Model-dependent behaviour (4.1, 4.2, 4.4, 4.5, 5.3, 5.4, 10.1) SHALL be verified by an evaluation set of at least 20 scripted prompts with expected outcomes (tool called or not, confirmation shown or not, citation present, declined), run against the deployed Assistant before each release and recorded in the spec's verification notes.
6. A deployed smoke check SHALL exercise: a guidance Turn on a page, a Read_Only_Tool Turn, a confirmed Mutating_Tool Turn in a test Use_Case, a rejected Action_Confirmation, the Audit Logs filter showing the Assistant entries, and the Assistant_Runtime_Toggle turning the panel off.
7. `backend/tests/mcp` SHALL remain green after this feature; any catalog or shared-layer change it needs SHALL be made in the same commit as the matching test updates.

### Requirement 15: Phased Delivery

**User Story:** As the owner, I want guidance and look-ups first, and state-changing actions only after the confirmation flow has been exercised in practice.

#### Acceptance Criteria

1. Phase 1 SHALL deliver Requirements 1–4, 6–14 and the Read_Only_Tool parts of Requirement 5, with the Actions_Switch forced off and not shown in the Settings section; the Action_Confirmation flow SHALL be implemented and tested but reachable only when the switch is on.
2. Phase 2 SHALL deliver the Mutating_Tool parts of Requirement 5 (Action_Confirmation, Confirmation_Binding, `status_tool` follow-up), the Actions_Switch (default on, Requirement 10.4) and a Destructive_Tool allow-list that the owner approves at Phase 2 sign-off; Destructive_Tools outside the allow-list SHALL NOT be offered to the Assistant_Model. (Owner decision 6: allow-list.)
3. Each phase SHALL end with the deployed smoke check (14.6) and the evaluation set (14.5) recorded in the spec's verification notes.

## Non-Negotiable Constraints

- **C1 — Same identity, authorization and audit path as MCP.** The Assistant acts only as the signed-in Principal, through the Tool_Catalog and the Tool_Pipeline. No service identity, no second credential, no stored tokens, no synthesized identity.
- **C2 — No state change without the user's confirmation.** Every Mutating_Tool invocation is preceded by an Action_Confirmation bound server-side to the exact tool, arguments, Principal and Conversation; `confirm` is never taken from the model or the client.
- **C3 — One tool catalog.** The Assistant adds no tool that reaches Portal state outside `mcp_portal/catalog.py`. Assistant-local functions are limited to reading the User_Guide and proposing navigation.
- **C4 — No conversation content in logs or audit.** CloudWatch and the Audit_Log receive identifiers and metadata only.
- **C5 — The MCP server's behaviour for external clients is unchanged.** `backend/tests/mcp` stays green; shared-layer or catalog changes are additive and land with their tests.
- **C6 — API Gateway limits are respected.** Resource headroom (41 of 300 in Oct 2026), the full authorizer quota, the nested-stack limit and the route-auth inventory gate are counted in the design.
- **C7 — Cloudscape frontend conventions.** New dependencies are limited to Cloudscape packages and, if needed, one Markdown renderer with sanitization; no other UI framework.
- **C8 — Public repository hygiene.** No deployment identifiers (account ids, pool ids, API ids, device names) in committed spec, code or docs text.
- **C9 — Removable.** The Assistant can be excluded from or removed from a deployment without a change to any other stack's exports.

## Open Design Decisions

The design phase must resolve each item and record the choice with reasoning. This document does not choose.

### Decision 1: Backend placement and Tool_Pipeline reuse
User need: one implementation of validation, authorization, dispatch and audit, so the Assistant can never drift from MCP. Facts: steps 2–9 of `on_call_tool` are a closure in `mcp_portal/server.py`; `Principal` is built from an access token (`principal_from_access_token`), while the Assistant has ID-token claims from the REST authorizer; `dispatch.invoke` is a Lambda-to-Lambda call keyed by `MCP_BACKEND_FUNCTIONS_PARAMETER`; `tests/mcp` pins catalog facts. Options: (a) a new Assistant Lambda that imports `mcp_portal` in-process (same layers, same SSM map, a `principal_from_claims`) after refactoring the closure into a `call_tool(spec, args, principal, ctx)` function both servers use; (b) the Assistant Lambda invokes the MCP Lambda through a trusted internal entry point; (c) the Assistant runs inside the MCP Lambda behind a second route. The design must choose, state the refactor's blast radius on `tests/mcp`, and define the Assistant's `Principal` construction.

### Decision 2: Transport for a Turn
User need: progressive display (8.1), stop (8.2), Turns longer than 29 seconds with several tool calls, and the Portal's existing Cognito authentication. Facts: the Portal REST API is edge-optimized (30-second stream idle timeout; a Regional API has 5 minutes); REST response streaming needs `STREAM` transfer mode, `InvokeWithResponseStream` and the streaming output format; Python Lambdas cannot stream natively (Node.js runtime, custom runtime or Lambda Web Adapter); the Tool_Pipeline and shared layer are Python; the browser has `fetch` + `ReadableStream`. Options: a Regional REST API (or the MCP HTTP API's pattern) with a streaming Lambda (Node.js front streaming + Python pipeline, or Python + Web Adapter); buffered REST calls with a client-driven step loop (one model or tool step per call under 29 s); an asynchronous Turn (start, poll or subscribe for events); a WebSocket API. The design must choose, define the event stream format (text deltas, tool start/end, confirmation request, end, error), and show how 8.5 (reconnect to stored state) is met.

### Decision 3: Assistant_Audit_Marker vocabulary
User need: tell Assistant entries from external MCP clients and browser activity (6.4, 6.5). Facts: `channel` is `'api'`/`'mcp'`; `GET /audit-logs` validates `channel` to those two; the MCP marker carries `mcp_client`; `tests/mcp` has audit-flag tests. Options: a new `channel='assistant'` (touches `shared_utils`, `audit_logs.py`, the page, and the MCP audit tests' vocabulary), or `channel='mcp'` with a fixed `mcp_client` label (`dda-portal-assistant/<version>`) and a `mcp_server` value naming the Assistant, plus a client filter on the audit page. The design must choose and list every file and test the choice touches.

### Decision 4: Grounding the model in the User_Guide
User need: accurate, cited guidance for the current page and the Journey (4.1, 4.2, 4.7) at predictable cost. Facts: 25 chapters, 26,444 words (≈ 35k tokens); chapter ↔ page mapping is in `README.md`; images are not useful to a text model; prompt caching requires a byte-identical prefix. Options: ship the guide text in the Lambda asset at build time and select the current page's chapter plus the index per Turn; ship the whole guide with a cache checkpoint; a Bedrock Knowledge Base (new vector store, cost and sync). The design must choose, define how the guide text is bundled so it matches the deployed build (4.7), and how citations (section title + link) are produced.

### Decision 5: Tool exposure per Turn
User need: the model sees the right tools without paying for all 183 (≈ 31k tokens) on every call. Facts: per-domain definition sizes above; the catalog has `domain` and view filtering; the Page_Context names the page. Options: all tools with a cache checkpoint; the current page's domain plus a tool-search meta-function; two-stage selection (intent → domain → tools). The design must choose and estimate per-Turn input tokens.

### Decision 6: Chat UI and panel mechanics
User need: Requirements 2 and 3 on Cloudscape. Facts: `AppLayout` offers the tools drawer (`toolsHide` today) and a `drawers` API with triggers, resizable widths and overlay behaviour on narrow viewports; `@cloudscape-design/chat-components` is not installed; no Markdown renderer or sanitizer is present. The design must choose the panel mechanism, the chat components, the Markdown renderer and sanitizer, how in-app links are intercepted, and where the Trigger sits relative to the fixed build banner.

### Decision 7: Conversation store and Confirmation_Binding
User need: Requirements 5.5, 9. Facts: Portal tables holding user data use a customer managed key with documented key-access grants (memory `portal-kms-encryption`); the IAM gate needs per-principal approvals for new key access. The design must define the table(s), keys (`sub` + Conversation id), TTL attribute, size bounds, the binding record (hash of tool + canonical arguments + `sub` + Conversation id, expiry, used flag), and the encryption key.

### Decision 8: Switches, limits and settings
User need: Requirements 10.3–10.4, 12, 13.1. Facts: `mcpMutatingToolsEnabled` is a CDK context flag read as an environment variable by the MCP Lambda; Portal settings live in `SETTINGS_TABLE` with the Bedrock configuration precedent and a PortalAdmin-only UI. The design must define the Assistant_Switch (context flag + deploy-script guard, as `DEPLOY_MCP_SERVER`), the Assistant_Runtime_Toggle (setting key, how the backend and every open browser tab learn a change within 60 s — a short-TTL read on each Turn and a lightweight state poll or the refusal response itself), the Actions_Switch (context flag, setting, or both; relation to `mcpMutatingToolsEnabled`), default limits (concurrent Turns, Turns per hour, tokens per day, model calls and tool calls per Turn, Turn duration) and the Turn_Budget.

### Decision 9: Evaluation harness
User need: Requirement 14.5. The design must define the evaluation set format, how it drives the deployed Assistant with a test account, how model nondeterminism is handled (pass thresholds, retries), and where results are recorded.

## Flagged Items (kept, not dropped; the design must address)

- **F1 — Python cannot stream from Lambda natively.** The Tool_Pipeline is Python; progressive display (8.1) needs a streaming-capable runtime or adapter in front of it. Decision 2.
- **F2 — The Portal REST API is edge-optimized.** Its 30-second stream idle timeout defeats long tool calls; a Regional API (as the MCP stack has) or a non-streaming transport is needed. Decision 2.
- **F3 — The model can emit `confirm`.** The catalog's destructive guard would accept a model-supplied `confirm`; the Assistant_Backend must strip it (5.6, C2). A test must prove it.
- **F4 — Prompt injection through tool results and guide text.** Tool results are Portal data written by users (names, descriptions, logs). The structural control is the confirmation gate for Mutating_Tools plus the Turn_Budget for Read_Only_Tools; the design should also consider marking tool results as untrusted in the prompt.
- **F5 — Token cost of tool definitions.** 183 definitions ≈ 31k tokens per model call without caching or filtering; multiplied by several model calls per Turn this dominates cost. Decision 5 and 7.4.
- **F6 — DataLabeler exposure.** Labeling teams can include external people with the DataLabeler role; giving them a Bedrock-backed Assistant has cost and exposure consequences. Requirement 1.6 defaults them out; owner decision 1.
- **F7 — Images and large collections in tool results.** Some tools return `ImageContent` (previews, captures) or collections cut at 200 KB. Requirement 5.11 asks for summaries; the design decides whether images are shown in the panel in Phase 1.
- **F8 — Conversations hold Portal data.** Tool results stored in Conversations carry the same classification as the Portal data they came from; retention (9.3), deletion (9.2) and encryption (9.4) apply. Deleting a Use_Case does not purge Conversations that mention it; the design should say whether that is acceptable.
- **F9 — Mutating switch semantics differ.** External MCP clients confirm nothing (Kiro's approval is client-side), so `mcpMutatingToolsEnabled` is a blunt deploy-time switch. The Assistant confirms every mutating action in the UI, so a deployment may want the Assistant's actions on while external MCP mutating tools stay off, or vice versa. Decision 8.
- **F10 — Guide freshness.** Guidance grounded on a guide version other than the deployed build's would describe pages the deployment does not have (4.7). The build must bundle the guide from the same commit as the frontend.
- **F11 — Lambda billing after disconnect.** A Turn keeps running (and billing) when the browser disconnects; the Stop control and the Turn_Budget bound it, and the design must make the backend observe client cancellation where the transport allows.

## Out of Scope

- An assistant on the device web UI or the Quality Station HMI, and tools of the Edge MCP server (each device's server is reachable only on its LAN or through a tunnel).
- Voice input, file or image upload, screenshots, or sharing page text or form contents with the Assistant (a possible follow-up; see Requirement 3.3).
- Changing the Portal MCP server's behaviour for external clients, its catalog rows, or its phases, beyond the additive audit-marker change Decision 3 may require.
- Non-Bedrock model providers, fine-tuning, custom models, or a model selection different from the Bedrock_Configuration.
- Shared or multi-user Conversations, and administrator access to other users' Conversations.
- Analysis of response feedback (thumbs up/down): the panel MAY collect it in the Cloudscape pattern's form; storing and acting on it is a follow-up.
- Proactive or unsolicited Assistant messages (tours, pop-ups, nudges) beyond the suggested prompts of Requirement 3.6 and the unread indicator of Requirement 2.6.
- Any implementation, infrastructure change, deploy, or git commit in this spec-authoring run.
