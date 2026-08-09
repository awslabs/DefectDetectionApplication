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
"""Example tests for the Modbus TCP framing module
(modbus-tcp-output feature): known-answer frame vectors anchoring the
wire format (Requirement 5.1) and the bounded socket timeout
(Requirement 4.8).
"""
import socket
import threading

import pytest

from workflow_engine.modbus_tcp import (
    COIL_ON,
    FUNCTION_WRITE_SINGLE_COIL,
    FUNCTION_WRITE_SINGLE_REGISTER,
    ModbusError,
    WriteFrame,
    check_response,
    decode_frame,
    encode_write_request,
    write_single,
)

# Canonical Write Single Coil ON frame: transaction 1, unit 1, coil at
# address 12 (0x000C) set ON (0xFF00).
CANONICAL_COIL_ON = bytes.fromhex("00 01 00 00 00 06 01 05 00 0C FF 00")


class TestKnownAnswerVectors:
    def test_encode_canonical_write_coil_on(self):
        frame = encode_write_request(
            transaction_id=1,
            unit_id=1,
            function_code=FUNCTION_WRITE_SINGLE_COIL,
            address=12,
            value=COIL_ON,
        )
        assert frame == CANONICAL_COIL_ON

    def test_decode_canonical_write_coil_on(self):
        assert decode_frame(CANONICAL_COIL_ON) == WriteFrame(
            transaction_id=1,
            unit_id=1,
            function_code=FUNCTION_WRITE_SINGLE_COIL,
            address=12,
            value=COIL_ON,
        )

    def test_encode_write_coil_off(self):
        # Same target, coil OFF: value bytes become 0x0000.
        frame = encode_write_request(1, 1, FUNCTION_WRITE_SINGLE_COIL, 12, 0)
        assert frame == bytes.fromhex("00 01 00 00 00 06 01 05 00 0C 00 00")

    def test_encode_write_holding_register(self):
        # Transaction 2, unit 1, register 40 (0x0028) := 1234 (0x04D2).
        frame = encode_write_request(
            2, 1, FUNCTION_WRITE_SINGLE_REGISTER, 40, 1234
        )
        assert frame == bytes.fromhex("00 02 00 00 00 06 01 06 00 28 04 D2")

    def test_exception_response_vector(self):
        # 9-byte exception response: fc | 0x80 = 0x85, code 0x02
        # ILLEGAL DATA ADDRESS.
        request = decode_frame(CANONICAL_COIL_ON)
        response = bytes.fromhex("00 01 00 00 00 03 01 85 02")
        with pytest.raises(ModbusError) as excinfo:
            check_response(request, response)
        message = str(excinfo.value)
        assert "0x02" in message
        assert "ILLEGAL DATA ADDRESS" in message


class TestSocketExchange:
    def test_successful_echo_exchange(self):
        """A server echoing the request frame completes the exchange."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()

        def echo_once():
            conn, _ = server.accept()
            with conn:
                data = conn.recv(12)
                conn.sendall(data)

        thread = threading.Thread(target=echo_once, daemon=True)
        thread.start()
        try:
            write_single(
                host, port, 1, FUNCTION_WRITE_SINGLE_COIL, 12, COIL_ON,
                timeout=2.0,
            )
        finally:
            thread.join(timeout=2.0)
            server.close()

    def test_response_timeout_names_host_and_port(self):
        """A listener that accepts but never responds trips the bounded
        response timeout, and the error names the host and port
        (Requirement 4.8)."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()

        accepted = []

        def accept_and_stall():
            conn, _ = server.accept()
            accepted.append(conn)  # keep the connection open, send nothing

        thread = threading.Thread(target=accept_and_stall, daemon=True)
        thread.start()
        try:
            with pytest.raises(ModbusError) as excinfo:
                write_single(
                    host, port, 1, FUNCTION_WRITE_SINGLE_COIL, 12, COIL_ON,
                    timeout=0.2,
                )
            message = str(excinfo.value)
            assert host in message
            assert str(port) in message
            assert "timed out" in message
        finally:
            thread.join(timeout=2.0)
            for conn in accepted:
                conn.close()
            server.close()
