# Requirements Document

## Introduction

This feature adds JetPack 7 (JetPack 7.1 and 7.2, Jetson Linux r38.x) build-target support to the DefectDetectionApplication build system, alongside the existing JP5 and JP6 targets. JetPack 7 targets Jetson Thor-class devices running Ubuntu 24.04 LTS (Linux kernel 6.8, SBSA-aligned aarch64).

The scope covers: a new backend Dockerfile (`src/backend/Dockerfile.jp7`) and edgemlsdk Dockerfile (`src/edgemlsdk/Dockerfile.jp7`) following the established JP5/JP6 patterns; base image selection for JetPack 7 (NVIDIA NGC publishes no `l4t-jetpack` r38.x tag as of early 2026, so the base image strategy must be selected and verified explicitly — NVIDIA guidance points to `nvcr.io/nvidia/cuda` CUDA 13.0.x Ubuntu 24.04 arm64 images, which do not bundle cuDNN/TensorRT the way `l4t-jetpack` bases do); build-script and gdk-config support for a new `aws.edgeml.dda.LocalServer.arm64JP7` Greengrass component; enablement of an Ubuntu 24.04 build server (existing builds run on Ubuntu 22.04 hosts); portal target-matrix/preflight awareness of the JP7 target; and the per-target test baselines (build baselines and security preservation baselines) that gate builds.

## Glossary

- **Build_System**: The collection of scripts and configuration that produce Greengrass components from this repository, including `build-custom.sh`, `gdk-config.json`, `src/edgemlsdk/build.sh`, and the Dockerfiles under `src/backend/` and `src/edgemlsdk/`.
- **Build_Script**: `build-custom.sh`, the script invoked by `gdk component build` that derives the build target from the Greengrass component name and orchestrates the Docker image builds and packaging.
- **EdgeMLSDK_Build_Script**: `src/edgemlsdk/build.sh`, the script that selects an edgemlsdk Dockerfile by JetPack version flag (`-j`).
- **JP7_Backend_Dockerfile**: The new `src/backend/Dockerfile.jp7` that builds the backend (flask-app) image for JetPack 7 devices.
- **JP7_EdgeMLSDK_Dockerfile**: The new `src/edgemlsdk/Dockerfile.jp7` that builds the edgemlsdk artifacts (Triton Python-backend stub and related debs/tars) for JetPack 7 devices.
- **JP7_Component**: The Greengrass component `aws.edgeml.dda.LocalServer.arm64JP7`, following the existing `arm64JP5`/`arm64JP6` naming convention.
- **JP7_Base_Image**: The pinned (digest-referenced) NVIDIA NGC container image used as the Docker build base for JetPack 7 images.
- **JP7_Device**: A Jetson device running JetPack 7.1 (Jetson Linux r38.4) or JetPack 7.2, on Ubuntu 24.04 LTS.
- **JP7_Build_Server**: A build host running Ubuntu 24.04 LTS registered to execute JP7 component builds.
- **Portal_Preflight**: The portal-side build validation logic (target matrix, architecture mapping, and component-identity checks) exercised by `test/backend-test/portal_builds/`.
- **GPU_ONNX_Runtime_Build**: The from-source GPU onnxruntime build performed by `edge_ml1_p_camera_management/install_onnxruntime_gpu.sh`, which links against the base image's CUDA/cuDNN/TensorRT.
- **Build_Baselines**: The per-target golden files under `test/backend-test/backend_jammy_pkgs/baselines/` and the edgemlsdk baseline suites (Dockerfile sha256 and masked-content baselines) that build gates verify.
- **Security_Preservation_Baselines**: The golden hashes under `test/backend-test/security/baselines/` that the security preservation gate pins for Dockerfiles and other tracked files.

## Requirements

### Requirement 1: JP7 Backend Dockerfile

**User Story:** As a build engineer, I want a backend Dockerfile for JetPack 7, so that the LocalServer backend image can be built for JetPack 7.1 and 7.2 devices.

#### Acceptance Criteria

