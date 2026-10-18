"""
The Device_Access_Document (portal-authorization-and-iam-scoping, task 6.1;
design I6 "Design" and "Test strategy").

Covers `functions/device_grants.py` (the pure builders and the IAM-writing
pair, driven with a recording IAM fake passed as `iam_client`), its two call
sites in `functions/deployments.py` (both deployment-create paths write the
document before the Greengrass `create_deployment` call and request no IAM
client when the use case needs no statement), the `functions/usecases.py`
hooks on PUT and DELETE /usecases/{id}, and the constants the document shares
with the CDK grant, the use-case account template and the Drift_Check.

Harnesses: the deployment-path fakes of
test_workflow_packaging_deployment_integration.py (FakeGreengrass / FakeIot)
for deployments.py; usecase_account_fakes.install for usecases.py (recording
STS / S3 / EventBridge / Lambda fakes, a recording Attach_Step that admits
every bucket, a private moto use-case table), with the module's
`get_usecase_client` replaced by a recorder that hands out the IAM fake.
No AWS call is made.

Fixture accounts: 111111111111 (portal) and moto's 123456789012.

Validates: Requirements 8.1, 8.2
"""
import ast
import copy
import importlib.util
import inspect
import json
import os
import re
import sys
import uuid
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from botocore.exceptions import ClientError

# functions/ is on sys.path through conftest; device_grants creates no client,
# so it is imported outside the moto stack.
import device_grants
import usecase_account_fakes as fx
from test_workflow_packaging_deployment_integration import (
    ACCOUNT_ID, REGION, FakeGreengrass, FakeIot)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS = os.path.join(_REPO, "edge-cv-portal", "backend", "functions")
_DRIFT_CHECK = os.path.join(_REPO, "edge-cv-portal", "scripts", "portal-role-drift-check.py")
_COMPUTE_STACK = os.path.join(_REPO, "edge-cv-portal", "infrastructure", "lib", "compute-stack.ts")
_ACCOUNT_STACK = os.path.join(_REPO, "edge-cv-portal", "infrastructure", "lib",
                              "usecase-account-stack.ts")

ROLE = "GreengrassV2TokenExchangeRole"
POLICY = "DDAUseCaseDeviceAccess"
LOCAL_SERVER = "aws.edgeml.dda.LocalServer.arm64JP6"
THING = "device-grant-jp6"


def client_error(code, operation="Op", message=None):
    return ClientError({"Error": {"Code": code, "Message": message or code}}, operation)


def uid():
    return str(uuid.uuid4())


def name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def read_statement(usecase_id, *buckets):
    return {"Sid": device_grants.usecase_sid(usecase_id), "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:GetBucketLocation"],
            "Resource": [arn for b in buckets for arn in (f"arn:aws:s3:::{b}", f"arn:aws:s3:::{b}/*")]}


def results_statement(usecase_id, bucket):
    return {"Sid": device_grants.usecase_sid(usecase_id) + "Results", "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:PutObjectTagging", "s3:GetObject", "s3:GetBucketLocation"],
            "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}


def doc(*statements):
    return {"Version": "2012-10-17", "Statement": list(statements)}


class FakeIam:
    """The device role's inline policies, and every call in order. With
    `as_string` the document is returned URL-encoded, as the IAM API sends
    it before the SDK decodes it."""

    def __init__(self, document=None, order=None, as_string=False):
        self.policies = {}
        if document is not None:
            self.policies[(ROLE, POLICY)] = copy.deepcopy(document)
        self.calls = []
        self.puts = []
        self.order = order if order is not None else []
        self.get_error = None
        self.put_error = None
        self.as_string = as_string

    def get_role_policy(self, RoleName, PolicyName):
        self.calls.append(("get_role_policy", RoleName, PolicyName))
        if self.get_error is not None:
            raise self.get_error
        if (RoleName, PolicyName) not in self.policies:
            raise client_error("NoSuchEntity", "GetRolePolicy",
                               f"The role policy with name {PolicyName} cannot be found.")
        document = copy.deepcopy(self.policies[(RoleName, PolicyName)])
        if self.as_string:
            document = quote(json.dumps(document))
        return {"RoleName": RoleName, "PolicyName": PolicyName, "PolicyDocument": document}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.calls.append(("put_role_policy", RoleName, PolicyName))
        if self.put_error is not None:
            raise self.put_error
        assert isinstance(PolicyDocument, str)
        document = json.loads(PolicyDocument)
        self.puts.append(document)
        self.policies[(RoleName, PolicyName)] = document
        self.order.append("iam.put_role_policy")
        return {}

    def document(self):
        return self.policies.get((ROLE, POLICY))

    def __getattr__(self, attribute):
        raise AssertionError(f"unexpected IAM call {attribute}")


# ===================================================================== builders

