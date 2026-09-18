"""
Tuning storage and Sample_Store lifecycle
(spec: .kiro/specs/quality-prompt-tuning, task 5.2).

Task 5.2 delivers the infrastructure the Portal side of VLM/LLM Anomaly
Tuning runs on: the ``dda-portal-workflow-tuning`` table, the
``workflow_tuning.py`` Lambda with its grants and its
``/workflow-tuning/anomaly/**`` routes, and the S3 lifecycle rules that
expire what the feature writes (Requirement 2.9).

The CDK-side assertions (table + TTL, Lambda grants, routes with the
authorizer, the device-role statement) are task 5.3's; what this file
covers is the part that is Python:

1. ``functions/tuning_settings.py`` — the lifecycle rules themselves:
   ``workflow-tuning/samples/`` at the Use_Case retention,
   ``workflow-tuning/jobs/`` and ``workflow-tuning/sessions/`` at 30 days,
   and the merge that leaves every foreign rule on the bucket untouched.
2. ``functions/deployments.py`` — the rules are applied to the Use_Case's
   Sample_Store bucket when (and only when) the export configuration is
   delivered, idempotently, and a failure to write them never fails the
   deployment.
3. The table name the three surfaces agree on (storage-stack.ts declares
   it, compute-stack.ts hands it to the Lambda, workflow_tuning.py reads
   it) — a mismatch would deploy a Lambda pointed at a table that does not
   exist, which no single-surface test would catch.
"""
import json
import pathlib
import re
import sys
import uuid

import pytest
from botocore.exceptions import ClientError

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
INFRA_LIB = REPO_ROOT / "edge-cv-portal" / "infrastructure" / "lib"
FUNCTIONS = REPO_ROOT / "edge-cv-portal" / "backend" / "functions"

TABLE_NAME = "dda-portal-workflow-tuning"
ACCOUNT_ID = "123456789012"
BUCKET = f"dda-inference-results-{ACCOUNT_ID}"


@pytest.fixture(scope="module")
def tuning_settings():
    import tuning_settings

    return tuning_settings


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


# ==========================================================================
# 1. The lifecycle rules (functions/tuning_settings.py)
# ==========================================================================

class TestLifecycleRules:
    """Requirement 2.9: Sample_Store objects expire after the Use_Case's
    configured retention; the run artifacts this feature writes expire
    after 30 days."""

    def test_three_rules_in_a_stable_order(self, tuning_settings):
        rules = tuning_settings.lifecycle_rules()
        assert [rule["ID"] for rule in rules] == [
            "DDAWorkflowTuningSamples",
            "DDAWorkflowTuningJobs",
            "DDAWorkflowTuningSessions",
        ]
        assert [rule["ID"] for rule in rules] == list(
            tuning_settings.LIFECYCLE_RULE_IDS)

    def test_each_rule_is_filtered_on_its_own_prefix(self, tuning_settings):
        rules = {rule["ID"]: rule for rule in tuning_settings.lifecycle_rules()}
        assert rules["DDAWorkflowTuningSamples"]["Filter"] == {
            "Prefix": "workflow-tuning/samples/"}
        assert rules["DDAWorkflowTuningJobs"]["Filter"] == {
            "Prefix": "workflow-tuning/jobs/"}
        assert rules["DDAWorkflowTuningSessions"]["Filter"] == {
            "Prefix": "workflow-tuning/sessions/"}
        # Nothing outside workflow-tuning/ is touched: no rule has an empty
        # or bucket-wide prefix.
        for rule in rules.values():
            assert rule["Filter"]["Prefix"].startswith("workflow-tuning/")

    def test_default_retention_is_thirty_days(self, tuning_settings):
        rules = {rule["ID"]: rule for rule in tuning_settings.lifecycle_rules()}
        assert rules["DDAWorkflowTuningSamples"]["Expiration"] == {"Days": 30}

    @pytest.mark.parametrize("days", [7, 14, 30, 90, 365])
    def test_sample_retention_follows_the_use_case_setting(
            self, tuning_settings, days):
        rules = {rule["ID"]: rule
                 for rule in tuning_settings.lifecycle_rules(days)}
        assert rules["DDAWorkflowTuningSamples"]["Expiration"] == {"Days": days}
        assert (rules["DDAWorkflowTuningSamples"]["NoncurrentVersionExpiration"]
                == {"NoncurrentDays": days})
        # The run artifacts keep their fixed 30 days regardless.
        assert rules["DDAWorkflowTuningJobs"]["Expiration"] == {"Days": 30}
        assert rules["DDAWorkflowTuningSessions"]["Expiration"] == {"Days": 30}
        assert tuning_settings.RUN_ARTIFACT_RETENTION_DAYS == 30

    def test_string_retention_is_coerced(self, tuning_settings):
        assert tuning_settings.lifecycle_rules("45") == \
            tuning_settings.lifecycle_rules(45)

    @pytest.mark.parametrize("days", [0, 6, 366, -1, "many", 12.5, True])
    def test_an_out_of_range_retention_is_refused(self, tuning_settings, days):
        with pytest.raises(tuning_settings.SettingError):
            tuning_settings.lifecycle_rules(days)

    def test_no_retention_argument_means_the_default(self, tuning_settings):
        assert tuning_settings.lifecycle_rules(None) == \
            tuning_settings.lifecycle_rules(
                tuning_settings.SAMPLE_RETENTION_DEFAULT_DAYS)

    def test_every_rule_is_enabled_and_expires(self, tuning_settings):
        for rule in tuning_settings.lifecycle_rules(60):
            assert rule["Status"] == "Enabled"
            assert rule["Expiration"]["Days"] >= 1
            assert "NoncurrentVersionExpiration" in rule


