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
Unit tests for the Sync_Generator in
``edge-cv-portal/backend/functions/build_source.py``
(build-source-selection, task 5.1).

**Validates: Requirements 4.3, 4.4**

The rule is restated here independently of the implementation. The
generated text must express exactly what ``scripts/portal-build-agent.sh``
Step 2 already does, because the agent re-runs its own sync after the
preamble and the two must be idempotent together:

* clone when the tree is absent, guarded on ``$REPO_DIR/.git``;
* ``git fetch --prune origin``;
* ``git checkout --force -B <ref> origin/<ref>`` when
  ``refs/remotes/origin/<ref>`` verifies, else ``git checkout --force
  <ref>``;
* every failure branch echoes ``PORTAL_SOURCE_SYNC_FAILED kind=<class>
  repository=<url> ref=<ref>`` and exits 65 (repository unreachable) or 66
  (ref not found) — never a bare 127, the opaque live failure
  (SSM ``e9281bdc`` / ``d75f1ea2``) this spec exists to eliminate;
* an empty or ``None`` ref yields the clone-only sequence, i.e. exactly
  today's behavior;
* ``bootstrap_commands`` is the sync plus ``bash ./setup-build-server.sh``
  plus the Bootstrap_Marker write as its last statement.

Two layers of coverage:

1. **Structural** — the generated command list, including injection safety
   for the repository, directory and ref (all three are operator input in
   Increment B, so hostile values are exercised: ``main; rm -rf /``,
   backticks, ``$(...)``, embedded quotes).
2. **Behavioral** — the generated text is actually executed against a
   temporary LOCAL git origin (``file`` paths only, no network, no AWS, no
   compute): a branch ref checks out, a tag checks out detached, a bogus
   ref exits 66 with the marker line, an unreachable origin exits 65, and
   hostile ref text never executes.

Pure module test. Run with ``--noconftest`` like the rest of the
``portal_builds`` suite.
"""
import os
import shlex
import subprocess
import sys

import pytest

# Import the pure source module from the portal Lambda bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_planner  # noqa: E402  (the one definition of the marker path)
import build_source  # noqa: E402

REPO_URL = "https://github.com/awslabs/DefectDetectionApplication"
REPO_DIR = "/home/ubuntu/DefectDetectionApplication"

#: Values an operator can type into the repository / ref fields in
#: Increment B. Each one changes the meaning of the script if it is
#: interpolated unquoted.
HOSTILE_VALUES = [
    "main; rm -rf /",
    "main && touch /tmp/dda-pwned",
    "main`touch /tmp/dda-pwned`",
    "main$(touch /tmp/dda-pwned)",
    "main'; touch /tmp/dda-pwned; '",
    'main" ; touch /tmp/dda-pwned ; "',
    "main\nrm -rf /",
    "main | tee /tmp/dda-pwned",
    "${REPO_DIR}-injected",
    "--upload-pack=touch /tmp/dda-pwned",
]


def script_text(commands):
    """The commands as the shell body a caller would run them as."""
    return "\n".join(commands)


# ---------------------------------------------------------------------------
# Structural: the sync semantics mirror the agent's Step 2
# ---------------------------------------------------------------------------

def test_branch_arm_recreates_the_local_branch_at_the_remote_tip():
    """The verified-remote-ref arm is `checkout --force -B <ref> origin/<ref>`."""
    commands = build_source.source_sync_commands(REPO_URL, REPO_DIR, "main")
    text = script_text(commands)

    # The branch test is the agent's exact rev-parse verification.
    assert ('git rev-parse --verify --quiet "refs/remotes/origin/$SOURCE_REF"'
            in text)
    # The verified arm recreates the local branch at the remote tip.
    assert ('git checkout --force -B "$SOURCE_REF" "origin/$SOURCE_REF"'
            in text)
    # The fetch precedes both checkout arms.
    assert text.index("git fetch --prune origin") < text.index("git checkout")


def test_non_branch_arm_is_a_plain_force_checkout():
    """The else arm (tag / commit SHA) is `checkout --force <ref>`."""
    commands = build_source.source_sync_commands(REPO_URL, REPO_DIR, "v1.2.3")
    text = script_text(commands)

    assert 'git checkout --force "$SOURCE_REF"' in text
    # The two arms are an if/else over the rev-parse verification, in that
    # order: branch first, non-branch fallback second.
    branch_arm = text.index('git checkout --force -B "$SOURCE_REF"')
    plain_arm = text.index('git checkout --force "$SOURCE_REF"')
    assert branch_arm < plain_arm
    assert "\nelse\n" in "\n" + text + "\n"


def test_clone_is_guarded_on_the_existing_git_directory():
    """Clone-if-absent, guarded exactly like the existing bootstrap."""
    commands = build_source.source_sync_commands(REPO_URL, REPO_DIR, "main")
    text = script_text(commands)

    assert 'if [ ! -d "$REPO_DIR/.git" ]; then' in text
    assert 'git clone "$REPO_URL" "$REPO_DIR"' in text
    assert 'mkdir -p "$(dirname "$REPO_DIR")"' in text
    # The tree is entered before any git operation on it.
    assert text.index('cd "$REPO_DIR"') < text.index("git fetch")


@pytest.mark.parametrize("ref", [None, "", "   ", 0, [], {}])
def test_absent_ref_yields_the_clone_only_sequence(ref):
    """No selected ref means today's behavior: clone only, no sync."""
    commands = build_source.source_sync_commands(REPO_URL, REPO_DIR, ref)
    text = script_text(commands)

    assert "git clone" in text
    assert "git fetch" not in text
    assert "git checkout" not in text
    assert "git rev-parse" not in text
    # The clone-guard failure branch is still classified, never bare.
    assert build_source.SYNC_MARKER in text
    # All three variables are still assigned: the failure branch references
    # them and the ephemeral user-data body runs under `set -u`.
    assert "SOURCE_REF=''" in text
    assert f"REPO_URL={shlex.quote(REPO_URL)}" in text


