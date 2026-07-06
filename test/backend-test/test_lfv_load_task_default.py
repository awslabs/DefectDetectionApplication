#  Copyright  Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Property-based test for the Base_Model's manifest task loader.

Covers the object-detection-visualization design's Property 9 (a manifest that
omits the ``task`` field resolves to the anomaly task), which guards Requirement
5.2's backward-compatibility guarantee.

Importing ``lfv_model_template.py`` normally requires the Triton Python-backend
module (``triton_python_backend_utils``) plus a chain of heavy inference
dependencies (dlr / torch / sklearn / cv2 / ...), none of which are installed in
the unit-test environment and none of which the ``__load_task`` code path
actually touches -- it only opens ``manifest.json`` and does ``json.load`` +
``dict.get``. Following the repo pattern of injecting lightweight import-time
stubs (see conftest.py's ``import_mocker`` and the mock_gi/mock_logger helpers),
we register a *fallback* meta-path finder that stubs only modules that cannot be
resolved normally. First-party packages (resolved via ``PYTHONPATH=src/backend``)
and already-installed test deps (numpy, hypothesis, pytest) import for real; only
the genuinely-missing native/inference deps get a stub, so the real
``__load_task`` implementation is exercised.
"""
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest.mock as mock

from hypothesis import given, settings
from hypothesis import strategies as st

# First-party packages must resolve for real (via PYTHONPATH=src/backend); never
# stub these or the module under test would not be the real one.
_FIRST_PARTY = {
    "lyra_science_processing_utils",
    "lyra_anomalies_mask_utils",
    "dda_triton",
    "inference_runtimes",
}


class _StubLoader(importlib.abc.Loader):
    """Loader producing an empty, package-like stub whose attribute access yields
    permissive MagicMocks (so class-bodies/decorators in stubbed deps don't blow
    up at import time)."""

    def create_module(self, spec):
        module = types.ModuleType(spec.name)
        module.__path__ = []  # mark as a package so submodule imports resolve
        module.__getattr__ = lambda _name: mock.MagicMock()
        return module

    def exec_module(self, module):
        pass


class _FallbackStubFinder(importlib.abc.MetaPathFinder):
    """Meta-path finder of last resort: stubs any module that no real finder could
    resolve, except first-party packages and builtins."""

    def find_spec(self, fullname, path=None, target=None):
        top = fullname.split(".")[0]
        if top in _FIRST_PARTY or top in sys.builtin_module_names:
            return None
        return importlib.machinery.ModuleSpec(
            fullname, _StubLoader(), is_package=True
        )


def _load_base_model_module():
    """Import lfv_model_template.py with the fallback stub finder installed."""
    finder = _FallbackStubFinder()
    # Appended (not prepended) so real finders win; only truly-missing deps stub.
    sys.meta_path.append(finder)
    try:
        template_path = os.path.join(
            os.getcwd(),
            "src",
            "backend",
            "dda_triton",
            "resources_for_copy",
            "lfv_model_template.py",
        )
        spec = importlib.util.spec_from_file_location(
            "lfv_model_template_under_test", template_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.meta_path.remove(finder)


_BASE_MODEL_MODULE = _load_base_model_module()
TritonPythonModel = _BASE_MODEL_MODULE.TritonPythonModel


def _call_load_task(model_dir):
    """Invoke the name-mangled private ``__load_task`` on a bare instance.

    ``__load_task`` uses only ``model_dir`` and class constants (no instance
    state), so an un-initialised instance is sufficient and avoids the heavy
    ``initialize`` path."""
    instance = object.__new__(TritonPythonModel)
    return instance._TritonPythonModel__load_task(model_dir)


def _write_manifest(model_dir, manifest):
    with open(
        os.path.join(model_dir, TritonPythonModel.MANIFEST_FILENAME),
        "w",
        encoding="utf-8",
    ) as fh:
        json.dump(manifest, fh)


# JSON-serialisable scalar / container values for arbitrary "other" manifest keys.
_json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**6), max_value=10**6),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=20),
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(keys=st.text(max_size=10), values=children, max_size=4),
    ),
    max_leaves=10,
)

# Manifests that OMIT the ``task`` key but may carry arbitrary other keys. The
# manifest task key ("task") is explicitly excluded so we only exercise the
# missing-task path.
_manifest_without_task = st.dictionaries(
    keys=st.text(max_size=15).filter(
        lambda k: k != TritonPythonModel.TASK_MANIFEST_KEY
    ),
    values=_json_values,
    max_size=8,
)


# Feature: object-detection-visualization, Property 9: Missing task defaults to anomaly
# Validates: Requirements 5.2
@settings(max_examples=100)
@given(manifest=_manifest_without_task)
def test_missing_task_defaults_to_anomaly(manifest):
    """For any manifest that omits the ``task`` field (but may contain arbitrary
    other keys), ``__load_task`` resolves to the anomaly task."""
    assert TritonPythonModel.TASK_MANIFEST_KEY not in manifest  # precondition
    with tempfile.TemporaryDirectory() as model_dir:
        _write_manifest(model_dir, manifest)
        resolved = _call_load_task(model_dir)
    assert resolved == TritonPythonModel.TASK_ANOMALY


# Feature: object-detection-visualization, Property 9: Missing task defaults to anomaly
# Validates: Requirements 5.2
def test_empty_manifest_defaults_to_anomaly():
    """Deterministic edge case: a manifest with no keys at all resolves to the
    anomaly task."""
    with tempfile.TemporaryDirectory() as model_dir:
        _write_manifest(model_dir, {})
        assert _call_load_task(model_dir) == TritonPythonModel.TASK_ANOMALY


# Feature: object-detection-visualization, Property 9: Missing task defaults to anomaly
# Validates: Requirements 5.2
def test_manifest_with_other_keys_defaults_to_anomaly():
    """Deterministic example: a manifest carrying unrelated keys but no ``task``
    resolves to the anomaly task."""
    with tempfile.TemporaryDirectory() as model_dir:
        _write_manifest(
            model_dir,
            {"runtime": "onnx", "class_names": ["cat", "dog"], "version": 3},
        )
        assert _call_load_task(model_dir) == TritonPythonModel.TASK_ANOMALY
