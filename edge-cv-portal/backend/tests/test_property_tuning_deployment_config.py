"""Property test for the tuning deployment configuration (spec task 5.3).

**Feature: quality-prompt-tuning, Property 17: Configuration and grants are
delivered iff export is enabled, and parsed safely** — **Validates:
Requirements 2.1, 2.8**

*For any* Use_Case, the deployment includes ``workflowTuning`` and the
device role statement iff ``tuning_sample_export`` is enabled, with the
Use_Case's bucket and the ``workflow-tuning/samples/`` prefix; and *for
any* LocalServer configuration shape (absent, non-object, disabled, empty
bucket, prefix without trailing slash, valid), export is disabled for
every malformed shape and enabled with the exact location otherwise.

Scope of the half asserted here
-------------------------------

This is the **deployment half**: the Portal side of the "iff". It drives
``deployments.deliver_workflow_tuning`` — the one function both deployment
submit paths call (``create_deployment`` and ``create_workflow_deployment``,
pinned by :class:`TestBothSubmitPathsDeliver` below and end-to-end through
the handler by ``test_tuning_export_configuration.py``) — over drawn
Use_Case settings and drawn component maps, and asserts against an
**independent restatement** of Requirements 2.1/2.8/2.9 transcribed in
this file, never imported from ``tuning_settings``:

* the ``workflowTuning`` component configuration is merged into the
  LocalServer entry iff export is enabled (and a Sample_Store bucket
  resolves), carrying exactly ``{enabled, bucket, prefix}`` with the
  Use_Case's bucket and ``workflow-tuning/samples/``;
* the device role's inline statement is written iff that configuration was
  delivered, granting put/get under ``workflow-tuning/*`` of that bucket
  and **no other permission** (Requirement 2.8);
* the Sample_Store lifecycle rules are applied iff it was delivered
  (Requirement 2.9), at the Use_Case's retention, preserving every foreign
  rule;
* WHILE export is disabled nothing at all is delivered — no configuration
  key, no IAM client, no S3 client — and a configuration carried over from
  a formerly enabled Use_Case is withdrawn (Requirement 11.3).

The **device half of the parse** (the second clause: which LocalServer
configuration shapes the device accepts) is task 3.4's
``test/backend-test/workflow_engine/test_property_tuning_sample_export.py``.
The two halves meet here in one assertion per example: the configuration
this deployment delivers is handed to the device's real
``ExportConfig.from_component_configuration`` and must parse to exactly
the delivered bucket and prefix (skipped when the device tree is not
importable in this environment).

The enumerated cases over the same space are task 5.1's
``test_tuning_export_configuration.py``; the CDK side of task 5.3 is
``edge-cv-portal/infrastructure/test/workflow-tuning-infra.test.ts``.

Harness: no moto traffic per example — the Use_Case item is a plain dict
and the Use_Case-account IAM/S3 clients are recording fakes, so the
property is pure Python over the delivery path.
"""
from __future__ import annotations

import copy
import inspect
import json
import os
import re
import sys
from unittest import mock

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

ACCOUNT_ID = "123456789012"
REGION = "us-east-2"
LOCAL_SERVER_PREFIX = "aws.edgeml.dda.LocalServer"

# --------------------------------------------------------------------------
# Independent restatement of the requirements under test.
#
# Transcribed from the requirements/design, NOT imported from
# tuning_settings — a mutation of the module must fail this test.
# --------------------------------------------------------------------------

#: Requirement 2.3 / design "LocalServer component configuration": the
#: Sample_Store prefix, trailing slash included (the device disables export
#: for a prefix that does not end in '/').
REF_PREFIX = "workflow-tuning/samples/"

#: Requirement 9.4: everything this feature writes lives under this prefix.
REF_ROOT = "workflow-tuning/"

REF_CONFIG_KEY = "workflowTuning"
REF_ROLE = "GreengrassV2TokenExchangeRole"
REF_POLICY = "DDAWorkflowTuningSampleAccess"

#: Requirement 2.9: default 30 days, bounded 7..365.
REF_RETENTION_DEFAULT = 30
REF_RETENTION_MIN = 7
REF_RETENTION_MAX = 365