class TestDeviceReadBuckets:
    def test_in_pattern_bucket_and_no_additional_buckets_is_empty(self):
        assert device_grants.device_read_buckets({"s3_bucket": "dda-media"}) == []
        assert device_grants.device_read_buckets({"s3_bucket": "plant-dda-images"}) == []

    def test_out_of_pattern_bucket_is_listed(self):
        assert device_grants.device_read_buckets({"s3_bucket": "customer-media"}) == ["customer-media"]

    def test_only_the_out_of_pattern_additional_bucket_is_listed(self):
        usecase = {"s3_bucket": "dda-media",
                   "additional_s3_buckets": ["team-dda-extra", "customer-extra"]}
        assert device_grants.device_read_buckets(usecase) == ["customer-extra"]

    def test_data_bucket_is_never_listed(self):
        usecase = {"s3_bucket": "dda-media", "data_s3_bucket": "customer-training-data"}
        assert device_grants.device_read_buckets(usecase) == []

    def test_duplicates_collapse_and_names_sort(self):
        usecase = {"s3_bucket": "zeta-media",
                   "additional_s3_buckets": ["alpha-media", "zeta-media", "alpha-media"]}
        assert device_grants.device_read_buckets(usecase) == ["alpha-media", "zeta-media"]

    def test_a_string_set_reads_like_a_list(self):
        usecase = {"s3_bucket": "dda-media", "additional_s3_buckets": {"beta-media", "alpha-media"}}
        assert device_grants.device_read_buckets(usecase) == ["alpha-media", "beta-media"]

    @pytest.mark.parametrize("usecase", [None, {}, {"s3_bucket": ""}, {"s3_bucket": 7},
                                         {"additional_s3_buckets": [None, "  "]}])
    def test_absent_or_malformed_fields_list_nothing(self, usecase):
        assert device_grants.device_read_buckets(usecase) == []


class TestUsecaseStatements:
    def test_nothing_out_of_pattern_needs_no_statement(self):
        usecase = {"usecase_id": uid(), "s3_bucket": "dda-media",
                   "inference_uploader_s3_bucket": f"dda-inference-results-{ACCOUNT_ID}"}
        assert device_grants.usecase_statements(usecase) == []

    def test_one_bucket_is_one_read_statement_with_two_arns(self):
        usecase_id = uid()
        assert device_grants.usecase_statements(
            {"usecase_id": usecase_id, "s3_bucket": "customer-media"}) == [
            read_statement(usecase_id, "customer-media")]

    def test_three_buckets_are_one_statement_with_six_arns(self):
        usecase_id = uid()
        usecase = {"usecase_id": usecase_id, "s3_bucket": "customer-media",
                   "additional_s3_buckets": ["b-extra", "a-extra", "dda-ignored"]}
        [statement] = device_grants.usecase_statements(usecase)
        assert statement == read_statement(usecase_id, "a-extra", "b-extra", "customer-media")
        assert len(statement["Resource"]) == 6

    def test_out_of_pattern_override_is_the_results_statement(self):
        usecase_id = uid()
        usecase = {"usecase_id": usecase_id, "s3_bucket": "dda-media",
                   "inference_uploader_s3_bucket": " fleet-results "}
        assert device_grants.usecase_statements(usecase) == [results_statement(usecase_id, "fleet-results")]

    def test_both_statements(self):
        usecase_id = uid()
        usecase = {"usecase_id": usecase_id, "s3_bucket": "customer-media",
                   "inference_uploader_s3_bucket": "fleet-results"}
        assert device_grants.usecase_statements(usecase) == [
            read_statement(usecase_id, "customer-media"), results_statement(usecase_id, "fleet-results")]

    def test_sids_have_the_drift_check_shape_and_nothing_is_granted_on_every_bucket(self):
        usecase = {"usecase_id": uid(), "s3_bucket": "customer-media",
                   "inference_uploader_s3_bucket": "fleet-results"}
        for statement in device_grants.usecase_statements(usecase):
            assert re.match(device_grants.SID_RE, statement["Sid"])
            for resource in statement["Resource"]:
                assert re.fullmatch(r"arn:aws:s3:::[a-z0-9.-]+(/\*)?", resource)

    def test_a_record_without_an_id_has_no_statement(self):
        assert device_grants.usecase_statements({"s3_bucket": "customer-media"}) == []


class TestMergeDocument:
    def test_upsert_into_an_absent_document(self):
        usecase_id = uid()
        statements = [read_statement(usecase_id, "customer-media")]
        document, changed = device_grants.merge_document(None, usecase_id, statements)
        assert changed is True
        assert document == doc(*statements)

    def test_upsert_replaces_only_this_use_cases_sids_in_place(self):
        mine, before, after = uid(), uid(), uid()
        existing = doc(read_statement(before, "x-bucket"), read_statement(mine, "old-bucket"),
                       results_statement(mine, "old-results"), read_statement(after, "y-bucket"))
        new = [read_statement(mine, "new-bucket")]
        document, changed = device_grants.merge_document(existing, mine, new)
        assert changed is True
        assert document == doc(read_statement(before, "x-bucket"), read_statement(mine, "new-bucket"),
                               read_statement(after, "y-bucket"))

    def test_re_merging_the_held_statements_changes_nothing(self):
        mine, other = uid(), uid()
        existing = doc(read_statement(mine, "a-bucket"), read_statement(other, "b-bucket"))
        document, changed = device_grants.merge_document(
            existing, mine, [read_statement(mine, "a-bucket")])
        assert changed is False
        assert document == existing

    def test_removal_keeps_every_other_statement(self):
        mine, other = uid(), uid()
        existing = doc(read_statement(mine, "a-bucket"), results_statement(mine, "r-bucket"),
                       read_statement(other, "b-bucket"))
        document, changed = device_grants.merge_document(existing, mine, [])
        assert (document, changed) == (doc(read_statement(other, "b-bucket")), True)

    def test_removing_what_is_absent_changes_nothing(self):
        other = uid()
        existing = doc(read_statement(other, "b-bucket"))
        assert device_grants.merge_document(existing, uid(), [])[1] is False
        assert device_grants.merge_document(None, uid(), []) == (doc(), False)

    def test_a_single_statement_object_is_read_as_a_list(self):
        other = uid()
        existing = {"Version": "2012-10-17", "Statement": read_statement(other, "b-bucket")}
        document, changed = device_grants.merge_document(existing, other, [read_statement(other, "b-bucket")])
        assert document == doc(read_statement(other, "b-bucket"))


