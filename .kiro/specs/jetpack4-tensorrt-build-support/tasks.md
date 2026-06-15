# Implementation Plan

## Overview

This is a build-system bugfix. The correctness properties are about **build
routing/gating** (a pure function of `componentName` + `architecture`) and the
resulting **image contents** plus **on-device behavior**. Per the bugfix
methodology, we first surface counterexamples on the **unfixed** code
(Property 1, expected to FAIL), then capture the behavior that must not change
(Property 2, expected to PASS on unfixed code), then apply the fix, then re-run
both properties for fix-checking and preservation-checking, and finally run the
device/build-server integration tests.

### Execution-environment legend

- 🟢 **CI/this-device-OK** — the routing/gating logic is a pure function of
  `(componentName, architecture)` and runs on any host (including this Xavier NX).
  These extend `test/backend-test/host_scripts/test_docker_profile_selection.py`.
- 🟠 **BUILD-SERVER-ONLY** — requires an L4T r32.7 / aarch64 build environment
  with the TensorRT 8.2.1 + CUDA 10.2 dev toolchain. **Do NOT run full image
  builds on this Xavier NX device — they are far too slow.**
- 🔴 **DEVICE-ONLY** — requires a JetPack 4.6 Xavier NX with the NVIDIA Container
  Runtime + `tensorrt.csv` injection to load `.plan` engines to `READY`.

## Tasks

- [ ] 1. Write bug condition exploration test (routing/gating)
  - **Property 1: Bug Condition** - JetPack 4.6 tokenless-aarch64 build must route to the TensorRT-enabled target
  - 🟢 **CI/this-device-OK** — this property is the pure routing function and runs here.
  - **CRITICAL**: This test MUST FAIL on unfixed code — failure confirms the bug exists.
  - **DO NOT attempt to fix the test or the code when it fails** at this step.
  - **NOTE**: This test encodes the expected (fixed) behavior — it will validate the fix when it passes after implementation (task 3.5).
  - **GOAL**: Surface counterexamples proving the tokenless `arm64` component on an aarch64 host is NOT routed to a TensorRT-capable build.
  - **Scoped PBT Approach**: Generate `(componentName, architecture)` pairs constrained to the bug condition — name contains `arm64`, no `JP5`/`JP6` token, `architecture != x86_64` (aarch64). For these inputs assert the routing decision yields `JETPACK_ARG == "4"` AND `ENABLE_TENSORRT_BACKEND == 1`.
  - Implementation: extend `test/backend-test/host_scripts/test_docker_profile_selection.py` (or add a sibling `test_jetpack4_routing.py` in the same dir). Reuse the existing technique of extracting the real shell decision block and running it under `bash -c`:
    - Extract the token-detection block from `build-custom.sh` (the `IS_JP5`/`IS_JP6`/`JETPACK_ARG` `if/elif` chain) and run it with controlled `COMPONENT_NAME` and `ARCHITECTURE`; assert the resulting `JETPACK_ARG`.
    - Extract the `-j` branch from `src/edgemlsdk/build.sh` and run it with controlled `jetpack`/`ARCHITECTURE`; assert the resulting `ENABLE_TENSORRT_BACKEND`.
  - Bug Condition reference (from design): `isBugCondition(X)` = `name CONTAINS "arm64" AND NOT CONTAINS "JP5" AND NOT CONTAINS "JP6" AND architecture = "aarch64" AND deviceJetPack = "4.6"`.
  - Expected-behavior assertion the test encodes (from design Property 1): tokenless aarch64 → `JETPACK_ARG=4` + `ENABLE_TENSORRT_BACKEND=1`.
  - Run on UNFIXED code.
  - **EXPECTED OUTCOME**: Test FAILS — on unfixed `build-custom.sh` the tokenless aarch64 case yields `JETPACK_ARG=""` (no `-j`) and `build.sh` has no JP4 branch so `ENABLE_TENSORRT_BACKEND` is effectively `0`. This is the defect.
  - Document the counterexamples found, e.g. `route("aws.edgeml.dda.LocalServer.arm64", "aarch64") -> JETPACK_ARG="" (expected "4"), ENABLE_TENSORRT_BACKEND=0 (expected 1)`.
  - Also record (do NOT automate here) the already-captured device/build-server counterexamples that complete the bug picture, marked as non-CI evidence:
    - 🟠 Image-contents counterexample: a `flask-app` image from the generic path has only `/opt/tritonserver/backends/python`; `/opt/tritonserver/backends/tensorrt` is absent; no `libnvinfer`.
    - 🔴 On-device counterexample: on a JP4.6 Xavier NX the segmentation models stay in `state: LOADING` and logs repeat "Pipeline started, waiting for Triton inference".
  - Mark task complete when the routing/gating test is written, run, and its failure is documented.
  - _Requirements: 1.1, 1.2, 1.3_

