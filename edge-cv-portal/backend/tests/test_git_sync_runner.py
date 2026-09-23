"""
Offline shell tests for the dda-plugin-git-sync runner
(custom-node-source-lifecycle task 6.2).

Drives edge-cv-portal/plugin-build-images/git-sync/runner.sh against a
local bare repository standing in for GitHub/GitLab and the fake-aws.sh
stub standing in for the AWS CLI (s3 sync/cp mapped to a local directory).
No network, no credentials: authentication-failure CLASSIFICATION is
covered by sourcing the script and calling `classify` on canned stderr.

Covers: verify (3.5 default branch detection), push happy path and the
Sync_Manifest (3.3, 3.10), branch creation from the default branch and on
an empty repository (3.5), no-change pushes (3.6), the Divergence_Guard
with and without `force` (3.7, 3.8), the moving-remote retry and
push_rejected (3.9), pull happy path with manifest/symlink exclusion
(4.3), missing ref/path (4.4), the file/size limits (4.5), the
result.json contract for every outcome, and the token never appearing in
the result or the log (2.4, 9.5).

Skipped when `git` is not installed.
"""
import json
import os
import shutil
import stat
import subprocess
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
GIT_SYNC_DIR = os.path.abspath(os.path.join(
    HERE, "..", "..", "plugin-build-images", "git-sync"))
RUNNER = os.path.join(GIT_SYNC_DIR, "runner.sh")
FAKE_AWS = os.path.join(GIT_SYNC_DIR, "tests", "fake-aws.sh")

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

BUCKET = "test-artifacts"
TOKEN = "ghp_SECRETTOKEN0123456789"


def git(*args, cwd=None, env=None):
    base_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    if env:
        base_env.update(env)
    return subprocess.run(["git", *args], cwd=cwd, env=base_env, check=True,
                          capture_output=True, text=True).stdout.strip()


