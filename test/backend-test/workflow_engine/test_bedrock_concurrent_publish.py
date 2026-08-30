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
"""Example tests for the concurrent Bedrock path
(detection-guided-bedrock-inspection task 10.3, Requirements 5.1, 5.3,
5.4, 5.5, 5.6, 5.8).

The concurrent path activates when ``BedrockInferenceProcessor`` is
constructed with ``output_processor=<OutputBindingProcessor>``; each
branch's downstream output bindings (per
``branching.bedrock_branches``) run in that branch's completion path
through ``OutputBindingProcessor.process_subset``. Completion order is
driven by injected fake invokers with ``threading.Event`` gates —
deterministic, no sleeps-as-synchronization, no network.
"""
import json
import os
import threading

import numpy as np
import cv2

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.node_status import STATUS_FAILURE, NodeStatusCollector
from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    OutputBindingProcessor,
    RunContext,
)

FRAME_NAME = "bedrock_frame_cam.jpg"
CAPTURE_ID = "cap-1"
ANSWER = '{"is_anomalous": false, "confidence": 0.93}'
GATE_TIMEOUT_SEC = 30.0


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class BranchInvoker:
    """Fake Bedrock invoker keyed by the binding's ``prompt`` parameter
    (set to the node id; anomaly mode appends the JSON instruction after
    a blank line, so the first line is the node id). ``gates`` maps a
    node id to a ``threading.Event`` the invocation blocks on."""

    def __init__(self, answers=None, gates=None):
        self.answers = dict(answers or {})
        self.gates = dict(gates or {})
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, model, prompt, images, region, max_tokens):
        node_id = prompt.splitlines()[0]
        with self._lock:
            self.calls.append(node_id)
        gate = self.gates.get(node_id)
        if gate is not None:
            assert gate.wait(timeout=GATE_TIMEOUT_SEC), (
                "invoker gate for node {0} was never released".format(
                    node_id))
        return self.answers.get(node_id, ANSWER)


class RecordingPublisher:
    """Thread-safe fake mqtt publisher; optionally signals an Event per
    topic so the test can synchronize on a publish having happened."""

    def __init__(self, signals=None):
        self.log = []
        self.signals = dict(signals or {})
        self._lock = threading.Lock()

    def __call__(self, host, port, topic, payload, qos, *args):
        with self._lock:
            self.log.append((topic, payload))
        signal = self.signals.get(topic)
        if signal is not None:
            signal.set()

    def topics(self):
        with self._lock:
            return [topic for topic, _ in self.log]

    def payloads_by_topic(self):
        with self._lock:
            return dict(self.log)


def write_frame(work_dir, width=48, height=32, name=FRAME_NAME):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    path = os.path.join(work_dir, name)
    assert cv2.imwrite(path, frame)
    return path


def make_detection():
    return {
        "id": "3f9a2c1e", "label": "blue box", "confidence": 0.9,
        "x_min": 10.0, "y_min": 8.0, "x_max": 40.0, "y_max": 28.0,
    }


def make_bedrock_binding(node_id, extra_parameters=None):
    parameters = {"prompt": node_id}
    parameters.update(extra_parameters or {})
    return {
        "nodeId": node_id,
        "binding": "bedrock_inference",
        "parameters": parameters,
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": [],
        "capturePaths": {"in": "{work_dir}/" + FRAME_NAME,
                         "reference": None},
    }


def make_mqtt_binding(node_id, bedrock_node_id, topic,
                      payload_template=None):
    return {
        "nodeId": node_id,
        "binding": "mqtt_publish",
        "parameters": {
            "broker_host": "broker.local",
            "topic": topic,
            "payload_template": payload_template or (
                '{{"node": "' + bedrock_node_id + '", "confidence": '
                '{bedrock.' + bedrock_node_id + '.confidence}}}'
            ),
        },
        "upstreamNodeIds": [bedrock_node_id],
        "downstreamNodeIds": [],
    }


def make_document(bindings):
    return {"schemaVersion": 1, "executorBindings": list(bindings)}


def make_processor(invoker, publisher):
    return BedrockInferenceProcessor(
        invoker=invoker,
        output_processor=OutputBindingProcessor(mqtt_publisher=publisher),
    )


def run_in_thread(processor, document, tag_values, work_dir, run_context):
    """Run ``process`` on a worker thread; returns ``(thread, done,
    outcome)`` where ``outcome`` collects the metadata or the raise."""
    outcome = {}
    done = threading.Event()

    def run():
        try:
            outcome["metadata"] = processor.process(
                document, tag_values, work_dir, run_context=run_context)
        except BaseException as error:  # noqa: BLE001 - surfaced by tests
            outcome["error"] = error
        finally:
            done.set()

    thread = threading.Thread(target=run, name="bedrock-under-test")
    thread.start()
    return thread, done, outcome


# ---------------------------------------------------------------------------
# Publish-on-completion (Requirement 5.3)
# ---------------------------------------------------------------------------

