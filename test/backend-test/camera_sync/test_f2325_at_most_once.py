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
"""The Edge_Sync_Agent applies each Portal change at most once, retries a
failed clear, and catches up on activation (rtsp-rtmp-stream-cameras task
30.3, finding 23; Requirement 5.13; design component 12).

- A redelivered create or delete, through a delta or the activation
  catch-up, is not applied again: one camera and one ack, no "doesn't exist".
  A change without a ``portalChangeId`` is applied on every delivery, as
  before.
- A failed clear is retried from ``pump()``, 1 s after the failure and then
  1, 2, 4 ... 30 s apart, reading the shadow before it writes: a ``None`` GET
  keeps every entry, ``False`` drops them, a GET or UPDATE that raises is a
  failed retry, a newer change in the shadow is left in place, and a newer
  pending clear added during a retry survives its completion.
- The record forgets the oldest change past ``PROCESSED_CHANGES_CAP``.
- The stale out-of-order case of the design's Edge cases is pinned.
- With the real worker thread, the catch-up gets a report written within 1 s
  during a 60 s backoff, and a failing retry never stops the worker.
- The new log lines carry no change payload (N3).

The agent runs over the real ``ImageSourceAccessor`` and a private sqlite
database (``f2122_stream_agent_support.make_stream_world``), and a scripted
shadow with AWS IoT merge semantics (``f2325_agent_support.ScriptedShadow``).
"""
import time

import pytest

from f2122_stream_agent_support import (
    PASSWORD,
    REF2,
    SECRET_ARN,
    URL,
    apply,
    assert_secret_free,
    capture_logs,
    delete_change,
    denied,
    granted,
    log_texts,
    make_stream_world,
    reference,
    rtsp_create,
    rtsp_update,
)
from f2325_agent_support import (
    IdlePinWorker,
    ScriptedShadow,
    acked_cameras,
    null_writes,
    reported_failures,
)

from camera_sync import agent as agent_module
from camera_sync.agent import PROCESSED_CHANGES_CAP, REASON_DISCOVERY_MANAGED

_AGENT_LOGGER = "camera_sync.agent"


@pytest.fixture
def world(tmp_path, monkeypatch):
    world = make_stream_world(tmp_path, monkeypatch, shadow=ScriptedShadow())
    yield world
    world.close()


def _deliver(agent, shadow):
    """A delta carrying every ``desired.changes`` entry, then one pump."""
    agent.on_delta(shadow.delta())
    return agent.pump()


def _names(world):
    return sorted(name for _, name in world.image_sources().values())


def _messages(caplog, fragment):
    return [record.getMessage() for record in caplog.records
            if record.name == _AGENT_LOGGER and fragment in record.getMessage()]


def _skip_line(pcid, csid):
    return ("Portal change {} for {} was already processed; clearing its desired entry "
            "without applying it again".format(pcid, csid))


def _creates(world):
    return [call for call in world.accessor.calls if call[0] == "create"]


# --- at most once -------------------------------------------------------------------------


def test_a_redelivered_create_makes_one_camera_and_one_ack(world, caplog):
    capture_logs(caplog)
    shadow, agent = world.shadow, world.make_agent()
    shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None, name="Dock A")})
    shadow.fail_desired = 1
    _deliver(agent, shadow)
    assert _names(world) == ["Dock A"] and shadow.failed, "setup: the clear did not fail"

    shadow.portal_writes({"portal-b": rtsp_create("pc-2", ref=None, name="Dock B")})
    _deliver(agent, shadow)  # carries portal-a (pc-1) again, and portal-b (pc-2)

    assert _names(world) == ["Dock A", "Dock B"]
    assert len(_creates(world)) == 2
    assert len(acked_cameras(shadow, "pc-1")) == 1 and len(acked_cameras(shadow, "pc-2")) == 1
    assert _messages(caplog, "already processed") == [_skip_line("pc-1", "portal-a")]
    assert shadow.desired_changes() == {}, "the skipped change's entry was not cleared"
    assert null_writes(shadow) == [{"changes": {"portal-a": None, "portal-b": None}}]
    assert agent._pending_clears == {}


