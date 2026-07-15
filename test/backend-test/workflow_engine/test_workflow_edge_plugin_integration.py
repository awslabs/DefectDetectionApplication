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
"""Edge integration tests for plugin checksum verification (task 14.4,
custom-node-designer Requirements 10.6, 11.4).

End-to-end from a Workflow_Component artifact set on disk (manifest with
``pluginChecksums``/``pluginComponents`` + compiled document + delivered
plugin files, inline and under a Plugin_Component install root) through
the real WorkflowWatcher/discovery registration, the real FastAPI
/workflows API, and the real WorkflowExecutor's run-scoped plugin path:

- checksum-verified plugins load (env prepend + registry scan) and the
  custom element executes within the compiled pipeline (10.6 positive
  path, 11.4);
- plugins installed by a depended-on Plugin_Component under the device
  plugins root join the scan path and load the same way (11.4);
- a checksum mismatch registers the workflow as invalid with the failing
  file identified, reported through the existing status path (the
  registrations API surfaces ``invalidReason``; triggering returns 409
  carrying the reason), and unverified bytes are never scanned (10.6).

GStreamer execution needs the ``gi`` runtime, so these tests script the
Gst boundary exactly like ``test_workflow_engine_integration``: a
scripted pipeline manager records the launch string and the plugin
environment, and ``gst_plugins._scan_registry`` is recorded instead of
importing gi. The real-GStreamer positive path is covered by
``@pytest.mark.edge_device`` (device/CI image only), mirroring the
suite's existing marker.
"""
import functools
import hashlib
import os
import threading
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    VALID_MANIFEST,
    make_session_factory,
    make_watcher,
    write_artifact_set,
)

from workflow_engine import api as workflow_engine_api
from workflow_engine import executor as executor_hook
from workflow_engine import gst_plugins, runtime
from workflow_engine import pipeline_executor as pipeline_executor_module
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    register_workflow_executor,
)

POLL_TIMEOUT_SEC = 10.0
WATCH_POLL_INTERVAL_SEC = 0.2

COMPONENT_NAME = "dda.plugin.plg-blur"
COMPONENT_VERSION = "2.0.0"
COMPONENT_PLUGIN_FILE = "libddablur.so"
COMPONENT_CHECKSUM_KEY = f"{COMPONENT_NAME}/{COMPONENT_PLUGIN_FILE}"

INLINE_PLUGIN_FILE = "libdda_emlcustomedge.so"
INLINE_CHECKSUM_KEY = f"plugins/{DEVICE_ARCH}/{INLINE_PLUGIN_FILE}"

PLUGIN_BYTES = b"\x7fELF-fake-custom-plugin-bytes"
PLUGIN_SHA = hashlib.sha256(PLUGIN_BYTES).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def wait_for(predicate, timeout=POLL_TIMEOUT_SEC, interval=0.02,
             message="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    pytest.fail("Timed out waiting for {0}".format(message))


def wait_for_execution(client, execution_id, timeout=POLL_TIMEOUT_SEC):
    def finished():
        response = client.get("/workflows/executions/{0}".format(execution_id))
        assert response.status_code == 200
        body = response.json()
        if body["status"] in (EXECUTION_STATUS_COMPLETED,
                              EXECUTION_STATUS_FAILED):
            return body
        return None

    return wait_for(
        finished, timeout=timeout,
        message="execution {0} to finish".format(execution_id),
    )


def trigger(client, registration_id):
    response = client.post(
        "/workflows/registrations/{0}/trigger".format(registration_id)
    )
    assert response.status_code == 200, response.text
    return response.json()["executionId"]


def join_execution_thread(execution_id, timeout=POLL_TIMEOUT_SEC):
    """Wait for the run's daemon thread (and with it the plugin-path
    context manager's env restore) to finish completely."""
    name = "workflow-execution-{0}".format(execution_id)
    for thread in threading.enumerate():
        if thread.name == name:
            thread.join(timeout)
            assert not thread.is_alive(), (
                "execution thread {0} did not finish".format(name)
            )


def write_plugin(path, data=PLUGIN_BYTES):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


class ScriptedPipelineManager:
    """Stands in for GstPipelineManager at the Gst boundary only:
    records the launch string and the GST_PLUGIN_PATH visible during
    the run, then returns scripted tag values."""

    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {"is_anomalous": False,
                                         "confidence": 0.1}
        self.calls = []
        self.env_during_run = None

    def run_pipeline(self, pipeline_str, frame_data=None,
                     latency_metrics=None):
        self.calls.append(pipeline_str)
        self.env_during_run = os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV)
        return dict(self.tag_values)


