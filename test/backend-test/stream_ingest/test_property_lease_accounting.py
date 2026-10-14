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
"""Property test for the ``StreamIngestManager`` lease accounting
(rtsp-rtmp-stream-cameras task 16.8), with recording fake sessions and an
injected clock.

**Feature: rtsp-rtmp-stream-cameras, Property 20: Lease accounting and session limit**
**Validates: Requirements 8.1, 8.2, 8.9, 8.10**
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402
from hypothesis import given, settings, strategies as st  # noqa: E402

from stream_fakes import ManualClock, rtsp_source  # noqa: E402
from stream_ingest import health  # noqa: E402
from stream_ingest.manager import (  # noqa: E402
    IDLE_GRACE_S,
    SessionLimitError,
    StreamIngestManager,
    camera_key_for_image_source,
    camera_key_for_url,
)


class RecordingSession:
    """A session that records what the manager does to it."""

    created = []

    def __init__(self, camera_key, source_provider, capabilities_provider, clock=None, on_health=None):
        self.camera_key = camera_key
        self.source_provider = source_provider
        self.on_health = on_health
        self.started = self.stopped = False
        self.restarts = 0
        self.leases = 0
        RecordingSession.created.append(self)

    def start(self):
        self.started = True

    def stop(self, reason=""):
        self.stopped = True

    def restart(self, reason=""):
        self.restarts += 1

    def tick(self, now=None):
        pass

    def set_leases(self, count):
        self.leases = count

    def health(self):
        return {"cameraKey": self.camera_key, "state": health.STREAMING, "leases": self.leases}


def make_manager(limit=4, clock=None):
    RecordingSession.created = []
    clock = clock or ManualClock()
    limits = {"value": limit}
    manager = StreamIngestManager(session_factory=RecordingSession, max_sessions=lambda: limits["value"],
                                  capabilities=object(), configured_source=lambda image_source_id: rtsp_source(),
                                  clock=clock, supervise=False)
    return manager, clock, limits


KEYS = [camera_key_for_image_source(index) for index in range(6)]

operations = st.lists(st.one_of(
    st.tuples(st.just("acquire"), st.integers(0, len(KEYS) - 1)),
    st.tuples(st.just("release"), st.integers(0, 30)),
    st.tuples(st.just("advance"), st.sampled_from([1.0, 10.0, 29.9, 30.0, 45.0])),
    st.tuples(st.just("limit"), st.integers(1, 5)),
), min_size=1, max_size=60)


class TestProperty20LeaseAccounting:
    @settings(max_examples=25, deadline=None)
    @given(ops=operations, limit=st.integers(1, 5))
    def test_sessions_follow_the_leases_the_grace_and_the_limit(self, ops, limit):
        manager, clock, limits = make_manager(limit)
        held = []  # leases the test holds, in acquisition order
        idle_since = {}
        for kind, argument in ops:
            if kind == "acquire":
                key = KEYS[argument]
                before = set(manager.session_keys())
                live = [session for session in RecordingSession.created if not session.stopped]
                if key not in before and len(before) >= limits["value"]:
                    with pytest.raises(SessionLimitError) as raised:
                        manager.acquire_lease(key, "test")
                    assert raised.value.category == health.SESSION_LIMIT
                    assert str(limits["value"]) in raised.value.message
                    # Refused without affecting existing sessions.
                    assert set(manager.session_keys()) == before
                    assert [session for session in RecordingSession.created if not session.stopped] == live
                    continue
                held.append(manager.acquire_lease(key, "test"))
                idle_since.pop(key, None)
            elif kind == "release":
                if held:
                    lease = held.pop(argument % len(held))
                    manager.release_lease(lease)
                    if not any(other.camera_key == lease.camera_key for other in held):
                        idle_since[lease.camera_key] = clock.now
            elif kind == "advance":
                clock.advance(argument)
                manager.tick()
                for key, since in list(idle_since.items()):
                    if clock.now - since >= IDLE_GRACE_S:
                        del idle_since[key]
                # Exactly while leased or within the idle grace.
                expected = {lease.camera_key for lease in held} | set(idle_since)
                assert set(manager.session_keys()) == expected
            else:
                limits["value"] = argument

            leased = {lease.camera_key for lease in held}
            keys = set(manager.session_keys())
            assert leased <= keys, "a leased camera always has its session"
            for key in keys:
                assert manager.lease_count(key) == sum(1 for lease in held if lease.camera_key == key)
                assert manager.session(key).leases == manager.lease_count(key)
            # One session per camera: at most one live session object per key.
            for key in KEYS:
                live = [session for session in RecordingSession.created
                        if session.camera_key == key and not session.stopped]
                assert len(live) <= 1
                assert (len(live) == 1) == (key in keys)

    def test_a_lease_within_the_grace_reuses_the_session(self):
        manager, clock, _limits = make_manager()
        key = KEYS[0]
        manager.release_lease(manager.acquire_lease(key, "viewer"))
        session = manager.session(key)
        clock.advance(IDLE_GRACE_S - 1)
        manager.tick()
        lease = manager.acquire_lease(key, "workflow")
        clock.advance(IDLE_GRACE_S * 2)
        manager.tick()
        assert manager.session(key) is session and not session.stopped
        manager.release_lease(lease)
        clock.advance(IDLE_GRACE_S)
        manager.tick()
        assert manager.session(key) is None and session.stopped

    def test_a_released_lease_is_released_once(self):
        manager, _clock, _limits = make_manager()
        first = manager.acquire_lease(KEYS[0], "a")
        second = manager.acquire_lease(KEYS[0], "b")
        manager.release_lease(first)
        manager.release_lease(first)
        assert manager.lease_count(KEYS[0]) == 1
        manager.release_lease(second)
        manager.release_lease(None)
        assert manager.lease_count(KEYS[0]) == 0

    def test_a_leased_camera_can_always_take_more_leases_at_the_limit(self):
        manager, _clock, _limits = make_manager(limit=1)
        manager.acquire_lease(KEYS[0], "a")
        manager.acquire_lease(KEYS[0], "b")
        with pytest.raises(SessionLimitError):
            manager.acquire_lease(KEYS[1], "c")
        assert manager.lease_count(KEYS[0]) == 2


class TestManagerSeams:
    def test_a_configuration_change_restarts_and_keeps_the_leases(self):
        manager, _clock, _limits = make_manager()
        key = camera_key_for_image_source("7")
        manager.acquire_lease(key, "workflow")
        manager.notify_config_changed("7")
        session = manager.session(key)
        assert session.restarts == 1 and not session.stopped
        assert manager.lease_count(key) == 1
        manager.notify_config_changed("8")  # no session: nothing to do

    def test_a_deleted_camera_stops_now_even_with_leases(self):
        manager, _clock, _limits = make_manager()
        key = camera_key_for_image_source("7")
        lease = manager.acquire_lease(key, "workflow")
        session = manager.session(key)
        manager.notify_deleted("7")
        assert session.stopped and manager.session(key) is None
        manager.release_lease(lease)  # harmless afterwards
        assert manager.lease_count(key) == 0

    def test_anonymous_streams_need_their_source_and_carry_it(self):
        manager, _clock, _limits = make_manager()
        key = camera_key_for_url("rtsp://10.0.4.21/stream1")
        with pytest.raises(ValueError):
            manager.acquire_lease(key, "workflow")
        source = rtsp_source()
        manager.acquire_lease(key, "workflow", source=source)
        assert manager.session(key).source_provider() is source
        with pytest.raises(ValueError):
            manager.acquire_lease("bogus-key", "workflow")

    def test_health_listeners_hear_session_changes_and_survive_failures(self):
        manager, _clock, _limits = make_manager()
        heard = []

        def broken(_key, _health):
            raise RuntimeError("listener bug")

        manager.add_health_listener(broken)
        manager.add_health_listener(lambda key, document: heard.append((key, document["state"])))
        key = KEYS[0]
        manager.acquire_lease(key, "viewer")
        manager.session(key).on_health(key, {"state": health.RECONNECTING})
        assert heard == [(key, health.RECONNECTING)]
        assert manager.health(key)["leases"] == 1
        assert manager.health_for_image_source("0")["cameraKey"] == key
        assert manager.health(KEYS[5]) is None

    def test_shutdown_stops_everything_and_refuses_new_leases(self):
        manager, _clock, _limits = make_manager()
        manager.acquire_lease(KEYS[0], "a")
        manager.acquire_lease(KEYS[1], "b")
        manager.shutdown()
        assert all(session.stopped for session in RecordingSession.created)
        assert manager.session_keys() == []
        with pytest.raises(Exception):
            manager.acquire_lease(KEYS[0], "c")
