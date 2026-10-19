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
"""Digital-input triggers and the run registry (triton-model-cleanup-wedge
task 4.3, design D7, "Digital-input triggers").

IN-IMAGE ONLY: ``utils/digital_input_thread_manager.py`` imports the
GStreamer executor and the Greengrass stack, as in
``test/backend-test/utils/test_digital_input_thread.py``; on a host without
``awsiot`` the module is skipped. The camera grab, the image-source read
and the executor's ``execute_workflow_pipeline`` are fakes that record the
order of the calls; the run registry is a fresh ``RunActivity``."""
import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("awsiot", reason="in the flask-app image only (GStreamer and Greengrass imports)")

from local_server_base_test_case import LocalServerBaseTestCase  # noqa: E402
from model.image_source import ImageSourceType  # noqa: E402
from utils.constants import GPIO_RISING  # noqa: E402
from utils import run_activity as ra  # noqa: E402

REFUSAL = ("The LocalServer backend is restarting to clear a wedged Triton model load (self-heal "
           "attempt 1); run the workflow again in a few minutes.")


class _FakeLatency:
    def __init__(self):
        self.stamps = {}

    def add_timestamp(self, name):
        self.stamps.setdefault(name, time.time())
        return self.stamps[name]

    def get_timestamp(self, name):
        return self.stamps.get(name, time.time())

    def commit_timestamps(self, session, capture_id):
        pass


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestDioRunRegistration(LocalServerBaseTestCase):

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"COMPONENT_WORK_PATH": "test/backend-test/resources",
                                      "LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": "test/",
                                      "INFERENCE_COMPONENT_DECOMPRESED_PATH": "test/"})
        env.start()
        self.addCleanup(env.stop)
        server_setup = types.ModuleType("utils.server_setup")
        server_setup.inference_result_accessor = MagicMock()
        modules = patch.dict(sys.modules, {"utils.server_setup": server_setup})
        modules.start()
        self.addCleanup(modules.stop)
        latency = patch("metrics.latency_metrics.LatencyMetrics", _FakeLatency)
        latency.start()
        self.addCleanup(latency.stop)
        self.activity = ra.RunActivity()
        previous = ra.install(self.activity)
        self.addCleanup(ra.install, previous)
        import utils.digital_input_thread_manager as dio
        self.dio = dio
        self.order = []
        self.release = threading.Event()
        self.nested = []
        for target, value in (("get_camera_id", MagicMock(return_value="cam-1")),
                              ("_get_image_source", MagicMock(side_effect=self._image_source)),
                              ("_get_camera_config", MagicMock(return_value={}))):
            p = patch.object(dio.DigitalInputThread, target, value)
            p.start()
            self.addCleanup(p.stop)
        for target, value in (("get_camera_frame", MagicMock(side_effect=self._grab)),
                              ("SessionLocal", _Session),
                              ("inference_results_utils", MagicMock())):
            p = patch.object(dio, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.workflow = {
            "workflowId": "wf-dio",
            "imageSourceId": "img-1",
            "inputConfigurations": [{"pin": 17, "debounceTime": 0, "triggerState": GPIO_RISING}],
            "featureConfigurations": [{"modelName": "model-a"}],
        }
        self.thread = dio.DigitalInputThread(self.workflow)
        self.addCleanup(self.thread.exit_event.set)
        self.thread.gst_pipeline_executor = MagicMock()
        self.thread.gst_pipeline_executor.execute_workflow_pipeline.side_effect = self._pipeline
        self.order.clear()

    def _image_source(self, workflow):
        self.order.append(("image_source", len(self.activity.snapshot().tickets)))
        return {"type": ImageSourceType.CAMERA}

    def _grab(self, camera_id, camera_config):
        self.order.append(("grab", len(self.activity.snapshot().tickets)))
        return {"data": b"\x00" * 12, "width": 2, "height": 2}

    def _pipeline(self, workflow, session, frame, latency_metrics=None):
        nested = ra.start_run("pipeline", "image-source-pipeline")
        self.nested.append(nested.noop)
        self.order.append(("pipeline", [t.kind for t in self.activity.snapshot().tickets]))
        self.release.wait(10)
        return "capture-1", {}

    def _wait_done(self):
        future = self.thread.future_in_progress
        if future is not None:
            future.result(10)

    def health(self):
        return self.dio.message_storage.get("wf-dio", {}).get("status")

    def test_a_trigger_registers_before_its_frame_grab(self):
        self.thread.trigger_workflow_and_debounce()
        self.assertEqual(self.order[0], ("image_source", 1))
        self.assertEqual(self.order[1], ("grab", 1))
        infos = self.activity.snapshot().tickets
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].kind, "pipeline")
        self.assertEqual(infos[0].models, ("model-a",))
        self.assertEqual(infos[0].workflow, "wf-dio")
        self.assertIsNone(ra._CURRENT.get(), "bind=False leaves the DIO thread's context empty")
        self.release.set()
        self._wait_done()

    def test_the_ticket_lives_until_execute_workflow_returns_on_the_pool_thread(self):
        self.thread.trigger_workflow_and_debounce()
        deadline = time.time() + 10
        while not self.nested and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.nested, [True], "run_pipeline's registration inside is a no-op")
        self.assertEqual(len(self.activity.snapshot().tickets), 1)
        self.release.set()
        self._wait_done()
        self.assertEqual(self.activity.snapshot().tickets, ())
        worker_context = self.thread.executor.submit(ra._CURRENT.get).result(10)
        self.assertIsNone(worker_context, "the worker thread's context holds no ticket afterwards")
        self.assertEqual(self.health(), self.dio.DIOProcessHealthStatusEnum.RUNNING)

    def test_a_refused_trigger_grabs_nothing_and_leaves_the_health(self):
        before = self.health()
        self.assertTrue(self.activity.begin_quiesce())
        self.assertEqual(self.activity.commit_restart(lambda info: True, REFUSAL), "committed")
        with self.assertLogs("utils.run_activity", level="INFO") as logs:
            self.thread.trigger_workflow_and_debounce()
        self.assertEqual(self.order, [])
        self.assertIsNone(self.thread.future_in_progress)
        self.thread.gst_pipeline_executor.execute_workflow_pipeline.assert_not_called()
        refusals = [line for line in logs.output if "Refusing workflow run wf-dio: " + REFUSAL in line]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(self.health(), before)
        self.assertEqual(self.activity.snapshot().tickets, ())

    def test_a_raising_grab_leaves_no_ticket(self):
        self.dio.get_camera_frame.side_effect = RuntimeError("camera unplugged")
        with self.assertRaises(RuntimeError):
            self.thread.trigger_workflow_and_debounce()
        self.assertEqual(self.activity.snapshot().tickets, ())

    def test_a_raising_submit_leaves_no_ticket(self):
        self.thread.executor = MagicMock()
        self.thread.executor.submit.side_effect = RuntimeError("cannot schedule new futures after shutdown")
        with self.assertRaises(RuntimeError):
            self.thread.trigger_workflow_and_debounce()
        self.assertEqual(self.activity.snapshot().tickets, ())

    def test_the_pipeline_is_full_branch_registers_nothing(self):
        busy = MagicMock()
        busy.done.return_value = False
        self.thread.future_in_progress = busy
        self.thread.trigger_workflow_and_debounce()
        self.assertEqual(self.order, [])
        self.assertEqual(self.activity.snapshot().tickets, ())

    def test_during_a_quiesce_the_trigger_grabs_at_once_and_defers_the_commit(self):
        self.assertTrue(self.activity.begin_quiesce())
        started = time.time()
        self.thread.trigger_workflow_and_debounce()
        self.assertLess(time.time() - started, 1.0)
        self.assertEqual([o[0] for o in self.order[:2]], ["image_source", "grab"])
        self.assertEqual(self.activity.commit_restart(lambda info: ra.is_blocked(info, ("model-a",)), REFUSAL),
                         "runs")
        self.activity.end_quiesce()
        self.release.set()
        self._wait_done()


if __name__ == "__main__":
    unittest.main()
