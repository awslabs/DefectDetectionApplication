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
"""Import helper for ``utils.camera_manager`` in the static-image-camera
test suite (tasks 5.2, 5.3).

``utils.camera_manager`` starts a ``BaseManager`` server process at import
time. On the flask-app container images (Python 3.10/3.11, ``fork`` start
method) that import just works — the forked child inherits the ``mock_gi``
stub. On newer hosts (Python 3.14 defaults to ``forkserver`` on Linux) the
server child re-imports the module WITHOUT the gi mock and the import dies
with ``EOFError``.

The multiprocessing manager machinery is irrelevant to the static-id
short-circuits under test (they run before any ``camera_objects`` /
``manager_base`` use by design), so: try the real import first (keeping
the full machinery available for any other test module in the same
session), and only if it fails fall back to importing with the manager
startup stubbed out.
"""
import sys
from unittest.mock import patch


def import_camera_manager():
    """Return the ``utils.camera_manager`` module, importable anywhere."""
    if "utils.camera_manager" in sys.modules:
        return sys.modules["utils.camera_manager"]
    try:
        import utils.camera_manager as camera_manager
        return camera_manager
    except Exception:
        # Real import failed (forkserver host): retry with the manager
        # startup stubbed. A failed import leaves no sys.modules entry,
        # but drop any partial state defensively.
        sys.modules.pop("utils.camera_manager", None)
        with patch("multiprocessing.managers.BaseManager.start"), \
                patch("multiprocessing.Manager") as manager_factory:
            manager_factory.return_value.dict.return_value = {}
            import utils.camera_manager as camera_manager
        return camera_manager
