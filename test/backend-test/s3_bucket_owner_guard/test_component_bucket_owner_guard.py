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
"""S3 bucket-squatting regression tests for the component publish scripts.

The Greengrass component artifact bucket is named deterministically from the
publishing account and region (dda-component-<region>-<account>). Anyone who
knows a target account id can pre-create that exact name in their own
account; the publish scripts used to probe the name with ``aws s3 ls`` and
upload with ``aws s3 cp``, so a reachable bucket of that name under another
account was silently adopted and the component recipe then pointed every
edge device at attacker-controlled artifacts.

The fix is ``scripts/s3-bucket-owner-guard.sh``: HeadBucket/PutObject with
``--expected-bucket-owner`` (the caller's own account) on every call, create
only when S3 reports the name absent and re-verify right after, and a HARD
failure (exit 2, nothing uploaded) whenever S3 does not confirm that the
caller owns the bucket.

These tests drive the real guard and the real ``build-inference-uploader.sh``
with a fake ``aws`` CLI on PATH that plays the pre-existing attacker-owned
bucket (HeadBucket 403, CreateBucket BucketAlreadyExists) and records every
invocation, and assert the scripts fail closed: no object is written, no
component version is created, and the operator sees the ownership conflict.
A final static gate keeps the unguarded ``aws s3 ls/mb/cp`` pattern from
returning to any publisher of the derived bucket.

Run on the host with ``--noconftest`` (the build_script lane's convention):
    python3 -m pytest test/backend-test/s3_bucket_owner_guard -q --noconftest
"""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap

import pytest

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir)
)
GUARD = os.path.join(REPO_ROOT, "scripts", "s3-bucket-owner-guard.sh")
UPLOADER_SCRIPT = os.path.join(REPO_ROOT, "build-inference-uploader.sh")

ACCOUNT = "111111111111"
REGION = "us-east-1"
BUCKET = f"dda-component-{REGION}-{ACCOUNT}"

# The publish scripts that derive the component bucket name.
PUBLISHERS = [
    "build-inference-uploader.sh",
    "com.dda.InferenceUploader/build-and-publish.sh",
    "gdk-component-build-and-publish.sh",
    "portal-build.sh",
    "publish-ecr-only.sh",
]

