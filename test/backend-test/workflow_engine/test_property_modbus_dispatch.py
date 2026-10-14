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
"""Property-based tests for the modbus_write output binding dispatch
(modbus-tcp-output feature, Properties 4-6, 8, and 9).

Properties 4 and 5 exercise ``OutputBindingProcessor._run_modbus_write``
through the injected ``modbus_writer`` constructor seam (a recording
fake — no sockets, no PLC). Property 6 exercises the production
``_default_modbus_writer`` with ``modbus_tcp.write_single`` and
``time.sleep`` patched, so the coil pulse write sequence is observable
without any network activity. Property 8 mixes a raising modbus binding
with the other output binding kinds to check per-binding containment,
and Property 9 checks the sent-message detail carries every field.
"""
import string

from hypothesis import given, settings
from hypothesis import strategies as st
from unittest.mock import patch

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine import modbus_tcp
from workflow_engine import output_bindings
from workflow_engine.output_bindings import (
    DEFAULT_MODBUS_PORT,
    OutputBindingError,
    OutputBindingProcessor,
    REGISTER_TYPE_COIL,
    REGISTER_TYPE_HOLDING_REGISTER,
    _default_modbus_writer,
)


# ---------------------------------------------------------------------------
# Harness (mirrors test_workflow_output_bindings.py)
# ---------------------------------------------------------------------------


def binding(node_id, kind, parameters=None, upstream=(), downstream=()):
    return {
        "nodeId": node_id,
        "binding": kind,
        "parameters": dict(parameters or {}),
        "upstreamNodeIds": list(upstream),
        "downstreamNodeIds": list(downstream),
    }


def document(*bindings):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": "aarch64-jp5",
        "segments": [],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


