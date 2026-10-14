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
- ``limit_mm_per_prompt`` as an AUTHORED engine-args field: the value in
  model.json reaches the engine factory verbatim, and when model.json
  omits the key NOTHING is injected — the engine falls back to vLLM's own
  one-image default (Requirement 6.6, as repointed below).
- Two-image prompt construction for a model authored for two images:
  input image placed before the reference in both the templated text and
  ``multi_modal_data["image"]``; a model authored for one image refuses
  the reference request rather than dropping it; the single-image prompt
  is unchanged; an undecodable reference raises :class:`GenerationError`
  naming the reference before the engine is invoked (Requirements 6.1,
  6.2, 6.5).
- Triton generate-extension server reference pass-through: a valid
  base64 ``reference_image`` is decoded and forwarded to the manager;
  invalid base64 maps to 422 naming the field; an absent field leaves
  the manager invocation reference-free (Requirement 5.5).

**CONSCIOUSLY REPOINTED** by `jp6-vllm-kv-cache-oom-regression` (design
Decision 1, spec task 3.6 OUTCOME "Collision B"). Two tests in this file
pinned the device-side default `engine_args.setdefault(
"limit_mm_per_prompt", {"image": 2})` in
`src/backend/vllm_runtime/manager.py` — that unbudgeted default IS defect
1.4 of that spec (it doubled the images a vision-language engine must
profile for, inside an unchanged `gpu_memory_utilization = 0.4` budget
whose ONE-image activation peak was already 4.92 GiB of 11.98 GiB) and
requirement 2.4 forbids it. Task 3.6 removed it, so these two tests could
not coexist with the fix. They are repointed, **not weakened**: each still
proves the capability it was written for — the two-image reference
guarantee of this feature — against the new contract, where the capability
comes from an AUTHORED, SIZED `limit_mm_per_prompt = {"image": 2}` in the
model's engine configuration (staged verbatim into model.json, sized by
the publish-time Fit_Check) instead of from a device-side injection. The
old names and assertions are recorded VERBATIM in comment blocks adjacent
to each repointed test.

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


TWO_IMAGE_MODEL_JSON = json.dumps({"limit_mm_per_prompt": {"image": 2}})
"""An engine configuration AUTHORED (and, at publish time, sized) for
two-image anomaly-reference generation — the post-Decision-1 home of the
multimodal limit, staged verbatim into ``model.json``."""


def _multimodal_manager(tmp_path, engine, model_name="qwen2-vl",
                        model_json="{}"):
    """A manager with one multimodal fake engine loaded READY.

    ``model_json`` defaults to the bare repository (no authored
    ``limit_mm_per_prompt``, i.e. vLLM's own one-image default). Pass
    :data:`TWO_IMAGE_MODEL_JSON` for a model authored for two images."""
    manager = VllmRuntimeManager(
        model_dir=tmp_path,
        engine_factory=lambda engine_args: engine,
        sampling_params_factory=dict,
    )
    _stage_repository(tmp_path, model_name, model_json=model_json)
    status = asyncio.run(manager.load(model_name))
    assert status.state is ModelState.READY
    return manager


# --- limit_mm_per_prompt authoring (Requirement 6.6) -------------------------


