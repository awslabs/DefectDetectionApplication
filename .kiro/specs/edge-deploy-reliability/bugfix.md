# Bugfix Requirements Document

## Introduction

A Greengrass deployment that added a workflow component (`dda.workflow.*`) to a JP6 device running `aws.edgeml.dda.LocalServer.arm64JP6` v1.0.43 failed with `FAILED_UNABLE_TO_ROLLBACK`, leaving the device without a working backend. The root cause is a restart race in the LocalServer docker-compose lifecycle: the deployment restarted the compose stack while the backend was mid-shutdown ("Cleaning up digital input workflows"), the backend exceeded Docker's default 10-second stop grace period and was SIGKILLed (ExitCode 137, Docker stop-timeout kill — not the kernel OOM killer), and the subsequent `compose up` never brought it back. The `restart: unless-stopped` policy in `src/docker-compose.yaml` does not apply to docker-stopped containers, so the backend container stayed `Exited(137)` permanently.

Three compounding gaps turned this race into a full deployment failure with poor diagnostics:

1. **RUNNING ≠ healthy**: the LocalServer component's Run lifecycle (`docker compose up --no-build`, attached) stayed alive serving only the frontend, so Greengrass reported LocalServer as RUNNING while its backend — including the vLLM runtime on 127.0.0.1:8901 — was dead. This defeated the HARD dependency that `model-vllm-opt125m-smoke` already declares on LocalServer.
2. **Missing workflow dependencies**: generated `dda.workflow.*` recipes (built by `edge-cv-portal/backend/functions/workflow_packaging.py::build_recipe`) declare no ComponentDependencies on the model components they use or on the target-arch LocalServer component, so Greengrass has no ordering or health relationship between them.
3. **Generic failure diagnostics**: the model prep script (`src/backend/dda_triton/vllm_model_prep.py`) retried the load 5 times against a dead runtime (~70s of Connection refused), then exited 1 with a generic retry-exhausted message that never named the actual cause (LocalServer backend container down).

This fix addresses the compose restart race (root cause) and hardens the three surrounding layers: health-gated Greengrass lifecycle, generated workflow component dependencies, and actionable failure diagnostics.

**Defect E (added after on-hardware verification of the A–D fixes)**: a second, fully verified incident on `aws.edgeml.dda.LocalServer.arm64JP6` v1.0.46 (device ryan-orin-nano) revealed a Shutdown/Startup teardown race that the health-gated lifecycle does not close. After a device reboot + nucleus restart, Greengrass ran the component Shutdown (`docker compose down`/kill: both containers received `kill` at t=1785789983) and then Startup. The frontend died and was destroyed within 1 second and Startup's `docker compose up` recreated it (t=1785790004–5). But the backend (`backend_tegra_gpu_enabled`) takes ~24 seconds to die after SIGKILL escalation (GPU/Triton teardown), so Startup's compose saw the still-dying backend as an existing running container, reported "Container ...backend_tegra_gpu_enabled-1 Running / Healthy" (the `--wait` health gate trusted the container's stale pre-kill 'healthy' healthcheck state) and exited 0. Three seconds later (t=1785790007) the old backend finished dying (stop/die/destroy events) — leaving NO backend container at all (`docker ps -a` showed only the frontend) while Greengrass reported the component RUNNING and the portal/API was dead. Manual recovery worked: `greengrass-cli component restart` once the old container was fully gone recreated both containers healthy (backend answering HTTP 200). The fix makes Shutdown synchronous (bounded wait for zero project containers) and Startup defensive (never adopt a previous incarnation's container; verify container freshness before trusting the health gate).

