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
"""Property test for the device's handling of deletes of cameras it never
created (rtsp-rtmp-stream-cameras task 29.3; the device clauses of Property
30; task 29.5 adds the registry clauses on the Portal side).

**Feature: rtsp-rtmp-stream-cameras, Property 30: Deletes of cameras the
device never created, and the registry's removal of pending deletes**

*For any* configured cameras, unreported create aliases, failure keys the
shadow holds (known to the device from its start-time read or from its own
write, or unknown because that read failed), and one change of any op to any
id (``cfg-`` existing and missing, ``disc-``, ``arv-``, both static ids,
``portal-<hex>``, and ``cam-1``, ``cfg``, ``CFG-1``, ``static-image-camera-2``):

- the change is refused as ``discovery-managed`` exactly when it targets a
  ``disc-`` id or a static id (creates included), is an update, a delete or
  an unsupported op on an ``arv-`` id, or is anything but a create or a
  delete on any other id without the ``cfg-`` prefix; every other create, and
  every change to a ``cfg-`` id, behaves as before;
- a delete of an unreported create alias deletes exactly the camera that
  create made;
- any other delete changes no Image_Source and reports no failure for the
  id, and the next successful report carries a null for the id's failure key
  exactly when the device knows the shadow holds it and the report carries
  no new failure for the id.

**Validates: Requirement 5.11**

Each example runs the real ``ImageSourceAccessor`` over a fresh sqlite
database and a shadow with AWS update semantics; the deadline is disabled.
"""
import itertools
import tempfile

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from f2122_stream_agent_support import MergingShadow, make_stream_world, rtsp_change

from camera_sync.agent import REASON_DISCOVERY_MANAGED

_PORTAL_IDS = ("portal-a1b2c3d4e5f6", "portal-be48f52dd98d", "portal-0f0f0f0f0f0f")
_FIXED_IDS = ("cfg-missing", "disc-000000000001", "arv-0123456789ab", "static-image-camera",
              "static-video-camera", "cam-1", "cfg", "CFG-1", "static-image-camera-2")
#: Ids whose update fails, so this process can write a failure for them.
_FAILABLE_IDS = _FIXED_IDS + _PORTAL_IDS
_OPS = ("create", "update", "delete", "frobnicate")
_SEEDED_FAILURE = {"reason": REASON_DISCOVERY_MANAGED, "portalChangeId": "pc-seed"}


def _refused(csid, op):
    if csid.startswith("disc-") or csid in ("static-image-camera", "static-video-camera"):
        return True
    if csid.startswith("cfg-") or op == "create":
        return False
    if csid.startswith("arv-"):
        return True
    return op != "delete"


def _never_created_delete(csid, op):
    return op == "delete" and not csid.startswith("cfg-") and not _refused(csid, op)


@st.composite
def _scenario(draw):
    configured = draw(st.integers(min_value=0, max_value=2))
    aliases = draw(st.lists(st.sampled_from(_PORTAL_IDS), unique=True, max_size=2))
    seeded = set(draw(st.lists(st.sampled_from(_FAILABLE_IDS), unique=True, max_size=3)))
    written = set(draw(st.lists(st.sampled_from(_FAILABLE_IDS), unique=True, max_size=2)))
    get_ok = draw(st.booleans())
    # The target: an unreported alias, another portal id, a fixed id, or an
    # existing cfg- camera (by index).
    kind = draw(st.sampled_from(["alias", "portal", "fixed", "cfg-existing"]))
    if kind == "alias" and aliases:
        target = draw(st.sampled_from(aliases))
    elif kind == "cfg-existing":
        configured = max(configured, 1)
        target = ("cfg-existing", draw(st.integers(min_value=0, max_value=configured - 1)))
    elif kind == "fixed":
        target = draw(st.sampled_from(_FIXED_IDS))
    else:
        target = draw(st.sampled_from(_PORTAL_IDS))
    if not isinstance(target, tuple):
        # The target's own failure key: none, one an earlier process left,
        # or one this process writes.
        seeded.discard(target)
        written.discard(target)
        key = draw(st.sampled_from(["written", "seeded", "none"]))
        if key == "seeded":
            seeded.add(target)
        elif key == "written":
            written.add(target)
    op = draw(st.sampled_from(("delete",) + _OPS))
    then_refused_update = draw(st.booleans())
    return (configured, aliases, sorted(seeded), sorted(written), get_ok, target, op,
            then_refused_update)


def _create_configured(world, count):
    ids = []
    for index in range(count):
        with world.factory() as db:
            result = world.accessor.create_image_source(
                {"name": "configured-{}".format(index), "type": "RTSP",
                 "location": "rtsp://10.0.4.{}:554/s".format(30 + index),
                 "streamSettings": {"latencyMs": 300}}, db)
        ids.append(str(result["imageSourceId"]))
    return ids


