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
"""The ``dda-workflow-telemetry`` desired-state application
(workflow-telemetry-dashboards design §5.4, §15.4; tasks.md 1.4, decision
Q3): version ordering through the shadow agent, clamping with the WARNING
text, ``personClasses`` defaults, each lease validation rule, the
replacement of the ``live`` map so that a removed lease is gone, nine
entries -> the deterministic eight survivors and the WARNING, the
``reported`` document and its cadence through ``ShadowControlAgent.report``.

Validates: R4.3, R5.6, R5.7, R6.1, R15.3, R19.4.
"""
import json
import logging

import pytest

from workflow_engine.telemetry import desired_state as desired_module
from workflow_engine.telemetry.control_shadow import ShadowControlAgent
from workflow_engine.telemetry.desired_state import MAX_LIVE_ENTRIES, TelemetryDesiredState, validate_lease
from workflow_engine.telemetry.live_frames import Lease

THING = "thing-1"
UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
WF_B = "1b2c3d4e-5f6a-4b7c-9d0e-1f2a3b4c5d6e"
NOW_MS = 1_792_075_062_000


def prefix(workflow_id=WF, usecase_id=UC, thing=THING):
    return "dda-telemetry/live/%s/%s/%s/" % (usecase_id, thing, workflow_id)


def live_entry(workflow_id=WF, **overrides):
    entry = {"expiresAt": NOW_MS + 60_000, "maxFps": 1, "maxWidth": 1280, "maxHeight": 720, "maxBytes": 307200,
             "bucket": "results-bucket", "region": "eu-west-1", "prefix": prefix(workflow_id)}
    entry.update(overrides)
    return entry


def document(**overrides):
    doc = {"v": 1, "usecaseId": UC, "enabled": True, "summaryIntervalS": 10, "idleHeartbeatS": 60,
           "personClasses": ["person", "human"], "live": {}, "updatedAt": NOW_MS - 1000}
    doc.update(overrides)
    return doc


@pytest.fixture
def state():
    changes = []
    s = TelemetryDesiredState(THING, wall=lambda: NOW_MS / 1000.0, on_change=changes.append)
    s.test_changes = changes
    return s


class TestDocumentGate:
    def test_a_complete_document_is_applied(self, state):
        assert state.apply(document(), 7) is True
        assert state.enabled is True and state.usecase_id == UC and state.applied_version == 7
        assert state.summary_interval_s == 10 and state.idle_heartbeat_s == 60
        assert state.person_classes == ("person", "human")
        assert state.active is True
        settings = state.summarizer_settings()
        assert settings.enabled is True and settings.summary_interval_s == 10 and settings.idle_heartbeat_s == 60
        assert settings.person_classes == ("person", "human")
        assert state.test_changes == [state]

    def test_unsupported_v_and_non_uuid_usecase_are_ignored_with_one_warning_per_version(self, state, caplog):
        caplog.set_level(logging.WARNING, logger="workflow_engine.telemetry.desired_state")
        assert state.apply(document(v=2), 1) is False
        assert state.apply(document(v=2), 1) is False
        assert state.apply(document(usecaseId="not-a-uuid"), 2) is False
        assert state.apply(document(usecaseId=None), 3) is False
        assert state.apply("not a dict", 4) is False
        assert state.enabled is False and state.usecase_id is None and state.active is False
        assert state.applied_version is None and state.ignored_documents == 5 and state.test_changes == []
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 4  # version 1 warned once
        assert any("unsupported document version 2" in w for w in warnings)
        assert any("usecaseId is not a UUID" in w for w in warnings)

    def test_an_ignored_document_keeps_the_last_applied_state(self, state):
        assert state.apply(document(), 1) is True
        assert state.apply(document(v=3, enabled=False), 2) is False
        assert state.enabled is True and state.applied_version == 1

    def test_enabled_defaults_to_false_and_non_booleans_are_false(self, state):
        doc = document()
        del doc["enabled"]
        state.apply(doc, 1)
        assert state.enabled is False and state.active is False
        assert state.summarizer_settings().enabled is False
        state.apply(document(enabled="true"), 2)
        assert state.enabled is False

    def test_the_shadow_agent_delivers_documents_in_version_order(self, state):
        """Through the real ``ShadowControlAgent``: a lower version is ignored."""
        shadow = ShadowControlAgent(THING, "dda-workflow-telemetry", state.apply, publisher=None,
                                    subscription_factory=lambda *a, **k: None)
        topic = shadow.topic_prefix
        shadow.handle_message(topic + "get/accepted", json.dumps({"state": {"desired": document(summaryIntervalS=20)},
                                                                  "version": 5}))
        assert state.summary_interval_s == 20 and state.applied_version == 5
        shadow.handle_message(topic + "update/documents", json.dumps(
            {"current": {"state": {"desired": document(summaryIntervalS=30)}, "version": 4}}))
        assert state.summary_interval_s == 20  # version 4 < 5: ignored
        shadow.handle_message(topic + "update/documents", json.dumps(
            {"current": {"state": {"desired": document(summaryIntervalS=30)}, "version": 6}}))
        assert state.summary_interval_s == 30 and state.applied_version == 6


