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
"""Process-wide shared Greengrass IPC connection (DD-19576), recoverable
(rtsp-rtmp-stream-cameras finding 24, Requirement 5.14, design component 20).

Why one shared connection
-------------------------
The backend talks to the Greengrass Nucleus over the local IPC socket
(``awsiot.greengrasscoreipc``). Call sites used to open a fresh connection per
call: ``utils.gg_utils`` connected and ``close()``d on every operation, and
``utils.feature_configs_utils`` connected and never closed, leaving each client
to be finalized by the garbage collector while the native event loop could
still be tearing down continuations. Both patterns drive a reference-counting
bug in the bundled ``aws-c-event-stream`` that aborts the whole process
(exit 255)::

    Fatal error condition occurred in .../event_stream_rpc_client.c:961:
    ref_count != 0 && "Continuation ref count has gone negative"

So every IPC caller reuses one long-lived connection.

Why it must recover
-------------------
The awsiot v1 SDK never reconnects: ``greengrasscoreipc.connect()`` builds a
new ``Connection`` on each call, and once a connection closes every ``new_*``
raises ``ConnectionClosedError`` (``eventstreamrpc.py:503-507``). On thor1 the
shared connection closed twice and nothing recovered it: the holders built at
import kept the closed client.

How it recovers
---------------
* :func:`get_ipc_client` returns one stable handle, :class:`SharedIpcClient`.
  It forwards every ``new_*`` call to the current client, so a holder built at
  import (``server_setup``, ``endpoints/system.py``, the ``local_auth`` reader)
  reaches the client adopted after a reconnect.
* Only three signals mean "closed": the adopted connection's lifecycle
  ``on_disconnect``; a ``ConnectionClosedError`` through the handle or
  :func:`call_with_ipc_retry`; and a watchdog that reads the SDK's connection
  state. A denial, a not-found, a validation error or a timeout never
  reconnects. :func:`report_connection_closed` marks a loss, once per
  connection generation.
* One ``ipc-reconnect`` daemon thread opens a fresh connection per loss, with
  backoff while the Nucleus refuses, and never gives up. At most one attempt is
  pending: a slow one is waited on, never abandoned, because awscrt keeps a
  timed-out connection alive with no references.

N1: no client is ever closed, and nothing reconnects per call. A replaced
client, and every retired attempt, stays referenced for the life of the
process, so the garbage collector never finalizes one while operations may
still be live on it (one ``AwsEventLoop`` thread per loss, the explicit N2
exception). :meth:`SharedIpcClient.close` only logs a WARNING.

Usage
-----
    from utils.ipc_client import get_ipc_client
    client = get_ipc_client()          # the shared handle; connects once
    op = client.new_list_components()
    ...

Callers MUST NOT close the client. :func:`call_with_ipc_retry` retries an
operation once after a closed connection. :func:`reset_ipc_client` stays for
tests and manual recovery; nothing in production calls it.
"""
import concurrent.futures
import itertools
import logging
import os
import threading
import time
from concurrent.futures import Future

import awsiot.greengrasscoreipc

try:
    from awsiot.eventstreamrpc import ConnectionClosedError
except ImportError:  # the SDK is stubbed (tests)
    class ConnectionClosedError(RuntimeError):
        """Stands in for ``awsiot.eventstreamrpc.ConnectionClosedError`` when
        the SDK is stubbed."""

logger = logging.getLogger(__name__)

# Timing, in seconds. Module attributes read at use time, so tests patch them
# to milliseconds.
CONNECT_TIMEOUT_S = 10.0
WATCHDOG_INTERVAL_S = 10.0
RECONNECT_BACKOFF_S = (1, 2, 4, 8, 10)
STABLE_CONNECTION_S = 60.0
RETRY_WAIT_S = 10.0

#: The SDK connection states in which every ``new_*`` raises
#: ``ConnectionClosedError`` once a connection was up (eventstreamrpc.py:503-507).
_CLOSED_STATES = ("DISCONNECTED", "DISCONNECTING")

