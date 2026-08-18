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
"""Device-side vLLM memory budget math (spec: jp6-vllm-kv-cache-oom-regression,
design File 5 / Decision 4).

A pure, stdlib-only sizing model plus the memory reader behind the device
preflight: it answers "can this staged configuration possibly load on THIS
device right now?" from `/proc/meminfo` and the staged engine args, before a
single byte of GPU memory is allocated. On the incident device a doomed load
costs ~4 minutes of engine profiling and blocks the runtime server's event
loop for the whole construction; this module answers in the time of one file
read plus a directory stat walk (defect 1.10, expected behavior 2.9).

BINDING INVARIANT — this module imports no torch, no CUDA and no vLLM, and it
never touches the GPU or NVML by any route. Availability comes from
`/proc/meminfo` (`MemTotal`, `MemAvailable`) and weights come from `os.stat`.

WHY the invariant is non-negotiable: a CUDA-initializing probe in the PARENT
backend process poisons every subsequently forked child (defect 1.3 of the
`vllm-jp7-engine-cuda-init` spec — `cudaErrorDevicesUnavailable` in engine
children after the parent initialized the driver). The manager's
`_reclaim_gpu_memory` already encodes exactly that rule: it gates on
`torch.cuda.is_initialized()`, a pure state read, and deliberately never on a
driver-initializing availability probe. A preflight that asked CUDA how much
memory is free would break the very load it is trying to protect, so the
preflight asks the kernel instead. Jetson unified memory makes that honest:
`MemAvailable` IS the memory the engine will draw from.

Second consequence of being pure: every function here is host-testable with no
GPU. `read_memory(reader=...)` is the injection seam every host test uses
(`test/backend-test/jp6_vllm_kv_cache_oom/`), and no public function in this
module raises — an undeterminable input degrades to `None` or to a labelled
`unverified` verdict, never to a guessed number and never to an exception on
the load path.
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

GIB = 1024 ** 3

#: The kernel file the preflight reads. Module-level (and read at call time)
#: so host tests can point it at a crafted file.
PROC_MEMINFO_PATH = "/proc/meminfo"

# ---------------------------------------------------------------------------
# Sizing constants — MIRRORED, single source of truth is the portal
# ---------------------------------------------------------------------------
# `edge-cv-portal/backend/functions/vllm_fit_check.py` is the SINGLE SOURCE OF
# TRUTH for every constant and for the `activation_allowance` formula in this
# block. They are duplicated here (not imported) because the portal function
# is a Lambda that is not on the device's import path at all. The two copies
# are pinned equal by the Property 8 cross-check test
# `test/backend-test/jp6_vllm_kv_cache_oom/test_property_portal_device_parity.py`
# (spec task 4.7), which imports BOTH modules and compares required bytes and
# the budget verdict over a grid of (arch, weights, utilization, images): a
# configuration accepted at publish time must never be refused by the device
# for a reason the portal could have predicted. If you edit a value here,
# edit the portal's and re-run that test.
#
# NOTE (spec task 3.2 lands the portal side): the portal file currently holds
# only `DEVICE_MEMORY_PROFILE_BYTES` and `MINIMUM_KV_CACHE_BYTES`; task 3.2
# adds the activation/co-tenancy constants below to it with the same values
# and the same provenance. Until it lands, the values here are the ones
# recorded in design.md Decision 2.

#: TOTAL device memory as the engine sees it, per Target_Architecture (NOT
#: "usable"): the `arm64_jp6` entry is reconciled against `free -g` total
#: 29 GB and vLLM's own four profiling terms summing to ≈29.95 GiB. Used only
#: for the co-tenancy cap and for portal parity — the preflight's own budget
#: arm uses the device's REAL `MemTotal`.
DEVICE_MEMORY_PROFILE_BYTES: Dict[str, int] = {
    'arm64_jp6': 30 * GIB,   # 32 GB Orin class
    'arm64_jp5': 30 * GIB,   # only reachable when JP5_VLLM_ENABLED
    'arm64_jp7': 120 * GIB,  # 128 GB Thor class
}

#: Serving-margin floor for KV cache — NOT a hard load threshold: the
#: incident device demonstrably served this model at 2.95x concurrency for
#: 4096 tokens with 0.65 GiB of KV. Breaching it is a thin-margin WARNING.
MINIMUM_KV_CACHE_BYTES = 1 * GIB

#: Conservative floor for the activation/profiling peak, for small models
#: where a fraction-of-weights term would round to nothing. ESTIMATE.
ACTIVATION_FLOOR_BYTES = 2 * GIB

#: Activation peak as a fraction of weights. Calibrated to the single
#: measured point: 4.92 GiB peak against 6.47 GiB of weights = 0.76, at
#: `enforce_eager=true`, `max_model_len=4096`. ESTIMATE — every message that
#: quotes the allowance must label it one.
ACTIVATION_WEIGHT_FRACTION = 0.75

#: Extra activation cost per additional image per prompt. UNMEASURED and
#: deliberately high, so two-image configurations must be sized explicitly
#: ([HARDWARE] H8 calibrates it).
MULTIMODAL_IMAGE_INCREMENT = 1.0

#: Memory held by other consumers of the same unified memory before vLLM
#: starts. `arm64_jp6` MEASURED: 3,909,200 + 1,030,612 + 921,184 kB of ONNX
#: Triton python-backend stubs plus the containers (`free -g` showed 6 GB used
#: at a clean backend restart with no engine). `arm64_jp7` is an ESTIMATE
#: (thor1 co-residency was not measured).
CO_TENANCY_RESERVATION_BYTES: Dict[str, int] = {
    'arm64_jp6': 6 * GIB,
    'arm64_jp5': 6 * GIB,
    'arm64_jp7': 8 * GIB,
}

#: Images per prompt when the authored configuration does not say. vLLM's own
#: default. The device NEVER injects a different value (defect 1.4).
DEFAULT_IMAGES_PER_PROMPT = 1

#: Default when the engine configuration omits the setting — mirrors
#: `model_import.ENGINE_DEFAULTS['gpu_memory_utilization']`.
DEFAULT_GPU_MEMORY_UTILIZATION = 0.5

#: PROPOSED THRESHOLD (design Open question 5, not measured): how much
#: available memory may legitimately fail to come back after a failed load
#: attempt before the Starvation_Latch is set. Reclaim itself is unchanged.
RECLAIM_TOLERANCE_BYTES = int(0.5 * GIB)

#: PROPOSED THRESHOLD (design Open question 5, not measured): a load that
#: reaches READY below this maximum concurrency is one retry from failing and
#: is reported as a thin margin. The incident's serving load was 2.95x.
THIN_MARGIN_CONCURRENCY = 2.0

#: Stable prefix of every preflight refusal reason. `vllm_model_prep.py`
#: duplicates this literal and matches it BEFORE `KV_CACHE_HINT_MARKERS`
#: (the diagnostic legitimately contains the string `gpu_memory_utilization`,
#: which would otherwise trigger the KV-OOM unload->reload recovery for a load
#: that never allocated anything). A host test pins the two copies equal.
PREFLIGHT_REFUSED_MARKER = "preflight-refused:"


@dataclass(frozen=True)
class MemoryReading:
    """One `/proc/meminfo` observation, in bytes."""
    total_bytes: int
    available_bytes: int


@dataclass(frozen=True)
class StarvationLatch:
    """Per-backend-life record that a failed attempt's memory did not come
    back (design Decision 5). Pure data: the manager owns the lifecycle
    (set on a non-recovering failure, cleared by an explicit unload) and
    passes it here so the preflight can refuse P3 instead of retrying into a
    starved device. Not persisted, no tombstone interaction."""
    model_name: str
    available_before_bytes: int
    available_after_bytes: int

    @property
    def lost_bytes(self) -> int:
        return max(self.available_before_bytes - self.available_after_bytes, 0)


@dataclass
class DeviceFitVerdict:
    """The preflight verdict. ``ok`` False means refuse before constructing an
    engine, with ``refusal_reason`` (prefixed by
    :data:`PREFLIGHT_REFUSED_MARKER`) as the FAILED reason. ``unverified``
    means a term could not be measured and the check ran against a documented
    lower bound rather than a guessed number — the same honesty rule the
    portal's Fit_Check uses for an undeterminable Weight_Estimate."""
    ok: bool
    refusal_reason: Optional[str] = None
    terms: Dict[str, Any] = field(default_factory=dict)
    unverified: bool = False


