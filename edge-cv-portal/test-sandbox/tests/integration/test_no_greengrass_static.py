"""Static no-Greengrass assertions for the test path (Requirement 12.9).

The Workflow_Test_Runner must execute test runs without creating any
Greengrass deployment or delivering artifacts to any edge device. These
source-level checks complement the behavioral check in
``test_sandbox_e2e.py`` and need no Docker/moto — they always run:

- The harness sources never reference Greengrass (no import, no client,
  no API call).
- Every boto3 client the harness constructs is an S3 client.
- The test-runner CDK stack (``infrastructure/lib/test-runner-stack.ts``)
  grants the sandbox task no Greengrass permissions.
"""

import os
import re

import pytest

pytestmark = pytest.mark.integration

TEST_SANDBOX_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HARNESS_DIR = os.path.join(TEST_SANDBOX_DIR, "harness")
TEST_RUNNER_STACK_TS = os.path.join(
    os.path.dirname(TEST_SANDBOX_DIR),
    "infrastructure", "lib", "test-runner-stack.ts")


def harness_sources():
    sources = {}
    for name in sorted(os.listdir(HARNESS_DIR)):
        if name.endswith(".py"):
            with open(os.path.join(HARNESS_DIR, name)) as handle:
                sources[name] = handle.read()
    assert sources, "no harness sources found at " + HARNESS_DIR
    return sources


def test_harness_sources_contain_no_greengrass_references():
    """No harness module imports, names, or calls anything Greengrass
    (Requirement 12.9)."""
    for name, source in harness_sources().items():
        assert "greengrass" not in source.lower(), \
            "harness/{0} references Greengrass".format(name)


def test_harness_boto3_clients_are_s3_only():
    """Every ``boto3.client(...)`` in the harness constructs an S3
    client; no other AWS service client exists in the test path
    (Requirement 12.9)."""
    pattern = re.compile(r"boto3\.client\(\s*([\"'])(?P<service>[^\"']+)\1")
    found = []
    for name, source in harness_sources().items():
        for match in pattern.finditer(source):
            found.append((name, match.group("service")))
    assert found, "expected at least one boto3.client call in the harness"
    non_s3 = [entry for entry in found if entry[1] != "s3"]
    assert not non_s3, \
        "harness constructs non-S3 boto3 clients: {0}".format(non_s3)
    # And no dynamically named clients slip through the literal check.
    dynamic = re.compile(r"boto3\.client\(\s*[^\"')]")
    for name, source in harness_sources().items():
        assert not dynamic.search(source), \
            "harness/{0} builds a boto3 client with a non-literal " \
            "service name".format(name)


def test_test_runner_stack_grants_no_greengrass_permissions():
    """The test-runner infrastructure (Fargate task roles, state machine,
    step Lambda) contains no Greengrass grant, action, or client — the
    sandbox cannot interact with Greengrass even by misconfiguration
    (Requirement 12.9)."""
    if not os.path.exists(TEST_RUNNER_STACK_TS):
        pytest.skip("test-runner-stack.ts not present at "
                    + TEST_RUNNER_STACK_TS)
    with open(TEST_RUNNER_STACK_TS) as handle:
        stack_source = handle.read()
    lowered = stack_source.lower()

    # No Greengrass IAM action can be granted ("greengrass:*",
    # "greengrass:CreateDeployment", ...).
    assert "greengrass:" not in lowered, \
        "test-runner-stack.ts grants Greengrass IAM actions"
    # No Greengrass CDK construct/SDK module is imported.
    assert "aws-greengrass" not in lowered, \
        "test-runner-stack.ts imports a Greengrass CDK module"

    # Any remaining "greengrass" mention must be documentation only:
    # comments or role description strings stating its absence.
    code_lines = [
        line for line in stack_source.splitlines()
        if "greengrass" in line.lower()
        and not line.strip().startswith(("//", "*", "/*"))
        and "description:" not in line.lower()
    ]
    assert not code_lines, \
        "test-runner-stack.ts references Greengrass outside " \
        "comments/descriptions: {0}".format(code_lines)
