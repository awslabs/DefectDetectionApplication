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
"""Device end-to-end integration tests for tuning (spec task 3.5).

The design's device integration test reads: "a compiled document with two
anomaly-mode nodes run through the processors with a recording fake
invoker and a fake S3 client → assert three objects per invocation with
byte-identical images and correct sidecars; then a job manifest through
the runner against a fake Text_Generation_API → outcome batches and
shadow reports."

Both legs run here against ONE fake S3 store, so the whole loop is
exercised the way production composes it:

    executions -> Sample_Export objects -> a Device_Score_Job manifest
    built from those objects -> the runner's replay -> outcome batches
    and shadow reports

which is also what makes the replay's faithfulness observable: the bytes
the runner sends are compared against the bytes the production
invocation sent, and (with the Candidate equal to the node's own
Prompt_Set) so is the whole request.

A third test covers the one-shot backfill end to end: an existing
artifact tree plus registration/execution rows through the REAL exporter
into the same fake S3.

Everything is a fake: recording invokers, a dict-backed S3 client, a
dict-backed named shadow, temporary artifact trees and an in-memory
sqlite database. No AWS call, no device, no network.

Requirements: 2.2, 2.7, 6.9, 6.15.
"""
import base64
import copy
import json
import os

import cv2
import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from dao.sqlite_db.sqlite_db_operations import Base
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.node_status import NodeStatusCollector
from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    LlmInferenceProcessor,
    RunContext,
)
from workflow_engine.tuning import backfill as backfill_module
from workflow_engine.tuning.job_runner import (
    OUTCOME_BATCH_SIZE,
    STATUS_COMPLETED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    TUNING_SHADOW_NAME,
    JobRunner,
    manifest_key,
    outcomes_key,
    outcomes_prefix,
)
from workflow_engine.tuning.sample_export import (
    SOURCE_BACKFILL,
    SOURCE_LIVE,
    ExportConfig,
    ExportContext,
    SampleExporter,
    set_sample_exporter,
)

BUCKET = "dda-inference-results-000000000000"
PREFIX = "workflow-tuning/samples/"
THING_NAME = "dda-edge-under-test"
WORKFLOW_ID = "wf-24680"
VERSION = "7"
BEDROCK_NODE = "bedrock_1"
LLM_NODE = "llm_1"
SESSION_ID = "sess-1a2b"
RUN_ID = "run-9f8e"
JOB_ID = "job-5c4d"

CONFIG = ExportConfig(bucket=BUCKET, prefix=PREFIX)

#: Enough executions that the replay (× 2 repeats) spans more than one
#: 20-outcome batch, so batching is exercised at the PRODUCTION bound.
EXECUTIONS = ["exec-{0:04d}".format(index) for index in range(11)]

VERDICT_TRUE = '{"is_anomalous": true, "confidence": 0.91}'
VERDICT_FALSE = '{"is_anomalous": false, "confidence": 0.07}'

BEDROCK_PROMPT = "Compare the input to the reference."
LLM_TEMPLATE = "Inspect {part_id} against the reference."
SYSTEM_PROMPT = "You are a QA inspector."
PART_ID = "part-XYZ"
VERDICT_INSTRUCTION = (
    'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.')


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeS3:
    """One dict-backed S3 store shared by the exporter (puts) and the job
    runner (gets and puts) — the Use_Case bucket, faked."""

    def __init__(self):
        self.objects = {}
        self.content_types = {}
        self.puts = []
        self.gets = []

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
        assert Bucket == BUCKET
        self.puts.append(Key)
        self.objects[Key] = Body
        self.content_types[Key] = ContentType
        return {}

    def get_object(self, Bucket=None, Key=None):
        assert Bucket == BUCKET
        self.gets.append(Key)
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey: " + str(Key))
        return {"Body": _Body(self.objects[Key])}

    def sidecars(self):
        return {
            key: json.loads(body.decode("utf-8"))
            for key, body in self.objects.items()
            if key.endswith(".json") and key.startswith(PREFIX)
        }


