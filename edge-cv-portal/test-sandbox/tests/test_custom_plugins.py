"""Custom plugin staging + stub identification in the test harness
(custom-node-designer tasks 13.1 and 13.3, Requirements 12.1, 12.2).

Covers the pure/AWS-boundary helpers without GStreamer:

- ``custom_plugins.json`` manifest key derivation, defensive parsing,
  and absent-manifest handling (runs without custom nodes),
- staging the listed custom x86_64 Plugin_Artifacts into the task's
  plugin scan directory and prepending it to GST_PLUGIN_PATH (12.1),
- identifying stubbed Custom_Node_Types from the compiled document's
  ``custom_stub_<nodeId>`` identity elements (12.2),
- the test run report shape for stubbed Custom_Node_Types (task 13.3):
  a full ``harness.execute`` run (GStreamer mocked) records exactly one
  ``custom_node_stub`` stubActivity entry per stubbed node whose note
  describes the limitation that the node was simulated because no
  x86_64 build exists, retained even when the pipeline later fails.
"""

import io
import json
import os

from botocore.exceptions import ClientError
from PIL import Image

from harness import harness, renderer
from harness.results import ResultsStore


def _baseline_jpeg_bytes(size=(16, 12)):
    """Real baseline JPEG bytes the harness staging can normalize."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color=0).save(buffer, "JPEG", progressive=False)
    return buffer.getvalue()


def element(factory, node_id=None, **args):
    return {"nodeId": node_id, "factory": factory, "args": args}


def document_with(elements):
    return {"segments": [
        {"name": "s0", "from": None, "linkTo": None, "elements": elements},
    ]}


class FakeS3:
    """get_object/download_file/put_object/paginator double over an
    in-memory object map."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.downloads = []
        self.puts = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
                "GetObject")
        import io
        return {"Body": io.BytesIO(self.objects[Key])}

    def download_file(self, bucket, key, target):
        if key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "missing"}},
                "GetObject")
        with open(target, "wb") as handle:
            handle.write(self.objects[key])
        self.downloads.append((key, target))

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body if isinstance(Body, bytes) else \
            Body.encode("utf-8")
        self.puts.append(Key)
        return {}

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        objects = self.objects

        class Paginator:
            def paginate(self, Bucket, Prefix):
                contents = [{"Key": key} for key in sorted(objects)
                            if key.startswith(Prefix)]
                yield {"Contents": contents}

        return Paginator()


RESULTS_KEY = "workflows/uc-1/test-runs/run-1/results.json"
MANIFEST_KEY = "workflows/uc-1/test-runs/run-1/custom_plugins.json"


class TestManifest:

    def test_manifest_key_lives_next_to_results_document(self):
        assert harness.custom_plugins_manifest_key(RESULTS_KEY) == MANIFEST_KEY

    def test_absent_manifest_yields_empty_shape(self):
        manifest = harness.load_custom_plugins_manifest(
            FakeS3(), "bucket", RESULTS_KEY)
        assert manifest == {"plugins": [], "stubbedNodeTypeIds": []}

    def test_manifest_round_trip(self):
        stored = {
            "plugins": [{"nodeTypeId": "custom.blur", "fileName": "blur.so",
                         "s3Key": "workflows/uc-1/test-runs/run-1/plugins/blur.so"}],
            "stubbedNodeTypeIds": ["custom.edge"],
        }
        s3 = FakeS3({MANIFEST_KEY: json.dumps(stored).encode("utf-8")})
        manifest = harness.load_custom_plugins_manifest(s3, "bucket",
                                                        RESULTS_KEY)
        assert manifest == stored

    def test_malformed_manifest_yields_empty_shape(self):
        s3 = FakeS3({MANIFEST_KEY: b"not json"})
        manifest = harness.load_custom_plugins_manifest(s3, "bucket",
                                                        RESULTS_KEY)
        assert manifest == {"plugins": [], "stubbedNodeTypeIds": []}

    def test_defensive_parse_drops_non_conforming_entries(self):
        parsed = harness.parse_custom_plugins_manifest({
            "plugins": [{"s3Key": "k"}, "not-a-dict"],
            "stubbedNodeTypeIds": ["custom.a", 7],
        })
        assert parsed == {"plugins": [{"s3Key": "k"}],
                          "stubbedNodeTypeIds": ["custom.a"]}


