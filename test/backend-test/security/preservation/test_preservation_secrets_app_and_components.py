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
"""S6 + S7 preservation baselines — ``app.py`` bind + ``components.py`` paging
(Req 3.6, 3.7).

Spec: security-secrets-credentials-jwt-fixes — Property 2: Preservation.

* **S6 (Req 3.6):** the uvicorn bind selection is unchanged — ``0.0.0.0:5443``
  with TLS when ``utils.is_authorization_enabled_on_station()`` is True, and the
  plaintext ``0.0.0.0:5000`` path otherwise. ``app.py`` imports the full backend
  stack (fastapi / structlog / alembic / panorama / dda_triton / ...) at module
  top and ``main()`` has live side effects, so — as the sibling suite does for
  hard-to-import modules — the S6 baseline is captured by **source inspection**:
  the exact ``uvicorn.Config(...)`` bind calls are recorded and asserted present.
  The S6 fix adds only a trailing ``# nosem`` / ``# nosec`` comment, so these
  ``Config(...)`` call substrings survive verbatim; task 8 re-asserts them.

* **S7 (Req 3.7):** ``list_private_components`` initializes an empty
  ``pagination_token`` cursor and pages the Resource Groups Tagging API until the
  token is empty. ``components.py`` loads in isolation (shared_utils / boto3
  stubbed), so the REAL pagination loop is exercised with a fake tagging client.
  The S7 fix adds only a ``# nosec B105`` comment, so the paging behavior is
  unchanged; task 8 re-runs this directly.

**Validates: Requirements 3.6, 3.7**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_secrets_app_and_components.py \
        -p no:cacheprovider --noconftest -v
"""
import types

from _preservation_support import load_module_from_path, read_repo_file

APP_REL = "src/backend/app.py"
COMPONENTS_REL = "edge-cv-portal/backend/functions/components.py"


# --------------------------------------------------------------------------- #
# S6 — uvicorn bind selection (recorded via source inspection)
# --------------------------------------------------------------------------- #
# The recorded baseline: the exact bind Config(...) calls. These substrings are
# the functional bind config; the S6 fix only appends a # nosem/# nosec comment,
# which lives outside the call, so the substrings are preserved byte-for-byte.
BASELINE_TLS_BIND = (
    'uvicorn.Config(app, host="0.0.0.0", port=5443, loop="asyncio", '
    'log_config="dda_logging/uvicorn_disable_logging.json", '
    "ssl_certfile=constants.DDA_LOCAL_SERVER_SSL_CERT, "
    "ssl_keyfile=constants.DDA_LOCAL_SERVER_SSL_KEY)"
)
BASELINE_PLAINTEXT_BIND = (
    'uvicorn.Config(app, host="0.0.0.0", port=5000, loop="asyncio", '
    'log_config="dda_logging/uvicorn_disable_logging.json")'
)


# Validates: Requirements 3.6
def test_s6_tls_bind_config_preserved():
    """Authorization enabled -> 0.0.0.0:5443 TLS bind, recorded verbatim."""
    src = read_repo_file(APP_REL)
    assert "if utils.is_authorization_enabled_on_station():" in src
    assert BASELINE_TLS_BIND in src


# Validates: Requirements 3.6
def test_s6_plaintext_bind_config_preserved():
    """Authorization disabled -> 0.0.0.0:5000 plaintext bind (no ssl), recorded
    verbatim."""
    src = read_repo_file(APP_REL)
    assert BASELINE_PLAINTEXT_BIND in src
    assert "ssl_certfile" not in BASELINE_PLAINTEXT_BIND
    assert "ssl_keyfile" not in BASELINE_PLAINTEXT_BIND


# Validates: Requirements 3.6
def test_s6_both_binds_use_zero_host():
    """Both binds use host 0.0.0.0 and their respective ports — the selection
    branch is preserved."""
    src = read_repo_file(APP_REL)
    assert src.count('host="0.0.0.0"') >= 2
    assert 'port=5443' in src
    assert 'port=5000' in src


# --------------------------------------------------------------------------- #
# S7 — components.py empty-cursor pagination (real loop, stubbed clients)
# --------------------------------------------------------------------------- #
def _components_stubs():
    shared = types.ModuleType("shared_utils")
    for name in [
        "get_user_from_event", "assume_cross_account_role", "cors_headers",
        "handle_error", "check_user_access", "create_boto3_client",
        "get_usecase_region",
    ]:
        setattr(shared, name, lambda *a, **k: None)

    boto3 = types.ModuleType("boto3")
    boto3.resource = lambda *a, **k: types.SimpleNamespace()
    boto3.client = lambda *a, **k: types.SimpleNamespace()

    botocore = types.ModuleType("botocore")
    exc = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        pass

    exc.ClientError = ClientError
    botocore.exceptions = exc

    return {
        "shared_utils": shared,
        "boto3": boto3,
        "botocore": botocore,
        "botocore.exceptions": exc,
    }


def _load_components():
    return load_module_from_path(
        "components_preservation", COMPONENTS_REL, injected_modules=_components_stubs()
    )


class _FakeTaggingClient:
    """Two-page tagging client; records the params it is called with so we can
    assert the empty-cursor initialization and the PaginationToken follow-up."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def get_resources(self, **params):
        self.calls.append(params)
        return self._pages.pop(0)


# Validates: Requirements 3.7
def test_s7_empty_pagination_cursor_initializes_first_page_unpaginated():
    """The empty ``pagination_token`` means the first ``get_resources`` call
    carries NO ``PaginationToken``; subsequent calls follow the returned token
    until it is empty."""
    mod = _load_components()
    tagging = _FakeTaggingClient(
        pages=[
            {"ResourceTagMappingList": [], "PaginationToken": "PAGE2"},
            {"ResourceTagMappingList": []},  # no token -> loop terminates
        ]
    )

    def _fake_create(service, credentials, region):
        return tagging

    mod.create_boto3_client = _fake_create

    result = mod.list_private_components(
        {"is_default_credentials": True}, "us-west-2", {}
    )

    assert result == []  # no tagged resources -> no components
    assert len(tagging.calls) == 2
    # First call: empty cursor -> no PaginationToken key.
    assert "PaginationToken" not in tagging.calls[0]
    assert tagging.calls[0]["ResourcesPerPage"] == 100
    assert tagging.calls[0]["TagFilters"] == [
        {"Key": "dda-portal:managed", "Values": ["true"]}
    ]
    # Second call: follows the returned token.
    assert tagging.calls[1]["PaginationToken"] == "PAGE2"


# Validates: Requirements 3.7
def test_s7_single_page_terminates_immediately():
    """A single page with no token pages exactly once."""
    mod = _load_components()
    tagging = _FakeTaggingClient(pages=[{"ResourceTagMappingList": []}])
    mod.create_boto3_client = lambda service, credentials, region: tagging

    result = mod.list_private_components(
        {"is_default_credentials": True}, "us-west-2", {}
    )
    assert result == []
    assert len(tagging.calls) == 1
    assert "PaginationToken" not in tagging.calls[0]
