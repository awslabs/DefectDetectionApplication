# Verification Notes — edgemlsdk-cmake-pin-failure

## Task 5.4 — Local validation checkpoint (Properties 1–4)

Date: 2026-08-08 (post-fix tree; tasks 1–4, 5.1–5.3 complete)

### Validation commands and results

1. Full docker security preservation suite (fixed tree, regenerated goldens):

   ```
   PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/security/preservation/ --noconftest
   ```

   **Result: 152 passed, 2 skipped** (31.6s, exit 0).

   - The 2 skips are pre-existing and intentional, unrelated to this spec:
     - `test_preservation_s3_out_of_scope_guard.py:101` — vendored
       `edgemlsdk/edgemlsdk/**` duplicates are gitignored build artifacts,
       excluded from the byte-for-byte out-of-scope guard.
     - `test_preservation_secrets_out_of_scope_guard.py:121` — same vendored
       `deploy.py` duplicate exclusion.
   - No pre-existing unrelated failures: the suite is fully green.
   - Masked-bytes mechanism intact (Req 3.4):
     `test_preservation_docker_masked_bytes.py` passes against the regenerated
     `docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt` — FROM-line masking
     and byte-for-byte comparison enforced unchanged.
   - Out-of-scope guard green (Req 2.5):
     `test_preservation_docker_out_of_scope.py` passes with the updated
     `src/edgemlsdk/Dockerfile` entry. Verified
     `sha256sum src/edgemlsdk/Dockerfile` =
     `5fe6186db15e31fa87b2924a4d014a729399561c8dcc110754c6bfbe95a7e355`,
     matching the single-entry update in
     `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`
     (git diff confirms exactly one entry changed; all other entries untouched).
   - JP6 golden unchanged (Req 3.1):
     `git diff --stat test/backend-test/security/baselines/docker_baseline_edgemlsdk_Dockerfile.jp6_masked.txt`
     is empty; `git status --porcelain` on the baselines directory shows only
     the two intentionally regenerated goldens modified
     (`docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt`,
     `docker_baseline_out_of_scope.json`).

2. Spec's own package (final tally):

   ```
   PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/edgemlsdk_cmake/ --noconftest
   ```

   **Result: 26 passed** (2.5s, exit 0).

### Property 1–4 traceability (all passing)

| Property | Test(s) | Requirement annotations |
| --- | --- | --- |
| Property 1 — Pinned Upstream Release-Binary CMake Install | `test/backend-test/edgemlsdk_cmake/test_bug_condition_exploration.py` (bionic/focal branches, jp5 step, `install_cmake`, jp6 ¬C anchor, repo-wide site scan, counterexample-inventory scoping) | Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4 (per-test docstrings) |
| Property 2 — Preservation: Non-CMake Lines and JP6 Unchanged | `test/backend-test/edgemlsdk_cmake/test_preservation_baseline.py` (jp6 sha256 golden, CMake-block-masked goldens b/c, `install_cmake`-masked golden d, mask-exactness, skip-path/else-branch verbatim, Hypothesis masking-helper properties) | Validates: Requirements 3.1, 3.2, 3.4, 3.5 (per-test docstrings) |
| Property 3 — No-4.x Pinned Version Range | `test/backend-test/edgemlsdk_cmake/test_version_range_properties.py` (Hypothesis version-range comparator, concrete `CMAKE_VER` assertions across all four files, arch-selection property) | Validates: Requirements 2.3, 3.3 |
| Property 4 — Baseline Regeneration: Goldens Track the Fixed Tree | `test/backend-test/security/preservation/test_preservation_docker_masked_bytes.py`, `test_preservation_docker_out_of_scope.py` (regenerated goldens, task 4) plus this full-suite run | Requirements 2.5, 3.1, 3.4 |

### No-Docker-build constraint (verified by inspection)

