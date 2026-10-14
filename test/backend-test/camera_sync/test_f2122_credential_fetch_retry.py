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
"""A denied credential fetch is retried for up to 60 s (rtsp-rtmp-stream-cameras
task 29.2, finding 21 fix (a); Requirement 5.6; design component 12,
**Denied credential fetch**). tasks.md names this file
``test_credential_fetch_retry.py``; it carries the ``f2122`` marker so its
basename is unique in the repository.

The agent runs over the real ``ImageSourceAccessor``, a private sqlite
database and a real Credential_Store (``f2122_stream_agent_support``), with
the real ``credential_fetch.fetch`` over a scripted Secrets Manager client.
It is driven through ``pump()`` without ``start()``; every retry timer is a
recording fake the test fires by hand.
"""
import logging
import threading
import time

import pytest

from f2122_stream_agent_support import (
    MALFORMED,
    PASSWORD,
    REF,
    REF2,
    SECRET_ARN,
    URL,
    AwsError,
    ImmediateTimer,
    MergingShadow,
    apply,
    assert_secret_free,
    capture_logs,
    delete_change,
    denied,
    granted,
    make_stream_world,
    not_found,
    reference,
    rtsp_create,
    rtsp_update,
)

from camera_sync.agent import CREDENTIAL_RETRY_DELAYS_S, CREDENTIAL_RETRY_NOTE, REASON_DISCOVERY_MANAGED
from stream_ingest.credential_fetch import RETRYABLE_REASONS, CredentialFetchError

_AGENT_LOGGER = "camera_sync.agent"


@pytest.fixture
def world(tmp_path, monkeypatch):
    world = make_stream_world(tmp_path, monkeypatch)
    yield world
    world.close()


def _fire_all(world, agent, limit=10):
    """Fire the retry timers in order until none is pending; their delays."""
    fired = []
    while world.retry_timer.pending:
        assert len(fired) < limit, "the retries never ended"
        fired.append(world.retry_timer.fire_next())
        agent.pump()
    return fired


def _failures(world):
    return [document["failures"] for document in world.shadow.reported if document["failures"]]


def _stream_camera(world, agent):
    """A configured stream camera holding REF's credentials, created through
    a Portal change; the call logs are reset afterwards."""
    world.fetcher.outcomes.append(granted())
    apply(agent, "portal-x", rtsp_create("pc-0"))
    [image_source_id] = world.image_sources()
    world.fetcher.calls.clear()
    world.accessor.calls.clear()
    return image_source_id


# --- the error classification --------------------------------------------------------


def test_only_a_denial_is_retryable():
    assert RETRYABLE_REASONS == {"AccessDeniedException", "AccessDenied"}
    assert CredentialFetchError("AccessDeniedException").retryable
    assert CredentialFetchError("AccessDenied").retryable
    for reason in ("ResourceNotFoundException", "InvalidReference", "MalformedSecret", "EmptySecret",
                   "TimeoutError", "accessdenied", "AccessDeniedException2"):
        assert not CredentialFetchError(reason).retryable, reason
    assert CREDENTIAL_RETRY_DELAYS_S == (2.0, 4.0, 8.0, 16.0, 30.0) and sum(CREDENTIAL_RETRY_DELAYS_S) == 60
    assert CREDENTIAL_RETRY_NOTE == "retried for 60 s"


# --- finding 21 ------------------------------------------------------------------------


def test_finding_21_a_denied_first_fetch_is_acknowledged_after_one_retry(world, caplog):
    """The Orin incident: the read grant had not propagated when the device
    fetched. The change is parked, retried 2 s later, and acknowledged; no
    report ever carries a failure for it, and the credentials land in the
    Credential_Store."""
    capture_logs(caplog)
    world.fetcher.outcomes = [denied(), granted()]
    agent = world.make_agent()
    apply(agent, "portal-x", rtsp_create("pc-1"))

    assert world.image_sources() == {} and world.fetcher.calls == [REF]
    assert world.retry_timer.delays == [2.0]
    assert "portal-x" not in world.shadow.reported[-1]["cameras"], "a parked create reports nothing"

    world.retry_timer.fire_next()
    agent.pump()

    [(image_source_id, (source_type, _))] = world.image_sources().items()
    assert source_type == "RTSP" and world.fetcher.calls == [REF, REF]
    assert world.store.get(image_source_id) == granted()
    assert world.stream_settings(image_source_id)["credentialRef"] == REF
    document = world.shadow.reported[-1]
    assert document["cameras"]["cfg-" + image_source_id]["ack"] == "pc-1"
    assert document["cameras"]["portal-x"]["ack"] == "pc-1"
    assert _failures(world) == []
    assert world.shadow.desired == [{"changes": {"portal-x": None}}]
    assert_secret_free(world.shadow, caplog)


