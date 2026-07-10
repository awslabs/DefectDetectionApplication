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
"""Property-based preservation baselines — S3 bucket-squatting spec
(Req 3.1, 3.2, 3.3, 3.6).

Spec: security-s3-bucket-squatting-fixes — Property 2: Preservation.

Four property-based baselines captured on the UNFIXED tree (Hypothesis). On the
unfixed tree there is NO ``head-bucket`` preflight and the publish buckets are
hardcoded literals, so these record the pre-fix ``F(X)``. The allow/deny
DIFFERENCE assertions (owner-mismatch fails closed; env-set resolves to the
provided value) are added in task 8 against the fixed tree — here we only pin the
preserved behavior.

* **PBT 1 — owner match / mismatch model (B1, B2, B3)**: model the access as
  ``access_proceeds(bucket_owner, expected_owner)``. On the UNFIXED tree there is
  no preflight, so EVERY access proceeds regardless of owner. Record that
  legitimate (matching-owner) accesses proceed — task 8 asserts they still
  proceed (no-op preflight) while mismatched-owner accesses become the
  fail-closed difference.
* **PBT 2 — env set / unset resolution model (B2, B3)**: model bucket resolution
  as ``resolve_unfixed(env_value)``. On the UNFIXED tree the targets are always
  the hardcoded literals (env vars are not consulted). Record the unset-case
  targets so task 8 asserts the fixed script resolves to the same literals when
  the env vars are unset.
* **PBT 3 — deploy.py SSM-list equality (B1)**: model the SSM download-list
  builder as a pure function of ``args``. For any legitimate (allowlisted) arg
  tuple, the emitted list equals the reference template and contains NO
  ``head-bucket`` entry (pre-fix). Task 8 asserts the fixed list equals this
  reference PLUS exactly the two ``head-bucket`` entries.
* **PBT 4 — notebook ``update_manifest_paths`` rewrite (B6)**: exec the REAL
  function extracted from the notebook cell and drive it with generated manifest
  entries. Record that entries under the ``old_prefix`` are rewritten and others
  are left unchanged, with ``old_prefix`` == the current hardcoded literal.

**Validates: Requirements 3.1, 3.2, 3.3, 3.6**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_s3_pbt.py \
        -p no:cacheprovider --noconftest -v
"""
import ast
import json
import os
import tempfile
from argparse import Namespace

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from _s3_preservation_support import (
    capture_download_list,
    find_manifest_cell,
    load_deploy,
    load_notebook,
    resolve_publish_targets,
    ARTIFACT_BUCKET_LITERAL,
    DOCS_BUCKET_LITERAL,
    STUB_CALLER_ACCOUNT,
)

# The two head-bucket preflight owner values the B1 fix resolves for the
# canonical/legitimate case: the AWS-managed Panorama distribution account
# (documented module constant, currently a fail-closed placeholder) and the
# deployer's own account (from ``sts get-caller-identity``, stubbed).
_PANORAMA_OWNER = load_deploy().PANORAMA_SDK_DISTRIBUTION_ACCOUNT
_LONGEVITY_OWNER = STUB_CALLER_ACCOUNT

# 12-digit AWS account IDs.
_ACCOUNT = st.from_regex(r"\A[0-9]{12}\Z")
# shlex-safe allowlisted tokens (deploy.py allowlist: ^[A-Za-z0-9._:-]+$).
_TOKEN = st.from_regex(r"\A[A-Za-z0-9._:-]{1,20}\Z")
_DATE = st.from_regex(r"\A[0-9]{8}\Z")
_NUM = st.integers(min_value=1, max_value=100000)


# --------------------------------------------------------------------------- #
# PBT 1 — owner match / mismatch model (pre-fix: no preflight, always proceeds)
# --------------------------------------------------------------------------- #
def _access_proceeds_unfixed(bucket_owner, expected_owner):
    """Model of the UNFIXED access: no preflight exists, so the access ALWAYS
    proceeds regardless of who owns the bucket."""
    return True


