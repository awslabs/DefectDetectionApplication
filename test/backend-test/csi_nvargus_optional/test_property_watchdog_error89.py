# Copyright 2025 Amazon Web Services, Inc.
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
"""Watchdog behavioral property suite (Task 4.1) for csi-nvargus-optional.

Property 4: Fix Checking — Watchdog Detects, Recovers, Rate-Limits, and Logs.

Every test drives the REAL ``src/host_scripts/nvargus_error89_watchdog.sh``
as a full ``bash`` invocation with stub ``journalctl`` / ``systemctl`` /
``logger`` binaries on PATH recording their argv (the ``deploy_reliability``
stub pattern), a temp ``STATE_DIR``, and the script's ``WATCHDOG_NOW`` epoch
override for time-window control. Generated journal streams come from
Hypothesis; threshold/interval constants are env-overridden where that keeps
generated cases small (design Files 4-6: all constants overridable for
tests).

Covered (task 4.1 text):

- ≥ SIG_THRESHOLD new signature lines (both signatures) + active daemon +
  no recent restart → exactly one ``systemctl restart nvargus-daemon`` +
  a warning-or-higher log naming counts and action (2.9, 2.11)
- threshold met within RESTART_MIN_INTERVAL of a recorded restart → zero
  restarts + a suppression log (2.10)
- escalation: ≥ ESCALATION_COUNT restarts inside ESCALATION_WINDOW →
  restarts stop, persistent error every scan (2.10, 2.11)
- inactive/disabled daemon (``systemctl is-active`` exits 3) → no restart
- threshold boundary (SIG_THRESHOLD-1 vs SIG_THRESHOLD) and
  one-signature-only near-misses → no action
- cursor discipline: the same journal lines are never counted twice across
  consecutive full-script runs (stub journalctl honoring ``--cursor-file``
  semantics)
- state-file corruption (garbage in last_restart_epoch / restart_history)
  tolerated: treated as zero/empty, no crash

Honesty guard: no real journalctl, systemd, or nvargus is touched — stub
binaries only.

Validates: Requirements 2.9, 2.10, 2.11, 3.8
"""
import os
import re
import stat
import subprocess
import tempfile

import pytest
from hypothesis import given, settings, strategies as st

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

WATCHDOG_SCRIPT = os.path.join(
    REPO_ROOT, "src", "host_scripts", "nvargus_error89_watchdog.sh")

#: A line matching the script's SIG_NVRM default
#: (``osCreateOsDescriptorFromFileHandle.*Error (89)``) — the real Thor
#: kernel signature text.
NVRM_LINE = ("NVRM: GPU0 osCreateOsDescriptorFromFileHandle: Error (89) "
             "while trying to import fd")
#: A line matching SIG_DMA (``Can't map dma attachment``).
DMA_LINE = "Can't map dma attachment!"

#: Benign kernel noise: matches NEITHER signature.
_BENIGN = st.sampled_from([
    "systemd[1]: Started Daily apt download activities.",
    "usb 1-2: new high-speed USB device number 4 using tegra-xusb",
    "IPv6: ADDRCONF(NETDEV_CHANGE): eth0: link becomes ready",
    "nvgpu: gv11b_fb_handle_l2tlb_ecc_isr corrected error",
    "CPU3: Core temperature above threshold, cpu clock throttled",
    "oom-reaper: reaped process 4242 (python3)",
])

_SYSTEMCTL_STUB = """\
#!/usr/bin/env bash
echo "$@" >> "$SYSTEMCTL_LOG"
if [ "${1:-}" = "is-active" ]; then
    exit "${IS_ACTIVE_RC:-0}"
fi
exit 0
"""

_LOGGER_STUB = """\
#!/usr/bin/env bash
echo "$@" >> "$LOGGER_LOG"
exit 0
"""

#: journalctl stub that serves the fake journal on EVERY invocation
#: (cursor ignored) — used by the single-scan properties.
_JOURNALCTL_PLAIN = """\
#!/usr/bin/env bash
cat "$FAKE_JOURNAL"
exit 0
"""

