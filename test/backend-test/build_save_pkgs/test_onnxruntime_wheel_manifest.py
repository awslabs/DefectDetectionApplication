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
"""Static pins for the Jetson images' GPU onnxruntime source: the committed
manifest ``src/backend/edge_ml1_p_camera_management/onnxruntime-wheels.manifest``
(one row per JetPack line: version, wheel file name, wheel sha256, upstream
release commit), its reader ``onnxruntime_wheel_manifest.sh``, and the two
scripts that consume it.

What is pinned:

* the manifest rows themselves (JetPack 5 / 6 / 7, the versions the three
  Dockerfiles' ORT lines pair with their CUDA/TensorRT bases, 64-hex wheel
  digests, 40-hex release commits whose abbreviations are the ``git-commit-id``
  the shipped wheels' build info reports, wheel names that spell the version and
  the cp311 aarch64 tags);
* the reader's lookups and its fail-closed ``ort_manifest_verify`` (a wheel of
  another JetPack line, a tampered wheel and an unknown JetPack are refused; the
  real digest passes), run through bash exactly as the scripts source it;
* ``install_onnxruntime_gpu.sh``: it sources the reader, tries a staged wheel
  FIRST (verified against the manifest before ``pip install``), falls back to a
  source checkout of the manifest's pinned commit (fetched by id from the
  configured repository, no tag or branch name, VERSION_NUMBER guarded) and
  only then compiles; the old tag-based clone of the upstream URL is gone;
* ``build-custom.sh``: it stages the manifest's wheel from the component bucket
  with owner-asserted reads, verifies the download against the manifest, stops
  on a mismatch, and lets an absent object fall through to the source build;
* the staged directory is gitignored (a wheel must never be committed).

Text-only; the one bash subprocess runs the sourceable reader against the
committed manifest and against temporary fixtures. Runs under ``--noconftest``.
"""
import hashlib
import os
import re
import subprocess
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
MGMT = os.path.join(REPO_ROOT, "src", "backend", "edge_ml1_p_camera_management")
MANIFEST = os.path.join(MGMT, "onnxruntime-wheels.manifest")
READER = os.path.join(MGMT, "onnxruntime_wheel_manifest.sh")
INSTALL = os.path.join(MGMT, "install_onnxruntime_gpu.sh")
BUILD_CUSTOM = os.path.join(REPO_ROOT, "build-custom.sh")
GITIGNORE = os.path.join(REPO_ROOT, ".gitignore")

# The three rows the manifest must carry: JetPack line -> (version, abbreviated
# build-info commit of the shipped wheel). The versions are the ones the
# Dockerfiles' CUDA/TensorRT bases pair with (install_onnxruntime_gpu.sh's
# "Per-JetPack defaults" table); the commits are what the published images'
# wheels report in onnxruntime.get_build_info().
EXPECTED_ROWS = {
    "5": ("1.16.3", "2ac381c"),
    "6": ("1.20.1", "5c1b7cc"),
    "7": ("1.23.2", "a83fc4d"),
}
SOURCE_REPO = "https://github.com/intel/onnxruntime.git"
UPSTREAM_TAG_CLONE = "https://github.com/microsoft/onnxruntime.git"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def manifest_rows():
    rows = {}
    for line in _read(MANIFEST).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split()
        assert len(parts) == 5, f"manifest row must have 5 columns: {line!r}"
        rows[parts[0]] = {"version": parts[1], "wheel": parts[2], "sha256": parts[3], "commit": parts[4]}
    return rows


