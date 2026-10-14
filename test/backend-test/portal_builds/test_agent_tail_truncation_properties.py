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
"""
Tail-preserving durable-message truncation properties
(build-fleet-execution-failures task 7.4).

**Property 18: Tail-Preserving Truncation** - for any failure line
below, at, or above the durable-message byte bound, the derived bounded
message preserves the trailing (root-cause) content of over-length lines
while in-bound content is byte-identical to the pre-change behavior.

**Validates: Requirements 2.22, 3.15**

Evidence gate (historical-evidence.md task 3.3, row 9 — CONFIRMED):
JP6 ephemeral job ``bd91c5d8-ac7e-4125-becc-711860660f2e`` failed with
ENOSPC during concurrent docker layer extraction, and the agent's former
``tail -n 5 "$BUILD_LOG" | head -c 512`` derivation cut the durable
message mid-path at ``write /var/snap/docker/common/``, dropping the
trailing ``no space left on device`` root cause. Task 7.4 changed the
derivation to tail-preserving semantics (``tail -c 512``).

Two seams are tested:

  1. the agent's ACTUAL shell pipeline, extracted verbatim from
     ``scripts/portal-build-agent.sh`` (the same technique the frozen
     task-14 exploration uses) and run against fixture logs in a temp
     dir — no agent process, build, AWS call, or instance is involved;
  2. the backend byte-bounding primitive
     ``build_reconciliation.bound_tail_text`` (task 4.1), which durable
     message derivation flows through.

Run ONLY this file, from the repository root, with a finite non-watch
command (this run contains property-based tests and may generate/shrink
counterexamples):

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
        test/backend-test/portal_builds/test_agent_tail_truncation_properties.py \\
        --noconftest -q
"""
import os
import re
import subprocess
import sys

from hypothesis import HealthCheck, given, settings, strategies as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_reconciliation as br  # noqa: E402

_AGENT_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "portal-build-agent.sh")

#: The durable-message byte bound of the agent's error-tail derivation.
BOUND = 512

#: The decisive trailing root cause of the retained bd91c5d8 record.
ENOSPC_CAUSE = "no space left on device"
#: Where the retained record's head-keeping cut landed (mid-path).
HEAD_KEPT_END = "write /var/snap/docker/common/"

_LAYER_SHA = "265a67" + "f" * 58
BUILDKIT_ENOSPC_LINE = (
    f"#109 ERROR: failed to extract layer sha256:{_LAYER_SHA}: "
    f"{HEAD_KEPT_END}"
    "var-lib-docker/containerd/io.containerd.snapshotter.v1.overlayfs/"
    "snapshots/384/fs/usr/local/cuda-12.6/targets/aarch64-linux/lib/"
    "libcudnn_engines_precompiled.so.9.1.0: failed to write file: "
    "write /var/snap/docker/common/var-lib-docker/containerd/"
    f"io.containerd.content.v1.content/ingest/{_LAYER_SHA}/data: "
    + ENOSPC_CAUSE
)


def _agent_error_tail_pipeline():
    """The exact ``ERROR_TAIL=$( ... )`` pipeline from the agent script,
    so this test exercises the SAME semantics production uses."""
    with open(_AGENT_SCRIPT, encoding="utf-8") as handle:
        for line in handle:
            match = re.match(r"\s*ERROR_TAIL=\$\((.+)\)\s*$", line)
            if match:
                return match.group(1)
    raise AssertionError(
        "ERROR_TAIL derivation not found in scripts/portal-build-agent.sh")


def _derive(build_log_path):
    """Run the agent's error-tail pipeline against a fixture log."""
    completed = subprocess.run(
        ["bash", "-c", _agent_error_tail_pipeline()],
        env={**os.environ, "BUILD_LOG": build_log_path},
        capture_output=True, timeout=30, check=False)
    return completed.stdout.decode("utf-8", errors="replace")


def _write_log(tmp_dir, lines):
    path = os.path.join(str(tmp_dir), "build.log")
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")
    return path


def _tail_five_stream(lines):
    """What ``tail -n 5`` of the log yields (the derivation's input)."""
    return "".join(line + "\n" for line in lines[-5:])


# an ASCII alphabet keeps byte arithmetic exact (1 char == 1 byte)
_LINE_TEXT = st.text(alphabet="abcdefghij #:/.-0123456789", min_size=0,
                     max_size=60)


