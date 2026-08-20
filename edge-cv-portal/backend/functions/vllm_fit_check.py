"""
vLLM preflight fit check — pure sizing math, no AWS dependencies.

Decides whether a vLLM model can be loaded inside the GPU memory budget
granted by ``gpu_memory_utilization`` on each target device architecture
(Device_Memory_Profile), over the terms vLLM actually charges against that
budget: the weights, the non-torch/co-tenant residency and the
activation/profiling peak —
plus a cap on the fraction itself, because on a Jetson's unified memory the
budget is a fraction of TOTAL device memory that other resident consumers
(the co-resident ONNX GPU models) are already holding.

    required := weights + NON_TORCH_MEMORY_BYTES
                + activation_allowance(weights, multimodal_units)

``MINIMUM_KV_CACHE_BYTES`` is NOT in that sum, and NO KV term is: the KV cache
is what the budget LEAVES OVER, so every verdict states the predicted remainder
(``kv_headroom_bytes`` == ``budget - required``) and WARNS when that remainder
falls below the serving-margin floor (amended 2026-08-19, spec task 14 /
H9 — the shipped code charged it as a hard term, which refused the exact
configuration LocalServer 1.0.59 demonstrably served).

Imported by model_import.py (registration/update warnings), models.py, and
greengrass_publish.py (publish gate), so this module must stay stdlib-only
and must never raise out of its public API.

SPEC: this module is the corrected sizing model of
`jp6-vllm-kv-cache-oom-regression` (design Decision 2 / Decision 3, File 1),
which REVISES the model specified by `vllm-sizing-and-packaging-errors`
Requirements 3.1/3.6/3.8/3.9. The shipped model was
``fits = util × profile[arch] >= weights + MINIMUM_KV_CACHE_BYTES``; for the
2026-08-17 `ryanorinagxdevkithomelabjp622` incident that reported 4.50 GiB of
slack (``0.4 × 30 GiB = 12.00 GiB`` against ``6.5 + 1 = 7.5 GiB``) for a load
whose device-measured KV remainder was **−7.83 GiB**, because it omitted the
activation/profiling peak (measured 4.92 GiB, ~41% of the budget) and knew
nothing about the ≈5.7 GiB of co-resident ONNX Triton stubs on the same
unified memory.

This module is also the **SINGLE SOURCE OF TRUTH** for the sizing constants
and the ``activation_allowance`` formula. `src/backend/vllm_runtime/
memory_budget.py` MIRRORS them (it cannot import a Lambda function module);
the two copies are pinned equal by the Property 8 parity test
`test/backend-test/jp6_vllm_kv_cache_oom/test_property_portal_device_parity.py`.
If you edit a value here, edit the mirror and re-run that test.

**Amended 2026-08-19 (measured on `ryanorinagxdevkithomelabjp622`,
LocalServer.arm64JP6 1.0.62).** The multimodal term of the allowance counts
the TOTAL authored multimodal UNITS (images + videos), not images alone,
because vLLM reserves its worst-case token budget per modality — its own
warning: "worst-case total number of multimodal tokens (32768) ... out of
which {'image': 16384, 'video': 16384} are reserved for multi-modal
embeddings". At ``gpu_memory_utilization = 0.55`` the same model profiled a
**2.47 GiB** activation peak with ``{"image": 1, "video": 0}`` (KV 6.43 GiB,
29.41x concurrency, READY) and **4.93 GiB** with ``{"image": 1}`` alone (KV
0.20 GiB, 0.89x, FAILED). An UNAUTHORED ``video`` key therefore costs a full
extra unit here, which is the honest, refusing direction.

The ratio those numbers imply is already what this module encodes (2 units
cost 2x one unit, MEASURED-CONFIRMED to within 0.01 GiB). They also showed
``ACTIVATION_WEIGHT_FRACTION`` to be ~2x too high PER UNIT, and that
recalibration is now APPLIED here and in the device mirror in ONE change
(0.75 -> 0.375, spec task 14 / H8), together with the ``required`` redesign
(the non-torch term added, and the KV floor REMOVED from ``required`` so it is
only the thin-margin warning threshold its own design always called it — task 14 / H9). The device mirror also adopted this
module's UNITS model in the same change, so Property 8 parity is EXACT again
in both authoring shapes.

HONEST NOTE ON DIRECTION: this change makes ``required`` SMALLER for the
incident model (12.28 -> 10.87 GiB at 6.45 GiB of weights, one unit), while
design Decision 2 states the corrected model is "deliberately more
conservative". That is not a reversal of intent. The shipped number was wrong
in BOTH directions at once: it omitted a term that is always consumed
(non-torch), double-counted activation (0.75 against a measured 0.375), and
hard-charged a floor its own design calls soft. The result is a more ACCURATE
model, and accuracy happens to be less strict here.

Publish-time math is a NECESSARY, not sufficient, condition: the non-torch
term vLLM charges against its own budget swung 8.34 GiB between two attempts
four minutes apart on the same device. The device-side preflight
(`memory_budget.evaluate_device_fit`) is the truth check.
"""
import json
import logging
import math
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

GIB = 1024 ** 3

# ---------------------------------------------------------------------------
# Sizing constants (SINGLE SOURCE OF TRUTH — mirrored by the device's
# src/backend/vllm_runtime/memory_budget.py; keep the two numerically equal)
# ---------------------------------------------------------------------------

# TOTAL device GPU memory as the engine sees it (unified memory on Jetson),
# per Target_Architecture, in bytes — NOT "usable" and NOT
# "available-to-vLLM". `gpu_memory_utilization` is a fraction the DEVICE
# applies to its real total, so the profile must be a total for the portal's
# budget to be the number vLLM targets.
# VALUES UNCHANGED from `vllm-sizing-and-packaging-errors` Requirement 3.8
# (which mandates the `arm64_jp6` 30 GiB entry); only the semantics are
# corrected. Reconciled against the incident device: `free -g` total 29 GB,
# and vLLM's own four profiling terms (weights 6.47 + non_torch −0.05 +
# activation peak 4.92 + KV 0.65, inside `util = 0.4`) sum to a total of
# ≈29.95 GiB. ~6 GB of that total is resident BEFORE vLLM starts, which is
# what CO_TENANCY_RESERVATION_BYTES below models.
DEVICE_MEMORY_PROFILE_BYTES = {
    'arm64_jp6': 30 * GIB,   # 32 GB Orin class, ≈29.95 GiB as the engine sees it
    'arm64_jp5': 30 * GIB,   # only reachable when JP5_VLLM_ENABLED
    'arm64_jp7': 120 * GIB,  # 128 GB Thor class
}

