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
"""Tests for the new surface of the Greengrass trigger-subscription fix.

Feature: keep the Greengrass IPC trigger subscription alive across a
workflow swap. ``test_trigger_swap_resubscribe.py`` reproduces the bug
through the manager (and fails on the pre-fix revision); this module covers
the pieces the fix adds, so it runs against the fixed code only:

(a) quiesce primitives — a closed ``TriggerGate`` discards for every
    concurrency policy; a quiesced ``WorkflowActivationCore`` discards before
    the ``WorkflowDisabled`` check and starts no further queued activation
    (``ActivationDispatcher.request_stop()``);
(b) the nucleus double itself (``greengrass_nucleus_double.FakeNucleus``) —
    its counting, UNSUBSCRIBE-on-last-close and piggyback semantics;
(c) ``GreengrassStreamRegistry`` alone, with fake streams and manual timers —
    immediate close with another live stream, linger of the last stream,
    release by a new subscriber, expiry (once), release of the lingering
    stream itself, the timer-factory fallback, ``close_all`` and the
    constructor validation;
(d) the real ``GreengrassIpcSubscriber`` on the double — a stopped worker
    lingers its last stream and drops deliveries until expiry, two
    departures produce one linger, a reconnect tears its lost stream down
    at once, and a reconnect subscribe completing after ``stop()`` hands the
    late stream to the registry instead of leaking it.
"""
import logging
import threading
from types import SimpleNamespace

import pytest

import workflow_engine.trigger_runtime as trigger_runtime
from workflow_engine.trigger_runtime import (
    GREENGRASS_STREAM_LINGER_SECONDS,
    HEALTH_CONNECTING,
    HEALTH_RECONNECTING,
    HEALTH_SUBSCRIBED,
    POLICY_DEBOUNCE,
    POLICY_DROP,
    POLICY_QUEUE,
    ActivationQueue,
    FiringSequence,
    GreengrassIpcSubscriber,
    GreengrassStreamRegistry,
    ReconnectEngine,
    RunActivation,
    TriggerGate,
    TriggerHealth,
    TriggerPolicy,
    WorkflowActivationCore,
)

from greengrass_nucleus_double import FakeNucleus, install_awsiot_stubs
from test_property_concurrency_policies import ManualTimerFactory
from test_trigger_lifecycle import make_overrides_config, wait_for

LOGGER = "workflow_engine.trigger_runtime"
NODE_ID = "trigger_t1"
TOPIC = "factory/line1/inspection/evaluate"
REGISTRATION_ID = "wfA:8"
CONTEXT = {"topic": TOPIC, "payload": "{}", "qos": 1, "timestamp": 1.0}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def awsiot_stubs(monkeypatch):
    return install_awsiot_stubs(monkeypatch)


@pytest.fixture
def manual_registry(monkeypatch):
    """A registry with manual timers, installed as the module default too
    (so a worker built without ``stream_registry=`` would use it); every
    lingering stream is closed at teardown."""
    timers = ManualTimerFactory()
    registry = GreengrassStreamRegistry(timer_factory=timers)
    monkeypatch.setattr(trigger_runtime, "DEFAULT_STREAM_REGISTRY", registry)
    yield SimpleNamespace(registry=registry, timers=timers)
    registry.close_all()


def messages(caplog, needle):
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


def record_states(health):
    """Capture every health state transition, in order."""
    states = []
    original = health.set_state

    def recording_set_state(state, error=None):
        states.append(state)
        original(state, error=error)

    health.set_state = recording_set_state
    return states


def make_synchronous_engine(health, worker_holder, retry_limit=0):
    """A ReconnectEngine running its loop inline with a non-blocking waiter,
    late-bound to the worker the holder carries (the manager's wiring)."""
    engine = ReconnectEngine(
        node_id=NODE_ID,
        target=TOPIC,
        health=health,
        retry_limit=retry_limit,
        waiter=lambda delay: False,
        spawn=lambda target: target(),
    )
    engine.bind(lambda: worker_holder["worker"].reconnect())
    return engine


