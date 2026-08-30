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
"""Property test for the ``OutputBindingProcessor.process_subset`` refactor.

**Feature: detection-guided-bedrock-inspection, Property 8: Non-branch
ordering**

*Bindings not in any Bedrock branch execute in the same order and with the
same effective metadata as today, regardless of branch concurrency.*

**Validates: Requirements 5.7**

``process()`` now delegates to ``process_subset`` with the full binding
list; this test pins that delegation to the pre-refactor semantics. The
pre-refactor oracle is implemented **by construction** (per the tasks.md
notes): the document generator controls every gate outcome — each
inference filter / conditional / binding condition is drawn from a pool
of expressions whose truth value over the generated metadata is known at
generation time — so the exact runner invocation sequence the
pre-refactor ``process`` body would have produced (``executorBindings``
emission order, minus gated/skipped bindings) is computed alongside the
document, with no new parameters involved anywhere. Invoking ``process()``
(the unchanged pre-refactor call shape) must reproduce that sequence
exactly, through fake runners recording calls in a single shared log so
cross-runner ordering is observable.

Runs with the hypothesis profiles registered in this directory's conftest
(``engine-fast`` locally, ``HYPOTHESIS_PROFILE=ci`` for a larger run).
"""
import itertools

from hypothesis import given, settings
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.output_bindings import OutputBindingProcessor

# --- condition pool ----------------------------------------------------------

#: (expression, predicate(metadata) -> True | False | None). ``None`` means
#: the expression cannot be evaluated over the run metadata (unknown field):
#: the processor gates like False and never actuates on an unevaluable rule.
_CONDITIONS = [
    ("true", lambda m: True),
    ("false", lambda m: False),
    ("is_anomalous", lambda m: bool(m["is_anomalous"])),
    ("confidence >= 0.5", lambda m: m["confidence"] >= 0.5),
    (
        "is_anomalous == true && confidence >= 0.5",
        lambda m: bool(m["is_anomalous"]) and m["confidence"] >= 0.5,
    ),
    ("bogus_field == 1", lambda m: None),
]

_CONDITION_INDICES = st.integers(min_value=0, max_value=len(_CONDITIONS) - 1)

#: Runner-backed output binding kinds (each drives a distinct fake client).
_OUTPUT_KINDS = st.sampled_from(
    ["mqtt_publish", "opcua_write", "digital_output"]
)

#: Kinds the pre-refactor loop skips without invoking any runner.
_SKIP_KINDS = st.sampled_from(
    ["digital_input", "bedrock_inference", "llm_inference",
     "metadata", "something_unknown"]
)

# --- document generator (oracle by construction) ------------------------------


def _output_binding(kind, node_id, token, condition=None, upstream=()):
    """One runner-backed output binding whose fake-client call carries
    ``token``, so the recorded invocation identifies the binding."""
    if kind == "mqtt_publish":
        parameters = {"broker_host": "b", "topic": str(token)}
    elif kind == "opcua_write":
        parameters = {
            "endpoint": "opc.tcp://plc.local:4840",
            "node_id": str(token),
        }
    else:  # digital_output
        parameters = {
            "pin": token, "signal_type": "high", "pulse_width_ms": 10,
        }
    if condition is not None:
        parameters["condition"] = condition
    return {
        "nodeId": node_id,
        "binding": kind,
        "parameters": parameters,
        "upstreamNodeIds": list(upstream),
        "downstreamNodeIds": [],
    }


def _conditional_binding(node_id, condition, true_ids, false_ids):
    """The conditional entry exactly as the compiler emits it: the "true"
    port gated by the condition, the "false" port by its negation."""
    return {
        "nodeId": node_id,
        "binding": "conditional",
        "parameters": {"condition": condition},
        "upstreamNodeIds": [],
        "downstreamNodeIds": list(true_ids) + list(false_ids),
        "downstreamNodeIdsByPort": {
            "true": list(true_ids), "false": list(false_ids),
        },
        "portConditions": {
            "true": condition, "false": "!({0})".format(condition),
        },
    }


