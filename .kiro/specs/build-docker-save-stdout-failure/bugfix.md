# Bugfix Requirements Document

## Introduction

AMD64 portal builds fail at the artifact-packaging step in `build-custom.sh`
line 366: `docker save flask-app > ./custom-build/$COMPONENT_NAME/flask-app.tar`
errors immediately with `write /dev/stdout: bad file descriptor` (EBADF),
leaving a 0-byte tar. Under `set -e` the script aborts, the job settles
`failed` (error_kind `building`, exit 1), and no publish step is reached.

Verified live: portal build job `d844a5fb-81d5-4294-956d-d6d6ae1f000e` (AMD64,
dedicated X86 server, source_ref `feature/workflow-triggers`, commit
`ab900d9`) settled `failed` on 2026-08-09 after ~9m32s at exactly this step.
The 0-byte tar was confirmed on-server via read-only SSM inspection; disk was
not the cause (29 GB free; the flask-app image is 4.78 GB). Full evidence:
`.kiro/specs/backend-jammy-retired-packages/verification-notes.md` (Task 7
re-execution section).

This is the first NON-apt blocker in the chain and a pre-existing latent
defect that was previously unreachable: this was the first AMD64 job ever to
build all three images completely (edgemlsdk, flask-app backend 63/63,
react-webapp frontend) and pass every in-build gate (security preservation
gate, CVE audit gate, backend unit tests). The three prior fixes
(`edgemlsdk-cmake-pin-failure`, `edgemlsdk-python-dev-ubuntu2204`,
`backend-jammy-retired-packages` — all verified live in this job's logs)
unmasked it. Those three specs remain open pending a `succeeded` build; this
follow-on spec removes the next blocker.

**Execution context (why the redirect fails):** the build server runs
`build-custom.sh` via SSM RunShellScript (agent-driven), and Docker is the
snap-packaged docker CLI. Snap confinement restricts the CLI's file access and
breaks its stdout plumbing when the parent process's stdout is the SSM
command pipe: the shell creates the target file (hence the 0-byte tar), but
the confined CLI cannot write to its redirected `/dev/stdout` and errors with
EBADF. Notably, the script's own comment at lines 361-365 documents that the
stdout-redirect form was itself a workaround for a snap Docker issue with
`docker save --output <file>` (snap writes a transient `.tmp-<name><rand>`
file in the destination dir and renames it, which raced the packaging `zip`
to exit 18). So NEITHER the `--output` form NOR the bare stdout-redirect form
works reliably under snap Docker; the redirect form additionally fails under
the SSM stdout context.

**Scan results** (repo scan for all `docker save` sites and similar redirect
patterns, per the whole-class-in-one-pass scoping):

- `build-custom.sh:366` — `docker save flask-app > .../flask-app.tar` — the
  confirmed live failure site.
- `build-custom.sh:367` — `docker save react-webapp > .../react-webapp.tar` —
  the identical failing pattern; never reached in job `d844a5fb` only because
  line 366 aborts first under `set -e`. Same class, must be fixed in the same
  pass.
- No other `docker save` (or `docker export`) invocation exists anywhere in
  the repo — not in `scripts/portal-build-agent.sh`, `publish-ecr-only.sh`,
  `com.dda.InferenceUploader/build-and-publish.sh`, `src/edgemlsdk/build.sh`,
  or any other shell script (verified by repo-wide grep). The class is exactly
  the two sites above.
- Golden coverage: NO test golden or security baseline embeds or hashes
  `build-custom.sh` bytes (verified: no `build-custom` reference under any
  `baselines/` directory, including the sibling packages' shell-script
  goldens — those cover `setup_station.sh`, `publish.sh`, and Dockerfiles
  only). The only automated scanner touching the file is
  `test/python_version_audit.py`, which lists `build-custom.sh` as a scoped
  artifact and greps for disallowed end-of-life Python 3.9 interpreter
  references — a fix to the `docker save` lines does not interact with it,
  but the audit must stay green.

**Bug condition C(X):** X is a `docker save` invocation site in
`build-custom.sh` that streams the image tar through the snap-confined docker
CLI's redirected stdout (`docker save <image> > <file>`), which fails with
`write /dev/stdout: bad file descriptor` when the script runs under the SSM
RunShellScript execution context, leaving a 0-byte tar and aborting the build.
Concretely today: lines 366 (flask-app) and 367 (react-webapp). Non-buggy
inputs ¬C(X) are every other line of `build-custom.sh` (the interpreter-version
audit guard, the edgemlsdk build and deb extraction, the docker-compose
builds, the in-image backend test / security gate block, the staging-dir
population, the explicit-member-list `zip` packaging with its `.tmp-*`
exclusion, the `zip -T` integrity check, and the copy to `greengrass-build`),
plus every other script in the repo.

**Fix-form constraint (direction left to design):** candidate directions
include piping through an intermediary (`docker save <img> | cat > <file>`),
using `--output` with a snap-accessible path then moving into place, writing
to a snap-allowed directory then `mv`, or running the save with stdout
explicitly re-plumbed (e.g. `sh -c` with the redirect inside). The design
phase chooses; the requirement here is only that whatever form is chosen MUST
produce a complete tar under BOTH snap Docker confinement AND the SSM
RunShellScript stdout context, MUST NOT reintroduce the snap `--output`
temp-file/zip race the current comment documents, and SHOULD also work in a
normal interactive shell (developers run this script locally).

**Validation constraint:** automated tests cannot run `docker save` against
real multi-gigabyte images. Fix and preservation checking are validated via
static/property tests over the `build-custom.sh` script text; the live portal
build is the true test. Live verification is gated: build servers sync from
origin, so a commit+push gate precedes the live-build gate. Both gates are
pre-authorized by the user for this chain but remain documented, separately
acknowledged gates.

**Completion criterion (user-mandated, shared with all three open sibling
specs):** this spec is complete only when an actual portal build reaches
`succeeded` including artifact publication.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `build-custom.sh` reaches line 366 (`docker save flask-app > ./custom-build/$COMPONENT_NAME/flask-app.tar`) under the snap Docker + SSM RunShellScript execution context THEN the system fails immediately with `write /dev/stdout: bad file descriptor`, leaves a 0-byte `flask-app.tar`, and aborts the script via `set -e` (verified: job `d844a5fb-81d5-4294-956d-d6d6ae1f000e`, 2026-08-09)

1.2 WHEN a portal build job targets AMD64 and all three images build and all in-build gates pass THEN the system terminates the job as `failed` (error_kind `building`, exit 1) at the artifact-packaging step, ~9-10 minutes in with warm layer cache, before creating the zip archive and before any publish step

1.3 WHEN line 367 (`docker save react-webapp > .../react-webapp.tar`) would execute under the same context THEN the system uses the identical failing stdout-redirect pattern (unreached today only because line 366 aborts first)

1.4 WHEN saving images under snap Docker THEN the system has no working form: the `--output` form breaks the packaging zip via snap's transient `.tmp-*` rename (documented at lines 361-365, the reason the redirect form was adopted), and the stdout-redirect form breaks under the SSM RunShellScript stdout context

### Expected Behavior (Correct)

2.1 WHEN `build-custom.sh` reaches the flask-app image-save step under the snap Docker + SSM RunShellScript execution context THEN the system SHALL produce a complete, non-empty, valid image tar at `./custom-build/$COMPONENT_NAME/flask-app.tar` without a `bad file descriptor` error

2.2 WHEN `build-custom.sh` reaches the react-webapp image-save step under the same context THEN the system SHALL likewise produce a complete, non-empty, valid image tar at `./custom-build/$COMPONENT_NAME/react-webapp.tar` — both C(X) sites fixed in the same pass, closing the class

2.3 WHEN the chosen save form executes THEN the system SHALL NOT reintroduce the snap `--output` temp-file hazard: no transient snap temp file may end up enumerated into or race the packaging `zip` (the explicit member list and `.tmp-*` exclusion at lines 404-421 continue to guard, but the save form itself must not depend on them failing open)

2.4 WHEN a developer runs `build-custom.sh` in a normal interactive shell (local build, stdout a terminal) THEN the system SHALL produce the same complete image tars — the fix must work in both the SSM and interactive contexts

2.5 WHEN a portal build job targets AMD64 THEN the system SHALL proceed past the image-save steps through zip packaging, the `zip -T` integrity check, the copy to `greengrass-build`, and on to artifact publication

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `build-custom.sh` runs THEN the system SHALL CONTINUE TO execute every line other than the fixed image-save step(s) (and their explanatory comment block at lines 360-365, which must be updated to stay truthful) byte-for-byte unchanged — including the interpreter-version audit guard, the edgemlsdk build and deb extraction, the docker-compose builds with their profile/build-arg selection, the in-image backend test and security gate block, the staging-dir population, the explicit-member-list zip packaging with diagnostics, the `zip -T` integrity check, and the copy to `greengrass-build`

3.2 WHEN the packaging zip is assembled THEN the system SHALL CONTINUE TO reference the exact tar destination paths and filenames (`custom-build/$COMPONENT_NAME/flask-app.tar`, `custom-build/$COMPONENT_NAME/react-webapp.tar`) in its explicit `ZIP_MEMBERS` list, with the saved tars landing at those exact paths

3.3 WHEN `test/python_version_audit.py` scans `build-custom.sh` (as a scoped artifact) THEN the system SHALL CONTINUE TO pass the interpreter-version audit guard with the fixed script

3.4 WHEN the security preservation suite and the three sibling test packages (`backend_jammy_pkgs`, `edgemlsdk_cmake`, `edgemlsdk_pythondev`) run against the fixed tree THEN the system SHALL CONTINUE TO pass with zero golden changes — no existing golden or baseline embeds `build-custom.sh` bytes (verified by scan), so the fix must not touch any file they cover (`src/docker-compose.yaml`, the backend/frontend/edgemlsdk Dockerfiles, `src/backend/requirements.txt`, `station_install/setup_station.sh`, and all sibling goldens)

3.5 WHEN builds run for the other targets (JP5, JP6, x86 NVIDIA) THEN the system SHALL CONTINUE TO package their artifacts through the same shared save/zip path unchanged in behavior — the fixed save form is target-agnostic and must not alter tar contents, naming, or the archive layout consumed by deployment
