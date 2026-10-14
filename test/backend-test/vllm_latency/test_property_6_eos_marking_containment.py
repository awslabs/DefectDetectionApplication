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
"""Property test for EOS marking containment (task 7.5).

# Feature: vllm-workflow-latency-optimization, Property 6: EOS marking containment

*For any* internal fault injected into the EOS transition path,
``mark_pipeline_success`` SHALL not raise, and the subsequent run lifecycle
(snapshots, binding processing, terminal persistence) SHALL proceed to the
same run outcome as a fault-free run.

**Validates: Requirements 2.6**

The test builds a :class:`workflow_engine.node_status.NodeStatusCollector`
from a hypothesis-generated construction (``name_map`` / ``extra_node_ids``)
and random status history, then injects an internal fault into the EOS
transition path before calling ``mark_pipeline_success``:

* ``_set_status`` patched to raise immediately, or to raise after N
  successful delegated writes (a partial EOS marking);
* ``_statuses`` swapped for a hostile mapping whose ``get`` raises;
* ``_pipeline_node_ids`` swapped for a hostile iterable that raises on
  iteration, or one that yields a prefix of the real ids then raises
  (another partial-marking shape);
* ``time.monotonic`` patched to raise (the timing-capture fault the
  collector's ``_set_status`` containment shell handles internally).

It asserts (a) ``mark_pipeline_success`` never raises under any injected
fault, and (b) after the fault window closes, the executor's subsequent
terminal lifecycle — clean success (``mark_success_all`` + ``finalize``) or
post-EOS attributed failure (``mark_failure`` + ``finalize``) — drives the
faulted collector to the same final terminal statuses and details as a
fault-free reference collector.

The reference collector deliberately performs NO EOS call: R2.6's
containment guarantee is that the run proceeds and terminal resolution
still applies even when the EOS marking partially or wholly did not
happen, so the fault-free run outcome to match is the pre-EOS-marking
lifecycle's. (A fault-free run WITH the EOS call reaches the same terminal
statuses by Property 4's parity, so one reference covers both readings.)
Duration values/presence are excluded from the parity comparison — timing
is best-effort by design (a contained timing fault may legitimately drop a
duration) — but every serialized duration must remain a non-negative
integer within the existing map shape, and both final maps must be fully
terminal.
"""
import contextlib
import time

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

# Small pools keep hypothesis focused on interesting collisions (shared
# node ids across elements, extra ids overlapping pipeline ids, unknown
# elements, untracked node ids).
_ELEMENTS = ["el0", "el1", "el2", "el3", "el4", "unmapped0"]
_NODE_POOL = ["n0", "n1", "n2", "n3", "n4"]
_DETAILS = ["boom", "late frame", "x"]


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
def _history_ops(draw):
    """One random status-history operation."""
    kind = draw(st.sampled_from(
        ["running_all", "sink_running", "sink_running", "sink_warning",
         "mark_failure", "set_detail", "invocation"]))
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
            draw(st.integers(min_value=0, max_value=5000)))


_FAULTS = st.one_of(
    st.just(("set_status_raises",)),
    st.tuples(st.just("set_status_raises_after"),
              st.integers(min_value=0, max_value=4)),
    st.just(("statuses_hostile",)),
    st.just(("pipeline_ids_hostile",)),
    st.tuples(st.just("pipeline_ids_partial"),
              st.integers(min_value=0, max_value=4)),
    st.just(("monotonic_raises",)),
)


@st.composite
def _cases(draw):
    name_map = draw(_NAME_MAPS)
    extra_ids = draw(_EXTRA_IDS)
    ops = draw(st.lists(_history_ops(), max_size=10))
    fault = draw(_FAULTS)
    # The executor reaches the mark_pipeline_success call site only on
    # run_pipeline's clean-return path; the subsequent lifecycle is either
    # a clean success or a post-EOS (binding-block) failure, which the
    # executor always attributes to a node.
    lifecycle = draw(st.sampled_from(
        ["success", "success", "post_eos_failure"]))
    fail_node = (draw(st.sampled_from(_NODE_POOL))
                 if lifecycle == "post_eos_failure" else None)
    fail_detail = draw(st.sampled_from(_DETAILS))
    return {
        "name_map": name_map,
        "extra_ids": extra_ids,
        "ops": ops,
        "fault": fault,
        "lifecycle": lifecycle,
        "fail_node": fail_node,
        "fail_detail": fail_detail,
    }


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------

class _InjectedFault(RuntimeError):
    """The hostile error every injected fault raises."""


class _HostileStatuses(dict):
    """A ``_statuses`` stand-in whose read path raises."""

    def get(self, *args, **kwargs):  # noqa: D102 - hostile by design
        raise _InjectedFault("hostile _statuses.get")


class _HostileIterable:
    """A ``_pipeline_node_ids`` stand-in that raises on iteration."""

    def __iter__(self):
        raise _InjectedFault("hostile _pipeline_node_ids iteration")