def test_absent_repo_dir_falls_back_to_the_authoritative_default():
    commands = build_source.source_sync_commands(REPO_URL, None, "main")
    assert f"REPO_DIR={shlex.quote(build_source.DEFAULT_REPO_DIR)}" in commands


# ---------------------------------------------------------------------------
# Structural: safe.directory precedes every git statement
# (live fix: SSM 30327734 / job 19f270c2 / srv-3f963f3b — the root-run
# preamble hits "detected dubious ownership" on a ubuntu-owned dedicated
# clone)
# ---------------------------------------------------------------------------

SAFE_DIRECTORY_LINE = (
    'git config --global --add safe.directory "$REPO_DIR" '
    "2>/dev/null || true"
)


@pytest.mark.parametrize("ref", [None, "", "main", "v1.2.3", "0" * 40])
def test_safe_directory_is_marked_before_any_git_statement(ref):
    """The safe.directory line sits after the variable assignments and
    before the first git clone/fetch statement, in both the ref and the
    clone-only sequences, so a root-run sync can operate on a tree the
    ubuntu user created."""
    commands = build_source.source_sync_commands(REPO_URL, REPO_DIR, ref)

    assert SAFE_DIRECTORY_LINE in commands
    index = commands.index(SAFE_DIRECTORY_LINE)
    # After all three assignments (it references $REPO_DIR).
    assignment_indexes = [i for i, line in enumerate(commands)
                          if line.startswith(("REPO_DIR=", "REPO_URL=",
                                              "SOURCE_REF="))]
    assert assignment_indexes and index > max(assignment_indexes)
    # Before the first git operation on the repository (clone and, when a
    # ref is selected, fetch).
    git_ops = [i for i, line in enumerate(commands)
               if "git clone" in line or "git fetch" in line]
    assert git_ops and index < min(git_ops)


def test_safe_directory_is_guarded_so_it_cannot_add_a_failure_mode():
    """`|| true`-guarded and stderr-silenced: a read-only home or a missing
    git binary must not introduce a new sync failure class."""
    for ref in (None, "main"):
        commands = build_source.source_sync_commands(REPO_URL, REPO_DIR, ref)
        line = commands[commands.index(SAFE_DIRECTORY_LINE)]
        assert line.endswith("|| true")
        assert "2>/dev/null" in line
        # It references the existing variable, never a new interpolation.
        assert '"$REPO_DIR"' in line
        assert REPO_DIR not in line


# ---------------------------------------------------------------------------
# Structural: failure classification (Req 4.4)
# ---------------------------------------------------------------------------

def test_constants_are_the_documented_marker_and_exit_codes():
    assert build_source.SYNC_MARKER == "PORTAL_SOURCE_SYNC_FAILED"
    assert build_source.EXIT_REPO_UNREACHABLE == 65
    assert build_source.EXIT_REF_NOT_FOUND == 66


