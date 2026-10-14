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
"""SHA-1 used only to derive stable, non-secret camera ids (Requirement 12).

The digest names a camera (``arv-`` and ``disc-`` Camera_Source ids); it
isn't used for authentication, tamper detection or password storage. The
algorithm and the callers' 12-hex-digit truncation must not change, because
persisted ``camera_source_id`` values and workflow bindings depend on them.
"""
import hashlib


def id_digest_hex(text: str) -> str:
    """Hex SHA-1 of ``text`` (UTF-8), marked as a non-security use."""
    data = text.encode("utf-8")
    try:
        digest = hashlib.sha1(data, usedforsecurity=False)
    except TypeError:
        # Python 3.8 has no usedforsecurity keyword (added in 3.9). Same digest,
        # still an identifier, not a security use.
        digest = hashlib.sha1(data)
    return digest.hexdigest()
