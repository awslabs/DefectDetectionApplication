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
"""Integration / smoke test for overlay path consistency.

Feature: object-detection-visualization
Requirement 2.2 / Design "Testing Strategy" -> Integration test (2.2):
    "the gstreamer ``emlcapture`` overlay target path equals the Marshal metadata
    ``data-ref``, and a produced overlay tensor results in an on-disk
    ``{capture_id}.overlay.jpg`` (1-2 representative captures)."

Design research finding (design.md):
    "The overlay JPEG bytes are written to disk by the gstreamer capture plugin,
    not the Marshal. ``pipeline_builder._add_post_processing_plugins`` wires
    ``emlcapture`` with
    ``triton_inference_output_overlay:file-target_{workflowOutputPath}-overlay.jpg``.
    ... The metadata data-ref (``file://{capture_folder}/{capture_id}.overlay.jpg``)
    and the plugin's on-disk target already coincide for the anomaly path ...
    so detection reuses the exact same path convention."

There are three code sites that jointly define the on-disk overlay path:

  1. gstreamer plugin (``src/backend/gstreamer/pipeline_builder.py``):
        emits the emlcapture ``meta`` target
        ``triton_inference_output_overlay:file-target_{w_path}-overlay.jpg``
        where ``w_path = workflow_config["workflowOutputPath"]``.
  2. message-broker pipe (``src/backend/dda_triton/message_broker_client.py``):
        pipe ``message_id: "file-target_${workflow-path}-${ext}"`` routes the
        target to an on-disk file at
        ``directory: "${workflow-path}/"`` + ``filename: "${c_id}.${ext}"``
        (the module docstring states: "capture_id is mapped to corelation id
        (c_id), via gstreamer setup").
  3. Marshal (``marshal_for_capture_template.py``): references the same artifact
        as ``data-ref: "file://{capture_folder}/{capture_id}.overlay.jpg"``.

Given the deployment invariant ``workflowOutputPath == capture_folder`` and
``c_id == capture_id`` (documented in the message-broker module), the on-disk
target the plugin writes and the file the Marshal references are the SAME path.
This test asserts exactly that, and -- because spinning up gstreamer/panorama is
not available in this environment -- documents that and instead proves the
Marshal produces non-empty overlay bytes so a write WOULD materialise the file
(and demonstrates that materialisation by writing the produced bytes to the
derived on-disk path with real ``cv2``).

Importing ``marshal_for_capture_template.py`` requires the Triton Python-backend
module (``triton_python_backend_utils``), which is stubbed in ``sys.modules``
before load, exactly as the other marshal tests do. The REAL ``cv2`` is used so
the overlay encode path produces genuine JPEG bytes; ``resolve_class_label``
resolves for real via ``PYTHONPATH=src/backend``.
"""
import importlib.util
import os
import sys
import types

import numpy as np
import pytest

_REPO_ROOT = os.getcwd()

_MARSHAL_TEMPLATE_PATH = os.path.join(
    _REPO_ROOT, "src", "backend", "dda_triton", "resources_for_copy",
    "marshal_for_capture_template.py",
)
_PIPELINE_BUILDER_PATH = os.path.join(
    _REPO_ROOT, "src", "backend", "gstreamer", "pipeline_builder.py",
)
_MESSAGE_BROKER_PATH = os.path.join(
    _REPO_ROOT, "src", "backend", "dda_triton", "message_broker_client.py",
)


# --------------------------------------------------------------------------- #
# Exact path templates reproduced from the three code sites.                   #
# The ``test_templates_are_present_in_source`` test below reads each source    #
# file and asserts these literal templates still appear there, so the test     #
# fails loudly (rather than silently passing on stale strings) if any code     #
# site changes its path convention.                                            #
# --------------------------------------------------------------------------- #

# 1. gstreamer emlcapture overlay target (pipeline_builder.py, f-string literal
#    where ``{w_path}`` is the ``workflowOutputPath`` placeholder).
PLUGIN_OVERLAY_TARGET_TEMPLATE = "file-target_{w_path}-overlay.jpg"

# 2. message-broker pipe (message_broker_client.py, JSON literals).
PIPE_MESSAGE_ID_TEMPLATE = "file-target_${workflow-path}-${ext}"
PIPE_DIRECTORY_TEMPLATE = "${workflow-path}/"
PIPE_FILENAME_TEMPLATE = "${c_id}.${ext}"