class Recorder:
    """Injectable modbus_writer fake recording every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_hosts = st.text(
    alphabet=string.ascii_lowercase + string.digits + ".-",
    min_size=1,
    max_size=24,
)
_node_ids = st.text(
    alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=12
)
#: None means "parameter omitted" — the runner applies the catalog default.
_ports = st.one_of(st.none(), st.integers(min_value=1, max_value=65535))
_unit_ids = st.one_of(st.none(), st.integers(min_value=0, max_value=255))
_addresses = st.integers(min_value=0, max_value=0xFFFF)
_pulse_ms = st.integers(min_value=0, max_value=60000)

#: (metadata value, expected coerced boolean) pairs matching the shared
#: ``_coerce`` normalization: 'true'/'false' strings (any case, padded),
#: numeric strings, ints, and native booleans.
_coil_values = st.one_of(
    st.sampled_from(
        [(True, True), ("true", True), ("True", True), (" true ", True),
         (1, True), ("1", True)]
    ),
    st.sampled_from(
        [(False, False), ("false", False), ("False", False),
         (0, False), ("0", False)]
    ),
)

#: (metadata value, expected coerced int) pairs for in-range holding
#: register writes: the native int or its decimal-string form.
_register_values = st.integers(min_value=0, max_value=0xFFFF).flatmap(
    lambda n: st.sampled_from([(n, n), (str(n), n)])
)

#: Rendered values NOT coercible to an integer in 0-65535: out-of-range
#: integers (either side) and non-numeric strings ("true"/"false" excluded —
#: _coerce maps those to booleans, which int() accepts).
_uncoercible_values = st.one_of(
    st.integers(min_value=-(10 ** 9), max_value=-1),
    st.integers(min_value=0x10000, max_value=10 ** 9),
    st.text(alphabet=string.ascii_letters, min_size=1, max_size=12).filter(
        lambda s: s.strip().lower() not in ("true", "false")
    ),
)


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------


# Feature: modbus-tcp-output, Property 4: Write dispatch carries the
# configured target and the coerced value
class TestProperty4WriteDispatchTargetAndCoercedValue:
    """# Feature: modbus-tcp-output, Property 4: Write dispatch carries
    the configured target and the coerced value

    For any modbus_write binding parameters and any inference metadata
    whose rendered value_template is coercible, processing calls the
    injected writer exactly once with the configured host, port, unit
    id, and address, and with the rendered value coerced per the
    register type — boolean for coil, integer for holding_register.

    **Validates: Requirements 4.1, 4.2, 4.3**
    """

    @settings(max_examples=100, deadline=None)
    @given(
        host=_hosts,
        port=_ports,
        unit_id=_unit_ids,
        address=_addresses,
        pulse_ms=_pulse_ms,
        coil_value=_coil_values,
        omit_template=st.booleans(),
    )
    def test_coil_dispatch_carries_target_and_boolean(
        self, host, port, unit_id, address, pulse_ms, coil_value,
        omit_template,
    ):
        metadata_value, expected = coil_value
        parameters = {
            "host": host,
            "register_type": REGISTER_TYPE_COIL,
            "address": address,
            "pulse_ms": pulse_ms,
        }
        if not omit_template:
            # The explicit form of the catalog default.
            parameters["value_template"] = "{is_anomalous}"
        if port is not None:
            parameters["port"] = port
        if unit_id is not None:
            parameters["unit_id"] = unit_id

        writer = Recorder()
        processor = OutputBindingProcessor(modbus_writer=writer)
        processor.process(
            None,
            document(binding("modbus1", "modbus_write", parameters)),
            {"is_anomalous": metadata_value, "confidence": 0.9},
        )

        # Exactly one writer call carrying the configured target
        # (defaults applied for omitted optionals) and the coerced value.
        assert writer.calls == [(
            host,
            port if port is not None else DEFAULT_MODBUS_PORT,
            unit_id if unit_id is not None else 1,
            REGISTER_TYPE_COIL,
            address,
            expected,
            pulse_ms,
        )]
        assert isinstance(writer.calls[0][5], bool)

    @settings(max_examples=100, deadline=None)
    @given(
        host=_hosts,
        port=_ports,
        unit_id=_unit_ids,
        address=_addresses,
        register_value=_register_values,
    )
    def test_holding_register_dispatch_carries_target_and_integer(
        self, host, port, unit_id, address, register_value
    ):
        metadata_value, expected = register_value
        parameters = {
            "host": host,
            "register_type": REGISTER_TYPE_HOLDING_REGISTER,
            "address": address,
            "value_template": "{confidence}",
        }
        if port is not None:
            parameters["port"] = port
        if unit_id is not None:
            parameters["unit_id"] = unit_id

        writer = Recorder()
        processor = OutputBindingProcessor(modbus_writer=writer)
        processor.process(
            None,
            document(binding("modbus1", "modbus_write", parameters)),
            {"is_anomalous": True, "confidence": metadata_value},
        )

        assert writer.calls == [(
            host,
            port if port is not None else DEFAULT_MODBUS_PORT,
            unit_id if unit_id is not None else 1,
            REGISTER_TYPE_HOLDING_REGISTER,
            address,
            expected,
            0,
        )]
        value = writer.calls[0][5]
        assert isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------


# Feature: modbus-tcp-output, Property 5: Out-of-range register values
# fail without writing
class TestProperty5OutOfRangeRegisterFailsWithoutWriting:
    """# Feature: modbus-tcp-output, Property 5: Out-of-range register
    values fail without writing

    For any holding-register binding whose rendered value is not
    coercible to an integer in 0-65535, processing issues no writer call
    and aggregates an OutputBindingError naming the binding's node id.

    **Validates: Requirements 4.4**
    """

    @settings(max_examples=100, deadline=None)
    @given(
        node_id=_node_ids,
        host=_hosts,
        address=_addresses,
        bad_value=_uncoercible_values,
    )
    def test_uncoercible_value_raises_without_writer_call(
        self, node_id, host, address, bad_value
    ):
        parameters = {
            "host": host,
            "register_type": REGISTER_TYPE_HOLDING_REGISTER,
            "address": address,
            "value_template": "{confidence}",
        }
        writer = Recorder()
        processor = OutputBindingProcessor(modbus_writer=writer)

        with pytest.raises(OutputBindingError) as excinfo:
            processor.process(
                None,
                document(binding(node_id, "modbus_write", parameters)),
                {"is_anomalous": True, "confidence": bad_value},
            )

        # The ValueError is raised BEFORE any write: zero writer calls.
        assert writer.calls == []
        # Aggregated error names the failing binding's node id.
        assert excinfo.value.node_ids == [node_id]
        assert node_id in str(excinfo.value)


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------

_PULSE_HOST = "192.168.1.30"
_PULSE_PORT = 502
_PULSE_UNIT = 1


# Feature: modbus-tcp-output, Property 6: Coil pulse semantics
class TestProperty6CoilPulseSemantics:
    """# Feature: modbus-tcp-output, Property 6: Coil pulse semantics

    For any coil value and any pulse_ms in 0-60000, the production
    writer issues exactly one coil write when pulse_ms is 0, and exactly
    two coil writes — the rendered value followed by its inverse,
    separated by the pulse_ms wait — when pulse_ms is greater than 0.

    **Validates: Requirements 4.5**
    """

    @staticmethod
    def _run_writer(value, pulse_ms, address):
        """Run _default_modbus_writer with modbus_tcp.write_single and
        time.sleep patched, returning the ordered event sequence."""
        events = []

        def fake_write_single(
            host, port, unit_id, function_code, addr, wire_value,
            timeout=None,
        ):
            events.append(
                ("write", host, port, unit_id, function_code, addr,
                 wire_value)
            )

        def fake_sleep(seconds):
            events.append(("sleep", seconds))

        with patch.object(modbus_tcp, "write_single", fake_write_single), \
                patch.object(output_bindings.time, "sleep", fake_sleep):
            _default_modbus_writer(
                _PULSE_HOST, _PULSE_PORT, _PULSE_UNIT,
                REGISTER_TYPE_COIL, address, value, pulse_ms,
            )
        return events

    @settings(max_examples=100, deadline=None)
    @given(value=st.booleans(), address=_addresses)
    def test_latch_issues_exactly_one_write(self, value, address):
        wire_on = modbus_tcp.COIL_ON if value else modbus_tcp.COIL_OFF

        events = self._run_writer(value, 0, address)

        assert events == [(
            "write", _PULSE_HOST, _PULSE_PORT, _PULSE_UNIT,
            modbus_tcp.FUNCTION_WRITE_SINGLE_COIL, address, wire_on,
        )]

    @settings(max_examples=100, deadline=None)
    @given(
        value=st.booleans(),
        pulse_ms=st.integers(min_value=1, max_value=60000),
        address=_addresses,
    )
    def test_pulse_writes_value_waits_then_writes_inverse(
        self, value, pulse_ms, address
    ):
        wire_on = modbus_tcp.COIL_ON if value else modbus_tcp.COIL_OFF
        wire_off = modbus_tcp.COIL_OFF if value else modbus_tcp.COIL_ON

        events = self._run_writer(value, pulse_ms, address)

        assert events == [
            ("write", _PULSE_HOST, _PULSE_PORT, _PULSE_UNIT,
             modbus_tcp.FUNCTION_WRITE_SINGLE_COIL, address, wire_on),
            ("sleep", pulse_ms / 1000.0),
            ("write", _PULSE_HOST, _PULSE_PORT, _PULSE_UNIT,
             modbus_tcp.FUNCTION_WRITE_SINGLE_COIL, address, wire_off),
        ]


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------

#: Address reserved for the failing modbus binding in Property 8; the
#: healthy modbus bindings use the low addresses 0, 1, ...
_FAIL_ADDRESS = 9999


class SelectiveFailWriter:
    """Injectable modbus_writer raising only for the failing address."""

    def __init__(self, fail_address):
        self.calls = []
        self.fail_address = fail_address

    def __call__(self, *args):
        self.calls.append(args)
        if args[4] == self.fail_address:
            raise RuntimeError("modbus connection refused")


#: Parameters per non-modbus output binding kind, as the existing
#: containment tests configure them (index keeps ids/targets unique).
def _other_binding(kind, index):
    node_id = "other{0}".format(index)
    if kind == "mqtt_publish":
        return binding(node_id, kind,
                       {"broker_host": "b", "topic": "t{0}".format(index)})
    if kind == "digital_output":
        return binding(node_id, kind,
                       {"pin": index, "signal_type": "high",
                        "condition": "true"})
    return binding(node_id, "opcua_write",
                   {"endpoint": "opc.tcp://p:4840",
                    "node_id": "n{0}".format(index)})


@st.composite
def _mixed_documents(draw):
    """A shuffled document mixing one raising modbus_write binding with
    other output bindings (and optionally healthy modbus bindings)."""
    other_kinds = draw(st.lists(
        st.sampled_from(("mqtt_publish", "digital_output", "opcua_write")),
        min_size=1, max_size=4,
    ))
    healthy_count = draw(st.integers(min_value=0, max_value=2))
    failing_id = draw(_node_ids.map(lambda s: "bad" + s))

    entries = [_other_binding(kind, i) for i, kind in enumerate(other_kinds)]
    for i in range(healthy_count):
        entries.append(binding(
            "good{0}".format(i), "modbus_write",
            {"host": "plc.local", "register_type": REGISTER_TYPE_COIL,
             "address": i},
        ))
    fail_position = draw(st.integers(min_value=0, max_value=len(entries)))
    entries.insert(fail_position, binding(
        failing_id, "modbus_write",
        {"host": "plc.local", "register_type": REGISTER_TYPE_COIL,
         "address": _FAIL_ADDRESS},
    ))
    return entries, other_kinds, healthy_count, failing_id


class OtherRecorder:
    """Injectable non-modbus client fake recording every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)