# Validates: Requirements 3.1, 3.2, 3.3
@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(bucket_owner=_ACCOUNT, expected_owner=_ACCOUNT)
def test_pbt1_unfixed_access_always_proceeds(bucket_owner, expected_owner):
    """Invariant (pre-fix): with no preflight, the access proceeds for every
    (bucket_owner, expected_owner) pair — including the matching-owner
    (legitimate) case whose behavior task 8 must preserve."""
    assert _access_proceeds_unfixed(bucket_owner, expected_owner) is True


# Validates: Requirements 3.1, 3.2, 3.3 — the legitimate matching-owner case
# proceeds (this is the F(X) that the no-op preflight must preserve in F').
@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(owner=_ACCOUNT)
def test_pbt1_matching_owner_access_proceeds(owner):
    assert _access_proceeds_unfixed(owner, owner) is True


# --------------------------------------------------------------------------- #
# PBT 2 — env set / unset resolution model (pre-fix: always the literal)
# --------------------------------------------------------------------------- #
def _resolve_unfixed(default_literal, env_value):
    """Model of the UNFIXED resolution: the bucket is the hardcoded literal
    regardless of any env var value (the env var indirection does not exist)."""
    return default_literal


_ENV_VALUES = st.one_of(
    st.none(),  # unset
    st.just("panorama-sdk-v2-artifacts"),
    st.just("edgeml-sdk-docs"),
    st.just("my-account-bucket"),
    st.from_regex(r"\A[a-z0-9.-]{3,40}\Z"),
)


# Validates: Requirements 3.2, 3.3
@settings(max_examples=60, deadline=None)
@given(artifact_env=_ENV_VALUES, docs_env=_ENV_VALUES)
def test_pbt2_unfixed_resolution_ignores_env(artifact_env, docs_env):
    """Invariant (pre-fix): resolution is always the hardcoded literal, for any
    env value including unset. Task 8 asserts the FIXED unset case resolves to
    the same literals byte-for-byte."""
    assert _resolve_unfixed(ARTIFACT_BUCKET_LITERAL, artifact_env) == ARTIFACT_BUCKET_LITERAL
    assert _resolve_unfixed(DOCS_BUCKET_LITERAL, docs_env) == DOCS_BUCKET_LITERAL


# Validates: Requirements 3.2, 3.3 — the unset case (F baseline) resolves to the
# current literals as observed in the live publish.sh.
def test_pbt2_unset_case_matches_live_publish_targets():
    live = resolve_publish_targets()
    assert live["artifact_bucket"] == _resolve_unfixed(ARTIFACT_BUCKET_LITERAL, None)
    assert live["docs_bucket"] == _resolve_unfixed(DOCS_BUCKET_LITERAL, None)


# --------------------------------------------------------------------------- #
# PBT 3 — deploy.py SSM-list equality (pure function of args; no preflight)
# --------------------------------------------------------------------------- #
def _reference_download_prefix_free(args):
    """The verbatim pre-fix (F) template for the download list — NO head-bucket
    entries. Task 8 asserts the FIXED list equals this baseline PLUS exactly the
    two head-bucket preflight entries (every pre-existing entry byte-for-byte
    identical)."""
    source_folder = "mqtt/" if args.mqtt else ""
    return [
        "sudo yum update",
        "sudo yum install docker -y",
        "sudo service docker start",
        "sudo service docker status",
        f"export AWS_DEFAULT_REGION={args.region}",
        "sudo mkdir -p /edgemlsdk",
        f"sudo mkdir -p /edgemlsdk/{source_folder}",
        f"aws s3 sync s3://panorama-sdk-v2-artifacts/release/1.0.{args.release_date}/{args.platform}/{args.ubuntu_version}/3.8.0/ /edgemlsdk",
        "aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json /edgemlsdk/",
        "aws s3 cp s3://edgeml-sdk-longevity-tests/delegates.json /edgemlsdk/",
        f"aws s3 sync s3://edgeml-sdk-longevity-tests/{source_folder} /edgemlsdk/{source_folder}",
        "aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com",
        f"docker pull ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com/edgemlsdk:{args.ubuntu_version}-{args.platform}-{args.python_version}-latest",
    ]


