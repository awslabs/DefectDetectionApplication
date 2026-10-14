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
"""Bug-condition exploration test: bridged pipeline camera frame feed stall
(bridged-pipeline-camera-frame-feed-stall, Property 1: Bug Condition).

Property 1: Bug Condition — Bridged runner receives the camera frame.

**This test asserts the FIXED (post-fix) executor behavior, so it is
EXPECTED TO FAIL on the UNFIXED tree.** The failure is the counterexample
confirming the bug: the bridged branch of ``WorkflowExecutor.execute``
(``workflow_engine/pipeline_executor.py``, bridged branch around line
1600) forwards ``frame_data=python_frame_data`` — the UNMERGED
Custom-Python-source variable, which is ``None`` for camera-fed
workflows — instead of the merged ``frame_data`` holding the Aravis
camera grab. ``_run_bridged`` omits the ``frame_data`` keyword entirely
when the value is ``None``, so ``run_bridged_pipeline`` runs feed-free:
``fed_source`` stays ``None``, no buffer and no EOS are ever pushed into
the pipeline's appsrc, and the pipeline stalls until the 120 s watchdog.

Expected counterexample on the UNFIXED tree: the recording bridged
runner double is invoked with NO ``frame_data`` keyword at all — the
grabbed camera frame is dropped on the floor.

Production counterexamples (jetson-thor1, JP7 LocalServer 1.0.16,
workflow ``bdfabc2a-d246-466f-a4ca-53bb40c9e119`` v5 — Basler camera →
``custom_python_preprocess_1`` → tee/funnel → ``n4`` →
``bedrock_inference_1``): executions
``ed3b60aa-fa34-4eca-9b10-93a5284f5384``,
``1aed6b7f-7879-4b53-897b-8fd598098eff``, and
``f7e430b7-e13a-4ba5-af7c-c35fde6451f2`` all stalled for exactly 120 s
and failed with ``Pipeline timed out after 120s without completing (no
EOS/ERROR received)``. With the one-line fix hot-patched on-device,
execution ``26f833f8-f687-4a59-89d2-1a8e2f79dcbc`` completed in 3.4 s
with a Bedrock verdict.

The SAME test is re-run in task 3.2 against the fixed executor
(``frame_data=frame_data`` in the bridged branch), where it must PASS.

**Validates: Requirements 1.1, 1.2, 1.3** (expected behavior 2.1, 2.2,
2.3)
"""
import os
import time
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

REGISTRATION_ID = "wf-1:3"
CAMERA_NODE_ID = "n1"
PYTHON_NODE_ID = "pynode"

#: A pass-through Custom_Python_Node handler — the bridged runner double
#: never actually runs it; it only needs to exist inside the artifact set
#: so ``build_bridges`` resolves the handler path.
ECHO_HANDLER = """\
def handle(frame, metadata):
    return frame, dict(metadata)
"""


