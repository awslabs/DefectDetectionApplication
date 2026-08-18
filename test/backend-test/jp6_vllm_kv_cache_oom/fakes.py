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
"""Suite-shared fakes for ``test/backend-test/jp6_vllm_kv_cache_oom``
(spec: jp6-vllm-kv-cache-oom-regression).

HONESTY GUARD (design "Honesty Guard", binding). Nothing here loads a real
vLLM engine, allocates GPU memory, touches CUDA/NVML, or reproduces Jetson
unified-memory accounting. Every memory number is **injected**: the
"device" is a crafted ``/proc/meminfo`` text, the engine is the manager's
existing public ``engine_factory`` seam, and KV sizing is a fake
``cache_config`` object. What these fakes can prove is decision logic,
message content, classification and exit codes — nothing about a GPU. The
GPU-only claims live in the [HARDWARE] H1-H8 tasks.

Pieces:

- :func:`build_staged_repo` — a valid staged Triton_vLLM_Repository
  (``config.pbtxt`` declaring ``backend: "vllm"`` + ``1/model.json``), the
  exact layout ``vllm_runtime.repository.parse_repository`` validates.
- :data:`INCIDENT_ENGINE_ARGS` — the device's staged args, verbatim from
  bugfix.md (note it does **not** set ``limit_mm_per_prompt``: that
  omission is what lets 1.0.61's unconditional default apply).
- :class:`FakeMeminfoReader` — a fake ``/proc/meminfo`` reader over a
  scripted sequence of (total, available) readings; the injection seam for
  every preflight/starvation case.
- :func:`install_memory_reader` — best-effort injection of that reader into
  whichever seam the fix exposes (see SEAM CONTRACT below). Returns the
  list of seams it installed — **empty on the unfixed tree, where no seam
  exists at all**, which is itself part of the counterexample for the
  preflight/starvation cases.
- :class:`RecordingEngineFactory` / :class:`FailingEngineFactory` — the
  manager's injectable ``engine_factory`` seam: one records every engine
  construction (zero calls == "refused before construction"), the other
  raises a chosen backend error to simulate a failed load with no GPU.
- :class:`FakeEngine` — the surface the manager reads: ``generate(...)``,
  ``errored``, and an inner ``engine`` carrying ``model_config`` (the
  multimodal flag) and ``cache_config`` (KV sizing for the thin-margin
  case).
- :func:`thin_cache_config` / :func:`healthy_cache_config` — KV sizings
  below and well above the margin, scaled to the incident's numbers.
- :func:`weight_tree` / :func:`hf_cache_tree` — sparse weight files (an
  N-GiB ``*.safetensors`` costs no disk) and the
  ``models--{org}--{name}`` HF cache layout, for the device-side
  weights-on-disk probe.
- :data:`KV_OOM_REASON` / :data:`NVML_ASSERT_REASON` and the crafted 409
  bodies — verbatim device strings from bugfix.md, for the failure
  classifier and the prep's classification.
- :func:`png_bytes` — a tiny real PNG so the multimodal prompt builder
  decodes actual image bytes (no mocked PIL).

SEAM CONTRACT (read this before implementing tasks 3.5/3.6). The fix's
memory-reader injection point is discovered by name against the live
module and constructor, from these candidates:

- ``vllm_runtime.memory_budget`` module attributes:
  :data:`READER_ATTR_CANDIDATES`
- ``vllm_runtime.memory_budget`` path constant:
  :data:`MEMINFO_PATH_ATTR_CANDIDATES`
- ``VllmRuntimeManager.__init__`` keyword: :data:`MANAGER_KWARG_CANDIDATES`

Implement at least one of them (any name from the lists) and these suites
drive the fix with no GPU. Do not weaken the tests to match a seam that
was named differently — add the name here.
"""
import inspect
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from vllm_runtime.manager import VllmRuntimeManager

GIB = 1024 ** 3

#: The incident's model (ryanorinagxdevkithomelabjp622, 2026-08-17).
DEFAULT_MODEL_NAME = "qwen2-5-vl-7b-instruct-awq"

#: The staged engine args on the device, VERBATIM from bugfix.md
#: ("Staged engine args, verbatim"). ``limit_mm_per_prompt`` is ABSENT —
#: that omission is what lets the runtime's unconditional
#: ``{"image": 2}`` default apply on 1.0.61 (defect 1.4).
INCIDENT_ENGINE_ARGS: Dict[str, Any] = {
    "dtype": "auto",
    "max_model_len": 4096,
    "gpu_memory_utilization": 0.4,
    "enforce_eager": True,
    "tensor_parallel_size": 1,
    "model": "Qwen/Qwen2.5-VL-7B-Instruct-AWQ",
}

