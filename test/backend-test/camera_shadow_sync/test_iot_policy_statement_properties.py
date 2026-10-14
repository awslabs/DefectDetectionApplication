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
"""Hypothesis property + unit tests for the Gap 1 policy helper
(camera-shadow-sync-provisioning task 3.2).

Under test: ``station_install/iot_policy_shadow_statement.py`` — the pure
stdlib helper extracted from the ``setup_station.sh`` thing-policy ensure
block. It decides whether an IoT policy document already carries an
HTTPS-compatible shadow grant (``check`` / ``has_https_shadow_statement``)
and, when it does not, appends the ``ShadowManagerHttpsDataPlaneSync``
statement while preserving every existing statement verbatim and in order
(``augment``).

Properties quantify over arbitrary policy documents: 0-8 statements;
actions as strings or lists drawn from the shadow actions, wildcards
(``iot:*``, ``*``), and unrelated actions; resources drawn from
variable-scoped ARNs, ``thing/*`` ARNs, prefix ARNs (``thing/dda-*``),
and ``"*"``; optional Sids; ``Statement`` as a list or a single object.
No AWS calls anywhere.

**Validates: Requirements 2.1, 2.2, 3.1**

Run (from the repository root):
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
        test/backend-test/camera_shadow_sync/test_iot_policy_statement_properties.py -v

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER_PATH = REPO_ROOT / "station_install" / "iot_policy_shadow_statement.py"

# station_install/ is not a package — load the helper straight from its file
# (sibling-file distribution per design D3).
_spec = importlib.util.spec_from_file_location(
    "iot_policy_shadow_statement", str(HELPER_PATH)
)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)

SHADOW_ACTIONS = sorted(helper.REQUIRED_ACTIONS)
WILDCARD_ACTIONS = ["iot:*", "*"]
UNRELATED_ACTIONS = [
    "iot:Connect",
    "iot:Publish",
    "iot:Subscribe",
    "iot:Receive",
    "greengrass:*",
    "s3:GetObject",
]
ALL_ACTIONS = SHADOW_ACTIONS + WILDCARD_ACTIONS + UNRELATED_ACTIONS

# Actions that cover at least one required shadow action (per the helper's
# coverage rule: exact match, "iot:*", or "*").
SHADOW_COVERING_ACTIONS = set(SHADOW_ACTIONS) | set(WILDCARD_ACTIONS)

MQTT_VARIABLE_RESOURCE = "arn:aws:iot:*:*:thing/${iot:Connection.Thing.ThingName}"
VARIABLE_RESOURCES = [
    MQTT_VARIABLE_RESOURCE,
    "arn:aws:iot:us-west-2:123456789012:thing/${iot:Connection.Thing.ThingName}",
]
THING_STAR_RESOURCES = [
    "arn:aws:iot:*:*:thing/*",
    "arn:aws:iot:us-east-1:123456789012:thing/*",
]
PREFIX_RESOURCES = ["arn:aws:iot:*:*:thing/dda-*"]
ALL_RESOURCES = VARIABLE_RESOURCES + THING_STAR_RESOURCES + PREFIX_RESOURCES + ["*"]

# The variable-scoped MQTT shadow statement the installer-era ensure block
# wrote (HTTPS-incompatible; must always survive augment verbatim).
MQTT_SHADOW_STATEMENT = {
    "Effect": "Allow",
    "Action": [
        "iot:GetThingShadow",
        "iot:UpdateThingShadow",
        "iot:DeleteThingShadow",
    ],
    "Resource": MQTT_VARIABLE_RESOURCE,
}

# The installer-created MQTT-only document (no shadow statement at all).
INSTALLER_STATEMENT = {
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
MQTT_ONLY_DOC = {
    "Version": "2012-10-17",
    "Statement": [copy.deepcopy(INSTALLER_STATEMENT)],
}

# What the UNFIXED heredoc wrote: installer statement + the variable-scoped
# shadow statement (the incident-state document; every HTTPS sync 403'd).
VARIABLE_ONLY_SHADOW_DOC = {
    "Version": "2012-10-17",
    "Statement": [
        copy.deepcopy(INSTALLER_STATEMENT),
        copy.deepcopy(MQTT_SHADOW_STATEMENT),
    ],
}

# The literal manually-deployed production version-3 document (account
# 164152369890): the original two statements plus the verbatim
# ShadowManagerHttpsDataPlaneSync statement.
PRODUCTION_VERSION_3_DOC = {
    "Version": "2012-10-17",
    "Statement": [
        copy.deepcopy(INSTALLER_STATEMENT),
        copy.deepcopy(MQTT_SHADOW_STATEMENT),
        {
            "Sid": "ShadowManagerHttpsDataPlaneSync",
            "Effect": "Allow",
            "Action": [
                "iot:GetThingShadow",
                "iot:UpdateThingShadow",
                "iot:DeleteThingShadow",
            ],
            "Resource": "arn:aws:iot:*:*:thing/*",
        },
    ],
}


def normalize(doc):
    """The helper's normalization: deep copy with ``Statement`` as a list."""
    result = copy.deepcopy(doc)
    result["Statement"] = helper._as_list(result.get("Statement"))
    return result


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def _string_or_list(values, max_size=4):
    """A policy field as a bare string or a list of strings."""
    return st.one_of(
        st.sampled_from(values),
        st.lists(st.sampled_from(values), min_size=0, max_size=max_size),
    )


