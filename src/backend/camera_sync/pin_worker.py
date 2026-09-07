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
"""Cloud-initiated static-image pin worker (feature:
cloud-static-camera-provisioning).

:class:`StaticImagePinWorker` consumes the ``desired.staticImagePin``
single-slot section of the ``dda-camera-registry`` named shadow (handed
over by ``EdgeSyncAgent``), retrieves and checksum-verifies the referenced
image through S3 with the device's ambient TES credentials, applies it
through the exact same :class:`~utils.static_image_camera.StaticImageStore`
primitives the Device_Pin_API uses (``pin_bytes`` / ``unpin`` — so
validation, atomic replacement, persistence, and enumeration are
definitionally identical to a device-initiated pin, Requirements 3.1–3.3,
7.3), and echoes the processed desired fields back under
``reported.staticImagePin`` with the outcome (the echo equality is what
silences the shadow delta).

Key behaviors:

- **Newest-wins single slot** (Requirements 5.4, 7.3): :meth:`on_desired`
  queues at most one desired document; a newer one replaces any queued
  older one. Processing runs on a dedicated daemon thread (started with
  :meth:`start`) so downloads — up to 3 × 120 s — never block the agent's
  camera-report scheduling.
- **Partial-delta tolerance** (bug found on hardware): AWS IoT computes
  the shadow delta per-field against the reported state, so a desired
  field equal to the previous request's echo (e.g. ``op``/``bucket`` on a
  pin→pin replace) is omitted from the delivered document. When required
  fields are missing, :meth:`process_one` fetches the CURRENT full
  desired document through the shadow accessor and uses it iff its
  ``requestId`` matches the delivery's; a failed GET or a mismatched
  requestId is reported ``failed`` naming the incomplete delivery —
  never a silent hang.
- **Retrieval retry policy** (Requirements 2.8–2.11): at most
  :data:`RETRIEVAL_MAX_ATTEMPTS` attempts, each bounded at
  :data:`RETRIEVAL_ATTEMPT_TIMEOUT_SECONDS` by a wall-clock bound on the
  streamed read (plus botocore connect/read timeouts on the default
  client), at least :data:`RETRIEVAL_RETRY_SPACING_SECONDS` between
  consecutive attempts. The sha256 is computed over the streamed bytes
  and compared before any store call; a mismatch discards the bytes and
  counts as one failed attempt; the download aborts past the 50 MB pin
  limit (defense in depth — ``pin_bytes`` re-enforces it). After the
  final failure the store is never invoked and the reported status is
  ``failed`` with a reason naming the final attempt's cause
  (``retrieval failure: …`` or ``checksum mismatch``).
- **Idempotence marker** (Requirements 3.5, 7.4): each terminal outcome
  is persisted atomically (temp + ``os.replace``, the pin store's
  discipline) at ``$COMPONENT_WORK_PATH/static_image_camera/`` +
  :data:`MARKER_FILE_NAME`. A delivery whose ``requestId`` matches the
  marker re-reports the recorded outcome without re-executing; a corrupt
  or missing marker is treated as no marker (re-applying is safe:
  ``pin_bytes`` of the same bytes reproduces the same state, removal of
  an empty store is a no-op).
- **Confirmation ordering** (Requirements 3.6, 7.7): the reported echo is
  written only after the store call returns (the store's ``os.replace``
  makes the image available to frame grabs before that return) and after
  the marker write. Shadow writes merge at the top level, so the echo
  never clobbers ``reported.cameras``.
- **Inventory trigger**: after any fresh terminal outcome the worker
  invokes the injected ``report_inventory`` callback (the agent's) so the
  camera-inventory change publishes promptly (Requirement 6.1).

All collaborators are injectable (the ``EdgeSyncAgent`` testing pattern):
fake shadow accessor, fake S3 client factory, temp-dir store factory and
marker path, fake clock/sleep.
"""
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from utils.static_image_camera import (
    MAX_PIN_FILE_BYTES,
    StaticImagePinError,
    get_store,
)

