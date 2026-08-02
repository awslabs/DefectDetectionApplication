"""ResultsPlugin: the Results_Bundle writer for the Edge_Test_Harness (Req 8.1).

A pytest plugin (instantiated and registered by ``conftest.py``) hooking
``pytest_runtest_logreport`` and ``pytest_sessionfinish``. It groups test
outcomes by stage (module) and writes ``results.json`` (``schema_version`` 1)
into the per-run output directory — ``--harness-output-dir``, defaulting to
``harness-results/<device>-<UTC timestamp>/`` — alongside the JUnit XML
(relocated from the ``pytest.ini`` addopts path at unconfigure) and a
``failures/`` directory with the bounded request/response captures (Req 8.2).

Channels the stages use:

* ``record_metric(name, value)`` — informational metrics (generate latency,
  token counts) recorded without thresholds (Req 5.4); exposed both as a
  plugin method and as a pytest fixture contributed by the plugin instance.
* ``record_failure_capture(nodeid, diagnostic)`` — structured
  ``DeviceApiError.diagnostic()`` payloads attached to the failing test's
  capture file in ``failures/`` (Req 8.2).
* ``set_local_server_version(version)`` — device identity populated by the
  health stage (Req 3.2).

Restoration warnings are read from the session :class:`~harnesslib.restoration.StateRegistry`
at write time so teardown outcomes always reach the bundle (Req 8.3).
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic as _monotonic
from typing import Any, Callable, Dict, List, Optional

import pytest
from harnesslib.config import DeviceTarget
from harnesslib.restoration import StateRegistry

#: results.json schema version (design: Data Models).
SCHEMA_VERSION = 1

#: Upper bound on the failure-message excerpt captured per failing test,
#: mirroring the client's response-body bound (Req 8.2).
FAILURE_EXCERPT_LIMIT = 8 * 1024

#: Root directory for per-run bundles when ``--harness-output-dir`` is unset.
DEFAULT_OUTPUT_ROOT = Path("harness-results")

OUTPUT_DIR_OPTION = "--harness-output-dir"

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def pytest_addoption(parser) -> None:
    """Register ``--harness-output-dir`` (delegated to by ``conftest.py``)."""
    parser.addoption(
        OUTPUT_DIR_OPTION,
        action="store",
        default=None,
        help=(
            "Directory receiving the Results_Bundle (results.json, junit.xml, "
            "failures/); defaults to harness-results/<device>-<UTC timestamp>/"
        ),
    )


def default_output_dir(device_name: str, now: Optional[datetime] = None) -> Path:
    """The default per-run bundle directory: ``harness-results/<device>-<ts>/``."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return DEFAULT_OUTPUT_ROOT / f"{device_name}-{stamp}"


def _sanitize_nodeid(nodeid: str) -> str:
    return _UNSAFE_FILENAME_CHARS.sub("_", nodeid)


def _stage_of(nodeid: str) -> str:
    """The stage a test belongs to: its module file stem (design: grouped by
    stage module, e.g. ``test_20_vllm_textgen``)."""
    module_path = nodeid.split("::", 1)[0]
    return Path(module_path).stem


def _skip_reason(report: Any) -> str:
    """The recorded skip reason of a skipped report (Req 2.1 reasons flow
    into results.json)."""
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        reason = str(longrepr[2])
    else:
        reason = str(longrepr) if longrepr is not None else ""
    prefix = "Skipped: "
    return reason[len(prefix) :] if reason.startswith(prefix) else reason


def _failure_message(report: Any) -> str:
    text = getattr(report, "longreprtext", None)
    if not text:
        longrepr = getattr(report, "longrepr", None)
        text = str(longrepr) if longrepr is not None else ""
    return text[:FAILURE_EXCERPT_LIMIT]


class _TestRecord:
    """Aggregated outcome of one test item across setup/call/teardown."""

    __slots__ = ("stage", "outcome", "skip_reason")

    def __init__(self, stage: str):
        self.stage = stage
        self.outcome: Optional[str] = None  # 'passed' | 'failed' | 'skipped'
        self.skip_reason: Optional[str] = None


