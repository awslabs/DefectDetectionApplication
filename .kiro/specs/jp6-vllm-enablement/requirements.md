# Requirements Document

## Introduction

The vllm-triton-inference feature delivered the complete vLLM serving stack — portal registration/packaging/deployment gates, the device companion `vllm_runtime` (manager + loopback server), the Text_Generation_API, and the workflow `llm_inference` node — but the JetPack 6 device image ships with an empty vLLM install layer. `src/backend/Dockerfile.jp6` gates the install on `VLLM_SPEC` (default empty), its default `VLLM_INDEX_URL` points at the retired `pypi.jetson-ai-lab.dev/jp6/cu122` index (domain gone, cu122 channel discontinued), and no published Jetson vLLM wheel matches the current image stack (l4t-jetpack r36.3.0 / CUDA 12.2 / CPython 3.11 versus the available cp310/cu126 wheels). A plain JP6 build therefore produces a vLLM-free image, and `app.py`'s capability probe keeps devices in pre-feature mode.

This feature finishes the enablement using prebuilt Jetson AI Lab wheels. The JP6 base image is bumped from l4t-jetpack r36.3.0 (CUDA 12.2) to an r36.4.x tag (CUDA 12.6) so that the pinned wheel `vllm==0.10.2+cu126` installs from `https://pypi.jetson-ai-lab.io/jp6/cu126`. The wheels are cp310-only, so the DDA backend process (`app.py` with the in-process `vllm_runtime`) runs under CPython 3.10 on JP6 — while CPython 3.11 remains installed and fully functional in the same image, because the Triton Python-backend stub, the PanoramaSDK-adjacent tooling, and the `install_edgemlsdk.sh` installs are built against cp311. The JP6 image must therefore work with both interpreters simultaneously: 3.10 hosting the backend and vLLM, 3.11 hosting the cp311-linked Triton stub stack and tooling/venvs. The shared `src/backend` code must be source-compatible with both 3.10 and 3.11 so JP5 and x86 images (which stay on 3.11) do not regress. `VLLM_ENABLE` defaults to enabled with the pin baked in — no more empty `VLLM_SPEC`. JetPack 5 is explicitly out of scope: `Dockerfile.jp5` behavior stays byte-identical, `JP5_VLLM_ENABLED` stays `False`, portal architecture gating is unchanged.

Known risks the design must address: preserving the CUDA 11.4 cudart staging trick for Neo/DLR vision models across the base bump; verifying the Triton stack (built by the edgemlsdk image) against the CUDA 12.6 runtime base; verifying vLLM 0.10.2 still carries the classic `AsyncLLMEngine`/`AsyncEngineArgs` API surface that `vllm_runtime/manager.py` is written against; and ensuring the cu126 wheel's own torch pin does not clobber the DDA app's existing Python dependency surface. The result is validated on a 64 GB AGX Orin JP6 device with two models — facebook/opt-125m as a fast smoke test of the full register→package→publish→deploy→generate pipeline, and Qwen/Qwen2.5-7B-Instruct as a realistic workload — documented as manual hardware validation procedures where automation is impossible.

## Glossary

