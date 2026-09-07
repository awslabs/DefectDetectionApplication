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
BUG CONDITION EXPLORATION for build-server-bootstrap-zip (task 1).

**Property 1: Bug Condition** - Rendered bootstrap never installs zip

**Validates: Requirements 1.1, 1.3, 2.1, 2.3**

**STATUS (task 3.2, 2026-09-07)**: the fix landed — both generators' root
apt lines now install ``git zip unzip`` — and this file, re-run UNCHANGED,
PASSES (6 passed, 1 skipped), confirming the expected behavior. The
framing below records the task 1 exploration run against the UNFIXED
code; the counterexamples section is that run's historical record.

**CRITICAL** (task 1 framing): every test in this file MUST FAIL on the
unfixed code. The failures ARE the result: they reproduce ``isBugCondition(X)``
from the bugfix design — the ROOT apt-get install line of every rendered
bootstrap (dedicated fleet AND ephemeral runner) does not install ``zip``
or ``unzip``, so ``build-custom.sh``'s packaging step dies with
``zip: command not found`` / exit 127 after ~1.5 hours of successful work
(Build_Job 53312133-1ce1-4c09-a1c8-3fa01d0e9d1a on
srv-aac90870-033e-4e9c-9994-29ee895da421 / i-07c2ca92f3a526c93,
2026-09-07).

The assertions encode the POST-FIX expected behavior (every render path's
root apt line installs zip AND unzip), so this exact file is re-run
unchanged in task 3.2 where it must PASS. Do NOT weaken these assertions
and do NOT change production code to make them pass now.

What counts as the ROOT apt line: the prologue statements cloud-init runs
as root, BEFORE any ``sudo -u ubuntu`` statement and OUTSIDE any here-doc
body. ``setup-build-server.sh``'s own apt line is deliberately NOT
counted: it is ref-dependent (a server bootstrapped onto an older ref
runs an older script without zip) and failure-tolerant
(``run_cmd ... || add_warning``), and it demonstrably did not protect the
live server — bugfix.md, "Why the setup script does not save us".

Render paths covered (bugfix.md single-source-of-truth verification):
  * ``build_fleet.render_user_data(repo_url, repo_dir, source_ref)`` —
    the POST /build-servers launch path (``launch_build_server`` →
    ``run_fleet_instance``), for BOTH ubuntu_flavor values (flavor only
    selects the AMI; both share this render path);
  * the module-level ``build_fleet.USER_DATA_TEMPLATE``;
  * ``build_dispatcher.runner_bootstrap_user_data(job, repo_dir)`` — the
    parallel EPHEMERAL bootstrap generator, for jobs with and without a
    selected source_ref (``BUILD_REPO_URL`` mocked exactly as
    ``test_run_as_ubuntu_unit.py`` does; the empty no-repo-URL case
    legitimately renders nothing and is skip-asserted).

--------------------------------------------------------------------------
COUNTEREXAMPLES OBSERVED ON UNFIXED CODE (task 1 run, 2026-09-07)
--------------------------------------------------------------------------

Every render path failed, with the root apt-get install line lacking
``zip``/``unzip``:

  * ``build_fleet.render_user_data(...)`` (all generated
    (repo_dir, source_ref) combinations, and the concrete incident-shaped
    default render ``render_user_data('https://example.invalid/dda.git')``):
        apt-get install -y git
  * ``build_fleet.USER_DATA_TEMPLATE``:
        apt-get install -y git
  * ``build_dispatcher.runner_bootstrap_user_data(job, repo_dir)`` (job
    with and without source_ref, all generated repo_dirs):
        apt-get update -y && apt-get install -y git

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

No test launches EC2 compute, sends an SSM command, calls real AWS, or
starts a build. Both generators are pure text producers here; the
repository URL is a non-resolvable ``example.invalid`` address that is
only ever interpolated into generated text.