# ---------------------------------------------------------------------------
# /proc/meminfo
# ---------------------------------------------------------------------------

def _default_proc_meminfo_reader() -> str:
    """Read :data:`PROC_MEMINFO_PATH` (looked up at call time, so tests can
    redirect it). Returns the raw text; any OS error propagates to
    :func:`read_memory`, which degrades to ``None``."""
    with open(PROC_MEMINFO_PATH, "r") as handle:
        return handle.read()


def _parse_meminfo(text: str) -> Optional[MemoryReading]:
    """Parse `MemTotal` / `MemAvailable` out of `/proc/meminfo` text.

    The kernel writes ``MemTotal:       30592348 kB``. Both fields must be
    present, carry a recognised unit and be positive; anything else (missing
    key, garbage value, missing unit, empty file) yields ``None`` so the
    caller can degrade to "unverified". A unit-less line is REFUSED rather
    than assumed: guessing the scale of a memory figure by a factor of 1024
    is exactly the kind of invented number this module never produces.
    """
    values: Dict[str, int] = {}
    for line in text.splitlines():
        key, separator, remainder = line.partition(":")
        if not separator:
            continue
        key = key.strip()
        if key not in ("MemTotal", "MemAvailable"):
            continue
        parts = remainder.split()
        if not parts:
            continue
        try:
            amount = int(parts[0])
        except (TypeError, ValueError):
            continue
        unit = parts[1].lower() if len(parts) > 1 else ""
        if unit in ("kb", "kib"):
            multiplier = 1024
        elif unit in ("mb", "mib"):
            multiplier = 1024 ** 2
        elif unit in ("b", "bytes"):
            multiplier = 1
        else:
            continue  # unrecognised or absent unit: do not guess the scale
        values[key] = amount * multiplier

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None or total <= 0 or available < 0:
        return None
    return MemoryReading(total_bytes=int(total), available_bytes=int(available))


