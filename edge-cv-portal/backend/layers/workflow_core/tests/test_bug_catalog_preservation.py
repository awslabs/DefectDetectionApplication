"""Catalog preservation property tests (bugfix task 2.1).

Bugfix spec: workflow-manager-integration-bugfixes.

**Property 6: Preservation — Behavior unchanged outside every bug condition**
Validates: Requirements 3.1, 3.5

Observation-first methodology: ``catalog_baseline.json`` records the
actual, byte-for-byte serialization (``dataclasses.asdict``) of every
node-type descriptor observed on the UNFIXED catalog. These property
tests pin that baseline so the five fixes cannot silently disturb any
descriptor outside their bug condition. They MUST PASS on the unfixed
code (establishing the baseline to preserve) and MUST keep passing after
the Bug 1 (``llm_inference`` input port ``InferenceMeta`` -> ``VideoFrames``)
and Bug 4 (``llm_inference`` label "LLM Inference" -> "VLM/LLM Inference")
fixes — hence the two mutable ``llm_inference`` fields (its ``in`` port
type and ``display_name``) are deliberately excluded from the preserved
subset while everything else about ``llm_inference`` is pinned.

Bug 2 (workflow-manager-integration-bugfixes) additionally changes the
``mqtt_publish`` descriptor: it adds an off-by-default ``greengrass``
parameter, relaxes ``broker_host`` from ``required=True`` to
``required=False``, and appends the ``python:awsiotsdk`` Greengrass IPC
runtime dependency. ``mqtt_publish`` is therefore a bug-condition
descriptor too, so those three intentionally-changed aspects are excluded
from the byte-identical subset (analogous to ``llm_inference``) while
everything Bug 2 must preserve is pinned below. The behavioral
preservation of the plain-broker and AWS IoT Core publish paths
(validation/packaging/publish) is covered by
``test_mqtt_broker_awsiot_preservation.py``.

Preserved by this property (Requirements 3.1, 3.5):
  * For every node type OTHER than ``llm_inference`` and ``mqtt_publish``:
    ``display_name``, ports (``inputs``/``outputs``), ``parameters``,
    ``mappings``, ``category``, ``type_id`` and ``hardware_dependent`` are
    byte-identical to the observed baseline.
  * For ``llm_inference``: its ``type_id`` (stays ``llm_inference``), its
    ``out`` port type (stays ``InferenceMeta``), its parameters
    (``modelName``, ``prompt_template``, ``max_tokens``, ``temperature``,
    ``top_p``) and its architecture mappings (vLLM-capable archs plus the
    ``sim`` stub ``sim_llm_inference``) are unchanged.
  * For ``mqtt_publish``: its ``type_id``, ``category``, ``display_name``,
    ports, ``hardware_dependent``, every parameter other than the changed
    ``broker_host`` (only its ``required`` flag flips) and the new
    ``greengrass`` parameter, and the retained ``python:paho-mqtt``
    dependency are unchanged.
"""

import dataclasses
import json
import os

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import NODE_CATALOG, get_node_type
from workflow_core.catalog.models import PORT_TYPE_INFERENCE_META


# ---------------------------------------------------------------------------
# Observed baseline (recorded from the UNFIXED catalog — see _gen_baseline.py)
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "catalog_baseline.json"), encoding="utf-8") as _fh:
    BASELINE = json.load(_fh)

#: The type id whose ``in`` port type and ``display_name`` the fixes
#: intentionally change (Bugs 1 and 4). Every other field of it, and every
#: field of every other descriptor (except ``mqtt_publish``), is preserved.
_MUTATED_TYPE_ID = "llm_inference"

#: The type id Bug 2 intentionally changes (adds ``greengrass``, relaxes
#: ``broker_host`` required, appends ``python:awsiotsdk``). Its other
#: fields are pinned by :class:`TestMqttPublishPreservation` below.
_MQTT_TYPE_ID = "mqtt_publish"

#: The ``mqtt_publish`` parameters Bug 2 intentionally changes: the new
#: ``greengrass`` option, and ``broker_host`` (only its ``required`` flag
#: flips True -> False; all other broker_host fields are preserved).
_MQTT_ADDED_PARAMETER = "greengrass"
_MQTT_RELAXED_PARAMETER = "broker_host"