def statements(actions=ALL_ACTIONS, resources=ALL_RESOURCES,
               effects=("Allow", "Deny")):
    """Arbitrary policy statements: string-or-list actions/resources,
    optional Sid, Allow or Deny."""
    return st.fixed_dictionaries(
        {
            "Effect": st.sampled_from(list(effects)),
            "Action": _string_or_list(actions),
            "Resource": _string_or_list(resources),
        },
        optional={
            "Sid": st.sampled_from(["Stmt0", "MqttShadow", "CameraSync"]),
        },
    )


def documents(statement_strategy=None):
    """Arbitrary policy documents: 0-8 statements, ``Statement`` as a list
    or a single object, optional non-Statement top-level keys."""
    stmt = statement_strategy if statement_strategy is not None else statements()
    statement_field = st.one_of(
        st.lists(stmt, min_size=0, max_size=8),  # list of 0-8
        stmt,                                    # single object, not a list
    )
    return st.fixed_dictionaries(
        {"Statement": statement_field},
        optional={
            "Version": st.sampled_from(["2012-10-17", "2008-10-17"]),
            "Id": st.sampled_from(["policy-1", "gg-thing-policy"]),
        },
    )


@st.composite
def documents_with_inserted_statement(draw, insert_strategy):
    """A generated document with one drawn statement inserted at an
    arbitrary position of the normalized statement list."""
    doc = normalize(draw(documents()))
    inserted = draw(insert_strategy)
    index = draw(st.integers(min_value=0, max_value=len(doc["Statement"])))
    doc["Statement"].insert(index, copy.deepcopy(inserted))
    return doc


def https_compatible_grant_statements():
    """Variable-free ``thing/*`` (or ``*``) Allow statements covering all
    three shadow actions — the shape that must satisfy the predicate."""
    covering_actions = st.one_of(
        st.just(list(SHADOW_ACTIONS)),
        st.sampled_from(WILDCARD_ACTIONS),
        st.lists(st.sampled_from(WILDCARD_ACTIONS + UNRELATED_ACTIONS),
                 min_size=0, max_size=3).map(lambda extra: extra + ["iot:*"]),
    )
    clean_resource = st.sampled_from(THING_STAR_RESOURCES + ["*"])
    resource_field = st.one_of(
        clean_resource,
        st.tuples(
            st.lists(st.sampled_from(ALL_RESOURCES), min_size=0, max_size=3),
            clean_resource,
        ).map(lambda pair: pair[0] + [pair[1]]),
    )
    return st.fixed_dictionaries(
        {
            "Effect": st.just("Allow"),
            "Action": covering_actions,
            "Resource": resource_field,
        },
        optional={"Sid": st.sampled_from(["HttpsGrant"])},
    )


def variable_only_shadow_statements():
    """Statements constrained so that every shadow-action Allow has ``${``
    in all its resources: Deny statements (ignored by the predicate),
    Allow statements with only unrelated actions, or Allow statements
    whose resources are all variable-scoped."""
    return st.one_of(
        statements(effects=("Deny",)),
        statements(actions=UNRELATED_ACTIONS, effects=("Allow",)),
        statements(resources=VARIABLE_RESOURCES, effects=("Allow",)),
    )


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(doc=documents())
def test_property_1_augment_yields_https_grant_preserving_prefix(doc):
    """**Feature: camera-shadow-sync-provisioning, Property 1: Bug Condition
    — Gap 1: provisioning yields an HTTPS-compatible shadow grant**

    For ALL generated documents, ``has_https_shadow_statement(augment(doc))``
    holds; when the predicate does not hold on ``doc``, the output is
    ``normalize(doc.Statement)`` as a preserved prefix (unchanged, in order)
    plus exactly SHADOW_STATEMENT appended; non-``Statement`` top-level keys
    are unchanged.

    **Validates: Requirements 2.1, 2.2**
    """
    original = copy.deepcopy(doc)
    normalized_statements = helper._as_list(copy.deepcopy(doc).get("Statement"))

    result = helper.augment(doc)

    assert helper.has_https_shadow_statement(result)
    assert doc == original, "augment must not mutate its input"

    if helper.has_https_shadow_statement(original):
        assert result["Statement"] == normalized_statements
    else:
        assert result["Statement"][:-1] == normalized_statements, (
            "original statements must be a preserved prefix, unchanged and "
            "in order"
        )
        assert result["Statement"][-1] == helper.SHADOW_STATEMENT

    non_statement = {k: v for k, v in original.items() if k != "Statement"}
    result_non_statement = {k: v for k, v in result.items() if k != "Statement"}
    assert result_non_statement == non_statement


