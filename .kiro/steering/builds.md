---
inclusion: always
---

# Greengrass Component Builds (JP5 / JP6 / JP7)

## CRITICAL: never run two component builds at the same time

JP5, JP6, and JP7 (and any other target) builds **must run strictly one at a time**.
Running two builds concurrently **corrupts the model versioning** (the builds
share the `NEXT_PATCH` version resolution plus the working directories and
docker image tags — `greengrass-build/`, `custom-build/`, and the shared
`edgemlsdk` / `flask-app` / `react-webapp` image tags — so concurrent runs
clobber each other and produce wrong/duplicate model versions).

**If two builds are ever running at once: STOP BOTH immediately, then restart
one at a time.** Do not let a second build start until the first has fully
finished.

## How to build

`gdk component build` builds the single component named in `gdk-config.json`.
The gdk config schema allows exactly ONE entry under `component`, so the file
holds one target at a time. To build multiple targets, build them
**sequentially**, swapping the component name in `gdk-config.json` between runs
(JP7 = `aws.edgeml.dda.LocalServer.arm64JP7`, JP6 =
`aws.edgeml.dda.LocalServer.arm64JP6`, JP5 =
`aws.edgeml.dda.LocalServer.arm64JP5`). Every entry follows the same structure
with the custom build command `bash build-custom.sh <component-name>
NEXT_PATCH`. The target (JP5 vs JP6 vs JP7) is derived from the component name
by `build-custom.sh`. `run_jp_builds.sh` automates the swap (e.g.
`TARGETS="7" ./run_jp_builds.sh`).

- Build only (no AWS creds needed): `gdk component build`.
- `gdk-config.json` is a build artifact excluded from commits; swapping it per
  target is fine, but restore it when done.
- Each target runs a full GPU `onnxruntime` source build by default
  (`ONNXRUNTIME_GPU=1` for JP5/JP6/JP7), so a single target takes ~1–2h.
- Capture each target's output to its own log: `.gdk_build_jp7.log` /
  `.gdk_build_jp6.log` / `.gdk_build_jp5.log`.

## Before dispatching any build

Check that no build is already running:

```
pgrep -af "gdk component build"
pgrep -af "build-custom.sh"
```

If either returns a process, do **not** start another build — wait for it to
finish (or stop it) first.

**Address the security preservation gate BEFORE starting the build.** The gate
runs late in the build (after the ~1h compile), so a stale baseline wastes the
whole build. Pre-build check:

1. See whether any preservation-tracked file changed since the last green
   build: `git status`/`git diff` against `src/docker-compose.yaml`, the
   backend/frontend/edgemlsdk Dockerfiles, `src/backend/requirements.txt`,
   the recipe variants, and `station_install/setup_station.sh`.