class FakeShadow:
    """A named-shadow accessor with IoT's merge semantics."""

    def __init__(self, desired=None):
        self.state = {"desired": {"jobs": dict(desired or {})},
                      "reported": {"jobs": {}}}
        self.reports = []

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        assert (thing_name, shadow_name) == (THING_NAME, TUNING_SHADOW_NAME)
        return copy.deepcopy(self.state)

    def update_thing_shadow_state_request(self, thing_name, shadow_name,
                                         payload):
        jobs = (payload or {}).get("reported", {}).get("jobs", {})
        for job_id, entry in jobs.items():
            self.reports.append((job_id, copy.deepcopy(entry)))
            if entry is None:
                self.state["reported"]["jobs"].pop(job_id, None)
            else:
                self.state["reported"]["jobs"][job_id] = copy.deepcopy(entry)
        return b"{}"

    def entries(self, job_id=JOB_ID):
        return [entry for identifier, entry in self.reports
                if identifier == job_id]


class RecordingInvoker:
    """Records every invocation's positional arguments and keywords."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, *args, **kwargs):
        index = len(self.calls)
        self.calls.append({"positional": list(args), "keywords": dict(kwargs)})
        sink = kwargs.get("metrics_sink")
        if sink is not None:
            sink({"output_tokens": 24})
        return self.answers[index % len(self.answers)]


class _NoRegistrations:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def query(self, *args):
        return self

    def filter(self, *args):
        return self

    def order_by(self, *args):
        return self

    def all(self):
        return []


def no_registrations():
    return _NoRegistrations()


# ---------------------------------------------------------------------------
# The compiled document
# ---------------------------------------------------------------------------

def _write_jpeg(path, size, tint):
    width, height = size
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    frame[:, :, 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    frame[:, :, 2] = np.uint8(tint)
    assert cv2.imwrite(path, frame)
    with open(path, "rb") as handle:
        return handle.read()


BEDROCK_PARAMETERS = {
    "model": "us.amazon.nova-lite-v1:0",
    "region": "us-east-2",
    "max_tokens": 512,
    "prompt": BEDROCK_PROMPT,
    "system_prompt": SYSTEM_PROMPT,
    # anomaly_mode defaults to true for bedrock_inference.
}
LLM_PARAMETERS = {
    "modelName": "qwen2-vl-2b",
    "prompt_template": LLM_TEMPLATE,
    "system_prompt": SYSTEM_PROMPT,
    "temperature": 0.7,
    "top_p": 0.9,
    "max_tokens": 192,
    "anomaly_mode": True,
}


def compiled_document():
    """A compiled document with TWO anomaly-mode Inspection_Nodes."""
    capture_paths = {"in": "{work_dir}/input.jpg",
                     "reference": "{work_dir}/reference.jpg"}
    return {
        "schemaVersion": 1,
        "executorBindings": [
            {
                "nodeId": BEDROCK_NODE,
                "binding": "bedrock_inference",
                "parameters": dict(BEDROCK_PARAMETERS),
                "upstreamNodeIds": ["cam"],
                "downstreamNodeIds": [],
                "capturePaths": dict(capture_paths),
            },
            {
                "nodeId": LLM_NODE,
                "binding": "llm_inference",
                "parameters": dict(LLM_PARAMETERS),
                "upstreamNodeIds": ["cam"],
                "downstreamNodeIds": [],
                "capturePaths": dict(capture_paths),
            },
        ],
    }


@pytest.fixture
def workspace(tmp_path):
    """A work directory with the two frames both nodes capture."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    frames = {
        "in": _write_jpeg(str(work_dir / "input.jpg"), (120, 90), 40),
        "reference": _write_jpeg(str(work_dir / "reference.jpg"), (64, 48),
                                 200),
    }
    return {"work_dir": str(work_dir), "root": str(tmp_path),
            "frames": frames}


