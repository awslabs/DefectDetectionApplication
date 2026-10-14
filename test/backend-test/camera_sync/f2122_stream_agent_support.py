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
"""Shared fakes for the task 29 device tests (rtsp-rtmp-stream-cameras 29.2
and 29.3). Not a test module: the ``test_f2122_*`` and
``test_property_f2122_*`` files of this directory import it, as others import
``pin_worker_support``.

- :func:`make_stream_world` is the real-accessor world of
  ``test_stream_camera_sync_agent.py``: the real ``ImageSourceAccessor`` over
  a private sqlite database and a real Credential_Store, behind a recording
  proxy. Every agent it builds gets a recording ``change_retry_timer`` and a
  recording stream timer, so no real 2-30 s timer outlives a test.
- :class:`ScriptedFetcher` is the agent's credential fetcher: the real
  ``credential_fetch.fetch`` over a fake Secrets Manager client that plays
  scripted outcomes, so every error goes through the real reduction to its
  code. The scripted AWS errors carry :data:`PASSWORD` in their message, which
  that reduction must drop.
- :class:`MergingShadow` applies AWS IoT update semantics (nested maps merge,
  a null deletes a key) and can hold a reported write mid-flight.
- :data:`PASSWORD` is the sentinel :func:`assert_secret_free` looks for in
  reports, desired writes, failure reasons and log records.
"""
import copy
import json
import logging
import os
import sys
import threading
import traceback
from typing import Any, Mapping

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")
_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC_IMAGE_TESTS = os.path.join(_HERE, "..", "static_image_camera")
if _STATIC_IMAGE_TESTS not in sys.path:
    sys.path.insert(0, _STATIC_IMAGE_TESTS)

from camera_manager_support import import_camera_manager  # noqa: E402

import_camera_manager()

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import dao.sqlite_db.models as db_models  # noqa: E402
from camera_sync import CameraSyncStateStore, EdgeSyncAgent  # noqa: E402
from dao.sqlite_db.sqlite_db_operations import Base  # noqa: E402
from stream_ingest import credential_fetch  # noqa: E402
from stream_ingest import credentials as credentials_module  # noqa: E402
from utils import constants, dda_user_management_utils  # noqa: E402

_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_DEFAULT_CAMERA_CONFIG = os.path.join(_REPO_ROOT, "src", "backend", "utils", "config",
                                      "default_camera_configurations.json")

#: The secret the fetcher hands out and the AWS errors quote: it may reach
#: the Credential_Store and nothing else.
PASSWORD = "pw-F2122-SENTINEL-7c3d"
URL = "rtsp://10.0.4.21:554/Streaming/Channels/101"
SECRET_ARN = ("arn:aws:secretsmanager:us-east-1:123456789012:secret:"
              "dda-portal/stream-camera-credentials/thing/portal-x-AbCdEf")


def reference(n: int = 1, secret_arn: str = SECRET_ARN) -> dict:
    """A Credential_Reference whose version is unique to ``n``."""
    return {"secretArn": secret_arn, "versionId": "0b4f7e7c-1d7a-4f45-9d3a-{:012d}".format(n)}


REF = reference(1)
REF2 = reference(2)


# --- timers and the fetcher ------------------------------------------------------


class RecordingTimer:
    """A timer seam that records ``(delay, action)`` and runs nothing until
    the test fires it."""

    def __init__(self):
        self.pending = []
        self.delays = []

    def __call__(self, delay, action):
        self.pending.append((delay, action))
        self.delays.append(delay)

    def fire(self, index: int = 0) -> float:
        delay, action = self.pending.pop(index)
        action()
        return delay

    def fire_next(self) -> float:
        return self.fire(0)


class ImmediateTimer:
    """A timer seam that runs its action at once, on the calling thread: a
    caller holding a lock the action takes would deadlock."""

    def __init__(self):
        self.delays = []

    def __call__(self, delay, action):
        self.delays.append(delay)
        action()


