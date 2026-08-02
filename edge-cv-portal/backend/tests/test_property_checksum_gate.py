"""Property test for the bootstrap checksum gate (station-quick-setup task 7.3).

**Feature: station-quick-setup, Property 12: The checksum gate executes if and
only if content verifies**

For any bundle content bytes, the real ``station_install/quick_setup/bootstrap.sh``
executes the bundle exactly when the downloaded bytes hash to the manifest
checksum; for any tampered content (any byte-level mutation, or a manifest
checksum that does not match the served bytes), it executes no part of the
bundle, reports an integrity error, and exits non-zero.

**Validates: Requirements 4.8, 4.9**

Approach (per design testing strategy for Property 12): a Hypothesis test that
generates random bundle bytes and random tamperings and drives the *real*
``bootstrap.sh`` via ``subprocess``. The station-facing external commands the
script shells out to are stubbed on ``PATH`` so the checksum gate is exercised
in isolation, regardless of the host:

* ``curl`` is stubbed to (1) answer the ``POST /bundle`` manifest request with a
  generated manifest JSON and HTTP 200, and (2) answer the bundle download by
  writing the *served* bundle bytes to the requested output file. This is the
  "local HTTP stub" role, implemented hermetically so the test never touches a
  real network.
* ``id`` / ``lsb_release`` / ``uname`` / ``df`` are stubbed so the preflight
  prerequisite checks (root, supported Ubuntu, supported arch, free disk) pass
  deterministically and control reaches the checksum gate.

The bundle is a real ``tar.gz`` whose ``quick_setup/run.sh`` — the entrypoint
``bootstrap.sh`` ``exec``s after a successful verify — writes a sentinel file.
The sentinel is the ground truth for "the bundle executed": it exists after a
run iff any part of the bundle ran.
"""
from __future__ import annotations

import hashlib
import io
import os
import stat
import subprocess
import tarfile
import tempfile

from hypothesis import assume, given, settings
from hypothesis import strategies as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
BOOTSTRAP = os.path.join(_REPO_ROOT, "station_install", "quick_setup", "bootstrap.sh")

# run.sh writes this marker; its presence == "the bundle executed".
_RUN_SH = (
    "#!/bin/bash\n"
    'if [ -n "${SENTINEL_FILE:-}" ]; then echo executed > "$SENTINEL_FILE"; fi\n'
)


def _build_bundle(payload: bytes) -> bytes:
    """A real tar.gz containing quick_setup/run.sh (sentinel writer) plus a
    data member carrying ``payload`` so the bundle bytes — and therefore the
    correct checksum — vary across examples."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        run_bytes = _RUN_SH.encode()
        run_info = tarfile.TarInfo("quick_setup/run.sh")
        run_info.size = len(run_bytes)
        run_info.mode = 0o755
        tar.addfile(run_info, io.BytesIO(run_bytes))

        data_info = tarfile.TarInfo("quick_setup/payload.bin")
        data_info.size = len(payload)
        tar.addfile(data_info, io.BytesIO(payload))
    return buf.getvalue()


def _write_exec(path: str, contents: str) -> None:
    with open(path, "w") as handle:
        handle.write(contents)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_stub_bin() -> str:
    """Create a directory of PATH stubs that let bootstrap.sh reach the
    checksum gate deterministically. Returned dir is prepended to PATH."""
    stub_dir = tempfile.mkdtemp(prefix="qs-stub-bin-")

    # curl: POST /bundle -> manifest + "200"; GET download -> served bytes.
    _write_exec(os.path.join(stub_dir, "curl"), r"""#!/usr/bin/env bash
out=""
is_post=0
prev=""
for arg in "$@"; do
    if [ "$prev" = "-o" ]; then out="$arg"; fi
    case "$arg" in
        POST|--data) is_post=1 ;;
    esac
    prev="$arg"
done
if [ "$is_post" -eq 1 ]; then
    printf '%s' "$QS_MANIFEST_JSON" > "$out"
    printf '200'
    exit 0
else
    cat "$QS_BUNDLE_FILE" > "$out"
    exit 0