#: Parameters of ``llm_inference`` that must survive the label/input fixes
#: unchanged (Requirement 3.1).
_LLM_PRESERVED_PARAMETERS = (
    "modelName",
    "prompt_template",
    "max_tokens",
    "temperature",
    "top_p",
)


def _asdict(descriptor):
    """The descriptor's canonical, JSON-comparable serialization — the same
    shape recorded in the baseline snapshot."""
    return dataclasses.asdict(descriptor)


# The other descriptors are pinned byte-for-byte; sample over them.
# ``llm_inference`` (Bugs 1/4) and ``mqtt_publish`` (Bug 2) are the two
# bug-condition descriptors and are pinned by their own scoped tests.
_OTHER_DESCRIPTORS = tuple(
    d for d in NODE_CATALOG
    if d.type_id not in (_MUTATED_TYPE_ID, _MQTT_TYPE_ID)
)
_other_descriptor = st.sampled_from(_OTHER_DESCRIPTORS)


# ---------------------------------------------------------------------------
# Property 6 — every non-llm_inference descriptor is byte-identical
# ---------------------------------------------------------------------------

class TestCatalogPreservation:
    """Property 6: Preservation — descriptors unchanged outside the bug conditions."""

    def test_baseline_covers_every_catalog_type(self):
        # Sanity: the recorded baseline and the live catalog describe the
        # same set of node types (nothing added or dropped).
        assert set(BASELINE) == {d.type_id for d in NODE_CATALOG}

    @settings(max_examples=50)
    @given(descriptor=_other_descriptor)
    def test_non_llm_descriptor_is_byte_identical_to_baseline(self, descriptor):
        """For any node type other than ``llm_inference`` and
        ``mqtt_publish``, the full descriptor — display_name, ports,
        parameters, mappings, category, type_id, hardware_dependent —
        equals the observed baseline exactly (Requirements 3.1, 3.5).
        """
        assert descriptor.type_id not in (_MUTATED_TYPE_ID, _MQTT_TYPE_ID)
        assert _asdict(descriptor) == BASELINE[descriptor.type_id]

    def test_every_non_llm_descriptor_is_byte_identical_exhaustively(self):
        # Belt-and-suspenders over the finite catalog: assert every
        # non-bug-condition descriptor at once so no type escapes the
        # sampled run (llm_inference and mqtt_publish are scoped below).
        for descriptor in _OTHER_DESCRIPTORS:
            assert _asdict(descriptor) == BASELINE[descriptor.type_id], \
                descriptor.type_id


# ---------------------------------------------------------------------------
# Property 6 — llm_inference: everything but the mutated fields is preserved
# ---------------------------------------------------------------------------

class TestLlmInferencePreservation:
    """Property 6: Preservation — ``llm_inference`` fields outside Bugs 1/4."""

    def test_type_id_unchanged(self):
        descriptor = get_node_type(_MUTATED_TYPE_ID)
        assert descriptor is not None
        assert descriptor.type_id == "llm_inference"
        assert BASELINE[_MUTATED_TYPE_ID]["type_id"] == "llm_inference"

    def test_out_port_stays_inference_meta(self):
        descriptor = get_node_type(_MUTATED_TYPE_ID)
        outputs = _asdict(descriptor)["outputs"]
        assert outputs == BASELINE[_MUTATED_TYPE_ID]["outputs"]
        assert outputs == [{"name": "out", "port_type": PORT_TYPE_INFERENCE_META}]

    def test_parameters_unchanged(self):
        # The full parameter list (names, types, defaults, constraints,
        # descriptions, examples) is byte-identical to the baseline.
        descriptor = get_node_type(_MUTATED_TYPE_ID)
        parameters = _asdict(descriptor)["parameters"]
        assert parameters == BASELINE[_MUTATED_TYPE_ID]["parameters"]
        assert tuple(p["name"] for p in parameters) == _LLM_PRESERVED_PARAMETERS

    def test_architecture_mappings_unchanged(self):
        # vLLM-capable archs plus the sim stub sim_llm_inference, unchanged.
        descriptor = get_node_type(_MUTATED_TYPE_ID)
        mappings = _asdict(descriptor)["mappings"]
        assert mappings == BASELINE[_MUTATED_TYPE_ID]["mappings"]
        sim = [m for m in mappings if m["arch"] == "sim"]
        assert sim == [{"arch": "sim", "element_chain": [],
                        "executor_binding": "sim_llm_inference",
                        "plugin_dependencies": []}]

    def test_category_and_hardware_dependence_unchanged(self):
        descriptor = get_node_type(_MUTATED_TYPE_ID)
        current = _asdict(descriptor)
        baseline = BASELINE[_MUTATED_TYPE_ID]
        assert current["category"] == baseline["category"]
        assert current["hardware_dependent"] == baseline["hardware_dependent"]

    @settings(max_examples=25)
    @given(param_name=st.sampled_from(_LLM_PRESERVED_PARAMETERS))
    def test_each_preserved_parameter_matches_baseline(self, param_name):
        """For each of the five parameters that must survive the Bug 1/4
        fixes, its full descriptor is byte-identical to the baseline
        (Requirement 3.1)."""
        descriptor = get_node_type(_MUTATED_TYPE_ID)
        current = {p["name"]: p for p in _asdict(descriptor)["parameters"]}
        baseline = {p["name"]: p for p in BASELINE[_MUTATED_TYPE_ID]["parameters"]}
        assert current[param_name] == baseline[param_name]