class _PartialIterable:
    """Yields the first ``n`` real ids, then raises mid-iteration."""

    def __init__(self, items, n):
        self._items = list(items)
        self._n = n

    def __iter__(self):
        for index, item in enumerate(self._items):
            if index >= self._n:
                raise _InjectedFault("hostile partial iteration")
            yield item


@contextlib.contextmanager
def _inject_fault(collector, fault):
    """Arm ``fault`` on ``collector`` for the EOS transition, then disarm.

    The fault window covers exactly the ``mark_pipeline_success`` call —
    the EOS transition path R2.6 scopes containment to. The subsequent run
    lifecycle executes fault-free, as it does in the executor.
    """
    kind = fault[0]
    if kind == "set_status_raises":
        def raiser(node_id, status):
            raise _InjectedFault("injected _set_status fault")
        collector._set_status = raiser
        try:
            yield
        finally:
            collector.__dict__.pop("_set_status", None)
    elif kind == "set_status_raises_after":
        limit = fault[1]
        calls = {"n": 0}
        real = NodeStatusCollector._set_status

        def flaky(node_id, status):
            if calls["n"] >= limit:
                raise _InjectedFault("injected delayed _set_status fault")
            calls["n"] += 1
            real(collector, node_id, status)
        collector._set_status = flaky
        try:
            yield
        finally:
            collector.__dict__.pop("_set_status", None)
    elif kind == "statuses_hostile":
        original = collector._statuses
        collector._statuses = _HostileStatuses(original)
        try:
            yield
        finally:
            collector._statuses = original
    elif kind == "pipeline_ids_hostile":
        original = collector._pipeline_node_ids
        collector._pipeline_node_ids = _HostileIterable()
        try:
            yield
        finally:
            collector._pipeline_node_ids = original
    elif kind == "pipeline_ids_partial":
        original = collector._pipeline_node_ids
        collector._pipeline_node_ids = _PartialIterable(original, fault[1])
        try:
            yield
        finally:
            collector._pipeline_node_ids = original
    elif kind == "monotonic_raises":
        original = time.monotonic

        def bad_monotonic():
            raise _InjectedFault("injected time.monotonic fault")
        time.monotonic = bad_monotonic
        try:
            yield
        finally:
            time.monotonic = original
    else:  # pragma: no cover - strategy exhausts the kinds above
        raise AssertionError("unknown fault kind: {0}".format(kind))


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


def _run_terminal_lifecycle(collector, case):
    """The executor's post-EOS terminal sequence (binding processing +
    terminal persistence)."""
    if case["lifecycle"] == "success":
        collector.mark_success_all()
        collector.finalize()
    else:
        collector.mark_failure(case["fail_node"], case["fail_detail"])
        collector.finalize(failure_detail=case["fail_detail"])


def _assert_map_shape(node_map):
    """The existing {status, detail?, durationMs?} shape and five-status
    vocabulary, with non-negative-integer durations."""
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

# Feature: vllm-workflow-latency-optimization, Property 6: EOS marking containment
@settings(max_examples=150, deadline=None)
@given(case=_cases())
def test_property_6_eos_marking_containment(case):
    """**Feature: vllm-workflow-latency-optimization, Property 6: EOS
    marking containment**

    **Validates: Requirements 2.6**
    """
    faulted = _build(case)
    reference = _build(case)
    _apply_history(faulted, case["ops"])
    _apply_history(reference, case["ops"])

    # ---- (a) mark_pipeline_success never raises under an injected fault.
    with _inject_fault(faulted, case["fault"]):
        try:
            faulted.mark_pipeline_success()
        except Exception as exc:  # noqa: BLE001 - the property under test
            raise AssertionError(
                "mark_pipeline_success raised under injected fault "
                "{0!r}: {1!r}".format(case["fault"], exc))

    # ---- (b) the subsequent run lifecycle proceeds to the same outcome
    # as fault-free. The reference performs NO EOS call: a contained fault
    # means the EOS marking may simply not have happened, and terminal
    # resolution must still produce the fault-free run outcome.
    _run_terminal_lifecycle(faulted, case)
    _run_terminal_lifecycle(reference, case)

    faulted_map = faulted.to_map()
    reference_map = reference.to_map()

    # Terminal persistence proceeds: serialization works and both maps
    # cover the same participating nodes.
    assert set(faulted_map) == set(reference_map)
    assert faulted.participating_nodes() == reference.participating_nodes()

    # Same run outcome: identical final terminal statuses and details.
    for nid in reference_map:
        assert faulted_map[nid]["status"] == reference_map[nid]["status"]
        assert (faulted_map[nid].get("detail")
                == reference_map[nid].get("detail"))

    # Fully-terminal maps within the existing shape; every serialized
    # duration a non-negative integer. (Duration presence/values are
    # excluded from parity: timing is best-effort, so a contained timing
    # fault may legitimately drop a duration.)
    for node_map in (faulted_map, reference_map):
        _assert_map_shape(node_map)
        for entry in node_map.values():
            assert entry["status"] in TERMINAL_STATES

    # The fault window is closed: the faulted collector serializes cleanly.
    faulted.to_json()
