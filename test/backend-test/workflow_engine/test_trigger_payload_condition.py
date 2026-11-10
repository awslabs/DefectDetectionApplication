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
"""``mqtt_subscribe.payload_condition`` evaluated per delivery in
``WorkflowActivationCore.fire`` (bedrock-inspection-chain design 8.1).

The three-way split: (i) a mismatch, or a ``ConditionFieldError`` whose
field root is a key of the evaluation document (``trigger``) -> DEBUG
discard, health untouched, no latch, the error case counted toward the
10-consecutive backstop; (ii) a syntax error, or a ``ConditionFieldError``
with a foreign root (unquoted literal, wrong root) -> discard, one WARNING
per 300 s per gate, ``TriggerHealth.detail`` set; (iii) any other exception
-> discard, rate-limited WARNING, no health change. Fake clock, no threads
(the core is never started; accepted firings pile up in its queue).
"""
import json
import logging

import pytest

from workflow_engine.output_bindings import ConditionFieldError, evaluate_condition
from workflow_engine.trigger_runtime import (
    PAYLOAD_CONDITION_UNRESOLVABLE_STREAK,
    PAYLOAD_CONDITION_WARNING_INTERVAL_SECONDS,
    POLICY_DROP,
    POLICY_QUEUE,
    TriggerHealth,
    TriggerPolicy,
    WorkflowActivationCore,
    payload_condition_document,
)

LOGGER = "workflow_engine.trigger_runtime"
NODE = "trigger_1"
REGISTRATION = "reg-1"
TOPIC = "plant/line1/evaluate"
MATCH = "trigger.payload_json.body.qc_stage == 'qc2'"


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_core(condition, clock=None, policy=POLICY_QUEUE, **core_kwargs):
    parameters = {
        "concurrency_policy": policy, "queue_depth": 100,
        "payload_condition": condition,
    }
    core = WorkflowActivationCore(
        REGISTRATION,
        policies=[TriggerPolicy.from_parameters(NODE, parameters)],
        run_starter=lambda activation: None,
        clock=clock or FakeClock(),
        **core_kwargs
    )
    health = TriggerHealth(NODE, "mqtt_subscribe")
    core.attach_health(NODE, health)
    return core, health


def delivery(stage="qc2", payload=None):
    if payload is None:
        payload = json.dumps(
            {"body": {"qc_stage": stage, "order": {"items": [1, 2, 3]}}})
    return {"topic": TOPIC, "payload": payload, "qos": 1, "timestamp": 1.0}


def warnings_of(caplog):
    return [r for r in caplog.records
            if r.name == LOGGER and r.levelno == logging.WARNING]


