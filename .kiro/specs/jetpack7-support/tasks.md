# Implementation Plan: JetPack 7 Support

## Overview

Add the JP7 build target following the established per-target patterns: new JP7 Dockerfiles and build-script branches first, then the install scripts they invoke, then portal target-matrix and device-architecture awareness, then the test baselines that gate the new files, and finally documentation. Hours-long docker builds and on-hardware deployments are represented as optional smoke tasks; all required tasks are concrete repo edits verifiable by the offline unit/property suites under `test/backend-test/`.

## Tasks

- [x] 1. JP7 backend Dockerfile
  - [x] 1.1 Create `src/backend/Dockerfile.jp7`
    - Re-check NGC for an `l4t-jetpack` r38.x tag; if absent, pin `nvcr.io/nvidia/cuda` CUDA 13.0.x Ubuntu 24.04 arm64 by tag AND sha256 digest, `${BASE_REGISTRY}`-parameterized (record the checked-and-chosen rationale, registry path, tag, and digest in the header comment block, with the comment digest character-identical to the FROM digest)
    - Install cuDNN 9.x and TensorRT 10.x dev packages at exact pinned versions (no ranges), followed by the version verification layer comparing `nvcc`/`cudnn_version.h`/`NvInferVersion.h` against ARG-declared pins and failing with `ERROR: <component> version mismatch: expected <X> got <Y>`
    - Python 3.11 via deadsnakes for noble arm64 (fallback: jp5-style source build, documented in comments), `PYTHON_VERSION` build-arg interface, `update-alternatives`, single `get-pip.py` bootstrap, and the jp6-style bare-`pip` targeting gate
    - Same pip layer sequence as `Dockerfile.jp6` (pycairo, PyGObject, psutil, awscrt workaround + import check, requirements.txt, model-conversion reqs, setuptools caps); `ONNXRUNTIME_GPU` branch invoking `JETPACK_MAJOR=7 install_onnxruntime_gpu.sh` (default GPU) or the cp311 CPU wheel; JP5-style disabled vLLM hook (`VLLM_ENABLE=0`); aravis flow with g-ir-scanner rewritten to the noble system python; no JP6 cuda114/trt8 DLR stages
    - Consolidated GPU-dependent import check gate (per-package `python3 -c "import X"` steps naming the failing package; onnxruntime asserts CUDA and TensorRT execution providers when GPU-enabled)
    - COPY set identical to `Dockerfile.jp6` (including `app.py` and `healthcheck.py` at `/healthcheck.py`), `CMD ["python3", "app.py"]`, `install_edgemlsdk.sh` + dlr phone-home disable; accept the full jp6 build-arg superset (`OS`, `PLATFORM`, `PYTHON_VERSION`, `BASE_REGISTRY`, `ONNXRUNTIME_SPEC`, `ONNXRUNTIME_GPU`, `VLLM_*`)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.1, 2.2, 2.3, 2.4, 2.5, 5.7_

  - [ ]* 1.2 Write unit tests for JP7 backend Dockerfile conventions
    - Fixed-file parse tests: every FROM pinned by tag + sha256 digest; GPU library installs use exact `=version` pins; COPY-set equality with `Dockerfile.jp6` plus CMD/healthcheck placement; ARG superset accepted; header-comment digest equals FROM digest
    - _Requirements: 1.2, 1.3, 1.5, 1.9, 2.1_