#: The design's fixed retention for job manifests and outcome batches.
REF_RUN_ARTIFACT_RETENTION = 30


def ref_export_enabled(usecase):
    """Requirement 2.1: whether the Use_Case has Sample_Export enabled.

    A real bool, or the exact strings ``"true"``/``"false"`` (trimmed,
    case-insensitive) — the same two shapes the device accepts for the
    delivered ``enabled`` flag. Anything else (absent, null, a stray
    string, a number) is disabled: a Use_Case that never opted in must be
    byte-identical to pre-feature (Requirement 11.3).
    """
    if not isinstance(usecase, dict) or "tuning_sample_export" not in usecase:
        return False
    value = usecase["tuning_sample_export"]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "true":
            return True
    return False


def ref_bucket(usecase, account_id):
    """The Sample_Store bucket: the Use_Case's inference results bucket,
    resolved as the InferenceUploader resolves it."""
    configured = usecase.get("inference_uploader_s3_bucket")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    account = account_id or usecase.get("account_id") or ""
    account = str(account).strip()
    if not account:
        return ""
    return f"dda-inference-results-{account}"


def ref_retention_days(usecase):
    """Requirement 2.9: the configured retention when it is a whole number
    of days within [7, 365], else the 30-day default."""
    if "tuning_sample_retention_days" not in usecase:
        return REF_RETENTION_DEFAULT
    value = usecase["tuning_sample_retention_days"]
    if isinstance(value, bool):
        return REF_RETENTION_DEFAULT
    days = None
    if isinstance(value, int):
        days = value
    elif isinstance(value, float):
        days = int(value) if value == int(value) else None
    elif isinstance(value, str):
        try:
            days = int(value.strip())
        except ValueError:
            days = None
    if days is None or not REF_RETENTION_MIN <= days <= REF_RETENTION_MAX:
        return REF_RETENTION_DEFAULT
    return days


def ref_component_configuration(usecase, account_id):
    """The ``workflowTuning`` configuration the deployment must carry, or
    None when export is disabled or no bucket resolves."""
    if not ref_export_enabled(usecase):
        return None
    bucket = ref_bucket(usecase, account_id)
    if not bucket:
        return None
    return {"enabled": True, "bucket": bucket, "prefix": REF_PREFIX}


def ref_local_server_name(components):
    """The LocalServer entry a deployment's component map carries, if any —
    the only entry the configuration can attach to."""
    return next((name for name in sorted(components or {})
                 if name.startswith(LOCAL_SERVER_PREFIX)), None)


def ref_existing_merge(entry):
    """``(merge_doc, parseable)`` for a component entry's existing
    ``configurationUpdate.merge``. An unparseable merge is never
    clobbered, so nothing is delivered into it."""
    update = entry.get("configurationUpdate") or {}
    raw = update.get("merge")
    if not raw:
        return {}, True
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None, False
    return (parsed if isinstance(parsed, dict) else {}), True


def ref_delivery(components, usecase, account_id):
    """What the deployment must do, restated: ``(config, action)`` where
    ``action`` is ``"deliver"``, ``"withdraw"`` or ``"nothing"``."""
    config = ref_component_configuration(usecase, account_id)
    name = ref_local_server_name(components)
    if name is None:
        return None, "nothing"
    merge_doc, parseable = ref_existing_merge(components[name])
    if not parseable:
        return None, "nothing"
    if config is None:
        # A key carried over from a revision of a formerly enabled Use_Case
        # must be dropped AND reset on the device; otherwise nothing.
        return None, ("withdraw" if REF_CONFIG_KEY in merge_doc
                      else "nothing")
    return config, "deliver"


def ref_device_policy(bucket):
    """Requirement 2.8: put/get under the Sample_Store prefix of the
    Use_Case bucket, and no other new permission."""
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "DDAWorkflowTuningSampleAccess",
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:GetObject"],
            "Resource": f"arn:aws:s3:::{bucket}/{REF_ROOT}*",
        }],
    }


