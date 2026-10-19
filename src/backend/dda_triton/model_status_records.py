#  Copyright 2026 Amazon Web Services, Inc.
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
"""Persisted model status records under ``/aws_dda/dda_triton/model_status``
(triton-model-cleanup-wedge, design D5 and D8).

* A load outcome record, ``<component>.json``, is written by a model
  component's Startup that could not confirm READY, so the outcome is not
  hidden behind a Startup that exits 0. The Startup clears it on READY, the
  cleanup after a removal, and the backend's watchdog once it sees the model
  READY: a record always means "not READY since ``recordedAt``".
* ``self_heal.json`` and the kill switch ``self-heal.disabled`` belong to
  the backend's watchdog; only their paths live here.

Writes are atomic (``mkstemp`` + ``fsync`` + ``os.replace``) and never
raise; reads tolerate anything and skip what they cannot use.

Host-shared: seeded to ``/aws_dda/dda_triton/`` and run by the host's
system ``python3`` (3.8 on JP5). Standard library only.
"""
import datetime
import json
import logging
import os
import re
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

MODEL_STATUS_DIR = "/aws_dda/dda_triton/model_status"
SELF_HEAL_RECORD = "self_heal.json"
SELF_HEAL_DISABLED_FILE = "self-heal.disabled"

SCHEMA = 1
DIR_MODE = 0o755
FILE_MODE = 0o644

#: Load outcome records: their file names, their size limit on read, and the
#: cap on their two free-text fields on write (design-review finding 9), so
#: a long ``initialize()`` traceback cannot push a record over the reader's
#: limit and hide the outcome the record exists to surface.
LOAD_OUTCOME_FILE_PATTERN = re.compile(r"^model-[A-Za-z0-9._-]+\.json$")
LOAD_OUTCOME_MAX_BYTES = 16 * 1024
LOAD_OUTCOME_TEXT_MAX = 2000
LOAD_OUTCOME_SOURCE = "model_component_startup"


def _dir(records_dir: Optional[str]) -> str:
    return records_dir or MODEL_STATUS_DIR


def utc_iso(epoch: Optional[float] = None) -> str:
    """``2026-10-14T14:52:17Z``."""
    moment = (datetime.datetime.utcnow() if epoch is None
              else datetime.datetime.utcfromtimestamp(float(epoch)))
    return moment.replace(microsecond=0).isoformat() + "Z"


def serialize(document: Any) -> bytes:
    """The bytes :func:`write_json_atomic` writes for ``document``."""
    return json.dumps(document, sort_keys=True, indent=2).encode("utf-8")


def write_json_atomic(path: str, document: Any) -> bool:
    """Write ``document`` to ``path`` atomically. False on any error."""
    tmp = None
    try:
        directory = os.path.dirname(path) or "."
        if not os.path.isdir(directory):
            os.makedirs(directory, mode=DIR_MODE, exist_ok=True)
            try:
                os.chmod(directory, DIR_MODE)  # makedirs applies the umask
            except OSError:
                pass
        data = serialize(document)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
        tmp = None
        return True
    except Exception as exc:  # noqa: BLE001 - the caller logs the ERROR
        logger.debug("Could not write %s: %s", path, exc)
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def read_json(path: str, max_bytes: Optional[int] = None) -> Optional[Any]:
    """The parsed JSON document at ``path``, or None (missing, too large,
    unreadable or malformed)."""
    try:
        if max_bytes is not None and os.path.getsize(path) > max_bytes:
            logger.debug("Ignoring %s: larger than %d bytes", path, max_bytes)
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return None


def load_outcome_path(model: str, records_dir: Optional[str] = None) -> str:
    return os.path.join(_dir(records_dir), model + ".json")


def _cap(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    text = str(text)
    return text[:LOAD_OUTCOME_TEXT_MAX]


def write_load_outcome(model: str, *, state: Optional[str], reason: str,
                       failure_reason: Optional[str] = None,
                       backend_reachable: bool = True, attempts: int = 0,
                       component_version: Optional[str] = None,
                       startup_started_at: Optional[str] = None,
                       pb_stub_missing_file: Optional[Dict[str, Any]] = None,
                       records_dir: Optional[str] = None) -> bool:
    """Write ``model``'s load outcome record. False when it cannot be
    written (the caller logs the ERROR)."""
    if not LOAD_OUTCOME_FILE_PATTERN.match(model + ".json"):
        logger.debug("Not recording a load outcome for invalid model name %r", model)
        return False
    record = {
        "schema": SCHEMA,
        "model": model,
        "componentVersion": component_version,
        "outcome": "not_ready",
        "state": state,
        "reason": _cap(reason),
        "failureReason": _cap(failure_reason),
        "backendReachable": bool(backend_reachable),
        "attempts": int(attempts),
        "startupStartedAt": startup_started_at,
        "recordedAt": utc_iso(),
        "pbStubMissingFile": pb_stub_missing_file,
        "source": LOAD_OUTCOME_SOURCE,
    }
    # 2,000 characters keep an ASCII record far below the reader's limit;
    # text that JSON escapes (control or non-ASCII characters) can take up
    # to 12 bytes a character, so shorten further until the record fits.
    while len(serialize(record)) > LOAD_OUTCOME_MAX_BYTES:
        texts = [k for k in ("reason", "failureReason") if record.get(k)]
        if not texts:
            break
        for key in texts:
            record[key] = record[key][:len(record[key]) // 2]
    return write_json_atomic(load_outcome_path(model, records_dir), record)


def _valid_load_outcome(name: str, record: Any) -> bool:
    return (isinstance(record, dict) and record.get("schema") == SCHEMA
            and record.get("outcome") == "not_ready"
            and record.get("model") == name[:-len(".json")])


def read_load_outcomes(records_dir: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """``{component: record}`` for every valid load outcome record."""
    out: Dict[str, Dict[str, Any]] = {}
    directory = _dir(records_dir)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return out
    for name in names:
        if not LOAD_OUTCOME_FILE_PATTERN.match(name):
            continue
        record = read_json(os.path.join(directory, name), max_bytes=LOAD_OUTCOME_MAX_BYTES)
        if not _valid_load_outcome(name, record):
            logger.debug("Ignoring load outcome file %s", name)
            continue
        out[record["model"]] = record
    return out


def clear_load_outcome(model: str, records_dir: Optional[str] = None) -> bool:
    """Remove ``model``'s record. True when there is none afterwards."""
    if not LOAD_OUTCOME_FILE_PATTERN.match(model + ".json"):
        return True
    try:
        os.unlink(load_outcome_path(model, records_dir))
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("Could not remove the load outcome record of %s: %s", model, exc)
        return False
    return True


def unlisted_recorded_models(listed: Iterable[str], repo_dir: str,
                             records_dir: Optional[str] = None
                             ) -> List[Tuple[str, Dict[str, Any]]]:
    """``[(model, record)]``, sorted by model, for every load outcome record
    whose model is not among ``listed`` and whose ``<repo_dir>/<model>``
    directory exists (design D8: models that edgemlsdk does not list)."""
    names = set(listed or ())
    out: List[Tuple[str, Dict[str, Any]]] = []
    for model, record in sorted(read_load_outcomes(records_dir).items()):
        if model in names:
            continue
        if os.path.isdir(os.path.join(repo_dir, model)):
            out.append((model, record))
    return out
