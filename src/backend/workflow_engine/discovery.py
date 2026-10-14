#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Workflow_Component artifact discovery and validation (Requirement 9.1).

Greengrass delivers Workflow_Component artifacts to

    /aws_dda/workflows/{workflowId}/{version}/
        manifest.json
        workflow.json
        compiled_pipeline.json
        plugins/<arch>/*.so        (optional)
        python/<nodeId>/...        (optional)

``scan_workflow_root`` enumerates candidate artifact sets and
``validate_artifact_set`` classifies each one:

- ``registered``: well-formed and compatible — runnable.
- ``invalid``: malformed (missing files, bad JSON) or incompatible
  (manifest ``minLocalServerVersion`` above the running LocalServer
  version, or ``targetArch`` different from this device's architecture).
  Invalid sets are still registered — with the reason reported — but can
  never be run (Requirement 13.3: a broken component never disturbs the
  device; the failure is surfaced through status reporting instead).

Pure functions over the filesystem; no database access, so the logic is
directly unit-testable with temporary directories.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

from workflow_engine import environment, gst_plugins

logger = logging.getLogger(__name__)

#: Root directory watched for Workflow_Component artifacts.
WORKFLOWS_ROOT = "/aws_dda/workflows"

MANIFEST_FILE = "manifest.json"
WORKFLOW_FILE = "workflow.json"
COMPILED_PIPELINE_FILE = "compiled_pipeline.json"

#: Files an artifact set must contain to be runnable.
REQUIRED_FILES = (MANIFEST_FILE, WORKFLOW_FILE, COMPILED_PIPELINE_FILE)

STATUS_REGISTERED = "registered"
STATUS_INVALID = "invalid"

# Non-active registration statuses (stale-workflow-registrations bugfix).
# ``removed``: the artifact directory no longer exists on disk (what the
# recipe's Shutdown cleanup produces on component replace/remove).
# ``superseded``: the directory is present but a higher numeric version of
# the same workflow is also on disk, so this version is not the deployed
# one. Rows with these statuses are preserved (execution history is never
# deleted) but are filtered from the default registrations listing and can
# never be triggered.
STATUS_REMOVED = "removed"
STATUS_SUPERSEDED = "superseded"

#: Statuses the default ``GET /workflows/registrations`` listing returns.
ACTIVE_STATUSES = (STATUS_REGISTERED, STATUS_INVALID)


@dataclass(frozen=True)
class DiscoveredArtifactSet:
    """One /{workflowId}/{version}/ directory found under the root."""

    workflow_id: str
    version: str
    path: str


@dataclass
class ArtifactValidation:
    """Classification of one artifact set."""

    status: str
    arch: str = "unknown"
    reason: Optional[str] = None
    manifest: Optional[dict] = None
    compiled_document: Optional[dict] = None

    @property
    def is_valid(self) -> bool:
        return self.status == STATUS_REGISTERED


def scan_workflow_root(root: str = WORKFLOWS_ROOT) -> List[DiscoveredArtifactSet]:
    """Enumerate every {workflowId}/{version} directory under ``root``.

    A missing root simply yields no artifact sets: devices that never
    received a Workflow_Component behave exactly as before
    (Requirement 13.6).
    """
    discovered: List[DiscoveredArtifactSet] = []
    if not os.path.isdir(root):
        return discovered

    for workflow_id in sorted(os.listdir(root)):
        workflow_dir = os.path.join(root, workflow_id)
        if not os.path.isdir(workflow_dir):
            continue
        for version in sorted(os.listdir(workflow_dir)):
            version_dir = os.path.join(workflow_dir, version)
            if not os.path.isdir(version_dir):
                continue
            discovered.append(
                DiscoveredArtifactSet(
                    workflow_id=workflow_id, version=version, path=version_dir
                )
            )
    return discovered


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        document = json.load(f)
    if not isinstance(document, dict):
        raise ValueError("top-level JSON value is not an object")
    return document


def validate_artifact_set(
    artifact_set: DiscoveredArtifactSet,
    device_arch: Optional[str] = None,
    running_version: Optional[str] = None,
    plugins_root: str = gst_plugins.DEVICE_PLUGINS_ROOT,
) -> ArtifactValidation:
    """Classify one artifact set as registered or invalid (with reason).

    ``device_arch`` / ``running_version`` default to this device's
    probed values; tests inject them explicitly. ``plugins_root`` is the
    Plugin_Component install root checksum verification resolves
    component-delivered plugin files under.
    """
    if device_arch is None:
        device_arch = environment.device_arch()
    if running_version is None:
        running_version = environment.local_server_version()

    # 1. Required files present
    for required in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(artifact_set.path, required)):
            return ArtifactValidation(
                status=STATUS_INVALID,
                reason=f"Missing required artifact file: {required}",
            )

    # 2. Parseable JSON
    try:
        manifest = _load_json(os.path.join(artifact_set.path, MANIFEST_FILE))
    except (ValueError, OSError) as e:
        return ArtifactValidation(
            status=STATUS_INVALID,
            reason=f"Malformed {MANIFEST_FILE}: {e}",
        )
    try:
        compiled_document = _load_json(
            os.path.join(artifact_set.path, COMPILED_PIPELINE_FILE)
        )
    except (ValueError, OSError) as e:
        return ArtifactValidation(
            status=STATUS_INVALID,
            reason=f"Malformed {COMPILED_PIPELINE_FILE}: {e}",
            manifest=manifest,
            arch=str(manifest.get("targetArch") or "unknown"),
        )

    arch = str(manifest.get("targetArch") or "unknown")

    # 3. Architecture matches this device
    if arch != device_arch:
        return ArtifactValidation(
            status=STATUS_INVALID,
            arch=arch,
            reason=(
                f"Artifact architecture '{arch}' does not match this "
                f"device's architecture '{device_arch}'"
            ),
            manifest=manifest,
            compiled_document=compiled_document,
        )

    # 4. LocalServer version satisfies the manifest minimum.
    #
    # LocalServer ships as independently-versioned per-architecture variants
    # whose version lineages are not comparable (the arm64 variant may be at
    # 1.0.124 while arm64_jp6 is at 1.0.35). A single scalar minimum is
    # therefore variant-blind: it would compare this device's own-lineage
    # version against a floor derived from a different lineage. When the
    # manifest carries the per-arch map ``minLocalServerVersions``, select the
    # floor for THIS device's arch (== the package's targetArch, checked in
    # step 3); fall back to the scalar ``minLocalServerVersion`` for manifests
    # packaged before the map existed.
    min_versions = manifest.get("minLocalServerVersions")
    if isinstance(min_versions, dict) and arch in min_versions:
        min_version = min_versions[arch]
    else:
        min_version = manifest.get("minLocalServerVersion")
    if min_version is not None:
        try:
            compatible = environment.is_version_compatible(
                str(min_version), running_version
            )
        except ValueError as e:
            return ArtifactValidation(
                status=STATUS_INVALID,
                arch=arch,
                reason=f"Malformed {MANIFEST_FILE}: {e}",
                manifest=manifest,
                compiled_document=compiled_document,
            )
        if not compatible:
            return ArtifactValidation(
                status=STATUS_INVALID,
                arch=arch,
                reason=(
                    f"Requires LocalServer >= {min_version} but this device "
                    f"runs {running_version}"
                ),
                manifest=manifest,
                compiled_document=compiled_document,
            )

    # 5. Compiled document must at least carry renderable segments
    if not isinstance(compiled_document.get("segments"), list):
        return ArtifactValidation(
            status=STATUS_INVALID,
            arch=arch,
            reason=f"Malformed {COMPILED_PIPELINE_FILE}: missing 'segments' list",
            manifest=manifest,
            compiled_document=compiled_document,
        )

    # 6. Every plugin file the manifest records a checksum for — inline
    # under plugins/<arch>/ or installed by a depended-on
    # Plugin_Component — must hash to that checksum (custom-node-designer
    # Requirement 10.6). A mismatch registers the workflow as invalid
    # with the failing file identified; it is reported but never runnable.
    verification = gst_plugins.verify_manifest_plugins(
        manifest, artifact_set.path, plugins_root=plugins_root
    )
    if not verification.ok:
        details = "; ".join(
            "{0} ({1}): {2}".format(
                key,
                gst_plugins.resolve_checksum_path(
                    key, manifest, artifact_set.path, plugins_root
                ),
                reason,
            )
            for key, reason in verification.failures
        )
        return ArtifactValidation(
            status=STATUS_INVALID,
            arch=arch,
            reason=f"Plugin checksum verification failed: {details}",
            manifest=manifest,
            compiled_document=compiled_document,
        )

    return ArtifactValidation(
        status=STATUS_REGISTERED,
        arch=arch,
        manifest=manifest,
        compiled_document=compiled_document,
    )