# ---------------------------------------------------------------------------
# Property 6 — mqtt_publish: everything but the Bug 2 changes is preserved
# ---------------------------------------------------------------------------

class TestMqttPublishPreservation:
    """Property 6: Preservation — ``mqtt_publish`` fields outside Bug 2.

    Bug 2 adds ``greengrass``, relaxes ``broker_host`` from
    ``required=True`` to ``required=False``, and appends
    ``python:awsiotsdk`` to the device mappings' plugin dependencies. Every
    other aspect of the descriptor is pinned to the recorded baseline here;
    the behavioral preservation of the plain-broker and AWS IoT Core paths
    lives in ``test_mqtt_broker_awsiot_preservation.py``.
    """

    def test_identity_ports_and_hardware_dependence_unchanged(self):
        descriptor = get_node_type(_MQTT_TYPE_ID)
        current = _asdict(descriptor)
        baseline = BASELINE[_MQTT_TYPE_ID]
        assert current["type_id"] == baseline["type_id"] == "mqtt_publish"
        assert current["category"] == baseline["category"]
        assert current["display_name"] == baseline["display_name"]
        assert current["inputs"] == baseline["inputs"]
        assert current["outputs"] == baseline["outputs"]
        assert current["hardware_dependent"] == baseline["hardware_dependent"]

    def test_unchanged_parameters_are_byte_identical_to_baseline(self):
        # Every parameter except the new greengrass option and the relaxed
        # broker_host is byte-identical to the baseline (topic, broker_port,
        # payload_template, qos, aws_iot, and every iot_* certificate path).
        descriptor = get_node_type(_MQTT_TYPE_ID)
        current = {p["name"]: p for p in _asdict(descriptor)["parameters"]}
        baseline = {p["name"]: p for p in BASELINE[_MQTT_TYPE_ID]["parameters"]}
        for name, baseline_param in baseline.items():
            if name in (_MQTT_ADDED_PARAMETER, _MQTT_RELAXED_PARAMETER):
                continue
            assert name in current, "mqtt_publish lost parameter %r" % name
            assert current[name] == baseline_param, \
                "mqtt_publish parameter %r drifted from baseline" % name

    def test_broker_host_changed_only_in_required_flag(self):
        # broker_host is relaxed to optional; every other field of it
        # (description, examples, constraints, default, param_type) is
        # unchanged from baseline.
        descriptor = get_node_type(_MQTT_TYPE_ID)
        current = next(p for p in _asdict(descriptor)["parameters"]
                       if p["name"] == _MQTT_RELAXED_PARAMETER)
        baseline = next(p for p in BASELINE[_MQTT_TYPE_ID]["parameters"]
                        if p["name"] == _MQTT_RELAXED_PARAMETER)
        assert current["required"] is False
        assert baseline["required"] is True
        assert {k: v for k, v in current.items() if k != "required"} == \
               {k: v for k, v in baseline.items() if k != "required"}

    def test_paho_mqtt_dependency_still_present(self):
        # The paho-mqtt dependency that serves the plain-broker and aws_iot
        # paths is retained on every device mapping (Bug 2 only appends
        # awsiotsdk alongside it).
        descriptor = get_node_type(_MQTT_TYPE_ID)
        for mapping in descriptor.mappings:
            if mapping.executor_binding == "mqtt_publish":
                assert "python:paho-mqtt" in mapping.plugin_dependencies
