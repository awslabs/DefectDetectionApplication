# Bugfix Requirements Document

## Introduction

**Incident (2026-08-16):** Build-fleet JP7 job `e1d672ce-7d49-4bc2-99c7-83270a4dd353` on dedicated server `srv-9dea6a61` (`i-092e45480d30c89c4`, 192 GB disk) failed with `RUNNER_DISK_FULL`: docker layer extraction hit `no space left on device` writing `libonnxruntime_providers_cuda.so` while unpacking the `backend_tegra_gpu_enabled` image (docker storage under `/var/snap/docker/common`, snap docker), and the agent's `tee` to `/tmp` also hit ENOSPC. The agent's disk preflight had PASSED with only 29 GB free (162/192 GB used): the preflight (`record_disk_capacity` in `scripts/portal-build-agent.sh`) is measurement-only evidence — `PREFLIGHT_MIN_DISK_GB` is unset in production, so any nonzero headroom passes. The build then ran ~1 hour before dying mid-extraction, wasting the GPU compile time and requiring a manual requeue (job `998b6f42-84f0-4f35-8536-fedeed115b25`).

**Root cause: unbounded accumulation across builds.** Each ECR-path publish in `portal-build.sh` runs `docker tag flask-app:latest ${ECR_REPO}:${COMPONENT_VERSION}` (same for react-webapp) and pushes — but never removes the local per-version tag, so every publish leaves a locally-tagged per-version image generation behind (observed at incident time: `dda/flask-app` 1.0.0/1.0.3/1.0.4/1.0.5 at ~29–42 GB each plus `dda/react-webapp` × 4 — ~74 GB of stale generations, all safely stored in ECR). Old `/tmp` build logs (`gdk-build-*.log`, `gdk-publish-*.log`, `portal-build-agent-*.log`, `inference-uploader-build-*.log` — all timestamped/job-suffixed, never reused) accumulate alongside. The ONLY between-build cleanup is `portal-build.sh` step [3/7]: `rm -rf greengrass-build/ .gdk/`. Nothing prunes old image generations, docker build cache growth, or `/tmp` logs, and the preflight enforces no minimum-free threshold.

**Manual remediation applied (must be absorbed by the durable fix):** the 8 stale locally-tagged generations (all present in ECR) and old `/tmp` logs were deleted, freeing ~74 GB (54 → 128 GB free). Deliberately KEPT: `edgemlsdk:latest`, `flask-app:latest`, `react-webapp:latest`, and the docker build cache — these carry the onnxruntime GPU compile layers; a blanket `docker system prune -af` would force the full ~1–2 h GPU recompile on every subsequent build. **The fix must preserve this cache-retention property.**

**User request (binding):** "add to the dedicated build server scripts to prune unused previous builds when a new build starts to preserve disk space."

**Scope guardrails:**
- Cloud + script change: the repo scripts (`scripts/portal-build-agent.sh`, `portal-build.sh`) are the primary surface; a `build_dispatcher.py` change is optional. No component build is required for the fix itself — the scripts ship from the repo checkout the fleet syncs (the dispatcher's source-sync preamble plus the agent's own Step 2 sync put the server's clone on the job's `source_ref` before every build), so the fix lands on the server with the next dispatched build after a push. A portal deploy is needed ONLY if `build_dispatcher.py` changes.
- The security preservation gate (`test/backend-test/security/preservation/`) does NOT pin `portal-build.sh` or `portal-build-agent.sh` (verified: no baseline references). However, `test/backend-test/build_save_pkgs/test_preservation_baseline.py::test_neighbor_script_sha256_goldens` pins `scripts/portal-build-agent.sh` bit-identical via a frozen sha256 golden — an agent-script edit requires an intended golden update there. `test/backend-test/portal_builds/` suites (`test_source_selection_preservation.py`, `test_agent_tail_truncation_properties.py`, `test_preflight_target_matrix_properties.py`) execute or textually extract the real agent script and pin its argument contract, exit codes, preflight markers, phase-event field sets, and the ERROR_TAIL derivation — intended updates there must be identified during design.
- Verification of the durable fix = a real fleet build on the dedicated server showing the prune log and threshold behavior.

## Bug Analysis

### Current Behavior (Defect)

Disk consumption on the dedicated build server grows without bound across builds until a build dies mid-run:

