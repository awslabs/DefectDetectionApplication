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
# Drift-proofing and downstream staging tests for the "inference_runtimes.py
# missing on the subsequent-setup path" bugfix (task 4).
#
# Spec: .kiro/specs/triton-inference-runtimes-missing-fix
#   Property 1 (Fix Checking, PBT): for ANY source resources_for_copy file set
#   (arbitrary new resource files alongside inference_runtimes.py) and ANY
#   pre-existing destination state, the fixed subsequent-setup re-sync in
#   cp_model_conversion_files() SHALL deliver EVERY source file to
#   /aws_dda/resources_for_copy. This proves the drift class of bug (a
#   hand-maintained allowlist silently omitting newly added resource files)
#   cannot recur.
#
#   Downstream staging (Requirement 2.2): with inference_runtimes.py now
#   present in /aws_dda/resources_for_copy after the fixed re-sync, the
#   model_convertor.py staging path SHALL copy it next to the staged
#   lfv_model_template.py (model.py) into the model version directory.
#
# These tests run against the FIXED code and are EXPECTED TO PASS.
#
# Validates: Requirements 2.1, 2.2, 3.2

import logging
import os
import shutil
import tempfile
from unittest.mock import patch

from hypothesis import given, settings, HealthCheck, strategies as st

import dda_triton.triton_setup as ts
from dda_triton.triton_setup import cp_model_conversion_files


# ---------------------------------------------------------------------------
# Path-translation harness (mirrors test_triton_inference_runtimes_bug.py and
# test_triton_inference_runtimes_preservation.py).
#
# cp_model_conversion_files() hard-codes the source ("/dda_triton/") and the
# destinations ("/aws_dda", "/aws_dda/dda_triton/", "/aws_dda/resources_for_copy").
# We patch the shutil / os primitives it uses and translate those hard-coded
# absolute prefixes onto temp directories, then let the REAL copy run so we can
# assert on the real resulting destination contents.
# ---------------------------------------------------------------------------

# Files the source resources_for_copy always ships. ensemble_model is a plain
# FILE in the shipped tree (not a directory).
SOURCE_RESOURCE_FILES = [
    "inference_runtimes.py",
    "ensemble_model",
    "lfv_model_template.py",
    "marshal_for_capture_template.py",
]

# The other resources the old allowlist re-copied; the fixed re-sync must keep
# delivering these (Requirement 3.2).
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


