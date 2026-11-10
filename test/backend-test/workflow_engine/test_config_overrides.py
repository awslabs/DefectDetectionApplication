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
"""``WorkflowNodeOverrides`` / ``WorkflowDisabled`` configuration switches
(bedrock-inspection-chain design 7, 11): parsing, the per-type allow-list,
descriptor coercion and checks, the poller with a fake reader (empty before
the first read, last-good after a failing one, ``snapshot()`` never reads),
``apply_overrides`` on a compiled document, the trigger core's disabled
check and the executor applying an override to the run's copy only.
"""
import copy
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from workflow_engine import gst_plugins
from workflow_engine.config_overrides import (
    ANY_WORKFLOW,
    DISABLED_KEY,
    EMPTY_SNAPSHOT,
    IGNORED_OVERRIDE_WARNING_INTERVAL_SECONDS,
    NODE_OVERRIDES_KEY,
    OVERRIDE_ALLOW_LIST,
    READ_FAILURE_WARNING_INTERVAL_SECONDS,
    WorkflowOverridesConfig,
    coerce_parameter_value,
    node_types_from_graph,
    parse_disabled,
    parse_node_overrides,
)
from workflow_engine.pipeline_executor import WorkflowExecutor
from workflow_engine.trigger_runtime import (
    TriggerPolicy,
    WorkflowActivationCore,
)
from workflow_engine.vendor.workflow_core.catalog import get_node_type

LOGGER = "workflow_engine.config_overrides"
WF_A = "wf-a"
WF_B = "wf-b"

#: A compiled document with one binding per allow-listed type plus a
#: trigger binding and a conditional (executor-level, not overridable).
DOCUMENT = {
    "schemaVersion": 1,
    "workflowId": WF_A,
    "segments": [{"name": "s0", "elements": [
        {"nodeId": "camera", "factory": "videotestsrc", "args": {}}]}],
    "executorBindings": [
        {"nodeId": "trigger_1", "binding": "mqtt_subscribe",
         "parameters": {"topic": "plant/line1/evaluate", "qos": 1}},
        {"nodeId": "check", "binding": "bedrock_inference",
         "parameters": {"model": "us.amazon.nova-pro-v1:0", "region": "us-east-1",
                        "enabled": True, "attempts": 2, "error_verdict": "error",
                        "prompt": "Check the order."}},
        {"nodeId": "upload", "binding": "s3_upload",
         "parameters": {"destination": "s3://example-bucket/{trigger.payload_json.body.image_key}",
                        "presign_get_expiry_s": 900, "timeout_s": 10, "retries": 1}},
        {"nodeId": "publish", "binding": "mqtt_publish",
         "parameters": {"topic": "plant/line1/result", "qos": 0, "retain": False}},
        {"nodeId": "gate", "binding": "conditional",
         "parameters": {"condition": "is_anomalous == true"}},
    ],
    "pluginDependencies": [],
}
GRAPH = {"schemaVersion": 1, "nodes": [
    {"id": "camera", "type": "usb_camera", "parameters": {}},
    {"id": "trigger_1", "type": "mqtt_subscribe", "parameters": {}},
    {"id": "check", "type": "bedrock_inference", "parameters": {}},
    {"id": "upload", "type": "s3_upload", "parameters": {}},
    {"id": "publish", "type": "mqtt_publish", "parameters": {}},
    {"id": "gate", "type": "conditional", "parameters": {}},
], "connections": []}
NODE_TYPES = {node["id"]: node["type"] for node in GRAPH["nodes"]}


