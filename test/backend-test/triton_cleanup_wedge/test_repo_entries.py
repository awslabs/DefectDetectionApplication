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
"""``dda_triton.repo_entries`` (triton-model-cleanup-wedge D4, task 3.1).

* ``inspect_entry(...).blocker`` equals the pre-change
  ``ModelGate._file_state`` for every generated repository layout. The old
  body is kept below, verbatim, as the oracle.
* The hidden-directory sweep ages stamped names by the boot clock only.

Runs under Python 3.8 as well.

_Requirements: 2.3, 3.9, 3.14; Property 4_
"""
import glob
import os
import re
import shutil
import tempfile
import time
from typing import List, Optional

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dda_triton import repo_entries

from triton_cleanup_wedge.fakes import write_entry

M = "model-entry-jetson-xavier-jp5"
NOW = time.time()

# ---------------------------------------------------------------- the oracle
# ModelGate._file_state and its helpers as they were before this change
# (src/backend/workflow_engine/model_gate.py at the base commit).
_STEP_MODEL_PATTERN = re.compile(r'model_name:\s*"([^"]+)"')
_PYTHON_BACKEND_PATTERN = re.compile(r'backend:\s*"python"')
_STAGING_PREFIX = ".staging-"
QUIET_PERIOD_S = 10.0


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _version_dirs(model_dir: str) -> List[str]:
    try:
        entries = sorted(os.listdir(model_dir))
    except OSError:
        return []
    return [entry for entry in entries
            if entry.isdigit() and os.path.isdir(os.path.join(model_dir, entry))]


class _OracleGate:
    def __init__(self, repo, wall):
        self._repo = repo
        self._wall = wall

    def _file_state(self, triton_model: str) -> Optional[str]:
        model_dir = os.path.join(self._repo, triton_model)
        config = os.path.join(model_dir, "config.pbtxt")
        if not os.path.isfile(config):
            return "{0} is missing".format(config)
        paths: List[str] = [model_dir, config]
        text = _read(config)
        if text is None:
            return "{0} is unreadable".format(config)
        steps = [step for step in dict.fromkeys(_STEP_MODEL_PATTERN.findall(text))
                 if step != triton_model]
        for step in steps:
            step_dir = os.path.join(self._repo, step)
            step_config = os.path.join(step_dir, "config.pbtxt")
            if not os.path.isfile(step_config):
                return "{0} is missing".format(step_config)
            paths.extend((step_dir, step_config))
            step_text = _read(step_config)
            if step_text is None:
                return "{0} is unreadable".format(step_config)
            if not _PYTHON_BACKEND_PATTERN.search(step_text):
                continue
            versions = _version_dirs(step_dir)
            if not versions:
                return "{0} is missing".format(os.path.join(step_dir, "<version>", "model.py"))
            for version in versions:
                model_py = os.path.join(step_dir, version, "model.py")
                if not os.path.isfile(model_py):
                    return "{0} is missing".format(model_py)
                paths.append(model_py)
        for name in [triton_model] + steps:
            staging = sorted(glob.glob(os.path.join(self._repo, _STAGING_PREFIX + name + "-*")))
            if staging:
                return "{0} is being published".format(staging[0])
        newest_path, newest = None, None
        for path in paths:
            mtime = _mtime(path)
            if mtime is None:
                return "{0} is missing".format(path)
            if newest is None or mtime > newest:
                newest_path, newest = path, mtime
        if newest is not None:
            age = self._wall() - newest
            if age < QUIET_PERIOD_S:
                return "{0} changed {1:.1f} s ago".format(newest_path, max(0.0, age))
        return None


# ----------------------------------------------------------- generated layouts

AGES = st.sampled_from([0.5, 5.0, 9.9, 10.5, 3600.0])
STEP_NAMES = ["base_" + M, "marshal_" + M, "other-step", M]
step_strategy = st.fixed_dictionaries({
    "config": st.booleans(),
    "python": st.booleans(),
    "versions": st.lists(st.sampled_from(["1", "2", "x"]), unique=True, max_size=2),
    "model_py": st.booleans(),
    "age": AGES,
})
layout_strategy = st.fixed_dictionaries({
    "has_dir": st.booleans(),
    "has_config": st.booleans(),
    "config_age": AGES,
    "steps": st.lists(st.sampled_from(STEP_NAMES), unique=True, max_size=3),
    "step_specs": st.lists(step_strategy, min_size=4, max_size=4),
    "staging": st.lists(st.sampled_from([M] + STEP_NAMES + [M + "-test"]), unique=True,
                        max_size=2),
    "stamped": st.booleans(),
})


