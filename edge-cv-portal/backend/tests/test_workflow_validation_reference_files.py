"""
Uploaded reference files in the validation Lambda
(functions/workflow_validation.py, inspection-format tuning fix, design
§9.13.6): the Lambda half of AC-42.

POST /workflows/{id}/validate lists the workflow's stored reference files
(ListObjectsV2 under workflows/{usecase}/{workflow}/references/, owner
asserted) only when the stored definition has an upload line, and passes
their names, without the .json upload records, to the validator, which then
reports each upload line naming a file the workflow does not store
(BEDROCK_REFERENCE_UPLOAD_MISSING). A listing error answers 500
REFERENCE_FILES_LOAD_FAILED and records no validation. moto S3 and
DynamoDB; the module's S3 client is wrapped by a recorder.

On the device's commit c299a6e1 the route never lists the files and never
reports the missing upload, and a failing listing cannot fail it. The test
that a definition without upload lines makes no S3 listing is a
preservation test and passes there too.
"""
import json
import sys

import pytest
from botocore.exceptions import ClientError

OWNER = "123456789012"
UPLOAD_MISSING = "BEDROCK_REFERENCE_UPLOAD_MISSING"
STORED = "a" * 64 + ".png"
MISSING = "b" * 64 + ".pdf"


class RecordingS3:
    """The module's real (moto-backed) client, recording (operation, kwargs)
    of every call; `fail_listing` makes ListObjectsV2 raise."""

    def __init__(self, real):
        self._real = real
        self.calls = []
        self.fail_listing = False

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name.startswith("_") or name in ("meta", "exceptions") or not callable(attr):
            return attr

        def call(*args, **kwargs):
            self.calls.append((name, kwargs))
            if name == "list_objects_v2" and self.fail_listing:
                raise ClientError({"Error": {"Code": "InternalError", "Message": "simulated"},
                                   "ResponseMetadata": {"HTTPStatusCode": 500}}, name)
            return attr(*args, **kwargs)

        return call


@pytest.fixture(scope="module")
def validation(aws_stack):
    """workflow_validation imported inside the moto mock."""
    sys.modules.pop("workflow_validation", None)
    import workflow_validation
    yield workflow_validation
    sys.modules.pop("workflow_validation", None)


def definition(reference):
    """folder_source -> bedrock_inference (inspection format) -> capture."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "check", "type": "bedrock_inference", "position": {"x": 200, "y": 0},
             "parameters": {"answer_format": "inspection",
                            "prompt": "Check the parts against the legend.",
                            "reference_payload_path": reference}},
            {"id": "cap", "type": "capture", "position": {"x": 400, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "check", "port": "in"}},
            {"id": "c2", "from": {"node": "check", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


class ValidationEnv:
    def __init__(self, env, module, monkeypatch):
        self.env = env
        self.module = module
        self.user = env.make_user(role="DataScientist")
        self.usecase_id = env.create_usecase()
        self.recorder = RecordingS3(module.s3)
        monkeypatch.setattr(module, "s3", self.recorder)

    def workflow(self, reference, stored=()):
        status, payload = self.env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id, "name": "validated",
            "definition": definition(reference)})
        assert status == 201, payload
        workflow_id = payload["workflow"]["workflow_id"]
        for key in stored:
            self.env.s3.put_object(
                Bucket=self.env.bucket,
                Key=f"workflows/{self.usecase_id}/{workflow_id}/references/{key}", Body=b"x")
        return workflow_id

    def validate(self, workflow_id):
        self.recorder.calls.clear()
        event = self.env.event("POST", "/workflows/{id}/validate", self.user,
                               workflow_id=workflow_id, body={})
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def listings(self):
        return [kwargs for name, kwargs in self.recorder.calls if name == "list_objects_v2"]

    def validation_status(self, workflow_id):
        item = self.env.stack.tables.versions.get_item(
            Key={"workflow_id": workflow_id, "version": 1})["Item"]
        return item["validation_status"]


@pytest.fixture
def venv(env, validation, monkeypatch):
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", OWNER)
    return ValidationEnv(env, validation, monkeypatch)


def upload_findings(body):
    return [f for f in body["findings"] if f["code"] == UPLOAD_MISSING]


def test_a_stored_upload_line_lists_the_files_and_gives_no_finding(venv):
    workflow_id = venv.workflow(f"upload://{STORED}", stored=(STORED, f"{STORED}.json"))
    status, body = venv.validate(workflow_id)
    assert status == 200, body
    assert venv.listings() == [{
        "Bucket": venv.env.bucket,
        "Prefix": f"workflows/{venv.usecase_id}/{workflow_id}/references/",
        "ExpectedBucketOwner": OWNER}]
    assert upload_findings(body) == []


def test_a_line_naming_a_file_the_workflow_does_not_store_is_reported(venv):
    workflow_id = venv.workflow(f"upload://{STORED}\nupload://{MISSING}",
                                stored=(STORED, f"{STORED}.json", f"{MISSING}.json"))
    status, body = venv.validate(workflow_id)
    assert status == 200, body
    assert len(venv.listings()) == 1
    (finding,) = upload_findings(body)
    assert finding["severity"] == "error"
    assert finding["nodeId"] == "check"
    assert "line 2 of 'reference_payload_path'" in finding["message"]
    assert "upload://bbbbbbbbbbbb" in finding["message"]
    assert body["passed"] is False


def test_a_failed_listing_answers_500_and_records_no_validation(venv):
    workflow_id = venv.workflow(f"upload://{STORED}", stored=(STORED,))
    venv.recorder.fail_listing = True
    status, body = venv.validate(workflow_id)
    assert status == 500, body
    assert body["error"]["code"] == "REFERENCE_FILES_LOAD_FAILED"
    assert venv.validation_status(workflow_id) == {"status": "none"}
    assert [name for name, _ in venv.recorder.calls if name == "put_object"] == []


def test_a_definition_without_upload_lines_lists_nothing(venv):
    """Preservation: a definition without upload lines makes no S3 listing and
    validates as before (c299a6e1 and the fix)."""
    workflow_id = venv.workflow("file:///aws_dda/images/source.jpg\nrefs.0.image")
    status, body = venv.validate(workflow_id)
    assert status == 200, body
    assert venv.listings() == []
    assert upload_findings(body) == []
    assert venv.validation_status(workflow_id)["status"] in ("passed", "failed")