#: Measured device totals (``free -g`` total 29 GB; vLLM's own four terms
#: sum to ≈29.95 GiB). Used as the injected ``MemTotal``.
DEVICE_TOTAL_BYTES = int(29.95 * GIB)

#: The verbatim KV-cache exhaustion reason (22:12:16Z, HTTP 409 body).
KV_OOM_REASON = (
    "No available memory for the cache blocks. Try increasing "
    "`gpu_memory_utilization` when initializing the engine."
)

#: The verbatim NVML allocator assert (13:36:30Z, 13:39:38Z, 21:44Z).
NVML_ASSERT_REASON = (
    'NVML_SUCCESS == r INTERNAL ASSERT FAILED at '
    '"/opt/pytorch/c10/cuda/CUDACachingAllocator.cpp":1131, please report '
    'a bug to PyTorch.'
)

#: Crafted Triton model-control 409 bodies (the shape
#: ``vllm_model_prep.extract_load_failure_reason`` parses).
KV_OOM_409_BODY = json.dumps({"error": KV_OOM_REASON})
NVML_409_BODY = json.dumps({"error": NVML_ASSERT_REASON})

#: Seam names the fake memory reader is installed under (SEAM CONTRACT).
READER_ATTR_CANDIDATES: Tuple[str, ...] = (
    "_default_proc_meminfo_reader",
    "_DEFAULT_MEMINFO_READER",
    "DEFAULT_MEMINFO_READER",
    "default_meminfo_reader",
    "_default_reader",
)
MEMINFO_PATH_ATTR_CANDIDATES: Tuple[str, ...] = (
    "PROC_MEMINFO_PATH",
    "MEMINFO_PATH",
    "_PROC_MEMINFO_PATH",
)
MANAGER_KWARG_CANDIDATES: Tuple[str, ...] = (
    "memory_reader",
    "meminfo_reader",
    "memory_reader_factory",
)


# ---------------------------------------------------------------------------
# Staged repository
# ---------------------------------------------------------------------------

