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
"""Device unit tests for Sample_Export and the one-shot backfill (spec
task 3.5).

The design's unit-test list for the device side reads: "``SampleExporter``
queue/drop/retry/skip-oversize; startup config parsing; backfill marker
and pairing". Those mechanisms are covered here as ENUMERATED
expectations — the invariants over them are Properties 2, 3, 4 and 17 in
``test_property_tuning_sample_export.py`` (spec task 3.4), so nothing
here draws inputs; every case states its expected value literally.

Everything is a fake: a dict-backed S3 client, recorded ``sleep`` delays,
temporary artifact trees and (for the startup path) stub
``defect_detection_config`` / ``utils.ipc_client`` modules. No AWS call,
no device, no network, no thread that outlives its test.

Requirements: 2.2, 2.4, 2.5, 2.7, 2.10.
"""
import base64
import dataclasses
import hashlib
import json
import logging
import os
import sys
import threading
import types

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine import runtime
from workflow_engine.tuning import backfill as backfill_module
from workflow_engine.tuning import sample_export as export_module
from workflow_engine.tuning.sample_export import (
    BACKOFF_BASE_SECONDS,
    CONFIG_KEY,
    DEFAULT_QUEUE_SIZE,
    MAX_IMAGE_BYTES,
    MAX_UPLOAD_ATTEMPTS,
    METADATA_SNIPPET_MAX_BYTES,
    SIDECAR_SCHEMA_VERSION,
    SOURCE_BACKFILL,
    SOURCE_LIVE,
    THING_NAME_ENV,
    ExportConfig,
    ExportContext,
    ExportedSample,
    SampleExporter,
    build_sidecar,
    configure_sample_exporter,
    export_bedrock_sample,
    export_llm_sample,
    metadata_snippet,
    object_key_base,
    object_keys,
    sample_exporter,
    set_sample_exporter,
    shutdown_sample_exporter,
)
from workflow_engine.vendor.workflow_core.anomaly_invocation import (
    prompt_fingerprint,
)

BUCKET = "dda-inference-results-000000000000"
PREFIX = "workflow-tuning/samples/"
THING_NAME = "dda-edge-under-test"
WORKFLOW_ID = "wf-24680"
EXECUTION_ID = "exec-0f1e2d3c"
NODE_ID = "bedrock_1"

INPUT_BYTES = b"\xff\xd8input-image\xff\xd9"
REFERENCE_BYTES = b"\xff\xd8reference-image\xff\xd9"
VERDICT_JSON = '{"is_anomalous": true, "confidence": 0.87}'

CONFIG = ExportConfig(bucket=BUCKET, prefix=PREFIX)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeS3:
    """A dict-backed S3 client recording every ``put_object`` in order."""

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


class StalledExporter(SampleExporter):
    """A real exporter whose upload worker never starts, so the queue
    bound and the drop-oldest rule are observable without threads."""

    def start(self):  # noqa: D102 - see class docstring
        return None


def sample(execution_id=EXECUTION_ID, input_bytes=INPUT_BYTES,
           reference_bytes=None, **overrides):
    """One ExportedSample with the fixed identifiers of this module."""
    fields = {
        "workflow_id": WORKFLOW_ID,
        "node_id": NODE_ID,
        "node_type": "bedrock_inference",
        "execution_id": execution_id,
        "input_bytes": input_bytes,
        "version": "7",
        "reference_bytes": reference_bytes,
        "answer": VERDICT_JSON,
        "verdict": {"is_anomalous": True, "confidence": 0.87},
        "prompt_fingerprint": "sha256:deadbeef",
    }
    fields.update(overrides)
    return ExportedSample(**fields)


def drained(exporter, timeout=10.0):
    assert exporter.wait_idle(timeout), "the upload worker did not drain"


# ===========================================================================
# Startup configuration parsing (Requirements 2.1, 2.6, 11.3)
# ===========================================================================

#: ``(section, expected (bucket, prefix) or None)`` — the enumerated table
#: the startup parser is pinned to.
CONFIG_ROWS = [
    ({"enabled": True, "bucket": BUCKET, "prefix": PREFIX},
     (BUCKET, PREFIX)),
    ({"enabled": "true", "bucket": BUCKET, "prefix": PREFIX},
     (BUCKET, PREFIX)),
    ({"enabled": " TRUE ", "bucket": BUCKET, "prefix": PREFIX},
     (BUCKET, PREFIX)),
    # The bucket is trimmed; the prefix is taken verbatim.
    ({"enabled": True, "bucket": "  " + BUCKET + " ", "prefix": "custom/x/"},
     (BUCKET, "custom/x/")),
    # Unknown keys are ignored.
    ({"enabled": True, "bucket": BUCKET, "prefix": PREFIX, "extra": 1},
     (BUCKET, PREFIX)),
    # Disabled in every shape that is not an explicit true.
    ({"bucket": BUCKET, "prefix": PREFIX}, None),
    ({"enabled": False, "bucket": BUCKET, "prefix": PREFIX}, None),
    ({"enabled": None, "bucket": BUCKET, "prefix": PREFIX}, None),
    ({"enabled": "yes", "bucket": BUCKET, "prefix": PREFIX}, None),
    ({"enabled": 1, "bucket": BUCKET, "prefix": PREFIX}, None),
    ({"enabled": "1", "bucket": BUCKET, "prefix": PREFIX}, None),
    # A missing, blank or non-string bucket.
    ({"enabled": True, "prefix": PREFIX}, None),
    ({"enabled": True, "bucket": "", "prefix": PREFIX}, None),
    ({"enabled": True, "bucket": "   ", "prefix": PREFIX}, None),
    ({"enabled": True, "bucket": 42, "prefix": PREFIX}, None),
    # A missing, blank, non-string or non-'/'-terminated prefix.
    ({"enabled": True, "bucket": BUCKET}, None),
    ({"enabled": True, "bucket": BUCKET, "prefix": ""}, None),
    ({"enabled": True, "bucket": BUCKET,
      "prefix": "workflow-tuning/samples"}, None),
    ({"enabled": True, "bucket": BUCKET, "prefix": 7}, None),
    # Malformed sections.
    (None, None),
    ("enabled", None),
    ([{"enabled": True}], None),
]