def greengrass_params(**overrides):
    params = {"topic": TOPIC, "qos": 1, "greengrass": True}
    params.update(overrides)
    return params


def make_worker(nucleus, registry, on_delivery=None, on_connection_lost=None, health=None):
    health = health if health is not None else TriggerHealth(NODE_ID, "mqtt_subscribe")
    worker = GreengrassIpcSubscriber(
        greengrass_params(),
        on_delivery if on_delivery is not None else (lambda context: True),
        on_connection_lost if on_connection_lost is not None else (lambda error: None),
        health,
        ipc_connect=nucleus.connect,
        stream_registry=registry,
    )
    return worker, health


class FakeStream:
    """A registry entry stand-in: topic, node id and a counting ``close``."""

    def __init__(self, topic=TOPIC, node_id=NODE_ID):
        self.topic = topic
        self.node_id = node_id
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class RecordingHandler:
    def __init__(self):
        self.events = []
        self.errors = []

    def on_stream_event(self, event):
        self.events.append(event)

    def on_stream_error(self, error):
        self.errors.append(error)
        return True

    def on_stream_closed(self):
        pass


def activate(nucleus, topic=TOPIC):
    """Open one stream on the double directly (no worker)."""
    client = nucleus.connect()
    handler = RecordingHandler()
    operation = client.new_subscribe_to_iot_core(handler)
    operation.activate(SimpleNamespace(topic_name=topic, qos=1))
    operation.get_response().result(timeout=1.0)
    return operation


# ---------------------------------------------------------------------------
# (a) Quiesce primitives: gate closed for every policy, core quiesce,
#     dispatcher intake stopped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", [POLICY_QUEUE, POLICY_DROP, POLICY_DEBOUNCE])
def test_closed_gate_discards_for_every_policy(policy, caplog):
    """After close(), fire() returns False, enqueues nothing and logs the
    discard at INFO naming the trigger node and the registration, for the
    queue, drop and debounce policies alike."""
    queue = ActivationQueue()
    gate = TriggerGate(
        TriggerPolicy(NODE_ID, concurrency_policy=policy),
        queue,
        FiringSequence(),
        lambda node: False,
        timer_factory=ManualTimerFactory(),
        registration_id=REGISTRATION_ID,
    )
    assert gate.closed is False
    gate.close()
    assert gate.closed is True

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert gate.fire(CONTEXT) is False

    assert len(queue) == 0
    discarded = messages(caplog, "firing discarded")
    assert len(discarded) == 1
    assert NODE_ID in discarded[0] and REGISTRATION_ID in discarded[0]
    assert "(gate closed)" in discarded[0]


def test_quiesced_core_discards_before_the_disabled_check(caplog):
    """A quiesced core refuses the firing on the closed gate first: no
    WorkflowDisabled line is written for a disabled, quiesced workflow."""
    core = WorkflowActivationCore(
        REGISTRATION_ID,
        policies=[TriggerPolicy(NODE_ID)],
        run_starter=lambda activation: None,
        workflow_id="wf-1",
        overrides_config=make_overrides_config({"wf-1": "true"}),
    )
    core.quiesce()

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert core.fire(NODE_ID, CONTEXT) is False

    assert len(messages(caplog, "gate closed")) == 1
    assert messages(caplog, "WorkflowDisabled") == []
    assert len(core.queue) == 0


def test_quiesce_lets_the_run_in_flight_finish_and_starts_no_queued_activation():
    """Two queued activations, the first in flight on a blocking run_starter:
    quiesce(), release the run, stop() — exactly one run happened and the
    second activation is still queued, never started."""
    release = threading.Event()
    runs = []

    def run_starter(activation):
        runs.append(activation)
        release.wait(5.0)

    core = WorkflowActivationCore(
        REGISTRATION_ID, policies=[TriggerPolicy(NODE_ID)], run_starter=run_starter
    )
    assert core.fire(NODE_ID, CONTEXT) is True
    assert core.fire(NODE_ID, CONTEXT) is True
    assert len(core.queue) == 2

    core.start()
    assert wait_for(core.dispatcher.is_in_flight)
    core.quiesce()
    release.set()
    core.stop()

    assert len(runs) == 1
    assert len(core.queue) == 1


