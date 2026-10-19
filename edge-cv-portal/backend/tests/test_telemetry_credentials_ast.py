"""Static rules over the telemetry and debug functions
(workflow-telemetry-dashboards design §2.4, §14.4; workflow-debug-mode tasks.md 3.2).

Walks every module under ``backend/functions/telemetry*``,
``backend/functions/workflow_debug*`` and
``backend/functions/edge_telemetry_common/`` and fails on:

- a call to ``get_usecase_client``, ``is_cross_account_setup`` or
  ``assume_cross_account_role`` (the second topology predicate);
- an ``assume_usecase_role`` or ``get_s3_client_for_bucket`` call without a
  ``session_policy`` keyword;
- a ``boto3.client(...)`` for ``iot``, ``iot-data``, ``greengrassv2``, ``s3``
  or ``sts`` outside ``edge_telemetry_common/credentials.py`` (a non-literal
  service name outside that module counts as well);
- any ``workflow_core.<x>`` import with ``x != stream_url`` (the layer's other
  modules carry a cp311-only native dependency and must not be imported by a
  Python 3.12 function).
"""
import ast
from pathlib import Path

import pytest

FUNCTIONS = Path(__file__).resolve().parents[1] / "functions"
CREDENTIALS_MODULE = FUNCTIONS / "edge_telemetry_common" / "credentials.py"

FORBIDDEN_CALLS = {"get_usecase_client", "is_cross_account_setup", "assume_cross_account_role"}
SESSION_POLICY_CALLS = {"assume_usecase_role", "get_s3_client_for_bucket"}
GUARDED_SERVICES = {"iot", "iot-data", "greengrassv2", "s3", "sts"}


def walked_files():
    files = set()
    for pattern in ("telemetry*", "workflow_debug*"):
        for path in FUNCTIONS.glob(pattern):
            if path.is_dir():
                files.update(path.rglob("*.py"))
            elif path.suffix == ".py":
                files.add(path)
    files.update((FUNCTIONS / "edge_telemetry_common").rglob("*.py"))
    return sorted(p for p in files if "__pycache__" not in p.parts)


def _callee_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_boto3_client_call(node):
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == "client"
            and isinstance(func.value, ast.Name) and func.value.id == "boto3")


def violations(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _callee_name(node)
            if name in FORBIDDEN_CALLS:
                found.append("line {0}: call to {1}".format(node.lineno, name))
            if name in SESSION_POLICY_CALLS:
                if not any(kw.arg == "session_policy" for kw in node.keywords):
                    found.append("line {0}: {1} without session_policy".format(node.lineno, name))
            if _is_boto3_client_call(node) and path.resolve() != CREDENTIALS_MODULE.resolve():
                service = node.args[0] if node.args else None
                if isinstance(service, ast.Constant) and isinstance(service.value, str):
                    if service.value in GUARDED_SERVICES:
                        found.append("line {0}: boto3.client({1!r}) outside credentials.py".format(
                            node.lineno, service.value))
                else:
                    found.append("line {0}: boto3.client with a non-literal service outside "
                                 "credentials.py".format(node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "workflow_core":
                names = [alias.name for alias in node.names]
                if names != ["stream_url"]:
                    found.append("line {0}: from workflow_core import {1}".format(node.lineno, names))
            elif module.startswith("workflow_core.") and module != "workflow_core.stream_url":
                found.append("line {0}: from {1} import ...".format(node.lineno, module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "workflow_core" or (
                        alias.name.startswith("workflow_core.")
                        and alias.name != "workflow_core.stream_url"):
                    found.append("line {0}: import {1}".format(node.lineno, alias.name))
    return found


def test_the_walk_covers_the_shared_package():
    files = walked_files()
    assert CREDENTIALS_MODULE in files
    assert any(p.parent.name == "edge_telemetry_common" and p.name == "topics.py" for p in files)


@pytest.mark.parametrize("path", walked_files(), ids=lambda p: str(p.relative_to(FUNCTIONS)))
def test_no_credential_or_layer_rule_violation(path):
    assert violations(path) == []


class TestRuleDetection:
    """The rules themselves, on synthetic sources (so a silent rule cannot pass)."""

    def _check(self, tmp_path, source, name="workflow_debug_probe.py"):
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        return violations(path)

    def test_flags_the_second_predicate(self, tmp_path):
        found = self._check(tmp_path, "import shared_utils\nshared_utils.is_cross_account_setup(u)\n")
        assert found and "is_cross_account_setup" in found[0]

    def test_flags_get_usecase_client_by_bare_name(self, tmp_path):
        found = self._check(tmp_path, "from shared_utils import get_usecase_client\nget_usecase_client('s3', u)\n")
        assert found and "get_usecase_client" in found[0]

    def test_flags_assume_role_without_session_policy(self, tmp_path):
        found = self._check(tmp_path, "import shared_utils\nshared_utils.assume_usecase_role(a, e, 'n')\n")
        assert found == ["line 2: assume_usecase_role without session_policy"]
        assert self._check(tmp_path, "import shared_utils\nshared_utils.assume_usecase_role(a, e, 'n', session_policy=p)\n") == []

    def test_flags_guarded_boto3_clients(self, tmp_path):
        for service in sorted(GUARDED_SERVICES):
            found = self._check(tmp_path, "import boto3\nc = boto3.client({0!r}, region_name='us-east-1')\n".format(service))
            assert found and service in found[0]
        assert self._check(tmp_path, "import boto3\nc = boto3.client('dynamodb')\n") == []
        assert self._check(tmp_path, "import boto3\nc = boto3.client('logs')\n") == []
        found = self._check(tmp_path, "import boto3\nc = boto3.client(name)\n")
        assert found and "non-literal" in found[0]

    def test_flags_other_workflow_core_imports(self, tmp_path):
        assert self._check(tmp_path, "from workflow_core.stream_url import redact\n") == []
        assert self._check(tmp_path, "from workflow_core import stream_url\n") == []
        assert self._check(tmp_path, "from workflow_core.serializer import load\n")
        assert self._check(tmp_path, "from workflow_core import catalog\n")
        assert self._check(tmp_path, "import workflow_core.compiler\n")
        assert self._check(tmp_path, "import workflow_core\n")
