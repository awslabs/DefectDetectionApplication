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
"""Model-based / equivalence property test for the Marshal's anomaly path.

Feature: object-detection-visualization
Property 7: Anomaly-classification behavior is unchanged.

For ANY reused ``anomalies`` payload that does NOT contain a ``bounding_box``
field (i.e. an anomaly payload: an empty list, or a list of dicts carrying the
segmentation keys ``name`` / ``hex_color`` / ``total_percentage_area``), the
metadata produced by the *current*
``TritonPythonModel._generate_capture_meta_data`` must be identical to a frozen
"legacy" reference implementation of the pre-detection anomaly branch.

The legacy reference oracle (``_legacy_generate_capture_meta_data`` below) is a
pure re-implementation of ``_generate_capture_meta_data`` as it behaved BEFORE
object-detection support was added: no detection typing, no ``Detection_count``,
and the overlay ``Auxiliary_Output_Reference`` emitted ONLY inside the
anomaly-mask branch. Asserting equivalence for random anomaly inputs guards that
the detection changes left the anomaly path byte-for-byte compatible (the
base64-encoded ``inference result`` and ``anomalies`` blocks compare as strings,
so any drift in ordering/content is caught).

Coverage across the generated inputs:
  * has-mask case  -> ``_has_anomaly_mask`` True (non-zero mask of the SAME shape
    as the source image) so the mask ref + segmentation metadata block AND the
    mask-based overlay ref are exercised.
  * no-mask case   -> zeroed / empty / mismatched-shape mask so no mask ref and
    (legacy) no overlay ref.
  * anomalous      -> ``inference_output`` truthy  -> "Anomaly".
  * normal         -> ``inference_output`` falsy   -> "Normal".

Importing ``marshal_for_capture_template.py`` requires the Triton Python-backend
module (``triton_python_backend_utils``) and ``cv2``, neither of which is
installed in the unit-test environment and neither of which the metadata
function actually touches (``_generate_capture_meta_data`` only uses numpy, json,
base64, os and ``self._has_anomaly_mask``/``self._is_detection_list``). Both are
stubbed in ``sys.modules`` before the module is loaded, mirroring the sibling
``test_lfv_detection_tensor_set.py`` stubbing approach.
"""
import base64
import importlib.util
import json
import os
import sys
import types

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st


_MARSHAL_TEMPLATE_PATH = os.path.join(
    os.getcwd(),
    "src",
    "backend",
    "dda_triton",
    "resources_for_copy",
    "marshal_for_capture_template.py",
)