class TestMergeLifecycleRules:
    """The bucket is the Use_Case's inference results bucket, shared with
    the InferenceUploader: the merge owns exactly its three rule ids."""

    def test_an_empty_configuration_gains_all_three(self, tuning_settings):
        rules, changed = tuning_settings.merge_lifecycle_rules([], 30)
        assert changed is True
        assert rules == tuning_settings.lifecycle_rules(30)

    def test_none_is_treated_as_empty(self, tuning_settings):
        rules, changed = tuning_settings.merge_lifecycle_rules(None)
        assert changed is True
        assert rules == tuning_settings.lifecycle_rules()

    def test_already_in_force_is_unchanged(self, tuning_settings):
        current = tuning_settings.lifecycle_rules(45)
        rules, changed = tuning_settings.merge_lifecycle_rules(current, 45)
        assert changed is False
        assert rules == current

    def test_foreign_rules_are_preserved_verbatim_and_in_order(
            self, tuning_settings):
        foreign_a = {"ID": "ExpireCaptures", "Status": "Enabled",
                     "Filter": {"Prefix": "captures/"},
                     "Expiration": {"Days": 7}}
        foreign_b = {"ID": "AbortUploads", "Status": "Enabled",
                     "Prefix": "",
                     "AbortIncompleteMultipartUpload": {
                         "DaysAfterInitiation": 3}}
        rules, changed = tuning_settings.merge_lifecycle_rules(
            [foreign_a, foreign_b], 30)
        assert changed is True
        assert rules[0] == foreign_a
        assert rules[1] == foreign_b
        assert rules[2:] == tuning_settings.lifecycle_rules(30)

    def test_a_stale_retention_is_replaced_in_place(self, tuning_settings):
        foreign = {"ID": "ExpireCaptures", "Status": "Enabled",
                   "Filter": {"Prefix": "captures/"},
                   "Expiration": {"Days": 7}}
        current = tuning_settings.lifecycle_rules(30)
        existing = [current[0], foreign, current[1], current[2]]
        rules, changed = tuning_settings.merge_lifecycle_rules(existing, 90)
        assert changed is True
        # Positions are preserved; only our samples rule's days changed.
        assert [rule["ID"] for rule in rules] == [
            "DDAWorkflowTuningSamples", "ExpireCaptures",
            "DDAWorkflowTuningJobs", "DDAWorkflowTuningSessions"]
        assert rules[0]["Expiration"] == {"Days": 90}
        assert rules[1] == foreign

    def test_a_partially_present_configuration_gains_the_missing_rules(
            self, tuning_settings):
        current = tuning_settings.lifecycle_rules(30)
        rules, changed = tuning_settings.merge_lifecycle_rules([current[1]], 30)
        assert changed is True
        assert [rule["ID"] for rule in rules] == [
            "DDAWorkflowTuningJobs", "DDAWorkflowTuningSamples",
            "DDAWorkflowTuningSessions"]

    def test_a_duplicate_of_one_of_ours_is_dropped(self, tuning_settings):
        current = tuning_settings.lifecycle_rules(30)
        rules, changed = tuning_settings.merge_lifecycle_rules(
            current + [dict(current[0])], 30)
        assert changed is True
        assert [rule["ID"] for rule in rules] == [
            rule["ID"] for rule in current]

    def test_an_unrecognizable_entry_is_left_alone(self, tuning_settings):
        rules, _ = tuning_settings.merge_lifecycle_rules(
            ["not-a-rule", {"Status": "Enabled"}], 30)
        assert rules[0] == "not-a-rule"
        assert rules[1] == {"Status": "Enabled"}


