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
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from dda_triton.constants import TRITON_MODEL_DIR

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

#: How long an UNKNOWN model's repository entry must have been unchanged
#: before the gate requests its load (triton-model-cleanup-wedge D4); the
#: same value as ``workflow_engine.model_gate.QUIET_PERIOD_S``.
ENTRY_QUIET_PERIOD_S = 10.0


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


def _inspect_entry(target: str):
    """The repository entry of ``target`` in :data:`TRITON_MODEL_DIR`
    (read at call time), judged exactly as the continuous ModelGate judges
    it (``dda_triton.repo_entries.inspect_entry``)."""
    from dda_triton import repo_entries

    return repo_entries.inspect_entry(
        TRITON_MODEL_DIR, target, wall=time.time, quiet_period_s=ENTRY_QUIET_PERIOD_S
    )


def _stall_for(target: str):
    """The watchdog's stall of ``target`` (``load_watchdog.stall_for``), or
    None; contained, so a missing watchdog means today's wait."""
    try:
        from dda_triton import load_watchdog
    except Exception:  # noqa: BLE001 - no watchdog, no stall to report
        return None
    return load_watchdog.stall_for(target)


def _gate_wait(target: str):
    """``run_activity.gate_wait(target)``, or a no-op context when the
    registry cannot be imported (the gate never depends on it)."""
    try:
        from utils import run_activity
    except Exception:  # noqa: BLE001
        return contextlib.nullcontext(None)
    return run_activity.gate_wait(target)


def _is_release_in_progress(exc: BaseException) -> bool:
    try:
        from dda_triton.load_tracker import ModelReleaseInProgress
    except Exception:  # noqa: BLE001 - no tracker, no refusal to recognize
        return False
    return isinstance(exc, ModelReleaseInProgress)


#: Blockers that name a path that is missing or unreadable: an entry that
#: stays like this needs its model component's Startup to republish it.
_REMEDY_BLOCKER_SUFFIXES = (" is missing", " is unreadable")