class TestProperty18TailPreservingTruncation:
    """**Property 18: Tail-Preserving Truncation**

    **Validates: Requirements 2.22, 3.15**
    """

    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(overshoot=st.integers(min_value=1, max_value=2048),
           pad_char=st.sampled_from(["a", "x", "/", ".", "0"]),
           previous_lines=st.lists(_LINE_TEXT, min_size=0, max_size=4))
    def test_over_bound_lines_preserve_the_trailing_root_cause(
            self, tmp_path_factory, overshoot, pad_char, previous_lines):
        """ABOVE the bound: the root-cause END of the over-length line
        survives into the derived durable message (Req 2.22)."""
        suffix = ": " + ENOSPC_CAUSE
        line = ("#7 ERROR: failed to commit snapshot: write /"
                + pad_char * (BOUND + overshoot)) + suffix
        assert len(line.encode("utf-8")) > BOUND
        log = _write_log(tmp_path_factory.mktemp("p18-over"),
                         previous_lines + [line])
        derived = _derive(log)
        assert len(derived.encode("utf-8")) <= BOUND
        assert ENOSPC_CAUSE in derived, (
            f"trailing root cause dropped; derived ends "
            f"...{derived[-60:]!r}")

    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(lines=st.lists(_LINE_TEXT, min_size=1, max_size=5))
    def test_in_bound_content_is_byte_identical(self, tmp_path_factory,
                                                lines):
        """BELOW the bound: the derived message is byte-identical to the
        pre-change behavior (the full ``tail -n 5`` stream — Req 3.15:
        only which end of OVER-length content is retained changed)."""
        stream = _tail_five_stream(lines)
        if len(stream.encode("utf-8")) > BOUND:
            return  # this case belongs to the over-bound property
        log = _write_log(tmp_path_factory.mktemp("p18-in"), lines)
        assert _derive(log) == stream

    def test_exactly_at_the_bound_is_unchanged(self, tmp_path):
        """AT the bound: content of exactly BOUND bytes is untouched."""
        # one line whose line+newline stream is exactly BOUND bytes
        line = "b" * (BOUND - 1)
        log = _write_log(tmp_path, [line])
        stream = _tail_five_stream([line])
        assert len(stream.encode("utf-8")) == BOUND
        assert _derive(log) == stream

    def test_observed_buildkit_fixture_preserves_the_cause(self, tmp_path):
        """The retained bd91c5d8-shaped over-length buildkit failure
        line: the durable message now ends with the ENOSPC cause instead
        of the mid-path cut at ``write /var/snap/docker/common/``."""
        assert len(BUILDKIT_ENOSPC_LINE.encode("utf-8")) > BOUND
        log = _write_log(tmp_path, [
            "#108 exporting layers",
            "#108 exporting layers 94.9s done",
            "#109 extracting sha256:" + _LAYER_SHA,
            BUILDKIT_ENOSPC_LINE,
        ])
        derived = _derive(log)
        assert len(derived.encode("utf-8")) <= BOUND
        assert derived.rstrip("\n").endswith(ENOSPC_CAUSE)

    # ------------------------------------------------------------------
    # Backend primitive: durable message derivation through the task 4.1
    # bounding primitives preserves the root-cause end of bounded lines.
    # ------------------------------------------------------------------

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(payload=st.text(min_size=0, max_size=2048),
           limit=st.integers(min_value=8, max_value=1024))
    def test_bound_tail_text_keeps_the_tail_and_the_bound(self, payload,
                                                          limit):
        """``bound_tail_text``: within the bound the text is unchanged
        (Req 3.15); above it, only trailing bytes are kept, within the
        limit (Req 2.22)."""
        bounded = br.bound_tail_text(payload, limit)
        raw = payload.encode("utf-8")
        assert len(bounded.text.encode("utf-8")) <= limit
        assert bounded.original_bytes == len(raw)
        if len(raw) <= limit:
            assert bounded.truncated is False
            assert bounded.text == payload
        else:
            assert bounded.truncated is True
            # the kept text is a suffix of the original
            assert raw.decode("utf-8", "ignore").endswith(bounded.text)

    def test_bound_tail_text_preserves_the_enospc_cause(self):
        bounded = br.bound_tail_text(BUILDKIT_ENOSPC_LINE, BOUND)
        assert bounded.truncated is True
        assert bounded.text.endswith(ENOSPC_CAUSE)
