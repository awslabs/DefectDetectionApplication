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
"""
Unit tests for the repository-directory resolver in
``edge-cv-portal/backend/functions/build_source.py``
(build-source-selection, task 3.1).

**Validates: Requirements 5.1, 5.2, 5.3, 5.4**

The rule is restated here independently of the implementation:

* ``DEFAULT_REPO_DIR`` is ``/home/ubuntu/DefectDetectionApplication`` —
  the location the existing dedicated bootstrap already clones into, so
  every server bootstrapped before this change keeps working untouched
  (Req 5.3).
* ``resolve_repo_dir(job, server=None, env_default=None)`` resolves in
  strict precedence: the directory recorded for this server/runner
  (``server['repo_dir']``, else ``job['runner']['repo_dir']``), else the
  configured environment override, else ``DEFAULT_REPO_DIR``.
* A recorded value that is absent, ``None`` or empty carries no
  information: all three are "not recorded" and fall through identically.
* ``agent_script_path(repo_dir)`` roots the agent script in the resolved
  directory, so the invoked path can never drift from the bootstrap
  directory (Req 5.1, 5.2).

Task 3.4 adds the Property 1 / Property 2 hypothesis tests; this file is
the resolver's own precedence unit coverage.

Pure module test: no AWS clients, no network, no compute. Run with
``--noconftest`` like the rest of the ``portal_builds`` suite.
"""
import os
import sys

import pytest

# Import the pure source module from the portal Lambda bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_source  # noqa: E402

#: The directory every dedicated server bootstrapped before this change
#: actually cloned into (`build_fleet.USER_DATA_TEMPLATE`).
LEGACY_BOOTSTRAP_REPO_DIR = "/home/ubuntu/DefectDetectionApplication"

#: The three ways a record can say "nothing was recorded".
NOT_RECORDED = ("absent", None, "")

RECORDED = "/srv/recorded/DefectDetectionApplication"
ENV_OVERRIDE = "/opt/dda/DefectDetectionApplication"


def _server(repo_dir_field="absent", server_id="srv-1"):
    server = {"server_id": server_id,
              "instance_id": "i-0123456789abcdef0"}
    if repo_dir_field != "absent":
        server["repo_dir"] = repo_dir_field
    return server


def _job(runner_repo_dir_field="absent", with_runner=True):
    job = {"build_job_id": "job-1", "build_target": "jp5"}
    if with_runner:
        runner = {"instance_id": "i-abcdefabcdefabcd0"}
        if runner_repo_dir_field != "absent":
            runner["repo_dir"] = runner_repo_dir_field
        job["runner"] = runner
    return job


class TestDefaultRepoDir:
    """Req 5.3: the default IS the legacy bootstrap location."""

    def test_default_is_the_legacy_bootstrap_directory(self):
        assert build_source.DEFAULT_REPO_DIR == LEGACY_BOOTSTRAP_REPO_DIR

    def test_module_is_pure(self):
        """No AWS clients / resources are created by importing it."""
        for attribute in ("boto3", "ssm", "ec2", "dynamodb", "client",
                          "resource"):
            assert not hasattr(build_source, attribute), attribute


class TestResolvePrecedence:
    """Req 5.1, 5.2, 5.4: recorded, then env override, then default."""

    def test_recorded_server_directory_wins_over_env_and_default(self):
        resolved = build_source.resolve_repo_dir(
            _job(), _server(RECORDED), env_default=ENV_OVERRIDE)
        assert resolved == RECORDED

    def test_recorded_runner_directory_wins_over_env_and_default(self):
        resolved = build_source.resolve_repo_dir(
            _job(RECORDED), None, env_default=ENV_OVERRIDE)
        assert resolved == RECORDED

    def test_server_record_wins_over_the_runner_record(self):
        """A dedicated server's own directory is the one its bootstrap
        used, so it outranks anything carried on the job."""
        resolved = build_source.resolve_repo_dir(
            _job("/runner/dir"), _server("/server/dir"),
            env_default=ENV_OVERRIDE)
        assert resolved == "/server/dir"

    def test_env_override_wins_over_the_default_when_nothing_recorded(self):
        resolved = build_source.resolve_repo_dir(
            _job(), _server(), env_default=ENV_OVERRIDE)
        assert resolved == ENV_OVERRIDE

    def test_default_when_neither_recorded_nor_configured(self):
        resolved = build_source.resolve_repo_dir(_job(), _server())
        assert resolved == build_source.DEFAULT_REPO_DIR

    def test_default_when_the_env_override_is_empty(self):
        """An unset override arrives as an empty string from os.environ
        defaults; it must not shadow DEFAULT_REPO_DIR."""
        assert build_source.resolve_repo_dir(
            _job(), _server(), env_default="") == \
            build_source.DEFAULT_REPO_DIR
        assert build_source.resolve_repo_dir(
            _job(), _server(), env_default=None) == \
            build_source.DEFAULT_REPO_DIR


