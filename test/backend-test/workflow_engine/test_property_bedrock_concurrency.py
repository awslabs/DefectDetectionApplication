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
"""Property tests for the concurrent Bedrock path
(detection-guided-bedrock-inspection task 10.3, Properties 5, 6).

- **Property 5: Branch isolation** — a recorded error in branch B gates
  only B's downstream bindings; the set of bindings run for every other
  branch is identical to the all-success case.
- **Property 6: Join completeness** — the run's terminal status is
  decided only after every Bedrock future has completed and merged;
  the returned Run_Metadata contains every branch's nested outcome
  (verdict or error).

The concurrent path activates when ``BedrockInferenceProcessor`` is
constructed with ``output_processor=<OutputBindingProcessor>``; branch
plans derive from ``branching.bedrock_branches(document)``. Concurrency
is driven by injected fake invokers with ``threading.Event``-controlled
completion order — deterministic, no sleeps-as-synchronization, no
network. The suite's registered hypothesis profile (``engine-fast`` /
``ci``, see ``conftest.py``) governs example counts.
"""
import shutil
import tempfile
import threading

import numpy as np
import cv2
from hypothesis import given, settings
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    OutputBindingProcessor,
    RunContext,
)

FRAME_NAME = "bedrock_frame_cam.jpg"
CAPTURE_ID = "cap-1"
ANSWER = '{"is_anomalous": false, "confidence": 0.93}'
GATE_TIMEOUT_SEC = 30.0

#: The two recorded-error mechanisms a branch can be configured with:
#: an out-of-range ``crop_detection_index`` (Requirement 2.4) and a
#: ``reference_payload_path`` with no trigger payload to resolve against
#: (Requirement 3.5). Both record ``bedrock.{nodeId}.error`` without
#: invoking Bedrock and never raise.
ERROR_KIND_CROP = "crop"
ERROR_KIND_REFERENCE = "reference"


# ---------------------------------------------------------------------------
# Harness (fake invoker / publisher; document builders)
# ---------------------------------------------------------------------------

class BranchInvoker:
    """Injectable Bedrock invoker keyed by the binding's ``prompt``
    parameter (each binding's prompt is set to its own node id; anomaly
    mode appends the JSON instruction after a blank line, so the first
    line is the node id).

    ``gates`` maps node id -> ``threading.Event``: the invocation for
    that node blocks until its gate is set, giving the test full,
    deterministic control over completion order — no sleeps."""

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
    """Thread-safe fake mqtt publisher recording ``(topic, payload)``
    in publish order; topics are ``{bedrock_node}/out{k}`` so publishes
    group by branch."""

    def __init__(self):
        self.log = []
        self._lock = threading.Lock()

    def __call__(self, host, port, topic, payload, qos, *args):
        with self._lock:
            self.log.append((topic, payload))

    def by_branch(self):
        grouped = {}
        with self._lock:
            entries = list(self.log)
        for topic, payload in entries:
            grouped.setdefault(topic.split("/")[0], []).append(
                (topic, payload))
        return grouped


def branch_node(index):
    return "bedrock_{0}".format(index)


def make_detection():
    return {
        "id": "3f9a2c1e", "label": "blue box", "confidence": 0.9,
        "x_min": 10.0, "y_min": 8.0, "x_max": 40.0, "y_max": 28.0,
    }


def write_frame(work_dir, width=48, height=32, name=FRAME_NAME):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    path = "{0}/{1}".format(work_dir, name)
    assert cv2.imwrite(path, frame)
    return path


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


def make_mqtt_binding(node_id, bedrock_node_id, topic, condition=None):
    """A branch output publishing its OWN node's dotted verdict keys, so
    the rendered payload depends only on the branch's own outcome (never
    on sibling completion interleaving)."""
    parameters = {
        "broker_host": "broker.local",
        "topic": topic,
        "payload_template": (
            '{{"node": "' + bedrock_node_id + '", "confidence": '
            '{bedrock.' + bedrock_node_id + '.confidence}}}'
        ),
    }
    if condition is not None:
        parameters["condition"] = condition
    return {
        "nodeId": node_id,
        "binding": "mqtt_publish",
        "parameters": parameters,
        "upstreamNodeIds": [bedrock_node_id],
        "downstreamNodeIds": [],
    }


