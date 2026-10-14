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
"""Preservation property tests (Task 2) for csi-nvargus-optional.

Property 2: Preservation — Everything Outside the CSI Exposure Surface Is
Unchanged. Two property-based legs:

1. **Watchdog neutrality (3.8)**: _for any_ kernel journal stream containing
   ZERO degraded-state signature lines (arbitrary benign kernel noise,
   including near-misses — dma-attachment text alone, ``Error (89)`` without
   the NVRM ``osCreateOsDescriptorFromFileHandle`` context, the NVRM function
   name without ``Error (89)``), the watchdog performs zero
   nvargus-daemon restarts, zero state-changing systemctl calls of any kind,
   and writes zero warning-or-higher log lines. Written SKIP-AS-ABSENT (the
   ``deploy_reliability/test_defect_e_preservation.py`` pattern): the
   watchdog script does not exist on the unfixed tree; this test binds
   automatically when task 3.4 creates
   ``src/host_scripts/nvargus_error89_watchdog.sh`` and is re-run bound in
   task 3.7. It drives the REAL script with stub ``journalctl`` /
   ``systemctl`` / ``logger`` binaries on PATH (the deploy_reliability
   stub pattern) and a fake journal, with ``STATE_DIR`` overridden to a
   temp dir (design Files 4-6: constants overridable for tests).

2. **Config-change detection identity (3.2)**: _for any_ generated
   ``config.json`` contents, the capture script's ``read_config`` extracts
   exactly the values the UNFIXED script's jq logic does (gain default 4,
   exposure default 5000000, crop edges default 0; present non-null values
   pass through). Observed on the unfixed tree by SOURCING the real
   script's ``read_config`` function (extracted verbatim, driven with real
   jq); the task 3.3 rewrite preserves the function and its jq expressions
   verbatim, so this property must keep passing unchanged.

Honesty guard: no gst-launch, Argus, CUDA, or real systemd is executed —
stub binaries and real jq only.
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

HOST_SCRIPTS_DIR = os.path.join(REPO_ROOT, "src", "host_scripts")
CAPTURE_SCRIPT = os.path.join(HOST_SCRIPTS_DIR, "nvidia_csi_capture.sh")
WATCHDOG_SCRIPT = os.path.join(
    HOST_SCRIPTS_DIR, "nvargus_error89_watchdog.sh")

#: The two halves of the degraded-state signature (design Files 4-6):
#: a signature line matches SIG_NVRM; the trigger additionally requires
#: SIG_DMA presence in the same window. A journal stream with ZERO SIG_NVRM
#: matches is signature-free by construction.
_SIG_NVRM_RE = re.compile(
    r"osCreateOsDescriptorFromFileHandle.*Error \(89\)")

#: logger(1) priorities at warning or higher — the watchdog must emit NONE
#: of these on a signature-free stream (3.8: never a silent restart, but
#: also never journal spam on a healthy device).
_LOUD_PRIORITY_RE = re.compile(
    r"\.(warning|warn|err|error|crit|alert|emerg|panic)\b")

#: systemctl verbs that change system state. Read-only queries (is-active,
#: is-enabled, status, show, list-*) are tolerated — the design's trigger
#: guard may legitimately probe daemon state — but nothing may be changed.
_STATE_CHANGING_SYSTEMCTL_RE = re.compile(
    r"^\s*(restart|start|stop|reload|try-restart|enable|disable|kill"
    r"|mask|unmask|daemon-reload|daemon-reexec)\b")


def _install_stub(bin_dir, name, content):
    path = os.path.join(bin_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP
             | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# Leg 1 — watchdog neutrality on signature-free journal streams (3.8).
# SKIP-AS-ABSENT until task 3.4 lands the watchdog script; binds and must
# PASS from then on (re-run in task 3.7).
# ---------------------------------------------------------------------------

#: Benign kernel-journal noise templates plus deliberate NEAR-MISSES: each
#: line carries at most ONE signature fragment, so no line can match
#: SIG_NVRM (which needs the NVRM function name AND "Error (89)" in order).
#: Dma-attachment lines alone are also neutral: the trigger is a
#: conjunction, and with zero SIG_NVRM lines it can never fire.
_BENIGN_TEMPLATES = st.sampled_from([
    "audit: type=1400 apparmor=\"ALLOWED\" operation=\"open\"",
    "nvgpu: gv11b_fb_handle_l2tlb_ecc_isr corrected error",
    "systemd[1]: Started Daily apt download activities.",
    "CPU3: Core temperature above threshold, cpu clock throttled",
    "usb 1-2: new high-speed USB device number 4 using tegra-xusb",
    "IPv6: ADDRCONF(NETDEV_CHANGE): eth0: link becomes ready",
    "tegra-mc c0b0000.memory-controller: mc-error: unknown interrupt",
    "oom-reaper: reaped process 4242 (python3)",
])

_NEAR_MISS_TEMPLATES = st.sampled_from([
    # dma-attachment text alone (SIG_DMA without any SIG_NVRM line).
    "Can't map dma attachment!",
    "nvmap: Can't map dma attachment! (retrying)",
    # the NVRM function name WITHOUT "Error (89)".
    "NVRM: GPU0 osCreateOsDescriptorFromFileHandle: importing fd 33",
    # "Error (89)" WITHOUT the NVRM function name.
    "NVRM: nvAssertFailed: Error (89) unrelated subsystem",
    "gstnvarguscamerasrc: Error (89) generating output",
    # reversed order: "Error (89)" BEFORE the function name never matches
    # the SIG_NVRM regex.
    "NVRM: Error (89) seen near osCreateOsDescriptorFromFileHandle",
])

_RANDOM_NOISE = st.text(
    alphabet=st.characters(
        codec="ascii", categories=("L", "N", "P", "Z"),
        exclude_characters="\n\r"),
    max_size=60)

_JOURNAL_LINE = st.one_of(
    _BENIGN_TEMPLATES,
    _NEAR_MISS_TEMPLATES,
    _RANDOM_NOISE,
).filter(lambda line: not _SIG_NVRM_RE.search(line))

_JOURNAL_STREAM = st.lists(_JOURNAL_LINE, max_size=40)

_RECORDING_STUB = """\
#!/usr/bin/env bash
echo "$@" >> "{log}"
{extra}
exit 0
"""


@pytest.mark.skipif(
    not os.path.isfile(WATCHDOG_SCRIPT),
    reason="src/host_scripts/nvargus_error89_watchdog.sh does not exist yet "
           "(created by task 3.4) — the watchdog-neutrality property binds "
           "automatically once the watchdog lands (re-run bound in task "
           "3.7)")
@settings(deadline=None)
@given(journal_lines=_JOURNAL_STREAM)
def test_watchdog_takes_zero_actions_on_signature_free_journal(
        journal_lines):
    """Requirement 3.8: a healthy device's watchdog is inert — for ANY
    journal stream with zero degraded-state signature lines (benign noise
    and near-misses included), the REAL watchdog script run with stub
    journalctl/systemctl/logger performs no restart, no state-changing
    systemctl call, and emits no warning-or-higher log line.

    # Validates: Requirements 3.8
    """
    journal_text = "\n".join(journal_lines)
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = os.path.join(tmp, "bin")
        state_dir = os.path.join(tmp, "state")
        os.makedirs(bin_dir)
        os.makedirs(state_dir)

        journal_file = os.path.join(tmp, "fake_journal.txt")
        with open(journal_file, "w", encoding="utf-8") as f:
            f.write(journal_text)

        systemctl_log = os.path.join(tmp, "systemctl.log")
        logger_log = os.path.join(tmp, "logger.log")
        # journalctl serves the generated (signature-free) kernel journal
        # regardless of argv (the real script passes -k --cursor-file ...).
        _install_stub(bin_dir, "journalctl",
                      "#!/usr/bin/env bash\ncat \"$FAKE_JOURNAL\"\nexit 0\n")
        # systemctl records every invocation; is-active reports the daemon
        # ACTIVE so a buggy trigger path would be free to restart it.
        _install_stub(bin_dir, "systemctl", _RECORDING_STUB.format(
            log=systemctl_log, extra=""))
        _install_stub(bin_dir, "logger", _RECORDING_STUB.format(
            log=logger_log, extra=""))

        env = dict(os.environ)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        env["FAKE_JOURNAL"] = journal_file
        # Design Files 4-6: constants at the top of the script, all
        # overridable for tests.
        env["STATE_DIR"] = state_dir

        result = subprocess.run(
            ["bash", WATCHDOG_SCRIPT], env=env, capture_output=True,
            text=True, timeout=60)
        assert result.returncode == 0, (
            "watchdog exited {} on a signature-free journal (stderr: {!r}) "
            "— a healthy stream must be a silent exit 0"
            .format(result.returncode, result.stderr))

        systemctl_calls = []
        if os.path.exists(systemctl_log):
            with open(systemctl_log, encoding="utf-8") as f:
                systemctl_calls = f.read().splitlines()
        state_changing = [call for call in systemctl_calls
                          if _STATE_CHANGING_SYSTEMCTL_RE.search(call)]
        assert not state_changing, (
            "watchdog changed system state on a SIGNATURE-FREE journal — "
            "requirement 3.8 (healthy device untouched) violated. "
            "state-changing systemctl calls: {!r}; journal was: {!r}"
            .format(state_changing, journal_lines))

        logger_calls = []
        if os.path.exists(logger_log):
            with open(logger_log, encoding="utf-8") as f:
                logger_calls = f.read().splitlines()
        loud = [call for call in logger_calls
                if _LOUD_PRIORITY_RE.search(call)]
        assert not loud, (
            "watchdog logged at warning-or-higher on a SIGNATURE-FREE "
            "journal — healthy devices must see zero watchdog journal "
            "spam (3.8). loud logger calls: {!r}; journal was: {!r}"
            .format(loud, journal_lines))


# ---------------------------------------------------------------------------
# Leg 2 — config-change detection identity (3.2): the capture script's
# read_config extracts the same values the unfixed jq logic does, for any
# config.json contents. Drives the REAL script's read_config function,
# extracted verbatim and run with real jq.
# ---------------------------------------------------------------------------

def _extract_read_config_function():
    """The read_config() { ... } block, verbatim, from the shipped capture
    script. The unfixed script defines it at top level; the task 3.3
    rewrite preserves the function and its jq expressions verbatim (design
    File 3), so extraction works on both trees."""
    with open(CAPTURE_SCRIPT, encoding="utf-8") as f:
        lines = f.read().splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*read_config\s*\(\)\s*\{?\s*$", line):
            start = i
            break
    assert start is not None, (
        "nvidia_csi_capture.sh no longer defines read_config() — the "
        "config-read contract shape was not preserved")
    for j in range(start + 1, len(lines)):
        if re.match(r"^\}\s*$", lines[j]):
            return "\n".join(lines[start:j + 1])
    raise AssertionError(
        "could not find the closing brace of read_config() in "
        "nvidia_csi_capture.sh")


def _expected_value(config, key, default):
    """Python mirror of the unfixed jq expressions (`.gain // 4` etc.):
    jq's // yields the default when the value is absent, null, or false;
    any other value (including 0) passes through. jq -r prints integers
    unchanged."""
    value = config
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return str(default)
        value = value[part]
    if value is None or value is False:
        return str(default)
    return str(value)


#: Generated config.json contents: the keys the backend actually writes
#: (integers), each optionally absent or null; crop optionally absent
#: entirely; plus the occasional unrelated extra key. Values stay integers
#: because that is what csi_capture.write_csi_config produces (jq -r echoes
#: integers verbatim, keeping expected-value comparison exact).
_MAYBE_INT = st.one_of(st.none(), st.integers(min_value=0,
                                              max_value=683709000))
_CROP_EDGE = st.one_of(st.none(), st.integers(min_value=0, max_value=2464))

_CONFIG_OBJECTS = st.fixed_dictionaries(
    {},
    optional={
        "gain": _MAYBE_INT,
        "exposure": _MAYBE_INT,
        "crop": st.one_of(
            st.none(),
            st.fixed_dictionaries(
                {},
                optional={"top": _CROP_EDGE, "bottom": _CROP_EDGE,
                          "left": _CROP_EDGE, "right": _CROP_EDGE})),
        "comment": st.text(
            alphabet=st.characters(codec="ascii",
                                   categories=("L", "N", "Z")),
            max_size=20),
    })

_READ_CONFIG_FUNC = _extract_read_config_function()

_DRIVER_TEMPLATE = """\
set -u
USE_JQ=true
CONFIG_FILE="$1"
{func}
read_config
printf '%s\\n%s\\n%s\\n%s\\n%s\\n%s\\n' \\
    "$GAIN" "$EXPOSURE" "$CROP_TOP" "$CROP_BOTTOM" "$CROP_LEFT" "$CROP_RIGHT"
