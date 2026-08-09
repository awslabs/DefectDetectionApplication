# Verification Notes — backend-jammy-retired-packages

Task 5 validation checkpoint (Properties 1-3), run on the fixed tree.
Local/static validation only: no deploy, push, SSM command, instance action,
artifact publication, or build was performed. Live verification remains gated
behind tasks 6 and 7 (separate explicit approvals).

Endpoint hygiene: this file contains NO live API Gateway URLs or other internal
endpoints (prior-chain lesson). Where an endpoint would otherwise be referenced,
use the placeholder `<REDACTED-INTERNAL-ENDPOINT>`.

## Tree state at checkpoint

- `git status` diff scope (spec-relevant): `src/backend/Dockerfile` (the fix,
  sha256 `ba076f2fae5de7819935daa08b71ed6c34ec2839ecbb519ba7dc5ffca3b0c655`),
  `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`
  (exactly ONE entry regenerated — the `src/backend/Dockerfile` hash,
  `40f7c9e0...` → `ba076f2f...`; compose/edgemlsdk/frontend entries untouched,
  confirmed by `git diff`), and the new untracked package
  `test/backend-test/backend_jammy_pkgs/`.
- Test-file freeze: sha256 of both test modules and both support modules
  captured before and after all checkpoint runs — identical
  (`test_bug_condition_exploration.py` `88ae09ff...`,
  `test_preservation_baseline.py` `cbe4b76e...`,
  `_jammy_support.py` `17151e7c...`, `_jammy_preservation_support.py`
  `d36c6512...`). The task-3-era corrective parsing fix to exploration case 2
  (shared `normalized_apt_bodies` helper) predates this checkpoint and is part
  of the frozen baseline; assertions were not weakened (re-verified against
  the unfixed tree during that fix).

## Task 5.1 — Property 1 (Expected Behavior)

Command:
`PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/backend_jammy_pkgs/test_bug_condition_exploration.py --noconftest`

Result: **7 passed** in 0.21s (all seven design exploration cases).
- Cases 1, 2, 6 (the former bug-condition failures on the unfixed tree) now
  PASS: zero jammy-retired tokens in any AMD64-reachable apt step; the libssl
  step is the exact `/etc/os-release`-gated conditional with allowlist
  {"18.04", "20.04"} and body `apt-get install libssl1.1 -y`; the guarded step
  is 18.04/20.04-reachable and 22.04-unreachable under the reachability model.
- Cases 3, 4, 5, 7 still PASS: retired-token scan finds zero reachable sites
  (post-fix meaning); class-closure verdict inventory fully vetted; `ARG OS`
  only before `FROM`, no re-declaration after; frontend Dockerfile has zero
  apt steps.
- Tests re-run UNCHANGED from task 1 (hash-verified above). Fix confirmed;
  class closed. _Requirements: 2.1, 2.2, 2.3, 2.4._

## Task 5.2 — Property 2 (Preservation)

Command:
`PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/backend_jammy_pkgs/test_preservation_baseline.py --noconftest`

Result: **21 passed** in 3.70s against the FROZEN goldens (no recapture).
- Masked golden `backend_Dockerfile_libssl_masked.txt`: byte-identical — the
  shape-agnostic mask matched the fixed comment+conditional block, proving
  every other `src/backend/Dockerfile` line (Python 3.11 source build, awscrt
  workaround, apt-update lines, line 72's six-package install, inert $OS
  conditional, CVE block, COPY/script invocations) survives verbatim.
- Mask-exactness: exactly ONE target block differs from the raw file, and its
  shape is the admissible "fixed" form.
- All 8 full-file sha256 goldens (frontend Dockerfile, compose,
  jp5/jp6/x86_64_nvidia variants, three install scripts): bit-identical.
- Hypothesis properties (classifier token-boundary, masking preservation,
  apt tokenization totality, reachability model): all green.
- Golden immutability verified after the run: sha256 of all 9 golden files in
  `test/backend-test/backend_jammy_pkgs/baselines/` identical to the pre-run
  capture (e.g. masked golden `aba912ae...`). _Requirements: 3.1, 3.2, 3.3._

## Task 5.3 — Property 3 (Baseline Regeneration) + suite-wide checks