# --------------------------------------------------------------------------
# Module under test + the device-side parse (the other half's rule)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


def _device_export_config():
    """The device's real ``ExportConfig`` class, or None when the device
    tree is not importable in this environment."""
    here = os.path.dirname(os.path.abspath(__file__))
    device_backend = os.path.abspath(
        os.path.join(here, "..", "..", "..", "src", "backend"))
    if device_backend not in sys.path:
        sys.path.append(device_backend)
    try:
        from workflow_engine.tuning.sample_export import ExportConfig
    except Exception:                                   # pragma: no cover
        return None
    return ExportConfig


DEVICE_EXPORT_CONFIG = _device_export_config()


# --------------------------------------------------------------------------
# Recording Use_Case-account clients
# --------------------------------------------------------------------------

class FakeIam:
    """Records every inline role policy read and written."""

    def __init__(self, policies=None):
        self.policies = dict(policies or {})
        self.get_calls = []
        self.put_calls = []

    def get_role_policy(self, RoleName, PolicyName):
        self.get_calls.append((RoleName, PolicyName))
        try:
            document = self.policies[(RoleName, PolicyName)]
        except KeyError:
            raise ClientError({"Error": {
                "Code": "NoSuchEntity",
                "Message": f"policy {PolicyName} not found"}},
                "GetRolePolicy")
        return {"PolicyDocument": document}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        document = json.loads(PolicyDocument)
        self.put_calls.append((RoleName, PolicyName, document))
        self.policies[(RoleName, PolicyName)] = document
        return {}


class FakeS3:
    """Records the bucket lifecycle configuration reads and writes."""

    def __init__(self, rules=None):
        self.rules = list(rules) if rules is not None else None
        self.get_calls = []
        self.put_calls = []

    def get_bucket_lifecycle_configuration(self, Bucket):
        self.get_calls.append(Bucket)
        if self.rules is None:
            raise ClientError({"Error": {
                "Code": "NoSuchLifecycleConfiguration",
                "Message": "no configuration"}},
                "GetBucketLifecycleConfiguration")
        return {"Rules": copy.deepcopy(self.rules)}

    def put_bucket_lifecycle_configuration(self, Bucket,
                                           LifecycleConfiguration):
        self.put_calls.append((Bucket,
                               copy.deepcopy(LifecycleConfiguration)))
        self.rules = copy.deepcopy(LifecycleConfiguration["Rules"])
        return {}


class Delivery:
    """One ``deliver_workflow_tuning`` call with the Use_Case-account
    clients recorded."""

    def __init__(self, deployments, components, usecase, account_id,
                 iam=None, s3=None):
        self.components = components
        self.iam = iam if iam is not None else FakeIam()
        self.s3 = s3 if s3 is not None else FakeS3()
        self.clients_requested = []

        def client(service_name, uc, session_name=None, region=None):
            self.clients_requested.append(service_name)
            if service_name == "iam":
                return self.iam
            if service_name == "s3":
                return self.s3
            raise AssertionError(f"unexpected client: {service_name}")

        with mock.patch.object(deployments, "get_usecase_client", client):
            self.result = deployments.deliver_workflow_tuning(
                components, usecase, account_id, region=REGION,
                session_name="tuning-property-test")

    # ------------------------------------------------------------ helpers
    def entry(self, name):
        return self.components[name]

    def merge_doc(self, name):
        update = self.components[name].get("configurationUpdate") or {}
        raw = update.get("merge")
        return json.loads(raw) if raw else {}

    def reset(self, name):
        update = self.components[name].get("configurationUpdate") or {}
        return list(update.get("reset") or [])


# --------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------

#: Values ``tuning_sample_export`` may carry in a stored Use_Case item.
#: Only a bool or the exact 'true'/'false' strings are recognized; every
#: other shape must read as disabled.
EXPORT_VALUES = st.sampled_from([
    True, False, "true", "false", "TRUE", "False", " true ", "  false  ",
    "TrUe", "yes", "no", "on", "off", "enabled", "1", "0", "", " ",
    "null", "None", 1, 0, 2, 1.0, None, [], {}, ["true"], {"enabled": True},
])