# ==========================================================================
# 2. Applying them at delivery time (functions/deployments.py)
# ==========================================================================

class FakeS3:
    """Recording fake of the Use_Case-account S3 client: the bucket's
    lifecycle configuration plus every call made."""

    def __init__(self, rules=None, missing=True):
        self.rules = list(rules or [])
        self.missing = missing and not self.rules
        self.get_calls = []
        self.put_calls = []
        self.get_error = None
        self.put_error = None

    def get_bucket_lifecycle_configuration(self, Bucket):
        self.get_calls.append(Bucket)
        if self.get_error is not None:
            raise self.get_error
        if self.missing:
            raise ClientError({"Error": {
                "Code": "NoSuchLifecycleConfiguration",
                "Message": "The lifecycle configuration does not exist"}},
                "GetBucketLifecycleConfiguration")
        return {"Rules": [dict(rule) for rule in self.rules]}

    def put_bucket_lifecycle_configuration(self, Bucket,
                                           LifecycleConfiguration):
        self.put_calls.append((Bucket, LifecycleConfiguration))
        if self.put_error is not None:
            raise self.put_error
        self.rules = [dict(rule)
                      for rule in LifecycleConfiguration["Rules"]]
        self.missing = False
        return {}


@pytest.fixture
def s3(deployments, monkeypatch):
    """A FakeS3 wired in as the Use_Case-account S3 client, recording which
    services were requested at all."""
    fake = FakeS3()
    requested = []

    def client(service_name, usecase, session_name=None, region=None):
        requested.append(service_name)
        if service_name == "s3":
            return fake
        raise AssertionError(f"unexpected client: {service_name}")

    monkeypatch.setattr(deployments, "get_usecase_client", client)
    fake.services_requested = requested
    return fake


def usecase(**attrs):
    return {"usecase_id": f"uc-{uuid.uuid4()}", "account_id": ACCOUNT_ID,
            **attrs}