- **JP6_Image**: The LocalServer JetPack 6 (`arm64_jp6`) container image built from `src/backend/Dockerfile.jp6`.
- **JP6_Base_Image**: The `FROM` base of the JP6_Image: currently `nvcr.io/nvidia/l4t-jetpack:r36.3.0` (digest-pinned); target an `nvcr.io/nvidia/l4t-jetpack` r36.4.x tag providing CUDA 12.6, digest-pinned and registry-parameterized via `${BASE_REGISTRY}`.
- **DDA_Interpreter**: The CPython interpreter the DDA backend process (`app.py` and the in-process `vllm_runtime`) runs on inside the JP6_Image: CPython 3.10, selected via the `PYTHON_VERSION` build arg.
- **Tooling_Interpreter**: CPython 3.11 inside the JP6_Image: the interpreter the cp311-linked components (Triton_Python_Stub stack, `install_edgemlsdk.sh` installs, model-conversion tooling) require; remains installed and functional alongside the DDA_Interpreter.
- **Triton_Python_Stub**: The Triton Python-backend stub process built by the edgemlsdk JP6 image against libpython3.11; it executes the vision model templates (`lfv_model_template.py`, `marshal_for_capture_template.py`) which import numpy/cv2/lyra utility packages from its interpreter's environment.
- **vLLM_Layer**: The build-arg-gated `pip install` layer in `Dockerfile.jp6` controlled by `VLLM_ENABLE`, `VLLM_SPEC`, and `VLLM_INDEX_URL`.
- **vLLM_Pin**: The default value of `VLLM_SPEC`: `vllm==0.10.2+cu126`.
- **Jetson_Wheel_Index**: The pip index `https://pypi.jetson-ai-lab.io/jp6/cu126` hosting prebuilt cp310 aarch64 vLLM (and companion torch) wheels for JetPack 6 / CUDA 12.6.
- **Capability_Probe**: The `app.py` startup check `importlib.util.find_spec("vllm")` that activates the companion vLLM runtime and Text_Generation_API router only when the vllm package is importable by the running interpreter.
- **Companion_Runtime**: The device `vllm_runtime` package (`VllmRuntimeManager` + `VllmRuntimeServer`) that loads Triton_vLLM_Repositories and serves generate calls; written against the classic vLLM `AsyncLLMEngine`/`AsyncEngineArgs`/`SamplingParams` API.
- **Text_Generation_API**: The device FastAPI router (`endpoints/text_generation.py`) exposing non-streaming generate and SSE streaming over loaded vLLM models.
- **Interpreter_Audit**: The build gate `test/python_version_audit.py` (wired into `build-custom.sh`) that scans scoped build/runtime/provisioning/doc artifacts and fails the build on disallowed end-of-life interpreter references (the 3.9 series).
- **Docker_Preservation_Goldens**: The masked-bytes and default-refs baselines under `test/backend-test/security/baselines/` (`docker_baseline_backend_Dockerfile.jp6_masked.txt`, `docker_baseline_backend_Dockerfile.jp5_masked.txt`, the edgemlsdk variants, `docker_baseline_default_refs.json`, multistage/out-of-scope baselines) asserted by `test/backend-test/security/preservation/test_preservation_docker_*.py`.
- **Security_Audit_Gates**: The six audit gates run inside `build-custom.sh`: injection/deserialization, secrets/credentials/JWT, IAM/authorization, S3 bucket-squatting, Docker non-ECR base image, and dependency/supply-chain CVE.
- **CUDA114_Staging**: The `Dockerfile.jp6` multi-stage step that copies the CUDA 11.4 runtime libs from the NGC `l4t-cuda:11.4.19-runtime` image to `/usr/local/cuda-11.4/targets/aarch64-linux/lib` so Neo/DLR-compiled vision models resolve `libcudart.so.11.0`.
- **ONNX_GPU_Build**: The opt-in from-source onnxruntime-gpu build (`install_onnxruntime_gpu.sh`, `ONNXRUNTIME_GPU=1`) compiled against the base image's CUDA/TensorRT for the DDA_Interpreter.
- **Smoke_Model**: facebook/opt-125m — a tiny HuggingFace model for a fast end-to-end smoke of the full register→package→publish→deploy→generate pipeline.
- **Realistic_Model**: Qwen/Qwen2.5-7B-Instruct — a mid-size HuggingFace model exercising a realistic workload on the 64 GB AGX Orin target, deployed with engine_configuration guidance of `gpu_memory_utilization` ≈ 0.5–0.6 and an appropriate `max_model_len`.
- **Register_LLM_Flow**: The portal's existing vLLM model registration flow (HuggingFace source) delivered by vllm-triton-inference.
- **On_Hardware_Validation_Procedure**: A documented manual test procedure executed on a physical 64 GB AGX Orin JP6 device, following the repo convention that on-hardware steps are documented rather than automated.

## Requirements

### Requirement 1: vLLM Installed and Enabled by Default on the JP6 Image

**User Story:** As an edge device operator, I want a plain JP6 image build to produce a vLLM-capable image with a working pinned wheel, so that deployed LLM models actually serve on JetPack 6 devices without special build flags.

#### Acceptance Criteria

