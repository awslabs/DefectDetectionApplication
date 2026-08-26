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
"""Property test for Pipeline_EOS terminal marking (task 7.3).

# Feature: vllm-workflow-latency-optimization, Property 4: Pipeline_EOS terminal marking is scoped and parity-preserving

*For any* collector state (random element name_map, extra binding node ids,
and status history), ``mark_pipeline_success`` SHALL transition exactly the
name_map-derived Pipeline_Nodes currently in ``running`` to ``success``,
SHALL retain ``warning`` (with detail) and ``failure``, SHALL leave
``pending`` Pipeline_Nodes and all binding nodes untouched, SHALL keep the
serialized map within the existing ``{status, detail?, durationMs?}`` shape
and five-status vocabulary; and *for any* full run lifecycle (success or
failure path), the final terminal status of every node SHALL equal the final
terminal status the pre-feature lifecycle produces, with all serialized
durations non-negative integers.

**Validates: Requirements 2.1, 2.3, 2.4, 2.5, 2.7**

The test drives :class:`workflow_engine.node_status.NodeStatusCollector`
directly (no GStreamer dependency) with a hypothesis-generated construction
(``name_map`` / ``extra_node_ids``) and a random status history (bus-sink
running/warning signals, explicit failures, details, invocation durations,
including unknown elements and untracked node ids). It then checks:

* **scoped marking** on a probe collector: ``mark_pipeline_success``
  transitions exactly the running Pipeline_Nodes to ``success`` and leaves
  everything else (statuses, details, recorded durations) byte-identical,
  with the serialized map staying inside the existing shape/vocabulary;
* **final-map parity** between a "featured" collector (with
  ``mark_pipeline_success`` at the Pipeline_EOS point, mirroring the
  executor's clean-return call site) and a pre-feature "reference"
  collector (without it), under each full-lifecycle terminal sequence the
  executor performs: clean success (``mark_success_all`` + ``finalize``),
  post-EOS attributed failure (``mark_failure`` + ``finalize``), and the
  no-EOS pipeline error/timeout path (where the call site is never
  reached), asserting identical final statuses and details and
  non-negative-integer serialized durations;
* the existing R2.3 finalize rules on the no-EOS path: ``failure``
  attributed to the identified failing node, ``warning`` resolution when
  the failure is unattributable, and a fully-terminal map.
"""
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.node_status import (
    NodeStatusCollector,
    STATUS_FAILURE,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_WARNING,
    TERMINAL_STATES,
)

_VOCABULARY = frozenset(
    {STATUS_PENDING, STATUS_RUNNING, STATUS_SUCCESS,
     STATUS_WARNING, STATUS_FAILURE})
_NON_TERMINAL = frozenset({STATUS_PENDING, STATUS_RUNNING})

# Small pools keep hypothesis focused on interesting collisions (shared
# node ids across elements, extra ids overlapping pipeline ids, unknown
# elements, untracked node ids).
_ELEMENTS = ["el0", "el1", "el2", "el3", "el4", "el5", "unmapped0", "unmapped1"]
_NODE_POOL = ["n0", "n1", "n2", "n3", "n4", "n5"]
_DETAILS = ["boom", "late frame", "buffer underrun", "x"]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_NAME_MAPS = st.dictionaries(
    keys=st.sampled_from(_ELEMENTS),
    values=st.one_of(st.none(), st.sampled_from(_NODE_POOL)),
    max_size=8,
)
_EXTRA_IDS = st.lists(st.sampled_from(_NODE_POOL), max_size=4)


@st.composite
def _history_ops(draw):
    """One random status-history operation."""
    kind = draw(st.sampled_from(
        ["running_all", "sink_running", "sink_running", "sink_warning",
         "sink_warning", "mark_failure", "set_detail", "invocation"]))
    if kind == "running_all":
        return ("running_all",)
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
            draw(st.one_of(st.integers(min_value=0, max_value=5000),
                           st.just(-1), st.just("bad"))))


