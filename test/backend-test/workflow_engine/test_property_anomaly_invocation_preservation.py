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
"""**Feature: quality-prompt-tuning, Property 7: The extraction into
``workflow_core`` is behaviour-neutral** (spec task 1.1).

*For any* ``bedrock_inference`` or ``llm_inference`` binding
configuration and captured-frame situation the executor supports today
(whole-frame, crop path, payload reference, missing/unfed/unreadable
reference, anomaly and freeform modes, with and without system prompt),
the invoker arguments, raised or recorded errors, persisted artifacts
and merged Run_Metadata after this feature SHALL equal a baseline
captured from the pre-feature executor.

**Validates: Requirements 11.1, 11.2**

How the baseline works
----------------------

The baseline fixture
``fixtures/anomaly_invocation_preservation_baseline.json`` was captured
from the PRE-REFACTOR processors (before any of
``workflow_core.anomaly_invocation`` existed), which is why this test
lands before the refactor (spec tasks 1.1 -> 1.5). It maps one
**scenario key** (the enumerated binding/frame situation — see
``CASES``) to the canonicalized observation of a run:

- every invoker call's positional arguments and keyword arguments
  (so the pre-feature invoker *arities* and keyword gating are pinned,
  not just the payload),
- the raised ``BedrockInferenceError`` (type, node id, message) or the
  recorded ``{'error': ...}`` node outcome,
- the merged Run_Metadata the processor returns,
- the run's node-status map (recorded error outcomes),
- the run artifacts written to the output directory (Detection_Crop,
  Original_Image, Annotated_Image) with their decoded dimensions,
- the order of ``duration_sink`` reports.

Scenario content (prompt, system prompt, model, region, max_tokens,
node id, detection id, prose, trigger value) is drawn freely by
Hypothesis and **symbolized** out of the observation (``{prompt}``,
``{system_prompt}``, ``{max_tokens}``, ...) before it is compared, so
the fixture pins the *composition* — the appended
``BEDROCK_JSON_INSTRUCTION``, the image labels and their order, the
argument positions, the metadata key layout, the artifact names — for
any content, with a finite baseline. Image bytes are symbolized to the
frame they came from, or described by their decoded dimensions when
derived (crop / downscale), so the fixture never depends on JPEG
encoder output.

Everything the situation needs is a fake: a recording invoker, a
temporary artifact directory, base64 payload references (never the
network) and a real ``NodeStatusCollector``.

Regenerating the baseline is a deliberate act: run this file with
``DDA_WRITE_PRESERVATION_BASELINE=1``. Doing so after the refactor
would defeat the purpose of the property — a failure here means the
refactor changed device behaviour and must be fixed, not rebaselined.
"""
import base64
import json
import os
import shutil
import tempfile

import cv2
import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.node_status import NodeStatusCollector
from workflow_engine.output_bindings import (
    BedrockInferenceError,
    BedrockInferenceProcessor,
    LlmInferenceProcessor,
    RunContext,
)

# ---------------------------------------------------------------------------
# Fixture location and the regeneration switch
# ---------------------------------------------------------------------------

BASELINE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "anomaly_invocation_preservation_baseline.json",
)

#: Set to "1" to (re)capture the baseline from the CURRENT code. Only
#: legitimate while the pre-refactor executor is in place (spec task
#: 1.1); after the refactor a mismatch is a behaviour change to fix.
WRITE_BASELINE = os.environ.get("DDA_WRITE_PRESERVATION_BASELINE") == "1"


# ---------------------------------------------------------------------------
# Fixed scenario material (never drawn: the observation must not depend
# on JPEG bytes, so frame sizes and pixel content stay case-level)
# ---------------------------------------------------------------------------

CAPTURE_ID = "cap-preservation"
INPUT_FRAME_NAME = "input_cam.jpg"
REFERENCE_FRAME_NAME = "reference_cam.jpg"
MISSING_INPUT_NAME = "missing_input.jpg"
MISSING_REFERENCE_NAME = "missing_reference.jpg"
INPUT_FRAME_SIZE = (100, 80)
REFERENCE_FRAME_SIZE = (60, 40)

#: The detection the crop path selects, in source-frame pixels.
DETECTION_BOX = {"x_min": 10, "y_min": 8, "x_max": 60, "y_max": 48}

