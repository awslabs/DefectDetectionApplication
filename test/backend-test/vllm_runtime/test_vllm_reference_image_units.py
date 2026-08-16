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
"""Unit tests for vLLM runtime two-image (reference) generation.

**Feature: vlm-anomaly-reference-parity, Task 4**
**Validates: Requirements 5.5, 6.1, 6.2, 6.5, 6.6**

Covers:
- ``limit_mm_per_prompt`` engine-args defaulting at load: absent from
  model.json -> defaulted to ``{"image": 2}`` before the engine factory;
  an explicit model.json value is honored unchanged (Requirement 6.6).
- Two-image prompt construction: input image placed before the reference
  in both the templated text and ``multi_modal_data["image"]``; the
  single-image prompt is unchanged; an undecodable reference raises
  :class:`GenerationError` naming the reference before the engine is
  invoked (Requirements 6.1, 6.2, 6.5).
- Triton generate-extension server reference pass-through: a valid
  base64 ``reference_image`` is decoded and forwarded to the manager;
  invalid base64 maps to 422 naming the field; an absent field leaves
  the manager invocation reference-free (Requirement 5.5).

Everything runs against fakes (injected engine factory, stubbed model
configs, a fake manager behind the FastAPI ``TestClient``); no GPU or
real vLLM install is required.
"""
import asyncio
import base64
import io
import json
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
    def __init__(self, architectures=None):
        self.hf_config = _HfConfig(list(architectures or []))


class _FakeRequestOutput:
    def __init__(self, text):
        self.outputs = [SimpleNamespace(text=text)]


class _FakeEngine:
    """Minimal AsyncLLMEngine surface recording every prompt argument."""

    def __init__(self, model_config=None, text="generated"):
        if model_config is not None:
            self.model_config = model_config
        self._text = text
        self.calls = []

    def generate(self, prompt, sampling_params, request_id):
        self.calls.append(prompt)
        return self._stream()

    async def _stream(self):
        yield _FakeRequestOutput(self._text)


def _stage_repository(model_dir: Path, model_name: str, model_json="{}"):
    """Stage a minimal valid Triton_vLLM_Repository for ``model_name``."""
    version_dir = model_dir / model_name / "1"
    version_dir.mkdir(parents=True)
    (model_dir / model_name / "config.pbtxt").write_text('backend: "vllm"\n')
    (version_dir / "model.json").write_text(model_json)