1. THE Build_System SHALL provide a JP7_Backend_Dockerfile at `src/backend/Dockerfile.jp7` that builds the backend image for arm64 JetPack 7 devices.
2. THE JP7_Backend_Dockerfile SHALL reference the JP7_Base_Image with an image reference containing both a tag and a sha256 digest, and every other registry image referenced by a FROM instruction in the JP7_Backend_Dockerfile SHALL likewise be pinned by tag and sha256 digest, following the digest-pinning convention of `Dockerfile.jp5` and `Dockerfile.jp6`.
3. WHERE the JP7_Base_Image does not bundle a GPU library required by the backend build steps (cuDNN or TensorRT development packages), THE JP7_Backend_Dockerfile SHALL install that library at an exact pinned version (no floating or range version specifiers) during the image build.
4. THE JP7_Backend_Dockerfile SHALL install a Python interpreter version selected for JetPack 7 compatibility, SHALL accept that version through the same `PYTHON_VERSION` build-argument interface used by `Dockerfile.jp5` and `Dockerfile.jp6`, and SHALL route all bare `pip` installs to that interpreter, following the interpreter-selection pattern of `Dockerfile.jp5` and `Dockerfile.jp6`.
5. THE JP7_Backend_Dockerfile SHALL include the same set of application COPY instructions as `Dockerfile.jp6` (including `app.py` and `healthcheck.py`), SHALL define the container startup command to run `app.py` under the selected interpreter, and SHALL place `healthcheck.py` at the location the existing docker-compose backend healthcheck invokes.
6. WHEN the backend image build completes, THE JP7_Backend_Dockerfile SHALL have verified by import checks that every GPU-dependent Python package installed by the JP7_Backend_Dockerfile (each Python package that loads CUDA, cuDNN, or TensorRT libraries when imported) loads successfully under the interpreter it was installed for, and IF an import check fails, THEN THE JP7_Backend_Dockerfile SHALL fail the image build with the failing package name in the build log.
7. WHEN all import checks pass, THE JP7_Backend_Dockerfile SHALL continue executing the remaining build steps, and IF any other build step fails, THEN THE JP7_Backend_Dockerfile SHALL fail the image build (passing import checks is necessary but not sufficient for build success).
8. WHEN pip bootstrap for the selected interpreter completes, THE JP7_Backend_Dockerfile SHALL verify that a bare `pip` invocation targets the selected interpreter, and IF the bare `pip` invocation targets any other interpreter, THEN THE JP7_Backend_Dockerfile SHALL fail the image build with a message identifying the mis-targeted interpreter.
9. THE JP7_Backend_Dockerfile SHALL accept the Docker build arguments that the Build_Script passes when building `Dockerfile.jp6`, so that the Build_Script can build the JP7 backend image without a JP7-specific build-argument contract.

### Requirement 2: JP7 Base Image Selection and Verification

**User Story:** As a build engineer, I want an explicit, verified base image choice for JetPack 7, so that JP7 image builds do not silently depend on an NGC tag that does not exist.

#### Acceptance Criteria

1. THE Build_System SHALL document the selected JP7_Base_Image (registry path, tag, and digest) and the rationale for the selection in the JP7_Backend_Dockerfile and JP7_EdgeMLSDK_Dockerfile comments, and the digest documented in each Dockerfile's comments SHALL be identical to the digest in that Dockerfile's FROM reference.
2. IF NVIDIA NGC provides no `l4t-jetpack` image tag for Jetson Linux r38.x at implementation time, THEN THE Build_System SHALL use a `nvcr.io/nvidia/cuda` CUDA 13.0.x Ubuntu 24.04 arm64 image as the JP7_Base_Image.
3. WHEN a JP7 image build starts, THE Build_System SHALL resolve the JP7_Base_Image by its pinned digest before executing any subsequent build step, and IF the digest cannot be resolved from the registry, THEN THE Build_System SHALL fail the build with the registry error in the build log without executing any subsequent build step.
4. WHEN all cuDNN and TensorRT installation steps in the JP7_Backend_Dockerfile have completed, THE JP7_Backend_Dockerfile SHALL verify that the CUDA, cuDNN, and TensorRT versions present in the image match the version values pinned in the JP7_Backend_Dockerfile itself, and IF a version check fails, THEN THE JP7_Backend_Dockerfile SHALL fail the image build with a message naming the mismatched component and stating the expected and actual version values.
5. IF NVIDIA NGC provides an `l4t-jetpack` image tag for Jetson Linux r38.x at implementation time, THEN THE Build_System SHALL select that `l4t-jetpack` image as the JP7_Base_Image in preference to the `nvcr.io/nvidia/cuda` fallback.
6. THE JP7_Backend_Dockerfile and the JP7_EdgeMLSDK_Dockerfile SHALL reference the identical JP7_Base_Image digest in their FROM references, and IF the two digests differ, THEN THE Build_Baselines checks SHALL fail.

