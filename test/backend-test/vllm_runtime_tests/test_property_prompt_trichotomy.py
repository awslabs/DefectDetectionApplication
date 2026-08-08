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
"""Property test for the vLLM runtime manager's engine-prompt construction
(task 5.3).

**Feature: edge-vlm-image-inference, Property 8: Runtime prompt-construction
trichotomy**

*For any* prompt, sampling parameters, and optional image bytes, the
manager's engine invocation SHALL be: the bare prompt string when image is
``None`` (byte-identical to pre-feature); a prompt dict whose
``multi_modal_data`` carries the decoded image and whose text contains the
model's image placeholder when the model is multimodal; and the bare prompt
string (with a logged warning, ``image_supported`` reporting ``False``) when
the model is not multimodal.

**Validates: Requirements 4.1, 4.3, 4.4, 6.3**

The decode-failure edge (Requirement 4.7) also rides the generator: image
bytes that cannot be decoded into an image raise :class:`GenerationError`
naming the decode failure with the engine invoked zero times.

Runs with the hypothesis profiles registered in the backend-test root
conftest (``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
No real vLLM/GPU: the manager is driven through its injectable
``engine_factory``/``sampling_params_factory`` with a fake engine capturing
the prompt argument and stubbed model configs.
"""
import asyncio
import io
import logging
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from vllm_runtime.manager import (
    GenerationError,
    ModelState,
    VllmRuntimeManager,
)

_MODEL_NAME = "model-under-test"

#: The image placeholder token every multimodal prompt text must carry —
#: both the Qwen VL literal fallback and the fake chat template emit it.
_IMAGE_PLACEHOLDER = "<|image_pad|>"


# --- fakes -----------------------------------------------------------------


class _FakeTokenizer:
    """Tokenizer stub with a usable chat template that renders the image
    placeholder plus the message's text content, mirroring the Qwen VL
    processor template shape."""

    chat_template = "{# fake qwen-vl template #}"

    def apply_chat_template(self, messages, tokenize=False,
                            add_generation_prompt=True):
        text = ""
        for item in messages[0]["content"]:
            if item.get("type") == "text":
                text = item.get("text", "")
        return (
            "<|im_start|>user\n<|vision_start|><|image_pad|><|vision_end|>"
            + text
            + "<|im_end|>\n<|im_start|>assistant\n"
        )


class _FakeEngine:
    """Fake ``AsyncLLMEngine`` surface: captures every ``generate`` call's
    prompt/sampling-params arguments and yields one request output."""

    def __init__(self, model_config, tokenizer=None):
        self.model_config = model_config
        self._tokenizer = tokenizer
        self.calls = []

    def get_tokenizer(self):
        return self._tokenizer

    async def generate(self, prompt, sampling_params, request_id):
        self.calls.append((prompt, sampling_params, request_id))
        yield SimpleNamespace(outputs=[SimpleNamespace(text="generated")])


# --- staging / manager construction ----------------------------------------


def _stage_repository(model_dir: Path, model_name: str) -> None:
    """Minimal valid Triton_vLLM_Repository: config.pbtxt declaring the
    vllm backend and a 1/model.json engine-args object."""
    repo = model_dir / model_name
    (repo / "1").mkdir(parents=True)
    (repo / "config.pbtxt").write_text('backend: "vllm"\n')
    (repo / "1" / "model.json").write_text('{"model": "/fake/weights"}')


def _make_ready_manager(model_dir: Path, engine: _FakeEngine):
    """Stage a repository and load it into the fake engine so the model
    is READY, exactly as production loads do."""
    _stage_repository(model_dir, _MODEL_NAME)
    manager = VllmRuntimeManager(
        model_dir=model_dir,
        engine_factory=lambda engine_args: engine,
        sampling_params_factory=lambda params: dict(params),
    )
    status = asyncio.run(manager.load(_MODEL_NAME))
    assert status.state is ModelState.READY
    return manager


# --- strategies --------------------------------------------------------------

_prompts = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)),
    max_size=80,
)

_sampling_params = st.one_of(
    st.none(),
    st.fixed_dictionaries(
        {
            "max_tokens": st.integers(min_value=1, max_value=512),
            "temperature": st.floats(
                min_value=0.0, max_value=2.0, allow_nan=False
            ),
        }
    ),
)

#: Multimodal model-config stubs: vLLM's own boolean flag, or the
#: hf_config architectures list carrying a Qwen VL family (no flag).
_multimodal_configs = st.sampled_from(["flag_true", "qwen2_vl", "qwen2_5_vl"])

#: Non-multimodal stubs: explicit False flag, or a text-only architecture.
_text_only_configs = st.sampled_from(["flag_false", "opt_arch"])


def _build_model_config(kind):
    if kind == "flag_true":
        return SimpleNamespace(is_multimodal_model=True)
    if kind == "qwen2_vl":
        return SimpleNamespace(
            hf_config=SimpleNamespace(
                architectures=["Qwen2VLForConditionalGeneration"]
            )
        )
    if kind == "qwen2_5_vl":
        return SimpleNamespace(
            hf_config=SimpleNamespace(
                architectures=["Qwen2_5_VLForConditionalGeneration"]
            )
        )
    if kind == "flag_false":
        return SimpleNamespace(is_multimodal_model=False)
    if kind == "opt_arch":
        return SimpleNamespace(
            hf_config=SimpleNamespace(architectures=["OPTForCausalLM"])
        )
    raise AssertionError("unknown config kind: {}".format(kind))


