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
"""Preservation property tests for the JetPack 4 TensorRT build-support bugfix
(spec: jetpack4-tensorrt-build-support, Property 2 — Preservation).

These tests capture the routing/gating decisions the build scripts make for
**non-bug-condition** inputs (JP5, JP6, x86_64/generic) and assert those exact
decisions, so the JP4 fix cannot silently change them. Following the
observation-first methodology, every assertion encodes the behavior the UNFIXED
``build-custom.sh`` + ``src/edgemlsdk/build.sh`` already produce today; the suite
PASSES on unfixed code (the baseline to preserve) and must keep passing after
the fix.

Technique (same as ``test_docker_profile_selection.py``): the real shell
decision blocks are extracted from the scripts and executed under ``bash -c``
with controlled inputs, so the contract is verified *behaviorally* against the
actual script text rather than a re-implementation. Inputs are passed via the
process environment (never interpolated into the snippet) so generated
component names cannot break or inject into the shell.

The routing/gating decision is a pure function of ``(componentName,
architecture)`` and runs on any host, so this suite is CI/this-device-OK.

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5
"""
import os
import re
import string
import subprocess
import unittest

from hypothesis import assume, given, settings
from hypothesis import strategies as st

_HERE = os.path.dirname(__file__)
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
BUILD_CUSTOM = os.path.join(_REPO_ROOT, "build-custom.sh")
EDGEMLSDK_BUILD = os.path.join(_REPO_ROOT, "src", "edgemlsdk", "build.sh")

# Property tests spawn a bash subprocess per example; keep the example count
# modest so the suite stays fast while still exercising a wide input space.
_PROP = settings(max_examples=40, deadline=None)


def _read(path):
    with open(path) as f:
        return f.read()


def _extract(content, pattern, label):
    m = re.search(pattern, content, re.DOTALL)
    assert m, f"Could not find the {label} block in the script"
    return m.group(1)


# ── Real decision blocks lifted verbatim from the scripts ───────────────────
_BC = _read(BUILD_CUSTOM)
_BS = _read(EDGEMLSDK_BUILD)

# build-custom.sh: component-name -> JETPACK_ARG (token detection if/elif chain).
_TOKEN_BLOCK = _extract(
    _BC, r'(if echo "\$COMPONENT_NAME" \| grep -q "JP6".*?\nfi)', "token-detection"
)
# build-custom.sh: IS_JP*/arch -> BACKEND_DOCKERFILE.
_BACKEND_BLOCK = _extract(
    _BC, r'(if \[ "\$IS_JP6" = "1" \].*?\nfi)', "BACKEND_DOCKERFILE"
)
# build-custom.sh: ARCHITECTURE -> docker-compose --profile selection.
_PROFILE_BLOCK = _extract(
    _BC, r'(if \[ "\$ARCHITECTURE" = "x86_64" \].*?\nfi)', "compose-profile"
)
# src/edgemlsdk/build.sh: jetpack -> DOCKERFILE (+ gating; the unfixed block
# never touches ENABLE_TENSORRT_BACKEND).
_JETPACK_BLOCK = _extract(
    _BS, r'(if \[ "\$jetpack" = "6" \].*?\nfi)', "edgemlsdk Dockerfile selection"
)


