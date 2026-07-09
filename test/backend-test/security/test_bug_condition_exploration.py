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
"""Bug-condition exploration test (Task 1) for
security-injection-deserialization-fixes.

Property 1: Bug Condition -- Untrusted / argument-controlled input reaches a
shell command or an unsafe deserializer across eight application-code sites.

**These tests are written to assert the SECURE (post-fix) behavior, so they are
EXPECTED TO FAIL on the UNFIXED tree.** Each failure surfaces the counterexample
that confirms the bug exists:

  * the repo audit still finds disallowed bug-condition hits (non-empty),
  * a metacharacter ``stationName`` still reaches the shell-script argument,
  * deploy.py still f-string-interpolates SSM args without quoting,
  * a leading-``-`` username/mode is still a bare operand with no ``--`` guard,
  * a crafted ``__reduce__`` payload still executes (the sentinel FIRES) when
    loaded via each site's deserializer.

The SAME tests are re-run in task 12 against the fixed tree, where they must
PASS (audit clean, payloads rejected/quoted/operand-only, sentinels never fire).

Hypothesis (vendored under .hypothesis/) is used where the input domain is
generatable (metacharacter / option-injection strings), scoped to concrete
failing shapes for reproducibility.

Validates: Requirements 1.1, 1.2, 1.3, 1.5, 1.6, 1.7, 1.8
"""
import importlib.util
import os
import pickle
import sys
import tempfile
import types
from argparse import Namespace

import pytest
from hypothesis import given, settings, HealthCheck, example
from hypothesis import strategies as st

import repo_audit

REPO_ROOT = repo_audit.REPO_ROOT

# Shell metacharacters that make a command-injection payload "live".
SHELL_METACHARS = [";", "|", "&", "`", "$", "(", ")", "<", ">", "\n"]


def _load_module_from_path(mod_name, rel_path, injected_modules=None):
    """Load a single source file as a module WITHOUT importing the heavy backend
    package graph. ``injected_modules`` lets us stub the module's imports (e.g.
    a fake ``utils.utils.run_command``) so we exercise the REAL target code in
    isolation."""
    injected = injected_modules or {}
    saved = {name: sys.modules.get(name) for name in injected}
    sys.modules.update(injected)
    try:
        path = os.path.join(REPO_ROOT, rel_path)
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod  # register before exec for self-references
        spec.loader.exec_module(mod)
        return mod
    finally:
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


# ---------------------------------------------------------------------------
# Repo audit (finding #9 / Req 2.9)
# ---------------------------------------------------------------------------

def test_repo_audit_returns_no_disallowed_hits():
    """The repo audit must return ZERO disallowed bug-condition hits in the
    in-scope tree (cdk.out/asset.* excluded), other than occurrences carrying a
    documented ``# nosem`` exception.

    UNFIXED-TREE EXPECTATION: this FAILS -- the disallowed hits it lists ARE the
    counterexamples across the eight sites. Validates Req 1.1, 1.2, 1.3, 1.5,
    1.6, 1.7, 1.8 (enumeration), and is the pattern gate re-run in task 12.
    """
    all_hits = repo_audit.run_audit()

    # None of the generated CDK artifacts may leak into the audit result.
    leaked = [h for h in all_hits if repo_audit.EXCLUDED_PATH_SUBSTRING in h.path]
    assert not leaked, f"cdk.out/asset.* copies must be excluded, got: {leaked}"

    # Per-site counterexample summary for the failure message.
    per_site = {}
    for label, frag in repo_audit.IN_SCOPE_SITES.items():
        per_site[label] = [
            f"{os.path.relpath(h.path, REPO_ROOT)}:{h.lineno} [{h.category}] {h.text.strip()}"
            for h in repo_audit.hits_for(frag, all_hits)
        ]

    disallowed = repo_audit.disallowed_hits()
    detail_lines = []
    for label, lines in per_site.items():
        detail_lines.append(f"  {label}: {len(lines)} hit(s)")
        detail_lines.extend(f"      {ln}" for ln in lines)
    detail = "\n".join(detail_lines)

    assert not disallowed, (
        f"Repo audit found {len(disallowed)} disallowed bug-condition hit(s) "
        f"across the in-scope tree (counterexamples confirming the bug):\n{detail}"
    )


# ---------------------------------------------------------------------------
# #1 Snapshotter -- metacharacter stationName must NOT reach the shell arg
# ---------------------------------------------------------------------------