class ResultsPlugin:
    """Per-run Results_Bundle writer (Reqs 8.1, 8.2, 3.2, 5.4).

    :param target: the selected device, providing identity and profile for
        the bundle header.
    :param output_dir: bundle directory; ``None`` selects the default
        ``harness-results/<device>-<UTC timestamp>/``.
    :param registry: session :class:`StateRegistry` whose accumulated
        warnings become ``restoration_warnings`` (Req 8.3).
    :param now: injectable UTC clock for the run timestamp (tests).
    :param monotonic: injectable monotonic clock for the run duration (tests).
    """

    def __init__(
        self,
        target: DeviceTarget,
        output_dir: Optional[Path] = None,
        registry: Optional[StateRegistry] = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = _monotonic,
    ):
        self.target = target
        self.registry = registry
        self._now = now
        self._monotonic = monotonic
        self.started_at = now()
        self._started_monotonic = monotonic()
        self.output_dir = (
            Path(output_dir)
            if output_dir is not None
            else default_output_dir(target.name, self.started_at)
        )
        self.local_server_version: Optional[str] = None
        self.metrics: Dict[str, Any] = {}
        self._tests: "Dict[str, _TestRecord]" = {}
        self._failures: List[Dict[str, Any]] = []
        self._api_captures: Dict[str, List[Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Channels used by stages and fixtures
    # ------------------------------------------------------------------

    def set_local_server_version(self, version: Optional[str]) -> None:
        """Device identity populated by the health stage (Req 3.2)."""
        self.local_server_version = version

    def record_metric(self, name: str, value: Any) -> None:
        """Record one informational metric, no thresholds asserted (Req 5.4)."""
        self.metrics[name] = value

    @pytest.fixture(name="record_metric")
    def record_metric_fixture(self):
        """The ``record_metric(name, value)`` fixture channel (Req 5.4)."""
        return self.record_metric

    def record_failure_capture(self, nodeid: str, diagnostic: Dict[str, Any]) -> None:
        """Attach a structured API diagnostic (``DeviceApiError.diagnostic()``)
        to the named test's ``failures/`` capture (Req 8.2)."""
        self._api_captures.setdefault(nodeid, []).append(diagnostic)

    # ------------------------------------------------------------------
    # pytest hooks
    # ------------------------------------------------------------------

    def pytest_runtest_logreport(self, report) -> None:
        """Aggregate per-phase reports into one outcome per test.

        Precedence per test: failed > skipped > passed — a teardown failure
        turns a passing call into a failure; a setup skip records its reason.
        """
        record = self._tests.get(report.nodeid)
        if record is None:
            record = self._tests[report.nodeid] = _TestRecord(_stage_of(report.nodeid))
        if report.failed:
            record.outcome = "failed"
            self._failures.append(
                {
                    "test": report.nodeid,
                    "phase": report.when,
                    "message": _failure_message(report),
                }
            )
        elif report.skipped:
            if record.outcome != "failed":
                record.outcome = "skipped"
                record.skip_reason = _skip_reason(report)
        elif report.when == "call" and record.outcome is None:
            record.outcome = "passed"
        elif report.when == "teardown" and record.outcome is None:
            # Setup-skipped items produce no call report; keep their state.
            record.outcome = "passed"

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        """Write ``results.json`` and the ``failures/`` captures (Req 8.1)."""
        self.write_bundle(int(exitstatus))

    def pytest_unconfigure(self, config) -> None:
        """Relocate the addopts-written JUnit XML into the bundle directory.

        Runs at unconfigure — after the junitxml plugin's own sessionfinish
        has written the file — so the bundle directory ends up holding
        results.json, junit.xml, and failures/ together (Req 8.1).
        """
        xmlpath = getattr(config.option, "xmlpath", None)
        if xmlpath:
            self.relocate_junit(Path(xmlpath))

    # ------------------------------------------------------------------
    # Bundle writing
    # ------------------------------------------------------------------

    def _stage_summaries(self) -> Dict[str, Dict[str, Any]]:
        stages: Dict[str, Dict[str, Any]] = {}
        for nodeid, record in self._tests.items():
            stage = stages.setdefault(
                record.stage,
                {"passed": 0, "failed": 0, "skipped": 0, "skip_reasons": [], "failures": []},
            )
            outcome = record.outcome or "passed"
            stage[outcome] += 1
            if record.skip_reason and record.skip_reason not in stage["skip_reasons"]:
                stage["skip_reasons"].append(record.skip_reason)
        for failure in self._failures:
            stages.setdefault(
                _stage_of(failure["test"]),
                {"passed": 0, "failed": 0, "skipped": 0, "skip_reasons": [], "failures": []},
            )["failures"].append(failure)
        return stages

    def results_document(self, exit_status: int) -> Dict[str, Any]:
        """The results.json document (``schema_version`` 1)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "device": self.target.name,
            "profile": {
                "architecture": self.target.profile.architecture,
                "capabilities": sorted(self.target.profile.capabilities),
            },
            "local_server_version": self.local_server_version,
            "started_at": self.started_at.isoformat(),
            "duration_s": round(self._monotonic() - self._started_monotonic, 3),
            "exit_status": exit_status,
            "outcome": "passed" if exit_status == 0 else "failed",
            "stages": self._stage_summaries(),
            "metrics": dict(self.metrics),
            "restoration_warnings": (
                list(self.registry.warnings) if self.registry is not None else []
            ),
        }

    def write_bundle(self, exit_status: int) -> Path:
        """Write results.json (and failures/ captures) into the output dir."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        results_path = self.output_dir / "results.json"
        results_path.write_text(
            json.dumps(self.results_document(exit_status), indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        self._write_failure_captures()
        return results_path

    def _write_failure_captures(self) -> None:
        """One JSON capture per failure under ``failures/``, carrying the
        bounded failure message and any structured API diagnostics (Req 8.2)."""
        if not self._failures:
            return
        failures_dir = self.output_dir / "failures"
        failures_dir.mkdir(parents=True, exist_ok=True)
        for index, failure in enumerate(self._failures):
            capture = dict(failure)
            capture["api_captures"] = self._api_captures.get(failure["test"], [])
            name = f"{index:02d}-{_sanitize_nodeid(failure['test'])}.json"
            (failures_dir / name).write_text(json.dumps(capture, indent=2) + "\n", encoding="utf-8")

    def relocate_junit(self, junit_path: Path) -> Optional[Path]:
        """Move the junitxml artifact into the bundle directory (no-op when
        the file is absent or already inside the bundle)."""
        try:
            if not junit_path.exists():
                return None
            destination = self.output_dir / "junit.xml"
            if junit_path.resolve() == destination.resolve():
                return destination
            self.output_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(junit_path), str(destination))
            return destination
        except OSError:
            # Artifact relocation must never fail the run.
            return None