**Defect F (added after a verified multi-arch packaging incident)**: the Defect C fix emits one HARD LocalServer ComponentDependencies entry per selected architecture into the single recipe-global dependency block. Greengrass ComponentDependencies is not per-platform-manifest, so a workflow packaged for architectures spanning distinct LocalServer variants produces a component that is undeployable on ANY single device. Verified incident: `dda.workflow.f81a4c66-...` v1.0.0 was packaged for both `arm64_jp5` and `arm64_jp6`, its recipe carried HARD deps on both `aws.edgeml.dda.LocalServer.arm64JP5` and `...arm64JP6`, and deployment 44f2c596 to the JP6 device ryan-orin-nano failed `FAILED_ROLLBACK_COMPLETE: Service aws.edgeml.dda.LocalServer.arm64JP5 in broken state after deployment` — even though the deployment document itself listed only arm64JP6 components, Greengrass resolved the recipe's dependency closure and installed the JP5 variant on the JP6 device. The design's documented mitigation ("the deployment service gates components against device architecture") does not cover the dependency closure. The fix: emit the LocalServer entry only when the selected architectures collapse to exactly one distinct LocalServer variant; when they span multiple variants, omit all LocalServer entries (model and plugin dependencies are unaffected either way).

**Defect G (verified packaging regression from the Defect C fix)**: `resolve_model_components` reads only the `published_component` (SINGULAR) registry field — the shape `greengrass_publish.py` writes for vLLM records. Vision model records carry `published_components` (PLURAL): a per-target list of `{component_name, target, component_version, status, ...}` entries (verified: `yolo_test` has `model-yolo-test-jetson-xavier-jp5` and `...-jp6` at 6.0.0, both status `published`, and NO singular field). The fail-closed gate therefore rejects EVERY workflow referencing a published vision model with "Model '...' has no published Greengrass component; publish the model before packaging" (verified in the portal packaging dialog for `yolo_test`, archs arm64_jp5+arm64_jp6). Additionally, vision components are per-target NAMED, so emitting model dependencies for a multi-arch package has the same recipe-global co-resolution problem Defect F fixed for LocalServer. Fix: resolve vision records through `published_components` filtered to the selected architectures' publish targets (fail closed only when a selected architecture has no published entry), and emit the model HARD entry only when the covered entries collapse to one distinct component name — omitting (with a log) when they span multiple, mirroring Defect F.

## Bug Analysis

### Current Behavior (Defect)

**Compose restart race (root cause)**

1.1 WHEN a Greengrass deployment restarts the LocalServer component while the backend container is performing shutdown cleanup that exceeds Docker's default 10-second stop grace period THEN the system SIGKILLs the backend container (ExitCode 137) mid-shutdown

1.2 WHEN the backend container is killed by a Docker stop-timeout during a compose restart THEN the system leaves the container in `Exited(137)` permanently — the subsequent `docker compose up` does not restart it and the `restart: unless-stopped` policy does not apply to docker-stopped containers

1.3 WHEN the backend container receives SIGTERM during a deployment restart THEN the system performs full non-essential shutdown cleanup (e.g. digital input workflow cleanup) inline, taking longer than the stop grace window

**Greengrass RUNNING ≠ backend healthy**

1.4 WHEN the backend container is dead but the frontend container is running THEN the system reports the LocalServer component as RUNNING in Greengrass, because the attached `docker compose up` Run script stays alive as long as any service runs

1.5 WHEN a component declares a HARD dependency on the LocalServer component THEN the system satisfies that dependency based on the RUNNING state alone, allowing dependent components to start against a dead backend and dead vLLM runtime (127.0.0.1:8901 unreachable)

1.6 WHEN the compose services are started by the Run lifecycle THEN the system defines no docker healthchecks for the backend or frontend services, so neither Docker nor the Greengrass lifecycle can observe backend health

**Missing workflow component dependencies**

1.7 WHEN `workflow_packaging.py` generates a `dda.workflow.*` recipe THEN the system emits no ComponentDependencies on the model component(s) the workflow uses or on the target-architecture LocalServer component (only custom Plugin_Component dependencies are emitted, when present)

1.8 WHEN a deployment adds a workflow component to a device THEN the system gives Greengrass no ordering or dependency relationship between the workflow, its model components, and LocalServer, allowing the deployment to proceed and fail in ways attributed to unrelated components

