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
"""Property test for at most once per Portal change (rtsp-rtmp-stream-cameras
task 30.3, finding 23).

**Feature: rtsp-rtmp-stream-cameras, Property 32: Each Portal change is
applied at most once per process, and a retried clear never removes a newer
change**

*For any* sequence of new Portal changes (carried by a delta at once, or
later), redeliveries, catch-up reads, desired-entry clears that fail or land,
unreadable shadow reads, fake-clock time and credential retry timers fired in
any order, where a redelivery replays the most recently delivered change for
its Camera_Source, whether that change was applied, failed or parked, as
Property 29 does:

- each ``(Camera_Source id, portalChangeId)`` is dispatched at most once on
  delivery (the credential retries of a parked change are task 29's), its
  create runs at most once, and its credential is fetched at most six times;
- every delivered change ends applied (acknowledged), failed or parked, or is
  superseded by a later change for its Camera_Source; after a final catch-up
  and the drain, nothing is parked, nothing is pending, and
  ``desired.changes`` is empty;
- a retried clear never nulls a desired entry that carries another
  ``portalChangeId``, nor an entry the shadow does not hold.

Stale, out-of-order redeliveries of a processed change are outside the
domain; ``test_f2325_at_most_once.py`` pins them.

**Validates: Requirement 5.13**

Each example runs the real ``ImageSourceAccessor`` over a fresh sqlite
database and the real ``credential_fetch.fetch``, with a scripted shadow
(``f2325_agent_support.ScriptedShadow``); the deadline is disabled.
"""
import collections
import copy
import itertools
import tempfile
from typing import Mapping

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from f2122_stream_agent_support import (
    ScriptedFetcher,
    denied,
    granted,
    make_stream_world,
    not_found,
    reference,
    rtsp_change,
)
from f2325_agent_support import ScriptedShadow

from camera_sync.agent import CREDENTIAL_RETRY_DELAYS_S

IDS = ("portal-a", "portal-b")
DENIED, GRANTED, OTHER = "denied", "granted", "other"
#: The first attempt and the five retries of task 29.
MAX_ATTEMPTS = 1 + len(CREDENTIAL_RETRY_DELAYS_S)

# A new Portal change for an id: whether a delta carries it at once, and
# whether the agent's next clear fails.
_NEW = st.tuples(st.just("new"), st.sampled_from(IDS), st.sampled_from(["create", "create", "update"]),
                 st.booleans(), st.booleans())
# A redelivery, and whether its clear fails.
_REDELIVER = st.tuples(st.just("redeliver"), st.sampled_from(IDS), st.booleans())
_TIME = st.tuples(st.just("time"), st.sampled_from([0.5, 1.0, 2.0, 4.0, 30.0]))
_FIRE = st.tuples(st.just("fire"), st.integers(min_value=0, max_value=7))
_STEP = st.one_of(
    _NEW, _NEW, _NEW, _REDELIVER, _REDELIVER, _TIME, _TIME, _FIRE, _FIRE,
    st.tuples(st.just("catch_up")),
    st.tuples(st.just("fail_clears"), st.integers(min_value=1, max_value=3)),
    st.tuples(st.just("unreadable"), st.integers(min_value=1, max_value=2)),
)
_SCRIPT = st.lists(st.sampled_from([DENIED, DENIED, GRANTED, GRANTED, GRANTED, OTHER]), max_size=10)


def _aws_outcome(outcome):
    return {DENIED: denied, GRANTED: granted, OTHER: not_found}[outcome]()


class PerCameraFetcher:
    """One scripted fetcher per Camera_Source, chosen by the change's
    reference version; denied once a script is used up."""

    def __init__(self, scripts, owner_of_version):
        self.fetchers = {csid: ScriptedFetcher(*[_aws_outcome(o) for o in script])
                         for csid, script in scripts.items()}
        self.owner_of_version = owner_of_version
        self.calls = []

    def __call__(self, ref):
        csid = self.owner_of_version[ref["versionId"]]
        self.calls.append((csid, ref["versionId"]))
        fetcher = self.fetchers[csid]
        if not fetcher.outcomes:
            fetcher.outcomes.append(denied())
        return fetcher(ref)


def _acks(shadow):
    return {entry["ack"] for document in shadow.reported
            for entry in (document.get("cameras") or {}).values()
            if isinstance(entry, Mapping) and "ack" in entry}


def _failures(shadow):
    return {(csid, failure.get("portalChangeId")) for document in shadow.reported
            for csid, failure in (document.get("failures") or {}).items()
            if isinstance(failure, Mapping)}


@settings(deadline=None)
@given(scripts=st.fixed_dictionaries({csid: _SCRIPT for csid in IDS}),
       steps=st.lists(_STEP, min_size=2, max_size=16))
