#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
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
"""A shadow topic subscription over the shared Greengrass IPC connection.

The awsiot v1 SDK delivers stream events (``eventstreamrpc.py:753``) and
operation responses (``:741``) on the connection's one event-loop thread. A
handler that waits on an IPC call inside the stream callback waits for a
response that thread can deliver only after the callback returns, so the call
times out (rtsp-rtmp-stream-cameras finding 23, Requirement 5.13). So the
callback only enqueues the event, and one worker thread per subscription runs
the handler, one event at a time, in arrival order (design component 20).

With ``on_active``, the subscription catches up after it becomes active: the
worker calls ``on_active()`` at the catch-up's position in the queue, after
every event enqueued before the request and before every later one. A
truthy result means done; a falsy one, or an exception, means not done.

``subscribe()`` supervises the subscription for the life of the process
(finding 24, Requirement 5.14): when its stream closes or errors, or the shared
connection is lost or replaced, it activates the subscription again on the
current connection (``utils.ipc_client.current()``), with backoff, and never
gives up. A stream lost within ``STABLE_SUBSCRIPTION_S`` of its activation is
an early death, and consecutive early deaths back off on
``SUBSCRIBE_BACKOFF_S``, as ``utils.ipc_client`` does for connections. Every
activation requests its own catch-up. An operation stays referenced until its
stream reports closed; a timed-out activation's operation is closed, never the
client (N1).
"""
import collections
import concurrent.futures
import logging
import threading
import time

import awsiot.greengrasscoreipc
from awsiot.greengrasscoreipc.client import SubscribeToIoTCoreStreamHandler
from awsiot.greengrasscoreipc.model import (
    QOS,
    SubscribeToIoTCoreRequest,
    UnauthorizedError,
)
from utils import ipc_client as shared_ipc

logger = logging.getLogger(__name__)

TIMEOUT = 10
SLEEP_TIME = 10
TOPIC_WILDCARD = "#"
#: Events a subscription keeps queued while its handler is busy; past this,
#: the oldest is dropped.
EVENT_QUEUE_MAX = 256
#: The wait before each re-activation attempt after a failed one, by the
#: number of consecutive failures (the last value repeats).
SUBSCRIBE_BACKOFF_S = (1, 2, 4, 8, 10)
#: The wait before the first re-activation after the loss of a stable stream:
#: the SDK fires the stream-closed callbacks before the connection's
#: ``on_disconnect``.
FIRST_RETRY_DELAY_S = 1
#: A stream that stays up this long after its activation is stable. One lost
#: sooner, a stream closed during its activation included, is an early death:
#: the n-th early death in a row waits ``SUBSCRIBE_BACKOFF_S[n - 1]`` before the
#: next activation, as ``utils.ipc_client`` does for connections
#: (``STABLE_CONNECTION_S``).
STABLE_SUBSCRIPTION_S = 60.0
#: Past this many operations held (their streams never reported closed), one
#: WARNING is logged.
RETAINED_OPERATIONS_WARN = 32


def _describe(error):
    """``type: text`` of an error (the type alone when its text is empty)."""
    try:
        text = str(error)
    except Exception:  # noqa: BLE001 - never let a log line stop supervision
        text = "<unprintable>"
    return "{}: {}".format(type(error).__name__, text) if text else type(error).__name__


class _Dispatcher(SubscribeToIoTCoreStreamHandler):
    """The stream handler of one activation attempt, ``sub_gen``. It runs on
    the IPC event-loop thread, so it never waits on IPC and never runs the
    wrapped handler's ``on_stream_event``: it only enqueues the event."""

    def __init__(self, owner, sub_gen):
        super().__init__()
        self._owner = owner
        self._sub_gen = sub_gen

    def on_stream_event(self, event):
        self._owner._dispatch_event(self._sub_gen, event)

    def on_stream_error(self, error):
        return self._owner._dispatch_error(self._sub_gen, error)

    def on_stream_closed(self):
        self._owner._dispatch_closed(self._sub_gen)