# Serving-margin floor for KV cache — the WARNING THRESHOLD ONLY. It is NOT a
# term in `required` and it never refuses a configuration (amended 2026-08-19,
# spec task 14 / H9). `required` charges NO KV term at all: the KV cache is
# what the budget LEAVES OVER once the weights, the non-torch residency and
# the activation peak are paid, which is exactly how vLLM computes it, so the
# honest surface is the PREDICTED REMAINDER (`kv_headroom_bytes`) plus a
# warning when that remainder falls under this floor.
#
# Value unchanged at 1 GiB, and its documented meaning unchanged: design
# Decision 2 always called it "a *serving-margin* floor, not a hard load
# threshold" and Decision 6 always made a sub-floor remainder the Thin_Margin
# WARNING. What changed is that the SHIPPED code charged it as a hard term in
# `required` — and that is what refused, at `gpu_memory_utilization = 0.4`,
# the exact configuration LocalServer 1.0.59 demonstrably SERVED (0.65 GiB of
# KV at 2.95x maximum concurrency for 4096 tokens). Same constant, two
# contradictory roles; it now keeps only the role its own design gave it.
#
# SUPERSEDED IMPLEMENTATION, recorded so the reasoning is auditable: an
# intermediate version of this change kept a HARD `KV_VIABILITY_FLOOR_BYTES =
# 0.25 GiB` term in `required`, sitting between the 0.65 GiB that served and
# the -0.22 GiB that failed. The operator took the simpler decision: no hard
# KV term. The measured -0.22 GiB failure is not admitted by accident either —
# that load ran with video UNBOUNDED, i.e. TWO multimodal units, and the units
# model charges it 4.84 GiB of activation, leaving a predicted remainder of
# 0.19 GiB that trips the thin-margin warning below.
MINIMUM_KV_CACHE_BYTES = 1 * GIB

# The non-torch / co-tenant residency vLLM subtracts from the SAME budget on
# every load, and which the shipped `required` OMITTED entirely — a
# PROPOSED THRESHOLD / ESTIMATE, under the same discipline as
# ACTIVATION_WEIGHT_FRACTION and RECLAIM_TOLERANCE_BYTES: pinned by a host test
# so a silent drift is visible, labelled an ESTIMATE in every message that
# quotes it, and owned for calibration by spec task 14 / H8.
#
# WHY IT MUST BE CHARGED: vLLM's own profiling, verbatim from
# `ryanorinagxdevkithomelabjp622` at `gpu_memory_utilization = 0.45`, is
# "the current vLLM instance can use total_gpu_memory (29.96GiB) x
# gpu_memory_utilization (0.45) = 13.48GiB" / "model weights take 6.59GiB;
# non_torch_memory takes 2.18GiB; PyTorch activation peak memory takes
# 4.93GiB; the rest of the memory reserved for KV Cache is -0.22GiB." That
# load PASSED the shipped preflight and then FAILED for real, because the
# preflight modelled three of the four terms. A term that always consumes the
# budget cannot be absent from the model of it (bugfix.md 2.1: "SHALL model
# every term that consumes the `gpu_memory_utilization` budget").
#
# PROVENANCE — MEASURED on that one device across seven runs (vLLM's own
# `non_torch_memory takes ...` line): -0.05, 0.94, 0.98, 2.18, 3.67, 4.76,
# 8.29 GiB. The median of those seven is 2.18 GiB, and 2 GiB is the nearest
# round figure to it — deliberately the MIDDLE of the observed spread and NOT
# the 8.29 GiB worst case, because encoding the worst case as a certainty would
# refuse the configuration that demonstrably serves today (the same reasoning
# `fraction_cap` gives for modelling co-tenancy as a cap on the fraction rather
# than an addend to `required`).
#
# KNOWN LIMITATION, not papered over: a run whose non-torch residency is high
# (3.67, 4.76, 8.29 GiB observed) is UNDER-predicted. Tolerable because this
# term does not hard-fail a load: vLLM computes
# `KV = budget - weights - non_torch - activation`, so a high non_torch
# SHRINKS the KV cache rather than aborting, until KV goes negative (the
# measured -0.22 GiB). The COMPENSATING CONTROL is the thin-margin WARNING
# (MINIMUM_KV_CACHE_BYTES above, surfaced by the portal's `thin_margin` and by
# `manager.py` after a load reaches READY), which makes a thin outcome
# visible instead of silent. Calibration is owned by spec task 14 / H8, the
# same discipline ACTIVATION_WEIGHT_FRACTION and RECLAIM_TOLERANCE_BYTES
# already follow.
#
# ALTERNATIVE CONSIDERED AND DEFERRED: read the PREVIOUS load's actual
# `non_torch_memory` off the engine and charge that instead of an estimate.
# It is the accurate model and it is too much for this build (it needs engine
# introspection plumbed through the device preflight and a persisted
# per-device figure). Deferred, not rejected; owner is task 14 / H8.
NON_TORCH_MEMORY_BYTES = 2 * GIB

# Conservative floor for the activation/profiling peak, so small models —
# where a fraction-of-weights term would round to nothing — still carry an
# allowance. ESTIMATE.
ACTIVATION_FLOOR_BYTES = 2 * GIB

# Activation/profiling peak as a fraction of weights, PER MULTIMODAL UNIT.
# ESTIMATE — every message that quotes the allowance must label it one.
#
# RECALIBRATED 2026-08-19 (spec task 14 / H8): 0.75 -> 0.375. The 0.75 came
# from one measured point (`PyTorch activation peak memory takes 4.92GiB`
# against `model weights take 6.47GiB` = 0.76) which is now understood to have
# been a TWO-unit measurement — video was unbounded, so vLLM sized its
# worst case for both modalities. The per-unit pair measured on
# `ryanorinagxdevkithomelabjp622` (LocalServer.arm64JP6 1.0.62, same model,
# same `gpu_memory_utilization = 0.55`, 6.59 GiB of weights) is:
#   * ONE unit  (`{'image': 1, 'video': 0}`) -> activation peak 2.47 GiB
#   * TWO units (`{'image': 1}`, video unbounded) -> activation peak 4.93 GiB
# 2.47 / 6.59 = 0.375. At 6.45 GiB of weights and one unit the recalibrated
# coefficient gives `max(2.00, 0.375 x 6.45) = 2.42 GiB` against the measured
# 2.47 GiB — within 0.05 GiB. The old 0.75 predicted 4.94 / 9.89 GiB against
# the measured 2.47 / 4.93, i.e. exactly 2x in both cases.
#
# CONSEQUENCE worth knowing: at 0.375 the ACTIVATION_FLOOR_BYTES (2 GiB) now
# BINDS for every model under ~5.33 GiB of weights (2 / 0.375), so the floor —
# not the fraction — is what sizes small models' allowance.
ACTIVATION_WEIGHT_FRACTION = 0.375