def test_every_failure_branch_carries_the_marker_line_and_a_class():
    """Marker + kind + repository + ref on every failure branch."""
    commands = build_source.source_sync_commands(REPO_URL, REPO_DIR, "main")
    failure_lines = [c for c in commands if build_source.SYNC_MARKER in c]

    # clone, cd, fetch, branch checkout, non-branch checkout
    assert len(failure_lines) == 5
    for line in failure_lines:
        assert "repository=$REPO_URL" in line
        assert "ref=$SOURCE_REF" in line
        assert ("kind=repository_unreachable" in line
                or "kind=ref_not_found" in line)
        assert ("exit 65" in line) or ("exit 66" in line)


def test_repository_failures_exit_65_and_ref_failures_exit_66():
    commands = build_source.source_sync_commands(REPO_URL, REPO_DIR, "main")
    by_command = {}
    for line in commands:
        if build_source.SYNC_MARKER not in line:
            continue
        head = line.split("||")[0].strip()
        by_command[head] = line

    unreachable = [head for head, line in by_command.items()
                   if "kind=repository_unreachable" in line]
    not_found = [head for head, line in by_command.items()
                 if "kind=ref_not_found" in line]

    # Obtaining or entering the repository → 65.
    assert any("git clone" in head for head in unreachable)
    assert any(head.startswith('cd "$REPO_DIR"') for head in unreachable)
    assert any("git fetch" in head for head in unreachable)
    for head in unreachable:
        assert "exit 65" in by_command[head]

    # Checking out the requested ref → 66.
    assert len(not_found) == 2
    for head in not_found:
        assert "git checkout" in head
        assert "exit 66" in by_command[head]


def test_no_failure_branch_leaks_a_bare_127():
    """127 is the opaque live failure this spec exists to eliminate."""
    for ref in (None, "main", "v1.2.3", "0" * 40):
        text = script_text(
            build_source.source_sync_commands(REPO_URL, REPO_DIR, ref))
        assert "exit 127" not in text
        # Every `exit` in the generated text is one of the two classified
        # codes.
        codes = {token.split(";")[0].strip()
                 for token in text.split("exit ")[1:]}
        assert codes <= {"65", "66"}, codes


# ---------------------------------------------------------------------------
# Structural: injection safety (every interpolated value is shell-quoted)
# ---------------------------------------------------------------------------

def assignments(commands):
    """The generated `VAR=value` assignment lines, parsed back to values."""
    parsed = {}
    for line in commands:
        for var in (build_source.VAR_REPO_DIR, build_source.VAR_REPO_URL,
                    build_source.VAR_SOURCE_REF):
            prefix = f"{var}="
            if line.startswith(prefix):
                # A single shell word: quoting round-trips to the raw value.
                words = shlex.split(line)
                assert len(words) == 1, line
                parsed[var] = words[0][len(prefix):]
    return parsed


@pytest.mark.parametrize("hostile", HOSTILE_VALUES)
def test_hostile_ref_is_quoted_and_appears_only_in_its_assignment(hostile):
    commands = build_source.source_sync_commands(REPO_URL, REPO_DIR, hostile)

    # The value survives quoting exactly (shlex round-trip).
    assert assignments(commands)[build_source.VAR_SOURCE_REF] == hostile
    # The quoted form is what got interpolated.
    assert f"SOURCE_REF={shlex.quote(hostile)}" in commands
    # Nowhere else: every other line references the shell variable only.
    others = [c for c in commands if not c.startswith("SOURCE_REF=")]
    for line in others:
        assert hostile not in line


@pytest.mark.parametrize("hostile", HOSTILE_VALUES)
def test_hostile_repository_and_directory_are_quoted(hostile):
    commands = build_source.source_sync_commands(hostile, hostile, "main")
    parsed = assignments(commands)

    assert parsed[build_source.VAR_REPO_URL] == hostile
    assert parsed[build_source.VAR_REPO_DIR] == hostile
    assert f"REPO_URL={shlex.quote(hostile)}" in commands
    assert f"REPO_DIR={shlex.quote(hostile)}" in commands
    others = [c for c in commands
              if not (c.startswith("REPO_URL=") or c.startswith("REPO_DIR="))]
    for line in others:
        assert hostile not in line


@pytest.mark.parametrize("ref", [None, "main", "v1.2.3"] + HOSTILE_VALUES)
def test_generated_text_is_syntactically_valid_shell(ref):
    """`bash -n` over the generated body (hostile values included)."""
    body = "#!/bin/bash\nset -uo pipefail\n" + script_text(
        build_source.source_sync_commands(REPO_URL, REPO_DIR, ref)) + "\n"
    result = subprocess.run(["bash", "-n"], input=body, text=True,
                            capture_output=True)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Structural: bootstrap_commands (the shared user-data body)
