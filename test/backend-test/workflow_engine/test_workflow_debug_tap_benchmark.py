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
"""Overhead budget of the log tap's ``emit`` (workflow-debug-mode design §5.1
``logtap.py``, R7.3; tasks.md 2.6).

``DebugLogHandler.emit`` for a 200-character message must take at most 50 us
at p95: it reads two context variables, formats the message, appends one
tuple to the inbox and returns. Same host calibration and skip rule as
``test_telemetry_hooks_benchmark.py``.
"""
import logging
import time

import pytest

from workflow_engine.telemetry import hooks
from workflow_engine.telemetry.debug.logtap import DebugLogHandler

ITERATIONS = 2000
BUDGET_US = 50.0
CALIBRATION_LIMIT_MS = 20.0


def _calibrate_ms() -> float:
    start = time.perf_counter()
    total = 0
    for i in range(200_000):
        total += i % 7
    return (time.perf_counter() - start) * 1000.0


def test_emit_of_a_200_character_message_is_under_50us_p95():
    calibration = _calibrate_ms()
    if calibration > CALIBRATION_LIMIT_MS:
        pytest.skip(
            "host too slow for a timing assertion (reference loop %.1f ms > %.1f ms)"
            % (calibration, CALIBRATION_LIMIT_MS)
        )
    handler = DebugLogHandler()
    message = "resolved model %s for node %s in %d ms: " + "x" * 150
    args = ("plates-detector", "model_1", 42)
    token = hooks.CURRENT_EXECUTION.set(("wf-1:3", "wf-1", "exec-bench"))
    samples = []
    try:
        for i in range(ITERATIONS):
            record = logging.LogRecord("workflow_engine.pipeline_executor", logging.DEBUG,
                                       "pipeline_executor.py", 1, message, args, None)
            start = time.perf_counter()
            handler.emit(record)
            samples.append((time.perf_counter() - start) * 1_000_000.0)
    finally:
        hooks.CURRENT_EXECUTION.reset(token)
    assert len(handler.inbox) == ITERATIONS
    assert len(handler.inbox[0][3]) >= 200
    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    print("\ntap emit benchmark: p50=%.2f us p95=%.2f us max=%.2f us (calibration %.1f ms)"
          % (samples[len(samples) // 2], p95, samples[-1], calibration))
    assert p95 <= BUDGET_US, "emit p95 %.2f us exceeds %.1f us" % (p95, BUDGET_US)