#: Values ``tuning_sample_retention_days`` may carry.
RETENTION_VALUES = st.sampled_from([
    7, 30, 45, 365, 6, 0, -1, 366, 1000, "7", " 45 ", "365", "6", "abc",
    "", None, 30.0, 30.5, True, False, [], {},
])

#: Values ``inference_uploader_s3_bucket`` may carry.
UPLOADER_BUCKET_VALUES = st.sampled_from([
    "fleet-results", "  padded-results  ", "", "   ", None, 42,
    ["a-bucket"], "dda-inference-results-999999999999",
])

ACCOUNT_VALUES = st.sampled_from(
    [ACCOUNT_ID, "999999999999", "", "   ", None])


@st.composite
def usecases(draw):
    """A stored Use_Case item: the two tuning settings in any shape (or
    absent), any bucket configuration, plus unrelated fields that must
    never influence the decision."""
    usecase = {"usecase_id": "uc-property", "name": "Property 17"}
    if draw(st.booleans()):
        usecase["account_id"] = draw(ACCOUNT_VALUES)
    if draw(st.booleans()):
        usecase["tuning_sample_export"] = draw(EXPORT_VALUES)
    if draw(st.booleans()):
        usecase["tuning_sample_retention_days"] = draw(RETENTION_VALUES)
    if draw(st.booleans()):
        usecase["inference_uploader_s3_bucket"] = draw(
            UPLOADER_BUCKET_VALUES)
    if draw(st.booleans()):
        # Unrelated settings that live beside the tuning ones.
        usecase["inference_uploader_enabled"] = draw(st.booleans())
        usecase["subscribe_topics"] = ["dda/trigger"]
    return usecase


#: Component names a deployment's map may carry. Exactly one LocalServer
#: entry per deployment (the device runs one), so the LocalServer name is
#: drawn separately from the other entries.
LOCAL_SERVER_NAMES = st.sampled_from([
    "aws.edgeml.dda.LocalServer.arm64JP6",
    "aws.edgeml.dda.LocalServer.arm64JP7",
    "aws.edgeml.dda.LocalServer.amd64",
])

OTHER_COMPONENTS = st.sampled_from([
    "aws.greengrass.Nucleus", "aws.greengrass.LogManager",
    "model-cookie-class", "dda.workflow.wf-1",
    "aws.edgeml.dda.InferenceUploader",
])

#: Existing ``configurationUpdate.merge`` documents on the LocalServer
#: entry: the recipe's own keys, the subscribe accessControl policies a
#: workflow deployment adds, a configuration carried over from a formerly
#: enabled Use_Case, and an unparseable merge.
FOREIGN_MERGE = {
    "accessControl": {"aws.greengrass.ipc.mqttproxy": {
        "dda:workflow-subscribe:wf-1": {
            "operations": ["aws.greengrass#SubscribeToIoTCore"],
            "resources": ["dda/trigger"]}}},
    "cameraTimeoutSec": 12,
}
CARRIED_OVER_TUNING = {
    "enabled": True, "bucket": "stale-results",
    "prefix": "workflow-tuning/samples/",
}


@st.composite
def component_maps(draw):
    """A deployment's component map: with or without a LocalServer entry,
    with or without an existing configuration update on it."""
    components = {}
    for name in draw(st.lists(OTHER_COMPONENTS, max_size=3, unique=True)):
        components[name] = {"componentVersion": "1.0.0"}

    local_server = None
    if draw(st.booleans()):
        local_server = draw(LOCAL_SERVER_NAMES)
        entry = {"componentVersion": "1.2.3"}
        shape = draw(st.sampled_from([
            "none", "empty", "foreign", "carried-over",
            "foreign+carried-over", "reset", "unparseable", "non-object",
        ]))
        if shape == "empty":
            entry["configurationUpdate"] = {"merge": json.dumps({})}
        elif shape == "foreign":
            entry["configurationUpdate"] = {
                "merge": json.dumps(FOREIGN_MERGE)}
        elif shape == "carried-over":
            entry["configurationUpdate"] = {"merge": json.dumps(
                {"workflowTuning": CARRIED_OVER_TUNING})}
        elif shape == "foreign+carried-over":
            entry["configurationUpdate"] = {"merge": json.dumps(
                dict(FOREIGN_MERGE, workflowTuning=CARRIED_OVER_TUNING))}
        elif shape == "reset":
            entry["configurationUpdate"] = {
                "merge": json.dumps(FOREIGN_MERGE),
                "reset": ["/workflowTuning", "/unrelated"]}
        elif shape == "unparseable":
            entry["configurationUpdate"] = {"merge": "{not json"}
        elif shape == "non-object":
            entry["configurationUpdate"] = {"merge": json.dumps([1, 2])}
        components[local_server] = entry
    return components, local_server