# Extra activation cost per ADDITIONAL multimodal unit per prompt, as a
# fraction of the one-unit allowance.
#
# MEASURED-CONFIRMED for the image<->video UNIT step (2026-08-19, same model,
# same `gpu_memory_utilization = 0.55`): one unit (`{"image": 1, "video": 0}`)
# profiled a 2.47 GiB activation peak and two units (`{"image": 1}`, video
# unbounded at vLLM's own default) profiled 4.93 GiB — a 2:1 ratio, which is
# exactly what 1.0 encodes, to within 0.01 GiB. Value UNCHANGED because the
# measurement confirmed it; only ACTIVATION_WEIGHT_FRACTION moved.
#
# STILL UNMEASURED in the per-additional-IMAGE direction: no `image >= 2`
# configuration has ever been profiled on this hardware, so extrapolating the
# unit step to a second IMAGE remains an ESTIMATE (spec task 14 / H8).
# Deliberately high there, so a multi-image configuration must be sized
# explicitly rather than sneaking into an already-published model's budget
# (defect 1.4).
MULTIMODAL_IMAGE_INCREMENT = 1.0

# Memory held by OTHER consumers of the same unified memory before vLLM
# starts, per Target_Architecture.
# `arm64_jp6` MEASURED on the incident device (`ps -eo rss`, model loaded):
# 3,909,200 + 1,030,612 + 921,184 kB in the three ONNX Triton python-backend
# stubs ≈ 5.7 GiB, plus the backend/frontend containers; `free -g` showed
# 6 GB used at a clean backend restart with no engine.
# `arm64_jp7` is an ESTIMATE (thor1 co-residency was never measured), chosen
# where a JP6-style headroom analysis cannot flip a JP7 verdict at the
# utilizations in use.
CO_TENANCY_RESERVATION_BYTES = {
    'arm64_jp6': 6 * GIB,
    'arm64_jp5': 6 * GIB,
    'arm64_jp7': 8 * GIB,
}

# Images per prompt when the authored Engine_Configuration does not say —
# vLLM's own default, and the value
# `model_import.ENGINE_DEFAULTS['limit_mm_per_prompt']` authors. The device
# never injects a larger one (defect 1.4).
DEFAULT_IMAGES_PER_PROMPT = 1

# Videos per prompt when the authored Engine_Configuration does not bound them
# — vLLM's OWN per-modality default, which is 1, i.e. UNBOUNDED as far as this
# product is concerned. An absent `limit_mm_per_prompt.video` is therefore
# MORE expensive than an explicit `"video": 0`, and that asymmetry is the
# point: vLLM sizes its worst case from the limits it is given. Verbatim from
# the engine on `ryanorinagxdevkithomelabjp622` (LocalServer.arm64JP6 1.0.62,
# 2026-08-19): "worst-case total number of multimodal tokens (32768) ... out
# of which {'image': 16384, 'video': 16384} are reserved for multi-modal
# embeddings" — half of that worst case is video this product never sends
# (inputs are camera frames and folder images). Measured, same model, same
# `gpu_memory_utilization = 0.55`:
#   * {'image': 1, 'video': 0} -> activation peak 2.47 GiB, KV 6.43 GiB,
#     Maximum concurrency 29.41x, READY.
#   * {'image': 1}             -> activation peak 4.93 GiB, KV 0.20 GiB,
#     concurrency 0.89x, FAILED (max seq len 4096 > 3664 KV tokens).
# So bounding video HALVES the measured activation peak, and a configuration
# that does not bound it must be sized for both modalities.
DEFAULT_VIDEOS_PER_PROMPT = 1

# The modality sub-keys the authored `limit_mm_per_prompt` may bound, matching
# `model_import.LIMIT_MM_ACCEPTED_KEYS` (image 1..8, video 0..8).
MULTIMODAL_MODALITY_KEYS = ('image', 'video')

# Multimodal units an Engine_Configuration that authors NOTHING is sized for:
# one image plus one (unbounded) video, i.e. vLLM's own defaults. The authored
# default `{'image': 1, 'video': 0}` is ONE unit, which is why authoring it is
# what buys the KV cache back.
DEFAULT_MULTIMODAL_UNITS = (DEFAULT_IMAGES_PER_PROMPT
                            + DEFAULT_VIDEOS_PER_PROMPT)

# A passing verdict this close to the Fraction_Cap carries a 'near_cap' soft
# warning: the configuration fits, but has no room for co-tenancy growth.
NEAR_CAP_WARNING_MARGIN = 0.05

# On-GPU bytes per weight element for each supported dtype ('auto'
# resolves to a 16-bit dtype on the models we target).
DTYPE_BYTES = {'float32': 4, 'auto': 2, 'float16': 2, 'bfloat16': 2}

# Default when the engine configuration omits the setting — must match
# model_import.ENGINE_DEFAULTS['gpu_memory_utilization'].
DEFAULT_GPU_MEMORY_UTILIZATION = 0.5

# Hugging Face model-metadata endpoint; ?blobs=true adds per-file sizes.
HF_MODEL_API_URL = 'https://huggingface.co/api/models/{model_id}?blobs=true'

# Keep registration/update latency bounded (Requirement 3.2).
HF_FETCH_TIMEOUT_SECONDS = 5


@dataclass
class WeightEstimate:
    """Estimated on-GPU size of a model's weights (Weight_Estimate)."""
    total_bytes: int
    method: str          # 'safetensors_files' | 'param_count' | 's3_artifact'
    detail: str          # human-readable derivation


