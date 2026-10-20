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
"""The registration view (workflow-telemetry-dashboards design §5.2
``registry_view.py``; workflow-debug-mode tasks.md 1.4).

Only ``registered`` rows are in the snapshot; a row flipping to
``superseded`` leaves after the watcher's notification; ``has_triton`` reads
the compiled document once per id; the 30 s fallback poll runs without a
watcher; ``get_or_create()`` returns one instance.

Validates: dashboards R2.8, R5.2; debug R2.2, R5.6.
"""
import logging
import time

import pytest

from workflow_engine_test_utils import make_session_factory

from workflow_engine.models import WorkflowRegistration
from workflow_engine.telemetry import registry_view
from workflow_engine.telemetry.registry_view import RegistrationInfo, RegistrationView


class FakeWatcher:
    def __init__(self):
        self.registrations_listeners = []

    def notify(self):
        for listener in list(self.registrations_listeners):
            listener()


class DocumentStore:
    """In-memory compiled documents by artifact path, counting reads."""

    def __init__(self):
        self.documents = {}
        self.reads = []

    def __call__(self, artifact_path):
        self.reads.append(artifact_path)
        return self.documents.get(artifact_path)


def triton_document(*models):
    return {
        "segments": [{"elements": [
            {"nodeId": "m%d" % i, "factory": "emltriton", "args": {"model": model}}
            for i, model in enumerate(models)
        ] + [{"nodeId": "sink", "factory": "fakesink", "args": {}}]}]
    }


def seed(session_factory, registration_id, status="registered", artifact_path=None):
    session = session_factory()
    try:
        workflow_id, version = registration_id.split(":")
        session.add(WorkflowRegistration(
            id=registration_id, workflow_id=workflow_id, version=version, arch="x86_64",
            artifact_path=artifact_path or "/art/" + registration_id, status=status,
            registered_at=int(time.time()),
        ))
        session.commit()
    finally:
        session.close()


def set_status(session_factory, registration_id, status):
    session = session_factory()
    try:
        row = session.get(WorkflowRegistration, registration_id)
        row.status = status
        session.commit()
    finally:
        session.close()


@pytest.fixture
def world():
    session_factory = make_session_factory()
    watcher = FakeWatcher()
    documents = DocumentStore()
    view = RegistrationView(session_factory, lambda: watcher, documents)
    return session_factory, watcher, documents, view


class TestSnapshot:
    def test_only_registered_rows_with_models_and_has_triton(self, world):
        session_factory, watcher, documents, view = world
        documents.documents["/art/wf-a:1"] = triton_document("plates", "plates", "helmets")
        documents.documents["/art/wf-b:2"] = {"segments": [{"elements": [
            {"nodeId": "src", "factory": "videotestsrc", "args": {}}]}]}
        seed(session_factory, "wf-a:1")
        seed(session_factory, "wf-b:2")
        seed(session_factory, "wf-c:3", status="invalid")
        seed(session_factory, "wf-d:4", status="superseded")
        seed(session_factory, "wf-e:5", status="removed")

        view.start()
        snapshot = view.snapshot()
        assert set(snapshot) == {"wf-a:1", "wf-b:2"}
        assert snapshot["wf-a:1"] == RegistrationInfo(
            workflow_id="wf-a", version="1", models=("plates", "helmets"), has_triton=True)
        assert snapshot["wf-b:2"] == RegistrationInfo(
            workflow_id="wf-b", version="2", models=(), has_triton=False)
        assert len(watcher.registrations_listeners) == 1

    def test_start_reads_once_immediately(self, world):
        session_factory, watcher, documents, view = world
        seed(session_factory, "wf-a:1")
        assert view.snapshot() == {}
        view.start()
        assert "wf-a:1" in view.snapshot()
        assert view.reads == 1
        view.start()  # idempotent
        assert view.reads == 1

    def test_snapshot_is_a_copy(self, world):
        session_factory, watcher, documents, view = world
        seed(session_factory, "wf-a:1")
        view.start()
        copy = view.snapshot()
        copy.clear()
        assert view.snapshot() != {}


class TestNotifications:
    def test_superseded_row_leaves_after_the_watcher_notification(self, world):
        session_factory, watcher, documents, view = world
        seed(session_factory, "wf-a:1")
        view.start()
        events = []
        view.add_listener(lambda added, removed: events.append((set(added), set(removed))))

        seed(session_factory, "wf-a:2")
        set_status(session_factory, "wf-a:1", "superseded")
        assert set(view.snapshot()) == {"wf-a:1"}  # nothing until the watcher says so
        watcher.notify()
        assert set(view.snapshot()) == {"wf-a:2"}
        assert events == [({"wf-a:2"}, {"wf-a:1"})]

    def test_no_change_no_notification(self, world):
        session_factory, watcher, documents, view = world
        seed(session_factory, "wf-a:1")
        view.start()
        events = []
        view.add_listener(lambda added, removed: events.append((added, removed)))
        watcher.notify()
        watcher.notify()
        assert events == []
        assert view.reads == 3

    def test_a_raising_listener_is_contained(self, world, caplog):
        session_factory, watcher, documents, view = world
        view.start()
        seen = []

        def boom(added, removed):
            raise RuntimeError("listener boom")

        view.add_listener(boom)
        view.add_listener(lambda added, removed: seen.append(added))
        seed(session_factory, "wf-a:1")
        with caplog.at_level(logging.WARNING, logger="workflow_engine.telemetry.registry_view"):
            watcher.notify()
        assert seen == [frozenset({"wf-a:1"})]
        assert any("listener" in r.getMessage() for r in caplog.records)

    def test_remove_listener(self, world):
        session_factory, watcher, documents, view = world
        view.start()
        seen = []
        callback = lambda added, removed: seen.append(added)  # noqa: E731
        view.add_listener(callback)
        view.remove_listener(callback)
        seed(session_factory, "wf-a:1")
        watcher.notify()
        assert seen == []


