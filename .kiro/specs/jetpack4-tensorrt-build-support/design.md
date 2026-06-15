# JetPack 4 TensorRT Build Support Bugfix Design

## Overview

The DDA `LocalServer` component for JetPack 4.6 devices is the plain
`aws.edgeml.dda.LocalServer.arm64` component (packaged from `recipe-arm64.yaml`,
no `JP5`/`JP6` token). On an aarch64 host this component does **not** use a
special base image: it is built through the **generic** `edgemlsdk` path
(`src/edgemlsdk/Dockerfile`, `FROM public.ecr.aws/ubuntu/ubuntu:${OS}` with
`OS=18.04` — Ubuntu 18.04 aarch64). `build-custom.sh` sets `OS`/`IMAGE_VER` from
`/etc/lsb-release` on the build host and, finding no JetPack token in the
component name, calls `edgemlsdk/build.sh` with no `-j` argument and sets
`BACKEND_DOCKERFILE="Dockerfile"`.

The key correction to the earlier design: the generic `edgemlsdk` Dockerfile
**already compiles Triton from source**. It clones
`triton-inference-server/server` at tag `v2.45.0`, copies and applies the in-repo
patches `patches/edgeml-triton-server.diff` and `patches/edgeml-triton-core.diff`
(the server patch internally applies the core patch), and runs
`python3.11 build.py ... --backend python --build-dir=...`. There is **no**
`--enable-gpu` and **no** `--backend tensorrt`, so the build emits only the
`python` backend; the staging step then moves **only** `backends/python` into
the install. On a JetPack 4.6 Jetson (L4T r32.7, TensorRT 8.2.1, CUDA 10.2) the
TensorRT segmentation model can never load — it sits in `state: LOADING` forever
and inference hangs.

This from-source compile **with the in-repo patches on the Ubuntu 18.04 aarch64
image is the original, historically-working JetPack 4.6 solution** (confirmed by
the user). The regression is narrow: the TensorRT backend simply stopped being
built. The fix therefore does **not** introduce a new base image or a prebuilt
Triton release. It re-enables building Triton's `tensorrt` backend **in the same
source build** — add `--enable-gpu` and `--backend tensorrt` (with the
TensorRT/CUDA-locating cmake args) to the existing `build.py` invocation, and
extend the staging move to include `backends/tensorrt` alongside `backends/python`.

Two implementation realities drive the rest of this design:

1. **Build-time TensorRT/CUDA availability.** The Ubuntu 18.04 base has no CUDA
   or TensorRT, so compiling the `tensorrt` backend from source requires the
   L4T r32.7 TensorRT 8.2.1 + CUDA 10.2 dev headers and libraries present **at
   build time**. This is the central implementation detail and the item most
   likely to need a build-server spike.
2. **x86_64 preservation.** The generic `edgemlsdk`/`Dockerfile` path is **also**
   used for x86_64 builds (`build-custom.sh`: `x86_64` → generic Dockerfile +
   `generic` profile). x86_64 has no TensorRT, so enabling the `tensorrt`
   backend must be **gated** to the aarch64/JP4 case and MUST NOT affect x86_64,
   which must stay CPU/`python`-only and byte-for-byte unchanged.

The JP5 and JP6 paths (`Dockerfile.jp5` / `Dockerfile.jp6`, based on
`l4t-jetpack:r35.4.1` / `r36.3.0`, which already provide a TensorRT-capable
Triton) remain completely unchanged.

## Glossary

- **Bug_Condition (C)**: The build/deploy condition that triggers the bug — the
  plain `arm64` component (no `JP5`/`JP6` token) built on an aarch64 host for a
  JetPack 4.6 (L4T r32.7) device, whose source-built Triton is compiled with
  `--backend python` only and therefore lacks the `tensorrt` backend.
- **Property (P)**: The desired behavior — the JetPack 4.6 build produces a
  `flask-app` image whose source-built Triton has a `tensorrt` backend under
  `/opt/tritonserver/backends/tensorrt`, so TensorRT segmentation models reach
  `state: READY` and inference completes.
- **Preservation**: The JP5, JP6, and **x86_64/generic** build paths, the
  interpreter-version audit guard, the backend unit-test step, the packaging
  step, and the `python` Triton backend behavior for non-TensorRT models — all
  must remain unchanged. In particular, x86_64 MUST stay CPU/`python`-only.
- **`src/edgemlsdk/Dockerfile`**: The **generic** EdgeML SDK Dockerfile,
  `FROM public.ecr.aws/ubuntu/ubuntu:${OS}` (`OS=18.04`). Used for **both**
  x86_64 and the aarch64/JP4.6 target. Compiles Triton `v2.45.0` from source with
  the in-repo patches and (today) `--backend python` only.
