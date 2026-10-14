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
"""Unit tests for the Edge_Sync_Agent pure merge and local version state.

Feature: camera-registry-sync (Requirements 1.1, 2.5, 11.3).
"""
import json

import pytest

from camera_discovery import DiscoveredCamera, DiscoveryResult, InventorySnapshot, TrackedCamera
from camera_sync import (
    ORIGIN_EDGE_CONFIGURED,
    ORIGIN_EDGE_DISCOVERED,
    TYPE_V4L2_DISCOVERED,
    CameraSyncStateStore,
    assign_versions,
    build_inventory,
)


def make_camera(stable_id="disc-000000000001", device_path="/dev/video0",
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


def make_image_source(image_source_id="is-1", name="Line 1 cam", type_="Camera",
                      camera_id="cam-1", device=None, gain=4, exposure=5000000):
    configuration = {"gain": gain, "exposure": exposure}
    if device is not None:
        configuration["device"] = device
    return {
        "imageSourceId": image_source_id,
        "name": name,
        "type": type_,
        "cameraId": camera_id,
        "imageSourceConfiguration": configuration,
    }


class TestBuildInventoryMerge:
    def test_path_match_merges_into_one_configured_entry(self):
        """Requirement 2.5: configured + discovered on the same path -> ONE entry."""
        camera = make_camera(device_path="/dev/video0")
        source = make_image_source(device="/dev/video0")

        entries = build_inventory([source], DiscoveryResult(cameras=[camera]))

        assert len(entries) == 1
        entry = entries[0]
        assert entry.camera_source_id == "cfg-is-1"
        assert entry.origin == ORIGIN_EDGE_CONFIGURED
        assert entry.discovered is True
        assert entry.params["devicePath"] == "/dev/video0"
        assert entry.params["cameraId"] == "cam-1"
        assert entry.params["gain"] == 4
        assert entry.capabilities["formats"] == [
            {"pixelFormat": "YUYV", "resolutions": [[1920, 1080], [1280, 720]]}
        ]

    def test_discovered_only_hardware_reports_edge_discovered(self):
        camera = make_camera()
        entries = build_inventory([], DiscoveryResult(cameras=[camera]))

        assert len(entries) == 1
        entry = entries[0]
        assert entry.camera_source_id == camera.stable_id
        assert entry.origin == ORIGIN_EDGE_DISCOVERED
        assert entry.type == TYPE_V4L2_DISCOVERED
        assert entry.discovered is True
        assert entry.params == {"devicePath": "/dev/video0"}

    def test_configured_without_matching_path_reports_undiscovered(self):
        source = make_image_source(device="/dev/video9")
        camera = make_camera(device_path="/dev/video0")

        entries = build_inventory([source], DiscoveryResult(cameras=[camera]))

        by_id = {entry.camera_source_id: entry for entry in entries}
        assert set(by_id) == {"cfg-is-1", camera.stable_id}
        assert by_id["cfg-is-1"].discovered is False
        assert by_id["cfg-is-1"].capabilities == {}

    def test_no_discovered_camera_appears_in_two_entries(self):
        camera = make_camera(device_path="/dev/video0")
        source = make_image_source(device="/dev/video0")

        entries = build_inventory([source], DiscoveryResult(cameras=[camera]))

        ids = [entry.camera_source_id for entry in entries]
        assert camera.stable_id not in ids  # merged, not duplicated
        assert len(ids) == len(set(ids))

    def test_absence_propagates_from_snapshot(self):
        camera = make_camera()
        snapshot = InventorySnapshot(
            cameras={
                camera.stable_id: TrackedCamera(
                    camera=camera, absent=True, absent_since=1729990000000
                )
            }
        )
        entries = build_inventory([], snapshot)

        assert entries[0].absent is True
        assert entries[0].absent_since == 1729990000000


class TestVersionState:
    def test_version_kept_when_content_unchanged_and_bumped_on_change(self, tmp_path):
        store = CameraSyncStateStore(path=str(tmp_path / "state.json"))
        entry = build_inventory([make_image_source()], DiscoveryResult())[0]

        first = store.advance([entry])
        second = store.advance([entry])
        assert first == second == {"cfg-is-1": 1}

        changed = build_inventory(
            [make_image_source(name="Renamed cam")], DiscoveryResult()
        )[0]
        third = store.advance([changed])
        assert third == {"cfg-is-1": 2}

    def test_missing_state_file_refloors_from_reported_versions(self, tmp_path):
        store = CameraSyncStateStore(path=str(tmp_path / "state.json"))
        entry = build_inventory([make_image_source()], DiscoveryResult())[0]

        versions = store.advance([entry], reported_versions={"cfg-is-1": 7, "disc-x": 9})
        assert versions == {"cfg-is-1": 10}  # max(reported) + 1

    def test_corrupt_state_file_refloors_from_reported_versions(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not json", encoding="utf-8")
        store = CameraSyncStateStore(path=str(path))
        entry = build_inventory([make_image_source()], DiscoveryResult())[0]

        versions = store.advance([entry], reported_versions={"cfg-is-1": 3})
        assert versions == {"cfg-is-1": 4}

    def test_state_file_written_atomically_and_round_trips(self, tmp_path):
        path = tmp_path / "state.json"
        store = CameraSyncStateStore(path=str(path))
        entry = build_inventory([make_image_source()], DiscoveryResult())[0]

        store.advance([entry])
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["schemaVersion"] == 1
        assert persisted["sources"]["cfg-is-1"]["version"] == 1
        # No leftover temp files from the atomic write.
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_assign_versions_new_source_floors_at_reported_version(self):
        entry = build_inventory([make_image_source()], DiscoveryResult())[0]
        state = assign_versions({}, [entry], reported_versions={"cfg-is-1": 5})
        assert state["cfg-is-1"]["version"] == 6