#: The canonical parseable answer (fixed: the parsed verdict values
#: land in the metadata, so drawing them would make the fixture depend
#: on the draw rather than on the executor's composition).
VERDICT_JSON = '{"is_anomalous": true, "confidence": 0.87}'
VERDICT_WITH_OBJECTS_JSON = (
    '{"is_anomalous": true, "confidence": 0.87, "objects": '
    '[{"name": "chip", "qc": "NOK", "bounding_box": '
    '{"x_min": 2, "y_min": 2, "x_max": 20, "y_max": 18}}]}'
)

#: The generation_metrics payload the metrics-emitting LLM fake reports.
GENERATION_METRICS = {
    "queueing_ms": 12,
    "prefill_ms": 34,
    "decode_ms": 56,
    "prompt_tokens": 78,
    "image_tokens": 90,
    "output_tokens": 21,
    "truncated": False,
}

#: A denied payload reference: the prefix gate rejects it BEFORE any
#: fetch, so this test never touches the network.
DENIED_REFERENCE_URI = "https://example.invalid/reference.png"
ALLOWED_URI_PREFIXES = "s3://allowed-bucket/\n"
PAYLOAD_REFERENCE_PATH = "ref.image"


def _png_bytes():
    """A small real PNG the payload-reference validator accepts."""
    array = np.arange(48, dtype=np.uint8).reshape(4, 4, 3)
    ok, encoded = cv2.imencode(".png", array)
    assert ok
    return encoded.tobytes()


PAYLOAD_PNG_BYTES = _png_bytes()
PAYLOAD_PNG_BASE64 = base64.b64encode(PAYLOAD_PNG_BYTES).decode("ascii")


