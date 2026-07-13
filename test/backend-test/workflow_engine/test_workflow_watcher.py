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
"""Tests for WorkflowWatcher registration (Requirements 9.1, 13.3, 13.6)."""
import shutil

import pytest

from workflow_engine_test_utils import (
    VALID_MANIFEST,
    make_session_factory,
    make_watcher,
    write_artifact_set,
)

from workflow_engine.models import WorkflowRegistration


@pytest.fixture
def session_factory():
    return make_session_factory()


def get_rows(session_factory):
    session = session_factory()
    try:
        rows = session.query(WorkflowRegistration).all()
        session.expunge_all()
        return {row.id: row for row in rows}
    finally:
        session.close()


class TestWorkflowWatcherSync:
    def test_registers_valid_artifact_set(self, tmp_path, session_factory):
        path = write_artifact_set(tmp_path, "wf-1", "3")
        watcher = make_watcher(tmp_path, session_factory)

        touched = watcher.sync_once()

        assert touched == ["wf-1:3"]
        rows = get_rows(session_factory)
        row = rows["wf-1:3"]
        assert row.workflow_id == "wf-1"
        assert row.version == "3"
        assert row.arch == "x86_64"
        assert row.artifact_path == path
        assert row.status == "registered"
        assert row.registered_at > 0
        assert watcher.invalid_reason("wf-1:3") is None

    def test_registers_invalid_artifact_set_with_reason(
        self, tmp_path, session_factory
    ):
        # Malformed artifacts are registered as invalid and reported,
        # never runnable (9.1, 13.3)
        write_artifact_set(tmp_path, "wf-bad", "1", omit=("compiled_pipeline.json",))
        watcher = make_watcher(tmp_path, session_factory)

        watcher.sync_once()

        row = get_rows(session_factory)["wf-bad:1"]
        assert row.status == "invalid"
        assert "compiled_pipeline.json" in watcher.invalid_reason("wf-bad:1")

    def test_incompatible_version_registered_invalid(self, tmp_path, session_factory):
        manifest = dict(VALID_MANIFEST, minLocalServerVersion="99.0.0")
        write_artifact_set(tmp_path, "wf-new", "1", manifest=manifest)
        watcher = make_watcher(tmp_path, session_factory)

        watcher.sync_once()

        row = get_rows(session_factory)["wf-new:1"]
        assert row.status == "invalid"
        assert "99.0.0" in watcher.invalid_reason("wf-new:1")

    def test_rescan_is_idempotent(self, tmp_path, session_factory):
        write_artifact_set(tmp_path, "wf-1", "3")
        watcher = make_watcher(tmp_path, session_factory)

        watcher.sync_once()
        touched_again = watcher.sync_once()

        assert touched_again == []
        assert len(get_rows(session_factory)) == 1

    def test_fixed_artifacts_flip_to_registered(self, tmp_path, session_factory):
        write_artifact_set(tmp_path, "wf-1", "3", omit=("manifest.json",))
        watcher = make_watcher(tmp_path, session_factory)
        watcher.sync_once()
        assert get_rows(session_factory)["wf-1:3"].status == "invalid"

        # deliver the missing manifest and rescan
        write_artifact_set(tmp_path, "wf-1", "3")
        touched = watcher.sync_once()

        assert touched == ["wf-1:3"]
        assert get_rows(session_factory)["wf-1:3"].status == "registered"
        assert watcher.invalid_reason("wf-1:3") is None

    def test_removed_artifacts_marked_invalid(self, tmp_path, session_factory):
        write_artifact_set(tmp_path, "wf-1", "3")
        watcher = make_watcher(tmp_path, session_factory)
        watcher.sync_once()

        shutil.rmtree(tmp_path / "wf-1")
        touched = watcher.sync_once()

        assert touched == ["wf-1:3"]
        assert get_rows(session_factory)["wf-1:3"].status == "invalid"
        assert "removed" in watcher.invalid_reason("wf-1:3")

    def test_empty_root_registers_nothing(self, tmp_path, session_factory):
        # Devices without Workflow_Components behave identically (13.6):
        # the watcher finds nothing and writes nothing.
        watcher = make_watcher(tmp_path / "missing", session_factory)
        assert watcher.sync_once() == []
        assert get_rows(session_factory) == {}

    def test_multiple_versions_and_workflows(self, tmp_path, session_factory):
        write_artifact_set(tmp_path, "wf-a", "1")
        write_artifact_set(tmp_path, "wf-a", "2")
        write_artifact_set(tmp_path, "wf-b", "1", omit=("workflow.json",))
        watcher = make_watcher(tmp_path, session_factory)

        watcher.sync_once()

        rows = get_rows(session_factory)
        assert set(rows) == {"wf-a:1", "wf-a:2", "wf-b:1"}
        assert rows["wf-a:1"].status == "registered"
        assert rows["wf-a:2"].status == "registered"
        assert rows["wf-b:1"].status == "invalid"


class TestWorkflowWatcherThread:
    def test_start_runs_startup_scan_and_stop_joins(self, tmp_path, session_factory):
        write_artifact_set(tmp_path, "wf-1", "3")
        watcher = make_watcher(tmp_path, session_factory, poll_interval=0.05)

        watcher.start()
        try:
            assert get_rows(session_factory)["wf-1:3"].status == "registered"
        finally:
            watcher.stop()

        assert not watcher._thread.is_alive()

    def test_watch_loop_picks_up_new_artifacts(self, tmp_path, session_factory):
        import time

        watcher = make_watcher(tmp_path, session_factory, poll_interval=0.05)
        watcher.start()
        try:
            write_artifact_set(tmp_path, "wf-late", "1")
            deadline = time.time() + 5
            while time.time() < deadline:
                rows = get_rows(session_factory)
                if "wf-late:1" in rows:
                    break
                time.sleep(0.05)
            assert get_rows(session_factory)["wf-late:1"].status == "registered"
        finally:
            watcher.stop()