@st.composite
def _documents_with_expected_sequences(draw):
    """A compiled document plus the runner invocation sequence the
    pre-refactor ``process`` body produces over it, computed at
    generation time from the drawn gate outcomes (the oracle)."""
    metadata = {
        "is_anomalous": draw(st.booleans()),
        "confidence": draw(st.sampled_from([0.0, 0.3, 0.5, 0.7, 1.0])),
    }
    bindings = []
    expected = []
    node_ids = itertools.count(1)
    tokens = itertools.count(100)

    def next_id():
        return "n{0}".format(next(node_ids))

    for _ in range(draw(st.integers(min_value=0, max_value=8))):
        shape = draw(st.sampled_from(
            ["plain", "conditioned", "filtered", "conditional", "skip"]))
        if shape == "plain":
            kind = draw(_OUTPUT_KINDS)
            token = next(tokens)
            bindings.append(_output_binding(kind, next_id(), token))
            expected.append((kind, token))
        elif shape == "conditioned":
            kind = draw(_OUTPUT_KINDS)
            token = next(tokens)
            expression, predicate = _CONDITIONS[draw(_CONDITION_INDICES)]
            bindings.append(_output_binding(
                kind, next_id(), token, condition=expression))
            if predicate(metadata) is True:
                expected.append((kind, token))
        elif shape == "filtered":
            filter_id = next_id()
            output_id = next_id()
            kind = draw(_OUTPUT_KINDS)
            token = next(tokens)
            expression, predicate = _CONDITIONS[draw(_CONDITION_INDICES)]
            bindings.append({
                "nodeId": filter_id,
                "binding": "inference_filter",
                "parameters": {"condition": expression},
                "upstreamNodeIds": [],
                "downstreamNodeIds": [output_id],
            })
            bindings.append(_output_binding(
                kind, output_id, token, upstream=[filter_id]))
            # A filter gates unless it evaluated True (unevaluable -> gate).
            if predicate(metadata) is True:
                expected.append((kind, token))
        elif shape == "conditional":
            conditional_id = next_id()
            true_id, false_id = next_id(), next_id()
            true_kind, false_kind = draw(_OUTPUT_KINDS), draw(_OUTPUT_KINDS)
            true_token, false_token = next(tokens), next(tokens)
            expression, predicate = _CONDITIONS[draw(_CONDITION_INDICES)]
            bindings.append(_conditional_binding(
                conditional_id, expression, [true_id], [false_id]))
            bindings.append(_output_binding(
                true_kind, true_id, true_token, upstream=[conditional_id]))
            bindings.append(_output_binding(
                false_kind, false_id, false_token,
                upstream=[conditional_id]))
            outcome = predicate(metadata)
            # An unevaluable condition gates BOTH ports (its negation is
            # equally unevaluable); otherwise exactly one port routes.
            if outcome is True:
                expected.append((true_kind, true_token))
            elif outcome is False:
                expected.append((false_kind, false_token))
        else:  # skip
            bindings.append({
                "nodeId": next_id(),
                "binding": draw(_SKIP_KINDS),
                "parameters": {},
                "upstreamNodeIds": [],
                "downstreamNodeIds": [],
            })

    document = {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": "aarch64-jp5",
        "segments": [],
        "executorBindings": bindings,
        "pluginDependencies": [],
    }
    return document, metadata, expected


# --- recording fakes (one shared log for cross-runner ordering) ---------------


class _RecordingClients:
    """Injectable fake clients appending to ONE ordered log, so the
    relative order of mqtt/opcua/dio invocations is observable."""

    def __init__(self):
        self.log = []

    def mqtt(self, host, port, topic, payload, qos, *args):
        self.log.append(("mqtt_publish", int(topic)))

    def opcua(self, endpoint, node_id, value, *args):
        self.log.append(("opcua_write", int(node_id)))

    def dio(self, pin, signal_type, pulse_width_ms):
        self.log.append(("digital_output", int(pin)))


# --- property ------------------------------------------------------------------


# Feature: detection-guided-bedrock-inspection, Property 8: Non-branch ordering
@settings(deadline=None)
@given(_documents_with_expected_sequences())
def test_process_matches_pre_refactor_invocation_sequence(case):
    """**Feature: detection-guided-bedrock-inspection, Property 8:
    Non-branch ordering**

    ``process()`` post-refactor (delegating to ``process_subset`` with the
    full binding list) produces exactly the runner invocation sequence the
    pre-refactor semantics dictate: ``executorBindings`` emission order,
    minus gated/condition-skipped/skipped-kind bindings — with no new
    parameters involved anywhere in the call shape.

    **Validates: Requirements 5.7**
    """
    document, metadata, expected = case
    clients = _RecordingClients()
    processor = OutputBindingProcessor(
        dio_actuator=clients.dio,
        mqtt_publisher=clients.mqtt,
        opcua_writer=clients.opcua,
    )

    processor(None, document, metadata)

    assert clients.log == expected
