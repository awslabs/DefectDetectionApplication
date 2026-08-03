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
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE any fix (tests written against unfixed code).
- Task 3 depends on wave 1; sub-tasks 3.5 and 3.6 depend on 3.1–3.4.
- Task 4 depends on task 3. Task 5 depends on task 4 and on the user's go-ahead (live JP6 device + ~1h gdk build).

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

## Notes

- **Test-first ordering is mandatory**: task 1 (bug conditions) must FAIL and task 2 (preservation) must PASS on the UNFIXED code before implementing task 3. Do not modify `src/docker-compose.yaml`, `src/backend/app.py`, the recipe variants, `workflow_packaging.py`, or `vllm_model_prep.py` until the tests are written and their expected outcomes documented.
- **Property references**: Properties 1–4 (Bug Condition/fix) validate Requirements 2.1–2.3+2.7 (A), 2.4–2.7 (B), 2.8–2.9 (C), 2.10 (D); Properties 5–8 (Preservation) validate 3.1+3.6, 3.4+3.5+3.7, 3.3+3.8, 3.2+3.9 respectively, per the design's Correctness Properties.
- **Config tests as the testable seam**: compose and recipe changes are hard to unit test as behavior, so Python tests parse the YAML and assert the reliability-critical properties; the JP6 device (task 5) is the final integration gate.
- **Security preservation gate (builds.md)**: `src/docker-compose.yaml` is preservation-tracked in `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`. The rebaseline (and any Dockerfile baseline updates if the flask-app or frontend Dockerfiles change for `healthcheck.py`/curl) MUST land in the same change per the documented procedure, or the component build gate fails with `preservation golden 'docker_baseline_out_of_scope.json' changed (F(X) != F'(X))`. This is an explicit item in task 3.2.
- **Ship 3.1 and 3.2 together**: `restart: always` cannot re-launch a docker-stopped container while the daemon runs — that recovery path is owned by the Startup `--wait` retry loop (a retried `compose up` starts existing stopped containers). The two changes are one fix for requirement 2.7.
- **Primary fix locations**: `src/docker-compose.yaml` + `src/backend/app.py` (Defect A); `src/backend/endpoints/health.py` + `src/backend/healthcheck.py` + `src/docker-compose.yaml` + `recipe-arm64-jp6.yaml`/`recipe-arm64-jp5.yaml`/`recipe-arm64.yaml`/`recipe-amd64.yaml` (Defect B); `edge-cv-portal/backend/functions/workflow_packaging.py` (Defect C); `src/backend/dda_triton/vllm_model_prep.py` (Defect D).
- **On-hardware gate is user-gated**: task 5 consumes a ~1h gdk build and exercises the live JP6 device (deployment restarts, docker stop of the backend). It runs only with the user's explicit go-ahead and coordination.