"""


@pytest.mark.skipif(
    subprocess.run(["bash", "-c", "command -v jq"],
                   capture_output=True).returncode != 0,
    reason="jq is not installed on this host — the real read_config "
           "function cannot be driven")
@settings(deadline=None)
@given(config=_CONFIG_OBJECTS)
def test_read_config_extracts_same_values_as_unfixed_jq_logic(config):
    """Requirement 3.2: for ANY config.json the backend could write, the
    shipped capture script's read_config (the REAL function, sourced
    verbatim and run with real jq) extracts exactly the unfixed values:
    gain `.gain // 4`, exposure `.exposure // 5000000`, crop edges
    `.crop.<edge> // 0` — present non-null values pass through, absent or
    null values yield the defaults. Observed on the unfixed tree; the task
    3.3 rewrite must preserve this identity.

    # Validates: Requirements 3.2
    """
    import json as _json
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            _json.dump(config, f)
        driver = _DRIVER_TEMPLATE.format(func=_READ_CONFIG_FUNC)
        result = subprocess.run(
            ["bash", "-c", driver, "read_config_driver", config_path],
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, (
            "read_config driver failed (rc={}, stderr={!r}) for config "
            "{!r}".format(result.returncode, result.stderr, config))
        got = result.stdout.splitlines()
        expected = [
            _expected_value(config, "gain", 4),
            _expected_value(config, "exposure", 5000000),
            _expected_value(config, "crop.top", 0),
            _expected_value(config, "crop.bottom", 0),
            _expected_value(config, "crop.left", 0),
            _expected_value(config, "crop.right", 0),
        ]
        assert got == expected, (
            "read_config no longer extracts the unfixed values for config "
            "{!r}: got GAIN/EXPOSURE/CROP_T/B/L/R = {!r}, unfixed jq logic "
            "yields {!r} — the config-change detection contract (gain "
            "default 4, exposure default 5000000, crop defaults 0) "
            "changed".format(config, got, expected))
