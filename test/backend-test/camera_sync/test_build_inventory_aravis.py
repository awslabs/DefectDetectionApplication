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
"""Unit tests for the Aravis branch of the ``build_inventory`` merge.

Feature: aravis-camera-input (Requirements 2.1, 2.3, 2.4, 7.2).
"""
from camera_discovery import (
    DiscoveredAravisCamera,
    DiscoveredCamera,
    DiscoveryResult,
    InventorySnapshot,
    TrackedCamera,
    aravis_stable_id,
)
from camera_sync import (
    ORIGIN_EDGE_CONFIGURED,
    ORIGIN_EDGE_DISCOVERED,
    TYPE_ARAVIS_DISCOVERED,
    CameraSourceState,
    build_inventory,
)


def make_aravis_camera(camera_id="Aravis-Fake-GV01", model="Fake GV",
                       address="192.168.1.20", physical_id="eth0",
                       protocol="GigEVision", serial="SN-0001",
                       vendor="Aravis"):
    return DiscoveredAravisCamera(
        stable_id=aravis_stable_id(vendor, model, serial, physical_id),
        camera_id=camera_id,
        model=model,
        address=address,
        physical_id=physical_id,
        protocol=protocol,
        serial=serial,
        vendor=vendor,
    )


def make_v4l2_camera(stable_id="disc-000000000001", device_path="/dev/video0",
                     card_name="Fake Cam", bus_info="usb-0000:00:14.0-1",
                     driver="uvcvideo", kind="v4l2", formats=None):
    return DiscoveredCamera(
        stable_id=stable_id,
        device_path=device_path,
        card_name=card_name,
        bus_info=bus_info,
        driver=driver,
        kind=kind,
        formats=formats if formats is not None else [
            {"pixel_format": "YUYV", "resolutions": [[1920, 1080], [1280, 720]]}
        ],
    )


def make_camera_image_source(image_source_id="is-1", name="Line 1 cam",
                             camera_id="Aravis-Fake-GV01", type_="Camera",
                             gain=4, exposure=5000000):
    return {
        "imageSourceId": image_source_id,
        "name": name,
        "type": type_,
        "cameraId": camera_id,
        "imageSourceConfiguration": {"gain": gain, "exposure": exposure},
    }


class TestConfiguredAravisMergeByCameraId:
    def test_camera_id_match_merges_into_one_configured_entry(self):
        """Requirement 2.4: configured Camera + discovered Aravis by
        cameraId -> ONE entry under cfg-{imageSourceId}."""
        camera = make_aravis_camera()
        source = make_camera_image_source(camera_id=camera.camera_id)

        entries = build_inventory([source], DiscoveryResult(cameras=[camera]))

        assert len(entries) == 1
        entry = entries[0]
        assert entry.camera_source_id == "cfg-is-1"
        assert entry.type == "Camera"
        assert entry.origin == ORIGIN_EDGE_CONFIGURED
        assert entry.discovered is True
        assert entry.absent is False
        # Configured params retained (cameraId, gain, exposure).
        assert entry.params["cameraId"] == camera.camera_id
        assert entry.params["gain"] == 4
        assert entry.params["exposure"] == 5000000
        # Discovered identity metadata under capabilities.aravis.
        assert entry.capabilities["aravis"] == {
            "model": "Fake GV",
            "address": "192.168.1.20",
            "physicalId": "eth0",
            "protocol": "GigEVision",
            "serial": "SN-0001",
            "vendor": "Aravis",
        }

    def test_merged_camera_contributes_to_exactly_one_entry(self):
        camera = make_aravis_camera()
        source = make_camera_image_source(camera_id=camera.camera_id)

        entries = build_inventory([source], DiscoveryResult(cameras=[camera]))

        ids = [entry.camera_source_id for entry in entries]
        assert camera.stable_id not in ids  # merged, not duplicated
        assert len(ids) == len(set(ids))

    def test_non_matching_camera_id_reports_separately(self):
        camera = make_aravis_camera()
        source = make_camera_image_source(camera_id="Other-Camera-99")

        entries = build_inventory([source], DiscoveryResult(cameras=[camera]))

        by_id = {entry.camera_source_id: entry for entry in entries}
        assert set(by_id) == {"cfg-is-1", camera.stable_id}
        assert by_id["cfg-is-1"].discovered is False
        assert by_id["cfg-is-1"].capabilities == {}

    def test_non_camera_type_source_never_merges_by_camera_id(self):
        camera = make_aravis_camera()
        source = make_camera_image_source(
            camera_id=camera.camera_id, type_="Folder"
        )

        entries = build_inventory([source], DiscoveryResult(cameras=[camera]))

        by_id = {entry.camera_source_id: entry for entry in entries}
        assert set(by_id) == {"cfg-is-1", camera.stable_id}
        assert by_id["cfg-is-1"].discovered is False


