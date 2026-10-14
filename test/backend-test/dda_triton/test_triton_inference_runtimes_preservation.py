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
# Preservation property tests for the "inference_runtimes.py missing on the
# subsequent-setup path" bugfix.
#
# Spec: .kiro/specs/triton-inference-runtimes-missing-fix
#   Property 2 (Preservation): for every NON-buggy invocation, the fixed
#   cp_model_conversion_files() SHALL behave identically to the original.
#
# METHODOLOGY: observation-first. These tests were written by first running the
# UNFIXED code, recording the actual resulting outputs, and then asserting those
# recorded outputs. They therefore PASS on the unfixed code (they capture the
# baseline behavior to preserve). After the fix is applied (task 3.3) these SAME
# tests must still pass -- that is what proves preservation.
#
# Recorded baseline observations on the UNFIXED code (see also the report):
#   * First-time path (destination /aws_dda/resources_for_copy does NOT exist):
#     shutil.copytree delivers the ENTIRE source resources_for_copy tree to the
#     destination -- inference_runtimes.py, ensemble_model, lfv_model_template.py,
#     marshal_for_capture_template.py, and any additional / nested source content.
#     The destination tree is byte-for-byte equal to the source tree.
#   * Auxiliary copies (both branches): files_to_copy_to_dda_triton
#     (constants.py, model_config_pb2.py, model_autostart_utils.py) land in
#     /dda_triton; files_to_copy_to_aws_dda (model_convertor.py,
#     convert_model_cleanup.py, model_conversion_requirements.txt) land in
#     /aws_dda. Content is preserved.
#   * DLR-only staging (model_convertor.py) when inference_runtimes.py is
#     legitimately absent: _create_base_model_structure() returns True (proceeds),
#     stages model.py, does NOT stage inference_runtimes.py, and logs only a
#     WARNING (no error, no crash).
#   * Healthy device staging when inference_runtimes.py IS present: it is staged
#     next to model.py and the function returns True.
#
# Validates: Requirements 3.1, 3.2, 3.3, 3.4

import logging
import os
import shutil
import tempfile
from unittest.mock import patch

from hypothesis import given, settings, HealthCheck, strategies as st

import dda_triton.triton_setup as ts
from dda_triton.triton_setup import cp_model_conversion_files


# ---------------------------------------------------------------------------
# Path-translation harness (mirrors test_triton_inference_runtimes_bug.py).
#
# cp_model_conversion_files() hard-codes the source ("/dda_triton/") and the
# destinations ("/aws_dda", "/aws_dda/dda_triton/", "/aws_dda/resources_for_copy").
# We patch the shutil / os primitives it uses and translate those hard-coded
# absolute prefixes onto temp directories, then let the REAL copy run so we can
# assert on the real resulting destination contents.
# ---------------------------------------------------------------------------

