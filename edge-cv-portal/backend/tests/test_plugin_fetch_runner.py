"""
Offline shell tests for the dda-plugin-fetch runner
(private-repo-plugin-import task 4.2; Requirements 1.5, 1.6, 1.8, 2.2, 3.3,
5.2, 6.2).

Drives `plugin-build-images/plugin-fetch/fetch.sh` against a local bare
repository standing in for GitHub/GitLab and the fake-aws.sh stand-in for
the AWS CLI (shared with the git-sync runner tests), so no network and no
account are needed. Every run asserts the token never reaches stdout,
stderr, result.json, or any file the runner leaves behind.
"""
import json
import os
import shutil
import stat
import subprocess

import pytest

HERE = os.path.dirname(__file__)
IMAGES_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "plugin-build-images"))
RUNNER = os.path.join(IMAGES_DIR, "plugin-fetch", "fetch.sh")
FAKE_AWS = os.path.join(IMAGES_DIR, "git-sync", "tests", "fake-aws.sh")
BUCKET = "test-artifacts"
TOKEN = "ghp_FETCHTESTTOKEN0123456789abcdefghijk"
DEST = "plugin-sources/uc-1/p-1/1"
RESULT_KEY = "plugin-sources/uc-1/p-1/1.fetch/result.json"


def git(*args, cwd=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class Harness:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.remote = tmp_path / "remote.git"
        self.s3_root = tmp_path / "s3"
        self.work = tmp_path / "work"
        self.repo_dir = tmp_path / "repo"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        aws = self.bin / "aws"
        shutil.copy(FAKE_AWS, aws)
        aws.chmod(aws.stat().st_mode | stat.S_IEXEC)
        self.s3_root.mkdir()
        git("init", "--bare", "--quiet", str(self.remote))
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=str(self.remote))
        self.commits = {}

    @property
    def repo_url(self):
        return f"file://{self.remote}"

    def seed(self, files, branch="main", message="seed", tag=None):
        clone = self.root / f"seed-{len(self.commits)}"
        git("clone", "--quiet", self.repo_url, str(clone))
        branches = git("for-each-ref", "--format=%(refname:short)", "refs/heads",
                       cwd=str(self.remote)).split()
        if branch in branches:
            git("checkout", "--quiet", branch, cwd=str(clone))
        elif branches:
            git("checkout", "--quiet", "-b", branch, cwd=str(clone))
        else:
            git("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=str(clone))
        for path, content in files.items():
            target = clone / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git("add", "-A", cwd=str(clone))
        git("commit", "--quiet", "-m", message, cwd=str(clone))
        if tag:
            git("tag", tag, cwd=str(clone))
            git("push", "--quiet", "origin", tag, cwd=str(clone))
        git("push", "--quiet", "origin", branch, cwd=str(clone))
        sha = git("rev-parse", "HEAD", cwd=str(clone))
        self.commits[message] = sha
        return sha

    def synced(self, prefix=DEST):
        base = self.s3_root / BUCKET / prefix
        if not base.exists():
            return {}
        return {str(p.relative_to(base)): p.read_text()
                for p in base.rglob("*") if p.is_file()}

    def result(self):
        path = self.s3_root / BUCKET / RESULT_KEY
        return json.loads(path.read_text()) if path.exists() else None

    def run(self, token=TOKEN, result_key=RESULT_KEY, **env):
        run_env = {
            **{k: v for k, v in os.environ.items()
               if k not in ("GIT_TOKEN", "GIT_ASKPASS", "SHALLOW", "REVISION",
                            "REPO_BRANCH", "REPO_SUBDIR", "RESULT_KEY")},
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_S3_ROOT": str(self.s3_root),
            "ARTIFACTS_BUCKET": BUCKET,
            "REPO_URL": self.repo_url,
            "DEST_PREFIX": DEST,
            "GIT_USERNAME": "x-access-token",
            "FETCH_WORK_DIR": str(self.work),
            "FETCH_REPO_DIR": str(self.repo_dir),
        }
        if token:
            run_env["GIT_TOKEN"] = token
        if result_key:
            run_env["RESULT_KEY"] = result_key
        run_env.update({k: str(v) for k, v in env.items()})
        proc = subprocess.run(["bash", RUNNER], env=run_env,
                              capture_output=True, text=True)
        # The token never surfaces anywhere observable (2.2).
        assert TOKEN not in proc.stdout and TOKEN not in proc.stderr
        for path in list(self.work.rglob("*")) if self.work.exists() else []:
            if path.is_file():
                assert TOKEN not in path.read_text(errors="replace"), path
        if self.result() is not None:
            assert TOKEN not in json.dumps(self.result())
        return proc


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


FILES = {"meson.build": "project('x', 'c')\n", "src/plugin.c": "int x;\n",
         "plugins/resize/meson.build": "project('resize', 'c')\n",
         "plugins/resize/resize.c": "int r;\n", "README.md": "hi\n"}


class TestAnonymousPreservation:
    def test_option_free_run_syncs_the_whole_tree_without_dot_git(self, h):
        sha = h.seed(FILES)
        proc = h.run(token=None, result_key=None)
        assert proc.returncode == 0, proc.stderr
        tree = h.synced()
        assert set(tree) == set(FILES)
        assert not any(p.startswith(".git") for p in tree)
        assert h.result() is None  # no RESULT_KEY -> no result document (6.2)
        assert "authenticated" not in proc.stdout
        assert sha  # sanity

    def test_revision_checkout_unchanged(self, h):
        h.seed(FILES, message="first", tag="v1.0")
        h.seed({**FILES, "README.md": "second\n"}, message="second")
        proc = h.run(token=None, result_key=None, REVISION="v1.0")
        assert proc.returncode == 0, proc.stderr
        assert h.synced()["README.md"] == "hi\n"


class TestAuthenticatedFetch:
    def test_askpass_clone_writes_result_with_commit_and_branch(self, h):
        sha = h.seed(FILES)
        proc = h.run()
        assert proc.returncode == 0, proc.stderr
        assert "authenticated fetch" in proc.stdout
        result = h.result()
        assert result["status"] == "succeeded"
        assert result["commit"] == sha
        assert result["branch"] == "main"
        assert result["failure_marker"] is None
        # The askpass helper references the environment, never the value.
        askpass = (h.work / "askpass.sh").read_text()
        assert "$GIT_TOKEN" in askpass and TOKEN not in askpass

    def test_branch_selects_the_clone_branch(self, h):
        h.seed(FILES, branch="main")
        sha = h.seed({**FILES, "README.md": "release\n"}, branch="release/1",
                     message="rel")
        proc = h.run(REPO_BRANCH="release/1")
        assert proc.returncode == 0, proc.stderr
        assert h.synced()["README.md"] == "release\n"
        result = h.result()
        assert result["commit"] == sha and result["branch"] == "release/1"

    def test_missing_branch_fails_fast_with_marker(self, h):
        h.seed(FILES)
        proc = h.run(REPO_BRANCH="does-not-exist")
        assert proc.returncode == 1
        assert h.result()["status"] == "failed"
        assert h.result()["failure_marker"] == "BRANCH_NOT_FOUND"
        assert h.synced() == {}

    def test_subdir_scopes_the_sync(self, h):
        h.seed(FILES)
        proc = h.run(REPO_SUBDIR="plugins/resize")
        assert proc.returncode == 0, proc.stderr
        assert set(h.synced()) == {"meson.build", "resize.c"}
        assert h.synced()["meson.build"] == "project('resize', 'c')\n"

    def test_missing_subdir_is_a_distinct_failure(self, h):
        h.seed(FILES)
        proc = h.run(REPO_SUBDIR="plugins/nope")
        assert proc.returncode == 1
        result = h.result()
        assert result["status"] == "failed"
        assert result["failure_marker"] == "PATH_NOT_FOUND"
        assert "plugins/nope" in result["stderr_tail"]
        assert h.synced() == {}

    @pytest.mark.parametrize("bad", ["../etc", "/abs", "a/../b"])
    def test_escaping_subdir_is_refused_even_if_the_lambda_missed_it(self, h, bad):
        h.seed(FILES)
        proc = h.run(REPO_SUBDIR=bad)
        assert proc.returncode == 1
        assert h.result()["failure_marker"] == "INVALID_SUBDIR"
        assert h.synced() == {}

    def test_shallow_clone_is_depth_one_and_still_syncs(self, h):
        h.seed(FILES, message="a")
        h.seed({**FILES, "README.md": "b\n"}, message="b")
        h.seed({**FILES, "README.md": "c\n"}, message="c")
        proc = h.run(SHALLOW="1")
        assert proc.returncode == 0, proc.stderr
        assert h.synced()["README.md"] == "c\n"
        assert h.result()["commit"] == h.commits["c"]
        # The runner removes .git before syncing, so depth is observable
        # only through what it logged.
        assert "(shallow)" in proc.stdout

    def test_shallow_clone_with_a_tag_revision_checks_it_out(self, h):
        h.seed(FILES, message="first", tag="v1.0")
        h.seed({**FILES, "README.md": "second\n"}, message="second")
        proc = h.run(SHALLOW="1", REVISION="v1.0")
        assert proc.returncode == 0, proc.stderr
        assert h.synced()["README.md"] == "hi\n"
        assert h.result()["commit"] == h.commits["first"]

    def test_shallow_clone_with_a_sha_revision_falls_back_to_deepen(self, h):
        first = h.seed(FILES, message="first")
        h.seed({**FILES, "README.md": "second\n"}, message="second")
        h.seed({**FILES, "README.md": "third\n"}, message="third")
        proc = h.run(SHALLOW="1", REVISION=first)
        assert proc.returncode == 0, proc.stderr
        assert h.synced()["README.md"] == "hi\n"
        assert h.result()["commit"] == first

    def test_unknown_revision_fails_with_marker(self, h):
        h.seed(FILES)
        proc = h.run(REVISION="v9.9.9")
        assert proc.returncode == 1
        assert h.result()["failure_marker"] == "REVISION_NOT_FOUND"
        assert h.synced() == {}

    def test_unreachable_repository_fails_with_stderr_captured(self, h):
        proc = h.run(REPO_URL="file:///nonexistent/repo.git")
        assert proc.returncode == 1
        result = h.result()
        assert result["status"] == "failed"
        assert result["failure_marker"] == "CLONE_FAILED"
        assert result["commit"] is None
        assert "nonexistent" in result["stderr_tail"]

    def test_no_token_leaves_no_askpass_and_no_home_override(self, h):
        h.seed(FILES)
        proc = h.run(token=None)
        assert proc.returncode == 0, proc.stderr
        assert not (h.work / "askpass.sh").exists()
        assert h.result()["status"] == "succeeded"


# ------------------------------------------------ the credential path (2.2)
#
# file:// remotes never prompt, so the tests above never exercise the
# askpass helper. A local HTTP server that serves the bare repository over
# git's dumb protocol and demands basic auth does: git gets a 401, asks
# GIT_ASKPASS for the username and the password, and retries.

import base64  # noqa: E402
import functools  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

_LAYER = os.path.join(HERE, "..", "layers", "shared", "python")
if _LAYER not in sys.path:
    sys.path.insert(0, _LAYER)
from git_connections import classify_failure  # noqa: E402


class BasicAuthGitServer:
    """Serves a bare repository (dumb HTTP) behind basic auth on 127.0.0.1."""

    def __init__(self, remote_dir, username, token):
        git("update-server-info", cwd=str(remote_dir))
        expected = "Basic " + base64.b64encode(
            f"{username}:{token}".encode()).decode()
        self.authorized = []
        self.challenged = 0
        server = self

        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                header = self.headers.get("Authorization")
                if header != expected:
                    server.challenged += 1
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.end_headers()
                    return
                server.authorized.append(self.path)
                super().do_GET()

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            functools.partial(Handler, directory=str(remote_dir)))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def auth_server(h):
    h.seed(FILES)
    server = BasicAuthGitServer(h.remote, "x-access-token", TOKEN)
    yield server
    server.close()