class TestDocumentSize:
    def test_is_the_compact_serialization(self):
        document = doc(read_statement(uid(), "customer-media"))
        assert device_grants.document_size(document) == len(json.dumps(document, separators=(",", ":")))

    def test_one_more_63_character_bucket_costs_less_than_200_bytes(self):
        usecase_id = uid()
        long_a, long_b = "a" * 63, "b" * 63
        one = doc(*device_grants.usecase_statements({"usecase_id": usecase_id, "s3_bucket": long_a}))
        two = doc(*device_grants.usecase_statements({"usecase_id": usecase_id, "s3_bucket": long_a,
                                                     "additional_s3_buckets": [long_b]}))
        growth = device_grants.document_size(two) - device_grants.document_size(one)
        assert 0 < growth < 200
        # Exactly the bucket's two ARNs and the separating comma: no second statement.
        arns = json.dumps([f"arn:aws:s3:::{long_b}", f"arn:aws:s3:::{long_b}/*"], separators=(",", ":"))
        assert growth == len(arns) - 2 + 1

    def test_the_cap_is_6000_bytes(self):
        assert device_grants.DEVICE_ACCESS_POLICY_MAX_BYTES == 6_000


# ============================================================ the writing pair

class TestGrantAndRevoke:
    def test_grant_writes_the_merged_document_once(self):
        mine, other = uid(), uid()
        iam = FakeIam(doc(read_statement(other, "b-bucket")))
        result = device_grants.grant_device_access(
            {"usecase_id": mine, "s3_bucket": "customer-media"}, iam_client=iam, region=REGION)
        assert result["status"] == "granted"
        assert (result["role_name"], result["policy_name"]) == (ROLE, POLICY)
        assert iam.calls == [("get_role_policy", ROLE, POLICY), ("put_role_policy", ROLE, POLICY)]
        assert iam.puts == [doc(read_statement(other, "b-bucket"), read_statement(mine, "customer-media"))]
        assert result["size"] == device_grants.document_size(iam.puts[0])

    def test_grant_is_idempotent(self):
        usecase = {"usecase_id": uid(), "s3_bucket": "customer-media"}
        iam = FakeIam()
        assert device_grants.grant_device_access(usecase, iam_client=iam)["status"] == "granted"
        assert device_grants.grant_device_access(usecase, iam_client=iam)["status"] == "unchanged"
        assert device_grants.grant_device_access(usecase, iam_client=iam)["status"] == "unchanged"
        assert len(iam.puts) == 1

    def test_an_absent_policy_reads_as_an_empty_document(self):
        usecase_id = uid()
        iam = FakeIam()
        device_grants.grant_device_access({"usecase_id": usecase_id, "s3_bucket": "customer-media"},
                                          iam_client=iam)
        assert iam.document() == doc(read_statement(usecase_id, "customer-media"))

    def test_a_url_encoded_document_is_decoded(self):
        mine, other = uid(), uid()
        iam = FakeIam(doc(read_statement(other, "b-bucket")), as_string=True)
        device_grants.grant_device_access({"usecase_id": mine, "s3_bucket": "customer-media"},
                                          iam_client=iam)
        assert iam.puts == [doc(read_statement(other, "b-bucket"), read_statement(mine, "customer-media"))]

    def test_over_the_cap_is_policy_size_limit_and_nothing_is_written(self):
        cap = device_grants.DEVICE_ACCESS_POLICY_MAX_BYTES
        filler = []
        while device_grants.document_size(doc(*filler, read_statement(uid(), "f" * 63))) <= cap - 100:
            filler.append(read_statement(uid(), "f" * 63))
        iam = FakeIam(doc(*filler))
        assert cap - 420 < device_grants.document_size(iam.document()) <= cap - 100
        # Four 63-character buckets: one statement of about 790 bytes.
        buckets = [f"{i}" + "g" * 62 for i in range(4)]
        result = device_grants.grant_device_access(
            {"usecase_id": uid(), "s3_bucket": buckets[0], "additional_s3_buckets": buckets[1:]},
            iam_client=iam)
        assert result["status"] == "failed" and result["reason"] == "policy_size_limit"
        assert result["remedy"] == device_grants.REMEDY_POLICY_SIZE_LIMIT
        assert "dda-*" in result["remedy"] and "no longer" in result["remedy"]
        assert result["size"] > device_grants.DEVICE_ACCESS_POLICY_MAX_BYTES
        assert iam.puts == []

    def test_limit_exceeded_from_iam_is_policy_size_limit(self):
        iam = FakeIam()
        iam.put_error = client_error("LimitExceeded", "PutRolePolicy",
                                     "Maximum policy size of 10240 bytes exceeded for role")
        result = device_grants.grant_device_access({"usecase_id": uid(), "s3_bucket": "customer-media"},
                                                   iam_client=iam)
        assert (result["status"], result["reason"]) == ("failed", "policy_size_limit")
        assert result["remedy"] == device_grants.REMEDY_POLICY_SIZE_LIMIT

    @pytest.mark.parametrize("side", ["get", "put"])
    def test_any_other_error_is_failed_with_the_error_never_raised(self, side):
        iam = FakeIam()
        setattr(iam, f"{side}_error", client_error("AccessDenied", "Op", "not authorized"))
        result = device_grants.grant_device_access({"usecase_id": uid(), "s3_bucket": "customer-media"},
                                                   iam_client=iam)
        assert result["status"] == "failed"
        assert result["error"] == "AccessDenied: not authorized"
        assert "reason" not in result

    def test_a_non_aws_error_is_failed_too(self):
        iam = FakeIam()
        iam.get_error = RuntimeError("connection reset")
        result = device_grants.revoke_device_access({"usecase_id": uid()}, iam_client=iam)
        assert result == {"status": "failed", "role_name": ROLE, "policy_name": POLICY,
                          "error": "connection reset"}

    def test_revoke_removes_exactly_this_use_cases_sids(self):
        mine, other = uid(), uid()
        iam = FakeIam(doc(read_statement(mine, "a-bucket"), read_statement(other, "b-bucket"),
                          results_statement(mine, "r-bucket")))
        result = device_grants.revoke_device_access({"usecase_id": mine}, iam_client=iam)
        assert result["status"] == "removed"
        assert iam.document() == doc(read_statement(other, "b-bucket"))

    def test_revoke_without_a_statement_is_unchanged(self):
        iam = FakeIam(doc(read_statement(uid(), "b-bucket")))
        assert device_grants.revoke_device_access({"usecase_id": uid()}, iam_client=iam) == {
            "status": "unchanged", "role_name": ROLE, "policy_name": POLICY}
        assert iam.puts == []

    def test_a_grant_that_no_longer_needs_statements_removes_them(self):
        mine, other = uid(), uid()
        iam = FakeIam(doc(read_statement(other, "b-bucket"), read_statement(mine, "customer-media")))
        result = device_grants.grant_device_access({"usecase_id": mine, "s3_bucket": "dda-media"},
                                                   iam_client=iam)
        assert result["status"] == "removed"
        assert iam.document() == doc(read_statement(other, "b-bucket"))

    def test_a_removal_that_would_empty_the_document_writes_nothing(self):
        mine = uid()
        iam = FakeIam(doc(read_statement(mine, "customer-media")))
        result = device_grants.revoke_device_access({"usecase_id": mine}, iam_client=iam)
        assert (result["status"], result["reason"]) == ("failed", "document_would_be_empty")
        assert result["remedy"] == device_grants.REMEDY_EMPTY_DOCUMENT
        assert f"delete-role-policy --role-name {ROLE} --policy-name {POLICY}" in result["remedy"]
        assert iam.puts == []

    def test_the_writers_create_no_client(self):
        tree = ast.parse(open(os.path.join(_FUNCTIONS, "device_grants.py"), encoding="utf-8").read())
        imported, called = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
            elif isinstance(node, ast.Call):
                func = node.func
                called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
        assert imported == {"fnmatch", "json", "logging", "urllib.parse", "tuning_settings"}
        assert not called & {"client", "resource", "Session", "get_usecase_client", "create_boto3_client"}