class TestPluginStaging:

    def test_plugins_staged_into_scan_dir(self, tmp_path):
        s3 = FakeS3({
            "workflows/uc-1/test-runs/run-1/plugins/blur.so": b"ELF blur",
            "workflows/uc-1/test-runs/run-1/plugins/edge.so": b"ELF edge",
        })
        scan_dir = str(tmp_path / "plugins")
        staged = harness.stage_custom_plugins(s3, "bucket", [
            {"nodeTypeId": "custom.blur", "fileName": "blur.so",
             "s3Key": "workflows/uc-1/test-runs/run-1/plugins/blur.so"},
            {"nodeTypeId": "custom.edge", "fileName": "edge.so",
             "s3Key": "workflows/uc-1/test-runs/run-1/plugins/edge.so"},
        ], scan_dir)
        assert [os.path.basename(p) for p in staged] == ["blur.so", "edge.so"]
        assert open(os.path.join(scan_dir, "blur.so"), "rb").read() == b"ELF blur"

    def test_file_name_falls_back_to_key_basename_with_so_suffix(self, tmp_path):
        s3 = FakeS3({"plugins/blur": b"ELF"})
        staged = harness.stage_custom_plugins(
            s3, "bucket", [{"s3Key": "plugins/blur"}], str(tmp_path))
        assert [os.path.basename(p) for p in staged] == ["blur.so"]

    def test_entries_without_key_are_skipped(self, tmp_path):
        staged = harness.stage_custom_plugins(
            FakeS3(), "bucket", [{"fileName": "x.so"}], str(tmp_path))
        assert staged == []

    def test_extend_plugin_path_prepends_once(self):
        assert harness.extend_plugin_path(None, "/scan") == "/scan"
        assert harness.extend_plugin_path("/other", "/scan") == \
            "/scan" + os.pathsep + "/other"
        assert harness.extend_plugin_path(
            "/scan" + os.pathsep + "/other", "/scan") == \
            "/scan" + os.pathsep + "/other"


class TestCustomStubIdentification:

    def test_custom_stub_nodes_detected(self):
        document = document_with([
            element("multifilesrc", "n1", location="/data/%05d.jpg"),
            element("identity", "n2", name="custom_stub_n2"),
            element("identity", "n3", name="sim_inference_n3"),
            element("identity", "n4"),
            element("multifilesink", "n5", location="/out/%05d.jpg"),
        ])
        assert renderer.custom_stub_node_ids(document) == ["n2"]

    def test_no_custom_stubs_in_plain_document(self):
        document = document_with([
            element("multifilesrc", "n1", location="/data/%05d.jpg"),
            element("emltriton", "n2", model="m"),
        ])
        assert renderer.custom_stub_node_ids(document) == []


# ---------------------------------------------------------------------------
# Task 13.3: report shape for stubbed Custom_Node_Types (12.1, 12.2)
# ---------------------------------------------------------------------------

DATASET_PREFIX = "workflows/uc-1/test-runs/run-1/dataset"


def stub_report_run(monkeypatch, document, pipeline_result=({}, None),
                    dataset_files=("a.jpg", "b.jpg", "c.jpg")):
    """Run ``harness.execute`` over ``document`` with GStreamer mocked
    (``run_gst_pipeline`` returns ``pipeline_result``); returns the exit
    code and the last flushed results document — what the test run
    report is built from."""
    monkeypatch.delenv("STAGED_MODELS", raising=False)
    monkeypatch.delenv("SIMULATED_INFERENCE", raising=False)
    monkeypatch.setattr(harness, "run_gst_pipeline",
                        lambda *args, **kwargs: pipeline_result)

    s3 = FakeS3({
        "{0}/{1}".format(DATASET_PREFIX, name): _baseline_jpeg_bytes()
        for name in dataset_files
    })
    flushed = []
    store = ResultsStore(renderer.all_node_ids(document), flushed.append)
    exit_code = harness.execute(
        s3, "bucket", RESULTS_KEY, DATASET_PREFIX, document, store)
    assert flushed, "execute() never flushed a results document"
    return exit_code, flushed[-1]