2. If any did, rebaseline the affected hashes NOW (see "Security preservation
   gate" section below) and run the preservation suite in the flask-app
   container to confirm it passes — before kicking off the build.
3. Move `edge-cv-portal/infrastructure/cdk.out` aside (`mv cdk.out
   cdk.out.bak-$(date +%Y%m%dT%H%M%SZ)`) — the cdk.out drift guard fails the
   gate if unbaselined copies exist.
4. **Do NOT run a portal deploy (deploy-portal.sh / deploy-infrastructure.sh /
   deploy-frontend.sh) while a component build is running.** Portal deploys
   regenerate `cdk.out` mid-build and the security gate (which runs AFTER the
   ~1h compile) fails on the drift guard, wasting the whole build. Sequence
   them: portal deploy fully finishes → move cdk.out aside → start the build.
5. **ALWAYS run the guard suite and confirm it is green before starting the
   build — never assume it.** This takes seconds and saves the whole build
   when a baseline is stale (a portal deploy having regenerated `cdk.out` is
   the classic cause):
   ```
   python3 -m pytest \
     test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py \
     test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py \
     -p no:cacheprovider --noconftest -q
   ```
   If the cdk.out drift guards fail, either move `cdk.out` aside (step 3) or
   add the new copies' sha256 entries to
   `test/backend-test/security/preservation/cdk_out_baseline.json` and
   `.../secrets_cdk_out_baseline.json`, re-run the guards, and only start the
   build once they pass. When practical, run the full preservation suite in
   the flask-app container too (see "Security preservation gate" below).
6. Only then start the build.

## CRITICAL: test new on-device edge features on real hardware before committing

Any change that runs **on the edge device** (the LocalServer backend/frontend
under `src/`, the workflow engine, Triton/model handling, GStreamer pipelines,
camera sync, Greengrass IPC usage, output bindings, the on-device `docker-compose`,
recipes, or host scripts) **must be verified on a real device before it is
committed** — not only in unit tests or the flask-app container.

Rationale: edge behavior depends on hardware/runtime specifics that host and
container tests do not reproduce (Jetson GPU/TensorRT/CUDA, JetPack GLIBC,
Greengrass IPC/Nucleus, native `awscrt`/`aws-c-event-stream`, real cameras and
OPC UA/network peers, model load timing). Several production incidents got
through green unit tests and only surfaced on device (e.g. the `awscrt`
"Continuation ref count has gone negative" abort, the Triton model-load race,
the SecureTunneling GLIBC incompatibility, negative-coordinate graph clipping).

Required before committing an on-device change:

1. Build the affected component and deploy (or hot-patch) it to a real device
   of the matching arch (JP5 and/or JP6 — test on every arch the change
   touches; do not assume JP5 behavior implies JP6 or vice versa).
2. Exercise the actual feature end-to-end on the device (run the workflow, hit
   the endpoint, drive the camera/inference/output path — whatever the change
   affects) and confirm it works AND that the backend stays healthy (no crash,
   no container restart, no crash-loop) for a sustained period, not just at
   startup.
3. Only then commit, and state in the commit/PR what was verified on which
   device(s).

For fast iteration you may hot-patch the running container to validate before a
full rebuild, but the change is not "done" until it has been verified on device
from a real built+deployed component. Unit tests and container runs are
necessary but **not sufficient** to call an edge feature working.

## Security preservation gate: update baselines when you intentionally change a tracked file

The backend build runs a **security preservation gate**
(`test/backend-test/security/preservation/`) that pins the sha256 (or line
count) of security-relevant, "out-of-scope" files against golden baselines in
`test/backend-test/security/baselines/`. Any intentional edit to a tracked
file changes its hash and **fails the build gate** with e.g.:

```
preservation golden 'docker_baseline_out_of_scope.json' changed (F(X) != F'(X))
ERROR: backend unit tests / security audit gate failed
```

This is by design — it forces a conscious baseline update. When the change is
**intended**, recompute the affected hash(es) and update the golden; do NOT
weaken or delete the test.

Files/baselines this commonly affects:
- `src/docker-compose.yaml`, `src/backend/Dockerfile`, `src/frontend/Dockerfile`,
  `src/edgemlsdk/Dockerfile` -> `baselines/docker_baseline_out_of_scope.json`
  (sha256 per file).
- `src/backend/requirements.txt` -> `baselines/dependency_baseline_requirements.txt`
  (dependency list + line count).
- Dockerfile content baselines -> `baselines/docker_baseline_backend_Dockerfile.jp5_masked.txt`
  / `...jp6_masked.txt`.

How to fix (intended change):
1. Recompute the hash of each changed file exactly as the test does — a plain
   file sha256: `sha256sum <path>` (e.g. `sha256sum src/docker-compose.yaml`).
2. Update the matching value in the golden JSON/txt under
   `test/backend-test/security/baselines/` (edit just the changed entry; leave
   the others alone).
3. Re-run the preservation suite in the flask-app container before rebuilding:
   ```
   docker run --rm -v "$(pwd)":/repo -w /repo \
     -e PYTHONPATH=/repo/src/backend:/repo/test/backend-test \
     flask-app:latest bash -lc \
     'PY=$(command -v python3.11 || command -v python3.10); \
      $PY -m pip install --no-cache-dir --quiet pytest sarge testfixtures hypothesis; \
      $PY -m pytest test/backend-test/security/preservation -q -p no:cacheprovider'
   ```
   Note the interpreter differs by image: JP5 uses **python3.11**, JP6 uses
   **python3.10** (app deps live under `/usr/local/lib/python3.10` on the JP6
   image). The `command -v` shim above picks whichever exists.
4. Commit the baseline update alongside the change that caused it, and say in
   the commit which file's hash was rebaselined and why.

Do this proactively: whenever a change touches any preservation-tracked file
above, update its baseline in the same commit so the build gate never fails on
an intended edit.