def make_camera_bridged_document():
    """A compiled document combining the two families of the bug
    condition: the Aravis camera source shape from
    ``test_workflow_aravis_executor.py`` (compiled appsrc chain +
    ``aravisBinding: true`` binding point) and an ``emlpython``
    Custom_Python_Node element as in ``test_workflow_python_bridge.py``.
    No Custom Python SOURCE node — ``python_frame_data`` stays ``None``,
    which is exactly the family the bug drops the camera frame for."""
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [
            {
                "name": "s0",
                "elements": [
                    {"nodeId": CAMERA_NODE_ID, "factory": "appsrc",
                     "args": {"name": "appsrc_{0}".format(CAMERA_NODE_ID)}},
                    {"nodeId": CAMERA_NODE_ID, "factory": "videoconvert",
                     "args": {}},
                    {"nodeId": PYTHON_NODE_ID, "factory": "emlpython",
                     "args": {"handler-path":
                              "python/{0}/handler.py".format(PYTHON_NODE_ID)}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "bindingPoints": [
            {
                "nodeId": CAMERA_NODE_ID,
                "nodeType": "aravis_camera_source",
                "parameters": {"camera_id": "Aravis-Fake-GV01",
                               "gain": 4, "exposure": 5000000},
                "slots": [],
                "aravisBinding": True,
            }
        ],
        "executorBindings": [],
        "pluginDependencies": ["dda-emlpython"],
    }


def make_frame(width=4, height=2, bytes_per_pixel=1):
    return {
        "data": b"\x00" * (width * height * bytes_per_pixel),
        "width": width,
        "height": height,
    }


class FakeCameraManager:
    """Callable frame grabber recording (camera_id, config) calls."""

    def __init__(self, frame=None):
        self.frame = frame if frame is not None else make_frame()
        self.calls = []

    def __call__(self, camera_id, config):
        self.calls.append((camera_id, dict(config)))
        return self.frame


class RecordingBridgedRunner:
    """Records every bridged-runner invocation exactly as it was made —
    ``(launch_string, bridges, kwargs)`` — so the presence/absence of the
    ``frame_data`` keyword is directly observable (``_run_bridged`` omits
    the kwarg entirely when the value is ``None``)."""

    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {}
        self.calls = []

    def __call__(self, launch_string, bridges, **kwargs):
        self.calls.append((launch_string, list(bridges), dict(kwargs)))
        return dict(self.tag_values)


class ExplodingPipelineManager:
    """The plain manager must never run for bridged documents — any
    fallthrough fails loudly instead of silently taking the wrong path."""

    def run_pipeline(self, *args, **kwargs):
        raise AssertionError(
            "GstPipelineManager.run_pipeline must not be used for "
            "documents with Custom_Python_Nodes"
        )


@pytest.fixture(autouse=True)
def no_registry_scan():
    """Never import gi in these tests."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


def seed_camera_bridged_run(session_factory, tmp_path):
    """Artifact set with the camera+bridge compiled document plus the
    emlpython handler file at python/<node>/handler.py."""
    artifact_path = write_artifact_set(
        tmp_path, compiled=make_camera_bridged_document()
    )
    handler_dir = os.path.join(artifact_path, "python", PYTHON_NODE_ID)
    os.makedirs(handler_dir, exist_ok=True)
    with open(os.path.join(handler_dir, "handler.py"), "w") as f:
        f.write(ECHO_HANDLER)
    session = session_factory()
    try:
        session.add(
            WorkflowRegistration(
                id=REGISTRATION_ID,
                workflow_id="wf-1",
                version="3",
                arch=DEVICE_ARCH,
                artifact_path=str(artifact_path),
                status="registered",
                registered_at=int(time.time()),
            )
        )
        session.add(
            WorkflowExecution(
                id="exec-1",
                registration_id=REGISTRATION_ID,
                started_at=int(time.time()),
                status=EXECUTION_STATUS_PENDING,
            )
        )
        session.commit()
    finally:
        session.close()
    return artifact_path


def get_execution(session_factory):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, "exec-1")
    finally:
        session.close()


class TestCameraBridgedFrameFeed:
    def test_bridged_runner_receives_the_grabbed_camera_frame(self, tmp_path):
        """Bug condition (bugfix.md 1.1/1.2/1.3): an Aravis frame feed is
        planned (camera grab succeeds, no Custom Python source) AND the
        document has emlpython bridges. The bridged runner MUST receive
        the grabbed camera frame as ``frame_data`` — otherwise no buffer
        and no EOS are ever pushed into the fed appsrc and the pipeline
        stalls to the 120 s watchdog (executions ed3b60aa / 1aed6b7f /
        f7e430b7 on jetson-thor1)."""
        session_factory = make_session_factory()
        seed_camera_bridged_run(session_factory, tmp_path)
        frame = make_frame(width=8, height=4)
        grabber = FakeCameraManager(frame=frame)
        runner = RecordingBridgedRunner(tag_values={"is_anomalous": False})

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=ExplodingPipelineManager,
            bridged_pipeline_runner=runner,
            frame_grabber=grabber,
        ).execute("exec-1")

        # The camera grab happened with the rendered default parameters.
        assert grabber.calls == [
            ("Aravis-Fake-GV01", {"gain": 4, "exposure": 5000000})
        ]

        # The bridged runner ran once, on the rewritten launch string
        # (Frame_Feed appsrc + the emlpython appsink/appsrc pair).
        assert len(runner.calls) == 1
        launch_string, bridges, kwargs = runner.calls[0]
        assert "appsrc name=appsrc " in launch_string
        assert "appsink name=py_in_{0}".format(PYTHON_NODE_ID) in launch_string
        assert [bridge.node_id for bridge in bridges] == [PYTHON_NODE_ID]

        # THE BUG: on the unfixed tree the bridged branch forwards the
        # unmerged python_frame_data (None for camera workflows), and
        # _run_bridged omits the frame_data keyword entirely — the
        # grabbed camera frame never reaches the pipeline.
        assert "frame_data" in kwargs, (
            "bridged runner was invoked WITHOUT the frame_data keyword — "
            "the grabbed camera frame was dropped (fed_source stays None; "
            "no buffer/EOS ever pushed; 120s watchdog stall). "
            "kwargs actually received: {0!r}".format(sorted(kwargs))
        )
        assert kwargs["frame_data"] is frame

        # And the run completed.
        assert get_execution(session_factory).status == (
            EXECUTION_STATUS_COMPLETED
        )
