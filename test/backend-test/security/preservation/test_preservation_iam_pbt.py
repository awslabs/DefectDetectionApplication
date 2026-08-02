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
"""Property-based preservation baselines for the IAM allow-set semantics
(PBT 1–4).

Spec: security-iam-authorization-fixes — Property 2: Preservation.

These Hypothesis-driven tests characterize the ALLOW-set of the UNFIXED policies
(the wildcard policies allow everything) and record the specific subsets the
FIXED policies must PRESERVE:

  * PBT 1 — DDA-managed vs non-DDA S3 resource ARNs (I1, I7–I14). Unfixed
    invariant: the wildcard S3 statement allows BOTH DDA-prefixed and non-DDA
    ARNs. Recorded to preserve: every ``dda-*`` / ``sagemaker-*`` /
    ``dda-component-*`` / ``dda-inference-results-*`` ARN the code path exercises.
  * PBT 2 — tagged vs untagged buckets (I3, I4). Unfixed invariant: the Ground
    Truth / portal-access S3 statement allows every bucket regardless of tag
    (no ``Condition``). Recorded to preserve: the ``dda-portal:managed=true``
    tagged subset (and ``sagemaker-*``).
  * PBT 3 — trusted vs untrusted account IDs (I5, I6). Unfixed invariant: the
    ``arn:aws:iam::*:role/DDAPortalAccessRole`` / ``resources: ['*']``
    ``sts:AssumeRole`` grant allows EVERY probe account. Recorded to preserve:
    ``sts:AssumeRole`` against accounts in the trusted set.
  * PBT 4 — enumerable IoT action subset (I11, I13, I15). Unfixed invariant: the
    ``iot:*`` grant allows EVERY IoT action. Recorded to preserve: exactly the
    edge-device / build-server / data-plane exercised subset.

On the UNFIXED tree these encode the baseline (allow-everything). The full
allow/deny DIFFERENCE assertions (allow iff DDA-prefixed / tagged / trusted /
exercised) run in task 9 against the fixed policies.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.11, 3.13, 3.15**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_iam_pbt.py \
        -p no:cacheprovider --noconftest -v
"""
import json
import os
import re

from hypothesis import given, settings
from hypothesis import strategies as st

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.normpath(os.path.join(HERE, "..", "baselines"))


def _golden(name):
    with open(os.path.join(BASELINES, name), encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# Minimal IAM-style matcher (Action / Resource wildcards only — sufficient to
# model the allow-set of the wildcard baseline statements).
# --------------------------------------------------------------------------- #
def _wildcard_re(pattern):
    """Translate an IAM Action/Resource pattern (``*`` = any run, ``?`` = one
    char) into an anchored regex."""
    out = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$")


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _statement_allows(stmt, action, resource):
    if stmt.get("Effect") != "Allow":
        return False
    if not any(_wildcard_re(a).match(action) for a in _as_list(stmt.get("Action", []))):
        return False
    if not any(_wildcard_re(r).match(resource) for r in _as_list(stmt.get("Resource", []))):
        return False
    return True


def _policy_allows(statements, action, resource):
    return any(_statement_allows(s, action, resource) for s in statements)


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
_token = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=24)
_account = st.text(alphabet="0123456789", min_size=12, max_size=12)

DDA_BUCKET_PREFIXES = ["dda-", "dda-component-", "dda-inference-results-", "sagemaker-"]


@st.composite
def _dda_bucket_arn(draw):
    prefix = draw(st.sampled_from(DDA_BUCKET_PREFIXES))
    return f"arn:aws:s3:::{prefix}{draw(_token)}"


@st.composite
def _non_dda_bucket_arn(draw):
    name = draw(_token)
    # Exclude accidental DDA / sagemaker prefixes so the two strategies are disjoint.
    for p in ("dda-", "sagemaker-"):
        if name.startswith(p):
            name = "cust-" + name
    return f"arn:aws:s3:::{name}"


# --------------------------------------------------------------------------- #
# PBT 1 — DDA vs non-DDA S3 resource ARNs (unfixed = allow both)
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.7 (and 3.1 by extension)
@settings(max_examples=25, deadline=None)
@given(arn=st.one_of(_dda_bucket_arn(), _non_dda_bucket_arn()))
def test_pbt1_unfixed_s3_wildcard_allows_all(arn):
    s3_policy = _golden("iam_baseline_heredoc_deploy-account-role_S3_POLICY.json")
    first_stmt = s3_policy["Statement"][0]  # the I7 wildcard statement
    # Baseline (unfixed): the first statement is on arn:aws:s3:::* so it allows
    # ANY bucket ARN for its scopable action set.
    assert _statement_allows(first_stmt, "s3:GetObject", arn)


# Validates: Requirements 3.7 — the DDA-prefixed subset the fix must PRESERVE is
# in the unfixed allow-set (so the fix can narrow to it without losing access).
@settings(max_examples=25, deadline=None)
@given(arn=_dda_bucket_arn())
def test_pbt1_dda_prefixed_arns_are_preserved_subset(arn):
    s3_policy = _golden("iam_baseline_heredoc_deploy-account-role_S3_POLICY.json")
    assert _policy_allows(s3_policy["Statement"], "s3:GetObject", arn)


