# Implementation Plan: Portal AI Assistant

## Overview

Phase 1 of the design (guidance, read-only tasks, the toggle) in a sibling worktree on branch `feature/portal-ai-assistant` cut from `origin/integration/all-specs` (`770926f3`, 2026-11-03). Phase 2 (actions) follows the owner's allow-list sign-off. Every task names the requirements it serves; the design sections are in parentheses.

Standing rule: implementation and local tests run autonomously; a live AWS change (`cdk deploy`), any lab-device step, and any push to the public remote each wait for the owner's numbered go.

### Verification commands

- Portal MCP and Assistant pytest: `cd edge-cv-portal/backend && uv run --no-project --python 3.11 --with-requirements requirements-dev.txt --with hypothesis --with moto -m pytest tests/mcp tests/assistant -q`
- Infra: `cd edge-cv-portal/infrastructure && npm run build && npx jest`
- Frontend: `cd edge-cv-portal/frontend && npx vitest run`
- Hygiene sweep before every commit: no 12-digit account ids, pool ids, API ids, device names, task-notes directory names or home paths in the diff.

## Tasks

### Wave 0 — branch and spec

- [x] 0.1 Create the worktree `~/github/jp5/main/portal-ai-assistant` on `feature/portal-ai-assistant` from `origin/integration/all-specs`; copy `requirements.md`, `design.md`, `tasks.md` into `.kiro/specs/portal-ai-assistant/`; commit `docs(spec): add the portal AI assistant spec`.

### Wave 1 — MCP pipeline refactor (design §1–§3; Requirements 5.1, 6.1–6.4, 14.7)

