# Edge Deploy Reliability Bugfix Design

## Overview

A Greengrass deployment adding a `dda.workflow.*` component to a JP6 device failed as `FAILED_UNABLE_TO_ROLLBACK` and left the device without a working backend. The verified incident timeline: the deployment restart delivered SIGTERM to the LocalServer component's attached `docker compose up` at t+0; the *new* component instance's Run script started a second `compose up` that brought up the frontend at ~t+11s; at t+13s the old compose stop SIGKILLed the backend container mid-shutdown (ExitCode 137, `OOMKilled=false` — a Docker stop-timeout kill, not the kernel OOM killer); the backend container stayed `Exited(137)` permanently while Greengrass reported LocalServer as RUNNING (the attached `compose up` kept serving the frontend); `model-vllm-opt125m-smoke` — which already declares a HARD dependency on LocalServer — retried its load against the dead vLLM runtime on 127.0.0.1:8901 for ~70 seconds, three component restarts in a row, went BROKEN, and failed the deployment.

The root cause is the compose restart race: the backend's shutdown cleanup (`cleanup_workflow_digital_inputs` in `src/backend/app.py`) exceeds Docker's default 10-second stop grace period, and neither `restart: unless-stopped` nor the racing second `compose up` (which saw the backend still "running", mid-stop) recovers a docker-stopped container.

The fix has four parts, each mapped to a defect in the requirements:

1. **Compose race fix** (2.1–2.3): `stop_grace_period` sized above worst-case graceful shutdown and `restart: always` on both backend services in `src/docker-compose.yaml`, plus a bounded fast-SIGTERM path in the backend's FastAPI shutdown handler so graceful shutdown always fits the grace window.
2. **Health-gated lifecycle** (2.4–2.7): docker healthchecks for the backend (new unauthenticated `/health` endpoint, including vLLM 8901 reachability when the runtime was started) and frontend, and a recipe lifecycle change across all four LocalServer recipe variants — `Run` (attached) replaced by `Startup` running `docker compose up -d --wait` — so Greengrass reports RUNNING only when the backend is actually healthy.
3. **Workflow component dependencies** (2.8, 2.9): `workflow_packaging.py` emits ComponentDependencies with HARD dependencies on each published model component the workflow uses and on the LocalServer component matching each target architecture, merged with the existing `dda.plugin.*` dependencies.
4. **Actionable diagnostics** (2.10): `vllm_model_prep.py` classifies a retry window that ends with pure connection-level failures (runtime never reachable) and emits an error naming the LocalServer backend container as the likely cause with concrete verification steps.

## Glossary

- **Bug_Condition (C)**: The condition triggering each defect — see the per-defect formal specifications in Bug Details. The umbrella condition is: a deployment restarts LocalServer while the backend's graceful shutdown exceeds the Docker stop grace window, and the surrounding layers (lifecycle health, recipe dependencies, prep-script diagnostics) fail to detect, prevent, or explain the resulting dead backend.
- **Property (P)**: The desired behavior — the backend survives (or automatically recovers from) a deployment restart, Greengrass RUNNING implies a healthy backend, generated workflow recipes carry real dependency edges, and an unreachable-runtime failure names its likely cause.
- **Preservation**: All behavior for non-bug inputs must be unchanged: clean fast shutdowns, model staging/loading against a healthy backend, existing workflow execution, profile/arch selection, `dda.plugin.*` dependency emission, and the prep script's existing specific error paths.
- **LocalServer**: The per-architecture Greengrass component (`aws.edgeml.dda.LocalServer.arm64JP4/.arm64JP5/.arm64JP6/.amd64`, plus the retired bare `.arm64` recipe variant) whose lifecycle runs the docker compose stack from `src/docker-compose.yaml`.
- **backend container**: The `flask-app` image container (`backend_tegra_gpu_enabled` under the `tegra` profile, `backend_generic` under the `generic` profile) hosting the FastAPI app, the embedded Triton client, and — on vLLM-capable images — the companion vLLM runtime on 127.0.0.1:8901.
- **`shutdown_event`**: The FastAPI `@app.on_event("shutdown")` handler in `src/backend/app.py` that runs `cleanup_workflow_digital_inputs()` (the "Cleaning up digital input workflows" path) and `disconnect_all_cameras()` inline on SIGTERM.
- **`build_recipe` / `plugin_component_dependencies`**: Functions in `edge-cv-portal/backend/functions/workflow_packaging.py` that generate the `dda.workflow.{workflowId}` Greengrass recipe; today the only ComponentDependencies emitted are `dda.plugin.*` HARD deps.
- **`model_ref` parameter**: Node parameter type (`workflow_core` catalog) whose value names a registered model record — `model_inference.modelName` (vision) and `llm_inference.modelName` (vLLM). The packaging flow can resolve these to published Greengrass model components via the training-jobs table (the same registry snapshot `workflow_validation.py` loads).
- **`request_load`**: The retry loop in `src/backend/dda_triton/vllm_model_prep.py` (backoff schedule 3/6/12/24/48s, ~70s total) that POSTs the model load to the vLLM runtime and currently exits with a generic retry-exhausted message.
- **stop grace period**: The time Docker waits between SIGTERM and SIGKILL when stopping a container; default 10s, configurable per service via `stop_grace_period` (also the default timeout used by `docker compose stop`/`down` when `-t` is not given).