# Feature: modbus-tcp-output, Property 8: Modbus failures are contained
# per binding
class TestProperty8ModbusFailuresAreContainedPerBinding:
    """# Feature: modbus-tcp-output, Property 8: Modbus failures are
    contained per binding

    For any document mixing a modbus_write binding with other output
    bindings, when the Modbus writer raises, every other binding is
    still processed normally and the raised OutputBindingError carries
    the failing Modbus node id.

    **Validates: Requirements 4.7**
    """

    @settings(max_examples=100, deadline=None)
    @given(layout=_mixed_documents())
    def test_other_bindings_run_and_error_names_only_the_failing_node(
        self, layout
    ):
        entries, other_kinds, healthy_count, failing_id = layout
        writer = SelectiveFailWriter(_FAIL_ADDRESS)
        dio, mqtt, opcua = OtherRecorder(), OtherRecorder(), OtherRecorder()
        processor = OutputBindingProcessor(
            dio_actuator=dio,
            mqtt_publisher=mqtt,
            opcua_writer=opcua,
            modbus_writer=writer,
        )

        with pytest.raises(OutputBindingError) as excinfo:
            processor.process(
                None, document(*entries),
                {"is_anomalous": True, "confidence": 0.9},
            )

        # Every other binding still ran, unaffected by the modbus failure.
        assert len(mqtt.calls) == other_kinds.count("mqtt_publish")
        assert len(dio.calls) == other_kinds.count("digital_output")
        assert len(opcua.calls) == other_kinds.count("opcua_write")
        healthy_writes = sorted(
            call[4] for call in writer.calls if call[4] != _FAIL_ADDRESS)
        assert healthy_writes == list(range(healthy_count))

        # The aggregated error names exactly the failing modbus node.
        assert excinfo.value.node_ids == [failing_id]
        assert failing_id in str(excinfo.value)


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------