def test_activation_pushed_after_quiesce_is_never_started(caplog):
    """A started, idle core: quiesce(), then an activation pushed straight
    into its queue (bypassing the gate), then stop(). Nothing ran; the
    activation is either still queued or was popped and discarded with one
    INFO line saying the registration is stopping."""
    runs = []
    core = WorkflowActivationCore(
        REGISTRATION_ID, policies=[TriggerPolicy(NODE_ID)], run_starter=runs.append
    )
    core.start()

    with caplog.at_level(logging.INFO, logger=LOGGER):
        core.quiesce()
        core.queue.push(RunActivation(NODE_ID, NODE_ID, 100, 0, {}))
        core.stop()

    assert runs == []
    not_started = messages(caplog, "not started")
    assert len(core.queue) + len(not_started) == 1
    for message in not_started:
        assert REGISTRATION_ID in message and NODE_ID in message


# ---------------------------------------------------------------------------
# (b) The nucleus double's own semantics
# ---------------------------------------------------------------------------


def test_fake_nucleus_counts_streams_and_piggybacks_on_a_live_filter():
    nucleus = FakeNucleus()
    first = activate(nucleus)
    second = activate(nucleus)

    assert nucleus.subscribes == [(TOPIC, "real"), (TOPIC, "piggyback")]
    assert nucleus.open_streams(TOPIC) == 2
    assert nucleus.cloud_subscribed == {TOPIC}

    first.close()
    assert nucleus.unsubscribes == []  # one stream still holds the filter
    assert nucleus.open_streams(TOPIC) == 1
    assert nucleus.deliver(TOPIC, b"x") == 1
    assert len(second.handler.events) == 1


def test_fake_nucleus_unsubscribes_on_the_last_close_and_piggybacks_in_the_window():
    """The bug's shape: the last close sends the UNSUBSCRIBE; a subscribe in
    the window is a piggyback that does not re-add the filter, so after the
    UNSUBACK the open stream receives nothing."""
    nucleus = FakeNucleus()
    only = activate(nucleus)
    only.close()

    assert nucleus.unsubscribes == [TOPIC]
    assert nucleus.unsub_in_flight == {TOPIC}
    assert nucleus.open_streams(TOPIC) == 0

    late = activate(nucleus)
    assert nucleus.subscribes[-1] == (TOPIC, "piggyback")
    assert nucleus.deliver(TOPIC, b"x") == 0  # UNSUBSCRIBE in flight

    nucleus.complete_unsubacks()
    assert TOPIC not in nucleus.cloud_subscribed
    assert nucleus.open_streams(TOPIC) == 1
    assert nucleus.deliver(TOPIC, b"x") == 0  # open, but dead
    assert late.handler.events == []
    assert late.closed is False


def test_fake_nucleus_client_close_closes_its_operations_once():
    nucleus = FakeNucleus()
    operation = activate(nucleus)
    client = operation.client

    operation.close()
    assert operation.closed and nucleus.unsubscribes == [TOPIC]
    operation.close()  # a second close changes nothing
    assert nucleus.unsubscribes == [TOPIC]

    client.close()
    assert client.closed and client.close_calls == 1
    assert nucleus.unsubscribes == [TOPIC]

    other = activate(nucleus)
    other.client.close()
    assert other.closed  # closing the client closes its streams
    assert other.client.close_calls == 1


# ---------------------------------------------------------------------------
# (c) GreengrassStreamRegistry alone
# ---------------------------------------------------------------------------


def make_registry():
    timers = ManualTimerFactory()
    return GreengrassStreamRegistry(timer_factory=timers), timers


def test_release_with_another_live_stream_closes_now():
    registry, timers = make_registry()
    first, second = FakeStream(), FakeStream()
    registry.register(first)
    registry.register(second)
    assert registry.live_count(TOPIC) == 2

    registry.release(first, linger=True)

    assert first.close_calls == 1
    assert registry.live_count(TOPIC) == 1
    assert registry.lingering_topics() == []
    assert timers.timers == []