def test_a_redelivered_delete_is_applied_once_and_reports_no_failure(world):
    shadow, agent = world.shadow, world.make_agent()
    shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None)})
    _deliver(agent, shadow)
    [image_source_id] = world.image_sources()
    cfg = "cfg-" + image_source_id
    shadow.portal_writes({cfg: delete_change("pc-3")})
    shadow.fail_desired = 1
    _deliver(agent, shadow)
    assert world.image_sources() == {} and shadow.failed, "setup: the delete's clear did not fail"

    _deliver(agent, shadow)  # the delta carries the delete again

    assert [call for call in world.accessor.calls if call[0] == "delete"] == [("delete", image_source_id)]
    assert reported_failures(shadow) == [], "the redelivered delete was reported as failed"
    assert shadow.desired_changes() == {}


def test_the_activation_catch_up_skips_a_processed_change_and_applies_a_new_one(world, caplog):
    capture_logs(caplog)
    shadow, agent = world.shadow, world.make_agent()
    shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None, name="Dock A")})
    shadow.fail_desired = 1
    _deliver(agent, shadow)
    # Written while the subscription was down: no delta carries it.
    shadow.portal_writes({"portal-b": rtsp_create("pc-2", ref=None, name="Dock B")})

    assert agent.on_subscription_active() is True
    agent.pump()

    assert _names(world) == ["Dock A", "Dock B"] and len(_creates(world)) == 2
    assert len(acked_cameras(shadow, "pc-1")) == 1 and len(acked_cameras(shadow, "pc-2")) == 1
    assert _messages(caplog, "already processed") == [_skip_line("pc-1", "portal-a")]
    assert null_writes(shadow)[-1] == {"changes": {"portal-a": None, "portal-b": None}}
    assert shadow.desired_changes() == {} and agent._pending_clears == {}


def test_the_catch_up_on_an_unreadable_shadow_returns_false_and_applies_nothing(world, caplog):
    capture_logs(caplog)
    shadow, agent = world.shadow, world.make_agent()
    shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None)})
    shadow.get_script = [None]

    assert agent.on_subscription_active() is False
    assert world.image_sources() == {}
    assert _messages(caplog, "after subscribing") == [
        "Could not read the camera-registry shadow after subscribing; retrying"]

    assert agent.on_subscription_active() is True
    assert len(world.image_sources()) == 1


def test_a_change_without_a_portal_change_id_is_applied_on_every_delivery(world):
    shadow, agent = world.shadow, world.make_agent()
    change = rtsp_create(None, ref=None, name="Dock A")
    del change["portalChangeId"]
    shadow.portal_writes({"portal-a": change})
    shadow.fail_desired = 1
    _deliver(agent, shadow)
    assert agent._pending_clears == {"portal-a": change}, "the identity of an id-less change is the change"

    _deliver(agent, shadow)

    assert _names(world) == ["Dock A", "Dock A"], "an id-less change is applied on every delivery"
    assert agent._processed_changes == {}
    assert agent._pending_clears == {} and shadow.desired_changes() == {}


# --- the clear retry, on a fake clock ----------------------------------------------------------


def test_a_failed_clear_is_retried_until_it_lands(world, caplog):
    capture_logs(caplog)
    shadow, agent, clock = world.shadow, world.make_agent(), world.clock
    shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None)})
    shadow.fail_desired = 3  # the first clear and the first two retries
    agent.on_delta(shadow.delta())
    assert agent.pump() == pytest.approx(1.0), "the first retry is 1 s after the failure"

    delays = []
    for _ in range(6):
        if "portal-a" not in shadow.desired_changes():
            break
        clock.now += 1.0
        delays.append(agent.pump())

    assert "portal-a" not in shadow.desired_changes(), "the failed clear never landed"
    assert delays == [pytest.approx(1.0), pytest.approx(2.0), pytest.approx(1.0), None]
    assert _messages(caplog, "applied desired change(s)") == [
        "Could not clear 1 applied desired change(s) from the camera-registry shadow (retry 1): "
        "TimeoutError: the desired-entry clear timed out; retrying in 1 s",
        "Could not clear 1 applied desired change(s) from the camera-registry shadow (retry 2): "
        "TimeoutError: the desired-entry clear timed out; retrying in 2 s",
        "Cleared 1 applied desired change(s) from the camera-registry shadow on retry 3",
    ]
    assert null_writes(shadow) == [{"changes": {"portal-a": None}}]
    assert len(world.image_sources()) == 1 and agent._pending_clears == {}


