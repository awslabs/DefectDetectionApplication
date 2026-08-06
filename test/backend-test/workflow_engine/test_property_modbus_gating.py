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
"""Property-based test for modbus_write conditional/filter gating
(modbus-tcp-output feature, Property 7).

Distributes modbus_write bindings across a conditional's true/false
output ports, behind an inference_filter, and ungated — exactly the
compiled-document shapes ``test_workflow_output_bindings.py`` drives —
and checks the injected writer fires for exactly the bindings whose
gates passed, with the gated-out bindings emitting the "not sent:
gated out" detail instead of a write.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.output_bindings import OutputBindingProcessor


# ---------------------------------------------------------------------------
# Harness (mirrors test_workflow_output_bindings.py)
# ---------------------------------------------------------------------------


def binding(node_id, kind, parameters=None, upstream=(), downstream=()):
    return {
        "nodeId": node_id,
        "binding": kind,
        "parameters": dict(parameters or {}),
        "upstreamNodeIds": list(upstream),
        "downstreamNodeIds": list(downstream),
    }


def conditional_binding(node_id, condition, true_downstream, false_downstream):
    """The conditional executor-binding entry as the compiler emits it: the
    "true" port gated by the condition, the "false" port by "!(condition)",
    with the downstream node ids partitioned per output port."""
    entry = binding(
        node_id, "conditional", {"condition": condition},
        downstream=list(true_downstream) + list(false_downstream),
    )
    entry["downstreamNodeIdsByPort"] = {
        "true": list(true_downstream),
        "false": list(false_downstream),
    }
    entry["portConditions"] = {
        "true": condition,
        "false": "!({0})".format(condition),
    }
    return entry


def document(*bindings):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": "aarch64-jp5",
        "segments": [],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


class Recorder:
    """Injectable modbus_writer fake recording every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)


GATED_DETAIL = (
    "not sent: gated out by an upstream inference filter or conditional"
)

#: Gate placement of each modbus_write binding: behind the conditional's
#: true port, its false port, behind an inference_filter, or ungated.
_placements = st.lists(
    st.sampled_from(("cond_true", "cond_false", "filter", "ungated")),
    min_size=1,
    max_size=6,
)


# Feature: modbus-tcp-output, Property 7: Gating decides exactly which
# writes execute
class TestProperty7GatingDecidesExactlyWhichWritesExecute:
    """# Feature: modbus-tcp-output, Property 7: Gating decides exactly
    which writes execute

    For any compiled document distributing modbus_write bindings across
    conditional output ports (and/or behind inference filters) and any
    inference metadata, the set of bindings for which the writer is
    called equals exactly the set whose gates evaluated true, and every
    gated-out binding emits the "not sent: gated out" detail instead of
    a write.

    **Validates: Requirements 6.1, 6.2, 6.3**
    """

    @settings(max_examples=100, deadline=None)
    @given(
        placements=_placements,
        is_anomalous=st.booleans(),
        filter_passes=st.booleans(),
    )
    def test_writer_fires_for_exactly_the_pass_set(
        self, placements, is_anomalous, filter_passes
    ):
        # One modbus_write binding per placement; the binding's address
        # doubles as its index so writer calls identify their binding.
        mb_ids = ["mb{0}".format(i) for i in range(len(placements))]
        upstream_by_placement = {
            "cond_true": "cond1",
            "cond_false": "cond1",
            "filter": "filt1",
            "ungated": "src1",
        }
        entries = [
            conditional_binding(
                "cond1",
                "is_anomalous == true",
                [mb_ids[i] for i, p in enumerate(placements)
                 if p == "cond_true"],
                [mb_ids[i] for i, p in enumerate(placements)
                 if p == "cond_false"],
            ),
            binding(
                "filt1", "inference_filter",
                {"condition": "confidence >= 0.5"},
                downstream=[mb_ids[i] for i, p in enumerate(placements)
                            if p == "filter"],
            ),
        ]
        for i, placement in enumerate(placements):
            entries.append(binding(
                mb_ids[i], "modbus_write",
                {"host": "plc.local", "register_type": "coil", "address": i},
                upstream=[upstream_by_placement[placement]],
            ))

        metadata = {
            "is_anomalous": is_anomalous,
            "confidence": 0.9 if filter_passes else 0.2,
        }
        gate_passes = {
            "cond_true": is_anomalous,
            "cond_false": not is_anomalous,
            "filter": filter_passes,
            "ungated": True,
        }
        expected = {
            mb_ids[i] for i, p in enumerate(placements) if gate_passes[p]
        }

        writer = Recorder()
        details = []
        processor = OutputBindingProcessor(modbus_writer=writer)
        processor.process(
            None, document(*entries), metadata,
            detail_sink=lambda node_id, detail: details.append(
                (node_id, detail)),
        )

        # The writer fired for exactly the pass set, once per binding
        # (the address field identifies the binding).
        written = {"mb{0}".format(call[4]) for call in writer.calls}
        assert written == expected
        assert len(writer.calls) == len(expected)

        # Every gated-out binding emitted the "not sent: gated out"
        # detail instead of a write; every executed binding emitted its
        # sent-message detail and no gated detail.
        gated = {nid for nid, detail in details if detail == GATED_DETAIL}
        assert gated == set(mb_ids) - expected
        sent = {nid for nid, detail in details
                if nid in mb_ids and detail.startswith("wrote ")}
        assert sent == expected
