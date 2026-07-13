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

"""Per-run GStreamer plugin path scoping for workflow runs (Requirement 9.2).

A Workflow_Component may deliver extra GStreamer plugins under
``{artifact_path}/plugins/<arch>/``. Those must be loadable for the
workflow's pipeline run without any process-wide, permanent environment
mutation that could leak into Pipeline_Configuration execution
(Requirements 13.1, 13.4).

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

import logging
import os
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

GST_PLUGIN_PATH_ENV = "GST_PLUGIN_PATH"


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


@contextmanager
def workflow_plugin_path(plugin_dir: str) -> Iterator[bool]:
    """Scope the component's plugin directory to one pipeline run.

    Yields True when the directory exists and was applied (env prepend +
    registry scan); a missing/empty directory is a no-op — most workflows
    have no extra plugins beyond the LocalServer-bundled set. On exit the
    prior ``GST_PLUGIN_PATH`` value is always restored.
    """
    if not plugin_dir or not os.path.isdir(plugin_dir):
        yield False
        return

    sentinel = object()
    previous = os.environ.get(GST_PLUGIN_PATH_ENV, sentinel)
    if previous is sentinel:
        os.environ[GST_PLUGIN_PATH_ENV] = plugin_dir
    else:
        os.environ[GST_PLUGIN_PATH_ENV] = "{0}:{1}".format(plugin_dir, previous)
    _scan_registry(plugin_dir)
    try:
        yield True
    finally:
        if previous is sentinel:
            os.environ.pop(GST_PLUGIN_PATH_ENV, None)
        else:
            os.environ[GST_PLUGIN_PATH_ENV] = previous
