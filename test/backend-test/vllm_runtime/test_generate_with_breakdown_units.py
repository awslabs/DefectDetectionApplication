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
"""Unit tests for the manager's instrumented generate path
(spec: vllm-workflow-latency-optimization, task 2.1).

Covers:
- ``generate_with_breakdown`` returns the generated text plus a
  Generation_Phase_Breakdown built from the manager's monotonic capture
  (Requirements 1.1, 1.3).
- ``generate`` delegates to it and returns exactly the same text with its
  signature and error semantics untouched (Requirement 9.1).
- Error paths are byte-identical: ModelUnavailableError for a non-READY
  model, GenerationError on an engine failure (Requirements 1.5, 9.1).
- The image placeholder token id is read best-effort from the engine's
  model config and cached per managed model (Requirement 5.1).

Everything runs against injected fake engines; no GPU or vLLM install.
"""
import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "backend"))

from vllm_runtime.generation_metrics import (  # noqa: E402
    GenerationPhaseBreakdown,
)
from vllm_runtime.manager import (  # noqa: E402
    GenerationError,
    ModelState,
    ModelUnavailableError,
    VllmRuntimeManager,
)


IMAGE_TOKEN_ID = 151655


class _HfConfig:
    def __init__(self, architectures, image_token_id=None):
        self.architectures = architectures
        if image_token_id is not None:
            self.image_token_id = image_token_id


class _ModelConfig:
    def __init__(self, architectures=None, image_token_id=None):
        self.hf_config = _HfConfig(list(architectures or []),
                                   image_token_id=image_token_id)


class _FakeRequestOutput:
    def __init__(self, text, prompt_token_ids=None, token_ids=None,
                 finish_reason=None):
        self.prompt_token_ids = prompt_token_ids
        self.outputs = [SimpleNamespace(text=text,
                                        token_ids=token_ids,
                                        finish_reason=finish_reason)]
        self.metrics = None


class _FakeEngine:
    """Minimal AsyncLLMEngine surface recording prompts, yielding one or
    more request outputs (or raising)."""

    def __init__(self, outputs=None, model_config=None, error=None,
                 tokenizer=None):
        if model_config is not None:
            self.model_config = model_config
        if tokenizer is not None:
            self._tokenizer = tokenizer
        self._outputs = outputs or [_FakeRequestOutput("generated")]
        self._error = error
        self.calls = []

    async def get_tokenizer(self):
        return self._tokenizer

    def generate(self, prompt, sampling_params, request_id):
        self.calls.append(prompt)
        return self._stream()

    async def _stream(self):
        if self._error is not None:
            raise self._error
        for output in self._outputs:
            yield output


def _stage_repository(model_dir: Path, model_name: str) -> None:
    version_dir = model_dir / model_name / "1"
    version_dir.mkdir(parents=True)
    (model_dir / model_name / "config.pbtxt").write_text('backend: "vllm"\n')
    (version_dir / "model.json").write_text("{}")


def _loaded_manager(tmp_path, engines):
    queue = [engine for _, engine in engines]
    manager = VllmRuntimeManager(
        model_dir=tmp_path,
        engine_factory=lambda engine_args: queue.pop(0),
        sampling_params_factory=dict,
    )
    for name, _ in engines:
        _stage_repository(tmp_path, name)
        status = asyncio.run(manager.load(name))
        assert status.state is ModelState.READY
    return manager


def _jpeg_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


class _FakeTokenizer:
    """Chat-template-less tokenizer so the multimodal prompt path uses the
    documented Qwen fallback form."""

    chat_template = None

    def apply_chat_template(self, *args, **kwargs):  # pragma: no cover
        raise ValueError("no chat template")


