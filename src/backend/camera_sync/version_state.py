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
"""Per-source version counters for the Edge_Sync_Agent (Requirement 1.1's
monotonically increasing version), persisted locally in
``/aws_dda/camera_sync_state.json``.

- A source's version bumps exactly when its content hash changes.
- The state file is written atomically (temp file in the same directory +
  ``os.replace``), so a crash mid-write leaves the previous state intact.
- A missing or corrupt state file only resets version counters *upward*:
  the next assignment floors every version at ``max(version reported in the
  shadow's current reported state) + 1``, so the Portal's version guard
  (Requirement 3.5) never discards the post-loss report.
"""
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict
from typing import Dict, Iterable, Mapping, Optional

from camera_sync.inventory import CameraSourceState

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = "/aws_dda/camera_sync_state.json"

_STATE_SCHEMA_VERSION = 1


def compute_content_hash(entry: CameraSourceState) -> str:
    """Deterministic content hash of everything the Portal sees for a
    source (absence transitions included — they must version-bump so the
    Portal's staleness guard accepts them)."""
    content = asdict(entry)
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def versions_from_reported(reported: Optional[Mapping]) -> Dict[str, int]:
    """Extract ``{camera_source_id: version}`` from the shadow's current
    reported document (the re-floor source after state-file loss)."""
    versions: Dict[str, int] = {}
    if not reported:
        return versions
    cameras = reported.get("cameras") or {}
    if not isinstance(cameras, Mapping):
        return versions
    for camera_source_id, entry in cameras.items():
        if not isinstance(entry, Mapping):
            continue
        version = entry.get("version")
        if isinstance(version, int) and version > 0:
            versions[str(camera_source_id)] = version
    return versions


def assign_versions(
    previous: Optional[Mapping[str, Mapping]],
    inventory: Iterable[CameraSourceState],
    reported_versions: Optional[Mapping[str, int]] = None,
) -> Dict[str, Dict]:
    """Pure version assignment for one inventory pass.

    ``previous`` is the loaded state (``{csid: {"version", "content_hash"}}``)
    or ``None`` when the state file was missing or corrupt. Returns the new
    state map for the same shape.

    - known source, unchanged hash: version kept
    - known source, changed hash: version + 1
    - new source: floored at the shadow's reported version for that id + 1
    - ``previous is None``: every version floored at
      ``max(all reported versions) + 1`` (upward-only reset)
    """
    reported_versions = dict(reported_versions or {})
    state: Dict[str, Dict] = {}

    if previous is None:
        floor = max(reported_versions.values(), default=0) + 1
        for entry in inventory:
            state[entry.camera_source_id] = {
                "version": floor,
                "content_hash": compute_content_hash(entry),
            }
        return state

    for entry in inventory:
        csid = entry.camera_source_id
        content_hash = compute_content_hash(entry)
        known = previous.get(csid)
        if known and known.get("content_hash") == content_hash and isinstance(
            known.get("version"), int
        ):
            version = known["version"]
        elif known and isinstance(known.get("version"), int):
            version = known["version"] + 1
        else:
            version = reported_versions.get(csid, 0) + 1
        state[csid] = {"version": version, "content_hash": content_hash}
    return state


class CameraSyncStateStore:
    """Atomic persistence for the version state file.

    The path is injectable so tests use a tmp directory; the on-device
    default is ``/aws_dda/camera_sync_state.json``.
    """

    def __init__(self, path: str = DEFAULT_STATE_PATH):
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> Optional[Dict[str, Dict]]:
        """The persisted ``{csid: {"version", "content_hash"}}`` map, or
        ``None`` when the file is missing or corrupt (the caller re-floors
        from the shadow's reported state)."""
        try:
            with open(self._path, "r", encoding="utf-8") as state_file:
                raw = json.load(state_file)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning(
                "Camera sync state file %s is unreadable or corrupt; "
                "re-flooring versions from the shadow's reported state",
                self._path,
            )
            return None

        sources = raw.get("sources") if isinstance(raw, dict) else None
        if not isinstance(sources, dict):
            logger.warning(
                "Camera sync state file %s has an unexpected shape; "
                "re-flooring versions from the shadow's reported state",
                self._path,
            )
            return None
        return sources

    def save(self, sources: Mapping[str, Mapping]) -> None:
        """Atomic write: temp file in the target directory, then rename."""
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        payload = {
            "schemaVersion": _STATE_SCHEMA_VERSION,
            "sources": dict(sources),
        }
        fd, temp_path = tempfile.mkstemp(
            prefix=".camera_sync_state.", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
                json.dump(payload, temp_file, sort_keys=True)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self._path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def advance(
        self,
        inventory: Iterable[CameraSourceState],
        reported_versions: Optional[Mapping[str, int]] = None,
    ) -> Dict[str, int]:
        """Load → assign → persist; returns ``{csid: version}`` for the
        current inventory. This is the Edge_Sync_Agent's per-report call."""
        previous = self.load()
        state = assign_versions(previous, inventory, reported_versions)
        self.save(state)
        return {csid: entry["version"] for csid, entry in state.items()}