- [x] 2. JP7 edgemlsdk Dockerfile and build script selection
  - [x] 2.1 Create `src/edgemlsdk/Dockerfile.jp7`
    - FROM the identical base-image digest as `src/backend/Dockerfile.jp7`, with the same header comment block documenting the selection; pinned cuDNN/TensorRT dev installs where the Triton build needs them
    - Python 3.11 via deadsnakes with `Python3_*` env pins so the Triton Python-backend stub links `libpython3.11`; CMake 3.x from the upstream tarball; Triton server version re-pinned to a CUDA 13 / Ubuntu 24.04-compatible release; noble gcc-13 toolchain
    - Produce the full `/debs` and `/tars` artifact set the extraction flow reads (PanoramaSDK.deb, aws-*.deb, gstreamer debs, openssl.deb, panorama.whl, triton-core.deb, triton-python-backend.deb, triton_installation_files.tar.gz)
    - _Requirements: 3.1, 3.2, 2.1, 2.6_

  - [x] 2.2 Add the `-j 7` branch to `src/edgemlsdk/build.sh`
    - `"7"` selects `Dockerfile.jp7` and logs the selected Dockerfile and base image; existing behavior for `5`, `6`, empty, and unrecognized values untouched; shared extraction flow and failure/exit semantics unchanged
    - _Requirements: 3.3, 3.4, 3.5, 3.6_

  - [ ]* 2.3 Write property test for edgemlsdk Dockerfile selection
    - **Feature: jetpack7-support, Property 1: EdgeMLSDK Dockerfile selection mapping**
    - New `test/backend-test/build_script/test_jp7_target_derivation_properties.py`: for any generated `-j` flag value, `build.sh` selects per the fixed table and logs the selection; stub `docker` on PATH records the `-f` argument, no image built; Hypothesis, `@settings(max_examples=100)` minimum
    - **Validates: Requirements 3.3, 3.4**

  - [ ]* 2.4 Write unit test for edgemlsdk build failure path
    - Stub `docker` forced to fail: `build.sh` exits non-zero with the error logged and performs no extraction; empty extraction directory logs a warning naming the directory
    - _Requirements: 3.5, 3.6_

- [x] 3. Build target derivation helper and build-custom.sh
  - [x] 3.1 Create `scripts/build-target-derivation.sh`
    - Sourceable helper with `derive_build_target COMPONENT_NAME` setting `IS_JP5/IS_JP6/IS_JP7/IS_X86_NVIDIA`, `JETPACK_ARG`, `BACKEND_DOCKERFILE` per the derivation table (JP7 -> `JETPACK_ARG=7`, `Dockerfile.jp7`; JP5/JP6/Nvidia rows unchanged; no token -> default CPU row), and `resolve_onnxruntime_gpu` honoring the existing `ONNXRUNTIME_GPU=0` opt-out
    - _Requirements: 4.2, 4.3, 4.4, 4.5_

  - [x] 3.2 Wire `build-custom.sh` to the helper and the JP7 target
    - Source the helper for target derivation, keeping all other behavior identical; JP7 logs the derived target, passes `-j 7` to the edgemlsdk build, exports `BACKEND_DOCKERFILE=Dockerfile.jp7`; `BACKEND_PYTHON_VERSION` derivation unchanged (JP7 falls through to the 3.11 default); GPU onnxruntime default condition gains `IS_JP7`; shared packaging, tar/zip integrity verification, and greengrass-build artifact copy unchanged
    - _Requirements: 4.2, 4.5, 4.8, 4.9_

  - [ ]* 3.3 Write property test for build target derivation
    - **Feature: jetpack7-support, Property 2: Build target derivation from component name**
    - In `test/backend-test/build_script/test_jp7_target_derivation_properties.py`: for any generated component name, sourcing the helper yields exactly the derivation-table row for its token (JP7/JP6/JP5/Nvidia/none), and for any GPU-default target `ONNXRUNTIME_GPU=0` in the environment overrides the default; Hypothesis, min 100 examples
    - **Validates: Requirements 4.2, 4.3, 4.4, 4.5**

