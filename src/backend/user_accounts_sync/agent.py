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
"""Account_Sync_Service edge agent over the ``dda-user-accounts`` named
shadow (Requirements 7.1, 7.4, 7.8, 7.9).

Mirrors ``camera_sync/agent.py``: started from ``server_setup.py`` on a
daemon thread (wiring is task 10.3); on start it ``GetThingShadow``s the
``dda-user-accounts`` named shadow (thing name from ``AWS_IOT_THING_NAME``)
to catch up on desired state that arrived while the device was offline,
then subscribes to
``$aws/things/{thing}/shadow/name/dda-user-accounts/update/delta`` through
the existing MQTT ``SubscriptionHandler`` pattern (see
:func:`make_shadow_stream_handler`).

On receiving a desired sync document the agent:

1. Validates the document shape with the pure function
   :func:`parse_sync_document`. Deleted accounts are normalized to
   ``enabled: false`` and are never dropped (Requirement 7.8).
2. Atomically replaces the Local_Credential_Cache file at
   :data:`DEFAULT_CACHE_PATH` (temp file mode ``0600``, then
   ``os.replace``) — full-set replacement makes application idempotent
   (Requirement 7.1).
3. Writes ``reported: {ackSyncId, appliedAt, accountCount}``; on
   validation failure it writes ``reported: {ackSyncId, error: reason}``
   with the existing cache untouched, so the portal records the failure
   rather than timing out (Requirements 7.4, 7.9).

All shadow I/O goes through the existing ``IoTShadowAccessor`` (Greengrass
IPC — the device's own AWS IoT identity and policies). Agent crashes are
logged and never take down the rest of LocalServer; the shadow retains
``desired`` while the device is offline, and the startup ``GetThingShadow``
is the catch-up path.
"""
import json
import logging
import os
import tempfile
import time
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: The named shadow carrying account sync state (design decision D2).
SHADOW_NAME = "dda-user-accounts"

#: The sync document schema version this agent understands.
DOCUMENT_VERSION = 1

#: The Local_Credential_Cache file schema version.
CACHE_VERSION = 1

#: The Local_Credential_Cache file the ``local_auth`` subsystem reads.
DEFAULT_CACHE_PATH = "/aws_dda/local_credential_cache.json"

#: Cache file permissions: owner read/write only.
CACHE_FILE_MODE = 0o600

#: Required verifier keys and the types they must carry.
_VERIFIER_SHAPE = (
    ("algorithm", str),
    ("iterations", int),
    ("salt", str),
    ("hash", str),
)


def delta_topic_prefix(thing_name: str, shadow_name: str = SHADOW_NAME) -> str:
    """The shadow update topic prefix the MQTT ``SubscriptionHandler``
    subscribes to (with its ``#`` wildcard); the ``delta`` subtopic carries
    portal-originated desired sync documents."""
    return "$aws/things/{}/shadow/name/{}/update/".format(thing_name, shadow_name)


# --- sync document validation (pure) -------------------------------------------


class SyncDocumentError(ValueError):
    """A desired sync document failed shape validation. The message is the
    reason written to ``reported.error`` (Requirement 7.4)."""


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _validate_verifier(username: str, verifier: Any) -> None:
    if not isinstance(verifier, Mapping):
        raise SyncDocumentError(
            "account '{}': 'verifier' must be an object".format(username)
        )
    for key, expected_type in _VERIFIER_SHAPE:
        value = verifier.get(key)
        if expected_type is int:
            if _is_bool(value) or not isinstance(value, int) or value <= 0:
                raise SyncDocumentError(
                    "account '{}': verifier '{}' must be a positive "
                    "integer".format(username, key)
                )
        elif not _is_str(value):
            raise SyncDocumentError(
                "account '{}': verifier '{}' must be a non-empty "
                "string".format(username, key)
            )