**Generic failure diagnostics**

1.9 WHEN the vLLM runtime at 127.0.0.1:8901 is unreachable for the full retry window (5 attempts, ~70 seconds of connection refused) THEN the system exits with a generic "load request did not succeed; exiting non-zero so the component retries" message that does not identify the likely cause, and after three Greengrass restarts the model component is marked BROKEN and the deployment fails as `FAILED_UNABLE_TO_ROLLBACK`

**Shutdown/Startup teardown race (Defect E)**

1.10 WHEN the component Shutdown runs `docker compose down` and a container's post-SIGKILL teardown outlasts the Shutdown script (the backend takes ~24 seconds to die after kill escalation due to GPU/Triton teardown, and the Shutdown block declares no Timeout — Greengrass's 15-second default truncates any wait) THEN the system exits Shutdown while a project container from the previous incarnation still exists and is still dying

1.11 WHEN Startup's `docker compose up -d --wait` runs while a container from the previous incarnation is still in its dying window THEN the system adopts the dying container as an existing running service instead of recreating it

1.12 WHEN the `--wait` health gate evaluates an adopted dying container THEN the system trusts the container's stale pre-kill 'healthy' healthcheck state, reports it "Running / Healthy", and Startup exits 0

1.13 WHEN the adopted container finishes dying after Startup has exited 0 THEN the system leaves no backend container at all (`docker ps -a` shows only the frontend) while Greengrass reports the component RUNNING and the portal/API is dead

**Multi-variant LocalServer dependencies make the component undeployable (Defect F)**

1.14 WHEN `workflow_packaging.py` packages a workflow for multiple architectures that map to distinct LocalServer variants (e.g. `arm64_jp5` + `arm64_jp6`) THEN the system emits a HARD ComponentDependencies entry for every variant into the single recipe-global dependency block

1.15 WHEN such a component is deployed to any single device THEN Greengrass resolves the full recipe dependency closure — regardless of which components the deployment document lists — and attempts to install a LocalServer variant that does not match the device, which enters a broken state and fails the whole deployment (verified: `FAILED_ROLLBACK_COMPLETE` on ryan-orin-nano)

1.16 WHEN the upstream deployment service applies its device-architecture gates THEN the system does not inspect or gate the workflow recipe's dependency closure, so the documented Defect C mitigation never engages for this failure mode

**Vision model packaging regression (Defect G)**

1.17 WHEN a workflow references a vision model whose registry record carries per-target `published_components` (plural) but no `published_component` (singular) THEN `resolve_model_components` raises PackagingError "no published Greengrass component" and packaging fails — even though the model is published for every selected architecture

1.18 WHEN the misleading error directs the user to "publish the model" THEN re-publishing does not help — the vision publish flow writes the plural field, so the gate can never pass for vision models

1.19 WHEN a vision model's per-target components have distinct names per architecture THEN emitting one HARD entry per target into the recipe-global ComponentDependencies would make a multi-arch package undeployable on any single device (the Defect F failure shape, for model components)

### Expected Behavior (Correct)

**Compose restart race (root cause)**

2.1 WHEN a Greengrass deployment restarts the LocalServer component while the backend container is shutting down THEN the system SHALL allow the backend a stop grace period (`stop_grace_period`) longer than its worst-case graceful shutdown, so it is never SIGKILLed mid-cleanup during a normal deployment restart

2.2 WHEN the backend container is killed during or after a compose restart for any reason THEN the system SHALL recover it automatically via a `restart: always` policy, so a deployment can never leave a dead backend behind

2.3 WHEN the backend container receives SIGTERM THEN the system SHALL complete shutdown fast enough to fit within the stop grace window by deferring or shortening non-essential cleanup work

**Health-gated Greengrass lifecycle**

2.4 WHEN the LocalServer component starts its compose services THEN the system SHALL only report the component as RUNNING once the backend container is actually healthy (e.g. `docker compose up -d --wait` in a Startup script with the Run script monitoring, or equivalent), so HARD dependencies gate on real backend health