@settings(deadline=None)
@given(scenario=_scenario())
def test_deletes_of_cameras_the_device_never_created(scenario):
    """**Feature: rtsp-rtmp-stream-cameras, Property 30: Deletes of cameras
    the device never created, and the registry's removal of pending
    deletes** (device clauses)

    **Validates: Requirement 5.11**
    """
    configured, aliases, seeded, written, get_ok, target, op, then_refused_update = scenario
    pcids = ("pc-{}".format(n) for n in itertools.count(1))
    shadow = MergingShadow({"reported": {"schemaVersion": 1, "cameras": {}, "failures": {
        csid: dict(_SEEDED_FAILURE) for csid in seeded}}})

    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as monkeypatch:
        world = make_stream_world(tmp, monkeypatch, shadow=shadow)
        try:
            configured_ids = _create_configured(world, configured)
            if isinstance(target, tuple):
                csid = "cfg-" + configured_ids[target[1] % len(configured_ids)]
            else:
                csid = target

            shadow.readable = get_ok
            agent = world.make_agent(refresh=True)
            shadow.readable = True

            # Failures this process writes (and so knows the shadow holds).
            for failing in sorted(written):
                agent.apply_desired_changes({failing: rtsp_change("update", next(pcids), ref=None)})
                agent.pump()
                assert isinstance(shadow.reported[-1]["failures"].get(failing), dict)
            known = set(written) | (set(seeded) if get_ok else set())

            # Unreported create aliases: applied, never reported.
            alias_targets = {}
            for alias in aliases:
                before = set(world.image_sources())
                agent.apply_desired_changes({alias: rtsp_change("create", next(pcids), ref=None)})
                [made] = set(world.image_sources()) - before
                alias_targets[alias] = made

            sources_before = set(world.image_sources())
            calls_before = len(world.accessor.calls)
            pcid = next(pcids)
            change = ({"op": "delete", "portalChangeId": pcid} if op == "delete"
                      else rtsp_change(op, pcid, ref=None))
            agent.apply_desired_changes({csid: change})
            rule_4 = _never_created_delete(csid, op)
            follow_up = None
            if rule_4 and then_refused_update:
                follow_up = next(pcids)
                agent.apply_desired_changes({csid: rtsp_change("update", follow_up, ref=None)})
            agent.pump()

            document = shadow.reported[-1]
            failures = document["failures"]
            calls = world.accessor.calls[calls_before:]
            sources_after = set(world.image_sources())
            nulls = {key for key, failure in failures.items() if failure is None}

            if _refused(csid, op):
                event("refused")
                assert failures[csid] == {"reason": REASON_DISCOVERY_MANAGED, "portalChangeId": pcid}
                assert calls == [] and sources_after == sources_before
                assert nulls == set()
            elif rule_4:
                created = alias_targets.get(csid)
                event("never-created delete of an unreported alias" if created is not None
                      else "never-created delete")
                if created is not None:
                    assert calls == [("delete", created)]
                    assert sources_after == sources_before - {created}
                    assert document["cameras"].get("cfg-" + created) is None
                else:
                    assert calls == [] and sources_after == sources_before
                assert document["cameras"].get(csid) is None
                event("new failure wins" if follow_up is not None else
                      "known failure key nulled" if csid in known else "no failure key known")
                if follow_up is not None:
                    assert failures[csid] == {"reason": REASON_DISCOVERY_MANAGED,
                                              "portalChangeId": follow_up}
                    assert nulls == set()
                elif csid in known:
                    assert csid in failures and failures[csid] is None
                    assert nulls == {csid}
                    assert csid not in shadow.failures
                else:
                    assert csid not in failures and nulls == set()
            elif op == "create":
                event("create applied")
                [(_, made_type, _)] = calls
                assert made_type == "RTSP"
                [made] = sources_after - sources_before
                assert document["cameras"]["cfg-" + made]["ack"] == pcid
                assert csid not in failures and nulls == set()
            else:
                image_source_id = csid[len("cfg-"):]
                exists = image_source_id in sources_before
                event("cfg- {} of {} camera".format(op, "an existing" if exists else "a missing"))
                if op == "frobnicate":
                    assert calls == []
                    assert failures[csid] == {"reason": "unsupported operation 'frobnicate'",
                                              "portalChangeId": pcid}
                elif exists and op == "update":
                    assert calls == [("update", image_source_id, None)]
                    assert document["cameras"][csid]["ack"] == pcid and csid not in failures
                elif exists:
                    assert calls == [("delete", image_source_id)]
                    assert sources_after == sources_before - {image_source_id}
                    assert csid not in failures
                else:
                    assert failures[csid]["portalChangeId"] == pcid
                    assert failures[csid]["reason"] and failures[csid]["reason"] != REASON_DISCOVERY_MANAGED
                assert nulls == set()
        finally:
            world.close()
