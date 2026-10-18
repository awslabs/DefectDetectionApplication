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
"""The public surface of the four shared device modules
(workflow-telemetry-dashboards design §5.2; tasks.md 1.6, decision Q1).

``hooks.py``, ``publisher.py``, ``control_shadow.py`` and ``registry_view.py``
are defined once, in the dashboards design, and used unchanged by both the
Telemetry_Agent and the Debug_Agent. This test pins their public symbol sets
(``__all__``, the module-level public names and the public methods of their
classes) to the §5.2 lists as the debug plan built them, and the signature of
``output_bindings.publish_via_ipc`` with its keyword-only ``retain=False``,
so that an addition made for either agent shows up as a failing assertion
here and is reviewed deliberately.

Validates: R5.4, R19.4, R20.3.
"""
import inspect
import types

import awsiot_host_stubs  # noqa: F401 - output_bindings imports utils.ipc_client
from workflow_engine import output_bindings
from workflow_engine.telemetry import control_shadow, hooks, publisher, registry_view

#: §5.2: the names each shared module exports.
EXPECTED_ALL = {
    hooks: [
        "RunRecord", "CURRENT_EXECUTION",
        "register_run_observer", "notify_run_finished",
        "register_drop_observer", "notify_trigger_dropped",
        "register_continuous_observer", "notify_continuous_tick",
        "register_model_observer", "notify_model_readiness", "notify_model_wait",
    ],
    publisher: ["IpcPublisher"],
    control_shadow: ["ShadowControlAgent"],
    registry_view: ["RegistrationInfo", "RegistrationView", "get_or_create"],
}

#: Every public (non-underscore) module-level name that is not an imported
#: module: ``__all__`` plus the tuning constants, the ``logger`` and the
#: typing names the module imports. A new public function or class fails here.
EXPECTED_PUBLIC_NAMES = {
    hooks: {
        "Any", "Callable", "Dict", "List", "Optional", "Tuple", "dataclass", "logger",
        "RunRecord", "CURRENT_EXECUTION",
        "register_run_observer", "notify_run_finished",
        "register_drop_observer", "notify_trigger_dropped",
        "register_continuous_observer", "notify_continuous_tick",
        "register_model_observer", "notify_model_readiness", "notify_model_wait",
    },
    publisher: {
        "Any", "Deque", "Dict", "Optional", "Tuple", "logger",
        "IpcPublisher", "classify_failure",
        "REASON_DENIED", "REASON_SPOOL_FULL", "REASON_OTHER",
    },
    control_shadow: {
        "Any", "Callable", "Dict", "Optional", "Sequence", "logger",
        "ShadowControlAgent",
        "DEFAULT_COUNTER_KEYS", "GET_IN_FLIGHT_S", "REPORT_INTERVAL_S", "REPORT_MAX_BYTES",
    },
    registry_view: {
        "Any", "Callable", "Dict", "FrozenSet", "List", "Optional", "Tuple", "dataclass", "logger",
        "RegistrationInfo", "RegistrationView", "get_or_create",
        "FALLBACK_POLL_S",
    },
}

#: The public methods and properties of the shared classes (§5.2).
EXPECTED_CLASS_SURFACE = {
    hooks.RunRecord: {"from_execution"},
    publisher.IpcPublisher: {"publish", "publish_message", "pending", "status", "close"},
    control_shadow.ShadowControlAgent: {"start", "close", "on_active", "handle_message", "report", "last_version"},
    registry_view.RegistrationInfo: set(),
    registry_view.RegistrationView: {"start", "stop", "snapshot", "add_listener", "remove_listener", "refresh"},
}

#: §5.2 ``RunRecord``: every field, in order.
EXPECTED_RUN_RECORD_FIELDS = [
    "registration_id", "workflow_id", "workflow_version", "execution_id", "status", "error",
    "failing_node_id", "traceback", "capture_id", "output_dir", "log_path", "enqueued_at_s", "accepted_ms",
    "run_started_ms", "finished_ms", "queued_ms", "setup_ms", "readiness_wait_ms", "trigger_context",
    "node_status", "tag_values", "detections", "detection_count", "continuous",
]


