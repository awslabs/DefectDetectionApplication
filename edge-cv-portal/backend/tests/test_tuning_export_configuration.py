"""
Use_Case Sample_Export settings, deployment configuration and device grant
(spec: .kiro/specs/quality-prompt-tuning, task 5.1).

Covers the three surfaces task 5.1 delivers:

1. ``functions/tuning_settings.py`` — the settings contract: what a value
   may be (``tuning_sample_export`` boolean, ``tuning_sample_retention_days``
   7..365 with the 30-day default), how a stored Use_Case item reads back,
   the Sample_Store bucket/prefix, the delivered ``workflowTuning``
   component configuration and the device role policy document
   (Requirements 2.1, 2.8, 2.9).
2. ``functions/usecases.py`` — the settings are accepted, validated and
   persisted on create and update; an invalid value is a 400 that writes
   nothing.
3. ``functions/deployments.py`` — WHERE export is enabled, every deployment
   of the Use_Case's devices carries the ``workflowTuning`` LocalServer
   component configuration and grants the devices' token-exchange role
   put/get under ``workflow-tuning/*`` of the Use_Case bucket; WHILE it is
   disabled the deployment reaches neither IAM nor the component
   configuration (Requirements 2.1, 2.8, 11.3).

These are deterministic, enumerated cases. The invariant over the same
space ("configuration and grants are delivered iff export is enabled, and
parsed safely" — Property 17) is task 5.3's property test; the device half
of the parse lives in
test/backend-test/workflow_engine/test_property_tuning_sample_export.py.

Harness: the shared deployment-path fakes (FakeGreengrass / FakeIot from
test_workflow_packaging_deployment_integration.py) wired in as the
Use_Case-account clients, extended with a FakeIam recording the inline
policy writes.
"""
import json
import sys
import uuid
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

from test_workflow_packaging_deployment_integration import (
    ACCOUNT_ID, REGION, FakeGreengrass, FakeIot)

LOCAL_SERVER = "aws.edgeml.dda.LocalServer.arm64JP6"
THING = "tuning-jp6-device"
EXPECTED_PREFIX = "workflow-tuning/samples/"
DEFAULT_BUCKET = f"dda-inference-results-{ACCOUNT_ID}"


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


@pytest.fixture(scope="module")
def usecases(aws_stack):
    sys.modules.pop("usecases", None)
    import usecases

    return usecases


# ==========================================================================
# 1. The settings contract (functions/tuning_settings.py)
# ==========================================================================

class TestSettingCoercion:
    """Requirement 2.1/2.9: what the Portal accepts for each setting."""

    @pytest.mark.parametrize("value,expected", [
        (True, True),
        (False, False),
        ("true", True),
        ("TRUE", True),
        ("  true  ", True),
        ("false", False),
        ("False", False),
    ])
    def test_accepted_export_values(self, tuning_settings, value, expected):
        assert tuning_settings.coerce_sample_export(value) is expected

    @pytest.mark.parametrize("value", [
        None, "", "yes", "no", "on", "off", "enabled", 1, 0, 2, 1.0,
        Decimal("1"), [], {}, ["true"],
    ])
    def test_rejected_export_values(self, tuning_settings, value):
        """Only a bool or the exact 'true'/'false' strings — the same two
        shapes the device accepts for workflowTuning.enabled, so a value
        can never mean one thing to the Portal and another on the device."""
        with pytest.raises(tuning_settings.SettingError) as excinfo:
            tuning_settings.coerce_sample_export(value)
        assert "tuning_sample_export" in str(excinfo.value)

    @pytest.mark.parametrize("value,expected", [
        (7, 7), (30, 30), (365, 365), (31, 31),
        ("7", 7), (" 45 ", 45), ("365", 365),
        (30.0, 30), (Decimal("90"), 90),
    ])
    def test_accepted_retention_values(self, tuning_settings, value, expected):
        assert tuning_settings.coerce_sample_retention_days(value) == expected

    @pytest.mark.parametrize("value", [
        6, 0, -1, 366, 1000, "6", "366", "abc", "", None, 30.5,
        Decimal("30.5"), True, False, [], {}, "30 days",
    ])
    def test_rejected_retention_values(self, tuning_settings, value):
        with pytest.raises(tuning_settings.SettingError) as excinfo:
            tuning_settings.coerce_sample_retention_days(value)
        assert "tuning_sample_retention_days" in str(excinfo.value)

    def test_bounds_are_the_documented_ones(self, tuning_settings):
        assert tuning_settings.SAMPLE_RETENTION_MIN_DAYS == 7
        assert tuning_settings.SAMPLE_RETENTION_MAX_DAYS == 365
        assert tuning_settings.SAMPLE_RETENTION_DEFAULT_DAYS == 30