@dataclass
class FitFinding:
    """Result of the fit check for one target architecture.

    The first five fields are the ORIGINAL contract and are unchanged in
    name, order, type and meaning — `greengrass_publish` serializes findings
    with ``asdict`` and the frontend's ``VllmFitCheckFinding`` reads them, so
    every new term below is ADDITIVE and optional for consumers
    (jp6-vllm-kv-cache-oom-regression design Decision 2, step 5).
    """
    arch: str
    fits: bool
    budget_bytes: int            # gpu_memory_utilization * profile[arch]
    # weights + non-torch allowance + activation allowance. NO KV term is
    # charged (amended 2026-08-19: the non-torch term added, and the KV floor
    # removed from the sum — it is the thin-margin WARNING threshold applied
    # to `kv_headroom_bytes`, task 14 / H9)
    required_bytes: int
    message: str                 # names every term, its number, remediation
    # --- additive terms: the audit trail behind `fits` ---
    weights_bytes: int = 0
    activation_bytes: int = 0        # ESTIMATE (see activation_allowance)
    # The serving-margin floor, i.e. the thin-margin WARNING threshold. Kept
    # in its original field and with its original value; it is NO LONGER a
    # term in `required_bytes` (task 14 / H9).
    kv_floor_bytes: int = 0
    # ESTIMATE: the non-torch/co-tenant residency vLLM charges against the
    # same budget, and a term in `required_bytes`.
    non_torch_bytes: int = 0
    # budget - required_bytes: the KV cache this model predicts will remain.
    # Below `kv_floor_bytes` it is a thin margin (a warning, never a refusal).
    kv_headroom_bytes: int = 0
    co_tenancy_bytes: int = 0
    fraction_cap: Optional[float] = None
    images_per_prompt: int = DEFAULT_IMAGES_PER_PROMPT
    # Videos per prompt and the TOTAL multimodal units the allowance was
    # sized for (images + videos). Additive like every field above: an
    # unauthored `limit_mm_per_prompt.video` reports vLLM's own default of 1,
    # which is what makes it cost as much as an image.
    videos_per_prompt: int = DEFAULT_VIDEOS_PER_PROMPT
    multimodal_units: int = DEFAULT_MULTIMODAL_UNITS
    failed_conditions: List[str] = field(default_factory=list)  # budget|co_tenancy
    warnings: List[str] = field(default_factory=list)           # thin_margin|near_cap


def _format_gib(num_bytes: Any) -> str:
    """Render a byte count as GiB with two decimals (e.g. '14.25 GiB')."""
    try:
        return f"{float(num_bytes or 0) / GIB:.2f} GiB"
    except (TypeError, ValueError):  # never raise out of the public API
        return "unknown"


def _round_up_fraction(value: float) -> float:
    """Smallest 2-decimal fraction that is >= ``value`` (a fraction quoted as
    'at least' must never round DOWN below what is needed)."""
    return math.ceil(value * 100) / 100.0


def activation_allowance(weights_bytes: Any,
                         multimodal_units: int = 1) -> int:
    """Estimated PyTorch activation/profiling peak vLLM charges against the
    budget, in bytes:

        max(ACTIVATION_FLOOR_BYTES, ACTIVATION_WEIGHT_FRACTION × weights)
        × (1 + MULTIMODAL_IMAGE_INCREMENT × (units − 1))

    ``multimodal_units`` is the TOTAL of the authored per-modality limits
    (images + videos, :func:`multimodal_units`), not the image count alone:
    vLLM reserves its worst-case token budget per modality, so an unbounded
    video modality costs a whole extra unit (measured 2.47 -> 4.93 GiB on JP6,
    2026-08-19). One unit is the baseline allowance.

    A fraction of weights rather than something cleverer on purpose: a
    per-model-class table would need measurements we do not have, a fixed
    absolute allowance would be wrong by an order of magnitude across the
    0.5B–70B range, and a first-principles computation (hidden size × batch
    tokens × layers) needs config fields this module never fetches and would
    produce false precision. One calibrated coefficient tracks model scale,
    is trivially auditable in the message, and errs high.

    This is an ESTIMATE and every message that quotes it says so. Tolerant of
    hostile input (never raises): unusable values degrade to 0 weights and to
    one multimodal unit.
    """
    try:
        weights = max(int(weights_bytes or 0), 0)
    except (TypeError, ValueError):
        weights = 0
    try:
        units = max(int(multimodal_units), 1)
    except (TypeError, ValueError):
        units = 1
    base = max(ACTIVATION_FLOOR_BYTES, ACTIVATION_WEIGHT_FRACTION * weights)
    multiplier = 1.0 + MULTIMODAL_IMAGE_INCREMENT * (units - 1)
    return int(base * multiplier)


def required_bytes(weights_bytes: Any, multimodal_units: int = 1) -> int:
    """Every term vLLM charges against the ``gpu_memory_utilization`` budget,
    in bytes::

        weights + NON_TORCH_MEMORY_BYTES
                + activation_allowance(weights, multimodal_units)

    No KV term is charged (spec task 14 / H9). The KV cache is what the budget
    LEAVES OVER once these three are paid — exactly how vLLM computes it — so
    :func:`kv_headroom_bytes` states the predicted remainder and
    :data:`MINIMUM_KV_CACHE_BYTES` is the WARNING threshold applied to it.
    Charging that 1 GiB floor hard is what refused, at
    ``gpu_memory_utilization = 0.4``, the exact configuration LocalServer
    1.0.59 demonstrably served (0.65 GiB of KV at 2.95x concurrency for 4096
    tokens).

    Mirrored verbatim by ``memory_budget.required_bytes`` on the device and
    pinned equal by the Property 8 parity test. Never raises.
    """
    try:
        weights = max(int(weights_bytes or 0), 0)
    except (TypeError, ValueError):
        weights = 0
    return (weights + NON_TORCH_MEMORY_BYTES
            + activation_allowance(weights, multimodal_units))


def kv_headroom_bytes(budget_bytes: Any, weights_bytes: Any,
                      multimodal_units: int = 1) -> int:
    """The KV cache this model predicts will remain::

        budget - required_bytes(weights, multimodal_units)

    i.e. exactly the quantity vLLM prints as "the rest of the memory reserved
    for KV Cache". Below :data:`MINIMUM_KV_CACHE_BYTES` it is a THIN MARGIN
    (a warning, never a refusal); at or below zero the configuration is
    refused by :func:`evaluate_fit`'s condition A, which is the same
    comparison written the other way round. Mirrored on the device and pinned
    equal by the Property 8 parity test. Never raises."""
    try:
        budget = int(budget_bytes or 0)
    except (TypeError, ValueError):
        budget = 0
    return budget - required_bytes(weights_bytes, multimodal_units)


def co_tenancy_reservation_bytes(arch: Optional[str]) -> int:
    """Memory other consumers of the same unified memory hold before vLLM
    starts, for ``arch``. Falls back to the measured JP6 reservation for an
    unknown architecture (conservative; never raises)."""
    if arch and arch in CO_TENANCY_RESERVATION_BYTES:
        return CO_TENANCY_RESERVATION_BYTES[arch]
    return CO_TENANCY_RESERVATION_BYTES['arm64_jp6']