def make_manifest(plugin_checksums=None, plugin_components=None,
                  workflow_id="wf-1"):
    manifest = dict(VALID_MANIFEST, workflowId=workflow_id)
    if plugin_checksums is not None:
        manifest["pluginChecksums"] = dict(plugin_checksums)
    if plugin_components is not None:
        manifest["pluginComponents"] = dict(plugin_components)
    return manifest


def make_compiled(workflow_id, custom_factory="emlcustomblur"):
    """Camera -> custom plugin element -> sink."""
    return {
        "schemaVersion": 1,
        "workflowId": workflow_id,
        "workflowVersion": "1",
        "targetArch": DEVICE_ARCH,
        "segments": [
            {
                "name": "s0",
                "elements": [
                    {"nodeId": "cam", "factory": "videotestsrc",
                     "args": {"num-buffers": 3}},
                    {"nodeId": "custom", "factory": custom_factory,
                     "args": {"strength": 7}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "executorBindings": [],
        "pluginDependencies": [],
    }


# ---------------------------------------------------------------------------
# Fixtures: real database, real watcher, real API app (as in
# test_workflow_engine_integration), plus a device plugins root
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture
def workflow_root(tmp_path):
    return str(tmp_path / "aws_dda_workflows")


@pytest.fixture
def plugins_root(tmp_path):
    """Stands in for /aws_dda/plugins, the Plugin_Component install root."""
    return str(tmp_path / "aws_dda_plugins")


@pytest.fixture
def watcher(workflow_root, plugins_root, session_factory):
    """A real WorkflowWatcher wired as THE process watcher so the API's
    invalid-reason reporting flows through the real runtime lookup;
    checksum verification resolves component files under plugins_root."""
    instance = make_watcher(
        workflow_root,
        session_factory,
        poll_interval=WATCH_POLL_INTERVAL_SEC,
        plugins_root=plugins_root,
    )
    with patch.object(runtime, "_watcher", instance):
        yield instance
    instance.stop()


@pytest.fixture
def client(session_factory):
    app = FastAPI()
    app.include_router(workflow_engine_api.router)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[workflow_engine_api.get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides = {}


@pytest.fixture(autouse=True)
def reset_executor_hook():
    yield
    executor_hook.set_executor(None)


@pytest.fixture
def no_registry_scan():
    """Record registry scans at the gi boundary instead of importing it."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True) as scan:
        yield scan


@pytest.fixture
def executor_plugins_root(plugins_root):
    """Redirect the executor's run-scoped loader to the test device
    plugins root. Only the default install root moves — the REAL
    workflow_plugin_path (verification, env prepend, registry scan,
    restore) still runs."""
    redirected = functools.partial(
        gst_plugins.workflow_plugin_path, plugins_root=plugins_root
    )
    with patch.object(
        pipeline_executor_module, "workflow_plugin_path", redirected
    ):
        yield plugins_root


def deploy(workflow_root, workflow_id, manifest, inline_plugins=()):
    """One artifact set under the watched root, with optional inline
    plugin files under plugins/<arch>/."""
    artifact_path = write_artifact_set(
        workflow_root,
        workflow_id=workflow_id,
        manifest=manifest,
        version="1",
        compiled=make_compiled(workflow_id),
    )
    for name, data in inline_plugins:
        write_plugin(
            os.path.join(artifact_path, "plugins", DEVICE_ARCH, name), data
        )
    return artifact_path


def get_registration(client, registration_id):
    registrations = {
        r["registrationId"]: r
        for r in client.get("/workflows/registrations").json()
    }
    assert registration_id in registrations, registrations
    return registrations[registration_id]


# ---------------------------------------------------------------------------
# 10.6 positive path + 11.4: checksum-verified load and custom element
# execution within the compiled pipeline
# ---------------------------------------------------------------------------


class TestChecksumVerifiedLoadAndExecution:
    def test_inline_verified_plugin_loads_and_custom_element_executes(
        self, workflow_root, watcher, client, session_factory,
        no_registry_scan,
    ):
        manifest = make_manifest(
            plugin_checksums={INLINE_CHECKSUM_KEY: PLUGIN_SHA},
            workflow_id="wf-inline",
        )
        artifact_path = deploy(
            workflow_root, "wf-inline", manifest,
            inline_plugins=[(INLINE_PLUGIN_FILE, PLUGIN_BYTES)],
        )
        watcher.start()

        # Checksum verification passed at registration: runnable, no
        # invalid reason reported.
        registration = get_registration(client, "wf-inline:1")
        assert registration["status"] == "registered"
        assert "invalidReason" not in registration

        manager = ScriptedPipelineManager()
        register_workflow_executor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        )
        env_before = os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV)

        execution_id = trigger(client, "wf-inline:1")
        body = wait_for_execution(client, execution_id)
        join_execution_thread(execution_id)

        # The custom element executed within the compiled pipeline.
        assert body["status"] == EXECUTION_STATUS_COMPLETED
        assert manager.calls == [
            "videotestsrc num-buffers=3 ! emlcustomblur strength=7 ! fakesink"
        ]

        # The verified plugin was loaded for the run: the inline
        # directory led GST_PLUGIN_PATH and was registry-scanned, and
        # nothing leaked afterwards.
        inline_dir = os.path.join(artifact_path, "plugins", DEVICE_ARCH)
        assert manager.env_during_run.startswith(inline_dir)
        no_registry_scan.assert_called_once_with(inline_dir)
        assert os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV) == env_before

    def test_plugin_component_install_root_plugin_loads_and_executes(
        self, workflow_root, watcher, client, session_factory,
        no_registry_scan, executor_plugins_root,
    ):
        # The plugin arrives via a depended-on Plugin_Component that
        # Greengrass installed under the device plugins root (11.4).
        install_dir = os.path.join(
            executor_plugins_root, "plg-blur", "2", DEVICE_ARCH
        )
        write_plugin(os.path.join(install_dir, COMPONENT_PLUGIN_FILE))
        manifest = make_manifest(
            plugin_checksums={COMPONENT_CHECKSUM_KEY: PLUGIN_SHA},
            plugin_components={COMPONENT_NAME: COMPONENT_VERSION},
            workflow_id="wf-component",
        )
        deploy(workflow_root, "wf-component", manifest)
        watcher.start()

        registration = get_registration(client, "wf-component:1")
        assert registration["status"] == "registered"

        manager = ScriptedPipelineManager()
        register_workflow_executor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        )

        execution_id = trigger(client, "wf-component:1")
        body = wait_for_execution(client, execution_id)
        join_execution_thread(execution_id)

        assert body["status"] == EXECUTION_STATUS_COMPLETED
        assert "emlcustomblur" in manager.calls[0]

        # The Plugin_Component install root joined the run's plugin
        # path and was registry-scanned.
        assert install_dir in manager.env_during_run.split(":")
        scanned = [call.args[0] for call in no_registry_scan.call_args_list]
        assert install_dir in scanned


# ---------------------------------------------------------------------------
# 10.6 negative path: mismatch rejection identifying the file, reported
# through the existing status path
# ---------------------------------------------------------------------------


class TestMismatchRejectionThroughStatusPath:
    def test_mismatch_registers_invalid_identifying_the_file_and_blocks_runs(
        self, workflow_root, plugins_root, watcher, client,
    ):
        # The installed component file does not match the manifest's
        # recorded checksum (tampered or corrupted delivery).
        write_plugin(
            os.path.join(
                plugins_root, "plg-blur", "2", DEVICE_ARCH,
                COMPONENT_PLUGIN_FILE,
            ),
            b"tampered bytes",
        )
        manifest = make_manifest(
            plugin_checksums={COMPONENT_CHECKSUM_KEY: PLUGIN_SHA},
            plugin_components={COMPONENT_NAME: COMPONENT_VERSION},
            workflow_id="wf-tampered",
        )
        deploy(workflow_root, "wf-tampered", manifest)
        watcher.start()

        # Registered as invalid with the failing file identified,
        # surfaced through the existing status path (registrations API).
        registration = get_registration(client, "wf-tampered:1")
        assert registration["status"] == "invalid"
        reason = registration["invalidReason"]
        assert "checksum" in reason
        assert COMPONENT_CHECKSUM_KEY in reason

        # Invalid registrations can never be run; the 409 carries the
        # same reported reason.
        response = client.post(
            "/workflows/registrations/wf-tampered:1/trigger"
        )
        assert response.status_code == 409
        assert "cannot be run" in response.json()["detail"]
        assert COMPONENT_CHECKSUM_KEY in response.json()["detail"]

    def test_file_tampered_after_registration_is_never_scanned_at_run_time(
        self, workflow_root, plugins_root, watcher, client, session_factory,
        no_registry_scan, executor_plugins_root,
    ):
        # Registration-time verification passed; the file is tampered
        # afterwards. The run-scoped loader independently re-verifies
        # and skips the directory, so unverified bytes are never
        # registry-scanned (fail closed, 10.6).
        plugin_path = write_plugin(
            os.path.join(
                plugins_root, "plg-blur", "2", DEVICE_ARCH,
                COMPONENT_PLUGIN_FILE,
            )
        )
        manifest = make_manifest(
            plugin_checksums={COMPONENT_CHECKSUM_KEY: PLUGIN_SHA},
            plugin_components={COMPONENT_NAME: COMPONENT_VERSION},
            workflow_id="wf-swapped",
        )
        deploy(workflow_root, "wf-swapped", manifest)
        # One deterministic scan (no watch thread): registered as valid.
        watcher.sync_once()
        assert get_registration(client, "wf-swapped:1")["status"] == (
            "registered"
        )

        write_plugin(plugin_path, b"tampered after registration")

        manager = ScriptedPipelineManager()
        register_workflow_executor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        )
        execution_id = trigger(client, "wf-swapped:1")
        body = wait_for_execution(client, execution_id)
        join_execution_thread(execution_id)

        # The pipeline itself still ran (bundled plugins unaffected),
        # but the mismatched plugin's directory never joined the plugin
        # path and was never scanned.
        assert body["status"] == EXECUTION_STATUS_COMPLETED
        install_dir = os.path.dirname(plugin_path)
        scanned = [call.args[0] for call in no_registry_scan.call_args_list]
        assert install_dir not in scanned
        assert not (manager.env_during_run or "").count(install_dir)


# ---------------------------------------------------------------------------
# Real GStreamer path — device/CI image only (10.6 positive path, 11.4)
# ---------------------------------------------------------------------------


@pytest.mark.edge_device
class TestOnEdgeDeviceRealGStreamer:
    def test_checksum_verified_plugin_loads_through_the_real_registry_scan(
        self, workflow_root, watcher, client, session_factory,
    ):
        """The full positive path with NO mocks: a delivered plugin .so
        (a real GStreamer plugin binary copied inline with its true
        checksum) passes verification, is loaded through the real
        ``Gst.Registry.scan_path``, and the triggered run completes
        through the real GstPipelineManager. Runs only where gi and the
        device container environment are available."""
        gi = pytest.importorskip(
            "gi",
            reason="real GStreamer (gi) is only available in the device "
            "container",
        )
        if "INFERENCE_COMPONENT_DECOMPRESED_PATH" not in os.environ:
            pytest.skip(
                "requires the device container environment "
                "(INFERENCE_COMPONENT_DECOMPRESED_PATH locates the bundled "
                "DDA GStreamer plugins GstPipelineManager loads per run)"
            )
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        source_plugin = Gst.Registry.get().find_plugin("coreelements")
        if source_plugin is None or not source_plugin.get_filename():
            pytest.skip("no on-disk GStreamer plugin available to deliver")
        with open(source_plugin.get_filename(), "rb") as f:
            real_bytes = f.read()
        real_sha = hashlib.sha256(real_bytes).hexdigest()

        plugin_file = os.path.basename(source_plugin.get_filename())
        manifest = make_manifest(
            plugin_checksums={
                f"plugins/{DEVICE_ARCH}/{plugin_file}": real_sha
            },
            workflow_id="wf-real-verified",
        )
        artifact_path = write_artifact_set(
            workflow_root,
            workflow_id="wf-real-verified",
            manifest=manifest,
            version="1",
            compiled={
                "schemaVersion": 1,
                "workflowId": "wf-real-verified",
                "workflowVersion": "1",
                "targetArch": DEVICE_ARCH,
                "segments": [
                    {
                        "name": "s0",
                        "elements": [
                            {"nodeId": "cam", "factory": "videotestsrc",
                             "args": {"num-buffers": 1}},
                            {"nodeId": None, "factory": "fakesink",
                             "args": {}},
                        ],
                    }
                ],
                "executorBindings": [],
                "pluginDependencies": [],
            },
        )
        write_plugin(
            os.path.join(
                artifact_path, "plugins", DEVICE_ARCH, plugin_file
            ),
            real_bytes,
        )
        watcher.start()

        registration = get_registration(client, "wf-real-verified:1")
        assert registration["status"] == "registered"

        register_workflow_executor(session_factory=session_factory)

        execution_id = trigger(client, "wf-real-verified:1")
        body = wait_for_execution(client, execution_id, timeout=30.0)

        assert body["status"] == EXECUTION_STATUS_COMPLETED
        assert body["failingNodeId"] is None
