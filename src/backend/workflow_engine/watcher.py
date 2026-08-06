#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""WorkflowWatcher: discovers deployed Workflow_Components (Requirement 9.1).

Runs a startup scan of ``/aws_dda/workflows/`` and then keeps watching:
inotify when the ``inotify_simple`` package is available, otherwise a
polling rescan. Every discovered ``{workflowId}/{version}`` artifact set
is validated (see :mod:`workflow_engine.discovery`) and upserted into the
``workflow_registrations`` table — malformed or incompatible sets with
status ``invalid`` plus a reported reason, so they are visible but never
runnable (Requirement 13.3).

The watcher runs on its own daemon thread and touches nothing outside
its own tables and log lines: on a device without Workflow_Components it
finds an empty (or absent) directory and Pipeline_Configuration behavior
is byte-for-byte identical (Requirement 13.6). Any error in a scan cycle
is contained and logged; the watcher never brings LocalServer down.
"""

import logging
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

from workflow_engine import discovery, gst_plugins
from workflow_engine.camera_binding import (
    STATUS_RESOLVED,
    ResolutionResult,
    resolve_bindings,
)
from workflow_engine.discovery import (
    STATUS_INVALID,
    STATUS_REMOVED,
    STATUS_SUPERSEDED,
    WORKFLOWS_ROOT,
    DiscoveredArtifactSet,
)
from workflow_engine.models import WorkflowRegistration

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 5.0

#: Invalid-registration reason when a document carries binding points but
#: the ``dda-camera-bindings`` shadow cannot be read (camera-registry-sync
#: Requirements 10.2, 11.1). Legacy documents without binding points
#: register as today even then.
REASON_BINDINGS_UNAVAILABLE = "bindings unavailable"


def registration_id_for(workflow_id: str, version: str) -> str:
    """Deterministic registration id so rescans upsert instead of duplicating."""
    return f"{workflow_id}:{version}"


def _numeric_version(version: str) -> Optional[int]:
    """The version as an int when it parses as one, else None.

    Only integer-parsing version directories participate in supersession
    ordering; non-numeric directories (manual tinkering) never supersede
    and are never superseded."""
    try:
        return int(version)
    except (TypeError, ValueError):
        return None


class WorkflowWatcher:
    """Startup scan + filesystem watch of the workflow artifact root."""

    def __init__(
        self,
        session_factory: Optional[Callable] = None,
        root: str = WORKFLOWS_ROOT,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        device_arch: Optional[str] = None,
        running_version: Optional[str] = None,
        plugins_root: str = gst_plugins.DEVICE_PLUGINS_ROOT,
        binding_store=None,
        inventory_provider: Optional[Callable] = None,
    ) -> None:
        if session_factory is None:
            # Imported lazily so the module is importable without the
            # COMPONENT_WORK_PATH environment the DAO layer requires.
            from dao.sqlite_db.sqlite_db_operations import SessionLocal

            session_factory = SessionLocal
        self._session_factory = session_factory
        self._root = root
        self._poll_interval = poll_interval
        self._device_arch = device_arch
        self._running_version = running_version
        self._plugins_root = plugins_root
        # Camera_Binding resolution (camera-registry-sync Requirements
        # 10.2, 10.4, 11.1). ``binding_store`` is a CameraBindingStore (or
        # a fake exposing ``bindings_for``/``invalidate``); when None the
        # feature is unwired and every document registers exactly as
        # before. ``inventory_provider`` is a zero-argument callable
        # returning the device-local Camera_Source inventory (the
        # ``build_inventory`` merge) — injectable for tests.
        self.binding_store = binding_store
        self._inventory_provider = inventory_provider
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # registration id -> reason, reported for invalid artifact sets.
        # Repopulated on every scan (including the startup scan), so the
        # API can surface the reason alongside the stored status.
        self._invalid_reasons: Dict[str, str] = {}
        self._reasons_lock = threading.Lock()
        # registration id -> ResolutionResult for registrations whose
        # bindings resolved (substituted document + adapter assignments).
        self._binding_resolutions: Dict[str, ResolutionResult] = {}
        # Additive registration-change listeners (trigger-activation-
        # runtime Requirement 6.1): zero-argument callables invoked after
        # every ``sync_once`` reconciliation — which includes removed/
        # superseded marking — so the TriggerSubscriptionManager can diff
        # the
        # registered trigger-driven artifact sets. Each listener is
        # contained: a failure never disturbs the scan or LocalServer.
        self.registrations_listeners: List[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Run the startup scan, then watch for changes on a daemon thread."""
        try:
            self.sync_once()
        except Exception:  # noqa: BLE001 - never take LocalServer down
            logger.exception("Workflow startup scan failed; will retry in watch loop")
        self._thread = threading.Thread(
            target=self._watch_loop, name="workflow-watcher", daemon=True
        )
        self._thread.start()
        logger.info("WorkflowWatcher started (root=%s)", self._root)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval + 1)

    def invalid_reason(self, registration_id: str) -> Optional[str]:
        """The reported reason for an invalid registration, if known."""
        with self._reasons_lock:
            return self._invalid_reasons.get(registration_id)

    def binding_resolution(self, registration_id: str) -> Optional[ResolutionResult]:
        """The latest successful Camera_Binding resolution for a
        registration (substituted document + adapter assignments), or
        None when the document has no applied bindings."""
        with self._reasons_lock:
            return self._binding_resolutions.get(registration_id)

    def on_discovery_change(self, snapshot=None) -> None:
        """Camera_Discovery ``on_change`` hook: the local inventory
        changed, so re-resolve registrations — an invalid registration
        flips to registered when its camera appeared (Requirement 10.4)."""
        self._resync_for_bindings()

    def on_bindings_delta(self, message=None) -> None:
        """Bindings-shadow delta hook: refresh the cached bindings and
        re-resolve registrations (Requirement 10.4)."""
        if self.binding_store is not None:
            try:
                self.binding_store.invalidate()
            except Exception:  # noqa: BLE001 - hook isolation (11.2)
                logger.exception("Could not invalidate the camera-binding cache")
        self._resync_for_bindings()

    def _resync_for_bindings(self) -> None:
        """One contained rescan; ``sync_once`` re-runs binding resolution
        for every artifact set, so status flips both ways as cameras and
        bindings come and go."""
        try:
            self.sync_once()
        except Exception:  # noqa: BLE001 - never take LocalServer down
            logger.exception("Camera-binding re-resolution scan failed")

    def sync_once(self) -> List[str]:
        """One full scan/validate/register pass.

        Returns the registration ids that were created or updated.
        Safe to call from any thread; each call uses its own session.

        Stale-registration reconciliation (stale-workflow-registrations
        bugfix): among the integer-parsing version directories of one
        workflow only the HIGHEST is the deployed one — it validates and
        registers exactly as before, while every lower numeric version is
        upserted as ``superseded`` (not runnable, filtered from the
        default listing). Non-numeric version directories never
        participate in supersession and register as before. Rows whose
        artifact directory disappeared are marked ``removed``. A
        directory that reappears (component restart re-copy, rollback)
        goes back through this normal path, so non-active rows flip back
        to ``registered``/``invalid``/``superseded`` per the current disk
        state. Rows and executions are never deleted.
        """
        artifact_sets = discovery.scan_workflow_root(self._root)
        touched: List[str] = []
        session = self._session_factory()
        try:
            seen_ids = set()
            by_workflow: Dict[str, List[DiscoveredArtifactSet]] = {}
            for artifact_set in artifact_sets:
                by_workflow.setdefault(artifact_set.workflow_id, []).append(
                    artifact_set
                )

            for sets in by_workflow.values():
                numeric = [
                    s for s in sets if _numeric_version(s.version) is not None
                ]
                highest = (
                    max(numeric, key=lambda s: _numeric_version(s.version))
                    if numeric
                    else None
                )
                for artifact_set in sets:
                    registration_id = registration_id_for(
                        artifact_set.workflow_id, artifact_set.version
                    )
                    seen_ids.add(registration_id)
                    is_superseded = (
                        highest is not None
                        and artifact_set is not highest
                        and _numeric_version(artifact_set.version) is not None
                    )
                    if is_superseded:
                        changed = self._mark_superseded(
                            session, registration_id, artifact_set,
                            highest.version,
                        )
                    else:
                        changed = self._register(
                            session, registration_id, artifact_set
                        )
                    if changed:
                        touched.append(registration_id)

            touched.extend(self._mark_removed(session, seen_ids))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        self._notify_registrations_listeners()
        return touched

    def _notify_registrations_listeners(self) -> None:
        """Invoke the additive registration-change listeners, each
        contained so one failing listener never affects the others, the
        scan cycle, or LocalServer."""
        for listener in list(self.registrations_listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - listener isolation
                logger.exception(
                    "Workflow registrations listener failed; continuing"
                )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(
        self,
        session,
        registration_id: str,
        artifact_set: DiscoveredArtifactSet,
    ) -> bool:
        """Upsert one artifact set. Returns True when the row changed."""
        validation = discovery.validate_artifact_set(
            artifact_set,
            device_arch=self._device_arch,
            running_version=self._running_version,
            plugins_root=self._plugins_root,
        )

        # Camera_Binding resolution can downgrade a structurally valid
        # artifact set to invalid (missing camera source / bindings
        # unavailable — Requirements 10.2, 11.1); the effective status and
        # reason drive the row, the reasons map, and the log lines below.
        status, reason, resolution = self._resolve_camera_bindings(
            artifact_set, validation
        )
        is_valid = status != STATUS_INVALID

        with self._reasons_lock:
            if is_valid:
                self._invalid_reasons.pop(registration_id, None)
            else:
                self._invalid_reasons[registration_id] = reason or ""
            if resolution is not None and is_valid:
                self._binding_resolutions[registration_id] = resolution
            else:
                self._binding_resolutions.pop(registration_id, None)

        row = session.get(WorkflowRegistration, registration_id)
        if row is None:
            session.add(
                WorkflowRegistration(
                    id=registration_id,
                    workflow_id=artifact_set.workflow_id,
                    version=artifact_set.version,
                    arch=validation.arch,
                    artifact_path=artifact_set.path,
                    status=status,
                    registered_at=int(time.time()),
                )
            )
            changed = True
        else:
            changed = (
                row.status != status
                or row.arch != validation.arch
                or row.artifact_path != artifact_set.path
            )
            if changed:
                row.arch = validation.arch
                row.artifact_path = artifact_set.path
                row.status = status
                row.registered_at = int(time.time())

        if not is_valid:
            # Reported, never runnable (Requirements 9.1, 13.3).
            logger.error(
                "Workflow artifact set %s registered as invalid: %s",
                registration_id,
                reason,
            )
        elif changed:
            logger.info(
                "Workflow %s version %s registered as runnable (arch=%s)",
                artifact_set.workflow_id,
                artifact_set.version,
                validation.arch,
            )
        return changed

    def _resolve_camera_bindings(self, artifact_set, validation):
        """Camera_Binding resolution for one artifact set (camera-registry-
        sync Requirements 10.2, 10.4, 11.1).

        Returns ``(status, reason, resolution)`` — the effective
        registration status/reason after resolution, and the
        ResolutionResult when bindings were applied. Rules:

        - No binding store wired, or structurally invalid artifact set:
          the discovery validation stands unchanged.
        - Bindings shadow unreadable: documents *with* binding points are
          invalid with reason ``bindings unavailable``; documents without
          binding points register as today (11.1).
        - An unresolved ``cameraSourceId`` (or override violation) marks
          the registration invalid with the resolver's reasons —
          ``missing camera source {csid}`` — so the existing
          invalid-registration path rejects triggers (10.2).
        """
        if not validation.is_valid or self.binding_store is None:
            return validation.status, validation.reason, None

        document = validation.compiled_document or {}
        has_binding_points = bool(document.get("bindingPoints"))

        try:
            bindings = self.binding_store.bindings_for(
                artifact_set.workflow_id, artifact_set.version
            )
        except Exception:  # noqa: BLE001 - store isolation (11.2)
            logger.exception("Camera-binding lookup failed")
            bindings = None

        if bindings is None:
            # Bindings shadow unreadable (10.2/11.1). Legacy documents
            # (no bindingPoints) never needed bindings — register as today.
            if has_binding_points:
                return STATUS_INVALID, REASON_BINDINGS_UNAVAILABLE, None
            return validation.status, validation.reason, None

        if not has_binding_points or not bindings:
            # Pre-feature document, or nothing bound: the compiled-in
            # values run exactly as before (10.5, 11.1).
            return validation.status, validation.reason, None

        resolution = resolve_bindings(document, bindings, self._local_inventory())
        if resolution.status != STATUS_RESOLVED:
            return STATUS_INVALID, "; ".join(resolution.errors), resolution
        return validation.status, validation.reason, resolution

    def _local_inventory(self):
        """The device-local Camera_Source inventory from the injected
        provider; a provider failure resolves against an empty inventory
        (the next re-resolution pass recovers, 10.4)."""
        if self._inventory_provider is None:
            return {}
        try:
            return self._inventory_provider()
        except Exception:  # noqa: BLE001 - provider isolation (11.2)
            logger.exception("Local camera inventory read failed")
            return {}

    def _mark_superseded(
        self,
        session,
        registration_id: str,
        artifact_set: DiscoveredArtifactSet,
        highest_version: str,
    ) -> bool:
        """Upsert a lower-than-highest numeric version as ``superseded``.

        Artifact validation is skipped — a superseded version is not
        runnable regardless of its artifact state. Returns True when the
        row changed. The row (and its executions) is preserved; a later
        scan where this version is the highest again flips it back to an
        active status through the normal ``_register`` path.
        """
        reason = f"superseded by version {highest_version}"
        with self._reasons_lock:
            self._invalid_reasons[registration_id] = reason
            self._binding_resolutions.pop(registration_id, None)

        row = session.get(WorkflowRegistration, registration_id)
        if row is None:
            session.add(
                WorkflowRegistration(
                    id=registration_id,
                    workflow_id=artifact_set.workflow_id,
                    version=artifact_set.version,
                    arch="unknown",
                    artifact_path=artifact_set.path,
                    status=STATUS_SUPERSEDED,
                    registered_at=int(time.time()),
                )
            )
            changed = True
        else:
            changed = (
                row.status != STATUS_SUPERSEDED
                or row.artifact_path != artifact_set.path
            )
            if changed:
                row.artifact_path = artifact_set.path
                row.status = STATUS_SUPERSEDED
                row.registered_at = int(time.time())

        if changed:
            logger.info(
                "Workflow registration %s marked superseded: %s",
                registration_id,
                reason,
            )
        return changed

    def _mark_removed(self, session, seen_ids) -> List[str]:
        """Mark registrations whose artifact directory disappeared as
        ``removed`` (from any prior status, idempotently).

        This is what the fixed recipe's Shutdown cleanup produces on
        component replace/remove. The row and its execution history are
        never deleted; a reappearing directory flips the registration
        back to an active status on the next scan."""
        touched: List[str] = []
        reason = "Artifact directory was removed"
        rows = session.query(WorkflowRegistration).all()
        for row in rows:
            if row.id in seen_ids:
                continue
            with self._reasons_lock:
                self._invalid_reasons[row.id] = reason
                self._binding_resolutions.pop(row.id, None)
            if row.status == STATUS_REMOVED:
                continue  # already retired; no touched-churn
            row.status = STATUS_REMOVED
            logger.info(
                "Workflow registration %s marked removed: %s", row.id, reason
            )
            touched.append(row.id)
        return touched

    # ------------------------------------------------------------------
    # Watch loop (inotify with polling fallback)
    # ------------------------------------------------------------------

    def _watch_loop(self) -> None:
        inotify = self._try_setup_inotify()
        while not self._stop_event.is_set():
            try:
                if inotify is not None:
                    self._inotify_wait(inotify)
                else:
                    self._stop_event.wait(self._poll_interval)
                if self._stop_event.is_set():
                    break
                self.sync_once()
            except Exception:  # noqa: BLE001 - never take LocalServer down
                logger.exception("Workflow watch cycle failed; continuing")
                self._stop_event.wait(self._poll_interval)

    def _try_setup_inotify(self):
        """inotify_simple instance, or None to use the polling fallback."""
        try:
            from inotify_simple import INotify  # type: ignore

            return INotify()
        except Exception:  # noqa: BLE001 - optional dependency
            logger.info(
                "inotify unavailable; WorkflowWatcher polling %s every %ss",
                self._root,
                self._poll_interval,
            )
            return None

    def _inotify_wait(self, inotify) -> None:
        """Refresh watches, then block until something changes (or timeout).

        Watches are (re)added for the root and its two directory levels on
        every pass — inotify watches are idempotent per path — so newly
        created workflow/version directories are picked up. A timeout-based
        rescan still runs as a safety net.
        """
        import os

        from inotify_simple import flags  # type: ignore

        watch_flags = (
            flags.CREATE | flags.DELETE | flags.CLOSE_WRITE
            | flags.MOVED_TO | flags.MOVED_FROM | flags.DELETE_SELF
        )
        paths = [self._root]
        for artifact_set in discovery.scan_workflow_root(self._root):
            paths.append(os.path.dirname(artifact_set.path))
            paths.append(artifact_set.path)
        for path in paths:
            try:
                inotify.add_watch(path, watch_flags)
            except OSError:
                continue

        # Block until events arrive or the timeout elapses; either way the
        # caller rescans (sync_once is cheap and idempotent), so a missed
        # event only delays registration by one poll interval.
        inotify.read(timeout=int(self._poll_interval * 1000))


def new_execution_id() -> str:
    return str(uuid.uuid4())
