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
"""Property test for the credential retry of the Edge_Sync_Agent
(rtsp-rtmp-stream-cameras task 29.2).

**Feature: rtsp-rtmp-stream-cameras, Property 29: A denied credential fetch is
retried within its bound**

*For any* sequence of fetch outcomes (denied, granted, another error) for
changes carrying a Credential_Reference, with newer, redelivered and
superseding changes for the same and another Camera_Source delivered at any
point of the schedule, and timers fired in any order (stale ones included),
the agent behaves like a reference model of design component 12:

- at most six attempts per delivery, each retry timed 2, 4, 8, 16 and 30 s,
  in turn, after the attempt before it, and never run on the delivering
  thread (the recording timer runs nothing until the test fires it);
- an ack exactly when an attempt succeeds, a failure exactly when an attempt
  fails for another reason or the last retry is denied, with the exact
  reason strings;
- a superseded change is never fetched or applied again, a redelivery of the
  parked change neither, and a stale timer does nothing: the fetch log and
  the accessor's create log equal the model's;
- a redelivery of a change that already ended is not applied again either
  (Requirement 5.13, task 30.3): the model's ``finished`` set;
- a change for another Camera_Source is applied when it is delivered.

**Validates: Requirement 5.6**

Each example runs the real ``ImageSourceAccessor`` over a fresh sqlite
database and the real ``credential_fetch.fetch``; the deadline is disabled.
"""
import itertools
import tempfile

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

from camera_sync.agent import CREDENTIAL_RETRY_DELAYS_S, REASON_DISCOVERY_MANAGED

IDS = ("portal-a", "portal-b")
DENIED, GRANTED, OTHER = "denied", "granted", "other"
_REASONS = {
    "last-denial": "credential retrieval failed: AccessDeniedException (retried for 60 s)",
    OTHER: "credential retrieval failed: ResourceNotFoundException",
}


class Model:
    """The reference schedule of design component 12."""

    def __init__(self, scripts):
        self.scripts = {csid: list(script) for csid, script in scripts.items()}
        self.parked = {}      # csid -> {change, attempt, token}
        self.timers = []      # aligned with the recording timer: (csid, token, delay)
        self.tokens = itertools.count(1)
        self.fetches = []     # (csid, versionId), in order
        self.creates = []     # versionId of every applied create, in order
        self.finished = set()  # (csid, pcid) of every change that ended (Requirement 5.13)
        self.acks, self.failures, self.report_requested = [], {}, False

    def begin_step(self):
        self.acks, self.failures, self.report_requested = [], {}, False

    def outcome(self, csid):
        script = self.scripts[csid]
        return script.pop(0) if script else DENIED

    def attempt(self, csid, change, attempt):
        # At most six attempts per delivery: the first and five retries.
        assert 0 <= attempt <= len(CREDENTIAL_RETRY_DELAYS_S)
        self.fetches.append((csid, change["version"]))
        outcome = self.outcome(csid)
        if outcome == GRANTED:
            if attempt:
                event("granted on a retry")
            self.creates.append(change["version"])
            self.acks.append(change["pcid"])
        elif outcome == DENIED and attempt < len(CREDENTIAL_RETRY_DELAYS_S):
            token = next(self.tokens)
            self.parked[csid] = {"change": change, "attempt": attempt + 1, "token": token}
            self.timers.append((csid, token, CREDENTIAL_RETRY_DELAYS_S[attempt]))
            return False
        else:
            event("last retry denied" if outcome == DENIED else
                  "other error on a retry" if attempt else "other error at once")
            reason = _REASONS["last-denial" if outcome == DENIED else OTHER]
            self.failures[csid] = {"reason": reason, "portalChangeId": change["pcid"]}
        self.finished.add((csid, change["pcid"]))
        return True

    def deliver(self, csid, change):
        self.report_requested = True
        if (csid, change["pcid"]) in self.finished:
            event("finished change redelivered")
            return  # applied at most once per process (Requirement 5.13): skipped, nothing recorded
        parked = self.parked.get(csid)
        if parked is not None:
            if parked["change"]["pcid"] == change["pcid"]:
                event("parked change redelivered")
                return  # the parked change, redelivered: not applied again
            event("parked change superseded")
            del self.parked[csid]  # superseded
        if change["op"] == "update":
            self.failures[csid] = {"reason": REASON_DISCOVERY_MANAGED, "portalChangeId": change["pcid"]}
            self.finished.add((csid, change["pcid"]))
            return
        self.attempt(csid, change, 0)

    def fire(self, index):
        csid, token, _ = self.timers.pop(index)
        parked = self.parked.get(csid)
        if parked is None or parked["token"] != token:
            event("stale timer fired")
            return  # stale
        del self.parked[csid]
        if self.attempt(csid, parked["change"], parked["attempt"]):
            self.report_requested = True