class TestValidateSettings:
    def test_normalizes_in_place_and_returns_none(self, tuning_settings):
        body = {"name": "keep", "tuning_sample_export": "true",
                "tuning_sample_retention_days": "45"}
        assert tuning_settings.validate_settings(body) is None
        assert body == {"name": "keep", "tuning_sample_export": True,
                        "tuning_sample_retention_days": 45}

    def test_absent_settings_are_left_absent(self, tuning_settings):
        body = {"name": "only a rename"}
        assert tuning_settings.validate_settings(body) is None
        assert body == {"name": "only a rename"}

    def test_reports_the_first_invalid_value(self, tuning_settings):
        body = {"tuning_sample_export": "maybe",
                "tuning_sample_retention_days": 3}
        message = tuning_settings.validate_settings(body)
        assert message and "tuning_sample_export" in message
        # Nothing was normalized: the caller answers 400 and writes nothing.
        assert body["tuning_sample_export"] == "maybe"

    def test_reports_an_out_of_range_retention(self, tuning_settings):
        body = {"tuning_sample_retention_days": 400}
        message = tuning_settings.validate_settings(body)
        assert message is not None
        assert "7" in message and "365" in message


class TestSettingReads:
    @pytest.mark.parametrize("item,expected", [
        ({}, False),
        ({"tuning_sample_export": None}, False),
        ({"tuning_sample_export": False}, False),
        ({"tuning_sample_export": "false"}, False),
        ({"tuning_sample_export": "yes"}, False),
        ({"tuning_sample_export": 1}, False),
        ({"tuning_sample_export": True}, True),
        ({"tuning_sample_export": "true"}, True),
    ])
    def test_sample_export_enabled(self, tuning_settings, item, expected):
        """Requirements 2.6/11.3: only a recognizable true value enables
        export; every other shape (including a stray one) is disabled."""
        assert tuning_settings.sample_export_enabled(item) is expected

    def test_sample_export_enabled_on_a_non_dict(self, tuning_settings):
        assert tuning_settings.sample_export_enabled(None) is False

    @pytest.mark.parametrize("item,expected", [
        ({}, 30),
        ({"tuning_sample_retention_days": 7}, 7),
        ({"tuning_sample_retention_days": Decimal("120")}, 120),
        ({"tuning_sample_retention_days": "45"}, 45),
        # Invalid stored values fall back to the default rather than
        # producing an unbounded lifecycle rule.
        ({"tuning_sample_retention_days": 4000}, 30),
        ({"tuning_sample_retention_days": "forever"}, 30),
        ({"tuning_sample_retention_days": None}, 30),
    ])
    def test_sample_retention_days(self, tuning_settings, item, expected):
        assert tuning_settings.sample_retention_days(item) == expected


