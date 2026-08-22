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
the engine from any state. An explicit ``unload`` of a still-staged
repository additionally writes the Unload_Tombstone marker
(:data:`~vllm_runtime.constants.UNLOAD_TOMBSTONE_NAME`, spec
vllm-model-reload-after-backend-restart Decision 2) so the post-restart
reconciler never re-drives a load the operator deliberately stopped;
staged-but-tombstoned repositories report the REPORTING-ONLY
``UNLOADED`` state (Decision 3 — derived from disk exactly the way
STAGED is, no transition-logic change), and an explicit ``load`` clears
the marker first-thing (re-arming reconciliation). Failures are
isolated: one model's load or
serve error (including GPU out-of-memory) transitions only that model to
FAILED — logged with the model name and the backend error — and never
touches another engine (4.6, 8.9). The embedded vision Triton scans its
own separate repository directory and is untouched by anything here (8.8).

Before an engine is constructed the manager runs the device memory
preflight (:mod:`vllm_runtime.memory_budget`, spec
jp6-vllm-kv-cache-oom-regression Decision 4): a doomed load costs ~4 min
of vLLM profiling and blocks the runtime server's event loop for the whole
construction, so a load whose requirement exceeds the device's measured
available memory — or the budget ``gpu_memory_utilization`` carves out of
the device's real total — is refused in the time of one ``/proc/meminfo``
read, with the full arithmetic in the FAILED reason. Around every failure
the manager measures whether the attempt's memory came back and, when it
did not, sets the per-backend-life Starvation_Latch so the next attempt is
refused instead of deepening the observed cascade (three failed loads left
the incident device at 26 GB used / 3 GB free with no model loaded).
Failure reasons carry exactly one stable category token (with the original
backend text verbatim after it), and a load that reaches READY with a KV
margin under the floor logs a WARNING instead of looking healthy.

