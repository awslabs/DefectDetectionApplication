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
"""No-migration smoke test (camera-registry-sync task 2.9, Requirement 11.4).

An Edge_Sync_Agent started against a populated fixture SQLite database —
the REAL ORM schema with every configuration table populated and every
column set — produces its full inventory report and leaves every
pre-existing row byte-identical. The comparison is two-fold:

- a per-table dict dump of every row of every table (clear diagnostics on
  divergence), taken straight over the DBAPI connection so no ORM-level
  coercion can mask a change; and
- the complete SQL text dump of the database (``sqlite3
  Connection.iterdump()`` — schema plus every row rendered as SQL
  literals), giving a genuinely byte-level before/after equality.

Harness follows test_property_portal_change_round_trip.py: a real
in-memory SQLite database on a ``StaticPool`` (one shared connection), the
real ``ImageSourceAccessor`` (hardware side effects patched out, schema
handling real), a recording fake shadow accessor, and a fake discovery
snapshot whose cameras both overlap a configured Image_Source's device
path (exercising the merge) and stand alone (discovered-only entry), plus
a discovery failure so ``discoveryErrors`` reporting is exercised too. The
agent is started with its real ``start()`` worker thread — the same way
server_setup.py runs it — and stopped after its startup report lands.
"""
import contextlib
import os
import pathlib
import tempfile
import time
from unittest import mock

from camera_discovery import DiscoveredCamera, DiscoveryResult
from camera_sync import CameraSyncStateStore, EdgeSyncAgent

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_DEFAULT_CAMERA_CONFIG_PATH = (
    _REPO_ROOT / "src" / "backend" / "utils" / "config"
    / "default_camera_configurations.json"
)

# --- lazy real-stack loading (mirrors the sibling round-trip harness) ----------

_real_modules = None


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
    """A fresh REAL in-memory SQLite device database with the real schema
    (``StaticPool`` shares the single in-memory connection)."""
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
    """The REAL ImageSourceAccessor with only its hardware side effects
    patched out; the agent's inventory path is read-only, so the camera
    manager and default-config patches exist purely as guard rails."""
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
        yield accessor_module.ImageSourceAccessor()


# --- fixture population ---------------------------------------------------------


def _populate_fixture(session_factory):
    """Pre-existing rows in every configuration table, every column set —
    the state a pre-feature LocalServer deployment leaves behind."""
    from dao.sqlite_db import models
    from model.image_source import ImageSourceType
    from utils.constants import ANOMALY, GPIO_FALLING, GPIO_RISING

    with session_factory() as session:
        session.add(models.ImageSourceConfiguration(
            imageSourceConfigId="cfgid-1",
            gain=10,
            exposure=1500,
            processingPipeline="videoconvert",
            creationTime=1700000000,
            imageCrop={"top": 1, "bottom": 2, "left": 3, "right": 4},
            device="/dev/video0",
            deviceName="Fixture Cam",
            advancedSettings={"reverseX": True, "balanceWhiteAuto": "Continuous"},
        ))
        session.add(models.ImageSource(
            imageSourceId="src-cam-1",
            name="Line camera",
            type=ImageSourceType.CAMERA,
            location=None,
            cameraId="cam-1",
            description="pre-existing camera source",
            creationTime=1700000001,
            lastUpdateTime=1700000002,
            imageCapturePath="/aws_dda/capture/src-cam-1",
            imageSourceConfigId="cfgid-1",
        ))
        session.add(models.ImageSource(
            imageSourceId="src-folder-1",
            name="Golden images",
            type=ImageSourceType.FOLDER,
            location="/tmp/dda-golden",
            cameraId=None,
            description="pre-existing folder source",
            creationTime=1700000003,
            lastUpdateTime=1700000004,
            imageCapturePath="/aws_dda/capture/src-folder-1",
            imageSourceConfigId=None,
        ))
        session.add(models.InputConfiguration(
            inputConfigurationId="input-1",
            creationTime=1700000005,
            pin="7",
            triggerState=GPIO_RISING,
            debounceTime=25,
        ))
        session.add(models.OutputConfiguration(
            outputConfigurationId="output-1",
            pin="11",
            signalType=GPIO_FALLING,
            pulseWidth=50,
            creationTime=1700000006,
            rule=ANOMALY,
        ))
        session.add(models.Workflow(
            workflowId="wf-1",
            name="Classic pipeline",
            description="pre-existing classic workflow",
            creationTime=1700000007,
            lastUpdatedTime=1700000008,
            workflowOutputPath="/aws_dda/out/wf-1",
            featureConfigurations={"anomalyThreshold": 0.5},
            inputConfigurations=["input-1"],
            outputConfigurations=["output-1"],
            imageSourceId="src-cam-1",
        ))
        session.commit()


# --- dumps -----------------------------------------------------------------------