@pytest.mark.parametrize("code", ["AccessDeniedException", "AccessDenied"])
def test_denied_six_times_fails_once_with_the_retry_note(world, caplog, code):
    capture_logs(caplog)
    world.fetcher.outcomes = [AwsError(code) for _ in range(6)]
    agent = world.make_agent()
    apply(agent, "portal-x", rtsp_create("pc-1"))

    assert _fire_all(world, agent) == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert world.fetcher.calls == [REF] * 6
    assert world.image_sources() == {} and world.accessor.calls == []
    assert _failures(world) == [{"portal-x": {
        "reason": "credential retrieval failed: {} (retried for 60 s)".format(code),
        "portalChangeId": "pc-1"}}]
    assert world.shadow.desired == [{"changes": {"portal-x": None}}]

    # One WARNING when the change is parked, INFO for each retry denied
    # again, one WARNING when it fails; each names only the camera id, the
    # change id and the error code.
    records = [record for record in caplog.records if record.name == _AGENT_LOGGER]
    assert [record.levelno for record in records] == [logging.WARNING] + [logging.INFO] * 4 + [logging.WARNING]
    for record in records:
        message = record.getMessage()
        assert "portal-x" in message and "pc-1" in message and code in message
        for value in (SECRET_ARN, REF["versionId"], URL, "viewer"):
            assert value not in message
    assert_secret_free(world.shadow, caplog)


# --- other failures fail at once ------------------------------------------------------------

_INVALID_REF = {"secretArn": SECRET_ARN, "versionId": "../x"}


@pytest.mark.parametrize("code, outcome, ref", [
    ("ResourceNotFoundException", not_found(), REF),
    ("MalformedSecret", MALFORMED, REF),
    ("InvalidReference", None, _INVALID_REF),
])
def test_a_failure_that_is_not_a_denial_fails_at_once(world, caplog, code, outcome, ref):
    capture_logs(caplog)
    world.fetcher.outcomes = [] if outcome is None else [outcome]
    agent = world.make_agent()
    apply(agent, "portal-x", rtsp_create("pc-1", ref=ref))
    assert world.retry_timer.delays == [] and world.image_sources() == {}
    assert _failures(world) == [{"portal-x": {
        "reason": "credential retrieval failed: " + code, "portalChangeId": "pc-1"}}]
    assert world.fetcher.client_calls == (0 if outcome is None else 1)
    assert_secret_free(world.shadow, caplog)


@pytest.mark.parametrize("code, outcome", [
    ("ResourceNotFoundException", not_found()),
    ("MalformedSecret", MALFORMED),
    ("InvalidReference", CredentialFetchError("InvalidReference")),
])
def test_a_failure_that_is_not_a_denial_on_a_retry_fails_at_once(world, caplog, code, outcome):
    capture_logs(caplog)
    world.fetcher.outcomes = [denied(), outcome]
    agent = world.make_agent()
    apply(agent, "portal-x", rtsp_create("pc-1"))
    assert _fire_all(world, agent) == [2.0]
    assert world.image_sources() == {}
    assert _failures(world) == [{"portal-x": {
        "reason": "credential retrieval failed: " + code, "portalChangeId": "pc-1"}}]
    final = [record for record in caplog.records if record.name == _AGENT_LOGGER][-1]
    assert final.levelno == logging.WARNING and code in final.getMessage()
    assert_secret_free(world.shadow, caplog)


# --- newer changes and redeliveries --------------------------------------------------------


