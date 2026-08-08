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
"""Unit tests for vLLM runtime multimodal detection, error paths, and the
Triton generate-extension server image pass-through.

**Feature: edge-vlm-image-inference, Task 5.4**
**Validates: Requirements 4.2, 4.5, 4.6, 4.7, 4.8**

Covers:
- Multimodal detection from stubbed engine model configs: Qwen2-VL and
  Qwen2.5-VL architectures detect as image-capable, text-only
  architectures do not, and vLLM's ``is_multimodal_model`` flag is
  preferred over the architectures list (Requirements 4.2, 4.5).
- An engine failure during a multimodal generate raises
  :class:`GenerationError` carrying the model name and backend reason,
  leaving other loaded models untouched (Requirement 4.6).
- Non-image bytes for a multimodal model raise :class:`GenerationError`
  naming the image decoding failure without ever invoking the engine
  (Requirement 4.7).
- The Triton generate-extension server decodes an optional base64
  ``image`` field and passes the exact bytes to the manager; invalid
  base64 maps to 422; an absent field passes ``image=None``
  (Requirement 4.8).

Everything runs against fakes (injected engine factory, stubbed model
configs, a fake manager behind the FastAPI ``TestClient``); no GPU or
real vLLM install is required.
"""
import asyncio
import base64
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "backend"))

from vllm_runtime.manager import (  # noqa: E402
    GenerationError,
    ModelState,
    ModelStatus,
    VllmRuntimeManager,
)
from vllm_runtime.server import create_app  # noqa: E402


# --- fakes ------------------------------------------------------------------


class _HfConfig:
    def __init__(self, architectures):
        self.architectures = architectures


class _ModelConfig:
    """Stub of vLLM's ModelConfig: an hf_config with an architectures
    list, and optionally the ``is_multimodal_model`` flag."""

    def __init__(self, architectures=None, is_multimodal_model=None):
        self.hf_config = _HfConfig(list(architectures or []))
        if is_multimodal_model is not None:
            self.is_multimodal_model = is_multimodal_model


class _FakeRequestOutput:
    def __init__(self, text):
        self.outputs = [SimpleNamespace(text=text)]


class _FakeEngine:
    """Minimal AsyncLLMEngine surface: ``generate(prompt, params,
    request_id)`` returning an async iterator, plus a ``model_config``
    for multimodal detection. Records every prompt it is invoked with."""

    def __init__(self, model_config=None, error=None, text="generated"):
        if model_config is not None:
            self.model_config = model_config
        self._error = error
        self._text = text
        self.calls = []

    def generate(self, prompt, sampling_params, request_id):
        self.calls.append(prompt)
        return self._stream()

    async def _stream(self):
        if self._error is not None:
            raise self._error
        yield _FakeRequestOutput(self._text)


def _stage_repository(model_dir: Path, model_name: str) -> None:
    """Stage a minimal valid Triton_vLLM_Repository for ``model_name``."""
    version_dir = model_dir / model_name / "1"
    version_dir.mkdir(parents=True)
    (model_dir / model_name / "config.pbtxt").write_text('backend: "vllm"\n')
    (version_dir / "model.json").write_text("{}")


def _loaded_manager(tmp_path, engines):
    """A manager with every (name, engine) pair staged and loaded READY.

    The injected engine factory hands out engines in load order; the
    injected sampling-params factory is a passthrough dict."""
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


# --- multimodal detection (Requirements 4.2, 4.5) ---------------------------


