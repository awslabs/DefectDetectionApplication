# Edge Deploy Reliability Bugfix Design

## Overview

A Greengrass deployment adding a `dda.workflow.*` component to a JP6 device failed as `FAILED_UNABLE_TO_ROLLBACK` and left the device without a working backend. The verified incident timeline: the deployment restart delivered SIGTERM to the LocalServer component's attached `docker compose up` at t+0; the *new* component instance's Run script started a second `compose up` that brought up the frontend at ~t+11s; at t+13s the old compose stop SIGKILLed the backend container mid-shutdown (ExitCode 137, `OOMKilled=false` — a Docker stop-timeout kill, not the kernel OOM killer); the backend container stayed `Exited(137)` permanently while Greengrass reported LocalServer as RUNNING (the attached `compose up` kept serving the frontend); `model-vllm-opt125m-smoke` — which already declares a HARD dependency on LocalServer — retried its load against the dead vLLM runtime on 127.0.0.1:8901 for ~70 seconds, three component restarts in a row, went BROKEN, and failed the deployment.

The root cause is the compose restart race: the backend's shutdown cleanup (`cleanup_workflow_digital_inputs` in `src/backend/app.py`) exceeds Docker's default 10-second stop grace period, and neither `restart: unless-stopped` nor the racing second `compose up` (which saw the backend still "running", mid-stop) recovers a docker-stopped container.

The fix has four parts, each mapped to a defect in the requirements:

1. **Compose race fix** (2.1–2.3): `stop_grace_period` sized above worst-case graceful shutdown and `restart: always` on both backend services in `src/docker-compose.yaml`, plus a bounded fast-SIGTERM path in the backend's FastAPI shutdown handler so graceful shutdown always fits the grace window.
2. **Health-gated lifecycle** (2.4–2.7): docker healthchecks for the backend (new unauthenticated `/health` endpoint, including vLLM 8901 reachability when the runtime was started) and frontend, and a recipe lifecycle change across all four LocalServer recipe variants — `Run` (attached) replaced by `Startup` running `docker compose up -d --wait` — so Greengrass reports RUNNING only when the backend is actually healthy.
3. **Workflow component dependencies** (2.8, 2.9): `workflow_packaging.py` emits ComponentDependencies with HARD dependencies on each published model component the workflow uses and on the LocalServer component matching each target architecture, merged with the existing `dda.plugin.*` dependencies.
4. **Actionable diagnostics** (2.10): `vllm_model_prep.py` classifies a retry window that ends with pure connection-level failures (runtime never reachable) and emits an error naming the LocalServer backend container as the likely cause with concrete verification steps.

**Defect E (added after on-hardware verification of A–D)**: a second verified incident on v1.0.46 (device ryan-orin-nano, JP6 Orin Nano) showed the health-gated lifecycle from Defect B has a Shutdown/Startup teardown race. After a device reboot + nucleus restart, Shutdown's `docker compose down`/kill delivered `kill` to both containers (t=1785789983); the frontend died and was destroyed within 1s and Startup's `compose up` recreated it (t=1785790004–5); but the backend takes ~24s to die after SIGKILL escalation (GPU/Triton teardown), so Startup's compose **adopted** the still-dying backend as an existing running container, the `--wait` health gate trusted its stale pre-kill 'healthy' healthcheck state ("Container ...backend_tegra_gpu_enabled-1 Running / Healthy"), and Startup exited 0. Three seconds later (t=1785790007) the old backend finished dying (stop/die/destroy) — leaving NO backend container while Greengrass reported RUNNING and the portal/API was dead. `greengrass-cli component restart` recovered it manually once the old container was fully gone.