vLLM is **not** imported at module import time: the ``vllm`` package only
exists on vLLM-capable images (JetPack 6 / JetPack 7), so the import happens lazily
inside the default engine/sampling-params factories. Both factories are
injectable, which is also how tests drive the manager with a fake
``AsyncLLMEngine`` and no GPU.
"""
import gc
import inspect
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Mapping, Optional, Union

from vllm_runtime import memory_budget
from vllm_runtime.constants import UNLOAD_TOMBSTONE_NAME, VLLM_MODEL_DIR
from vllm_runtime.memory_budget import (
    MINIMUM_KV_CACHE_BYTES,
    PREFLIGHT_REFUSED_MARKER,
    RECLAIM_TOLERANCE_BYTES,
    THIN_MARGIN_CONCURRENCY,
    StarvationLatch,
    format_gib,
)
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

#: System-role block prepended to the Qwen VL fallback forms when a
#: system prompt is configured (json-trigger-metadata-pipeline
#: Requirement 8.4). Absent/empty system prompt leaves the fallback
#: strings byte-identical to the pre-feature forms (Requirement 8.5).
_QWEN_VL_SYSTEM_PREFIX = "<|im_start|>system\n{system}<|im_end|>\n"

# --- failure classification (spec jp6-vllm-kv-cache-oom-regression, ------
# --- Decision 6; defect 1.6, expected behavior 2.6) ----------------------
#
# The device could not tell an accounting fault from a budget fault: the
# KV-cache exhaustion message (21:59:50Z, 22:12:16Z) and the NVML allocator
# INTERNAL ASSERT (13:36:30Z, 13:39:38Z, 21:44Z) both surfaced as raw
# reasons. Each failure reason now carries exactly ONE category token,
# PREPENDED, with the original backend text preserved VERBATIM after it —
# so `dda_triton.vllm_model_prep.KV_CACHE_HINT_MARKERS` (and the
# reconciler's mirror of it) keep matching, and no status surface changes
# shape. Whether the NVML assert is the same exhaustion seen from the
# allocator or a distinct CUDA/NVML fault is an OPEN QUESTION; the token
# records the symptom, not a cause.

#: vLLM could not reserve KV-cache blocks inside the configured budget.
KV_CACHE_EXHAUSTION_TOKEN = "kv-cache-exhaustion:"
#: torch's caching allocator / NVML failed while querying device memory.
ALLOCATOR_NVML_FAULT_TOKEN = "allocator-nvml-fault:"
#: The staged Triton_vLLM_Repository did not validate (no engine attempted).
REPOSITORY_INVALID_TOKEN = "repository-invalid:"
#: Anything else that broke engine construction or serving.
ENGINE_CONSTRUCTION_ERROR_TOKEN = "engine-construction-error:"

#: Token for a failure reason the classifier CANNOT recognise.
#:
#: DELIBERATELY EMPTY, and reported as a deviation from design Property 7
#: (which names `engine-construction-error:` here). The reason is a direct
#: contract collision: the sibling spec
#: `vllm-model-reload-after-backend-restart` — preserved verbatim by this
#: spec's Requirement 3.7 ("truthful status surfaces") — pins the retained
#: FAILED reason to the BACKEND text exactly
#: (`test_property_truthful_status.py`: `retained == reason`,
#: `test_property_vllm_reload_preservation.py`: full `ModelStatus` identity,
#: `test_property_reconciler_lifecycle.py`: `status.reason ==
#: plan.permanent_reason(...)`). Prefixing an unrecognised reason breaks
#: five of those legs, and they encode a validated on-device contract.
#:
#: Nothing this spec needs is lost: the categories that make a symptom
#: DISTINGUISHABLE (defect 1.6 — KV-cache exhaustion vs the NVML allocator
#: assert, plus preflight refusals and repository invalidity) are all
#: recognised and tokenized. An unrecognised reason carries no category
#: because the manager genuinely does not know one.
#:
#: Setting this back to :data:`ENGINE_CONSTRUCTION_ERROR_TOKEN` is the whole
#: change if those sibling legs are instead repointed to accept a token.
UNCLASSIFIED_FAILURE_TOKEN = ""

#: Failure categories that must NOT trigger the offline-cache gate's single
#: retry (:data:`HF_OFFLINE_ENV_VARS`). The retry exists for ONE reason: an
#: incomplete local snapshot, which `estimate_weights_on_disk` cannot
#: detect because verifying it against the repo manifest needs the network.
#: A KV-cache exhaustion, an allocator/NVML fault or a preflight refusal is
#: a DEVICE-MEMORY failure — retrying it would spend another ~4 min of
#: doomed profiling and start the second attempt with less memory than the
#: first, which is precisely the cascade defect 1.5 / Decision 5 exist to
#: stop (three failed loads left the device at 26 GB used / 3 GB free with
#: no model loaded). Deviation from the dispatch brief, recorded
#: deliberately: the brief said "retry whenever offline mode was applied by
#: us", and scoping it this way is what keeps the "no retry into a starved
#: device" contract (exploration Case 6 and Property 6-D both pin exactly
#: one construction per failed KV-OOM load) intact without weakening it.
_NO_OFFLINE_RETRY_TOKENS = (
    KV_CACHE_EXHAUSTION_TOKEN,
    ALLOCATOR_NVML_FAULT_TOKEN,
    PREFLIGHT_REFUSED_MARKER,
)

#: Every category token, including the preflight marker owned by
#: :mod:`vllm_runtime.memory_budget` (that reason arrives already tokenized,
#: so the classifier must not prepend a second one).
FAILURE_CATEGORY_TOKENS = (
    KV_CACHE_EXHAUSTION_TOKEN,
    ALLOCATOR_NVML_FAULT_TOKEN,
    PREFLIGHT_REFUSED_MARKER,
    REPOSITORY_INVALID_TOKEN,
    ENGINE_CONSTRUCTION_ERROR_TOKEN,
)

#: Case-insensitive signatures of the KV-cache exhaustion path. The first
#: two are vLLM's own text (verbatim from the incident's HTTP 409 body);
#: the third is the same wording the prep matches.
_KV_CACHE_SIGNATURES = (
    "no available memory for the cache",
    "memory for the cache blocks",
    "gpu_memory_utilization",
)

#: Case-insensitive signatures of the allocator/NVML fault path, verbatim
#: from ``NVML_SUCCESS == r INTERNAL ASSERT FAILED at
#: "/opt/pytorch/c10/cuda/CUDACachingAllocator.cpp":1131``.
_ALLOCATOR_NVML_SIGNATURES = (
    "nvml_success",
    "cudacachingallocator",
)

# --- offline engine construction (spec jp6-vllm-kv-cache-oom-regression, -
# --- task 11 OUTCOME block 18; defect 1.9's blast radius) ----------------
#
# PROVENANCE — DO NOT "simplify" THIS AWAY. On 2026-08-19 the JP6 model
# component went BROKEN after three consecutive load failures at 12:00:47Z,
# 12:02:09Z and 12:03:22Z (each `Startup script exited. {exitCode=1}`) whose
# reason was, verbatim:
#
#   (MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443):
#   Max retries exceeded with url: /api/models/Qwen/Qwen2.5-VL-7B-Instruct-
#   AWQ/tree/main?recursive=True&expand=False (Caused by
#   NameResolutionError("...: Failed to resolve \'huggingface.co\'
#   ([Errno -3] Temporary failure in name resolution)"))')
#
# The repository was ALREADY staged locally (gpu_memory_utilization=0.55,
# max_model_len=4096) and the weights were already in the HF cache; the
# network call is vLLM/transformers resolving the repo id
# `Qwen/Qwen2.5-VL-7B-Instruct-AWQ`, NOT our code (`src/backend` contains no
# huggingface_hub / HfApi / snapshot_download call in the load path). Two
# deployed workflows HARD-depend on that component, so they were left stuck
# at INSTALLED: a transient DNS fault became a workflow outage.
#
# So when the weights are demonstrably on disk, the engine is constructed
# with Hugging Face OFFLINE MODE enabled — an unreachable huggingface.co
# then cannot fail an already-staged model.
HF_OFFLINE_ENV_VARS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")

# --- Starvation_Latch settling (spec jp6-vllm-kv-cache-oom-regression, ---
# --- task 11 OUTCOME block 9, defect (b); Decision 5) --------------------
#
# The latch read `/proc/meminfo` immediately after `_shutdown_engine` +
# `_reclaim_gpu_memory`, i.e. against an UNSETTLED reading, because the
# driver releases asynchronously. Measured: the latch reported `9.45 GiB
# did not come back` and later in the SAME backend life, with NO restart,
# MemAvailable had recovered by ~5.2 GiB unaided — the genuinely-stranded
# amount was ~4 GiB, not 9.45 (a later occurrence reported 12.35 GiB). So
# it OVER-TRIGGERED: it refused loads and demanded a container restart when
# the memory would have come back on its own.
#
# PROPOSED THRESHOLDS (the same discipline as
# `memory_budget.RECLAIM_TOLERANCE_BYTES` and `THIN_MARGIN_CONCURRENCY`):
# these two numbers are NOT measured, and task 14 / H8 owns their
# calibration alongside the other proposed constants.

#: Delay between settle re-samples, seconds.
STARVATION_SETTLE_DELAY_SECONDS = 1.5
#: How many EXTRA samples to take after a first reading that looked
#: starved. Worst case adds 3 x 1.5 s = 4.5 s, and ONLY on a failure path
#: that already cost minutes of engine profiling — the healthy path (a
#: first reading within tolerance) sleeps not at all, so the runtime
#: server is never stalled for long.
STARVATION_SETTLE_RESAMPLES = 3

#: Sleep used between settle re-samples — a module-level callable so host
#: tests never actually sleep (patch this attribute, or pass ``sleep=`` to
#: :class:`VllmRuntimeManager`). Resolved at CALL time.
_default_sleep = time.sleep

# --- KV-margin readability (task 11 OUTCOME block 9, defect (d)) ---------
#
# A load reached READY with 0.77 GiB of KV against the 1 GiB floor —
# exactly the H5 shape — and NO WARNING appeared: `kv_bytes` introspection
# returned None and the only fallback was `logger.debug`, so the thinnest
# margin yet observed was reported as an unqualified success. The reader
# now says WHICH of three cases a reading is, so the caller can reach a
# verdict from a PARTIAL reading instead of falling silent.

#: Blocks, block size AND KV bytes were all readable.
KV_MARGIN_FULL = "full"
#: The geometry (blocks x block size, and usually ``max_model_len``) was
#: readable but ``kv_bytes`` was not — the shape the device hit.
KV_MARGIN_PARTIAL = "partial"
#: A ``cache_config`` exists but nothing usable could be read from it.
KV_MARGIN_UNREADABLE = "unreadable"


def classify_failure_reason(
    reason: Optional[str],
    default_token: str = UNCLASSIFIED_FAILURE_TOKEN,
) -> str:
    """The category token for a failure reason — a pure function.

    A reason that ALREADY starts with one of
    :data:`FAILURE_CATEGORY_TOKENS` keeps it (the preflight composes its own
    marker), so classification is idempotent and a reason never carries two
    tokens. ``default_token`` is the answer for an unrecognised reason and
    is how the caller says "this was a repository validation failure"
    instead of an engine-construction one.
    """
    text = (reason or "").strip()
    for token in FAILURE_CATEGORY_TOKENS:
        if text.startswith(token):
            return token
    lowered = text.lower()
    if any(signature in lowered for signature in _ALLOCATOR_NVML_SIGNATURES):
        return ALLOCATOR_NVML_FAULT_TOKEN
    if any(signature in lowered for signature in _KV_CACHE_SIGNATURES):
        return KV_CACHE_EXHAUSTION_TOKEN
    return default_token


def classify_failure(
    reason: Optional[str],
    default_token: str = UNCLASSIFIED_FAILURE_TOKEN,
) -> str:
    """``"<category token> <original reason>"`` — the classified reason
    stored on the model status and logged. The original text is preserved
    verbatim after the token; an already-classified reason is returned
    unchanged."""
    text = reason or ""
    token = classify_failure_reason(text, default_token)
    if not token or text.strip().startswith(token):
        return text
    # The original text follows the token BYTE FOR BYTE (whitespace and
    # all): every existing consumer matches substrings of it.
    return "{} {}".format(token, text)


def _safe_attr(obj: Any, name: str) -> Any:
    """``getattr`` that answers ``None`` instead of raising — engine
    introspection meets exotic shapes whose attribute access explodes."""
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - introspection is best-effort
        return None


def _positive_int(value: Any) -> Optional[int]:
    """``value`` when it is a strictly-positive plain ``int`` (a ``bool`` is
    an ``int`` and is rejected), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _default_kv_margin_reader(engine: Any) -> Optional[Dict[str, Any]]:
    """Best-effort read of a loaded engine's KV sizing — the injection seam
    behind the thin-margin WARNING (Decision 6; expected behavior 2.7).

    A small ``getattr`` chain over ``engine.engine.cache_config``
    (``num_gpu_blocks``, ``block_size``) plus the model config's
    ``max_model_len`` and KV geometry. Never touches CUDA: every value here
    is a plain attribute the engine already computed, and no exception
    escapes.

    The answer carries a ``readable`` classification, because "unreadable"
    used to collapse three different situations into one silent ``None``
    (defect (d): the device reached READY with 0.77 GiB of KV and nothing
    warned, because ``kv_bytes`` alone was ``None``):

    - ``None`` — the engine exposes NO ``cache_config`` at all (V1's
      engine-core child, and every pre-Decision-6 fake): there is nothing
      to report and nothing to escalate, so the caller stays quiet.
    - :data:`KV_MARGIN_UNREADABLE` — a ``cache_config`` exists but nothing
      usable could be read from it: the caller says so at WARNING level.
    - :data:`KV_MARGIN_PARTIAL` — the geometry is readable but ``kv_bytes``
      is not: the caller still reaches a verdict from the concurrency.
    - :data:`KV_MARGIN_FULL` — everything, including ``kv_bytes``.
    """
    try:
        inner = _safe_attr(engine, "engine") or engine
        cache_config = _safe_attr(inner, "cache_config")
        if cache_config is None:
            cache_config = _safe_attr(engine, "cache_config")
        if cache_config is None:
            return None

        model_config = _safe_attr(inner, "model_config")
        if model_config is None:
            model_config = _safe_attr(engine, "model_config")
        max_model_len = _positive_int(_safe_attr(cache_config,
                                                 "max_model_len"))
        if max_model_len is None:
            max_model_len = _positive_int(_safe_attr(model_config,
                                                     "max_model_len"))

        blocks = _positive_int(_safe_attr(cache_config, "num_gpu_blocks"))
        block_size = _positive_int(_safe_attr(cache_config, "block_size"))
        if blocks is None or block_size is None:
            return {
                "readable": KV_MARGIN_UNREADABLE,
                "num_gpu_blocks": blocks,
                "block_size": block_size,
                "tokens": None,
                "max_model_len": max_model_len,
                "concurrency": None,
                "kv_bytes": None,
            }
        tokens = blocks * block_size
        concurrency = (tokens / float(max_model_len)
                       if max_model_len is not None else None)
        kv_bytes = _kv_bytes_from_geometry(model_config, tokens)

        return {
            "readable": (KV_MARGIN_FULL if kv_bytes is not None
                         else KV_MARGIN_PARTIAL),
            "num_gpu_blocks": blocks,
            "block_size": block_size,
            "tokens": tokens,
            "max_model_len": max_model_len,
            "concurrency": concurrency,
            "kv_bytes": kv_bytes,
        }
    except Exception:  # noqa: BLE001 - introspection is strictly best-effort
        return None