#: Lifecycle rules the Use_Case bucket may already carry (None = no
#: lifecycle configuration at all). Foreign rules must survive verbatim.
EXISTING_LIFECYCLE = st.sampled_from([
    None,
    [],
    [{"ID": "ExpireInferenceResults", "Filter": {"Prefix": "results/"},
      "Status": "Enabled", "Expiration": {"Days": 90}}],
    [{"ID": "DDAWorkflowTuningSamples",
      "Filter": {"Prefix": "workflow-tuning/samples/"},
      "Status": "Enabled", "Expiration": {"Days": 999},
      "NoncurrentVersionExpiration": {"NoncurrentDays": 999}}],
])


# ==========================================================================
# Property 17 (deployment half)
# ==========================================================================

@settings(max_examples=100, deadline=None)
@given(usecase=usecases(), maps=component_maps(),
       existing_rules=EXISTING_LIFECYCLE,
       account_argument=st.sampled_from([ACCOUNT_ID, None]))
def test_property_configuration_and_grants_delivered_iff_enabled(
        deployments, usecase, maps, existing_rules, account_argument):
    """**Feature: quality-prompt-tuning, Property 17: Configuration and
    grants are delivered iff export is enabled, and parsed safely**

    For any Use_Case and any deployment component map, the
    ``workflowTuning`` configuration, the device role statement and the
    Sample_Store lifecycle rules are delivered iff ``tuning_sample_export``
    is enabled, with the Use_Case's bucket and the
    ``workflow-tuning/samples/`` prefix — and the delivered configuration
    is one the device accepts, pointing at exactly that location.
    """
    components, local_server = maps
    expected_config, expected_action = ref_delivery(
        components, usecase, account_argument)
    before = copy.deepcopy(components)

    delivery = Delivery(deployments, components, usecase, account_argument,
                        s3=FakeS3(existing_rules))

    # ---------------------------------------------------------- the "iff"
    assert delivery.result == expected_config

    if expected_action == "nothing":
        # Requirement 11.3: nothing is delivered and nothing is touched —
        # neither the component map nor IAM nor S3.
        assert components == before
        assert delivery.clients_requested == []
        assert delivery.iam.put_calls == []
        assert delivery.s3.put_calls == []
        # No configuration was merged anywhere (a pre-existing withdrawal
        # pointer under `reset` is not a configuration).
        for name in components:
            merge_doc, parseable = ref_existing_merge(components[name])
            if parseable:
                assert REF_CONFIG_KEY not in merge_doc
        return

    if expected_action == "withdraw":
        # A configuration carried over from a formerly enabled Use_Case is
        # dropped from the merge AND reset on the device (Greengrass keeps
        # the last-applied value for a key a merge no longer mentions), and
        # still no IAM/S3 call is made.
        merge_doc = delivery.merge_doc(local_server)
        assert REF_CONFIG_KEY not in merge_doc
        assert "/workflowTuning" in delivery.reset(local_server)
        assert delivery.clients_requested == []
        assert delivery.iam.put_calls == []
        assert delivery.s3.put_calls == []
        # Every other key of the merge survives verbatim.
        previous, _ = ref_existing_merge(before[local_server])
        for key, value in previous.items():
            if key != REF_CONFIG_KEY:
                assert merge_doc[key] == value
        return

    # ------------------------------------------------- delivered: Req 2.1
    assert expected_config is not None
    bucket = expected_config["bucket"]
    assert expected_config == {"enabled": True, "bucket": bucket,
                               "prefix": REF_PREFIX}
    assert bucket == ref_bucket(usecase, account_argument)

    merge_doc = delivery.merge_doc(local_server)
    assert merge_doc[REF_CONFIG_KEY] == expected_config
    previous, _ = ref_existing_merge(before[local_server])
    for key, value in previous.items():
        if key != REF_CONFIG_KEY:
            assert merge_doc[key] == value, "a foreign merge key was lost"
    # Re-enabling must not reset the configuration it delivers.
    assert "/workflowTuning" not in delivery.reset(local_server)
    if "reset" in (before[local_server].get("configurationUpdate") or {}):
        assert "/unrelated" in delivery.reset(local_server)
    # No other component entry is touched.
    for name, entry in before.items():
        if name != local_server:
            assert components[name] == entry

    # ------------------------------------------------- delivered: Req 2.8
    [(role, policy, document)] = delivery.iam.put_calls
    assert role == REF_ROLE
    assert policy == REF_POLICY
    assert document == ref_device_policy(bucket)
    # "and no other new permission": one statement, two object actions,
    # scoped to this bucket's workflow-tuning/ prefix.
    [statement] = document["Statement"]
    assert sorted(statement["Action"]) == ["s3:GetObject", "s3:PutObject"]
    assert statement["Resource"] == (
        f"arn:aws:s3:::{bucket}/{REF_ROOT}*")

    # ------------------------------------------------- delivered: Req 2.9
    [(lifecycle_bucket, configuration)] = delivery.s3.put_calls
    assert lifecycle_bucket == bucket
    rules = {rule["ID"]: rule for rule in configuration["Rules"]}
    assert rules["DDAWorkflowTuningSamples"]["Filter"] == {
        "Prefix": REF_PREFIX}
    assert (rules["DDAWorkflowTuningSamples"]["Expiration"]["Days"]
            == ref_retention_days(usecase))
    for rule_id, prefix in (("DDAWorkflowTuningJobs", REF_ROOT + "jobs/"),
                            ("DDAWorkflowTuningSessions",
                             REF_ROOT + "sessions/")):
        assert rules[rule_id]["Filter"] == {"Prefix": prefix}
        assert (rules[rule_id]["Expiration"]["Days"]
                == REF_RUN_ARTIFACT_RETENTION)
    for rule in (existing_rules or []):
        if rule["ID"] not in rules:
            assert rule in configuration["Rules"], (
                "a foreign lifecycle rule was dropped")
    # Nothing outside workflow-tuning/ is expired by a rule of ours.
    for rule_id in ("DDAWorkflowTuningSamples", "DDAWorkflowTuningJobs",
                    "DDAWorkflowTuningSessions"):
        assert rules[rule_id]["Filter"]["Prefix"].startswith(REF_ROOT)
        assert rules[rule_id]["Status"] == "Enabled"

    # ---------------------------------- the device accepts what we deliver
    # The tie to the other half of Property 17: the delivered component
    # configuration parses on the device to exactly this location.
    if DEVICE_EXPORT_CONFIG is not None:
        parsed = DEVICE_EXPORT_CONFIG.from_component_configuration(
            json.loads(json.dumps(merge_doc)))
        assert parsed is not None, (
            "the delivered configuration is one the device rejects")
        assert (parsed.bucket, parsed.prefix) == (bucket, REF_PREFIX)

    # ------------------------------------------------------- idempotence
    # Every deployment of the Use_Case carries the configuration, but the
    # grant and the lifecycle rules are written only when they differ.
    second = Delivery(deployments, components, usecase, account_argument,
                      iam=delivery.iam, s3=delivery.s3)
    assert second.result == expected_config
    assert delivery.merge_doc(local_server) == merge_doc
    assert len(delivery.iam.put_calls) == 1
    assert len(delivery.s3.put_calls) == 1


