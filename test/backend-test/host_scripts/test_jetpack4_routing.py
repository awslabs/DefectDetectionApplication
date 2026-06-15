# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Bug-condition exploration test for the JetPack 4.6 build routing/gating.

Spec: jetpack4-tensorrt-build-support (bugfix). Task 1 — Property 1
(Bug Condition). 🟢 CI/this-device-OK: the routing/gating decision is a pure
function of (componentName, architecture) and runs on any host.

Bug picture (design / bugfix.md):
  The plain ``aws.edgeml.dda.LocalServer.arm64`` component (no JP5/JP6 token)
  built on an aarch64 host for a JetPack 4.6 device falls through the
  build-target selection and is built via the generic Ubuntu path with the
  Triton ``tensorrt`` backend disabled. ``build-custom.sh`` keys ``JETPACK_ARG``
  off the component name (only ``JP6``/``JP5`` tokens), and ``edgemlsdk/build.sh``
  selects the Dockerfile off ``-j`` with no JP4 branch and no
  ``ENABLE_TENSORRT_BACKEND`` gate.

isBugCondition(X):
    name CONTAINS "arm64" AND NOT CONTAINS "JP5" AND NOT CONTAINS "JP6"
    AND architecture = "aarch64" (and deviceJetPack = "4.6")

Expected (fixed) behavior this test ENCODES (design Property 1):
    tokenless aarch64  ->  JETPACK_ARG == "4"  AND  ENABLE_TENSORRT_BACKEND == 1

This test follows the technique used by ``test_docker_profile_selection.py``:
it extracts the REAL shell decision blocks from ``build-custom.sh`` and
``src/edgemlsdk/build.sh`` and runs them under ``bash -c`` with controlled
inputs, so the routing/gating contract is verified behaviorally against the
actual scripts (not a re-implementation).

NOTE (bugfix methodology): on the UNFIXED code this test is EXPECTED TO FAIL —
the tokenless aarch64 case yields ``JETPACK_ARG=""`` and there is no JP4 branch,
so ``ENABLE_TENSORRT_BACKEND`` is effectively ``0``. That failure confirms the
bug. Do NOT change the scripts to make it pass at task 1; it becomes the
fix-check at task 3.5.

Validates: Requirements 1.1, 1.2, 1.3 (and, once the fix lands, 2.1)
"""
import os
import re
import subprocess

from hypothesis import assume, given, settings
from hypothesis import strategies as st

_HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
BUILD_CUSTOM_PATH = os.path.join(REPO_ROOT, "build-custom.sh")
EDGEMLSDK_BUILD_PATH = os.path.join(REPO_ROOT, "src", "edgemlsdk", "build.sh")


def _read(path):
    with open(path) as f:
        return f.read()


def _extract_jetpack_arg_block():
    """Extract the JETPACK_ARG token-detection if/elif chain from
    build-custom.sh (the ``IS_JP5=0 ... fi`` block)."""
    content = _read(BUILD_CUSTOM_PATH)
    match = re.search(r"(IS_JP5=0\n.*?\nfi)", content, re.DOTALL)
    assert match, "Could not find the JETPACK_ARG token-detection block in build-custom.sh"
    return match.group(1)


def _extract_dockerfile_select_block():
    """Extract the JetPack/Dockerfile + ENABLE_TENSORRT_BACKEND selection block
    from src/edgemlsdk/build.sh (the ``if [ "$jetpack" = "6" ] ... fi`` block).

    On unfixed code this block only sets DOCKERFILE; it never touches
    ENABLE_TENSORRT_BACKEND."""
    content = _read(EDGEMLSDK_BUILD_PATH)
    match = re.search(r'(if \[ "\$jetpack" = "6" \].*?\nfi)', content, re.DOTALL)
    assert match, "Could not find the Dockerfile/jetpack selection block in src/edgemlsdk/build.sh"
    return match.group(1)


def _run_jetpack_arg(component_name, architecture):
    """Run build-custom.sh's real token-detection block with controlled
    COMPONENT_NAME / ARCHITECTURE and return the resulting JETPACK_ARG string."""
    block = _extract_jetpack_arg_block()
    snippet = (
        f'COMPONENT_NAME={_q(component_name)}\n'
        f'ARCHITECTURE={_q(architecture)}\n'
        f'{block}\n'
        f'echo "RESULT_JETPACK_ARG=[$JETPACK_ARG]"\n'
    )
    out = _bash(snippet)
    m = re.search(r"RESULT_JETPACK_ARG=\[(.*?)\]", out)
    assert m, f"No JETPACK_ARG in output: {out!r}"
    return m.group(1)


def _run_enable_tensorrt(jetpack, architecture):
    """Run src/edgemlsdk/build.sh's real selection block with a controlled
    ``jetpack`` value and return the resulting ENABLE_TENSORRT_BACKEND (int).

    ENABLE_TENSORRT_BACKEND is pre-seeded to 0 (the default the fix also uses),
    so the value reflects exactly what the extracted block does to it."""
    block = _extract_dockerfile_select_block()
    snippet = (
        f'jetpack={_q(jetpack)}\n'
        f'platform={_q(architecture)}\n'
        f'ubuntu="18.04"\n'
        f'ENABLE_TENSORRT_BACKEND=0\n'
        f'{block}\n'
        f'echo "RESULT_ENABLE_TRT=[$ENABLE_TENSORRT_BACKEND]"\n'
        f'echo "RESULT_DOCKERFILE=[$DOCKERFILE]"\n'
    )
    out = _bash(snippet)
    m = re.search(r"RESULT_ENABLE_TRT=\[(.*?)\]", out)
    assert m, f"No ENABLE_TENSORRT_BACKEND in output: {out!r}"
    raw = m.group(1).strip()
    return int(raw) if raw.isdigit() else 0


def _route(component_name, architecture):
    """The full build routing/gating decision for an input build target.

    Composes the two real shell blocks: build-custom.sh maps
    (componentName, architecture) -> JETPACK_ARG, which is threaded as ``-j``
    into edgemlsdk/build.sh, which maps it -> ENABLE_TENSORRT_BACKEND.
    Returns (jetpack_arg, enable_tensorrt_backend)."""
    jetpack_arg = _run_jetpack_arg(component_name, architecture)
    enable_trt = _run_enable_tensorrt(jetpack_arg, architecture)
    return jetpack_arg, enable_trt


def _q(value):
    """Single-quote a value for safe shell embedding."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def _bash(snippet):
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
    assert result.returncode == 0, f"decision block failed: {result.stderr}\n--snippet--\n{snippet}"
    return result.stdout


