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
# Bug condition exploration test for the "inference_runtimes.py missing on the
# subsequent-setup path" defect.
#
# Spec: .kiro/specs/triton-inference-runtimes-missing-fix
#   Property 1 (Bug Condition): inference_runtimes.py must end up present in
#   /aws_dda/resources_for_copy after cp_model_conversion_files() runs on the
#   subsequent-setup path (destination resources dir already exists).
#
# This module encodes the EXPECTED (fixed) behavior. It is written BEFORE the fix
# and the PRIMARY property test is EXPECTED TO FAIL on the current (unfixed) code:
# the unfixed else-branch copies only the hand-maintained allowlist
# (ensemble_model, lfv_model_template.py, marshal_for_capture_template.py), which
# omits inference_runtimes.py, so the assertion that the file lands in the
# destination fails. That failure is the counterexample that confirms the bug.
# After the fix (full re-sync of resources_for_copy) the same test will PASS.
#
# Two companion reproduction tests document the downstream consequences of the
# missing file and are EXPECTED TO PASS on the unfixed code (they assert the
# current buggy behavior):
#   * downstream staging (Bug 1.2): the file is NOT staged into the model version
#     dir and only a warning is logged.
#   * import failure (Bug 1.3): importing the runtime module from a model version
#     dir that lacks it raises ModuleNotFoundError: No module named
#     'inference_runtimes'.
#
# Validates: Requirements 1.1, 1.2, 1.3, 2.1

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest
from hypothesis import given, settings, HealthCheck, strategies as st

import dda_triton.triton_setup as ts
from dda_triton.triton_setup import cp_model_conversion_files


# ---------------------------------------------------------------------------
# Path-translation harness
#
# cp_model_conversion_files() hard-codes the source ("/dda_triton/") and the
# destinations ("/aws_dda", "/aws_dda/dda_triton/", "/aws_dda/resources_for_copy").
# To exercise the real function without touching real system directories we patch
# the shutil / os primitives it uses and translate those hard-coded absolute
# prefixes onto temp directories, then let the REAL copy actually run. This lets
# us assert on the real resulting destination contents (Expected Behavior 2.1).
# ---------------------------------------------------------------------------

# Files the source resources_for_copy always ships (the source always includes
# inference_runtimes.py). ensemble_model is a plain file in the shipped tree.
SOURCE_RESOURCE_FILES = [
    "inference_runtimes.py",
    "ensemble_model",
    "lfv_model_template.py",
    "marshal_for_capture_template.py",
]

# The stale allowlist that the unfixed else-branch re-copies. Notably this omits
# inference_runtimes.py -- that omission is the bug.
ALLOWLIST_RESOURCE_FILES = [
    "ensemble_model",
    "lfv_model_template.py",
    "marshal_for_capture_template.py",
]

SRC_AUX_DDA_TRITON = ["constants.py", "model_config_pb2.py", "model_autostart_utils.py"]
SRC_AUX_AWS_DDA = [
    "model_convertor.py",
    "convert_model_cleanup.py",
    "model_conversion_requirements.txt",
    # Added additively by the vllm-triton-inference spec (task 9.2).
    "vllm_model_prep.py",
]


def _make_translate(mapping):
    """Return a translate(path) that rewrites known hard-coded prefixes onto temp
    dirs. `mapping` is a list of (prefix, replacement) ordered longest-prefix first.
    """

    def translate(path):
        p = os.fspath(path)
        for prefix, repl in mapping:
            if p == prefix or p.startswith(prefix):
                return repl + p[len(prefix):]
        return p

    return translate


def _build_harness(base, stale_files, nested_subtree):
    """Lay out source + destination trees under `base` and return (translate, dest_resources).

    * source (/dda_triton/) gets the full aux files and a resources_for_copy that
      INCLUDES inference_runtimes.py.
    * the destination /aws_dda/resources_for_copy is PRE-CREATED (forcing the
      else / subsequent-setup branch) with a stale file set that OMITS
      inference_runtimes.py, plus arbitrary incidental contents.
    """
    src_root = os.path.join(base, "src_dda_triton")
    src_resources = os.path.join(src_root, "resources_for_copy")
    dest_aws_dda = os.path.join(base, "aws_dda")
    dest_dda_triton = os.path.join(dest_aws_dda, "dda_triton")
    dest_resources = os.path.join(dest_aws_dda, "resources_for_copy")

    os.makedirs(src_resources)
    os.makedirs(dest_dda_triton)
    os.makedirs(dest_resources)

    # Source aux files.
    for name in SRC_AUX_DDA_TRITON + SRC_AUX_AWS_DDA:
        with open(os.path.join(src_root, name), "w") as fh:
            fh.write(f"# source {name}\n")
    # Source resources -- always includes inference_runtimes.py.
    for name in SOURCE_RESOURCE_FILES:
        with open(os.path.join(src_resources, name), "w") as fh:
            fh.write(f"# source resource {name}\n")

    # Pre-existing (stale) destination resources: a subset of the allowlist, never
    # inference_runtimes.py, plus incidental stale/extra files.
    for name in stale_files:
        with open(os.path.join(dest_resources, name), "w") as fh:
            fh.write(f"# stale {name}\n")
    if nested_subtree:
        nested_dir = os.path.join(dest_resources, "leftover_subtree")
        os.makedirs(nested_dir)
        with open(os.path.join(nested_dir, "old.txt"), "w") as fh:
            fh.write("stale nested content\n")

    # Longest-prefix-first so /aws_dda/resources_for_copy and /aws_dda/dda_triton
    # win over the bare /aws_dda mapping.
    mapping = [
        ("/aws_dda/resources_for_copy", dest_resources),
        ("/aws_dda/dda_triton", dest_dda_triton),
        ("/aws_dda", dest_aws_dda),
        ("/dda_triton", src_root),
    ]
    return _make_translate(mapping), dest_resources


