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
"""Bug-condition (exploration) test for the Triton model-repo assembly race.

Reproduces the field incident where Triton's Python backend failed with
"Python model file not found in .../<model>/<version>/model.py" because the
model version directory was published (config.pbtxt written) before model.py /
symlinks were in place.

Invariant under test: a directory named exactly ``<repo>/base_<model>`` must
NEVER be observable (i.e. exist with a config.pbtxt) while its
``<version>/model.py`` backend file is still missing. Any code path that lets
Triton see a half-built, real-named model dir violates this and can wedge the
load queue.

Against the pre-fix, write-in-place implementation this test FAILS (the window
exists). Against the atomic staging-and-swap fix it PASSES.
"""
import json
import os
import shutil

import pytest

import dda_triton.model_convertor as mc


def _write_fixture_model(deployed_dir):
    os.makedirs(deployed_dir, exist_ok=True)
    manifest = {"runtime": "dlr", "dataset": {"image_height": 224, "image_width": 224}}
    with open(os.path.join(deployed_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    # A stand-in compiled artifact that will be symlinked into the version dir.
    with open(os.path.join(deployed_dir, "compiled.so"), "w", encoding="utf-8") as f:
        f.write("stub")


def test_real_model_dir_never_visible_without_backend(tmp_path):
    """The published base model dir is never seen with config.pbtxt but no model.py."""
    repo = str(tmp_path / "triton_model_repo")
    os.makedirs(repo)
    deployed = str(tmp_path / "deployed" / "cookies")
    _write_fixture_model(deployed)

    model_name = "model-cookies"
    base_dir = os.path.join(repo, f"base_{model_name}")

    violations = []

    def _probe(context):
        """Record if the real base dir exists (config present) but model.py absent."""
        cfg = os.path.join(base_dir, "config.pbtxt")
        # Only a *real* published dir counts; staging dirs are hidden ('.staging-').
        if os.path.isdir(base_dir) and os.path.exists(cfg):
            # Any version subdir missing model.py while config is visible == race.
            for ver in os.listdir(base_dir):
                ver_path = os.path.join(base_dir, ver)
                if os.path.isdir(ver_path) and not os.path.exists(
                    os.path.join(ver_path, "model.py")
                ):
                    violations.append(f"{context}: {base_dir} visible without model.py")

    real_copy = shutil.copy
    real_symlink = os.symlink

    def copy_with_probe(src, dst, *a, **k):
        _probe(f"before copy {os.path.basename(str(dst))}")
        result = real_copy(src, dst, *a, **k)
        _probe(f"after copy {os.path.basename(str(dst))}")
        return result

    def symlink_with_probe(src, dst, *a, **k):
        _probe("before symlink")
        result = real_symlink(src, dst, *a, **k)
        _probe("after symlink")
        return result

    with pytest.MonkeyPatch.context() as m:
        m.setattr(mc.shutil, "copy", copy_with_probe)
        m.setattr(mc.os, "symlink", symlink_with_probe)
        ok = mc.convert_to_triton_structure(
            model_repo_dir=repo,
            deployed_model_path=deployed,
            model_name=model_name,
            model_version="10",
        )

    assert ok is True, "conversion should succeed"
    # Final published state must be complete.
    assert os.path.exists(os.path.join(base_dir, "config.pbtxt"))
    assert os.path.exists(os.path.join(base_dir, "10", "model.py"))
    # The core invariant: never visible-but-incomplete during assembly.
    assert not violations, "race window detected: " + "; ".join(violations)