def test_an_unreadable_shadow_keeps_every_entry_and_backs_off_up_to_30_s(world, caplog):
    capture_logs(caplog)
    shadow, agent, clock = world.shadow, world.make_agent(), world.clock
    shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None, name="Dock A"),
                          "portal-b": rtsp_create("pc-2", ref=None, name="Dock B")})
    shadow.fail_desired = 1
    agent.on_delta(shadow.delta())
    delay = agent.pump()
    shadow.get_script = [None] * 8

    delays = []
    for _ in range(8):
        clock.now += delay
        delay = agent.pump()
        delays.append(delay)
        assert agent._pending_clears == {"portal-a": "pc-1", "portal-b": "pc-2"}

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    assert null_writes(shadow) == [], "a retry wrote without reading the shadow first"
    warnings = _messages(caplog, "change(s) from the camera-registry shadow (retry")
    assert len(warnings) == 8 and warnings[0] == (
        "Could not clear 2 applied desired change(s) from the camera-registry shadow (retry 1): "
        "the shadow could not be read; retrying in 1 s")

    clock.now += delay
    assert agent.pump() is None
    assert shadow.desired_changes() == {} and agent._pending_clears == {}
    assert _messages(caplog, "Cleared") == [
        "Cleared 2 applied desired change(s) from the camera-registry shadow on retry 9"]


def test_no_shadow_drops_the_pending_clears(world, caplog):
    capture_logs(caplog)
    shadow, agent, clock = world.shadow, world.make_agent(), world.clock
    shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None)})
    shadow.fail_desired = 1
    agent.on_delta(shadow.delta())
    agent.pump()
    shadow.get_script = [False]

    clock.now += 1.0
    assert agent.pump() is None

    assert agent._pending_clears == {}
    assert null_writes(shadow) == [] and _messages(caplog, "applied desired change(s)") == []
    clock.now += 60.0
    gets = shadow.gets
    assert agent.pump() is None and shadow.gets == gets, "a dropped clear was retried"


def test_a_get_or_an_update_that_raises_is_a_failed_retry(world, caplog):
    capture_logs(caplog)
    shadow, agent, clock = world.shadow, world.make_agent(), world.clock
    shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None)})
    shadow.fail_desired = 1
    agent.on_delta(shadow.delta())
    agent.pump()

    shadow.get_script = [ConnectionError("shadow offline")]
    clock.now += 1.0
    assert agent.pump() == pytest.approx(1.0)
    assert agent._pending_clears == {"portal-a": "pc-1"}

    shadow.fail_desired = 1
    clock.now += 1.0
    assert agent.pump() == pytest.approx(2.0)
    assert agent._pending_clears == {"portal-a": "pc-1"}

    clock.now += 2.0
    assert agent.pump() is None
    assert shadow.desired_changes() == {} and agent._pending_clears == {}
    assert _messages(caplog, "applied desired change(s)") == [
        "Could not clear 1 applied desired change(s) from the camera-registry shadow (retry 1): "
        "ConnectionError: shadow offline; retrying in 1 s",
        "Could not clear 1 applied desired change(s) from the camera-registry shadow (retry 2): "
        "TimeoutError: the desired-entry clear timed out; retrying in 2 s",
        "Cleared 1 applied desired change(s) from the camera-registry shadow on retry 3",
    ]


def test_a_newer_change_in_the_shadow_is_left_in_place(world):
    shadow, agent, clock = world.shadow, world.make_agent(), world.clock
    shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None, name="Dock A")})
    shadow.fail_desired = 1
    agent.on_delta(shadow.delta())
    agent.pump()
    # The Portal writes a newer change for the id before the retry; its delta
    # has not arrived yet.
    shadow.portal_writes({"portal-a": rtsp_update("pc-2", ref=None, name="Dock A2")})

    clock.now += 1.0
    assert agent.pump() is None

    assert shadow.desired_changes()["portal-a"]["portalChangeId"] == "pc-2", (
        "the retried clear nulled the newer change")
    assert null_writes(shadow) == [] and agent._pending_clears == {}