- **`build.py`**: Triton's `triton-inference-server/server` build script invoked
  inside `src/edgemlsdk/Dockerfile`. `--backend <name>` selects which backends to
  build; `--enable-gpu` turns on GPU/TensorRT support; `--extra-backend-cmake-arg`
  / `--extra-core-cmake-arg` pass cmake variables (e.g. TensorRT/CUDA locations).
- **In-repo Triton patches**: `patches/edgeml-triton-server.diff` and
  `patches/edgeml-triton-core.diff`, copied into the cloned server tree and
  applied (server patch internally applies the core patch) to build with static
  `stdc++`. These are retained unchanged.
- **`ENABLE_TENSORRT_BACKEND`**: New Docker build ARG (default `0`) added to
  `src/edgemlsdk/Dockerfile`. When `1`, the build installs the L4T r32.7
  TensorRT/CUDA dev libs, adds `--enable-gpu --backend tensorrt` (+ cmake args)
  to `build.py`, and stages `backends/tensorrt`. Defaulted off so x86_64 is
  unchanged; set on only for the JP4/aarch64 build.
- **`build-custom.sh`**: The top-level build gate at the repo root. Sets
  `OS`/`IMAGE_VER` from `/etc/lsb-release`, detects the JetPack target from the
  component name (`JETPACK_ARG`), drives `edgemlsdk/build.sh`, selects
  `BACKEND_DOCKERFILE`, picks the docker-compose profile by architecture, runs
  the audit guard + backend unit tests, and packages the artifact.
- **`edgemlsdk/build.sh`**: Builds the `edgemlsdk` image. The `-j`/`jetpack`
  flag (`getopts j:`) selects the Dockerfile and (new) the
  `ENABLE_TENSORRT_BACKEND` build arg.
- **Triton tensorrt backend**: `libtriton_tensorrt.so` — a **C++** Triton
  backend that loads serialized `.plan` TensorRT engines through the C++
  TensorRT runtime (`libnvinfer.so.8.2.1`). It does **not** require the
  TensorRT **Python** bindings.
- **CSV injection / nvidia runtime**: On Jetson, the NVIDIA Container Runtime
  injects host TensorRT libs (`tensorrt.csv` lists `libnvinfer.so.8.2.1`) into
  the container at run time. The host is already configured correctly; the image
  simply lacks a Triton backend that can consume those libs.

## Bug Details

### Bug Condition

The bug manifests when the plain `aws.edgeml.dda.LocalServer.arm64` component
(no `JP5`/`JP6` token) is built on an aarch64 host for, and deployed to, a
JetPack 4.6 (L4T r32.7) Jetson. `build-custom.sh` finds no JetPack token, so it
calls `edgemlsdk/build.sh` with no `-j` argument. The generic
`src/edgemlsdk/Dockerfile` then compiles Triton `v2.45.0` from source with the
in-repo patches but with **`--backend python` only** — no `--enable-gpu` and no
`--backend tensorrt`. The staging step moves only `backends/python` into the
install, so the resulting `flask-app` image has no `tensorrt` backend and cannot
load TensorRT models.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type BuildTarget   // { componentName, architecture, deviceJetPack }
  OUTPUT: boolean

  // The plain arm64 component (no JP5/JP6 token), built on an aarch64 host and
  // deployed to a JetPack 4.6 device, is routed through the generic source
  // build that compiles Triton with --backend python only (no tensorrt backend).
  RETURN  X.componentName CONTAINS "arm64"
      AND X.componentName DOES NOT CONTAIN "JP5"
      AND X.componentName DOES NOT CONTAIN "JP6"
      AND X.architecture = "aarch64"
      AND X.deviceJetPack = "4.6"          // L4T r32.7, TensorRT 8.2.1, CUDA 10.2
