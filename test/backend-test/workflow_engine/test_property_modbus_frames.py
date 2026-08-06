#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Property-based tests for the Modbus TCP framing layer
(modbus-tcp-output feature, Properties 1-3).

All three properties exercise the pure frame functions in
``workflow_engine.modbus_tcp`` — no sockets.
"""
import struct

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from workflow_engine.modbus_tcp import (
    COIL_OFF,
    COIL_ON,
    EXCEPTION_MEANINGS,
    FUNCTION_WRITE_SINGLE_COIL,
    FUNCTION_WRITE_SINGLE_REGISTER,
    ModbusError,
    WriteFrame,
    check_response,
    decode_frame,
    encode_write_request,
)

import pytest

_transaction_ids = st.integers(min_value=0, max_value=0xFFFF)
_unit_ids = st.integers(min_value=0, max_value=0xFF)
_addresses = st.integers(min_value=0, max_value=0xFFFF)
_register_values = st.integers(min_value=0, max_value=0xFFFF)

# A valid write request: coil writes carry exactly COIL_ON/COIL_OFF;
# register writes carry any 16-bit value (Requirement 5.1).
_requests = st.one_of(
    st.tuples(
        _transaction_ids,
        _unit_ids,
        st.just(FUNCTION_WRITE_SINGLE_COIL),
        _addresses,
        st.sampled_from([COIL_ON, COIL_OFF]),
    ),
    st.tuples(
        _transaction_ids,
        _unit_ids,
        st.just(FUNCTION_WRITE_SINGLE_REGISTER),
        _addresses,
        _register_values,
    ),
)


class TestProperty1RoundTripAndEchoValidation:
    """# Feature: modbus-tcp-output, Property 1: Modbus frame round trip
    and echo validation

    For any valid write request, decoding the encoded request frame
    yields the original field values, and validating the request's own
    echo as a response reports success.

    **Validates: Requirements 5.1, 5.2, 5.3**
    """

    @settings(max_examples=100, deadline=None)
    @given(request=_requests)
    def test_round_trip_and_echo_validation(self, request):
        transaction_id, unit_id, function_code, address, value = request

        frame = encode_write_request(
            transaction_id, unit_id, function_code, address, value
        )

        # Wire shape: 12 bytes, big-endian, protocol id 0, length 6.
        assert len(frame) == 12
        assert frame[2:4] == b"\x00\x00"  # protocol id
        assert frame[4:6] == b"\x00\x06"  # length field

        # Round trip: decode(encode(x)) == x (Requirement 5.3).
        decoded = decode_frame(frame)
        assert decoded == WriteFrame(
            transaction_id, unit_id, function_code, address, value
        )

        # A response echoing the request validates as success
        # (Requirement 5.2): check_response returns None, raises nothing.
        assert check_response(decoded, frame) is None


class TestProperty2ExceptionResponsesNamed:
    """# Feature: modbus-tcp-output, Property 2: Exception responses
    raise with the named meaning

    For any valid write request and any Modbus exception code,
    validating an exception response raises a Modbus error whose message
    names the exception code, and for codes 0x01-0x04 also names the
    standard Modbus meaning.

    **Validates: Requirements 5.4**
    """

    @settings(max_examples=100, deadline=None)
    @given(
        request=_requests,
        exception_code=st.integers(min_value=0, max_value=0xFF),
    )
    def test_exception_response_names_code_and_meaning(
        self, request, exception_code
    ):
        transaction_id, unit_id, function_code, address, value = request
        request_frame = WriteFrame(
            transaction_id, unit_id, function_code, address, value
        )
        # 9-byte exception response: MBAP (length 3) + (fc | 0x80) + code.
        response = struct.pack(
            ">HHHBBB",
            transaction_id,
            0,
            3,
            unit_id,
            function_code | 0x80,
            exception_code,
        )

        with pytest.raises(ModbusError) as excinfo:
            check_response(request_frame, response)

        message = str(excinfo.value)
        assert f"0x{exception_code:02X}" in message
        if 0x01 <= exception_code <= 0x04:
            assert EXCEPTION_MEANINGS[exception_code] in message


class TestProperty3MalformedResponsesRejected:
    """# Feature: modbus-tcp-output, Property 3: Malformed responses
    never validate as success

    For any valid write request and any response malformation
    (truncation, mismatched transaction id, non-zero protocol id, or an
    unexpected function code), validation raises a Modbus error.

    **Validates: Requirements 5.5**
    """

    @settings(max_examples=100, deadline=None)
    @given(
        request=_requests,
        truncate_to=st.integers(min_value=0, max_value=11),
    )
    def test_truncated_response_rejected(self, request, truncate_to):
        transaction_id, unit_id, function_code, address, value = request
        request_frame = WriteFrame(
            transaction_id, unit_id, function_code, address, value
        )
        echo = encode_write_request(
            transaction_id, unit_id, function_code, address, value
        )

        with pytest.raises(ModbusError):
            check_response(request_frame, echo[:truncate_to])

    @settings(max_examples=100, deadline=None)
    @given(
        request=_requests,
        other_transaction_id=_transaction_ids,
    )
    def test_transaction_id_mismatch_rejected(
        self, request, other_transaction_id
    ):
        transaction_id, unit_id, function_code, address, value = request
        assume(other_transaction_id != transaction_id)
        request_frame = WriteFrame(
            transaction_id, unit_id, function_code, address, value
        )
        response = encode_write_request(
            other_transaction_id, unit_id, function_code, address, value
        )

        with pytest.raises(ModbusError) as excinfo:
            check_response(request_frame, response)
        assert "transaction id" in str(excinfo.value)

    @settings(max_examples=100, deadline=None)
    @given(
        request=_requests,
        protocol_id=st.integers(min_value=1, max_value=0xFFFF),
    )
    def test_nonzero_protocol_id_rejected(self, request, protocol_id):
        transaction_id, unit_id, function_code, address, value = request
        request_frame = WriteFrame(
            transaction_id, unit_id, function_code, address, value
        )
        response = struct.pack(
            ">HHHBBHH",
            transaction_id,
            protocol_id,
            6,
            unit_id,
            function_code,
            address,
            value,
        )

        with pytest.raises(ModbusError) as excinfo:
            check_response(request_frame, response)
        assert "protocol id" in str(excinfo.value)

    @settings(max_examples=100, deadline=None)
    @given(
        request=_requests,
        other_function_code=st.integers(min_value=0, max_value=0xFF),
    )
    def test_unexpected_function_code_rejected(
        self, request, other_function_code
    ):
        transaction_id, unit_id, function_code, address, value = request
        assume(other_function_code != function_code)
        request_frame = WriteFrame(
            transaction_id, unit_id, function_code, address, value
        )
        response = struct.pack(
            ">HHHBBHH",
            transaction_id,
            0,
            6,
            unit_id,
            other_function_code,
            address,
            value,
        )

        # An unexpected function code — including the exception form
        # (fc | 0x80) — never validates as success.
        with pytest.raises(ModbusError):
            check_response(request_frame, response)

    @settings(max_examples=100, deadline=None)
    @given(request=_requests, other=_requests)
    def test_echo_field_mismatch_rejected(self, request, other):
        transaction_id, unit_id, function_code, address, value = request
        _, _, _, other_address, other_value = other
        assume((other_address, other_value) != (address, value))
        request_frame = WriteFrame(
            transaction_id, unit_id, function_code, address, value
        )
        response = encode_write_request(
            transaction_id, unit_id, function_code, other_address, other_value
        )

        with pytest.raises(ModbusError) as excinfo:
            check_response(request_frame, response)
        assert "echo mismatch" in str(excinfo.value)
