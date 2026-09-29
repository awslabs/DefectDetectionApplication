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
"""The Credential_Store (rtsp-rtmp-stream-cameras Requirement 6.2).

Stream_Credentials of the device's stream Image_Sources, keyed by
Image_Source id, in one JSON file:

- ``${COMPONENT_WORK_PATH}/stream_credentials/credentials.json``;
- the directory is mode 0700 and the file mode 0600, so only the owner
  can reach them;
- outside the SQLite databases and outside every path the LocalServer
  serves over HTTP, and excluded from the diagnostic snapshot
  (``snapshot/snapshot.sh``);
- written atomically: a temporary file in the same directory, flushed
  and fsynced, then ``os.replace`` — the precedent of
  ``camera_sync/version_state.py`` — so a crash never leaves a torn file.

Nothing here logs a credential: messages carry Image_Source ids only.
:meth:`CredentialStore.secret_values` feeds the log redaction filter
(``dda_logging/redaction.py``), which masks every stored value.
"""
import json
import logging
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: Directory and file names under ``COMPONENT_WORK_PATH``.
STORE_DIRECTORY_NAME = "stream_credentials"
STORE_FILE_NAME = "credentials.json"

#: Required modes (Requirement 6.2).
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600

#: The credential fields an entry may hold (model.stream_source).
ENTRY_FIELDS = ("username", "password", "urlSecret")

_FORMAT_VERSION = 1


class CredentialStoreError(Exception):
    """The Credential_Store could not be read or written. The message never
    contains a credential."""


def default_store_directory() -> str:
    """``${COMPONENT_WORK_PATH}/stream_credentials``."""
    return os.path.join(os.environ["COMPONENT_WORK_PATH"], STORE_DIRECTORY_NAME)


class CredentialStore:
    """Stream_Credentials by Image_Source id.

    ``get`` returns a copy of an entry's fields, ``put`` replaces an entry,
    ``delete`` removes one, and ``secret_values`` lists every stored value
    for redaction. All access is serialized by one lock, and the file is
    re-read only when it changed on disk, so the redaction filter's call
    on every log record stays cheap.
    """

    def __init__(self, directory: Optional[str] = None,
                 time_fn=time.time):
        self._directory = directory or default_store_directory()
        self._path = os.path.join(self._directory, STORE_FILE_NAME)
        self._time = time_fn
        self._lock = threading.RLock()
        self._entries: Optional[Dict[str, Dict[str, str]]] = None
        self._loaded_signature = None

    @property
    def path(self) -> str:
        return self._path

    # -- reading -----------------------------------------------------------

    def _signature(self):
        """What identifies the file's current content: None when there is
        no file, and a fixed marker per error when it cannot be examined,
        so an unreadable store is reported once rather than on every read
        (the redaction filter reads the store for every log record)."""
        try:
            stat = os.stat(self._path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as error:
            return ("unreadable", error.errno)
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def _load(self) -> Dict[str, Dict[str, str]]:
        """The entries, re-read when the file changed (caller holds lock)."""
        signature = self._signature()
        if self._entries is not None and signature == self._loaded_signature:
            return self._entries
        if signature is None:
            self._entries, self._loaded_signature = {}, None
            return self._entries
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as error:
            # A corrupt or unreadable store must not take the backend down;
            # every stream camera then connects without credentials and
            # reports authentication_failed, which is visible to operators.
            # The state is set before logging: the log redaction filter
            # reads this store, and must find it loaded rather than recurse.
            self._entries, self._loaded_signature = {}, signature
            logger.error("Credential_Store %s could not be read (%s); treating "
                         "it as empty", self._path, type(error).__name__)
            return self._entries
        entries = document.get("entries") if isinstance(document, dict) else None
        if not isinstance(entries, dict):
            entries = {}
        self._entries = {
            str(key): {field: value for field, value in entry.items()
                       if field in ENTRY_FIELDS and isinstance(value, str) and value}
            for key, entry in entries.items() if isinstance(entry, dict)
        }
        # Entries left without any value are no entries.
        self._entries = {key: entry for key, entry in self._entries.items() if entry}
        self._loaded_signature = signature
        return self._entries

    def get(self, image_source_id: str) -> Optional[Dict[str, str]]:
        """A copy of the entry of ``image_source_id``, or None."""
        with self._lock:
            entry = self._load().get(str(image_source_id))
            return dict(entry) if entry else None

    def configured(self, image_source_id: str) -> bool:
        """Whether ``image_source_id`` has stored credentials."""
        return self.get(image_source_id) is not None

    def secret_values(self) -> List[str]:
        """Every stored credential value, for the redaction filter."""
        with self._lock:
            return [value for entry in self._load().values()
                    for value in entry.values() if value]

    # -- writing -----------------------------------------------------------

    def put(self, image_source_id: str, credentials: Dict[str, str]) -> None:
        """Store the non-empty ``credentials`` fields of ``image_source_id``,
        replacing its entry. An entry without any value is a delete."""
        fields = {field: value for field, value in (credentials or {}).items()
                  if field in ENTRY_FIELDS and isinstance(value, str) and value}
        with self._lock:
            entries = dict(self._load())
            if fields:
                entries[str(image_source_id)] = fields
            else:
                entries.pop(str(image_source_id), None)
            self._write(entries)

    def delete(self, image_source_id: str) -> bool:
        """Remove the entry of ``image_source_id``; whether one existed."""
        with self._lock:
            entries = dict(self._load())
            if entries.pop(str(image_source_id), None) is None:
                return False
            self._write(entries)
            return True

    def _ensure_directory(self) -> None:
        os.makedirs(self._directory, mode=DIRECTORY_MODE, exist_ok=True)
        # makedirs honours the umask and leaves an existing directory as it
        # was, so the mode is enforced explicitly.
        os.chmod(self._directory, DIRECTORY_MODE)

    def _write(self, entries: Dict[str, Dict[str, str]]) -> None:
        """Atomically replace the file with ``entries`` (caller holds lock)."""
        document = {"version": _FORMAT_VERSION,
                    "updatedAtMs": int(self._time() * 1000),
                    "entries": entries}
        try:
            self._ensure_directory()
            descriptor, temp_path = tempfile.mkstemp(
                prefix=".credentials.", suffix=".tmp", dir=self._directory)
        except OSError as error:
            raise CredentialStoreError(
                f"The Credential_Store directory is not writable ({type(error).__name__})") from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                # mkstemp creates the file 0600 already; chmod keeps the
                # guarantee explicit even under an unusual umask.
                os.fchmod(handle.fileno(), FILE_MODE)
                json.dump(document, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
            os.chmod(self._path, FILE_MODE)
        except OSError as error:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise CredentialStoreError(
                f"The Credential_Store could not be written ({type(error).__name__})") from None
        self._entries = entries
        self._loaded_signature = self._signature()


_store: Optional[CredentialStore] = None
_store_lock = threading.Lock()


def get_credential_store() -> CredentialStore:
    """The process-wide Credential_Store."""
    global _store
    with _store_lock:
        if _store is None:
            _store = CredentialStore()
        return _store


def set_credential_store(store: Optional[CredentialStore]) -> None:
    """Replace the process-wide store (tests)."""
    global _store
    with _store_lock:
        _store = store
