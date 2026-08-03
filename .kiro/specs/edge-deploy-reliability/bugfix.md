# Bugfix Requirements Document

## Introduction

A Greengrass deployment that added a workflow component (`dda.workflow.*`) to a JP6 device running `aws.edgeml.dda.LocalServer.arm64JP6` v1.0.43 failed with `FAILED_UNABLE_TO_ROLLBACK`, leaving the device without a working backend. The root cause is a restart race in the LocalServer docker-compose lifecycle: the deployment restarted the compose stack while the backend was mid-shutdown ("Cleaning up digital input workflows"), the backend exceeded Docker's default 10-second stop grace period and was SIGKILLed (ExitCode 137, Docker stop-timeout kill — not the kernel OOM killer), and the subsequent `compose up` never brought it back. The `restart: unless-stopped` policy in `src/docker-compose.yaml` does not apply to docker-stopped containers, so the backend container stayed `Exited(137)` permanently.

Three compounding gaps turned this race into a full deployment failure with poor diagnostics:

1. **RUNNING ≠ healthy**: the LocalServer component's Run lifecycle (`docker compose up --no-build`, attached) stayed alive serving only the frontend, so Greengrass reported LocalServer as RUNNING while its backend — including the vLLM runtime on 127.0.0.1:8901 — was dead. This defeated the HARD dependency that `model-vllm-opt125m-smoke` already declares on LocalServer.
2. **Missing workflow dependencies**: generated `dda.workflow.*` recipes (built by `edge-cv-portal/backend/functions/workflow_packaging.py::build_recipe`) declare no ComponentDependencies on the model components they use or on the target-arch LocalServer component, so Greengrass has no ordering or health relationship between them.
3. **Generic failure diagnostics**: the model prep script (`src/backend/dda_triton/vllm_model_prep.py`) retried the load 5 times against a dead runtime (~70s of Connection refused), then exited 1 with a generic retry-exhausted message that never named the actual cause (LocalServer backend container down).

This fix addresses the compose restart race (root cause) and hardens the three surrounding layers: health-gated Greengrass lifecycle, generated workflow component dependencies, and actionable failure diagnostics.

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
