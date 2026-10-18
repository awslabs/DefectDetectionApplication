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

import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import hashlib
import json
from collections import namedtuple
import dda_triton.model_config_pb2 as triton_model_config
from dda_triton.constants import *
from google.protobuf import text_format
import argparse
import sys
import requests
from dda_triton.model_autostart_utils import wait_for_server

# Optional host-shared helpers (triton-model-cleanup-wedge). The backend
# re-seeds them with a non-atomic copy on every start, so a half-written file
# can raise SyntaxError as well as ImportError; none of them may fail a
# lifecycle step (design D5, "Imports").
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
_IMPORT_CONSEQUENCE = {
    "model_locks": "publishing without the model lock",
    "repo_entries": "skipping the hidden-directory sweep",
    "model_status_records": "not recording the load outcome",
}
_IMPORT_WARNED = set()


def _warn_import(module):
    if module in _IMPORT_WARNED or module not in _IMPORT_ERRORS:
        return
    _IMPORT_WARNED.add(module)
    logging.warning(
        f"Could not import dda_triton.{module} ({_IMPORT_ERRORS[module]}); "
        f"{_IMPORT_CONSEQUENCE[module]}"
    )

parser = argparse.ArgumentParser(description=" Script converts model to Triton format")
parser.add_argument("--unarchived_model_path", help="Path where the model is unarchived")
parser.add_argument("--model_version", help="Model version")
parser.add_argument("--model_name", help="Model name")

logging.basicConfig(
    format="%(levelname)s:%(message)s",
    level=logging.DEBUG,
    handlers=[logging.StreamHandler(sys.stdout)],
)


# Load-queue hardening knobs. A model load is asynchronous: POST-ing /start
# enqueues the load and returns 200 immediately, so a bare 200 does NOT mean the
# model is usable. We verify the model actually reaches READY, and retry the
# whole start if it does not, so a transient hiccup no longer requires a manual
# LocalServer restart to recover. Values are intentionally generous because
# DLR/Neo base models can take tens of seconds to initialise on a Jetson.
START_MODEL_MAX_ATTEMPTS = 3
START_MODEL_READY_TIMEOUT_S = 120
START_MODEL_POLL_INTERVAL_S = 3
START_MODEL_BACKOFF_S = 5

#: How long a changed publish waits for the model's exclusive lock (design D9).
PUBLISH_LOCK_WAIT_S = 300
PUBLISH_LOCK_POLL_S = 1
PUBLISH_PROGRESS_LOG_EVERY_S = 30

#: pb_stub capture caps (design D5).
PB_STUB_MAX_CONTAINERS = 3
PB_STUB_SCAN_BYTES = 20 * 1024 * 1024
PB_STUB_PER_CONTAINER_S = 10
PB_STUB_TOTAL_S = 20
PB_STUB_LINE_MAX = 300
PB_STUB_MARKER = "Python model file not found in '"

#: A component name (design "Input Validation").
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")

#: What a Startup's start attempts ended with (design D5).
StartOutcome = namedtuple(
    "StartOutcome",
    ["ready", "state", "reason", "failure_reason", "attempts", "elapsed_s", "backend_reachable"],
)


def _get_triton_model_state(model_name, host="localhost", port=5000, timeout=10):
    """Return the reported Triton state for ``model_name`` (or None if unknown).

    Reads the feature-configuration list and matches on ``modelName``. Tolerant
    of transient errors — returns None rather than raising so callers can retry.
    """
    try:
        url = f"http://{host}:{port}/feature-configurations"
        response = requests.get(url, headers={"accept": "application/json"}, timeout=timeout)
        if response.status_code != 200:
            return None
        for entry in response.json() or []:
            if isinstance(entry, dict) and entry.get("modelName") == model_name:
                return entry.get("status")
    except Exception as e:
        logging.debug(f"StartModel: unable to read model state for {model_name}: {e}")
    return None


