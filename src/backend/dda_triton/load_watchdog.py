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
"""The Triton model-load watchdog (triton-model-cleanup-wedge, design D6-D8).

* **Detection (D6).** One daemon thread, ``triton-load-watchdog``, reads the
  Triton model index every :data:`WATCHDOG_INTERVAL_S`. A model is
  *stalled* only on positive evidence: it has been ``LOADING`` for at least
  :data:`STALL_AFTER_S` by the monotonic clock, the loading models' python
  backend stubs used less than :data:`ACTIVITY_CPU_THRESHOLD_S` CPU-seconds
  over a covered :data:`ACTIVITY_WINDOW_S`, and either its repository entry
  went missing or changed after the load request (E1) or the index has not
  changed for :data:`STALL_AFTER_S` (E2). See :func:`evaluate_wedge`.
* **Self-heal (D7).** On by default on every device, with the per-device
  opt-out (the ``ModelLoadSelfHealEnabled`` configuration key, or the kill
  file ``self-heal.disabled``). It restarts the backend process only when a
  model is stalled, at most 3 times in 24 h and 30 min apart, never while a
  run that the wedge does not block is in flight, a continuous workflow is
  running, a vLLM model is loading or the GPU shows load. Every attempt is
  persisted before the signal and logged as one ERROR line. See
  :func:`decide_self_heal` and :meth:`LoadWatchdog._act`.
* **Surfaces (D8).** :func:`model_annotations` (``/feature-configurations``),
  :func:`build_model_status_snapshot` (the ``dda-model-status`` shadow) and
  :func:`stall_for` (the per-run gate's stall error) read only the published,
  immutable :class:`WatchdogView`.

Importing this module has no side effects and imports no ``panorama``.
"""
import calendar
import collections
import errno
import json
import logging
import os
import re
import signal
import tempfile
import threading
import time
import types
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from dda_triton import model_status_records, provider_visibility, repo_entries
from dda_triton.constants import TRITON_MODEL_DIR
from utils import model_status_shadow
from utils import run_activity as run_activity_module

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------- constants

WATCHDOG_INTERVAL_S = 30
WATCHDOG_STALE_S = 300
LOAD_HEALTH_AFTER_S = 120
STALL_AFTER_S = 900
ACTIVITY_WINDOW_S = 300
#: The /proc and GPU samplers run only while a model has been LOADING this long.
ACTIVITY_SAMPLE_AFTER_S = 60
ACTIVITY_CPU_THRESHOLD_S = 5.0
GPU_SAMPLE_READS = 5
GPU_SAMPLE_GAP_S = 0.1
GPU_ACTIVITY_PERMILLE = 1
GPU_NODE_NAMES = ("gv11b", "ga10b", "gb10b", "gp10b", "gpu")
STUB_PROCESS_NAME = "triton_python_backend_stub"

SWITCH_KEY = "ModelLoadSelfHealEnabled"
SWITCH_SUCCESS_KEY = "StreamShadowName"
SWITCH_REFRESH_S = 300
SWITCH_RETRY_AFTER_S = 60
SWITCH_ON = ("true", "1", "yes", "on", "enabled")
SWITCH_OFF = ("false", "0", "no", "off", "disabled")

SELF_HEAL_MAX_PER_24H = 3
SELF_HEAL_MIN_INTERVAL_S = 1800
SELF_HEAL_WINDOW_S = 86400
QUIESCE_RETRY_AFTER_S = 300
QUIESCE_DRAIN_S = 60
QUIESCE_DRAIN_POLL_S = 1.0
INTERRUPT_BUDGET_S = 10
SELF_HEAL_BACKSTOP_S = 60
SELF_HEAL_BACKSTOP_EXIT_CODE = 86
SELF_HEAL_HISTORY_MAX = 20
SELF_HEAL_ATTEMPT_MODELS_MAX = 10
SELF_HEAL_ATTEMPT_EVIDENCE_MAX = 5
SELF_HEAL_RUNS_INTERRUPTED_MAX = 10
SELF_HEAL_TEXT_MAX = 300
SELF_HEAL_RECORD_WRITE_MAX_BYTES = 49152
SELF_HEAL_RECORD_READ_MAX_BYTES = 65536
DEFERRAL_LOG_EVERY_S = 600
EXHAUSTED_LOG_EVERY_S = 3600
LAST_SELF_HEAL_VISIBLE_S = 86400

SHADOW_MODELS_MAX = 20
SHADOW_NOT_READY_MAX = 10
SHADOW_LAST_MODELS_MAX = 5
SHADOW_LAST_EVIDENCE_MAX = 3
SHADOW_STRING_MAX = 200
READINESS_SHADOW_BUDGET_BYTES = 4096
REPORT_HEARTBEAT_S = 900
RETIREMENTS_MAX = 100
RETIREMENT_KEY_MAX = 255

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

#: The self-heal decisions (D7) and the ``selfHeal.state`` they map to (D8).
DECISION_IDLE = "idle"
DECISION_DISABLED = "disabled"
DECISION_BLOCKED_RECORD = "blocked_record"
DECISION_EXHAUSTED = "exhausted"
DECISION_COOLDOWN = "cooldown"
DECISION_DEFERRED = "deferred"
DECISION_ACT = "act"
_STATE_OF_DECISION = {DECISION_ACT: "restarting"}

STATE_READY = "READY"
STATE_LOADING = "LOADING"
STATE_UNKNOWN = "UNKNOWN"
STATE_UNAVAILABLE = "UNAVAILABLE"
STATE_UNLOADING = "UNLOADING"

_STEP_PREFIXES = ("base_", "marshal_")
_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|\+00:00)$")


# ------------------------------------------------------------------ helpers

