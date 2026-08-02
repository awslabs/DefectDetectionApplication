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
"""Type-coercion tests for the opcua_write output node.

A ``value_template`` renders the value's native Python type (e.g.
``"{is_anomalous}"`` -> the int ``1``), but the target OPC UA node may be a
Boolean / numeric / string tag. Writing a mismatched Python type makes the
server reject the write with ``BadTypeMismatch`` (observed on-device: Int64 ->
Boolean ``DefectDetected`` tag). ``_default_opcua_writer`` now reads the node's
declared variant type and coerces the value to it before writing.

These tests inject a self-contained fake ``opcua`` module (Client / ua /
VariantType / DataValue / Variant) so they run without the real package.
"""
import sys
import types

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.output_bindings import (
    _default_opcua_writer,
    _opcua_coerce,
    _opcua_security_from_params,
)


# ---------------------------------------------------------------------------
# Fake ``opcua`` module (Client / ua) injected at the import boundary.
# ---------------------------------------------------------------------------


class _FakeVariantType:
    """String-valued stand-ins for ua.VariantType members (identity by value)."""
    Boolean = "Boolean"
    SByte = "SByte"
    Byte = "Byte"
    Int16 = "Int16"
    UInt16 = "UInt16"
    Int32 = "Int32"
    UInt32 = "UInt32"
    Int64 = "Int64"
    UInt64 = "UInt64"
    Float = "Float"
    Double = "Double"
    String = "String"
    DateTime = "DateTime"  # an intentionally-unhandled type


class _FakeVariant:
    def __init__(self, value, variant_type):
        self.Value = value
        self.VariantType = variant_type


class _FakeDataValue:
    def __init__(self, variant):
        self.Value = variant


def _make_fake_opcua(node):
    ua = types.SimpleNamespace(
        VariantType=_FakeVariantType,
        Variant=_FakeVariant,
        DataValue=_FakeDataValue,
    )

    class _FakeClient:
        def __init__(self, endpoint):
            self.endpoint = endpoint
            node.events.append(("client", endpoint))

        def set_user(self, username):
            node.events.append(("set_user", username))

        def set_password(self, password):
            node.events.append(("set_password", password))

        def set_security_string(self, string):
            node.events.append(("set_security_string", string))

        def connect(self):
            node.events.append(("connect",))

        def get_node(self, node_id):
            node.node_id = node_id
            return node

        def disconnect(self):
            node.events.append(("disconnect",))

    module = types.ModuleType("opcua")
    module.Client = _FakeClient
    module.ua = ua
    return module


class _FakeNode:
    def __init__(self, variant_type):
        self._variant_type = variant_type
        self.written = []
        self.events = []
        self.node_id = None

    def get_data_type_as_variant_type(self):
        return self._variant_type

    def set_value(self, value):
        self.written.append(value)


def _write(node, value, security=None):
    module = _make_fake_opcua(node)
    with pytest.MonkeyPatch.context() as m:
        m.setitem(sys.modules, "opcua", module)
        _default_opcua_writer("opc.tcp://host:4840/", "ns=2;i=7", value, security)


# ---------------------------------------------------------------------------
# _opcua_coerce unit behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (1, True), (0, False), ("1", True), ("0", False),
    ("true", True), ("false", False), (True, True), (False, False),
])
def test_coerce_boolean(value, expected):
    with pytest.MonkeyPatch.context() as m:
        m.setitem(sys.modules, "opcua", _make_fake_opcua(_FakeNode("Boolean")))
        assert _opcua_coerce(value, _FakeVariantType.Boolean) is expected


@pytest.mark.parametrize("vt", [
    _FakeVariantType.Int16, _FakeVariantType.Int32, _FakeVariantType.Int64,
    _FakeVariantType.Byte, _FakeVariantType.UInt32,
])
def test_coerce_integer(vt):
    with pytest.MonkeyPatch.context() as m:
        m.setitem(sys.modules, "opcua", _make_fake_opcua(_FakeNode(vt)))
        assert _opcua_coerce("1", vt) == 1
        assert isinstance(_opcua_coerce("1", vt), int)


def test_coerce_float():
    with pytest.MonkeyPatch.context() as m:
        m.setitem(sys.modules, "opcua", _make_fake_opcua(_FakeNode("Double")))
        assert _opcua_coerce("0.93626", _FakeVariantType.Double) == pytest.approx(0.93626)


