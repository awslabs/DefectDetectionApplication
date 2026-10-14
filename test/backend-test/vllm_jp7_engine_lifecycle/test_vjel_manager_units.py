# Copyright 2026 Amazon Web Services, Inc.
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
"""Manager wiring of the Construction_Watchdog and Decision 3 (a) (spec
vllm-jp7-engine-lifecycle, design changes 3, 4 and 6).

- The H2 shape: a construction that blocks without any engine core. The
  watchdog has nothing to stop, so after the Unblock_Grace the manager
  reports FAILED at once (while the construction is still blocked), logs
  CRITICAL, writes the Hang_Marker and restarts the backend (a test double).
  The late return keeps FAILED and shuts the new engine down.
- The Hang_Marker after the "restart": FAILED with the recorded reason, one
  WARNING, the reconciler does not re-drive it, an explicit load clears it.
- A timed-out construction is never retried in offline-cache mode, while an
  ordinary failure still is.
- The settings resolve from the environment.
"""
import asyncio
import json
import logging
import threading
import time

import pytest

from vllm_jp7_engine_lifecycle.fakes import (
    CHILD_SLEEP,
    DEFAULT_MODEL_NAME,
    BlockingFactory,
    EngineCoreLikeFactory,
    FakeEngine,
    SelfRestartRecorder,
    build_staged_repo,
    make_manager,
    run_load_on_thread,
)
from vllm_runtime import constants, memory_budget
from vllm_runtime.construction_watchdog import ENGINE_CONSTRUCTION_TIMEOUT_MARKER
from vllm_runtime.manager import (
    _NO_OFFLINE_RETRY_TOKENS,
    FAILURE_CATEGORY_TOKENS,
    ModelState,
    VllmRuntimeManager,
    classify_failure_reason,
)

MARKER_NAME = constants.CONSTRUCTION_HANG_MARKER_NAME


@pytest.fixture
def staged(tmp_path):
    model_dir = tmp_path / "vllm_model_repo"
    build_staged_repo(model_dir, DEFAULT_MODEL_NAME)
    return model_dir


def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def test_timeout_marker_is_a_category_token_with_no_offline_retry():
    assert ENGINE_CONSTRUCTION_TIMEOUT_MARKER in FAILURE_CATEGORY_TOKENS
    assert ENGINE_CONSTRUCTION_TIMEOUT_MARKER in _NO_OFFLINE_RETRY_TOKENS
    reason = "{} vLLM engine construction for 'm' made no progress".format(
        ENGINE_CONSTRUCTION_TIMEOUT_MARKER)
    assert classify_failure_reason(reason) == ENGINE_CONSTRUCTION_TIMEOUT_MARKER


def test_h2_blocked_construction_is_failed_marked_and_restarted(staged, tmp_path, caplog):
    factory = BlockingFactory()
    restart = SelfRestartRecorder()
    manager = make_manager(staged, factory, diagnostics_dir=tmp_path / "logs",
                           bound_s=30.0, stall_window_s=0.5, grace_s=0.2,
                           self_restart=restart)
    caplog.set_level(logging.WARNING)
    status, _elapsed, thread = run_load_on_thread(manager, DEFAULT_MODEL_NAME,
                                                  budget_s=0.1)
    assert status is None and factory.entered.is_set()
    try:
        # Decision 3 (a) ran while the construction is STILL blocked.
        assert restart.called.wait(10), "the self-restart never ran"
        live = manager.state(DEFAULT_MODEL_NAME)
        assert live.state is ModelState.FAILED, live
        assert live.reason.startswith(ENGINE_CONSTRUCTION_TIMEOUT_MARKER)
        assert "Unblock_Grace" in live.reason
        marker = staged / DEFAULT_MODEL_NAME / MARKER_NAME
        assert marker.is_file()
        content = json.loads(marker.read_text())
        assert content["reason"] == live.reason
        assert content["marker"] == "construction hang"
        critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert critical, "no CRITICAL log"
        assert restart.calls == 1
    finally:
        factory.release()
    thread.join(10)
    # The late return: the engine was shut down and the load stayed FAILED.
    final = manager.state(DEFAULT_MODEL_NAME)
    assert final.state is ModelState.FAILED
    assert factory.engines and factory.engines[0].shutdowns == 1


def test_late_successful_return_after_a_fired_watchdog_keeps_failed(staged, tmp_path):
    factory = BlockingFactory()
    manager = make_manager(staged, factory, diagnostics_dir=tmp_path / "logs",
                           bound_s=30.0, stall_window_s=0.3, grace_s=30.0)
    holder = {}

    def _load():
        holder["status"] = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    thread = threading.Thread(target=_load, daemon=True)
    thread.start()
    assert factory.entered.wait(5)
    # Wait until the watchdog fired (its diagnostics file exists), then let
    # the construction "succeed" late, inside the grace.
    assert _wait(lambda: list((tmp_path / "logs").glob("vllm-construction-timeout-*")))
    factory.release()
    thread.join(10)
    status = holder["status"]
    assert status.state is ModelState.FAILED
    assert status.reason.startswith(ENGINE_CONSTRUCTION_TIMEOUT_MARKER)
    assert factory.engines[0].shutdowns == 1
    assert not (staged / DEFAULT_MODEL_NAME / MARKER_NAME).exists()


def _restarted_manager(model_dir, factory=None):
    """A fresh manager over the surviving tree: the backend after its
    self-restart."""
    return make_manager(model_dir, factory or (lambda args: FakeEngine(args)))


