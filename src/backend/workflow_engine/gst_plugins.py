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

"""Per-run GStreamer plugin path scoping for workflow runs (Requirement 9.2)
and manifest checksum verification of delivered plugins
(custom-node-designer Requirements 10.6, 11.4).

A Workflow_Component may deliver extra GStreamer plugins two ways:

- inline under ``{artifact_path}/plugins/<arch>/`` (built-in/curated
  plugins bundled by the Component_Packager), and
- via depended-on Plugin_Components that Greengrass installs under
  ``/aws_dda/plugins/{pluginId}/{pluginVersion}/{arch}/`` (custom
  plugins; the workflow's ``manifest.json`` names them in
  ``pluginComponents`` and records their expected SHA-256 checksums in
  ``pluginChecksums``).

Both locations must be loadable for the workflow's pipeline run without
any process-wide, permanent environment mutation that could leak into
Pipeline_Configuration execution (Requirements 13.1, 13.4). Before the
registry scan every plugin file referenced by ``pluginChecksums`` is
verified against its recorded checksum; a mismatch skips the plugin
(fail closed) and the same verification drives workflow registration
validity in :mod:`workflow_engine.discovery` (Requirement 10.6).
Bundled LocalServer plugins and Pipeline_Configuration execution are
never touched.

Two mechanisms, both scoped to the run:

1. **Environment prepend (restored afterwards)**: ``GST_PLUGIN_PATH`` is
   prepended with the component plugin directory for the duration of the
   run and restored on exit. Note that ``GstPipelineManager.run_pipeline``
   overwrites ``GST_PLUGIN_PATH`` from ``utils.get_gst_plugins_path()``
   at the start of every run (and GStreamer only reads the variable at
   first ``Gst.init`` anyway), so the prepend alone is not sufficient —
   it covers first-init ordering and any element-spawned subprocesses,
   and the restore guarantees no lasting mutation either way.

2. **Registry scan**: ``Gst.Registry.get().scan_path(dir)`` explicitly
   loads plugins from the component directory into the in-process
   registry before the pipeline is parsed. This is what actually makes
   the plugins available to ``Gst.parse_launch`` on an already-initialized
   process. The scan is additive: bundled LocalServer plugins are never
   removed or replaced, so Pipeline_Configuration pipelines are untouched.

GStreamer import happens lazily inside the scan so this module stays
importable (and the executor testable) without ``gi``.
"""

import hashlib
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

GST_PLUGIN_PATH_ENV = "GST_PLUGIN_PATH"

#: Where Greengrass Plugin_Components install their artifacts
#: (``/aws_dda/plugins/{pluginId}/{pluginVersion}/{arch}/``), mirroring
#: ``plugin_components.py``'s DEVICE_PLUGINS_ROOT.
DEVICE_PLUGINS_ROOT = "/aws_dda/plugins"

#: Plugin_Component naming convention: ``dda.plugin.{pluginId}`` with
#: component version ``{pluginVersion}.0.0``.
PLUGIN_COMPONENT_PREFIX = "dda.plugin."
_COMPONENT_VERSION_SUFFIX = ".0.0"

MANIFEST_PLUGIN_CHECKSUMS_KEY = "pluginChecksums"
MANIFEST_PLUGIN_COMPONENTS_KEY = "pluginComponents"
MANIFEST_TARGET_ARCH_KEY = "targetArch"


