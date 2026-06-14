# Bugfix Requirements Document

## Introduction

On the `python_310` branch, the DDA backend container performs a live `pip install -r /dda_triton/model_conversion_requirements.txt` at container startup, inside `dda_triton.triton_setup.create_virtual_env` (invoked during the backend's Triton/model setup). This requires outbound network access to a package index at runtime.

On offline/air-gapped edge devices (e.g. the Jetson Xavier NX device `amazoncam-xavier-nx`) there is frequently no internet or DNS. The runtime pip install therefore fails to resolve the index host and cannot find the pinned distributions, so the model conversion dependencies are never installed and the Triton/model setup is left in a broken state. Device logs show name-resolution failures and `No matching distribution found for protobuf==4.25.8`.

The fix is to install the model conversion dependencies into the backend image at build time (baked into the Docker image) so they are already present at container startup, and to make the runtime `triton_setup` step succeed without any network access.

This spec is scoped ONLY to the offline runtime pip-install problem. The separate Greengrass Nucleus/LogManager version conflict and the device DNS/offline networking concerns are deployment/cloud-config issues and are out of scope.

## Bug Analysis

### Current Behavior (Defect)

When the backend container runs `dda_triton.triton_setup.create_virtual_env`, it shells out to `pip install -r /dda_triton/model_conversion_requirements.txt` against a remote package index. On a device with no network/DNS this fails.

1.1 WHEN the backend container starts its Triton/model setup on a device with no internet or DNS access THEN the system attempts a live `pip install -r /dda_triton/model_conversion_requirements.txt` that fails with a name-resolution error (`[Errno -3] Temporary failure in name resolution`)

1.2 WHEN the runtime pip install runs and cannot reach the package index THEN the system reports `Could not find a version that satisfies the requirement protobuf==4.25.8` / `No matching distribution found for protobuf==4.25.8` and the dependency install returns a non-zero exit status

1.3 WHEN the runtime pip install fails THEN the model conversion dependencies (setuptools, wheel, meson, grpcio==1.56.2, grpcio-tools==1.51.1, protobuf==4.25.8, requests==2.32.3, opencv-python, urllib3==2.2.3, scikit-learn==1.0.2, numpy==1.24.3) are absent from the container's Python environment and the model/Triton setup cannot complete correctly

### Expected Behavior (Correct)

The model conversion dependencies are baked into the backend image at build time, so they are already importable at container startup and the runtime setup requires no network access.

2.1 WHEN the backend container starts its Triton/model setup on a device with no internet or DNS access THEN the system SHALL complete the setup successfully without attempting any network-dependent install at runtime

2.2 WHEN the backend image is built THEN the system SHALL install the pinned model conversion dependencies (setuptools, wheel, meson, grpcio==1.56.2, grpcio-tools==1.51.1, protobuf==4.25.8, requests==2.32.3, opencv-python, urllib3==2.2.3, scikit-learn==1.0.2, numpy==1.24.3) into the image's Python environment

2.3 WHEN the Triton/model setup runs at container startup THEN the system SHALL find all model conversion dependencies already present and importable, so that model conversion can proceed offline

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the backend container runs on a device that does have network access THEN the system SHALL CONTINUE TO have all model conversion dependencies available and the Triton/model setup SHALL CONTINUE TO complete successfully

3.2 WHEN the Triton/model setup runs THEN the system SHALL CONTINUE TO copy the model conversion files and resources to their destination folders (the `cp_model_conversion_files` behavior) as it does today

3.3 WHEN the backend image is built THEN the system SHALL CONTINUE TO install the existing backend `requirements.txt` dependencies and all other build steps SHALL CONTINUE TO produce a working backend image across the standard, jp5, and jp6 build variants

3.4 WHEN model conversion runs after setup THEN the system SHALL CONTINUE TO use the same pinned dependency versions, preserving existing conversion and inference behavior