class TestSampleStoreLocation:
    def test_defaults_to_the_inference_results_bucket(self, tuning_settings):
        assert tuning_settings.sample_store_bucket(
            {"account_id": ACCOUNT_ID}) == DEFAULT_BUCKET

    def test_configured_uploader_bucket_wins(self, tuning_settings):
        assert tuning_settings.sample_store_bucket({
            "account_id": ACCOUNT_ID,
            "inference_uploader_s3_bucket": "  my-results  ",
        }) == "my-results"

    def test_account_id_argument_is_used_when_the_item_has_none(
            self, tuning_settings):
        assert tuning_settings.sample_store_bucket(
            {}, ACCOUNT_ID) == DEFAULT_BUCKET

    def test_no_bucket_resolves_to_empty(self, tuning_settings):
        assert tuning_settings.sample_store_bucket({}) == ""
        assert tuning_settings.sample_store_bucket(
            {"inference_uploader_s3_bucket": "   "}) == ""

    def test_prefixes(self, tuning_settings):
        assert tuning_settings.TUNING_ROOT_PREFIX == "workflow-tuning/"
        assert tuning_settings.SAMPLE_STORE_PREFIX == EXPECTED_PREFIX
        assert tuning_settings.SAMPLE_STORE_PREFIX.endswith("/")
        assert tuning_settings.COMPONENT_CONFIG_KEY == "workflowTuning"


class TestDerivedArtefacts:
    def test_component_configuration_when_enabled(self, tuning_settings):
        assert tuning_settings.component_configuration({
            "account_id": ACCOUNT_ID, "tuning_sample_export": True,
        }) == {"enabled": True, "bucket": DEFAULT_BUCKET,
               "prefix": EXPECTED_PREFIX}

    @pytest.mark.parametrize("item", [
        {},
        {"account_id": ACCOUNT_ID},
        {"account_id": ACCOUNT_ID, "tuning_sample_export": False},
        {"account_id": ACCOUNT_ID, "tuning_sample_export": "yes"},
        # Enabled but no bucket resolves: nothing is delivered rather than
        # a configuration pointing at "dda-inference-results-".
        {"tuning_sample_export": True},
    ])
    def test_no_component_configuration(self, tuning_settings, item):
        assert tuning_settings.component_configuration(item) is None

    def test_device_policy_document(self, tuning_settings):
        """Requirement 2.8: put and get under the Sample_Store prefix of the
        Use_Case bucket, and no other new permission."""
        document = tuning_settings.device_policy_document(DEFAULT_BUCKET)
        assert document == {
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "DDAWorkflowTuningSampleAccess",
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject"],
                "Resource": (f"arn:aws:s3:::{DEFAULT_BUCKET}/"
                             f"workflow-tuning/*"),
            }],
        }
        [statement] = document["Statement"]
        assert set(statement["Action"]) == {"s3:PutObject", "s3:GetObject"}
        assert tuning_settings.DEVICE_ROLE_NAME == \
            "GreengrassV2TokenExchangeRole"
        assert tuning_settings.DEVICE_POLICY_NAME == \
            "DDAWorkflowTuningSampleAccess"


# ==========================================================================
# 2. The Use_Case handler (functions/usecases.py)
# ==========================================================================

class UseCaseEnv:
    """PUT/POST /usecases through the real handler against the moto-backed
    Use_Cases table."""

    def __init__(self, env, usecases):
        self.env = env
        self.usecases = usecases
        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.table = env.stack.tables.usecases
        self.table.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Tuning Settings Test",
            "account_id": ACCOUNT_ID,
            "s3_bucket": "seed-bucket",
        })
        env.assign_role(self.user, self.usecase_id, "UseCaseAdmin")

    def update(self, body):
        event = self.env.event("PUT", "/usecases/{id}", self.user,
                               workflow_id=self.usecase_id, body=body)
        response = self.usecases.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def create(self, body):
        event = self.env.event("POST", "/usecases", self.user, body=body)
        response = self.usecases.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def item(self, usecase_id=None):
        return self.table.get_item(
            Key={"usecase_id": usecase_id or self.usecase_id})["Item"]


@pytest.fixture
def uc_env(env, usecases):
    return UseCaseEnv(env, usecases)


