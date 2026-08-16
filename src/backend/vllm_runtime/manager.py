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
"""``VllmRuntimeManager`` — the companion vLLM runtime that owns every
vLLM model on the device (design section 9; Requirements 4.1, 4.6, 4.7,
8.8, 8.9).

A load request names a staged Triton_vLLM_Repository under
:data:`~vllm_runtime.constants.VLLM_MODEL_DIR`; the manager validates it
(``config.pbtxt`` declaring ``backend: "vllm"``, ``1/model.json`` parsed
into vLLM ``AsyncEngineArgs``) and creates one ``AsyncLLMEngine`` per
model. Each model moves through the per-model state machine

    STAGED -> LOADING -> READY | FAILED(reason)

with ``UNKNOWN`` the answer for names never staged, and ``unload`` freeing
the engine from any state. Failures are isolated: one model's load or
serve error (including GPU out-of-memory) transitions only that model to
FAILED — logged with the model name and the backend error — and never
touches another engine (4.6, 8.9). The embedded vision Triton scans its
own separate repository directory and is untouched by anything here (8.8).

vLLM is **not** imported at module import time: the ``vllm`` package only
exists on vLLM-capable images (JetPack 6 / JetPack 7), so the import happens lazily
inside the default engine/sampling-params factories. Both factories are
injectable, which is also how tests drive the manager with a fake
``AsyncLLMEngine`` and no GPU.
"""
import gc
import inspect
import logging
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Mapping, Optional, Union

from vllm_runtime.constants import VLLM_MODEL_DIR
from vllm_runtime.repository import (
    CONFIG_PBTXT_RELATIVE_PATH,
    RepositoryValidationError,
    parse_repository,
)

logger = logging.getLogger(__name__)

#: HF architectures known to accept image input. Qwen2-VL and Qwen2.5-VL
#: are the minimum supported vision-language families (edge-vlm-image-
#: inference Requirements 4.2, 4.5); vLLM's own ``is_multimodal_model``
#: flag is preferred when the model config exposes it.
MULTIMODAL_ARCHITECTURES = frozenset(
    {
        "Qwen2VLForConditionalGeneration",
        "Qwen2_5_VLForConditionalGeneration",
    }
)

#: Documented Qwen VL chat form, used when the model tokenizer offers no
#: usable chat template (edge-vlm-image-inference design, section 4).
_QWEN_VL_PROMPT_FALLBACK = (
    "<|im_start|>user\n<|vision_start|><|image_pad|><|vision_end|>"
    "{prompt}<|im_end|>\n<|im_start|>assistant\n"
)

#: Two-image variant of the Qwen VL literal chat form: one image pad per
#: image, labeled like the Bedrock content blocks so prompts written for
#: Bedrock port over (vlm-anomaly-reference-parity Requirement 6.1).
_QWEN_VL_TWO_IMAGE_PROMPT_FALLBACK = (
    "<|im_start|>user\nInput image: "
    "<|vision_start|><|image_pad|><|vision_end|>\n"
    "Reference image: <|vision_start|><|image_pad|><|vision_end|>\n"
    "{prompt}<|im_end|>\n<|im_start|>assistant\n"
)


class ModelState(str, Enum):
    """Per-model serving states (design: runtime model state machine)."""

    #: A validated repository exists but no engine has been created yet.
    STAGED = "STAGED"
    #: Engine creation is in progress (Requirement 4.7).
    LOADING = "LOADING"
    #: The engine is serving; generate calls are accepted.
    READY = "READY"
    #: Load or serve failed; the backend reason is retained (4.6).
    FAILED = "FAILED"
    #: The name was never staged on this device.
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ModelStatus:
    """A model's observable state, with the retained backend failure
    reason when the state is FAILED."""

    state: ModelState
    reason: Optional[str] = None


#: Status returned for names the device has never seen.
UNKNOWN_STATUS = ModelStatus(ModelState.UNKNOWN)


class VllmRuntimeError(Exception):
    """Base error for the companion vLLM runtime."""


class ModelUnavailableError(VllmRuntimeError):
    """A generate call named a model that is not READY. Carries the
    model's actual status so callers (the Text_Generation_API) can
    distinguish loading / failed / unknown (Requirement 5.5)."""

    def __init__(self, model_name: str, status: ModelStatus):
        self.model_name = model_name
        self.status = status
        message = "model '{}' is not ready (state: {})".format(
            model_name, status.state.value
        )
        if status.reason:
            message += ": {}".format(status.reason)
        super().__init__(message)


