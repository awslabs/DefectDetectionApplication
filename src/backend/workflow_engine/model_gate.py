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
"""The Continuous_Runner's Triton model gate (rtsp-rtmp-stream-cameras
Requirements 11.1, 11.11, 11.12; design component 14, "Model gate").

Why this exists (finding 16, measured on the MIC-730 and the Dell)
------------------------------------------------------------------
After a LocalServer deployment Greengrass restarts the dependent model
components, and each one's Startup rewrites its entries in the Triton
model repository — about 26-31 s after the backend starts. A continuous
registration that was persisted as running resumes within seconds of that
start, so without a gate its first runs ask Triton to load a model whose
repository files are missing or half-written. Triton then queues a load
that can never succeed, and edgemlsdk's cached state stays ``LOADING``.
On the MIC-730 the workflows then made no progress for 19 minutes, until
the backend was restarted; on the Dell 97 runs failed during the rewrite.

:class:`ModelGate` is the fix. :meth:`ModelGate.check` returns ``None``
while every model the workflow uses is ready, and otherwise a
:class:`ModelWait` describing the model the runner is waiting for. The
runner starts no runs while a wait is outstanding, so no run is burnt on
a repository that is still being written.

What it does NOT do
-------------------
It never replaces the executor's per-run gate
(``dda_triton.model_readiness.ensure_model_ready``), which still runs
exactly as before. And it fails OPEN: any unexpected failure while
reading Triton state counts as ready, with one WARNING, so the gate can
never be the sole reason a previously working workflow stops running.

``dda_triton`` and ``utils`` are imported lazily, inside functions,
because ``dda_triton.triton_edge_client`` imports the on-device
``panorama`` module at module level.
"""
import glob
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: The Triton model repository the model components publish into. The same
#: path the executor resolves model names against.
MODEL_REPO = "/aws_dda/dda_triton/triton_model_repo"

#: Triton's own state vocabulary (``triton_server.cpp::GetModelStatus``).
STATE_READY = "READY"
STATE_LOADING = "LOADING"
STATE_UNLOADING = "UNLOADING"
STATE_UNAVAILABLE = "UNAVAILABLE"
STATE_UNKNOWN = "UNKNOWN"
#: The gate's own two states, decided from the filesystem alone.
STATE_NOT_DEPLOYED = "NOT_DEPLOYED"
STATE_INCOMPLETE = "INCOMPLETE"

#: How long the model's repository files must have been unchanged before a
#: load may be requested (Requirement 11.12). The model components write
#: config.pbtxt last, but they publish base, marshal and ensemble as three
#: separate atomic renames, so a complete-looking repository can still be
#: mid-rewrite.
QUIET_PERIOD_S = 10.0

#: An UNKNOWN model whose directory predates the engine's start is only
#: loaded once this long has passed since that start: the model components
#: rewrite the repository about 26-31 s in, and a load requested against
#: the about-to-be-replaced directory is the load that wedges.
UNKNOWN_LOAD_GRACE_S = 120.0

#: Seconds after the FIRST UNAVAILABLE sighting at which a load is
#: requested again, then :data:`UNAVAILABLE_RETRY_INTERVAL_S` apart.
UNAVAILABLE_BACKOFF_S = (15.0, 30.0, 60.0, 120.0)
UNAVAILABLE_RETRY_INTERVAL_S = 300.0

#: A wait longer than this is reported as stalled (Requirement 11.12), and
#: the status then advises a backend restart. The same budget as the
#: per-run gate's ``model_readiness.READY_TIMEOUT_S``: a legitimate load can
#: take minutes. After the JP5 1.0.51 deployment on the MIC-730 the model
#: components' own loads of its three models took 4 min 17 s, and the
#: workflows waited 4 min 35 s; the 300 s first chosen would have advised a
#: restart for a slightly slower but normal deployment.
STALL_AFTER_S = 600.0