class TestMultimodalDetection:
    def test_qwen2_vl_architecture_detects_multimodal(self, tmp_path):
        engine = _FakeEngine(
            model_config=_ModelConfig(["Qwen2VLForConditionalGeneration"])
        )
        manager = _loaded_manager(tmp_path, [("qwen2-vl", engine)])
        assert manager.image_supported("qwen2-vl") is True

    def test_qwen2_5_vl_architecture_detects_multimodal(self, tmp_path):
        engine = _FakeEngine(
            model_config=_ModelConfig(["Qwen2_5_VLForConditionalGeneration"])
        )
        manager = _loaded_manager(tmp_path, [("qwen2.5-vl", engine)])
        assert manager.image_supported("qwen2.5-vl") is True

    def test_text_only_architecture_is_not_multimodal(self, tmp_path):
        engine = _FakeEngine(model_config=_ModelConfig(["OPTForCausalLM"]))
        manager = _loaded_manager(tmp_path, [("opt-125m", engine)])
        assert manager.image_supported("opt-125m") is False

    def test_is_multimodal_model_flag_preferred_over_architectures(
        self, tmp_path
    ):
        # The flag wins in both directions: True with a text-only
        # architecture list, False with a Qwen VL architecture list.
        flagged_on = _FakeEngine(
            model_config=_ModelConfig(
                ["OPTForCausalLM"], is_multimodal_model=True
            )
        )
        flagged_off = _FakeEngine(
            model_config=_ModelConfig(
                ["Qwen2VLForConditionalGeneration"], is_multimodal_model=False
            )
        )
        manager = _loaded_manager(
            tmp_path, [("flag-on", flagged_on), ("flag-off", flagged_off)]
        )
        assert manager.image_supported("flag-on") is True
        assert manager.image_supported("flag-off") is False

    def test_unloaded_model_is_not_multimodal(self, tmp_path):
        manager = VllmRuntimeManager(model_dir=tmp_path)
        assert manager.image_supported("never-loaded") is False


# --- engine failure during multimodal generate (Requirement 4.6) ------------


class TestMultimodalEngineFailure:
    def test_engine_error_raises_generation_error_with_name_and_reason(
        self, tmp_path
    ):
        failing = _FakeEngine(
            model_config=_ModelConfig(["Qwen2VLForConditionalGeneration"]),
            error=RuntimeError("CUDA error: device kaput"),
        )
        healthy = _FakeEngine(
            model_config=_ModelConfig(["OPTForCausalLM"]), text="still fine"
        )
        manager = _loaded_manager(
            tmp_path, [("vlm-a", failing), ("text-b", healthy)]
        )

        with pytest.raises(GenerationError) as excinfo:
            asyncio.run(
                manager.generate("vlm-a", "describe", image=_jpeg_bytes())
            )
        assert excinfo.value.model_name == "vlm-a"
        assert "CUDA error: device kaput" in excinfo.value.reason
        # The failing engine received exactly one (multimodal) invocation.
        assert len(failing.calls) == 1
        assert isinstance(failing.calls[0], dict)
        assert "multi_modal_data" in failing.calls[0]

        # Every other loaded model is untouched: still READY, still serving.
        assert manager.state("text-b").state is ModelState.READY
        assert asyncio.run(manager.generate("text-b", "hi")) == "still fine"
        assert healthy.calls == ["hi"]

    def test_per_request_error_on_healthy_engine_keeps_model_ready(
        self, tmp_path
    ):
        # The fake engine exposes no ``errored`` attribute, so the manager
        # treats the failure as per-request and leaves the model READY for
        # the caller's retry policy.
        failing = _FakeEngine(
            model_config=_ModelConfig(["Qwen2VLForConditionalGeneration"]),
            error=RuntimeError("boom"),
        )
        manager = _loaded_manager(tmp_path, [("vlm-a", failing)])
        with pytest.raises(GenerationError):
            asyncio.run(
                manager.generate("vlm-a", "describe", image=_jpeg_bytes())
            )
        assert manager.state("vlm-a").state is ModelState.READY


# --- undecodable image bytes (Requirement 4.7) -------------------------------


