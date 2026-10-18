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
"""A model component's Shutdown: remove the model's Triton repository entries.

Run by the model component recipe as
``python3 /aws_dda/convert_model_cleanup.py --model_name <component>``.

The removal never lands under a load of the model that the backend tracks
(triton-model-cleanup-wedge, design D2): it takes the model's cross-process
lock exclusive (``dda_triton.model_locks``), which every tracked load holds
shared until it ends, waiting at most :data:`RELEASE_WAIT_S`. Under the lock
it re-reads the model's state, unloads a READY model, and renames the
entries to trash names; it deletes them after releasing the lock. A load
still in flight at the bound makes it skip the delete with a WARNING and
leave the entries in place. Only argument errors exit 1.
"""
import argparse
import logging
import os
import re
import shutil
import sys
import time

import requests

from dda_triton.constants import TRITON_MODEL_DIR
from dda_triton.model_autostart_utils import wait_for_server

# The backend re-seeds these helpers with a non-atomic copy on every start, so
# a half-written file can raise SyntaxError as well as ImportError. None of
# them may fail the Shutdown (design D2, "Imports").
_IMPORT_ERRORS = {}
try:
    from dda_triton import model_locks
except Exception as _exc:  # noqa: BLE001
    model_locks = None
    _IMPORT_ERRORS["model_locks"] = _exc
try:
    from dda_triton import repo_entries
except Exception as _exc:  # noqa: BLE001
    repo_entries = None
    _IMPORT_ERRORS["repo_entries"] = _exc
try:
    from dda_triton import model_status_records
except Exception as _exc:  # noqa: BLE001
    model_status_records = None
    _IMPORT_ERRORS["model_status_records"] = _exc

parser = argparse.ArgumentParser(description=" Script converts model to Triton format")
parser.add_argument("--model_name", help="Model name")
logging.basicConfig(
    format="%(levelname)s:%(message)s",
    level=logging.DEBUG,
    handlers=[logging.StreamHandler(sys.stdout)],
)

RELEASE_WAIT_S = 300
UNLOAD_WAIT_S = 60
HTTP_POLL_S = 3
LOCK_POLL_S = 1
PROGRESS_LOG_EVERY_S = 30
HTTP_TIMEOUT_S = 10

BACKEND_HOST = "localhost"
BACKEND_PORT = 5000

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_IN_FLIGHT = ("LOADING", "UNLOADING")

_IMPORT_CONSEQUENCE = {
    "model_locks": "falling back to the backend's reported state",
    "repo_entries": "skipping the hidden-directory sweep",
    "model_status_records": "not clearing the load outcome record",
}


def valid_model_name(name) -> bool:
    return bool(isinstance(name, str) and _NAME_PATTERN.match(name) and name not in (".", ".."))


def _warn_import(module: str, warned: set) -> None:
    if module in warned or module not in _IMPORT_ERRORS:
        return
    warned.add(module)
    logging.warning(
        f"Could not import dda_triton.{module} ({_IMPORT_ERRORS[module]}); "
        f"{_IMPORT_CONSEQUENCE[module]}"
    )


def _trash_name(directory):
    if repo_entries is not None:
        return repo_entries.trash_name(directory)
    # Unstamped: the sweep ages it by mtime (D4).
    return ".trash-{0}-{1}-{2}".format(directory, os.getpid(), time.time_ns())


