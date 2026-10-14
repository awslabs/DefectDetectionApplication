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
"""Preservation property tests for detection result persistence (Defect B).

Spec: ``static-camera-pixel-format-and-detection-results`` -- **Property 4:
Preservation Checking**, Defect B.

**Validates: Requirements 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15, 3.16, 3.17,
3.18, 3.19**

THESE TESTS MUST PASS ON UNFIXED CODE. They were written observation-first: the
UNFIXED code was run, its actual outputs recorded, and those recorded outputs
are what is asserted here. They pin the behavior the Defect B fix must NOT
disturb, and task 6.6 re-runs them unchanged after the fix.

What the fix is allowed to change, and how that is accommodated
---------------------------------------------------------------
The fix widens the STORED prediction vocabulary by exactly ONE value,
``"Detection"``, applied only to ``InferenceResultSchema.prediction``
(bugfix.md 2.6, 2.7). So:

* the accepted prediction set grows from ``{Normal, Anomaly}`` to
  ``{Normal, Anomaly, Detection}`` and NOTHING else moves -- asserted as sets,
  not as marshmallow's rendered message, because that message legitimately
  grows a member. Recorded today:
  ``{'prediction': ['Must be one of: Normal, Anomaly.']}``.
* the summary grows an ADDITIVE ``detection`` bucket (bugfix.md 2.9, 3.15), so
  the summary assertions allow that one extra key and require it to be ``0``
  for windows holding no Detection rows, while ``normal`` / ``anomaly`` /
  ``totalInference`` keep exactly today's values.

Every other vocabulary stays byte-for-byte: ``humanClassification`` on BOTH
``InferenceResultSchema`` and ``CapturedDataSchema`` keeps rendering
``Must be one of: Normal, Anomaly.`` (Requirement 3.13), ``captureType`` keeps
rendering ``Must be one of: Capture, Inference.``, and the download-file
prediction check keeps reading ``constants.PREDICTION`` (Requirement 2.7).

Recorded baselines (observed in the flask-app x86 container on unfixed code)
---------------------------------------------------------------------------
* ``InferenceResultSchema().load()`` requires exactly ``captureId``,
  ``captureType``, ``workflowId``, ``inferenceCreationTime``, ``prediction``,
  ``confidence``, ``anomalyScore``, ``anomalyThreshod``, ``inputImageFilePath``,
  ``modelId``; everything else is optional. ``dump()`` always emits all 22
  fields, filling omitted ones from ``InferenceResult.__init__`` defaults
  (``maskImage`` ``''``, ``maskBackground`` / ``anomalyLabels`` /
  ``humanClassification`` / ``textNote`` ``None``, ``humanReviewRequired``
  ``False``, ``modelConfidenceThresholds`` ``{}``). Unknown keys are REJECTED
  (``{'detections': ['Unknown field.']}``), which is why the marshal's
  ``detections`` / ``detection_count`` are not stored columns.
* ``GetInferenceResults.save_image_object`` -- three branches, recorded
  key-for-key AND in key order: detection surfaces the OVERLAY as
  ``imageDataFilePath`` and carries ``detections`` + ``detection_count``;
  segmentation surfaces ``out.jpg`` and carries ``mask_background`` (hex →
  rgb), ``mask_image`` and ``anomalies`` (with the ``"0"`` background entry
  removed) only when a mask is present; classification surfaces the INPUT image
  and applies the ``temp_get_confidence`` rule (``1 - anomaly_score`` for a
  Normal capture whose anomaly score is close to its confidence).
* branch predicates: ``is_detection_model_output_result`` is True iff an
  ``overlay.jpg`` output exists AND the ``json_with_base64_encoding`` block
  decodes to a dict carrying ``detections``;
  ``is_segmentation_model_output_result`` is True iff an ``out.jpg`` output or
  any ``mask*`` output exists.
* ``convert_inference_res_to_save_in_db`` -- recorded field-for-field including
  the ``None``-stripping and the ``get_default_configs_lfv``
  ``ResourceNotFoundError`` fallback (``modelAlias`` = the model id,
  ``modelConfidenceThresholds`` = ``{}``).
* ``get_inference_result_summary`` -- ``{"totalInference", "normal",
  "anomaly"}`` with ``totalInference == normal + anomaly``, window boundary
  INCLUSIVE (``inferenceCreationTime >= summaryStartTime``); envelope
  ``{"stats", "lastResetTime"}``.
* ``GET /workflows/{id}/results`` -- envelope ``{total, page, size, results}``,
  20-field result items, ``inferenceCreationTime`` DESCENDING, every filter as
  recorded below; ``prediction=Detection`` and an out-of-vocabulary
  ``captureType`` both answer 400.
* manual run -- full mode returns the marshalled result plus a float
  ``processingTime`` (minus ``image`` when ``returnImageString`` is false);
  ``returnPartialResultsEarly`` returns exactly
  ``{"captureId", "inferenceResult": {"confidence", "inference_result"}}`` in
  both ``returnImageString`` modes; a persistence failure inside the background
  task leaves the response at 200 with an unchanged payload (Requirement 3.17).
* ``generate_smgt_format_manifest`` and the download-file prediction check --
  ``Normal`` / ``Anomaly`` prefix the exported zip name, anything else does not.

The ORM enum (the finding task 6 must handle)
---------------------------------------------
``dao/sqlite_db/models.py`` declares
``prediction = Column(Enum(ANOMALY, NORMAL, name="enum_prediction_type"))`` with
LITERAL members. On SQLAlchemy 2.0.21 an unlisted value INSERTs and counts in
SQL but ``Enum._object_value_for_elem`` raises
``LookupError: 'Detection' is not among the defined enum values`` on READBACK,
so task 6 must widen that enum too. ``TestOrmEnumReadbackUnchanged`` pins what
must survive that widening: Normal and Anomaly still store and read back
identically, and the emitted sqlite DDL for the column carries NO CHECK
constraint (observed: ``prediction VARCHAR(7)``), so the widening needs no
migration -- Requirement 3.19's "no database schema migration" holds. The
recorded VARCHAR LENGTH is deliberately not pinned: it is derived from the
longest enum member and sqlite ignores it.

Frontend (Requirement 3.18), deliberately not a new test framework
------------------------------------------------------------------
Normal / Anomaly must keep rendering identically on the live card and the
history page. The device HMI has no jest / vitest suites, so
``test_normal_and_anomaly_history_rendering_is_unchanged`` pins the Normal
branch of ``ClassificationTypeTag`` at the SOURCE level only; task 8 verifies
the rendering on device.

Existing suites that are THEMSELVES preservation coverage
---------------------------------------------------------
These must keep passing untouched -- do not edit them to accommodate the fix:
``test/backend-test/utils/test_inference_results_utils.py``,
``test/backend-test/resources/test_inference_result_accessor.py``,
``test/backend-test/api-endpoints/test_inference_result_api.py``,
``test_marshal_payload_discrimination.py``, ``test_marshal_detections_block.py``,
``test_marshal_detection_count_confidence.py``,
``test_marshal_detection_typing.py``,
``test_marshal_anomaly_backward_compat.py``, and
``test_lfv_detection_tensor_set.py``.

Conventions
-----------
* **hypothesis** (not fast-check), run in the flask-app x86 container. Root
  ``conftest.py`` profiles: ``fast`` = 25 examples, ``HYPOTHESIS_PROFILE=ci`` =
  100. ``LocalServerBaseTestCase`` is the harness for anything needing the app,
  the metadata database or the TestClient;
  ``suppress_health_check=[HealthCheck.function_scoped_fixture]`` because
  ``setUp`` runs once per test method and is reused across hypothesis examples.
  Schema-only and predicate-only properties are plain module-level tests.
* **Container invocation.** FLIPPED interpreter order (``python3.10 ||
  python3.11``): ``flask-app:latest`` is the JP6-layout image, so the documented
  ``python3.11 || python3.10`` shim picks a dep-less 3.11.
  ``LD_LIBRARY_PATH`` must include ``/opt/tritonserver/lib`` or
  ``LocalServerBaseTestCase.setUp``'s ``from app import app`` dies on
  ``ImportError: libtritonserver.so``::

    docker run --rm -v "$(pwd)":/repo -w /repo \
      -e PYTHONPATH=/repo/src/backend:/repo/test/backend-test \
      -e LD_LIBRARY_PATH=/opt/tritonserver/lib:/usr/local/cuda/lib64 \
      flask-app:latest bash -lc 'PY=$(command -v python3.10 || command -v python3.11); \
        $PY -m pip install --no-cache-dir --quiet pytest hypothesis sarge testfixtures; \
        $PY -m pytest test/backend-test/utils/test_property_detection_result_preservation.py \
          -q -p no:cacheprovider'
"""
import base64
import json
import math
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from marshmallow import ValidationError
from sqlalchemy.orm import Session
from unittest.mock import patch

from local_server_base_test_case import LocalServerBaseTestCase
from model.inference_result import CapturedDataSchema, InferenceResultSchema
from utils import constants
# Imported at MODULE level on purpose, mirroring
# ``utils/test_inference_results_utils.py`` and the task 3 suite. The flask-app
# image also carries a baked-in copy of the backend at ``/`` whose
# ``dda_triton`` predates ``provider_visibility``; importing the repo's
# ``utils.*`` during collection pins the repo copies in ``sys.modules`` before
# anything resolves the image copy. A lazy in-test import instead fails with
# ``ModuleNotFoundError: No module named 'dda_triton.provider_visibility'``.
from utils.inference_results_utils import (
    GetInferenceResults,
    convert_inference_res_to_save_in_db,
    generate_smgt_format_manifest,
    is_detection_model_output_result,
    is_segmentation_model_output_result,
)