# ==========================================================================
# Supporting guards: the property's reach
# ==========================================================================

class TestFlippingTheFlagFlipsDelivery:
    """The "iff" stated directly on a pair that differs only in the flag,
    so the property cannot be satisfied by a rule that never delivers (or
    always delivers)."""

    def usecase(self, **extra):
        return {"usecase_id": "uc", "account_id": ACCOUNT_ID, **extra}

    def components(self):
        return {"aws.edgeml.dda.LocalServer.arm64JP7": {
            "componentVersion": "1.2.3"}}

    @pytest.mark.parametrize("enabled_value", [True, "true", " TRUE "])
    def test_enabled_delivers(self, deployments, enabled_value):
        delivery = Delivery(
            deployments, self.components(),
            self.usecase(tuning_sample_export=enabled_value), ACCOUNT_ID)

        assert delivery.result == {
            "enabled": True,
            "bucket": f"dda-inference-results-{ACCOUNT_ID}",
            "prefix": REF_PREFIX}
        assert len(delivery.iam.put_calls) == 1
        assert len(delivery.s3.put_calls) == 1

    @pytest.mark.parametrize("settings_", [
        {}, {"tuning_sample_export": False},
        {"tuning_sample_export": "false"},
        {"tuning_sample_export": "yes"},
        {"tuning_sample_retention_days": 45},
    ])
    def test_disabled_delivers_nothing(self, deployments, settings_):
        components = self.components()

        delivery = Delivery(deployments, components,
                            self.usecase(**settings_), ACCOUNT_ID)

        assert delivery.result is None
        assert components == self.components()
        assert delivery.clients_requested == []