_STEP = st.one_of(
    st.tuples(st.just("deliver"),
              st.dictionaries(st.sampled_from(IDS), st.sampled_from(["create", "redeliver", "update"]),
                              min_size=1, max_size=2)),
    st.tuples(st.just("fire"), st.integers(min_value=0, max_value=7)),
)
_SCRIPT = st.lists(st.sampled_from([DENIED, DENIED, DENIED, GRANTED, GRANTED, OTHER]), max_size=12)


def _aws_outcome(outcome):
    return {DENIED: denied, GRANTED: granted, OTHER: not_found}[outcome]()


class PerCameraFetcher:
    """One scripted fetcher per Camera_Source, chosen by the change's
    reference version; denied once a script is used up, like the model."""

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


def _acks(document):
    return sorted(entry["ack"] for csid, entry in document["cameras"].items()
                  if entry and csid.startswith("cfg-") and "ack" in entry)


@settings(deadline=None)
@given(scripts=st.fixed_dictionaries({csid: _SCRIPT for csid in IDS}),
       steps=st.lists(_STEP, min_size=1, max_size=14))
def test_a_denied_credential_fetch_is_retried_within_its_bound(scripts, steps):
    """**Feature: rtsp-rtmp-stream-cameras, Property 29: A denied credential
    fetch is retried within its bound**

    **Validates: Requirement 5.6**
    """
    model = Model(scripts)
    numbers = itertools.count(100)
    owner_of_version = {}
    last_delivered = {}

    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as monkeypatch:
        world = make_stream_world(tmp, monkeypatch)
        try:
            fetcher = PerCameraFetcher(scripts, owner_of_version)
            agent = world.make_agent(credential_fetcher=fetcher)

            def new_change(csid, op):
                n = next(numbers)
                ref = reference(n)
                owner_of_version[ref["versionId"]] = csid
                change = rtsp_change(op, "pc-{}".format(n), ref=ref if op == "create" else None)
                change["version"], change["pcid"] = ref["versionId"], change["portalChangeId"]
                return change

            def check(step):
                reports = len(world.shadow.reported)
                agent.pump()
                written = world.shadow.reported[reports:]
                assert len(written) == (1 if model.report_requested else 0), step
                if written:
                    [document] = written
                    assert _acks(document) == sorted(model.acks), step
                    assert {csid: failure for csid, failure in document["failures"].items() if failure} \
                        == model.failures, step
                assert [delay for delay, _ in world.retry_timer.pending] == \
                    [delay for _, _, delay in model.timers], step
                assert fetcher.calls == model.fetches, step
                assert [call[2] for call in world.accessor.calls if call[0] == "create"] == model.creates, step

            for step in steps:
                model.begin_step()
                if step[0] == "fire":
                    if not world.retry_timer.pending:
                        continue
                    index = step[1] % len(world.retry_timer.pending)
                    model.fire(index)
                    world.retry_timer.fire(index)
                else:
                    batch = {}
                    for csid, kind in sorted(step[1].items()):
                        if kind == "redeliver":
                            if csid not in last_delivered:
                                continue
                            change = dict(last_delivered[csid])
                        else:
                            change = new_change(csid, kind)
                        last_delivered[csid] = change
                        batch[csid] = change
                    if not batch:
                        continue
                    for csid in sorted(batch):
                        model.deliver(csid, batch[csid])
                    agent.apply_desired_changes(
                        {csid: {k: v for k, v in change.items() if k not in ("version", "pcid")}
                         for csid, change in batch.items()})
                check(step)

            # Drain: every parked change runs to its end, within its bound.
            for _ in range(len(IDS) * len(CREDENTIAL_RETRY_DELAYS_S) + len(model.timers)):
                if not world.retry_timer.pending:
                    break
                model.begin_step()
                model.fire(0)
                world.retry_timer.fire(0)
                check("drain")
            assert world.retry_timer.pending == [] and model.parked == {}
        finally:
            world.close()