class FakeClock:
    def __init__(self, now=5000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeReader:
    """Returns the queued configurations in order (an Exception instance
    is raised); the last one is repeated."""

    def __init__(self, *configurations):
        self.queue = list(configurations)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        return copy.deepcopy(item)


def component_config(overrides=None, disabled=None):
    config = {"StationName": "Station", "ComponentName": "aws.edgeml.dda.LocalServer"}
    if overrides is not None:
        config[NODE_OVERRIDES_KEY] = overrides
    if disabled is not None:
        config[DISABLED_KEY] = disabled
    return config


def make_config(*configurations, clock=None):
    reader = FakeReader(*(configurations or (component_config(),)))
    return WorkflowOverridesConfig(read_config=reader, clock=clock or FakeClock()), reader


def warnings_of(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and r.levelno == logging.WARNING]



class TestParseDisabled:
    @pytest.mark.parametrize("value", ["true", True, "TRUE", " true "])
    def test_true_spellings_disable(self, value):
        assert parse_disabled({WF_A: value}) == (frozenset({WF_A}), [])

    @pytest.mark.parametrize("value", ["false", False, "False"])
    def test_false_spellings_enable(self, value):
        assert parse_disabled({WF_A: value}) == (frozenset(), [])

    @pytest.mark.parametrize("value", ["yes", "on", "1", 1, None, "", "disabled"])
    def test_anything_else_disables_fail_closed_with_a_warning(self, value):
        disabled, warnings = parse_disabled({WF_A: value})
        assert disabled == frozenset({WF_A})
        assert len(warnings) == 1 and "unrecognised value" in warnings[0]
        assert "WorkflowDisabled['wf-a']" in warnings[0]

    def test_absent_and_star(self):
        assert parse_disabled(None) == (frozenset(), [])
        assert parse_disabled({}) == (frozenset(), [])
        assert parse_disabled({ANY_WORKFLOW: "true", WF_B: "false"})[0] == frozenset({"*"})

    def test_non_map_subtree_is_empty_with_a_warning(self):
        disabled, warnings = parse_disabled("true")
        assert disabled == frozenset()
        assert warnings == [
            "WorkflowDisabled is not a map of workflow ids (got str); treating it as empty"]


class TestParseNodeOverrides:
    def test_nested_shape_is_kept_with_raw_values(self):
        raw = {WF_A: {"check": {"model": "m", "attempts": "2"}}, "*": {"publish": {"qos": 1}}}
        assert parse_node_overrides(raw) == (raw, [])

    def test_parameters_no_type_allows_are_dropped_with_a_warning(self):
        parsed, warnings = parse_node_overrides(
            {WF_A: {"check": {"prompt": "x", "answer_format": "legacy", "attempts": 1},
                    "publish": {"topic": "t", "payload_template": "p"},
                    "upload": {"destination": "s3://x/y", "source": "frame"}}})
        assert parsed == {WF_A: {"check": {"attempts": 1}}}
        assert len(warnings) == 6
        assert all("is not an overridable parameter of any node type" in w for w in warnings)
        assert "WorkflowNodeOverrides['wf-a']['check']['prompt']" in warnings[0]

    def test_non_map_subtrees_and_non_scalar_values_are_dropped(self):
        parsed, warnings = parse_node_overrides(
            {WF_A: "oops", WF_B: {"check": "oops", "publish": {"qos": [1], "retain": "true"}}})
        assert parsed == {WF_B: {"publish": {"retain": "true"}}}
        assert len(warnings) == 3
        assert parse_node_overrides([]) == ({}, [
            "WorkflowNodeOverrides is not a map of workflow ids (got list); treating it as empty"])
        assert parse_node_overrides(None) == ({}, [])


class TestCoercion:
    @pytest.mark.parametrize("node_type, name, raw, expected", [
        ("bedrock_inference", "enabled", "false", False),
        ("bedrock_inference", "enabled", "True", True),
        ("bedrock_inference", "enabled", True, True),
        ("bedrock_inference", "attempts", "2", 2),
        ("bedrock_inference", "attempts", 2.0, 2),
        ("bedrock_inference", "attempts", "abc", "abc"),
        ("bedrock_inference", "model", "us.anthropic.claude-opus-5-5", "us.anthropic.claude-opus-5-5"),
        ("bedrock_inference", "box_scale", " pixels ", "pixels"),
        ("bedrock_inference", "error_verdict", "fail", "fail"),
        ("mqtt_publish", "qos", "1", 1),
        ("mqtt_publish", "qos", 1, 1),
        ("mqtt_publish", "qos", "5", "5"),
        ("mqtt_publish", "retain", "true", True),
        ("s3_upload", "presign_get_expiry_s", "300", 300),
    ])
    def test_values_are_coerced_to_the_descriptor_type(self, node_type, name, raw, expected):
        descriptor = next(p for p in get_node_type(node_type).parameters if p.name == name)
        value = coerce_parameter_value(descriptor, raw)
        assert value == expected and type(value) is type(expected)

    def test_allow_list_is_exactly_the_design_list(self):
        assert OVERRIDE_ALLOW_LIST == {
            "bedrock_inference": ("model", "region", "enabled", "error_verdict", "attempts",
                                  "attempt_timeout_s", "budget_s", "max_image_dimension",
                                  "max_tokens", "box_scale"),
            "s3_upload": ("presign_get_expiry_s", "timeout_s", "retries"),
            "mqtt_publish": ("qos", "retain"),
        }
        for node_type, names in OVERRIDE_ALLOW_LIST.items():
            declared = {p.name for p in get_node_type(node_type).parameters}
            assert set(names) <= declared, node_type



class TestPoller:
    def test_empty_before_the_first_read_and_snapshot_never_reads(self):
        config, reader = make_config(component_config({WF_A: {"check": {"model": "m"}}}))
        assert config.snapshot() == EMPTY_SNAPSHOT
        assert config.is_disabled(WF_A) is False
        assert config.overrides_for(WF_A) == {}
        assert reader.calls == 0
        config.refresh()
        assert reader.calls == 1
        for _ in range(5):
            config.snapshot(); config.is_disabled(WF_A); config.overrides_for(WF_A)
        assert reader.calls == 1

    def test_refresh_parses_both_keys(self, caplog):
        config, _ = make_config(component_config(
            {"*": {"publish": {"qos": "1"}}, WF_A: {"check": {"enabled": "false"}}},
            {WF_B: "true", WF_A: "false"}))
        with caplog.at_level(logging.INFO, logger=LOGGER):
            snapshot = config.refresh()
        assert snapshot.node_overrides == {
            "*": {"publish": {"qos": "1"}}, WF_A: {"check": {"enabled": "false"}}}
        assert snapshot.disabled == frozenset({WF_B})
        assert config.is_disabled(WF_B) and not config.is_disabled(WF_A)
        assert config.is_disabled(None) is False
        assert config.overrides_for(WF_A) == {"publish": {"qos": "1"}, "check": {"enabled": "false"}}
        assert config.overrides_for(WF_B) == {"publish": {"qos": "1"}}
        assert config.has_overrides("other") is True  # via the "*" entry
        assert any("configuration overrides changed" in r.getMessage() for r in caplog.records)

    def test_star_disables_everything_and_exact_id_wins_per_parameter(self):
        config, _ = make_config(component_config(
            {"*": {"check": {"model": "m-star", "attempts": "1"}}, WF_A: {"check": {"model": "m-a"}}},
            {"*": "true"}))
        config.refresh()
        assert config.is_disabled(WF_A) and config.is_disabled("anything")
        assert config.overrides_for(WF_A) == {"check": {"model": "m-a", "attempts": "1"}}
        assert config.overrides_for(WF_B) == {"check": {"model": "m-star", "attempts": "1"}}

    def test_failing_reads_keep_the_last_good_snapshot_and_warn_once_a_minute(self, caplog):
        clock = FakeClock()
        good = component_config(disabled={WF_A: "true"})
        config, _ = make_config(good, RuntimeError("ipc down"), clock=clock)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            config.refresh()
            assert config.is_disabled(WF_A)
            for _ in range(3):
                config.refresh()
            assert config.is_disabled(WF_A)
            assert len(warnings_of(caplog)) == 1 and "ipc down" in warnings_of(caplog)[0]
            clock.advance(READ_FAILURE_WARNING_INTERVAL_SECONDS)
            config.refresh()
            assert len(warnings_of(caplog)) == 2

    @pytest.mark.parametrize("bad", [{}, None, "not a map", []])
    def test_an_empty_or_non_map_read_counts_as_a_failure(self, bad, caplog):
        config, _ = make_config(component_config(disabled={WF_A: "true"}), bad)
        config.refresh()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert config.refresh().disabled == frozenset({WF_A})
        assert len(warnings_of(caplog)) == 1

    def test_unchanged_configuration_is_not_reparsed_and_warnings_are_not_repeated(self, caplog):
        bad = component_config({WF_A: {"check": {"prompt": "x"}}}, {WF_A: "yes"})
        config, _ = make_config(bad)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            first = config.refresh()
            second = config.refresh()
        assert first is second
        assert first.disabled == frozenset({WF_A}) and first.node_overrides == {}
        assert len(warnings_of(caplog)) == 2

    def test_start_reads_immediately_and_stop_joins(self):
        config, reader = make_config(component_config(disabled={WF_A: "true"}))
        config._poll_interval = 3600
        config.start()
        try:
            assert reader.calls == 1 and config.is_disabled(WF_A)
            thread = config._poll_thread
            assert thread is not None and thread.daemon and thread.is_alive()
            config.start()  # idempotent for the thread (re-reads once, like McpConfig)
            assert reader.calls == 2
            assert config._poll_thread is thread
        finally:
            config.stop()
        assert config._poll_thread is None and not thread.is_alive()

    def test_default_reader_is_deferred_and_fails_closed_to_an_empty_snapshot(self, caplog):
        config = WorkflowOverridesConfig()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            with patch.dict("sys.modules", {"utils.ipc_client": None}):
                assert config.refresh() == EMPTY_SNAPSHOT
        assert len(warnings_of(caplog)) == 1



def loaded(overrides, clock=None):
    config, _ = make_config(component_config(overrides), clock=clock)
    config.refresh()
    return config


class TestApplyOverrides:
    def test_document_without_overrides_is_returned_as_the_same_object(self):
        config = loaded({WF_B: {"check": {"model": "m"}}})
        before = json.dumps(DOCUMENT, sort_keys=True)
        assert config.apply_overrides(DOCUMENT, WF_A, NODE_TYPES) is DOCUMENT
        assert EMPTY_SNAPSHOT == WorkflowOverridesConfig(read_config=lambda: {}).snapshot()
        assert WorkflowOverridesConfig(read_config=lambda: {}).apply_overrides(
            DOCUMENT, WF_A, NODE_TYPES) is DOCUMENT
        assert json.dumps(DOCUMENT, sort_keys=True) == before

    def test_allow_listed_values_are_coerced_checked_and_written_to_a_copy(self, caplog):
        config = loaded({WF_A: {
            "check": {"model": "us.anthropic.claude-opus-5-5", "enabled": "false",
                      "attempts": "1", "error_verdict": "fail", "budget_s": 11},
            "upload": {"presign_get_expiry_s": "300"},
            "publish": {"qos": "1", "retain": "true"}}})
        before = copy.deepcopy(DOCUMENT)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            result = config.apply_overrides(DOCUMENT, WF_A, NODE_TYPES)
        assert result is not DOCUMENT and DOCUMENT == before
        by_id = {b["nodeId"]: b["parameters"] for b in result["executorBindings"]}
        assert by_id["check"] == {
            "model": "us.anthropic.claude-opus-5-5", "region": "us-east-1", "enabled": False,
            "attempts": 1, "error_verdict": "fail", "budget_s": 11, "prompt": "Check the order."}
        assert by_id["upload"]["presign_get_expiry_s"] == 300
        assert by_id["publish"] == {"topic": "plant/line1/result", "qos": 1, "retain": True}
        assert warnings_of(caplog) == []
        info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert info[-1].startswith("Workflow wf-a run applies 8 configuration override(s): ")
        assert "check.model" in info[-1] and "us.anthropic" not in info[-1]

    def test_ignored_overrides_warn_once_per_minute_and_keep_deployed_values(self, caplog):
        clock = FakeClock()
        config = loaded({WF_A: {
            "check": {"attempts": "9", "qos": 1},        # out of range; not a bedrock parameter
            "upload": {"model": "m"},                      # allow-listed for another type
            "publish": {"qos": "5", "retain": "maybe"},   # invalid enum / bool
            "trigger_1": {"qos": 0},                       # trigger binding: never
            "gate": {"enabled": "false"},                  # executor-level, no allow-list
            "camera": {"retries": 1},                      # GStreamer node: no binding
            "ghost": {"retries": 1},                       # unknown node
        }}, clock=clock)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert config.apply_overrides(DOCUMENT, WF_A, NODE_TYPES) is DOCUMENT
            first = warnings_of(caplog)
            assert config.apply_overrides(DOCUMENT, WF_A, NODE_TYPES) is DOCUMENT
            assert warnings_of(caplog) == first
            clock.advance(IGNORED_OVERRIDE_WARNING_INTERVAL_SECONDS)
            assert config.apply_overrides(DOCUMENT, WF_A, NODE_TYPES) is DOCUMENT
            assert len(warnings_of(caplog)) == 2 * len(first)
        assert len(first) == 9
        joined = "\n".join(first)
        assert "check.attempts = '9' rejected" in joined and "above the maximum 3" in joined
        assert "check.qos is not an overridable parameter of a bedrock_inference node" in joined
        assert "upload.model is not an overridable parameter of a s3_upload node" in joined
        assert "publish.qos = '5' rejected" in joined
        assert "publish.retain = 'maybe' rejected" in joined
        assert "node 'trigger_1' has type 'mqtt_subscribe', which takes no configuration overrides" in joined
        assert "node 'gate' has type 'conditional', which takes no" in joined
        assert "node 'camera' is not an executor-level node" in joined
        assert "node 'ghost' is not an executor-level node" in joined

    def test_binding_kind_types_a_node_missing_from_the_graph(self):
        config = loaded({"*": {"publish": {"qos": 2}}})
        result = config.apply_overrides(DOCUMENT, WF_B, {})
        assert result["executorBindings"][3]["parameters"]["qos"] == 2
        result = config.apply_overrides(DOCUMENT, WF_B, None)
        assert result["executorBindings"][3]["parameters"]["qos"] == 2

    def test_graph_node_types(self):
        assert node_types_from_graph(GRAPH) == NODE_TYPES
        assert node_types_from_graph(None) == {}
        assert node_types_from_graph({"nodes": [{"id": 1, "type": "x"}, "junk"]}) == {}

    def test_malformed_documents_pass_through(self):
        config = loaded({WF_A: {"check": {"model": "m"}}})
        for document in (None, [], {}, {"executorBindings": "x"}, {"executorBindings": ["x"]}):
            assert config.apply_overrides(document, WF_A, NODE_TYPES) is document



class TestTriggerCoreDisabled:
    NODE = "trigger_1"

    def make_core(self, disabled, workflow_id):
        config, _ = make_config(component_config(disabled=disabled))
        config.refresh()
        return WorkflowActivationCore(
            "reg-1", policies=[TriggerPolicy(trigger_node_id=self.NODE, queue_depth=100)],
            run_starter=lambda activation: None,
            workflow_id=workflow_id, overrides_config=config,
        )

    def test_none_workflow_id_is_never_disabled(self):
        core = self.make_core({"*": "true"}, None)
        assert core.is_disabled() is False
        assert core.fire(self.NODE, {"topic": "t", "payload": "{}"}) is True

    def test_exact_id_and_star_discard_at_info(self, caplog):
        for disabled, workflow_id in (({WF_A: "true"}, WF_A), ({"*": "true"}, WF_B)):
            core = self.make_core(disabled, workflow_id)
            with caplog.at_level(logging.INFO, logger="workflow_engine.trigger_runtime"):
                assert core.fire(self.NODE, {"topic": "t", "payload": "{}"}) is False
            assert core.queue.pending_count(self.NODE) == 0
            assert any("disabled by the WorkflowDisabled configuration" in r.getMessage()
                       for r in caplog.records)

    def test_other_ids_and_false_fire(self):
        assert self.make_core({WF_A: "true"}, WF_B).fire(self.NODE, {"payload": "{}"}) is True
        assert self.make_core({WF_A: "false"}, WF_A).fire(self.NODE, {"payload": "{}"}) is True

    def test_core_without_a_config_never_asks(self):
        core = WorkflowActivationCore("reg-1", policies=[TriggerPolicy(trigger_node_id=self.NODE)],
                                      run_starter=lambda activation: None, workflow_id=WF_A)
        assert core.is_disabled() is False
        assert core.fire(self.NODE, {"payload": "{}"}) is True


class TestExecutorApplies:
    @pytest.fixture(autouse=True)
    def no_registry_scan(self):
        with patch.object(gst_plugins, "_scan_registry", return_value=True):
            yield

    def make_executor(self, config):
        return WorkflowExecutor(session_factory=lambda: None,
                                pipeline_manager_factory=lambda: None,
                                overrides_config=config)

    def registration(self, tmp_path, graph=GRAPH):
        with open(os.path.join(str(tmp_path), "workflow.json"), "w", encoding="utf-8") as f:
            json.dump(graph, f)
        return SimpleNamespace(id="reg-1", workflow_id=WF_A, version="3",
                               artifact_path=str(tmp_path))

    def test_override_lands_in_the_run_copy_only(self, tmp_path):
        config = loaded({WF_A: {"check": {"model": "us.anthropic.claude-opus-5-5"}}})
        document = copy.deepcopy(DOCUMENT)
        result = self.make_executor(config)._apply_configuration_overrides(
            document, self.registration(tmp_path))
        assert result is not document and document == DOCUMENT
        assert result["executorBindings"][1]["parameters"]["model"] == "us.anthropic.claude-opus-5-5"
        assert result["executorBindings"][1]["parameters"]["prompt"] == "Check the order."

    def test_without_a_config_or_without_overrides_the_same_object_runs(self, tmp_path):
        document = copy.deepcopy(DOCUMENT)
        registration = self.registration(tmp_path)
        assert self.make_executor(None)._apply_configuration_overrides(
            document, registration) is document
        config = loaded({WF_B: {"check": {"model": "m"}}})
        assert self.make_executor(config)._apply_configuration_overrides(
            document, registration) is document
        assert document == DOCUMENT

    def test_a_config_fault_keeps_the_deployed_document(self, tmp_path, caplog):
        class Broken:
            def has_overrides(self, workflow_id):
                raise RuntimeError("boom")

        document = copy.deepcopy(DOCUMENT)
        with caplog.at_level(logging.ERROR, logger="workflow_engine.pipeline_executor"):
            assert self.make_executor(Broken())._apply_configuration_overrides(
                document, self.registration(tmp_path)) is document
        assert any("Could not apply the configuration overrides" in r.getMessage()
                   for r in caplog.records)
