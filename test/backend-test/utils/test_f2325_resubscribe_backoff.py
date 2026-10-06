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
"""Re-subscription backs off across activations whose stream dies early
(rtsp-rtmp-stream-cameras task 30 follow-up, review item 3; Requirement 5.14;
design component 20, **Subscription failures and losses**).

A stream the Nucleus accepts and then closes, with the connection intact, is
activated again on the ``SUBSCRIBE_BACKOFF_S`` ladder (1, 2, 4, 8, 10 s, here
in tenths of a second), for ever, as ``utils.ipc_client``'s ``_early_deaths``
does for connections:
- each loss within ``STABLE_SUBSCRIPTION_S`` of its activation climbs the
  ladder, a stream closed during its activation included;
- the loss of a stream that stayed up past it starts again at
  ``FIRST_RETRY_DELAY_S``, and the next early loss at the ladder's foot;
- the first loss of an episode logs an ERROR, and every repeated one a
  WARNING;
- a replaced connection is still subscribed at once, whatever the ladder.

The fake IPC model is ``f2325_ipc_support``; the timing is scaled 1:10.
"""
import logging
import threading
import time

import pytest

from f2325_ipc_support import (
    CLOSED_DURING_ACTIVATION,
    GET_TOPIC,
    PREFIX,
    RecordingPublisher,
    RecordingStreamHandler,
    fast_timings,
    install,
    settle,
    uninstall,
)

from mqtt import SubscriptionHandler as subscription_module
from utils import ipc_client as shared_ipc

TOPIC = PREFIX + "#"
#: ``SUBSCRIBE_BACKOFF_S`` (1, 2, 4, 8, 10 s) and ``FIRST_RETRY_DELAY_S`` (1 s), 1:10.
LADDER_S = (0.1, 0.2, 0.4, 0.8, 1.0)
FIRST_RETRY_S = 0.1
#: ``STABLE_SUBSCRIPTION_S`` for these tests.
STABLE_S = 0.5
#: Scheduling slack allowed on top of a scheduled wait.
SLACK_S = 0.3


@pytest.fixture
def world(monkeypatch):
    fake = install(monkeypatch)
    fast_timings(monkeypatch, subscription_module, FIRST_RETRY_DELAY_S=FIRST_RETRY_S,
                 SUBSCRIBE_BACKOFF_S=LADDER_S)
    # Set on its own, without raising, so this file also runs (red) on the tree before the fix.
    monkeypatch.setattr(subscription_module, "STABLE_SUBSCRIPTION_S", STABLE_S, raising=False)
    try:
        yield fake
    finally:
        uninstall(fake)


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG, logger="mqtt.SubscriptionHandler")
    return caplog


def _subscription(caplog, level):
    return [record.getMessage() for record in caplog.records
            if record.name == "mqtt.SubscriptionHandler" and record.levelno == level]


def _lost(caplog, level):
    return [line for line in _subscription(caplog, level) if line.startswith("Lost the IPC subscription")]


def _first_loss(reason):
    return "Lost the IPC subscription to {} ({})".format(TOPIC, reason)


def _is_repeated_loss(line, reason):
    """The WARNING of a repeated loss: ``... again (<reason>), <s> s after subscribing``."""
    prefix = "Lost the IPC subscription to {} again ({}), ".format(TOPIC, reason)
    return line.startswith(prefix) and line.endswith(" s after subscribing")


class Running:
    """A ``SubscriptionHandler`` whose ``subscribe()`` runs on a daemon thread
    until ``close()``; ``__enter__`` waits for its first activation."""

    def __init__(self, world):
        self.world = world
        self.publisher = RecordingPublisher()
        self.subscription = subscription_module.SubscriptionHandler(
            PREFIX, RecordingStreamHandler(), self.publisher)
        self.errors = []
        self.thread = threading.Thread(target=self._subscribe, name="f2325-subscribe", daemon=True)

    def _subscribe(self):
        try:
            self.subscription.subscribe()
        except Exception as error:  # noqa: BLE001 - reported by the test
            self.errors.append(error)

    def __enter__(self):
        self.thread.start()
        assert self.wait_activated(1), (
            "setup: the subscription to {} never activated (errors: {})".format(TOPIC, self.errors))
        return self

    def __exit__(self, *exc_info):
        self.subscription.close()
        self.thread.join(5)
        return False

    def wait_activated(self, count, timeout=5.0):
        """True once ``count`` activations completed: each publishes ``.../get``."""
        return self.publisher.wait_for(GET_TOPIC, count, timeout)

    def close_newest_stream(self, count):
        """The Nucleus closes the stream of activation ``count``, on the loop
        thread. Returns the wait from the close to the start of the next
        activation."""
        operation = self.subscription.operation
        closed_at = time.monotonic()
        assert settle([operation.client.submit(operation.deliver_stream_closed)])
        assert self.wait_activated(count + 1), "activation {} never came".format(count + 1)
        return self.subscription.operation.activated_at - closed_at


