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
"""Camera ids derived through the non-security SHA-1 helper
(security-scan-remediation-high, Requirement 12).

``aravis_stable_id`` and ``make_stable_id`` hash through
``camera_discovery.stable_hash.id_digest_hex``, which marks SHA-1 as a
non-security use and falls back to the plain call on Python 3.8. The ids
must not change: the goldens were computed from the unchanged derivations
at 4a3f960, and every persisted ``camera_source_id`` depends on them.

Feature: security-scan-remediation-high, Property 7: Camera ids don't
change. Validates: Requirements 12.2, 12.3
"""
import ast
import hashlib
import os
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from camera_discovery import stable_hash
from camera_discovery.aravis import aravis_stable_id
from camera_discovery.discovery import make_stable_id
from camera_discovery.stable_hash import id_digest_hex

_REPO = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, os.pardir))
PACKAGE = os.path.join(_REPO, "src", "backend", "camera_discovery")

# Computed from the unchanged derivations at 4a3f960 (design `R12 tests`).
GOLDENS = [
    (aravis_stable_id, ("Basler", "acA1920", "12345678"), "arv-261e4a8e765b"),
    (aravis_stable_id, ("V", "M", "", "phys-1"), "arv-31d3ec281d08"),
    (aravis_stable_id, ("Aravis", "Fake GV Camera", "GV01"), "arv-68b74f7de25a"),
    (make_stable_id, ("usb-0000:00:14.0-1", "Cam A"), "disc-da0f363d2660"),
    (make_stable_id, ("platform:tegra-capture-vi:0", "vi-output, imx219 9-0010"),
     "disc-f8dd2b32a75b"),
]
GOLDEN_IDS = [golden for _, _, golden in GOLDENS]


@pytest.mark.parametrize("derive, args, golden", GOLDENS, ids=GOLDEN_IDS)
def test_camera_ids_match_the_4a3f960_goldens(derive, args, golden):
    assert derive(*args) == golden


# Python 3.8 fallback (Requirement 12.3), on any interpreter

class Py38Hashlib:
    """``hashlib`` as Python 3.8 has it: ``sha1`` rejects the
    ``usedforsecurity`` keyword (added in 3.9) and otherwise delegates."""

    def __init__(self):
        self.calls = []

    def sha1(self, *args, **kwargs):
        self.calls.append(sorted(kwargs))
        if "usedforsecurity" in kwargs:
            raise TypeError("'usedforsecurity' is an invalid keyword argument "
                            "for openssl_sha1()")
        return hashlib.sha1(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(hashlib, name)


@pytest.fixture
def py38_hashlib(monkeypatch):
    stub = Py38Hashlib()
    monkeypatch.setattr(stable_hash, "hashlib", stub)
    return stub


@pytest.mark.parametrize("derive, args, golden", GOLDENS, ids=GOLDEN_IDS)
def test_the_python_38_fallback_keeps_the_goldens(
        py38_hashlib, derive, args, golden):
    assert derive(*args) == golden
    # The keyword call was refused, then the plain call gave the digest.
    assert py38_hashlib.calls == [["usedforsecurity"], []]


@pytest.mark.parametrize("path", ["usedforsecurity", "python-3.8-fallback"])
@settings(max_examples=settings.get_profile("default").max_examples, deadline=None)
@given(text=st.text())
def test_property_the_id_digest_is_the_sha1_the_ids_were_derived_from(
        path, text):
    """**Feature: security-scan-remediation-high, Property 7: Camera ids
    don't change.** For any text, on either path, ``id_digest_hex`` is the
    hex SHA-1 of its UTF-8 bytes, as the derivations computed before.
    **Validates: Requirements 12.2, 12.3**"""
    expected = hashlib.sha1(text.encode("utf-8")).hexdigest()
    if path == "usedforsecurity":
        assert id_digest_hex(text) == expected
    else:
        with mock.patch.object(stable_hash, "hashlib", Py38Hashlib()):
            assert id_digest_hex(text) == expected


# Static shape of the package

def _parsed_modules():
    """Every module of the ``camera_discovery`` source package, parsed."""
    trees = {}
    for name in sorted(os.listdir(PACKAGE)):
        if name.endswith(".py"):
            path = os.path.join(PACKAGE, name)
            with open(path, encoding="utf-8") as handle:
                trees[name] = ast.parse(handle.read(), path)
    return trees


def _imports_hashlib(tree):
    return any(
        (isinstance(node, ast.Import)
         and any(alias.name == "hashlib" for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "hashlib")
        for node in ast.walk(tree))


def _sha1_calls(tree):
    return sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Call)
         and isinstance(node.func, ast.Attribute) and node.func.attr == "sha1"
         and isinstance(node.func.value, ast.Name)
         and node.func.value.id == "hashlib"),
        key=lambda node: node.lineno)


def test_sha1_is_called_only_in_the_helper_and_marked_non_security():
    trees = _parsed_modules()
    assert {"aravis.py", "discovery.py", "stable_hash.py"} <= set(trees)
    assert [name for name, tree in trees.items()
            if _imports_hashlib(tree)] == ["stable_hash.py"]
    assert [name for name, tree in trees.items()
            if _sha1_calls(tree)] == ["stable_hash.py"]
    calls = _sha1_calls(trees["stable_hash.py"])
    assert len(calls) == 2
    (keyword,) = calls[0].keywords
    assert keyword.arg == "usedforsecurity"
    assert isinstance(keyword.value, ast.Constant) and keyword.value.value is False
    # The one plain call is the Python 3.8 fallback, under ``except TypeError``.
    assert calls[1].keywords == []
    handlers = [node for node in ast.walk(trees["stable_hash.py"])
                if isinstance(node, ast.ExceptHandler)
                and isinstance(node.type, ast.Name) and node.type.id == "TypeError"]
    assert len(handlers) == 1
    assert any(node is calls[1] for node in ast.walk(handlers[0]))


@pytest.mark.parametrize("name", ["stable_hash.py", "aravis.py", "discovery.py"])
def test_the_id_modules_parse_as_python_38(name):
    """Requirement 12.3 names Python 3.8, which no shipped image runs these
    modules on; this grammar gate and the fallback cases cover it, in the
    style of the 3.10 gate in ``test/backend-test/test_py310_compat.py``."""
    path = os.path.join(PACKAGE, name)
    with open(path, encoding="utf-8") as handle:
        ast.parse(handle.read(), filename=path, feature_version=(3, 8))
