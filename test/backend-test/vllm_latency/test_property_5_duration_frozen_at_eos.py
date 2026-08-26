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
"""Property test for duration freezing at Pipeline_EOS (task 7.4).

# Feature: vllm-workflow-latency-optimization, Property 5: Node_Execution_Time frozen at Pipeline_EOS

*For any* Pipeline_Node marked terminal by ``mark_pipeline_success`` and
*for any* subsequent sequence of terminal markings (``mark_success_all``,
``finalize``, ``mark_failure`` on other nodes), the node's recorded
duration SHALL remain the value frozen at the EOS transition — later run
activity never increases it.

**Validates: Requirements 2.2**

The test drives :class:`workflow_engine.node_status.NodeStatusCollector`
directly with a hypothesis-generated construction (``name_map`` /
``extra_node_ids``) and a random pre-EOS status history that puts pipeline
nodes into ``running``. It calls ``mark_pipeline_success``, records the
frozen serialized durations of exactly the nodes that transitioned at EOS,
sleeps a couple of milliseconds so that any later re-derivation of a
duration from the node's ``running`` start would produce a strictly larger
value, then applies an arbitrary generated sequence of subsequent terminal
markings and run activity (``mark_success_all``, ``finalize`` with and
without a failure detail, ``mark_failure`` on OTHER nodes, bus sink
signals, ``set_detail``, ``record_invocation_duration`` on OTHER nodes,
and repeated ``mark_pipeline_success``). It asserts that every EOS-frozen
node's duration — via both ``duration_ms_of`` and the serialized
``to_map()`` ``durationMs`` — is unchanged afterwards.

Invocation durations are deliberately never recorded against pipeline /
EOS-frozen nodes: ``record_invocation_duration`` takes serialization
precedence over the lifecycle duration by existing design (R1.4 of the
node-execution-timing feature), so keeping frozen nodes invocation-free
makes the public serialized value equal the EOS-frozen LIFECYCLE duration,
which is exactly what this property is about. R1.4 precedence itself is
covered by the node-execution-timing feature's own tests.
"""
import time

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.node_status import (
    NodeStatusCollector,
    STATUS_RUNNING,
)

# Small pools keep hypothesis focused on interesting collisions (shared
# node ids across elements, extra ids overlapping pipeline ids, unknown
# elements, untracked node ids).
_ELEMENTS = ["el0", "el1", "el2", "el3", "el4", "unmapped0"]
_NODE_POOL = ["n0", "n1", "n2", "n3", "n4"]
_DETAILS = ["boom", "late frame", "buffer underrun", "x"]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_NAME_MAPS = st.dictionaries(
    keys=st.sampled_from(_ELEMENTS),
    values=st.one_of(st.none(), st.sampled_from(_NODE_POOL)),
    max_size=6,
)
_EXTRA_IDS = st.lists(st.sampled_from(_NODE_POOL), max_size=3)


@st.composite
def _pre_eos_ops(draw):
    """One random pre-EOS status-history operation."""
    kind = draw(st.sampled_from(
        ["sink_running", "sink_running", "sink_warning",
         "mark_failure", "set_detail", "invocation"]))
    if kind == "sink_running":
        return ("sink", draw(st.sampled_from(_ELEMENTS)), "running", None)
    if kind == "sink_warning":
        return ("sink", draw(st.sampled_from(_ELEMENTS)), "warning",
                draw(st.one_of(st.none(), st.sampled_from(_DETAILS))))
    if kind == "mark_failure":
        return ("mark_failure",
                draw(st.one_of(st.none(), st.sampled_from(_NODE_POOL))),
                draw(st.one_of(st.none(), st.sampled_from(_DETAILS))))
    if kind == "set_detail":
        return ("set_detail", draw(st.sampled_from(_NODE_POOL)),
                draw(st.sampled_from(_DETAILS)))
    return ("invocation", draw(st.sampled_from(_NODE_POOL)),
            draw(st.integers(min_value=0, max_value=5000)))


@st.composite
def _post_eos_ops(draw):
    """One random subsequent terminal-marking / run-activity operation."""
    kind = draw(st.sampled_from(
        ["mark_success_all", "finalize", "mark_failure", "sink_running",
         "sink_warning", "set_detail", "invocation",
         "mark_pipeline_success"]))
    if kind == "mark_success_all":
        return ("mark_success_all",)
    if kind == "finalize":
        return ("finalize",
                draw(st.one_of(st.none(), st.sampled_from(_DETAILS))))
    if kind == "mark_failure":
        return ("mark_failure",
                draw(st.one_of(st.none(), st.sampled_from(_NODE_POOL))),
                draw(st.one_of(st.none(), st.sampled_from(_DETAILS))))
    if kind == "sink_running":
        return ("sink", draw(st.sampled_from(_ELEMENTS)), "running", None)
    if kind == "sink_warning":
        return ("sink", draw(st.sampled_from(_ELEMENTS)), "warning",
                draw(st.one_of(st.none(), st.sampled_from(_DETAILS))))
    if kind == "set_detail":
        return ("set_detail", draw(st.sampled_from(_NODE_POOL)),
                draw(st.sampled_from(_DETAILS)))
    if kind == "invocation":
        return ("invocation", draw(st.sampled_from(_NODE_POOL)),
                draw(st.integers(min_value=0, max_value=5000)))
    return ("mark_pipeline_success",)