class TestUseCaseSettingsPersistence:
    def test_update_persists_both_settings(self, uc_env):
        status, payload = uc_env.update({"tuning_sample_export": True,
                                         "tuning_sample_retention_days": 45})

        assert status == 200, payload
        item = uc_env.item()
        assert item["tuning_sample_export"] is True
        assert int(item["tuning_sample_retention_days"]) == 45

    def test_string_forms_are_normalized_before_they_are_stored(self, uc_env):
        status, payload = uc_env.update({"tuning_sample_export": "true",
                                         "tuning_sample_retention_days": "90"})

        assert status == 200, payload
        item = uc_env.item()
        # Stored as the typed values the deliverer reads, not as strings.
        assert item["tuning_sample_export"] is True
        assert int(item["tuning_sample_retention_days"]) == 90

    def test_disabling_persists_false(self, uc_env):
        uc_env.update({"tuning_sample_export": True})
        status, _ = uc_env.update({"tuning_sample_export": False})

        assert status == 200
        assert uc_env.item()["tuning_sample_export"] is False

    def test_an_unrelated_update_leaves_the_settings_untouched(self, uc_env):
        uc_env.update({"tuning_sample_export": True,
                       "tuning_sample_retention_days": 120})
        status, _ = uc_env.update({"name": "Renamed"})

        assert status == 200
        item = uc_env.item()
        assert item["name"] == "Renamed"
        assert item["tuning_sample_export"] is True
        assert int(item["tuning_sample_retention_days"]) == 120

    @pytest.mark.parametrize("body,field", [
        ({"tuning_sample_export": "maybe"}, "tuning_sample_export"),
        ({"tuning_sample_export": 1}, "tuning_sample_export"),
        ({"tuning_sample_retention_days": 3},
         "tuning_sample_retention_days"),
        ({"tuning_sample_retention_days": 400},
         "tuning_sample_retention_days"),
        ({"tuning_sample_retention_days": "soon"},
         "tuning_sample_retention_days"),
    ])
    def test_invalid_values_are_rejected_and_write_nothing(
            self, uc_env, body, field):
        before = uc_env.item()

        status, payload = uc_env.update(dict(body, name="Should not apply"))

        assert status == 400, payload
        assert field in payload["error"]
        assert uc_env.item() == before

    def test_create_accepts_the_settings(self, uc_env):
        status, payload = uc_env.create({
            "name": "Created with export on",
            "s3_bucket": "created-bucket",
            "tuning_sample_export": "true",
            "tuning_sample_retention_days": 14,
        })

        assert status == 201, payload
        item = uc_env.item(payload["usecase"]["usecase_id"])
        assert item["tuning_sample_export"] is True
        assert int(item["tuning_sample_retention_days"]) == 14

    def test_create_without_the_settings_stores_neither(self, uc_env):
        status, payload = uc_env.create({"name": "Plain", "s3_bucket": "b"})

        assert status == 201, payload
        item = uc_env.item(payload["usecase"]["usecase_id"])
        assert "tuning_sample_export" not in item
        assert "tuning_sample_retention_days" not in item

    def test_create_rejects_an_invalid_setting(self, uc_env):
        status, payload = uc_env.create({
            "name": "Bad", "s3_bucket": "b",
            "tuning_sample_retention_days": 2,
        })

        assert status == 400, payload
        assert "tuning_sample_retention_days" in payload["error"]


# ==========================================================================
# 3. Delivery in deployments (functions/deployments.py)
# ==========================================================================

class FakeIam:
    """Recording fake of the Use_Case-account IAM client: the inline role
    policies the portal writes, plus every call made."""

    def __init__(self):
        self.policies = {}          # (role, policy) -> document dict
        self.get_calls = []
        self.put_calls = []
        self.put_error = None

    def get_role_policy(self, RoleName, PolicyName):
        self.get_calls.append((RoleName, PolicyName))
        try:
            document = self.policies[(RoleName, PolicyName)]
        except KeyError:
            raise ClientError({"Error": {
                "Code": "NoSuchEntity",
                "Message": f"policy {PolicyName} not found"}},
                "GetRolePolicy")
        return {"RoleName": RoleName, "PolicyName": PolicyName,
                "PolicyDocument": document}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.put_calls.append((RoleName, PolicyName,
                               json.loads(PolicyDocument)))
        if self.put_error is not None:
            raise self.put_error
        self.policies[(RoleName, PolicyName)] = json.loads(PolicyDocument)
        return {}