#: The single value the fix adds to the STORED prediction vocabulary. Kept as a
#: literal because ``constants.DETECTION`` does not exist on unfixed code. It is
#: excluded from the "still rejected" generators so those hold before AND after
#: the fix (bugfix.md 2.6 / 2.7).
DETECTION = "Detection"

#: Observed required fields of ``InferenceResultSchema``.
REQUIRED_FIELDS = (
    "captureId", "captureType", "workflowId", "inferenceCreationTime",
    "prediction", "confidence", "anomalyScore", "anomalyThreshod",
    "inputImageFilePath", "modelId",
)

#: Observed optional fields -- omitting any one of these still validates.
OPTIONAL_FIELDS = (
    "anomalyLabels", "maskImage", "maskBackground", "outputImageFilePath",
    "modelName", "flagForReview", "downloaded", "humanClassification",
    "textNote", "humanReviewRequired", "modelConfidenceThresholds",
)

#: Every field ``InferenceResultSchema().dump()`` emits, observed.
SCHEMA_FIELDS = frozenset(REQUIRED_FIELDS + OPTIONAL_FIELDS)

#: Values ``dump()`` emits for fields the input row omitted, observed
#: (``InferenceResult.__init__`` defaults).
LOADED_DEFAULTS = {
    "anomalyLabels": None,
    "maskImage": "",
    "maskBackground": None,
    "outputImageFilePath": "",
    "modelName": "",
    "flagForReview": False,
    "downloaded": False,
    "humanClassification": None,
    "textNote": None,
    "humanReviewRequired": False,
    "modelConfidenceThresholds": {},
}

#: Observed ``CapturedDataSchema().dump()`` fields.
CAPTURED_DATA_FIELDS = frozenset((
    "captureId", "captureType", "workflowId", "inferenceCreationTime",
    "inputImageFilePath", "flagForReview", "downloaded", "humanClassification",
    "textNote",
))

#: Observed ``GET /workflows/{id}/results`` envelope and item shape.
LIST_ENVELOPE_KEYS = frozenset(("total", "page", "size", "results"))
LIST_ITEM_FIELDS = frozenset((
    "anomalyLabels", "anomalyScore", "anomalyThreshod", "captureId",
    "captureType", "confidence", "downloaded", "flagForReview",
    "humanClassification", "humanReviewRequired", "inferenceCreationTime",
    "inputImageFilePath", "maskBackground", "maskImage", "modelId", "modelName",
    "outputImageFilePath", "prediction", "textNote", "workflowId",
))

#: Observed summary buckets. ``detection`` is the fix's ADDITIVE key.
SUMMARY_KEYS = frozenset(("totalInference", "normal", "anomaly"))

_MODEL_CONFIG = {
    "modelAlias": "cookie-model",
    "modelMetaData": "someMetadata",
    "modelVersion": "1",
    "modelConfidenceThresholds": {"AnomalyThreshold": "0.9", "NormalThreshold": "0.8"},
}

_COLORED_INFERENCE_BOX_PATH = os.path.join(
    os.getcwd(), "src", "frontend", "src", "components", "result-history",
    "ColoredInferenceBox.tsx",
)


# ---------------------------------------------------------------------------
# Generators -- constrained to the real input space
# ---------------------------------------------------------------------------

_predictions = st.sampled_from([constants.NORMAL, constants.ANOMALY])
_capture_ids = st.text(alphabet="0123456789abcdef", min_size=8, max_size=32)
_ids = st.text(alphabet="0123456789abcdefghijklmnopqrstuvwxyz-", min_size=4, max_size=24)
_paths = st.builds(
    lambda stem: "/aws_dda/image-capture/{}.jpg".format(stem),
    st.text(alphabet="0123456789abcdef", min_size=4, max_size=16),
)
# float32-representable so the value survives a JSON / sqlite round trip exactly.
_unit_floats = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False, width=32
)
_creation_times = st.integers(min_value=0, max_value=2_000_000_000)
_text_notes = st.text(max_size=constants.DB_TEXT_NOTE_MAX_LENGTH)
# Anything that is neither of today's two members nor the single value the fix
# adds: rejected before the fix and rejected after it.
_non_vocabulary = st.text(max_size=12).filter(
    lambda value: value not in (constants.NORMAL, constants.ANOMALY, DETECTION)
)


def _base_row(draw):
    return {
        "captureId": draw(_capture_ids),
        "captureType": draw(st.sampled_from(constants.CAPTURE_TYPE)),
        "workflowId": draw(_ids),
        "inferenceCreationTime": draw(_creation_times),
        "prediction": draw(_predictions),
        "confidence": draw(_unit_floats),
        "anomalyScore": draw(_unit_floats),
        "anomalyThreshod": draw(_unit_floats),
        "inputImageFilePath": draw(_paths),
        "outputImageFilePath": draw(_paths),
        "modelId": draw(_ids),
        "modelName": draw(_ids),
        "flagForReview": draw(st.booleans()),
        "downloaded": draw(st.booleans()),
        "humanReviewRequired": draw(st.booleans()),
        "modelConfidenceThresholds": {"AnomalyThreshold": 0.9, "NormalThreshold": 0.1},
    }


@st.composite
def _segmentation_rows(draw):
    """A stored segmentation row: ``prediction`` in {Normal, Anomaly} plus the
    mask / anomaly-label fields a segmentation capture carries."""
    row = _base_row(draw)
    row["maskImage"] = draw(st.text(alphabet="0123456789abcdef+/=", max_size=24))
    row["maskBackground"] = {
        "class-name": "background",
        "rgb-color": draw(st.lists(st.integers(min_value=0, max_value=255),
                                   min_size=3, max_size=3)),
        "total-percentage-area": draw(_unit_floats),
    }
    row["anomalyLabels"] = [
        {"class-name": draw(st.text(max_size=8)),
         "hex-color": "#23a436",
         "total-percentage-area": draw(_unit_floats)}
        for _ in range(draw(st.integers(min_value=0, max_value=3)))
    ]
    return row


@st.composite
def _classification_rows(draw):
    """A stored classification row: no mask fields at all."""
    return _base_row(draw)


def _expected_dump(row):
    """What ``dump(load(row))`` emitted on unfixed code: every schema field, the
    row's own values, omitted fields filled from the recorded defaults."""
    expected = dict(LOADED_DEFAULTS)
    expected.update(row)
    return expected


def _assert_load_and_dump_unchanged(row):
    schema = InferenceResultSchema()
    loaded = schema.load(row)
    assert loaded.prediction == row["prediction"]
    dumped = schema.dump(loaded)
    assert set(dumped) == SCHEMA_FIELDS, (
        "Requirement 3.9 / 3.10: the dumped stored-row shape changed. "
        "added={!r} removed={!r}".format(
            set(dumped) - SCHEMA_FIELDS, SCHEMA_FIELDS - set(dumped))
    )
    assert dumped == _expected_dump(row), (
        "Requirement 3.9 / 3.10: a non-detection row no longer round-trips "
        "unchanged.\n  row:      {!r}\n  dumped:   {!r}".format(row, dumped)
    )


# ---------------------------------------------------------------------------
# Requirements 3.9 / 3.10 / 3.13: the accepted and rejected row sets
# ---------------------------------------------------------------------------

# Property 4: validatesForStorage'(row) = validatesForStorage(row) and
#             storedRow'(X) = storedRow(X) for prediction != DETECTION
# Validates: Requirements 3.9
@settings(max_examples=25, deadline=None)
@given(row=_segmentation_rows())
def test_segmentation_row_validation_is_unchanged(row):
    """Every arbitrary segmentation row (Normal / Anomaly, ``anomalyScore``,
    ``anomalyThreshod``, ``maskImage``, ``maskBackground``, ``anomalyLabels``)
    validates and round-trips exactly as recorded on unfixed code.

    Validates: Requirements 3.9
    """
    _assert_load_and_dump_unchanged(row)


# Property 4: validatesForStorage'(row) = validatesForStorage(row)
# Validates: Requirements 3.10
@settings(max_examples=25, deadline=None)
@given(row=_classification_rows())
def test_classification_row_validation_is_unchanged(row):
    """Every arbitrary classification row (no mask fields) validates and
    round-trips exactly as recorded, with the mask fields filled from the
    recorded defaults rather than appearing or disappearing.

    Validates: Requirements 3.10
    """
    _assert_load_and_dump_unchanged(row)
    assert "maskImage" not in row
    schema = InferenceResultSchema()
    dumped = schema.dump(schema.load(row))
    assert dumped["maskImage"] == LOADED_DEFAULTS["maskImage"]
    assert dumped["maskBackground"] is None
    assert dumped["anomalyLabels"] is None


# Property 4: the REJECTED set is unchanged -- missing required fields
# Validates: Requirements 3.9, 3.10
@settings(max_examples=25, deadline=None)
@given(row=_classification_rows(), field=st.sampled_from(REQUIRED_FIELDS))
def test_missing_required_field_is_still_rejected(row, field):
    """A row missing any one of the recorded required fields -- including
    ``anomalyScore``, ``anomalyThreshod`` and ``confidence`` -- is still
    rejected, with the same message for the same field.

    Validates: Requirements 3.9, 3.10
    """
    incomplete = dict(row)
    del incomplete[field]
    with pytest.raises(ValidationError) as excinfo:
        InferenceResultSchema().load(incomplete)
    assert excinfo.value.messages == {field: ["Missing data for required field."]}, (
        "the rejection for a missing {!r} changed: {!r}".format(
            field, excinfo.value.messages)
    )


# Property 4: the ACCEPTED set is unchanged -- optional fields stay optional
# Validates: Requirements 3.9, 3.10
@settings(max_examples=25, deadline=None)
@given(row=_segmentation_rows(), field=st.sampled_from(OPTIONAL_FIELDS))
def test_optional_field_is_still_optional(row, field):
    """Omitting any recorded optional field still validates.

    Validates: Requirements 3.9, 3.10
    """
    trimmed = dict(row)
    trimmed.pop(field, None)
    InferenceResultSchema().load(trimmed)


