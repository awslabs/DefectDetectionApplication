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
"""A delete of a camera the device never created is acknowledged
(rtsp-rtmp-stream-cameras task 29.3, finding 21 fix (c); Requirement 5.11;
design component 12, **Deletes of cameras the device never created**).
tasks.md names this file ``test_never_created_delete.py``; it carries the
``f2122`` marker so its basename is unique in the repository.

The agent runs over the real ``ImageSourceAccessor`` and a shadow with AWS
update semantics (``f2122_stream_agent_support.MergingShadow``: nested maps
merge, a null deletes a key). Each agent first runs the start-time shadow GET
(``_refresh_reported_versions``), as ``start()`` does.
"""
import logging
import threading

import pytest

from f2122_stream_agent_support import (
    SECRET_ARN,
    URL,
    MergingShadow,
    apply,
    assert_secret_free,
    capture_logs,
    delete_change,
    denied,
    granted,
    make_stream_world,
    not_found,
    report,
    rtsp_create,
    rtsp_update,
)

from camera_sync.agent import REASON_DISCOVERY_MANAGED

_OLD_FAILURE = {"reason": REASON_DISCOVERY_MANAGED, "portalChangeId": "pc-old"}


def _shadow_with_failure(csid="portal-x", failure=None):
    return MergingShadow({"reported": {"schemaVersion": 1, "cameras": {},
                                       "failures": {csid: dict(failure or _OLD_FAILURE)}}})