class TuningDeployEnv:
    """Both deployment submit paths with the Use_Case-account fakes wired
    in, over a Use_Case whose tuning settings the test controls."""

    def __init__(self, env, deployments, monkeypatch):
        self.env = env
        self.deployments = deployments

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Tuning Export Delivery Test",
            "account_id": ACCOUNT_ID,
        })

        self.gg = FakeGreengrass()
        self.iot = FakeIot()
        self.iam = FakeIam()
        self.clients_requested = []

        def deployment_client(service_name, usecase, session_name=None,
                              region=None):
            assert usecase["usecase_id"] == self.usecase_id
            self.clients_requested.append(service_name)
            if service_name == "greengrassv2":
                return self.gg
            if service_name == "iot":
                return self.iot
            if service_name == "iam":
                return self.iam
            raise AssertionError(f"unexpected client: {service_name}")

        monkeypatch.setattr(deployments, "get_usecase_client",
                            deployment_client)

    # ------------------------------------------------------------- setup
    def set_settings(self, **attrs):
        expression = ", ".join(f"#{key} = :{key}" for key in attrs)
        self.env.stack.tables.usecases.update_item(
            Key={"usecase_id": self.usecase_id},
            UpdateExpression=f"SET {expression}",
            ExpressionAttributeNames={f"#{k}": k for k in attrs},
            ExpressionAttributeValues={f":{k}": v for k, v in attrs.items()})

    def enable_export(self, **attrs):
        self.set_settings(tuning_sample_export=True, **attrs)

    def register_device(self, thing_name=THING):
        self.gg.register_device(thing_name, local_server_version="99.0.0",
                                arch="arm64JP6")

    def thing_arn(self, thing_name=THING):
        return f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{thing_name}"

    def seed_workflow(self, version=1):
        workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
        self.env.stack.tables.workflows.put_item(Item={
            "workflow_id": workflow_id,
            "usecase_id": self.usecase_id,
            "name": "tuning-deploy",
            "latest_version": version,
            "created_at": 1,
        })
        self.env.stack.tables.versions.put_item(Item={
            "workflow_id": workflow_id,
            "version": version,
            "validation_status": {"status": "passed"},
            "component_arn": (f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:"
                              f"components:dda.workflow.{workflow_id}"
                              f":versions:{version}.0.0"),
        })
        return workflow_id

    # ------------------------------------------------------------ invoke
    def deploy(self, components=None, **body):
        components = components if components is not None else [
            {"component_name": LOCAL_SERVER, "component_version": "1.2.0"}]
        body = {"usecase_id": self.usecase_id, "components": components,
                "target_devices": [THING], **body}
        event = self.env.event("POST", "/deployments", self.user, body=body)
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def deploy_workflow(self, workflow_id, **body):
        body = {"component_type": "workflow", "usecase_id": self.usecase_id,
                "workflow_id": workflow_id, "target_devices": [THING],
                **body}
        event = self.env.event("POST", "/deployments", self.user, body=body)
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    # ----------------------------------------------------------- asserts
    def submitted_components(self, index=-1):
        return self.gg.create_deployment_calls[index]["components"]

    def merge_doc(self, component=LOCAL_SERVER, index=-1):
        entry = self.submitted_components(index)[component]
        return json.loads(entry["configurationUpdate"]["merge"])

    def tuning_config(self, component=LOCAL_SERVER, index=-1):
        return self.merge_doc(component, index).get("workflowTuning")


@pytest.fixture
def dep_env(env, deployments, monkeypatch):
    return TuningDeployEnv(env, deployments, monkeypatch)


