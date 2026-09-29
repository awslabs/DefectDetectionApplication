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
"""Property test for continuous run retention.

**Feature: rtsp-rtmp-stream-cameras, Property 22: Retention invariants**

*For any* sequence of continuous run completions, notable or not, and any
caps, after each completion:

- a registration's retained runs are exactly its most recent
  ``keep_recent_runs`` runs, plus its newest notable runs up to
  ``keep_notable_runs``, within the device byte cap;
- staged bytes do not exceed the staging cap;
- runs of registrations without a continuous plan are never deleted.

**Validates: Requirements 12.1, 12.2, 12.4, 12.8**

The retention runs for real: sqlite rows, run directories with real
files, RAM staging and persistent roots in a temp directory. Runs are
notable by each route the design names (a failed run, an output node
whose detail reports a send, an event gate ``activated``/``cleared``
transition) or plain (no output, a ``not sent`` skip, a ``none``
transition). Interleaved with them are runs the retention must never
delete: another registration's MQTT-triggered runs and manual runs of the
continuous registration itself. The model is the specification: first
the recent and notable windows, then the device cap on persisted bytes
(oldest notable runs outside a recent window first, then the oldest
persisted), then the staging cap (oldest non-notable first).
"""
import itertools
import json
import os
import shutil
import tempfile
import time
from types import SimpleNamespace

from hypothesis import example, given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import make_session_factory

from workflow_engine.models import WorkflowExecution
from workflow_engine.run_retention import RunRetention

_KINDS = ("failed", "sent", "activated", "cleared", "plain", "skipped", "quiet-gate")
_NOTABLE = {"failed", "sent", "activated", "cleared"}


@st.composite
def _scenarios(draw):
    registrations = ["wf-a:1", "wf-c:1"][:draw(st.integers(min_value=1, max_value=2))]
    windows = {rid: (draw(st.integers(min_value=0, max_value=4)), draw(st.integers(min_value=0, max_value=4)))
               for rid in registrations}
    events = []
    for _ in range(draw(st.integers(min_value=3, max_value=25))):
        choice = draw(st.sampled_from(["continuous"] * 5 + ["other", "manual"]))
        # A few artifact sizes, so the byte caps bind often.
        size = draw(st.sampled_from([60, 250, 500, 900]))
        if choice == "continuous":
            events.append(("continuous", draw(st.sampled_from(registrations)),
                           draw(st.sampled_from(_KINDS)), size))
        else:
            events.append((choice, None, "plain", size))
    staging = draw(st.booleans())
    caps = SimpleNamespace(retention_bytes=draw(st.sampled_from([1500, 4000, 10**9])),
                           staging_bytes=draw(st.sampled_from([1200, 3000, 10**9])))
    return registrations, windows, events, staging, caps


def _write_run(directory, capture_id, size, tag_values):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "{0}.jpg".format(capture_id)), "wb") as handle:
        handle.write(b"\xff" * size)
    with open(os.path.join(directory, "run.log"), "w", encoding="utf-8") as handle:
        handle.write("log\n")
    with open(os.path.join(directory, "{0}.json".format(capture_id)), "w", encoding="utf-8") as handle:
        json.dump(tag_values, handle)


def _bytes(directory):
    return sum(os.path.getsize(os.path.join(root, name))
               for root, _dirs, files in os.walk(directory) for name in files)