# ---------------------------------------------------------------------------

def test_bootstrap_commands_is_the_sync_plus_setup_plus_marker_write():
    sync = build_source.source_sync_commands(REPO_URL, REPO_DIR, "main")
    commands = build_source.bootstrap_commands(REPO_URL, REPO_DIR, "main")

    # The sync is used verbatim: one generator, no divergent second copy.
    assert commands[:len(sync)] == sync
    assert commands[len(sync)] == "bash ./setup-build-server.sh"
    # The Bootstrap_Marker write is the LAST statement.
    assert commands[-1] == f"touch {build_planner.BOOTSTRAP_MARKER_PATH}"
    assert len(commands) == len(sync) + 2


def test_bootstrap_marker_path_is_not_duplicated_in_build_source():
    """The marker/log literals live in build_planner only (task 4.1)."""
    with open(os.path.join(_FUNCTIONS_DIR, "build_source.py")) as handle:
        source = handle.read()
    assert "dda-build-server-bootstrap" not in source
    assert "BOOTSTRAP_MARKER_PATH" in source  # referenced, not re-spelled


def test_bootstrap_commands_clone_only_for_an_absent_ref():
    commands = build_source.bootstrap_commands(REPO_URL, REPO_DIR, None)
    text = script_text(commands)
    assert "git checkout" not in text
    assert commands[-1] == f"touch {build_planner.BOOTSTRAP_MARKER_PATH}"


# ---------------------------------------------------------------------------
# Behavioral: run the generated text against a temporary LOCAL git origin
# ---------------------------------------------------------------------------

def git_env(home):
    """Hermetic git environment: no user, system or global config."""
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "DDA Test",
        "GIT_AUTHOR_EMAIL": "dda-test@example.com",
        "GIT_COMMITTER_NAME": "DDA Test",
        "GIT_COMMITTER_EMAIL": "dda-test@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def run_git(args, cwd, env):
    result = subprocess.run(["git"] + args, cwd=str(cwd), env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, f"git {args}: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def local_origin(tmp_path):
    """A local git origin: `main` WITHOUT the agent script, a feature
    branch WITH it, and a tag — the shape of the live failure, where
    scripts/portal-build-agent.sh exists only off the default branch.

    No network: the generated `git clone` runs against this filesystem
    path.
    """
    env = git_env(tmp_path / "home")
    (tmp_path / "home").mkdir()
    origin = tmp_path / "origin"
    origin.mkdir()
    run_git(["init", "-q"], origin, env)
    run_git(["checkout", "-q", "-b", "main"], origin, env)
    (origin / "README.md").write_text("default branch\n")
    run_git(["add", "-A"], origin, env)
    run_git(["commit", "-q", "-m", "default branch"], origin, env)
    run_git(["tag", "v1.0.0"], origin, env)

    run_git(["checkout", "-q", "-b", "feature/agent"], origin, env)
    (origin / "scripts").mkdir()
    (origin / "scripts" / "portal-build-agent.sh").write_text("#!/bin/bash\n")
    run_git(["add", "-A"], origin, env)
    run_git(["commit", "-q", "-m", "add the build agent"], origin, env)
    run_git(["checkout", "-q", "main"], origin, env)

    return {"path": origin, "env": env, "home": tmp_path / "home"}


def run_sync(local_origin, tmp_path, repo_dir, ref, repo_url=None, name="sync"):
    """Execute the generated sync text and return the completed process."""
    body = "#!/bin/bash\nset -uo pipefail\n" + script_text(
        build_source.source_sync_commands(
            repo_url if repo_url is not None else str(local_origin["path"]),
            str(repo_dir), ref)) + "\n"
    script = tmp_path / f"{name}.sh"
    script.write_text(body)
    return subprocess.run(["bash", str(script)], capture_output=True,
                          text=True, env=local_origin["env"],
                          cwd=str(tmp_path))


def test_generated_sync_checks_out_a_branch_that_carries_the_agent(
        local_origin, tmp_path):
    """The live case: the agent exists only on the non-default branch."""
    work = tmp_path / "work"
    result = run_sync(local_origin, tmp_path, work, "feature/agent")

    assert result.returncode == 0, result.stdout + result.stderr
    env = local_origin["env"]
    assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], work, env) == \
        "feature/agent"
    # The `-B` arm produced a real local branch at the remote tip.
    assert run_git(["rev-parse", "HEAD"], work, env) == \
        run_git(["rev-parse", "refs/remotes/origin/feature/agent"], work, env)
    # And the agent script the dispatcher invokes is now obtainable.
    assert (work / "scripts" / "portal-build-agent.sh").exists()
    assert build_source.SYNC_MARKER not in result.stdout