- [ ] 4. Component identity, recipe, gdk config, and portal build scripts
  - [x] 4.1 Create `recipe-arm64-jp7.yaml`
    - Copy of `recipe-arm64-jp6.yaml` with `ComponentName: "aws.edgeml.dda.LocalServer.arm64JP7"` and `StationName: "DDA_Station_ARM64_JP7"`; accessControl policy blocks (including `mqttproxy:2`) and lifecycle content per the jp5/jp6 conventions
    - _Requirements: 4.6_

  - [x] 4.2 Add the JP7 component entry to `gdk-config.json`
    - Entry for `aws.edgeml.dda.LocalServer.arm64JP7` with custom build command `bash build-custom.sh aws.edgeml.dda.LocalServer.arm64JP7 NEXT_PATCH`, following the existing entry structure
    - _Requirements: 4.1_

  - [x] 4.3 Update `portal-build.sh`, `gdk-component-build-and-publish.sh`, and `scripts/portal-build-agent.sh`
    - Argument parsing accepts `7|jp7|JP7|--jp7`; aarch64 case maps JetPack 7 to `recipe-arm64-jp7.yaml` + `arm64JP7` (the `cp "$RECIPE_FILE" recipe.yaml` step feeds the greengrass-build recipes directory); usage text updated; agent maps `BUILD_TARGET=JP7` to `./portal-build.sh aarch64 7`
    - _Requirements: 4.7, 6.1_

  - [ ]* 4.4 Write unit tests for the JP7 recipe and gdk config entry
    - Structure checks: gdk-config JP7 entry shape and build command; recipe declares the JP7 component name and follows the jp5/jp6 recipe conventions
    - _Requirements: 4.1, 4.6_

- [x] 5. Checkpoint - Ensure all tests pass
  - Run the offline suites touched so far (`test/backend-test/build_script/`, Dockerfile convention tests); ensure all tests pass, ask the user if questions arise.

- [x] 6. GPU ONNX runtime and aravis install scripts
  - [x] 6.1 Add the JetPack 7 case to `edge_ml1_p_camera_management/install_onnxruntime_gpu.sh`
    - Additive `7)` case: `ONNXRUNTIME_VERSION` default from the 1.23 line (exact tag chosen against the pinned TensorRT version), `CUDA_ARCHITECTURES=110` (Thor), both overridable via the existing environment variables; unrecognized-value error becomes "must be 5, 6 or 7"; prerequisite-check error text generalized to name the missing prerequisite and required dev package without claiming an l4t-jetpack base; JP5 gcc-10 override and all JP5/JP6 behavior untouched; wheel install/uninstall and CUDA+TensorRT provider verification semantics unchanged
    - _Requirements: 5.1, 5.2, 5.3, 5.6, 5.7_

  - [x] 6.2 Verify and minimally adjust `edge_ml1_p_camera_management/install_aravis.sh` for noble arm64
    - Audit each stage (dependency install, meson configure, ninja compile, install) for Ubuntu 24.04 arm64 deltas (apt package renames such as libgirepository, gtk-doc tooling, meson behavior); any change branched on the detected environment or superset-compatible so JP5/JP6 behavior including existing aarch64 static-lib handling is byte-preserved; if modified, rebaseline `install_aravis.sh.sha256.txt` in the same change per the builds.md protocol
    - _Requirements: 5.4, 5.5, 5.6_

  - [ ]* 6.3 Write unit tests for the onnxruntime install script JP7 case
    - JP7 defaults echoed by the case branch; fail-fast on missing CUDA toolkit / `NvInfer.h` naming the prerequisite; JP5/JP6 case branches unchanged
    - _Requirements: 5.2, 5.6_

