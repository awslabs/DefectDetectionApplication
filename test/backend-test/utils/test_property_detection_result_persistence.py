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
"""Bug condition exploration test for detection result persistence (Defect B).

Spec: ``static-camera-pixel-format-and-detection-results`` -- **Property 3: Bug
Condition / Fix Checking**, Defect B.

**Validates: Requirements 2.6, 2.7, 2.8, 2.9, 2.10, 2.11**

THESE TESTS ARE EXPECTED TO FAIL ON UNFIXED CODE. The failures ARE the result:
they are the counterexamples proving the defect exists. They encode the EXPECTED
behavior (bugfix.md 2.6 - 2.11) and become the fix's validation in task 6.5.
Do not "fix" a failure here by weakening an assertion.

The defect
----------
The marshal types an object-detection capture distinctly and deliberately::

    inf_result["Inference result"] = "Detection"

(``src/backend/dda_triton/resources_for_copy/marshal_for_capture_template.py``
line 399 -- "never labeled Anomaly/Normal"). ``convert_inference_res_to_save_in_db``
SUCCEEDS and produces a row carrying ``prediction: "Detection"``. Storage then
rejects it: ``InferenceResultAccessor.store_inference_result``
(``resources/accessors/inference_result_accessor.py`` line 58,
``result = self.schema.load(data)``) raises::

    marshmallow.exceptions.ValidationError: {'prediction': ['Must be one of: Normal, Anomaly.']}

because ``InferenceResultSchema.prediction`` is
``fields.Str(validate=validate.OneOf(PREDICTION), required=True)``
(``model/inference_result.py`` line 83) and ``PREDICTION = ['Normal', 'Anomaly']``
(``utils/constants.py`` line 53). It is caught at line 65 and re-raised as
``HTTPException: 400: Unable to store inference result.`` inside the FastAPI
BackgroundTask scheduled at ``endpoints/workflow.py`` line 172 -- AFTER the
response is sent -- so the user sees a correct rendered result, no error, and an
empty history.

A second, independent defect (bugfix.md 1.15): ``get_inference_result_summary``
(``dao/sqlite_db/inference_result_dao.py``) counts only Normal and Anomaly and
derives ``totalInference`` from those two, so a Detection row is counted nowhere
and the total under-reports. That stays wrong after the validation fix.

Live counterexample reproduced on ``jetson-thor1`` (LocalServer arm64JP7 1.0.24):
``POST /workflows/pagb7vj8/run`` returned HTTP 200 with a 5-object detection
result (capture ``db9721b27fc54b8f993fb06ef8738631``, model
``model-yolo-test-jetson-xavier-jp7``) while ``GET /workflows/pagb7vj8/results``
stayed ``{"total":0,...}`` and ``.../results/summary`` stayed
``{"stats":{"totalInference":0,"normal":0,"anomaly":0},...}`` with no detection
bucket.

Scope of the property
---------------------
The defect is deterministic in ``prediction``, so the property is SCOPED to the
detection prediction value and generates everything else: arbitrary
``captureId``, ``confidence`` in [0, 1], ``anomalyScore``, ``anomalyThreshod``,
``detection_count``, arbitrary numbers of detections with arbitrary class labels
and bounding boxes, and arbitrary ``humanReviewRequired``.

Conventions
-----------
* **hypothesis** (not fast-check), run in the flask-app x86 container. Root
  ``conftest.py`` profiles: ``fast`` = 25 examples, ``HYPOTHESIS_PROFILE=ci`` =
  100. ``LocalServerBaseTestCase`` is the harness for anything needing the app,
  the metadata database, or the TestClient (mirrors
  ``utils/test_inference_results_utils.py`` and
  ``resources/test_inference_result_accessor.py``);
  ``suppress_health_check=[HealthCheck.function_scoped_fixture]`` follows
  ``preservation/test_preservation_fastapi_endpoints.py``, since ``setUp`` runs
  once per test method and is reused across hypothesis examples.
* Schema-only properties are plain module-level hypothesis tests -- they need
  neither the app nor a database.
* **Container invocation.** Use the FLIPPED interpreter order
  (``python3.10 || python3.11``): ``flask-app:latest`` is the JP6-layout image,
  so the documented ``python3.11 || python3.10`` shim picks a dep-less 3.11.
  ``LD_LIBRARY_PATH`` must also include ``/opt/tritonserver/lib`` -- otherwise
  ``LocalServerBaseTestCase.setUp``'s ``from app import app`` dies on
  ``ImportError: libtritonserver.so``, which is environmental and hits the
  EXISTING suites identically (``resources/test_inference_result_accessor.py``
  goes 18-failed -> 18-passed with it)::

    docker run --rm -v "$(pwd)":/repo -w /repo \
      -e PYTHONPATH=/repo/src/backend:/repo/test/backend-test \
      -e LD_LIBRARY_PATH=/opt/tritonserver/lib:/usr/local/cuda/lib64 \
      flask-app:latest bash -lc 'PY=$(command -v python3.10 || command -v python3.11); \
        $PY -m pip install --no-cache-dir --quiet pytest hypothesis sarge testfixtures; \
        $PY -m pytest test/backend-test/utils/test_property_detection_result_persistence.py \
          -q -p no:cacheprovider'

Frontend gap (Requirement 2.10), deliberately NOT a new test framework
----------------------------------------------------------------------
``src/frontend/src/components/result-history/ColoredInferenceBox.tsx``
``ClassificationTypeTag`` renders ``Normal`` as a success indicator and hardcodes
the label ``Anomaly`` for EVERYTHING else, so a persisted Detection row would
read "Anomaly". The device HMI has **no jest / vitest suites** (no ``.test.tsx``
files under ``src/frontend/src/components/``) and standing one up is out of
scope, so that one line is asserted here at the SOURCE level only
(``test_history_page_label_is_not_hardcoded_anomaly``) and is verified visually
on device in task 8 step (l).

Note for task 6 -- the ORM enum, surfaced by this test
------------------------------------------------------
``dao/sqlite_db/models.py`` declares
``prediction = Column(Enum(ANOMALY, NORMAL, name="enum_prediction_type"))`` with
literal members, NOT ``PREDICTION``. SQLAlchemy 2.0.21 accepts an unlisted string
on INSERT and counts it in SQL, but ``Enum._object_value_for_elem`` raises
``LookupError: 'Detection' is not among the defined enum values`` on READBACK --
and the DAO's ``store_inference_result`` ends with ``db.refresh(...)``, which is
a readback. So widening ``InferenceResultSchema.prediction`` alone is not
sufficient for Requirement 2.8 ("that row SHALL appear in
``GET /workflows/{id}/results``"): the ORM enum must accept Detection too. That
widening needs no DDL change and no migration -- verified in the container that
the emitted sqlite DDL is a bare ``VARCHAR`` with NO CHECK constraint -- so
Requirement 3.19's "no database schema migration" still holds.
"""
import base64
import json
import logging
import os
import tempfile
import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from marshmallow import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from unittest.mock import patch