def bash(snippet, env=None):
    """Run ``snippet`` in bash with the reader sourced; returns (rc, stdout, stderr)."""
    full_env = dict(os.environ)
    full_env.update(env or {})
    proc = subprocess.run(
        ["bash", "-c", f'. "{READER}"\n{snippet}'],
        capture_output=True, text=True, env=full_env, timeout=120,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


class TestManifestRows:
    def test_three_jetpack_rows_with_well_formed_columns(self):
        rows = manifest_rows()
        assert set(rows) == set(EXPECTED_ROWS), sorted(rows)
        for jp, row in rows.items():
            version, commit_abbrev = EXPECTED_ROWS[jp]
            assert row["version"] == version, (jp, row)
            assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), (jp, row["sha256"])
            assert re.fullmatch(r"[0-9a-f]{40}", row["commit"]), (jp, row["commit"])
            assert row["commit"].startswith(commit_abbrev), (
                f"JetPack {jp}: commit {row['commit']} is not the shipped wheel's build-info "
                f"commit {commit_abbrev}")
            assert row["wheel"] == f"onnxruntime_gpu-{version}-cp311-cp311-linux_aarch64.whl", (jp, row["wheel"])

    def test_digests_and_commits_are_distinct(self):
        rows = manifest_rows()
        assert len({r["sha256"] for r in rows.values()}) == 3
        assert len({r["commit"] for r in rows.values()}) == 3
        assert len({r["wheel"] for r in rows.values()}) == 3

    def test_manifest_explains_both_uses(self):
        text = _read(MANIFEST)
        assert "VERSION_NUMBER" in text
        assert "sha256" in text
        assert "install_onnxruntime_gpu.sh" in text and "build-custom.sh" in text