# ---------------------------------------------------------------------------
# Fake `aws` CLI
# ---------------------------------------------------------------------------
# Scenario (FAKE_AWS_SCENARIO) -> how `s3api head-bucket` / `s3api create-bucket`
# answer. Every invocation is appended, one line, to FAKE_AWS_LOG.
#
#   owned             head-bucket 200 (the account's own bucket)
#   squatted          head-bucket 403 (the name exists under another account)
#   absent_then_owned head-bucket 404, create-bucket ok, then head-bucket 200
#   absent_then_taken head-bucket 404, create-bucket BucketAlreadyExists
#   absent_unverified head-bucket 404, create-bucket ok, then head-bucket 403
#   denied            head-bucket: a non-404, non-403 error (throttling)
FAKE_AWS = textwrap.dedent(r"""
    #!/bin/bash
    printf '%s\n' "$*" >> "$FAKE_AWS_LOG"
    scenario="${FAKE_AWS_SCENARIO:-owned}"
    state="${FAKE_AWS_LOG}.state"
    has_flag() { local f="$1"; shift; for a in "$@"; do [ "$a" = "$f" ] && return 0; done; return 1; }
    case "$1 $2" in
      "sts get-caller-identity")
        if has_flag --query "$@"; then echo "111111111111"; else echo '{"Account": "111111111111"}'; fi; exit 0 ;;
      "configure get")
        echo "us-east-1"; exit 0 ;;
      "configure export-credentials")
        exit 1 ;;
      "s3api head-bucket")
        has_flag --expected-bucket-owner "$@" || { echo "FAKE: head-bucket without --expected-bucket-owner" >&2; exit 99; }
        created=0; [ -f "$state" ] && grep -q created "$state" && created=1
        case "$scenario" in
          owned) exit 0 ;;
          squatted) echo "An error occurred (403) when calling the HeadBucket operation: Forbidden" >&2; exit 254 ;;
          absent_then_owned) if [ $created = 1 ]; then exit 0; fi
             echo "An error occurred (404) when calling the HeadBucket operation: Not Found" >&2; exit 254 ;;
          absent_then_taken) echo "An error occurred (404) when calling the HeadBucket operation: Not Found" >&2; exit 254 ;;
          absent_unverified) if [ $created = 1 ]; then echo "An error occurred (403) when calling the HeadBucket operation: Forbidden" >&2; exit 254; fi
             echo "An error occurred (404) when calling the HeadBucket operation: Not Found" >&2; exit 254 ;;
          denied) echo "An error occurred (SlowDown) when calling the HeadBucket operation: Please reduce your request rate." >&2; exit 254 ;;
        esac ;;
      "s3api create-bucket")
        case "$scenario" in
          absent_then_taken) echo "An error occurred (BucketAlreadyExists) when calling the CreateBucket operation: The requested bucket name is not available." >&2; exit 254 ;;
          *) echo created >> "$state"; echo '{"Location": "/bucket"}'; exit 0 ;;
        esac ;;
      "s3api put-object")
        has_flag --expected-bucket-owner "$@" || { echo "FAKE: put-object without --expected-bucket-owner" >&2; exit 99; }
        if [ "${FAKE_AWS_PUT_FORBIDDEN:-0}" = "1" ]; then
          echo "An error occurred (AccessDenied) when calling the PutObject operation: Access Denied" >&2; exit 254
        fi
        echo '{"ETag": "\"abc\""}'; exit 0 ;;
      "s3 "*)
        # The legacy high-level commands (ls/mb/cp/sync) carry no owner assertion.
        echo "FAKE: unguarded high-level s3 command: $*" >&2; exit 98 ;;
      "greengrassv2 list-component-versions")
        echo "None"; exit 0 ;;
      "greengrassv2 create-component-version")
        echo '{"arn": "arn:aws:greengrass:us-east-1:111111111111:components:aws.edgeml.dda.InferenceUploader:versions:1.0.0"}'; exit 0 ;;
      "greengrassv2 list-components")
        echo "arn:aws:greengrass:us-east-1:111111111111:components:aws.edgeml.dda.InferenceUploader"; exit 0 ;;
      *)
        exit 0 ;;
    esac
""").lstrip()


