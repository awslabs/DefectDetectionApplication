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
"""Property test for the recoverable shared Greengrass IPC connection
(rtsp-rtmp-stream-cameras task 30.4, finding 24).

**Feature: rtsp-rtmp-stream-cameras, Property 33: One reconnect per lost
connection, and every IPC caller reaches the newest client**

*For any* interleaving of connection-loss signals from any number of threads,
``reset_ipc_client()`` calls at any point, and connect outcomes (refused any
number of times, slow past the connect timeout, then successful):

- each lost connection generation gets at most one adoption, and exactly one
  when no reset intervenes;
- after a reset, nothing is adopted except the next lazy connect;
- at most one adoptable attempt is pending, counting a lazy connect; without a
  reset, two attempts are never pending at once;
- each lost generation logs one ERROR;
- no client's ``close()`` is called;
- after an adoption, every call through the shared handle reaches the newest
  client.

**Validates: Requirement 5.14**

Each example runs the real ``utils.ipc_client`` and its ``ipc-reconnect``
thread over a fresh fake world (``f2325_ipc_support``, plan P4) with
millisecond timing. A loss closes the current fake connection, then 1-6
threads tell it, each with one signal: a call through the handle (its
``ConnectionClosedError``), ``report_connection_closed``, the connection's
``on_disconnect``, or nothing (the watchdog finds the closed state). Adoptions
are journaled at ``_adopt``, the one place every successful connect goes
through; the attempts' intervals come from the fake factory. Every thread an
example starts is joined; the deadline is disabled.
"""
import logging
import threading
import time

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from awsiot.eventstreamrpc import ConnectionClosedError

from f2325_ipc_support import REFUSED, SLOW, eventually, fast_timings, install, uninstall

from utils import ipc_client as shared_ipc

CONNECT_TIMEOUT_S = 0.03
TIMINGS = {"CONNECT_TIMEOUT_S": CONNECT_TIMEOUT_S, "WATCHDOG_INTERVAL_S": 0.005,
           "RECONNECT_BACKOFF_S": (0.001, 0.002, 0.004, 0.008, 0.01), "RETRY_WAIT_S": 0.2}
#: How the threads of one loss tell it.
SIGNALS = ("call", "report", "disconnect", "watchdog")

_LOSS = st.fixed_dictionaries({
    "signals": st.lists(st.sampled_from(SIGNALS), min_size=1, max_size=6),
    "refused": st.integers(min_value=0, max_value=3),
    "slow": st.booleans(),
    # None, or when a reset_ipc_client() comes: with the signal threads,
    # right after them, or after a delay (in a backoff, during a pending
    # attempt, or after the adoption).
    "reset": st.sampled_from([None, None, "with-signals", "after-signals", "delayed"]),
    "delay": st.floats(min_value=0.0, max_value=0.08),
})


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []
        self._lock = threading.Lock()

    def emit(self, record):
        with self._lock:
            self.records.append(record)

    def lost_lines(self):
        with self._lock:
            return [record.getMessage() for record in self.records
                    if record.levelno == logging.ERROR and record.getMessage().startswith("Greengrass IPC connection ")]


class _Example:
    """One example's world, its adoptions ``(generation, client, epoch)`` in
    order (the epoch counts resets, read inside ``_adopt``), its resets as
    ``(before, after)`` times, and its losses."""

    def __init__(self, world):
        self.world = world
        self.adoptions = []
        self.resets = []
        self.handle = None

    def reset(self):
        before = time.monotonic()
        shared_ipc.reset_ipc_client()
        self.resets.append((before, time.monotonic()))


def _signal(kind, example, client, gen):
    if kind == "call":
        try:
            example.handle.new_get_thing_shadow()
        except ConnectionClosedError:
            pass
    elif kind == "report":
        shared_ipc.report_connection_closed(gen, "a test signal")
    elif kind == "disconnect":
        client.notify_lifecycle("on_disconnect", ConnectionResetError("AWS_IO_SOCKET_CLOSED"))
    else:  # the watchdog finds the closed state; wait until it did (or a reset made it moot)
        eventually(lambda: not shared_ipc.connection_usable() or shared_ipc.generation() != gen
                   or shared_ipc.current_client() is not client, 1.0, interval=0.002)


def _quiesce(world):
    """Every attempt in flight resolves, and the reconnect thread acts on it."""
    eventually(lambda: not world.pending_attempts(), 2.0, interval=0.005)
    time.sleep(3 * CONNECT_TIMEOUT_S)