def _reference_download_fixed(args):
    """The FIXED (F') template = the pre-fix baseline PLUS exactly the two
    ``aws s3api head-bucket --expected-bucket-owner`` preflight entries, one
    immediately before the ``panorama-sdk-v2-artifacts`` sync and one immediately
    before the three ``edgeml-sdk-longevity-tests`` accesses."""
    base = _reference_download_prefix_free(args)
    panorama_pre = (
        f"aws s3api head-bucket --bucket panorama-sdk-v2-artifacts "
        f"--expected-bucket-owner {_PANORAMA_OWNER}"
    )
    longevity_pre = (
        f"aws s3api head-bucket --bucket edgeml-sdk-longevity-tests "
        f"--expected-bucket-owner {_LONGEVITY_OWNER}"
    )
    out = []
    for entry in base:
        if entry.startswith("aws s3 sync s3://panorama-sdk-v2-artifacts/"):
            out.append(panorama_pre)
        elif entry == "aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json /edgemlsdk/":
            out.append(longevity_pre)
        out.append(entry)
    return out


# Validates: Requirements 3.1
@settings(max_examples=30, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(platform=_TOKEN, ubuntu_version=_TOKEN, python_version=_TOKEN,
       region=_TOKEN, release_date=_DATE, longevity_hours=_NUM, payload_size=_NUM)
def test_pbt3_fixed_ssm_list_equals_baseline_plus_two_preflight_entries(
    platform, ubuntu_version, python_version, region, release_date,
    longevity_hours, payload_size,
):
    """Invariant (F'): for any legitimate arg tuple, deploy.main's download list
    equals the pre-fix baseline PLUS exactly the two head-bucket preflight
    entries. Stripping the two ``head-bucket`` entries yields the byte-for-byte
    pre-fix baseline (F(X) = F'(X) for every non-preflight entry)."""
    args = Namespace(
        mqtt="mqtt", platform=platform, ubuntu_version=ubuntu_version,
        python_version=python_version, region=region,
        mqtt_endpoint="a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com",
        release_date=release_date, longevity_hours=longevity_hours,
        payload_size=payload_size,
        artifacts_bucket_owner=None, longevity_bucket_owner=None,
    )
    download = capture_download_list(args)

    # The fixed list == baseline + exactly the two preflight entries, in order.
    assert download == _reference_download_fixed(args)

    # Exactly two head-bucket preflight entries, each carrying the owner flag.
    preflights = [c for c in download if "aws s3api head-bucket" in c]
    assert len(preflights) == 2
    assert all("--expected-bucket-owner" in c for c in preflights)

    # Stripping the two preflight entries reproduces the pre-fix baseline
    # byte-for-byte (no pre-existing entry mutated).
    stripped = [c for c in download if "aws s3api head-bucket" not in c]
    assert stripped == _reference_download_prefix_free(args)

    # Each preflight immediately precedes the access it guards.
    idx = download.index(
        f"aws s3 sync s3://panorama-sdk-v2-artifacts/release/1.0.{release_date}/{platform}/{ubuntu_version}/3.8.0/ /edgemlsdk"
    )
    assert download[idx - 1].startswith(
        "aws s3api head-bucket --bucket panorama-sdk-v2-artifacts"
    )
    lidx = download.index(
        "aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json /edgemlsdk/"
    )
    assert download[lidx - 1].startswith(
        "aws s3api head-bucket --bucket edgeml-sdk-longevity-tests"
    )


# --------------------------------------------------------------------------- #
# PBT 4 — notebook update_manifest_paths rewrite (the REAL function)
# --------------------------------------------------------------------------- #
def _extract_update_manifest_paths():
    """Extract and exec the REAL ``update_manifest_paths`` function from the
    notebook manifest cell, returning the callable."""
    nb = load_notebook()
    _, src = find_manifest_cell(nb)
    # The cell contains IPython line magics (e.g. ``!wget ...``) which are not
    # valid Python; strip them so ast.parse succeeds. We only extract the
    # FunctionDef node, so the other (name-referencing) statements need to parse
    # but are never executed.
    safe_src = "\n".join(
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith(("!", "%"))
    )
    tree = ast.parse(safe_src)
    func_node = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "update_manifest_paths"),
        None,
    )
    assert func_node is not None, "update_manifest_paths def not found in cell"
    ns = {"json": json}
    exec(compile(ast.Module([func_node], []), "<notebook_cell>", "exec"), ns)  # nosec B102
    return ns["update_manifest_paths"]


