"""Generation_Gate pure decision logic (portal-build-fleet-and-workflow-gates).

Classifies Workflow_Validator findings for generated Workflow_Definitions
into Structural_Errors and Unrepairable_Errors and decides whether a
generation is accepted, repaired (one Repair_Pass), or rejected
(design.md section 8; Requirements 8.2, 8.3, 8.5).

This module is deliberately pure: no AWS clients, no I/O — only the
`workflow_core` layer's constants — so the gate is fully unit- and
property-testable without mocks. `workflow_generator.py` (same Lambda
bundle) consumes it.

Public interface:

- ``STRUCTURAL_ERROR_CODES``: frozenset of validator finding codes that
  are Structural_Errors, pinning the eight Req 8.2 categories to the
  actual ``workflow_core.validator`` codes (see
  ``STRUCTURAL_ERROR_CATEGORIES`` for the category -> codes map).
- ``classify(findings, catalog) -> GateDecision``
- ``build_repair_message(definition_json, structural_errors) -> str``
- ``user_readable_errors(structural_errors, definition) -> list[dict]``
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from workflow_core.catalog.models import CATEGORY_INPUT, CATEGORY_OUTPUT
from workflow_core.validator import (
    CODE_V1_NO_INPUT_NODE,
    CODE_V1_NO_OUTPUT_NODE,
    CODE_V2_INCOMPATIBLE_TYPES,
    CODE_V2_SOURCE_NOT_OUTPUT,
    CODE_V2_TARGET_NOT_INPUT,
    CODE_V2_UNKNOWN_NODE,
    CODE_V2_UNKNOWN_PORT,
    CODE_V3_CYCLE,
    CODE_V5_UNREACHABLE_NODE,
    CODE_V7_COEXISTENCE_CONFLICT,
    SEVERITY_ERROR,
)

# --------------------------------------------------------------------------
# Structural_Error classification (Requirement 8.2)
# --------------------------------------------------------------------------

#: The eight Req 8.2 categories mapped onto the actual
#: ``workflow_core.validator`` finding codes. A unit test (task 4.2)
#: asserts every category maps to a real validator code.
STRUCTURAL_ERROR_CATEGORIES: Dict[str, tuple] = {
    # Connections joining incompatible port types.
    "incompatible_port_types": (CODE_V2_INCOMPATIBLE_TYPES,),
    # Backwards edges: endpoints that are not an output port joined to
    # an input port.
    "backwards_edge": (CODE_V2_SOURCE_NOT_OUTPUT, CODE_V2_TARGET_NOT_INPUT),
    # Cycles in the node graph.
    "cycle": (CODE_V3_CYCLE,),
    # Nodes unreachable from any input-category node.
    "unreachable_node": (CODE_V5_UNREACHABLE_NODE,),
    # Connections referencing nonexistent nodes or ports.
    "unknown_reference": (CODE_V2_UNKNOWN_NODE, CODE_V2_UNKNOWN_PORT),
    # Absence of an input-category node.
    "missing_input_node": (CODE_V1_NO_INPUT_NODE,),
    # Absence of an output-category node.
    "missing_output_node": (CODE_V1_NO_OUTPUT_NODE,),
    # Combinations of node types that cannot coexist in one workflow.
    "coexistence_conflict": (CODE_V7_COEXISTENCE_CONFLICT,),
}

#: Validator finding codes that make an error-severity finding a
#: Structural_Error. Error-severity findings NOT in this set (parameter
#: violations, unresolved model references, mqtt target, unknown node
#: type, ...) are not Structural_Errors and flow to the client inside
#: the complete findings list as today (Req 8.3).
STRUCTURAL_ERROR_CODES: frozenset = frozenset(
    code for codes in STRUCTURAL_ERROR_CATEGORIES.values() for code in codes
)

#: More Structural_Errors than this means generation collapse: one
#: Repair_Pass over such a graph predictably fails and wastes a long
#: Bedrock call, so the gate rejects without repairing (design section 8,
#: unrepairability rule 2).
UNREPAIRABLE_ERROR_THRESHOLD = 10

#: GateDecision.action values.
ACTION_ACCEPT = "accept"
ACTION_REPAIR = "repair"
ACTION_REJECT = "reject"


@dataclass
class GateDecision:
    """The gate's verdict on one generated Workflow_Definition.

    ``structural_errors`` / ``unrepairable_errors`` / ``all_findings``
    hold findings in wire form (the camelCase dict shape of
    ``ValidationFinding.to_dict``).
    """

    action: str  # ACTION_ACCEPT | ACTION_REPAIR | ACTION_REJECT
    structural_errors: List[Dict[str, Any]] = field(default_factory=list)
    unrepairable_errors: List[Dict[str, Any]] = field(default_factory=list)
    all_findings: List[Dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Finding / catalog normalization helpers
# --------------------------------------------------------------------------

def _wire(finding: Any) -> Dict[str, Any]:
    """One finding in wire form: accepts ``ValidationFinding`` dataclasses
    (via ``to_dict``) and already-wire-form dicts alike."""
    if hasattr(finding, "to_dict"):
        return finding.to_dict()
    return dict(finding)


def _descriptor_category(descriptor: Any) -> Optional[str]:
    """The category of one catalog entry: a ``NodeTypeDescriptor`` (the
    ``.category`` attribute) or a wire-form mapping (the ``category``
    key)."""
    category = getattr(descriptor, "category", None)
    if category is None and isinstance(descriptor, Mapping):
        category = descriptor.get("category")
    return category


# --------------------------------------------------------------------------
# classify (Requirements 8.2, 8.3, 8.5)
# --------------------------------------------------------------------------

def classify(findings: Iterable[Any], catalog: Sequence[Any]) -> GateDecision:
    """Classify validator ``findings`` and decide the gate action.

    A finding is a Structural_Error iff its severity is error AND its
    code is in :data:`STRUCTURAL_ERROR_CODES` (Req 8.2).

    A Structural_Error is an Unrepairable_Error iff (design section 8):

    1. it is a missing-input-node / missing-output-node finding and the
       effective ``catalog`` contains no node type of that category (no
       Repair_Pass can add a node type that does not exist); or
    2. the total Structural_Error count exceeds
       :data:`UNREPAIRABLE_ERROR_THRESHOLD` (generation collapse — every
       Structural_Error is then classified unrepairable).

    Decision: no Structural_Errors -> ``accept``; any Unrepairable_Error
    -> ``reject`` (no Repair_Pass, Req 8.5); otherwise -> ``repair``
    (exactly one pass, Req 8.4).
    """
    all_findings = [_wire(finding) for finding in findings]

    structural_errors = [
        finding for finding in all_findings
        if finding.get("severity") == SEVERITY_ERROR
        and finding.get("code") in STRUCTURAL_ERROR_CODES
    ]

    catalog_categories = {_descriptor_category(d) for d in catalog}

    unrepairable_errors = []
    if len(structural_errors) > UNREPAIRABLE_ERROR_THRESHOLD:
        unrepairable_errors = list(structural_errors)
    else:
        for finding in structural_errors:
            code = finding.get("code")
            if (code == CODE_V1_NO_INPUT_NODE
                    and CATEGORY_INPUT not in catalog_categories):
                unrepairable_errors.append(finding)
            elif (code == CODE_V1_NO_OUTPUT_NODE
                    and CATEGORY_OUTPUT not in catalog_categories):
                unrepairable_errors.append(finding)

    if not structural_errors:
        action = ACTION_ACCEPT
    elif unrepairable_errors:
        action = ACTION_REJECT
    else:
        action = ACTION_REPAIR

    return GateDecision(
        action=action,
        structural_errors=structural_errors,
        unrepairable_errors=unrepairable_errors,
        all_findings=all_findings,
    )


# --------------------------------------------------------------------------
# Repair_Pass message (Requirement 8.4)
# --------------------------------------------------------------------------

#: Per-code correction instruction embedded in the repair message.
_REPAIR_INSTRUCTIONS: Dict[str, str] = {
    CODE_V2_INCOMPATIBLE_TYPES: (
        "Reconnect these ports so the source output port type matches the "
        "target input port type, inserting or removing intermediate nodes "
        "as needed."
    ),
    CODE_V2_SOURCE_NOT_OUTPUT: (
        "Rewire this connection so its 'from' endpoint is an output port "
        "of the source node."
    ),
    CODE_V2_TARGET_NOT_INPUT: (
        "Rewire this connection so its 'to' endpoint is an input port of "
        "the target node."
    ),
    CODE_V3_CYCLE: (
        "Remove or redirect connections so the graph has no cycles; data "
        "must flow strictly from input nodes toward output nodes."
    ),
    CODE_V5_UNREACHABLE_NODE: (
        "Connect this node (directly or indirectly) downstream of an "
        "input node, or remove it if it is not needed."
    ),
    CODE_V2_UNKNOWN_NODE: (
        "Fix this connection to reference an existing node id, or remove "
        "the connection."
    ),
    CODE_V2_UNKNOWN_PORT: (
        "Fix this connection to reference a port that the node's type "
        "actually declares, or remove the connection."
    ),
    CODE_V1_NO_INPUT_NODE: (
        "Add an input-category node (a source) and connect it into the "
        "graph."
    ),
    CODE_V1_NO_OUTPUT_NODE: (
        "Add an output-category node (a sink) and connect the graph's "
        "results into it."
    ),
    CODE_V7_COEXISTENCE_CONFLICT: (
        "Keep at most one node of this type in the workflow: remove the "
        "extra instances and rewire their connections."
    ),
}

_GENERIC_REPAIR_INSTRUCTION = (
    "Correct the workflow so this validation error no longer occurs."
)


def _affected_ids(finding: Mapping[str, Any]) -> str:
    """A short human-readable locator for the finding's graph element."""
    node_id = finding.get("nodeId")
    if node_id:
        return "node '{0}'".format(node_id)
    connection_id = finding.get("connectionId")
    if connection_id:
        return "connection '{0}'".format(connection_id)
    return "the whole workflow"