1.1 WHEN a build publishes via the ECR path THEN `portal-build.sh` leaves the per-version locally-tagged image generation (`<registry>/dda/flask-app:<version>`, `<registry>/dda/react-webapp:<version>`) on the server and no subsequent build removes it (observed: 8 stale generations totaling ~74 GB at incident time)

1.2 WHEN a new build starts THEN the only between-build cleanup performed is `portal-build.sh` step [3/7] (`rm -rf greengrass-build/ .gdk/`) — no pruning of stale locally-tagged image generations, dangling images, docker build cache growth, or old `/tmp` build logs occurs

1.3 WHEN the agent's disk preflight runs THEN it measures and records disk capacity as evidence only and passes on any nonzero free space (`PREFLIGHT_MIN_DISK_GB` unset in production; job `e1d672ce` passed at 29 GB free on a 192 GB disk)

1.4 WHEN accumulated consumption leaves less free space than the build's actual footprint THEN the build proceeds anyway and fails mid-build (~1 h in, after the expensive GPU compile has started) with ENOSPC during docker layer extraction, and the agent's own log `tee` to `/tmp` also hits ENOSPC

1.5 WHEN disk exhaustion occurs THEN recovery requires manual operator intervention on the server (identify and delete stale image generations and logs by hand, then requeue the job)

### Expected Behavior (Correct)

When a new build starts, the server reclaims stale disk space before spending any expensive build time, without sacrificing the layers that make incremental builds fast:

2.1 WHEN a new build starts on a dedicated build server THEN the system SHALL, BEFORE the build proceeds, prune stale per-version locally-tagged image generations (retaining the `:latest` lineage and the current build's own tags, and deleting only images that are safely stored in ECR or otherwise reproducible), old per-job workspaces and `/tmp` build logs from previous builds, and dangling images

2.2 WHEN pruning runs THEN it SHALL NOT evict the layers that make incremental builds fast — the `edgemlsdk:latest` / `flask-app:latest` / `react-webapp:latest` images and the docker build cache carrying the onnxruntime GPU compile layers SHALL survive pruning (the exact retention policy is decided in design)

2.3 WHEN the pre-build disk check measures free space below a minimum threshold THEN the system SHALL fail fast (or prune and then re-check) instead of proceeding into the expensive build (the threshold is sized in design from the observed JP7 build footprint — the 29 GB pass led to a mid-build death after ~1 h; design decides the number and whether it is per-target)

2.4 WHEN free space is still below the minimum after pruning THEN the system SHALL fail the job BEFORE the expensive build starts, with a clear error identifying the disk insufficiency and the measured capacity

2.5 WHEN prune actions execute THEN the system SHALL log each prune action and the sizes freed into the build log

### Unchanged Behavior (Regression Prevention)

3.1 WHEN disk space is plentiful (above threshold, nothing stale to prune) THEN the system SHALL CONTINUE TO produce identical build behavior and published artifacts — same build steps, same component versions, same phase events, same `PORTAL_BUILD_RESULT` output

3.2 WHEN pruning deletes locally-tagged image generations THEN the corresponding images stored in ECR SHALL CONTINUE TO exist — pruning SHALL never delete anything from ECR

3.3 WHEN a build is in progress THEN its own images, tags, workspace, and logs SHALL CONTINUE TO be untouched by pruning (including the serialization guarantee that only one build runs at a time under `/var/lock/dda-build.lock`)

3.4 WHEN a genuine mid-build disk exhaustion still occurs THEN the fleet job lifecycle and error codes SHALL CONTINUE TO work unchanged — ENOSPC evidence classifies as `RUNNER_DISK_FULL` (via `build_reconciliation` / `build_events.py`) exactly as today

3.5 WHEN local development builds run via `run_jp_builds.sh` or direct `gdk component build` THEN they SHALL CONTINUE TO behave unchanged (they do not invoke `portal-build.sh`; they are affected only if design deliberately shares the pruning helper with them)

3.6 WHEN the existing test suites that pin the build scripts run THEN they SHALL CONTINUE TO pass after intended, explicitly-recorded updates only — the `build_save_pkgs` neighbor-script sha256 golden for `scripts/portal-build-agent.sh` is rebaselined as an intended change, and the `portal_builds` preservation/property suites' pinned contracts (agent argument contract, exit codes 64/75/78, preflight markers, phase-event field sets, ERROR_TAIL derivation) are preserved or updated deliberately, never weakened
