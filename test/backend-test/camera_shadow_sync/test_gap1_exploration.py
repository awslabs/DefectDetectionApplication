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
"""Bug condition exploration tests — Gap 1 (camera-shadow-sync-provisioning).

Spec: camera-shadow-sync-provisioning — exploration tests written BEFORE the
fix. They MUST FAIL on the unfixed tree; the failures are the executable
counterexamples confirming the root cause (already confirmed empirically on
device ryanorinagxdevkithomelabjp622 in account 164152369890). After the fix
they encode the expected behavior and must pass.

Gap 1: ``aws.greengrass.ShadowManager`` cloud sync uses the IoT data plane
over HTTPS (8443) authenticated with the device X.509 certificate, so it is
authorized by the certificate's IoT policy. Thing policy variables
(``${iot:Connection.Thing.ThingName}``) resolve only on MQTT connections, so
a shadow statement scoped with them denies every HTTPS ``CloudUpdateSyncRequest``
with ForbiddenException 403. The ``setup_station.sh`` thing-policy ensure block
(a) writes exactly such a variable-scoped statement in its heredoc, and
(b) uses ``grep -q "iot:UpdateThingShadow"`` as its idempotency check, which
that broken statement satisfies — so re-running setup can never repair it.

These tests parse ``station_install/setup_station.sh`` as text and execute its
embedded logic locally. No AWS calls.

**Validates: Requirements 1.1, 1.2, 1.3**

Run:
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/camera_shadow_sync/test_gap1_exploration.py -v
"""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SETUP_STATION = REPO_ROOT / "station_install" / "setup_station.sh"
SHADOW_HELPER = REPO_ROOT / "station_install" / "iot_policy_shadow_statement.py"

ENSURE_BLOCK_START = "Ensuring IoT thing policy allows shadow data-plane sync"
ENSURE_BLOCK_END = "Configuring Greengrass permissions"

SHADOW_ACTIONS = {
    "iot:GetThingShadow",
    "iot:UpdateThingShadow",
    "iot:DeleteThingShadow",
}

# The counterexample document from the incident: a policy whose ONLY shadow
# statement is thing-policy-variable-scoped (HTTPS-incompatible). This is what
# the unfixed ensure block itself writes, and what the production device's
# policy looked like while every CloudUpdateSyncRequest 403'd.
VARIABLE_ONLY_SHADOW_DOC = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "iot:Connect",
                "iot:Publish",
                "iot:Subscribe",
                "iot:Receive",
                "greengrass:*",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "iot:GetThingShadow",
                "iot:UpdateThingShadow",
                "iot:DeleteThingShadow",
            ],
            "Resource": "arn:aws:iot:*:*:thing/${iot:Connection.Thing.ThingName}",
        },
    ],
}

# The installer-created MQTT-only document (no shadow statement at all): the
# state of a freshly provisioned device before the ensure block runs.
MQTT_ONLY_DOC = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "iot:Connect",
                "iot:Publish",
                "iot:Subscribe",
                "iot:Receive",
                "greengrass:*",
            ],
            "Resource": "*",
        }
    ],
}


def _subprocess_env():
    """Environment for child processes that spawn python3.

    The backend-test conftest sets PYTHONHOME to sys.executable (needed by
    Triton's python backend); inherited by a child ``python3`` it makes the
    interpreter fail to boot (``No module named 'encodings'``). Strip it so
    the harness runs the script's real logic, exactly as on a station."""
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    return env


def _script_text():
    return SETUP_STATION.read_text()


def _ensure_block():
    """The thing-policy ensure block of setup_station.sh, extracted by content
    anchors (not line numbers)."""
    text = _script_text()
    start = text.index(ENSURE_BLOCK_START)
    end = text.index(ENSURE_BLOCK_END, start)
    return text[start:end]


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _shadow_statements(doc):
    """Statements of a policy document granting any of the shadow actions."""
    statements = _as_list(doc.get("Statement"))
    return [
        stmt
        for stmt in statements
        if isinstance(stmt, dict)
        and SHADOW_ACTIONS & set(_as_list(stmt.get("Action")))
    ]


