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
"""``VllmReconciler`` — staged-model reload after a backend restart
(spec: vllm-model-reload-after-backend-restart, design File 1;
Requirements 2.1, 2.2, 3.2).

On the unfixed tree the vLLM model load was a ONE-SHOT action in the
model component's Startup script (``vllm_model_prep.py`` stage →
``POST /load``): the loaded engine lives in the backend process
(``VllmRuntimeManager``, recreated EMPTY by ``app.py::
start_vllm_runtime()`` on every start), so any backend restart silently
orphaned every staged model — component RUNNING, feature-config
"LOADING" forever, generate 409 forever (jetson-thor1, 2026-08-16).
The reconciler closes that gap: one daemon thread per backend start
takes ONE snapshot of the staged, desired models and re-drives each
load SEQUENTIALLY through the loopback model-control endpoint, with
bounded retries and per-model failure isolation.

Package convention: nothing from ``vllm`` is imported here, and merely
importing this module has no side effects (no thread starts, no
requests) — the reconciler only runs when ``start_vllm_runtime()``
constructs it and calls :meth:`VllmReconciler.start`.
"""
import json
import logging
import threading
import time

import requests

from vllm_runtime.constants import VLLM_RUNTIME_HOST, VLLM_RUNTIME_PORT
from vllm_runtime.manager import ModelState

logger = logging.getLogger(__name__)

#: Bounded post-restart retry schedule per model (Decision 1): the
#: incident showed engine-killing churn 45-100 s after a backend spawn,
#: so the schedule gives a fast second attempt, then waits out
#: deployment/abort churn windows. 4 attempts total (initial + one per
#: backoff entry), ~10.5 min worst case, then the model is LEFT in
#: FAILED with its retained reason — bounded retries, never a retry
#: storm (Requirement 3.2).
RECONCILE_RETRY_BACKOFF_SECONDS = (30, 120, 480)

#: An engine load can pull/initialize a large model; mirrors
#: ``vllm_model_prep.LOAD_REQUEST_TIMEOUT_SECONDS`` (the component
#: Startup's timeout class for the SAME endpoint).
LOAD_REQUEST_TIMEOUT_SECONDS = 1500

#: Timeout of the KV-OOM recovery unload request; mirrors
#: ``vllm_model_prep.UNLOAD_REQUEST_TIMEOUT_SECONDS``.
UNLOAD_REQUEST_TIMEOUT_SECONDS = 300

#: Markers in an extracted load-failure reason indicating vLLM could not
#: reserve KV-cache blocks. MUST stay in sync with
#: ``dda_triton.vllm_model_prep.KV_CACHE_HINT_MARKERS`` (the reconciler
#: mirrors ``request_load``'s marker-driven unload→reload recovery,
#: validated on-device; that module is deliberately not imported — it
#: configures logging at import time).
KV_CACHE_HINT_MARKERS = (
    "No available memory for the cache blocks",
    "gpu_memory_utilization",
)


