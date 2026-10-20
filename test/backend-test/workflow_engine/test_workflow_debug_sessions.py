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
"""The session state machine (workflow-debug-mode design §3, §5.1
``sessions.py``, §5.3; tasks.md 2.5) with a fake ``RegistrationView`` and a
fake clock.

Validation of every field including ``usecaseId`` and the prefix; version
ordering; expiry; removal when a key is absent; an unchanged entry is a no-op;
two sessions per device; ``active`` on the next tick with a ``registered``
row; ``no_registration`` after 30 s then ``active`` on the view's
notification; a row leaving ``registered`` moves an active session back and
forth; the restart path (new instance, ``seq`` 1, ``reason: resumed``).

Validates: debug R2.2, R5.1, R5.3, R5.6, R12.3.
"""
import uuid

import pytest

from workflow_engine.telemetry.debug import sessions as sessions_module
from workflow_engine.telemetry.debug.batcher import Batcher
from workflow_engine.telemetry.debug.sessions import (
    ACTIVE,
    EXPIRED,
    NO_REGISTRATION,
    PENDING,
    REJECTED,
    STOPPED,
    SessionManager,
    validate_entry,
)
from workflow_engine.telemetry.registry_view import RegistrationInfo

THING = "thing-1"
UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF_A = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
WF_B = "9b8a7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"
NOW_MS = 1_792_075_062_000


class FakeClock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = NOW_MS / 1000.0

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds


class FakeView:
    def __init__(self):
        self.rows = {}

    def snapshot(self):
        return dict(self.rows)

    def register(self, registration_id, workflow_id, version="1"):
        self.rows[registration_id] = RegistrationInfo(workflow_id=workflow_id, version=version,
                                                      models=(), has_triton=False)

    def unregister(self, registration_id):
        self.rows.pop(registration_id, None)


def entry(workflow_id=WF_A, session_id=SESSION_A, **overrides):
    values = {
        "sessionId": session_id, "usecaseId": UC, "enabledAt": NOW_MS - 60_000,
        "expiresAt": NOW_MS + 3_600_000, "level": "debug", "images": False, "imageMaxKb": 150,
        "bucket": "results-bucket", "region": "us-east-1",
        "prefix": "dda-telemetry/debug/%s/%s/%s/%s/" % (UC, THING, workflow_id, session_id),
        "maxMessagesPerSecond": 10, "maxBytesPerSecond": 262144,
    }
    values.update(overrides)
    for key in [k for k, v in values.items() if v is ...]:
        del values[key]
    return values


@pytest.fixture
def world():
    clock = FakeClock()
    view = FakeView()
    manager = SessionManager(THING, view, clock=lambda: clock.mono, wall=lambda: clock.wall,
                             agent_started_ms=NOW_MS)
    return manager, view, clock


def states(transitions):
    return [(t.session.workflow_id, t.previous, t.state, t.reason) for t in transitions]