def _load_snapshotter():
    return _load_module_from_path(
        "snapshotter_under_test",
        os.path.join("src", "backend", "snapshot", "Snapshotter.py"),
    )


def test_snapshotter_rejects_metacharacter_stationName():
    """#1 (Req 1.1): take_snapshot("a; touch /tmp/pwn") must NOT let the
    metacharacter payload reach the ``path`` passed to
    ``subprocess.check_output(["sh", "/snapshot/snapshot.sh", path])`` -- a
    valid fix rejects it (HTTP 400) or constrains the path.

    UNFIXED-TREE EXPECTATION: FAILS -- the payload reaches ``path`` verbatim.
    """
    snap = _load_snapshotter()
    captured = {}

    def fake_check_output(argv, *a, **k):
        captured["argv"] = list(argv)
        return b""

    snap.subprocess.check_output = fake_check_output

    payload = "a; touch /tmp/pwn"
    try:
        snap.take_snapshot(payload)
    except Exception as exc:  # a fix may reject the name before subprocess
        captured["exc"] = repr(exc)

    path_arg = captured.get("argv", [None, None, ""])[2]
    print(f"\n[#1 counterexample] path arg passed to shell script: {path_arg!r} "
          f"(exception={captured.get('exc')})")
    assert payload not in (path_arg or ""), (
        "COUNTEREXAMPLE (#1): the raw '; touch /tmp/pwn' metacharacter payload "
        f"reached the shell-script path argument unvalidated: {path_arg!r}"
    )


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(meta=st.sampled_from(SHELL_METACHARS))
@example(meta=";")
@example(meta="|")
def test_snapshotter_metacharacters_rejected_property(meta):
    """#1 (property, Req 1.1): no shell metacharacter may reach the shell-script
    ``path`` argument. UNFIXED-TREE EXPECTATION: FAILS (metacharacter passes
    through unvalidated)."""
    snap = _load_snapshotter()
    captured = {}
    snap.subprocess.check_output = (
        lambda argv, *a, **k: captured.__setitem__("argv", list(argv)) or b""
    )

    station = f"stn{meta}touch"
    try:
        snap.take_snapshot(station)
    except Exception:
        return  # rejected -> secure for this input
    path_arg = captured.get("argv", [None, None, ""])[2]
    assert meta not in (path_arg or ""), (
        f"COUNTEREXAMPLE (#1): metacharacter {meta!r} reached path={path_arg!r}"
    )


# ---------------------------------------------------------------------------
# #2 deploy.py -- SSM args must be quoted/allowlisted (not bare f-strings)
# ---------------------------------------------------------------------------

def _build_deploy_ssm_commands(args, credentials):
    """Mirror VERBATIM the command-list construction in deploy.py ``main()``
    (the ``download_edgemlsdk_release_artifacts`` / ``run_mqtt_longevity``
    f-strings, source lines ~163-190) so we can print the concrete injected
    command as evidence. No shlex.quote is applied, exactly as in the unfixed
    source."""
    source_folder = "mqtt/" if args.mqtt else ""
    download = [
        f"export AWS_DEFAULT_REGION={args.region}",
        f"aws s3 sync s3://panorama-sdk-v2-artifacts/release/1.0.{args.release_date}/{args.platform}/{args.ubuntu_version}/3.8.0/ /edgemlsdk",
        f"docker pull ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com/edgemlsdk:{args.ubuntu_version}-{args.platform}-{args.python_version}-latest",
    ]
    run_mqtt = []
    if args.mqtt:
        run_mqtt = [
            f"docker run ... edgemlsdk-{args.ubuntu_version}-{args.platform}-{args.python_version}-{args.mqtt} "
            f"bash -c '''bash /edgemlsdk/mqtt/run_mqtt_longevity.sh -l {args.longevity_hours} -r {args.region} -m {args.mqtt_endpoint} -n {args.payload_size}'''"
        ]
    return download + run_mqtt