def _heredoc_documents(block):
    """All JSON policy documents embedded as heredocs in a script block."""
    docs = []
    for match in re.finditer(
        r"<<\s*'?(\w+)'?\n(.*?)\n\1\b", block, flags=re.DOTALL
    ):
        body = match.group(2)
        try:
            docs.append(json.loads(body))
        except ValueError:
            continue
    return docs


def _idempotency_condition(block):
    """The shell condition guarding the 'already grants' skip path, extracted
    from the ensure block's ``elif <condition>; then`` line."""
    match = re.search(r"^\s*elif (.+\$policy_doc.+); then\s*$", block, re.MULTILINE)
    assert match is not None, (
        "could not locate the ensure block's idempotency check "
        "(the `elif ...$policy_doc...; then` line) in setup_station.sh"
    )
    return match.group(1)


def _run_idempotency_check(doc):
    """Execute the script's own idempotency condition against a policy
    document. Returns the exit code: 0 means the script would report
    'already grants shadow data-plane actions' and skip the repair write."""
    condition = _idempotency_condition(_ensure_block())
    harness = "\n".join(
        [
            "set -u",
            "policy_doc={}".format(shlex.quote(json.dumps(doc))),
            # Post-fix the condition invokes the sibling helper via
            # $shadow_helper; pre-fix this variable is simply unused.
            "shadow_helper={}".format(shlex.quote(str(SHADOW_HELPER))),
            "if {}; then exit 0; else exit 3; fi".format(condition),
        ]
    )
    result = subprocess.run(
        ["bash", "-c", harness],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=_subprocess_env(),
    )
    return result.returncode


def _iot_layer_written_shadow_statements():
    """Every shadow statement the ensure block can write to the IoT policy via
    ``aws iot create-policy-version``: statements of heredoc documents embedded
    in the block, plus (post-fix) the statement appended by the
    iot_policy_shadow_statement.py helper's augment path."""
    block = _ensure_block()
    statements = []
    for doc in _heredoc_documents(block):
        statements.extend(_shadow_statements(doc))
    if "iot_policy_shadow_statement.py" in block and SHADOW_HELPER.is_file():
        result = subprocess.run(
            ["python3", str(SHADOW_HELPER), "augment"],
            input=json.dumps(MQTT_ONLY_DOC).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_subprocess_env(),
        )
        if result.returncode == 0:
            statements.extend(_shadow_statements(json.loads(result.stdout.decode())))
    return statements


def _resources_of(stmt):
    return [r for r in _as_list(stmt.get("Resource")) if isinstance(r, str)]


# **Feature: camera-shadow-sync-provisioning, Property 1: Bug Condition — Gap 1:
# provisioning yields an HTTPS-compatible shadow grant**
# Validates: Requirements 1.1, 1.2, 1.3
def test_exploration_case_1_heredoc_shadow_statement_is_https_compatible():
    """Exploration case 1 — every policy document the ensure block embeds as a
    heredoc must carry only HTTPS-compatible shadow statements (no ``${``
    policy variables in the resource).

    FAILS on unfixed code: the heredoc's shadow resource is
    ``arn:aws:iot:*:*:thing/${iot:Connection.Thing.ThingName}``, which never
    resolves over HTTPS — every ShadowManager CloudUpdateSyncRequest 403s
    (isBugCondition_Gap1)."""
    block = _ensure_block()
    heredoc_docs = _heredoc_documents(block)
    offending = [
        resource
        for doc in heredoc_docs
        for stmt in _shadow_statements(doc)
        for resource in _resources_of(stmt)
        if "${" in resource
    ]
    assert offending == [], (
        "the thing-policy ensure block's heredoc writes thing-policy-variable-"
        "scoped shadow resources {} — these resolve only on MQTT, never on the "
        "HTTPS 8443 data plane ShadowManager cloud sync actually uses, so every "
        "CloudUpdateSyncRequest fails ForbiddenException 403".format(offending)
    )