### Requirement 3: JP7 EdgeMLSDK Dockerfile and Build Script Selection

**User Story:** As a build engineer, I want an edgemlsdk Dockerfile for JetPack 7 selectable from the existing build script, so that the edgemlsdk artifacts are produced for JP7 with the same workflow used for JP5 and JP6.

#### Acceptance Criteria

1. THE Build_System SHALL provide a JP7_EdgeMLSDK_Dockerfile at `src/edgemlsdk/Dockerfile.jp7` that produces the edgemlsdk debs and tars for arm64 JetPack 7 devices and places them at the in-image `/debs` and `/tars` locations that the EdgeMLSDK_Build_Script extraction flow reads.
2. THE JP7_EdgeMLSDK_Dockerfile SHALL reference its base image by tag and sha256 digest, following the digest-pinning convention of the existing edgemlsdk `Dockerfile.jp5` and `Dockerfile.jp6` variants.
3. WHEN the EdgeMLSDK_Build_Script is invoked with the JetPack flag value "7", THE EdgeMLSDK_Build_Script SHALL select the JP7_EdgeMLSDK_Dockerfile and SHALL log the selected Dockerfile and base image.
4. WHEN the EdgeMLSDK_Build_Script is invoked with JetPack flag values "5" or "6", with no JetPack flag, or with any unrecognized JetPack flag value, THE EdgeMLSDK_Build_Script SHALL preserve its existing Dockerfile selection behavior for those values (`Dockerfile.jp5`, `Dockerfile.jp6`, or the standard `Dockerfile`).
5. WHEN the JP7 edgemlsdk image build completes, THE EdgeMLSDK_Build_Script SHALL extract the produced debs into `extracted-debs/debs/` and tars into `extracted-debs/tars/` using the same extraction flow as the JP5 and JP6 builds, and IF an extraction directory is empty after extraction, THEN THE EdgeMLSDK_Build_Script SHALL log a warning naming the empty directory.
6. IF the JP7 edgemlsdk image build fails, THEN THE EdgeMLSDK_Build_Script SHALL exit with a non-zero status and the build error in the log, and SHALL NOT perform extraction.

### Requirement 4: JP7 Greengrass Component Target

**User Story:** As a build engineer, I want a JP7 Greengrass component target derived from the component name, so that `gdk component build` produces a JP7 LocalServer component the same way it does for JP5 and JP6.

#### Acceptance Criteria