# ===================================================== constants shared elsewhere

def _load(path, module_name):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSharedConstants:
    def test_the_role_name_is_tuning_settings_one(self):
        import tuning_settings
        assert device_grants.DEVICE_ROLE_NAME is tuning_settings.DEVICE_ROLE_NAME
        source = open(os.path.join(_FUNCTIONS, "device_grants.py"), encoding="utf-8").read()
        assert "'GreengrassV2TokenExchangeRole'" not in source and '"GreengrassV2TokenExchangeRole"' not in source

    def test_the_cdk_grants_name_the_settings_default_role(self, monkeypatch):
        monkeypatch.delenv("DEVICE_TOKEN_EXCHANGE_ROLE", raising=False)
        default = _load(os.path.join(_FUNCTIONS, "tuning_settings.py"),
                        "tuning_settings_default_probe").DEVICE_ROLE_NAME
        source = open(_COMPUTE_STACK, encoding="utf-8").read()
        for role in ("deploymentsRole", "usecasesRole"):
            blocks = [block.split("}));")[0] for block in source.split(f"{role}.addToPolicy(")[1:]]
            grants = [block for block in blocks if "iam:PutRolePolicy" in block]
            assert len(grants) == 1, role
            assert re.findall(r"arn:aws:iam::\$\{cdk\.Aws\.ACCOUNT_ID\}:role/([A-Za-z0-9+=,.@_-]+)`",
                              grants[0]) == [default], role
            assert "'iam:GetRolePolicy', 'iam:PutRolePolicy'" in grants[0]

    def test_template_patterns_are_the_account_stacks(self):
        source = open(_ACCOUNT_STACK, encoding="utf-8").read()
        block = source.split("const modelArtifactResources = [", 1)[1].split("];", 1)[0]
        resources = re.findall(r"'([^']+)'", block)
        assert sorted(r for r in resources if not r.endswith("/*")) == sorted(
            f"arn:aws:s3:::{pattern}" for pattern in device_grants.TEMPLATE_BUCKET_PATTERNS)
        assert sorted(r[:-2] for r in resources if r.endswith("/*")) == sorted(
            f"arn:aws:s3:::{pattern}" for pattern in device_grants.TEMPLATE_BUCKET_PATTERNS)
        results = source.split("sid: 'AllowInferenceResultsUpload'", 1)[1].split("})", 1)[0]
        assert f"'arn:aws:s3:::{device_grants.TEMPLATE_RESULTS_PATTERN}'" in results

    def test_the_drift_check_constants_equal_device_grants(self):
        drift = _load(_DRIFT_CHECK, "portal_role_drift_check_constants_probe")
        grants = _load(os.path.join(_FUNCTIONS, "device_grants.py"), "device_grants_constants_probe")
        for constant in ("DEVICE_ACCESS_POLICY_NAME", "SID_RE", "READ_ACTIONS", "RESULTS_ACTIONS",
                         "TEMPLATE_BUCKET_PATTERNS"):
            assert getattr(drift, constant) == getattr(grants, constant), constant
        # The two derivations the check and the writer share agree as well.
        record = {"usecase_id": uid(), "s3_bucket": "customer-media",
                  "additional_s3_buckets": ["dda-x", "b-extra", "b-extra"]}
        assert drift.device_read_buckets(record) == grants.device_read_buckets(record)
        assert drift.usecase_sid(record["usecase_id"]) == grants.usecase_sid(record["usecase_id"])