class TestValidation:
    def test_a_valid_entry_is_normalized_with_defaults(self):
        raw = entry()
        del raw["imageMaxKb"]
        del raw["maxMessagesPerSecond"]
        del raw["maxBytesPerSecond"]
        normalized = validate_entry(WF_A, raw, THING, NOW_MS)
        assert isinstance(normalized, dict)
        assert normalized["imageMaxKb"] == 150
        assert normalized["maxMessagesPerSecond"] == 10
        assert normalized["maxBytesPerSecond"] == 262144
        assert normalized["level"] == "debug" and normalized["images"] is False

    @pytest.mark.parametrize("overrides, reason", [
        ({"sessionId": "not-a-uuid"}, "invalid_session_id"),
        ({"sessionId": None}, "invalid_session_id"),
        ({"usecaseId": "12345"}, "invalid_usecase_id"),
        ({"usecaseId": ...}, "invalid_usecase_id"),
        ({"enabledAt": NOW_MS + 6 * 60_000}, "enabled_at_invalid"),
        ({"enabledAt": "yesterday"}, "enabled_at_invalid"),
        ({"expiresAt": NOW_MS - 60_000}, "expires_at_out_of_range"),
        ({"expiresAt": NOW_MS - 60_000 + 25 * 3_600_000}, "expires_at_out_of_range"),
        ({"expiresAt": None}, "expires_at_out_of_range"),
        ({"level": "trace"}, "invalid_level"),
        ({"images": "yes"}, "invalid_images"),
        ({"imageMaxKb": 49}, "image_max_kb_out_of_range"),
        ({"imageMaxKb": 301}, "image_max_kb_out_of_range"),
        ({"maxMessagesPerSecond": 0}, "max_messages_per_second_out_of_range"),
        ({"maxMessagesPerSecond": 21}, "max_messages_per_second_out_of_range"),
        ({"maxBytesPerSecond": 32767}, "max_bytes_per_second_out_of_range"),
        ({"maxBytesPerSecond": 1048577}, "max_bytes_per_second_out_of_range"),
        ({"maxBytesPerSecond": "fast"}, "max_bytes_per_second_out_of_range"),
        ({"region": "US-EAST-1"}, "invalid_region"),
        ({"region": "us-east"}, "invalid_region"),
        ({"region": ..., "images": True}, "invalid_region"),
        ({"bucket": "", "images": True}, "invalid_bucket"),
        ({"prefix": "dda-telemetry/debug/%s/other-thing/%s/%s/" % (UC, WF_A, SESSION_A)}, "invalid_prefix"),
        ({"prefix": "dda-telemetry/debug/%s/%s/%s/%s" % (UC, THING, WF_A, SESSION_A)}, "invalid_prefix"),
        ({"prefix": ...}, "invalid_prefix"),
    ])
    def test_rejections(self, overrides, reason):
        assert validate_entry(WF_A, entry(**overrides), THING, NOW_MS) == reason

    def test_prefix_is_bound_to_the_usecase_thing_workflow_and_session(self):
        raw = entry(workflow_id=WF_B)  # the prefix names WF_B but the key is WF_A
        assert validate_entry(WF_A, raw, THING, NOW_MS) == "invalid_prefix"
        assert isinstance(validate_entry(WF_B, raw, THING, NOW_MS), dict)
        other_uc = dict(raw, usecaseId=SESSION_B)  # a UUID, but not the one in the prefix
        assert validate_entry(WF_B, other_uc, THING, NOW_MS) == "invalid_prefix"

    def test_region_and_bucket_are_optional_without_images(self):
        raw = entry()
        del raw["region"]
        del raw["bucket"]
        assert isinstance(validate_entry(WF_A, raw, THING, NOW_MS), dict)

    def test_gov_region_and_non_object(self):
        assert isinstance(validate_entry(WF_A, entry(region="us-gov-west-1"), THING, NOW_MS), dict)
        assert validate_entry(WF_A, "nope", THING, NOW_MS) == "not_an_object"

    def test_rejected_entry_is_reported_with_its_reason_once(self, world, caplog):
        manager, view, clock = world
        import logging

        with caplog.at_level(logging.WARNING, logger="workflow_engine.telemetry.debug.sessions"):
            transitions = manager.apply_desired({WF_A: entry(level="loud")}, 1)
            manager.apply_desired({WF_A: entry(level="loud")}, 2)  # unchanged invalid entry
        assert states(transitions) == [(WF_A, None, REJECTED, "invalid_level")]
        reported = manager.reported("inst", "1.0")["sessions"][WF_A]
        assert reported["state"] == "rejected" and reported["reason"] == "invalid_level"
        assert len([r for r in caplog.records if "rejected" in r.getMessage()]) == 1

    def test_unsupported_v_rejects_every_entry(self, world):
        manager, view, clock = world
        transitions = manager.apply_desired({WF_A: entry()}, 1, v=2)
        assert states(transitions) == [(WF_A, None, REJECTED, "unsupported_version")]


