"""The ingest's redaction module depends on the ``workflow_core`` layer
(workflow-telemetry-dashboards design §2.4; tasks.md 3.1).

``backend/tests/conftest.py`` puts ``backend/layers/workflow_core/python`` on
``sys.path``, so every in-process test imports ``workflow_core.stream_url``
whether or not the deployed function carries the layer. This test runs a
subprocess whose ``sys.path`` holds the functions directory and the shared
layer but NOT the ``workflow_core`` layer, and asserts that importing
``telemetry.redaction`` fails with ``ImportError``; with the layer path it
succeeds and exposes ``redact``.
"""
import os
import subprocess
import sys

import pytest

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FUNCTIONS = os.path.join(BACKEND, "functions")
SHARED_LAYER = os.path.join(BACKEND, "layers", "shared", "python")
WORKFLOW_CORE_LAYER = os.path.join(BACKEND, "layers", "workflow_core", "python")

PROBE = r"""
import sys
try:
    from telemetry import redaction
except ImportError as error:
    print("IMPORT_ERROR " + error.__class__.__name__)
    sys.exit(3)
print("OK " + redaction.redact("rtsp://user:hunter2@camera.example.com/stream", ()))
"""


def run_probe(*paths):
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH",)}
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=60)


def test_import_fails_without_the_workflow_core_layer():
    result = run_probe(FUNCTIONS, SHARED_LAYER)
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout.strip().startswith("IMPORT_ERROR")
    assert "ModuleNotFoundError" in result.stdout or "ImportError" in result.stdout


def test_import_succeeds_with_the_workflow_core_layer():
    result = run_probe(FUNCTIONS, SHARED_LAYER, WORKFLOW_CORE_LAYER)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "OK rtsp://***@camera.example.com/stream"


def test_the_module_is_one_import_and_nothing_else():
    """The module stays the one-line import the design names (plus its docstring
    and ``__all__``): no logic may hide behind the layer dependency."""
    import ast
    with open(os.path.join(FUNCTIONS, "telemetry", "redaction.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    statements = [node for node in tree.body if not isinstance(node, ast.Expr)]  # drop the docstring
    assert len(statements) == 2
    import_node, all_node = statements
    assert isinstance(import_node, ast.ImportFrom)
    assert import_node.module == "workflow_core.stream_url"
    assert [alias.name for alias in import_node.names] == ["redact"]
    assert isinstance(all_node, ast.Assign) and all_node.targets[0].id == "__all__"


@pytest.mark.parametrize("secret", ["hunter2", "s3cr3tvalue"])
def test_redact_masks_user_information_and_literals(secret):
    from telemetry import redaction
    text = "pull failed for rtsp://admin:{0}@cam.example.com/live token={0}".format(secret)
    masked = redaction.redact(text, (secret,))
    assert secret not in masked
    assert "rtsp://***@cam.example.com/live" in masked