def _jpeg_bytes(color=(200, 30, 30)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _multimodal_manager(tmp_path, engine, model_name="qwen2-vl"):
    """A manager with one multimodal fake engine loaded READY."""
    manager = VllmRuntimeManager(
        model_dir=tmp_path,
        engine_factory=lambda engine_args: engine,
        sampling_params_factory=dict,
    )
    _stage_repository(tmp_path, model_name)
    status = asyncio.run(manager.load(model_name))
    assert status.state is ModelState.READY
    return manager


# --- limit_mm_per_prompt defaulting (Requirement 6.6) ------------------------


class TestLimitMmPerPromptDefaulting:
    def test_absent_model_json_value_is_defaulted_before_the_factory(
        self, tmp_path
    ):
        captured = {}

        def factory(engine_args):
            captured.update(engine_args)
            return _FakeEngine()

        manager = VllmRuntimeManager(
            model_dir=tmp_path,
            engine_factory=factory,
            sampling_params_factory=dict,
        )
        _stage_repository(tmp_path, "qwen2-vl", model_json="{}")
        status = asyncio.run(manager.load("qwen2-vl"))
        assert status.state is ModelState.READY
        assert captured["limit_mm_per_prompt"] == {"image": 2}
        # The tracked engine args carry the applied default too.
        assert manager.engine_args("qwen2-vl")["limit_mm_per_prompt"] == {
            "image": 2
        }

    def test_explicit_model_json_value_is_honored_unchanged(self, tmp_path):
        captured = {}

        def factory(engine_args):
            captured.update(engine_args)
            return _FakeEngine()

        manager = VllmRuntimeManager(
            model_dir=tmp_path,
            engine_factory=factory,
            sampling_params_factory=dict,
        )
        _stage_repository(
            tmp_path,
            "qwen2-vl",
            model_json=json.dumps(
                {"limit_mm_per_prompt": {"image": 5}, "max_model_len": 2048}
            ),
        )
        status = asyncio.run(manager.load("qwen2-vl"))
        assert status.state is ModelState.READY
        assert captured["limit_mm_per_prompt"] == {"image": 5}
        assert captured["max_model_len"] == 2048


# --- two-image prompt construction (Requirements 6.1, 6.2, 6.5) --------------


class TestTwoImagePrompt:
    def test_reference_builds_ordered_two_image_prompt(self, tmp_path):
        engine = _FakeEngine(
            model_config=_ModelConfig(["Qwen2VLForConditionalGeneration"])
        )
        manager = _multimodal_manager(tmp_path, engine)
        text = asyncio.run(
            manager.generate(
                "qwen2-vl",
                "compare the two",
                image=_jpeg_bytes(),
                reference_image=_jpeg_bytes((30, 200, 30)),
            )
        )
        assert text == "generated"
        assert len(engine.calls) == 1
        prompt = engine.calls[0]
        assert isinstance(prompt, dict)
        # Input label before reference label in the templated text.
        templated = prompt["prompt"]
        assert "Input image:" in templated
        assert "Reference image:" in templated
        assert templated.index("Input image:") < templated.index(
            "Reference image:"
        )
        assert "compare the two" in templated
        # Two decoded images, input first.
        images = prompt["multi_modal_data"]["image"]
        assert isinstance(images, list)
        assert len(images) == 2

    def test_single_image_prompt_is_unchanged(self, tmp_path):
        engine = _FakeEngine(
            model_config=_ModelConfig(["Qwen2VLForConditionalGeneration"])
        )
        manager = _multimodal_manager(tmp_path, engine)
        asyncio.run(
            manager.generate("qwen2-vl", "describe", image=_jpeg_bytes())
        )
        prompt = engine.calls[0]
        assert isinstance(prompt, dict)
        # Pre-feature single-image form: no labels, single (non-list) image.
        assert "Reference image:" not in prompt["prompt"]
        assert not isinstance(prompt["multi_modal_data"]["image"], list)

    def test_undecodable_reference_raises_before_engine_invocation(
        self, tmp_path
    ):
        engine = _FakeEngine(
            model_config=_ModelConfig(["Qwen2VLForConditionalGeneration"])
        )
        manager = _multimodal_manager(tmp_path, engine)
        with pytest.raises(GenerationError) as excinfo:
            asyncio.run(
                manager.generate(
                    "qwen2-vl",
                    "compare",
                    image=_jpeg_bytes(),
                    reference_image=b"definitely not a JPEG",
                )
            )
        assert excinfo.value.model_name == "qwen2-vl"
        assert "reference" in excinfo.value.reason.lower()
        assert engine.calls == []


# --- server reference pass-through (Requirement 5.5) -------------------------


class _FakeManager:
    """Fake of the VllmRuntimeManager surface ``create_app`` uses,
    recording every generate invocation's arguments including the
    optional reference image."""

    def __init__(self):
        self.calls = []

    def state(self, model_name):
        return ModelStatus(ModelState.READY)

    async def generate(
        self,
        model_name,
        prompt,
        sampling_params=None,
        image=None,
        reference_image=None,
    ):
        self.calls.append(
            {
                "model_name": model_name,
                "prompt": prompt,
                "sampling_params": sampling_params,
                "image": image,
                "reference_image": reference_image,
            }
        )
        return "generated text"

    async def generate_stream(
        self,
        model_name,
        prompt,
        sampling_params=None,
        image=None,
        reference_image=None,
    ):
        self.calls.append(
            {
                "model_name": model_name,
                "prompt": prompt,
                "sampling_params": sampling_params,
                "image": image,
                "reference_image": reference_image,
            }
        )
        yield "generated"


@pytest.fixture()
def server_client():
    manager = _FakeManager()
    client = TestClient(create_app(manager))
    return client, manager


class TestServerReferencePassThrough:
    def test_generate_forwards_exact_decoded_reference_bytes(
        self, server_client
    ):
        client, manager = server_client
        image_bytes = b"\x00\x01 input payload \xff"
        reference_bytes = b"\x02\x03 reference payload \x89"
        response = client.post(
            "/v2/models/qwen2-vl/generate",
            json={
                "text_input": "compare",
                "parameters": {"max_tokens": 32},
                "image": base64.b64encode(image_bytes).decode("ascii"),
                "reference_image": base64.b64encode(reference_bytes).decode(
                    "ascii"
                ),
            },
        )
        assert response.status_code == 200
        assert len(manager.calls) == 1
        call = manager.calls[0]
        assert call["image"] == image_bytes
        assert call["reference_image"] == reference_bytes

    def test_generate_invalid_reference_base64_returns_422(
        self, server_client
    ):
        client, manager = server_client
        response = client.post(
            "/v2/models/qwen2-vl/generate",
            json={
                "text_input": "compare",
                "image": base64.b64encode(b"payload").decode("ascii"),
                "reference_image": "!!!not-base64!!!",
            },
        )
        assert response.status_code == 422
        assert "reference_image" in str(response.json().get("detail", ""))
        assert manager.calls == []

    def test_generate_without_reference_leaves_invocation_reference_free(
        self, server_client
    ):
        client, manager = server_client
        response = client.post(
            "/v2/models/qwen2-vl/generate",
            json={
                "text_input": "describe",
                "image": base64.b64encode(b"payload").decode("ascii"),
            },
        )
        assert response.status_code == 200
        assert len(manager.calls) == 1
        assert manager.calls[0]["reference_image"] is None

    def test_generate_stream_forwards_exact_decoded_reference_bytes(
        self, server_client
    ):
        client, manager = server_client
        image_bytes = b"stream input \x00"
        reference_bytes = b"stream reference \x7f"
        response = client.post(
            "/v2/models/qwen2-vl/generate_stream",
            json={
                "text_input": "compare",
                "image": base64.b64encode(image_bytes).decode("ascii"),
                "reference_image": base64.b64encode(reference_bytes).decode(
                    "ascii"
                ),
            },
        )
        assert response.status_code == 200
        assert len(manager.calls) == 1
        assert manager.calls[0]["image"] == image_bytes
        assert manager.calls[0]["reference_image"] == reference_bytes

    def test_generate_stream_invalid_reference_base64_returns_422(
        self, server_client
    ):
        client, manager = server_client
        response = client.post(
            "/v2/models/qwen2-vl/generate_stream",
            json={
                "text_input": "compare",
                "reference_image": "%%%bad%%%",
            },
        )
        assert response.status_code == 422
        assert "reference_image" in str(response.json().get("detail", ""))
        assert manager.calls == []