class TestConfigurationDeliveredWhenEnabled:
    """Requirement 2.1: WHERE Sample_Export is enabled, LocalServer gets the
    Sample_Store location as component configuration in every deployment."""

    def test_local_server_entry_carries_the_workflow_tuning_config(
            self, dep_env):
        dep_env.enable_export()
        dep_env.register_device()

        status, payload = dep_env.deploy()

        assert status == 201, payload
        assert dep_env.tuning_config() == {
            "enabled": True, "bucket": DEFAULT_BUCKET,
            "prefix": EXPECTED_PREFIX}

    def test_configured_uploader_bucket_is_the_sample_store(self, dep_env):
        dep_env.enable_export(inference_uploader_s3_bucket="fleet-results")
        dep_env.register_device()

        status, payload = dep_env.deploy()

        assert status == 201, payload
        assert dep_env.tuning_config()["bucket"] == "fleet-results"

    def test_the_deployment_is_still_created_when_the_grant_fails(
            self, dep_env):
        """The grant is best-effort: a deployment must not fail because the
        Portal could not write the device policy."""
        dep_env.enable_export()
        dep_env.register_device()
        dep_env.iam.put_error = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "nope"}},
            "PutRolePolicy")

        status, payload = dep_env.deploy()

        assert status == 201, payload
        assert dep_env.tuning_config()["enabled"] is True

    def test_model_only_deployment_has_nowhere_to_attach(self, dep_env):
        """No LocalServer entry ⇒ nothing merged and no grant written; the
        configuration rides the next deployment that includes LocalServer."""
        dep_env.enable_export()
        dep_env.register_device()

        status, payload = dep_env.deploy(components=[
            {"component_name": "model-cookie-class",
             "component_version": "1.0.0"}])

        assert status == 201, payload
        components = dep_env.submitted_components()
        assert "configurationUpdate" not in components["model-cookie-class"]
        assert json.dumps(components).find("workflowTuning") == -1
        assert "iam" not in dep_env.clients_requested


class TestDeviceGrant:
    """Requirement 2.8: the devices' token-exchange role is granted put/get
    under the Sample_Store prefix, and no other new permission."""

    def test_inline_policy_is_written_once_with_the_exact_document(
            self, dep_env, tuning_settings):
        dep_env.enable_export()
        dep_env.register_device()

        status, payload = dep_env.deploy()

        assert status == 201, payload
        [(role, policy, document)] = dep_env.iam.put_calls
        assert role == "GreengrassV2TokenExchangeRole"
        assert policy == "DDAWorkflowTuningSampleAccess"
        assert document == tuning_settings.device_policy_document(
            DEFAULT_BUCKET)
        [statement] = document["Statement"]
        assert statement["Action"] == ["s3:PutObject", "s3:GetObject"]
        assert statement["Resource"] == (
            f"arn:aws:s3:::{DEFAULT_BUCKET}/workflow-tuning/*")

    def test_the_grant_is_idempotent_across_deployments(self, dep_env):
        dep_env.enable_export()
        dep_env.register_device()

        assert dep_env.deploy()[0] == 201
        assert dep_env.deploy()[0] == 201
        assert dep_env.deploy()[0] == 201

        # Read every time, written only when it differs.
        assert len(dep_env.iam.get_calls) == 3
        assert len(dep_env.iam.put_calls) == 1

    def test_a_changed_bucket_rewrites_the_policy(self, dep_env):
        dep_env.enable_export()
        dep_env.register_device()
        assert dep_env.deploy()[0] == 201

        dep_env.set_settings(inference_uploader_s3_bucket="new-results")
        assert dep_env.deploy()[0] == 201

        assert len(dep_env.iam.put_calls) == 2
        assert dep_env.iam.put_calls[-1][2]["Statement"][0]["Resource"] == \
            "arn:aws:s3:::new-results/workflow-tuning/*"


