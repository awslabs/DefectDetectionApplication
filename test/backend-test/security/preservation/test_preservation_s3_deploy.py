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
"""B1 deploy.py SSM-list preservation baseline (Req 3.1).

Spec: security-s3-bucket-squatting-fixes — Property 2: Preservation.

``deploy.py`` builds the ``download_edgemlsdk_release_artifacts`` list of
``AWS-RunShellScript`` SSM commands, including four ``aws s3 cp`` / ``aws s3
sync`` accesses against the predictable ``panorama-sdk-v2-artifacts`` /
``edgeml-sdk-longevity-tests`` buckets, then ``dpkg -i`` / ``pip install`` on the
deployed instance. The B1 fix (task 5) will PREPEND two ``aws s3api head-bucket
--expected-bucket-owner`` preflight entries but MUST leave every existing entry —
the four ``aws s3 cp`` / ``aws s3 sync`` strings, the ``export
AWS_DEFAULT_REGION``, the ``mkdir``s, and the ``ecr`` / ``docker`` entries —
byte-for-byte identical, and MUST preserve the ``# nosec B105`` secret-name line
and the ``shlex.quote``'d argument interpolation (secrets-spec baseline).

Methodology (observation-first): this test captures ``F(X)`` — the exact ordered
command list handed to SSM — on the UNFIXED tree by running the REAL
``deploy.main`` with boto3 stubbed (mirroring the sibling
``test_preservation_deploy_ssm.py`` ``_capture_ssm_commands``). Task 8 re-runs
this file against the fixed tree and asserts the recorded strings are still
present in-order (only the two preflight entries added).

**Validates: Requirements 3.1**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_s3_deploy.py \
        -p no:cacheprovider --noconftest -v
"""
import json
import os

from _s3_preservation_support import (
    baseline_path,
    canonical_deploy_args,
    capture_download_list,
    read_repo_file,
    DEPLOY_REL,
)

BASELINE = baseline_path("s3_baseline_deploy_ssm_list.json")


def _load_baseline():
    with open(BASELINE, encoding="utf-8") as fh:
        return json.load(fh)


# The four S3 access strings and the surrounding entries that MUST survive the
# fix byte-for-byte (only preflight entries are added around them).
_EXPECTED_S3_LINES = [
    "aws s3 sync s3://panorama-sdk-v2-artifacts/release/1.0.20230918/aarch64/22.04/3.8.0/ /edgemlsdk",
    "aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json /edgemlsdk/",
    "aws s3 cp s3://edgeml-sdk-longevity-tests/delegates.json /edgemlsdk/",
    "aws s3 sync s3://edgeml-sdk-longevity-tests/mqtt/ /edgemlsdk/mqtt/",
]


# Validates: Requirements 3.1
def test_baseline_file_exists_and_recorded():
    """The B1 golden was captured on the unfixed tree with the four S3 accesses,
    the region export, the mkdirs, and the ecr/docker entries."""
    b = _load_baseline()
    dl = b["download_list"]
    assert dl, "expected a recorded download list"
    for s3_line in _EXPECTED_S3_LINES:
        assert s3_line in dl, f"golden missing S3 line: {s3_line}"
    assert "export AWS_DEFAULT_REGION=us-west-2" in dl
    assert "sudo mkdir -p /edgemlsdk" in dl
    assert "sudo mkdir -p /edgemlsdk/mqtt/" in dl
    assert any(c.startswith("aws ecr get-login-password") for c in dl)
    assert any(c.startswith("docker pull ") for c in dl)


# Validates: Requirements 3.1
def test_live_download_list_matches_golden_exactly():
    """Running the REAL deploy.main for the canonical args reproduces the exact
    recorded download list (byte-for-byte, in order)."""
    b = _load_baseline()
    live = capture_download_list(canonical_deploy_args())
    assert live == b["download_list"], (
        "deploy.py download SSM list drifted from the recorded baseline"
    )


# Validates: Requirements 3.1
def test_nosec_and_shlex_quote_lines_preserved():
    """The `# nosec B105` secret-name lines and the shlex.quote'd interpolation
    lines (secrets-spec baseline) are present byte-for-byte in deploy.py."""
    b = _load_baseline()
    src = read_repo_file(DEPLOY_REL)

    assert b["nosec_b105_lines"], "expected recorded # nosec B105 lines"
    for line in b["nosec_b105_lines"]:
        assert line in src, f"missing # nosec B105 line: {line!r}"

    assert b["shlex_quote_lines"], "expected recorded shlex.quote lines"
    for line in b["shlex_quote_lines"]:
        assert line in src, f"missing shlex.quote line: {line!r}"


# Validates: Requirements 3.1
def test_s3_access_strings_present_in_source():
    """The four predictable-bucket S3 access strings are present verbatim in the
    deploy.py source (the fix only adds preflight entries, never edits these)."""
    src = read_repo_file(DEPLOY_REL)
    # Source uses shlex-quoted f-string interpolation; assert the stable literal
    # fragments that must remain byte-for-byte.
    assert "aws s3 sync s3://panorama-sdk-v2-artifacts/release/1.0." in src
    assert "aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json /edgemlsdk/" in src
    assert "aws s3 cp s3://edgeml-sdk-longevity-tests/delegates.json /edgemlsdk/" in src
    assert "aws s3 sync s3://edgeml-sdk-longevity-tests/" in src