@settings(max_examples=100, deadline=None)
@given(doc=documents(statement_strategy=variable_only_shadow_statements()))
def test_property_1_predicate_false_when_shadow_allows_are_variable_scoped(doc):
    """**Feature: camera-shadow-sync-provisioning, Property 1: Bug Condition
    — Gap 1: provisioning yields an HTTPS-compatible shadow grant**

    Predicate soundness (false direction): for any document where every
    shadow-action Allow statement has ``${`` in all its resources (the
    generator emits only Deny statements, unrelated-action Allows, and
    variable-scoped-resource Allows), the predicate is false — the same
    class as the installer's MQTT-only document and the unfixed heredoc
    document (asserted literally in the unit tests below).

    **Validates: Requirements 2.1, 2.2**
    """
    assert not helper.has_https_shadow_statement(doc), (
        "predicate must not report an HTTPS-compatible grant for a document "
        "whose only shadow-action Allows are thing-policy-variable-scoped — "
        "that is exactly the grep false-idempotency bug"
    )


@settings(max_examples=100, deadline=None)
@given(doc=documents_with_inserted_statement(https_compatible_grant_statements()))
def test_property_1_predicate_true_with_variable_free_grant(doc):
    """**Feature: camera-shadow-sync-provisioning, Property 1: Bug Condition
    — Gap 1: provisioning yields an HTTPS-compatible shadow grant**

    Predicate soundness (true direction): any document containing a
    variable-free ``thing/*`` (or ``*``) Allow covering all three shadow
    actions satisfies the predicate, wherever the statement sits.

    **Validates: Requirements 2.1, 2.2**
    """
    assert helper.has_https_shadow_statement(doc)


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(doc=documents())
def test_property_2_augment_is_idempotent(doc):
    """**Feature: camera-shadow-sync-provisioning, Property 2: Bug Condition
    — Gap 1: the ensure step is idempotent**

    ``augment(augment(doc)) == augment(doc)`` for ALL documents; when
    ``has_https_shadow_statement(doc)`` holds, ``augment(doc) ==
    normalize(doc)`` (no write — repeated setup_station.sh runs are stable
    and never exhaust the 5-version limit).

    **Validates: Requirements 2.1**
    """
    once = helper.augment(doc)
    twice = helper.augment(once)
    assert twice == once

    if helper.has_https_shadow_statement(doc):
        assert once == normalize(doc)


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(doc=documents_with_inserted_statement(st.just(MQTT_SHADOW_STATEMENT)))
def test_property_5_mqtt_variable_statement_survives_augment_verbatim(doc):
    """**Feature: camera-shadow-sync-provisioning, Property 5: Preservation
    — setup_station.sh outside the fix sites, and the golden**

    For any generated document containing the variable-scoped MQTT shadow
    statement (``arn:aws:iot:*:*:thing/${iot:Connection.Thing.ThingName}``),
    that statement appears verbatim in ``augment``'s output — the statement
    that continues to serve MQTT is never removed or rewritten.

    **Validates: Requirements 3.1**
    """
    result = helper.augment(doc)
    assert MQTT_SHADOW_STATEMENT in result["Statement"], (
        "the variable-scoped MQTT shadow statement must survive augment "
        "verbatim"
    )
    # Stronger: it survives at its original position (augment only appends).
    original_positions = [
        i for i, s in enumerate(normalize(doc)["Statement"])
        if s == MQTT_SHADOW_STATEMENT
    ]
    for i in original_positions:
        assert result["Statement"][i] == MQTT_SHADOW_STATEMENT


# ---------------------------------------------------------------------------
# Unit tests — CLI (examples from the design)
# ---------------------------------------------------------------------------

