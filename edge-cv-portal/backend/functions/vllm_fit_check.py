"""
vLLM preflight fit check — pure sizing math, no AWS dependencies.

Computes whether a vLLM model's estimated weight footprint plus a minimum
KV-cache reservation fits inside the GPU memory budget granted by
``gpu_memory_utilization`` on each target device architecture
(Device_Memory_Profile). Imported by model_import.py (registration/update
warnings), models.py, and greengrass_publish.py (publish gate), so this
module must stay stdlib-only and must never raise out of its public API.
"""
import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

GIB = 1024 ** 3

# Per-Target_Architecture usable device GPU memory (unified memory on
# Jetson), in bytes. Conservative "usable" figures, not nameplate RAM.
DEVICE_MEMORY_PROFILE_BYTES = {
    'arm64_jp6': 30 * GIB,   # 32 GB Orin class, ~30 GiB usable
    'arm64_jp5': 30 * GIB,   # only reachable when JP5_VLLM_ENABLED
    'arm64_jp7': 120 * GIB,  # 128 GB Thor class, ~120 GiB usable
}

# Floor for vLLM KV-cache blocks beyond weights + activation overhead.
# Below this, vLLM cannot allocate cache blocks and the load fails.
MINIMUM_KV_CACHE_BYTES = 1 * GIB

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
    """Result of the fit check for one target architecture."""
    arch: str
    fits: bool
    budget_bytes: int            # gpu_memory_utilization * profile[arch]
    required_bytes: int          # estimate + MINIMUM_KV_CACHE_BYTES
    message: str                 # names the profile entry, numbers, remediation


def _format_gib(num_bytes: int) -> str:
    """Render a byte count as GiB with two decimals (e.g. '14.25 GiB')."""
    return f"{num_bytes / GIB:.2f} GiB"


def evaluate_fit(engine_configuration: Dict[str, Any],
                 estimate: Any,
                 architectures: Iterable[str]) -> List[FitFinding]:
    """Evaluate the Fit_Check for each requested Target_Architecture.

    For every architecture present in DEVICE_MEMORY_PROFILE_BYTES:
    ``fits = gpu_memory_utilization * profile[arch] >= estimate +
    MINIMUM_KV_CACHE_BYTES``. Architectures without a profile entry are
    skipped (no finding emitted).

    Args:
        engine_configuration: resolved Engine_Configuration; only
            ``gpu_memory_utilization`` is read (Decimal/int/float accepted;
            defaults to DEFAULT_GPU_MEMORY_UTILIZATION when absent).
        estimate: estimated on-GPU weight size in bytes, either a plain
            number or an object exposing ``total_bytes`` (WeightEstimate).
        architectures: Target_Architecture identifiers to evaluate.

    Returns:
        One FitFinding per architecture with a profile entry. Failing
        findings name the profile entry, the budget, the estimate, and the
        remediation (raise gpu_memory_utilization, reduce max_model_len,
        or choose a smaller model — never advice to lower the fraction).
    """
    raw_util = engine_configuration.get('gpu_memory_utilization',
                                        DEFAULT_GPU_MEMORY_UTILIZATION)
    gpu_memory_utilization = float(raw_util)
    estimate_bytes = int(getattr(estimate, 'total_bytes', estimate))
    required_bytes = estimate_bytes + MINIMUM_KV_CACHE_BYTES

    findings = []
    for arch in architectures:
        if arch not in DEVICE_MEMORY_PROFILE_BYTES:
            continue
        profile_bytes = DEVICE_MEMORY_PROFILE_BYTES[arch]
        budget_bytes = int(gpu_memory_utilization * profile_bytes)
        fits = budget_bytes >= required_bytes

        if fits:
            message = (
                f"Fit check passed for {arch}: estimated weights "
                f"{_format_gib(estimate_bytes)} + minimum KV cache "
                f"{_format_gib(MINIMUM_KV_CACHE_BYTES)} fit within the "
                f"{_format_gib(budget_bytes)} budget "
                f"(gpu_memory_utilization={gpu_memory_utilization:g} of the "
                f"{arch} profile's {_format_gib(profile_bytes)} usable memory)."
            )
        else:
            message = (
                f"Fit check FAILED for {arch}: estimated weights "
                f"{_format_gib(estimate_bytes)} + minimum KV cache "
                f"{_format_gib(MINIMUM_KV_CACHE_BYTES)} = "
                f"{_format_gib(required_bytes)} exceed the "
                f"{_format_gib(budget_bytes)} budget "
                f"(gpu_memory_utilization={gpu_memory_utilization:g} of the "
                f"{arch} profile's {_format_gib(profile_bytes)} usable memory). "
                f"Remediation: raise gpu_memory_utilization (the weights alone "
                f"exceed the configured budget), reduce max_model_len, or "
                f"choose a smaller model."
            )

        findings.append(FitFinding(
            arch=arch,
            fits=fits,
            budget_bytes=budget_bytes,
            required_bytes=required_bytes,
            message=message,
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