def run_one_execution(workspace, execution_id, answers):
    """One Execution of both nodes, as the pipeline executor drives them.

    Returns ``{node_id: sent}`` where ``sent`` is
    ``(input_bytes, reference_bytes)`` — the exact bytes the invocation
    handed the transport, read back off the recorded call.
    """
    document = compiled_document()
    tag_values = {"part_id": PART_ID}
    output_dir = os.path.join(workspace["root"], "out", execution_id)
    os.makedirs(output_dir, exist_ok=True)
    export_context = ExportContext(
        workflow_id=WORKFLOW_ID, version=VERSION, execution_id=execution_id)

    bedrock_invoker = RecordingInvoker(answers)
    collector = NodeStatusCollector(extra_node_ids=[BEDROCK_NODE, LLM_NODE])
    bedrock_metadata = BedrockInferenceProcessor(
        invoker=bedrock_invoker).process(
            document, tag_values, workspace["work_dir"],
            run_context=RunContext(
                tag_values=tag_values, output_dir=output_dir,
                capture_id=execution_id, graph_document=None,
                node_status=collector),
            export_context=export_context)
    llm_invoker = RecordingInvoker(answers)
    llm_metadata = LlmInferenceProcessor(invoker=llm_invoker).process(
        document, tag_values, workspace["work_dir"],
        export_context=export_context)

    assert len(bedrock_invoker.calls) == 1
    assert len(llm_invoker.calls) == 1
    bedrock_images = bedrock_invoker.calls[0]["positional"][2]
    assert [label for label, _data in bedrock_images] == [
        "Input image", "Reference image"]
    llm_positional = llm_invoker.calls[0]["positional"]
    return {
        BEDROCK_NODE: {
            "sent": (bedrock_images[0][1], bedrock_images[1][1]),
            "metadata": bedrock_metadata,
            "call": bedrock_invoker.calls[0],
        },
        LLM_NODE: {
            "sent": (base64.b64decode(llm_positional[3]),
                     base64.b64decode(llm_positional[4])),
            "metadata": llm_metadata,
            "call": llm_invoker.calls[0],
        },
    }


def key_base(node_id, execution_id):
    return "{0}{1}/{2}/{3}/{4}".format(
        PREFIX, WORKFLOW_ID, node_id, THING_NAME, execution_id)


# ===========================================================================
# Leg 1: production executions export three objects per invocation
# ===========================================================================

def test_two_anomaly_mode_nodes_export_three_objects_per_invocation(
        workspace):
    """A compiled document with two anomaly-mode nodes, run through the
    processors with recording invokers and a fake S3 client, exports
    exactly three objects per invocation with byte-identical images and
    correct sidecars (Requirements 2.2, 2.3)."""
    s3 = FakeS3()
    exporter = SampleExporter(CONFIG, s3_factory=lambda: s3,
                             thing_name=THING_NAME)
    set_sample_exporter(exporter)
    try:
        first = run_one_execution(workspace, EXECUTIONS[0], [VERDICT_TRUE])
        second = run_one_execution(workspace, EXECUTIONS[1], [VERDICT_FALSE])
        assert exporter.wait_idle(15.0)
    finally:
        set_sample_exporter(None)
        exporter.stop(2.0)

    # Two executions × two nodes × three objects.
    assert len(s3.objects) == 12
    for execution_id, run, answer in ((EXECUTIONS[0], first, VERDICT_TRUE),
                                      (EXECUTIONS[1], second, VERDICT_FALSE)):
        for node_id, node_type in ((BEDROCK_NODE, "bedrock_inference"),
                                   (LLM_NODE, "llm_inference")):
            base = key_base(node_id, execution_id)
            sent_input, sent_reference = run[node_id]["sent"]
            assert s3.objects[base + ".input.jpg"] == sent_input
            assert s3.objects[base + ".reference.jpg"] == sent_reference
            assert s3.content_types[base + ".input.jpg"] == "image/jpeg"
            # The images land BEFORE the sidecar that references them.
            assert s3.puts.index(base + ".input.jpg") < s3.puts.index(
                base + ".json")
            document = json.loads(s3.objects[base + ".json"].decode("utf-8"))
            assert document["workflowId"] == WORKFLOW_ID
            assert document["version"] == int(VERSION)
            assert document["executionId"] == execution_id
            assert document["nodeId"] == node_id
            assert document["nodeType"] == node_type
            assert document["thingName"] == THING_NAME
            assert document["source"] == SOURCE_LIVE
            assert document["input"]["key"] == base + ".input.jpg"
            assert document["input"]["bytes"] == len(sent_input)
            assert document["reference"]["key"] == base + ".reference.jpg"
            assert document["recorded"]["answer"] == answer
            assert document["recorded"]["parseError"] is None
            assert document["promptFingerprint"].startswith("sha256:")
            # The exported verdict is the one the run recorded.
            section = ("bedrock" if node_type == "bedrock_inference"
                       else "llm")
            recorded = run[node_id]["metadata"][section][node_id]
            assert document["recorded"]["isAnomalous"] == recorded[
                "is_anomalous"]
            assert document["recorded"]["confidence"] == recorded[
                "confidence"]
            # No image bytes ride in the sidecar, in any encoding.
            body = s3.objects[base + ".json"]
            for data in (sent_input, sent_reference):
                assert data not in body
                assert base64.b64encode(data) not in body
    # The llm node's sidecar carries the Run_Metadata its template needs.
    llm_document = json.loads(
        s3.objects[key_base(LLM_NODE, EXECUTIONS[0]) + ".json"].decode(
            "utf-8"))
    assert llm_document["metadataSnippet"] == {"part_id": PART_ID}
    assert "metadataSnippet" not in json.loads(
        s3.objects[key_base(BEDROCK_NODE, EXECUTIONS[0]) + ".json"].decode(
            "utf-8"))