def records_by_node(report):
    return {record["nodeId"]: record for record in report["nodes"]}


class TestStubReportShape:
    """The report identifies stubbed Custom_Node_Types and describes the
    limitation that they were simulated because no x86_64 build exists
    (Requirements 12.1, 12.2; display on top of this shape is covered by
    the Workflow_Builder TestPanel tests)."""

    STUBBED_DOCUMENT = document_with([
        element("multifilesrc", "n1", location="{dataset_location}"),
        element("identity", "n2", name="custom_stub_n2"),
        element("fakesink", "n3"),
    ])

    def test_report_identifies_stubbed_custom_node_with_limitation_note(
            self, monkeypatch):
        """A stubbed Custom_Node_Type carries exactly one
        ``custom_node_stub`` stubActivity entry naming the substituted
        element and the frames passed through, with the note describing
        the limitation (simulated because no x86_64 build exists);
        executed and dataset-fed nodes carry no such entry (12.2)."""
        exit_code, report = stub_report_run(
            monkeypatch, json.loads(json.dumps(self.STUBBED_DOCUMENT)))
        assert exit_code == 0

        records = records_by_node(report)
        assert set(records) == {"n1", "n2", "n3"}

        stub_entries = [entry for entry in records["n2"]["stubActivity"]
                        if entry.get("type") == "custom_node_stub"]
        assert len(stub_entries) == 1
        entry = stub_entries[0]
        assert entry["element"] == "custom_stub_n2"
        assert entry["frameCount"] == 3
        assert entry["note"] == harness.CUSTOM_NODE_STUB_NOTE
        assert records["n2"]["status"] == "completed"

        # Only the stubbed node is identified as a custom-node stub.
        for other in ("n1", "n3"):
            assert not [entry for entry in records[other]["stubActivity"]
                        if entry.get("type") == "custom_node_stub"]

    def test_limitation_note_describes_missing_x86_64_build(self):
        """The recorded note is the report's limitation description: the
        Custom_Node_Type was simulated because no x86_64 build exists
        and a pass-through stub substituted it (12.2, wording consumed
        by the Workflow_Builder display per 12.3)."""
        note = harness.CUSTOM_NODE_STUB_NOTE
        assert "no x86_64 build" in note
        assert "pass-through" in note
        assert note.startswith("Simulated")

    def test_stub_entry_retained_when_pipeline_fails_later(self, monkeypatch):
        """The stub substitution is recorded and flushed before pipeline
        execution, so a later pipeline failure retains the stubbed-node
        identification and its limitation note in the report (12.2 with
        the 12.10 retention semantics)."""
        failure = {"element": None, "message": "pipeline exploded"}
        exit_code, report = stub_report_run(
            monkeypatch, json.loads(json.dumps(self.STUBBED_DOCUMENT)),
            pipeline_result=({}, failure))
        assert exit_code == 1

        records = records_by_node(report)
        stub_entries = [entry for entry in records["n2"]["stubActivity"]
                        if entry.get("type") == "custom_node_stub"]
        assert len(stub_entries) == 1
        assert stub_entries[0]["note"] == harness.CUSTOM_NODE_STUB_NOTE

    def test_no_custom_node_stub_entries_without_stubbed_nodes(
            self, monkeypatch):
        """Runs whose custom nodes all executed for real (or that have
        no custom nodes) record no custom_node_stub activity (12.1)."""
        document = document_with([
            element("multifilesrc", "n1", location="{dataset_location}"),
            element("videoflip", "n2", method="rotate-180"),
            element("fakesink", "n3"),
        ])
        exit_code, report = stub_report_run(monkeypatch, document)
        assert exit_code == 0
        for record in records_by_node(report).values():
            assert not [entry for entry in record["stubActivity"]
                        if entry.get("type") == "custom_node_stub"]