# ── Generators constrained to the bug condition ─────────────────────────────
# isBugCondition: name CONTAINS "arm64", NOT "JP5"/"JP6", architecture aarch64.
_SAFE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"


@st.composite
def bug_condition_component_names(draw):
    """Component names that satisfy the bug condition: always contain
    ``arm64`` and never the ``JP5``/``JP6`` tokens."""
    prefix = draw(st.text(alphabet=_SAFE_ALPHABET, max_size=24))
    suffix = draw(st.text(alphabet=_SAFE_ALPHABET, max_size=12))
    name = f"{prefix}arm64{suffix}"
    # Guard: a random fragment must not accidentally introduce a JP token.
    assume("JP5" not in name and "JP6" not in name)
    return name


@given(component_name=bug_condition_component_names(), architecture=st.just("aarch64"))
@settings(max_examples=150, deadline=None)
def test_bugcondition_routes_to_tensorrt_enabled_jp4_target(component_name, architecture):
    """Property 1 (Bug Condition): for every build target where the bug
    condition holds (tokenless ``arm64`` name on an aarch64 host), the routing
    decision MUST select the JetPack 4.6 TensorRT-enabled target:

        JETPACK_ARG == "4"  AND  ENABLE_TENSORRT_BACKEND == 1

    On unfixed code this FAILS (JETPACK_ARG == "" and ENABLE_TENSORRT_BACKEND
    == 0), which confirms the bug. Validates: Requirements 1.1, 1.2, 1.3.
    """
    jetpack_arg, enable_trt = _route(component_name, architecture)

    assert jetpack_arg == "4", (
        f"BUG: tokenless aarch64 component {component_name!r} routed to "
        f'JETPACK_ARG={jetpack_arg!r}, expected "4" (JP4.6 target). It falls '
        f"through to the generic Ubuntu path (no -j arg)."
    )
    assert enable_trt == 1, (
        f"BUG: tokenless aarch64 component {component_name!r} produced "
        f"ENABLE_TENSORRT_BACKEND={enable_trt}, expected 1. The Triton "
        f"tensorrt backend is never built, so the image ships python-only."
    )


def test_concrete_counterexample_plain_arm64_component():
    """Documented concrete counterexample (the live battle case):

        route("aws.edgeml.dda.LocalServer.arm64", "aarch64")
          -> JETPACK_ARG="" (expected "4"), ENABLE_TENSORRT_BACKEND=0 (expected 1)

    On unfixed code this FAILS, demonstrating the exact defect that leaves the
    TensorRT segmentation model stuck in state: LOADING on a JP4.6 device.
    """
    jetpack_arg, enable_trt = _route("aws.edgeml.dda.LocalServer.arm64", "aarch64")
    assert (jetpack_arg, enable_trt) == ("4", 1), (
        f'route("aws.edgeml.dda.LocalServer.arm64", "aarch64") -> '
        f"JETPACK_ARG={jetpack_arg!r} (expected '4'), "
        f"ENABLE_TENSORRT_BACKEND={enable_trt} (expected 1)"
    )
