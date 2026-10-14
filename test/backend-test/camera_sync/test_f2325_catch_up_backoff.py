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
"""The activation catch-up resets the report and clear backoffs only after a
readable shadow GET (rtsp-rtmp-stream-cameras task 30 follow-up, review item
1; Requirement 5.14; design component 12, **Catch-up on activation**).

While the camera-registry stream is up and the catch-up GET keeps failing, the
subscription's supervisor runs ``on_subscription_active()`` again about every
``SLEEP_TIME``. A catch-up whose GET fails (``None``, or a GET that raises)
leaves the report debounce and backoff, the report request and the clear
backoff as they were: it forces no report, and a failing write keeps backing
off instead of starting again from 1 s. A readable GET (a mapping, or
``False`` when the shadow does not exist) resets them, as before.

The agent runs over the real ``ImageSourceAccessor`` and a private sqlite
database (``f2122_stream_agent_support.make_stream_world``), on a fake clock,
with the scripted shadow of ``f2325_agent_support``.
"""
import pytest

from f2122_stream_agent_support import capture_logs, make_stream_world, rtsp_create
from f2325_agent_support import STATE, ScriptedShadow

from camera_sync.agent import BACKOFF_INITIAL_SECONDS, CLEAR_RETRY_INITIAL_SECONDS

_AGENT_LOGGER = "camera_sync.agent"
#: The subscription supervisor's catch-up retry interval (``SLEEP_TIME``).
SUPERVISOR_TICK_S = 10.0


@pytest.fixture
def world(tmp_path, monkeypatch):
    world = make_stream_world(tmp_path, monkeypatch, shadow=ScriptedShadow())
    yield world
    world.close()


def _schedule(agent):
    """The report and clear schedule a catch-up may reset."""
    with agent._lock:
        return {"dirty": agent._dirty, "not_before": agent._not_before,
                "retry_delay": agent._retry_delay, "clear_not_before": agent._clear_not_before,
                "clear_retry_delay": agent._clear_retry_delay}


def _reset_schedule():
    return {"dirty": True, "not_before": 0.0, "retry_delay": BACKOFF_INITIAL_SECONDS,
            "clear_not_before": 0.0, "clear_retry_delay": CLEAR_RETRY_INITIAL_SECONDS}


def _catch_up_warnings(caplog):
    return [record.getMessage() for record in caplog.records if record.name == _AGENT_LOGGER
            and record.getMessage() == "Could not read the camera-registry shadow after subscribing; retrying"]


def _backing_off(world):
    """An agent whose report write and clear both keep failing: the report
    backoff has grown to 8 s and the clear backoff to 4 s."""
    shadow, clock = world.shadow, world.clock
    agent = world.make_agent()
    shadow.portal_writes({"portal-a": rtsp_create("pc-1", ref=None)})
    shadow.fail_desired = 1        # the first clear
    shadow.fail_reported = 1000    # every report
    agent.on_delta(shadow.delta())
    shadow.get_script = [None, None]  # the first two clear retries cannot read the shadow
    for _ in range(4):
        clock.now += agent.pump()
    schedule = _schedule(agent)
    assert schedule["retry_delay"] == 8.0 and schedule["clear_retry_delay"] == 4.0, (
        "setup: the report and clear backoffs did not grow: {}".format(schedule))
    assert agent._pending_clears == {"portal-a": "pc-1"} and len(world.image_sources()) == 1
    return agent


@pytest.mark.parametrize("readable", [STATE, False], ids=["mapping", "no-shadow"])
@pytest.mark.parametrize("unreadable", [None, ConnectionError("shadow offline")], ids=["none", "raises"])
def test_a_failed_catch_up_keeps_every_backoff_and_a_readable_one_resets_them(world, caplog, unreadable, readable):
    capture_logs(caplog)
    shadow, clock = world.shadow, world.clock
    agent = _backing_off(world)
    attempts = len(shadow.failed)

    for _ in range(6):  # the supervisor's retries while the stream is up and the GET fails
        before = _schedule(agent)
        shadow.get_script = [unreadable]
        if isinstance(unreadable, BaseException):
            with pytest.raises(type(unreadable)):
                agent.on_subscription_active()
        else:
            assert agent.on_subscription_active() is False
        assert _schedule(agent) == before, (
            "a catch-up whose GET failed reset the report or clear schedule: {} -> {}".format(
                before, _schedule(agent)))
        clock.now += SUPERVISOR_TICK_S
    assert len(shadow.failed) == attempts, "a failed catch-up wrote to the shadow"
    if unreadable is None:
        assert len(_catch_up_warnings(caplog)) == 6

    shadow.get_script = [readable]
    assert agent.on_subscription_active() is True
    assert _schedule(agent) == _reset_schedule(), "a readable catch-up did not reset the schedule"
    shadow.fail_reported = 0
    reports = len(shadow.reported)
    agent.pump()
    assert len(shadow.reported) == reports + 1, "the readable catch-up did not get a report written at once"
    assert len(world.image_sources()) == 1, "the catch-up applied the processed change again"


def test_while_the_catch_up_get_keeps_failing_no_report_is_forced(world, caplog):
    """The review's cadence: one undebounced report per failed catch-up,
    about every 10 s. Over 60 s of supervisor retries with nothing changed,
    no report is written; the first readable catch-up writes one."""
    capture_logs(caplog)
    shadow, clock = world.shadow, world.clock
    agent = world.make_agent()
    agent.report_inventory()
    assert agent.pump() is None and len(shadow.reported) == 1, "setup: the report was not written"

    for second in range(1, 61):
        clock.now += 1.0
        if second % int(SUPERVISOR_TICK_S) == 0:
            shadow.get_script = [None]
            assert agent.on_subscription_active() is False
        agent.pump()

    assert len(_catch_up_warnings(caplog)) == 6
    assert len(shadow.reported) == 1, (
        "{} report(s) written in 60 s while only the catch-up GET failed".format(len(shadow.reported) - 1))

    assert agent.on_subscription_active() is True
    assert agent.pump() is None
    assert len(shadow.reported) == 2, "the readable catch-up did not get a report written"


def test_a_failing_report_keeps_backing_off_while_the_catch_up_get_keeps_failing(world):
    """The review's backoff: a failing write's 1-60 s backoff restarted from 1 s
    at every failed catch-up. Over 120 s of supervisor retries the report
    writes follow their own backoff, 1, 2, 4 ... 60 s apart."""
    shadow, clock = world.shadow, world.clock
    agent = world.make_agent()
    shadow.fail_reported = 1000
    agent.report_inventory()

    written_at = []
    for second in range(0, 121):
        if second and second % int(SUPERVISOR_TICK_S) == 0:
            shadow.get_script = [None]
            assert agent.on_subscription_active() is False
        failed = len(shadow.failed)
        agent.pump()
        if len(shadow.failed) > failed:
            written_at.append(second)
        clock.now += 1.0

    gaps = [later - earlier for earlier, later in zip(written_at, written_at[1:])]
    assert gaps == [1, 2, 4, 8, 16, 32], (
        "the report writes were {} s apart, not on their own backoff: failed catch-ups "
        "restarted it".format(gaps))
    assert _schedule(agent)["retry_delay"] == 60.0