1. Full security preservation suite:
   `PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/security/preservation/ --noconftest`
   → **152 passed, 2 skipped** in 31.74s. The 2 skips are the known
   pre-existing vendored-duplicate skips
   (`test_preservation_s3_out_of_scope_guard.py`,
   `test_preservation_secrets_out_of_scope_guard.py`) — expected shape.
   Out-of-scope guard green with the regenerated `src/backend/Dockerfile`
   entry; masked-bytes mechanism, backend/edgemlsdk jp5+jp6 masked goldens,
   and default-refs guard all intact (no golden other than the one JSON entry
   modified — `git status` on `test/backend-test/security/baselines/` shows
   only `docker_baseline_out_of_scope.json`).

2. Sibling packages (run per-package; a single combined invocation hits a
   pytest module-basename collision between the two rootless packages'
   identically named test files — pre-existing layout property, no files
   touched):
   - `pytest test/backend-test/edgemlsdk_cmake/ --noconftest` → **26 passed**
   - `pytest test/backend-test/edgemlsdk_pythondev/ --noconftest` → **16 passed**
   Total 42/42. `git status` over both packages: clean — all sibling goldens
   bit-identical (`src/edgemlsdk/**` untouched, Req 3.5).

3. No-Docker-build contract: grep over
   `test/backend-test/backend_jammy_pkgs/*.py` — imports are only `hashlib`,
   `os`, `re`, `collections`, `pytest`, `hypothesis`, and the two local
   support modules; zero `docker`/`subprocess`/`os.system`/`Popen`/shell-out
   call sites (the only textual matches are docstrings stating the contract
   and file-path references). No automated test in this spec runs a Docker
   build.

4. Property traceability:
   - Property 1 → task 5.1 run (7/7), Requirements 2.1-2.4.
   - Property 2 → task 5.2 run (21/21), Requirements 3.1-3.3.
   - Property 3 → task 4 regeneration + this checkpoint's suite runs
     (152 passed/2 skipped + 42/42, exactly one golden entry regenerated,
     mechanisms intact), Requirements 2.5, 3.4, 3.5.

Pre-existing unrelated failures: none observed in any suite run.

## Status

Local validation checkpoint COMPLETE. Spec remains OPEN per the user-mandated
completion criterion (an actual portal build must reach `succeeded` including
artifact publication). Next gates: task 6 (explicit push approval) and task 7
(separate explicit live-build approval). Neither is authorized by this
checkpoint.

## Task 7 live verification build — attempt blocked (AWS credentials expired)

Date: attempt aborted before any AWS-dependent preflight step or dispatch.

Approved scope (restated, unexecuted): exactly ONE live build — target AMD64,
mode dedicated (existing X86 build server, same shape as evidence job
`3d18ba88`), source_ref `feature/workflow-triggers` (fix commit
`ab900d9ff2f2cb57c6c3b59f5f33dd35dc7e3cf3` on origin). No JP5/arm64 build.

### Local (non-AWS) preflight — PASSED
- Repo drift: `git status --porcelain` empty for all preservation-tracked
  files (`src/docker-compose.yaml`, backend/frontend/edgemlsdk Dockerfiles,
  `src/backend/requirements.txt`, `station_install/setup_station.sh`).
- HEAD `ab900d9ff2f2cb57c6c3b59f5f33dd35dc7e3cf3` ==
  `origin/feature/workflow-triggers` (fix commit pushed; build servers sync
  from origin).
- Guard run GREEN: out-of-scope guard + secrets guard
  (`-p no:cacheprovider --noconftest -q`) → **6 passed, 1 skipped** (known
  vendored-duplicate skip).

### AWS-dependent preflight — NOT RUN (blocker)
`aws sts get-caller-identity` on the sole (`default`) profile returns
"Your session has expired. Please reauthenticate using 'aws login'."
Therefore the concurrent-build scan (build-jobs table), read-only server
check, fleet/instance health check, temp Cognito user lifecycle, and build
dispatch were all NOT attempted.

### Outcome
- **Zero builds dispatched. Zero state-changing operations performed.**
  No temp Cognito user created (none to clean up).
- Task 7 remains UNCHECKED in all three specs; the shared completion
  criterion (a `succeeded` AMD64 build including artifact publication) is
  still unmet.
- Next step: reauthenticate AWS, then re-run task 7 from the top of its
  preflight (all preflight checks must be repeated fresh at dispatch time).

