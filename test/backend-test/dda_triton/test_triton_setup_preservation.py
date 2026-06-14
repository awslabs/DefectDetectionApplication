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
# Preservation property tests for the offline runtime pip-install bugfix.
#
# Spec: .kiro/specs/triton-offline-dependency-install
#   Property 2 (Preservation): Online Path, File Copy, and Pinned Versions Unchanged.
#
# Methodology: observation-first. The behaviour below was OBSERVED on the UNFIXED
# code (see the recorded baseline in each test docstring) and is asserted here so it
# is preserved after the fix. These tests are written BEFORE the fix and are
# EXPECTED TO PASS on the current (unfixed) code: they capture the baseline the fix
# must not regress.
#
# Validates: Requirements 3.1, 3.2, 3.3, 3.4

import os

import pytest
from hypothesis import given, settings, HealthCheck, strategies as st

import dda_triton.triton_setup as ts
from dda_triton.triton_setup import cp_model_conversion_files, create_virtual_env


# ---------------------------------------------------------------------------
# Recorded baselines (OBSERVED on the UNFIXED code)
# ---------------------------------------------------------------------------
SOURCE_FOLDER = "/dda_triton/"
DEST_DDA_TRITON = "/aws_dda/dda_triton/"   # constants.DDA_TRITON_FOLDER
DEST_AWS_DDA = "/aws_dda"                   # constants.DDA_ROOT_FOLDER
RESOURCES_DEST = "/aws_dda/resources_for_copy"
RESOURCES_SRC = SOURCE_FOLDER + "resources_for_copy/"
# The path cp_model_conversion_files() probes to decide between a full tree copy and
# per-file copies (the destination dir, with a trailing slash).
RESOURCES_DEST_CHECK = "/aws_dda/resources_for_copy/"

# Files copied to the dda_triton destination (Req 3.2).
EXPECTED_DDA_TRITON_COPIES = {
    (SOURCE_FOLDER + "constants.py", DEST_DDA_TRITON),
    (SOURCE_FOLDER + "model_config_pb2.py", DEST_DDA_TRITON),
    (SOURCE_FOLDER + "model_autostart_utils.py", DEST_DDA_TRITON),
}

# Files copied to the aws_dda destination (Req 3.2).
EXPECTED_AWS_DDA_COPIES = {
    (SOURCE_FOLDER + "model_convertor.py", DEST_AWS_DDA),
    (SOURCE_FOLDER + "convert_model_cleanup.py", DEST_AWS_DDA),
    (SOURCE_FOLDER + "model_conversion_requirements.txt", DEST_AWS_DDA),
}

# Resource files copied individually when the resources destination already exists.
EXPECTED_RESOURCE_FILE_COPIES = {
    (RESOURCES_SRC + "ensemble_model", RESOURCES_DEST),
    (RESOURCES_SRC + "lfv_model_template.py", RESOURCES_DEST),
    (RESOURCES_SRC + "marshal_for_capture_template.py", RESOURCES_DEST),
}