class TestPublishOnCompletion:
    def test_first_completed_branch_publishes_while_sibling_still_runs(
            self, tmp_path):
        """Two branches: branch A completes immediately, branch B's
        invoker is held on a gate. A's message is published while B's
        invocation is provably still in flight — the first result never
        waits on the last inference (Requirement 5.3)."""
        write_frame(str(tmp_path))
        gate_b = threading.Event()
        published_a = threading.Event()
        publisher = RecordingPublisher(signals={"a/out": published_a})
        invoker = BranchInvoker(gates={"bedrock_b": gate_b})
        processor = make_processor(invoker, publisher)
        document = make_document([
            make_bedrock_binding("bedrock_a"),
            make_mqtt_binding("mqtt_a", "bedrock_a", "a/out"),
            make_bedrock_binding("bedrock_b"),
            make_mqtt_binding("mqtt_b", "bedrock_b", "b/out"),
        ])

        thread, done, outcome = run_in_thread(
            processor, document, {}, str(tmp_path), None)
        try:
            # Branch A publishes while B's gate is still held: B's
            # future cannot have completed, so this IS
            # publish-on-completion, not publish-after-join.
            assert published_a.wait(timeout=GATE_TIMEOUT_SEC), (
                "branch A never published while branch B was in flight")
            assert publisher.topics() == ["a/out"]
            assert not done.is_set(), (
                "process returned while branch B was still in flight")
        finally:
            gate_b.set()
            thread.join(timeout=GATE_TIMEOUT_SEC)
        assert not thread.is_alive()
        assert "error" not in outcome
        # Both branches published exactly once, A (first completed) first.
        assert publisher.topics() == ["a/out", "b/out"]


# ---------------------------------------------------------------------------
# Errored branch never publishes; siblings always do (Requirements 5.4, 5.5)
# ---------------------------------------------------------------------------

class TestErroredBranchIsolation:
    def test_errored_branch_never_publishes_siblings_always_do(
            self, tmp_path):
        """Three branches; the middle one records an error (out-of-range
        ``crop_detection_index``). Its mqtt_publish never fires and its
        node status is failed; both siblings' messages publish
        (Requirements 5.4, 5.5) and every outcome is recorded."""
        write_frame(str(tmp_path))
        publisher = RecordingPublisher()
        invoker = BranchInvoker()
        processor = make_processor(invoker, publisher)
        document = make_document([
            make_bedrock_binding("bedrock_1"),
            make_mqtt_binding("mqtt_1", "bedrock_1", "bedrock_1/out"),
            make_bedrock_binding(
                "bedrock_2", {"crop_detection_index": 5}),
            make_mqtt_binding("mqtt_2", "bedrock_2", "bedrock_2/out"),
            make_bedrock_binding("bedrock_3"),
            make_mqtt_binding("mqtt_3", "bedrock_3", "bedrock_3/out"),
        ])
        collector = NodeStatusCollector(extra_node_ids=[
            "bedrock_1", "bedrock_2", "bedrock_3",
            "mqtt_1", "mqtt_2", "mqtt_3",
        ])
        run_context = RunContext(
            tag_values={"detections": [make_detection()]},
            output_dir=str(tmp_path), capture_id=CAPTURE_ID,
            node_status=collector)

        metadata = processor.process(
            document, {}, str(tmp_path), run_context=run_context)

        # The errored branch: no publish, no Bedrock invocation, a
        # failed node status, and the recorded error outcome.
        assert sorted(publisher.topics()) == \
            ["bedrock_1/out", "bedrock_3/out"]
        assert "bedrock_2" not in invoker.calls
        assert collector.status_of("bedrock_2") == STATUS_FAILURE
        assert "error" in metadata["bedrock"]["bedrock_2"]
        # The siblings: verdicts recorded, messages published.
        for node_id in ("bedrock_1", "bedrock_3"):
            assert metadata["bedrock"][node_id]["is_anomalous"] is False
            assert metadata["bedrock"][node_id]["confidence"] == 0.93


# ---------------------------------------------------------------------------
# Every outcome present in the returned metadata (Requirement 5.6)
# ---------------------------------------------------------------------------

class TestJoinOutcomeCompleteness:
    def test_every_branch_outcome_present_after_the_join(self, tmp_path):
        """A mixed run (two verdicts, two recorded errors — one crop,
        one payload-reference): the metadata ``process`` returns carries
        all four branches' nested outcomes (Requirement 5.6)."""
        write_frame(str(tmp_path))
        publisher = RecordingPublisher()
        processor = make_processor(BranchInvoker(), publisher)
        document = make_document([
            make_bedrock_binding("bedrock_1"),
            make_mqtt_binding("mqtt_1", "bedrock_1", "bedrock_1/out"),
            make_bedrock_binding(
                "bedrock_2", {"crop_detection_index": 5}),
            make_mqtt_binding("mqtt_2", "bedrock_2", "bedrock_2/out"),
            make_bedrock_binding("bedrock_3"),
            make_mqtt_binding("mqtt_3", "bedrock_3", "bedrock_3/out"),
            make_bedrock_binding(
                "bedrock_4", {"reference_payload_path": "refs.0.image"}),
            make_mqtt_binding("mqtt_4", "bedrock_4", "bedrock_4/out"),
        ])
        run_context = RunContext(
            tag_values={"detections": [make_detection()]},
            output_dir=str(tmp_path), capture_id=CAPTURE_ID)

        metadata = processor.process(
            document, {}, str(tmp_path), run_context=run_context)

        assert sorted(metadata["bedrock"]) == \
            ["bedrock_1", "bedrock_2", "bedrock_3", "bedrock_4"]
        for node_id in ("bedrock_1", "bedrock_3"):
            entry = metadata["bedrock"][node_id]
            assert entry["is_anomalous"] is False
            assert entry["confidence"] == 0.93
        for node_id in ("bedrock_2", "bedrock_4"):
            assert "error" in metadata["bedrock"][node_id]