def _load_marshal_module():
    """Load ``marshal_for_capture_template`` with triton + cv2 stubbed."""
    pb_utils_stub = types.ModuleType("triton_python_backend_utils")
    pb_utils_stub.triton_string_to_numpy = lambda s: np.float32
    sys.modules["triton_python_backend_utils"] = pb_utils_stub

    # cv2 is only used by overlay/mask encoders, not by the metadata function
    # under test. Prefer the real cv2 when importable so no stub leaks into
    # sibling tests that need the genuine library; only stub when cv2 is absent.
    try:
        import cv2  # noqa: F401
    except ImportError:  # pragma: no cover - depends on runner
        sys.modules.setdefault("cv2", types.ModuleType("cv2"))

    spec = importlib.util.spec_from_file_location(
        "marshal_for_capture_template_under_test", _MARSHAL_TEMPLATE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MARSHAL_MODULE = _load_marshal_module()
TritonPythonModel = _MARSHAL_MODULE.TritonPythonModel

_MODEL_NAME = "unittest_model"
_MODEL_VERSION = "7"


def _make_marshal_instance():
    """Build a Marshal instance exposing only the attributes the metadata
    function reads (``model_name`` / ``model_version``), bypassing the heavy
    ``initialize`` path."""
    instance = TritonPythonModel.__new__(TritonPythonModel)
    instance.model_name = _MODEL_NAME
    instance.model_version = _MODEL_VERSION
    return instance


def _legacy_generate_capture_meta_data(
    instance,
    capture_meta_data,
    inference_output,
    time_str,
    inference_confidence,
    inference_mask,
    inference_anomalies,
    inference_score,
    input_image,
):
    """Frozen reference oracle: the pre-detection anomaly-branch behavior of
    ``_generate_capture_meta_data``. This is intentionally a faithful copy of the
    legacy logic (no detection typing, overlay ref only under the mask branch)."""
    ret = {}
    ret["deviceGroundTruthData"] = []
    ret["deviceGroundTruthData"].append({})
    idx = 0
    capture_id = capture_meta_data["capture_id"]
    workflow_id = capture_meta_data["workflow_id"]  # noqa: F841 (kept for parity)
    input_file_path = ""
    if capture_meta_data["capture_folder"] and capture_meta_data["capture_id"]:
        input_file_path = os.path.join(
            capture_meta_data["capture_folder"], f"{capture_id}.jpg"
        )
        ret["deviceGroundTruthData"][idx]["source-ref"] = os.path.join(
            "file:/", input_file_path
        )

    class_name = ""
    if inference_output:
        ret["deviceGroundTruthData"][idx]["anomaly-label-detected"] = 1
        class_name = "Anomaly"
    else:
        ret["deviceGroundTruthData"][idx]["anomaly-label-detected"] = 0
        class_name = "Normal"
    label_detected_metadata = {}
    label_detected_metadata["class-name"] = class_name
    label_detected_metadata["creation-date"] = time_str
    label_detected_metadata["human-annotated"] = "no"
    label_detected_metadata["type"] = "groundtruth/image-classification"
    label_detected_metadata["confidence"] = inference_confidence.astype(float)
    ret["deviceGroundTruthData"][idx][
        "anomaly-label-detected-metadata"
    ] = label_detected_metadata
    mask_file_path = ""
    if instance._has_anomaly_mask(inference_mask, input_image):
        mask_file_path = os.path.join(
            capture_meta_data["capture_folder"], f"{capture_id}.mask.png"
        )
        ret["deviceGroundTruthData"][idx]["anomaly-mask-ref-detected"] = os.path.join(
            "file:/", mask_file_path
        )
        anomaly_mask_ref_detected_meta = {}
        d = {}
        for i in range(len(inference_anomalies)):
            detail = {}
            detail["name"] = inference_anomalies[i]["name"]
            detail["hex-color"] = inference_anomalies[i]["hex_color"].lower()
            detail["total-percentage-area"] = inference_anomalies[i][
                "total_percentage_area"
            ]
            d[str(i)] = detail
        anomaly_mask_ref_detected_meta["internal-color-map"] = d
        anomaly_mask_ref_detected_meta["creation-date"] = time_str
        anomaly_mask_ref_detected_meta["human-annotated"] = "no"
        anomaly_mask_ref_detected_meta["type"] = "groundtruth/semantic-segmentation"
        anomaly_mask_ref_detected_meta["job-name"] = "labeling-job/segmentation-job"
        ret["deviceGroundTruthData"][idx][
            "anomaly-mask-ref-detected-metadata"
        ] = anomaly_mask_ref_detected_meta
    # auxiliary data
    ret["deviceFleetAuxiliaryInputs"] = []
    ret["deviceFleetAuxiliaryOutputs"] = []

    if input_file_path:
        ret["deviceFleetAuxiliaryInputs"].append(
            {
                "data-ref": f"file://{input_file_path}",
                "encoding": "NONE",
                "observedContentType": "jpg",
            }
        )
    if mask_file_path:
        ret["deviceFleetAuxiliaryOutputs"].append(
            {
                "data-ref": f"file://{mask_file_path}",
                "encoding": "NONE",
                "observedContentType": "mask.png",
            }
        )
    # LEGACY overlay behavior: the overlay data-ref was emitted ONLY when an
    # anomaly mask was present.
    if instance._has_anomaly_mask(inference_mask, input_image):
        overlay_file_path = os.path.join(
            capture_meta_data["capture_folder"], f"{capture_id}.overlay.jpg"
        )
        if overlay_file_path:
            ret["deviceFleetAuxiliaryOutputs"].append(
                {
                    "data-ref": f"file://{overlay_file_path}",
                    "encoding": "NONE",
                    "observedContentType": "overlay.jpg",
                }
            )
    # inference result
    inf_result = {}
    inf_result["Inference status"] = "success"
    if inference_output:
        inf_result["Inference result"] = "Anomaly"
    else:
        inf_result["Inference result"] = "Normal"
    inf_result["Confidence"] = inference_confidence.astype(float)
    inf_result["Anomaly_score"] = inference_score.astype(float)
    inf_result["Anomaly_threshold"] = 1.0
    inf_result["Error msg"] = ""
    inf_result_str = json.dumps(inf_result)
    inf_result_str_encoded = base64.b64encode(inf_result_str.encode()).decode()
    ret["deviceFleetAuxiliaryOutputs"].append(
        {
            "data": inf_result_str_encoded,
            "encoding": "BASE64",
            "observedContentType": "json",
        }
    )
    # anomaly list
    anomalies = inference_anomalies
    d = {}
    for i, anomaly in enumerate(anomalies):
        detail = {
            "class-name": anomaly["name"],
            "hex-color": anomaly["hex_color"].lower(),
            "total-percentage-area": anomaly["total_percentage_area"],
        }
        d[str(i)] = detail

    if anomalies:
        anomaly_data = {"anomalies": d}
        anomaly_str = json.dumps(anomaly_data)
        anomaly_str_encoded = base64.b64encode(anomaly_str.encode()).decode()
        ret["deviceFleetAuxiliaryOutputs"].append(
            {
                "data": anomaly_str_encoded,
                "encoding": "BASE64",
                "observedContentType": "json_with_base64_encoding",
            }
        )

    # meta data
    ret["eventMetadata"] = {
        "capture_folder": capture_meta_data.get("capture_folder", ""),
        "eventId": capture_meta_data.get("event_id", ""),
        "deviceFleetName": capture_meta_data.get("device_fleet_name", ""),
        "modelName": instance.model_name,
        "modelVersion": instance.model_version,
        "inferenceTime": time_str,
    }
    ret["eventVersion"] = "0"
    return ret


# --------------------------------------------------------------------------- #
# Hypothesis strategies for anomaly (NON-detection) inputs.
# --------------------------------------------------------------------------- #

# Finite floats only: NaN would make numpy-scalar dict equality (Confidence /
# total-percentage-area) spuriously fail (NaN != NaN).
_finite_floats = st.floats(
    allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6, width=32
)

_hex_color = st.text(alphabet="0123456789ABCDEFabcdef#", min_size=1, max_size=7)
_name_text = st.text(max_size=20)

# An anomaly/segmentation entry: carries the segmentation keys the anomaly path
# reads and, crucially, NO ``bounding_box`` field (so it is never mistaken for a
# detection payload).
_anomaly_entry = st.fixed_dictionaries(
    {
        "name": _name_text,
        "hex_color": _hex_color,
        "total_percentage_area": _finite_floats,
    }
)

_anomaly_list = st.lists(_anomaly_entry, min_size=0, max_size=4)

# Identifier-ish strings (allow empty to exercise the missing-path branch).
_id_text = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=0, max_size=12
)

