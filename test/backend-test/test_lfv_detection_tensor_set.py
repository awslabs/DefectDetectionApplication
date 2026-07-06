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
"""Smoke test for the Base_Model object-detection output contract.

Feature: object-detection-visualization
Requirement 5.1: THE Base_Model SHALL emit Detection_Results through the existing
variable-length ``anomalies`` tensor WITHOUT adding or removing tensors from the
output contract.

This asserts ``TritonPythonModel.__build_detection_tensors`` emits EXACTLY the
tensor names ``{output, output_confidence, output_score, mask, anomalies}`` for
both a with-detections capture and the zero-object sentinel capture (empty
``objects``) -- no added or removed tensors in either path.

Importing ``lfv_model_template.py`` requires ``triton_python_backend_utils`` (the
Triton Python-backend module, not installed in the test env) and drags in the
sklearn-dependent ``model_graph_factory`` at import time. Neither is needed by
``__build_detection_tensors`` (which only uses ``pb_utils.Tensor``, numpy, json,
``resolve_class_label`` and instance dtype attributes), so both are stubbed in
``sys.modules`` before the module is loaded. The fake ``pb_utils.Tensor`` records
the tensor NAME so the emitted set can be asserted directly.
"""
import importlib.util
import os
import sys
import types

import numpy as np
import pytest

from lyra_science_processing_utils.utils.object_detection_result import (
    ObjectDetectionResult,
)

# The exact frozen output-contract tensor set (Requirement 5.1).
EXPECTED_TENSOR_NAMES = {
    "output",
    "output_confidence",
    "output_score",
    "mask",
    "anomalies",
}

_LFV_MODEL_TEMPLATE_PATH = os.path.join(
    os.getcwd(),
    "src",
    "backend",
    "dda_triton",
    "resources_for_copy",
    "lfv_model_template.py",
)


class _FakeTensor:
    """Minimal stand-in for ``pb_utils.Tensor`` that records the tensor name.

    The real Triton Tensor validates the name against the model config; here we
    only need to capture the name so the test can assert the emitted tensor set.
    """

    def __init__(self, name, arr):
        self._name = name
        self._arr = arr

    def name(self):
        return self._name


def _load_lfv_model_template():
    """Load ``lfv_model_template`` with triton + the heavy graph-factory import
    stubbed, returning the module object."""
    pb_utils_stub = types.ModuleType("triton_python_backend_utils")
    pb_utils_stub.Tensor = _FakeTensor
    pb_utils_stub.triton_string_to_numpy = lambda s: np.float32
    sys.modules["triton_python_backend_utils"] = pb_utils_stub

    # model_graph_factory pulls in sklearn (unavailable) at import time and is
    # unused by the detection-tensor builder; stub it out.
    graph_factory_stub = types.ModuleType(
        "lyra_science_processing_utils.model_graph_factory"
    )

    class _ModelGraphFactory:  # pragma: no cover - stub
        pass

    graph_factory_stub.ModelGraphFactory = _ModelGraphFactory
    sys.modules[
        "lyra_science_processing_utils.model_graph_factory"
    ] = graph_factory_stub

    spec = importlib.util.spec_from_file_location(
        "lfv_model_template", _LFV_MODEL_TEMPLATE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeObject:
    """Stand-in for a per-object inference entry exposing ``object_detection``."""

    def __init__(self, object_detection):
        self.object_detection = object_detection


class _FakeInferenceOutput:
    """Stand-in for the inference output exposing ``objects``."""

    def __init__(self, objects):
        self.objects = objects


def _make_detection_model_instance(module):
    """Build a ``TritonPythonModel`` instance with only the attributes that
    ``__build_detection_tensors`` reads, bypassing ``__init__`` (which loads a
    real model)."""
    instance = module.TritonPythonModel.__new__(module.TritonPythonModel)
    instance.output_dtype = np.uint8
    instance.confidence_dtype = np.float32
    instance.score_dtype = np.float32
    instance.mask_dtype = np.uint8
    instance.anomalies_dtype = np.uint8
    # Name-mangled private attribute read by the builder for label resolution.
    instance._TritonPythonModel__class_names = None
    return instance


def _build_detection_tensors(instance, inference_output, input_np):
    build = getattr(instance, "_TritonPythonModel__build_detection_tensors")
    return build(inference_output, input_np)


@pytest.fixture(scope="module")
def lfv_module():
    return _load_lfv_model_template()


def test_detection_path_emits_exact_tensor_set_with_detections(lfv_module):
    """A capture with real detections emits exactly the frozen tensor set."""
    instance = _make_detection_model_instance(lfv_module)
    detections = [
        _FakeObject(ObjectDetectionResult([12, 40, 220, 310], "17", 0.83, 0.5)),
        _FakeObject(ObjectDetectionResult([5, 6, 30, 44], "3", 0.61, 0.5)),
    ]
    inference_output = _FakeInferenceOutput(detections)
    input_np = np.zeros((8, 8))

    tensors = _build_detection_tensors(instance, inference_output, input_np)
    emitted = [t.name() for t in tensors]

    # Exactly the frozen set: no added and no removed tensors, no duplicates.
    assert set(emitted) == EXPECTED_TENSOR_NAMES
    assert len(emitted) == len(EXPECTED_TENSOR_NAMES)


def test_detection_path_emits_exact_tensor_set_zero_objects(lfv_module):
    """The zero-object sentinel capture emits the SAME exact tensor set."""
    instance = _make_detection_model_instance(lfv_module)
    inference_output = _FakeInferenceOutput([])  # no detected objects
    input_np = np.zeros((8, 8))

    tensors = _build_detection_tensors(instance, inference_output, input_np)
    emitted = [t.name() for t in tensors]

    assert set(emitted) == EXPECTED_TENSOR_NAMES
    assert len(emitted) == len(EXPECTED_TENSOR_NAMES)


def test_detection_tensor_set_identical_across_object_counts(lfv_module):
    """The emitted tensor NAME set must not depend on the number of detections:
    the with-detections and zero-object paths emit an identical set."""
    instance = _make_detection_model_instance(lfv_module)
    input_np = np.zeros((8, 8))

    with_detections = _build_detection_tensors(
        instance,
        _FakeInferenceOutput(
            [_FakeObject(ObjectDetectionResult([1, 2, 3, 4], "17", 0.9, 0.5))]
        ),
        input_np,
    )
    zero_objects = _build_detection_tensors(
        instance, _FakeInferenceOutput([]), input_np
    )

    assert {t.name() for t in with_detections} == {
        t.name() for t in zero_objects
    } == EXPECTED_TENSOR_NAMES