def stop_model(model_name, http=None, host=BACKEND_HOST, port=BACKEND_PORT):
    """Ask the backend to unload ``model_name``. True on HTTP 200."""
    http = http or requests
    url = f"http://{host}:{port}/feature-configurations/models/{model_name}/stop"
    headers = {"accept": "application/json"}
    try:
        response = http.get(url, headers=headers, timeout=HTTP_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        logging.warning(f"StopModel: request failed ({e}); removing the entries anyway")
        return False
    if response.status_code == 200:
        logging.info("Model stopped successfully!")
        return True
    logging.warning(
        f"StopModel: Request failed with status code: {response.status_code}: "
        f"{str(response.text)[:200]}"
    )
    return False


def read_state(model_name, http=None, host=BACKEND_HOST, port=BACKEND_PORT):
    """The backend's reported state of ``model_name``: ``UNKNOWN`` when the
    backend does not list it, None when the read fails."""
    http = http or requests
    url = f"http://{host}:{port}/feature-configurations"
    try:
        response = http.get(url, headers={"accept": "application/json"}, timeout=HTTP_TIMEOUT_S)
        if response.status_code != 200:
            logging.debug(f"Release {model_name}: state read answered {response.status_code}")
            return None
        for entry in response.json() or []:
            if isinstance(entry, dict) and entry.get("modelName") == model_name:
                return str(entry.get("status") or "UNKNOWN").strip().upper()
        return "UNKNOWN"
    except Exception as e:  # noqa: BLE001
        logging.debug(f"Release {model_name}: state read failed: {e}")
        return None


def release_model(model_name, repo=TRITON_MODEL_DIR, http=None, clock=None, sleep=None,
                  server_waiter=None, lock_dir=None, records_dir=None,
                  host=BACKEND_HOST, port=BACKEND_PORT):
    """Remove ``model_name``'s repository entries unless a load of it is in
    flight. Returns ``"removed"`` or ``"skipped"``."""
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    server_waiter = server_waiter or wait_for_server
    warned = set()
    m = model_name

    def state_now():
        return read_state(m, http=http, host=host, port=port)

    reachable = bool(server_waiter(host, port, "StopModel"))
    started = clock()
    deadline = started + RELEASE_WAIT_S
    if lock_dir is None and model_locks is not None:
        lock_dir = model_locks.lock_dir_for(repo)

    if model_locks is None:
        _warn_import("model_locks", warned)
        return _release_without_lock(m, repo, reachable, deadline, clock, sleep, state_now,
                                     http, host, port, records_dir, warned)

    first = True
    waiting_logged = False
    last = None
    progress = {"at": started}

    def on_wait(_elapsed):
        now = clock()
        if now - progress["at"] >= PROGRESS_LOG_EVERY_S:
            progress["at"] = now
            logging.info(
                f"Release {m}: still waiting for the in-flight load "
                f"({int(now - started)}s of {RELEASE_WAIT_S}s)"
            )

    while True:
        state = state_now() if reachable else None
        if state is not None:
            last = state
        if first:
            first = False
            if reachable:
                logging.info(f"Release {m}: backend reachable, state {state or 'unknown'}")
            else:
                logging.info(f"Release {m}: backend not reachable; relying on the model lock")
        if state in _IN_FLIGHT and not waiting_logged:
            waiting_logged = True
            logging.info(
                f"Release {m}: a load is in flight (state {state}); waiting up to "
                f"{RELEASE_WAIT_S}s for it to finish before removing its repository entries"
            )
        try:
            handle = model_locks.acquire(
                m, exclusive=True, timeout_s=max(0.0, deadline - clock()),
                poll_s=LOCK_POLL_S, on_wait=on_wait, lock_dir=lock_dir,
                clock=clock, sleep=sleep)
        except model_locks.LockTimeout:
            _skip_in_flight(m, last)
            return "skipped"
        except model_locks.LockUnavailable as e:
            logging.warning(
                f"Release {m}: the model lock is unavailable ({e}); falling back to the "
                f"backend's reported state"
            )
            return _release_without_lock(m, repo, reachable, deadline, clock, sleep, state_now,
                                         http, host, port, records_dir, warned)
        trash = []
        try:
            # 4a: one fresh read under the lock, whatever the ladder said:
            # the backend may have come up meanwhile.
            state = state_now()
            if state is not None:
                last = state
            if state == "READY":
                # 4b
                _unload(m, clock, sleep, state_now, http, host, port)
            elif state in _IN_FLIGHT:
                # 4c: a load the backend does not track (it holds no lock).
                handle.release()
                if not waiting_logged:
                    waiting_logged = True
                    logging.info(
                        f"Release {m}: a load is in flight (state {state}); waiting up to "
                        f"{RELEASE_WAIT_S}s for it to finish before removing its repository "
                        f"entries"
                    )
                if clock() >= deadline:
                    _skip_in_flight(m, last)
                    return "skipped"
                sleep(HTTP_POLL_S)
                if clock() >= deadline:
                    _skip_in_flight(m, last)
                    return "skipped"
                continue
            # 4d
            trash = _rename_to_trash(m, repo, warned)
        finally:
            handle.release()
        _delete_trash(m, repo, trash)
        _finish(m, repo, records_dir, warned)
        return "removed"


def _skip_in_flight(m, state):
    logging.warning(
        f"Release {m}: skipped deleting the repository entries: a load of the model is still "
        f"in flight after {RELEASE_WAIT_S}s (state {state or 'unknown'}). The entries are left "
        f"in place."
    )


def _unload(m, clock, sleep, state_now, http, host, port):
    logging.info(f"Release {m}: load finished (READY); unloading before removing its "
                 f"repository entries")
    stop_model(m, http=http, host=host, port=port)
    unload_deadline = clock() + UNLOAD_WAIT_S
    state = state_now()
    while state in ("READY", "UNLOADING"):
        if clock() >= unload_deadline:
            logging.warning(
                f"Release {m}: the unload did not finish within {UNLOAD_WAIT_S}s (state "
                f"{state}); removing the entries anyway, no load is in flight"
            )
            return
        sleep(HTTP_POLL_S)
        state = state_now()
    logging.info(f"Release {m}: unloaded (state {state or 'unknown'})")


def _entry_names(m):
    return [f"base_{m}", f"marshal_{m}", m]


def _rename_to_trash(m, repo, warned):
    """Rename every existing entry directory to a trash name; returns
    ``[(original_path, trash_path)]``. A rename that fails for another
    reason than ENOENT deletes that directory in place (the lock is held)."""
    if repo_entries is None:
        _warn_import("repo_entries", warned)
    moved = []
    for name in _entry_names(m):
        original = os.path.join(repo, name)
        if not os.path.isdir(original) or os.path.islink(original):
            continue
        trash = os.path.join(repo, _trash_name(name))
        try:
            os.rename(original, trash)
            moved.append((original, trash))
        except FileNotFoundError:
            continue
        except OSError as e:
            logging.warning(f"Release {m}: could not rename {original} ({e}); deleting it in place")
            try:
                shutil.rmtree(original)
                logging.info(f"Cleaned directory: {original}")
            except FileNotFoundError:
                logging.info(f"Cleaned directory: {original}")
            except OSError as e2:
                logging.error(f"Release {m}: could not delete {original} ({e2})")
    return moved


def _delete_trash(m, repo, trash):
    for original, path in trash:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass  # a concurrent sweep already removed it
        except OSError as e:
            logging.error(
                f"Release {m}: could not delete {path} ({e}); a later sweep removes it"
            )
            continue
        logging.info(f"Cleaned directory: {original}")


def _finish(m, repo, records_dir, warned):
    if repo_entries is not None:
        for name in repo_entries.sweep_stale_hidden_dirs(repo):
            if name.startswith(".trash-"):
                logging.info(f"Release {m}: removed stale trash {name}")
            else:
                logging.info(f"Removed stale staging directory {name}")
    else:
        _warn_import("repo_entries", warned)
    if model_status_records is not None:
        model_status_records.clear_load_outcome(m, records_dir=records_dir)
    else:
        _warn_import("model_status_records", warned)
    logging.info("Directory cleanup finished")


def _release_without_lock(m, repo, reachable, deadline, clock, sleep, state_now, http, host,
                          port, records_dir, warned):
    """The lock file is unusable or model_locks cannot be imported: rely on
    the backend's reported state alone."""
    if not reachable:
        logging.warning(
            f"Release {m}: skipped deleting the repository entries: the model lock is "
            f"unavailable and the backend is not reachable, so no in-flight load can be "
            f"ruled out"
        )
        return "skipped"
    state = state_now()
    while state in _IN_FLIGHT:
        if clock() >= deadline:
            _skip_in_flight(m, state)
            return "skipped"
        sleep(HTTP_POLL_S)
        state = state_now()
    if state == "READY":
        _unload(m, clock, sleep, state_now, http, host, port)
    trash = _rename_to_trash(m, repo, warned)
    _delete_trash(m, repo, trash)
    _finish(m, repo, records_dir, warned)
    return "removed"


def main(argv=None):
    args = parser.parse_args(argv)
    if not args.model_name:
        logging.error(
            "Exception occurred while cleaning model dir: Args not provided to perform the cleanup."
        )
        return 1
    if not valid_model_name(args.model_name):
        logging.error(
            f"Exception occurred while cleaning model dir: invalid model name "
            f"{args.model_name!r}"
        )
        return 1
    try:
        release_model(args.model_name)
    except Exception as e:  # noqa: BLE001 - the Shutdown completes; see design D2
        logging.error(
            f"Release {args.model_name}: unexpected error ({e}); the repository entries are left "
            f"as they are and the Shutdown exits 0",
            exc_info=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