@pytest.fixture
def fake_aws(tmp_path):
    """A PATH directory holding the fake `aws`, plus the invocation log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws = bin_dir / "aws"
    aws.write_text(FAKE_AWS)
    aws.chmod(aws.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "aws.log"
    log.write_text("")
    return bin_dir, log


def _env(fake_aws, scenario, **extra):
    bin_dir, log = fake_aws
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_AWS_SCENARIO"] = scenario
    env["FAKE_AWS_LOG"] = str(log)
    env.update(extra)
    return env


def _calls(fake_aws):
    _bin_dir, log = fake_aws
    return [line for line in log.read_text().splitlines() if line]


def _reset(fake_aws):
    """Fresh invocation log and scenario state for the next run."""
    _bin_dir, log = fake_aws
    log.write_text("")
    state = log.with_name(log.name + ".state")
    if state.exists():
        state.unlink()


def _run_guard(fake_aws, scenario, body, cwd=None, **extra):
    """Source the guard in a fresh bash and run `body`."""
    _reset(fake_aws)
    script = f'set -o pipefail\n. "{GUARD}"\n{body}\n'
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd or REPO_ROOT, env=_env(fake_aws, scenario, **extra),
        capture_output=True, text=True, timeout=60,
    )


# ---------------------------------------------------------------------------
# The guard library
# ---------------------------------------------------------------------------

def test_bucket_name_derivation_matches_gdk_and_the_portal():
    out = subprocess.run(
        ["bash", "-c", f'. "{GUARD}"; dda_component_bucket_name "{REGION}" "{ACCOUNT}"'],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0
    assert out.stdout.strip() == BUCKET
    # Missing inputs never derive a name (and never a partial one).
    bad = subprocess.run(
        ["bash", "-c", f'. "{GUARD}"; dda_component_bucket_name "{REGION}" ""'],
        capture_output=True, text=True, timeout=30,
    )
    assert bad.returncode != 0 and bad.stdout.strip() == ""


def test_owned_bucket_is_verified_without_a_create(fake_aws):
    result = _run_guard(fake_aws, "owned",
                        f'dda_ensure_owned_bucket "{BUCKET}" "{REGION}" "{ACCOUNT}"')
    assert result.returncode == 0, result.stderr
    calls = _calls(fake_aws)
    assert len(calls) == 1
    assert calls[0].startswith("s3api head-bucket")
    assert f"--bucket {BUCKET}" in calls[0]
    assert f"--expected-bucket-owner {ACCOUNT}" in calls[0]
    assert "owned by account" in result.stdout


def test_squatted_bucket_is_a_hard_error_and_never_created_or_written(fake_aws):
    """The reported finding: a bucket of the derived name pre-created by
    another account. HeadBucket answers 403 -> exit 2 before any create or
    upload, with the conflict spelled out for the operator."""
    result = _run_guard(fake_aws, "squatted",
                        f'dda_ensure_owned_bucket "{BUCKET}" "{REGION}" "{ACCOUNT}"')
    assert result.returncode == 2
    assert "SECURITY ERROR" in result.stderr
    assert "ownership conflict" in result.stderr
    assert "bucket squatting" in result.stderr
    assert BUCKET in result.stderr and ACCOUNT in result.stderr
    calls = _calls(fake_aws)
    assert [c.split()[1] for c in calls] == ["head-bucket"]


def test_absent_bucket_is_created_then_ownership_is_re_verified(fake_aws):
    result = _run_guard(fake_aws, "absent_then_owned",
                        f'dda_ensure_owned_bucket "{BUCKET}" "{REGION}" "{ACCOUNT}"')
    assert result.returncode == 0, result.stderr
    ops = [c.split()[1] for c in _calls(fake_aws)]
    assert ops == ["head-bucket", "create-bucket", "head-bucket"]
    create = _calls(fake_aws)[1]
    # us-east-1 rejects a LocationConstraint.
    assert "--create-bucket-configuration" not in create
    assert "Verified" in result.stdout


def test_create_in_another_region_passes_the_location_constraint(fake_aws):
    bucket = f"dda-component-eu-west-1-{ACCOUNT}"
    result = _run_guard(fake_aws, "absent_then_owned",
                        f'dda_ensure_owned_bucket "{bucket}" "eu-west-1" "{ACCOUNT}"')
    assert result.returncode == 0, result.stderr
    create = _calls(fake_aws)[1]
    assert "--create-bucket-configuration LocationConstraint=eu-west-1" in create


def test_name_taken_between_check_and_create_is_a_hard_error(fake_aws):
    result = _run_guard(fake_aws, "absent_then_taken",
                        f'dda_ensure_owned_bucket "{BUCKET}" "{REGION}" "{ACCOUNT}"')
    assert result.returncode == 2
    assert "ownership conflict" in result.stderr
    assert "BucketAlreadyExists" in result.stderr
    ops = [c.split()[1] for c in _calls(fake_aws)]
    assert ops == ["head-bucket", "create-bucket"]


def test_post_create_verification_failure_is_a_hard_error(fake_aws):
    """Even after a successful CreateBucket, HeadBucket with the expected
    owner must confirm ownership before anything is written."""
    result = _run_guard(fake_aws, "absent_unverified",
                        f'dda_ensure_owned_bucket "{BUCKET}" "{REGION}" "{ACCOUNT}"')
    assert result.returncode == 2
    assert "ownership conflict" in result.stderr
    ops = [c.split()[1] for c in _calls(fake_aws)]
    assert ops == ["head-bucket", "create-bucket", "head-bucket"]


def test_an_error_that_says_nothing_about_ownership_is_not_treated_as_absent(fake_aws):
    result = _run_guard(fake_aws, "denied",
                        f'dda_ensure_owned_bucket "{BUCKET}" "{REGION}" "{ACCOUNT}"')
    assert result.returncode == 2
    assert "SlowDown" in result.stderr
    assert [c.split()[1] for c in _calls(fake_aws)] == ["head-bucket"]


def test_missing_account_never_verifies(fake_aws):
    for account in ("", "None"):
        result = _run_guard(fake_aws, "owned",
                            f'dda_ensure_owned_bucket "{BUCKET}" "{REGION}" "{account}"')
        assert result.returncode == 2
        assert _calls(fake_aws) == []


def test_put_object_asserts_the_owner_and_fails_closed_on_403(fake_aws, tmp_path):
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"zip")
    result = _run_guard(
        fake_aws, "owned",
        f'dda_s3_put_object "{BUCKET}" "comp/1.0.0/artifact.zip" "{artifact}" "{REGION}" "{ACCOUNT}"')
    assert result.returncode == 0, result.stderr
    put = _calls(fake_aws)[-1]
    assert put.startswith("s3api put-object")
    assert f"--bucket {BUCKET}" in put and "--key comp/1.0.0/artifact.zip" in put
    assert f"--expected-bucket-owner {ACCOUNT}" in put

    result = _run_guard(
        fake_aws, "owned",
        f'dda_s3_put_object "{BUCKET}" "comp/1.0.0/artifact.zip" "{artifact}" "{REGION}" "{ACCOUNT}"',
        FAKE_AWS_PUT_FORBIDDEN="1")
    assert result.returncode == 2
    assert "ownership conflict" in result.stderr


def test_gdk_config_gets_the_upload_owner_assertion(fake_aws, tmp_path):
    """`gdk component publish` uploads with its own client; the guard hands it
    ExpectedBucketOwner through publish.options.file_upload_args, for every
    component in the config, leaving the rest of the config intact."""
    config = tmp_path / "gdk-config.json"
    original = {
        "component": {
            "aws.edgeml.dda.LocalServer.arm64JP6": {
                "author": "Amazon", "version": "NEXT_PATCH",
                "build": {"build_system": "custom", "custom_build_command": ["bash", "build-custom.sh"]},
                "publish": {"bucket": "dda-component", "region": REGION},
            }
        },
        "gdk_version": "1.0.0",
    }
    config.write_text(json.dumps(original))
    result = _run_guard(fake_aws, "owned", f'dda_gdk_config_assert_owner "{config}" "{ACCOUNT}"')
    assert result.returncode == 0, result.stderr
    updated = json.loads(config.read_text())
    publish = updated["component"]["aws.edgeml.dda.LocalServer.arm64JP6"]["publish"]
    assert publish["bucket"] == "dda-component" and publish["region"] == REGION
    assert publish["options"]["file_upload_args"] == {"ExpectedBucketOwner": ACCOUNT}
    assert updated["gdk_version"] == "1.0.0"
    assert updated["component"]["aws.edgeml.dda.LocalServer.arm64JP6"]["build"] == \
        original["component"]["aws.edgeml.dda.LocalServer.arm64JP6"]["build"]

    # Never without an account.
    result = _run_guard(fake_aws, "owned", f'dda_gdk_config_assert_owner "{config}" ""')
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# The InferenceUploader publish script end to end (the reported code path)
# ---------------------------------------------------------------------------

@pytest.fixture
def uploader_checkout(tmp_path):
    """A minimal copy of the repo files build-inference-uploader.sh touches,
    so the real script runs without writing into the checkout."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(GUARD, root / "scripts" / "s3-bucket-owner-guard.sh")
    shutil.copy(UPLOADER_SCRIPT, root / "build-inference-uploader.sh")
    artifacts = root / "inference-uploader" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "inference_uploader.py").write_text("print('uploader')\n")
    (artifacts / "requirements.txt").write_text("boto3\n")
    shutil.copy(os.path.join(REPO_ROOT, "inference-uploader", "recipe.yaml"),
                root / "inference-uploader" / "recipe.yaml")
    return root