1. THE vLLM_Layer SHALL default `VLLM_ENABLE` to `1`, default `VLLM_SPEC` to the vLLM_Pin, and default `VLLM_INDEX_URL` to the Jetson_Wheel_Index, replacing the retired `pypi.jetson-ai-lab.dev/jp6/cu122` default.
2. WHEN the JP6_Image is built with default build args, THE vLLM_Layer SHALL install the vLLM_Pin from the Jetson_Wheel_Index such that `import vllm` succeeds under the DDA_Interpreter in the built image.
3. WHEN a JP6_Image containing the vLLM_Pin starts, THE Capability_Probe SHALL find the vllm package and activate the Companion_Runtime and the Text_Generation_API router.
4. THE vLLM_Pin SHALL expose the classic API surface the Companion_Runtime uses: `AsyncEngineArgs(**engine_args)`, `AsyncLLMEngine.from_engine_args`, `engine.generate(prompt, sampling_params, request_id)` yielding cumulative request outputs, `SamplingParams(**params)`, `shutdown_background_loop`, and the `errored` attribute — verified during design against the vLLM 0.10.2 source.
5. WHEN the vLLM_Layer installs the vLLM_Pin and its transitive dependencies (including the torch pin the Jetson_Wheel_Index supplies), THE DDA_Interpreter's dependency environment SHALL remain functionally consistent: every package the DDA backend imports at startup SHALL remain importable, and the built image's dependency resolution SHALL report no broken requirements for packages the DDA backend uses.
6. WHEN the JP6_Image is built with `VLLM_ENABLE=0`, THE vLLM_Layer SHALL install nothing and the built image SHALL run the pre-feature startup sequence via the Capability_Probe.
7. IF the vLLM_Layer's pip install fails during a build with `VLLM_ENABLE=1` and a non-empty `VLLM_SPEC`, THEN THE JP6_Image build SHALL fail with the pip error visible in the build output.

### Requirement 2: Dual-Interpreter JP6 Image (3.10 Backend, 3.11 Tooling)

**User Story:** As a build operator, I want the JP6 image to carry a working Python 3.10 and a working Python 3.11 simultaneously, so that the DDA backend loads the cp310-only vLLM wheel while every cp311-linked component keeps functioning.

#### Acceptance Criteria

1. THE JP6_Image SHALL contain both CPython 3.10 and CPython 3.11 as functional interpreters, with the DDA_Interpreter (3.10) selected via the `PYTHON_VERSION` build arg for the backend and the Tooling_Interpreter (3.11) installed alongside it.
2. THE JP6_Image container start command SHALL invoke `app.py` through the interpreter selected by `PYTHON_VERSION` rather than a hardcoded `python3.11` binary.
3. THE JP6_Image SHALL install the DDA backend's Python dependencies (the `requirements.txt` set, the awscrt pre-build, PyGObject/pycairo, the model-conversion dependency set, the panorama wheel and the `install_edgemlsdk.sh` package set, and the onnxruntime wheel) for the DDA_Interpreter, so the backend process resolves all of its imports under 3.10.
4. THE JP6_Image SHALL keep the Tooling_Interpreter functional for the cp311-linked components: the Triton_Python_Stub SHALL be able to import every package the vision model templates use (including numpy, opencv, and the lyra utility packages) under 3.11 in the built image.
5. WHEN `build-custom.sh` builds a JP6 component, THE build SHALL thread `PYTHON_VERSION=3.10` to the JP6 backend image build while the edgemlsdk build, JP5 builds, and x86 builds retain their existing 3.11 interpreter version, and THE in-image backend test and security gate execution SHALL run under the built image's DDA_Interpreter.
6. THE shared `src/backend` application code SHALL be source-compatible with CPython 3.10 and CPython 3.11, verified by an automated check of the `src/backend` tree for 3.11-only syntax and 3.11-only standard-library usage.
7. THE JP5 and x86 images SHALL continue to build and run on CPython 3.11 with no behavior change from this feature.
8. THE Interpreter_Audit SHALL continue to pass after this feature's changes, and THE Interpreter_Audit's scoped artifact set and patterns SHALL NOT be weakened (no scoped artifact removed, no pattern relaxed).

### Requirement 3: JP6 Base Image Bump with Vision-Stack Preservation

**User Story:** As an edge device operator, I want the JP6 base image bumped to r36.4.x (CUDA 12.6) without regressing any existing vision-stack function, so that the prebuilt cu126 vLLM wheel is compatible while every previously supported model type keeps working.

#### Acceptance Criteria

