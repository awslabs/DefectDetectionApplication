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
"""The host-run model scripts and their helpers import only what the host's
system ``python3`` has (triton-model-cleanup-wedge, task 3.5).

The model components' lifecycle runs ``/aws_dda/model_convertor.py`` and
``/aws_dda/convert_model_cleanup.py`` with the host interpreter (3.8 on
JP5), next to the helpers the backend seeds into ``/aws_dda/dda_triton/``.
They may import the standard library, ``requests``, ``google.protobuf`` and
``dda_triton.*`` only, and the three helpers that this spec added are
imported inside ``try: ... except Exception:`` because the backend re-seeds
them with a non-atomic copy.

_Requirements: 3.14_
"""
import ast
import importlib.util
import os
import sys
import sysconfig

import pytest

from triton_cleanup_wedge.fakes import BACKEND_SRC

DDA_TRITON = os.path.join(BACKEND_SRC, "dda_triton")
HOST_MODULES = ("model_convertor.py", "convert_model_cleanup.py", "model_autostart_utils.py",
                "constants.py", "model_locks.py", "model_status_records.py", "repo_entries.py")
OPTIONAL = {"dda_triton.model_locks", "dda_triton.repo_entries",
            "dda_triton.model_status_records"}
ALLOWED_THIRD_PARTY = {"requests", "google"}


def _tree(name):
    with open(os.path.join(DDA_TRITON, name), encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=name)


def _imports(tree):
    """``(module, node)`` for every import, with ``from dda_triton import x``
    reported as ``dda_triton.x``."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                out.append(("." + (node.module or ""), node))
            elif node.module == "dda_triton":
                for alias in node.names:
                    out.append(("dda_triton." + alias.name, node))
            else:
                out.append((node.module, node))
    return out


def _is_stdlib(top):
    if top in sys.builtin_module_names:
        return True
    spec = importlib.util.find_spec(top)
    if spec is None:
        return False
    origin = spec.origin or ""
    if origin in ("built-in", "frozen"):
        return True
    stdlib = os.path.realpath(sysconfig.get_paths()["stdlib"])
    platstdlib = os.path.realpath(sysconfig.get_paths()["platstdlib"])
    real = os.path.realpath(origin)
    if "site-packages" in real or "dist-packages" in real:
        return False
    return real.startswith(stdlib) or real.startswith(platstdlib)


def _guarded(tree, target):
    """True when ``target`` sits in the body of a ``try`` with an
    ``except Exception`` handler."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches = any(isinstance(h.type, ast.Name) and h.type.id in ("Exception", "BaseException")
                      for h in node.handlers)
        if catches and any(target is child for stmt in node.body for child in ast.walk(stmt)):
            return True
    return False


@pytest.mark.parametrize("name", HOST_MODULES)
def test_imports_are_stdlib_requests_protobuf_or_dda_triton(name):
    bad = []
    for module, _node in _imports(_tree(name)):
        top = module.split(".")[0]
        if module.startswith("."):
            bad.append(module)
        elif top == "dda_triton":
            if not os.path.exists(os.path.join(DDA_TRITON, module.split(".")[1] + ".py")):
                bad.append(module)
        elif top in ALLOWED_THIRD_PARTY:
            if top == "google" and not module.startswith("google.protobuf"):
                bad.append(module)
        elif not _is_stdlib(top):
            bad.append(module)
    assert bad == [], "{0} imports {1}".format(name, bad)


@pytest.mark.parametrize("name", ["model_convertor.py", "convert_model_cleanup.py"])
def test_the_new_helpers_are_optional_imports(name):
    tree = _tree(name)
    found = {m: node for m, node in _imports(tree) if m in OPTIONAL}
    assert set(found) == OPTIONAL
    for module, node in found.items():
        assert _guarded(tree, node), "{0}: {1} is not inside try/except Exception".format(
            name, module)


@pytest.mark.parametrize("name", ["model_locks.py", "model_status_records.py", "repo_entries.py"])
def test_the_helpers_do_not_import_each_other(name):
    for module, _node in _imports(_tree(name)):
        assert module not in OPTIONAL, "{0} imports {1}".format(name, module)


@pytest.mark.parametrize("name", HOST_MODULES)
def test_host_modules_parse_as_python_3_8(name):
    with open(os.path.join(DDA_TRITON, name), encoding="utf-8") as handle:
        ast.parse(handle.read(), filename=name, feature_version=(3, 8))
