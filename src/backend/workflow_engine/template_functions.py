# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Template functions of the ``mqtt_publish`` ``payload_format: json`` mode
(bedrock-inspection-chain): exactly ``ulid``, ``now_iso`` and ``latency_ms``.

They are reached only through ``output_bindings.render_json_template`` when
a JSON template string is exactly ``{ulid}``, ``{now_iso}`` or
``{latency_ms}`` (or carries one of them embedded in text); the ``text``
payload format never calls them. Standard library only.
"""

import secrets
import time
from datetime import datetime, timezone
from typing import Optional

#: Crockford base32 alphabet (no I, L, O, U), the ULID encoding.
ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: 26 characters encode 130 bits; a ULID uses 128 (48 time + 80 random).
ULID_LENGTH = 26
_ULID_TIME_BITS = 48
_ULID_RANDOM_BITS = 80


def ulid(now_ms: Optional[int] = None) -> str:
    """A fresh 26-character ULID: the 48-bit Unix-millisecond timestamp
    (``now_ms``, default the wall clock) followed by 80 random bits from
    :mod:`secrets`, Crockford-base32 encoded most significant character
    first (so ULIDs minted later sort later by their time part)."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    value = ((int(now_ms) & ((1 << _ULID_TIME_BITS) - 1)) << _ULID_RANDOM_BITS) | secrets.randbits(_ULID_RANDOM_BITS)
    chars = []
    for index in range(ULID_LENGTH):
        shift = 5 * (ULID_LENGTH - 1 - index)
        chars.append(ULID_ALPHABET[(value >> shift) & 31])
    return "".join(chars)


def now_iso() -> str:
    """The current UTC time as an RFC 3339 ``date-time`` with millisecond
    precision and an explicit ``+00:00`` offset."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def latency_ms(trigger_timestamp, now: Optional[float] = None) -> Optional[int]:
    """Whole milliseconds elapsed since ``trigger_timestamp`` (the trigger
    receipt time in Unix seconds, ``trigger.timestamp`` of the run metadata)
    at ``now`` (default the wall clock). ``None`` -- the placeholder stays
    unresolved -- when the timestamp is absent or not a number."""
    if isinstance(trigger_timestamp, bool) or not isinstance(
            trigger_timestamp, (int, float)):
        return None
    if now is None:
        now = time.time()
    return int((now - trigger_timestamp) * 1000)
