# Bugfix Requirements Document

## Introduction

The DefectDetectionApplication (DDA) backend is built and run on Python 3.9.
Python 3.9 reached end-of-life and no longer receives security patches, so every
build and deployment of the application ships an unsupported interpreter. This is
a security-compliance violation: known and future CVEs in the interpreter and its
standard library cannot be remediated.

Python 3.9 is currently pinned in many places across the codebase, including:

- `build-custom.sh` — passes `-y 3.9` to the edgemlsdk `build.sh` and runs the
  in-image backend test suite with `python3.9 -m pytest` / `python3.9 -m pip`.
- `src/edgemlsdk/build.sh` — defaults `python=3.9`.
- Backend Dockerfiles (`src/backend/Dockerfile.jp5`, `Dockerfile.jp6`,
  `Dockerfile`) — install `python3.9` / `python3.9-dev`, switch `python3` to
  3.9, fetch the 3.9 `get-pip.py`, run `python3.9 -m grpc_tools.protoc`, set
  `CMD ["python3.9", "app.py"]`, and run `python3.9 dlr_disable_phone_home.py`.
- EdgeML SDK Dockerfiles (`src/edgemlsdk/Dockerfile.jp5`, `Dockerfile.jp6`,
  `Dockerfile`) — set `Python3_*` paths to `python3.9` / `libpython3.9.so` /
  `python3.9` include dirs and build Triton's Python backend with
  `PYTHON_EXECUTABLE=/usr/bin/python3.9` and `PYBIND11_PYTHON_VERSION=3.9`.
- `src/backend/edge_ml1_p_camera_management/install_edgemlsdk.sh` — installs
  the Panorama wheel and other packages with `python3.9 -m pip`.
- `test/backend-test/conftest.py` — sets `PYTHONHOME=/usr/bin/python3.9`.
- Host/build provisioning scripts (`setup-build-server.sh`,
  `station_install/setup_station.sh`,
  `station_install/patch_docker_host_prereqs.sh`) and docs (`README.md`,
  `README_main.md`).

The intended target is Python 3.11 (the repo already carries
`Python-3-9-to-3-11-transform` and `python-upgrade` branches). The fix must
migrate the application to a supported, security-patched Python version while
keeping it fully functional on the JetPack 5 (L4T r35.x, Ubuntu 20.04), JetPack 6
(L4T r36.x, Ubuntu 22.04), and amd64 build targets, and while preserving all
native-dependency integrations (edgemlsdk/Panorama wheel, Triton Python backend,
gi/Aravis/GStreamer).

The lists above are the known pins; this requirement drives a thorough audit of
the repository so that no Python 3.9 dependency remains, not just the references
enumerated here.

### Bug Condition and Properties

The bug-condition methodology frames this fix as follows.

**Bug Condition `C(X)`** — identifies the inputs/configurations that trigger the
defect. Here the "input" is any build, runtime, test, or provisioning path of the
DDA system:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type SystemArtifact   // a build script, Dockerfile, runtime
                                     // process, test invocation, install script,
                                     // or doc that selects a Python interpreter
  OUTPUT: boolean

  // True when the artifact depends on, installs, selects, or runs an
  // unsupported (end-of-life) Python 3.9 interpreter.
  RETURN dependsOnPython(X, "3.9")
END FUNCTION
```

**Fix Property `P`** — desired behavior for all buggy inputs after the fix `F'`:

```pascal
// Property: Fix Checking - Supported Python interpreter
FOR ALL X WHERE isBugCondition(X) DO
  result ← F'(X)
  ASSERT dependsOnPython(result, "3.11")
     AND NOT dependsOnPython(result, "3.9")
     AND isSecuritySupported(interpreterOf(result))
END FOR
```

**Preservation Property** — for every artifact that does NOT depend on Python
3.9, the fixed system behaves identically to the original system `F`:

```pascal
// Property: Preservation Checking - No functional regressions
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

In addition, the externally observable behavior of the application (camera/Aravis
capture, GStreamer/Triton inference, the FastAPI app and its endpoints,
workflows, and the concurrent-camera-stream-viewing feature) must remain
unchanged after the interpreter is upgraded.

- **F**: the original system, built and run on Python 3.9.
- **F'**: the fixed system, built and run on the supported target Python 3.11.

## Bug Analysis

### Current Behavior (Defect)

The application is built, tested, deployed, and run on Python 3.9, which is
end-of-life and no longer receives security patches.

1.1 WHEN the build runs `build-custom.sh` THEN the system invokes the edgemlsdk
build with `-y 3.9` and runs the in-image backend tests with `python3.9 -m
pytest` / `python3.9 -m pip`, producing an image whose interpreter is the
unsupported Python 3.9.

1.2 WHEN `src/edgemlsdk/build.sh` runs without an explicit `-y` argument THEN the
system defaults to `python=3.9` and builds the EdgeML SDK and Triton Python
backend against Python 3.9.

1.3 WHEN a backend image is built from `Dockerfile.jp5`, `Dockerfile.jp6`, or
`Dockerfile` THEN the system installs Python 3.9, switches `python3` to 3.9,
fetches the 3.9 `get-pip.py`, generates gRPC stubs with `python3.9`, and sets the
container entrypoint to `python3.9 app.py`, so the running backend executes on
Python 3.9.

1.4 WHEN the EdgeML SDK images are built THEN the system configures the Triton
Python backend and native bindings against `python3.9` / `libpython3.9.so` /
`PYBIND11_PYTHON_VERSION=3.9`, producing Panorama and Triton artifacts bound to
Python 3.9.

1.5 WHEN `install_edgemlsdk.sh` installs the Panorama wheel and supporting
packages THEN the system installs them into the Python 3.9 environment via
`python3.9 -m pip`.

1.6 WHEN the backend test suite is collected THEN `conftest.py` sets
`PYTHONHOME=/usr/bin/python3.9`, binding test execution and the Triton python
backend interpreter lookup to Python 3.9.

1.7 WHEN a build server or edge station is provisioned THEN the provisioning
scripts install and select Python 3.9 (including 3.9 venv site-packages paths),
establishing Python 3.9 as the runtime for DDA components.

1.8 WHEN the deployed application runs on a JetPack 5, JetPack 6, or amd64 target
THEN the FastAPI app, camera/Aravis capture, GStreamer pipelines, and Triton
inference all execute under the unsupported Python 3.9 interpreter, leaving
interpreter and standard-library CVEs unpatchable.

### Expected Behavior (Correct)

The application is built, tested, deployed, and run on the supported target
Python 3.11, with no remaining dependency on Python 3.9.

2.1 WHEN the build runs `build-custom.sh` THEN the system SHALL invoke the
edgemlsdk build targeting Python 3.11 and run the in-image backend tests under
the Python 3.11 interpreter, producing an image whose interpreter is Python 3.11.

2.2 WHEN `src/edgemlsdk/build.sh` runs without an explicit `-y` argument THEN the
system SHALL default to Python 3.11 and build the EdgeML SDK and Triton Python
backend against Python 3.11.

2.3 WHEN a backend image is built from `Dockerfile.jp5`, `Dockerfile.jp6`, or
`Dockerfile` THEN the system SHALL install and select Python 3.11, fetch the
matching pip, generate gRPC stubs under Python 3.11, and set the container
entrypoint to run `app.py` on Python 3.11.

2.4 WHEN the EdgeML SDK images are built THEN the system SHALL configure the
Triton Python backend and native bindings against Python 3.11 (interpreter,
include dir, `libpython3.11.so`, and `PYBIND11_PYTHON_VERSION=3.11`), producing
Panorama and Triton artifacts that load under Python 3.11.

2.5 WHEN `install_edgemlsdk.sh` installs the Panorama wheel and supporting
packages THEN the system SHALL install them into the Python 3.11 environment and
the Panorama wheel SHALL import successfully under Python 3.11.

2.6 WHEN the backend test suite is collected THEN the interpreter lookup
(`PYTHONHOME` / Triton python backend path) SHALL resolve to Python 3.11, and the
in-image test suite SHALL run to completion under Python 3.11.

2.7 WHEN a build server or edge station is provisioned THEN the provisioning
scripts SHALL install and select Python 3.11 (including any venv/site-packages
paths) for DDA components.

2.8 WHEN the repository is audited for Python 3.9 references THEN the system SHALL
contain no remaining Python 3.9 pin in build scripts, Dockerfiles, install
scripts, test configuration, provisioning scripts, or documentation that affects
how the application is built or run.

2.9 WHEN the deployed application runs on a JetPack 5, JetPack 6, or amd64 target
THEN the FastAPI app, camera/Aravis capture, GStreamer pipelines, and Triton
inference SHALL execute under the supported, security-patched Python 3.11
interpreter.

### Unchanged Behavior (Regression Prevention)

All existing functionality must continue to work after the interpreter upgrade.
For every behavior that does not depend on the Python 3.9 pin, the fixed system
must behave identically to the original.

3.1 WHEN a camera is connected and streamed via Aravis/`gi` (GObject
introspection) THEN the system SHALL CONTINUE TO discover, configure, and capture
from the camera as it did on Python 3.9.

3.2 WHEN a GStreamer pipeline runs for video streaming or snapshot capture THEN
the system SHALL CONTINUE TO build and execute the pipeline and produce the same
output as on Python 3.9.

3.3 WHEN an inference request is served through the Triton Python backend THEN the
system SHALL CONTINUE TO load the model and return inference results equivalent to
those produced on Python 3.9.

3.4 WHEN a client calls the FastAPI application endpoints (including auth,
user/group management, and the existing API surface) THEN the system SHALL
CONTINUE TO return the same responses and status codes as on Python 3.9.

3.5 WHEN the concurrent-camera-stream-viewing feature is exercised (subscriptions,
heartbeats, staleness, and multi-viewer streaming) THEN the system SHALL CONTINUE
TO behave according to its existing specification.

3.6 WHEN defect-detection workflows and triggers (including DIO outputs and
model-conversion host scripts) run THEN the system SHALL CONTINUE TO execute and
produce the same results as on Python 3.9.

3.7 WHEN the build runs on a JetPack 5 (L4T r35.x / Ubuntu 20.04), JetPack 6 (L4T
r36.x / Ubuntu 22.04), or amd64 host THEN the system SHALL CONTINUE TO produce a
working, packaged deployment artifact for that target.

3.8 WHEN an existing TinyDB datastore, configuration file, or other persisted
data created by the Python 3.9 deployment is present THEN the system SHALL
CONTINUE TO read and use it without migration errors.