- The spec's `test/backend-test/edgemlsdk_cmake/` package parses the affected
  files as TEXT only (`_cmake_support.py` documents "no Docker builds — the
  spec's validation constraint"). No `docker`, `subprocess`, `Popen`,
  `os.system`, or `check_output` usage anywhere in the package.
- The docker preservation suite's only subprocess usage
  (`test_preservation_docker_profile.py`) runs a bash snippet and a nested
  pytest for docker-profile selection logic — it launches no Docker build.
- No automated test in this spec runs a Docker build.

### No-live-action contract

Confirmed for this checkpoint: no deploy, no push, no SSM command, no
instance start/stop/terminate, no artifact publication, no build dispatched.
Live verification proceeds only through tasks 6 (approval-gated push) and
7 (separately approval-gated AMD64 dedicated verification build).

Per the user-mandated completion criterion in `bugfix.md`, this spec is NOT
complete until a portal build reaches `succeeded` including artifact
publication (task 7).

## Task 7 live verification build evidence

Date: 2026-08-09 (dispatched ~00:54 UTC). AWS account 164152369890, us-east-1,
portal API `https://<portal-api-id>.execute-api.us-east-1.amazonaws.com/v1` (redacted).

### Approved scope (restated)
Exactly ONE live build: target AMD64, execution mode dedicated (existing X86
build server — same shape as failing evidence job `40b036fc`), source_ref
`feature/workflow-triggers` (user's task-6 branch choice, overriding the
tasks.md original; fix commit `63ecb99f6d523ef2695aba7edf318cd11190877c` is on
origin there). No JP5/arm64 build authorized. Builds one at a time per
steering.

### Preflight (pre-dispatch, per .kiro/steering/builds.md)
- No concurrent build: `pgrep -af "gdk component build"` and
  `pgrep -af "build-custom.sh"` both empty.
- No preservation-tracked file drift: `git status` clean for
  `src/docker-compose.yaml`, backend/frontend/edgemlsdk Dockerfiles,
  `src/backend/requirements.txt`, recipes, `station_install/setup_station.sh`
  (only `.kiro/.../tasks.md` locally modified; tree at `63ecb99` == origin).
- Guard run GREEN: `python3 -m pytest
  test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py
  test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py
  -p no:cacheprovider --noconftest -q` → **6 passed, 1 skipped** (known
  vendored-duplicate skip).
- Fleet/instance health: server `srv-5b214096-91a9-41b7-9d62-cc03ba205c15`
  ("X86 build server", x86_64, dedicated) lifecycle_state `running`, no busy
  residue; instance `i-0865b0697fb050036` (m6i.4xlarge) EC2 `running`, SSM
  PingStatus `Online`. Build-jobs table scan: zero non-terminal jobs
  (one-at-a-time honored).
- AWS session had expired mid-preflight; user refreshed credentials
  (account 164152369890 re-verified via `sts get-caller-identity`).

### Portal API auth (user-selected mechanism)
Temporary Cognito user `kiro-build-verify-1786236861` (user pool
`us-east-1_<pool-id>` (redacted), `custom:role=UseCaseAdmin` → `builds:submit`), random
password held only in a 600-perm temp file, USER_PASSWORD_AUTH ID token via
client `<app-client-id>` (redacted). User to be deleted after the build
settles; no credentials stored in the repo or notes.

### Job
- build_job_id: `08a1e2bd-45f9-4521-ac4a-b41b52222e2e`
  (request_id `98d4028d-a8e9-4b0b-9a81-918e29f690e7`, request_order 0, single
  job as approved).
- Submitted via POST /builds → 201, status `queued`, created_at 1786236899876
  (2026-08-09T00:54:59.876Z).
- config_snapshot: repository
  `https://github.com/awslabs/DefectDetectionApplication`, source_ref
  `feature/workflow-triggers`, max_runtime_hours 4, volume_size_gb 200,
  x86_64_instance_type m6i.4xlarge, region us-east-1.

### Dispatcher preflight (deployed backend, runs first)
PASSED at dispatched_at 1786236900294 (00:55:00.294Z; queue ~418 ms):
checks {execution_mode dedicated, build_target AMD64, required_arch x86_64,
component_name aws.edgeml.dda.LocalServer.amd64, repo_dir
/home/ubuntu/DefectDetectionApplication, callback_bus default,
quoting_round_trip true}, failures []. Disk evidence: 90 GB available
(/var/snap/docker/common), agent_checks passed. SSM attempt
`91e0d6ca-398b-4106-8e8f-dbf184f735ad`, CommandId
`6257c0b1-ccfa-4d32-9369-10d38b4c58bb`, instance `i-0865b0697fb050036`.

### CMake-step evidence (Build Log API, ~5 min in) — BUG FIXED
Docker build step #16 (edgemlsdk CMake install) now logs:

```
#16 0.574 CMake Installer Version: 3.31.6, Copyright (c) Kitware
#16 0.574 This is a self-extracting archive.
#16 0.574 The archive will be extracted to: /usr/local
#16 1.688 Unpacking finished successfully
#16 1.696 cmake version 3.31.6
#16 1.696 CMake suite maintained and supported by Kitware (kitware.com/cmake).
```

- GitHub-release self-contained installer ran and the in-build
  `cmake --version` check printed `cmake version 3.31.6` (Req 2.1, 2.3).
- NO `apt.kitware.com` resolution, NO `cmake=3.21.3-0kitware*` pin, NO apt
  exit 100 — the defect from job `40b036fc` no longer reproduces (Req 1.4).
- Build proceeded past the CMake step into the Python 3.11 source build
  (step #19) — strictly further than the failing evidence job ever got.

### Final status — FAILED at a LATER step (new evidence, outside this spec's fix scope)

Job `08a1e2bd-45f9-4521-ac4a-b41b52222e2e` settled **`failed`**, error code
`BUILD_FAILED` ("Build failed (exit 1)"). Timeline (build-jobs record):
created 2026-08-09T00:54:59.876Z → dispatched 00:55:00.294Z → execution start
00:55:03.498Z → ended 2026-08-09T01:08:35.299Z (~13m35s total). Terminal
effects completed (allocation release, audit, promotion wakeup done).

#### Failing step (CloudWatch `/dda/portal-builds`, stream
`6257c0b1-ccfa-4d32-9369-10d38b4c58bb/i-0865b0697fb050036/aws-runShellScript/stdout`)

Docker build step **#65 = Dockerfile step 61/83**, `src/edgemlsdk/Dockerfile`
**line 286** (`RUN apt-get install python-dev -y`), apt **exit code 100**:

```
 > [61/83] RUN apt-get install python-dev -y:
1.403 Package python-dev is not available, but is referred to by another package.
1.403 However the following packages replace it:
1.403   python2-dev python2 python-dev-is-python3
1.407 E: Package 'python-dev' has no installation candidate
#65 ERROR: process "/bin/sh -c apt-get install python-dev -y" did not complete successfully: exit code: 100
ERROR: failed to build: failed to solve: process "/bin/sh -c apt-get install python-dev -y" did not complete successfully: exit code: 100
ERROR: edgemlsdk Docker build failed
```

`gdk component build` then failed (`build-custom.sh ... exit status 1`), the
agent emitted `phase=failed error_kind=building`, and the job settled `failed`.
No publish step was reached.

#### Classification: (b) follow-on failure PAST the CMake step — NOT a fix insufficiency

- The fixed CMake step (**#16, step 12/83**) succeeded: GitHub-release
  installer, `cmake version 3.31.6`, zero `apt.kitware.com` resolution, no
  `cmake=3.21.3-0kitware*` pin, no apt exit 100 at that step (see CMake-step
  evidence above). The original defect (job `40b036fc`) did **not** reproduce
  — this spec's fix held (Req 1.4, 2.1, 2.3).
- The new failure is a **pre-existing latent defect ~49 Dockerfile steps
  later**: `python-dev` is a transitional package absent on Ubuntu 22.04
  (replaced by `python-dev-is-python3` / `python2-dev`). It was previously
  unreachable because the build always died at the CMake step before it; this
  spec's fix unmasked it. It is unrelated to CMake, Kitware, or any file this
  spec changed a behavior of (line 286 is a non-CMake line preserved
  byte-for-byte per Property 2).

#### Routing

- New evidence recorded here per the task 7 new-failure handling; route the
  `python-dev` defect to a **follow-on bugfix spec** (suggested name:
  `edgemlsdk-python-dev-ubuntu2204`) covering `src/edgemlsdk/Dockerfile:286`
  (and a repo scan for other obsolete `python-dev` install sites).
- **This spec stays open**: per the user-mandated completion criterion in
  `bugfix.md`, it completes only when a portal build reaches `succeeded`
  including artifact publication. This job is progress evidence (strictly
  further than `40b036fc`), not completion.
- Cleanup reminder (from the auth note above): temporary Cognito user
  `kiro-build-verify-1786236861` is to be deleted now that the build settled.