# ================================================== deployments.py call sites

@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


class DeployEnv:
    """Both deployment-create paths with recording Use_Case-account clients;
    `order` interleaves the IAM writes and the Greengrass submissions."""

    def __init__(self, env, deployments, monkeypatch, **usecase_fields):
        self.env = env
        self.deployments = deployments
        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_id = uid()
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id, "name": "Device Access Delivery Test",
            "account_id": ACCOUNT_ID, **usecase_fields})
        self.order = []
        self.gg = FakeGreengrass()
        self.iot = FakeIot()
        self.iam = FakeIam(order=self.order)
        self.iam_client_error = None
        self.clients_requested = []
        submit = self.gg.create_deployment

        def create_deployment(**params):
            self.order.append("greengrass.create_deployment")
            return submit(**params)

        self.gg.create_deployment = create_deployment

        def client(service_name, usecase, session_name=None, region=None):
            assert usecase["usecase_id"] == self.usecase_id
            self.clients_requested.append(service_name)
            if service_name == "greengrassv2":
                return self.gg
            if service_name == "iot":
                return self.iot
            if service_name == "iam":
                if self.iam_client_error is not None:
                    raise self.iam_client_error
                return self.iam
            raise AssertionError(f"unexpected client: {service_name}")

        monkeypatch.setattr(deployments, "get_usecase_client", client)
        self.gg.register_device(THING, local_server_version="99.0.0", arch="arm64JP6")

    def deploy(self):
        body = {"usecase_id": self.usecase_id, "target_devices": [THING],
                "components": [{"component_name": LOCAL_SERVER, "component_version": "1.2.0"}]}
        response = self.deployments.handler(
            self.env.event("POST", "/deployments", self.user, body=body), None)
        return response["statusCode"], json.loads(response["body"])

    def deploy_workflow(self):
        workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
        self.env.stack.tables.workflows.put_item(Item={
            "workflow_id": workflow_id, "usecase_id": self.usecase_id, "name": "device-access",
            "latest_version": 1, "created_at": 1})
        self.env.stack.tables.versions.put_item(Item={
            "workflow_id": workflow_id, "version": 1, "validation_status": {"status": "passed"},
            "component_arn": (f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:components:"
                              f"dda.workflow.{workflow_id}:versions:1.0.0")})
        body = {"component_type": "workflow", "usecase_id": self.usecase_id,
                "workflow_id": workflow_id, "target_devices": [THING]}
        response = self.deployments.handler(
            self.env.event("POST", "/deployments", self.user, body=body), None)
        return response["statusCode"], json.loads(response["body"])


PATHS = ["create_deployment", "create_workflow_deployment"]


def submit(dep, path):
    return dep.deploy() if path == "create_deployment" else dep.deploy_workflow()


