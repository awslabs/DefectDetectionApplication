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
"""Overhead budget of the run hook (workflow-telemetry-dashboards R5.2;
workflow-debug-mode tasks.md 1.1).

``notify_run_finished`` with a 1,000-detection record, delivered to an
enqueue-and-return observer, must return within 2 ms at p95 over 200
iterations. The budget is the engine-side cost of telemetry per run; folding
and assembly happen on the agents' threads.

The host is calibrated first: a host that needs more than 20 ms for a
reference loop of pure-Python work is too slow or too loaded for a timing
assertion to mean anything, and the test is skipped with that reason.
"""
import collections
import time
from types import SimpleNamespace

import pytest

from workflow_engine.telemetry import hooks

ITERATIONS = 200
BUDGET_MS = 2.0
#: A reference loop slower than this means the host cannot keep timing
#: promises (CI under load); the benchmark is skipped there.
CALIBRATION_LIMIT_MS = 20.0


def _calibrate_ms() -> float:
    start = time.perf_counter()
    total = 0
    for i in range(200_000):
        total += i % 7
    return (time.perf_counter() - start) * 1000.0


def _record_with_detections(count):
    items = [
        {"id": "%08x" % i, "label": "person", "confidence": 0.5,
         "x_min": 1.0, "y_min": 2.0, "x_max": 30.0, "y_max": 40.0}
        for i in range(count)
    ]
    execution = SimpleNamespace(
        id="exec-bench", status="completed", error=None, failing_node_id=None,
        capture_id="wf-1-exec-bench", output_dir="/tmp/out", log_path="/tmp/out/run.log",
        node_status_json=None,
    )
    registration = SimpleNamespace(id="wf-1:3", workflow_id="wf-1", version="3")
    return hooks.RunRecord.from_execution(
        execution, registration, None, {"detections": items}, {},
        1_700_000_000, 1_700_000_001_000, 1_700_000_001_100, 1_700_000_001_900, 0, None,
    )


def test_notify_run_finished_with_1000_detections_is_under_2ms_p95():
    calibration = _calibrate_ms()
    if calibration > CALIBRATION_LIMIT_MS:
        pytest.skip(
            "host too slow for a timing assertion (reference loop %.1f ms > %.1f ms)"
            % (calibration, CALIBRATION_LIMIT_MS)
        )

    hooks._clear_observers()
    inbox = collections.deque(maxlen=10_000)
    hooks.register_run_observer(inbox.append)
    try:
        record = _record_with_detections(1000)
        assert len(record.detections) == 1000
        samples = []
        for _ in range(ITERATIONS):
            start = time.perf_counter()
            hooks.notify_run_finished(record)
            samples.append((time.perf_counter() - start) * 1000.0)
    finally:
        hooks._clear_observers()

    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    # Recorded for the evidence file ($D/debug/edge-tests.md).
    print("\nhook benchmark: p50=%.4f ms p95=%.4f ms max=%.4f ms (calibration %.1f ms)"
          % (samples[len(samples) // 2], p95, samples[-1], calibration))
    assert len(inbox) == ITERATIONS
    assert p95 <= BUDGET_MS, "notify_run_finished p95 %.3f ms exceeds %.1f ms" % (p95, BUDGET_MS)
