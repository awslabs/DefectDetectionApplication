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
"""Tests for the Bedrock branch planner (detection-guided-bedrock-inspection
Requirements 5.1, 5.3, 5.7).

A binding belongs to a Bedrock branch iff its transitive ``upstreamNodeIds``
closure over the compiled document's ``executorBindings`` reaches exactly
one ``bedrock_inference`` node and no ``llm_inference`` node; everything
else is non-branch and keeps the post-run ordering. ``metadata`` bindings
join a branch through their ``attachTo`` targets.
"""
import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.branching import BranchPlan, bedrock_branches


def binding(node_id, kind, upstream=(), **extra):
    made = {
        "nodeId": node_id,
        "binding": kind,
        "parameters": {},
        "upstreamNodeIds": list(upstream),
        "downstreamNodeIds": [],
    }
    made.update(extra)
    return made


def document(*bindings):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": "aarch64-jp7",
        "segments": [],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


class TestEmptyAndBedrockFreeDocuments:
    def test_empty_document(self):
        assert bedrock_branches({}) == {}
        assert bedrock_branches({"executorBindings": []}) == {}

    def test_none_document(self):
        assert bedrock_branches(None) == {}

    def test_document_without_bedrock_nodes(self):
        doc = document(
            binding("filter_1", "inference_filter", upstream=["model_1"]),
            binding("mqtt_1", "mqtt_publish", upstream=["filter_1"]),
        )
        assert bedrock_branches(doc) == {}


class TestSingleBranch:
    def test_publish_directly_downstream(self):
        bedrock = binding("bedrock_1", "bedrock_inference", upstream=["model_1"])
        doc = document(
            bedrock,
            binding("mqtt_1", "mqtt_publish", upstream=["bedrock_1"]),
        )
        plans = bedrock_branches(doc)
        assert set(plans) == {"bedrock_1"}
        plan = plans["bedrock_1"]
        assert isinstance(plan, BranchPlan)
        assert plan.bedrock_node_id == "bedrock_1"
        assert plan.bedrock_binding is bedrock
        assert plan.binding_ids == ["mqtt_1"]

    def test_transitive_closure_through_gates(self):
        # bedrock_1 -> filter_1 -> cond_1 -> mqtt_1: every gate and the
        # publish are branch members via the transitive closure.
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("filter_1", "inference_filter", upstream=["bedrock_1"]),
            binding("cond_1", "conditional", upstream=["filter_1"]),
            binding("mqtt_1", "mqtt_publish", upstream=["cond_1"]),
        )
        plans = bedrock_branches(doc)
        assert plans["bedrock_1"].binding_ids == [
            "filter_1", "cond_1", "mqtt_1",
        ]

    def test_bedrock_binding_itself_is_not_a_member(self):
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("mqtt_1", "mqtt_publish", upstream=["bedrock_1"]),
        )
        plans = bedrock_branches(doc)
        assert "bedrock_1" not in plans["bedrock_1"].binding_ids

    def test_bedrock_node_with_no_downstream_gets_empty_plan(self):
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
        )
        plans = bedrock_branches(doc)
        assert set(plans) == {"bedrock_1"}
        assert plans["bedrock_1"].binding_ids == []


class TestParallelBranches:
    def test_three_parallel_branches(self):
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("bedrock_2", "bedrock_inference", upstream=["model_1"]),
            binding("bedrock_3", "bedrock_inference", upstream=["model_1"]),
            binding("cond_1", "conditional", upstream=["bedrock_1"]),
            binding("mqtt_1", "mqtt_publish", upstream=["cond_1"]),
            binding("cond_2", "conditional", upstream=["bedrock_2"]),
            binding("mqtt_2", "mqtt_publish", upstream=["cond_2"]),
            binding("cond_3", "conditional", upstream=["bedrock_3"]),
            binding("mqtt_3", "mqtt_publish", upstream=["cond_3"]),
        )
        plans = bedrock_branches(doc)
        assert set(plans) == {"bedrock_1", "bedrock_2", "bedrock_3"}
        assert plans["bedrock_1"].binding_ids == ["cond_1", "mqtt_1"]
        assert plans["bedrock_2"].binding_ids == ["cond_2", "mqtt_2"]
        assert plans["bedrock_3"].binding_ids == ["cond_3", "mqtt_3"]

    def test_branch_membership_is_disjoint(self):
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("bedrock_2", "bedrock_inference", upstream=["model_1"]),
            binding("mqtt_1", "mqtt_publish", upstream=["bedrock_1"]),
            binding("mqtt_2", "mqtt_publish", upstream=["bedrock_2"]),
        )
        plans = bedrock_branches(doc)
        assert set(plans["bedrock_1"].binding_ids) & set(
            plans["bedrock_2"].binding_ids) == set()