from local_server_base_test_case import LocalServerBaseTestCase
from model.inference_result import CapturedDataSchema, InferenceResultSchema
from utils import constants
# Imported at MODULE level on purpose, mirroring
# ``utils/test_inference_results_utils.py``. The flask-app image also carries a
# baked-in copy of the backend at ``/`` whose ``dda_triton`` predates
# ``provider_visibility``; importing the repo's ``utils.*`` (and through it
# ``dda_triton``) during collection pins the repo copies in ``sys.modules``
# before anything resolves the image copy. A lazy in-test import instead fails
# with ``ModuleNotFoundError: No module named 'dda_triton.provider_visibility'``.
from utils.inference_results_utils import (
    GetInferenceResults,
    convert_inference_res_to_save_in_db,
)

# The stored prediction value the marshal emits for a detection capture. Kept as
# a literal because ``constants.DETECTION`` does not exist on unfixed code;
# ``test_marshal_still_emits_the_detection_prediction`` pins it against the
# marshal source so a drift fails loudly instead of silently.
DETECTION = "Detection"

# Confirmed live values from ``jetson-thor1`` (bugfix.md 1.13).
LIVE_CAPTURE_ID = "db9721b27fc54b8f993fb06ef8738631"
LIVE_WORKFLOW_ID = "pagb7vj8"
LIVE_MODEL_ID = "model-yolo-test-jetson-xavier-jp7"
LIVE_CONFIDENCE = 0.8879303932189941
LIVE_ANOMALY_SCORE = 0.8879303932189941
LIVE_ANOMALY_THRESHOLD = 1.0
LIVE_DETECTION_COUNT = 5
LIVE_INFERENCE_TIME = "2025-01-01T00:00:00"

_MARSHAL_TEMPLATE_PATH = os.path.join(
    os.getcwd(), "src", "backend", "dda_triton", "resources_for_copy",
    "marshal_for_capture_template.py",
)
_COLORED_INFERENCE_BOX_PATH = os.path.join(
    os.getcwd(), "src", "frontend", "src", "components", "result-history",
    "ColoredInferenceBox.tsx",
)

_MODEL_CONFIG = {
    "modelAlias": "yolo-test",
    "modelMetaData": "someMetadata",
    "modelVersion": "1",
    "modelConfidenceThresholds": {"AnomalyThreshold": "0.9", "NormalThreshold": "0.8"},
}


# ---------------------------------------------------------------------------
# Generators -- constrained to the real input space
# ---------------------------------------------------------------------------