1. THE Build_System SHALL define a `gdk-config.json` component entry for the JP7_Component name `aws.edgeml.dda.LocalServer.arm64JP7` whose custom build command invokes the Build_Script with the JP7_Component name and component version, following the structure of the existing component entries.
2. WHEN the Build_Script receives a component name containing "JP7", THE Build_Script SHALL derive the JetPack 7 target, SHALL log the derived target in the build output, SHALL pass JetPack version "7" to the EdgeMLSDK_Build_Script, and SHALL select the JP7_Backend_Dockerfile for the backend image build.
3. WHEN the Build_Script receives a component name containing "JP5", "JP6", or "Nvidia", THE Build_Script SHALL preserve its existing target derivation for those names.
4. IF the component name contains none of "JP5", "JP6", "JP7", or "Nvidia", THEN THE Build_Script SHALL preserve its existing default (non-JetPack, CPU-only) target derivation.
5. WHEN the Build_Script builds the JP7 target, THE Build_Script SHALL enable the GPU_ONNX_Runtime_Build with a JetPack major version of 7 unless the build invocation disables it via the existing `ONNXRUNTIME_GPU=0` opt-out, matching the default-GPU and opt-out behavior of the JP5 and JP6 targets.
6. THE Build_System SHALL provide a Greengrass recipe variant for the JP7_Component that declares the component name `aws.edgeml.dda.LocalServer.arm64JP7`, following the naming and content conventions of the existing `recipe-arm64-jp5.yaml` and `recipe-arm64-jp6.yaml` variants.
7. WHEN the Build_Script builds the JP7 target, THE Build_Script SHALL place a recipe declaring the JP7_Component name into the greengrass-build recipes directory.
8. WHEN the JP7_Component build completes, THE Build_Script SHALL produce a component archive named with the JP7_Component name and host architecture that contains the backend image tar, the frontend image tar, `docker-compose.yaml`, and the staged host scripts, and SHALL copy the archive into the greengrass-build artifacts directory for the JP7_Component and version, using the same packaging flow as the JP5 and JP6 targets.
9. IF a saved Docker image tar or the packaged component archive fails its integrity verification during a JP7_Component build, THEN THE Build_Script SHALL fail the build with a message identifying the failed artifact, matching the JP5 and JP6 verification behavior.

### Requirement 5: GPU ONNX Runtime and Native Dependency Builds for JP7

**User Story:** As a build engineer, I want the from-source GPU onnxruntime build and native dependency installs to support JetPack 7, so that the JP7 backend image has working GPU inference and camera support.

#### Acceptance Criteria

1. WHEN `install_onnxruntime_gpu.sh` is invoked with a JetPack major version of 7, THE GPU_ONNX_Runtime_Build SHALL build onnxruntime with the CUDA and TensorRT execution providers against the JP7_Base_Image's CUDA and TensorRT libraries, using pinned JetPack 7 defaults for the onnxruntime version and CUDA architecture set (overridable through the script's existing environment variables), and SHALL install the produced aarch64 wheel into the interpreter selected in Requirement 1.4, replacing any previously installed CPU onnxruntime package under that interpreter.
2. IF the CUDA toolkit or the TensorRT development headers are not present in the JP7 build environment when the GPU_ONNX_Runtime_Build starts, THEN THE GPU_ONNX_Runtime_Build SHALL fail before compilation begins with an error message naming the missing prerequisite.
3. IF the GPU_ONNX_Runtime_Build fails for the JP7 target, THEN THE Build_System SHALL fail the image build with a non-zero exit status and the onnxruntime build error in the build log.
4. WHEN `install_aravis.sh` runs during a JP7 backend image build, THE Build_System SHALL complete the aravis source build on the Ubuntu 24.04 arm64 environment, with each build stage (dependency installation, build configuration, compilation, and installation) exiting with status 0.
5. IF any aravis build stage fails during a JP7 backend image build, THEN THE Build_System SHALL fail the image build with the failing stage's error in the build log.
6. WHEN `install_onnxruntime_gpu.sh` or `install_aravis.sh` is modified for JP7 support, THE Build_System SHALL preserve each script's existing behavior for the JP5 and JP6 targets unchanged, including the existing aarch64 handling for l4t environments.
7. WHEN the JP7 backend image build completes, THE JP7_Backend_Dockerfile SHALL have verified by an import check that the built onnxruntime module loads under the selected interpreter and reports both the CUDA and TensorRT execution providers as available, and IF the import check fails, THEN THE JP7_Backend_Dockerfile SHALL fail the image build.

### Requirement 6: Ubuntu 24.04 Build Server Enablement

**User Story:** As a build operator, I want JP7 builds to run on an Ubuntu 24.04 build host, so that the JP7 images can be built natively against the JetPack 7 userspace generation.

#### Acceptance Criteria

