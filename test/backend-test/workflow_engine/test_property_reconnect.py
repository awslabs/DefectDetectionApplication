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
"""Property tests for the shared reconnect/backoff engine (Task 7.5).

# Feature: trigger-activation-runtime, Property 12: Reconnect bounding, backoff, and failure marking

*For any `retry_limit` value and any generated sequence of connection
failures (with an injected clock), the reconnect delays follow 1 s doubling
capped at 60 s; when `retry_limit` >= 1 the attempt count never exceeds it,
and on exhaustion the trigger's health is `failed` with an error message
containing the trigger node id and the topic (MQTT) or endpoint (OPC UA);
when `retry_limit` is 0 attempts continue for the whole generated sequence.*

**Validates: Requirements 8.1, 8.3**
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.trigger_runtime import (
    BINDING_MQTT_SUBSCRIBE,
    BINDING_OPCUA_SUBSCRIBE,
    HEALTH_FAILED,
    HEALTH_POLLING,
    HEALTH_RECONNECTING,
    HEALTH_SUBSCRIBED,
    MECHANISM_POLL,
    MECHANISM_SUBSCRIBE,
    RECONNECT_MAX_DELAY_SECONDS,
    ReconnectEngine,
    TriggerHealth,
    describe_trigger_target,
)

_NODE_IDS = st.sampled_from(["trigger-1", "mqtt_sub_a", "opc.node-7"])

_TARGET_TEXT = st.text(
    alphabet="abcdefgh0123456789/._-", min_size=1, max_size=24
)

# (binding kind, target parameter name, OPC UA mechanism or None for MQTT)
_TARGET_KINDS = st.sampled_from(
    [
        (BINDING_MQTT_SUBSCRIBE, "topic", None),
        (BINDING_OPCUA_SUBSCRIBE, "endpoint", MECHANISM_SUBSCRIBE),
        (BINDING_OPCUA_SUBSCRIBE, "endpoint", MECHANISM_POLL),
    ]
)


def _expected_delays(attempt_count):
    """The design backoff series: 1 s doubling, capped at 60 s
    (Requirement 8.1) — one wait before each attempt."""
    delays = []
    delay = 1.0
    for _ in range(attempt_count):
        delays.append(delay)
        delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)
    return delays


class _Harness:
    """A synchronous ReconnectEngine harness: recording waiter, fake
    clock, `spawn=lambda fn: fn()`, and a reconnect callable that fails
    (returning False or raising, per the generated plan) a set number of
    times before succeeding (or throughout)."""

    def __init__(self, node_id, kind, target_param, mechanism,
                 target, retry_limit, failure_plan, then_succeed):
        self.node_id = node_id
        self.kind = kind
        self.target = target
        self.health = TriggerHealth(node_id, kind, mechanism=mechanism)
        self.recorded_delays = []
        self.attempt_calls = 0
        self.states_seen_in_attempts = []
        self._failure_plan = failure_plan  # list of bools: True = raise
        self._then_succeed = then_succeed
        self._now = [0.0]
        self.engine = ReconnectEngine(
            node_id=node_id,
            target=target,
            health=self.health,
            retry_limit=retry_limit,
            reconnect=self._reconnect,
            clock=self._clock,
            waiter=self._waiter,
            spawn=lambda fn: fn(),
        )

    def _clock(self):
        return self._now[0]

    def _waiter(self, delay):
        self.recorded_delays.append(delay)
        self._now[0] += delay
        return False  # never cancelled

    def _reconnect(self):
        # The engine must have marked health `reconnecting` before this
        # attempt runs (Requirement 8.2's transition, asserted per call).
        self.states_seen_in_attempts.append(self.health.state)
        index = self.attempt_calls
        self.attempt_calls += 1
        if index < len(self._failure_plan):
            if self._failure_plan[index]:
                raise ConnectionError(f"attempt-{index}-raised")
            return False
        if self._then_succeed:
            return True
        return False


@st.composite
def _exhaustion_scenarios(draw):
    """retry_limit >= 1 with the reconnect callable failing throughout."""
    kind, target_param, mechanism = draw(_TARGET_KINDS)
    node_id = draw(_NODE_IDS)
    target_value = draw(_TARGET_TEXT)
    retry_limit = draw(st.integers(min_value=1, max_value=10))
    # Enough failure entries to cover every attempt; each is raise-or-False.
    failure_plan = draw(
        st.lists(st.booleans(), min_size=retry_limit, max_size=retry_limit)
    )
    return kind, target_param, mechanism, node_id, target_value, retry_limit, failure_plan


@st.composite
def _success_scenarios(draw):
    """A failure sequence that ends in success: retry_limit 0 (unbounded)
    or a bound the failure count stays strictly below."""
    kind, target_param, mechanism = draw(_TARGET_KINDS)
    node_id = draw(_NODE_IDS)
    target_value = draw(_TARGET_TEXT)
    unbounded = draw(st.booleans())
    if unbounded:
        retry_limit = 0
        failures = draw(st.integers(min_value=0, max_value=15))
    else:
        retry_limit = draw(st.integers(min_value=1, max_value=10))
        failures = draw(st.integers(min_value=0, max_value=retry_limit - 1))
    failure_plan = draw(
        st.lists(st.booleans(), min_size=failures, max_size=failures)
    )
    return kind, target_param, mechanism, node_id, target_value, retry_limit, failure_plan


# Feature: trigger-activation-runtime, Property 12: Reconnect bounding, backoff, and failure marking
@settings(max_examples=100)
@given(_exhaustion_scenarios())
def test_bounded_retry_limit_exhausts_with_failed_health(scenario):
    """With retry_limit >= 1 and every attempt failing, the engine makes
    exactly retry_limit attempts with the 1 s-doubling-capped-60 s delay
    series, then marks health `failed` with a message naming the trigger
    node id and the topic (MQTT) / endpoint (OPC UA), and parks.

    **Validates: Requirements 8.1, 8.3**
    """
    (kind, target_param, mechanism, node_id, target_value,
     retry_limit, failure_plan) = scenario
    target = describe_trigger_target(kind, {target_param: target_value})
    assert target == target_value

    harness = _Harness(
        node_id, kind, target_param, mechanism, target,
        retry_limit, failure_plan, then_succeed=False,
    )
    harness.engine.on_connection_lost(ConnectionError("initial-drop"))

    # Attempt bound: exactly retry_limit attempts, never more (8.1).
    assert harness.attempt_calls == retry_limit
    assert harness.health.reconnect_attempts == retry_limit

    # Backoff series: one wait per attempt, 1 s doubling capped at 60 s.
    assert harness.recorded_delays == _expected_delays(retry_limit)

    # Health was `reconnecting` during every attempt of the loop (8.2).
    assert harness.states_seen_in_attempts == (
        [HEALTH_RECONNECTING] * retry_limit
    )

    # Exhaustion: `failed` health with the actionable message naming the
    # node id and the topic/endpoint, and the engine parked (8.3).
    assert harness.health.state == HEALTH_FAILED
    record = harness.health.to_wire()
    assert record["lastError"] is not None
    assert f"Trigger '{node_id}'" in record["lastError"]
    assert target_value in record["lastError"]
    assert f"failed after {retry_limit} reconnect attempts" in record["lastError"]
    assert harness.engine.parked

    # Parked engines refuse further reconnect loops.
    harness.engine.on_connection_lost(ConnectionError("late-drop"))
    assert harness.attempt_calls == retry_limit


# Feature: trigger-activation-runtime, Property 12: Reconnect bounding, backoff, and failure marking
@settings(max_examples=100)
@given(_success_scenarios())
def test_failures_then_success_restores_health_and_resets_attempts(scenario):
    """For any failure sequence ending in success — unbounded
    (retry_limit 0) or under a bound — the engine keeps attempting through
    the whole generated sequence with the capped doubling delay series,
    then restores health (`subscribed`/`polling` per the node's mechanism),
    resets the attempt count, and does not park.

    **Validates: Requirements 8.1, 8.3**
    """
    (kind, target_param, mechanism, node_id, target_value,
     retry_limit, failure_plan) = scenario
    target = describe_trigger_target(kind, {target_param: target_value})

    harness = _Harness(
        node_id, kind, target_param, mechanism, target,
        retry_limit, failure_plan, then_succeed=True,
    )
    harness.engine.on_connection_lost(ConnectionError("initial-drop"))

    failures = len(failure_plan)
    total_attempts = failures + 1  # every failure, then the success

    # Unbounded (0) keeps attempting through the whole sequence; bounded
    # succeeds before exhaustion — either way every generated failure got
    # its attempt plus the final successful one (8.1).
    assert harness.attempt_calls == total_attempts
    assert harness.recorded_delays == _expected_delays(total_attempts)
    assert harness.states_seen_in_attempts == (
        [HEALTH_RECONNECTING] * total_attempts
    )

    # Success: restored state per mechanism, attempts reset, not parked.
    expected_state = (
        HEALTH_POLLING if mechanism == MECHANISM_POLL else HEALTH_SUBSCRIBED
    )
    assert harness.health.state == expected_state
    assert harness.health.reconnect_attempts == 0
    assert not harness.engine.parked
    assert harness.health.to_wire()["lastError"] is None