# Property 4: the rejected prediction set is unchanged apart from DETECTION
# Validates: Requirements 3.9, 3.10
@settings(max_examples=25, deadline=None)
@given(row=_classification_rows(), prediction=_non_vocabulary)
def test_prediction_outside_the_vocabulary_is_still_rejected(row, prediction):
    """A ``prediction`` that is neither of today's two members nor the single
    value the fix adds stays rejected on the ``prediction`` field.

    Asserted as a SET rather than against marshmallow's rendered message: today
    the message is ``Must be one of: Normal, Anomaly.`` and the fix legitimately
    grows it by one member (bugfix.md 2.6). ``Normal`` and ``Anomaly``
    themselves stay accepted, which the two round-trip properties above cover.

    Validates: Requirements 3.9, 3.10
    """
    with pytest.raises(ValidationError) as excinfo:
        InferenceResultSchema().load(dict(row, prediction=prediction))
    assert set(excinfo.value.messages) == {"prediction"}, (
        "an out-of-vocabulary prediction must still be rejected on the "
        "'prediction' field alone; got {!r}".format(excinfo.value.messages)
    )


# Property 4: the captureType vocabulary is NOT widened
# Validates: Requirements 3.9, 3.19
@settings(max_examples=25, deadline=None)
@given(row=_classification_rows(),
       capture_type=st.text(max_size=12).filter(
           lambda value: value not in constants.CAPTURE_TYPE))
def test_capture_type_outside_capture_type_is_still_rejected(row, capture_type):
    """A ``captureType`` outside ``CAPTURE_TYPE`` is still rejected with exactly
    today's message -- this vocabulary is not touched by the fix.

    Validates: Requirements 3.9, 3.19
    """
    with pytest.raises(ValidationError) as excinfo:
        InferenceResultSchema().load(dict(row, captureType=capture_type))
    assert excinfo.value.messages == {
        "captureType": ["Must be one of: Capture, Inference."]
    }, "the captureType vocabulary changed: {!r}".format(excinfo.value.messages)


# Property 4: the textNote length boundary is unchanged
# Validates: Requirements 3.9
def test_text_note_length_boundary_is_unchanged():
    """``textNote`` accepts exactly ``DB_TEXT_NOTE_MAX_LENGTH`` characters and
    rejects one more, with today's message.

    Validates: Requirements 3.9
    """
    row = {
        "captureId": "5c602574fa5e450c820df6f9b5af8c2f",
        "captureType": constants.INFERENCE,
        "workflowId": "fake-wf-id",
        "inferenceCreationTime": 12345,
        "prediction": constants.NORMAL,
        "confidence": 0.83,
        "anomalyScore": 0.16,
        "anomalyThreshod": 0.91,
        "inputImageFilePath": "hi.jpg",
        "modelId": "model-123",
    }
    schema = InferenceResultSchema()
    accepted = "x" * constants.DB_TEXT_NOTE_MAX_LENGTH
    assert schema.load(dict(row, textNote=accepted)).textNote == accepted

    with pytest.raises(ValidationError) as excinfo:
        schema.load(dict(row, textNote="x" * (constants.DB_TEXT_NOTE_MAX_LENGTH + 1)))
    assert excinfo.value.messages == {
        "textNote": ["Longer than maximum length {}.".format(
            constants.DB_TEXT_NOTE_MAX_LENGTH)]
    }, "the textNote length rejection changed: {!r}".format(excinfo.value.messages)


# Property 4: allowedHumanClassification'() = allowedHumanClassification()
# Validates: Requirements 3.13
@settings(max_examples=25, deadline=None)
@given(row=_classification_rows(), classification=_non_vocabulary)
def test_human_classification_stays_binary_on_both_schemas(row, classification):
    """A human verdict stays a binary judgement on BOTH schemas: any value
    outside ``{Normal, Anomaly}`` -- ``"Detection"`` included -- is rejected
    with exactly today's message, so this vocabulary provably did not ride
    along with the stored-prediction widening.

    Validates: Requirements 3.13
    """
    expected = {"humanClassification": ["Must be one of: Normal, Anomaly."]}

    for value in (classification, DETECTION):
        with pytest.raises(ValidationError) as excinfo:
            InferenceResultSchema().load(dict(row, humanClassification=value))
        assert excinfo.value.messages == expected, (
            "InferenceResultSchema.humanClassification changed: {!r}"
            .format(excinfo.value.messages)
        )

        with pytest.raises(ValidationError) as excinfo:
            CapturedDataSchema().load({
                "captureId": row["captureId"],
                "captureType": constants.CAPTURE,
                "workflowId": row["workflowId"],
                "inferenceCreationTime": row["inferenceCreationTime"],
                "inputImageFilePath": row["inputImageFilePath"],
                "humanClassification": value,
            })
        assert excinfo.value.messages == expected, (
            "CapturedDataSchema.humanClassification changed: {!r}"
            .format(excinfo.value.messages)
        )

    for accepted in (constants.NORMAL, constants.ANOMALY):
        assert InferenceResultSchema().load(
            dict(row, humanClassification=accepted)).humanClassification == accepted


# Property 4: the marshal's detections block is still not a stored column
# Validates: Requirements 3.9, 3.11
@settings(max_examples=25, deadline=None)
@given(row=_classification_rows(),
       extra=st.sampled_from(["detections", "detection_count", "detectionCount"]))
def test_unknown_field_is_still_rejected(row, extra):
    """Unknown keys stay rejected, so the marshal's ``detections`` /
    ``detection_count`` remain non-stored and cannot leak into a row.

    Validates: Requirements 3.9, 3.11
    """
    with pytest.raises(ValidationError) as excinfo:
        InferenceResultSchema().load(dict(row, **{extra: {}}))
    assert excinfo.value.messages == {extra: ["Unknown field."]}


def test_captured_data_schema_shape_is_unchanged():
    """``CapturedDataSchema`` dumps exactly the recorded nine fields.

    Validates: Requirements 3.9, 3.19
    """
    schema = CapturedDataSchema()
    dumped = schema.dump(schema.load({
        "captureId": "12345",
        "captureType": constants.CAPTURE,
        "workflowId": "fake-wf-id",
        "inferenceCreationTime": 123456,
        "inputImageFilePath": "path",
    }))
    assert set(dumped) == CAPTURED_DATA_FIELDS
    assert dumped["flagForReview"] is False
    assert dumped["downloaded"] is False
    assert dumped["humanClassification"] is None
    assert dumped["textNote"] is None


# ---------------------------------------------------------------------------
# Requirement 3.10: the branch-selection predicates over arbitrary output lists
# ---------------------------------------------------------------------------

_CONTENT_TYPES = ["jpg", "json", "out.jpg", "overlay.jpg", "mask.jpg", "mask.png",
                  "something-else"]
_PAYLOAD_KINDS = ["detections", "anomalies", "empty", "list", "garbage", "absent"]


def _b64_json(payload):
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _label_block(kind):
    if kind == "detections":
        return _b64_json({"detections": {"0": {"class_index": "0",
                                               "class_label": "person",
                                               "bounding_box": [1.0, 2.0, 3.0, 4.0],
                                               "confidence": 0.5}}})
    if kind == "anomalies":
        return _b64_json({"anomalies": {"0": {"class-name": "background",
                                              "hex-color": "#ffffff",
                                              "total-percentage-area": 0.97}}})
    if kind == "empty":
        return _b64_json({})
    if kind == "list":
        return _b64_json([1, 2, 3])
    return "!!!not-base64!!!"


# Property 4: isDetectionOutput'(l) = isDetectionOutput(l) and
#             isSegmentationOutput'(l) = isSegmentationOutput(l)
# Validates: Requirements 3.10, 3.11
@settings(max_examples=25, deadline=None)
@given(
    content_types=st.lists(st.sampled_from(_CONTENT_TYPES), min_size=0, max_size=5,
                           unique=True),
    payload_kind=st.sampled_from(_PAYLOAD_KINDS),
)
def test_branch_predicates_are_unchanged(content_types, payload_kind):
    """Over arbitrary ``deviceFleetAuxiliaryOutputs`` lists both predicates
    answer exactly what they answered on unfixed code:

    * detection iff an ``overlay.jpg`` output exists AND the
      ``json_with_base64_encoding`` block decodes to a dict carrying
      ``detections`` (an undecodable block or a non-dict payload is not a
      detection),
    * segmentation iff an ``out.jpg`` output or any ``mask*`` output exists.

    Validates: Requirements 3.10, 3.11
    """
    output_list = [
        {"observedContentType": content_type, "data-ref": "file:///x", "data": "eyJ9"}
        for content_type in content_types
    ]
    if payload_kind != "absent":
        output_list.append({
            "observedContentType": constants.INFERENCE_OUTPUT_RES_LABEL_CONTENT_TYPE,
            "data": _label_block(payload_kind),
        })

    expected_detection = (
        constants.INFERENCE_OUTPUT_OVERLAY_CONTENT_TYPE in content_types
        and payload_kind == "detections"
    )
    expected_segmentation = (
        constants.INFERENCE_OUTPUT_IMAGE_CONTENT_TYPE in content_types
        or any(content_type.startswith(
            constants.INFERENCE_OUTPUT_MASK_CONTENT_TYPE_PREFIX)
            for content_type in content_types)
    )

    assert is_detection_model_output_result(output_list) == expected_detection, (
        "is_detection_model_output_result changed for content types {!r} with a "
        "{!r} label block".format(content_types, payload_kind)
    )
    assert is_segmentation_model_output_result(output_list) == expected_segmentation, (
        "is_segmentation_model_output_result changed for content types {!r}"
        .format(content_types)
    )