def _kv_bytes_from_geometry(model_config: Any,
                            tokens: int) -> Optional[int]:
    """KV-cache bytes for ``tokens`` tokens, from the model config's KV
    geometry (``layers × kv_heads × head_size × 2 (K and V) × 2 bytes``) —
    the same arithmetic vLLM's own ``the rest of the memory reserved for KV
    Cache is …`` line reports. ``None`` when the geometry is not readable
    (exotic engine shapes, accessors needing arguments this call cannot
    supply); the concurrency arm of the thin-margin check still applies."""
    if model_config is None:
        return None

    def _measure(name: str) -> Optional[int]:
        accessor = getattr(model_config, name, None)
        if not callable(accessor):
            return None
        try:
            value = accessor()
        except TypeError:
            return None
        except Exception:  # noqa: BLE001 - best-effort
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return value

    layers = _measure("get_num_layers")
    kv_heads = _measure("get_num_kv_heads")
    head_size = _measure("get_head_size")
    if None in (layers, kv_heads, head_size):
        return None
    # 2 tensors (K and V) x 2 bytes per element (fp16/bf16 KV dtype).
    return int(tokens) * layers * kv_heads * head_size * 2 * 2


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
    #: REPORTING-ONLY (spec vllm-model-reload-after-backend-restart,
    #: Decision 3; Requirements 2.3, 2.4): a staged repository whose most
    #: recent lifecycle event was an explicit unload — the
    #: Unload_Tombstone marker is present. Derived from disk state
    #: exactly the way STAGED already is; NO transition logic reaches
    #: this state, and the reconciler never re-drives such a model.
    UNLOADED = "UNLOADED"


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
    run with fakes and no GPU. ``memory_reader`` (a callable returning
    ``/proc/meminfo``-shaped text) and ``kv_margin_reader`` (an engine ->
    KV-sizing mapping) follow the same convention: they default to the
    real readers so host tests can drive the preflight, the
    Starvation_Latch and the thin-margin check with fakes and no GPU.
    ``sleep`` is the same convention for the Starvation_Latch's
    settle-and-re-sample delay, so host tests never actually sleep. All
    state access is lock-guarded, so the manager is safe to touch from the
    HTTP server's event loop and from status-reporting threads alike.
    """

    def __init__(
        self,
        model_dir: Union[str, Path] = VLLM_MODEL_DIR,
        engine_factory: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        sampling_params_factory: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        memory_reader: Optional[Callable[[], str]] = None,
        kv_margin_reader: Optional[Callable[[Any], Optional[Dict[str, Any]]]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        self.model_dir = Path(model_dir)
        self._engine_factory = engine_factory or _default_engine_factory
        self._sampling_params_factory = (
            sampling_params_factory or _default_sampling_params_factory
        )
        #: ``None`` keeps :func:`memory_budget.read_memory`'s own default,
        #: resolved at CALL time so the module attribute stays patchable.
        self._memory_reader = memory_reader
        self._kv_margin_reader = kv_margin_reader or _default_kv_margin_reader
        #: ``None`` keeps the module-level :data:`_default_sleep`, resolved
        #: at CALL time so the module attribute stays patchable. Only the
        #: Starvation_Latch's settle-and-re-sample uses it.
        self._sleep = sleep
        self._lock = threading.Lock()
        self._models: Dict[str, _ManagedModel] = {}
        #: Starvation_Latch (Decision 5): set when a failed attempt's memory
        #: did not come back, cleared by an explicit unload. Per backend
        #: life, lock-guarded, NEVER persisted — no tombstone interaction,
        #: no new status surface.
        self._starvation_latch: Optional[StarvationLatch] = None

    # --- inspection --------------------------------------------------------

    def state(self, model_name: str) -> ModelStatus:
        """The model's current status: its tracked state when the manager
        knows it, STAGED when a repository directory exists on disk but no
        load was requested yet (UNLOADED when that repository carries the
        Unload_Tombstone — Decision 3, reporting-only), UNKNOWN for
        never-staged names."""
        with self._lock:
            entry = self._models.get(model_name)
            if entry is not None:
                return entry.status
        return self._disk_derived_status(model_name)

    def list_models(self) -> Dict[str, ModelStatus]:
        """Every model the manager tracks plus every repository staged on
        disk, with its current status — the feed for the device model
        status mechanisms (Requirements 4.6, 4.7)."""
        with self._lock:
            statuses = {name: entry.status for name, entry in self._models.items()}
        if self.model_dir.is_dir():
            for child in sorted(self.model_dir.iterdir()):
                if child.name not in statuses and self._repository_staged(child.name):
                    statuses[child.name] = self._disk_derived_status(child.name)
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

    def _disk_derived_status(self, model_name: str) -> ModelStatus:
        """Status of an UNTRACKED name, derived purely from disk state
        (spec vllm-model-reload-after-backend-restart, Decision 3):
        UNLOADED when the staged repository carries the Unload_Tombstone,
        STAGED when staged without it, UNKNOWN when nothing is staged.
        Reporting-only — no transition logic involved."""
        if self._repository_staged(model_name):
            if self._tombstoned(model_name):
                return ModelStatus(ModelState.UNLOADED)
            return ModelStatus(ModelState.STAGED)
        return UNKNOWN_STATUS

    # --- Unload_Tombstone helpers (Decision 2; Requirements 2.4, 3.5) ------

    def _tombstone_path(self, model_name: str) -> Path:
        return self.model_dir / model_name / UNLOAD_TOMBSTONE_NAME

    def _tombstoned(self, model_name: str) -> bool:
        """Whether the model's repository carries the Unload_Tombstone.
        Only the marker's EXISTENCE matters — its content is triage-only
        JSON (a UTC timestamp) and is never parsed, so a corrupt or
        unreadable marker still counts as tombstoned."""
        try:
            return self._tombstone_path(model_name).exists()
        except OSError:  # unreadable marker/directory still suppresses
            return True

    def _clear_tombstone(self, model_name: str) -> None:
        """Best-effort Unload_Tombstone removal (an explicit load re-arms
        reconciliation). Removal failure is logged and the caller
        proceeds."""
        try:
            self._tombstone_path(model_name).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Could not remove the unload tombstone of vLLM model "
                "'%s'; proceeding anyway",
                model_name,
                exc_info=True,
            )

    def _write_tombstone(self, model_name: str) -> None:
        """Best-effort Unload_Tombstone write after an explicit unload of
        a still-staged repository, so the post-restart reconciler never
        re-drives a load the operator deliberately stopped (Decision 2).
        The content is triage-only JSON with a UTC timestamp; ANY
        filesystem error is logged and swallowed — the unload's return
        value and semantics stay byte-identical (Requirement 3.5 is
        categorical)."""
        if not self._repository_staged(model_name):
            return
        try:
            self._tombstone_path(model_name).write_text(
                json.dumps(
                    {
                        "marker": "explicit unload",
                        "unloaded_at_utc": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    }
                )
            )
        except Exception:  # noqa: BLE001 - unload must always succeed (3.5)
            logger.exception(
                "Could not write the unload tombstone of vLLM model "
                "'%s'; the unload still succeeds (the model will be "
                "reconciled on the next backend start)",
                model_name,
            )

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

        FIRST action: best-effort removal of any Unload_Tombstone — an
        explicit load re-arms post-restart reconciliation (Decision 2;
        removal failure logs and the load proceeds).
        """
        self._clear_tombstone(model_name)
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
            return self._fail(model_name, str(err),
                              category=REPOSITORY_INVALID_TOKEN)
        # NOTHING is defaulted into the engine args here — deliberately.
        #
        # `limit_mm_per_prompt` used to be forced to {"image": 2} on this
        # line so two-image reference generation worked on any model
        # (vlm-anomaly-reference-parity Requirement 6.6). That default is
        # REMOVED (spec jp6-vllm-kv-cache-oom-regression, Decision 1): it
        # doubled the images a vision-language engine profiles for, inside
        # an unchanged gpu_memory_utilization budget whose one-image
        # activation peak was already 4.92 GiB of 11.98 GiB, and it was
        # invisible to every sizing surface by construction — the model was
        # published as fitting and then could not load (defect 1.4).
        #
        # The multimodal limit is now an AUTHORED, SIZED engine setting
        # (`model_import.ENGINE_DEFAULTS['limit_mm_per_prompt']`, default
        # {"image": 1}, sized by the publish-time Fit_Check and propagated
        # verbatim into model.json). When the staged args omit the key the
        # engine uses vLLM's own default of one image — exactly what
        # LocalServer 1.0.59 profiled for. A model that needs two images is
        # authored with {"image": 2} and sized for it; a two-image request
        # against a one-image model fails truthfully in
        # `_build_multimodal_prompt` rather than silently answering a
        # different question. DO NOT re-add a device-side default here.

        # Device memory preflight (Decision 4): refuse a doomed load in the
        # time of one /proc/meminfo read instead of ~4 min of engine
        # profiling that blocks the runtime server's event loop, and refuse
        # outright while the Starvation_Latch is set (Decision 5). Pure
        # kernel/disk reads — never CUDA, never NVML (that invariant is
        # `memory_budget`'s module docstring and `_reclaim_gpu_memory`'s).
        with self._lock:
            latch = self._starvation_latch
        verdict = memory_budget.preflight(
            engine_args,
            reader=self._memory_reader,
            latch=latch,
            model_name=model_name,
        )
        available_before = verdict.terms.get("available_bytes")
        failed_conditions = tuple(verdict.terms.get("failed_conditions") or ())
        # A refusal is only ENFORCED when its arithmetic rests on measured
        # terms. When the weights could not be sized on disk the verdict is
        # built from the documented ACTIVATION_FLOOR + KV-floor lower bound
        # (never a guessed weight), and refusing a load on a number this
        # runtime did not measure would be exactly the kind of invented
        # verdict the unsound publish-time gate was made of: the diagnostic
        # is logged and the engine decides. The Starvation_Latch arm is
        # enforced either way — it needs no weight estimate, only the two
        # readings around the previous failed attempt.
        if not verdict.ok and (not verdict.unverified
                               or "latch" in failed_conditions):
            return self._fail(
                model_name,
                verdict.refusal_reason or "{} vLLM model '{}' was refused by "
                "the device memory preflight".format(
                    PREFLIGHT_REFUSED_MARKER, model_name),
            )
        if not verdict.ok:
            logger.warning(
                "Device memory preflight for vLLM model '%s' did NOT clear, "
                "but its weights could not be sized on disk, so the verdict "
                "rests on a lower bound and the load PROCEEDS (it may still "
                "fail in engine profiling): %s",
                model_name, verdict.refusal_reason,
            )
        elif verdict.unverified:
            logger.info(
                "Device memory preflight for vLLM model '%s' ran UNVERIFIED "
                "(the weights could not be sized on disk; measured available "
                "%s against a %s lower-bound requirement); the load proceeds",
                model_name,
                format_gib(verdict.terms.get("available_bytes")),
                format_gib(verdict.terms.get("required_bytes")),
            )

        with self._lock:
            entry = self._models.get(model_name)
            if entry is None:  # unloaded mid-flight
                return UNKNOWN_STATUS
            entry.engine_args = dict(engine_args)
            entry.status = ModelStatus(ModelState.LOADING)
        logger.info("Loading vLLM model '%s'", model_name)

        # Offline-cache gate (task 11 OUTCOME block 18): when the weights
        # are demonstrably on this device's disk, construct the engine with
        # Hugging Face offline mode enabled so an unreachable huggingface.co
        # cannot fail an already-staged model. Restored in the `finally`
        # BELOW so an exception can never leak offline mode into unrelated
        # work in this process (the manager serialises loads, but the
        # restore must not depend on that).
        restore_env = self._apply_hf_offline_mode(model_name, engine_args)
        try:
            try:
                engine = await self._construct_engine(engine_args)
            except Exception as err:  # noqa: BLE001 - isolation (4.6, 8.9)
                if restore_env is None or classify_failure_reason(
                        str(err)) in _NO_OFFLINE_RETRY_TOKENS:
                    # Either offline mode was NOT applied by us (nothing to
                    # undo, nothing to retry), or the failure is a
                    # device-memory failure rather than a cache miss — see
                    # `_NO_OFFLINE_RETRY_TOKENS`: retrying would repeat the
                    # ~4 min profiling on a device with less memory than the
                    # first attempt had.
                    return self._fail(model_name, str(err),
                                      available_before=available_before)
                # Cache-miss fallback, EXACTLY ONCE:
                # `estimate_weights_on_disk` sizes weight files, it does
                # NOT verify the snapshot against the repo manifest (that
                # needs the network), so an incomplete cache is possible
                # and a first-time download must not regress.
                logger.warning(
                    "Constructing the engine of vLLM model '%s' in Hugging "
                    "Face offline mode FAILED (%s). The locally cached "
                    "weights may be incomplete, so the load is retried ONCE "
                    "with the offline environment restored — a first-time or "
                    "partial download must not be blocked by this gate.",
                    model_name, err,
                )
                self._restore_environment(restore_env)
                restore_env = None
                try:
                    engine = await self._construct_engine(engine_args)
                except Exception as retry_err:  # noqa: BLE001 - isolation
                    return self._fail(model_name, str(retry_err),
                                      available_before=available_before)
        finally:
            if restore_env is not None:
                self._restore_environment(restore_env)

        with self._lock:
            entry = self._models.get(model_name)
            if entry is None:  # unloaded mid-flight: free the fresh engine
                self._shutdown_engine(model_name, engine)
                return UNKNOWN_STATUS
            entry.engine = engine
            entry.status = ModelStatus(ModelState.READY)
        logger.info("vLLM model '%s' is READY", model_name)
        # READY is still READY: this only adds a WARNING when the engine's
        # own KV sizing says the load is one retry from failing (2.7).
        self._warn_on_thin_kv_margin(model_name, engine)
        return ModelStatus(ModelState.READY)

    async def _construct_engine(self, engine_args: Mapping[str, Any]) -> Any:
        """One engine construction through the injectable factory, awaiting
        an awaitable result. Exactly the two lines that used to sit inline
        in :meth:`load`, factored out so the offline-cache gate can run it
        at most twice without duplicating them."""
        engine = self._engine_factory(engine_args)
        if inspect.isawaitable(engine):
            engine = await engine
        return engine

    def _apply_hf_offline_mode(
        self, model_name: str, engine_args: Mapping[str, Any]
    ) -> Optional[Dict[str, Optional[str]]]:
        """Enable Hugging Face offline mode for the coming engine
        construction WHEN the model's weights are already on this device's
        disk (task 11 OUTCOME block 18; :data:`HF_OFFLINE_ENV_VARS`).

        Returns the snapshot to hand :meth:`_restore_environment`, or
        ``None`` when nothing was changed — which is both the
        weights-not-locatable answer (behaviour unchanged: no environment
        manipulation, one construction attempt) and the caller's signal
        that the single cache-miss retry does NOT apply.

        :func:`memory_budget.estimate_weights_on_disk` is the probe: pure,
        stdlib-only, network-free and CUDA-free, and it already resolves
        ``models--{org}--{name}`` under ``HF_HUB_CACHE`` / ``$HF_HOME/hub``
        / ``~/.cache/huggingface/hub``. A non-``None``, positive answer
        means the weights are there.
        """
        try:
            located = memory_budget.estimate_weights_on_disk(engine_args)
        except Exception:  # noqa: BLE001 - the probe never decides alone
            located = None
        if not isinstance(located, int) or located <= 0:
            return None

        snapshot: Dict[str, Optional[str]] = {
            name: os.environ.get(name) for name in HF_OFFLINE_ENV_VARS
        }
        for name in HF_OFFLINE_ENV_VARS:
            os.environ[name] = "1"
        logger.info(
            "vLLM model '%s' has its weights on local disk (%s located in "
            "the Hugging Face cache), so the engine is constructed with "
            "Hugging Face offline mode enabled (%s): an unreachable "
            "huggingface.co cannot fail an already-staged model (three "
            "consecutive name-resolution failures took this component "
            "BROKEN and left two dependent workflows stuck at INSTALLED).",
            model_name,
            format_gib(located),
            ", ".join(HF_OFFLINE_ENV_VARS),
        )
        return snapshot

    @staticmethod
    def _restore_environment(snapshot: Mapping[str, Optional[str]]) -> None:
        """Restore the EXACT prior state of the offline-mode variables:
        a variable that was absent is DELETED again (never left as ``""``),
        a variable that had a value gets that value back."""
        for name, previous in snapshot.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    def unload(self, model_name: str) -> bool:
        """Remove the model from any state, shutting its engine down and
        freeing GPU memory. Returns True when the manager was tracking the
        model.

        When the repository is still staged on disk, a best-effort
        Unload_Tombstone is written AFTER the engine shutdown/reclaim
        (Decision 2) so the post-restart reconciler treats the model as
        explicitly stopped; any filesystem error is logged and the
        unload's return value and semantics stay byte-identical
        (Requirement 3.5).

        An explicit unload also CLEARS the Starvation_Latch (Decision 5): a
        deliberate stop/start cycle is allowed to try again. The latch is
        the "do not retry into a starved device" memory of a failed
        attempt, not a permanent verdict — and when the device really is
        still starved the preflight's measured-availability arm refuses the
        next load anyway."""
        with self._lock:
            entry = self._models.pop(model_name, None)
            latch = self._starvation_latch
            self._starvation_latch = None
        if latch is not None:
            logger.info(
                "Cleared the vLLM starvation latch (set by the failed load "
                "of '%s': %s available before, %s after) on the explicit "
                "unload of '%s'; the next load is measured afresh",
                latch.model_name,
                format_gib(latch.available_before_bytes),
                format_gib(latch.available_after_bytes),
                model_name,
            )
        if entry is None:
            self._write_tombstone(model_name)
            return False
        if entry.engine is not None:
            self._shutdown_engine(model_name, entry.engine)
        self._reclaim_gpu_memory(model_name)
        self._write_tombstone(model_name)
        logger.info("vLLM model '%s' unloaded", model_name)
        return True

    def _fail(
        self,
        model_name: str,
        reason: str,
        available_before: Optional[int] = None,
        category: str = UNCLASSIFIED_FAILURE_TOKEN,
    ) -> ModelStatus:
        """Transition one model to FAILED, retaining and logging the
        backend reason with the model name (Requirement 4.6). No other
        model is touched.

        A failed engine CONSTRUCTION (the GPU out-of-memory case) has no
        engine object to shut down, yet the aborted initialization can
        leave many GB of GPU allocations behind — observed on-device as a
        first-load OOM that keeps OOMing on every plain retry until an
        unload releases the memory. Reclaim it here so the next load
        attempt starts from a clean allocator state.

        The retained reason gains at most ONE category token
        (:func:`classify_failure`), with the original backend text verbatim
        after it. ``category`` is the answer for a reason the classifier
        does not recognise — the caller's knowledge of WHICH layer failed
        (see :data:`UNCLASSIFIED_FAILURE_TOKEN` for why the default adds no
        token).

        ``available_before`` is the memory reading taken before this
        attempt's engine construction. When it is supplied, the reclaim is
        MEASURED (Decision 5): if the memory did not come back within
        :data:`RECLAIM_TOLERANCE_BYTES` the Starvation_Latch is set and a
        prominent WARNING says so, and the preflight refuses further loads
        in this backend life instead of deepening the cascade.
        """
        classified = classify_failure(reason, category)
        logger.error("vLLM model '%s' failed: %s", model_name, classified)
        status = ModelStatus(ModelState.FAILED, reason=classified)
        with self._lock:
            entry = self._models.get(model_name)
            engine = entry.engine if entry is not None else None
            if entry is not None:
                entry.status = status
                entry.engine = None
        if engine is not None:
            self._shutdown_engine(model_name, engine)
        self._reclaim_gpu_memory(model_name)
        if available_before is not None:
            self._latch_starvation_if_memory_did_not_return(
                model_name, available_before
            )
        return status

    def _latch_starvation_if_memory_did_not_return(
        self, model_name: str, available_before: int
    ) -> None:
        """Measure whether the failed attempt's memory came back, and latch
        the answer when it did not (Decision 5; defect 1.5, expected
        behavior 2.5).

        Read AFTER ``_shutdown_engine`` + ``_reclaim_gpu_memory``, so it
        measures the outcome of the reclaim rather than predicting it —
        which is exactly what the evidence supports: reclaim cleared an
        8.34 GiB non-torch swing on the KV-OOM path and cleared NOTHING on
        the NVML-assert path (three failed loads → 26 GB used / 3 GB free
        with no model loaded, recovered only by a container restart). No
        knowledge of WHY is needed to measure WHETHER.

        The first reading is taken exactly as before, and a reading within
        :data:`RECLAIM_TOLERANCE_BYTES` returns IMMEDIATELY — no added
        latency, no behaviour change, on the healthy path. Only a reading
        that looks starved is re-sampled after
        :data:`STARVATION_SETTLE_DELAY_SECONDS`, up to
        :data:`STARVATION_SETTLE_RESAMPLES` more times, because the driver
        releases ASYNCHRONOUSLY: the latch once reported "9.45 GiB did not
        come back" and ~5.2 GiB came back unaided in the same backend life
        with no restart (defect (b) of task 11's ninth OUTCOME block). The
        latch is set only when EVERY sample was short, and it records the
        BEST reading observed rather than the transient one.
        """
        sleeper = self._sleep if self._sleep is not None else _default_sleep
        best_after: Optional[int] = None
        samples = 0
        settled_seconds = 0.0
        for attempt in range(STARVATION_SETTLE_RESAMPLES + 1):
            if attempt:
                # Settle: the reading looked starved, so give the driver
                # time to finish releasing before believing it.
                sleeper(STARVATION_SETTLE_DELAY_SECONDS)
                settled_seconds += STARVATION_SETTLE_DELAY_SECONDS
            reading = memory_budget.read_memory(self._memory_reader)
            if reading is None:
                if best_after is None:
                    logger.info(
                        "Could not measure the memory reclaimed after the "
                        "failed load of vLLM model '%s'; no starvation "
                        "verdict is recorded", model_name,
                    )
                    return
                break  # keep the readings we did get and judge on them
            samples += 1
            available_after = reading.available_bytes
            if best_after is None or available_after > best_after:
                best_after = available_after
            if available_after >= available_before - RECLAIM_TOLERANCE_BYTES:
                if attempt:
                    # The observation this defect was missed for.
                    logger.info(
                        "The memory of the failed load of vLLM model '%s' "
                        "CAME BACK after %.1f seconds of settling (%s "
                        "available before the attempt, %s on sample %d of at "
                        "most %d, within the %s reclaim tolerance); the "
                        "driver had simply not finished releasing it, so NO "
                        "starvation verdict is recorded",
                        model_name, settled_seconds,
                        format_gib(available_before),
                        format_gib(available_after),
                        samples, STARVATION_SETTLE_RESAMPLES + 1,
                        format_gib(RECLAIM_TOLERANCE_BYTES),
                    )
                return

        latch = StarvationLatch(
            model_name=model_name,
            available_before_bytes=int(available_before),
            available_after_bytes=int(best_after),
        )
        with self._lock:
            if self._starvation_latch is None:
                self._starvation_latch = latch
        logger.warning(
            "STARVED DEVICE: the failed load of vLLM model '%s' did NOT "
            "return its memory — %s available before the attempt, %s after "
            "the engine shutdown and CUDA reclaim (the BEST of %d sample(s) "
            "taken over %.1f seconds of settling, so this is the settled "
            "figure and not a transient one; %s did not come back, more "
            "than the %s reclaim tolerance). Further vLLM loads are "
            "refused in this backend life to stop the allocation cascade "
            "(every retry would start with less memory than the last, and "
            "the co-resident GPU models share this memory). Recovery "
            "requires a BACKEND CONTAINER RESTART; an explicit unload of a "
            "vLLM model clears this latch and allows one measured retry.",
            model_name,
            format_gib(available_before),
            format_gib(best_after),
            samples,
            settled_seconds,
            format_gib(latch.lost_bytes),
            format_gib(RECLAIM_TOLERANCE_BYTES),
        )

    def _warn_on_thin_kv_margin(self, model_name: str, engine: Any) -> None:
        """Best-effort thin-margin report for a model that just reached
        READY (Decision 6; defect 1.7, expected behavior 2.7).

        The incident's "successful" retry reached READY with ``the rest of
        the memory reserved for KV Cache is 0.65GiB`` at ``Maximum
        concurrency for 4096 tokens per request: 2.95x`` — one retry and
        0.65 GiB from failing, reported as an unqualified success. A
        derived KV size below :data:`MINIMUM_KV_CACHE_BYTES`, or a derived
        concurrency below :data:`THIN_MARGIN_CONCURRENCY`, logs a WARNING.
        READY is still READY in every case.

        Three readability cases, kept DISTINCT (defect (d) of task 11's
        ninth OUTCOME block: a load reached READY with 0.77 GiB of KV
        against the 1 GiB floor and nothing warned, because ``kv_bytes``
        alone was ``None`` and the only fallback was an invisible debug
        line):

        (i)   nothing readable — an engine exposing no ``cache_config`` at
              all keeps the byte-identical debug line and stays quiet
              (there is no evidence of a KV margin to verify); a
              ``cache_config`` that IS exposed but yields nothing usable
              now WARNS that the margin could not be verified.
        (ii)  partially readable — geometry present, ``kv_bytes`` ``None``
              because the KV accessors were unavailable: the verdict is
              derived from what IS available (the concurrency), and the
              message says the KV size is unknown.
        (iii) fully readable — unchanged.
        """
        try:
            margin = self._kv_margin_reader(engine)
        except Exception:  # noqa: BLE001 - introspection is best-effort
            margin = None
        if not margin:
            logger.debug(
                "KV-cache sizing of vLLM model '%s' is not readable on this "
                "engine; no thin-margin check was performed", model_name,
            )
            return
        kv_bytes = margin.get("kv_bytes")
        concurrency = margin.get("concurrency")
        readable_bytes = isinstance(kv_bytes, (int, float)) \
            and not isinstance(kv_bytes, bool)
        readable_concurrency = isinstance(concurrency, (int, float)) \
            and not isinstance(concurrency, bool)
        if not readable_bytes and not readable_concurrency:
            # Case (i) with a cache_config present: escalated from
            # `logger.debug` to WARNING, because on the one device where it
            # mattered the debug line was invisible in practice.
            logger.warning(
                "KV-CACHE MARGIN NOT VERIFIED: vLLM model '%s' reached READY "
                "but this engine's KV sizing could NOT be read (blocks %s, "
                "block size %s, max_model_len %s), so a thin margin against "
                "the %s serving-margin floor CANNOT be ruled out for this "
                "model — READY is still READY, and this load is NOT reported "
                "as an unqualified success.",
                model_name,
                margin.get("num_gpu_blocks"),
                margin.get("block_size"),
                margin.get("max_model_len"),
                format_gib(MINIMUM_KV_CACHE_BYTES),
            )
            return
        # Case (ii)/(iii): judge on whichever arms ARE readable. An unknown
        # `kv_bytes` no longer silences the concurrency arm — that silence
        # is exactly how a 0.77 GiB margin passed as healthy.
        thin_bytes = readable_bytes and kv_bytes < MINIMUM_KV_CACHE_BYTES
        thin_concurrency = readable_concurrency \
            and concurrency < THIN_MARGIN_CONCURRENCY
        if not (thin_bytes or thin_concurrency):
            return
        logger.warning(
            "THIN KV-CACHE MARGIN: vLLM model '%s' reached READY with only "
            "%s of KV cache (%s GPU blocks x %s tokens = %s tokens, "
            "maximum concurrency %s at max_model_len %s) against a %s "
            "serving-margin floor and a %.1fx thin-margin threshold. This "
            "load is ONE RETRY from failing: the same configuration failed "
            "on this device when the co-resident memory happened to be "
            "higher at profiling time. Reduce demand (max_model_len, "
            "limit_mm_per_prompt.image, a smaller or more quantized model) "
            "or free device memory; raising gpu_memory_utilization takes "
            "memory the co-resident GPU models are already using.",
            model_name,
            format_gib(kv_bytes) if isinstance(kv_bytes, (int, float))
            else "an unknown amount",
            margin.get("num_gpu_blocks"),
            margin.get("block_size"),
            margin.get("tokens"),
            "{:.2f}x".format(concurrency)
            if isinstance(concurrency, (int, float)) else "unknown",
            margin.get("max_model_len"),
            format_gib(MINIMUM_KV_CACHE_BYTES),
            THIN_MARGIN_CONCURRENCY,
        )

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
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate to completion and return the generated text.

        ``image`` optionally carries encoded image bytes for multimodal
        generation on vision-language models (edge-vlm-image-inference
        Requirements 4.1, 4.3, 4.4); ``reference_image`` optionally adds
        a second, reference image to the same prompt
        (vlm-anomaly-reference-parity Requirement 6.1). Text-only
        invocations are byte-identical to pre-feature behavior.
        ``system_prompt`` optionally carries system-role instructions
        placed ahead of the user prompt (json-trigger-metadata-pipeline
        Requirements 8.3, 8.4, 8.7); absent/empty leaves every prompt
        form byte-identical to pre-feature behavior (Requirement 8.5).

        Raises :class:`ModelUnavailableError` when the model is not READY
        (carrying its actual status) and :class:`GenerationError` when the
        engine reports a failure — logged with the model name and backend
        error, other models untouched (Requirements 4.6, 8.8).
        """
        final = None
        async for output in self._request(
            model_name, prompt, sampling_params, image, reference_image,
            system_prompt,
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
        system_prompt: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Async iterator of incremental token text, in generation order.

        The engine yields cumulative request outputs; this yields only
        each step's new suffix. Errors surface as
        :class:`GenerationError` after already-yielded tokens (the caller
        decides how to signal them in-stream)."""
        previous = ""
        async for output in self._request(
            model_name, prompt, sampling_params, image, reference_image,
            system_prompt,
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
        system_prompt: Optional[str] = None,
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

        A non-empty ``system_prompt`` places system text ahead of user
        text on every path (json-trigger-metadata-pipeline 8.3, 8.4,
        8.7): the bare-string paths carry no role-tagged template, so
        ordered concatenation with a blank-line separator is the defined
        "ahead of" form; the multimodal path prepends a system-role chat
        message. Absent/empty ⇒ byte-identical to pre-feature (8.5).
        """
        engine = self._ready_engine(model_name)
        if image is None:
            engine_prompt: Any = (
                "{0}\n\n{1}".format(system_prompt, prompt)
                if system_prompt else prompt
            )
        elif self._is_multimodal(model_name):
            engine_prompt = self._build_multimodal_prompt(
                model_name, prompt, image, reference_image, system_prompt
            )
        else:
            logger.warning(
                "vLLM model '%s' is not multimodal; ignoring the supplied "
                "image and generating text-only",
                model_name,
            )
            engine_prompt = (
                "{0}\n\n{1}".format(system_prompt, prompt)
                if system_prompt else prompt
            )
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
            status = self._disk_derived_status(model_name)
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
        system_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the vLLM multimodal engine prompt: chat-templated text
        containing the model's image placeholder tokens plus
        ``multi_modal_data`` carrying the decoded image(s) (Requirement
        4.1; vlm-anomaly-reference-parity 6.1, 6.2).

        A non-empty ``system_prompt`` prepends a system-role message
        ahead of the user entry before the chat template is applied, for
        both the single-image and the two-image forms
        (json-trigger-metadata-pipeline Requirement 8.3); the Qwen VL
        fallback forms gain a ``<|im_start|>system…`` block ahead of the
        user section with the vision placeholder tokens and remainder
        unchanged (Requirement 8.4). Absent/empty ⇒ messages and
        fallback strings byte-identical to pre-feature (Requirement 8.5).

        Without ``reference_bytes`` the message and return value are
        byte-identical to the pre-reference single-image form. With a
        reference, the content labels and places the input image first
        and the reference second — mirroring Bedrock's content blocks —
        and ``multi_modal_data["image"]`` is the two-element list in
        that order (vLLM's standard multi-image form).

        Undecodable image bytes raise :class:`GenerationError` naming the
        image (input or reference) decoding failure, before the engine is
        ever invoked (Requirement 4.7; vlm-anomaly-reference-parity 6.5).
        A reference-image request against a model whose AUTHORED
        ``limit_mm_per_prompt.image`` is below 2 raises the same way, with
        the effective limit and the remediation (spec
        jp6-vllm-kv-cache-oom-regression Decision 1) — the reference image
        is never silently dropped, because answering the one-image question
        confidently is worse in a defect-detection product than failing
        loudly with an exact fix. PIL is imported lazily so the module keeps
        importing on images without the vLLM wheel."""
        if reference_bytes is not None:
            self._require_two_image_capacity(model_name)
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
        if system_prompt:
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": [{"type": "text", "text": system_prompt}],
                },
            )
            fallback = (
                _QWEN_VL_SYSTEM_PREFIX.replace("{system}", system_prompt)
                + fallback
            )
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

    def _require_two_image_capacity(self, model_name: str) -> None:
        """Refuse a two-image (reference) request when the loaded model's
        AUTHORED multimodal limit is one image (Decision 1; expected
        behavior 2.4, preservation 3.9).

        The limit is read from the model's tracked engine args — the staged
        ``model.json``, verbatim — so the answer is one per published model
        rather than a function of transient device state. Models authored
        with ``limit_mm_per_prompt = {"image": 2}`` (and sized for it by the
        publish-time Fit_Check) keep serving two-image anomaly-reference
        requests unchanged."""
        effective = memory_budget.images_per_prompt(
            self.engine_args(model_name)
        )
        if effective >= 2:
            return
        raise GenerationError(
            model_name,
            "this request supplies a reference image, which needs two images "
            "per prompt, but model '{}' is authored for "
            "limit_mm_per_prompt.image = {} — the reference image is NOT "
            "silently dropped, because a one-image answer would be a "
            "confident verdict about a different question. Remediation: set "
            "`limit_mm_per_prompt.image = 2` in the model's engine "
            "configuration, then re-package and re-publish the model (the "
            "publish-time fit check sizes the larger two-image profiling "
            "peak, so the configuration is checked against the device "
            "budget before it ships)".format(model_name, effective),
        )

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
