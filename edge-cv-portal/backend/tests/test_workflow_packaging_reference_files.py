"""
Uploaded reference files in packaging (functions/workflow_packaging.py,
inspection-format tuning fix, design §9.13.7): AC-40.

package_workflow downloads every file the version's upload lines name from
the Portal artifacts bucket (owner asserted), verifies it against its name's
sha256 before anything is staged, and ships it once per architecture's zip
as references/{name}; manifest.json lists each under referenceFiles with its
sha256 and size. A missing or mismatched file fails packaging with the
PackagingError naming references/{name}, and nothing is staged or
registered. moto S3 and DynamoDB, a fake Use_Case-account Greengrass client
(the test_workflow_packaging_atomicity.py harness).

On the device's commit c299a6e1 the zip has no references/ entry, the
manifest no referenceFiles, a missing stored file does not fail packaging,
and the builders reject reference_files= with a TypeError. The tests of a
workflow without upload lines and of the builders called without the
keyword are preservation tests and pass there too.
"""
import hashlib
import io
import json
import sys
import time
import uuid
import zipfile
from unittest.mock import MagicMock

import pytest

OWNER = "123456789012"
STAGING_ROOT = "workflows/staging"
COMPONENTS_ROOT = "workflows/components"
ARCHS = ["x86_64", "arm64_jp5"]
PNG = b"\x89PNG\r\n\x1a\n" + b"legend image bytes for packaging"
PDF = b"%PDF-1.4\n% assembly sheet\n%%EOF\n"
PNG_NAME = hashlib.sha256(PNG).hexdigest() + ".png"
PDF_NAME = hashlib.sha256(PDF).hexdigest() + ".pdf"
MISSING_TEXT = ("Uploaded reference file upload://{name} is missing from this workflow or "
                "does not match its checksum; upload it again and save a new version")


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """workflow_packaging imported inside the moto mock."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging
    return workflow_packaging


def node(node_id, node_type, x, **parameters):
    return {"id": node_id, "type": node_type, "position": {"x": x, "y": 0},
            "parameters": parameters}


def connection(conn_id, source, target):
    return {"id": conn_id, "from": {"node": source, "port": "out"},
            "to": {"node": target, "port": "in"}}


def bedrock_definition(*references):
    """An MQTT trigger activating one folder_source -> bedrock_inference ->
    capture branch per entry of `references`, the bedrock node carrying it as
    reference_payload_path. The trigger keeps the graph compilable on both
    commits (c299a6e1 reads every line as a payload path)."""
    nodes = [node("trig", "mqtt_subscribe", 0, topic="plant/line1/evaluate", greengrass=True)]
    connections = []
    for index, reference in enumerate(references, start=1):
        nodes += [
            node(f"src{index}", "folder_source", 0, location="/data/images"),
            node(f"check{index}", "bedrock_inference", 200, answer_format="inspection",
                 prompt="Check the parts against the legend.",
                 reference_payload_path=reference,
                 allowed_uri_prefixes="file:///aws_dda/images/"),
            node(f"cap{index}", "capture", 400, output_path="/out"),
        ]
        connections += [
            {"id": f"t{index}", "from": {"node": "trig", "port": "out"},
             "to": {"node": f"src{index}", "port": "activation"}},
            connection(f"a{index}", f"src{index}", f"check{index}"),
            connection(f"b{index}", f"check{index}", f"cap{index}"),
        ]
    return {"schemaVersion": 1, "nodes": nodes, "connections": connections}


def make_greengrass():
    gg = MagicMock(name="greengrassv2")
    gg.create_component_version.return_value = {
        "arn": f"arn:aws:greengrass:us-east-1:123456789012:components:test:versions:{uuid.uuid4()}"
    }
    gg.describe_component.return_value = {
        "status": {"componentState": "DEPLOYABLE", "message": "simulated"}
    }
    return gg


class RecordingS3:
    """The packaging module's portal client, recording (operation, kwargs)."""

    def __init__(self, real):
        self._real = real
        self.calls = []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name.startswith("_") or name in ("meta", "exceptions") or not callable(attr):
            return attr

        def call(*args, **kwargs):
            self.calls.append((name, kwargs))
            return attr(*args, **kwargs)

        return call