logger = logging.getLogger(__name__)

#: Idempotence marker file name, next to the pin store's files so it
#: shares the Pinned_Image's lifetime across restarts (design Decision 3).
MARKER_FILE_NAME = "applied_pin_request.json"

#: Retrieval retry policy (Requirements 2.9, 2.10, 2.11).
RETRIEVAL_MAX_ATTEMPTS = 3
RETRIEVAL_ATTEMPT_TIMEOUT_SECONDS = 120.0
RETRIEVAL_RETRY_SPACING_SECONDS = 5.0

#: botocore timeouts for the default S3 client — kept inside the 120 s
#: per-attempt wall-clock bound so a stuck socket cannot exceed it.
BOTO_CONNECT_TIMEOUT_SECONDS = 30.0
BOTO_READ_TIMEOUT_SECONDS = 60.0

#: Terminal Sync_Status values reported through the shadow echo.
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"

#: Desired-document operation types (design section 3 document shapes).
OP_PIN = "pin"
OP_REMOVE = "remove"

#: Streaming chunk size for the S3 download; the size cap and the
#: wall-clock bound are enforced as the stream accumulates.
_DOWNLOAD_CHUNK_BYTES = 1 << 20

#: Substring identifying the pin store's "nothing to remove" error, which
#: a removal Pin_Request maps to a successful no-op confirmation
#: (Requirement 7.4 — removal converges to "no Pinned_Image").
_NO_IMAGE_PINNED_MARKER = "no image is pinned"


class _RetrievalFailure(Exception):
    """Every retrieval attempt for a pin request failed. The message names
    the final attempt's cause — ``retrieval failure: …`` or
    ``checksum mismatch`` (Requirement 2.11)."""


class _PartialDeliveryFailure(Exception):
    """A partial delta document could not be resolved to the full desired
    document (shadow GET failed or the slot's requestId no longer matches
    the delivery's). The request is reported ``failed`` with this message
    — never silently dropped (which would hang the request pending
    forever on the portal side)."""


def _is_partial_delivery(desired: Mapping[str, Any]) -> bool:
    """Whether a delivered desired document is missing fields its
    operation requires — the partial-delta case (see
    :meth:`StaticImagePinWorker.process_one`): no ``op`` at all, or a pin
    operation missing any of the transport reference fields."""
    op = desired.get("op")
    if not op:
        return True
    if op == OP_PIN and any(
        not desired.get(field) for field in ("bucket", "key", "sha256")
    ):
        return True
    return False


def default_marker_path() -> str:
    """The production marker location, resolved lazily so importing this
    module never requires ``COMPONENT_WORK_PATH``."""
    return os.path.join(
        os.environ["COMPONENT_WORK_PATH"], "static_image_camera", MARKER_FILE_NAME
    )


