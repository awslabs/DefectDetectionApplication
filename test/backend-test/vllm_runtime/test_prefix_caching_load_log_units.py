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
"""Unit tests for the manager's prefix-caching load-time log line
(spec: vllm-workflow-latency-optimization, task 2.2).

Covers:
- A successful load whose engine arguments carry a truthy
  ``enable_prefix_caching`` writes exactly one INFO application-log line
  naming the model and stating Prefix_Caching is active (Requirement 4.1).
- A falsy or absent ``enable_prefix_caching`` produces no such line and
  leaves the load behavior unchanged (Requirement 4.6).

Everything runs against injected fake engines; no GPU or vLLM install.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "backend"))

from vllm_runtime.manager import (  # noqa: E402
    ModelState,
    VllmRuntimeManager,
)


def _stage_repository(model_dir: Path, model_name: str,
                      engine_args: dict) -> None:
    version_dir = model_dir / model_name / "1"
    version_dir.mkdir(parents=True)
    (model_dir / model_name / "config.pbtxt").write_text('backend: "vllm"\n')
    (version_dir / "model.json").write_text(json.dumps(engine_args))


def _load(tmp_path, model_name, engine_args, caplog):
    manager = VllmRuntimeManager(
        model_dir=tmp_path,
        engine_factory=lambda args: object(),
        sampling_params_factory=dict,
    )
    _stage_repository(tmp_path, model_name, engine_args)
    with caplog.at_level(logging.INFO, logger="vllm_runtime.manager"):
        status = asyncio.run(manager.load(model_name))
    assert status.state is ModelState.READY
    return [
        record for record in caplog.records
        if record.levelno == logging.INFO
        and "Prefix_Caching" in record.getMessage()
    ]


class TestPrefixCachingLoadLog:
    def test_truthy_flag_logs_one_info_line_naming_model(
            self, tmp_path, caplog):
        lines = _load(tmp_path, "qwen3-vl",
                      {"enable_prefix_caching": True}, caplog)
        assert len(lines) == 1
        message = lines[0].getMessage()
        assert "qwen3-vl" in message
        assert "Prefix_Caching" in message
        assert "active" in message

    def test_false_flag_logs_nothing(self, tmp_path, caplog):
        assert _load(tmp_path, "text-a",
                     {"enable_prefix_caching": False}, caplog) == []

    def test_absent_flag_logs_nothing(self, tmp_path, caplog):
        assert _load(tmp_path, "text-b", {}, caplog) == []