class Model:
    """The retention specification over (id, registration, notable, size,
    staged) records, oldest first."""

    def __init__(self, windows, caps):
        self.windows = windows
        self.caps = caps
        self.runs = []

    def complete(self, run):
        self.runs.append(run)
        keep_recent, keep_notable = self.windows[run["rid"]]
        mine = [r for r in self.runs if r["rid"] == run["rid"]]
        # The newest keep_recent runs (all of them when there are fewer).
        recent = mine[-keep_recent:] if keep_recent else []
        older = mine[:-keep_recent] if keep_recent else mine
        notable_older = [r for r in older if r["notable"]]
        kept = {r["id"] for r in recent} | {r["id"] for r in (notable_older[-keep_notable:]
                                                             if keep_notable else [])}
        window_evicted = {r["id"] for r in mine if r["id"] not in kept}
        self.runs = [r for r in self.runs if r["id"] not in window_evicted]
        for staged, cap in ((False, self.caps.retention_bytes), (True, self.caps.staging_bytes)):
            pool = [r for r in self.runs if r["staged"] == staged]
            total = sum(r["size"] for r in pool)
            if staged:
                first = [r for r in pool if not r["notable"]]
            else:
                recent_ids = set()
                for rid, (keep_recent, _) in self.windows.items():
                    mine = [r for r in self.runs if r["rid"] == rid]
                    if keep_recent:
                        recent_ids.update(r["id"] for r in mine[-keep_recent:])
                first = [r for r in pool if r["notable"] and r["id"] not in recent_ids]
            removed = set()
            for r in first + pool:
                if total <= cap:
                    break
                if r["id"] in removed:
                    continue
                removed.add(r["id"])
                total -= r["size"]
            self.runs = [r for r in self.runs if r["id"] not in removed]
        return window_evicted


#: A shrunk counterexample hypothesis found: a registration with fewer runs
#: than its window keeps them all, in the window step and when the device
#: cap judges which runs are recent.
_WINDOW_EXAMPLE = (
    ["wf-a:1", "wf-c:1"], {"wf-a:1": (2, 0), "wf-c:1": (4, 1)},
    [("continuous", "wf-a:1", "failed", 250), ("other", None, "plain", 60),
     ("continuous", "wf-a:1", "failed", 250), ("other", None, "plain", 60), ("other", None, "plain", 60),
     ("continuous", "wf-c:1", "failed", 250), ("continuous", "wf-c:1", "plain", 60),
     ("other", None, "plain", 60), ("other", None, "plain", 60), ("continuous", "wf-c:1", "activated", 500)],
    False, SimpleNamespace(retention_bytes=1500, staging_bytes=1200))