5. **Synchronous teardown + adoption-proof Startup** (2.11–2.14): a new compose-lifecycle helper script in `src/host_scripts/` (shipped with every recipe variant via the existing `build-custom.sh` host_scripts copy) gives Shutdown a bounded wait-for-zero-project-containers after `down` (with a Shutdown `Timeout` sized above worst-case teardown — the current recipes declare none, so Greengrass's 15s default truncates any wait), and gives Startup `--force-recreate` (never adopt a previous incarnation's container) plus a post-`--wait` freshness verification (every project container's StartedAt newer than the Startup script's start time) before Greengrass may see RUNNING.

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
- **dying window (Defect E)**: The interval between a container receiving `kill` and its final stop/die/destroy events. Normally near-instant, but the backend's GPU/Triton teardown holds the process in kernel teardown for ~24s after SIGKILL; during this window `docker compose ps` still reports the container as an existing running service with its last-recorded healthcheck state.
- **adoption (Defect E)**: docker compose's default behavior of leaving an existing container that matches the service's current configuration in place on `up` instead of recreating it. Adoption of a healthy steady-state container is normal; adoption of a dying previous-incarnation container is the Defect E bug condition. `--force-recreate` disables adoption entirely.
- **compose lifecycle helper**: The new `src/host_scripts/compose_lifecycle.sh` (shipped into every component artifact by the existing `build-custom.sh` `cp -r src/host_scripts` step; host_scripts are not security-baseline-tracked). Two subcommands, both pure functions of `docker` CLI output so they are testable with a stubbed `docker` on PATH: `wait-empty` (poll `docker compose ... ps -aq` until empty, bounded timeout) and `verify-fresh` (assert every project container's `State.StartedAt` is at or after a reference epoch).
- **Shutdown Timeout (Greengrass)**: Greengrass gives lifecycle Shutdown scripts a 15-second default timeout when the recipe declares none — the current recipes declare none, so any Shutdown wait longer than 15s is truncated.

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

**Defect E — Shutdown/Startup teardown race (1.10–1.13):** manifests when Startup's `compose up` runs while a container from the previous incarnation is still in its (slow) dying window.

```
FUNCTION isBugCondition_E(cycle)
  INPUT: cycle of type LifecycleCycle
         {shutdownExitedWithContainersRemaining, dyingContainer, startupUp}
  OUTPUT: boolean

  RETURN cycle.shutdownExitedWithContainersRemaining          -- down/kill returned before
                                                              -- destroy; no post-down wait,
                                                              -- no Shutdown Timeout (15s default)
         AND inDyingWindow(cycle.dyingContainer)              -- kill delivered, destroy pending
                                                              -- (~24s GPU/Triton teardown)
         AND composeAdopts(cycle.startupUp, cycle.dyingContainer)  -- seen as existing running
         AND healthGateTrusts(staleHealthState(cycle.dyingContainer)) -- 'healthy' from before kill
         AND startupExitCode(cycle) = 0
         AND eventually(containerCount(project, "backend") = 0)   -- destroy completes after exit
END FUNCTION
```

### Examples

- **Incident (Defect A + B)**: deployment restart SIGTERM at t+0 → new `compose up` starts the frontend at ~t+11s → backend SIGKILLed at t+13s (exit 137, `OOMKilled=false`), stuck `Exited(137)`; Greengrass reports LocalServer RUNNING for the rest of the incident. Expected: the backend finishes shutdown gracefully and comes back up, or LocalServer is not RUNNING until it does.
- **Defect B downstream**: `model-vllm-opt125m-smoke` (HARD dep on LocalServer) starts, `vllm_model_prep.py` retries 127.0.0.1:8901 for ~70s of connection-refused, exits 1; after three Greengrass restarts the component is BROKEN and the deployment ends `FAILED_UNABLE_TO_ROLLBACK`. Expected: the HARD dependency blocks the model component until the backend is actually healthy.
- **Defect C**: `build_recipe("wf-123", 3, ...)` for a workflow whose `llm_inference` node binds `modelName: opt125m-smoke`, packaged for `arm64_jp6`, emits `ComponentDependencies` containing only `dda.plugin.*` entries (or none). Expected: HARD entries for `model-vllm-opt125m-smoke` and `aws.edgeml.dda.LocalServer.arm64JP6`.
- **Defect D**: every load attempt dies in `requests.ConnectionError` (connection refused); the final log is the generic "staged but the load request did not succeed" message. Expected: an error naming the LocalServer backend container as the likely cause and how to check it.
- **Edge case (Defect A)**: backend shutdown completes in 2s — no kill occurs today and none may occur after the fix (preservation 3.1).
- **Edge case (Defect D)**: attempt 1 is connection-refused but attempt 2 receives HTTP 409 — NOT the bug condition; the existing authoritative HTTP-error message must be kept (3.9).
- **Incident (Defect E, verified via docker events on ryan-orin-nano, v1.0.46)**: reboot + nucleus restart → Shutdown kill at t=1785789983 → frontend destroyed <1s, backend enters ~24s dying window → Startup `up -d --wait` at t=1785790004–5 recreates the frontend but adopts the dying backend, prints "Running / Healthy" against its stale healthcheck state, exits 0 → backend stop/die/destroy at t=1785790007 → `docker ps -a` shows only the frontend; Greengrass RUNNING; portal dead. Cloud status showed ERRORED then RUNNING. Expected: Startup never adopts a previous incarnation's container and RUNNING implies a backend created by this Startup.
- **Edge case (Defect E)**: Shutdown's `down` completes with all containers destroyed before it exits (fast teardown) — NOT the bug condition; Startup starts from zero containers and must behave exactly as today (3.10, 3.13).
- **Recovery example (Defect E)**: `greengrass-cli component restart` issued after the old container fully died → Shutdown `down` (nothing to remove), Startup created both containers fresh, backend healthy, HTTP 200 — the behavior the fixed lifecycle must produce automatically.

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
- **(Defect E)** Normal cold start is unchanged: with zero pre-existing project containers, Startup's health-gated `up -d --no-build --wait --wait-timeout 600` behaves exactly as the Defect B fix established, `--force-recreate` being a no-op when nothing exists to recreate, and the wait/verify helpers returning immediately (3.10).
- **(Defect E)** The Defect B health-gate semantics are unchanged: RUNNING still requires all started services to pass their compose healthchecks; the freshness check is an additional gate, never a relaxation (3.11).
- **(Defect E)** All four recipe variants stay in sync: the Shutdown wait, Shutdown Timeout, `--force-recreate`, and freshness verification are applied identically to `recipe-arm64-jp5.yaml`, `recipe-arm64-jp6.yaml`, `recipe-arm64.yaml`, and `recipe-amd64.yaml`; `gdk-config.json` and the root `recipe.yaml` are build artifacts and are never touched (3.12).
- **(Defect E)** Fast teardowns keep Shutdown fast: the wait-for-empty loop exits as soon as `docker compose ps -aq` is empty, adding only one poll interval to the common case (3.13).

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

**Defect E causal chain (verified via docker events on the device; the exploratory tests confirm each structural gap on unfixed code):**

7. **Shutdown is asynchronous with respect to slow teardown**: `docker compose down`'s kill escalation path returns once the kill is delivered and the daemon acknowledges, not once a container stuck in kernel-side GPU/Triton teardown reaches destroyed; and the Shutdown blocks declare no `Timeout`, so Greengrass's 15-second default truncates any longer wait the down might have attempted. Either way, Shutdown exits while the backend is still dying.
8. **Startup adopts instead of recreating**: `docker compose up` treats the still-dying backend (kill delivered, destroy pending, `ps` still listing it as running) as an existing up-to-date service and does not recreate it. Nothing in the Startup line forces recreation.
9. **The `--wait` health gate trusts stale state**: the adopted container's healthcheck state was 'healthy' from before the kill; `--wait` saw "Running / Healthy" and exited 0 immediately — the Defect B gate verifies health but not container freshness, so a container about to vanish passed it.
10. **The destroy lands after RUNNING**: 3 seconds after Startup exited 0, the old backend's stop/die/destroy events completed, leaving zero backend containers behind a component Greengrass had just marked RUNNING. With no attached process and no further lifecycle activity, nothing re-created it (`restart: always` does not apply — the container was destroyed, not stopped).

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

Property 9: Bug Condition - Startup never trusts a previous incarnation's container

_For any_ lifecycle cycle in which a container from the previous incarnation still exists when Startup runs (isBugCondition_E — Shutdown exited during the container's dying window), the fixed lifecycle SHALL NOT report the component RUNNING over that container: Shutdown waits (bounded) after `down` until `docker compose ps -aq` for the project is empty and declares a Timeout sized above worst-case teardown; Startup's compose invocation uses `--force-recreate` so existing containers are never adopted; and after `--wait` succeeds, Startup verifies every project container's StartedAt is at or after the Startup script's start time, exiting non-zero (so Greengrass retries) if any container is stale or the previous incarnation's containers cannot be cleared.

**Validates: Requirements 2.11, 2.12, 2.13, 2.14**

Property 10: Preservation - Cold-start and health-gate semantics unchanged

_For any_ lifecycle cycle where no previous-incarnation container exists when Startup runs (NOT isBugCondition_E — the normal cold start after a completed teardown), the fixed lifecycle SHALL behave identically to the Defect B lifecycle: the same health-gated `up -d --no-build --wait --wait-timeout 600` semantics (RUNNING iff all started services pass their healthchecks), `--force-recreate` a no-op with nothing to recreate, the wait and freshness helpers returning immediately (fast teardowns add at most one poll interval to Shutdown), and all four recipe variants identical to their pre-E form except for the intended Shutdown Timeout, post-down wait, `--force-recreate`, and freshness-verification edits, applied uniformly.

**Validates: Requirements 3.10, 3.11, 3.12, 3.13**

Property 11: Bug Condition - Packaged workflow components are deployable on every targeted device

_For any_ workflow packaged with a non-empty architecture set A (isBugCondition_F when the variants of A are plural): let V = the set of distinct LocalServer variants mapped from A. The fixed `local_server_component_dependencies` SHALL emit exactly one HARD LocalServer entry (that variant, with its per-arch minimum-version floor) when |V| = 1, and SHALL emit zero LocalServer entries when |V| > 1 — so the recipe's dependency closure never contains a LocalServer variant that cannot co-resolve with the device's own variant, and the packaged component is deployable on every device matching any architecture in A. Model and plugin entries are emitted identically in both cases.

**Validates: Requirements 2.15, 2.16, 2.17**

Property 12: Preservation - Single-variant packaging output unchanged

_For any_ workflow packaged with an architecture set collapsing to a single LocalServer variant (NOT isBugCondition_F — including the `x86_64` + `x86_64_nvidia` → `amd64` collapse), the fixed packaging output SHALL be byte-identical to the pre-F output: the same single LocalServer entry (name, version floor, HARD type), the same model and `dda.plugin.*` entries, and every other `build_recipe` field unchanged. In the multi-variant case, all fields other than the removed LocalServer entries SHALL also be unchanged.

**Validates: Requirements 3.14, 3.15, 3.16**

Property 13: Bug Condition - Published vision models resolve and package deployably

_For any_ workflow referencing a vision model whose registry record carries per-target `published_components` entries with status `published` covering every selected architecture's publish target (isBugCondition_G — the plural-only record shape today's gate rejects), the fixed `resolve_model_components` SHALL resolve the model (no PackagingError), and the model dependency emission SHALL emit exactly one unpinned HARD entry when the covered entries collapse to one distinct component name and zero entries when they span several (never a multi-name set into the recipe-global block). A selected architecture with no published entry SHALL fail closed naming the model and the missing architecture.

**Validates: Requirements 2.18, 2.19, 2.20**

Property 14: Preservation - vLLM resolution and other gates unchanged

_For any_ workflow referencing vLLM models (singular `published_component` records), the fixed resolution and dependency output SHALL be byte-identical to today's; records with no registry entry SHALL keep the existing "no record" error; plugin and LocalServer dependency emission SHALL be untouched.

**Validates: Requirements 2.21, 3.17, 3.18, 3.19**

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

#### 5. Synchronous teardown + adoption-proof Startup — Defect E (Requirements 2.11, 2.12, 2.13, 2.14)

Chosen mechanism (from the candidate directions): **both** a synchronous Shutdown **and** a defensive Startup. The Shutdown wait shrinks the race window to near zero in the normal case; the Startup defenses are the correctness gate for anything that still slips through (a Shutdown wait timeout, a hard nucleus kill, a crashed Shutdown). `--force-recreate` is preferred over a Startup-side wait-for-zero because a blanket wait would break legitimate cold-boot cases where `restart: always` has already relaunched containers before Greengrass starts; recreation converts every pre-existing container — dying, stopped, or running — into a deterministic fresh one, and is a no-op in the normal flow where Shutdown's `down` already removed everything.

**File**: `src/host_scripts/compose_lifecycle.sh` (new; shipped into every component artifact by the existing `build-custom.sh` `cp -r src/host_scripts` step, alongside `setup_paths.sh` etc.; host_scripts are not security-baseline-tracked, and `gdk-config.json`/root `recipe.yaml` are build artifacts that are never touched)

**Specific Changes**:

1. **`wait-empty` subcommand**: `compose_lifecycle.sh wait-empty <timeout-seconds> -- <docker compose args...>` polls `docker compose <args> ps -aq` every 2 seconds until the output is empty; exits 0 when empty (immediately if already empty — the fast-teardown/cold-start case, 3.13), exits 1 with a diagnostic listing the surviving container IDs when the bounded timeout elapses. `docker` is resolved via PATH, so the script is a pure function of stubbed CLI output in tests.

2. **`verify-fresh` subcommand**: `compose_lifecycle.sh verify-fresh <since-epoch> -- <docker compose args...>` lists `docker compose <args> ps -q` and, for each container, compares `docker inspect -f '{{.State.StartedAt}}'` (parsed to epoch via `date -d`) against `<since-epoch>`; exits 0 iff every project container started at or after the reference time, exits 1 naming any stale container. This is the belt-and-braces gate behind `--force-recreate`: with recreation in force it always passes, and it catches any residual adoption path (e.g. a future compose behavior change) before Greengrass can see RUNNING (2.13).

**Files**: `recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml` (all four variants, identical shape — 3.12; the retired bare `.arm64` variant is included to keep the set in sync, matching the Defect B treatment)

3. **Shutdown — synchronous teardown (2.11)**: add `Timeout: 300` to each Shutdown block (Greengrass's 15s default truncates any teardown wait; 300s covers the 120s stop grace period plus the observed ~24s post-kill dying window with ample margin), and after the existing `docker compose ... down` line add:

```
bash .../host_scripts/compose_lifecycle.sh wait-empty 240 -- --profile ${DOCKER_PROFILE:-tegra} -f .../docker-compose.yaml || true
```

   Best-effort (`|| true`): a Shutdown that times out waiting must not wedge the lifecycle — the Startup defenses below are the authoritative gate. 240s sits inside the 300s block Timeout. The `systemctl stop nvidia-csi-capture` line (arm variants) and the `/tmp/.dda.env` export are unchanged.

4. **Startup — never adopt (2.12)**: record the script's start time as the first Startup line (`STARTUP_EPOCH=$(date +%s)`), and add `--force-recreate` to the compose invocation:

```
docker compose --profile $DOCKER_PROFILE -f .../docker-compose.yaml up -d --no-build --force-recreate --wait --wait-timeout 600
```

   Recreation removes any pre-existing project container before creating the new one. For a container still in its dying window, the removal blocks until the kernel teardown completes (converting the race into a bounded wait) or fails with a removal-in-progress error — in which case `compose up` exits non-zero, Startup errors, and Greengrass retries (2.14): no silent adoption on any path. In the normal flow Shutdown's `down` (now synchronous) has already removed everything, so `--force-recreate` changes nothing (3.10). The existing `Timeout: 900` on the Startup block already accommodates the added worst-case wait.

5. **Startup — freshness gate (2.13)**: after the compose up line succeeds, append:

```
bash .../host_scripts/compose_lifecycle.sh verify-fresh $STARTUP_EPOCH -- --profile $DOCKER_PROFILE -f .../docker-compose.yaml
```

   Not best-effort: a non-zero exit fails Startup so Greengrass retries rather than reporting RUNNING over a container this Startup did not create. Placed after `--wait` so it evaluates the final set of started containers.

6. **Untouched**: `src/docker-compose.yaml` (no new keys — no security-baseline rebaseline needed for Defect E), the `/health` endpoint and `healthcheck.py`, the Install blocks, `SetEnv`, host setup script invocations, and the `workflow_packaging.py`/`vllm_model_prep.py` fixes from Defects C/D.

#### 7. Multi-variant LocalServer dependency fix — Defect F (Requirements 2.15, 2.16, 2.17)

**Location**: `edge-cv-portal/backend/functions/workflow_packaging.py::local_server_component_dependencies`

The Defect C design documented the recipe-global ComponentDependencies constraint (§6 above) and relied on upstream deployment arch gates as mitigation; the verified f81a4c66 incident (deployment 44f2c596, ryan-orin-nano, `FAILED_ROLLBACK_COMPLETE`) proved the mitigation does not cover the dependency closure — Greengrass installs every HARD dependency in the recipe regardless of the deployment document's component list.

Change `local_server_component_dependencies(archs)`:

1. Map the selected archs to distinct LocalServer variants exactly as today (fail-closed naming, `x86_64`/`x86_64_nvidia` collapse to `amd64`).
2. If exactly one distinct variant results: return the single entry unchanged (same name, per-arch minimum-version floor via `min_local_server_version_for`, HARD) — the Defect C behavior, byte-identical (Property 12). When multiple archs collapse to one variant, the version floor is the maximum of the per-arch floors (today's amd64 collapse already resolves to a single floor).
3. If more than one distinct variant results: return `{}` and log a warning naming the workflow's variants (e.g. "workflow packaged for multiple LocalServer variants [arm64JP5, arm64JP6]; omitting LocalServer ComponentDependencies — deployability takes precedence over the ordering/health edge"). Model and plugin dependencies are unaffected; they still provide the transitive LocalServer health edge on devices where the model components themselves depend on LocalServer.
4. Update the function docstring: replace the multi-variant caveat with the new single-variant-only emission rule.

The merge site in the packaging handler and `build_recipe` are unchanged.

**Rationale for omission over alternatives**: SOFT dependencies still install the component (same failure); per-arch workflow component names change the component naming contract portal-wide (out of scope for a bugfix); rejecting multi-arch packaging at validation time removes an existing user capability. Omission preserves deployability and loses only the LocalServer ordering edge in the multi-variant case — which is partially restored transitively through the model components' own LocalServer dependencies.

#### 8. Vision model resolution fix — Defect G (Requirements 2.18–2.21)

**Location**: `edge-cv-portal/backend/functions/workflow_packaging.py` — `resolve_model_components` and the model dependency call path in the packaging handler.

Registry field shapes (verified against dda-portal-training-jobs):
- vLLM records: `published_component` (singular map, `component_name` = `model-vllm-...`) — today's only supported shape.
- Vision records: `published_components` (plural LIST of `{component_name, target, component_version, status, platform, component_arn}`), one entry per publish target (e.g. `jetson-xavier-jp5`, `jetson-xavier-jp6`), no singular field.

Changes:

1. **Arch→publish-target mapping**: reuse/mirror the existing target naming discipline (`greengrass_publish.py` target names: `arm64_jp4 → jetson-xavier-jp4`-style; confirm exact mapping constants from greengrass_publish/TARGET naming and deployments.py's arch↔target translation) as a module map in workflow_packaging.
2. **`resolve_model_components(model_names, usecase, archs)`** (signature gains the selected archs): per record, first try `published_component` (singular — vLLM path, unchanged); else filter `published_components` to entries with `status == 'published'` whose `target` matches a selected architecture's publish target. Every selected arch must be covered by ≥1 entry, else PackagingError naming the model and the uncovered arch (2.19). Resolved value carries the covered entries' distinct `component_name` set.
3. **Model dependency emission**: for singular-resolved records, emit as today. For plural-resolved records: |distinct names| == 1 → one unpinned HARD entry; > 1 → omit all entries for that model and log a warning naming the model and the per-target names (the Defect F discipline; the transitive LocalServer/health edges still exist via each per-target component's own dependencies).
4. The misleading "publish the model" message survives only for records with a singular field lacking `component_name` AND no plural entries at all — i.e. genuinely unpublished models.

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
6. **Teardown-race lifecycle exposure (Defect E)**: parse all four recipe variants; assert each Shutdown block declares a `Timeout` and invokes the compose-lifecycle `wait-empty` helper after `down`, and each Startup uses `--force-recreate` and invokes `verify-fresh` after the health gate — fails on unfixed recipes (no Shutdown Timeout, so Greengrass's 15s default truncates any wait; bare `down`; adoption-permitting `up`; no freshness gate) (will fail on unfixed code)
7. **Missing helper exposure (Defect E)**: assert `src/host_scripts/compose_lifecycle.sh` exists and, with a stubbed `docker` on PATH simulating the incident's dying window (`ps -aq` reporting the backend container for several polls before emptying; `inspect` reporting a StartedAt older than the reference epoch), that `wait-empty` blocks until empty and `verify-fresh` rejects the stale container — fails on unfixed code (the helper does not exist) (will fail on unfixed code)

**Expected Counterexamples**:
- Absent `stop_grace_period`/`restart: always`/healthchecks; attached Run lifecycle; ComponentDependencies without model/LocalServer entries; the literal generic message; an unbounded shutdown handler
- Possible causes all confirmed by incident evidence: Docker stop-timeout kill, restart-policy semantics for docker-stopped containers, recipe-global RUNNING-on-attach, dependency emission limited to `dda.plugin.*`
- (Defect E) Shutdown blocks with no Timeout and no post-down wait; Startup compose lines without `--force-recreate` or freshness verification; no compose-lifecycle helper in host_scripts — the structural gaps behind the verified adoption-of-a-dying-container event sequence

### Fix Checking

**Goal**: Verify that for all inputs where a bug condition holds, the fixed artifacts produce the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition_X(input) DO      -- X ∈ {A, B, C, D}
  result := fixedArtifact(input)
  ASSERT expectedBehavior_X(result)                 -- Properties 1–4
END FOR
```

Concretely: config tests assert Properties 1–2 over the fixed YAML (grace period ≥ 120s, `restart: always`, healthchecks present with sane parameters, every recipe variant's Startup uses `up -d --wait` with a Timeout, Shutdown unchanged, all four variants consistent); packaging tests assert Property 3 over generated recipes; prep-script tests assert Property 4 over mocked connection-failure sequences; the bounded-shutdown test asserts the handler returns within budget when cleanup blocks forever. For Property 9 (Defect E): config tests assert every variant's Shutdown declares a Timeout ≥ the wait-empty bound and invokes `wait-empty` after `down`, and every Startup records its start epoch, uses `--force-recreate`, and gates on `verify-fresh`; helper behavior tests with a stubbed `docker` assert `wait-empty` waits through a simulated dying window and times out non-zero when containers never clear, and `verify-fresh` exits non-zero for any container whose StartedAt predates the reference epoch (the incident's adopted-container shape).

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
5. **Recipe equality modulo the Defect E edits (Property 10)**: observe the current (post-Defect-B, pre-E) structure of all four recipe variants; verify the fixed recipes are deep-equal after removing only the Shutdown `Timeout` key, the `wait-empty` invocation line, the `STARTUP_EPOCH` line, the `--force-recreate` flag, and the `verify-fresh` line — Install, SetEnv, host setup scripts, the `--wait --wait-timeout 600` health gate, the `down` command, and the csi stop all byte-identical; `src/docker-compose.yaml` untouched by Defect E (byte-identical)
6. **Fast-path helper behavior (Property 10)**: with a stubbed `docker` reporting zero project containers, `wait-empty` returns 0 within one poll interval and `verify-fresh` returns 0 with nothing to check — the cold-start no-op guarantee

### Unit Tests

- `vllm_model_prep.request_load` classification: all-connection-refused → `LOAD_UNREACHABLE`; refused-then-HTTP-409 → `LOAD_HTTP_ERROR` with existing message; refused-then-200 → `LOAD_OK` (mocked `requests`/`wait_for_server`, stubbed sleeps)
- `prepare` terminal messages per classification; exit codes unchanged (always 1 on failure, 0 on success)
- `gather_model_references`: model_inference-only, llm_inference-only, mixed, duplicates deduplicated, no-model workflows → empty
- `resolve_model_components` fail-closed on unpublished records (PackagingError naming the model)
- `local_server_component_dependencies`: each arch id maps to its exact variant name; `x86_64`+`x86_64_nvidia` collapse to one `amd64` entry; per-arch version floors applied
- `/health` endpoint: 200 with no vLLM server set; 200 with vLLM set and 8901 accepting; 503 with vLLM set and 8901 refused (loopback stub server)
- Bounded shutdown handler: blocking cleanup → returns within budget and logs the warning; fast cleanup → both calls executed in order
- `compose_lifecycle.sh wait-empty` (stubbed `docker`): already-empty → immediate 0; empties after N polls → 0 after ~2N seconds; never empties → 1 at the bound with surviving IDs in the diagnostic
- `compose_lifecycle.sh verify-fresh` (stubbed `docker`): all containers fresh → 0; any container with StartedAt before the epoch → 1 naming it; zero containers → 0; malformed inspect output → non-zero (fail closed)

### Property-Based Tests

- _For any_ generated workflow definition (random model-ref sets, plugin sets, arch subsets of DEVICE_ARCHITECTURES): the fixed recipe contains a HARD entry per distinct published model component and per distinct LocalServer variant, preserves all plugin entries exactly, and equals the unfixed recipe outside ComponentDependencies (Properties 3, 7)
- _For any_ random attempt-outcome sequence over {connection-error, http-error(code), success}: the classification is UNREACHABLE iff no HTTP response appears in the sequence prefix consumed by the loop, and the emitted message matches the classification (Properties 4, 8)
- _For any_ random cleanup duration: the shutdown handler's runtime is ≤ budget + ε, and cleanup effects occur iff duration ≤ budget (Properties 1, 5)
- _For any_ random dying-window length (stubbed `docker` emptying after k polls, k drawn across and beyond the timeout bound): `wait-empty` exits 0 iff the window clears within the bound and 1 otherwise, never exiting 0 with containers remaining (Property 9)
- _For any_ random set of container StartedAt timestamps relative to a reference epoch: `verify-fresh` exits 0 iff every timestamp ≥ epoch (Properties 9, 10)

### Integration Tests

- **On-hardware JP6 gate (final)**: build and publish the modified LocalServer component (gdk) and portal packaging changes; deploy to the JP6 device; while a workflow with an `llm_inference` node is running, trigger a deployment restart of LocalServer and verify: the backend is never SIGKILLed (no exit 137), or if killed is recovered by the Startup retry; Greengrass reports LocalServer RUNNING only after `docker compose ps` shows the backend healthy; `model-vllm-opt125m-smoke` never goes BROKEN; the deployment completes without `FAILED_UNABLE_TO_ROLLBACK`
- **Dead-backend truthfulness**: on the device, `docker stop` the backend and verify the component's next lifecycle cycle fails Startup (not silent RUNNING) and the retried Startup brings the stopped container back up
- **Recipe dependency ordering**: deploy a freshly packaged workflow component and verify in the Greengrass logs that Greengrass orders it after its model component and LocalServer (HARD edges visible in dependency resolution)
- **On-hardware Defect E gate (rides the next LocalServer build; user-gated)**: on the JP6 device (ryan-orin-nano), reproduce the incident trigger — device reboot + nucleus restart (and a `greengrass-cli component restart` while the backend is mid-teardown) — and verify via `docker events` that the backend container is recreated (create/start events from this Startup), never adopted; `docker ps -a` shows both containers after RUNNING is reported; the portal answers HTTP 200; and a forced wait-empty timeout (simulated stuck teardown) fails Startup non-zero with a Greengrass retry rather than silent RUNNING