# ===========================================================================
# Leg 2: the exported samples replay through the Device_Score_Job runner
# ===========================================================================

def test_exported_samples_replay_through_the_job_runner(workspace):
    """The full loop: production executions export samples, the Portal's
    manifest names those objects, and the runner replays them against a
    fake Text_Generation_API into outcome batches and shadow reports
    (Requirements 6.9, 6.15).

    With the Candidate equal to the node's own Prompt_Set, the request the
    replay sends must equal the request production sent — the property
    the whole feature rests on ("a candidate's score predicts the
    deployed node").
    """
    s3 = FakeS3()
    exporter = SampleExporter(CONFIG, s3_factory=lambda: s3,
                             thing_name=THING_NAME)
    set_sample_exporter(exporter)
    production = {}
    try:
        for index, execution_id in enumerate(EXECUTIONS):
            answer = VERDICT_TRUE if index % 2 else VERDICT_FALSE
            production[execution_id] = run_one_execution(
                workspace, execution_id, [answer])
        assert exporter.wait_idle(20.0)
    finally:
        set_sample_exporter(None)
        exporter.stop(2.0)

    # -- the Portal's manifest, built from the exported objects ---------
    samples = []
    for index, execution_id in enumerate(EXECUTIONS):
        base = key_base(LLM_NODE, execution_id)
        sidecar = json.loads(s3.objects[base + ".json"].decode("utf-8"))
        samples.append({
            "sampleId": execution_id,
            "inputKey": sidecar["input"]["key"],
            "referenceKey": sidecar["reference"]["key"],
            "label": "NOK" if index % 2 else "OK",
            "metadataSnippet": sidecar["metadataSnippet"],
        })
    manifest = {
        "jobId": JOB_ID,
        "sessionId": SESSION_ID,
        "runId": RUN_ID,
        "workflowId": WORKFLOW_ID,
        "nodeId": LLM_NODE,
        "nodeParameters": {key: value for key, value in
                           LLM_PARAMETERS.items() if key != "anomaly_mode"},
        # The Candidate under test == the deployed node's Prompt_Set.
        "promptSet": {"prompt_template": LLM_TEMPLATE,
                      "system_prompt": SYSTEM_PROMPT,
                      "max_tokens": LLM_PARAMETERS["max_tokens"]},
        "repeats": 2,
        "samples": samples,
    }
    key = manifest_key(PREFIX, JOB_ID)
    s3.objects[key] = json.dumps(manifest).encode("utf-8")

    # -- the device executes it off the named shadow --------------------
    shadow = FakeShadow({JOB_ID: {"manifestKey": key, "cancel": False}})
    invoker = RecordingInvoker([VERDICT_TRUE, VERDICT_FALSE])
    runner = JobRunner(
        CONFIG, shadow_accessor=shadow, s3_factory=lambda: s3,
        thing_name=THING_NAME, invoker=invoker,
        session_factory=no_registrations, backoff_base_seconds=0.0,
        sleep=lambda _seconds: None)
    try:
        runner.sync()
        assert runner.wait_idle(30.0), "the job worker did not finish"
    finally:
        runner.stop(2.0)

    total = len(EXECUTIONS) * 2
    assert total > OUTCOME_BATCH_SIZE, "the run must span >1 batch"

    # -- the outcome batches -------------------------------------------
    run_prefix = outcomes_prefix(PREFIX, SESSION_ID, RUN_ID)
    batch_keys = [outcomes_key(PREFIX, SESSION_ID, RUN_ID, index)
                  for index in (1, 2)]
    assert [put for put in s3.puts if put.startswith(run_prefix)] == (
        batch_keys)
    documents = [json.loads(s3.objects[batch_key].decode("utf-8"))
                 for batch_key in batch_keys]
    assert [len(document["outcomes"]) for document in documents] == [
        OUTCOME_BATCH_SIZE, total - OUTCOME_BATCH_SIZE]
    outcomes = [outcome for document in documents
                for outcome in document["outcomes"]]
    pairs = [(outcome["sampleId"], outcome["repeat"]) for outcome in outcomes]
    assert pairs == [(execution_id, repeat) for execution_id in EXECUTIONS
                     for repeat in (1, 2)]
    for outcome in outcomes:
        assert outcome["category"] in (
            "correct", "false_pass", "false_fail")
        assert outcome["error"] is None
        assert outcome["parseError"] is None
        assert outcome["outputTokens"] == 24

    # -- the shadow reports --------------------------------------------
    entries = shadow.entries()
    assert [(entry["status"], entry["done"], entry["total"])
            for entry in entries] == [
        (STATUS_QUEUED, 0, None),
        (STATUS_RUNNING, 0, total),
        (STATUS_RUNNING, OUTCOME_BATCH_SIZE, total),
        (STATUS_RUNNING, total, total),
        (STATUS_COMPLETED, total, total),
    ]

    # -- faithfulness: the replay sends what production sent ------------
    assert len(invoker.calls) == total
    for call, (sample_id, _repeat) in zip(invoker.calls, pairs):
        positional = call["positional"]
        produced = production[sample_id][LLM_NODE]
        assert positional[0] == LLM_PARAMETERS["modelName"]
        # The rendered prompt (with the Verdict_Instruction appended) is
        # byte-for-byte the production prompt.
        assert positional[1] == produced["call"]["positional"][1]
        assert positional[1].endswith(VERDICT_INSTRUCTION)
        assert PART_ID in positional[1]
        assert positional[2]["max_tokens"] == LLM_PARAMETERS["max_tokens"]
        # ... and so are the images: the exported bytes are the sent ones.
        sent_input, sent_reference = produced["sent"]
        assert base64.b64decode(positional[3]) == sent_input
        assert base64.b64decode(positional[4]) == sent_reference
        assert call["keywords"]["system_prompt"] == SYSTEM_PROMPT

    # -- and nothing else in the bucket was touched --------------------
    assert all(put.startswith(PREFIX) or put.startswith(run_prefix)
               for put in s3.puts)
    assert set(s3.gets) <= (
        {key}
        | {sample["inputKey"] for sample in samples}
        | {sample["referenceKey"] for sample in samples})