def _run_cli(mode, stdin_bytes):
    # setup_station.sh invokes the helper as a plain `python3` child process.
    # The backend-test conftest exports PYTHONHOME (a Triton requirement)
    # which breaks any child interpreter, so run with it stripped — exactly
    # the clean environment the script has on a station.
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH")}
    return subprocess.run(
        [sys.executable, str(HELPER_PATH), mode],
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def test_cli_check_exit_0_when_statement_present():
    result = _run_cli("check", json.dumps(PRODUCTION_VERSION_3_DOC).encode())
    assert result.returncode == 0


def test_cli_check_exit_1_when_statement_absent():
    result = _run_cli("check", json.dumps(MQTT_ONLY_DOC).encode())
    assert result.returncode == 1


def test_cli_check_exit_2_on_garbage_stdin():
    result = _run_cli("check", b"this is not json {")
    assert result.returncode == 2


def test_cli_check_exit_2_on_non_object_document():
    result = _run_cli("check", b'["not", "an", "object"]')
    assert result.returncode == 2


def test_cli_augment_exit_2_on_garbage_stdin():
    result = _run_cli("augment", b"%%% garbage")
    assert result.returncode == 2


def test_cli_augment_output_is_valid_policy_document_json():
    """augment output must be valid JSON parseable as a policy document
    (fit for ``aws iot create-policy-version --policy-document``)."""
    result = _run_cli("augment", json.dumps(MQTT_ONLY_DOC).encode())
    assert result.returncode == 0
    parsed = json.loads(result.stdout.decode())
    assert isinstance(parsed, dict)
    assert isinstance(parsed["Statement"], list)
    assert parsed["Version"] == "2012-10-17"
    assert parsed["Statement"][0] == INSTALLER_STATEMENT
    assert parsed["Statement"][-1] == helper.SHADOW_STATEMENT
    # And the augmented document now passes check (exit 0).
    assert _run_cli("check", result.stdout).returncode == 0


# ---------------------------------------------------------------------------
# Unit tests — predicate examples (from the design)
# ---------------------------------------------------------------------------

def test_installer_mqtt_only_document_absent():
    assert not helper.has_https_shadow_statement(MQTT_ONLY_DOC)


def test_variable_only_shadow_document_absent():
    """The unfixed heredoc document (incident state): its only shadow
    statement is thing-policy-variable-scoped — absent."""
    assert not helper.has_https_shadow_statement(VARIABLE_ONLY_SHADOW_DOC)


def test_production_version_3_document_present_and_augment_noop():
    """Regression example (Property 2): the literal manually-deployed
    production version-3 document — predicate true, augment is a no-op
    (no new policy version; re-runs never exhaust the 5-version limit)."""
    assert helper.has_https_shadow_statement(PRODUCTION_VERSION_3_DOC)
    assert helper.augment(PRODUCTION_VERSION_3_DOC) == normalize(
        PRODUCTION_VERSION_3_DOC
    )


def test_iot_wildcard_action_document_present():
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "iot:*", "Resource": "arn:aws:iot:*:*:thing/*"}
        ],
    }
    assert helper.has_https_shadow_statement(doc)


def test_star_wildcard_action_document_present():
    doc = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["*"], "Resource": "*"}],
    }
    assert helper.has_https_shadow_statement(doc)


def test_statement_as_single_object_handled():
    """``Statement`` given as a single object (not a list) is handled by
    both the predicate and augment (which normalizes it to a list)."""
    present = {
        "Version": "2012-10-17",
        "Statement": {
            "Effect": "Allow",
            "Action": ["iot:GetThingShadow", "iot:UpdateThingShadow",
                        "iot:DeleteThingShadow"],
            "Resource": "arn:aws:iot:*:*:thing/*",
        },
    }
    assert helper.has_https_shadow_statement(present)
    assert helper.augment(present)["Statement"] == [present["Statement"]]

    absent = {"Version": "2012-10-17",
              "Statement": copy.deepcopy(INSTALLER_STATEMENT)}
    assert not helper.has_https_shadow_statement(absent)
    augmented = helper.augment(absent)
    assert augmented["Statement"] == [INSTALLER_STATEMENT,
                                      helper.SHADOW_STATEMENT]


def test_prefix_scoped_resource_deliberately_not_https_compatible():
    """Documented conservatism: prefix-scoped resources like ``thing/dda-*``
    do NOT satisfy the predicate (worst case is one extra appended statement,
    which satisfies the predicate on every later run)."""
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["iot:GetThingShadow", "iot:UpdateThingShadow",
                            "iot:DeleteThingShadow"],
                "Resource": "arn:aws:iot:*:*:thing/dda-*",
            }
        ],
    }
    assert not helper.has_https_shadow_statement(doc)
    assert helper.has_https_shadow_statement(helper.augment(doc))
