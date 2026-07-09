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
"""Out-of-scope preservation guard (Req 3.7).

Spec: security-injection-deserialization-fixes — Property 2: Preservation.

The generated CDK build artifacts under
``edge-cv-portal/infrastructure/cdk.out/asset.*`` and the AWS credentials
embedded in ``deploy.py``'s SSM commands were handled by SEPARATE remediation
groups and MUST NOT be modified by this spec's fix.

Recorded baselines:
  * ``cdk_out_baseline.json`` — the exact sha256 of every ``cdk.out/asset.*``
    copy of the in-scope source files (the 11 generated ``model_converter.py``
    duplicates). Task 13 re-runs this and asserts the bytes are unchanged.
  * The embedded-credential handling in ``deploy.py`` — the successor spec
    ``security-secrets-credentials-jwt-fixes`` (S2 / Req 2.2, task 5) has now
    landed and has REMOVED the two ``export AWS_*`` list entries and the
    trailing ``-a {access_key} -s {secret_key}`` mqtt fragment. The legitimate
    boto3-client credential kwargs (``credentials.access_key`` /
    ``credentials.secret_key``) and the ``edgeml-sdk-longevity-tests`` secret
    name remain in place. The assertion below is re-pinned accordingly.

**Validates: Requirements 3.7**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py \
        -p no:cacheprovider --noconftest -v
"""
import hashlib
import json
import os

from _preservation_support import REPO_ROOT, read_repo_file

HERE = os.path.dirname(os.path.abspath(__file__))
CDK_BASELINE = os.path.join(HERE, "cdk_out_baseline.json")

DEPLOY_REL = "src/edgemlsdk/src/test/longevity/deploy.py"


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# --------------------------------------------------------------------------- #
# cdk.out/asset.* generated copies are byte-for-byte unchanged
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.7
def test_cdk_out_asset_copies_unchanged():
    """Every recorded ``cdk.out/asset.*`` copy still hashes to its baseline."""
    with open(CDK_BASELINE) as fh:
        baseline = json.load(fh)

    recorded = baseline["sha256"]
    assert recorded, "expected recorded cdk.out/asset.* baseline hashes"

    mismatches = []
    missing = []
    for rel, expected_hash in recorded.items():
        full = os.path.join(REPO_ROOT, rel)
        if not os.path.exists(full):
            missing.append(rel)
            continue
        actual = _sha256(full)
        if actual != expected_hash:
            mismatches.append(f"{rel}: {expected_hash} -> {actual}")

    assert not missing, f"generated cdk.out copies disappeared: {missing}"
    assert not mismatches, (
        "generated cdk.out/asset.* copies were modified (must stay unchanged):\n"
        + "\n".join(mismatches)
    )


# Validates: Requirements 3.7
def test_cdk_out_baseline_covers_all_current_copies():
    """The baseline still enumerates every current in-scope cdk.out copy (a new
    unrecorded copy would mean the artifact set drifted)."""
    with open(CDK_BASELINE) as fh:
        baseline = json.load(fh)
    basenames = baseline["basenames"]

    import glob
    current = set()
    for base in basenames:
        for path in glob.glob(
            os.path.join(REPO_ROOT, "edge-cv-portal/infrastructure/cdk.out/asset.*/**", base),
            recursive=True,
        ):
            current.add(os.path.relpath(path, REPO_ROOT))

    recorded = set(baseline["sha256"].keys())
    assert current == recorded, (
        f"cdk.out copy set drifted.\n only-current: {sorted(current - recorded)}\n"
        f" only-recorded: {sorted(recorded - current)}"
    )


# --------------------------------------------------------------------------- #
# deploy.py embedded-credential handling: removed by the group-2 successor spec
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.7
def test_deploy_embedded_credentials_removed_by_group2_spec():
    """The embedded AWS credentials in ``deploy.py``'s SSM command strings were
    the responsibility of a separate remediation group. That successor spec
    (``security-secrets-credentials-jwt-fixes`` S2 / Req 2.2, task 5) has now
    landed and has REMOVED the two ``export AWS_*`` list entries and the
    trailing ``-a {credentials.access_key} -s {credentials.secret_key}`` mqtt
    fragment. The legitimate boto3-client credential kwargs and the
    ``edgeml-sdk-longevity-tests`` secret name remain in place — those are
    valid uses this spec never touched."""
    src = read_repo_file(DEPLOY_REL)

    # The secret name and the boto3 client credential kwargs are still present
    # (these are legitimate control-plane SDK uses, not a command-string sink).
    assert 'secret_name = "edgeml-sdk-longevity-tests"' in src
    assert "credentials.access_key" in src
    assert "credentials.secret_key" in src

    # The env-var *names* remain (they are used as Secrets Manager keys and set
    # via os.environ inside set_aws_access_keys_from_secrets_manager) — the fix
    # removed only the SSM command-string exports.
    assert "AWS_ACCESS_KEY_ID" in src
    assert "AWS_SECRET_ACCESS_KEY" in src

    # The S2 fix REMOVED the two credential export entries from the constructed
    # SSM command list.
    assert "export AWS_ACCESS_KEY_ID=" not in src
    assert "export AWS_SECRET_ACCESS_KEY=" not in src

    # The S2 fix REMOVED the trailing ``-a {credentials.access_key} -s
    # {credentials.secret_key}`` fragment from the mqtt run command. Do NOT
    # assert bare ``-a``/``-s`` are absent — those tokens still appear in the
    # file (getopts flag string in a comment, argparse flags for other args).
    assert "-a {credentials.access_key} -s {credentials.secret_key}" not in src