def test_a_newer_update_drops_the_parked_update(world):
    agent = world.make_agent()
    image_source_id = _stream_camera(world, agent)
    csid = "cfg-" + image_source_id
    world.fetcher.outcomes = [denied(), granted("rotated-pw")]
    apply(agent, csid, rtsp_update("pc-A", ref=REF2, latencyMs=900))
    assert world.retry_timer.delays == [2.0]

    apply(agent, csid, rtsp_update("pc-B", ref=reference(3), latencyMs=700))
    assert world.shadow.reported[-1]["cameras"][csid]["ack"] == "pc-B"
    world.retry_timer.fire_next()
    agent.pump()

    assert world.fetcher.calls == [REF2, reference(3)], "the superseded update is never fetched again"
    assert [call for call in world.accessor.calls if call[0] == "update"] == [
        ("update", image_source_id, reference(3)["versionId"])]
    settings = world.stream_settings(image_source_id)
    assert settings["latencyMs"] == 700 and settings["credentialRef"] == reference(3)
    assert world.store.get(image_source_id) == granted("rotated-pw")
    assert _failures(world) == []


def test_a_delete_drops_the_parked_update(world):
    agent = world.make_agent()
    image_source_id = _stream_camera(world, agent)
    csid = "cfg-" + image_source_id
    world.fetcher.outcomes = [denied(), granted()]
    apply(agent, csid, rtsp_update("pc-A", ref=REF2, latencyMs=900))
    apply(agent, csid, delete_change("pc-B"))
    assert world.image_sources() == {} and world.store.get(image_source_id) is None

    world.retry_timer.fire_next()
    agent.pump()
    assert world.fetcher.calls == [REF2]
    assert world.image_sources() == {}
    assert [call[0] for call in world.accessor.calls] == ["delete"]
    assert _failures(world) == []


def test_a_redelivered_parked_change_is_applied_once(world):
    world.fetcher.outcomes = [denied(), granted()]
    agent = world.make_agent()
    change = rtsp_create("pc-1")
    apply(agent, "portal-x", change)
    apply(agent, "portal-x", dict(change))
    assert world.fetcher.calls == [REF], "the redelivery is not applied again"
    assert world.retry_timer.delays == [2.0], "and the parked change keeps its schedule"
    assert world.shadow.desired == [{"changes": {"portal-x": None}}] * 2

    world.retry_timer.fire_next()
    agent.pump()
    assert len(world.image_sources()) == 1
    assert [call[0] for call in world.accessor.calls] == ["create"]
    acks = [entry["ack"] for document in world.shadow.reported
            for csid, entry in document["cameras"].items()
            if entry and csid.startswith("cfg-") and "ack" in entry]
    assert acks == ["pc-1"]


def test_a_stale_timer_does_not_disturb_the_newer_changes_schedule(world):
    """A is parked; a newer B for the same id is denied and parked; then A's
    timer fires. Nothing runs, and B keeps the 2, 4, 8, 16 and 30 s of its
    own attempts."""
    world.fetcher.outcomes = [denied() for _ in range(7)]
    agent = world.make_agent()
    apply(agent, "portal-x", rtsp_create("pc-A", ref=reference(10)))
    apply(agent, "portal-x", rtsp_create("pc-B", ref=reference(11)))
    assert [delay for delay, _ in world.retry_timer.pending] == [2.0, 2.0]
    reports = len(world.shadow.reported)

    world.retry_timer.fire(0)  # A's timer
    agent.pump()
    assert world.fetcher.calls == [reference(10), reference(11)]
    assert len(world.shadow.reported) == reports, "a stale timer requests no report"

    assert _fire_all(world, agent) == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert world.fetcher.calls == [reference(10)] + [reference(11)] * 6
    assert _failures(world) == [{"portal-x": {
        "reason": "credential retrieval failed: AccessDeniedException (retried for 60 s)",
        "portalChangeId": "pc-B"}}]
    assert world.image_sources() == {}


def test_other_changes_apply_while_a_change_is_parked(world):
    """In the same delta and in later ones; nothing waits on the delivering
    thread: one fetch per delivery, the retry on the timer."""
    world.fetcher.outcomes = [denied(), granted()]
    agent = world.make_agent()
    started = time.monotonic()
    agent.apply_desired_changes({"portal-a": rtsp_create("pc-a"),
                                 "portal-b": rtsp_create("pc-b", ref=None)})
    agent.pump()
    assert time.monotonic() - started < 2.0
    assert world.fetcher.calls == [REF] and world.retry_timer.delays == [2.0]
    document = world.shadow.reported[-1]
    assert document["cameras"]["portal-b"]["ack"] == "pc-b"
    assert "portal-a" not in document["cameras"] and document["failures"] == {}

    apply(agent, "portal-c", rtsp_create("pc-c", ref=None))
    assert world.shadow.reported[-1]["cameras"]["portal-c"]["ack"] == "pc-c"
    assert len(world.image_sources()) == 2

    world.retry_timer.fire_next()
    agent.pump()
    assert world.shadow.reported[-1]["cameras"]["portal-a"]["ack"] == "pc-a"
    assert len(world.image_sources()) == 3 and _failures(world) == []


