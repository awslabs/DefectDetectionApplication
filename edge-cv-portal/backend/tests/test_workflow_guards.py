"""
API guard unit tests (Workflow Manager).

Task 6.5 (spec: workflow-manager). Focused guard-level coverage:
  - workflow_guards.check_workflow_version_validated: packaging /
    publishing / deployment rejection on validation errors and on
    missing passed-validation records (Requirements 4.7, 4.10)
  - workflow_packaging.validation_guard: 400 on a failed validation
    record, 409 on a missing one (Requirements 4.7, 4.10)
  - workflows.deployment_references_workflow /
    find_active_workflow_deployments: delete-with-active-deployments
    identifying exactly the referencing active deployments
    (Requirement 5.6)

Runs against the shared moto stack from conftest.py; the guard modules
are imported inside the mock so their module-level boto3 clients are
intercepted.

Validates: Requirements 4.7, 4.10, 5.6
"""
import json
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import TEST_ENV


@pytest.fixture(scope="module")
def mods(aws_stack):
    """Import the modules under test while the moto mock is active.

    Follows the conftest pattern: pop any previously imported copies so
    the fresh imports bind moto-intercepted module-level boto3 clients.
    (aws_stack already imported the real shared_utils + workflows.)
    """
    for module_name in ("workflow_guards", "workflow_packaging"):
        sys.modules.pop(module_name, None)
    import workflow_guards
    import workflow_packaging
    return SimpleNamespace(
        guards=workflow_guards,
        packaging=workflow_packaging,
        workflows=aws_stack.workflows,
    )


def put_version(aws_stack, validation_status=None, version=1):
    """Insert a WorkflowVersions item; returns its unique workflow_id."""
    workflow_id = f"wf-{uuid.uuid4()}"
    item = {
        "workflow_id": workflow_id,
        "version": version,
        "s3_definition_key": f"workflows/uc/{workflow_id}/versions/{version}/workflow.json",
    }
    if validation_status is not None:
        item["validation_status"] = validation_status
    aws_stack.tables.versions.put_item(Item=item)
    return workflow_id


def error_finding(message="cycle detected"):
    return {"severity": "error", "code": "CYCLE", "message": message, "nodeId": "n1"}


def warning_finding(message="deprecated parameter"):
    return {"severity": "warning", "code": "DEPRECATED", "message": message, "nodeId": "n2"}


# ---------------------------------------------------------------- 4.7, 4.10
class TestCheckWorkflowVersionValidated:
    """workflow_guards.check_workflow_version_validated"""

    def test_passed_with_no_findings_returns_none(self, aws_stack, mods):
        """A version with a recorded passed run and no findings passes the
        guard (Req 4.10)."""
        workflow_id = put_version(
            aws_stack, {"status": "passed", "validated_at": 1000}
        )
        assert mods.guards.check_workflow_version_validated(workflow_id, 1) is None

    def test_passed_with_only_warnings_returns_none(self, aws_stack, mods):
        """Warning-severity findings do not block packaging/publishing/
        deployment; only errors do (Req 4.10 zero *errors*)."""
        workflow_id = put_version(
            aws_stack,
            {"status": "passed", "findings": [warning_finding()]},
        )
        assert mods.guards.check_workflow_version_validated(workflow_id, 1) is None

    def test_inline_error_findings_rejected_with_errors(self, aws_stack, mods):
        """A version whose recorded findings contain errors is rejected
        with 409 and the validation errors for display (Req 4.7)."""
        errors = [error_finding("cycle a->b->a"), error_finding("missing param")]
        workflow_id = put_version(
            aws_stack,
            {
                "status": "failed",
                "validated_at": 2000,
                "findings": errors + [warning_finding()],
            },
        )
        failure = mods.guards.check_workflow_version_validated(workflow_id, 1)
        assert failure is not None
        assert failure["status_code"] == 409
        assert failure["code"] == "WORKFLOW_VALIDATION_ERRORS"
        # Only the error-severity findings are surfaced as errors.
        assert failure["details"]["errors"] == errors
        assert failure["details"]["workflow_id"] == workflow_id
        assert failure["details"]["version"] == 1
        assert failure["details"]["validated_at"] == 2000

    def test_error_findings_loaded_from_s3_rejected(self, aws_stack, mods):
        """Findings referenced via findings_key are loaded from portal S3
        and error findings reject the request (Req 4.7)."""
        errors = [error_finding("unreachable node")]
        findings_key = f"workflows/findings/{uuid.uuid4()}.json"
        aws_stack.s3.put_object(
            Bucket=TEST_ENV["PORTAL_ARTIFACTS_BUCKET"],
            Key=findings_key,
            Body=json.dumps({"findings": errors + [warning_finding()]}),
        )
        workflow_id = put_version(
            aws_stack,
            {"status": "failed", "findings_key": findings_key},
        )
        failure = mods.guards.check_workflow_version_validated(workflow_id, 1)
        assert failure is not None
        assert failure["status_code"] == 409
        assert failure["code"] == "WORKFLOW_VALIDATION_ERRORS"
        assert failure["details"]["errors"] == errors

    def test_status_none_rejected_as_not_validated(self, aws_stack, mods):
        """A version whose recorded status is 'none' has no passed run and
        is rejected with 409 (Req 4.10)."""
        workflow_id = put_version(aws_stack, {"status": "none"})
        failure = mods.guards.check_workflow_version_validated(workflow_id, 1)
        assert failure is not None
        assert failure["status_code"] == 409
        assert failure["code"] == "WORKFLOW_VERSION_NOT_VALIDATED"
        assert failure["details"]["validation_status"] == "none"

    def test_missing_validation_record_rejected_as_not_validated(self, aws_stack, mods):
        """A version never validated (no validation_status attribute) is
        rejected with 409 (Req 4.10)."""
        workflow_id = put_version(aws_stack, validation_status=None)
        failure = mods.guards.check_workflow_version_validated(workflow_id, 1)
        assert failure is not None
        assert failure["status_code"] == 409
        assert failure["code"] == "WORKFLOW_VERSION_NOT_VALIDATED"

    def test_missing_version_rejected_404(self, mods):
        failure = mods.guards.check_workflow_version_validated("no-such-wf", 3)
        assert failure is not None
        assert failure["status_code"] == 404
        assert failure["code"] == "WORKFLOW_VERSION_NOT_FOUND"
        assert failure["details"] == {"workflow_id": "no-such-wf", "version": 3}