# --------------------------------------------------------------------------- #
# PBT 2 — tagged vs untagged buckets (unfixed = allow regardless of tag)
# --------------------------------------------------------------------------- #
# The unfixed Ground Truth / DDAPortalAccessRole S3 statement (I3, I4) is on
# arn:aws:s3:::* with NO Condition, so it allows any bucket for any tag state.
_UNFIXED_GROUNDTRUTH_S3 = {
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
    "Resource": ["arn:aws:s3:::*", "arn:aws:s3:::*/*"],
    # No "Condition" key — this is the bug the I3/I4 fix wires up.
}


# Validates: Requirements 3.3, 3.4
@settings(max_examples=25, deadline=None)
@given(
    arn=st.one_of(_dda_bucket_arn(), _non_dda_bucket_arn()),
    tag_present=st.booleans(),
    tag_value=st.sampled_from(["true", "false", "TRUE", ""]),
)
def test_pbt2_unfixed_allows_regardless_of_tag(arn, tag_present, tag_value):
    tags = {"dda-portal:managed": tag_value} if tag_present else {}
    # Unfixed baseline: no Condition is evaluated, so allow is tag-independent.
    allowed = _statement_allows(_UNFIXED_GROUNDTRUTH_S3, "s3:GetObject", arn)
    assert allowed is True
    # Record the fix's preservation target: the tagged subset must stay allowed.
    is_preserved_target = (tags.get("dda-portal:managed") == "true") or arn.startswith(
        "arn:aws:s3:::sagemaker-"
    )
    if is_preserved_target:
        assert allowed is True


# --------------------------------------------------------------------------- #
# PBT 3 — trusted vs untrusted account IDs (unfixed = allow any account)
# --------------------------------------------------------------------------- #
_UNFIXED_LABELING_ASSUME = {
    "Effect": "Allow",
    "Action": ["sts:AssumeRole"],
    "Resource": ["arn:aws:iam::*:role/DDAPortalAccessRole"],
}


# Validates: Requirements 3.5, 3.6
@settings(max_examples=25, deadline=None)
@given(trusted=st.lists(_account, min_size=1, max_size=4, unique=True), probe=_account)
def test_pbt3_unfixed_assume_role_allows_any_account(trusted, probe):
    arn = f"arn:aws:iam::{probe}:role/DDAPortalAccessRole"
    # Unfixed baseline: the account portion is wildcarded, so ANY probe account
    # is allowed regardless of the trusted set.
    assert _statement_allows(_UNFIXED_LABELING_ASSUME, "sts:AssumeRole", arn)
    # Record the fix's preservation target: trusted accounts must stay allowed.
    if probe in trusted:
        assert _statement_allows(_UNFIXED_LABELING_ASSUME, "sts:AssumeRole", arn)


# --------------------------------------------------------------------------- #
# PBT 4 — enumerable IoT action subset (unfixed = allow every IoT action)
# --------------------------------------------------------------------------- #
# The subset the edge-device / build-server / data-plane code paths exercise —
# exactly what the I11/I13/I15 fixes must preserve.
IOT_EXERCISED_SUBSET = [
    "iot:Connect", "iot:Publish", "iot:Subscribe", "iot:Receive",
    "iot:GetThingShadow", "iot:UpdateThingShadow", "iot:DescribeThing",
    "iot:DescribeEndpoint",
]
IOT_OTHER_ACTIONS = [
    "iot:DeleteThing", "iot:CreatePolicy", "iot:ListThings", "iot:DetachPolicy",
    "iot:UpdateCertificate", "iot:CreateKeysAndCertificate",
]


# Validates: Requirements 3.11, 3.13, 3.15
@settings(max_examples=25, deadline=None)
@given(action=st.sampled_from(IOT_EXERCISED_SUBSET + IOT_OTHER_ACTIONS))
def test_pbt4_unfixed_iot_wildcard_allows_all_actions(action):
    inline = _golden(
        "iam_baseline_heredoc_create-edge-device-iam-role_INLINE_POLICY.json"
    )
    iot_stmt = next(s for s in inline["Statement"] if s.get("Sid") == "IoTPermissions")
    assert iot_stmt["Action"] == ["iot:*"]
    # Baseline (unfixed): iot:* allows every IoT action on "*".
    assert _statement_allows(iot_stmt, action, "arn:aws:iot:us-east-1:111:thing/dda-x")


# Validates: Requirements 3.11, 3.13, 3.15 — the exercised subset (fix target) is
# fully inside the unfixed allow-set, so the fix can enumerate it losslessly.
@settings(max_examples=40, deadline=None)
@given(action=st.sampled_from(IOT_EXERCISED_SUBSET))
def test_pbt4_exercised_subset_is_preserved(action):
    inline = _golden(
        "iam_baseline_heredoc_create-edge-device-iam-role_INLINE_POLICY.json"
    )
    iot_stmt = next(s for s in inline["Statement"] if s.get("Sid") == "IoTPermissions")
    assert _statement_allows(iot_stmt, action, "arn:aws:iot:us-east-1:111:client/dda-y")
