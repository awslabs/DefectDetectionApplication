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
"""Standalone JSON policy template preservation baseline (I15).

Spec: security-iam-authorization-fixes — Property 2: Preservation.

``station_install/edge-device-iam-policy.json`` has one bug-condition sid
(``IoTDataPlane`` on ``"Resource": "*"``, split by the I15 fix) and FOUR sids
that must remain byte-for-byte identical: ``GreengrassComponentDownload``,
``CloudWatchLogsUpload``, ``AssumeDataAccountRole``, ``GreengrassConnectivity``.

This baseline records the four preserved sids and the pre-fix ``IoTDataPlane``
shape. On the unfixed tree the four preserved sids equal the golden (identity,
PASS). Task 9 re-runs this file against the fixed template and asserts the four
preserved sids are still byte-for-byte identical while ``IoTDataPlane`` has been
split by IoT resource type.

**Validates: Requirements 3.15**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_iam_json_template.py \
        -p no:cacheprovider --noconftest -v
"""
import hashlib
import json
import os

import pytest

from _iam_preservation_support import read_repo_file

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.normpath(os.path.join(HERE, "..", "baselines"))

TEMPLATE_REL = "station_install/edge-device-iam-policy.json"
PRESERVED_SIDS = [
    "GreengrassComponentDownload",
    "CloudWatchLogsUpload",
    "AssumeDataAccountRole",
    "GreengrassConnectivity",
]


def _baseline():
    with open(os.path.join(BASELINES, "iam_baseline_edge-device-iam-policy.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def _current():
    return json.loads(read_repo_file(TEMPLATE_REL))


@pytest.mark.parametrize("sid", PRESERVED_SIDS)
# Validates: Requirements 3.15 — the four non-I15 sids are byte-for-byte identical.
def test_preserved_sid_byte_for_byte(sid):
    current = {s["Sid"]: s for s in _current()["Statement"] if "Sid" in s}
    golden = _baseline()["preserved_sids"]
    assert sid in current, f"preserved sid {sid} missing from template"
    # json.dumps with sorted keys gives a canonical byte-for-byte comparison of
    # the statement content (key order in the source file is irrelevant to IAM).
    assert json.dumps(current[sid], sort_keys=True) == json.dumps(
        golden[sid], sort_keys=True
    ), f"preserved sid {sid} drifted from baseline"


# Validates: Requirements 3.15 — F(X): the IoTDataPlane sid on the unfixed tree
# is a single statement on "Resource": "*" (the baseline the I15 fix splits).
def test_iotdataplane_baseline_is_wildcard():
    golden = _baseline()
    iot = [s for s in golden["full"]["Statement"] if s.get("Sid") == "IoTDataPlane"]
    assert len(iot) == 1
    assert iot[0]["Resource"] == "*"
    assert sorted(iot[0]["Action"]) == ["iot:Connect", "iot:Publish",
                                        "iot:Receive", "iot:Subscribe"]


# Validates: Requirements 3.15 — records the whole-file sha256 so task 9 can show
# exactly which sids changed; not an equality gate (the file legitimately changes
# at the IoTDataPlane sid).
def test_whole_file_sha256_recorded():
    golden = _baseline()
    # The baseline recorded a valid whole-file sha256 on the unfixed tree.
    assert isinstance(golden["sha256"], str) and len(golden["sha256"]) == 64
    current_sha = hashlib.sha256(read_repo_file(TEMPLATE_REL).encode("utf-8")).hexdigest()
    if golden["sha256"] == current_sha:
        # UNFIXED tree: the recorded sha matches the current file.
        return
    # FIXED tree: the file legitimately changes ONLY at the IoTDataPlane sid
    # (split by IoT resource type). Assert the drift is confined there — every
    # non-IoT-data-plane statement is byte-for-byte identical to the baseline.
    def _non_iot(statements):
        iot_actions = {"iot:Connect", "iot:Publish", "iot:Subscribe", "iot:Receive"}

        def _is_iot_dataplane(stmt):
            actions = stmt["Action"]
            if isinstance(actions, str):
                actions = [actions]
            return bool(set(actions) & iot_actions)

        return [json.dumps(s, sort_keys=True) for s in statements
                if not _is_iot_dataplane(s)]

    assert _non_iot(_current()["Statement"]) == _non_iot(golden["full"]["Statement"]), (
        "whole-file drift is not confined to the IoTDataPlane sid"
    )