# --- state, guarded by _lock --------------------------------------------------------
# _lock is never held across a connect, an IPC call, a log call or a listener.
_lock = threading.Lock()
_cond = threading.Condition(_lock)
_client = None          # the adopted client
_token = None           # the adopted client's attempt token
_generation = 0         # +1 per adoption, never reset
_connected_at = 0.0     # monotonic time of the current adoption
_lost = False           # the current generation is marked lost ...
_lost_at = 0.0          # ... since then ...
_lost_reason = None     # ... for this reason
_pending_token = None   # the reconnect attempt that may still be adopted
_watchdog_on = True     # the state watchdog's switch for the current client
_epoch = 0              # +1 per reset
_early_deaths = 0       # consecutive adopted connections that died within STABLE_CONNECTION_S
_retired = []           # every replaced client and retired attempt, kept for life (N1)
_listeners = []         # cb(kind, generation), kind "lost" or "replaced"

# --- outside _lock ------------------------------------------------------------------
_connect_lock = threading.Lock()  # serializes the first and lazy connects
_wake = threading.Event()         # set only when a loss is newly marked
_tokens = itertools.count(1)      # connection attempt tokens
_reconnect_thread = None          # under _connect_lock


def _describe(error):
    """``type: text`` of an error (the type alone when its text is empty)."""
    try:
        text = str(error)
    except Exception:  # noqa: BLE001 - never let a log line fail the reconnect thread
        text = "<unprintable>"
    return "{}: {}".format(type(error).__name__, text) if text else type(error).__name__


def _type_name(value):
    return "None" if value is None else type(value).__name__


def _still_wanted(epoch0, lost_gen):
    """With ``_lock`` held: the loss the reconnect thread is handling still
    needs a connection (no reset and no adoption since)."""
    return _epoch == epoch0 and _generation == lost_gen and _lost and _client is not None


def _adopt(client, token):
    """With ``_lock`` held: make ``client`` the current client and return its
    generation. Every successful connect goes through here."""
    global _client, _token, _generation, _connected_at, _lost, _lost_at, _lost_reason
    global _pending_token, _watchdog_on
    if _client is not None:
        _retired.append(_client)  # kept, never closed (N1)
    _client, _token = client, token
    _connected_at = time.monotonic()
    _generation += 1
    _lost, _lost_at, _lost_reason = False, 0.0, None
    _pending_token = None
    _watchdog_on = True
    _cond.notify_all()
    return _generation


def _retire_locked(client, token):
    """With ``_lock`` held: keep an attempt that will never be adopted."""
    global _pending_token
    _retired.append(client)
    if _pending_token == token:
        _pending_token = None


def _ensure_connected():
    """The first connect, and a lazy one after a reset. A failure raises to
    the caller: at ``server_setup`` import it fails the backend start, as
    before this module recovered."""
    if _client is not None:
        return
    with _connect_lock:
        while True:
            with _lock:
                if _client is not None:
                    return
                epoch0 = _epoch
                token = next(_tokens)
            # connect is looked up at call time: tests patch it there.
            client = awsiot.greengrasscoreipc.connect(
                lifecycle_handler=_Lifecycle(token), timeout=CONNECT_TIMEOUT_S)
            with _lock:
                adopted = None
                if _epoch == epoch0:
                    adopted = _adopt(client, token)
                else:
                    _retired.append(client)  # a reset overtook it: never adopted
            if adopted is None:
                logger.debug("Retired Greengrass IPC connect attempt %d: a reset overtook it", token)
                continue
            logger.info("Created shared Greengrass IPC client (connection %d)", adopted)
            _start_reconnect_thread()
            return


def _start_reconnect_thread():
    """With ``_connect_lock`` held: start the ``ipc-reconnect`` thread once."""
    global _reconnect_thread
    if _reconnect_thread is not None and _reconnect_thread.is_alive():
        return
    _reconnect_thread = threading.Thread(target=_reconnect_loop, name="ipc-reconnect", daemon=True)
    _reconnect_thread.start()


def _open_connection(lifecycle):
    """One reconnect attempt: what ``greengrasscoreipc.connect()`` does, with
    the same public classes, except that it returns at once with the client
    and its connect future. The caller keeps the connection it started: a
    ``connect()`` that times out raises without returning it, and awscrt keeps
    it alive with no references (``awscrt/eventstream/rpc.py:289-290``)."""
    from awscrt.io import (
        ClientBootstrap,
        DefaultHostResolver,
        EventLoopGroup,
        SocketDomain,
        SocketOptions,
    )
    from awsiot.eventstreamrpc import Connection, MessageAmendment
    from awsiot.greengrasscoreipc.client import GreengrassCoreIPCClient

    elg = EventLoopGroup(num_threads=1)
    options = SocketOptions()
    options.domain = SocketDomain.Local
    connection = Connection(
        host_name=os.environ["AWS_GG_NUCLEUS_DOMAIN_SOCKET_FILEPATH_FOR_COMPONENT"],
        port=0, bootstrap=ClientBootstrap(elg, DefaultHostResolver(elg)),
        socket_options=options,
        connect_message_amender=MessageAmendment.create_static_authtoken_amender(
            os.environ["SVCUID"]))
    return GreengrassCoreIPCClient(connection), connection.connect(lifecycle)