def test_coerce_string():
    with pytest.MonkeyPatch.context() as m:
        m.setitem(sys.modules, "opcua", _make_fake_opcua(_FakeNode("String")))
        assert _opcua_coerce(1, _FakeVariantType.String) == "1"


def test_coerce_unknown_type_passes_through():
    with pytest.MonkeyPatch.context() as m:
        m.setitem(sys.modules, "opcua", _make_fake_opcua(_FakeNode("DateTime")))
        sentinel = object()
        assert _opcua_coerce(sentinel, _FakeVariantType.DateTime) is sentinel


# ---------------------------------------------------------------------------
# _default_opcua_writer end-to-end coercion (the on-device bug)
# ---------------------------------------------------------------------------


def test_writer_coerces_int_to_boolean_node():
    """The exact on-device case: is_anomalous=1 written to a Boolean node
    is coerced to True and written as a Boolean-typed Variant."""
    node = _FakeNode(_FakeVariantType.Boolean)
    _write(node, 1)

    assert len(node.written) == 1
    datavalue = node.written[0]
    # Written as a DataValue(Variant(True, Boolean)) — not the raw int 1.
    assert datavalue.Value.Value is True
    assert datavalue.Value.VariantType == _FakeVariantType.Boolean
    # Connected and disconnected around the write.
    assert ("connect",) in node.events and ("disconnect",) in node.events


def test_writer_falls_back_to_native_when_type_unreadable():
    """If the node's data type can't be read, the writer still writes the
    raw value (prior behavior) rather than failing."""
    class _NoTypeNode(_FakeNode):
        def get_data_type_as_variant_type(self):
            raise RuntimeError("cannot read data type")

    node = _NoTypeNode(_FakeVariantType.Boolean)
    _write(node, True)

    # Raw value written directly (not wrapped in a DataValue).
    assert node.written == [True]


# ---------------------------------------------------------------------------
# Authentication / security (username/password and certificate)
# ---------------------------------------------------------------------------


def test_security_from_params_none_when_absent():
    assert _opcua_security_from_params({"endpoint": "x", "node_id": "y"}) is None
    # Empty strings are treated as unset.
    assert _opcua_security_from_params({"username": "", "password": ""}) is None


def test_security_from_params_collects_configured():
    params = {
        "endpoint": "x", "node_id": "y", "value_template": "{is_anomalous}",
        "username": "op", "password": "pw",
        "security_policy": "Basic256Sha256", "security_mode": "SignAndEncrypt",
        "client_cert_path": "/c/cert.der", "client_key_path": "/c/key.pem",
    }
    sec = _opcua_security_from_params(params)
    assert sec == {
        "username": "op", "password": "pw",
        "security_policy": "Basic256Sha256", "security_mode": "SignAndEncrypt",
        "client_cert_path": "/c/cert.der", "client_key_path": "/c/key.pem",
    }


def test_writer_applies_username_password():
    node = _FakeNode(_FakeVariantType.Boolean)
    _write(node, 1, security={"username": "operator", "password": "s3cret"})
    assert ("set_user", "operator") in node.events
    assert ("set_password", "s3cret") in node.events
    # Auth is applied before connect.
    assert node.events.index(("set_user", "operator")) < node.events.index(("connect",))


def test_writer_applies_certificate_security_string():
    node = _FakeNode(_FakeVariantType.Boolean)
    _write(node, 1, security={
        "security_policy": "Basic256Sha256",
        "security_mode": "SignAndEncrypt",
        "client_cert_path": "/c/cert.der",
        "client_key_path": "/c/key.pem",
    })
    assert ("set_security_string",
            "Basic256Sha256,SignAndEncrypt,/c/cert.der,/c/key.pem") in node.events


def test_writer_certificate_defaults_mode_and_appends_server_cert():
    node = _FakeNode(_FakeVariantType.Boolean)
    _write(node, 1, security={
        "security_policy": "Basic256Sha256",
        "client_cert_path": "/c/cert.der",
        "client_key_path": "/c/key.pem",
        "server_cert_path": "/c/server.der",
    })
    # No mode given -> defaults to SignAndEncrypt; server cert appended.
    assert ("set_security_string",
            "Basic256Sha256,SignAndEncrypt,/c/cert.der,/c/key.pem,/c/server.der") \
        in node.events


def test_writer_anonymous_when_no_security():
    node = _FakeNode(_FakeVariantType.Boolean)
    _write(node, 1, security=None)
    kinds = {e[0] for e in node.events}
    assert "set_user" not in kinds
    assert "set_security_string" not in kinds