class TestNothingDeliveredWhenDisabled:
    """Requirement 11.3: WHILE export is disabled the Portal delivers no
    tuning configuration and touches no IAM."""

    @pytest.mark.parametrize("settings", [
        {},                                        # never opted in
        {"tuning_sample_export": False},
        {"tuning_sample_export": "false"},
        {"tuning_sample_export": "yes"},           # not a recognized true
        {"tuning_sample_retention_days": 45},      # retention alone
    ])
    def test_no_configuration_and_no_iam_call(self, dep_env, settings):
        if settings:
            dep_env.set_settings(**settings)
        dep_env.register_device()

        status, payload = dep_env.deploy()

        assert status == 201, payload
        components = dep_env.submitted_components()
        assert "workflowTuning" not in json.dumps(components)
        assert "configurationUpdate" not in components[LOCAL_SERVER]
        assert "iam" not in dep_env.clients_requested

    def test_enabled_without_a_resolvable_bucket_delivers_nothing(
            self, dep_env):
        """An enabled Use_Case whose account id is missing has no Sample_Store
        to point at; nothing is delivered rather than a broken location."""
        dep_env.env.stack.tables.usecases.update_item(
            Key={"usecase_id": dep_env.usecase_id},
            UpdateExpression="SET account_id = :empty, "
                             "tuning_sample_export = :on",
            ExpressionAttributeValues={":empty": "", ":on": True})
        dep_env.register_device()

        status, payload = dep_env.deploy()

        assert status == 201, payload
        assert "workflowTuning" not in json.dumps(
            dep_env.submitted_components())
        assert "iam" not in dep_env.clients_requested


class TestWorkflowDeploymentPath:
    """Requirement 2.1: "every deployment" includes the workflow deployment
    path, which builds its own components map from the target's existing
    component set."""

    def test_carried_over_local_server_gains_the_configuration(self, dep_env):
        dep_env.enable_export()
        dep_env.register_device()
        workflow_id = dep_env.seed_workflow()
        dep_env.gg.seed_deployment(dep_env.thing_arn(), {
            LOCAL_SERVER: {"componentVersion": "1.0.51"},
            "aws.greengrass.Nucleus": {"componentVersion": "2.12.0"},
        })

        status, payload = dep_env.deploy_workflow(workflow_id)

        assert status == 201, payload
        assert dep_env.tuning_config() == {
            "enabled": True, "bucket": DEFAULT_BUCKET,
            "prefix": EXPECTED_PREFIX}
        assert len(dep_env.iam.put_calls) == 1

    def test_disabling_withdraws_a_carried_over_configuration(self, dep_env):
        """A revision of a Use_Case that has since disabled export must not
        keep shipping the configuration — and must reset it on the device,
        since Greengrass keeps the last-applied value for a key a merge no
        longer mentions."""
        dep_env.register_device()
        workflow_id = dep_env.seed_workflow()
        dep_env.gg.seed_deployment(dep_env.thing_arn(), {
            LOCAL_SERVER: {
                "componentVersion": "1.0.51",
                "configurationUpdate": {"merge": json.dumps({
                    "workflowTuning": {"enabled": True,
                                       "bucket": DEFAULT_BUCKET,
                                       "prefix": EXPECTED_PREFIX},
                    "somethingElse": {"keep": True},
                })},
            },
        })

        status, payload = dep_env.deploy_workflow(workflow_id)

        assert status == 201, payload
        entry = dep_env.submitted_components()[LOCAL_SERVER]
        merge_doc = json.loads(entry["configurationUpdate"]["merge"])
        assert "workflowTuning" not in merge_doc
        assert merge_doc["somethingElse"] == {"keep": True}
        assert entry["configurationUpdate"]["reset"] == ["/workflowTuning"]
        assert "iam" not in dep_env.clients_requested


