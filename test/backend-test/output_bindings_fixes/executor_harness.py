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
"""WorkflowExecutor test harness shared by the exploration cases 4-6.

Mirrors the ``test_workflow_pipeline_executor.py`` /
``test_workflow_capture_routing.py`` fixture style: a fake pipeline manager,
a temp-file-backed sqlite database, and compiled-document builders for the
tritonless llm/capture workflows this spec's defects live in (the live JP6
workflow ``dda.workflow.1f0b4c0c-...`` shape: folder_source ->
llm_inference -> capture + mqtt_publish, where folder_source compiles to a
plain ``filesrc`` and llm_inference to an executor binding — no ``emltriton``
element anywhere).
"""
import json
import time

from workflow_engine_test_utils import DEVICE_ARCH

from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import EXECUTION_STATUS_PENDING

WORKFLOW_ID = "wf-1"
EXECUTION_ID = "exec-1"
REGISTRATION_ID = "wf-1:3"

#: The per-run id ``pipeline_executor.execute`` computes:
#: ``{workflow_id}-{execution_id}``.
CAPTURE_ID = "{0}-{1}".format(WORKFLOW_ID, EXECUTION_ID)


def make_doc(segments, bindings):
    return {
        "schemaVersion": 1,
        "workflowId": WORKFLOW_ID,
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": segments,
        "executorBindings": bindings,
        "pluginDependencies": [],
    }


#: A tritonless pipeline-only segment (no emltriton -> nothing ever attaches
#: a buffer correlation id).
PLAIN_SEGMENTS = [
    {
        "name": "s0",
        "elements": [
            {"nodeId": "n1", "factory": "videotestsrc",
             "args": {"num-buffers": 1}},
            {"nodeId": None, "factory": "fakesink", "args": {}},
        ],
    }
]

#: A tritonless terminal-capture segment (the live workflow's capture_1).
CAPTURE_SEGMENTS = [
    {
        "name": "s0",
        "elements": [
            {"nodeId": "n1", "factory": "videotestsrc",
             "args": {"num-buffers": 1}},
            {"nodeId": "capture_1", "factory": "emlcapture", "args": {}},
        ],
    }
]

LLM_BINDING = {
    "nodeId": "llm_inference_1",
    "binding": "llm_inference",
    "parameters": {
        "modelName": "opt125m-smoke",
        "prompt_template": "Describe the inspection result",
    },
}


class FakePipelineManager:
    """Mocked GstPipelineManager: records the launch string, returns tags.

    ``on_run`` (optional) is called once per run before returning, so a test
    can simulate pipeline side effects — e.g. the message broker writing a
    ``.jpg`` capture file with an empty basename."""

    def __init__(self, tag_values=None, on_run=None):
        self.tag_values = tag_values or {}
        self.on_run = on_run
        self.calls = []

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        self.calls.append(pipeline_str)
        if self.on_run is not None:
            self.on_run()
        return dict(self.tag_values)


def seed_run(session_factory, artifact_path):
    """A registration + pending execution pair ready for execute()."""
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id=REGISTRATION_ID,
            workflow_id=WORKFLOW_ID,
            version="3",
            arch=DEVICE_ARCH,
            artifact_path=str(artifact_path),
            status="registered",
            registered_at=int(time.time()),
        ))
        session.add(WorkflowExecution(
            id=EXECUTION_ID,
            registration_id=REGISTRATION_ID,
            started_at=int(time.time()),
            status=EXECUTION_STATUS_PENDING,
        ))
        session.commit()
    finally:
        session.close()
    return EXECUTION_ID


def get_execution(session_factory, execution_id=EXECUTION_ID):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, execution_id)
    finally:
        session.close()


def node_status_map(row):
    assert row.node_status_json, (
        "no node_status_json was persisted for execution {0}".format(row.id))
    return json.loads(row.node_status_json)