class TestDeploymentCallSites:
    @pytest.mark.parametrize("path", PATHS)
    def test_the_document_is_written_before_the_greengrass_submission(self, env, deployments,
                                                                     monkeypatch, path):
        bucket = name("customer-media")
        dep = DeployEnv(env, deployments, monkeypatch, s3_bucket=bucket)
        status, payload = submit(dep, path)
        assert status == 201, payload
        assert dep.order == ["iam.put_role_policy", "greengrass.create_deployment"]
        assert dep.iam.document() == doc(read_statement(dep.usecase_id, bucket))
        assert payload["device_grants"] == {"device_access": {
            "status": "granted", "role_name": ROLE, "policy_name": POLICY,
            "size": device_grants.document_size(dep.iam.document())}}

    @pytest.mark.parametrize("path", PATHS)
    def test_no_iam_client_when_the_use_case_needs_no_statement(self, env, deployments,
                                                               monkeypatch, path):
        dep = DeployEnv(env, deployments, monkeypatch, s3_bucket=name("dda-media"),
                        inference_uploader_s3_bucket=f"dda-inference-results-{ACCOUNT_ID}")
        status, payload = submit(dep, path)
        assert status == 201, payload
        assert "iam" not in dep.clients_requested
        assert "device_grants" not in payload
        assert dep.order == ["greengrass.create_deployment"]

    @pytest.mark.parametrize("path", PATHS)
    def test_a_failed_grant_never_fails_the_deployment(self, env, deployments, monkeypatch, path):
        dep = DeployEnv(env, deployments, monkeypatch, s3_bucket=name("customer-media"))
        dep.iam.put_error = client_error("AccessDenied", "PutRolePolicy", "not authorized")
        status, payload = submit(dep, path)
        assert status == 201, payload
        assert payload["device_grants"]["device_access"]["status"] == "failed"
        assert dep.order == ["greengrass.create_deployment"]

    @pytest.mark.parametrize("path", PATHS)
    def test_an_unavailable_iam_client_never_fails_the_deployment(self, env, deployments,
                                                                  monkeypatch, path):
        dep = DeployEnv(env, deployments, monkeypatch, s3_bucket=name("customer-media"))
        dep.iam_client_error = client_error("AccessDenied", "AssumeRole", "denied")
        status, payload = submit(dep, path)
        assert status == 201, payload
        assert payload["device_grants"]["device_access"] == {
            "status": "failed", "role_name": ROLE, "policy_name": POLICY, "error": "AccessDenied: denied"}

    def test_redeploying_writes_nothing_new(self, env, deployments, monkeypatch):
        dep = DeployEnv(env, deployments, monkeypatch, s3_bucket=name("customer-media"),
                        inference_uploader_s3_bucket="fleet-results")
        assert dep.deploy()[0] == 201
        status, payload = dep.deploy()
        assert status == 201, payload
        assert payload["device_grants"]["device_access"]["status"] == "unchanged"
        assert len(dep.iam.puts) == 1
        assert [s["Sid"] for s in dep.iam.document()["Statement"]] == [
            device_grants.usecase_sid(dep.usecase_id), device_grants.usecase_sid(dep.usecase_id) + "Results"]

    def test_the_call_sits_outside_the_tuning_delivery_and_off_the_remediation_paths(self, deployments):
        for function in (deployments.create_deployment, deployments.create_workflow_deployment):
            source = inspect.getsource(function)
            assert source.count("deliver_device_access(") == 1, function.__name__
            assert source.index("deliver_workflow_tuning(") < source.index("deliver_device_access(") \
                < source.index("greengrass_client.create_deployment("), function.__name__
        for function in (deployments.deliver_workflow_tuning, deployments._submit_store_remediation,
                         deployments._resume_original_deployment,
                         deployments.remediate_component_store_failures):
            source = inspect.getsource(function)
            assert "device_grants" not in source and "deliver_device_access" not in source

    def test_no_handler_imports_another_handler_module(self):
        def imports(module_file):
            tree = ast.parse(open(os.path.join(_FUNCTIONS, module_file), encoding="utf-8").read())
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module)
            return names
        assert "device_grants" in imports("deployments.py") and "device_grants" in imports("usecases.py")
        assert "usecases" not in imports("deployments.py")
        assert "deployments" not in imports("usecases.py")


# ====================================================== usecases.py hooks

@pytest.fixture(scope="module")
def shared(aws_stack):
    import shared_utils
    assert hasattr(shared_utils, "account_trusted"), "this suite needs the real shared layer"
    return shared_utils


@pytest.fixture(scope="module")
def uc(aws_stack, shared):
    sys.modules.pop("usecases", None)
    import usecases
    return usecases


@pytest.fixture
def hooks(aws_stack, shared, uc, monkeypatch):
    fakes = fx.install(uc, shared, monkeypatch)
    fx.clear_usecases_table()
    state = SimpleNamespace(uc=uc, shared=shared, stack=aws_stack, fakes=fakes, mp=monkeypatch,
                            iam=FakeIam(), requested=[], client_error=None)

    def client(service_name, usecase, session_name=None, region=None):
        state.requested.append((service_name, usecase.get("usecase_id")))
        if service_name != "iam":
            raise AssertionError(f"unexpected client: {service_name}")
        if state.client_error is not None:
            raise state.client_error
        return state.iam

    monkeypatch.setattr(uc, "get_usecase_client", client)
    state.admin = fx.portal_admin(aws_stack)
    return state


def put_record(**fields):
    """A Single_Account_Record with a uuid id (the Sid shape) and an
    in-pattern s3_bucket unless `fields` says otherwise."""
    usecase_id = uid()
    item = {"usecase_id": usecase_id, "name": f"Device access {usecase_id[:8]}",
            "account_id": fx.PORTAL, "cross_account_role_arn": fx.root_arn(fx.PORTAL),
            "sagemaker_execution_role_arn": fx.sagemaker_role(fx.PORTAL),
            "s3_bucket": name("dda-media"), "region": fx.REGION, "setup_type": "single-account"}
    item.update(fields)
    fx.usecases_table().put_item(Item=item)
    return usecase_id


def put(h, usecase_id, body):
    return fx.call(h.uc, fx.api_event("PUT", f"/usecases/{usecase_id}", h.admin, usecase_id, body))


def delete(h, usecase_id):
    return fx.call(h.uc, fx.api_event("DELETE", f"/usecases/{usecase_id}", h.admin, usecase_id))


