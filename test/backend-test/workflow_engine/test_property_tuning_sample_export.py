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
"""Device property tests for Sample_Export (spec task 3.4).

Four properties of the device's tuning side channel
(``workflow_engine/tuning/sample_export.py`` and ``tuning/backfill.py``):

- **Property 2: Every completed anomaly-mode invocation exports exactly
  what was sent** — Requirements 2.2, 2.3;
- **Property 3: Export is contained, bounded and inert when
  unconfigured** — Requirements 2.4, 2.5, 2.6, 11.2, 11.3;
- **Property 4: Backfill pairs by the executor's artifact rules, once,
  within bounds** — Requirement 2.7;
- **Property 17** (device configuration half): *for any* LocalServer
  configuration shape export is disabled for every malformed shape and
  enabled with the exact location otherwise — Requirements 2.6, 11.3.

Everything is a fake: the S3 client is a dict-backed recorder, the
invokers record what they were handed, artifact trees live in temporary
directories and the backfill's registrations/executions live in a
private in-memory sqlite database. No AWS call, no device and no
network are involved.

Harness conventions follow the suite's existing processor tests
(``test_property_anomaly_invocation_preservation.py``,
``test_bedrock_response_mode.py``): a compiled document with one
binding, a recording invoker, a temp work/output directory and a real
``NodeStatusCollector``.
"""
import base64
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager

import cv2
import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from dao.sqlite_db.sqlite_db_operations import Base
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.node_status import NodeStatusCollector
from workflow_engine.output_bindings import (
    BedrockInferenceError,
    BedrockInferenceProcessor,
    LlmInferenceProcessor,
    RunContext,
    downscale_image_bytes,
)
from workflow_engine.tuning import backfill as backfill_module
from workflow_engine.tuning import sample_export as export_module
from workflow_engine.tuning.sample_export import (
    DEFAULT_QUEUE_SIZE,
    MAX_IMAGE_BYTES,
    MAX_UPLOAD_ATTEMPTS,
    SOURCE_BACKFILL,
    SOURCE_LIVE,
    ExportConfig,
    ExportContext,
    ExportedSample,
    SampleExporter,
    configure_sample_exporter,
    object_keys,
    set_sample_exporter,
)
from workflow_engine.vendor.workflow_core.anomaly_invocation import (
    prompt_fingerprint,
)

# ---------------------------------------------------------------------------
# Fixed scenario material
# ---------------------------------------------------------------------------

BUCKET = "dda-inference-results-000000000000"
PREFIX = "workflow-tuning/samples/"
THING_NAME = "dda-edge-under-test"
WORKFLOW_ID = "wf-24680"
VERSION = "7"
EXECUTION_ID = "exec-0f1e2d3c"
CAPTURE_ID = "cap-tuning"

INPUT_FRAME_NAME = "input_cam.jpg"
REFERENCE_FRAME_NAME = "reference_cam.jpg"
MISSING_NAME = "missing_frame.jpg"
INPUT_FRAME_SIZE = (120, 90)
REFERENCE_FRAME_SIZE = (64, 48)

DETECTION_ID = "det-a3f50a41"
DETECTION_SLOT = 0
DETECTION_BOX = {"x_min": 10, "y_min": 8, "x_max": 70, "y_max": 58}

VERDICT_JSON = '{"is_anomalous": true, "confidence": 0.87}'
FENCED_VERDICT = "```json\n" + VERDICT_JSON + "\n```"
UNPARSEABLE = "the plate looks fine to me"

#: A denied payload reference: the prefix gate rejects it BEFORE any
#: fetch, so no scenario here touches the network.
DENIED_REFERENCE_URI = "https://example.invalid/reference.png"
ALLOWED_URI_PREFIXES = "s3://allowed-bucket/\n"
PAYLOAD_REFERENCE_PATH = "ref.image"


def _png_bytes():
    array = np.arange(48, dtype=np.uint8).reshape(4, 4, 3)
    ok, encoded = cv2.imencode(".png", array)
    assert ok
    return encoded.tobytes()


PAYLOAD_PNG_BYTES = _png_bytes()
PAYLOAD_PNG_BASE64 = base64.b64encode(PAYLOAD_PNG_BYTES).decode("ascii")


def _write_jpeg(path, size, tint):
    """A deterministic JPEG frame so crops decode and downscales work."""
    width, height = size
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    frame[:, :, 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    frame[:, :, 2] = np.uint8(tint)
    assert cv2.imwrite(path, frame)
    with open(path, "rb") as handle:
        return handle.read()


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeS3:
    """A dict-backed S3 client recording every ``put_object``.

    ``fail_first`` puts raise before any succeed (retry behaviour);
    ``always_fail`` never accepts anything (permanent failure).
    """

    def __init__(self, fail_first=0, always_fail=False):
        self.objects = {}
        self.puts = []
        self.fail_first = int(fail_first)
        self.always_fail = bool(always_fail)

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
        self.puts.append(Key)
        if self.always_fail or self.fail_first > 0:
            self.fail_first = max(0, self.fail_first - 1)
            raise RuntimeError("fake S3 rejected " + str(Key))
        self.objects[Key] = {
            "bucket": Bucket, "body": Body, "contentType": ContentType}
        return {}


class RecordingExporter(SampleExporter):
    """The REAL exporter (real config, bounds, sidecar and key layout)
    that additionally records every sample OFFERED to :meth:`enqueue`.

    ``offered`` is what the executor handed over — identical to what was
    queued except for a sample the bound or the 8 MiB cap rejects, which
    is deliberately visible here so those two rules can be asserted.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.offered = []

    def enqueue(self, sample):
        self.offered.append(sample)
        super().enqueue(sample)


class NeverDrainingExporter(RecordingExporter):
    """A real exporter whose upload worker never starts — a stalled
    uploader, deterministically and without threads, so the queue bound
    and the drop-oldest rule are observable."""

    def start(self):  # noqa: D102 - see class docstring
        return None


class DefectiveExporter:
    """An exporter that raises on every interaction.

    Nothing about it may reach the run (Requirements 11.1, 11.2): the
    export call sites are wrapped in a bare ``except`` logged at debug.
    """

    @property
    def config(self):
        raise RuntimeError("defective exporter: config")

    def enqueue(self, sample):
        raise RuntimeError("defective exporter: enqueue")


class RecordingBedrockInvoker:
    """Records every Converse invocation verbatim (``*args``, so the
    image bytes actually sent are observable)."""

    def __init__(self, answer, error_text=None):
        self.answer = answer
        self.error_text = error_text
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"positional": list(args), "keywords": dict(kwargs)})
        if self.error_text is not None:
            raise RuntimeError(self.error_text)
        return self.answer

    @property
    def sent_images(self):
        """``[(label, bytes)]`` of the single recorded invocation."""
        assert len(self.calls) == 1, self.calls
        return list(self.calls[0]["positional"][2])


class RecordingLlmInvoker:
    """Text_Generation_API fake accepting any keyword (so the
    processor's ``metrics_sink`` gating forwards the sink)."""

    def __init__(self, answer, error_text=None):
        self.answer = answer
        self.error_text = error_text
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"positional": list(args), "keywords": dict(kwargs)})
        sink = kwargs.get("metrics_sink")
        if sink is not None:
            sink(None)
        if self.error_text is not None:
            raise RuntimeError(self.error_text)
        return self.answer

    @property
    def sent_base64(self):
        """``(image_b64, reference_b64)`` of the single recorded
        invocation, from its positional arity."""
        assert len(self.calls) == 1, self.calls
        positional = self.calls[0]["positional"]
        image = positional[3] if len(positional) > 3 else None
        reference = positional[4] if len(positional) > 4 else None
        return image, reference


