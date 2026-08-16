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
"""Fix-check tests for ``GET /feature-configurations/gpu-status`` (task 4.2,
design fix-check case 6 + the route-registration unit test).

- The gpu-status route returns the ``device_gpu_status`` aggregate computed
  from the Active_Provider_Records of the non-base/marshal Triton models.
- The no-Triton and empty-repo guards return the non-degraded EMPTY shape
  WITHOUT standing up a Triton server (the empty-repo hang guard).
- Route registration does not shadow the ``/models/{name}/start|stop``
  routes (first-match inspection over the router's path regexes).

Import reality (task 3.4 outcome, recorded honestly): ``endpoints.
feature_config`` does not import bare host-side for PRE-EXISTING reasons —
its top pulls ``utils.server_setup`` (gstreamer -> ``import gi``, a
container-only dep) and ``dao.sqlite_db`` (requires ``COMPONENT_WORK_PATH``),
plus ``dda_triton.triton_edge_client`` (tritonclient, container-only). Those
pre-existing container deps are stubbed here (sys.modules pre-seed, the
suite's established pattern) together with the suite's awsiot stubs;
everything THIS spec added imports for real.

Honesty guard: GPU-free, host-runnable; no real ORT/Triton/IPC.
"""
import datetime
import os
import sys
import tempfile
import types

import pytest
from unittest.mock import MagicMock, patch

from model_gpu_fallback_visibility.fakes import (
    build_model_tree,
    import_with_awsiot_stubs,
    make_record,
    seed_active_provider_record,
)

# dao.sqlite_db resolves COMPONENT_WORK_PATH at import time (pre-existing).
os.environ.setdefault(
    "COMPONENT_WORK_PATH",
    tempfile.mkdtemp(prefix="dda-gpu-status-endpoint-test-"))


def _import_endpoint_module():
    """Import ``endpoints.feature_config`` with the PRE-EXISTING
    container-only deps stubbed (``utils.server_setup``,
    ``dda_triton.triton_edge_client``) plus the suite's awsiot stubs.
    Stub modules are dropped from ``sys.modules`` after the import so
    nothing leaks into other test modules; the endpoint module object
    keeps its own bound references.
    """
    installed = []

    def _register(name, module):
        if name in sys.modules:
            return
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module
            installed.append(name)

    server_setup = types.ModuleType("utils.server_setup")
    server_setup.input_cfg_accessor = MagicMock()
    server_setup.output_cfg_accessor = MagicMock()
    server_setup.lfv_edge_agent = MagicMock()
    server_setup.iot_shadow_accessor = MagicMock()
    _register("utils.server_setup", server_setup)

    triton_edge_client = types.ModuleType("dda_triton.triton_edge_client")

    class _StubTritonEdgeClient:
        @staticmethod
        def get_instance():
            raise AssertionError(
                "host-side test: TritonEdgeClient.get_instance must be "
                "patched by the test, never reached for real")

    triton_edge_client.TritonEdgeClient = _StubTritonEdgeClient
    _register("dda_triton.triton_edge_client", triton_edge_client)

    try:
        return import_with_awsiot_stubs("endpoints.feature_config")
    finally:
        for name in installed:
            sys.modules.pop(name, None)