END FUNCTION
```

Where:
- **F** (original build path): generic `src/edgemlsdk/Dockerfile`, no `-j` arg,
  `build.py ... --backend python` only, staging moves only `backends/python` →
  Triton with only the `python` backend.
- **F'** (fixed build path): the **same** generic source build with the
  `tensorrt` backend re-enabled for the aarch64/JP4 case
  (`ENABLE_TENSORRT_BACKEND=1` via `edgemlsdk/build.sh -j 4`):
  `build.py ... --enable-gpu --backend tensorrt --backend python` plus the
  TensorRT/CUDA cmake args, staging both `backends/tensorrt` and
  `backends/python` → Triton including the C++ `tensorrt` backend.

### Examples

- **Battle case (the bug):** Build `aws.edgeml.dda.LocalServer.arm64` v1.0.116
  on an aarch64 host, deploy to a Xavier NX (JP4.6). Inside the `flask-app`
  container, `/opt/tritonserver/backends/` contains only `python`;
  `import tensorrt` raises `ModuleNotFoundError`; no `libnvinfer` is present.
  Models `base_model-...-segmentation` and `model-...-segmentation` stay in
  `state: LOADING`; logs repeat "Pipeline started, waiting for Triton
  inference". **Expected:** `/opt/tritonserver/backends/tensorrt` exists, the
  segmentation models reach `READY`, and inference completes.
- **Preservation case (JP5):** Build `aws.edgeml.dda.LocalServer.arm64JP5` —
  still selects `Dockerfile.jp5` / `edgemlsdk/build.sh -j 5`. Unchanged.
- **Preservation case (JP6):** Build `aws.edgeml.dda.LocalServer.arm64JP6` —
  still selects `Dockerfile.jp6` / `edgemlsdk/build.sh -j 6`. Unchanged.
- **Preservation case (x86_64):** Build the plain `arm64` component on an
  `x86_64` host — still uses the generic `src/edgemlsdk/Dockerfile` with
  `ENABLE_TENSORRT_BACKEND=0` (default), so `build.py` runs with
  `--backend python` only and the image stays CPU/`python`-only. The `generic`
  compose profile only. Byte-for-byte unchanged.
- **Preservation case (python backend):** The python-backend `marshal_model-...`
  reaches `READY` today and must continue to on the JP4 image.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- A component name containing `JP6` SHALL continue to select `Dockerfile.jp6`
  and `edgemlsdk/build.sh -j 6` (Req 3.1).
- A component name containing `JP5` SHALL continue to select `Dockerfile.jp5`
  and `edgemlsdk/build.sh -j 5` (Req 3.2).
- Building on an `x86_64` host SHALL continue to use the generic
  `src/edgemlsdk/Dockerfile` with `ENABLE_TENSORRT_BACKEND=0` (no TensorRT deps,
  `build.py --backend python` only) and the `generic` compose profile only, and
  SHALL stay CPU/`python`-only (Req 3.3).
- The interpreter-version audit guard (`test/python_version_audit.py`), the
  in-image backend unit tests, and the artifact packaging step SHALL continue to
  run and behave as today (Req 3.4).
- The `python` Triton backend SHALL continue to load non-TensorRT models (e.g.
  `marshal_model-...`) to `READY` (Req 3.5).

**Scope:**
All inputs where `isBugCondition` is false MUST be completely unaffected. This
includes:
- JP5 and JP6 component builds (token present).
- x86_64/generic host builds (no token, `ARCHITECTURE = x86_64`,
  `ENABLE_TENSORRT_BACKEND` defaulted off).
- The audit guard, backend unit tests, and packaging for every target.

**Note:** The concrete correct behavior for buggy inputs is defined in
Correctness Properties (Property 1). This section enumerates what must NOT
change (Property 2).

## Hypothesized Root Cause

The cause is now well understood and confirmed by the live evidence and the
repo: the plain `arm64` aarch64 build runs the generic
`src/edgemlsdk/Dockerfile`, which compiles Triton `v2.45.0` from source with the
in-repo patches but invokes `build.py` with **`--backend python` only** — no
`--enable-gpu`, no `--backend tensorrt` — and then stages only `backends/python`.
The TensorRT backend therefore is never built or shipped. Restoring the original,
historically-working behavior means re-enabling the `tensorrt` backend in this
**same** source build. The substantive design questions are now implementation
details, not architectural ones:

1. **Build-time TensorRT/CUDA toolchain (central detail).** The Ubuntu 18.04
   aarch64 base has no CUDA or TensorRT. Compiling the `tensorrt` backend from
   source needs the L4T r32.7 TensorRT 8.2.1 dev libs (`libnvinfer-dev`,
   `libnvinfer-plugin-dev`, `libnvonnxparsers-dev`) and the CUDA 10.2 toolkit
   present **at build time**. This is the item most likely to need a build-server
   spike (see Fix Implementation).

2. **Locating TensorRT/CUDA for the Triton backend build.** `build.py` must be
   told where the TensorRT headers/libs and CUDA toolkit live via cmake args, and
   `--enable-gpu` must be set so the core and backend build with GPU support.

3. **x86_64 must stay CPU/python-only.** Because the same generic Dockerfile
   serves x86_64, the TensorRT-enabling changes must be gated so x86_64 builds
   are byte-for-byte unchanged.

4. **Interpreter version is not implicated.** The Triton `tensorrt` backend is
   **C++** and loads `.plan` engines through `libnvinfer.so.8.2.1`
   (host-injected via CSV at run time). It does **not** import the 3.6-only
   TensorRT Python bindings, so the fix does not require changing the migrated
   Python 3.11 interpreter. The 3.6-only Python binding matters only for Python
   code that does `import tensorrt` (on-device model conversion), which is out of
   scope here (Neo compiles `.plan` engines offline; on-device conversion is
   tracked by `triton-offline-dependency-install`).

### Options Considered (how JP4 obtains the TensorRT backend)

- **Option A (previously chosen, now REJECTED): new L4T r32.7 base + prebuilt
  Jetson Triton release.** Base a new `Dockerfile.jp4` on
  `nvcr.io/nvidia/l4t-base:r32.7.1` and stage a prebuilt Jetson Triton 2.19 /
  r22.02 release for the `tensorrt` backend. Rejected: this contradicts the
  verified ground truth. The JP4.6 path never used a special base image, and
  mixing a prebuilt Triton 2.19 `tensorrt` backend with the EdgeML-built
  `v2.45.0` core would create a backend-ABI mismatch. It also forks a large new
  Dockerfile away from the proven generic build.

- **Option C (now CHOSEN): re-enable the TensorRT backend in the existing
  from-source compile, with the in-repo patches, on the Ubuntu 18.04 aarch64
  base.** Keep the generic `src/edgemlsdk/Dockerfile` and its
  `edgeml-triton-server.diff` / `edgeml-triton-core.diff` patches exactly as they
  are. In the same `build.py` invocation that already builds the `python`
  backend for Triton `v2.45.0`, add `--enable-gpu` and `--backend tensorrt`
  (with TensorRT/CUDA cmake args), and extend the staging move to include
  `backends/tensorrt`. The TensorRT backend is then ABI-matched to the same
  `v2.45.0` core by construction. This restores the original historically-working
  solution with the smallest, most faithful change.

**Gating mechanism — two sub-options:**

- **(a) CHOSEN — single Dockerfile gated by a build ARG.** Add
  `ARG ENABLE_TENSORRT_BACKEND=0` to `src/edgemlsdk/Dockerfile`. When `1`
  (set only for the JP4/aarch64 build via `build-custom.sh` → `edgemlsdk/build.sh
  -j 4`), the build installs the L4T r32.7 TensorRT/CUDA dev libs, appends
  `--enable-gpu --backend tensorrt` (+ cmake args) to `build.py`, and stages
  `backends/tensorrt`. When `0` (default, x86_64), the `build.py` call and the
  staging move are byte-for-byte the originals.

- **(b) rejected — a dedicated `Dockerfile.jp4` derived from the generic one.**
  Forking the full ~380-line generic Dockerfile creates long-term maintenance
  drift (every future generic fix must be mirrored) and cannot guarantee x86_64
  stays byte-for-byte identical. Rejected in favor of the lower-duplication
  ARG-gated single Dockerfile.

**Decision:** Adopt **Option C with gating mechanism (a)**. Keep the Ubuntu 18.04
aarch64 base and the existing patches; re-enable the `tensorrt` backend in the
same source build, gated by `ENABLE_TENSORRT_BACKEND` so x86_64 is unchanged.
Reuse `recipe-arm64.yaml` (no new recipe). JP5/JP6 paths are untouched.

### Interpreter-version recommendation

**Keep `PYTHON_VERSION=3.11` for the JP4 target — do NOT pin the DDA interpreter
to 3.6.** The bug (TensorRT models stuck `LOADING`) is fixed by the **C++**
`tensorrt` backend + host-injected `libnvinfer.so.8.2.1`; neither needs the
3.6-only TensorRT Python bindings. The repo-audit guard in `build-custom.sh`
fails only on disallowed **3.9** references and otherwise assumes 3.11, so
keeping the DDA app and the EdgeML-built `python` Triton backend at 3.11
preserves Req 3.4. The generic Dockerfile already builds CPython 3.11 from
source on the Ubuntu 18.04 base, so the interpreter story is entirely unchanged
by this fix.

## Correctness Properties

Property 1: Bug Condition — JetPack 4.6 image ships a working TensorRT backend

_For any_ build target where the bug condition holds (`isBugCondition` returns
true — the plain `arm64` component built on aarch64 for a JetPack 4.6 device),
the fixed build path SHALL produce a `flask-app` image whose source-built Triton
includes a `tensorrt` backend at `/opt/tritonserver/backends/tensorrt` (built
from `v2.45.0` with `--enable-gpu --backend tensorrt` against L4T r32.7 /
TensorRT 8.2.1), such that a TensorRT segmentation model reaches `state: READY`
and inference completes rather than hanging.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation — non-JP4.6 targets are unchanged

_For any_ build target where the bug condition does NOT hold
(`isBugCondition` returns false — JP5/JP6 components, or any build on an
`x86_64` host), the fixed build path SHALL produce exactly the same result as
the original build path: JP6 → `Dockerfile.jp6`, JP5 → `Dockerfile.jp5`, x86_64
→ generic `src/edgemlsdk/Dockerfile` with `ENABLE_TENSORRT_BACKEND=0`
(`build.py --backend python` only, no TensorRT deps) staying CPU/`python`-only +
`generic` profile; the audit guard, backend unit tests, and packaging behave
identically; and the `python` backend continues to load non-TensorRT models to
`READY`.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

### Changes Required

The fix keeps the Ubuntu 18.04 aarch64 base and the existing in-repo Triton
patches, and re-enables the `tensorrt` backend in the existing source build,
gated for the aarch64/JP4 case.

**1. `src/edgemlsdk/Dockerfile` — add the `ENABLE_TENSORRT_BACKEND` ARG, the
gated build-time TensorRT/CUDA deps, the gated `build.py` flags, and the gated
staging move. (Central change.)**

Declare the ARG near the top, defaulted off so x86_64 is unchanged:

```dockerfile
ARG ENABLE_TENSORRT_BACKEND=0
```

**1a. Build-time TensorRT/CUDA toolchain (the central detail / likely spike).**
Immediately before the Triton clone, install the L4T r32.7 TensorRT 8.2.1 +
CUDA 10.2 dev packages **only when** `ENABLE_TENSORRT_BACKEND=1`. The
recommended approach is to add the NVIDIA L4T apt repository for r32.7 and pull
the dev debs; require/confirm this on a JP4.6 aarch64 build host:

```dockerfile
ARG ENABLE_TENSORRT_BACKEND
RUN if [ "$ENABLE_TENSORRT_BACKEND" = "1" ]; then \
      # Add the NVIDIA L4T r32.7 apt repo (or mount JetPack dev debs) and install
      # the TensorRT 8.2.1 + CUDA 10.2 dev headers/libs needed to compile the
      # Triton tensorrt backend from source.
      apt-get update && apt-get install -y \
        cuda-toolkit-10-2 \
        libnvinfer-dev libnvinfer-plugin-dev libnvonnxparsers-dev ; \
    fi