# --- the public API -----------------------------------------------------------------


class SharedIpcClient:
    """The stable handle every holder keeps (``get_ipc_client()``).

    A ``new_*`` attribute is a wrapper that runs on the current client; a
    ``ConnectionClosedError`` from it reports the loss of the generation that
    raised it, then propagates. Any other public attribute is the current
    client's. Private names are never forwarded. :meth:`close` closes nothing.
    """

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        client, gen = current()
        attribute = getattr(client, name)
        if not name.startswith("new_"):
            return attribute

        def new_operation(*args, **kwargs):
            try:
                return attribute(*args, **kwargs)
            except ConnectionClosedError:
                report_connection_closed(gen, "ConnectionClosedError from {}".format(name))
                raise

        return new_operation

    def close(self):
        """The shared client is never closed (DD-19576, N1)."""
        logger.warning("the shared Greengrass IPC client is never closed")
        future = Future()
        future.set_result(None)
        return future

    def __repr__(self):
        return "<SharedIpcClient connection {}>".format(_generation)


_HANDLE = SharedIpcClient()


def get_ipc_client():
    """Return the shared handle, connecting the shared client on first use.

    Thread-safe: concurrent first callers make one connect. The awsiot client
    supports many concurrent operations (each ``new_*`` creates its own
    continuation), so one shared connection serves the whole process.
    """
    _ensure_connected()
    return _HANDLE


def current():
    """``(client, generation)`` of the current client, connecting if needed;
    both read together, so the generation belongs to the client."""
    while True:
        _ensure_connected()
        with _lock:
            if _client is not None:
                return _client, _generation


def current_client():
    """The current client, or None; never connects (tests and diagnostics)."""
    return _client


def generation():
    """The current connection generation (+1 per adoption, never reset)."""
    return _generation


def connection_usable():
    """False only while the current generation is marked lost."""
    with _lock:
        return not _lost


def add_connection_listener(listener):
    """Call ``listener(kind, generation)`` with ``"lost"`` and ``"replaced"``
    for each reconnect, on the reconnect thread. It must not block; an
    exception from it is logged. A reset forgets every listener."""
    with _lock:
        _listeners.append(listener)


def report_connection_closed(gen, reason):
    """Mark connection ``gen`` lost and wake the reconnect thread; the only
    code that marks a loss, and it owns the loss's ERROR line. Returns False,
    with no log and no wake, for a stale generation, one already lost, or
    after a reset. Never blocks: safe on the IPC event-loop thread."""
    global _lost, _lost_at, _lost_reason
    with _lock:
        if gen != _generation or _lost or _client is None:
            return False                       # stale, already lost, or reset: no log, no wake
        _lost, _lost_at, _lost_reason = True, time.monotonic(), reason
    logger.error("Greengrass IPC connection %d lost (%s); reconnecting", gen, reason)
    _wake.set()
    return True


def wait_for_new_connection(after_generation, timeout_s):
    """True once a connection newer than ``after_generation`` is adopted,
    False when ``timeout_s`` passes first."""
    with _cond:
        return _cond.wait_for(lambda: _generation > after_generation, max(0.0, timeout_s))


def call_with_ipc_retry(operation):
    """Run ``operation(client)`` on the shared handle, and once more after the
    shared reconnect when the connection was closed.

    Only a ``ConnectionClosedError`` is retried: the loss is reported, the
    call waits for a newer connection for whatever is left of ``RETRY_WAIT_S``
    since the loss (nothing once an outage is older, so calls then fail at
    once), and the operation runs once more. A ``ConnectionClosedError`` from
    the retry is reported for the generation it used, then raised. Any other
    error, a denial or a timeout included, and a wait that ends without a new
    connection, propagate: they say nothing about the connection (R5). After a
    reset (tests and manual recovery) the retry connects lazily at once.
    """
    client = get_ipc_client()
    gen = generation()  # read after the connect: a first call reports its own generation
    try:
        return operation(client)
    except ConnectionClosedError:
        report_connection_closed(gen, "ConnectionClosedError from a call")
        with _lock:
            reset = _client is None
            lost_at = _lost_at if (_lost and _generation == gen) else None
        if not reset:
            remaining = 0.0 if lost_at is None else RETRY_WAIT_S - (time.monotonic() - lost_at)
            if not wait_for_new_connection(gen, remaining):
                raise
    logger.debug("Retrying a Greengrass IPC call after connection %d was lost", gen)
    client = get_ipc_client()
    gen = generation()
    try:
        return operation(client)
    except ConnectionClosedError:
        report_connection_closed(gen, "ConnectionClosedError from a retried call")
        raise