@contextmanager
def captured_logs(logger_name, level=logging.DEBUG):
    """Collect a module logger's records without a pytest fixture.

    ``caplog`` is function-scoped and Hypothesis rejects function-scoped
    fixtures inside ``@given``, so the property tests install their own
    handler.
    """
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = _Handler(level)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(level)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


EXPORT_LOGGER = export_module.__name__


# ---------------------------------------------------------------------------
# The invocation scenario table
# ---------------------------------------------------------------------------

#: Bedrock image-resolution situations and whether the invocation happens
#: at all (a crop failure and an unreadable frame never reach the model,
#: so they can never export).
BEDROCK_IMAGE_SITUATIONS = {
    "whole_frame": True,
    "crop_ok": True,
    "crop_error": False,        # recorded error (index out of range)
    "in_unreadable": False,     # raises BedrockInferenceError
}

#: Bedrock reference situations: whether the invocation happens, and
#: which bytes ride as the "Reference image".
BEDROCK_REFERENCE_SITUATIONS = {
    "unfed": True,
    "fed_readable": True,
    "fed_unreadable": True,     # warning + single-image inference
    "payload_base64": True,     # payload-resolved reference (not on disk)
    "payload_denied": False,    # recorded error, no fetch, no invocation
}

#: LLM image situations as ``(the invocation happens, an image is sent)``.
#: An unfed 'in' port does NOT fail an llm node — the executor issues the
#: pre-feature text-only 3-argument invocation — so it invokes without an
#: image; an unreadable frame is a recorded error and never invokes.
LLM_IMAGE_SITUATIONS = {
    "in_fed": (True, True),
    "in_unfed": (True, False),
    "in_unreadable": (False, False),
}
LLM_REFERENCE_SITUATIONS = ("unfed", "fed_readable")

MODES = ("anomaly", "freeform")
ANSWERS = ("verdict", "verdict_fenced", "unparseable", "raises")
SYSTEM_SITUATIONS = ("absent", "present")
MAX_DIMS = ("absent", "downscale")


BEDROCK_CASES = st.fixed_dictionaries({
    "node": st.just("bedrock"),
    "image": st.sampled_from(sorted(BEDROCK_IMAGE_SITUATIONS)),
    "reference": st.sampled_from(sorted(BEDROCK_REFERENCE_SITUATIONS)),
    "mode": st.sampled_from(MODES),
    "answer": st.sampled_from(ANSWERS),
    "system": st.sampled_from(SYSTEM_SITUATIONS),
})

LLM_CASES = st.fixed_dictionaries({
    "node": st.just("llm"),
    "image": st.sampled_from(sorted(LLM_IMAGE_SITUATIONS)),
    "reference": st.sampled_from(LLM_REFERENCE_SITUATIONS),
    "mode": st.sampled_from(MODES),
    "answer": st.sampled_from(ANSWERS),
    "system": st.sampled_from(SYSTEM_SITUATIONS),
    "max_dim": st.sampled_from(MAX_DIMS),
})

CASES = st.one_of(BEDROCK_CASES, LLM_CASES)


def invocation_happens(case):
    """Whether the drawn scenario reaches the model at all.

    An independent restatement of the executor's image-resolution rules
    (transcribed from ``output_bindings``, never imported): a crop
    failure, an unreadable input frame and a payload-reference failure
    all end the node before any request is built.
    """
    if case["node"] == "bedrock":
        return (BEDROCK_IMAGE_SITUATIONS[case["image"]]
                and BEDROCK_REFERENCE_SITUATIONS[case["reference"]])
    return LLM_IMAGE_SITUATIONS[case["image"]][0]


def exports_a_sample(case):
    """Whether the drawn scenario exports exactly one Tuning_Sample.

    Requirement 2.2 as implemented: an Anomaly_Mode invocation that
    returned an answer exports; a freeform invocation, an invocation
    that raised, and a node that never invoked export nothing. An
    ``llm_inference`` invocation that carried NO image (its 'in' port is
    unfed, so the executor issues the text-only invocation) also exports
    nothing — there would be no image pair to replay. That is task 3.1's
    recorded decision, asserted here rather than assumed.
    """
    if not (invocation_happens(case)
            and case["mode"] == "anomaly"
            and case["answer"] != "raises"):
        return False
    if case["node"] == "llm":
        return LLM_IMAGE_SITUATIONS[case["image"]][1]
    return True


def answer_text(case):
    """``(answer, invoker_error_text)`` for the drawn answer shape."""
    if case["answer"] == "verdict":
        return VERDICT_JSON, None
    if case["answer"] == "verdict_fenced":
        return FENCED_VERDICT, None
    if case["answer"] == "unparseable":
        return UNPARSEABLE, None
    return None, "the model endpoint refused the request"


def parses(case):
    """Whether the drawn answer shape parses into a verdict."""
    return case["answer"] in ("verdict", "verdict_fenced")


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------