def test_a_newer_pending_clear_added_during_a_retry_survives_its_completion(world):
    shadow, agent, clock = world.shadow, world.make_agent(), world.clock
    shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None, name="Dock A")})
    shadow.fail_desired = 1
    agent.on_delta(shadow.delta())
    agent.pump()

    def newer_change_whose_clear_fails():
        # Inside the retry's GET: a newer change for the id is delivered, and
        # its own first clear fails.
        shadow.portal_writes({"portal-a": rtsp_update("pc-2", ref=None, name="Dock A2")})
        shadow.fail_desired = 1
        agent.apply_desired_changes({"portal-a": rtsp_update("pc-2", ref=None, name="Dock A2")})

    shadow.on_get = newer_change_whose_clear_fails
    clock.now += 1.0
    agent.pump()

    assert agent._pending_clears == {"portal-a": "pc-2"}, (
        "the retry's completion removed the newer pending clear")
    clock.now += 30.0
    agent.pump()
    assert agent._pending_clears == {} and shadow.desired_changes() == {}


def test_an_unchanged_entry_without_a_portal_change_id_is_nulled_by_the_retry(world):
    shadow, agent, clock = world.shadow, world.make_agent(), world.clock
    change = rtsp_create(None, ref=None)
    del change["portalChangeId"]
    shadow.portal_writes({"portal-a": change})
    shadow.fail_desired = 1
    agent.on_delta(shadow.delta())
    agent.pump()

    clock.now += 1.0
    assert agent.pump() is None
    assert shadow.desired_changes() == {} and agent._pending_clears == {}
    assert null_writes(shadow) == [{"changes": {"portal-a": None}}]


def test_with_nothing_pending_pump_runs_no_clear_retry(world):
    shadow, agent, clock = world.shadow, world.make_agent(), world.clock
    shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None)})
    _deliver(agent, shadow)
    gets = shadow.gets
    for _ in range(5):
        clock.now += 30.0
        assert agent.pump() is None
    assert shadow.gets == gets and null_writes(shadow) == [{"changes": {"portal-a": None}}]


# --- the record --------------------------------------------------------------------------------


def test_the_record_forgets_the_oldest_change_past_its_cap(world):
    """Refused changes to a discovery id are recorded like any other; the
    257th evicts the oldest, and a skipped change counts as used."""
    shadow, agent = world.shadow, world.make_agent()
    assert PROCESSED_CHANGES_CAP == 256

    def refused(n):
        return {"disc-cam": {"op": "update", "portalChangeId": "pc-{}".format(n)}}

    for n in range(PROCESSED_CHANGES_CAP + 1):
        agent.apply_desired_changes(refused(n))
    agent.pump()
    assert len(agent._processed_changes) == PROCESSED_CHANGES_CAP
    assert ("disc-cam", "pc-0") not in agent._processed_changes
    assert next(iter(agent._processed_changes)) == ("disc-cam", "pc-1")

    agent.apply_desired_changes(refused(1))  # skipped, and now the newest
    agent.apply_desired_changes(refused(PROCESSED_CHANGES_CAP + 1))
    assert ("disc-cam", "pc-1") in agent._processed_changes
    assert ("disc-cam", "pc-2") not in agent._processed_changes

    agent.pump()
    reports = len(shadow.reported)
    agent.apply_desired_changes(refused(0))  # forgotten: applied again
    agent.pump()
    assert reported_failures(shadow)[-1] == ("disc-cam", "pc-0", REASON_DISCOVERY_MANAGED)
    agent.apply_desired_changes(refused(PROCESSED_CHANGES_CAP))  # remembered: skipped
    agent.pump()
    assert len(shadow.reported) == reports + 2
    assert "disc-cam" not in (shadow.reported[-1].get("failures") or {}), (
        "a remembered change was applied again")