## Task 7 live verification build — re-execution (fix VERIFIED; job failed LATER at a new, out-of-scope step)

Date: 2026-08-09. AWS account 164152369890 (verified via `sts get-caller-identity`
after the user refreshed credentials), us-east-1. Portal API
`<REDACTED-INTERNAL-ENDPOINT>` (no live endpoints in this file).

### Approved scope (restated)
Exactly ONE live build: target AMD64, mode dedicated (X86 build server
`srv-5b214096-91a9-41b7-9d62-cc03ba205c15`, instance `i-0865b0697fb050036` —
same shape as evidence job `3d18ba88`), source_ref `feature/workflow-triggers`
(fix commit `ab900d9ff2f2cb57c6c3b59f5f33dd35dc7e3cf3` on origin). No JP5/arm64
build. One at a time per steering.

### Preflight (fresh, per .kiro/steering/builds.md) — ALL PASSED
- Credentials valid: account 164152369890.
- No concurrent build: build-jobs table scan — 20 jobs total, ZERO non-terminal;
  read-only SSM check on the server (CommandId `cfc471d8-3016-4034-89f6-7023ea072ef0`) —
  `pgrep -af "gdk component build"` and `pgrep -af build-custom.sh` both empty.
- No preservation-tracked drift: `git status --porcelain` empty for
  `src/docker-compose.yaml`, backend/frontend/edgemlsdk Dockerfiles, the
  backend Dockerfile variants, `src/backend/requirements.txt`,
  `station_install/setup_station.sh`; HEAD
  `ab900d9ff2f2cb57c6c3b59f5f33dd35dc7e3cf3` == `origin/feature/workflow-triggers`
  (fetched fresh).
- Guard run GREEN: out-of-scope guard + secrets guard
  (`-p no:cacheprovider --noconftest -q`) → **6 passed, 1 skipped** (known
  vendored-duplicate skip).
- Fleet/instance health: server record lifecycle_state `running`
  (instance_id matches); EC2 `running` (m6i.4xlarge); SSM PingStatus `Online`
  (agent 3.3.4793.0).

### Portal API auth (temp-user lifecycle honored)
Stale temp user `kiro-build-verify-1786287107` found and DELETED first. Fresh
temporary Cognito user `kiro-build-verify-1786287399`
(`custom:role=UseCaseAdmin`); random password and ID token held only in
600-perm temp files. User DELETED and all temp files shredded after the build
settled. No credentials stored in the repo or notes.

### Job
- build_job_id: `d844a5fb-81d5-4294-956d-d6d6ae1f000e`
  (request_id `8fd34d6f-2a92-4e4f-80da-05125f0b4711`, request_order 0, single
  job as approved). Submitted via POST /builds → HTTP 201, status `queued`.
- config_snapshot: repository `https://github.com/awslabs/DefectDetectionApplication`,
  source_ref `feature/workflow-triggers`, max_runtime_hours 4.
- Timeline: created 2026-08-09T14:58:48.344Z → dispatched 14:58:48.749Z →
  execution start 14:58:51.872Z → ended 15:08:20.875Z (**~9m32s** — heavy
  Docker layer cache reuse from job `3d18ba88`). SSM CommandId
  `6febfd7a-89a3-421f-846f-581fdb54635d`, attempt
  `df5d7783-f2b1-4837-912b-a33c4008ba55`. Agent preflight PASSED (failures [],
  37 GB available). Terminal effects completed (allocation release, audit done).

### Checkpoint evidence (CloudWatch `/dda/portal-builds`, stream
`6febfd7a-.../i-0865b0697fb050036/aws-runShellScript/stdout`)

(a) **edgemlsdk CMake step clean** — `#49 [12/83] RUN CMAKE_VER=3.31.6 && ...`
→ `#49 CACHED` (layer from job `3d18ba88`, which logged `cmake version 3.31.6`).
No apt.kitware.com resolution, no exit 100.

(b) **python-dev step clean** — `#39 [61/83] RUN apt-get install
python-dev-is-python3 -y` → `#39 CACHED`. edgemlsdk image completed and
exported (`naming to docker.io/library/edgemlsdk:latest done`).

(c) **THE FIXED STEP — guarded conditional SKIPS on jammy, no apt exit 100**
(former failure site of job `3d18ba88`):