1. WHEN the JP7_Component build is executed on a JP7_Build_Server running Ubuntu 24.04 LTS on arm64, THE Build_System SHALL complete the build and produce the component archive and greengrass-build artifacts defined in Requirement 4.8.
2. THE Build_System SHALL document the JP7_Build_Server provisioning steps in the repository documentation, covering host prerequisites (Ubuntu 24.04 LTS on arm64 and the tooling required by the Build_System), Docker configuration, and registration for portal-dispatched builds, such that an operator following only the documented steps produces a host the portal accepts as registered for the JP7 target.
3. WHEN a JP7_Component build is dispatched by the portal, THE Portal_Preflight SHALL route the build to exactly one build server registered as capable of the JP7 target, and IF more than one capable build server is registered, THEN THE Portal_Preflight SHALL select exactly one of them for the dispatch.
4. IF a JP7_Component build is dispatched and no capable build server is registered, THEN THE Portal_Preflight SHALL fail the dispatch with a diagnostic naming the missing target capability before any build work starts.
5. IF the selected build server does not accept the dispatch within 60 seconds, or reports at dispatch time that it is no longer capable of the JP7 target, THEN THE Portal_Preflight SHALL fail the dispatch with the same missing-capability diagnostic as the no-registered-server case and SHALL leave no build in a running or partially started state.
6. WHILE another component build is running on the same host, THE Build_System SHALL NOT start a dispatched JP7_Component build on the JP7_Build_Server, and the dispatched build SHALL remain pending rather than fail.
7. WHEN the in-progress component build on the JP7_Build_Server completes, THE Build_System SHALL start at most one pending JP7_Component build on that host, preserving one-build-at-a-time execution per host.

### Requirement 7: Portal Target-Matrix Awareness of JP7

**User Story:** As a portal user, I want the portal's build target matrix and device-compatibility logic to recognize JP7, so that JP7 builds can be requested and JP7 components deploy only to compatible devices.

#### Acceptance Criteria

1. THE Portal_Preflight SHALL include the JP7 target in its supported target matrix with an architecture mapping of JP7 to arm64 and a component-identity mapping of JP7 to `aws.edgeml.dda.LocalServer.arm64JP7`, and SHALL leave the existing JP5, JP6, AMD64, and AMD64_NVIDIA matrix entries (their architecture mappings and component identities) unchanged.
2. WHEN a build request names the JP7 target and records the component identity `aws.edgeml.dda.LocalServer.arm64JP7`, THE Portal_Preflight SHALL pass the component-identity check in every supported execution mode, applying the same validation rules applied to the JP5 and JP6 component identities.
3. WHEN device compatibility is evaluated for a JP7_Component deployment, THE Portal_Preflight SHALL treat as compatible only devices recorded with a JetPack 7-specific architecture value (following the existing `arm64_jp5`/`arm64_jp6` naming, i.e. `arm64_jp7`) matched by exact name, SHALL treat devices recorded as `arm64_jp5` or `arm64_jp6` as incompatible with the JP7_Component, and SHALL NOT use the coarse arm64 platform (identical across JetPack releases) as the basis for compatibility.
4. WHEN device compatibility is evaluated for a JP5 or JP6 component deployment, THE Portal_Preflight SHALL treat devices recorded with the JetPack 7 architecture value as incompatible with those components by the same exact-name matching rule.
5. WHEN a build request names a target outside the supported target matrix (which after this change comprises JP5, JP6, AMD64, AMD64_NVIDIA, and JP7), THE Portal_Preflight SHALL reject the request before any build or publish work starts with a diagnostic naming the unsupported target, and the addition of JP7 SHALL NOT cause any previously rejected target or execution-mode combination to be accepted.
6. IF a build request names the JP7 target but records a component identity other than `aws.edgeml.dda.LocalServer.arm64JP7` (including a JP5 or JP6 component identity), THEN THE Portal_Preflight SHALL reject the request before any build work starts with a diagnostic naming the component-identity mismatch, matching the cross-wired-identity rejection behavior of the JP5 and JP6 targets.
7. IF device compatibility is evaluated for a JP7_Component deployment and a target device has no recorded architecture value, THEN THE Portal_Preflight SHALL treat that device as incompatible with the JP7_Component (failing closed) and SHALL surface a message indicating that the device's architecture must be recorded before the deployment can proceed.

