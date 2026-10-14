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
"""Device-side execution of the CSI and ICAM input nodes.

**Feature: csi-icam-input-nodes**

- ``plan_capture_sources`` is pure over the compiled document (Component 5).
- Property 10: a CSI run writes the effective gain/exposure to the CSI
  config file before the pipeline starts, stages the JP6 PNG at the
  compiled read path, and fails with the CSI node id on a missing frame
  (Requirements 7.1, 7.2, 7.4).
- Property 11: an ICAM run takes the unstaged path and a neither-node run
  is unchanged (Requirements 7.3, 7.5).
"""
import itertools
import json
import os
import shutil
import tempfile
import time
from unittest.mock import patch

from workflow_engine_test_utils import make_session_factory, write_artifact_set

from workflow_engine import csi_capture, gst_plugins
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    CapturePlan,
    CsiCapture,
    WorkflowExecutor,
    plan_capture_sources,
)


# ---------------------------------------------------------------------------
# plan_capture_sources (pure)
# ---------------------------------------------------------------------------

def _doc(binding_points):
    return {
        "schemaVersion": 1,
        "segments": [{"name": "s0", "elements": [
            {"nodeId": "n1", "factory": "videotestsrc",
             "args": {"num-buffers": 1}},
            {"nodeId": "n1", "factory": "fakesink", "args": {}},
        ]}],
        "bindingPoints": binding_points,
    }


class TestPlanCaptureSources:
    def test_neither_node_plans_nothing(self):
        assert plan_capture_sources({"segments": []}, "x86_64").is_empty
        # A pre-feature document with no bindingPoints section.
        assert plan_capture_sources(
            {"schemaVersion": 1, "segments": []}, "arm64_jp6").is_empty

    def test_csi_node_carries_rendered_gain_exposure(self):
        plan = plan_capture_sources(_doc([{
            "nodeId": "csi1", "nodeType": "csi_camera_source",
            "parameters": {"gain": 10, "exposure": 16000000},
            "slots": [], "csiSensorBinding": True,
        }]), "arm64_jp5")
        assert plan.icam_nodes == []
        assert plan.csi_nodes == [
            CsiCapture(node_id="csi1", gain=10, exposure=16000000)]

    def test_csi_defaults_when_parameters_absent(self):
        plan = plan_capture_sources(_doc([{
            "nodeId": "csi1", "nodeType": "csi_camera_source",
            "parameters": {}, "slots": [], "csiSensorBinding": True,
        }]), "x86_64")
        assert plan.csi_nodes == [CsiCapture(
            node_id="csi1",
            gain=csi_capture.DEFAULT_GAIN,
            exposure=csi_capture.DEFAULT_EXPOSURE)]

    def test_csi_resolved_override_takes_precedence(self):
        class _Resolution:
            csi_assignments = {"csi1": {"params": {"gain": 42,
                                                   "exposure": 7}}}

        plan = plan_capture_sources(_doc([{
            "nodeId": "csi1", "nodeType": "csi_camera_source",
            "parameters": {"gain": 4, "exposure": 5000000},
            "slots": [], "csiSensorBinding": True,
        }]), "x86_64", _Resolution())
        assert plan.csi_nodes == [
            CsiCapture(node_id="csi1", gain=42, exposure=7)]

    def test_icam_node_collected(self):
        plan = plan_capture_sources(_doc([{
            "nodeId": "cam1", "nodeType": "icam_source",
            "parameters": {"device": "/dev/video0"},
            "slots": [{"param": "device", "segment": 0,
                       "element": 0, "arg": "device"}],
        }]), "x86_64")
        assert plan.csi_nodes == []
        assert plan.icam_nodes == ["cam1"]


class TestWriteCsiConfig:
    def test_writes_gain_exposure_json(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "config.json")
            assert csi_capture.write_csi_config(
                gain=10, exposure=16000000, config_file=path) is True
            with open(path) as handle:
                assert json.load(handle) == {"gain": 10, "exposure": 16000000}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_write_failure_is_tolerant(self):
        # A path whose parent cannot be created returns False, never raises.
        assert csi_capture.write_csi_config(
            config_file="/proc/nonexistent-dir/config.json") is False


# ---------------------------------------------------------------------------
# Executor integration
# ---------------------------------------------------------------------------

class FakePipelineManager:
    def __init__(self):
        self.calls = []

    def run_pipeline(self, pipeline_str, *args, **kwargs):
        self.calls.append((pipeline_str, args, kwargs))
        return {}


_SESSION_FACTORY = None
_IDS = itertools.count(1)


def _session_factory():
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = make_session_factory()
    return _SESSION_FACTORY


def _seed_run(session_factory, artifact_path, arch, sequence):
    registration_id = "wf-1:3:{0}".format(sequence)
    execution_id = "exec-{0}".format(sequence)
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id=registration_id, workflow_id="wf-1", version="3", arch=arch,
            artifact_path=str(artifact_path), status="registered",
            registered_at=int(time.time())))
        session.add(WorkflowExecution(
            id=execution_id, registration_id=registration_id,
            started_at=int(time.time()), status=EXECUTION_STATUS_PENDING))
        session.commit()
    finally:
        session.close()
    return registration_id, execution_id


def _get_execution(session_factory, execution_id):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, execution_id)
    finally:
        session.close()


