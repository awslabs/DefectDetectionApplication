"""Stage 50 — the Edge MCP server, read-only part (design "Edge — on-hardware
harness", steps (1), (2), (4), (5), (6); Requirements 16.7, 13.3, 8.5, 12.3).

Capability-gated on ``mcp``. The stage:

1. reads ``GET /mcp/status`` through the authenticated harness client
   (``client.login()`` with the configured ``env:``/``file:`` credential
   reference; no password is stored);
2. connects the official ``mcp`` ``Client`` to ``{base_url}/mcp`` with the
   harness session's bearer header (never logged), lists the tools and calls
   the read-only tools the design names per area present:
   ``edge_system_health_get``, ``edge_registrations_list``,
   ``edge_cameras_list``, ``edge_image_sources_list``,
   ``edge_feature_configurations_list``, ``edge_static_image_pin_get``,
   ``edge_streams_capabilities_get`` (``stream_cameras``),
   ``edge_execution_output_image_get`` on the latest execution,
   ``edge_image_source_preview`` on the first camera or stream source
   (exactly one ``ImageContent``), ``edge_workflow_images_list`` with no
   ``limit`` on the harness workflow when the device serves that route for
   it (a digital-input-triggered workflow with its process running; skipped
   otherwise), ``edge_results_list``, ``edge_registration_get``
   (``executions`` dropped) and ``edge_textgen_models_list`` (``vllm``);
4. asserts ``/health`` stays healthy and the restart counts stay unchanged
   across the stage and for ``timeouts.mcp_soak_s`` (``DDA_HARNESS_MCP_SOAK_S``,
   default 600 s): the MCP child's ``restartCount`` from ``/mcp/status``
   always, the backend container's ``RestartCount`` when the harness runs on
   the device with a usable ``docker`` CLI (a remote harness cannot observe
   it; the stage records that);
5. negatives at the proxy: a corrupted token -> 401 (local-login devices),
   an ``Origin`` header -> 403;
6. with ``DDA_HARNESS_ON_DEVICE=true``, direct loopback checks on
   ``http://127.0.0.1:5790/mcp``: ``Origin: http://127.0.0.1:5000`` -> 403,
   and no ``Authorization`` on a local-login device -> an ``isError``
   ``not_authenticated`` tool result; skipped when the harness runs remotely.

Step (3), the mutating tools with restoration, belongs to Phase 2.

The ``mcp`` SDK needs Python 3.10 or later (README "MCP stage"). When it is
not importable every test of the stage skips with that reason, so the other
stages keep running under the host's Python.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest

try:
    import anyio
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client
except ImportError as err:  # host Python 3.9, or no mcp package installed
    MCP_IMPORT_ERROR: Optional[str] = f"{type(err).__name__}: {err}"
else:
    MCP_IMPORT_ERROR = None

pytestmark = [pytest.mark.stage("mcp"), pytest.mark.capability("mcp")]

#: The child's loopback listener (on-device checks, step 6).
LOOPBACK_MCP_URL = "http://127.0.0.1:5790/mcp"
#: The Backend_API origin a browser on the device would send (D28).
LOOPBACK_ORIGIN = "http://127.0.0.1:5000"
#: An origin no deployment admits (step 5).
FOREIGN_ORIGIN = "https://harness-origin.invalid"
#: Image_Source types that can be previewed (cameras and stream sources).
PREVIEW_SOURCE_TYPES = ("Camera", "ICam", "NvidiaCSI", "RTSP", "RTMP")
#: The route bound of ``edge_workflow_images_list`` (``maxResults`` maximum).
WORKFLOW_IMAGES_MAX = 2
#: ``edge_results_list`` page size the stage asks for.
RESULTS_LIMIT = 5
#: Seconds between soak samples (step 4).
SOAK_INTERVAL_S = 30.0
MODERN_REVISION = "2026-07-28"
#: The read-only tools of step (2) that every LocalServer serves.
AREA_TOOLS = (
    "edge_system_health_get",
    "edge_registrations_list",
    "edge_cameras_list",
    "edge_image_sources_list",
    "edge_feature_configurations_list",
    "edge_static_image_pin_get",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _mcp_sdk_importable():
    """Module-scoped, so pytest sets it up before the module-scoped
    ``mcp_baseline``: without the SDK the stage skips before any MCP call."""
    if MCP_IMPORT_ERROR is not None:
        pytest.skip(
            "the mcp package is not importable under this interpreter "
            f"({MCP_IMPORT_ERROR}); run the stage under Python 3.11 with "
            "mcp==2.3.0 (README 'MCP stage')"
        )


def _session_authorization(edge_client) -> Optional[str]:
    """The harness session's ``Authorization`` header value, or None. It is
    passed to the MCP client as is and never formatted into a message."""
    return edge_client._session.headers.get("Authorization")


def _run_mcp(url: str, coroutine: Callable, authorization: Optional[str]) -> Any:
    """``coroutine(client)`` with the official SDK client against ``url``."""
    headers = {"Authorization": authorization} if authorization else {}

    async def main():
        async with httpx2.AsyncClient(headers=headers, timeout=150.0) as http:
            async with Client(streamable_http_client(url, http_client=http)) as client:
                return await coroutine(client)

    return anyio.run(main)


def _call(harness_target, edge_client, name: str, arguments: Optional[dict] = None):
    return _run_mcp(
        f"{harness_target.base_url.rstrip('/')}/mcp",
        lambda client: client.call_tool(name, arguments or {}),
        _session_authorization(edge_client),
    )


def _content(result) -> Dict[str, Any]:
    return result.structured_content or {}


def _assert_ok(name: str, result) -> Dict[str, Any]:
    content = _content(result)
    assert not result.is_error, (
        f"{name} returned an error result: category={content.get('category')!r} "
        f"message={content.get('message')!r}"
    )
    return content


def _images(result) -> List[Any]:
    return [item for item in result.content if getattr(item, "type", None) == "image"]


def _path_values(value: Any, key: Optional[str] = None) -> Iterator[tuple]:
    """``(key, value)`` for every string under a key ending in ``Path``."""
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _path_values(child, child_key)
    elif isinstance(value, list):
        for child in value:
            yield from _path_values(child, key)
    elif isinstance(value, str) and key is not None and key.endswith("Path"):
        yield key, value


def _items(result_value: Any, key: str) -> List[Dict[str, Any]]:
    if isinstance(result_value, list):
        return result_value
    if isinstance(result_value, dict) and isinstance(result_value.get(key), list):
        return result_value[key]
    return []


def _proxy_post(edge_client, harness_target, headers: Dict[str, str]):
    """One raw ``tools/list`` POST to ``{base_url}/mcp`` through the harness
    session (its own Authorization header unless ``headers`` replaces it)."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MODERN_REVISION,
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MODERN_REVISION,
        "Mcp-Method": "tools/list",
    }
    request_headers.update(headers)
    return edge_client._session.post(
        f"{harness_target.base_url.rstrip('/')}/mcp",
        data=json.dumps(body),
        headers=request_headers,
        timeout=30,
    )


