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
"""Exploration suite — bug condition for the Static_Image_Camera being
invisible to workflow camera-binding resolution.

Bugfix: `.kiro/specs/static-camera-workflow-binding-invisible/`
(bugfix.md = requirements, design.md = source of truth), task 1.2.

These tests assert the POST-FIX expectations and therefore MUST FAIL on
unfixed code. They pin the two sequential defects separately, because the
first masks the second and a fix for only the first relocates the failure
from registration time to run time (bugfix.md 1.6-1.8):

* **Defect 1** — `workflow_engine/runtime.py`'s production
  `inventory_provider()` closure calls `build_inventory(image_sources,
  snapshot)` without `static_image_pinned`, so the virtual
  `static-image-camera` entry is never in the workflow inventory and
  `resolve_bindings` reports `missing camera source static-image-camera`.
* **Defect 2** — the virtual entry carries `params: {}` by design with its
  identity under `capabilities.staticImage`, and
  `_resolved_parameter_values` projects only `params`, so a *resolved*
  static binding yields an empty assignment,
  `aravis_feed._effective_values` discards the node's rendered `camera_id`,
  and the feed plan fails with "no camera id".

Recorded failure texts on unfixed code (task 1.2 outcome):

* Defect 1 — `resolve_bindings` returns `STATUS_INVALID` with
  `errors == ("missing camera source static-image-camera",)`, and
  `static-image-camera not in provider()`.
* Defect 2 — `AravisFeedError`: "Aravis camera source 'n2': no camera id:
  neither the resolved binding nor the rendered parameters carry a
  non-empty camera_id".

_Requirements: 1.1-1.8, 2.1-2.4, Property 1_
"""
import os
import sys
import types

import pytest

from camera_sync.inventory import build_inventory
from utils.static_image_camera import STATIC_IMAGE_CAMERA_ID
from workflow_engine.aravis_feed import AravisFeedError, plan_aravis_feeds
from workflow_engine.camera_binding import STATUS_RESOLVED, resolve_bindings


NODE_ID = "n2"


# --------------------------------------------------------------- fixtures

@pytest.fixture
def pinned_store(tmp_path, monkeypatch):
    """A real `StaticImageStore` over a temp dir holding a real pinned
    PNG, installed as the module singleton.

    A real store (not a stub) is what makes this an exploration test of the
    production path: `status()` does the same disk inspection and Pillow
    decode it does on device.
    """
    from PIL import Image

    import utils.static_image_camera as static_module

    monkeypatch.setenv("COMPONENT_WORK_PATH", str(tmp_path))
    store = static_module.StaticImageStore(
        base_dir=str(tmp_path / "static_image_camera")
    )
    image_path = tmp_path / "seed.png"
    Image.new("RGB", (32, 24), color=(10, 20, 30)).save(image_path)
    store.pin_bytes(image_path.read_bytes(), "seed.png")
    assert store.is_pinned(), "fixture failed to pin its seed image"

    monkeypatch.setattr(static_module, "_store", store, raising=False)
    return store


@pytest.fixture
def production_provider(pinned_store, monkeypatch):
    """The REAL `runtime._camera_binding_dependencies()` inventory provider.

    Substitutes only what the closure reaches out to — `utils.server_setup`
    (which connects Greengrass IPC at import time and exists on-device
    only), the SQLite session factory, and the binding store's shadow
    accessor — so the closure itself, including its `build_inventory` call,
    is the code under test. Every other camera-binding test in the repo
    injects its own `inventory_provider` lambda instead, which is precisely
    why this bug shipped (bugfix.md 1.9).
    """
    # Import the real modules FIRST so their own import chains resolve
    # (workflow_engine.models needs Base from the real dao module), then
    # substitute only the two attributes the closure reaches through.
    import dao.sqlite_db.sqlite_db_operations as dao_module
    import utils
    from workflow_engine import runtime

    class _Accessor:
        def list_image_sources(self, _filter, _session):
            return []

    fake_server_setup = types.ModuleType("utils.server_setup")
    fake_server_setup.iot_shadow_accessor = object()
    fake_server_setup.camera_discovery = None
    fake_server_setup.image_source_accessor = _Accessor()
    # `from utils import server_setup` resolves the package attribute, and
    # importlib consults sys.modules; set both. utils.server_setup connects
    # Greengrass IPC at import time and exists on-device only.
    monkeypatch.setitem(sys.modules, "utils.server_setup", fake_server_setup)
    monkeypatch.setattr(utils, "server_setup", fake_server_setup,
                        raising=False)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(dao_module, "SessionLocal", _Session)

    class _CameraBindingStore:
        def __init__(self, _accessor):
            pass

    import workflow_engine.camera_binding_store as store_module

    monkeypatch.setattr(store_module, "CameraBindingStore",
                        _CameraBindingStore)

    _store, provider = runtime._camera_binding_dependencies()
    return provider


# ---------------------------------------------------------------- helpers

