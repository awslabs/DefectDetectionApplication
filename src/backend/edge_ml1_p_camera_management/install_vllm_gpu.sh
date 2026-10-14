#!/bin/bash
#
# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Build vLLM from source for the JetPack 7 (Jetson Thor) LocalServer image,
# compiling the CUDA kernels for Thor (sm_110) against the container's
# pre-installed cu130 PyTorch, and install the wheel into the DDA python
# (cp311) interpreter.
#
# WHY FROM SOURCE: no prebuilt vLLM wheel exists for Thor/CUDA 13/cp311.
# vLLM's released wheels target x86_64 CUDA 12.x, and pypi.jetson-ai-lab.io
# (the JP6 prebuilt source) publishes nothing for Thor/cu130/cp311. So the
# wheel must be compiled here, against the torch layer the Dockerfile
# installs immediately before invoking this script.
#
# This is a LONG build (hours, RAM-hungry). It is gated in Dockerfile.jp7 by
# the VLLM_ENABLE build-arg (default 1; VLLM_ENABLE=0 opts out).
#
# Optional env (override the pinned defaults):
#   PYBIN               interpreter to build for/install into (default python3,
#                       the DDA cp311 alternative)
#   VLLM_VERSION        git tag of vLLM to build (default v0.11.2)
#   CUDA_ARCHITECTURES  TORCH_CUDA_ARCH_LIST value (default "11.0", Thor sm_110)
#   CUDA_HOME           CUDA toolkit root (default /usr/local/cuda)
#   VLLM_BUILD_JOBS     parallel build jobs (default: min(nproc, 6))

set -e

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# ── Environment contract (pinned defaults, env-var overridable) ────────────
# Build against the interpreter selected by PYBIN so the produced wheel is
# tagged for exactly this interpreter — regardless of where the python3
# alternative points. Default: python3 (the DDA cp311 interpreter on JP7).
PYBIN="${PYBIN:-python3}"
VLLM_VERSION="${VLLM_VERSION:-v0.11.2}"
# Exported later as TORCH_CUDA_ARCH_LIST; vLLM's CMake derives the family/'a'
# variants for kernels that need them. Thor is compute capability 11.0.
CUDA_ARCHITECTURES="${CUDA_ARCHITECTURES:-11.0}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"

# Cap parallelism: the vLLM CUDA compile is memory-heavy and OOMs with too
# many jobs on Jetson-class RAM — min(nproc, 6), the same memory-safety cap
# as install_onnxruntime_gpu.sh. Exported later as MAX_JOBS (vLLM's setup.py
# honors it).
NPROC=$(nproc)
VLLM_BUILD_JOBS="${VLLM_BUILD_JOBS:-$(( NPROC > 6 ? 6 : NPROC ))}"

echo "=========================================================="
echo " Building vLLM from source"
echo "   vLLM tag          : ${VLLM_VERSION}"
echo "   CUDA home         : ${CUDA_HOME}"
echo "   CUDA architectures: ${CUDA_ARCHITECTURES}"
echo "   python            : $(${PYBIN} --version 2>&1)"
echo "   build jobs        : ${VLLM_BUILD_JOBS}"
echo "=========================================================="

# ── Prerequisite checks — fail fast BEFORE any long work ───────────────────
# Each check exits nonzero naming the missing prerequisite so an hours-long
# compile never starts against a broken environment.

# 1. CUDA toolkit at the expected path, with a working nvcc.
if [ ! -d "${CUDA_HOME}" ]; then
    echo "ERROR: missing prerequisite: CUDA toolkit not found at ${CUDA_HOME}." >&2
    echo "       The base image must provide the CUDA toolkit development" >&2
    echo "       files (cuda-toolkit), or set CUDA_HOME to its location." >&2
    exit 1
fi
if ! "${CUDA_HOME}/bin/nvcc" --version >/dev/null 2>&1; then
    echo "ERROR: missing prerequisite: nvcc not runnable at ${CUDA_HOME}/bin/nvcc." >&2
    echo "       A devel (not runtime) CUDA base image is required." >&2
    exit 1
fi

# 2. The torch installation the vLLM build compiles against (the Torch_Pin
#    layer in Dockerfile.jp7 installs it immediately before this script).
if ! ${PYBIN} -c "import torch" >/dev/null 2>&1; then
    echo "ERROR: missing prerequisite: torch is not importable under ${PYBIN}." >&2
    echo "       The Dockerfile's cu130 torch layer must run before this" >&2
    echo "       script — vLLM is compiled against the installed torch." >&2
    exit 1