@pytest.fixture
def debug_log(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        yield caplog



class TestPolicyAndDocument:
    def test_from_parameters_strips_the_rule_and_maps_blank_to_none(self):
        assert TriggerPolicy.from_parameters(NODE, {}).payload_condition is None
        assert TriggerPolicy.from_parameters(
            NODE, {"payload_condition": "   "}).payload_condition is None
        assert TriggerPolicy.from_parameters(
            NODE, {"payload_condition": "  " + MATCH + " \n"}
        ).payload_condition == MATCH

    def test_legacy_policy_dataclass_default_is_none(self):
        assert TriggerPolicy(trigger_node_id=NODE).payload_condition is None

    def test_evaluation_document_has_the_trigger_root_only(self):
        document = payload_condition_document(delivery("qc1"))
        assert list(document) == ["trigger"]
        assert document["trigger"]["topic"] == TOPIC
        assert document["trigger"]["payload_json"]["body"]["qc_stage"] == "qc1"
        assert payload_condition_document(
            delivery(payload="not json"))["trigger"]["payload_json"] is None
        assert payload_condition_document({"topic": TOPIC})["trigger"]["payload"] == ""

    def test_condition_field_error_is_a_value_error_with_the_same_message(self):
        with pytest.raises(ValueError) as info:
            evaluate_condition("missing == 1", {"a": 1})
        assert isinstance(info.value, ConditionFieldError)
        assert info.value.field == "missing"
        assert str(info.value) == "Unknown metadata field 'missing' in condition"
        with pytest.raises(ConditionFieldError) as info:
            evaluate_condition("a.b.c == 1", {"a": {"b": {}}})
        assert info.value.field == "a.b.c"


class TestPerMessageOutcomes:
    def test_match_fires_and_mismatch_is_discarded(self, debug_log):
        core, health = make_core(MATCH)
        assert core.fire(NODE, delivery("qc2")) is True
        assert core.queue.pending_count(NODE) == 1
        assert core.fire(NODE, delivery("qc1")) is False
        assert core.queue.pending_count(NODE) == 1
        assert warnings_of(debug_log) == []
        assert "detail" not in health.to_wire()

    def test_missing_field_and_non_json_are_debug_discards_without_a_latch(self, debug_log):
        core, health = make_core(MATCH)
        assert core.fire(NODE, delivery(payload=json.dumps({"body": {}}))) is False
        assert core.fire(NODE, delivery(payload="not json at all")) is False
        assert core.fire(NODE, delivery(payload="")) is False
        assert warnings_of(debug_log) == []
        assert health.to_wire() == TriggerHealth(NODE, "mqtt_subscribe").to_wire()
        assert health.detail is None
        assert core.gate(NODE).unresolvable_streak == 3
        # The NEXT matching delivery fires: nothing was remembered.
        assert core.fire(NODE, delivery("qc2")) is True
        assert core.queue.pending_count(NODE) == 1
        assert core.gate(NODE).unresolvable_streak == 0

    def test_no_condition_admits_every_delivery_as_before(self, debug_log):
        core, _ = make_core("")
        assert core.gate(NODE).policy.payload_condition is None
        for payload in ("not json", json.dumps({"body": {"qc_stage": "qc1"}})):
            assert core.fire(NODE, delivery(payload=payload)) is True
        assert core.queue.pending_count(NODE) == 2

    def test_alternating_stages_never_warn(self, debug_log):
        core, health = make_core(MATCH)
        accepted = [core.fire(NODE, delivery(["qc1", "qc2"][i % 2])) for i in range(40)]
        assert accepted == [False, True] * 20
        assert core.queue.pending_count(NODE) == 20
        assert warnings_of(debug_log) == []
        assert "detail" not in health.to_wire()



class TestDefinitionErrors:
    SYNTAX = "trigger.payload_json.body.qc_stage =="

    def test_syntax_error_discards_every_delivery_with_one_warning_and_a_detail(self, debug_log):
        clock = FakeClock()
        core, health = make_core(self.SYNTAX, clock=clock)
        assert [core.fire(NODE, delivery("qc2")) for _ in range(7)] == [False] * 7
        assert core.queue.pending_count(NODE) == 0
        records = warnings_of(debug_log)
        assert len(records) == 1
        message = records[0].getMessage()
        assert REGISTRATION in message and NODE in message
        assert "Unexpected end of condition" in message
        wire = health.to_wire()
        assert wire["detail"] == (
            "payload_condition definition error: Unexpected end of condition")
        assert wire["state"] == "connecting"  # the worker's field is untouched
        # A timestamp, not a latch: evaluated again, warned again after 300 s.
        clock.advance(PAYLOAD_CONDITION_WARNING_INTERVAL_SECONDS)
        assert core.fire(NODE, delivery("qc2")) is False
        assert len(warnings_of(debug_log)) == 2
        assert core.gate(NODE).unresolvable_streak == 0

    @pytest.mark.parametrize("rule, field", [
        ("trigger.payload_json.body.qc_stage == qc2", "qc2"),
        ("payload_json.body.qc_stage == 'qc2'", "payload_json.body.qc_stage"),
    ])
    def test_unquoted_literal_and_wrong_root_are_definition_errors_on_delivery_one(
            self, debug_log, rule, field):
        core, health = make_core(rule)
        assert core.fire(NODE, delivery("qc2")) is False
        records = warnings_of(debug_log)
        assert len(records) == 1
        assert repr(field) in records[0].getMessage()
        assert health.to_wire()["detail"] == (
            "payload_condition definition error: Unknown metadata field "
            "{0!r} in condition".format(field))
        assert core.gate(NODE).unresolvable_streak == 0
        # Still per-delivery: the second one is discarded at DEBUG.
        assert core.fire(NODE, delivery("qc2")) is False
        assert len(warnings_of(debug_log)) == 1

    def test_typo_below_the_root_hits_the_streak_backstop_without_a_detail(self, debug_log):
        clock = FakeClock()
        core, health = make_core(
            "trigger.payload_json.body.qc_stag == 'qc2'", clock=clock)
        for n in range(1, PAYLOAD_CONDITION_UNRESOLVABLE_STREAK):
            assert core.fire(NODE, delivery("qc2")) is False
            assert warnings_of(debug_log) == [], n
        assert core.fire(NODE, delivery("qc2")) is False  # the 10th
        records = warnings_of(debug_log)
        assert len(records) == 1
        message = records[0].getMessage()
        assert "discarded 10 consecutive deliveries as unresolvable" in message
        assert "check the rule's field paths" in message
        assert "Unknown metadata field 'trigger.payload_json.body.qc_stag'" in message
        for _ in range(11, 21):
            assert core.fire(NODE, delivery("qc2")) is False
        assert len(warnings_of(debug_log)) == 1
        clock.advance(PAYLOAD_CONDITION_WARNING_INTERVAL_SECONDS)
        assert core.fire(NODE, delivery("qc2")) is False
        assert len(warnings_of(debug_log)) == 2
        assert "discarded 21 consecutive" in warnings_of(debug_log)[1].getMessage()
        assert "detail" not in health.to_wire()
        assert health.detail is None

    def test_other_exceptions_warn_without_touching_health_or_the_streak(self, debug_log):
        core, health = make_core("trigger.payload_json.body.count > 'x'")
        payload = json.dumps({"body": {"count": 3}})
        assert core.fire(NODE, delivery(payload=payload)) is False
        assert core.fire(NODE, delivery(payload=payload)) is False
        records = warnings_of(debug_log)
        assert len(records) == 1
        assert "TypeError" in records[0].getMessage()
        assert "detail" not in health.to_wire()
        assert core.gate(NODE).unresolvable_streak == 0



class TestPolicyInterplay:
    def test_drop_policy_is_applied_after_the_condition(self, debug_log):
        core, _ = make_core(MATCH, policy=POLICY_DROP)
        assert core.fire(NODE, delivery("qc2")) is True
        # Pending activation: a second match is dropped by the policy, a
        # mismatch by the condition; neither touches health or warns.
        assert core.fire(NODE, delivery("qc2")) is False
        assert core.fire(NODE, delivery("qc1")) is False
        assert core.queue.pending_count(NODE) == 1
        core.queue.pop_nowait()
        assert core.fire(NODE, delivery("qc1")) is False
        assert core.fire(NODE, delivery("qc2")) is True
        assert warnings_of(debug_log) == []

    def test_drop_policy_without_a_condition_is_unchanged(self):
        core, _ = make_core("", policy=POLICY_DROP)
        assert core.fire(NODE, delivery("qc1")) is True
        assert core.fire(NODE, delivery("qc1")) is False

    def test_unknown_trigger_is_still_ignored_before_any_evaluation(self, debug_log):
        core, _ = make_core(MATCH)
        assert core.fire("ghost", delivery("qc2")) is False
        assert len(warnings_of(debug_log)) == 1

    def test_attach_health_for_an_unknown_node_is_a_no_op(self):
        core, _ = make_core(MATCH)
        core.attach_health("ghost", TriggerHealth("ghost", "mqtt_subscribe"))
        assert core.gate("ghost") is None


class TestHealthDetail:
    def test_detail_is_absent_until_set_and_clearable(self):
        health = TriggerHealth(NODE, "mqtt_subscribe")
        assert "detail" not in health.to_wire()
        health.set_detail("payload_condition definition error: x")
        assert health.to_wire()["detail"] == "payload_condition definition error: x"
        health.set_detail(None)
        assert "detail" not in health.to_wire()
        assert set(health.to_wire()) == {
            "nodeId", "state", "autoFallback", "reconnectAttempts", "lastError"}
