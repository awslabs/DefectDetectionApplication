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
#
# Bug condition exploration test for the offline runtime pip-install defect.
#
# Spec: .kiro/specs/triton-offline-dependency-install
#   Property 1 (Bug Condition): Offline Runtime Pip Install Fails / Offline Triton
#   Setup Succeeds With Baked-In Dependencies.
#
# This test encodes the EXPECTED (fixed) behavior. It is written BEFORE the fix and
# is EXPECTED TO FAIL on the current (unfixed) code: the unfixed
# `create_virtual_env()` still shells out to
# `python3 -m pip install -r /dda_triton/model_conversion_requirements.txt`, so the
# `assert_not_called()` assertion below fails. That failure is the counterexample
# that confirms the bug exists. After the fix (verify-only, deps baked into the
# image at build time) the same test will PASS.
#
# Validates: Requirements 1.1, 1.2, 1.3 (bug condition) / 2.1, 2.2, 2.3 (fixed
# behavior the assertions encode).

import subprocess
from unittest.mock import patch

import pytest
from hypothesis import given, settings, HealthCheck, strategies as st

from dda_triton.triton_setup import create_virtual_env


# The pinned model conversion dependencies (single source of truth:
# src/backend/dda_triton/model_conversion_requirements.txt). Mapping of
# distribution name -> pinned version (None means unpinned in the requirements
# file). These are the deps that must be baked into the image and present at
# container startup so the offline runtime step needs no network.
PINNED_DEPS = {
    "setuptools": None,
    "wheel": None,
    "meson": None,
    "grpcio": "1.56.2",
    "grpcio-tools": "1.51.1",
    "protobuf": "4.25.8",
    "requests": "2.32.3",
    "opencv-python": None,
    "urllib3": "2.2.3",
    "scikit-learn": "1.0.2",
    "numpy": "1.24.3",
}

REQUIREMENTS_CONTENT = "\n".join(
    name if version is None else f"{name}=={version}"
    for name, version in PINNED_DEPS.items()
)


@pytest.fixture(scope="module")
def requirements_file(tmp_path_factory):
    """A real, present model_conversion_requirements.txt.

    The bug condition requires the requirements file to be present (that is what
    makes the unfixed code attempt the runtime `pip install`). Module-scoped so it
    is created once and reused across all hypothesis examples.
    """
    path = tmp_path_factory.mktemp("dda_triton") / "model_conversion_requirements.txt"
    path.write_text(REQUIREMENTS_CONTENT)
    return str(path)


# ---------------------------------------------------------------------------
# Strategy scoped to the concrete failing domain (the bug is deterministic):
#   networkAvailable == false, depsBakedIntoImage == false, requirements present.
# We vary the offline failure mode (error message, index host, exit status) to
# mirror the real-world name-resolution / "No matching distribution" failures.
# ---------------------------------------------------------------------------
OFFLINE_ERROR_MESSAGES = st.sampled_from(
    [
        "[Errno -3] Temporary failure in name resolution",
        "Could not find a version that satisfies the requirement protobuf==4.25.8 "
        "(from versions: none)",
        "No matching distribution found for protobuf==4.25.8",
        "Failed to establish a new connection: [Errno -3] Temporary failure in name "
        "resolution",
        "WARNING: Retrying ... Temporary failure in name resolution",
    ]
)

INDEX_HOSTS = st.sampled_from(
    [
        "pypi.org",
        "files.pythonhosted.org",
        "pypi.python.org",
        "internal-mirror.example.invalid",
    ]
)

RETURN_CODES = st.integers(min_value=1, max_value=255)


def _present_version(distribution_name, *args, **kwargs):
    """Simulate the baked-in image: every pinned dep resolves to its pinned version."""
    return PINNED_DEPS.get(distribution_name) or "0.0.0"


def _present_spec(name, *args, **kwargs):
    """Simulate the baked-in image: every pinned dep is importable/locatable."""
    return object()


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    error_message=OFFLINE_ERROR_MESSAGES,
    index_host=INDEX_HOSTS,
    return_code=RETURN_CODES,
)
def test_offline_create_virtual_env_does_no_network_install(
    requirements_file, error_message, index_host, return_code
):
    """Property 1: offline setup must issue NO pip install / no network call and
    must complete successfully with the pinned deps present (baked into the image).

    Setup simulates the bug-condition input domain:
      - networkAvailable == false  -> subprocess.check_call raises CalledProcessError
        mimicking name-resolution / "No matching distribution" failures.
      - depsBakedIntoImage == true (the FIXED precondition) -> importlib reports all
        pinned deps present, modelling deps baked in at build time.
      - requirements file present.

    EXPECTED (fixed) behavior asserted here:
      - create_virtual_env() does NOT call subprocess.check_call (no pip install,
        no network access).
      - create_virtual_env() completes successfully (does not raise).

    On UNFIXED code this FAILS: create_virtual_env() still runs
    `python3 -m pip install -r <requirements>` via subprocess.check_call, so
    assert_not_called() raises and surfaces the offline failure as the counterexample.
    """
    network_failure = subprocess.CalledProcessError(
        returncode=return_code,
        cmd=f"/usr/local/bin/python3 -m pip install -r {requirements_file}",
        output=f"Looking in indexes: https://{index_host}/simple\n{error_message}",
        stderr=error_message,
    )

    with patch(
        "dda_triton.triton_setup.subprocess.check_call",
        side_effect=network_failure,
    ) as mock_check_call, patch(
        "importlib.metadata.version", side_effect=_present_version
    ), patch(
        "importlib.util.find_spec", side_effect=_present_spec
    ):
        # Must complete successfully (no exception) even though the device is offline.
        create_virtual_env(requirements_file=requirements_file)

        # EXPECTED (fixed) behavior: verify-only, so NO network-dependent pip install
        # is ever attempted. On unfixed code this assertion fails because the runtime
        # pip install is still issued.
        mock_check_call.assert_not_called()


def test_offline_xavier_nx_primary_defect_no_pip_install(requirements_file):
    """Concrete primary-defect example (offline Jetson Xavier NX).

    Mirrors the device log: a runtime `pip install` that fails with
    `[Errno -3] Temporary failure in name resolution` and
    `No matching distribution found for protobuf==4.25.8`.

    Asserts the fixed behavior: no pip install is issued and setup completes.
    FAILS on unfixed code (the install is still attempted).
    """
    network_failure = subprocess.CalledProcessError(
        returncode=1,
        cmd=f"/usr/local/bin/python3 -m pip install -r {requirements_file}",
        output="[Errno -3] Temporary failure in name resolution",
        stderr="No matching distribution found for protobuf==4.25.8",
    )

    with patch(
        "dda_triton.triton_setup.subprocess.check_call",
        side_effect=network_failure,
    ) as mock_check_call, patch(
        "importlib.metadata.version", side_effect=_present_version
    ), patch(
        "importlib.util.find_spec", side_effect=_present_spec
    ):
        create_virtual_env(requirements_file=requirements_file)

        mock_check_call.assert_not_called()
