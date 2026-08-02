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
"""Property test for the Edge_Sync_Agent portal-change apply/report round trip.

**Feature: camera-registry-sync, Property 7: Portal change apply/report
round trip**

*For any* portal-originated Camera_Source change, applying it through the
Edge_Sync_Agent against a device database and reducing the resulting report
in the Portal ends with: for schema-valid changes, the device-local state
matching the change, the report acknowledging the ``portal_change_id``, and
the registry entry marked ``synced`` with the applied content; for
schema-invalid changes, the device-local state unchanged and the registry
entry marked ``failed`` with a non-empty reason.

**Validates: Requirements 5.2, 5.3, 5.4**

Harness:

- The device database is a REAL in-memory SQLite database (``StaticPool``
  so every session shares the one in-memory connection) carrying the real
  ORM schema, accessed through the REAL ``ImageSourceAccessor`` so the
  exact accessor validation and error messages travel the round trip
  (Requirement 5.2). The hardware/filesystem side effects (camera-manager
  connect/disconnect/status, folder creation, Aravis default-config
  lookup) are patched out exactly like the existing
  ``resources/test_image_source_accessor.py`` suite does.
- The Portal side is the real ``reduce_report`` reducer imported from
  ``edge-cv-portal/backend/functions/camera_sync.py`` (loaded by file path
  under a distinct module name — the edge package is also called
  ``camera_sync``). Only the ingest handler's reduction loop (cameras ->
  failures -> deletion candidates, mirroring ``_process_report``) is
  re-implemented here over an in-memory registry dict in place of
  DynamoDB.
- The agent is driven deterministically through :meth:`EdgeSyncAgent.pump`
  with a fake clock and a recording fake shadow accessor, like the other
  camera_sync suites.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100); the
deadline is disabled because every example builds a real database.
"""
import contextlib
import importlib.util
import os
import pathlib
import tempfile
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from camera_discovery import DiscoveredCamera, DiscoveryResult
from camera_sync import (
    REASON_DISCOVERY_MANAGED,
    CameraSyncStateStore,
    EdgeSyncAgent,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PORTAL_CAMERA_SYNC_PATH = (
    _REPO_ROOT / "edge-cv-portal" / "backend" / "functions" / "camera_sync.py"
)
_DEFAULT_CAMERA_CONFIG_PATH = (
    _REPO_ROOT / "src" / "backend" / "utils" / "config"
    / "default_camera_configurations.json"
)

#: Default image-source configuration returned in place of the Aravis
#: camera lookup (mirrors resources/test_image_source_accessor.py).
_DEFAULT_CONFIG = {
    "gain": 1,
    "exposure": 500,
    "processingPipeline": "videoconvert",
}


# --- lazy real-stack / portal-reducer loading ---------------------------------

_portal_module = None
_real_modules = None


def _portal_sync():
    """The real Portal_Sync_Service reducer module, loaded by file path
    under a distinct name (the edge package is also ``camera_sync``)."""
    global _portal_module
    if _portal_module is None:
        spec = importlib.util.spec_from_file_location(
            "portal_camera_sync_for_property_7", str(_PORTAL_CAMERA_SYNC_PATH)
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _portal_module = module
    return _portal_module


def _real_stack():
    """The real accessor module and ORM declarative base, imported lazily
    (inside the test run) so the conftest's import mocks are in place."""
    global _real_modules
    if _real_modules is None:
        os.environ.setdefault("COMPONENT_WORK_PATH", tempfile.gettempdir())
        import resources.accessors.image_source_accessor as accessor_module
        from dao.sqlite_db.sqlite_db_operations import Base
        import dao.sqlite_db.models  # noqa: F401 - registers tables on Base

        _real_modules = (accessor_module, Base)
    return _real_modules


def _make_device_db():
    """A fresh REAL in-memory SQLite device database with the real schema.

    ``StaticPool`` shares the single in-memory connection across every
    session the agent and the test open.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    _, Base = _real_stack()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


@contextlib.contextmanager
def _real_accessor():
    """The REAL ImageSourceAccessor with only its hardware/filesystem side
    effects patched out (camera manager, folder creation, Aravis default
    configuration) — schema validation and error messages stay real."""
    accessor_module, _ = _real_stack()
    with mock.patch.object(accessor_module, "connect_camera"), \
            mock.patch.object(accessor_module, "disconnect_camera"), \
            mock.patch.object(
                accessor_module, "get_camera_status", return_value=mock.Mock()
            ), \
            mock.patch(
                "utils.constants.DEFAULT_CAMERA_CONFIG_FILE_PATH",
                str(_DEFAULT_CAMERA_CONFIG_PATH),
            ):
        accessor = accessor_module.ImageSourceAccessor()
        accessor._ImageSourceAccessor__create_folder = mock.Mock(
            return_value=None
        )
        # side-effect style: a FRESH dict per call — the config accessor
        # mutates the dict it is given (adds imageSourceConfigId).
        accessor._ImageSourceAccessor__get_default_image_source_configuration = (
            lambda camera_id: dict(_DEFAULT_CONFIG)
        )
        yield accessor


# --- fakes (same patterns as the sibling camera_sync suites) -------------------


class _FakeClock:
    def __init__(self, start: float = 1_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeShadowAccessor:
    """Records reported documents and desired writes separately."""

    def __init__(self):
        self.reported_writes = []
        self.desired_writes = []

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return None

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        if "reported" in state:
            self.reported_writes.append(state["reported"])
        if "desired" in state:
            self.desired_writes.append(state["desired"])


class _FakeDiscovery:
    def __init__(self, snapshot):
        self.latest_snapshot = snapshot


def _flush(agent, clock, max_iterations=10):
    for _ in range(max_iterations):
        delay = agent.pump()
        if delay is None:
            return
        clock.advance(delay + 0.001)
    raise AssertionError("agent never went idle")


# --- generators ----------------------------------------------------------------

_NAME = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=15,
)
_GAIN = st.integers(min_value=0, max_value=48)
_EXPOSURE = st.integers(min_value=1, max_value=5_000_000)
_PIPELINE = st.sampled_from(
    ["videoconvert", "capsfilter caps=video/x-raw,format=RGBA ! videoconvert"]
)

#: Change kinds whose apply is expected to succeed (schema-valid).
_VALID_KINDS = frozenset({
    "valid_update_name",
    "valid_update_config",
    "valid_delete",
    "valid_create_camera",
    "valid_create_folder",
})


@st.composite
def _scenario(draw):
    """(initial Image_Source specs, discovered-camera count, change specs).

    Change kinds cover valid and invalid creates/updates/deletes plus
    discovery-managed targets; kinds needing an existing configured or
    discovered source are only offered when one exists.
    """
    source_specs = []
    for index in range(draw(st.integers(min_value=0, max_value=2))):
        if draw(st.sampled_from(["Camera", "Folder"])) == "Camera":
            source_specs.append({
                "name": draw(_NAME),
                "type": "Camera",
                "cameraId": "cam-{}".format(index),
                "imageSourceConfiguration": {
                    "device": "/dev/video{}".format(index),
                    "gain": draw(_GAIN),
                    "exposure": draw(_EXPOSURE),
                    "processingPipeline": draw(_PIPELINE),
                },
            })
        else:
            source_specs.append({
                "name": draw(_NAME),
                "type": "Folder",
                "location": "/tmp/dda-prop7-{}".format(index),
            })
    disc_count = draw(st.integers(min_value=0, max_value=1))

    kinds = [
        "valid_create_camera",
        "valid_create_folder",
        "invalid_create_folder",
        "invalid_create_camera_config",
        "invalid_update_missing",
        "invalid_delete_missing",
        "discovery_managed_noncfg",
    ]
    camera_indices = [
        i for i, s in enumerate(source_specs) if s["type"] == "Camera"
    ]
    if source_specs:
        kinds += [
            "valid_update_name",
            "valid_delete",
            "invalid_update_bad_type",
            "invalid_update_partial_config",
        ]
    if camera_indices:
        kinds.append("valid_update_config")
    if disc_count:
        kinds.append("discovery_managed_disc")

    change_specs = []
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        kind = draw(st.sampled_from(kinds))
        spec = {"kind": kind}
        if kind in ("valid_update_name", "valid_delete",
                    "invalid_update_bad_type", "invalid_update_partial_config"):
            spec["index"] = draw(
                st.integers(min_value=0, max_value=len(source_specs) - 1)
            )
        if kind == "valid_update_config":
            spec["index"] = draw(st.sampled_from(camera_indices))
        if kind in ("valid_update_name", "valid_update_config",
                    "valid_create_camera", "valid_create_folder",
                    "invalid_create_folder", "invalid_create_camera_config"):
            spec["name"] = draw(_NAME)
        if kind in ("valid_update_config", "valid_create_camera"):
            spec["device"] = "/dev/video{}".format(
                draw(st.integers(min_value=0, max_value=7))
            )
            spec["gain"] = draw(_GAIN)
            spec["exposure"] = draw(_EXPOSURE)
            spec["pipeline"] = draw(_PIPELINE)
        if kind in ("invalid_update_partial_config",
                    "invalid_create_camera_config"):
            spec["gain"] = draw(_GAIN)
        change_specs.append(spec)
    return source_specs, disc_count, change_specs


# --- scenario materialization ---------------------------------------------------


def _create_initial_sources(accessor, session_factory, source_specs):
    """Create the pre-existing configured Image_Sources through the real
    accessor (create, then attach the full configuration exactly like the
    agent's own create path does)."""
    ids = []
    for spec in source_specs:
        data = {"name": spec["name"], "type": spec["type"]}
        if spec["type"] == "Camera":
            data["cameraId"] = spec["cameraId"]
        else:
            data["location"] = spec["location"]
        with session_factory() as session:
            new_id = str(accessor.create_image_source(data, session)["imageSourceId"])
            if spec["type"] == "Camera":
                accessor.update_image_source(
                    new_id,
                    {"imageSourceConfiguration": dict(spec["imageSourceConfiguration"])},
                    session,
                )
        ids.append(new_id)
    return ids


def _make_snapshot(count):
    """Discovered cameras on paths disjoint from every configured/updated
    device path (/dev/video0..7), so merge behavior stays out of scope."""
    return DiscoveryResult(cameras=[
        DiscoveredCamera(
            stable_id="disc-{:012d}".format(index),
            device_path="/dev/video9{}".format(index),
            card_name="USB Camera {}".format(index),
            bus_info="usb-0000:00:14.0-{}".format(index),
            driver="uvcvideo",
            kind="v4l2",
            formats=[{"pixel_format": "YUYV", "resolutions": [[1920, 1080]]}],
        )
        for index in range(count)
    ])


def _build_change(spec, position, source_ids, source_specs, snapshot):
    """Materialize one generated change spec into (csid, change document,
    expected classification: "valid" | "invalid" | "discovery")."""
    kind = spec["kind"]
    pcid = "pc-{}".format(position)

    def _cfg_csid():
        return "cfg-" + source_ids[spec["index"]]

    if kind == "valid_update_name":
        target = source_specs[spec["index"]]
        return _cfg_csid(), {
            "op": "update", "portalChangeId": pcid,
            "name": spec["name"], "type": target["type"], "params": {},
        }, "valid"
    if kind == "valid_update_config":
        target = source_specs[spec["index"]]
        return _cfg_csid(), {
            "op": "update", "portalChangeId": pcid,
            "name": spec["name"], "type": "Camera",
            "params": {
                "devicePath": spec["device"],
                "cameraId": target["cameraId"],
                "gain": spec["gain"],
                "exposure": spec["exposure"],
                "processingPipeline": spec["pipeline"],
            },
        }, "valid"
    if kind == "valid_delete":
        return _cfg_csid(), {
            "op": "delete", "portalChangeId": pcid, "baseVersion": 1,
        }, "valid"
    if kind == "valid_create_camera":
        return "portal-new-{}".format(position), {
            "op": "create", "portalChangeId": pcid,
            "name": spec["name"], "type": "Camera",
            "params": {
                "cameraId": "cam-new-{}".format(position),
                "devicePath": spec["device"],
                "gain": spec["gain"],
                "exposure": spec["exposure"],
                "processingPipeline": spec["pipeline"],
            },
        }, "valid"
    if kind == "valid_create_folder":
        return "portal-new-{}".format(position), {
            "op": "create", "portalChangeId": pcid,
            "name": spec["name"], "type": "Folder",
            "params": {"location": "/tmp/dda-prop7-new-{}".format(position)},
        }, "valid"
    if kind == "invalid_create_folder":
        # Folder without a location: the real ImageSourceSchema rejects it.
        return "portal-new-{}".format(position), {
            "op": "create", "portalChangeId": pcid,
            "name": spec["name"], "type": "Folder", "params": {},
        }, "invalid"
    if kind == "invalid_create_camera_config":
        # The follow-up configuration update fails real validation (gain
        # alone misses required exposure/processingPipeline), so the
        # half-created source must be compensated away.
        return "portal-new-{}".format(position), {
            "op": "create", "portalChangeId": pcid,
            "name": spec["name"], "type": "Camera",
            "params": {
                "cameraId": "cam-new-{}".format(position),
                "gain": spec["gain"],
            },
        }, "invalid"
    if kind == "invalid_update_missing":
        return "cfg-missing-{}".format(position), {
            "op": "update", "portalChangeId": pcid,
            "name": "Ghost", "type": "Camera", "params": {},
        }, "invalid"
    if kind == "invalid_delete_missing":
        return "cfg-missing-{}".format(position), {
            "op": "delete", "portalChangeId": pcid, "baseVersion": 1,
        }, "invalid"
    if kind == "invalid_update_bad_type":
        return _cfg_csid(), {
            "op": "update", "portalChangeId": pcid,
            "name": "Bad type", "type": "Tricorder", "params": {},
        }, "invalid"
    if kind == "invalid_update_partial_config":
        target = source_specs[spec["index"]]
        return _cfg_csid(), {
            "op": "update", "portalChangeId": pcid,
            "name": target["name"], "type": target["type"],
            "params": {"gain": spec["gain"]},
        }, "invalid"
    if kind == "discovery_managed_disc":
        return snapshot.cameras[0].stable_id, {
            "op": "update", "portalChangeId": pcid,
            "name": "hijack", "type": "Camera", "params": {},
        }, "discovery"
    assert kind == "discovery_managed_noncfg"
    return "portal-ghost-{}".format(position), {
        "op": "delete", "portalChangeId": pcid,
    }, "discovery"


def _dump_db(session_factory):
    """Device-local state snapshot: every Image_Source row plus its
    attached configuration, keyed by imageSourceId."""
    from dao.sqlite_db import models

    with session_factory() as session:
        rows = {}
        for row in session.query(models.ImageSource).all():
            configuration = row.imageSourceConfiguration
            rows[str(row.imageSourceId)] = {
                "name": row.name,
                "type": getattr(row.type, "value", row.type),
                "cameraId": row.cameraId,
                "location": row.location,
                "device": configuration.device if configuration else None,
                "gain": configuration.gain if configuration else None,
                "exposure": configuration.exposure if configuration else None,
                "pipeline": (
                    configuration.processingPipeline if configuration else None
                ),
            }
        return rows


# --- portal-side reduction mirror (ingest loop over the real reducer) -----------


def _apply_outcome(portal, registry, csid, outcome):
    if outcome.action == portal.ACTION_DISCARD_STALE:
        return
    assert outcome.action != portal.ACTION_CONFLICT, (
        "no scenario in this property should classify a conflict, got one "
        "for {}".format(csid)
    )
    if outcome.entry is None:
        registry.pop(csid, None)
    else:
        entry = dict(outcome.entry)
        entry["camera_source_id"] = csid
        registry[csid] = entry


def _reduce_document(portal, registry, document):
    """Mirror of the ingest handler's reduction loop (`_process_report`):
    camera entries, then failure entries, then reported deletions —
    every reduction through the REAL `reduce_report`."""
    entries = {csid: dict(entry) for csid, entry in registry.items()}
    now_ms = document["reportedAt"]
    cameras = document.get("cameras", {})
    failures = document.get("failures", {})
    for csid, incoming in cameras.items():
        _apply_outcome(
            portal, registry, csid,
            portal.reduce_report(entries.get(csid), incoming, now_ms),
        )
    for csid, failure in failures.items():
        incoming = {"status": "failed", **failure}
        _apply_outcome(
            portal, registry, csid,
            portal.reduce_report(entries.get(csid), incoming, now_ms),
        )
    for csid, entry in entries.items():
        if csid in cameras or csid in failures:
            continue
        pending_content = entry.get("pending_content") or {}
        if (
            entry.get("sync_status") == portal.SYNC_STATUS_PENDING
            and pending_content.get("op") == "create"
        ):
            continue
        _apply_outcome(
            portal, registry, csid,
            portal.reduce_report(entry, None, now_ms),
        )


def _seed_pending(registry, csid, change):
    """The Portal marks the entry pending with the change content before
    delivering it (Requirement 5.1) — the state `reduce_report` resolves
    acks, failures, and delete agreements against."""
    pending_content = {"op": change["op"]}
    for key in ("name", "type", "params"):
        if key in change:
            pending_content[key] = change[key]
    entry = registry.setdefault(csid, {"camera_source_id": csid})
    entry["sync_status"] = "pending"
    entry["portal_change_id"] = change["portalChangeId"]
    entry["pending_content"] = pending_content


# --- assertions ------------------------------------------------------------------


def _assert_valid_round_trip(portal, registry, document, session_factory,
                             csid, change, db_after):
    """Schema-valid change: device state matches the change, the report
    acks the portal_change_id, and the registry entry is synced with the
    applied content (Requirements 5.2, 5.3)."""
    pcid = change["portalChangeId"]

    if change["op"] == "delete":
        image_source_id = csid[len("cfg-"):]
        assert image_source_id not in db_after  # applied on the device (5.2)
        assert csid not in document["cameras"]
        assert csid not in document["failures"]
        assert csid not in registry  # portal agreement: entry removed
        return

    # create / update: the reported entry acknowledges the change (5.3).
    reported = document["cameras"].get(csid)
    assert reported is not None, "applied change for {} was not reported".format(csid)
    assert reported.get("ack") == pcid

    if change["op"] == "create":
        # The report also carries the real cfg- entry (same ack) the
        # placeholder mirrors; the device row is the applied state (5.2).
        real_csids = [
            other for other, entry in document["cameras"].items()
            if other != csid and entry.get("ack") == pcid
        ]
        assert len(real_csids) == 1
        image_source_id = real_csids[0][len("cfg-"):]
        assert document["cameras"][real_csids[0]] == reported
        assert registry[real_csids[0]]["sync_status"] == portal.SYNC_STATUS_SYNCED
    else:
        image_source_id = csid[len("cfg-"):]

    # Device-local state matches the change (5.2).
    row = db_after[image_source_id]
    assert row["name"] == change["name"]
    assert row["type"] == change["type"]
    params = change.get("params") or {}
    if "cameraId" in params:
        assert row["cameraId"] == params["cameraId"]
    if "location" in params:
        assert row["location"] == params["location"]
    if "devicePath" in params:
        assert row["device"] == params["devicePath"]
    if "gain" in params:
        assert row["gain"] == params["gain"]
    if "exposure" in params:
        assert row["exposure"] == params["exposure"]
    if "processingPipeline" in params:
        assert row["pipeline"] == params["processingPipeline"]

    # Registry entry synced with the applied content (5.3).
    entry = registry[csid]
    assert entry["sync_status"] == portal.SYNC_STATUS_SYNCED
    assert entry["name"] == change["name"]
    assert entry["type"] == change["type"]
    for param_key in ("devicePath", "gain", "exposure", "cameraId", "location"):
        if param_key in params:
            assert entry["params"].get(param_key) == params[param_key]


def _assert_failed_round_trip(portal, registry, document, csid, change,
                              expected_reason=None):
    """Failed change: the report carries the failure with a descriptive
    reason, and the registry entry is marked failed with that reason
    verbatim (Requirement 5.4)."""
    failure = document["failures"].get(csid)
    assert failure is not None, "failed change for {} was not reported".format(csid)
    assert failure.get("portalChangeId") == change["portalChangeId"]
    reason = failure.get("reason")
    assert isinstance(reason, str) and reason  # non-empty descriptive reason
    if expected_reason is not None:
        assert reason == expected_reason

    entry = registry.get(csid)
    assert entry is not None
    assert entry["sync_status"] == portal.SYNC_STATUS_FAILED
    assert entry["failure_reason"] == reason  # verbatim (5.4)


# --- the property -----------------------------------------------------------------


@settings(deadline=None)
@given(scenario=_scenario())
def test_portal_change_apply_report_round_trip(scenario):
    """**Feature: camera-registry-sync, Property 7: Portal change
    apply/report round trip**

    **Validates: Requirements 5.2, 5.3, 5.4**
    """
    source_specs, disc_count, change_specs = scenario
    portal = _portal_sync()
    engine, session_factory = _make_device_db()
    snapshot = _make_snapshot(disc_count)

    try:
        with tempfile.TemporaryDirectory() as tmp_dir, _real_accessor() as accessor:
            source_ids = _create_initial_sources(
                accessor, session_factory, source_specs
            )

            clock = _FakeClock()
            shadow = _FakeShadowAccessor()
            agent = EdgeSyncAgent(
                iot_shadow_accessor=shadow,
                image_source_accessor=accessor,
                camera_discovery=_FakeDiscovery(snapshot),
                db_session_factory=session_factory,
                state_store=CameraSyncStateStore(
                    os.path.join(tmp_dir, "camera_sync_state.json")
                ),
                thing_name="test-thing",
                clock=clock,
                wall_clock=clock,
            )

            # Baseline: the device's full report reduced into an empty
            # registry — every entry starts synced.
            agent.report_inventory()
            _flush(agent, clock)
            registry = {}
            _reduce_document(portal, registry, shadow.reported_writes[-1])

            # Materialize the portal changes (distinct targets per batch)
            # and mark each one pending in the registry (5.1).
            changes, expectations = {}, []
            for position, spec in enumerate(change_specs):
                csid, change, expected = _build_change(
                    spec, position, source_ids, source_specs, snapshot
                )
                if csid in changes:
                    continue
                changes[csid] = change
                expectations.append((csid, change, expected))
                _seed_pending(registry, csid, change)

            db_before = _dump_db(session_factory)

            # Deliver the desired changes as a shadow delta and let the
            # agent apply and report (5.2); reduce the resulting report
            # through the real portal reducer.
            agent.on_delta({"state": {"changes": changes}})
            _flush(agent, clock)
            document = shadow.reported_writes[-1]
            _reduce_document(portal, registry, document)

            db_after = _dump_db(session_factory)

            for csid, change, expected in expectations:
                if expected == "valid":
                    _assert_valid_round_trip(
                        portal, registry, document, session_factory,
                        csid, change, db_after,
                    )
                elif expected == "discovery":
                    _assert_failed_round_trip(
                        portal, registry, document, csid, change,
                        expected_reason=REASON_DISCOVERY_MANAGED,
                    )
                    if csid.startswith("cfg-"):
                        assert db_after.get(csid[len("cfg-"):]) == \
                            db_before.get(csid[len("cfg-"):])
                else:
                    _assert_failed_round_trip(
                        portal, registry, document, csid, change
                    )
                    # Device-local state unchanged for the failed change (5.4).
                    if csid.startswith("cfg-") and change["op"] != "create":
                        target_id = csid[len("cfg-"):]
                        assert db_after.get(target_id) == db_before.get(target_id)

            # Failed creates left no row behind: the surviving ids are the
            # initial ones minus applied deletes plus applied creates.
            applied_creates = {
                other[len("cfg-"):]
                for other, entry in document["cameras"].items()
                if other.startswith("cfg-") and other[len("cfg-"):] not in db_before
            }
            applied_deletes = {
                csid[len("cfg-"):]
                for csid, change, expected in expectations
                if expected == "valid" and change["op"] == "delete"
            }
            assert set(db_after) == (
                set(db_before) - applied_deletes
            ) | applied_creates
    finally:
        engine.dispose()