class TestGenerateWithBreakdown:
    def test_returns_text_and_manager_clock_breakdown(self, tmp_path):
        output = _FakeRequestOutput(
            "generated", prompt_token_ids=[1, 2, 3],
            token_ids=[7, 8], finish_reason="stop",
        )
        manager = _loaded_manager(
            tmp_path, [("text-a", _FakeEngine(outputs=[output]))]
        )
        text, breakdown = asyncio.run(
            manager.generate_with_breakdown("text-a", "hi")
        )
        assert text == "generated"
        assert isinstance(breakdown, GenerationPhaseBreakdown)
        # V1-shaped output (metrics None): manager monotonic fallback.
        assert breakdown.queueing_ms is None
        assert breakdown.prefill_includes_queueing is True
        assert isinstance(breakdown.prefill_ms, int)
        assert breakdown.prefill_ms >= 0
        assert isinstance(breakdown.decode_ms, int)
        assert breakdown.decode_ms >= 0
        assert breakdown.prompt_tokens == 3
        assert breakdown.output_tokens == 2
        assert breakdown.image_tokens_applicable is False
        assert breakdown.truncated is False

    def test_generate_delegates_and_returns_identical_text(self, tmp_path):
        outputs = [
            _FakeRequestOutput("generated", prompt_token_ids=[1],
                               token_ids=[5], finish_reason="stop"),
        ]
        engine_a = _FakeEngine(outputs=list(outputs))
        engine_b = _FakeEngine(outputs=list(outputs))
        manager = _loaded_manager(
            tmp_path, [("text-a", engine_a), ("text-b", engine_b)]
        )
        via_generate = asyncio.run(manager.generate("text-a", "hi"))
        via_breakdown, _ = asyncio.run(
            manager.generate_with_breakdown("text-b", "hi")
        )
        assert via_generate == via_breakdown == "generated"
        assert engine_a.calls == engine_b.calls == ["hi"]

    def test_model_unavailable_error_preserved(self, tmp_path):
        manager = VllmRuntimeManager(
            model_dir=tmp_path,
            engine_factory=lambda engine_args: _FakeEngine(),
            sampling_params_factory=dict,
        )
        with pytest.raises(ModelUnavailableError):
            asyncio.run(manager.generate_with_breakdown("nope", "hi"))
        with pytest.raises(ModelUnavailableError):
            asyncio.run(manager.generate("nope", "hi"))

    def test_generation_error_preserved(self, tmp_path):
        manager = _loaded_manager(
            tmp_path,
            [("text-a", _FakeEngine(error=RuntimeError("engine broke")))],
        )
        with pytest.raises(GenerationError) as excinfo:
            asyncio.run(manager.generate_with_breakdown("text-a", "hi"))
        assert excinfo.value.model_name == "text-a"
        assert "engine broke" in excinfo.value.reason

    def test_no_output_raises_generation_error(self, tmp_path):
        class _EmptyEngine(_FakeEngine):
            async def _stream(self):
                return
                yield  # pragma: no cover

        manager = _loaded_manager(tmp_path, [("text-a", _EmptyEngine())])
        with pytest.raises(GenerationError):
            asyncio.run(manager.generate_with_breakdown("text-a", "hi"))

    def test_image_tokens_counted_from_placeholder_id(self, tmp_path):
        prompt_ids = [10, IMAGE_TOKEN_ID, IMAGE_TOKEN_ID, IMAGE_TOKEN_ID, 11]
        output = _FakeRequestOutput(
            "described", prompt_token_ids=prompt_ids,
            token_ids=[9], finish_reason="stop",
        )
        engine = _FakeEngine(
            outputs=[output],
            model_config=_ModelConfig(
                ["Qwen2VLForConditionalGeneration"],
                image_token_id=IMAGE_TOKEN_ID,
            ),
            tokenizer=_FakeTokenizer(),
        )
        manager = _loaded_manager(tmp_path, [("qwen2-vl", engine)])
        text, breakdown = asyncio.run(
            manager.generate_with_breakdown(
                "qwen2-vl", "describe", image=_jpeg_bytes()
            )
        )
        assert text == "described"
        assert breakdown.image_tokens_applicable is True
        assert breakdown.image_tokens == 3

    def test_unreadable_placeholder_id_marks_unavailable(self, tmp_path):
        output = _FakeRequestOutput(
            "described", prompt_token_ids=[1, 2],
            token_ids=[9], finish_reason="stop",
        )
        engine = _FakeEngine(
            outputs=[output],
            model_config=_ModelConfig(["Qwen2VLForConditionalGeneration"]),
            tokenizer=_FakeTokenizer(),
        )
        manager = _loaded_manager(tmp_path, [("qwen2-vl", engine)])
        _, breakdown = asyncio.run(
            manager.generate_with_breakdown(
                "qwen2-vl", "describe", image=_jpeg_bytes()
            )
        )
        assert breakdown.image_tokens_applicable is True
        assert breakdown.image_tokens is None

    def test_placeholder_id_cached_per_managed_model(self, tmp_path):
        config = _ModelConfig(
            ["Qwen2VLForConditionalGeneration"],
            image_token_id=IMAGE_TOKEN_ID,
        )
        engine = _FakeEngine(model_config=config, tokenizer=_FakeTokenizer())
        manager = _loaded_manager(tmp_path, [("qwen2-vl", engine)])
        assert manager._image_placeholder_token_id(
            "qwen2-vl") == IMAGE_TOKEN_ID
        # Mutating the config after the first read must not change the
        # cached answer (cached like the multimodal flag).
        config.hf_config.image_token_id = 42
        assert manager._image_placeholder_token_id(
            "qwen2-vl") == IMAGE_TOKEN_ID
