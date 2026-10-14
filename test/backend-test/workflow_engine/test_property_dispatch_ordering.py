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
"""Property tests for Activation_Queue dispatch ordering (Task 6.4).

# Feature: trigger-activation-runtime, Property 10: Priority-then-FIFO dispatch order

*For any generated set of pending Run_Activations, the Activation_Dispatcher
dequeues them in exactly the order sorted by (`priority` ascending,
`firing_seq` ascending).*

**Validates: Requirements 7.5**
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.trigger_runtime import (
    ActivationDispatcher,
    ActivationQueue,
    FiringSequence,
    RunActivation,
)

# Priorities drawn from a small pool inside the catalog 0-1000 range so that
# duplicate priorities (the FIFO tie-break case) are generated often.
_PRIORITIES = st.sampled_from([0, 1, 5, 100, 100, 100, 500, 1000])

_TRIGGER_NODE_IDS = st.sampled_from(["trig-a", "trig-b", "trig-c"])


@st.composite
def _pending_activation_sets(draw):
    """A set of Run_Activations with firing_seq assigned in firing order
    (via FiringSequence, exactly as TriggerGate._enqueue does) and a push
    order that may differ from the firing order, so the heap has to restore
    (priority, firing_seq) order itself."""
    sequence = FiringSequence()
    specs = draw(
        st.lists(
            st.tuples(_PRIORITIES, _TRIGGER_NODE_IDS), min_size=0, max_size=20
        )
    )
    activations = [
        RunActivation(
            trigger_node_id=node_id,
            activation_group=node_id,
            priority=priority,
            firing_seq=sequence.next(),
            context={"seq": index},
        )
        for index, (priority, node_id) in enumerate(specs)
    ]
    push_order = draw(st.permutations(activations))
    return activations, push_order


# Feature: trigger-activation-runtime, Property 10: Priority-then-FIFO dispatch order
@settings(max_examples=100)
@given(_pending_activation_sets())
def test_queue_drains_in_priority_then_fifo_order(activation_set):
    """Draining the ActivationQueue yields exactly the pending activations
    sorted by (priority ascending, firing_seq ascending).

    **Validates: Requirements 7.5**
    """
    activations, push_order = activation_set
    queue = ActivationQueue()
    for activation in push_order:
        queue.push(activation)

    drained = []
    while True:
        activation = queue.pop_nowait()
        if activation is None:
            break
        drained.append(activation)

    expected = sorted(activations, key=lambda a: (a.priority, a.firing_seq))
    assert drained == expected
    assert len(queue) == 0


# Feature: trigger-activation-runtime, Property 10: Priority-then-FIFO dispatch order
@settings(max_examples=100)
@given(_pending_activation_sets())
def test_dispatcher_runs_activations_in_priority_then_fifo_order(activation_set):
    """Driving the drain through ActivationDispatcher.run_activation (the
    dispatcher loop body) with a recording run_starter dispatches in exactly
    sorted (priority, firing_seq) order.

    **Validates: Requirements 7.5**
    """
    activations, push_order = activation_set
    queue = ActivationQueue()
    for activation in push_order:
        queue.push(activation)

    ran = []
    dispatcher = ActivationDispatcher(queue, ran.append, name="prop-10")

    # Synchronous pop loop mirroring ActivationDispatcher._loop, without the
    # background thread: the dispatcher pops from the queue and runs each
    # activation through run_activation (failure-contained loop body).
    while True:
        activation = queue.pop_nowait()
        if activation is None:
            break
        dispatcher.run_activation(activation)

    expected = sorted(activations, key=lambda a: (a.priority, a.firing_seq))
    assert ran == expected
    assert not dispatcher.is_in_flight()