@pytest.fixture
def world(tmp_path, monkeypatch):
    world = make_stream_world(tmp_path, monkeypatch)
    yield world
    world.close()


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A world whose shadow holds a failure an earlier process or build
    left for ``portal-x``."""
    world = make_stream_world(tmp_path, monkeypatch, shadow=_shadow_with_failure())
    yield world
    world.close()


def _failed_create(world, agent, csid="portal-x", pcid="pc-1"):
    world.fetcher.outcomes.append(not_found())
    apply(agent, csid, rtsp_create(pcid))
    assert world.shadow.failures[csid] == {
        "reason": "credential retrieval failed: ResourceNotFoundException", "portalChangeId": pcid}


def _null_keys(document):
    return sorted(csid for csid, failure in document["failures"].items() if failure is None)


# --- finding 21 ---------------------------------------------------------------------


def test_finding_21_a_failed_create_can_be_deleted(world, caplog):
    """The Orin incident: a failed create left ``failures.portal-x``, and
    every delete the Portal sent got a new ``discovery-managed`` refusal.
    The delete is acknowledged, and its report removes the old failure."""
    capture_logs(caplog)
    agent = world.make_agent(refresh=True)
    _failed_create(world, agent)

    apply(agent, "portal-x", delete_change("pc-2"))

    document = world.shadow.reported[-1]
    assert document["failures"] == {"portal-x": None}
    assert "portal-x" not in world.shadow.failures
    assert world.image_sources() == {}
    assert [call[0] for call in world.accessor.calls] == []
    assert world.shadow.desired[-1] == {"changes": {"portal-x": None}}

    [line] = [record for record in caplog.records
              if record.name == "camera_sync.agent" and "never created" in record.getMessage()]
    assert line.levelno == logging.INFO
    assert "portal-x" in line.getMessage() and "pc-2" in line.getMessage()
    for value in (SECRET_ARN, URL):
        assert value not in line.getMessage()
    assert_secret_free(world.shadow, caplog)


# --- which failure keys are nulled ---------------------------------------------------------


def test_a_failure_key_seeded_from_the_start_get_is_nulled(seeded):
    agent = seeded.make_agent(refresh=True)
    apply(agent, "portal-x", delete_change("pc-2"))
    assert _null_keys(seeded.shadow.reported[-1]) == ["portal-x"]
    assert seeded.shadow.failures == {}


def test_a_failure_key_this_process_wrote_is_nulled(world):
    agent = world.make_agent(refresh=True)
    _failed_create(world, agent, csid="portal-y")
    apply(agent, "portal-y", delete_change("pc-2"))
    assert _null_keys(world.shadow.reported[-1]) == ["portal-y"]
    assert world.shadow.failures == {}


def test_a_delete_of_an_id_without_a_failure_key_writes_no_null(seeded):
    agent = seeded.make_agent(refresh=True)
    apply(agent, "portal-q", delete_change("pc-2"))
    document = seeded.shadow.reported[-1]
    assert document["failures"] == {}
    assert seeded.shadow.failures == {"portal-x": _OLD_FAILURE}


def test_the_null_survives_a_failed_write_and_is_written_once(seeded):
    agent = seeded.make_agent(refresh=True)
    seeded.shadow.failing = True
    apply(agent, "portal-x", delete_change("pc-2"))
    assert seeded.shadow.reported == []
    seeded.shadow.failing = False
    seeded.clock.now += 10  # past the write backoff
    agent.pump()
    assert _null_keys(seeded.shadow.reported[-1]) == ["portal-x"]
    assert seeded.shadow.failures == {}

    later = report(agent, seeded.shadow)
    assert "portal-x" not in later["failures"]


def test_a_new_failure_for_the_id_in_the_same_report_wins(seeded):
    agent = seeded.make_agent(refresh=True)
    agent.apply_desired_changes({"portal-x": delete_change("pc-2")})
    seeded.fetcher.outcomes.append(not_found())
    agent.apply_desired_changes({"portal-x": rtsp_create("pc-3")})
    agent.pump()

    new_failure = {"reason": "credential retrieval failed: ResourceNotFoundException",
                   "portalChangeId": "pc-3"}
    assert seeded.shadow.reported[-1]["failures"] == {"portal-x": new_failure}
    assert seeded.shadow.failures == {"portal-x": new_failure}
    assert "portal-x" not in report(agent, seeded.shadow)["failures"]

    # The device knows it wrote that failure: a later delete nulls it.
    apply(agent, "portal-x", delete_change("pc-4"))
    assert _null_keys(seeded.shadow.reported[-1]) == ["portal-x"]
    assert seeded.shadow.failures == {}


def test_without_a_readable_start_get_an_earlier_failure_gets_no_null(seeded):
    """The empty seed of design component 12: the device does not know of a
    failure an earlier process left, so it writes no null; the key stays in
    the shadow, where the registry ignores it once the entry is gone."""
    seeded.shadow.readable = False
    agent = seeded.make_agent(refresh=True)
    seeded.shadow.readable = True
    apply(agent, "portal-x", delete_change("pc-2"))
    assert seeded.shadow.reported[-1]["failures"] == {}
    assert seeded.shadow.failures == {"portal-x": _OLD_FAILURE}


# --- races with the create -------------------------------------------------------------------


def test_a_delete_racing_an_unreported_create_deletes_the_created_camera(world):
    agent = world.make_agent(refresh=True)
    agent.apply_desired_changes({"portal-x": rtsp_create("pc-1", ref=None)})
    [image_source_id] = world.image_sources()

    apply(agent, "portal-x", delete_change("pc-2"))

    assert world.image_sources() == {}
    assert world.accessor.calls[-1] == ("delete", image_source_id)
    assert world.stream.deleted == [image_source_id]
    document = world.shadow.reported[-1]
    assert "portal-x" not in document["cameras"] and "cfg-" + image_source_id not in document["cameras"]
    assert document["failures"] == {}
    assert world.shadow.cameras == {}


def test_a_delete_while_the_alias_report_is_written_deletes_the_camera(world):
    """The delete arrives while the report carrying the alias is being
    written: the alias is consumed only when that write returns, so the
    delete still deletes the camera, and the next report retires both
    keys."""
    agent = world.make_agent(refresh=True)
    agent.apply_desired_changes({"portal-x": rtsp_create("pc-1", ref=None)})
    [image_source_id] = world.image_sources()
    created = "cfg-" + image_source_id

    entered, release = world.shadow.hold_next_reported_write()
    errors = []

    def write_the_alias_report():
        try:
            agent.pump()
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    writer = threading.Thread(target=write_the_alias_report, name="f2122-report", daemon=True)
    writer.start()
    assert entered.wait(10), "the alias report was never written"
    agent.apply_desired_changes({"portal-x": delete_change("pc-2")})
    assert world.image_sources() == {}
    release.set()
    writer.join(10)
    assert not writer.is_alive() and errors == []

    alias_report = world.shadow.reported[-1]
    assert alias_report["cameras"][created]["ack"] == "pc-1"
    assert alias_report["cameras"]["portal-x"]["ack"] == "pc-1"

    agent.pump()
    retiring = world.shadow.reported[-1]
    assert retiring["cameras"] == {created: None, "portal-x": None}
    assert retiring["failures"] == {}
    assert world.shadow.cameras == {}


def test_a_delete_after_the_alias_report_is_acknowledged_and_the_camera_stays(world):
    agent = world.make_agent(refresh=True)
    apply(agent, "portal-x", rtsp_create("pc-1", ref=None))
    [image_source_id] = world.image_sources()
    created = "cfg-" + image_source_id

    apply(agent, "portal-x", delete_change("pc-2"))

    assert list(world.image_sources()) == [image_source_id]
    assert [call[0] for call in world.accessor.calls] == ["create"]
    document = world.shadow.reported[-1]
    assert created in document["cameras"] and document["cameras"]["portal-x"] is None
    assert document["failures"] == {}
    assert set(world.shadow.cameras) == {created}


# --- a parked create -------------------------------------------------------------------------


def test_a_delete_of_a_parked_create_cancels_its_retry(world):
    agent = world.make_agent(refresh=True)
    world.fetcher.outcomes = [denied(), granted()]
    apply(agent, "portal-x", rtsp_create("pc-1"))
    assert world.retry_timer.delays == [2.0]

    apply(agent, "portal-x", delete_change("pc-2"))
    assert world.shadow.reported[-1]["failures"] == {}

    world.retry_timer.fire_next()
    agent.pump()
    assert len(world.fetcher.calls) == 1
    assert world.image_sources() == {} and world.accessor.calls == []


# --- the refusals that stay (Requirement 5.11) ----------------------------------------------


@pytest.mark.parametrize("csid, change", [
    ("disc-000000000001", delete_change("pc-9")),
    ("static-image-camera", delete_change("pc-9")),
    ("static-video-camera", delete_change("pc-9")),
    ("arv-0123456789ab", delete_change("pc-9")),
    ("arv-0123456789ab", rtsp_update("pc-9", ref=None)),
    ("portal-x", rtsp_update("pc-9", ref=None)),
    ("portal-x", dict(rtsp_update("pc-9", ref=None), op="frobnicate")),
])
def test_the_refusals_that_stay_never_null_a_failure_key(seeded, csid, change):
    agent = seeded.make_agent(refresh=True)
    apply(agent, csid, change)
    document = seeded.shadow.reported[-1]
    assert document["failures"] == {csid: {"reason": REASON_DISCOVERY_MANAGED, "portalChangeId": "pc-9"}}
    assert seeded.accessor.calls == []
    assert seeded.shadow.failures[csid] == document["failures"][csid]
    if csid != "portal-x":
        assert seeded.shadow.failures["portal-x"] == _OLD_FAILURE


def test_a_delete_of_a_missing_cfg_id_still_fails_with_the_accessors_404(world):
    agent = world.make_agent(refresh=True)
    apply(agent, "cfg-missing", delete_change("pc-9"))
    failure = world.shadow.reported[-1]["failures"]["cfg-missing"]
    assert failure["portalChangeId"] == "pc-9"
    assert "doesn't exist" in failure["reason"]