def test_each_portal_change_is_applied_at_most_once(scripts, steps):
    """**Feature: rtsp-rtmp-stream-cameras, Property 32: Each Portal change is
    applied at most once per process, and a retried clear never removes a
    newer change**

    **Validates: Requirement 5.13**
    """
    numbers = itertools.count(100)
    owner_of_version = {}
    shadow = ScriptedShadow()

    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as monkeypatch:
        world = make_stream_world(tmp, monkeypatch, shadow=shadow)
        try:
            fetcher = PerCameraFetcher(scripts, owner_of_version)
            agent = world.make_agent(credential_fetcher=fetcher)
            delivered = []         # (csid, pcid) of every change the agent was given, in order
            given_keys = set()
            last_delivered = {}    # csid -> the change last delivered for it
            dispatched = collections.Counter()
            violations = []
            phase = ["deliver"]

            # Every delivery (delta, redelivery, catch-up) goes through
            # apply_desired_changes; record what the agent was given first.
            apply_desired_changes = agent.apply_desired_changes

            def recording_apply(changes):
                for csid, change in changes.items():
                    if isinstance(change, Mapping):
                        key = (csid, change.get("portalChangeId"))
                        if key in agent._processed_changes:
                            event("a processed change delivered again")
                        given_keys.add(key)
                        delivered.append(key)
                        last_delivered[csid] = copy.deepcopy(dict(change))
                return apply_desired_changes(changes)

            agent.apply_desired_changes = recording_apply

            dispatch_change = agent._dispatch_change

            def counting_dispatch(csid, change, op, portal_change_id, attempt):
                if attempt == 0:
                    dispatched[(csid, portal_change_id)] += 1
                return dispatch_change(csid, change, op, portal_change_id, attempt)

            agent._dispatch_change = counting_dispatch

            def on_null(csid, entry):
                # During pump() every desired write is a retried clear; a
                # first clear nulls the batch just delivered.
                if phase[0] != "pump":
                    return
                pcid = entry.get("portalChangeId") if isinstance(entry, Mapping) else None
                if pcid is None or (csid, pcid) not in given_keys:
                    violations.append((csid, entry))

            shadow.on_null = on_null

            def pump():
                phase[0] = "pump"
                try:
                    agent.pump()
                finally:
                    phase[0] = "deliver"

            def new_change(csid, op):
                n = next(numbers)
                pcid = "pc-{}".format(n)
                ref = reference(n)
                owner_of_version[ref["versionId"]] = csid
                return rtsp_change(op, pcid, ref=ref if op == "create" else None)

            def check(step):
                again = {key: count for key, count in dispatched.items() if count > 1}
                assert not again, ("a change was applied more than once", step, again)
                creates = collections.Counter(call[2] for call in world.accessor.calls if call[0] == "create")
                assert all(count <= 1 for count in creates.values()), (step, creates)
                fetches = collections.Counter(version for _, version in fetcher.calls)
                assert all(count <= MAX_ATTEMPTS for count in fetches.values()), (step, fetches)
                assert violations == [], ("a retried clear nulled an entry it was not for", step, violations)

            for step in steps:
                kind = step[0]
                if kind == "new":
                    _, csid, op, at_once, clear_fails = step
                    shadow.portal_writes({csid: new_change(csid, op)})
                    if clear_fails:
                        shadow.fail_desired = max(shadow.fail_desired, 1)
                    if at_once:
                        agent.on_delta(shadow.delta())
                elif kind == "redeliver":
                    _, csid, clear_fails = step
                    if csid not in last_delivered:
                        continue
                    if clear_fails:
                        shadow.fail_desired = max(shadow.fail_desired, 1)
                    agent.apply_desired_changes({csid: copy.deepcopy(last_delivered[csid])})
                elif kind == "catch_up":
                    if agent.on_subscription_active() is False:
                        event("catch-up on an unreadable shadow")
                elif kind == "fail_clears":
                    shadow.fail_desired = step[1]
                elif kind == "unreadable":
                    shadow.get_script = [None] * step[1]
                elif kind == "time":
                    world.clock.now += step[1]
                else:  # fire
                    if not world.retry_timer.pending:
                        continue
                    world.retry_timer.fire(step[1] % len(world.retry_timer.pending))
                pump()
                check(step)
                if agent._pending_clears:
                    event("a clear is pending")
                if agent._credential_retries:
                    event("a change is parked")

            # The drain: clears land from here on; a final catch-up delivers
            # every change still in desired.changes, every timer runs, and
            # the pending clears are retried.
            shadow.fail_desired, shadow.get_script = 0, []
            assert agent.on_subscription_active() is True
            pump()
            for _ in range(60):
                if not world.retry_timer.pending:
                    break
                world.retry_timer.fire(0)
                pump()
            for _ in range(5):
                if not agent._pending_clears:
                    break
                world.clock.now += 60.0
                pump()
            check("drain")
            assert world.retry_timer.pending == [] and agent._credential_retries == {}
            assert agent._pending_clears == {}, "a pending clear never landed"
            assert shadow.desired_changes() == {}, "desired.changes kept an entry"

            acks, failures = _acks(shadow), _failures(shadow)
            for index, (csid, pcid) in enumerate(delivered):
                superseded = any(c == csid and p != pcid for c, p in delivered[index + 1:])
                assert pcid in acks or (csid, pcid) in failures or superseded, (
                    "delivered change {} for {} never ended".format(pcid, csid), steps)
                if superseded and pcid not in acks and (csid, pcid) not in failures:
                    event("a change was superseded")
        finally:
            world.close()
