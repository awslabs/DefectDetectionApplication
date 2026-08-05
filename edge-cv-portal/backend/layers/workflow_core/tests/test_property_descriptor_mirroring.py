# Feature: trigger-activation-runtime, Property 1: Descriptor mirroring
"""Property test P1 — the subscribe-trigger descriptors mirror their
publish/write counterparts and each other.

For every connection parameter name shared with ``mqtt_publish``, the
``mqtt_subscribe`` descriptor's ``ParameterDescriptor`` equals the
``mqtt_publish`` counterpart field-for-field; for every
endpoint/security parameter name shared with ``opcua_write``, the
``opcua_subscribe`` descriptor's ``ParameterDescriptor`` equals the
``opcua_write`` counterpart field-for-field; and for every
policy-family parameter name, the two trigger descriptors'
declarations are equal to each other.

Equality scope (established by reading the live descriptors):

- ``topic`` is the sole deliberately re-worded parameter — its
  ``description`` (topic filter vs publish target) and ``examples``
  legitimately differ, so it is compared on every other field
  (name / param_type / required / default / constraints / depends_on).
- Every other mirrored parameter (including ``greengrass`` and
  ``aws_iot``, whose descriptions kept the publish wording verbatim)
  is compared with full dataclass equality — descriptions and
  examples included — so any future wording drift is caught.

**Validates: Requirements 1.2, 2.2, 2.3, 2.5**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import get_node_type

# ---------------------------------------------------------------------------
# Mirrored parameter names per requirement group
# ---------------------------------------------------------------------------

#: mqtt_subscribe vs mqtt_publish shared connection parameters (Req 1.2).
_MQTT_CONNECTION_PARAMS = (
    "topic",
    "qos",
    "greengrass",
    "aws_iot",
    "iot_thing_name",
    "iot_ca_cert_path",
    "iot_client_cert_path",
    "iot_private_key_path",
    "broker_host",
    "broker_port",
)

#: opcua_subscribe vs opcua_write shared endpoint/security parameters
#: (Reqs 2.2, 2.3).
_OPCUA_MIRRORED_PARAMS = (
    "endpoint",
    "node_id",
    "username",
    "password",
    "security_policy",
    "security_mode",
    "client_cert_path",
    "client_key_path",
    "server_cert_path",
)

#: The shared policy family, equal across the two trigger descriptors
#: (Reqs 1.3, 1.4 via 2.5).
_POLICY_FAMILY_PARAMS = (
    "concurrency_policy",
    "queue_depth",
    "debounce_ms",
    "retry_limit",
    "priority",
)

#: Parameters whose description/examples were deliberately re-worded for
#: the subscribe side; compared on all *other* fields.
_REWORDED = {"topic"}

#: The field-for-field comparison set used when descriptions legitimately
#: differ (everything except description and examples).
_CORE_FIELDS = ("name", "param_type", "required", "default",
                "constraints", "depends_on")

#: (mirror descriptor, counterpart descriptor, parameter name) cases.
_CASES = (
    [("mqtt_subscribe", "mqtt_publish", n) for n in _MQTT_CONNECTION_PARAMS]
    + [("opcua_subscribe", "opcua_write", n) for n in _OPCUA_MIRRORED_PARAMS]
    + [("mqtt_subscribe", "opcua_subscribe", n) for n in _POLICY_FAMILY_PARAMS]
)


def _param(type_id, name):
    descriptor = get_node_type(type_id)
    assert descriptor is not None, "unknown node type: {0}".format(type_id)
    by_name = {p.name: p for p in descriptor.parameters}
    assert name in by_name, (
        "{0} is missing mirrored parameter '{1}'".format(type_id, name))
    return by_name[name]


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(case=st.sampled_from(_CASES))
def test_descriptor_mirroring(case):
    """**Feature: trigger-activation-runtime, Property 1: Descriptor
    mirroring**

    **Validates: Requirements 1.2, 2.2, 2.3, 2.5**
    """
    mirror_type, counterpart_type, name = case
    mirror = _param(mirror_type, name)
    counterpart = _param(counterpart_type, name)

    if name in _REWORDED:
        # topic: description/examples re-worded for subscribe; every
        # other field must match field-for-field.
        for field_name in _CORE_FIELDS:
            assert getattr(mirror, field_name) == \
                getattr(counterpart, field_name), (
                    "{0}.{1}.{2} != {3}.{1}.{2}".format(
                        mirror_type, name, field_name, counterpart_type))
    else:
        # All other mirrored parameters are verbatim copies: full
        # dataclass equality (descriptions and examples included).
        assert mirror == counterpart, (
            "{0}.{1} != {2}.{1}".format(mirror_type, name, counterpart_type))