class GenerationError(VllmRuntimeError):
    """The engine reported an error while generating. Carries the model
    name and the backend reason (Requirement 4.6)."""

    def __init__(self, model_name: str, reason: str):
        self.model_name = model_name
        self.reason = reason
        super().__init__(
            "generation failed for model '{}': {}".format(model_name, reason)
        )


@dataclass
class _ManagedModel:
    """Book-keeping for one model owned by the manager."""

    status: ModelStatus
    engine: Any = None
    engine_args: Dict[str, Any] = field(default_factory=dict)
    #: Cached multimodal-capability answer for the loaded engine
    #: (``None`` until first queried; edge-vlm-image-inference 4.2).
    multimodal: Optional[bool] = None


def _default_engine_factory(engine_args: Mapping[str, Any]) -> Any:
    """Build a real ``AsyncLLMEngine`` from parsed model.json engine
    arguments. ``vllm`` is imported here — and only here — so the module
    imports cleanly on images without the vLLM wheel."""
    from vllm import AsyncEngineArgs
    from vllm.engine.async_llm_engine import AsyncLLMEngine

    args = AsyncEngineArgs(**dict(engine_args))
    return AsyncLLMEngine.from_engine_args(args)


def _default_sampling_params_factory(params: Mapping[str, Any]) -> Any:
    """Build real ``vllm.SamplingParams`` from a plain mapping; lazy
    import for the same reason as the engine factory."""
    from vllm import SamplingParams

    return SamplingParams(**dict(params))