def _validate_account(username: Any, account: Any) -> Dict[str, Any]:
    if not _is_str(username):
        raise SyncDocumentError("account usernames must be non-empty strings")
    if not isinstance(account, Mapping):
        raise SyncDocumentError(
            "account '{}' must be an object".format(username)
        )
    if not isinstance(account.get("email"), str):
        raise SyncDocumentError(
            "account '{}': 'email' must be a string".format(username)
        )
    if not _is_str(account.get("role")):
        raise SyncDocumentError(
            "account '{}': 'role' must be a non-empty string".format(username)
        )
    if not _is_bool(account.get("enabled")):
        raise SyncDocumentError(
            "account '{}': 'enabled' must be a boolean".format(username)
        )
    if "deleted" in account and not _is_bool(account["deleted"]):
        raise SyncDocumentError(
            "account '{}': 'deleted' must be a boolean".format(username)
        )
    if "verifier" in account and account["verifier"] is not None:
        _validate_verifier(username, account["verifier"])

    # Full record preservation (Requirement 7.8 / Property 12): every
    # attribute travels into the cache; a deleted account is normalized to
    # enabled=false — marked disabled, never dropped.
    record = {key: _deep_copy_json(value) for key, value in account.items()}
    if record.get("deleted"):
        record["enabled"] = False
    return record


def _deep_copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _deep_copy_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_copy_json(v) for v in value]
    return value


def parse_sync_document(document: Any) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    """Pure validation of a desired sync document (design section 4).

    Expected shape::

        {"syncId": "uuid", "version": 1,
         "accounts": {username: {email, role, enabled, deleted?, verifier?}}}

    Returns ``(sync_id, accounts)`` where ``accounts`` is a deep copy with
    every deleted account normalized to ``enabled: false`` (never dropped,
    Requirement 7.8). Raises :class:`SyncDocumentError` with the reason on
    any shape violation — the caller reports the reason and leaves the
    existing cache untouched.
    """
    if not isinstance(document, Mapping):
        raise SyncDocumentError("sync document must be an object")
    sync_id = document.get("syncId")
    if not _is_str(sync_id):
        raise SyncDocumentError("'syncId' must be a non-empty string")
    version = document.get("version")
    if _is_bool(version) or version != DOCUMENT_VERSION:
        raise SyncDocumentError(
            "unsupported sync document version {!r} (expected {})".format(
                version, DOCUMENT_VERSION
            )
        )
    accounts = document.get("accounts")
    if not isinstance(accounts, Mapping):
        raise SyncDocumentError("'accounts' must be an object")
    parsed = {
        str(username): _validate_account(username, account)
        for username, account in accounts.items()
    }
    return sync_id, parsed


# --- cache replacement (pure builder + atomic write) ----------------------------


def build_cache_document(
    sync_id: str,
    accounts: Mapping[str, Mapping[str, Any]],
    applied_at_ms: int,
) -> Dict[str, Any]:
    """Pure builder of the Local_Credential_Cache file contents (design
    data model: ``/aws_dda/local_credential_cache.json``)."""
    return {
        "version": CACHE_VERSION,
        "syncId": sync_id,
        "appliedAt": int(applied_at_ms),
        "accounts": {k: dict(v) for k, v in accounts.items()},
    }