def _dbapi_connection(engine):
    raw = engine.raw_connection()
    dbapi = getattr(raw, "driver_connection", None) or raw.connection
    return raw, dbapi


def _dump_rows(engine):
    """Every row of every table, all columns, straight over the DBAPI
    connection (no ORM coercion): {table: [{column: raw value}]}."""
    raw, dbapi = _dbapi_connection(engine)
    try:
        cursor = dbapi.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        dump = {}
        for name in tables:
            cursor.execute('SELECT * FROM "{}"'.format(name))
            columns = [c[0] for c in cursor.description]
            dump[name] = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return dump
    finally:
        raw.close()


def _dump_sql(engine):
    """The complete SQL text dump (schema + every row as SQL literals) —
    the byte-identical comparison of Requirement 11.4."""
    raw, dbapi = _dbapi_connection(engine)
    try:
        return "\n".join(dbapi.iterdump())
    finally:
        raw.close()


# --- fakes (same patterns as the sibling camera_sync suites) ---------------------


class _FakeShadowAccessor:
    def __init__(self):
        self.reported_writes = []

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return None

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        if "reported" in state:
            self.reported_writes.append(state["reported"])


class _FakeDiscovery:
    def __init__(self, snapshot):
        self.latest_snapshot = snapshot


def _make_snapshot():
    return DiscoveryResult(
        cameras=[
            # Same device path as src-cam-1's configuration: merges into
            # the configured entry (origin edge-configured, discovered).
            DiscoveredCamera(
                stable_id="disc-aaaaaaaaaaaa",
                device_path="/dev/video0",
                card_name="Fixture Cam",
                bus_info="usb-0000:00:14.0-1",
                driver="uvcvideo",
                kind="v4l2",
                formats=[{"pixel_format": "YUYV", "resolutions": [[1920, 1080]]}],
            ),
            # Discovered-only hardware: separate edge-discovered entry.
            DiscoveredCamera(
                stable_id="disc-bbbbbbbbbbbb",
                device_path="/dev/video1",
                card_name="USB Camera",
                bus_info="usb-0000:00:14.0-2",
                driver="uvcvideo",
                kind="v4l2",
                formats=[{"pixel_format": "MJPG", "resolutions": [[1280, 720]]}],
            ),
        ],
        failures=[{"device_path": "/dev/video7", "error": "VIDIOC_QUERYCAP failed"}],
    )


# --- the smoke test ---------------------------------------------------------------


def test_agent_startup_report_leaves_every_existing_row_byte_identical():
    """Requirement 11.4: the agent runs without migration — a populated
    fixture database is byte-identical after the agent starts, reads the
    inventory through the existing accessor, and reports it."""
    engine, session_factory = _make_device_db()
    try:
        _populate_fixture(session_factory)
        rows_before = _dump_rows(engine)
        sql_before = _dump_sql(engine)

        shadow = _FakeShadowAccessor()
        with tempfile.TemporaryDirectory() as tmp_dir, _real_accessor() as accessor:
            agent = EdgeSyncAgent(
                iot_shadow_accessor=shadow,
                image_source_accessor=accessor,
                camera_discovery=_FakeDiscovery(_make_snapshot()),
                db_session_factory=session_factory,
                state_store=CameraSyncStateStore(
                    os.path.join(tmp_dir, "camera_sync_state.json")
                ),
                thing_name="test-thing",
            )
            agent.start()
            try:
                deadline = time.time() + 10.0
                while not shadow.reported_writes and time.time() < deadline:
                    time.sleep(0.02)
            finally:
                agent.stop()

        # Sanity: the agent really ran against the fixture database — its
        # startup report carries the configured sources (merged and plain),
        # the discovered-only camera, and the discovery failure.
        assert shadow.reported_writes, "the agent never produced a report"
        document = shadow.reported_writes[-1]
        cameras = document["cameras"]
        assert "cfg-src-cam-1" in cameras
        assert cameras["cfg-src-cam-1"]["discovered"] is True
        assert cameras["cfg-src-cam-1"]["origin"] == "edge-configured"
        assert "cfg-src-folder-1" in cameras
        assert "disc-bbbbbbbbbbbb" in cameras
        assert cameras["disc-bbbbbbbbbbbb"]["origin"] == "edge-discovered"
        assert "disc-aaaaaaaaaaaa" not in cameras  # merged, never duplicated
        assert document["discoveryErrors"] == [
            {"devicePath": "/dev/video7", "error": "VIDIOC_QUERYCAP failed"}
        ]

        # Requirement 11.4: every pre-existing row is untouched. Row-level
        # comparison first (readable diagnostics), then the byte-level SQL
        # dump equality (schema + every row as SQL literals).
        assert _dump_rows(engine) == rows_before
        assert _dump_sql(engine) == sql_before
    finally:
        engine.dispose()