_capture_ids = st.text(alphabet="0123456789abcdef", min_size=8, max_size=32)
_ids = st.text(alphabet="0123456789abcdefghijklmnopqrstuvwxyz-", min_size=4, max_size=24)
_paths = st.builds(
    lambda stem: "/aws_dda/image-capture/{}.jpg".format(stem),
    st.text(alphabet="0123456789abcdef", min_size=4, max_size=16),
)
# Confidence is a probability; anomaly score / threshold are the model's own
# [0, 1] values (the live detection capture carries threshold 1.0).
_unit_floats = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False, width=32
)
_creation_times = st.integers(min_value=0, max_value=2_000_000_000)


@st.composite
def _detections_blocks(draw):
    """The marshal's ``{"0": {class_index, class_label, bounding_box, confidence}}``
    detections map, with an arbitrary number of arbitrary boxes and labels."""
    count = draw(st.integers(min_value=0, max_value=6))
    block = {}
    for idx in range(count):
        block[str(idx)] = {
            "class_index": str(draw(st.integers(min_value=0, max_value=90))),
            "class_label": draw(st.text(max_size=12)),
            "bounding_box": draw(
                st.lists(
                    st.floats(
                        min_value=0.0, max_value=4096.0, allow_nan=False,
                        allow_infinity=False, width=32,
                    ),
                    min_size=4, max_size=4,
                )
            ),
            "confidence": draw(_unit_floats),
        }
    return block


@st.composite
def _detection_rows(draw):
    """A stored-row-shaped detection result: exactly the keys
    ``convert_inference_res_to_save_in_db`` emits for a detection capture, all
    carrying ``prediction: "Detection"``."""
    return {
        "captureId": draw(_capture_ids),
        "captureType": constants.INFERENCE,
        "workflowId": draw(_ids),
        "inferenceCreationTime": draw(_creation_times),
        "prediction": DETECTION,
        "confidence": draw(_unit_floats),
        "anomalyScore": draw(_unit_floats),
        "anomalyThreshod": draw(_unit_floats),
        "inputImageFilePath": draw(_paths),
        "outputImageFilePath": draw(_paths),
        "modelId": draw(_ids),
        "modelName": draw(_ids),
        "flagForReview": False,
        "downloaded": False,
        "humanReviewRequired": draw(st.booleans()),
        "modelConfidenceThresholds": {"AnomalyThreshold": 0.9, "NormalThreshold": 0.1},
    }


@st.composite
def _detection_inference_results(draw):
    """The ``inference_res`` shape ``GetInferenceResults.save_image_object``
    returns for a detection capture, ready for
    ``convert_inference_res_to_save_in_db``. Carries the generated detections
    block and ``detection_count`` the way the detection branch does."""
    detections = draw(_detections_blocks())
    return {
        "captureId": draw(_capture_ids),
        "captureType": constants.INFERENCE,
        "creationTime": "2025-01-01T00:00:00",
        "imageDataFilePath": draw(_paths),
        "inputImageFilePath": draw(_paths),
        "inferenceFilePath": draw(_paths),
        "humanReviewRequired": draw(st.booleans()),
        "inferenceResult": {
            "confidence": draw(_unit_floats),
            "inference_result": DETECTION,
            "anomaly_score": draw(_unit_floats),
            "anomaly_threshold": draw(_unit_floats),
            "detections": detections,
            "detection_count": len(detections),
        },
    }


def _workflow(workflow_id, model_id):
    return {
        "workflowId": workflow_id,
        "name": "yolotest",
        "featureConfigurations": [{"type": "LFVModel", "modelName": model_id}],
        "outputConfigurations": [],
        "inputConfigurations": [],
        "workflowOutputPath": "/aws_dda/yolotest",
    }


def _normal_row(**overrides):
    """A minimal, valid Normal row -- the baseline the scoping assertions vary."""
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


def _assert_validates_and_round_trips(row):
    """``InferenceResultSchema().load(row)`` succeeds and every field other than
    ``prediction`` round-trips unchanged (Requirement 2.6)."""
    schema = InferenceResultSchema()
    try:
        loaded = schema.load(row)
    except ValidationError as err:
        raise AssertionError(
            "Requirement 2.6: a detection row must validate for storage, but "
            "InferenceResultSchema().load() rejected it.\n"
            "  counterexample row: {!r}\n"
            "  ValidationError: {!r}".format(row, err.messages)
        ) from err

    assert loaded.prediction == DETECTION, (
        "the stored prediction must remain the marshal's own value {!r}, got {!r}"
        .format(DETECTION, loaded.prediction)
    )
    dumped = schema.dump(loaded)
    for field, value in row.items():
        if field == "prediction":
            continue
        assert dumped[field] == value, (
            "field {!r} did not round-trip unchanged: {!r} -> {!r}"
            .format(field, value, dumped[field])
        )


# ---------------------------------------------------------------------------
# Property 3, part b1: a detection row validates for storage (Requirement 2.6)
# ---------------------------------------------------------------------------