fi
""")

    _write_exec(os.path.join(stub_dir, "id"), "#!/bin/sh\necho 0\n")
    _write_exec(
        os.path.join(stub_dir, "lsb_release"),
        '#!/bin/sh\ncase "$1" in\n  -is) echo Ubuntu ;;\n'
        '  -rs) echo 22.04 ;;\nesac\n',
    )
    _write_exec(
        os.path.join(stub_dir, "uname"),
        '#!/bin/sh\nif [ "$1" = "-m" ]; then echo x86_64; else /bin/uname "$@"; fi\n',
    )
    # df -Pk /: second row, 4th column is available KB (well above the 2GB gate).
    _write_exec(
        os.path.join(stub_dir, "df"),
        "#!/bin/sh\n"
        'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"\n'
        'echo "/dev/root 100000000 1000 99999000 1% /"\n',
    )
    return stub_dir


def _run_bootstrap(served_bundle: bytes, manifest_sha: str, stub_dir: str):
    """Drive the real bootstrap.sh with the given served bytes + manifest
    checksum. Returns (returncode, stdout, stderr, executed?)."""
    work = tempfile.mkdtemp(prefix="qs-checksum-gate-")
    bundle_path = os.path.join(work, "served-bundle.tar.gz")
    sentinel_path = os.path.join(work, "SENTINEL")
    with open(bundle_path, "wb") as handle:
        handle.write(served_bundle)

    manifest = (
        '{"bundle_url": "https://stub.invalid/bundle.tar.gz", '
        f'"bundle_sha256": "{manifest_sha}", '
        '"registration_id": "reg-1", "device_name": "dev-1", '
        '"device_group": "grp-1", "aws_region": "us-east-1", '
        '"quick_setup_url": "https://stub.invalid/quick-setup"}'
    )

    env = dict(os.environ)
    env["PATH"] = stub_dir + os.pathsep + env.get("PATH", "")
    env["QS_MANIFEST_JSON"] = manifest
    env["QS_BUNDLE_FILE"] = bundle_path
    env["SENTINEL_FILE"] = sentinel_path

    proc = subprocess.run(
        ["bash", BOOTSTRAP, "--endpoint", "https://stub.invalid/quick-setup",
         "--token", "dqs1.reg-1.secret"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    executed = os.path.exists(sentinel_path)
    return proc.returncode, proc.stdout, proc.stderr, executed


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

payloads = st.binary(min_size=0, max_size=48)
modes = st.sampled_from(["match", "tamper_bytes", "tamper_manifest"])
suffixes = st.binary(min_size=1, max_size=8)
wrong_shas = st.from_regex(r"[0-9a-f]{64}", fullmatch=True)


@settings(max_examples=100, deadline=None)
@given(payload=payloads, mode=modes, suffix=suffixes, wrong_sha=wrong_shas)
def test_checksum_gate_executes_iff_content_verifies(payload, mode, suffix,
                                                     wrong_sha):
    """**Feature: station-quick-setup, Property 12: The checksum gate executes
    if and only if content verifies**

    The bundle runs iff the served bytes hash to the manifest checksum; any
    tampering (mutated bytes or a mismatched manifest checksum) makes the
    bootstrap execute nothing, print an integrity error, and exit non-zero.

    **Validates: Requirements 4.8, 4.9**
    """
    stub_dir = _make_stub_bin()
    bundle = _build_bundle(payload)
    correct_sha = hashlib.sha256(bundle).hexdigest()

    if mode == "match":
        served, manifest_sha, should_execute = bundle, correct_sha, True
    elif mode == "tamper_bytes":
        # Any byte-level mutation of the served content: append bytes so the
        # served hash provably differs from the (correct) manifest checksum.
        served, manifest_sha, should_execute = bundle + suffix, correct_sha, False
        assume(hashlib.sha256(served).hexdigest() != manifest_sha)
    else:  # tamper_manifest: honest bytes, a manifest checksum that mismatches.
        assume(wrong_sha != correct_sha)
        served, manifest_sha, should_execute = bundle, wrong_sha, False

    rc, out, err, executed = _run_bootstrap(served, manifest_sha, stub_dir)

    if should_execute:
        # Verified content: the gate opens, run.sh executes, exit 0.
        assert executed, (
            "verified bundle should have executed run.sh (no sentinel)\n"
            f"stdout={out!r}\nstderr={err!r}"
        )
        assert rc == 0, f"verified run exited non-zero: rc={rc}\nstderr={err!r}"
        assert "Bundle integrity verified" in out
    else:
        # Tampered content: nothing executes, integrity error, non-zero exit.
        assert not executed, (
            "tampered bundle must NOT execute any part of the bundle\n"
            f"stdout={out!r}\nstderr={err!r}"
        )
        assert rc != 0, "tampered bundle must exit non-zero"
        assert "integrity check FAILED" in err, (
            f"expected an integrity error on stderr, got: {err!r}"
        )


def test_checksum_gate_match_concrete():
    """Concrete verified bundle: gate opens and run.sh executes (Req 4.8)."""
    stub_dir = _make_stub_bin()
    bundle = _build_bundle(b"hello-bundle")
    sha = hashlib.sha256(bundle).hexdigest()
    rc, out, err, executed = _run_bootstrap(bundle, sha, stub_dir)
    assert executed and rc == 0, f"rc={rc}\nstdout={out!r}\nstderr={err!r}"
    assert "Bundle integrity verified" in out


def test_checksum_gate_mismatch_concrete():
    """Concrete tampered bundle: nothing runs, integrity error, exit≠0 (Req 4.9)."""
    stub_dir = _make_stub_bin()
    bundle = _build_bundle(b"hello-bundle")
    sha = hashlib.sha256(bundle).hexdigest()
    rc, out, err, executed = _run_bootstrap(bundle + b"\x00tampered", sha, stub_dir)
    assert not executed, "tampered bundle must not execute"
    assert rc != 0
    assert "integrity check FAILED" in err