def reset_ipc_client():
    """Forget the current client, so the next call connects again.

    For tests and manual recovery; nothing in production calls it. The client
    is retired, never closed (N1). A pending reconnect attempt can no longer be
    adopted (the reconnect thread retires it when it next checks), a backoff
    wait of the reconnect thread ends at once, and the listeners and the
    early-death count are cleared.
    """
    global _client, _token, _lost, _lost_at, _lost_reason, _pending_token
    global _watchdog_on, _epoch, _early_deaths
    with _lock:
        if _client is not None:
            _retired.append(_client)
        _client = _token = None
        _pending_token = None
        _lost, _lost_at, _lost_reason = False, 0.0, None
        del _listeners[:]
        _early_deaths = 0
        _watchdog_on = True
        _epoch += 1
        _cond.notify_all()


# --- detection ----------------------------------------------------------------------


class _Lifecycle:
    """The lifecycle handler of one connection attempt (its ``token``): the
    four methods the SDK calls (``eventstreamrpc.LifecycleHandler``). Only the
    adopted attempt's ``on_disconnect`` reports a loss. Every body is
    contained: the SDK calls ``on_disconnect`` with nothing around it
    (``eventstreamrpc.py:286``), and an exception from the others closes the
    connection (``:322-324``)."""

    def __init__(self, token):
        self.token = token

    def on_connect(self):
        try:
            logger.debug("Greengrass IPC connect attempt %d acknowledged", self.token)
        except Exception:  # noqa: BLE001 - never into the SDK
            logger.exception("Error handling the connect of Greengrass IPC attempt %d", self.token)

    def on_disconnect(self, reason):
        try:
            with _lock:
                token, gen = _token, _generation
            if token == self.token:
                report_connection_closed(gen, "disconnected: {}".format(_type_name(reason)))
            else:
                logger.debug("Ignoring the disconnect of Greengrass IPC attempt %d: not the "
                             "current connection", self.token)
        except Exception:  # noqa: BLE001 - never into the SDK
            logger.exception("Error handling the disconnect of Greengrass IPC attempt %d", self.token)

    def on_error(self, error):
        try:
            logger.error("Greengrass IPC protocol error on attempt %d: %s", self.token, _describe(error))
        except Exception:  # noqa: BLE001 - never into the SDK
            logger.exception("Error handling a protocol error of Greengrass IPC attempt %d", self.token)
        return True  # the SDK default: close the connection on a protocol error

    def on_ping(self, headers, payload):
        """Nothing to do, as the SDK default."""


def _run_watchdog():
    """Report the current connection lost when the SDK's own state says it is
    closed, with or without a callback: ``client._connection._synced.state``
    is ``DISCONNECTED`` or ``DISCONNECTING`` exactly when ``new_*`` raises
    ``ConnectionClosedError``. A private read, pinned by ``awsiotsdk==1.31.0``
    and a unit test against the installed SDK; it sends nothing and makes no
    native call. A state that cannot be read as a string (test fakes) turns
    the watchdog off for that client, until the next adoption."""
    global _watchdog_on
    with _lock:
        client, gen, armed = _client, _generation, _watchdog_on
    if client is None or not armed:
        return
    try:
        state = client._connection._synced.state.name
    except Exception:  # noqa: BLE001 - unreadable: the watchdog goes off below
        state = None
    if not isinstance(state, str):
        with _lock:
            switch_off = _client is client and _watchdog_on
            if switch_off:
                _watchdog_on = False
        if switch_off:
            logger.warning("Cannot read the state of Greengrass IPC connection %d; its state "
                           "watchdog is off", gen)
        return
    if state in _CLOSED_STATES:
        report_connection_closed(gen, "state {}".format(state))


