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
"""B-1: an engine construction that never completes (spec
vllm-jp7-engine-lifecycle, Defect B; bugfix.md 1.3, 2.3; design Property 3).

The fake factory mimics vLLM 0.11's ``wait_for_engine_startup``: it starts a
child that looks like vLLM's engine core and sleeps forever (H1), polls the
child's liveness, and raises only when the child has died. On the unfixed
tree nothing bounds that wait, so ``load`` never returns: this test drives
the load on a worker thread and FAILS at the budget instead of hanging the
suite. On the fixed tree the Construction_Watchdog stops the engine core and
the load returns FAILED with the timeout reason, the runtime then serves the
next requests, and a diagnostics file exists.
"""
import asyncio

import pytest

from vllm_jp7_engine_lifecycle.fakes import (
    CHILD_SLEEP,
    CHILD_SPIN,
    DEFAULT_MODEL_NAME,
    EngineCoreLikeFactory,
    build_staged_repo,
    diagnostics_files,
    make_manager,
    run_load_on_thread,
)
from vllm_runtime.manager import ModelState

#: Test settings: a 1 s stall window inside a 2 s bound.
STALL_WINDOW_S = 1.0
BOUND_S = 2.0
#: Design Property 3: FAILED within the bound + one vLLM poll + 5 s; the fake
#: polls every 0.05 s, so the bound plus a 1 s margin is the test budget.
BUDGET_S = BOUND_S + 1.0

TIMEOUT_MARKER = "engine-construction-timeout:"


@pytest.fixture
def staged(tmp_path):
    model_dir = tmp_path / "vllm_model_repo"
    build_staged_repo(model_dir, DEFAULT_MODEL_NAME)
    return model_dir


def _load_hanging(staged, tmp_path, mode, **settings):
    """Returns ``(manager, factory, status, elapsed, thread, state_at_budget)``."""
    factory = EngineCoreLikeFactory(mode=mode)
    manager = make_manager(staged, factory, diagnostics_dir=tmp_path / "logs",
                           **settings)
    try:
        status, elapsed, thread = run_load_on_thread(
            manager, DEFAULT_MODEL_NAME, budget_s=BUDGET_S + 2.0)
        state_at_budget = manager.state(DEFAULT_MODEL_NAME).state
    finally:
        # Unfixed tree: the load is still blocked; killing the child is what
        # lets the fake (and the worker thread) finish.
        factory.cleanup()
    return manager, factory, status, elapsed, thread, state_at_budget


def _counterexample(what, state):
    return ("COUNTEREXAMPLE (defect 1.3): {} never returned; the model stayed "
            "{} and the runtime would serve nothing".format(what, state.value))


def test_hung_engine_core_construction_fails_within_the_stall_window(staged, tmp_path):
    """H1 shape (a live, idle engine core that never sends its handshake):
    FAILED at about the stall window, well inside the bound."""
    manager, factory, status, elapsed, thread, state = _load_hanging(
        staged, tmp_path, CHILD_SLEEP, bound_s=BOUND_S,
        stall_window_s=STALL_WINDOW_S)
    thread.join(10)
    assert status is not None, _counterexample("the construction", state)
    assert elapsed <= BUDGET_S, elapsed
    assert status.state is ModelState.FAILED
    assert status.reason.startswith(TIMEOUT_MARKER), status.reason
    assert "made no progress" in status.reason
    assert DEFAULT_MODEL_NAME in status.reason
    # The engine core was stopped: the vLLM-like wait saw it die.
    assert factory.children and factory.children[0].poll() is not None
    assert factory.raised, "vLLM-like wait never saw the engine core die"
    # Diagnostics were written and named in the reason.
    files = diagnostics_files(tmp_path / "logs")
    assert len(files) == 1
    assert str(files[0]) in status.reason
    text = files[0].read_text()
    assert "trigger: stall" in text
    assert "backend Python stacks" in text
    assert "engine core {}".format(factory.children[0].pid) in text


def test_busy_construction_fails_at_the_bound(staged, tmp_path):
    """A construction that keeps burning CPU is not a stall; the hard bound
    stops it."""
    manager, factory, status, elapsed, thread, state = _load_hanging(
        staged, tmp_path, CHILD_SPIN, bound_s=BOUND_S, stall_window_s=STALL_WINDOW_S)
    thread.join(10)
    assert status is not None, _counterexample("the busy construction", state)
    assert BOUND_S <= elapsed <= BUDGET_S, elapsed
    assert status.state is ModelState.FAILED
    assert status.reason.startswith(TIMEOUT_MARKER), status.reason
    assert "Construction_Bound" in status.reason


def test_runtime_serves_again_after_a_stopped_construction(staged, tmp_path):
    """Property 3: after the timeout the state is FAILED (not LOADING), and
    the next unload, index and load requests are served."""
    manager, factory, status, _elapsed, thread, state = _load_hanging(
        staged, tmp_path, CHILD_SLEEP, bound_s=BOUND_S,
        stall_window_s=STALL_WINDOW_S)
    thread.join(10)
    assert status is not None, _counterexample("the construction", state)
    assert status.state is ModelState.FAILED
    assert manager.state(DEFAULT_MODEL_NAME).state is ModelState.FAILED
    assert manager.list_models()[DEFAULT_MODEL_NAME].state is ModelState.FAILED

    # A later load tries again (a fresh construction, under a fresh watchdog).
    from vllm_jp7_engine_lifecycle.fakes import CHILD_READY
    factory.mode = CHILD_READY
    factory.ready_after_s = 0.2
    try:
        retry = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    finally:
        factory.cleanup()
    assert retry.state is ModelState.READY, retry
    assert factory.calls == 2
    assert manager.unload(DEFAULT_MODEL_NAME) is True