- [x] 7. Portal build target matrix and dispatch
  - [x] 7.1 Add JP7 to `BUILD_TARGETS` in `build_domain.py`
    - `TARGET_JP7 = 'JP7'` with `component_name: aws.edgeml.dda.LocalServer.arm64JP7`, `recipe: recipe-arm64-jp7.yaml`, `required_arch: ARCH_ARM64`; existing four entries byte-unchanged; downstream helpers (`SUPPORTED_BUILD_TARGETS`, `is_supported_target`, `target_definition`, `validate_build_request`, `create_build_jobs`, dispatcher preflight) derive from the table with no further logic change
    - _Requirements: 7.1, 7.2, 7.5, 7.6, 6.3, 6.4_

  - [x] 7.2 Extend the `FROZEN_MATRIX` oracle in `test/backend-test/portal_builds/test_preflight_target_matrix_properties.py`
    - Add `"JP7": ("arm64", "aws.edgeml.dda.LocalServer.arm64JP7")` to the deliberately re-spelled oracle in the same change as 7.1 so the matrix-equality test enforces exactly five targets with no drift window
    - _Requirements: 7.1_

  - [ ]* 7.3 Write property test for target-matrix exactness with JP7
    - **Feature: jetpack7-support, Property 3: Target and mode matrix exactness with JP7**
    - Extend `test_preflight_target_matrix_properties.py`: for any supported target, mode, repo dir, and quotable ref, preflight preserves the frozen matrix exactly and a job recording its own target's component identity passes; Hypothesis, min 100 examples
    - **Validates: Requirements 7.1, 7.2**

  - [ ]* 7.4 Write property test for cross-wired component identity
    - **Feature: jetpack7-support, Property 4: Cross-wired component identity always fails**
    - For any ordered pair of distinct supported targets, a job for the first recording the second's component identity fails preflight in every mode with `component_identity_mismatch` before any build/publish work; Hypothesis, min 100 examples
    - **Validates: Requirements 7.6**

  - [ ]* 7.5 Write property test for unsupported target rejection
    - **Feature: jetpack7-support, Property 5: Unsupported targets keep failing**
    - For any target name outside the five-target set and any mode, the request is rejected with a diagnostic naming the unsupported target before any work; adding JP7 accepts no previously rejected combination; Hypothesis, min 100 examples
    - **Validates: Requirements 7.5**

  - [ ]* 7.6 Write property test for JP7 dispatch capability
    - **Feature: jetpack7-support, Property 7: JP7 dispatch requires a capable server**
    - Extend the fleet-validation property files: for any generated fleet state, a JP7 dedicated request is accepted only with an existing, running, arm64 server; otherwise rejected naming the missing capability with recorder seams empty; when capable, exactly one dispatch; Hypothesis, min 100 examples
    - **Validates: Requirements 6.3, 6.4**

  - [ ]* 7.7 Write dispatch edge example tests (JP7-parameterized)
    - Acceptance-timeout / capability-lost-at-dispatch fails with the missing-capability diagnostic leaving no running or partially started build; busy-host serialization keeps a dispatched JP7 build pending (not failed) and starts at most one pending build on completion
    - _Requirements: 6.5, 6.6, 6.7_

- [x] 8. Device architecture vocabulary (`arm64_jp7`)
  - [x] 8.1 Add `ARCH_ARM64_JP7` to the workflow_core catalogs
    - `ARCH_ARM64_JP7 = 'arm64_jp7'` added to `DEVICE_ARCHITECTURES` (NOT to `VLLM_ARCHITECTURES`) in `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog.py` and the vendored `src/backend/workflow_engine/vendor/workflow_core/catalog.py`
    - _Requirements: 7.3_

  - [x] 8.2 Add `arm64_jp7` to the portal backend functions
    - `devices.py` and `quick_setup.py` `TARGET_ARCHITECTURES` tuples; `deployments.py` `_component_arch` gains `'jp7' in suffix -> 'arm64_jp7'` before the legacy bare-arm64 fallback; `workflow_packaging.py` gains `ARCH_TO_PLATFORM['arm64_jp7']='aarch64'`, `ARCH_TO_LOCAL_SERVER_COMPONENT[ARCH_ARM64_JP7]='aws.edgeml.dda.LocalServer.arm64JP7'`, and a new `ARCH_TO_PUBLISH_TARGET` entry; vLLM supported-architecture functions deliberately unchanged; fail-closed null-architecture behavior and message preserved on JP7 paths
    - _Requirements: 7.3, 7.4, 7.7_

  - [x] 8.3 Add JP7 detection to `src/backend/workflow_engine/environment.py`
    - `"JP7" in component_path -> ARCH_ARM64_JP7`, checked before the JP6/JP5 checks
    - _Requirements: 7.3_

  - [x] 8.4 Add r38 detection to `station_install/setup_station.sh`
    - L4T major release r38 derives `arm64_jp7` from the same source as the existing jp4/jp5/jp6 derivation (`/etc/nv_tegra_release` or l4t apt sources)
    - _Requirements: 7.3_

  - [x] 8.5 Add JP7 token inference to the frontend deploy screen
    - The `JP7` component-name token requires `arm64_jp7` in the deploy-screen JetPack-token inference
    - _Requirements: 7.3_

  - [ ]* 8.6 Write property test for device-compatibility exact-name matching
    - **Feature: jetpack7-support, Property 6: Device compatibility is exact-name matching**
    - New `test/backend-test/portal_builds/test_jp7_device_compatibility_properties.py` against `deployments._component_arch`, the workflow_core catalog sets, the compatibility/gate predicates, and `workflow_engine.environment` derivation: for any recorded architecture value (fixed set, arbitrary strings, None) and any LocalServer variant, compatible iff exact-name equal; None always incompatible (fails closed) with the record-architecture message; jp5/jp6 devices incompatible with JP7 and vice versa; Hypothesis, min 100 examples
    - **Validates: Requirements 7.3, 7.4, 7.7**