def _touch(path, age):
    stamp = NOW - age
    os.utime(path, (stamp, stamp))


def _build(repo, layout):
    model_dir = os.path.join(repo, M)
    if layout["has_dir"]:
        os.makedirs(model_dir)
        if layout["has_config"]:
            text = 'name: "{0}"\nplatform: "ensemble"\n'.format(M) + "".join(
                'step {{ model_name: "{0}" }}\n'.format(s) for s in layout["steps"])
            with open(os.path.join(model_dir, "config.pbtxt"), "w") as handle:
                handle.write(text)
            _touch(os.path.join(model_dir, "config.pbtxt"), layout["config_age"])
        _touch(model_dir, layout["config_age"])
    for step, spec in zip(STEP_NAMES, layout["step_specs"]):
        if step == M:
            continue
        step_dir = os.path.join(repo, step)
        os.makedirs(step_dir)
        if spec["config"]:
            body = 'name: "{0}"\n{1}'.format(
                step, 'backend: "python"\n' if spec["python"] else 'platform: "onnx"\n')
            with open(os.path.join(step_dir, "config.pbtxt"), "w") as handle:
                handle.write(body)
            _touch(os.path.join(step_dir, "config.pbtxt"), spec["age"])
        for version in spec["versions"]:
            os.makedirs(os.path.join(step_dir, version))
            if spec["model_py"]:
                path = os.path.join(step_dir, version, "model.py")
                with open(path, "w") as handle:
                    handle.write("# model\n")
                _touch(path, spec["age"])
        _touch(step_dir, spec["age"])
    for name in layout["staging"]:
        prefix = (repo_entries.staging_prefix(name) if layout["stamped"]
                  else ".staging-{0}-".format(name))
        tempfile.mkdtemp(prefix=prefix, dir=repo)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(layout=layout_strategy)
def test_blocker_equals_the_pre_change_file_state(layout):
    repo = tempfile.mkdtemp(prefix="repo-entries-")
    try:
        _build(repo, layout)
        wall = lambda: NOW  # noqa: E731
        expected = _OracleGate(repo, wall)._file_state(M)
        got = repo_entries.inspect_entry(repo, M, wall=wall, quiet_period_s=QUIET_PERIOD_S)
        assert got.blocker == expected
        assert got.present == os.path.isdir(os.path.join(repo, M))
        if expected is not None and expected.endswith(" is missing"):
            assert got.missing_path == expected[:-len(" is missing")]
        else:
            assert got.missing_path is None
    finally:
        shutil.rmtree(repo)


def test_a_complete_stable_entry_has_no_blocker_and_reports_its_paths(tmp_path):
    repo = str(tmp_path / "repo")
    write_entry(repo, M, age_s=60.0)
    got = repo_entries.inspect_entry(repo, M, wall=time.time, quiet_period_s=10.0)
    assert got.present is True
    assert got.blocker is None
    assert got.missing_path is None
    assert os.path.join(repo, "base_" + M, "1", "model.py") in got.paths
    assert got.newest_path in got.paths
    assert got.newest_mtime == pytest.approx(time.time() - 60.0, abs=5.0)


def test_quiet_period_zero_still_reports_ages(tmp_path):
    repo = str(tmp_path / "repo")
    write_entry(repo, M, age_s=1.0)
    assert repo_entries.inspect_entry(repo, M, quiet_period_s=10.0).blocker.endswith("s ago")
    got = repo_entries.inspect_entry(repo, M, quiet_period_s=0)
    assert got.blocker is None
    assert got.newest_mtime is not None


