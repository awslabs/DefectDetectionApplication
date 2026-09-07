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
PRESERVATION properties for build-server-bootstrap-zip (task 2).

**Property 2: Preservation** - Bootstrap text unchanged outside the apt
package list

**Validates: Requirements 3.1, 3.2, 3.3, 3.5**

Observation-first (recorded on UNFIXED code, 2026-09-07): both bootstrap
generators were rendered for representative inputs and their exact output
transcribed into the FROZEN normalized oracles below
(``frozen_fleet_bootstrap_normalized`` /
``frozen_runner_bootstrap_normalized``). ``normalize_bootstrap`` maps the
ROOT apt-get install line — the ONE line the zip fix is allowed to touch —
to the canonical token ``@@ROOT_APT_INSTALL@@`` and leaves every other
byte alone, so these tests PASS on unfixed code (task 2) and MUST STILL
PASS, unchanged, after the fix (task 3.3). This is the bugfix.md
Preservation Goal: ``normalize(F(inputs)) = normalize(F'(inputs))``.

Pinned for the dedicated fleet bootstrap
(``build_fleet.render_user_data`` / ``USER_DATA_TEMPLATE``), for
hypothesis-generated (repo_url, repo_dir, source_ref):

  * shebang + ``set -x``;
  * the non-fatal log redirect (``BOOTSTRAP_LOG=...`` guarded ``exec``);
  * the ``sudo -u ubuntu -H git clone <repo_url> <repo_dir>`` line;
  * the Source_Sync here-doc, byte-equal to
    ``build_source.source_sync_commands`` output (its single origin);
  * the ``setup-build-server.sh`` invocation;
  * the tolerated ``touch <marker> || true`` as the LAST statement;
  * ``{repo_url}`` as USER_DATA_TEMPLATE's ONLY unbound placeholder;
  * ``bash -n`` cleanliness (the test_bootstrap_gate_property.py
    syntax-check approach — parse only, nothing executed).

Pinned for the ephemeral runner bootstrap
(``build_dispatcher.runner_bootstrap_user_data``): the same normalized
byte-equality, with the root prologue in its recorded order — log
redirect -> HOME export -> apt line -> parent-prepare/ownership-heal ->
sudo (build-user) body -> classified sync exits -> marker LAST — plus the
''-BUILD_REPO_URL case still rendering nothing at all.

Safety: pure text generation plus local ``bash -n`` parses. No EC2, no
SSM, no real AWS call, no build. The repository URLs are non-resolvable
``example.invalid`` addresses (or the public repo URL used purely as
text), only ever interpolated into generated strings.

Run from the repository root::

    python3 -m pytest \\
        test/backend-test/portal_builds/test_bootstrap_zip_preservation.py \\
        --noconftest -q -p no:cacheprovider

(This run contains property-based tests.)
"""
import json
import os
import re
import shlex
import string
import subprocess
import sys
import tempfile
import types
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handlers bind boto3 clients and
# env-derived settings at import time (sibling-file discipline — see
# test_bootstrap_zip_exploration.py / test_source_selection_preservation.py).
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "bootstrap-zip-preserve"
os.environ["BUILD_JOBS_TABLE"] = f"dda-portal-build-jobs-{_SUFFIX}"
os.environ["BUILD_SERVERS_TABLE"] = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["SETTINGS_TABLE"] = f"dda-portal-settings-{_SUFFIX}"
os.environ["AUDIT_LOG_TABLE"] = f"dda-portal-audit-log-{_SUFFIX}"

# Nothing may be dispatched from any code path exercised here.
os.environ.pop("BUILD_DISPATCHER_FUNCTION_NAME", None)
# BUILD_REPO_URL popped before import: the dispatcher's module global
# starts '' and the URL-bearing cases patch the module attribute, exactly
# as test_run_as_ubuntu_unit.py / the exploration suite do.
os.environ.pop("BUILD_REPO_URL", None)
# Deliberately NOT set, so BUILD_REPO_DIR keeps its module default (the
# deployed Lambda sets no override either — design A1).
os.environ.pop("BUILD_REPO_DIR", None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")
    module.log_audit_event = lambda **kwargs: None
    module.create_response = lambda status_code, body: {
        "statusCode": status_code, "body": body}
    module.get_user_from_event = lambda event: {
        "user_id": "bootstrap-zip-preserve", "role": "PortalAdmin"}
    return module


def _fake_rbac_middleware():
    module = types.ModuleType("rbac_middleware")

    def _identity_decorator_factory(*d_args, **d_kwargs):
        def decorator(func):
            return func
        return decorator

    module.require_builds_read = _identity_decorator_factory
    module.require_builds_submit = _identity_decorator_factory
    module.require_builds_cancel = _identity_decorator_factory
    module.super_user_only = lambda func: func
    return module


# The exploration suite's pure text helpers are reused for the
# normalization (imported BEFORE the module pops below, so this module's
# own build_* imports are the authoritative fresh ones).
from test_bootstrap_zip_exploration import (  # noqa: E402
    _HEREDOC_OPEN, _apt_install_packages, root_apt_install_lines)

for _module in ("build_domain", "build_planner", "build_dispatcher",
                "build_fleet", "build_source", "shared_utils",
                "rbac_middleware"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()
sys.modules["rbac_middleware"] = _fake_rbac_middleware()

import build_domain  # noqa: E402
import build_dispatcher  # noqa: E402
import build_fleet  # noqa: E402
import build_planner  # noqa: E402
import build_source  # noqa: E402

#: Non-resolvable default URL, only ever interpolated into text.
REPO_URL = "https://example.invalid/dda.git"

#: The region this module pinned into the environment above;
#: build_dispatcher.dispatch_region reads os.environ['AWS_REGION'] at
#: call time, so the runner oracle's export lines use exactly this value.
_REGION = os.environ["AWS_REGION"]

#: FROZEN copies of values the oracles must not re-derive from the code
#: under test (the jp7 integration suite's convention).
FROZEN_DEFAULT_REPO_DIR = "/home/ubuntu/DefectDetectionApplication"
FROZEN_LOG_LINE = "BOOTSTRAP_LOG=/var/log/dda-build-server-bootstrap.log"
FROZEN_MARKER_STATEMENT = \
    "touch /var/log/dda-build-server-bootstrap.done || true"

#: The canonical token the ONE fix-touchable line is normalized to.
APT_TOKEN = "@@ROOT_APT_INSTALL@@"


# ---------------------------------------------------------------------------
# Normalization: the root apt-get install line -> canonical token,
# every other byte untouched (bugfix.md Preservation Goal's normalize()).
# ---------------------------------------------------------------------------

def normalize_bootstrap(text):
    """``text`` with each ROOT apt-get install line (outside every
    here-doc body, not under ``sudo``, comments excluded) replaced by
    APT_TOKEN. Everything else — including here-doc bodies and the
    trailing newline — is preserved byte-for-byte."""
    out = []
    pending_delimiter = None
    for line in text.splitlines():
        if pending_delimiter is not None:
            out.append(line)
            if line.strip() == pending_delimiter:
                pending_delimiter = None
            continue
        match = _HEREDOC_OPEN.search(line)
        stripped = line.strip()
        is_root_apt = False
        if (not match and stripped and not stripped.startswith("#")
                and not stripped.startswith("sudo ")):
            for segment in re.split(r"&&|\|\||;", stripped):
                if _apt_install_packages(segment) is not None:
                    is_root_apt = True
                    break
        out.append(APT_TOKEN if is_root_apt else line)
        if match:
            pending_delimiter = (match.group(1) or match.group(2)
                                 or match.group(3))
    normalized = "\n".join(out)
    if text.endswith("\n") and not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def _observed(**fields):
    return json.dumps(fields, indent=2, default=str)


def _assert_normalized_equal(actual_text, expected_normalized, **context):
    actual_normalized = normalize_bootstrap(actual_text)
    assert actual_normalized == expected_normalized, _observed(
        context=context,
        first_diff_line=next(
            (index for index, (a, b) in enumerate(zip(
                actual_normalized.splitlines(),
                expected_normalized.splitlines())) if a != b),
            None),
        actual=actual_normalized, expected=expected_normalized)
    # Exactly one root apt line existed before normalization: the token
    # replaced something real, and only one something.
    assert actual_normalized.count(APT_TOKEN) == 1, _observed(
        context=context, actual=actual_normalized)
    assert len(root_apt_install_lines(actual_text)) == 1, _observed(
        context=context, actual=actual_text)


# ---------------------------------------------------------------------------
# FROZEN normalized oracles — re-spelled from the output recorded on
# UNFIXED code (2026-09-07 observation run). The Source_Sync body is the
# byte output of build_source.source_sync_commands, its single origin
# (Req 3.2); everything else is literal.
# ---------------------------------------------------------------------------

def frozen_fleet_bootstrap_normalized(repo_url, repo_dir, source_ref):
    """The dedicated fleet bootstrap, normalized: every line
    ``build_fleet.render_user_data(repo_url, repo_dir, source_ref)``
    emits, with the root apt install line as APT_TOKEN."""
    sync = "\n".join(build_source.source_sync_commands(
        repo_url, repo_dir, source_ref))
    return "\n".join([
        "#!/bin/bash",
        "set -x",
        FROZEN_LOG_LINE,
        'if : > "$BOOTSTRAP_LOG" 2>/dev/null; then',
        '  exec >> "$BOOTSTRAP_LOG" 2>&1',
        "fi",
        "",
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update",
        APT_TOKEN,
        "",
        "# Clone the source repository for the build agent (design §2/§5).",
        f"sudo -u ubuntu -H git clone {repo_url} {repo_dir}",
        "",
        "# Put that tree on the selected (repository, ref) through the shared",
        "# Sync_Generator (Req 4.1, 4.3): clone-if-absent, fetch, checkout. "
        "Run as",
        "# the build user, both because the clone above is owned by it and "
        "because",
        "# the build itself runs as that user. A sync failure echoes",
        "# PORTAL_SOURCE_SYNC_FAILED and exits 65/66 inside this block; the",
        "# bootstrap continues so the marker below is still written and the",
        "# dispatcher's readiness gate is not left hanging.",
        "sudo -u ubuntu -H bash -s <<'DDA_SOURCE_SYNC'",
        sync,
        "DDA_SOURCE_SYNC",
        "",
        "# Run the repository's build-environment bootstrap as the build user",
        "# (setup-build-server.sh equivalent: docker via snap, "
        "docker-compose,",
        "# Python 3.11, AWS CLI, botocore[crt], GDK CLI).",
        f"cd {repo_dir}",
        f"sudo -u ubuntu -H bash -c 'cd {repo_dir} && "
        "./setup-build-server.sh' || true",
        "",
        FROZEN_MARKER_STATEMENT,
        "",
    ])


def frozen_runner_bootstrap_normalized(repo_url, repo_dir, source_ref,
                                       region):
    """The ephemeral runner bootstrap, normalized: every line
    ``build_dispatcher.runner_bootstrap_user_data(job, repo_dir)`` emits,
    with the root apt install line as APT_TOKEN. Root prologue order as
    recorded: log redirect -> HOME export -> apt -> parent prepare /
    ownership heal -> build-user body -> classified sync exits ->
    marker LAST."""
    qdir = shlex.quote(repo_dir)
    body = ['export HOME="${HOME:-/home/ubuntu}"']
    if region:
        qregion = shlex.quote(region)
        body += [f"export AWS_DEFAULT_REGION={qregion}",
                 f"export AWS_REGION={qregion}"]
    body += ['export PATH="${HOME:-/home/ubuntu}/.local/bin:$PATH"']
    body += list(build_source.source_sync_commands(
        repo_url, repo_dir, source_ref))
    body += ["bash ./setup-build-server.sh"]
    return "\n".join([
        "#!/bin/bash",
        "set -uo pipefail",
        FROZEN_LOG_LINE,
        'if : > "$BOOTSTRAP_LOG" 2>/dev/null; then',
        '  exec >> "$BOOTSTRAP_LOG" 2>&1',
        "fi",
        'export HOME="${HOME:-/root}"',
        "export DEBIAN_FRONTEND=noninteractive",
        APT_TOKEN,
        'mkdir -p "$(dirname %s)"' % qdir,
        'chown ubuntu:ubuntu "$(dirname %s)" 2>/dev/null || true' % qdir,
        "if [ -d %s ]; then chown -R ubuntu:ubuntu %s 2>/dev/null || true; fi"
        % (qdir, qdir),
        'PORTAL_RUN_SCRIPT="$(mktemp /tmp/portal-build-run.XXXXXX)" '
        "|| exit 1",
        "cat > \"$PORTAL_RUN_SCRIPT\" <<'PORTAL_RUN_EOF'",
        "\n".join(body),
        "PORTAL_RUN_EOF",
        'chmod 644 "$PORTAL_RUN_SCRIPT"',
        'if [ "$(id -u)" = "0" ] && id ubuntu >/dev/null 2>&1; then',
        '  sudo -H -u ubuntu bash "$PORTAL_RUN_SCRIPT"',
        "else",
        '  bash "$PORTAL_RUN_SCRIPT"',
        "fi",
        'PORTAL_RUN_STATUS="$?"',
        'rm -f "$PORTAL_RUN_SCRIPT"',
        'case "$PORTAL_RUN_STATUS" in',
        '  65|66) exit "$PORTAL_RUN_STATUS";;',
        "esac",
        FROZEN_MARKER_STATEMENT,
        "",
    ])


# ---------------------------------------------------------------------------
# bash -n (parse only — nothing is executed): the
# test_bootstrap_gate_property.py approach.
# ---------------------------------------------------------------------------

_BASH = "/usr/bin/bash" if os.path.exists("/usr/bin/bash") else "/bin/bash"


def assert_bash_parses(text, **context):
    with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False) as handle:
        handle.write(text)
        path = handle.name
    try:
        result = subprocess.run([_BASH, "-n", path],
                                capture_output=True, text=True)
        assert result.returncode == 0, _observed(
            context=context, stderr=result.stderr, text=text)
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Generators — shell-safe values, the exploration/alignment suites' domains
# ---------------------------------------------------------------------------

#: Realistic shell-safe repository URLs (shlex.quote-invariant), only ever
#: interpolated into generated text.
repo_urls = st.sampled_from([
    "https://example.invalid/dda.git",
    "https://github.com/awslabs/DefectDetectionApplication",
    "https://git.example.invalid/team/DefectDetectionApplication.git",
])

_directories = st.sampled_from([
    "/home/ubuntu/DefectDetectionApplication",
    "/opt/dda/DefectDetectionApplication",
    "/srv/dda-build/tree",
    "/mnt/build/dda",
])
repo_dirs = st.one_of(st.none(), st.just(""), _directories)

source_refs = st.one_of(
    st.none(),
    st.just(""),
    st.sampled_from(["main", "v1.2.3", "a" * 40,
                     "feature/portal-build-fleet-and-workflow-gates"]),
)


def _job(source_ref=None):
    """A minimal ephemeral Build_Job (test_run_as_ubuntu_unit.py::_job)."""
    return {
        "build_job_id": "0a1b2c3d",
        "build_target": build_domain.TARGET_JP6,
        "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
        "config_snapshot": {"source_ref": source_ref,
                            "max_runtime_hours": 4},
    }


# ===========================================================================
# Property 2 — Preservation: the dedicated fleet bootstrap (Req 3.1-3.3)
# ===========================================================================

class TestFleetBootstrapPreservation:

    def test_incident_shaped_default_render(self):
        """The POST /build-servers default render, normalized, is exactly
        the recorded unfixed text with the apt line canonicalized."""
        text = build_fleet.render_user_data(REPO_URL)
        _assert_normalized_equal(
            text,
            frozen_fleet_bootstrap_normalized(
                REPO_URL, FROZEN_DEFAULT_REPO_DIR, None),
            render_path="build_fleet.render_user_data", repo_url=REPO_URL)

    @given(repo_url=repo_urls, repo_dir=repo_dirs, source_ref=source_refs)
    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_every_fleet_render_is_pinned_outside_the_apt_line(
            self, repo_url, repo_dir, source_ref):
        """For EVERY (repo_url, repo_dir, source_ref): shebang/set -x, the
        non-fatal log redirect, the clone line, the Source_Sync here-doc
        (byte-equal to build_source.source_sync_commands output), the
        setup-build-server.sh invocation and the tolerated marker write —
        all byte-identical to the recorded baseline, marker LAST."""
        resolved_dir = repo_dir or FROZEN_DEFAULT_REPO_DIR
        text = build_fleet.render_user_data(repo_url, repo_dir, source_ref)
        _assert_normalized_equal(
            text,
            frozen_fleet_bootstrap_normalized(
                repo_url, resolved_dir, source_ref),
            render_path="build_fleet.render_user_data", repo_url=repo_url,
            repo_dir=repo_dir, source_ref=source_ref)
        assert text.rstrip("\n").splitlines()[-1] == \
            FROZEN_MARKER_STATEMENT, _observed(text=text)

    def test_user_data_template_repo_url_is_the_only_placeholder(self):
        """{repo_url} stays USER_DATA_TEMPLATE's ONLY unbound field:
        .format(repo_url=...) succeeds, equals the render_user_data
        output, and no other replacement field remains (Req 3.2)."""
        fields = {field for _, field, _, _
                  in string.Formatter().parse(build_fleet.USER_DATA_TEMPLATE)
                  if field is not None}
        assert fields == {"repo_url"}, _observed(fields=sorted(fields))
        rendered = build_fleet.USER_DATA_TEMPLATE.format(repo_url=REPO_URL)
        assert rendered == build_fleet.render_user_data(REPO_URL), _observed(
            template_render=rendered,
            function_render=build_fleet.render_user_data(REPO_URL))

    @given(repo_url=repo_urls, repo_dir=repo_dirs, source_ref=source_refs)
    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_fleet_render_is_valid_bash(self, repo_url, repo_dir,
                                        source_ref):
        """bash -n clean for every render (Req 2.4/3.3 discipline)."""
        assert_bash_parses(
            build_fleet.render_user_data(repo_url, repo_dir, source_ref),
            render_path="build_fleet.render_user_data", repo_url=repo_url,
            repo_dir=repo_dir, source_ref=source_ref)


# ===========================================================================
# Property 2 — Preservation: the ephemeral runner bootstrap (Req 3.1-3.3,
# 3.5)
# ===========================================================================

class TestRunnerBootstrapPreservation:

    def test_job_without_source_ref(self):
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            text = build_dispatcher.runner_bootstrap_user_data(_job(None))
        _assert_normalized_equal(
            text,
            frozen_runner_bootstrap_normalized(
                REPO_URL, FROZEN_DEFAULT_REPO_DIR, None, _REGION),
            render_path="build_dispatcher.runner_bootstrap_user_data",
            source_ref=None)

    @given(repo_url=repo_urls, repo_dir=repo_dirs, source_ref=source_refs)
    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_every_runner_render_is_pinned_outside_the_apt_line(
            self, repo_url, repo_dir, source_ref):
        """For EVERY (repo_url, repo_dir, source_ref): the recorded root
        prologue order — log redirect -> HOME export -> apt ->
        parent-prepare/ownership-heal -> build-user body (here-doc
        transport) -> classified sync exits (65/66) -> marker LAST — all
        byte-identical to the recorded baseline outside the apt line."""
        resolved_dir = repo_dir or FROZEN_DEFAULT_REPO_DIR
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               repo_url):
            text = build_dispatcher.runner_bootstrap_user_data(
                _job(source_ref), repo_dir or None)
        _assert_normalized_equal(
            text,
            frozen_runner_bootstrap_normalized(
                repo_url, resolved_dir, source_ref, _REGION),
            render_path="build_dispatcher.runner_bootstrap_user_data",
            repo_url=repo_url, repo_dir=repo_dir, source_ref=source_ref)
        assert text.rstrip("\n").splitlines()[-1] == \
            FROZEN_MARKER_STATEMENT, _observed(text=text)

    def test_root_prologue_order(self):
        """The recorded prologue order asserted explicitly (clearer
        failure than the byte-oracle when only ordering drifts)."""
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            text = build_dispatcher.runner_bootstrap_user_data(_job("main"))
        normalized = normalize_bootstrap(text)
        lines = normalized.splitlines()
        order = [
            lines.index(FROZEN_LOG_LINE),
            lines.index('export HOME="${HOME:-/root}"'),
            lines.index(APT_TOKEN),
            lines.index('mkdir -p "$(dirname %s)"'
                        % shlex.quote(FROZEN_DEFAULT_REPO_DIR)),
            lines.index("cat > \"$PORTAL_RUN_SCRIPT\" <<'PORTAL_RUN_EOF'"),
            lines.index('  65|66) exit "$PORTAL_RUN_STATUS";;'),
            lines.index(FROZEN_MARKER_STATEMENT),
        ]
        assert order == sorted(order), _observed(order=order,
                                                 text=normalized)
        assert lines[-1] == FROZEN_MARKER_STATEMENT or (
            lines[-1] == "" and lines[-2] == FROZEN_MARKER_STATEMENT), \
            _observed(text=normalized)

    @given(repo_url=repo_urls, repo_dir=repo_dirs, source_ref=source_refs)
    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_runner_render_is_valid_bash(self, repo_url, repo_dir,
                                         source_ref):
        """bash -n clean for every render (Req 2.4/3.3 discipline)."""
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               repo_url):
            text = build_dispatcher.runner_bootstrap_user_data(
                _job(source_ref), repo_dir or None)
        assert_bash_parses(
            text,
            render_path="build_dispatcher.runner_bootstrap_user_data",
            repo_url=repo_url, repo_dir=repo_dir, source_ref=source_ref)

    def test_no_repo_url_still_renders_nothing(self):
        """The ''-BUILD_REPO_URL case CONTINUES to render no bootstrap at
        all (a pre-provisioned AMI is assumed) — Req 3.5 flavor of the
        readiness-gate semantics staying put."""
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", ""):
            assert build_dispatcher.runner_bootstrap_user_data(
                _job(None)) == ""