def build_document(branch_count, output_counts, with_condition,
                   errored_kinds):
    """N Bedrock branches, each with its own mqtt outputs.

    ``errored_kinds`` maps branch index -> ERROR_KIND_*: the branch's
    Bedrock binding is configured to produce a recorded error outcome
    (out-of-range crop index / unresolvable payload reference)."""
    bindings = []
    for index in range(branch_count):
        node_id = branch_node(index)
        extra = {}
        kind = errored_kinds.get(index)
        if kind == ERROR_KIND_CROP:
            extra["crop_detection_index"] = 5
        elif kind == ERROR_KIND_REFERENCE:
            extra["reference_payload_path"] = "refs.0.image"
        bindings.append(make_bedrock_binding(node_id, extra))
        for output in range(output_counts[index]):
            condition = (
                "bedrock.{0}.confidence >= 0.5".format(node_id)
                if with_condition[index][output] else None
            )
            bindings.append(make_mqtt_binding(
                "{0}_out{1}".format(node_id, output),
                node_id,
                "{0}/out{1}".format(node_id, output),
                condition,
            ))
    return {"schemaVersion": 1, "executorBindings": bindings}


def run_concurrent(document, work_dir, invoker=None):
    """Run the document through the CONCURRENT path (output_processor
    injected) and return ``(publisher, invoker, metadata)``."""
    publisher = RecordingPublisher()
    invoker = invoker or BranchInvoker()
    processor = BedrockInferenceProcessor(
        invoker=invoker,
        output_processor=OutputBindingProcessor(mqtt_publisher=publisher),
    )
    run_context = RunContext(
        tag_values={"detections": [make_detection()]},
        output_dir=work_dir, capture_id=CAPTURE_ID)
    metadata = processor.process(
        document, {}, work_dir, run_context=run_context)
    return publisher, invoker, metadata


# ---------------------------------------------------------------------------
# Property 5: Branch isolation
# ---------------------------------------------------------------------------

@st.composite
def _isolation_scenarios(draw):
    """N branches (2-4), each with 1-2 mqtt outputs (optionally gated by
    a condition over its own branch's dotted verdict key), plus a
    non-empty set of branches configured to record an error (mixing the
    crop and payload-reference error mechanisms)."""
    branch_count = draw(st.integers(min_value=2, max_value=4))
    output_counts = [
        draw(st.integers(min_value=1, max_value=2))
        for _ in range(branch_count)
    ]
    with_condition = [
        [draw(st.booleans()) for _ in range(count)]
        for count in output_counts
    ]
    errored_indices = draw(st.sets(
        st.integers(min_value=0, max_value=branch_count - 1), min_size=1))
    errored_kinds = {
        index: draw(st.sampled_from([ERROR_KIND_CROP, ERROR_KIND_REFERENCE]))
        for index in sorted(errored_indices)
    }
    return branch_count, output_counts, with_condition, errored_kinds


@given(scenario=_isolation_scenarios())
@settings(deadline=None)
def test_property_5_error_gates_only_its_own_branch(scenario):
    """**Feature: detection-guided-bedrock-inspection, Property 5:
    Branch isolation**

    For any set of Bedrock branches (each with its own output bindings,
    optionally condition-gated) and any non-empty subset configured to
    record an error (out-of-range crop index — Requirement 2.4 — or a
    failed payload reference — Requirement 3.5), running the concurrent
    path once all-success and once with the errored subset: every
    errored branch publishes NOTHING (and never invokes Bedrock), while
    every other branch publishes exactly the same (topic, payload)
    sequence as in the all-success case.

    **Validates: Requirements 2.4, 3.5, 5.4, 5.5**
    """
    branch_count, output_counts, with_condition, errored_kinds = scenario
    work_dir = tempfile.mkdtemp(prefix="bedrock-property5-")
    try:
        write_frame(work_dir)
        success_document = build_document(
            branch_count, output_counts, with_condition, {})
        errored_document = build_document(
            branch_count, output_counts, with_condition, errored_kinds)

        success_publisher, _, success_metadata = run_concurrent(
            success_document, work_dir)
        errored_publisher, errored_invoker, errored_metadata = \
            run_concurrent(errored_document, work_dir)

        success_by_branch = success_publisher.by_branch()
        errored_by_branch = errored_publisher.by_branch()
        for index in range(branch_count):
            node_id = branch_node(index)
            if index in errored_kinds:
                # The errored branch is fully gated: no publish, no
                # Bedrock invocation, a recorded error outcome.
                assert node_id not in errored_by_branch, (
                    "BRANCH ISOLATION VIOLATION (Property 5): errored "
                    "branch {0} published {1!r}".format(
                        node_id, errored_by_branch.get(node_id)))
                assert node_id not in errored_invoker.calls
                assert "error" in errored_metadata["bedrock"][node_id]
            else:
                # Every sibling branch ran the exact binding set of the
                # all-success case, rendering the exact same payloads.
                assert errored_by_branch.get(node_id) == \
                    success_by_branch.get(node_id), (
                        "BRANCH ISOLATION VIOLATION (Property 5): sibling "
                        "branch {0} ran a different binding set under a "
                        "sibling's error".format(node_id))
                assert errored_metadata["bedrock"][node_id] == \
                    success_metadata["bedrock"][node_id]
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 6: Join completeness
# ---------------------------------------------------------------------------

