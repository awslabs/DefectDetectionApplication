#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Bedrock branch planner (detection-guided-bedrock-inspection,
Requirements 5.1, 5.3, 5.7).

Derives, once per run, which of a compiled document's ``executorBindings``
belong to a *Bedrock branch*: the set of bindings whose transitive
``upstreamNodeIds`` closure reaches exactly ONE ``bedrock_inference`` node
and NO ``llm_inference`` node. Those bindings (the branch's
``mqtt_publish``/other outputs plus the ``inference_filter`` /
``conditional`` gates between them and the Bedrock node) are evaluated
per-branch in the Bedrock processor's completion path, so each
inspection's result publishes as it lands (Requirement 5.3).

Everything else is *non-branch* and keeps today's post-run ordering
byte-identical (Requirement 5.7): bindings reaching zero Bedrock nodes,
bindings reaching multiple Bedrock nodes (their gating spans branches, so
they must see the fully-merged metadata), and bindings reaching any LLM
node (the LLM processor runs after the Bedrock join).

``metadata`` bindings resolve trigger-payload mappings, not inference
results, so their own upstream closure never reaches a Bedrock node; a
metadata binding is branch-scoped when any of its ``attachTo`` targets is
a member of that branch (Requirement 5.8: attachments must reach the
branch's payloads inside ``process_subset``, which only sees the branch's
binding list). A metadata binding attaching to outputs in several
branches appears in each — resolution is idempotent and trigger-only, so
repeated evaluation is safe.

Binding kinds with their own processors (``bedrock_inference`` itself and
``llm_inference``) are never branch members: the Bedrock binding is
carried on the plan explicitly, and LLM bindings always run in the
post-join ``LlmInferenceProcessor``.

This module deliberately avoids importing ``output_bindings`` (which will
grow an import of this planner), so the binding-kind discriminators are
mirrored as local constants; they are pinned to the compiled-document
contract, not to that module.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List

logger = logging.getLogger(__name__)

#: Compiled-document binding kinds this planner discriminates on
#: (mirrors output_bindings; kept local to avoid a circular import).
BINDING_BEDROCK_INFERENCE = "bedrock_inference"
BINDING_LLM_INFERENCE = "llm_inference"
BINDING_METADATA = "metadata"


@dataclass
class BranchPlan:
    """One Bedrock branch: the ``bedrock_inference`` binding plus the
    branch-scoped output/gate binding ids to run (via
    ``OutputBindingProcessor.process_subset``) when its outcome lands.

    ``binding_ids`` holds the member bindings' ``nodeId`` values in
    ``executorBindings`` emission order (the order the post-run
    processor would have visited them), never including the Bedrock
    binding itself.
    """

    bedrock_node_id: Any
    bedrock_binding: dict
    binding_ids: List[Any] = field(default_factory=list)


def _upstream_closures(
    bindings: List[dict],
) -> Dict[Any, FrozenSet[Any]]:
    """Transitive ``upstreamNodeIds`` closure per binding node id.

    The closure of a node is every node id reachable by repeatedly
    following ``upstreamNodeIds`` edges through bindings; upstream ids
    with no executor binding (pipeline-only nodes such as
    ``model_inference`` or camera sources) terminate their path but are
    still included in the closure. Iterative DFS with a memo; a cyclic
    document (never produced by the compiler, but this is defensive
    executor code) terminates with each node's partial closure rather
    than recursing forever.
    """
    upstream_by_node: Dict[Any, List[Any]] = {}
    for binding in bindings:
        node_id = binding.get("nodeId")
        upstream_by_node[node_id] = list(binding.get("upstreamNodeIds") or [])

    closures: Dict[Any, FrozenSet[Any]] = {}

    def compute(start: Any) -> FrozenSet[Any]:
        reached: set = set()
        stack = list(upstream_by_node.get(start) or [])
        while stack:
            node = stack.pop()
            if node in reached or node == start:
                continue
            reached.add(node)
            memoized = closures.get(node)
            if memoized is not None:
                reached.update(memoized)
                continue
            stack.extend(upstream_by_node.get(node) or [])
        return frozenset(reached)

    for node_id in upstream_by_node:
        if node_id not in closures:
            closures[node_id] = compute(node_id)
    return closures


def bedrock_branches(document: dict) -> Dict[Any, BranchPlan]:
    """Derive the document's Bedrock branches (Requirements 5.1, 5.3, 5.7).

    Returns one :class:`BranchPlan` per ``bedrock_inference`` binding,
    keyed by its node id (a Bedrock node with no downstream bindings gets
    an empty plan — its verdict still merges, it just publishes nothing
    itself). A non-Bedrock/non-LLM binding is a member of a branch iff
    its transitive upstream closure reaches exactly that one Bedrock node
    and no LLM node; ``metadata`` bindings additionally join every branch
    containing one of their ``attachTo`` targets. All other bindings are
    non-branch and keep the post-run ordering (Requirement 5.7).

    An empty or binding-less document yields ``{}``.
    """
    bindings = [
        binding
        for binding in ((document or {}).get("executorBindings") or [])
        if isinstance(binding, dict)
    ]
    if not bindings:
        return {}

    bedrock_ids = {
        binding.get("nodeId")
        for binding in bindings
        if binding.get("binding") == BINDING_BEDROCK_INFERENCE
    }
    if not bedrock_ids:
        return {}
    llm_ids = {
        binding.get("nodeId")
        for binding in bindings
        if binding.get("binding") == BINDING_LLM_INFERENCE
    }

    plans: Dict[Any, BranchPlan] = {}
    for binding in bindings:
        if binding.get("binding") == BINDING_BEDROCK_INFERENCE:
            node_id = binding.get("nodeId")
            plans[node_id] = BranchPlan(
                bedrock_node_id=node_id, bedrock_binding=binding)

    closures = _upstream_closures(bindings)

    # Membership by upstream closure: exactly one Bedrock node, no LLM
    # node. Bedrock/LLM bindings themselves never join (own processors);
    # metadata bindings join by attachTo below (their closure is
    # trigger-side by construction, but the closure rule still applies
    # if a graph ever wires one downstream of a Bedrock node).
    members: Dict[Any, set] = {node_id: set() for node_id in plans}
    for binding in bindings:
        kind = binding.get("binding")
        if kind in (BINDING_BEDROCK_INFERENCE, BINDING_LLM_INFERENCE):
            continue
        node_id = binding.get("nodeId")
        reached = closures.get(node_id) or frozenset()
        reached_bedrock = reached & bedrock_ids
        if len(reached_bedrock) != 1 or (reached & llm_ids):
            continue
        members[next(iter(reached_bedrock))].add(node_id)

    # Metadata pass: attach to every branch containing an attachTo target.
    for binding in bindings:
        if binding.get("binding") != BINDING_METADATA:
            continue
        attach_to = set(binding.get("attachTo") or [])
        if not attach_to:
            continue
        node_id = binding.get("nodeId")
        for member_ids in members.values():
            if node_id not in member_ids and (attach_to & member_ids):
                member_ids.add(node_id)

    # Emit binding_ids in executorBindings order (deterministic; matches
    # the order the post-run processor would have visited them).
    for binding in bindings:
        node_id = binding.get("nodeId")
        for bedrock_node_id, member_ids in members.items():
            if node_id in member_ids:
                plans[bedrock_node_id].binding_ids.append(node_id)

    if plans:
        logger.debug(
            "Bedrock branch plan: %s",
            {
                node_id: plan.binding_ids
                for node_id, plan in plans.items()
            },
        )
    return plans