def build_repair_message(definition_json: str,
                         structural_errors: Iterable[Any]) -> str:
    """The single additional user turn of the Repair_Pass (Req 8.4).

    Embeds the failed Workflow_Definition JSON and one numbered
    correction instruction per Structural_Error, and instructs the model
    to return the complete corrected definition.
    """
    errors = [_wire(error) for error in structural_errors]

    lines: List[str] = [
        "The workflow definition you produced failed structural "
        "validation and cannot work as generated.",
        "",
        "This is the failed workflow definition:",
        "```json",
        definition_json,
        "```",
        "",
        "It has the following structural errors. Correct every one of "
        "them:",
        "",
    ]
    for position, error in enumerate(errors, 1):
        code = error.get("code", "")
        message = error.get("message", "")
        instruction = _REPAIR_INSTRUCTIONS.get(code, _GENERIC_REPAIR_INSTRUCTION)
        lines.append("{0}. [{1}] {2} (affects {3}). {4}".format(
            position, code, message, _affected_ids(error), instruction))
    lines.extend([
        "",
        "Return the complete corrected workflow definition using the "
        "create_workflow tool. Correct every listed error while keeping "
        "the parts of the workflow that are not affected by the errors "
        "unchanged.",
    ])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# User-readable rejection details (Requirement 8.8)
