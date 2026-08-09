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
"""Property test for trigger concurrency policies (task 6.3).

# Feature: trigger-activation-runtime, Property 9: Concurrency policy conformance

*For any* trigger node configuration and any generated firing sequence
(with a controlled in-flight run), the pending activations conform to the
node's policy: under ``queue``, the pending count never exceeds
``queue_depth`` and overflow firings are discarded; under ``drop``, a
firing is discarded whenever an activation from that node is pending or in
flight; under ``debounce``, all firings within a trailing ``debounce_ms``
window coalesce into exactly one activation carrying the most recent
Trigger_Context.

**Validates: Requirements 7.2, 7.3, 7.4**

The generated scenarios drive the real :class:`TriggerGate` +
:class:`ActivationQueue` pair with a stub ``in_flight_probe`` (the
controlled in-flight run) and a manual-fire ``timer_factory`` so debounce
expiry is deterministic without sleeping.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.trigger_runtime import (
    POLICY_DEBOUNCE,
    POLICY_DROP,
    POLICY_QUEUE,
    ActivationQueue,
    FiringSequence,
    TriggerGate,
    TriggerPolicy,
)

NODE_ID = "trig-under-test"


# --- fakes -------------------------------------------------------------------


class ManualTimer:
    """A ``threading.Timer`` stand-in the test fires by hand, so debounce
    expiry is deterministic without sleeping."""

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.started = False
        self.cancelled = False
        self.fired = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    @property
    def active(self):
        """Armed and not yet consumed (a real Timer fires once)."""
        return self.started and not self.cancelled and not self.fired

    def fire(self):
        """Simulate the debounce window elapsing."""
        if self.active:
            self.fired = True
            self.callback()


class ManualTimerFactory:
    """Collects every timer the gate arms; the test expires the most
    recently armed (un-cancelled) one to end a debounce window."""

    def __init__(self):
        self.timers = []

    def __call__(self, delay, callback):
        timer = ManualTimer(delay, callback)
        self.timers.append(timer)
        return timer

    def expire_active(self):
        active = [t for t in self.timers if t.active]
        assert len(active) <= 1, "at most one debounce timer may be armed"
        for timer in active:
            timer.fire()


class FakeClock:
    """Injectable monotonic clock (no wall time involved)."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _make_gate(policy, in_flight, timer_factory=None):
    """The real gate + queue pair with a stub in-flight probe.

    ``in_flight`` is a one-element list the scenario mutates to model the
    controlled in-flight run (Requirement 7.3's "in flight" clause).
    """
    queue = ActivationQueue()
    gate = TriggerGate(
        policy,
        queue,
        FiringSequence(),
        in_flight_probe=lambda node_id: bool(in_flight[0]),
        timer_factory=timer_factory or ManualTimerFactory(),
        clock=FakeClock(),
    )
    return gate, queue


def _context(n):
    """A distinguishable Trigger_Context payload for firing ``n``."""
    return {"topic": "t/x", "payload": "message-{}".format(n), "qos": 0}


# --- generators --------------------------------------------------------------

# An operation sequence over one trigger node: fire (with the controlled
# in-flight flag at that instant), or dispatch (pop) the head activation.
_OPS = st.lists(
    st.one_of(
        st.tuples(st.just("fire"), st.booleans()),
        st.just(("pop", False)),
    ),
    min_size=1,
    max_size=40,
)

# Debounce scenarios: bursts of firings; each burst stays inside one
# trailing window (the manual timer never expires mid-burst), then the
# window elapses.
_BURSTS = st.lists(
    st.integers(min_value=1, max_value=8), min_size=1, max_size=6
)


