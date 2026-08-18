"""
vLLM preflight fit check — pure sizing math, no AWS dependencies.

Decides whether a vLLM model can be loaded inside the GPU memory budget
granted by ``gpu_memory_utilization`` on each target device architecture
(Device_Memory_Profile), over the terms vLLM actually charges against that
budget: the weights, the activation/profiling peak, and a KV-cache floor —
plus a cap on the fraction itself, because on a Jetson's unified memory the
budget is a fraction of TOTAL device memory that other resident consumers
(the co-resident ONNX GPU models) are already holding.

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

# Serving-margin floor for KV-cache blocks beyond weights + activation —
# NOT a hard load threshold. The incident device demonstrably SERVED this
# model with 0.65 GiB of KV at 2.95x maximum concurrency for 4096 tokens, so
# a sub-floor remainder is a thin margin (a warning), not proof of failure.
# The floor is kept at 1 GiB as the margin we size for. Value unchanged.
MINIMUM_KV_CACHE_BYTES = 1 * GIB

# Conservative floor for the activation/profiling peak, so small models —
# where a fraction-of-weights term would round to nothing — still carry an
# allowance. ESTIMATE.
ACTIVATION_FLOOR_BYTES = 2 * GIB

# Activation/profiling peak as a fraction of weights. Calibrated to the one
# measured point available: `PyTorch activation peak memory takes 4.92GiB`
# against `model weights take 6.47GiB` = 0.76, at `enforce_eager=true`,
# `max_model_len=4096`, one image per prompt. ESTIMATE — every message that
# quotes the allowance must label it one. It is used only conservatively (to
# refuse), never permissively.
ACTIVATION_WEIGHT_FRACTION = 0.75

# Extra activation cost per ADDITIONAL image per prompt, as a fraction of the
# one-image allowance. UNMEASURED and deliberately high, so a two-image
# configuration must be sized explicitly rather than sneaking into an
# already-published model's budget (defect 1.4). [HARDWARE H8 to calibrate]
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
    required_bytes: int          # weights + activation allowance + KV floor
    message: str                 # names every term, its number, remediation
    # --- additive terms: the audit trail behind `fits` ---
    weights_bytes: int = 0
    activation_bytes: int = 0        # ESTIMATE (see activation_allowance)
    kv_floor_bytes: int = 0
    co_tenancy_bytes: int = 0
    fraction_cap: Optional[float] = None
    images_per_prompt: int = DEFAULT_IMAGES_PER_PROMPT
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
                         images_per_prompt: int = DEFAULT_IMAGES_PER_PROMPT
                         ) -> int:
    """Estimated PyTorch activation/profiling peak vLLM charges against the
    budget, in bytes:

        max(ACTIVATION_FLOOR_BYTES, ACTIVATION_WEIGHT_FRACTION × weights)
        × (1 + MULTIMODAL_IMAGE_INCREMENT × (images − 1))

    A fraction of weights rather than something cleverer on purpose: a
    per-model-class table would need measurements we do not have, a fixed
    absolute allowance would be wrong by an order of magnitude across the
    0.5B–70B range, and a first-principles computation (hidden size × batch
    tokens × layers) needs config fields this module never fetches and would
    produce false precision. One calibrated coefficient tracks model scale,
    is trivially auditable in the message, and errs high.

    This is an ESTIMATE and every message that quotes it says so. Tolerant of
    hostile input (never raises): unusable values degrade to 0 weights and to
    one image.
    """
    try:
        weights = max(int(weights_bytes or 0), 0)
    except (TypeError, ValueError):
        weights = 0
    try:
        images = max(int(images_per_prompt), 1)
    except (TypeError, ValueError):
        images = DEFAULT_IMAGES_PER_PROMPT
    base = max(ACTIVATION_FLOOR_BYTES, ACTIVATION_WEIGHT_FRACTION * weights)
    multiplier = 1.0 + MULTIMODAL_IMAGE_INCREMENT * (images - 1)
    return int(base * multiplier)


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


def _terms_sentence(arch: str, weights_bytes: int, activation_bytes: int,
                    required_bytes: int, budget_bytes: int,
                    profile_bytes: int, utilization: float,
                    reservation_bytes: int, cap: Optional[float],
                    images: int) -> str:
    """The auditable statement of every term with its number, shared by the
    passing and failing branches — so an operator can check the verdict
    instead of trusting it. The activation allowance is labelled an
    ESTIMATE wherever it appears."""
    cap_clause = ("no co-tenancy cap is known for this architecture"
                  if cap is None else
                  f"co-tenancy cap on the fraction {cap:.2f}")
    return (
        f"estimated weights {_format_gib(weights_bytes)} + activation "
        f"allowance {_format_gib(activation_bytes)} (an ESTIMATE: "
        f"max({_format_gib(ACTIVATION_FLOOR_BYTES)}, "
        f"{ACTIVATION_WEIGHT_FRACTION:g} x weights) for {images} image(s) "
        f"per prompt) + KV cache floor "
        f"{_format_gib(MINIMUM_KV_CACHE_BYTES)} = "
        f"{_format_gib(required_bytes)} required, against a "
        f"{_format_gib(budget_bytes)} budget "
        f"(gpu_memory_utilization={utilization:g} of the {arch} profile's "
        f"{_format_gib(profile_bytes)} TOTAL device memory as the engine "
        f"sees it, of which co-resident consumers hold about "
        f"{_format_gib(reservation_bytes)}; {cap_clause})"
    )


def _remediation_sentences(arch: str, engine_configuration: Dict[str, Any],
                           images: int, utilization: float,
                           cap: Optional[float], profile_bytes: int,
                           reservation_bytes: int, required_bytes: int,
                           weights_exceed_budget: bool) -> str:
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
        f"{images}), reduce max_model_len (configured "
        f"{max_model_len if max_model_len is not None else 'unset'}), choose "
        f"a smaller or more quantized model, or free device memory by "
        f"stopping unused model components.",
    ]
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

    needed_fraction = required_bytes / float(profile_bytes)
    last = (
        f"Last resort — raise gpu_memory_utilization only within the "
        f"co-tenancy cap: it may be raised to at most {cap:.2f} on {arch} "
        f"({_format_gib(profile_bytes)} total minus "
        f"{_format_gib(reservation_bytes)} held by co-resident models), and "
        f"the budget you need is {_format_gib(required_bytes)}, i.e. at "
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

        activation := activation_allowance(weights, images_per_prompt)
        required   := weights + activation + MINIMUM_KV_CACHE_BYTES
        budget     := gpu_memory_utilization * profile[arch]
        cap        := (profile[arch] - co_tenancy[arch]) / profile[arch]

        A (budget sufficiency) : budget >= required
        B (co-tenancy safety)  : gpu_memory_utilization <= cap
        fits := A and B

    Architectures without a profile entry are skipped (no finding emitted).
    ``budget_bytes`` keeps its original definition and value.

    Args:
        engine_configuration: resolved Engine_Configuration.
            ``gpu_memory_utilization`` (Decimal/int/float, default
            DEFAULT_GPU_MEMORY_UTILIZATION) sizes the budget;
            ``limit_mm_per_prompt.image`` (default
            DEFAULT_IMAGES_PER_PROMPT) sizes the multimodal term of the
            activation allowance; ``max_model_len`` is quoted in the
            remediation.
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
    try:
        weights_bytes = int(getattr(estimate, 'total_bytes', estimate))
    except (TypeError, ValueError):
        # Never raise out of the public API: an unreadable estimate is
        # indistinguishable from "unverified", and callers treat an empty
        # findings list that way (Requirement 3.4).
        logger.warning("Fit check skipped: unreadable weight estimate "
                       f"{estimate!r}")
        return []

    activation_bytes = activation_allowance(weights_bytes, images)
    required_bytes = (weights_bytes + activation_bytes
                      + MINIMUM_KV_CACHE_BYTES)

    findings = []
    for arch in architectures:
        if arch not in DEVICE_MEMORY_PROFILE_BYTES:
            continue
        profile_bytes = DEVICE_MEMORY_PROFILE_BYTES[arch]
        budget_bytes = int(utilization * profile_bytes)
        reservation_bytes = co_tenancy_reservation_bytes(arch)
        cap = fraction_cap(arch)

        budget_ok = budget_bytes >= required_bytes
        co_tenancy_ok = cap is None or utilization <= cap
        fits = budget_ok and co_tenancy_ok
        failed_conditions = []
        if not budget_ok:
            failed_conditions.append('budget')
        if not co_tenancy_ok:
            failed_conditions.append('co_tenancy')

        # Soft warnings on a PASSING verdict: it fits, with a recorded
        # caution (design Decision 2's `warnings` status keeps a meaning).
        warnings: List[str] = []
        if fits:
            if budget_bytes - required_bytes < MINIMUM_KV_CACHE_BYTES:
                warnings.append('thin_margin')
            if cap is not None and cap - utilization <= NEAR_CAP_WARNING_MARGIN:
                warnings.append('near_cap')

        terms = _terms_sentence(
            arch, weights_bytes, activation_bytes, required_bytes,
            budget_bytes, profile_bytes, utilization, reservation_bytes, cap,
            images)

        if fits:
            message = f"Fit check passed for {arch}: {terms}."
            if 'thin_margin' in warnings:
                message += (
                    f" WARNING (thin margin): only "
                    f"{_format_gib(budget_bytes - required_bytes)} remains "
                    f"beyond the requirement, under the "
                    f"{_format_gib(MINIMUM_KV_CACHE_BYTES)} KV cache floor "
                    f"— this configuration is one co-tenancy swing from "
                    f"failing on device.")
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
                    f"by {_format_gib(required_bytes - budget_bytes)})")
            else:
                headline = (
                    f"Fit check FAILED for {arch} (budget condition: short "
                    f"by {_format_gib(required_bytes - budget_bytes)}; and "
                    f"co-tenancy condition: gpu_memory_utilization="
                    f"{utilization:g} exceeds the {cap:.2f} cap)")
            message = (
                f"{headline}: {terms}. "
                + _remediation_sentences(
                    arch, engine_configuration or {}, images, utilization,
                    cap, profile_bytes, reservation_bytes, required_bytes,
                    weights_exceed_budget=weights_bytes > budget_bytes))

        findings.append(FitFinding(
            arch=arch,
            fits=fits,
            budget_bytes=budget_bytes,
            required_bytes=required_bytes,
            message=message,
            weights_bytes=weights_bytes,
            activation_bytes=activation_bytes,
            kv_floor_bytes=MINIMUM_KV_CACHE_BYTES,
            co_tenancy_bytes=reservation_bytes,
            fraction_cap=cap,
            images_per_prompt=images,
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