# --------------------------------------------------------------------------

#: Per-code plain-language explanation of why the graph cannot work.
_EXPLANATIONS: Dict[str, str] = {
    CODE_V2_INCOMPATIBLE_TYPES: (
        "This connection joins two ports that carry different kinds of "
        "data, so the receiving node could never understand what it is "
        "sent."
    ),
    CODE_V2_SOURCE_NOT_OUTPUT: (
        "This connection starts at a port that is not an output port, so "
        "no data can flow along it — connections must run from an output "
        "port to an input port."
    ),
    CODE_V2_TARGET_NOT_INPUT: (
        "This connection ends at a port that is not an input port, so "
        "the receiving node cannot accept the data — connections must "
        "run from an output port to an input port."
    ),
    CODE_V3_CYCLE: (
        "These nodes form a loop, so data would circulate forever and "
        "never reach an output."
    ),
    CODE_V5_UNREACHABLE_NODE: (
        "No path from any input node reaches this node, so it would "
        "never receive any data to process."
    ),
    CODE_V2_UNKNOWN_NODE: (
        "This connection refers to a node that does not exist in the "
        "workflow, so it connects nothing."
    ),
    CODE_V2_UNKNOWN_PORT: (
        "This connection refers to a port that does not exist on the "
        "node it points at, so it connects nothing."
    ),
    CODE_V1_NO_INPUT_NODE: (
        "The workflow has no input node, so no data can ever enter the "
        "pipeline."
    ),
    CODE_V1_NO_OUTPUT_NODE: (
        "The workflow has no output node, so results would never leave "
        "the pipeline."
    ),
    CODE_V7_COEXISTENCE_CONFLICT: (
        "These nodes are of a type that supports only one instance per "
        "workflow, so they cannot run together in the same workflow."
    ),
}