```

> **Spike flag:** Sourcing the L4T r32.7 dev debs into an Ubuntu 18.04 aarch64
> image (apt repo URLs/keys vs. mounting JetPack-provided debs vs. requiring a
> preconfigured JP4.6 aarch64 build host) is the part most likely to need a
> build-server spike. The exact package names/versions and repo wiring must be
> confirmed against an L4T r32.7 environment before this is finalized.

**1b. `build.py` invocation — add `--enable-gpu`, `--backend tensorrt`, and the
TensorRT/CUDA cmake args when enabled.** The current `RUN` ends with
`--backend python --build-dir=\`pwd\`/build`. Gate the extra args so the x86_64
(default) call is byte-for-byte unchanged:

```dockerfile
ARG ENABLE_TENSORRT_BACKEND
RUN export PYBIND11_FINDPYTHON=ON && \
    cd /dependencies/server && \
    git apply edgeml-triton-server.diff && \
    TRT_ARGS="" && \
    if [ "$ENABLE_TENSORRT_BACKEND" = "1" ]; then \
      TRT_ARGS="--enable-gpu --backend tensorrt \
        --extra-core-cmake-arg=TRITON_ENABLE_GPU=ON \
        --extra-core-cmake-arg=CMAKE_CUDA_COMPILER=/usr/local/cuda-10.2/bin/nvcc \
        --extra-backend-cmake-arg=tensorrt:TRITON_TENSORRT_INCLUDE_PATHS=/usr/include/aarch64-linux-gnu \
        --extra-backend-cmake-arg=tensorrt:TRITON_TENSORRT_LIB_PATHS=/usr/lib/aarch64-linux-gnu" ; \
    fi && \
    python3.11 build.py --enable-logging --enable-stats --enable-metrics --enable-cpu-metrics --no-container-build \
    $TRT_ARGS \
    --extra-core-cmake-arg=TRITON_ENABLE_ENSEMBLE=ON \
    --extra-core-cmake-arg=PYBIND11_FINDPYTHON=ON \
    --extra-core-cmake-arg=-DPYBIND11_FINDPYTHON=ON \
    --extra-core-cmake-arg=CMAKE_POLICY_VERSION_MINIMUM=3.5 \
    --extra-core-cmake-arg=PYTHON_EXECUTABLE=/usr/local/bin/python3.11 \
    --extra-core-cmake-arg=-DPYTHON_EXECUTABLE=/usr/local/bin/python3.11 \
    --extra-core-cmake-arg=PYTHON_INCLUDE_DIR=/usr/local/include/python3.11 \
    --extra-core-cmake-arg=PYTHON_LIBRARY=/usr/local/lib/libpython3.11.so \
    --extra-core-cmake-arg=PYTHON_LIBRARIES=/usr/local/lib/libpython3.11.so \
    --extra-core-cmake-arg=-DPYTHON_INCLUDE_DIR=/usr/local/include/python3.11 \
    --extra-core-cmake-arg=PYBIND11_PYTHON_VERSION=3.11 \
    --backend python --build-dir=`pwd`/build
```

