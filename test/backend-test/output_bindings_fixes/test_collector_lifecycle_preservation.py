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
"""Preservation property tests (Task 2) for workflow-output-bindings-fixes.

Property 5: Preservation — collector lifecycle identity: for any
element-only name map (no binding nodes) and any sequence of bus signals
and terminal transitions, ``NodeStatusCollector.to_map()`` produces exactly
the map today's semantics produce, and a ``warning``/``failure`` status is
never downgraded.

**Validates: Requirements 3.5, 3.8**

Observation-first: the reference model below (``_ReferenceCollector``) is a
straight re-encoding of the transitions OBSERVED on the current (unfixed)
``node_status.py``:

* construction seeds every distinct non-None nodeId of the name map as
  ``pending``;
* ``sink(element, "running")`` advances only ``pending`` -> ``running``;
* ``sink(element, "warning", detail)`` sets ``warning`` (retaining the
  detail) unless the node is already ``failure``; unknown/synthetic
  element names are ignored;
* ``mark_running_all`` advances every ``pending`` node to ``running``;
* ``mark_success_all`` / ``finalize`` resolve ``pending``/``running`` to
  ``success`` and never touch ``warning``/``failure``;
* ``mark_failure(node, detail)`` sets ``failure`` unconditionally (None is
  a no-op) and retains the detail;
* ``to_map()`` emits ``{status}`` plus ``detail`` only when truthy.

The fix for this spec seeds executor-BINDING node ids into the collector —
a strict extension. Every input generated here is an element-only map, so
these tests MUST PASS today and keep passing after the fix (extends the
``test_property_node_status.py`` patterns with a full lifecycle-identity
model).

Runs with the hypothesis profiles registered in ``test/backend-test/
conftest.py`` (``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci``
= 100).
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.node_status import (
    NodeStatusCollector,
    STATUS_FAILURE,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_WARNING,
)


# ---------------------------------------------------------------------------
# Reference model: today's transitions, re-encoded independently
# ---------------------------------------------------------------------------

class _ReferenceCollector:
    """The OBSERVED (unfixed) NodeStatusCollector semantics."""

    def __init__(self, name_map):
        self.name_map = dict(name_map)
        self.statuses = {}
        self.details = {}
        for node_id in self.name_map.values():
            if node_id is not None and node_id not in self.statuses:
                self.statuses[node_id] = STATUS_PENDING

    def apply(self, op):
        kind = op[0]
        if kind == "sink_running":
            node_id = self.name_map.get(op[1])
            if node_id is not None and \
                    self.statuses.get(node_id) == STATUS_PENDING:
                self.statuses[node_id] = STATUS_RUNNING
        elif kind == "sink_warning":
            node_id = self.name_map.get(op[1])
            if node_id is not None and \
                    self.statuses.get(node_id) != STATUS_FAILURE:
                self.statuses[node_id] = STATUS_WARNING
                if op[2]:
                    self.details[node_id] = op[2]
        elif kind == "mark_running_all":
            for node_id, status in self.statuses.items():
                if status == STATUS_PENDING:
                    self.statuses[node_id] = STATUS_RUNNING
        elif kind == "mark_success_all" or kind == "finalize":
            for node_id, status in self.statuses.items():
                if status in (STATUS_PENDING, STATUS_RUNNING):
                    self.statuses[node_id] = STATUS_SUCCESS
        elif kind == "mark_failure":
            node_id = op[1]
            if node_id is not None:
                self.statuses[node_id] = STATUS_FAILURE
                if op[2]:
                    self.details[node_id] = op[2]

    def to_map(self):
        result = {}
        for node_id, status in self.statuses.items():
            entry = {"status": status}
            detail = self.details.get(node_id)
            if detail:
                entry["detail"] = detail
            result[node_id] = entry
        return result


def _apply_real(collector, op):
    kind = op[0]
    if kind == "sink_running":
        collector.sink(op[1], "running")
    elif kind == "sink_warning":
        collector.sink(op[1], "warning", op[2])
    elif kind == "mark_running_all":
        collector.mark_running_all()
    elif kind == "mark_success_all":
        collector.mark_success_all()
    elif kind == "finalize":
        collector.finalize()
    elif kind == "mark_failure":
        collector.mark_failure(op[1], op[2])


# ---------------------------------------------------------------------------
# Generators: element-only name maps (no binding nodes) + op sequences
# ---------------------------------------------------------------------------

_NODE_IDS = st.sampled_from(["n0", "n1", "n2", "n3", "n4", None])


@st.composite
def _name_maps(draw):
    """A realistic element-name -> nodeId map (names unique, nodeIds drawn
    from a pool with duplicates and Nones), as rendering.element_name_map
    produces it for an element-only document."""
    node_ids = draw(st.lists(_NODE_IDS, min_size=1, max_size=8))
    return {
        "el{0}".format(index): node_id
        for index, node_id in enumerate(node_ids)
    }


@st.composite
def _op_sequences(draw, name_map):
    """A run-shaped operation sequence: element names include unknown
    (synthetic) names, failure targets include participating nodes and
    None; the interleaving is arbitrary so retention rules are exercised
    from every state."""
    element_names = list(name_map) + ["synthetic-tee0", "pipeline0"]
    participating = sorted(
        {n for n in name_map.values() if n is not None}) or ["n0"]
    details = st.one_of(st.none(), st.sampled_from(["w", "boom", "detail-x"]))
    op = st.one_of(
        st.tuples(st.just("sink_running"), st.sampled_from(element_names)),
        st.tuples(st.just("sink_warning"), st.sampled_from(element_names),
                  details),
        st.tuples(st.just("mark_running_all")),
        st.tuples(st.just("mark_success_all")),
        st.tuples(st.just("finalize")),
        st.tuples(st.just("mark_failure"),
                  st.one_of(st.none(), st.sampled_from(participating)),
                  details),
    )
    return draw(st.lists(op, min_size=0, max_size=12))


@st.composite
def _cases(draw):
    name_map = draw(_name_maps())
    ops = draw(_op_sequences(name_map))
    return name_map, ops


# ---------------------------------------------------------------------------
# Property 5: lifecycle identity + no downgrade
# ---------------------------------------------------------------------------

@given(case=_cases())
@settings(deadline=None)
def test_collector_lifecycle_identity(case):
    """**Property 5: Preservation — collector lifecycle identity.** For any
    element-only name map and any operation sequence, the collector's
    ``to_map()`` equals the reference model of today's semantics after
    every prefix of the sequence.

    **Validates: Requirements 3.5, 3.8**
    """
    name_map, ops = case
    collector = NodeStatusCollector(name_map)
    reference = _ReferenceCollector(name_map)

    assert collector.to_map() == reference.to_map(), (
        "PRESERVATION REGRESSION (Property 5): construction seeding "
        "changed for an element-only map {0!r}".format(name_map))

    for index, op in enumerate(ops):
        _apply_real(collector, op)
        reference.apply(op)
        assert collector.to_map() == reference.to_map(), (
            "PRESERVATION REGRESSION (Property 5): collector diverged from "
            "today's semantics after op {0} ({1!r}):\n  actual:   {2!r}\n"
            "  expected: {3!r}".format(
                index, op, collector.to_map(), reference.to_map()))


@given(case=_cases())
@settings(deadline=None)
def test_warning_and_failure_are_never_downgraded(case):
    """**Property 5: Preservation — no downgrade.** Once a node reaches
    ``warning`` it can only move to ``failure``; once it reaches
    ``failure`` it never changes again — across any operation sequence.

    **Validates: Requirements 3.5, 3.8**
    """
    name_map, ops = case
    collector = NodeStatusCollector(name_map)

    previous = {n: e["status"] for n, e in collector.to_map().items()}
    for op in ops:
        _apply_real(collector, op)
        current = {n: e["status"] for n, e in collector.to_map().items()}
        for node_id, status_before in previous.items():
            status_after = current.get(node_id)
            if status_before == STATUS_FAILURE:
                assert status_after == STATUS_FAILURE, (
                    "PRESERVATION REGRESSION (Property 5): {0} downgraded "
                    "from failure to {1} by op {2!r}".format(
                        node_id, status_after, op))
            elif status_before == STATUS_WARNING:
                assert status_after in (STATUS_WARNING, STATUS_FAILURE), (
                    "PRESERVATION REGRESSION (Property 5): {0} downgraded "
                    "from warning to {1} by op {2!r}".format(
                        node_id, status_after, op))
        previous = current