class TestApplyLifecycle:
    """Requirement 2.9: the rules land on the Use_Case's bucket."""

    def test_a_bucket_without_a_configuration_gets_ours(
            self, deployments, tuning_settings, s3):
        result = deployments.apply_workflow_tuning_lifecycle(
            usecase(tuning_sample_export=True), BUCKET)
        assert result["status"] == "applied"
        assert s3.get_calls == [BUCKET]
        assert len(s3.put_calls) == 1
        bucket, configuration = s3.put_calls[0]
        assert bucket == BUCKET
        assert configuration == {
            "Rules": tuning_settings.lifecycle_rules(30)}

    def test_the_use_case_retention_is_the_sample_expiry(
            self, deployments, s3):
        deployments.apply_workflow_tuning_lifecycle(
            usecase(tuning_sample_export=True,
                    tuning_sample_retention_days=120), BUCKET)
        rules = {rule["ID"]: rule
                 for rule in s3.put_calls[0][1]["Rules"]}
        assert rules["DDAWorkflowTuningSamples"]["Expiration"] == {"Days": 120}
        assert rules["DDAWorkflowTuningJobs"]["Expiration"] == {"Days": 30}

    def test_an_invalid_stored_retention_falls_back_to_the_default(
            self, deployments, s3):
        deployments.apply_workflow_tuning_lifecycle(
            usecase(tuning_sample_retention_days="not-a-number"), BUCKET)
        rules = {rule["ID"]: rule for rule in s3.put_calls[0][1]["Rules"]}
        assert rules["DDAWorkflowTuningSamples"]["Expiration"] == {"Days": 30}

    def test_it_is_idempotent_across_deployments(self, deployments, s3):
        first = deployments.apply_workflow_tuning_lifecycle(usecase(), BUCKET)
        second = deployments.apply_workflow_tuning_lifecycle(usecase(), BUCKET)
        assert first["status"] == "applied"
        assert second["status"] == "unchanged"
        assert len(s3.put_calls) == 1
        assert len(s3.get_calls) == 2

    def test_a_changed_retention_rewrites_the_rule(self, deployments, s3):
        deployments.apply_workflow_tuning_lifecycle(
            usecase(tuning_sample_retention_days=30), BUCKET)
        result = deployments.apply_workflow_tuning_lifecycle(
            usecase(tuning_sample_retention_days=7), BUCKET)
        assert result["status"] == "applied"
        assert len(s3.put_calls) == 2
        rules = {rule["ID"]: rule for rule in s3.put_calls[1][1]["Rules"]}
        assert rules["DDAWorkflowTuningSamples"]["Expiration"] == {"Days": 7}

    def test_the_inference_uploader_rules_survive(self, deployments, s3):
        foreign = {"ID": "ExpireInferenceResults", "Status": "Enabled",
                   "Filter": {"Prefix": ""},
                   "Expiration": {"Days": 90}}
        s3.rules = [foreign]
        s3.missing = False
        deployments.apply_workflow_tuning_lifecycle(usecase(), BUCKET)
        written = s3.put_calls[0][1]["Rules"]
        assert written[0] == foreign
        assert [rule["ID"] for rule in written[1:]] == [
            "DDAWorkflowTuningSamples", "DDAWorkflowTuningJobs",
            "DDAWorkflowTuningSessions"]

    def test_a_write_failure_is_reported_without_raising(
            self, deployments, s3):
        s3.put_error = ClientError({"Error": {
            "Code": "AccessDenied",
            "Message": "not authorized to perform s3:PutLifecycleConfiguration"
        }}, "PutBucketLifecycleConfiguration")
        result = deployments.apply_workflow_tuning_lifecycle(usecase(), BUCKET)
        assert result["status"] == "failed"
        assert "AccessDenied" in result["error"]

    def test_an_unreadable_configuration_is_reported_without_raising(
            self, deployments, s3):
        s3.get_error = ClientError({"Error": {
            "Code": "AccessDenied", "Message": "denied"}},
            "GetBucketLifecycleConfiguration")
        result = deployments.apply_workflow_tuning_lifecycle(usecase(), BUCKET)
        assert result["status"] == "failed"
        assert s3.put_calls == []

    def test_an_unavailable_client_is_reported_without_raising(
            self, deployments, monkeypatch):
        def client(service_name, usecase, session_name=None, region=None):
            raise ClientError({"Error": {"Code": "AccessDenied",
                                         "Message": "assume-role denied"}},
                              "AssumeRole")

        monkeypatch.setattr(deployments, "get_usecase_client", client)
        result = deployments.apply_workflow_tuning_lifecycle(
            usecase(), BUCKET)
        assert result["status"] == "failed"