@pytest.mark.parametrize("section,expected", CONFIG_ROWS)
def test_export_config_parsing_table(section, expected):
    """The ``workflowTuning`` section parses to exactly one location, or
    to ``None`` (export disabled) — Requirement 2.6."""
    parsed = ExportConfig.from_section(section)
    whole = ExportConfig.from_component_configuration({CONFIG_KEY: section})
    assert whole == parsed
    if expected is None:
        assert parsed is None
    else:
        assert (parsed.bucket, parsed.prefix) == expected


@pytest.mark.parametrize("configuration", [
    None, "workflowTuning", ["workflowTuning"], 7,
    {"otherComponent": {"enabled": True, "bucket": BUCKET}},
])
def test_absent_or_non_mapping_configuration_disables_export(configuration):
    """A configuration without a usable ``workflowTuning`` section
    disables export (Requirement 11.3: the pre-feature state)."""
    assert ExportConfig.from_component_configuration(configuration) is None


def test_configure_installs_the_exporter_and_builds_no_client():
    """A valid configuration installs the process-wide exporter with the
    exact location; no S3 client is constructed at parse time."""
    created = []

    def factory():
        created.append(True)
        return FakeS3()

    try:
        exporter = configure_sample_exporter(
            {CONFIG_KEY: {"enabled": "true", "bucket": BUCKET,
                          "prefix": PREFIX}},
            s3_factory=factory, thing_name=THING_NAME)
        assert exporter is not None
        assert sample_exporter() is exporter
        assert exporter.enabled is True
        assert (exporter.config.bucket, exporter.config.prefix) == (
            BUCKET, PREFIX)
        assert exporter.thing_name == THING_NAME
        assert exporter.queue_size == DEFAULT_QUEUE_SIZE
        assert created == []
    finally:
        shutdown_sample_exporter(2.0)
    assert sample_exporter() is None


def test_configure_disabled_installs_nothing_and_clears_a_previous():
    """A disabled configuration installs no exporter and clears any
    previously installed one (Requirement 2.6)."""
    previous = SampleExporter(CONFIG, s3_factory=FakeS3,
                             thing_name=THING_NAME)
    set_sample_exporter(previous)
    try:
        assert configure_sample_exporter(
            {CONFIG_KEY: {"enabled": False, "bucket": BUCKET,
                          "prefix": PREFIX}}) is None
        assert sample_exporter() is None
    finally:
        shutdown_sample_exporter(2.0)