#: A Triton ``config.pbtxt`` ensemble step: ``model_name: "base_model-x"``.
#: pbtxt has no nesting problem here — only ensemble steps use the key.
_STEP_MODEL_PATTERN = re.compile(r'model_name:\s*"([^"]+)"')
#: A model config served by the python backend, whose version directory
#: must therefore carry ``model.py`` before Triton can preinitialize it.
_PYTHON_BACKEND_PATTERN = re.compile(r'backend:\s*"python"')
#: ``model_convertor._atomic_publish_model_dir``'s staging sibling.
_STAGING_PREFIX = ".staging-"


@dataclass(frozen=True)
class ModelWait:
    """Why the Continuous_Runner is not starting runs.

    ``model`` is the document's own ``emltriton`` model name and
    ``triton_model`` the deployed name it resolved to; ``since_ms`` is the
    wall time the current wait began, and ``stalled`` is true once it has
    lasted longer than :data:`STALL_AFTER_S`.
    """

    model: str
    triton_model: str
    state: str
    reason: Optional[str]
    since_ms: int
    stalled: bool

    def as_document(self) -> Dict[str, Any]:
        """The ``modelReadiness`` document of the Continuous status."""
        return {
            "model": self.model,
            "tritonModel": self.triton_model,
            "state": self.state,
            "reason": self.reason,
            "sinceMs": self.since_ms,
            "stalled": self.stalled,
        }


def _default_client():
    """The process-wide Triton client, imported lazily (see the module
    docstring)."""
    from dda_triton.triton_edge_client import TritonEdgeClient

    return TritonEdgeClient.get_instance()


def _default_repo_has_models(repo: str) -> bool:
    """Whether anything at all is deployed. Reuses the existing guard:
    creating the Triton server against an empty repository hangs."""
    from utils.feature_configs_utils import triton_repo_has_models

    return bool(triton_repo_has_models(repo))