### Requirement 8: JetPack 7.1 and 7.2 Device Support

**User Story:** As an operator of Jetson Thor devices, I want the JP7 component to run on both JetPack 7.1 and JetPack 7.2 devices, so that a single JP7 component covers the supported JetPack 7 releases.

#### Acceptance Criteria

1. THE Build_System SHALL produce one JP7_Component artifact per component version, with no separate JetPack 7.1 or JetPack 7.2 variants, deployable without modification to devices running JetPack 7.1 (Jetson Linux r38.4) and devices running JetPack 7.2.
2. WHEN the JP7_Component is deployed to a JP7_Device, THE JP7_Component SHALL start the backend and frontend containers using the existing docker-compose deployment flow and SHALL report the component as RUNNING only after the backend container has passed its docker-compose healthcheck within the existing 300-second healthcheck start budget.
3. WHEN the same JP7_Component artifact is deployed to a device running JetPack 7.1 and to a device running JetPack 7.2, THE JP7_Component SHALL reach the RUNNING state on both devices without any release-specific configuration change, rebuild, or manual intervention differing between the two releases.
4. IF the backend container fails to reach a healthy state on a JP7_Device within the 300-second healthcheck start budget, THEN THE JP7_Component SHALL report the deployment as failed rather than RUNNING, and the backend container logs SHALL retain the startup error for operator inspection.
5. IF the JP7 backend container fails to start on a JP7_Device because of a missing host library or driver interface, THEN THE Build_System SHALL document the host-side prerequisite in the JP7 deployment documentation, identifying the required host package or driver interface and the JetPack 7 release(s) on which it is required.

### Requirement 9: Test Baselines for JP7

**User Story:** As a build engineer, I want JP7 counterparts of the per-target build and security baselines, so that the existing build gates pass for JP7 builds and keep guarding the JP5 and JP6 targets.

#### Acceptance Criteria

1. THE Build_System SHALL provide a Build_Baselines file named `backend_Dockerfile.jp7.sha256.txt`, following the naming convention of the existing `backend_Dockerfile.jp5.sha256.txt` and `backend_Dockerfile.jp6.sha256.txt` baselines, whose content is the sha256 hex digest of the JP7_Backend_Dockerfile bytes at baseline capture time.
2. THE Build_System SHALL provide a JP7 counterpart, named by substituting the "jp7" token for the JetPack token in the existing file name, for each per-JetPack baseline file in the edgemlsdk baseline suites that has both a jp5 and a jp6 variant.
3. THE Build_System SHALL provide Security_Preservation_Baselines masked-content files `docker_baseline_backend_Dockerfile.jp7_masked.txt` and `docker_baseline_edgemlsdk_Dockerfile.jp7_masked.txt`, following the existing `docker_baseline_backend_Dockerfile.jp5_masked.txt` and `...jp6_masked.txt` convention, and SHALL include the JP7_Backend_Dockerfile and JP7_EdgeMLSDK_Dockerfile in the set of files the security preservation gate verifies.
4. WHEN the JP7 baselines are added, THE Build_System SHALL leave every existing JP5 and JP6 baseline file byte-for-byte unchanged.
5. WHEN the baseline verification suites run against a repository whose JP7_Backend_Dockerfile and JP7_EdgeMLSDK_Dockerfile are unchanged since their baselines were captured, THE Build_System SHALL report a passing result for every JP7 baseline check, regardless of any pre-existing JP5 or JP6 baseline check failures.
6. IF the content of the JP7_Backend_Dockerfile or the JP7_EdgeMLSDK_Dockerfile differs from the content recorded in its corresponding JP7 baseline, THEN THE Security_Preservation_Baselines SHALL cause the preservation gate to fail with a result identifying the mismatched file, and the gate SHALL continue to fail until the corresponding JP7 baseline is updated to match the changed file, matching the gate behavior for the JP5 and JP6 Dockerfiles.