# ---------------------------------------------------------------------------
# Checksum verification (custom-node-designer Requirements 10.6, 11.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChecksumVerification:
    """Outcome of verifying a manifest's ``pluginChecksums``.

    ``verified`` holds the manifest keys whose file bytes hashed to the
    recorded checksum; ``failures`` holds ``(key, reason)`` pairs — the
    key identifies the failing plugin file (Requirement 10.6).
    """

    verified: Tuple[str, ...] = ()
    failures: Tuple[Tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


def verify_plugin_checksums(
    manifest: dict,
    read_bytes: Callable[[str], Optional[bytes]],
) -> ChecksumVerification:
    """Pure verification decision: (manifest, file-bytes lookup) ->
    verified keys + failures.

    For every ``pluginChecksums`` entry the delivered bytes (obtained
    through ``read_bytes(key)``; ``None`` means the file is missing)
    must hash (SHA-256) to the recorded checksum. A manifest without
    ``pluginChecksums`` entries verifies vacuously — workflows without
    custom plugins are unaffected.
    """
    checksums = manifest.get(MANIFEST_PLUGIN_CHECKSUMS_KEY) or {}
    if not isinstance(checksums, dict):
        return ChecksumVerification(
            failures=(
                (
                    "manifest.json#" + MANIFEST_PLUGIN_CHECKSUMS_KEY,
                    "pluginChecksums is not an object",
                ),
            )
        )

    verified: List[str] = []
    failures: List[Tuple[str, str]] = []
    for key in sorted(checksums):
        expected = checksums[key]
        if not isinstance(expected, str) or not expected:
            failures.append((key, "recorded checksum is not a string"))
            continue
        data = read_bytes(key)
        if data is None:
            failures.append((key, "plugin file is missing or unreadable"))
            continue
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected.strip().lower():
            failures.append(
                (
                    key,
                    "checksum mismatch: manifest records {0}, file is {1}".format(
                        expected, actual
                    ),
                )
            )
            continue
        verified.append(key)
    return ChecksumVerification(
        verified=tuple(verified), failures=tuple(failures)
    )


def _component_install_dir(
    component_name: str, component_version: str, arch: str, plugins_root: str
) -> str:
    """``/aws_dda/plugins/{pluginId}/{pluginVersion}/{arch}`` for one
    ``pluginComponents`` entry (``dda.plugin.{pluginId}`` at component
    version ``{pluginVersion}.0.0``)."""
    plugin_id = component_name
    if plugin_id.startswith(PLUGIN_COMPONENT_PREFIX):
        plugin_id = plugin_id[len(PLUGIN_COMPONENT_PREFIX):]
    version = str(component_version)
    if version.endswith(_COMPONENT_VERSION_SUFFIX):
        version = version[: -len(_COMPONENT_VERSION_SUFFIX)]
    return os.path.join(plugins_root, plugin_id, version, arch)


def resolve_checksum_path(
    key: str,
    manifest: dict,
    artifact_path: str,
    plugins_root: str = DEVICE_PLUGINS_ROOT,
) -> str:
    """Absolute path of one ``pluginChecksums`` entry.

    ``<pluginComponentName>/<file>`` keys resolve under the depended-on
    Plugin_Component's install root; anything else is an inline delivery
    relative to the workflow artifact directory (a bare file name means
    ``plugins/<arch>/<file>``).
    """
    components = manifest.get(MANIFEST_PLUGIN_COMPONENTS_KEY) or {}
    arch = str(manifest.get(MANIFEST_TARGET_ARCH_KEY) or "unknown")
    first, _, rest = key.partition("/")
    if rest and isinstance(components, dict) and first in components:
        install_dir = _component_install_dir(
            first, str(components[first]), arch, plugins_root
        )
        return os.path.join(install_dir, rest)
    if "/" in key:
        return os.path.join(artifact_path, key)
    return os.path.join(artifact_path, "plugins", arch, key)


def verify_manifest_plugins(
    manifest: dict,
    artifact_path: str,
    plugins_root: str = DEVICE_PLUGINS_ROOT,
) -> ChecksumVerification:
    """Verify every ``pluginChecksums`` entry against the delivered file
    bytes on disk (inline or Plugin_Component-installed)."""

    def read_bytes(key: str) -> Optional[bytes]:
        path = resolve_checksum_path(key, manifest, artifact_path, plugins_root)
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    return verify_plugin_checksums(manifest, read_bytes)


def plugin_scan_dirs(
    manifest: Optional[dict],
    artifact_path: str,
    plugins_root: str = DEVICE_PLUGINS_ROOT,
) -> List[str]:
    """Every directory the workflow's plugins may live in: the inline
    ``plugins/<arch>/`` directory plus the install root of each
    Plugin_Component named in the manifest (Requirement 11.4)."""
    manifest = manifest or {}
    arch = str(manifest.get(MANIFEST_TARGET_ARCH_KEY) or "unknown")
    dirs = [os.path.join(artifact_path, "plugins", arch)]
    components = manifest.get(MANIFEST_PLUGIN_COMPONENTS_KEY) or {}
    if isinstance(components, dict):
        for name in sorted(components):
            dirs.append(
                _component_install_dir(
                    name, str(components[name]), arch, plugins_root
                )
            )
    return dirs


def _scan_registry(plugin_dir: str) -> bool:
    """Load plugins from ``plugin_dir`` into the in-process GStreamer
    registry. Returns True when the registry changed.

    Failures are contained: a component with a broken plugin directory
    produces a normal pipeline error for that run instead of taking the
    process down (Requirement 13.7).
    """
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        changed = bool(Gst.Registry.get().scan_path(plugin_dir))
        logger.info(
            "Scanned workflow plugin directory %s (registry changed: %s)",
            plugin_dir,
            changed,
        )
        return changed
    except Exception:  # noqa: BLE001 - plugin loading must never propagate
        logger.exception(
            "Failed to scan workflow plugin directory %s", plugin_dir
        )
        return False


def _verified_scan_dirs(
    plugin_dir: str,
    manifest: Optional[dict],
    artifact_path: Optional[str],
    plugins_root: str,
) -> List[str]:
    """The existing directories to scan for this run, after checksum
    verification (Requirements 10.6, 11.4).

    Without a manifest this is just ``plugin_dir`` — the pre-existing
    inline behavior. With one, the Plugin_Component install roots named
    in the manifest join the scan path, and any directory containing a
    plugin file that failed checksum verification is skipped (the
    mismatched plugin is never loaded; the workflow registration is
    independently invalid through :mod:`workflow_engine.discovery`).
    """
    dirs: List[str] = [plugin_dir] if plugin_dir else []
    if manifest is not None:
        base = artifact_path if artifact_path is not None else (
            os.path.dirname(os.path.dirname(plugin_dir)) if plugin_dir else ""
        )
        for candidate in plugin_scan_dirs(manifest, base, plugins_root):
            if candidate not in dirs:
                dirs.append(candidate)
        verification = verify_manifest_plugins(manifest, base, plugins_root)
        skipped = set()
        for key, reason in verification.failures:
            path = resolve_checksum_path(key, manifest, base, plugins_root)
            skipped.add(os.path.dirname(path))
            logger.error(
                "Skipping workflow plugin %s (%s): %s", key, path, reason
            )
        if skipped:
            dirs = [d for d in dirs if d not in skipped]
    return [d for d in dirs if d and os.path.isdir(d)]


@contextmanager
def workflow_plugin_path(
    plugin_dir: str,
    manifest: Optional[dict] = None,
    artifact_path: Optional[str] = None,
    plugins_root: str = DEVICE_PLUGINS_ROOT,
) -> Iterator[bool]:
    """Scope the component's plugin directories to one pipeline run.

    ``plugin_dir`` is the inline ``{artifact_path}/plugins/<arch>``
    directory. When the workflow ``manifest`` is provided, the scan path
    additionally covers the install roots of the Plugin_Components it
    names (Requirement 11.4), and every plugin file referenced by
    ``pluginChecksums`` is verified first — a mismatch skips that
    plugin's directory so unverified bytes are never loaded
    (Requirement 10.6).

    Yields True when at least one directory exists and was applied (env
    prepend + registry scan); missing/empty directories are a no-op —
    most workflows have no extra plugins beyond the LocalServer-bundled
    set. On exit the prior ``GST_PLUGIN_PATH`` value is always restored.
    """
    dirs = _verified_scan_dirs(plugin_dir, manifest, artifact_path, plugins_root)
    if not dirs:
        yield False
        return

    prepend = ":".join(dirs)
    sentinel = object()
    previous = os.environ.get(GST_PLUGIN_PATH_ENV, sentinel)
    if previous is sentinel:
        os.environ[GST_PLUGIN_PATH_ENV] = prepend
    else:
        os.environ[GST_PLUGIN_PATH_ENV] = "{0}:{1}".format(prepend, previous)
    for directory in dirs:
        _scan_registry(directory)
    try:
        yield True
    finally:
        if previous is sentinel:
            os.environ.pop(GST_PLUGIN_PATH_ENV, None)
        else:
            os.environ[GST_PLUGIN_PATH_ENV] = previous


# ---------------------------------------------------------------------------
# Pipeline factory preflight (custom-node element name hardening)
# ---------------------------------------------------------------------------


def missing_factories(factories: List[str]) -> List[str]:
    """The names in ``factories`` with no registered GStreamer element
    factory — i.e. names ``Gst.parse_launch`` would reject with
    ``no element "<name>"``.

    Called after the run's :func:`workflow_plugin_path` scan so custom
    plugin elements are already in the registry. Contained like
    :func:`_scan_registry`: any error (including GStreamer being
    unavailable, e.g. in tests) disables the guard by reporting nothing
    missing — the pipeline parse remains the authority (Requirement 13.7).
    """
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        return [
            name for name in factories
            if Gst.ElementFactory.find(name) is None
        ]
    except Exception:  # noqa: BLE001 - preflight must never fail a run itself
        logger.debug(
            "Pipeline factory preflight unavailable; skipping", exc_info=True
        )
        return []


def provided_elements(
    plugin_dir: str,
    manifest: Optional[dict] = None,
    artifact_path: Optional[str] = None,
    plugins_root: str = DEVICE_PLUGINS_ROOT,
) -> List[str]:
    """The element factory names registered by plugins living in this
    run's verified workflow plugin directories (the same directories
    :func:`workflow_plugin_path` scans).

    Diagnostic companion to :func:`missing_factories`: when a declared
    custom-node factory is missing, these are the names the delivered
    plugin actually registers. Contained: any error yields ``[]``.
    """
    dirs = _verified_scan_dirs(plugin_dir, manifest, artifact_path, plugins_root)
    if not dirs:
        return []
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        roots = tuple(os.path.abspath(d) for d in dirs)
        registry = Gst.Registry.get()
        names = set()
        for plugin in registry.get_plugin_list():
            filename = plugin.get_filename() or ""
            if os.path.dirname(os.path.abspath(filename)) not in roots:
                continue
            for feature in registry.get_feature_list_by_plugin(
                plugin.get_name()
            ):
                names.add(feature.get_name())
        return sorted(names)
    except Exception:  # noqa: BLE001 - diagnostics must never fail a run
        logger.debug(
            "Could not list workflow plugin elements for %s", dirs,
            exc_info=True,
        )
        return []
