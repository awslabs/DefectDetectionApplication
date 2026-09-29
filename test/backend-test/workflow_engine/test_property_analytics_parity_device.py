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
"""The device half of the analytics parity test.

**Feature: rtsp-rtmp-stream-cameras, Property 27: Analytics parity
between the device and the sandbox**

*For any* Detection_List, analytics parameters, frame size, and outcome
sequence, the LocalServer bindings and the sandbox bindings SHALL produce
identical ``counter``, ``association``, and ``event`` metadata.

**Validates: Requirements 13.9, 14.6, 15.6**

The corpus is the one the sandbox half replays
(``edge-cv-portal/test-sandbox/tests/fixtures/analytics_parity_cases.json``,
generated from the real compiler with an oracle, task 12.2): 51 cases of
chained counters, associations and gates over 176 runs, covering every
emit mode, repeat intervals, warnings (no Detection_List), errors (a zone
without a frame size, malformed parameters), and unevaluable conditions.
Each case runs through the real device path, carrying gate state from run
to run exactly as a registration does:

- ``scene_analytics.apply_scene_analytics`` (what ``execute()`` calls
  after the Detection_List merge), then
- ``OutputBindingProcessor.process_subset`` (what the post-run handler
  runs), which steps the event gates and gates the outputs.

A probe output downstream of every analytics node shows the gating: a
gate's probe is sent exactly when the gate passes, and a counter's or an
association's exactly when its outcome is not ``error``.
"""
import copy
import json
import os

import pytest

from workflow_engine.output_bindings import OutputBindingProcessor
from workflow_engine.scene_analytics import (
    EVENT_GATE_BINDING,
    EventGateStateStore,
    apply_scene_analytics,
)

FIXTURES = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "edge-cv-portal", "test-sandbox", "tests", "fixtures",
    "analytics_parity_cases.json"))
SECTIONS = ("counter", "association", "event")


def _cases():
    if not os.path.isfile(FIXTURES):
        return []
    with open(FIXTURES, "r", encoding="utf-8") as handle:
        return json.load(handle)["cases"]


CASES = _cases()


def _document(case):
    bindings = copy.deepcopy(case["bindings"])
    probes = [{"nodeId": "probe_{0}".format(binding["nodeId"]), "binding": "mqtt_publish",
               "parameters": {"topic": "probe/{0}".format(binding["nodeId"]), "greengrass": True},
               "upstreamNodeIds": [binding["nodeId"]], "downstreamNodeIds": []}
              for binding in case["bindings"]]
    return {"workflowId": "wf-parity", "workflowVersion": "1", "executorBindings": bindings + probes}


def test_the_shared_corpus_is_present():
    if not CASES:
        pytest.skip("the Portal test sandbox fixtures are not in this checkout")
    assert len(CASES) >= 50
    assert sum(len(case["runs"]) for case in CASES) >= 170


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_device_reproduces_the_sandbox_metadata(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 27: Analytics parity
    between the device and the sandbox**

    **Validates: Requirements 13.9, 14.6, 15.6**
    """
    document = _document(case)
    gates = [binding["nodeId"] for binding in case["bindings"] if binding["binding"] == EVENT_GATE_BINDING]
    store = EventGateStateStore()
    for index, run in enumerate(case["runs"]):
        tag_values = dict(run["tags"])
        if run["detections"] is not None:
            tag_values["detections"] = copy.deepcopy(run["detections"])
        # The executor works on a private copy of the document each run.
        run_document = copy.deepcopy(document)

        outcomes = apply_scene_analytics(run_document, tag_values, frame_size=case["frameSize"])

        sent = []
        processor = OutputBindingProcessor(
            greengrass_publisher=lambda topic, payload, qos, **kwargs: sent.append(topic),
            event_gate_store=store, clock_ms=lambda: run["nowMs"])
        processor.process_subset(run_document, tag_values,
                                 [binding["nodeId"] for binding in run_document["executorBindings"]])

        produced = {key: tag_values[key] for key in SECTIONS if key in tag_values}
        assert produced == run["expected"], "run {0}".format(index)
        assert outcomes == run["outcomes"], "run {0}".format(index)
        assert {gate: "probe/{0}".format(gate) in sent for gate in gates} == run["passed"], "run {0}".format(index)
        for node_id, outcome in outcomes.items():
            assert ("probe/{0}".format(node_id) in sent) == (outcome != "error"), "run {0}".format(index)