@st.composite
def _join_scenarios(draw):
    """N branches (2-5, exceeding the pool bound of 4 at the top end so
    queued futures are exercised), a possibly-empty errored subset that
    leaves at least one success branch, and a completion (gate release)
    order over the success branches."""
    branch_count = draw(st.integers(min_value=2, max_value=5))
    errored_indices = sorted(draw(st.sets(
        st.integers(min_value=0, max_value=branch_count - 1),
        max_size=branch_count - 1)))
    success_indices = [
        index for index in range(branch_count)
        if index not in errored_indices
    ]
    release_order = list(draw(st.permutations(success_indices)))
    return branch_count, errored_indices, release_order


@given(scenario=_join_scenarios())
@settings(deadline=None)
def test_property_6_terminal_only_after_every_outcome_merged(scenario):
    """**Feature: detection-guided-bedrock-inspection, Property 6: Join
    completeness**

    For any branch count, errored subset, and completion order (driven
    by per-node ``threading.Event`` gates on the fake invoker):
    ``process`` has NOT returned while any success branch's gate is
    still held (the terminal status is decided only after every Bedrock
    future has completed and merged), and the metadata it returns
    carries every branch's nested outcome — a verdict for every success
    branch, an error for every errored branch — with every success
    branch's outputs published.

    **Validates: Requirements 5.6**
    """
    branch_count, errored_indices, release_order = scenario
    errored_kinds = {
        index: ERROR_KIND_CROP for index in errored_indices}
    work_dir = tempfile.mkdtemp(prefix="bedrock-property6-")
    gates = {
        branch_node(index): threading.Event() for index in release_order}
    document = build_document(
        branch_count,
        [1] * branch_count,
        [[False]] * branch_count,
        errored_kinds,
    )
    publisher = RecordingPublisher()
    invoker = BranchInvoker(gates=gates)
    processor = BedrockInferenceProcessor(
        invoker=invoker,
        output_processor=OutputBindingProcessor(mqtt_publisher=publisher),
    )
    run_context = RunContext(
        tag_values={"detections": [make_detection()]},
        output_dir=work_dir, capture_id=CAPTURE_ID)
    outcome = {}
    done = threading.Event()

    def run():
        try:
            outcome["metadata"] = processor.process(
                document, {}, work_dir, run_context=run_context)
        except BaseException as error:  # noqa: BLE001 - surfaced below
            outcome["error"] = error
        finally:
            done.set()

    thread = threading.Thread(target=run, name="bedrock-join-under-test")
    try:
        write_frame(work_dir)
        thread.start()
        # Release every success branch but the last in the drawn order.
        for index in release_order[:-1]:
            gates[branch_node(index)].set()
        # One success branch's future cannot complete (its gate is still
        # held), so the run cannot have reached its terminal state —
        # guaranteed, not timing-dependent.
        assert not done.is_set(), (
            "JOIN VIOLATION (Property 6): process returned while a "
            "Bedrock future was still incomplete")
        gates[branch_node(release_order[-1])].set()
        thread.join(timeout=GATE_TIMEOUT_SEC)
        assert done.is_set() and not thread.is_alive(), (
            "process did not return after every gate was released")
    finally:
        for gate in gates.values():
            gate.set()
        if thread.is_alive():
            thread.join(timeout=GATE_TIMEOUT_SEC)
        shutil.rmtree(work_dir, ignore_errors=True)

    assert "error" not in outcome, (
        "process raised unexpectedly: {0!r}".format(outcome.get("error")))
    metadata = outcome["metadata"]
    for index in range(branch_count):
        node_id = branch_node(index)
        entry = metadata["bedrock"][node_id]
        if index in errored_indices:
            assert "error" in entry, (
                "JOIN COMPLETENESS VIOLATION (Property 6): errored "
                "branch {0} outcome missing".format(node_id))
        else:
            assert entry["is_anomalous"] is False
            assert entry["confidence"] == 0.93
    # Every success branch's outputs ran before the join returned.
    assert set(publisher.by_branch()) == {
        branch_node(index) for index in release_order}
