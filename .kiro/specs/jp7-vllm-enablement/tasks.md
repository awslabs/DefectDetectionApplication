# Implementation Plan: JP7 vLLM Enablement

## Overview

Implementation proceeds in two independent halves. The device half creates the from-source vLLM build script (`install_vllm_gpu.sh`), rewires `Dockerfile.jp7` (Torch_Pin layer, enabled-by-default vLLM layer, extended verification gates), adds the static convention test, and recaptures the JP7 Docker preservation goldens. The portal half adds `arm64_jp7` to every vLLM architecture gating set, the fit-check memory profile, and the frontend property-test generators, with the five design correctness properties implemented as property-based tests. Documentation (README, on-hardware validation procedure) lands last. No device Python runtime code changes; JP5/JP6 artifacts stay byte-identical.

## Tasks

- [x] 1. Create the vLLM build script (install_vllm_gpu.sh)
  - [x] 1.1 Create script skeleton with env contract, prerequisite checks, and ccache logging
    - Create `src/backend/edge_ml1_p_camera_management/install_vllm_gpu.sh` (executable, `set -e`) following the `install_onnxruntime_gpu.sh` structure
    - Env contract with defaults: `PYBIN=python3`, `VLLM_VERSION=v0.11.2`, `CUDA_ARCHITECTURES=11.0`, `CUDA_HOME=/usr/local/cuda`, `VLLM_BUILD_JOBS=min(nproc, 6)`
    - Fail-fast prerequisite checks before any long work, each naming the missing prerequisite: `${CUDA_HOME}`/nvcc present, `${PYBIN} -c "import torch"` succeeds with `torch.version.cuda` starting `13.` (record `TORCH_BEFORE`), git/pip/libpython3.11 dev headers
    - ccache logging: log compile-through-ccache when `command -v ccache` succeeds; log "building without compiler cache" and continue when absent — ccache absence never exits nonzero
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.8, 1.12_

  - [x] 1.2 Add checkout, existing-torch mode, and Classic-API compatibility patch
    - `git clone --depth 1 --branch "${VLLM_VERSION}"` into `WORK_DIR=/tmp/vllm-build`
    - Run `${PYBIN} use_existing_torch.py` so the build compiles against the installed Torch_Pin and the wheel metadata never demands a different torch
    - Guard that `vllm/engine/async_llm_engine.py` still matches the expected shim shape (`grep -q "AsyncLLMEngine = AsyncLLM"`); a guard miss exits nonzero naming the file
    - Overwrite the shim with the `AsyncLLMEngine(AsyncLLM)` compatibility subclass adding `shutdown_background_loop()` delegating to `shutdown()` (exact content per design Components §2 step 5)
    - _Requirements: 1.2, 2.5, 3.6_

  - [x] 1.3 Add wheel build, validation, staging, install, verification, and cleanup
    - Install vLLM build deps under `${PYBIN}`, then `VLLM_TARGET_DEVICE=cuda TORCH_CUDA_ARCH_LIST="${CUDA_ARCHITECTURES}" MAX_JOBS="${VLLM_BUILD_JOBS}" ${PYBIN} -m pip wheel . --no-deps --no-build-isolation -w dist/`
    - Wheel validation: exit nonzero naming the absent wheel if no `dist/vllm-*.whl`; verify aarch64 platform tag and cp311-compatible (py, abi) tags via `packaging.tags` (abi3 tags like `cp38-abi3` pass)
    - Stage the wheel to `/opt/vllm-wheels` before any cleanup; install with the `"numpy>=1.24,<2"` co-constraint
    - Verification from `cd /`: `import vllm` alone first (import failure exits nonzero immediately with the import error, before symbol checks); then per-symbol checks each exiting nonzero naming the missing symbol (`AsyncEngineArgs`, `SamplingParams`, `AsyncLLMEngine.from_engine_args`, `.generate`, `.shutdown_background_loop`)
    - Torch-unchanged check: installed torch version equals `TORCH_BEFORE`, else exit nonzero naming both versions
    - `rm -rf "${WORK_DIR}"` after staging and verification
    - _Requirements: 1.3, 1.6, 1.7, 1.9, 1.10, 1.11, 2.1_