## Bug Details

### Bug Condition

Four related defects share one incident. The umbrella condition and per-defect conditions:

**Defect A — compose restart race (root cause, 1.1–1.3):** manifests when a deployment restarts the LocalServer component while the backend is running shutdown cleanup that exceeds Docker's 10-second default grace window.

**Formal Specification:**
```
FUNCTION isBugCondition_A(input)
  INPUT: input of type DeploymentRestart
         {backendShutdownDuration, stopGracePeriod, restartPolicy}
  OUTPUT: boolean

  RETURN input.backendShutdownDuration > input.stopGracePeriod   -- 10s default today
         AND backendSIGKILLed(input)                             -- exit 137, OOMKilled=false
         AND input.restartPolicy = "unless-stopped"              -- does not restart docker-stopped containers
         AND backendContainerState(after: composeUp(input)) = Exited
END FUNCTION
```

**Defect B — RUNNING ≠ healthy (1.4–1.6):** manifests when the backend container is dead but the frontend still runs.

```
FUNCTION isBugCondition_B(state)
  INPUT: state of type DeviceState
  OUTPUT: boolean

  RETURN state.backendContainer = DEAD
         AND state.frontendContainer = RUNNING
         AND greengrassState(LocalServer) = RUNNING       -- attached `compose up` still alive
         AND dependentComponentsStart(state)              -- HARD deps satisfied by RUNNING alone
         AND NOT composeDefinesHealthchecks(state)
END FUNCTION
```

**Defect C — missing workflow dependencies (1.7, 1.8):** manifests whenever `workflow_packaging.py` packages a workflow.

```
FUNCTION isBugCondition_C(recipe)
  INPUT: recipe generated by build_recipe for a workflow using models M ≠ ∅
         and target architectures A
  OUTPUT: boolean

  RETURN (∀ m ∈ M: modelComponent(m) ∉ recipe.ComponentDependencies)
         AND (∀ a ∈ A: localServerComponent(a) ∉ recipe.ComponentDependencies)
         -- only dda.plugin.* entries are ever present
END FUNCTION
```

**Defect D — generic diagnostics (1.9):** manifests when the vLLM runtime is never reachable across the full retry window.

```
FUNCTION isBugCondition_D(attempts)
  INPUT: attempts — the outcome sequence of request_load's retry loop
  OUTPUT: boolean

  RETURN (∀ attempt ∈ attempts: outcome(attempt) ∈
            {SERVER_NOT_REACHABLE, CONNECTION_ERROR})     -- never any HTTP response
         AND emittedMessage = "load request did not succeed; exiting non-zero
                               so the component retries"  -- names no cause
END FUNCTION
```

### Examples