@st.composite
def _cases(draw):
    name_map = draw(_NAME_MAPS)
    extra_ids = draw(_EXTRA_IDS)
    # Bias toward starting the run (mark_running_all) so pipeline nodes
    # are usually running at EOS and the property is non-vacuous.
    run_all = draw(st.sampled_from([True, True, True, False]))
    pre_ops = draw(st.lists(_pre_eos_ops(), max_size=8))
    post_ops = draw(st.lists(_post_eos_ops(), min_size=1, max_size=10))
    return {
        "name_map": name_map,
        "extra_ids": extra_ids,
        "run_all": run_all,
        "pre_ops": pre_ops,
        "post_ops": post_ops,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_op(collector, op, skip_nodes):
    """Apply one history operation; node-targeted duration-relevant ops
    (``mark_failure``, ``record_invocation_duration``) are skipped when they
    target a node in ``skip_nodes`` (pipeline nodes pre-EOS, EOS-frozen
    nodes post-EOS) — the property is about markings on OTHER nodes, and
    invocation durations would mask the lifecycle value (R1.4)."""
    if op[0] == "sink":
        collector.sink(op[1], op[2], op[3])
    elif op[0] == "mark_failure":
        if op[1] not in skip_nodes:
            collector.mark_failure(op[1], op[2])
    elif op[0] == "set_detail":
        collector.set_detail(op[1], op[2])
    elif op[0] == "invocation":
        if op[1] not in skip_nodes:
            collector.record_invocation_duration(op[1], op[2])
    elif op[0] == "mark_success_all":
        collector.mark_success_all()
    elif op[0] == "finalize":
        collector.finalize(failure_detail=op[1])
    elif op[0] == "mark_pipeline_success":
        collector.mark_pipeline_success()


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

# Feature: vllm-workflow-latency-optimization, Property 5: Node_Execution_Time frozen at Pipeline_EOS
@settings(max_examples=100, deadline=None)
@given(case=_cases())
def test_property_5_duration_frozen_at_eos(case):
    """**Feature: vllm-workflow-latency-optimization, Property 5:
    Node_Execution_Time frozen at Pipeline_EOS**

    **Validates: Requirements 2.2**
    """
    pipeline_ids = {nid for nid in case["name_map"].values()
                    if nid is not None}

    collector = NodeStatusCollector(
        name_map=dict(case["name_map"]),
        extra_node_ids=list(case["extra_ids"]),
    )

    # ---- pre-EOS history: put pipeline nodes into running ----------------
    if case["run_all"]:
        collector.mark_running_all()
    for op in case["pre_ops"]:
        _apply_op(collector, op, skip_nodes=pipeline_ids)

    # The nodes mark_pipeline_success will mark terminal at EOS.
    eos_candidates = {
        nid for nid in pipeline_ids
        if collector.status_of(nid) == STATUS_RUNNING
    }

    # ---- Pipeline_EOS: freeze durations (R2.2) ----------------------------
    collector.mark_pipeline_success()

    frozen = {}
    for nid in eos_candidates:
        duration = collector.duration_ms_of(nid)
        # The EOS transition records a duration for every node it marks
        # terminal (the node was running, so a start timestamp exists).
        assert isinstance(duration, int)
        assert not isinstance(duration, bool)
        assert duration >= 0
        frozen[nid] = duration
    serialized_at_eos = {
        nid: collector.to_map()[nid].get("durationMs") for nid in frozen
    }
    assert serialized_at_eos == frozen

    # Let real time advance so that any later re-derivation of a duration
    # from the node's running-start timestamp would be strictly larger
    # than the frozen value — making an overwrite observable.
    time.sleep(0.002)

    # ---- arbitrary subsequent terminal markings / run activity -----------
    for op in case["post_ops"]:
        _apply_op(collector, op, skip_nodes=set(frozen))

    # ---- R2.2: the frozen durations never change --------------------------
    final_map = collector.to_map()
    for nid, frozen_duration in frozen.items():
        assert collector.duration_ms_of(nid) == frozen_duration
        assert final_map[nid].get("durationMs") == frozen_duration