# --- properties --------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(queue_depth=st.integers(min_value=1, max_value=10), ops=_OPS)
def test_queue_policy_bounds_pending_and_discards_overflow(queue_depth, ops):
    """# Feature: trigger-activation-runtime, Property 9: Concurrency policy conformance

    Under ``queue`` the pending count never exceeds ``queue_depth`` and a
    firing is discarded exactly when the bound is already reached.

    **Validates: Requirements 7.2**
    """
    in_flight = [False]
    policy = TriggerPolicy(
        trigger_node_id=NODE_ID,
        concurrency_policy=POLICY_QUEUE,
        queue_depth=queue_depth,
    )
    gate, queue = _make_gate(policy, in_flight)

    expected_pending = []  # model: contexts pending, FIFO
    for i, (op, flag) in enumerate(ops):
        if op == "fire":
            in_flight[0] = flag  # queue ignores in-flight; bound is pending
            context = _context(i)
            accepted = gate.fire(context)
            # Overflow firings are discarded, others append (7.2).
            if len(expected_pending) >= queue_depth:
                assert accepted is False
            else:
                assert accepted is True
                expected_pending.append(context)
        else:
            popped = queue.pop_nowait()
            if expected_pending:
                assert popped is not None
                assert popped.context == expected_pending.pop(0)
            else:
                assert popped is None

        # The bound holds at every instant.
        assert queue.pending_count(NODE_ID) == len(expected_pending)
        assert queue.pending_count(NODE_ID) <= queue_depth


@settings(max_examples=100, deadline=None)
@given(ops=_OPS)
def test_drop_policy_discards_while_pending_or_in_flight(ops):
    """# Feature: trigger-activation-runtime, Property 9: Concurrency policy conformance

    Under ``drop`` a firing is discarded whenever an activation from the
    node is pending or in flight, and accepted otherwise.

    **Validates: Requirements 7.3**
    """
    in_flight = [False]
    policy = TriggerPolicy(
        trigger_node_id=NODE_ID, concurrency_policy=POLICY_DROP
    )
    gate, queue = _make_gate(policy, in_flight)

    pending = 0  # model: at most one under drop
    for i, (op, flag) in enumerate(ops):
        if op == "fire":
            in_flight[0] = flag
            accepted = gate.fire(_context(i))
            if pending > 0 or flag:
                assert accepted is False
            else:
                assert accepted is True
                pending += 1
        else:
            popped = queue.pop_nowait()
            if pending:
                assert popped is not None
                pending -= 1
            else:
                assert popped is None

        assert queue.pending_count(NODE_ID) == pending
        assert pending <= 1


@settings(max_examples=100, deadline=None)
@given(bursts=_BURSTS, debounce_ms=st.integers(min_value=1, max_value=60000))
def test_debounce_policy_coalesces_to_latest_context(bursts, debounce_ms):
    """# Feature: trigger-activation-runtime, Property 9: Concurrency policy conformance

    Under ``debounce`` all firings within one trailing ``debounce_ms``
    window coalesce into exactly one activation carrying the most recent
    Trigger_Context, and the armed timer's delay is ``debounce_ms``.

    **Validates: Requirements 7.4**
    """
    in_flight = [False]
    timer_factory = ManualTimerFactory()
    policy = TriggerPolicy(
        trigger_node_id=NODE_ID,
        concurrency_policy=POLICY_DEBOUNCE,
        debounce_ms=debounce_ms,
    )
    gate, queue = _make_gate(policy, in_flight, timer_factory=timer_factory)

    n = 0
    for burst_index, burst_size in enumerate(bursts):
        contexts = []
        for _ in range(burst_size):
            context = _context(n)
            n += 1
            contexts.append(context)
            # Every firing inside the window is accepted (coalesced), and
            # nothing is enqueued until the window elapses.
            assert gate.fire(context) is True
            assert queue.pending_count(NODE_ID) == burst_index

        # Trailing semantics: exactly one timer is armed, at debounce_ms,
        # re-armed by the burst's last firing.
        active = [t for t in timer_factory.timers if t.active]
        assert len(active) == 1
        assert active[0].delay == debounce_ms / 1000.0

        # The window elapses: exactly one activation, latest context (7.4).
        timer_factory.expire_active()
        assert queue.pending_count(NODE_ID) == burst_index + 1

    # One activation per burst, each carrying its burst's most recent
    # Trigger_Context, dispatched in firing order.
    expected_latest = []
    m = 0
    for burst_size in bursts:
        m += burst_size
        expected_latest.append(_context(m - 1))
    drained = []
    while True:
        activation = queue.pop_nowait()
        if activation is None:
            break
        assert activation.trigger_node_id == NODE_ID
        drained.append(activation.context)
    assert drained == expected_latest
