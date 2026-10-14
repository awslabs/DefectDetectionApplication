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
"""Triton model readiness gate for the workflow run paths.

Bugfix: `.kiro/specs/cold-model-first-run-failure/` (design.md Decisions
2-5), task 2.1.

Why this exists
---------------
Nothing on either workflow run path ever read Triton model state, and the
only readiness check that exists is native, synchronous and does not wait:
``emltriton``'s ``Initialize()`` calls ``LoadModel`` — which merely ENQUEUES
(``triton_server.cpp``) — and then ``CheckModelLoaded()`` on the very next
line (``emltriton.cpp``). A model whose asynchronous load is still in flight
therefore fails instantly, the GStreamer state change returns FAILURE, and the
operator is told only "Pipeline failed to change state to PLAYING", which
names neither the model nor its state.

Because Triton load state lives ONLY in the backend process, every backend
restart re-opens that window for every model. Measured on jetson-thor1 on
2026-09-22: of twelve executions of one workflow, the single failure was the
first one after a backend container restart.

This module is the gate: it runs BEFORE the pipeline is built, so a cold model
produces an honest error instead of a generic one — and, on the classic path,
the run never reaches the catch-all that moves the source image to ``failed/``
(design.md Decision 7: that harm is fixed by ordering, not by a special case).

The vocabulary matters
----------------------
``GetModelStatus`` returns one of five tokens, and treating them uniformly is
what makes a naive "poll until READY" loop hang on the exact case that
motivated this bugfix:

===============  =========================================================
``READY``        loaded; proceed immediately (the warm path pays nothing)
``LOADING``      a load is in flight; wait, and do NOT request another
``UNKNOWN``      this process has never been asked to load it — the state a
                 restarted backend sees. A pure wait never converges, so
                 exactly one load is kicked, then we wait
``UNAVAILABLE``  terminal failure carrying Triton's own ``reason``; fail fast
``UNLOADING``    on its way out; fail fast rather than wait for a model that
                 is being removed
===============  =========================================================

Deliberately NOT here: a boot-time reconciler that re-drives staged loads so
the window is short (design.md Decision 1 defers it, mirroring
`.kiro/specs/vllm-model-reload-after-backend-restart/`). The gate makes the
window harmless; a reconciler would only make it shorter, and the INFO wait
logging below is the evidence that decides whether it is ever worth building.
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


#: Triton's state vocabulary (``triton_server.cpp::GetModelStatus``).
STATE_READY = "READY"
STATE_LOADING = "LOADING"
STATE_UNKNOWN = "UNKNOWN"
STATE_UNAVAILABLE = "UNAVAILABLE"
STATE_UNLOADING = "UNLOADING"

#: Seconds between readiness polls. Matches
#: ``model_convertor.START_MODEL_POLL_INTERVAL_S`` so the two waiters behave
#: identically against the same server.
POLL_INTERVAL_S = 3.0

#: Total seconds to wait for a model to reach READY.
#:
#: Sized for the case that produced the original 2026-08-14 report, NOT for
#: the DLR case: a first ONNX load on Thor can build a TensorRT engine for a
#: ~300 MB model and take minutes, and ``model_convertor``'s own 120 s budget
#: is documented as sized for DLR ("tens of seconds"). A budget that is too
#: small times out on a load that is progressing perfectly well, which is the
#: failure mode this gate exists to remove.
READY_TIMEOUT_S = 600.0


@dataclass(frozen=True)
class ReadinessOutcome:
    """The gate's answer. ``ready`` is the only success."""

    ready: bool
    state: str
    #: Operator-facing reason, populated only when ``ready`` is false. Names
    #: the model, the resolved Triton model, the observed state and the
    #: elapsed wait (Requirement 2.11(b), design.md Decision 5).
    message: Optional[str] = None
    waited_seconds: float = 0.0


def _client():
    """The process-wide Triton client.

    Imported lazily and deliberately: ``dda_triton.triton_edge_client``
    imports ``panorama`` at module level, which exists on-device only, so a
    module-level import here would make this gate — and everything that
    imports it — unimportable off-device and untestable on the host.
    """
    from dda_triton.triton_edge_client import TritonEdgeClient

    return TritonEdgeClient.get_instance()


def _repo_has_models() -> bool:
    """Whether the Triton model repository holds anything.

    Reuses the existing guard rather than adding a second one:
    ``TritonEdgeClient.get_instance()`` CREATES the native server when absent,
    and standing Triton up against an empty repository has a documented hang
    (Requirement 2.15, consistent with 3.12).
    """
    from utils.feature_configs_utils import triton_repo_has_models

    return bool(triton_repo_has_models())