Run ONLY this file, from the repository root::

    python3 -m pytest \\
        test/backend-test/portal_builds/test_bootstrap_zip_exploration.py \\
        --noconftest -q -p no:cacheprovider

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import json
import os
import re
import sys
import types
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handlers bind boto3 clients and
# env-derived settings at import time. No AWS call is ever made; the
# dummy credentials only satisfy client construction (same discipline as
# test_source_selection_preservation.py / test_source_dir_alignment_property.py).
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "bootstrap-zip-explore"
os.environ["BUILD_JOBS_TABLE"] = f"dda-portal-build-jobs-{_SUFFIX}"
os.environ["BUILD_SERVERS_TABLE"] = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["SETTINGS_TABLE"] = f"dda-portal-settings-{_SUFFIX}"
os.environ["AUDIT_LOG_TABLE"] = f"dda-portal-audit-log-{_SUFFIX}"

# Nothing may be dispatched from any code path exercised here.
os.environ.pop("BUILD_DISPATCHER_FUNCTION_NAME", None)
# BUILD_REPO_URL is POPPED before import (sibling-file discipline):
# the dispatcher's module global starts '', and the repo-URL-bearing
# cases patch the module attribute exactly as test_run_as_ubuntu_unit.py
# does. The '' default is itself the skip-asserted no-repo-URL case.
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


# ---------------------------------------------------------------------------
# Minimal stand-ins for the Lambda layer modules the handlers import
# (same convention as the sibling exploration suites, e.g.
# test_jp7_ephemeral_provisioning_exploration.py).
# ---------------------------------------------------------------------------

def _fake_shared_utils():
    module = types.ModuleType("shared_utils")
    module.log_audit_event = lambda **kwargs: None
    module.create_response = lambda status_code, body: {
        "statusCode": status_code, "body": body}
    module.get_user_from_event = lambda event: {
        "user_id": "bootstrap-zip-explore", "role": "PortalAdmin"}
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