def ensure_model_ready(
    model_name: str,
    resolved_name: Optional[str] = None,
    client=None,
    sleep=time.sleep,
    clock=time.monotonic,
    timeout_s: Optional[float] = None,
    poll_interval_s: Optional[float] = None,
    entry_inspector: Optional[Callable[[str], object]] = None,
    stall_lookup: Optional[Callable[[str], object]] = None,
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

    ``client``, ``sleep`` and ``clock`` are injectable for tests, and so is
    ``entry_inspector(target)``, which judges an UNKNOWN model's repository
    entry before a load is requested (None means the real one, on
    :data:`TRITON_MODEL_DIR`). A load is requested only for an absent entry
    or one that is complete and unchanged for :data:`ENTRY_QUIET_PERIOD_S`
    (triton-model-cleanup-wedge D4).

    ``stall_lookup(target)`` (None means ``load_watchdog.stall_for``)
    answers whether a LOADING model is stalled; a stalled model fails at
    once with the stall and its recovery action instead of waiting out the
    budget. From the first read that is not READY until it returns, the
    call holds ``run_activity.gate_wait(target)``, so a self-heal decision
    counts the run as blocked by the wedge while it waits here; a gate wait
    refused because the backend is restarting returns not-ready at once
    with the refusal text.

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

    inspector = entry_inspector if entry_inspector is not None else _inspect_entry
    lookup = stall_lookup if stall_lookup is not None else _stall_for
    with contextlib.ExitStack() as gate:
        return _wait_until_ready(model_name, target, triton, sleep, clock, timeout_s,
                                 poll_interval_s, inspector, lookup, gate)


def _wait_until_ready(model_name, target, triton, sleep, clock, timeout_s, poll_interval_s,
                      inspector, lookup, gate) -> ReadinessOutcome:
    """The polling loop of :func:`ensure_model_ready`; ``gate`` is the
    ``ExitStack`` that holds the run-activity gate wait."""
    started = clock()
    kicked = False
    #: The entry's blocker at the latest UNKNOWN poll that requested nothing.
    blocker: Optional[str] = None
    blocker_logged = False
    refusal_logged = False
    in_gate = False

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

        if state != STATE_READY and not in_gate:
            in_gate = True
            try:
                ticket = gate.enter_context(_gate_wait(target))
            except Exception:  # noqa: BLE001 - the gate never depends on it
                logger.debug("Could not register the gate wait of %s", target, exc_info=True)
                ticket = None
            refused = getattr(ticket, "refused", None)
            if refused:
                return ReadinessOutcome(ready=False, state=state, message=refused,
                                        waited_seconds=waited)

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

        if state == STATE_LOADING:
            try:
                stall = lookup(target)
            except Exception:  # noqa: BLE001 - no stall known: keep waiting
                logger.debug("Could not ask the watchdog about %s", target, exc_info=True)
                stall = None
            if stall is not None:
                return ReadinessOutcome(
                    ready=False,
                    state=state,
                    message=_stall_message(model_name, target, stall, clock),
                    waited_seconds=waited,
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
            # component's Startup uses — but only for an absent entry or one
            # that is complete and stable: a load of an entry that is still
            # being written or removed is the load that wedges Triton.
            try:
                inspection = inspector(target)
                present = bool(getattr(inspection, "present", False))
                blocker = getattr(inspection, "blocker", None) if present else None
            except Exception:  # noqa: BLE001 - fail open: treat as absent
                logger.debug("Could not inspect the repository entry of %s; "
                             "treating it as absent", target, exc_info=True)
                blocker = None
            if blocker is not None:
                if not blocker_logged:
                    blocker_logged = True
                    logger.info(
                        "Triton model %s is UNKNOWN but its repository entry is not "
                        "ready (%s); waiting before requesting a load (workflow "
                        "model %s)", target, blocker, model_name
                    )
            else:
                kicked = True
                logger.info(
                    "Triton model %s is UNKNOWN; requesting a load before "
                    "waiting (workflow model %s)", target, model_name
                )
                try:
                    triton.start_triton_model(target)
                except Exception as exc:  # noqa: BLE001 - a refused kick is not fatal
                    if _is_release_in_progress(exc):
                        # A removal or a replacement holds the model's lock:
                        # inspect the entry again at the next poll and ask
                        # again, so a removed entry fails fast and a replaced
                        # one is loaded.
                        kicked = False
                        if not refusal_logged:
                            refusal_logged = True
                            logger.info(
                                "Load request for Triton model %s was refused: its "
                                "repository entries are being removed or replaced; "
                                "checking the entry again before the next request "
                                "(workflow model %s)", target, model_name
                            )
                    else:
                        # The route refuses (403) unless the state is
                        # UNKNOWN/UNAVAILABLE, so a race with another loader
                        # lands here; keep waiting, the other loader is making
                        # progress.
                        logger.warning(
                            "Load request for Triton model %s was refused; "
                            "continuing to wait", target, exc_info=True
                        )

        if waited >= timeout_s:
            if state == STATE_UNKNOWN and not kicked and blocker is not None:
                return ReadinessOutcome(
                    ready=False,
                    state=state,
                    message=_entry_not_ready_message(model_name, target, blocker,
                                                     waited),
                    waited_seconds=waited,
                )
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


def _stall_message(model_name, target, stall, clock) -> str:
    """The run-gate error for a stalled model (design D8)."""
    try:
        from dda_triton.load_watchdog import fmt_dur
    except Exception:  # noqa: BLE001
        def fmt_dur(seconds):
            return "{0:.0f}s".format(seconds)
    loading_for = getattr(stall, "loading_for_s", None)
    no_progress = getattr(stall, "no_progress_s", None)
    try:
        now = clock()
        loading_for = loading_for(now) if callable(loading_for) else loading_for
        no_progress = no_progress(now) if callable(no_progress) else no_progress
    except Exception:  # noqa: BLE001
        loading_for, no_progress = None, None
    evidence = tuple(getattr(stall, "evidence", ()) or ())
    return (
        "Model '{0}' (Triton model '{1}') is not ready for inference: it is stalled, "
        "LOADING for {2} with no load progress for {3} ({4}). {5}".format(
            model_name, target, fmt_dur(loading_for or 0), fmt_dur(no_progress or 0),
            evidence[0] if evidence else "no evidence recorded",
            getattr(stall, "recovery_action", None) or "Restart the LocalServer backend.")
    )


def _entry_not_ready_message(model_name, target, blocker, waited) -> str:
    """The failure of a run whose model's repository entry never became
    complete and stable, so no load was requested (design D4)."""
    detail = (
        "Model '{0}' (Triton model '{1}') is not ready for inference: state "
        "UNKNOWN; its repository entry is not ready ({2}). Waited {3:.0f}s; no "
        "load was requested.".format(model_name, target, blocker, waited)
    )
    if str(blocker).endswith(_REMEDY_BLOCKER_SUFFIXES):
        detail += (
            " If the entry stays incomplete, restart or redeploy the model "
            "component {0} so that its Startup republishes it.".format(target)
        )
    return detail