def fmt_dur(seconds: Optional[float]) -> str:
    """``1h07m12s``, ``15m02s`` or ``42s``."""
    total = int(max(0.0, float(seconds or 0.0)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "{0}h{1:02d}m{2:02d}s".format(hours, minutes, secs)
    if minutes:
        return "{0}m{1:02d}s".format(minutes, secs)
    return "{0}s".format(secs)


def iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    try:
        return model_status_records.utc_iso(epoch)
    except (OverflowError, OSError, ValueError):
        return None


def parse_iso(text: Any) -> Optional[float]:
    """Epoch seconds of an ISO-8601 UTC timestamp (``Z`` or ``+00:00``,
    any fraction), else None."""
    if not isinstance(text, str):
        return None
    match = _ISO.match(text.strip())
    if not match:
        return None
    try:
        base = calendar.timegm(time.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    fraction = float("0" + match.group(2)) if match.group(2) else 0.0
    return float(base) + fraction


def _cut(text: Any, limit: int) -> Any:
    if isinstance(text, str) and len(text) > limit:
        return text[:limit]
    return text


def _cut_all(value: Any, limit: int) -> Any:
    """Every string in ``value`` cut to ``limit`` characters."""
    if isinstance(value, str):
        return _cut(value, limit)
    if isinstance(value, dict):
        return {k: _cut_all(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cut_all(v, limit) for v in value]
    return value


def component_of(name: str) -> str:
    """``base_model-a`` and ``marshal_model-a`` belong to ``model-a``."""
    for prefix in _STEP_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def is_component(name: str) -> bool:
    return bool(name) and not name.startswith(".") and not name.startswith(_STEP_PREFIXES)


def _canonical(document: Any) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


# ------------------------------------------------------------- data classes

class LoadingModel(collections.namedtuple("LoadingModel", "since_mono since_wall stubs")):
    """A ``LOADING`` component model: when its episode began (``since(m)``,
    D6 step 5) and how many of its stub processes are alive."""

    def __new__(cls, since_mono, since_wall, stubs=0):
        return super().__new__(cls, since_mono, since_wall, stubs)


@dataclass(frozen=True)
class ModelVerdict:
    model: str
    candidate: bool
    stalled: bool
    e1: bool
    e2: bool
    evidence: Tuple[str, ...]
    loading_for_s: float


@dataclass(frozen=True)
class WedgeVerdict:
    models: Mapping[str, ModelVerdict]
    #: The stalled models, by loading start (oldest first), then name.
    stalled: Tuple[str, ...]
    culprit: Optional[str]
    veto: bool
    window_cpu_s: float
    window_covered: bool
    no_progress_s: float


@dataclass(frozen=True)
class SwitchState:
    """The effective self-heal switch."""

    enabled: bool
    #: ``"config"``, ``"file"``, ``"config_unreadable"`` or None.
    disabled_by: Optional[str]
    #: The configured value as read, None for a missing key.
    value: Optional[str] = None
    #: At least one configuration read succeeded in this process.
    read: bool = False
    kill_file: Optional[str] = None


@dataclass(frozen=True)
class GpuWindow:
    available: bool
    max_permille: Optional[int] = None


@dataclass(frozen=True)
class VllmView:
    """``models``: name -> (manager state, feature-config state)."""

    models: Mapping[str, Tuple[str, str]] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass(frozen=True)
class LocalHoldoffs:
    """This process's hold-offs (D7 rules 5 and 8)."""

    deferred_at: Optional[float] = None
    deferred_by: Optional[str] = None
    deferred_recovery: Optional[str] = None
    deferred_until_wall: Optional[float] = None
    aborted_at: Optional[float] = None
    aborted_at_wall: Optional[float] = None


@dataclass(frozen=True)
class History:
    """The persisted self-heal attempts, as read at a decision."""

    attempts: Tuple[Mapping[str, Any], ...] = ()
    #: Set when the record is unreadable or invalid (rule 3).
    error: Optional[str] = None
    boot_id: Optional[str] = None
    boottime: Optional[float] = None


@dataclass(frozen=True)
class SelfHealDecision:
    kind: str
    deferred_by: Optional[str] = None
    #: ``act``: this attempt's number. ``exhausted``: the attempts in 24 h.
    attempt: Optional[int] = None
    attempts_in_24h: int = 0
    until_wall: Optional[float] = None
    rearms_at_wall: Optional[float] = None
    last_attempt_wall: Optional[float] = None
    runs: Tuple[str, ...] = ()
    continuous: Tuple[str, ...] = ()
    vllm_model: Optional[str] = None
    vllm_state: Optional[str] = None
    vllm_error: Optional[str] = None
    gpu_max_permille: Optional[int] = None
    record_error: Optional[str] = None
    #: ``deferred``/``recheck``: what could not be read (D8 recovery text).
    recheck_reason: Optional[str] = None
    #: The recovery text a hold-off recorded (shown until it ends).
    recovery: Optional[str] = None

    @property
    def state(self) -> str:
        return _STATE_OF_DECISION.get(self.kind, self.kind)


IDLE_DECISION = SelfHealDecision(kind=DECISION_IDLE)


@dataclass(frozen=True)
class LoadHealth:
    model: str
    state: str  # "loading" | "stalled"
    loading_since_mono: float
    loading_since_wall: float
    last_progress_mono: float
    stub_count: int
    window_cpu_s: float
    gpu_max_permille: Optional[int]
    evidence: Tuple[str, ...] = ()
    recovery_action: Optional[str] = None
    self_heal_state: Optional[str] = None
    self_heal_until: Optional[str] = None
    culprit: bool = False
    tick_mono: float = 0.0

    def loading_for_s(self, now_mono: float) -> float:
        return max(0.0, now_mono - self.loading_since_mono)

    def no_progress_s(self, now_mono: float) -> float:
        return max(0.0, now_mono - self.last_progress_mono)


@dataclass(frozen=True)
class WatchdogView:
    """Everything the readers may see, published by one reference
    assignment at the end of every tick and at every decision change."""

    tick_mono: float
    tick_wall: float
    health: Mapping[str, LoadHealth]
    stalled: Tuple[str, ...]
    culprit: Optional[str]
    decision: SelfHealDecision
    switch: SwitchState
    recovery_action: Optional[str]
    gpu_node: Optional[str]
    state_since: Mapping[str, Tuple[str, float]]
    reasons: Mapping[str, str]
    boot_id: Optional[str]
    boottime: Optional[float]


def _frozen(mapping: Mapping) -> Mapping:
    return types.MappingProxyType(dict(mapping))


# ------------------------------------------------------- detection (pure)

def _rel(path: Optional[str], repo: Optional[str]) -> str:
    if not path:
        return "<unknown>"
    if repo:
        try:
            rel = os.path.relpath(path, repo)
            if not rel.startswith(".."):
                return rel
        except ValueError:
            pass
    return path


def _pb_stub_item(model: str, line: Any) -> Optional[Tuple[float, str]]:
    if not isinstance(line, Mapping):
        return None
    at = parse_iso(line.get("at"))
    text = line.get("line")
    if at is None or not isinstance(text, str):
        return None
    return at, '[pb_stub] "{0}" at {1} (captured by the {2} Startup from the backend container log)'.format(
        text, line.get("at"), model)


def evaluate_wedge(now_mono: float, now_wall: float, loading: Mapping[str, LoadingModel],
                   last_progress_mono: float, window_cpu_s: float, window_covered: bool,
                   entries: Mapping[str, Any], pb_stub_lines: Mapping[str, Any],
                   repo: Optional[str] = None) -> WedgeVerdict:
    """The wedge verdict (D6). Pure.

    ``loading`` maps each ``LOADING`` component model to its
    :class:`LoadingModel`; ``entries`` maps a model to its
    ``repo_entries.inspect_entry(..., quiet_period_s=0)`` result (or None);
    ``pb_stub_lines`` maps a model to its load outcome record's
    ``pbStubMissingFile``. A model is stalled when it is a candidate
    (``LOADING`` for at least :data:`STALL_AFTER_S`), the device-wide
    activity veto does not apply, and E1 or E2 holds.
    """
    no_progress = max(0.0, now_mono - last_progress_mono)
    veto = (not window_covered) or window_cpu_s >= ACTIVITY_CPU_THRESHOLD_S
    e2 = no_progress >= STALL_AFTER_S
    stubs_alive = sum(int(info.stubs or 0) for info in loading.values())
    verdicts: Dict[str, ModelVerdict] = {}
    for model in sorted(loading):
        info = loading[model]
        loading_for = max(0.0, now_mono - info.since_mono)
        candidate = loading_for >= STALL_AFTER_S
        evidence: List[str] = []
        e1 = False
        inspection = entries.get(model)
        if inspection is not None:
            missing = getattr(inspection, "missing_path", None)
            newest = getattr(inspection, "newest_mtime", None)
            if missing:
                e1 = True
                evidence.append("[repository] {0} was missing at {1}, after the load request at {2}".format(
                    _rel(missing, repo), iso(now_wall), iso(info.since_wall)))
            elif newest is not None and newest > info.since_wall + 1:
                e1 = True
                evidence.append("[repository] {0} changed at {1}, after the load request at {2}".format(
                    _rel(getattr(inspection, "newest_path", None), repo), iso(newest),
                    iso(info.since_wall)))
        if e2:
            evidence.append(
                "[stall] the Triton model index has not changed for {0} and the loading models' "
                "stub processes used {1:.1f} CPU-s in the last 5m ({2} stub process(es) alive)".format(
                    fmt_dur(no_progress), window_cpu_s, stubs_alive))
        stalled = candidate and not veto and (e1 or e2)
        item = _pb_stub_item(model, pb_stub_lines.get(model)) if stalled else None
        if item is not None and item[0] >= info.since_wall - 60:
            evidence.append(item[1])
        verdicts[model] = ModelVerdict(model=model, candidate=candidate, stalled=stalled,
                                       e1=e1, e2=e2,
                                       evidence=tuple(evidence) if stalled else (),
                                       loading_for_s=loading_for)
    stalled = tuple(sorted((m for m, v in verdicts.items() if v.stalled),
                           key=lambda m: (loading[m].since_mono, m)))
    culprit = None
    if stalled:
        with_e1 = [m for m in stalled if verdicts[m].e1]
        with_stub = [m for m in stalled if loading[m].stubs]
        culprit = (with_e1 or with_stub or list(stalled))[0]
    return WedgeVerdict(models=_frozen(verdicts), stalled=stalled, culprit=culprit, veto=veto,
                        window_cpu_s=float(window_cpu_s), window_covered=bool(window_covered),
                        no_progress_s=no_progress)


# ------------------------------------------------------------------ samplers

def find_gpu_load_node(sys_root: str = "/") -> Optional[str]:
    """The integrated Jetson GPU's devfreq ``load`` file, or None."""
    base = os.path.join(sys_root, "sys", "class", "devfreq")
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    for entry in entries:
        entry_path = os.path.join(base, entry)
        try:
            with open(os.path.join(entry_path, "device", "of_node", "name"), "rb") as handle:
                name = handle.read().decode("ascii", "replace").strip().strip("\x00").strip()
        except OSError:
            continue
        if name not in GPU_NODE_NAMES:
            continue
        load = os.path.join(os.path.realpath(os.path.join(entry_path, "device")), "load")
        try:
            with open(load, "rb") as handle:
                handle.read(64)
        except OSError:
            continue
        return load
    return None


def _read_gpu_value(path: str) -> Optional[int]:
    try:
        with open(path, "rb") as handle:
            text = handle.read(64).decode("ascii", "replace").strip()
        value = int(text)
    except (OSError, ValueError):
        logger.debug("GPU load read from %s failed or was malformed", path, exc_info=True)
        return None
    if 0 <= value <= 1000:
        return value
    logger.debug("GPU load %r from %s is out of range", value, path)
    return None


def read_gpu_burst(path: Optional[str], reads: int = GPU_SAMPLE_READS,
                   gap_s: float = GPU_SAMPLE_GAP_S,
                   sleep: Callable[[float], None] = time.sleep) -> Optional[int]:
    """The maximum of ``reads`` reads ``gap_s`` apart (per mille), or None
    when no read succeeded."""
    if not path:
        return None
    best = None
    for index in range(int(reads)):
        if index:
            sleep(gap_s)
        value = _read_gpu_value(path)
        if value is not None and (best is None or value > best):
            best = value
    return best


def sample_stubs(proc_root: str, repo: str) -> Dict[int, Tuple[str, float]]:
    """``{pid: (component, cpu_seconds)}`` for every python-backend stub
    whose model lies under ``repo``. Raises ``OSError`` when ``proc_root``
    cannot be listed; a pid that vanishes or cannot be parsed is skipped."""
    names = os.listdir(proc_root)
    try:
        ticks = float(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        ticks = 100.0
    prefix = os.path.join(repo, "")
    out: Dict[int, Tuple[str, float]] = {}
    for name in names:
        if not name.isdigit():
            continue
        pid_dir = os.path.join(proc_root, name)
        try:
            with open(os.path.join(pid_dir, "cmdline"), "rb") as handle:
                argv = handle.read().split(b"\0")
            if len(argv) < 2:
                continue
            if os.path.basename(argv[0].decode("utf-8", "replace")) != STUB_PROCESS_NAME:
                continue
            model_path = argv[1].decode("utf-8", "replace")
            if not model_path.startswith(prefix):
                continue
            step = model_path[len(prefix):].split("/", 1)[0]
            if not step:
                continue
            with open(os.path.join(pid_dir, "stat"), "rb") as handle:
                stat = handle.read().decode("ascii", "replace")
            fields = stat[stat.rindex(")") + 1:].split()
            # Fields 14 and 15 of /proc/<pid>/stat; field 3 is fields[0].
            cpu = (int(fields[11]) + int(fields[12])) / ticks
        except (OSError, ValueError, IndexError):
            logger.debug("Skipping /proc entry %s", name, exc_info=True)
            continue
        out[int(name)] = (component_of(step), cpu)
    return out


def window_cpu(samples: Sequence[Tuple[float, Mapping[int, Tuple[str, float]]]],
               components: Iterable[str]) -> float:
    """The CPU-seconds the stubs of ``components`` used across ``samples``."""
    wanted = set(components)
    total = 0.0
    for (_, before), (_, after) in zip(samples, list(samples)[1:]):
        for pid, (component, cpu) in after.items():
            if component not in wanted:
                continue
            previous = before.get(pid)
            total += max(0.0, cpu - (previous[1] if previous is not None else 0.0))
    return total


# --------------------------------------------------------- the switch (pure)

def parse_self_heal_switch(config: Mapping[str, Any]) -> Tuple[bool, Optional[str], bool]:
    """``(on, value, recognized)`` for a successful configuration read: a
    missing key and ``true``/``1``/``yes``/``on``/``enabled`` mean on;
    ``false``/``0``/``no``/``off``/``disabled`` mean off; anything else is
    off and unrecognized (an opt-out, never consent to a restart)."""
    if SWITCH_KEY not in config:
        return True, None, True
    raw = config.get(SWITCH_KEY)
    value = str(raw).strip()
    lowered = value.lower()
    if lowered in SWITCH_ON:
        return True, value, True
    if lowered in SWITCH_OFF:
        return False, value, True
    return False, value, False


# --------------------------------------------------------- the policy (pure)

def attempt_age_s(attempt: Mapping[str, Any], boot_id: Optional[str], boottime: Optional[float],
                  now_wall: float) -> float:
    """An attempt's age: by ``CLOCK_BOOTTIME`` within the same boot, else by
    the wall clock; a negative age counts as 0."""
    seconds = attempt.get("bootSeconds")
    if (boot_id and boottime is not None and attempt.get("bootId") == boot_id
            and isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
            and seconds >= 0):
        return max(0.0, float(boottime) - float(seconds))
    requested = parse_iso(attempt.get("requestedAt"))
    if requested is None:
        return 0.0
    return max(0.0, now_wall - requested)


def _ages(history: History, now_wall: float) -> List[Tuple[Mapping[str, Any], float]]:
    return [(a, attempt_age_s(a, history.boot_id, history.boottime, now_wall))
            for a in history.attempts]


def attempts_in_window(history: History, now_wall: float) -> List[Tuple[Mapping[str, Any], float]]:
    return [(a, age) for a, age in _ages(history, now_wall) if age < SELF_HEAL_WINDOW_S]


def rearms_at(history: History, now_wall: float) -> Optional[float]:
    """When fewer than the maximum attempts remain inside 24 h, or None
    when self-heal is not exhausted."""
    recent = sorted((age for _, age in attempts_in_window(history, now_wall)), reverse=True)
    if len(recent) < SELF_HEAL_MAX_PER_24H:
        return None
    return now_wall + SELF_HEAL_WINDOW_S - recent[len(recent) - SELF_HEAL_MAX_PER_24H]


def decide_self_heal(verdict: WedgeVerdict, switch: SwitchState, history: History,
                     activity: Optional[Any], gpu: GpuWindow, vllm: Optional[VllmView],
                     local: LocalHoldoffs, now_wall: float, now_mono: float) -> SelfHealDecision:
    """One self-heal decision (D7 rules 1-9, the first that matches). Pure.

    ``activity`` is a ``run_activity.ActivitySnapshot`` (None: treated as
    idle, as at the re-check before the commit); ``vllm`` the mapped vLLM
    states; ``local`` this process's hold-offs.
    """
    if not verdict.stalled:
        return IDLE_DECISION
    if not switch.enabled:
        return SelfHealDecision(kind=DECISION_DISABLED)
    if history.error is not None:
        return SelfHealDecision(kind=DECISION_BLOCKED_RECORD, record_error=history.error)
    recent = attempts_in_window(history, now_wall)
    if len(recent) >= SELF_HEAL_MAX_PER_24H:
        return SelfHealDecision(kind=DECISION_EXHAUSTED, attempt=len(recent),
                                attempts_in_24h=len(recent),
                                rearms_at_wall=rearms_at(history, now_wall))
    ages = _ages(history, now_wall)
    newest_age = min((age for _, age in ages), default=None)
    until = None
    last_wall = None
    if newest_age is not None and newest_age < SELF_HEAL_MIN_INTERVAL_S:
        until = now_wall + SELF_HEAL_MIN_INTERVAL_S - newest_age
        last_wall = now_wall - newest_age
    if local.aborted_at is not None and now_mono - local.aborted_at < SELF_HEAL_MIN_INTERVAL_S:
        local_until = now_wall + SELF_HEAL_MIN_INTERVAL_S - (now_mono - local.aborted_at)
        if until is None or local_until > until:
            until = local_until
            last_wall = now_wall - (now_mono - local.aborted_at)
    if until is not None:
        return SelfHealDecision(kind=DECISION_COOLDOWN, until_wall=until, last_attempt_wall=last_wall,
                                attempts_in_24h=len(recent))
    if vllm is not None:
        if vllm.error is not None:
            return SelfHealDecision(kind=DECISION_DEFERRED, deferred_by="vllm",
                                    vllm_error=vllm.error, attempts_in_24h=len(recent))
        for name in sorted(vllm.models):
            raw, mapped = vllm.models[name]
            if str(mapped).upper() == STATE_LOADING:
                return SelfHealDecision(kind=DECISION_DEFERRED, deferred_by="vllm", vllm_model=name,
                                        vllm_state=str(raw).upper(), attempts_in_24h=len(recent))
    if gpu.available and gpu.max_permille is not None and gpu.max_permille >= GPU_ACTIVITY_PERMILLE:
        return SelfHealDecision(kind=DECISION_DEFERRED, deferred_by="gpu",
                                gpu_max_permille=gpu.max_permille, attempts_in_24h=len(recent))
    runs: List[str] = []
    continuous: List[str] = []
    if activity is not None:
        continuous = run_activity_module.running_continuous(activity)
        labels_of_continuous = set(continuous)
        for info in activity.tickets:
            if run_activity_module.is_blocked(info, verdict.stalled):
                continue
            label = info.workflow or info.run_id
            if info.continuous and label in labels_of_continuous:
                continue
            runs.append(label)
    if runs or continuous:
        return SelfHealDecision(kind=DECISION_DEFERRED, deferred_by="runs", runs=tuple(runs),
                                continuous=tuple(continuous), attempts_in_24h=len(recent))
    if local.deferred_at is not None and now_mono - local.deferred_at < QUIESCE_RETRY_AFTER_S:
        return SelfHealDecision(kind=DECISION_DEFERRED, deferred_by=local.deferred_by or "runs",
                                until_wall=local.deferred_until_wall, recovery=local.deferred_recovery,
                                attempts_in_24h=len(recent))
    return SelfHealDecision(kind=DECISION_ACT, attempt=len(recent) + 1,
                            attempts_in_24h=len(recent))


def _switch_reason(switch: SwitchState) -> str:
    if switch.disabled_by == "file":
        return "file {0}".format(switch.kill_file or _kill_file_path(None))
    if switch.disabled_by == "config_unreadable":
        return "LocalServer configuration not read yet"
    return "{0}={1}".format(SWITCH_KEY, switch.value)


def _record_path(records_dir: Optional[str]) -> str:
    return os.path.join(records_dir or model_status_records.MODEL_STATUS_DIR,
                        model_status_records.SELF_HEAL_RECORD)


def _kill_file_path(records_dir: Optional[str]) -> str:
    return os.path.join(records_dir or model_status_records.MODEL_STATUS_DIR,
                        model_status_records.SELF_HEAL_DISABLED_FILE)


def recovery_action(decision: SelfHealDecision, culprit: Optional[str], switch: SwitchState,
                    records_dir: Optional[str] = None) -> Optional[str]:
    """The ``recoveryAction`` text of D8 for a decision."""
    kind = decision.kind
    if kind == DECISION_IDLE:
        return None
    if kind == DECISION_ACT:
        return ("Self-heal is restarting the LocalServer backend (attempt {0} of {1} in 24 h)."
                .format(decision.attempt, SELF_HEAL_MAX_PER_24H))
    if kind == DECISION_DEFERRED:
        if decision.recovery:
            return decision.recovery
        by = decision.deferred_by
        if by == "vllm":
            if decision.vllm_error is not None or not decision.vllm_model:
                return ("Self-heal will restart the LocalServer backend once the vLLM runtime can "
                        "list its models again.")
            return ("Self-heal will restart the LocalServer backend once the vLLM model {0} "
                    "finishes loading.".format(decision.vllm_model))
        if by == "gpu":
            return ("Self-heal will restart the LocalServer backend once the GPU shows no load for "
                    "5 min (it reached {0:.1f}% in the last 5 min, which can be a long model load "
                    "such as a TensorRT engine build). Restart the LocalServer backend manually to "
                    "clear the wedge now.".format((decision.gpu_max_permille or 0) / 10.0))
        if by == "recheck":
            return ("Self-heal will check the wedge again after {0}: immediately before restarting "
                    "it could not confirm the stall ({1}). Restart the LocalServer backend "
                    "manually to clear the wedge now.".format(
                        iso(decision.until_wall), decision.recheck_reason or "the check took too long"))
        return ("Self-heal will restart the LocalServer backend once no workflow run or capture "
                "task is in flight ({0} in flight: {1}) and no continuous workflow is running "
                "(running: {2}). Pause those workflows, or restart the LocalServer backend "
                "manually, to clear the wedge.".format(
                    len(decision.runs), ", ".join(decision.runs) or "none",
                    ", ".join(decision.continuous) or "none"))
    if kind == DECISION_COOLDOWN:
        return ("Self-heal will restart the LocalServer backend after {0} (at most one restart "
                "per 30 min).".format(iso(decision.until_wall)))
    if kind == DECISION_EXHAUSTED:
        return ("Self-heal is exhausted: it restarted the backend {0} times in the last 24 h. "
                "Restart the LocalServer backend manually and investigate {1}; self-heal re-arms "
                "at {2}.".format(decision.attempts_in_24h, culprit, iso(decision.rearms_at_wall)))
    if kind == DECISION_BLOCKED_RECORD:
        reason = "the record {0} {1}".format(_record_path(records_dir),
                                             decision.record_error or "is unreadable")
    else:
        if switch.disabled_by == "file":
            reason = "the kill file {0} exists".format(switch.kill_file or _kill_file_path(records_dir))
        elif switch.disabled_by == "config_unreadable":
            reason = "the LocalServer configuration has not been read yet"
        else:
            reason = "{0}={1}".format(SWITCH_KEY, switch.value)
    return ("Self-heal is off on this device ({0}). Restart the LocalServer backend: LocalServer "
            "UI, Application health overview, Application logs and restart; or on the device "
            "`sudo docker restart <backend container>`.".format(reason))


# ------------------------------------------------------------- the record

def validate_record(document: Any) -> Optional[str]:
    """None for a valid ``self_heal.json`` document, else the reason."""
    if not isinstance(document, dict):
        return "not a JSON object"
    if document.get("schema") != model_status_records.SCHEMA:
        return "schema is not {0}".format(model_status_records.SCHEMA)
    attempts = document.get("attempts", [])
    if not isinstance(attempts, list):
        return "attempts is not a list"
    if len(attempts) > SELF_HEAL_HISTORY_MAX:
        return "more than {0} attempts".format(SELF_HEAL_HISTORY_MAX)
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict):
            return "attempt {0} is not an object".format(index)
        if parse_iso(attempt.get("requestedAt")) is None:
            return "attempt {0} has no ISO-8601 UTC requestedAt".format(index)
        action = attempt.get("action", "backend_restart")
        if action not in ("backend_restart", "not_performed"):
            return "attempt {0} has an unknown action".format(index)
        runs = attempt.get("runsInterrupted")
        if runs is not None and (not isinstance(runs, list)
                                 or len(runs) > SELF_HEAL_RUNS_INTERRUPTED_MAX
                                 or not all(isinstance(r, dict) for r in runs)):
            return "attempt {0} has an invalid runsInterrupted".format(index)
    return None


def read_self_heal_record(records_dir: Optional[str] = None) -> Tuple[Optional[dict], Optional[str]]:
    """``(document, None)``, ``(None, None)`` when there is no record, or
    ``(None, error)`` when it is unreadable or invalid (rule 3)."""
    path = _record_path(records_dir)
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, "is unreadable ({0})".format(exc)
    if size > SELF_HEAL_RECORD_READ_MAX_BYTES:
        return None, "is unreadable (larger than {0} bytes)".format(SELF_HEAL_RECORD_READ_MAX_BYTES)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, "is unreadable ({0})".format(exc)
    problem = validate_record(document)
    if problem is not None:
        return None, "is unreadable ({0})".format(problem)
    return document, None


def serialize_record(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, indent=1).encode("utf-8")


def write_record(records_dir: Optional[str], document: Any) -> Optional[str]:
    """Write the record atomically; None on success, else the error."""
    path = _record_path(records_dir)
    directory = os.path.dirname(path)
    tmp = None
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory, mode=model_status_records.DIR_MODE, exist_ok=True)
            try:
                os.chmod(directory, model_status_records.DIR_MODE)
            except OSError:
                pass
        data = serialize_record(document)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-self-heal-")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, model_status_records.FILE_MODE)
        os.replace(tmp, path)
        tmp = None
        return None
    except Exception as exc:  # noqa: BLE001 - the caller decides
        return str(exc) or exc.__class__.__name__
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def prune_record(document: dict, boot_id: Optional[str], boottime: Optional[float],
                 now_wall: float) -> dict:
    """Keep the last :data:`SELF_HEAL_HISTORY_MAX` attempts, then drop the
    oldest attempt older than 24 h while the record is larger than
    :data:`SELF_HEAL_RECORD_WRITE_MAX_BYTES`."""
    attempts = list(document.get("attempts", []))[-SELF_HEAL_HISTORY_MAX:]
    document = dict(document, attempts=attempts)
    while len(serialize_record(document)) > SELF_HEAL_RECORD_WRITE_MAX_BYTES:
        old = [i for i, a in enumerate(attempts)
               if attempt_age_s(a, boot_id, boottime, now_wall) >= SELF_HEAL_WINDOW_S]
        if not old:
            break
        del attempts[old[0]]
        document = dict(document, attempts=attempts)
    return document


# ------------------------------------------------------------ default deps

_SWITCH_READER = None


def _read_localserver_config() -> Any:
    """LocalServer's live configuration through one module-level reader,
    created on first use and reused (as ``local_auth/config.py`` does)."""
    global _SWITCH_READER
    if _SWITCH_READER is None:
        from defect_detection_config.defect_detection_config import DefectDetectionConfig
        from utils.ipc_client import get_ipc_client

        _SWITCH_READER = DefectDetectionConfig(get_ipc_client())
    reader = _SWITCH_READER
    return reader.get_component_config(reader.get_local_server_component_name())


def _default_vllm_states(fcu: Any = None) -> Dict[str, Tuple[str, str]]:
    """``{name: (manager state, feature-config state)}``; empty without a
    vLLM runtime; raises when the manager cannot list its models."""
    if fcu is None:
        from utils import feature_configs_utils as fcu  # noqa: N813 - lazy, device-only deps
    manager = fcu.get_vllm_manager()
    if manager is None:
        return {}
    statuses = manager.list_models()
    out: Dict[str, Tuple[str, str]] = {}
    for name in sorted(statuses or {}):
        status = statuses[name]
        state = getattr(status, "state", status)
        raw = str(getattr(state, "value", state)).upper()
        out[str(name)] = (raw, str(fcu._vllm_feature_status(status)[0]).upper())
    return out


def _default_client():
    from dda_triton.triton_edge_client import TritonEdgeClient

    return TritonEdgeClient.get_instance()


def _default_repo_has_models() -> bool:
    from utils.feature_configs_utils import triton_repo_has_models

    return bool(triton_repo_has_models())


def _default_shadow_reader(thing_name: str, shadow_name: str) -> Any:
    from utils import server_setup

    return server_setup.iot_shadow_accessor.get_thing_shadow_state_request(thing_name, shadow_name)


def _default_boottime() -> Optional[float]:
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return None


def _read_boot_id(path: str = BOOT_ID_PATH) -> Optional[str]:
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value[:64] or None


# ------------------------------------------------------------ the switch

class _Switch:
    """The switch reader's state (D7, "The reader"): runs on the watchdog
    thread only."""

    def __init__(self, reader: Callable[[], Any], clock: Callable[[], float],
                 kill_file: str):
        self._reader = reader
        self._clock = clock
        self.kill_file = kill_file
        self.read = False
        self.on = False
        self.value: Optional[str] = None
        self.recognized = True
        self._last_success: Optional[float] = None
        self._last_failure: Optional[float] = None
        self._failing = False
        self._warned_values = set()

    def refresh(self, force: bool = False) -> None:
        now = self._clock()
        if not force:
            if self._last_failure is not None and (self._last_success is None
                                                   or self._last_failure > self._last_success):
                if now - self._last_failure < SWITCH_RETRY_AFTER_S:
                    return
            elif self._last_success is not None and now - self._last_success < SWITCH_REFRESH_S:
                return
        try:
            config = self._reader()
            error = None
            if not isinstance(config, dict):
                error = "the configuration read returned {0}".format(type(config).__name__)
            elif not config:
                error = "the configuration read returned an empty configuration"
            elif SWITCH_SUCCESS_KEY not in config:
                error = "the configuration has no {0}".format(SWITCH_SUCCESS_KEY)
        except Exception as exc:  # noqa: BLE001 - a failed read
            config, error = None, str(exc) or exc.__class__.__name__
        if error is not None:
            self._last_failure = self._clock()
            if not self._failing:
                self._failing = True
                if self.read:
                    tail = "using the last value read ({0})".format(
                        self.value if self.value is not None else "not set, on by default")
                else:
                    tail = "self-heal stays off until a read succeeds"
                logger.warning("Self-heal configuration could not be read (%s); %s", error, tail)
            return
        self._failing = False
        self._last_success = self._clock()
        self.read = True
        self.on, self.value, self.recognized = parse_self_heal_switch(config)
        if not self.recognized and self.value not in self._warned_values:
            self._warned_values.add(self.value)
            logger.warning("%s has an unrecognized value '%s'; self-heal is off (treated as an "
                           "opt-out)", SWITCH_KEY, self.value)

    def kill_file_exists(self) -> bool:
        try:
            return os.path.lexists(self.kill_file)
        except Exception:  # noqa: BLE001 - an unreadable path counts as present
            return True

    def state(self) -> SwitchState:
        if self.kill_file_exists():
            return SwitchState(False, "file", self.value, self.read, self.kill_file)
        if not self.read:
            return SwitchState(False, "config_unreadable", None, False, self.kill_file)
        if not self.on:
            return SwitchState(False, "config", self.value, True, self.kill_file)
        return SwitchState(True, None, self.value, True, self.kill_file)


def _switch_start_text(switch: SwitchState) -> str:
    if switch.disabled_by == "file":
        return "off (file {0})".format(switch.kill_file)
    if switch.disabled_by == "config_unreadable":
        return "off until the LocalServer configuration is read"
    if switch.disabled_by == "config":
        return "off ({0}={1})".format(SWITCH_KEY, switch.value)
    if switch.value is None:
        return "on (default)"
    return "on ({0}={1})".format(SWITCH_KEY, switch.value)


# -------------------------------------------------------------- the watchdog

@dataclass
class _Observation:
    """One reading of the index and the samplers (a tick or the re-check)."""

    now_mono: float
    now_wall: float
    index: Dict[str, Tuple[str, Optional[str]]]
    statuses: Dict[str, str]
    loading: Dict[str, LoadingModel]
    window_cpu_s: float
    window_covered: bool
    gpu: GpuWindow
    verdict: WedgeVerdict


class _Interrupted(Exception):
    """A re-check read failed (the stall cannot be confirmed)."""

    def __init__(self, reason: str, recovery_reason: str):
        super().__init__(reason)
        self.reason = reason
        self.recovery_reason = recovery_reason


class LoadWatchdog:
    """Detection, self-heal and the surfaces' published view (D6-D8)."""

    def __init__(self, client_provider: Callable[[], Any] = _default_client,
                 repo_has_models: Callable[[], bool] = _default_repo_has_models,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 proc_root: str = "/proc", sys_root: str = "/",
                 config_reader: Callable[[], Any] = _read_localserver_config,
                 vllm_states: Callable[[], Mapping[str, Tuple[str, str]]] = _default_vllm_states,
                 run_activity: Any = run_activity_module,
                 shadow_reader: Callable[[str, str], Any] = _default_shadow_reader,
                 killer: Callable[[int, int], None] = os.kill,
                 exiter: Callable[[int], None] = os._exit,
                 records_dir: Optional[str] = None,
                 repo_dir: str = TRITON_MODEL_DIR,
                 sleep: Callable[[float], None] = time.sleep,
                 boot_id: Optional[Callable[[], Optional[str]]] = None,
                 boottime: Callable[[], Optional[float]] = _default_boottime,
                 tracker: Any = None,
                 reporter: Optional[Callable[[dict], Any]] = None,
                 size_rejection: Optional[Callable[[], bool]] = None,
                 backstop_sleep: Callable[[float], None] = time.sleep):
        if tracker is None:
            from dda_triton import load_tracker as tracker  # noqa: N813
        self._client_provider = client_provider
        self._repo_has_models = repo_has_models
        self._clock = clock
        self._wall = wall
        self._proc_root = proc_root
        self._sys_root = sys_root
        self._vllm_states = vllm_states
        self._run_activity = run_activity
        self._shadow_reader = shadow_reader
        self._killer = killer
        self._exiter = exiter
        self.records_dir = records_dir or model_status_records.MODEL_STATUS_DIR
        self.repo_dir = repo_dir
        self._sleep = sleep
        self._boot_id_reader = boot_id or _read_boot_id
        self._boottime = boottime
        self._tracker = tracker
        self._reporter = reporter or model_status_shadow.report
        self._size_rejection = size_rejection or model_status_shadow.take_size_rejection
        self._backstop_sleep = backstop_sleep
        self.record_path = _record_path(self.records_dir)
        self._switch = _Switch(config_reader, clock, _kill_file_path(self.records_dir))

        self.view: Optional[WatchdogView] = None
        self.retirements: Tuple[str, ...] = ()
        self.started = False
        self.signalled = False
        self.gpu_node: Optional[str] = None
        self.boot_id: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._tick_failing = False
        self._index_failing = False
        # Detection state (watchdog thread only).
        start = clock()
        self._last_progress = start
        self._prev_signature = None
        self._first_loading: Dict[str, Tuple[float, float]] = {}
        self._last_outside: Dict[str, Tuple[float, float]] = {}
        self._state_since: Dict[str, Tuple[str, float]] = {}
        self._reasons: Dict[str, str] = {}
        self._cpu_samples: collections.deque = collections.deque()
        self._gpu_samples: collections.deque = collections.deque()
        self._last_obs: Optional[_Observation] = None
        # Self-heal state (watchdog thread only).
        self._local = LocalHoldoffs()
        self._episode = False
        self._episode_logged: set = set()
        self._stalled_logged: set = set()
        self._deferral_logged_at: Dict[str, float] = {}
        self._exhausted_logged_at: Optional[float] = None
        self._decision: SelfHealDecision = IDLE_DECISION
        # Reporting state.
        self._last_report_signature: Optional[str] = None
        self._last_report_mono: Optional[float] = None

    # -- lifecycle ---------------------------------------------------------------

    def startup(self) -> None:
        """Close out the record, find the GPU node and read the switch once
        (for the start line). Idempotent."""
        if self.started:
            return
        self.started = True
        self.boot_id = self._safe(self._boot_id_reader, None)
        self._close_out()
        try:
            self.gpu_node = find_gpu_load_node(self._sys_root)
        except Exception:  # noqa: BLE001
            self.gpu_node = None
        self._switch.refresh(force=True)
        logger.info(
            "Triton model-load watchdog started (self-heal %s; GPU load signal %s; stall after "
            "15m, at most 3 restarts per 24h and 30m apart)",
            _switch_start_text(self._switch.state()), self.gpu_node or "unavailable")

    def start(self) -> "LoadWatchdog":
        self.startup()
        thread = threading.Thread(target=self._run, name="triton-load-watchdog", daemon=True)
        thread.start()
        self._thread = thread
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set() and not self.signalled:
            self.contained_tick()
            if self.signalled:
                return
            self._sleep(WATCHDOG_INTERVAL_S)

    def contained_tick(self) -> bool:
        """One tick inside the thread's containment (one ERROR per streak)."""
        try:
            self.tick()
        except Exception as exc:  # noqa: BLE001 - the next tick runs in 30 s
            if not self._tick_failing:
                logger.error(
                    "Triton model-load watchdog tick failed (%s); detection and self-heal resume "
                    "at the next tick in %ds", exc, WATCHDOG_INTERVAL_S, exc_info=True)
            else:
                logger.debug("Triton model-load watchdog tick failed again", exc_info=True)
            self._tick_failing = True
            return False
        self._tick_failing = False
        return True

    @staticmethod
    def _safe(fn, default):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return default

    # -- the tick ------------------------------------------------------------------

    def tick(self) -> None:
        self.startup()
        if self.signalled:
            return
        now = self._clock()
        self._hygiene()
        self._switch.refresh(force=False)
        if not self._repo_has_models():
            self._reset_tracking(now)
            self._decide_and_publish(None, now)
            self._check_size_rejection(set())
            self._maybe_report({}, now)
            return
        client = self._client_provider()
        try:
            index = client.index_states()
        except Exception as exc:  # noqa: BLE001 - the tick is skipped
            if not self._index_failing:
                self._index_failing = True
                logger.warning("Triton model-load watchdog could not read the Triton model index "
                               "(%s); skipping this tick", exc)
            if self.view is not None:
                self.view = replace(self.view, tick_mono=self._clock(), tick_wall=self._wall())
            return
        self._index_failing = False
        obs = self._observe(index)
        self._resolve_load_outcomes(obs)
        self._decide_and_publish(obs, now)
        if self._decision.kind == DECISION_ACT and not self.signalled:
            self._act(obs)
        self._check_size_rejection(set(index))
        self._maybe_report(obs.statuses, now)

    def _reset_tracking(self, now: float) -> None:
        self._prev_signature = None
        self._last_progress = now
        self._first_loading.clear()
        self._last_outside.clear()
        self._state_since.clear()
        self._reasons.clear()
        self._cpu_samples.clear()
        self._gpu_samples.clear()
        self._stalled_logged.clear()
        self._last_obs = None

    def _hygiene(self) -> None:
        try:
            before = {}
            try:
                for name in os.listdir(self.repo_dir):
                    if name.startswith((".staging-", ".trash-")):
                        try:
                            before[name] = os.path.getmtime(os.path.join(self.repo_dir, name))
                        except OSError:
                            pass
            except OSError:
                pass
            stamp = repo_entries.boot_stamp()
            removed = repo_entries.sweep_stale_hidden_dirs(self.repo_dir)
            for name in removed:
                self._log_removed(name, stamp, before.get(name))
        except Exception:  # noqa: BLE001 - hygiene never fails a tick
            logger.debug("Hidden-directory sweep failed", exc_info=True)
        try:
            if self._tracker.inflight():
                self._tracker.ensure_tracker_thread()
        except Exception:  # noqa: BLE001
            logger.debug("Could not check the load tracker thread", exc_info=True)

    def _log_removed(self, name: str, stamp: Optional[str], mtime: Optional[float]) -> None:
        staging = name.startswith(".staging-")
        pattern = repo_entries._STAMPED_STAGING if staging else repo_entries._STAMPED_TRASH
        match = pattern.match(name)
        age = None
        if match is not None and stamp:
            boot, _, seconds = stamp.partition("-")
            if match.group("boot") != boot:
                age = "from an earlier boot"
            else:
                try:
                    age = "{0} old".format(fmt_dur(int(seconds) - int(match.group("bt"))))
                except ValueError:
                    age = None
        if age is None and mtime is not None:
            age = "{0} old".format(fmt_dur(self._wall() - mtime))
        age = age or "stale"
        if staging:
            logger.info("Removed stale staging directory %s (%s; left by an interrupted publish)",
                        name, age)
        else:
            logger.info("Removed stale trash directory %s (%s)", name, age)

    def _observe(self, index: Mapping[str, Any]) -> _Observation:
        now = self._clock()
        now_wall = self._wall()
        states: Dict[str, Tuple[str, Optional[str]]] = {}
        for name, value in dict(index).items():
            if isinstance(value, (tuple, list)):
                state, reason = (list(value) + [None, None])[:2]
            else:
                state, reason = value, None
            states[str(name)] = (str(state or STATE_UNKNOWN).strip().upper() or STATE_UNKNOWN,
                                 reason if reason else None)
        # Step 4: load progress.
        signature = frozenset((n, s, r) for n, (s, r) in states.items()
                              if s != STATE_LOADING and not n.startswith("."))
        if self._prev_signature is not None and signature - self._prev_signature:
            self._last_progress = now
        self._prev_signature = signature
        # Step 5: the loading set and the sightings.
        statuses = {n: s for n, (s, _r) in states.items() if is_component(n)}
        for name, (state, reason) in states.items():
            if not is_component(name):
                continue
            previous = self._state_since.get(name)
            if previous is None or previous[0] != state:
                self._state_since[name] = (state, now_wall)
            if reason:
                self._reasons[name] = str(reason)
            else:
                self._reasons.pop(name, None)
        for name in list(self._state_since):
            if name not in statuses:
                del self._state_since[name]
                self._reasons.pop(name, None)
        loading_names = sorted(n for n, s in statuses.items() if s == STATE_LOADING)
        for name in loading_names:
            self._first_loading.setdefault(name, (now, now_wall))
        for name in list(self._first_loading):
            if name not in loading_names:
                del self._first_loading[name]
                self._last_outside[name] = (now, now_wall)
                if name in self._stalled_logged:
                    self._stalled_logged.discard(name)
                    logger.info("Triton model %s is no longer stalled (state %s)", name,
                                statuses.get(name, "absent from the index"))
        for name in statuses:
            if name not in loading_names:
                self._last_outside[name] = (now, now_wall)
        since: Dict[str, Tuple[float, float]] = {}
        for name in loading_names:
            requested = None
            try:
                requested = self._tracker.requested_at(name)
            except Exception:  # noqa: BLE001
                requested = None
            outside = self._last_outside.get(name)
            if requested is not None and (outside is None or requested[1] > outside[0]):
                since[name] = (float(requested[1]), float(requested[0]))
            else:
                since[name] = self._first_loading[name]
        # Step 6: activity.
        sampling = any(now - mono >= ACTIVITY_SAMPLE_AFTER_S for mono, _w in since.values())
        stubs_of: Dict[str, int] = collections.Counter()
        if sampling:
            try:
                sample = sample_stubs(self._proc_root, self.repo_dir)
                self._cpu_samples.append((now, sample))
                while (len(self._cpu_samples) >= 2
                       and self._cpu_samples[1][0] <= now - ACTIVITY_WINDOW_S):
                    self._cpu_samples.popleft()
                for _pid, (component, _cpu) in sample.items():
                    stubs_of[component] += 1
            except Exception:  # noqa: BLE001 - activity unknown, so nothing is stalled
                logger.debug("Could not sample the stub processes under %s", self._proc_root,
                             exc_info=True)
                self._cpu_samples.clear()
            if self.gpu_node:
                value = None
                try:
                    value = read_gpu_burst(self.gpu_node, sleep=self._sleep)
                except Exception:  # noqa: BLE001
                    value = None
                if value is not None:
                    self._gpu_samples.append((now, value))
        else:
            self._cpu_samples.clear()
        while self._gpu_samples and self._gpu_samples[0][0] < now - ACTIVITY_WINDOW_S:
            self._gpu_samples.popleft()
        covered = bool(self._cpu_samples) and now - self._cpu_samples[0][0] >= ACTIVITY_WINDOW_S
        cpu = window_cpu(list(self._cpu_samples), loading_names) if covered else 0.0
        gpu = GpuWindow(available=bool(self.gpu_node and self._gpu_samples),
                        max_permille=max((v for _t, v in self._gpu_samples), default=None))
        loading = {name: LoadingModel(since[name][0], since[name][1], stubs_of.get(name, 0))
                   for name in loading_names}
        # Step 8 (detection part): the verdict.
        entries: Dict[str, Any] = {}
        pb_stub: Dict[str, Any] = {}
        records = None
        for name in loading_names:
            if now - loading[name].since_mono < STALL_AFTER_S:
                continue
            try:
                entries[name] = repo_entries.inspect_entry(self.repo_dir, name, wall=self._wall,
                                                           quiet_period_s=0)
            except Exception:  # noqa: BLE001
                entries[name] = None
            if records is None:
                records = model_status_records.read_load_outcomes(self.records_dir)
            record = records.get(name) or {}
            if record.get("pbStubMissingFile"):
                pb_stub[name] = record["pbStubMissingFile"]
        verdict = evaluate_wedge(now, now_wall, loading, self._last_progress, cpu, covered,
                                 entries, pb_stub, repo=self.repo_dir)
        obs = _Observation(now_mono=now, now_wall=now_wall, index=states, statuses=statuses,
                           loading=loading, window_cpu_s=cpu, window_covered=covered, gpu=gpu,
                           verdict=verdict)
        self._last_obs = obs
        return obs

    def _resolve_load_outcomes(self, obs: _Observation) -> None:
        ready = [n for n, s in obs.statuses.items() if s == STATE_READY]
        if not ready:
            return
        try:
            records = model_status_records.read_load_outcomes(self.records_dir)
        except Exception:  # noqa: BLE001
            return
        for name in ready:
            if name in records:
                path = model_status_records.load_outcome_path(name, self.records_dir)
                if model_status_records.clear_load_outcome(name, self.records_dir):
                    logger.info("Load outcome for %s resolved: the model is READY; removed %s",
                                name, path)

    # -- deciding ----------------------------------------------------------------

    def _history(self) -> History:
        document, error = read_self_heal_record(self.records_dir)
        attempts = tuple((document or {}).get("attempts", []) or ())
        return History(attempts=attempts, error=error, boot_id=self.boot_id,
                       boottime=self._safe(self._boottime, None))

    def _vllm_view(self) -> VllmView:
        try:
            models = self._vllm_states() or {}
            return VllmView(models=_frozen({str(k): tuple(v) for k, v in dict(models).items()}))
        except Exception as exc:  # noqa: BLE001 - a raising list defers (rule 6)
            return VllmView(error=str(exc) or exc.__class__.__name__)

    def _snapshot_activity(self):
        try:
            return self._run_activity.snapshot()
        except Exception:  # noqa: BLE001 - unknown activity defers
            logger.exception("Run activity snapshot failed; treating a run as in flight")
            return run_activity_module.ActivitySnapshot(
                tickets=(run_activity_module.TicketInfo(
                    kind="unknown", run_id="unknown", models=(), workflow="unknown",
                    started_at=0.0, in_gate=False, gate_only=False, outside_gate_since=0.0,
                    outside_gate_for_s=0.0, has_callback=False, continuous=False),),
                continuous_starts={}, now=0.0, state="unknown")

    def decide(self, verdict: WedgeVerdict, activity: Any = "snapshot",
               switch: Optional[SwitchState] = None) -> SelfHealDecision:
        now_mono, now_wall = self._clock(), self._wall()
        switch = switch or self._switch.state()
        if not verdict.stalled:
            return IDLE_DECISION
        history = self._history()
        vllm = self._vllm_view()
        if activity == "snapshot":
            activity = self._snapshot_activity()
        return decide_self_heal(verdict, switch, history, activity, self._last_obs.gpu
                                if self._last_obs else GpuWindow(False), vllm, self._local,
                                now_wall, now_mono)

    def _decide_and_publish(self, obs: Optional[_Observation], now: float) -> None:
        if obs is None:
            verdict = evaluate_wedge(now, self._wall(), {}, self._last_progress, 0.0, False, {}, {})
        else:
            verdict = obs.verdict
        decision = self.decide(verdict)
        if not verdict.stalled:
            if self._episode:
                self._episode = False
                self._episode_logged.clear()
                self._deferral_logged_at.clear()
                self._exhausted_logged_at = None
        else:
            self._episode = True
        self._decision = decision
        self._publish(decision)
        self._log_decision(decision, verdict, obs)

    def _publish(self, decision: SelfHealDecision) -> None:
        obs = self._last_obs
        switch = self._switch.state()
        now = self._clock()
        culprit = obs.verdict.culprit if obs else None
        recovery = recovery_action(decision, culprit, switch, self.records_dir)
        health: Dict[str, LoadHealth] = {}
        if obs is not None:
            until = decision.until_wall
            if decision.kind == DECISION_EXHAUSTED:
                until = decision.rearms_at_wall
            for name, info in obs.loading.items():
                verdict = obs.verdict.models.get(name)
                stalled = bool(verdict and verdict.stalled)
                health[name] = LoadHealth(
                    model=name, state="stalled" if stalled else "loading",
                    loading_since_mono=info.since_mono, loading_since_wall=info.since_wall,
                    last_progress_mono=self._last_progress, stub_count=info.stubs,
                    window_cpu_s=obs.window_cpu_s,
                    gpu_max_permille=obs.gpu.max_permille if obs.gpu.available else None,
                    evidence=verdict.evidence if stalled else (),
                    recovery_action=recovery if stalled else None,
                    self_heal_state=decision.state if stalled else None,
                    self_heal_until=iso(until) if stalled else None,
                    culprit=name == culprit, tick_mono=now)
        self.view = WatchdogView(
            tick_mono=now, tick_wall=self._wall(), health=_frozen(health),
            stalled=obs.verdict.stalled if obs else (), culprit=culprit, decision=decision,
            switch=switch, recovery_action=recovery, gpu_node=self.gpu_node,
            state_since=_frozen(self._state_since), reasons=_frozen(self._reasons),
            boot_id=self.boot_id, boottime=self._safe(self._boottime, None))

    def _loading_text(self, verdict: WedgeVerdict, obs: Optional[_Observation]) -> str:
        if obs is None or not verdict.stalled:
            return "none"
        order = sorted(verdict.stalled, key=lambda m: (m != verdict.culprit,
                                                       obs.loading[m].since_mono, m))
        parts = []
        for name in order:
            text = "{0} for {1}".format(name, fmt_dur(obs.now_mono - obs.loading[name].since_mono))
            if name == verdict.culprit:
                text += " (suspected culprit)"
            parts.append(text)
        return ", ".join(parts)

    def _evidence_text(self, verdict: WedgeVerdict) -> List[str]:
        items: List[str] = []
        order = sorted(verdict.stalled, key=lambda m: m != verdict.culprit)
        for name in order:
            for item in verdict.models[name].evidence:
                if item not in items:
                    items.append(item)
        return items

    def _log_decision(self, decision: SelfHealDecision, verdict: WedgeVerdict,
                      obs: Optional[_Observation]) -> None:
        now = self._clock()
        if obs is not None:
            recovery = self.view.recovery_action if self.view else None
            for name in verdict.stalled:
                if name in self._stalled_logged:
                    continue
                self._stalled_logged.add(name)
                info = obs.loading[name]
                logger.warning(
                    "Triton model %s is stalled: LOADING for %s with no load progress for %s; "
                    "evidence: %s. Recovery: %s", name, fmt_dur(now - info.since_mono),
                    fmt_dur(now - self._last_progress), "; ".join(verdict.models[name].evidence),
                    recovery)
        if not verdict.stalled:
            return
        kind = decision.kind
        loading = self._loading_text(verdict, obs)
        evidence = "; ".join(self._evidence_text(verdict))
        if kind == DECISION_DISABLED:
            if "disabled" not in self._episode_logged:
                self._episode_logged.add("disabled")
                logger.warning(
                    "Triton model load wedge detected (models LOADING: %s; evidence: %s); self-heal "
                    "is off on this device (%s). Restart the LocalServer backend manually.",
                    loading, evidence, _switch_reason(self.view.switch))
        elif kind == DECISION_BLOCKED_RECORD:
            if "blocked_record" not in self._episode_logged:
                self._episode_logged.add("blocked_record")
                self._log_record_error(decision.record_error)
        elif kind == DECISION_EXHAUSTED:
            text = (
                "SELF-HEAL EXHAUSTED: the Triton model load is wedged (models LOADING: {0}) but "
                "self-heal already restarted the backend {1} times in the last 24h; not "
                "restarting. Evidence: {2}. Restart the LocalServer backend manually and "
                "investigate {3}; self-heal re-arms at {4}. Record: {5}".format(
                    loading, decision.attempts_in_24h, evidence, verdict.culprit,
                    iso(decision.rearms_at_wall), self.record_path))
            if "exhausted" not in self._episode_logged:
                self._episode_logged.add("exhausted")
                self._exhausted_logged_at = now
                logger.error(text)
            elif (self._exhausted_logged_at is None
                  or now - self._exhausted_logged_at >= EXHAUSTED_LOG_EVERY_S):
                self._exhausted_logged_at = now
                logger.warning(text)
        elif kind == DECISION_COOLDOWN:
            if "cooldown" not in self._episode_logged:
                self._episode_logged.add("cooldown")
                logger.warning(
                    "SELF-HEAL waiting: the Triton model load is wedged (models LOADING: %s); the "
                    "last self-heal restart was at %s and self-heal restarts at most once per 30m; "
                    "next possible restart at %s", loading, iso(decision.last_attempt_wall),
                    iso(decision.until_wall))
        elif kind == DECISION_DEFERRED and decision.recovery is None:
            key = decision.deferred_by or "runs"
            last = self._deferral_logged_at.get(key)
            if last is not None and now - last < DEFERRAL_LOG_EVERY_S:
                return
            self._deferral_logged_at[key] = now
            if key == "vllm" and decision.vllm_error is not None:
                logger.warning(
                    "SELF-HEAL deferred: the Triton model load is wedged (models LOADING: %s); "
                    "waiting because the vLLM runtime could not list its models (%s)", loading,
                    decision.vllm_error)
            elif key == "vllm":
                logger.warning(
                    "SELF-HEAL deferred: the Triton model load is wedged (models LOADING: %s); "
                    "waiting because the vLLM model %s is %s", loading, decision.vllm_model,
                    decision.vllm_state)
            elif key == "gpu":
                logger.warning(
                    "SELF-HEAL deferred: the Triton model load is wedged (models LOADING: %s); "
                    "waiting because the GPU was busy in the last 5m (up to %.1f%%), which can be a "
                    "long model load such as a TensorRT engine build. Restart the LocalServer "
                    "backend manually to clear the wedge now. Re-checking every 30s", loading,
                    (decision.gpu_max_permille or 0) / 10.0)
            elif key == "runs":
                logger.warning(
                    "SELF-HEAL deferred: the Triton model load is wedged (models LOADING: %s); "
                    "waiting because %d workflow run(s) or capture task(s) not blocked by the wedge "
                    "are in flight (%s) and continuous workflow(s) %s are running on READY models. "
                    "Pause those workflows, or restart the LocalServer backend manually, to clear "
                    "the wedge. Re-checking every 30s", loading, len(decision.runs),
                    ", ".join(decision.runs) or "none", ", ".join(decision.continuous) or "none")

    def _log_record_error(self, error: Optional[str]) -> None:
        error = error or "is unreadable"
        if error.startswith("cannot be written"):
            logger.error("SELF-HEAL off: the record %s cannot be written (%s); fix the directory or "
                         "delete the file to re-enable self-heal", self.record_path,
                         error[len("cannot be written ("):-1] or error)
        else:
            detail = error[len("is unreadable ("):-1] if error.startswith("is unreadable (") else error
            logger.error("SELF-HEAL off: the record %s is unreadable (%s); delete or repair it to "
                         "re-enable self-heal", self.record_path, detail)

    # -- the action (D7, steps 1-11) ----------------------------------------------

    def _is_blocked(self, info) -> bool:
        stalled = self._last_obs.verdict.stalled if self._last_obs else ()
        return run_activity_module.is_blocked(info, stalled)

    def _unblocked_labels(self) -> List[str]:
        snapshot = self._run_activity.snapshot()
        return [t.workflow or t.run_id for t in snapshot.tickets if not self._is_blocked(t)]

    def _set_decision(self, decision: SelfHealDecision) -> None:
        self._decision = decision
        self._publish(decision)

    def _act(self, obs: _Observation) -> None:
        verdict = obs.verdict
        # Step 1: the switch, fresh.
        self._switch.refresh(force=True)
        switch = self._switch.state()
        if not switch.enabled:
            self._set_decision(SelfHealDecision(kind=DECISION_DISABLED))
            self._log_decision(self._decision, verdict, obs)
            return
        # Step 2: probe the record.
        document, error = read_self_heal_record(self.records_dir)
        if error is not None:
            decision = SelfHealDecision(kind=DECISION_BLOCKED_RECORD, record_error=error)
            self._set_decision(decision)
            self._log_decision(decision, verdict, obs)
            return
        write_error = write_record(self.records_dir, document if document is not None
                                   else {"schema": model_status_records.SCHEMA, "attempts": []})
        if write_error is not None:
            decision = SelfHealDecision(kind=DECISION_BLOCKED_RECORD,
                                        record_error="cannot be written ({0})".format(write_error))
            self._set_decision(decision)
            self._log_decision(decision, verdict, obs)
            return
        attempt_no = self._decision.attempt or 1
        refusal = ("The LocalServer backend is restarting to clear a wedged Triton model load "
                   "(self-heal attempt {0}); run the workflow again in a few minutes.".format(attempt_no))
        # Step 3: quiesce.
        if not self._run_activity.begin_quiesce():
            state = self._safe(self._run_activity.state, "unknown")
            logger.warning("SELF-HEAL: run activity is %s, not IDLE; not quiescing, and self-heal "
                           "waits 30m before it acts again", state)
            self._abort_holdoff()
            return
        outcome, step = None, "the quiesce"
        try:
            unblocked = self._unblocked_labels()
            logger.info(
                "SELF-HEAL quiescing: the Triton model load is wedged (models LOADING: %s); pausing "
                "continuous workflows and waiting up to %ds for %d run(s) not blocked by the wedge "
                "to finish; new workflow runs count as in flight",
                self._loading_text(verdict, obs), QUIESCE_DRAIN_S, len(unblocked))
            step = "the drain"
            remaining = self._drain()
            if remaining:
                self._defer_after_quiesce(remaining, "still in flight after 60s")
                return
            step = "the re-check"
            decision, reason = self._recheck()
            if decision.kind != DECISION_ACT:
                self._defer_at_commit(decision, reason)
                return
            attempt_no = decision.attempt or attempt_no
            refusal = ("The LocalServer backend is restarting to clear a wedged Triton model load "
                       "(self-heal attempt {0}); run the workflow again in a few minutes."
                       .format(attempt_no))
            step = "the commit"
            outcome = self._run_activity.commit_restart(self._is_blocked, refusal)
            if outcome == "runs":
                self._defer_after_quiesce(self._unblocked_labels(), "in flight at the commit")
                return
            if outcome != "committed":
                self._defer_at_commit(self._recheck_decision("the check took too long"),
                                      "the quiesce expired after 120s before the commit")
                return
        except Exception as exc:  # noqa: BLE001 - no action; continuous workflows resume
            logger.error(
                "SELF-HEAL not performed: %s failed before the commit (%s); continuous workflows "
                "resume, and self-heal waits 30m before it acts again. Restart the LocalServer "
                "backend manually to clear the wedge.", step, exc, exc_info=True)
            self._abort_holdoff()
            return
        finally:
            if outcome != "committed":
                self._run_activity.end_quiesce()
        # RESTARTING from here.
        self._restart(attempt_no)

    def _abort_holdoff(self) -> None:
        """Rule 5 after an aborted attempt: no new attempt from this process
        for 30 min; publishes the ``cooldown`` decision. Never raises."""
        try:
            now_wall = self._wall()
            self._local = replace(self._local, aborted_at=self._clock(), aborted_at_wall=now_wall)
            self._set_decision(SelfHealDecision(
                kind=DECISION_COOLDOWN, until_wall=now_wall + SELF_HEAL_MIN_INTERVAL_S,
                last_attempt_wall=now_wall, attempts_in_24h=self._decision.attempts_in_24h))
        except Exception:  # noqa: BLE001
            logger.debug("Could not record the aborted attempt", exc_info=True)

    def _drain(self) -> List[str]:
        deadline = self._clock() + QUIESCE_DRAIN_S
        while True:
            remaining = self._unblocked_labels()
            if not remaining or self._clock() >= deadline:
                return remaining
            self._sleep(QUIESCE_DRAIN_POLL_S)

    def _recheck_decision(self, recovery_reason: str) -> SelfHealDecision:
        until = self._wall() + QUIESCE_RETRY_AFTER_S
        return SelfHealDecision(kind=DECISION_DEFERRED, deferred_by="recheck",
                                recheck_reason=recovery_reason, until_wall=until)

    def _recheck(self) -> Tuple[SelfHealDecision, str]:
        """D7 step 5: the decision on fresh inputs, with run activity
        treated as idle; read failures give ``deferred``/``recheck``."""
        self._switch.refresh(force=True)
        switch = self._switch.state()
        vllm = self._vllm_view()
        if self.gpu_node:
            value = read_gpu_burst(self.gpu_node, sleep=self._sleep)
            if value is not None:
                self._gpu_samples.append((self._clock(), value))
        previous = self._last_obs
        try:
            index = self._client_provider().index_states()
        except Exception as exc:  # noqa: BLE001
            reason = "the Triton model index could not be read ({0})".format(exc)
            return self._recheck_decision("the Triton model index could not be read"), reason
        try:
            os.listdir(self._proc_root)
        except Exception as exc:  # noqa: BLE001
            reason = "the stub processes under /proc could not be read ({0})".format(exc)
            return self._recheck_decision("the stub processes could not be read"), reason
        obs = self._observe(index)
        verdict = obs.verdict
        now_mono, now_wall = self._clock(), self._wall()
        decision = decide_self_heal(verdict, switch, self._history(), None, obs.gpu, vllm,
                                    self._local, now_wall, now_mono)
        reason = self._commit_reason(decision, previous, obs)
        return decision, reason

    def _commit_reason(self, decision: SelfHealDecision, previous: Optional[_Observation],
                       obs: _Observation) -> str:
        kind = decision.kind
        if kind == DECISION_DISABLED:
            switch = self._switch.state()
            if switch.disabled_by == "file":
                return "the kill file {0} exists".format(switch.kill_file)
            return "{0}={1}".format(SWITCH_KEY, switch.value)
        if kind == DECISION_DEFERRED and decision.deferred_by == "vllm":
            if decision.vllm_error is not None:
                return "the vLLM runtime could not list its models ({0})".format(decision.vllm_error)
            return "the vLLM model {0} is {1}".format(decision.vllm_model, decision.vllm_state)
        if kind == DECISION_DEFERRED and decision.deferred_by == "gpu":
            return "the GPU was busy (up to {0:.1f}%)".format((decision.gpu_max_permille or 0) / 10.0)
        if kind == DECISION_IDLE:
            before = previous.verdict.stalled if previous else ()
            for name in before:
                state = obs.statuses.get(name, "absent from the index")
                if state != STATE_LOADING:
                    return "no model is stalled any more ({0} is {1})".format(name, state)
            if obs.verdict.window_covered and obs.verdict.window_cpu_s >= ACTIVITY_CPU_THRESHOLD_S:
                return "no model is stalled any more (stub CPU {0:.1f} CPU-s)".format(
                    obs.verdict.window_cpu_s)
            return "no model is stalled any more (load progress)"
        return "the self-heal record changed ({0})".format(decision.state)

    def _defer_after_quiesce(self, labels: Sequence[str], when: str) -> None:
        decision = SelfHealDecision(kind=DECISION_DEFERRED, deferred_by="runs", runs=tuple(labels))
        recovery = recovery_action(decision, None, self._switch.state(), self.records_dir)
        logger.warning(
            "SELF-HEAL deferred after quiescing: %d run(s) or capture task(s) not blocked by the "
            "wedge were %s (%s); the quiesce ends and continuous workflows resume. Not quiescing "
            "again for 5m", len(labels), when, ", ".join(labels) or "none")
        self._local = replace(self._local, deferred_at=self._clock(), deferred_by="runs",
                              deferred_recovery=recovery,
                              deferred_until_wall=self._wall() + QUIESCE_RETRY_AFTER_S)
        self._set_decision(replace(decision, recovery=recovery))

    def _defer_at_commit(self, decision: SelfHealDecision, reason: str) -> None:
        holdoff = decision.kind == DECISION_DEFERRED and decision.deferred_by in ("vllm", "gpu",
                                                                                 "recheck")
        logger.warning(
            "SELF-HEAL deferred at commit: %s; the quiesce ends and continuous workflows resume%s",
            reason, ". Not quiescing again for 5m" if holdoff else "")
        if holdoff:
            recovery = recovery_action(decision, None, self._switch.state(), self.records_dir)
            self._local = replace(self._local, deferred_at=self._clock(),
                                  deferred_by=decision.deferred_by, deferred_recovery=recovery,
                                  deferred_until_wall=self._wall() + QUIESCE_RETRY_AFTER_S)
            decision = replace(decision, recovery=recovery)
        self._set_decision(decision)

    def _build_attempt(self, attempt_no: int) -> Tuple[dict, Any]:
        obs = self._last_obs
        verdict = obs.verdict
        now_mono = self._clock()
        order = sorted(verdict.stalled, key=lambda m: (m != verdict.culprit,
                                                       obs.loading[m].since_mono, m))
        models = [{"name": m, "loadingForSeconds": int(now_mono - obs.loading[m].since_mono),
                   "suspectedCulprit": m == verdict.culprit}
                  for m in order][:SELF_HEAL_ATTEMPT_MODELS_MAX]
        evidence = self._evidence_text(verdict)[:SELF_HEAL_ATTEMPT_EVIDENCE_MAX]
        snapshot = self._run_activity.snapshot()
        interrupted = [{"kind": t.kind, "id": t.run_id, "workflow": t.workflow}
                       for t in snapshot.tickets if t.has_callback][:SELF_HEAL_RUNS_INTERRUPTED_MAX]
        try:
            dropped = self._dropped_vllm()
        except Exception as exc:  # noqa: BLE001 - recorded as unknown
            logger.warning("SELF-HEAL could not list the vLLM models (%s); the attempt records "
                           "vllmModelsDropped as unknown", exc)
            dropped = None
        attempt = {
            "attempt": attempt_no,
            "requestedAt": iso(self._wall()),
            "bootId": self.boot_id,
            "bootSeconds": self._safe(self._boottime, None),
            "restartedAt": None,
            "models": models,
            "evidence": evidence,
            "action": "backend_restart",
            "runsBlocked": len(snapshot.tickets),
            "runsInterrupted": interrupted,
            "vllmModelsDropped": dropped,
            "budget": {"maxPer24h": SELF_HEAL_MAX_PER_24H,
                       "minIntervalSeconds": SELF_HEAL_MIN_INTERVAL_S},
        }
        return _cut_all(attempt, SELF_HEAL_TEXT_MAX), snapshot

    def _dropped_vllm(self) -> List[dict]:
        states = self._vllm_states() or {}
        out = []
        for name in sorted(states):
            raw, _mapped = tuple(states[name])
            if str(raw).upper() in ("READY", "LOADING", "STAGED"):
                out.append({"name": str(name), "state": str(raw).upper()})
        return out

    def _persist_attempt(self, attempt: dict) -> None:
        document, error = read_self_heal_record(self.records_dir)
        if error is not None:
            raise RuntimeError("the record {0}".format(error))
        document = dict(document or {"schema": model_status_records.SCHEMA, "attempts": []})
        document["attempts"] = list(document.get("attempts", [])) + [attempt]
        document = prune_record(document, self.boot_id, self._safe(self._boottime, None),
                                self._wall())
        write_error = write_record(self.records_dir, document)
        if write_error is not None:
            raise OSError(write_error)

    def _rewrite_attempt(self, attempt_no: int, requested_at: str, **changes) -> Optional[str]:
        document, error = read_self_heal_record(self.records_dir)
        if error is not None or document is None:
            return error or "the record is gone"
        attempts = list(document.get("attempts", []))
        for index in range(len(attempts) - 1, -1, -1):
            item = attempts[index]
            if item.get("attempt") == attempt_no and item.get("requestedAt") == requested_at:
                attempts[index] = _cut_all(dict(item, **changes), SELF_HEAL_TEXT_MAX)
                break
        else:
            return "the attempt is not in the record"
        return write_record(self.records_dir, dict(document, attempts=attempts))

    def _restart(self, attempt_no: int) -> None:
        step = "building"
        attempt = None
        written = False
        try:
            attempt, snapshot = self._build_attempt(attempt_no)
            step = "recording"
            self._persist_attempt(attempt)
            written = True
            step = "logging"
            obs = self._last_obs
            dropped = attempt["vllmModelsDropped"]
            if dropped is None:
                dropped_text = "unknown"
            else:
                dropped_text = ", ".join("{0} ({1})".format(d["name"], d["state"])
                                         for d in dropped) or "none"
            logger.error(
                "SELF-HEAL: restarting the LocalServer backend to clear a wedged Triton model load. "
                "Models LOADING: %s. Evidence: %s. Action: backend process restart (attempt %d of "
                "at most 3 per 24h, at least 30m apart; %d run(s) blocked by the wedge are "
                "interrupted, %d of them to be marked failed; vLLM models dropped: %s). Record: %s",
                self._loading_text(obs.verdict, obs), "; ".join(attempt["evidence"]), attempt_no,
                attempt["runsBlocked"], len(attempt["runsInterrupted"]), dropped_text,
                self.record_path)
            _flush_logging()
        except Exception as exc:  # noqa: BLE001 - nothing was signalled; resume service
            self._run_activity.abort_restart()
            recorded = "could not be recorded"
            if written and attempt is not None:
                problem = self._rewrite_attempt(
                    attempt_no, attempt["requestedAt"], action="not_performed",
                    notPerformedReason=_cut("{0} the attempt failed: {1}".format(step, exc),
                                            SELF_HEAL_TEXT_MAX))
                if problem is None:
                    recorded = "is recorded as not performed"
                else:
                    logger.warning("SELF-HEAL could not record attempt %d as not performed (%s); it "
                                   "stays in the record as written and counts toward the limit",
                                   attempt_no, problem)
            logger.error(
                "SELF-HEAL not performed: %s the attempt failed (%s); new workflow runs are accepted "
                "again, the attempt %s, and self-heal waits 30m before it acts again. Restart the "
                "LocalServer backend manually to clear the wedge.", step, exc, recorded)
            self._abort_holdoff()
            return
        # Nothing after step 8 can abort.
        self._set_decision(replace(self._decision, kind=DECISION_ACT, attempt=attempt_no))
        backstop = self._arm_backstop()
        try:
            text = ("Interrupted: self-heal attempt {0} restarted the LocalServer backend to clear "
                    "a wedged Triton model load; run the workflow again.".format(attempt_no))
            report = self._run_activity.interrupt_all(text, budget_s=INTERRUPT_BUDGET_S)
            labels = {r["id"]: r.get("workflow") for r in attempt["runsInterrupted"]}
            if report.marked:
                logger.info("SELF-HEAL marked %d interrupted workflow run(s) failed: %s",
                            len(report.marked), ", ".join(
                                "{0} ({1})".format(r, labels.get(r) or "unknown")
                                for r in report.marked))
            if report.skipped:
                logger.warning("SELF-HEAL skipped marking %d interrupted workflow run(s): the %ds "
                               "budget ran out", report.skipped, INTERRUPT_BUDGET_S)
        except Exception as exc:  # noqa: BLE001 - the restart proceeds
            logger.warning("SELF-HEAL could not mark the interrupted workflow runs failed (%s)", exc)
        try:
            dropped = self._dropped_vllm()
            known = {d["name"] for d in (attempt["vllmModelsDropped"] or [])}
            if any(d["name"] not in known for d in dropped):
                problem = self._rewrite_attempt(attempt_no, attempt["requestedAt"],
                                                vllmModelsDropped=dropped)
                if problem is not None:
                    raise OSError(problem)
        except Exception as exc:  # noqa: BLE001 - the signal is sent all the same
            logger.warning("SELF-HEAL could not refresh vllmModelsDropped before the restart (%s); "
                           "the record keeps the list taken at the commit", exc)
        self.signalled = True
        if not backstop:
            self._exiter(SELF_HEAL_BACKSTOP_EXIT_CODE)
            return
        try:
            self._killer(os.getpid(), signal.SIGTERM)
        except Exception as exc:  # noqa: BLE001
            logger.error("SELF-HEAL could not send SIGTERM to the backend process (%s); exiting "
                         "now", exc)
            self._exiter(SELF_HEAL_BACKSTOP_EXIT_CODE)

    def _arm_backstop(self) -> bool:
        def _backstop():
            self._backstop_sleep(SELF_HEAL_BACKSTOP_S)
            self._exiter(SELF_HEAL_BACKSTOP_EXIT_CODE)

        try:
            thread = threading.Thread(target=_backstop, name="self-heal-backstop", daemon=True)
            thread.start()
            self.backstop_thread = thread
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("SELF-HEAL could not arm the %ds restart backstop (%s); the backend "
                         "process exits without a graceful shutdown", SELF_HEAL_BACKSTOP_S, exc)
            return False

    def _close_out(self) -> None:
        """After a self-heal restart: stamp ``restartedAt`` on every
        performed attempt that has none (D7, "After the restart")."""
        try:
            document, error = read_self_heal_record(self.records_dir)
            if error is not None or not document:
                return
            attempts = list(document.get("attempts", []))
            closed = []
            for index, attempt in enumerate(attempts):
                if attempt.get("action", "backend_restart") != "backend_restart":
                    continue
                if attempt.get("restartedAt") is None:
                    attempts[index] = dict(attempt, restartedAt=iso(self._wall()))
                    closed.append(attempt)
            if not closed:
                return
            problem = write_record(self.records_dir, dict(document, attempts=attempts))
            if problem is not None:
                logger.warning("Could not record the self-heal restart in %s (%s)",
                               self.record_path, problem)
                return
            for attempt in closed:
                logger.info("SELF-HEAL: the backend restarted after self-heal attempt %s "
                            "(requested %s); record: %s", attempt.get("attempt"),
                            attempt.get("requestedAt"), self.record_path)
        except Exception:  # noqa: BLE001 - the watchdog starts all the same
            logger.exception("Could not close out the self-heal record %s", self.record_path)

    # -- reporting (D8) -------------------------------------------------------------

    def _check_size_rejection(self, index_names: Iterable[str]) -> None:
        try:
            rejected = self._size_rejection()
        except Exception:  # noqa: BLE001
            rejected = False
        if not rejected:
            return
        names = set(index_names)
        thing = os.environ.get("AWS_IOT_THING_NAME")
        reason = None
        stale: List[str] = []
        try:
            if not thing:
                reason = "no thing name"
            else:
                document = self._shadow_reader(thing, model_status_shadow.MODEL_STATUS_SHADOW_NAME)
                reported = document.get("reported") if isinstance(document, dict) else None
                models = reported.get("models") if isinstance(reported, dict) else None
                if not isinstance(models, dict):
                    reason = ("the reported document could not be read" if not isinstance(document, dict)
                              else "the reported document has no models object")
                else:
                    known = set(self.retirements)
                    for key in sorted(models):
                        if (isinstance(key, str) and len(key) <= RETIREMENT_KEY_MAX
                                and key not in names and key not in known):
                            stale.append(key)
                    room = max(0, RETIREMENTS_MAX - len(self.retirements))
                    stale = stale[:room]
                    if not stale:
                        reason = "every entry belongs to a model in the Triton repository"
        except Exception as exc:  # noqa: BLE001
            reason = "the reported document could not be read ({0})".format(exc)
        if stale:
            self.retirements = tuple(list(self.retirements) + stale)
            logger.warning(
                "dda-model-status: ShadowManager rejected a report for its size; retiring %d "
                "GPU-fallback %s of models that are no longer in the Triton repository: %s",
                len(stale), _plural(len(stale), "entry", "entries"), ", ".join(stale))
        else:
            logger.warning(
                "dda-model-status: ShadowManager rejected a report for its size, but no stale "
                "GPU-fallback entries could be retired (%s); the next rejection retries", reason)
        self._last_report_signature = None

    def _maybe_report(self, statuses: Mapping[str, str], now: float) -> None:
        try:
            readiness, self_heal = readiness_parts(statuses, self.view, self.records_dir,
                                                   self.repo_dir, now_mono=self._clock(),
                                                   now_wall=self._wall())
            signature = _canonical({"modelReadiness": dict(readiness, updatedAt=None),
                                    "selfHeal": self_heal})
            due = signature != self._last_report_signature
            if not due and readiness.get("notReady") and (
                    self._last_report_mono is None
                    or now - self._last_report_mono >= REPORT_HEARTBEAT_S):
                due = True
            if not due:
                return
            snapshot = build_model_status_snapshot(statuses, records_dir=self.records_dir,
                                                   repo_dir=self.repo_dir, watchdog=self)
            result = self._reporter(snapshot)
            if result in ("scheduled", "unchanged"):
                self._last_report_signature = signature
                self._last_report_mono = now
        except Exception:  # noqa: BLE001 - reporting never fails a tick
            logger.debug("dda-model-status report from the watchdog failed", exc_info=True)


def _flush_logging() -> None:
    for log in (logger, logging.getLogger()):
        for handler in list(getattr(log, "handlers", [])):
            try:
                handler.flush()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- the surfaces

_WATCHDOG: Optional[LoadWatchdog] = None


def current() -> Optional[LoadWatchdog]:
    """The running watchdog, or None."""
    return _WATCHDOG


def _fresh_view(watchdog: Optional[LoadWatchdog] = None) -> Optional[WatchdogView]:
    watchdog = watchdog if watchdog is not None else _WATCHDOG
    if watchdog is None:
        return None
    view = watchdog.view
    if view is None:
        return None
    try:
        if watchdog._clock() - view.tick_mono > WATCHDOG_STALE_S:
            return None
    except Exception:  # noqa: BLE001
        return None
    return view


def _now_mono(watchdog: Optional[LoadWatchdog] = None) -> float:
    watchdog = watchdog if watchdog is not None else _WATCHDOG
    try:
        return watchdog._clock() if watchdog is not None else time.monotonic()
    except Exception:  # noqa: BLE001
        return time.monotonic()


def _records_dir(records_dir: Optional[str]) -> str:
    if records_dir:
        return records_dir
    watchdog = _WATCHDOG
    if watchdog is not None:
        return watchdog.records_dir
    return model_status_records.MODEL_STATUS_DIR


def records_dir() -> str:
    """The records directory the surfaces read: the running watchdog's,
    else :data:`model_status_records.MODEL_STATUS_DIR`."""
    return _records_dir(None)


def _drop_none(entry: Mapping[str, Any]) -> dict:
    """An array element without the keys that do not apply (an element is
    replaced whole, so nothing in it can go stale)."""
    return {k: v for k, v in entry.items() if v is not None}


def stall_for(name: str) -> Optional[LoadHealth]:
    """The stall of ``name`` from a fresh published view, or None (not
    stalled, no watchdog, or a stale view)."""
    try:
        view = _fresh_view()
        health = view.health.get(name) if view is not None else None
        if health is not None and health.state == "stalled":
            return health
    except Exception:  # noqa: BLE001
        logger.debug("stall_for(%s) failed", name, exc_info=True)
    return None


def load_health(name: str) -> Optional[LoadHealth]:
    """The load health of ``name`` once it has been LOADING for at least
    :data:`LOAD_HEALTH_AFTER_S`, from a fresh view."""
    try:
        view = _fresh_view()
        health = view.health.get(name) if view is not None else None
        if health is not None and health.loading_for_s(_now_mono()) >= LOAD_HEALTH_AFTER_S:
            return health
    except Exception:  # noqa: BLE001
        logger.debug("load_health(%s) failed", name, exc_info=True)
    return None


def _load_outcome_record(model: str, records_dir: str) -> Optional[dict]:
    if not model_status_records.LOAD_OUTCOME_FILE_PATTERN.match(str(model) + ".json"):
        return None
    record = model_status_records.read_json(
        model_status_records.load_outcome_path(model, records_dir),
        max_bytes=model_status_records.LOAD_OUTCOME_MAX_BYTES)
    if (isinstance(record, dict) and record.get("schema") == model_status_records.SCHEMA
            and record.get("outcome") == "not_ready" and record.get("model") == model):
        return record
    return None


def _load_outcome_doc(record: Mapping[str, Any]) -> dict:
    return {
        "outcome": record.get("outcome"),
        "state": record.get("state"),
        "reason": record.get("reason"),
        "failureReason": record.get("failureReason"),
        "recordedAt": record.get("recordedAt"),
        "componentVersion": record.get("componentVersion"),
        "pbStubMissingFile": record.get("pbStubMissingFile"),
    }


def _load_health_doc(health: LoadHealth, now_mono: float) -> dict:
    gpu = health.gpu_max_permille
    doc = {
        "state": health.state,
        "loadingSince": iso(health.loading_since_wall),
        "loadingForSeconds": int(health.loading_for_s(now_mono)),
        "noLoadProgressForSeconds": int(health.no_progress_s(now_mono)),
        "activity": {
            "stubProcesses": int(health.stub_count),
            "cpuSecondsLastWindow": round(float(health.window_cpu_s), 1),
            "gpuMaxPercentLastWindow": None if gpu is None else round(gpu / 10.0, 1),
            "windowSeconds": ACTIVITY_WINDOW_S,
        },
    }
    if health.state == "stalled":
        doc["evidence"] = list(health.evidence)
        doc["recoveryAction"] = health.recovery_action
        doc["selfHeal"] = {"state": health.self_heal_state, "until": health.self_heal_until}
    return doc


def _history_for(records_dir: str, view: Optional[WatchdogView]) -> Tuple[List[Tuple[dict, float]], float]:
    document, error = read_self_heal_record(records_dir)
    if error is not None or not document:
        return [], time.time()
    now_wall = time.time()
    if view is not None:
        boot_id, boottime = view.boot_id, _default_boottime()
    else:
        boot_id, boottime = _read_boot_id(), _default_boottime()
    history = History(attempts=tuple(document.get("attempts", [])), boot_id=boot_id,
                      boottime=boottime)
    return attempts_in_window(history, now_wall), now_wall


def model_annotations(model_id: str, status: Any, records_dir: Optional[str] = None) -> dict:
    """The additive ``defaultConfiguration`` fields of one Triton model
    (D8): ``loadOutcome``, ``loadHealth`` and ``lastSelfHeal``, each only
    when it applies."""
    out: dict = {}
    directory = _records_dir(records_dir)
    state = str(status or "").strip().upper()
    if state != STATE_READY:
        record = _load_outcome_record(model_id, directory)
        if record is not None:
            out["loadOutcome"] = _load_outcome_doc(record)
    if state == STATE_LOADING:
        view = _fresh_view()
        health = view.health.get(model_id) if view is not None else None
        now = _now_mono()
        if health is not None and health.loading_for_s(now) >= LOAD_HEALTH_AFTER_S:
            out["loadHealth"] = _load_health_doc(health, now)
    recent, _now = _history_for(directory, _fresh_view())
    named = [(a, age) for a, age in recent
             if any(isinstance(m, dict) and m.get("name") == model_id for m in a.get("models", []))]
    if named:
        attempt, _age = min(named, key=lambda item: item[1])
        loading_for = next((m.get("loadingForSeconds") for m in attempt.get("models", [])
                            if isinstance(m, dict) and m.get("name") == model_id), None)
        out["lastSelfHeal"] = {
            "attempt": attempt.get("attempt"),
            "requestedAt": attempt.get("requestedAt"),
            "restartedAt": attempt.get("restartedAt"),
            "loadingForSeconds": loading_for,
            "evidence": list(attempt.get("evidence") or []),
            "action": attempt.get("action", "backend_restart"),
        }
    return out


def self_heal_document(view: Optional[WatchdogView], records_dir: Optional[str] = None) -> dict:
    """The ``selfHeal`` object of the shadow (D8), with its fixed key set.
    Never raises."""
    doc = {"enabled": False, "disabledBy": "watchdog_not_running", "state": None,
           "deferredBy": None, "attemptsIn24h": 0, "maxPer24h": SELF_HEAL_MAX_PER_24H,
           "minIntervalSeconds": SELF_HEAL_MIN_INTERVAL_S, "rearmsAt": None, "last": None}
    try:
        if view is not None:
            doc["enabled"] = bool(view.switch.enabled)
            doc["disabledBy"] = view.switch.disabled_by
            doc["state"] = view.decision.state
            doc["deferredBy"] = (view.decision.deferred_by
                                 if view.decision.kind == DECISION_DEFERRED else None)
        directory = records_dir or model_status_records.MODEL_STATUS_DIR
        recent, now_wall = _history_for(directory, view)
        doc["attemptsIn24h"] = len(recent)
        if len(recent) >= SELF_HEAL_MAX_PER_24H:
            ages = sorted((age for _a, age in recent), reverse=True)
            doc["rearmsAt"] = iso(now_wall + SELF_HEAL_WINDOW_S
                                  - ages[len(ages) - SELF_HEAL_MAX_PER_24H])
        if recent:
            attempt, _age = min(recent, key=lambda item: item[1])
            dropped = attempt.get("vllmModelsDropped")
            doc["last"] = {
                "attempt": attempt.get("attempt"),
                "requestedAt": attempt.get("requestedAt"),
                "restartedAt": attempt.get("restartedAt"),
                "action": attempt.get("action", "backend_restart"),
                "models": [_drop_none({"name": m.get("name"),
                                       "loadingForSeconds": m.get("loadingForSeconds")})
                           for m in attempt.get("models", []) if isinstance(m, dict)],
                "evidence": [e for e in (attempt.get("evidence") or []) if isinstance(e, str)],
                "vllmModelsDropped": ([_drop_none({"name": d.get("name"), "state": d.get("state")})
                                       for d in dropped if isinstance(d, dict)]
                                      if isinstance(dropped, list) else None),
            }
    except Exception:  # noqa: BLE001 - the fixed key set all the same
        logger.debug("selfHeal document failed", exc_info=True)
    return doc


def _cut_entry(entry: dict, limit: int) -> dict:
    return _cut_all(entry, limit)


def readiness_parts(statuses: Mapping[str, Any], view: Optional[WatchdogView],
                    records_dir: Optional[str] = None, repo_dir: Optional[str] = None,
                    now_mono: Optional[float] = None, now_wall: Optional[float] = None
                    ) -> Tuple[dict, dict]:
    """The ``modelReadiness`` and ``selfHeal`` keys (D8), size-bounded."""
    directory = records_dir or model_status_records.MODEL_STATUS_DIR
    repo = repo_dir or TRITON_MODEL_DIR
    now_wall = time.time() if now_wall is None else now_wall
    components = {str(n): str(s or STATE_UNKNOWN).strip().upper() or STATE_UNKNOWN
                  for n, s in dict(statuses).items() if is_component(str(n))}
    try:
        records = model_status_records.read_load_outcomes(directory)
    except Exception:  # noqa: BLE001
        records = {}
    try:
        for name, record in model_status_records.unlisted_recorded_models(
                list(statuses), repo, records_dir=directory):
            components.setdefault(name, STATE_UNKNOWN)
    except Exception:  # noqa: BLE001
        logger.debug("Could not list record-backed models", exc_info=True)
    health = view.health if view is not None else {}
    models = []
    not_ready = []
    for name in sorted(components):
        state = components[name]
        item = health.get(name)
        stalled = bool(item is not None and item.state == "stalled" and state == STATE_LOADING)
        models.append({"model": name, "state": state, "stalled": stalled})
        record = records.get(name) if state != STATE_READY else None
        if state in (STATE_LOADING, STATE_UNAVAILABLE, STATE_UNLOADING) or record is not None:
            entry = {"model": name, "status": state, "stalled": stalled}
            since = None
            if state == STATE_LOADING and item is not None:
                since = iso(item.loading_since_wall)
            elif view is not None and view.state_since.get(name, (None,))[0] == state:
                since = iso(view.state_since[name][1])
            elif record is not None:
                since = record.get("recordedAt")
            if since:
                entry["since"] = since
            reason = None
            if stalled and item.evidence:
                reason = item.evidence[0]
            elif view is not None and view.reasons.get(name):
                reason = view.reasons[name]
            elif record is not None and record.get("reason"):
                reason = record.get("reason")
            if reason:
                entry["reason"] = reason
            if record is not None:
                entry["loadOutcome"] = _drop_none({"state": record.get("state"),
                                                   "recordedAt": record.get("recordedAt"),
                                                   "reason": record.get("reason")})
            not_ready.append(_cut_entry(entry, SHADOW_STRING_MAX))
    ready = sum(1 for s in components.values() if s == STATE_READY)
    cold = sum(1 for s in components.values() if s == STATE_UNKNOWN)
    models_prio = sorted(models, key=lambda m: (m["state"] == STATE_READY, m["model"]))
    not_ready_prio = sorted(not_ready, key=lambda m: (not m["stalled"], m["model"]))
    truncated = len(models_prio) > SHADOW_MODELS_MAX or len(not_ready_prio) > SHADOW_NOT_READY_MAX
    models_prio = [_cut_entry(m, SHADOW_STRING_MAX) for m in models_prio[:SHADOW_MODELS_MAX]]
    not_ready_prio = not_ready_prio[:SHADOW_NOT_READY_MAX]
    self_heal = self_heal_document(view, directory)
    last = self_heal.get("last")
    if isinstance(last, dict):
        last = dict(last)
        last["models"] = last["models"][:SHADOW_LAST_MODELS_MAX]
        last["evidence"] = last["evidence"][:SHADOW_LAST_EVIDENCE_MAX]
        self_heal = dict(self_heal, last=_cut_all(last, SHADOW_STRING_MAX))
    self_heal = _cut_all(self_heal, SHADOW_STRING_MAX)
    updated = iso(now_wall)

    def render(models_list, not_ready_list, heal, cut):
        readiness = {"updatedAt": updated, "readyModels": ready, "coldModels": cold,
                     "models": sorted(models_list, key=lambda m: m["model"]),
                     "notReady": sorted(not_ready_list, key=lambda m: m["model"]),
                     "truncated": bool(cut)}
        return readiness, heal

    def size(parts) -> int:
        return len(_canonical({"modelReadiness": parts[0], "selfHeal": parts[1]}).encode("utf-8"))

    parts = render(models_prio, not_ready_prio, self_heal, truncated)
    if size(parts) <= READINESS_SHADOW_BUDGET_BYTES:
        return parts

    def last_with(heal, evidence):
        if isinstance(heal.get("last"), dict):
            return dict(heal, last=dict(heal["last"], evidence=evidence))
        return heal

    def cut_reasons(entries, limit):
        out = []
        for entry in entries:
            entry = dict(entry)
            if "reason" in entry:
                entry["reason"] = _cut(entry["reason"], limit)
            if isinstance(entry.get("loadOutcome"), dict):
                entry["loadOutcome"] = dict(entry["loadOutcome"],
                                            reason=_cut(entry["loadOutcome"].get("reason"), limit))
            out.append(entry)
        return out

    def drop_reasons(entries):
        out = []
        for entry in entries:
            entry = {k: v for k, v in entry.items() if k != "reason"}
            if isinstance(entry.get("loadOutcome"), dict):
                entry["loadOutcome"] = {k: v for k, v in entry["loadOutcome"].items()
                                        if k != "reason"}
            out.append(entry)
        return out

    # 1. selfHeal.last.evidence keeps one item.
    if isinstance(self_heal.get("last"), dict):
        self_heal = last_with(self_heal, self_heal["last"]["evidence"][:1])
    parts = render(models_prio, not_ready_prio, self_heal, True)
    if size(parts) <= READINESS_SHADOW_BUDGET_BYTES:
        return parts
    # 2. every reason in notReady is cut to 80 characters.
    not_ready_prio = cut_reasons(not_ready_prio, 80)
    parts = render(models_prio, not_ready_prio, self_heal, True)
    if size(parts) <= READINESS_SHADOW_BUDGET_BYTES:
        return parts
    # 3. notReady keeps 5 entries.
    not_ready_prio = not_ready_prio[:5]
    parts = render(models_prio, not_ready_prio, self_heal, True)
    if size(parts) <= READINESS_SHADOW_BUDGET_BYTES:
        return parts
    # 4. models keeps 10 entries.
    models_prio = models_prio[:10]
    parts = render(models_prio, not_ready_prio, self_heal, True)
    if size(parts) <= READINESS_SHADOW_BUDGET_BYTES:
        return parts
    # 5. the reason fields and selfHeal.last.evidence are dropped.
    not_ready_prio = drop_reasons(not_ready_prio)
    if isinstance(self_heal.get("last"), dict):
        self_heal = last_with(self_heal, None)
    parts = render(models_prio, not_ready_prio, self_heal, True)
    # 6. entries are dropped from the end of models, then of notReady.
    while size(parts) > READINESS_SHADOW_BUDGET_BYTES and (models_prio or not_ready_prio):
        if models_prio:
            models_prio = models_prio[:-1]
        else:
            not_ready_prio = not_ready_prio[:-1]
        parts = render(models_prio, not_ready_prio, self_heal, True)
    return parts


def _empty_parts() -> Tuple[dict, dict]:
    return ({"updatedAt": iso(time.time()), "readyModels": 0, "coldModels": 0, "models": [],
             "notReady": [], "truncated": False},
            {"enabled": False, "disabledBy": "watchdog_not_running", "state": None,
             "deferredBy": None, "attemptsIn24h": 0, "maxPer24h": SELF_HEAL_MAX_PER_24H,
             "minIntervalSeconds": SELF_HEAL_MIN_INTERVAL_S, "rearmsAt": None, "last": None})


def build_model_status_snapshot(statuses: Mapping[str, Any], records_dir: Optional[str] = None,
                                repo_dir: Optional[str] = None,
                                watchdog: Optional[LoadWatchdog] = None) -> dict:
    """The ``dda-model-status`` snapshot (D8): the unchanged
    ``device_gpu_status`` document plus ``modelReadiness`` and ``selfHeal``.
    Never creates Triton: ``statuses`` (model -> Triton status) is passed
    in, everything else comes from the running watchdog and the records."""
    statuses = dict(statuses or {})
    records = {name: provider_visibility.read_active_provider_record(name) for name in statuses}
    document = provider_visibility.device_gpu_status(records, statuses)
    watchdog = watchdog if watchdog is not None else _WATCHDOG
    try:
        for name in (watchdog.retirements if watchdog is not None else ()):
            if name not in statuses and name not in document["models"]:
                document["models"][name] = None
    except Exception:  # noqa: BLE001
        logger.debug("Could not apply the shadow retirements", exc_info=True)
    try:
        directory = records_dir or (watchdog.records_dir if watchdog is not None else None)
        repo = repo_dir or (watchdog.repo_dir if watchdog is not None else None)
        readiness, self_heal = readiness_parts(
            statuses, _fresh_view(watchdog), directory, repo,
            now_wall=watchdog._wall() if watchdog is not None else None)
    except Exception:  # noqa: BLE001 - both keys are in every report
        logger.warning("modelReadiness and selfHeal could not be built; reporting them empty",
                       exc_info=True)
        readiness, self_heal = _empty_parts()
    document["modelReadiness"] = readiness
    document["selfHeal"] = self_heal
    return document


def start_model_load_watchdog() -> Optional[LoadWatchdog]:
    """Start the watchdog on Triton devices. Contained: any failure is
    logged and LocalServer runs without it."""
    global _WATCHDOG
    try:
        from utils.get_is_triton import get_is_triton

        if not get_is_triton():
            return None
        if _WATCHDOG is not None:
            return _WATCHDOG
        watchdog = LoadWatchdog()
        watchdog.start()
        _WATCHDOG = watchdog
        return watchdog
    except Exception:  # noqa: BLE001 - LocalServer starts as before
        logger.exception("Triton model-load watchdog could not be started; LocalServer runs "
                         "without it")
        return None
