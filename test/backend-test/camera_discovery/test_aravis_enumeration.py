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
"""Unit tests for the injectable Aravis enumeration module.

Feature: aravis-camera-input (Requirements 2.1, 2.3, 2.6, 2.7).

Example tests of the injectable construction, the default lazy-import
fallback, and failure isolation. The property tests (stable id determinism,
enumeration completeness) live in separate ``test_property_*`` modules.
"""
import hashlib
import sys

from camera_discovery import (
    AravisDiscoveryResult,
    DiscoveredAravisCamera,
    aravis_stable_id,
    enumerate_aravis,
)


class FakeAravisCamera:
    """The attribute shape of ``model.Camera`` from aravis_functions."""

    def __init__(
        self,
        id="Aravis-Fake-GV01",
        model="Fake GV Camera",
        address="192.168.0.10",
        physical_id="fake-phys-0",
        protocol="Fake",
        serial="GV01",
        vendor="Aravis",
    ):
        self.id = id
        self.model = model
        self.address = address
        self.physical_id = physical_id
        self.protocol = protocol
        self.serial = serial
        self.vendor = vendor


class TestInjectableConstruction:
    def test_injected_enumerator_cameras_mapped_with_identity(self):
        # Requirements 2.1, 2.3, 2.7: injected enumerator drives the pass,
        # every identity field is captured on the discovered camera.
        fake = FakeAravisCamera(
            id="Basler-12345678",
            model="acA1920-40gc",
            address="192.168.1.20",
            physical_id="00:30:53:2a:bc:de",
            protocol="GigEVision",
            serial="12345678",
            vendor="Basler",
        )
        result = enumerate_aravis(enumerator=lambda: [fake])

        assert result.failures == []
        (camera,) = result.cameras
        assert camera.camera_id == "Basler-12345678"
        assert camera.model == "acA1920-40gc"
        assert camera.address == "192.168.1.20"
        assert camera.physical_id == "00:30:53:2a:bc:de"
        assert camera.protocol == "GigEVision"
        assert camera.serial == "12345678"
        assert camera.vendor == "Basler"
        assert camera.stable_id == aravis_stable_id(
            "Basler", "acA1920-40gc", "12345678", "00:30:53:2a:bc:de"
        )

    def test_multiple_cameras_each_contribute_one_entry(self):
        cams = [
            FakeAravisCamera(id="A", serial="s1", vendor="V1"),
            FakeAravisCamera(id="B", serial="s2", vendor="V2"),
        ]
        result = enumerate_aravis(enumerator=lambda: cams)

        assert [c.camera_id for c in result.cameras] == ["A", "B"]
        assert len({c.stable_id for c in result.cameras}) == 2

    def test_discovered_camera_is_frozen(self):
        result = enumerate_aravis(enumerator=lambda: [FakeAravisCamera()])
        (camera,) = result.cameras
        assert isinstance(camera, DiscoveredAravisCamera)
        try:
            camera.camera_id = "other"
            raised = False
        except AttributeError:
            raised = True
        assert raised

    def test_empty_enumeration_yields_empty_result(self):
        result = enumerate_aravis(enumerator=lambda: [])
        assert result.cameras == []
        assert result.failures == []


class TestStableIdDerivation:
    def test_stable_id_shape_and_derivation(self):
        expected = (
            "arv-"
            + hashlib.sha1("Basler|acA1920|12345678".encode()).hexdigest()[:12]
        )
        assert aravis_stable_id("Basler", "acA1920", "12345678") == expected

    def test_serial_present_ignores_physical_id(self):
        a = aravis_stable_id("V", "M", "S", "phys-1")
        b = aravis_stable_id("V", "M", "S", "phys-2")
        assert a == b

    def test_empty_serial_falls_back_to_physical_id(self):
        a = aravis_stable_id("V", "M", "", "phys-1")
        b = aravis_stable_id("V", "M", "", "phys-2")
        assert a != b
        assert a.startswith("arv-")


class TestDefaultLazyImportFallback:
    def test_missing_aravis_stack_yields_failure_record(self, monkeypatch):
        # Requirements 2.6, 2.7: the default enumerator lazily imports the
        # aravis_functions module; when the gi/Aravis stack is absent the
        # result is empty with a failure record — never an exception.
        monkeypatch.setitem(
            sys.modules, "edge_ml1_p_camera_management.aravis_functions", None
        )
        monkeypatch.setitem(sys.modules, "edge_ml1_p_camera_management", None)

        result = enumerate_aravis()

        assert isinstance(result, AravisDiscoveryResult)
        assert result.cameras == []
        assert len(result.failures) == 1
        assert "error" in result.failures[0]

    def test_default_enumerator_used_when_import_succeeds(self, monkeypatch):
        # Requirement 2.1: the default maps aravis_functions.getCameras().
        import types

        fake_module = types.SimpleNamespace(
            getCameras=lambda: [FakeAravisCamera(id="Aravis-Fake-GV01")]
        )
        fake_package = types.ModuleType("edge_ml1_p_camera_management")
        fake_package.aravis_functions = fake_module
        monkeypatch.setitem(
            sys.modules, "edge_ml1_p_camera_management", fake_package
        )
        monkeypatch.setitem(
            sys.modules,
            "edge_ml1_p_camera_management.aravis_functions",
            fake_module,
        )

        result = enumerate_aravis()

        assert [c.camera_id for c in result.cameras] == ["Aravis-Fake-GV01"]
        assert result.failures == []


class TestFailureIsolation:
    def test_raising_enumerator_yields_failure_record_not_exception(self):
        # Requirement 2.6
        def broken():
            raise RuntimeError("bus exploded")

        result = enumerate_aravis(enumerator=broken)

        assert result.cameras == []
        (failure,) = result.failures
        assert "bus exploded" in failure["error"]

    def test_camera_with_no_usable_identity_recorded_and_skipped(self):
        # Design Error Handling: no runtime id / no bus-stable identity
        # fields -> failures list, pass continues with the good cameras.
        no_id = FakeAravisCamera(id="")
        no_identity = FakeAravisCamera(
            id="ghost", model="", serial="", vendor="", physical_id=""
        )
        good = FakeAravisCamera(id="Good-1", serial="s-good")

        result = enumerate_aravis(
            enumerator=lambda: [no_id, good, no_identity]
        )

        assert [c.camera_id for c in result.cameras] == ["Good-1"]
        assert len(result.failures) == 2
        assert all("error" in f for f in result.failures)

    def test_none_identity_fields_coerced_not_crashing(self):
        cam = FakeAravisCamera(
            id="With-Nones", address=None, protocol=None, serial=None
        )
        result = enumerate_aravis(enumerator=lambda: [cam])

        (camera,) = result.cameras
        assert camera.address == ""
        assert camera.protocol == ""
        assert camera.serial == ""
        assert result.failures == []