for _module in ("build_domain", "build_planner", "build_dispatcher",
                "build_fleet", "build_source", "shared_utils",
                "rbac_middleware"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()
sys.modules["rbac_middleware"] = _fake_rbac_middleware()

import build_domain  # noqa: E402
import build_dispatcher  # noqa: E402
import build_fleet  # noqa: E402

#: A deliberately non-resolvable repository URL, only ever interpolated
#: into generated text — the same value the alignment suite uses, and the
#: incident-shaped concrete case from the task.
REPO_URL = "https://example.invalid/dda.git"


# ---------------------------------------------------------------------------
# Root apt line extraction
# ---------------------------------------------------------------------------

#: Opening of a (quoted or bare) here-doc anywhere on a line:
#: ``<<'DDA_SOURCE_SYNC'``, ``<< 'PORTAL_RUN_EOF...'``, ``<<-EOF`` ...
_HEREDOC_OPEN = re.compile(r"<<-?\s*(?:'([A-Za-z0-9_]+)'|\"([A-Za-z0-9_]+)\""
                           r"|([A-Za-z0-9_]+))")


def _strip_heredoc_bodies(text):
    """The lines of ``text`` with every here-doc BODY removed.

    A here-doc body is not root-prologue shell: the fleet bootstrap
    transports its Source_Sync block through ``<<'DDA_SOURCE_SYNC'`` and
    the dispatcher transports the whole build-user body through the
    ``PORTAL_RUN_EOF``-family delimiter, and any apt text inside those
    runs as the build user (or is data), not as the root prologue.
    """
    kept = []
    pending_delimiter = None
    for line in text.splitlines():
        if pending_delimiter is not None:
            if line.strip() == pending_delimiter:
                pending_delimiter = None
            continue
        match = _HEREDOC_OPEN.search(line)
        kept.append(line)
        if match:
            pending_delimiter = (match.group(1) or match.group(2)
                                 or match.group(3))
    return kept


def _apt_install_packages(segment):
    """The package tokens of one ``apt-get ... install ...`` command
    segment, or None when the segment is not an apt-get install at all
    (``apt-get update``, coreutils ``install -d``, plain shell)."""
    tokens = segment.split()
    if "apt-get" not in tokens:
        return None
    apt_index = tokens.index("apt-get")
    rest = tokens[apt_index + 1:]
    if "install" not in rest:
        return None
    install_index = rest.index("install")
    return [token for token in rest[install_index + 1:]
            if not token.startswith("-")]


def root_apt_install_lines(text):
    """``(line, packages)`` for every ROOT apt-get install statement of a
    rendered bootstrap: outside every here-doc body, not under
    ``sudo`` (the build-user statements), comments excluded. Compound
    lines are split on ``&&`` / ``||`` / ``;`` so
    ``apt-get update -y && apt-get install -y git`` yields its install
    segment."""
    results = []
    for line in _strip_heredoc_bodies(text):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("sudo "):
            continue
        for segment in re.split(r"&&|\|\||;", stripped):
            packages = _apt_install_packages(segment)
            if packages is not None:
                results.append((stripped, packages))
    return results


def assert_root_apt_installs_zip(text, **context):
    """The POST-FIX expected behavior (bugfix.md Property, Fix Checking):
    the rendered bootstrap has at least one root apt-get install line and
    the union of its root-installed packages contains zip AND unzip.

    On UNFIXED code this fails with the counterexample apt line(s) in the
    assertion message — that failure is this task's result."""
    found = root_apt_install_lines(text)
    observed = json.dumps({
        "context": context,
        "root_apt_install_lines": [line for line, _ in found],
        "root_installed_packages": sorted(
            {package for _, packages in found for package in packages}),
    }, indent=2, default=str)
    assert found, (
        "no root apt-get install line found in the rendered bootstrap\n"
        + observed)
    packages = {package for _, packages in found for package in packages}
    assert "zip" in packages and "unzip" in packages, (
        "COUNTEREXAMPLE (bug condition isBugCondition(X) holds): the root "
        "apt-get install line(s) of this rendered bootstrap do not install "
        "zip/unzip — build-custom.sh's packaging step exits 127 on such a "
        "host\n" + observed)


# ---------------------------------------------------------------------------
# Generators — shell-safe values, matching the alignment suite's domains
# ---------------------------------------------------------------------------

#: Shell-safe absolute directories an operator could configure or a
#: bootstrap could have recorded (test_source_dir_alignment_property.py's
#: `_directories`), plus the None/'' defaulting variants.
_directories = st.sampled_from([
    "/home/ubuntu/DefectDetectionApplication",
    "/opt/dda/DefectDetectionApplication",
    "/srv/dda-build/tree",
    "/mnt/build/dda",
])
repo_dirs = st.one_of(st.none(), st.just(""), _directories)

#: Refs a launch/submission can select: nothing (None / ''), a branch, a
#: slashed feature branch, a tag, a 40-hex commit.
source_refs = st.one_of(
    st.none(),
    st.just(""),
    st.sampled_from(["main", "v1.2.3", "a" * 40,
                     "feature/portal-build-fleet-and-workflow-gates"]),
)


def _job(source_ref=None):
    """A minimal ephemeral Build_Job, the shape
    test_run_as_ubuntu_unit.py::_job uses."""
    return {
        "build_job_id": "0a1b2c3d",
        "build_target": build_domain.TARGET_JP6,
        "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
        "config_snapshot": {"source_ref": source_ref,
                            "max_runtime_hours": 4},
    }


# ===========================================================================
# Property 1 — Bug Condition: the dedicated fleet bootstrap
# (build_fleet.render_user_data / USER_DATA_TEMPLATE) never installs zip
# (Req 1.1 / 2.1). MUST FAIL on unfixed code.
# ===========================================================================

class TestFleetBootstrapRootAptInstallsZip:

    def test_incident_shaped_default_render(self):
        """The EXACT render path of the live incident: POST /build-servers
        → launch_build_server → run_fleet_instance →
        render_user_data(repo_url) with default repo_dir and no source_ref
        (srv-aac90870-033e-4e9c-9994-29ee895da421 /
        i-07c2ca92f3a526c93, Build_Job 53312133)."""
        text = build_fleet.render_user_data(REPO_URL)
        assert_root_apt_installs_zip(
            text, render_path="build_fleet.render_user_data",
            repo_url=REPO_URL, repo_dir=None, source_ref=None,
            incident="job 53312133 / srv-aac90870 / i-07c2ca92f3a526c93")

    def test_module_level_user_data_template(self):
        """The module-level USER_DATA_TEMPLATE (``{repo_url}`` its only
        placeholder) carries the same root apt line every render does."""
        text = build_fleet.USER_DATA_TEMPLATE.format(repo_url=REPO_URL)
        assert_root_apt_installs_zip(
            text, render_path="build_fleet.USER_DATA_TEMPLATE",
            repo_url=REPO_URL)

    @given(repo_dir=repo_dirs, source_ref=source_refs)
    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_every_fleet_render_installs_zip(self, repo_dir, source_ref):
        """For EVERY (repo_dir, source_ref) combination a dedicated launch
        can render — default and explicit directories, no ref and
        ref-bearing — the root apt line must install zip and unzip. Today
        it is exactly ``apt-get install -y git`` for all of them."""
        text = build_fleet.render_user_data(REPO_URL, repo_dir, source_ref)
        assert_root_apt_installs_zip(
            text, render_path="build_fleet.render_user_data",
            repo_url=REPO_URL, repo_dir=repo_dir, source_ref=source_ref)


# ===========================================================================
# Property 1 — Bug Condition: the ephemeral runner bootstrap
# (build_dispatcher.runner_bootstrap_user_data) never installs zip
# (Req 1.3 / 2.3). MUST FAIL on unfixed code.
# ===========================================================================

class TestRunnerBootstrapRootAptInstallsZip:

    def test_job_without_source_ref(self):
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            text = build_dispatcher.runner_bootstrap_user_data(_job(None))
        assert text, "expected non-empty runner bootstrap text"
        assert_root_apt_installs_zip(
            text,
            render_path="build_dispatcher.runner_bootstrap_user_data",
            source_ref=None)

    def test_job_with_source_ref(self):
        ref = "feature/portal-build-fleet-and-workflow-gates"
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            text = build_dispatcher.runner_bootstrap_user_data(_job(ref))
        assert text, "expected non-empty runner bootstrap text"
        assert_root_apt_installs_zip(
            text,
            render_path="build_dispatcher.runner_bootstrap_user_data",
            source_ref=ref)

    @given(repo_dir=repo_dirs, source_ref=source_refs)
    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_every_runner_render_installs_zip(self, repo_dir, source_ref):
        """For EVERY (repo_dir, source_ref) an ephemeral provisioning pass
        can render, the root prologue apt line must install zip and unzip.
        Today it is exactly
        ``apt-get update -y && apt-get install -y git``."""
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            text = build_dispatcher.runner_bootstrap_user_data(
                _job(source_ref), repo_dir or None)
        assert text, "expected non-empty runner bootstrap text"
        assert_root_apt_installs_zip(
            text,
            render_path="build_dispatcher.runner_bootstrap_user_data",
            repo_dir=repo_dir, source_ref=source_ref)

    def test_no_repo_url_renders_nothing(self):
        """The empty-string no-repo-URL case legitimately renders NO
        bootstrap at all (a pre-provisioned AMI is assumed), so there is
        no apt line to assert on — skip-asserted per the task."""
        assert build_dispatcher.BUILD_REPO_URL == ""
        text = build_dispatcher.runner_bootstrap_user_data(_job(None))
        assert text == "", (
            "with no BUILD_REPO_URL the runner bootstrap must render "
            f"nothing; got: {text!r}")
        pytest.skip("no repository URL configured: the runner bootstrap "
                    "legitimately renders no text, nothing to assert")