# The pinned model conversion dependency versions, the single source of truth being
# src/backend/dda_triton/model_conversion_requirements.txt. None means the package is
# present in the file but intentionally unpinned (Req 3.4).
EXPECTED_PINNED_DEPS = {
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


def _patched_cp_call(dda_triton_exists, resources_exist):
    """Run cp_model_conversion_files() against a simulated destination pre-state and
    capture every filesystem mutation it requests, without touching the real disk.

    Returns (copy2_calls, copytree_calls, makedirs_calls).
    """
    from unittest.mock import patch

    real_exists = os.path.exists
    copy2_calls = []
    copytree_calls = []
    makedirs_calls = []

    def fake_exists(path):
        if path == DEST_DDA_TRITON:
            return dda_triton_exists
        if path == RESOURCES_DEST_CHECK:  # "/aws_dda/resources_for_copy/"
            return resources_exist
        return real_exists(path)

    with patch("shutil.copy2", side_effect=lambda s, d: copy2_calls.append((s, d))), \
         patch("shutil.copytree", side_effect=lambda s, d: copytree_calls.append((s, d))), \
         patch.object(ts.os, "makedirs", side_effect=lambda p: makedirs_calls.append(p)), \
         patch.object(ts.os.path, "exists", side_effect=fake_exists):
        cp_model_conversion_files()

    return copy2_calls, copytree_calls, makedirs_calls


# ---------------------------------------------------------------------------
# Req 3.2 — cp_model_conversion_files copies the SAME files to the SAME
# destinations regardless of the destination-folder pre-state.
# ---------------------------------------------------------------------------
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    dda_triton_exists=st.booleans(),
    resources_exist=st.booleans(),
)
def test_cp_model_conversion_files_copies_same_files_to_same_destinations(
    dda_triton_exists, resources_exist
):
    """Property 2 (Req 3.2): for any destination-folder pre-state, the model
    conversion files and resources are copied to the same destinations.

    OBSERVED baseline (UNFIXED code), for all four (dda_triton_exists,
    resources_exist) combinations:
      - constants.py / model_config_pb2.py / model_autostart_utils.py -> /aws_dda/dda_triton/
      - model_convertor.py / convert_model_cleanup.py /
        model_conversion_requirements.txt -> /aws_dda
      - resources: if /aws_dda/resources_for_copy/ exists -> the 3 resource files are
        copied individually into it; otherwise the whole resources_for_copy/ tree is
        copytree'd into /aws_dda/resources_for_copy
      - the dda_triton destination is created (makedirs) iff it did not already exist

    Validates: Requirements 3.2
    """
    copy2_calls, copytree_calls, makedirs_calls = _patched_cp_call(
        dda_triton_exists, resources_exist
    )
    copy2_set = set(copy2_calls)

    # The core model conversion files are always copied to their destinations.
    assert EXPECTED_DDA_TRITON_COPIES <= copy2_set
    assert EXPECTED_AWS_DDA_COPIES <= copy2_set

    # The destination folder is created only when it does not already exist.
    if dda_triton_exists:
        assert makedirs_calls == []
    else:
        assert makedirs_calls == [DEST_DDA_TRITON]

    # Resource handling depends solely on whether the resources destination exists.
    if resources_exist:
        # Individual resource files copied in; no tree copy.
        assert EXPECTED_RESOURCE_FILE_COPIES <= copy2_set
        assert copytree_calls == []
    else:
        # Whole resource tree copied; no individual resource copies.
        assert copytree_calls == [(RESOURCES_SRC, RESOURCES_DEST)]
        assert EXPECTED_RESOURCE_FILE_COPIES.isdisjoint(copy2_set)

    # No stray destinations: every copy2 target is one of the known destinations.
    allowed = (
        EXPECTED_DDA_TRITON_COPIES
        | EXPECTED_AWS_DDA_COPIES
        | EXPECTED_RESOURCE_FILE_COPIES
    )
    assert copy2_set <= allowed


def test_cp_model_conversion_files_default_pre_state_baseline():
    """Concrete baseline example (Req 3.2): the common case where neither destination
    pre-exists copies all six files plus a full resource-tree copytree.

    Validates: Requirements 3.2
    """
    copy2_calls, copytree_calls, makedirs_calls = _patched_cp_call(
        dda_triton_exists=False, resources_exist=False
    )
    assert set(copy2_calls) == EXPECTED_DDA_TRITON_COPIES | EXPECTED_AWS_DDA_COPIES
    assert copytree_calls == [(RESOURCES_SRC, RESOURCES_DEST)]
    assert makedirs_calls == [DEST_DDA_TRITON]


# ---------------------------------------------------------------------------
# Req 3.4 — pinned model conversion versions are exactly as recorded.
# ---------------------------------------------------------------------------
def _requirements_file_path():
    """Locate the real model_conversion_requirements.txt next to the dda_triton package."""
    pkg_dir = os.path.dirname(ts.__file__)
    return os.path.join(pkg_dir, "model_conversion_requirements.txt")


def _parse_requirements(path):
    """Parse a requirements file into {name: pinned_version_or_None}."""
    parsed = {}
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "==" in line:
                name, version = line.split("==", 1)
                parsed[name.strip()] = version.strip()
            else:
                parsed[line] = None
    return parsed