# ===========================================================================
# The one-shot backfill, end to end through the real exporter
# ===========================================================================

NODE_FRAME_TEMPLATE = "{capture_id}.node.{node_id}.{port}.jpg"


def _artifact_tree(root, executions):
    """One registration's ``workflow.json`` plus per-execution artifacts.

    Returns ``(artifact_path, rows, frames)``; every execution has an
    ``in`` frame, a ``reference`` frame and a recorded verdict for both
    Inspection_Nodes.
    """
    artifact_path = os.path.join(root, "registration")
    os.makedirs(artifact_path, exist_ok=True)
    with open(os.path.join(artifact_path, "workflow.json"), "w") as handle:
        json.dump({"nodes": [
            {"id": BEDROCK_NODE, "type": "bedrock_inference",
             "parameters": dict(BEDROCK_PARAMETERS)},
            {"id": LLM_NODE, "type": "llm_inference",
             "parameters": dict(LLM_PARAMETERS)},
            # A freeform node: never tunable, never backfilled.
            {"id": "bedrock_2", "type": "bedrock_inference",
             "parameters": dict(BEDROCK_PARAMETERS, anomaly_mode=False)},
        ]}, handle)
    rows = []
    frames = {}
    for index, execution_id in enumerate(executions):
        output_dir = os.path.join(root, "runs", execution_id)
        os.makedirs(output_dir, exist_ok=True)
        metadata = {"part_id": PART_ID, "bedrock": {}, "llm": {}}
        for node_id, section, answer_key in (
            (BEDROCK_NODE, "bedrock", "text"),
            (LLM_NODE, "llm", "generated_text"),
            ("bedrock_2", "bedrock", "text"),
        ):
            metadata[section][node_id] = {
                answer_key: VERDICT_TRUE, "is_anomalous": True,
                "confidence": 0.91}
            for port, size, tint in (("in", (120, 90), 40 + index),
                                     ("reference", (64, 48), 200)):
                frames[(execution_id, node_id, port)] = _write_jpeg(
                    os.path.join(output_dir, NODE_FRAME_TEMPLATE.format(
                        capture_id=execution_id, node_id=node_id,
                        port=port)), size, tint)
        with open(os.path.join(output_dir, execution_id + ".json"),
                  "w") as handle:
            json.dump(metadata, handle)
        rows.append({"id": execution_id, "output_dir": output_dir,
                     "capture_id": execution_id,
                     "started_at": 2_000_000 - index})
    return artifact_path, rows, frames