# --- the reconnect thread -----------------------------------------------------------


def _notify(listeners, kind, gen):
    for listener in listeners:
        try:
            listener(kind, gen)
        except Exception:  # noqa: BLE001 - listener isolation
            logger.exception("Error in a Greengrass IPC connection listener (%s, connection %d)",
                             kind, gen)


def _reconnect_loop():
    """The ``ipc-reconnect`` thread: one pass per wake-up, for ever."""
    while True:
        try:
            _reconnect_pass()
        except Exception:  # noqa: BLE001 - the thread never dies
            logger.exception("Error in the Greengrass IPC reconnect thread")
            time.sleep(1.0)


def _reconnect_pass():
    """Wait for a loss or the watchdog tick, then reconnect a marked loss."""
    global _early_deaths
    _wake.wait(WATCHDOG_INTERVAL_S)
    _wake.clear()
    _run_watchdog()
    with _lock:
        if _client is None or not _lost:
            return
        epoch0, lost_gen, lost_at = _epoch, _generation, _lost_at
        # A connection's lifetime runs from its adoption to its loss.
        if _lost_at - _connected_at < STABLE_CONNECTION_S:
            _early_deaths += 1
        else:
            _early_deaths = 0
        n = _early_deaths
        listeners = list(_listeners)
    _notify(listeners, "lost", lost_gen)
    _reconnect(epoch0, lost_gen, lost_at, n)


def _backoff(n):
    return RECONNECT_BACKOFF_S[min(n - 1, len(RECONNECT_BACKOFF_S) - 1)]


def _reconnect(epoch0, lost_gen, lost_at, n):
    """Attempts for the loss of ``lost_gen``, one at a time, until one is
    adopted or the loss is no longer wanted (a reset meanwhile)."""
    global _pending_token
    attempt = 0
    while True:
        if n > 0:
            # Only a reset or an adoption notifies _cond: a loss report cannot end the wait.
            with _cond:
                _cond.wait_for(lambda: not _still_wanted(epoch0, lost_gen), _backoff(n))
        with _lock:
            if not _still_wanted(epoch0, lost_gen):
                return
            token = _pending_token = next(_tokens)
        attempt += 1
        try:
            client, future = _open_connection(_Lifecycle(token))
            adopted = _await_attempt(epoch0, lost_gen, token, attempt, client, future)
        except Exception as error:  # noqa: BLE001 - a failed attempt: retried for ever
            with _lock:
                if _pending_token == token:
                    _pending_token = None
            n += 1
            logger.warning("Could not reconnect to Greengrass IPC (attempt %d): %s; retrying in %g s",
                           attempt, _describe(error), _backoff(n))
            continue
        if adopted is None:
            return
        gen, kept, listeners = adopted
        _notify(listeners, "replaced", gen)
        logger.info("Greengrass IPC connection %d is up after %.1f s (%d failed attempts, "
                    "%d replaced connections kept)", gen, time.monotonic() - lost_at, attempt - 1, kept)
        # An attempt can die between its acknowledgement and its adoption.
        _run_watchdog()
        return


def _await_attempt(epoch0, lost_gen, token, attempt, client, future):
    """Wait on one attempt's connect future in slices of ``CONNECT_TIMEOUT_S``,
    keeping it (at most one attempt is pending, and none is abandoned).

    Returns ``(generation, retired count, listeners)`` once adopted, or None
    when the loss is no longer wanted (the attempt is then retired). A failed
    attempt raises its error."""
    waited = 0.0
    while True:
        try:
            future.result(CONNECT_TIMEOUT_S)
            break
        except concurrent.futures.TimeoutError:
            if future.done():
                raise  # the attempt itself failed with a timeout
            waited += CONNECT_TIMEOUT_S
            with _lock:
                wanted = _still_wanted(epoch0, lost_gen)
                if not wanted:
                    _retire_locked(client, token)
            if not wanted:
                logger.debug("Retired Greengrass IPC connect attempt %d: a reset overtook it", attempt)
                return None
            logger.warning("Greengrass IPC connect attempt %d still waiting after %g s", attempt, waited)
    with _lock:
        if _still_wanted(epoch0, lost_gen) and _pending_token == token:
            return _adopt(client, token), len(_retired), list(_listeners)
        _retire_locked(client, token)
    logger.debug("Retired Greengrass IPC connect attempt %d: a reset overtook it", attempt)
    return None