def test_model_conversion_requirements_pins_match_exactly():
    """Property 2 (Req 3.4): the pinned model conversion versions match the recorded
    baseline exactly, so conversion/inference behaviour is preserved.

    OBSERVED baseline (UNFIXED code) — src/backend/dda_triton/model_conversion_requirements.txt:
      grpcio==1.56.2, grpcio-tools==1.51.1, protobuf==4.25.8, requests==2.32.3,
      urllib3==2.2.3, scikit-learn==1.0.2, numpy==1.24.3, plus unpinned
      setuptools / wheel / meson / opencv-python.

    Validates: Requirements 3.4
    """
    parsed = _parse_requirements(_requirements_file_path())

    # Exactly the expected set of packages is present (no additions/removals).
    assert set(parsed) == set(EXPECTED_PINNED_DEPS)

    # Each pin matches exactly; the unpinned packages remain unpinned but present.
    for name, expected_version in EXPECTED_PINNED_DEPS.items():
        assert name in parsed, f"missing expected package: {name}"
        assert parsed[name] == expected_version, (
            f"{name}: expected pin {expected_version!r}, found {parsed[name]!r}"
        )


@settings(max_examples=25, deadline=None)
@given(name=st.sampled_from(
    [n for n, v in EXPECTED_PINNED_DEPS.items() if v is not None]
))
def test_each_pinned_version_preserved(name):
    """Property 2 (Req 3.4): each individually pinned dependency keeps its exact
    version in the requirements file.

    Validates: Requirements 3.4
    """
    parsed = _parse_requirements(_requirements_file_path())
    assert parsed.get(name) == EXPECTED_PINNED_DEPS[name]


# ---------------------------------------------------------------------------
# Req 3.1 — online / deps-present path: setup completes successfully.
# ---------------------------------------------------------------------------
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    requirements_body=st.lists(
        st.sampled_from(
            [
                "numpy==1.24.3",
                "protobuf==4.25.8",
                "grpcio==1.56.2",
                "scikit-learn==1.0.2",
                "requests==2.32.3",
                "setuptools",
                "wheel",
            ]
        ),
        min_size=1,
        max_size=7,
        unique=True,
    )
)
def test_online_deps_present_path_completes_successfully(tmp_path, requirements_body):
    """Property 2 (Req 3.1): on the online / deps-present path setup completes
    successfully (no exception raised).

    OBSERVED baseline (UNFIXED code): with the requirements file present and the
    dependency install succeeding (network available), create_virtual_env() returns
    normally (None) without raising. This observable success is what must be
    preserved; the test deliberately does NOT assert on whether a pip install is
    issued, since that is exactly what the fix changes.

    Validates: Requirements 3.1
    """
    from unittest.mock import patch

    req_file = tmp_path / "model_conversion_requirements.txt"
    req_file.write_text("\n".join(requirements_body) + "\n")

    def _present_version(distribution_name, *args, **kwargs):
        return "0.0.0"

    def _present_spec(modname, *args, **kwargs):
        return object()

    # Network available -> the install (if attempted) succeeds. Deps present ->
    # verification (if attempted) succeeds. Either way setup must complete.
    with patch.object(ts.subprocess, "check_call", return_value=0), \
         patch("importlib.metadata.version", side_effect=_present_version), \
         patch("importlib.util.find_spec", side_effect=_present_spec):
        result = create_virtual_env(requirements_file=str(req_file))

    assert result is None  # completes successfully, returns nothing, does not raise


def test_missing_requirements_file_skips_gracefully(tmp_path):
    """Baseline edge case to preserve: a non-existent requirements file is handled
    gracefully (logged and skipped) without raising and without any install.

    OBSERVED baseline (UNFIXED code): create_virtual_env() with a missing
    requirements_file logs "No model_conversion_requirements.txt file found ...
    Skipping dependency installation.", makes NO subprocess.check_call, and returns
    None without raising.

    Validates: Requirements 3.1
    """
    from unittest.mock import patch

    missing = str(tmp_path / "does_not_exist.txt")
    assert not os.path.exists(missing)

    with patch.object(ts.subprocess, "check_call", return_value=0) as mock_check_call:
        result = create_virtual_env(requirements_file=missing)

    assert result is None              # no exception, graceful skip
    mock_check_call.assert_not_called()  # no install attempted for a missing file
