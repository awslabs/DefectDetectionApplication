"""
Harness for the source-derived gate suites (portal-authorization-and-iam-
scoping, design I2; task 2.3): the real compilation, packaging and
greengrass_publish handlers and the five internal-caller fabricators
(training_events, model_import, compilation_events,
packaging._trigger_component_creation, detector_conversion.build_finalize_event),
loaded under the moto stack.

Everything after each handler's role check is faked (training-job table,
use-case lookup, SageMaker / S3 / Greengrass clients, the artifact
repackaging and packaging steps, the audit log), so a request that passes
the gate reaches its audit row and answers 200 without real AWS work; the
role check itself is the real `check_user_access` over the moto user-roles
table, so the decision is the one the portal makes in either enforcement
state.
"""
import importlib.util
import json
import os
import sys
import uuid
from types import SimpleNamespace
from typing import Dict, List, Optional

_FUNCTIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "functions")
MOTO_ACCOUNT = "123456789012"
USECASE_BUCKET = "gate-usecase-bucket"
COMPILATION_FUNCTION = "test-compilation-fn"
PACKAGING_FUNCTION = "test-packaging-fn"
PUBLISH_FUNCTION = "test-greengrass-publish-fn"
ROLES = ("Viewer", "Operator", "DataScientist", "UseCaseAdmin", "PortalAdmin")