def _mcp_status(edge_client) -> Dict[str, Any]:
    return edge_client._request("GET", "/mcp/status").json()


def _health(edge_client) -> Dict[str, Any]:
    return edge_client._request("GET", "/health").json()


def _container_restart_counts() -> Optional[Dict[str, int]]:
    """``{container name: RestartCount}`` of the backend containers, read
    with the local ``docker`` CLI, or None when it cannot be read."""
    if shutil.which("docker") is None:
        return None
    try:
        ids = subprocess.run(
            ["docker", "ps", "-q"], capture_output=True, text=True, timeout=30, check=True
        ).stdout.split()
        if not ids:
            return None
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{.Name}} {{.RestartCount}}", *ids],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    counts = {}
    for line in out.splitlines():
        name, _, count = line.strip().rpartition(" ")
        if "backend" in name and count.isdigit():
            counts[name.lstrip("/")] = int(count)
    return counts or None


# ---------------------------------------------------------------------------
# Session fixtures of the stage
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mcp_baseline(harness_target, edge_client) -> Dict[str, Any]:
    """The stage's starting point for step (4): the child's restart count,
    the backend containers' restart counts (on the device only) and the
    clock. It requests no function-scoped fixture (``record_metric``);
    ``test_health_and_restart_counts_hold`` records what it observed."""
    status = _mcp_status(edge_client)
    containers = _container_restart_counts() if harness_target.on_device else None
    return {
        "restartCount": status.get("restartCount"),
        "containers": containers,
        "started": time.monotonic(),
    }


@pytest.fixture(scope="module")
def harness_workflow_id(harness_target, edge_client) -> str:
    """The harness workflow: the first ``expected.workflows`` entry, by
    workflowId or name; the dependent checks skip when none is configured."""
    expected = harness_target.expected.workflows
    if not expected:
        pytest.skip(f"no workflows expected for device {harness_target.name}")
    wanted = expected[0]
    for workflow in edge_client.workflows():
        if wanted in (workflow.get("workflowId"), workflow.get("name")):
            return workflow["workflowId"]
    pytest.fail(f"expected workflow {wanted!r} is not reported by the device")


# ---------------------------------------------------------------------------
# (1) GET /mcp/status through the authenticated harness client
# ---------------------------------------------------------------------------