class Harness:
    """A bare remote, a fake S3 root, and a runner invoker."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.remote = tmp_path / "remote.git"
        self.s3_root = tmp_path / "s3"
        self.work = tmp_path / "work"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        aws = self.bin / "aws"
        shutil.copy(FAKE_AWS, aws)
        aws.chmod(aws.stat().st_mode | stat.S_IEXEC)
        os.chmod(RUNNER, os.stat(RUNNER).st_mode | stat.S_IEXEC)
        self.s3_root.mkdir()
        git("init", "--bare", "--quiet", str(self.remote))
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=str(self.remote))

    @property
    def repo_url(self):
        return f"file://{self.remote}"

    # ---------------------------------------------------------- remote setup
    def seed_remote(self, files, branch="main", message="seed"):
        """Commit `files` ({path: content}) to the bare remote's branch."""
        clone = self.root / f"seed-{abs(hash(tuple(sorted(files))))}"
        if clone.exists():
            shutil.rmtree(clone)
        git("clone", "--quiet", self.repo_url, str(clone))
        if branch in self.remote_branches():
            git("checkout", "--quiet", branch, cwd=str(clone))
        elif self.remote_branches():
            git("checkout", "--quiet", "-b", branch, cwd=str(clone))
        else:
            # Empty remote: the clone has an unborn HEAD; name it.
            git("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=str(clone))
        for path, content in files.items():
            target = clone / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)
        git("add", "-A", cwd=str(clone))
        git("commit", "--quiet", "-m", message, cwd=str(clone))
        git("push", "--quiet", "origin", branch, cwd=str(clone))
        return git("rev-parse", "HEAD", cwd=str(clone))

    def remote_tree(self, branch="main", path=""):
        """{relative path: content} of the remote branch (under `path`)."""
        listing = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", branch, *( [path] if path else [])],
            cwd=str(self.remote), capture_output=True, text=True, check=True).stdout.split()
        out = {}
        for name in listing:
            content = subprocess.run(["git", "show", f"{branch}:{name}"],
                                     cwd=str(self.remote), capture_output=True,
                                     check=True).stdout
            out[name] = content.decode("utf-8", errors="replace")
        return out

    def remote_head(self, branch="main"):
        return git("rev-parse", branch, cwd=str(self.remote))

    def remote_branches(self):
        return git("for-each-ref", "--format=%(refname:short)", "refs/heads",
                   cwd=str(self.remote)).split()

    def install_hook(self, script):
        hooks = self.remote / "hooks"
        hooks.mkdir(exist_ok=True)
        hook = hooks / "pre-receive"
        hook.write_text(script)
        hook.chmod(0o755)

    # ---------------------------------------------------------- fake s3
    def put_source(self, prefix, files):
        base = self.s3_root / BUCKET / prefix
        for path, content in files.items():
            target = base / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def staged_tree(self, prefix):
        base = self.s3_root / BUCKET / prefix
        if not base.exists():
            return {}
        return {str(p.relative_to(base)): p.read_text()
                for p in base.rglob("*") if p.is_file()}

    # ---------------------------------------------------------- runner
    def run(self, kind, **env):
        if self.work.exists():
            shutil.rmtree(self.work)
        result_file = self.root / f"result-{kind}-{len(os.listdir(self.root))}.json"
        run_env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_S3_ROOT": str(self.s3_root),
            "SYNC_KIND": kind,
            "OPERATION_ID": "op-1",
            "REPO_URL": self.repo_url,
            "GIT_PROVIDER": "github",
            "GIT_USERNAME": "x-access-token",
            "GIT_TOKEN": TOKEN,
            "ARTIFACTS_BUCKET": BUCKET,
            "RESULT_KEY": "plugin-git-sync/op-1/result.json",
            "RESULT_FILE": str(result_file),
            "WORK_DIR": str(self.work),
        }
        run_env.update({k: str(v) for k, v in env.items()})
        proc = subprocess.run(["bash", RUNNER], env=run_env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert result_file.exists(), "runner must always write result.json"
        result = json.loads(result_file.read_text())
        assert result["kind"] == kind
        assert isinstance(result["ok"], bool)
        assert TOKEN not in proc.stdout and TOKEN not in proc.stderr
        assert TOKEN not in result_file.read_text()
        return result, proc


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


SOURCE = {
    "plugin/frame_processing_hook.py": "def process_frame(frame, params):\n    return frame\n",
    "plugin/gstblur.c": "#include <gst/gst.h>\n",
    "builds/x86_64/meson.build": "project('blur', 'c')\n",
    "README.md": "# blur\n",
}
MANIFEST = json.dumps({"ddaPlugin": 1, "pluginId": "p-1", "version": 1})


# ------------------------------------------------------------------- verify

class TestVerify:
    def test_ok_detects_default_branch(self, h):
        h.seed_remote({"README.md": "x"}, branch="main")
        result, _ = h.run("verify")
        assert result["ok"] is True
        assert result["default_branch"] == "main"

    def test_missing_repository_is_not_found(self, h):
        result, _ = h.run("verify", REPO_URL=f"file://{h.root}/nope.git")
        assert result["ok"] is False
        assert result["category"] == "not_found"
        assert result["message"]


# --------------------------------------------------------------------- push

class TestPush:
    def test_happy_path_replaces_path_and_writes_manifest(self, h):
        first = h.seed_remote({"docs/other.md": "keep me\n",
                               "plugins/blur/stale.txt": "old\n"})
        h.put_source("plugin-sources/uc/p-1/1/", SOURCE)
        result, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main",
                          REPO_PATH="plugins/blur",
                          SOURCE_PREFIX="plugin-sources/uc/p-1/1/",
                          COMMIT_MESSAGE="DDA Portal: blur v1", MANIFEST_JSON=MANIFEST)
        assert result["ok"] is True, result
        assert result["commit"] == h.remote_head()
        assert result["commit"] != first
        assert result["no_changes"] is False
        assert result["branch_created"] is False
        tree = h.remote_tree()
        assert tree["docs/other.md"] == "keep me\n"          # outside path untouched (3.4)
        assert "plugins/blur/stale.txt" not in tree           # removed (3.3)
        for path, content in SOURCE.items():
            assert tree[f"plugins/blur/{path}"] == content
        assert json.loads(tree["plugins/blur/dda-plugin.json"]) == json.loads(MANIFEST)
        assert result["files"] == len(SOURCE) + 1
        log = git("log", "-1", "--format=%s", "main", cwd=str(h.remote))
        assert log == "DDA Portal: blur v1"

    def test_creates_branch_from_default(self, h):
        h.seed_remote({"docs/other.md": "x\n"}, branch="main")
        h.put_source("plugin-sources/uc/p-1/1/", SOURCE)
        result, _ = h.run("push", BRANCH="plugins/blur-dev", DEFAULT_BRANCH="main",
                          REPO_PATH="blur", SOURCE_PREFIX="plugin-sources/uc/p-1/1/")
        assert result["ok"] is True, result
        assert result["branch_created"] is True
        assert "plugins/blur-dev" in h.remote_branches()
        tree = h.remote_tree(branch="plugins/blur-dev")
        assert tree["docs/other.md"] == "x\n"
        assert tree["blur/README.md"] == SOURCE["README.md"]

    def test_empty_repository_gets_first_commit(self, h):
        h.put_source("plugin-sources/uc/p-1/1/", SOURCE)
        result, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main",
                          REPO_PATH="blur", SOURCE_PREFIX="plugin-sources/uc/p-1/1/")
        assert result["ok"] is True, result
        assert result["branch_created"] is True
        assert h.remote_branches() == ["main"]
        assert set(h.remote_tree()) == {f"blur/{p}" for p in SOURCE}

    def test_identical_content_makes_no_commit(self, h):
        h.put_source("plugin-sources/uc/p-1/1/", SOURCE)
        first, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main", REPO_PATH="blur",
                         SOURCE_PREFIX="plugin-sources/uc/p-1/1/", MANIFEST_JSON=MANIFEST)
        second, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main", REPO_PATH="blur",
                          SOURCE_PREFIX="plugin-sources/uc/p-1/1/", MANIFEST_JSON=MANIFEST,
                          LAST_SYNC_COMMIT=first["commit"])
        assert second["ok"] is True
        assert second["no_changes"] is True
        assert second["commit"] == first["commit"] == h.remote_head()

    def test_divergence_guard_blocks_then_force_overwrites(self, h):
        h.put_source("plugin-sources/uc/p-1/1/", SOURCE)
        first, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main", REPO_PATH="blur",
                         SOURCE_PREFIX="plugin-sources/uc/p-1/1/")
        # Someone edits the mirrored directory in the repository.
        h.seed_remote({"blur/plugin/gstblur.c": "// edited in IDE\n"})
        h.put_source("plugin-sources/uc/p-1/1/", {**SOURCE, "README.md": "# v2\n"})

        blocked, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main", REPO_PATH="blur",
                           SOURCE_PREFIX="plugin-sources/uc/p-1/1/",
                           LAST_SYNC_COMMIT=first["commit"])
        assert blocked["ok"] is False
        assert blocked["category"] == "diverged"
        assert blocked["changed_files"] == ["blur/plugin/gstblur.c"]
        assert h.remote_tree()["blur/plugin/gstblur.c"] == "// edited in IDE\n"  # untouched

        forced, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main", REPO_PATH="blur",
                          SOURCE_PREFIX="plugin-sources/uc/p-1/1/",
                          LAST_SYNC_COMMIT=first["commit"], FORCE=1)
        assert forced["ok"] is True, forced
        tree = h.remote_tree()
        assert tree["blur/plugin/gstblur.c"] == SOURCE["plugin/gstblur.c"]
        assert tree["blur/README.md"] == "# v2\n"

    def test_changes_outside_path_do_not_diverge(self, h):
        h.put_source("plugin-sources/uc/p-1/1/", SOURCE)
        first, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main", REPO_PATH="blur",
                         SOURCE_PREFIX="plugin-sources/uc/p-1/1/")
        h.seed_remote({"docs/changelog.md": "unrelated\n"})
        h.put_source("plugin-sources/uc/p-1/1/", {**SOURCE, "README.md": "# v2\n"})
        result, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main", REPO_PATH="blur",
                          SOURCE_PREFIX="plugin-sources/uc/p-1/1/",
                          LAST_SYNC_COMMIT=first["commit"])
        assert result["ok"] is True, result
        tree = h.remote_tree()
        assert tree["docs/changelog.md"] == "unrelated\n"
        assert tree["blur/README.md"] == "# v2\n"

    def test_unreachable_last_sync_commit_is_diverged(self, h):
        h.seed_remote({"docs/x.md": "x\n"})
        h.put_source("plugin-sources/uc/p-1/1/", SOURCE)
        result, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main", REPO_PATH="blur",
                          SOURCE_PREFIX="plugin-sources/uc/p-1/1/",
                          LAST_SYNC_COMMIT="0" * 40)
        assert result["ok"] is False and result["category"] == "diverged"
        assert result["changed_files"] == ["<unknown>"]

    def test_moving_remote_is_retried_once(self, h):
        h.seed_remote({"docs/x.md": "x\n"})
        h.put_source("plugin-sources/uc/p-1/1/", SOURCE)
        marker = h.root / "hook-fired"
        # First push attempt: the hook advances main with an unrelated commit
        # and rejects; the runner must fetch, rebase, and push again.
        h.install_hook(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            set -e
            if [ ! -f "{marker}" ]; then
              touch "{marker}"
              read old new ref
              # Leave the push quarantine so the ref update lands.
              unset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
              export GIT_INDEX_FILE="$(mktemp)"
              git read-tree "$old"
              blob=$(printf 'concurrent\\n' | git hash-object -w --stdin)
              git update-index --add --cacheinfo 100644,"$blob",docs/concurrent.md
              tree=$(git write-tree)
              commit=$(GIT_AUTHOR_NAME=o GIT_AUTHOR_EMAIL=o@x GIT_COMMITTER_NAME=o \\
                       GIT_COMMITTER_EMAIL=o@x git commit-tree "$tree" -p "$old" -m concurrent)
              git update-ref "$ref" "$commit" "$old"
              echo "simulated concurrent push" >&2
              exit 1
            fi
            exit 0
        """))
        result, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main", REPO_PATH="blur",
                          SOURCE_PREFIX="plugin-sources/uc/p-1/1/")
        assert result["ok"] is True, result
        tree = h.remote_tree()
        assert tree["docs/concurrent.md"] == "concurrent\n"
        assert tree["blur/README.md"] == SOURCE["README.md"]
        assert result["commit"] == h.remote_head()

    def test_persistent_rejection_is_push_rejected(self, h):
        h.seed_remote({"docs/x.md": "x\n"})
        h.put_source("plugin-sources/uc/p-1/1/", SOURCE)
        h.install_hook("#!/usr/bin/env bash\necho 'nope' >&2\nexit 1\n")
        before = h.remote_head()
        result, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main", REPO_PATH="blur",
                          SOURCE_PREFIX="plugin-sources/uc/p-1/1/")
        assert result["ok"] is False
        assert result["category"] == "push_rejected"
        assert h.remote_head() == before

    def test_invalid_repo_path_is_internal(self, h):
        h.put_source("plugin-sources/uc/p-1/1/", SOURCE)
        result, _ = h.run("push", BRANCH="main", DEFAULT_BRANCH="main",
                          REPO_PATH="../escape", SOURCE_PREFIX="plugin-sources/uc/p-1/1/")
        assert result["ok"] is False and result["category"] == "internal"


# --------------------------------------------------------------------- pull

class TestPull:
    def test_happy_path_stages_tree_without_manifest_or_symlinks(self, h):
        commit = h.seed_remote({f"blur/{p}": c for p, c in SOURCE.items()}
                               | {"blur/dda-plugin.json": MANIFEST, "docs/other.md": "o\n"})
        # Add a symlink inside the mirrored path.
        clone = h.root / "linker"
        git("clone", "--quiet", h.repo_url, str(clone))
        os.symlink("README.md", clone / "blur" / "LINK.md")
        git("add", "-A", cwd=str(clone))
        git("commit", "--quiet", "-m", "link", cwd=str(clone))
        git("push", "--quiet", "origin", "main", cwd=str(clone))
        head = h.remote_head()
        assert head != commit

        result, _ = h.run("pull", BRANCH="main", REF="main", REPO_PATH="blur",
                          STAGING_PREFIX="plugin-git-sync/op-1/tree/")
        assert result["ok"] is True, result
        assert result["commit"] == head
        assert result["ref"] == "main"
        staged = h.staged_tree("plugin-git-sync/op-1/tree/")
        assert staged == SOURCE
        assert result["tree_files"] == len(SOURCE)
        assert result["tree_bytes"] == sum(len(c.encode()) for c in SOURCE.values())

    def test_pull_by_commit_sha_and_tag(self, h):
        first = h.seed_remote({"blur/a.txt": "one\n"})
        git("tag", "v1", first, cwd=str(h.remote))
        h.seed_remote({"blur/a.txt": "two\n"})
        by_sha, _ = h.run("pull", REF=first, REPO_PATH="blur",
                          STAGING_PREFIX="plugin-git-sync/op-sha/tree/")
        assert by_sha["ok"] is True and by_sha["commit"] == first
        assert h.staged_tree("plugin-git-sync/op-sha/tree/") == {"a.txt": "one\n"}
        by_tag, _ = h.run("pull", REF="v1", REPO_PATH="blur",
                          STAGING_PREFIX="plugin-git-sync/op-tag/tree/")
        assert by_tag["ok"] is True and by_tag["commit"] == first

    def test_missing_ref_is_not_found(self, h):
        h.seed_remote({"blur/a.txt": "one\n"})
        result, _ = h.run("pull", REF="no-such-branch", REPO_PATH="blur",
                          STAGING_PREFIX="plugin-git-sync/op-1/tree/")
        assert result["ok"] is False and result["category"] == "not_found"
        assert h.staged_tree("plugin-git-sync/op-1/tree/") == {}

    def test_missing_path_is_not_found(self, h):
        h.seed_remote({"other/a.txt": "one\n"})
        result, _ = h.run("pull", REF="main", REPO_PATH="blur",
                          STAGING_PREFIX="plugin-git-sync/op-1/tree/")
        assert result["ok"] is False and result["category"] == "not_found"

    def test_manifest_only_path_is_not_found(self, h):
        h.seed_remote({"blur/dda-plugin.json": MANIFEST})
        result, _ = h.run("pull", REF="main", REPO_PATH="blur",
                          STAGING_PREFIX="plugin-git-sync/op-1/tree/")
        assert result["ok"] is False and result["category"] == "not_found"

    def test_file_limit(self, h):
        h.seed_remote({f"blur/{p}": c for p, c in SOURCE.items()})
        result, _ = h.run("pull", REF="main", REPO_PATH="blur",
                          STAGING_PREFIX="plugin-git-sync/op-1/tree/", MAX_TREE_FILES=2)
        assert result["ok"] is False and result["category"] == "invalid_source"
        assert result["limit"] == "files" and result["tree_files"] == len(SOURCE)
        assert h.staged_tree("plugin-git-sync/op-1/tree/") == {}

    def test_byte_limit(self, h):
        h.seed_remote({f"blur/{p}": c for p, c in SOURCE.items()})
        result, _ = h.run("pull", REF="main", REPO_PATH="blur",
                          STAGING_PREFIX="plugin-git-sync/op-1/tree/", MAX_TREE_BYTES=10)
        assert result["ok"] is False and result["category"] == "invalid_source"
        assert result["limit"] == "bytes"


# ------------------------------------------------------ sourced unit checks

class TestSourcedHelpers:
    def _call(self, fn, arg):
        proc = subprocess.run(
            ["bash", "-c", f'source "{RUNNER}"; {fn} "$1"', "_", arg],
            capture_output=True, text=True, env={**os.environ, "SYNC_KIND": "verify"})
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    @pytest.mark.parametrize("stderr, category", [
        ("remote: Invalid username or password.\nfatal: Authentication failed for 'https://x'", "authentication"),
        ("fatal: could not read Username for 'https://github.com': terminal prompts disabled", "authentication"),
        ("remote: HTTP Basic: Access denied\nfatal: unable to access 'https://gitlab/x.git/': The requested URL returned error: 403", "authentication"),
        ("remote: Repository not found.\nfatal: repository 'https://github.com/o/r.git/' not found", "not_found"),
        ("fatal: couldn't find remote ref refs/heads/nope", "not_found"),
        ("fatal: unable to access 'https://gitlab.example/x.git/': Could not resolve host: gitlab.example", "unreachable"),
        ("fatal: unable to access 'https://x/': Failed to connect to x port 443: Connection timed out", "unreachable"),
        ("error: something odd happened", "internal"),
        ("", "internal"),
    ])
    def test_classify(self, stderr, category):
        assert self._call("classify", stderr) == category

    def test_unknown_kind_writes_internal_result(self, h):
        result, _ = h.run("bogus")
        assert result["ok"] is False and result["category"] == "internal"