# ---------------------------------------------------------------------------
# N branches -> N independent rendered payloads (Requirement 5.1)
# ---------------------------------------------------------------------------

class TestIndependentBranchPayloads:
    def test_each_branch_renders_its_own_template_from_its_own_verdict(
            self, tmp_path):
        """Three branches with three distinct verdicts: each branch's
        mqtt_publish renders ITS node's template over ITS node's dotted
        verdict keys — three independent messages, no cross-branch
        bleed-through (Requirement 5.1)."""
        write_frame(str(tmp_path))
        answers = {
            "bedrock_1": '{"is_anomalous": false, "confidence": 0.91}',
            "bedrock_2": '{"is_anomalous": true, "confidence": 0.92}',
            "bedrock_3": '{"is_anomalous": false, "confidence": 0.93}',
        }
        publisher = RecordingPublisher()
        processor = make_processor(
            BranchInvoker(answers=answers), publisher)
        document = make_document([
            binding
            for node_id in ("bedrock_1", "bedrock_2", "bedrock_3")
            for binding in (
                make_bedrock_binding(node_id),
                make_mqtt_binding(
                    "mqtt_" + node_id, node_id, node_id + "/out"),
            )
        ])

        processor.process(document, {}, str(tmp_path), run_context=None)

        payloads = publisher.payloads_by_topic()
        assert sorted(payloads) == \
            ["bedrock_1/out", "bedrock_2/out", "bedrock_3/out"]
        for node_id, confidence in (
            ("bedrock_1", 0.91), ("bedrock_2", 0.92), ("bedrock_3", 0.93),
        ):
            assert json.loads(payloads[node_id + "/out"]) == {
                "node": node_id, "confidence": confidence,
            }


# ---------------------------------------------------------------------------
# Metadata-node attachments reach branch payloads (Requirement 5.8)
# ---------------------------------------------------------------------------

class TestMetadataAttachmentInBranches:
    def test_metadata_node_attachments_reach_each_branch_payload(
            self, tmp_path):
        """A Metadata_Node maps each reference entry's id from the
        trigger payload onto its branch's mqtt_publish; the attached
        entries land in the branch payloads published from the
        concurrent completion path (Requirement 5.8)."""
        write_frame(str(tmp_path))
        publisher = RecordingPublisher()
        processor = make_processor(BranchInvoker(), publisher)
        document = make_document([
            make_bedrock_binding("bedrock_1"),
            make_mqtt_binding("mqtt_1", "bedrock_1", "bedrock_1/out"),
            make_bedrock_binding("bedrock_2"),
            make_mqtt_binding("mqtt_2", "bedrock_2", "bedrock_2/out"),
            {
                "nodeId": "meta_1",
                "binding": "metadata",
                "parameters": {},
                "metadataMappings": [
                    {"fieldPath": "refs.0.id", "key": "ref_id"},
                ],
                "staticJson": {"line": "cell-7"},
                "attachTo": ["mqtt_1"],
                "upstreamNodeIds": ["trigger_1"],
                "downstreamNodeIds": ["mqtt_1"],
            },
            {
                "nodeId": "meta_2",
                "binding": "metadata",
                "parameters": {},
                "metadataMappings": [
                    {"fieldPath": "refs.1.id", "key": "ref_id"},
                ],
                "staticJson": {"line": "cell-7"},
                "attachTo": ["mqtt_2"],
                "upstreamNodeIds": ["trigger_1"],
                "downstreamNodeIds": ["mqtt_2"],
            },
        ])
        tag_values = {
            "trigger": {"payload_json": {"refs": [
                {"id": "plate-A", "image": "s3://bucket/refA.jpg"},
                {"id": "plate-B", "image": "s3://bucket/refB.jpg"},
            ]}},
        }

        processor.process(
            document, tag_values, str(tmp_path), run_context=None)

        payloads = publisher.payloads_by_topic()
        assert json.loads(payloads["bedrock_1/out"]) == {
            "node": "bedrock_1", "confidence": 0.93,
            "ref_id": "plate-A", "line": "cell-7",
        }
        assert json.loads(payloads["bedrock_2/out"]) == {
            "node": "bedrock_2", "confidence": 0.93,
            "ref_id": "plate-B", "line": "cell-7",
        }