1. THE JP6_Image SHALL build `FROM` an `nvcr.io/nvidia/l4t-jetpack` r36.4.x tag providing CUDA 12.6, pinned by digest and registry-parameterized via `${BASE_REGISTRY}` exactly as the current `FROM` line is.
2. WHEN the JP6_Image is built from the new JP6_Base_Image, THE CUDA114_Staging SHALL stage the CUDA 11.4 runtime libraries at `/usr/local/cuda-11.4/targets/aarch64-linux/lib` with `libcudart.so.11` resolving via ldconfig, and the build SHALL fail if the library does not resolve.
3. WHEN the JP6_Image is built from the new JP6_Base_Image, THE Triton installation artifacts produced by the edgemlsdk build (the Triton debs and `triton_installation_files.tar.gz`) SHALL install successfully and `libtritonserver.so` SHALL remain loadable in the built image against the CUDA 12.6 base.
4. WHEN the JP6_Image is built from the new JP6_Base_Image with `ONNXRUNTIME_GPU=1`, THE ONNX_GPU_Build SHALL produce and install an onnxruntime wheel for the DDA_Interpreter whose available providers include the CUDA and TensorRT execution providers against the new base's CUDA and TensorRT.
5. WHEN the JP6_Image is built from the new JP6_Base_Image, THE aravis/GStreamer from-source build steps, the py3compile/py3clean disable-restore workaround, and the g-ir-scanner system-python shebang fix SHALL complete correctly against the new base's distro userspace.
6. THE JetPack 5 backend image (`src/backend/Dockerfile.jp5`), the JetPack 4 and x86 backend images, and the edgemlsdk JP5 image SHALL be byte-identical in behavior to their pre-feature content.
7. THE `JP5_VLLM_ENABLED` catalog flag SHALL remain `False`, `Dockerfile.jp5`'s `VLLM_ENABLE` default SHALL remain `0`, and THE portal architecture gating SHALL be unchanged with zero portal source modifications from this feature.

### Requirement 4: Build and Test Gates Stay Green

**User Story:** As a maintainer, I want every existing build gate and test baseline to pass with the changed JP6 image, so that the enablement carries no hidden regression.

#### Acceptance Criteria

1. WHEN the Docker_Preservation_Goldens are re-run after this feature's changes, THE baselines for `Dockerfile.jp6` (masked golden and any affected default-refs entries) SHALL be recaptured to reflect exactly this feature's intended changes, THE `Dockerfile.jp5` and edgemlsdk JP5 goldens SHALL remain byte-identical to their pre-feature content, and THE edgemlsdk JP6 golden SHALL be recaptured only if the design requires an edgemlsdk `Dockerfile.jp6` change.
2. WHEN the Security_Audit_Gates run after this feature's changes, THE six audit gates SHALL all pass, including the Docker non-ECR base image audit against the new digest-pinned `FROM` line.
3. WHEN the device backend test suite under `test/backend-test/` runs after this feature's changes, THE suite SHALL pass with its existing baselines.
4. WHEN the portal backend, workflow_core layer, and frontend test suites run after this feature's changes, THE suites SHALL pass unchanged.
5. WHEN the Interpreter_Audit runs as part of `build-custom.sh` after this feature's changes, THE audit SHALL pass with zero disallowed hits.

### Requirement 5: On-Hardware Validation with Two Test Models

**User Story:** As a QA engineer, I want a documented on-hardware validation procedure covering a fast smoke-test model and a realistic mid-size model on a 64 GB AGX Orin JP6 device, so that the default-enabled vLLM path is proven end-to-end before release.

#### Acceptance Criteria

1. THE feature SHALL provide an On_Hardware_Validation_Procedure covering the Smoke_Model through the complete pipeline: registration through the Register_LLM_Flow with a HuggingFace source, packaging, publish, deployment to the JP6 device, READY status propagation to the portal, a non-streaming generate round trip, and an SSE streaming session through the Text_Generation_API.
2. THE On_Hardware_Validation_Procedure SHALL cover the Realistic_Model through the same pipeline stages with an engine_configuration sized for the 64 GB AGX Orin target: `gpu_memory_utilization` in the 0.5–0.6 range and an appropriate `max_model_len`.
3. THE On_Hardware_Validation_Procedure SHALL document the Register_LLM_Flow portal steps for each model: navigating to the Models page's "Register LLM" action, selecting the HuggingFace source and entering the model ID, the exact engine settings values to enter in the engine settings section (per-model `gpu_memory_utilization` and `max_model_len`), submitting the form, and the expected resulting model record (the `LLM (vLLM)` type badge and the record's supported architectures).
4. THE On_Hardware_Validation_Procedure SHALL include executing an `llm_inference` workflow node against a deployed model on the JP6 device and verifying the generated text appears in the node's inference metadata output.
5. THE On_Hardware_Validation_Procedure SHALL include verifying vision-model coexistence on the JP6 device: a previously supported vision model and a vLLM model loaded and serving simultaneously.
6. THE On_Hardware_Validation_Procedure SHALL specify expected outcomes and failure triage steps for each stage (register, build, deploy, load, generate, stream, workflow).
7. WHERE registration seed scripts reduce manual effort, THE feature SHALL provide scripts or seed payloads that register the Smoke_Model and the Realistic_Model through the portal API with engine configurations sized for the 64 GB AGX Orin target.