def fraction_cap(arch: Optional[str]) -> Optional[float]:
    """Largest ``gpu_memory_utilization`` that does not, by construction,
    claim memory the co-tenants already hold:
    ``(profile[arch] − CO_TENANCY_RESERVATION_BYTES[arch]) / profile[arch]``.
    JP6: ``(30 − 6)/30 = 0.80``. JP7: ``(120 − 8)/120 = 0.9333``.

    Co-tenancy is modelled as a cap on the FRACTION rather than an addend to
    ``required`` deliberately: vLLM charges resident foreign memory through
    its variable ``non_torch_memory`` term, which swung from −0.05 GiB to
    8.29 GiB on the same device four minutes apart, so adding a fixed 6 GiB
    to ``required`` would encode that worst case as a certainty and refuse
    the configuration that demonstrably serves today. The cap answers the
    question that IS deterministic: does this fraction, by construction,
    claim memory the co-tenants hold?

    Returns None for an architecture with no Device_Memory_Profile entry —
    callers then omit the cap rather than invent one. Never raises.
    """
    profile_bytes = DEVICE_MEMORY_PROFILE_BYTES.get(arch) if arch else None
    if not profile_bytes:
        return None
    reservation = co_tenancy_reservation_bytes(arch)
    if reservation >= profile_bytes:
        return 0.0
    return (profile_bytes - reservation) / float(profile_bytes)


def images_per_prompt(engine_configuration: Any) -> int:
    """Effective images per prompt from the AUTHORED ``limit_mm_per_prompt``
    (``{"image": N}``) — the term the activation allowance scales with
    (`model_import.ENGINE_DEFAULTS`, design Decision 1).

    Tolerant by contract: missing, malformed, ``Decimal``, boolean or
    out-of-range values fall back to :data:`DEFAULT_IMAGES_PER_PROMPT`. The
    Fit_Check never invents a larger multimodal limit than the configuration
    states, and never raises.
    """
    try:
        limit = (engine_configuration or {}).get('limit_mm_per_prompt')
        raw = limit.get('image') if isinstance(limit, dict) else limit
        if raw is None or isinstance(raw, bool):
            return DEFAULT_IMAGES_PER_PROMPT
        images = int(raw)
        return images if images >= 1 else DEFAULT_IMAGES_PER_PROMPT
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_IMAGES_PER_PROMPT


def video_is_authored(engine_configuration: Any) -> bool:
    """True when the Engine_Configuration explicitly bounds
    ``limit_mm_per_prompt.video`` with a usable integer.

    False means vLLM's own per-modality default (1) applies, i.e. the video
    modality is UNBOUNDED and is charged a full extra multimodal unit — the
    distinction the messages must state, because it is the difference between
    a 2.47 GiB and a 4.93 GiB measured activation peak. Never raises.
    """
    try:
        limit = (engine_configuration or {}).get('limit_mm_per_prompt')
        if not isinstance(limit, dict) or 'video' not in limit:
            return False
        raw = limit['video']
        if raw is None or isinstance(raw, bool):
            return False
        return int(raw) >= 0
    except (AttributeError, TypeError, ValueError):
        return False


def videos_per_prompt(engine_configuration: Any) -> int:
    """Effective videos per prompt from the AUTHORED ``limit_mm_per_prompt``
    (``{"video": N}``), the second modality the activation allowance scales
    with (`model_import.ENGINE_DEFAULTS`, design Decision 1 as amended
    2026-08-19).

    An absent, malformed, boolean or negative value falls back to
    :data:`DEFAULT_VIDEOS_PER_PROMPT` — vLLM's own default of 1, i.e.
    UNBOUNDED — so NOT authoring the key is deliberately MORE expensive than
    authoring ``"video": 0``. Zero is a legal authored value and the one the
    product's default uses. Never raises.
    """
    try:
        limit = (engine_configuration or {}).get('limit_mm_per_prompt')
        if not isinstance(limit, dict):
            # A non-dict limit is read as an image count by
            # `images_per_prompt`; it bounds no video modality at all.
            return DEFAULT_VIDEOS_PER_PROMPT
        raw = limit.get('video')
        if raw is None or isinstance(raw, bool):
            return DEFAULT_VIDEOS_PER_PROMPT
        videos = int(raw)
        return videos if videos >= 0 else DEFAULT_VIDEOS_PER_PROMPT
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_VIDEOS_PER_PROMPT


def multimodal_units(engine_configuration: Any) -> int:
    """TOTAL multimodal units per prompt the activation allowance is sized
    for: ``images_per_prompt + videos_per_prompt``.

    vLLM reserves its worst-case multimodal token budget PER MODALITY (its own
    warning: 32768 tokens, ``{'image': 16384, 'video': 16384}``), so the term
    that scales the activation peak is the total number of units, not the
    image count. Measured consequence on JP6 (2026-08-19, same model, same
    ``gpu_memory_utilization = 0.55``): ``{"image": 1, "video": 0}`` is ONE
    unit and profiled a 2.47 GiB peak (READY, 29.41x concurrency), while
    ``{"image": 1}`` is TWO units — video unbounded — and profiled 4.93 GiB
    (FAILED on KV cache). Always at least 1 (the image floor). Never raises.
    """
    return (images_per_prompt(engine_configuration)
            + videos_per_prompt(engine_configuration))


def _gpu_memory_utilization(engine_configuration: Any) -> float:
    """``gpu_memory_utilization`` from the configuration (``Decimal``/int/
    float accepted), defaulting to
    :data:`DEFAULT_GPU_MEMORY_UTILIZATION`. Values outside ``(0, 1]`` cannot
    be what the device will apply, so they fall back to the default rather
    than producing a budget the device would never target (mirrors the
    device module). Never raises."""
    try:
        raw = (engine_configuration or {}).get(
            'gpu_memory_utilization', DEFAULT_GPU_MEMORY_UTILIZATION)
        util = float(raw)
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_GPU_MEMORY_UTILIZATION
    if not 0.0 < util <= 1.0:
        return DEFAULT_GPU_MEMORY_UTILIZATION
    return util


def _multimodal_clause(images: int, videos: int, units: int,
                       video_authored: bool) -> str:
    """The multimodal term the activation allowance assumed, named in full:
    the total units and the per-modality counts behind it, plus — when the
    video modality is NOT authored — what that omission costs and how to fix
    it. Every message that quotes the allowance carries this clause."""
    clause = (f"{units} multimodal unit(s) per prompt "
              f"({images} image(s) + {videos} video(s))")
    if not video_authored:
        clause += (
            " — limit_mm_per_prompt.video is NOT authored, so vLLM's own "
            "per-modality default of 1 applies and the video modality is "
            "sized as a full extra unit; authoring \"video\": 0 removes it "
            "(measured on JP6: activation peak 4.93 GiB unbounded vs "
            "2.47 GiB at \"video\": 0)")
    return clause