class TestBothSubmitPathsDeliver:
    """Requirement 2.1 says *every* deployment. The property drives the
    delivery function; this pins that both submit paths call it, so the
    property's reach is the whole deployment surface (the end-to-end
    handler cases are in test_tuning_export_configuration.py)."""

    def test_create_deployment_and_workflow_deployment_both_call_it(
            self, deployments):
        for function in (deployments.create_deployment,
                         deployments.create_workflow_deployment):
            source = inspect.getsource(function)
            assert re.search(r"\bdeliver_workflow_tuning\(", source), (
                f"{function.__name__} does not deliver the tuning "
                f"configuration")


class TestRestatementMatchesThePublishedContract:
    """The restatement above is independent, but it must describe the same
    contract the other surfaces read, or the property would pin a fiction.
    """

    def test_prefix_and_key_are_the_published_ones(self):
        import tuning_settings

        assert tuning_settings.SAMPLE_STORE_PREFIX == REF_PREFIX
        assert tuning_settings.TUNING_ROOT_PREFIX == REF_ROOT
        assert tuning_settings.COMPONENT_CONFIG_KEY == REF_CONFIG_KEY
        assert tuning_settings.DEVICE_ROLE_NAME == REF_ROLE
        assert tuning_settings.DEVICE_POLICY_NAME == REF_POLICY

    @pytest.mark.skipif(DEVICE_EXPORT_CONFIG is None,
                        reason="device tree not importable")
    def test_the_device_rejects_every_malformed_shape(self):
        """The second clause of Property 17 is asserted in full on the
        device side (task 3.4); this is the deployment side's sanity check
        that the shapes it can never deliver are the shapes the device
        rejects."""
        parse = DEVICE_EXPORT_CONFIG.from_component_configuration
        assert parse({}) is None                            # absent
        assert parse({"workflowTuning": "on"}) is None      # non-object
        assert parse({"workflowTuning": {
            "enabled": False, "bucket": "b", "prefix": REF_PREFIX}}) is None
        assert parse({"workflowTuning": {
            "enabled": True, "bucket": "", "prefix": REF_PREFIX}}) is None
        assert parse({"workflowTuning": {
            "enabled": True, "bucket": "b",
            "prefix": "workflow-tuning/samples"}}) is None
        valid = parse({"workflowTuning": {
            "enabled": True, "bucket": "b", "prefix": REF_PREFIX}})
        assert (valid.bucket, valid.prefix) == ("b", REF_PREFIX)