# The source resources_for_copy always ships these. ensemble_model is a plain
# FILE in the shipped tree (not a directory).
SOURCE_RESOURCE_FILES = [
    "inference_runtimes.py",
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


def _read_tree(root):
    """Return {relpath: content} for every file under `root` (dirs implied)."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            with open(full) as fh:
                out[rel] = fh.read()
    return out


def _build_firsttime_harness(base, extra_files, nested_subtree):
    """Lay out source + destination for the FIRST-TIME path (non-buggy input).

    The destination /aws_dda/resources_for_copy does NOT exist, forcing the
    `copytree` branch. The source resources_for_copy always includes the shipped
    files plus any generated extras / nested subtree, so we can assert the
    destination ends up byte-for-byte equal to the source (Requirement 3.1).
    """
    src_root = os.path.join(base, "src_dda_triton")
    src_resources = os.path.join(src_root, "resources_for_copy")
    dest_aws_dda = os.path.join(base, "aws_dda")
    dest_dda_triton = os.path.join(dest_aws_dda, "dda_triton")
    dest_resources = os.path.join(dest_aws_dda, "resources_for_copy")

    os.makedirs(src_resources)
    os.makedirs(dest_aws_dda)
    # Intentionally do NOT create dest_resources (first-time path).
    # dest_dda_triton is left absent so the function's makedirs branch runs too.

    # Source aux files.
    for name in SRC_AUX_DDA_TRITON + SRC_AUX_AWS_DDA:
        with open(os.path.join(src_root, name), "w") as fh:
            fh.write(f"# source {name}\n")

    # Source resources -- always includes the shipped files ...
    for name in SOURCE_RESOURCE_FILES:
        with open(os.path.join(src_resources, name), "w") as fh:
            fh.write(f"# source resource {name}\n")
    # ... plus generated extra files (proves copytree delivers everything).
    for name in extra_files:
        with open(os.path.join(src_resources, name), "w") as fh:
            fh.write(f"# extra {name}\n")
    # ... plus an optional nested subtree.
    if nested_subtree:
        nested_dir = os.path.join(src_resources, "nested", "deep")
        os.makedirs(nested_dir)
        with open(os.path.join(nested_dir, "child.txt"), "w") as fh:
            fh.write("nested source content\n")

    mapping = [
        ("/aws_dda/resources_for_copy", dest_resources),
        ("/aws_dda/dda_triton", dest_dda_triton),
        ("/aws_dda", dest_aws_dda),
        ("/dda_triton", src_root),
    ]
    return _make_translate(mapping), {
        "src_root": src_root,
        "src_resources": src_resources,
        "dest_aws_dda": dest_aws_dda,
        "dest_dda_triton": dest_dda_triton,
        "dest_resources": dest_resources,
    }


# ---------------------------------------------------------------------------
# Preservation 3.1 -- first-time copytree delivers the whole source tree.
#
# Property-based: vary the source resource layout (extra files, nested subtree)
# for the non-buggy first-time path and assert the destination tree equals the
# source tree exactly (the recorded copytree baseline).
# ---------------------------------------------------------------------------

_EXTRA_RESOURCE_FILES = st.lists(
    st.sampled_from(
        [
            "new_runtime_helper.py",
            "extra_config.json",
            "labels.txt",
            "postprocess.py",
            "notes.md",
            "future_resource.py",
        ]
    ),
    unique=True,
    max_size=6,
)


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(extra_files=_EXTRA_RESOURCE_FILES, nested_subtree=st.booleans())
def test_firsttime_copytree_delivers_full_tree(extra_files, nested_subtree):
    """Preservation (Requirement 3.1): on the first-time path (destination
    resources dir absent) cp_model_conversion_files() delivers the ENTIRE source
    resources_for_copy tree via copytree. For any source layout the resulting
    destination tree equals the source tree byte-for-byte.

    Recorded baseline (unfixed code): destination == source after copytree. This
    passes on unfixed code and must continue to pass after the fix.

    Validates: Requirements 3.1
    """
    base = tempfile.mkdtemp(prefix="cp_pres_firsttime_")
    try:
        translate, paths = _build_firsttime_harness(base, extra_files, nested_subtree)

        # Precondition: first-time path -- destination resources dir absent.
        assert not os.path.exists(paths["dest_resources"])

        _run_cp_with_harness(translate)

        # copytree ran and reproduced the whole source tree exactly.
        assert os.path.isdir(paths["dest_resources"])
        assert _read_tree(paths["dest_resources"]) == _read_tree(paths["src_resources"])

        # The shipped files (incl. inference_runtimes.py) are all present.
        for name in SOURCE_RESOURCE_FILES:
            assert os.path.exists(os.path.join(paths["dest_resources"], name))
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# Preservation 3.2 -- /dda_triton and /aws_dda auxiliary copies unchanged.
# ---------------------------------------------------------------------------

@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(extra_files=_EXTRA_RESOURCE_FILES, nested_subtree=st.booleans())
def test_auxiliary_copies_land_in_dda_triton_and_aws_dda(extra_files, nested_subtree):
    """Preservation (Requirement 3.2): files_to_copy_to_dda_triton land in
    /dda_triton and files_to_copy_to_aws_dda land in /aws_dda, with content
    preserved, regardless of the resources layout.

    Recorded baseline (unfixed code): the three dda_triton aux files and the three
    aws_dda aux files are each present in their destination with matching content.
    This is the invariant that must be identical before and after the fix.

    Validates: Requirements 3.2
    """
    base = tempfile.mkdtemp(prefix="cp_pres_aux_")
    try:
        translate, paths = _build_firsttime_harness(base, extra_files, nested_subtree)

        _run_cp_with_harness(translate)

        # dda_triton aux files present with source content.
        for name in SRC_AUX_DDA_TRITON:
            dst = os.path.join(paths["dest_dda_triton"], name)
            assert os.path.exists(dst), f"{name} missing from /dda_triton dest"
            with open(dst) as fh:
                assert fh.read() == f"# source {name}\n"

        # aws_dda aux files present with source content.
        for name in SRC_AUX_AWS_DDA:
            dst = os.path.join(paths["dest_aws_dda"], name)
            assert os.path.exists(dst), f"{name} missing from /aws_dda dest"
            with open(dst) as fh:
                assert fh.read() == f"# source {name}\n"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# Downstream staging harness (mirrors test_triton_inference_runtimes_bug.py).
#
# model_convertor._create_base_model_structure() resolves resources_for_copy
# relative to os.path.realpath(__file__). We patch realpath so it points at a
# temp "working dir" whose resources_for_copy we control, then run the real
# staging.
# ---------------------------------------------------------------------------

def _run_staging(base, include_inference_runtimes):
    """Run _create_base_model_structure() against a temp working dir whose
    resources_for_copy contains lfv_model_template.py and optionally
    inference_runtimes.py. Returns (ret, version_dir, warning_records)."""
    import dda_triton.model_convertor as mc

    stage_dir = os.path.join(base, "aws_dda")
    stage_resources = os.path.join(stage_dir, "resources_for_copy")
    os.makedirs(stage_resources)
    with open(os.path.join(stage_resources, "lfv_model_template.py"), "w") as fh:
        fh.write("# template\n")
    if include_inference_runtimes:
        with open(os.path.join(stage_resources, "inference_runtimes.py"), "w") as fh:
            fh.write("# runtime module\n")

    model_repo_dir = os.path.join(base, "model_repo")
    os.makedirs(model_repo_dir)

    deployed_model_path = os.path.join(base, "deployed")
    os.makedirs(deployed_model_path)
    with open(os.path.join(deployed_model_path, "weights.bin"), "w") as fh:
        fh.write("w")

    orig_realpath = os.path.realpath

    def fake_realpath(p, *a, **k):
        if str(p).endswith("model_convertor.py"):
            return os.path.join(stage_dir, "model_convertor.py")
        return orig_realpath(p, *a, **k)

    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _ListHandler(level=logging.WARNING)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        with patch.object(mc.os.path, "realpath", side_effect=fake_realpath):
            ret = mc._create_base_model_structure(
                model_repo_dir=model_repo_dir,
                deployed_model_path=deployed_model_path,
                model_name="m",
                model_version="1",
                manifest={},
            )
    finally:
        root_logger.removeHandler(handler)

    version_dir = os.path.join(model_repo_dir, "base_m", "1")
    return ret, version_dir, records


def test_dlr_only_staging_proceeds_with_warning_no_error():
    """Preservation (Requirement 3.3): on a DLR-only device where
    inference_runtimes.py is legitimately absent from resources_for_copy, staging
    SHALL proceed without error -- model.py is staged, inference_runtimes.py is
    NOT staged, only a WARNING is logged, and the function returns True.

    Recorded baseline (unfixed code): ret is True, model.py present,
    inference_runtimes.py absent, exactly a warning (no error). This path is
    untouched by the fix and must remain identical.

    Validates: Requirements 3.3
    """
    base = tempfile.mkdtemp(prefix="pres_stage_dlr_")
    try:
        ret, version_dir, records = _run_staging(base, include_inference_runtimes=False)

        # Proceeds without error.
        assert ret is True
        # model.py was staged (the staging block executed).
        assert os.path.exists(os.path.join(version_dir, "model.py"))
        # inference_runtimes.py is NOT staged (legitimately absent on DLR-only).
        assert not os.path.exists(os.path.join(version_dir, "inference_runtimes.py"))
        # Exactly a warning was logged -- no error records.
        warned = [
            r
            for r in records
            if r.levelno == logging.WARNING
            and "inference_runtimes.py not found" in r.getMessage()
        ]
        assert warned, "expected a warning about the missing inference_runtimes.py"
        errored = [r for r in records if r.levelno >= logging.ERROR]
        assert not errored, f"unexpected error log(s): {[r.getMessage() for r in errored]}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_healthy_device_staging_stages_runtime_and_succeeds():
    """Preservation (Requirement 3.4): a device that already has
    inference_runtimes.py present in resources_for_copy SHALL continue to stage it
    next to model.py and succeed (returns True) -- the already-healthy device path
    is unchanged.

    Recorded baseline (unfixed code): ret is True, model.py present,
    inference_runtimes.py staged, no warning about a missing file.

    Validates: Requirements 3.4
    """
    base = tempfile.mkdtemp(prefix="pres_stage_healthy_")
    try:
        ret, version_dir, records = _run_staging(base, include_inference_runtimes=True)

        assert ret is True
        assert os.path.exists(os.path.join(version_dir, "model.py"))
        assert os.path.exists(os.path.join(version_dir, "inference_runtimes.py"))
        missing_warn = [
            r
            for r in records
            if r.levelno == logging.WARNING
            and "inference_runtimes.py not found" in r.getMessage()
        ]
        assert not missing_warn, "should not warn about a missing file when present"
    finally:
        shutil.rmtree(base, ignore_errors=True)
