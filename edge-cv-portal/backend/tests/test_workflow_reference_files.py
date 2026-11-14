"""
Reference files uploaded for Bedrock Inference nodes: the three routes of
functions/workflow_reference_files.py, the content check, duplicate and
delete (inspection-format tuning fix, design §9.13.2 to §9.13.5).

AC-38 (upload validation and authorization) and AC-39 (immutability,
duplicate and delete). moto S3 and DynamoDB with the real shared_utils RBAC
and audit code (conftest's aws_stack); the module's S3 client
(workflow_reference_files.reference_s3_client) is wrapped by a recorder that
keeps every call's arguments and can fail a chosen call. The test plays the
browser: it PUTs the staged object with the conftest client. moto does not
verify presigned signatures, so the SigV4 assertions read the URL's query.
moto 5.1 does not evaluate CopySourceIfMatch either, so the recorder answers
a copy whose source ETag differs with S3's 412 PreconditionFailed.

On the device's commit c299a6e1, workflows.handler does not route the three
resources: they answer 404 NOT_FOUND, so the route tests fail on the missing
answer fields, and duplicate copies no stored file. The module does not exist
there either: the content-check tests import it inside the test and fail
with an ImportError.
"""
import hashlib
import io
import json
import logging
import re
import struct
import zipfile
import zlib
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

#: PORTAL_ACCOUNT_ID in these tests (moto's default account).
OWNER = "123456789012"
UPLOAD_URL = "/workflows/{id}/reference-files/upload-url"
FILES = "/workflows/{id}/reference-files"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_BYTES = 4_500_000
STEP = 1 << 20
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
         (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


# --------------------------------------------------------------------------
# Generated files
# --------------------------------------------------------------------------

def jpeg_bytes(width=64, height=48, components=3, end=True):
    """A baseline JPEG's segment structure: SOI, APP0, SOF0, SOS, scan
    bytes without markers, EOI."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (b"\xff\xc0" + struct.pack(">HBHHB", 8 + 3 * components, 8, height, width, components)
           + b"".join(bytes([i + 1, 0x11, 0]) for i in range(components)))
    sos = (b"\xff\xda" + struct.pack(">HB", 6 + 2 * components, components)
           + b"".join(bytes([i + 1, 0]) for i in range(components)) + b"\x00\x3f\x00")
    scan = bytes((i * 7) % 250 for i in range(300))
    return b"\xff\xd8" + app0 + sof + sos + scan + (b"\xff\xd9" if end else b"")


def png_chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def rgb_rows(width, height, seed=0):
    """Non-interlaced image data of an 8-bit RGB image: filter byte 0, then
    the row's pixels."""
    return b"".join(
        b"\x00" + bytes((x * 37 + y * 11 + seed) % 256 for x in range(width * 3))
        for y in range(height))


def png_bytes(width=4, height=3, colour_type=2, bit_depth=8, interlace=0,
              raw=None, idat=None, seed=0):
    if idat is None:
        idat = zlib.compress(raw if raw is not None else rgb_rows(width, height, seed))
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, colour_type, 0, 0, interlace)
    return (PNG_SIGNATURE + png_chunk(b"IHDR", ihdr) + png_chunk(b"IDAT", idat)
            + png_chunk(b"IEND", b""))


def adam7_rows(pixels, width, height):
    """Adam7 image data of an 8-bit RGB image given as pixels[y][x]."""
    out = b""
    for x0, y0, dx, dy in ADAM7:
        xs, ys = range(x0, width, dx), range(y0, height, dy)
        if len(xs) == 0 or len(ys) == 0:
            continue
        for y in ys:
            out += b"\x00" + b"".join(pixels[y][x] for x in xs)
    return out


def zip_bytes(*parts):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for part in parts:
            archive.writestr(part, "<x/>")
    return buffer.getvalue()


def png_bad_crc():
    data = bytearray(png_bytes(width=8, height=8))
    idat = data.index(b"IDAT")
    data[idat + 6] ^= 0xFF  # inside the IDAT data; its CRC is left as it was
    return bytes(data)


def png_cut_inside_idat():
    data = png_bytes(width=16, height=16)
    return data[:data.index(b"IDAT") + 10]


