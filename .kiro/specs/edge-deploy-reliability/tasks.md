# Implementation Plan

## Overview

This plan fixes the four edge-deploy-reliability defects using the exploratory bugfix workflow:
surface each defect on UNFIXED code first (Properties 1–4: Bug Condition), capture existing behavior
that must not change (Properties 5–8: Preservation), apply the four fixes, then validate and confirm
no regressions. All exploration and preservation tests are written and run against the UNFIXED code
before any fix is applied. Defect A hardens the compose restart race (`stop_grace_period: 120s`,
`restart: always`, bounded 20s SIGTERM cleanup). Defect B health-gates the Greengrass lifecycle
(`/health` endpoint, docker healthchecks, Run→Startup `up -d --wait` across all four recipe
variants). Defect C makes `workflow_packaging.py` emit HARD ComponentDependencies on model
components and per-arch LocalServer variants. Defect D classifies never-reachable vLLM runtime
failures and emits an actionable message naming the dead backend container. A final on-hardware JP6
gate (task 5) verifies the fix end-to-end on the real device — it consumes a ~1h gdk build and
touches the live device, so it runs only with the user's explicit go-ahead.

**Defect E (tasks 6–10, added after on-hardware verification of A–D)**: a second verified incident
(v1.0.46, device ryan-orin-nano) showed a Shutdown/Startup teardown race the Defect B health gate
does not close — Startup's `compose up` adopted a still-dying backend container from the previous
incarnation, the `--wait` gate trusted its stale 'healthy' state, Startup exited 0, and the container
finished dying 3s later, leaving no backend at all behind a RUNNING component. The fix (design
Fix Implementation §5) adds a compose-lifecycle helper (`src/host_scripts/compose_lifecycle.sh`,
`wait-empty` + `verify-fresh`, testable with a stubbed docker), makes Shutdown synchronous (bounded
wait for zero project containers, explicit Shutdown Timeout), and makes Startup adoption-proof
(`--force-recreate` plus a StartedAt freshness gate). Same exploratory workflow: tasks 6/7 run
against the E-unfixed tree before task 8 implements; task 10 is the user-gated on-hardware gate
riding the next LocalServer build.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Write tests against UNFIXED code: task 1 (Bug Conditions for Defects A/B/C/D) FAILS; task 2 (Preservation) PASSES. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Apply the four fixes (3.1 compose race, 3.2 health-gated lifecycle + security baseline rebaseline, 3.3 workflow dependencies, 3.4 actionable diagnostics), then re-run task 1 (3.5) and task 2 (3.6). Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Checkpoint - run the relevant test suites and ensure all tests pass. Depends on wave 2."
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "On-hardware JP6 verification gate (build/publish + live-device tests). Requires user coordination — runs only with explicit go-ahead. Depends on wave 3."
    },
    {
      "wave": 5,
      "tasks": ["6", "7"],
      "description": "Defect E — write tests against the E-unfixed tree: task 6 (Bug Condition, Property 9) FAILS; task 7 (Preservation, Property 10) PASSES. Independent of each other. Depend on wave 4 (Defect E was diagnosed on the deployed A–D fix)."
    },
    {
      "wave": 6,
      "tasks": ["8"],
      "description": "Apply the Defect E fix (8.1 compose-lifecycle helper, 8.2 synchronous Shutdown + adoption-proof Startup across all four recipe variants), then re-run task 6 (8.3) and task 7 (8.4). Depends on wave 5."
    },
    {
      "wave": 7,
      "tasks": ["9"],
      "description": "Checkpoint - run the relevant test suites and ensure all tests pass. Depends on wave 6."
    },
    {
      "wave": 8,
      "tasks": ["10"],
      "description": "Defect E on-hardware verification gate on ryan-orin-nano (rides the next LocalServer build). Requires user coordination — runs only with explicit go-ahead. Depends on wave 7."
    },
    {
      "wave": 9,
      "tasks": ["11", "12"],
      "description": "Defect F — write tests against the F-unfixed tree: task 11 (Bug Condition, Property 11) FAILS; task 12 (Preservation, Property 12) PASSES. Independent of each other. Independent of wave 8 (task 10 is user-gated; Defect F is portal-side only)."
    },
    {
      "wave": 10,
      "tasks": ["13"],
      "description": "Apply the Defect F fix (13.1 single-variant-only LocalServer emission), then re-run task 11 (13.2) and task 12 (13.3). Depends on wave 9."
    },
    {
      "wave": 11,
      "tasks": ["14"],
      "description": "Checkpoint — run the relevant suites, then deploy the portal backend so the fixed packaging Lambda is live. Depends on wave 10."
    },
    {
      "wave": 12,
      "tasks": ["15", "16"],
      "description": "Defect G — write tests against the G-unfixed tree: task 15 (Bug Condition, Property 13) FAILS; task 16 (Preservation, Property 14) PASSES. Independent of each other. Depends on wave 11 (builds on the Defect F single-variant discipline)."
    },
    {
      "wave": 13,
      "tasks": ["17"],
      "description": "Apply the Defect G fix (17.1 vision published_components resolution + single-name model emission), then re-run task 15 (17.2) and task 16 (17.3). Depends on wave 12."
    },
    {
      "wave": 14,
      "tasks": ["18"],
      "description": "Checkpoint — suites green, deploy the portal packaging Lambda, user repackages the vision workflow. Depends on wave 13."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE any fix (tests written against unfixed code).
- Task 3 depends on wave 1; sub-tasks 3.5 and 3.6 depend on 3.1–3.4.
- Task 4 depends on task 3. Task 5 depends on task 4 and on the user's go-ahead (live JP6 device + ~1h gdk build).
- Tasks 6 and 7 (Defect E) are independent and must be completed BEFORE task 8 (tests written against the E-unfixed tree).
- Task 8 depends on wave 5; sub-tasks 8.3 and 8.4 depend on 8.1–8.2.
- Task 9 depends on task 8. Task 10 depends on task 9 and on the user's go-ahead (live JP6 device + ~1h gdk build).

## Tasks

- [x] 1. Write bug condition exploration tests (BEFORE implementing the fix)
  - **Property 1: Bug Condition** - Backend survives or recovers from deployment restarts (Defect A); **Property 2: Bug Condition** - Greengrass RUNNING implies healthy backend (Defect B); **Property 3: Bug Condition** - Generated workflow recipes carry model and LocalServer dependencies (Defect C); **Property 4: Bug Condition** - Never-reachable runtime failures are actionable (Defect D)
  - **CRITICAL**: These tests MUST FAIL on unfixed code — the failures confirm each defect exists
  - **DO NOT attempt to fix the tests or the code when they fail**
  - **NOTE**: These tests encode the expected behavior — they will validate the fix when they pass after implementation
  - **GOAL**: Surface counterexamples that demonstrate all four defects exist (confirming the evidence-backed causal chain from the incident)
  - **Scoped PBT Approach**: These are deterministic configuration/behavior defects — scope each property to the concrete failing artifact/case for reproducibility; compose and recipe defects use config tests (parse the YAML, assert the reliability-critical properties) as the testable seam
  - Exploration case 1 — compose config exposure (`isBugCondition_A`/`isBugCondition_B` structurally, design Bug Details): parse `src/docker-compose.yaml`; assert both backend services (`backend_tegra_gpu_enabled`, `backend_generic`) declare a `stop_grace_period` and a `healthcheck` — FAILS on the unfixed file (fields absent; `restart: unless-stopped` with default 10s grace is the incident configuration)
  - Exploration case 2 — recipe lifecycle exposure (`isBugCondition_B`, design Bug Details): parse all four recipe variants (`recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml`); assert each gates RUNNING on health — a `Startup` block running `docker compose up -d --wait` — FAILS on the unfixed recipes (attached `Run … up --no-build` keeps the component RUNNING while only the frontend serves)
  - Exploration case 3 — missing workflow dependencies (`isBugCondition_C`, design Bug Details): call the unfixed `build_recipe` in `edge-cv-portal/backend/functions/workflow_packaging.py` for a definition whose `llm_inference` node binds a model ref (e.g. `modelName: opt125m-smoke`), arch `arm64_jp6`; assert `model-vllm-*` and `aws.edgeml.dda.LocalServer.arm64JP6` appear in ComponentDependencies — FAILS on unfixed code (only `dda.plugin.*` entries, or none)
  - Exploration case 4 — generic diagnostics (`isBugCondition_D`, design Bug Details): run `prepare` in `src/backend/dda_triton/vllm_model_prep.py` with mocked `requests` raising `ConnectionError` on every attempt and `time.sleep` stubbed (backoff 3/6/12/24/48s must not really elapse); assert the terminal output names the LocalServer backend container (flask-app) as the likely cause — FAILS on unfixed code (literal generic "load request did not succeed; exiting non-zero so the component retries" message)
  - Exploration case 5 — unbounded shutdown handler (`isBugCondition_A` behaviorally): invoke the unfixed `shutdown_event` in `src/backend/app.py` with `terminate_digital_input_task` (via `cleanup_workflow_digital_inputs`) mocked to block ~30s; assert the handler returns within a 20-second cleanup budget — FAILS on unfixed code (handler runs inline past the budget, the shape that exceeds Docker's 10s grace window and gets SIGKILLed as exit 137)
  - Run all tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests FAIL (absent `stop_grace_period`/healthchecks; attached Run lifecycle in all four variants; ComponentDependencies without model/LocalServer entries; the literal generic prep message; an unbounded shutdown handler)
  - Document counterexamples found (e.g. "backend services declare no stop_grace_period, Docker SIGKILLs at 10s"; "recipe-arm64-jp6.yaml uses attached Run, RUNNING while backend Exited(137)"; "build_recipe('wf-123', …) emits only dda.plugin.* deps"; "all-connection-refused ends in the generic retry-exhausted message")
  - Mark task complete when tests are written, run, and failures are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9_

- [x] 2. Write preservation property tests (BEFORE implementing the fix)
  - **Property 5: Preservation** - Clean shutdowns and crash recovery unchanged; **Property 6: Preservation** - Compose and recipe structure unchanged beyond the intended edits; **Property 7: Preservation** - Existing packaging output unchanged apart from added dependencies; **Property 8: Preservation** - Prep script's specific error paths unchanged
  - **IMPORTANT**: Follow observation-first methodology — observe behavior on UNFIXED code, record it (golden behavior), then encode it as tests that must keep passing after the fix
  - Observe on UNFIXED code: `build_recipe` output across generated workflow definitions and arches — every field, and the exact `dda.plugin.*` ComponentDependencies entries (names, pinned VersionRequirements, HARD type) `plugin_component_dependencies` emits today, including with Custom_Node_Type plugins present
  - Observe on UNFIXED code: `vllm_model_prep.py` messages and exit codes for repository validation defects, unresolvable weights paths, and authoritative HTTP 4xx/5xx responses (mocked `requests`), plus the HTTP-200 success path
  - Observe on UNFIXED code: a fast `shutdown_event` executes `cleanup_workflow_digital_inputs()` then `disconnect_all_cameras()` in that order and returns promptly
  - Observe on UNFIXED code: the full parsed structure of `src/docker-compose.yaml` (services, profiles, images, build args, volumes, environment, ports) and of each recipe variant (Install, Shutdown, dependencies, configuration, artifacts)
  - Write property-based tests (Hypothesis, already used in this repo) capturing these patterns from the design Preservation Requirements:
    - Recipe equality modulo ComponentDependencies: for any generated workflow definition (random model-ref sets, plugin sets, arch subsets), the fixed `build_recipe` output equals the unfixed output in every field except ComponentDependencies, and all original `dda.plugin.*` entries survive byte-identical as a subset (Property 7; Requirements 3.3, 3.8)
    - Prep-script specific error paths: validation defects, bad weights paths, and HTTP-error responses (refused-then-HTTP-409 included — NOT the bug condition) reproduce the exact unfixed messages and exit codes; HTTP 200 behaves identically (Property 8; Requirements 3.2, 3.9)
    - Fast-shutdown equivalence: for any cleanup duration within the budget, the fixed handler executes the same two cleanup calls in the same order as the original (Property 5; Requirements 3.1, 3.6)
    - Compose deep-equality modulo added keys: parse original and fixed `src/docker-compose.yaml`; deep-equal after deleting only `stop_grace_period`, `restart`, and `healthcheck` keys — profiles/arch selection and the shared-file contract for JP5/x86 variants intact (Property 6; Requirements 3.4, 3.5, 3.7)
  - **Testing Approach**: Property-based testing is recommended — the preservation guarantees are universal ("for all non-bug inputs"); Hypothesis generates many cases automatically and catches edge cases manual tests miss
  - Run tests on UNFIXED code (equality tests trivially pass pre-fix by comparing the unfixed artifacts to the recorded goldens)
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

- [x] 3. Fix the four edge-deploy-reliability defects

  - [x] 3.1 Defect A — compose restart race (root cause)
    - In `src/docker-compose.yaml`, add `stop_grace_period: 120s` to both backend services (`backend_tegra_gpu_enabled`, `backend_generic`) — sized 5–6x above the bounded 20s cleanup; `docker compose stop`/`down` inherits it as the default timeout, so recipe Shutdown scripts need no change
    - In `src/docker-compose.yaml`, change `restart: unless-stopped` → `restart: always` on both backend services (frontend keeps `unless-stopped` — it was never part of the failure mode); update the existing restart-policy comment to document the new rationale, including the honest caveat that no restart policy re-launches a docker-stopped container while the daemon runs (that recovery path is owned by the Startup `--wait` retry in 3.2, which is why both ship together)
    - In `src/backend/app.py` `shutdown_event`, wrap the existing cleanup body (`cleanup_workflow_digital_inputs()` then `disconnect_all_cameras()`, same order) in a single `asyncio.wait_for(... run_in_executor ...)` with `SHUTDOWN_CLEANUP_BUDGET_SECONDS = 20` (strictly below the 120s grace period); on `TimeoutError`, log the abandoned-cleanup warning and proceed with shutdown (abandoned work is non-essential — the container is being torn down and `setup_workflow_digital_inputs()` reconstructs state on next start)
    - _Bug_Condition: isBugCondition_A(input) — backendShutdownDuration > stopGracePeriod (10s default) AND backend SIGKILLed (exit 137, OOMKilled=false) AND restartPolicy = "unless-stopped" AND backend Exited after the racing compose up (from design)_
    - _Expected_Behavior: Property 1 — grace period ≥ 120s and restart: always on both backend services; SIGTERM handler completes within the 20s budget strictly below the grace period; backend never SIGKILLed mid-cleanup and a killed backend never remains Exited behind a completed lifecycle cycle_
    - _Preservation: Property 5 — fast cleanup executes the same actions in the same order; restart: always is a strict superset of unless-stopped for crash exits (AWS CRT SIGABRT protection kept); Property 6 — compose unchanged beyond the added keys_
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.6_

  - [x] 3.2 Defect B — health-gated Greengrass lifecycle
    - Add an unauthenticated `GET /health` endpoint (`src/backend/endpoints/health.py`, registered like `local_auth`'s unauthenticated router, exempt from `authorize_request`): 200 when the app is serving AND — only if a vLLM runtime server was actually started in-process (`health.set_vllm_server(...)` called with the non-None `start_vllm_runtime()` result from the `__main__` startup sequence) — 127.0.0.1:8901 accepts a short-timeout TCP connect; 503 otherwise. A contained vLLM startup failure (returns None) does NOT flip the backend unhealthy; the probe is never a model invocation
    - Add the healthcheck helper `src/backend/healthcheck.py`, shipped in the flask-app image (Dockerfile COPY): probes `http://127.0.0.1:5000/health`, falling back to `https://127.0.0.1:5443/health` with cert verification disabled (backend serves 5443/TLS when station authorization is enabled, 5000 otherwise); exit 0 iff either returns 200 — Python because the image is not guaranteed to carry curl/wget; loopback works under `network_mode: host`
    - In `src/docker-compose.yaml`, add the backend healthcheck to both backend services: `test: ["CMD", "python3", "/healthcheck.py"]` (path per Dockerfile COPY), `interval: 15s`, `timeout: 10s`, `retries: 4`, `start_period: 300s` (DB migration + triton setup + vLLM runtime start on JP6)
    - In `src/docker-compose.yaml`, add a basic frontend healthcheck probing the nginx-served app on container port 80 (`curl -fsS http://127.0.0.1:80/`, `interval: 30s`, `timeout: 5s`, `retries: 3`, `start_period: 30s`); verify the react-webapp image carries curl (add it in `src/frontend/Dockerfile` or fall back to `wget -q -O /dev/null` / a node one-liner)
    - In all four recipe variants (`recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml`), replace the `Run` block with a `Startup` block containing the identical script (same `SetEnv`, host setup scripts, `/tmp/.dda.env` export) except the final line becomes `docker compose --profile $DOCKER_PROFILE -f .../docker-compose.yaml up -d --no-build --wait --wait-timeout 600`, with `Timeout: 900` on the Startup block (Greengrass default Startup timeout of 120s is far below a cold JP6 boot); `Shutdown` blocks unchanged (`docker compose down`, plus `systemctl stop nvidia-csi-capture` on arm variants) — a retried Startup's `compose up` starts existing stopped containers, closing the 2.7 recovery path
    - **Security preservation gate (builds.md) — rebaseline in the same change**: `src/docker-compose.yaml` is a preservation-tracked file; recompute its sha256 (`sha256sum src/docker-compose.yaml`, covering the 3.1 edits too) and update `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`; if the flask-app Dockerfile changed to COPY `healthcheck.py` (or the frontend Dockerfile changed for curl), rebaseline those Dockerfile entries/content baselines (`docker_baseline_backend_Dockerfile.jp5_masked.txt` / `...jp6_masked.txt`) as well; re-run the preservation suite in the flask-app container per the documented procedure — otherwise the component build gate WILL fail
    - _Bug_Condition: isBugCondition_B(state) — backend DEAD, frontend RUNNING, Greengrass reports LocalServer RUNNING (attached compose up alive), HARD deps satisfied by RUNNING alone, no compose healthchecks (from design)_
    - _Expected_Behavior: Property 2 — every recipe variant's Startup runs `up -d --wait` (exit 0 only when all started services pass healthchecks); backend healthcheck probes /health including conditional vLLM 8901 reachability; frontend has a basic healthcheck; RUNNING implies healthy backend_
    - _Preservation: Property 6 — compose identical beyond stop_grace_period/restart/healthcheck; recipes identical beyond Run→Startup (Install, Shutdown, dependencies, configuration, artifacts unchanged); profile/arch selection and shared compose across JP5/x86 variants intact; gdk pipeline unchanged beyond intended edits_
    - _Requirements: 2.4, 2.5, 2.6, 2.7, 3.4, 3.5, 3.7_

  - [x] 3.3 Defect C — workflow component dependencies
    - In `edge-cv-portal/backend/functions/workflow_packaging.py`, add `gather_model_references(definition, descriptors_by_id)`: collect effective values of every `model_ref`-typed parameter (`PARAM_TYPE_MODEL_REF` — today `model_inference.modelName` and `llm_inference.modelName`), deduplicated, stable order; generic over the parameter type, not node-type allowlists
    - Add `resolve_model_components(model_names, usecase)`: resolve each name against the Use_Case model registry the same way `workflow_validation.py` does (training-jobs table via `usecase-training-index`, keyed by `model_name`) and extract `published_component.component_name`; **fail closed** — a record with no published component raises the existing `PackagingError` path naming the model (all-or-nothing, mirroring the plugin gates)
    - Add `model_component_dependencies(resolved)`: one entry per distinct component, `{'VersionRequirement': '>=0.0.0', 'DependencyType': 'HARD'}` — deliberately unpinned (model components version independently; the deployment pins the concrete version; the dependency's job is the ordering/health edge)
    - Add `local_server_component_dependencies(archs)`: per-arch mapping `arm64_jp4 → aws.edgeml.dda.LocalServer.arm64JP4`, `arm64_jp5 → …arm64JP5`, `arm64_jp6 → …arm64JP6`, `x86_64`/`x86_64_nvidia` → `…amd64` (fail-closed naming discipline as `greengrass_publish.TARGET_TO_LOCAL_SERVER`; the retired bare `.arm64` name is never emitted), with `{'VersionRequirement': '>=' + min_local_server_version_for(arch), 'DependencyType': 'HARD'}` reusing the existing per-arch `minLocalServerVersion` floors; one entry per distinct variant (`x86_64`+`x86_64_nvidia` collapse to one amd64 entry)
    - Merge in the packaging handler: `{**plugin_component_dependencies(dep_records), **model_component_dependencies(...), **local_server_component_dependencies(architectures)}` — the three namespaces are disjoint so the merge cannot collide; `build_recipe` itself unchanged (already attaches non-empty component_dependencies); plugin entries pass through byte-identical; document the recipe-global ComponentDependencies multi-variant caveat in the function docstring per the design
    - _Bug_Condition: isBugCondition_C(recipe) — no modelComponent(m) and no localServerComponent(a) in ComponentDependencies for any used model m or target arch a; only dda.plugin.* entries ever present (from design)_
    - _Expected_Behavior: Property 3 — for any workflow with model refs M and non-empty arch set A, ComponentDependencies contains a HARD entry per distinct published model component of M and a LocalServer entry per distinct arch of A with the per-arch minimum-version floor_
    - _Preservation: Property 7 — build_recipe output equal to original in every field except ComponentDependencies; original dda.plugin.* entries unchanged (names, pinned versions, HARD) as a subset; deployed workflow components untouched_
    - _Requirements: 2.8, 2.9, 3.3, 3.8_

  - [x] 3.4 Defect D — actionable never-reachable diagnostics
    - In `src/backend/dda_triton/vllm_model_prep.py`, change `request_load` to return a classification instead of a bare bool: `LOAD_OK` (HTTP 200), `LOAD_HTTP_ERROR` (an authoritative non-200 HTTP response was received — message and single-attempt semantics unchanged), `LOAD_UNREACHABLE` (every attempt ended in `wait_for_server` failure or a connection-level `requests.RequestException` with no HTTP response ever received); tracking is one `got_http_response` boolean plus the existing loop; per-attempt log lines unchanged
    - In `prepare`, emit the actionable terminal message for `LOAD_UNREACHABLE` only (exit code stays 1): the message from the design naming the LocalServer backend container (image 'flask-app') as the likely cause, with concrete verification steps (`sudo docker ps -a --filter ancestor=flask-app` looking for Exited, `sudo docker logs <container-id>`, and the LocalServer component log `/greengrass/v2/logs/aws.edgeml.dda.LocalServer.*.log`)
    - Leave untouched: `validate_repository` defects, the weights-path FAILED message, the HTTP-error logging in `request_load`, `request_unload`/`cleanup`, and the success path — exact current messages and exit codes
    - _Bug_Condition: isBugCondition_D(attempts) — every attempt outcome in {SERVER_NOT_REACHABLE, CONNECTION_ERROR}, no HTTP response ever received, generic retry-exhausted message emitted (from design)_
    - _Expected_Behavior: Property 4 — exit non-zero with an error naming the LocalServer backend container (flask-app) as the likely cause and including concrete verification steps_
    - _Preservation: Property 8 — identical messages and exit codes for validation defects, weights-path failures, and authoritative HTTP errors; HTTP 200 path identical_
    - _Requirements: 2.10, 3.9_

  - [x] 3.5 Verify the bug condition exploration tests now pass
    - **Property 1: Expected Behavior** - Backend survives or recovers from deployment restarts; **Property 2: Expected Behavior** - Greengrass RUNNING implies healthy backend; **Property 3: Expected Behavior** - Generated workflow recipes carry model and LocalServer dependencies; **Property 4: Expected Behavior** - Never-reachable runtime failures are actionable
    - **IMPORTANT**: Re-run the SAME tests from task 1 — do NOT write new tests
    - The tests from task 1 encode the expected behavior; when they pass they confirm each defect is fixed
    - Run all five exploration cases from task 1
    - **EXPECTED OUTCOME**: Tests PASS (backend services declare stop_grace_period ≥ 120s and healthchecks; all four recipe variants use Startup `up -d --wait` with Timeout; build_recipe emits the model and LocalServer HARD entries; the prep script names the flask-app backend container; the shutdown handler returns within the 20s budget)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10_

  - [x] 3.6 Verify preservation tests still pass
    - **Property 5: Preservation** - Clean shutdowns and crash recovery unchanged; **Property 6: Preservation** - Compose and recipe structure unchanged beyond the intended edits; **Property 7: Preservation** - Existing packaging output unchanged apart from added dependencies; **Property 8: Preservation** - Prep script's specific error paths unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - Run the preservation property tests from task 2
    - **EXPECTED OUTCOME**: Tests PASS (no regressions: recipe equality modulo ComponentDependencies with dda.plugin.* passthrough intact; prep-script specific error paths byte-identical; fast-shutdown equivalence; compose deep-equality modulo the three added keys)
    - Confirm all tests still pass after the fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the relevant test suites: the backend test suite covering the compose/recipe config tests, prep-script tests, and shutdown-handler tests (`test/backend-test`, including the security preservation suite `test/backend-test/security/preservation` to confirm the rebaselined hashes pass), and the portal packaging tests (`edge-cv-portal/backend/tests`) including the Hypothesis property tests; ensure all tests pass, ask the user if questions arise

- [x] 5. On-hardware JP6 verification gate (REQUIRES USER COORDINATION — do not start without explicit go-ahead)
  - **NOTE**: This task consumes a ~1h gdk component build and touches the live JP6 device; run it only when the user says go. Per builds.md: never run two component builds at once (check `pgrep -af "gdk component build"` / `pgrep -af "build-custom.sh"` first), build sequentially with the target name swapped in `gdk-config.json`, and capture output to `.gdk_build_jp6.log`
  - Build and publish the modified `aws.edgeml.dda.LocalServer.arm64JP6` component (gdk) plus the portal packaging changes; deploy to the JP6 device
  - **Restart-under-load test** (Properties 1, 2): while a workflow with an `llm_inference` node is running, trigger a deployment restart of LocalServer and verify the backend is never SIGKILLed (no exit 137) — or if killed, is recovered by the Startup retry; Greengrass reports LocalServer RUNNING only after `docker compose ps` shows the backend healthy; `model-vllm-opt125m-smoke` never goes BROKEN; the deployment completes without `FAILED_UNABLE_TO_ROLLBACK`
  - **Dead-backend truthfulness test** (Property 2): on the device, `docker stop` the backend and verify the component's next lifecycle cycle fails Startup (not silent RUNNING) and the retried Startup brings the stopped container back up
  - **Workflow dependency ordering test** (Property 3): deploy a freshly packaged workflow component and verify in the Greengrass logs that it is ordered after its model component and LocalServer (HARD edges visible in dependency resolution)
  - Per builds.md, the change is not "done" until verified on device from a real built+deployed component; state in the commit/PR what was verified on which device
  - _Requirements: 2.1, 2.2, 2.4, 2.7, 2.8, 2.9, 3.1, 3.2, 3.3_

- [x] 6. Write Defect E bug condition exploration tests (BEFORE implementing the fix)
  - **Property 9: Bug Condition** - Startup never trusts a previous incarnation's container (Defect E)
  - **CRITICAL**: These tests MUST FAIL on the E-unfixed tree — the failures confirm the defect exists
  - **DO NOT attempt to fix the tests or the code when they fail**
  - **NOTE**: These tests encode the expected behavior — they will validate the fix when they pass after implementation
  - **GOAL**: Surface counterexamples demonstrating the structural gaps behind the verified incident (v1.0.46, ryan-orin-nano: Startup adopted the ~24s-dying backend, `--wait` trusted its stale 'healthy' state, Startup exited 0, container destroyed 3s later — no backend behind a RUNNING component)
  - **Scoped PBT Approach**: deterministic configuration/behavior defect — config tests parse the recipes as the testable seam (existing `test/backend-test/deploy_reliability/` pattern); helper behavior tests use a stubbed `docker` executable on PATH simulating the incident's dying window
  - Exploration case 6 — teardown-race lifecycle exposure (`isBugCondition_E` structurally, design Bug Details): parse all four recipe variants (`recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml`); assert each Shutdown block declares a `Timeout` (≥ the wait bound; unfixed recipes declare none, so Greengrass's 15s default truncates any wait) and invokes `compose_lifecycle.sh wait-empty` after `down`, and each Startup records `STARTUP_EPOCH`, uses `--force-recreate` on the compose up line, and invokes `compose_lifecycle.sh verify-fresh` after the `--wait` gate — FAILS on unfixed recipes (bare `down`, adoption-permitting `up`, no freshness gate)
  - Exploration case 7 — missing helper exposure (`isBugCondition_E` behaviorally): assert `src/host_scripts/compose_lifecycle.sh` exists and, with a stubbed `docker` simulating the dying window (`compose ps -aq` reporting the backend container for several polls before emptying; `inspect` reporting a StartedAt older than the reference epoch), that `wait-empty` blocks until empty then exits 0 and `verify-fresh` exits non-zero for the stale container — FAILS on unfixed tree (the helper does not exist)
  - Place tests in `test/backend-test/deploy_reliability/` following the existing exploration test patterns (e.g. `test_recipe_lifecycle_exploration.py`)
  - Run all tests on the E-unfixed tree
  - **EXPECTED OUTCOME**: Tests FAIL (no Shutdown Timeout or post-down wait in any variant; no `--force-recreate` or freshness verification in any Startup; no compose-lifecycle helper in host_scripts)
  - Document counterexamples found (e.g. "recipe-arm64-jp6.yaml Shutdown has no Timeout and exits after a bare `down` — Startup can race a dying container"; "Startup compose line permits adoption; `--wait` trusts stale healthcheck state")
  - Mark task complete when tests are written, run, and failures are documented
  - _Requirements: 1.10, 1.11, 1.12, 1.13_

- [x] 7. Write Defect E preservation property tests (BEFORE implementing the fix)
  - **Property 10: Preservation** - Cold-start and health-gate semantics unchanged (Defect E)
  - **IMPORTANT**: Follow observation-first methodology — observe behavior on the E-unfixed tree, record it (golden behavior), then encode it as tests that must keep passing after the fix
  - Observe on the E-unfixed tree: the full parsed structure of all four recipe variants in their post-Defect-B form (Install, SetEnv, host setup script invocations, the Startup `up -d --no-build --wait --wait-timeout 600` health gate with `Timeout: 900`, the Shutdown `down` + `/tmp/.dda.env` export + `systemctl stop nvidia-csi-capture` on arm variants) — record fresh goldens alongside the existing `goldens/recipe_*_structure.golden.json`
  - Observe on the E-unfixed tree: `src/docker-compose.yaml` is untouched by Defect E (byte-identical; the Defect A/B keys unchanged) — no security-baseline rebaseline is needed for this defect
  - Write property-based tests (Hypothesis, existing repo pattern) capturing these patterns from the design Preservation Requirements:
    - Recipe equality modulo the Defect E edits: the fixed recipes are deep-equal to the goldens after removing only the Shutdown `Timeout` key, the `wait-empty` invocation line, the `STARTUP_EPOCH` line, the `--force-recreate` flag, and the `verify-fresh` line; the `--wait --wait-timeout 600` health gate, `down` command, and all other structure byte-identical; all four variants receive identical edits (Requirements 3.11, 3.12)
    - Cold-start no-op guarantee: with a stubbed `docker` reporting zero project containers, `wait-empty` returns 0 within one poll interval and `verify-fresh` returns 0 with nothing to check (Requirements 3.10, 3.13) — written now, asserting the helper contract; trivially skipped-as-absent or vacuous pre-fix, binding once 8.1 lands
    - Compose byte-identity: `src/docker-compose.yaml` hash equals the recorded golden before and after the Defect E fix (Requirement 3.10)
  - **Testing Approach**: property-based testing for the equality-modulo-edits and timestamp-domain checks; the preservation guarantees are universal ("for all cycles without a previous-incarnation container")
  - Run tests on the E-unfixed tree
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on the E-unfixed tree
  - _Requirements: 3.10, 3.11, 3.12, 3.13_

- [x] 8. Fix Defect E — synchronous teardown + adoption-proof Startup

  - [x] 8.1 Implement the compose-lifecycle helper
    - Create `src/host_scripts/compose_lifecycle.sh` (shipped into every component artifact by the existing `build-custom.sh` `cp -r src/host_scripts` step; host_scripts are not security-baseline-tracked; `gdk-config.json` and root `recipe.yaml` are build artifacts — never touched)
    - `wait-empty <timeout-seconds> -- <docker compose args...>`: poll `docker compose <args> ps -aq` every 2s until empty; exit 0 when empty (immediately if already empty — fast-teardown/cold-start case), exit 1 with surviving container IDs in the diagnostic at the bound; `docker` resolved via PATH (stub-testable)
    - `verify-fresh <since-epoch> -- <docker compose args...>`: for each `docker compose <args> ps -q` container, parse `docker inspect -f '{{.State.StartedAt}}'` to epoch and require ≥ `<since-epoch>`; exit 0 iff all fresh (0 containers → 0), exit 1 naming any stale container; malformed inspect output → non-zero (fail closed)
    - _Bug_Condition: isBugCondition_E(cycle) — Shutdown exits with containers remaining; Startup adopts the dying container; health gate trusts stale 'healthy'; destroy lands after RUNNING (from design)_
    - _Expected_Behavior: Property 9 — wait-empty never exits 0 with containers remaining; verify-fresh rejects any container whose StartedAt predates the reference epoch_
    - _Preservation: Property 10 — both subcommands are immediate no-ops with zero project containers_
    - _Requirements: 2.11, 2.13, 2.14, 3.10, 3.13_

  - [x] 8.2 Wire the synchronous Shutdown and adoption-proof Startup into all four recipe variants
    - In `recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml` (identical edits — variants stay in sync):
    - Shutdown: add `Timeout: 300` (Greengrass's 15s default truncates any teardown wait; 300s covers the 120s stop grace + observed ~24s post-kill dying window with margin); after the `docker compose ... down` line, add `bash .../host_scripts/compose_lifecycle.sh wait-empty 240 -- --profile ${DOCKER_PROFILE:-tegra} -f .../docker-compose.yaml || true` — best-effort: a timed-out wait must not wedge the lifecycle; Startup is the authoritative gate (use each variant's artifact path and profile default per the existing Shutdown lines)
    - Startup: record `STARTUP_EPOCH=$(date +%s)` as the first script line; add `--force-recreate` to the compose up line (`up -d --no-build --force-recreate --wait --wait-timeout 600`) — recreation never adopts; a dying container's removal blocks until teardown completes or errors compose up non-zero (Greengrass retries — no silent adoption on any path); after the up line, add `bash .../host_scripts/compose_lifecycle.sh verify-fresh $STARTUP_EPOCH -- --profile $DOCKER_PROFILE -f .../docker-compose.yaml` NOT best-effort — non-zero fails Startup so Greengrass retries rather than reporting RUNNING over a container this Startup did not create
    - Leave untouched: `src/docker-compose.yaml` (no rebaseline needed), Install blocks, SetEnv, host setup script invocations, the existing `Timeout: 900` on Startup, `/tmp/.dda.env` exports, and the `systemctl stop nvidia-csi-capture` lines
    - _Bug_Condition: isBugCondition_E(cycle) from design_
    - _Expected_Behavior: Property 9 — Shutdown waits (bounded, with adequate Timeout) for zero project containers; Startup force-recreates and gates RUNNING on container freshness; unclearable containers fail the lifecycle non-zero_
    - _Preservation: Property 10 — cold start identical to the Defect B lifecycle (--force-recreate a no-op with nothing to recreate, helpers immediate); health-gate semantics unchanged; all four variants identical modulo per-variant paths; compose file byte-identical_
    - _Requirements: 2.11, 2.12, 2.13, 2.14, 3.10, 3.11, 3.12, 3.13_

  - [x] 8.3 Verify the Defect E bug condition exploration tests now pass
    - **Property 9: Expected Behavior** - Startup never trusts a previous incarnation's container
    - **IMPORTANT**: Re-run the SAME tests from task 6 — do NOT write new tests
    - The tests from task 6 encode the expected behavior; when they pass they confirm the defect is fixed
    - Run exploration cases 6 and 7 from task 6
    - **EXPECTED OUTCOME**: Tests PASS (every variant's Shutdown declares Timeout: 300 and invokes wait-empty after down; every Startup records STARTUP_EPOCH, uses --force-recreate, and gates on verify-fresh; the helper waits through a stubbed dying window and rejects stale containers)
    - _Requirements: 2.11, 2.12, 2.13, 2.14_

  - [x] 8.4 Verify the Defect E preservation tests still pass
    - **Property 10: Preservation** - Cold-start and health-gate semantics unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 7 — do NOT write new tests
    - Run the preservation property tests from task 7
    - **EXPECTED OUTCOME**: Tests PASS (recipes equal to goldens modulo the five intended edits, applied identically across variants; compose file byte-identical; helpers immediate no-ops with zero containers)
    - Confirm all tests still pass after the fix (no regressions)
    - _Requirements: 3.10, 3.11, 3.12, 3.13_

- [x] 9. Checkpoint - Ensure all tests pass
  - Run the relevant test suites: `test/backend-test/deploy_reliability/` in full (tasks 1/2 tests must still pass alongside the new task 6/7 tests — the Defect E recipe edits must not break the Defect B config/preservation tests, so update the deploy_reliability goldens per their documented regeneration procedure if the intended E edits are flagged), plus the security preservation suite (`test/backend-test/security/preservation`) to confirm no baseline drift (compose untouched, host_scripts untracked); ignore the known pre-existing failures listed in the repo steering (IAM CDK-synth statement-count, cdk.out drift guard, portal workflow test-runner, `test_property_setup_command_wellformed` collection order); ask the user if questions arise

- [x] 10. Defect E on-hardware verification gate (REQUIRES USER COORDINATION — do not start without explicit go-ahead)
  - **VERIFIED 2026-08-04 on ryan-orin-nano (LocalServer arm64JP6 v1.0.49), component-restart teardown-race path**: issued `greengrass-cli component restart` mid-teardown; `docker events` proved the fix — `die`->`destroy`(t=157)->`create`(t=158)->`start`(t=159)->healthy, i.e. the old backend container is fully destroyed BEFORE the new one is created (no adoption of a dying container). Both containers healthy afterward; `/health` = 200. The reboot + nucleus-restart variant was not exercised (requires disruptive device reboot); the restart path — the primary incident trigger — is confirmed fixed on-device.
  - **NOTE**: The fix rides the next LocalServer build (~1h gdk build) and touches the live JP6 device (ryan-orin-nano); run only when the user says go. Per builds.md: never run two component builds at once (check `pgrep -af "gdk component build"` / `pgrep -af "build-custom.sh"` first), build sequentially with the target name swapped in `gdk-config.json`, capture output to `.gdk_build_jp6.log`
  - Build and publish the modified `aws.edgeml.dda.LocalServer.arm64JP6` component (gdk); deploy to ryan-orin-nano
  - **Teardown-race reproduction test** (Property 9): reproduce the incident trigger — device reboot + nucleus restart, and separately a `greengrass-cli component restart` issued while the backend is mid-teardown; verify via `docker events` that the backend is recreated by the new Startup (create/start events, never adoption of a prior container); `docker ps -a` shows both containers once RUNNING is reported; the portal/API answers HTTP 200
  - **Synchronous Shutdown test** (Property 9): during a component restart, verify Shutdown does not exit until `docker compose ps -aq` is empty (or its bounded 240s wait elapses), and Greengrass no longer truncates the wait at 15s
  - **Freshness gate test** (Property 9): with a manually stalled teardown (or a pre-seeded stale container), verify Startup fails non-zero (verify-fresh or force-recreate removal error) and the Greengrass retry recovers — never silent RUNNING over a stale/absent backend
  - **Cold-start preservation test** (Property 10): a normal deployment cycle from clean state completes with the same health-gated behavior and no material Shutdown/Startup slowdown
  - Per builds.md, the change is not "done" until verified on device from a real built+deployed component; state in the commit/PR what was verified on which device
  - _Requirements: 2.11, 2.12, 2.13, 2.14, 3.10, 3.11, 3.13_

- [x] 11. Write Defect F bug condition exploration test (BEFORE implementing the fix)
  - **Property 11: Bug Condition** - Packaged workflow components are deployable on every targeted device
  - **CRITICAL**: This test MUST FAIL on the F-unfixed tree — the failure confirms the defect exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior — it will validate the fix when it passes after implementation
  - **GOAL**: Surface the counterexample from the verified incident (dda.workflow.f81a4c66 v1.0.0 packaged for arm64_jp5+arm64_jp6 → HARD deps on both LocalServer.arm64JP5 and .arm64JP6 → deployment 44f2c596 to ryan-orin-nano FAILED_ROLLBACK_COMPLETE)
  - **Scoped PBT Approach**: deterministic pure-function defect — call `local_server_component_dependencies` directly; a Hypothesis strategy over arch subsets of {arm64_jp4, arm64_jp5, arm64_jp6, x86_64, x86_64_nvidia} asserting the deployability invariant (at most one distinct LocalServer variant among emitted entries) is the natural property
  - Add the test alongside the existing Defect C packaging tests (follow the existing test location/pattern for `workflow_packaging.py` dependency tests — `test/backend-test/deploy_reliability/` or `edge-cv-portal/backend/tests/`, wherever the task 1 case-3 exploration test lives)
  - Exploration case 8 — multi-variant emission (`isBugCondition_F`, design Bug Details): call the unfixed `local_server_component_dependencies(['arm64_jp5', 'arm64_jp6'])`; assert at most one distinct LocalServer variant is emitted — FAILS on unfixed code (two HARD entries: arm64JP5 and arm64JP6); property case: for any arch subset mapping to >1 distinct variant, assert zero LocalServer entries — FAILS on unfixed code
  - Run the test on the F-UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct — it reproduces the incident's recipe shape)
  - Document the counterexample found
  - Mark task complete when the test is written, run, and the failure is documented
  - _Requirements: 1.14, 1.15, 1.16_

- [x] 12. Write Defect F preservation property tests (BEFORE implementing the fix)
  - **Property 12: Preservation** - Single-variant packaging output unchanged
  - **IMPORTANT**: Follow observation-first methodology — observe the F-unfixed behavior first, then encode it
  - Observe on F-unfixed code: `local_server_component_dependencies` output for every single-arch input and for the `x86_64`+`x86_64_nvidia` → single `amd64` collapse (exact names, version floors, HARD type); `build_recipe` output fields; model and `dda.plugin.*` entries pass through the merge unchanged
  - Write property-based tests (Hypothesis, per repo convention): for any arch set collapsing to ONE distinct LocalServer variant, the emitted LocalServer entry equals the unfixed output exactly; for any arch set and model/plugin inputs, the model and plugin entries are unchanged; `build_recipe` output equal in every non-ComponentDependencies field
  - Run the tests on F-UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.14, 3.15, 3.16_

- [x] 13. Fix Defect F — single-variant-only LocalServer dependency emission

  - [x] 13.1 Implement the fix in `edge-cv-portal/backend/functions/workflow_packaging.py`
    - Change `local_server_component_dependencies(archs)` per design Fix Implementation §7: map archs to distinct variants exactly as today (fail-closed naming, amd64 collapse); |variants| == 1 → return the single entry byte-identical to today (per-arch minimum-version floor; max of floors if several archs collapse to one variant); |variants| > 1 → return `{}` and log a warning naming the omitted variants
    - Update the function docstring: replace the multi-variant caveat with the single-variant-only emission rule and the deployability rationale
    - Leave untouched: `gather_model_references`, `resolve_model_components`, `model_component_dependencies`, `plugin_component_dependencies`, the merge site, and `build_recipe`
    - _Bug_Condition: isBugCondition_F(package) — selected archs map to >1 distinct LocalServer variant, from design_
    - _Expected_Behavior: Property 11 — exactly one LocalServer entry when |V| = 1, zero when |V| > 1, logged omission, from design_
    - _Preservation: Property 12 — single-variant output byte-identical; model/plugin entries untouched in all cases_
    - _Requirements: 2.15, 2.16, 2.17, 3.14, 3.15, 3.16_

  - [x] 13.2 Verify the Defect F bug condition exploration test now passes
    - **IMPORTANT**: Re-run the SAME test from task 11 — do NOT write a new test
    - **EXPECTED OUTCOME**: Test PASSES (confirms the defect is fixed)
    - _Requirements: 2.15, 2.16, 2.17_

  - [x] 13.3 Verify the Defect F preservation tests still pass
    - **IMPORTANT**: Re-run the SAME tests from task 12 — do NOT write new tests
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - _Requirements: 3.14, 3.15, 3.16_

- [x] 14. Checkpoint — Ensure all tests pass, then deploy the fixed packaging Lambda
  - Run the relevant suites: the portal packaging tests (`edge-cv-portal/backend/tests`, including the Defect C Hypothesis tests and the vllm-model-name-mismatch packaging tests — same file, must not regress) and `test/backend-test/deploy_reliability/`; ignore the known pre-existing failures listed in the repo steering
  - Deploy the portal backend (infrastructure deploy so the WorkflowPackagingHandler Lambda picks up the fix), following the same deploy procedure used for the vllm-model-name-mismatch fix; verify the deployed Lambda contains the fix
  - After deploy, the user repackages the affected workflow (f81a4c66) — for both architectures or JP6-only, either now yields a deployable component — and revises deployment 44f2c596 on ryan-orin-nano
  - Ask the user if questions arise

- [x] 15. Write Defect G bug condition exploration test (BEFORE implementing the fix)
  - **Property 13: Bug Condition** - Published vision models resolve and package deployably
  - **CRITICAL**: This test MUST FAIL on the G-unfixed tree. **DO NOT fix the code or the test when it fails**
  - **GOAL**: reproduce the verified incident — a vision record with per-target `published_components` (plural, status published, targets covering the selected archs, e.g. the yolo_test shape: jetson-xavier-jp5 + jetson-xavier-jp6 at 6.0.0) and NO singular `published_component`, rejected by `resolve_model_components` with "no published Greengrass component"
  - Place alongside the Defect F tests (edge-cv-portal/backend/tests/, moto/aws_stack harness — seed the training-jobs table with the plural-shape record, follow the existing packaging-test seeding patterns)
  - Exploration case 9: `resolve_model_components(['yolo_test'], usecase, archs=['arm64_jp5','arm64_jp6'])` resolves without PackagingError; the model dependency emission for the resolved record emits ZERO entries when the per-target names differ (deployability, Defect F discipline) and exactly one when they collapse; an arch with no published entry raises naming the model AND the arch
  - Run on UNFIXED code. **EXPECTED OUTCOME**: FAILS (PackagingError "no published Greengrass component" — the misleading rejection). Document the counterexample
  - Mark task 15 [x] when written, run, and the failure documented
  - _Requirements: 1.17, 1.18, 1.19_

- [x] 16. Write Defect G preservation property tests (BEFORE implementing the fix)
  - **Property 14: Preservation** - vLLM resolution and other gates unchanged
  - Observation-first on the G-unfixed tree: vLLM records (singular `published_component` with component_name) resolve to today's exact output; records with no registry entry raise the "no record" error; genuinely unpublished records (no singular component_name, no plural entries) raise the "no published Greengrass component" error; `model_component_dependencies`, `plugin_component_dependencies`, `local_server_component_dependencies` outputs for sample inputs are stable
  - Write Hypothesis properties encoding ONLY behavior that must survive the fix (do not constrain the plural-record path — that is Property 13's domain)
  - Run on UNFIXED code. **EXPECTED OUTCOME**: PASSES
  - Mark task 16 [x] when written, run, and passing
  - _Requirements: 2.21, 3.17, 3.18, 3.19_

- [x] 17. Fix Defect G — vision published_components resolution + single-name model emission

  - [x] 17.1 Implement per design Fix Implementation §8 in `edge-cv-portal/backend/functions/workflow_packaging.py`
    - Add the arch→publish-target map (mirror greengrass_publish's target naming; verify the exact target strings from greengrass_publish.py / deployments.py before hardcoding)
    - `resolve_model_components` gains the selected archs: singular `published_component` path unchanged (vLLM); else filter plural `published_components` to status-published entries whose target matches a selected arch; every selected arch covered or PackagingError naming model + uncovered arch; keep today's messages for no-record and genuinely-unpublished records
    - Model dependency emission: single distinct component name → one unpinned HARD entry (as today); multiple → omit that model's entries with a logged warning (Defect F discipline)
    - Update `resolve_model_components`'s docstring for both record shapes
    - _Bug_Condition: isBugCondition_G — plural-only vision record rejected / multi-name emission, from design_
    - _Expected_Behavior: Property 13, from design_
    - _Preservation: Property 14 — vLLM path byte-identical, other gates untouched_
    - _Requirements: 2.18, 2.19, 2.20, 2.21, 3.17, 3.18, 3.19_

  - [x] 17.2 Re-run the SAME task 15 test — EXPECTED: PASSES
    - _Requirements: 2.18, 2.19, 2.20_

  - [x] 17.3 Re-run the SAME task 16 tests — EXPECTED: PASSES
    - _Requirements: 2.21, 3.17, 3.18, 3.19_

- [x] 18. Checkpoint — Ensure all tests pass, then deploy the fixed packaging Lambda
  - Run edge-cv-portal/backend/tests (Defect C/F/G + name-mismatch packaging tests must pass; known pre-existing failures per repo steering) and test/backend-test/deploy_reliability/
  - Deploy the portal backend (same procedure as task 14: `CDK_STACKS="EdgeCVPortalComputeStack" ./deploy_portal_fixes.sh`) and verify the deployed WorkflowPackagingHandler Lambda contains the plural-resolution logic
  - After deploy, the user repackages the vision workflow (yolo_test) — packaging should now succeed for arm64_jp5+arm64_jp6
  - Ask the user if questions arise

## Notes

- **Test-first ordering is mandatory**: task 1 (bug conditions) must FAIL and task 2 (preservation) must PASS on the UNFIXED code before implementing task 3. Do not modify `src/docker-compose.yaml`, `src/backend/app.py`, the recipe variants, `workflow_packaging.py`, or `vllm_model_prep.py` until the tests are written and their expected outcomes documented.
- **Property references**: Properties 1–4 (Bug Condition/fix) validate Requirements 2.1–2.3+2.7 (A), 2.4–2.7 (B), 2.8–2.9 (C), 2.10 (D); Properties 5–8 (Preservation) validate 3.1+3.6, 3.4+3.5+3.7, 3.3+3.8, 3.2+3.9 respectively, per the design's Correctness Properties.
- **Config tests as the testable seam**: compose and recipe changes are hard to unit test as behavior, so Python tests parse the YAML and assert the reliability-critical properties; the JP6 device (task 5) is the final integration gate.
- **Security preservation gate (builds.md)**: `src/docker-compose.yaml` is preservation-tracked in `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`. The rebaseline (and any Dockerfile baseline updates if the flask-app or frontend Dockerfiles change for `healthcheck.py`/curl) MUST land in the same change per the documented procedure, or the component build gate fails with `preservation golden 'docker_baseline_out_of_scope.json' changed (F(X) != F'(X))`. This is an explicit item in task 3.2.
- **Ship 3.1 and 3.2 together**: `restart: always` cannot re-launch a docker-stopped container while the daemon runs — that recovery path is owned by the Startup `--wait` retry loop (a retried `compose up` starts existing stopped containers). The two changes are one fix for requirement 2.7.
- **Primary fix locations**: `src/docker-compose.yaml` + `src/backend/app.py` (Defect A); `src/backend/endpoints/health.py` + `src/backend/healthcheck.py` + `src/docker-compose.yaml` + `recipe-arm64-jp6.yaml`/`recipe-arm64-jp5.yaml`/`recipe-arm64.yaml`/`recipe-amd64.yaml` (Defect B); `edge-cv-portal/backend/functions/workflow_packaging.py` (Defect C); `src/backend/dda_triton/vllm_model_prep.py` (Defect D).
- **On-hardware gate is user-gated**: task 5 consumes a ~1h gdk build and exercises the live JP6 device (deployment restarts, docker stop of the backend). It runs only with the user's explicit go-ahead and coordination.
- **Defect E test-first ordering is mandatory**: task 6 (bug condition) must FAIL and task 7 (preservation) must PASS on the E-unfixed tree before implementing task 8. Do not create `src/host_scripts/compose_lifecycle.sh` or modify the recipe variants until the tests are written and their expected outcomes documented.
- **Defect E property references**: Property 9 (Bug Condition/fix) validates Requirements 2.11–2.14; Property 10 (Preservation) validates 3.10–3.13, per the design's Correctness Properties.
- **Defect E fix locations**: `src/host_scripts/compose_lifecycle.sh` (new helper, shipped via `build-custom.sh`'s existing host_scripts copy) + the four recipe variants (`recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml` — identical edits, variants in sync). Recipes are NOT security-baseline-tracked and `src/docker-compose.yaml` is untouched by this defect, so no rebaseline is needed; `gdk-config.json` and root `recipe.yaml` are build artifacts and are never touched.
- **Defect E testable seam**: the lifecycle logic is shell/YAML, so tests target the extracted helper (`compose_lifecycle.sh` with a stubbed `docker` on PATH) and config-parse the recipes, following the existing `test/backend-test/deploy_reliability/` patterns. The Defect B config/preservation tests in that directory assert recipe structure — expect the intended E edits to require regenerating those goldens (task 9 covers this).
- **Ship 8.1 and 8.2 together**: the recipe edits invoke the helper, and the helper is inert until the recipes call it — one fix, like the 3.1/3.2 pairing.
- **Known pre-existing test failures to ignore** (repo steering): IAM CDK-synth statement-count, cdk.out drift guard, portal workflow test-runner, `test_property_setup_command_wellformed` collection order.
- **Defect E on-hardware gate is user-gated**: task 10 rides the next LocalServer build (~1h gdk) and exercises the live ryan-orin-nano device (reboots, nucleus restarts, stalled-teardown injection). It runs only with the user's explicit go-ahead and coordination — same pattern as task 5.
- **Defect F test-first ordering is mandatory**: task 11 (bug condition) must FAIL and task 12 (preservation) must PASS on the F-unfixed tree before implementing task 13. Do not modify `workflow_packaging.py` until the tests are written and their expected outcomes documented.
- **Defect F property references**: Property 11 (Bug Condition/fix) validates Requirements 2.15–2.17; Property 12 (Preservation) validates 3.14–3.16, per the design's Correctness Properties.
- **Defect F fix location**: `edge-cv-portal/backend/functions/workflow_packaging.py::local_server_component_dependencies` only. Portal-side change — no gdk build, no device build; task 14's portal deploy makes it live. Independent of the user-gated task 10.
- **Defect G test-first ordering is mandatory**: task 15 must FAIL and task 16 must PASS on the G-unfixed tree before task 17. Portal-side only (`workflow_packaging.py`); no device build. Verified registry shapes: vision records carry per-target `published_components` (plural list), vLLM records carry `published_component` (singular map).
- **Defect G property references**: Property 13 validates Requirements 2.18–2.20; Property 14 validates 2.21 + 3.17–3.19.