- [x] 1.1 Add `mcp_portal/pipeline.py` with `ToolOutcome` and `call_tool`; move `find_spec`, `validation_failure`, `_target_value`, `expected_confirm`, `confirmation_failure` into it; re-export them from `server.py`; rewrite `on_call_tool` as authenticate → look up → `call_tool` → render.
- [x] 1.2 Add `McpContext.channel` (default `mcp`) and write it in `dispatch.build_backend_event` and `audit.marker_event`; add the `event` keyword to `telemetry.log_tool_completion` (`assistant_tool` lines, same pinned keys).
- [x] 1.3 Add `principal_from_claims` to `mcp_portal/principal.py`.
- [x] 1.4 Shared layer: `AUDIT_CHANNEL_ASSISTANT`, `MARKED_CHANNELS`, the two channel sites in `attribution_from` / `_attribution_attributes`; `audit_logs.py` accepts `channel=assistant`; move `categorize_bedrock_error` to `bedrock_common.py` with a re-import in `code_assist.py`.
- [x] 1.5 Tests: `tests/mcp/test_pipeline_equivalence.py` (P13), channel tests for `assistant` in the attribution and audit-logs filter suites; run the whole `tests/mcp` suite and record the counts (must equal the trunk's 3,799 passed plus the new tests).
- [x] 1.6 Commit `refactor(mcp): extract the tool pipeline and add the assistant audit channel`.

### Wave 2 — Assistant backend package (design §4, §5, §7, §8, §9)

- [x] 2.1 `assistant/__init__.py`, `errors.py`, `schemas.py`, `limits.py`, `telemetry.py` (Requirements 3.1, 10.3, 10.6, 9.5, 13.2).
- [x] 2.2 `store.py`: conversations, messages, turns with `append_progress`, bindings with `consume_binding`, quotas, canonical JSON and hashing, TTLs (Requirements 5.5, 9.1–9.4, 9.6, 10.3).
- [x] 2.3 `settings.py`: configuration defaults, read (cached and uncached), write with per-field audit entries, model capability record (Requirement 12).
- [x] 2.4 `guide.py` and `assistant/route-chapters.json`: route → chapter map, bounded reads, section lookup (Requirements 4.1, 4.7).
- [x] 2.5 `journey.py`: the eight steps, `Summary`, `next_step`, `summary_from_outcomes` (Requirement 4.2).
- [x] 2.6 `tools.py`: exposure, `bedrock_tool_spec` with `oneOf` flattening, local tools (`guide_read_section`, `journey_status`, `find_tools`), `ToolOutcome` → `toolResult` (Requirements 5.1–5.3, 7.3; design D5, D10, D11).
- [x] 2.7 `prompts.py`: system blocks S1–S4 (Requirements 4.3–4.6, 7.2, 10.1).
- [x] 2.8 `bedrock.py`: `converse_stream` wrapper and normalized events, request assembly with cache points, guardrail, error categorization, tool-use capability detection (Requirements 7.1, 7.4, 7.6, 7.7, 11.1).
- [x] 2.9 `turn.py`: the loop of design §8.2, confirmation suspend and resume (§8.3; reachable only when `ACTIONS_DELIVERED`), stop, budgets, failures, final persistence (Requirements 5.3–5.9, 6.8, 7.5, 8.2–8.3, 11.2–11.3).
- [x] 2.10 `api.py` and `worker.py`: the 11 routes, ownership, quotas, worker invocation, lazy reap, state and settings routes (Requirements 1.5, 1.6, 3.2, 3.3, 6.1, 6.6, 9.2, 10.2, 10.6, 12.1–12.8).
- [x] 2.11 Tests `backend/tests/assistant/`: unit tests per module, API route tests (moto), worker tests with recorded Bedrock streams, properties P1–P10, P13 harness reuse; log-hygiene sentinel test (Requirement 14.1, 14.4).
- [x] 2.12 Commit `feat(assistant): add the Portal assistant backend`.

### Wave 3 — Infrastructure (design §10, §11)

- [x] 3.1 `lib/mcp-layer.ts`: move `MCP_LAYER_SOURCE_DIR` and `mcpLayerBundling()` out of `mcp-stack.ts`; `context-helpers.ts`: `assistantEnabled()`.
- [x] 3.2 `lib/assistant-stack.ts`: key, table, SSM parameter, three layers, two roles, two functions (worker `retryAttempts: 0`, reserved concurrency 10), Regional REST API with authorizer, methods, throttles, CORS, alarms, output.
- [x] 3.3 `bin/app.ts` wiring behind `deployAssistant`; `scripts/portal-assistant-stack.sh` guard (standalone copy of the MCP guard), `deploy-infrastructure.sh` hook; `deploy-frontend.sh` writes `assistantApiUrl` into `config.json` (`frontend-stack.ts` unchanged).
- [x] 3.4 Route-auth inventory: add the stack, regenerate `ROUTE_AUTH.md`; IAM gate approvals for the two roles and the key grant.
- [x] 3.5 Tests: `assistant-stack.test.ts`, inventory and authorizer-count suites, synth with `deployAssistant=false`, bats for the guard; `npm run build && npx jest` green; the IAM synth gate green (Requirements 13.1, 13.3, 13.4, 14.3).
- [x] 3.6 Commit `feat(assistant): add EdgeCVPortalAssistantStack`.

### Wave 4 — Frontend (design §12, §13)

- [x] 4.1 Dependencies: `@cloudscape-design/chat-components`, `react-markdown`, `remark-gfm`; `config.ts` fields; `assistantApi.ts` with the shared 401 refresh and no loading bar.
- [x] 4.2 `AssistantProvider.tsx`: availability, first-visit and builder rules, persistence, polling with backoff, unread, state poll every 30 s and on `assistant_disabled` (Requirements 1.1–1.6, 2.4–2.6, 3.4, 3.9, 8.4, 12.4).
- [x] 4.3 `AssistantDrawer.tsx`, `MessageMarkdown.tsx`, `ActivityTrace.tsx`, `ConfirmationCard.tsx`, `ConversationList.tsx`, `suggestedPrompts.ts`, `AssistantErrorBoundary.tsx` (Requirements 1.4, 2.1–2.3, 2.7–2.8, 3.1, 3.5–3.8, 5.3–5.4, 8.1–8.2, 8.5, 9.2, 11.1, 11.4, 13.6).
- [x] 4.4 `Layout.tsx` drawer wiring and banner padding; `AuditLogs.tsx` option; `Settings.tsx` tab and `AssistantSettings.tsx` (Requirements 6.5, 12.1, 12.6).
- [x] 4.5 Tests: `Layout.assistantDrawer.test.tsx`, `AssistantDrawer.test.tsx` (provider and drawer), `MessageMarkdown.property.test.tsx` (P12), `AssistantSettings.test.tsx`, `assistantUnits.test.ts` (P11 and the pure helpers), the audit filter test; `npx vitest run` green (Requirement 14.2).
- [x] 4.6 Commit `feat(assistant): add the assistant drawer, settings tab and audit filter`.

### Wave 5 — Documentation and evaluation harness

- [x] 5.1 `docs/user-guide/25-assistant.md` and the `README.md` index row; `README.md` pointer; `ADMIN_GUIDE.md` "Portal assistant" section; `COST_ESTIMATE.md` rows (Requirement 13.5).
- [x] 5.2 `edge-cv-portal/tests/assistant-eval/`: `cases.json` (≥ 20 Phase-1 cases), `run_eval.py`, `smoke_check.sh`, README (Requirements 14.5, 14.6; design D9).
- [x] 5.3 Commit `docs(assistant): user guide chapter, admin guide section, cost rows and the evaluation harness`.

### Wave 6 — Owner gates (each waits for a numbered go)

- [x] 6.1 **Deploy gate.** Read-only `cdk diff` of the full app with the deployed context; present the diff summary (new stack only, no change to other stacks' templates beyond the shared-layer asset hash); on go: `./deploy-infrastructure.sh` then `./deploy-frontend.sh`.
- [x] 6.2 Validate-first (i)–(v) on the deployed stack; smoke check (14.6) and the evaluation set (14.5); write `verification-notes.md`.
- [ ] 6.3 **Push gate.** Hygiene sweep of the branch; on go: push `feature/portal-ai-assistant`, open the PR to `integration/all-specs`, wait for CodeQL, merge only on a passing check with no new alerts.

### Phase 2 — actions (after the owner's allow-list sign-off)

- [x] 7.1 `ACTIONS_DELIVERED = True`; `DESTRUCTIVE_ALLOWLIST` from the sign-off; Actions_Switch in Settings (default on); the Phase-2 eval cases.
- [x] 7.2 Deploy gate, smoke check with a confirmed and a rejected action in a test Use_Case, push gate; `verification-notes.md` updated.

## Requirement coverage

| Requirement | Tasks |
|---|---|
| 1 Presence | 2.10, 4.2, 4.4, 4.5 |
| 2 Hide/show | 4.2, 4.3, 4.4, 4.5 |
| 3 Prompts and context | 2.1, 2.9, 2.10, 4.3 |
| 4 Guidance | 2.4, 2.5, 2.6, 2.7, 5.1, 5.2 |
| 5 Tasks | 1.1, 2.6, 2.9, 7.1 |
| 6 Identity, authorization, audit | 1.2–1.5, 2.9, 2.10, 4.4 |
| 7 Model | 2.6, 2.8, 2.9 |
| 8 Delivery | 2.9, 2.10, 4.2, 4.3 |
| 9 Persistence | 2.2, 2.10, 4.3 |
| 10 Safety | 2.1, 2.7, 2.9, 2.10, 3.2 |
| 11 Failures | 2.8, 2.9, 4.3 |
| 12 Admin disable | 2.3, 2.10, 4.2, 4.4 |
| 13 Operations and docs | 3.2–3.5, 5.1 |
| 14 Testing | 1.5, 2.11, 3.5, 4.5, 5.2, 6.2 |
| 15 Phasing | 6.x, 7.x |