- [x] 2. Rewire Dockerfile.jp7
  - [x] 2.1 Add the Torch_Pin layer with torch import gate
    - New layer immediately after the onnxruntime GPU layer, gated on `VLLM_ENABLE` (default `1`), installing `torch==2.9.0+cu130 torchvision==0.24.0 torchaudio==2.9.0 triton==3.5.0` with `--index-url https://download.pytorch.org/whl/cu130` (exact content per design Components §1)
    - Torch import gate: `import torch` succeeds and `torch.version.cuda` starts with `13.`; each check fails the build naming the failed check; skipped with logged message when `VLLM_ENABLE=0`
    - _Requirements: 2.1, 2.3, 2.6, 3.7_

  - [x] 2.2 Replace the disabled vLLM hook with the enabled-by-default script invocation
    - `VLLM_ENABLE` defaults to `1`; remove the legacy `VLLM_SPEC`/`VLLM_INDEX_URL` ARGs; no `VLLM_USE_V1` ENV (with the per-JetPack difference comment per design Components §3)
    - The vLLM layer RUN gates solely on `[ "$VLLM_ENABLE" = "1" ]` and invokes `./edge_ml1_p_camera_management/install_vllm_gpu.sh` with `VLLM_VERSION` passthrough; else-branch echoes the skip with the `VLLM_ENABLE` value
    - Script failure fails the RUN (and the build) with the script's error output in the log; no fallback install path
    - Layer order: onnxruntime GPU layer → torch layer → vLLM layer
    - _Requirements: 3.1, 3.2, 3.7, 3.8, 3.9_

  - [x] 2.3 Extend the import check gate and add the dependency-consistency gate
    - Extend the GPU import gate with separate named checks (each failing the build naming the package or symbol): `import vllm`, `import torch`, `AsyncEngineArgs`, `SamplingParams`, and `hasattr(AsyncLLMEngine, ...)` for `from_engine_args`, `shutdown_background_loop`, `errored` (per design Components §4)
    - Add the dependency-consistency RUN: startup-critical app imports (numpy, cv2, gi, fastapi, sqlalchemy, awscrt.mqtt, transformers), numpy `>=1.24,<2` constraint assertion, and `pip check`
    - Both gates skipped with logged messages when `VLLM_ENABLE=0`
    - _Requirements: 2.4, 2.7, 3.4_

- [x] 3. Add static convention tests for the JP7 build structure
  - [x] 3.1 Create test_jp7_vllm_layer.py
    - New `test/backend-test/backend_jammy_pkgs/test_jp7_vllm_layer.py`, text-only (`test_jp7_digest_equality.py` convention: no docker, no subprocess)
    - Dockerfile checks: `VLLM_ENABLE=1` default, no `VLLM_SPEC`/`VLLM_INDEX_URL` ARG, no `VLLM_USE_V1` ENV, sole-gate RUN condition with skip echo, layer order (onnxruntime → torch → vLLM → gates), exact torch pins + cu130 index URL, one named gate check per Classic_Engine_API symbol
    - Script checks: `install_vllm_gpu.sh` exists and is executable, env defaults (`VLLM_VERSION=v0.11.2`, `CUDA_ARCHITECTURES=11.0`), `min(nproc, 6)` job cap, staging to `/opt/vllm-wheels` before the work-dir `rm -rf`, `import vllm` check preceding per-symbol checks, `use_existing_torch.py` invocation, no reference from any non-JP7 Dockerfile
    - No sha256 baseline registered for the script (the `install_onnxruntime_gpu.sh` untracked treatment per design)
    - _Requirements: 1.1, 1.2, 1.4, 1.7, 1.9, 2.1, 2.6, 3.1, 3.2, 3.4, 3.7, 3.9, 5.3, 5.9_