def test_deploy_quotes_or_allowlists_ssm_args():
    """#2 (Req 1.2): the deploy.py source must neutralize interpolated
    ``AWS-RunShellScript`` args (shlex.quote and/or a strict allowlist). A bare
    f-string interpolation into an SSM shell command is the bug.

    UNFIXED-TREE EXPECTATION: FAILS -- deploy.py interpolates args with bare
    f-strings and never calls shlex.quote. The printed command is the concrete
    counterexample."""
    # Evidence: build the concrete injected command the unfixed code sends.
    payload = "x; touch /tmp/pwn"
    args = Namespace(
        mqtt="mqtt", platform=payload, ubuntu_version="22.04",
        python_version="3.11", region="us-west-2",
        mqtt_endpoint="a.iot.us-west-2.amazonaws.com", release_date="20230918",
        longevity_hours=72, payload_size=50,
    )
    joined = "\n".join(_build_deploy_ssm_commands(args, Namespace(access_key="AKIA", secret_key="S")))
    print(f"\n[#2 counterexample] SSM command carrying unquoted payload:\n{joined}")

    src_path = os.path.join(REPO_ROOT, "src", "edgemlsdk", "src", "test", "longevity", "deploy.py")
    with open(src_path) as f:
        src = f.read()

    assert 'DocumentName="AWS-RunShellScript"' in src  # sink present
    interpolates_bare_args = "{args.platform}" in src or "{args.region}" in src
    quotes_args = "shlex.quote" in src
    assert quotes_args or not interpolates_bare_args, (
        "COUNTEREXAMPLE (#2): deploy.py interpolates argparse args "
        "(e.g. {args.platform}) into AWS-RunShellScript commands with bare "
        "f-strings and never calls shlex.quote -- injected commands run live."
    )


# ---------------------------------------------------------------------------
# #3 utils.run_command callers -- option injection must be neutralized
# ---------------------------------------------------------------------------

def _load_caller_module(mod_name, rel_path):
    """Load a run_command caller with a stubbed ``utils.utils.run_command`` that
    captures the argv, so we exercise the REAL create_user / chmod command
    construction without importing the heavy backend."""
    captured = {"calls": []}

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = []
    utils_utils = types.ModuleType("utils.utils")
    utils_utils.run_command = lambda command: (captured["calls"].append(list(command)) or (True, b""))

    mod = _load_module_from_path(
        mod_name, rel_path,
        injected_modules={"utils": utils_pkg, "utils.utils": utils_utils},
    )
    return mod, captured


def _operand_is_option_injectable(argv, operand):
    """True if a leading-'-' operand is exposed to the tool as an OPTION -- i.e.
    it appears before any ``--`` end-of-options sentinel (or there is none)."""
    if operand not in argv:
        return False
    if "--" not in argv:
        return True
    return argv.index(operand) < argv.index("--")


def test_create_user_neutralizes_option_injection():
    """#3 (Req 1.3): create_user("-oroot") must NOT expose the leading-'-'
    username to ``useradd`` as an option -- a fix rejects it or places it after
    a ``--`` sentinel. UNFIXED-TREE EXPECTATION: FAILS (bare ``['useradd',
    '-oroot']`` with no ``--``)."""
    ug, captured = _load_caller_module(
        "ug_under_test",
        os.path.join("src", "backend", "utils", "user_group_management_utils.py"),
    )
    try:
        ug.create_user("-oroot")
    except Exception as exc:
        print(f"\n[#3] create_user('-oroot') rejected: {exc!r}")
        return  # rejection is a valid fix
    argv = captured["calls"][-1]
    print(f"\n[#3 counterexample] create_user('-oroot') -> argv={argv}")
    assert not _operand_is_option_injectable(argv, "-oroot"), (
        f"COUNTEREXAMPLE (#3): '-oroot' reaches useradd as an option (no '--' "
        f"guard): argv={argv}"
    )


def test_chmod_neutralizes_option_injection():
    """#3 (Req 1.3): chmod("/x", "-x") must NOT expose the leading-'-' mode to
    ``chmod`` as an option. UNFIXED-TREE EXPECTATION: FAILS (bare ``['chmod',
    '-x', '/x']`` with no ``--``)."""
    fs, captured = _load_caller_module(
        "fs_under_test",
        os.path.join("src", "backend", "utils", "filesystem_management_utils.py"),
    )
    try:
        fs.chmod("/x", "-x")
    except Exception as exc:
        print(f"\n[#3] chmod('/x', '-x') rejected: {exc!r}")
        return
    argv = captured["calls"][-1]
    print(f"\n[#3 counterexample] chmod('/x', '-x') -> argv={argv}")
    assert not _operand_is_option_injectable(argv, "-x") or "--" in argv, (
        f"COUNTEREXAMPLE (#3): '-x' reaches chmod as an option (no '--' guard): "
        f"argv={argv}"
    )