class PackagingEnv:
    def __init__(self, env, packaging, monkeypatch, definition):
        self.env = env
        self.packaging = packaging
        self.s3 = env.s3
        monkeypatch.setattr(packaging, "COMPONENT_STATUS_POLL_SECONDS", 0)
        self.recorder = RecordingS3(packaging.s3)
        monkeypatch.setattr(packaging, "s3", self.recorder)
        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        self.s3.create_bucket(Bucket=self.usecase_bucket)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id, "name": "Reference packaging",
            "account_id": "123456789012", "s3_bucket": self.usecase_bucket})
        status, payload = env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id, "name": "packaged references",
            "definition": definition})
        assert status == 201, payload
        self.workflow_id = payload["workflow"]["workflow_id"]
        env.stack.tables.versions.update_item(
            Key={"workflow_id": self.workflow_id, "version": 1},
            UpdateExpression="SET validation_status = :v",
            ExpressionAttributeValues={":v": {"status": "passed", "validated_at": 1,
                                              "findings_key": "findings/none.json"}})
        self.greengrass = make_greengrass()

        def fake_get_usecase_client(service_name, usecase, session_name=None, region=None):
            if service_name == "s3":
                return self.s3
            if service_name == "greengrassv2":
                return self.greengrass
            raise AssertionError(f"unexpected usecase client: {service_name}")

        monkeypatch.setattr(packaging, "get_usecase_client", fake_get_usecase_client)

    def store(self, name, data):
        self.s3.put_object(
            Bucket=self.env.bucket,
            Key=f"workflows/{self.usecase_id}/{self.workflow_id}/references/{name}", Body=data)

    def package(self):
        event = self.env.event("POST", "/workflows/{id}/package", self.user,
                               workflow_id=self.workflow_id, body={"architectures": ARCHS})
        response = self.packaging.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def keys_under(self, prefix):
        listed = self.s3.list_objects_v2(Bucket=self.usecase_bucket, Prefix=prefix)
        return [o["Key"] for o in listed.get("Contents", [])]

    def zip_of(self, arch):
        key = f"{COMPONENTS_ROOT}/{self.workflow_id}/1/1.0.0/{arch}/workflow-{arch}.zip"
        body = self.s3.get_object(Bucket=self.usecase_bucket, Key=key)["Body"].read()
        return zipfile.ZipFile(io.BytesIO(body))

    def assert_nothing_packaged(self):
        assert self.keys_under(f"{STAGING_ROOT}/{self.workflow_id}/") == []
        assert self.keys_under(f"{COMPONENTS_ROOT}/{self.workflow_id}/") == []
        self.greengrass.create_component_version.assert_not_called()
        item = self.env.stack.tables.versions.get_item(
            Key={"workflow_id": self.workflow_id, "version": 1})["Item"]
        assert not item.get("component_arn")


@pytest.fixture
def make_env(env, packaging, monkeypatch):
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", OWNER)
    return lambda definition: PackagingEnv(env, packaging, monkeypatch, definition)


SHARED_AND_DOCUMENT = (f"upload://{PNG_NAME}", f"upload://{PNG_NAME}\nupload://{PDF_NAME}")


def test_each_named_file_ships_once_per_zip_with_its_checksum(make_env):
    pkg = make_env(bedrock_definition(*SHARED_AND_DOCUMENT))
    pkg.store(PNG_NAME, PNG)
    pkg.store(PDF_NAME, PDF)
    status, body = pkg.package()
    assert status == 201, body
    for arch in ARCHS:
        with pkg.zip_of(arch) as archive:
            names = archive.namelist()
            assert names.count(f"references/{PNG_NAME}") == 1
            assert names.count(f"references/{PDF_NAME}") == 1
            assert archive.read(f"references/{PNG_NAME}") == PNG
            assert archive.read(f"references/{PDF_NAME}") == PDF
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["referenceFiles"] == {
                f"references/{PNG_NAME}": {"sha256": PNG_NAME[:64], "bytes": len(PNG)},
                f"references/{PDF_NAME}": {"sha256": PDF_NAME[:64], "bytes": len(PDF)},
            }
            compiled = json.loads(archive.read("compiled_pipeline.json"))
            lines = sorted(b["parameters"]["reference_payload_path"]
                           for b in compiled["executorBindings"]
                           if b["binding"] == "bedrock_inference")
            assert lines == sorted(SHARED_AND_DOCUMENT)
    reads = [kwargs for name, kwargs in pkg.recorder.calls
             if name == "get_object" and "/references/" in kwargs["Key"]]
    assert sorted(kwargs["Key"].rsplit("/", 1)[1] for kwargs in reads) == sorted(
        [PNG_NAME, PDF_NAME])
    assert all(kwargs.get("ExpectedBucketOwner") == OWNER for kwargs in reads)


def test_a_missing_stored_file_fails_packaging_and_nothing_is_staged(make_env):
    pkg = make_env(bedrock_definition(*SHARED_AND_DOCUMENT))
    pkg.store(PNG_NAME, PNG)
    status, body = pkg.package()
    assert status == 502, body
    assert body["error"]["code"] == "PACKAGING_FAILED"
    assert body["error"]["details"]["failing_artifact"] == f"references/{PDF_NAME}"
    assert body["error"]["message"] == MISSING_TEXT.format(name=PDF_NAME)
    pkg.assert_nothing_packaged()