- **Incident (Defect A + B)**: deployment restart SIGTERM at t+0 → new `compose up` starts the frontend at ~t+11s → backend SIGKILLed at t+13s (exit 137, `OOMKilled=false`), stuck `Exited(137)`; Greengrass reports LocalServer RUNNING for the rest of the incident. Expected: the backend finishes shutdown gracefully and comes back up, or LocalServer is not RUNNING until it does.
- **Defect B downstream**: `model-vllm-opt125m-smoke` (HARD dep on LocalServer) starts, `vllm_model_prep.py` retries 127.0.0.1:8901 for ~70s of connection-refused, exits 1; after three Greengrass restarts the component is BROKEN and the deployment ends `FAILED_UNABLE_TO_ROLLBACK`. Expected: the HARD dependency blocks the model component until the backend is actually healthy.
- **Defect C**: `build_recipe("wf-123", 3, ...)` for a workflow whose `llm_inference` node binds `modelName: opt125m-smoke`, packaged for `arm64_jp6`, emits `ComponentDependencies` containing only `dda.plugin.*` entries (or none). Expected: HARD entries for `model-vllm-opt125m-smoke` and `aws.edgeml.dda.LocalServer.arm64JP6`.
- **Defect D**: every load attempt dies in `requests.ConnectionError` (connection refused); the final log is the generic "staged but the load request did not succeed" message. Expected: an error naming the LocalServer backend container as the likely cause and how to check it.
- **Edge case (Defect A)**: backend shutdown completes in 2s — no kill occurs today and none may occur after the fix (preservation 3.1).
- **Edge case (Defect D)**: attempt 1 is connection-refused but attempt 2 receives HTTP 409 — NOT the bug condition; the existing authoritative HTTP-error message must be kept (3.9).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- Fast, clean shutdowns keep working: when the backend finishes its cleanup within the grace window, the compose down/up cycle completes exactly as today and all services come up (3.1).
- Model components (Triton/LFV vision and vLLM) starting against a healthy backend continue to stage and load successfully — the seed-wait Startup gates, staging, and load requests are untouched (3.2).
- Previously deployed workflow components keep executing; the workflow artifact layout, install path (`/aws_dda/workflows/...`), and one-shot Run lifecycle of `dda.workflow.*` recipes are unchanged (3.3).
- Compose profile and architecture selection (`tegra`/`generic` via `$DOCKER_PROFILE`, JP4/JP5/JP6/x86 recipe variants) is unchanged; the shared `src/docker-compose.yaml` keeps working for every variant (3.4, 3.5).
- Crash auto-recovery: a backend that dies on its own (e.g. the AWS CRT event-stream SIGABRT the current restart policy comment documents) is still restarted automatically — `restart: always` is a strict superset of `unless-stopped` for crash exits (3.6).
- The gdk build/publish pipeline is unchanged except for the intended recipe and compose edits (3.7).
- `workflow_packaging.py` keeps emitting the existing `dda.plugin.*` HARD dependencies, pinned exactly as `plugin_component_dependencies` does today, alongside the new entries (3.8).
- `vllm_model_prep.py` keeps its exact existing messages and exit codes for repository validation defects, unresolvable weights paths, and authoritative HTTP error responses from the runtime (3.9).

**Scope:**

All inputs that do NOT involve a deployment-restart race, a dead backend, workflow recipe generation, or a never-reachable vLLM runtime are completely unaffected. This includes:

- Normal steady-state operation of the compose stack (no restart in flight)
- Vision model conversion/loading paths (`model_convertor.py`) and the vLLM runtime's own load/unload semantics
- The workflow compiler, validator, artifact assembly, staging/promotion, and component registration flow (only the recipe's ComponentDependencies block gains entries)
- The prep script's happy path (HTTP 200 → exit 0)

## Hypothesized Root Cause

The incident evidence confirms the causal chain (this is a verified root cause, not a hypothesis to refute; the exploratory tests below confirm each link on unfixed code):

1. **Backend graceful shutdown exceeds the default grace window**: `shutdown_event` in `src/backend/app.py` runs `cleanup_workflow_digital_inputs()` — iterating every workflow with image sources and calling `terminate_digital_input_task` — plus `disconnect_all_cameras()` inline. On a device with configured workflows this exceeds 10 seconds. Neither backend service in `src/docker-compose.yaml` declares `stop_grace_period`, so Docker SIGKILLs at 10s (exit 137).
2. **`restart: unless-stopped` cannot recover a docker-stopped container**: a container stopped by the Docker daemon (including a stop that escalated to SIGKILL) is "stopped" for restart-policy purposes; no restart policy re-launches it while the daemon runs.
3. **The Greengrass restart races old-stop against new-up**: Greengrass restarts the component by signaling the old attached `Run` process while starting the new instance. The new `compose up --no-build` at t+11s observed the backend container still present and "running" (mid-stop), so it did not (re)create or start it; the old stop then killed it at t+13s. Result: `Exited(137)` forever.
4. **The attached `Run` script hides the death**: `docker compose up` (attached) stays alive while any service runs; the frontend kept it alive, so Greengrass reported RUNNING and every HARD dependency on LocalServer was satisfied against a dead backend. No healthcheck existed for Docker or Greengrass to observe otherwise (`docker compose up -d --wait` would have failed; there was nothing for it to wait on).
5. **No dependency edges from the workflow component**: the generated `dda.workflow.*` recipe declares no dependency on `model-vllm-opt125m-smoke` or `aws.edgeml.dda.LocalServer.arm64JP6`, so Greengrass had no ordering/health relationship to enforce or to report; the failure surfaced as an unrelated-looking model component break.
6. **Diagnostics dead-end**: ~70s of connection-refused ended in a generic retry-exhausted message; nothing pointed at the actual dead backend container.

## Correctness Properties

Property 1: Bug Condition - Backend survives or recovers from deployment restarts

_For any_ deployment restart where the backend's graceful shutdown work would exceed Docker's default grace window (isBugCondition_A), the fixed configuration and shutdown path SHALL prevent the permanent-dead-backend outcome: both backend services declare `stop_grace_period` of at least 120 seconds and `restart: always`, and the backend's SIGTERM handler completes within a bounded cleanup budget (20 seconds) strictly below the grace period, so the backend is never SIGKILLed mid-cleanup and a killed backend never remains `Exited` behind a completed lifecycle cycle.

**Validates: Requirements 2.1, 2.2, 2.3, 2.7**

Property 2: Bug Condition - Greengrass RUNNING implies healthy backend

_For any_ LocalServer component start where the backend container does not reach a healthy state (isBugCondition_B), the fixed lifecycle SHALL NOT report the component as RUNNING: every recipe variant's Startup script runs `docker compose up -d --wait` (exit 0 only when all started services pass their healthchecks), the backend services declare a healthcheck probing the new `/health` endpoint (which verifies vLLM 8901 reachability whenever the vLLM runtime was started in-process), and the frontend declares a basic healthcheck.

**Validates: Requirements 2.4, 2.5, 2.6, 2.7**

Property 3: Bug Condition - Generated workflow recipes carry model and LocalServer dependencies

_For any_ workflow definition containing `model_ref` parameter values M (each resolving to a published model component) and any non-empty selected architecture set A, the fixed `build_recipe` output SHALL contain a ComponentDependencies block with a HARD entry for each distinct published model component of M and an entry for the LocalServer component matching each distinct architecture in A (with the per-arch minimum-version floor as its VersionRequirement).

**Validates: Requirements 2.8, 2.9**

Property 4: Bug Condition - Never-reachable runtime failures are actionable

_For any_ retry sequence in which every load attempt fails at the connection level and no HTTP response is ever received (isBugCondition_D), the fixed prep script SHALL exit non-zero with an error message that names the LocalServer backend container (flask-app) as the likely cause and includes concrete verification steps (checking `docker ps -a` for the backend container and the LocalServer component logs).

**Validates: Requirements 2.10**

Property 5: Preservation - Clean shutdowns and crash recovery are unchanged

_For any_ shutdown whose cleanup completes within the cleanup budget (NOT isBugCondition_A), the fixed shutdown handler SHALL execute exactly the same cleanup actions in the same order as the original (digital input workflow cleanup, then camera disconnect) and complete the compose down/up cycle cleanly; and for any self-crash of the backend process, the `restart: always` policy SHALL auto-recover the container as `unless-stopped` did.

**Validates: Requirements 3.1, 3.6**

Property 6: Preservation - Compose and recipe structure unchanged beyond the intended edits

_For any_ compose profile/arch selection (NOT isBugCondition_B), the fixed `src/docker-compose.yaml` SHALL be identical to the original except for the added `stop_grace_period`, `restart` value, and `healthcheck` keys (services, profiles, images, build args, volumes, environment, ports all unchanged), and each fixed recipe variant SHALL be identical to the original except for the Run→Startup lifecycle replacement (Install, Shutdown, dependencies, configuration, artifacts unchanged).

**Validates: Requirements 3.4, 3.5, 3.7**

Property 7: Preservation - Existing packaging output unchanged apart from added dependencies

_For any_ workflow input (with or without Custom_Node_Type plugins), the fixed `build_recipe` output SHALL equal the original output in every field except ComponentDependencies, and its ComponentDependencies SHALL contain the original `dda.plugin.*` entries unchanged (same names, pinned VersionRequirements, HARD type) as a subset; existing workflow components already on devices are untouched (no recipe field they consume changes).

**Validates: Requirements 3.3, 3.8**

Property 8: Preservation - Prep script's specific error paths unchanged

_For any_ input hitting a repository validation defect, an unresolvable weights path, or an authoritative HTTP error response from the runtime (NOT isBugCondition_D), the fixed `vllm_model_prep.py` SHALL produce the same error messages and the same exit codes as the original, and for any successful load (HTTP 200) SHALL behave identically; model components starting against a healthy backend stage and load exactly as before.

**Validates: Requirements 3.2, 3.9**

## Fix Implementation

### Changes Required

The root cause analysis is confirmed by the incident evidence; the changes below follow directly from it.

#### 1. Compose race fix (Requirements 2.1, 2.2, 2.3)

**File**: `src/docker-compose.yaml`

**Services**: `backend_tegra_gpu_enabled`, `backend_generic`

**Specific Changes**:

1. **`stop_grace_period: 120s`** on both backend services. Sized well above the worst-case graceful shutdown: the fixed backend bounds its cleanup at 20s (below), and uvicorn's own connection draining plus process teardown stays in the low seconds; 120s gives a 5–6x margin for slow devices under load. `docker compose stop`/`down` uses this value as the default timeout when `-t` is not passed, so the recipe Shutdown script needs no change.
2. **`restart: unless-stopped` → `restart: always`** on both backend services (frontend keeps `unless-stopped`; it was never part of the failure mode and requirement 2.2 targets the backend). `always` is a strict superset for crash recovery (preserving the AWS CRT SIGABRT protection, 3.6) and additionally re-launches a previously-stopped backend when the Docker daemon restarts (device reboot / dockerd restart), one of the ways an `Exited` backend could otherwise persist. Update the existing policy comment to document the new rationale. Note honestly captured in the comment: no restart policy re-launches a docker-stopped container while the daemon runs — that recovery path is owned by the Startup `--wait` retry loop in change 2, which is why both changes ship together (2.2 + 2.7).

**File**: `src/backend/app.py`

**Function**: `shutdown_event` (the `@app.on_event("shutdown")` handler; the "Cleaning up digital input workflows" path)

**Specific Changes**:

3. **Bounded fast-SIGTERM path**: run the existing cleanup body (`cleanup_workflow_digital_inputs()` then `disconnect_all_cameras()`) inside a single time budget:

```python
SHUTDOWN_CLEANUP_BUDGET_SECONDS = 20  # << stop_grace_period (120s)

@app.on_event("shutdown")
async def shutdown_event():
    def _cleanup():
        cleanup_workflow_digital_inputs()
        disconnect_all_cameras()
    try:
        await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _cleanup),
            timeout=SHUTDOWN_CLEANUP_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(
            "Shutdown cleanup exceeded %ss budget; proceeding with shutdown "
            "(remaining cleanup is abandoned — the container is being torn "
            "down)", SHUTDOWN_CLEANUP_BUDGET_SECONDS)
```

   This is the minimal change: same cleanup, same order, but the process can no longer hang past the grace window on a slow `terminate_digital_input_task` or camera disconnect. The abandoned work is non-essential on the shutdown path — the container (and its digital-input threads/processes) is being destroyed anyway, and `setup_workflow_digital_inputs()` reconstructs the digital input state on the next start.

#### 2. Health-gated lifecycle (Requirements 2.4, 2.5, 2.6, 2.7)

**File**: `src/backend/app.py` (+ a small new module, e.g. `src/backend/endpoints/health.py`)

**Specific Changes**:

1. **Unauthenticated `GET /health` endpoint**: returns 200 when the app is serving AND, if the vLLM runtime was started in-process, 127.0.0.1:8901 accepts a connection; 503 otherwise. Registered like `local_auth`'s unauthenticated router (exempt from `authorize_request`). Conditionality lives here, not in compose: `start_vllm_runtime()` already returns the started `VllmRuntimeServer` or `None` (vLLM absent from the image, or startup failed containedly per vllm-triton-inference 4.3); the endpoint checks 8901 only when a runtime server was actually started (`health.set_vllm_server(...)` called with a non-None value from the `__main__` startup sequence). A contained vLLM startup failure therefore does not flip the backend unhealthy — preserving the existing containment semantics — while a started-then-dead runtime (the incident shape, had the backend half-survived) is reported unhealthy. The 8901 probe is a short-timeout TCP connect (or `GET /v2/repository/index`), never a model invocation.

2. **Healthcheck helper script** `src/backend/healthcheck.py` (shipped in the flask-app image): probes `http://127.0.0.1:5000/health` and, on failure, `https://127.0.0.1:5443/health` with certificate verification disabled — the backend serves on 5443/TLS when station authorization is enabled, 5000 otherwise (`main()` in app.py). Exit 0 iff either returns 200. Python is used because the backend image is not guaranteed to carry curl/wget; both backend services use `network_mode: host`, so loopback works.

**File**: `src/docker-compose.yaml`

3. **Backend healthcheck** on both backend services:

```yaml
healthcheck:
  test: ["CMD", "python3", "/healthcheck.py"]   # path per Dockerfile COPY
  interval: 15s
  timeout: 10s
  retries: 4
  start_period: 300s   # DB migration + triton setup + vLLM runtime start on JP6
```

4. **Frontend healthcheck**: a basic HTTP probe of the nginx-served app on container port 80, e.g. `test: ["CMD", "curl", "-fsS", "http://127.0.0.1:80/"]` with `interval: 30s, timeout: 5s, retries: 3, start_period: 30s`. Implementation note: verify the react-webapp image carries curl (add it in `src/frontend/Dockerfile` or fall back to `wget -q -O /dev/null` / a node one-liner, whichever the image supports).

**Files**: `recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml` (all four variants share the compose file and the same lifecycle shape — 3.5)

5. **Run → Startup lifecycle**: Greengrass allows `Run` or `Startup`, not both, and reports a component RUNNING when its Startup script exits 0. Replace each recipe's `Run` block with a `Startup` block containing the identical script (same `SetEnv`, same host setup scripts, same `/tmp/.dda.env` export) except the final line becomes detached and health-gated:

```
docker compose --profile $DOCKER_PROFILE -f .../docker-compose.yaml up -d --no-build --wait --wait-timeout 600
```

   with `Timeout: 900` on the Startup block (Greengrass's default Startup timeout of 120s is far below a cold JP6 backend boot). Semantics:
   - `--wait` blocks until every started service is `running`/`healthy` and exits non-zero if any container fails or goes unhealthy → Greengrass marks the lifecycle errored, retries, and after three failures the component is BROKEN — the deployment now fails *at LocalServer*, truthfully, instead of reporting RUNNING over a dead backend (2.4) and letting a model component take the blame.
   - A retried Startup re-runs `compose up`, which **starts existing stopped containers** — this is the recovery path for a container the race managed to kill anyway, and (with `down` removing containers on Shutdown) guarantees no lifecycle cycle leaves behind a stopped backend that a subsequent up won't start (2.7).
   - After Startup exits, no attached process remains; in-steady-state crash recovery is owned by the Docker restart policy exactly as it (factually) already was — the attached `compose up` never restarted docker-stopped containers either (3.6 preserved).
   - `Shutdown` blocks are unchanged (`docker compose down`, plus `systemctl stop nvidia-csi-capture` on the arm variants); `down` now inherits the 120s grace period as its stop timeout.

#### 3. Workflow component dependencies (Requirements 2.8, 2.9, 3.8)

**File**: `edge-cv-portal/backend/functions/workflow_packaging.py`

**Specific Changes**:

1. **`gather_model_references(definition, descriptors_by_id) -> List[str]`**: collect the effective values of every `model_ref`-typed parameter (`PARAM_TYPE_MODEL_REF`) across the definition's nodes — today that is `model_inference.modelName` and `llm_inference.modelName` — deduplicated, in stable order. Generic over the parameter type (not node-type allowlists) so future model-bound node types are covered automatically.

2. **`resolve_model_components(model_names, usecase) -> Dict[str, Dict]`**: resolve each model name against the Use_Case's model registry the same way `workflow_validation.py` does (training-jobs table via `usecase-training-index`, keyed by `model_name` — records already validated to exist by the packaging-time validation guard) and extract the published Greengrass component: `published_component.component_name` (e.g. `model-vllm-opt125m-smoke` for vLLM records, the `model-*` component name for vision records). **Fail closed**: a model record with no published component raises the existing `PackagingError` path (all-or-nothing, no component registered) naming the model — mirroring the plugin gates.

3. **`model_component_dependencies(resolved) -> Dict`**: one entry per distinct component: `{name: {'VersionRequirement': '>=0.0.0', 'DependencyType': 'HARD'}}`. Deliberately unpinned, unlike plugin deps: model components version independently (major-only bumps on republish) and the deployment specifies the concrete version; the dependency's job is the ordering/health edge, not version pinning.

4. **`local_server_component_dependencies(archs) -> Dict`**: map each selected workflow_core arch to its LocalServer variant — `arm64_jp4 → aws.edgeml.dda.LocalServer.arm64JP4`, `arm64_jp5 → …arm64JP5`, `arm64_jp6 → …arm64JP6`, `x86_64`/`x86_64_nvidia` → `…amd64` (same fail-closed naming discipline as `greengrass_publish.TARGET_TO_LOCAL_SERVER`; the retired bare `.arm64` name is never emitted) — with `{'VersionRequirement': '>=' + min_local_server_version_for(arch), 'DependencyType': 'HARD'}`, reusing the existing per-arch minimum-version floors (`minLocalServerVersion` machinery). One entry per distinct variant.

5. **Merge and thread through**: in the packaging handler, `component_dependencies = {**plugin_component_dependencies(dep_records), **model_component_dependencies(...), **local_server_component_dependencies(architectures)}` — the three namespaces (`dda.plugin.*`, `model-*`, `aws.edgeml.dda.LocalServer.*`) are disjoint, so the merge cannot collide; `build_recipe` itself is unchanged (it already attaches any non-empty `component_dependencies`). Plugin entries are passed through byte-identical (3.8).

6. **Documented constraint**: Greengrass `ComponentDependencies` is recipe-global, not per-platform-manifest. A package spanning architectures with *different* LocalServer variants therefore emits multiple LocalServer entries, which cannot co-resolve on one device. Mitigations, recorded in the function docstring: (a) the deployment service already gates components against device architecture (device-arch-compatibility / vLLM arch gates), so mixed-variant deployments are rejected upstream; (b) vLLM workflows package to `arm64_jp6` (plus flag-gated `arm64_jp5`) today, making single-variant the operative case. This matches requirement 2.9 ("matching each target architecture") while stating the multi-variant caveat explicitly rather than hiding it.

#### 4. Actionable diagnostics (Requirements 2.10, 3.9)

**File**: `src/backend/dda_triton/vllm_model_prep.py`

**Function**: `request_load` (and its caller `prepare`)

**Specific Changes**:

1. **Classify the terminal failure** instead of returning a bare bool: `request_load` returns one of `LOAD_OK`, `LOAD_HTTP_ERROR` (an authoritative HTTP response was received and was not 200 — message and single-attempt semantics unchanged), `LOAD_UNREACHABLE` (every attempt ended in `wait_for_server` failure or a connection-level `requests.RequestException` — `ConnectionError`/refused/reset — with no HTTP response ever received). Tracking is one boolean (`got_http_response`) plus the existing loop; per-attempt log lines are unchanged.

2. **Actionable terminal message in `prepare`** for `LOAD_UNREACHABLE` (replacing the generic message only for this classification; exit code stays 1):

```
Model '{m}' staged, but the vLLM runtime at 127.0.0.1:8901 was never
reachable across the full retry window (~70s of connection failures).
Likely cause: the LocalServer backend container (image 'flask-app') is not
running — a deployment restart can leave it stopped. Verify with:
  sudo docker ps -a --filter ancestor=flask-app   (look for Exited)
  sudo docker logs <container-id>
and check the LocalServer component log
(/greengrass/v2/logs/aws.edgeml.dda.LocalServer.*.log). Exiting non-zero
so the component retries once the backend is back.
```

3. **Untouched paths** (3.9): `validate_repository` defects, the weights-path FAILED message, the HTTP-error logging in `request_load`, `request_unload`/`cleanup`, and the success path keep their exact current messages and exit codes.

## Testing Strategy

### Validation Approach

Two phases: first surface counterexamples demonstrating each defect on UNFIXED code (confirming the causal chain), then verify the fixes and preservation. Compose and recipe changes are hard to unit test as behavior, so they get **config tests** — Python tests that parse the YAML and assert the reliability-critical properties — as the testable seam; the JP6 device is the final integration gate.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate each defect BEFORE implementing the fix, confirming the (already evidence-backed) root cause analysis. If any is refuted, re-hypothesize.

**Test Plan**: Config assertions run against the unfixed YAML; Python tests run against the unfixed functions with mocked I/O.

**Test Cases**:
1. **Compose config exposure**: parse `src/docker-compose.yaml`; assert backend services declare a `stop_grace_period` and a `healthcheck` — fails on unfixed file (fields absent), demonstrating isBugCondition_A/B structurally (will fail on unfixed code)
2. **Recipe lifecycle exposure**: parse all four `recipe-*.yaml`; assert the lifecycle gates RUNNING on health (`Startup` with `up -d --wait`) — fails on unfixed recipes (attached `Run … up --no-build`) (will fail on unfixed code)
3. **Missing dependencies**: call `build_recipe` (unfixed) for a definition with an `llm_inference` model ref, arch `arm64_jp6`; assert `model-vllm-*` and `aws.edgeml.dda.LocalServer.arm64JP6` appear in ComponentDependencies — fails on unfixed code (only plugin entries)
4. **Generic diagnostics**: run `prepare` with mocked `requests.post` raising `ConnectionError` on every attempt (and `time.sleep` stubbed); assert the output names the LocalServer backend container — fails on unfixed code (generic retry-exhausted message)
5. **Slow-shutdown exposure**: invoke the unfixed `shutdown_event` with `terminate_digital_input_task` mocked to block 30s; measure that the handler runs past a 20s budget (may fail on unfixed code depending on timer resolution — expected to exceed)

**Expected Counterexamples**:
- Absent `stop_grace_period`/`restart: always`/healthchecks; attached Run lifecycle; ComponentDependencies without model/LocalServer entries; the literal generic message; an unbounded shutdown handler
- Possible causes all confirmed by incident evidence: Docker stop-timeout kill, restart-policy semantics for docker-stopped containers, recipe-global RUNNING-on-attach, dependency emission limited to `dda.plugin.*`

### Fix Checking

**Goal**: Verify that for all inputs where a bug condition holds, the fixed artifacts produce the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition_X(input) DO      -- X ∈ {A, B, C, D}
  result := fixedArtifact(input)
  ASSERT expectedBehavior_X(result)                 -- Properties 1–4
END FOR
```

Concretely: config tests assert Properties 1–2 over the fixed YAML (grace period ≥ 120s, `restart: always`, healthchecks present with sane parameters, every recipe variant's Startup uses `up -d --wait` with a Timeout, Shutdown unchanged, all four variants consistent); packaging tests assert Property 3 over generated recipes; prep-script tests assert Property 4 over mocked connection-failure sequences; the bounded-shutdown test asserts the handler returns within budget when cleanup blocks forever.

### Preservation Checking

**Goal**: Verify that for all inputs where no bug condition holds, the fixed functions produce the same result as the original functions.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition_X(input) DO
  ASSERT originalFunction(input) = fixedFunction(input)
END FOR
```

**Testing Approach**: Property-based testing (Hypothesis, already used in this repo) is recommended for preservation checking because it generates many cases across the input domain, catches edge cases manual tests miss, and gives strong guarantees that non-buggy behavior is unchanged.

**Test Plan**: Capture the unfixed `build_recipe` and `vllm_model_prep` outputs for non-bug inputs first (golden behavior), then write property-based tests asserting the fixed versions match.

**Test Cases**:
1. **Recipe equality modulo dependencies**: observe unfixed `build_recipe` output across generated workflows/arches, then verify the fixed output is identical in every field except ComponentDependencies, and that all original `dda.plugin.*` entries survive unchanged (Property 7)
2. **Prep script error-path equality**: observe unfixed messages/exit codes for validation defects, bad weights paths, and HTTP 4xx/5xx (mocked), then verify the fixed script reproduces them exactly (Property 8)
3. **Fast-shutdown equivalence**: observe that quick cleanup executes both cleanup calls in order on unfixed code, then verify the fixed handler does the same when cleanup completes within budget (Property 5)
4. **Compose equality modulo added keys**: parse original and fixed compose files; verify deep-equality after deleting only `stop_grace_period`, `restart`, `healthcheck` (Property 6)

### Unit Tests

- `vllm_model_prep.request_load` classification: all-connection-refused → `LOAD_UNREACHABLE`; refused-then-HTTP-409 → `LOAD_HTTP_ERROR` with existing message; refused-then-200 → `LOAD_OK` (mocked `requests`/`wait_for_server`, stubbed sleeps)
- `prepare` terminal messages per classification; exit codes unchanged (always 1 on failure, 0 on success)
- `gather_model_references`: model_inference-only, llm_inference-only, mixed, duplicates deduplicated, no-model workflows → empty
- `resolve_model_components` fail-closed on unpublished records (PackagingError naming the model)
- `local_server_component_dependencies`: each arch id maps to its exact variant name; `x86_64`+`x86_64_nvidia` collapse to one `amd64` entry; per-arch version floors applied
- `/health` endpoint: 200 with no vLLM server set; 200 with vLLM set and 8901 accepting; 503 with vLLM set and 8901 refused (loopback stub server)
- Bounded shutdown handler: blocking cleanup → returns within budget and logs the warning; fast cleanup → both calls executed in order

### Property-Based Tests

- _For any_ generated workflow definition (random model-ref sets, plugin sets, arch subsets of DEVICE_ARCHITECTURES): the fixed recipe contains a HARD entry per distinct published model component and per distinct LocalServer variant, preserves all plugin entries exactly, and equals the unfixed recipe outside ComponentDependencies (Properties 3, 7)
- _For any_ random attempt-outcome sequence over {connection-error, http-error(code), success}: the classification is UNREACHABLE iff no HTTP response appears in the sequence prefix consumed by the loop, and the emitted message matches the classification (Properties 4, 8)
- _For any_ random cleanup duration: the shutdown handler's runtime is ≤ budget + ε, and cleanup effects occur iff duration ≤ budget (Properties 1, 5)

### Integration Tests

- **On-hardware JP6 gate (final)**: build and publish the modified LocalServer component (gdk) and portal packaging changes; deploy to the JP6 device; while a workflow with an `llm_inference` node is running, trigger a deployment restart of LocalServer and verify: the backend is never SIGKILLed (no exit 137), or if killed is recovered by the Startup retry; Greengrass reports LocalServer RUNNING only after `docker compose ps` shows the backend healthy; `model-vllm-opt125m-smoke` never goes BROKEN; the deployment completes without `FAILED_UNABLE_TO_ROLLBACK`
- **Dead-backend truthfulness**: on the device, `docker stop` the backend and verify the component's next lifecycle cycle fails Startup (not silent RUNNING) and the retried Startup brings the stopped container back up
- **Recipe dependency ordering**: deploy a freshly packaged workflow component and verify in the Greengrass logs that Greengrass orders it after its model component and LocalServer (HARD edges visible in dependency resolution)
