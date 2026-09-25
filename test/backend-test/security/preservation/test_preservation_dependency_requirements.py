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
"""``src/backend/requirements.txt`` full-file golden (Req 3.3) -- dependency CVE
spec.

Spec: security-dependency-cve-fixes -- Property 2: Preservation.

Capture every line of ``src/backend/requirements.txt``, explicitly recording
line 8 ``urllib3==2.8.0`` and line 9 ``requests==2.34.2`` (the F2 site), and
assert every line except line 9 is byte-for-byte identical to the golden, with
line 9 differing at most in its ``requests`` version token. Re-baselined by
dependabot-remediation (Pillow/urllib3/requests/pydantic/marshmallow/grpcio
bumps and python-opcua -> asyncua); the golden is the post-remediation file.

Golden: ``baselines/dependency_baseline_requirements.txt``.

**Validates: Requirements 3.3**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_dependency_requirements.py \
        -p no:cacheprovider --noconftest -v
"""
from _dependency_preservation_support import (
    BACKEND_REQS_REL,
    BASELINE_REQUESTS_VERSION,
    assert_pin_file_matches_baseline,
)

_GOLDEN = "dependency_baseline_requirements.txt"

# The F2 pin line, located by content. ``requirements.txt`` has exactly one
# ``requests==`` pin (line 9). The audit's pin regex would also match the
# ``requests`` substring in a bare token, but this file only pins it once.
_F2_PIN_SUBSTRING = "requests=="


# Validates: Requirements 3.3
def test_requirements_full_file_golden():
    """Every line of ``requirements.txt`` is byte-for-byte identical to the
    baseline except the F2 pin line (line 9), which may differ ONLY in its
    version token."""
    assert_pin_file_matches_baseline(_GOLDEN, BACKEND_REQS_REL, _F2_PIN_SUBSTRING)


# Validates: Requirements 3.3
def test_requirements_records_urllib3_and_requests_lines():
    """The baseline records line 8 ``urllib3==2.8.0`` and line 9
    ``requests==2.34.2`` (both >= their Dependabot-patched floors)."""
    golden_lines, _current_lines, pin_idx = assert_pin_file_matches_baseline(
        _GOLDEN, BACKEND_REQS_REL, _F2_PIN_SUBSTRING
    )
    # Line 9 (the requests pin) records the baseline version.
    assert golden_lines[pin_idx] == f"requests=={BASELINE_REQUESTS_VERSION}", (
        f"baseline F2 pin line should be 'requests=={BASELINE_REQUESTS_VERSION}', got: "
        f"{golden_lines[pin_idx]!r}"
    )
    # Line immediately above (line 8) is the patched urllib3 pin.
    assert golden_lines[pin_idx - 1] == "urllib3==2.8.0", (
        f"expected line 8 'urllib3==2.8.0' immediately above the requests pin, "
        f"got: {golden_lines[pin_idx - 1]!r}"
    )