def _bash(snippet, env_overrides):
    env = dict(os.environ)
    env.update(env_overrides)
    result = subprocess.run(
        ["bash", "-c", snippet], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, f"snippet failed: {result.stderr}\n{snippet}"
    return result.stdout


def _grab(out, key):
    """Pull RESULT key emitted as ``key=[value]`` (brackets make empty parseable)."""
    m = re.search(re.escape(key) + r"=\[(.*?)\]", out)
    assert m, f"No {key} in output: {out!r}"
    return m.group(1)


def route(component_name, architecture):
    """Run build-custom.sh's real routing blocks; return (jetpack_arg, backend_dockerfile)."""
    snippet = (
        'IS_JP4=0\nIS_JP5=0\nIS_JP6=0\nJETPACK_ARG=""\n'
        + _TOKEN_BLOCK
        + "\n"
        + _BACKEND_BLOCK
        + '\necho "RES_JETPACK_ARG=[$JETPACK_ARG]"'
        + '\necho "RES_BACKEND=[$BACKEND_DOCKERFILE]"\n'
    )
    out = _bash(
        snippet,
        {"COMPONENT_NAME": component_name, "ARCHITECTURE": architecture},
    )
    return _grab(out, "RES_JETPACK_ARG"), _grab(out, "RES_BACKEND")


def gate(jetpack_arg):
    """Run src/edgemlsdk/build.sh's real Dockerfile/gating block; return
    (enable_tensorrt_backend, edgemlsdk_dockerfile). ENABLE_TENSORRT_BACKEND is
    pre-initialized to 0 so we observe whether the block flips it."""
    snippet = (
        'ENABLE_TENSORRT_BACKEND=0\nubuntu="18.04"\n'
        + _JETPACK_BLOCK
        + '\necho "RES_TRT=[$ENABLE_TENSORRT_BACKEND]"'
        + '\necho "RES_DOCKERFILE=[$DOCKERFILE]"\n'
    )
    out = _bash(snippet, {"jetpack": jetpack_arg})
    return _grab(out, "RES_TRT"), _grab(out, "RES_DOCKERFILE")


def compose_profiles(architecture):
    """Run build-custom.sh's real profile-selection block with docker-compose
    stubbed to echo its args; return the list of --profile values chosen."""
    snippet = (
        'docker-compose() { echo "COMPOSE_ARGS:$*"; }\n'
        'IMAGE_VER="18.04"\nPYTHON_VERSION="3.11"\n'
        + _PROFILE_BLOCK
        + "\n"
    )
    out = _bash(snippet, {"ARCHITECTURE": architecture})
    line = next((l for l in out.splitlines() if l.startswith("COMPOSE_ARGS:")), None)
    assert line is not None, f"profile block did not invoke docker-compose: {out!r}"
    return re.findall(r"--profile\s+(\S+)", line)


# ── Hypothesis strategies ───────────────────────────────────────────────────
# Realistic component-name filler: alphanumerics plus the punctuation that
# appears in real Greengrass component names. Excludes whitespace/control chars
# (component names never contain them) so env-var transport and line-based grep
# stay well-defined.
_FILLER_ALPHABET = string.ascii_letters + string.digits + "._-"
_filler = st.text(alphabet=_FILLER_ALPHABET, max_size=24)
_ARCH = st.sampled_from(["aarch64", "x86_64"])


def _name_with(token):
    """A component name guaranteed to contain ``token`` somewhere (start/mid/end)."""
    return st.tuples(_filler, _filler).map(lambda fg: fg[0] + token + fg[1])


class TestJp6Preservation(unittest.TestCase):
    """Req 3.1: any input containing JP6 -> JETPACK_ARG=6 / Dockerfile.jp6, any arch."""

    @_PROP
    @given(name=_name_with("JP6"), arch=_ARCH)
    def test_jp6_routes_to_6(self, name, arch):
        jetpack_arg, backend = route(name, arch)
        self.assertEqual(jetpack_arg, "6", f"{name!r}@{arch}")
        self.assertEqual(backend, "Dockerfile.jp6", f"{name!r}@{arch}")
        # And the JP6 jetpack arg keeps the JP6 edgemlsdk Dockerfile, TRT off.
        trt, dockerfile = gate("6")
        self.assertEqual(trt, "0")
        self.assertEqual(dockerfile, "Dockerfile.jp6")


class TestJp5Preservation(unittest.TestCase):
    """Req 3.2: any input containing JP5 (and not JP6) -> JETPACK_ARG=5 / Dockerfile.jp5."""

    @_PROP
    @given(name=_name_with("JP5"), arch=_ARCH)
    def test_jp5_routes_to_5(self, name, arch):
        assume("JP6" not in name)  # JP6 has higher precedence; excluded here.
        jetpack_arg, backend = route(name, arch)
        self.assertEqual(jetpack_arg, "5", f"{name!r}@{arch}")
        self.assertEqual(backend, "Dockerfile.jp5", f"{name!r}@{arch}")
        trt, dockerfile = gate("5")
        self.assertEqual(trt, "0")
        self.assertEqual(dockerfile, "Dockerfile.jp5")


class TestX86Preservation(unittest.TestCase):
    """Req 3.3: x86_64 stays CPU/python-only — TRT off, no -j, generic Dockerfile,
    generic profile only."""

    @_PROP
    @given(name=_name_with("arm64"), arch=st.just("x86_64"))
    def test_tokenless_x86_is_generic_cpu_only(self, name, arch):
        assume("JP5" not in name and "JP6" not in name)  # tokenless component.
        jetpack_arg, backend = route(name, arch)
        self.assertEqual(jetpack_arg, "", f"{name!r} should get no -j arg")
        self.assertEqual(backend, "Dockerfile")
        trt, dockerfile = gate(jetpack_arg)
        self.assertEqual(trt, "0", "x86_64 must keep TensorRT backend disabled")
        self.assertEqual(dockerfile, "Dockerfile")
        self.assertEqual(compose_profiles(arch), ["generic"])

    @_PROP
    @given(name=_filler, arch=st.just("x86_64"))
    def test_any_x86_uses_generic_profile_only(self, name, arch):
        # The compose-profile decision is purely architecture-driven: every
        # x86_64 build gets the generic profile only, regardless of token.
        self.assertEqual(compose_profiles(arch), ["generic"])

    def test_aarch64_uses_tegra_and_generic(self):
        # Baseline for the other arch: aarch64 builds both profiles (unchanged).
        self.assertEqual(compose_profiles("aarch64"), ["tegra", "generic"])


class TestBuildShGating(unittest.TestCase):
    """build.sh with no -j / -j 5 / -j 6 -> ENABLE_TENSORRT_BACKEND=0."""

    @_PROP
    @given(jetpack_arg=st.sampled_from(["", "5", "6"]))
    def test_non_jp4_keeps_tensorrt_disabled(self, jetpack_arg):
        trt, _ = gate(jetpack_arg)
        self.assertEqual(trt, "0", f"jetpack={jetpack_arg!r} must keep TRT off")

    def test_dockerfile_mapping(self):
        self.assertEqual(gate(""), ("0", "Dockerfile"))
        self.assertEqual(gate("5"), ("0", "Dockerfile.jp5"))
        self.assertEqual(gate("6"), ("0", "Dockerfile.jp6"))


class TestTokenPrecedence(unittest.TestCase):
    """JP6/JP5 detection precedence stays ahead of any tokenless fallthrough,
    across case / token-placement variants."""

    @_PROP
    @given(a=_filler, b=_filler, c=_filler)
    def test_jp6_beats_jp5(self, a, b, c):
        # A name containing BOTH tokens must resolve to JP6 (checked first).
        name = a + "JP6" + b + "JP5" + c
        self.assertEqual(route(name, "aarch64")[0], "6", name)
        name2 = a + "JP5" + b + "JP6" + c
        self.assertEqual(route(name2, "aarch64")[0], "6", name2)

    @_PROP
    @given(name=_name_with("JP5"), arch=_ARCH)
    def test_jp5_beats_tokenless_fallthrough(self, name, arch):
        # JP5 + arm64 on aarch64 must stay JP5 (not fall through to a JP4/tokenless route).
        assume("JP6" not in name)
        full = name + "arm64"
        self.assertEqual(route(full, arch)[0], "5", full)

    def test_lowercase_tokens_are_not_detected(self):
        # grep is case-sensitive: lowercase jp5/jp6 are NOT JetPack tokens.
        # On x86_64 such names route as tokenless (no -j) on both unfixed and
        # fixed code (the JP4 branch never applies to x86_64).
        self.assertEqual(route("aws.edgeml.dda.LocalServer.arm64jp6", "x86_64")[0], "")
        self.assertEqual(route("aws.edgeml.dda.LocalServer.arm64jp5", "x86_64")[0], "")


class TestStaticPipelineWiring(unittest.TestCase):
    """Req 3.4: build-custom.sh still wires the audit guard, the backend
    unit-test step (incl. test_docker_profile_selection.py), and packaging.
    These are statically asserted so the fix can't drop any of them."""

    def setUp(self):
        self.bc = _BC

    def test_invokes_interpreter_version_audit_guard(self):
        self.assertIn("test/python_version_audit.py", self.bc)
        self.assertIn("python3 test/python_version_audit.py", self.bc)

    def test_runs_backend_unit_tests_including_profile_selection(self):
        self.assertRegex(self.bc, r"python\$\{PYTHON_VERSION\}\s+-m\s+pytest")
        self.assertIn(
            "test/backend-test/host_scripts/test_docker_profile_selection.py",
            self.bc,
        )

    def test_still_packages_the_artifact(self):
        self.assertIn('zip -r -X "$ARCHIVE"', self.bc)
        self.assertIn(
            'cp "$ARCHIVE" ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/',
            self.bc,
        )


if __name__ == "__main__":
    unittest.main()