def test_a_file_whose_bytes_do_not_hash_to_its_name_fails_packaging(make_env):
    pkg = make_env(bedrock_definition(f"upload://{PNG_NAME}"))
    pkg.store(PNG_NAME, PNG + b"tampered")
    status, body = pkg.package()
    assert status == 502, body
    assert body["error"]["details"]["failing_artifact"] == f"references/{PNG_NAME}"
    assert body["error"]["message"] == MISSING_TEXT.format(name=PNG_NAME)
    pkg.assert_nothing_packaged()


def test_a_workflow_without_upload_lines_packages_no_reference_files(make_env):
    """Preservation: a workflow whose reference lines are typed URIs and
    payload paths packages no references/ entry and no referenceFiles
    (c299a6e1 and the fix)."""
    pkg = make_env(bedrock_definition("file:///aws_dda/images/source.jpg", ""))
    status, body = pkg.package()
    assert status == 201, body
    for arch in ARCHS:
        with pkg.zip_of(arch) as archive:
            assert [n for n in archive.namelist() if n.startswith("references/")] == []
            manifest = json.loads(archive.read("manifest.json"))
            assert "referenceFiles" not in manifest
    assert [kwargs for name, kwargs in pkg.recorder.calls
            if name == "get_object" and "/references/" in kwargs["Key"]] == []


# --------------------------------------------------------------------------
# The builders' keyword (round-4 finding 8)
# --------------------------------------------------------------------------

USER = {"user_id": "user-packager"}
MANIFEST_ARGS = ("wf-1", 3, "x86_64", [], ["boto3"], [], USER)
CUSTOM_NODES = [{"node_id": "py", "code": "def handler(m):\n    return m\n", "requirements": ""}]


@pytest.fixture
def frozen(packaging, monkeypatch):
    """Fixed packagedAt and zip entry times, so two builds compare byte for byte."""
    monkeypatch.setattr(packaging, "now_ms", lambda: 1_700_000_000_000)
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    return packaging


def zip_bytes(packaging, tmp_path, label, **keyword):
    path = tmp_path / f"{label}.zip"
    manifest = packaging.build_manifest(*MANIFEST_ARGS)
    packaging.build_arch_zip(str(path), "x86_64", manifest, '{"schemaVersion": 1}',
                             '{"segments": []}', [], CUSTOM_NODES, **keyword)
    return path.read_bytes()


def test_builders_without_the_keyword_give_todays_output(frozen, tmp_path):
    """Preservation: build_manifest and build_arch_zip called as every existing
    caller calls them produce today's manifest and entries (c299a6e1 and the
    fix)."""
    manifest = frozen.build_manifest(*MANIFEST_ARGS)
    assert "referenceFiles" not in manifest
    assert manifest["packagedAt"] == 1_700_000_000_000
    assert manifest["customPythonNodeIds"] == []
    with zipfile.ZipFile(io.BytesIO(zip_bytes(frozen, tmp_path, "plain"))) as archive:
        assert archive.namelist() == ["manifest.json", "workflow.json", "compiled_pipeline.json",
                                      "python/py/handler.py", "python/py/requirements.txt"]
        assert json.loads(archive.read("manifest.json")) == manifest


def test_builders_with_no_reference_files_are_byte_identical(frozen, tmp_path):
    plain_manifest = frozen.build_manifest(*MANIFEST_ARGS)
    for value in (None, []):
        assert frozen.build_manifest(*MANIFEST_ARGS, reference_files=value) == plain_manifest
    plain = zip_bytes(frozen, tmp_path, "plain")
    assert zip_bytes(frozen, tmp_path, "none", reference_files=None) == plain
    assert zip_bytes(frozen, tmp_path, "empty", reference_files=[]) == plain


def test_builders_write_each_reference_file_and_its_checksum(frozen, tmp_path):
    local = tmp_path / PNG_NAME
    local.write_bytes(PNG)
    reference = frozen.PackagedReference(PNG_NAME, str(local), PNG_NAME[:64], len(PNG))
    manifest = frozen.build_manifest(*MANIFEST_ARGS, reference_files=[reference])
    assert manifest["referenceFiles"] == {
        f"references/{PNG_NAME}": {"sha256": PNG_NAME[:64], "bytes": len(PNG)}}
    data = zip_bytes(frozen, tmp_path, "with", reference_files=[reference])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist().count(f"references/{PNG_NAME}") == 1
        assert archive.read(f"references/{PNG_NAME}") == PNG