2.5 WHEN the backend service is defined in docker-compose THEN the system SHALL declare a docker healthcheck that verifies backend health, including vLLM runtime reachability on 127.0.0.1:8901 where the active profile includes the vLLM runtime

2.6 WHEN the frontend service is defined in docker-compose THEN the system SHALL declare a basic docker healthcheck

2.7 WHEN the LocalServer component's Shutdown and Run/Startup lifecycle scripts complete THEN the system SHALL never leave a stopped backend container behind that a subsequent `compose up` will not restart

**Workflow component dependencies**

2.8 WHEN `workflow_packaging.py` generates a `dda.workflow.*` recipe THEN the system SHALL emit ComponentDependencies including a HARD dependency on each model component the workflow uses

2.9 WHEN `workflow_packaging.py` generates a `dda.workflow.*` recipe THEN the system SHALL emit a ComponentDependencies entry on the LocalServer component matching each target architecture

**Clear failure diagnostics**

2.10 WHEN the vLLM runtime at 127.0.0.1:8901 is unreachable for the full retry window THEN the system SHALL fail with an actionable error message naming the likely cause — the LocalServer backend container being down — and suggesting how to verify it, rather than a generic retry-exhausted message

**Synchronous teardown and adoption-proof Startup (Defect E)**

2.11 WHEN the component Shutdown runs `docker compose down` THEN the system SHALL wait, with a bounded timeout, until `docker compose ps` for the project reports zero containers (all containers fully destroyed) before Shutdown exits, and the Shutdown block SHALL declare a Timeout sized above the worst-case teardown so Greengrass does not truncate the wait

2.12 WHEN Startup's compose up runs while any container from a previous incarnation exists (running, dying, or stopped) THEN the system SHALL recreate the project containers rather than adopt them, so the health gate never evaluates a previous incarnation's container