class StaticImagePinWorker:
    """Applies cloud-initiated static-image Pin_Requests on the device.

    ``iot_shadow_accessor`` is the existing ``IoTShadowAccessor`` (or a
    fake exposing ``update_thing_shadow_state_request``). ``store_factory``
    yields the pin store (default: the module singleton).
    ``s3_client_factory`` yields the S3 client (default: a lazily created
    boto3 client on the device's ambient TES credentials — the
    ``workflow_engine.payload_fetch`` pattern). ``report_inventory`` is the
    agent's inventory-report trigger, invoked after any fresh terminal
    outcome.
    """

    def __init__(
        self,
        iot_shadow_accessor,
        thing_name: str,
        shadow_name: str,
        store_factory: Callable = get_store,
        s3_client_factory: Optional[Callable] = None,
        marker_path: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
        report_inventory: Optional[Callable[[], None]] = None,
    ):
        self._shadow = iot_shadow_accessor
        self.thing_name = thing_name
        self.shadow_name = shadow_name
        self._store_factory = store_factory
        self._s3_client_factory = s3_client_factory
        self._marker_path = marker_path
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock
        #: Invoked after any fresh terminal outcome (settable post-init by
        #: the owning agent).
        self.report_inventory = report_inventory

        self._s3 = None
        self._cond = threading.Condition()
        self._pending: Optional[Dict[str, Any]] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the processing daemon thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="static-image-pin-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the processing thread and wait for it to exit."""
        self._stop_event.set()
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join()
        self._thread = None

    # --- desired-document intake (Requirements 5.4, 7.3) --------------------

    def on_desired(self, desired: Optional[Mapping]) -> None:
        """Queue a ``desired.staticImagePin`` document for processing.

        Single-slot, newest-wins: a newer document replaces any queued
        older one, mirroring the shadow's single desired slot locally so
        superseded requests queued in a burst are never executed
        (Requirement 5.4). Safe from any thread.
        """
        if not isinstance(desired, Mapping) or not desired:
            return
        with self._cond:
            self._pending = dict(desired)
            self._cond.notify_all()

    def process_pending(self) -> Optional[Dict[str, Any]]:
        """Pop the queued slot (if any) and process it — one synchronous
        step; the thread loop's body and the deterministic test seam.
        Returns the reported document, or ``None`` when nothing was
        queued."""
        with self._cond:
            desired = self._pending
            self._pending = None
        if desired is None:
            return None
        return self.process_one(desired)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            with self._cond:
                while self._pending is None and not self._stop_event.is_set():
                    self._cond.wait()
                if self._stop_event.is_set():
                    return
            try:
                self.process_pending()
            except Exception:  # noqa: BLE001 - worker isolation
                logger.exception("Static-image pin worker cycle failed")

    # --- one full cycle ------------------------------------------------------

    def process_one(self, desired: Mapping) -> Optional[Dict[str, Any]]:
        """One full cycle: marker check → (pin) retrieve + verify with the
        retry policy → apply via the pin store → marker write → reported
        echo. Returns the reported document."""
        if not isinstance(desired, Mapping):
            return None
        desired = dict(desired)
        request_id = desired.get("requestId")
        if not request_id:
            logger.warning(
                "Ignoring static-image pin desired document without a "
                "requestId: %s",
                desired,
            )
            return None

        # Partial-delta resolution (bug found on hardware): AWS IoT
        # computes the delta per-field against the reported state, so any
        # desired field equal to the previous request's echo — e.g. `op`
        # and `bucket` on a pin→pin replace — is omitted from the
        # delivered document, starving the request of required fields.
        # Resolve by fetching the CURRENT full desired document from the
        # shadow and using it iff its requestId matches this delivery's.
        # Newest-wins is preserved: the GET returns the current single
        # slot, which is definitionally the newest request. Resolved
        # before the marker check; no marker re-evaluation is needed
        # afterwards because requestId is always present in a delta (it
        # changes with every request). A failed resolution is carried
        # into the outcome below — never a silent drop.
        resolution_failure: Optional[str] = None
        if _is_partial_delivery(desired):
            try:
                desired = self._resolve_partial_delivery(desired, request_id)
            except _PartialDeliveryFailure as exc:
                resolution_failure = str(exc)

        # Idempotence: a request already carried to a terminal outcome is
        # re-reported from the marker, never re-executed (Reqs 3.5, 7.4).
        marker = self._read_marker()
        if marker is not None and marker.get("requestId") == request_id:
            report = self._build_report(
                desired,
                str(marker.get("status") or STATUS_FAILED),
                marker.get("reason"),
                marker.get("metadata"),
                marker.get("completedAtEpochMs"),
            )
            self._write_reported(report)
            return report

        op = str(desired.get("op") or "")
        status = STATUS_APPLIED
        reason: Optional[str] = None
        metadata: Optional[Dict[str, Any]] = None
        try:
            if resolution_failure is not None:
                raise _PartialDeliveryFailure(resolution_failure)
            if op == OP_REMOVE:
                metadata = self._apply_remove()
            elif op == OP_PIN:
                data = self._retrieve_verified(desired)
                metadata = self._apply_pin(desired, data)
            else:
                raise StaticImagePinError(
                    "unsupported static-image pin operation '{}'".format(op)
                )
        except _PartialDeliveryFailure as exc:
            status = STATUS_FAILED
            reason = str(exc)
        except _RetrievalFailure as exc:
            status = STATUS_FAILED
            reason = str(exc)
        except StaticImagePinError as exc:
            # The store's descriptive validation/storage error, verbatim
            # (Requirements 3.4, 7.8); the prior Pinned_Image is untouched
            # by the store's own guarantee.
            status = STATUS_FAILED
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - apply isolation
            logger.exception(
                "Applying static-image pin request %s failed", request_id
            )
            status = STATUS_FAILED
            reason = str(exc)

        completed_ms = int(self._wall_clock() * 1000)
        marker_doc: Dict[str, Any] = {
            "requestId": request_id,
            "op": op,
            "status": status,
            "metadata": metadata,
            "completedAtEpochMs": completed_ms,
        }
        if reason:
            marker_doc["reason"] = reason
        self._write_marker(marker_doc)

        report = self._build_report(desired, status, reason, metadata, completed_ms)
        self._write_reported(report)
        self._notify_inventory()
        return report

    def applied_request_id(self) -> Optional[str]:
        """The requestId recorded by the idempotence marker, or ``None``
        (startup catch-up seam for the owning agent, Requirement 5.2)."""
        marker = self._read_marker()
        if marker is None:
            return None
        request_id = marker.get("requestId")
        return str(request_id) if request_id else None

    def applied_marker(self) -> Optional[Dict[str, Any]]:
        """The full recorded terminal-outcome marker document, or ``None``
        when there is no (readable) marker.

        Seam for the owning agent's stable ``absent_since`` derivation
        (Requirement 6.2, second hardware finding): a marker recording an
        applied ``remove`` carries the removal's ``completedAtEpochMs``,
        which the agent reports as the Static_Image_Camera's
        ``absentSince`` — stable across reports and restarts."""
        return self._read_marker()

    # --- partial-delta resolution (hardware-found bug) ------------------------

    def _resolve_partial_delivery(
        self, delta_doc: Mapping[str, Any], request_id: str
    ) -> Dict[str, Any]:
        """Fetch the CURRENT full ``desired.staticImagePin`` document
        through the shadow accessor to fill in the fields a partial delta
        omitted.

        The full document is used iff its ``requestId`` matches the
        delivery's; otherwise (or when the GET fails — the production
        accessor swallows errors and returns ``None``/``False``) a
        :class:`_PartialDeliveryFailure` is raised so the request is
        reported ``failed`` naming the incomplete delivery, never
        silently hung."""
        state = None
        try:
            state = self._shadow.get_thing_shadow_state_request(
                self.thing_name, self.shadow_name
            )
        except Exception as exc:  # noqa: BLE001 - GET failure -> reported
            raise _PartialDeliveryFailure(
                "incomplete delta delivery for request '{}': required "
                "fields were omitted and the full desired document could "
                "not be read from the shadow ({})".format(request_id, exc)
            ) from exc
        if not isinstance(state, Mapping):
            raise _PartialDeliveryFailure(
                "incomplete delta delivery for request '{}': required "
                "fields were omitted and the full desired document could "
                "not be read from the shadow".format(request_id)
            )
        desired = state.get("desired")
        full = desired.get("staticImagePin") if isinstance(desired, Mapping) else None
        if not isinstance(full, Mapping) or full.get("requestId") != request_id:
            raise _PartialDeliveryFailure(
                "incomplete delta delivery for request '{}': required "
                "fields were omitted and the shadow's current desired "
                "document does not carry this request".format(request_id)
            )
        logger.info(
            "Resolved a partial delta delivery for static-image pin "
            "request %s from the shadow's full desired document",
            request_id,
        )
        return dict(full)

    # --- retrieval (Requirements 2.8-2.11) -----------------------------------

    def _retrieve_verified(self, desired: Mapping) -> bytes:
        """Download and sha256-verify the referenced image content.

        Returns the verified bytes; raises :class:`_RetrievalFailure`
        naming the final attempt's cause when all
        :data:`RETRIEVAL_MAX_ATTEMPTS` attempts fail. No store call is
        ever made from here — verification strictly precedes apply
        (Requirement 2.8)."""
        bucket = str(desired.get("bucket") or "")
        key = str(desired.get("key") or "")
        expected_sha = str(desired.get("sha256") or "").strip().lower()
        last_cause = "retrieval failure: no attempt completed"
        for attempt in range(1, RETRIEVAL_MAX_ATTEMPTS + 1):
            if attempt > 1:
                # At least 5 seconds between consecutive attempts (2.10).
                self._sleep(RETRIEVAL_RETRY_SPACING_SECONDS)
            try:
                data, digest = self._download_once(bucket, key)
            except Exception as exc:  # noqa: BLE001 - every cause counts (2.9)
                last_cause = "retrieval failure: {}".format(exc)
                logger.warning(
                    "Static-image pin retrieval attempt %d/%d for s3://%s/%s "
                    "failed: %s",
                    attempt,
                    RETRIEVAL_MAX_ATTEMPTS,
                    bucket,
                    key,
                    exc,
                )
                continue
            if digest != expected_sha:
                # Discard the bytes; the mismatch is one failed attempt (2.9).
                del data
                last_cause = "checksum mismatch"
                logger.warning(
                    "Static-image pin retrieval attempt %d/%d for s3://%s/%s "
                    "returned bytes not matching the declared checksum",
                    attempt,
                    RETRIEVAL_MAX_ATTEMPTS,
                    bucket,
                    key,
                )
                continue
            return data
        raise _RetrievalFailure(last_cause)

    def _download_once(self, bucket: str, key: str) -> Tuple[bytes, str]:
        """One streamed GET attempt, bounded by the 120 s wall clock and
        the 50 MB pin limit; returns ``(bytes, sha256 hex digest)`` with
        the digest computed over the streamed chunks."""
        client = self._get_s3_client()
        deadline = self._clock() + RETRIEVAL_ATTEMPT_TIMEOUT_SECONDS
        body = client.get_object(Bucket=bucket, Key=key)["Body"]
        hasher = hashlib.sha256()
        chunks = []
        total = 0
        while True:
            if self._clock() >= deadline:
                raise TimeoutError(
                    "the retrieval attempt did not complete within {} "
                    "seconds".format(int(RETRIEVAL_ATTEMPT_TIMEOUT_SECONDS))
                )
            chunk = body.read(_DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PIN_FILE_BYTES:
                raise ValueError(
                    "downloaded content exceeds the {}-byte pin size "
                    "limit".format(MAX_PIN_FILE_BYTES)
                )
            hasher.update(chunk)
            chunks.append(chunk)
        return b"".join(chunks), hasher.hexdigest()

    def _get_s3_client(self):
        """The injected S3 client, or a lazily created boto3 client on the
        device's ambient TES credentials (the ``payload_fetch`` pattern)."""
        if self._s3 is None:
            if self._s3_client_factory is not None:
                self._s3 = self._s3_client_factory()
            else:
                import boto3
                from botocore.config import Config

                self._s3 = boto3.client(
                    "s3",
                    config=Config(
                        connect_timeout=BOTO_CONNECT_TIMEOUT_SECONDS,
                        read_timeout=BOTO_READ_TIMEOUT_SECONDS,
                        retries={"max_attempts": 0},
                    ),
                )
        return self._s3

    # --- apply (Requirements 3.1, 7.3, 7.4) ----------------------------------

    def _apply_pin(self, desired: Mapping, data: bytes) -> Dict[str, Any]:
        """Exactly the Device_Pin_API's pin operation — no second code
        path, so validation, atomic replacement, and enumeration are
        identical to a device-initiated pin (Requirement 3.1)."""
        store = self._store_factory()
        return store.pin_bytes(data, str(desired.get("fileName") or ""))

    def _apply_remove(self) -> None:
        """Exactly the Device_Pin_API's unpin; an already-unpinned store
        converges to "no Pinned_Image" as a successful no-op
        (Requirement 7.4)."""
        store = self._store_factory()
        try:
            store.unpin()
        except StaticImagePinError as exc:
            if _NO_IMAGE_PINNED_MARKER in str(exc):
                return None
            raise
        return None

    # --- idempotence marker (Requirements 3.5, 7.4) --------------------------

    def _marker_file(self) -> str:
        if self._marker_path is None:
            self._marker_path = default_marker_path()
        return self._marker_path

    def _read_marker(self) -> Optional[Dict[str, Any]]:
        """The recorded terminal outcome, or ``None``. A corrupt or
        unreadable marker is treated as no marker (re-applying is safe)."""
        path = self._marker_file()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                marker = json.load(handle)
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001 - corrupt marker == no marker
            logger.warning(
                "Static-image pin marker %s is unreadable (%s); treating "
                "it as absent",
                path,
                exc,
            )
            return None
        if not isinstance(marker, dict) or not marker.get("requestId"):
            logger.warning(
                "Static-image pin marker %s is malformed; treating it as "
                "absent",
                path,
            )
            return None
        return marker

    def _write_marker(self, marker: Mapping[str, Any]) -> None:
        """Atomic write (temp + ``os.replace``, the pin store's
        discipline). A failed marker write is logged, not fatal: a
        redelivery would re-execute, which is safe by construction."""
        path = self._marker_file()
        directory = os.path.dirname(path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as staging:
                    json.dump(marker, staging, indent=2)
                    staging.flush()
                    os.fsync(staging.fileno())
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise
        except Exception:  # noqa: BLE001 - marker write is best-effort
            logger.exception(
                "Could not persist the static-image pin idempotence marker"
            )

    # --- reported echo (Requirements 3.6, 7.7) --------------------------------

    def _build_report(
        self,
        desired: Mapping[str, Any],
        status: str,
        reason: Optional[str],
        metadata: Optional[Mapping[str, Any]],
        completed_ms: Optional[int],
    ) -> Dict[str, Any]:
        """Verbatim echo of every desired field plus the outcome fields —
        the echo equality is what silences the shadow delta (Decision 1)."""
        report: Dict[str, Any] = dict(desired)
        report["status"] = status
        if reason:
            report["reason"] = reason
        if metadata is not None:
            report["metadata"] = dict(metadata)
        if completed_ms is not None:
            report["completedAtEpochMs"] = int(completed_ms)
        return report

    def _write_reported(self, report: Mapping[str, Any]) -> None:
        """Merge-safe top-level shadow write: the pin section never
        clobbers ``reported.cameras``. A failed write is logged; shadow
        delta redelivery re-triggers the echo through the marker path."""
        payload = {"reported": {"staticImagePin": dict(report)}}
        try:
            self._shadow.update_thing_shadow_state_request(
                self.thing_name, self.shadow_name, payload
            )
        except Exception:  # noqa: BLE001 - offline echo retries via redelivery
            logger.exception(
                "Could not write the static-image pin confirmation to the "
                "camera-registry shadow"
            )

    def _notify_inventory(self) -> None:
        callback = self.report_inventory
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - report trigger isolation
            logger.exception(
                "Static-image pin inventory report trigger failed"
            )