def _build_subsequent_harness(
    base, extra_source_files, source_nested_subtree, stale_files, stale_nested_subtree
):
    """Lay out source + destination for the SUBSEQUENT-SETUP path with a VARIED
    source resource file set and a VARIED pre-existing destination state.

    * source (/dda_triton/) gets the aux files and a resources_for_copy that
      includes the shipped files (incl. inference_runtimes.py) PLUS arbitrary
      new resource files and an optional nested subtree (the drift scenario:
      files added to the source after the device was provisioned).
    * the destination /aws_dda/resources_for_copy is PRE-CREATED (forcing the
      else / subsequent-setup branch) with arbitrary stale contents -- stale
      copies of shipped files with DIFFERENT content, unrelated leftovers, and
      an optional stale nested subtree. It never contains inference_runtimes.py.
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

    # Source resources: shipped files plus arbitrary new resource files.
    for name in SOURCE_RESOURCE_FILES:
        with open(os.path.join(src_resources, name), "w") as fh:
            fh.write(f"# source resource {name}\n")
    for name in extra_source_files:
        with open(os.path.join(src_resources, name), "w") as fh:
            fh.write(f"# source resource {name}\n")
    if source_nested_subtree:
        nested_dir = os.path.join(src_resources, "runtime_helpers", "deep")
        os.makedirs(nested_dir)
        with open(os.path.join(nested_dir, "helper.py"), "w") as fh:
            fh.write("# source resource runtime_helpers/deep/helper.py\n")

    # Pre-existing (stale) destination resources -- never inference_runtimes.py.
    for name in stale_files:
        with open(os.path.join(dest_resources, name), "w") as fh:
            fh.write(f"# STALE {name}\n")
    if stale_nested_subtree:
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
    return _make_translate(mapping), {
        "src_root": src_root,
        "src_resources": src_resources,
        "dest_aws_dda": dest_aws_dda,
        "dest_dda_triton": dest_dda_triton,
        "dest_resources": dest_resources,
    }


# ---------------------------------------------------------------------------
# Property 1: Fix Checking (PBT) -- drift-proofing.
#
# Vary the SOURCE resource file set (arbitrary new resource files alongside
# inference_runtimes.py, optional nested subtree) AND the pre-existing
# destination state (stale shipped-file copies, unrelated leftovers, optional
# stale subtree). The fixed re-sync must deliver EVERY source file to the
# destination with source content, so the drift class of bug cannot recur.
# ---------------------------------------------------------------------------

# Arbitrary "future" resource files that could be added to the source tree
# after a device was provisioned -- exactly the drift scenario that produced
# this bug when inference_runtimes.py was added.
_EXTRA_SOURCE_FILES = st.lists(
    st.sampled_from(
        [
            "future_runtime.py",
            "new_postprocess.py",
            "extra_config.json",
            "labels.txt",
            "calibration.bin",
            "notes.md",
        ]
    ),
    unique=True,
    max_size=6,
)

# Incidental pre-existing destination contents, none of which is
# inference_runtimes.py. Stale copies of shipped files get DIFFERENT content in
# the harness, so the re-sync must overwrite them.
_STALE_DEST_FILES = st.lists(
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
@given(
    extra_source_files=_EXTRA_SOURCE_FILES,
    source_nested_subtree=st.booleans(),
    stale_files=_STALE_DEST_FILES,
    stale_nested_subtree=st.booleans(),
)
def test_resync_delivers_every_source_file(
    extra_source_files, source_nested_subtree, stale_files, stale_nested_subtree
):
    """Property 1 (Fix Checking): for ANY source resources_for_copy file set and
    ANY pre-existing destination state on the subsequent-setup path, the fixed
    cp_model_conversion_files() SHALL deliver EVERY source file (by relative
    path, with source content) to /aws_dda/resources_for_copy.

    In particular inference_runtimes.py is always delivered (Requirement 2.1)
    and the previously allowlisted resources are still delivered (Requirement
    3.2). Because the assertion quantifies over the WHOLE source tree --
    including arbitrary new resource files the old allowlist knew nothing
    about -- it proves the allowlist-drift class of bug cannot recur.

    Validates: Requirements 2.1, 3.2
    """
    base = tempfile.mkdtemp(prefix="cp_fix_drift_")
    try:
        translate, paths = _build_subsequent_harness(
            base,
            extra_source_files,
            source_nested_subtree,
            stale_files,
            stale_nested_subtree,
        )

        # Precondition: subsequent-setup path -- destination exists and omits
        # inference_runtimes.py (the bug condition holds).
        assert os.path.exists(paths["dest_resources"])
        assert not os.path.exists(
            os.path.join(paths["dest_resources"], "inference_runtimes.py")
        )

        _run_cp_with_harness(translate)

        src_tree = _read_tree(paths["src_resources"])
        dest_tree = _read_tree(paths["dest_resources"])

        # EVERY source file (shipped, extra, nested) is delivered with source
        # content -- stale destination copies are overwritten.
        for rel, content in src_tree.items():
            assert rel in dest_tree, (
                f"source resource '{rel}' was not delivered to the destination "
                "resources_for_copy by the subsequent-setup re-sync"
            )
            assert dest_tree[rel] == content, (
                f"destination copy of '{rel}' does not match the source content "
                "after the re-sync (stale content not overwritten)"
            )

        # Requirement 2.1: inference_runtimes.py in particular is present.
        assert "inference_runtimes.py" in dest_tree

        # Requirement 3.2: the previously allowlisted resources are still there.
        for name in ALLOWLIST_RESOURCE_FILES:
            assert name in dest_tree
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# Downstream staging verification (Requirement 2.2).
#
# End-to-end through the fixed pipeline: cp_model_conversion_files() re-syncs
# the subsequent-setup destination (delivering inference_runtimes.py), then the
# model_convertor.py staging path stages it from that destination next to the
# staged lfv_model_template.py (model.py) into the model version directory.
# ---------------------------------------------------------------------------
def test_resynced_inference_runtimes_is_staged_into_model_version_dir():
    """Requirement 2.2: with inference_runtimes.py present in
    /aws_dda/resources_for_copy (delivered by the fixed subsequent-setup
    re-sync), model_convertor._create_base_model_structure() SHALL copy it next
    to the staged lfv_model_template.py (model.py) in the model version
    directory, without warning about a missing file.

    Validates: Requirements 2.1, 2.2
    """
    import dda_triton.model_convertor as mc

    base = tempfile.mkdtemp(prefix="cp_fix_stage_")
    try:
        # Step 1: subsequent-setup re-sync -- destination pre-exists WITHOUT
        # inference_runtimes.py; the fixed function delivers it.
        translate, paths = _build_subsequent_harness(
            base,
            extra_source_files=[],
            source_nested_subtree=False,
            stale_files=list(ALLOWLIST_RESOURCE_FILES),
            stale_nested_subtree=False,
        )
        assert not os.path.exists(
            os.path.join(paths["dest_resources"], "inference_runtimes.py")
        )
        _run_cp_with_harness(translate)
        assert os.path.exists(
            os.path.join(paths["dest_resources"], "inference_runtimes.py")
        )

        # Step 2: run the REAL staging path against the re-synced destination.
        # model_convertor resolves resources_for_copy relative to
        # os.path.realpath(__file__); point it at the harness /aws_dda so it
        # stages from the destination the re-sync just populated.
        model_repo_dir = os.path.join(base, "model_repo")
        os.makedirs(model_repo_dir)
        deployed_model_path = os.path.join(base, "deployed")
        os.makedirs(deployed_model_path)
        with open(os.path.join(deployed_model_path, "weights.bin"), "w") as fh:
            fh.write("w")

        orig_realpath = os.path.realpath

        def fake_realpath(p, *a, **k):
            if str(p).endswith("model_convertor.py"):
                return os.path.join(paths["dest_aws_dda"], "model_convertor.py")
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

        assert ret is True
        version_dir = os.path.join(model_repo_dir, "base_m", "1")

        # The template was staged as model.py ...
        assert os.path.exists(os.path.join(version_dir, "model.py"))
        # ... and inference_runtimes.py was staged NEXT TO it (Requirement 2.2),
        staged_runtime = os.path.join(version_dir, "inference_runtimes.py")
        assert os.path.exists(staged_runtime), (
            "inference_runtimes.py was not staged into the model version dir "
            "even though the re-sync delivered it to resources_for_copy"
        )
        # ... with the source content delivered end-to-end through the pipeline.
        with open(staged_runtime) as fh:
            assert fh.read() == "# source resource inference_runtimes.py\n"

        # No warning about a missing inference_runtimes.py.
        missing_warn = [
            r
            for r in records
            if r.levelno == logging.WARNING
            and "inference_runtimes.py not found" in r.getMessage()
        ]
        assert not missing_warn, "staging warned about a missing file that is present"
    finally:
        shutil.rmtree(base, ignore_errors=True)