def _write_jpeg(path, size, tint):
    """A deterministic JPEG frame (gradient) so crops decode and size."""
    width, height = size
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    frame[:, :, 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    frame[:, :, 2] = np.uint8(tint)
    assert cv2.imwrite(path, frame)
    with open(path, "rb") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# Drawn scenario content, and its symbols
# ---------------------------------------------------------------------------

#: Drawn text carries a sentinel so symbolization is unambiguous, and
#: excludes digits and braces so it can never contain a node/detection
#: id (which do carry digits) nor break ``render_prompt``.
_TEXT_ALPHABET = "abcdefghijklmnopqrstuvwxyz ,.:-_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_TEXT = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=32)
_HEX = st.text(alphabet="0123456789abcdef", min_size=8, max_size=8)


class Content(object):
    """One draw of the free scenario content."""

    FIELDS = (
        "prompt", "system_prompt", "model", "region", "max_tokens",
        "node_id", "detection_id", "prose", "trigger_value",
    )

    def __init__(self, prompt, system_prompt, model, region, max_tokens,
                 node_id, detection_id, prose, trigger_value):
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.model = model
        self.region = region
        self.max_tokens = max_tokens
        self.node_id = node_id
        self.detection_id = detection_id
        self.prose = prose
        self.trigger_value = trigger_value


def _sentinel(kind, text):
    return "@@{0}:{1}@@".format(kind, text)


CONTENT = st.builds(
    Content,
    prompt=_TEXT.map(lambda t: _sentinel("prompt", t)),
    system_prompt=_TEXT.map(lambda t: _sentinel("system", t)),
    model=_TEXT.map(lambda t: _sentinel("model", t)),
    region=_TEXT.map(lambda t: _sentinel("region", t)),
    # 1000..4000 never collides with a literal the executor substitutes
    # (256 default, frame dimensions, token counts).
    max_tokens=st.integers(min_value=1000, max_value=4000),
    node_id=_HEX.map(lambda h: "node-" + h),
    detection_id=_HEX.map(lambda h: "det-" + h),
    prose=_TEXT.map(lambda t: _sentinel("prose", t)),
    trigger_value=_TEXT.map(lambda t: _sentinel("trigger", t)),
)

#: The content the baseline was captured with (and the exhaustive
#: whole-table test uses): one fixed draw, so a regeneration is
#: reproducible.
DEFAULT_CONTENT = Content(
    prompt=_sentinel("prompt", "inspect the part"),
    system_prompt=_sentinel("system", "you are a QA inspector"),
    model=_sentinel("model", "nova-lite"),
    region=_sentinel("region", "us-east-2"),
    max_tokens=1234,
    node_id="node-0123abcd",
    detection_id="det-89efcdab",
    prose=_sentinel("prose", "the plate looks fine to me"),
    trigger_value=_sentinel("trigger", "part XYZ"),
)


# ---------------------------------------------------------------------------
# Recording fakes
# ---------------------------------------------------------------------------

class RecordingBedrockInvoker(object):
    """Records every Converse invocation verbatim (``*args`` so the
    pre-feature positional arity is observable)."""

    def __init__(self, answer, error_text=None):
        self.answer = answer
        self.error_text = error_text
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append({
            "positional": list(args),
            "keywords": dict(kwargs),
        })
        if self.error_text is not None:
            raise RuntimeError(self.error_text)
        return self.answer


class RecordingLlmInvoker(object):
    """Text_Generation_API fake accepting any keyword (so the
    processor's ``metrics_sink`` gating forwards the sink)."""

    def __init__(self, answer, error_text=None, metrics=None):
        self.answer = answer
        self.error_text = error_text
        self.metrics = metrics
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append({
            "positional": list(args),
            "keywords": dict(kwargs),
        })
        sink = kwargs.get("metrics_sink")
        if sink is not None:
            sink(self.metrics)
        if self.error_text is not None:
            raise RuntimeError(self.error_text)
        return self.answer


class ExplicitLlmInvoker(object):
    """A pre-``metrics_sink`` fake: it accepts ``system_prompt`` but
    declares no ``**kwargs``, so ``_accepts_keyword`` must NOT forward
    the metrics sink to it."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, *args, system_prompt=None):
        keywords = {}
        if system_prompt is not None:
            keywords["system_prompt"] = system_prompt
        self.calls.append({
            "positional": list(args),
            "keywords": keywords,
        })
        return self.answer


# ---------------------------------------------------------------------------
# The scenario case table (the enumerated situations the baseline keys on)
# ---------------------------------------------------------------------------

BEDROCK_IMAGE_SITUATIONS = (
    "whole_frame",            # 'in' fed and readable
    "whole_frame_unfed",      # capturePaths.in None -> raises
    "whole_frame_unreadable",  # 'in' path missing on disk -> raises
    "crop_ok",                # crop_detection_index resolves
    "crop_index_out_of_range",  # recorded error
    "crop_no_detections",     # recorded error
    "crop_malformed_entry",   # recorded error
    "crop_unfed",             # recorded error (never a raise)
)

BEDROCK_REFERENCE_SITUATIONS = (
    "unfed",
    "fed_readable",
    "fed_unreadable",         # warning + single-image inference
    "payload_base64",
    "payload_denied",         # recorded error, no fetch
    "payload_no_trigger",     # recorded error
    "payload_unresolvable",   # recorded error
)

BEDROCK_MODES = ("anomaly_default", "anomaly_true", "freeform")
SYSTEM_SITUATIONS = ("absent", "whitespace", "present")
BEDROCK_ANSWERS = (
    "verdict", "verdict_fenced", "verdict_prose", "verdict_objects",
    "unparseable", "empty", "raises",
)

LLM_IMAGE_SITUATIONS = (
    "in_fed_readable", "in_absent_key", "in_unfed", "in_unreadable",
)
LLM_REFERENCE_SITUATIONS = (
    "unfed", "absent_key", "fed_readable", "fed_unreadable",
)
LLM_MODES = ("freeform_absent", "anomaly_true", "freeform_false")
LLM_MAX_TOKENS = ("absent", "valid", "invalid")
LLM_MAX_DIMS = ("absent", "downscale", "noop", "invalid")
LLM_ANSWERS = ("verdict", "verdict_fenced", "unparseable", "raises")
LLM_TEMPLATES = ("plain", "placeholder", "unresolved")
LLM_INVOKERS = ("kwargs_metrics_capable", "explicit_no_metrics")


def _bedrock_case(**overrides):
    case = {
        "node": "bedrock",
        "image": "whole_frame",
        "reference": "unfed",
        "mode": "anomaly_default",
        "system": "absent",
        "answer": "verdict",
        "margin": "absent",
    }
    case.update(overrides)
    return case


def _llm_case(**overrides):
    case = {
        "node": "llm",
        "image": "in_fed_readable",
        "reference": "unfed",
        "mode": "anomaly_true",
        "system": "absent",
        "max_tokens": "absent",
        "max_dim": "absent",
        "answer": "verdict",
        "template": "plain",
        "invoker": "kwargs_metrics_capable",
        "metrics": False,
    }
    case.update(overrides)
    return case


def _case_key(case):
    return "|".join(
        "{0}={1}".format(field, case[field])
        for field in sorted(case)
    )


def _build_cases():
    """The enumerated situation table.

    Built one-factor-at-a-time around a base configuration per node type
    (plus the image x reference product, where the interaction matters),
    so the table stays small enough to snapshot while covering every
    situation the design's Property 7 names."""
    cases = {}

    def add(case):
        cases.setdefault(_case_key(case), case)

    # Bedrock: every image situation (reference held at 'unfed').
    for image in BEDROCK_IMAGE_SITUATIONS:
        add(_bedrock_case(image=image))
    # Bedrock: every reference situation on both image-resolution paths.
    for image in ("whole_frame", "crop_ok"):
        for reference in BEDROCK_REFERENCE_SITUATIONS:
            add(_bedrock_case(image=image, reference=reference))
    # Bedrock: response mode x system prompt.
    for mode in BEDROCK_MODES:
        for system in SYSTEM_SITUATIONS:
            add(_bedrock_case(reference="fed_readable", mode=mode,
                              system=system))
    # Bedrock: every answer shape on the crop path (artifacts + parse).
    for answer in BEDROCK_ANSWERS:
        for mode in ("anomaly_default", "freeform"):
            add(_bedrock_case(image="crop_ok", reference="fed_readable",
                              mode=mode, answer=answer))
    # Bedrock: crop margin resolution.
    for margin in ("absent", "25", "invalid"):
        add(_bedrock_case(image="crop_ok", margin=margin))

    # LLM: one factor at a time around the base case.
    for image in LLM_IMAGE_SITUATIONS:
        add(_llm_case(image=image))
    for reference in LLM_REFERENCE_SITUATIONS:
        add(_llm_case(reference=reference))
    for mode in LLM_MODES:
        add(_llm_case(mode=mode))
    for system in SYSTEM_SITUATIONS:
        add(_llm_case(system=system))
    for max_tokens in LLM_MAX_TOKENS:
        add(_llm_case(max_tokens=max_tokens))
    for max_dim in LLM_MAX_DIMS:
        for reference in ("unfed", "fed_readable"):
            add(_llm_case(max_dim=max_dim, reference=reference))
    for answer in LLM_ANSWERS:
        add(_llm_case(answer=answer))
    for template in LLM_TEMPLATES:
        add(_llm_case(template=template))
    for invoker in LLM_INVOKERS:
        add(_llm_case(invoker=invoker))
    add(_llm_case(metrics=True))
    add(_llm_case(invoker="explicit_no_metrics", metrics=True))
    return cases


CASES = _build_cases()
CASE_KEYS = sorted(CASES)


# ---------------------------------------------------------------------------
# Symbolization: content out, structure in
# ---------------------------------------------------------------------------

#: The base64 alphabet, for recognizing an encoded image in a request
#: body field (the LLM path sends base64 text, not bytes).
_B64_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")


class Symbols(object):
    """Replaces the run's drawn content and derived bytes with stable
    tokens so the observation depends only on the scenario."""

    def __init__(self):
        self._bytes = []       # (value, token), longest first
        self._text = []        # (value, token), longest first
        self._ints = {}        # int value -> token

    def add_bytes(self, value, token):
        if not value:
            return
        self._bytes.append((value, token))
        self._bytes.sort(key=lambda pair: len(pair[0]), reverse=True)

    def add_text(self, value, token):
        if not isinstance(value, str) or not value:
            return
        self._text.append((value, token))
        self._text.sort(key=lambda pair: len(pair[0]), reverse=True)

    def add_int(self, value, token):
        self._ints[value] = token

    def describe_bytes(self, value):
        value = bytes(value)
        for known, token in self._bytes:
            if value == known:
                return token
        image = cv2.imdecode(np.frombuffer(value, dtype=np.uint8),
                             cv2.IMREAD_COLOR)
        if image is None:
            return "<opaque bytes>"
        height, width = image.shape[:2]
        return "<derived image {0}x{1}>".format(width, height)

    def _describe_base64(self, value):
        """The token for a base64-encoded image field, or None when the
        string is not base64 image text. Derived encodings (downscaled
        frames) are described by their decoded dimensions so the
        observation never depends on JPEG encoder output."""
        if len(value) < 64 or set(value) - _B64_CHARS:
            return None
        try:
            data = base64.b64decode(value, validate=True)
        except Exception:  # noqa: BLE001 - not base64 after all
            return None
        described = self.describe_bytes(data)
        if described == "<opaque bytes>":
            return None
        return "{{base64 of {0}}}".format(described.strip("{}<>"))

    def text(self, value):
        described = self._describe_base64(value)
        if described is not None:
            return described
        for known, token in self._text:
            if known in value:
                value = value.replace(known, token)
        return value

    def __call__(self, value):
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (bytes, bytearray)):
            return self.describe_bytes(value)
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, int):
            return self._ints.get(value, value)
        if isinstance(value, float):
            return value
        if isinstance(value, dict):
            return {
                (self(key) if isinstance(key, str) else key): self(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self(item) for item in value]
        if callable(value):
            return "<callable>"
        return "<{0}>".format(type(value).__name__)


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------

def _answer_for(kind, content):
    """``(answer_text, invoker_error_text)`` for an answer situation."""
    if kind == "verdict":
        return VERDICT_JSON, None
    if kind == "verdict_fenced":
        return "```json\n" + VERDICT_JSON + "\n```", None
    if kind == "verdict_prose":
        return "{0}\n{1}\n{0}".format(content.prose, VERDICT_JSON), None
    if kind == "verdict_objects":
        return VERDICT_WITH_OBJECTS_JSON, None
    if kind == "unparseable":
        return content.prose, None
    if kind == "empty":
        return "", None
    if kind == "raises":
        return None, "invoker failed: " + content.prose
    raise AssertionError("unknown answer situation " + kind)


def _system_value(situation, content):
    if situation == "absent":
        return None
    if situation == "whitespace":
        return "   "
    return content.system_prompt


def _register_artifact_bytes(output_dir, symbols):
    """Give the run's artifacts their own byte tokens BEFORE the
    observation is symbolized, so the bytes handed to the invoker
    resolve to the artifact they were persisted as — pinning that the
    Detection_Crop sent is byte-identical to the persisted crop and to
    the Inspection's Original_Image."""
    for name in sorted(os.listdir(output_dir)):
        path = os.path.join(output_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            data = handle.read()
        if ".crop." in name:
            symbols.add_bytes(data, "{detection_crop_bytes}")
        elif name.endswith(".annotated.jpg"):
            symbols.add_bytes(data, "{annotated_frame_bytes}")


def _observe_artifacts(output_dir, symbols):
    entries = []
    for name in sorted(os.listdir(output_dir)):
        path = os.path.join(output_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            data = handle.read()
        entries.append([symbols(name), symbols.describe_bytes(data)])
    return entries


def _run_bedrock(case, content, work_dir, output_dir, symbols):
    node_id = content.node_id
    input_bytes = _write_jpeg(
        os.path.join(work_dir, INPUT_FRAME_NAME), INPUT_FRAME_SIZE, 40)
    symbols.add_bytes(input_bytes, "{input_frame_bytes}")

    parameters = {
        "model": content.model,
        "region": content.region,
        "max_tokens": content.max_tokens,
        "prompt": content.prompt,
    }
    if case["mode"] == "anomaly_true":
        parameters["anomaly_mode"] = True
    elif case["mode"] == "freeform":
        parameters["anomaly_mode"] = False
    system_prompt = _system_value(case["system"], content)
    if system_prompt is not None:
        parameters["system_prompt"] = system_prompt

    tag_values = {}

    # -- image resolution situation -------------------------------------
    image = case["image"]
    if image == "whole_frame_unfed" or image == "crop_unfed":
        capture_paths = {"in": None}
    elif image == "whole_frame_unreadable":
        capture_paths = {"in": "{work_dir}/" + MISSING_INPUT_NAME}
    else:
        capture_paths = {"in": "{work_dir}/" + INPUT_FRAME_NAME}
    if image.startswith("crop"):
        parameters["crop_detection_index"] = (
            3 if image == "crop_index_out_of_range" else 0)
        if image == "crop_malformed_entry":
            tag_values["detections"] = [{"id": content.detection_id}]
        elif image != "crop_no_detections":
            entry = {"id": content.detection_id}
            entry.update(DETECTION_BOX)
            tag_values["detections"] = [entry]
        if case["margin"] == "25":
            parameters["crop_margin_percent"] = 25
        elif case["margin"] == "invalid":
            parameters["crop_margin_percent"] = "wide"

    # -- reference situation --------------------------------------------
    reference = case["reference"]
    if reference == "fed_readable":
        reference_bytes = _write_jpeg(
            os.path.join(work_dir, REFERENCE_FRAME_NAME),
            REFERENCE_FRAME_SIZE, 200)
        symbols.add_bytes(reference_bytes, "{reference_frame_bytes}")
        capture_paths["reference"] = "{work_dir}/" + REFERENCE_FRAME_NAME
    elif reference == "fed_unreadable":
        capture_paths["reference"] = "{work_dir}/" + MISSING_REFERENCE_NAME
    else:
        capture_paths["reference"] = None
    if reference.startswith("payload"):
        symbols.add_bytes(PAYLOAD_PNG_BYTES, "{payload_reference_bytes}")
        parameters["reference_payload_path"] = (
            "ref.missing" if reference == "payload_unresolvable"
            else PAYLOAD_REFERENCE_PATH)
        if reference == "payload_denied":
            parameters["allowed_uri_prefixes"] = ALLOWED_URI_PREFIXES
            tag_values["trigger"] = {
                "payload_json": {"ref": {"image": DENIED_REFERENCE_URI}}}
        elif reference == "payload_no_trigger":
            pass
        else:
            tag_values["trigger"] = {
                "payload_json": {"ref": {"image": PAYLOAD_PNG_BASE64}}}

    answer, error_text = _answer_for(case["answer"], content)
    invoker = RecordingBedrockInvoker(answer, error_text)
    node_status = NodeStatusCollector(extra_node_ids=[node_id])
    run_context = RunContext(
        tag_values=tag_values,
        output_dir=output_dir,
        capture_id=CAPTURE_ID,
        graph_document=None,
        node_status=node_status,
    )
    document = {
        "schemaVersion": 1,
        "executorBindings": [{
            "nodeId": node_id,
            "binding": "bedrock_inference",
            "parameters": parameters,
            "upstreamNodeIds": ["cam"],
            "downstreamNodeIds": ["mqtt"],
            "capturePaths": capture_paths,
        }],
    }
    durations = []
    processor = BedrockInferenceProcessor(invoker=invoker)
    raised = None
    metadata = None
    try:
        metadata = processor.process(
            document, tag_values, work_dir,
            duration_sink=lambda nid, ms: durations.append(nid),
            run_context=run_context,
        )
    except BedrockInferenceError as error:
        raised = {
            "type": type(error).__name__,
            "node_id": error.node_id,
            "message": str(error),
        }
    status_map = {
        nid: {"status": entry.get("status"), "detail": entry.get("detail")}
        for nid, entry in node_status.to_map().items()
    }
    return {
        "invocations": invoker.calls,
        "raised": raised,
        "metadata": metadata,
        "node_status": status_map,
        "durations": durations,
    }


def _run_llm(case, content, work_dir, output_dir, symbols):
    node_id = content.node_id
    input_bytes = _write_jpeg(
        os.path.join(work_dir, INPUT_FRAME_NAME), INPUT_FRAME_SIZE, 40)
    symbols.add_bytes(input_bytes, "{input_frame_bytes}")

    if case["template"] == "placeholder":
        template = content.prompt + " {part_id}"
    elif case["template"] == "unresolved":
        template = content.prompt + " {absent_key}"
    else:
        template = content.prompt
    parameters = {
        "modelName": content.model,
        "prompt_template": template,
        "temperature": 0.7,
        "top_p": 1.0,
    }
    if case["mode"] == "anomaly_true":
        parameters["anomaly_mode"] = True
    elif case["mode"] == "freeform_false":
        parameters["anomaly_mode"] = False
    if case["max_tokens"] == "valid":
        parameters["max_tokens"] = content.max_tokens
    elif case["max_tokens"] == "invalid":
        parameters["max_tokens"] = 0
    if case["max_dim"] == "downscale":
        parameters["max_image_dimension"] = 16
    elif case["max_dim"] == "noop":
        parameters["max_image_dimension"] = 4096
    elif case["max_dim"] == "invalid":
        parameters["max_image_dimension"] = -5
    system_prompt = _system_value(case["system"], content)
    if system_prompt is not None:
        parameters["system_prompt"] = system_prompt

    capture_paths = {}
    image = case["image"]
    if image == "in_fed_readable":
        capture_paths["in"] = "{work_dir}/" + INPUT_FRAME_NAME
    elif image == "in_unfed":
        capture_paths["in"] = None
    elif image == "in_unreadable":
        capture_paths["in"] = "{work_dir}/" + MISSING_INPUT_NAME
    reference = case["reference"]
    if reference == "fed_readable":
        reference_bytes = _write_jpeg(
            os.path.join(work_dir, REFERENCE_FRAME_NAME),
            REFERENCE_FRAME_SIZE, 200)
        symbols.add_bytes(reference_bytes, "{reference_frame_bytes}")
        capture_paths["reference"] = "{work_dir}/" + REFERENCE_FRAME_NAME
    elif reference == "fed_unreadable":
        capture_paths["reference"] = "{work_dir}/" + MISSING_REFERENCE_NAME
    elif reference == "unfed":
        capture_paths["reference"] = None

    binding = {
        "nodeId": node_id,
        "binding": "llm_inference",
        "parameters": parameters,
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
    }
    if image != "in_absent_key":
        binding["capturePaths"] = capture_paths
    elif capture_paths:
        binding["capturePaths"] = {
            key: value for key, value in capture_paths.items()
            if key != "in"
        }

    answer, error_text = _answer_for(case["answer"], content)
    if case["invoker"] == "explicit_no_metrics":
        invoker = ExplicitLlmInvoker(answer)
        if error_text is not None:
            # The explicit fake exists to pin keyword gating, not error
            # handling; keep the error path on the kwargs fake.
            invoker = RecordingLlmInvoker(answer, error_text)
    else:
        invoker = RecordingLlmInvoker(
            answer, error_text,
            metrics=GENERATION_METRICS if case["metrics"] else None)

    tag_values = {"part_id": content.trigger_value}
    document = {"schemaVersion": 1, "executorBindings": [binding]}
    durations = []
    processor = LlmInferenceProcessor(invoker=invoker)
    metadata = processor.process(
        document, tag_values, work_dir,
        duration_sink=lambda nid, ms: durations.append(nid),
    )
    return {
        "invocations": invoker.calls,
        "raised": None,
        "metadata": metadata,
        "node_status": {},
        "durations": durations,
    }


def observe(case, content):
    """Run one scenario and return its canonicalized observation."""
    root = tempfile.mkdtemp(prefix="dda-preservation-")
    try:
        work_dir = os.path.join(root, "work")
        output_dir = os.path.join(root, "out")
        os.makedirs(work_dir)
        os.makedirs(output_dir)
        symbols = Symbols()
        symbols.add_text(output_dir, "{output_dir}")
        symbols.add_text(work_dir, "{work_dir}")
        symbols.add_text(content.prompt, "{prompt}")
        symbols.add_text(content.system_prompt, "{system_prompt}")
        symbols.add_text(content.model, "{model}")
        symbols.add_text(content.region, "{region}")
        symbols.add_text(content.prose, "{prose}")
        symbols.add_text(content.trigger_value, "{trigger_value}")
        symbols.add_text(content.node_id, "{node_id}")
        symbols.add_text(content.detection_id, "{detection_id}")
        symbols.add_int(content.max_tokens, "{max_tokens}")

        runner = _run_bedrock if case["node"] == "bedrock" else _run_llm
        observation = runner(case, content, work_dir, output_dir, symbols)
        _register_artifact_bytes(output_dir, symbols)
        observation["artifacts"] = _observe_artifacts(output_dir, symbols)
        canonical = symbols(observation)
        # Round-trip so the comparison is against exactly what the
        # fixture can hold (tuples -> lists, no stray objects).
        return json.loads(json.dumps(canonical, sort_keys=True,
                                     default=lambda value: "<unserializable>"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Baseline access
# ---------------------------------------------------------------------------

def _capture_baseline():
    return {key: observe(CASES[key], DEFAULT_CONTENT) for key in CASE_KEYS}


def _write_baseline():
    baseline = _capture_baseline()
    os.makedirs(os.path.dirname(BASELINE_PATH), exist_ok=True)
    with open(BASELINE_PATH, "w") as handle:
        json.dump(baseline, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return baseline


def _load_baseline():
    if WRITE_BASELINE:
        return _write_baseline()
    if not os.path.exists(BASELINE_PATH):
        pytest.fail(
            "the Property 7 preservation baseline is missing at {0}; it "
            "must be captured from the PRE-refactor executor with "
            "DDA_WRITE_PRESERVATION_BASELINE=1".format(BASELINE_PATH))
    with open(BASELINE_PATH) as handle:
        return json.load(handle)


BASELINE = _load_baseline()


def _assert_matches_baseline(case_key, content):
    expected = BASELINE.get(case_key)
    assert expected is not None, (
        "no baseline entry for scenario {0!r}: the case table changed "
        "without recapturing the baseline".format(case_key))
    observed = observe(CASES[case_key], content)
    assert observed == expected, (
        "scenario {0!r} no longer matches the pre-feature baseline\n"
        "expected: {1}\nobserved: {2}".format(
            case_key,
            json.dumps(expected, indent=1, sort_keys=True),
            json.dumps(observed, indent=1, sort_keys=True)))


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------

@given(case_key=st.sampled_from(CASE_KEYS), content=CONTENT)
@settings(max_examples=100, deadline=None)
def test_property_extraction_is_behaviour_neutral(case_key, content):
    """**Feature: quality-prompt-tuning, Property 7: The extraction into
    ``workflow_core`` is behaviour-neutral.**

    For any supported binding configuration and captured-frame situation
    (drawn from the enumerated table) and ANY scenario content (prompt,
    system prompt, model, region, max_tokens, node id, detection id,
    prose, trigger value — drawn freely and symbolized out), the invoker
    arguments, raised/recorded errors, persisted artifacts and merged
    Run_Metadata equal the baseline captured from the pre-feature
    executor.

    **Validates: Requirements 11.1, 11.2**"""
    _assert_matches_baseline(case_key, content)


@pytest.mark.parametrize("case_key", CASE_KEYS)
def test_every_scenario_matches_the_baseline(case_key):
    """The whole table, deterministically: every enumerated situation is
    compared against its baseline entry with the fixed content the
    baseline was captured with, so no case can go unverified just
    because Hypothesis did not draw it."""
    _assert_matches_baseline(case_key, DEFAULT_CONTENT)


def test_baseline_covers_the_case_table_and_the_named_situations():
    """The fixture and the table agree, and the table really does cover
    the situations Property 7 enumerates (a regression guard against a
    silently shrinking baseline)."""
    assert sorted(BASELINE) == CASE_KEYS
    assert len(CASE_KEYS) >= 60, len(CASE_KEYS)
    for node in ("bedrock", "llm"):
        assert any(CASES[key]["node"] == node for key in CASE_KEYS)
    for situation in BEDROCK_IMAGE_SITUATIONS:
        assert any(CASES[key].get("image") == situation
                   for key in CASE_KEYS), situation
    for situation in BEDROCK_REFERENCE_SITUATIONS:
        assert any(CASES[key].get("reference") == situation
                   for key in CASE_KEYS), situation
    for situation in LLM_IMAGE_SITUATIONS + LLM_REFERENCE_SITUATIONS:
        assert any(CASES[key].get("image") == situation
                   or CASES[key].get("reference") == situation
                   for key in CASE_KEYS), situation
    for mode in BEDROCK_MODES + LLM_MODES:
        assert any(CASES[key].get("mode") == mode for key in CASE_KEYS), mode
    for system in SYSTEM_SITUATIONS:
        assert any(CASES[key].get("system") == system
                   for key in CASE_KEYS), system

    # The baseline is only meaningful if it pins real invocations: at
    # least one scenario per node type must have reached the invoker
    # with the appended verdict instruction, and the crop path must have
    # produced artifacts.
    instruction = 'Respond with JSON: {"is_anomalous": true|false'
    assert any(
        any(instruction in str(part)
            for call in entry["invocations"]
            for part in call["positional"])
        for entry in BASELINE.values())
    assert any(entry["artifacts"] for entry in BASELINE.values())