def read_memory(reader: Optional[Callable[[], str]] = None
                ) -> Optional[MemoryReading]:
    """Read the device's memory state: ``MemTotal`` / ``MemAvailable`` from
    `/proc/meminfo`, in bytes.

    ``reader`` is the injection seam every host test uses: any callable
    returning `/proc/meminfo`-shaped text. ``None`` selects the module-level
    :func:`_default_proc_meminfo_reader` **at call time** (rather than binding
    it as a default argument value) so monkeypatching the module attribute
    reaches this function too.

    Returns ``None`` when the file cannot be read or the two fields cannot be
    parsed — callers degrade to "unverified" and NEVER raise (a preflight must
    not be able to break a load it merely wanted to check).
    """
    try:
        text = (reader or _default_proc_meminfo_reader)()
    except Exception:  # noqa: BLE001 - unreadable device state is "unverified"
        logger.warning("Could not read %s; the vLLM memory preflight will "
                       "run unverified", PROC_MEMINFO_PATH, exc_info=True)
        return None
    try:
        return _parse_meminfo(text if isinstance(text, str) else str(text))
    except Exception:  # noqa: BLE001 - same degradation for a hostile body
        logger.warning("Could not parse %s; the vLLM memory preflight will "
                       "run unverified", PROC_MEMINFO_PATH, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Engine-arg readers (tolerant: this module never raises)
# ---------------------------------------------------------------------------

def images_per_prompt(engine_args: Optional[Mapping[str, Any]]) -> int:
    """Effective images per prompt from the AUTHORED ``limit_mm_per_prompt``
    (``{"image": N}``), the term the activation allowance scales with.

    Malformed, missing, non-integer, boolean or out-of-range values fall back
    to :data:`DEFAULT_IMAGES_PER_PROMPT` — the device never invents a larger
    multimodal limit than the configuration states (defect 1.4).
    """
    try:
        limit = (engine_args or {}).get("limit_mm_per_prompt")
        if isinstance(limit, Mapping):
            raw = limit.get("image")
        else:
            raw = limit
        if isinstance(raw, bool) or raw is None:
            return DEFAULT_IMAGES_PER_PROMPT
        images = int(raw)
        return images if images >= 1 else DEFAULT_IMAGES_PER_PROMPT
    except Exception:  # noqa: BLE001 - tolerant by contract
        return DEFAULT_IMAGES_PER_PROMPT


def gpu_memory_utilization(engine_args: Optional[Mapping[str, Any]]) -> float:
    """``gpu_memory_utilization`` from the staged args (``Decimal``/int/float
    accepted), defaulting to :data:`DEFAULT_GPU_MEMORY_UTILIZATION`. Values
    outside ``(0, 1]`` fall back to the default: they cannot be what the
    device will apply, so refusing on them would be a guess."""
    try:
        raw = (engine_args or {}).get("gpu_memory_utilization",
                                      DEFAULT_GPU_MEMORY_UTILIZATION)
        util = float(raw)
        if not 0.0 < util <= 1.0:
            return DEFAULT_GPU_MEMORY_UTILIZATION
        return util
    except Exception:  # noqa: BLE001 - tolerant by contract
        return DEFAULT_GPU_MEMORY_UTILIZATION


# ---------------------------------------------------------------------------
# The sizing model (mirrored from the portal — see the constants block)
# ---------------------------------------------------------------------------

def activation_allowance(weights_bytes: Optional[int],
                         images: int = DEFAULT_IMAGES_PER_PROMPT) -> int:
    """Estimated PyTorch activation/profiling peak vLLM charges against the
    budget:

        max(ACTIVATION_FLOOR_BYTES, ACTIVATION_WEIGHT_FRACTION * weights)
        * (1 + MULTIMODAL_IMAGE_INCREMENT * (images - 1))

    An ESTIMATE, and every message that quotes it says so. It is used only
    conservatively (to refuse), never permissively.
    """
    try:
        weights = max(int(weights_bytes or 0), 0)
    except Exception:  # noqa: BLE001 - tolerant by contract
        weights = 0
    try:
        count = max(int(images), 1)
    except Exception:  # noqa: BLE001 - tolerant by contract
        count = DEFAULT_IMAGES_PER_PROMPT
    base = max(ACTIVATION_FLOOR_BYTES, ACTIVATION_WEIGHT_FRACTION * weights)
    multiplier = 1.0 + MULTIMODAL_IMAGE_INCREMENT * (count - 1)
    return int(base * multiplier)


def co_tenancy_reservation_bytes(arch: Optional[str] = None,
                                 total_bytes: Optional[int] = None) -> int:
    """Memory other consumers of the same unified memory hold before vLLM
    starts. Keyed by Target_Architecture when known; otherwise inferred from
    the device's total (a Thor-class total takes the JP7 entry, everything
    else the measured JP6 entry)."""
    if arch and arch in CO_TENANCY_RESERVATION_BYTES:
        return CO_TENANCY_RESERVATION_BYTES[arch]
    try:
        if total_bytes is not None and int(total_bytes) >= 64 * GIB:
            return CO_TENANCY_RESERVATION_BYTES['arm64_jp7']
    except Exception:  # noqa: BLE001 - tolerant by contract
        pass
    return CO_TENANCY_RESERVATION_BYTES['arm64_jp6']


def fraction_cap(arch: Optional[str] = None,
                 total_bytes: Optional[int] = None) -> Optional[float]:
    """The largest ``gpu_memory_utilization`` that does not, by construction,
    claim memory the co-tenants already hold:
    ``(total - co_tenancy_reservation) / total``. JP6: ``(30-6)/30 = 0.80``.

    Uses the device's measured total when given (the number the device will
    actually apply the fraction to), else the architecture's profile entry.
    Returns ``None`` when neither is available (unknown architecture, no
    reading) — callers then omit the cap sentence rather than invent one.
    """
    try:
        total = int(total_bytes) if total_bytes else 0
        if total <= 0:
            if not arch or arch not in DEVICE_MEMORY_PROFILE_BYTES:
                return None
            total = DEVICE_MEMORY_PROFILE_BYTES[arch]
        reservation = co_tenancy_reservation_bytes(arch, total)
        if reservation >= total:
            return 0.0
        return (total - reservation) / float(total)
    except Exception:  # noqa: BLE001 - tolerant by contract
        return None


def required_bytes(weights_bytes: Optional[int],
                   images: int = DEFAULT_IMAGES_PER_PROMPT) -> int:
    """``weights + activation_allowance + KV floor`` — the same required-bytes
    term the portal's corrected Fit_Check computes (Property 8).

    When the weights are undeterminable the weights-dependent terms degrade to
    the ``ACTIVATION_FLOOR + KV floor`` LOWER BOUND (never a guessed weight),
    and the verdict that uses it is marked ``unverified``.
    """
    try:
        weights = max(int(weights_bytes or 0), 0)
    except Exception:  # noqa: BLE001 - tolerant by contract
        weights = 0
    return weights + activation_allowance(weights, images) \
        + MINIMUM_KV_CACHE_BYTES


# ---------------------------------------------------------------------------
# Weights on disk
# ---------------------------------------------------------------------------

#: Weight-file suffixes summed by :func:`estimate_weights_on_disk`.
WEIGHT_FILE_SUFFIXES: Tuple[str, ...] = (".safetensors", ".bin", ".gguf")


def default_hf_cache_roots() -> Tuple[str, ...]:
    """Hugging Face hub cache roots to search for a repo-id model, resolved
    from the environment AT CALL TIME (the device sets
    ``HF_HOME=/aws_dda/hf_cache`` in `src/docker-compose.yaml`)."""
    roots: List[str] = []
    hub_cache = os.environ.get("HF_HUB_CACHE")
    if hub_cache:
        roots.append(hub_cache)
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        roots.append(os.path.join(hf_home, "hub"))
    roots.append(os.path.join(os.path.expanduser("~"), ".cache",
                              "huggingface", "hub"))
    unique: List[str] = []
    for root in roots:
        if root and root not in unique:
            unique.append(root)
    return tuple(unique)


def _weight_bytes_under(directory: str) -> int:
    """Apparent total of every weight file under ``directory``. Unreadable
    entries are skipped (a partial sum is still a useful lower bound; a
    hostile tree can never raise here)."""
    total = 0
    try:
        walker = os.walk(directory, followlinks=False)
    except Exception:  # noqa: BLE001 - tolerant by contract
        return 0
    for dirpath, _dirnames, filenames in walker:
        for filename in filenames:
            if not filename.endswith(WEIGHT_FILE_SUFFIXES):
                continue
            try:
                total += os.stat(os.path.join(dirpath, filename)).st_size
            except OSError:
                continue
    return total


def _hf_snapshot_weight_bytes(folder: str) -> int:
    """Weight bytes of ONE snapshot of a `models--{org}--{name}` cache folder.

    Snapshots share blobs by symlink, so summing every snapshot would
    double-count: prefer the revision named by ``refs/main``, else the
    snapshot with the largest weight total.
    """
    snapshots = os.path.join(folder, "snapshots")
    if not os.path.isdir(snapshots):
        # A cache folder without snapshots (or an unusual layout): fall back
        # to whatever weight files are present.
        return _weight_bytes_under(folder)

    try:
        revision = ""
        main_ref = os.path.join(folder, "refs", "main")
        if os.path.isfile(main_ref):
            with open(main_ref, "r") as handle:
                revision = handle.read().strip()
        if revision:
            pinned = os.path.join(snapshots, revision)
            if os.path.isdir(pinned):
                total = _weight_bytes_under(pinned)
                if total > 0:
                    return total
        return max(
            [_weight_bytes_under(os.path.join(snapshots, name))
             for name in sorted(os.listdir(snapshots))
             if os.path.isdir(os.path.join(snapshots, name))] or [0]
        )
    except Exception:  # noqa: BLE001 - tolerant by contract
        return 0


def estimate_weights_on_disk(engine_args: Optional[Mapping[str, Any]],
                             hf_cache_roots: Optional[Iterable[str]] = None
                             ) -> Optional[int]:
    """Size the model's weights from the device's own disk — the honest
    device-side counterpart of the portal's Weight_Estimate.

    Two shapes of the staged ``model`` argument are understood:

    - a local directory (the S3-sourced record's rewritten path): the sum of
      every ``*.safetensors`` / ``*.bin`` / ``*.gguf`` file under it;
    - a Hugging Face repo id (``org/name``): the
      ``models--{org}--{name}`` snapshot under ``hf_cache_roots``
      (defaulting to :func:`default_hf_cache_roots`).

    Returns ``None`` when neither route yields bytes — a model that has not
    been pulled yet, an unreadable tree, a missing/blank ``model``. ``None``
    means "undeterminable": the caller degrades to the documented lower bound
    and marks the verdict ``unverified``. It never guesses and never raises.
    """
    try:
        model = (engine_args or {}).get("model")
        if not isinstance(model, str) or not model.strip():
            return None
        model = model.strip()

        if os.path.isdir(model):
            total = _weight_bytes_under(model)
            return total if total > 0 else None

        if os.path.exists(model):  # a single-file checkpoint
            try:
                size = os.stat(model).st_size
            except OSError:
                return None
            return size if size > 0 else None

        org, separator, name = model.partition("/")
        if separator and name and "/" not in name:
            folder_name = "models--{}--{}".format(org, name)
        elif not separator and org:
            folder_name = "models--{}".format(org)
        else:
            return None

        for root in (hf_cache_roots if hf_cache_roots is not None
                     else default_hf_cache_roots()):
            if not root:
                continue
            folder = os.path.join(str(root), folder_name)
            if not os.path.isdir(folder):
                continue
            total = _hf_snapshot_weight_bytes(folder)
            if total > 0:
                return total
        return None
    except Exception:  # noqa: BLE001 - "undeterminable", never an exception
        logger.warning("Could not size the vLLM model's weights on disk; the "
                       "memory preflight will run unverified", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The preflight verdict
# ---------------------------------------------------------------------------

def format_gib(num_bytes: Optional[float]) -> str:
    """Render a byte count as GiB with two decimals (the portal's wording)."""
    try:
        return "{:.2f} GiB".format(float(num_bytes or 0) / GIB)
    except Exception:  # noqa: BLE001 - tolerant by contract
        return "unknown"


def _remediation_menu(engine_args: Mapping[str, Any],
                      images: int,
                      util: float,
                      cap: Optional[float],
                      total_bytes: Optional[int],
                      needed_bytes: int) -> str:
    """Decision 3's ordered remediation menu, one line.

    Order is the whole point (defect 1.3): the co-tenancy hazard first, then
    the remediations that reduce OUR demand, and only last — quantified and
    bounded by the cap — raising the fraction. Nothing here ever advises
    LOWERING ``gpu_memory_utilization`` as a cure for insufficient KV.
    """
    max_model_len = (engine_args or {}).get("max_model_len")
    reservation = co_tenancy_reservation_bytes(total_bytes=total_bytes)
    parts = [
        "This device shares unified memory with the co-resident ONNX GPU "
        "models and gpu_memory_utilization is a fraction of TOTAL memory, so "
        "a larger fraction takes memory those models are already using.",
        "Reduce demand first: bound limit_mm_per_prompt.image (effective "
        "{}), reduce max_model_len (staged {}), choose a smaller or more "
        "quantized model, or free device memory by stopping unused model "
        "components.".format(images, max_model_len
                             if max_model_len is not None else "unset"),
    ]
    if cap is None:
        return " ".join(parts)
    if util >= cap:
        parts.append(
            "Raising gpu_memory_utilization is unsafe here: the staged "
            "{:g} already meets or exceeds the {:.2f} co-tenancy cap for this "
            "device ({} total minus {} held by co-resident models).".format(
                util, cap, format_gib(total_bytes), format_gib(reservation)))
        return " ".join(parts)

    sentence = (
        "gpu_memory_utilization may be raised to at most {:.2f} on this "
        "device ({} total minus {} held by co-resident models), and the "
        "budget you need is {}".format(
            cap, format_gib(total_bytes), format_gib(reservation),
            format_gib(needed_bytes)))
    if total_bytes:
        needed_fraction = needed_bytes / float(total_bytes)
        if needed_fraction <= cap:
            sentence += " — i.e. at least {:.2f}.".format(needed_fraction)
        else:
            sentence += (" — which exceeds that cap, so the fraction cannot "
                         "buy this load safely.")
    else:
        sentence += "."
    parts.append(sentence)
    return " ".join(parts)


def evaluate_device_fit(engine_args: Optional[Mapping[str, Any]],
                        reading: Optional[MemoryReading],
                        weights_bytes: Optional[int] = None,
                        latch: Optional[StarvationLatch] = None,
                        model_name: Optional[str] = None,
                        arch: Optional[str] = None) -> DeviceFitVerdict:
    """Decide whether to attempt this load AT ALL, before any allocation.

    Three refusal conditions, any one of which refuses (design Decision 4):

    - **P1 starvation** — ``available < weights + activation_allowance +
      KV_floor``: the memory the device actually has right now cannot hold the
      requirement, whatever the configured fraction says.
    - **P2 budget** — ``util × MemTotal < weights + activation_allowance +
      KV_floor``: the portal's condition A re-evaluated against the device's
      REAL total (the incident: ``0.4 × 29.95 GiB = 11.98 GiB`` against a
      12.38 GiB requirement).
    - **P3 latch** — a previous failed attempt's memory did not come back in
      this backend life; retrying into a starved device only deepens the
      cascade (defect 1.5).

    When ``weights_bytes`` is ``None`` the weights-dependent arms degrade to
    the ``ACTIVATION_FLOOR + KV floor`` lower bound and the verdict is marked
    ``unverified`` — never a guessed number. When ``reading`` is ``None``
    nothing is measurable, so P1/P2 cannot fire: the verdict is ``ok`` and
    ``unverified`` (a preflight that cannot measure must not refuse), while P3
    still applies because it needs no fresh reading.

    Returns a :class:`DeviceFitVerdict`; never raises.
    """
    args: Mapping[str, Any] = engine_args or {}
    name = model_name or str(args.get("model") or "unknown")
    images = images_per_prompt(args)
    util = gpu_memory_utilization(args)
    unverified = weights_bytes is None
    weights = 0 if unverified else max(int(weights_bytes or 0), 0)
    activation = activation_allowance(weights, images)
    required = weights + activation + MINIMUM_KV_CACHE_BYTES

    total = reading.total_bytes if reading is not None else None
    available = reading.available_bytes if reading is not None else None
    budget = int(util * total) if total else None
    cap = fraction_cap(arch, total)

    terms: Dict[str, Any] = {
        "model": name,
        "weights_bytes": None if unverified else weights,
        "activation_bytes": activation,
        "kv_floor_bytes": MINIMUM_KV_CACHE_BYTES,
        "required_bytes": required,
        "available_bytes": available,
        "total_bytes": total,
        "budget_bytes": budget,
        "gpu_memory_utilization": util,
        "images_per_prompt": images,
        "co_tenancy_bytes": co_tenancy_reservation_bytes(arch, total),
        "fraction_cap": cap,
        "unverified": unverified,
        "failed_conditions": [],
    }

    if latch is not None:
        terms["failed_conditions"] = ["latch"]
        reason = (
            "{marker} vLLM model '{name}' is not retried: a previous failed "
            "load in this backend life did not return its memory "
            "(available before {before}, after {after} — {lost} did not come "
            "back, more than the {tolerance} reclaim tolerance), so this "
            "device is starved and a further attempt would only deepen the "
            "cascade. Recovery requires a backend container restart; the "
            "computed requirement for this model is {required} = weights "
            "{weights} + activation allowance {activation} (ESTIMATE, {images}"
            " image(s)) + KV-cache floor {floor}. {menu}"
        ).format(
            marker=PREFLIGHT_REFUSED_MARKER,
            name=name,
            before=format_gib(latch.available_before_bytes),
            after=format_gib(latch.available_after_bytes),
            lost=format_gib(latch.lost_bytes),
            tolerance=format_gib(RECLAIM_TOLERANCE_BYTES),
            required=format_gib(required),
            weights="undeterminable (lower bound used)" if unverified
                    else format_gib(weights),
            activation=format_gib(activation),
            images=images,
            floor=format_gib(MINIMUM_KV_CACHE_BYTES),
            menu=_remediation_menu(args, images, util, cap, total, required),
        )
        return DeviceFitVerdict(ok=False, refusal_reason=reason, terms=terms,
                                unverified=unverified)

    if reading is None:
        # Nothing measurable: the preflight declines to judge rather than
        # refusing a load that might well succeed.
        return DeviceFitVerdict(ok=True, terms=terms, unverified=True)

    failed: List[str] = []
    if available is not None and available < required:
        failed.append("starvation")
    if budget is not None and budget < required:
        failed.append("budget")
    terms["failed_conditions"] = failed
    if not failed:
        return DeviceFitVerdict(ok=True, terms=terms, unverified=unverified)

    reason = (
        "{marker} vLLM model '{name}' cannot be loaded on this device now: "
        "measured available memory {available} (MemAvailable) and device "
        "budget {budget} (gpu_memory_utilization={util:g} x MemTotal {total}) "
        "against a computed requirement of {required} = weights {weights} + "
        "activation allowance {activation} (ESTIMATE, {images} image(s)) + "
        "KV-cache floor {floor}; failed condition(s): {failed}"
        "{unverified_note}. {menu}"
    ).format(
        marker=PREFLIGHT_REFUSED_MARKER,
        name=name,
        available=format_gib(available),
        budget=format_gib(budget),
        util=util,
        total=format_gib(total),
        required=format_gib(required),
        weights="undeterminable (lower bound used)" if unverified
                else format_gib(weights),
        activation=format_gib(activation),
        images=images,
        floor=format_gib(MINIMUM_KV_CACHE_BYTES),
        failed=", ".join(failed),
        unverified_note=(
            "; the weights could not be sized on disk, so this is the "
            "ACTIVATION_FLOOR + KV floor lower bound and the check is "
            "UNVERIFIED" if unverified else ""),
        menu=_remediation_menu(args, images, util, cap, total, required),
    )
    return DeviceFitVerdict(ok=False, refusal_reason=reason, terms=terms,
                            unverified=unverified)


def preflight(engine_args: Optional[Mapping[str, Any]],
              reader: Optional[Callable[[], str]] = None,
              hf_cache_roots: Optional[Sequence[str]] = None,
              latch: Optional[StarvationLatch] = None,
              model_name: Optional[str] = None,
              arch: Optional[str] = None) -> DeviceFitVerdict:
    """Convenience composition for the manager's call site: read memory, size
    the weights on disk, and evaluate. Both reads are injectable; neither can
    raise out of here."""
    reading = read_memory(reader)
    weights = estimate_weights_on_disk(engine_args, hf_cache_roots)
    return evaluate_device_fit(engine_args, reading, weights, latch,
                               model_name=model_name, arch=arch)