- [ ] 2. Write preservation property tests (BEFORE implementing the fix)
  - **Property 2: Preservation** - JP5 / JP6 / x86_64 routing and gating are unchanged
  - 🟢 **CI/this-device-OK** — pure routing function; runs here.
  - **IMPORTANT**: Follow the observation-first methodology — capture the routing decisions the UNFIXED code makes for non-bug-condition inputs, then assert those exact decisions.
  - Observe on UNFIXED code and record:
    - `route("...arm64JP6", any-arch)` → `JETPACK_ARG="6"`, `BACKEND_DOCKERFILE="Dockerfile.jp6"`.
    - `route("...arm64JP5", any-arch)` → `JETPACK_ARG="5"`, `BACKEND_DOCKERFILE="Dockerfile.jp5"`.
    - `route("...arm64", "x86_64")` (tokenless on x86_64) → `JETPACK_ARG=""` (no `-j`), `ENABLE_TENSORRT_BACKEND=0`, `BACKEND_DOCKERFILE="Dockerfile"`, `generic` profile only.
    - `build.sh` with no `-j`, `-j 5`, `-j 6` → `ENABLE_TENSORRT_BACKEND=0`.
  - Write **property-based tests** (Hypothesis — the repo already uses it) over generated `(componentName, architecture)` pairs:
    - For every input containing `JP6` → `JETPACK_ARG="6"` / `Dockerfile.jp6`, regardless of arch (Req 3.1).
    - For every input containing `JP5` (and not `JP6`) → `JETPACK_ARG="5"` / `Dockerfile.jp5` (Req 3.2).
    - For every `x86_64` input → `ENABLE_TENSORRT_BACKEND=0`, no `-j`, generic `Dockerfile`, `generic` profile only — CPU/python-only (Req 3.3).
    - Generate case / token-placement variants to confirm `JP6`/`JP5` detection precedence stays ahead of the tokenless JP4 fallthrough.
  - Also assert (static, 🟢) that `build-custom.sh` still invokes the interpreter-version audit guard (`test/python_version_audit.py`) and the backend unit-test step (including `test_docker_profile_selection.py`), and still packages the artifact — these must remain wired (Req 3.4).
  - Preservation Requirements reference (from design): all `isBugCondition(X) == false` inputs (JP5, JP6, x86_64/generic) must be byte-for-byte unchanged; audit guard, backend unit tests, packaging, and the `python` backend loading non-TensorRT models to `READY` are unchanged (Req 3.5).
  - Run tests on UNFIXED code.
  - **EXPECTED OUTCOME**: Tests PASS — this establishes the baseline routing/gating behavior to preserve.
  - Mark task complete when the tests are written, run, and passing on unfixed code.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 3. Fix: re-enable the Triton TensorRT backend in the existing from-source compile (Option C, ARG-gated)

  - [ ] 3.1 SPIKE — confirm the L4T r32.7 TensorRT/CUDA dev toolchain and the Triton `build.py`/cmake wiring (DO THIS BEFORE editing the Dockerfile)
    - 🟠 **BUILD-SERVER-ONLY** — requires an L4T r32.7 / aarch64 build environment. **This is the central risk of the fix.**
    - **GOAL**: De-risk the Dockerfile change by confirming, on a real L4T r32.7 aarch64 host, the exact wiring the design left as representative.
    - Confirm how to source the TensorRT 8.2.1 + CUDA 10.2 **dev** packages into an Ubuntu 18.04 aarch64 image: NVIDIA L4T r32.7 apt repo URLs/keys vs. mounting JetPack-provided debs vs. requiring a preconfigured JP4.6 build host.
    - Confirm the exact dev-package names/versions (e.g. `cuda-toolkit-10-2`, `libnvinfer-dev`, `libnvinfer-plugin-dev`, `libnvonnxparsers-dev`).
    - Confirm the exact `build.py` flags and cmake variable names/paths for the `v2.45.0` tensorrt backend on this toolchain: `--enable-gpu`, `--backend tensorrt`, the CUDA compiler/root, and `TRITON_TENSORRT_INCLUDE_PATHS` / `TRITON_TENSORRT_LIB_PATHS` (or their real equivalents).
    - Confirm the actual source path of the built tensorrt backend under `build/` so the staging move lands it at `tritonserver/install/backends/tensorrt`.
    - **Decision gate**: If the `tensorrt` backend cannot compile on the r32.7 toolchain (missing dep / wrong cmake var), re-hypothesize the build-time TensorRT/CUDA wiring before proceeding. Record the confirmed package names, repo wiring, and cmake args for use in 3.2.
    - _Bug_Condition: isBugCondition(X) — tokenless arm64 on aarch64 for JP4.6 (L4T r32.7)_
    - _Expected_Behavior: source-built Triton `v2.45.0` with `--enable-gpu --backend tensorrt` produces an ABI-matched `tensorrt` backend_
    - _Requirements: 2.1, 2.2_

  - [ ] 3.2 Edit `src/edgemlsdk/Dockerfile` — add the `ENABLE_TENSORRT_BACKEND` ARG and the gated TensorRT/CUDA deps, `build.py` flags, and staging move (central change)
    - 🟠 **BUILD-SERVER-ONLY for verification** (edit anywhere; the build that exercises it must run on the build server, not this device).
    - Declare `ARG ENABLE_TENSORRT_BACKEND=0` near the top (defaulted off so x86_64 is unchanged).
    - (1a) Before the Triton clone, add a gated `RUN` that installs the L4T r32.7 TensorRT 8.2.1 + CUDA 10.2 dev packages **only when** `ENABLE_TENSORRT_BACKEND=1`, using the repo/package wiring confirmed in 3.1.
    - (1b) In the existing `build.py` invocation, build a gated `TRT_ARGS` (empty when disabled) that appends `--enable-gpu --backend tensorrt` plus the confirmed TensorRT/CUDA cmake args; keep the `--backend python` build and all existing cmake args. The default (disabled) call must be **byte-for-byte unchanged** from today.
    - (1c) Extend the backend staging `mv` so the `tensorrt` backend is moved to `tritonserver/install/backends` alongside `python` **only when** `ENABLE_TENSORRT_BACKEND=1`; the disabled path keeps the original single move.
    - Keep the Ubuntu 18.04 aarch64 base and the existing in-repo patches (`edgeml-triton-server.diff` / `edgeml-triton-core.diff`) exactly as-is. No new backend Dockerfile — the tensorrt backend ships inside `triton_installation_files.tar.gz`.
    - _Bug_Condition: isBugCondition(X) — generic source build compiled `--backend python` only, no tensorrt backend staged_
    - _Expected_Behavior: `/opt/tritonserver/backends/tensorrt` present in the produced image (design Property 1)_
    - _Preservation: when `ENABLE_TENSORRT_BACKEND=0` the `build.py` call and staging move are byte-for-byte the originals → x86_64 stays CPU/python-only (Req 3.3)_
    - _Requirements: 2.1, 2.2_

  - [ ] 3.3 Edit `src/edgemlsdk/build.sh` — add the `-j 4` branch
    - 🟢 **CI/this-device-OK to edit and unit-verify** the gating; full image build is 🟠 BUILD-SERVER-ONLY.
    - Initialize `ENABLE_TENSORRT_BACKEND=0`. Add an `elif [ "$jetpack" = "4" ]` branch that keeps `DOCKERFILE="Dockerfile"` (generic Ubuntu 18.04 aarch64 base) and sets `ENABLE_TENSORRT_BACKEND=1`.
    - Thread `--build-arg ENABLE_TENSORRT_BACKEND=$ENABLE_TENSORRT_BACKEND` into the `docker build` invocation.
    - Leave the `-j 5` / `-j 6` / no-`-j` branches unchanged (they keep `ENABLE_TENSORRT_BACKEND=0`).
    - _Bug_Condition: isBugCondition(X) — no `-j` arg today → generic build with TensorRT backend off_
    - _Expected_Behavior: `-j 4` → generic `Dockerfile` + `ENABLE_TENSORRT_BACKEND=1`_
    - _Preservation: no `-j`, `-j 5`, `-j 6` → `ENABLE_TENSORRT_BACKEND=0` (Req 3.1, 3.2, 3.3)_
    - _Requirements: 2.1_

  - [ ] 3.4 Edit `build-custom.sh` — detect the tokenless aarch64 (JP4.6) target and thread `-j 4`
    - 🟢 **CI/this-device-OK to edit and unit-verify** the routing.
    - Add `IS_JP4=0` and an `elif` after the `JP5` branch: `echo "$COMPONENT_NAME" | grep -q "arm64" && [ "$ARCHITECTURE" != "x86_64" ]` → `IS_JP4=1`, `JETPACK_ARG="4"`. Keep `JP6` and `JP5` branches ahead of it so detection precedence is preserved.
    - `edgemlsdk/build.sh` is already invoked with `-j "$JETPACK_ARG"` when non-empty, so `JETPACK_ARG="4"` threads through automatically.
    - Leave `BACKEND_DOCKERFILE` selection unchanged (JP4 keeps generic `Dockerfile`; tensorrt backend ships in the edgemlsdk tar).
    - Leave the architecture-based compose-profile selection, the audit guard, the backend unit-test step, and packaging exactly as-is.
    - Reuse `recipe-arm64.yaml` as-is — no new recipe.
    - _Bug_Condition: isBugCondition(X) — tokenless arm64 on aarch64 falls through to generic, no `-j`_
    - _Expected_Behavior: tokenless arm64 on aarch64 → `JETPACK_ARG="4"` → `-j 4` → `ENABLE_TENSORRT_BACKEND=1`_
    - _Preservation: `JP6`→6/`Dockerfile.jp6`, `JP5`→5/`Dockerfile.jp5`, tokenless x86_64→ no `-j`/`ENABLE_TENSORRT_BACKEND=0`/`generic` only; audit guard + unit tests + packaging unchanged (Req 3.1–3.5)_
    - _Requirements: 2.1, 3.3_

  - [ ] 3.5 Verify the bug condition exploration test now passes (fix-checking)
    - **Property 1: Expected Behavior** - tokenless-aarch64 routes to the TensorRT-enabled target
    - 🟢 **CI/this-device-OK** for the routing/gating property.
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test.
    - Run the routing/gating property test against the fixed `build-custom.sh` + `src/edgemlsdk/build.sh`.
    - **EXPECTED OUTCOME**: Test PASSES — tokenless aarch64 → `JETPACK_ARG="4"` + `ENABLE_TENSORRT_BACKEND=1` (confirms the routing half of the bug is fixed).
    - 🟠🔴 **Note**: The image-contents assertion (`/opt/tritonserver/backends/tensorrt` present) and the on-device `READY` / inference-completes checks are NOT runnable here — they are covered by the integration tests (task 5) on the build server / JP4.6 device.
    - _Requirements: 2.1, 2.2, 2.3 (Expected Behavior Properties from design Property 1)_

  - [ ] 3.6 Verify the preservation tests still pass (preservation-checking)
    - **Property 2: Preservation** - JP5 / JP6 / x86_64 routing and gating unchanged
    - 🟢 **CI/this-device-OK**.
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests.
    - Run the preservation property tests against the fixed code.
    - **EXPECTED OUTCOME**: Tests PASS — JP5/JP6 routing unchanged, every x86_64 input still maps to `ENABLE_TENSORRT_BACKEND=0` (CPU/python-only), audit guard + unit tests + packaging still wired (no regressions).
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 4. Checkpoint — all CI-runnable tests pass
  - 🟢 **CI/this-device-OK**.
  - Run the full backend unit-test suite that `build-custom.sh` invokes, including the extended `test/backend-test/host_scripts/test_docker_profile_selection.py` (and any new `test_jetpack4_routing.py`).
  - Confirm Property 1 (routing) now passes and Property 2 (preservation) still passes.
  - Confirm the interpreter-version audit guard still passes (no disallowed 3.9 references; JP4 keeps `PYTHON_VERSION=3.11`).
  - Ask the user if questions arise (e.g. the spike in 3.1 surfaces a toolchain blocker).

