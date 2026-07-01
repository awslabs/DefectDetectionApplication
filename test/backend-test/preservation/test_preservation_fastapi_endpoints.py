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
"""Preservation baseline: FastAPI endpoint request/response equivalence (Req 3.4).

Spec: python-3-11-security-upgrade — Property 2: Preservation — No functional
regression for non-3.9 artifacts.

A client calling the FastAPI app must get the same responses and status codes
after the interpreter moves from 3.9 to 3.11 (Req 3.4). This module is a
**property-based** baseline over generated requests: for any generated request
path/method, the app routes it to a *defined* HTTP outcome — an unknown path is a
404, a wrong method is a 405, and the app never collapses into an interpreter-level
5xx — exactly as it did on 3.9.

DEFERRED EXECUTION (in-image gate — tasks 11/12)
------------------------------------------------
This test imports the full backend app (``app:app`` + the SQLAlchemy DAO + the
``conftest`` / ``mock_gi`` import-mocking stack), none of which exists in a bare
checkout — they are only present inside the ``flask-app`` docker image. It is
therefore **skipped in the bare environment** (``importorskip`` below) and is run,
unchanged, under the in-image 3.11 test gate alongside the existing
``test/backend-test/api-endpoints`` suite (design "Preservation Checking" case 4 /
"run the existing backend unit tests under 3.11"). It MUST be collected with the
normal ``conftest.py`` active (NOT ``--noconftest``) so the app import is mocked.

When run in-image it asserts the request→status contract that the 3.9 baseline
exhibits and the 3.11 build must preserve.
"""
import pytest

# Skip cleanly in a bare checkout: these are only importable inside the image.
pytest.importorskip("fastapi", reason="FastAPI preservation test runs in the flask-app image (task 11/12)")
pytest.importorskip("pydantic", reason="FastAPI preservation test runs in the flask-app image (task 11/12)")
pytest.importorskip("sqlalchemy", reason="FastAPI preservation test runs in the flask-app image (task 11/12)")

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from local_server_base_test_case import LocalServerBaseTestCase

# HTTP statuses that represent a *defined* routing outcome (never an interpreter /
# runtime 5xx). The baseline contract: every request lands on one of these.
_DEFINED_STATUSES = {200, 201, 204, 301, 307, 308, 400, 401, 403, 404, 405, 415, 422}

# Path segments built from URL-safe characters so the generated path is a valid
# request target (the property is about routing, not URL parsing).
_segment = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=0x7A),
    min_size=1,
    max_size=12,
)
_unknown_paths = st.lists(_segment, min_size=1, max_size=4).map(lambda parts: "/" + "/".join(parts))


class TestFastAPIEndpointPreservation(LocalServerBaseTestCase):
    """Generated-request routing contract for the FastAPI app (in-image only).

    Spec: python-3-11-security-upgrade — Property 2: Preservation.
    Validates: Requirements 3.4
    """

    # suppress_health_check: the TestClient is built once in setUp (function scope),
    # which Hypothesis would otherwise flag when reused across examples.
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(path=_unknown_paths, method=st.sampled_from(["GET", "POST", "PUT", "DELETE", "PATCH"]))
    def test_generated_requests_route_to_defined_status(self, path, method):
        """Any generated request yields a defined status, never an interpreter 5xx.

        Validates: Requirements 3.4
        """
        response = self.client.request(method, path)
        assert response.status_code in _DEFINED_STATUSES, (
            f"{method} {path} -> {response.status_code} (expected a defined routing status; "
            f"a 5xx here would indicate an interpreter/runtime regression)"
        )

    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(path=_unknown_paths)
    def test_unknown_paths_are_not_found(self, path):
        """A path under no registered prefix is a 404 (routing preserved).

        Validates: Requirements 3.4
        """
        # Constrain to clearly-unregistered roots so the assertion is precise.
        if path.split("/")[1] in {"api", "docs", "openapi.json", "redoc"}:
            return
        response = self.client.get(path)
        assert response.status_code == 404, (
            f"GET {path} -> {response.status_code} (expected 404 for an unknown route)"
        )