class TestReader:
    def test_lookups_match_the_manifest(self):
        rows = manifest_rows()
        for jp, row in rows.items():
            rc, out, _ = bash(
                f'echo "$(ort_manifest_version {jp})|$(ort_manifest_wheel_name {jp})|'
                f'$(ort_manifest_sha256 {jp})|$(ort_manifest_commit {jp})"')
            assert rc == 0
            assert out == f"{row['version']}|{row['wheel']}|{row['sha256']}|{row['commit']}"

    def test_unknown_jetpack_is_refused(self):
        rc, out, _ = bash("ort_manifest_version 4; echo rc=$?")
        assert out.endswith("rc=1") and "." not in out.replace("rc=1", "")

    def test_default_source_repository_is_the_configured_mirror(self):
        rc, out, _ = bash('echo "$ORT_SOURCE_REPO_URL"')
        assert rc == 0 and out == SOURCE_REPO

    def test_verify_passes_only_the_listed_digest(self):
        """A temporary manifest with the digest of a temporary file: the real
        digest passes (rc 0); a tampered byte, another JetPack line's name and
        an unlisted JetPack are refused (rc 2, nothing printed on stdout)."""
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "onnxruntime_gpu-9.9.9-cp311-cp311-linux_aarch64.whl")
            with open(good, "wb") as fh:
                fh.write(b"not a real wheel, just bytes to hash\n")
            digest = hashlib.sha256(open(good, "rb").read()).hexdigest()
            manifest = os.path.join(tmp, "m.manifest")
            with open(manifest, "w", encoding="utf-8") as fh:
                fh.write("# fixture\n")
                fh.write(f"5 9.9.9 {os.path.basename(good)} {digest} {'0' * 40}\n")
                fh.write(f"6 8.8.8 onnxruntime_gpu-8.8.8-cp311-cp311-linux_aarch64.whl {'f' * 64} {'1' * 40}\n")
            env = {"ORT_MANIFEST_FILE": manifest}
            rc, out, err = bash(f'ort_manifest_verify "{good}" 5; echo rc=$?', env)
            assert out == "rc=0", (out, err)
            # staged lookup finds the listed name only
            rc, out, _ = bash(f'ort_manifest_staged_wheel "{tmp}" 5; echo rc=$?', env)
            assert out == f"{good}\nrc=0"
            rc, out, _ = bash(f'ort_manifest_staged_wheel "{tmp}" 6; echo rc=$?', env)
            assert out == "rc=1"
            # tampered content
            with open(good, "ab") as fh:
                fh.write(b"x")
            rc, out, err = bash(f'ort_manifest_verify "{good}" 5; echo rc=$?', env)
            assert out == "rc=2" and "sha256 mismatch" in err, (out, err)
            # the right bytes under another line's name are refused by name
            other = os.path.join(tmp, "onnxruntime_gpu-8.8.8-cp311-cp311-linux_aarch64.whl")
            os.replace(good, other)
            rc, out, err = bash(f'ort_manifest_verify "{other}" 5; echo rc=$?', env)
            assert out == "rc=2" and "is not the JetPack 5 wheel" in err, (out, err)
            # unknown JetPack
            rc, out, err = bash(f'ort_manifest_verify "{other}" 7; echo rc=$?', env)
            assert out == "rc=2" and "no entry for JetPack" in err, (out, err)

    def test_verify_against_the_committed_manifest_refuses_a_wrong_file(self):
        """Against the real manifest: a file carrying a listed name but other
        bytes is refused, so the committed digests are what gates an install."""
        rows = manifest_rows()
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, rows["6"]["wheel"])
            with open(fake, "wb") as fh:
                fh.write(b"\x00" * 16)
            rc, out, err = bash(f'ort_manifest_verify "{fake}" 6; echo rc=$?')
            assert out == "rc=2" and rows["6"]["sha256"] in err, (out, err)

    def test_pinned_source_fetch_refuses_a_malformed_commit_without_touching_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "src")
            rc, out, err = bash(f'ort_fetch_pinned_source "{dest}" "{SOURCE_REPO}" v1.16.3 1.16.3; echo rc=$?')
            assert out == "rc=2" and "not a 40-hex commit id" in err, (out, err)
            assert not os.path.exists(dest)

    def test_pinned_source_fetch_guards_version_number(self):
        """A local repository whose VERSION_NUMBER differs from the expected
        version is refused after checkout (rc 2); the right version passes."""
        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin")
            env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
            subprocess.run(["git", "init", "-q", origin], check=True)
            with open(os.path.join(origin, "VERSION_NUMBER"), "w") as fh:
                fh.write("1.20.1\n")
            subprocess.run(["git", "-C", origin, "add", "VERSION_NUMBER"], check=True)
            subprocess.run(["git", "-C", origin, "commit", "-q", "-m", "fixture"], check=True,
                           env={**os.environ, **env})
            subprocess.run(["git", "-C", origin, "config", "uploadpack.allowReachableSHA1InWant", "true"], check=True)
            commit = subprocess.check_output(["git", "-C", origin, "rev-parse", "HEAD"], text=True).strip()
            rc, out, err = bash(f'ort_fetch_pinned_source "{tmp}/wrong" "{origin}" {commit} 1.16.3; echo rc=$?')
            assert out.endswith("rc=2") and "VERSION_NUMBER '1.20.1', not 1.16.3" in err, (out, err)
            rc, out, err = bash(f'ort_fetch_pinned_source "{tmp}/right" "{origin}" {commit} 1.20.1; echo rc=$?')
            assert out.endswith("rc=0") and f"checked out {commit} (VERSION_NUMBER 1.20.1)" in out, (out, err)