class TestImageDecodeFailure:
    def test_non_image_bytes_raise_generation_error_without_engine_call(
        self, tmp_path
    ):
        engine = _FakeEngine(
            model_config=_ModelConfig(["Qwen2VLForConditionalGeneration"])
        )
        manager = _loaded_manager(tmp_path, [("qwen2-vl", engine)])

        with pytest.raises(GenerationError) as excinfo:
            asyncio.run(
                manager.generate(
                    "qwen2-vl", "describe", image=b"definitely not a JPEG"
                )
            )
        assert excinfo.value.model_name == "qwen2-vl"
        assert "decode" in excinfo.value.reason.lower()
        assert len(engine.calls) == 0

    def test_empty_image_bytes_raise_generation_error_without_engine_call(
        self, tmp_path
    ):
        engine = _FakeEngine(
            model_config=_ModelConfig(["Qwen2_5_VLForConditionalGeneration"])
        )
        manager = _loaded_manager(tmp_path, [("qwen2.5-vl", engine)])
        with pytest.raises(GenerationError) as excinfo:
            asyncio.run(manager.generate("qwen2.5-vl", "describe", image=b""))
        assert "decode" in excinfo.value.reason.lower()
        assert len(engine.calls) == 0


# --- server image pass-through (Requirement 4.8) -----------------------------


class _FakeManager:
    """Fake exposing the VllmRuntimeManager surface ``create_app`` uses:
    ``state``, ``generate``, ``generate_stream``. Records every generate
    invocation's arguments."""

    def __init__(self):
        self.calls = []

    def state(self, model_name):
        return ModelStatus(ModelState.READY)

    async def generate(
        self, model_name, prompt, sampling_params=None, image=None
    ):
        self.calls.append(
            {
                "model_name": model_name,
                "prompt": prompt,
                "sampling_params": sampling_params,
                "image": image,
            }
        )
        return "generated text"

    async def generate_stream(
        self, model_name, prompt, sampling_params=None, image=None
    ):
        self.calls.append(
            {
                "model_name": model_name,
                "prompt": prompt,
                "sampling_params": sampling_params,
                "image": image,
            }
        )
        yield "generated"


@pytest.fixture()
def server_client():
    manager = _FakeManager()
    client = TestClient(create_app(manager))
    return client, manager


class TestServerImagePassThrough:
    def test_generate_forwards_exact_decoded_image_bytes(self, server_client):
        client, manager = server_client
        image_bytes = b"\x00\x01\x02\xff jpeg-ish payload \x89"
        response = client.post(
            "/v2/models/qwen2-vl/generate",
            json={
                "text_input": "describe the part",
                "parameters": {"max_tokens": 32},
                "image": base64.b64encode(image_bytes).decode("ascii"),
            },
        )
        assert response.status_code == 200
        assert response.json() == {
            "model_name": "qwen2-vl",
            "text_output": "generated text",
        }
        assert len(manager.calls) == 1
        call = manager.calls[0]
        assert call["image"] == image_bytes
        assert call["model_name"] == "qwen2-vl"
        assert call["prompt"] == "describe the part"
        assert call["sampling_params"] == {"max_tokens": 32}

    def test_generate_invalid_base64_returns_422_without_manager_call(
        self, server_client
    ):
        client, manager = server_client
        response = client.post(
            "/v2/models/qwen2-vl/generate",
            json={"text_input": "describe", "image": "!!!not-base64!!!"},
        )
        assert response.status_code == 422
        assert "image" in str(response.json().get("detail", ""))
        assert manager.calls == []

    def test_generate_without_image_passes_none(self, server_client):
        client, manager = server_client
        response = client.post(
            "/v2/models/text-model/generate",
            json={"text_input": "hello", "parameters": {}},
        )
        assert response.status_code == 200
        assert len(manager.calls) == 1
        assert manager.calls[0]["image"] is None

    def test_generate_stream_forwards_exact_decoded_image_bytes(
        self, server_client
    ):
        client, manager = server_client
        image_bytes = b"stream payload \x00\x7f\x80"
        response = client.post(
            "/v2/models/qwen2-vl/generate_stream",
            json={
                "text_input": "describe",
                "image": base64.b64encode(image_bytes).decode("ascii"),
            },
        )
        assert response.status_code == 200
        assert len(manager.calls) == 1
        assert manager.calls[0]["image"] == image_bytes