# np scalar inference_output; 0 -> Normal, non-zero -> Anomaly.
_inference_output = st.integers(min_value=0, max_value=3).map(np.int64)

_mask_mode = st.sampled_from(["mask", "zeros", "empty"])
_image_dim = st.integers(min_value=2, max_value=12)


@st.composite
def _anomaly_case(draw):
    """Generate a full anomaly (non-detection) invocation of the metadata fn."""
    h = draw(_image_dim)
    w = draw(_image_dim)
    input_image = np.zeros((h, w, 3), dtype=np.uint8)

    mode = draw(_mask_mode)
    if mode == "mask":
        # Non-zero mask of the SAME shape -> _has_anomaly_mask True.
        inference_mask = np.zeros((h, w, 3), dtype=np.uint8)
        inference_mask[0, 0, 0] = draw(st.integers(min_value=1, max_value=255))
    elif mode == "zeros":
        # Same shape but all-zero -> np.any False -> no mask.
        inference_mask = np.zeros((h, w, 3), dtype=np.uint8)
    else:  # "empty"
        # Shape mismatch -> no mask regardless of content.
        inference_mask = np.array([], dtype=np.uint8)

    capture_meta_data = {
        "capture_id": draw(_id_text),
        "workflow_id": draw(_id_text),
        "capture_folder": draw(_id_text),
        "event_id": draw(_id_text),
        "device_fleet_name": draw(_id_text),
    }

    return {
        "capture_meta_data": capture_meta_data,
        "inference_output": draw(_inference_output),
        "time_str": draw(st.text(max_size=25)),
        "inference_confidence": np.float32(draw(_finite_floats)),
        "inference_mask": inference_mask,
        "inference_anomalies": draw(_anomaly_list),
        "inference_score": np.float32(draw(_finite_floats)),
        "input_image": input_image,
    }


def _run_current(instance, case):
    return instance._generate_capture_meta_data(
        capture_meta_data=case["capture_meta_data"],
        inference_output=case["inference_output"],
        time_str=case["time_str"],
        inference_confidence=case["inference_confidence"],
        inference_mask=case["inference_mask"],
        inference_anomalies=case["inference_anomalies"],
        inference_score=case["inference_score"],
        input_image=case["input_image"],
    )


def _run_legacy(instance, case):
    return _legacy_generate_capture_meta_data(
        instance,
        capture_meta_data=case["capture_meta_data"],
        inference_output=case["inference_output"],
        time_str=case["time_str"],
        inference_confidence=case["inference_confidence"],
        inference_mask=case["inference_mask"],
        inference_anomalies=case["inference_anomalies"],
        inference_score=case["inference_score"],
        input_image=case["input_image"],
    )