#: journalctl stub honoring --cursor-file semantics: the first invocation
#: emits the fake journal and seeds the cursor file; every later invocation
#: (cursor file present) emits nothing new — exactly journalctl's
#: incremental-scan contract the watchdog relies on.
_JOURNALCTL_CURSOR = """\
#!/usr/bin/env bash
cursor=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "--cursor-file" ]; then
        cursor="$arg"
    fi
    prev="$arg"
done
if [ -n "$cursor" ] && [ -f "$cursor" ]; then
    exit 0
fi
cat "$FAKE_JOURNAL"
if [ -n "$cursor" ]; then
    echo "s=seeded" > "$cursor"
fi
exit 0
"""


class _Harness:
    """Stub-binary harness around a full ``bash nvargus_error89_watchdog.sh``
    run. Reusable across runs (cursor/state/logs persist) for the
    cursor-discipline and escalation multi-scan tests."""

    def __init__(self, tmp, journalctl_stub=_JOURNALCTL_PLAIN):
        self.tmp = tmp
        self.bin_dir = os.path.join(tmp, "bin")
        self.state_dir = os.path.join(tmp, "state")
        os.makedirs(self.bin_dir)
        os.makedirs(self.state_dir)
        self.journal_file = os.path.join(tmp, "fake_journal.txt")
        self.systemctl_log = os.path.join(tmp, "systemctl.log")
        self.logger_log = os.path.join(tmp, "logger.log")
        self._install("journalctl", journalctl_stub)
        self._install("systemctl", _SYSTEMCTL_STUB)
        self._install("logger", _LOGGER_STUB)

    def _install(self, name, content):
        path = os.path.join(self.bin_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR
                 | stat.S_IXGRP | stat.S_IXOTH)

    def write_journal(self, lines):
        with open(self.journal_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def seed_state(self, last_restart_epoch=None, restart_history=None):
        if last_restart_epoch is not None:
            with open(os.path.join(self.state_dir, "last_restart_epoch"),
                      "w", encoding="utf-8") as f:
                f.write("{}\n".format(last_restart_epoch))
        if restart_history is not None:
            with open(os.path.join(self.state_dir, "restart_history"),
                      "w", encoding="utf-8") as f:
                f.write("".join("{}\n".format(e) for e in restart_history))

    def run(self, now, is_active_rc=0, env_overrides=None):
        env = dict(os.environ)
        env["PATH"] = self.bin_dir + os.pathsep + env.get("PATH", "")
        env["FAKE_JOURNAL"] = self.journal_file
        env["SYSTEMCTL_LOG"] = self.systemctl_log
        env["LOGGER_LOG"] = self.logger_log
        env["IS_ACTIVE_RC"] = str(is_active_rc)
        env["STATE_DIR"] = self.state_dir
        env["WATCHDOG_NOW"] = str(now)
        if env_overrides:
            env.update({k: str(v) for k, v in env_overrides.items()})
        return subprocess.run(
            ["bash", WATCHDOG_SCRIPT], env=env, capture_output=True,
            text=True, timeout=60)

    @staticmethod
    def _read_log(path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()

    def systemctl_calls(self):
        return self._read_log(self.systemctl_log)

    def restart_calls(self):
        return [c for c in self.systemctl_calls()
                if re.match(r"^restart\b", c)]

    def logger_calls(self):
        return self._read_log(self.logger_log)

    def state_file(self, name):
        path = os.path.join(self.state_dir, name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return f.read()


_NOW = 1_000_000  # deterministic "current" epoch (WATCHDOG_NOW override)


@st.composite
def _triggering_journal(draw):
    """A journal window that MUST trigger: threshold t (env-overridden,
    kept small so generated cases stay small), nvrm_count >= t NVRM
    signature lines, >= 1 dma-attachment line, benign noise interleaved."""
    threshold = draw(st.integers(min_value=1, max_value=4))
    nvrm_count = threshold + draw(st.integers(min_value=0, max_value=4))
    dma_count = draw(st.integers(min_value=1, max_value=3))
    noise = draw(st.lists(_BENIGN, max_size=6))
    lines = draw(st.permutations(
        [NVRM_LINE] * nvrm_count + [DMA_LINE] * dma_count + noise))
    return threshold, nvrm_count, dma_count, list(lines)


# ---------------------------------------------------------------------------
# Restart leg (2.9, 2.11): threshold met + both signatures + active daemon +
# no recorded restart → exactly ONE restart, loudly logged with the counts.
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(trigger=_triggering_journal())
def test_threshold_met_active_daemon_no_recent_restart_restarts_exactly_once(
        trigger):
    """Requirements 2.9 + 2.11: for ANY journal window with >= SIG_THRESHOLD
    new NVRM signature lines AND the dma-attachment signature, an active
    daemon, and no recorded prior restart, the watchdog performs exactly one
    `systemctl restart nvargus-daemon`, records the restart epoch, and logs
    ONE warning-or-higher line naming the counts and the action.

    # Validates: Requirements 2.9, 2.11
    """
    threshold, nvrm_count, dma_count, lines = trigger
    with tempfile.TemporaryDirectory() as tmp:
        h = _Harness(tmp)
        h.write_journal(lines)
        result = h.run(_NOW, env_overrides={"SIG_THRESHOLD": threshold})

        assert result.returncode == 0, (
            "watchdog exited {} (stderr: {!r})".format(
                result.returncode, result.stderr))
        restarts = h.restart_calls()
        assert restarts == ["restart nvargus-daemon"], (
            "expected exactly one `systemctl restart nvargus-daemon` for "
            "{} NVRM + {} dma lines (threshold {}), got restart calls {!r} "
            "(all systemctl calls: {!r})".format(
                nvrm_count, dma_count, threshold, restarts,
                h.systemctl_calls()))
        loud = [c for c in h.logger_calls()
                if re.search(r"-p daemon\.(err|warning)\b", c)]
        assert len(loud) == 1, (
            "expected exactly one warning-or-higher log line, got {!r}"
            .format(h.logger_calls()))
        assert "-p daemon.err" in loud[0]
        assert "{} new Error(89) lines".format(nvrm_count) in loud[0], (
            "restart log does not name the NVRM count: {!r}".format(loud[0]))
        assert "{} dma-attachment lines".format(dma_count) in loud[0], (
            "restart log does not name the dma count: {!r}".format(loud[0]))
        assert "restarting nvargus-daemon" in loud[0], (
            "restart log does not name the action: {!r}".format(loud[0]))
        # The restart epoch is recorded (feeds the 2.10 rate-limit).
        assert (h.state_file("last_restart_epoch") or "").strip() == \
            str(_NOW), "restart epoch was not recorded"


# ---------------------------------------------------------------------------
# Rate-limit leg (2.10): threshold met INSIDE RESTART_MIN_INTERVAL of the
# recorded restart → zero restarts + a suppression log.
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(trigger=_triggering_journal(),
       elapsed=st.integers(min_value=0, max_value=599))
def test_threshold_met_inside_min_interval_suppresses_with_log(
        trigger, elapsed):
    """Requirement 2.10: for ANY triggering journal window arriving within
    RESTART_MIN_INTERVAL (default 600s) of a recorded automatic restart, the
    watchdog performs ZERO restarts and logs the suppression (with counts)
    at warning-or-higher.

    # Validates: Requirements 2.10, 2.11
    """
    threshold, nvrm_count, dma_count, lines = trigger
    with tempfile.TemporaryDirectory() as tmp:
        h = _Harness(tmp)
        h.write_journal(lines)
        last = _NOW - elapsed
        h.seed_state(last_restart_epoch=last, restart_history=[last])
        result = h.run(_NOW, env_overrides={"SIG_THRESHOLD": threshold})

        assert result.returncode == 0
        assert h.restart_calls() == [], (
            "restart fired {}s after the last automatic restart — inside "
            "RESTART_MIN_INTERVAL=600s the watchdog must suppress. "
            "systemctl calls: {!r}".format(elapsed, h.systemctl_calls()))
        loud = [c for c in h.logger_calls()
                if re.search(r"-p daemon\.(err|warning)\b", c)]
        assert len(loud) == 1 and "-p daemon.warning" in loud[0], (
            "expected exactly one daemon.warning suppression log, got {!r}"
            .format(h.logger_calls()))
        assert "restart suppressed" in loud[0], (
            "suppression log does not name the action: {!r}".format(loud[0]))
        assert "{} new Error(89) lines".format(nvrm_count) in loud[0]
        assert "{} dma-attachment lines".format(dma_count) in loud[0]
        # The recorded epoch is untouched — no restart happened.
        assert (h.state_file("last_restart_epoch") or "").strip() == \
            str(last)


# ---------------------------------------------------------------------------
# Escalation leg (2.10, 2.11): >= ESCALATION_COUNT restarts inside
# ESCALATION_WINDOW → restarts STOP; persistent error logged EVERY scan.
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(trigger=_triggering_journal(),
       # The second scan runs at _NOW + 30; a history entry must stay inside
       # ESCALATION_WINDOW (3600s) at BOTH scan times for the escalation
       # precondition to hold on every asserted scan, so offsets are capped
       # at 3600 - 30 = 3570. (An entry aging OUT of the window between
       # scans legitimately de-escalates the watchdog — the sliding window
       # is the recovery path, not a defect.)
       offsets=st.lists(st.integers(min_value=0, max_value=3570),
                        min_size=3, max_size=6))
def test_escalation_stops_restarts_and_logs_persistent_error_every_scan(
        trigger, offsets):
    """Requirements 2.10 + 2.11: once >= ESCALATION_COUNT (default 3)
    automatic restarts are recorded inside ESCALATION_WINDOW (default
    3600s), the watchdog stops restarting entirely and logs a persistent
    daemon.err naming the condition on EVERY subsequent scan — even scans
    whose window meets the trigger threshold again.

    # Validates: Requirements 2.10, 2.11
    """
    threshold, nvrm_count, dma_count, lines = trigger
    history = [_NOW - off for off in offsets]  # all inside the window
    with tempfile.TemporaryDirectory() as tmp:
        h = _Harness(tmp)  # plain journalctl: SAME window served each scan
        h.write_journal(lines)
        h.seed_state(last_restart_epoch=max(history),
                     restart_history=sorted(history))

        for scan in range(2):  # "every scan": two consecutive scans
            result = h.run(_NOW + scan * 30,
                           env_overrides={"SIG_THRESHOLD": threshold})
            assert result.returncode == 0

        assert h.restart_calls() == [], (
            "escalated watchdog still restarted the daemon — after {} "
            "restarts inside ESCALATION_WINDOW automatic restarts must "
            "stop. systemctl calls: {!r}".format(
                len(history), h.systemctl_calls()))
        errors = [c for c in h.logger_calls() if "-p daemon.err" in c]
        assert len(errors) == 2, (
            "expected the persistent escalation error on EVERY scan (2 "
            "scans), got logger calls {!r}".format(h.logger_calls()))
        for line in errors:
            assert "automatic restarts suppressed" in line, (
                "escalation log does not name the condition: {!r}"
                .format(line))
            assert "manual intervention required" in line


# ---------------------------------------------------------------------------
# Inactive-daemon guard: trigger threshold met but nvargus-daemon is not
# active → no restart attempted (a stopped daemon holds no poisoned state).
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(trigger=_triggering_journal())
def test_inactive_daemon_never_restarted(trigger):
    """Requirement 2.9 (guard leg): the restart fires only if
    nvargus-daemon is active. With `systemctl is-active` exiting 3
    (inactive/disabled), ANY triggering window produces zero restarts; the
    detection is still visibly logged.

    # Validates: Requirements 2.9, 2.11
    """
    threshold, nvrm_count, dma_count, lines = trigger
    with tempfile.TemporaryDirectory() as tmp:
        h = _Harness(tmp)
        h.write_journal(lines)
        result = h.run(_NOW, is_active_rc=3,
                       env_overrides={"SIG_THRESHOLD": threshold})

        assert result.returncode == 0
        assert h.restart_calls() == [], (
            "watchdog restarted an INACTIVE nvargus-daemon. systemctl "
            "calls: {!r}".format(h.systemctl_calls()))
        # The is-active probe is the read-only guard that was consulted.
        assert any(c.startswith("is-active") for c in h.systemctl_calls())
        loud = [c for c in h.logger_calls()
                if re.search(r"-p daemon\.(err|warning)\b", c)]
        assert len(loud) == 1 and "no restart attempted" in loud[0], (
            "expected one visible not-active log line, got {!r}"
            .format(h.logger_calls()))
        assert h.state_file("last_restart_epoch") is None, (
            "no restart happened, yet a restart epoch was recorded")


# ---------------------------------------------------------------------------
# Threshold boundary and one-signature-only near-misses → NO action.
# ---------------------------------------------------------------------------

@st.composite
def _non_triggering_journal(draw):
    """A window that must NOT trigger: below threshold, or missing one half
    of the two-signature conjunction entirely."""
    threshold = draw(st.integers(min_value=1, max_value=4))
    kind = draw(st.sampled_from(["below-threshold", "nvrm-only",
                                 "dma-only"]))
    if kind == "below-threshold":
        nvrm_count = draw(st.integers(min_value=0, max_value=threshold - 1))
        dma_count = draw(st.integers(min_value=0, max_value=3))
    elif kind == "nvrm-only":  # >= threshold NVRM lines but ZERO dma lines
        nvrm_count = threshold + draw(st.integers(min_value=0, max_value=4))
        dma_count = 0
    else:  # dma lines only, zero NVRM lines
        nvrm_count = 0
        dma_count = draw(st.integers(min_value=1, max_value=5))
    noise = draw(st.lists(_BENIGN, max_size=6))
    lines = draw(st.permutations(
        [NVRM_LINE] * nvrm_count + [DMA_LINE] * dma_count + noise))
    return threshold, list(lines)


@settings(deadline=None)
@given(non_trigger=_non_triggering_journal())
def test_below_threshold_or_single_signature_takes_no_action(non_trigger):
    """Requirement 2.9 (threshold and conjunction legs): for ANY window
    below SIG_THRESHOLD, or with only one of the two signatures present
    (NVRM lines without any dma-attachment line, or vice versa), the
    watchdog takes NO action at all: zero systemctl calls, zero log lines,
    exit 0.

    # Validates: Requirements 2.9, 3.8
    """
    threshold, lines = non_trigger
    with tempfile.TemporaryDirectory() as tmp:
        h = _Harness(tmp)
        h.write_journal(lines)
        result = h.run(_NOW, env_overrides={"SIG_THRESHOLD": threshold})

        assert result.returncode == 0, (
            "watchdog exited {} on a non-triggering window (stderr: {!r})"
            .format(result.returncode, result.stderr))
        assert h.systemctl_calls() == [], (
            "non-triggering window produced systemctl calls: {!r}"
            .format(h.systemctl_calls()))
        assert h.logger_calls() == [], (
            "non-triggering window produced log lines: {!r}"
            .format(h.logger_calls()))


def test_threshold_boundary_minus_one_is_silent_at_exactly_fires():
    """Requirement 2.9 boundary: with the default SIG_THRESHOLD=3, a window
    with exactly 2 NVRM lines (+ dma) takes no action; exactly 3 NVRM lines
    (+ dma) restarts.

    # Validates: Requirements 2.9
    """
    # SIG_THRESHOLD - 1 → silent.
    with tempfile.TemporaryDirectory() as tmp:
        h = _Harness(tmp)
        h.write_journal([NVRM_LINE] * 2 + [DMA_LINE])
        result = h.run(_NOW)  # default SIG_THRESHOLD=3
        assert result.returncode == 0
        assert h.systemctl_calls() == []
        assert h.logger_calls() == []
    # Exactly SIG_THRESHOLD → one restart.
    with tempfile.TemporaryDirectory() as tmp:
        h = _Harness(tmp)
        h.write_journal([NVRM_LINE] * 3 + [DMA_LINE])
        result = h.run(_NOW)
        assert result.returncode == 0
        assert h.restart_calls() == ["restart nvargus-daemon"]


# ---------------------------------------------------------------------------
# Cursor discipline: the same journal lines are never counted twice across
# consecutive scans (full-script runs; stub journalctl honors --cursor-file).
# ---------------------------------------------------------------------------

def test_cursor_discipline_same_lines_never_counted_twice_across_scans():
    """Requirement 2.9 (incremental-scan leg): drive the FULL script twice
    against a stub journalctl that honors --cursor-file semantics (first
    run returns the signature window and seeds the cursor; the second run
    returns nothing new). With rate-limiting disabled
    (RESTART_MIN_INTERVAL=0), a re-count of the same lines WOULD restart
    again — so exactly one restart total proves each line is counted
    exactly once across scans.

    # Validates: Requirements 2.9
    """
    with tempfile.TemporaryDirectory() as tmp:
        h = _Harness(tmp, journalctl_stub=_JOURNALCTL_CURSOR)
        h.write_journal(
            [NVRM_LINE] * 4 + [DMA_LINE] * 2
            + ["systemd[1]: Started Daily apt download activities."])
        overrides = {"RESTART_MIN_INTERVAL": 0}

        result1 = h.run(_NOW, env_overrides=overrides)
        assert result1.returncode == 0
        assert h.restart_calls() == ["restart nvargus-daemon"], (
            "first scan should have restarted once; systemctl calls: {!r}"
            .format(h.systemctl_calls()))
        cursor = os.path.join(h.state_dir, "cursor")
        assert os.path.isfile(cursor), (
            "the scan did not maintain the cursor file — incremental "
            "journal reads are broken")

        # Second scan, later: journalctl (honoring the cursor) returns
        # nothing new, so nothing may happen — no second restart, no log.
        result2 = h.run(_NOW + 120, env_overrides=overrides)
        assert result2.returncode == 0
        assert h.restart_calls() == ["restart nvargus-daemon"], (
            "the SAME journal lines were counted twice across consecutive "
            "scans — one restart total expected, got systemctl calls {!r}"
            .format(h.systemctl_calls()))
        loud = [c for c in h.logger_calls()
                if re.search(r"-p daemon\.(err|warning)\b", c)]
        assert len(loud) == 1, (
            "second (empty) scan produced extra log lines: {!r}"
            .format(h.logger_calls()))


# ---------------------------------------------------------------------------
# State-file corruption tolerance: garbage state is treated as zero/empty —
# the watchdog neither crashes nor loses its ability to act.
# ---------------------------------------------------------------------------

_GARBAGE = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1, max_size=40,
).filter(lambda s: not re.fullmatch(r"[0-9]+", s.strip()))


@settings(deadline=None)
@given(trigger=_triggering_journal(),
       last_garbage=_GARBAGE,
       history_garbage=st.lists(_GARBAGE, min_size=1, max_size=5))
def test_state_file_corruption_treated_as_empty_no_crash(
        trigger, last_garbage, history_garbage):
    """Robustness leg (task 4.1): ANY non-numeric garbage in
    last_restart_epoch and restart_history is treated as zero/empty state —
    the scan exits 0 and a triggering window still restarts exactly once
    (garbage neither crashes the script, nor fakes a recent restart, nor
    fakes escalation).

    # Validates: Requirements 2.9, 2.10
    """
    threshold, nvrm_count, dma_count, lines = trigger
    with tempfile.TemporaryDirectory() as tmp:
        h = _Harness(tmp)
        h.write_journal(lines)
        with open(os.path.join(h.state_dir, "last_restart_epoch"), "w",
                  encoding="utf-8") as f:
            f.write(last_garbage + "\n")
        with open(os.path.join(h.state_dir, "restart_history"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(history_garbage) + "\n")

        result = h.run(_NOW, env_overrides={"SIG_THRESHOLD": threshold})

        assert result.returncode == 0, (
            "watchdog crashed (exit {}) on corrupted state files. stderr: "
            "{!r}; last_restart_epoch={!r}, restart_history={!r}".format(
                result.returncode, result.stderr, last_garbage,
                history_garbage))
        assert h.restart_calls() == ["restart nvargus-daemon"], (
            "corrupted state must be treated as zero/empty (no prior "
            "restart, no escalation): a triggering window should restart "
            "exactly once. systemctl calls: {!r}, logger: {!r}".format(
                h.systemctl_calls(), h.logger_calls()))
        # The corrupt marker is replaced by the real epoch after the
        # restart is recorded.
        assert (h.state_file("last_restart_epoch") or "").strip() == \
            str(_NOW)
