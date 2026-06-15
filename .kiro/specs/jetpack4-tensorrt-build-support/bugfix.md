> **⚠️ SUPERSEDED (root cause corrected).** This document hypothesized a missing
> Triton tensorrt backend. Investigation (see `PIVOT-FINDINGS.md`) proved the
> backend was never built and the real cause is **runtime TensorRT injection on
> the JP4.6 device** (`DOCKER_PROFILE`/`tegra`/`runtime: nvidia`/`tensorrt.csv`).
> Retained for historical context; follow the revised `tasks.md`.

# Bugfix Requirements Document

## Introduction

The DDA `LocalServer` component for JetPack 4.6 devices (component name `aws.edgeml.dda.LocalServer.arm64`, packaged from `recipe-arm64.yaml`, with no JP5/JP6 suffix) is built through the generic Ubuntu build path. That path produces a Triton inference server that contains only the `python` backend and no TensorRT backend. On a JetPack 4.6 Jetson (Xavier NX; L4T r32.7, TensorRT 8.2.1, CUDA 10.2, kernel 4.9), the TensorRT segmentation model therefore never loads and all inference hangs.

The build-target selection in `build-custom.sh` keys off the component name: a name containing `JP6` selects `Dockerfile.jp6`, `JP5` selects `Dockerfile.jp5`, and anything else falls through to the generic `Dockerfile`. The plain `aws.edgeml.dda.LocalServer.arm64` name has no JetPack token, so it falls through to the generic path. The JP5 and JP6 Dockerfiles build Triton with `--backend python` only but still ship a TensorRT backend because their base images (`nvcr.io/nvidia/l4t-jetpack:r35.4.1` / `r36.3.0`) include Triton + TensorRT preinstalled. The generic Dockerfile is based on `public.ecr.aws/ubuntu/ubuntu:${OS}` (plain Ubuntu), so it has neither a Jetson Triton nor a TensorRT backend.

JetPack 4.6 TensorRT inference worked before the JP5/JP6 build targets were introduced, when the base Dockerfile handled JP4.6. The repository's git history is squashed (the initial commit already reflects the post-JP5/JP6 structure), so the original JP4.6-capable base Dockerfile is not recoverable by reverting a commit and the build path must be reconstructed. This is a regression in JetPack 4 support.

**Observed evidence (live, on the device):**
- Running component `aws.edgeml.dda.LocalServer.arm64` version 1.0.116 (the `python_310` build), state RUNNING.
- The `backend_tegra_gpu_enabled` container (image `flask-app`) runs Python 3.11.9 with the nvidia runtime attached.
- Inside the container, `/opt/tritonserver/backends/` contains only `python`; there is no `tensorrt` backend, `import tensorrt` raises `ModuleNotFoundError`, and no `libnvinfer` is present.
- Triton models `base_model-bd-dda-test-segmentation` and `model-bd-dda-test-segmentation` are stuck in `state: LOADING`; the python-backend `marshal_model-...` is `READY`; logs repeat "Pipeline started, waiting for Triton inference" while model conversion itself succeeded.
- The host is correctly configured to inject TensorRT (tensorrt.csv lists `libnvinfer.so.8.2.1`; nvidia is the default runtime), but the image has no Triton TensorRT backend to use it.

**Scope considerations (not part of this bug, noted for the design phase):**
- The Python 3.11 migration itself is a separate concern (tracked by `python-3-11-security-upgrade`). TensorRT 8.2.1 on L4T r32.7 only provides Python 3.6 bindings, which may interact with the JP4 target's interpreter choice and should be considered during design.
- The offline `model_conversion` pip install is tracked separately by `triton-offline-dependency-install`.
- The Greengrass Nucleus/LogManager version conflict and device networking are out of scope.

This spec is focused on restoring a JetPack 4.6 (L4T r32.7) TensorRT-capable build target for the plain `arm64` component.

## Bug Analysis

### Current Behavior (Defect)

When the `aws.edgeml.dda.LocalServer.arm64` component (no JP5/JP6 suffix) is built and deployed to a JetPack 4.6 device:

1.1 WHEN `build-custom.sh` is invoked with a component name that contains no `JP5`/`JP6` token THEN the system selects the generic Ubuntu `Dockerfile` (and calls `edgemlsdk/build.sh` with no `-j` jetpack argument), producing a `flask-app` image whose Triton has only the `python` backend and no `tensorrt` backend
1.2 WHEN the resulting `flask-app` image runs on a JetPack 4.6 Jetson and Triton attempts to load a TensorRT segmentation model THEN the system leaves the model in `state: LOADING` indefinitely because `/opt/tritonserver/backends/` contains no `tensorrt` backend
1.3 WHEN a TensorRT model fails to reach `READY` THEN the system hangs all inference, repeatedly logging "Pipeline started, waiting for Triton inference" and never completing inference

### Expected Behavior (Correct)

2.1 WHEN `build-custom.sh` is invoked for the JetPack 4.6 `aws.edgeml.dda.LocalServer.arm64` component THEN the system SHALL select a JetPack 4.6 (L4T r32.7) build target that produces a `flask-app` image whose Triton includes the TensorRT backend
2.2 WHEN the resulting `flask-app` image runs on a JetPack 4.6 Jetson and Triton attempts to load a TensorRT segmentation model THEN the system SHALL have a `tensorrt` backend available under `/opt/tritonserver/backends/` so the model reaches `state: READY`
2.3 WHEN the TensorRT segmentation model reaches `READY` on a JetPack 4.6 device THEN the system SHALL complete inference rather than hang

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `build-custom.sh` is invoked with a component name containing `JP6` THEN the system SHALL CONTINUE TO select `Dockerfile.jp6` and build the JetPack 6 image as it does today
3.2 WHEN `build-custom.sh` is invoked with a component name containing `JP5` THEN the system SHALL CONTINUE TO select `Dockerfile.jp5` and build the JetPack 5 image as it does today
3.3 WHEN building for an `x86_64` host THEN the system SHALL CONTINUE TO use the generic Ubuntu `Dockerfile` and the `generic` compose profile, unaffected by the new JetPack 4 target
3.4 WHEN building any target THEN the system SHALL CONTINUE TO run the interpreter-version audit guard and backend unit tests, and SHALL CONTINUE TO package the component artifact as it does today
3.5 WHEN the `python` Triton backend is used by non-TensorRT models (e.g. `marshal_model-...`) THEN the system SHALL CONTINUE TO load those models to `READY` as it does today

## Bug Condition

**Bug Condition Function** — identifies the build/deploy inputs that trigger the bug:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type BuildTarget   // { componentName, deviceJetPack }
  OUTPUT: boolean

  // The plain arm64 component (no JP5/JP6 token) deployed to a JetPack 4.6
  // device is built through the generic Ubuntu path and lacks the TensorRT
  // Triton backend.
  RETURN  X.componentName CONTAINS "arm64"
      AND X.componentName DOES NOT CONTAIN "JP5"
      AND X.componentName DOES NOT CONTAIN "JP6"
      AND X.deviceJetPack = "4.6"
END FUNCTION
```

**Property Specification** — defines correct behavior for buggy inputs:

```pascal
// Property: Fix Checking — JetPack 4.6 image has the TensorRT backend
FOR ALL X WHERE isBugCondition(X) DO
  image  ← F'(X)                       // flask-app image produced by the build
  ASSERT tensorrtBackendPresent(image) // /opt/tritonserver/backends/tensorrt exists
  ASSERT tensorRTModelReachesReady(image) AND inferenceCompletes(image)
END FOR
```

Where:
- **F**: the original (unfixed) build path — generic Ubuntu Dockerfile, no TensorRT backend.
- **F'**: the fixed build path — a JetPack 4.6 (L4T r32.7) target that yields a Triton with the TensorRT backend, with `build-custom.sh` routing the plain `arm64` component to it.

**Preservation Goal:**

```pascal
// Property: Preservation Checking — non-JP4.6 targets are unchanged
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

This ensures the JP5, JP6, and x86_64/generic build paths behave identically before and after the fix.