class TestDiscoveredOnlyAravisEntry:
    def test_discovered_only_entry_shape(self):
        """Requirements 2.1, 2.3: unmerged Aravis camera -> AravisDiscovered
        / edge-discovered entry with identity params and capabilities."""
        camera = make_aravis_camera()

        entries = build_inventory([], DiscoveryResult(cameras=[camera]))

        assert len(entries) == 1
        entry = entries[0]
        assert entry.camera_source_id == camera.stable_id
        assert entry.camera_source_id.startswith("arv-")
        assert entry.name == "Aravis Fake GV"
        assert entry.type == TYPE_ARAVIS_DISCOVERED
        assert entry.origin == ORIGIN_EDGE_DISCOVERED
        assert entry.discovered is True
        assert entry.absent is False
        assert entry.absent_since is None
        assert entry.params == {
            "cameraId": "Aravis-Fake-GV01",
            "serial": "SN-0001",
            "protocol": "GigEVision",
            "address": "192.168.1.20",
        }
        assert entry.capabilities == {
            "aravis": {
                "model": "Fake GV",
                "address": "192.168.1.20",
                "physicalId": "eth0",
                "protocol": "GigEVision",
                "serial": "SN-0001",
                "vendor": "Aravis",
            }
        }

    def test_absent_state_propagates_from_snapshot(self):
        camera = make_aravis_camera()
        snapshot = InventorySnapshot(
            cameras={
                camera.stable_id: TrackedCamera(
                    camera=camera, absent=True, absent_since=1729990000000
                )
            }
        )

        entries = build_inventory([], snapshot)

        assert len(entries) == 1
        assert entries[0].absent is True
        assert entries[0].absent_since == 1729990000000

    def test_absent_state_propagates_into_merged_configured_entry(self):
        camera = make_aravis_camera()
        source = make_camera_image_source(camera_id=camera.camera_id)
        snapshot = InventorySnapshot(
            cameras={
                camera.stable_id: TrackedCamera(
                    camera=camera, absent=True, absent_since=1729990000000
                )
            }
        )

        entries = build_inventory([source], snapshot)

        assert len(entries) == 1
        entry = entries[0]
        assert entry.camera_source_id == "cfg-is-1"
        assert entry.discovered is True
        assert entry.absent is True
        assert entry.absent_since == 1729990000000


class TestNoAravisOutputIdentity:
    def test_no_aravis_inputs_produce_pre_feature_output(self):
        """Requirement 7.2: expectation captured from the pre-feature
        build_inventory behavior for the same configured + V4L2 inputs."""
        present = make_v4l2_camera()
        gone = make_v4l2_camera(
            stable_id="disc-000000000002",
            device_path="/dev/video2",
            card_name="Gone Cam",
            bus_info="usb-0000:00:14.0-2",
            formats=[],
        )
        sources = [
            {
                "imageSourceId": "is-1",
                "name": "Line 1 cam",
                "type": "Camera",
                "cameraId": "cam-1",
                "imageSourceConfiguration": {
                    "gain": 4, "exposure": 5000000, "device": "/dev/video0"
                },
            },
            {
                "imageSourceId": "is-2",
                "name": "Folder source",
                "type": "Folder",
                "imageSourceConfiguration": {},
            },
        ]
        snapshot = InventorySnapshot(
            cameras={
                present.stable_id: TrackedCamera(camera=present),
                gone.stable_id: TrackedCamera(
                    camera=gone, absent=True, absent_since=1729990000000
                ),
            }
        )

        entries = build_inventory(sources, snapshot)

        # Captured verbatim from the pre-feature implementation's output.
        assert entries == [
            CameraSourceState(
                camera_source_id="cfg-is-1",
                name="Line 1 cam",
                type="Camera",
                origin="edge-configured",
                params={
                    "devicePath": "/dev/video0",
                    "cameraId": "cam-1",
                    "gain": 4,
                    "exposure": 5000000,
                },
                capabilities={
                    "formats": [
                        {
                            "pixelFormat": "YUYV",
                            "resolutions": [[1920, 1080], [1280, 720]],
                        }
                    ],
                    "driver": "uvcvideo",
                    "busInfo": "usb-0000:00:14.0-1",
                    "kind": "v4l2",
                },
                discovered=True,
                absent=False,
                absent_since=None,
            ),
            CameraSourceState(
                camera_source_id="cfg-is-2",
                name="Folder source",
                type="Folder",
                origin="edge-configured",
                params={},
                capabilities={},
                discovered=False,
                absent=False,
                absent_since=None,
            ),
            CameraSourceState(
                camera_source_id="disc-000000000002",
                name="Gone Cam",
                type="V4L2Discovered",
                origin="edge-discovered",
                params={"devicePath": "/dev/video2"},
                capabilities={
                    "formats": [],
                    "driver": "uvcvideo",
                    "busInfo": "usb-0000:00:14.0-2",
                    "kind": "v4l2",
                },
                discovered=True,
                absent=True,
                absent_since=1729990000000,
            ),
        ]


class TestMixedFamilies:
    def test_v4l2_and_aravis_families_report_side_by_side(self):
        """Aravis entries join the inventory without disturbing the V4L2
        path merge; every discovered camera contributes exactly once."""
        v4l2_camera = make_v4l2_camera()
        aravis_camera = make_aravis_camera()
        v4l2_source = {
            "imageSourceId": "is-1",
            "name": "V4L2 cam",
            "type": "Camera",
            "cameraId": "cam-1",
            "imageSourceConfiguration": {"device": "/dev/video0"},
        }
        aravis_source = make_camera_image_source(
            image_source_id="is-2",
            name="GenICam cam",
            camera_id=aravis_camera.camera_id,
        )

        entries = build_inventory(
            [v4l2_source, aravis_source],
            DiscoveryResult(cameras=[v4l2_camera, aravis_camera]),
        )

        by_id = {entry.camera_source_id: entry for entry in entries}
        assert set(by_id) == {"cfg-is-1", "cfg-is-2"}
        assert by_id["cfg-is-1"].capabilities["driver"] == "uvcvideo"
        assert "aravis" not in by_id["cfg-is-1"].capabilities
        assert by_id["cfg-is-2"].capabilities["aravis"]["vendor"] == "Aravis"
        assert all(entry.discovered for entry in entries)