> The exact cmake variable names/paths for locating TensorRT and CUDA (e.g.
> `TRITON_TENSORRT_INCLUDE_PATHS`/`TRITON_TENSORRT_LIB_PATHS`, CUDA toolkit root)
> are representative and must be confirmed during the build-server spike against
> the `v2.45.0` tensorrt-backend cmake on L4T r32.7. The gating logic (empty
> `$TRT_ARGS` when disabled) keeps the x86_64 call identical to today's.

**1c. Backend staging move — extend to include `backends/tensorrt` when
enabled.** The current line moves only the python backend:

```dockerfile
RUN mv /dependencies/server/build/python/install/backends/python /dependencies/server/build/tritonserver/install/backends
```

Extend it (gated) so the tensorrt backend is staged alongside the python backend
for the JP4 build, while x86_64 keeps the original single move:

```dockerfile
ARG ENABLE_TENSORRT_BACKEND
RUN mv /dependencies/server/build/python/install/backends/python /dependencies/server/build/tritonserver/install/backends && \
    if [ "$ENABLE_TENSORRT_BACKEND" = "1" ]; then \
      mv /dependencies/server/build/tensorrt/install/backends/tensorrt /dependencies/server/build/tritonserver/install/backends ; \
    fi
```

> The exact source path of the built tensorrt backend under `build/` must be
> confirmed during the spike; the staged result must be
> `tritonserver/install/backends/tensorrt`, which the existing
> `triton_installation_files.tar.gz` packaging then carries through unchanged.

