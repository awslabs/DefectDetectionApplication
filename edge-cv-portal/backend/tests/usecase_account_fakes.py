"""
Recording fakes for the use-case authorization suites
(portal-authorization-and-iam-scoping, tasks 1.3 and 3.2).

`install(uc, shared, mp)` puts recording STS / S3 / EventBridge / Lambda
clients on the usecases module only (the sibling suite's pattern: the
handler's `boto3.client` is replaced, the shared layer keeps moto), a
recording Attach_Step (the attach validator plus the ABAC registration)
that admits every bucket, a recording
`log_audit_event` on both modules, and a write recorder around the
handler's DynamoDB tables, so a test can assert which calls and writes
happened and in which order. The use-case table is a private moto table
(USECASES_TABLE below) that the suites empty between cases, because the
grant remover scans it for other records of an account.

Fixture accounts (the spec's set): 111111111111 portal, 222222222222 trusted
use-case account, 333333333333 data account, 444444444444 untrusted, and
moto's 123456789012.
"""
import json
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

PORTAL = "111111111111"
TRUSTED = "222222222222"
DATA = "333333333333"
UNTRUSTED = "444444444444"
MOTO = "123456789012"
REGION = "us-east-1"
COMPONENT_BUCKET = f"dda-component-{REGION}-{PORTAL}"
SHARED_COMPONENTS_FUNCTION = "test-shared-components-fn"
USECASES_TABLE = "test-usecases-account-authz"
MANAGED = "dda-portal:managed"


def access_role(account: str) -> str:
    return f"arn:aws:iam::{account}:role/DDAPortalAccessRole"


def data_role(account: str) -> str:
    return f"arn:aws:iam::{account}:role/DDAPortalDataAccessRole"


def sagemaker_role(account: str) -> str:
    return f"arn:aws:iam::{account}:role/DDASageMakerExecutionRole"


def root_arn(account: str) -> str:
    return f"arn:aws:iam::{account}:root"