class TestUpdateHook:
    def test_moving_s3_bucket_out_of_the_pattern_adds_the_statement_and_keeps_every_other(self, hooks):
        other = read_statement(uid(), "neighbour-media")
        hooks.iam = FakeIam(doc(other))
        usecase_id = put_record()
        bucket = name("customer-media")
        status, payload, _ = put(hooks, usecase_id, {"s3_bucket": bucket})
        assert status == 200, payload
        assert hooks.iam.puts == [doc(other, read_statement(usecase_id, bucket))]
        assert payload["device_grants"]["device_access"]["status"] == "granted"
        assert hooks.requested == [("iam", usecase_id)]

    def test_moving_it_back_into_the_pattern_removes_the_statement_only(self, hooks):
        bucket = name("customer-media")
        usecase_id = put_record(s3_bucket=bucket)
        other = read_statement(uid(), "neighbour-media")
        hooks.iam = FakeIam(doc(read_statement(usecase_id, bucket), other))
        status, payload, _ = put(hooks, usecase_id, {"s3_bucket": name("dda-media")})
        assert status == 200, payload
        assert hooks.iam.puts == [doc(other)]
        assert payload["device_grants"]["device_access"]["status"] == "removed"

    def test_adding_an_out_of_pattern_bucket_extends_the_statement_by_two_arns(self, hooks):
        bucket, extra = name("customer-media"), name("customer-extra")
        usecase_id = put_record(s3_bucket=bucket)
        other = read_statement(uid(), "neighbour-media")
        hooks.iam = FakeIam(doc(read_statement(usecase_id, bucket), other))
        status, payload, _ = put(hooks, usecase_id, {"additional_s3_buckets_add": [extra]})
        assert status == 200, payload
        assert payload["added"] == [extra]
        assert hooks.iam.puts == [doc(read_statement(usecase_id, *sorted([bucket, extra])), other)]
        [statement, _] = hooks.iam.document()["Statement"]
        assert len(statement["Resource"]) == 4
        assert payload["device_grants"]["device_access"]["status"] == "granted"

    def test_adding_an_in_pattern_bucket_makes_no_iam_call(self, hooks):
        usecase_id = put_record(s3_bucket=name("customer-media"))
        status, payload, _ = put(hooks, usecase_id, {"additional_s3_buckets_add": [name("dda-extra")]})
        assert status == 200, payload
        assert hooks.requested == [] and hooks.iam.calls == []
        assert "device_grants" not in payload

    def test_a_refused_add_makes_no_iam_call(self, hooks):
        refused = name("customer-untagged")
        validator = hooks.fakes.validator

        def attach(**kwargs):
            if kwargs["bucket"] == refused:
                raise hooks.shared.BucketNotEligible(
                    "The bucket is not tagged for the portal", bucket=refused,
                    usecase_id=kwargs["target_usecase_id"], purpose=kwargs["purpose"], reason="no_tags")
            return validator(**kwargs)

        hooks.mp.setattr(hooks.uc, "attach_bucket", attach)
        usecase_id = put_record()
        status, payload, _ = put(hooks, usecase_id, {"additional_s3_buckets_add": [refused]})
        assert status == 400 and payload["error_code"] == "bucket_not_eligible", payload
        assert hooks.requested == [] and hooks.iam.calls == []

    def test_removing_the_last_out_of_pattern_bucket_removes_the_statement(self, hooks):
        extra = name("customer-extra")
        usecase_id = put_record(additional_s3_buckets=[extra])
        other = read_statement(uid(), "neighbour-media")
        hooks.iam = FakeIam(doc(other, read_statement(usecase_id, extra)))
        status, payload, _ = put(hooks, usecase_id, {"additional_s3_buckets_remove": [extra]})
        assert status == 200, payload
        assert payload["removed"] == [extra]
        assert hooks.iam.puts == [doc(other)]
        assert payload["device_grants"]["device_access"]["status"] == "removed"

    def test_a_description_only_update_makes_no_iam_call(self, hooks):
        usecase_id = put_record(s3_bucket=name("customer-media"))
        status, payload, _ = put(hooks, usecase_id, {"description": "renamed line"})
        assert status == 200, payload
        assert hooks.requested == [] and "device_grants" not in payload

    def test_a_failed_write_never_fails_the_update(self, hooks):
        usecase_id = put_record()
        hooks.client_error = client_error("AccessDenied", "AssumeRole", "denied")
        bucket = name("customer-media")
        status, payload, _ = put(hooks, usecase_id, {"s3_bucket": bucket})
        assert status == 200, payload
        assert payload["device_grants"]["device_access"] == {
            "status": "failed", "role_name": ROLE, "policy_name": POLICY, "error": "AccessDenied: denied"}
        assert fx.record(usecase_id)["s3_bucket"] == bucket