# ---------------------------------------------------------------------------
# Requirements 3.10 / 3.11: save_image_object, all three branches
# ---------------------------------------------------------------------------

_INFERENCE_TIME = "2023-11-10T22:00:21"
_FILE_BYTES = {
    "input.jpg": b"\xff\xd8IN\xff\xd9",
    "overlay.jpg": b"\xff\xd8OV\xff\xd9",
    "out.jpg": b"\xff\xd8OUT\xff\xd9",
    "mask.jpg": b"\xff\xd8MASK\xff\xd9",
}


class TestMarshalBranchesUnchanged(LocalServerBaseTestCase):
    """``GetInferenceResults.save_image_object`` for the detection, segmentation
    (with and without a mask) and classification branches, asserted key-for-key
    AND in key order -- the fix widens what the STORE accepts, never what the
    marshal produces (Requirement 3.11)."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="preservation-marshal-")
        self.paths = {}
        for name, content in _FILE_BYTES.items():
            path = os.path.join(self.dir, name)
            with open(path, "wb") as handle:
                handle.write(content)
            self.paths[name] = path
        self.jsonl = os.path.join(self.dir, "cap-1.jsonl")

    def _b64_file(self, name):
        return base64.b64encode(_FILE_BYTES[name]).decode()

    def _ref(self, name, content_type):
        return {"data-ref": "file://" + self.paths[name], "encoding": "NONE",
                "observedContentType": content_type}

    def _payload(self, outputs):
        return {
            "deviceFleetAuxiliaryInputs": [
                self._ref("input.jpg", constants.INFERENCE_INPUT_IMAGE_CONTENT_TYPE)],
            "deviceFleetAuxiliaryOutputs": outputs,
            "eventMetadata": {
                "capture_folder": self.dir,
                "eventId": "cap-1",
                "deviceFleetName": "fleet-A",
                "modelName": "cookie-model",
                "modelVersion": "1",
                "inferenceTime": _INFERENCE_TIME,
            },
            "eventVersion": "0",
        }

    def _marshal(self, outputs):
        with patch("utils.inference_results_utils.get_default_configs_lfv",
                   return_value=_MODEL_CONFIG):
            query = GetInferenceResults(stream_id="wf-1", sort="desc",
                                        starting_point=0, max_results=1)
            return query.save_image_object(self._payload(outputs), self.jsonl,
                                           capture_id="cap-1")

    def _assert_identical(self, actual, expected):
        assert list(actual) == list(expected), (
            "Requirement 3.10 / 3.11: the marshalled image object's keys or "
            "their order changed.\n  actual:   {!r}\n  expected: {!r}"
            .format(list(actual), list(expected))
        )
        assert list(actual["inferenceResult"]) == list(expected["inferenceResult"]), (
            "Requirement 3.10 / 3.11: the inferenceResult keys or their order "
            "changed.\n  actual:   {!r}\n  expected: {!r}".format(
                list(actual["inferenceResult"]), list(expected["inferenceResult"]))
        )
        assert actual == expected, (
            "Requirement 3.10 / 3.11: the marshalled image object changed.\n"
            "  actual:   {!r}\n  expected: {!r}".format(actual, expected)
        )

    # Property 4: saveImageObject'(outputList) = saveImageObject(outputList)
    # Validates: Requirements 3.10, 3.11
    def test_detection_branch_output_is_unchanged(self):
        """The detection branch surfaces the OVERLAY image and carries the
        structured ``detections`` block plus ``detection_count``.

        Validates: Requirements 3.10, 3.11
        """
        detections = {"0": {"class_index": "0", "class_label": "person",
                            "bounding_box": [1.0, 2.0, 3.0, 4.0], "confidence": 0.83}}
        result = self._marshal([
            self._ref("overlay.jpg",
                      constants.INFERENCE_OUTPUT_OVERLAY_CONTENT_TYPE),
            {"data": _b64_json({"Inference status": "success",
                                "Inference result": DETECTION,
                                "Detection_count": 1,
                                "Confidence": 0.83,
                                "Anomaly_score": 0.16,
                                "Anomaly_threshold": 0.91,
                                "Error msg": ""}),
             "encoding": "BASE64", "observedContentType": "json"},
            {"data": _b64_json({"detections": detections}), "encoding": "BASE64",
             "observedContentType": "json_with_base64_encoding"},
        ])

        self._assert_identical(result, {
            "imageDataFilePath": self.paths["overlay.jpg"],
            "inputImageFilePath": self.paths["input.jpg"],
            "creationTime": _INFERENCE_TIME,
            "inferenceResult": {
                "confidence": 0.83,
                "inference_result": DETECTION,
                "anomaly_score": 0.16,
                "anomaly_threshold": 0.91,
                "detections": detections,
                "detection_count": 1,
            },
            "inferenceFilePath": self.jsonl,
            "image": self._b64_file("overlay.jpg"),
            # confidence 0.83 < AnomalyThreshold 0.9
            "humanReviewRequired": True,
            "captureId": "cap-1",
        })

    # Property 4: saveImageObject'(outputList) = saveImageObject(outputList)
    # Validates: Requirements 3.10
    def test_segmentation_with_mask_branch_output_is_unchanged(self):
        """The segmentation branch surfaces ``out.jpg``, converts the mask
        background's hex color to rgb, base64s the mask image off disk and drops
        the ``"0"`` background entry from ``anomalies``.

        Validates: Requirements 3.10
        """
        result = self._marshal([
            self._ref("mask.jpg", "mask.jpg"),
            self._ref("out.jpg", constants.INFERENCE_OUTPUT_IMAGE_CONTENT_TYPE),
            {"data": _b64_json({"Inference status": "success",
                                "Inference result": constants.ANOMALY,
                                "Confidence": 0.83,
                                "Anomaly_score": 0.16,
                                "Anomaly_threshold": 0.91,
                                "Error msg": ""}),
             "encoding": "BASE64", "observedContentType": "json"},
            {"data": _b64_json({"anomalies": {
                "0": {"class-name": "background", "hex-color": "#ffffff",
                      "total-percentage-area": 0.97},
                "1": {"class-name": "cracked", "hex-color": "#23a436",
                      "total-percentage-area": 0.03},
            }}), "encoding": "BASE64",
             "observedContentType": "json_with_base64_encoding"},
        ])

        self._assert_identical(result, {
            "imageDataFilePath": self.paths["out.jpg"],
            "inputImageFilePath": self.paths["input.jpg"],
            "creationTime": _INFERENCE_TIME,
            "inferenceResult": {
                "confidence": 0.83,
                "inference_result": constants.ANOMALY,
                "anomaly_score": 0.16,
                "anomaly_threshold": 0.91,
                "mask_background": {"class-name": "background",
                                    "rgb-color": [255, 255, 255],
                                    "total-percentage-area": 0.97},
                "mask_image": self._b64_file("mask.jpg"),
                "anomalies": {"1": {"class-name": "cracked",
                                    "hex-color": "#23a436",
                                    "total-percentage-area": 0.03}},
            },
            "inferenceFilePath": self.jsonl,
            "image": self._b64_file("out.jpg"),
            "humanReviewRequired": True,
            "captureId": "cap-1",
        })

    # Property 4: saveImageObject'(outputList) = saveImageObject(outputList)
    # Validates: Requirements 3.10
    def test_segmentation_without_mask_branch_output_is_unchanged(self):
        """With no mask output the segmentation branch emits no ``mask_image`` /
        ``mask_background`` / ``anomalies`` keys at all.

        Validates: Requirements 3.10
        """
        result = self._marshal([
            self._ref("out.jpg", constants.INFERENCE_OUTPUT_IMAGE_CONTENT_TYPE),
            {"data": _b64_json({"Inference status": "success",
                                "Inference result": constants.ANOMALY,
                                "Confidence": 0.83,
                                "Anomaly_score": 0.16,
                                "Anomaly_threshold": 0.91,
                                "Error msg": ""}),
             "encoding": "BASE64", "observedContentType": "json"},
            {"data": _b64_json({"anomalies": {
                "0": {"class-name": "background", "hex-color": "#ffffff",
                      "total-percentage-area": 0.97},
                "1": {"class-name": "cracked", "hex-color": "#23a436",
                      "total-percentage-area": 0.03},
            }}), "encoding": "BASE64",
             "observedContentType": "json_with_base64_encoding"},
        ])

        self._assert_identical(result, {
            "imageDataFilePath": self.paths["out.jpg"],
            "inputImageFilePath": self.paths["input.jpg"],
            "creationTime": _INFERENCE_TIME,
            "inferenceResult": {
                "confidence": 0.83,
                "inference_result": constants.ANOMALY,
                "anomaly_score": 0.16,
                "anomaly_threshold": 0.91,
            },
            "inferenceFilePath": self.jsonl,
            "image": self._b64_file("out.jpg"),
            "humanReviewRequired": True,
            "captureId": "cap-1",
        })

    # Property 4: saveImageObject'(outputList) = saveImageObject(outputList)
    # Validates: Requirements 3.10
    def test_classification_branch_output_is_unchanged(self):
        """The classification branch surfaces the INPUT image, emits
        ``inference_result`` LAST, and applies the ``temp_get_confidence``
        rule: a Normal capture whose anomaly score is close to its confidence
        reports ``1 - anomaly_score``.

        Validates: Requirements 3.10
        """
        result = self._marshal([
            {"data": _b64_json({"Inference status": "success",
                                "Inference result": constants.NORMAL,
                                "Confidence": 0.83,
                                "Anomaly_score": 0.83,
                                "Anomaly_threshold": 0.91,
                                "Error msg": ""}),
             "encoding": "BASE64", "observedContentType": "json"},
        ])

        self._assert_identical(result, {
            "imageDataFilePath": self.paths["input.jpg"],
            "inputImageFilePath": self.paths["input.jpg"],
            "creationTime": _INFERENCE_TIME,
            "inferenceResult": {
                "confidence": 1 - 0.83,
                "anomaly_score": 0.83,
                "anomaly_threshold": 0.91,
                "inference_result": constants.NORMAL,
            },
            "inferenceFilePath": self.jsonl,
            "image": self._b64_file("input.jpg"),
            # Confidence 0.83 >= NormalThreshold 0.8
            "humanReviewRequired": False,
            "captureId": "cap-1",
        })

    # Property 4: saveImageObject'(outputList) = saveImageObject(outputList)
    # Validates: Requirements 3.10
    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(prediction=_predictions, confidence=_unit_floats,
           anomaly_score=_unit_floats, anomaly_threshold=_unit_floats)
    def test_classification_confidence_rule_is_unchanged(
            self, prediction, confidence, anomaly_score, anomaly_threshold):
        """Over arbitrary model numbers the reported confidence stays
        ``1 - anomaly_score`` for a Normal capture whose anomaly score is close
        to its confidence, and the raw confidence otherwise.

        Validates: Requirements 3.10
        """
        result = self._marshal([
            {"data": _b64_json({"Inference status": "success",
                                "Inference result": prediction,
                                "Confidence": confidence,
                                "Anomaly_score": anomaly_score,
                                "Anomaly_threshold": anomaly_threshold,
                                "Error msg": ""}),
             "encoding": "BASE64", "observedContentType": "json"},
        ])

        expected = (1 - anomaly_score
                    if prediction == constants.NORMAL
                    and math.isclose(anomaly_score, confidence)
                    else confidence)
        assert result["inferenceResult"] == {
            "confidence": expected,
            "anomaly_score": anomaly_score,
            "anomaly_threshold": anomaly_threshold,
            "inference_result": prediction,
        }, "the classification branch's inferenceResult changed"


# ---------------------------------------------------------------------------
# Requirement 3.12: convert_inference_res_to_save_in_db is NOT modified
# ---------------------------------------------------------------------------

def _inference_res(row_id, creation_time, inference_result, extras=None):
    """The ``inference_res`` shape ``save_full_inference_result`` hands to the db
    marshal (``endpoints/workflow.py`` line 124: no ``image``, ``captureType``
    set to ``Inference``)."""
    result = {
        "confidence": 0.83,
        "inference_result": inference_result,
        "anomaly_score": 0.16,
        "anomaly_threshold": 0.91,
    }
    result.update(extras or {})
    return {
        "captureId": row_id,
        "captureType": constants.INFERENCE,
        "creationTime": creation_time,
        "imageDataFilePath": "out-path",
        "inputImageFilePath": "in-path",
        "inferenceFilePath": "jsonl-path",
        "humanReviewRequired": True,
        "inferenceResult": result,
    }


def _workflow(workflow_id, model_id):
    return {
        "workflowId": workflow_id,
        "name": "test-workflow",
        "featureConfigurations": [{"type": "LFVModel", "modelName": model_id}],
        "outputConfigurations": [],
        "inputConfigurations": [],
        "workflowOutputPath": "/tmp",
    }


class TestConvertForDbUnchanged(LocalServerBaseTestCase):
    """``convert_inference_res_to_save_in_db`` field-for-field, including the
    ``None``-stripping and the ``get_default_configs_lfv`` fallback. The fix does
    NOT change this function (Requirement 3.12)."""

    # Property 4: convertForDb'(inferenceRes, workflow) = convertForDb(...)
    # Validates: Requirements 3.12
    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(prediction=_predictions, capture_id=_capture_ids, model_id=_ids,
           epoch=st.integers(min_value=1_000_000_000, max_value=1_900_000_000),
           with_mask=st.booleans(), with_anomalies=st.booleans())
    def test_convert_for_db_is_unchanged(self, prediction, capture_id, model_id,
                                         epoch, with_mask, with_anomalies):
        """Over arbitrary segmentation and classification results the marshalled
        row carries exactly the recorded keys and values: the same model config
        reads, the same ``anomalies`` → ``anomalyLabels`` flattening, and the
        same ``None``-stripping (``maskImage`` / ``maskBackground`` /
        ``humanClassification`` / ``textNote`` absent rather than ``None``).

        The creation time is generated as an epoch and formatted the way the
        marshal parses it back, so the assertion needs no timezone assumption.

        Validates: Requirements 3.12
        """
        extras = {}
        expected_extra = {}
        if with_mask:
            extras["mask_image"] = "bWFzaw=="
            extras["mask_background"] = {"class-name": "background",
                                         "rgb-color": [255, 255, 255]}
            expected_extra["maskImage"] = extras["mask_image"]
            expected_extra["maskBackground"] = extras["mask_background"]
        else:
            extras["mask_image"] = None
            extras["mask_background"] = None
        if with_anomalies:
            extras["anomalies"] = {"1": {"class-name": "cracked",
                                         "hex-color": "#23a436"}}
            expected_extra["anomalyLabels"] = [extras["anomalies"]["1"]]
        else:
            extras["anomalies"] = None

        creation_time = datetime.fromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%S")
        inference_res = _inference_res(capture_id, creation_time, prediction, extras)

        with patch("utils.inference_results_utils.get_default_configs_lfv",
                   return_value=_MODEL_CONFIG):
            row = convert_inference_res_to_save_in_db(
                inference_res, _workflow("wf-1", model_id))

        expected = {
            "workflowId": "wf-1",
            "modelId": model_id,
            "modelName": _MODEL_CONFIG["modelAlias"],
            "captureId": capture_id,
            "captureType": constants.INFERENCE,
            "inputImageFilePath": "in-path",
            "outputImageFilePath": "out-path",
            "inferenceCreationTime": epoch,
            "confidence": 0.83,
            "anomalyScore": 0.16,
            "anomalyThreshod": 0.91,
            "prediction": prediction,
            "flagForReview": False,
            "downloaded": False,
            "humanReviewRequired": True,
            "modelConfidenceThresholds": _MODEL_CONFIG["modelConfidenceThresholds"],
        }
        expected.update(expected_extra)

        assert row == expected, (
            "Requirement 3.12: convert_inference_res_to_save_in_db changed.\n"
            "  actual:   {!r}\n  expected: {!r}".format(row, expected)
        )
        for stripped in ("humanClassification", "textNote"):
            assert stripped not in row, (
                "Requirement 3.12: the None-stripping changed -- {!r} leaked "
                "into the row".format(stripped)
            )
        # The marshalled row is exactly what storage accepts.
        InferenceResultSchema().load(row)

    # Property 4: convertForDb'(inferenceRes, workflow) = convertForDb(...)
    # Validates: Requirements 3.12
    def test_convert_for_db_resource_not_found_fallback_is_unchanged(self):
        """With no Greengrass component for the model, the REAL
        ``get_default_configs_lfv`` falls back to
        ``{modelAlias: model_id, modelMetaData: {}, modelVersion: "1.0.0",
        modelConfidenceThresholds: {}}`` and the marshalled row carries
        ``modelName == model_id`` with an empty thresholds dict.

        Validates: Requirements 3.12
        """
        import awsiot.greengrasscoreipc.model as gg_model
        from utils import feature_configs_utils

        # A fresh id per run: get_default_configs_lfv is lru_cached.
        model_id = "model-" + uuid.uuid4().hex[:8]
        with patch.object(feature_configs_utils, "get_ipc_client",
                          side_effect=gg_model.ResourceNotFoundError()):
            assert feature_configs_utils.get_default_configs_lfv(model_id) == {
                "modelAlias": model_id,
                "modelMetaData": {},
                "modelVersion": "1.0.0",
                "modelConfidenceThresholds": {},
            }, "the ResourceNotFoundError fallback changed"

            row = convert_inference_res_to_save_in_db(
                _inference_res("cap-rnf", "2023-11-10T22:00:21", constants.NORMAL,
                               {"anomalies": None, "mask_image": None,
                                "mask_background": None}),
                _workflow("wf-1", model_id))

        assert row["modelId"] == model_id
        assert row["modelName"] == model_id
        assert row["modelConfidenceThresholds"] == {}
        InferenceResultSchema().load(row)


# ---------------------------------------------------------------------------
# Requirements 3.14 / 3.15: the summary and the results list endpoint
# ---------------------------------------------------------------------------

def _db_row(**overrides):
    """A minimal stored row for the DAO / endpoint properties. Fixed floats so
    nothing is at the mercy of a sqlite float round trip."""
    row = {
        "captureId": "5c602574fa5e450c820df6f9b5af8c2f",
        "captureType": constants.INFERENCE,
        "workflowId": "fake-wf-id",
        "inferenceCreationTime": 12345,
        "prediction": constants.NORMAL,
        "confidence": 0.83,
        "anomalyScore": 0.16,
        "anomalyThreshod": 0.91,
        "inputImageFilePath": "hi.jpg",
        "outputImageFilePath": "path",
        "modelId": "model-123",
        "modelName": "fake-model",
        "flagForReview": False,
        "downloaded": False,
    }
    row.update(overrides)
    return row


class TestSummaryAndListEndpointUnchanged(LocalServerBaseTestCase):
    """``get_inference_result_summary`` and ``GET /workflows/{id}/results``
    against the REAL DAO / route over the metadata database."""

    def setUp(self):
        super().setUp()
        self.session = Session(self.metadata_engine)

    def tearDown(self):
        self.session.close()
        super().tearDown()

    def _add(self, workflow_id, rows):
        from dao.sqlite_db.models import InferenceResult

        for index, row in enumerate(rows):
            self.session.add(InferenceResult(**_db_row(
                captureId=row.pop("captureId", "{}-{}".format(workflow_id, index)),
                workflowId=workflow_id, **row)))
        self.session.commit()

    def _drop(self, workflow_id):
        from dao.sqlite_db.models import InferenceResult

        self.session.query(InferenceResult).filter(
            InferenceResult.workflowId == workflow_id).delete()
        self.session.commit()

    # Property 4: summary'(X) = summary(X) EXTENDED WITH {detection: 0}
    # Validates: Requirements 3.15
    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(normal=st.integers(min_value=0, max_value=4),
           anomaly=st.integers(min_value=0, max_value=4))
    def test_summary_for_normal_and_anomaly_only_windows_is_unchanged(
            self, normal, anomaly):
        """For a window holding only Normal and Anomaly rows the summary keeps
        exactly today's ``normal`` / ``anomaly`` / ``totalInference`` values. The
        ONLY tolerated difference is an additive ``detection`` key, which must
        be ``0`` here.

        Validates: Requirements 3.15
        """
        from dao.sqlite_db import inference_result_dao
        from resources.accessors.inference_result_accessor import InferenceResultAccessor

        workflow_id = "wf-" + uuid.uuid4().hex[:8]
        rows = ([{"prediction": constants.NORMAL, "inferenceCreationTime": 1000 + i}
                 for i in range(normal)]
                + [{"prediction": constants.ANOMALY,
                    "inferenceCreationTime": 2000 + i} for i in range(anomaly)])
        try:
            self._add(workflow_id, rows)
            stats = inference_result_dao.get_inference_result_summary(
                self.session, workflow_id, 0)

            assert stats["normal"] == normal
            assert stats["anomaly"] == anomaly
            assert stats["totalInference"] == normal + anomaly, (
                "Requirement 3.15: totalInference changed for a window with no "
                "Detection rows; got {!r}".format(stats)
            )
            extra = set(stats) - SUMMARY_KEYS
            assert extra <= {"detection"}, (
                "Requirement 3.15: the summary grew unexpected keys {!r}"
                .format(extra)
            )
            assert SUMMARY_KEYS <= set(stats), (
                "Requirement 3.15: the summary lost a bucket; got {!r}".format(stats)
            )
            if "detection" in stats:
                assert stats["detection"] == 0, (
                    "the additive detection bucket must be 0 for a window with "
                    "no Detection rows; got {!r}".format(stats)
                )

            envelope = InferenceResultAccessor().get_inference_result_summary(
                self.session, workflow_id, 0)
            assert set(envelope) == {"stats", "lastResetTime"}, (
                "Requirement 3.15: the summary envelope changed; got {!r}"
                .format(sorted(envelope))
            )
            assert envelope["stats"] == stats
            assert envelope["lastResetTime"] == 0
        finally:
            self.session.rollback()
            self._drop(workflow_id)

    # Property 4: summary'(X) = summary(X) EXTENDED WITH {detection: 0}
    # Validates: Requirements 3.15
    def test_summary_start_time_boundary_is_unchanged(self):
        """The ``summaryStartTime`` window stays INCLUSIVE. Recorded on unfixed
        code for rows at 99 / 100 / 200 / 200 / 300::

            start=0   -> total 5, normal 3, anomaly 2
            start=99  -> total 5, normal 3, anomaly 2
            start=100 -> total 4, normal 2, anomaly 2
            start=200 -> total 3, normal 1, anomaly 2
            start=301 -> total 0, normal 0, anomaly 0

        Validates: Requirements 3.15
        """
        from dao.sqlite_db import inference_result_dao
        from resources.accessors.inference_result_accessor import InferenceResultAccessor

        workflow_id = "wf-" + uuid.uuid4().hex[:8]
        try:
            self._add(workflow_id, [
                {"prediction": constants.NORMAL, "inferenceCreationTime": 99},
                {"prediction": constants.NORMAL, "inferenceCreationTime": 100},
                {"prediction": constants.NORMAL, "inferenceCreationTime": 200},
                {"prediction": constants.ANOMALY, "inferenceCreationTime": 200},
                {"prediction": constants.ANOMALY, "inferenceCreationTime": 300},
            ])
            for start, expected in ((0, (5, 3, 2)), (99, (5, 3, 2)), (100, (4, 2, 2)),
                                    (200, (3, 1, 2)), (301, (0, 0, 0))):
                stats = inference_result_dao.get_inference_result_summary(
                    self.session, workflow_id, start)
                assert (stats["totalInference"], stats["normal"], stats["anomaly"]) \
                    == expected, (
                    "Requirement 3.15: the summary window changed at "
                    "summaryStartTime={}; got {!r}".format(start, stats)
                )

            envelope = InferenceResultAccessor().get_inference_result_summary(
                self.session, workflow_id, 100)
            assert envelope["lastResetTime"] == 100
            assert envelope["stats"]["normal"] == 2
            assert envelope["stats"]["anomaly"] == 2
        finally:
            self.session.rollback()
            self._drop(workflow_id)

    # Property 4: listResults'(query) = listResults(query)
    # Validates: Requirements 3.14
    def test_list_results_filters_and_shape_are_unchanged(self):
        """Every filter combination recorded on unfixed code, against three rows
        (``c1`` Normal@100 not downloaded / no note / no human classification /
        review required / Inference, ``c2`` Anomaly@300 downloaded / note
        "hello note" / human classification Normal / no review / Inference,
        ``c3`` Anomaly@200 not downloaded / note "other" / no human
        classification / review NULL / Capture).

        Also pins the envelope ``{total, page, size, results}``, the 20-field
        item shape, the descending ``inferenceCreationTime`` ordering, and the
        400 for an out-of-vocabulary ``prediction`` filter value.

        Validates: Requirements 3.14
        """
        workflow_id = "wf-" + uuid.uuid4().hex[:8]
        try:
            self._add(workflow_id, [
                {"captureId": "c1", "prediction": constants.NORMAL,
                 "inferenceCreationTime": 100, "downloaded": False, "textNote": None,
                 "humanClassification": None, "humanReviewRequired": True,
                 "captureType": constants.INFERENCE},
                {"captureId": "c2", "prediction": constants.ANOMALY,
                 "inferenceCreationTime": 300, "downloaded": True,
                 "textNote": "hello note", "humanClassification": constants.NORMAL,
                 "humanReviewRequired": False, "captureType": constants.INFERENCE},
                {"captureId": "c3", "prediction": constants.ANOMALY,
                 "inferenceCreationTime": 200, "downloaded": False,
                 "textNote": "other", "humanClassification": None,
                 "humanReviewRequired": None, "captureType": constants.CAPTURE},
            ])

            recorded = [
                ({}, ["c2", "c3", "c1"], 3, 1, 12),
                ({"prediction": constants.NORMAL}, ["c1"], 1, 1, 12),
                ({"prediction": constants.ANOMALY}, ["c2", "c3"], 2, 1, 12),
                ({"downloaded": "true"}, ["c2"], 1, 1, 12),
                ({"downloaded": "false"}, ["c3", "c1"], 2, 1, 12),
                ({"textNoteFilter": "note"}, ["c2"], 1, 1, 12),
                # whitespace-only note filter is ignored entirely
                ({"textNoteFilter": " "}, ["c2", "c3", "c1"], 3, 1, 12),
                ({"humanClassificationProvided": "true"}, ["c2"], 1, 1, 12),
                ({"humanClassificationProvided": "false"}, ["c3", "c1"], 2, 1, 12),
                ({"humanReviewRequired": "true"}, ["c1"], 1, 1, 12),
                # false also matches the NULL row
                ({"humanReviewRequired": "false"}, ["c2", "c3"], 2, 1, 12),
                ({"captureType": constants.INFERENCE}, ["c2", "c1"], 2, 1, 12),
                ({"captureType": constants.CAPTURE}, ["c3"], 1, 1, 12),
                ({"page": 1, "size": 2}, ["c2", "c3"], 3, 1, 2),
                ({"page": 2, "size": 2}, ["c1"], 3, 2, 2),
            ]

            with patch("utils.server_setup.workflow_accessor.get_workflow_by_id",
                       return_value={}):
                for params, expected_ids, total, page, size in recorded:
                    response = self.client.get(
                        "/workflows/{}/results".format(workflow_id), params=params)
                    assert response.status_code == 200, (
                        "Requirement 3.14: {!r} answered {}".format(
                            params, response.status_code)
                    )
                    body = response.json()
                    assert set(body) == LIST_ENVELOPE_KEYS, (
                        "Requirement 3.14: the paginated envelope changed; got {!r}"
                        .format(sorted(body))
                    )
                    assert (body["total"], body["page"], body["size"]) \
                        == (total, page, size), (
                        "Requirement 3.14: pagination changed for {!r}; got {!r}"
                        .format(params, body)
                    )
                    assert [item["captureId"] for item in body["results"]] \
                        == expected_ids, (
                        "Requirement 3.14: filtering or ordering changed for "
                        "{!r}; got {!r}".format(
                            params, [item["captureId"] for item in body["results"]])
                    )
                    for item in body["results"]:
                        assert set(item) == LIST_ITEM_FIELDS, (
                            "Requirement 3.14: the result item shape changed; "
                            "added={!r} removed={!r}".format(
                                set(item) - LIST_ITEM_FIELDS,
                                LIST_ITEM_FIELDS - set(item))
                        )

                # The prediction query Literal is NOT widened (Requirement 3.14):
                # derive the rejection status from a CONTROL request on another
                # Literal, because the app maps RequestValidationError to 400.
                control = self.client.get(
                    "/workflows/{}/results".format(workflow_id),
                    params={"captureType": "NotACaptureType"})
                assert control.status_code in (400, 422)
                rejected = self.client.get(
                    "/workflows/{}/results".format(workflow_id),
                    params={"prediction": DETECTION})
                assert rejected.status_code == control.status_code, (
                    "Requirement 3.14: the prediction query filter must still "
                    "reject {!r}; got {}".format(DETECTION, rejected.status_code)
                )
                assert "prediction" in rejected.text
        finally:
            self.session.rollback()
            self._drop(workflow_id)

    # Property 4: listResults'(query) = listResults(query)
    # Validates: Requirements 3.14
    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(timestamps=st.lists(st.integers(min_value=0, max_value=10_000),
                               min_size=0, max_size=6),
           size=st.integers(min_value=1, max_value=4),
           page=st.integers(min_value=1, max_value=3))
    def test_list_results_ordering_and_pagination_are_unchanged(
            self, timestamps, size, page):
        """Over arbitrary row sets and page windows the endpoint keeps returning
        ``inferenceCreationTime`` DESCENDING, a ``total`` equal to the row count
        and a page slice of the recorded size.

        Ties are asserted as a non-increasing timestamp sequence rather than a
        fixed id order, since sqlite does not define an order within a tie.

        Validates: Requirements 3.14
        """
        workflow_id = "wf-" + uuid.uuid4().hex[:8]
        try:
            self._add(workflow_id, [
                {"captureId": "{}-{}".format(workflow_id, index),
                 "inferenceCreationTime": timestamp}
                for index, timestamp in enumerate(timestamps)
            ])
            with patch("utils.server_setup.workflow_accessor.get_workflow_by_id",
                       return_value={}):
                response = self.client.get(
                    "/workflows/{}/results".format(workflow_id),
                    params={"page": page, "size": size})
            assert response.status_code == 200
            body = response.json()
            assert body["total"] == len(timestamps)
            assert (body["page"], body["size"]) == (page, size)

            offset = (page - 1) * size
            expected_count = max(0, min(size, len(timestamps) - offset))
            assert len(body["results"]) == expected_count, (
                "Requirement 3.14: the page slice changed for {} rows at "
                "page={} size={}".format(len(timestamps), page, size)
            )
            returned = [item["inferenceCreationTime"] for item in body["results"]]
            assert returned == sorted(timestamps, reverse=True)[
                offset:offset + expected_count], (
                "Requirement 3.14: the descending inferenceCreationTime ordering "
                "changed; got {!r} from {!r}".format(returned, timestamps)
            )
        finally:
            self.session.rollback()
            self._drop(workflow_id)


# ---------------------------------------------------------------------------
# Requirement 3.17: the manual-run response payload
# ---------------------------------------------------------------------------

class TestManualRunResponseUnchanged(LocalServerBaseTestCase):
    """``POST /workflows/{id}/run`` in both ``returnPartialResultsEarly`` modes
    and both ``returnImageString`` values. Persistence stays a background task
    that cannot fail the response (Requirement 3.17), so the background call is
    stood in for and the RESPONSE is what is asserted."""

    def setUp(self):
        super().setUp()
        self.workflow = {
            "workflowId": "wf-run",
            "name": "yolotest",
            "imageSources": [{"imageSourceId": "is-1"}],
            "featureConfigurations": [{"type": "LFVModel", "modelName": "model-123"}],
            "workflowOutputPath": "/tmp",
        }
        # A fresh capture id per run: latency_time is keyed
        # (inferenceCaptureId, latencyType).
        self.capture_id = "cap-" + uuid.uuid4().hex[:8]

    def _result(self):
        return {
            "captureId": self.capture_id,
            "creationTime": "2023-11-10T22:00:21",
            "imageDataFilePath": "out-path",
            "inputImageFilePath": "in-path",
            "inferenceFilePath": "jsonl-path",
            "humanReviewRequired": False,
            "image": "aGVsbG8=",
            "inferenceResult": {
                "confidence": 0.83,
                "inference_result": constants.ANOMALY,
                "anomaly_score": 0.16,
                "anomaly_threshold": 0.91,
            },
        }

    def _run(self, body, persist=None, early_persist=None, client=None):
        """Drive the route with the pipeline and the background persistence
        stood in. The stand-in pipeline adds the INFERENCE_RECEIVED timestamp,
        exactly as ``gst_pipeline`` does (line 281), because the route derives
        ``processingTime`` from it."""
        from endpoints import workflow as workflow_module

        def fake_pipeline(workflow, db, latency_metrics):
            latency_metrics.add_timestamp(constants.INFERENCE_RECEIVED_TIMESTAMP)
            return self.capture_id, {"confidence": 0.83, "is_anomalous": True}

        with patch.object(workflow_module.workflow_accessor, "get_workflow_by_id",
                          return_value=self.workflow), \
             patch.object(workflow_module, "configure_image_source_and_run_pipeline",
                          side_effect=fake_pipeline), \
             patch.object(workflow_module, "read_inference_result",
                          side_effect=lambda *args, **kwargs: self._result()), \
             patch.object(workflow_module, "save_full_inference_result",
                          side_effect=persist or (lambda result, workflow: None)), \
             patch.object(workflow_module, "read_full_results_and_save",
                          side_effect=early_persist
                          or (lambda *args, **kwargs: None)):
            return (client or self.client).post(
                "/workflows/wf-run/run", json=body)

    # Property 4: manualRunResponse'(runRequest) = manualRunResponse(runRequest)
    # Validates: Requirements 3.17
    def test_full_mode_response_is_unchanged(self):
        """``returnPartialResultsEarly`` false returns the marshalled result plus
        a float ``processingTime``, and drops ``image`` only when
        ``returnImageString`` is false.

        Validates: Requirements 3.17
        """
        for return_image in (True, False):
            self.capture_id = "cap-" + uuid.uuid4().hex[:8]
            response = self._run({"returnPartialResultsEarly": False,
                                  "returnImageString": return_image})
            assert response.status_code == 200
            body = response.json()

            expected = self._result()
            if not return_image:
                del expected["image"]
            processing_time = body.pop("processingTime", None)
            assert isinstance(processing_time, float), (
                "Requirement 3.17: processingTime must stay a float; got {!r}"
                .format(processing_time)
            )
            assert body == expected, (
                "Requirement 3.17: the manual-run response changed for "
                "returnImageString={}.\n  actual:   {!r}\n  expected: {!r}"
                .format(return_image, body, expected)
            )

    # Property 4: manualRunResponse'(runRequest) = manualRunResponse(runRequest)
    # Validates: Requirements 3.17
    def test_early_return_response_is_unchanged(self):
        """``returnPartialResultsEarly`` true returns exactly
        ``{"captureId", "inferenceResult": {"confidence", "inference_result"}}``
        -- no ``image``, no ``processingTime`` -- in BOTH ``returnImageString``
        modes, and the prediction is the Anomaly / Normal read of the parsed
        tags.

        Validates: Requirements 3.17
        """
        for return_image in (True, False):
            self.capture_id = "cap-" + uuid.uuid4().hex[:8]
            response = self._run({"returnPartialResultsEarly": True,
                                  "returnImageString": return_image})
            assert response.status_code == 200
            assert response.json() == {
                "captureId": self.capture_id,
                "inferenceResult": {"confidence": 0.83,
                                    "inference_result": constants.ANOMALY},
            }, (
                "Requirement 3.17: the early-return shape changed for "
                "returnImageString={}; got {!r}".format(return_image,
                                                        response.json())
            )

    # Property 4: manualRunResponse'(runRequest) = manualRunResponse(runRequest)
    # Validates: Requirements 3.17
    def test_persistence_failure_does_not_change_the_response(self):
        """A persistence failure raised from the background task leaves the
        response byte-identical to a successful run (bar the per-run capture id
        and timing) -- failing the response on a persistence error would regress
        every currently working model.

        ``raise_server_exceptions=False`` mirrors
        ``api-endpoints/test_inference_result_api.py``: the TestClient runs the
        ASGI app in-process, so it would otherwise re-raise the background
        task's exception at the call site even though the real server has
        already flushed the response.

        Validates: Requirements 3.17
        """
        from app import app
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)

        self.capture_id = "cap-" + uuid.uuid4().hex[:8]
        healthy = self._run({}, client=client)
        assert healthy.status_code == 200
        healthy_body = healthy.json()
        healthy_body.pop("processingTime")
        healthy_body.pop("captureId")

        self.capture_id = "cap-" + uuid.uuid4().hex[:8]
        failing = self._run(
            {},
            persist=RuntimeError("simulated persistence failure"),
            early_persist=RuntimeError("simulated persistence failure"),
            client=client)

        assert failing.status_code == 200, (
            "Requirement 3.17: a persistence failure must not fail the "
            "manual-run response; got {}".format(failing.status_code)
        )
        failing_body = failing.json()
        assert failing_body.pop("captureId") == self.capture_id
        assert isinstance(failing_body.pop("processingTime"), float)
        assert failing_body == healthy_body, (
            "Requirement 3.17: the manual-run response changed when persistence "
            "failed.\n  failed:  {!r}\n  healthy: {!r}".format(
                failing_body, healthy_body)
        )


# ---------------------------------------------------------------------------
# Requirement 3.16: the SMGT manifest and the download prediction check
# ---------------------------------------------------------------------------

# Property 4: the manifest path is untouched for Normal / Anomaly rows
# Validates: Requirements 3.16
@settings(max_examples=25, deadline=None)
@given(prediction=_predictions,
       human_classification=st.one_of(st.none(), _predictions),
       confidence=_unit_floats,
       text_note=st.one_of(st.none(), _text_notes))
def test_generate_smgt_format_manifest_is_unchanged(
        prediction, human_classification, confidence, text_note):
    """For arbitrary Normal / Anomaly rows the manifest keeps preferring the
    human classification over the model prediction, keeps marking
    ``human-annotated``, keeps basenaming ``source-ref``, and keeps mapping
    ``anomaly-label`` to 1 only for Anomaly.

    Validates: Requirements 3.16
    """
    row = {
        "inputImageFilePath": "my/path/foo.jpg",
        "prediction": prediction,
        "confidence": confidence,
        "inferenceCreationTime": 1712785191,
        "humanClassification": human_classification,
        "textNote": text_note,
    }
    classification = human_classification or prediction
    assert generate_smgt_format_manifest([row]) == [{
        "source-ref": "foo.jpg",
        "source-ref-metadata": {"notes": text_note},
        "anomaly-label": 1 if classification == constants.ANOMALY else 0,
        "anomaly-label-metadata": {
            "class-name": classification,
            "confidence": confidence,
            "type": "groundtruth/image-classification",
            "human-annotated": "yes" if human_classification else "no",
            "creation-date": datetime.fromtimestamp(
                1712785191, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        },
    }], "Requirement 3.16: generate_smgt_format_manifest changed"


def test_generate_smgt_format_manifest_recorded_case_is_unchanged():
    """The exact two-row manifest recorded on unfixed code, plus the empty case.

    Validates: Requirements 3.16
    """
    assert generate_smgt_format_manifest([]) == []
    assert generate_smgt_format_manifest([
        {"inputImageFilePath": "foo.jpg", "prediction": constants.ANOMALY,
         "confidence": 0.6, "inferenceCreationTime": 1712785191,
         "humanClassification": None, "textNote": "bar"},
        {"inputImageFilePath": "my/path/foobar.jpg", "prediction": constants.NORMAL,
         "confidence": 0.5, "inferenceCreationTime": 1712784822,
         "humanClassification": constants.NORMAL, "textNote": None},
    ]) == [
        {"source-ref": "foo.jpg",
         "source-ref-metadata": {"notes": "bar"},
         "anomaly-label": 1,
         "anomaly-label-metadata": {"class-name": "Anomaly", "confidence": 0.6,
                                    "type": "groundtruth/image-classification",
                                    "human-annotated": "no",
                                    "creation-date": "2024-04-10T21:39:51"}},
        {"source-ref": "foobar.jpg",
         "source-ref-metadata": {"notes": None},
         "anomaly-label": 0,
         "anomaly-label-metadata": {"class-name": "Normal", "confidence": 0.5,
                                    "type": "groundtruth/image-classification",
                                    "human-annotated": "yes",
                                    "creation-date": "2024-04-10T21:33:42"}},
    ]


class TestDownloadPredictionCheckUnchanged(LocalServerBaseTestCase):
    """The download / export path's prediction check reads
    ``constants.PREDICTION`` and is NOT widened (Requirements 2.7, 3.16)."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="preservation-export-")
        self.image_path = os.path.join(self.dir, "img.jpg")
        with open(self.image_path, "wb") as handle:
            handle.write(b"\xff\xd8\xff\xd9")

    # Property 4: the download-file prediction check is untouched
    # Validates: Requirements 3.16
    def test_export_zip_name_prediction_prefix_is_unchanged(self):
        """A ``Normal`` / ``Anomaly`` ``predictionResult`` still prefixes the
        exported zip name (the contents are guaranteed single-prediction); an
        absent or out-of-vocabulary value still does not. Recorded on unfixed
        code as ``Normal-images-...``, ``Anomaly-images-...`` and
        ``images-...``.

        Validates: Requirements 3.16
        """
        rows = [{
            "inputImageFilePath": self.image_path,
            "prediction": constants.ANOMALY,
            "confidence": 0.9,
            "inferenceCreationTime": 1234567890,
            "humanClassification": None,
            "textNote": None,
        }]
        with patch("utils.server_setup.inference_result_accessor."
                   "list_inference_result_data_for_retraining", return_value=rows), \
             patch("endpoints.download_file.DDA_SYSTEM_FOLDER", self.dir):
            for prediction_result, prefixed in ((None, False),
                                                (constants.NORMAL, True),
                                                (constants.ANOMALY, True),
                                                (DETECTION, False)):
                params = ({} if prediction_result is None
                          else {"predictionResult": prediction_result})
                response = self.client.get(
                    "/workflows/wf-dl/results/export", params=params)
                assert response.status_code == 200, (
                    "Requirement 3.16: the export route answered {} for {!r}"
                    .format(response.status_code, prediction_result)
                )
                disposition = response.headers.get("content-disposition", "")
                expected_stem = ("{}-images-wf-dl-".format(prediction_result)
                                 if prefixed else "images-wf-dl-")
                assert "filename={}".format(expected_stem) in disposition, (
                    "Requirement 3.16 / 2.7: the download prediction check "
                    "changed for {!r}; got {!r}".format(prediction_result,
                                                        disposition)
                )
        assert constants.PREDICTION == [constants.NORMAL, constants.ANOMALY], (
            "Requirement 2.7: constants.PREDICTION -- which the download check "
            "reads -- must not be widened; got {!r}".format(constants.PREDICTION)
        )