class TestDocuments:
    def test_document_is_read_once_per_id_and_cache_evicted_when_it_leaves(self, world):
        session_factory, watcher, documents, view = world
        documents.documents["/art/wf-a:1"] = triton_document("m")
        seed(session_factory, "wf-a:1")
        view.start()
        watcher.notify()
        watcher.notify()
        assert documents.reads == ["/art/wf-a:1"]
        set_status(session_factory, "wf-a:1", "removed")
        watcher.notify()
        assert view.snapshot() == {}
        set_status(session_factory, "wf-a:1", "registered")
        watcher.notify()
        assert documents.reads == ["/art/wf-a:1", "/art/wf-a:1"]  # re-read after it left

    def test_unreadable_document_means_no_triton_with_one_warning(self, world, caplog):
        session_factory, watcher, documents, view = world
        seed(session_factory, "wf-a:1")  # no document in the store
        with caplog.at_level(logging.WARNING, logger="workflow_engine.telemetry.registry_view"):
            view.start()
            watcher.notify()
            watcher.notify()
        info = view.snapshot()["wf-a:1"]
        assert info.models == ()
        assert info.has_triton is False
        warnings = [r for r in caplog.records if "unreadable" in r.getMessage()]
        assert len(warnings) == 1
        assert "wf-a:1" in warnings[0].getMessage()

    def test_a_raising_reader_counts_as_unreadable(self, world):
        session_factory, watcher, documents, view = world
        seed(session_factory, "wf-a:1")

        def reader(path):
            raise OSError("disk")

        view = RegistrationView(session_factory, lambda: watcher, reader)
        view.start()
        assert view.snapshot()["wf-a:1"].has_triton is False


class TestFallback:
    def test_poll_without_a_watcher_then_adopts_it(self, caplog):
        session_factory = make_session_factory()
        seed(session_factory, "wf-a:1")
        holder = {"watcher": None}
        view = RegistrationView(session_factory, lambda: holder["watcher"], lambda p: None,
                                fallback_poll_s=0.05)
        with caplog.at_level(logging.INFO, logger="workflow_engine.telemetry.registry_view"):
            view.start()
        try:
            assert "wf-a:1" in view.snapshot()
            assert any("no workflow watcher" in r.getMessage() for r in caplog.records)
            assert view._poll_thread is not None and view._poll_thread.is_alive()
            seed(session_factory, "wf-b:2")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and "wf-b:2" not in view.snapshot():
                time.sleep(0.01)
            assert "wf-b:2" in view.snapshot()  # picked up by the poll
            watcher = FakeWatcher()
            holder["watcher"] = watcher
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not watcher.registrations_listeners:
                time.sleep(0.01)
            assert len(watcher.registrations_listeners) == 1  # adopted
            view._poll_thread.join(5.0)
            assert not view._poll_thread.is_alive()
            seed(session_factory, "wf-c:3")
            watcher.notify()
            assert "wf-c:3" in view.snapshot()
        finally:
            view.stop()

    def test_read_errors_keep_the_last_snapshot(self, world, caplog):
        session_factory, watcher, documents, view = world
        seed(session_factory, "wf-a:1")
        view.start()
        calls = {"n": 0}
        real_factory = session_factory

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db down")
            return real_factory()

        view._session_factory = flaky
        with caplog.at_level(logging.WARNING, logger="workflow_engine.telemetry.registry_view"):
            watcher.notify()
        assert set(view.snapshot()) == {"wf-a:1"}
        assert any("could not read" in r.getMessage() for r in caplog.records)
        watcher.notify()
        assert set(view.snapshot()) == {"wf-a:1"}


class TestSingleton:
    def test_get_or_create_returns_one_started_instance(self, monkeypatch):
        session_factory = make_session_factory()
        seed(session_factory, "wf-a:1")
        watcher = FakeWatcher()
        monkeypatch.setattr(registry_view, "_default_session_factory", session_factory)
        monkeypatch.setattr(registry_view, "_default_watcher_provider", lambda: watcher)
        monkeypatch.setattr(registry_view, "_default_document_reader", lambda p: None)
        registry_view._reset_for_tests()
        try:
            first = registry_view.get_or_create()
            second = registry_view.get_or_create()
            assert first is second
            assert "wf-a:1" in first.snapshot()
            assert len(watcher.registrations_listeners) == 1
        finally:
            registry_view._reset_for_tests()
        assert watcher.registrations_listeners == []


def test_module_surface():
    assert registry_view.__all__ == ["RegistrationInfo", "RegistrationView", "get_or_create"]