- [x] 4. Checkpoint - Ensure device-side tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Recapture Docker preservation goldens
  - [x] 5.1 Recapture the JP7 sha256 and masked baselines
    - Update `test/backend-test/backend_jammy_pkgs/baselines/backend_Dockerfile.jp7.sha256.txt` to the sha256 of the final `src/backend/Dockerfile.jp7`
    - Recapture `test/backend-test/security/baselines/docker_baseline_backend_Dockerfile.jp7_masked.txt` as the masked form of the same content; verify the diff against the pre-feature masked baseline contains only the torch layer, vLLM layer, and gate lines
    - Verify `test_jp7_digest_equality.py` passes unchanged and JP5/JP6/JP4/x86 Dockerfiles and baselines have zero diff lines
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 6. Portal backend gating gains arm64_jp7
  - [x] 6.1 Add ARCH_ARM64_JP7 to VLLM_ARCHITECTURES in the catalog layer and vendored copy
    - Edit `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py` and `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py` with identical membership
    - Leave `JP5_VLLM_ENABLED = False`, the `arm64_jp4` exclusion, and all JP5/JP6 entries unchanged
    - _Requirements: 4.1, 4.7_

  - [x] 6.2 Add arm64_jp7 to vllm_supported_architectures() and VLLM_ARCH_TO_TARGET
    - Add `'arm64_jp7'` to `vllm_supported_architectures()` in `edge-cv-portal/backend/functions/packaging.py`, `greengrass_publish.py`, and `model_import.py`
    - Add `VLLM_ARCH_TO_TARGET['arm64_jp7'] = 'jetson-xavier-jp7'` in `packaging.py` so every architecture returned by `vllm_supported_architectures()` has a target entry
    - _Requirements: 4.1, 4.2, 4.4_

  - [x] 6.3 Add the arm64_jp7 device memory profile to vllm_fit_check.py
    - `DEVICE_MEMORY_PROFILE_BYTES['arm64_jp7'] = 120 * GIB` (128849018880 bytes; 128 GB nameplate × 30/32 derate per design)
    - _Requirements: 4.3_

  - [x] 6.4 Update gating-set test expectations that intentionally grow
    - Extend existing portal backend, workflow_core layer, and catalog test expectations asserting vLLM architecture-set membership to include `arm64_jp7`
    - Do not modify, delete, skip, or weaken any pre-existing JP5/JP6 gating expectation
    - _Requirements: 4.4, 5.6_

  - [ ]* 6.5 Write portal unit assertions for the new memberships
    - `VLLM_ARCHITECTURES` membership and layer/vendored equality; `vllm_supported_architectures()` membership in all three functions; `VLLM_ARCH_TO_TARGET` totality and the `jetson-xavier-jp7` value; `DEVICE_MEMORY_PROFILE_BYTES['arm64_jp7'] == 120 * GIB`; `JP5_VLLM_ENABLED is False`
    - _Requirements: 4.1, 4.2, 4.3, 4.7_

  - [ ]* 6.6 Write property test for the fit check Thor profile
    - **Property 1: Fit check evaluates arm64_jp7 with the Thor profile**
    - Hypothesis (portal backend), generating `gpu_memory_utilization` and estimate bytes against `evaluate_fit`; exactly one `arm64_jp7` finding, `budget_bytes == int(gpu_memory_utilization × 128849018880)`, `fits` verdict per the 1 GiB KV-cache reservation rule; minimum 100 iterations, tagged `Feature: jp7-vllm-enablement, Property 1`
    - **Validates: Requirements 4.3**

  - [ ]* 6.7 Write property test for llm_inference packaging acceptance
    - **Property 4: llm_inference packaging accepts arm64_jp7**
    - Hypothesis over generated workflow documents containing `llm_inference` nodes, validated for `arm64_jp7`: zero `V6_LLM_ARCH_UNSUPPORTED` findings; minimum 100 iterations, tagged `Feature: jp7-vllm-enablement, Property 4`
    - **Validates: Requirements 4.9**

  - [ ]* 6.8 Write property test for the vLLM architecture gate biconditional
    - **Property 2: vLLM architecture gate biconditional for arm64_jp7 devices**
    - Hypothesis on the backend gate and fast-check on `evaluateVllmArchGate` (extending `archCompatibility.property.test.ts`'s generators, which already include `arm64_jp7`), generating manifests/devices with and without `arm64_jp7` membership: pass exactly when the component's supported set contains `arm64_jp7`, else a miss entry with reason `ARCH_UNSUPPORTED` carrying the supported set; minimum 100 iterations, tagged `Feature: jp7-vllm-enablement, Property 2`
    - **Validates: Requirements 4.5, 4.10**

  - [ ]* 6.9 Write property test for JP5/JP6 gating invariance
    - **Property 5: JP5/JP6 gating verdicts are invariant under the arm64_jp7 extension**
    - Metamorphic test (Hypothesis/fast-check): run the gate twice, with supported sets with and without added `arm64_jp7`, and assert identical verdicts for all non-JP7 devices (jp4/jp5/jp6/x86/absent); minimum 100 iterations, tagged `Feature: jp7-vllm-enablement, Property 5`
    - **Validates: Requirements 4.7, 4.8**

- [x] 7. Frontend vLLM gating surfaces
  - [x] 7.1 Extend the vllm-publish property-test architecture arbitraries with arm64_jp7
    - **Property 3: Supported-architecture surfaces render arm64_jp7**
    - Add `'arm64_jp7'` to every `fc.constantFrom('arm64_jp6', 'arm64_jp5', 'x86_64')` architecture generator in `publishState.errors.property.test.ts`, `publishState.gating.property.test.ts`, and `publishState.session.property.test.ts` so the existing rendering/gating properties quantify over it (production TS is data-driven — no production change)
    - Run the extended suites plus `archCompatibility.property.test.ts` to confirm they pass with `arm64_jp7` coverage
    - **Validates: Requirements 4.6**
    - _Requirements: 4.6_

- [x] 8. Checkpoint - Ensure all portal and device test suites pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Documentation
  - [x] 9.1 Update the README JP7 sections
    - Replace the "vLLM is disabled" limitation with the enablement note: vLLM enabled by default via from-source build (`install_vllm_gpu.sh`, vLLM v0.11.2, `sm_110`, torch 2.9.0+cu130); verify a README-wide search finds zero remaining statements that vLLM is disabled/unsupported on JP7
    - State the expected JP7 build duration impact as a bounded range (placeholder to be filled from the measured Build_Server build during validation), document the `VLLM_ENABLE=0` opt-out and its effect, and note that JP7 builds run on the Build_Server one at a time
    - _Requirements: 5.7, 5.8_

  - [x] 9.2 Create the on-hardware validation procedure document
    - New `test/on-hardware/jp7_vllm_validation.md` mirroring `jp6_vllm_validation.md`: per-stage tables (register, package, publish, deploy, load, generate, SSE stream, workflow, coexistence) with exact Register LLM portal steps, observable expected outcomes, and failure triage for each stage
    - Smoke_Model facebook/opt-125m (`gpu_memory_utilization=0.3`, `max_model_len=2048`) and Realistic_Model Qwen/Qwen2.5-7B-Instruct (`gpu_memory_utilization=0.5`, `max_model_len=8192`) per the design's recorded engine configurations
    - Include the `llm_inference` workflow node step, vision-model coexistence step, and in-container checks (`torch.cuda.is_available()` true, engine load reaching loaded status) with triage distinguishing container-image from host driver-stack problems
    - _Requirements: 2.8, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

- [x] 10. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate the five universal correctness properties from the design (portal gating half); the image-build half uses static convention tests and in-build gates per the design's Testing Strategy
- The full `VLLM_ENABLE=1` Build_Server integration build (hours-long, one at a time per `.kiro/steering/builds.md`) and the on-hardware Thor validation are executed outside this task list; task 9.2 delivers the documented manual procedure and task 9.1 carries the duration placeholder they fill in
- Requirement 5.4/5.5 (Security_Audit_Gates) need no code task: the audits' scoped files are untouched (design Research #14) and the gates run inside `build-custom.sh`

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "6.1", "6.2", "6.3", "7.1", "9.1", "9.2"] },
    { "id": 1, "tasks": ["1.2", "2.2", "6.4", "6.5", "6.6", "6.7"] },
    { "id": 2, "tasks": ["1.3", "2.3", "6.8", "6.9"] },
    { "id": 3, "tasks": ["3.1", "5.1"] }
  ]
}
```
