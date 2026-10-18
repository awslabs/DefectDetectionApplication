# Copyright 2026 Amazon Web Services, Inc.
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
"""Where every workflow run path registers with ``utils.run_activity``
(triton-model-cleanup-wedge task 4.3, design D7).

``endpoints/workflow.py``, ``gstreamer/gst_pipeline.py`` and
``utils/digital_input_thread_manager.py`` cannot be imported on the host
(GStreamer and the device stack; ``test_cold_model_classic_path.py``
explains why), so their registrations are checked statically, in that
suite's style. The capture task is checked by behavior with its two
device-only imports replaced by stubs."""
import ast
import asyncio
import importlib
import pathlib
import sys
import types

import pytest

from utils import run_activity as ra

SRC = pathlib.Path(__file__).resolve().parents[3] / "src" / "backend"
START_RUN_KEYWORDS = {"kind", "run_id", "models", "workflow", "continuous", "on_interrupt", "bind"}
REFUSAL = ("The LocalServer backend is restarting to clear a wedged Triton model load (self-heal "
           "attempt 1); run the workflow again in a few minutes.")


def function_source(relative, name):
    text = (SRC / relative).read_text()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(text, node), node
    raise AssertionError("{0} not found in {1}".format(name, relative))


def calls_in(node, attr):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == attr]


def finally_calls(node, attr):
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Try):
            for statement in child.finalbody:
                found.extend(calls_in(statement, attr))
    return found


# ----------------------------------------------------------- the classic run

def test_the_classic_handler_registers_before_its_gate_and_finishes_in_a_finally():
    body, node = function_source("endpoints/workflow.py", "run_inference_for_stream")
    requirements = body.find("validate_workflow_requirements(")
    start = body.find("run_activity.start_run(")
    gate = body.find("validate_model_readiness(")
    after = body.find("run_activity.after_gate(")
    pipeline = body.find("configure_image_source_and_run_pipeline(")
    assert -1 not in (requirements, start, gate, after, pipeline)
    assert requirements < start < gate < after < pipeline
    assert finally_calls(node, "finish_run"), "finish_run sits in a finally"
    refusal = body[start:gate]
    assert "ticket.refused" in refusal and "status_code=503" in refusal
    assert "status_code=503, detail=refused" in body[after:pipeline]


def test_run_pipeline_registers_before_its_try_and_finishes_in_its_finally():
    body, node = function_source("gstreamer/gst_pipeline.py", "run_pipeline")
    start = body.find("run_activity.start_run(")
    first_try = body.find("        try:\n            Gst.init(None)")
    assert -1 not in (start, first_try) and start < first_try
    assert "models_from_launch(pipeline_str)" in body[start:first_try]
    assert "PipelineExecutionException(ticket.refused, status_code=503)" in body[start:first_try]
    assert finally_calls(node, "finish_run")


def test_the_digital_input_trigger_registers_before_its_frame_grab():
    body, node = function_source("utils/digital_input_thread_manager.py", "trigger_workflow_and_debounce")
    start = body.find("run_activity.start_run(")
    source = body.find("self._get_image_source(")
    grab = body.find("get_camera_frame(")
    full = body.find("Pipeline is full")
    assert -1 not in (start, source, grab, full) and start < source < grab < full
    assert "bind=False" in body[start:source]
    assert finally_calls(node, "finish_run"), "a raising read, grab or submit finishes the ticket"
    body, node = function_source("utils/digital_input_thread_manager.py", "execute_workflow")
    assert "run_activity.bound(ticket)" in body
    assert finally_calls(node, "finish_run")


def test_the_continuous_runner_checks_the_quiesce_after_its_model_gate():
    body, _node = function_source("workflow_engine/continuous_runner.py", "step")
    gate = body.find("self._model_gate(now)")
    quiesce = body.find("run_activity.quiescing()")
    frame = body.find("latest_frame(")
    assert -1 not in (gate, quiesce, frame) and gate < quiesce < frame
    assert "return QUIESCE_POLL_S" in body[quiesce:frame]