def test_configure_warns_once_for_a_malformed_section(caplog):
    """A present-but-malformed section logs exactly one WARNING naming
    what it needs, so a misconfigured Use_Case is diagnosable."""
    with caplog.at_level(logging.WARNING, logger=export_module.__name__):
        assert configure_sample_exporter(
            {CONFIG_KEY: {"enabled": True, "bucket": BUCKET,
                          "prefix": "no-trailing-slash"}}) is None
    warnings = [record for record in caplog.records
                if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert CONFIG_KEY in message
    assert "prefix" in message


def test_configure_is_silent_when_the_section_is_absent(caplog):
    """The default state of every device that has not enabled tuning
    logs no warning at all."""
    with caplog.at_level(logging.WARNING, logger=export_module.__name__):
        assert configure_sample_exporter({"otherComponent": {}}) is None
    assert [record for record in caplog.records
            if record.levelno >= logging.WARNING] == []


def test_thing_name_defaults_to_the_greengrass_environment(monkeypatch):
    """Without an explicit name the device's thing name comes from the
    Greengrass environment (the Sample_Store key's device segment)."""
    monkeypatch.setenv(THING_NAME_ENV, "adlink-dlap-701")
    exporter = SampleExporter(CONFIG, s3_factory=FakeS3)
    assert exporter.thing_name == "adlink-dlap-701"
    monkeypatch.delenv(THING_NAME_ENV, raising=False)
    assert SampleExporter(CONFIG, s3_factory=FakeS3).thing_name == ""


def test_runtime_startup_reads_the_local_server_component_configuration(
        monkeypatch):
    """``runtime._configure_sample_export`` reads the LocalServer
    component configuration ONCE through the IPC reader and installs the
    exporter it describes (Requirement 2.1).

    The on-device readers are stubbed into ``sys.modules``: the function
    imports them lazily, so this exercises the real startup path without
    Greengrass IPC.
    """
    calls = []

    class _Reader:
        def __init__(self, client):
            calls.append(("reader", client))

        def get_local_server_component_name(self):
            return "aws.edgeml.dda.LocalServer.arm64JP7"

        def get_component_config(self, component_name):
            calls.append(("config", component_name))
            return {CONFIG_KEY: {"enabled": "true", "bucket": BUCKET,
                                 "prefix": PREFIX}}

    config_module = types.ModuleType(
        "defect_detection_config.defect_detection_config")
    config_module.DefectDetectionConfig = _Reader
    ipc_module = types.ModuleType("utils.ipc_client")
    ipc_module.get_ipc_client = lambda: "ipc-client"
    monkeypatch.setitem(
        sys.modules, "defect_detection_config.defect_detection_config",
        config_module)
    monkeypatch.setitem(sys.modules, "utils.ipc_client", ipc_module)
    try:
        exporter = runtime._configure_sample_export()
        assert exporter is not None
        assert exporter.config == CONFIG
        assert sample_exporter() is exporter
        assert calls == [
            ("reader", "ipc-client"),
            ("config", "aws.edgeml.dda.LocalServer.arm64JP7"),
        ]
    finally:
        shutdown_sample_exporter(2.0)


# ===========================================================================
# The bounded queue (Requirements 2.4, 2.10)
# ===========================================================================

def test_queue_is_bounded_and_drops_the_oldest_with_a_warning(caplog):
    """At 200 entries the OLDEST sample is dropped, with a WARNING naming
    its execution, so the newest samples survive a stalled uploader."""
    exporter = StalledExporter(CONFIG, s3_factory=FakeS3,
                               thing_name=THING_NAME)
    with caplog.at_level(logging.WARNING, logger=export_module.__name__):
        for index in range(DEFAULT_QUEUE_SIZE + 5):
            exporter.enqueue(sample(execution_id="exec-{0}".format(index)))
    assert exporter.queue_depth == DEFAULT_QUEUE_SIZE == 200
    warnings = [record.getMessage() for record in caplog.records
                if "queue is full" in record.getMessage()]
    assert len(warnings) == 5
    for index, message in enumerate(warnings):
        assert "execution exec-{0},".format(index) in message
    # The five oldest are gone; the newest is still queued.
    remaining = [entry.execution_id for entry in exporter._queue]
    assert remaining[0] == "exec-5"
    assert remaining[-1] == "exec-204"


@pytest.mark.parametrize("input_size,reference_size", [
    (MAX_IMAGE_BYTES + 1, 0),
    (16, MAX_IMAGE_BYTES + 1),
])
def test_an_oversized_image_skips_the_sample(caplog, input_size,
                                             reference_size):
    """A sample whose input OR reference exceeds 8 MiB is skipped with an
    INFO naming its execution (Requirement 2.10)."""
    oversized = sample(
        execution_id="exec-oversize",
        input_bytes=b"\0" * input_size,
        reference_bytes=(b"\0" * reference_size if reference_size else None))
    assert oversized.oversize is True
    exporter = StalledExporter(CONFIG, s3_factory=FakeS3,
                               thing_name=THING_NAME)
    with caplog.at_level(logging.INFO, logger=export_module.__name__):
        exporter.enqueue(oversized)
    assert exporter.queue_depth == 0
    messages = [record.getMessage() for record in caplog.records]
    assert any("exec-oversize" in message and "exceeds" in message
               for message in messages), messages


def test_a_sample_at_the_cap_is_still_exported():
    """The cap is inclusive: exactly 8 MiB is not oversized."""
    at_cap = sample(input_bytes=b"\0" * MAX_IMAGE_BYTES)
    assert at_cap.oversize is False
    exporter = StalledExporter(CONFIG, s3_factory=FakeS3,
                               thing_name=THING_NAME)
    exporter.enqueue(at_cap)
    assert exporter.queue_depth == 1


def test_enqueue_never_raises_on_a_defective_sample(caplog):
    """``enqueue`` is on the run path, so it swallows everything
    (Requirements 11.1, 11.2)."""

    class _Defective:
        execution_id = "exec-defective"

        @property
        def oversize(self):
            raise RuntimeError("defective sample")

    exporter = StalledExporter(CONFIG, s3_factory=FakeS3,
                               thing_name=THING_NAME)
    with caplog.at_level(logging.DEBUG, logger=export_module.__name__):
        exporter.enqueue(_Defective())
    assert exporter.queue_depth == 0


def test_a_disabled_exporter_allocates_nothing():
    """``config=None`` builds no queue, starts no worker and constructs
    no client (Requirement 2.6)."""

    def factory():
        raise AssertionError("a disabled exporter builds no S3 client")

    exporter = SampleExporter(None, s3_factory=factory)
    assert exporter.enabled is False
    assert exporter._queue is None
    exporter.enqueue(sample())
    exporter.start()
    assert exporter.queue_depth == 0
    assert exporter.wait_idle(0.1) is True
    assert exporter._worker is None
    exporter.stop(0.1)


# ===========================================================================
# Upload: retries, ordering and the sidecar (Requirements 2.3, 2.5)
# ===========================================================================

def test_upload_is_attempted_three_times_with_doubling_backoff(caplog):
    """A permanently failing upload is attempted exactly 3 times with
    1 s then 2 s backoff, then logs ONE error naming the object key and
    changes nothing else (Requirement 2.5)."""
    delays = []
    s3 = FakeS3(always_fail=True)
    exporter = SampleExporter(
        CONFIG, s3_factory=lambda: s3, thing_name=THING_NAME,
        sleep=delays.append)
    with caplog.at_level(logging.ERROR, logger=export_module.__name__):
        exporter.enqueue(sample())
        drained(exporter)
    exporter.stop(2.0)
    assert delays == [BACKOFF_BASE_SECONDS, BACKOFF_BASE_SECONDS * 2]
    assert len(s3.puts) == MAX_UPLOAD_ATTEMPTS == 3
    errors = [record.getMessage() for record in caplog.records
              if record.levelno == logging.ERROR]
    assert len(errors) == 1
    sidecar_key, _, _ = object_keys(PREFIX, sample(), THING_NAME)
    assert sidecar_key in errors[0]
    assert "3 attempt(s)" in errors[0]
    assert s3.objects == {}


def test_a_retried_upload_still_lands():
    """Two failures then success: all three objects are uploaded."""
    s3 = FakeS3(fail_first=2)
    exporter = SampleExporter(
        CONFIG, s3_factory=lambda: s3, thing_name=THING_NAME,
        sleep=lambda _seconds: None)
    try:
        exporter.enqueue(sample(reference_bytes=REFERENCE_BYTES))
        drained(exporter)
    finally:
        exporter.stop(2.0)
    sidecar_key, input_key, reference_key = object_keys(
        PREFIX, sample(reference_bytes=REFERENCE_BYTES), THING_NAME)
    assert set(s3.objects) == {sidecar_key, input_key, reference_key}


def test_images_are_uploaded_before_the_sidecar():
    """The sidecar is the index entry, so it must never exist without the
    objects it references (Requirement 2.3)."""
    s3 = FakeS3()
    exporter = SampleExporter(CONFIG, s3_factory=lambda: s3,
                             thing_name=THING_NAME)
    with_reference = sample(reference_bytes=REFERENCE_BYTES)
    try:
        exporter.enqueue(with_reference)
        drained(exporter)
    finally:
        exporter.stop(2.0)
    sidecar_key, input_key, reference_key = object_keys(
        PREFIX, with_reference, THING_NAME)
    assert s3.puts == [input_key, reference_key, sidecar_key]
    assert s3.objects[input_key]["body"] == INPUT_BYTES
    assert s3.objects[input_key]["contentType"] == "image/jpeg"
    assert s3.objects[reference_key]["body"] == REFERENCE_BYTES
    assert s3.objects[sidecar_key]["contentType"] == "application/json"
    assert all(entry["bucket"] == BUCKET for entry in s3.objects.values())


def test_a_single_image_sample_uploads_two_objects():
    """No reference bytes: no reference key and no reference block."""
    s3 = FakeS3()
    exporter = SampleExporter(CONFIG, s3_factory=lambda: s3,
                             thing_name=THING_NAME)
    try:
        exporter.enqueue(sample())
        drained(exporter)
    finally:
        exporter.stop(2.0)
    sidecar_key, input_key, reference_key = object_keys(
        PREFIX, sample(), THING_NAME)
    assert reference_key is None
    assert set(s3.objects) == {sidecar_key, input_key}
    document = json.loads(s3.objects[sidecar_key]["body"].decode("utf-8"))
    assert "reference" not in document


def test_uploads_run_on_a_daemon_worker_named_for_the_feature():
    """Uploads never run on the enqueueing (execution) thread
    (Requirement 2.4)."""
    seen = []

    class _S3(FakeS3):
        def put_object(self, **kwargs):
            seen.append(threading.current_thread())
            return super().put_object(**kwargs)

    exporter = SampleExporter(CONFIG, s3_factory=_S3,
                             thing_name=THING_NAME)
    try:
        exporter.enqueue(sample())
        drained(exporter)
        assert exporter._worker.daemon is True
    finally:
        exporter.stop(2.0)
    assert seen
    for thread in seen:
        assert thread is not threading.current_thread()
        assert thread.name == "tuning-sample-export"


def test_stop_and_shutdown_are_idempotent():
    """Shutdown is best-effort and may be called twice."""
    exporter = SampleExporter(CONFIG, s3_factory=FakeS3,
                             thing_name=THING_NAME)
    set_sample_exporter(exporter)
    exporter.stop(1.0)
    exporter.stop(1.0)
    shutdown_sample_exporter(1.0)
    shutdown_sample_exporter(1.0)
    assert sample_exporter() is None


# ---------------------------------------------------------------------------
# The object layout and the sidecar document
# ---------------------------------------------------------------------------

def test_object_keys_use_the_identifiers_verbatim():
    """The Portal indexes by listing ``{prefix}{workflowId}/{nodeId}/``,
    so nothing here may be rewritten."""
    odd = dataclasses.replace(sample(reference_bytes=REFERENCE_BYTES),
                              node_id="Bedrock Node 1")
    base = object_key_base(PREFIX, odd, "adlink dlap-701")
    assert base == (
        "workflow-tuning/samples/wf-24680/Bedrock Node 1/adlink dlap-701/"
        + EXECUTION_ID)
    sidecar_key, input_key, reference_key = object_keys(
        PREFIX, odd, "adlink dlap-701")
    assert (sidecar_key, input_key, reference_key) == (
        base + ".json", base + ".input.jpg", base + ".reference.jpg")


def test_sidecar_document_shape():
    """The sidecar carries identifiers, image references and the recorded
    answer — and never image bytes (Requirement 2.3)."""
    with_reference = sample(reference_bytes=REFERENCE_BYTES,
                            detection_id="det-1", detection_slot=2)
    document = build_sidecar(
        with_reference, THING_NAME, PREFIX, exported_at=1_700_000_000)
    sidecar_key, input_key, reference_key = object_keys(
        PREFIX, with_reference, THING_NAME)
    assert document == {
        "schemaVersion": SIDECAR_SCHEMA_VERSION,
        "source": SOURCE_LIVE,
        "workflowId": WORKFLOW_ID,
        # An all-digit registration version is recorded as a number.
        "version": 7,
        "executionId": EXECUTION_ID,
        "nodeId": NODE_ID,
        "nodeType": "bedrock_inference",
        "thingName": THING_NAME,
        "exportedAt": 1_700_000_000,
        "input": {
            "key": input_key,
            "sha256": hashlib.sha256(INPUT_BYTES).hexdigest(),
            "bytes": len(INPUT_BYTES),
        },
        "reference": {
            "key": reference_key,
            "sha256": hashlib.sha256(REFERENCE_BYTES).hexdigest(),
            "bytes": len(REFERENCE_BYTES),
        },
        "recorded": {
            "isAnomalous": True,
            "confidence": 0.87,
            "answer": VERDICT_JSON,
            "parseError": None,
        },
        "promptFingerprint": "sha256:deadbeef",
        "detectionId": "det-1",
        "detectionSlot": 2,
    }
    body = json.dumps(document, sort_keys=True).encode("utf-8")
    for data in (INPUT_BYTES, REFERENCE_BYTES):
        assert data not in body
        assert base64.b64encode(data) not in body
    assert sidecar_key.endswith(".json")


@pytest.mark.parametrize("version,expected", [
    ("7", 7), (7, 7), ("v1.2", "v1.2"), ("", None), (None, None),
])
def test_sidecar_version_normalization(version, expected):
    """The device stores versions as strings; the sidecar records a
    number when the version is all digits."""
    document = build_sidecar(sample(version=version), THING_NAME, PREFIX)
    assert document["version"] == expected


def test_sidecar_records_a_parse_failure_without_a_verdict():
    """An unparseable answer is exported WITH its parse failure."""
    document = build_sidecar(
        sample(verdict=None, parse_error="no JSON object in the answer"),
        THING_NAME, PREFIX)
    assert document["recorded"] == {
        "isAnomalous": None,
        "confidence": None,
        "answer": VERDICT_JSON,
        "parseError": "no JSON object in the answer",
    }


# ---------------------------------------------------------------------------
# The Run_Metadata snippet for llm_inference replay
# ---------------------------------------------------------------------------

def test_metadata_snippet_carries_only_referenced_top_level_keys():
    """Only the top-level keys the Prompt_Template references travel, so
    the manifest path stays small (Requirement 9.6)."""
    metadata = {
        "part_id": "part-XYZ",
        "trigger": {"payload_json": {"lot": "L-9"}},
        "unrelated": "x" * 100,
    }
    assert metadata_snippet(
        "Compare {part_id} against {trigger.payload_json.lot}.",
        metadata) == {
        "part_id": "part-XYZ",
        "trigger": {"payload_json": {"lot": "L-9"}},
    }


@pytest.mark.parametrize("template,metadata", [
    ("no placeholders here", {"part_id": "p"}),
    ("{part_id}", None),
    ("{part_id}", "not a mapping"),
    ("{missing}", {"part_id": "p"}),
    (None, {"part_id": "p"}),
])
def test_metadata_snippet_is_none_when_there_is_nothing_to_carry(
        template, metadata):
    """No placeholders, no metadata or no matching key yields ``None`` —
    the replay then reports the unresolved placeholder."""
    assert metadata_snippet(template, metadata) is None


def test_metadata_snippet_drops_bytes_values():
    """An image or payload blob must never ride in the sidecar
    (Requirement 2.3)."""
    assert metadata_snippet(
        "{frame} {part_id}",
        {"frame": b"\xff\xd8jpeg", "part_id": "p"}) == {"part_id": "p"}


def test_metadata_snippet_is_bounded_largest_key_first():
    """Entries are dropped largest-first until the snippet fits."""
    metadata = {"small": "s", "large": "x" * 4096}
    snippet = metadata_snippet("{small}{large}", metadata, max_bytes=1024)
    assert snippet == {"small": "s"}
    # Nothing fits at all -> None rather than an empty snippet.
    assert metadata_snippet("{large}", metadata, max_bytes=16) is None
    assert METADATA_SNIPPET_MAX_BYTES == 64 * 1024


# ---------------------------------------------------------------------------
# The executor's export call sites: gating and containment
# ---------------------------------------------------------------------------

class _Invocation:
    """The fields the export helpers read off a built invocation."""

    def __init__(self, anomaly_mode=True, images=(), image_b64=None,
                 reference_b64=None):
        self.anomaly_mode = anomaly_mode
        self.images = images
        self.image_b64 = image_b64
        self.reference_b64 = reference_b64


class _Collector:
    """An exporter double recording what the call sites offered."""

    def __init__(self):
        self.samples = []

    def enqueue(self, offered):
        self.samples.append(offered)


CONTEXT = ExportContext(
    workflow_id=WORKFLOW_ID, version="7", execution_id=EXECUTION_ID)
BEDROCK_IMAGES = (("Input image", INPUT_BYTES),
                  ("Reference image", REFERENCE_BYTES))
PROMPT_SET = {"prompt": "Compare the input to the reference.",
              "system_prompt": "You are a QA inspector.", "max_tokens": 512}


def test_bedrock_export_call_site_records_the_sent_images():
    """The exported bytes come off the built invocation, so they are by
    construction the bytes the invoker was handed (Requirement 2.2)."""
    collector = _Collector()
    export_bedrock_sample(
        CONTEXT, NODE_ID, _Invocation(images=BEDROCK_IMAGES),
        dict(PROMPT_SET, crop_detection_index="1"), VERDICT_JSON,
        verdict={"is_anomalous": True, "confidence": 0.87},
        detection_id="det-9", exporter=collector)
    assert len(collector.samples) == 1
    exported = collector.samples[0]
    assert exported.input_bytes == INPUT_BYTES
    assert exported.reference_bytes == REFERENCE_BYTES
    assert exported.node_type == "bedrock_inference"
    assert exported.execution_id == EXECUTION_ID
    assert exported.detection_id == "det-9"
    assert exported.detection_slot == 1
    assert exported.prompt_fingerprint == prompt_fingerprint(
        dict(PROMPT_SET, crop_detection_index="1"))
    assert exported.source == SOURCE_LIVE


def test_llm_export_call_site_decodes_the_sent_base64():
    """The llm path exports the (downscaled) bytes the request carried."""
    collector = _Collector()
    export_llm_sample(
        CONTEXT, "llm_1",
        _Invocation(
            image_b64=base64.b64encode(INPUT_BYTES).decode("ascii"),
            reference_b64=base64.b64encode(REFERENCE_BYTES).decode("ascii")),
        {"prompt_template": "Compare {part_id}.", "max_tokens": 128},
        VERDICT_JSON, {"part_id": "part-XYZ", "other": 1},
        verdict={"is_anomalous": False, "confidence": 0.1},
        exporter=collector)
    assert len(collector.samples) == 1
    exported = collector.samples[0]
    assert exported.input_bytes == INPUT_BYTES
    assert exported.reference_bytes == REFERENCE_BYTES
    assert exported.node_type == "llm_inference"
    assert exported.metadata_snippet == {"part_id": "part-XYZ"}


@pytest.mark.parametrize("kind,context,invocation", [
    ("no context", None, _Invocation(images=BEDROCK_IMAGES)),
    ("freeform", CONTEXT, _Invocation(anomaly_mode=False,
                                      images=BEDROCK_IMAGES)),
    ("no image", CONTEXT, _Invocation(images=())),
])
def test_bedrock_export_call_site_gating(kind, context, invocation):
    """No run identity, a freeform invocation or no image exports
    nothing (Requirements 2.2, 11.3)."""
    collector = _Collector()
    export_bedrock_sample(context, NODE_ID, invocation, PROMPT_SET,
                          VERDICT_JSON, exporter=collector)
    assert collector.samples == [], kind


@pytest.mark.parametrize("kind,context,invocation", [
    ("no context", None, _Invocation(image_b64="aGk=")),
    ("freeform", CONTEXT, _Invocation(anomaly_mode=False,
                                      image_b64="aGk=")),
    ("no image", CONTEXT, _Invocation(image_b64=None)),
])
def test_llm_export_call_site_gating(kind, context, invocation):
    """An llm invocation without an image has nothing to replay."""
    collector = _Collector()
    export_llm_sample(context, "llm_1", invocation, {}, VERDICT_JSON,
                      exporter=collector)
    assert collector.samples == [], kind


def test_export_call_sites_contain_a_defective_exporter():
    """A defect in the export path can never reach the run
    (Requirements 11.1, 11.2)."""

    class _Defective:
        def enqueue(self, offered):
            raise RuntimeError("defective exporter")

    export_bedrock_sample(
        CONTEXT, NODE_ID, _Invocation(images=BEDROCK_IMAGES), PROMPT_SET,
        VERDICT_JSON, exporter=_Defective())
    export_llm_sample(
        CONTEXT, "llm_1", _Invocation(image_b64="!!! not base64"),
        {}, VERDICT_JSON, exporter=_Defective())


def test_export_call_sites_are_inert_without_an_installed_exporter():
    """With no process-wide exporter the call sites return immediately
    (the pre-feature state, Requirement 11.3)."""
    set_sample_exporter(None)
    export_bedrock_sample(CONTEXT, NODE_ID,
                          _Invocation(images=BEDROCK_IMAGES), PROMPT_SET,
                          VERDICT_JSON)
    export_llm_sample(CONTEXT, "llm_1", _Invocation(image_b64="aGk="), {},
                      VERDICT_JSON)
    assert sample_exporter() is None


# ===========================================================================
# The one-shot backfill: the marker and the pairing rule (Requirement 2.7)
# ===========================================================================

NODE_FRAME_TEMPLATE = "{capture_id}.node.{node_id}.{port}.jpg"


def _write_artifacts(output_dir, capture_id, node_id, ports, metadata):
    """Write node frames and the run metadata JSON; return the bytes."""
    os.makedirs(output_dir, exist_ok=True)
    written = {}
    for port, data in ports.items():
        path = os.path.join(output_dir, NODE_FRAME_TEMPLATE.format(
            capture_id=capture_id, node_id=node_id, port=port))
        with open(path, "wb") as handle:
            handle.write(data)
        written[port] = data
    with open(os.path.join(output_dir, capture_id + ".json"), "w") as handle:
        json.dump(metadata, handle)
    return written


def _bedrock_node(**parameters):
    fields = {"prompt": "Compare the input to the reference.",
              "system_prompt": "You are a QA inspector.", "max_tokens": 512,
              "crop_detection_index": 3}
    fields.update(parameters)
    return backfill_module.TunableNode(
        node_id=NODE_ID, node_type="bedrock_inference", parameters=fields)


def test_marker_document_records_what_the_walk_did(tmp_path):
    """The marker is the one-shot guard AND the diagnosis record."""
    marker_path = str(tmp_path / "state" / "backfilled.json")
    assert backfill_module.marker_exists(marker_path) is False
    summary = backfill_module.BackfillSummary(
        registrations=1, nodes=2, executions_scanned=4, exported=3)
    summary.skip(backfill_module.SKIP_ERROR_OUTCOME)
    assert backfill_module.write_marker(
        summary, marker_path, config=CONFIG,
        clock=lambda: 1_700_000_000.9) is True
    assert backfill_module.marker_exists(marker_path) is True
    with open(marker_path) as handle:
        document = json.load(handle)
    assert document == {
        "schemaVersion": backfill_module.MARKER_SCHEMA_VERSION,
        "backfilledAt": 1_700_000_000,
        "bucket": BUCKET,
        "prefix": PREFIX,
        "registrations": 1,
        "nodes": 2,
        "executionsScanned": 4,
        "exported": 3,
        "skipped": {backfill_module.SKIP_ERROR_OUTCOME: 1},
        "error": None,
    }


def test_an_unwritable_marker_is_contained(tmp_path):
    """A marker that cannot be written is reported, not raised (the
    backfill then runs again on the next start)."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    assert backfill_module.write_marker(
        backfill_module.BackfillSummary(),
        str(blocker / "backfilled.json")) is False


def test_an_unreadable_marker_path_is_read_as_already_done(monkeypatch):
    """A backfill that cannot prove it is due must not flood the
    Sample_Store: an inspection failure means "already ran"."""

    def _raise(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(backfill_module.os.path, "exists", _raise)
    assert backfill_module.marker_exists("/aws_dda/whatever.json") is True


def test_the_marker_guard_reads_nothing_at_all(tmp_path):
    """With the marker present the walk does not even open the run
    database (a second start exports nothing)."""
    marker_path = str(tmp_path / "backfilled.json")
    backfill_module.write_marker(
        backfill_module.BackfillSummary(), marker_path, config=CONFIG)

    def _factory():
        raise AssertionError("the marker guard must precede every read")

    exporter = SampleExporter(CONFIG, s3_factory=FakeS3,
                             thing_name=THING_NAME)
    summary = backfill_module.run_backfill(
        exporter, session_factory=_factory, marker_path=marker_path)
    assert summary.already_done is True
    assert summary.ran is False
    assert summary.exported == 0


def test_an_unconfigured_device_writes_no_marker(tmp_path):
    """Without export configured nothing is read and NO marker is
    written, so the backfill still happens the day export is enabled."""
    marker_path = str(tmp_path / "never.json")

    def _factory():
        raise AssertionError("an unconfigured backfill reads nothing")

    summary = backfill_module.run_backfill(
        SampleExporter(None), session_factory=_factory,
        marker_path=marker_path)
    assert summary.not_configured is True
    assert os.path.exists(marker_path) is False


def test_a_walk_failure_is_contained_and_recorded(tmp_path):
    """A failing walk is recorded in the summary and the marker, and the
    workflow runtime never sees the error."""
    marker_path = str(tmp_path / "backfilled.json")

    def _factory():
        raise RuntimeError("the run database is unavailable")

    exporter = SampleExporter(CONFIG, s3_factory=FakeS3,
                             thing_name=THING_NAME)
    summary = backfill_module.run_backfill(
        exporter, session_factory=_factory, marker_path=marker_path)
    assert summary.error == "the run database is unavailable"
    with open(marker_path) as handle:
        assert json.load(handle)["error"] == (
            "the run database is unavailable")


@pytest.mark.parametrize("metadata,node_type,expected", [
    ({"bedrock": {NODE_ID: {"text": "a"}}}, "bedrock_inference",
     {"text": "a"}),
    ({"llm": {"llm_1": {"generated_text": "a"}}}, "llm_inference",
     {"generated_text": "a"}),
    ({"bedrock": {"other": {"text": "a"}}}, "bedrock_inference", None),
    ({"llm": {NODE_ID: {"text": "a"}}}, "bedrock_inference", None),
    ({"bedrock": "not a mapping"}, "bedrock_inference", None),
    ({}, "bedrock_inference", None),
    ("not a mapping", "bedrock_inference", None),
])
def test_node_outcome_reads_the_persisted_sections(metadata, node_type,
                                                   expected):
    """``bedrock.{nodeId}`` / ``llm.{nodeId}`` — the shapes
    ``output_bindings`` merges into the run metadata."""
    node = backfill_module.TunableNode(
        node_id=(NODE_ID if node_type == "bedrock_inference" else "llm_1"),
        node_type=node_type, parameters={})
    assert backfill_module.node_outcome(metadata, node) == expected


def test_backfill_pairs_the_crop_frame_when_the_outcome_names_a_detection(
        tmp_path):
    """The executor's pairing rule: ``original`` (the exact
    Detection_Crop bytes sent) plus ``reference`` when persisted."""
    output_dir = str(tmp_path / "run")
    outcome = {"text": VERDICT_JSON, "is_anomalous": True,
               "confidence": 0.87, "detection_id": "det-7"}
    metadata = {"bedrock": {NODE_ID: outcome}}
    frames = _write_artifacts(
        output_dir, "cap-1", NODE_ID,
        # An 'in' frame exists too: the rule must pick 'original'.
        {"original": b"crop-bytes", "in": b"whole-frame-bytes",
         "reference": REFERENCE_BYTES},
        metadata)
    node = _bedrock_node()
    exported, reason = backfill_module.build_sample(
        node, WORKFLOW_ID, "7", "exec-1", output_dir, "cap-1", metadata,
        outcome, started_at=1_699_000_000)
    assert reason is None
    assert exported.input_bytes == frames["original"]
    assert exported.reference_bytes == REFERENCE_BYTES
    assert exported.detection_id == "det-7"
    assert exported.detection_slot == 3
    assert exported.source == SOURCE_BACKFILL
    assert exported.answer == VERDICT_JSON
    assert exported.verdict == {"is_anomalous": True, "confidence": 0.87}
    assert exported.prompt_fingerprint == prompt_fingerprint(node.parameters)
    # A backfilled sample is dated by its RUN, not by the upload.
    assert exported.exported_at == 1_699_000_000


def test_backfill_pairs_the_in_frame_without_a_detection(tmp_path):
    """No ``detection_id``: the captured ``in`` frame, single-image when
    no reference was persisted."""
    output_dir = str(tmp_path / "run")
    outcome = {"text": VERDICT_JSON, "is_anomalous": False,
               "confidence": 0.2}
    metadata = {"bedrock": {NODE_ID: outcome}}
    _write_artifacts(output_dir, "cap-1", NODE_ID, {"in": b"whole-frame"},
                     metadata)
    exported, reason = backfill_module.build_sample(
        _bedrock_node(), WORKFLOW_ID, "7", "exec-1", output_dir, "cap-1",
        metadata, outcome)
    assert reason is None
    assert exported.input_bytes == b"whole-frame"
    assert exported.reference_bytes is None
    assert exported.detection_id is None
    assert exported.detection_slot is None


@pytest.mark.parametrize("ports,outcome,expected_reason", [
    # An error outcome never produced a judged pair.
    ({"in": b"frame"}, {"error": "the model endpoint refused"},
     backfill_module.SKIP_ERROR_OUTCOME),
    # No input frame on disk -> nothing to replay.
    ({"reference": REFERENCE_BYTES}, {"text": VERDICT_JSON},
     backfill_module.SKIP_MISSING_INPUT),
    # An empty frame file carries no image.
    ({"in": b""}, {"text": VERDICT_JSON},
     backfill_module.SKIP_MISSING_INPUT),
])
def test_backfill_skip_reasons(tmp_path, ports, outcome, expected_reason):
    """Requirement 2.7's skips, with the reason the summary counts."""
    output_dir = str(tmp_path / "run")
    metadata = {"bedrock": {NODE_ID: outcome}}
    _write_artifacts(output_dir, "cap-1", NODE_ID, ports, metadata)
    exported, reason = backfill_module.build_sample(
        _bedrock_node(), WORKFLOW_ID, "7", "exec-1", output_dir, "cap-1",
        metadata, outcome)
    assert exported is None
    assert reason == expected_reason