# ---------------------------------------------------------------- 4.7, 4.10
class TestPackagingValidationGuard:
    """workflow_packaging.validation_guard (packaging-route guard)"""

    @staticmethod
    def unpack(response):
        return response["statusCode"], json.loads(response["body"])

    def test_passed_record_allows_packaging(self, mods):
        version_item = {"version": 1, "validation_status": {"status": "passed"}}
        assert mods.packaging.validation_guard(version_item) is None

    def test_failed_record_rejected_400_with_findings_reference(self, mods):
        """A validated-and-failed version gets 400 VALIDATION_FAILED with
        the findings reference (Req 4.7)."""
        version_item = {
            "version": 2,
            "validation_status": {
                "status": "failed",
                "findings_key": "workflows/findings/abc.json",
                "validated_at": 3000,
            },
        }
        status, payload = self.unpack(mods.packaging.validation_guard(version_item))
        assert status == 400
        assert payload["error"]["code"] == "VALIDATION_FAILED"
        assert payload["error"]["details"]["version"] == 2
        assert payload["error"]["details"]["findings_key"] == "workflows/findings/abc.json"

    def test_missing_record_rejected_409_validation_required(self, mods):
        """A version with no validation record gets 409 VALIDATION_REQUIRED
        (Req 4.10)."""
        status, payload = self.unpack(mods.packaging.validation_guard({"version": 1}))
        assert status == 409
        assert payload["error"]["code"] == "VALIDATION_REQUIRED"
        assert payload["error"]["details"]["version"] == 1

    def test_stale_or_unknown_status_rejected_409(self, mods):
        version_item = {"version": 1, "validation_status": {"status": "running"}}
        status, payload = self.unpack(mods.packaging.validation_guard(version_item))
        assert status == 409
        assert payload["error"]["code"] == "VALIDATION_REQUIRED"


# ---------------------------------------------------------------------- 5.6
class TestDeleteWithActiveDeploymentsGuard:
    """workflows.deployment_references_workflow /
    find_active_workflow_deployments (delete-rejection guard)"""

    def test_reference_via_association_attributes(self, mods):
        deployment = {"component_type": "workflow", "workflow_id": "wf-1"}
        assert mods.workflows.deployment_references_workflow(deployment, "wf-1") is True
        assert mods.workflows.deployment_references_workflow(deployment, "wf-2") is False

    def test_reference_via_component_name(self, mods):
        deployment = {"components": [{"component_name": "dda.workflow.wf-1"}]}
        assert mods.workflows.deployment_references_workflow(deployment, "wf-1") is True
        assert mods.workflows.deployment_references_workflow(deployment, "wf-2") is False

    def test_unrelated_deployment_does_not_reference(self, mods):
        deployment = {
            "component_type": "model",
            "workflow_id": "wf-1",
            "components": [{"component_name": "dda.model.something"}, "not-a-dict"],
        }
        assert mods.workflows.deployment_references_workflow(deployment, "wf-1") is False

    def test_find_returns_exactly_the_active_referencing_deployments(self, env, mods):
        """Only active deployments that reference the workflow are
        identified; inactive and unrelated ones are excluded (Req 5.6)."""
        usecase_id = env.create_usecase()
        workflow_id = f"wf-{uuid.uuid4()}"

        dep_assoc = env.put_deployment(
            usecase_id, status="IN_PROGRESS",
            component_type="workflow", workflow_id=workflow_id,
        )
        dep_component = env.put_deployment(
            usecase_id, status="ACTIVE",
            components=[{"component_name": f"dda.workflow.{workflow_id}"}],
        )
        # Inactive deployment of this workflow: excluded.
        env.put_deployment(
            usecase_id, status="FAILED",
            component_type="workflow", workflow_id=workflow_id,
        )
        # Active deployment of a different workflow: excluded.
        env.put_deployment(
            usecase_id, status="ACTIVE",
            component_type="workflow", workflow_id="other-workflow",
        )

        found = mods.workflows.find_active_workflow_deployments(usecase_id, workflow_id)
        assert found == sorted([dep_assoc, dep_component])

    def test_find_status_matching_is_case_insensitive(self, env, mods):
        usecase_id = env.create_usecase()
        workflow_id = f"wf-{uuid.uuid4()}"
        dep = env.put_deployment(
            usecase_id, status="in_progress",
            component_type="workflow", workflow_id=workflow_id,
        )
        found = mods.workflows.find_active_workflow_deployments(usecase_id, workflow_id)
        assert found == [dep]

    def test_find_returns_empty_when_no_active_references(self, env, mods):
        usecase_id = env.create_usecase()
        workflow_id = f"wf-{uuid.uuid4()}"
        env.put_deployment(
            usecase_id, status="CANCELLED",
            component_type="workflow", workflow_id=workflow_id,
        )
        assert mods.workflows.find_active_workflow_deployments(usecase_id, workflow_id) == []
