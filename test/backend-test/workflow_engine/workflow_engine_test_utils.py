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
"""Shared helpers for the workflow engine tests.

Builds Workflow_Component artifact sets in temporary directories and
in-memory sqlite sessions, so the tests run without GStreamer, a real
/aws_dda tree, or the production databases.
"""
import atexit
import json
import os
import tempfile

# The DAO layer resolves its sqlite paths from COMPONENT_WORK_PATH at
# import time; point it at /tmp before anything imports it (the engine it
# creates is never used by these tests — sessions come from make_session_factory).
os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dao.sqlite_db.sqlite_db_operations import Base
import workflow_engine.models  # noqa: F401 - registers the tables on Base

DEVICE_ARCH = "x86_64"
RUNNING_VERSION = "1.2.0"

VALID_MANIFEST = {
    "componentName": "dda.workflow.wf-1",
    "componentVersion": "3.0.0",
    "workflowId": "wf-1",
    "workflowVersion": 3,
    "targetArch": DEVICE_ARCH,
    "minLocalServerVersion": "1.0.0",
    "pluginDependencies": [],
    "pythonDependencies": [],
    "customPythonNodeIds": [],
}

VALID_COMPILED = {
    "schemaVersion": 1,
    "workflowId": "wf-1",
    "workflowVersion": "3",
    "targetArch": DEVICE_ARCH,
    "segments": [
        {
            "name": "s0",
            "elements": [{"nodeId": "n1", "factory": "videotestsrc", "args": {}}],
        }
    ],
    "executorBindings": [],
    "pluginDependencies": [],
}


def write_artifact_set(
    root,
    workflow_id="wf-1",
    version="3",
    manifest=None,
    compiled=None,
    omit=(),
    raw_manifest=None,
):
    """Create /{workflowId}/{version}/ with manifest/workflow/compiled files.

    ``omit`` skips files; ``raw_manifest`` writes a raw (possibly broken)
    string instead of JSON-dumping ``manifest``.
    """
    version_dir = os.path.join(str(root), workflow_id, version)
    os.makedirs(version_dir, exist_ok=True)

    if "manifest.json" not in omit:
        path = os.path.join(version_dir, "manifest.json")
        if raw_manifest is not None:
            with open(path, "w") as f:
                f.write(raw_manifest)
        else:
            document = dict(VALID_MANIFEST if manifest is None else manifest)
            document.setdefault("workflowId", workflow_id)
            with open(path, "w") as f:
                json.dump(document, f)

    if "workflow.json" not in omit:
        with open(os.path.join(version_dir, "workflow.json"), "w") as f:
            json.dump({"schemaVersion": 1, "nodes": [], "connections": []}, f)

    if "compiled_pipeline.json" not in omit:
        with open(os.path.join(version_dir, "compiled_pipeline.json"), "w") as f:
            json.dump(VALID_COMPILED if compiled is None else compiled, f)

    return version_dir


def make_session_factory():
    """A sessionmaker over a private, temp-file-backed sqlite database.

    A file-backed database (rather than ``sqlite://`` in-memory with a
    StaticPool) gives every thread its own DBAPI connection, so concurrent
    commits from the watcher/executor/API threads serialize through
    sqlite's file locking instead of racing on one shared connection
    ("cannot commit - no transaction is active").
    """
    fd, path = tempfile.mkstemp(prefix="workflow_engine_test_", suffix=".db")
    os.close(fd)
    atexit.register(lambda: os.path.exists(path) and os.remove(path))
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def make_watcher(root, session_factory, **kwargs):
    from workflow_engine.watcher import WorkflowWatcher

    kwargs.setdefault("device_arch", DEVICE_ARCH)
    kwargs.setdefault("running_version", RUNNING_VERSION)
    return WorkflowWatcher(
        session_factory=session_factory, root=str(root), **kwargs
    )