# ---------------------------------------------------------------------------
# Requirements 3.9 / 3.19: the ORM enum readback and the absent migration
# ---------------------------------------------------------------------------

class TestOrmEnumReadbackUnchanged(LocalServerBaseTestCase):
    """``dao/sqlite_db/models.py`` types ``prediction`` and
    ``humanClassification`` as ``Enum(ANOMALY, NORMAL)`` with LITERAL members.
    Task 6 must widen the ``prediction`` one, because SQLAlchemy raises
    ``LookupError`` on READBACK for an unlisted value and
    ``store_inference_result`` ends in a ``db.refresh``. These assertions pin
    what must survive that widening."""

    def setUp(self):
        super().setUp()
        self.session = Session(self.metadata_engine)

    def tearDown(self):
        self.session.close()
        super().tearDown()

    # Property 4: storedRow'(X) = storedRow(X) for prediction != DETECTION
    # Validates: Requirements 3.9, 3.10
    def test_normal_and_anomaly_still_store_and_read_back(self):
        """Both of today's prediction values -- and both human-classification
        values -- still round-trip through the REAL accessor, the ORM enum and
        the listing, field-for-field.

        Validates: Requirements 3.9, 3.10
        """
        from dao.sqlite_db.models import InferenceResult
        from resources.accessors.inference_result_accessor import InferenceResultAccessor

        accessor = InferenceResultAccessor()
        workflow_id = "wf-" + uuid.uuid4().hex[:8]
        try:
            for prediction in (constants.NORMAL, constants.ANOMALY):
                row = _db_row(captureId="{}-{}".format(workflow_id, prediction),
                              workflowId=workflow_id, prediction=prediction,
                              humanClassification=prediction,
                              humanReviewRequired=True,
                              textNote="a note",
                              modelConfidenceThresholds={"AnomalyThreshold": 0.9})
                capture_id = accessor.store_inference_result(self.session, row)
                stored = self.session.get(InferenceResult, capture_id)
                assert stored is not None
                for field, value in row.items():
                    assert getattr(stored, field) == value, (
                        "Requirement 3.9 / 3.10: field {!r} changed on the round "
                        "trip through storage: {!r} -> {!r}".format(
                            field, value, getattr(stored, field))
                    )

            listed = accessor.list_inference_results(self.session, workflow_id)
            assert sorted(entry.prediction for entry in listed) == [
                constants.ANOMALY, constants.NORMAL]
        finally:
            self.session.rollback()
            self.session.query(InferenceResult).filter(
                InferenceResult.workflowId == workflow_id).delete()
            self.session.commit()

    # Validates: Requirements 3.19
    def test_prediction_column_needs_no_schema_migration(self):
        """The emitted sqlite DDL for ``prediction`` is a bare VARCHAR with NO
        CHECK constraint (recorded: ``prediction VARCHAR(7)``), so widening the
        ORM enum is a Python-side change with no migration -- Requirement
        3.19's "no database schema migration" holds. The VARCHAR length is
        deliberately not pinned: it is derived from the longest enum member and
        sqlite ignores it.

        Validates: Requirements 3.19
        """
        from sqlalchemy.schema import CreateTable
        from dao.sqlite_db.models import InferenceResult

        ddl = str(CreateTable(InferenceResult.__table__).compile(self.metadata_engine))
        assert re.search(r"\bprediction VARCHAR(\(\d+\))?", ddl), (
            "the prediction column is no longer a plain VARCHAR: {!r}".format(ddl)
        )
        assert "CHECK" not in ddl.upper(), (
            "Requirement 3.19: a CHECK constraint appeared on "
            "inference_result_metadata, which would make widening the stored "
            "prediction vocabulary a migration: {!r}".format(ddl)
        )