class Scenario:
    """One run of one Inspection_Node in a temporary artifact tree."""

    def __init__(self, case, root):
        self.case = case
        self.root = root
        self.work_dir = os.path.join(root, "work")
        self.output_dir = os.path.join(root, "out")
        os.makedirs(self.work_dir)
        os.makedirs(self.output_dir)
        self.node_id = (
            "bedrock_1" if case["node"] == "bedrock" else "llm_1")
        self.input_frame = _write_jpeg(
            os.path.join(self.work_dir, INPUT_FRAME_NAME),
            INPUT_FRAME_SIZE, 40)
        self.reference_frame = _write_jpeg(
            os.path.join(self.work_dir, REFERENCE_FRAME_NAME),
            REFERENCE_FRAME_SIZE, 200)
        self.parameters = None
        self.invoker = None
        self.metadata = None
        self.raised = None
        self.node_status = None

    # -- the compiled binding -------------------------------------------
    def _bedrock_binding(self):
        case = self.case
        parameters = {
            "model": "us.amazon.nova-lite-v1:0",
            "region": "us-east-2",
            "max_tokens": 512,
            "prompt": "Compare the input to the reference.",
        }
        if case["mode"] == "freeform":
            parameters["anomaly_mode"] = False
        if case["system"] == "present":
            parameters["system_prompt"] = "You are a QA inspector."
        capture_paths = {"in": "{work_dir}/" + INPUT_FRAME_NAME,
                         "reference": None}
        if case["image"] == "in_unreadable":
            capture_paths["in"] = "{work_dir}/" + MISSING_NAME
        if case["image"].startswith("crop"):
            parameters["crop_detection_index"] = (
                9 if case["image"] == "crop_error" else DETECTION_SLOT)
        if case["reference"] == "fed_readable":
            capture_paths["reference"] = "{work_dir}/" + REFERENCE_FRAME_NAME
        elif case["reference"] == "fed_unreadable":
            capture_paths["reference"] = "{work_dir}/" + MISSING_NAME
        if case["reference"].startswith("payload"):
            parameters["reference_payload_path"] = PAYLOAD_REFERENCE_PATH
            if case["reference"] == "payload_denied":
                parameters["allowed_uri_prefixes"] = ALLOWED_URI_PREFIXES
        return {
            "nodeId": self.node_id,
            "binding": "bedrock_inference",
            "parameters": parameters,
            "upstreamNodeIds": ["cam"],
            "downstreamNodeIds": ["mqtt"],
            "capturePaths": capture_paths,
        }

    def _llm_binding(self):
        case = self.case
        parameters = {
            "modelName": "qwen2-vl-2b",
            "prompt_template": "Compare the input to {part_id}.",
            "temperature": 0.7,
            "top_p": 0.9,
            "max_tokens": 128,
        }
        if case["mode"] == "anomaly":
            parameters["anomaly_mode"] = True
        if case["system"] == "present":
            parameters["system_prompt"] = "You are a QA inspector."
        if case["max_dim"] == "downscale":
            parameters["max_image_dimension"] = 32
        capture_paths = {}
        if case["image"] == "in_fed":
            capture_paths["in"] = "{work_dir}/" + INPUT_FRAME_NAME
        elif case["image"] == "in_unreadable":
            capture_paths["in"] = "{work_dir}/" + MISSING_NAME
        else:
            capture_paths["in"] = None
        capture_paths["reference"] = (
            "{work_dir}/" + REFERENCE_FRAME_NAME
            if case["reference"] == "fed_readable" else None)
        return {
            "nodeId": self.node_id,
            "binding": "llm_inference",
            "parameters": parameters,
            "upstreamNodeIds": ["cam"],
            "downstreamNodeIds": ["mqtt"],
            "capturePaths": capture_paths,
        }

    def _tag_values(self):
        case = self.case
        values = {"part_id": "part-XYZ"}
        if case["node"] != "bedrock":
            return values
        if case["image"].startswith("crop"):
            entry = {"id": DETECTION_ID}
            entry.update(DETECTION_BOX)
            values["detections"] = [entry]
        if case["reference"] == "payload_base64":
            values["trigger"] = {
                "payload_json": {"ref": {"image": PAYLOAD_PNG_BASE64}}}
        elif case["reference"] == "payload_denied":
            values["trigger"] = {
                "payload_json": {"ref": {"image": DENIED_REFERENCE_URI}}}
        return values

    # -- the run --------------------------------------------------------
    def run(self, export_context=None):
        case = self.case
        answer, error_text = answer_text(case)
        binding = (self._bedrock_binding() if case["node"] == "bedrock"
                   else self._llm_binding())
        self.parameters = dict(binding["parameters"])
        document = {"schemaVersion": 1, "executorBindings": [binding]}
        tag_values = self._tag_values()
        collector = NodeStatusCollector(extra_node_ids=[self.node_id])
        self.node_status = collector
        if case["node"] == "bedrock":
            self.invoker = RecordingBedrockInvoker(answer, error_text)
            processor = BedrockInferenceProcessor(invoker=self.invoker)
            run_context = RunContext(
                tag_values=tag_values,
                output_dir=self.output_dir,
                capture_id=CAPTURE_ID,
                graph_document=None,
                node_status=collector,
            )
            try:
                self.metadata = processor.process(
                    document, tag_values, self.work_dir,
                    run_context=run_context,
                    export_context=export_context)
            except BedrockInferenceError as error:
                self.raised = {"node_id": error.node_id,
                               "message": str(error)}
        else:
            self.invoker = RecordingLlmInvoker(answer, error_text)
            processor = LlmInferenceProcessor(invoker=self.invoker)
            self.metadata = processor.process(
                document, tag_values, self.work_dir,
                export_context=export_context)
        return self

    # -- observation ----------------------------------------------------
    def observation(self):
        """The run's outcome, normalized so two runs in different
        temporary directories compare equal."""
        def normalize(value):
            if isinstance(value, str):
                return value.replace(self.work_dir, "{work_dir}").replace(
                    self.output_dir, "{output_dir}")
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            if isinstance(value, (bytes, bytearray)):
                return _sha256(bytes(value))
            return value

        artifacts = []
        for name in sorted(os.listdir(self.output_dir)):
            path = os.path.join(self.output_dir, name)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as handle:
                artifacts.append((name, _sha256(handle.read())))
        return {
            "metadata": normalize(self.metadata),
            "raised": normalize(self.raised),
            "node_status": normalize({
                node_id: entry.get("status")
                for node_id, entry in self.node_status.to_map().items()
            }),
            "artifacts": artifacts,
            "invocations": normalize([
                {"positional": call["positional"],
                 "keywords": sorted(call["keywords"])}
                for call in self.invoker.calls
            ]),
        }

    # -- the bytes the invocation really sent ---------------------------
    def sent_images(self):
        """``(input_bytes, reference_bytes_or_None)`` as handed to the
        invoker — the bytes an exported sample must equal."""
        if self.case["node"] == "bedrock":
            images = self.invoker.sent_images
            assert images[0][0] == "Input image", images
            reference = None
            if len(images) > 1:
                assert images[1][0] == "Reference image", images
                reference = images[1][1]
            return images[0][1], reference
        image_b64, reference_b64 = self.invoker.sent_base64
        return (
            base64.b64decode(image_b64) if image_b64 is not None else None,
            (base64.b64decode(reference_b64)
             if reference_b64 is not None else None),
        )


