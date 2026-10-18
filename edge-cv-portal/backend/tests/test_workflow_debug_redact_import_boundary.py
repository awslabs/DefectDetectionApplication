"""The ingest's redaction module needs the ``workflow_core`` layer
(workflow-debug-mode design §2, §15; tasks.md 3.4).

``workflow_debug.redact`` imports ``workflow_core.stream_url``; on the Portal
that module exists only in ``backend/layers/workflow_core``, which the debug
stack attaches to the ingest function. ``conftest.py`` puts that layer on
``sys.path`` for every test, so an omission would be masked here and surface
only in the deployed function. This test runs a subprocess whose path holds
the functions directory and the shared layer but **not** the workflow_core
layer and asserts the import fails with ``ImportError``, then runs it again
with the layer path and asserts it succeeds.
"""
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
FUNCTIONS = os.path.join(_BACKEND, "functions")
SHARED_LAYER = os.path.join(_BACKEND, "layers", "shared", "python")
WORKFLOW_CORE_LAYER = os.path.join(_BACKEND, "layers", "workflow_core", "python")

PROBE = (
    "try:\n"
    "    import workflow_debug.redact as m\n"
    "except ImportError as e:\n"
    "    print('IMPORT_ERROR', type(e).__name__)\n"
    "else:\n"
    "    print('IMPORTED', m._base_redact.__module__)\n"
)


def _run(paths):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-c", PROBE], env=env, cwd=_HERE,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_import_fails_without_the_workflow_core_layer():
    assert _run([FUNCTIONS, SHARED_LAYER]) == "IMPORT_ERROR ModuleNotFoundError"


def test_import_succeeds_with_the_workflow_core_layer():
    assert _run([FUNCTIONS, SHARED_LAYER, WORKFLOW_CORE_LAYER]) == "IMPORTED workflow_core.stream_url"