- [ ] 5. Integration tests (build-server + device — CANNOT be validated on this Xavier NX / CI host)
  - 🟠🔴 **BUILD-SERVER-ONLY and DEVICE-ONLY**. Do NOT run full image builds on this Xavier NX device — run them on the build server.

  - [ ] 5.1 Build-server: full JP4 build + image-contents fix-check
    - 🟠 **BUILD-SERVER-ONLY** (L4T r32.7 / aarch64 with TensorRT 8.2.1 + CUDA 10.2 dev toolchain).
    - Build the `edgemlsdk` image via `edgemlsdk/build.sh -j 4` (`ENABLE_TENSORRT_BACKEND=1`), then the `flask-app` image.
    - Assert `/opt/tritonserver/backends/` contains **both** `tensorrt` and `python` (fix-check, design Property 1).
    - _Requirements: 2.1, 2.2_

  - [ ] 5.2 Device: on a JP4.6 Xavier NX, confirm the TensorRT model loads and inference completes
    - 🔴 **DEVICE-ONLY** (JP4.6 Xavier NX, NVIDIA Container Runtime, `tensorrt.csv` injection of `libnvinfer.so.8.2.1`).
    - Deploy the rebuilt `aws.edgeml.dda.LocalServer.arm64` component; assert the segmentation models reach `state: READY` and inference completes (no more "waiting for Triton inference" hang).
    - Confirm the python-backend `marshal_model-...` still reaches `READY` (Req 3.5 preservation on the JP4 image).
    - _Requirements: 2.2, 2.3, 3.5_

  - [ ] 5.3 Build-server: regression build of JP5, JP6, and x86_64 targets
    - 🟠 **BUILD-SERVER-ONLY**.
    - Rebuild a JP5 and a JP6 component and an x86_64 build; confirm successful builds and byte-for-byte identical routing/profile behavior, and that x86_64 stays CPU/python-only (`generic` profile, only `backends/python`).
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