class SubscriptionHandler:
    """Subscribes to ``<topic_prefix>#`` and runs ``handler`` (a
    ``SubscribeToIoTCoreStreamHandler``) for every stream event, on a worker
    thread of its own.

    Two locks, never nested, and neither held across an IPC call, the
    handler or ``on_active``: ``_lock`` guards the in-memory fields, and
    ``_cond`` (a Condition with a lock of its own) guards the queue.
    """

    def __init__(self, topic_prefix, handler, publish_handler, *, on_active=None):
        self.operation = None
        self.topic_prefix = topic_prefix
        self.qos = QOS.AT_MOST_ONCE
        self.handler = handler
        self.publish_handler = publish_handler
        self._on_active = on_active
        self._topic = topic_prefix + TOPIC_WILDCARD

        self._lock = threading.Lock()
        self._attempt_sub_gen = 0  # this subscription's activation attempts
        self._newest_active = 0  # the newest attempt recorded as active
        self._closed_sub_gens = set()  # attempts whose stream closed, recorded or not
        self._active = None  # (sub_gen, connection generation) of the active stream
        self._retry_at = 0.0  # no activation attempt before this monotonic time
        self._failed_attempts = 0  # consecutive failed activations
        self._active_since = 0.0  # monotonic time the active stream was recorded active
        self._early_deaths = 0  # consecutive activations whose stream died within STABLE_SUBSCRIPTION_S
        self._ever_lost = False  # a loss was logged: a later early death repeats its episode
        self._ever_active = False
        self._retained = collections.OrderedDict()  # sub_gen -> operation, until its stream closed
        self._retained_warned = False
        self._catch_up_failed = False

        self._cond = threading.Condition(threading.Lock())
        self._queue = collections.deque(maxlen=EVENT_QUEUE_MAX)  # (seq, sub_gen, event)
        self._seq = 0
        self._catch_up_at = None  # the seq the requested catch-up runs after
        self._overflowing = False
        self._closing = False

        self._worker = None
        self._wake = threading.Event()

    def subscribe(self):
        """Keep the subscription active until :meth:`close`: activate it,
        and activate it again, with backoff, whenever its stream or its
        connection is lost (design section 3). Each activation publishes
        ``.../get`` and, with ``on_active``, requests the catch-up.

        A denied first activation (``UnauthorizedError``) still runs
        ``on_active`` once, on this thread, so pending shadow changes are
        applied at start; the denial is then raised. Every other failure, and
        a denied re-activation, is retried.
        """
        topic = self._topic
        self._start_worker()
        shared_ipc.add_connection_listener(self._on_connection_event)
        logger.info("Subscribing to topic %s", topic)
        request = SubscribeToIoTCoreRequest()
        request.topic_name = topic
        request.qos = self.qos
        while not self._is_closing():
            self._wake.clear()
            try:
                delay = self._supervise(request)
            except UnauthorizedError:
                raise  # a denied first activation ends supervision
            except Exception:  # noqa: BLE001 - supervision never stops
                logger.exception("Error supervising the subscription to %s", topic)
                delay = SLEEP_TIME
            if self._is_closing():
                break
            self._wake.wait(delay)

    def close(self):
        # To stop subscribing, close the operation stream.
        logger.info("Closing MQTT connection")
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        self._wake.set()
        with self._lock:
            operation = self.operation
            self._active = None  # supervision stops: the close below is not a loss
        if operation:
            operation.close()

    # --- supervision (the subscribing thread) -------------------------------

    def _supervise(self, request):
        """One pass: activate when there is no active stream, the connection
        is usable and the retry delay has passed; otherwise check for a missed
        replacement and retry a failed catch-up. Returns the wait."""
        with self._lock:
            active, retry_at = self._active, self._retry_at
        if active is None:
            if not shared_ipc.connection_usable():
                return SLEEP_TIME  # the "replaced" listener call wakes the supervisor
            remaining = retry_at - time.monotonic()
            if remaining > 0:
                return min(remaining, SLEEP_TIME)
            self._activate(request)
            with self._lock:
                active, retry_at = self._active, self._retry_at
            return SLEEP_TIME if active is not None else max(0.0, retry_at - time.monotonic())
        if active[1] < shared_ipc.generation():
            # A replacement whose listener call never came.
            self._stream_lost(active, "IPC connection replaced", retry_delay=0.0)
            return 0.0
        # The catch-up retry runs only while a stream is active: every
        # activation requests its own catch-up.
        with self._lock:
            failed, self._catch_up_failed = self._catch_up_failed, False
        if failed:
            with self._cond:
                self._request_catch_up()
        return SLEEP_TIME

    def _activate(self, request):
        """One activation attempt on the current connection."""
        topic = self._topic
        with self._lock:
            self._attempt_sub_gen += 1
            sub_gen = self._attempt_sub_gen
            self._closed_sub_gens = {gen for gen in self._closed_sub_gens if gen >= sub_gen}
            first = not self._ever_active
        operation = None
        try:
            client, gen = shared_ipc.current()
            try:
                operation = client.new_subscribe_to_iot_core(_Dispatcher(self, sub_gen))
            except shared_ipc.ConnectionClosedError:
                shared_ipc.report_connection_closed(
                    gen, "ConnectionClosedError from new_subscribe_to_iot_core")
                raise
            self._retain(sub_gen, operation)
            operation.activate(request)
            operation.get_response().result(TIMEOUT)
        except UnauthorizedError as error:
            if not first:
                self._attempt_failed(error)
                return
            # No stream was opened, so no event can be queued to order the
            # catch-up against: it runs here, once.
            if self._on_active is not None:
                self._run_catch_up()
            raise
        except (concurrent.futures.TimeoutError, TimeoutError) as error:
            # The Nucleus may still answer this operation and open a second
            # stream for the topic: close the operation (never the client,
            # N1); its on_stream_closed releases it.
            self._close_operation(operation)
            self._attempt_failed(error)
            return
        except Exception as error:  # noqa: BLE001 - CCE, StreamClosedError, ...: retried
            self._attempt_failed(error)
            return

        with self._lock:
            closed_during_activation = sub_gen in self._closed_sub_gens
            if closed_during_activation:
                repeated = self._schedule_after_loss_locked(0.0)  # an early death
            else:
                self._active = (sub_gen, gen)
                self._active_since = time.monotonic()
                self._newest_active = sub_gen
                self._closed_sub_gens.clear()
                self._failed_attempts = 0
                self._ever_active = True
                self.operation = operation
        if closed_during_activation:
            # The response resolved on the event-loop thread, which then ran
            # the close (eventstreamrpc.py:741, :783-794).
            self._log_loss("stream closed during activation", repeated, 0.0)
            return
        if self._is_closing():
            with self._lock:
                self._active = None  # supervision stopped: this close is not a loss
            self._close_operation(operation)  # close() may have missed it
            return

        logger.info("Get current content from topic %s", topic)
        # Publish an empty message on shadow topic to retrive the current shadow document.
        # Ref https://docs.aws.amazon.com/iot/latest/developerguide/device-shadow-mqtt.html
        try:
            self.publish_handler.publish_message(self.topic_prefix + "get", "")
        except Exception as error:  # noqa: BLE001 - the subscription stands without it
            logger.warning("Could not request the current document of %s: %s: %s",
                           topic, type(error).__name__, error)
        if self._on_active is not None:
            with self._cond:
                self._request_catch_up()
        if not first:
            logger.info("Subscribed to %s again", topic)

    def _attempt_failed(self, error):
        with self._lock:
            self._failed_attempts += 1
            attempt = self._failed_attempts
            delay = SUBSCRIBE_BACKOFF_S[min(attempt - 1, len(SUBSCRIBE_BACKOFF_S) - 1)]
            self._retry_at = time.monotonic() + delay
        logger.warning("Could not subscribe to %s (attempt %d): %s; retrying in %g s",
                       self._topic, attempt, _describe(error), delay)

    def _retain(self, sub_gen, operation):
        """Keep ``operation`` referenced until its stream reports closed."""
        with self._lock:
            self._retained[sub_gen] = operation
            held = len(self._retained)
            warn = held > RETAINED_OPERATIONS_WARN and not self._retained_warned
            if warn:
                self._retained_warned = True
        if warn:
            logger.warning("Holding %d operations of the IPC subscription to %s whose streams "
                           "never reported closed", held, self._topic)

    def _close_operation(self, operation):
        """Close one operation (never the client), quietly."""
        if operation is None:
            return
        try:
            operation.close()
        except Exception as error:  # noqa: BLE001 - the operation stays retained
            logger.debug("Could not close an operation of %s: %s", self._topic, _describe(error))

    def _stream_lost(self, active, reason, retry_delay=None):
        """Mark the active stream ``active`` lost, if it still is, and log the
        loss (:meth:`_log_loss`); the supervisor activates again after the
        delay."""
        with self._lock:
            lost = active is not None and self._active == active
            if lost:
                repeated, lived = self._lose_active_locked(retry_delay)
        if lost:
            self._log_loss(reason, repeated, lived)
        self._wake.set()

    def _lose_active_locked(self, retry_delay=None):
        """Clear the active stream and schedule the next activation. Returns
        ``(repeated, lived)``: whether the loss repeats its episode's, and
        how long the stream was up."""
        lived = time.monotonic() - self._active_since
        self._active = None
        return self._schedule_after_loss_locked(lived, retry_delay), lived

    def _schedule_after_loss_locked(self, lived, retry_delay=None):
        """Count one lost activation whose stream was up ``lived`` seconds,
        and set the retry time; ``retry_delay``, when given, is the wait (a
        missed replacement: none). Returns whether the loss repeats an
        episode's.

        As ``utils.ipc_client``'s ``_early_deaths``: a stream lost within
        ``STABLE_SUBSCRIPTION_S`` of its activation is an early death, and
        the n-th in a row waits ``SUBSCRIBE_BACKOFF_S[n - 1]``; the loss of a
        stream that stayed up longer resets the count and waits
        ``FIRST_RETRY_DELAY_S``. An episode starts with a loss and ends when a
        stream outlives the window: every early death after its first loss
        repeats it."""
        early = lived < STABLE_SUBSCRIPTION_S
        self._early_deaths = self._early_deaths + 1 if early else 0
        repeated = early and self._ever_lost
        self._ever_lost = True
        if retry_delay is None:
            n = self._early_deaths
            retry_delay = (SUBSCRIBE_BACKOFF_S[min(n - 1, len(SUBSCRIBE_BACKOFF_S) - 1)]
                           if n else FIRST_RETRY_DELAY_S)
        self._retry_at = time.monotonic() + retry_delay
        return repeated

    def _log_loss(self, reason, repeated, lived):
        """ERROR for the first loss of an episode, naming the topic. A repeated
        one is a re-subscription whose stream died early, a failed attempt in
        Requirement 5.14's terms: a WARNING, at the backoff rate."""
        if repeated:
            logger.warning("Lost the IPC subscription to %s again (%s), %.1f s after subscribing",
                           self._topic, reason, lived)
        else:
            logger.error("Lost the IPC subscription to %s (%s)", self._topic, reason)

    def _on_connection_event(self, kind, gen):
        """The shared connection's listener (the reconnect thread; never
        blocks). ``"lost"`` marks a stream on that connection lost;
        ``"replaced"`` clears the retry delay, so the supervisor activates at
        once."""
        if kind == "lost":
            with self._lock:
                active = self._active
            if active is not None and active[1] == gen:
                self._stream_lost(active, "IPC connection {} lost".format(gen))
                return
        elif kind == "replaced":
            with self._lock:
                self._retry_at = 0.0
        self._wake.set()

    # --- the queue (under self._cond) -------------------------------------

    def _enqueue(self, sub_gen, event):    # event-loop thread, under self._cond
        full = len(self._queue) == EVENT_QUEUE_MAX
        starts_episode = full and not self._overflowing
        self._seq += 1
        self._queue.append((self._seq, sub_gen, event))  # deque(maxlen=EVENT_QUEUE_MAX = 256) drops the oldest
        if full:
            self._overflowing = True
            if self._on_active is not None:
                self._request_catch_up()   # positioned after the new event
        self._cond.notify()
        return starts_episode              # the dispatcher logs the overflow ERROR after releasing _cond

    def _request_catch_up(self):           # under self._cond: supervisor, overflow, catch-up retry
        self._catch_up_at = self._seq      # every event enqueued so far runs first
        self._cond.notify()

    def _is_closing(self):
        with self._cond:
            return self._closing

    # --- the dispatcher's callbacks (IPC event-loop thread) ---------------

    def _dispatch_event(self, sub_gen, event):
        with self._lock:
            stale = sub_gen < self._newest_active
        if stale:
            logger.debug("Dropping an event of %s from an older stream (attempt %d)",
                         self._topic, sub_gen)
            return
        with self._cond:
            starts_episode = self._enqueue(sub_gen, event)
        if starts_episode:
            logger.error("Dropping queued events of %s: its handler is busy (queue full at %d)",
                         self._topic, EVENT_QUEUE_MAX)

    def _dispatch_error(self, sub_gen, error):
        """The wrapped handler's ``on_stream_error``, which only logs; its
        result tells the SDK whether to close the stream, and an exception
        counts as True. Only then (truthy or None, ``eventstreamrpc.py:773-776``)
        is the stream reported lost."""
        try:
            result = self.handler.on_stream_error(error)
        except Exception:  # noqa: BLE001 - counts as True
            logger.exception("Error handling a stream error of %s", self._topic)
            result = True
        if result or result is None:
            self._report_loss(sub_gen, "stream error: {}".format(type(error).__name__))
        return result

    def _dispatch_closed(self, sub_gen):
        try:
            self.handler.on_stream_closed()
        except Exception:  # noqa: BLE001 - callback isolation
            logger.exception("Error handling the stream close of %s", self._topic)
        self._report_loss(sub_gen, "stream closed", release=True)

    def _report_loss(self, sub_gen, reason, release=False):
        """Attempt ``sub_gen``'s stream closed or errored: recorded for the
        current attempt whether or not it is active yet (a close that lands
        before its activation is recorded is caught there), and the active
        stream is marked lost. ``release`` drops its retained operation."""
        with self._lock:
            if release:
                self._retained.pop(sub_gen, None)
            if sub_gen >= self._attempt_sub_gen:
                self._closed_sub_gens.add(sub_gen)
            lost = self._active is not None and self._active[0] == sub_gen
            if lost:
                repeated, lived = self._lose_active_locked()
        if lost:
            self._log_loss(reason, repeated, lived)
        self._wake.set()

    # --- the worker ---------------------------------------------------------

    def _start_worker(self):
        with self._cond:
            if self._worker is not None:
                return
            worker = self._worker = threading.Thread(
                target=self._work, name="subscription-worker", daemon=True)
        worker.start()

    def _work(self):
        """Run events and catch-ups one at a time, in queue order, until
        :meth:`close`."""
        while True:
            with self._cond:
                self._cond.wait_for(
                    lambda: self._queue or self._catch_up_at is not None or self._closing)
                if self._closing:
                    return
                catch_up = self._catch_up_at is not None and (
                    not self._queue or self._queue[0][0] > self._catch_up_at)
                if catch_up:
                    self._catch_up_at = None
                else:
                    _, _, event = self._queue.popleft()
                    if not self._queue:
                        self._overflowing = False  # the episode ends when the queue empties
            # No lock held from here.
            if catch_up:
                self._run_catch_up()
                continue
            try:
                self.handler.on_stream_event(event)
            except Exception:  # noqa: BLE001 - handler isolation: the next event still runs
                logger.exception("Error handling a %s event", self._topic)

    def _run_catch_up(self):
        """``on_active()``; an exception counts as not done."""
        try:
            done = bool(self._on_active())
        except Exception:  # noqa: BLE001 - counts as False
            logger.exception("Error catching up %s", self._topic)
            done = False
        with self._lock:
            self._catch_up_failed = not done