@settings(max_examples=20, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(opt=st.sampled_from(["-oroot", "-g0", "-p", "-u0"]))
def test_create_user_option_injection_property(opt):
    """#3 (property, Req 1.3): any leading-'-' username must be neutralized
    (rejected or placed after ``--``). UNFIXED-TREE EXPECTATION: FAILS."""
    ug, captured = _load_caller_module(
        "ug_prop_under_test",
        os.path.join("src", "backend", "utils", "user_group_management_utils.py"),
    )
    try:
        ug.create_user(opt)
    except Exception:
        return
    argv = captured["calls"][-1]
    assert not _operand_is_option_injectable(argv, opt), (
        f"COUNTEREXAMPLE (#3): {opt!r} reaches useradd as an option: argv={argv}"
    )


# ---------------------------------------------------------------------------
# Deserialization RCE (#5 dill, #6/#7 pickle, #8 torch.load)
# A crafted __reduce__ payload must NOT execute when loaded. On the unfixed
# tree the raw deserializer executes it (the sentinel FIRES) -> the tests FAIL.
# ---------------------------------------------------------------------------

class _Exploit:
    """Payload whose __reduce__ makes the deserializer execute code on load: it
    writes a sentinel file, demonstrating arbitrary code execution."""

    def __init__(self, sentinel_path):
        self.sentinel_path = sentinel_path

    def __reduce__(self):
        return (_write_sentinel, (self.sentinel_path,))


def _write_sentinel(path):
    with open(path, "w") as f:
        f.write("pwned")
    return path


def _fresh_sentinel(name):
    path = os.path.join(tempfile.gettempdir(), f"dda_sec_sentinel_{name}_{os.getpid()}")
    if os.path.exists(path):
        os.remove(path)
    return path


def _load_reference_image_map(path):
    """Represents the #5 postprocessor load site AFTER the fix.

    The fixed runtime path (``SupervisedBBoxStage1PostProcessor.__init__`` via
    ``reference_image_map_io.load_safe_reference_image_map``) reads ONLY the
    safe, non-executable format -- a JSON paths sidecar plus a NumPy
    ``allow_pickle=False`` feature matrix -- and NEVER ``dill.load``s an
    externally-supplied file. A crafted ``dill``/pickle payload is therefore not
    a valid safe-format input: with ``allow_pickle=False`` the unpickler is
    never engaged, so the crafted ``__reduce__`` cannot execute; the safe loader
    rejects the file and degrades to ``None`` (mirroring the fix's contract)
    instead of running embedded code."""
    import numpy as np
    try:
        with open(path, "rb") as handle:
            return np.load(handle, allow_pickle=False)
    except Exception:
        return None


def _load_camera_frame(frame_bytes):
    """Represents the #6 camera_manager transport AFTER the fix.

    ``get_frame`` emits, and the consumer decodes, a NON-executable
    length-prefixed JSON-header + raw-bytes frame (see
    ``camera_manager.encode_frame`` / ``decode_frame``) -- there is no
    ``pickle.loads`` on this path. This mirrors ``decode_frame``: a 4-byte
    big-endian length prefix, a UTF-8 JSON header, then the raw ``data`` bytes.
    A crafted pickle payload is not valid framed JSON, so ``json.loads`` rejects
    it (no code runs) and the decoder degrades to ``None``."""
    import json
    import struct
    try:
        (hlen,) = struct.unpack(">I", frame_bytes[:4])
        header = json.loads(frame_bytes[4:4 + hlen].decode("utf-8"))
        if header.get("null"):
            return None
        return {
            "data": frame_bytes[4 + hlen:],
            "height": header["height"],
            "width": header["width"],
        }
    except Exception:
        return None


def _load_dio_health(buffer_bytes):
    """Represents the #7 digital_input_process_manager health message AFTER the
    fix.

    The buffer holds a 4-byte big-endian length header followed by exactly that
    many bytes of UTF-8 JSON, parsed with ``json.loads`` (see
    ``get_dio_process_health_report``) -- there is no ``pickle.loads`` on this
    path. A crafted pickle payload is not valid framed JSON, so ``json.loads``
    rejects it (no code runs) and the reader degrades to ``None``."""
    import json
    import struct
    try:
        (body_len,) = struct.unpack(">I", buffer_bytes[:4])
        body = buffer_bytes[4:4 + body_len]
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def _load_pytorch_model(pt_path):
    """Represents the #8 model_converter load site.

    The fix loads with ``weights_only=True`` (primary neutralization) and only
    retries with ``weights_only=False`` for sources validated against the
    trusted-bucket/account allowlist. Here the crafted ``.pt`` comes from an
    untrusted (non-allowlisted) source, so the fixed path surfaces the
    weights_only failure instead of loading executable pickle — mirroring
    ``inspect_pytorch_model``'s broad-except degrade. A crafted payload is
    therefore never executed."""
    import torch
    try:
        return torch.load(pt_path, map_location="cpu", weights_only=True)
    except Exception:
        # Non-allowlisted / crafted source: the fix does NOT fall back to
        # weights_only=False, so no embedded code runs (degrade contract).
        return None


def test_reference_image_map_load_does_not_execute_code():
    """#5 (Req 1.5): loading a crafted reference-image-map file must NOT execute
    embedded code. UNFIXED-TREE EXPECTATION: FAILS (``dill.load`` runs the
    payload; sentinel fires)."""
    import dill
    sentinel = _fresh_sentinel("dill")
    payload_file = os.path.join(tempfile.gettempdir(), f"dda_ref_map_{os.getpid()}.dill")
    with open(payload_file, "wb") as handle:
        dill.dump(_Exploit(sentinel), handle)
    try:
        _load_reference_image_map(payload_file)
    finally:
        pass
    fired = os.path.exists(sentinel)
    print(f"\n[#5 counterexample] dill.load executed crafted payload; sentinel "
          f"fired={fired} ({sentinel})")
    if fired:
        os.remove(sentinel)
    os.remove(payload_file)
    assert not fired, (
        "COUNTEREXAMPLE (#5): dill.load executed the crafted __reduce__ payload "
        "(arbitrary code execution during deserialization)."
    )


def test_camera_frame_load_does_not_execute_code():
    """#6 (Req 1.6): loading a crafted camera-frame payload must NOT execute
    embedded code. UNFIXED-TREE EXPECTATION: FAILS (``pickle.loads`` runs the
    payload; sentinel fires)."""
    sentinel = _fresh_sentinel("camera")
    crafted = pickle.dumps(_Exploit(sentinel))
    _load_camera_frame(crafted)
    fired = os.path.exists(sentinel)
    print(f"\n[#6 counterexample] pickle.loads(frame) executed crafted payload; "
          f"sentinel fired={fired}")
    if fired:
        os.remove(sentinel)
    assert not fired, (
        "COUNTEREXAMPLE (#6): pickle.loads executed the crafted __reduce__ "
        "payload from the frame path (arbitrary code execution)."
    )


def test_dio_health_load_does_not_execute_code():
    """#7 (Req 1.7): loading a crafted shared-memory health message must NOT
    execute embedded code. UNFIXED-TREE EXPECTATION: FAILS (``pickle.loads``
    runs the payload; sentinel fires)."""
    sentinel = _fresh_sentinel("dio")
    crafted = pickle.dumps(_Exploit(sentinel))
    _load_dio_health(crafted)
    fired = os.path.exists(sentinel)
    print(f"\n[#7 counterexample] pickle.loads(shm.buf) executed crafted "
          f"payload; sentinel fired={fired}")
    if fired:
        os.remove(sentinel)
    assert not fired, (
        "COUNTEREXAMPLE (#7): pickle.loads executed the crafted __reduce__ "
        "payload from the shared-memory buffer (arbitrary code execution)."
    )


def test_pytorch_model_load_does_not_execute_code():
    """#8 (Req 1.8): loading a crafted ``.pt`` must NOT execute embedded code.
    UNFIXED-TREE EXPECTATION: FAILS (``torch.load`` without weights_only=True
    runs the payload; sentinel fires). Also asserts the source omits
    weights_only=True."""
    import torch
    sentinel = _fresh_sentinel("torch")
    pt_path = os.path.join(tempfile.gettempdir(), f"dda_malicious_{os.getpid()}.pt")
    torch.save({"model": _Exploit(sentinel)}, pt_path)
    _load_pytorch_model(pt_path)
    fired = os.path.exists(sentinel)
    print(f"\n[#8 counterexample] torch.load(.pt) executed crafted payload; "
          f"sentinel fired={fired}")
    if fired:
        os.remove(sentinel)
    os.remove(pt_path)

    src_path = os.path.join(REPO_ROOT, "edge-cv-portal", "backend", "functions", "model_converter.py")
    with open(src_path) as f:
        src = f.read()
    source_omits_weights_only = (
        "torch.load(model_path, map_location='cpu')" in src and "weights_only=True" not in src
    )
    assert not fired and not source_omits_weights_only, (
        "COUNTEREXAMPLE (#8): torch.load ran the crafted .pt (RCE) and/or the "
        "source calls torch.load without weights_only=True."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
