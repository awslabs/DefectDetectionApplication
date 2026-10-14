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
"""GPU memory reclaim on failed load / unload (spec:
vllm-restart-model-recovery).

Observed on-device (ryan-orin-nano/JP6, LocalServer v1.0.56): a failed
engine CONSTRUCTION (KV-cache out-of-memory) has no engine object to shut
down, yet the aborted initialization leaves ~14GB of GPU allocations
pinned in the runtime process - every plain load retry keeps OOMing until
an unload releases the memory. The manager now runs a best-effort reclaim
(gc + torch.cuda.empty_cache) after any failure transition and after
every unload, so the next load attempt starts from a clean allocator
state.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "backend"))

from vllm_runtime.manager import (  # noqa: E402
    ModelState,
    VllmRuntimeManager,
)


def _stage_repository(model_dir: Path, model_name: str) -> None:
    version_dir = model_dir / model_name / "1"
    version_dir.mkdir(parents=True)
    (model_dir / model_name / "config.pbtxt").write_text('backend: "vllm"\n')
    (version_dir / "model.json").write_text("{}")


def _recording_reclaim(monkeypatch):
    calls = []
    monkeypatch.setattr(
        VllmRuntimeManager, "_reclaim_gpu_memory",
        staticmethod(calls.append))
    return calls


class TestReclaimOnFailedLoad:
    def test_failed_engine_construction_triggers_reclaim(
            self, tmp_path, monkeypatch):
        """The OOM case: engine_factory raises, no engine object exists,
        the model transitions to FAILED and the reclaim runs so the
        aborted initialization's allocations are released."""
        calls = _recording_reclaim(monkeypatch)

        def exploding_factory(engine_args):
            raise RuntimeError(
                "No available memory for the cache blocks. Try increasing "
                "`gpu_memory_utilization` when initializing the engine.")

        manager = VllmRuntimeManager(
            model_dir=tmp_path, engine_factory=exploding_factory)
        _stage_repository(tmp_path, "qwen")

        status = asyncio.run(manager.load("qwen"))

        assert status.state is ModelState.FAILED
        assert "No available memory" in status.reason
        assert calls == ["qwen"]

    def test_successful_load_does_not_reclaim(self, tmp_path, monkeypatch):
        calls = _recording_reclaim(monkeypatch)
        manager = VllmRuntimeManager(
            model_dir=tmp_path, engine_factory=lambda engine_args: object())
        _stage_repository(tmp_path, "opt")

        status = asyncio.run(manager.load("opt"))

        assert status.state is ModelState.READY
        assert calls == []


class TestReclaimOnUnload:
    def test_unload_triggers_reclaim(self, tmp_path, monkeypatch):
        calls = _recording_reclaim(monkeypatch)
        manager = VllmRuntimeManager(
            model_dir=tmp_path, engine_factory=lambda engine_args: object())
        _stage_repository(tmp_path, "opt")
        asyncio.run(manager.load("opt"))

        assert manager.unload("opt") is True
        assert calls == ["opt"]

    def test_unload_of_untracked_model_does_not_reclaim(
            self, tmp_path, monkeypatch):
        calls = _recording_reclaim(monkeypatch)
        manager = VllmRuntimeManager(model_dir=tmp_path)
        assert manager.unload("never-loaded") is False
        assert calls == []


class TestReclaimIsBestEffort:
    def test_reclaim_tolerates_missing_torch(self):
        """torch only exists on vLLM-capable images; the reclaim must
        never raise regardless of the host environment."""
        VllmRuntimeManager._reclaim_gpu_memory("any-model")