def _terms_sentence(arch: str, weights_bytes: int, activation_bytes: int,
                    required: int, budget_bytes: int,
                    profile_bytes: int, utilization: float,
                    reservation_bytes: int, cap: Optional[float],
                    images: int, videos: int = DEFAULT_VIDEOS_PER_PROMPT,
                    units: Optional[int] = None,
                    video_authored: bool = False,
                    headroom_bytes: Optional[int] = None) -> str:
    """The auditable statement of every term with its number, shared by the
    passing and failing branches — so an operator can check the verdict
    instead of trusting it. BOTH estimated terms — the activation allowance
    and the non-torch allowance — are labelled ESTIMATEs wherever they
    appear, and NO KV floor is presented as part of the requirement (task 14 /
    H9): what is stated instead is the PREDICTED KV REMAINDER the budget
    leaves over, the quantity vLLM prints as "the rest of the memory reserved
    for KV Cache"."""
    cap_clause = ("no co-tenancy cap is known for this architecture"
                  if cap is None else
                  f"co-tenancy cap on the fraction {cap:.2f}")
    if units is None:
        units = images + videos
    if headroom_bytes is None:
        headroom_bytes = budget_bytes - required
    return (
        f"estimated weights {_format_gib(weights_bytes)} + non-torch "
        f"allowance {_format_gib(NON_TORCH_MEMORY_BYTES)} (an ESTIMATE: "
        f"the median of seven measured non_torch_memory readings on JP6, "
        f"-0.05 to 8.29 GiB) + activation "
        f"allowance {_format_gib(activation_bytes)} (an ESTIMATE: "
        f"max({_format_gib(ACTIVATION_FLOOR_BYTES)}, "
        f"{ACTIVATION_WEIGHT_FRACTION:g} x weights) for "
        f"{_multimodal_clause(images, videos, units, video_authored)}) = "
        f"{_format_gib(required)} required, against a "
        f"{_format_gib(budget_bytes)} budget "
        f"(gpu_memory_utilization={utilization:g} of the {arch} profile's "
        f"{_format_gib(profile_bytes)} TOTAL device memory as the engine "
        f"sees it, of which co-resident consumers hold about "
        f"{_format_gib(reservation_bytes)}; {cap_clause}), leaving a "
        f"predicted KV cache remainder of {_format_gib(headroom_bytes)} "
        f"against the {_format_gib(MINIMUM_KV_CACHE_BYTES)} serving-margin "
        f"floor"
    )


def _remediation_sentences(arch: str, engine_configuration: Dict[str, Any],
                           images: int, utilization: float,
                           cap: Optional[float], profile_bytes: int,
                           reservation_bytes: int, required: int,
                           weights_exceed_budget: bool,
                           videos: int = DEFAULT_VIDEOS_PER_PROMPT,
                           video_authored: bool = False) -> str:
    """Decision 3's ORDERED remediation menu.

    The order is the whole point (defect 1.3): the co-tenancy hazard first,
    then the remediations that reduce OUR OWN demand, and only last —
    quantified and bounded by the Fraction_Cap — raising the fraction. When
    the fraction is already at or above the cap, raising it is declared
    unsafe and nothing is offered. No branch here ever advises LOWERING
    ``gpu_memory_utilization`` as a cure for insufficient KV cache
    (`vllm-sizing-and-packaging-errors` Requirement 3.9's invariant, kept).
    """
    max_model_len = (engine_configuration or {}).get('max_model_len')
    parts = [
        f"Hazard first: this device shares unified memory with the "
        f"co-resident ONNX GPU models and gpu_memory_utilization is a "
        f"fraction of TOTAL memory, so a larger fraction takes memory those "
        f"models are already using.",
        f"Reduce demand first: bound limit_mm_per_prompt.image (effective "
        f"{images}) and limit_mm_per_prompt.video (effective {videos}"
        f"{'' if video_authored else ', NOT authored'}), reduce "
        f"max_model_len (configured "
        f"{max_model_len if max_model_len is not None else 'unset'}), choose "
        f"a smaller or more quantized model, or free device memory by "
        f"stopping unused model components.",
    ]
    if not video_authored:
        # The cheapest demand reduction there is, and the one this product
        # can always take: video is never an input here (measured on JP6
        # 2026-08-19 — bounding it halved the activation peak and turned a
        # failing load into one serving with 6.43 GiB of KV cache).
        parts.append(
            "Cheapest first: set limit_mm_per_prompt.video = 0. vLLM "
            "otherwise reserves half of its worst-case multimodal token "
            "budget (32768 tokens, {'image': 16384, 'video': 16384}) for a "
            "modality this product never sends, which on JP6 measured a "
            "4.93 GiB activation peak instead of 2.47 GiB.")
    if weights_exceed_budget:
        parts.insert(1, (
            "The weights alone exceed the configured budget, so no "
            "activation or KV-cache tuning can make this configuration fit."))

    if cap is None:
        return ' '.join(parts)

    if utilization >= cap:
        parts.append(
            f"Raising the fraction is unsafe here: the configured "
            f"{utilization:g} already meets or exceeds the {cap:.2f} "
            f"co-tenancy cap for {arch} ({_format_gib(profile_bytes)} total "
            f"minus {_format_gib(reservation_bytes)} held by co-resident "
            f"models), so the memory it would claim is memory those models "
            f"are holding.")
        return ' '.join(parts)

    needed_fraction = required / float(profile_bytes)
    last = (
        f"Last resort — raise gpu_memory_utilization only within the "
        f"co-tenancy cap: it may be raised to at most {cap:.2f} on {arch} "
        f"({_format_gib(profile_bytes)} total minus "
        f"{_format_gib(reservation_bytes)} held by co-resident models), and "
        f"the budget you need is {_format_gib(required)}, i.e. at "
        f"least {_round_up_fraction(needed_fraction):.2f}")
    if needed_fraction > cap:
        last += (" — which exceeds that cap, so raising the fraction cannot "
                 "make this configuration fit safely.")
    else:
        last += "."
    parts.append(last)
    return ' '.join(parts)