def _run_uploader(fake_aws, scenario, cwd):
    _reset(fake_aws)
    return subprocess.run(
        ["bash", "./build-inference-uploader.sh"], cwd=cwd, env=_env(fake_aws, scenario),
        capture_output=True, text=True, timeout=120,
    )


def test_uploader_publish_fails_closed_on_a_squatted_bucket(fake_aws, uploader_checkout):
    """The exact scenario of the finding: the derived bucket name exists under
    another account. The publish must stop before any upload or component
    registration, and must never fall back to `aws s3 ls`/`mb`/`cp`."""
    result = _run_uploader(fake_aws, "squatted", uploader_checkout)
    assert result.returncode != 0
    assert "SECURITY ERROR" in result.stderr and "ownership conflict" in result.stderr
    calls = _calls(fake_aws)
    ops = [" ".join(c.split()[:2]) for c in calls]
    assert "s3api put-object" not in ops
    assert "greengrassv2 create-component-version" not in ops
    assert not any(op.startswith("s3 ") for op in ops), ops
    assert ops[-1] == "s3api head-bucket"
    assert not (uploader_checkout / "recipe_processed.yaml").exists()


def test_uploader_publish_fails_closed_when_the_name_is_taken_during_create(fake_aws, uploader_checkout):
    result = _run_uploader(fake_aws, "absent_then_taken", uploader_checkout)
    assert result.returncode != 0
    ops = [" ".join(c.split()[:2]) for c in _calls(fake_aws)]
    assert "s3api put-object" not in ops
    assert "greengrassv2 create-component-version" not in ops


