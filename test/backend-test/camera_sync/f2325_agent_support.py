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
"""The scripted camera-registry shadow of the task 30.3 agent tests
(rtsp-rtmp-stream-cameras, finding 23). Not a test module:
``test_f2325_at_most_once.py`` and ``test_property_f2325_at_most_once.py``
import it, beside ``f2122_stream_agent_support`` (whose real-accessor world
they run on).

:class:`ScriptedShadow` applies AWS IoT update semantics (f2122's ``merge``:
nested maps merge, a null deletes a key) and scripts what thor1 showed:

- ``fail_desired`` / ``fail_reported`` fail that many of the agent's next
  desired / reported writes with ``TimeoutError``; a failed write changes
  nothing (:meth:`ScriptedShadow.portal_writes`, the Portal's write, always
  lands);
- ``get_script`` holds the outcomes of the next GETs, in order: ``None`` (the
  accessor's answer for an unreadable shadow), ``False`` (no shadow), an
  exception (raised), or :data:`STATE` (the merged document, the default);
- ``on_get``, when set, runs once inside the next GET, after the document
  was read and before the GET returns (a write landing in a retry's window);
- ``on_null(csid, entry)``, when set, is told of every ``desired.changes``
  entry a landed write nulls, with the entry it held.
"""
import copy
import threading
from typing import Mapping

from f2122_stream_agent_support import merge

THING = "thing"
SHADOW = "dda-camera-registry"
#: A GET outcome: the merged document.
STATE = "state"


class ScriptedShadow:
    """See the module docstring. ``reported`` and ``desired`` keep the
    writes that landed, ``failed`` the ones that failed; ``gets`` counts the
    GETs."""

    def __init__(self, state=None):
        self.state = copy.deepcopy(state) if state else {}
        self.reported = []
        self.desired = []
        self.failed = []
        self.gets = 0
        self.fail_desired = 0
        self.fail_reported = 0
        self.get_script = []
        self.on_get = None
        self.on_null = None
        self._lock = threading.RLock()

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        with self._lock:
            self.gets += 1
            outcome = self.get_script.pop(0) if self.get_script else STATE
            state = copy.deepcopy(self.state)
            hook, self.on_get = self.on_get, None
        if hook is not None:
            hook()
        if isinstance(outcome, BaseException):
            raise outcome
        return state if outcome == STATE else outcome

    def update_thing_shadow_state_request(self, thing_name, shadow_name, update):
        with self._lock:
            if "desired" in update and self.fail_desired > 0:
                self.fail_desired -= 1
                self.failed.append(copy.deepcopy(update))
                raise TimeoutError("the desired-entry clear timed out")
            if "reported" in update and self.fail_reported > 0:
                self.fail_reported -= 1
                self.failed.append(copy.deepcopy(update))
                raise TimeoutError("the report timed out")
            nulled = []
            desired_update = update.get("desired")
            changes_update = (desired_update.get("changes")
                              if isinstance(desired_update, Mapping) else None)
            if isinstance(changes_update, Mapping):
                held = self.desired_changes()
                nulled = [(csid, held.get(csid)) for csid, value in changes_update.items()
                          if value is None]
            merge(self.state, update)
            if "reported" in update:
                self.reported.append(copy.deepcopy(update["reported"]))
            if "desired" in update:
                self.desired.append(copy.deepcopy(update["desired"]))
            hook = self.on_null
        if hook is not None:
            for csid, entry in nulled:
                hook(csid, entry)
        return b'{"state": {}}'

    def portal_writes(self, changes):
        """The Portal's desired write (``camera_registry.write_desired_change``).
        It lands whatever ``fail_desired`` says: only the agent's writes are
        scripted to fail."""
        update = {"desired": {"changes": copy.deepcopy(changes)}}
        with self._lock:
            merge(self.state, update)
            self.desired.append(copy.deepcopy(update["desired"]))

    def desired_changes(self):
        with self._lock:
            desired = self.state.get("desired") or {}
            return copy.deepcopy(desired.get("changes") or {})

    def delta(self):
        """What a delta carries now: every ``desired.changes`` entry (a delta
        is desired minus reported, and ``changes`` is never reported)."""
        return {"state": {"changes": self.desired_changes()}}


class IdlePinWorker:
    """A pin worker that does nothing, for agents whose real worker thread
    runs (``start()``): no pin-worker threads outlive the test."""

    report_inventory = None

    def applied_request_id(self):
        return None

    def on_desired(self, desired):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def null_writes(shadow):
    """The landed desired writes that only null ``changes`` entries: the
    agent's clears, first and retried."""
    return [document for document in shadow.desired
            if isinstance(document.get("changes"), Mapping) and document["changes"]
            and all(value is None for value in document["changes"].values())]


def acked_cameras(shadow, change_id):
    """The cfg- cameras the landed reports acknowledged ``change_id`` on."""
    return sorted({csid for document in shadow.reported
                   for csid, entry in (document.get("cameras") or {}).items()
                   if csid.startswith("cfg-") and isinstance(entry, Mapping)
                   and entry.get("ack") == change_id})


def reported_failures(shadow):
    """``(csid, portalChangeId, reason)`` of every failure a landed report
    carried, one per report that carried it."""
    return [(csid, failure.get("portalChangeId"), failure.get("reason"))
            for document in shadow.reported
            for csid, failure in (document.get("failures") or {}).items()
            if isinstance(failure, Mapping)]