feature_config = _import_endpoint_module()
pv = feature_config.provider_visibility
model_status_shadow = feature_config.model_status_shadow


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Keep the shadow reporter inert (no thing name -> report is a no-op)
    and neutralize the module-level transition state around every test."""
    monkeypatch.delenv("AWS_IOT_THING_NAME", raising=False)
    pv._last_gpu_degraded = None
    model_status_shadow._reset_state()
    yield
    pv._last_gpu_degraded = None
    model_status_shadow._reset_state()


def _empty_shape_asserts(response):
    assert response["gpuDegraded"] is False
    assert response["gpuChainModels"] == 0
    assert response["gpuActiveModels"] == 0
    assert response["models"] == {}
    datetime.datetime.strptime(response["updatedAt"], "%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The gpu-status route returns the aggregate (design fix-check case 6)
# ---------------------------------------------------------------------------

def test_gpu_status_returns_device_aggregate_from_records():
    """With Triton up and records seeded, the handler returns the
    ``device_gpu_status`` aggregate over the non-base/marshal models:
    two GPU-chain fallback records -> gpuDegraded true, base_/marshal_
    listing entries filtered out, record-less models excluded from the
    per-model map (Decision 6)."""
    fake_server = MagicMock()
    fake_server.list_triton_models.return_value = [
        {"model_component": "base_yolo", "status": "READY"},     # filtered
        {"model_component": "marshal_yolo", "status": "READY"},  # filtered
        {"model_component": "yolo", "status": "READY"},
        {"model_component": "seg", "status": "READY"},
        {"model_component": "norecord", "status": "LOADING"},    # no record
    ]
    fake_client = MagicMock()
    fake_client.get_instance.return_value = fake_server

    with tempfile.TemporaryDirectory() as repo:
        for name in ("yolo", "seg"):
            tree = build_model_tree(repo, name)
            seed_active_provider_record(
                tree["version_dir"],
                make_record(f"base_{name}_1", gpu_requested=True,
                            gpu_active=False))

        with patch.object(feature_config, "get_is_triton",
                          return_value=True), \
                patch.object(feature_config.feature_configs_utils,
                             "triton_repo_has_models", return_value=True), \
                patch.object(feature_config, "TritonEdgeClient",
                             fake_client), \
                patch.object(pv, "TRITON_MODEL_DIR", repo):
            response = feature_config.get_gpu_status()

    assert response["gpuDegraded"] is True
    assert response["gpuChainModels"] == 2
    assert response["gpuActiveModels"] == 0
    # Only the recorded, non-base/marshal models appear in the map.
    assert set(response["models"]) == {"yolo", "seg"}
    for name in ("yolo", "seg"):
        assert response["models"][name] == {
            "status": "READY",
            "runtime": "onnx",
            "gpuRequested": True,
            "gpuActive": False,
        }
    datetime.datetime.strptime(response["updatedAt"], "%Y-%m-%dT%H:%M:%SZ")


def test_gpu_status_healthy_records_not_degraded():
    """A single healthy-GPU record -> gpuDegraded false with the counts
    reflecting the active GPU-chain model (the non-degraded aggregate leg
    of case 6)."""
    fake_server = MagicMock()
    fake_server.list_triton_models.return_value = [
        {"model_component": "yolo", "status": "READY"},
    ]
    fake_client = MagicMock()
    fake_client.get_instance.return_value = fake_server

    with tempfile.TemporaryDirectory() as repo:
        tree = build_model_tree(repo, "yolo")
        seed_active_provider_record(
            tree["version_dir"],
            make_record("base_yolo_1", gpu_requested=True, gpu_active=True))

        with patch.object(feature_config, "get_is_triton",
                          return_value=True), \
                patch.object(feature_config.feature_configs_utils,
                             "triton_repo_has_models", return_value=True), \
                patch.object(feature_config, "TritonEdgeClient",
                             fake_client), \
                patch.object(pv, "TRITON_MODEL_DIR", repo):
            response = feature_config.get_gpu_status()

    assert response["gpuDegraded"] is False
    assert response["gpuChainModels"] == 1
    assert response["gpuActiveModels"] == 1
    assert response["models"]["yolo"]["gpuActive"] is True


# ---------------------------------------------------------------------------
# Guards: empty repo / no Triton return the empty shape WITHOUT a server
# (design fix-check case 6, the empty-repo hang guard)
# ---------------------------------------------------------------------------

def test_no_triton_guard_returns_empty_shape_without_server():
    """``get_is_triton()`` false -> the non-degraded empty shape; the
    repo check short-circuits and no Triton server is ever stood up."""
    fake_client = MagicMock()
    repo_has_models = MagicMock(return_value=True)

    with patch.object(feature_config, "get_is_triton",
                      return_value=False), \
            patch.object(feature_config.feature_configs_utils,
                         "triton_repo_has_models", repo_has_models), \
            patch.object(feature_config, "TritonEdgeClient", fake_client):
        response = feature_config.get_gpu_status()

    _empty_shape_asserts(response)
    repo_has_models.assert_not_called()          # short-circuited
    fake_client.get_instance.assert_not_called()  # no server stood up


def test_empty_repo_guard_returns_empty_shape_without_server():
    """Triton enabled but the model repository is EMPTY -> the non-degraded
    empty shape, and the Triton server is NOT created (creating it against
    an empty repo blocks indefinitely — the pre-existing hang guard the
    route must honor)."""
    fake_client = MagicMock()

    with patch.object(feature_config, "get_is_triton",
                      return_value=True), \
            patch.object(feature_config.feature_configs_utils,
                         "triton_repo_has_models", return_value=False), \
            patch.object(feature_config, "TritonEdgeClient", fake_client):
        response = feature_config.get_gpu_status()

    _empty_shape_asserts(response)
    fake_client.get_instance.assert_not_called()  # no server stood up


# ---------------------------------------------------------------------------
# Route registration does not shadow /models/{name}/start|stop
# (design Unit Tests)
# ---------------------------------------------------------------------------

def test_route_registration_does_not_shadow_model_routes():
    """First-match dispatch over the router's path regexes (FastAPI routes
    in registration order): every concrete URL resolves to ITS handler —
    the gpu-status route neither shadows nor is shadowed by the list route
    or the ``/models/{modelName}/start|stop`` routes."""
    from fastapi.routing import APIRoute

    routes = [r for r in feature_config.router.routes
              if isinstance(r, APIRoute)]
    paths = [r.path for r in routes]

    for expected in (
            "/feature-configurations",
            "/feature-configurations/gpu-status",
            "/feature-configurations/models/{modelName}/start",
            "/feature-configurations/models/{modelName}/stop"):
        assert expected in paths, f"route {expected} missing from router"

    def first_match(url):
        for route in routes:
            if route.path_regex.match(url):
                return route
        return None

    cases = {
        "/feature-configurations":
            feature_config.list_feature_configs,
        "/feature-configurations/gpu-status":
            feature_config.get_gpu_status,
        "/feature-configurations/models/model-x/start":
            feature_config.start_feature_config,
        "/feature-configurations/models/model-x/stop":
            feature_config.stop_feature_configs,
    }
    for url, handler in cases.items():
        route = first_match(url)
        assert route is not None, f"no route matches {url}"
        assert route.endpoint is handler, (
            f"{url} dispatches to {route.path} ({route.endpoint.__name__}) "
            f"instead of {handler.__name__} — route shadowing")