class TestInstallScript:
    def test_sources_the_reader_and_takes_versions_from_the_manifest(self):
        text = _read(INSTALL)
        assert '. "${SCRIPT_DIR}/onnxruntime_wheel_manifest.sh"' in text
        assert 'MANIFEST_VERSION="$(ort_manifest_version "${JETPACK_MAJOR}")"' in text
        assert 'ONNXRUNTIME_COMMIT="${ONNXRUNTIME_COMMIT:-$(ort_manifest_commit "${JETPACK_MAJOR}")}"' in text
        # the per-JetPack case no longer hardcodes a version to clone
        assert not re.search(r'ONNXRUNTIME_VERSION:-v1\.\d+\.\d+', text)

    def test_order_staged_wheel_then_pinned_source_then_compile(self):
        text = _read(INSTALL)
        staged = text.index('ort_manifest_staged_wheel "${STAGED_DIR}" "${JETPACK_MAJOR}"')
        verify = text.index('ort_manifest_verify "${STAGED_WHL}" "${JETPACK_MAJOR}" || exit 1')
        staged_install = text.index('install_wheel_and_verify "${STAGED_WHL}"')
        fetch = text.index('ort_fetch_pinned_source onnxruntime "${ORT_SOURCE_REPO_URL}" "${ONNXRUNTIME_COMMIT}" "${ONNXRUNTIME_VERSION#v}" || exit 1')
        compile_step = text.index("./tools/ci_build/build.py")
        assert staged < verify < staged_install < fetch < compile_step
        # the staged path verifies BEFORE installing and ends the script
        tail = text[staged_install:fetch]
        assert "exit 0" in tail
        # no apt-get, build-dependency pip install or source fetch runs before
        # the staged decision: the staged path needs no network beyond the
        # wheel install itself (comments and the shared install helper's own
        # `pip install` of the wheel excluded)
        head_lines = [ln for ln in text[:staged].splitlines() if not ln.lstrip().startswith("#")]
        assert not any("apt-get" in ln for ln in head_lines), [ln for ln in head_lines if "apt-get" in ln]
        assert not any("ort_fetch_pinned_source" in ln for ln in head_lines)
        assert [ln for ln in head_lines if "pip install" in ln] == [
            '    ${PYBIN} -m pip install --no-cache-dir "${whl}"']

    def test_source_fallback_is_the_pinned_commit_not_a_tag(self):
        text = _read(INSTALL)
        assert UPSTREAM_TAG_CLONE not in text
        assert "--branch" not in text
        assert 'git clone' not in text  # the fetch-by-id helper replaces it
        assert "VERSION_NUMBER" in text

    def test_version_override_needs_a_commit(self):
        text = _read(INSTALL)
        assert "set ONNXRUNTIME_COMMIT to the 40-hex release commit" in text
        # a non-manifest version never uses a staged wheel
        assert 'if [ "${ONNXRUNTIME_VERSION#v}" = "${MANIFEST_VERSION}" ]; then\n    STAGED_WHL=' in text


class TestBuildCustom:
    def test_stages_the_manifest_wheel_with_owner_asserted_reads(self):
        text = _read(BUILD_CUSTOM)
        block_start = text.index("# ── Staged GPU onnxruntime wheel (JP5/JP6/JP7)")
        block_end = text.index('echo "Building docker-compose images from')
        block = text[block_start:block_end]
        assert ". backend/edge_ml1_p_camera_management/onnxruntime_wheel_manifest.sh" in block
        assert ". ../scripts/s3-bucket-owner-guard.sh" in block
        assert 'ORT_BUCKET="$(dda_component_bucket_name "$ORT_REGION" "$ORT_ACCOUNT")"' in block
        for call in ("head-bucket", "head-object", "get-object"):
            assert re.search(rf"aws s3api {call} .*--expected-bucket-owner \"\$ORT_ACCOUNT\"", block), call
        assert 'ort_manifest_verify "$ORT_STAGED_DIR/$ORT_WHEEL_NAME" "$JETPACK_ARG"' in block
        # mismatch stops the build; absence falls through to the source build
        assert "does not match onnxruntime-wheels.manifest; aborting build." in block
        assert block.count("compiles onnxruntime from source") >= 3
        # only GPU Jetson targets, and the staged dir is cleared first
        assert '[ "$ONNXRUNTIME_GPU" = "1" ] && [ "$IS_JP5$IS_JP6$IS_JP7" != "000" ]' in block
        assert 'rm -rf "$ORT_STAGED_DIR"' in block
        # the block runs before the compose build
        assert block_end > block_start

    def test_staged_directory_is_gitignored(self):
        assert "src/backend/edge_ml1_p_camera_management/onnxruntime-wheels/" in _read(GITIGNORE).splitlines()


@pytest.mark.parametrize("path", [MANIFEST, READER, INSTALL, BUILD_CUSTOM])
def test_scripts_parse(path):
    if path.endswith(".manifest"):
        assert manifest_rows()
        return
    proc = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