PDF = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"
DOCUMENTS = {
    "manual.pdf": (PDF, "pdf", "application/pdf"),
    "manual.docx": (zip_bytes("[Content_Types].xml", "word/document.xml"), "docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "parts.xlsx": (zip_bytes("[Content_Types].xml", "xl/workbook.xml"), "xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "manual.doc": (OLE2_MAGIC + bytes(504), "doc", "application/msword"),
    "parts.xls": (OLE2_MAGIC + bytes(504), "xls", "application/vnd.ms-excel"),
    "notes.txt": (b"Bolts: 4\n", "txt", "text/plain; charset=utf-8"),
    "notes.markdown": (b"# Legend\n- red: fender\n", "md", "text/markdown; charset=utf-8"),
    "parts.csv": (b"part,colour\nfender,red\n", "csv", "text/csv; charset=utf-8"),
    "legend.htm": (b"<p>legend</p>\n", "html", "text/plain; charset=utf-8"),
}

REJECTED = [
    ("a PDF named .jpg", "legend.jpg", PDF, "REFERENCE_FILE_INVALID",
     "legend.jpg is not a valid JPEG or PNG image: "),
    ("a PNG with a bad chunk CRC", "legend.png", png_bad_crc(), "REFERENCE_FILE_INVALID",
     "legend.png is not a valid JPEG or PNG image: a chunk fails its CRC check"),
    ("a PNG cut inside its IDAT data", "legend.png", png_cut_inside_idat(),
     "REFERENCE_FILE_INVALID", "legend.png is not a valid JPEG or PNG image: it is truncated"),
    ("a JPEG without an end marker", "legend.jpg", jpeg_bytes(end=False),
     "REFERENCE_FILE_INVALID", "legend.jpg is not a valid JPEG or PNG image: it has no end marker"),
    ("an image of 8001 px on a side", "wide.png",
     png_bytes(width=8001, height=1, raw=b"\x00" + bytes(8001 * 3)), "REFERENCE_FILE_INVALID",
     "wide.png is not a valid JPEG or PNG image: it is 8001 × 1 px"),
    ("an 8,388,609-byte image", "large.png", None, "REFERENCE_FILE_TOO_LARGE", "large.png"),
    ("a 4,500,001-byte TXT", "large.txt", b"a" * (MAX_DOCUMENT_BYTES + 1),
     "REFERENCE_FILE_TOO_LARGE", "large.txt"),
    ("a DOCX without word/document.xml", "manual.docx", zip_bytes("[Content_Types].xml"),
     "REFERENCE_FILE_INVALID", "manual.docx is not a valid DOCX document: "
     "it has no word/document.xml part"),
    ("a TXT that is not UTF-8", "notes.txt", b"\xff\xfe\x00 not utf-8",
     "REFERENCE_FILE_INVALID", "notes.txt is not a valid TXT document: it is not UTF-8 text"),
    ("an empty MD", "notes.md", b"  \n\n", "REFERENCE_FILE_INVALID",
     "notes.md is not a valid MD document: it is empty"),
]


def large_image():
    image = png_bytes()
    return image + bytes(MAX_IMAGE_BYTES + 1 - len(image))


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

def bedrock_definition(reference=""):
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


class RecordingS3:
    """The module's real (moto-backed) client, recording (operation, kwargs)
    of every call, raising a ClientError (or `error`) from a call `fail`
    matches, running an `after` hook once a call returned, and evaluating a
    copy's CopySourceIfMatch as S3 does."""

    def __init__(self, real):
        self._real = real
        self.calls = []
        self._failures = {}
        self._hooks = {}

    def fail(self, operation, predicate, code="InternalError", error=None):
        self._failures[operation] = (predicate, code, error)

    def after(self, operation, hook):
        self._hooks[operation] = hook

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name.startswith("_") or name in ("meta", "exceptions") or not callable(attr):
            return attr

        def call(*args, **kwargs):
            self.calls.append((name, kwargs))
            failure = self._failures.get(name)
            if failure is not None and failure[0](kwargs):
                if failure[2] is not None:
                    raise failure[2]
                raise ClientError({"Error": {"Code": failure[1], "Message": "simulated"},
                                   "ResponseMetadata": {"HTTPStatusCode": 500}}, name)
            if name == "copy_object" and "CopySourceIfMatch" in kwargs:
                source = kwargs["CopySource"]
                etag = self._real.head_object(Bucket=source["Bucket"], Key=source["Key"])["ETag"]
                if etag != kwargs["CopySourceIfMatch"]:
                    raise ClientError({"Error": {"Code": "PreconditionFailed",
                                                 "Message": "simulated"},
                                       "ResponseMetadata": {"HTTPStatusCode": 412}}, name)
            result = attr(*args, **kwargs)
            hook = self._hooks.get(name)
            if hook is not None:
                hook(kwargs)
            return result

        return call


class ReferenceFilesEnv:
    def __init__(self, env, monkeypatch):
        self.env = env
        self.s3 = env.s3
        self.bucket = env.bucket
        self.user = env.make_user(role="DataScientist")
        self.usecase_id = env.create_usecase()
        self.workflow_id = self.create_workflow()
        try:
            import workflow_reference_files as module
        except ImportError:  # c299a6e1: no module, the routes answer 404
            module = None
        self.module = module
        self.recorder = None
        if module is not None:
            self.recorder = RecordingS3(module.reference_s3_client())
            monkeypatch.setattr(module, "_reference_s3_client", self.recorder)

    def create_workflow(self, reference=""):
        status, payload = self.env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id, "name": "reference files",
            "definition": bedrock_definition(reference)})
        assert status == 201, payload
        return payload["workflow"]["workflow_id"]

    def invoke(self, method, resource, user=None, body=None, workflow_id=None, query=None):
        return self.env.invoke(method, resource, user or self.user,
                               workflow_id or self.workflow_id, body, query)

    def request_upload(self, file_name, user=None, workflow_id=None):
        return self.invoke("POST", UPLOAD_URL, user=user, body={"fileName": file_name},
                           workflow_id=workflow_id)

    def upload(self, file_name, data, workflow_id=None):
        """upload-url, the browser's PUT, complete: (status, body, stagingKey)."""
        status, answer = self.request_upload(file_name, workflow_id=workflow_id)
        assert status == 200 and "stagingKey" in answer, (status, answer)
        self.s3.put_object(Bucket=self.bucket, Key=answer["stagingKey"], Body=data)
        status, body = self.invoke("POST", FILES, workflow_id=workflow_id, body={
            "stagingKey": answer["stagingKey"], "fileName": file_name})
        return status, body, answer["stagingKey"]

    def stored(self, file_name, data, workflow_id=None):
        status, body, _ = self.upload(file_name, data, workflow_id)
        assert status == 201, body
        return body["file"]

    def exists(self, key):
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def prefix(self, workflow_id=None):
        return f"workflows/{self.usecase_id}/{workflow_id or self.workflow_id}/references/"

    def keys(self, prefix):
        listed = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        return sorted(o["Key"] for o in listed.get("Contents", []))

    def stored_names(self, workflow_id=None):
        prefix = self.prefix(workflow_id)
        return [key[len(prefix):] for key in self.keys(prefix)]

    def read(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def save(self, reference):
        status, body = self.invoke("PUT", "/workflows/{id}",
                                   body={"definition": bedrock_definition(reference)})
        assert status == 200, body
        return body["version"]


@pytest.fixture
def refs(env, monkeypatch):
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", OWNER)
    return ReferenceFilesEnv(env, monkeypatch)


def reference_module():
    import workflow_reference_files
    return workflow_reference_files


# --------------------------------------------------------------------------
# AC-38: the upload URL
# --------------------------------------------------------------------------

class TestUploadUrl:
    def test_the_upload_url_is_sigv4_over_host_and_the_owner_header(self, refs):
        status, body = refs.request_upload("legend.png")
        assert status == 200 and "uploadUrl" in body, (status, body)
        query = parse_qs(urlparse(body["uploadUrl"]).query)
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        assert query["X-Amz-Expires"] == ["900"]
        assert query["X-Amz-SignedHeaders"] == ["host;x-amz-expected-bucket-owner"]
        assert "content-type" not in query["X-Amz-SignedHeaders"][0].split(";")
        assert body["uploadHeaders"] == {"x-amz-expected-bucket-owner": OWNER}
        assert body["expiresInSeconds"] == 900
        assert body["maxBytes"] == MAX_IMAGE_BYTES
        assert re.fullmatch(rf"workflow-reference-staging/{refs.workflow_id}/[0-9a-f]{{32}}",
                            body["stagingKey"])
        # Presigning makes no S3 request; it goes through the module's client.
        assert [name for name, _ in refs.recorder.calls] == ["generate_presigned_url"]
        (_, kwargs), = refs.recorder.calls
        assert kwargs["Params"]["ExpectedBucketOwner"] == OWNER
        assert "ContentType" not in kwargs["Params"]

    def test_a_document_gets_the_document_cap(self, refs):
        status, body = refs.request_upload("C:\\fakepath\\manual.PDF")
        assert status == 200 and "maxBytes" in body, (status, body)
        assert body["maxBytes"] == MAX_DOCUMENT_BYTES

    def test_an_unsupported_extension_is_refused(self, refs):
        status, body = refs.request_upload("legend.gif")
        assert status == 400, body
        assert body["error"]["code"] == "UNSUPPORTED_REFERENCE_FILE"
        assert ".png" in body["error"]["details"]["accepted_extensions"]
        assert refs.recorder.calls == []

    def test_a_control_character_in_the_file_name_is_refused(self, refs):
        status, body = refs.request_upload("leg\x07end.png")
        assert status == 400, body
        assert body["error"]["code"] == "INVALID_FILE_NAME"


# --------------------------------------------------------------------------
# AC-38: accepted files
# --------------------------------------------------------------------------

class TestAcceptedFiles:
    def test_a_jpeg_and_a_png_are_stored_with_kind_format_and_size(self, refs):
        jpeg = jpeg_bytes(width=64, height=48)
        status, body, staging_key = refs.upload("legend.jpeg", jpeg)
        assert status == 201, body
        record = body["file"]
        sha = hashlib.sha256(jpeg).hexdigest()
        assert body["created"] is True
        assert record["name"] == f"{sha}.jpg"
        assert record["line"] == f"upload://{sha}.jpg"
        assert (record["kind"], record["format"], record["width"], record["height"]) == (
            "image", "jpeg", 64, 48)
        assert (record["bytes"], record["fileName"], record["schema"]) == (len(jpeg), "legend.jpeg", 1)
        assert record["uploadedBy"] == refs.user["user_id"]
        key = refs.prefix() + record["name"]
        assert refs.read(key) == jpeg
        assert refs.s3.head_object(Bucket=refs.bucket, Key=key)["ContentType"] == "image/jpeg"
        assert json.loads(refs.read(key + ".json")) == record
        assert not refs.exists(staging_key)

        png = png_bytes(width=4, height=3)
        record = refs.stored("legend.png", png)
        assert (record["kind"], record["format"], record["width"], record["height"]) == (
            "image", "png", 4, 3)
        assert record["name"] == f"{hashlib.sha256(png).hexdigest()}.png"

    def test_a_png_named_jpg_is_stored_as_png(self, refs):
        png = png_bytes(width=5, height=2, seed=3)
        record = refs.stored("legend.jpg", png)
        assert record["line"].endswith(".png")
        assert record["format"] == "png"
        key = refs.prefix() + record["name"]
        assert refs.s3.head_object(Bucket=refs.bucket, Key=key)["ContentType"] == "image/png"

    @pytest.mark.parametrize("file_name", sorted(DOCUMENTS))
    def test_every_document_format_is_accepted(self, refs, file_name):
        data, ext, content_type = DOCUMENTS[file_name]
        status, body, staging_key = refs.upload(file_name, data)
        assert status == 201, body
        record = body["file"]
        assert record["name"] == f"{hashlib.sha256(data).hexdigest()}.{ext}"
        assert (record["kind"], record["format"], record["ext"]) == ("document", ext, ext)
        assert "width" not in record and "height" not in record
        key = refs.prefix() + record["name"]
        assert refs.s3.head_object(Bucket=refs.bucket, Key=key)["ContentType"] == content_type
        assert not refs.exists(staging_key)

    def test_an_adam7_png_and_a_non_interlaced_one_of_the_same_pixels_are_accepted(self, refs):
        pixels = [[bytes([(x * 80 + y * 30) % 256, (x * 20 + 7) % 256, (y * 90) % 256])
                   for x in range(3)] for y in range(3)]
        plain = b"".join(b"\x00" + b"".join(row) for row in pixels)
        interlaced = adam7_rows(pixels, 3, 3)
        assert len(interlaced) == 1 * 4 + 1 * 4 + 1 * 7 + 2 * 4 + 1 * 10  # passes 1, 4, 5, 6, 7
        for interlace, raw in ((0, plain), (1, interlaced)):
            record = refs.stored(f"tiny-{interlace}.png",
                                 png_bytes(width=3, height=3, interlace=interlace, raw=raw))
            assert (record["width"], record["height"]) == (3, 3)


# --------------------------------------------------------------------------
# AC-38: rejected files
# --------------------------------------------------------------------------

class TestRejectedFiles:
    @pytest.mark.parametrize("case", REJECTED, ids=[case[0] for case in REJECTED])
    def test_each_bad_file_is_rejected_and_its_staged_object_deleted(self, refs, case):
        _, file_name, data, code, message = case
        status, body, staging_key = refs.upload(file_name, large_image() if data is None else data)
        assert status == 400, body
        assert body["error"]["code"] == code
        assert body["error"]["message"].startswith(message)
        assert not refs.exists(staging_key)
        assert refs.stored_names() == []

    def test_a_staging_key_of_another_workflow_or_without_32_hex_reads_nothing(self, refs):
        other_workflow = refs.create_workflow()
        status, answer = refs.request_upload("legend.png", workflow_id=other_workflow)
        assert status == 200 and "stagingKey" in answer, (status, answer)
        refs.s3.put_object(Bucket=refs.bucket, Key=answer["stagingKey"], Body=png_bytes())
        refs.recorder.calls.clear()
        for staging_key in (answer["stagingKey"],
                            f"workflow-reference-staging/{refs.workflow_id}/{'a' * 31}",
                            f"workflow-reference-staging/{refs.workflow_id}/{'A' * 32}"):
            status, body = refs.invoke("POST", FILES, body={"stagingKey": staging_key,
                                                            "fileName": "legend.png"})
            assert status == 400, body
        assert refs.recorder.calls == []
        assert refs.exists(answer["stagingKey"])


# --------------------------------------------------------------------------
# AC-38: authorization and owner assertions
# --------------------------------------------------------------------------

class TestAuthorization:
    def test_a_user_without_workflow_read_gets_404_on_every_route(self, refs):
        stranger = refs.env.make_user(role="DataLabeler")
        for method, resource, body in (("POST", UPLOAD_URL, {"fileName": "legend.png"}),
                                       ("POST", FILES, {"stagingKey": "x", "fileName": "a.png"}),
                                       ("GET", FILES, None)):
            status, answer = refs.invoke(method, resource, user=stranger, body=body)
            assert status == 404, (resource, answer)
            assert answer["error"]["code"] == "WORKFLOW_NOT_FOUND"

    def test_a_reader_gets_403_with_the_audit_on_both_writes_and_200_on_the_list(self, refs):
        viewer = refs.env.make_user(role="Viewer")
        for resource, body in ((UPLOAD_URL, {"fileName": "legend.png"}),
                               (FILES, {"stagingKey": "x", "fileName": "legend.png"})):
            status, answer = refs.invoke("POST", resource, user=viewer, body=body)
            assert status == 403, (resource, answer)
            assert answer["error"]["code"] == "FORBIDDEN"
            assert answer["error"]["details"]["required_permissions"] == ["workflow:edit"]
        denied = [item for item in refs.env.stack.tables.audit_log.scan()["Items"]
                  if item["user_id"] == viewer["user_id"]
                  and item["action"] == "unauthorized_access"]
        assert sorted(item["resource_id"] for item in denied) == sorted([UPLOAD_URL, FILES])
        status, answer = refs.invoke("GET", FILES, user=viewer)
        assert status == 200 and answer.get("files") == [], (status, answer)

    def test_every_s3_call_asserts_the_portal_account_as_owner(self, refs):
        png = png_bytes(width=6, height=6, seed=9)
        refs.stored("legend.png", png)
        status, body, _ = refs.upload("again.png", png)
        assert status == 200, body
        status, body = refs.invoke("GET", FILES)
        assert status == 200, body
        operations = {name for name, _ in refs.recorder.calls}
        assert {"generate_presigned_url", "head_object", "get_object", "copy_object",
                "put_object", "delete_object", "list_objects_v2"} <= operations
        for name, kwargs in refs.recorder.calls:
            arguments = kwargs["Params"] if name == "generate_presigned_url" else kwargs
            assert arguments.get("ExpectedBucketOwner") == OWNER, (name, kwargs)
            if name == "copy_object":
                assert kwargs.get("ExpectedSourceBucketOwner") == OWNER, kwargs


class TestAuditAndLogs:
    def test_an_accepted_upload_audits_once_and_no_log_holds_its_name_or_content(
            self, refs, caplog):
        caplog.set_level(logging.INFO)
        file_name = "pump-legend-q7.txt"
        marker = "LEGEND-CONTENT-7c1"
        data = f"{marker}: red is the front fender\n".encode()
        record = refs.stored(file_name, data)
        rejected_name = "pump-legend-q8.txt"
        status, body, _ = refs.upload(rejected_name, b"\xff" + marker.encode())
        assert status == 400, body

        events = [item for item in refs.env.stack.tables.audit_log.scan()["Items"]
                  if item["action"] == "upload_workflow_reference_file"
                  and item["resource_id"] == refs.workflow_id]
        assert len(events) == 1
        assert events[0]["result"] == "success"
        assert events[0]["details"] == {
            "usecase_id": refs.usecase_id, "name": record["name"], "kind": "document",
            "format": "txt", "bytes": len(data), "file_name": file_name}
        messages = [r.getMessage() for r in caplog.records]
        assert any(record["name"] in m for m in messages)
        for message in messages:
            assert file_name not in message and rejected_name not in message, message
            assert marker not in message, message
            assert "X-Amz-Signature" not in message, message


# --------------------------------------------------------------------------
# AC-38: the PNG inflate rule (unit level)
# --------------------------------------------------------------------------

class CountingDecompressor:
    """A zlib decompressor that counts the output it returns."""

    def __init__(self, real):
        self._real = real
        self.output = 0
        self.max_lengths = []

    def decompress(self, data, max_length=0):
        out = self._real.decompress(data, max_length)
        self.output += len(out)
        self.max_lengths.append(max_length)
        return out

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def counting(monkeypatch):
    """Wrap every zlib decompressor the module creates in a counter."""
    module = reference_module()
    real = zlib.decompressobj
    counters = []

    def factory(*args, **kwargs):
        counters.append(CountingDecompressor(real(*args, **kwargs)))
        return counters[-1]

    monkeypatch.setattr(module.zlib, "decompressobj", factory)
    return module, counters


def zeros_stream(compressed_bytes):
    """About `compressed_bytes` of a zlib stream of zeros: one full-flush
    segment of 1 MiB of zeros repeated (no stream end)."""
    compressor = zlib.compressobj(9)
    head = compressor.compress(bytes(STEP)) + compressor.flush(zlib.Z_FULL_FLUSH)
    segment = compressor.compress(bytes(STEP)) + compressor.flush(zlib.Z_FULL_FLUSH)
    return head + segment * (compressed_bytes // len(segment))


class TestInflateRule:
    def test_image_data_inflating_beyond_its_declared_size_stops_within_one_step(self, counting):
        module, counters = counting
        stream = zeros_stream(500_000)
        assert 450_000 < len(stream) < 520_000
        data = png_bytes(width=100, height=100, colour_type=6, idat=stream)
        declared = 100 * (1 + 100 * 4)
        assert module.png_image_data_length(100, 100, 6, 8, 0) == declared
        info, reason = module.inspect_reference_file("bomb.png", data)
        assert (info, reason) == (None, "its image data inflates beyond its declared size")
        (counter,) = counters
        assert counter.output > declared
        assert counter.output <= declared + STEP
        assert counter.max_lengths and all(m == STEP for m in counter.max_lengths)

    def test_complete_idat_chunks_holding_a_stream_that_ends_early_are_truncated(self, counting):
        module, _ = counting
        full = zlib.compress(rgb_rows(32, 32))
        data = png_bytes(width=32, height=32, idat=full[:len(full) // 2])
        assert module.inspect_reference_file("legend.png", data) == (
            None, "its image data is truncated")
        short = png_bytes(width=32, height=32, raw=rgb_rows(32, 31))
        assert module.inspect_reference_file("legend.png", short) == (
            None, "its image data is truncated")

    def test_corrupt_image_data_is_rejected(self, counting):
        module, _ = counting
        data = png_bytes(width=4, height=4, idat=b"\x78\x9c" + b"\xff" * 64)
        assert module.inspect_reference_file("legend.png", data) == (
            None, "its image data is corrupt")

    def test_the_declared_length_counts_the_adam7_passes(self):
        module = reference_module()
        assert module.png_image_data_length(3, 3, 2, 8, 0) == 3 * (1 + 9)
        assert module.png_image_data_length(3, 3, 2, 8, 1) == 33
        assert module.png_image_data_length(1, 1, 0, 1, 1) == 2
        assert module.png_image_data_length(8000, 8000, 6, 16, 0) == 512_008_000


# --------------------------------------------------------------------------
# AC-39: immutability, duplicate and delete
# --------------------------------------------------------------------------

class TestImmutabilityDuplicateDelete:
    def test_the_same_bytes_twice_give_one_line_and_one_stored_file(self, refs):
        png = png_bytes(width=7, height=7, seed=5)
        status, first, _ = refs.upload("legend.png", png)
        assert status == 201, first
        status, second, staging_key = refs.upload("legend-copy.png", png)
        assert status == 200, second
        assert second["created"] is False
        assert second["file"]["line"] == first["file"]["line"]
        assert second["file"]["fileName"] == "legend.png"
        name = first["file"]["name"]
        assert refs.stored_names() == [name, f"{name}.json"]
        assert not refs.exists(staging_key)

    def test_the_same_bytes_as_txt_and_md_are_two_files(self, refs):
        data = b"- red: front fender\n"
        txt = refs.stored("notes.txt", data)
        md = refs.stored("notes.md", data)
        assert txt["line"] != md["line"]
        assert {txt["line"][-4:], md["line"][-3:]} == {".txt", ".md"}
        assert len(refs.stored_names()) == 4

    def test_a_saved_version_keeps_its_line_and_its_bytes(self, refs):
        first_png, second_png = png_bytes(seed=1), png_bytes(seed=2)
        first = refs.stored("legend.png", first_png)
        kept_version = refs.save(first["line"])
        second = refs.stored("legend.png", second_png)
        refs.save(second["line"])
        status, body = refs.invoke("GET", "/workflows/{id}", query={"version": str(kept_version)})
        assert status == 200, body
        nodes = {node["id"]: node for node in body["definition"]["nodes"]}
        assert nodes["check"]["parameters"]["reference_payload_path"] == first["line"]
        assert refs.read(refs.prefix() + first["name"]) == first_png

    def test_duplicate_copies_exactly_the_files_and_records_the_latest_version_names(self, refs):
        named = refs.stored("legend.png", png_bytes(seed=11))
        refs.stored("unused.pdf", PDF)
        refs.save(named["line"])
        status, body = refs.invoke("POST", "/workflows/{id}/duplicate", body={"name": "copy"})
        assert status == 201, body
        copy_id = body["workflow"]["workflow_id"]
        assert refs.stored_names(copy_id) == [named["name"], f"{named['name']}.json"]
        assert refs.read(refs.prefix(copy_id) + named["name"]) == png_bytes(seed=11)
        assert json.loads(refs.read(refs.prefix(copy_id) + named["name"] + ".json")) == named

    def test_a_failed_copy_answers_500_and_creates_nothing(self, refs):
        named = refs.stored("legend.png", png_bytes(seed=12))
        refs.save(named["line"])
        if refs.recorder is not None:
            refs.recorder.fail("copy_object", lambda kwargs: kwargs["Key"].endswith(".json"))
        status, body = refs.invoke("POST", "/workflows/{id}/duplicate", body={"name": "copy"})
        assert status == 500, body
        assert body["error"]["code"] == "REFERENCE_FILE_COPY_FAILED"
        workflows = refs.env.workflows.query_workflows_by_usecase(refs.usecase_id)
        assert [w["workflow_id"] for w in workflows] == [refs.workflow_id]
        source_prefix = f"workflows/{refs.usecase_id}/{refs.workflow_id}/"
        assert [k for k in refs.keys(f"workflows/{refs.usecase_id}/")
                if not k.startswith(source_prefix)] == []

    def test_delete_removes_the_stored_files_and_records(self, refs):
        refs.stored("legend.png", png_bytes(seed=13))
        refs.stored("notes.txt", b"bolts\n")
        assert len(refs.stored_names()) == 4
        status, body = refs.invoke("DELETE", "/workflows/{id}")
        assert status == 200, body
        assert refs.stored_names() == []


# --------------------------------------------------------------------------
# AC-38: only checked bytes are stored; S3 failures (design §16, §18)
# --------------------------------------------------------------------------

def transport_error():
    return EndpointConnectionError(endpoint_url="https://s3.example.invalid")


def make_owner_unknown(monkeypatch, refs):
    from shared_utils import BucketOwnerUnknown

    def unknown():
        raise BucketOwnerUnknown("no account id")

    monkeypatch.setattr(refs.module, "component_bucket_owner", unknown)


class TestStoreRace:
    def test_bytes_put_to_the_staging_key_after_the_check_are_never_stored(self, refs):
        checked = png_bytes(width=8, height=8, seed=21)
        status, answer = refs.request_upload("legend.png")
        assert status == 200 and "stagingKey" in answer, (status, answer)
        staging_key = answer["stagingKey"]
        checked_etag = refs.s3.put_object(Bucket=refs.bucket, Key=staging_key,
                                          Body=checked)["ETag"]
        if refs.recorder is not None:
            def swap(kwargs):  # the browser PUTs again once the route read the object
                if kwargs["Key"] == staging_key:
                    refs.s3.put_object(Bucket=refs.bucket, Key=staging_key, Body=b"\x00" * 4096)

            refs.recorder.after("get_object", swap)
        status, body = refs.invoke("POST", FILES, body={"stagingKey": staging_key,
                                                        "fileName": "legend.png"})
        assert status == 502, body
        assert body["error"]["code"] == "REFERENCE_FILE_STORE_FAILED"
        assert refs.stored_names() == []
        assert not refs.exists(staging_key)
        copies = [kwargs for name, kwargs in refs.recorder.calls if name == "copy_object"]
        assert [kwargs["CopySourceIfMatch"] for kwargs in copies] == [checked_etag]
        # The content-addressed name is not taken: the same file uploads again.
        refs.recorder.after("get_object", lambda kwargs: None)
        record = refs.stored("legend.png", checked)
        assert record["name"] == f"{hashlib.sha256(checked).hexdigest()}.png"
        assert refs.read(refs.prefix() + record["name"]) == checked


class TestS3Failures:
    @pytest.mark.parametrize("failure", ["listing", "record read", "transport", "owner unknown"])
    def test_a_failed_listing_answers_502_reference_files_load_failed(
            self, refs, monkeypatch, caplog, failure):
        refs.stored("legend.png", png_bytes(seed=31))
        if refs.recorder is not None:
            if failure == "listing":
                refs.recorder.fail("list_objects_v2", lambda kwargs: True)
            elif failure == "record read":
                refs.recorder.fail("get_object", lambda kwargs: kwargs["Key"].endswith(".json"))
            elif failure == "transport":
                refs.recorder.fail("list_objects_v2", lambda kwargs: True, error=transport_error())
            else:
                make_owner_unknown(monkeypatch, refs)
        with caplog.at_level(logging.INFO):
            status, body = refs.invoke("GET", FILES)
        assert status == 502, body
        assert body["error"]["code"] == "REFERENCE_FILES_LOAD_FAILED"
        assert [r.levelno for r in caplog.records
                if "reference files of workflow" in r.getMessage()] == [logging.ERROR]

    @pytest.mark.parametrize("operation", ["head_object", "get_object", "copy_object"])
    def test_a_transport_error_while_storing_answers_502_and_deletes_the_staged_object(
            self, refs, operation):
        if refs.recorder is not None:
            refs.recorder.fail(operation, lambda kwargs: True, error=transport_error())
        status, body, staging_key = refs.upload("legend.png", png_bytes(seed=32))
        assert status == 502, body
        assert body["error"]["code"] == "REFERENCE_FILE_STORE_FAILED"
        assert not refs.exists(staging_key)
        assert refs.stored_names() == []

    def test_a_transport_error_while_duplicating_answers_500_and_creates_nothing(self, refs):
        named = refs.stored("legend.png", png_bytes(seed=33))
        refs.save(named["line"])
        if refs.recorder is not None:
            refs.recorder.fail("copy_object", lambda kwargs: kwargs["Key"].endswith(".json"),
                               error=transport_error())
        status, body = refs.invoke("POST", "/workflows/{id}/duplicate", body={"name": "copy"})
        assert status == 500, body
        assert body["error"]["code"] == "REFERENCE_FILE_COPY_FAILED"
        workflows = refs.env.workflows.query_workflows_by_usecase(refs.usecase_id)
        assert [w["workflow_id"] for w in workflows] == [refs.workflow_id]
        source_prefix = f"workflows/{refs.usecase_id}/{refs.workflow_id}/"
        assert [k for k in refs.keys(f"workflows/{refs.usecase_id}/")
                if not k.startswith(source_prefix)] == []

    def test_an_unknown_bucket_owner_fails_complete_and_duplicate_without_an_s3_call(
            self, refs, monkeypatch):
        named = refs.stored("legend.png", png_bytes(seed=34))
        refs.save(named["line"])
        status, answer = refs.request_upload("other.png")
        assert status == 200 and "stagingKey" in answer, (status, answer)
        refs.s3.put_object(Bucket=refs.bucket, Key=answer["stagingKey"], Body=png_bytes(seed=35))
        make_owner_unknown(monkeypatch, refs)
        refs.recorder.calls.clear()
        status, body = refs.invoke("POST", FILES, body={"stagingKey": answer["stagingKey"],
                                                        "fileName": "other.png"})
        assert status == 502, body
        assert body["error"]["code"] == "REFERENCE_FILE_STORE_FAILED"
        status, body = refs.invoke("POST", "/workflows/{id}/duplicate", body={"name": "copy"})
        assert status == 500, body
        assert body["error"]["code"] == "REFERENCE_FILE_COPY_FAILED"
        assert refs.recorder.calls == []
        assert refs.stored_names() == [named["name"], f"{named['name']}.json"]
