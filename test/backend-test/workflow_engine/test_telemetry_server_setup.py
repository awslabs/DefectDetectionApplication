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
"""The LocalServer start of the Telemetry_Agent (workflow-telemetry-dashboards
design §5.2 ``agent.py``, R5.8; tasks.md 1.5): the
``camera_sync/test_server_setup_isolation.py`` scenarios for the
``workflow-telemetry-startup`` wiring, beside the debug plan's
``test_workflow_debug_server_setup.py``.

``utils.server_setup`` starts the agent on a daemon thread behind the same
exception guards as the camera-registry sync: a raising constructor or
``start()`` is logged and swallowed, the module global stays ``None`` and
startup proceeds untouched; a clean start publishes the agent through
``agent.set_agent``.

As the debug plan's test, this file runs the import and the scenarios in one
subprocess (``utils.server_setup`` has heavy module-level imports and cannot
be imported twice in one session under scoped ``sys.modules`` stubs) and
asserts on the JSON it prints.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_ENV = {
    "COMPONENT_WORK_PATH": "/tmp",
    "AWS_IOT_THING_NAME": "iot_thing_test",
    "LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": "comp_decomp_path_test",
    "KERNEL_ROOT_PATH": ".",
    "PYTHONDONTWRITEBYTECODE": "1",
}

SCENARIO_SCRIPT = r'''
import importlib, json, logging, os, sys, threading, types
from unittest import mock

import awsiot_host_stubs  # the guarded SDK stub

def install(name, module):
    if name not in sys.modules:
        sys.modules[name] = module

install("grpc", mock.MagicMock(name="grpc"))
panorama = types.ModuleType("panorama")
for sub in ("messagebroker", "application", "media"):
    m = mock.MagicMock(name="panorama." + sub)
    setattr(panorama, sub, m)
    install("panorama." + sub, m)
install("panorama", panorama)
deprecated = types.ModuleType("deprecated")
def _deprecated(*args, **kwargs):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    def wrap(func):
        return func
    return wrap
deprecated.deprecated = _deprecated
install("deprecated", deprecated)

import mock_gi  # noqa: F401
import mock_edge_agent_pb2  # noqa: F401
import mock_edge_agent_pb2_grpc  # noqa: F401

patchers = [
    mock.patch("edge_agent_pb2_grpc.EdgeAgentStub", return_value=None, create=True),
    mock.patch("awsiot.greengrasscoreipc.connect"),
    mock.patch("utils.constants.DEFAULT_CAMERA_CONFIG_FILE_PATH",
               "./src/backend/utils/config/default_camera_configurations.json"),
]
for p in patchers:
    p.start()

from workflow_engine.telemetry import agent as telemetry_agent
from workflow_engine.telemetry.debug import agent as debug_agent

result = {}
class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []
    def emit(self, record):
        self.lines.append((record.levelname, record.getMessage()))
capture = Capture()
logging.getLogger("utils.server_setup").addHandler(capture)
logging.getLogger("utils.server_setup").setLevel(logging.DEBUG)

# Import with both agents' constructors raising: the import-time startup
# threads end through the guards under test, and the import still completes.
with mock.patch.object(telemetry_agent, "TelemetryAgent",
                       mock.Mock(side_effect=RuntimeError("import-time boom"))), \
     mock.patch.object(debug_agent, "DebugAgent", mock.Mock(side_effect=RuntimeError("debug boom"))):
    importlib.import_module("utils.digital_input_process_manager")
    server_setup = importlib.import_module("utils.server_setup")
    for t in threading.enumerate():
        if t.name in ("workflow-telemetry-startup", "workflow-debug-startup", "camera-sync-startup"):
            t.join(15.0)
result["import_ok"] = True
result["startup_thread_alive"] = any(t.name == "workflow-telemetry-startup" for t in threading.enumerate())
result["import_time_global_none"] = server_setup.workflow_telemetry_agent is None
result["import_time_logged"] = any("Workflow telemetry agent failed to start" in m for _, m in capture.lines)
source = open(server_setup.__file__).read()
result["startup_thread_spawned_daemon"] = ('name="workflow-telemetry-startup"' in source)
result["beside_the_debug_start"] = (source.index("_start_workflow_debug_isolated")
                                    < source.index("_start_workflow_telemetry_isolated")
                                    < source.index("_start_user_accounts_sync_isolated"))

class FakeAgent:
    def __init__(self, *a, **k):
        self.started = False
    def start(self):
        self.started = True
        return self
class RaisingStart(FakeAgent):
    def start(self):
        raise RuntimeError("agent start boom")

def scenario(name, agent_cls):
    capture.lines.clear()
    server_setup.workflow_telemetry_agent = None
    telemetry_agent.set_agent(None)
    with mock.patch.object(telemetry_agent, "TelemetryAgent", agent_cls):
        try:
            server_setup._start_workflow_telemetry_isolated()
            raised = False
        except Exception:
            raised = True
    result[name] = {
        "raised": raised,
        "global_is_none": server_setup.workflow_telemetry_agent is None,
        "global_started": bool(getattr(server_setup.workflow_telemetry_agent, "started", False)),
        "published": telemetry_agent.get_agent() is server_setup.workflow_telemetry_agent
                     and server_setup.workflow_telemetry_agent is not None,
        "published_none": telemetry_agent.get_agent() is None,
        "error_logged": any(level == "ERROR" and "Workflow telemetry agent failed to start" in m
                            for level, m in capture.lines),
        "info_logged": any(level == "INFO" and "Workflow telemetry agent started" in m
                           for level, m in capture.lines),
    }

scenario("raising_constructor", mock.Mock(side_effect=RuntimeError("constructor boom")))
scenario("raising_start", RaisingStart)
scenario("clean_start", FakeAgent)
print("RESULT " + json.dumps(result))
'''


@pytest.fixture(scope="module")
def scenarios():
    env = dict(os.environ)
    # The root conftest points PYTHONHOME at the interpreter path for Triton's
    # python backend; a child interpreter cannot start with it.
    env.pop("PYTHONHOME", None)
    env.update(TEST_ENV)
    env["PYTHONPATH"] = os.pathsep.join([
        str(REPO_ROOT / "src" / "backend"),
        str(REPO_ROOT / "test" / "backend-test"),
        str(REPO_ROOT / "test" / "backend-test" / "workflow_engine"),
    ])
    completed = subprocess.run(
        [sys.executable, "-c", SCENARIO_SCRIPT], cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=240, universal_newlines=True,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT ")]
    assert completed.returncode == 0 and lines, (
        "the server_setup scenario subprocess failed (exit %s)\nstdout:\n%s\nstderr:\n%s"
        % (completed.returncode, completed.stdout[-4000:], completed.stderr[-6000:])
    )
    return json.loads(lines[-1][len("RESULT "):])


def test_import_completes_although_the_agent_constructor_raises(scenarios):
    assert scenarios["import_ok"] is True
    assert scenarios["import_time_global_none"] is True
    assert scenarios["import_time_logged"] is True
    assert scenarios["startup_thread_alive"] is False  # it ended through the guard
    assert scenarios["startup_thread_spawned_daemon"] is True
    assert scenarios["beside_the_debug_start"] is True


def test_raising_agent_constructor_is_isolated(scenarios):
    outcome = scenarios["raising_constructor"]
    assert outcome["raised"] is False
    assert outcome["global_is_none"] is True
    assert outcome["published_none"] is True
    assert outcome["error_logged"] is True


def test_raising_agent_start_is_isolated(scenarios):
    outcome = scenarios["raising_start"]
    assert outcome["raised"] is False
    assert outcome["global_is_none"] is True
    assert outcome["published_none"] is True
    assert outcome["error_logged"] is True


def test_successful_start_sets_the_global_and_publishes_the_agent(scenarios):
    outcome = scenarios["clean_start"]
    assert outcome["raised"] is False
    assert outcome["global_is_none"] is False
    assert outcome["global_started"] is True
    assert outcome["published"] is True
    assert outcome["info_logged"] is True
    assert outcome["error_logged"] is False
