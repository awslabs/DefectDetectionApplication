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
"""IoT policy shadow-statement helper for setup_station.sh (Gap 1 fix).

``aws.greengrass.ShadowManager`` cloud sync calls the IoT data plane over
HTTPS (port 8443) authenticated with the device X.509 certificate, so it is
authorized by the certificate's IoT policy. Thing policy variables
(``${iot:Connection.Thing.ThingName}``) resolve only on MQTT connections;
a shadow statement scoped with them denies every HTTPS sync request with
ForbiddenException 403. This module decides whether a policy document
already carries an HTTPS-compatible shadow grant (``check``) and, when it
does not, appends the ``ShadowManagerHttpsDataPlaneSync`` statement while
preserving every existing statement verbatim and in order (``augment``).

Constraints (design D3, camera-shadow-sync-provisioning spec):

- Pure stdlib, Python 3.6-compatible (JP4 devices run Ubuntu 18.04's system
  python3). No AWS SDK, no network: input is a policy document JSON on
  stdin; output is an exit code (``check``) or the augmented document JSON
  on stdout (``augment``).
- Distributed as a sibling file of setup_station.sh, following the
  ``edge_manager_agent_config.json`` precedent.

Scope note: ``Deny`` statements are deliberately ignored — the predicate
only asks whether an HTTPS-compatible *Allow* of the three shadow actions
exists. Reasoning about Deny/Allow interaction is IoT policy evaluation,
which is out of scope for this provisioning check.

CLI:
    python3 iot_policy_shadow_statement.py check   < policy.json
        exit 0: HTTPS-compatible shadow statement present
        exit 1: absent
        exit 2: unparseable JSON / malformed document
    python3 iot_policy_shadow_statement.py augment < policy.json
        writes the augmented document JSON to stdout; exit 0 or 2
"""
import copy
import json
import sys

# The three shadow actions ShadowManager cloud sync needs on the data plane.
REQUIRED_ACTIONS = frozenset({
    "iot:GetThingShadow",
    "iot:UpdateThingShadow",
    "iot:DeleteThingShadow",
})

# The statement appended by augment(). Matches the verified manual production
# fix (policy version 3 in account 164152369890): no policy variables, so it
# authorizes the certificate-authenticated HTTPS data plane.
SHADOW_STATEMENT = {
    "Sid": "ShadowManagerHttpsDataPlaneSync",
    "Effect": "Allow",
    "Action": [
        "iot:GetThingShadow",
        "iot:UpdateThingShadow",
        "iot:DeleteThingShadow",
    ],
    "Resource": "arn:aws:iot:*:*:thing/*",
}


def _as_list(value):
    """Normalize a string-or-list policy field to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _action_covers(action_entry, required_action):
    """An action entry covers a required action iff it equals the action,
    ``"iot:*"``, or ``"*"``."""
    return action_entry in (required_action, "iot:*", "*")


def _is_https_compatible_resource(resource):
    """A resource is HTTPS-compatible iff it is a string containing no
    ``"${"`` (no policy variables) and either equals ``"*"`` or is an
    ``arn:aws:iot:...`` ARN whose final ``":"``-segment is exactly
    ``"thing/*"``.

    Deliberately conservative: prefix-scoped resources like ``thing/dda-*``
    do NOT satisfy the predicate. The worst case of a false negative is one
    extra appended statement, which satisfies the predicate on every later
    run."""
    if not isinstance(resource, str):
        return False
    if "${" in resource:
        return False
    if resource == "*":
        return True
    return (
        resource.startswith("arn:aws:iot:")
        and resource.split(":")[-1] == "thing/*"
    )


def statement_grants_https_shadow(stmt):
    """True iff ``stmt`` is an Allow statement covering every REQUIRED_ACTIONS
    member with at least one HTTPS-compatible resource."""
    if not isinstance(stmt, dict):
        return False
    if stmt.get("Effect") != "Allow":
        return False
    actions = _as_list(stmt.get("Action"))
    for required in REQUIRED_ACTIONS:
        if not any(_action_covers(entry, required) for entry in actions):
            return False
    resources = _as_list(stmt.get("Resource"))
    return any(_is_https_compatible_resource(r) for r in resources)


def has_https_shadow_statement(doc):
    """True iff any statement in the normalized ``Statement`` list grants
    HTTPS-compatible shadow access. Deny statements are ignored (out of
    scope — see module docstring)."""
    statements = _as_list(doc.get("Statement"))
    return any(statement_grants_https_shadow(stmt) for stmt in statements)


def augment(doc):
    """Return a deep copy of ``doc`` with ``Statement`` normalized to a list
    and, unless the HTTPS-compatible shadow predicate already holds
    (defensive idempotence), SHADOW_STATEMENT appended.

    All pre-existing statements and every other top-level key (``Version``,
    etc.) are preserved verbatim and in order — the MQTT thing-policy-variable
    statement is never removed or rewritten; augment only appends."""
    result = copy.deepcopy(doc)
    result["Statement"] = _as_list(result.get("Statement"))
    if not has_https_shadow_statement(result):
        result["Statement"].append(copy.deepcopy(SHADOW_STATEMENT))
    return result


def _read_document(stream):
    """Parse a policy document from a stream; raise ValueError if the JSON is
    unparseable or the top level is not an object."""
    doc = json.loads(stream.read())
    if not isinstance(doc, dict):
        raise ValueError("policy document must be a JSON object")
    return doc


def main(argv):
    if len(argv) != 2 or argv[1] not in ("check", "augment"):
        sys.stderr.write(
            "usage: iot_policy_shadow_statement.py {check|augment} "
            "< policy-document.json\n"
        )
        return 2

    try:
        doc = _read_document(sys.stdin)
    except ValueError as exc:
        sys.stderr.write(
            "iot_policy_shadow_statement.py: malformed policy document: "
            "{}\n".format(exc)
        )
        return 2

    if argv[1] == "check":
        return 0 if has_https_shadow_statement(doc) else 1

    sys.stdout.write(json.dumps(augment(doc), indent=2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
