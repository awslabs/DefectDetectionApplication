"""
Unit tests for edge-cv-portal/scripts/portal-bucket-abac-audit.py
(portal-authorization-and-iam-scoping, task 5.1; design I5 "Existing
registered buckets").

The script is loaded by file path; every AWS client is a fixture fake, so no
AWS call is made. The single-account predicate is the shared layer's own
`is_cross_account_setup`, loaded through the script's path loader.

Validates: Requirements 7.3, 7.9
"""
import importlib.util
import os
import re
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.abspath(os.path.join(_HERE, "..", "..", "scripts", "portal-bucket-abac-audit.py"))

PORTAL = "111111111111"
USECASE = "222222222222"
DATA = "333333333333"
REGION = "us-east-1"
ARTIFACTS = f"dda-portal-artifacts-{PORTAL}-{REGION}"
RESULTS = f"dda-inference-results-{PORTAL}"


def _load_script():
    spec = importlib.util.spec_from_file_location("portal_bucket_abac_audit", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load_script()


@pytest.fixture(scope="module")
def is_cross():
    """The shared layer's predicate, loaded the way the script loads it."""
    return audit.load_shared_layer().is_cross_account_setup


def client_error(code, status=403, operation="Operation"):
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


# ------------------------------------------------------------------- fakes

class FakeTable:
    def __init__(self, items, page_size=2):
        self.items = list(items)
        self.page_size = page_size
        self.calls = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        start = kwargs.get("ExclusiveStartKey", {}).get("index", 0)
        page = self.items[start:start + self.page_size]
        response = {"Items": page}
        if start + self.page_size < len(self.items):
            response["LastEvaluatedKey"] = {"index": start + self.page_size}
        return response


class FakeS3:
    """Bucket state keyed by name: location, tags (None = NoSuchTagSet, an
    exception = raised), abac ('Enabled' | 'Disabled' | None)."""

    def __init__(self, region, buckets, log, writes_allowed):
        self.region = region
        self.buckets = buckets
        self.log = log
        self.writes_allowed = writes_allowed

    def _bucket(self, op, bucket):
        self.log.append((op, self.region, bucket))
        return self.buckets[bucket]

    def get_bucket_location(self, Bucket):
        return {"LocationConstraint": self._bucket("get_bucket_location", Bucket)["location"]}

    def get_bucket_tagging(self, Bucket):
        tags = self._bucket("get_bucket_tagging", Bucket)["tags"]
        if isinstance(tags, Exception):
            raise tags
        if tags is None:
            raise client_error("NoSuchTagSet", 404, "GetBucketTagging")
        return {"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]}

    def get_bucket_abac(self, Bucket):
        status = self._bucket("get_bucket_abac", Bucket)["abac"]
        return {"AbacStatus": {} if status is None else {"Status": status}}

    def put_bucket_abac(self, Bucket, AbacStatus):
        if not self.writes_allowed:
            raise AssertionError("put_bucket_abac outside --apply")
        self.log.append(("put_bucket_abac", self.region, Bucket))


class FakeS3Control:
    def __init__(self, region, log, writes_allowed):
        self.region = region
        self.log = log
        self.writes_allowed = writes_allowed

    def tag_resource(self, AccountId, ResourceArn, Tags):
        if not self.writes_allowed:
            raise AssertionError("tag_resource outside --apply")
        self.log.append(("tag_resource", self.region, ResourceArn, AccountId, tuple(
            (t["Key"], t["Value"]) for t in Tags)))


class FakeClients:
    def __init__(self, *, usecases, training, buckets, writes_allowed=False, outputs=None,
                 lambda_env=None, abac_operations=True):
        self.region = REGION
        self.log = []
        self.created = []
        self.tables = {"uc-table": FakeTable(usecases), "tj-table": FakeTable(training)}
        self.buckets = buckets
        self.writes_allowed = writes_allowed
        self.outputs = outputs
        self.lambda_env = lambda_env
        self.abac_operations = abac_operations
        self._s3, self._s3control = {}, {}

    def s3(self, region):
        if region not in self._s3:
            self.created.append(("s3", region))
            client = FakeS3(region, self.buckets, self.log, self.writes_allowed)
            if not self.abac_operations:
                client = SimpleNamespace(get_bucket_location=client.get_bucket_location)
            self._s3[region] = client
        return self._s3[region]

    def s3control(self, region):
        if region not in self._s3control:
            self.created.append(("s3control", region))
            self._s3control[region] = FakeS3Control(region, self.log, self.writes_allowed)
        return self._s3control[region]

    def sts(self):
        return SimpleNamespace(get_caller_identity=lambda: {"Account": PORTAL})

    def cloudformation(self):
        def describe_stacks(StackName):
            self.log.append(("describe_stacks", StackName))
            if self.outputs is None:
                raise client_error("AccessDenied")
            return {"Stacks": [{"Outputs": [{"OutputKey": k, "OutputValue": v}
                                            for k, v in self.outputs.items()]}]}
        return SimpleNamespace(describe_stacks=describe_stacks)

    def lambda_(self):
        def get_function_configuration(FunctionName):
            self.log.append(("get_function_configuration", FunctionName))
            return {"Environment": {"Variables": self.lambda_env or {}}}
        return SimpleNamespace(get_function_configuration=get_function_configuration)

    def table(self, name):
        return self.tables[name]


def single(usecase_id, **fields):
    record = {"usecase_id": usecase_id, "account_id": PORTAL,
              "cross_account_role_arn": f"arn:aws:iam::{PORTAL}:root"}
    record.update(fields)
    return record


def multi(usecase_id, **fields):
    record = {"usecase_id": usecase_id, "account_id": USECASE,
              "cross_account_role_arn": f"arn:aws:iam::{USECASE}:role/DDAPortalAccessRole"}
    record.update(fields)
    return record


def bucket_state(location=None, tags=None, abac="Disabled"):
    return {"location": location, "tags": tags, "abac": abac}


MANAGED = {"dda-portal:managed": "true"}


def run(clients, argv, is_cross):
    lines = []
    status = audit.main(["--usecases-table", "uc-table", "--training-jobs-table", "tj-table"] + argv,
                        clients_factory=lambda profile, region: clients, is_cross_account=is_cross,
                        out=lines.append)
    return status, lines


# ------------------------------------------------------- URI extraction

@pytest.mark.parametrize("uri, expected", [
    ("s3://model-bucket/k.tar.gz", "model-bucket"),
    ("s3://model-bucket/models/a/b/model.tar.gz", "model-bucket"),
    ("s3://model-bucket", "model-bucket"),
    ("s3://Legacy_Bucket.1/k", "Legacy_Bucket.1"),
    ("https://model-bucket.s3.amazonaws.com/k", None),
    ("S3://model-bucket/k", None),
    ("s3://", None),
    ("s3:///k", None),
    ("s3://ab/k", None),
    ("s3://bucket@other/k", None),
    ("s3://bucket name/k", None),
    ("", None),
    (None, None),
    (42, None),
])
def test_bucket_from_uri(uri, expected):
    assert audit.bucket_from_uri(uri) == expected


# ------------------------------------------------------------ classifier

def test_classify_lifecycle(is_cross):
    usecases = [
        single("uc-a", s3_bucket="a-primary", data_s3_bucket="a-data",
               additional_s3_buckets=["a-extra", "shared"], inference_uploader_s3_bucket="a-results"),
        single("uc-b", s3_bucket="b-primary", data_account_id=DATA, data_s3_bucket="b-data",
               data_account_role_arn=f"arn:aws:iam::{DATA}:role/DDAPortalDataAccessRole"),
        single("uc-c", s3_bucket="c-primary", data_account_id=PORTAL, data_s3_bucket="c-data"),
        multi("uc-m", s3_bucket="m-primary", additional_s3_buckets={"shared"}),
        single("uc-own", s3_bucket=RESULTS, additional_s3_buckets=[ARTIFACTS]),
        {"usecase_id": "uc-legacy", "account_id": PORTAL, "s3_bucket": "legacy-primary"},
        single("uc-empty", s3_bucket="", data_s3_bucket=None, additional_s3_buckets=[]),
    ]
    own = audit.portal_own_buckets(PORTAL, REGION).values()
    result = audit.classify_lifecycle(usecases, own, is_cross)
    kinds = {bucket: entry["kind"] for bucket, entry in result.items()}
    assert kinds == {
        "a-primary": "portal-account", "a-data": "portal-account", "a-extra": "portal-account",
        "a-results": "portal-account", "shared": "portal-account",
        "b-primary": "portal-account", "b-data": "cross-account",
        "c-primary": "portal-account", "c-data": "portal-account",
        "m-primary": "cross-account",
        RESULTS: "listed-portal-own", ARTIFACTS: "listed-portal-own",
        "legacy-primary": "portal-account",
    }
    assert result["shared"]["usecases"] == {"uc-a", "uc-m"}
    assert result["a-results"]["fields"] == {"inference_uploader_s3_bucket"}


def test_portal_own_buckets_by_name():
    assert audit.portal_own_buckets(PORTAL, "eu-west-1") == {
        "artifacts": f"dda-portal-artifacts-{PORTAL}-eu-west-1",
        "component": f"dda-component-eu-west-1-{PORTAL}",
        "inference-results": f"dda-inference-results-{PORTAL}"}
    assert audit.portal_own_buckets(PORTAL, REGION, "custom-artifacts")["artifacts"] == "custom-artifacts"


def test_probe_bucket_compliance():
    clients = FakeClients(usecases=[], training=[], buckets={
        "ok": bucket_state(tags=MANAGED, abac="Enabled"),
        "abac-off": bucket_state(tags=MANAGED, abac="Disabled"),
        "abac-empty": bucket_state(tags=MANAGED, abac=None),
        "untagged": bucket_state(tags=None, abac="Enabled"),
        "created-by-only": bucket_state(tags={"dda-portal:created-by": "portal"}, abac="Enabled"),
        "denied": bucket_state(tags=client_error("AccessDenied"), abac="Enabled"),
        "eu": bucket_state(location="EU", tags=MANAGED, abac="Enabled"),
        "west": bucket_state(location="us-west-2", tags=MANAGED, abac="Enabled"),
    })
    results = {name: audit.probe_bucket(clients, name) for name in clients.buckets}
    assert {n: r["compliant"] for n, r in results.items()} == {
        "ok": True, "abac-off": False, "abac-empty": False, "untagged": False,
        "created-by-only": False, "denied": False, "eu": True, "west": True}
    assert results["denied"]["error"] == "AccessDenied"
    assert results["untagged"]["error"] is None and results["untagged"]["tagged"] is False
    assert (results["ok"]["region"], results["eu"]["region"], results["west"]["region"]) == (
        "us-east-1", "eu-west-1", "us-west-2")
    # tagging and ABAC are read with the bucket's regional client
    assert ("get_bucket_abac", "us-west-2", "west") in clients.log


def test_results_override_counts():
    usecases = [single("a", inference_uploader_s3_bucket=RESULTS),
                single("b", inference_uploader_s3_bucket="custom-results"),
                single("c", inference_uploader_s3_bucket=""), single("d")]
    assert audit.results_override_counts(usecases) == (2, 1)


# ------------------------------------------------- legacy artifact buckets

def test_legacy_artifact_classification(is_cross):
    usecases = [single("uc-1", s3_bucket="uc1-bucket"), single("uc-2", s3_bucket="uc2-bucket"),
                multi("uc-m", s3_bucket="m-bucket")]
    by_id = {r["usecase_id"]: r for r in usecases}
    lifecycle = {"uc1-bucket", "uc2-bucket", "m-bucket"}
    own = audit.portal_own_buckets(PORTAL, REGION).values()
    training = [
        {"training_id": "t-in-set", "usecase_id": "uc-1", "artifact_s3": "s3://uc1-bucket/m.tar.gz"},
        {"training_id": "t-other-set", "usecase_id": "uc-1", "artifact_s3": "s3://uc2-bucket/m.tar.gz"},
        {"training_id": "t-own", "usecase_id": "uc-1", "artifact_s3": f"s3://{ARTIFACTS}/m.tar.gz"},
        {"training_id": "t-legacy-1", "usecase_id": "uc-1", "source": "imported",
         "model_source": {"s3_model_artifact": "s3://import-bucket/model.tar.gz"}},
        {"training_id": "t-legacy-2", "usecase_id": "uc-2", "source": "imported",
         "artifact_s3": "s3://import-bucket/other.tar.gz"},
        {"training_id": "t-multi", "usecase_id": "uc-m", "source": "imported",
         "artifact_s3": "s3://cross-import/model.tar.gz"},
        {"training_id": "t-orphan", "usecase_id": "uc-gone", "artifact_s3": "s3://import-bucket/x"},
        {"training_id": "t-bad", "usecase_id": "uc-1", "artifact_s3": "not-a-uri"},
        {"training_id": "t-none", "usecase_id": "uc-1"},
    ]
    result = audit.classify_artifacts(training, by_id, lifecycle, own, is_cross)
    assert result["legacy"] == {"import-bucket": {"records": {"t-legacy-1", "t-legacy-2"},
                                                  "usecases": {"uc-1", "uc-2"}}}
    assert result["not_probed"] == {"cross-import": {"records": {"t-multi"}, "usecases": {"uc-m"}}}
    assert result["orphans"] == ["t-orphan"]
    assert result["unparseable"] == [("t-bad", "artifact_s3")]


# --------------------------------------------------------- whole read-only run

def _lab(writes_allowed=False, **extra):
    usecases = [
        single("uc-1", s3_bucket="uc1-bucket", additional_s3_buckets=["uc1-extra"],
               inference_uploader_s3_bucket="uc1-results"),
        multi("uc-m", s3_bucket="m-bucket"),
        single("uc-own", s3_bucket=RESULTS),
    ]
    training = [{"training_id": "t-1", "usecase_id": "uc-1", "source": "imported",
                 "artifact_s3": "s3://import-bucket/model.tar.gz"}]
    buckets = {
        "uc1-bucket": bucket_state(tags=MANAGED, abac="Enabled"),
        "uc1-extra": bucket_state(location="eu-west-1", tags=MANAGED, abac="Disabled"),
        "uc1-results": bucket_state(location="us-west-2", tags=None, abac="Disabled"),
    }
    return FakeClients(usecases=usecases, training=training, buckets=buckets,
                       writes_allowed=writes_allowed, **extra)


def test_read_only_run_reports_and_makes_no_write(is_cross):
    clients = _lab()                        # put_bucket_abac / tag_resource raise if called
    status, lines = run(clients, [], is_cross)
    assert status == 1
    text = "\n".join(lines)
    assert "uc1-bucket | portal-account | uc-1 | us-east-1 | True | True | True" in text
    assert "uc1-extra | portal-account | uc-1 | eu-west-1 | True | False | False" in text
    assert "m-bucket | cross-account | uc-m | - | - | - | not probed (cross-account role)" in text
    assert f"{RESULTS} | listed-portal-own" in text
    assert "Portal_Account_Buckets: 3; compliant: 1; non-compliant: 2" in text
    assert "use cases with a results-bucket override: 1 (1 outside dda-inference-results-*)" in text
    assert "legacy-artifact-bucket import-bucket — 1 record(s), use cases uc-1" in text
    assert "legacy-artifact buckets: 1" in text
    assert "ModelConverter TRUSTED_MODEL_BUCKETS / TRUSTED_MODEL_ACCOUNTS: not checked" in text
    ops = {entry[0] for entry in clients.log}
    assert ops <= {"get_bucket_location", "get_bucket_tagging", "get_bucket_abac"}
    assert not [c for c in clients.created if c[0] == "s3control"]


def test_compliant_run_exits_zero(is_cross):
    clients = FakeClients(usecases=[single("uc-1", s3_bucket="uc1-bucket")], training=[],
                          buckets={"uc1-bucket": bucket_state(tags=MANAGED, abac="Enabled")})
    status, lines = run(clients, [], is_cross)
    assert status == 0
    assert "legacy-artifact buckets: 0" in lines


def test_output_masks_account_ids(is_cross):
    clients = _lab()
    lines = []
    status = audit.main(["--usecases-table", "uc-table", "--training-jobs-table", "tj-table"],
                        clients_factory=lambda p, r: clients, is_cross_account=is_cross,
                        out=lambda line: lines.append(audit.mask(line)))
    text = "\n".join(lines)
    assert status == 1
    assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", text)
    assert "********1111" in text


def test_trusted_model_variables_are_printed_when_asked(is_cross):
    clients = _lab(lambda_env={"TRUSTED_MODEL_BUCKETS": "", "TRUSTED_MODEL_ACCOUNTS": ""})
    _, lines = run(clients, ["--model-converter-function", "converter-fn"], is_cross)
    assert "ModelConverter TRUSTED_MODEL_BUCKETS='' TRUSTED_MODEL_ACCOUNTS=''" in lines
    assert ("get_function_configuration", "converter-fn") in clients.log


def test_table_defaults_from_the_storage_stack_and_fallbacks(is_cross):
    clients = _lab(outputs={"UseCasesTableName": "uc-table", "TrainingJobsTableName": "tj-table"})
    lines = []
    audit.main([], clients_factory=lambda p, r: clients, is_cross_account=is_cross, out=lines.append)
    assert ("describe_stacks", "EdgeCVPortalStorageStack") in clients.log
    assert "tables uc-table, tj-table" in lines[0]

    unreadable = _lab()                         # describe_stacks denied -> documented fallbacks
    unreadable.tables["dda-portal-usecases"] = unreadable.tables.pop("uc-table")
    unreadable.tables["dda-portal-training-jobs"] = unreadable.tables.pop("tj-table")
    lines = []
    audit.main([], clients_factory=lambda p, r: unreadable, is_cross_account=is_cross, out=lines.append)
    assert "tables dda-portal-usecases, dda-portal-training-jobs" in lines[0]


def test_help_names_the_table_defaults():
    text = audit.build_parser().format_help()
    for name in ("EdgeCVPortalStorageStack", "UseCasesTableName", "TrainingJobsTableName",
                 "dda-portal-usecases", "dda-portal-training-jobs"):
        assert name in text


def test_scan_projection_aliases_every_name_and_pages():
    table = FakeTable([{"training_id": str(i)} for i in range(5)], page_size=2)
    items = audit.scan_all(table, audit.TRAINING_PROJECTION)
    assert [item["training_id"] for item in items] == ["0", "1", "2", "3", "4"]
    assert len(table.calls) == 3
    first = table.calls[0]
    names = first["ExpressionAttributeNames"]
    assert set(names.values()) == {"training_id", "usecase_id", "source", "artifact_s3",
                                   "model_source", "s3_model_artifact"}
    assert "source" not in first["ProjectionExpression"].replace("#", " ").split()
    assert any("." in path for path in first["ProjectionExpression"].split(", "))


def test_local_sdk_without_abac_operations_is_refused(is_cross):
    clients = _lab(abac_operations=False)
    status, lines = run(clients, [], is_cross)
    assert status == 2
    assert "GetBucketAbac" in lines[0]
    assert clients.log == []


# -------------------------------------------------------------------- --apply

def test_apply_routes_each_call_to_the_bucket_region(is_cross):
    clients = _lab(writes_allowed=True)
    status, lines = run(clients, ["--apply", "--buckets", "uc1-extra", "uc1-results"], is_cross)
    assert status == 0
    writes = [entry for entry in clients.log if entry[0] in ("tag_resource", "put_bucket_abac")]
    assert writes == [
        ("put_bucket_abac", "eu-west-1", "uc1-extra"),                       # tagged already
        ("tag_resource", "us-west-2", "arn:aws:s3:::uc1-results", PORTAL,
         (("dda-portal:managed", "true"),)),
        ("put_bucket_abac", "us-west-2", "uc1-results"),
    ]
    assert ("s3control", "us-west-2") in clients.created
    assert ("s3control", "eu-west-1") not in clients.created


def test_apply_enable_first_reverses_the_order(is_cross):
    clients = _lab(writes_allowed=True)
    run(clients, ["--apply", "--buckets", "uc1-results", "--enable-first"], is_cross)
    writes = [entry[0] for entry in clients.log if entry[0] in ("tag_resource", "put_bucket_abac")]
    assert writes == ["put_bucket_abac", "tag_resource"]


def test_apply_skips_a_compliant_bucket(is_cross):
    clients = _lab(writes_allowed=True)
    status, lines = run(clients, ["--apply", "--buckets", "uc1-bucket"], is_cross)
    assert status == 0
    assert "uc1-bucket: already compliant; nothing applied" in lines
    assert not [e for e in clients.log if e[0] in ("tag_resource", "put_bucket_abac")]


@pytest.mark.parametrize("name, reason", [
    ("not-registered", "not in the scanned Lifecycle_Set"),
    ("m-bucket", "not a Portal_Account_Bucket (cross-account)"),
    (RESULTS, "not a Portal_Account_Bucket (listed-portal-own)"),
    ("import-bucket", "a legacy-artifact bucket"),
])
def test_apply_refuses_and_applies_nothing(is_cross, name, reason):
    clients = _lab()                          # any write would raise
    status, lines = run(clients, ["--apply", "--buckets", "uc1-extra", name], is_cross)
    assert status == 2
    assert any(line.startswith(f"refused: {name}: {reason}") for line in lines)
    assert lines[-1] == "nothing applied"
    assert not [e for e in clients.log if e[0] in ("tag_resource", "put_bucket_abac")]


def test_apply_and_buckets_go_together(is_cross):
    assert run(_lab(), ["--apply"], is_cross)[0] == 2
    assert run(_lab(), ["--buckets", "uc1-extra"], is_cross)[0] == 2


# ------------------------------------------------------- the path loader

def test_shared_layer_is_loaded_by_path_under_a_private_name():
    before = sys.modules.get("shared_utils")
    module = audit.load_shared_layer()
    assert module.__name__ == "portal_audit_shared_utils"
    assert sys.modules.get("shared_utils") is before
    assert os.path.dirname(module.__file__) == audit.LAYER_DIR
    assert not module.is_cross_account_setup(single("u"))
    assert module.is_cross_account_setup(multi("u"))
    assert not module.is_cross_account_setup({"usecase_id": "u", "account_id": PORTAL})