def load(filename: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(_FUNCTIONS, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


class FakeTable:
    """One table for every name: items by training_id, writes recorded."""

    def __init__(self):
        self.items: Dict[str, Dict] = {}
        self.writes: List[tuple] = []

    def get_item(self, Key):
        item = self.items.get(Key.get("training_id"))
        return {"Item": json.loads(json.dumps(item))} if item is not None else {}

    def put_item(self, Item):
        self.writes.append(("put_item", Item))
        if "training_id" in Item:
            self.items[Item["training_id"]] = Item
        return {}

    def update_item(self, **kwargs):
        self.writes.append(("update_item", kwargs))
        return {}

    def scan(self, **kwargs):
        items = list(self.items.values())
        name = (kwargs.get("ExpressionAttributeValues") or {}).get(":name")
        if name is not None:
            items = [item for item in items if item.get("training_job_name") == name]
        return {"Items": json.loads(json.dumps(items))}


class FakeDynamo:
    def __init__(self):
        self.table = FakeTable()

    def Table(self, name):            # the boto3 resource API's method name
        return self.table


class FakeSageMaker:
    def __init__(self):
        self.calls: List[Dict] = []

    def create_compilation_job(self, **kwargs):
        self.calls.append(kwargs)
        return {"CompilationJobArn": f"arn:aws:sagemaker:us-east-1:{MOTO_ACCOUNT}:compilation-job/"
                                     f"{kwargs['CompilationJobName']}"}


class FakeGreengrass:
    def __init__(self):
        self.calls: List[Dict] = []

    def create_component_version(self, **kwargs):
        self.calls.append(kwargs)
        return {"arn": f"arn:aws:greengrass:us-east-1:{MOTO_ACCOUNT}:components:"
                       f"model-gate:versions:1.0.0"}

    def describe_component(self, arn):
        return {"status": {"componentState": "DEPLOYABLE"}}


class FakeLambda:
    def __init__(self):
        self.invocations: List[Dict] = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"StatusCode": 202}

    def payloads(self) -> List[Dict]:
        return [json.loads(invocation["Payload"]) for invocation in self.invocations]


class AuditRecorder:
    def __init__(self):
        self.rows: List[Dict] = []

    def __call__(self, user_id, action, resource_type, resource_id, result,
                 details=None, identity=None, event=None):
        self.rows.append({"user_id": user_id, "action": action, "resource_id": resource_id,
                          "result": result, "details": details or {}})

    def of(self, action: str) -> List[Dict]:
        return [row for row in self.rows if row["action"] == action]


class Gates(SimpleNamespace):
    def reset(self) -> None:
        self.audit.rows.clear()
        self.sagemaker.calls.clear()
        self.greengrass.calls.clear()
        self.lam.invocations.clear()
        self.trigger_calls.clear()
        self.dynamo.table.writes.clear()

    # ----------------------------------------------------------- records
    def seed_jobs(self) -> Dict[str, str]:
        """A trained job (compile), an imported ONNX job (package) and a
        packaged job (publish) on the harness use case."""
        ids = {kind: f"train-{kind}-{uuid.uuid4().hex[:8]}" for kind in ("compile", "package", "publish")}
        base = {"usecase_id": self.usecase_id, "status": "Completed", "model_name": "gate-model",
                "model_type": "classification",
                "artifact_s3": f"s3://{USECASE_BUCKET}/models/model.tar.gz"}
        self.dynamo.table.items[ids["compile"]] = {**base, "training_id": ids["compile"],
                                                   "source": "trained"}
        self.dynamo.table.items[ids["package"]] = {
            **base, "training_id": ids["package"], "source": "imported",
            "metadata": {"framework": "ONNX", "model_file": "model.onnx"}}
        self.dynamo.table.items[ids["publish"]] = {
            **base, "training_id": ids["publish"], "source": "trained",
            "packaged_components": [{"target": "x86_64-cpu", "status": "packaged",
                                     "component_package_s3": f"s3://{USECASE_BUCKET}/c/gate.zip"}]}
        return ids

    # ------------------------------------------------------------- users
    def user(self, role: Optional[str]) -> Dict[str, str]:
        """A caller holding `role` on the harness use case (with the global
        provisioning row enforcement requires), or no role at all."""
        sub = f"gate-user-{uuid.uuid4().hex[:10]}"
        if role is not None:
            for scope, scope_role in (("global", "Viewer"), (self.usecase_id, role)):
                self.stack.tables.user_roles.put_item(Item={
                    "user_id": sub, "usecase_id": scope, "role": scope_role, "status": "enabled"})
        return {"user_id": sub, "email": f"{sub}@example.com", "username": sub,
                "role": role or "Viewer"}

    # ------------------------------------------------------------ events
    @staticmethod
    def gateway_event(path: str, training_id: str, caller: Dict[str, str], body: Dict,
                      **extra) -> Dict:
        event = {
            "httpMethod": "POST", "path": path, "resource": path,
            "pathParameters": {"id": training_id}, "body": json.dumps(body),
            "requestContext": {
                "requestId": f"req-{uuid.uuid4().hex[:8]}", "apiId": "portalapi",
                "authorizer": {"claims": {
                    "sub": caller["user_id"], "email": caller["email"],
                    "cognito:username": caller["username"], "custom:role": caller["role"]}},
            },
        }
        event.update(extra)
        return event

    def compile(self, event: Dict):
        return self._answer(self.compilation.start_compilation_job(event, None))

    def package(self, event: Dict):
        return self._answer(self.packaging.package_components(event, None))

    def publish(self, event: Dict):
        return self._answer(self.publish_module.publish_component(event, None))

    @staticmethod
    def _answer(response):
        return response["statusCode"], json.loads(response["body"])

    # ------------------------------------------------------- fabricators
    def training_events_event(self, training_id: str) -> Dict:
        """The compile event training_events sends for a completed job."""
        job = self.dynamo.table.items[training_id]
        job.update({"training_job_name": f"job-{training_id}", "auto_compile": True,
                    "compilation_targets": ["x86_64"]})
        self.lam.invocations.clear()
        response = self.training_events.handle_training_state_change({
            "source": "aws.sagemaker", "detail-type": "SageMaker Training Job State Change",
            "detail": {"TrainingJobName": job["training_job_name"], "TrainingJobStatus": "Completed",
                       "ModelArtifacts": {"S3ModelArtifacts": job["artifact_s3"]}}}, None)
        assert response["statusCode"] == 200, response
        [payload] = self.lam.payloads()
        return payload

    def model_import_event(self, importer: Dict[str, str]) -> Dict:
        """The compile event model_import sends after an auto-compile import."""
        self.lam.invocations.clear()
        body = {"usecase_id": self.usecase_id, "model_name": "imported-gate",
                "model_version": "1.0.0",
                "model_s3_uri": f"s3://{USECASE_BUCKET}/imports/model.tar.gz",
                "auto_compile": True, "compilation_targets": ["x86_64-cpu"]}
        response = self.model_import.import_model(
            self.gateway_event("/api/v1/models/import", None, importer, body), None)
        assert response["statusCode"] == 201, response["body"]
        [payload] = self.lam.payloads()
        return payload

    def compilation_events_event(self, training_id: str) -> Dict:
        """The package event compilation_events sends once every job completed."""
        job = self.dynamo.table.items[training_id]
        job["compilation_jobs"] = [{"target": "x86_64-cpu", "status": "INPROGRESS",
                                    "compilation_job_name": f"cmp-{training_id}"}]
        self.lam.invocations.clear()
        response = self.compilation_events.handle_compilation_state_change({
            "source": "aws.sagemaker", "detail-type": "SageMaker Compilation Job State Change",
            "detail": {"CompilationJobName": f"cmp-{training_id}",
                       "CompilationJobStatus": "Completed",
                       "ModelArtifacts": {"S3ModelArtifacts": f"s3://{USECASE_BUCKET}/compiled/"}}},
            None)
        assert response["statusCode"] == 200, response
        [payload] = self.lam.payloads()
        return payload

    def publish_chain_event(self, training_id: str) -> Dict:
        """The publish event packaging._trigger_component_creation sends."""
        self.lam.invocations.clear()
        self.real_trigger(training_id, {"model_name": "gate-model"})
        [payload] = self.lam.payloads()
        return payload

    def finalize_event(self, training_id: str) -> Dict:
        """The package event detector_conversion.build_finalize_event builds."""
        return json.loads(json.dumps(self.detector_conversion.build_finalize_event(training_id)))


def install(stack, mp) -> Gates:
    """Load the handlers and fabricators and install the fakes (`mp` is a
    pytest MonkeyPatch the caller undoes)."""
    import boto3 as real_boto3
    import detector_conversion

    compilation = load("compilation.py", "portal_compilation_gates")
    packaging = load("packaging.py", "portal_packaging_gates")
    publish_module = load("greengrass_publish.py", "portal_greengrass_publish_gates")
    training_events = load("training_events.py", "portal_training_events_gates")
    compilation_events = load("compilation_events.py", "portal_compilation_events_gates")
    model_import = load("model_import.py", "portal_model_import_gates")

    usecase_id = f"uc-gates-{uuid.uuid4().hex[:8]}"
    usecase = {"usecase_id": usecase_id, "name": "Gate use case", "account_id": MOTO_ACCOUNT,
               "s3_bucket": USECASE_BUCKET, "cross_account_role_arn": f"arn:aws:iam::{MOTO_ACCOUNT}:root"}
    dynamo, audit = FakeDynamo(), AuditRecorder()
    sagemaker, greengrass, lam = FakeSageMaker(), FakeGreengrass(), FakeLambda()
    trigger_calls: List[str] = []

    def usecase_client(service_name, usecase_record, session_name=None, region=None):
        return {"sagemaker": sagemaker, "greengrassv2": greengrass}.get(service_name, SimpleNamespace())

    lambda_boto3 = SimpleNamespace(client=lambda name, **kwargs: lam, resource=real_boto3.resource)
    for module in (compilation, packaging, publish_module):
        mp.setattr(module, "dynamodb", dynamo)
        mp.setattr(module, "get_usecase", lambda _usecase_id: dict(usecase))
        mp.setattr(module, "get_usecase_client", usecase_client)
        mp.setattr(module, "log_audit_event", audit)
    mp.setattr(compilation, "extract_and_repackage_model", lambda uri, s3: (
        f"s3://{USECASE_BUCKET}/repackaged/model.tar.gz", json.dumps({"input0": [1, 3, 224, 224]})))
    mp.setattr(packaging, "package_onnx_component", lambda uri, s3, record: (
        f"s3://{USECASE_BUCKET}/components/gate.zip"))
    real_trigger = packaging._trigger_component_creation
    mp.setattr(packaging, "_trigger_component_creation",
               lambda training_id, job: trigger_calls.append(training_id))
    mp.setattr(packaging, "boto3", lambda_boto3)
    mp.setattr(publish_module.time, "sleep", lambda seconds: None)
    for module in (training_events, compilation_events):
        mp.setattr(module, "dynamodb", dynamo)
        mp.setattr(module, "boto3", lambda_boto3)
    mp.setattr(model_import, "dynamodb", dynamo)
    mp.setattr(model_import, "boto3", lambda_boto3)
    mp.setattr(model_import, "log_audit_event", audit)
    mp.setattr(model_import, "get_usecase_details", lambda _usecase_id: dict(usecase))
    mp.setattr(model_import, "assert_bucket_allowed", lambda *args, **kwargs: None)
    mp.setattr(model_import, "validate_model_artifact", lambda uri, record, purpose: {
        "valid": True, "metadata": {"model_type": "classification"}})
    mp.setenv("COMPILATION_FUNCTION_NAME", COMPILATION_FUNCTION)
    mp.setenv("PACKAGING_FUNCTION_NAME", PACKAGING_FUNCTION)
    mp.setenv("GREENGRASS_PUBLISH_FUNCTION_NAME", PUBLISH_FUNCTION)

    return Gates(stack=stack, usecase_id=usecase_id, dynamo=dynamo, audit=audit,
                 sagemaker=sagemaker, greengrass=greengrass, lam=lam, trigger_calls=trigger_calls,
                 real_trigger=real_trigger, compilation=compilation, packaging=packaging,
                 publish_module=publish_module, training_events=training_events,
                 compilation_events=compilation_events, model_import=model_import,
                 detector_conversion=detector_conversion)