def _assert_waits(waits, expected):
    assert len(waits) == len(expected), (waits, expected)
    for wait, delay in zip(waits, expected):
        assert delay * 0.9 <= wait < delay + SLACK_S, (
            "the waits before each re-activation were {} s, not {} s".format(
                [round(w, 3) for w in waits], list(expected)))


def test_a_stream_accepted_then_closed_each_time_waits_1_2_4_8_10_s_for_ever(world, logs):
    with Running(world) as running:
        waits = [running.close_newest_stream(count) for count in range(1, 7)]
        assert running.errors == []

    _assert_waits(waits, LADDER_S + (LADDER_S[-1],))
    assert _lost(logs, logging.ERROR) == [_first_loss("stream closed")]
    repeated = _lost(logs, logging.WARNING)
    assert len(repeated) == 5 and all(_is_repeated_loss(line, "stream closed") for line in repeated), repeated
    assert _subscription(logs, logging.INFO).count("Subscribed to {} again".format(TOPIC)) == 6


def test_a_stream_that_stays_up_past_the_window_starts_the_ladder_again(world, logs):
    with Running(world) as running:
        early = [running.close_newest_stream(count) for count in (1, 2, 3)]
        time.sleep(STABLE_S + 0.1)  # activation 4 stays up past STABLE_SUBSCRIPTION_S
        stable = running.close_newest_stream(4)
        again = [running.close_newest_stream(count) for count in (5, 6)]

    _assert_waits(early + [stable] + again, LADDER_S[:3] + (FIRST_RETRY_S,) + LADDER_S[:2])
    # Two episodes: losses 1-3, then the stable stream's loss 4 and losses 5-6.
    assert _lost(logs, logging.ERROR) == [_first_loss("stream closed")] * 2
    repeated = _lost(logs, logging.WARNING)
    assert len(repeated) == 4 and all(_is_repeated_loss(line, "stream closed") for line in repeated), repeated


def test_streams_closed_during_their_activation_climb_the_ladder_too(world, logs):
    with Running(world) as running:
        first = running.subscription.operation
        world.script("subscribe_to_iot_core", CLOSED_DURING_ACTIVATION, CLOSED_DURING_ACTIVATION)
        closed_at = time.monotonic()
        assert settle([first.client.submit(first.deliver_stream_closed)])
        assert running.wait_activated(2)
        attempts = world.subscribe_attempts()

    assert len(attempts) == 4
    _assert_waits([attempts[1].activated_at - closed_at,
                   attempts[2].activated_at - attempts[1].responded_at,
                   attempts[3].activated_at - attempts[2].responded_at], LADDER_S[:3])
    assert _lost(logs, logging.ERROR) == [_first_loss("stream closed")]
    repeated = _lost(logs, logging.WARNING)
    assert len(repeated) == 2 and all(
        _is_repeated_loss(line, "stream closed during activation") for line in repeated), repeated


def test_a_replaced_connection_is_subscribed_at_once_whatever_the_ladder(world, logs):
    """The connection recovery is unchanged: its ``"replaced"`` call clears
    the retry delay, so a stream lost with its connection is made again on the
    new one as soon as it is adopted, even with the ladder at 10 s."""
    with Running(world) as running:
        for count in range(1, 6):  # five early losses: the next wait would be the ladder's top
            running.close_newest_stream(count)
        operation = running.subscription.operation
        client, generation = operation.client, shared_ipc.generation()
        closed_at = time.monotonic()
        assert settle([client.close_connection(ConnectionResetError("AWS_IO_SOCKET_CLOSED"))])
        assert running.wait_activated(7)
        moved = running.subscription.operation
        assert running.errors == []

    assert moved.client is not client and shared_ipc.generation() == generation + 1
    assert moved.activated_at - closed_at < LADDER_S[-1] / 2, (
        "the re-activation on the new connection waited {:.3f} s".format(moved.activated_at - closed_at))