---

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "description": "Pre-fix property tests (run on unfixed code) + de-risking spike",
      "tasks": ["1", "2", "3.1"],
      "notes": "Tasks 1 and 2 are CI/this-device-OK and independent. Task 3.1 (spike) is BUILD-SERVER-ONLY and can start in parallel since it only investigates the toolchain."
    },
    {
      "wave": 2,
      "description": "Apply the fix (depends on spike findings for the Dockerfile change)",
      "tasks": ["3.2", "3.3", "3.4"],
      "notes": "3.2 depends on 3.1 (confirmed package/cmake wiring). 3.3 and 3.4 depend only on the design and can proceed once task 1 establishes the routing test; grouped here for a coherent fix commit."
    },
    {
      "wave": 3,
      "description": "Re-run properties for fix-checking and preservation-checking",
      "tasks": ["3.5", "3.6"],
      "notes": "CI-runnable. Depend on 3.3 + 3.4 (routing/gating edits). 3.5 re-runs task 1; 3.6 re-runs task 2."
    },
    {
      "wave": 4,
      "description": "Checkpoint",
      "tasks": ["4"],
      "notes": "Depends on 3.5 and 3.6 passing."
    },
    {
      "wave": 5,
      "description": "Build-server + device integration tests",
      "tasks": ["5.1", "5.2", "5.3"],
      "notes": "5.1 depends on 3.2 (TensorRT compile). 5.2 depends on 5.1 (needs the built image) and is DEVICE-ONLY. 5.3 depends on 3.2/3.4. None runnable on this Xavier NX / CI host."
    }
  ],
  "criticalPath": ["1", "3.1", "3.2", "5.1", "5.2"],
  "criticalPathRationale": "The routing exploration test (1) defines the contract; the spike (3.1) gates the central Dockerfile change (3.2), which is the only path to a TensorRT-capable image; the build-server build (5.1) produces that image and the device test (5.2) is the only place the fix can be fully validated (model reaching READY).",
  "parallelizable": [
    ["1", "2", "3.1"],
    ["3.3", "3.4"]
  ],
  "environmentConstraints": {
    "ci_this_device_ok": ["1", "2", "3.3", "3.4", "3.5", "3.6", "4"],
    "build_server_only": ["3.1", "3.2", "5.1", "5.3"],
    "device_only": ["5.2"]
  }
}
```

**Critical path:** `1 → 3.1 → 3.2 → 5.1 → 5.2`

## Notes

The routing/gating properties (tasks 1, 2, 3.5, 3.6, 4) are fully validatable on
this device. The actual TensorRT-backend **compile** (3.1, 3.2, 5.1, 5.3) needs
an L4T r32.7 / aarch64 build server, and the on-device READY/inference check
(5.2) needs a JP4.6 Xavier NX. The spike (3.1) deliberately leads the Dockerfile
edits because the dev-package/cmake wiring is the central risk of this fix.

- **Do NOT run full image builds on this Xavier NX device** — they are far too
  slow. All 🟠 BUILD-SERVER-ONLY tasks belong on the L4T r32.7 build server.
- The fix introduces **no new base image and no new recipe** — `recipe-arm64.yaml`
  is reused and the JP5/JP6 paths are untouched.
- x86_64 must stay byte-for-byte CPU/python-only: `ENABLE_TENSORRT_BACKEND`
  defaults to `0`, so the disabled `build.py` call and staging move are identical
  to today's.