@settings(deadline=None)
@given(scenario=_scenarios())
@example(scenario=_WINDOW_EXAMPLE)
def test_retention_invariants(scenario):
    """**Feature: rtsp-rtmp-stream-cameras, Property 22: Retention
    invariants**

    **Validates: Requirements 12.1, 12.2, 12.4, 12.8**
    """
    registrations, windows, events, staging, caps = scenario
    root = tempfile.mkdtemp(prefix="retention-property-")
    try:
        captures = os.path.join(root, "captures")
        staging_root = os.path.join(root, "shm", "dda-continuous")
        os.makedirs(captures)
        session_factory = make_session_factory()
        retention = RunRetention(session_factory=session_factory, persistent_root=captures,
                                 staging_candidate=staging_root if staging else os.path.join(root, "absent", "x"),
                                 limits=lambda: caps, debug_log_path=None, min_staging_free_bytes=0)
        if not staging:
            # Staging unavailable: the candidate cannot be created.
            open(os.path.join(root, "absent"), "w").close()
        assert (retention.staging_root is not None) == staging

        model = Model(windows, caps)
        protected = []
        ids = itertools.count(1)
        for tick, (kind_of_run, rid, kind, size) in enumerate(events, start=1):
            execution_id = "exec-{0:03d}".format(next(ids))
            workflow_id = (rid or "wf-b:1").split(":")[0]
            capture_id = "{0}-{1}".format(workflow_id, execution_id)
            if kind_of_run == "continuous":
                context = {"source": "continuous", "frameSeq": tick, "frameAcquiredAtMs": tick,
                           "tickAtMs": 1_790_000_000_000 + tick * 1000}
                registration_id = rid
                base = retention.capture_root_for(None, context) or captures
            else:
                context = {"source": "mqtt", "topic": "line/trigger"} if kind_of_run == "other" else None
                registration_id = "wf-b:1" if kind_of_run == "other" else registrations[0]
                base = captures
            directory = os.path.join(base, workflow_id, execution_id)
            gate = {"activated": "activated", "cleared": "cleared", "quiet-gate": "none"}.get(kind)
            tags = {"is_anomalous": False}
            if gate is not None:
                tags["event"] = {"gate_1": {"state": "active", "transition": gate}}
            tags["stream"] = {"cam": {"seq": tick}}
            _write_run(directory, capture_id, size, tags)
            run_bytes = _bytes(directory)
            detail = {"sent": "sent to topic 'line/alerts' (qos 1, retain false): {}",
                      "skipped": "not sent: condition 'counter.c.total > 3' evaluated false"}.get(kind)
            node_status = {"cam": {"status": "success"}}
            if detail is not None:
                node_status["out1"] = {"status": "success", "detail": detail}
            session = session_factory()
            session.add(WorkflowExecution(
                id=execution_id, registration_id=registration_id, started_at=int(time.time()),
                finished_at=int(time.time()), status="failed" if kind == "failed" else "completed",
                capture_id=capture_id, output_dir=directory, log_path=os.path.join(directory, "run.log"),
                node_status_json=json.dumps(node_status),
                trigger_context_json=json.dumps(context) if context is not None else None))
            session.commit()
            session.close()

            if kind_of_run != "continuous":
                protected.append((execution_id, directory))
                # The retention never retains or deletes a run that is not
                # continuous, even when asked.
                outcome = retention.on_run_complete(execution_id, 0, 0, output_ids=("out1",))
                assert outcome.status == "completed"
                continue

            notable = kind in _NOTABLE
            outcome = retention.on_run_complete(execution_id, *windows[rid], output_ids=("out1",),
                                                stream_node_id="cam")
            assert outcome.notable == notable
            assert outcome.outputs_sent == (1 if kind == "sent" else 0)
            assert outcome.processed_seq == tick
            model.complete({"id": execution_id, "rid": rid, "notable": notable, "size": run_bytes,
                            "staged": staging and not notable})

            # Exactly the model's runs are retained, per registration, with
            # rows and directories agreeing.
            session = session_factory()
            rows = {row.id: row for row in session.query(WorkflowExecution).all()}
            session.close()
            for registration_id in registrations:
                expected = [r["id"] for r in model.runs if r["rid"] == registration_id]
                assert retention.retained(registration_id) == expected
            retained = {r["id"]: r for r in model.runs}
            continuous_rows = {execution_id for execution_id, row in rows.items()
                               if row.trigger_context_json and '"continuous"' in row.trigger_context_json}
            assert continuous_rows == set(retained)
            for run in model.runs:
                row = rows[run["id"]]
                assert os.path.isdir(row.output_dir)
                assert row.log_path == os.path.join(row.output_dir, "run.log")
                # Notable runs are persisted; plain runs stay staged.
                assert row.output_dir.startswith(staging_root if run["staged"] else captures)
            # Within the caps.
            persisted = sum(r["size"] for r in model.runs if not r["staged"])
            staged_bytes = sum(r["size"] for r in model.runs if r["staged"])
            assert persisted <= caps.retention_bytes and staged_bytes <= caps.staging_bytes
            assert retention.usage() == {"persistedBytes": persisted, "stagedBytes": staged_bytes}
            # Nothing else is ever deleted.
            for execution_id_kept, directory_kept in protected:
                assert execution_id_kept in rows
                assert os.path.isdir(directory_kept)
            # Deleted runs leave no directory behind.
            for base_root in (staging_root, captures):
                if not os.path.isdir(base_root):
                    continue
                for workflow_name in os.listdir(base_root):
                    for name in os.listdir(os.path.join(base_root, workflow_name)):
                        assert name in rows, name
    finally:
        shutil.rmtree(root, ignore_errors=True)
