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
"""F5b: unchanged entries are left alone; changed ones are swapped together
under one exclusive hold (triton-model-cleanup-wedge D9, task 3.5).

Runs under Python 3.8 as well.

_Requirements: 2.8, 3.2; Property 9_
"""
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import dda_triton.model_convertor as mc
from dda_triton import model_locks

from triton_cleanup_wedge.fakes import (
    BACKEND_SRC,
    FakeTime,
    LockHolder,
    hidden_entries,
    lock_is_free,
    published_dirs,
    write_fixture_model,
)

M = "model-publish-jetson-xavier-jp5"
RESOURCES = os.path.join(BACKEND_SRC, "dda_triton", "resources_for_copy")


def _write_inputs(deployed, inputs):
    os.makedirs(deployed, exist_ok=True)
    manifest = {"runtime": inputs["runtime"],
                "dataset": {"image_height": inputs["size"], "image_width": inputs["size"]}}
    if inputs["pixel"]:
        manifest["model_graph"] = {"pixel_level_classes": {"names": ["defect"]}}
    with open(os.path.join(deployed, "manifest.json"), "w") as handle:
        json.dump(manifest, handle)
    for name in os.listdir(deployed):
        if name.startswith("artifact"):
            os.unlink(os.path.join(deployed, name))
    for name in inputs["artifacts"]:
        with open(os.path.join(deployed, name), "w") as handle:
            handle.write("payload")


def _stats(repo):
    out = {}
    for path in published_dirs(repo, M):
        st_ = os.stat(path)
        out[os.path.basename(path)] = (st_.st_ino, st_.st_mtime_ns)
    return out


def _layout(repo):
    return {os.path.basename(p): mc._tree_manifest(p) for p in published_dirs(repo, M)}


def _expected_layout(deployed, inputs, version):
    """Today's layout, written out: the three config.pbtxt texts from the
    convertor's own config builders, the copied resources, and a symlink
    per deployed artifact."""
    manifest = json.load(open(os.path.join(deployed, "manifest.json")))
    shape = mc._resolve_base_input_shape(manifest)

    def sha(text):
        import hashlib
        return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()

    def file_sha(path):
        return mc._file_sha256(path)

    base = {"config.pbtxt": ("file", sha(mc._create_base_model_config_pbtxt(M, shape))),
            version: ("dir",),
            os.path.join(version, "model.py"): (
                "file", file_sha(os.path.join(RESOURCES, "lfv_model_template.py"))),
            os.path.join(version, "inference_runtimes.py"): (
                "file", file_sha(os.path.join(RESOURCES, "inference_runtimes.py")))}
    for name in os.listdir(deployed):
        base[os.path.join(version, name)] = ("link", os.path.join(deployed, name))
    marshal = {"config.pbtxt": ("file", sha(mc._create_marshal_model_config_pbtxt(M, shape))),
               version: ("dir",),
               os.path.join(version, "model.py"): (
                   "file", file_sha(os.path.join(RESOURCES, "marshal_for_capture_template.py")))}
    ensemble = {"config.pbtxt": ("file", sha(mc._create_ensemble_model_config_pbtxt(M, shape))),
                version: ("dir",),
                os.path.join(version, "ensemble_model"): (
                    "file", file_sha(os.path.join(RESOURCES, "ensemble_model")))}
    return {"base_" + M: base, "marshal_" + M: marshal, M: ensemble}


inputs_strategy = st.fixed_dictionaries({
    "runtime": st.sampled_from(["dlr", "onnx"]),
    "size": st.sampled_from([224, 512]),
    "pixel": st.booleans(),
    "artifacts": st.lists(st.sampled_from(["artifact.so", "artifact.params", "artifact.json"]),
                          unique=True, min_size=1, max_size=3),
    "version": st.sampled_from(["1", "2"]),
})


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(first=inputs_strategy, second=inputs_strategy, runtime_extras=st.booleans())
def test_property_9_content_aware_publish(first, second, runtime_extras):
    root = tempfile.mkdtemp(prefix="publish-")
    try:
        repo = os.path.join(root, "dda_triton", "triton_model_repo")
        os.makedirs(repo)
        deployed = os.path.join(root, "deployed")
        _write_inputs(deployed, first)
        assert mc.convert_to_triton_structure(repo, deployed, M, first["version"]) is True
        assert _layout(repo) == _expected_layout(deployed, first, first["version"])
        if runtime_extras:
            version_dir = os.path.join(repo, "base_" + M, first["version"])
            os.makedirs(os.path.join(version_dir, "__pycache__"))
            open(os.path.join(version_dir, "__pycache__", "model.cpython-38.pyc"), "w").close()
            open(os.path.join(version_dir, "dda_active_providers.json"), "w").close()
            open(os.path.join(version_dir, ".dda_active_providers.1234.tmp"), "w").close()
        before = _stats(repo)
        before_layout = {k: mc._tree_manifest(os.path.join(repo, k), ignore_runtime=True)
                         for k in before}
        _write_inputs(deployed, second)
        assert mc.convert_to_triton_structure(repo, deployed, M, second["version"]) is True
        after = _stats(repo)
        expected = _expected_layout(deployed, second, second["version"])
        assert {k: mc._tree_manifest(os.path.join(repo, k), ignore_runtime=True)
                for k in after} == expected
        for name in after:
            unchanged = before_layout[name] == expected[name]
            assert (after[name] == before[name]) is unchanged, name
        assert hidden_entries(repo) == []
    finally:
        shutil.rmtree(root)