- [x] 9. Checkpoint - Ensure all tests pass
  - Run the full offline backend test suite (`test/backend-test/`); ensure all tests pass, ask the user if questions arise.

- [x] 10. Test baselines and gates for JP7
  - [x] 10.1 Capture the four JP7 baseline files
    - `test/backend-test/backend_jammy_pkgs/baselines/backend_Dockerfile.jp7.sha256.txt` (sha256 hex of the backend Dockerfile bytes); `test/backend-test/edgemlsdk_pythondev/baselines/edgemlsdk_Dockerfile.jp7.sha256.txt` (re-scan the edgemlsdk suites to confirm this is the only family with both jp5 and jp6 variants); `test/backend-test/security/baselines/docker_baseline_backend_Dockerfile.jp7_masked.txt` and `docker_baseline_edgemlsdk_Dockerfile.jp7_masked.txt` (masked content); every existing JP5/JP6 baseline file byte-for-byte unchanged
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

  - [x] 10.2 Register the JP7 Dockerfiles in the preservation gate and add the digest-equality check
    - Security preservation suite tracked-file list and `docker_base_image_audit.py` in-scope Jetson Dockerfile set gain both JP7 Dockerfiles (per-FROM `${BASE_REGISTRY}` + digest-pin enforcement covers them); new backend-baseline-suite check parses the FROM digest of both JP7 Dockerfiles and fails if they differ; JP7 checks structured as independent per-file checks that pass regardless of unrelated jp5/jp6 baseline state
    - _Requirements: 9.3, 9.5, 9.6, 2.6_

  - [ ]* 10.3 Write property test for the baseline gate mutation round trip
    - **Feature: jetpack7-support, Property 8: Baseline gate mutation round trip**
    - New test alongside the baseline suites exercising the hash/masked comparison logic in a temp tree (real baselines never modified): any generated mutation of tracked JP7 Dockerfile content fails the check identifying the file; recomputing the baseline from mutated content makes it pass; unchanged content always passes regardless of unrelated jp5/jp6 baseline state; Hypothesis, min 100 examples
    - **Validates: Requirements 9.5, 9.6**

  - [ ]* 10.4 Write unit tests for JP7 baseline files
    - Existence and content correctness of all four JP7 baselines; existing jp5/jp6 baselines unchanged; JP7 checks pass standalone
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