class TestCredentialPath:
    def test_askpass_helper_answers_gits_prompts(self, h):
        """The generated helper, run exactly as git runs it."""
        h.seed(FILES)
        assert h.run().returncode == 0
        askpass = h.work / "askpass.sh"
        env = {**os.environ, "GIT_USERNAME": "x-access-token", "GIT_TOKEN": TOKEN}
        username = subprocess.run(
            [str(askpass), "Username for 'https://github.com': "],
            env=env, capture_output=True, text=True, check=True).stdout
        password = subprocess.run(
            [str(askpass), "Password for 'https://x-access-token@github.com': "],
            env=env, capture_output=True, text=True, check=True).stdout
        assert username == "x-access-token\n"
        assert password == TOKEN + "\n"

    def test_http_remote_authenticates_through_askpass(self, h, auth_server):
        proc = h.run(REPO_URL=auth_server.url)
        assert proc.returncode == 0, proc.stderr
        assert auth_server.challenged >= 1  # git tried anonymously first...
        assert auth_server.authorized       # ...then retried with the token
        assert set(h.synced()) == set(FILES)
        result = h.result()
        assert result["status"] == "succeeded"
        assert result["commit"] == h.commits["seed"]
        # No credential helper stored the token: the isolated HOME holds
        # only the helper-reset gitconfig (the harness already asserted the
        # token is in no file under the work dir).
        home_files = sorted(p.name for p in (h.work / "home").rglob("*") if p.is_file())
        assert home_files == [".gitconfig"]

    def test_http_remote_rejects_a_wrong_token_fast(self, h, auth_server):
        proc = h.run(REPO_URL=auth_server.url, token="ghp_WRONGTOKEN0123456789abcdefghijk")
        assert proc.returncode == 1
        assert auth_server.authorized == []
        result = h.result()
        assert result["status"] == "failed"
        assert result["failure_marker"] == "CLONE_FAILED"
        assert "Authentication failed" in result["stderr_tail"]
        # The Lambda's shared classifier reads this as a token problem (3.1).
        assert classify_failure(result["stderr_tail"]) == "authentication"
        assert h.synced() == {}

    def test_http_remote_without_a_token_fails_without_prompting(self, h, auth_server):
        proc = h.run(REPO_URL=auth_server.url, token=None)
        assert proc.returncode == 1
        result = h.result()
        assert result["failure_marker"] == "CLONE_FAILED"
        assert "terminal prompts disabled" in result["stderr_tail"]
        assert classify_failure(result["stderr_tail"]) == "authentication"
        assert not (h.work / "askpass.sh").exists()