class TestNotRecordedVariants:
    """Req 5.3: absent / None / empty are the same input."""

    @pytest.mark.parametrize("field", NOT_RECORDED)
    def test_server_variants_all_fall_through_to_the_default(self, field):
        assert build_source.resolve_repo_dir(_job(), _server(field)) == \
            build_source.DEFAULT_REPO_DIR

    @pytest.mark.parametrize("field", NOT_RECORDED)
    def test_server_variants_all_fall_through_to_the_env_override(self, field):
        assert build_source.resolve_repo_dir(
            _job(), _server(field), env_default=ENV_OVERRIDE) == ENV_OVERRIDE

    @pytest.mark.parametrize("field", NOT_RECORDED)
    def test_runner_variants_all_fall_through_identically(self, field):
        assert build_source.resolve_repo_dir(_job(field), None) == \
            build_source.DEFAULT_REPO_DIR
        assert build_source.resolve_repo_dir(
            _job(field), None, env_default=ENV_OVERRIDE) == ENV_OVERRIDE

    def test_all_three_variants_resolve_identically(self):
        resolved = {
            build_source.resolve_repo_dir(_job(field), _server(field),
                                          env_default=ENV_OVERRIDE)
            for field in NOT_RECORDED
        }
        assert resolved == {ENV_OVERRIDE}

    @pytest.mark.parametrize("field", NOT_RECORDED)
    def test_an_unrecorded_server_still_reads_the_runner_record(self, field):
        """"Not recorded" on the server falls through to the runner, not
        straight to the configured value."""
        assert build_source.resolve_repo_dir(
            _job(RECORDED), _server(field),
            env_default=ENV_OVERRIDE) == RECORDED


class TestResolveIsTotal:
    """Resolution never raises and never mutates its inputs (Req 5.4)."""

    @pytest.mark.parametrize("job", [None, {}, {"runner": None},
                                     {"runner": {}}, {"runner": "i-123"}])
    def test_missing_or_odd_job_shapes_resolve_to_the_default(self, job):
        assert build_source.resolve_repo_dir(job) == \
            build_source.DEFAULT_REPO_DIR

    @pytest.mark.parametrize("server", [None, {}, {"repo_dir": 17},
                                        {"repo_dir": "   "}])
    def test_missing_or_odd_server_shapes_resolve_to_the_default(self, server):
        assert build_source.resolve_repo_dir({}, server) == \
            build_source.DEFAULT_REPO_DIR

    def test_inputs_are_not_mutated(self):
        job = _job()
        server = _server()
        job_before, server_before = dict(job), dict(server)
        build_source.resolve_repo_dir(job, server, env_default=ENV_OVERRIDE)
        assert job == job_before
        assert server == server_before


class TestAgentScriptPath:
    """Req 5.1, 5.2: the agent path is composed from the resolved dir."""

    @pytest.mark.parametrize("repo_dir", [
        "/home/ubuntu/DefectDetectionApplication",
        "/opt/dda/DefectDetectionApplication",
        "/srv/recorded/DefectDetectionApplication",
    ])
    def test_composition(self, repo_dir):
        assert build_source.agent_script_path(repo_dir) == \
            f"{repo_dir}/scripts/portal-build-agent.sh"

    def test_path_is_rooted_in_the_resolved_directory(self):
        for field in NOT_RECORDED + (RECORDED,):
            resolved = build_source.resolve_repo_dir(_job(), _server(field))
            path = build_source.agent_script_path(resolved)
            assert path.startswith(f"{resolved}/")
            assert path.endswith("/scripts/portal-build-agent.sh")

    def test_default_resolution_yields_the_legacy_agent_path(self):
        assert build_source.agent_script_path(
            build_source.resolve_repo_dir({}, {})) == \
            f"{LEGACY_BOOTSTRAP_REPO_DIR}/scripts/portal-build-agent.sh"