def test_backfill_llm_sample_carries_the_metadata_snippet(tmp_path):
    """An ``llm_inference`` sample carries the Run_Metadata keys its
    Prompt_Template references so a Device_Score_Job can re-render it."""
    output_dir = str(tmp_path / "run")
    outcome = {"generated_text": VERDICT_JSON, "is_anomalous": True,
               "confidence": 0.9}
    metadata = {"llm": {"llm_1": outcome}, "part_id": "part-XYZ",
                "unused": "y"}
    _write_artifacts(output_dir, "cap-1", "llm_1", {"in": b"frame"},
                     metadata)
    node = backfill_module.TunableNode(
        node_id="llm_1", node_type="llm_inference",
        parameters={"modelName": "qwen2-vl-2b",
                    "prompt_template": "Compare {part_id}.",
                    "max_tokens": 128})
    exported, reason = backfill_module.build_sample(
        node, WORKFLOW_ID, "7", "exec-1", output_dir, "cap-1", metadata,
        outcome)
    assert reason is None
    assert exported.metadata_snippet == {"part_id": "part-XYZ"}
    assert exported.node_type == "llm_inference"


def test_backfill_paces_itself_against_the_export_queue():
    """A large backfill waits (bounded) for the queue to drain below half
    its depth, so it cannot drop most of itself; a stalled uploader can
    never stall the backfill thread."""
    slept = []
    clock = {"now": 0.0}

    class _Full:
        enabled = True
        queue_size = 200
        queue_depth = 200

    def _sleep(seconds):
        slept.append(seconds)
        clock["now"] += seconds

    backfill_module._pace(_Full(), 1.0, _sleep, lambda: clock["now"])
    assert slept, "a full queue must be waited on"
    assert sum(slept) <= 1.0 + backfill_module.PACE_POLL_SECONDS

    # An idle queue is not waited on at all, and an exporter that reports
    # no depth is not paced.
    slept.clear()

    class _Idle(_Full):
        queue_depth = 0

    backfill_module._pace(_Idle(), 1.0, _sleep, lambda: clock["now"])
    backfill_module._pace(object(), 1.0, _sleep, lambda: clock["now"])
    assert slept == []


def test_documented_backfill_bounds():
    """The bounds these tests assert against are the design's."""
    assert backfill_module.MAX_EXECUTIONS_PER_NODE == 500
    assert backfill_module.BACKFILL_MARKER_PATH == (
        "/aws_dda/workflow-tuning/backfilled.json")
    assert set(backfill_module.SKIP_REASONS) == {
        "no_artifacts", "no_outcome", "error_outcome", "missing_input",
        "unreadable_input", "unreadable_reference"}