def test_last_stream_lingers_with_the_grace_timer(caplog):
    registry, timers = make_registry()
    stream = FakeStream()
    registry.register(stream)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        registry.release(stream, linger=True)

    assert stream.close_calls == 0
    assert registry.live_count(TOPIC) == 0
    assert registry.lingering_topics() == [TOPIC]
    assert len(timers.timers) == 1
    timer = timers.timers[-1]
    assert timer.delay == GREENGRASS_STREAM_LINGER_SECONDS
    assert timer.started and not timer.cancelled
    kept = messages(caplog, "kept open")
    assert len(kept) == 1 and TOPIC in kept[0] and NODE_ID in kept[0]
    assert kept[0].startswith("Trigger subscriptions")


def test_register_on_a_lingering_filter_releases_the_lingering_stream(caplog):
    registry, timers = make_registry()
    old = FakeStream()
    registry.register(old)
    registry.release(old, linger=True)
    new = FakeStream(node_id="trigger_t2")

    with caplog.at_level(logging.INFO, logger=LOGGER):
        registry.register(new)

    assert old.close_calls == 1
    assert timers.timers[-1].cancelled
    assert registry.lingering_topics() == []
    assert registry.live_count(TOPIC) == 1
    released = messages(caplog, "released")
    assert len(released) == 1 and "trigger_t2" in released[0]
    assert released[0].startswith("Trigger subscriptions")
    # The cancelled timer's callback does nothing if it ever ran.
    timers.timers[-1].callback()
    assert old.close_calls == 1


def test_expiry_closes_the_lingering_stream_exactly_once(caplog):
    registry, timers = make_registry()
    stream = FakeStream()
    registry.register(stream)
    registry.release(stream, linger=True)
    timer = timers.timers[-1]

    with caplog.at_level(logging.INFO, logger=LOGGER):
        timer.fire()

    assert stream.close_calls == 1
    assert registry.lingering_topics() == []
    closed = messages(caplog, "closed after")
    assert len(closed) == 1 and TOPIC in closed[0]
    assert closed[0].startswith("Trigger subscriptions")

    timer.callback()  # a second expiry of the same timer is a no-op
    assert stream.close_calls == 1


def test_never_live_stream_closes_now_and_leaves_the_lingering_entry():
    registry, _timers = make_registry()
    lingering = FakeStream()
    registry.register(lingering)
    registry.release(lingering, linger=True)
    stranger = FakeStream()

    registry.release(stranger, linger=True)

    assert stranger.close_calls == 1
    assert lingering.close_calls == 0
    assert registry.lingering_topics() == [TOPIC]


def test_two_live_streams_released_in_turn_linger_only_the_last():
    registry, timers = make_registry()
    first, second = FakeStream(), FakeStream()
    registry.register(first)
    registry.register(second)

    registry.release(first, linger=True)
    assert first.close_calls == 1
    assert registry.lingering_topics() == []

    registry.release(second, linger=True)
    assert second.close_calls == 0
    assert registry.lingering_topics() == [TOPIC]
    assert len(timers.timers) == 1


def test_release_without_linger_closes_now():
    registry, timers = make_registry()
    stream = FakeStream()
    registry.register(stream)

    registry.release(stream, linger=False)

    assert stream.close_calls == 1
    assert registry.lingering_topics() == []
    assert registry.live_count(TOPIC) == 0
    assert timers.timers == []


def test_releasing_the_lingering_stream_itself_closes_it_once():
    """AC15: the lingering entry released again is removed, its timer
    cancelled and the stream closed once; the timer callback is a no-op."""
    registry, timers = make_registry()
    stream = FakeStream()
    registry.register(stream)
    registry.release(stream, linger=True)
    timer = timers.timers[-1]

    registry.release(stream, linger=False)

    assert stream.close_calls == 1
    assert timer.cancelled
    assert registry.lingering_topics() == []
    timer.callback()
    assert stream.close_calls == 1