_GENERIC_EXPLANATION = (
    "This structural problem prevents the workflow graph from working."
)


def _display_name(element: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The display name of one definition node, when one exists.

    Definition nodes have no intrinsic display-name field (the canvas
    shows the catalog type's display name), so this resolves the
    user-assigned name a definition can carry: advisory ``data`` keys
    (``label`` / ``displayName``) first, then ``name`` / ``label``
    parameters. Returns None when the element carries none of them —
    the affected entry then identifies the element by id alone.
    """
    if not isinstance(element, Mapping):
        return None
    data = element.get("data")
    if isinstance(data, Mapping):
        for key in ("label", "displayName"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    parameters = element.get("parameters")
    if isinstance(parameters, Mapping):
        for key in ("name", "label"):
            value = parameters.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _affected_entry(kind: str, element_id: str,
                    element: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """One affected node/connection: id plus display name when one
    exists; id alone otherwise (Req 8.8)."""
    entry: Dict[str, Any] = {"id": element_id, "kind": kind}
    display_name = _display_name(element)
    if display_name is not None:
        entry["displayName"] = display_name
    return entry


def user_readable_errors(structural_errors: Iterable[Any],
                         definition: Any) -> List[Dict[str, Any]]:
    """Render Structural_Errors for user display (Req 8.8).

    Returns one entry per error:
    ``{code, message, affected: [{id, displayName?, kind}], explanation}``
    with each affected node or connection resolved to its identifier and
    display name (identifier alone when no display name exists) and a
    non-empty plain-language explanation of why the graph cannot work.

    ``definition`` is the Workflow_Definition wire document (mapping or
    JSON string); graph-level errors (e.g. a missing input node) have an
    empty ``affected`` list.
    """
    if isinstance(definition, str):
        try:
            definition = json.loads(definition)
        except ValueError:
            definition = {}
    if not isinstance(definition, Mapping):
        definition = {}

    nodes_by_id = {
        node.get("id"): node
        for node in definition.get("nodes") or []
        if isinstance(node, Mapping)
    }
    connections_by_id = {
        connection.get("id"): connection
        for connection in definition.get("connections") or []
        if isinstance(connection, Mapping)
    }

    entries: List[Dict[str, Any]] = []
    for error in structural_errors:
        error = _wire(error)
        code = error.get("code", "")

        affected: List[Dict[str, Any]] = []
        node_id = error.get("nodeId")
        if node_id:
            affected.append(_affected_entry("node", node_id,
                                            nodes_by_id.get(node_id)))
        connection_id = error.get("connectionId")
        if connection_id:
            affected.append(_affected_entry("connection", connection_id,
                                            connections_by_id.get(connection_id)))

        entries.append({
            "code": code,
            "message": error.get("message", ""),
            "affected": affected,
            "explanation": _EXPLANATIONS.get(code, _GENERIC_EXPLANATION),
        })
    return entries