# Property 3: Bug Condition (Defect B) -- validatesForStorage'(X.row) = TRUE
# Validates: Requirements 2.6
@settings(max_examples=25, deadline=None)
@given(row=_detection_rows())
def test_detection_row_validates_for_storage(row):
    """Every generated detection row validates and round-trips unchanged.

    On unfixed code this raises
    ``ValidationError: {'prediction': ['Must be one of: Normal, Anomaly.']}``
    for EVERY generated row (``model/inference_result.py`` line 83 against
    ``PREDICTION`` in ``utils/constants.py`` line 53).

    Validates: Requirements 2.6
    """
    _assert_validates_and_round_trips(row)


# Property 3: Bug Condition (Defect B) -- the db marshal's output validates
# Validates: Requirements 2.6, 2.8
@settings(max_examples=25, deadline=None)
@given(inference_res=_detection_inference_results())
def test_db_marshalled_detection_result_validates_for_storage(inference_res):
    """``convert_inference_res_to_save_in_db`` SUCCEEDS for a detection result
    (it is not the failure point, bugfix.md 1.10) and the row it produces
    validates for storage.

    Arbitrary ``detection_count`` and arbitrary detections with arbitrary class
    labels and bounding boxes are generated as inputs; they are correctly not
    persisted (the stored row has no such columns), and the fix must not change
    that.

    Validates: Requirements 2.6, 2.8
    """
    workflow = _workflow("wf-" + uuid.uuid4().hex[:8], LIVE_MODEL_ID)
    with patch(
        "utils.inference_results_utils.get_default_configs_lfv",
        return_value=_MODEL_CONFIG,
    ):
        row = convert_inference_res_to_save_in_db(inference_res, workflow)

    assert row["prediction"] == DETECTION, (
        "bugfix.md 1.10: the db marshal must keep typing the capture "
        "{!r}; got {!r}".format(DETECTION, row["prediction"])
    )
    assert "detections" not in row and "detection_count" not in row, (
        "the detections block is not a stored column and must not leak into the row"
    )
    _assert_validates_and_round_trips(row)


def test_live_detection_row_validates_for_storage():
    """The exact row the live ``jetson-thor1`` failure produced validates.

    Concrete counterexample from bugfix.md 1.13 / 1.11.

    Validates: Requirements 2.6
    """
    _assert_validates_and_round_trips({
        "captureId": LIVE_CAPTURE_ID,
        "captureType": constants.INFERENCE,
        "workflowId": LIVE_WORKFLOW_ID,
        "inferenceCreationTime": 1786814771,
        "prediction": DETECTION,
        "confidence": LIVE_CONFIDENCE,
        "anomalyScore": LIVE_ANOMALY_SCORE,
        "anomalyThreshod": LIVE_ANOMALY_THRESHOLD,
        "inputImageFilePath": "/aws_dda/yolotest/bus.jpg",
        "outputImageFilePath":
            "/aws_dda/image-capture/{}.overlay.jpg".format(LIVE_CAPTURE_ID),
        "modelId": LIVE_MODEL_ID,
        "modelName": "yolo-test",
        "flagForReview": False,
        "downloaded": False,
        "humanReviewRequired": True,
    })


# ---------------------------------------------------------------------------
# Requirement 2.7: the widening is SCOPED to the stored prediction field
# ---------------------------------------------------------------------------

# Property 3: allowedHumanClassification'() = allowedHumanClassification()
# Validates: Requirements 2.7
def test_inference_result_human_classification_still_rejects_detection():
    """A human verdict stays binary: ``humanClassification`` must NOT be widened
    on ``InferenceResultSchema`` (Requirement 2.7, 3.13)."""
    with pytest.raises(ValidationError) as excinfo:
        InferenceResultSchema().load(_normal_row(humanClassification=DETECTION))
    assert "humanClassification" in excinfo.value.messages


# Property 3: allowedHumanClassification'() = allowedHumanClassification()
# Validates: Requirements 2.7
def test_captured_data_human_classification_still_rejects_detection():
    """Same for ``CapturedDataSchema`` (Requirement 2.7, 3.13)."""
    with pytest.raises(ValidationError) as excinfo:
        CapturedDataSchema().load({
            "captureId": "12345",
            "captureType": constants.CAPTURE,
            "workflowId": "fake-wf-id",
            "inferenceCreationTime": 123456,
            "inputImageFilePath": "path",
            "humanClassification": DETECTION,
        })
    assert "humanClassification" in excinfo.value.messages


# Property 3: OUTPUT_RULE' = OUTPUT_RULE
# Validates: Requirements 2.7
def test_scoped_vocabularies_are_unchanged():
    """``OUTPUT_RULE``, ``PREDICTION`` and ``CAPTURE_TYPE`` stay exactly as they
    are; only the STORED prediction vocabulary grows (Requirement 2.7)."""
    assert constants.OUTPUT_RULE == ["All", constants.NORMAL, constants.ANOMALY]
    assert constants.PREDICTION == [constants.NORMAL, constants.ANOMALY]
    assert constants.CAPTURE_TYPE == [constants.CAPTURE, constants.INFERENCE]