def _write_marker(model_dir, reason):
    (model_dir / DEFAULT_MODEL_NAME / MARKER_NAME).write_text(json.dumps({
        "marker": "construction hang", "recorded_at_utc": "2026-10-02T00:00:00+00:00",
        "reason": reason}))


def test_hang_marker_reports_failed_once_and_the_reconciler_skips_it(staged, caplog):
    reason = "{} recorded hang".format(ENGINE_CONSTRUCTION_TIMEOUT_MARKER)
    _write_marker(staged, reason)
    manager = _restarted_manager(staged)
    caplog.set_level(logging.WARNING, logger="vllm_runtime.manager")
    for _ in range(3):
        status = manager.state(DEFAULT_MODEL_NAME)
        assert status.state is ModelState.FAILED and status.reason == reason
    assert manager.list_models()[DEFAULT_MODEL_NAME].state is ModelState.FAILED
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "Hang_Marker" in r.getMessage()]
    assert len(warnings) == 1

    from vllm_runtime.reconciler import VllmReconciler
    requests = []
    reconciler = VllmReconciler(manager, port=1, backoff=(),
                                request_fn=lambda url, **kw: requests.append(url))
    assert reconciler._candidates() == []
    reconciler._run()
    assert requests == []


def test_corrupt_hang_marker_still_counts(staged):
    (staged / DEFAULT_MODEL_NAME / MARKER_NAME).write_text("{not json")
    status = _restarted_manager(staged).state(DEFAULT_MODEL_NAME)
    assert status.state is ModelState.FAILED
    assert status.reason.startswith(ENGINE_CONSTRUCTION_TIMEOUT_MARKER)


def test_explicit_load_clears_the_hang_marker(staged):
    _write_marker(staged, "{} recorded".format(ENGINE_CONSTRUCTION_TIMEOUT_MARKER))
    manager = _restarted_manager(staged)
    status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert status.state is ModelState.READY
    assert not (staged / DEFAULT_MODEL_NAME / MARKER_NAME).exists()


def test_unload_tombstone_takes_precedence_over_the_hang_marker(staged):
    _write_marker(staged, "{} recorded".format(ENGINE_CONSTRUCTION_TIMEOUT_MARKER))
    manager = _restarted_manager(staged)
    assert manager.unload(DEFAULT_MODEL_NAME) is False
    assert manager.state(DEFAULT_MODEL_NAME).state is ModelState.UNLOADED


def test_a_timed_out_construction_is_not_retried_offline(staged, tmp_path, monkeypatch):
    # The offline-cache gate applies: the weights "are on disk".
    monkeypatch.setattr(memory_budget, "estimate_weights_on_disk", lambda *a, **k: 1)
    factory = EngineCoreLikeFactory(mode=CHILD_SLEEP)
    manager = make_manager(staged, factory, diagnostics_dir=tmp_path / "logs",
                           bound_s=5.0, stall_window_s=0.6, grace_s=1.0)
    try:
        status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    finally:
        factory.cleanup()
    assert status.state is ModelState.FAILED
    assert status.reason.startswith(ENGINE_CONSTRUCTION_TIMEOUT_MARKER)
    assert factory.calls == 1, "a timed-out construction was retried"


def test_an_ordinary_failure_is_still_retried_offline(staged, monkeypatch):
    monkeypatch.setattr(memory_budget, "estimate_weights_on_disk", lambda *a, **k: 1)
    calls = []

    def factory(args):
        calls.append(1)
        raise RuntimeError("an incomplete snapshot")

    status = asyncio.run(make_manager(staged, factory).load(DEFAULT_MODEL_NAME))
    assert status.state is ModelState.FAILED
    assert len(calls) == 2


def test_settings_resolve_from_the_environment(monkeypatch, staged):
    monkeypatch.setenv(constants.ENGINE_CONSTRUCTION_TIMEOUT_ENV, "120")
    monkeypatch.setenv(constants.ENGINE_STALL_WINDOW_ENV, "0")
    monkeypatch.setenv(constants.ENGINE_UNBLOCK_GRACE_ENV, "not-a-number")
    manager = VllmRuntimeManager(model_dir=staged, engine_factory=FakeEngine)
    assert manager._construction_bound_s == 120.0
    assert manager._stall_window_s == 0.0
    assert manager._unblock_grace_s == constants.DEFAULT_ENGINE_UNBLOCK_GRACE_S
    monkeypatch.delenv(constants.ENGINE_CONSTRUCTION_TIMEOUT_ENV)
    monkeypatch.setenv(constants.ENGINE_STALL_WINDOW_ENV, "-5")
    manager = VllmRuntimeManager(model_dir=staged, engine_factory=FakeEngine)
    assert manager._construction_bound_s == constants.DEFAULT_ENGINE_CONSTRUCTION_TIMEOUT_S
    assert manager._stall_window_s == constants.DEFAULT_ENGINE_STALL_WINDOW_S


def test_defaults_are_the_owner_approved_values():
    assert constants.DEFAULT_ENGINE_CONSTRUCTION_TIMEOUT_S == 600.0
    assert constants.DEFAULT_ENGINE_STALL_WINDOW_S == 120.0
    assert constants.DEFAULT_ENGINE_UNBLOCK_GRACE_S == 30.0
    # Below the component's 1500 s load request timeout and 1800 s Startup
    # timeout, with room for the component's retry (bugfix.md 3.8).
    assert (constants.DEFAULT_ENGINE_CONSTRUCTION_TIMEOUT_S
            + constants.DEFAULT_ENGINE_UNBLOCK_GRACE_S) < 1500