def ensure_model_ready(
    model_name: str,
    resolved_name: Optional[str] = None,
    client=None,
    sleep=time.sleep,
    clock=time.monotonic,
    timeout_s: Optional[float] = None,
    poll_interval_s: Optional[float] = None,
) -> ReadinessOutcome:
    """Wait, bounded, for ``resolved_name`` to reach READY in Triton.

    ``model_name`` is the workflow's own (registry) model name and appears in
    the operator-facing message; ``resolved_name`` is the deployed Triton
    model name the pipeline will actually ask for, defaulting to
    ``model_name``.

    Returns a :class:`ReadinessOutcome`; never raises for a model-state
    reason. An unexpected failure while *reading* state is reported as
    ready=True (fail-open) so this gate can never be the sole reason a
    previously working run stops working — the pipeline then fails exactly as
    it does today, which is the pre-gate behaviour.

    ``client``, ``sleep`` and ``clock`` are injectable for tests.

    ``timeout_s`` / ``poll_interval_s`` default to :data:`READY_TIMEOUT_S` /
    :data:`POLL_INTERVAL_S`, resolved HERE rather than as default argument
    values — a default argument would bind the constant at import time, so
    neither an operator override nor a test could change it.
    """
    target = resolved_name or model_name
    if timeout_s is None:
        timeout_s = READY_TIMEOUT_S
    if poll_interval_s is None:
        poll_interval_s = POLL_INTERVAL_S

    if not _repo_has_models():
        # Nothing is deployed; creating the server here could hang, and the
        # pipeline's own failure is the honest outcome.
        return ReadinessOutcome(ready=True, state=STATE_UNKNOWN)

    try:
        triton = client if client is not None else _client()
    except Exception:  # noqa: BLE001 - never block a run on gate plumbing
        logger.exception(
            "Triton readiness gate could not reach the Triton client; "
            "proceeding without the readiness check for model %s", target
        )
        return ReadinessOutcome(ready=True, state=STATE_UNKNOWN)

    started = clock()
    kicked = False

    while True:
        try:
            state = triton.get_model_status(target)
        except Exception:  # noqa: BLE001 - fail open, see docstring
            logger.exception(
                "Could not read Triton state for model %s; proceeding "
                "without the readiness check", target
            )
            return ReadinessOutcome(ready=True, state=STATE_UNKNOWN)

        state = str(state or STATE_UNKNOWN).strip().upper()
        waited = max(0.0, clock() - started)

        if state == STATE_READY:
            if waited >= poll_interval_s:
                # The data that decides whether design.md Decision 1's
                # deferred boot-time reconciler is ever worth building.
                logger.info(
                    "Triton model %s reached READY after %.1fs of waiting "
                    "(workflow model %s)", target, waited, model_name
                )
            return ReadinessOutcome(
                ready=True, state=state, waited_seconds=waited
            )

        if state in (STATE_UNAVAILABLE, STATE_UNLOADING):
            return ReadinessOutcome(
                ready=False,
                state=state,
                message=_message(model_name, target, state, waited,
                                 _reason_for(triton, target)),
                waited_seconds=waited,
            )

        if state == STATE_UNKNOWN and not kicked:
            # This process has never been asked to load the model — the
            # state a restarted backend sees. Waiting alone never converges,
            # so kick exactly one load, through the same route the model
            # component's Startup uses.
            kicked = True
            logger.info(
                "Triton model %s is UNKNOWN; requesting a load before "
                "waiting (workflow model %s)", target, model_name
            )
            try:
                triton.start_triton_model(target)
            except Exception:  # noqa: BLE001 - a refused kick is not fatal
                # The route refuses (403) unless the state is
                # UNKNOWN/UNAVAILABLE, so a race with another loader lands
                # here; keep waiting, the other loader is making progress.
                logger.warning(
                    "Load request for Triton model %s was refused; "
                    "continuing to wait", target, exc_info=True
                )

        if waited >= timeout_s:
            return ReadinessOutcome(
                ready=False,
                state=state,
                message=_message(model_name, target, state, waited,
                                 _reason_for(triton, target)),
                waited_seconds=waited,
            )

        sleep(poll_interval_s)


def _reason_for(triton, target) -> Optional[str]:
    """Triton's own failure reason for ``target``, when it has one.

    ``list_triton_models`` carries ``reason`` for an UNAVAILABLE model (the
    field commit 7812407 added so a failed load stops looking like an
    in-flight one). Best-effort: the message is better with it, correct
    without it.
    """
    try:
        for record in triton.list_triton_models() or []:
            if record.get("model_component") == target:
                return record.get("reason")
    except Exception:  # noqa: BLE001 - message enrichment only
        logger.debug("Could not read a Triton failure reason for %s", target,
                     exc_info=True)
    return None


def _message(model_name, target, state, waited, reason) -> str:
    """The operator-facing failure. Names the model and its state, which the
    generic "Pipeline failed to change state to PLAYING" did not
    (Requirement 2.11(b), defects 1.3 and 1.18)."""
    detail = (
        "Model '{0}' (Triton model '{1}') is not ready for inference: "
        "state {2}".format(model_name, target, state)
    )
    if reason:
        detail += " (reason: {0})".format(reason)
    if state == STATE_UNAVAILABLE:
        detail += (
            ". The model failed to load; redeploy or restart its model "
            "component."
        )
    elif state == STATE_UNLOADING:
        detail += ". The model is being unloaded."
    else:
        detail += (
            ". Waited {0:.0f}s for it to become READY. A first load can "
            "take minutes while the runtime builds its engine; retry once "
            "the model reports READY.".format(waited)
        )
    return detail