def _wait_for_model_ready(
    model_name,
    host="localhost",
    port=5000,
    timeout=START_MODEL_READY_TIMEOUT_S,
    interval=START_MODEL_POLL_INTERVAL_S,
    observer=None,
):
    """Poll until ``model_name`` is READY (True) or a deadline passes (False).

    A terminal failure state (UNAVAILABLE / FAILED) short-circuits to False so
    the caller can retry rather than waiting out the full timeout.
    ``observer(state)``, when given, receives every state read.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _get_triton_model_state(model_name, host, port)
        if observer is not None:
            observer(state)
        if state == "READY":
            return True
        if state in ("UNAVAILABLE", "FAILED"):
            logging.error(f"StartModel: {model_name} entered terminal state {state}")
            return False
        time.sleep(interval)
    return False


def _final_model_entry(model_name, host="localhost", port=5000, timeout=10):
    """``(status, failureReason)`` of ``model_name`` from one read of
    ``/feature-configurations``; ``(None, None)`` on any error."""
    try:
        url = f"http://{host}:{port}/feature-configurations"
        response = requests.get(url, headers={"accept": "application/json"}, timeout=timeout)
        if response.status_code != 200:
            return None, None
        for entry in response.json() or []:
            if isinstance(entry, dict) and entry.get("modelName") == model_name:
                config = entry.get("defaultConfiguration")
                reason = config.get("failureReason") if isinstance(config, dict) else None
                return entry.get("status"), reason
    except Exception as e:
        logging.debug(f"StartModel: unable to read the final state of {model_name}: {e}")
    return None, None


def start_model_detailed(
    model_name,
    host="localhost",
    port=5000,
    max_attempts=START_MODEL_MAX_ATTEMPTS,
    ready_timeout=START_MODEL_READY_TIMEOUT_S,
    backoff=START_MODEL_BACKOFF_S,
):
    """Ask LocalServer to load ``model_name`` and verify it reaches READY.

    Hardened against the model-load race: issuing /start only enqueues an
    asynchronous load, so we confirm the model actually becomes READY and retry
    the whole start (bounded, with backoff) if it does not. Failures are
    contained — this never raises, so one model's failure to load cannot abort a
    sibling model component's start (each model component starts its own model
    independently). Returns a :data:`StartOutcome`; on not-ready it carries the
    last observed state and the reason the Startup records (design D5).
    """
    started = time.monotonic()
    observed = {"state": None}

    def observe(state):
        if state is not None:
            observed["state"] = state

    def outcome(ready, attempts, reachable, last_http=None):
        elapsed = max(0.0, time.monotonic() - started)
        if ready:
            return StartOutcome(True, "READY", None, None, attempts, elapsed, reachable)
        state, failure_reason = (None, None)
        if reachable:
            state, failure_reason = _final_model_entry(model_name, host, port)
        state = state or observed["state"]
        state = str(state).strip().upper() if state else None
        if not reachable:
            reason = "LocalServer backend was not reachable; no load was requested"
        elif state == "LOADING":
            reason = (
                f"still LOADING after {attempts} start attempts over {int(elapsed)} s; "
                f"the load continues in the backend"
            )
        elif state == "UNAVAILABLE":
            reason = "load failed (UNAVAILABLE): {0}".format(
                failure_reason or "no failure reason was reported")
        else:
            reason = (
                f"the load request was not accepted (last state {state or 'unknown'}, "
                f"last start answer HTTP {last_http if last_http is not None else 'none'})"
            )
        return StartOutcome(False, state, reason, failure_reason, attempts, elapsed, reachable)

    if not wait_for_server(host, port, "StartModel"):
        logging.error(f"StartModel: {host}:{port} is not reachable; cannot start {model_name}")
        return outcome(False, 0, False)

    url = f"http://{host}:{port}/feature-configurations/models/{model_name}/start"
    headers = {"accept": "application/json"}
    last_http = None
    attempts = 0

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        # If the model is already READY (e.g. a prior attempt or a concurrent
        # trigger loaded it), we are done.
        current = _get_triton_model_state(model_name, host, port)
        observe(current)
        if current == "READY":
            logging.info(f"Model {model_name} already READY")
            return outcome(True, attempt, True)
        try:
            response = requests.get(url, headers=headers, timeout=30)
        except Exception as e:
            logging.error(f"StartModel: attempt {attempt}/{max_attempts} request error: {e}")
            time.sleep(backoff)
            continue
        last_http = response.status_code

        # 200 => load enqueued. 403 => model is not in a startable state, which
        # includes the case where it is already LOADING/READY; both mean "a load
        # is in flight", so fall through to the readiness wait either way.
        if response.status_code in (200, 403):
            if _wait_for_model_ready(
                model_name, host, port, timeout=ready_timeout, observer=observe
            ):
                logging.info(f"Model {model_name} started successfully and is READY")
                return outcome(True, attempt, True)
            logging.error(
                f"StartModel: {model_name} not READY after attempt {attempt}/{max_attempts}"
            )
        else:
            logging.error(
                f"StartModel: attempt {attempt}/{max_attempts} failed with status "
                f"{response.status_code}: {response.text}"
            )
        if attempt < max_attempts:
            time.sleep(backoff)

    logging.error(
        f"StartModel: gave up starting {model_name} after {max_attempts} attempts; "
        f"model did not reach READY"
    )
    return outcome(False, attempts, True, last_http)


def start_model(
    model_name,
    host="localhost",
    port=5000,
    max_attempts=START_MODEL_MAX_ATTEMPTS,
    ready_timeout=START_MODEL_READY_TIMEOUT_S,
    backoff=START_MODEL_BACKOFF_S,
):
    """:func:`start_model_detailed`, reduced to True when the model is
    confirmed READY, else False. Never raises."""
    return start_model_detailed(
        model_name,
        host=host,
        port=port,
        max_attempts=max_attempts,
        ready_timeout=ready_timeout,
        backoff=backoff,
    ).ready


def clean_directory(path: str) -> bool:
    """
    Clean the directory by removing all files and subdirectories.

    Args:
        path (str): The path to the directory to be cleaned.

    Returns:
        bool: True if the directory is cleaned successfully, False otherwise.
    """
    try:
        shutil.rmtree(path)
        return True
    except OSError as e:
        logging.error(f"Unable to clean directory '{path}' error {str(e)}")
        return False


def create_sym_links(src_dir: str, dst_dir: str) -> bool:
    """
    Create symbolic links for all files in the source directory to the destination directory.

    Args:
        src_dir (str): The path to the source directory.
        dst_dir (str): The path to the destination directory.

    Returns:
        bool: True if the symbolic links are created successfully, False otherwise.
    """
    try:
        for file in os.listdir(src_dir):
            src_file_path = os.path.join(src_dir, file)
            dst_file_path = os.path.join(dst_dir, file)
            if os.path.isfile(src_file_path):
                os.symlink(src_file_path, dst_file_path)
            elif os.path.isdir(src_file_path):
                os.symlink(src_file_path, dst_file_path, target_is_directory=True)
        return True
    except OSError as e:
        logging.error(
            f"Unable to create symbolic links from '{src_dir}' to '{dst_dir}' error {str(e)}"
        )
        return False


def _staging_prefix(published_name: str) -> str:
    if repo_entries is not None:
        try:
            return repo_entries.staging_prefix(published_name)
        except Exception as e:  # noqa: BLE001 - fall back to the unstamped prefix
            logging.debug(f"Unable to stamp the staging prefix of '{published_name}': {e}")
    return f".staging-{published_name}-"


def _stage_model_dir(model_repo_dir: str, published_name: str, build_fn):
    """Build ``published_name`` into a hidden staging sibling.

    ``build_fn(staging_dir)`` fully populates the staging directory (which
    stands in for ``<model_repo_dir>/<published_name>``) and returns True on
    success. Returns the staging path, or None after removing the staging
    directory. The sibling lives in ``model_repo_dir``, so the later swap is an
    atomic rename on one filesystem. Its name carries the boot stamp
    (``dda_triton.repo_entries``), so a leftover is aged without the wall clock.
    """
    try:
        staging_path = tempfile.mkdtemp(prefix=_staging_prefix(published_name), dir=model_repo_dir)
    except OSError as e:
        logging.error(f"Unable to create staging directory for '{published_name}' error {str(e)}")
        return None
    try:
        if not build_fn(staging_path):
            clean_directory(staging_path)
            return None
    except Exception as e:
        logging.error(f"Exception while staging model '{published_name}': {str(e)}")
        clean_directory(staging_path)
        return None
    return staging_path


def _swap_model_dir(model_repo_dir: str, published_name: str, staging_path: str) -> bool:
    """Swap a staged directory into place: remove any existing published
    directory, then rename the staging directory atomically. On failure the
    staging directory is removed."""
    published_path = os.path.join(model_repo_dir, published_name)
    try:
        if os.path.exists(published_path) and os.path.isdir(published_path):
            if not clean_directory(published_path):
                logging.error(f"Unable to clean existing model dir '{published_path}'")
                clean_directory(staging_path)
                return False
        os.replace(staging_path, published_path)
    except OSError as e:
        logging.error(f"Unable to publish model dir '{published_path}' error {str(e)}")
        clean_directory(staging_path)
        return False
    return True


#: What the python stub writes into a published tree while it loads.
_RUNTIME_DIR_NAMES = ("__pycache__",)
_RUNTIME_FILE_NAMES = ("dda_active_providers.json",)
_RUNTIME_FILE_PATTERN = re.compile(r"^\.dda_active_providers\..*\.tmp$")


def _is_runtime_file(name: str) -> bool:
    return name in _RUNTIME_FILE_NAMES or bool(_RUNTIME_FILE_PATTERN.match(name))


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_manifest(path: str, ignore_runtime: bool = False) -> dict:
    """``{relative path: ("dir",) | ("file", sha256) | ("link", target)}`` of
    the tree under ``path``, without following symlinks. ``ignore_runtime``
    skips what the stub writes while it loads (``__pycache__``,
    ``dda_active_providers.json``, ``.dda_active_providers.*.tmp``)."""
    manifest = {}
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        rel_dir = os.path.relpath(dirpath, path)
        keep = []
        for name in sorted(dirnames):
            full = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if os.path.islink(full):
                manifest[rel] = ("link", os.readlink(full))
                continue
            if ignore_runtime and name in _RUNTIME_DIR_NAMES:
                continue
            manifest[rel] = ("dir",)
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if os.path.islink(full):
                manifest[rel] = ("link", os.readlink(full))
                continue
            if ignore_runtime and _is_runtime_file(name):
                continue
            manifest[rel] = ("file", _file_sha256(full))
    return manifest


def _unchanged(staging_path: str, published_path: str, model_name: str) -> bool:
    """True when the staged tree equals the published one (runtime-written
    files aside). A missing published directory, or a manifest that cannot be
    computed, counts as changed."""
    if not os.path.isdir(published_path) or os.path.islink(published_path):
        return False
    try:
        return _tree_manifest(staging_path) == _tree_manifest(published_path, ignore_runtime=True)
    except Exception as e:  # noqa: BLE001 - publish as today
        logging.warning(
            f"Publishing {model_name}: could not compare {published_path} with the new entry "
            f"({e}); publishing it"
        )
        return False


def _lock_key_or_name(published_name: str) -> str:
    if model_locks is not None:
        try:
            return model_locks.lock_key(published_name)
        except ValueError:
            return published_name
    for prefix in ("base_", "marshal_"):
        if published_name.startswith(prefix):
            return published_name[len(prefix):]
    return published_name


def _acquire_publish_lock(model_repo_dir: str, model_name: str):
    """The model's exclusive lock for the swaps, or None when the publish
    proceeds without it (design D9 step 5)."""
    if model_locks is None:
        _warn_import("model_locks")
        return None
    started = time.monotonic()
    progress = {"at": started}

    def on_wait(_elapsed):
        now = time.monotonic()
        if now - progress["at"] >= PUBLISH_PROGRESS_LOG_EVERY_S:
            progress["at"] = now
            logging.info(
                f"Publishing {model_name}: waiting for an in-flight load of the model to "
                f"finish ({int(now - started)}s of {PUBLISH_LOCK_WAIT_S}s)"
            )

    try:
        return model_locks.acquire(
            model_name,
            exclusive=True,
            timeout_s=PUBLISH_LOCK_WAIT_S,
            poll_s=PUBLISH_LOCK_POLL_S,
            on_wait=on_wait,
            lock_dir=model_locks.lock_dir_for(model_repo_dir),
        )
    except model_locks.LockTimeout:
        logging.warning(
            f"Publishing {model_name}: a load of the model is still in flight after "
            f"{PUBLISH_LOCK_WAIT_S}s; replacing the changed entries anyway so the new "
            f"artifacts land"
        )
    except (model_locks.LockUnavailable, ValueError) as e:
        logging.warning(
            f"Publishing {model_name}: the model lock is unavailable ({e}); replacing the "
            f"changed entries without it"
        )
    return None


def _publish_model_dirs(model_repo_dir: str, model_name: str, specs) -> bool:
    """Publish a model's directories (design D9).

    ``specs`` is a list of ``(published_name, build_fn, label)``. Every
    directory is staged first; an unchanged one is left untouched (same inode
    and mtime), and the changed ones are swapped in order under ONE exclusive
    hold of the model's lock, so no tracked load can start between the swaps.
    After a :data:`PUBLISH_LOCK_WAIT_S` wait for the lock they are swapped
    without it. Staging directories are removed on every exit path, including
    ``SystemExit`` from the SIGTERM handler.
    """
    owned = []
    try:
        # 1. Stale staging directories of these names (D4).
        if repo_entries is not None:
            names = [name for name, _build, _label in specs]
            for removed in repo_entries.sweep_stale_hidden_dirs(model_repo_dir, names=names):
                logging.info(f"Removed stale staging directory {removed}")
        else:
            _warn_import("repo_entries")
        # 2. Stage everything first; nothing published changes on a failure.
        staged = []
        for name, build_fn, label in specs:
            staging_path = _stage_model_dir(model_repo_dir, name, build_fn)
            if staging_path is None:
                if label:
                    logging.error(f"Unable to create {label} model structure")
                return False
            owned.append(staging_path)
            staged.append((name, staging_path, label))
        # 3-4. Leave unchanged entries alone.
        changed = []
        for name, staging_path, label in staged:
            published_path = os.path.join(model_repo_dir, name)
            if _unchanged(staging_path, published_path, model_name):
                logging.info(
                    f"Model {name} unchanged; leaving the published repository entry untouched"
                )
                clean_directory(staging_path)
                owned.remove(staging_path)
            else:
                logging.info(f"Model {name} changed; publishing the new entry")
                changed.append((name, staging_path, label))
        if not changed:
            return True
        # 5-7. One exclusive hold for every swap, released before start_model.
        handle = _acquire_publish_lock(model_repo_dir, model_name)
        try:
            for name, staging_path, label in changed:
                swapped = _swap_model_dir(model_repo_dir, name, staging_path)
                # Swapped into place, or removed by the failed swap.
                owned.remove(staging_path)
                if not swapped:
                    if label:
                        logging.error(f"Unable to create {label} model structure")
                    return False
        finally:
            if handle is not None:
                handle.release()
        return True
    finally:
        # 8. Any staging directory still owned, also on SystemExit.
        for staging_path in owned:
            if os.path.isdir(staging_path):
                clean_directory(staging_path)


def _atomic_publish_model_dir(model_repo_dir: str, published_name: str, build_fn) -> bool:
    """Build a model directory off to the side and atomically swap it into place.

    Triton recognizes a model as loadable from its ``config.pbtxt`` + version
    directory, so a directory that is created and populated in place is briefly
    observable in a half-built state (config present, ``model.py`` / artifact
    symlinks not yet copied). A concurrent Triton poll or ``/start`` that hits
    that window fails the Python-backend preinitialize ("model file not found")
    and can wedge the load queue.

    The single-directory form of :func:`_publish_model_dirs`: the directory is
    staged, left untouched when unchanged, and otherwise swapped in under the
    model's exclusive lock. On any failure the staging dir is removed and the
    previously published dir is left untouched.

    Args:
        model_repo_dir: The Triton model repository directory.
        published_name: The final model directory name (e.g. ``base_<model>``).
        build_fn: Callable taking the staging directory path; returns bool.

    Returns:
        bool: True if the model directory was published atomically, else False.
    """
    return _publish_model_dirs(
        model_repo_dir, _lock_key_or_name(published_name), [(published_name, build_fn, None)]
    )


def _create_base_model_config_pbtxt(model_name: str, input_shape: list) -> str:
    global triton_model_config
    base_model_input = triton_model_config.ModelInput()
    base_model_input.name = "input"
    base_model_input.data_type = triton_model_config.DataType.TYPE_UINT8
    base_model_input.dims.extend(input_shape)
    # Output information based on lfv model template backend file.
    base_model_output_anomaly = triton_model_config.ModelOutput()
    base_model_output_anomaly.name = "output"
    base_model_output_anomaly.data_type = triton_model_config.DataType.TYPE_UINT8
    base_model_output_anomaly.dims.extend([1])
    base_model_output_confidence = triton_model_config.ModelOutput()
    base_model_output_confidence.name = "output_confidence"
    base_model_output_confidence.data_type = triton_model_config.DataType.TYPE_FP32
    base_model_output_confidence.dims.extend([1])
    base_model_output_score = triton_model_config.ModelOutput()
    base_model_output_score.name = "output_score"
    base_model_output_score.data_type = triton_model_config.DataType.TYPE_FP32
    base_model_output_score.dims.extend([1])
    base_model_output_mask = triton_model_config.ModelOutput()
    base_model_output_mask.name = "mask"
    base_model_output_mask.data_type = triton_model_config.DataType.TYPE_UINT8
    # Mask shape is same as input shape.
    base_model_output_mask.dims.extend(input_shape)
    base_model_anomalies = triton_model_config.ModelOutput()
    base_model_anomalies.name = "anomalies"
    base_model_anomalies.data_type = triton_model_config.DataType.TYPE_UINT8
    base_model_anomalies.dims.extend([-1])
    # Create model config file to generate config.pbtxt
    base_model_config = triton_model_config.ModelConfig()
    base_model_config.name = f"base_{model_name}"
    base_model_config.backend = "python"
    base_model_config.max_batch_size = 0
    base_model_config.input.extend([base_model_input])
    base_model_config.output.extend(
        [
            base_model_output_anomaly,
            base_model_output_confidence,
            base_model_output_score,
            base_model_output_mask,
            base_model_anomalies,
        ]
    )
    return text_format.MessageToString(
        base_model_config, use_short_repeated_primitives=True, use_index_order=True
    )


# Used to see if model supports anomaly localization.
def _has_pixel_level_classes(manifest: dict) -> bool:
    graph_manifest = manifest.get(MODEL_GRAPH_MANIFEST_KEY, {})
    if not graph_manifest:
        return False
    pixel_level_classes = graph_manifest.get(PIXEL_LEVEL_CLASSES, {})
    if not pixel_level_classes:
        return False
    names = pixel_level_classes.get("names", [])
    return bool(len(names))


def _resolve_base_input_shape(manifest: dict) -> list:
    """Resolve the Triton "input" tensor dims shared by the base/marshal/ensemble
    configs.

    The GStreamer pipeline feeds the RAW source image (native resolution) to
    Triton with no videoscale (see pipeline_builder._add_pre_processing_plugins),
    so the emltriton element requires the incoming GstBuffer to exactly match the
    model's input tensor size. Two cases:

    * DLR/Neo models: the input size is baked into the compiled model, and
      anomaly-localization (pixel_level_classes) models declare a fixed
      [H, W, 3] input so the mask output can mirror it. Source images are already
      that size, so a fixed tensor is correct.
    * ONNX-runtime models: the Triton python model (lfv_model_template) resizes
      the incoming image internally to the network input (per manifest
      ``preprocessing.resize``) before running onnxruntime, and the returned mask
      is sized to the source image. Forcing a fixed [H, W, 3] input here makes
      emltriton reject any source image whose size differs from the manifest
      dataset dims ("Incoming GstBuffer is not the same size as the input
      tensor"). So ONNX models MUST use a dynamic [-1, -1, -1] input regardless
      of pixel_level_classes.
    """
    runtime = str(manifest.get("runtime", "dlr")).lower()
    if runtime == "onnx":
        return [-1, -1, -1]
    model_internal = manifest.get("dataset", {})
    if _has_pixel_level_classes(manifest):
        return [model_internal["image_height"], model_internal["image_width"], 3]
    return [-1, -1, -1]


def _create_marshal_model_config_pbtxt(model_name: str, input_shape: list) -> str:
    global triton_model_config
    marshal_model_input = triton_model_config.ModelInput()
    marshal_model_input.name = "input"
    marshal_model_input.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_input.dims.extend(input_shape)
    marshal_model_inference_output = triton_model_config.ModelInput()
    marshal_model_inference_output.name = "inference_output"
    marshal_model_inference_output.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_inference_output.dims.extend([1])
    marshal_model_inference_mask = triton_model_config.ModelInput()
    marshal_model_inference_mask.name = "inference_mask"
    marshal_model_inference_mask.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_inference_mask.dims.extend(input_shape)
    marshal_model_inference_score = triton_model_config.ModelInput()
    marshal_model_inference_score.name = "inference_score"
    marshal_model_inference_score.data_type = triton_model_config.DataType.TYPE_FP32
    marshal_model_inference_score.dims.extend([1])
    marshal_model_inference_confidence = triton_model_config.ModelInput()
    marshal_model_inference_confidence.name = "inference_confidence"
    marshal_model_inference_confidence.data_type = triton_model_config.DataType.TYPE_FP32
    marshal_model_inference_confidence.dims.extend([1])
    marshal_model_inference_anomalies = triton_model_config.ModelInput()
    marshal_model_inference_anomalies.name = "inference_anomalies"
    marshal_model_inference_anomalies.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_inference_anomalies.dims.extend([-1])
    marshal_model_metadata = triton_model_config.ModelInput()
    marshal_model_metadata.name = "metadata"
    marshal_model_metadata.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_metadata.dims.extend([-1])
    # outputs
    marshal_model_output = triton_model_config.ModelOutput()
    marshal_model_output.name = "output"
    marshal_model_output.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_output.dims.extend([-1])
    marshal_model_output_mask = triton_model_config.ModelOutput()
    marshal_model_output_mask.name = "mask"
    marshal_model_output_mask.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_output_mask.dims.extend([-1])
    marshal_model_output_overlay = triton_model_config.ModelOutput()
    marshal_model_output_overlay.name = "overlay"
    marshal_model_output_overlay.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_output_overlay.dims.extend([-1])
    marshal_model_output_anomalous = triton_model_config.ModelOutput()
    marshal_model_output_anomalous.name = "output_anomalous"
    marshal_model_output_anomalous.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_output_anomalous.dims.extend([1])
    marshal_model_output_confidence = triton_model_config.ModelOutput()
    marshal_model_output_confidence.name = "output_confidence"
    marshal_model_output_confidence.data_type = triton_model_config.DataType.TYPE_FP32
    marshal_model_output_confidence.dims.extend([1])
    marshal_model_config = triton_model_config.ModelConfig()
    marshal_model_config.name = f"marshal_{model_name}"
    marshal_model_config.backend = "python"
    marshal_model_config.max_batch_size = 0
    marshal_model_config.input.extend(
        [
            marshal_model_input,
            marshal_model_inference_output,
            marshal_model_inference_mask,
            marshal_model_inference_confidence,
            marshal_model_inference_score,
            marshal_model_inference_anomalies,
            marshal_model_metadata,
        ]
    )
    marshal_model_config.output.extend(
        [
            marshal_model_output,
            marshal_model_output_mask,
            marshal_model_output_overlay,
            marshal_model_output_anomalous,
            marshal_model_output_confidence,
        ]
    )
    return text_format.MessageToString(
        marshal_model_config, use_short_repeated_primitives=True, use_index_order=True
    )


def _create_ensemble_model_config_pbtxt(model_name: str, input_shape: list) -> str:
    global triton_model_config
    ensemble_model_input = triton_model_config.ModelInput()
    ensemble_model_input.name = "input"
    ensemble_model_input.data_type = triton_model_config.DataType.TYPE_UINT8
    ensemble_model_input.dims.extend(input_shape)
    ensemble_model_input_metadata = triton_model_config.ModelInput()
    # used in emltriton.
    ensemble_model_input_metadata.name = "METADATA"
    ensemble_model_input_metadata.data_type = triton_model_config.DataType.TYPE_UINT8
    ensemble_model_input_metadata.dims.extend([-1])

    ensemble_model_output = triton_model_config.ModelOutput()
    ensemble_model_output.name = "output_overlay"
    ensemble_model_output.data_type = triton_model_config.DataType.TYPE_UINT8
    ensemble_model_output.dims.extend([-1])
    ensemble_model_output_mask = triton_model_config.ModelOutput()
    ensemble_model_output_mask.name = "output_mask"
    ensemble_model_output_mask.data_type = triton_model_config.DataType.TYPE_UINT8
    ensemble_model_output_mask.dims.extend([-1])
    ensemble_model_output_capture = triton_model_config.ModelOutput()
    ensemble_model_output_capture.name = "output_capture"
    ensemble_model_output_capture.data_type = triton_model_config.DataType.TYPE_UINT8
    ensemble_model_output_capture.dims.extend([-1])
    ensemble_model_output_anomalous = triton_model_config.ModelOutput()
    ensemble_model_output_anomalous.name = "output_anomalous"
    ensemble_model_output_anomalous.data_type = triton_model_config.DataType.TYPE_UINT8
    ensemble_model_output_anomalous.dims.extend([1])
    ensemble_model_output_confidence = triton_model_config.ModelOutput()
    ensemble_model_output_confidence.name = "output_confidence"
    ensemble_model_output_confidence.data_type = triton_model_config.DataType.TYPE_FP32
    ensemble_model_output_confidence.dims.extend([1])
    ensemble_model_config = triton_model_config.ModelConfig()
    ensemble_model_config.name = model_name
    ensemble_model_config.platform = "ensemble"
    ensemble_model_config.max_batch_size = 0
    ensemble_model_config.input.extend([ensemble_model_input, ensemble_model_input_metadata])
    ensemble_model_config.output.extend(
        [
            ensemble_model_output_capture,
            ensemble_model_output_mask,
            ensemble_model_output,
            ensemble_model_output_anomalous,
            ensemble_model_output_confidence,
        ]
    )
    # steps
    ensemble_model_step_1 = triton_model_config.ModelEnsembling.Step()
    ensemble_model_step_1.model_name = f"base_{model_name}"
    # -1 indicates latest
    ensemble_model_step_1.model_version = -1
    ensemble_model_step_1.input_map["input"] = "input"
    ensemble_model_step_1.output_map["output"] = "inference_output"

    ensemble_model_step_1.output_map["mask"] = "inference_mask"

    ensemble_model_step_1.output_map["anomalies"] = "inference_anomalies"
    ensemble_model_step_1.output_map["output_score"] = "inference_score"
    ensemble_model_step_1.output_map["output_confidence"] = "inference_confidence"

    ensemble_model_step_2 = triton_model_config.ModelEnsembling.Step()
    ensemble_model_step_2.model_name = f"marshal_{model_name}"
    # -1 indicates latest
    ensemble_model_step_2.model_version = -1
    ensemble_model_step_2.input_map["input"] = "input"
    ensemble_model_step_2.input_map["inference_output"] = "inference_output"
    ensemble_model_step_2.input_map["inference_mask"] = "inference_mask"
    ensemble_model_step_2.input_map["inference_anomalies"] = "inference_anomalies"
    ensemble_model_step_2.input_map["inference_confidence"] = "inference_confidence"
    ensemble_model_step_2.input_map["inference_score"] = "inference_score"

    ensemble_model_step_2.input_map["metadata"] = "METADATA"
    ensemble_model_step_2.output_map["output"] = "output_capture"
    ensemble_model_step_2.output_map["mask"] = "output_mask"
    ensemble_model_step_2.output_map["overlay"] = "output_overlay"
    ensemble_model_step_2.output_map["output_anomalous"] = "output_anomalous"
    ensemble_model_step_2.output_map["output_confidence"] = "output_confidence"
    ensemble_model_ensemble = triton_model_config.ModelEnsembling()
    ensemble_model_ensemble.step.extend([ensemble_model_step_1, ensemble_model_step_2])
    ensemble_model_config.ensemble_scheduling.CopyFrom(ensemble_model_ensemble)
    return text_format.MessageToString(
        ensemble_model_config, use_short_repeated_primitives=True, use_index_order=True
    )


def _base_model_builder(
    deployed_model_path: str,
    model_name: str,
    model_version: str,
    manifest: dict,
):
    # Base lfv model create config.pbtxt.
    # Use dataset information.
    # Dynamic input if the model does not support anomaly localization, OR if it
    # runs on the ONNX runtime (which resizes internally). See
    # _resolve_base_input_shape.
    input_shape = _resolve_base_input_shape(manifest)
    base_model_config_pbtxt = _create_base_model_config_pbtxt(model_name, input_shape)

    def _build(staging_path: str) -> bool:
        # Populate the staging dir fully; config.pbtxt is written LAST so the
        # published dir is only ever observable in a complete state.
        try:
            os.makedirs(os.path.join(staging_path, model_version))
        except OSError as e:
            logging.error(f"Unable to create directory '{staging_path}' error {str(e)}")
            return False
        # Get working directory of this code to get the model.py file
        working_dir = os.path.dirname(os.path.realpath(__file__))
        model_py_path = os.path.abspath(
            os.path.join(working_dir, "resources_for_copy", "lfv_model_template.py")
        )
        if not os.path.exists(model_py_path):
            logging.error(f"Unable to find lfv_model_template.py file at '{model_py_path}'")
            return False
        try:
            shutil.copy(model_py_path, os.path.join(staging_path, model_version, "model.py"))
        except OSError as e:
            logging.error(f"Unable to copy model.py file error {str(e)}")
            return False
        # Always copy the pluggable runtime abstraction next to model.py so the
        # template can dispatch DLR / ONNX / PyTorch engines per the manifest
        # `runtime` field (see docs/multi-runtime-inference.md). Absent for DLR-only
        # devices this is harmless; the DLR path is unchanged.
        runtimes_py_path = os.path.abspath(
            os.path.join(working_dir, "resources_for_copy", "inference_runtimes.py")
        )
        if os.path.exists(runtimes_py_path):
            try:
                shutil.copy(
                    runtimes_py_path,
                    os.path.join(staging_path, model_version, "inference_runtimes.py"),
                )
            except OSError as e:
                logging.error(f"Unable to copy inference_runtimes.py file error {str(e)}")
                return False
        else:
            logging.warning(
                f"inference_runtimes.py not found at '{runtimes_py_path}'; "
                f"non-DLR runtimes will be unavailable"
            )
        # Create symlinks for model folders
        ret = create_sym_links(deployed_model_path, os.path.join(staging_path, model_version))
        if not ret:
            logging.error(f"Unable to create symbolic links for model folders")
            return False
        # Write config.pbtxt LAST — this is what makes the model discoverable.
        try:
            with open(os.path.join(staging_path, "config.pbtxt"), "w", encoding="utf-8") as f:
                f.write(base_model_config_pbtxt)
        except OSError as e:
            logging.error(f"Unable to write config.pbtxt file error {str(e)}")
            return False
        return True

    return _build


def _create_base_model_structure(
    model_repo_dir: str,
    deployed_model_path: str,
    model_name: str,
    model_version: str,
    manifest: dict,
) -> bool:
    return _atomic_publish_model_dir(
        model_repo_dir,
        f"base_{model_name}",
        _base_model_builder(deployed_model_path, model_name, model_version, manifest),
    )


def _marshal_model_builder(model_name: str, model_version: str, manifest: dict):
    input_shape = _resolve_base_input_shape(manifest)
    marshal_model_config_pbtxt = _create_marshal_model_config_pbtxt(model_name, input_shape)

    def _build(staging_path: str) -> bool:
        try:
            os.makedirs(os.path.join(staging_path, model_version))
        except OSError as e:
            logging.error(f"Unable to create directory '{staging_path}' error {str(e)}")
            return False
        # Get working directory of this code to get the model.py file
        working_dir = os.path.dirname(os.path.realpath(__file__))
        model_py_path = os.path.abspath(
            os.path.join(working_dir, "resources_for_copy", "marshal_for_capture_template.py")
        )
        if not os.path.exists(model_py_path):
            logging.error(
                f"Unable to find marshal_for_capture_template.py file at '{model_py_path}'"
            )
            return False
        try:
            shutil.copy(model_py_path, os.path.join(staging_path, model_version, "model.py"))
        except OSError as e:
            logging.error(f"Unable to copy model.py file error {str(e)}")
            return False
        # Write config.pbtxt LAST — this is what makes the model discoverable.
        try:
            with open(os.path.join(staging_path, "config.pbtxt"), "w", encoding="utf-8") as f:
                f.write(marshal_model_config_pbtxt)
        except OSError as e:
            logging.error(f"Unable to write config.pbtxt file error {str(e)}")
            return False
        return True

    return _build


def _create_marshal_model_structure(
    model_repo_dir: str, model_name: str, model_version: str, manifest: dict
) -> bool:
    return _atomic_publish_model_dir(
        model_repo_dir,
        f"marshal_{model_name}",
        _marshal_model_builder(model_name, model_version, manifest),
    )


def _ensemble_model_builder(model_name: str, model_version: str, manifest: dict):
    input_shape = _resolve_base_input_shape(manifest)
    ensemble_model_config_pbtxt = _create_ensemble_model_config_pbtxt(model_name, input_shape)

    def _build(staging_path: str) -> bool:
        try:
            os.makedirs(os.path.join(staging_path, model_version))
        except OSError as e:
            logging.error(f"Unable to create directory '{staging_path}' error {str(e)}")
            return False
        # Get working directory of this code to get the ensemble_model file
        working_dir = os.path.dirname(os.path.realpath(__file__))
        ensemble_file_path = os.path.abspath(
            os.path.join(working_dir, "resources_for_copy", "ensemble_model")
        )
        if not os.path.exists(ensemble_file_path):
            logging.error(f"Unable to find ensemble_model file at '{ensemble_file_path}'")
            return False
        try:
            shutil.copy(
                ensemble_file_path, os.path.join(staging_path, model_version, "ensemble_model")
            )
        except OSError as e:
            logging.error(f"Unable to copy ensemble_model file error {str(e)}")
            return False
        # Write config.pbtxt LAST — this is what makes the model discoverable.
        try:
            with open(os.path.join(staging_path, "config.pbtxt"), "w", encoding="utf-8") as f:
                f.write(ensemble_model_config_pbtxt)
        except OSError as e:
            logging.error(f"Unable to write config.pbtxt file error {str(e)}")
            return False
        return True

    return _build


def _create_ensemble_model_structure(
    model_repo_dir: str, model_name: str, model_version: str, manifest: dict
) -> bool:
    return _atomic_publish_model_dir(
        model_repo_dir, model_name, _ensemble_model_builder(model_name, model_version, manifest)
    )


def convert_to_triton_structure(
    model_repo_dir: str, deployed_model_path: str, model_name: str, model_version: str = "1"
) -> bool:
    """
    Convert the model to Triton's structure and save it to the specified path.

    Args:
        model_repo_dir (str): The path to the model repository directory.
        deployed_model_path (str): The original model path of lfv deployed model.
        model_name (str): The name of the model.
        model_version (str): Model version passed from lfv, default is 1


    Returns:
        bool: True if the conversion is successful, False otherwise.
    """
    # Check if model_repo_dir exists
    if not os.path.exists(model_repo_dir):
        os.makedirs(model_repo_dir)
        logging.info(f"model_repo_dir '{model_repo_dir}' created")

    # Check if model_repo_dir is an absolute path
    if not os.path.isabs(model_repo_dir) or not os.path.isdir(model_repo_dir):
        logging.error(
            f"model_repo_dir must be an absolute path and a directory, got '{model_repo_dir}'"
        )
        return False

    # Check if deployed_model_path exists
    if not os.path.exists(deployed_model_path):
        logging.error(f"deployed_model_path '{deployed_model_path}' does not exist")
        return False

    # Check if deployed_model_path is an absolute path
    if not os.path.isabs(deployed_model_path) or not os.path.isdir(deployed_model_path):
        logging.error(
            f"deployed_model_path must be an absolute path and a directory,  got '{deployed_model_path}'"
        )
        return False

    # Check if model_name is empty.
    if not model_name:
        logging.error(f"model_name cannot be empty got '{model_name}'")
        return False
    try:
        model_version_int = int(model_version)
        if model_version_int <= 0:
            raise Exception("model version should be > 0")
    except Exception as e:
        logging.error(
            f"Error converting model_version to int , it should be > 0 got {model_version}, exception : {e}"
        )
        return False
    manifest_json_path = os.path.join(deployed_model_path, "manifest.json")
    if not os.path.exists(manifest_json_path):
        logging.error(f"unable to find manifest.json in model_repo_dir '{model_repo_dir}'")
        return False
    manifest = None
    try:
        with open(manifest_json_path, encoding="utf-8") as f:
            manifest = json.load(f)
        if manifest is None:
            logging.error("Error reading lfv manifest")
            return False
    except Exception as e:
        logging.error(f"Error reading lfv manifest: {str(e)}")
        return False
    # Stage all three directories, leave unchanged ones untouched, and swap the
    # changed ones under one exclusive hold of the model's lock (design D9).
    return _publish_model_dirs(
        model_repo_dir,
        model_name,
        [
            (
                f"base_{model_name}",
                _base_model_builder(deployed_model_path, model_name, model_version, manifest),
                "base",
            ),
            (
                f"marshal_{model_name}",
                _marshal_model_builder(model_name, model_version, manifest),
                "marshal",
            ),
            (model_name, _ensemble_model_builder(model_name, model_version, manifest), "ensemble"),
        ],
    )


def _sigterm_exit(signum, frame):
    """Greengrass ends a Startup with SIGTERM. Raising SystemExit runs the
    ``finally`` blocks (staging directories removed, lock released) and keeps
    the exit code 143 (design D9)."""
    raise SystemExit(143)


def valid_model_name(name) -> bool:
    return bool(
        isinstance(name, str) and _MODEL_NAME_PATTERN.match(name) and name not in (".", "..")
    )


def _utc_iso(epoch=None):
    if model_status_records is not None:
        return model_status_records.utc_iso(epoch)
    moment = time.gmtime(time.time() if epoch is None else epoch)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", moment)


def _scan_container_log(container_id, since, model_name, budget_s):
    """The first pb_stub "model file not found" line of ``model_name`` in one
    container's log since ``since``, or None."""
    logs_cmd = ["docker", "logs", "--timestamps", "--since", str(since), container_id]
    proc = subprocess.Popen(logs_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    timer = threading.Timer(max(0.0, min(PB_STUB_PER_CONTAINER_S, budget_s)), proc.kill)
    timer.daemon = True
    timer.start()
    needles = (f"/base_{model_name}/", f"/marshal_{model_name}/")
    scanned = 0
    try:
        for raw in proc.stdout:
            scanned += len(raw)
            if scanned > PB_STUB_SCAN_BYTES:
                logging.debug(f"pb_stub capture: {container_id} log exceeds the scan cap")
                return None
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if PB_STUB_MARKER not in line or not any(n in line for n in needles):
                continue
            stamp, _sep, text = line.partition(" ")
            return {
                "line": text[:PB_STUB_LINE_MAX],
                "at": stamp,
                "capturedAt": _utc_iso(),
            }
        return None
    finally:
        timer.cancel()
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass


def capture_pb_stub_missing_file(model_name, startup_started_epoch):
    """Best effort: the stub's "Python model file not found" line for this
    model from the backend container's log, or None (design D5)."""
    started = time.monotonic()
    try:
        ids_cmd = ["docker", "ps", "-q", "--filter", "ancestor=flask-app"]
        ids = subprocess.run(
            ids_cmd, capture_output=True, text=True, timeout=min(10, PB_STUB_TOTAL_S)
        )
        container_ids = [c.strip() for c in (ids.stdout or "").splitlines() if c.strip()]
        since = int(startup_started_epoch)
        for container_id in container_ids[:PB_STUB_MAX_CONTAINERS]:
            remaining = PB_STUB_TOTAL_S - (time.monotonic() - started)
            if remaining <= 0:
                logging.debug("pb_stub capture: the time budget ran out")
                return None
            found = _scan_container_log(container_id, since, model_name, remaining)
            if found is not None:
                return found
        logging.debug(f"pb_stub capture: no matching line for {model_name}")
    except Exception as e:  # noqa: BLE001 - best effort
        logging.debug(f"pb_stub capture failed: {e}")
    return None


def _report_not_ready(model_name, model_version, outcome, startup_started_epoch):
    """Record a Startup that could not confirm READY and log it (design D5).
    Never raises."""
    state = outcome.state or "unknown"
    head = (
        f"StartModel: {model_name} is not READY at the end of its Startup (state {state}, "
        f"{outcome.attempts} attempts over {int(outcome.elapsed_s)}s): {outcome.reason}."
    )
    level = logging.WARNING if outcome.state == "LOADING" else logging.ERROR
    path = os.path.join(
        model_status_records.MODEL_STATUS_DIR if model_status_records is not None
        else "/aws_dda/dda_triton/model_status",
        f"{model_name}.json",
    )
    try:
        if model_status_records is None:
            _warn_import("model_status_records")
            written = False
        else:
            path = model_status_records.load_outcome_path(model_name)
            pb_stub = capture_pb_stub_missing_file(model_name, startup_started_epoch)
            written = model_status_records.write_load_outcome(
                model_name,
                state=outcome.state,
                reason=outcome.reason,
                failure_reason=outcome.failure_reason,
                backend_reachable=outcome.backend_reachable,
                attempts=outcome.attempts,
                component_version=model_version,
                startup_started_at=_utc_iso(startup_started_epoch),
                pb_stub_missing_file=pb_stub,
            )
    except Exception as e:  # noqa: BLE001
        logging.debug(f"StartModel: recording the outcome of {model_name} failed: {e}")
        written = False
    if written:
        logging.log(
            level,
            f"{head} Recorded the outcome in {path}; the Startup still exits 0 so the "
            f"deployment is not rolled back.",
        )
    else:
        logging.error(
            f"{head} The outcome could not be recorded in {path}; the Startup still exits 0 "
            f"so the deployment is not rolled back."
        )


def main(argv=None):
    signal.signal(signal.SIGTERM, _sigterm_exit)
    args = parser.parse_args(argv)
    if not (args.unarchived_model_path and args.model_name and args.model_version is not None):
        logging.error("Args not provided to do the conversion")
        return 0
    if not valid_model_name(args.model_name):
        logging.error(
            f"Exception occured while doing model conversion : invalid model name "
            f"{args.model_name!r}"
        )
        return 1
    startup_started_epoch = time.time()
    try:
        input_model_path = args.unarchived_model_path
        model_name = args.model_name
        model_version = args.model_version
        model_version_spiltted = model_version.split(".")[0]
        logging.info(
            f"model unzipped path and major version provided is {input_model_path} : {model_version.split('.')[0]}"
        )
        model_converted = convert_to_triton_structure(
            model_repo_dir=TRITON_MODEL_DIR,
            deployed_model_path=input_model_path,
            model_name=model_name,
            model_version=model_version_spiltted,
        )
        if not model_converted:
            raise Exception("Model conversion failed")
    except Exception as e:
        logging.error(f"Exception occured while doing model conversion : {str(e)}")
        return 1
    logging.info("Model converted successfully")
    try:
        outcome = start_model_detailed(model_name)
        if outcome.ready:
            if model_status_records is not None:
                model_status_records.clear_load_outcome(model_name)
        else:
            _report_not_ready(model_name, model_version, outcome, startup_started_epoch)
    except Exception as e:  # noqa: BLE001 - a recorded or not, the Startup exits 0
        logging.error(f"StartModel: unexpected error after the conversion of {model_name}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