def test_no_start_run_call_can_hold_a_run():
    offenders = []
    calls = 0
    for path in SRC.rglob("*.py"):
        if "vendor" in path.parts:
            continue
        text = path.read_text(errors="replace")
        if "start_run(" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "start_run":
                continue
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                    and func.value.id in ("self", "_instance"):
                continue  # the module's own delegation
            calls += 1
            keywords = {k.arg for k in node.keywords}
            if not keywords <= START_RUN_KEYWORDS or len(node.args) > 2 or any(
                    "hold" in (k or "") or "wait" in (k or "") for k in keywords):
                offenders.append("{0}:{1}".format(path.relative_to(SRC), node.lineno))
    assert calls >= 5, "the registration points were not found"
    assert offenders == []


# ----------------------------------------------------------- the capture task

@pytest.fixture
def capture_manager(monkeypatch):
    calls = {"pipelines": [], "stored": []}

    captured = types.ModuleType("utils.captured_images_utils")

    def run_image_capture_pipeline(image_source, workflow_output_path, gst_pipeline_executor,
                                   file_prefix):
        nested = ra.start_run("pipeline", "image-source-pipeline")  # run_pipeline's own call
        calls["pipelines"].append((nested.noop, [t.kind for t in ra.snapshot().tickets]))
        ra.finish_run(nested)
        if calls.get("raise_at") == len(calls["pipelines"]):
            raise RuntimeError("camera unplugged")
        return "/captures/{0}.png".format(file_prefix)

    captured.run_image_capture_pipeline = run_image_capture_pipeline
    captured.convert_captured_data_to_db = lambda captured_data, workflow_id: captured_data

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    sqlite_ops = types.ModuleType("dao.sqlite_db.sqlite_db_operations")
    sqlite_ops.SessionLocal = _Session
    monkeypatch.setitem(sys.modules, "utils.captured_images_utils", captured)
    monkeypatch.setitem(sys.modules, "dao.sqlite_db.sqlite_db_operations", sqlite_ops)
    monkeypatch.delitem(sys.modules, "utils.capture_task_manager", raising=False)
    module = importlib.import_module("utils.capture_task_manager")
    accessor = types.SimpleNamespace(store_captured_data=lambda session, data: calls["stored"].append(data))
    manager = module.CaptureTaskManager.__new__(module.CaptureTaskManager)
    manager.inference_result_accessor = accessor
    manager.gst_pipeline_executor = object()
    monkeypatch.delitem(sys.modules, "utils.capture_task_manager", raising=False)
    return module, manager, calls


@pytest.fixture
def activity():
    instance = ra.RunActivity()
    previous = ra.install(instance)
    token = ra._CURRENT.set(None)
    yield instance
    ra._CURRENT.reset(token)
    ra.install(previous)


def run(coroutine):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


def task(module, count=3):
    return module.CaptureTaskParameters(capture_task_id="cap-1", interval=0.01, count=count,
                                        workflow_id="wf-1", workflow_output_path="/out",
                                        image_source_dict={"type": "Camera"})


def test_a_capture_task_holds_one_ticket_across_its_sleeps(capture_manager, activity):
    module, manager, calls = capture_manager
    seen_between = []
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        seen_between.append([(t.kind, t.run_id) for t in activity.snapshot().tickets])
        await real_sleep(0)

    module.asyncio.sleep = sleep
    try:
        run(manager.capture_image_in_time_interval(task(module)))
    finally:
        module.asyncio.sleep = real_sleep
    assert [noop for noop, _kinds in calls["pipelines"]] == [True, True, True], "per-image calls are nested"
    assert all(kinds == ["capture"] for _noop, kinds in calls["pipelines"])
    assert seen_between == [[("capture", "cap-1")]] * 3
    assert activity.snapshot().tickets == (), "finished when the task ends"
    assert len(calls["stored"]) == 3


def test_a_capture_task_that_raises_finishes_its_ticket(capture_manager, activity):
    module, manager, calls = capture_manager
    calls["raise_at"] = 2
    with pytest.raises(RuntimeError):
        run(manager.capture_image_in_time_interval(task(module)))
    assert activity.snapshot().tickets == ()


def test_a_refused_capture_task_ends_before_its_first_image(capture_manager, activity, caplog):
    import logging

    caplog.set_level(logging.INFO)
    module, manager, calls = capture_manager
    assert activity.begin_quiesce()
    assert activity.commit_restart(lambda info: True, REFUSAL) == "committed"
    run(manager.capture_image_in_time_interval(task(module)))
    assert calls["pipelines"] == [] and calls["stored"] == []
    assert "Refusing capture task cap-1: " + REFUSAL in caplog.text