class TestLifecycle:
    def test_pending_then_active_on_the_next_tick_with_a_registered_row(self, world):
        manager, view, clock = world
        view.register("reg-a:3", WF_A, "3")
        transitions = manager.apply_desired({WF_A: entry()}, 1)
        # apply_desired evaluates at once: pending, then active in the same call.
        assert states(transitions) == [(WF_A, None, PENDING, "applied"), (WF_A, PENDING, ACTIVE, "registered")]
        session = manager.session_for_workflow(WF_A)
        assert session.registration_id == "reg-a:3" and session.workflow_version == "3"
        assert session.topic == "dda/debug/%s/%s/record" % (UC, WF_A)
        assert manager.tick() == []
        assert manager.first_record_reason(session) == "resumed"  # enabledAt before the agent start

    def test_started_when_enabled_after_the_agent_start(self, world):
        manager, view, clock = world
        manager.apply_desired({WF_A: entry(enabledAt=NOW_MS + 1000, expiresAt=NOW_MS + 3_600_000)}, 1)
        assert manager.first_record_reason(manager.session_for_workflow(WF_A)) == "started"

    def test_no_registration_after_30_s_then_active_when_the_row_appears(self, world):
        manager, view, clock = world
        transitions = manager.apply_desired({WF_A: entry()}, 1)
        assert states(transitions) == [(WF_A, None, PENDING, "applied")]
        clock.advance(29)
        assert manager.tick() == []
        clock.advance(1.5)
        assert states(manager.tick()) == [(WF_A, PENDING, NO_REGISTRATION, "no_registration")]
        assert manager.reported("i", "v")["sessions"][WF_A]["state"] == "no_registration"
        view.register("reg-a:1", WF_A)  # the view's notification: the agent ticks
        assert states(manager.tick()) == [(WF_A, NO_REGISTRATION, ACTIVE, "registered")]

    def test_a_row_leaving_registered_moves_the_session_back_and_forth(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A, "1")
        manager.apply_desired({WF_A: entry()}, 1)
        assert manager.session_for_workflow(WF_A).state == ACTIVE
        view.unregister("reg-a:1")
        assert states(manager.tick()) == [(WF_A, ACTIVE, NO_REGISTRATION, "registration_left")]
        view.register("reg-a:2", WF_A, "2")
        assert states(manager.tick()) == [(WF_A, NO_REGISTRATION, ACTIVE, "registered")]
        session = manager.session_for_workflow(WF_A)
        assert session.registration_id == "reg-a:2" and session.workflow_version == "2"

    def test_highest_version_row_wins(self, world):
        manager, view, clock = world
        view.register("reg-a:2", WF_A, "2")
        view.register("reg-a:10", WF_A, "10")
        manager.apply_desired({WF_A: entry()}, 1)
        assert manager.session_for_workflow(WF_A).registration_id == "reg-a:10"

    def test_expiry_is_checked_against_expires_at(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A)
        manager.apply_desired({WF_A: entry(expiresAt=NOW_MS + 10_000)}, 1)
        clock.advance(9.9)
        assert manager.tick() == []
        clock.advance(0.2)
        assert states(manager.tick()) == [(WF_A, ACTIVE, EXPIRED, "expired")]
        assert manager.tick() == []
        assert manager.status()[WF_A] == {"active": False, "expiresAt": None, "state": "expired",
                                          "sessionId": SESSION_A}

    def test_an_offline_device_applies_an_already_expired_entry_as_expired(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A)
        # enabledAt an hour ago, 50 minutes long: the device was offline past the end.
        transitions = manager.apply_desired(
            {WF_A: entry(enabledAt=NOW_MS - 3_600_000, expiresAt=NOW_MS - 600_000)}, 1)
        assert states(transitions) == [(WF_A, None, PENDING, "applied"), (WF_A, PENDING, EXPIRED, "expired")]

    def test_remaining_time_counts_from_enabled_at(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A)
        manager.apply_desired({WF_A: entry(enabledAt=NOW_MS - 3_000_000, expiresAt=NOW_MS + 600_000)}, 1)
        clock.advance(599)
        assert manager.tick() == []
        clock.advance(2)
        assert states(manager.tick()) == [(WF_A, ACTIVE, EXPIRED, "expired")]

    def test_absent_key_stops_the_session_and_it_is_forgotten_after_the_report(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A)
        manager.apply_desired({WF_A: entry()}, 1)
        assert states(manager.apply_desired({}, 2)) == [(WF_A, ACTIVE, STOPPED, "stopped")]
        reported = manager.reported("i", "v")
        assert reported["sessions"][WF_A]["state"] == "stopped"
        assert manager.apply_desired({}, 3) == []
        reported = manager.reported("i", "v")
        assert reported["sessions"] == {WF_A: None}  # the tombstone, once
        assert manager.reported("i", "v")["sessions"] == {}

    def test_null_entry_counts_as_absent(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A)
        manager.apply_desired({WF_A: entry()}, 1)
        assert states(manager.apply_desired({WF_A: None}, 2)) == [(WF_A, ACTIVE, STOPPED, "stopped")]

    def test_unchanged_entry_is_a_no_op(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A)
        manager.apply_desired({WF_A: entry()}, 1)
        assert manager.apply_desired({WF_A: entry()}, 2) == []
        # An unknown extra key is a change of the same session: updated in place, no transition.
        assert manager.apply_desired({WF_A: dict(entry(), extra="x")}, 3) == []
        assert manager.session_for_workflow(WF_A).entry["extra"] == "x"

    def test_changed_entry_of_the_same_session_updates_in_place(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A)
        manager.apply_desired({WF_A: entry()}, 1)
        session = manager.session_for_workflow(WF_A)
        session.batcher = Batcher(publisher=None, clock=lambda: clock.mono, topic=session.topic,
                                  max_messages_per_second=10, max_bytes_per_second=262144)
        transitions = manager.apply_desired(
            {WF_A: entry(expiresAt=NOW_MS + 7_200_000, maxMessagesPerSecond=2, maxBytesPerSecond=65536)}, 2)
        assert transitions == []
        assert manager.session_for_workflow(WF_A) is session
        assert session.expires_at_ms == NOW_MS + 7_200_000
        assert session.batcher.messages_bucket.rate == 2.0
        assert session.batcher.bytes_bucket.rate == 65536.0

    def test_a_different_session_id_replaces_the_session(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A)
        manager.apply_desired({WF_A: entry()}, 1)
        transitions = manager.apply_desired({WF_A: entry(session_id=SESSION_B)}, 2)
        assert states(transitions) == [
            (WF_A, ACTIVE, STOPPED, "replaced"), (WF_A, STOPPED, PENDING, "applied"),
            (WF_A, PENDING, ACTIVE, "registered"),
        ]
        assert manager.session_for_workflow(WF_A).session_id == SESSION_B

    def test_version_ordering(self, world):
        manager, view, clock = world
        manager.apply_desired({WF_A: entry()}, 5)
        assert manager.apply_desired({}, 4) == []  # older: ignored
        assert manager.apply_desired({}, 5) == []  # equal: ignored
        assert states(manager.apply_desired({}, 6)) == [(WF_A, PENDING, STOPPED, "stopped")]

    def test_two_sessions_per_device(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A)
        view.register("reg-b:1", WF_B)
        transitions = manager.apply_desired({WF_A: entry(), WF_B: entry(workflow_id=WF_B, session_id=SESSION_B)}, 1)
        assert sorted(t.session.workflow_id for t in transitions if t.state == ACTIVE) == sorted([WF_A, WF_B])
        assert len(manager.active_sessions()) == 2
        assert manager.any_debug_level_active() is True
        assert manager.session_for_registration("reg-b:1").session_id == SESSION_B
        assert manager.session_for_registration("reg-zzz") is None
        status = manager.status()
        assert status[WF_A]["active"] and status[WF_B]["active"]
        assert status[WF_A]["expiresAt"] == NOW_MS + 3_600_000

    def test_reported_document_shape(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A)
        manager.apply_desired({WF_A: entry()}, 1)
        document = manager.reported("inst-1", "1.2.3")
        assert document["v"] == 1 and document["agentVersion"] == "1.2.3" and document["instance"] == "inst-1"
        session_doc = document["sessions"][WF_A]
        assert session_doc["sessionId"] == SESSION_A and session_doc["state"] == "active"
        assert session_doc["appliedAt"] == NOW_MS
        assert set(session_doc["counters"]) == set(sessions_module.COUNTER_KEYS)
        assert "reason" not in session_doc

    def test_ended_session_is_not_revived_by_the_same_entry(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A)
        manager.apply_desired({WF_A: entry(expiresAt=NOW_MS + 1000)}, 1)
        clock.advance(2)
        manager.tick()
        assert manager.session_for_workflow(WF_A).state == EXPIRED
        assert manager.apply_desired({WF_A: entry(expiresAt=NOW_MS + 1000)}, 2) == []