def test_timer_factory_failure_closes_now_with_a_warning(caplog):
    def broken_factory(delay, callback):
        raise RuntimeError("no timers today")

    registry = GreengrassStreamRegistry(timer_factory=broken_factory)
    stream = FakeStream()
    registry.register(stream)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        registry.release(stream, linger=True)

    assert stream.close_calls == 1
    assert registry.lingering_topics() == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "could not arm" in warnings[0].getMessage()
    assert "no timers today" in warnings[0].getMessage()


def test_close_all_cancels_and_closes_every_lingering_stream():
    registry, timers = make_registry()
    streams = [FakeStream(topic="a/b"), FakeStream(topic="c/d")]
    live = FakeStream(topic="e/f")
    for stream in streams + [live]:
        registry.register(stream)
    for stream in streams:
        registry.release(stream, linger=True)
    assert registry.lingering_topics() == ["a/b", "c/d"]

    registry.close_all()

    assert [s.close_calls for s in streams] == [1, 1]
    assert all(timer.cancelled for timer in timers.timers)
    assert registry.lingering_topics() == []
    assert live.close_calls == 0  # live streams belong to their workers
    assert registry.live_count("e/f") == 1


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), "30", None])
def test_constructor_rejects_a_bad_linger(bad):
    with pytest.raises(ValueError):
        GreengrassStreamRegistry(linger_seconds=bad)


def test_constructor_rejects_a_non_callable_timer_factory():
    with pytest.raises(TypeError):
        GreengrassStreamRegistry(timer_factory="threading.Timer")


def test_constructor_accepts_zero_and_the_default():
    assert GreengrassStreamRegistry(linger_seconds=0).linger_seconds == 0.0
    assert GreengrassStreamRegistry().linger_seconds == GREENGRASS_STREAM_LINGER_SECONDS


# ---------------------------------------------------------------------------
# (d) The real worker on the nucleus double
# ---------------------------------------------------------------------------


def test_stopped_worker_lingers_its_last_stream_until_expiry(awsiot_stubs, manual_registry):
    """AC4: after stop() the stream stays open (no UNSUBSCRIBE), deliveries
    reach it but never the stopped worker; the timer's expiry closes
    operation and client once and the double records the UNSUBSCRIBE; a
    second expiry and a later register() change nothing."""
    registry, timers = manual_registry.registry, manual_registry.timers
    nucleus = FakeNucleus()
    delivered = []
    worker, health = make_worker(nucleus, registry, on_delivery=delivered.append)
    worker.start()
    assert health.state == HEALTH_SUBSCRIBED
    assert registry.live_count(TOPIC) == 1

    worker.stop()

    assert nucleus.open_streams(TOPIC) == 1
    assert nucleus.unsubscribes == []
    assert registry.lingering_topics() == [TOPIC]
    assert nucleus.deliver(TOPIC, b"x") == 1
    assert delivered == []

    timer = timers.timers[-1]
    timer.fire()
    client = nucleus.clients[0]
    assert client.close_calls == 1
    assert client.operations[0].closed
    assert nucleus.unsubscribes == [TOPIC]
    assert nucleus.open_streams(TOPIC) == 0

    timer.callback()
    assert client.close_calls == 1 and nucleus.unsubscribes == [TOPIC]

    second, _health = make_worker(nucleus, registry)
    second.start()
    assert client.close_calls == 1 and nucleus.unsubscribes == [TOPIC]
    second.stop()


def test_two_departures_produce_one_linger(awsiot_stubs, manual_registry):
    """AC5: the first departing stream closes at once (the second is live);
    the second lingers; no UNSUBSCRIBE until its timer fires."""
    registry, timers = manual_registry.registry, manual_registry.timers
    nucleus = FakeNucleus()
    first, _h1 = make_worker(nucleus, registry)
    second, _h2 = make_worker(nucleus, registry)
    first.start()
    second.start()
    assert nucleus.open_streams(TOPIC) == 2

    first.stop()
    assert nucleus.open_streams(TOPIC) == 1
    assert nucleus.clients[0].close_calls == 1
    assert registry.lingering_topics() == []

    second.stop()
    assert nucleus.open_streams(TOPIC) == 1
    assert registry.lingering_topics() == [TOPIC]
    assert nucleus.unsubscribes == []

    timers.timers[-1].fire()
    assert nucleus.unsubscribes == [TOPIC]
    assert nucleus.open_streams(TOPIC) == 0


