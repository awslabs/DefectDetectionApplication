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
"""``mqtt.SubscriptionHandler`` handles shadow events off the IPC callback
(rtsp-rtmp-stream-cameras task 30.2; Requirement 5.13; design component 20):
the stream callback only enqueues, and one worker per subscription runs the
handler and the ``on_active`` catch-up, one at a time, in queue order.

- Events run in order per subscription, and two subscriptions never wait on
  each other; the event-loop thread is never held by a handler.
- A handler exception is logged and the next event still runs (R1).
- Overflow: the oldest events are dropped, one ERROR per episode, and a
  catch-up is requested after the newest event.
- The catch-up position: after every event enqueued before the request,
  before every later one; at once on an empty queue; a second request while
  one waits moves it, so one catch-up runs.
- An ``on_active`` that raises counts as not done and is logged.
- A denied first activation runs ``on_active`` once, then raises.
- ``close()`` stops the worker, and ``subscribe()`` returns.

The fake IPC client (``f2325_ipc_support``, plan P4) delivers every response
and stream event on one fake event-loop thread, as the SDK does.
"""
import logging
import threading

import pytest
from awsiot.greengrasscoreipc.model import UnauthorizedError

from f2325_ipc_support import (
    GET_TOPIC,
    PREFIX,
    CatchUp,
    Journal,
    RecordingPublisher,
    RecordingStreamHandler,
    delta_event,
    eventually,
    install,
    settle,
    uninstall,
)

from mqtt import SubscriptionHandler as subscription_module

TOPIC = PREFIX + "#"
OTHER_PREFIX = "$aws/things/thing-f2325/shadow/name/dda-user-accounts/update/"
#: The callback budget of AC 1.
CALLBACK_BUDGET_S = 0.05


@pytest.fixture
def world(monkeypatch):
    fake = install(monkeypatch)
    try:
        yield fake
    finally:
        uninstall(fake)


class Running:
    """A ``SubscriptionHandler`` whose ``subscribe()`` runs on a daemon
    thread until ``close()``."""

    def __init__(self, world, prefix=PREFIX, on_active=None, journal=None):
        self.world = world
        self.prefix = prefix
        self.journal = journal if journal is not None else Journal()
        self.handler = RecordingStreamHandler(self.journal)
        self.publisher = RecordingPublisher()
        kwargs = {} if on_active is None else {"on_active": on_active}
        self.subscription = subscription_module.SubscriptionHandler(
            prefix, self.handler, self.publisher, **kwargs)
        self.errors = []
        self.thread = threading.Thread(target=self._subscribe, name="f2325-subscribe", daemon=True)

    def _subscribe(self):
        try:
            self.subscription.subscribe()
        except Exception as error:  # noqa: BLE001 - reported by the test
            self.errors.append(error)

    def __enter__(self):
        self.thread.start()
        assert self.publisher.wait_for(self.prefix + "get", 1, 2.0), (
            "setup: the subscription to {} never activated (errors: {})".format(self.prefix, self.errors))
        [self.operation] = [op for op in self.world.activations()
                            if op.request.topic_name == self.prefix + "#"]
        return self

    def __exit__(self, *exc_info):
        self.subscription.close()
        self.thread.join(5)
        return False

    def deliver(self, *names):
        """Deliver one event per name on the loop thread; wait until each
        loop task returned. Returns the loop tasks' hold times."""
        client = self.operation.client
        futures = [client.deliver(self.operation, delta_event(name, self.prefix)) for name in names]
        assert settle(futures, 10.0), "the loop never ran the deliveries"
        held = []
        for future in futures:
            started, returned, error = future.result()
            assert error is None, "the stream callback raised {!r}".format(error)
            held.append(returned - started)
        return held

    def request_catch_up(self):
        with self.subscription._cond:
            self.subscription._request_catch_up()


def _names(prefix, first, last):
    return ["{}{}".format(prefix, n) for n in range(first, last + 1)]


# --- order and isolation ------------------------------------------------------------------


def test_events_run_in_order_and_subscriptions_never_wait_on_each_other(world):
    """Subscription A's handler is blocked on its first event; B's events
    still run at once, the callback returns at once for both, and A then runs
    its events in arrival order on its own worker thread."""
    with Running(world) as a, Running(world, prefix=OTHER_PREFIX) as b:
        entered = a.handler.gate("a1")
        held = a.deliver("a1")
        assert entered.wait(2.0), "A's worker never started a1"
        held += a.deliver("a2", "a3", "a4", "a5")
        held += b.deliver("b1", "b2", "b3")

        assert b.journal.wait_for_count(3, 2.0), "B waited on A's busy handler: {}".format(
            b.journal.snapshot())
        assert b.journal.snapshot() == ["b1", "b2", "b3"]
        assert a.journal.snapshot() == []
        assert max(held) < CALLBACK_BUDGET_S, (
            "a stream callback held the IPC event-loop thread for {:.3f} s".format(max(held)))

        a.handler.release("a1")
        assert a.journal.wait_for_count(5, 2.0)
        assert a.journal.snapshot() == ["a1", "a2", "a3", "a4", "a5"]
        assert a.handler.threads == {"subscription-worker"}
        assert b.handler.threads == {"subscription-worker"}