class TestDeliveryAppliesLifecycle:
    """The rules are applied exactly when the export configuration is
    delivered — a Use_Case with export disabled reaches no S3 API at all
    (Requirement 11.3)."""

    def _components(self):
        return {"aws.edgeml.dda.LocalServer.arm64JP6": {
            "componentVersion": "1.2.0"}}

    def test_enabled_delivers_configuration_grant_and_lifecycle(
            self, deployments, tuning_settings, monkeypatch):
        calls = []
        monkeypatch.setattr(
            deployments, "grant_workflow_tuning_device_access",
            lambda *a, **k: calls.append(("grant", a[1])) or {})
        monkeypatch.setattr(
            deployments, "apply_workflow_tuning_lifecycle",
            lambda *a, **k: calls.append(("lifecycle", a[1])) or {})
        components = self._components()
        config = deployments.deliver_workflow_tuning(
            components, usecase(tuning_sample_export=True), ACCOUNT_ID)
        assert config == {"enabled": True, "bucket": BUCKET,
                          "prefix": tuning_settings.SAMPLE_STORE_PREFIX}
        assert calls == [("grant", BUCKET), ("lifecycle", BUCKET)]

    def test_disabled_reaches_neither_iam_nor_s3(self, deployments,
                                                 monkeypatch):
        def client(service_name, usecase, session_name=None, region=None):
            raise AssertionError(
                f"a disabled Use_Case must request no {service_name} client")

        monkeypatch.setattr(deployments, "get_usecase_client", client)
        components = self._components()
        assert deployments.deliver_workflow_tuning(
            components, usecase(), ACCOUNT_ID) is None
        assert "configurationUpdate" not in components[
            "aws.edgeml.dda.LocalServer.arm64JP6"]

    def test_a_lifecycle_failure_does_not_fail_the_delivery(
            self, deployments, s3):
        s3.put_error = ClientError({"Error": {"Code": "AccessDenied",
                                              "Message": "denied"}},
                                   "PutBucketLifecycleConfiguration")
        components = self._components()
        # The device grant goes through the same (s3-only) fake client, so
        # it fails too: the configuration must still be delivered.
        config = deployments.deliver_workflow_tuning(
            components, usecase(tuning_sample_export=True), ACCOUNT_ID)
        assert config is not None
        merge = json.loads(
            components["aws.edgeml.dda.LocalServer.arm64JP6"][
                "configurationUpdate"]["merge"])
        assert merge["workflowTuning"]["bucket"] == BUCKET


# ==========================================================================
# 3. The store the Lambda is pointed at
# ==========================================================================

class TestTuningTableName:
    """storage-stack.ts creates the table, compute-stack.ts hands its name
    to the Lambda and workflow_tuning.py reads it: a disagreement would
    deploy a handler pointed at a table that does not exist."""

    def test_storage_stack_declares_the_table_with_pk_sk_and_ttl(self):
        source = (INFRA_LIB / "storage-stack.ts").read_text()
        assert f"tableName: '{TABLE_NAME}'" in source
        block = source.split("WorkflowTuningTable", 1)[1].split(
            "PortalArtifactsBucket", 1)[0]
        assert "name: 'pk'" in block
        assert "name: 'sk'" in block
        assert "timeToLiveAttribute: 'ttl'" in block

    def test_compute_stack_hands_the_same_name_to_the_lambda(self):
        source = (INFRA_LIB / "compute-stack.ts").read_text()
        assert f"const WORKFLOW_TUNING_TABLE_NAME = '{TABLE_NAME}'" in source
        assert "WORKFLOW_TUNING_TABLE: WORKFLOW_TUNING_TABLE_NAME" in source
        assert "handler: 'workflow_tuning.handler'" in source

    def test_the_handler_reads_that_environment_variable(self):
        source = (FUNCTIONS / "workflow_tuning.py").read_text()
        assert "os.environ.get('WORKFLOW_TUNING_TABLE'" in source
        assert TABLE_NAME in source

    def test_every_designed_route_is_registered(self):
        """The route table of design section 3 is what the nested API stack
        registers (task 6.1 fills the handler behind it)."""
        source = (INFRA_LIB / "workflow-tuning-api-stack.ts").read_text()
        # Resource path parts the design's routes require.
        for part in ("'workflow-tuning'", "'anomaly'", "'workflows'",
                     "'sessions'", "'{id}'", "'refresh'", "'samples'",
                     "'labels'", "'synthetic-negatives'", "'candidates'",
                     "'{cid}'", "'preview'", "'score-runs'", "'{rid}'",
                     "'outcomes'", "'cancel'", "'diff'", "'{other}'",
                     "'selection'", "'apply'"):
            assert f"addResource({part}" in source, part
        # Every method sits behind the Cognito authorizer.
        assert "authorizationType: apigateway.AuthorizationType.COGNITO" \
            in source
        assert re.search(r"defaultCorsPreflightOptions:\s*corsOptions", source)
