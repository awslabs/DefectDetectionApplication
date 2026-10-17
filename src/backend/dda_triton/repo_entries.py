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
"""A model's Triton repository entry, and the hidden directories next to it
(triton-model-cleanup-wedge, design D4).

* :func:`inspect_entry` decides whether an entry is complete and has been
  unchanged for a quiet period. Its ``blocker`` is exactly what the
  continuous ModelGate's ``_file_state`` returned before this change (the
  body moved here verbatim), so the per-run gate, ModelGate and the
  watchdog all judge an entry the same way.
* Hidden directories: a publish stages into ``.staging-<name>-<stamp>-<rnd>``
  and a removal renames into ``.trash-<dir>-<stamp>-<pid>``. The boot stamp
  (``<boot id[:8]>-<CLOCK_BOOTTIME seconds>``) makes their age independent of
  the wall clock, which NTP can step by more than 10 min at boot on a Jetson
  without a working RTC. :func:`sweep_stale_hidden_dirs` removes the ones
  that are stale: from another boot, or older than
  :data:`HIDDEN_DIR_STALE_S` by ``CLOCK_BOOTTIME``.

Host-shared: seeded to ``/aws_dda/dda_triton/`` and run by the host's
system ``python3`` (3.8 on JP5). Standard library only.
"""
import glob
import logging
import os
import re
import shutil
import time
from typing import Callable, Iterable, List, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

#: A stamped hidden directory from this boot is stale after this many
#: seconds of ``CLOCK_BOOTTIME``.
HIDDEN_DIR_STALE_S = 600

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

#: A Triton ``config.pbtxt`` ensemble step: ``model_name: "base_model-x"``.
_STEP_MODEL_PATTERN = re.compile(r'model_name:\s*"([^"]+)"')
#: A model config served by the python backend, whose version directory
#: must therefore carry ``model.py`` before Triton can preinitialize it.
_PYTHON_BACKEND_PATTERN = re.compile(r'backend:\s*"python"')
#: ``model_convertor``'s staging sibling.
_STAGING_PREFIX = ".staging-"
_TRASH_PREFIX = ".trash-"

_BOOT8 = re.compile(r"^[0-9a-f]{8}$")
_STAMPED_STAGING = re.compile(
    r"^\.staging-(?P<name>.+)-(?P<boot>[0-9a-f]{8})-(?P<bt>[0-9]+)-[a-z0-9_]{8}$")
_STAMPED_TRASH = re.compile(
    r"^\.trash-(?P<dir>.+)-(?P<boot>[0-9a-f]{8})-(?P<bt>[0-9]+)-(?P<pid>[0-9]+)$")
#: ``tempfile.mkdtemp``'s random suffix.
_MKDTEMP_SUFFIX = re.compile(r"^[a-z0-9_]{8}$")


class EntryInspection(NamedTuple):
    """What :func:`inspect_entry` found for one model."""

    #: ``<repo>/<target>`` is a directory (ModelGate's NOT_DEPLOYED test).
    present: bool
    #: None when the entry is complete and unchanged for the quiet period;
    #: otherwise the reason, exactly as ``ModelGate._file_state`` gave it.
    blocker: Optional[str]
    #: The path named by the first "is missing" check; None otherwise.
    missing_path: Optional[str]
    #: The paths whose mtimes were compared (complete only when nothing is
    #: missing or unreadable).
    paths: Tuple[str, ...]
    newest_path: Optional[str]
    newest_mtime: Optional[float]


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


def inspect_entry(repo: str, target: str, wall: Callable[[], float] = time.time,
                  quiet_period_s: float = 10.0) -> EntryInspection:
    """Inspect ``<repo>/<target>`` and the ensemble steps it names."""
    found = {"missing": None, "paths": [], "complete": False}

    def missing(path: str) -> str:
        if found["missing"] is None:
            found["missing"] = path
        return "{0} is missing".format(path)

    def file_state() -> Optional[str]:
        # Moved verbatim from ModelGate._file_state (model_gate.py, before
        # triton-model-cleanup-wedge); only self._repo, self._wall() and
        # QUIET_PERIOD_S became parameters, and the "is missing" returns
        # also record the path.
        model_dir = os.path.join(repo, target)
        config = os.path.join(model_dir, "config.pbtxt")
        if not os.path.isfile(config):
            return missing(config)
        paths: List[str] = [model_dir, config]
        found["paths"] = paths
        text = _read(config)
        if text is None:
            return "{0} is unreadable".format(config)
        steps = [step for step in dict.fromkeys(_STEP_MODEL_PATTERN.findall(text))
                 if step != target]
        for step in steps:
            step_dir = os.path.join(repo, step)
            step_config = os.path.join(step_dir, "config.pbtxt")
            if not os.path.isfile(step_config):
                return missing(step_config)
            paths.extend((step_dir, step_config))
            step_text = _read(step_config)
            if step_text is None:
                return "{0} is unreadable".format(step_config)
            if not _PYTHON_BACKEND_PATTERN.search(step_text):
                continue
            versions = _version_dirs(step_dir)
            if not versions:
                return missing(os.path.join(step_dir, "<version>", "model.py"))
            for version in versions:
                model_py = os.path.join(step_dir, version, "model.py")
                if not os.path.isfile(model_py):
                    return missing(model_py)
                paths.append(model_py)
        found["complete"] = True
        for name in [target] + steps:
            staging = sorted(glob.glob(os.path.join(repo, _STAGING_PREFIX + name + "-*")))
            if staging:
                return "{0} is being published".format(staging[0])
        newest_path, newest = None, None
        for path in paths:
            mtime = _mtime(path)
            if mtime is None:
                found["complete"] = False
                return missing(path)
            if newest is None or mtime > newest:
                newest_path, newest = path, mtime
        found["newest"] = (newest_path, newest)
        if newest is not None:
            age = wall() - newest
            if age < quiet_period_s:
                return "{0} changed {1:.1f} s ago".format(newest_path, max(0.0, age))
        return None

    blocker = file_state()
    paths = tuple(found["paths"])
    newest_path, newest = found.get("newest", (None, None))
    if found["complete"] and "newest" not in found:
        # A staging sibling stopped the check before the mtimes were read;
        # read them anyway so callers that need the ages (the watchdog) get
        # them. The blocker stays the one the check returned.
        for path in paths:
            mtime = _mtime(path)
            if mtime is None:
                newest_path, newest = None, None
                break
            if newest is None or mtime > newest:
                newest_path, newest = path, mtime
    return EntryInspection(
        present=os.path.isdir(os.path.join(repo, target)),
        blocker=blocker,
        missing_path=found["missing"],
        paths=paths,
        newest_path=newest_path,
        newest_mtime=newest,
    )