def test_generated_sync_is_idempotent_over_an_existing_tree(
        local_origin, tmp_path):
    """Re-running the same sync (the agent re-runs it) is a no-op."""
    work = tmp_path / "work"
    first = run_sync(local_origin, tmp_path, work, "feature/agent",
                     name="first")
    assert first.returncode == 0, first.stdout + first.stderr
    head = run_git(["rev-parse", "HEAD"], work, local_origin["env"])

    second = run_sync(local_origin, tmp_path, work, "feature/agent",
                      name="second")
    assert second.returncode == 0, second.stdout + second.stderr
    assert run_git(["rev-parse", "HEAD"], work, local_origin["env"]) == head


def test_generated_sync_checks_out_a_tag_through_the_non_branch_arm(
        local_origin, tmp_path):
    work = tmp_path / "work"
    result = run_sync(local_origin, tmp_path, work, "v1.0.0")

    assert result.returncode == 0, result.stdout + result.stderr
    env = local_origin["env"]
    # A tag is checked out detached (the plain `--force` arm).
    assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], work, env) == "HEAD"
    assert run_git(["rev-parse", "HEAD"], work, env) == \
        run_git(["rev-parse", "v1.0.0^{commit}"], work, env)


def test_clone_only_sequence_leaves_the_default_branch(
        local_origin, tmp_path):
    work = tmp_path / "work"
    result = run_sync(local_origin, tmp_path, work, None)

    assert result.returncode == 0, result.stdout + result.stderr
    assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], work,
                   local_origin["env"]) == "main"


def test_bogus_ref_exits_66_with_the_marker_line(local_origin, tmp_path):
    work = tmp_path / "work"
    result = run_sync(local_origin, tmp_path, work, "no-such-ref-42")

    assert result.returncode == build_source.EXIT_REF_NOT_FOUND
    marker = [line for line in result.stdout.splitlines()
              if line.startswith(build_source.SYNC_MARKER)]
    assert len(marker) == 1, result.stdout
    assert "kind=ref_not_found" in marker[0]
    # The line names BOTH the repository and the ref (Req 4.4).
    assert f"repository={local_origin['path']}" in marker[0]
    assert "ref=no-such-ref-42" in marker[0]


def test_unreachable_origin_exits_65_with_the_marker_line(
        local_origin, tmp_path):
    missing = tmp_path / "no-such-origin"
    result = run_sync(local_origin, tmp_path, tmp_path / "work2", "main",
                      repo_url=str(missing))

    assert result.returncode == build_source.EXIT_REPO_UNREACHABLE
    marker = [line for line in result.stdout.splitlines()
              if line.startswith(build_source.SYNC_MARKER)]
    assert len(marker) == 1, result.stdout
    assert "kind=repository_unreachable" in marker[0]
    assert f"repository={missing}" in marker[0]
    assert "ref=main" in marker[0]


def test_ref_naming_a_shell_variable_is_not_expanded(local_origin, tmp_path):
    """A ref of `$REPO_DIR` stays literal text end to end."""
    result = run_sync(local_origin, tmp_path, tmp_path / "work", "$REPO_DIR")

    assert result.returncode == build_source.EXIT_REF_NOT_FOUND
    marker = [line for line in result.stdout.splitlines()
              if line.startswith(build_source.SYNC_MARKER)]
    assert len(marker) == 1, result.stdout
    assert "ref=$REPO_DIR" in marker[0]


@pytest.mark.parametrize("hostile", HOSTILE_VALUES)
def test_hostile_ref_never_executes(local_origin, tmp_path, hostile):
    """Injection canary: the hostile ref fails as a ref, nothing runs."""
    canary = tmp_path / "canary"
    ref = hostile.replace("/tmp/dda-pwned", str(canary)).replace(
        "rm -rf /", f"touch {canary}")
    result = run_sync(local_origin, tmp_path, tmp_path / "work", ref)

    assert not canary.exists(), result.stdout + result.stderr
    # It is rejected as a ref, with the classified code — never a bare 127.
    assert result.returncode == build_source.EXIT_REF_NOT_FOUND
    assert result.returncode != 127
