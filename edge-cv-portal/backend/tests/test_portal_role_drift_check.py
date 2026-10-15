"""
Unit tests for edge-cv-portal/scripts/portal-role-drift-check.py
(portal-authorization-and-iam-scoping, task 6.3; design I6 "Design").

The script is loaded by file path and driven with fixture policy sets through
a fake IAM client that only implements the six read operations; no AWS call is
made.

Validates: Requirements 8.4
"""
import importlib.util
import json
import os
import re
from types import SimpleNamespace
from urllib.parse import quote

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_SCRIPT = os.path.join(_REPO, "edge-cv-portal", "scripts", "portal-role-drift-check.py")

PORTAL = "111111111111"
USECASE = "222222222222"
DEVICE_ROLE = "GreengrassV2TokenExchangeRole"
LAMBDA_ROLE = "EdgeCVPortalComputeStack-ModelImportHandlerRoleAB12CD34-EF56GH78"
# uuid4-shaped use-case ids; the last group keeps a hex letter so no
# twelve-digit run (which the output mask treats as an account id) appears.
UC_B = "0b6f2c1e-1111-4a2b-9c3d-a00000000001"
UC_C = "0b6f2c1e-1111-4a2b-9c3d-a00000000002"
UC_D = "0b6f2c1e-1111-4a2b-9c3d-a00000000003"
UC_E = "0b6f2c1e-1111-4a2b-9c3d-a00000000004"
UC_F = "0b6f2c1e-1111-4a2b-9c3d-a00000000005"
UC_A = "0b6f2c1e-1111-4a2b-9c3d-a00000000006"