def test_a_handler_exception_is_logged_and_the_next_event_still_runs(world, caplog):
    caplog.set_level(logging.ERROR, logger="mqtt.SubscriptionHandler")
    with Running(world) as running:
        running.handler.raises.add("e1")
        running.deliver("e1", "e2")
        assert running.journal.wait_for_count(1, 2.0)
        assert running.journal.snapshot() == ["e2"]

    [record] = [r for r in caplog.records if r.name == "mqtt.SubscriptionHandler"
                and r.getMessage().startswith("Error handling a")]
    assert record.getMessage() == "Error handling a {} event".format(TOPIC)
    assert record.exc_info is not None and "the handler failed on e1" in str(record.exc_info[1])


# --- overflow -------------------------------------------------------------------------------


def test_overflow_drops_the_oldest_logs_once_per_episode_and_requests_a_catch_up(world, caplog):
    caplog.set_level(logging.ERROR, logger="mqtt.SubscriptionHandler")
    cap = subscription_module.EVENT_QUEUE_MAX
    assert cap == 256
    journal = Journal()
    catch_up = CatchUp(journal)
    with Running(world, on_active=catch_up, journal=journal) as running:
        assert journal.wait_for_count(1, 2.0) and journal.snapshot() == ["catch-up"], (
            "setup: the activation's catch-up did not run first")

        # Episode 1: the worker holds e0; 256 + 4 more events overflow by 4.
        entered = running.handler.gate("e0")
        running.deliver("e0")
        assert entered.wait(2.0)
        running.deliver(*_names("e", 1, cap + 4))
        overflow = [r for r in caplog.records if r.getMessage().startswith("Dropping queued events")]
        assert [r.getMessage() for r in overflow] == [
            "Dropping queued events of {}: its handler is busy (queue full at 256)".format(TOPIC)]
        assert overflow[0].levelno == logging.ERROR
        running.handler.release("e0")

        expected = ["catch-up", "e0"] + _names("e", 5, cap + 4) + ["catch-up"]
        assert journal.wait_for_count(len(expected), 5.0), journal.snapshot()[-5:]
        assert journal.snapshot() == expected, "the oldest 4 were not the ones dropped, or the " \
            "catch-up did not run after the newest event"

        # Episode 2, after the queue emptied: logged again.
        entered = running.handler.gate("f0")
        running.deliver("f0")
        assert entered.wait(2.0)
        running.deliver(*_names("f", 1, cap + 1))
        running.handler.release("f0")
        expected += ["f0"] + _names("f", 2, cap + 1) + ["catch-up"]
        assert journal.wait_for_count(len(expected), 5.0)
        assert journal.snapshot() == expected

    overflow = [r for r in caplog.records if r.getMessage().startswith("Dropping queued events")]
    assert len(overflow) == 2, "one ERROR per overflow episode"
    assert catch_up.threads == ["subscription-worker"] * 3


def test_overflow_without_on_active_requests_no_catch_up(world):
    cap = subscription_module.EVENT_QUEUE_MAX
    with Running(world) as running:
        entered = running.handler.gate("e0")
        running.deliver("e0")
        assert entered.wait(2.0)
        running.deliver(*_names("e", 1, cap + 1))
        running.handler.release("e0")
        expected = ["e0"] + _names("e", 2, cap + 1)
        assert running.journal.wait_for_count(len(expected), 5.0)
        assert running.journal.snapshot() == expected


# --- the catch-up position ----------------------------------------------------------------------


def test_a_catch_up_runs_after_the_events_enqueued_before_its_request(world):
    journal = Journal()
    with Running(world, on_active=CatchUp(journal), journal=journal) as running:
        assert journal.wait_for_count(1, 2.0)
        entered = running.handler.gate("e1")
        running.deliver("e1")
        assert entered.wait(2.0)
        running.deliver("e2", "e3")
        running.request_catch_up()
        running.deliver("e4", "e5")
        running.handler.release("e1")

        assert journal.wait_for_count(7, 2.0)
        assert journal.snapshot() == ["catch-up", "e1", "e2", "e3", "catch-up", "e4", "e5"]


def test_a_catch_up_requested_on_an_empty_queue_runs_at_once(world):
    journal = Journal()
    catch_up = CatchUp(journal)
    with Running(world, on_active=catch_up, journal=journal) as running:
        assert journal.wait_for_count(1, 2.0) and catch_up.calls == 1
        running.request_catch_up()
        assert journal.wait_for_count(2, 1.0), "the catch-up waited for an event"
        assert journal.snapshot() == ["catch-up", "catch-up"]


