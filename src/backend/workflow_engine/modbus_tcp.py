#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Minimal Modbus TCP client for the ``modbus_write`` output node
(modbus-tcp-output feature).

Pure framing functions (encode/decode/validate) over the 12-byte
MBAP-header-prefixed Write Single Coil (function code 0x05) and Write
Single Register (function code 0x06) frames, plus one socket exchange
helper. Implemented with only the Python standard library
(``socket``/``struct``) so the preservation-tracked dependency surface
(``src/backend/requirements.txt``) stays unchanged (Requirement 5.6).

Wire format (big-endian, Requirement 5.1):

======  ====  ==============  =========================================
Offset  Size  Field           Value
======  ====  ==============  =========================================
0       2     Transaction id  per-exchange counter
2       2     Protocol id     always 0
4       2     Length          6 (unit id + PDU)
6       1     Unit id         0-255
7       1     Function code   0x05 / 0x06 (exception response: +0x80)
8       2     Address         0-65535
10      2     Value           coil: 0xFF00/0x0000; register: 0-65535
======  ====  ==============  =========================================

A successful write response echoes the request's function code, address,
and value with a matching transaction id (Requirement 5.2). An exception
response is 9 bytes: MBAP + (function code | 0x80) + one exception code
byte (Requirement 5.4). Truncated, transaction-id-mismatched,
non-zero-protocol-id, and unexpected-function-code responses are rejected
rather than treated as success (Requirement 5.5). The TCP connect and
the response wait are each bounded by a 5-second default timeout
(Requirement 4.8).
"""

import itertools
import socket
import struct
import threading
from dataclasses import dataclass

FUNCTION_WRITE_SINGLE_COIL = 0x05
FUNCTION_WRITE_SINGLE_REGISTER = 0x06

#: 16-bit wire values for the two coil states (Requirement 5.1).
COIL_ON = 0xFF00
COIL_OFF = 0x0000

#: Bound on both the TCP connect and the response wait (Requirement 4.8).
MODBUS_TIMEOUT_SEC = 5.0

#: Standard Modbus exception code meanings (Requirement 5.4).
EXCEPTION_MEANINGS = {
    0x01: "ILLEGAL FUNCTION",
    0x02: "ILLEGAL DATA ADDRESS",
    0x03: "ILLEGAL DATA VALUE",
    0x04: "SERVER DEVICE FAILURE",
    0x05: "ACKNOWLEDGE",
    0x06: "SERVER DEVICE BUSY",
    0x08: "MEMORY PARITY ERROR",
    0x0A: "GATEWAY PATH UNAVAILABLE",
    0x0B: "GATEWAY TARGET DEVICE FAILED TO RESPOND",
}

_FRAME_LEN = 12  # MBAP header (7) + function code (1) + address (2) + value (2)
_EXCEPTION_FRAME_LEN = 9  # MBAP header (7) + function code (1) + code (1)
_MBAP_LENGTH_FIELD = 6  # unit id (1) + function code (1) + address (2) + value (2)
_FRAME_STRUCT = ">HHHBBHH"


class ModbusError(Exception):
    """Modbus exchange failure: exception response, malformed response,
    or a bounded-timeout expiry."""


@dataclass(frozen=True)
class WriteFrame:
    """The field content of one write request (or its response echo)."""

    transaction_id: int
    unit_id: int
    function_code: int
    address: int
    value: int


def _check_range(name: str, value: int, low: int, high: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not (
        low <= value <= high
    ):
        raise ValueError(
            f"{name} must be an integer in {low}-{high}, got {value!r}"
        )


def encode_write_request(
    transaction_id: int,
    unit_id: int,
    function_code: int,
    address: int,
    value: int,
) -> bytes:
    """Encode a 12-byte MBAP+PDU write request frame, big-endian
    (Requirement 5.1)."""
    _check_range("transaction_id", transaction_id, 0, 0xFFFF)
    _check_range("unit_id", unit_id, 0, 0xFF)
    if function_code not in (
        FUNCTION_WRITE_SINGLE_COIL,
        FUNCTION_WRITE_SINGLE_REGISTER,
    ):
        raise ValueError(
            f"function_code must be 0x05 or 0x06, got {function_code!r}"
        )
    _check_range("address", address, 0, 0xFFFF)
    _check_range("value", value, 0, 0xFFFF)
    return struct.pack(
        _FRAME_STRUCT,
        transaction_id,
        0,  # protocol id
        _MBAP_LENGTH_FIELD,
        unit_id,
        function_code,
        address,
        value,
    )


def decode_frame(frame: bytes) -> WriteFrame:
    """Parse a 12-byte write request/echo frame into its fields.

    Raises :class:`ModbusError` on truncation, a non-zero protocol id, or
    a length field other than 6 (Requirements 5.2, 5.5).
    """
    if len(frame) < _FRAME_LEN:
        raise ModbusError(
            f"truncated Modbus frame: got {len(frame)} bytes, "
            f"expected {_FRAME_LEN}"
        )
    (
        transaction_id,
        protocol_id,
        length,
        unit_id,
        function_code,
        address,
        value,
    ) = struct.unpack(_FRAME_STRUCT, frame[:_FRAME_LEN])
    if protocol_id != 0:
        raise ModbusError(
            f"non-zero Modbus protocol id {protocol_id} (must be 0)"
        )
    if length != _MBAP_LENGTH_FIELD:
        raise ModbusError(
            f"bad Modbus MBAP length field {length} "
            f"(expected {_MBAP_LENGTH_FIELD})"
        )
    return WriteFrame(transaction_id, unit_id, function_code, address, value)


def check_response(request: WriteFrame, response_bytes: bytes) -> None:
    """Validate a response against its request; returns None on success.

    - An exception response (function code | 0x80) raises
      :class:`ModbusError` naming the exception code and its standard
      Modbus meaning (Requirement 5.4).
    - Truncation, a transaction-id mismatch, a non-zero protocol id, an
      unexpected function code, or an address/value echo mismatch raises
      :class:`ModbusError` describing the malformation (Requirement 5.5).
    """
    if (
        len(response_bytes) >= _EXCEPTION_FRAME_LEN
        and response_bytes[7] == (request.function_code | 0x80)
    ):
        code = response_bytes[8]
        meaning = EXCEPTION_MEANINGS.get(code, "UNKNOWN EXCEPTION CODE")
        raise ModbusError(
            f"Modbus exception 0x{code:02X} ({meaning}) for function "
            f"code 0x{request.function_code:02X} at address "
            f"{request.address}"
        )
    echo = decode_frame(response_bytes)
    if echo.transaction_id != request.transaction_id:
        raise ModbusError(
            f"Modbus transaction id mismatch: sent "
            f"{request.transaction_id}, response carries "
            f"{echo.transaction_id}"
        )
    if echo.function_code != request.function_code:
        raise ModbusError(
            f"unexpected Modbus function code in response: sent "
            f"0x{request.function_code:02X}, response carries "
            f"0x{echo.function_code:02X}"
        )
    if echo.address != request.address or echo.value != request.value:
        raise ModbusError(
            f"Modbus response echo mismatch: sent address="
            f"{request.address} value={request.value}, response echoes "
            f"address={echo.address} value={echo.value}"
        )


# Per-exchange transaction id counter (wraps at the 16-bit boundary).
_transaction_counter = itertools.count()
_transaction_lock = threading.Lock()


def _next_transaction_id() -> int:
    with _transaction_lock:
        return next(_transaction_counter) & 0xFFFF


def _recv_bounded(sock: socket.socket, count: int) -> bytes:
    """Receive up to ``count`` bytes, stopping early if the peer closes.

    The socket's timeout (set by :func:`write_single`) bounds each recv.
    """
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            break  # peer closed; the validator reports the truncation
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def write_single(
    host: str,
    port: int,
    unit_id: int,
    function_code: int,
    address: int,
    value: int,
    timeout: float = MODBUS_TIMEOUT_SEC,
) -> None:
    """One Modbus TCP write exchange: connect, send, receive, validate,
    close (Requirements 4.1, 4.8).

    ``timeout`` bounds both the TCP connect and the response wait; a
    timeout raises :class:`ModbusError` naming the host and port.
    """
    transaction_id = _next_transaction_id()
    request = WriteFrame(transaction_id, unit_id, function_code, address, value)
    request_bytes = encode_write_request(
        transaction_id, unit_id, function_code, address, value
    )
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except socket.timeout as exc:
        raise ModbusError(
            f"Modbus TCP connect to {host}:{port} timed out "
            f"after {timeout}s"
        ) from exc
    try:
        sock.settimeout(timeout)
        sock.sendall(request_bytes)
        # MBAP + function code + first PDU byte: enough to distinguish a
        # 9-byte exception response from a 12-byte write echo.
        head = _recv_bounded(sock, _EXCEPTION_FRAME_LEN)
        if (
            len(head) >= _EXCEPTION_FRAME_LEN
            and head[7] == (function_code | 0x80)
        ):
            response = head
        else:
            response = head + _recv_bounded(
                sock, _FRAME_LEN - len(head)
            )
        check_response(request, response)
    except socket.timeout as exc:
        raise ModbusError(
            f"Modbus TCP response from {host}:{port} timed out "
            f"after {timeout}s"
        ) from exc
    finally:
        sock.close()
