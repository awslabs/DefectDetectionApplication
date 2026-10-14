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
"""Confinement of the legacy reference-image map migration
(security-scan-remediation-high, Requirements 10.1 and 10.2).

``migrate_legacy_map`` loads a legacy map only through
``open_trusted_legacy_map``: the file must resolve below
``TRUSTED_LEGACY_MAP_ROOTS``, and the descriptor actually opened must lie
below them too and be a regular file owned by root or the effective uid,
writable by neither group nor others. The roots are monkeypatched to a
temporary directory. In the refusal cases the deserializer's ``load`` is
replaced with a recorder, so each case also shows it never ran.

Feature: security-scan-remediation-high, Property 6: Legacy maps are loaded
only from trusted files. Validates: Requirements 10.1, 10.2
"""
import ast
import contextlib
import errno
import filecmp
import os
import signal
import stat

import numpy as np
import pytest

from lyra_science_processing_utils.model_processors import (
    reference_image_map_migration as migration,
)
from lyra_science_processing_utils.model_processors.reference_image_map_io import (
    derive_safe_paths,
    load_safe_reference_image_map,
)

_REPO = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, os.pardir))
_MODULE = os.path.join("lyra_science_processing_utils", "model_processors",
                       "reference_image_map_migration.py")
COPIES = (
    os.path.join(_REPO, "src", "backend", _MODULE),
    os.path.join(_REPO, "edge-cv-portal", "test-sandbox", "dda_triton_resources",
                 _MODULE),
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    """The test's one trusted root, a temporary directory. ``raising=False``
    lets the round-trip cases run against a module without the constant, as
    at 4a3f960, which they preserve."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    monkeypatch.setattr(migration, "TRUSTED_LEGACY_MAP_ROOTS", (str(trusted),),
                        raising=False)
    return trusted


def write_legacy_map(path, image_index, mode=0o644):
    """A legacy map as training wrote it, with the legacy serializer, given
    ``mode``; owned by the test user, who is also the effective uid."""
    dill = pytest.importorskip("dill")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "wb") as handle:
        dill.dump({"image_index": image_index}, handle)
    os.chmod(str(path), mode)
    return path


@pytest.fixture
def outside(tmp_path):
    """A well-formed legacy map outside the trusted root."""
    return write_legacy_map(
        tmp_path / "elsewhere" / "map.legacy", {"/refs/x.png": np.zeros((1, 2))})


@pytest.fixture
def load_calls(monkeypatch):
    """Every call of the deserializer's ``load``, recorded instead of run."""
    dill = pytest.importorskip("dill")
    calls = []
    monkeypatch.setattr(dill, "load", lambda *args, **kwargs: calls.append(args))
    return calls


@contextlib.contextmanager
def fails_fast(seconds=10):
    """An open that blocks (a FIFO opened without ``O_NONBLOCK``) fails the
    test instead of hanging it."""
    def expire(signum, frame):
        raise TimeoutError("open_trusted_legacy_map blocked")
    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def refusal(path, load_calls):
    """The message of ``migrate_legacy_map(path)``'s refusal, after checking
    that nothing was loaded or written and no descriptor stayed open."""
    open_before = set(os.listdir("/proc/self/fd"))
    with fails_fast(), pytest.raises(migration.UntrustedLegacyMapError) as e:
        migration.migrate_legacy_map(str(path))
    assert set(os.listdir("/proc/self/fd")) <= open_before
    assert load_calls == []
    for sidecar in derive_safe_paths(str(path)):
        assert not os.path.exists(sidecar)
    message = str(e.value)
    assert repr(str(path)) in message
    assert "Nothing was loaded or written" in message
    assert "(chown root, chmod go-w)" in message
    return message


# Round trip (Requirement 10.2: only JSON and allow_pickle=False NumPy out)

def test_a_trusted_legacy_map_migrates_to_the_safe_format(root):
    rng = np.random.default_rng(10)
    image_index = {path: rng.random((1, 8))
                   for path in ("/refs/b.png", "/refs/a.png", "/refs/c.png")}
    legacy = write_legacy_map(root / "model" / "reference_image_map.legacy",
                              image_index)

    written = migration.migrate_legacy_map(str(legacy))

    assert written == derive_safe_paths(str(legacy))
    paths, gallery = load_safe_reference_image_map(str(legacy))
    assert paths == ["/refs/b.png", "/refs/a.png", "/refs/c.png"]
    expected = np.vstack(list(image_index.values()))
    np.testing.assert_array_equal(gallery, expected)
    np.testing.assert_array_equal(np.load(written[1], allow_pickle=False), expected)


def test_the_cli_converts_a_trusted_map_to_the_chosen_output_base(
        root, tmp_path, capsys):
    feature = np.arange(4, dtype=np.float32).reshape(1, 4)
    legacy = write_legacy_map(root / "reference_image_map.legacy",
                              {"/refs/x.png": feature}, mode=0o600)
    base = tmp_path / "output" / "refmap.legacy"
    base.parent.mkdir()

    assert migration.main(
        [str(legacy), "--reference-image-map-file", str(base)]) == 0

    paths, gallery = load_safe_reference_image_map(str(base))
    assert paths == ["/refs/x.png"]
    np.testing.assert_array_equal(gallery, feature)
    assert str(base.parent / "refmap.paths.json") in capsys.readouterr().out


# Refusals (Property 6): the deserializer never runs, nothing is written

def test_a_file_outside_the_roots_is_refused(root, outside, load_calls):
    assert "Outside the trusted roots" in refusal(outside, load_calls)


def test_a_dot_dot_escape_is_refused(root, outside, load_calls):
    escape = os.path.join(str(root), os.pardir, "elsewhere", "map.legacy")
    message = refusal(escape, load_calls)
    assert "Outside the trusted roots" in message
    assert "resolves to {0!r}".format(os.path.realpath(str(outside))) in message


def test_a_symlink_inside_the_root_pointing_outside_is_refused(
        root, outside, load_calls):
    link = root / "link.legacy"
    link.symlink_to(outside)
    message = refusal(link, load_calls)
    assert "Outside the trusted roots" in message
    assert "resolves to {0!r}".format(os.path.realpath(str(outside))) in message


def test_a_root_whose_own_path_is_a_symlink_is_not_followed(
        tmp_path, monkeypatch, load_calls):
    """The roots are compared as written. A symlink put in place of a root,
    as whoever can rename entries in its parent could do, doesn't make the
    directory it points to trusted, even for an otherwise trusted map."""
    swapped_in = tmp_path / "swapped-in"
    target = write_legacy_map(swapped_in / "map.legacy",
                              {"/refs/x.png": np.zeros((1, 2))})
    trusted = tmp_path / "trusted"
    trusted.symlink_to(swapped_in, target_is_directory=True)
    monkeypatch.setattr(migration, "TRUSTED_LEGACY_MAP_ROOTS", (str(trusted),),
                        raising=False)
    message = refusal(trusted / "map.legacy", load_calls)
    assert "Outside the trusted roots" in message
    assert "resolves to {0!r}".format(str(target)) in message


def test_a_symlink_swapped_in_after_the_resolve_fails_the_open(
        root, outside, load_calls, monkeypatch):
    """``O_NOFOLLOW``: a final-component symlink that replaces the file
    between the resolve and the open fails the open with ``ELOOP``. The race
    is simulated by a ``realpath`` that returns the in-root symlink as is."""
    link = root / "map.legacy"
    link.symlink_to(outside)
    real_realpath = os.path.realpath

    def realpath(path, *args, **kwargs):
        if str(path) == str(link):
            return str(path)
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(migration.os.path, "realpath", realpath)
    open_before = set(os.listdir("/proc/self/fd"))
    with fails_fast(), pytest.raises(OSError) as e:
        migration.migrate_legacy_map(str(link))
    assert e.value.errno == errno.ELOOP
    assert set(os.listdir("/proc/self/fd")) <= open_before
    assert load_calls == []
    for sidecar in derive_safe_paths(str(link)):
        assert not os.path.exists(sidecar)


@pytest.mark.parametrize("mode", [0o664, 0o646], ids=["0664", "0646"])
def test_a_group_or_world_writable_file_is_refused(root, load_calls, mode):
    legacy = write_legacy_map(root / "map.legacy",
                              {"/refs/x.png": np.zeros((1, 2))}, mode=mode)
    message = refusal(legacy, load_calls)
    assert "Group- or world-writable (mode {0:04o})".format(mode) in message


def test_a_directory_is_refused(root, load_calls):
    directory = root / "map.legacy"
    directory.mkdir()
    assert "Not a regular file" in refusal(directory, load_calls)


def test_a_fifo_is_refused_without_blocking(root, load_calls):
    fifo = root / "map.legacy"
    os.mkfifo(str(fifo))
    assert "Not a regular file" in refusal(fifo, load_calls)


def test_the_location_check_applies_to_the_descriptor_actually_opened(
        root, outside, load_calls, monkeypatch):
    legacy = write_legacy_map(root / "map.legacy",
                              {"/refs/x.png": np.zeros((1, 2))})
    real_readlink = os.readlink
    asked = []

    def readlink(path, *args, **kwargs):
        if str(path).startswith("/proc/self/fd/"):
            asked.append(path)
            return str(outside)  # as if a parent directory had been swapped
        return real_readlink(path, *args, **kwargs)

    monkeypatch.setattr(migration.os, "readlink", readlink)
    message = refusal(legacy, load_calls)
    assert len(asked) == 1
    assert "Outside the trusted roots" in message
    assert "resolves to {0!r}".format(str(outside)) in message


# Owner rule, on synthetic fstat results: a test can't chown without root

def synthetic_stat(uid, mode=stat.S_IFREG | 0o644):
    return os.stat_result((mode, 1, 1, 1, uid, 0, 64, 0, 0, 0))


@pytest.mark.parametrize("uid", [0, 4242], ids=["root", "effective-uid"])
def test_the_owner_rule_admits_root_and_the_effective_uid(monkeypatch, uid):
    monkeypatch.setattr(migration.os, "geteuid", lambda: 4242)
    migration._check_trusted_stat(synthetic_stat(uid), "/trusted/map.legacy")


def test_the_owner_rule_refuses_any_other_uid(monkeypatch):
    monkeypatch.setattr(migration.os, "geteuid", lambda: 4242)
    with pytest.raises(migration.UntrustedLegacyMapError) as e:
        migration._check_trusted_stat(synthetic_stat(4243), "/trusted/map.legacy")
    assert "Owned by uid 4243, not root or the effective uid 4242" in str(e.value)


# CLI: a refusal goes to parser.error, exit code 2

@pytest.mark.parametrize("case", ["outside", "group-writable"])
def test_a_cli_refusal_exits_2_and_names_the_condition(
        root, outside, load_calls, capsys, case):
    if case == "outside":
        path, condition = outside, "Outside the trusted roots"
    else:
        path = write_legacy_map(root / "map.legacy",
                                {"/refs/x.png": np.zeros((1, 2))}, mode=0o664)
        condition = "Group- or world-writable (mode 0664)"
    with pytest.raises(SystemExit) as e:
        migration.main([str(path)])
    assert e.value.code == 2
    assert condition in capsys.readouterr().err
    assert load_calls == []


# Parity and static shape of the two copies

def test_the_two_copies_are_byte_identical():
    assert filecmp.cmp(COPIES[0], COPIES[1], shallow=False)


def _calls_named(node, name):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == name]


def _imports_the_deserializer(node):
    if isinstance(node, ast.Import):
        return any(alias.name.split(".")[0] == "dill" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] == "dill"
    return False


def _calls_the_deserializer(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "dill")


@pytest.mark.parametrize("copy", COPIES, ids=["src-backend", "test-sandbox"])
def test_the_deserializer_is_imported_and_called_only_after_the_trusted_open(copy):
    with open(copy) as handle:
        tree = ast.parse(handle.read(), copy)
    migrate = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "migrate_legacy_map")
    inside = {id(node) for node in ast.walk(migrate)}
    imports = [node for node in ast.walk(tree) if _imports_the_deserializer(node)]
    calls = [node for node in ast.walk(tree) if _calls_the_deserializer(node)]
    trusted_open = _calls_named(migrate, "open_trusted_legacy_map")
    assert (len(imports), len(calls), len(trusted_open)) == (1, 1, 1)
    assert id(imports[0]) in inside and id(calls[0]) in inside
    assert trusted_open[0].lineno < imports[0].lineno < calls[0].lineno
    assert _calls_named(migrate, "open") == []  # it loads only the trusted handle