def write_cache_atomically(
    document: Mapping[str, Any], path: str = DEFAULT_CACHE_PATH
) -> None:
    """Atomically replace the credential cache file.

    The document is written to a temp file in the target directory with
    mode ``0600``, fsynced, then moved over the cache path with
    ``os.replace`` — readers see either the old complete file or the new
    complete file, never a partial write. Any failure leaves the existing
    cache untouched.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=directory, prefix=".{}.".format(os.path.basename(path)), suffix=".tmp"
    )
    try:
        os.fchmod(fd, CACHE_FILE_MODE)
        with os.fdopen(fd, "w") as handle:
            json.dump(document, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


# --- the agent -----------------------------------------------------------------


class UserAccountsSyncAgent:
    """Applies portal-staged account sets from the ``dda-user-accounts``
    named shadow to the Local_Credential_Cache and acks through the
    shadow's reported state (Requirements 7.1, 7.4, 7.8, 7.9).

    ``iot_shadow_accessor`` is the existing ``IoTShadowAccessor`` (or a
    fake exposing ``get_thing_shadow_state_request`` /
    ``update_thing_shadow_state_request``). ``wall_clock`` is injectable
    so tests pin ``appliedAt`` deterministically.
    """

    def __init__(
        self,
        iot_shadow_accessor,
        thing_name: Optional[str] = None,
        shadow_name: str = SHADOW_NAME,
        cache_path: str = DEFAULT_CACHE_PATH,
        wall_clock: Callable[[], float] = time.time,
    ):
        self._shadow = iot_shadow_accessor
        self.thing_name = (
            thing_name
            if thing_name is not None
            else os.environ.get("AWS_IOT_THING_NAME", "")
        )
        self.shadow_name = shadow_name
        self.cache_path = cache_path
        self._wall_clock = wall_clock

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Startup catch-up: read the shadow and apply any desired sync
        document that arrived while the device was offline. Never raises —
        an offline or crashing start must not take down LocalServer."""
        try:
            self._catch_up()
        except Exception:  # noqa: BLE001 - crash isolation
            logger.exception("User-accounts sync catch-up failed")

    def _catch_up(self) -> None:
        try:
            state = self._shadow.get_thing_shadow_state_request(
                self.thing_name, self.shadow_name
            )
        except Exception:  # noqa: BLE001 - offline start must not crash
            logger.exception("Could not read the user-accounts shadow at start")
            return
        if not isinstance(state, Mapping):
            # False (shadow does not exist yet) or None (transport error):
            # nothing staged for this device.
            return
        desired = state.get("desired")
        if not isinstance(desired, Mapping) or not desired:
            return
        reported = state.get("reported")
        already_acked = (
            isinstance(reported, Mapping)
            and _is_str(desired.get("syncId"))
            and reported.get("ackSyncId") == desired.get("syncId")
        )
        if already_acked and self._cache_sync_id() == desired.get("syncId"):
            logger.info(
                "User-accounts shadow desired state %s already applied",
                desired.get("syncId"),
            )
            return
        self.apply_sync_document(desired)

    # --- delta path ------------------------------------------------------

    def on_delta(self, message: Mapping[str, Any]) -> None:
        """A portal-originated desired sync document (shadow delta). The
        delta payload carries ``{"state": {...sync document...}}``; a bare
        document is tolerated for the startup catch-up path."""
        logger.info("Received user-accounts shadow delta")
        state = message.get("state") if isinstance(message, Mapping) else None
        document = state if isinstance(state, Mapping) else message
        self.apply_sync_document(document)

    # --- apply path (Requirements 7.1, 7.4, 7.8) --------------------------

    def apply_sync_document(self, document: Any) -> None:
        """Validate, atomically replace the credential cache, and ack.
        Validation or write failure leaves the existing cache untouched
        and reports the error. Never raises."""
        try:
            self._apply(document)
        except Exception:  # noqa: BLE001 - crash isolation
            logger.exception("Applying a user-accounts sync document failed")

    def _apply(self, document: Any) -> None:
        try:
            sync_id, accounts = parse_sync_document(document)
        except SyncDocumentError as err:
            reason = str(err)
            logger.warning(
                "Rejected user-accounts sync document: %s; the existing "
                "credential cache is untouched",
                reason,
            )
            self._report_error(_extract_sync_id(document), reason)
            return

        applied_at = int(self._wall_clock() * 1000)
        try:
            write_cache_atomically(
                build_cache_document(sync_id, accounts, applied_at),
                self.cache_path,
            )
        except Exception as err:  # noqa: BLE001 - report, keep old cache
            logger.exception("Could not replace the local credential cache")
            self._report_error(
                sync_id, "could not write the credential cache: {}".format(err)
            )
            return

        logger.info(
            "Applied user-accounts sync %s (%d accounts)", sync_id, len(accounts)
        )
        self._report({
            "ackSyncId": sync_id,
            "appliedAt": applied_at,
            "accountCount": len(accounts),
            "error": None,  # clear any previous failure ack
        })

    # --- reported acks (Requirements 7.4, 7.9) -----------------------------

    def _report_error(self, sync_id: Optional[str], reason: str) -> None:
        ack: Dict[str, Any] = {
            "error": reason,
            # clear any previous success ack fields
            "appliedAt": None,
            "accountCount": None,
        }
        if sync_id is not None:
            ack["ackSyncId"] = sync_id
        self._report(ack)

    def _report(self, reported: Dict[str, Any]) -> None:
        """Write the reported ack. A failed write is logged and dropped:
        the portal's 60-second timeout records the sync as device
        unreachable and its 5-minute schedule retries (7.9 is handled
        portal-side; the edge just acks promptly)."""
        try:
            self._shadow.update_thing_shadow_state_request(
                self.thing_name, self.shadow_name, {"reported": reported}
            )
        except Exception:  # noqa: BLE001 - portal timeout handles lost acks
            logger.exception("Could not write the user-accounts sync ack")

    # --- helpers -----------------------------------------------------------

    def _cache_sync_id(self) -> Optional[str]:
        """The syncId of the currently applied cache file, or ``None`` when
        the cache is missing or unreadable (forces a re-apply)."""
        try:
            with open(self.cache_path, "r") as handle:
                cache = json.load(handle)
        except (OSError, ValueError):
            return None
        if isinstance(cache, Mapping) and _is_str(cache.get("syncId")):
            return cache["syncId"]
        return None