def test_uploader_publish_asserts_the_owner_on_every_upload_when_the_bucket_is_owned(
        fake_aws, uploader_checkout):
    result = _run_uploader(fake_aws, "owned", uploader_checkout)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(fake_aws)
    puts = [c for c in calls if c.startswith("s3api put-object")]
    assert len(puts) == 2
    for put in puts:
        assert f"--bucket {BUCKET}" in put
        assert f"--expected-bucket-owner {ACCOUNT}" in put
    assert not any(c.startswith("s3 ") for c in calls)
    assert any(c.startswith("greengrassv2 create-component-version") for c in calls)
    # The recipe the component version was created from points at the
    # verified bucket.
    assert f"Using S3 bucket: {BUCKET}" in result.stdout


# ---------------------------------------------------------------------------
# deploy-portal.sh's component bucket prerequisite
# ---------------------------------------------------------------------------

DEPLOY_PORTAL = os.path.join(REPO_ROOT, "edge-cv-portal", "deploy-portal.sh")


def _deploy_portal_bucket_functions():
    """The COMPONENT_BUCKET_PREFIX assignment and the two bucket functions of
    deploy-portal.sh, extracted verbatim so they run without the deploy."""
    text = open(DEPLOY_PORTAL, encoding="utf-8").read()
    parts = [re.search(r'^COMPONENT_BUCKET_PREFIX="dda-component"$', text, re.M).group(0)]
    for name in ("_component_bucket_name", "ensure_component_bucket"):
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", text, re.M | re.S)
        assert match, f"{name}() not found in deploy-portal.sh"
        parts.append(match.group(0))
    return "\n".join(parts)


def _run_deploy_portal_ensure(fake_aws, scenario):
    _reset(fake_aws)
    script = _deploy_portal_bucket_functions() + "\nensure_component_bucket\n"
    return subprocess.run(
        ["bash", "-c", script], cwd=REPO_ROOT,
        env=_env(fake_aws, scenario, REGION=REGION, ACCOUNT=ACCOUNT),
        capture_output=True, text=True, timeout=60,
    )