#: ``None`` → Qwen VL literal fallback; tokenizer → chat-template path.
#: Both render the image placeholder token.
_tokenizers = st.sampled_from(["none", "template"])


def _build_tokenizer(kind):
    return _FakeTokenizer() if kind == "template" else None


@st.composite
def _valid_image_bytes(draw):
    """Tiny real images (1x1..4x4 PNG/JPEG) rendered to bytes via PIL."""
    width = draw(st.integers(min_value=1, max_value=4))
    height = draw(st.integers(min_value=1, max_value=4))
    fmt = draw(st.sampled_from(["PNG", "JPEG"]))
    color = draw(
        st.tuples(
            st.integers(0, 255), st.integers(0, 255), st.integers(0, 255)
        )
    )
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format=fmt)
    return buffer.getvalue()


#: Bytes guaranteed not to decode as any image (no format magic), plus the
#: empty payload.
_non_image_bytes = st.one_of(
    st.just(b""),
    st.binary(max_size=64).map(lambda tail: b"not-an-image:" + tail),
)


@st.composite
def _scenarios(draw):
    """One trichotomy scenario: the model-config stub, tokenizer shape,
    image payload, and the expected engine-invocation branch."""
    branch = draw(
        st.sampled_from(
            ["no_image", "mm_image", "non_mm_image", "mm_decode_failure"]
        )
    )
    tokenizer_kind = draw(_tokenizers)
    if branch == "no_image":
        config_kind = draw(st.one_of(_multimodal_configs, _text_only_configs))
        image = None
    elif branch == "mm_image":
        config_kind = draw(_multimodal_configs)
        image = draw(_valid_image_bytes())
    elif branch == "non_mm_image":
        config_kind = draw(_text_only_configs)
        # A text-only model ignores the image whether or not it decodes.
        image = draw(st.one_of(_valid_image_bytes(), _non_image_bytes))
    else:  # mm_decode_failure
        config_kind = draw(_multimodal_configs)
        image = draw(_non_image_bytes)
    return {
        "branch": branch,
        "config_kind": config_kind,
        "tokenizer_kind": tokenizer_kind,
        "image": image,
    }


# --- the property ------------------------------------------------------------


class _RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@given(scenario=_scenarios(), prompt=_prompts, sampling=_sampling_params)
@settings(deadline=None)
def test_prompt_construction_trichotomy(scenario, prompt, sampling):
    """**Feature: edge-vlm-image-inference, Property 8: Runtime
    prompt-construction trichotomy**

    **Validates: Requirements 4.1, 4.3, 4.4, 6.3**
    """
    engine = _FakeEngine(
        model_config=_build_model_config(scenario["config_kind"]),
        tokenizer=_build_tokenizer(scenario["tokenizer_kind"]),
    )
    model_dir = Path(tempfile.mkdtemp(prefix="vllm-trichotomy-"))
    handler = _RecordingHandler()
    manager_logger = logging.getLogger("vllm_runtime.manager")
    manager_logger.addHandler(handler)
    try:
        manager = _make_ready_manager(model_dir, engine)
        branch = scenario["branch"]
        image = scenario["image"]

        if branch == "mm_decode_failure":
            # Requirement 4.7 edge: undecodable image bytes raise the
            # existing generation error naming the decode failure, with
            # the engine invoked zero times.
            with pytest.raises(GenerationError) as excinfo:
                asyncio.run(
                    manager.generate(_MODEL_NAME, prompt, sampling, image)
                )
            assert "decode" in str(excinfo.value)
            assert engine.calls == []
            return

        text = asyncio.run(
            manager.generate(_MODEL_NAME, prompt, sampling, image)
        )
        assert text == "generated"
        assert len(engine.calls) == 1
        engine_prompt, engine_params, _request_id = engine.calls[0]

        # Sampling params reach the engine through the injected factory
        # unchanged in every branch.
        assert engine_params == dict(sampling or {})

        if branch == "no_image":
            # No image → the bare prompt string, byte-identical to the
            # pre-feature invocation (Requirements 4.4, 6.3).
            assert isinstance(engine_prompt, str)
            assert engine_prompt == prompt
        elif branch == "mm_image":
            # Image + multimodal model → a prompt dict whose
            # multi_modal_data carries the decoded PIL image and whose
            # text bears the image placeholder tokens (Requirement 4.1).
            assert isinstance(engine_prompt, dict)
            assert set(engine_prompt.keys()) == {
                "prompt", "multi_modal_data"
            }
            assert _IMAGE_PLACEHOLDER in engine_prompt["prompt"]
            assert prompt in engine_prompt["prompt"]
            pil_image = engine_prompt["multi_modal_data"]["image"]
            assert isinstance(pil_image, Image.Image)
            assert (
                pil_image.size
                == Image.open(io.BytesIO(image)).size
            )
            assert manager.image_supported(_MODEL_NAME) is True
        else:  # non_mm_image
            # Image + non-multimodal model → the bare prompt string with
            # a logged warning and image_supported False (Requirement 4.3).
            assert isinstance(engine_prompt, str)
            assert engine_prompt == prompt
            assert manager.image_supported(_MODEL_NAME) is False
            warnings = [
                record
                for record in handler.records
                if record.levelno == logging.WARNING
                and "not multimodal" in record.getMessage()
            ]
            assert len(warnings) == 1
            assert _MODEL_NAME in warnings[0].getMessage()
    finally:
        manager_logger.removeHandler(handler)
        shutil.rmtree(model_dir, ignore_errors=True)
