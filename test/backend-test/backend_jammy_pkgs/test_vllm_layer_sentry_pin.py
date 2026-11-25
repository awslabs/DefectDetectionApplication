# Copyright 2026 Amazon Web Services, Inc.
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
"""The vLLM layers of the two Jetson images that install vLLM cap sentry-sdk
below 3.

vllm requires ``fastapi[standard]``; its ``fastapi-cloud-cli`` extra depends
on an unbounded ``sentry-sdk``. sentry-sdk 3.0 removed ``configure_scope``,
which the app's pinned ``asgi-correlation-id==4.2.0`` imports from its Sentry
extension on every request (the extension activates on the mere presence of
``sentry_sdk``), so an image whose vLLM layer resolved sentry-sdk 3.x answered
every request with HTTP 500. The ``"sentry-sdk<3"`` co-constraint rides the
same pip resolution as the existing numpy / transformers co-constraints:

* ``src/backend/Dockerfile.jp6`` -- the prebuilt-wheel vLLM layer's
  ``pip install`` line;
* ``src/backend/edge_ml1_p_camera_management/install_vllm_gpu.sh`` -- the
  from-source build's wheel install line, which ``Dockerfile.jp7``'s vLLM
  layer runs.

The JP5 / amd64 / amd64Nvidia images have no vLLM layer and never resolve
sentry-sdk, so they carry no cap. Text-only structural checks (the
``test_jp7_vllm_layer.py`` convention: no docker, no subprocess); import-light
so it runs under ``pytest test/backend-test/backend_jammy_pkgs/ --noconftest``.

Run (finite, non-watch):
    python3 -m pytest test/backend-test/backend_jammy_pkgs/test_vllm_layer_sentry_pin.py \
        -p no:cacheprovider --noconftest -v
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))

DOCKERFILE_JP6_REL = os.path.join("src", "backend", "Dockerfile.jp6")
DOCKERFILE_JP7_REL = os.path.join("src", "backend", "Dockerfile.jp7")
SCRIPT_REL = os.path.join(
    "src", "backend", "edge_ml1_p_camera_management", "install_vllm_gpu.sh"
)
REQUIREMENTS_REL = os.path.join("src", "backend", "requirements.txt")
SENTRY_CAP = '"sentry-sdk<3"'


def _read(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _noncomment_lines(text):
    return [
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    ]


def _logical_instructions(text):
    """Join backslash-continued physical lines into logical Dockerfile
    instructions (comment lines dropped)."""
    logical = []
    buf = None
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        buf = line if buf is None else buf + "\n" + line
        if line.rstrip().endswith("\\"):
            continue
        logical.append(buf)
        buf = None
    if buf is not None:
        logical.append(buf)
    return logical


def _single_run(dockerfile_rel, pattern):
    """The single logical RUN instruction matching ``pattern`` (a regex,
    DOTALL)."""
    runs = [
        ins for ins in _logical_instructions(_read(dockerfile_rel))
        if ins.lstrip().startswith("RUN") and re.search(pattern, ins, re.S)
    ]
    assert len(runs) == 1, (
        f"{dockerfile_rel}: expected exactly one RUN matching {pattern!r}, "
        f"found {len(runs)}"
    )
    return runs[0]


# A pip install whose argument list (up to the next ';' or '&&') names
# ${VLLM_SPEC}: the one command that resolves vllm and its dependencies.
_JP6_VLLM_PIP_INSTALL = r"pip install(?:(?!;|&&).)*\$\{VLLM_SPEC\}"


def test_jp6_vllm_layer_caps_sentry_sdk_below_3():
    """The JP6 vLLM layer's ``pip install`` of ``${VLLM_SPEC}`` carries the
    ``"sentry-sdk<3"`` co-constraint on the same line as the numpy and
    transformers co-constraints (one resolution, so the cap binds)."""
    run = _single_run(DOCKERFILE_JP6_REL, _JP6_VLLM_PIP_INSTALL)
    # Every pip install's argument list (up to the next ';' or '&&'); the
    # vLLM one names ${VLLM_SPEC}. The else-branch echo mentions VLLM_SPEC
    # too, but sits after a ';' and is not a pip install argument list.
    installs = [
        m.group(0) for m in re.finditer(r"pip install(?:(?!;|&&).)*", run, re.S)
        if "${VLLM_SPEC}" in m.group(0)
    ]
    assert len(installs) == 1, (
        f"{DOCKERFILE_JP6_REL}: expected exactly one pip install of "
        f"${{VLLM_SPEC}}, found {len(installs)}"
    )
    vllm_install = installs[0]
    for co_constraint in ('"numpy>=1.26,<2"', SENTRY_CAP):
        assert co_constraint in vllm_install, (
            f"{DOCKERFILE_JP6_REL}: the vLLM pip install must carry "
            f"{co_constraint} beside ${{VLLM_SPEC}}"
        )


def test_jp7_vllm_wheel_install_caps_sentry_sdk_below_3():
    """The from-source installer's wheel install line (``${WHL}``) carries the
    ``"sentry-sdk<3"`` co-constraint beside ``"numpy>=1.24,<2"``."""
    installs = [
        line for line in _noncomment_lines(_read(SCRIPT_REL))
        if "pip install" in line and '"${WHL}"' in line
    ]
    assert len(installs) == 1, (
        f"{SCRIPT_REL}: expected exactly one pip install of the built wheel, "
        f"found {len(installs)}"
    )
    for co_constraint in ('"numpy>=1.24,<2"', SENTRY_CAP):
        assert co_constraint in installs[0], (
            f"{SCRIPT_REL}: the wheel install must carry {co_constraint}"
        )


def test_jp7_vllm_layer_runs_the_capped_installer():
    """Dockerfile.jp7's vLLM layer installs vLLM only through
    install_vllm_gpu.sh (no other pip install of vllm), so the cap in the
    script is the one the JP7 image resolves."""
    text = _read(DOCKERFILE_JP7_REL)
    assert "install_vllm_gpu.sh" in _single_run(DOCKERFILE_JP7_REL, "install_vllm_gpu.sh")
    for line in _noncomment_lines(text):
        if "pip install" in line:
            assert not re.search(r"\bvllm\b", line), (
                f"{DOCKERFILE_JP7_REL}: vllm must be installed only by "
                f"install_vllm_gpu.sh (which caps sentry-sdk), found: {line!r}"
            )


def test_cap_is_the_only_sentry_sdk_mention_outside_the_vllm_layers():
    """The app itself never depends on sentry-sdk: requirements.txt names
    neither sentry-sdk nor a version of asgi-correlation-id other than the
    pinned 4.2.0 whose Sentry extension the cap protects."""
    requirements = _noncomment_lines(_read(REQUIREMENTS_REL))
    assert not any("sentry" in line.lower() for line in requirements), (
        f"{REQUIREMENTS_REL}: sentry-sdk must not become a direct dependency; "
        f"lift the vLLM-layer cap deliberately instead"
    )
    assert "asgi-correlation-id==4.2.0" in requirements, (
        f"{REQUIREMENTS_REL}: asgi-correlation-id is no longer pinned to 4.2.0; "
        f"revisit the sentry-sdk<3 cap (5.x no longer imports configure_scope)"
    )