def test_a_stale_redelivery_while_a_newer_change_is_parked(world, caplog):
    """The design's Edge cases: a stale, out-of-order redelivery of a
    processed change while a newer change is parked. Task 29's rule runs
    first and drops the parked change, with its INFO line; the record then
    skips the stale change. Neither is applied, and the parked change's
    timer then does nothing (the checks are not reordered)."""
    capture_logs(caplog)
    agent = world.make_agent()
    world.fetcher.outcomes = [granted()]
    apply(agent, "portal-x", rtsp_create("pc-0"))
    [image_source_id] = world.image_sources()
    cfg = "cfg-" + image_source_id
    world.fetcher.outcomes = [granted("pw-2")]
    apply(agent, cfg, rtsp_update("pc-1", ref=REF2))
    world.fetcher.outcomes = [denied(), granted("pw-3")]
    apply(agent, cfg, rtsp_update("pc-2", ref=reference(3)))
    assert world.retry_timer.delays == [2.0], "setup: pc-2 was not parked"
    fetches = len(world.fetcher.calls)

    apply(agent, cfg, rtsp_update("pc-1", ref=REF2))  # the stale redelivery

    assert len(world.fetcher.calls) == fetches
    world.retry_timer.fire_next()
    agent.pump()
    assert len(world.fetcher.calls) == fetches, "the dropped parked change was retried"
    assert world.stream_settings(image_source_id)["credentialRef"] == REF2
    assert _messages(caplog, "pc-1 for " + cfg) == [
        "Portal change pc-1 for {} supersedes change pc-2, which was waiting for a "
        "credential retry".format(cfg),
        _skip_line("pc-1", cfg),
    ]


# --- the real worker thread ----------------------------------------------------------------------


def _wait(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)
    return True


def test_the_catch_up_gets_a_report_written_within_1_s_during_a_60_s_backoff(world):
    shadow = world.shadow
    shadow.fail_reported = 1
    agent = world.make_agent(backoff_initial_seconds=60.0, backoff_max_seconds=60.0,
                             pin_worker=IdlePinWorker(), video_pin_worker=IdlePinWorker())
    agent.start()
    try:
        assert _wait(lambda: agent._not_before > world.clock.now, 5.0) and shadow.failed, (
            "setup: the first report did not fail into its 60 s backoff")
        requested = time.monotonic()
        assert agent.on_subscription_active() is True
        assert _wait(lambda: shadow.reported, 1.0), (
            "no report within 1 s of the catch-up: the worker slept through its backoff")
        assert time.monotonic() - requested < 1.0
    finally:
        agent.stop()


def test_a_failing_clear_retry_never_stops_the_worker(world, monkeypatch):
    monkeypatch.setattr(agent_module, "CLEAR_RETRY_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(agent_module, "CLEAR_RETRY_MAX_SECONDS", 0.05)
    shadow = world.shadow
    agent = world.make_agent(clock=time.monotonic, pin_worker=IdlePinWorker(),
                             video_pin_worker=IdlePinWorker())
    agent.start()
    try:
        assert _wait(lambda: shadow.reported, 2.0), "setup: no start-time report"
        shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None)})
        shadow.get_script = [ConnectionError("shadow offline"), RuntimeError("the GET broke")]
        shadow.fail_desired = 2  # the first clear and the third retry's UPDATE
        agent.apply_desired_changes(shadow.desired_changes())

        assert _wait(lambda: "portal-a" not in shadow.desired_changes(), 3.0), (
            "the clear never landed: the worker stopped retrying")
        assert agent._thread.is_alive()
        reports = len(shadow.reported)
        agent.report_inventory()
        assert _wait(lambda: len(shadow.reported) > reports, 1.0), "the worker stopped reporting"
    finally:
        agent.stop()


# --- secret hygiene (N3) -----------------------------------------------------------------------------


def test_the_new_log_lines_hold_no_change_payload(world, caplog):
    capture_logs(caplog)
    shadow, agent, clock = world.shadow, world.make_agent(), world.clock
    world.fetcher.outcomes = [granted()]
    shadow.portal_writes({"portal-x": rtsp_create("pc-1")})
    shadow.fail_desired = 2
    agent.apply_desired_changes(shadow.desired_changes())
    agent.pump()
    agent.apply_desired_changes(shadow.desired_changes())  # the redelivery: skipped
    agent.pump()
    shadow.get_script = [None]
    clock.now += 1.0
    agent.pump()
    clock.now += 1.0
    agent.pump()

    new_lines = [text for text in _messages(caplog, "") if "already processed" in text
                 or "applied desired change(s)" in text]
    assert new_lines == [
        _skip_line("pc-1", "portal-x"),
        "Could not clear 1 applied desired change(s) from the camera-registry shadow (retry 1): "
        "the shadow could not be read; retrying in 1 s",
        "Cleared 1 applied desired change(s) from the camera-registry shadow on retry 2",
    ]
    for text in new_lines:
        assert SECRET_ARN not in text and URL not in text and "credentialRef" not in text
    assert_secret_free(shadow, caplog)
    assert all(PASSWORD not in text for text in log_texts(caplog))
