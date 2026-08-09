# Verification Notes — edgemlsdk-python-dev-ubuntu2204

## Task 5: Fix, preservation, and property validation checkpoint (local/static only)

Date: 2026-08-09 (post-fix tree; fix at `src/edgemlsdk/Dockerfile:286` =
`RUN apt-get install python-dev-is-python3 -y`; two goldens regenerated via
sanctioned paths — the `src/edgemlsdk/Dockerfile` entry in
`docker_baseline_out_of_scope.json` now `021a7f60…`, and the `edgemlsdk_cmake`
CMake-masked golden recaptured through `capture_or_assert_text`).

Pre-run state check: `git status --porcelain` over `src/edgemlsdk/`,
`test/backend-test/edgemlsdk_pythondev/`, `test/backend-test/edgemlsdk_cmake/`,
`test/backend-test/security/` shows exactly the expected diff scope —
`M src/edgemlsdk/Dockerfile`,
`M test/backend-test/edgemlsdk_cmake/baselines/edgemlsdk_Dockerfile_cmake_masked.txt`,
`M test/backend-test/security/baselines/docker_baseline_out_of_scope.json`,
`?? test/backend-test/edgemlsdk_pythondev/` (the new package). No test file in
the package was modified between tasks 1/2 and this checkpoint.

### Task 5.1 — Property 1: Expected Behavior (Reqs 2.1, 2.2, 2.3)

Command:

```
PYTHONPATH=src/backend:test/backend-test pytest \
  test/backend-test/edgemlsdk_pythondev/test_bug_condition_exploration.py --noconftest
```

Result: **6 passed** in 0.21s (same tests as task 1, unmodified).

- No retired transitional Python package token in any apt install step of the
  fixed `src/edgemlsdk/Dockerfile` (the two cases that FAILED on the unfixed
  tree in task 1 now pass — fix confirmed for the C(X) site).
- Triton-section single-package step is exactly
  `RUN apt-get install python-dev-is-python3 -y` (token-boundary discipline
  held: the fixed token does not false-positive as `python-dev`).