def test_backfill_uploads_existing_runs_through_the_real_exporter(tmp_path):
    """The enabled transition: existing Run_Artifacts become Sample_Store
    objects marked ``source: backfill``, once (Requirement 2.7)."""
    executions = ["exec-back-0", "exec-back-1", "exec-back-2"]
    artifact_path, rows, frames = _artifact_tree(str(tmp_path), executions)
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(WorkflowRegistration(
            id="reg-1", workflow_id=WORKFLOW_ID, version=VERSION,
            arch="aarch64", artifact_path=artifact_path, status="registered",
            registered_at=1_000_000))
        for row in rows:
            session.add(WorkflowExecution(
                id=row["id"], registration_id="reg-1",
                started_at=row["started_at"], finished_at=None,
                status="success", capture_id=row["capture_id"],
                output_dir=row["output_dir"]))
        session.commit()

    s3 = FakeS3()
    exporter = SampleExporter(CONFIG, s3_factory=lambda: s3,
                             thing_name=THING_NAME)
    marker_path = str(tmp_path / "state" / "backfilled.json")
    try:
        summary = backfill_module.run_backfill(
            exporter, session_factory=factory, marker_path=marker_path,
            max_executions=2, sleep=lambda _seconds: None)
        assert exporter.wait_idle(15.0)
    finally:
        exporter.stop(2.0)

    # The newest two executions × the two TUNABLE nodes.
    assert summary.exported == 4
    assert summary.ran is True
    expected_bases = {
        key_base(node_id, execution_id)
        for execution_id in executions[:2]
        for node_id in (BEDROCK_NODE, LLM_NODE)
    }
    assert set(s3.objects) == {
        base + suffix for base in expected_bases
        for suffix in (".json", ".input.jpg", ".reference.jpg")}
    for base in expected_bases:
        document = json.loads(s3.objects[base + ".json"].decode("utf-8"))
        assert document["source"] == SOURCE_BACKFILL
        assert document["recorded"]["answer"] == VERDICT_TRUE
        assert document["recorded"]["isAnomalous"] is True
        node_id = document["nodeId"]
        execution_id = document["executionId"]
        # The exact persisted bytes of the frames the executor sent.
        assert s3.objects[base + ".input.jpg"] == frames[
            (execution_id, node_id, "in")]
        assert s3.objects[base + ".reference.jpg"] == frames[
            (execution_id, node_id, "reference")]
        # A backfilled sample is dated by its run, not by the upload.
        assert document["exportedAt"] in (2_000_000, 1_999_999)
    # The freeform node was never backfilled.
    assert not any("bedrock_2" in key for key in s3.objects)

    # A second start exports nothing: the marker is the one-shot guard.
    with open(marker_path) as handle:
        assert json.load(handle)["exported"] == 4
    again_s3 = FakeS3()
    again_exporter = SampleExporter(CONFIG, s3_factory=lambda: again_s3,
                                   thing_name=THING_NAME)
    try:
        again = backfill_module.run_backfill(
            again_exporter, session_factory=factory,
            marker_path=marker_path, sleep=lambda _seconds: None)
    finally:
        again_exporter.stop(2.0)
    assert again.already_done is True
    assert again_s3.objects == {}