def test_a_parked_update_leaves_the_camera_as_it_was(world):
    agent = world.make_agent()
    image_source_id = _stream_camera(world, agent)
    before = world.stream_settings(image_source_id)
    world.fetcher.outcomes = [denied()]
    apply(agent, "cfg-" + image_source_id, rtsp_update("pc-A", ref=REF2, latencyMs=900, url=URL + "2"))
    assert world.retry_timer.delays == [2.0]
    assert world.stream_settings(image_source_id) == before
    assert before["latencyMs"] == 400 and before["credentialRef"] == REF
    assert world.store.get(image_source_id) == granted()
    assert world.accessor.calls == []
    document = world.shadow.reported[-1]
    assert "ack" not in document["cameras"]["cfg-" + image_source_id] and document["failures"] == {}


def test_the_start_time_catch_up_parks_the_same_way(tmp_path, monkeypatch):
    shadow = MergingShadow({"desired": {"changes": {"portal-x": rtsp_create("pc-1")}},
                            "reported": {"schemaVersion": 1, "cameras": {}, "failures": {}}})
    world = make_stream_world(tmp_path, monkeypatch, shadow=shadow)
    try:
        world.fetcher.outcomes = [denied(), granted()]
        agent = world.make_agent(refresh=True)
        state = shadow.get_thing_shadow_state_request("thing", "dda-camera-registry")
        agent.apply_desired_changes(dict(state["desired"]["changes"], **{"portal-old": None}))
        agent.pump()
        assert world.retry_timer.delays == [2.0] and world.image_sources() == {}
        assert shadow.desired == [{"changes": {"portal-x": None}}]
        assert shadow.state["desired"]["changes"] == {}

        world.retry_timer.fire_next()
        agent.pump()
        assert len(world.image_sources()) == 1
        assert shadow.reported[-1]["cameras"]["portal-x"]["ack"] == "pc-1"
    finally:
        world.close()


def test_stop_drops_the_parked_change(world):
    world.fetcher.outcomes = [denied(), granted()]
    agent = world.make_agent()
    apply(agent, "portal-x", rtsp_create("pc-1"))
    agent.stop()
    world.retry_timer.fire_next()
    agent.pump()
    assert world.fetcher.calls == [REF]
    assert world.image_sources() == {} and world.accessor.calls == []


def test_an_update_of_a_parked_create_drops_it_and_is_refused(world):
    """Design component 12: an update of a parked create is a newer change.
    The device drops the create, and refuses the update as for any id it
    never created; the create's old timer then does nothing."""
    world.fetcher.outcomes = [denied(), granted()]
    agent = world.make_agent()
    apply(agent, "portal-x", rtsp_create("pc-1"))
    apply(agent, "portal-x", rtsp_update("pc-2", ref=None, latencyMs=900))
    assert world.shadow.reported[-1]["failures"] == {
        "portal-x": {"reason": REASON_DISCOVERY_MANAGED, "portalChangeId": "pc-2"}}

    world.retry_timer.fire_next()
    agent.pump()
    assert world.fetcher.calls == [REF]
    assert world.image_sources() == {} and world.accessor.calls == []


def test_no_retry_timer_starts_while_the_apply_lock_is_held(world):
    """With a timer that runs its action at once, a denied-then-granted
    change completes. Had the timer started under ``_apply_lock``, the retry
    would wait on that lock forever, and the join below would time out."""
    world.fetcher.outcomes = [denied(), granted()]
    timer = ImmediateTimer()
    agent = world.make_agent(change_retry_timer=timer)
    errors = []

    def deliver():
        try:
            agent.apply_desired_changes({"portal-x": rtsp_create("pc-1")})
            agent.pump()
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    worker = threading.Thread(target=deliver, name="f2122-delivery", daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "applying the change deadlocked on _apply_lock"
    assert errors == []
    assert timer.delays == [2.0] and world.fetcher.calls == [REF, REF]
    [image_source_id] = world.image_sources()
    assert world.shadow.reported[-1]["cameras"]["cfg-" + image_source_id]["ack"] == "pc-1"
    assert PASSWORD not in str(world.shadow.reported)