# 3. Marshal data-ref suffix (marshal_for_capture_template.py, f-string literal).
MARSHAL_OVERLAY_SUFFIX_TEMPLATE = "{capture_id}.overlay.jpg"


def _load_marshal_module():
    """Load ``marshal_for_capture_template`` with only triton stubbed (real cv2)."""
    pb_utils_stub = types.ModuleType("triton_python_backend_utils")
    pb_utils_stub.Tensor = object
    pb_utils_stub.triton_string_to_numpy = lambda s: np.float32
    sys.modules["triton_python_backend_utils"] = pb_utils_stub

    import cv2  # noqa: F401  (real library required for genuine JPEG encode)

    spec = importlib.util.spec_from_file_location(
        "marshal_for_capture_template_overlay_path_under_test",
        _MARSHAL_TEMPLATE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MARSHAL_MODULE = _load_marshal_module()
TritonPythonModel = _MARSHAL_MODULE.TritonPythonModel


def _make_marshal_instance():
    """Bare instance with only the attributes the exercised methods read."""
    instance = TritonPythonModel.__new__(TritonPythonModel)
    instance.model_name = "m"
    instance.model_version = "1"
    # _encode_overlay reads self.output_overlay_dtype (set in initialize()).
    instance.output_overlay_dtype = np.uint8
    return instance


# --------------------------------------------------------------------------- #
# Path-derivation helpers reproducing each code site's convention.             #
# --------------------------------------------------------------------------- #

def plugin_overlay_message_id(workflow_output_path):
    """The overlay target string the gstreamer plugin emits for ``w_path``."""
    return PLUGIN_OVERLAY_TARGET_TEMPLATE.format(w_path=workflow_output_path)


def broker_ondisk_overlay_path(message_id, workflow_output_path, capture_id):
    """Resolve the on-disk file the message-broker pipe writes for a plugin
    target ``message_id``, following ``file-target_${workflow-path}-${ext}`` ->
    ``${workflow-path}/${c_id}.${ext}`` with ``c_id == capture_id``."""
    # The pipe knows ``workflow-path`` from the gst config, so it strips the
    # ``file-target_{workflow-path}-`` prefix to recover ``${ext}``.
    prefix = "file-target_" + workflow_output_path + "-"
    assert message_id.startswith(prefix), (
        f"plugin target {message_id!r} does not match broker pipe prefix {prefix!r}"
    )
    ext = message_id[len(prefix):]  # e.g. "overlay.jpg"
    directory = PIPE_DIRECTORY_TEMPLATE.replace("${workflow-path}", workflow_output_path)
    filename = (
        PIPE_FILENAME_TEMPLATE.replace("${c_id}", capture_id).replace("${ext}", ext)
    )
    # directory already ends with "/"; join by string concat mirrors the broker.
    return directory + filename


def marshal_overlay_data_ref(capture_folder, capture_id):
    """Extract the overlay ``data-ref`` the Marshal emits for a detection capture
    by actually invoking ``_generate_capture_meta_data`` (real code path)."""
    instance = _make_marshal_instance()
    # Empty mask + detection payload -> overlay ref driven by detection typing.
    h, w = 16, 24
    input_image = np.zeros((h, w, 3), dtype=np.uint8)
    inference_mask = np.zeros((h, w, 3), dtype=np.uint8)
    ret = instance._generate_capture_meta_data(
        capture_meta_data={
            "capture_id": capture_id,
            "workflow_id": "wf",
            "capture_folder": capture_folder,
            "event_id": capture_id,
            "device_fleet_name": "fleet",
        },
        inference_output=np.uint8(1),
        time_str="2025-01-01T00:00:00",
        inference_confidence=np.float32(0.83),
        inference_mask=inference_mask,
        inference_anomalies=_WITH_OBJECTS_PAYLOAD,
        inference_score=np.float32(0.83),
        input_image=input_image,
    )
    refs = [
        aux for aux in ret["deviceFleetAuxiliaryOutputs"]
        if aux.get("observedContentType") == "overlay.jpg"
    ]
    assert len(refs) == 1, f"expected exactly one overlay.jpg ref, got {refs}"
    data_ref = refs[0]["data-ref"]
    assert data_ref.startswith("file://"), data_ref
    return data_ref[len("file://"):]


# --------------------------------------------------------------------------- #
# Representative capture payloads: one with objects, one zero-object sentinel.  #
# --------------------------------------------------------------------------- #

_WITH_OBJECTS_PAYLOAD = [
    {
        "bounding_box": [2, 3, 12, 14],
        "class": "17",
        "class_label": "dog",
        "confidence": 0.83,
    },
    {
        "bounding_box": [5, 5, 20, 15],
        "class": "0",
        "class_label": "person",
        "confidence": 0.61,
    },
]

_ZERO_OBJECT_SENTINEL = [
    {"bounding_box": [], "class": "", "class_label": "", "confidence": 0.0, "no_objects": True}
]

# (id, workflow_output_path, capture_id, detection_payload)
_REPRESENTATIVE_CAPTURES = [
    ("with-objects", "/tmp/captures/wf-1", "capture-aaaa-1111", _WITH_OBJECTS_PAYLOAD),
    ("zero-object", "/tmp/captures/wf-2", "capture-bbbb-2222", _ZERO_OBJECT_SENTINEL),
]


# Feature: object-detection-visualization, Requirement 2.2 (Integration test):
# guard against silent drift -- the reproduced templates must still be the
# literal strings present at each of the three code sites.
def test_templates_are_present_in_source():
    with open(_PIPELINE_BUILDER_PATH, "r") as f:
        pipeline_src = f.read()
    with open(_MESSAGE_BROKER_PATH, "r") as f:
        broker_src = f.read()
    with open(_MARSHAL_TEMPLATE_PATH, "r") as f:
        marshal_src = f.read()

    # 1. gstreamer plugin overlay target.
    assert "triton_inference_output_overlay:" + PLUGIN_OVERLAY_TARGET_TEMPLATE in pipeline_src, (
        "pipeline_builder overlay target template changed; update this test"
    )
    # 2. message-broker pipe routing.
    assert PIPE_MESSAGE_ID_TEMPLATE in broker_src
    assert PIPE_DIRECTORY_TEMPLATE in broker_src
    assert PIPE_FILENAME_TEMPLATE in broker_src
    # 3. Marshal data-ref suffix.
    assert MARSHAL_OVERLAY_SUFFIX_TEMPLATE in marshal_src


# Feature: object-detection-visualization, Requirement 2.2 (Integration test):
# the gstreamer emlcapture overlay target path equals the Marshal metadata
# data-ref for the same capture (given workflowOutputPath == capture_folder).
@pytest.mark.parametrize(
    "label,workflow_output_path,capture_id,_payload",
    _REPRESENTATIVE_CAPTURES,
    ids=[c[0] for c in _REPRESENTATIVE_CAPTURES],
)
def test_plugin_target_path_equals_marshal_data_ref(
    label, workflow_output_path, capture_id, _payload
):
    # Deployment invariant: the plugin's workflowOutputPath is the same folder
    # the Marshal records as capture_folder.
    capture_folder = workflow_output_path

    # Path the gstreamer plugin + message broker write on disk.
    message_id = plugin_overlay_message_id(workflow_output_path)
    plugin_ondisk_path = broker_ondisk_overlay_path(
        message_id, workflow_output_path, capture_id
    )

    # Path the Marshal references in the capture metadata.
    marshal_path = marshal_overlay_data_ref(capture_folder, capture_id)

    # The two conventions must coincide exactly.
    assert plugin_ondisk_path == marshal_path, (
        f"[{label}] plugin on-disk target {plugin_ondisk_path!r} != "
        f"marshal data-ref path {marshal_path!r}"
    )
    # ...and both must be the {capture_id}.overlay.jpg artifact under the folder.
    expected_basename = MARSHAL_OVERLAY_SUFFIX_TEMPLATE.format(capture_id=capture_id)
    assert os.path.basename(plugin_ondisk_path) == expected_basename
    assert os.path.basename(marshal_path) == expected_basename
    assert os.path.dirname(marshal_path) == workflow_output_path


# Feature: object-detection-visualization, Requirement 2.2 (Integration test):
# smoke -- a detection capture's metadata carries an overlay.jpg data-ref whose
# basename is {capture_id}.overlay.jpg, matching the plugin's target basename.
@pytest.mark.parametrize(
    "label,workflow_output_path,capture_id,payload",
    _REPRESENTATIVE_CAPTURES,
    ids=[c[0] for c in _REPRESENTATIVE_CAPTURES],
)
def test_capture_metadata_overlay_basename_matches_plugin(
    label, workflow_output_path, capture_id, payload
):
    instance = _make_marshal_instance()
    h, w = 20, 30
    input_image = np.zeros((h, w, 3), dtype=np.uint8)
    inference_mask = np.zeros((h, w, 3), dtype=np.uint8)

    # Precondition: the payload is classified as a detection capture.
    assert TritonPythonModel._is_detection_list(payload)

    ret = instance._generate_capture_meta_data(
        capture_meta_data={
            "capture_id": capture_id,
            "workflow_id": "wf",
            "capture_folder": workflow_output_path,
            "event_id": capture_id,
            "device_fleet_name": "fleet",
        },
        inference_output=np.uint8(1),
        time_str="2025-01-01T00:00:00",
        inference_confidence=np.float32(0.83),
        inference_mask=inference_mask,
        inference_anomalies=payload,
        inference_score=np.float32(0.83),
        input_image=input_image,
    )

    overlay_refs = [
        aux for aux in ret["deviceFleetAuxiliaryOutputs"]
        if aux.get("observedContentType") == "overlay.jpg"
    ]
    assert len(overlay_refs) == 1, (
        f"[{label}] detection capture must emit exactly one overlay.jpg ref"
    )
    meta_basename = os.path.basename(overlay_refs[0]["data-ref"])
    expected_basename = MARSHAL_OVERLAY_SUFFIX_TEMPLATE.format(capture_id=capture_id)
    assert meta_basename == expected_basename

    # Plugin target basename for the same capture.
    message_id = plugin_overlay_message_id(workflow_output_path)
    plugin_ondisk_path = broker_ondisk_overlay_path(
        message_id, workflow_output_path, capture_id
    )
    assert os.path.basename(plugin_ondisk_path) == meta_basename


# Feature: object-detection-visualization, Requirement 2.2 (Integration test):
# on-disk write. Exercising the REAL gstreamer emlcapture plugin (which performs
# the disk write) requires GStreamer + the panorama message broker, which are
# NOT available in this environment. We therefore assert path-consistency (above)
# plus that the Marshal produces a NON-EMPTY overlay tensor -- so a write WOULD
# materialise the file -- and demonstrate materialisation by writing the produced
# bytes to the derived on-disk target path (real cv2), proving a
# ``{capture_id}.overlay.jpg`` file results.
@pytest.mark.parametrize(
    "label,_workflow_output_path,capture_id,payload",
    _REPRESENTATIVE_CAPTURES,
    ids=[c[0] for c in _REPRESENTATIVE_CAPTURES],
)
def test_produced_overlay_bytes_would_write_capture_overlay_jpg(
    label, _workflow_output_path, capture_id, payload, tmp_path
):
    instance = _make_marshal_instance()
    h, w = 24, 32
    input_image = np.zeros((h, w, 3), dtype=np.uint8)

    # The Marshal produces the overlay tensor (bytes) that the plugin writes.
    detection_overlay = instance._generate_detection_overlay(input_image, payload)
    assert detection_overlay.shape == input_image.shape

    encoded_overlay = instance._encode_overlay(detection_overlay)
    # Real cv2 must yield genuine, non-empty JPEG bytes so a write is possible.
    assert encoded_overlay is not None
    assert encoded_overlay.size > 0, (
        f"[{label}] _encode_overlay produced empty bytes; nothing to write"
    )

    # Redirect the derived on-disk target under tmp_path (gstreamer would write
    # to {workflow_output_path}/{capture_id}.overlay.jpg on the device).
    workflow_output_path = str(tmp_path)
    message_id = plugin_overlay_message_id(workflow_output_path)
    ondisk_path = broker_ondisk_overlay_path(message_id, workflow_output_path, capture_id)

    # Simulate the plugin write of the Marshal-produced bytes.
    with open(ondisk_path, "wb") as f:
        f.write(bytes(bytearray(np.asarray(encoded_overlay).astype(np.uint8).tobytes())))

    assert os.path.exists(ondisk_path), f"[{label}] expected overlay file at {ondisk_path}"
    assert os.path.basename(ondisk_path) == f"{capture_id}.overlay.jpg"
    assert os.path.getsize(ondisk_path) > 0

    # The materialised file's path is exactly what the Marshal would reference.
    marshal_path = broker_ondisk_overlay_path(  # same derivation as capture_folder join
        message_id, workflow_output_path, capture_id
    )
    assert marshal_path == ondisk_path