def _public_names(module):
    return {
        name for name, value in vars(module).items()
        if not name.startswith("_") and not isinstance(value, types.ModuleType)
    }


def _public_members(cls):
    return {name for name in vars(cls) if not name.startswith("_")}


def test_all_lists_equal_the_design():
    for module, expected in EXPECTED_ALL.items():
        assert list(module.__all__) == expected, module.__name__


def test_public_module_names_equal_the_design():
    for module, expected in EXPECTED_PUBLIC_NAMES.items():
        assert _public_names(module) == expected, module.__name__


def test_public_class_members_equal_the_design():
    for cls, expected in EXPECTED_CLASS_SURFACE.items():
        assert _public_members(cls) == expected, cls.__name__


def test_run_record_fields_and_frozenness():
    fields = hooks.RunRecord.__dataclass_fields__
    assert list(fields) == EXPECTED_RUN_RECORD_FIELDS
    assert hooks.RunRecord.__dataclass_params__.frozen is True
    assert registry_view.RegistrationInfo.__dataclass_params__.frozen is True
    assert list(registry_view.RegistrationInfo.__dataclass_fields__) == ["workflow_id", "version", "models",
                                                                          "has_triton"]


def test_hook_signatures():
    assert list(inspect.signature(hooks.notify_run_finished).parameters) == ["record"]
    assert list(inspect.signature(hooks.notify_trigger_dropped).parameters) == ["registration_id", "node_id",
                                                                                "reason"]
    assert list(inspect.signature(hooks.notify_continuous_tick).parameters) == ["registration_id", "key", "amount"]
    assert list(inspect.signature(hooks.notify_model_readiness).parameters) == [
        "registration_id", "execution_id", "model", "state", "waited_ms", "ready"]
    assert list(inspect.signature(hooks.notify_model_wait).parameters) == ["registration_id", "wait_document"]
    for register in (hooks.register_run_observer, hooks.register_drop_observer,
                     hooks.register_continuous_observer, hooks.register_model_observer):
        assert list(inspect.signature(register).parameters) == ["observer"]


def test_publisher_and_shadow_and_view_constructor_signatures():
    assert list(inspect.signature(publisher.IpcPublisher.__init__).parameters) == ["self", "name", "maxlen", "clock"]
    assert inspect.signature(publisher.IpcPublisher.__init__).parameters["maxlen"].default == 1000
    assert list(inspect.signature(publisher.IpcPublisher.publish).parameters) == ["self", "topic", "payload", "qos"]
    assert list(inspect.signature(publisher.IpcPublisher.publish_message).parameters) == ["self", "topic",
                                                                                           "payload"]
    shadow_params = inspect.signature(control_shadow.ShadowControlAgent.__init__).parameters
    assert list(shadow_params)[:5] == ["self", "thing_name", "shadow_name", "on_desired", "publisher"]
    assert all(shadow_params[name].kind is inspect.Parameter.KEYWORD_ONLY
               for name in ("subscription_factory", "clock", "counter_keys"))
    assert list(inspect.signature(control_shadow.ShadowControlAgent.report).parameters) == ["self", "state"]
    view_params = inspect.signature(registry_view.RegistrationView.__init__).parameters
    assert list(view_params)[:4] == ["self", "session_factory", "watcher_provider", "document_reader"]
    assert list(inspect.signature(registry_view.get_or_create).parameters) == []


def test_publish_via_ipc_signature_has_the_keyword_only_retain_false():
    signature = inspect.signature(output_bindings.publish_via_ipc)
    parameters = signature.parameters
    assert list(parameters) == ["topic", "payload", "qos", "retain"]
    assert parameters["payload"].annotation is bytes
    retain = parameters["retain"]
    assert retain.kind is inspect.Parameter.KEYWORD_ONLY and retain.default is False
    assert retain.annotation is bool
    # the original publisher keeps its signature (str payload, keyword-only retain)
    original = inspect.signature(output_bindings._default_greengrass_publisher).parameters
    assert list(original) == ["topic", "payload", "qos", "retain"]
    assert original["payload"].annotation is str
    assert original["retain"].kind is inspect.Parameter.KEYWORD_ONLY and original["retain"].default is False