class ModelGate:
    """The models a continuous workflow needs, and whether Triton has them.

    ``models`` are the document's distinct ``emltriton`` model names. Every
    collaborator is injectable so the gate is testable off-device against a
    temporary repository directory: ``client_provider`` returns the Triton
    client, ``repo_has_models`` takes the repository path, ``clock`` is
    monotonic (waits and backoff) and ``wall`` is epoch seconds (file ages
    and ``sinceMs``). ``engine_started_at`` is a wall time — the
    ContinuousRunnerManager's construction time, i.e. the engine's start.
    """

    def __init__(self, models: Sequence[str], *,
                 repo: str = MODEL_REPO,
                 client_provider: Optional[Callable[[], Any]] = None,
                 repo_has_models: Optional[Callable[[str], bool]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 engine_started_at: Optional[float] = None):
        self._models: Tuple[str, ...] = tuple(dict.fromkeys(m for m in models if m))
        self._repo = repo
        self._client_provider = client_provider or _default_client
        self._repo_has_models = repo_has_models or _default_repo_has_models
        self._clock = clock
        self._wall = wall
        self._engine_started_at = float(
            engine_started_at if engine_started_at is not None else wall())
        self._wait_since: Optional[float] = None
        self._wait_since_ms: Optional[int] = None
        #: Models a load has been requested for while UNKNOWN: exactly one
        #: per model, until it is seen READY again (a republish reloads).
        self._load_requested = set()
        self._unavailable_since: Dict[str, float] = {}
        self._unavailable_attempts: Dict[str, int] = {}
        self._failed_open = False

    @property
    def models(self) -> Tuple[str, ...]:
        return self._models

    # -- the gate ---------------------------------------------------------------

    def check(self) -> Optional[ModelWait]:
        """None when every model is ready (or there is no model at all);
        otherwise the wait for the first model that is not.

        Reads Triton's index at most once per call, so a document with
        several models pays for one listing.
        """
        if not self._models:
            return None
        cache: Dict[str, Any] = {}
        for model in self._models:
            unready = self._check_one(model, cache)
            if unready is None:
                continue
            triton_model, state, reason = unready
            if self._wait_since is None:
                self._wait_since = self._clock()
                self._wait_since_ms = int(self._wall() * 1000)
            waited = max(0.0, self._clock() - self._wait_since)
            return ModelWait(model=model, triton_model=triton_model, state=state,
                             reason=reason, since_ms=int(self._wait_since_ms or 0),
                             stalled=waited > STALL_AFTER_S)
        self._wait_since = None
        self._wait_since_ms = None
        return None

    def _check_one(self, model: str, cache: Dict[str, Any]):
        """``(triton_model, state, reason)`` while ``model`` is not ready,
        else None."""
        triton_model = self._resolve(model, cache)
        model_dir = os.path.join(self._repo, triton_model)
        if not os.path.isdir(model_dir) or not self._repo_deployed(cache):
            return (triton_model, STATE_NOT_DEPLOYED,
                    "{0} is not deployed in {1}".format(triton_model, self._repo))
        incomplete = self._file_state(triton_model)
        if incomplete is not None:
            return (triton_model, STATE_INCOMPLETE, incomplete)
        states = self._states(cache)
        if states is None:
            return None  # fail open, see the module docstring
        entry = states.get(triton_model) or {}
        state = entry.get("state") or STATE_UNKNOWN
        reason = entry.get("reason")
        if state == STATE_READY:
            self._load_requested.discard(model)
            self._unavailable_since.pop(model, None)
            self._unavailable_attempts.pop(model, None)
            return None
        if state == STATE_UNKNOWN:
            self._maybe_load_unknown(model, triton_model, model_dir)
        elif state == STATE_UNAVAILABLE:
            self._maybe_reload_unavailable(model, triton_model)
        return (triton_model, state, reason)

    # -- the repository --------------------------------------------------------

    def _resolve(self, model: str, cache: Dict[str, Any]) -> str:
        """The deployed Triton name, resolved exactly as the executor does."""
        if "resolve" not in cache:
            try:
                from workflow_engine.pipeline_executor import (
                    _loaded_ensemble_models,
                    resolve_triton_model_name,
                )

                cache["resolve"] = (resolve_triton_model_name, _loaded_ensemble_models(self._repo))
            except Exception:  # noqa: BLE001 - resolution is best effort
                logger.debug("Triton model-name resolution unavailable", exc_info=True)
                cache["resolve"] = None
        resolution = cache["resolve"]
        if resolution is None:
            return model
        resolve, loaded = resolution
        try:
            return resolve(model, loaded) or model
        except Exception:  # noqa: BLE001
            logger.debug("Could not resolve Triton model %s", model, exc_info=True)
            return model

    def _repo_deployed(self, cache: Dict[str, Any]) -> bool:
        if "deployed" not in cache:
            try:
                cache["deployed"] = bool(self._repo_has_models(self._repo))
            except Exception:  # noqa: BLE001 - treat an unreadable repo as empty
                logger.debug("Could not read the Triton model repository %s", self._repo,
                             exc_info=True)
                cache["deployed"] = False
        return cache["deployed"]

    def _file_state(self, triton_model: str) -> Optional[str]:
        """None when the model's repository files are complete and have been
        unchanged for :data:`QUIET_PERIOD_S`; otherwise the reason, naming
        the missing or changing path (Requirement 11.12)."""
        model_dir = os.path.join(self._repo, triton_model)
        config = os.path.join(model_dir, "config.pbtxt")
        if not os.path.isfile(config):
            return "{0} is missing".format(config)
        paths: List[str] = [model_dir, config]
        text = _read(config)
        if text is None:
            return "{0} is unreadable".format(config)
        steps = [step for step in dict.fromkeys(_STEP_MODEL_PATTERN.findall(text))
                 if step != triton_model]
        for step in steps:
            step_dir = os.path.join(self._repo, step)
            step_config = os.path.join(step_dir, "config.pbtxt")
            if not os.path.isfile(step_config):
                return "{0} is missing".format(step_config)
            paths.extend((step_dir, step_config))
            step_text = _read(step_config)
            if step_text is None:
                return "{0} is unreadable".format(step_config)
            if not _PYTHON_BACKEND_PATTERN.search(step_text):
                continue
            versions = _version_dirs(step_dir)
            if not versions:
                return "{0} is missing".format(os.path.join(step_dir, "<version>", "model.py"))
            for version in versions:
                model_py = os.path.join(step_dir, version, "model.py")
                if not os.path.isfile(model_py):
                    return "{0} is missing".format(model_py)
                paths.append(model_py)
        for name in [triton_model] + steps:
            staging = sorted(glob.glob(os.path.join(self._repo, _STAGING_PREFIX + name + "-*")))
            if staging:
                return "{0} is being published".format(staging[0])
        newest_path, newest = None, None
        for path in paths:
            mtime = _mtime(path)
            if mtime is None:
                return "{0} is missing".format(path)
            if newest is None or mtime > newest:
                newest_path, newest = path, mtime
        if newest is not None:
            age = self._wall() - newest
            if age < QUIET_PERIOD_S:
                return "{0} changed {1:.1f} s ago".format(newest_path, max(0.0, age))
        return None

    # -- Triton ----------------------------------------------------------------

    def _states(self, cache: Dict[str, Any]) -> Optional[Dict[str, Dict[str, Any]]]:
        """Triton's index, read FRESH, or None to fail open.

        ``ListModels`` refreshes edgemlsdk's cached states from Triton's own
        index; ``get_model_status`` returns only that cache, which is what
        stayed ``LOADING`` for 19 minutes on the MIC-730.
        """
        if "states" in cache:
            return cache["states"]
        states: Optional[Dict[str, Dict[str, Any]]] = None
        try:
            records = self._client_provider().list_triton_models(quiet=True) or []
            states = {}
            for record in records:
                if not isinstance(record, dict):
                    continue
                name = record.get("model_component")
                if not name:
                    continue
                states[name] = {
                    "state": str(record.get("status") or STATE_UNKNOWN).strip().upper(),
                    "reason": record.get("reason"),
                }
        except Exception:  # noqa: BLE001 - fail open, see the module docstring
            states = None
            if not self._failed_open:
                self._failed_open = True
                logger.warning(
                    "Could not read Triton model state; continuous runs proceed without "
                    "the model gate", exc_info=True)
        cache["states"] = states
        return states

    def _maybe_load_unknown(self, model: str, triton_model: str, model_dir: str) -> None:
        """One load for a model this Triton process has never been asked for.

        The files are already known complete and stable. The extra grace
        keeps the load away from the window in which the model components
        republish the repository after a LocalServer deployment.
        """
        if model in self._load_requested:
            return
        mtime = _mtime(model_dir)
        fresh = mtime is not None and mtime > self._engine_started_at
        if not fresh and self._wall() - self._engine_started_at < UNKNOWN_LOAD_GRACE_S:
            return
        self._load_requested.add(model)
        self._load(triton_model)

    def _maybe_reload_unavailable(self, model: str, triton_model: str) -> None:
        now = self._clock()
        first = self._unavailable_since.setdefault(model, now)
        attempts = self._unavailable_attempts.get(model, 0)
        if now - first < _backoff_at(attempts):
            return
        self._unavailable_attempts[model] = attempts + 1
        self._load(triton_model)

    def _load(self, triton_model: str) -> None:
        try:
            self._client_provider().start_triton_model(triton_model)
            logger.info("Requested a Triton load of model %s", triton_model)
        except Exception:  # noqa: BLE001 - the next poll reports the state
            logger.warning("Could not request a Triton load of model %s", triton_model,
                           exc_info=True)


def _backoff_at(attempts: int) -> float:
    """15, 30, 60, 120 s after the first UNAVAILABLE sighting, then every
    300 s."""
    if attempts < len(UNAVAILABLE_BACKOFF_S):
        return UNAVAILABLE_BACKOFF_S[attempts]
    extra = attempts - len(UNAVAILABLE_BACKOFF_S) + 1
    return UNAVAILABLE_BACKOFF_S[-1] + UNAVAILABLE_RETRY_INTERVAL_S * extra


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _version_dirs(model_dir: str) -> List[str]:
    """Triton's numeric version directories, oldest name first."""
    try:
        entries = sorted(os.listdir(model_dir))
    except OSError:
        return []
    return [entry for entry in entries
            if entry.isdigit() and os.path.isdir(os.path.join(model_dir, entry))]
