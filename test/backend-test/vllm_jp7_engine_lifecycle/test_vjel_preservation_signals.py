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
"""Preservation for Defect A's fix (spec vllm-jp7-engine-lifecycle;
bugfix.md 3.1, 3.4; design Property 2). These hold on the unfixed and the
fixed tree, on every Python.

- Property 2: a signal sent to the backend process ITSELF still runs its
  main server's handler (docker stop's SIGTERM still shuts it down).
- 3.4: a forked child left at ``SIG_DFL`` (the digital-input process) still
  dies from SIGTERM, and the backend is not signalled.
- The hook module: an idempotent install, one registration, and a
  child-side reset that tolerates ``ValueError``.
"""
import signal

import pytest

from vllm_jp7_engine_lifecycle.harness_support import run_harness


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGINT"])
def test_property2_signal_to_the_backend_itself_still_runs_its_handler(signal_name):
    outcome = run_harness("app", signal_name, child="handler", target="self")
    assert outcome["parent_fired"], outcome


def test_sig_dfl_child_still_dies_from_sigterm():
    outcome = run_harness("app", "SIGTERM", child="default", target="child")
    assert outcome["child_exitcode"] == -signal.SIGTERM, outcome
    assert not outcome["parent_fired"], outcome


def test_install_is_idempotent_and_registers_once(monkeypatch):
    from utils import fork_signal_hygiene

    calls = []
    monkeypatch.setattr(fork_signal_hygiene, "_installed", False)
    monkeypatch.setattr(fork_signal_hygiene.os, "register_at_fork",
                        lambda **kwargs: calls.append(kwargs), raising=False)
    fork_signal_hygiene.install()
    fork_signal_hygiene.install()
    assert fork_signal_hygiene.installed()
    assert len(calls) == 1
    assert calls[0] == {"after_in_child": fork_signal_hygiene._detach_signal_wakeup_fd}


def test_install_is_a_noop_without_register_at_fork(monkeypatch):
    from utils import fork_signal_hygiene

    monkeypatch.setattr(fork_signal_hygiene, "_installed", False)
    monkeypatch.delattr(fork_signal_hygiene.os, "register_at_fork", raising=False)
    fork_signal_hygiene.install()
    assert not fork_signal_hygiene.installed()


@pytest.mark.parametrize("error", [ValueError("not the main thread"), OSError("bad fd")])
def test_child_side_reset_tolerates_errors(monkeypatch, error):
    from utils import fork_signal_hygiene

    def _raise(_fd):
        raise error
    monkeypatch.setattr(fork_signal_hygiene.signal, "set_wakeup_fd", _raise)
    fork_signal_hygiene._detach_signal_wakeup_fd()  # must not raise


def test_child_side_reset_detaches_the_wakeup_fd(monkeypatch):
    from utils import fork_signal_hygiene

    seen = []
    monkeypatch.setattr(fork_signal_hygiene.signal, "set_wakeup_fd",
                        lambda fd: seen.append(fd) or -1)
    fork_signal_hygiene._detach_signal_wakeup_fd()
    assert seen == [-1]