def test_an_absent_entry(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    got = repo_entries.inspect_entry(repo, M)
    assert got.present is False
    assert got.missing_path == os.path.join(repo, M, "config.pbtxt")


def test_a_missing_step_model_py_names_the_path(tmp_path):
    repo = str(tmp_path / "repo")
    write_entry(repo, M, skip=(os.path.join("base_" + M, "1", "model.py"),))
    got = repo_entries.inspect_entry(repo, M)
    path = os.path.join(repo, "base_" + M, "1", "model.py")
    assert got.present is True
    assert got.blocker == path + " is missing"
    assert got.missing_path == path


def test_a_staging_sibling_blocks_and_still_reports_ages(tmp_path):
    repo = str(tmp_path / "repo")
    write_entry(repo, M, age_s=60.0)
    staging = tempfile.mkdtemp(prefix=repo_entries.staging_prefix("marshal_" + M), dir=repo)
    got = repo_entries.inspect_entry(repo, M)
    assert got.blocker == staging + " is being published"
    assert got.newest_mtime is not None


# -------------------------------------------------------------- boot stamps

def test_boot_stamp_reads_the_boot_id_and_boottime(tmp_path):
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("3F2A9C1D-1111-2222-3333-4444ABCD5555\n")
    assert repo_entries.boot_stamp(str(boot_id), boottime=lambda: 1234.9) == "3f2a9c1d-1234"
    real = repo_entries.boot_stamp()
    assert real is None or re.match(r"^[0-9a-f]{8}-[0-9]+$", real)


@pytest.mark.parametrize("content", ["", "zzzzzzzz-1111", "12ab\n"])
def test_boot_stamp_is_none_for_a_malformed_boot_id(tmp_path, content):
    boot_id = tmp_path / "boot_id"
    boot_id.write_text(content)
    assert repo_entries.boot_stamp(str(boot_id)) is None
    assert repo_entries.boot_stamp(str(tmp_path / "absent")) is None


def test_names_without_a_stamp_are_todays():
    assert repo_entries.staging_prefix("base_" + M, stamp_reader=lambda: None) == \
        ".staging-base_{0}-".format(M)
    name = repo_entries.trash_name(M, stamp_reader=lambda: None, pid=42)
    assert re.match(r"^\.trash-{0}-42-[0-9]+$".format(re.escape(M)), name)


def test_stamped_names_round_trip_through_the_sweep_patterns_and_the_gate_glob(tmp_path):
    stamp = lambda: "0123abcd-500"  # noqa: E731
    for name in ("base_" + M, "marshal_" + M, M, "model-yolo"):
        path = tempfile.mkdtemp(prefix=repo_entries.staging_prefix(name, stamp_reader=stamp),
                                dir=str(tmp_path))
        base = os.path.basename(path)
        match = repo_entries._STAMPED_STAGING.match(base)
        assert match and match.group("name") == name
        assert match.group("boot") == "0123abcd" and match.group("bt") == "500"
        assert path in glob.glob(os.path.join(str(tmp_path), ".staging-" + name + "-*"))
        trash = repo_entries.trash_name(name, stamp_reader=stamp, pid=77)
        match = repo_entries._STAMPED_TRASH.match(trash)
        assert match and match.group("dir") == name and match.group("pid") == "77"


# ------------------------------------------------------------------ the sweep

def _stamped_staging(repo, name, boot, bt):
    return tempfile.mkdtemp(prefix=".staging-{0}-{1}-{2}-".format(name, boot, bt), dir=repo)


def _mkdir(repo, name, age=None):
    path = os.path.join(repo, name)
    os.makedirs(path)
    os.makedirs(os.path.join(path, "1"))
    if age is not None:
        _touch(path, age)
    return path


BOOT = "0123abcd"


def test_a_same_boot_staging_dir_is_aged_by_the_boot_clock(tmp_path):
    repo = str(tmp_path)
    old = _stamped_staging(repo, M, BOOT, 10000 - 1200)  # 20 min old
    young = _stamped_staging(repo, "base_" + M, BOOT, 10000 - 300)  # 5 min old
    removed = repo_entries.sweep_stale_hidden_dirs(
        repo, stamp_reader=lambda: BOOT + "-10000", wall=lambda: NOW)
    assert removed == [os.path.basename(old)]
    assert os.path.isdir(young) and not os.path.exists(old)


def test_a_wall_clock_jump_never_ages_a_same_boot_directory(tmp_path):
    repo = str(tmp_path)
    young = _stamped_staging(repo, M, BOOT, 10000 - 60)
    trash = _mkdir(repo, ".trash-{0}-{1}-{2}-99".format(M, BOOT, 10000 - 60))
    for jump in (3600.0, 86400.0 * 30):
        assert repo_entries.sweep_stale_hidden_dirs(
            repo, stamp_reader=lambda: BOOT + "-10000", wall=lambda: NOW + jump) == []
    assert os.path.isdir(young) and os.path.isdir(trash)


def test_a_stamped_directory_from_another_boot_is_removed_at_once(tmp_path):
    repo = str(tmp_path)
    other = _stamped_staging(repo, M, "ffff0000", 9999)
    trash = _mkdir(repo, ".trash-{0}-ffff0000-9999-12".format(M))
    removed = repo_entries.sweep_stale_hidden_dirs(repo, stamp_reader=lambda: BOOT + "-100")
    assert sorted(removed) == sorted([os.path.basename(other), os.path.basename(trash)])


def test_a_stamped_trash_directory_is_aged_by_its_stamp(tmp_path):
    repo = str(tmp_path)
    old = _mkdir(repo, ".trash-{0}-{1}-{2}-12".format(M, BOOT, 1000), age=0)
    young = _mkdir(repo, ".trash-base_{0}-{1}-{2}-12".format(M, BOOT, 1500), age=99999)
    removed = repo_entries.sweep_stale_hidden_dirs(repo, stamp_reader=lambda: BOOT + "-1700")
    assert removed == [os.path.basename(old)]
    assert os.path.isdir(young)


def test_unstamped_names_are_aged_by_their_mtime(tmp_path):
    repo = str(tmp_path)
    old = _mkdir(repo, ".staging-{0}-abcdefgh".format(M), age=700)
    young = _mkdir(repo, ".staging-base_{0}-abcdefgh".format(M), age=60)
    old_trash = _mkdir(repo, ".trash-{0}-42-1792164968505901608".format(M), age=700)
    removed = repo_entries.sweep_stale_hidden_dirs(
        repo, stamp_reader=lambda: BOOT + "-100", wall=lambda: NOW)
    assert sorted(removed) == sorted([os.path.basename(old), os.path.basename(old_trash)])
    assert os.path.isdir(young)


def test_without_a_boot_stamp_every_name_is_aged_by_its_mtime(tmp_path):
    repo = str(tmp_path)
    stamped_young = _stamped_staging(repo, M, "ffff0000", 1)
    _touch(stamped_young, 60)
    stamped_old = _stamped_staging(repo, "base_" + M, "ffff0000", 1)
    _touch(stamped_old, 700)
    removed = repo_entries.sweep_stale_hidden_dirs(repo, stamp_reader=lambda: None,
                                                   wall=lambda: NOW)
    assert removed == [os.path.basename(stamped_old)]


def test_names_restricts_the_sweep_to_those_staging_names(tmp_path):
    repo = str(tmp_path)
    mine = _stamped_staging(repo, "model-yolo", "ffff0000", 1)
    other = _stamped_staging(repo, "model-yolo-test", "ffff0000", 1)
    unstamped_other = _mkdir(repo, ".staging-model-yolo-test-abcdefgh", age=9999)
    unstamped_mine = _mkdir(repo, ".staging-model-yolo-abcdefgh", age=9999)
    trash = _mkdir(repo, ".trash-model-yolo-ffff0000-1-12")
    removed = repo_entries.sweep_stale_hidden_dirs(
        repo, names=["model-yolo"], stamp_reader=lambda: BOOT + "-100", wall=lambda: NOW)
    assert sorted(removed) == sorted([os.path.basename(mine), os.path.basename(unstamped_mine)])
    for kept in (other, unstamped_other, trash):
        assert os.path.isdir(kept)


def test_the_sweep_ignores_files_links_and_entries(tmp_path):
    repo = str(tmp_path / "repo")
    write_entry(repo, M)
    with open(os.path.join(repo, ".staging-x-ffff0000-1-abcdefgh"), "w") as handle:
        handle.write("file")
    os.symlink(repo, os.path.join(repo, ".trash-y-ffff0000-1-1"))
    assert repo_entries.sweep_stale_hidden_dirs(repo, stamp_reader=lambda: BOOT + "-1") == []
    assert os.path.isdir(os.path.join(repo, M))


def test_the_sweep_never_raises(tmp_path, monkeypatch):
    repo = str(tmp_path)
    stale = _stamped_staging(repo, M, "ffff0000", 1)

    def boom(path):
        raise OSError("busy")

    monkeypatch.setattr(repo_entries.shutil, "rmtree", boom)
    assert repo_entries.sweep_stale_hidden_dirs(repo, stamp_reader=lambda: BOOT + "-1") == []
    assert os.path.isdir(stale)
    assert repo_entries.sweep_stale_hidden_dirs(str(tmp_path / "absent")) == []


def test_a_directory_removed_meanwhile_is_not_reported(tmp_path, monkeypatch):
    repo = str(tmp_path)
    _stamped_staging(repo, M, "ffff0000", 1)

    def gone(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(repo_entries.shutil, "rmtree", gone)
    assert repo_entries.sweep_stale_hidden_dirs(repo, stamp_reader=lambda: BOOT + "-1") == []
