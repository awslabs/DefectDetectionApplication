"""State_Restoration registry for the Edge_Test_Harness (Reqs 4.3, 6.4, 8.3).

The :class:`StateRegistry` records every model/workflow the harness acts on as
``(kind, name, pre_state)``. Session teardown calls :meth:`restore_all`, which
reverses the harness's starts in LIFO order — on success and failure alike
(the registry is driven from ``yield``-fixture teardown, which pytest runs
regardless of test outcome).

Two guarantees:

* **Found-running entries are untouched** (Reqs 4.3, 6.4): only entries whose
  pre-run state was not an active one are stopped; components the harness
  found already running are left exactly as found.
* **Restoration never masks a test outcome** (design: Error Handling):
  failures during restoration are logged and collected as warnings for the
  results bundle (``restoration_warnings``) — they are never raised.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Pre-run states meaning the component was already active when the harness
#: found it. Entries recorded with one of these pre-states are never stopped
#: by restoration — the device must be left in the state it was found.
RUNNING_PRE_STATES = frozenset({"READY", "RUNNING", "LOADING", "STARTING"})


@dataclass(frozen=True)
class RestorationEntry:
    """One component the harness acted on: ``(kind, name, pre_state)``."""

    kind: str
    name: str
    pre_state: Optional[str]

    @property
    def harness_started(self) -> bool:
        """True when the harness started this component (its pre-run state
        was not active), so restoration must stop it (Reqs 4.3, 6.4)."""
        return self.pre_state not in RUNNING_PRE_STATES


class StateRegistry:
    """Session registry upholding State_Restoration (Req 8.3).

    Stages call :meth:`record` for every component they touch, passing the
    device-reported pre-run state and a zero-argument ``stop`` callable that
    restores it (typically ``lambda: client.stop_model(name)``). Keeping the
    stop action a callable decouples the registry from the client and lets
    one registry restore models and workflows alike.
    """

    def __init__(self):
        self._entries: List[Tuple[RestorationEntry, Callable[[], Any]]] = []
        #: Warnings from failed restoration attempts, for ``restoration_warnings``
        #: in results.json. Never cleared: the results plugin reads it after
        #: teardown.
        self.warnings: List[str] = []

    def record(
        self,
        kind: str,
        name: str,
        pre_state: Optional[str],
        stop: Callable[[], Any],
    ) -> RestorationEntry:
        """Record one component the harness acted on.

        :param kind: component kind, e.g. ``"model"`` or ``"workflow"``.
        :param name: device-side component name.
        :param pre_state: device-reported state observed *before* the harness
            acted; determines whether restoration stops the component.
        :param stop: zero-argument callable issuing the restoring stop.
        """
        entry = RestorationEntry(kind=kind, name=name, pre_state=pre_state)
        self._entries.append((entry, stop))
        return entry

    @property
    def entries(self) -> "tuple[RestorationEntry, ...]":
        """Recorded entries in record order (for auditing and tests)."""
        return tuple(entry for entry, _ in self._entries)

    def restore_all(self) -> List[str]:
        """Reverse-order teardown: stop only harness-started entries.

        Iterates the recorded entries in LIFO order; entries whose pre-run
        state was active are skipped (found running → left untouched). A
        failing stop is logged and collected into :attr:`warnings` — it never
        raises, so restoration cannot mask the test outcome (Req 8.3).

        Recorded entries are consumed: a second call is a no-op.

        :returns: the accumulated warnings (also kept on :attr:`warnings`).
        """
        entries, self._entries = self._entries, []
        for entry, stop in reversed(entries):
            if not entry.harness_started:
                logger.info(
                    "Restoration: leaving %s %r untouched (found in state %s)",
                    entry.kind,
                    entry.name,
                    entry.pre_state,
                )
                continue
            try:
                stop()
            except Exception as err:  # noqa: BLE001 — must never mask outcomes
                warning = (
                    f"Restoration failed for {entry.kind} {entry.name!r} "
                    f"(pre-run state: {entry.pre_state}): {err}"
                )
                logger.warning(warning)
                self.warnings.append(warning)
        return list(self.warnings)
