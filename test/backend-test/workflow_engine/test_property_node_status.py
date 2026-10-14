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
"""Property tests for per-node run-status collection (Task 4).

**Feature: deployed-workflow-run-observability, Property 1: Node-status
coverage and terminality**

*For any finished WorkflowExecution, ``node_status_json`` contains exactly
the set of ``nodeId``s that map to elements in the Compiled_Pipeline_Document,
and every entry is in a terminal state (``success``/``failure``/``warning``),
never ``pending``/``running``.*

**Validates: Requirements 3.1, 3.6**

**Feature: deployed-workflow-run-observability, Property 2: Single failure
attribution**

*When a run fails at an identifiable element, exactly the mapped node is
``failure`` and carries the error detail; when no element is identifiable, no
node is spuriously marked ``failure``.*

**Validates: Requirements 3.2**

Runs with the hypothesis profiles registered in this directory's conftest
(``engine-fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from workflow_engine.node_status import (
    NodeStatusCollector,
    STATUS_FAILURE,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    TERMINAL_STATES,
)

# A small nodeId pool (with None for synthetic elements) so element->node maps
# have realistic duplication (many elements -> one node) and synthetic nodes.
_NODE_IDS = st.sampled_from(["n0", "n1", "n2", "n3", "n4", None])


@st.composite
def _name_maps(draw):
    """A realistic element-name -> nodeId map (names unique, nodeIds drawn
    from the pool with duplicates and Nones), as rendering.element_name_map
    produces it."""
    node_ids = draw(st.lists(_NODE_IDS, min_size=1, max_size=8))
    return {
        "{0}{1}".format("el", index): node_id
        for index, node_id in enumerate(node_ids)
    }


def _participating(name_map):
    return {n for n in name_map.values() if n is not None}


# --- Property 1 --------------------------------------------------------------


@given(
    name_map=_name_maps(),
    warn_indices=st.sets(st.integers(min_value=0, max_value=7)),
    outcome=st.sampled_from(["success", "failure"]),
)
@settings(deadline=None)
def test_node_status_coverage_and_terminality(name_map, warn_indices, outcome):
    """**Feature: deployed-workflow-run-observability, Property 1:
    Node-status coverage and terminality**

    **Validates: Requirements 3.1, 3.6**
    """
    participating = _participating(name_map)
    collector = NodeStatusCollector(name_map)

    # Simulate a run: start, drive some live warnings, then a terminal outcome.
    collector.mark_running_all()
    element_names = list(name_map)
    for i in warn_indices:
        if i < len(element_names):
            collector.sink(element_names[i], "warning", "w{0}".format(i))

    if outcome == "success":
        collector.mark_success_all()
    else:
        # Fail an arbitrary participating node (or nothing when the document
        # has only synthetic elements).
        target = next(iter(sorted(participating)), None)
        collector.mark_failure(target, "boom")
    collector.finalize()

    result = collector.to_map()

    # Coverage: exactly the participating nodeIds, no more, no less (R3.1).
    assert set(result) == participating

    # Terminality: no node remains pending/running for a finished run (R3.6).
    for entry in result.values():
        assert entry["status"] in TERMINAL_STATES
        assert entry["status"] not in (STATUS_PENDING, STATUS_RUNNING)


# --- Property 2 --------------------------------------------------------------


@given(
    name_map=_name_maps(),
    failing_index=st.integers(min_value=0, max_value=7),
)
@settings(deadline=None)
def test_single_failure_attribution(name_map, failing_index):
    """**Feature: deployed-workflow-run-observability, Property 2: Single
    failure attribution**

    **Validates: Requirements 3.2**
    """
    element_names = list(name_map)
    assume(failing_index < len(element_names))
    failing_element = element_names[failing_index]
    failing_node = name_map[failing_element]
    # The failure must map to an identifiable node for this property.
    assume(failing_node is not None)

    collector = NodeStatusCollector(name_map)
    collector.mark_running_all()
    collector.mark_failure(failing_node, "element error detail")
    collector.finalize()
    result = collector.to_map()

    # Exactly the mapped node is failure, and it carries the detail (R3.2).
    failed = [n for n, e in result.items() if e["status"] == STATUS_FAILURE]
    assert failed == [failing_node]
    assert result[failing_node]["detail"] == "element error detail"

    # Every other participating node resolved best-effort (success), never
    # spuriously failed.
    for node_id, entry in result.items():
        if node_id != failing_node:
            assert entry["status"] == STATUS_SUCCESS


@given(name_map=_name_maps())
@settings(deadline=None)
def test_unidentifiable_failure_marks_no_node(name_map):
    """**Feature: deployed-workflow-run-observability, Property 2: Single
    failure attribution**

    When no element is identifiable (``failing_node_id`` is None), no node is
    spuriously marked ``failure``.

    **Validates: Requirements 3.2**
    """
    collector = NodeStatusCollector(name_map)
    collector.mark_running_all()
    collector.mark_failure(None, "unattributable")
    collector.finalize()
    result = collector.to_map()
    assert all(e["status"] != STATUS_FAILURE for e in result.values())