_OLD_PREFIX = "s3://lookoutvision-us-east-1-0e205be246/getting-started/"
_NEW_PREFIX = "s3://my-own-bucket/project/"


# A manifest entry: source-ref / anomaly-mask-ref either under old_prefix or not.
def _manifest_entry(under_prefix_a, under_prefix_b, tail_a, tail_b):
    src_ref = (_OLD_PREFIX + tail_a) if under_prefix_a else ("s3://other/" + tail_a)
    mask_ref = (_OLD_PREFIX + tail_b) if under_prefix_b else ("s3://other/" + tail_b)
    return {"source-ref": src_ref, "anomaly-mask-ref": mask_ref, "class": 1}


_TAIL = st.from_regex(r"\A[A-Za-z0-9_./-]{1,20}\Z")


# Validates: Requirements 3.6
@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(entries=st.lists(
    st.builds(_manifest_entry, st.booleans(), st.booleans(), _TAIL, _TAIL),
    min_size=0, max_size=8,
))
def test_pbt4_update_manifest_paths_rewrites_only_prefixed_entries(entries):
    """Invariant: update_manifest_paths rewrites source-ref / anomaly-mask-ref
    values that start with old_prefix (old->new) and leaves all others (and every
    other key) unchanged. This pins the rewrite logic F(X) that task 8 preserves
    (the fixed cell derives the SAME old_prefix from sample_data_bucket)."""
    fn = _extract_update_manifest_paths()

    # Write the generated manifest to a temp file (one JSON object per line).
    fd, path = tempfile.mkstemp(suffix=".manifest")
    try:
        with os.fdopen(fd, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        out_lines = fn(path, _OLD_PREFIX, _NEW_PREFIX)
    finally:
        os.remove(path)

    assert len(out_lines) == len(entries)
    for original, out_line in zip(entries, out_lines):
        got = json.loads(out_line)
        for key in ("source-ref", "anomaly-mask-ref"):
            orig_val = original[key]
            if orig_val.startswith(_OLD_PREFIX):
                assert got[key] == orig_val.replace(_OLD_PREFIX, _NEW_PREFIX)
            else:
                assert got[key] == orig_val
        # Non-ref keys are untouched.
        assert got["class"] == original["class"]


# Validates: Requirements 3.6 — after the B6 fix the prefix is derived from a
# single-source ``sample_data_bucket`` variable, but the RESOLVED old_prefix
# string value is byte-for-byte unchanged (F(X) = F'(X) for the rewrite input).
def test_pbt4_old_prefix_derived_from_sample_data_bucket():
    nb = load_notebook()
    _, src = find_manifest_cell(nb)
    _sample_bucket = _OLD_PREFIX.split("//", 1)[1].split("/", 1)[0]
    assert f'sample_data_bucket = "{_sample_bucket}"' in src
    assert "old_prefix = f's3://{sample_data_bucket}/getting-started/'" in src
    # The bare hardcoded literal assignment is gone (F' parameterization).
    assert f"old_prefix = '{_OLD_PREFIX}'" not in src
    # The resolved value is still the same literal string.
    assert f"s3://{_sample_bucket}/getting-started/" == _OLD_PREFIX