fi
# Record the pre-build torch version: the vLLM build must NOT replace,
# upgrade, or downgrade torch; the post-install verification compares
# against this value.
TORCH_BEFORE=$(${PYBIN} -c "import torch; print(torch.__version__)")
TORCH_CUDA=$(${PYBIN} -c "import torch; print(torch.version.cuda or '')")
case "${TORCH_CUDA}" in
    13.*)
        echo "torch ${TORCH_BEFORE} (CUDA ${TORCH_CUDA}) found — building against it"
        ;;
    *)
        echo "ERROR: missing prerequisite: torch reports CUDA build '${TORCH_CUDA}'," >&2
        echo "       expected a CUDA 13.x build (cu130). Install the cu130" >&2
        echo "       Torch_Pin before running this script." >&2
        exit 1
        ;;
esac

# 3. Required build tools: git, pip, and the libpython dev headers.
if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: missing prerequisite: git is not installed." >&2
    exit 1
fi
if ! ${PYBIN} -m pip --version >/dev/null 2>&1; then
    echo "ERROR: missing prerequisite: pip is not available under ${PYBIN}." >&2
    exit 1
fi
# Derive the dev-headers package from the actual container interpreter
# (e.g. python3.11 -> libpython3.11-dev) rather than hardcoding a version.
# The Dockerfile already installs python${PYTHON_VERSION}-dev; the apt
# install here is a safety net (the install_onnxruntime_gpu.sh pattern).
PY_MM=$(${PYBIN} -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_INCLUDE=$(${PYBIN} -c 'import sysconfig; print(sysconfig.get_paths()["include"])')
if [ ! -f "${PY_INCLUDE}/Python.h" ]; then
    echo "libpython${PY_MM} dev headers not found; attempting apt install..."
    apt-get update -y
    apt-get install -y --no-install-recommends "libpython${PY_MM}-dev" || true
fi
if [ ! -f "${PY_INCLUDE}/Python.h" ]; then
    echo "ERROR: missing prerequisite: libpython${PY_MM} dev headers" >&2
    echo "       (${PY_INCLUDE}/Python.h). Install libpython${PY_MM}-dev." >&2
    exit 1
fi

# ── ccache (optional — presence is logged, absence is never an error) ──────
# vLLM's setup.py auto-detects ccache on PATH and wires it as
# CMAKE_{C,CXX,CUDA}_COMPILER_LAUNCHER itself, so a repeated build reuses
# prior compilation results. Nothing to configure here beyond logging.
if command -v ccache >/dev/null 2>&1; then
    echo "ccache found ($(command -v ccache)) — vLLM's build will compile through ccache"
else
    echo "ccache not found — building without compiler cache"
fi

# ── Checkout: shallow clone of the pinned vLLM tag ──────────────────────────
WORK_DIR="${WORK_DIR:-/tmp/vllm-build}"
rm -rf "${WORK_DIR}"
mkdir -p "${WORK_DIR}"
cd "${WORK_DIR}"

echo "Cloning vLLM ${VLLM_VERSION}..."
git clone --depth 1 --branch "${VLLM_VERSION}" \
    https://github.com/vllm-project/vllm.git
cd vllm

# ── Existing-torch mode ─────────────────────────────────────────────────────
# vLLM's use_existing_torch.py strips every torch-family line from
# requirements/*.txt and pyproject.toml, so the source build compiles
# against — and the wheel's metadata never demands — any torch other than
# the installed Torch_Pin. Run unconditionally so a future VLLM_VERSION
# override cannot silently move torch either.
echo "Switching the vLLM build to the installed torch (use_existing_torch.py)..."
${PYBIN} use_existing_torch.py

# ── Classic-API compatibility patch (guarded) ───────────────────────────────
# At v0.11.2, vllm/engine/async_llm_engine.py is a shim aliasing the V1
# engine: `AsyncLLMEngine = AsyncLLM`. The V1 AsyncLLM already provides the
# Classic_Engine_API that vllm_runtime/manager.py uses (from_engine_args,
# generate, errored) EXCEPT shutdown_background_loop, which V1 renamed to
# shutdown(). Overwrite the shim with a compatibility subclass restoring the
# classic name, so the built wheel itself exposes the full classic surface.
#
# Guard first: if the file no longer matches the expected shim shape (e.g. a
# future VLLM_VERSION override changed it), fail loudly instead of silently
# clobbering unknown code — the JP6 guarded-sed convention.
SHIM_FILE="vllm/engine/async_llm_engine.py"
if ! grep -q "AsyncLLMEngine = AsyncLLM" "${SHIM_FILE}"; then
    echo "ERROR: ${SHIM_FILE} does not match the expected shim shape" >&2
    echo "       (no 'AsyncLLMEngine = AsyncLLM' line found). The pinned" >&2
    echo "       vLLM version (${VLLM_VERSION}) may have changed this file;" >&2
    echo "       review the Classic-API compatibility patch before building." >&2
    exit 1
fi
echo "Applying the Classic-API compatibility patch to ${SHIM_FILE}..."
cat > "${SHIM_FILE}" <<'EOF'
from vllm.v1.engine.async_llm import AsyncLLM


class AsyncLLMEngine(AsyncLLM):
    """Classic-API compatibility subclass (jp7-vllm-enablement).

    V1 renamed shutdown_background_loop() to shutdown(); the classic
    name delegates so vllm_runtime/manager.py's engine surface holds.
    from_engine_args is a classmethod returning cls(...), so it
    constructs this subclass; generate/errored are inherited.
    """

    def shutdown_background_loop(self) -> None:
        self.shutdown()
EOF

# ── Build dependencies ──────────────────────────────────────────────────────
# vLLM's [build-system] requires (cmake, ninja, packaging, setuptools-scm,
# wheel, ...) must be present under ${PYBIN} because the wheel is built with
# --no-build-isolation below. requirements/build.txt mirrors pyproject's
# build-system requires; use_existing_torch.py already stripped its torch
# line, so this install cannot move the Torch_Pin.
echo "Installing vLLM build dependencies..."
${PYBIN} -m pip install --no-cache-dir -r requirements/build.txt

# ── Build: compile the CUDA kernels and produce the wheel ──────────────────
#   VLLM_TARGET_DEVICE=cuda    build the CUDA backend
#   TORCH_CUDA_ARCH_LIST       Thor sm_110 (vLLM's CMake derives family/'a'
#                              variants for kernels that need them)
#   MAX_JOBS                   the min(nproc, 6) memory-safety cap — vLLM's
#                              setup.py honors it
#   --no-deps                  the wheel only; runtime deps resolve at install
#   --no-build-isolation       compile against the INSTALLED torch (the
#                              Torch_Pin), not a fresh isolated-env torch
echo "Building the vLLM wheel (this is the long step)..."
VLLM_TARGET_DEVICE=cuda \
TORCH_CUDA_ARCH_LIST="${CUDA_ARCHITECTURES}" \
MAX_JOBS="${VLLM_BUILD_JOBS}" \
    ${PYBIN} -m pip wheel . --no-deps --no-build-isolation -w dist/

# ── Wheel validation ────────────────────────────────────────────────────────
# A compile error above already aborted via set -e before any install — no
# partial artifact is ever installed. Here, confirm a wheel exists and that
# its tags declare the aarch64 platform and compatibility with this (cp311)
# interpreter.
WHL=$(ls dist/vllm-*.whl 2>/dev/null | head -1)
if [ -z "${WHL}" ]; then
    echo "ERROR: build produced no vLLM wheel (dist/vllm-*.whl is absent)." >&2
    ls -R dist/ 2>/dev/null || true
    exit 1
fi
# vLLM builds its extensions with py_limited_api, so the wheel is tagged
# cp38-abi3 — which DECLARES cp311 compatibility via the stable ABI. Validate
# with packaging.tags (installed via requirements/build.txt) rather than a
# literal cp311 substring match: an exact cp311 tag and an abi3 tag both pass.
${PYBIN} - "${WHL}" <<'PYEOF'
import sys
from pathlib import Path
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename

whl = Path(sys.argv[1])
_, _, _, tags = parse_wheel_filename(whl.name)
tags = set(tags)
print(whl.name, "tags:", ", ".join(sorted(str(t) for t in tags)))

if not any("aarch64" in t.platform for t in tags):
    raise SystemExit(
        f"ERROR: wheel {whl.name} has no aarch64 platform tag."
    )

supported = {(t.interpreter, t.abi) for t in sys_tags()}
if not any((t.interpreter, t.abi) in supported for t in tags):
    this = "cp%d%d" % sys.version_info[:2]
    raise SystemExit(
        f"ERROR: wheel {whl.name} (py, abi) tags are not compatible with "
        f"this interpreter ({this})."
    )
print("wheel tags OK: aarch64 platform, interpreter-compatible (py, abi)")
PYEOF

# ── Stage the wheel BEFORE any cleanup ──────────────────────────────────────
# A fixed directory outside the (soon-deleted) build tree, so a successful
# hours-long build is preserved in the image for inspection and reuse
# (mirrors /opt/onnxruntime-wheels).
echo "Staging ${WHL} to /opt/vllm-wheels..."
mkdir -p /opt/vllm-wheels
cp "${WHL}" /opt/vllm-wheels/

# ── Install the wheel into the ${PYBIN} interpreter ─────────────────────────
# The "numpy>=1.24,<2" co-constraint rides the same resolution so vLLM's
# transitive deps (transformers, opencv-python-headless, numba, ...) cannot
# bump numpy across the 2.0 ABI break the app's pinned numpy sits below
# (the JP6 precedent).
echo "Installing ${WHL}..."
${PYBIN} -m pip install --no-cache-dir "numpy>=1.24,<2" "${WHL}"

# ── Post-install verification ───────────────────────────────────────────────
# Run from a neutral directory: the vLLM source tree contains a `vllm/`
# package dir that would shadow the *installed* package (whose compiled
# extensions live in site-packages). cd / avoids the shadow.
cd /

# 1. import vllm ALONE first: an import failure exits nonzero immediately
#    with the import error (stderr is not suppressed), before any symbol
#    check runs.
if ! ${PYBIN} -c "import vllm; print('vllm', vllm.__version__, 'import OK')"; then
    echo "ERROR: post-install verification failed: import vllm" >&2
    exit 1
fi

# 2. Per-symbol Classic_Engine_API checks, each exiting nonzero naming the
#    missing symbol (the surface vllm_runtime/manager.py is written against).
if ! ${PYBIN} -c "from vllm import AsyncEngineArgs" >/dev/null 2>&1; then
    echo "ERROR: missing vLLM symbol: AsyncEngineArgs" >&2
    exit 1
fi
if ! ${PYBIN} -c "from vllm import SamplingParams" >/dev/null 2>&1; then
    echo "ERROR: missing vLLM symbol: SamplingParams" >&2
    exit 1
fi
if ! ${PYBIN} -c "from vllm.engine.async_llm_engine import AsyncLLMEngine; assert hasattr(AsyncLLMEngine, 'from_engine_args')" >/dev/null 2>&1; then
    echo "ERROR: missing vLLM symbol: AsyncLLMEngine.from_engine_args" >&2
    exit 1
fi
if ! ${PYBIN} -c "from vllm.engine.async_llm_engine import AsyncLLMEngine; assert hasattr(AsyncLLMEngine, 'generate')" >/dev/null 2>&1; then
    echo "ERROR: missing vLLM symbol: AsyncLLMEngine.generate" >&2
    exit 1
fi
if ! ${PYBIN} -c "from vllm.engine.async_llm_engine import AsyncLLMEngine; assert hasattr(AsyncLLMEngine, 'shutdown_background_loop')" >/dev/null 2>&1; then
    echo "ERROR: missing vLLM symbol: AsyncLLMEngine.shutdown_background_loop" >&2
    exit 1
fi
echo "Classic_Engine_API symbols verified."

# 3. Torch-unchanged check: the vLLM build must NOT have replaced, upgraded,
#    or downgraded the Torch_Pin recorded before the build.
TORCH_AFTER=$(${PYBIN} -c "import torch; print(torch.__version__)")
if [ "${TORCH_AFTER}" != "${TORCH_BEFORE}" ]; then
    echo "ERROR: the vLLM build changed torch: was ${TORCH_BEFORE}," >&2
    echo "       now ${TORCH_AFTER}. The build must compile against the" >&2
    echo "       installed Torch_Pin without moving it." >&2
    exit 1
fi
echo "torch unchanged: ${TORCH_AFTER}"

# ── Cleanup: remove the (large) build tree — AFTER staging + verification ──
rm -rf "${WORK_DIR}"
echo "vLLM installation completed successfully"
