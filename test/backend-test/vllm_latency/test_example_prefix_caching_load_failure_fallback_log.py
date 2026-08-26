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
"""Example test for the prefix-caching load-failure fallback log (task 2.6).

# Feature: vllm-workflow-latency-optimization, Example: prefix-caching
# load-failure fallback log

A load failure for a model whose engine arguments set
``enable_prefix_caching`` flows through the manager's existing FAILED path
(Requirement 4.4) and produces the existing ERROR application-log line
naming the model and the classified failure reason — that ERROR line is the
R4.5 fallback notification. No new mechanism exists: the test pins the
established behavior so the fallback notification cannot silently regress.

**Validates: Requirements 4.4, 4.5**

The failure is injected through the manager's injectable ``engine_factory``
seam (the established pattern of
``test/backend-test/vllm_runtime/test_manager_memory_reclaim.py``) with a
KV-cache out-of-memory message, the on-device shape of a memory rejection
that engine construction reports when prefix caching enlarges the KV
budget. No GPU or vLLM install is required.
"""
import asyncio
import json
import logging
from pathlib import Path

from vllm_runtime.manager import (
    KV_CACHE_EXHAUSTION_TOKEN,
    ModelState,
    VllmRuntimeManager,
)

MODEL_NAME = "qwen3-vl-prefix"
OOM_MESSAGE = (
    "No available memory for the cache blocks. Try increasing "
    "`gpu_memory_utilization` when initializing the engine."
)


def _stage_repository(model_dir: Path, model_name: str,
                      engine_args: dict) -> None:
    version_dir = model_dir / model_name / "1"
    version_dir.mkdir(parents=True)
    (model_dir / model_name / "config.pbtxt").write_text('backend: "vllm"\n')
    (version_dir / "model.json").write_text(json.dumps(engine_args))


class TestPrefixCachingLoadFailureFallbackLog:
    def test_failed_load_with_prefix_caching_logs_model_and_classified_reason(
            self, tmp_path, caplog):
        """A load failure with ``enable_prefix_caching`` set ends FAILED via
        the existing path (R4.4) and writes the existing ERROR line naming
        the model and the classified reason — the R4.5 fallback
        notification."""
        def exploding_factory(engine_args):
            raise RuntimeError(OOM_MESSAGE)

        manager = VllmRuntimeManager(
            model_dir=tmp_path,
            engine_factory=exploding_factory,
            sampling_params_factory=dict,
        )
        _stage_repository(tmp_path, MODEL_NAME,
                          {"enable_prefix_caching": True})

        with caplog.at_level(logging.INFO, logger="vllm_runtime.manager"):
            status = asyncio.run(manager.load(MODEL_NAME))

        # Existing FAILED path: terminal FAILED state with the classified
        # reason retained on the status (R4.4).
        assert status.state is ModelState.FAILED
        assert status.reason.startswith(KV_CACHE_EXHAUSTION_TOKEN)
        assert OOM_MESSAGE in status.reason

        # The existing ERROR application-log line names the model and the
        # classified reason — the R4.5 fallback notification.
        error_lines = [
            record.getMessage() for record in caplog.records
            if record.levelno == logging.ERROR
        ]
        assert len(error_lines) == 1
        message = error_lines[0]
        assert MODEL_NAME in message
        assert "failed" in message
        assert KV_CACHE_EXHAUSTION_TOKEN in message
        assert OOM_MESSAGE in message

        # The Prefix_Caching-active INFO line belongs to the load-SUCCESS
        # path only (R4.1) — a failed load must not claim it is active.
        assert not any(
            "Prefix_Caching" in record.getMessage()
            for record in caplog.records
        )