class TestClampingAndPersonClasses:
    def test_intervals_are_clamped_with_a_warning_naming_the_value(self, state, caplog):
        caplog.set_level(logging.WARNING, logger="workflow_engine.telemetry.desired_state")
        state.apply(document(summaryIntervalS=2, idleHeartbeatS=900), 1)
        assert state.summary_interval_s == 5 and state.idle_heartbeat_s == 300
        messages = [r.getMessage() for r in caplog.records]
        assert any("summaryIntervalS 2 is outside 5-60; clamped to 5" in m for m in messages)
        assert any("idleHeartbeatS 900 is outside 30-300; clamped to 300" in m for m in messages)
        caplog.clear()
        state.apply(document(summaryIntervalS=120, idleHeartbeatS=10), 2)
        assert state.summary_interval_s == 60 and state.idle_heartbeat_s == 30
        messages = [r.getMessage() for r in caplog.records]
        assert any("summaryIntervalS 120 is outside 5-60; clamped to 60" in m for m in messages)
        assert any("idleHeartbeatS 10 is outside 30-300; clamped to 30" in m for m in messages)

    def test_in_range_and_missing_intervals(self, state, caplog):
        caplog.set_level(logging.WARNING, logger="workflow_engine.telemetry.desired_state")
        state.apply(document(summaryIntervalS=60, idleHeartbeatS=30), 1)
        assert state.summary_interval_s == 60 and state.idle_heartbeat_s == 30
        doc = document()
        del doc["summaryIntervalS"]
        del doc["idleHeartbeatS"]
        state.apply(doc, 2)
        assert state.summary_interval_s == 10 and state.idle_heartbeat_s == 60
        assert caplog.records == []
        state.apply(document(summaryIntervalS="ten", idleHeartbeatS=45.0), 3)
        assert state.summary_interval_s == 10 and state.idle_heartbeat_s == 45
        assert any("summaryIntervalS 'ten' is not an integer; using 10" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("value", [None, [], "person", ["a"] * 17, ["x" * 65], [1, 2], ["ok", ""]])
    def test_person_classes_default_when_absent_or_invalid(self, state, value, caplog):
        caplog.set_level(logging.WARNING, logger="workflow_engine.telemetry.desired_state")
        doc = document()
        if value is None:
            del doc["personClasses"]
        else:
            doc["personClasses"] = value
        state.apply(doc, 1)
        assert state.person_classes == ("person", "human")
        if value is not None:
            assert any("personClasses is invalid" in r.getMessage() for r in caplog.records)
        else:
            assert caplog.records == []

    def test_valid_person_classes_are_kept(self, state):
        labels = ["worker-%02d" % i for i in range(16)]
        state.apply(document(personClasses=labels), 1)
        assert state.person_classes == tuple(labels)
        assert state.summarizer_settings().person_classes == tuple(labels)


class TestLeases:
    def test_a_valid_entry_becomes_a_lease(self, state):
        state.apply(document(live={WF: live_entry()}), 1)
        assert set(state.leases) == {WF}
        lease = state.leases[WF]
        assert isinstance(lease, Lease)
        assert lease.expires_at_ms == NOW_MS + 60_000 and lease.max_fps == 1 and lease.max_width == 1280
        assert lease.max_height == 720 and lease.max_bytes == 307200 and lease.bucket == "results-bucket"
        assert lease.region == "eu-west-1" and lease.prefix == prefix()
        assert state.current_leases() == {WF: lease}

    @pytest.mark.parametrize("overrides, reason", [
        ({"expiresAt": NOW_MS}, "expires_at_not_in_future"),
        ({"expiresAt": NOW_MS - 1}, "expires_at_not_in_future"),
        ({"expiresAt": "soon"}, "expires_at_not_in_future"),
        ({"maxFps": 3}, "max_fps_out_of_range"),
        ({"maxFps": 0}, "max_fps_out_of_range"),
        ({"maxFps": "fast"}, "max_fps_out_of_range"),
        ({"maxWidth": 1281}, "max_width_out_of_range"),
        ({"maxWidth": 0}, "max_width_out_of_range"),
        ({"maxHeight": 721}, "max_height_out_of_range"),
        ({"maxBytes": 307201}, "max_bytes_out_of_range"),
        ({"maxBytes": 0}, "max_bytes_out_of_range"),
        ({"bucket": ""}, "invalid_bucket"),
        ({"bucket": "b" * 64}, "invalid_bucket"),
        ({"bucket": None}, "invalid_bucket"),
        ({"region": "EU-WEST-1"}, "invalid_region"),
        ({"region": "us-east"}, "invalid_region"),
        ({"region": "eu-west-1a"}, "invalid_region"),
        ({"region": None}, "invalid_region"),
        ({"prefix": prefix(thing="other-thing")}, "invalid_prefix"),
        ({"prefix": prefix(usecase_id=WF_B)}, "invalid_prefix"),
        ({"prefix": prefix()[:-1]}, "invalid_prefix"),
        ({"prefix": None}, "invalid_prefix"),
    ])
    def test_each_validation_rule_ignores_the_entry_with_a_warning(self, state, overrides, reason, caplog):
        caplog.set_level(logging.WARNING, logger="workflow_engine.telemetry.desired_state")
        assert validate_lease(WF, live_entry(**overrides), UC, THING, NOW_MS) == reason
        state.apply(document(live={WF: live_entry(**overrides)}), 1)
        assert state.leases == {} and state.rejected_live == {WF: reason}
        assert any("live entry for workflow %s ignored: %s" % (WF, reason) in r.getMessage()
                   for r in caplog.records)

    def test_region_grammar_accepts_the_partitions(self):
        for region in ("us-east-1", "eu-west-3", "ap-southeast-2", "us-gov-west-1", "cn-north-1"):
            assert isinstance(validate_lease(WF, live_entry(region=region), UC, THING, NOW_MS), Lease), region

    def test_absent_size_and_fps_fields_take_the_lease_defaults(self):
        entry = {"expiresAt": NOW_MS + 1000, "bucket": "b", "region": "us-east-1", "prefix": prefix()}
        lease = validate_lease(WF, entry, UC, THING, NOW_MS)
        assert (lease.max_fps, lease.max_width, lease.max_height, lease.max_bytes) == (1, 1280, 720, 307200)
        lease = validate_lease(WF, dict(entry, maxFps=0.5, maxWidth=640, maxHeight=360, maxBytes=100000), UC, THING,
                               NOW_MS)
        assert (lease.max_fps, lease.max_width, lease.max_height, lease.max_bytes) == (0.5, 640, 360, 100000)

    def test_not_an_object_and_null_entries(self, state):
        state.apply(document(live={WF: "soon", WF_B: None, "": live_entry()}), 1)
        assert state.leases == {} and state.rejected_live == {WF: "not_an_object"}
        state.apply(document(live="not a map"), 2)
        assert state.leases == {}

    def test_the_live_map_is_replaced_so_a_removed_lease_is_gone(self, state):
        state.apply(document(live={WF: live_entry(), WF_B: live_entry(WF_B)}), 1)
        assert set(state.leases) == {WF, WF_B}
        state.apply(document(live={WF_B: live_entry(WF_B)}), 2)  # WF removed (the Portal wrote null)
        assert set(state.leases) == {WF_B}
        state.apply(document(live={WF_B: None}), 3)
        assert state.leases == {}
        doc = document()
        del doc["live"]
        state.apply(doc, 4)
        assert state.leases == {}

    def test_leases_are_withheld_while_the_agent_is_inactive(self, state):
        state.apply(document(enabled=False, live={WF: live_entry()}), 1)
        assert set(state.leases) == {WF} and state.current_leases() == {}

    def test_nine_entries_keep_the_eight_latest_with_ties_by_workflow_id(self, state, caplog):
        caplog.set_level(logging.WARNING, logger="workflow_engine.telemetry.desired_state")
        ids = ["%08x-0000-4000-8000-0a0b0c0d0e0f" % i for i in range(9)]
        live = {}
        for index, workflow_id in enumerate(ids):
            # ids 0..6 expire later and later; ids 7 and 8 share the earliest expiry
            expires = NOW_MS + 100_000 + index * 1000 if index < 7 else NOW_MS + 50_000
            live[workflow_id] = live_entry(workflow_id, expiresAt=expires)
        state.apply(document(live=live), 9)
        assert len(state.leases) == MAX_LIVE_ENTRIES == 8
        # the tie between ids 7 and 8 is broken by the workflow id: the smaller one survives
        assert ids[8] not in state.leases and ids[7] in state.leases
        assert state.dropped_live == [ids[8]]
        warnings = [r.getMessage() for r in caplog.records]
        assert len(warnings) == 1
        assert "9 live entries exceed the cap of 8" in warnings[0] and ids[8] in warnings[0]
        reported = state.reported(agent_version="1.0.0", counters={}, last_publish_at=None)
        assert reported["live"]["dropped"] == [ids[8]]
        assert set(reported["live"]) - {"dropped"} == set(state.leases)
        # an invalid ninth entry does not count towards the cap
        live[ids[8]] = live_entry(ids[8], maxFps=9)
        state.apply(document(live=live), 10)
        assert len(state.leases) == 8 and state.dropped_live == [] and state.rejected_live == {ids[8]: "max_fps_out_of_range"}

    def test_the_survivors_are_the_latest_expiries(self, state):
        ids = ["%08x-0000-4000-8000-0a0b0c0d0e0f" % i for i in range(9)]
        live = {wf: live_entry(wf, expiresAt=NOW_MS + 10_000 * (9 - i)) for i, wf in enumerate(ids)}
        state.apply(document(live=live), 1)
        assert set(state.leases) == set(ids[:8]) and state.dropped_live == [ids[8]]


class TestReported:
    def test_reported_document_shape(self, state):
        state.apply(document(live={WF: live_entry()}), 3)
        reported = state.reported(agent_version="1.0.0",
                                 counters={"published": 12, "queueDrops": 1, "publishFailures": 2},
                                 last_publish_at=NOW_MS,
                                 live_status={WF: {"activeUntil": NOW_MS + 60_000, "lastSeq": 4, "framesTooLarge": 1}})
        assert reported == {
            "v": 1, "agentVersion": "1.0.0", "usecaseId": UC, "enabled": True, "summaryIntervalS": 10,
            "idleHeartbeatS": 60, "personClasses": ["person", "human"], "appliedVersion": 3,
            "lastPublishAt": NOW_MS, "counters": {"published": 12, "queueDrops": 1, "publishFailures": 2},
            "live": {WF: {"activeUntil": NOW_MS + 60_000, "lastSeq": 4, "framesTooLarge": 1}},
        }
        assert len(json.dumps(reported).encode("utf-8")) < 4096

    def test_reported_before_any_document(self, state):
        reported = state.reported(agent_version="1.0.0", counters={}, last_publish_at=None)
        assert reported["enabled"] is False and reported["usecaseId"] is None and reported["appliedVersion"] is None
        assert reported["counters"] == {"published": 0, "queueDrops": 0, "publishFailures": 0}
        assert reported["live"] == {}

    def test_reported_live_entry_without_sender_status(self, state):
        state.apply(document(live={WF: live_entry()}), 1)
        reported = state.reported(agent_version="1.0.0", counters={}, last_publish_at=None)
        assert reported["live"] == {WF: {"activeUntil": NOW_MS + 60_000, "lastSeq": None, "framesTooLarge": 0}}

    def test_cadence_through_the_shadow_agent(self, state):
        """A write on a non-counter change, else at most once per 5 minutes;
        counters, ``lastPublishAt``, ``lastSeq`` and ``framesTooLarge`` ride along."""
        clock = {"now": 1000.0}
        writes = []

        class Publisher:
            def publish_message(self, topic, payload):
                writes.append((topic, json.loads(payload)))

        shadow = ShadowControlAgent(THING, "dda-workflow-telemetry", state.apply, Publisher(),
                                    subscription_factory=lambda *a, **k: None, clock=lambda: clock["now"],
                                    counter_keys=("counters", "lastPublishAt", "lastSeq", "framesTooLarge"))

        def report(published, last_publish_at, last_seq=None, frames_too_large=0):
            return shadow.report(state.reported(
                agent_version="1.0.0", counters={"published": published, "queueDrops": 0, "publishFailures": 0},
                last_publish_at=last_publish_at,
                live_status={WF: {"activeUntil": NOW_MS + 60_000, "lastSeq": last_seq,
                                  "framesTooLarge": frames_too_large}}))

        state.apply(document(), 1)
        assert report(0, None) is True  # the first write
        assert report(1, NOW_MS) is False  # only counters changed
        clock["now"] += 299
        assert report(2, NOW_MS + 10_000) is False
        clock["now"] += 2
        assert report(3, NOW_MS + 20_000) is True  # 5 minutes: the periodic write with the counters
        assert writes[-1][1]["state"]["reported"]["counters"]["published"] == 3
        state.apply(document(live={WF: live_entry()}), 2)  # a lease becoming active: a non-counter change
        assert report(3, NOW_MS + 20_000, last_seq=None) is True
        assert report(4, NOW_MS + 21_000, last_seq=0) is False  # lastSeq moves: a counter
        assert report(5, NOW_MS + 22_000, last_seq=1, frames_too_large=1) is False
        state.apply(document(live={}), 3)  # the lease ends: a non-counter change
        assert report(5, NOW_MS + 22_000) is True
        assert writes[-1][0].endswith("/shadow/name/dda-workflow-telemetry/update")
        assert writes[-1][1]["state"]["reported"]["live"] == {}
        assert writes[-1][1]["state"]["reported"]["appliedVersion"] == 3

    def test_status_shape(self, state):
        state.apply(document(live={WF: live_entry(), WF_B: live_entry(WF_B, maxFps=5)}), 2)
        status = state.status()
        assert status["enabled"] is True and status["usecaseId"] == UC and status["appliedVersion"] == 2
        assert status["leases"] == [WF] and status["rejectedLive"] == {WF_B: "max_fps_out_of_range"}
        assert status["appliedDocuments"] == 1 and status["ignoredDocuments"] == 0 and status["droppedLive"] == []

    def test_on_change_failures_are_contained(self):
        def boom(state):
            raise RuntimeError("handler boom")

        s = TelemetryDesiredState(THING, wall=lambda: NOW_MS / 1000.0, on_change=boom)
        assert s.apply(document(), 1) is True and s.enabled is True

    def test_module_defaults(self):
        assert desired_module.DEFAULTS == {"enabled": False, "summaryIntervalS": 10, "idleHeartbeatS": 60,
                                           "personClasses": ["person", "human"]}