def test_marshal_still_emits_the_detection_prediction():
    """The marshal keeps typing a detection capture ``"Detection"``
    (``marshal_for_capture_template.py`` line 399). The fix widens what the
    STORE accepts, never what the marshal produces (Requirement 3.11), so this
    also pins the literal this module scopes its property to."""
    with open(_MARSHAL_TEMPLATE_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert 'inf_result["Inference result"] = "Detection"' in source


# ---------------------------------------------------------------------------
# Requirement 2.10: the history page must not hardcode the Anomaly label
# ---------------------------------------------------------------------------

def test_history_page_label_is_not_hardcoded_anomaly():
    """``ClassificationTypeTag`` must render the classification's OWN value for
    the non-Normal branch, mirroring the live card
    (``LiveResultCard.tsx``: ``<StatusIndicator type={predictionType}>{prediction}</StatusIndicator>``).

    Today it hardcodes ``<StatusIndicator type="error">Anomaly</StatusIndicator>``,
    so a persisted Detection row would read "Anomaly".

    Asserted at the SOURCE level on purpose: the device HMI has no jest / vitest
    suite and standing one up is out of scope. Task 8 step (l) verifies the
    rendered label on device.

    Validates: Requirements 2.10
    """
    with open(_COLORED_INFERENCE_BOX_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    marker = "function ClassificationTypeTag"
    assert marker in source, "ClassificationTypeTag not found in ColoredInferenceBox.tsx"
    tag_body = source[source.index(marker):]

    assert ">Anomaly<" not in tag_body, (
        "Requirement 2.10: ClassificationTypeTag hardcodes the label 'Anomaly' for "
        "every non-Normal prediction, so a Detection row renders as 'Anomaly'. It "
        "must render the classification value itself."
    )
    assert "{classification}" in tag_body, (
        "Requirement 2.10: ClassificationTypeTag must render the classification "
        "value, the way LiveResultCard renders {prediction}."
    )


# ---------------------------------------------------------------------------
# Requirements 2.8 / 2.9: the row stores, is readable, and is counted
# ---------------------------------------------------------------------------

class TestDetectionResultPersistence(LocalServerBaseTestCase):
    """Storage and summary against the REAL accessor / DAO over the metadata
    database created by ``LocalServerBaseTestCase``."""

    def setUp(self):
        super().setUp()
        from resources.accessors.inference_result_accessor import InferenceResultAccessor

        self.session = Session(self.metadata_engine)
        self.accessor = InferenceResultAccessor()

    def tearDown(self):
        self.session.close()
        super().tearDown()

    def _add_rows(self, workflow_id, predictions):
        from dao.sqlite_db.models import InferenceResult

        for index, prediction in enumerate(predictions):
            self.session.add(InferenceResult(**_normal_row(
                captureId="{}-{}".format(workflow_id, index),
                workflowId=workflow_id,
                prediction=prediction,
                inferenceCreationTime=1000 + index,
            )))
        self.session.commit()

    def _drop_rows(self, workflow_id):
        from dao.sqlite_db.models import InferenceResult

        self.session.query(InferenceResult).filter(
            InferenceResult.workflowId == workflow_id
        ).delete()
        self.session.commit()

    # Property 3: Bug Condition (Defect B) -- storedRow'(X).prediction = DETECTION
    # Validates: Requirements 2.6, 2.8
    @settings(max_examples=25, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(row=_detection_rows())
    def test_detection_row_stores_and_reads_back(self, row):
        """The REAL ``store_inference_result`` persists exactly one detection row
        per capture and that row reads back with ``prediction: "Detection"``, so
        it can appear in ``GET /workflows/{id}/results``.

        On unfixed code this raises ``HTTPException: 400: Unable to store
        inference result. {'prediction': ['Must be one of: Normal, Anomaly.']}``
        (``inference_result_accessor.py`` lines 58 / 65).

        Validates: Requirements 2.6, 2.8
        """
        from dao.sqlite_db.models import InferenceResult

        row = dict(row)
        row["captureId"] = uuid.uuid4().hex
        row["workflowId"] = "wf-" + uuid.uuid4().hex[:8]
        try:
            capture_id = self.accessor.store_inference_result(self.session, row)
            assert capture_id == row["captureId"]

            stored = self.session.get(InferenceResult, row["captureId"])
            assert stored is not None, "no row was written for the detection capture"
            assert stored.prediction == DETECTION, (
                "the stored row must read back as {!r}, got {!r}"
                .format(DETECTION, stored.prediction)
            )
            for field in ("confidence", "anomalyScore", "anomalyThreshod",
                          "inputImageFilePath", "outputImageFilePath",
                          "modelId", "humanReviewRequired"):
                assert getattr(stored, field) == row[field], (
                    "field {!r} changed on the round trip through storage: {!r} -> {!r}"
                    .format(field, row[field], getattr(stored, field))
                )

            listed = self.accessor.list_inference_results(self.session, row["workflowId"])
            assert [entry.captureId for entry in listed] == [row["captureId"]], (
                "exactly one row per capture must be listed for the workflow"
            )
        finally:
            self.session.rollback()
            self._drop_rows(row["workflowId"])

    # Property 3: Bug Condition (Defect B) -- part b2, the summary counts detections
    # Validates: Requirements 2.9
    @settings(max_examples=25, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        normal=st.integers(min_value=0, max_value=4),
        anomaly=st.integers(min_value=0, max_value=4),
        detection=st.integers(min_value=1, max_value=4),
    )
    def test_summary_counts_detection_rows(self, normal, anomaly, detection):
        """For a workflow whose rows include Detection rows, the summary reports
        a ``detection`` count equal to the Detection row count, and
        ``totalInference`` equals ``normal + anomaly + detection`` equals the
        total row count in the window.

        On unfixed code there is no ``detection`` key at all and
        ``totalInference`` is ``normal + anomaly``, excluding the detections
        entirely (bugfix.md 1.15).

        Validates: Requirements 2.9
        """
        from dao.sqlite_db import inference_result_dao
        from dao.sqlite_db.models import InferenceResult

        workflow_id = "wf-" + uuid.uuid4().hex[:8]
        predictions = (
            [constants.NORMAL] * normal
            + [constants.ANOMALY] * anomaly
            + [DETECTION] * detection
        )
        try:
            self._add_rows(workflow_id, predictions)
            rows_in_window = self.session.scalar(
                select(func.count())
                .where(InferenceResult.workflowId == workflow_id)
                .where(InferenceResult.inferenceCreationTime >= 0)
            )
            assert rows_in_window == len(predictions)

            stats = inference_result_dao.get_inference_result_summary(
                self.session, workflow_id, 0)

            assert stats.get("detection") == detection, (
                "Requirement 2.9: the summary must report a 'detection' count of "
                "{}; got {!r} from stats {!r}".format(
                    detection, stats.get("detection"), stats)
            )
            assert stats["normal"] == normal
            assert stats["anomaly"] == anomaly
            assert stats["totalInference"] == (
                stats["normal"] + stats["anomaly"] + stats["detection"]
            ), (
                "Requirement 2.9: totalInference must be the sum of all three "
                "buckets; got {!r}".format(stats)
            )
            assert stats["totalInference"] == rows_in_window, (
                "Requirement 2.9: totalInference must count every row in the "
                "window ({}); got {!r}".format(rows_in_window, stats)
            )

            envelope = self.accessor.get_inference_result_summary(
                self.session, workflow_id, 0)
            assert set(envelope) == {"stats", "lastResetTime"}, (
                "the summary envelope must stay {'stats', 'lastResetTime'} "
                "(Requirement 3.15)"
            )
            assert envelope["lastResetTime"] == 0
        finally:
            self.session.rollback()
            self._drop_rows(workflow_id)

    # Property 3: resultsListPredictionFilterValues'() = resultsListPredictionFilterValues()
    # Validates: Requirements 2.7
    def test_results_list_prediction_filter_still_accepts_only_normal_and_anomaly(self):
        """The ``prediction`` query parameter on ``GET /workflows/{id}/results``
        is NOT widened: only ``Normal`` / ``Anomaly`` are accepted
        (Requirement 2.7, 3.14)."""
        import typing

        from endpoints.inference_result import list_inference_results

        annotation = typing.get_type_hints(list_inference_results)["prediction"]
        literals = [
            arg for arg in typing.get_args(annotation) if arg is not type(None)
        ]
        assert len(literals) == 1
        assert set(typing.get_args(literals[0])) == {constants.NORMAL, constants.ANOMALY}

        # The app maps RequestValidationError to 400, not FastAPI's default 422
        # (``exceptions/handlers/exception_handlers.py``
        # ``request_validation_exception_handler``), so derive the rejection
        # status from a CONTROL request on another Literal that is not being
        # widened rather than pinning a literal status here.
        control = self.client.get(
            "/workflows/fake-wf-id/results",
            params={"captureType": "NotACaptureType"},
        )
        rejected_status = control.status_code
        assert rejected_status in (400, 422), (
            "unexpected status for an invalid query Literal: {}".format(rejected_status)
        )

        rejected = self.client.get(
            "/workflows/fake-wf-id/results", params={"prediction": DETECTION})
        assert rejected.status_code == rejected_status, (
            "Requirement 2.7 / 3.14: the results-list prediction filter must "
            "still reject {!r}; got {}".format(DETECTION, rejected.status_code)
        )
        assert "prediction" in rejected.text

        for accepted in (constants.NORMAL, constants.ANOMALY):
            response = self.client.get(
                "/workflows/fake-wf-id/results", params={"prediction": accepted})
            assert not (
                response.status_code == rejected_status
                and "prediction" in response.text
            ), "{!r} must still be an accepted prediction filter value".format(accepted)


# ---------------------------------------------------------------------------
# Requirements 2.6 / 2.8: end to end, marshal -> store, with the live payload
# ---------------------------------------------------------------------------

class TestDetectionMarshalToStore(LocalServerBaseTestCase):
    """The real ``deviceFleetAuxiliaryOutputs`` / ``deviceFleetAuxiliaryInputs``
    shape the marshal emits for ``task=object_detection``, run through
    ``GetInferenceResults.save_image_object`` and
    ``convert_inference_res_to_save_in_db``."""

    def setUp(self):
        super().setUp()
        self.capture_dir = tempfile.mkdtemp(prefix="detection-capture-")
        self.input_path = os.path.join(self.capture_dir, "bus.jpg")
        self.overlay_path = os.path.join(
            self.capture_dir, "{}.overlay.jpg".format(LIVE_CAPTURE_ID))
        # save_image_object base64-reads the surfaced image off disk, so both
        # files must really exist.
        for path in (self.input_path, self.overlay_path):
            with open(path, "wb") as handle:
                handle.write(b"\xff\xd8\xff\xd9")

    def _capture_record(self):
        """Exactly the aux-output shape ``_generate_capture_meta_data`` emits for
        a detection capture: overlay data-ref, the base64 ``json`` inference
        summary, and the base64 ``json_with_base64_encoding`` detections block."""
        inf_result = {
            "Inference status": "success",
            "Inference result": DETECTION,
            "Detection_count": LIVE_DETECTION_COUNT,
            "Confidence": LIVE_CONFIDENCE,
            "Anomaly_score": LIVE_ANOMALY_SCORE,
            "Anomaly_threshold": LIVE_ANOMALY_THRESHOLD,
            "Error msg": "",
        }
        # The live capture: four persons plus a bus (bugfix.md 1.13).
        labels = ["person", "person", "person", "person", "bus"]
        detections = {
            str(index): {
                "class_index": str(index),
                "class_label": label,
                "bounding_box": [10.0 * index, 20.0 * index, 100.0, 200.0],
                "confidence": LIVE_CONFIDENCE,
            }
            for index, label in enumerate(labels)
        }
        encode = lambda payload: base64.b64encode(
            json.dumps(payload).encode()).decode()
        return {
            "deviceFleetAuxiliaryInputs": [
                {"data-ref": "file://{}".format(self.input_path),
                 "encoding": "NONE", "observedContentType": "jpg"},
            ],
            "deviceFleetAuxiliaryOutputs": [
                {"data-ref": "file://{}".format(self.overlay_path),
                 "encoding": "NONE", "observedContentType": "overlay.jpg"},
                {"data": encode(inf_result), "encoding": "BASE64",
                 "observedContentType": "json"},
                {"data": encode({"detections": detections}), "encoding": "BASE64",
                 "observedContentType": "json_with_base64_encoding"},
            ],
            "eventMetadata": {
                "capture_folder": self.capture_dir,
                "eventId": LIVE_CAPTURE_ID,
                "deviceFleetName": "fleet-A",
                "modelName": LIVE_MODEL_ID,
                "modelVersion": "1",
                "inferenceTime": LIVE_INFERENCE_TIME,
            },
            "eventVersion": "0",
        }

    def test_live_detection_capture_marshals_and_stores(self):
        """The confirmed live detection payload marshals to a row carrying
        ``prediction: "Detection"`` with the live ``modelId`` / ``captureId`` /
        confidence values, that row validates, and it stores.

        On unfixed code the marshal half SUCCEEDS (bugfix.md 1.10) and storage
        rejects the row (bugfix.md 1.11 / 1.12).

        Validates: Requirements 2.6, 2.8
        """
        from resources.accessors.inference_result_accessor import InferenceResultAccessor

        workflow = _workflow(LIVE_WORKFLOW_ID, LIVE_MODEL_ID)
        with patch(
            "utils.inference_results_utils.get_default_configs_lfv",
            return_value=_MODEL_CONFIG,
        ):
            query = GetInferenceResults(
                stream_id=LIVE_WORKFLOW_ID, sort="desc", starting_point=0,
                max_results=1,
            )
            inference_result = query.save_image_object(
                self._capture_record(),
                os.path.join(self.capture_dir, "{}.jsonl".format(LIVE_CAPTURE_ID)),
                capture_id=LIVE_CAPTURE_ID,
            )

            assert inference_result["inferenceResult"]["inference_result"] == DETECTION
            assert inference_result["inferenceResult"]["detection_count"] == \
                LIVE_DETECTION_COUNT
            assert inference_result["inferenceResult"]["confidence"] == LIVE_CONFIDENCE
            # The overlay is the surfaced output image for a detection capture.
            assert inference_result["imageDataFilePath"] == self.overlay_path

            # Mirror save_full_inference_result (endpoints/workflow.py line 124).
            del inference_result["image"]
            inference_result["captureType"] = constants.INFERENCE
            row = convert_inference_res_to_save_in_db(inference_result, workflow)

        assert row["prediction"] == DETECTION
        assert row["modelId"] == LIVE_MODEL_ID
        assert row["captureId"] == LIVE_CAPTURE_ID
        assert row["confidence"] == LIVE_CONFIDENCE
        assert row["anomalyScore"] == LIVE_ANOMALY_SCORE
        assert row["anomalyThreshod"] == LIVE_ANOMALY_THRESHOLD
        assert row["outputImageFilePath"] == self.overlay_path

        _assert_validates_and_round_trips(row)

        session = Session(self.metadata_engine)
        try:
            capture_id = InferenceResultAccessor().store_inference_result(session, row)
            assert capture_id == LIVE_CAPTURE_ID
        finally:
            session.rollback()
            session.close()


# ---------------------------------------------------------------------------
# Requirement 2.11: a persistence failure is loud, identifiable, and contained
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("caplog")
class TestPersistenceFailureObservability(LocalServerBaseTestCase):
    """A background-task persistence failure must name the workflow and the
    capture, and must never reach the manual-run response."""

    def test_persistence_failure_logs_workflow_and_capture_id(self):
        """``save_full_inference_result`` logs an ERROR naming the workflow id
        AND the capture id AND the exception, and does not propagate -- so the
        already-sent manual-run response is unaffected (Requirements 2.11, 3.17).

        On unfixed code there is no try/except at all: the exception escapes the
        background task and the only trace is an anonymous traceback on
        container stdout naming neither the workflow nor the capture
        (bugfix.md 1.17).

        Validates: Requirements 2.11
        """
        from endpoints import workflow as workflow_module

        workflow_id = "wf-" + uuid.uuid4().hex[:8]
        workflow = _workflow(workflow_id, LIVE_MODEL_ID)
        capture_id = uuid.uuid4().hex
        inference_result = {
            "captureId": capture_id,
            "creationTime": LIVE_INFERENCE_TIME,
            "imageDataFilePath": "/aws_dda/image-capture/out.jpg",
            "inputImageFilePath": "/aws_dda/yolotest/bus.jpg",
            "inferenceFilePath": "/aws_dda/yolotest/out.jsonl",
            "humanReviewRequired": False,
            "image": "aGVsbG8=",
            "inferenceResult": {
                "confidence": LIVE_CONFIDENCE,
                "inference_result": DETECTION,
                "anomaly_score": LIVE_ANOMALY_SCORE,
                "anomaly_threshold": LIVE_ANOMALY_THRESHOLD,
                "detections": {},
                "detection_count": 0,
            },
        }
        boom = RuntimeError("simulated persistence failure")

        # Harness self-check: prove this class really captures ERROR records from
        # the logger of the module under test, so the assertions below can only
        # fail for a MISSING log line and never for a silent caplog.
        with self.caplog.at_level(logging.ERROR):
            workflow_module.logger.error("caplog self-check %s", capture_id)
        assert any(
            capture_id in record.getMessage() for record in self.caplog.records
        ), "caplog is not capturing endpoints.workflow ERROR records"
        self.caplog.clear()

        raised = None
        with self.caplog.at_level(logging.ERROR), \
                patch("utils.inference_results_utils.get_default_configs_lfv",
                      return_value=_MODEL_CONFIG), \
                patch.object(workflow_module.inference_result_accessor,
                             "store_inference_result", side_effect=boom):
            try:
                workflow_module.save_full_inference_result(inference_result, workflow)
            except BaseException as err:  # noqa: BLE001 - recorded, asserted below
                raised = err

        errors = [
            record.getMessage()
            for record in self.caplog.records
            if record.levelno >= logging.ERROR
        ]
        named = [
            message for message in errors
            if workflow_id in message and capture_id in message
        ]
        assert named, (
            "Requirement 2.11: a persistence failure must be logged at ERROR "
            "naming the workflow id ({}) and the capture id ({}). "
            "ERROR records seen: {!r}. The persistence call raised {!r}, so the "
            "failure path really was exercised.".format(
                workflow_id, capture_id, errors, raised)
        )
        assert any(str(boom) in message for message in named), (
            "Requirement 2.11: the log line must carry the exception too; got {!r}"
            .format(named)
        )
        assert raised is None, (
            "Requirements 2.11 / 3.17: persistence must stay in a background "
            "task that cannot fail the response, so the failure must be "
            "swallowed after being logged; it propagated {!r}".format(raised)
        )