- ¬C cases still pass: retired-token scan finds zero sites post-fix (scoping
  check's post-fix meaning), JP6 anchor requests `python3-dev`, JP5 retains
  its out-of-scope `python-dev` token, downstream `rm /usr/bin/python`
  (no `-f`) step present and after the install site.

PBT status: **passed**.

### Task 5.2 — Property 2: Preservation (Reqs 3.1, 3.2, 3.3)

Command:

```
PYTHONPATH=src/backend:test/backend-test pytest \
  test/backend-test/edgemlsdk_pythondev/test_preservation_baseline.py --noconftest
```

Result: **10 passed** in 2.66s against the FROZEN task-2 goldens (no
recapture/rebaseline).

- Python-dev-line-masked view of `src/edgemlsdk/Dockerfile` byte-identical to
  the unfixed capture (CMake block, Python 3.11 source build,
  `rapidjson-dev libre2-dev` step, `rm /usr/bin/python` step verbatim).
- Mask-exactness: exactly one line differs between the raw file and the
  masked view.
- `Dockerfile.jp5` and `Dockerfile.jp6` full-file sha256 goldens bit-identical.
- Hypothesis helper properties green (masking preservation, retired-token
  classifier exactness incl. `python-dev-is-python3` prefix trap, apt-line
  tokenization totality).

Golden immutability verified after the run: sha256 of all three files in
`test/backend-test/edgemlsdk_pythondev/baselines/` identical before/after
(`sha256sum -c` OK) and mtimes unchanged (all 1786250694).

PBT status: **passed**.

### Task 5.3 — Property 3: Baseline Regeneration (Reqs 2.4, 3.4, 3.5)

Command 1 (full docker security preservation suite):

```
PYTHONPATH=src/backend:test/backend-test pytest \
  test/backend-test/security/preservation/ --noconftest
```

Result: **152 passed, 2 skipped** in ~31s. The 2 skips are the known
pre-existing vendored-duplicate skips (unrelated to this fix):

- `test_preservation_s3_out_of_scope_guard.py:101` — vendored
  `edgemlsdk/edgemlsdk/**` duplicates are gitignored build artifacts
- `test_preservation_secrets_out_of_scope_guard.py:121` — vendored
  `edgemlsdk/edgemlsdk` `deploy.py` duplicate, same reason

Out-of-scope guard green with the regenerated `src/edgemlsdk/Dockerfile` hash;
jp5/jp6 masked goldens bit-identical; masked-bytes and hash-guard mechanisms
ran unchanged. Pre-existing warnings only (torch.load TorchScript UserWarning,
PyJWT algorithms DeprecationWarning) — both unrelated to this fix.

Command 2 (prior spec's package):

```
PYTHONPATH=src/backend:test/backend-test pytest \
  test/backend-test/edgemlsdk_cmake/ --noconftest
```

Result: **26 passed** in 2.57s. Its CMake-focused assertions unaffected; the
regenerated CMake-masked golden asserts green through the same
capture-or-assert mechanism.

Golden diff scoping (`git diff --stat` over `edgemlsdk_cmake/baselines/` and
`security/baselines/`): exactly two files changed, one line each —
`edgemlsdk_Dockerfile_cmake_masked.txt` and
`docker_baseline_out_of_scope.json`. The other three `edgemlsdk_cmake` goldens
(`edgemlsdk_Dockerfile.jp5_cmake_masked.txt`,
`edgemlsdk_Dockerfile.jp6.sha256.txt`,
`machine_setup.sh_install_cmake_masked.txt`) and the security masked goldens
(`docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt`,
`docker_baseline_edgemlsdk_Dockerfile.jp6_masked.txt`) are bit-identical
(no diff).

No-Docker-build contract verified by inspection: the only imports in
`test/backend-test/edgemlsdk_pythondev/*.py` are `hashlib`, `os`, `re`,
`hypothesis`, and the package's own support modules. Zero occurrences of
`subprocess`, `os.system`, `Popen`, `shell=`, `check_output`, `check_call`,
or any `docker` invocation in code (the words appear only in docstrings
documenting this constraint). All parsing is TEXT only.

Requirement traceability confirmed: every test carries a
`**Validates: Requirements …**` annotation.
- Property 1 (fix check) → exploration tests → Reqs 1.1–1.3, 2.1–2.3
- Property 2 (preservation) → frozen-golden + helper property tests → Reqs 3.1–3.3
- Property 3 (baseline regeneration) → security preservation suite +
  `edgemlsdk_cmake` package + diff scoping above → Reqs 2.4, 3.4, 3.5

PBT status: **passed**.

### Summary

| Suite | Result |
|-------|--------|
| edgemlsdk_pythondev exploration (Property 1) | 6 passed |
| edgemlsdk_pythondev preservation (Property 2) | 10 passed |
| security/preservation (Property 3) | 152 passed, 2 pre-existing skips |
| edgemlsdk_cmake (Property 3 / Req 3.5) | 26 passed |

Pre-existing unrelated failures: **none** (only the two documented
vendored-duplicate skips and two pre-existing dependency warnings).

**STOP honored**: no deploy, push, SSM command, instance action, artifact
publication, or build was performed in this checkpoint. Live verification
proceeds only through the separately approval-gated tasks 6 (commit + push)
and 7 (AMD64 dedicated live build; user-mandated completion criterion:
`succeeded` including artifact publication).

## Task 7 live verification build evidence

Date: 2026-08-09 (dispatched 05:14 UTC). AWS account 164152369890, us-east-1,
portal API `https://<portal-api-id>.execute-api.us-east-1.amazonaws.com/v1` (redacted).

### Approved scope (restated)
Exactly ONE live build: target AMD64, execution mode dedicated (existing X86
build server — same shape as evidence jobs `40b036fc`/`08a1e2bd`), source_ref
`feature/workflow-triggers` (fix commit
`4e1ce8cd1491b171ed6ce61c15d9f872f81afe9e` on origin there). No JP5/arm64
build authorized. Builds one at a time per steering.

### Preflight (pre-dispatch, per .kiro/steering/builds.md) — ALL PASSED
- No concurrent build: read-only SSM RunShellScript on the server
  (CommandId `7c964e49-c7b7-4853-9941-a4c56f1ff87e`) — `pgrep -af "gdk
  component build"` and `pgrep -af build-custom.sh` both empty; build-jobs
  table scan: 19 jobs total, ZERO non-terminal (one-at-a-time honored).
- No preservation-tracked file drift: `git status --porcelain` empty for
  `src/docker-compose.yaml`, backend/frontend/edgemlsdk Dockerfiles,
  `src/backend/requirements.txt`, `station_install/setup_station.sh`;
  HEAD = `4e1ce8c` == `origin/feature/workflow-triggers`.
- Guard run GREEN: `python3 -m pytest
  test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py
  test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py
  -p no:cacheprovider --noconftest -q` → **6 passed, 1 skipped** (known
  vendored-duplicate skip).
- Fleet/instance health: server `srv-5b214096-91a9-41b7-9d62-cc03ba205c15`
  ("X86 build server") lifecycle_state `running`; instance
  `i-0865b0697fb050036` (m6i.4xlarge) EC2 `running`, SSM PingStatus `Online`
  (agent 3.3.4793.0).
- AWS credentials valid (account 164152369890 verified via
  `sts get-caller-identity`).

### Portal API auth (temp-user lifecycle honored)
Stale prior temp user `kiro-build-verify-1786236861` found and DELETED first.
Fresh temporary Cognito user `kiro-build-verify-1786252370` (user pool
`us-east-1_<pool-id>` (redacted), `custom:role=UseCaseAdmin` → `builds:submit`),
random password held only in a 600-perm temp file; USER_PASSWORD_AUTH ID token
via app client (redacted). User DELETED and temp files shredded after the
build settled. No credentials stored in the repo or notes.

### Job
- build_job_id: `3d18ba88-9c17-490a-811b-8c21360216f4`
  (request_id `0fd32b97-603b-4b7b-89c7-ce6446b3cdc7`, request_order 0, single
  job as approved).
- Submitted via POST /builds → 201, status `queued`, created_at
  1786252479645 (2026-08-09T05:14:39.645Z).
- config_snapshot: repository
  `https://github.com/awslabs/DefectDetectionApplication`, source_ref
  `feature/workflow-triggers`, max_runtime_hours 4, volume_size_gb 200,
  x86_64_instance_type m6i.4xlarge, region us-east-1.
- Timeline: created 05:14:39.645Z → dispatched 05:14:40.021Z → execution
  start 05:14:43.205Z → ended 05:36:30.736Z (**~21m51s**; strictly further
  than job `08a1e2bd`'s ~13m35s). SSM CommandId
  `42792bb4-1146-42ea-b880-58dfe976fbdf`, attempt
  `baf146d9-c52a-4405-acde-d1a3d75d8911`. Dispatcher preflight PASSED
  (failures [], disk 79 GB available). Terminal effects completed
  (allocation release, audit, promotion wakeup done).

### Step 61/83 evidence (CloudWatch `/dda/portal-builds`, stream
`42792bb4-.../i-0865b0697fb050036/aws-runShellScript/stdout`) — BUG FIXED

Docker build step #65 = Dockerfile step 61/83 now runs the FIXED line and
succeeds — NO apt exit 100:

```
#65 [61/83] RUN apt-get install python-dev-is-python3 -y
#65 1.523 The following NEW packages will be installed:
#65 1.524   libpython3-dev libpython3.10-dev python-dev-is-python3 python-is-python3
#65 1.524   python3-dev python3.10-dev
#65 1.700 0 upgraded, 10 newly installed, 0 to remove and 10 not upgraded.
#65 3.208 Unpacking python-dev-is-python3 (3.9.2-2) ...
#65 3.408 Setting up python3-dev (3.10.6-1~22.04.1) ...
#65 3.423 Setting up python-dev-is-python3 (3.9.2-2) ...
#65 DONE 5.0s
```

- `python-dev-is-python3` resolved on jammy and transitively installed
  `python3-dev` (the JP6-anchor dev-headers package) AND `python-is-python3`
  (Reqs 2.1, 2.2) — exactly design Decision 1's predicted closure.
- The defect from job `08a1e2bd` (apt exit 100 at this step) did NOT
  reproduce (Reqs 1.1, 1.2, 2.3).
- Downstream precondition held: step #67 = 63/83
  (`RUN apt-get install libnuma-dev -y && rm /usr/bin/python && ln -s
  /usr/bin/python3.11 /usr/bin/python`) executed successfully —
  `/usr/bin/python` existed for the no-`-f` `rm` because
  `python-is-python3` provided it.

### CMake-step evidence (sibling spec's fix holding)
Docker step #34 = Dockerfile step 12/83 is the pinned GitHub-release
installer with `CMAKE_VER=3.31.6`; this run resolved it as `#34 CACHED` —
Docker reused the layer built by job `08a1e2bd`, which logged
`cmake version 3.31.6` (see the sibling spec's Task 7 evidence). No
`apt.kitware.com` resolution, no `cmake=3.21.3-0kitware*` pin, no apt
exit 100 anywhere in this run's edgemlsdk build.

### edgemlsdk image COMPLETED — all 83/83 steps
The edgemlsdk Docker build ran to completion: step #87 `[83/83] RUN apt-get
clean`, then `#88 exporting to image` / `naming to
docker.io/library/edgemlsdk:latest done`. Both this spec's fix and the
sibling's CMake fix fully verified within the edgemlsdk image.

### Final status — FAILED at a LATER, DIFFERENT image (new evidence, outside this spec's fix scope)

Job `3d18ba88-9c17-490a-811b-8c21360216f4` settled **`failed`**, error code
`BUILD_FAILED` ("Build failed (exit 1)"). The build progressed past the
entire edgemlsdk image (and the react-webapp frontend image exported too)
into the docker-compose backend image build, then died there:

#### Failing step: `src/backend/Dockerfile` line 70, target `backend_generic`, step 24/63, apt exit 100

```
#46 [backend_generic 24/63] RUN apt-get install libssl1.1 -y
#46 1.493 E: Unable to locate package libssl1.1
#46 1.493 E: Couldn't find any package by glob 'libssl1.1'
#46 1.493 E: Couldn't find any package by regex 'libssl1.1'
#46 ERROR: process "/bin/sh -c apt-get install libssl1.1 -y" did not complete successfully: exit code: 100
Dockerfile:70
  70 | >>> RUN apt-get install libssl1.1 -y
target backend_generic: failed to solve: ... exit code: 100
ERROR: docker-compose build failed
```

`build-custom.sh aws.edgeml.dda.LocalServer.amd64 NEXT_PATCH` exited 1, the
agent emitted `phase=failed error_kind=building`, and the job settled
`failed`. No publish step was reached.

#### Classification: follow-on failure PAST step 61/83 — NOT a fix insufficiency

- This spec's C(X) site (edgemlsdk Dockerfile line 286 / step 61/83)
  succeeded, and the whole edgemlsdk image built and exported. The fix held.
- The new failure is the SAME defect class in a DIFFERENT file:
  `libssl1.1` (OpenSSL 1.1 runtime) has no installation candidate on Ubuntu
  22.04 (jammy ships OpenSSL 3; `libssl1.1` existed through focal). Another
  pre-existing latent defect unmasked by this spec's fix — previously
  unreachable because the build always died in the edgemlsdk image first.
- Per this spec's bug-condition scoping, C(X) was limited to apt install
  steps in `src/edgemlsdk/Dockerfile`; `src/backend/Dockerfile` is out of
  scope (its bytes are preservation-tracked by the security suite and were
  untouched).

#### Routing

- Route the `libssl1.1` defect to a follow-on bugfix spec (suggested name:
  `backend-libssl11-ubuntu2204`) covering `src/backend/Dockerfile:70` plus a
  repo scan for other retired-package install sites on the 22.04 base
  (pattern now twice-confirmed: retired transitional/EOL packages surfacing
  one at a time as each earlier blocker is fixed).
- **This spec stays open**: per the user-mandated completion criterion in
  `bugfix.md`, it completes only when a portal build reaches `succeeded`
  including artifact publication. This job is strong progress evidence
  (edgemlsdk image fully built for the first time on AMD64; ~22 min vs the
  prior ~13m35s), not completion. The sibling spec
  `edgemlsdk-cmake-pin-failure` likewise remains open on the same criterion.
- Constraint honored: exactly one build dispatched; no retry, no second
  dispatch, no other state-changing operations beyond the approved build
  submission and temp-auth lifecycle.