def evaluate_fit(engine_configuration: Dict[str, Any],
                 estimate: Any,
                 architectures: Iterable[str]) -> List[FitFinding]:
    """Evaluate the Fit_Check for each requested Target_Architecture.

    For every architecture present in DEVICE_MEMORY_PROFILE_BYTES, two named
    conditions over documented terms (design Decision 2)::

        activation := activation_allowance(weights, multimodal_units)
        required   := weights + NON_TORCH_MEMORY_BYTES + activation
        budget     := gpu_memory_utilization * profile[arch]
        cap        := (profile[arch] - co_tenancy[arch]) / profile[arch]

        A (budget sufficiency) : budget >= required
        B (co-tenancy safety)  : gpu_memory_utilization <= cap
        fits := A and B

    NO KV term is charged in ``required``: the KV cache is what the budget
    leaves over. A passing verdict whose predicted KV headroom
    (:func:`kv_headroom_bytes`) falls below ``MINIMUM_KV_CACHE_BYTES`` carries
    the ``thin_margin`` WARNING instead of being refused (task 14 / H9).

    Architectures without a profile entry are skipped (no finding emitted).
    ``budget_bytes`` keeps its original definition and value.

    Args:
        engine_configuration: resolved Engine_Configuration.
            ``gpu_memory_utilization`` (Decimal/int/float, default
            DEFAULT_GPU_MEMORY_UTILIZATION) sizes the budget;
            ``limit_mm_per_prompt`` sizes the multimodal term of the
            activation allowance as the TOTAL of its per-modality counts
            (``image`` default DEFAULT_IMAGES_PER_PROMPT, ``video`` default
            DEFAULT_VIDEOS_PER_PROMPT — vLLM's own default of 1, so an
            unauthored video modality costs a full extra unit);
            ``max_model_len`` is quoted in the remediation.
        estimate: estimated on-GPU weight size in bytes, either a plain
            number or an object exposing ``total_bytes`` (WeightEstimate).
        architectures: Target_Architecture identifiers to evaluate.

    Returns:
        One FitFinding per architecture with a profile entry. Every message
        names the profile entry and every term with its number, labels the
        activation allowance an ESTIMATE, and (when failing) carries
        Decision 3's ordered remediation menu. No message ever advises
        lowering ``gpu_memory_utilization``. Returns [] rather than raising
        if the estimate cannot be read at all.
    """
    utilization = _gpu_memory_utilization(engine_configuration)
    images = images_per_prompt(engine_configuration)
    videos = videos_per_prompt(engine_configuration)
    units = images + videos
    video_authored = video_is_authored(engine_configuration)
    try:
        weights_bytes = int(getattr(estimate, 'total_bytes', estimate))
    except (TypeError, ValueError):
        # Never raise out of the public API: an unreadable estimate is
        # indistinguishable from "unverified", and callers treat an empty
        # findings list that way (Requirement 3.4).
        logger.warning("Fit check skipped: unreadable weight estimate "
                       f"{estimate!r}")
        return []

    activation_bytes = activation_allowance(weights_bytes, units)
    # weights + non-torch (ESTIMATE) + activation (ESTIMATE). No KV term:
    # the 1 GiB serving margin is a warning threshold applied to the predicted
    # remainder, never a charge in `required` (task 14 / H9).
    required = (weights_bytes + NON_TORCH_MEMORY_BYTES + activation_bytes)

    findings = []
    for arch in architectures:
        if arch not in DEVICE_MEMORY_PROFILE_BYTES:
            continue
        profile_bytes = DEVICE_MEMORY_PROFILE_BYTES[arch]
        budget_bytes = int(utilization * profile_bytes)
        reservation_bytes = co_tenancy_reservation_bytes(arch)
        cap = fraction_cap(arch)

        budget_ok = budget_bytes >= required
        co_tenancy_ok = cap is None or utilization <= cap
        fits = budget_ok and co_tenancy_ok
        failed_conditions = []
        if not budget_ok:
            failed_conditions.append('budget')
        if not co_tenancy_ok:
            failed_conditions.append('co_tenancy')

        # The KV cache this model predicts will remain (what vLLM prints as
        # "the rest of the memory reserved for KV Cache").
        headroom_bytes = budget_bytes - required

        # Soft warnings on a PASSING verdict: it fits, with a recorded
        # caution (design Decision 2's `warnings` status keeps a meaning).
        warnings: List[str] = []
        if fits:
            # THIN MARGIN, not a refusal (design Decision 2 / Decision 6,
            # task 14 / H9): the configuration fits, but the KV cache its
            # budget leaves over is under the 1 GiB serving margin. 0.65 GiB
            # demonstrably served at 2.95x concurrency for 4096 tokens, so
            # this is a caution, never a verdict.
            if headroom_bytes < MINIMUM_KV_CACHE_BYTES:
                warnings.append('thin_margin')
            if cap is not None and cap - utilization <= NEAR_CAP_WARNING_MARGIN:
                warnings.append('near_cap')

        terms = _terms_sentence(
            arch, weights_bytes, activation_bytes, required,
            budget_bytes, profile_bytes, utilization, reservation_bytes, cap,
            images, videos, units, video_authored, headroom_bytes)

        if fits:
            message = f"Fit check passed for {arch}: {terms}."
            if 'thin_margin' in warnings:
                message += (
                    f" WARNING (thin margin): only "
                    f"{_format_gib(headroom_bytes)} of KV cache is predicted "
                    f"to remain, under the "
                    f"{_format_gib(MINIMUM_KV_CACHE_BYTES)} KV cache "
                    f"serving-margin floor (a warning, not a refusal — "
                    f"0.65 GiB served this model at 2.95x concurrency for "
                    f"4096 tokens) — this configuration is one co-tenancy "
                    f"swing from failing on device.")
            if 'near_cap' in warnings:
                message += (
                    f" WARNING (near the co-tenancy cap): "
                    f"gpu_memory_utilization={utilization:g} is within "
                    f"{NEAR_CAP_WARNING_MARGIN:g} of the {cap:.2f} cap for "
                    f"{arch}, so the budget nearly reaches memory the "
                    f"co-resident ONNX GPU models hold.")
        else:
            if failed_conditions == ['co_tenancy']:
                headline = (
                    f"Fit check FAILED for {arch} (co-tenancy condition: "
                    f"gpu_memory_utilization={utilization:g} exceeds the "
                    f"{cap:.2f} cap, so the budget claims memory the "
                    f"co-resident ONNX GPU models are using)")
            elif failed_conditions == ['budget']:
                headline = (
                    f"Fit check FAILED for {arch} (budget condition: short "
                    f"by {_format_gib(required - budget_bytes)})")
            else:
                headline = (
                    f"Fit check FAILED for {arch} (budget condition: short "
                    f"by {_format_gib(required - budget_bytes)}; and "
                    f"co-tenancy condition: gpu_memory_utilization="
                    f"{utilization:g} exceeds the {cap:.2f} cap)")
            message = (
                f"{headline}: {terms}. "
                + _remediation_sentences(
                    arch, engine_configuration or {}, images, utilization,
                    cap, profile_bytes, reservation_bytes, required,
                    weights_exceed_budget=weights_bytes > budget_bytes,
                    videos=videos, video_authored=video_authored))

        findings.append(FitFinding(
            arch=arch,
            fits=fits,
            budget_bytes=budget_bytes,
            required_bytes=required,
            message=message,
            weights_bytes=weights_bytes,
            activation_bytes=activation_bytes,
            kv_floor_bytes=MINIMUM_KV_CACHE_BYTES,
            non_torch_bytes=NON_TORCH_MEMORY_BYTES,
            kv_headroom_bytes=headroom_bytes,
            co_tenancy_bytes=reservation_bytes,
            fraction_cap=cap,
            images_per_prompt=images,
            videos_per_prompt=videos,
            multimodal_units=units,
            failed_conditions=failed_conditions,
            warnings=warnings,
        ))

    return findings