# **Feature: camera-shadow-sync-provisioning, Property 2: Bug Condition — Gap 1:
# the ensure step is idempotent**
# Validates: Requirements 1.1, 1.2
def test_exploration_case_2_idempotency_check_rejects_variable_only_document():
    """Exploration case 2 — the ensure block's idempotency check must NOT
    report 'already grants shadow data-plane actions' for a document whose
    only shadow statement is thing-policy-variable-scoped (the broken,
    HTTPS-incompatible state).

    FAILS on unfixed code: ``echo "$policy_doc" | grep -q "iot:UpdateThingShadow"``
    exits 0 on the variable-only document — the false-idempotency
    counterexample: once any shadow statement exists (even the broken one),
    re-running setup_station.sh can never repair the policy."""
    exit_code = _run_idempotency_check(VARIABLE_ONLY_SHADOW_DOC)
    assert exit_code != 0, (
        "the ensure block's idempotency check exits 0 ('already granted') on a "
        "document whose only shadow statement is scoped to "
        "arn:aws:iot:*:*:thing/${iot:Connection.Thing.ThingName} — it cannot "
        "distinguish an HTTPS-compatible grant from the variable-scoped one, so "
        "re-runs never repair a broken policy"
    )


# **Feature: camera-shadow-sync-provisioning, Property 2 (sanity — passes both
# before and after the fix): a document with NO shadow statement takes the
# repair path**
def test_exploration_case_2_sanity_mqtt_only_document_not_reported_granted():
    """Sanity anchor for case 2: the installer's MQTT-only document (no shadow
    statement at all) must not be reported 'already granted' either — true on
    both the unfixed and fixed trees, confirming the harness executes the
    script's real check."""
    exit_code = _run_idempotency_check(MQTT_ONLY_DOC)
    assert exit_code != 0


# **Feature: camera-shadow-sync-provisioning, Property 1: Bug Condition — Gap 1:
# provisioning yields an HTTPS-compatible shadow grant**
# Validates: Requirements 1.1, 1.2, 1.3
def test_exploration_case_3_shadow_grant_provisioned_at_iot_policy_layer():
    """Exploration case 3 — the script must provision the shadow grant at the
    IoT policy layer (an ``aws iot create-policy-version`` writing a
    variable-free shadow statement), not solely the ShadowManagerSyncPolicy
    IAM policy on GreengrassV2TokenExchangeRole.

    FAILS on unfixed code: the only variable-free shadow grant in the script
    is the step 3.6 IAM one — the wrong authorization layer, never consulted
    by ShadowManager's certificate-authenticated HTTPS sync (IAM simulation
    said allowed while the device still got 403)."""
    block = _ensure_block()
    assert "create-policy-version" in block, (
        "the ensure block no longer writes IoT policy versions — cannot locate "
        "the IoT-policy-layer provisioning path"
    )
    # The IAM-layer grant (step 3.6) exists and is variable-free, but it is
    # invisible to ShadowManager sync; it must not be the only one.
    assert "ShadowManagerSyncPolicy" in _script_text()

    written = _iot_layer_written_shadow_statements()
    https_compatible = [
        stmt
        for stmt in written
        if stmt.get("Effect") == "Allow"
        and SHADOW_ACTIONS <= set(_as_list(stmt.get("Action")))
        and any("${" not in r for r in _resources_of(stmt))
    ]
    assert https_compatible, (
        "the script writes no variable-free shadow statement at the IoT policy "
        "layer (via aws iot create-policy-version); the only variable-free "
        "grant is the step 3.6 ShadowManagerSyncPolicy IAM policy on "
        "GreengrassV2TokenExchangeRole, which ShadowManager's cert-authenticated "
        "HTTPS sync never consults — isBugCondition_Gap1 holds for every device "
        "this script provisions"
    )