def _run_cp_with_harness(translate):
    """Run cp_model_conversion_files() with shutil/os primitives redirected through
    `translate` so the real copies land in the temp harness."""
    orig_copy2 = shutil.copy2
    orig_copytree = shutil.copytree
    orig_exists = os.path.exists
    orig_makedirs = os.makedirs

    def t_copy2(src, dst, *a, **k):
        return orig_copy2(translate(src), translate(dst), *a, **k)

    def t_copytree(src, dst, *a, **k):
        return orig_copytree(translate(src), translate(dst), *a, **k)

    def t_exists(path):
        return orig_exists(translate(path))

    def t_makedirs(path, *a, **k):
        return orig_makedirs(translate(path), *a, **k)

    with patch("shutil.copy2", side_effect=t_copy2), \
         patch("shutil.copytree", side_effect=t_copytree), \
         patch.object(ts.os.path, "exists", side_effect=t_exists), \
         patch.object(ts.os, "makedirs", side_effect=t_makedirs):
        cp_model_conversion_files()


# ---------------------------------------------------------------------------
# Property 1 (Bug Condition / Expected Behavior 2.1) -- PRIMARY test.
# EXPECTED TO FAIL on unfixed code.
# ---------------------------------------------------------------------------

# Incidental destination contents to vary, none of which is inference_runtimes.py.
_INCIDENTAL_FILES = st.lists(
    st.sampled_from(
        [
            "ensemble_model",
            "lfv_model_template.py",
            "marshal_for_capture_template.py",
            "stale_config.json",
            "old_notes.txt",
            "leftover.pyc",
            "README",
        ]
    ),
    unique=True,
    max_size=7,
)


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(stale_files=_INCIDENTAL_FILES, nested_subtree=st.booleans())
def test_subsequent_setup_delivers_inference_runtimes(stale_files, nested_subtree):
    """Property 1: for any subsequent-setup invocation (destination resources dir
    already exists and its stale file set omits inference_runtimes.py while the
    source includes it), cp_model_conversion_files() SHALL leave
    inference_runtimes.py present in /aws_dda/resources_for_copy.

    This assertion encodes Expected Behavior 2.1. On the UNFIXED code the
    else-branch copies only the allowlist (which omits inference_runtimes.py), so
    the file is never delivered and this test FAILS -- that failure is the
    counterexample proving the bug (Bug 1.1).

    Validates: Requirements 1.1, 2.1
    """
    base = tempfile.mkdtemp(prefix="cp_harness_")
    try:
        translate, dest_resources = _build_harness(base, stale_files, nested_subtree)

        # Precondition: the bug condition holds -- destination exists, file absent.
        assert os.path.exists(dest_resources)
        assert not os.path.exists(os.path.join(dest_resources, "inference_runtimes.py"))

        _run_cp_with_harness(translate)

        # Sanity: the else-branch actually ran (an allowlisted file was delivered),
        # so a failure below is due to the property, not a broken harness.
        assert os.path.exists(os.path.join(dest_resources, "lfv_model_template.py"))

        # Expected Behavior 2.1: inference_runtimes.py must be present.
        assert os.path.exists(os.path.join(dest_resources, "inference_runtimes.py")), (
            "inference_runtimes.py was not delivered to the destination "
            "resources_for_copy on the subsequent-setup path"
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_subsequent_setup_missing_file_minimal_counterexample():
    """Deterministic minimal counterexample (Bug 1.1 / Expected Behavior 2.1).

    Destination resources_for_copy pre-exists with the stale allowlist only (no
    inference_runtimes.py); after cp_model_conversion_files() the file must be
    present. FAILS on unfixed code.

    Validates: Requirements 1.1, 2.1
    """
    base = tempfile.mkdtemp(prefix="cp_harness_min_")
    try:
        translate, dest_resources = _build_harness(
            base, stale_files=list(ALLOWLIST_RESOURCE_FILES), nested_subtree=False
        )
        _run_cp_with_harness(translate)
        assert os.path.exists(os.path.join(dest_resources, "inference_runtimes.py")), (
            "counterexample: <dest>/resources_for_copy/inference_runtimes.py does "
            "not exist after subsequent-setup on unfixed code"
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# Downstream staging reproduction (Bug 1.2) -- EXPECTED TO PASS on unfixed code.
# ---------------------------------------------------------------------------
def test_downstream_staging_skips_missing_inference_runtimes():
    """Bug 1.2 reproduction: when inference_runtimes.py is absent from the resources
    dir that model_convertor.py stages from, the staging path does NOT copy it into
    the model version directory and only logs a warning (no error).

    This documents the buggy downstream consequence of the missing file and PASSES
    on the unfixed code.

    Validates: Requirements 1.2
    """
    import dda_triton.model_convertor as mc

    base = tempfile.mkdtemp(prefix="stage_harness_")
    try:
        # Fake "working dir" for model_convertor with a resources_for_copy that has
        # lfv_model_template.py but NOT inference_runtimes.py.
        stage_dir = os.path.join(base, "aws_dda")
        stage_resources = os.path.join(stage_dir, "resources_for_copy")
        os.makedirs(stage_resources)
        with open(os.path.join(stage_resources, "lfv_model_template.py"), "w") as fh:
            fh.write("# template\n")
        # Intentionally do NOT create inference_runtimes.py here.

        model_repo_dir = os.path.join(base, "model_repo")
        os.makedirs(model_repo_dir)

        # deployed model path with a dummy artifact so create_sym_links succeeds.
        deployed_model_path = os.path.join(base, "deployed")
        os.makedirs(deployed_model_path)
        with open(os.path.join(deployed_model_path, "weights.bin"), "w") as fh:
            fh.write("w")

        orig_realpath = os.path.realpath

        def fake_realpath(p, *a, **k):
            if str(p).endswith("model_convertor.py"):
                return os.path.join(stage_dir, "model_convertor.py")
            return orig_realpath(p, *a, **k)

        # Capture warning records emitted via the root logger.
        records = []

        class _ListHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _ListHandler(level=logging.WARNING)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        try:
            with patch.object(mc.os.path, "realpath", side_effect=fake_realpath):
                mc._create_base_model_structure(
                    model_repo_dir=model_repo_dir,
                    deployed_model_path=deployed_model_path,
                    model_name="m",
                    model_version="1",
                    manifest={},
                )
        finally:
            root_logger.removeHandler(handler)

        version_dir = os.path.join(model_repo_dir, "base_m", "1")

        # The staging block was reached (model.py was copied from the template).
        assert os.path.exists(os.path.join(version_dir, "model.py"))

        # Bug 1.2: inference_runtimes.py is NOT staged next to model.py ...
        assert not os.path.exists(os.path.join(version_dir, "inference_runtimes.py"))

        # ... and only a warning is logged (no error path for the missing file).
        warned = [
            r
            for r in records
            if r.levelno == logging.WARNING
            and "inference_runtimes.py not found" in r.getMessage()
        ]
        assert warned, "expected a warning about the missing inference_runtimes.py"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# Import-failure reproduction (Bug 1.3) -- EXPECTED TO PASS on unfixed code.
# ---------------------------------------------------------------------------
def test_missing_module_raises_module_not_found():
    """Bug 1.3 reproduction: a model version dir that lacks inference_runtimes.py --
    even with that dir on sys.path (the guard lfv_model_template.py adds) -- cannot
    import the runtime module. `from inference_runtimes import make_runner` raises
    ModuleNotFoundError: No module named 'inference_runtimes'.

    Run in a clean subprocess whose PYTHONPATH is exactly the empty version dir so
    the result is hermetic. PASSES on unfixed code.

    Validates: Requirements 1.3
    """
    base = tempfile.mkdtemp(prefix="import_harness_")
    try:
        version_dir = os.path.join(base, "base_m", "1")
        os.makedirs(version_dir)
        # The version dir deliberately has no inference_runtimes.py.
        assert not os.path.exists(os.path.join(version_dir, "inference_runtimes.py"))

        env = dict(os.environ)
        env["PYTHONPATH"] = version_dir  # mirror the template's sys.path guard
        proc = subprocess.run(
            [sys.executable, "-c", "from inference_runtimes import make_runner"],
            capture_output=True,
            text=True,
            env=env,
            cwd=base,
        )
        assert proc.returncode != 0
        assert (
            "ModuleNotFoundError: No module named 'inference_runtimes'" in proc.stderr
        ), f"unexpected stderr:\n{proc.stderr}"
    finally:
        shutil.rmtree(base, ignore_errors=True)