def client_error(code: str, message: Optional[str] = None, status: int = 400,
                 operation: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message or code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class OperationLog:
    """(service, operation, detail) entries, appended when an operation has
    returned, so the order of the entries is the order the writes landed."""

    def __init__(self):
        self.entries: List[tuple] = []

    def add(self, service: str, operation: str, detail: Any = None) -> None:
        self.entries.append((service, operation, detail))

    def ops(self) -> List[tuple]:
        return [(service, operation) for service, operation, _ in self.entries]

    def index(self, service: str, operation: str) -> int:
        return self.ops().index((service, operation))

    def clear(self) -> None:
        self.entries.clear()


class FakeSTS:
    """sts: `assume_role` answers an AssumedRoleUser in the role's account
    (or in `assumed_account`), or raises `outcome`; every call is recorded."""

    def __init__(self, log: OperationLog):
        self.log = log
        self.calls: List[tuple] = []
        self.outcome: Optional[Exception] = None
        self.assumed_account: Optional[str] = None

    def assume_role(self, **kwargs):
        self.calls.append(("assume_role", kwargs))
        if self.outcome is not None:
            raise self.outcome
        account = self.assumed_account or kwargs["RoleArn"].split(":")[4]
        role_name = kwargs["RoleArn"].rsplit("/", 1)[-1]
        self.log.add("sts", "assume_role", kwargs["RoleArn"])
        return {
            "Credentials": {"AccessKeyId": "fake-key", "SecretAccessKey": "fake-secret",
                            "SessionToken": "fake-token"},
            "AssumedRoleUser": {
                "Arn": f"arn:aws:sts::{account}:assumed-role/{role_name}/{kwargs['RoleSessionName']}",
                "AssumedRoleId": f"AROAFAKE:{kwargs['RoleSessionName']}"},
        }

    def get_caller_identity(self):
        self.calls.append(("get_caller_identity", {}))
        return {"Account": PORTAL}

    def reset(self):
        self.calls.clear()
        self.outcome = None
        self.assumed_account = None


class FakeComponentS3:
    """s3 in the portal account: the component bucket policy, per bucket."""

    def __init__(self, log: OperationLog):
        self.log = log
        self.calls: List[tuple] = []
        self.policies: Dict[str, str] = {}
        self.put_error: Optional[Exception] = None

    def get_bucket_policy(self, Bucket):
        self.calls.append(("get_bucket_policy", Bucket))
        if Bucket not in self.policies:
            raise client_error("NoSuchBucketPolicy", status=404, operation="GetBucketPolicy")
        return {"Policy": self.policies[Bucket]}

    def put_bucket_policy(self, Bucket, Policy):
        self.calls.append(("put_bucket_policy", Bucket))
        if self.put_error is not None:
            raise self.put_error
        document = json.loads(Policy)
        assert document.get("Statement"), "S3 stores no policy without statements"
        self.policies[Bucket] = Policy
        self.log.add("s3", "put_bucket_policy", Bucket)
        return {}

    def statements(self, bucket: str = COMPONENT_BUCKET) -> List[Dict]:
        if bucket not in self.policies:
            return []
        return json.loads(self.policies[bucket])["Statement"]

    def sids(self, bucket: str = COMPONENT_BUCKET) -> List[str]:
        return sorted(statement.get("Sid") for statement in self.statements(bucket))

    def seed(self, statements: List[Dict], bucket: str = COMPONENT_BUCKET) -> None:
        self.policies[bucket] = json.dumps({"Version": "2012-10-17", "Statement": statements})

    def reset(self):
        self.calls.clear()
        self.policies.clear()
        self.put_error = None

    def __getattr__(self, name):          # any other S3 call is a test failure
        raise AssertionError(f"unexpected S3 call {name}")


class FakeEvents:
    """events: the default bus policy as {StatementId: principal}."""

    def __init__(self, log: OperationLog):
        self.log = log
        self.calls: List[tuple] = []
        self.statements: Dict[str, str] = {}
        self.remove_error: Optional[Exception] = None

    def describe_event_bus(self, Name):
        self.calls.append(("describe_event_bus", Name))
        response = {"Name": Name}
        if self.statements:
            response["Policy"] = json.dumps({"Version": "2012-10-17", "Statement": [
                {"Sid": sid, "Effect": "Allow", "Principal": {"AWS": principal},
                 "Action": "events:PutEvents"} for sid, principal in self.statements.items()]})
        return response

    def put_permission(self, **kwargs):
        self.calls.append(("put_permission", kwargs))
        self.statements[kwargs["StatementId"]] = kwargs["Principal"]
        self.log.add("events", "put_permission", kwargs["StatementId"])
        return {}

    def remove_permission(self, **kwargs):
        self.calls.append(("remove_permission", kwargs))
        if self.remove_error is not None:
            raise self.remove_error
        if kwargs["StatementId"] not in self.statements:
            raise client_error("ResourceNotFoundException", operation="RemovePermission")
        del self.statements[kwargs["StatementId"]]
        self.log.add("events", "remove_permission", kwargs["StatementId"])
        return {}

    def reset(self):
        self.calls.clear()
        self.statements.clear()
        self.remove_error = None

    def __getattr__(self, name):
        raise AssertionError(f"unexpected EventBridge call {name}")


class FakeLambda:
    """lambda: records every invoke (after it returned), or raises `error`."""

    def __init__(self, log: OperationLog):
        self.log = log
        self.invocations: List[Dict] = []
        self.error: Optional[Exception] = None

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        if self.error is not None:
            raise self.error
        self.log.add("lambda", "invoke", kwargs.get("FunctionName"))
        return {"StatusCode": 202}

    def reset(self):
        self.invocations.clear()
        self.error = None

    def __getattr__(self, name):
        raise AssertionError(f"unexpected Lambda call {name}")


class RecordingValidator:
    """Stand-in for the Attach_Step (`attach_bucket`, which runs the
    sibling's attach validator): records every keyword call, `source`
    included, and admits the bucket as tagged, ABAC-enabled and reachable."""

    def __init__(self, shared):
        self.shared = shared
        self.calls: List[Dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        eligibility = self.shared.EligibilityResult(
            eligible=True, reason="eligible", tags={MANAGED: "true"},
            portal_created=False, claimed_by=None)
        return self.shared.AttachOutcome(eligibility, True, True, True, None, "already_enabled")


class AuditRecorder:
    """Recording `log_audit_event` (no table write)."""

    def __init__(self):
        self.rows: List[Dict] = []

    def __call__(self, user_id, action, resource_type, resource_id, result,
                 details=None, identity=None, event=None):
        self.rows.append({"user_id": user_id, "action": action, "resource_type": resource_type,
                          "resource_id": resource_id, "result": result,
                          "details": details or {}, "event": event})

    def of(self, action: str, **match) -> List[Dict]:
        return [row for row in self.rows if row["action"] == action
                and all(row.get(key) == value for key, value in match.items())]


class RecordingTable:
    """A moto table whose writes are logged as ('dynamodb', op, table)."""

    def __init__(self, real, name: str, log: OperationLog):
        self._real = real
        self._name = name
        self._log = log

    def _write(self, operation: str):
        def call(**kwargs):
            response = getattr(self._real, operation)(**kwargs)
            self._log.add("dynamodb", operation, self._name)
            return response
        return call

    def __getattr__(self, name):
        if name in ("put_item", "update_item", "delete_item"):
            return self._write(name)
        return getattr(self._real, name)


class RecordingDynamo:
    def __init__(self, real, log: OperationLog):
        self._real = real
        self._log = log

    def Table(self, name):            # noqa: N802 - boto3 resource API
        return RecordingTable(self._real.Table(name), name, self._log)


def ensure_usecases_table() -> None:
    client = boto3.client("dynamodb", region_name=REGION)
    if USECASES_TABLE not in client.list_tables()["TableNames"]:
        client.create_table(
            TableName=USECASES_TABLE,
            KeySchema=[{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "usecase_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")


def usecases_table():
    return boto3.resource("dynamodb", region_name=REGION).Table(USECASES_TABLE)


def clear_usecases_table() -> None:
    table = usecases_table()
    for item in table.scan(ProjectionExpression="usecase_id")["Items"]:
        table.delete_item(Key={"usecase_id": item["usecase_id"]})


class Fakes(SimpleNamespace):
    def reset(self) -> None:
        """Back to the state `install` left: no calls, no policies, no rows."""
        self.log.clear()
        for fake in (self.sts, self.s3, self.events, self.lam):
            fake.reset()
        self.validator.calls.clear()
        self.audit.rows.clear()

    def sts_calls(self) -> List[tuple]:
        return list(self.sts.calls)

    def writes(self, table: Optional[str] = None) -> List[tuple]:
        return [(service, operation, detail) for service, operation, detail in self.log.entries
                if service == "dynamodb" and (table is None or detail == table)]


def install(uc, shared, mp, trusted: str = TRUSTED) -> Fakes:
    """Install the fakes and the environment the suites run under."""
    import boto3 as real_boto3

    log = OperationLog()
    fakes = Fakes(log=log, sts=FakeSTS(log), s3=FakeComponentS3(log), events=FakeEvents(log),
                  lam=FakeLambda(log), validator=RecordingValidator(shared), audit=AuditRecorder())

    def client(service, **kwargs):
        if service == "sts":
            return fakes.sts
        if service == "s3":
            return fakes.s3
        if service == "events":
            return fakes.events
        if service == "lambda":
            return fakes.lam
        return real_boto3.client(service, **kwargs)

    mp.setattr(uc, "boto3", SimpleNamespace(client=client, resource=real_boto3.resource))
    mp.setattr(uc, "sts", fakes.sts)
    mp.setattr(uc, "attach_bucket", fakes.validator)
    for module in (shared, uc):
        mp.setattr(module, "log_audit_event", fakes.audit)
    mp.setattr(uc, "dynamodb", RecordingDynamo(
        real_boto3.resource("dynamodb", region_name=REGION), log))
    ensure_usecases_table()
    mp.setattr(uc, "USECASES_TABLE", USECASES_TABLE)
    mp.delenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)
    mp.delenv("COMPONENT_BUCKET", raising=False)
    mp.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    mp.setenv("AWS_REGION", REGION)
    mp.setenv("TRUSTED_USECASE_ACCOUNT_IDS", trusted)
    mp.setenv("SHARED_COMPONENTS_FUNCTION_NAME", SHARED_COMPONENTS_FUNCTION)
    return fakes


# ------------------------------------------------------------------ actors

def user(stack, claim: str, scope: Optional[str] = None, registry_role: Optional[str] = None,
         status: Optional[str] = "enabled") -> Dict[str, str]:
    """A caller with the `custom:role` claim and, when `registry_role` is
    given, a user-roles row for `scope` (a Portal_Identity row)."""
    sub = f"user-{uuid.uuid4().hex[:12]}"
    if registry_role is not None:
        row = {"user_id": sub, "usecase_id": scope or "global", "role": registry_role,
               "username": sub, "email": f"{sub}@example.com",
               "assigned_by": "test", "assigned_at": 1}
        if status is not None:
            row["status"] = status
        stack.tables.user_roles.put_item(Item=row)
    return {"user_id": sub, "email": f"{sub}@example.com", "username": sub, "role": claim}


def portal_admin(stack) -> Dict[str, str]:
    return user(stack, "PortalAdmin", "global", "PortalAdmin")


def api_event(method: str, path: str, caller: Dict[str, str], usecase_id: Optional[str] = None,
              body: Optional[Dict] = None) -> Dict:
    """An API Gateway (Cognito-authorized) Lambda-proxy event."""
    return {
        "httpMethod": method, "path": path, "resource": path,
        "pathParameters": {"id": usecase_id} if usecase_id else None,
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "requestId": f"req-{uuid.uuid4().hex[:8]}", "apiId": "portalapi",
            "authorizer": {"claims": {
                "sub": caller["user_id"], "email": caller["email"],
                "cognito:username": caller["username"], "custom:role": caller["role"]}},
        },
    }


def call(uc, event: Dict):
    response = uc.handler(event, None)
    return response["statusCode"], json.loads(response["body"]), response["body"]


# ----------------------------------------------------------------- records

def put_single_account_record(**fields) -> str:
    """A Single_Account_Record as create_usecase writes one."""
    usecase_id = f"uc-{uuid.uuid4().hex[:12]}"
    item = {"usecase_id": usecase_id, "name": f"Single {usecase_id}", "account_id": PORTAL,
            "cross_account_role_arn": root_arn(PORTAL),
            "sagemaker_execution_role_arn": sagemaker_role(PORTAL),
            "external_id": "stored-external-id", "s3_bucket": "single-bucket",
            "region": REGION, "setup_type": "single-account"}
    item.update(fields)
    usecases_table().put_item(Item=item)
    return usecase_id


def put_multi_account_record(account: str = TRUSTED, **fields) -> str:
    """A multi-account record in `account`."""
    usecase_id = f"uc-{uuid.uuid4().hex[:12]}"
    item = {"usecase_id": usecase_id, "name": f"Multi {usecase_id}", "account_id": account,
            "cross_account_role_arn": access_role(account),
            "sagemaker_execution_role_arn": sagemaker_role(account),
            "external_id": "ext-stored", "s3_bucket": "multi-bucket",
            "region": REGION, "setup_type": "multi-account"}
    item.update(fields)
    usecases_table().put_item(Item=item)
    return usecase_id


def record(usecase_id: str) -> Optional[Dict]:
    return usecases_table().get_item(Key={"usecase_id": usecase_id}).get("Item")


def multi_account_body(account: str = TRUSTED, **fields) -> Dict:
    body = {"name": f"created-{uuid.uuid4().hex[:6]}", "s3_bucket": "created-bucket",
            "account_id": account, "cross_account_role_arn": access_role(account),
            "sagemaker_execution_role_arn": sagemaker_role(account),
            "external_id": "ext-supplied", "provision_shared_components": False}
    body.update(fields)
    return body


def single_account_body(**fields) -> Dict:
    body = {"name": f"created-{uuid.uuid4().hex[:6]}", "s3_bucket": "created-bucket",
            "provision_shared_components": False}
    body.update(fields)
    return body
