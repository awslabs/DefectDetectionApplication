"""``workflow_debug.targets`` (workflow-debug-mode design §7.2; tasks.md 3.4):
the statuses, both reference rules, both device lists and pagination of
``registered_devices``; equality with ``workflow_tuning.registered_devices``
on shared moto fixtures with ``DEPLOYMENTS_TABLE`` set; the two
``ACTIVE_DEPLOYMENT_STATUSES`` tuples equal; the intersection and its two
empty-set reasons; the per-session cap. ``targets.py`` imports nothing from
``workflow_tuning`` (a static check).
"""
import ast
import os
import sys
import uuid

import pytest

from workflow_debug import targets

WF = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
OTHER_WF = "ffffffff-4e5f-4a6b-8c9d-0e1f2a3b4c5d"


@pytest.fixture
def workflow_tuning(aws_stack):
    """The real ``workflow_tuning`` imported inside the moto stack (its
    module-level DynamoDB resource must be intercepted)."""
    sys.modules.pop("workflow_tuning", None)
    import workflow_tuning
    return workflow_tuning


def seed(env, usecase_id):
    """The shared fixtures: every status, both reference rules, both device
    lists, duplicates, an unrelated workflow and an unrelated use case."""
    env.put_deployment(usecase_id, status="ACTIVE", component_type="workflow", workflow_id=WF,
                       target_devices=["thing-b", "thing-a"])
    env.put_deployment(usecase_id, status="completed", components=[{"component_name": "dda.workflow." + WF}],
                       target_device_names=["thing-c", "thing-a"])
    env.put_deployment(usecase_id, status="IN_PROGRESS", components=["dda.workflow." + WF],
                       target_devices=["thing-d"])
    env.put_deployment(usecase_id, status="PENDING", component_type="workflow", workflow_id=WF,
                       target_devices=["thing-e"], target_device_names=["thing-f"])
    env.put_deployment(usecase_id, status="QUEUED", component_type="workflow", workflow_id=WF,
                       target_devices=["thing-g"])
    env.put_deployment(usecase_id, status="DEPLOYED", component_type="workflow", workflow_id=WF,
                       target_devices=["thing-h", "", None])
    # Not counted: a terminal status, another workflow, a model deployment.
    env.put_deployment(usecase_id, status="FAILED", component_type="workflow", workflow_id=WF,
                       target_devices=["thing-failed"])
    env.put_deployment(usecase_id, status="CANCELLED", components=["dda.workflow." + WF],
                       target_devices=["thing-cancelled"])
    env.put_deployment(usecase_id, status="ACTIVE", component_type="workflow", workflow_id=OTHER_WF,
                       target_devices=["thing-other"])
    env.put_deployment(usecase_id, status="ACTIVE", component_type="model", workflow_id=WF,
                       target_devices=["thing-model"])
    env.put_deployment(usecase_id, status="ACTIVE", components=[{"component_name": "dda.workflow." + OTHER_WF}],
                       target_devices=["thing-other-component"])
    env.put_deployment("uc-{0}".format(uuid.uuid4()), status="ACTIVE", component_type="workflow",
                       workflow_id=WF, target_devices=["thing-elsewhere"])


EXPECTED = ["thing-a", "thing-b", "thing-c", "thing-d", "thing-e", "thing-f", "thing-g", "thing-h"]


class TestRegisteredDevices:
    def test_statuses_both_rules_both_lists_sorted_and_unique(self, env):
        usecase_id = env.create_usecase()
        seed(env, usecase_id)
        assert targets.registered_devices(env.stack.tables.deployments, usecase_id, WF) == EXPECTED

    def test_equal_to_workflow_tuning_registered_devices_on_shared_fixtures(self, env, workflow_tuning):
        usecase_id = env.create_usecase()
        seed(env, usecase_id)
        assert workflow_tuning.DEPLOYMENTS_TABLE == os.environ["DEPLOYMENTS_TABLE"]
        ours = targets.registered_devices(env.stack.tables.deployments, usecase_id, WF)
        theirs = workflow_tuning.registered_devices(usecase_id, WF)
        assert ours == theirs == EXPECTED
        assert targets.registered_devices(env.stack.tables.deployments, usecase_id, OTHER_WF) == \
            workflow_tuning.registered_devices(usecase_id, OTHER_WF) == ["thing-other", "thing-other-component"]
        empty = "uc-{0}".format(uuid.uuid4())
        assert targets.registered_devices(env.stack.tables.deployments, empty, WF) == \
            workflow_tuning.registered_devices(empty, WF) == []

    def test_the_two_status_tuples_are_equal(self, workflow_tuning):
        assert targets.ACTIVE_DEPLOYMENT_STATUSES == workflow_tuning.ACTIVE_DEPLOYMENT_STATUSES
        assert targets.ACTIVE_DEPLOYMENT_STATUSES == ('ACTIVE', 'COMPLETED', 'IN_PROGRESS', 'PENDING', 'QUEUED', 'DEPLOYED')

    def test_pagination(self):
        class PagingTable:
            def __init__(self):
                self.calls = []

            def query(self, **kwargs):
                self.calls.append(kwargs)
                page = kwargs.get("ExclusiveStartKey", {"n": 0})["n"]
                items = [[{"deployment_status": "ACTIVE", "component_type": "workflow", "workflow_id": WF,
                           "target_devices": ["z-thing"]}],
                         [{"deployment_status": "active", "components": ["dda.workflow." + WF],
                           "target_device_names": ["a-thing", "z-thing"]}],
                         [{"deployment_status": "FAILED", "component_type": "workflow", "workflow_id": WF,
                           "target_devices": ["failed-thing"]}]][page]
                response = {"Items": items}
                if page < 2:
                    response["LastEvaluatedKey"] = {"n": page + 1}
                return response

        table = PagingTable()
        assert targets.registered_devices(table, "uc-x", WF) == ["a-thing", "z-thing"]
        assert len(table.calls) == 3
        assert table.calls[0]["IndexName"] == "usecase-deployments-index"
        assert table.calls[0]["ExpressionAttributeValues"] == {":uid": "uc-x"}
        assert table.calls[2]["ExclusiveStartKey"] == {"n": 2}

    def test_no_workflow_tuning_import(self):
        source = open(targets.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("workflow_tuning") for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("workflow_tuning")
                assert not (node.module or "").startswith("workflow_core")


class TestTargetSet:
    def test_intersection_and_reasons(self):
        result = targets.target_set(["thing-a", "thing-b", "thing-c"], ["thing-b", "thing-c", "thing-d"])
        assert result.devices == ["thing-b", "thing-c"]
        assert result.no_device_record == ["thing-a"]
        assert result.no_deployment == ["thing-d"]
        assert result.over_cap is False

    def test_empty_set_reasons(self):
        result = targets.target_set(["thing-a"], ["thing-z"])
        assert result.devices == []
        assert result.no_deployment == ["thing-z"]
        assert result.no_device_record == ["thing-a"]
        nothing = targets.target_set([], [])
        assert nothing == targets.TargetSet([], [], [], False)

    def test_per_session_cap(self):
        many = ["thing-%02d" % i for i in range(21)]
        assert targets.target_set(many, many, max_devices=20).over_cap is True
        assert targets.target_set(many[:20], many, max_devices=20).over_cap is False
        assert targets.target_set(many, many).over_cap is False

    def test_blank_names_are_ignored(self):
        result = targets.target_set(["", None, "a"], ["a", ""])
        assert result == targets.TargetSet(["a"], [], [], False)
