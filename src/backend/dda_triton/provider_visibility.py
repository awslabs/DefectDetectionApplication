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
"""Backend-side reader/aggregator for the GPU-fallback visibility signal
(spec: model-gpu-fallback-visibility, design File 2).

The Triton python-backend stub (the per-model copy of
``resources_for_copy/inference_runtimes.py``) writes an atomic
Active_Provider_Record sidecar (``dda_active_providers.json``) into the
model VERSION directory on every ``OnnxRunner`` load (design Decision 1).
This module is the backend's side of that filesystem channel:

- :func:`read_active_provider_record` loads the record for one model,
  absence-tolerant per design Decision 6 (missing/corrupt/unreadable
  means "no information" — ``None``, never an exception).
- :func:`execution_provider_info` shapes the record into the additive
  ``defaultConfiguration.executionProviderInfo`` payload merged into
  ``/feature-configurations`` Triton entries (requirement 2.2).
- :func:`device_gpu_status` computes the device-level degraded-GPU signal
  (requirement 2.4, correctness Property 4) and logs the state TRANSITIONS
  (WARNING on entering degraded, INFO on recovery, silence on steady state).
"""
import datetime
import json
import logging
import os

# Module-level so tests (and future callers) can repoint the reader at a
# different repository via patching; same source of truth as the rest of
# the backend (dda_triton.constants).
from dda_triton.constants import TRITON_MODEL_DIR  # noqa: F401

log = logging.getLogger(__name__)

# --- Keep in sync with resources_for_copy/inference_runtimes.py (the record
# WRITER). The per-model runner copy runs inside the Triton python-backend
# stub process and cannot import backend modules, so the constants are
# deliberately duplicated (the established template keep-in-sync convention).
GPU_PROVIDERS = {"CUDAExecutionProvider", "TensorrtExecutionProvider"}

#: Active_Provider_Record sidecar filename in the model VERSION directory.
ACTIVE_PROVIDER_RECORD = "dda_active_providers.json"


def read_active_provider_record(model_name):
    """Read the Active_Provider_Record for ``model_name``; ``None`` if absent.

    ``model_name`` is the bare model name as ``get_features_triton`` sees it
    (the ``model_component`` with the ``base_``/``marshal_`` entries already
    filtered out); a ``base_``-prefixed name is tolerated and normalized.
    Resolves ``{TRITON_MODEL_DIR}/base_{model_name}/``, picks the highest
    INTEGER version directory (non-numeric entries ignored), and loads the
    sidecar JSON.

    Absence tolerance (design Decision 6): a missing base dir, no numeric
    version dir, a missing sidecar, corrupt/empty JSON, or a permission
    error all return ``None`` — "no information", never a raised exception
    and never a false signal.
    """
    try:
        base_name = model_name
        if base_name.startswith("base_"):
            base_name = base_name[len("base_"):]
        base_dir = os.path.join(TRITON_MODEL_DIR, f"base_{base_name}")

        versions = [entry for entry in os.listdir(base_dir)
                    if entry.isdigit()
                    and os.path.isdir(os.path.join(base_dir, entry))]
        if not versions:
            return None
        version_dir = os.path.join(base_dir, max(versions, key=int))

        record_path = os.path.join(version_dir, ACTIVE_PROVIDER_RECORD)
        with open(record_path, encoding="utf-8") as fh:
            record = json.load(fh)
        if not isinstance(record, dict) or not record:
            return None
        return record
    except Exception:
        # Missing dir/file, corrupt JSON, permission error, unexpected
        # model_name type, ... — all "no information" (Decision 6).
        return None


def _flatten_stage_lists(record, key):
    """Ordered de-duplicated union of one per-stage provider list across all
    stages, in stage (insertion) order — the model-level flattening for the
    ``executionProviderInfo`` payload. Multi-stage models request the same
    chain per stage in practice, so this is a plain identity for them; for
    any divergent case the union preserves first-seen order.
    """
    seen = {}
    stages = record.get("stages") or {}
    for stage_record in stages.values():
        if not isinstance(stage_record, dict):
            continue
        for provider in stage_record.get(key) or []:
            # TRT rides as a (name, options) tuple in the REQUESTED chain;
            # the writer normalizes to names, but stay tuple-safe anyway.
            name = provider[0] if isinstance(provider, (tuple, list)) \
                else provider
            seen.setdefault(name, None)
    return list(seen)


def execution_provider_info(record):
    """Shape an Active_Provider_Record into the additive
    ``executionProviderInfo`` payload (requirement 2.2, design Decision 2).

    ``gpuRequested``/``gpuActive`` are the record's model-level aggregates
    (computed by the writer: any stage requested / every GPU-requesting
    stage obtained); ``gpuFallback`` is derived, never stored.
    """
    gpu_requested = bool(record.get("gpuRequested"))
    gpu_active = bool(record.get("gpuActive"))
    return {
        "requestedProviders": _flatten_stage_lists(
            record, "requestedProviders"),
        "activeProviders": _flatten_stage_lists(record, "activeProviders"),
        "gpuRequested": gpu_requested,
        "gpuActive": gpu_active,
        "gpuFallback": gpu_requested and not gpu_active,
        "updatedAt": record.get("updatedAt"),
    }


# Transition state for the degraded-GPU WARNING/INFO logging: None until the
# first aggregation, then the last computed gpuDegraded value. Only
# TRANSITIONS log (entering degraded -> WARNING, recovering -> INFO); steady
# state is silent.
_last_gpu_degraded = None


def device_gpu_status(records, statuses):
    """Compute the device-level degraded-GPU signal (requirement 2.4).

    ``records`` maps model name -> Active_Provider_Record or ``None``
    (absent); ``statuses`` maps model name -> Triton model status string.

    Property 4 aggregation: ``gpuDegraded`` is true iff at least one loaded
    GPU-chain model HAS a record and NO recorded GPU-chain model has an
    active GPU provider. Models without records contribute nothing in
    either direction (Decision 6), and CPU-by-design models
    (``gpuRequested: false``) never count toward the GPU-chain totals —
    they appear in the per-model map (with their record's flags) but can
    neither cause nor mask degradation.
    """
    global _last_gpu_degraded

    models = {}
    gpu_chain = 0
    gpu_active = 0
    for name, record in records.items():
        if not isinstance(record, dict) or not record:
            continue  # absent record: excluded entirely (Decision 6)
        model_gpu_requested = bool(record.get("gpuRequested"))
        model_gpu_active = bool(record.get("gpuActive"))
        if model_gpu_requested:
            gpu_chain += 1
            if model_gpu_active:
                gpu_active += 1
        models[name] = {
            "status": statuses.get(name),
            "runtime": record.get("runtime"),
            "gpuRequested": model_gpu_requested,
            "gpuActive": model_gpu_active,
        }

    degraded = gpu_chain > 0 and gpu_active == 0

    if degraded and _last_gpu_degraded is not True:
        log.warning(
            f"DEVICE GPU DEGRADED: {gpu_chain} GPU-chain ONNX models "
            f"loaded, none has an active GPU provider — all inference is "
            f"running on CPU fallback "
            f"(spec: model-gpu-fallback-visibility)"
        )
    elif not degraded and _last_gpu_degraded is True:
        log.info(
            f"Device GPU status recovered: {gpu_active} of {gpu_chain} "
            f"GPU-chain ONNX models hold an active GPU provider"
        )
    _last_gpu_degraded = degraded

    return {
        "gpuDegraded": degraded,
        "gpuChainModels": gpu_chain,
        "gpuActiveModels": gpu_active,
        "models": models,
        "updatedAt": datetime.datetime.now(datetime.timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
