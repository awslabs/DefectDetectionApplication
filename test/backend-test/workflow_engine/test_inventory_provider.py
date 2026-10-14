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
"""The PRODUCTION camera inventory provider — the closure that actually
ships, not an injected substitute.

Bugfix: `.kiro/specs/static-camera-workflow-binding-invisible/`, task 3.1,
bugfix.md Property 3.

This is the test that would have caught the bug. Every other camera-binding
test in the repo injects its own `inventory_provider` lambda, and every
`build_inventory` static-camera test calls the function directly with
explicit kwargs — so the one closure that ships
(`runtime._camera_binding_dependencies()`) was never exercised, and its
missing `static_image_pinned` argument went unnoticed through two specs that
added and then depended on it (bugfix.md 1.9).

The keyword arguments are asserted directly, so a future kwarg omission fails
here rather than on hardware.

_Requirements: 2.1, 2.5, 2.7, Property 3_
"""
import sys
import types

import pytest

from utils.static_image_camera import STATIC_IMAGE_CAMERA_ID


# --------------------------------------------------------------- fixtures

@pytest.fixture
def wired(tmp_path, monkeypatch):
    """`runtime._camera_binding_dependencies()` with only its on-device
    reach-outs substituted.

    Returns a namespace carrying the provider factory plus the knobs each
    test needs: the store base dir, and a recorder of the `build_inventory`
    calls the closure makes.
    """
    # BEFORE any import: dao.sqlite_db.sqlite_db_operations reads
    # COMPONENT_WORK_PATH at module import time to build its SQLite URL.
    monkeypatch.setenv("COMPONENT_WORK_PATH", str(tmp_path))

    import dao.sqlite_db.sqlite_db_operations as dao_module
    import utils
    import utils.static_image_camera as static_module
    import workflow_engine.camera_binding_store as store_module
    from workflow_engine import runtime

    class _Accessor:
        def list_image_sources(self, _filter, _session):
            return []

    fake_server_setup = types.ModuleType("utils.server_setup")
    fake_server_setup.iot_shadow_accessor = object()
    fake_server_setup.camera_discovery = None
    fake_server_setup.image_source_accessor = _Accessor()
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

    monkeypatch.setattr(store_module, "CameraBindingStore",
                        _CameraBindingStore)

    # Record the keyword arguments the closure passes, without changing the
    # merge's behaviour.
    import camera_sync.inventory as inventory_module

    calls = []
    real_build_inventory = inventory_module.build_inventory

    def recording_build_inventory(*args, **kwargs):
        calls.append(kwargs)
        return real_build_inventory(*args, **kwargs)

    monkeypatch.setattr(inventory_module, "build_inventory",
                        recording_build_inventory)

    def make_store():
        return static_module.StaticImageStore(
            base_dir=str(tmp_path / "static_image_camera")
        )

    def install_store(store):
        monkeypatch.setattr(static_module, "_store", store, raising=False)

    return types.SimpleNamespace(
        runtime=runtime,
        calls=calls,
        make_store=make_store,
        install_store=install_store,
        static_module=static_module,
        tmp_path=tmp_path,
    )


def pin_an_image(store, tmp_path, name="seed.png"):
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", (16, 12), color=(1, 2, 3)).save(path)
    store.pin_bytes(path.read_bytes(), name)
    return store


def entry_ids(inventory):
    return {
        entry.camera_source_id if hasattr(entry, "camera_source_id")
        else entry["cameraSourceId"]
        for entry in inventory
    }


# -------------------------------------------------------------- the tests

def test_provider_surfaces_the_static_camera_while_pinned(wired):
    """Pinned → the virtual entry is present, with the production shape
    (`params: {}`, identity under `capabilities.staticImage`).

    _Requirements: 2.1_
    """
    store = pin_an_image(wired.make_store(), wired.tmp_path)
    wired.install_store(store)

    _store, provider = wired.runtime._camera_binding_dependencies()
    inventory = provider()

    assert STATIC_IMAGE_CAMERA_ID in entry_ids(inventory)
    entry = next(
        item for item in inventory
        if item.camera_source_id == STATIC_IMAGE_CAMERA_ID
    )
    assert entry.params == {}
    assert entry.capabilities["staticImage"]["id"] == STATIC_IMAGE_CAMERA_ID


def test_provider_passes_the_static_image_keyword_arguments(wired):
    """The regression guard for the omission itself: assert on the call, not
    only on its result, so dropping the kwarg again fails here.

    `static_image_absent_since` is deliberately NOT passed (design.md
    Decision 3) — the local resolver never reads `absent`, and the value's
    meaning is shadow-merge-specific.

    _Requirements: 2.1, 2.6, 2.7_
    """
    store = pin_an_image(wired.make_store(), wired.tmp_path)
    wired.install_store(store)

    _store, provider = wired.runtime._camera_binding_dependencies()
    provider()

    assert wired.calls, "the provider never called build_inventory"
    kwargs = wired.calls[-1]
    assert kwargs.get("static_image_pinned") is True
    assert kwargs.get("static_image_metadata") is not None
    assert "static_image_absent_since" not in kwargs, (
        "the workflow provider must not pass static_image_absent_since "
        "(design.md Decision 3)"
    )


def test_provider_omits_the_static_camera_when_nothing_is_pinned(wired):
    """Unpinned → no entry at all, per design.md Decision 3's recorded
    choice. The registration flips to invalid within one poll tick and the
    existing re-resolution hooks restore it when an image is pinned again.

    _Requirements: 2.6_
    """
    wired.install_store(wired.make_store())

    _store, provider = wired.runtime._camera_binding_dependencies()
    inventory = provider()

    assert STATIC_IMAGE_CAMERA_ID not in entry_ids(inventory)
    kwargs = wired.calls[-1]
    assert kwargs.get("static_image_pinned") is False
    assert "static_image_absent_since" not in kwargs


def test_a_failing_pin_store_does_not_empty_the_inventory(wired,
                                                          monkeypatch):
    """Requirement 2.5, the one that matters most.

    `get_store()` raises `KeyError` when `COMPONENT_WORK_PATH` is unset, and
    `watcher._local_inventory()` turns ANY provider exception into `{}` —
    which would mark every camera binding on the device invalid. The guard
    must therefore contain the failure and still return the other sources.
    """
    from camera_sync import CameraSourceState

    def exploding_get_store():
        raise KeyError("COMPONENT_WORK_PATH")

    monkeypatch.setattr(wired.static_module, "get_store",
                        exploding_get_store)

    configured = CameraSourceState(
        camera_source_id="cfg-present",
        name="a real camera",
        type="Camera",
        origin="edge-configured",
        params={"cameraId": "Aravis-Real-01"},
        capabilities={},
        discovered=False,
    )

    import camera_sync.inventory as inventory_module

    monkeypatch.setattr(
        inventory_module, "build_inventory",
        lambda *args, **kwargs: [configured],
    )

    _store, provider = wired.runtime._camera_binding_dependencies()
    inventory = provider()

    assert entry_ids(inventory) == {"cfg-present"}, (
        "a pin-store failure emptied or broke the inventory; the watcher "
        "would then invalidate every camera binding on the device"
    )