def _load_script():
    spec = importlib.util.spec_from_file_location("portal_role_drift_check", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


drift = _load_script()


def doc(*statements):
    return {"Version": "2012-10-17", "Statement": list(statements)}


def allow(actions, resources, sid=None, condition=None):
    statement = {"Effect": "Allow", "Action": actions, "Resource": resources}
    if sid:
        statement["Sid"] = sid
    if condition:
        statement["Condition"] = condition
    return statement


def sid(usecase_id, results=False):
    return "UseCase" + usecase_id.replace("-", "") + ("Results" if results else "")


TEMPLATE_COMPONENT_ACCESS = doc(
    allow(["s3:GetObject", "s3:GetBucketLocation"],
          [f"arn:aws:s3:::dda-component-us-east-1-{PORTAL}", f"arn:aws:s3:::dda-component-us-east-1-{PORTAL}/*"],
          "AllowPortalComponentBucketAccess"),
    allow(["s3:GetObject", "s3:GetBucketLocation", "s3:HeadObject"],
          ["arn:aws:s3:::dda-*", "arn:aws:s3:::dda-*/*", "arn:aws:s3:::*-dda-*", "arn:aws:s3:::*-dda-*/*"],
          "AllowDDABucketPatternAccess"),
    allow(["s3:PutObject", "s3:PutObjectTagging", "s3:GetObject", "s3:GetBucketLocation"],
          ["arn:aws:s3:::dda-inference-results-*", "arn:aws:s3:::dda-inference-results-*/*"],
          "AllowInferenceResultsUpload"),
    allow(["ecr:GetAuthorizationToken"], ["*"], "AllowEcrAuthToken"),
)
TEMPLATE_COMPONENT_S3 = doc(allow(["s3:GetObject", "s3:GetObjectVersion"],
                                  ["arn:aws:s3:::dda-component-*/*",
                                   "arn:aws:s3:::dda-component-us-east-1-*/*"]))
TEMPLATE_LOGS = doc(allow(["logs:CreateLogGroup", "logs:PutLogEvents"],
                          "arn:aws:logs:*:*:log-group:/aws/greengrass/*"))
DEVICE_ACCESS = doc(
    allow(list(drift.READ_ACTIONS), ["arn:aws:s3:::customer-bucket", "arn:aws:s3:::customer-bucket/*"],
          sid(UC_C)),
    allow(list(drift.RESULTS_ACTIONS), ["arn:aws:s3:::custom-results", "arn:aws:s3:::custom-results/*"],
          sid(UC_C, results=True)),
    allow(list(drift.READ_ACTIONS), ["arn:aws:s3:::f-bucket", "arn:aws:s3:::f-bucket/*",
                                     "arn:aws:s3:::f-extra", "arn:aws:s3:::f-extra/*"], sid(UC_F)),
)
TUNING = doc(allow(["s3:PutObject", "s3:GetObject"],
                   f"arn:aws:s3:::dda-inference-results-{PORTAL}/workflow-tuning/*",
                   "DDAWorkflowTuningSampleAccess"))
HAND_GET_ALL = doc(allow("s3:GetObject", "arn:aws:s3:::*"), allow("s3:GetBucketLocation", "*"))
HAND_LOGS_ONLY = doc(allow(["logs:PutLogEvents"], "*"))


class FakeIAM:
    """IAM fake with the six read operations only; pages lists one item at a
    time; inline documents come back URL-encoded like the raw API."""

    def __init__(self, role, inline, attached):
        self.role = role
        self.inline = inline
        self.attached = attached
        self.calls = []

    def _page(self, items, key, marker):
        index = int(marker or 0)
        response = {key: items[index:index + 1]}
        if index + 1 < len(items):
            response.update(IsTruncated=True, Marker=str(index + 1))
        return response

    def get_role(self, RoleName):
        self.calls.append("get_role")
        return {"Role": self.role}

    def list_role_policies(self, RoleName, Marker=None):
        self.calls.append("list_role_policies")
        return self._page(list(self.inline), "PolicyNames", Marker)

    def get_role_policy(self, RoleName, PolicyName):
        self.calls.append("get_role_policy")
        return {"PolicyDocument": quote(json.dumps(self.inline[PolicyName]))}

    def list_attached_role_policies(self, RoleName, Marker=None):
        self.calls.append("list_attached_role_policies")
        items = [{"PolicyName": name, "PolicyArn": arn} for name, (arn, _) in self.attached.items()]
        return self._page(items, "AttachedPolicies", Marker)

    def get_policy(self, PolicyArn):
        self.calls.append("get_policy")
        return {"Policy": {"DefaultVersionId": "v3"}}

    def get_policy_version(self, PolicyArn, VersionId):
        self.calls.append("get_policy_version")
        document = next(d for arn, d in self.attached.values() if arn == PolicyArn)
        return {"PolicyVersion": {"Document": document}}


def device_role():
    return {"RoleName": DEVICE_ROLE, "Arn": f"arn:aws:iam::{PORTAL}:role/{DEVICE_ROLE}",
            "AssumeRolePolicyDocument": doc({"Effect": "Allow", "Action": "sts:AssumeRole",
                                             "Principal": {"Service": "credentials.iot.amazonaws.com"}})}


def lambda_role(name=LAMBDA_ROLE):
    return {"RoleName": name, "Arn": f"arn:aws:iam::{PORTAL}:role/{name}",
            "AssumeRolePolicyDocument": doc({"Effect": "Allow", "Action": "sts:AssumeRole",
                                             "Principal": {"Service": "lambda.amazonaws.com"}})}


def managed(name, document):
    return (f"arn:aws:iam::{PORTAL}:policy/{name}", document)


def aws_managed(name, document):
    return (f"arn:aws:iam::aws:policy/service-role/{name}", document)


def run_check(iam, **kwargs):
    lines = []
    state = drift.read_role(iam, iam.role["RoleName"])
    status = drift.check(state, lines.append, **kwargs)
    return status, lines, state


def verdicts(state):
    return {p["name"]: p["verdict"] for p in state["policies"]}


# ------------------------------------------------------------- constants

def test_shape_constants():
    assert drift.DEVICE_ACCESS_POLICY_NAME == "DDAUseCaseDeviceAccess"
    assert drift.SID_RE == r"^UseCase[0-9a-f]{32}(Results)?$"
    assert drift.READ_ACTIONS == ("s3:GetObject", "s3:GetObjectVersion", "s3:GetBucketLocation")
    assert drift.RESULTS_ACTIONS == ("s3:PutObject", "s3:PutObjectTagging", "s3:GetObject",
                                     "s3:GetBucketLocation")
    assert drift.TEMPLATE_BUCKET_PATTERNS == ("dda-*", "*-dda-*")
    assert re.match(drift.SID_RE, sid(UC_B)) and re.match(drift.SID_RE, sid(UC_B, results=True))
    assert not re.match(drift.SID_RE, "UseCase" + "0" * 31)


def _read(rel):
    with open(os.path.join(_REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def test_embedded_templates_come_from_the_repository():
    """Every template policy name and S3 resource pattern the script embeds
    appears in the repository file that produces it, so the two cannot drift
    unnoticed."""
    sources = "\n".join(_read(rel) for rel in (
        "station_install/setup_station.sh", "station_install/create-edge-device-iam-role.sh",
        "station_install/edge-device-iam-policy.json", "README.md",
        "edge-cv-portal/infrastructure/lib/usecase-account-stack.ts",
        "edge-cv-portal/infrastructure/lib/compute-stack.ts"))
    for name in drift.DEVICE_ROLE_TEMPLATE_POLICIES:
        assert name in sources, name
    # the provisioner's policy for the device role, which compute-stack.ts lets
    # the station provisioning role manage by name
    assert DEVICE_ROLE + drift.PROVISIONER_POLICY_SUFFIX in sources
    for _actions, resources in drift.DEVICE_ROLE_TEMPLATE_S3_STATEMENTS:
        for resource in resources:
            if resource != "*":
                assert resource in sources, resource
    stack = _read("edge-cv-portal/infrastructure/lib/usecase-account-stack.ts")
    assert "const componentBucketName = props.componentBucketName || `dda-component-${region}-${portalAccountId}`" in stack
    for policy in drift.PORTAL_AWS_MANAGED_POLICIES:
        assert policy in sources or policy in "\n".join(
            _read(f"edge-cv-portal/infrastructure/lib/{n}")
            for n in os.listdir(os.path.join(_REPO, "edge-cv-portal/infrastructure/lib")) if n.endswith(".ts"))


# --------------------------------------------------------- device role

def test_device_role_classification_and_exit_2_for_hand_attached_s3():
    iam = FakeIAM(device_role(),
                  inline={"GreengrassComponentS3Access": TEMPLATE_COMPONENT_S3,
                          "CloudWatchLogsPolicy": TEMPLATE_LOGS,
                          drift.DEVICE_ACCESS_POLICY_NAME: DEVICE_ACCESS,
                          drift.TUNING_SAMPLE_POLICY_NAME: TUNING},
                  attached={"DDAPortalComponentAccessPolicy": managed("DDAPortalComponentAccessPolicy",
                                                                      TEMPLATE_COMPONENT_ACCESS),
                            "HandGetEverything": managed("HandGetEverything", HAND_GET_ALL)})
    status, lines, state = run_check(iam)
    assert verdicts(state) == {
        "GreengrassComponentS3Access": "in-template", "CloudWatchLogsPolicy": "in-template",
        "DDAUseCaseDeviceAccess": "portal-written", "DDAWorkflowTuningSampleAccess": "portal-written",
        "DDAPortalComponentAccessPolicy": "in-template", "HandGetEverything": "hand-attached"}
    assert status == 2
    assert "hand-attached S3 statements: 2" in lines
    assert any(line.startswith("  HandGetEverything [-] ") and "arn:aws:s3:::*" in line for line in lines)


def test_hand_attached_policy_without_s3_does_not_fail_the_check():
    iam = FakeIAM(device_role(), inline={"CloudWatchLogsPolicy": TEMPLATE_LOGS},
                  attached={"HandLogs": managed("HandLogs", HAND_LOGS_ONLY)})
    status, lines, state = run_check(iam)
    assert verdicts(state)["HandLogs"] == "hand-attached"
    assert status == 0
    assert "hand-attached S3 statements: 0" in lines


@pytest.mark.parametrize("document", [
    doc(allow(["s3:GetObject"], "arn:aws:s3:::*")),                                   # wider resource
    doc(allow(["s3:*"], "arn:aws:s3:::dda-component-*/*")),                            # wider action
    doc(allow(["s3:GetObject", "s3:DeleteObject"], "arn:aws:s3:::dda-component-*/*")),  # extra action
    doc({"Effect": "Allow", "NotAction": "iam:*", "Resource": "*"}),
])
def test_a_template_named_policy_beyond_the_templates_is_hand_attached(document):
    iam = FakeIAM(device_role(), inline={"GreengrassComponentS3Access": document}, attached={})
    status, _, state = run_check(iam)
    assert verdicts(state) == {"GreengrassComponentS3Access": "hand-attached"}
    assert status == 2


@pytest.mark.parametrize("statement", [
    allow(list(drift.READ_ACTIONS), ["arn:aws:s3:::b", "arn:aws:s3:::b/*"], "Foreign"),          # Sid shape
    allow(list(drift.READ_ACTIONS), ["arn:aws:s3:::b"], "UseCase" + "A" * 32),                   # upper hex
    allow(["s3:GetObject", "s3:PutObject"], ["arn:aws:s3:::b", "arn:aws:s3:::b/*"], sid(UC_B)),  # write in read
    allow(["s3:DeleteObject"], ["arn:aws:s3:::b/*"], sid(UC_B, results=True)),
    allow(["s3:Get*"], ["arn:aws:s3:::b/*"], sid(UC_B)),
    allow(list(drift.READ_ACTIONS), ["arn:aws:s3:::*"], sid(UC_B)),                              # wildcard
    allow(list(drift.READ_ACTIONS), ["arn:aws:s3:::b/*"], sid(UC_B),
          condition={"Bool": {"aws:SecureTransport": "true"}}),
    {"Effect": "Deny", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::b/*"], "Sid": sid(UC_B)},
])
def test_a_portal_named_document_with_a_foreign_statement_is_hand_attached(statement):
    document = doc(allow(list(drift.READ_ACTIONS), ["arn:aws:s3:::ok", "arn:aws:s3:::ok/*"], sid(UC_C)),
                   statement)
    iam = FakeIAM(device_role(), inline={drift.DEVICE_ACCESS_POLICY_NAME: document}, attached={})
    _, _, state = run_check(iam)
    assert verdicts(state) == {drift.DEVICE_ACCESS_POLICY_NAME: "hand-attached"}


@pytest.mark.parametrize("document", [
    doc(allow(["s3:*"], "arn:aws:s3:::bucket/workflow-tuning/*")),
    doc(allow(["s3:GetObject"], "arn:aws:s3:::bucket/*")),
    doc(),
])
def test_a_tuning_named_document_of_another_shape_is_hand_attached(document):
    iam = FakeIAM(device_role(), inline={drift.TUNING_SAMPLE_POLICY_NAME: document}, attached={})
    _, _, state = run_check(iam)
    assert verdicts(state) == {drift.TUNING_SAMPLE_POLICY_NAME: "hand-attached"}


def test_the_portal_writers_documents_are_portal_written():
    """The documents the two existing portal writers put on the device role
    (workflow-tuning samples, stream-camera credentials) classify as
    portal-written, built by the writers' own functions."""
    import stream_credentials
    import tuning_settings
    assert drift.TUNING_SAMPLE_POLICY_NAME == tuning_settings.DEVICE_POLICY_NAME
    assert drift.STREAM_CREDENTIAL_POLICY_NAME == stream_credentials.DEVICE_POLICY_NAME
    tuning = tuning_settings.device_policy_document(f"dda-inference-results-{PORTAL}")
    stream = stream_credentials.device_policy_document("us-east-1", PORTAL)
    iam = FakeIAM(device_role(), inline={tuning_settings.DEVICE_POLICY_NAME: tuning,
                                         stream_credentials.DEVICE_POLICY_NAME: stream}, attached={})
    status, _, state = run_check(iam)
    assert set(verdicts(state).values()) == {"portal-written"} and status == 0

    widened = doc(allow(["secretsmanager:GetSecretValue"],
                        f"arn:aws:secretsmanager:us-east-1:{PORTAL}:secret:*"))
    iam = FakeIAM(device_role(), inline={stream_credentials.DEVICE_POLICY_NAME: widened}, attached={})
    assert verdicts(run_check(iam)[2]) == {stream_credentials.DEVICE_POLICY_NAME: "hand-attached"}


def test_the_provisioner_policy_is_in_template_only_in_its_installed_shape():
    provisioner = DEVICE_ROLE + drift.PROVISIONER_POLICY_SUFFIX
    installed = doc(allow(["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                           "logs:DescribeLogStreams", "s3:GetBucketLocation"], "*"))
    iam = FakeIAM(device_role(), inline={}, attached={provisioner: managed(provisioner, installed)})
    status, _, state = run_check(iam)
    assert verdicts(state) == {provisioner: "in-template"} and status == 0

    edited = doc(allow(["logs:PutLogEvents", "iot:Connect", "s3:*"], "*", "VisualEditor0"))
    iam = FakeIAM(device_role(), inline={}, attached={provisioner: managed(provisioner, edited)})
    status, _, state = run_check(iam)
    assert verdicts(state) == {provisioner: "hand-attached"} and status == 2

    # the suffix rule names the policy of the role being checked, nothing else
    other = "OtherRole" + drift.PROVISIONER_POLICY_SUFFIX
    iam = FakeIAM(device_role(), inline={}, attached={other: managed(other, installed)})
    assert verdicts(run_check(iam)[2]) == {other: "hand-attached"}


def test_aws_managed_policy_on_the_device_role_is_hand_attached():
    iam = FakeIAM(device_role(), inline={},
                  attached={"AmazonS3ReadOnlyAccess": aws_managed("AmazonS3ReadOnlyAccess",
                                                                  doc(allow(["s3:Get*", "s3:List*"], "*")))})
    status, _, state = run_check(iam)
    assert verdicts(state) == {"AmazonS3ReadOnlyAccess": "hand-attached"}
    assert status == 2


def test_inline_policy_size_against_the_limit():
    iam = FakeIAM(device_role(), inline={"GreengrassComponentS3Access": TEMPLATE_COMPONENT_S3,
                                         drift.DEVICE_ACCESS_POLICY_NAME: DEVICE_ACCESS},
                  attached={"DDAPortalComponentAccessPolicy": managed("DDAPortalComponentAccessPolicy",
                                                                      TEMPLATE_COMPONENT_ACCESS)})
    _, lines, _ = run_check(iam)
    expected = sum(len(json.dumps(d, separators=(",", ":")))
                   for d in (TEMPLATE_COMPONENT_S3, DEVICE_ACCESS))
    assert f"inline policy size: {expected} / 10240 characters" in lines


# ------------------------------------------------- missing or stale statements

def _usecases():
    return [
        {"usecase_id": UC_A, "account_id": PORTAL, "s3_bucket": "dda-a-bucket"},          # in pattern
        {"usecase_id": UC_B, "account_id": PORTAL, "s3_bucket": "customer-b"},            # missing
        {"usecase_id": UC_C, "account_id": PORTAL, "s3_bucket": "customer-bucket",
         "additional_s3_buckets": ["newly-allowed", "my-dda-bucket"]},                     # stale
        {"usecase_id": UC_D, "account_id": PORTAL, "s3_bucket": "not-deployed"},          # no deployment
        {"usecase_id": UC_E, "account_id": USECASE, "s3_bucket": "other-account"},       # another account
        {"usecase_id": UC_F, "account_id": PORTAL, "s3_bucket": "f-bucket",
         "additional_s3_buckets": {"f-extra"}},                                            # covered
    ]


def test_missing_and_stale_statements():
    deployed = {UC_A, UC_B, UC_C, UC_E, UC_F}
    findings = drift.missing_or_stale(_usecases(), deployed, PORTAL, DEVICE_ACCESS)
    assert findings == [(UC_B, "missing", ["customer-b"]),
                        (UC_C, "stale", ["newly-allowed"])]
    assert drift.missing_or_stale(_usecases(), deployed, PORTAL, None) == [
        (UC_B, "missing", ["customer-b"]), (UC_C, "missing", ["customer-bucket", "newly-allowed"]),
        (UC_F, "missing", ["f-bucket", "f-extra"])]


def test_device_read_buckets():
    assert drift.device_read_buckets({"s3_bucket": "dda-x"}) == []
    assert drift.device_read_buckets({"s3_bucket": "b", "additional_s3_buckets": ["a", "b", "x-dda-y"]}) == ["a", "b"]
    assert drift.device_read_buckets({"s3_bucket": None, "additional_s3_buckets": None}) == []


def test_check_prints_the_missing_or_stale_section():
    iam = FakeIAM(device_role(), inline={drift.DEVICE_ACCESS_POLICY_NAME: DEVICE_ACCESS}, attached={})
    status, lines, _ = run_check(iam, usecases=_usecases(), deployed={UC_B, UC_C, UC_F})
    assert status == 0
    assert "device access statements: DDAUseCaseDeviceAccess present; use cases missing or stale: 2" in lines
    assert f"  missing {sid(UC_B)} (use case {UC_B}): customer-b" in lines
    assert f"  stale {sid(UC_C)} (use case {UC_C}): newly-allowed" in lines


# ------------------------------------------------------- portal Lambda roles

def test_lambda_role_object_statements_with_condition_or_unconditioned():
    conditioned = {"StringEquals": {"aws:ResourceTag/dda-portal:managed": "true"}}
    iam = FakeIAM(lambda_role(),
                  inline={"ModelImportHandlerRoleDefaultPolicy1A2B3C4D": doc(
                      allow(["s3:GetObject", "s3:PutObject"], "arn:aws:s3:::*/*"),
                      allow(["dynamodb:GetItem"], "*"))},
                  attached={"AWSLambdaBasicExecutionRole": aws_managed("AWSLambdaBasicExecutionRole",
                                                                        doc(allow(["logs:PutLogEvents"], "*"))),
                            "EdgeCVPortalComputeStack-ModelImportHandlerRoleOverflowPolicy1ABCDEF12-XYZ":
                                managed("EdgeCVPortalComputeStack-ModelImportHandlerRoleOverflowPolicy1ABCDEF12-XYZ",
                                        doc(allow(["s3:GetObject"], "arn:aws:s3:::*/*", condition=conditioned),
                                            allow(["s3:ListBucket", "s3:GetBucketLocation"], "arn:aws:s3:::*",
                                                  condition=conditioned),
                                            allow(["s3:GetBucketTagging", "s3:GetBucketABAC"], "arn:aws:s3:::*")))})
    status, lines, state = run_check(iam)
    assert set(verdicts(state).values()) == {"in-template"}
    assert status == 0
    assert lines[0].endswith("portal Lambda role")
    objects = lines[lines.index("S3 object statements:") + 1:
                    lines.index("S3 ListBucket / GetBucketLocation statements:")]
    assert objects == [
        '  ModelImportHandlerRoleDefaultPolicy1A2B3C4D [-] ["s3:GetObject", "s3:PutObject"] on '
        '["arn:aws:s3:::*/*"]: UNCONDITIONED',
        '  EdgeCVPortalComputeStack-ModelImportHandlerRoleOverflowPolicy1ABCDEF12-XYZ [-] ["s3:GetObject"] on '
        '["arn:aws:s3:::*/*"]: ' + json.dumps(conditioned, sort_keys=True),
    ]
    bucket_level = lines[lines.index("S3 ListBucket / GetBucketLocation statements:") + 1:]
    assert len(bucket_level) == 1 and bucket_level[0].endswith(json.dumps(conditioned, sort_keys=True))


def test_lambda_role_hand_put_inline_s3_policy_fails_the_check():
    iam = FakeIAM(lambda_role(), inline={"Inline": doc(allow(["s3:GetObject"], "arn:aws:s3:::*/*"))},
                  attached={"CustomerPolicy": managed("CustomerPolicy", HAND_LOGS_ONLY)})
    status, lines, state = run_check(iam)
    assert verdicts(state) == {"Inline": "hand-attached", "CustomerPolicy": "hand-attached"}
    assert status == 2
    assert any(line.endswith("UNCONDITIONED") for line in lines)


def test_a_role_without_the_portal_prefix_is_not_a_portal_lambda_role():
    iam = FakeIAM(lambda_role("OtherServiceRole"),
                  inline={"OtherServiceRoleDefaultPolicy1A2B3C4D": doc(allow(["s3:GetObject"], "arn:aws:s3:::*/*"))},
                  attached={})
    status, lines, _ = run_check(iam)
    assert status == 2
    assert "S3 object statements:" not in lines
    status, lines, _ = run_check(FakeIAM(lambda_role("OtherServiceRole"), inline={}, attached={}),
                                 role_prefix="Other")
    assert lines[0].endswith("portal Lambda role")


# ------------------------------------------------------------- read path

def test_read_role_uses_only_iam_read_calls_and_pages():
    iam = FakeIAM(device_role(),
                  inline={"GreengrassComponentS3Access": TEMPLATE_COMPONENT_S3, "CloudWatchLogsPolicy": TEMPLATE_LOGS},
                  attached={"DDAPortalComponentAccessPolicy": managed("DDAPortalComponentAccessPolicy",
                                                                      TEMPLATE_COMPONENT_ACCESS),
                            "HandLogs": managed("HandLogs", HAND_LOGS_ONLY)})
    state = drift.read_role(iam, DEVICE_ROLE)
    assert [p["name"] for p in state["policies"]] == [
        "GreengrassComponentS3Access", "CloudWatchLogsPolicy", "DDAPortalComponentAccessPolicy", "HandLogs"]
    assert state["policies"][0]["document"] == TEMPLATE_COMPONENT_S3      # URL-encoded JSON decoded
    assert set(iam.calls) == {"get_role", "list_role_policies", "get_role_policy",
                              "list_attached_role_policies", "get_policy", "get_policy_version"}
    assert iam.calls.count("list_role_policies") == 2 and iam.calls.count("list_attached_role_policies") == 2


class FakeTable:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        return {"Items": self.items}


def _session(iam, tables):
    def factory(profile_name=None, region_name=None):
        return SimpleNamespace(client=lambda service: iam,
                               resource=lambda service: SimpleNamespace(Table=lambda name: tables[name]))
    return factory


def test_main_masks_account_ids_and_reads_the_tables():
    iam = FakeIAM(device_role(), inline={drift.DEVICE_ACCESS_POLICY_NAME: DEVICE_ACCESS},
                  attached={"HandGetEverything": managed("HandGetEverything", HAND_GET_ALL)})
    tables = {"uc": FakeTable(_usecases()), "dep": FakeTable([{"usecase_id": UC_B}])}
    lines = []
    status = drift.main(["--role-name", DEVICE_ROLE, "--usecases-table", "uc", "--deployments-table", "dep"],
                        session_factory=_session(iam, tables),
                        out=lambda line: lines.append(drift.mask(line)))
    text = "\n".join(lines)
    assert status == 2
    assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", text)
    assert "********1111" in text
    assert f"  missing {sid(UC_B)} (use case {UC_B}): customer-b" in lines
    projection = tables["uc"].calls[0]["ExpressionAttributeNames"]
    assert set(projection.values()) == {"usecase_id", "s3_bucket", "additional_s3_buckets", "account_id"}


def test_main_usage_and_read_errors_exit_1():
    iam = FakeIAM(device_role(), inline={}, attached={})
    assert drift.main(["--role-name", DEVICE_ROLE, "--usecases-table", "uc"],
                      session_factory=_session(iam, {}), out=lambda line: None) == 1

    def missing_role(RoleName):
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": "NoSuchEntity", "Message": "x"}}, "GetRole")

    iam.get_role = missing_role
    lines = []
    assert drift.main(["--role-name", "absent"], session_factory=_session(iam, {}), out=lines.append) == 1
    assert lines == ["read failed: NoSuchEntity"]
    with pytest.raises(SystemExit) as info:
        drift.main([], session_factory=_session(iam, {}), out=lambda line: None)
    assert info.value.code == 1