def test_deploy_portal_refuses_a_squatted_component_bucket(fake_aws):
    result = _run_deploy_portal_ensure(fake_aws, "squatted")
    assert result.returncode == 2
    assert "cannot determine whether" in result.stderr
    assert [c.split()[1] for c in _calls(fake_aws)] == ["head-bucket"]


def test_deploy_portal_verifies_ownership_right_after_creating_the_bucket(fake_aws):
    """Ticket step 3: an explicit HeadBucket with ExpectedBucketOwner right
    after CreateBucket, before any write, and a hard stop when it fails."""
    result = _run_deploy_portal_ensure(fake_aws, "absent_then_owned")
    assert result.returncode == 0, result.stderr
    ops = [c.split()[1] for c in _calls(fake_aws)]
    assert ops == ["head-bucket", "create-bucket", "head-bucket"]
    assert f"--expected-bucket-owner {ACCOUNT}" in _calls(fake_aws)[2]
    assert "Verified" in result.stdout

    result = _run_deploy_portal_ensure(fake_aws, "absent_unverified")
    assert result.returncode == 2
    assert "bucket-squatting" in result.stderr
    assert "nothing" in result.stderr and "was written" in result.stderr


def test_deploy_portal_treats_a_taken_name_as_a_security_conflict(fake_aws):
    result = _run_deploy_portal_ensure(fake_aws, "absent_then_taken")
    assert result.returncode == 2
    assert "bucket-squatting" in result.stderr
    assert "never adopt" in result.stderr


# ---------------------------------------------------------------------------
# Static gate: no unguarded access to the derived bucket in any publisher
# ---------------------------------------------------------------------------

_HIGH_LEVEL_S3 = re.compile(r"\baws\s+s3\s+(ls|mb|cp|sync|rm|mv)\b")
_DERIVED_REFS = ("dda-component", "S3_BUCKET", "BUCKET_NAME", "S3_URI", "GDK_BUCKET")


def test_no_publisher_reaches_the_derived_bucket_with_an_unguarded_command():
    """Every publisher sources the guard, derives the name through it, and
    never touches the derived bucket with a high-level `aws s3` command (which
    cannot assert the owner)."""
    for rel in PUBLISHERS:
        text = open(os.path.join(REPO_ROOT, rel), encoding="utf-8").read()
        assert "s3-bucket-owner-guard.sh" in text, f"{rel} does not source the guard"
        assert "dda_ensure_owned_bucket" in text, f"{rel} never verifies bucket ownership"
        assert not re.search(r'="dda-component-\$\{', text), \
            f"{rel} derives the bucket name outside the guard"
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or not _HIGH_LEVEL_S3.search(line):
                continue
            assert not any(ref in line for ref in _DERIVED_REFS), \
                f"{rel}:{lineno} reaches the derived bucket without an owner assertion: {stripped}"
    # Every GDK publish is preceded by the owner assertion in gdk-config.json.
    for rel in ("gdk-component-build-and-publish.sh", "portal-build.sh"):
        text = open(os.path.join(REPO_ROOT, rel), encoding="utf-8").read()
        publish_at = text.index("gdk component publish 2>&1")
        assert "dda_gdk_config_assert_owner gdk-config.json" in text[:publish_at], \
            f"{rel}: gdk component publish runs without the upload owner assertion"


def test_guard_asserts_the_owner_on_every_s3_call_it_makes():
    lines = open(GUARD, encoding="utf-8").read().splitlines()
    code = [line for line in lines if not line.strip().startswith("#")]
    for lineno, line in enumerate(lines, start=1):
        if line.strip().startswith("#"):
            continue
        if re.search(r"aws s3api (head-bucket|put-object)", line):
            window = "\n".join(lines[lineno - 1:lineno + 2])
            assert "--expected-bucket-owner" in window, f"{GUARD}:{lineno}: {line.strip()}"
    assert not _HIGH_LEVEL_S3.search("\n".join(code))
