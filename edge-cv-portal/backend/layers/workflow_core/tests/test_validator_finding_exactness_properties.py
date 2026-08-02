"""Property test for validator finding-set exactness (task 3.4).

**Feature: workflow-manager, Property 3: Validator finding-set exactness**

For all graphs constructed by seeding a random valid graph with a random
set of known defects (missing input/output nodes, incompatible-port
connections, injected cycles, cleared required parameters, detached
unreachable nodes), the validator returns findings that exactly match the
seeded defect set — every seeded defect is reported with the correct
node/connection identifier (and cycle findings name nodes actually in the
cycle), and no findings are reported for defect classes that were not
seeded.

**Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6**
"""

from __future__ import annotations

from typing import FrozenSet

from hypothesis import given

from workflow_core.validator import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    validate,
)

from .generators import (
    ExpectedFinding,
    graph_strategy,
    seeded_graph_strategy,
)


def _error_findings(findings) -> FrozenSet[ExpectedFinding]:
    """Project validate()'s error-severity findings onto the
    (code, node_id, connection_id) shape used by SeededGraph.expected."""
    return frozenset(
        ExpectedFinding(
            code=finding.code,
            node_id=finding.node_id,
            connection_id=finding.connection_id,
        )
        for finding in findings
        if finding.severity == SEVERITY_ERROR
    )


@given(seeded=seeded_graph_strategy())
def test_validator_reports_exactly_the_seeded_defects(seeded):
    """**Feature: workflow-manager, Property 3: Validator finding-set exactness**

    The error-severity findings returned by validate() are exactly the
    expected findings carried by the seeded graph: every seeded defect is
    reported with the correct node/connection identifier and code, and no
    error findings appear for defect classes that were not seeded.

    **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6**
    """
    findings = validate(seeded.graph)

    actual = _error_findings(findings)

    missing = seeded.expected - actual
    unexpected = actual - seeded.expected
    assert actual == seeded.expected, (
        "validator findings do not exactly match the seeded defect set "
        "(defect classes seeded: {0})\n"
        "  missing (seeded but not reported): {1}\n"
        "  unexpected (reported but not seeded): {2}".format(
            sorted(seeded.defects), sorted(missing, key=repr),
            sorted(unexpected, key=repr),
        )
    )

    # Every finding carries the fields Requirement 4.6 mandates.
    for finding in findings:
        assert finding.severity in (SEVERITY_ERROR, SEVERITY_WARNING)
        assert finding.code
        assert finding.message


@given(graph=graph_strategy())
def test_validator_reports_no_errors_for_valid_graphs(graph):
    """**Feature: workflow-manager, Property 3: Validator finding-set exactness**

    The zero-defect case of finding-set exactness: for graphs with no
    seeded defects, validate() returns no error-severity findings
    (warnings such as unused output ports are permitted).

    **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6**
    """
    findings = validate(graph)

    errors = [f for f in findings if f.severity == SEVERITY_ERROR]
    assert errors == [], (
        "valid graph produced error findings: {0}".format(
            [f.to_dict() for f in errors]
        )
    )