class TestLimitMmPerPromptAuthoring:
    # CONSCIOUS REPOINT (jp6-vllm-kv-cache-oom-regression, design Decision 1;
    # task 3.6 OUTCOME "Collision B"): this test was
    # ``test_absent_model_json_value_is_defaulted_before_the_factory`` and
    # pinned the exact defect (1.4 of that bugfix, forbidden by its
    # requirement 2.4) — an absent model.json key being forced to
    # {"image": 2} device-side, invisible to every sizing surface by
    # construction, doubling the images a vision-language engine profiles
    # for inside an unchanged gpu_memory_utilization = 0.4 budget. It is the
    # exact inverse of that spec's exploration case 4. Its pre-fix
    # assertions, VERBATIM:
    #
    #     def test_absent_model_json_value_is_defaulted_before_the_factory(
    #         self, tmp_path
    #     ):
    #         captured = {}
    #
    #         def factory(engine_args):
    #             captured.update(engine_args)
    #             return _FakeEngine()
    #
    #         manager = VllmRuntimeManager(
    #             model_dir=tmp_path,
    #             engine_factory=factory,
    #             sampling_params_factory=dict,
    #         )
    #         _stage_repository(tmp_path, "qwen2-vl", model_json="{}")
    #         status = asyncio.run(manager.load("qwen2-vl"))
    #         assert status.state is ModelState.READY
    #         assert captured["limit_mm_per_prompt"] == {"image": 2}
    #         # The tracked engine args carry the applied default too.
    #         assert manager.engine_args("qwen2-vl")["limit_mm_per_prompt"] == {
    #             "image": 2
    #         }
    #
    # The capability being pinned — "a two-image model reaches the engine
    # with room for two images per prompt" — is NOT weakened: it is asserted
    # below against the AUTHORED value, which is the real
    # vlm-anomaly-reference-parity guarantee (the model is authored and
    # sized for two images). The second half of the test adds the new
    # invariant that replaced the injection.
    def test_authored_two_image_value_reaches_the_factory_verbatim(
        self, tmp_path
    ):
        """A model AUTHORED for two images carries
        ``limit_mm_per_prompt = {"image": 2}`` in its staged model.json, and
        that value reaches the engine factory verbatim — so the two-image
        anomaly-reference capability still holds end to end (Requirement
        6.6). And when model.json OMITS the key, NOTHING is injected: the
        engine gets no ``limit_mm_per_prompt`` at all and falls back to
        vLLM's own one-image default, which is what restores the 1.0.59
        memory demand (jp6-vllm-kv-cache-oom-regression Decision 1)."""
        authored = {}

        def authored_factory(engine_args):
            authored.update(engine_args)
            return _FakeEngine()

        authored_manager = VllmRuntimeManager(
            model_dir=tmp_path / "authored",
            engine_factory=authored_factory,
            sampling_params_factory=dict,
        )
        _stage_repository(
            tmp_path / "authored", "qwen2-vl",
            model_json=TWO_IMAGE_MODEL_JSON,
        )
        status = asyncio.run(authored_manager.load("qwen2-vl"))
        assert status.state is ModelState.READY
        # The authored two-image capability reaches the engine verbatim.
        assert authored["limit_mm_per_prompt"] == {"image": 2}
        # And the tracked engine args carry it (this is what the two-image
        # prompt guard reads to admit a reference request).
        assert authored_manager.engine_args("qwen2-vl")[
            "limit_mm_per_prompt"
        ] == {"image": 2}

        # NEW INVARIANT (defect 1.4 / requirement 2.4): an absent key is
        # NOT defaulted device-side. Nothing is injected anywhere.
        bare = {}

        def bare_factory(engine_args):
            bare.update(engine_args)
            return _FakeEngine()

        bare_manager = VllmRuntimeManager(
            model_dir=tmp_path / "bare",
            engine_factory=bare_factory,
            sampling_params_factory=dict,
        )
        _stage_repository(tmp_path / "bare", "qwen2-vl", model_json="{}")
        status = asyncio.run(bare_manager.load("qwen2-vl"))
        assert status.state is ModelState.READY
        assert "limit_mm_per_prompt" not in bare
        assert "limit_mm_per_prompt" not in bare_manager.engine_args(
            "qwen2-vl"
        )

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
    # CONSCIOUS REPOINT (jp6-vllm-kv-cache-oom-regression, design Decision 1;
    # task 3.6 OUTCOME "Collision B"): this test staged ``model.json = {}``
    # and expected a two-image prompt to build anyway — it depended on the
    # removed device-side ``setdefault("limit_mm_per_prompt", {"image": 2})``
    # (defect 1.4, forbidden by requirement 2.4) and is the exact inverse of
    # that spec's exploration case 9, which now raises ``GenerationError``
    # with a remediation. Its pre-fix body, VERBATIM — the manager was built
    # by ``_multimodal_manager(tmp_path, engine)``, whose ``model_json``
    # defaulted to ``"{}"``:
    #
    #     def test_reference_builds_ordered_two_image_prompt(self, tmp_path):
    #         engine = _FakeEngine(
    #             model_config=_ModelConfig(["Qwen2VLForConditionalGeneration"])
    #         )
    #         manager = _multimodal_manager(tmp_path, engine)
    #         text = asyncio.run(
    #             manager.generate(
    #                 "qwen2-vl",
    #                 "compare the two",
    #                 image=_jpeg_bytes(),
    #                 reference_image=_jpeg_bytes((30, 200, 30)),
    #             )
    #         )
    #         assert text == "generated"
    #         assert len(engine.calls) == 1
    #         prompt = engine.calls[0]
    #         assert isinstance(prompt, dict)
    #         # Input label before reference label in the templated text.
    #         templated = prompt["prompt"]
    #         assert "Input image:" in templated
    #         assert "Reference image:" in templated
    #         assert templated.index("Input image:") < templated.index(
    #             "Reference image:"
    #         )
    #         assert "compare the two" in templated
    #         # Two decoded images, input first.
    #         images = prompt["multi_modal_data"]["image"]
    #         assert isinstance(images, list)
    #         assert len(images) == 2
    #
    # NOTHING is weakened: every assertion above is kept BYTE FOR BYTE below
    # (the ordered labels, the prompt text, the two-element image list, the
    # single engine call, the returned text). The ONLY change is that the
    # capability is now unlocked by the model's AUTHORED engine
    # configuration instead of a device-side injection. The companion
    # assertion at the end covers the other side of Decision 1: a
    # one-image-authored model refuses the reference request loudly rather
    # than silently dropping the reference and answering a different
    # question.
    def test_reference_builds_ordered_two_image_prompt(self, tmp_path):
        """A model AUTHORED for two images
        (``limit_mm_per_prompt = {"image": 2}`` in the staged model.json,
        sized as such by the publish-time Fit_Check) builds the full ordered
        two-image prompt — input before reference in both the templated text
        and ``multi_modal_data["image"]`` (Requirements 6.1, 6.2). A model
        authored for ONE image raises :class:`GenerationError` naming the
        effective limit and the remediation, before the engine is invoked
        (jp6-vllm-kv-cache-oom-regression Decision 1 / requirement 2.4;
        preservation 3.9 keeps the feature for models sized for it)."""
        engine = _FakeEngine(
            model_config=_ModelConfig(["Qwen2VLForConditionalGeneration"])
        )
        manager = _multimodal_manager(
            tmp_path / "authored", engine,
            model_json=TWO_IMAGE_MODEL_JSON,
        )
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

        # COMPANION (the new contract's other half): the same request
        # against a model authored for one image is REFUSED, not degraded.
        one_image_engine = _FakeEngine(
            model_config=_ModelConfig(["Qwen2VLForConditionalGeneration"])
        )
        one_image_manager = _multimodal_manager(
            tmp_path / "one-image", one_image_engine, model_json="{}",
        )
        with pytest.raises(GenerationError) as excinfo:
            asyncio.run(
                one_image_manager.generate(
                    "qwen2-vl",
                    "compare the two",
                    image=_jpeg_bytes(),
                    reference_image=_jpeg_bytes((30, 200, 30)),
                )
            )
        reason = excinfo.value.reason
        assert excinfo.value.model_name == "qwen2-vl"
        # Names the limit that blocks the request ...
        assert "limit_mm_per_prompt.image = 1" in reason
        # ... states the reference is not silently dropped ...
        assert "NOT silently dropped" in reason
        # ... and carries the remediation.
        assert "limit_mm_per_prompt.image = 2" in reason
        assert "re-package and re-publish" in reason
        # Nothing reached the engine: no degraded one-image answer.
        assert one_image_engine.calls == []

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