# Feature: object-detection-visualization, Property 7: Anomaly-classification behavior is unchanged
# Validates: Requirements 1.7, 2.6, 5.3, 5.4
@settings(max_examples=200)
@given(case=_anomaly_case())
def test_anomaly_metadata_matches_legacy_baseline(case):
    """For any payload with no ``bounding_box`` field, the current Marshal
    metadata (incl. the mask-based overlay ref) equals the frozen legacy
    baseline."""
    instance = _make_marshal_instance()

    # Precondition: this is genuinely an anomaly payload (no bounding_box).
    assert not TritonPythonModel._is_detection_list(case["inference_anomalies"])

    current = _run_current(instance, case)
    legacy = _run_legacy(instance, case)

    assert current == legacy
    # The anomaly path must never introduce detection-only keys.
    inf_result = _decode_inf_result(current)
    assert inf_result["Inference result"] in ("Anomaly", "Normal")
    assert "Detection_count" not in inf_result


def _decode_inf_result(meta):
    """Pull and decode the base64 ``inference result`` JSON blob from metadata."""
    for aux in meta["deviceFleetAuxiliaryOutputs"]:
        if aux.get("encoding") == "BASE64" and aux.get("observedContentType") == "json":
            return json.loads(base64.b64decode(aux["data"]).decode())
    raise AssertionError("no inference-result block found")


# --------------------------------------------------------------------------- #
# Deterministic example / edge tests covering each combination explicitly.
# --------------------------------------------------------------------------- #


def _base_case(**overrides):
    case = {
        "capture_meta_data": {
            "capture_id": "cap1",
            "workflow_id": "wf1",
            "capture_folder": "/tmp/captures",
            "event_id": "cap1",
            "device_fleet_name": "fleet1",
        },
        "inference_output": np.int64(0),
        "time_str": "2025-01-01T00:00:00",
        "inference_confidence": np.float32(0.42),
        "inference_mask": np.array([], dtype=np.uint8),
        "inference_anomalies": [],
        "inference_score": np.float32(0.42),
        "input_image": np.zeros((8, 8, 3), dtype=np.uint8),
    }
    case.update(overrides)
    return case


def test_normal_no_mask_matches_legacy():
    instance = _make_marshal_instance()
    case = _base_case(inference_output=np.int64(0))
    current = _run_current(instance, case)
    assert current == _run_legacy(instance, case)
    assert _decode_inf_result(current)["Inference result"] == "Normal"
    # No mask -> no overlay ref in the legacy (and current) anomaly path.
    assert not any(
        aux.get("observedContentType") == "overlay.jpg"
        for aux in current["deviceFleetAuxiliaryOutputs"]
    )


def test_anomalous_no_mask_matches_legacy():
    instance = _make_marshal_instance()
    case = _base_case(inference_output=np.int64(1))
    current = _run_current(instance, case)
    assert current == _run_legacy(instance, case)
    assert _decode_inf_result(current)["Inference result"] == "Anomaly"


def test_anomalous_with_mask_matches_legacy_and_has_overlay_ref():
    instance = _make_marshal_instance()
    mask = np.zeros((8, 8, 3), dtype=np.uint8)
    mask[0, 0, 0] = 200
    case = _base_case(
        inference_output=np.int64(1),
        inference_mask=mask,
        inference_anomalies=[
            {"name": "scratch", "hex_color": "#AABBCC", "total_percentage_area": 12.5}
        ],
    )
    current = _run_current(instance, case)
    assert current == _run_legacy(instance, case)
    # Mask present -> mask ref AND overlay ref emitted (matching legacy).
    content_types = [
        aux.get("observedContentType")
        for aux in current["deviceFleetAuxiliaryOutputs"]
    ]
    assert "mask.png" in content_types
    assert "overlay.jpg" in content_types
    # Segmentation metadata block is present.
    gt = current["deviceGroundTruthData"][0]
    assert "anomaly-mask-ref-detected" in gt
    assert gt["anomaly-mask-ref-detected-metadata"]["internal-color-map"]["0"][
        "hex-color"
    ] == "#aabbcc"


def test_empty_capture_folder_omits_source_ref():
    instance = _make_marshal_instance()
    case = _base_case(
        capture_meta_data={
            "capture_id": "",
            "workflow_id": "wf1",
            "capture_folder": "",
            "event_id": "",
            "device_fleet_name": "fleet1",
        }
    )
    current = _run_current(instance, case)
    assert current == _run_legacy(instance, case)
    assert "source-ref" not in current["deviceGroundTruthData"][0]
    assert current["deviceFleetAuxiliaryInputs"] == []