class TestNonBranchBindings:
    def test_binding_reaching_two_bedrock_nodes_is_non_branch(self):
        # A summary publish downstream of BOTH bedrock nodes spans
        # branches, so it keeps the post-run ordering (Requirement 5.7).
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("bedrock_2", "bedrock_inference", upstream=["model_1"]),
            binding("mqtt_all", "mqtt_publish",
                    upstream=["bedrock_1", "bedrock_2"]),
        )
        plans = bedrock_branches(doc)
        assert plans["bedrock_1"].binding_ids == []
        assert plans["bedrock_2"].binding_ids == []

    def test_transitively_reaching_two_bedrock_nodes_is_non_branch(self):
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("bedrock_2", "bedrock_inference", upstream=["model_1"]),
            binding("cond_1", "conditional", upstream=["bedrock_1"]),
            binding("mqtt_all", "mqtt_publish",
                    upstream=["cond_1", "bedrock_2"]),
        )
        plans = bedrock_branches(doc)
        # cond_1 reaches only bedrock_1: in-branch. mqtt_all reaches both.
        assert plans["bedrock_1"].binding_ids == ["cond_1"]
        assert plans["bedrock_2"].binding_ids == []

    def test_llm_downstream_exclusion(self):
        # A binding whose closure reaches an llm_inference node is
        # non-branch even when it also reaches exactly one bedrock node
        # (the LLM processor runs after the Bedrock join).
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("llm_1", "llm_inference", upstream=["model_1"]),
            binding("mqtt_1", "mqtt_publish",
                    upstream=["bedrock_1", "llm_1"]),
        )
        plans = bedrock_branches(doc)
        assert plans["bedrock_1"].binding_ids == []

    def test_transitive_llm_exclusion(self):
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("llm_1", "llm_inference", upstream=["model_1"]),
            binding("cond_1", "conditional", upstream=["llm_1"]),
            binding("mqtt_1", "mqtt_publish",
                    upstream=["bedrock_1", "cond_1"]),
        )
        plans = bedrock_branches(doc)
        assert plans["bedrock_1"].binding_ids == []

    def test_binding_reaching_zero_bedrock_nodes_is_non_branch(self):
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("mqtt_1", "mqtt_publish", upstream=["bedrock_1"]),
            binding("mqtt_plain", "mqtt_publish", upstream=["model_1"]),
        )
        plans = bedrock_branches(doc)
        assert plans["bedrock_1"].binding_ids == ["mqtt_1"]

    def test_llm_binding_never_joins_a_branch(self):
        # An llm_inference binding downstream of a bedrock node runs in
        # the LlmInferenceProcessor after the join, never per-branch.
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("llm_1", "llm_inference", upstream=["bedrock_1"]),
        )
        plans = bedrock_branches(doc)
        assert plans["bedrock_1"].binding_ids == []


class TestGateAndMetadataBindingsInsideABranch:
    def test_filter_conditional_and_metadata_are_branch_scoped(self):
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("filter_1", "inference_filter", upstream=["bedrock_1"]),
            binding("cond_1", "conditional", upstream=["bedrock_1"]),
            binding("mqtt_1", "mqtt_publish",
                    upstream=["filter_1", "cond_1"]),
            binding("meta_1", "metadata", upstream=["trigger_1"],
                    attachTo=["mqtt_1"]),
        )
        plans = bedrock_branches(doc)
        assert plans["bedrock_1"].binding_ids == [
            "filter_1", "cond_1", "mqtt_1", "meta_1",
        ]

    def test_metadata_attaching_only_to_non_branch_output_stays_out(self):
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("mqtt_1", "mqtt_publish", upstream=["bedrock_1"]),
            binding("mqtt_plain", "mqtt_publish", upstream=["model_1"]),
            binding("meta_1", "metadata", upstream=["trigger_1"],
                    attachTo=["mqtt_plain"]),
        )
        plans = bedrock_branches(doc)
        assert plans["bedrock_1"].binding_ids == ["mqtt_1"]

    def test_metadata_attached_to_outputs_in_two_branches_joins_both(self):
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("bedrock_2", "bedrock_inference", upstream=["model_1"]),
            binding("mqtt_1", "mqtt_publish", upstream=["bedrock_1"]),
            binding("mqtt_2", "mqtt_publish", upstream=["bedrock_2"]),
            binding("meta_1", "metadata", upstream=["trigger_1"],
                    attachTo=["mqtt_1", "mqtt_2"]),
        )
        plans = bedrock_branches(doc)
        assert plans["bedrock_1"].binding_ids == ["mqtt_1", "meta_1"]
        assert plans["bedrock_2"].binding_ids == ["mqtt_2", "meta_1"]


class TestRobustness:
    def test_cyclic_upstream_references_terminate(self):
        # The compiler never emits cycles; the planner must still
        # terminate on a malformed document instead of recursing forever.
        doc = document(
            binding("bedrock_1", "bedrock_inference", upstream=["model_1"]),
            binding("cond_a", "conditional", upstream=["cond_b", "bedrock_1"]),
            binding("cond_b", "conditional", upstream=["cond_a", "bedrock_1"]),
            binding("mqtt_1", "mqtt_publish", upstream=["cond_a"]),
        )
        plans = bedrock_branches(doc)
        assert plans["bedrock_1"].binding_ids == [
            "cond_a", "cond_b", "mqtt_1",
        ]

    def test_upstream_ids_without_bindings_terminate_the_walk(self):
        # model_inference / camera nodes compile to pipeline elements,
        # not executor bindings; the closure walks through them safely.
        doc = document(
            binding("bedrock_1", "bedrock_inference",
                    upstream=["model_1", "camera_1"]),
            binding("mqtt_1", "mqtt_publish", upstream=["bedrock_1"]),
        )
        plans = bedrock_branches(doc)
        assert plans["bedrock_1"].binding_ids == ["mqtt_1"]