def test_unchanged_directories_log_and_changed_ones_are_published(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    repo = str(tmp_path / "repo")
    deployed = write_fixture_model(str(tmp_path / "deployed"))
    assert mc.convert_to_triton_structure(repo, deployed, M, "1")
    caplog.clear()
    assert mc.convert_to_triton_structure(repo, deployed, M, "1")
    lines = [r.getMessage() for r in caplog.records]
    for name in ("base_" + M, "marshal_" + M, M):
        assert ("Model {0} unchanged; leaving the published repository entry "
                "untouched").format(name) in lines
    caplog.clear()
    assert mc.convert_to_triton_structure(repo, deployed, M, "2")
    lines = [r.getMessage() for r in caplog.records]
    for name in ("base_" + M, "marshal_" + M, M):
        assert "Model {0} changed; publishing the new entry".format(name) in lines


def test_a_failed_staging_step_changes_nothing_and_leaves_no_staging(tmp_path, monkeypatch,
                                                                     caplog):
    caplog.set_level(logging.INFO)
    repo = str(tmp_path / "repo")
    deployed = write_fixture_model(str(tmp_path / "deployed"))
    assert mc.convert_to_triton_structure(repo, deployed, M, "1")
    before = _stats(repo)
    real_copy = shutil.copy

    def failing_copy(src, dst, *a, **k):
        if os.path.basename(src) == "ensemble_model":
            raise OSError("disk full")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(mc.shutil, "copy", failing_copy)
    assert mc.convert_to_triton_structure(repo, deployed, M, "2") is False
    assert _stats(repo) == before
    assert hidden_entries(repo) == []
    assert "Unable to create ensemble model structure" in [r.getMessage() for r in caplog.records]


def test_stale_staging_of_this_model_is_swept_first(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    stale = os.path.join(repo, ".staging-base_{0}-ffff0000-1-abcdefgh".format(M))
    other = os.path.join(repo, ".staging-base_{0}-test-ffff0000-1-abcdefgh".format(M))
    os.makedirs(stale)
    os.makedirs(other)
    deployed = write_fixture_model(str(tmp_path / "deployed"))
    assert mc.convert_to_triton_structure(repo, deployed, M, "1")
    assert not os.path.exists(stale) and os.path.isdir(other)
    assert "Removed stale staging directory {0}".format(os.path.basename(stale)) in [
        r.getMessage() for r in caplog.records]


def test_every_swap_runs_under_one_exclusive_hold(tmp_path, monkeypatch):
    repo = str(tmp_path / "repo")
    deployed = write_fixture_model(str(tmp_path / "deployed"))
    lock_path = model_locks.lock_path(M, model_locks.lock_dir_for(repo))
    acquired = []
    real_acquire = model_locks.acquire
    monkeypatch.setattr(mc.model_locks, "acquire",
                        lambda *a, **k: (acquired.append((a, k)), real_acquire(*a, **k))[1])
    held = []
    real_swap = mc._swap_model_dir

    def swap(repo_dir, name, staging):
        held.append((name, lock_is_free(lock_path, exclusive=False)))
        return real_swap(repo_dir, name, staging)

    monkeypatch.setattr(mc, "_swap_model_dir", swap)
    assert mc.convert_to_triton_structure(repo, deployed, M, "1")
    assert [n for n, _free in held] == ["base_" + M, "marshal_" + M, M]
    assert all(free is False for _n, free in held)
    assert len(acquired) == 1
    assert acquired[0][1]["exclusive"] is True and acquired[0][1]["timeout_s"] == 300
    assert acquired[0][1]["lock_dir"] == model_locks.lock_dir_for(repo)
    assert lock_is_free(lock_path)


def test_a_held_shared_lock_waits_about_300_s_then_swaps_anyway(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    repo = str(tmp_path / "repo")
    deployed = write_fixture_model(str(tmp_path / "deployed"))
    lock_dir = model_locks.lock_dir_for(repo)
    os.makedirs(lock_dir)
    fake = FakeTime().install(monkeypatch)
    with LockHolder(model_locks.lock_path(M, lock_dir), exclusive=False):
        started = fake.now
        assert mc.convert_to_triton_structure(repo, deployed, M, "1") is True
        waited = fake.now - started
    assert waited == pytest.approx(300.0)
    lines = [(r.levelname, r.getMessage()) for r in caplog.records]
    timeouts = [l for l in lines if "still in flight after 300s" in l[1]]
    assert timeouts == [("WARNING", "Publishing {0}: a load of the model is still in flight "
                                    "after 300s; replacing the changed entries anyway so the "
                                    "new artifacts land".format(M))]
    progress = [l for l in lines if "waiting for an in-flight load" in l[1]]
    assert progress[0] == ("INFO", "Publishing {0}: waiting for an in-flight load of the model "
                                   "to finish (30s of 300s)".format(M))
    for path in published_dirs(repo, M):
        assert os.path.isdir(path)


def test_an_unusable_lock_publishes_as_today(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    repo = str(tmp_path / "repo")
    deployed = write_fixture_model(str(tmp_path / "deployed"))
    os.makedirs(model_locks.lock_path(M, model_locks.lock_dir_for(repo)))
    assert mc.convert_to_triton_structure(repo, deployed, M, "1") is True
    for path in published_dirs(repo, M):
        assert os.path.isdir(path)
    assert any(r.levelname == "WARNING" and "the model lock is unavailable" in r.getMessage()
               for r in caplog.records)


def test_a_manifest_error_counts_as_changed(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    repo = str(tmp_path / "repo")
    deployed = write_fixture_model(str(tmp_path / "deployed"))
    assert mc.convert_to_triton_structure(repo, deployed, M, "1")
    before = _stats(repo)

    def boom(path, ignore_runtime=False):
        raise OSError("unreadable")

    monkeypatch.setattr(mc, "_tree_manifest", boom)
    assert mc.convert_to_triton_structure(repo, deployed, M, "1")
    after = _stats(repo)
    assert all(after[k] != before[k] for k in before)
    assert any(r.levelname == "WARNING" and "could not compare" in r.getMessage()
               for r in caplog.records)


def test_the_single_directory_wrapper_still_publishes(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    deployed = write_fixture_model(str(tmp_path / "deployed"))
    manifest = json.load(open(os.path.join(deployed, "manifest.json")))
    assert mc._create_base_model_structure(repo, deployed, M, "1", manifest) is True
    first = os.stat(os.path.join(repo, "base_" + M)).st_ino
    assert mc._create_base_model_structure(repo, deployed, M, "1", manifest) is True
    assert os.stat(os.path.join(repo, "base_" + M)).st_ino == first
    assert os.path.isfile(os.path.join(repo, "base_" + M, "1", "model.py"))


_SIGTERM_SCRIPT = textwrap.dedent('''\
    import os, sys, time, signal
    sys.path.insert(0, sys.argv[1])
    import dda_triton.model_convertor as mc
    signal.signal(signal.SIGTERM, mc._sigterm_exit)
    real_swap = mc._swap_model_dir
    def blocking_swap(repo, name, staging):
        sys.stdout.write("SWAPPING\\n")
        sys.stdout.flush()
        time.sleep(60)
        return real_swap(repo, name, staging)
    mc._swap_model_dir = blocking_swap
    mc.convert_to_triton_structure(sys.argv[2], sys.argv[3], sys.argv[4], "1")
    ''')


def test_a_sigterm_during_the_publish_exits_143_without_leftovers(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    deployed = write_fixture_model(str(tmp_path / "deployed"))
    proc = subprocess.Popen(
        [sys.executable, "-c", _SIGTERM_SCRIPT, BACKEND_SRC, repo, deployed, M],
        stdout=subprocess.PIPE, universal_newlines=True)
    try:
        line = proc.stdout.readline()
        while line and line.strip() != "SWAPPING":
            line = proc.stdout.readline()
        assert line.strip() == "SWAPPING"
        lock_path = model_locks.lock_path(M, model_locks.lock_dir_for(repo))
        assert not lock_is_free(lock_path)
        assert [n for n in os.listdir(repo) if n.startswith(".staging-")]
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 143
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not [n for n in os.listdir(repo) if n.startswith(".staging-")]
    assert lock_is_free(lock_path)