class VllmRuntimeManager:
    """Owns every vLLM model on the device.

    ``engine_factory`` maps parsed engine arguments to an engine exposing
    the ``AsyncLLMEngine`` surface used here (``generate(prompt,
    sampling_params, request_id)`` yielding request outputs, optional
    ``shutdown_background_loop()``, optional ``errored``);
    ``sampling_params_factory`` maps a plain parameter mapping to the
    sampling-params object the engine expects. Both default to the real
    vLLM implementations (imported lazily) and are injectable so tests
    run with fakes and no GPU. All state access is lock-guarded, so the
    manager is safe to touch from the HTTP server's event loop and from
    status-reporting threads alike.
    """

    def __init__(
        self,
        model_dir: Union[str, Path] = VLLM_MODEL_DIR,
        engine_factory: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        sampling_params_factory: Optional[Callable[[Mapping[str, Any]], Any]] = None,
    ):
        self.model_dir = Path(model_dir)
        self._engine_factory = engine_factory or _default_engine_factory
        self._sampling_params_factory = (
            sampling_params_factory or _default_sampling_params_factory
        )
        self._lock = threading.Lock()
        self._models: Dict[str, _ManagedModel] = {}

    # --- inspection --------------------------------------------------------

    def state(self, model_name: str) -> ModelStatus:
        """The model's current status: its tracked state when the manager
        knows it, STAGED when a repository directory exists on disk but no
        load was requested yet, UNKNOWN for never-staged names."""
        with self._lock:
            entry = self._models.get(model_name)
            if entry is not None:
                return entry.status
        if self._repository_staged(model_name):
            return ModelStatus(ModelState.STAGED)
        return UNKNOWN_STATUS

    def list_models(self) -> Dict[str, ModelStatus]:
        """Every model the manager tracks plus every repository staged on
        disk, with its current status — the feed for the device model
        status mechanisms (Requirements 4.6, 4.7)."""
        with self._lock:
            statuses = {name: entry.status for name, entry in self._models.items()}
        if self.model_dir.is_dir():
            for child in sorted(self.model_dir.iterdir()):
                if child.name not in statuses and self._repository_staged(child.name):
                    statuses[child.name] = ModelStatus(ModelState.STAGED)
        return statuses

    def engine_args(self, model_name: str) -> Dict[str, Any]:
        """The parsed model.json engine arguments of a tracked model
        (e.g. ``max_model_len`` for request validation); empty for
        untracked names."""
        with self._lock:
            entry = self._models.get(model_name)
            return dict(entry.engine_args) if entry is not None else {}

    def _repository_staged(self, model_name: str) -> bool:
        return (
            self.model_dir / model_name / CONFIG_PBTXT_RELATIVE_PATH
        ).is_file()

    # --- lifecycle ---------------------------------------------------------

    async def load(self, model_name: str) -> ModelStatus:
        """Load the staged repository ``{model_dir}/{model_name}`` into a
        new engine: STAGED -> LOADING -> READY | FAILED(reason).

        Idempotent for READY models and a no-op while a load is already in
        flight (the current status is returned). Any failure — repository
        validation, engine construction, GPU out-of-memory — transitions
        only this model to FAILED with the backend reason retained and
        logged with the model name; every other engine is untouched
        (Requirements 4.6, 4.7, 8.9).
        """
        with self._lock:
            entry = self._models.get(model_name)
            if entry is not None and entry.status.state in (
                ModelState.LOADING,
                ModelState.READY,
            ):
                return entry.status
            # STAGED: a load was requested for this name.
            entry = _ManagedModel(status=ModelStatus(ModelState.STAGED))
            self._models[model_name] = entry

        try:
            engine_args = parse_repository(self.model_dir / model_name)
        except RepositoryValidationError as err:
            return self._fail(model_name, str(err))
        # vLLM's default caps images per prompt at 1; two-image reference
        # generation needs 2 (vlm-anomaly-reference-parity Requirement
        # 6.6). setdefault: an explicit model.json value wins unchanged,
        # and the arg is a standard EngineArgs field, harmless for
        # text-only models.
        engine_args.setdefault("limit_mm_per_prompt", {"image": 2})

        with self._lock:
            entry = self._models.get(model_name)
            if entry is None:  # unloaded mid-flight
                return UNKNOWN_STATUS
            entry.engine_args = dict(engine_args)
            entry.status = ModelStatus(ModelState.LOADING)
        logger.info("Loading vLLM model '%s'", model_name)

        try:
            engine = self._engine_factory(engine_args)
            if inspect.isawaitable(engine):
                engine = await engine
        except Exception as err:  # noqa: BLE001 - failure isolation (4.6, 8.9)
            return self._fail(model_name, str(err))

        with self._lock:
            entry = self._models.get(model_name)
            if entry is None:  # unloaded mid-flight: free the fresh engine
                self._shutdown_engine(model_name, engine)
                return UNKNOWN_STATUS
            entry.engine = engine
            entry.status = ModelStatus(ModelState.READY)
        logger.info("vLLM model '%s' is READY", model_name)
        return ModelStatus(ModelState.READY)

    def unload(self, model_name: str) -> bool:
        """Remove the model from any state, shutting its engine down and
        freeing GPU memory. Returns True when the manager was tracking the
        model."""
        with self._lock:
            entry = self._models.pop(model_name, None)
        if entry is None:
            return False
        if entry.engine is not None:
            self._shutdown_engine(model_name, entry.engine)
        self._reclaim_gpu_memory(model_name)
        logger.info("vLLM model '%s' unloaded", model_name)
        return True

    def _fail(self, model_name: str, reason: str) -> ModelStatus:
        """Transition one model to FAILED, retaining and logging the
        backend reason with the model name (Requirement 4.6). No other
        model is touched.

        A failed engine CONSTRUCTION (the GPU out-of-memory case) has no
        engine object to shut down, yet the aborted initialization can
        leave many GB of GPU allocations behind — observed on-device as a
        first-load OOM that keeps OOMing on every plain retry until an
        unload releases the memory. Reclaim it here so the next load
        attempt starts from a clean allocator state.
        """
        logger.error("vLLM model '%s' failed: %s", model_name, reason)
        status = ModelStatus(ModelState.FAILED, reason=reason)
        with self._lock:
            entry = self._models.get(model_name)
            engine = entry.engine if entry is not None else None
            if entry is not None:
                entry.status = status
                entry.engine = None
        if engine is not None:
            self._shutdown_engine(model_name, engine)
        self._reclaim_gpu_memory(model_name)
        return status

    @staticmethod
    def _shutdown_engine(model_name: str, engine: Any) -> None:
        """Best-effort engine shutdown so GPU memory is released."""
        shutdown = getattr(engine, "shutdown_background_loop", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:  # noqa: BLE001 - unload must always succeed
                logger.exception(
                    "Error shutting down the engine of vLLM model '%s'",
                    model_name,
                )

    @staticmethod
    def _reclaim_gpu_memory(model_name: str) -> None:
        """Best-effort release of GPU memory left behind by a failed or
        shut-down engine: drop unreachable Python objects, then return
        cached CUDA blocks to the driver. ``torch`` is imported lazily
        (it only exists on vLLM-capable images) and every failure is
        swallowed — reclaim must never break unload/fail handling.

        Invariant: reclaim must never be the first CUDA touch in a
        process. The gate is ``torch.cuda.is_initialized()`` — a pure
        state read — never a driver-initializing probe like
        ``torch.cuda.is_available()``: such a probe in the parent
        backend process poisons every subsequently forked child
        (defect 1.3, spec vllm-jp7-engine-cuda-init), and on JP7/V1 the
        engine memory lives in the engine-core child anyway, so there
        is nothing for the parent to reclaim. ``empty_cache()`` is only
        meaningful when torch CUDA is already initialized in THIS
        process — exactly the JP6/V0 in-process engine case."""
        gc.collect()
        try:
            import torch
        except ImportError:
            return
        try:
            if torch.cuda.is_initialized():
                torch.cuda.empty_cache()
                logger.info(
                    "Reclaimed cached CUDA memory after unload/failure of "
                    "vLLM model '%s'", model_name,
                )
        except Exception:  # noqa: BLE001 - reclaim is strictly best-effort
            logger.exception(
                "Error reclaiming CUDA memory after vLLM model '%s'",
                model_name,
            )

    # --- inference ---------------------------------------------------------

    async def generate(
        self,
        model_name: str,
        prompt: str,
        sampling_params: Optional[Mapping[str, Any]] = None,
        image: Optional[bytes] = None,
        reference_image: Optional[bytes] = None,
    ) -> str:
        """Generate to completion and return the generated text.

        ``image`` optionally carries encoded image bytes for multimodal
        generation on vision-language models (edge-vlm-image-inference
        Requirements 4.1, 4.3, 4.4); ``reference_image`` optionally adds
        a second, reference image to the same prompt
        (vlm-anomaly-reference-parity Requirement 6.1). Text-only
        invocations are byte-identical to pre-feature behavior.

        Raises :class:`ModelUnavailableError` when the model is not READY
        (carrying its actual status) and :class:`GenerationError` when the
        engine reports a failure — logged with the model name and backend
        error, other models untouched (Requirements 4.6, 8.8).
        """
        final = None
        async for output in self._request(
            model_name, prompt, sampling_params, image, reference_image
        ):
            final = output
        text = self._output_text(final)
        if text is None:
            raise GenerationError(model_name, "engine produced no output")
        return text

    async def generate_stream(
        self,
        model_name: str,
        prompt: str,
        sampling_params: Optional[Mapping[str, Any]] = None,
        image: Optional[bytes] = None,
        reference_image: Optional[bytes] = None,
    ) -> AsyncIterator[str]:
        """Async iterator of incremental token text, in generation order.

        The engine yields cumulative request outputs; this yields only
        each step's new suffix. Errors surface as
        :class:`GenerationError` after already-yielded tokens (the caller
        decides how to signal them in-stream)."""
        previous = ""
        async for output in self._request(
            model_name, prompt, sampling_params, image, reference_image
        ):
            text = self._output_text(output)
            if text is None:
                continue
            delta = text[len(previous):]
            previous = text
            if delta:
                yield delta

    async def _request(
        self,
        model_name: str,
        prompt: str,
        sampling_params: Optional[Mapping[str, Any]],
        image: Optional[bytes] = None,
        reference_image: Optional[bytes] = None,
    ) -> AsyncIterator[Any]:
        """Shared generate/generate_stream core: READY-check, sampling
        params construction, engine invocation, failure isolation.

        Engine prompt trichotomy (edge-vlm-image-inference 4.1, 4.3, 4.4,
        6.3): no image → the bare prompt string exactly as pre-feature
        (a reference cannot arrive alone — the API rejects it — but an
        image-less request is defensively text-only); image + multimodal
        model → a prompt dict carrying the chat-templated text and
        ``multi_modal_data`` (one or two images,
        vlm-anomaly-reference-parity 6.1, 6.2); image + text-only model
        → logged warning, bare prompt string.
        """
        engine = self._ready_engine(model_name)
        if image is None:
            engine_prompt: Any = prompt
        elif self._is_multimodal(model_name):
            engine_prompt = self._build_multimodal_prompt(
                model_name, prompt, image, reference_image
            )
        else:
            logger.warning(
                "vLLM model '%s' is not multimodal; ignoring the supplied "
                "image and generating text-only",
                model_name,
            )
            engine_prompt = prompt
        params = self._sampling_params_factory(dict(sampling_params or {}))
        request_id = uuid.uuid4().hex
        try:
            stream = engine.generate(engine_prompt, params, request_id)
            if inspect.isawaitable(stream):
                stream = await stream
            async for output in stream:
                yield output
        except Exception as err:  # noqa: BLE001 - serve-failure isolation (4.6)
            logger.error(
                "vLLM backend error serving model '%s': %s", model_name, err
            )
            # A dead engine can serve nothing further: mark this model —
            # and only this model — FAILED. A per-request error with a
            # healthy engine leaves the model READY for the caller's
            # retry policy (5.6).
            if getattr(engine, "errored", False):
                self._fail(model_name, str(err))
            raise GenerationError(model_name, str(err)) from err

    def _ready_engine(self, model_name: str) -> Any:
        with self._lock:
            entry = self._models.get(model_name)
            if entry is not None and entry.status.state is ModelState.READY:
                return entry.engine
            status = entry.status if entry is not None else None
        if status is None:
            status = (
                ModelStatus(ModelState.STAGED)
                if self._repository_staged(model_name)
                else UNKNOWN_STATUS
            )
        raise ModelUnavailableError(model_name, status)

    # --- multimodal support (edge-vlm-image-inference) ---------------------

    def image_supported(self, model_name: str) -> bool:
        """Whether the loaded model accepts image input — the public
        capability answer callers (the Text_Generation_API) use for
        ``image_used`` reporting (Requirements 4.2, 4.3). ``False`` for
        models that are not loaded."""
        return self._is_multimodal(model_name)

    def _is_multimodal(self, model_name: str) -> bool:
        """Whether the loaded engine serves a vision-language model,
        determined from the model's configuration alone (no per-model
        operator settings — Requirement 4.2) and cached per loaded model
        (the cache lives on the ``_ManagedModel`` entry, so a reload
        re-detects)."""
        with self._lock:
            entry = self._models.get(model_name)
            if entry is None:
                return False
            if entry.multimodal is not None:
                return entry.multimodal
            engine = entry.engine
        result = self._detect_multimodal(engine)
        with self._lock:
            entry = self._models.get(model_name)
            if entry is not None:
                entry.multimodal = result
        return result

    @staticmethod
    def _detect_multimodal(engine: Any) -> bool:
        """Inspect an engine's model config: prefer vLLM's own
        ``ModelConfig.is_multimodal_model`` flag where available, fall
        back to the hf_config architectures list (Qwen2-VL / Qwen2.5-VL
        at minimum — Requirement 4.5)."""
        if engine is None:
            return False
        inner = getattr(engine, "engine", None) or engine
        model_config = getattr(inner, "model_config", None)
        if model_config is None:
            model_config = getattr(engine, "model_config", None)
        if model_config is None:
            return False
        try:
            flag = getattr(model_config, "is_multimodal_model", None)
        except Exception:  # noqa: BLE001 - property access on exotic configs
            flag = None
        if isinstance(flag, bool):
            return flag
        hf_config = getattr(model_config, "hf_config", None)
        architectures = getattr(hf_config, "architectures", None) or []
        return any(arch in MULTIMODAL_ARCHITECTURES for arch in architectures)

    def _build_multimodal_prompt(
        self,
        model_name: str,
        prompt: str,
        image_bytes: bytes,
        reference_bytes: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """Build the vLLM multimodal engine prompt: chat-templated text
        containing the model's image placeholder tokens plus
        ``multi_modal_data`` carrying the decoded image(s) (Requirement
        4.1; vlm-anomaly-reference-parity 6.1, 6.2).

        Without ``reference_bytes`` the message and return value are
        byte-identical to the pre-reference single-image form. With a
        reference, the content labels and places the input image first
        and the reference second — mirroring Bedrock's content blocks —
        and ``multi_modal_data["image"]`` is the two-element list in
        that order (vLLM's standard multi-image form).

        Undecodable image bytes raise :class:`GenerationError` naming the
        image (input or reference) decoding failure, before the engine is
        ever invoked (Requirement 4.7; vlm-anomaly-reference-parity 6.5).
        PIL is imported lazily so the module keeps importing on images
        without the vLLM wheel."""
        import io

        try:
            from PIL import Image
        except ImportError as err:
            raise GenerationError(
                model_name,
                "image decoding unavailable: PIL could not be imported "
                "({})".format(err),
            ) from err
        try:
            pil_image = Image.open(io.BytesIO(image_bytes))
            pil_image.load()
        except Exception as err:  # noqa: BLE001 - decode failure isolation (4.7)
            raise GenerationError(
                model_name,
                "failed to decode the supplied image bytes: {}".format(err),
            ) from err
        pil_reference = None
        if reference_bytes is not None:
            try:
                pil_reference = Image.open(io.BytesIO(reference_bytes))
                pil_reference.load()
            except Exception as err:  # noqa: BLE001 - decode failure isolation (6.5)
                raise GenerationError(
                    model_name,
                    "failed to decode the supplied reference image bytes: "
                    "{}".format(err),
                ) from err

        if pil_reference is None:
            content = [
                {"type": "image"},
                {"type": "text", "text": prompt},
            ]
            fallback = _QWEN_VL_PROMPT_FALLBACK.replace("{prompt}", prompt)
            multi_modal_data: Dict[str, Any] = {"image": pil_image}
        else:
            content = [
                {"type": "text", "text": "Input image:"},
                {"type": "image"},
                {"type": "text", "text": "Reference image:"},
                {"type": "image"},
                {"type": "text", "text": prompt},
            ]
            fallback = _QWEN_VL_TWO_IMAGE_PROMPT_FALLBACK.replace(
                "{prompt}", prompt
            )
            multi_modal_data = {"image": [pil_image, pil_reference]}

        messages = [{"role": "user", "content": content}]
        templated = None
        tokenizer = self._resolve_tokenizer(model_name)
        apply = getattr(tokenizer, "apply_chat_template", None)
        if callable(apply) and getattr(tokenizer, "chat_template", None):
            try:
                templated = apply(
                    messages, tokenize=False, add_generation_prompt=True
                )
            except Exception as err:  # noqa: BLE001 - fall back to literal form
                logger.warning(
                    "Chat template of model '%s' failed for image content "
                    "(%s); using the Qwen VL literal prompt form",
                    model_name,
                    err,
                )
                templated = None
        if not templated:
            templated = fallback
        return {
            "prompt": templated,
            "multi_modal_data": multi_modal_data,
        }

    def _resolve_tokenizer(self, model_name: str) -> Any:
        """Best-effort synchronous tokenizer lookup on the loaded engine
        (``LLMEngine.get_tokenizer()`` or the tokenizer group); ``None``
        when no usable tokenizer is reachable, in which case the caller
        falls back to the Qwen VL literal prompt form."""
        with self._lock:
            entry = self._models.get(model_name)
            engine = entry.engine if entry is not None else None
        if engine is None:
            return None
        inner = getattr(engine, "engine", None) or engine
        for candidate in (inner, engine):
            get_tokenizer = getattr(candidate, "get_tokenizer", None)
            if callable(get_tokenizer):
                try:
                    tokenizer = get_tokenizer()
                except Exception:  # noqa: BLE001 - fall through to attributes
                    tokenizer = None
                if inspect.isawaitable(tokenizer):
                    tokenizer.close()  # async surface: use attribute access
                    tokenizer = None
                if tokenizer is not None:
                    return tokenizer
        group = getattr(inner, "tokenizer", None)
        if group is None:
            return None
        return getattr(group, "tokenizer", group)

    @staticmethod
    def _output_text(output: Any) -> Optional[str]:
        """The generated text of a vLLM ``RequestOutput`` (first
        completion), tolerant of fakes exposing the same shape."""
        if output is None:
            return None
        outputs = getattr(output, "outputs", None)
        if not outputs:
            return None
        return getattr(outputs[0], "text", None)