def test_mcp_status(edge_client, mcp_baseline, record_metric):
    status = _mcp_status(edge_client)
    assert status.get("enabled") is True, f"/mcp/status reports enabled={status.get('enabled')!r}"
    assert status.get("listening") is True, (
        f"the MCP child is not listening: lastExitCode={status.get('lastExitCode')!r} "
        f"lastError={status.get('lastError')!r}"
    )
    assert status.get("endpoint") == "/mcp"
    assert status.get("transport") == "streamable-http"
    assert MODERN_REVISION in (status.get("protocolRevisions") or [])
    assert status.get("deliveredPhases") == [1]
    assert isinstance(status.get("mutatingToolsEnabled"), bool)
    assert status.get("restartCount") == mcp_baseline["restartCount"]
    record_metric("mcp_server_version", status.get("serverVersion"))
    record_metric("mcp_component_version", status.get("componentVersion"))


# ---------------------------------------------------------------------------
# (2) the official client: list, then the read-only tools per area
# ---------------------------------------------------------------------------


def test_tools_list_is_read_only(harness_target, edge_client, mcp_baseline, record_metric):
    listed = _run_mcp(
        f"{harness_target.base_url.rstrip('/')}/mcp",
        lambda client: client.list_tools(),
        _session_authorization(edge_client),
    )
    names = [tool.name for tool in listed.tools]
    assert names == sorted(names), "tools/list is not sorted by name"
    missing = [name for name in AREA_TOOLS if name not in names]
    assert not missing, f"tools/list lacks {missing}"
    mutating = [t.name for t in listed.tools if not t.annotations.read_only_hint]
    assert mutating == [], f"Phase 1 lists mutating tools: {mutating}"
    record_metric("mcp_tools_listed", len(names))


@pytest.mark.parametrize("name", AREA_TOOLS)
def test_area_read_only_tool(harness_target, edge_client, mcp_baseline, name):
    _assert_ok(name, _call(harness_target, edge_client, name))


def test_streams_capabilities(harness_target, edge_client, mcp_baseline):
    if not harness_target.profile.grants("stream_cameras"):
        pytest.skip(
            f"capability 'stream_cameras' not granted by device profile {harness_target.name}"
        )
    _assert_ok(
        "edge_streams_capabilities_get",
        _call(harness_target, edge_client, "edge_streams_capabilities_get"),
    )


def test_execution_output_image(harness_target, edge_client, mcp_baseline):
    registrations = edge_client.workflow_registrations()
    if not registrations:
        pytest.skip("the device has no workflow registration, so no execution")
    execution_id = None
    for registration in registrations:
        executions = _items(
            _assert_ok(
                "edge_registration_executions_list",
                _call(
                    harness_target,
                    edge_client,
                    "edge_registration_executions_list",
                    {"registration_id": registration["registrationId"], "limit": 1},
                ),
            ).get("result"),
            "executions",
        )
        if executions:
            execution_id = executions[0].get("executionId")
            break
    if not execution_id:
        pytest.skip("no registration has an execution yet")
    result = _call(
        harness_target,
        edge_client,
        "edge_execution_output_image_get",
        {"execution_id": execution_id},
    )
    if result.is_error:
        # An execution whose workflow writes no output image answers not_found.
        assert _content(result).get("category") == "not_found", _content(result).get("category")
        return
    images = _images(result)
    assert len(images) == 1, f"expected one ImageContent, got {len(images)}"
    assert images[0].mime_type.startswith("image/")


def test_image_source_preview(harness_target, edge_client, mcp_baseline):
    sources = [
        source
        for source in edge_client.image_sources()
        if source.get("type") in PREVIEW_SOURCE_TYPES
    ]
    if not sources:
        pytest.skip("the device has no camera or stream Image_Source to preview")
    result = _call(
        harness_target,
        edge_client,
        "edge_image_source_preview",
        {"image_source_id": sources[0]["imageSourceId"]},
    )
    content = _assert_ok("edge_image_source_preview", result)
    assert content.get("category") != "result_too_large"
    images = _images(result)
    assert len(images) == 1, f"expected exactly one ImageContent, got {len(images)}"
    assert images[0].mime_type.startswith("image/"), images[0].mime_type


def test_workflow_images_default_limit(
    harness_target, edge_client, mcp_baseline, harness_workflow_id
):
    try:
        edge_client.workflow_images(harness_workflow_id)
    except Exception as err:  # noqa: BLE001 - the route serves no other workflow
        pytest.skip(
            f"GET /workflows/{harness_workflow_id}/images is not served for the "
            "harness workflow (it serves a digital-input-triggered workflow with "
            f"its process running): {type(err).__name__}"
        )
    content = _assert_ok(
        "edge_workflow_images_list",
        _call(
            harness_target,
            edge_client,
            "edge_workflow_images_list",
            {"workflow_id": harness_workflow_id},
        ),
    )
    images = _items(content.get("result"), "images")
    assert len(images) <= WORKFLOW_IMAGES_MAX, f"{len(images)} items for the default limit"
    with_slash = [key for key, value in _path_values(content.get("result")) if "/" in value]
    assert with_slash == [], f"path values with '/' under keys {with_slash}"


