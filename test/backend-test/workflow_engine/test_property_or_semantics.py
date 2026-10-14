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
"""Property test for OR semantics with activation isolation (Task 6.5).

# Feature: trigger-activation-runtime, Property 11: OR semantics with activation isolation

*For any generated interleaving of firings across a workflow's multiple
triggers, every firing not discarded by a concurrency policy yields exactly
one Run_Activation whose Activation_Group id is its trigger node id, and a
failed run never prevents subsequent pending activations from dispatching.*

**Validates: Requirements 7.1, 7.7**

The test drives the :class:`WorkflowActivationCore` synchronously (no
dispatcher thread): firings and dispatch steps are interleaved per the
generated operation sequence, dispatch pops the queue head and runs it
through ``dispatcher.run_activation`` with an injected ``run_starter`` that
records every activation and raises on the drawn failure set. Policy
discards (``drop``, bounded ``queue``) are accounted for precisely with a
mirror model, so the accepted-firing count is deterministic.
"""
import heapq

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.trigger_runtime import (
    POLICY_DROP,
    POLICY_QUEUE,
    TriggerPolicy,
    WorkflowActivationCore,
)

# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: Ample queue depth — no queue-policy discard can occur at this bound.
_AMPLE_DEPTH = 1000

#: Cap on generated operations (also bounds firing_seq values).
_MAX_OPS = 30


@st.composite
def _trigger_policies(draw):
    """2–4 trigger nodes with varied, discard-deterministic policies.

    ``queue`` with ample depth never discards; ``queue`` with a small
    depth and ``drop`` discard deterministically as a function of the
    per-node pending count (dispatch is synchronous, so nothing is ever
    in flight when a firing arrives). ``debounce`` is timer-driven and
    deliberately excluded to keep the accept count deterministic.
    """
    n_triggers = draw(st.integers(min_value=2, max_value=4))
    policies = []
    for i in range(n_triggers):
        kind = draw(st.sampled_from(["queue_ample", "queue_small", "drop"]))
        if kind == "drop":
            policy = POLICY_DROP
            depth = _AMPLE_DEPTH
        elif kind == "queue_small":
            policy = POLICY_QUEUE
            depth = draw(st.integers(min_value=1, max_value=3))
        else:
            policy = POLICY_QUEUE
            depth = _AMPLE_DEPTH
        policies.append(
            TriggerPolicy(
                trigger_node_id=f"trig{i}",
                concurrency_policy=policy,
                queue_depth=depth,
                # Small priority pool so cross-trigger ties exercise FIFO.
                priority=draw(st.integers(min_value=0, max_value=3)),
            )
        )
    return policies


@st.composite
def _interleavings(draw, n_triggers):
    """A sequence of operations: ('fire', trigger_index) or ('dispatch',)."""
    ops = draw(
        st.lists(
            st.one_of(
                st.tuples(
                    st.just("fire"),
                    st.integers(min_value=0, max_value=n_triggers - 1),
                ),
                st.tuples(st.just("dispatch")),
            ),
            min_size=1,
            max_size=_MAX_OPS,
        )
    )
    return ops


# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_or_semantics_with_activation_isolation(data):
    """# Feature: trigger-activation-runtime, Property 11: OR semantics with activation isolation

    **Validates: Requirements 7.1, 7.7**
    """
    policies = data.draw(_trigger_policies())
    ops = data.draw(_interleavings(len(policies)))
    # Injected run failures: activations whose firing_seq is drawn here
    # raise inside run_starter (Requirement 7.7 containment).
    fail_seqs = data.draw(
        st.sets(st.integers(min_value=0, max_value=_MAX_OPS), max_size=_MAX_OPS)
    )

    dispatched = []  # every activation the run_starter received, in order

    def run_starter(activation):
        dispatched.append(activation)
        if activation.firing_seq in fail_seqs:
            raise RuntimeError(
                f"injected failure for firing_seq={activation.firing_seq}"
            )

    core = WorkflowActivationCore(
        registration_id="reg-or-semantics",
        policies=policies,
        run_starter=run_starter,
    )
    # No core.start(): dispatch is driven synchronously below (no threads).

    by_node = {p.trigger_node_id: p for p in policies}

    # Mirror model of the queue/policy state.
    model_heap = []  # (priority, firing_seq, node_id)
    model_pending = {p.trigger_node_id: 0 for p in policies}
    next_seq = 0
    accepted_count = 0
    expected_order = []  # (priority, firing_seq, node_id) in dispatch order

    def model_accepts(policy):
        pending = model_pending[policy.trigger_node_id]
        if policy.concurrency_policy == POLICY_DROP:
            # Nothing is in flight between synchronous dispatches, so drop
            # discards exactly when an activation from this node is pending.
            return pending == 0
        return pending < policy.queue_depth

    def dispatch_one():
        """Pop the queue head and run it synchronously; verify against the
        model's expected (priority, firing_seq, node)."""
        nonlocal accepted_count
        activation = core.queue.pop_nowait()
        if not model_heap:
            assert activation is None
            return
        expected = heapq.heappop(model_heap)
        assert activation is not None
        assert (
            activation.priority,
            activation.firing_seq,
            activation.trigger_node_id,
        ) == expected
        model_pending[expected[2]] -= 1
        expected_order.append(expected)
        # Failure containment: run_activation never raises even when the
        # injected run_starter does (Requirement 7.7).
        core.dispatcher.run_activation(activation)

    for op in ops:
        if op[0] == "fire":
            policy = policies[op[1]]
            node_id = policy.trigger_node_id
            expected_accept = model_accepts(policy)
            accepted = core.fire(node_id, {"seq": next_seq, "node": node_id})
            assert accepted == expected_accept
            if expected_accept:
                heapq.heappush(
                    model_heap, (policy.priority, next_seq, node_id)
                )
                model_pending[node_id] += 1
                accepted_count += 1
            # firing_seq is allocated only for accepted (enqueued) firings.
            if expected_accept:
                next_seq += 1
        else:
            dispatch_one()

    # Drain everything still pending — a failed run must never prevent the
    # remaining activations from dispatching (Requirement 7.7).
    while model_heap:
        dispatch_one()
    assert core.queue.pop_nowait() is None
    assert len(core.queue) == 0

    # Every firing not discarded by a concurrency policy yielded exactly one
    # Run_Activation, and all of them dispatched despite injected failures
    # (Requirements 7.1, 7.7).
    assert len(dispatched) == accepted_count
    assert [
        (a.priority, a.firing_seq, a.trigger_node_id) for a in dispatched
    ] == expected_order

    # OR semantics extension point: every activation's Activation_Group id
    # is its own trigger node id (one implicit single-member group per
    # trigger, Requirement 7.1).
    for activation in dispatched:
        assert activation.activation_group == activation.trigger_node_id
        assert activation.trigger_node_id in by_node
        assert activation.context["node"] == activation.trigger_node_id

    core.stop()