# Feature: modbus-tcp-output, Property 9: Sent-message detail
# completeness
class TestProperty9SentMessageDetailCompleteness:
    """# Feature: modbus-tcp-output, Property 9: Sent-message detail
    completeness

    For any successfully processed modbus_write binding, the emitted
    Detail_Sink summary contains the written value, the register type,
    the address, the host and port, the unit id, and — when pulsed —
    the pulse duration.

    **Validates: Requirements 4.9**
    """

    @staticmethod
    def _process_one(parameters, metadata):
        """Process one modbus_write binding with a recording detail sink,
        returning the single (node_id, detail) it emitted."""
        details = []
        processor = OutputBindingProcessor(modbus_writer=Recorder())
        processor.process(
            None,
            document(binding("modbus1", "modbus_write", parameters)),
            metadata,
            detail_sink=lambda node_id, detail: details.append(
                (node_id, detail)),
        )
        assert len(details) == 1
        return details[0]

    @settings(max_examples=100, deadline=None)
    @given(
        host=_hosts,
        port=_ports,
        unit_id=_unit_ids,
        address=_addresses,
        pulse_ms=_pulse_ms,
        coil_value=_coil_values,
    )
    def test_coil_detail_carries_every_field(
        self, host, port, unit_id, address, pulse_ms, coil_value
    ):
        metadata_value, expected = coil_value
        parameters = {
            "host": host,
            "register_type": REGISTER_TYPE_COIL,
            "address": address,
            "pulse_ms": pulse_ms,
        }
        if port is not None:
            parameters["port"] = port
        if unit_id is not None:
            parameters["unit_id"] = unit_id

        node_id, detail = self._process_one(
            parameters, {"is_anomalous": metadata_value, "confidence": 0.9})

        assert node_id == "modbus1"
        assert repr(expected) in detail
        assert REGISTER_TYPE_COIL in detail
        assert str(address) in detail
        effective_port = port if port is not None else DEFAULT_MODBUS_PORT
        assert "{0}:{1}".format(host, effective_port) in detail
        assert "unit {0}".format(
            unit_id if unit_id is not None else 1) in detail
        if pulse_ms > 0:
            assert "pulse {0}ms".format(pulse_ms) in detail
        else:
            assert ", pulse" not in detail

    @settings(max_examples=100, deadline=None)
    @given(
        host=_hosts,
        port=_ports,
        unit_id=_unit_ids,
        address=_addresses,
        register_value=_register_values,
    )
    def test_holding_register_detail_carries_every_field(
        self, host, port, unit_id, address, register_value
    ):
        metadata_value, expected = register_value
        parameters = {
            "host": host,
            "register_type": REGISTER_TYPE_HOLDING_REGISTER,
            "address": address,
            "value_template": "{confidence}",
        }
        if port is not None:
            parameters["port"] = port
        if unit_id is not None:
            parameters["unit_id"] = unit_id

        node_id, detail = self._process_one(
            parameters, {"is_anomalous": True, "confidence": metadata_value})

        assert node_id == "modbus1"
        assert repr(expected) in detail
        assert REGISTER_TYPE_HOLDING_REGISTER in detail
        assert str(address) in detail
        effective_port = port if port is not None else DEFAULT_MODBUS_PORT
        assert "{0}:{1}".format(host, effective_port) in detail
        assert "unit {0}".format(
            unit_id if unit_id is not None else 1) in detail
        # Register writes never pulse.
        assert ", pulse" not in detail