def build_staged_repo(
    model_dir: Union[str, Path],
    model_name: str = DEFAULT_MODEL_NAME,
    engine_args: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Write a valid staged Triton_vLLM_Repository for ``model_name`` under
    ``model_dir`` (the ``VLLM_MODEL_DIR`` stand-in): ``config.pbtxt``
    declaring ``backend: "vllm"`` and ``1/model.json`` holding the engine
    args as a JSON object."""
    repo = Path(model_dir) / model_name
    (repo / "1").mkdir(parents=True, exist_ok=True)
    (repo / "config.pbtxt").write_text('backend: "vllm"\n')
    args = dict(INCIDENT_ENGINE_ARGS if engine_args is None else engine_args)
    (repo / "1" / "model.json").write_text(json.dumps(args, indent=2))
    return repo


# ---------------------------------------------------------------------------
# Injected /proc/meminfo readings
# ---------------------------------------------------------------------------

def meminfo_text(total_bytes: int, available_bytes: int) -> str:
    """A crafted ``/proc/meminfo`` body carrying the two fields the device
    preflight reads (kB units, exactly as the kernel writes them)."""
    total_kb = int(total_bytes // 1024)
    available_kb = int(available_bytes // 1024)
    return (
        "MemTotal:       {:>12} kB\n"
        "MemFree:        {:>12} kB\n"
        "MemAvailable:   {:>12} kB\n"
        "Buffers:               1024 kB\n"
        "Cached:              204800 kB\n"
    ).format(total_kb, max(available_kb - 2048, 0), available_kb)


class FakeMeminfoReader:
    """Fake ``/proc/meminfo`` reader over a scripted sequence of
    ``(total_bytes, available_bytes)`` readings.

    Call N returns reading N; once the script is exhausted the LAST reading
    repeats (a device whose memory did not come back stays that way). Every
    call is recorded, so a test can state exactly which readings the code
    under test observed.
    """

    def __init__(self, readings: Sequence[Tuple[int, int]]):
        if not readings:
            raise ValueError("at least one reading is required")
        self.readings: List[Tuple[int, int]] = [
            (int(total), int(available)) for total, available in readings
        ]
        self.observed: List[Tuple[int, int]] = []

    @property
    def call_count(self) -> int:
        return len(self.observed)

    def __call__(self) -> str:
        index = min(self.call_count, len(self.readings) - 1)
        reading = self.readings[index]
        self.observed.append(reading)
        return meminfo_text(*reading)

    def describe(self) -> str:
        return ", ".join(
            "reading {}: total={:.2f} GiB available={:.2f} GiB".format(
                i + 1, total / GIB, available / GIB
            )
            for i, (total, available) in enumerate(self.observed)
        ) or "no readings observed"


def install_memory_reader(monkeypatch, reader: FakeMeminfoReader,
                          tmp_path: Optional[Union[str, Path]] = None
                          ) -> List[str]:
    """Install ``reader`` into every memory-reading seam the fix exposes
    (SEAM CONTRACT in the module docstring).

    Returns the list of installed seam names. An EMPTY list means the tree
    under test has no memory-reading seam at all — on the unfixed tree that
    is expected and is part of the counterexample (no preflight exists);
    after the fix it means a seam was named outside the candidate lists and
    the candidates must be extended (never the assertions weakened).
    """
    installed: List[str] = []
    try:
        import vllm_runtime.memory_budget as memory_budget
    except ImportError:
        return installed

    for name in READER_ATTR_CANDIDATES:
        if hasattr(memory_budget, name):
            monkeypatch.setattr(memory_budget, name, reader)
            installed.append("memory_budget.{}".format(name))

    if tmp_path is not None:
        for name in MEMINFO_PATH_ATTR_CANDIDATES:
            if hasattr(memory_budget, name):
                path = Path(tmp_path) / "proc-meminfo-{}".format(name)
                path.write_text(meminfo_text(*reader.readings[0]))
                monkeypatch.setattr(memory_budget, name, str(path))
                installed.append("memory_budget.{}".format(name))

    return installed


def manager_reader_kwargs(reader: FakeMeminfoReader) -> Dict[str, Any]:
    """The subset of :data:`MANAGER_KWARG_CANDIDATES` the live
    ``VllmRuntimeManager.__init__`` accepts, mapped to ``reader``. Empty on
    the unfixed tree (the constructor has no memory seam)."""
    parameters = inspect.signature(VllmRuntimeManager.__init__).parameters
    return {name: reader for name in MANAGER_KWARG_CANDIDATES
            if name in parameters}


def make_manager(
    model_dir: Union[str, Path],
    engine_factory: Any,
    memory_reader: Optional[FakeMeminfoReader] = None,
    sampling_params_factory: Any = dict,
) -> VllmRuntimeManager:
    """A ``VllmRuntimeManager`` over ``model_dir`` driven by the injected
    engine factory (and, when the constructor exposes the seam, the
    injected memory reader). Never touches a GPU."""
    kwargs: Dict[str, Any] = {}
    if memory_reader is not None:
        kwargs.update(manager_reader_kwargs(memory_reader))
    return VllmRuntimeManager(
        model_dir=model_dir,
        engine_factory=engine_factory,
        sampling_params_factory=sampling_params_factory,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Fake engine (the manager's public engine_factory seam)
# ---------------------------------------------------------------------------

class _FakeCompletion:
    def __init__(self, text: str):
        self.text = text


class _FakeRequestOutput:
    """The ``RequestOutput`` shape ``_output_text`` reads."""

    def __init__(self, text: str):
        self.outputs = [_FakeCompletion(text)]
        self.finished = True


GENERATED_TEXT = "fake-generated-text"


def thin_cache_config(num_gpu_blocks: int = 340, block_size: int = 16) -> Any:
    """KV sizing BELOW the margin, scaled to the incident's successful-but-
    marginal load (``the rest of the memory reserved for KV Cache is
    0.65GiB``, ``Maximum concurrency for 4096 tokens per request: 2.95x``).

    ``340 blocks × 16 tokens = 5,440 tokens`` — 1.33x concurrency at
    ``max_model_len=4096`` — and, with the companion
    :func:`fake_model_config` KV geometry, ≈0.29 GiB of KV: below the
    1 GiB floor on either route (bytes or concurrency)."""
    return SimpleNamespace(
        num_gpu_blocks=num_gpu_blocks,
        num_cpu_blocks=0,
        block_size=block_size,
        cache_dtype="auto",
        gpu_memory_utilization=0.4,
    )


def healthy_cache_config(num_gpu_blocks: int = 20000,
                         block_size: int = 16) -> Any:
    """KV sizing well ABOVE the margin (320,000 tokens — 78x concurrency at
    ``max_model_len=4096``): the no-warning control."""
    return SimpleNamespace(
        num_gpu_blocks=num_gpu_blocks,
        num_cpu_blocks=0,
        block_size=block_size,
        cache_dtype="auto",
        gpu_memory_utilization=0.4,
    )


def fake_model_config(multimodal: bool = True,
                      architectures: Optional[Iterable[str]] = None) -> Any:
    """A fake ``ModelConfig``: vLLM's own ``is_multimodal_model`` flag plus
    the hf_config architectures fallback and the KV-geometry accessors a
    bytes-based KV computation would use (28 layers × 4 KV heads × 128 head
    size × 2 bytes = 57,344 bytes/token)."""
    hf_config = SimpleNamespace(
        architectures=list(
            architectures or ["Qwen2_5_VLForConditionalGeneration"]
        )
    )
    return SimpleNamespace(
        is_multimodal_model=multimodal,
        hf_config=hf_config,
        max_model_len=4096,
        get_num_layers=lambda *a, **k: 28,
        get_num_kv_heads=lambda *a, **k: 4,
        get_head_size=lambda *a, **k: 128,
    )


class FakeEngine:
    """A fake ``AsyncLLMEngine`` exposing exactly the surface the manager
    uses: ``generate(prompt, sampling_params, request_id)`` yielding request
    outputs, the ``errored`` flag, and an inner ``engine`` carrying
    ``model_config`` / ``cache_config`` for capability detection and KV
    introspection."""

    def __init__(self, engine_args: Mapping[str, Any],
                 cache_config: Optional[Any] = None,
                 multimodal: bool = True,
                 text: str = GENERATED_TEXT):
        self.engine_args = dict(engine_args)
        self.text = text
        self.errored = False
        self.prompts: List[Any] = []
        self.engine = SimpleNamespace(
            model_config=fake_model_config(multimodal=multimodal),
            cache_config=cache_config or healthy_cache_config(),
        )

    async def generate(self, prompt, sampling_params, request_id):
        self.prompts.append(prompt)
        yield _FakeRequestOutput(self.text)


class RecordingEngineFactory:
    """Recording fake for the manager's injectable ``engine_factory`` seam.
    Every engine construction is recorded with the engine args it was
    handed — that recording is what proves whether the runtime injected a
    ``limit_mm_per_prompt`` the staged args never contained (defect 1.4),
    and ``call_count == 0`` is what proves a preflight refused before
    construction (defect 1.10)."""

    def __init__(self, cache_config: Optional[Any] = None,
                 multimodal: bool = True):
        self.calls: List[Dict[str, Any]] = []
        self.engines: List[FakeEngine] = []
        self._cache_config = cache_config
        self._multimodal = multimodal

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, engine_args: Mapping[str, Any]) -> FakeEngine:
        self.calls.append(dict(engine_args))
        engine = FakeEngine(
            engine_args,
            cache_config=self._cache_config,
            multimodal=self._multimodal,
        )
        self.engines.append(engine)
        return engine


class FailingEngineFactory:
    """An ``engine_factory`` that raises — a failed engine CONSTRUCTION with
    no GPU involved. ``reason`` defaults to the device's verbatim KV-cache
    exhaustion text."""

    def __init__(self, reason: str = KV_OOM_REASON):
        self.reason = reason
        self.calls: List[Dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, engine_args: Mapping[str, Any]):
        self.calls.append(dict(engine_args))
        raise RuntimeError(self.reason)


# ---------------------------------------------------------------------------
# Weights on disk (sparse files: an N-GiB "checkpoint" costs no disk)
# ---------------------------------------------------------------------------

def weight_tree(root: Union[str, Path], total_bytes: int,
                shards: int = 2, suffix: str = ".safetensors") -> Path:
    """A local weights directory (the S3-sourced rewritten-path shape)
    holding ``shards`` sparse weight files summing to ``total_bytes``.
    Sparse via ``truncate``: ``st_size`` reports the full size while no
    blocks are allocated, so a 6.5 GiB "checkpoint" is free."""
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps({"model_type": "qwen2_5_vl"}))
    per_shard, remainder = divmod(int(total_bytes), shards)
    for index in range(shards):
        size = per_shard + (remainder if index == shards - 1 else 0)
        path = directory / "model-{:05d}-of-{:05d}{}".format(
            index + 1, shards, suffix)
        with open(path, "wb") as handle:
            handle.truncate(size)
    return directory


def hf_cache_tree(cache_root: Union[str, Path], repo_id: str,
                  total_bytes: int, revision: str = "deadbeef") -> Path:
    """A fake Hugging Face cache layout for ``org/name``:
    ``models--{org}--{name}/snapshots/{revision}/`` holding sparse weight
    files summing to ``total_bytes``. Returns the snapshot directory."""
    org, _, name = repo_id.partition("/")
    folder = Path(cache_root) / "models--{}--{}".format(org, name or org)
    snapshot = folder / "snapshots" / revision
    weight_tree(snapshot, total_bytes)
    refs = folder / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(revision)
    return snapshot


def disk_usage_of(path: Union[str, Path]) -> int:
    """Apparent (``st_size``) total of every regular file under ``path`` —
    what a weights-on-disk probe sums."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(str(path)):
        for filename in filenames:
            try:
                total += os.stat(os.path.join(dirpath, filename)).st_size
            except OSError:
                continue
    return total


# ---------------------------------------------------------------------------
# Real (tiny) image bytes — no mocked PIL
# ---------------------------------------------------------------------------

def png_bytes(size: Tuple[int, int] = (8, 8),
              color: Tuple[int, int, int] = (200, 30, 30)) -> bytes:
    """A real, decodable PNG. The multimodal prompt builder decodes these
    with the actual PIL on this host — no mocking, no GPU."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()