Because the `tensorrt` backend ships **inside** `triton_installation_files.tar.gz`
(already produced by this Dockerfile and extracted by the backend image's
`install_edgemlsdk.sh`), **no new backend Dockerfile is required** — the existing
generic backend `Dockerfile` consumes whatever tar the edgemlsdk build produced.

**2. `src/edgemlsdk/build.sh` — handle `-j 4` by keeping the generic Dockerfile
and setting `ENABLE_TENSORRT_BACKEND=1`.**

`getopts j:` already captures `-j 4`. Add a JP4 branch that keeps
`DOCKERFILE="Dockerfile"` (the generic Ubuntu 18.04 aarch64 base — the original
JP4.6 path) but turns on the TensorRT backend build arg, and thread the arg into
`docker build`:

```bash
ENABLE_TENSORRT_BACKEND=0
if [ "$jetpack" = "6" ]; then
    DOCKERFILE="Dockerfile.jp6"
    echo "Using JP6 Dockerfile (l4t-jetpack:r36.3.0 base, native build)"
elif [ "$jetpack" = "5" ]; then
    DOCKERFILE="Dockerfile.jp5"
    echo "Using JP5 Dockerfile (l4t-jetpack:r35.4.1 base, native build)"
elif [ "$jetpack" = "4" ]; then
    DOCKERFILE="Dockerfile"            # generic Ubuntu 18.04 aarch64 base — original JP4.6 path
    ENABLE_TENSORRT_BACKEND=1          # re-enable the Triton tensorrt backend in the source build
    echo "Using generic Dockerfile with TensorRT backend ENABLED (JP4.6 / L4T r32.7)"
else
    DOCKERFILE="Dockerfile"
    echo "Using standard Dockerfile (Ubuntu ${ubuntu} base)"
fi
```

```bash
docker build \
    --load \
    --build-arg OS=$ubuntu \
    --build-arg PLATFORM=$platform \
    --build-arg PWSH_ARCH=$pwsh_arch \
    --build-arg PYTHON_VERSION=$python \
    --build-arg ENABLE_TENSORRT_BACKEND=$ENABLE_TENSORRT_BACKEND \
    -f $DOCKERFILE \
    -t edgemlsdk . || { echo "ERROR: edgemlsdk Docker build failed"; exit 1; }
```

**3. `build-custom.sh` — detect the tokenless aarch64 (JP4.6) target and thread
`-j 4`; keep x86_64 generic and CPU/python-only.**

Extend the token detection so a tokenless component on a non-x86_64 (aarch64)
host selects the JP4 target, while a tokenless build on `x86_64` stays generic
(Req 3.3). `ARCHITECTURE` is already `uname -m`:

```bash
IS_JP4=0
IS_JP5=0
IS_JP6=0
JETPACK_ARG=""
if echo "$COMPONENT_NAME" | grep -q "JP6"; then
    IS_JP6=1
    JETPACK_ARG="6"
elif echo "$COMPONENT_NAME" | grep -q "JP5"; then
    IS_JP5=1
    JETPACK_ARG="5"
elif echo "$COMPONENT_NAME" | grep -q "arm64" && [ "$ARCHITECTURE" != "x86_64" ]; then
    # Plain arm64 component on an aarch64 host == the JetPack 4.6 target.
    IS_JP4=1
    JETPACK_ARG="4"
fi
```

`edgemlsdk/build.sh` is already invoked with `-j "$JETPACK_ARG"` whenever
`JETPACK_ARG` is non-empty, so `JETPACK_ARG="4"` threads through automatically
and sets `ENABLE_TENSORRT_BACKEND=1` inside `build.sh`.

`BACKEND_DOCKERFILE` selection is **unchanged**: JP4 keeps the generic
`Dockerfile` (the tensorrt backend ships in the edgemlsdk tar, so the generic
backend image just extracts it):

```bash
if [ "$IS_JP6" = "1" ]; then
  export BACKEND_DOCKERFILE="Dockerfile.jp6"
elif [ "$IS_JP5" = "1" ]; then
  export BACKEND_DOCKERFILE="Dockerfile.jp5"
else
  export BACKEND_DOCKERFILE="Dockerfile"   # generic backend image — JP4 included
fi
```

The architecture-based compose-profile selection is **unchanged**: x86_64 builds
`--profile generic` only (CPU/python-only); aarch64 builds
`--profile tegra --profile generic`, and the `backend_tegra_gpu_enabled` service
(`runtime: nvidia`, Tegra lib mounts) runs on the JP4.6 device so the C++
`tensorrt` backend gets `libnvinfer.so.8.2.1` injected via `tensorrt.csv`. The
audit guard, backend unit-test step, and packaging are left exactly as-is
(Req 3.4).

**4. `recipe-arm64.yaml` — reuse as-is.**

The broken component **is** `aws.edgeml.dda.LocalServer.arm64`, so no new recipe
is required. Routing happens entirely in `build-custom.sh` via architecture +
absence-of-token. JP5/JP6 recipes/paths are untouched.

## Testing Strategy

### Validation Approach

Two phases: first surface counterexamples that demonstrate the bug on the
**unfixed** build, then verify the JP4 target fixes it and that
JP5/JP6/x86_64 remain unchanged. Because the bug only fully manifests on real
Jetson hardware (and the fix only fully compiles where the L4T r32.7 TensorRT/CUDA
toolchain is available), the strategy separates checks that can run on any CI
host (routing, gating, image contents) from checks that **require a JetPack 4.6
device or an L4T r32.7 build environment** (actual `tensorrt` backend compilation
and model loading to `READY`).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing
the fix, and confirm/refute the root cause (Triton built with `--backend python`
only; no `tensorrt` backend staged). If refuted (e.g. the `tensorrt` backend
fails to compile on the r32.7 toolchain because of a missing dep or cmake var),
re-hypothesize the build-time TensorRT/CUDA wiring.

**Test Plan**: Exercise the build-routing logic and inspect the produced image
on the unfixed code.

**Test Cases**:
1. **Routing test (unfixed)**: Run the component-name + architecture →
   `JETPACK_ARG` / `BACKEND_DOCKERFILE` selection for the plain `arm64` name on
   an aarch64 host; assert it currently yields no `-j` arg and `Dockerfile`
   (generic) with `ENABLE_TENSORRT_BACKEND` effectively `0` — the defect.
2. **Image-contents test (unfixed)**: In a `flask-app` image built from the
   generic path, assert `/opt/tritonserver/backends/tensorrt` is **absent** and
   only `python` is present — the counterexample.
3. **On-device test (unfixed)**: On a JP4.6 Xavier NX, deploy the generic-built
   image and observe the segmentation model stuck in `LOADING` and the repeating
   "waiting for Triton inference" log. (Requires a JP4.6 device.)
4. **Edge case**: Confirm the python-backend `marshal_model-...` reaches `READY`
   on the same unfixed image (establishes the preservation baseline for Req 3.5).

**Expected Counterexamples**:
- Plain `arm64` aarch64 build runs `build.py --backend python` only.
- `/opt/tritonserver/backends/` has no `tensorrt` entry; `import tensorrt`
  fails; no `libnvinfer` present in the image.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