```
#46 [backend_generic 24/63] RUN . /etc/os-release && if [ "$VERSION_ID" = "18.04" ] || [ "$VERSION_ID" = "20.04" ] ; then     apt-get install libssl1.1 -y;     fi
#46 DONE 0.2s
```

No `Unable to locate package libssl1.1`, no exit 100 — the guard evaluated
false on the 22.04 base and the step completed in 0.2s. **Bug condition did
not reproduce (Reqs 1.1, 1.2, 2.1, 2.3).**

(d) **Line-72 six-package install green** (closes the flagged ¬C verdict live):
`#48 [backend_generic 26/63] RUN apt install libexif12 libcurl4 libarchive13
gstreamer1.0-tools gstreamer1.0-libav ffmpeg -y` → resolved, "0 upgraded, 35
newly installed", DONE. **CVE-block install green**: `#81 [backend_generic
59/63] RUN apt-get update && apt-get install -y --no-install-recommends
build-essential libgnutls28-dev libuv1 ...` → "12 newly installed", DONE
(Req 2.4 verified live — class closed).

(e) **Backend image COMPLETED — all 63/63 steps** (`#85 [backend_generic
63/63] RUN python3.11 dlr_disable_phone_home.py`), exported and named
(`naming to docker.io/library/flask-app:latest done`). Frontend
`react-webapp:latest` exported too. In-build gates all green: security
preservation gate, dependency/CVE audit gate ("Security dependency/supply-chain
CVE audit gate passed"), "Backend unit tests passed".

### Final status — `failed` at a NEW, LATER, DIFFERENT step (outside this spec's scope)

Job settled **`failed`**, error_kind `building` ("Build failed (exit 1)").
The failure is in `build-custom.sh` line 366's packaging step, AFTER all three
images built and all gates passed:

```
save docker images as tarvballs
write /dev/stdout: bad file descriptor
```

`docker save flask-app > ./custom-build/aws.edgeml.dda.LocalServer.amd64/flask-app.tar`
failed immediately: the shell created the target file (0 bytes, confirmed by
read-only SSM inspection) but the snap-confined docker CLI errored writing its
stdout stream (`EBADF`). NOT a disk issue (29 GB free post-run; flask-app image
is 4.78 GB) and NOT an apt/package issue — no publish step was reached.

#### Classification: follow-on failure PAST the fixed step — NOT a fix insufficiency

- This spec's C(X) site (backend Dockerfile libssl step 24/63) succeeded as
  designed; the backend image built completely for the first time on AMD64.
  Both siblings' fixed steps logged clean (CACHED). The whole
  jammy-retired-package class is closed live (Req 1.4).
- The new failure is a DIFFERENT defect class entirely: the artifact-packaging
  `docker save`-to-stdout-redirect step under snap Docker — itself a prior
  workaround for a snap `--output` temp-file issue (see the comment at
  `build-custom.sh:361-365`). First time this step has ever been reached on
  AMD64.

#### Routing

- Route to a follow-on bugfix spec (suggested name:
  `build-docker-save-stdout-failure`) covering `build-custom.sh:366-367` —
  `docker save` failing with `write /dev/stdout: bad file descriptor` under
  the snap Docker + SSM RunShellScript execution context, leaving a 0-byte
  tar. (Pattern continues: each fix unmasks the next latent blocker; this is
  the first NON-apt blocker in the chain.)
- **This spec stays open**: the user-mandated completion criterion (a portal
  build reaching `succeeded` INCLUDING artifact publication) is NOT met. This
  job is the strongest progress evidence yet (all images built + exported,
  all gates green, ~9m32s), but not completion. Task 7 remains unchecked in
  all three specs (`backend-jammy-retired-packages`,
  `edgemlsdk-cmake-pin-failure`, `edgemlsdk-python-dev-ubuntu2204`).
- Constraints honored: exactly one build dispatched; no retry, no second
  dispatch; the only state-changing operations were the approved build
  submission and the temp-auth lifecycle (stale user deletion, fresh user
  create/delete).

### Cleanup
Temp Cognito user `kiro-build-verify-1786287399` deleted after settlement;
password/token/response temp files and the local log-analysis scratch file
shredded. Verified zero `kiro-build-verify-*` users remain in the pool.