2.13 WHEN the Startup health gate passes THEN the system SHALL have verified that each project container was created by this Startup invocation (its StartedAt is newer than the Startup script's start time) before reporting the component RUNNING

2.14 WHEN a previous incarnation's containers cannot be cleared within the bounded wait window during Startup THEN the system SHALL exit Startup non-zero so Greengrass retries the lifecycle, rather than reporting RUNNING over an adopted dying container

**Deployable single-variant LocalServer dependency (Defect F)**

2.15 WHEN the selected architectures map to exactly one distinct LocalServer variant (including `x86_64` + `x86_64_nvidia`, which collapse to the single `amd64` variant) THEN the system SHALL emit that single HARD LocalServer entry with its per-arch minimum-version floor, exactly as the Defect C fix does today

2.16 WHEN the selected architectures map to more than one distinct LocalServer variant THEN the system SHALL omit ALL LocalServer entries from ComponentDependencies, so the packaged component remains deployable on each targeted device (model and plugin dependency entries are still emitted unchanged)

2.17 WHEN LocalServer entries are omitted because the selection spans multiple variants THEN the system SHALL log the omission (naming the variants involved) so the reduced dependency protection is observable in the packaging logs

**Vision model resolution and deployable model dependencies (Defect G)**

2.18 WHEN a workflow references a vision model and the selected architectures each have a `published_components` entry with status `published` (matching the arch's publish target, e.g. `arm64_jp5` → `jetson-xavier-jp5`) THEN the system SHALL resolve the model through those entries and packaging SHALL proceed

2.19 WHEN a selected architecture has NO published entry for a referenced vision model THEN the system SHALL fail closed with a PackagingError naming the model AND the missing architecture/target (an accurate message, unlike today's misleading one)

2.20 WHEN the resolved entries for the selected architectures collapse to exactly one distinct component name THEN the system SHALL emit that single unpinned HARD model dependency (the Defect C behavior); WHEN they span multiple distinct component names THEN the system SHALL omit the model's ComponentDependencies entries and log the omission (the Defect F discipline applied to model components)

2.21 WHEN a workflow references a vLLM model whose record carries `published_component` (singular) THEN the system SHALL CONTINUE TO resolve it exactly as today

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the backend shuts down quickly within the grace window during a normal deployment THEN the system SHALL CONTINUE TO complete the compose down/up cycle cleanly and bring up all services

3.2 WHEN existing model components (Triton/LFV and vLLM) start against a healthy LocalServer backend THEN the system SHALL CONTINUE TO stage and load their models successfully

3.3 WHEN previously deployed workflow components are running on a device THEN the system SHALL CONTINUE TO execute them without disruption from the recipe or compose changes

3.4 WHEN the LocalServer component selects a compose profile and architecture (tegra/generic, JP4/JP5/JP6/x86) THEN the system SHALL CONTINUE TO use the existing profile and arch selection logic unchanged

3.5 WHEN the shared `src/docker-compose.yaml` is used by JP5 and x86 recipe variants THEN the system SHALL CONTINUE TO work consistently across those variants

3.6 WHEN the backend process crashes on its own (e.g. the AWS CRT event-stream SIGABRT the current restart policy guards against) THEN the system SHALL CONTINUE TO auto-recover the backend container via its restart policy

3.7 WHEN components are versioned and built through the existing Greengrass build pipeline (gdk) THEN the system SHALL CONTINUE TO build and publish unchanged except for the intended recipe and compose modifications

3.8 WHEN `workflow_packaging.py` packages a workflow using Custom_Node_Type plugins THEN the system SHALL CONTINUE TO emit the existing `dda.plugin.*` ComponentDependencies entries alongside the new model and LocalServer dependencies

3.9 WHEN the vLLM model prep script encounters a repository validation defect, unresolvable weights path, or an authoritative HTTP error from the runtime THEN the system SHALL CONTINUE TO fail with the existing specific error messages for those causes

3.10 WHEN the component starts cold with no pre-existing project containers (the normal deployment flow after a completed Shutdown) THEN the system SHALL CONTINUE TO bring up all services via the existing health-gated `docker compose up -d --no-build --wait` Startup with no material delay added by the teardown-wait logic

3.11 WHEN the Startup health gate evaluates freshly created containers THEN the system SHALL CONTINUE TO report the component RUNNING only when all started services pass their compose healthchecks (the Defect B health-gated Startup semantics are unchanged)

3.12 WHEN any of the four LocalServer recipe variants (JP4/JP5/JP6/amd64) is built THEN the system SHALL CONTINUE TO share the same lifecycle shape — the Defect E changes SHALL be applied identically to all variants

3.13 WHEN Shutdown runs and the project containers tear down promptly (the fast-teardown case) THEN the system SHALL CONTINUE TO complete Shutdown quickly, the bounded wait returning as soon as zero containers remain

3.14 WHEN a workflow is packaged for a single architecture (or any set collapsing to one LocalServer variant) THEN the system SHALL CONTINUE TO emit the identical LocalServer ComponentDependencies entry the Defect C fix emits today (same name, same per-arch minimum-version floor, HARD type)

3.15 WHEN a workflow is packaged for any architecture selection THEN the system SHALL CONTINUE TO emit the model component and `dda.plugin.*` ComponentDependencies entries unchanged — the Defect F fix touches only the LocalServer entries

3.16 WHEN `build_recipe` produces the packaged recipe THEN the system SHALL CONTINUE TO produce output identical to the pre-F output in every field except the LocalServer ComponentDependencies entries in the multi-variant case

3.17 WHEN a workflow references only vLLM models (singular `published_component` records) THEN the system SHALL CONTINUE TO produce byte-identical resolution and dependency output to today

3.18 WHEN a referenced model has no registry record at all THEN the system SHALL CONTINUE TO fail closed with the existing "no record in the Use_Case model registry" error

3.19 WHEN plugin and LocalServer dependency emission run THEN they SHALL CONTINUE TO behave identically — Defect G touches only model resolution (`resolve_model_components`) and model dependency emission (`model_component_dependencies` call path)