class AwsError(Exception):
    """A botocore-style ClientError: the code in ``response``, and a message
    that quotes :data:`PASSWORD`, which the code reduction must drop."""

    def __init__(self, code: str):
        message = "User is not authorized to read {}".format(PASSWORD)
        super().__init__("An error occurred ({}) when calling the GetSecretValue "
                         "operation: {}".format(code, message))
        self.response = {"Error": {"Code": code, "Message": message}}


def denied() -> AwsError:
    return AwsError("AccessDeniedException")


def not_found() -> AwsError:
    return AwsError("ResourceNotFoundException")


def granted(password: str = PASSWORD) -> dict:
    return {"username": "viewer", "password": password}


#: A SecretString that is not JSON: ``fetch`` fails with MalformedSecret.
MALFORMED = "not json"


class ScriptedFetcher:
    """The real ``credential_fetch.fetch`` over a fake Secrets Manager client
    that plays ``outcomes`` in order: an exception is raised by the client
    (``fetch`` reduces it to its code), a dict is the secret's JSON and a
    str its raw SecretString. A ``CredentialFetchError`` outcome is raised
    as it is, before ``fetch`` runs (an invalid reference on a retry).
    ``calls`` are the references fetched; an invalid one fails inside
    ``fetch``, before any client call."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.client_calls = 0

    def __call__(self, reference_):
        self.calls.append(copy.deepcopy(dict(reference_)) if isinstance(reference_, Mapping)
                          else reference_)
        if self.outcomes and isinstance(self.outcomes[0], credential_fetch.CredentialFetchError):
            raise self.outcomes.pop(0)
        return credential_fetch.fetch(reference_, client_factory=lambda region: self)

    def get_secret_value(self, SecretId, VersionId):  # noqa: N803 - boto3's keyword names
        self.client_calls += 1
        if not self.outcomes:
            raise AwsError("NoScriptedOutcomeLeft")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return {"SecretString": outcome if isinstance(outcome, str) else json.dumps(outcome)}


# --- the shadow --------------------------------------------------------------------


def merge(target: dict, update: Mapping) -> None:
    """AWS IoT shadow update semantics: nested maps merge, a null deletes."""
    for key, value in update.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class MergingShadow:
    """The camera-registry shadow with AWS update semantics. ``reported`` and
    ``desired`` keep the raw writes; ``state`` is the merged document.
    ``failing`` fails every write, ``readable = False`` fails the GET, and
    :meth:`hold_next_reported_write` parks the next reported write until
    the test releases it."""

    def __init__(self, state=None):
        self.state = copy.deepcopy(state) if state else {}
        self.failing = False
        self.readable = True
        self.reported = []
        self.desired = []
        self._held = None
        self._lock = threading.Lock()

    def hold_next_reported_write(self):
        """``(entered, release)``: ``entered`` is set once the next reported
        write has started, which then waits for ``release``."""
        entered, release = threading.Event(), threading.Event()
        self._held = (entered, release)
        return entered, release

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        if not self.readable:
            raise ConnectionError("shadow offline")
        with self._lock:
            return copy.deepcopy(self.state) if self.state else None

    def update_thing_shadow_state_request(self, thing_name, shadow_name, update):
        if "reported" in update and self._held is not None:
            entered, release = self._held
            self._held = None
            entered.set()
            if not release.wait(10):
                raise TimeoutError("the held shadow write was never released")
        if self.failing:
            raise ConnectionError("shadow offline")
        with self._lock:
            merge(self.state, update)
            if "reported" in update:
                self.reported.append(copy.deepcopy(update["reported"]))
            if "desired" in update:
                self.desired.append(copy.deepcopy(update["desired"]))

    @property
    def failures(self) -> dict:
        return (self.state.get("reported") or {}).get("failures") or {}

    @property
    def cameras(self) -> dict:
        return (self.state.get("reported") or {}).get("cameras") or {}


# --- the real-accessor world -------------------------------------------------------


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Discovery:
    latest_snapshot = None


class FakeStreamManager:
    def __init__(self):
        self.health_listeners = []
        self.capability_listeners = []
        self.notified = []
        self.deleted = []

    def add_health_listener(self, listener):
        self.health_listeners.append(listener)

    def on_capabilities(self, listener):
        self.capability_listeners.append(listener)

    def health_for_image_source(self, image_source_id):
        return None

    def capabilities(self, wait_s=0):
        return None

    def notify_config_changed(self, image_source_id):
        self.notified.append(image_source_id)

    def notify_deleted(self, image_source_id):
        self.deleted.append(image_source_id)


def _reference_version(managed):
    ref = (managed or {}).get("credentialRef")
    return ref.get("versionId") if isinstance(ref, Mapping) else None


class RecordingAccessor:
    """The real ImageSourceAccessor, recording every write: the op, the id
    or type, and the version of the Credential_Reference it applied. Never
    a credential."""

    def __init__(self, accessor):
        self._accessor = accessor
        self.calls = []

    def __getattr__(self, name):
        return getattr(self._accessor, name)

    def create_image_source(self, data, db, managed_stream_settings=None):
        self.calls.append(("create", data.get("type"), _reference_version(managed_stream_settings)))
        return self._accessor.create_image_source(data, db, managed_stream_settings=managed_stream_settings)

    def update_image_source(self, image_source_id, data, db, managed_stream_settings=None):
        self.calls.append(("update", image_source_id, _reference_version(managed_stream_settings)))
        return self._accessor.update_image_source(image_source_id, data, db,
                                                  managed_stream_settings=managed_stream_settings)

    def delete_image_source(self, image_source_id, db):
        self.calls.append(("delete", image_source_id))
        return self._accessor.delete_image_source(image_source_id, db)


class StreamWorld:
    """See :func:`make_stream_world`."""

    def __init__(self, tmp_path, monkeypatch, shadow=None):
        from resources.accessors.image_source_accessor import ImageSourceAccessor
        from stream_ingest import manager as manager_module

        tmp_path = str(tmp_path)
        self._manager_module = manager_module
        monkeypatch.setattr(constants, "DEFAULT_CAMERA_CONFIG_FILE_PATH", _DEFAULT_CAMERA_CONFIG)
        monkeypatch.setattr(constants, "IMAGE_CAPTURE_DIR", os.path.join(tmp_path, "capture"))
        monkeypatch.setattr(dda_user_management_utils, "create_dda_user_directory",
                            lambda folder_path: (os.makedirs(folder_path, exist_ok=True), folder_path)[1])
        monkeypatch.setenv("COMPONENT_WORK_PATH", tmp_path)
        self.tmp_path = tmp_path
        self.engine = create_engine("sqlite:///{}".format(os.path.join(tmp_path, "agent.db")),
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.store = credentials_module.CredentialStore(
            directory=os.path.join(tmp_path, "stream_credentials"))
        credentials_module.set_credential_store(self.store)
        self.stream = FakeStreamManager()
        manager_module.set_stream_ingest_manager(self.stream)
        self.clock = Clock()
        self.timer = RecordingTimer()
        self.retry_timer = RecordingTimer()
        self.fetcher = ScriptedFetcher()
        self.shadow = shadow if shadow is not None else MergingShadow()
        self.accessor = RecordingAccessor(ImageSourceAccessor())

    def make_agent(self, refresh=False, **overrides) -> EdgeSyncAgent:
        """An agent over this world; ``refresh`` runs the start-time shadow
        GET (``_refresh_reported_versions``), as ``start()`` does first."""
        arguments = dict(
            iot_shadow_accessor=self.shadow, image_source_accessor=self.accessor,
            camera_discovery=Discovery(), db_session_factory=self.factory,
            state_store=CameraSyncStateStore(os.path.join(self.tmp_path, "state.json")),
            thing_name="thing", clock=self.clock, wall_clock=lambda: 1_790_000_000.0,
            debounce_seconds=0.0, stream_ingest=self.stream, stream_timer=self.timer,
            credential_fetcher=self.fetcher, credential_store=self.store,
            change_retry_timer=self.retry_timer)
        arguments.update(overrides)
        agent = EdgeSyncAgent(**arguments)
        if refresh:
            agent._refresh_reported_versions()
        return agent

    def image_sources(self):
        with self.factory() as db:
            return {str(source.imageSourceId): (getattr(source.type, "value", source.type), source.name)
                    for source in db.query(db_models.ImageSource).all()}

    def stream_settings(self, image_source_id):
        with self.factory() as db:
            source = db.get(db_models.ImageSource, image_source_id)
            return dict(source.imageSourceConfiguration.streamSettings or {})

    def close(self):
        credentials_module.set_credential_store(None)
        self._manager_module.set_stream_ingest_manager(None)
        self.engine.dispose()


def make_stream_world(tmp_path, monkeypatch, shadow=None) -> StreamWorld:
    """The real-accessor world (``test_stream_camera_sync_agent.py``'s
    ``world`` fixture as a function). The caller calls ``close()``."""
    return StreamWorld(tmp_path, monkeypatch, shadow=shadow)


def apply(agent, csid, change):
    """Deliver one change as a delta does, then run one report step."""
    agent.apply_desired_changes({csid: change})
    agent.pump()


def report(agent, shadow):
    agent.report_inventory()
    agent.pump()
    return shadow.reported[-1]


# --- change builders -----------------------------------------------------------------


def rtsp_change(op, pcid, ref=REF, name="Dock 3", **params):
    """A Portal change to an RTSP camera; ``ref=None`` mentions no
    credentials. ``params`` given as None are left out."""
    base = {"url": URL, "transport": "udp", "latencyMs": 400}
    if ref is not None:
        base.update(credentialRef=dict(ref), credentialsConfigured=True,
                    credentialsUpdatedAt=1_790_000_000_123)
    base.update(params)
    return {"op": op, "portalChangeId": pcid, "name": name, "type": "RTSP",
            "params": {key: value for key, value in base.items() if value is not None}}


def rtsp_create(pcid, ref=REF, **params):
    return rtsp_change("create", pcid, ref=ref, **params)


def rtsp_update(pcid, ref=REF2, **params):
    return rtsp_change("update", pcid, ref=ref, **params)


def delete_change(pcid):
    return {"op": "delete", "portalChangeId": pcid}


# --- secret hygiene ----------------------------------------------------------------------


def log_texts(caplog):
    """Every captured log record as text, its traceback included."""
    texts = []
    for record in caplog.records:
        texts.append(record.getMessage())
        if record.exc_info:
            texts.append("".join(traceback.format_exception(*record.exc_info)))
        if record.exc_text:
            texts.append(record.exc_text)
    return texts


def capture_logs(caplog):
    """INFO and up from every logger (SQLAlchemy's statement log stays off).
    ``set_level`` also sets caplog's handler level, so the INFO call is last."""
    caplog.set_level(logging.WARNING, logger="sqlalchemy")
    caplog.set_level(logging.INFO)


def assert_secret_free(shadow, caplog=None, secret=PASSWORD):
    """No report, desired write, failure reason or log record holds
    ``secret``."""
    blobs = [json.dumps(shadow.reported), json.dumps(shadow.desired)]
    for document in shadow.reported:
        for failure in (document.get("failures") or {}).values():
            if isinstance(failure, Mapping):
                blobs.append(str(failure.get("reason")))
    if caplog is not None:
        blobs.extend(log_texts(caplog))
    leaks = [blob.replace(secret, "<SECRET>")[:200] for blob in blobs if secret in blob]
    assert not leaks, "the secret leaked into: {}".format(leaks)
