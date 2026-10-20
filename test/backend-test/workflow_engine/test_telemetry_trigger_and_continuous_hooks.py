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
"""The trigger gate and continuous runner hook points
(workflow-telemetry-dashboards design §5.2 "Hook points"; workflow-debug-mode
tasks.md 1.6).

``TriggerGate.fire`` notifies ``drop_policy`` and ``queue_full`` discards
with the owning registration id (which ``WorkflowActivationCore.add_trigger``
passes); ``ContinuousRunner._count`` notifies every counter increment and
``_note_model_wait`` the wait document, the ``modelUnavailable`` tick still
arriving beside it.

Validates: dashboards R2.2, R2.5, R5.2; debug R4.1.
"""
import pytest

from workflow_engine_test_utils import make_session_factory  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.continuous_runner import ContinuousRunner
from workflow_engine.model_gate import ModelWait
from workflow_engine.stream_feed import StreamFeed
from workflow_engine.telemetry import hooks
from workflow_engine.trigger_runtime import (
    POLICY_DROP,
    POLICY_QUEUE,
    ActivationQueue,
    FiringSequence,
    TriggerGate,
    TriggerPolicy,
    WorkflowActivationCore,
)

REGISTRATION_ID = "wf-1:3"


@pytest.fixture(autouse=True)
def clean_registries():
    hooks._clear_observers()
    yield
    hooks._clear_observers()


class ManualTimerFactory:
    def __call__(self, delay, callback):
        class _Timer:
            def start(self):
                pass

            def cancel(self):
                pass

        return _Timer()


def make_gate(policy_name, *, queue_depth=10, in_flight=False, registration_id=REGISTRATION_ID):
    policy = TriggerPolicy(
        trigger_node_id="trig-1", concurrency_policy=policy_name,
        queue_depth=queue_depth, debounce_ms=500, priority=100,
    )
    queue = ActivationQueue()
    gate = TriggerGate(
        policy, queue, FiringSequence(), in_flight_probe=lambda node_id: in_flight,
        timer_factory=ManualTimerFactory(), clock=lambda: 0.0,
        registration_id=registration_id,
    )
    return gate, queue


class TestTriggerGate:
    def test_drop_policy_discard_notifies_with_the_registration_id(self):
        drops = []
        hooks.register_drop_observer(lambda *a: drops.append(a))
        gate, queue = make_gate(POLICY_DROP, in_flight=True)
        assert gate.fire({"topic": "a/b"}) is False
        assert drops == [(REGISTRATION_ID, "trig-1", "drop_policy")]
        assert queue.pending_count("trig-1") == 0

    def test_accepted_firings_notify_nothing(self):
        drops = []
        hooks.register_drop_observer(lambda *a: drops.append(a))
        gate, queue = make_gate(POLICY_QUEUE, queue_depth=2)
        assert gate.fire({"topic": "a"}) is True
        assert gate.fire({"topic": "b"}) is True
        assert drops == []
        assert queue.pending_count("trig-1") == 2

    def test_queue_full_discard_notifies_with_the_registration_id(self):
        drops = []
        hooks.register_drop_observer(lambda *a: drops.append(a))
        gate, queue = make_gate(POLICY_QUEUE, queue_depth=1)
        assert gate.fire({"topic": "a"}) is True
        assert gate.fire({"topic": "b"}) is False
        assert gate.fire({"topic": "c"}) is False
        assert drops == [
            (REGISTRATION_ID, "trig-1", "queue_full"),
            (REGISTRATION_ID, "trig-1", "queue_full"),
        ]

    def test_a_gate_built_without_a_registration_id_notifies_none(self):
        drops = []
        hooks.register_drop_observer(lambda *a: drops.append(a))
        gate, _ = make_gate(POLICY_DROP, in_flight=True, registration_id=None)
        gate.fire({})
        assert drops == [(None, "trig-1", "drop_policy")]

    def test_activation_core_passes_its_registration_id_to_the_gate(self):
        core = WorkflowActivationCore(REGISTRATION_ID, run_starter=lambda activation: None,
                                      timer_factory=ManualTimerFactory())
        policy = TriggerPolicy(trigger_node_id="trig-9", concurrency_policy=POLICY_DROP,
                               queue_depth=10, debounce_ms=500, priority=100)
        gate = core.add_trigger(policy)
        assert gate._registration_id == REGISTRATION_ID
        drops = []
        hooks.register_drop_observer(lambda *a: drops.append(a))
        assert core.fire("trig-9", {"topic": "x"}) is True
        assert core.fire("trig-9", {"topic": "y"}) is False  # pending activation: drop
        assert drops == [(REGISTRATION_ID, "trig-9", "drop_policy")]