def _launchable_csi_document(arch, gain=10, exposure=16000000):
    """A run-safe document (videotestsrc chain) carrying a CSI binding
    point — the executor's CSI handling is keyed off bindingPoints, not
    the element chain."""
    return {
        "schemaVersion": 1, "workflowId": "wf-1", "workflowVersion": "3",
        "targetArch": arch,
        "segments": [{"name": "s0", "elements": [
            {"nodeId": "csi1", "factory": "videotestsrc",
             "args": {"num-buffers": 1}},
            {"nodeId": "csi1", "factory": "fakesink", "args": {}},
        ]}],
        "executorBindings": [], "pluginDependencies": [],
        "bindingPoints": [{
            "nodeId": "csi1", "nodeType": "csi_camera_source",
            "parameters": {"gain": gain, "exposure": exposure},
            "slots": [], "csiSensorBinding": True,
        }],
    }


def _run(document, arch, csi_frame_present, stage_recorder=None):
    """Drive one execution; returns (execution_row, manager, write_calls,
    stage_calls). ``csi_frame_present`` controls whether the CSI capture
    frame exists on disk."""
    session_factory = _session_factory()
    sequence = next(_IDS)
    root = tempfile.mkdtemp(prefix="csi-icam-exec-")
    capture_dir = tempfile.mkdtemp(prefix="csi-capture-")
    latest_jpg = os.path.join(capture_dir, "latest.jpg")
    if csi_frame_present:
        with open(latest_jpg, "wb") as handle:
            handle.write(b"\xff\xd8\xff\xe0jpegbytes")
    write_calls = []
    stage_calls = []

    def fake_write(gain=csi_capture.DEFAULT_GAIN,
                   exposure=csi_capture.DEFAULT_EXPOSURE, crop=None,
                   config_file=None):
        write_calls.append({"gain": gain, "exposure": exposure})
        return True

    def fake_stage(path):
        stage_calls.append(path)
        png = "{0}.dda_decoded.png".format(path)
        with open(png, "wb") as handle:
            handle.write(b"png")
        return png

    manager = FakePipelineManager()
    try:
        artifact_path = write_artifact_set(root, compiled=document)
        _, execution_id = _seed_run(session_factory, artifact_path, arch,
                                    sequence)
        executor = WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager)
        with patch.object(gst_plugins, "_scan_registry", return_value=True), \
                patch.object(csi_capture, "CSI_LATEST_JPG", latest_jpg), \
                patch.object(csi_capture, "write_csi_config", fake_write), \
                patch(
                    "workflow_engine.pipeline_executor._stage_decoded_png",
                    fake_stage):
            executor.execute(execution_id)
        return (_get_execution(session_factory, execution_id), manager,
                write_calls, stage_calls)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(capture_dir, ignore_errors=True)


class TestCsiExecution:
    def test_writes_config_and_runs_on_non_jp6(self):
        # Requirement 7.1: config written with effective gain/exposure
        # before the pipeline runs; no JP6 staging on x86_64.
        row, manager, writes, stages = _run(
            _launchable_csi_document("x86_64", gain=10, exposure=16000000),
            "x86_64", csi_frame_present=True)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert writes == [{"gain": 10, "exposure": 16000000}]
        assert stages == []
        assert len(manager.calls) == 1

    def test_jp6_stages_the_png(self):
        # Requirement 7.2: on arm64_jp6 the capture frame is staged as a
        # decoded PNG at the compiled read path before the pipeline runs.
        row, manager, writes, stages = _run(
            _launchable_csi_document("arm64_jp6"),
            "arm64_jp6", csi_frame_present=True)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert writes == [{"gain": 10, "exposure": 16000000}]
        assert len(stages) == 1
        assert stages[0].endswith("latest.jpg")
        assert len(manager.calls) == 1

    def test_missing_frame_fails_with_csi_node_id(self):
        # Requirement 7.4: a missing capture frame fails the run attributed
        # to the CSI node, and the pipeline never starts.
        row, manager, writes, stages = _run(
            _launchable_csi_document("x86_64"),
            "x86_64", csi_frame_present=False)
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == "csi1"
        assert "csi" in (row.error or "").lower() or "capture" in (
            row.error or "").lower()
        assert manager.calls == []


class TestIcamAndNeitherExecution:
    def _icam_document(self):
        return {
            "schemaVersion": 1, "workflowId": "wf-1", "workflowVersion": "3",
            "targetArch": "x86_64",
            "segments": [{"name": "s0", "elements": [
                {"nodeId": "cam1", "factory": "videotestsrc",
                 "args": {"num-buffers": 1}},
                {"nodeId": "cam1", "factory": "fakesink", "args": {}},
            ]}],
            "executorBindings": [], "pluginDependencies": [],
            "bindingPoints": [{
                "nodeId": "cam1", "nodeType": "icam_source",
                "parameters": {"device": "/dev/video0"},
                "slots": [{"param": "device", "segment": 0,
                           "element": 0, "arg": "device"}],
            }],
        }

    def test_icam_runs_unstaged(self):
        # Requirement 7.3: ICAM takes the unstaged path — no CSI config
        # write, no PNG staging.
        row, manager, writes, stages = _run(
            self._icam_document(), "x86_64", csi_frame_present=False)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert writes == []
        assert stages == []
        assert len(manager.calls) == 1

    def test_neither_node_run_is_unaffected(self):
        # Requirement 7.5: a document with no typed camera input plans no
        # CSI/ICAM work and runs exactly as before.
        document = {
            "schemaVersion": 1, "workflowId": "wf-1", "workflowVersion": "3",
            "targetArch": "x86_64",
            "segments": [{"name": "s0", "elements": [
                {"nodeId": "n1", "factory": "videotestsrc",
                 "args": {"num-buffers": 1}},
                {"nodeId": "n1", "factory": "fakesink", "args": {}},
            ]}],
            "executorBindings": [], "pluginDependencies": [],
        }
        row, manager, writes, stages = _run(
            document, "arm64_jp6", csi_frame_present=False)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert writes == []
        assert stages == []
        assert len(manager.calls) == 1