def _extract_sync_id(document: Any) -> Optional[str]:
    """Best-effort ``syncId`` extraction from an invalid document so the
    error ack can still be correlated by the portal."""
    if isinstance(document, Mapping) and _is_str(document.get("syncId")):
        return document["syncId"]
    return None


# --- delta subscription (SubscriptionHandler pattern) --------------------------


def make_shadow_stream_handler(agent: UserAccountsSyncAgent):
    """A ``SubscribeToIoTCoreStreamHandler`` dispatching the agent's shadow
    topics, following the existing ``camera_sync`` / ``SubscriptionHandler``
    pattern: pass this handler and :func:`delta_topic_prefix` to an
    ``mqtt.SubscriptionHandler`` when wiring the agent (task 10.3).

    The awsiot import is deferred so this module stays importable without
    the Greengrass IPC runtime (tests use fakes).
    """
    import awsiot.greengrasscoreipc.client as client

    from dao.iotshadow.ShadowUtils import decode_shadow_payload, remove_prefix

    prefix = delta_topic_prefix(agent.thing_name, agent.shadow_name)

    class _UserAccountsShadowHandler(client.SubscribeToIoTCoreStreamHandler):
        def on_stream_event(self, event) -> None:
            try:
                topic_name = event.message.topic_name
                subtopic = remove_prefix(topic_name, prefix)
                if subtopic == "delta":
                    message = decode_shadow_payload(event.message.payload)
                    agent.on_delta(message)
                elif subtopic == "rejected":
                    message = decode_shadow_payload(event.message.payload)
                    logger.warning(
                        "User-accounts shadow update rejected: %s", message
                    )
                # accepted/documents notifications need no edge-side action
            except Exception:  # noqa: BLE001 - handler isolation
                logger.exception("Error handling user-accounts shadow message")

        def on_stream_error(self, error: Exception) -> bool:
            logger.error("User-accounts shadow stream error: %s", error)
            return True  # close the stream; the wiring layer resubscribes

        def on_stream_closed(self) -> None:
            logger.info("User-accounts shadow stream closed")

    return _UserAccountsShadowHandler()