def test_reconnect_teardown_closes_the_lost_stream_at_once(awsiot_stubs, manual_registry):
    """AC9: a stream the nucleus reports lost is closed by the reconnect
    immediately (nothing lingers) and the new stream is registered live;
    health goes reconnecting → subscribed."""
    registry = manual_registry.registry
    nucleus = FakeNucleus()
    health = TriggerHealth(NODE_ID, "mqtt_subscribe")
    states = record_states(health)
    holder = {}
    engine = make_synchronous_engine(health, holder)
    worker, _health = make_worker(
        nucleus, registry, on_connection_lost=engine.on_connection_lost, health=health
    )
    holder["worker"] = worker
    worker.start()
    lost = nucleus.streams[TOPIC][0]

    nucleus.lose_stream(lost)

    assert registry.lingering_topics() == []
    assert registry.live_count(TOPIC) == 1
    # connecting → subscribed (start), reconnecting (the engine), then
    # subscribed — written by the worker's reconnect and again by the
    # engine's success path, as before the fix.
    assert states[:3] == [HEALTH_CONNECTING, HEALTH_SUBSCRIBED, HEALTH_RECONNECTING]
    assert set(states[3:]) == {HEALTH_SUBSCRIBED}
    assert health.state == HEALTH_SUBSCRIBED
    assert lost.client.close_calls == 1
    assert len(nucleus.clients) == 2
    assert nucleus.streams[TOPIC][0] is not lost
    assert not engine.parked
    worker.stop()


def test_reconnect_subscribe_completing_after_stop_hands_the_stream_to_the_registry(
    awsiot_stubs, manual_registry, caplog
):
    """AC14: stop() lands while a reconnect's subscribe waits for the
    nucleus response; the late stream is not kept by the worker — it goes
    to the registry and lingers (it is the filter's last) — no subscribed
    INFO line is written, and the expiry closes it exactly once."""
    registry, timers = manual_registry.registry, manual_registry.timers
    nucleus = FakeNucleus()
    worker, _health = make_worker(nucleus, registry)
    worker.start()
    assert registry.live_count(TOPIC) == 1

    gate = nucleus.hold_next_response()
    thread = threading.Thread(target=worker.reconnect, daemon=True)
    thread.start()
    assert wait_for(lambda: nucleus.holding)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        worker.stop()
        gate.set()
        thread.join(2.0)
    assert not thread.is_alive()

    assert registry.live_count(TOPIC) == 0
    assert registry.lingering_topics() == [TOPIC]
    assert messages(caplog, "subscribed to IoT Core") == []
    assert len(nucleus.clients) == 2
    late_client = nucleus.clients[1]
    assert late_client.close_calls == 0
    assert nucleus.open_streams(TOPIC) == 1

    timer = timers.timers[-1]
    timer.fire()
    assert late_client.close_calls == 1
    assert nucleus.open_streams(TOPIC) == 0
    timer.callback()
    assert late_client.close_calls == 1


def test_worker_without_an_injected_registry_uses_the_module_default(awsiot_stubs, manual_registry):
    """The production wiring (no ``stream_registry=``) lands in
    ``DEFAULT_STREAM_REGISTRY`` — here the fixture's patched instance."""
    nucleus = FakeNucleus()
    worker = GreengrassIpcSubscriber(
        greengrass_params(),
        lambda context: True,
        lambda error: None,
        TriggerHealth(NODE_ID, "mqtt_subscribe"),
        ipc_connect=nucleus.connect,
    )
    worker.start()
    assert manual_registry.registry.live_count(TOPIC) == 1
    worker.stop()
    assert manual_registry.registry.lingering_topics() == [TOPIC]