class TestApplyWorkflowTuningConfigurationUnit:
    """The merge helper on its own, for the shapes the submit paths cannot
    reach directly."""

    def enabled_usecase(self):
        return {"usecase_id": "uc", "account_id": ACCOUNT_ID,
                "tuning_sample_export": True}

    def test_merges_into_an_entry_without_a_configuration_update(
            self, deployments):
        components = {LOCAL_SERVER: {"componentVersion": "1.0.0"}}

        config = deployments.apply_workflow_tuning_configuration(
            components, self.enabled_usecase())

        assert config == {"enabled": True, "bucket": DEFAULT_BUCKET,
                          "prefix": EXPECTED_PREFIX}
        merge_doc = json.loads(
            components[LOCAL_SERVER]["configurationUpdate"]["merge"])
        assert merge_doc == {"workflowTuning": config}

    def test_is_idempotent(self, deployments):
        components = {LOCAL_SERVER: {"componentVersion": "1.0.0"}}
        usecase = self.enabled_usecase()

        deployments.apply_workflow_tuning_configuration(components, usecase)
        first = json.dumps(components, sort_keys=True)
        deployments.apply_workflow_tuning_configuration(components, usecase)

        assert json.dumps(components, sort_keys=True) == first

    def test_an_existing_merge_keeps_its_other_keys(self, deployments):
        """Greengrass deep-merges configuration maps by key: the delivered
        configuration must sit beside the recipe defaults, a caller-supplied
        merge and the subscribe accessControl policies, never replace them."""
        existing = {
            "accessControl": {"aws.greengrass.ipc.mqttproxy": {
                "dda:workflow-subscribe:wf-1": {
                    "operations": ["aws.greengrass#SubscribeToIoTCore"],
                    "resources": ["dda/trigger"]}}},
            "cameraTimeoutSec": 12,
        }
        components = {LOCAL_SERVER: {
            "componentVersion": "1.0.0",
            "configurationUpdate": {"merge": json.dumps(existing)},
        }}

        deployments.apply_workflow_tuning_configuration(
            components, self.enabled_usecase())

        merge_doc = json.loads(
            components[LOCAL_SERVER]["configurationUpdate"]["merge"])
        assert merge_doc["accessControl"] == existing["accessControl"]
        assert merge_doc["cameraTimeoutSec"] == 12
        assert merge_doc["workflowTuning"]["prefix"] == EXPECTED_PREFIX

    def test_re_enabling_drops_the_withdrawal_reset_pointer(self, deployments):
        components = {LOCAL_SERVER: {
            "componentVersion": "1.0.0",
            "configurationUpdate": {"merge": json.dumps({}),
                                    "reset": ["/workflowTuning",
                                              "/unrelated"]},
        }}

        deployments.apply_workflow_tuning_configuration(
            components, self.enabled_usecase())

        config_update = components[LOCAL_SERVER]["configurationUpdate"]
        assert config_update["reset"] == ["/unrelated"]
        assert json.loads(config_update["merge"])["workflowTuning"][
            "enabled"] is True

    def test_an_unparseable_merge_is_never_clobbered(self, deployments):
        components = {LOCAL_SERVER: {
            "componentVersion": "1.0.0",
            "configurationUpdate": {"merge": "{not json"},
        }}

        config = deployments.apply_workflow_tuning_configuration(
            components, self.enabled_usecase())

        assert config is None
        assert components[LOCAL_SERVER]["configurationUpdate"]["merge"] == \
            "{not json"

    def test_disabled_use_case_touches_nothing(self, deployments):
        components = {LOCAL_SERVER: {"componentVersion": "1.0.0"}}

        assert deployments.apply_workflow_tuning_configuration(
            components, {"usecase_id": "uc", "account_id": ACCOUNT_ID}) is None
        assert components == {LOCAL_SERVER: {"componentVersion": "1.0.0"}}

    def test_grant_reports_a_failure_without_raising(self, deployments,
                                                    monkeypatch):
        def broken_client(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                "AssumeRole")

        monkeypatch.setattr(deployments, "get_usecase_client", broken_client)

        result = deployments.grant_workflow_tuning_device_access(
            self.enabled_usecase(), DEFAULT_BUCKET)

        assert result["status"] == "failed"
        assert result["role_name"] == "GreengrassV2TokenExchangeRole"