# ------------------------------------------------------- hidden directories

def _boottime() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def boot_stamp(boot_id_path: str = BOOT_ID_PATH,
               boottime: Optional[Callable[[], float]] = None) -> Optional[str]:
    """``<boot8>-<boottime>``, or None when the boot id cannot be read."""
    try:
        with open(boot_id_path, "r", encoding="ascii", errors="replace") as handle:
            boot8 = handle.read().strip()[:8].lower()
        if not _BOOT8.match(boot8):
            return None
        seconds = int((boottime or _boottime)())
    except (OSError, ValueError, AttributeError):
        return None
    return "{0}-{1}".format(boot8, seconds)


def staging_prefix(name: str, stamp_reader: Callable[[], Optional[str]] = None) -> str:
    """The ``tempfile.mkdtemp`` prefix of a publish's staging directory."""
    stamp = (stamp_reader or boot_stamp)()
    if stamp:
        return "{0}{1}-{2}-".format(_STAGING_PREFIX, name, stamp)
    return "{0}{1}-".format(_STAGING_PREFIX, name)


def trash_name(directory: str, stamp_reader: Callable[[], Optional[str]] = None,
               pid: Optional[int] = None) -> str:
    """The name a removal renames ``directory`` to."""
    stamp = (stamp_reader or boot_stamp)()
    pid = os.getpid() if pid is None else pid
    if stamp:
        return "{0}{1}-{2}-{3}".format(_TRASH_PREFIX, directory, stamp, pid)
    return "{0}{1}-{2}-{3}".format(_TRASH_PREFIX, directory, pid, time.time_ns())


def _split_stamp(stamp: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
    if not stamp:
        return None, None
    try:
        boot, seconds = stamp.split("-", 1)
        return boot, int(seconds)
    except ValueError:
        return None, None


def _unstamped_staging_name_matches(entry: str, names: Iterable[str]) -> bool:
    for name in names:
        prefix = "{0}{1}-".format(_STAGING_PREFIX, name)
        if entry.startswith(prefix) and _MKDTEMP_SUFFIX.match(entry[len(prefix):]):
            return True
    return False


def sweep_stale_hidden_dirs(repo: str, names: Optional[Iterable[str]] = None,
                            stale_after_s: float = HIDDEN_DIR_STALE_S,
                            stamp_reader: Callable[[], Optional[str]] = None,
                            wall: Callable[[], float] = time.time) -> List[str]:
    """Remove stale ``.staging-*`` and ``.trash-*`` directories of ``repo``
    and return their names. With ``names``, only the staging directories of
    those model directories are considered (no trash). Never raises."""
    removed: List[str] = []
    try:
        wanted = None if names is None else set(names)
        current_boot, current_bt = _split_stamp((stamp_reader or boot_stamp)())
        try:
            entries = sorted(os.listdir(repo))
        except OSError:
            return removed
        for entry in entries:
            try:
                is_staging = entry.startswith(_STAGING_PREFIX)
                is_trash = entry.startswith(_TRASH_PREFIX)
                if not (is_staging or is_trash):
                    continue
                if wanted is not None and not is_staging:
                    continue
                path = os.path.join(repo, entry)
                if os.path.islink(path) or not os.path.isdir(path):
                    continue
                match = (_STAMPED_STAGING if is_staging else _STAMPED_TRASH).match(entry)
                if wanted is not None:
                    if match is not None:
                        if match.group("name") not in wanted:
                            continue
                    elif not _unstamped_staging_name_matches(entry, wanted):
                        continue
                if match is not None and current_boot is not None:
                    if match.group("boot") != current_boot:
                        stale = True
                    else:
                        stale = current_bt - int(match.group("bt")) > stale_after_s
                else:
                    mtime = _mtime(path)
                    stale = mtime is not None and wall() - mtime > stale_after_s
                if not stale:
                    continue
                try:
                    shutil.rmtree(path)
                except FileNotFoundError:
                    continue  # removed meanwhile (a concurrent sweep or cleanup)
                removed.append(entry)
            except Exception:  # noqa: BLE001 - the directory stays for the next sweep
                logger.debug("Could not sweep hidden directory %s in %s", entry, repo,
                             exc_info=True)
    except Exception:  # noqa: BLE001 - a sweep never fails its caller
        logger.debug("Hidden-directory sweep of %s failed", repo, exc_info=True)
    return removed