# ---------------------------------------------------------------------------
# Requirement 3.18: Normal / Anomaly rendering is unchanged on both views
# ---------------------------------------------------------------------------

def test_normal_and_anomaly_history_rendering_is_unchanged():
    """``ClassificationTypeTag`` keeps rendering ``Normal`` as a success
    indicator on the history page. Requirement 2.10 changes only the label of
    the NON-Normal branch (today hardcoded "Anomaly"), so the Normal branch here
    is the preservation half.

    Asserted at the SOURCE level on purpose: the device HMI has no jest /
    vitest suite and standing one up is out of scope. Task 8 verifies the
    rendered Normal and Anomaly tags on device.

    Validates: Requirements 3.18
    """
    with open(_COLORED_INFERENCE_BOX_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    marker = "function ClassificationTypeTag"
    assert marker in source, "ClassificationTypeTag not found in ColoredInferenceBox.tsx"
    tag_body = source[source.index(marker):]

    assert "classification === PredictionType.Normal" in tag_body, (
        "Requirement 3.18: the Normal branch's condition changed"
    )
    assert "<StatusIndicator>Normal</StatusIndicator>" in tag_body, (
        "Requirement 3.18: the Normal tag on the history page must keep "
        "rendering the default (success) StatusIndicator labelled 'Normal'"
    )
    assert 'type="error"' in tag_body, (
        "Requirement 3.18: the non-Normal branch must keep the error "
        "StatusIndicator type -- Requirement 2.10 changes only its LABEL"
    )