- [x] 11. Documentation
  - [x] 11.1 Write the JP7 build server provisioning documentation
    - Extend the arm64 build-server docs: Ubuntu 24.04 LTS arm64 host, Docker Engine + buildx, a `docker-compose` command shim (noble ships only the `docker compose` plugin and `build-custom.sh` invokes `docker-compose`), zip/python3/gdk CLI, repository clone as `ubuntu`, and portal registration (dedicated-server flow recording `arch=arm64`); add a Ubuntu 24.04 AMI option or documented parameter to `edge-cv-portal/launch-arm64-build-server.sh`
    - _Requirements: 6.2_

  - [x] 11.2 Write the JP7 deployment documentation
    - Host-side prerequisites for JetPack 7.1/7.2 devices (required host packages or driver interfaces per release, as discovered); documented known limitations: DLR-only models unsupported on JP7 (per-model lazy-import degradation), vLLM disabled on JP7
    - _Requirements: 8.5_
    > **Amendment note** (see `.kiro/specs/onnx-compile-error-diagnostics/`): the DLR-only limitation makes the ONNX export path the designated vision route for JP7; its start-failure diagnostics are hardened by the referenced spec. There is still NO `jetson-xavier-jp7` **compile** target and none is added (`jetson-xavier-jp7` remains a packaging-target identifier only, in `packaging.py` `VLLM_ARCH_TO_TARGET` and `workflow_packaging.py`; SageMaker Neo cannot target CUDA 13 — its `cuda-ver` ceiling is 11.x, per the `jetson-xavier-jp6` comment in `COMPILATION_TARGETS`).

- [x] 12. Final checkpoint - Ensure all tests pass
  - Run the full offline test suite; ensure all tests pass, ask the user if questions arise.

- [ ] 13. Integration and smoke verification (optional, long-running)
  - [ ]* 13.1 Run the JP7 component build smoke on an Ubuntu 24.04 arm64 build server
    - `gdk component build` with the arm64JP7 entry (one build at a time per builds.md, ~1–2 h with GPU ORT), logging to `.gdk_build_jp7.log`; the in-build gates verify base digest resolution, version pins, pip targeting, import checks, ORT providers, aravis stages, and packaging integrity; re-check NGC for an `l4t-jetpack` r38.x tag before pinning and record the outcome in the Dockerfile comments
    - _Requirements: 1.1, 1.4, 1.6, 1.7, 1.8, 2.2, 2.3, 2.4, 2.5, 3.1, 3.5, 4.7, 4.8, 4.9, 5.1, 5.3, 5.4, 5.5, 5.7, 6.1_

  - [ ]* 13.2 Run a portal-dispatched JP7 build end-to-end
    - Registration -> dispatch -> publish on the registered noble server
    - _Requirements: 6.1, 6.3_

  - [ ]* 13.3 Deploy the JP7 artifact to JetPack 7.1 and 7.2 devices
    - Same artifact to both releases; component reaches RUNNING within the 300-second healthcheck budget without release-specific changes; exercise camera + ONNX GPU inference per the builds.md on-hardware protocol; record any discovered host prerequisites in the JP7 deployment docs
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP; tasks 13.1–13.3 are hours-long build/deploy smoke activities per the design's Testing Strategy and are not run in the unit suites
- Property tests use Hypothesis with a minimum of 100 iterations, each tagged `**Feature: jetpack7-support, Property N: <title>**`
- The FROZEN_MATRIX oracle (7.2) is updated in the same change as `build_domain.py` (7.1) so the equality test never sees a drift window
- Any modification to `install_aravis.sh` or `install_onnxruntime_gpu.sh` rebaselines the affected sha256 goldens in the same commit (builds.md protocol); every existing JP5/JP6 baseline stays byte-for-byte unchanged
- Checkpoints ensure incremental validation at reasonable breaks

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "3.1", "4.1", "6.1", "6.2", "7.1"] },
    { "id": 1, "tasks": ["2.1", "3.2", "4.2", "7.2", "8.1", "11.1"] },
    { "id": 2, "tasks": ["1.2", "2.2", "4.3", "6.3", "7.3", "8.2", "8.3"] },
    { "id": 3, "tasks": ["2.3", "4.4", "7.4", "8.4", "8.5", "10.1"] },
    { "id": 4, "tasks": ["2.4", "3.3", "7.5", "8.6", "10.2", "11.2"] },
    { "id": 5, "tasks": ["7.6", "10.3"] },
    { "id": 6, "tasks": ["7.7", "10.4"] },
    { "id": 7, "tasks": ["13.1", "13.2", "13.3"] }
  ]
}
```