class TestRestartPath:
    def test_a_new_manager_resumes_with_a_new_instance_and_seq_1(self, world):
        manager, view, clock = world
        view.register("reg-a:1", WF_A)
        manager.apply_desired({WF_A: entry()}, 7)
        first = manager.session_for_workflow(WF_A)
        first.batcher = Batcher(publisher=None, clock=lambda: clock.mono, topic=first.topic)
        first.batcher.seq = 42

        # LocalServer restarts: a new agent instance, the shadow re-read.
        clock.advance(120)
        restarted = SessionManager(THING, view, clock=lambda: clock.mono, wall=lambda: clock.wall,
                                   agent_started_ms=int(clock.wall * 1000))
        transitions = restarted.apply_desired({WF_A: entry()}, 7)  # the same shadow version
        assert [t.state for t in transitions] == [PENDING, ACTIVE]
        session = restarted.session_for_workflow(WF_A)
        assert restarted.first_record_reason(session) == "resumed"
        session.batcher = Batcher(publisher=None, clock=lambda: clock.mono, topic=session.topic)
        assert session.batcher.seq == 0  # the first message of the new instance carries seq 1
        new_instance, old_instance = str(uuid.uuid4()), str(uuid.uuid4())
        assert new_instance != old_instance
        assert restarted.reported(new_instance, "v")["instance"] == new_instance