def _default_hf_fetch(url: str) -> Any:
    """Fetch and JSON-decode a Hugging Face API URL (short timeout)."""
    request = urllib.request.Request(
        url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(
            request, timeout=HF_FETCH_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode('utf-8'))


def _quantization_bits_per_weight(quantization_config: Dict[str, Any]) -> Optional[float]:
    """Bits per weight from a HF quantization_config, or None if unknown."""
    bits = quantization_config.get('bits')
    if bits is None:
        bits = quantization_config.get('weight_bits')
    if bits is None:
        if quantization_config.get('load_in_4bit'):
            bits = 4
        elif quantization_config.get('load_in_8bit'):
            bits = 8
    if bits is None:
        return None
    bits = float(bits)
    return bits if bits > 0 else None


def _estimate_from_hf(hf_model_id: str,
                      engine_configuration: Dict[str, Any],
                      hf_fetch: Callable[[str], Any]) -> Optional[WeightEstimate]:
    """Weight_Estimate for a Hugging Face-sourced record (Requirement 3.2).

    Primary: sum of `*.safetensors` file sizes from the blobs listing — the
    stored weight bytes, which match on-GPU bytes for non-quantized (and
    pre-quantized) checkpoints. Fallback: parameter count × dtype byte
    width, sized by the quantization_config's bits-per-weight when present.
    """
    url = HF_MODEL_API_URL.format(model_id=quote(hf_model_id, safe='/'))
    metadata = hf_fetch(url)
    if not isinstance(metadata, dict):
        return None

    # Primary: per-file safetensors sizes from the blobs listing.
    safetensors_bytes = 0
    file_count = 0
    for sibling in metadata.get('siblings') or []:
        if not isinstance(sibling, dict):
            continue
        name = sibling.get('rfilename')
        size = sibling.get('size')
        if (isinstance(name, str) and name.endswith('.safetensors')
                and isinstance(size, (int, float)) and size > 0):
            safetensors_bytes += int(size)
            file_count += 1
    if safetensors_bytes > 0:
        return WeightEstimate(
            total_bytes=safetensors_bytes,
            method='safetensors_files',
            detail=(f"sum of {file_count} *.safetensors file size(s) for "
                    f"'{hf_model_id}' ({_format_gib(safetensors_bytes)})"),
        )

    # Fallback: parameter count × bytes per weight.
    safetensors_meta = metadata.get('safetensors') or {}
    param_count = safetensors_meta.get('total')
    if not isinstance(param_count, (int, float)) or param_count <= 0:
        return None
    param_count = int(param_count)

    config = metadata.get('config') or {}
    quantization_config = config.get('quantization_config') \
        if isinstance(config, dict) else None
    if isinstance(quantization_config, dict):
        bits = _quantization_bits_per_weight(quantization_config)
        if bits is not None:
            total_bytes = int(param_count * bits / 8)
            return WeightEstimate(
                total_bytes=total_bytes,
                method='param_count',
                detail=(f"{param_count:,} parameters × {bits:g} bits/weight "
                        f"(quantization_config) for '{hf_model_id}' "
                        f"({_format_gib(total_bytes)})"),
            )

    dtype = str((engine_configuration or {}).get('dtype', 'auto'))
    bytes_per_param = DTYPE_BYTES.get(dtype, DTYPE_BYTES['auto'])
    total_bytes = param_count * bytes_per_param
    return WeightEstimate(
        total_bytes=total_bytes,
        method='param_count',
        detail=(f"{param_count:,} parameters × {bytes_per_param} bytes "
                f"(dtype={dtype}) for '{hf_model_id}' "
                f"({_format_gib(total_bytes)})"),
    )


def _estimate_from_s3(s3_model_artifact: str,
                      s3_head: Callable[..., Dict[str, Any]]) -> Optional[WeightEstimate]:
    """Weight_Estimate for an S3-sourced record (Requirement 3.3).

    Uses the artifact object's ContentLength as-is. The compressed archive
    size slightly underestimates the unpacked weights — acceptable for a
    warning-grade estimate (noted in the detail).
    """
    parsed = urlparse(s3_model_artifact)
    bucket = parsed.netloc
    key = parsed.path.lstrip('/')
    if not bucket or not key:
        return None
    response = s3_head(Bucket=bucket, Key=key)
    content_length = response.get('ContentLength') \
        if isinstance(response, dict) else None
    if not isinstance(content_length, (int, float)) or content_length <= 0:
        return None
    total_bytes = int(content_length)
    return WeightEstimate(
        total_bytes=total_bytes,
        method='s3_artifact',
        detail=(f"S3 artifact size of '{s3_model_artifact}' "
                f"({_format_gib(total_bytes)}; compressed archive size, "
                f"slightly underestimates unpacked weights)"),
    )


def estimate_weights(record: Dict[str, Any],
                     s3_head: Optional[Callable[..., Dict[str, Any]]] = None,
                     hf_fetch: Optional[Callable[[str], Any]] = None
                     ) -> Optional[WeightEstimate]:
    """Estimate the on-GPU weight size for a vLLM_Model_Record.

    Sources (Requirements 3.2, 3.3):
    - Hugging Face (``model_source.huggingface_model_id``): per-file
      safetensors sizes from the blobs listing, falling back to parameter
      count × dtype/quantization byte width. ``hf_fetch(url) -> parsed
      JSON`` is injectable; the default uses urllib with a ~5 s timeout.
    - S3 (``model_source.s3_model_artifact``): the artifact object's
      ContentLength via the injected ``s3_head(Bucket=..., Key=...)``
      callable (pass ``s3_client.head_object``). No boto3 is imported
      here, keeping the module dependency-free.

    Returns None on any fetch/parse failure or when the needed fetcher is
    unavailable — callers skip the Fit_Check and report "unverified",
    never blocking the operation (Requirement 3.4). Never raises.
    """
    try:
        model_source = record.get('model_source') or {}
        if not isinstance(model_source, dict):
            return None

        hf_model_id = model_source.get('huggingface_model_id')
        if hf_model_id:
            return _estimate_from_hf(
                str(hf_model_id),
                record.get('engine_configuration') or {},
                hf_fetch or _default_hf_fetch,
            )

        s3_model_artifact = model_source.get('s3_model_artifact')
        if s3_model_artifact and s3_head is not None:
            return _estimate_from_s3(str(s3_model_artifact), s3_head)

        return None
    except Exception as e:  # noqa: BLE001 — degrade to "unverified" (3.4)
        logger.warning(f"Weight estimation failed, fit check will be "
                       f"skipped: {e}")
        return None