@st.composite
def _cases(draw):
    name_map = draw(_NAME_MAPS)
    extra_ids = draw(_EXTRA_IDS)
    ops = draw(st.lists(_history_ops(), max_size=12))
    lifecycle = draw(st.sampled_from(
        ["success", "success", "post_eos_failure", "no_eos_failure"]))
    if lifecycle == "post_eos_failure":
        # The executor attributes post-EOS (binding-block) failures to a
        # node; an unattributed finalize only occurs on pre-EOS paths.
        fail_node = draw(st.sampled_from(_NODE_POOL))
    elif lifecycle == "no_eos_failure":
        fail_node = draw(st.one_of(st.none(), st.sampled_from(_NODE_POOL)))
    else:
        fail_node = None
    fail_detail = draw(st.sampled_from(_DETAILS))
    return {
        "name_map": name_map,
        "extra_ids": extra_ids,
        "ops": ops,
        "lifecycle": lifecycle,
        "fail_node": fail_node,
        "fail_detail": fail_detail,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build(case):
    return NodeStatusCollector(
        name_map=dict(case["name_map"]),
        extra_node_ids=list(case["extra_ids"]),
    )


def _apply_history(collector, ops):
    for op in ops:
        if op[0] == "running_all":
            collector.mark_running_all()
        elif op[0] == "sink":
            collector.sink(op[1], op[2], op[3])
        elif op[0] == "mark_failure":
            collector.mark_failure(op[1], op[2])
        elif op[0] == "set_detail":
            collector.set_detail(op[1], op[2])
        elif op[0] == "invocation":
            collector.record_invocation_duration(op[1], op[2])


def _snapshot(collector):
    """(statuses, details, serialized durations) via public API only."""
    statuses = {nid: collector.status_of(nid)
                for nid in collector.participating_nodes()}
    node_map = collector.to_map()
    details = {nid: entry.get("detail") for nid, entry in node_map.items()}
    durations = {nid: collector.duration_ms_of(nid) for nid in statuses}
    return statuses, details, durations


def _assert_map_shape(node_map):
    """The existing {status, detail?, durationMs?} shape and five-status
    vocabulary (R2.4), with non-negative-integer durations (R2.3)."""
    for entry in node_map.values():
        assert set(entry.keys()) <= {"status", "detail", "durationMs"}
        assert entry["status"] in _VOCABULARY
        if "detail" in entry:
            assert isinstance(entry["detail"], str) and entry["detail"]
        if "durationMs" in entry:
            duration = entry["durationMs"]
            assert isinstance(duration, int)
            assert not isinstance(duration, bool)
            assert duration >= 0


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

# Feature: vllm-workflow-latency-optimization, Property 4: Pipeline_EOS terminal marking is scoped and parity-preserving
@settings(max_examples=150, deadline=None)
@given(case=_cases())
def test_property_4_eos_marking_scoped_and_parity_preserving(case):
    """**Feature: vllm-workflow-latency-optimization, Property 4:
    Pipeline_EOS terminal marking is scoped and parity-preserving**

    **Validates: Requirements 2.1, 2.3, 2.4, 2.5, 2.7**
    """
    pipeline_ids = {nid for nid in case["name_map"].values()
                    if nid is not None}

    # ---- Part A: scoped marking on a probe collector (R2.1, 2.4, 2.7) ----
    probe = _build(case)
    _apply_history(probe, case["ops"])
    pre_statuses, pre_details, pre_durations = _snapshot(probe)

    probe.mark_pipeline_success()

    post_statuses, post_details, post_durations = _snapshot(probe)
    # mark_pipeline_success adds/removes no participating node.
    assert set(post_statuses) == set(pre_statuses)
    for nid, before in pre_statuses.items():
        after = post_statuses[nid]
        if nid in pipeline_ids and before == STATUS_RUNNING:
            # R2.1: exactly the running Pipeline_Nodes become success ...
            assert after == STATUS_SUCCESS
            # ... freezing a non-negative-integer duration at EOS.
            duration = post_durations[nid]
            assert isinstance(duration, int)
            assert not isinstance(duration, bool)
            assert duration >= 0
        else:
            # R2.4/R2.7: warning (with detail) and failure retained,
            # pending Pipeline_Nodes and all binding nodes untouched.
            assert after == before
            assert post_durations[nid] == pre_durations[nid]
        # Details are never altered by the EOS marking.
        assert post_details[nid] == pre_details[nid]

    # R2.4: serialized map stays within the existing shape/vocabulary.
    probe_map = probe.to_map()
    _assert_map_shape(probe_map)
    assert json.loads(probe.to_json()) == probe_map

    # ---- Part B: final-map parity, featured vs pre-feature (R2.3, 2.5) ----
    featured = _build(case)
    reference = _build(case)
    _apply_history(featured, case["ops"])
    _apply_history(reference, case["ops"])

    if case["lifecycle"] == "success":
        # Clean run: EOS marking (featured only), then the executor's
        # terminal sequence on both.
        featured.mark_pipeline_success()
        for collector in (featured, reference):
            collector.mark_success_all()
            collector.finalize()
    elif case["lifecycle"] == "post_eos_failure":
        # Pipeline reached EOS, a later (binding-block) failure is
        # attributed to a node; terminal sequence identical on both.
        featured.mark_pipeline_success()
        for collector in (featured, reference):
            collector.mark_failure(case["fail_node"], case["fail_detail"])
            collector.finalize(failure_detail=case["fail_detail"])
    else:
        # Pipeline error/timeout without EOS (R2.3): the executor's failure
        # handlers return before the mark_pipeline_success call site, so
        # NEITHER collector performs the EOS marking; the existing finalize
        # rules apply byte-identically.
        pre_terminal, _, _ = _snapshot(reference)
        for collector in (featured, reference):
            if case["fail_node"] is not None:
                collector.mark_failure(case["fail_node"],
                                       case["fail_detail"])
            collector.finalize(failure_detail=case["fail_detail"])

        # R2.3: failure attributed to the identified failing node; when no
        # node holds failure, non-terminal nodes resolve to warning.
        attributed = case["fail_node"] is not None or any(
            status == STATUS_FAILURE for status in pre_terminal.values())
        reference_map = reference.to_map()
        for nid, before in pre_terminal.items():
            status = reference_map[nid]["status"]
            if nid == case["fail_node"]:
                assert status == STATUS_FAILURE
            elif before in _NON_TERMINAL:
                assert status == (STATUS_SUCCESS if attributed
                                  else STATUS_WARNING)
                if not attributed:
                    assert "detail" in reference_map[nid]
            else:
                assert status == before

    featured_map = featured.to_map()
    reference_map = reference.to_map()

    # R2.5: the final terminal status (and detail) of every node equals the
    # pre-feature lifecycle's; only durations may differ in value.
    assert set(featured_map) == set(reference_map)
    for nid in featured_map:
        assert featured_map[nid]["status"] == reference_map[nid]["status"]
        assert (featured_map[nid].get("detail")
                == reference_map[nid].get("detail"))
        assert (("durationMs" in featured_map[nid])
                == ("durationMs" in reference_map[nid]))

    # Fully-terminal maps within the existing shape, all serialized
    # durations non-negative integers (R2.3, R2.4).
    for node_map in (featured_map, reference_map):
        _assert_map_shape(node_map)
        for entry in node_map.values():
            assert entry["status"] in TERMINAL_STATES