def _run_loss(example, loss):
    world = example.world
    client, gen = shared_ipc.current_client(), shared_ipc.generation()
    world.open_script = [REFUSED] * loss["refused"] + (
        [(SLOW, 2.5 * CONNECT_TIMEOUT_S)] if loss["slow"] else [])
    client.mark_closed()  # the connection is gone; each signal tells it in its own way
    threads = [threading.Thread(target=_signal, args=(kind, example, client, gen), name="f2325-signal")
               for kind in loss["signals"]]
    if loss["reset"] == "with-signals":
        threads.append(threading.Thread(target=example.reset, name="f2325-reset"))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)
    assert not any(thread.is_alive() for thread in threads), "a signal thread hung"

    if loss["reset"] == "after-signals":
        example.reset()
    elif loss["reset"] == "delayed":
        time.sleep(loss["delay"])
        example.reset()
    event("reset: {}".format(loss["reset"]))
    event("refused {}, slow {}".format(loss["refused"], loss["slow"]))
    if loss["reset"] is None:
        assert shared_ipc.wait_for_new_connection(gen, 2.0), (
            "connection {} was lost and never replaced".format(gen))
    else:
        shared_ipc.get_ipc_client()  # the next lazy connect (a no-op when a call made it)
    _quiesce(world)
    return gen


def _check_handle(example):
    """Every call through the handle reaches the newest adopted client."""
    newest = example.adoptions[-1][1]
    assert shared_ipc.current_client() is newest
    reached = []

    def call():
        reached.append(example.handle.new_get_thing_shadow().client)

    threads = [threading.Thread(target=call, name="f2325-caller") for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)
    assert reached == [newest] * 3, "a call through the handle missed the newest client"


def _check_journal(example, losses, records):
    world = example.world
    kinds = {id(attempt[1]): attempt[0] for attempt in world.attempts if attempt[1] is not None}
    lost = {gen for gen, _ in losses}

    adoptions = example.adoptions
    for index, (gen, client, epoch) in enumerate(adoptions):
        if index == 0:
            assert kinds[id(client)] == "connect", "the first adoption was not the first connect"
            continue
        previous_gen, _, previous_epoch = adoptions[index - 1]
        assert gen == previous_gen + 1
        if kinds[id(client)] == "open":
            # A reconnect adopts only for a lost generation, once, and never
            # once a reset came after that generation's adoption.
            assert previous_gen in lost, (
                "connection {} was adopted with no loss of {}".format(gen, previous_gen))
            assert epoch == previous_epoch, (
                "a reconnect attempt was adopted after a reset (connection {})".format(gen))
        else:
            # A lazy connect adopts only after a reset, and only the next one.
            assert epoch > previous_epoch, (
                "connection {}: a lazy connect adopted with no reset since the last adoption".format(gen))

    for gen, reset in losses:
        adopted = [entry for entry in adoptions if entry[0] == gen + 1]
        assert len(adopted) <= 1
        if reset is None:
            assert len(adopted) == 1 and kinds[id(adopted[0][1])] == "open", (
                "lost connection {} did not get exactly one reconnect".format(gen))

    # Without a reset between their starts, two attempts are never pending at once.
    attempts = sorted(world.attempts, key=lambda attempt: attempt[2])
    for i, first in enumerate(attempts):
        for second in attempts[i + 1:]:
            ended = first[3] if first[3] is not None else float("inf")
            if second[2] < ended:
                assert any(first[2] < after and before < second[2] for before, after in example.resets), (
                    "two connect attempts were pending at once with no reset between them")

    lines = records.lost_lines()
    for gen, reset in losses:
        mine = [line for line in lines if line.startswith("Greengrass IPC connection {} lost (".format(gen))]
        if reset == "with-signals":
            assert len(mine) <= 1, mine
        else:
            assert len(mine) == 1, "lost connection {} logged {} ERROR lines".format(gen, len(mine))
    assert all(any(line.startswith("Greengrass IPC connection {} lost (".format(gen)) for gen in lost)
               for line in lines), "an ERROR for a connection that was not lost: {}".format(lines)

    assert [client.close_calls for client in world.clients] == [0] * len(world.clients), (
        "a client's close() was called (N1)")


@settings(deadline=None)
@given(losses=st.lists(_LOSS, min_size=1, max_size=3))
def test_one_reconnect_per_lost_connection_and_every_caller_reaches_the_newest_client(losses):
    """**Feature: rtsp-rtmp-stream-cameras, Property 33: One reconnect per lost
    connection, and every IPC caller reaches the newest client**
    **Validates: Requirement 5.14**
    """
    records = _Records()
    logger = logging.getLogger("utils.ipc_client")
    with pytest.MonkeyPatch.context() as monkeypatch:
        world = install(monkeypatch)
        fast_timings(monkeypatch, **TIMINGS)
        example = _Example(world)
        adopt = shared_ipc._adopt

        def journaled_adopt(client, token):
            # Called with _lock held: it only appends (and reads the reset
            # count, _epoch, consistent under that lock).
            gen = adopt(client, token)
            example.adoptions.append((gen, client, shared_ipc._epoch))
            return gen

        monkeypatch.setattr(shared_ipc, "_adopt", journaled_adopt)
        logger.addHandler(records)
        level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            example.handle = shared_ipc.get_ipc_client()
            done = []
            for loss in losses:
                gen = _run_loss(example, loss)
                done.append((gen, loss["reset"]))
                _check_handle(example)
            assert shared_ipc.connection_usable()
            _check_journal(example, done, records)
        finally:
            logger.removeHandler(records)
            logger.setLevel(level)
            uninstall(world)
            time.sleep(3 * CONNECT_TIMEOUT_S)  # the reconnect thread sees the reset before the next example