def test_results_list_bounds(harness_target, edge_client, mcp_baseline, harness_workflow_id):
    content = _assert_ok(
        "edge_results_list",
        _call(
            harness_target,
            edge_client,
            "edge_results_list",
            {"workflow_id": harness_workflow_id, "limit": RESULTS_LIMIT},
        ),
    )
    results = (content.get("result") or {}).get("results") or []
    assert len(results) <= RESULTS_LIMIT, f"{len(results)} results for limit {RESULTS_LIMIT}"
    assert not [item for item in results if isinstance(item, dict) and "maskImage" in item]


def test_registration_get_drops_executions(harness_target, edge_client, mcp_baseline):
    registrations = edge_client.workflow_registrations()
    if not registrations:
        pytest.skip("the device has no workflow registration")
    content = _assert_ok(
        "edge_registration_get",
        _call(
            harness_target,
            edge_client,
            "edge_registration_get",
            {"registration_id": registrations[0]["registrationId"]},
        ),
    )
    assert "executions" not in (content.get("result") or {})
    assert "executions" in (content.get("dropped_fields") or [])


def test_textgen_models_list(harness_target, edge_client, mcp_baseline):
    if not harness_target.profile.grants("vllm"):
        pytest.skip(f"capability 'vllm' not granted by device profile {harness_target.name}")
    _assert_ok(
        "edge_textgen_models_list",
        _call(harness_target, edge_client, "edge_textgen_models_list"),
    )


# ---------------------------------------------------------------------------
# (5) negatives at the proxy
# ---------------------------------------------------------------------------


def test_corrupted_token_is_401(harness_target, edge_client, mcp_baseline):
    if not harness_target.profile.grants("auth_enabled"):
        pytest.skip("no Local_Session_Token on a device without local login")
    authorization = _session_authorization(edge_client) or ""
    corrupted = authorization[:-4] + ("AAAA" if not authorization.endswith("AAAA") else "BBBB")
    response = _proxy_post(edge_client, harness_target, {"Authorization": corrupted})
    assert response.status_code == 401, f"corrupted token answered {response.status_code}"


def test_origin_is_403(harness_target, edge_client, mcp_baseline):
    control = _proxy_post(edge_client, harness_target, {})
    assert control.status_code == 200, f"the control request answered {control.status_code}"
    response = _proxy_post(edge_client, harness_target, {"Origin": FOREIGN_ORIGIN})
    assert response.status_code == 403, f"an Origin header answered {response.status_code}"


# ---------------------------------------------------------------------------
# (6) direct loopback checks (on the device only)
# ---------------------------------------------------------------------------


def test_on_device_loopback(harness_target, edge_client, mcp_baseline):
    if not harness_target.on_device:
        pytest.skip("the harness runs remotely (DDA_HARNESS_ON_DEVICE is not true)")
    import requests

    response = requests.post(
        LOOPBACK_MCP_URL,
        data=b"{}",
        headers={"Content-Type": "application/json", "Origin": LOOPBACK_ORIGIN},
        timeout=30,
    )
    assert response.status_code == 403, f"loopback Origin answered {response.status_code}"
    if harness_target.profile.grants("auth_enabled"):
        result = _run_mcp(
            LOOPBACK_MCP_URL,
            lambda client: client.call_tool("edge_system_health_get", {}),
            None,
        )
        assert result.is_error
        assert _content(result).get("category") == "not_authenticated"


# ---------------------------------------------------------------------------
# (4) health and restart counts across the stage and the soak window
# ---------------------------------------------------------------------------


def test_health_and_restart_counts_hold(harness_target, edge_client, mcp_baseline, record_metric):
    record_metric(
        "mcp_container_restart_count_observable",
        mcp_baseline["containers"] is not None,
    )
    soak_s = harness_target.timeouts.mcp_soak_s
    deadline = time.monotonic() + soak_s
    samples = 0
    while True:
        health = _health(edge_client)
        assert health.get("status") == "ok", f"/health reports {health!r}"
        restarts = _mcp_status(edge_client).get("restartCount")
        assert restarts == mcp_baseline["restartCount"], (
            f"the MCP child restarted during the stage: restartCount "
            f"{mcp_baseline['restartCount']!r} -> {restarts!r}"
        )
        samples += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(SOAK_INTERVAL_S, remaining))
    if mcp_baseline["containers"] is not None:
        assert (
            _container_restart_counts() == mcp_baseline["containers"]
        ), "a backend container restarted during the stage"
    record_metric("mcp_soak_s", soak_s)
    record_metric("mcp_soak_samples", samples)
    record_metric("mcp_stage_s", round(time.monotonic() - mcp_baseline["started"], 1))