def make_feed(fps=1.0):
    return StreamFeed(node_id="cam", protocol="rtsp", camera_key="cfg-7", url="rtsp://camera.local/main",
                      processing_mode="continuous", frames_per_second=fps, max_frame_age_ms=2000,
                      keep_recent_runs=20, keep_notable_runs=200, camera_source_id="cfg-7")


class FakeStream:
    def health(self, camera_key):
        return {"cameraKey": camera_key, "state": "streaming"}

    def latest_frame(self, camera_key, after_seq=0, max_age_ms=None, wait_ms=0):
        return None


class MemoryStore:
    def insert(self, registration_id, context):
        return "exec-1"

    def status(self, execution_id):
        return "completed"


def make_runner():
    return ContinuousRunner(REGISTRATION_ID, make_feed(), execute=lambda execution_id: None,
                            stream_manager=FakeStream(), store=MemoryStore(),
                            clock=lambda: 100.0, wall=lambda: 1_790_000_000.0)


class TestContinuousRunner:
    def test_count_notifies_every_counter_key(self):
        ticks = []
        hooks.register_continuous_observer(lambda *a: ticks.append(a))
        runner = make_runner()
        runner._count("skippedBusy")
        runner._count("skippedNoNewFrame", 3)
        runner._count("outputsSent")
        assert ticks == [
            (REGISTRATION_ID, "skippedBusy", 1),
            (REGISTRATION_ID, "skippedNoNewFrame", 3),
            (REGISTRATION_ID, "outputsSent", 1),
        ]
        assert runner.counters()["skippedBusy"] == 1
        assert runner.counters()["skippedNoNewFrame"] == 3

    def test_note_model_wait_notifies_the_document_and_the_model_unavailable_tick(self):
        ticks, model = [], []
        hooks.register_continuous_observer(lambda *a: ticks.append(a))
        hooks.register_model_observer(lambda *a: model.append(a))
        runner = make_runner()
        wait = ModelWait(model="plates", triton_model="model-plates", state="LOADING",
                         reason=None, since_ms=1_790_000_000_000, stalled=False)

        runner._note_model_wait(wait, 100.0)
        runner._note_model_wait(wait, 103.0)  # a later poll of the same wait
        runner._note_model_wait(None, 106.0)

        assert model == [
            ("wait", REGISTRATION_ID, wait.as_document()),
            ("wait", REGISTRATION_ID, wait.as_document()),
            ("wait", REGISTRATION_ID, None),
        ]
        # The modelUnavailable tick still arrives, once per None -> wait transition.
        assert ticks == [(REGISTRATION_ID, "modelUnavailable", 1)]
        assert runner.counters()["modelUnavailable"] == 1

    def test_a_raising_observer_never_disturbs_the_runner(self):
        def boom(*args):
            raise RuntimeError("observer boom")

        hooks.register_continuous_observer(boom)
        hooks.register_model_observer(boom)
        runner = make_runner()
        runner._count("skippedBusy")
        runner._note_model_wait(None, 100.0)
        assert runner.counters()["skippedBusy"] == 1