def test_a_second_request_while_one_waits_moves_it_so_one_catch_up_runs(world):
    journal = Journal()
    catch_up = CatchUp(journal)
    with Running(world, on_active=catch_up, journal=journal) as running:
        assert journal.wait_for_count(1, 2.0)
        entered = running.handler.gate("e1")
        running.deliver("e1")
        assert entered.wait(2.0)
        running.deliver("e2")
        running.request_catch_up()
        running.deliver("e3")
        running.request_catch_up()
        running.deliver("e4")
        running.handler.release("e1")

        assert journal.wait_for_count(6, 2.0)
        assert not journal.wait_for_count(7, 0.2), journal.snapshot()
        assert journal.snapshot() == ["catch-up", "e1", "e2", "e3", "catch-up", "e4"]
        assert catch_up.calls == 2


def test_the_catch_up_is_requested_after_the_activation_and_runs_on_the_worker(world):
    """With ``on_active``, ``subscribe()`` requests one catch-up after the
    first activation and its ``.../get``: the worker runs it, once."""
    journal = Journal()
    catch_up = CatchUp(journal)
    with Running(world, on_active=catch_up, journal=journal) as running:
        assert journal.wait_for_count(1, 2.0)
        assert not journal.wait_for_count(2, 0.2)
        assert catch_up.threads == ["subscription-worker"]
        assert running.publisher.times(GET_TOPIC), "the .../get was not published"


# --- on_active's result -------------------------------------------------------------------------


def test_an_on_active_that_raises_counts_as_false_and_is_logged(world, caplog):
    caplog.set_level(logging.ERROR, logger="mqtt.SubscriptionHandler")
    journal = Journal()
    catch_up = CatchUp(journal, RuntimeError("the shadow GET failed"), False, True)
    with Running(world, on_active=catch_up, journal=journal) as running:
        assert journal.wait_for_count(1, 2.0)
        assert eventually(lambda: running.subscription._catch_up_failed, 1.0)
        [record] = [r for r in caplog.records if r.getMessage().startswith("Error catching up")]
        assert record.getMessage() == "Error catching up {}".format(TOPIC)
        assert record.exc_info is not None

        running.request_catch_up()  # returns False: still failed
        assert journal.wait_for_count(2, 2.0)
        assert eventually(lambda: catch_up.calls == 2, 1.0)
        assert running.subscription._catch_up_failed is True

        running.request_catch_up()  # returns True: done
        assert journal.wait_for_count(3, 2.0)
        assert eventually(lambda: running.subscription._catch_up_failed is False, 1.0)

        running.deliver("e1")
        assert journal.wait_for_count(4, 2.0), "the worker stopped after on_active raised"


def test_a_denied_first_activation_runs_on_active_once_then_raises(world):
    world.outcomes["subscribe_to_iot_core"] = UnauthorizedError(message="not authorized")
    journal = Journal()
    catch_up = CatchUp(journal)
    publisher = RecordingPublisher()
    subscription = subscription_module.SubscriptionHandler(
        PREFIX, RecordingStreamHandler(journal), publisher, on_active=catch_up)
    try:
        with pytest.raises(UnauthorizedError):
            subscription.subscribe()
        assert catch_up.calls == 1
        assert catch_up.threads == [threading.current_thread().name], (
            "on_active did not run on the subscribing thread")
        assert subscription._catch_up_failed is False
        assert publisher.published == []
        assert not journal.wait_for_count(2, 0.2)
    finally:
        subscription.close()


def test_a_denied_first_activation_without_on_active_raises(world):
    world.outcomes["subscribe_to_iot_core"] = UnauthorizedError(message="not authorized")
    subscription = subscription_module.SubscriptionHandler(
        PREFIX, RecordingStreamHandler(), RecordingPublisher())
    try:
        with pytest.raises(UnauthorizedError):
            subscription.subscribe()
    finally:
        subscription.close()


# --- the dispatcher and close() -------------------------------------------------------------------


@pytest.mark.parametrize("result, expected", [(True, True), (False, False), (None, None),
                                              (RuntimeError("boom"), True)])
def test_the_dispatcher_forwards_a_stream_error_and_returns_its_result(world, result, expected):
    """``on_stream_error`` runs the wrapped handler's on the loop thread and
    returns its result; an exception counts as True."""
    with Running(world) as running:
        running.handler.error_result = result
        future = running.operation.client.fail_stream(running.operation, RuntimeError("reset"))
        assert settle([future], 2.0)
        assert future.result() is expected
        assert len(running.handler.errors) == 1


def test_the_dispatcher_forwards_the_stream_close(world):
    with Running(world) as running:
        future = running.operation.client.submit(running.operation.deliver_stream_closed)
        assert settle([future], 2.0)
        assert running.handler.closes == 1


def test_close_stops_the_worker_and_subscribe_returns(world):
    running = Running(world)
    with running:
        worker = running.subscription._worker
        assert worker is not None and worker.is_alive() and worker.daemon
    # __exit__ closed it and joined the subscribing thread.
    assert not running.thread.is_alive(), "subscribe() did not return after close()"
    assert eventually(lambda: not worker.is_alive(), 2.0), "the worker kept running after close()"
    assert running.errors == []
    assert running.operation.close_calls == 1
    assert eventually(lambda: running.handler.closes == 1, 2.0)
    running.deliver("late")
    assert not running.journal.wait_for_count(1, 0.2), "an event ran after close()"
