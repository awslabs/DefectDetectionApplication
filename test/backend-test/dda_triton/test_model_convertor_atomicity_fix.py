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
"""Fix-verification tests for atomic Triton model-repo assembly.

Confirms the staging-and-swap fix: the published model directories
(``base_<model>``, ``marshal_<model>``, ``<model>``) are only ever observable in
a complete state, and a mid-build failure leaves no partial or staging dir
behind.
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
    with open(os.path.join(deployed_dir, "compiled.so"), "w", encoding="utf-8") as f:
        f.write("stub")


def _published_names(repo, model_name):
    return {
        f"base_{model_name}": ("model.py", os.path.join(repo, f"base_{model_name}")),
        f"marshal_{model_name}": ("model.py", os.path.join(repo, f"marshal_{model_name}")),
        model_name: ("ensemble_model", os.path.join(repo, model_name)),
    }


def test_published_dirs_never_incomplete_during_build(tmp_path):
    repo = str(tmp_path / "triton_model_repo")
    os.makedirs(repo)
    deployed = str(tmp_path / "deployed" / "cookies")
    _write_fixture_model(deployed)
    model_name = "model-cookies"
    version = "10"
    names = _published_names(repo, model_name)

    violations = []

    def _probe():
        for _, (backend_file, path) in names.items():
            cfg = os.path.join(path, "config.pbtxt")
            if os.path.isdir(path) and os.path.exists(cfg):
                # Published (config visible) => the version backend file must exist.
                ver_backend = os.path.join(path, version, backend_file)
                if not os.path.exists(ver_backend):
                    violations.append(f"{path} published without {backend_file}")

    real_copy = shutil.copy
    real_symlink = os.symlink

    def copy_probe(src, dst, *a, **k):
        _probe()
        r = real_copy(src, dst, *a, **k)
        _probe()
        return r

    def symlink_probe(src, dst, *a, **k):
        _probe()
        r = real_symlink(src, dst, *a, **k)
        _probe()
        return r

    with pytest.MonkeyPatch.context() as m:
        m.setattr(mc.shutil, "copy", copy_probe)
        m.setattr(mc.os, "symlink", symlink_probe)
        ok = mc.convert_to_triton_structure(repo, deployed, model_name, version)

    assert ok is True
    assert not violations, "; ".join(violations)
    # Final published layout is complete for all three model dirs.
    assert os.path.exists(os.path.join(repo, f"base_{model_name}", version, "model.py"))
    assert os.path.exists(os.path.join(repo, f"base_{model_name}", "config.pbtxt"))
    assert os.path.exists(os.path.join(repo, f"marshal_{model_name}", version, "model.py"))
    assert os.path.exists(os.path.join(repo, model_name, version, "ensemble_model"))
    # The base artifact symlink survived the atomic swap (absolute target).
    assert os.path.exists(os.path.join(repo, f"base_{model_name}", version, "compiled.so"))
    # No staging dirs leaked.
    assert not [d for d in os.listdir(repo) if d.startswith(".staging-")]


def test_failed_build_leaves_no_partial_or_staging_dir(tmp_path):
    repo = str(tmp_path / "triton_model_repo")
    os.makedirs(repo)
    deployed = str(tmp_path / "deployed" / "cookies")
    _write_fixture_model(deployed)
    model_name = "model-cookies"

    real_symlink = os.symlink

    def boom_symlink(src, dst, *a, **k):
        raise OSError("simulated symlink failure")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(mc.os, "symlink", boom_symlink)
        ok = mc.convert_to_triton_structure(repo, deployed, model_name, "10")

    assert ok is False
    # No real published base dir, and crucially no leaked staging dir.
    assert not os.path.exists(os.path.join(repo, f"base_{model_name}"))
    assert not [d for d in os.listdir(repo) if d.startswith(".staging-")]


def test_swap_preserves_prior_dir_on_failure(tmp_path):
    """A failed re-convert must not destroy the previously-good published dir."""
    repo = str(tmp_path / "triton_model_repo")
    os.makedirs(repo)
    deployed = str(tmp_path / "deployed" / "cookies")
    _write_fixture_model(deployed)
    model_name = "model-cookies"

    # First conversion succeeds.
    assert mc.convert_to_triton_structure(repo, deployed, model_name, "10") is True
    base_dir = os.path.join(repo, f"base_{model_name}")
    assert os.path.exists(os.path.join(base_dir, "10", "model.py"))

    # Second conversion fails mid-build; prior good dir must remain intact.
    def boom_symlink(src, dst, *a, **k):
        raise OSError("simulated symlink failure")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(mc.os, "symlink", boom_symlink)
        ok = mc.convert_to_triton_structure(repo, deployed, model_name, "11")

    assert ok is False
    assert os.path.exists(os.path.join(base_dir, "10", "model.py"))
    assert not [d for d in os.listdir(repo) if d.startswith(".staging-")]