source build produces a Triton with a working `tensorrt` backend.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  image := buildFixed(X)                       // edgemlsdk -j 4, ENABLE_TENSORRT_BACKEND=1
  ASSERT tensorrtBackendPresent(image)          // /opt/tritonserver/backends/tensorrt exists
  ASSERT tensorRTModelReachesReady(image)       // requires JP4.6 device / L4T r32.7 runtime
  ASSERT inferenceCompletes(image)
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the
fixed build path produces the same result as the original — in particular that
x86_64 stays CPU/`python`-only and byte-for-byte unchanged.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT buildOriginal(X) = buildFixed(X)
END FOR
```

**Testing Approach**: Property-based testing is well suited to preservation
because the routing decision is a pure function of `(componentName,
architecture)` over a small, enumerable input domain — generate many
`(name, arch)` pairs and assert the selected `JETPACK_ARG` /
`ENABLE_TENSORRT_BACKEND` / `BACKEND_DOCKERFILE` / compose profile is unchanged
versus the pre-fix mapping for every non-JP4.6 input. In particular every
`x86_64` input MUST map to `ENABLE_TENSORRT_BACKEND=0` (CPU/python-only).

**Test Plan**: Capture the pre-fix routing decisions for JP5/JP6/x86_64 inputs,
then assert the post-fix code yields identical decisions.

**Test Cases**:
1. **JP6 preservation**: name contains `JP6` → `Dockerfile.jp6`, `-j 6`
   (any arch). Unchanged (Req 3.1).
2. **JP5 preservation**: name contains `JP5` → `Dockerfile.jp5`, `-j 5`.
   Unchanged (Req 3.2).
3. **x86_64 preservation**: tokenless name on `x86_64` → generic `Dockerfile`,
   no `-j`, `ENABLE_TENSORRT_BACKEND=0`, `generic` profile only, image stays
   CPU/`python`-only. Unchanged (Req 3.3).
4. **Guard/tests/packaging preservation**: the audit guard, in-image backend
   unit tests, and `zip` packaging run identically for every target (Req 3.4).
5. **Python-backend preservation**: on the JP4 image, the python-backend
   `marshal_model-...` still reaches `READY` (Req 3.5; requires a JP4.6 device
   or L4T r32.7 runtime).

### Unit Tests

- Component-name + architecture → `JETPACK_ARG` / `BACKEND_DOCKERFILE` mapping,
  including the new tokenless-aarch64 → `-j 4` branch and the
  x86_64-stays-generic guard. This extends the existing
  `test/backend-test/host_scripts/test_docker_profile_selection.py` pattern
  already run by `build-custom.sh`.
- `edgemlsdk/build.sh` `-j 4` → generic `Dockerfile` + `ENABLE_TENSORRT_BACKEND=1`
  build arg; no `-j` and `-j 5`/`-j 6` → `ENABLE_TENSORRT_BACKEND=0`.
- Image-contents assertion: `/opt/tritonserver/backends/` contains both
  `tensorrt` and `python` after a JP4 build (run where a JP4 image is available);
  contains only `python` for an x86_64 build.

### Property-Based Tests

- Generate random `(componentName, architecture)` pairs and assert the routing
  function is unchanged for every non-JP4.6 input (preservation) and selects
  `-j 4` / `ENABLE_TENSORRT_BACKEND=1` for every tokenless aarch64 input (fix),
  while every `x86_64` input maps to `ENABLE_TENSORRT_BACKEND=0`.
- Generate component-name variants (case, token placement) to confirm `JP5`/
  `JP6` detection precedence is preserved ahead of the JP4 fallthrough.

### Integration Tests

- Full JP4 build on an L4T r32.7 / aarch64 build environment (with the TensorRT
  8.2.1 + CUDA 10.2 dev toolchain available): build the `edgemlsdk` image via
  `-j 4`, then the `flask-app` image, load it, and assert the source-built
  `tensorrt` backend is present and a sample `.plan` segmentation model reaches
  `READY`.
- End-to-end on a JP4.6 Xavier NX: deploy the rebuilt
  `aws.edgeml.dda.LocalServer.arm64` component and confirm the segmentation
  model loads and inference completes (no more "waiting for Triton inference"
  hang).
- Regression: rebuild a JP5 and a JP6 component, and an x86_64 build, and confirm
  byte-for-byte identical routing/profile behavior, that x86_64 stays
  CPU/`python`-only, and successful builds.

> **Validation caveat:** Full fix verification (compiling the `tensorrt` backend
> from source and actually loading TensorRT engines to `READY`) requires a
> JetPack 4.6 device or an L4T r32.7 build environment with the TensorRT 8.2.1 +
> CUDA 10.2 dev toolchain, the NVIDIA Container Runtime, and `tensorrt.csv`
> injection. CI-only x86_64 hosts can verify routing, gating, and static image
> contents but cannot exercise the TensorRT compile or the GPU runtime path.
