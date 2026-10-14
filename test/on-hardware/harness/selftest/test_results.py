"""Unit tests for harnesslib.results (Reqs 8.1, 8.2, 3.2, 5.4).

Drives the ResultsPlugin with fake pytest reports (the same attributes real
``TestReport`` objects carry): results.json schema contents, stage grouping,
skip reasons propagated, metrics channel, failure captures, restoration
warnings, and junit relocation.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from harnesslib.config import DeviceProfile, DeviceTarget
from harnesslib.restoration import StateRegistry
from harnesslib.results import (
    FAILURE_EXCERPT_LIMIT,
    SCHEMA_VERSION,
    ResultsPlugin,
    default_output_dir,
)

FIXED_NOW = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class FakeReport:
    """The TestReport surface the plugin reads."""

    def __init__(
        self,
        nodeid,
        when="call",
        passed=False,
        failed=False,
        skipped=False,
        longrepr=None,
        longreprtext="",
    ):
        self.nodeid = nodeid
        self.when = when
        self.passed = passed
        self.failed = failed
        self.skipped = skipped
        self.longrepr = longrepr
        self.longreprtext = longreprtext


def passing(nodeid):
    """The setup/call/teardown report sequence of one passing test."""
    return [
        FakeReport(nodeid, when="setup", passed=True),
        FakeReport(nodeid, when="call", passed=True),
        FakeReport(nodeid, when="teardown", passed=True),
    ]


def failing(nodeid, message="AssertionError: nope"):
    return [
        FakeReport(nodeid, when="setup", passed=True),
        FakeReport(nodeid, when="call", failed=True, longreprtext=message),
        FakeReport(nodeid, when="teardown", passed=True),
    ]


def skipped(nodeid, reason):
    """A marker-skipped test: setup skips, no call phase."""
    return [
        FakeReport(
            nodeid,
            when="setup",
            skipped=True,
            longrepr=("stages/x.py", 3, f"Skipped: {reason}"),
        ),
        FakeReport(nodeid, when="teardown", passed=True),
    ]


def make_target(name="jp6-orinagx"):
    return DeviceTarget(
        name=name,
        base_url="http://device:5000",
        profile=DeviceProfile(
            architecture="arm64_jp6",
            capabilities=frozenset({"vllm", "workflows"}),
        ),
    )


def make_plugin(tmp_path, registry=None, **kwargs):
    clock = {"now": 100.0}
    kwargs.setdefault("now", lambda: FIXED_NOW)
    kwargs.setdefault("monotonic", lambda: clock["now"])
    plugin = ResultsPlugin(
        make_target(), output_dir=tmp_path / "bundle", registry=registry, **kwargs
    )
    clock["now"] = 112.5  # 12.5s run duration
    return plugin


def feed(plugin, *report_sequences):
    for reports in report_sequences:
        for report in reports:
            plugin.pytest_runtest_logreport(report)


def written_results(plugin, exit_status=0):
    path = plugin.write_bundle(exit_status)
    return json.loads(path.read_text())


class TestResultsSchema:
    def test_bundle_header_identity_and_profile(self, tmp_path):
        plugin = make_plugin(tmp_path)
        plugin.set_local_server_version("1.4.2")
        doc = written_results(plugin)
        assert doc["schema_version"] == SCHEMA_VERSION
        assert doc["device"] == "jp6-orinagx"
        assert doc["profile"] == {
            "architecture": "arm64_jp6",
            "capabilities": ["vllm", "workflows"],
        }
        assert doc["local_server_version"] == "1.4.2"
        assert doc["started_at"] == "2025-01-02T03:04:05+00:00"
        assert doc["duration_s"] == 12.5

    def test_overall_outcome_reflects_exit_status(self, tmp_path):
        assert written_results(make_plugin(tmp_path), exit_status=0)["outcome"] == "passed"
        assert written_results(make_plugin(tmp_path), exit_status=1)["outcome"] == "failed"

    def test_local_server_version_none_when_never_reported(self, tmp_path):
        doc = written_results(make_plugin(tmp_path))
        assert doc["local_server_version"] is None

    def test_results_json_written_into_output_dir(self, tmp_path):
        plugin = make_plugin(tmp_path)
        path = plugin.write_bundle(0)
        assert path == tmp_path / "bundle" / "results.json"
        assert path.exists()


class TestStageGrouping:
    def test_outcomes_grouped_by_stage_module(self, tmp_path):
        plugin = make_plugin(tmp_path)
        feed(
            plugin,
            passing("stages/test_00_health.py::test_health"),
            passing("stages/test_00_health.py::test_identity"),
            failing("stages/test_20_vllm_textgen.py::test_generate"),
            skipped(
                "stages/test_30_workflows.py::test_run",
                "capability 'workflows' not granted by device profile jp6-orinagx",
            ),
        )
        stages = written_results(plugin, exit_status=1)["stages"]
        assert stages["test_00_health"]["passed"] == 2
        assert stages["test_00_health"]["failed"] == 0
        assert stages["test_20_vllm_textgen"]["failed"] == 1
        assert stages["test_30_workflows"]["skipped"] == 1

    def test_skip_reasons_propagated(self, tmp_path):
        plugin = make_plugin(tmp_path)
        reason = "capability 'vllm' not granted by device profile jp5-xavier"
        feed(plugin, skipped("stages/test_20_vllm_textgen.py::test_generate", reason))
        stages = written_results(plugin)["stages"]
        assert stages["test_20_vllm_textgen"]["skip_reasons"] == [reason]

    def test_duplicate_skip_reasons_recorded_once(self, tmp_path):
        plugin = make_plugin(tmp_path)
        reason = "capability 'vllm' not granted"
        feed(
            plugin,
            skipped("stages/test_20_vllm_textgen.py::test_a", reason),
            skipped("stages/test_20_vllm_textgen.py::test_b", reason),
        )
        stage = written_results(plugin)["stages"]["test_20_vllm_textgen"]
        assert stage["skipped"] == 2
        assert stage["skip_reasons"] == [reason]

    def test_teardown_failure_marks_test_failed(self, tmp_path):
        plugin = make_plugin(tmp_path)
        nodeid = "stages/test_10_vision_models.py::test_start"
        feed(
            plugin,
            [
                FakeReport(nodeid, when="setup", passed=True),
                FakeReport(nodeid, when="call", passed=True),
                FakeReport(nodeid, when="teardown", failed=True, longreprtext="boom"),
            ],
        )
        stage = written_results(plugin, exit_status=1)["stages"]["test_10_vision_models"]
        assert stage["failed"] == 1
        assert stage["passed"] == 0

    def test_failure_entries_carry_test_phase_and_message(self, tmp_path):
        plugin = make_plugin(tmp_path)
        nodeid = "stages/test_20_vllm_textgen.py::test_generate"
        feed(plugin, failing(nodeid, message="DeviceApiError: HTTP 500"))
        (failure,) = written_results(plugin, exit_status=1)["stages"]["test_20_vllm_textgen"][
            "failures"
        ]
        assert failure == {
            "test": nodeid,
            "phase": "call",
            "message": "DeviceApiError: HTTP 500",
        }

    def test_failure_message_bounded(self, tmp_path):
        plugin = make_plugin(tmp_path)
        feed(
            plugin,
            failing("stages/test_x.py::test_big", message="x" * (FAILURE_EXCERPT_LIMIT + 500)),
        )
        (failure,) = written_results(plugin, exit_status=1)["stages"]["test_x"]["failures"]
        assert len(failure["message"]) == FAILURE_EXCERPT_LIMIT


class TestMetricsChannel:
    def test_record_metric_lands_in_results(self, tmp_path):
        plugin = make_plugin(tmp_path)
        plugin.record_metric("generate_latency_s", 1.42)
        plugin.record_metric("generate_token_count", 17)
        doc = written_results(plugin)
        assert doc["metrics"] == {
            "generate_latency_s": 1.42,
            "generate_token_count": 17,
        }

    def test_metrics_empty_by_default(self, tmp_path):
        assert written_results(make_plugin(tmp_path))["metrics"] == {}


class TestRestorationWarnings:
    def test_registry_warnings_reach_the_bundle(self, tmp_path):
        registry = StateRegistry()
        registry.record(
            "model",
            "m",
            "UNAVAILABLE",
            lambda: (_ for _ in ()).throw(RuntimeError("device went away")),
        )
        plugin = make_plugin(tmp_path, registry=registry)
        registry.restore_all()
        (warning,) = written_results(plugin)["restoration_warnings"]
        assert "device went away" in warning

    def test_empty_without_registry(self, tmp_path):
        assert written_results(make_plugin(tmp_path))["restoration_warnings"] == []


class TestFailureCaptures:
    def test_capture_files_written_with_api_diagnostics(self, tmp_path):
        plugin = make_plugin(tmp_path)
        nodeid = "stages/test_20_vllm_textgen.py::test_generate"
        diagnostic = {
            "method": "POST",
            "path": "/text-generation/m/generate",
            "status": 500,
            "body_excerpt": "engine dead",
            "elapsed_s": 0.4,
            "request_headers": {"Authorization": "<redacted>"},
        }
        plugin.record_failure_capture(nodeid, diagnostic)
        feed(plugin, failing(nodeid, message="DeviceApiError: HTTP 500"))
        plugin.write_bundle(1)
        captures = sorted((tmp_path / "bundle" / "failures").glob("*.json"))
        assert len(captures) == 1
        capture = json.loads(captures[0].read_text())
        assert capture["test"] == nodeid
        assert capture["phase"] == "call"
        assert capture["api_captures"] == [diagnostic]

    def test_no_failures_directory_when_all_pass(self, tmp_path):
        plugin = make_plugin(tmp_path)
        feed(plugin, passing("stages/test_00_health.py::test_health"))
        plugin.write_bundle(0)
        assert not (tmp_path / "bundle" / "failures").exists()


class TestSessionFinishHook:
    def test_sessionfinish_writes_the_bundle(self, tmp_path):
        plugin = make_plugin(tmp_path)
        feed(plugin, passing("stages/test_00_health.py::test_health"))
        plugin.pytest_sessionfinish(session=None, exitstatus=0)
        doc = json.loads((tmp_path / "bundle" / "results.json").read_text())
        assert doc["outcome"] == "passed"
        assert doc["stages"]["test_00_health"]["passed"] == 1


class TestJunitRelocation:
    def test_junit_moved_into_bundle(self, tmp_path):
        plugin = make_plugin(tmp_path)
        junit = tmp_path / "elsewhere" / "junit.xml"
        junit.parent.mkdir()
        junit.write_text("<testsuites/>")
        destination = plugin.relocate_junit(junit)
        assert destination == tmp_path / "bundle" / "junit.xml"
        assert destination.exists()
        assert not junit.exists()

    def test_missing_junit_is_a_noop(self, tmp_path):
        plugin = make_plugin(tmp_path)
        assert plugin.relocate_junit(tmp_path / "absent.xml") is None


class TestDefaultOutputDir:
    def test_default_dir_names_device_and_timestamp(self):
        path = default_output_dir("jp6-orinagx", now=FIXED_NOW)
        assert path == Path("harness-results") / "jp6-orinagx-20250102-030405"

    def test_plugin_defaults_when_no_output_dir_given(self):
        plugin = ResultsPlugin(make_target(), now=lambda: FIXED_NOW)
        assert plugin.output_dir == (Path("harness-results") / "jp6-orinagx-20250102-030405")