@contextmanager
def scenario(case):
    root = tempfile.mkdtemp(prefix="dda-tuning-export-")
    try:
        yield Scenario(case, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@contextmanager
def installed(exporter):
    """Install a process-wide exporter for the duration of one run."""
    set_sample_exporter(exporter)
    try:
        yield exporter
    finally:
        set_sample_exporter(None)
        stop = getattr(exporter, "stop", None)
        if callable(stop):
            stop(2.0)


def run_context_export():
    return ExportContext(
        workflow_id=WORKFLOW_ID, version=VERSION, execution_id=EXECUTION_ID)


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------

@given(case=CASES)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_property_export_is_exactly_what_was_sent(case):
    """**Feature: quality-prompt-tuning, Property 2: Every completed
    anomaly-mode invocation exports exactly what was sent.**

    For any Anomaly_Mode invocation that returns an answer on a device
    with export configured, exactly one ExportedSample is enqueued whose
    input bytes and reference bytes are byte-identical to the image bytes
    passed to the invoker, whose answer/verdict equal the recorded
    Run_Metadata values, whose fingerprint equals ``prompt_fingerprint``
    of the node's Prompt_Set, and whose ``detectionId``/``detectionSlot``
    equal the crop path's; freeform-mode invocations and invocations that
    raised enqueue nothing. The three uploaded objects carry the images
    byte-identically and the sidecar carries no image bytes.

    **Validates: Requirements 2.2, 2.3**"""
    s3 = FakeS3()
    exporter = RecordingExporter(
        ExportConfig(bucket=BUCKET, prefix=PREFIX),
        s3_factory=lambda: s3, thing_name=THING_NAME)
    with scenario(case) as run, installed(exporter):
        run.run(export_context=run_context_export())
        assert exporter.wait_idle(10.0), "the upload worker did not drain"

    expected_export = exports_a_sample(case)
    assert (len(run.invoker.calls) == 1) is invocation_happens(case), (
        "the scenario's invocation expectation is wrong: "
        "{0!r} produced {1} call(s)".format(case, len(run.invoker.calls)))

    if not expected_export:
        assert exporter.offered == [], (
            "scenario {0!r} must export nothing".format(case))
        assert s3.objects == {}
        assert s3.puts == []
        return

    assert len(exporter.offered) == 1, exporter.offered
    sample = exporter.offered[0]
    sent_input, sent_reference = run.sent_images()

    # -- exactly what was sent ------------------------------------------
    assert sample.input_bytes == sent_input
    assert sample.reference_bytes == sent_reference
    if case["node"] == "llm" and case["max_dim"] == "downscale":
        # The DOWNSCALED bytes, not the frame on disk: what the model saw.
        assert sample.input_bytes == downscale_image_bytes(
            run.input_frame, 32)
        assert sample.input_bytes != run.input_frame
    if case["node"] == "bedrock" and case["reference"] == "payload_base64":
        # A payload-resolved reference exists nowhere on disk.
        assert sample.reference_bytes == PAYLOAD_PNG_BYTES

    # -- identity ------------------------------------------------------
    assert sample.workflow_id == WORKFLOW_ID
    assert sample.version == VERSION
    assert sample.execution_id == EXECUTION_ID
    assert sample.node_id == run.node_id
    assert sample.node_type == (
        "bedrock_inference" if case["node"] == "bedrock" else "llm_inference")
    assert sample.source == SOURCE_LIVE

    # -- the recorded answer and verdict -------------------------------
    answer, _ = answer_text(case)
    assert sample.answer == answer
    if parses(case):
        assert sample.parse_error is None
        assert sample.verdict is not None
        assert sample.verdict["is_anomalous"] is True
        assert sample.verdict["confidence"] == 0.87
        # ... and they are the values the run recorded.
        if case["node"] == "bedrock":
            nested = run.metadata["bedrock"][run.node_id]
            assert nested["text"] == answer
            assert sample.verdict["is_anomalous"] == nested["is_anomalous"]
            assert sample.verdict["confidence"] == nested["confidence"]
        else:
            nested = run.metadata["llm"][run.node_id]
            assert nested["generated_text"] == answer
            assert sample.verdict["is_anomalous"] == nested["is_anomalous"]
            assert sample.verdict["confidence"] == nested["confidence"]
    else:
        # An unparseable answer is exported WITH its parse failure, and
        # the run's own error surfacing is unchanged.
        assert sample.verdict is None
        assert sample.parse_error
        if case["node"] == "bedrock":
            assert run.raised is not None
            assert sample.parse_error in run.raised["message"]
        else:
            assert sample.parse_error == (
                run.metadata["llm"][run.node_id]["error"])

    # -- the Prompt_Set fingerprint ------------------------------------
    prompt_set = {
        "prompt": run.parameters.get("prompt"),
        "prompt_template": run.parameters.get("prompt_template"),
        "system_prompt": run.parameters.get("system_prompt"),
        "max_tokens": run.parameters.get("max_tokens"),
    }
    assert sample.prompt_fingerprint == prompt_fingerprint(prompt_set)
    # The fingerprint is a function of the Prompt_Set ALONE: changing a
    # Node_Parameter must not change it.
    other = dict(prompt_set)
    other["model"] = "us.amazon.nova-pro-v1:0"
    other["region"] = "eu-west-1"
    assert prompt_fingerprint(other) == sample.prompt_fingerprint

    # -- the crop path's detection --------------------------------------
    if case["node"] == "bedrock" and case["image"] == "crop_ok":
        assert sample.detection_id == DETECTION_ID
        assert sample.detection_slot == DETECTION_SLOT
        if parses(case):
            # The run records the same detection the exported sample
            # attributes its verdict to.
            assert (run.metadata["bedrock"][run.node_id]["detection_id"]
                    == sample.detection_id)
    else:
        assert sample.detection_id is None
        assert sample.detection_slot is None

    # -- the uploaded objects (Requirement 2.3) -------------------------
    sidecar_key, input_key, reference_key = object_keys(
        PREFIX, sample, THING_NAME)
    base = "{0}{1}/{2}/{3}/{4}".format(
        PREFIX, WORKFLOW_ID, run.node_id, THING_NAME, EXECUTION_ID)
    assert sidecar_key == base + ".json"
    assert input_key == base + ".input.jpg"
    expected_keys = {sidecar_key, input_key}
    if sent_reference is not None:
        assert reference_key == base + ".reference.jpg"
        expected_keys.add(reference_key)
    else:
        assert reference_key is None
    assert set(s3.objects) == expected_keys
    assert all(entry["bucket"] == BUCKET for entry in s3.objects.values())
    assert s3.objects[input_key]["body"] == sent_input
    assert s3.objects[input_key]["contentType"] == "image/jpeg"
    if sent_reference is not None:
        assert s3.objects[reference_key]["body"] == sent_reference
    # The images land BEFORE the sidecar that references them.
    assert s3.puts.index(sidecar_key) == len(s3.puts) - 1

    body = s3.objects[sidecar_key]["body"]
    assert s3.objects[sidecar_key]["contentType"] == "application/json"
    document = json.loads(body.decode("utf-8"))
    assert document["workflowId"] == WORKFLOW_ID
    assert document["executionId"] == EXECUTION_ID
    assert document["nodeId"] == run.node_id
    assert document["thingName"] == THING_NAME
    assert document["source"] == SOURCE_LIVE
    assert document["input"] == {
        "key": input_key, "sha256": _sha256(sent_input),
        "bytes": len(sent_input)}
    if sent_reference is not None:
        assert document["reference"] == {
            "key": reference_key, "sha256": _sha256(sent_reference),
            "bytes": len(sent_reference)}
    else:
        assert "reference" not in document
    assert document["recorded"]["answer"] == answer
    assert document["promptFingerprint"] == sample.prompt_fingerprint
    assert document["detectionId"] == sample.detection_id
    assert document["detectionSlot"] == sample.detection_slot
    if parses(case):
        assert document["recorded"]["isAnomalous"] is True
        assert document["recorded"]["confidence"] == 0.87
        assert document["recorded"]["parseError"] is None
    else:
        assert document["recorded"]["isAnomalous"] is None
        assert document["recorded"]["parseError"] == sample.parse_error
    # NO image bytes in the sidecar, in any encoding.
    assert sent_input not in body
    assert base64.b64encode(sent_input) not in body
    if sent_reference is not None:
        assert sent_reference not in body
        assert base64.b64encode(sent_reference) not in body


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------

#: Exporter behaviours a run must be indifferent to.
BEHAVIOURS = (
    "disabled",          # no exporter installed at all
    "inert",             # an exporter built from no configuration
    "uploads_succeed",
    "uploads_fail",      # every put_object raises
    "uploads_retry",     # the first two puts raise, the third succeeds
    "stalled",           # the upload worker never drains the queue
    "defective",         # every exporter interaction raises
)


def _exporter_for(behaviour, s3):
    """The exporter under test for a drawn behaviour, or ``None``."""
    config = ExportConfig(bucket=BUCKET, prefix=PREFIX)
    if behaviour == "disabled":
        return None
    if behaviour == "inert":
        return RecordingExporter(
            None, s3_factory=lambda: s3, thing_name=THING_NAME)
    if behaviour == "defective":
        return DefectiveExporter()
    if behaviour == "stalled":
        return NeverDrainingExporter(
            config, s3_factory=lambda: s3, thing_name=THING_NAME)
    return RecordingExporter(
        config, s3_factory=lambda: s3, thing_name=THING_NAME,
        backoff_base_seconds=0.0)


@given(case=CASES, behaviour=st.sampled_from(BEHAVIOURS),
       queued=st.integers(min_value=0, max_value=260))
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_property_export_is_contained_bounded_and_inert(
        case, behaviour, queued):
    """**Feature: quality-prompt-tuning, Property 3: Export is contained,
    bounded and inert when unconfigured.**

    For any execution and any exporter behaviour (uploads succeed, fail
    permanently, or stall) and any queue state, the execution's outcome,
    artifacts and Run_Metadata are identical to those with export
    disabled; the queue never exceeds 200 entries (oldest dropped with a
    warning); each sample is attempted at most 3 times; and with no or
    malformed configuration no S3 client is created and no queue
    allocated.

    **Validates: Requirements 2.4, 2.5, 2.6, 11.2, 11.3**"""
    # -- 1. the run is indifferent to the exporter ----------------------
    with scenario(case) as baseline:
        baseline.run(export_context=None)
        expected = baseline.observation()

    # A defective exporter is checked on EVERY example (not only when the
    # behaviour is drawn), because containment must hold for the drawn
    # node type's own export call site (Requirements 11.1, 11.2).
    with scenario(case) as contained:
        with installed(DefectiveExporter()):
            contained.run(export_context=run_context_export())
        assert contained.observation() == expected, (
            "a defective exporter changed the run")

    s3 = FakeS3(
        always_fail=(behaviour == "uploads_fail"),
        fail_first=(2 if behaviour == "uploads_retry" else 0))
    exporter = _exporter_for(behaviour, s3)
    with scenario(case) as run:
        if exporter is None:
            run.run(export_context=run_context_export())
        else:
            with installed(exporter):
                run.run(export_context=run_context_export())
                if isinstance(exporter, SampleExporter) and exporter.enabled:
                    exporter.wait_idle(10.0)
        assert run.observation() == expected, (
            "exporter behaviour {0!r} changed the run".format(behaviour))

    # -- 2. an unconfigured exporter is completely inert ----------------
    if behaviour in ("disabled", "inert"):
        assert s3.puts == []
        assert s3.objects == {}
    if behaviour == "inert":
        assert exporter.enabled is False
        assert exporter.queue_depth == 0
        # Offered samples are dropped on the floor; no client is built.
        exporter.enqueue(_synthetic_sample("inert"))
        assert exporter.queue_depth == 0
        assert s3.puts == []

    # -- 3. at most 3 attempts per sample (Requirement 2.5) -------------
    if behaviour == "uploads_fail" and exports_a_sample(case):
        with captured_logs(EXPORT_LOGGER, logging.ERROR) as records:
            failing = RecordingExporter(
                ExportConfig(bucket=BUCKET, prefix=PREFIX),
                s3_factory=lambda: FakeS3(always_fail=True),
                thing_name=THING_NAME, backoff_base_seconds=0.0)
            with installed(failing):
                failing.enqueue(_synthetic_sample("attempts"))
                assert failing.wait_idle(10.0)
        assert len(records) == 1, [r.getMessage() for r in records]
        message = records[0].getMessage()
        assert "3 attempt(s)" in message
        assert ".json" in message
    if behaviour == "uploads_retry" and exports_a_sample(case):
        # A retried upload still lands: 2 failures then the real objects.
        assert set(s3.objects), s3.puts

    # -- 4. the queue bound and the drop-oldest rule --------------------
    with captured_logs(EXPORT_LOGGER, logging.WARNING) as records:
        bounded = NeverDrainingExporter(
            ExportConfig(bucket=BUCKET, prefix=PREFIX),
            s3_factory=lambda: FakeS3(), thing_name=THING_NAME)
        for index in range(queued):
            bounded.enqueue(_synthetic_sample("queued-{0}".format(index)))
    assert bounded.queue_size == DEFAULT_QUEUE_SIZE == 200
    assert bounded.queue_depth == min(queued, DEFAULT_QUEUE_SIZE)
    dropped = max(0, queued - DEFAULT_QUEUE_SIZE)
    warnings = [
        record.getMessage() for record in records
        if "queue is full" in record.getMessage()
    ]
    assert len(warnings) == dropped
    if dropped:
        # The OLDEST entries are the ones dropped, and the warning names
        # the dropped execution.
        assert "queued-0" in warnings[0]
        assert all("queued-{0}".format(index) in warnings[index]
                   for index in range(dropped))

    # -- 5. an oversized sample is skipped (Requirement 2.10) -----------
    with captured_logs(EXPORT_LOGGER, logging.INFO) as records:
        skipping = NeverDrainingExporter(
            ExportConfig(bucket=BUCKET, prefix=PREFIX),
            s3_factory=lambda: FakeS3(), thing_name=THING_NAME)
        skipping.enqueue(_synthetic_sample(
            "oversize", input_bytes=b"\0" * (MAX_IMAGE_BYTES + 1)))
    assert skipping.queue_depth == 0
    assert any("exceeds" in record.getMessage() for record in records)


def _synthetic_sample(execution_id, input_bytes=b"\xff\xd8input\xff\xd9",
                      reference_bytes=None):
    return ExportedSample(
        workflow_id=WORKFLOW_ID,
        node_id="bedrock_1",
        node_type="bedrock_inference",
        execution_id=execution_id,
        input_bytes=input_bytes,
        version=VERSION,
        reference_bytes=reference_bytes,
        answer=VERDICT_JSON,
        verdict={"is_anomalous": True, "confidence": 0.5},
    )


# ---------------------------------------------------------------------------
# Property 4: the one-shot backfill
# ---------------------------------------------------------------------------

NODE_FRAME_TEMPLATE = "{capture_id}.node.{node_id}.{port}.jpg"

#: Per-(node, execution) artifact situations the backfill must classify.
OUTCOME_SITUATIONS = (
    "crop",            # outcome carries detection_id -> the 'original' frame
    "whole_frame",     # no detection_id -> the 'in' frame
    "error",           # error outcome -> skipped
    "missing_input",   # no input frame on disk -> skipped
    "no_outcome",      # the node did not run -> skipped
)

NODE_SPECS = (
    ("bedrock_1", "bedrock_inference", None),      # anomaly by default
    ("bedrock_2", "bedrock_inference", False),     # freeform: not tunable
    ("llm_1", "llm_inference", True),              # anomaly
    ("llm_2", "llm_inference", None),              # not tunable
    ("model_1", "model_inference", True),          # not an Inspection_Node
)

TUNABLE_NODE_IDS = ("bedrock_1", "llm_1")


class CollectingExporter:
    """An enabled exporter that only collects (the backfill's pacing
    reads ``queue_depth``/``queue_size``, so both are reported)."""

    enabled = True
    queue_size = DEFAULT_QUEUE_SIZE
    queue_depth = 0

    def __init__(self):
        self.config = ExportConfig(bucket=BUCKET, prefix=PREFIX)
        self.samples = []

    def enqueue(self, sample):
        self.samples.append(sample)


def _memory_session_factory():
    """A sessionmaker over a private in-memory sqlite database.

    The backfill walk is driven directly (not on its thread), so one
    shared connection is enough and no temp file is left behind.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _write_run(root, executions, node_situations, references):
    """Write one registration's artifact tree and return the rows.

    ``executions`` is the list of execution ids newest-first;
    ``node_situations[(execution_id, node_id)]`` gives the situation and
    ``references[(execution_id, node_id)]`` whether a reference frame
    was persisted. Returns ``(artifact_path, rows, frames)`` where
    ``frames`` maps ``(execution_id, node_id, port)`` to its bytes.
    """
    artifact_path = os.path.join(root, "registration")
    os.makedirs(artifact_path, exist_ok=True)
    graph = {"nodes": [
        {"id": node_id, "type": node_type,
         "parameters": _node_parameters(node_id, node_type, anomaly)}
        for node_id, node_type, anomaly in NODE_SPECS
    ]}
    with open(os.path.join(artifact_path, "workflow.json"), "w") as handle:
        json.dump(graph, handle)

    rows = []
    frames = {}
    for index, execution_id in enumerate(executions):
        output_dir = os.path.join(root, "runs", execution_id)
        os.makedirs(output_dir, exist_ok=True)
        metadata = {"bedrock": {}, "llm": {}, "part_id": "part-XYZ"}
        for node_id, node_type, _anomaly in NODE_SPECS:
            situation = node_situations[(execution_id, node_id)]
            section = "bedrock" if node_type == "bedrock_inference" else "llm"
            if node_type not in ("bedrock_inference", "llm_inference"):
                continue
            if situation != "no_outcome":
                answer_key = (
                    "text" if section == "bedrock" else "generated_text")
                outcome = {answer_key: VERDICT_JSON,
                           "is_anomalous": True, "confidence": 0.87}
                if situation == "error":
                    outcome = {
                        "error": "the model endpoint refused the request"}
                if situation == "crop":
                    outcome["detection_id"] = DETECTION_ID
                metadata[section][node_id] = outcome
            if situation == "missing_input":
                continue
            # Frames are persisted for an errored node and for a node
            # with no recorded outcome too, so that a skip can only be
            # attributed to the rule under test (the error outcome, the
            # missing outcome) and never to an absent frame.
            port = "original" if situation == "crop" else "in"
            frames[(execution_id, node_id, port)] = _write_jpeg(
                os.path.join(output_dir, NODE_FRAME_TEMPLATE.format(
                    capture_id=execution_id, node_id=node_id, port=port)),
                INPUT_FRAME_SIZE, 40 + index)
            if references[(execution_id, node_id)]:
                frames[(execution_id, node_id, "reference")] = _write_jpeg(
                    os.path.join(output_dir, NODE_FRAME_TEMPLATE.format(
                        capture_id=execution_id, node_id=node_id,
                        port="reference")),
                    REFERENCE_FRAME_SIZE, 200)
        with open(os.path.join(output_dir, execution_id + ".json"),
                  "w") as handle:
            json.dump(metadata, handle)
        rows.append({
            "id": execution_id,
            "output_dir": output_dir,
            "capture_id": execution_id,
            # Newest first in ``executions``, so started_at descends.
            "started_at": 2_000_000 - index,
        })
    return artifact_path, rows, frames


def _node_parameters(node_id, node_type, anomaly):
    parameters = {"prompt": "Compare the input to the reference.",
                  "system_prompt": "You are a QA inspector.",
                  "max_tokens": 512}
    if node_type == "llm_inference":
        parameters = {"modelName": "qwen2-vl-2b",
                      "prompt_template": "Compare {part_id}.",
                      "system_prompt": None, "max_tokens": 128}
    if anomaly is not None:
        parameters["anomaly_mode"] = anomaly
    parameters["crop_detection_index"] = DETECTION_SLOT
    return parameters


@given(
    executions=st.lists(
        st.integers(min_value=0, max_value=999), min_size=1, max_size=6,
        unique=True).map(
            lambda seeds: ["exec-{0:04d}".format(seed) for seed in seeds]),
    situations=st.lists(st.sampled_from(OUTCOME_SITUATIONS),
                        min_size=12, max_size=12),
    reference_flags=st.lists(st.booleans(), min_size=12, max_size=12),
    max_executions=st.integers(min_value=1, max_value=6),
)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_property_backfill_pairs_by_the_executors_rules(
        executions, situations, reference_flags, max_executions):
    """**Feature: quality-prompt-tuning, Property 4: Backfill pairs by
    the executor's artifact rules, once, within bounds.**

    For any artifact tree with generated Run_Metadata and node frames,
    backfill exports exactly one sample per (tunable node, execution)
    whose outcome is not an error and whose input frame exists —
    ``original`` when the outcome carries a ``detection_id``, else
    ``in``; ``reference`` iff present — limited to the newest
    ``max_executions`` executions per node, marked ``source: backfill``,
    and a second start exports nothing.

    **Validates: Requirement 2.7**"""
    root = tempfile.mkdtemp(prefix="dda-tuning-backfill-")
    try:
        # Situations/references are drawn per (execution, node) from the
        # two fixed-length lists, so the draw is independent of the
        # number of executions.
        node_situations = {}
        reference_map = {}
        for e_index, execution_id in enumerate(executions):
            for n_index, (node_id, _t, _a) in enumerate(NODE_SPECS):
                slot = (e_index * len(NODE_SPECS) + n_index) % 12
                node_situations[(execution_id, node_id)] = situations[slot]
                reference_map[(execution_id, node_id)] = reference_flags[slot]

        artifact_path, rows, frames = _write_run(
            root, executions, node_situations, reference_map)
        factory = _memory_session_factory()
        with factory() as session:
            session.add(WorkflowRegistration(
                id="reg-1", workflow_id=WORKFLOW_ID, version=VERSION,
                arch="aarch64", artifact_path=artifact_path,
                status="registered", registered_at=1_000_000))
            for row in rows:
                session.add(WorkflowExecution(
                    id=row["id"], registration_id="reg-1",
                    started_at=row["started_at"], finished_at=None,
                    status="success", capture_id=row["capture_id"],
                    output_dir=row["output_dir"]))
            session.commit()

        marker_path = os.path.join(root, "marker", "backfilled.json")
        exporter = CollectingExporter()
        summary = backfill_module.run_backfill(
            exporter, session_factory=factory, marker_path=marker_path,
            max_executions=max_executions, sleep=lambda _seconds: None)

        # -- the expected set, restated from Requirement 2.7 ------------
        considered = executions[:max_executions]
        expected = {}
        for execution_id in considered:
            for node_id in TUNABLE_NODE_IDS:
                situation = node_situations[(execution_id, node_id)]
                if situation in ("error", "missing_input", "no_outcome"):
                    continue
                port = "original" if situation == "crop" else "in"
                expected[(execution_id, node_id)] = {
                    "input": frames[(execution_id, node_id, port)],
                    "reference": frames.get(
                        (execution_id, node_id, "reference")),
                    "detection_id": (
                        DETECTION_ID if situation == "crop" else None),
                }

        exported = {
            (sample.execution_id, sample.node_id): sample
            for sample in exporter.samples
        }
        assert len(exporter.samples) == len(exported), (
            "a (node, execution) pair was exported twice")
        assert set(exported) == set(expected)
        assert summary.exported == len(expected)
        assert summary.ran is True
        for key, expectation in expected.items():
            sample = exported[key]
            # The pairing rule: the exact persisted bytes of the frame
            # the executor would have sent.
            assert sample.input_bytes == expectation["input"]
            assert sample.reference_bytes == expectation["reference"]
            assert sample.detection_id == expectation["detection_id"]
            assert sample.detection_slot == (
                DETECTION_SLOT if expectation["detection_id"] else None)
            assert sample.source == SOURCE_BACKFILL
            assert sample.workflow_id == WORKFLOW_ID
            assert sample.version == VERSION
            assert sample.answer == VERDICT_JSON
            assert sample.verdict == {"is_anomalous": True,
                                      "confidence": 0.87}
            assert sample.prompt_fingerprint

        # -- the bound and the ordering ---------------------------------
        assert len(considered) == min(len(executions), max_executions)
        assert all(sample.execution_id in considered
                   for sample in exporter.samples)

        # -- a second start exports nothing -----------------------------
        assert os.path.exists(marker_path)
        with open(marker_path) as handle:
            marker = json.load(handle)
        assert marker["exported"] == len(expected)
        second = CollectingExporter()
        again = backfill_module.run_backfill(
            second, session_factory=factory, marker_path=marker_path,
            max_executions=max_executions, sleep=lambda _seconds: None)
        assert second.samples == []
        assert again.already_done is True
        assert again.ran is False

        # -- and without a configured exporter nothing is read at all --
        unconfigured_marker = os.path.join(root, "marker", "never.json")
        unconfigured = backfill_module.run_backfill(
            SampleExporter(None), session_factory=factory,
            marker_path=unconfigured_marker,
            max_executions=max_executions, sleep=lambda _seconds: None)
        assert unconfigured.not_configured is True
        assert not os.path.exists(unconfigured_marker)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 17 (device configuration half)
# ---------------------------------------------------------------------------

BUCKET_VALUES = (None, "", "   ", BUCKET, "  " + BUCKET + "  ", 42, ["b"])
PREFIX_VALUES = (None, "", "workflow-tuning/samples", PREFIX,
                 "custom/tuning/", 7, {"p": 1})
ENABLED_VALUES = (True, False, None, "true", "TRUE", " true ", "false",
                  "yes", 1, 0, "1", [], {})


def _expected_config(section):
    """An independent restatement of Requirement 2.6 / Property 17.

    Enabled only for a mapping whose ``enabled`` is the boolean ``True``
    or the string ``"true"`` (case-insensitive, trimmed), whose
    ``bucket`` is a non-blank string and whose ``prefix`` is a non-empty
    string ending in ``/``. The recorded bucket is trimmed; the prefix is
    verbatim.
    """
    if not isinstance(section, dict):
        return None
    enabled = section.get("enabled")
    if enabled is not True and not (
        isinstance(enabled, str) and enabled.strip().lower() == "true"
    ):
        return None
    bucket = section.get("bucket")
    if not isinstance(bucket, str) or not bucket.strip():
        return None
    prefix = section.get("prefix")
    if not isinstance(prefix, str) or not prefix or not prefix.endswith("/"):
        return None
    return (bucket.strip(), prefix)


CONFIG_SHAPES = st.one_of(
    st.just("absent"),
    st.sampled_from(["non_object_config", "non_object_section",
                     "section_none", "section_list"]),
    st.just("fields"),
)


@given(
    shape=CONFIG_SHAPES,
    enabled=st.sampled_from(ENABLED_VALUES),
    bucket=st.sampled_from(BUCKET_VALUES),
    prefix=st.sampled_from(PREFIX_VALUES),
    extra=st.booleans(),
)
@settings(max_examples=100, deadline=None)
def test_property_configuration_is_parsed_safely(
        shape, enabled, bucket, prefix, extra):
    """**Feature: quality-prompt-tuning, Property 17: Configuration and
    grants are delivered iff export is enabled, and parsed safely**
    (device configuration half).

    For any LocalServer configuration shape (absent, non-object,
    disabled, empty bucket, prefix without trailing slash, valid),
    export is disabled for every malformed shape — no exporter, no
    queue, no S3 client, no request — and enabled with the exact
    location otherwise.

    **Validates: Requirements 2.6, 11.3**"""
    if shape == "absent":
        configuration = {"otherComponent": {"enabled": True}}
        section = None
    elif shape == "non_object_config":
        configuration = ["workflowTuning"]
        section = None
    elif shape == "non_object_section":
        configuration = {"workflowTuning": "enabled"}
        section = "enabled"
    elif shape == "section_none":
        configuration = {"workflowTuning": None}
        section = None
    elif shape == "section_list":
        configuration = {"workflowTuning": [{"enabled": True}]}
        section = [{"enabled": True}]
    else:
        section = {}
        if enabled is not None or extra:
            section["enabled"] = enabled
        if bucket is not None or extra:
            section["bucket"] = bucket
        if prefix is not None or extra:
            section["prefix"] = prefix
        if extra:
            section["unknownKey"] = "ignored"
        configuration = {"workflowTuning": section}

    expected = _expected_config(section)

    # -- the parser -----------------------------------------------------
    parsed = ExportConfig.from_component_configuration(configuration)
    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert (parsed.bucket, parsed.prefix) == expected
    # -- the section parser agrees with the whole-configuration parser --
    assert ExportConfig.from_section(section) == parsed

    # -- decision-sensitive cross-checks -------------------------------
    # Each drawn field is ALSO checked against an otherwise-valid
    # configuration, so a mis-parsed value cannot escape just because
    # the draw paired it with another malformed field.
    for probe in ({"enabled": enabled, "bucket": BUCKET, "prefix": PREFIX},
                  {"enabled": True, "bucket": bucket, "prefix": PREFIX},
                  {"enabled": True, "bucket": BUCKET, "prefix": prefix},
                  {"enabled": enabled, "bucket": bucket, "prefix": prefix}):
        probed = ExportConfig.from_section(probe)
        expectation = _expected_config(probe)
        assert (probed is None) == (expectation is None), probe
        if probed is not None:
            assert (probed.bucket, probed.prefix) == expectation, probe

    # -- what the parse installs ---------------------------------------
    created = []

    def factory():
        created.append(True)
        return FakeS3()

    try:
        exporter = configure_sample_exporter(
            configuration, s3_factory=factory, thing_name=THING_NAME)
        if expected is None:
            assert exporter is None
            assert export_module.sample_exporter() is None
        else:
            assert exporter is not None
            assert export_module.sample_exporter() is exporter
            assert exporter.enabled is True
            assert exporter.config.bucket == expected[0]
            assert exporter.config.prefix == expected[1]
            assert exporter.thing_name == THING_NAME
        # No configuration shape builds an S3 client at parse time.
        assert created == []
    finally:
        export_module.shutdown_sample_exporter(2.0)

    # -- a disabled device is completely inert -------------------------
    if expected is None:
        inert = SampleExporter(None, s3_factory=factory)
        assert inert.enabled is False
        inert.enqueue(_synthetic_sample("inert"))
        assert inert.queue_depth == 0
        assert created == []


# ---------------------------------------------------------------------------
# Supporting checks: the bounds the properties assert are the design's
# ---------------------------------------------------------------------------

def test_documented_bounds():
    """The numbers Properties 3 and 4 bound against are the design's."""
    assert DEFAULT_QUEUE_SIZE == 200
    assert MAX_UPLOAD_ATTEMPTS == 3
    assert MAX_IMAGE_BYTES == 8 * 1024 * 1024
    assert backfill_module.MAX_EXECUTIONS_PER_NODE == 500
    assert backfill_module.BACKFILL_MARKER_PATH == (
        "/aws_dda/workflow-tuning/backfilled.json")


def test_object_layout_is_the_documented_one():
    """The design's Sample_Store layout, literally."""
    sample = _synthetic_sample("c76c5060", reference_bytes=b"reference")
    sidecar, image, reference = object_keys(PREFIX, sample, "adlink-dlap-701")
    assert sidecar == (
        "workflow-tuning/samples/wf-24680/bedrock_1/adlink-dlap-701/"
        "c76c5060.json")
    assert image == sidecar[:-len(".json")] + ".input.jpg"
    assert reference == sidecar[:-len(".json")] + ".reference.jpg"
    single = _synthetic_sample("c76c5060")
    assert object_keys(PREFIX, single, "adlink-dlap-701")[2] is None


def test_backfill_selects_exactly_the_tunable_nodes():
    """The backfill's node selection is the SHARED rule, so it covers
    exactly the nodes live export covers (Property 1)."""
    graph = {"nodes": [
        {"id": node_id, "type": node_type,
         "parameters": _node_parameters(node_id, node_type, anomaly)}
        for node_id, node_type, anomaly in NODE_SPECS
    ]}
    selected = backfill_module.tunable_nodes(graph)
    assert [node.node_id for node in selected] == list(TUNABLE_NODE_IDS)
    assert backfill_module.tunable_nodes(None) == []
    assert backfill_module.tunable_nodes({"nodes": "x"}) == []


def test_export_worker_runs_off_the_calling_thread():
    """Uploads never happen on the thread that enqueued (Requirement
    2.4: an execution is never delayed by an upload)."""
    threads = []
    barrier = threading.Event()

    class _ThreadRecordingS3(FakeS3):
        def put_object(self, **kwargs):
            threads.append(threading.current_thread().name)
            barrier.set()
            return super().put_object(**kwargs)

    exporter = SampleExporter(
        ExportConfig(bucket=BUCKET, prefix=PREFIX),
        s3_factory=_ThreadRecordingS3, thing_name=THING_NAME)
    try:
        exporter.enqueue(_synthetic_sample("threaded"))
        assert barrier.wait(10.0)
        assert exporter.wait_idle(10.0)
    finally:
        exporter.stop(2.0)
    assert threads
    assert all(name != threading.current_thread().name for name in threads)
    assert all(name == "tuning-sample-export" for name in threads)


@pytest.mark.parametrize("case", [
    {"node": "bedrock", "image": "whole_frame", "reference": "fed_readable",
     "mode": "anomaly", "answer": "verdict", "system": "present"},
    {"node": "llm", "image": "in_fed", "reference": "fed_readable",
     "mode": "anomaly", "answer": "verdict", "system": "absent",
     "max_dim": "absent"},
])
def test_no_export_context_means_no_export(case):
    """The pre-feature call shape (no ``export_context``) exports
    nothing even with an exporter installed (Requirement 11.3)."""
    s3 = FakeS3()
    exporter = RecordingExporter(
        ExportConfig(bucket=BUCKET, prefix=PREFIX),
        s3_factory=lambda: s3, thing_name=THING_NAME)
    with scenario(case) as run, installed(exporter):
        run.run(export_context=None)
        assert exporter.wait_idle(5.0)
    assert exporter.offered == []
    assert s3.puts == []
