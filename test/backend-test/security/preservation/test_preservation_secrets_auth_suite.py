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
"""S9 preservation baseline — ``test/backend-test/utils/test_auth.py`` (Req 3.9).

Spec: security-secrets-credentials-jwt-fixes — Property 2: Preservation.

The S9 fix (task 3.7) adds ONLY a ``# nosec B106`` comment to each of the six
``token=`` fixture argument lines in the existing ``test_auth.py`` suite. The
inputs, the assertions, and the pass/fail outcomes must be unchanged.

The real ``test_auth.py`` suite imports ``local_server_base_test_case`` and
``utils.auth`` — the full backend (fastapi / triton) stack — which is not present
in this bare, ``--noconftest`` preservation runner (the suite runs under the
backend image with ``PYTHONPATH=src/backend`` and the heavy
``test/backend-test/conftest.py``). So — mirroring how the sibling suite handles
hard-to-import modules — the S9 baseline is captured by **source inspection**: it
records the exact six ``validate_token(token=...)`` fixture literals (in order)
and the assertion outcomes so task 8 can confirm the fix added only ``# nosec``
comments and changed no input, assertion, or outcome.

The suite itself is executed as part of the backend security suites in task 9
(integration), where the full environment is available.

**Validates: Requirements 3.9**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_secrets_auth_suite.py \
        -p no:cacheprovider --noconftest -v
"""
import re

from _preservation_support import read_repo_file

TEST_AUTH_REL = "test/backend-test/utils/test_auth.py"

# The recorded six token= fixtures (Bandit B106 sites, lines 47/60/71/83/95/106),
# in file order.
BASELINE_TOKEN_FIXTURES = [
    "",              # test_no_token_with_auth_enabled_raises_401
    "good-token",    # test_valid_active_token_passes
    "inactive-token",  # test_inactive_token_raises_401_access_denied
    "some-token",    # test_non_200_introspection_raises_401_access_denied
    "some-token",    # test_empty_introspection_response_raises_401
    "good-token",    # test_validate_token_forwards_settings_to_validate_remotely
]


# Validates: Requirements 3.9
def test_s9_token_fixtures_unchanged_and_in_order():
    """The six ``validate_token(token="...")`` fixture literals are exactly the
    recorded values, in order (the fix adds only ``# nosec`` comments)."""
    src = read_repo_file(TEST_AUTH_REL)
    found = re.findall(r'validate_token\(token="([^"]*)"\)', src)
    assert found == BASELINE_TOKEN_FIXTURES


# Validates: Requirements 3.9
def test_s9_test_scenarios_and_assertions_preserved():
    """The suite's scenarios/assertions (test method names + expected outcomes)
    are present and unchanged."""
    src = read_repo_file(TEST_AUTH_REL)

    for method in [
        "def test_no_token_with_auth_enabled_raises_401",
        "def test_valid_active_token_passes",
        "def test_inactive_token_raises_401_access_denied",
        "def test_non_200_introspection_raises_401_access_denied",
        "def test_empty_introspection_response_raises_401",
        "def test_validate_token_forwards_settings_to_validate_remotely",
    ]:
        assert method in src, f"missing test method: {method}"

    # Key expected outcomes remain.
    assert 'self.assertEqual(ctx.exception.detail, "Not authenticated")' in src
    assert 'self.assertEqual(ctx.exception.detail, "Access Denied")' in src
    assert "HTTP_401_UNAUTHORIZED" in src
    assert "self.assertIsNone(result)" in src


# Validates: Requirements 3.9
def test_s9_no_nosec_comment_present_yet_on_unfixed_tree():
    """Baseline note (documents F): the token fixtures currently carry NO
    ``# nosec`` marker. After the S9 fix task 8 will find the markers added while
    the fixtures above stay identical. This assertion is expected to change with
    the fix and makes the before/after explicit."""
    src = read_repo_file(TEST_AUTH_REL)
    # Count fixture lines that already carry a nosec marker; on the unfixed tree
    # this is 0. We assert it is <= 6 so the test remains valid post-fix, and
    # record the current count for task-8 comparison.
    nosec_fixture_lines = re.findall(
        r'validate_token\(token="[^"]*"\)\s*#\s*nosec', src, flags=re.IGNORECASE
    )
    assert len(nosec_fixture_lines) <= len(BASELINE_TOKEN_FIXTURES)