class TestDeleteHook:
    def test_delete_removes_exactly_this_use_cases_sids(self, hooks):
        usecase_id = put_record(s3_bucket=name("customer-media"), inference_uploader_s3_bucket="fleet-results")
        other = read_statement(uid(), "neighbour-media")
        hooks.iam = FakeIam(doc(read_statement(usecase_id, "customer-media"), other,
                                results_statement(usecase_id, "fleet-results")))
        status, payload, _ = delete(hooks, usecase_id)
        assert status == 200, payload
        assert hooks.iam.puts == [doc(other)]
        assert payload["grants_removed"]["device_access"]["status"] == "removed"
        assert fx.record(usecase_id) is None

    def test_delete_without_a_statement_is_unchanged(self, hooks):
        usecase_id = put_record()
        hooks.iam = FakeIam(doc(read_statement(uid(), "neighbour-media")))
        status, payload, _ = delete(hooks, usecase_id)
        assert status == 200, payload
        assert payload["grants_removed"]["device_access"] == {
            "status": "unchanged", "role_name": ROLE, "policy_name": POLICY}
        assert hooks.iam.puts == []

    def test_delete_reports_a_failed_revoke_and_still_deletes(self, hooks):
        usecase_id = put_record(s3_bucket=name("customer-media"))
        hooks.iam.get_error = client_error("AccessDenied", "GetRolePolicy", "not authorized")
        status, payload, _ = delete(hooks, usecase_id)
        assert status == 200, payload
        assert payload["grants_removed"]["device_access"] == {
            "status": "failed", "role_name": ROLE, "policy_name": POLICY,
            "error": "AccessDenied: not authorized"}
        assert fx.record(usecase_id) is None

    def test_delete_of_the_only_statement_reports_the_manual_removal(self, hooks):
        usecase_id = put_record(s3_bucket=name("customer-media"))
        hooks.iam = FakeIam(doc(read_statement(usecase_id, "customer-media")))
        status, payload, _ = delete(hooks, usecase_id)
        assert status == 200, payload
        device_access = payload["grants_removed"]["device_access"]
        assert (device_access["status"], device_access["reason"]) == ("failed", "document_would_be_empty")
        assert hooks.iam.puts == []

    def test_delete_of_an_unknown_use_case_attempts_no_revoke(self, hooks):
        status, payload, _ = delete(hooks, uid())
        assert status == 200, payload
        assert payload["grants_removed"] == {"status": "skipped", "reason": "usecase_not_found"}
        assert hooks.requested == []


class TestAccountMoveHook:
    """A PUT that moves a use case to another account rewrites the
    Device_Access_Document in both accounts (design I1, I6; Requirements
    2.5.5, 8.2): the move revokes the use case's statement in the old
    account's device role (with the stored record's client) and grants it in
    the new account's (with the reloaded record's), reported under
    grants_removed / grants_added. It does both even when the device-read
    buckets are unchanged, which the bucket-change hook never covers.

    The Drift_Check lists an account's own use cases whose statement is
    missing or stale, never a leftover for a use case that has moved away, so
    the revoke is the only thing that clears the old account; and without the
    new-account grant the moved use case would read as 'missing' there once it
    has a deployment.
    """

    @staticmethod
    def _multi_record(usecase_id, account, bucket):
        fx.usecases_table().put_item(Item={
            "usecase_id": usecase_id, "name": "move-device", "account_id": account,
            "cross_account_role_arn": fx.access_role(account),
            "sagemaker_execution_role_arn": fx.sagemaker_role(account),
            "external_id": "ext-stored", "s3_bucket": bucket,
            "region": fx.REGION, "setup_type": "multi-account"})

    def test_move_revokes_in_the_old_account_and_grants_in_the_new(self, hooks):
        hooks.mp.setenv("TRUSTED_USECASE_ACCOUNT_IDS", f"{fx.TRUSTED},{fx.DATA}")
        bucket = "customer-media"                 # out of the dda-* / *-dda-* pattern
        usecase_id, neighbour = uid(), uid()
        self._multi_record(usecase_id, fx.TRUSTED, bucket)

        # The old account's device role already carries the moved use case's
        # statement (an earlier deployment wrote it) beside a neighbour's.
        old_iam = FakeIam(doc(read_statement(usecase_id, bucket),
                              read_statement(neighbour, "neighbour-media")))
        new_iam = FakeIam()
        iams = {fx.TRUSTED: old_iam, fx.DATA: new_iam}

        def client(service_name, usecase, session_name=None, region=None):
            assert service_name == "iam", service_name
            return iams[usecase["account_id"]]

        hooks.mp.setattr(hooks.uc, "get_usecase_client", client)

        body = {"account_id": fx.DATA, "cross_account_role_arn": fx.access_role(fx.DATA),
                "external_id": "ext-moved", "sagemaker_execution_role_arn": fx.sagemaker_role(fx.DATA)}
        status, payload, _ = put(hooks, usecase_id, body)
        assert status == 200, payload

        # The statement leaves the old account and appears in the new, each
        # reported beside the other moved grants.
        assert payload["grants_removed"]["device_access"]["status"] == "removed"
        assert payload["grants_added"]["device_access"]["status"] == "granted"
        assert old_iam.document() == doc(read_statement(neighbour, "neighbour-media"))
        assert new_iam.document() == doc(read_statement(usecase_id, bucket))

        # Drift_Check: the old account's leftover is invisible to it (the moved
        # record names the new account, so the check skips the use case there),
        # which is why the revoke is necessary; on the new account the grant the
        # move wrote clears what would otherwise be a 'missing' finding.
        drift = _load(_DRIFT_CHECK, "portal_role_drift_check_move_probe")
        moved = fx.record(usecase_id)
        assert moved["account_id"] == fx.DATA
        assert drift.missing_or_stale(
            [moved], {usecase_id}, fx.TRUSTED, doc(read_statement(usecase_id, bucket))) == []
        assert drift.missing_or_stale([moved], {usecase_id}, fx.DATA, new_iam.document()) == []
        assert drift.missing_or_stale([moved], {usecase_id}, fx.DATA, None) == [
            (usecase_id, "missing", [bucket])]