def static_binding_document():
    """A compiled document with one Aravis binding point whose rendered
    parameters name a physical camera — the shape the Portal produces when
    a document authored for a physical camera is re-bound to the static
    camera (`static-image-camera-source` Requirement 4.5)."""
    return {
        "schemaVersion": 1,
        "segments": [
            {"elements": [{"nodeId": NODE_ID, "type": "appsrc", "args": {}}]}
        ],
        "bindingPoints": [
            {
                "nodeId": NODE_ID,
                "nodeType": "aravis_camera_source",
                "parameters": {
                    "camera_id": "Aravis-Fake-GV01",
                    "gain": 4,
                    "exposure": 5000000,
                },
                "slots": [],
                "aravisBinding": True,
            }
        ],
    }


def static_bindings():
    return {NODE_ID: {"cameraSourceId": STATIC_IMAGE_CAMERA_ID}}


def virtual_entry(pin_metadata=None):
    """The production virtual entry, built by the production merge so its
    shape (notably `params: {}`) is never a test invention."""
    entries = build_inventory(
        [], None, static_image_pinned=True, static_image_metadata=pin_metadata
    )
    matching = [
        entry for entry in entries
        if entry.camera_source_id == STATIC_IMAGE_CAMERA_ID
    ]
    assert len(matching) == 1, (
        f"expected exactly one virtual entry, got {len(matching)}"
    )
    return matching[0]


# ------------------------------------------------- Defect 1: the inventory

def test_production_provider_includes_the_static_camera_while_pinned(
    production_provider,
):
    """Defect 1. The shipped provider must surface the pinned virtual
    entry; today it passes no static-image arguments to `build_inventory`,
    whose defaults are falsy, so the entry can never appear.

    _Requirements: 2.1_
    """
    inventory = production_provider()
    ids = {
        entry.camera_source_id if hasattr(entry, "camera_source_id")
        else entry["cameraSourceId"]
        for entry in inventory
    }

    assert STATIC_IMAGE_CAMERA_ID in ids, (
        f"the production inventory provider omitted "
        f"{STATIC_IMAGE_CAMERA_ID!r} while an image was pinned; it calls "
        f"build_inventory without static_image_pinned "
        f"(workflow_engine/runtime.py), so every workflow bound to the "
        f"static camera is permanently invalid on device. Saw: {sorted(ids)}"
    )


def test_static_binding_resolves_against_the_production_inventory(
    production_provider,
):
    """Defect 1, at the resolver: the binding the Portal delivers must
    resolve, not report the camera missing.

    _Requirements: 2.2_
    """
    result = resolve_bindings(
        static_binding_document(), static_bindings(), production_provider()
    )

    assert result.status == STATUS_RESOLVED, (
        f"a binding to the pinned static camera did not resolve: "
        f"status={result.status} errors={result.errors}"
    )
    assert not result.errors
    assert result.missing == ()


# ----------------------------------------------- Defect 2: the feed plan

def test_resolved_static_binding_can_plan_its_frame_feed():
    """Defect 2, reached by handing the resolver the virtual entry
    directly — i.e. with Defect 1 stubbed past.

    This is the test that proves the one-line provider fix is not enough:
    the entry resolves, and then the run dies because the assignment's
    `params` is empty and `_effective_values` prefers it over the node's
    rendered parameters without merging.

    _Requirements: 2.3, 2.4_
    """
    document = static_binding_document()
    result = resolve_bindings(document, static_bindings(), [virtual_entry()])
    assert result.status == STATUS_RESOLVED, (
        "precondition failed: the virtual entry did not resolve even when "
        "supplied directly, so this test is not exercising Defect 2"
    )

    try:
        feeds = plan_aravis_feeds(result.document, result)
    except AravisFeedError as error:
        pytest.fail(
            f"a resolved static-camera binding could not plan its frame "
            f"feed: {error}. The virtual entry carries params={{}} with its "
            f"identity under capabilities.staticImage, and "
            f"_resolved_parameter_values projects only params, so the "
            f"assignment discards the node's rendered camera_id."
        )

    assert len(feeds) == 1, f"expected exactly one feed, got {len(feeds)}"
    assert feeds[0].camera_id == STATIC_IMAGE_CAMERA_ID, (
        f"the planned feed grabs from {feeds[0].camera_id!r} rather than "
        f"the static camera"
    )


def test_static_binding_end_to_end_from_the_production_provider(
    production_provider,
):
    """Both defects in one pass, the on-device sequence: the provider's
    inventory resolves the binding AND the resolution plans a feed that
    grabs from the static camera.

    _Requirements: 2.1-2.4, Property 1_
    """
    document = static_binding_document()
    result = resolve_bindings(document, static_bindings(), production_provider())
    assert result.status == STATUS_RESOLVED, (
        f"resolution failed before the feed plan: {result.errors}"
    )

    feeds = plan_aravis_feeds(result.document, result)

    assert [feed.camera_id for feed in feeds] == [STATIC_IMAGE_CAMERA_ID]