def _extract_load_failure_reason(body_text):
    """The human-readable reason inside a Triton model-control error
    body. Mirrors ``vllm_model_prep.extract_load_failure_reason``:
    Triton returns ``{"error": "..."}``; the ``error`` text is returned
    when the body is a JSON object carrying a non-empty ``error`` field,
    otherwise the raw body text (stripped) is the reason."""
    try:
        parsed = json.loads(body_text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("error"):
        return str(parsed["error"])
    return body_text.strip()


class VllmReconciler:
    """Re-drives the load of every staged, desired vLLM model after a
    backend restart (spec: vllm-model-reload-after-backend-restart,
    Requirement 2.1).

    INVARIANT: every load is issued through the loopback model-control
    endpoint — never ``manager.load()`` directly — so engine
    construction happens on the runtime server's event loop and all
    load requests serialize there (Decision 1). A reconciler-side
    ``asyncio.run(manager.load(...))`` would create the engine on a
    throwaway loop and break every subsequent generate; the HTTP path
    is byte-identical to the component Startup's load path, so
    reconciliation cannot drift from first-deploy semantics.

    One model per step, sequentially, in sorted name order: engine
    loads are GPU-memory-hungry and today's reality is serial. One
    model's failure never stops the scan (Requirement 3.2), and the
    entire thread body is exception-contained — a reconciler crash is
    logged and never touches the backend or the runtime server.
    """

    def __init__(
        self,
        manager,
        port=VLLM_RUNTIME_PORT,
        backoff=RECONCILE_RETRY_BACKOFF_SECONDS,
        request_fn=None,  # injectable for tests
    ):
        self._manager = manager
        self._port = int(port)
        self._backoff = tuple(backoff)
        self._request_fn = request_fn if request_fn is not None else requests.post

    # --- lifecycle ---------------------------------------------------------

    def start(self):
        """Start the one reconciliation pass on a daemon thread named
        ``vllm-reconciler``. Returns the thread. The entire body is
        try/except-contained (the ``start_vllm_runtime`` containment
        convention): a reconciler failure is logged and never touches
        the backend or the runtime server."""
        thread = threading.Thread(
            name="vllm-reconciler", target=self._run, daemon=True
        )
        thread.start()
        return thread

    def _run(self):
        try:
            candidates = self._candidates()
            if not candidates:
                logger.info(
                    "vLLM reconciler: no staged models awaiting reload; "
                    "nothing to do"
                )
                return
            logger.info(
                "vLLM reconciler: re-driving the load of %d staged "
                "model(s): %s", len(candidates), ", ".join(candidates)
            )
            for model_name in candidates:
                try:
                    self._reconcile_one(model_name)
                except Exception:  # noqa: BLE001 - per-model isolation (3.2)
                    logger.exception(
                        "vLLM reconciler: unexpected error reconciling "
                        "model '%s'; continuing with the remaining models",
                        model_name,
                    )
            logger.info("vLLM reconciler: reconciliation pass finished")
        except Exception:  # noqa: BLE001 - containment: never touch the backend
            logger.exception(
                "vLLM reconciler: reconciliation pass crashed; the backend "
                "and the runtime server are unaffected"
            )

    # --- reconciliation core -------------------------------------------------

    def _candidates(self):
        """ONE snapshot of the reload candidates at startup:
        ``manager.list_models()`` entries in STAGED state (UNLOADED —
        tombstoned — entries are already excluded by the manager,
        Decision 3), sorted by name for deterministic sequential order.
        New stagings after backend start are the component Startup's
        job — the reconciler's only mission is the restart-orphaned
        staged set, fully known at start."""
        statuses = self._manager.list_models()
        return sorted(
            name
            for name, status in statuses.items()
            if status.state is ModelState.STAGED
        )

    def _reconcile_one(self, model_name):
        """Drive one model's reload: bounded retries over the backoff
        schedule, the validated KV-OOM single unload→reload recovery per
        attempt, exhaustion → one prominent ERROR with the retained
        reason (state stays FAILED — truthful, Requirement 2.3)."""
        attempts = len(self._backoff) + 1
        for attempt in range(1, attempts + 1):
            # Re-check right before the POST: the component Startup may
            # have gotten there first (the fresh-deploy case) or an
            # earlier caller already drove the model home.
            state = self._manager.state(model_name).state
            if state in (ModelState.LOADING, ModelState.READY):
                logger.info(
                    "vLLM reconciler: model '%s' is already %s; skipping",
                    model_name, state.value,
                )
                return
            logger.info(
                "vLLM reconciler: requesting load of '%s' (attempt %d/%d)",
                model_name, attempt, attempts,
            )
            loaded, reason = self._request_load(model_name)
            if loaded:
                logger.info(
                    "vLLM reconciler: model '%s' loaded successfully",
                    model_name,
                )
                return
            if reason is not None and any(
                marker in reason for marker in KV_CACHE_HINT_MARKERS
            ):
                # Mirrors vllm_model_prep.request_load's validated
                # recovery: the first load after a runtime restart can
                # fail with a KV-cache OOM because the failed attempt
                # itself leaves its GPU allocations pinned; an unload
                # releases them and the immediately following load
                # succeeds. Exactly ONE recovery cycle per attempt.
                logger.warning(
                    "vLLM reconciler: load of '%s' hit a KV-cache "
                    "out-of-memory failure; attempting the validated "
                    "unload -> reload recovery (a failed load can leave "
                    "its GPU allocations pinned in the runtime)",
                    model_name,
                )
                self._request_unload(model_name)
                loaded, _ = self._request_load(model_name)
                if loaded:
                    logger.info(
                        "vLLM reconciler: model '%s' loaded successfully "
                        "after KV-cache OOM recovery", model_name,
                    )
                    return
            if attempt <= len(self._backoff):
                delay = self._backoff[attempt - 1]
                logger.info(
                    "vLLM reconciler: load of '%s' failed; retrying in %s "
                    "seconds (%d attempt(s) left)",
                    model_name, delay, attempts - attempt,
                )
                time.sleep(delay)
        retained = self._manager.state(model_name).reason
        logger.error(
            "vLLM reconciler: model '%s' FAILED to reload after %d "
            "attempts — automatic retries are exhausted; retained "
            "reason: %s. The model stays FAILED until an explicit load "
            "or a component restart re-drives it.",
            model_name, attempts, retained,
        )

    # --- model-control requests ----------------------------------------------

    def _request_load(self, model_name):
        """One load request through the loopback model-control endpoint
        (the Decision 1 invariant). Returns ``(loaded, reason)`` where
        ``reason`` is the extracted reason of an authoritative HTTP
        failure (KV-OOM marker matching input), else ``None``."""
        url = "http://{}:{}/v2/repository/models/{}/load".format(
            VLLM_RUNTIME_HOST, self._port, model_name
        )
        try:
            response = self._request_fn(
                url, timeout=LOAD_REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as err:
            logger.error(
                "vLLM reconciler: load request for '%s' failed at the "
                "connection level: %s", model_name, err,
            )
            return False, None
        if response.status_code == 200:
            return True, None
        reason = _extract_load_failure_reason(response.text)
        logger.error(
            "vLLM reconciler: model '%s' load failed (HTTP %s): %s",
            model_name, response.status_code, reason,
        )
        return False, reason

    def _request_unload(self, model_name):
        """Best-effort recovery unload through the model-control
        endpoint (mirrors ``vllm_model_prep.request_unload``). Returns
        True on HTTP 200."""
        url = "http://{}:{}/v2/repository/models/{}/unload".format(
            VLLM_RUNTIME_HOST, self._port, model_name
        )
        try:
            response = self._request_fn(
                url, timeout=UNLOAD_REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as err:
            logger.error(
                "vLLM reconciler: recovery unload request for '%s' "
                "failed: %s", model_name, err,
            )
            return False
        if response.status_code == 200:
            return True
        logger.error(
            "vLLM reconciler: recovery unload of '%s' failed (HTTP %s): %s",
            model_name, response.status_code, response.text,
        )
        return False
