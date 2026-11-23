"""
Smart Import of an RF-DETR-Seg ONNX graph (spec rfdetr-seg-support, Group 8:
tasks 8.1 - 8.4; Requirements 8.1 - 8.8, 15.2, 15.3, 17.4; design s8, DD6,
DD7, DD15, DD16).

`inspect_onnx_model` reports a three-output graph as RF-DETR-Seg only when it
matches the Seg_Output_Contract ([1,Q,4] + [1,Q,C+1] + [1,Q,H,W] sharing Q),
with `num_classes = S - 1`, `num_queries`, `mask_grid`, `network_input` and the
output names in graph order; anything else is `unknown`. `convert_model` admits
`model_type = instance_segmentation` (ONNX): the switch 403, the `iou_threshold`
400, the arch default / 400, the request dims against the graph's static input
/ mask grid (400), the class-names rule, the 0.5 defaults, the
Output_Mode, the legacy `segmentation + rf_detr` alias with its deprecation
note, the `segmentation`-with-Seg-graph 400, the manifest built through the
shared builder (parity per Output_Mode) and the auto-import body's record
block. `model_import.import_model` persists that block and the Output_Mode and
maps the Seg stage types. `package_onnx_component` nests `model.onnx` under the
Seg_Stage_Type without a Seg branch. `GET /models/types` lists the type only
while the switch is on.

Harness (the test_model_converter_fine_tunable.py pattern): moto S3 for the use
case bucket (source graph in, package out), the real `generate_dda_package`,
the real `inspect_onnx_model` over synthetic ONNX protobufs from
`fixtures.checkpoints.build_onnx`. RBAC, STS, the use-case lookup and the audit
log are recorders; the torch inspector is a tripwire; the import Lambda is a
recorder that answers 201. The detection ONNX and `pytorch` paths are pinned
unchanged beside the new cases (Req 8.8).

# Validates: Requirements 8.1, 8.2, 8.3, 8.5, 8.6, 8.7, 8.8, 15.2, 15.3
"""
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import zipfile
from types import SimpleNamespace

import boto3
import pytest
import yaml
from moto import mock_aws

import detection_training as dt
import model_converter as mc
import model_import as mi
from fixtures import checkpoints as fx

_FUNCTIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "functions")

REGION = "us-east-1"
ACCOUNT = "123456789012"           # the scrubbed placeholder every fixture uses
USER = {"sub": "user-seg", "email": "user-seg@example.com", "cognito:username": "user-seg"}
NAMES = ["disc", "block"]
NATIVE = 384                      # RF-DETR-Seg small: Q 100, grid 96
GRID = NATIVE // dt.RFDETR_SEG_MASK_DOWNSAMPLE
SEG_OUTPUTS = [("dets", [1, 100, 4]), ("labels", [1, 100, 3]), ("masks", [1, 100, GRID, GRID])]
SHUFFLED_OUTPUTS = [("masks", [1, 100, GRID, GRID]), ("labels", [1, 100, 3]), ("dets", [1, 100, 4])]
NAMELESS_OUTPUTS = [("", [1, 100, 3]), ("", [1, 100, GRID, GRID]), ("", [1, 100, 4])]
THREE_CLASS_OUTPUTS = [("dets", [1, 100, 4]), ("labels", [1, 100, 4]), ("masks", [1, 100, GRID, GRID])]
SYMBOLIC_OUTPUTS = [("dets", [1, 100, 4]), ("labels", [1, 100, 3]), ("masks", [1, "num_queries", "h", "w"])]
RFDETR_OUTPUTS = [("pred_boxes", [1, 300, 4]), ("pred_logits", [1, 300, 91])]
SEMANTIC_OUTPUTS = [("output", [1, 3, NATIVE, NATIVE])]
LEGACY_IMPORT_KEYS = {"usecase_id", "model_name", "model_version", "model_s3_uri", "description",
                      "fine_tunable"}
STUB_ONNX_INFO = {"type": "onnx", "architecture_hints": ["ONNX model"]}
SEG_DIMENSIONS = [312, 384, 432, 504, 624, 768]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def graph_file(path, outputs=SEG_OUTPUTS, network_input=NATIVE):
    return fx.build_onnx(path, outputs=outputs, input_shape=[1, 3, network_input, network_input])


def inspect(outputs, network_input=NATIVE, tmp=None):
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "model.onnx")
        graph_file(path, outputs, network_input)
        return mc.inspect_onnx_model(path)


class _FakeLambda:
    """Records the auto-import body and answers like a 201 import."""

    def __init__(self):
        self.payloads = []

    def invoke(self, FunctionName, InvocationType, Payload):
        event = json.loads(Payload)
        self.payloads.append(json.loads(event["body"]))
        body = json.dumps({"training_id": "tid-seg", "status": "Completed"})
        return {"Payload": io.BytesIO(json.dumps({"statusCode": 201, "body": body}).encode())}


@pytest.fixture
def h(monkeypatch, tmp_path):
    with mock_aws():
        bucket = f"uc-bucket-seg-{id(tmp_path) & 0xffff:x}"
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=bucket)
        usecase = {"usecase_id": "uc-seg", "s3_bucket": bucket,
                   "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT}:role/uc", "external_id": "ext"}
        fake_lambda = _FakeLambda()
        audits = []
        real_client = boto3.client

        def fake_boto_client(service, *args, **kwargs):
            if service == "lambda":
                return fake_lambda
            return real_client(service, *args, **kwargs)

        def tripwire(*_a, **_k):
            raise AssertionError("inspect_pytorch_model (torch) must not run on the ONNX path")

        monkeypatch.setattr(mc, "check_user_access", lambda *a, **k: True)
        monkeypatch.setattr(mc, "get_usecase_details", lambda _id: dict(usecase))
        monkeypatch.setattr(mc, "assume_usecase_role", lambda *a, **k: {"AccessKeyId": "t"})
        monkeypatch.setattr(mc, "make_usecase_s3_client", lambda _creds: s3)
        monkeypatch.setattr(mc, "inspect_pytorch_model", tripwire)
        monkeypatch.setattr(mc, "log_audit_event", lambda **k: audits.append(k))
        monkeypatch.setattr(mc.boto3, "client", fake_boto_client)
        monkeypatch.setenv("MODEL_IMPORT_FUNCTION_NAME", "import-fn")
        monkeypatch.delenv("PORTAL_RFDETR_SEG_ENABLED", raising=False)
        yield SimpleNamespace(s3=s3, bucket=bucket, usecase=usecase, lam=fake_lambda,
                              audits=audits, tmp=tmp_path)


def put_graph(h, key, outputs=SEG_OUTPUTS, network_input=NATIVE):
    path = h.tmp / os.path.basename(key)
    graph_file(str(path), outputs, network_input)
    data = path.read_bytes()
    h.s3.put_object(Bucket=h.bucket, Key=key, Body=data)
    return data


def event(body):
    return {"body": json.dumps(body), "requestContext": {"authorizer": {"claims": USER}}}


def convert(h, key, **overrides):
    body = {"usecase_id": "uc-seg", "model_s3_uri": f"s3://{h.bucket}/{key}", "model_name": "Disc Seg",
            "model_type": "instance_segmentation", "image_width": NATIVE, "image_height": NATIVE,
            "class_names": list(NAMES), "export_format": "onnx", "auto_import": True}
    body.update(overrides)
    body = {k: v for k, v in body.items() if v is not ...}
    resp = mc.convert_model(event(body), None)
    return resp["statusCode"], json.loads(resp["body"])


def keys(h):
    listing = h.s3.list_objects_v2(Bucket=h.bucket, Prefix="converted-models/")
    return sorted(o["Key"] for o in listing.get("Contents", []))


def package_members(h, result):
    """The three package members written by generate_dda_package."""
    key = result["converted_model_s3_uri"].split(f"s3://{h.bucket}/", 1)[1]
    data = h.s3.get_object(Bucket=h.bucket, Key=key)["Body"].read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
    return (json.loads(members["export_artifacts/manifest.json"]), json.loads(members["mochi.json"]),
            yaml.safe_load(members["config.yaml"]), sorted(members))


def builder_manifest(output_mode, class_names=NAMES, num_classes=2, score=0.5, mask=0.5,
                     output_names=("dets", "labels", "masks"), queries=100, native=NATIVE):
    """What packaging writes for a trained Seg record: the shared builder's
    manifest minus `dataset`, with the three real shapes on the stage."""
    m = dt.build_detection_device_manifest(
        image_width=native, image_height=native, num_classes=num_classes, class_names=list(class_names),
        score_threshold=score, preserve_aspect=False, detection_arch="rf_detr_seg", top_k=queries,
        output_mode=output_mode, mask_threshold=mask,
        output_names=list(output_names) if output_names else None)
    m.pop("dataset")
    grid = native // dt.RFDETR_SEG_MASK_DOWNSAMPLE
    stage = m["model_graph"]["stages"][0]
    stage["output_shape"] = [1, queries, num_classes + 1]
    stage["output_shapes"] = [[1, queries, 4], [1, queries, num_classes + 1], [1, queries, grid, grid]]
    return m


def record_block(output_mode="instance", score=0.5, mask=0.5, class_names=NAMES, num_classes=2,
                 top_k=100, native=NATIVE):
    return {"detection_arch": "rf_detr_seg", "num_classes": num_classes, "class_names": list(class_names),
            "score_threshold": score, "mask_threshold": mask, "top_k": top_k,
            "network_input_width": native, "network_input_height": native, "preserve_aspect": False,
            "output_mode": output_mode}


# ---------------------------------------------------------------------------
# inspect_onnx_model (Req 8.1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outputs,names", [
    (SEG_OUTPUTS, ["dets", "labels", "masks"]),
    (SHUFFLED_OUTPUTS, ["masks", "labels", "dets"]),
    (NAMELESS_OUTPUTS, None),
], ids=["stamped", "shuffled", "nameless"])
def test_three_output_contract_is_reported_as_seg(outputs, names):
    info = inspect(outputs)
    assert info["suggested_type"] == "instance_segmentation" and info["detection_arch"] == "rf_detr_seg"
    assert info["num_classes"] == 2          # S = 3 slots minus the background slot
    assert info["num_queries"] == 100 and info["mask_grid"] == [GRID, GRID]
    assert info["network_input"] == NATIVE and info["output_names"] == names
    assert info["architecture_hints"] == ["RF-DETR-Seg instance segmentation: boxes + logits + masks"]
    assert info["output_shapes"] == [list(s) for _n, s in outputs]
    assert (info["input_width"], info["input_height"], info["num_outputs"]) == (NATIVE, NATIVE, 3)


@pytest.mark.parametrize("outputs,expected_names", [
    (THREE_CLASS_OUTPUTS, ["dets", "labels", "masks"]),
    ([("labels", [1, 100, 4]), ("dets", [1, 100, 4]), ("masks", [1, 100, GRID, GRID])],
     ["labels", "dets", "masks"]),
    ([("x", [1, 100, 4]), ("labels", [1, 100, 4]), ("masks", [1, 100, GRID, GRID])], ["x", "labels", "masks"]),
    ([("dets", [1, 100, 4]), ("y", [1, 100, 4]), ("masks", [1, 100, GRID, GRID])], ["dets", "y", "masks"]),
], ids=["dets-labels", "labels-first", "labels-only", "dets-only"])
def test_three_class_graph_is_settled_by_the_output_names(outputs, expected_names):
    info = inspect(outputs)
    assert info["suggested_type"] == "instance_segmentation"
    assert info["num_classes"] == 3 and info["num_queries"] == 100
    assert info["output_names"] == expected_names


def test_three_class_graph_without_names_is_ambiguous():
    info = inspect([("", [1, 100, 4]), ("", [1, 100, 4]), ("", [1, 100, GRID, GRID])])
    assert info["suggested_type"] == "unknown"
    assert "detection_arch" not in info and "num_classes" not in info
    [hint] = info["architecture_hints"]
    assert "cannot be told apart without output names (dets / labels)" in hint


@pytest.mark.parametrize("outputs,fragment", [
    ([("a", [1, 100, 5]), ("b", [1, 100, 3]), ("c", [1, 100, GRID, GRID])], "no [1, Q, 4] boxes tensor"),
    ([("a", [1, 100, 4]), ("b", [1, 100, 3]), ("c", [1, 100, 3])], "one rank-4 mask tensor"),
    ([("a", [1, 100, 4]), ("b", [1, 300, 3]), ("c", [1, 100, GRID, GRID])], "sharing a static Q"),
    ([("a", [1, 100, 4]), ("b", [1, 100, 3]), ("c", [1, 200, GRID, GRID])], "does not share Q=100"),
    ([("a", [1, 100, 4]), ("b", [1, 100, 3]), ("c", [1, 100, 80, 80])], "contradicts network_input // 4 = 96"),
    ([("a", [1, 100, 4]), ("b", [1, 100, 3]), ("c", [1, 100, GRID, GRID]), ("d", [1, 2])],
     "4 output tensors"),
    ([("a", [1, 100, 4]), ("b", [1, 100, 1]), ("c", [1, 100, GRID, GRID])], "sharing a static Q"),
], ids=["no-boxes", "no-masks", "q-mismatch", "mask-q-mismatch", "grid-mismatch", "four-outputs",
        "one-slot"])
def test_non_matching_three_outputs_are_unknown_with_the_shapes(outputs, fragment):
    info = inspect(outputs)
    assert info["suggested_type"] == "unknown", info
    assert "detection_arch" not in info and "num_queries" not in info
    [hint] = info["architecture_hints"]
    assert fragment in hint
    assert str([list(s) for _n, s in outputs][0]) in hint or str(len(outputs)) in hint


def test_symbolic_mask_dims_of_a_foreign_export_are_accepted_with_the_inferred_grid():
    """M-1 (task 0.3): a raw rfdetr 1.10.1 export declares the mask tensor's
    non-batch dims symbolically; the two static rank-3 outputs settle the
    contract and the grid is network_input // 4."""
    info = inspect(SYMBOLIC_OUTPUTS)
    assert info["suggested_type"] == "instance_segmentation" and info["detection_arch"] == "rf_detr_seg"
    assert info["num_classes"] == 2 and info["num_queries"] == 100
    assert info["mask_grid"] == [GRID, GRID] and info["network_input"] == NATIVE
    assert info["output_shapes"][2] == [1, None, None, None]
    [hint] = info["architecture_hints"]
    assert hint.startswith("RF-DETR-Seg instance segmentation: boxes + logits + masks")
    assert "mask dims declared symbolically by the exporter; grid inferred as network_input / 4" in hint
    # Q stamped, H / W symbolic: the same acceptance.
    half = inspect([("dets", [1, 100, 4]), ("labels", [1, 100, 3]), ("masks", [1, 100, "h", "w"])])
    assert half["suggested_type"] == "instance_segmentation" and half["mask_grid"] == [GRID, GRID]
    # A dynamic input leaves nothing to infer the grid from.
    dynamic = inspect(SYMBOLIC_OUTPUTS, network_input="batch")
    assert dynamic["suggested_type"] == "unknown"
    assert "cannot be inferred" in dynamic["architecture_hints"][0]


def test_static_mask_grid_is_taken_from_the_graph_and_checked_against_the_input():
    info = inspect([("dets", [1, 200, 4]), ("labels", [1, 200, 5]), ("masks", [1, 200, 108, 108])],
                   network_input=432)
    assert info["num_queries"] == 200 and info["num_classes"] == 4 and info["mask_grid"] == [108, 108]
    assert info["network_input"] == 432


@pytest.mark.parametrize("outputs,suggested,arch,num_classes,hint", [
    (RFDETR_OUTPUTS, "object_detection", "rf_detr", 91,
     "RF-DETR-style detection: 2 output tensors (boxes + logits), NMS-free."),
    ([("output0", [1, 5, 8400])], "object_detection", "yolo", 1,
     "YOLO-style detection: single output tensor, NMS decode."),
    ([("logits", [1, 7])], "classification", None, 7, "Classification: single [batch, num_classes] output."),
], ids=["rf_detr", "yolo", "classification"])
def test_one_and_two_output_heuristics_are_unchanged(outputs, suggested, arch, num_classes, hint):
    info = inspect(outputs, network_input=640)
    assert info["suggested_type"] == suggested and info.get("detection_arch") == arch
    assert info["num_classes"] == num_classes and info["architecture_hints"] == [hint]
    for key in ("num_queries", "mask_grid", "network_input", "output_names"):
        assert key not in info


def test_model_types_gain_instance_segmentation_and_keep_segmentation():
    assert mc.MODEL_TYPES["instance_segmentation"] == {
        "description": "RF-DETR-Seg instance segmentation",
        "output_format": "[1,Q,4] + [1,Q,C+1] + [1,Q,H,W]"}
    assert mc.MODEL_TYPES["segmentation"] == {"description": "Semantic segmentation",
                                              "output_format": "[batch, num_classes, height, width]"}
    assert list(mc.MODEL_TYPES) == ["classification", "object_detection", "segmentation",
                                    "anomaly_detection", "instance_segmentation"]


# ---------------------------------------------------------------------------
# inspect_model_endpoint and GET /models/types under the Seg_Switch (Req 15.2)
# ---------------------------------------------------------------------------

def inspect_endpoint(h, key):
    resp = mc.inspect_model_endpoint(event({"usecase_id": "uc-seg", "model_s3_uri": f"s3://{h.bucket}/{key}"}),
                                     None)
    return resp["statusCode"], json.loads(resp["body"])


def test_inspect_endpoint_reports_seg_and_the_disabled_reason_only_when_off(h, monkeypatch):
    put_graph(h, "uploads/seg.onnx")
    status, body = inspect_endpoint(h, "uploads/seg.onnx")
    assert status == 200
    info = body["inspection_result"]
    assert info["suggested_type"] == "instance_segmentation" and "disabled_reason" not in info
    assert info["architecture_hints"] == ["RF-DETR-Seg instance segmentation: boxes + logits + masks"]
    assert "instance_segmentation" in body["supported_model_types"]

    monkeypatch.setenv("PORTAL_RFDETR_SEG_ENABLED", "false")
    status, body = inspect_endpoint(h, "uploads/seg.onnx")
    assert status == 200
    info = body["inspection_result"]
    assert info["suggested_type"] == "instance_segmentation"      # kept (C4)
    assert info["disabled_reason"] == "RF-DETR-Seg is disabled on this portal"
    assert info["architecture_hints"] == ["RF-DETR-Seg instance segmentation: boxes + logits + masks",
                                          "RF-DETR-Seg is disabled on this portal"]
    # A detection graph never sees the switch.
    put_graph(h, "uploads/det.onnx", RFDETR_OUTPUTS, 640)
    status, body = inspect_endpoint(h, "uploads/det.onnx")
    assert status == 200 and "disabled_reason" not in body["inspection_result"]
    assert body["inspection_result"]["suggested_type"] == "object_detection"


def test_supported_types_list_instance_segmentation_only_while_on(monkeypatch):
    monkeypatch.delenv("PORTAL_RFDETR_SEG_ENABLED", raising=False)
    on = json.loads(mc.get_supported_types({}, None)["body"])
    assert on["model_types"] == mc.MODEL_TYPES and "instance_segmentation" in on["model_types"]
    assert on["common_dimensions"]["instance_segmentation"] == SEG_DIMENSIONS
    assert SEG_DIMENSIONS == sorted(dt.RFDETR_SEG_SIZES.values())

    monkeypatch.setenv("PORTAL_RFDETR_SEG_ENABLED", "false")
    off = json.loads(mc.get_supported_types({}, None)["body"])
    assert "instance_segmentation" not in off["model_types"]
    assert list(off["model_types"]) == ["classification", "object_detection", "segmentation", "anomaly_detection"]
    for state in (on, off):
        for name in ("classification", "object_detection", "segmentation", "anomaly_detection"):
            assert state["model_types"][name] == mc.MODEL_TYPES[name]
        assert state["common_dimensions"]["classification"] == [224, 256, 299, 384, 512]
        assert state["common_dimensions"]["object_detection"] == [320, 416, 512, 640, 1280]
        assert state["common_dimensions"]["segmentation"] == [256, 512, 768, 1024]
        assert state["common_dimensions"]["anomaly_detection"] == [224, 256, 512]
        assert state["supported_frameworks"] == ["PYTORCH"]
        assert state["framework_versions"] == ["1.8", "1.9", "1.10", "1.11", "1.12", "1.13", "2.0"]


# ---------------------------------------------------------------------------
# convert_model: the Seg import (Req 8.2, 8.3, 8.7)
# ---------------------------------------------------------------------------

def test_seg_import_defaults_and_payload(h):
    put_graph(h, "uploads/seg.onnx")
    status, result = convert(h, "uploads/seg.onnx")
    assert status == 200, result
    assert result["model_type"] == "instance_segmentation" and result["output_mode"] == "instance"
    assert "deprecation" not in result and result["fine_tunable"] is None
    assert result["model_info"]["suggested_type"] == "instance_segmentation"
    assert result["model_info"]["num_queries"] == 100
    assert result["input_shape"] == [1, 3, NATIVE, NATIVE]

    manifest, mochi, config, members = package_members(h, result)
    assert members == ["config.yaml", "export_artifacts/manifest.json", "export_artifacts/model.onnx",
                       "mochi.json"]
    assert manifest == builder_manifest("instance")
    assert json.dumps(manifest) == json.dumps(builder_manifest("instance"))
    det = manifest["detection"]
    assert (det["score_threshold"], det["mask_threshold"], det["output_mode"]) == (0.5, 0.5, "instance")
    assert det["class_names"] == NAMES and det["output_names"] == ["dets", "labels", "masks"]
    assert det["top_k"] == 100 and det["preserve_aspect"] is False and "iou_threshold" not in det
    assert manifest["model_graph"]["stages"][0]["type"] == "rf_detr_instance_segmentation"
    assert manifest["model_graph"]["pixel_level_classes"]["names"] == ["background", "disc", "block"]
    assert mochi["stages"][0] == {"type": "rf_detr_instance_segmentation", "input_shape": [1, 3, NATIVE, NATIVE],
                                  "output_shape": [1, 100, 3], "num_classes": 2}
    assert config == {"dataset": {"image_width": NATIVE, "image_height": NATIVE, "num_classes": 2,
                                  "class_names": NAMES}}

    [payload] = h.lam.payloads
    assert set(payload) == LEGACY_IMPORT_KEYS | {"model_type", "detection"}
    assert payload["model_type"] == "instance_segmentation" and payload["fine_tunable"] is None
    assert payload["detection"] == record_block("instance")
    assert payload["model_s3_uri"] == result["converted_model_s3_uri"]
    assert h.audits[-1]["details"]["model_type"] == "instance_segmentation"
    assert h.audits[-1]["details"]["preserve_aspect"] is False


def test_seg_import_semantic_mode_and_explicit_thresholds(h):
    put_graph(h, "uploads/seg.onnx")
    status, result = convert(h, "uploads/seg.onnx", output_mode="semantic", score_threshold=0.3,
                             mask_threshold=0.4, num_classes=2)
    assert status == 200, result
    assert result["output_mode"] == "semantic"
    manifest, mochi, _config, _members = package_members(h, result)
    assert manifest == builder_manifest("semantic", score=0.3, mask=0.4)
    assert json.dumps(manifest) == json.dumps(builder_manifest("semantic", score=0.3, mask=0.4))
    assert manifest["model_graph"]["stages"][0]["type"] == "rf_detr_semantic_segmentation"
    assert "task" not in manifest and manifest["detection"]["output_mode"] == "semantic"
    assert mochi["stages"][0]["type"] == "rf_detr_semantic_segmentation"
    [payload] = h.lam.payloads
    assert payload["detection"] == record_block("semantic", score=0.3, mask=0.4)


@pytest.mark.parametrize("output_mode", ["instance", "semantic"])
@pytest.mark.parametrize("size,native", sorted(dt.RFDETR_SEG_SIZES.items(), key=lambda kv: kv[1]))
def test_seg_import_manifest_equals_the_shared_builder_for_each_mode_and_size(h, output_mode, size, native):
    queries = dt.RFDETR_SEG_QUERIES_FOR_SIZE[size]
    grid = native // dt.RFDETR_SEG_MASK_DOWNSAMPLE
    outputs = [("dets", [1, queries, 4]), ("labels", [1, queries, 3]), ("masks", [1, queries, grid, grid])]
    put_graph(h, "uploads/seg.onnx", outputs, native)
    status, result = convert(h, "uploads/seg.onnx", output_mode=output_mode, image_width=native,
                             image_height=native)
    assert status == 200, result
    manifest = package_members(h, result)[0]
    expected = builder_manifest(output_mode, queries=queries, native=native)
    assert json.dumps(manifest) == json.dumps(expected)
    assert manifest["model_graph"]["stages"][0]["output_shapes"][2] == [1, queries, grid, grid]
    assert h.lam.payloads[0]["detection"]["top_k"] == queries


def test_seg_import_nameless_graph_writes_no_output_names(h):
    put_graph(h, "uploads/seg.onnx", NAMELESS_OUTPUTS)
    status, result = convert(h, "uploads/seg.onnx")
    assert status == 200, result
    manifest = package_members(h, result)[0]
    assert "output_names" not in manifest["detection"]
    assert manifest == builder_manifest("instance", output_names=None)


def test_seg_import_over_a_symbolic_mask_graph_uses_the_inferred_grid(h):
    put_graph(h, "uploads/seg.onnx", SYMBOLIC_OUTPUTS)
    status, result = convert(h, "uploads/seg.onnx")
    assert status == 200, result
    manifest = package_members(h, result)[0]
    assert manifest["model_graph"]["stages"][0]["output_shapes"] == [[1, 100, 4], [1, 100, 3], [1, 100, GRID, GRID]]
    assert manifest == builder_manifest("instance")


def test_seg_import_num_classes_defaults_to_the_graphs_slots_minus_one(h):
    put_graph(h, "uploads/seg.onnx", THREE_CLASS_OUTPUTS)
    status, result = convert(h, "uploads/seg.onnx", class_names=["a", "b", "c"])
    assert status == 200, result
    manifest = package_members(h, result)[0]
    assert manifest["detection"]["num_classes"] == 3
    assert manifest["model_graph"]["pixel_level_classes"]["names"] == ["background", "a", "b", "c"]
    assert h.lam.payloads[0]["detection"]["num_classes"] == 3
    # The user may override to the full slot count for a foreign graph.
    put_graph(h, "uploads/seg4.onnx", THREE_CLASS_OUTPUTS)
    status, result = convert(h, "uploads/seg4.onnx", num_classes=4, class_names=["a", "b", "c", "d"])
    assert status == 200, result
    assert package_members(h, result)[0]["detection"]["num_classes"] == 4


@pytest.mark.parametrize("overrides,fragment", [
    ({"class_names": ...}, "class_names must list 2 names"),
    ({"class_names": None}, "class_names must list 2 names"),
    ({"class_names": []}, "class_names must list 2 names"),
    ({"class_names": ["only_one"]}, "class_names must list 2 names"),
    ({"class_names": ["a", "b", "c"]}, "class_names must list 2 names"),
    ({"class_names": ["a", ""]}, "class_names must list 2 names"),
    ({"class_names": "disc,block"}, "class_names must list 2 names"),
    ({"num_classes": 3}, "class_names must list 3 names"),
], ids=["absent", "null", "empty", "too-few", "too-many", "blank-name", "string", "override-mismatch"])
def test_seg_class_names_are_required_with_num_classes_entries(h, overrides, fragment):
    put_graph(h, "uploads/seg.onnx")
    status, body = convert(h, "uploads/seg.onnx", **overrides)
    assert status == 400, body
    assert body["error"].startswith(fragment)
    assert keys(h) == [] and h.lam.payloads == []


@pytest.mark.parametrize("overrides,fragment", [
    ({"iou_threshold": 0.45}, "'iou_threshold' does not apply to RF-DETR-Seg"),
    ({"detection_arch": "rf_detr"}, "model_type instance_segmentation requires detection_arch rf_detr_seg"),
    ({"detection_arch": "yolo"}, "model_type instance_segmentation requires detection_arch rf_detr_seg"),
    ({"output_mode": "overlay"}, "Invalid output_mode 'overlay'"),
    ({"mask_threshold": 1.5}, "'mask_threshold' must be strictly between 0 and 1"),
    ({"mask_threshold": 0}, "'mask_threshold' must be strictly between 0 and 1"),
    ({"score_threshold": 1.0}, "'score_threshold' must be strictly between 0 and 1"),
    ({"preserve_aspect": True}, "'preserve_aspect' must be false for RF-DETR-Seg"),
], ids=["iou", "arch-rf_detr", "arch-yolo", "output_mode", "mask-high", "mask-zero", "score", "letterbox"])
def test_seg_request_field_rules_are_400_before_any_download(h, overrides, fragment):
    put_graph(h, "uploads/seg.onnx")
    status, body = convert(h, "uploads/seg.onnx", **overrides)
    assert status == 400, body
    assert body["error"].startswith(fragment)
    assert keys(h) == [] and h.lam.payloads == [] and h.audits == []


def test_seg_request_accepts_the_arch_explicitly_and_defaults_it_when_absent(h):
    put_graph(h, "uploads/seg.onnx")
    status, result = convert(h, "uploads/seg.onnx", detection_arch="rf_detr_seg")
    assert status == 200, result
    assert h.lam.payloads[-1]["detection"]["detection_arch"] == "rf_detr_seg"
    status, result = convert(h, "uploads/seg.onnx", detection_arch=None, mask_threshold=None, output_mode=None)
    assert status == 200, result        # explicit nulls read as "not given"
    assert result["output_mode"] == "instance" and h.lam.payloads[-1]["detection"]["mask_threshold"] == 0.5


def test_rf_detr_seg_arch_with_another_model_type_is_400(h):
    put_graph(h, "uploads/det.onnx", RFDETR_OUTPUTS, 640)
    status, body = convert(h, "uploads/det.onnx", model_type="object_detection", detection_arch="rf_detr_seg",
                           image_width=640, image_height=640)
    assert status == 400, body
    assert body["error"].startswith("detection_arch rf_detr_seg requires model_type instance_segmentation")
    assert keys(h) == []


def test_seg_type_over_a_non_seg_graph_is_400_quoting_the_shapes(h):
    put_graph(h, "uploads/det.onnx", RFDETR_OUTPUTS, 640)
    status, body = convert(h, "uploads/det.onnx", image_width=640, image_height=640)
    assert status == 400, body
    assert body["error"].startswith("instance_segmentation needs an RF-DETR-Seg graph")
    assert "[[1, 300, 4], [1, 300, 91]]" in body["error"]
    assert keys(h) == [] and h.lam.payloads == []
    # An ambiguous three-class graph is refused the same way (inspect says unknown).
    put_graph(h, "uploads/amb.onnx", [("", [1, 100, 4]), ("", [1, 100, 4]), ("", [1, 100, GRID, GRID])])
    status, body = convert(h, "uploads/amb.onnx", class_names=["a", "b", "c"])
    assert status == 400 and body["error"].startswith("instance_segmentation needs an RF-DETR-Seg graph")


@pytest.mark.parametrize("width,height", [(640, 640), (768, 768), (NATIVE, 432)],
                         ids=["640", "768", "height-only"])
def test_seg_request_dims_must_equal_the_static_graph_input(h, width, height):
    """design 'Validation of external inputs': image_width / image_height must
    equal the graph input. The package would otherwise carry the request as
    the stage input -- a tensor the static graph refuses on every frame -- and
    a mask grid derived from it that the real mask tensor contradicts."""
    put_graph(h, "uploads/seg.onnx")
    status, body = convert(h, "uploads/seg.onnx", image_width=width, image_height=height)
    assert status == 400, body
    assert body["error"] == (f"image_width/image_height {width}x{height} must equal the graph input "
                             f"{NATIVE}x{NATIVE}")
    assert keys(h) == [] and h.lam.payloads == []
    # the same request over the symbolic-mask export: the static input decides
    put_graph(h, "uploads/sym.onnx", SYMBOLIC_OUTPUTS)
    status, body = convert(h, "uploads/sym.onnx", image_width=width, image_height=height)
    assert status == 400 and body["error"].startswith(f"image_width/image_height {width}x{height} must equal")
    assert keys(h) == [] and h.lam.payloads == []


def test_seg_request_dims_against_a_dynamic_input_graph_follow_the_static_mask_grid(h):
    """A graph with a dynamic input declares its geometry only through the
    static mask grid: the request dims // 4 must equal it (400 otherwise);
    with agreeing dims the manifest takes the request as the stage input and
    the graph's grid as the mask shape."""
    put_graph(h, "uploads/dyn.onnx", SEG_OUTPUTS, network_input="height")
    status, body = convert(h, "uploads/dyn.onnx", image_width=640, image_height=640)
    assert status == 400, body
    assert body["error"] == (f"image_width/image_height 640x640 imply a mask grid [160, 160]; "
                             f"the graph declares [{GRID}, {GRID}]")
    assert keys(h) == [] and h.lam.payloads == []
    status, result = convert(h, "uploads/dyn.onnx")
    assert status == 200, result
    assert result["model_info"]["input_shapes"] == [[1, 3, None, None]]
    assert result["model_info"]["network_input"] is None and result["model_info"]["mask_grid"] == [GRID, GRID]
    manifest = package_members(h, result)[0]
    assert manifest == builder_manifest("instance")
    assert manifest["model_graph"]["stages"][0]["input_shape"] == [1, 3, NATIVE, NATIVE]
    assert manifest["model_graph"]["stages"][0]["output_shapes"][2] == [1, 100, GRID, GRID]
    assert manifest["detection"]["network_input"] == NATIVE
    assert h.lam.payloads[-1]["detection"]["network_input_width"] == NATIVE


def test_seg_convert_is_403_while_the_switch_is_off(h, monkeypatch):
    put_graph(h, "uploads/seg.onnx")
    monkeypatch.setenv("PORTAL_RFDETR_SEG_ENABLED", "false")
    status, body = convert(h, "uploads/seg.onnx")
    assert status == 403 and body == {"error": "RF-DETR-Seg is disabled on this portal"}
    # the alias is a Seg request too (Req 15.2)
    status, body = convert(h, "uploads/seg.onnx", model_type="segmentation", detection_arch="rf_detr")
    assert status == 403 and body == {"error": "RF-DETR-Seg is disabled on this portal"}
    assert keys(h) == [] and h.lam.payloads == []
    # a detection import is unaffected
    put_graph(h, "uploads/det.onnx", RFDETR_OUTPUTS, 640)
    status, result = convert(h, "uploads/det.onnx", model_type="object_detection", detection_arch="rf_detr",
                             image_width=640, image_height=640)
    assert status == 200, result


# ---------------------------------------------------------------------------
# The legacy alias and the two `segmentation` paths (Req 8.5)
# ---------------------------------------------------------------------------

def test_legacy_segmentation_rf_detr_alias_is_rewritten_with_a_deprecation_note(h):
    put_graph(h, "uploads/seg.onnx")
    status, result = convert(h, "uploads/seg.onnx", model_type="segmentation", detection_arch="rf_detr")
    assert status == 200, result
    assert result["model_type"] == "instance_segmentation" and result["output_mode"] == "semantic"
    assert result["deprecation"] == ("model_type=segmentation+detection_arch=rf_detr is an alias of "
                                     "instance_segmentation; send instance_segmentation with output_mode")
    manifest, mochi, _config, _members = package_members(h, result)
    assert manifest["model_graph"]["stages"][0]["type"] == "rf_detr_semantic_segmentation"
    assert mochi["stages"][0]["type"] == "rf_detr_semantic_segmentation"
    assert manifest == builder_manifest("semantic")
    [payload] = h.lam.payloads
    assert payload["model_type"] == "instance_segmentation"
    assert payload["detection"] == record_block("semantic")
    assert h.audits[-1]["details"]["model_type"] == "instance_segmentation"


def test_legacy_alias_keeps_the_seg_rules(h):
    put_graph(h, "uploads/seg.onnx")
    status, body = convert(h, "uploads/seg.onnx", model_type="segmentation", detection_arch="rf_detr",
                           class_names=None)
    assert status == 400 and body["error"].startswith("class_names must list 2 names")
    status, body = convert(h, "uploads/seg.onnx", model_type="segmentation", detection_arch="rf_detr",
                           iou_threshold=0.5)
    assert status == 400 and body["error"].startswith("'iou_threshold' does not apply to RF-DETR-Seg")
    status, body = convert(h, "uploads/seg.onnx", model_type="segmentation", detection_arch="rf_detr",
                           image_width=640, image_height=640)
    assert status == 400 and body["error"] == (f"image_width/image_height 640x640 must equal the graph "
                                               f"input {NATIVE}x{NATIVE}")
    assert keys(h) == [] and h.lam.payloads == []


def test_generic_segmentation_import_is_untouched(h):
    put_graph(h, "uploads/sem.onnx", SEMANTIC_OUTPUTS)
    status, result = convert(h, "uploads/sem.onnx", model_type="segmentation", class_names=None,
                             num_classes=3)
    assert status == 200, result
    assert result["model_type"] == "segmentation"
    assert "output_mode" not in result and "deprecation" not in result
    manifest, mochi, config, members = package_members(h, result)
    stage = manifest["model_graph"]["stages"][0]
    assert stage == {"type": "segmentation", "input_shape": [1, 3, NATIVE, NATIVE],
                     "output_shape": [1, 3, NATIVE, NATIVE], "image_width": NATIVE, "image_height": NATIVE,
                     "image_range_scale": True, "normalize": False, "threshold": 0.25, "num_classes": 3}
    assert "detection" not in manifest and "task" not in manifest
    assert "pixel_level_classes" not in manifest["model_graph"]
    assert mochi["stages"][0]["type"] == "segmentation"
    assert config == {"dataset": {"image_width": NATIVE, "image_height": NATIVE, "num_classes": 3}}
    [payload] = h.lam.payloads
    assert set(payload) == LEGACY_IMPORT_KEYS
    # the generic path still takes a detection_arch and ignores it (yolo default today)
    status, result = convert(h, "uploads/sem.onnx", model_type="segmentation", class_names=None,
                             num_classes=3, detection_arch="yolo")
    assert status == 200 and set(h.lam.payloads[-1]) == LEGACY_IMPORT_KEYS


def test_generic_segmentation_request_over_a_seg_graph_is_400(h):
    put_graph(h, "uploads/seg.onnx")
    for arch in (..., "yolo"):
        status, body = convert(h, "uploads/seg.onnx", model_type="segmentation", detection_arch=arch,
                               class_names=None)
        assert status == 400, body
        assert body["error"] == "this is an RF-DETR-Seg graph; import it as model_type instance_segmentation"
    assert keys(h) == [] and h.lam.payloads == []


# ---------------------------------------------------------------------------
# Preservation of the other ONNX / pytorch paths (Req 8.8, 17.1)
# ---------------------------------------------------------------------------

def test_detection_onnx_import_keeps_the_stub_model_info_and_payload(h):
    put_graph(h, "uploads/det.onnx", RFDETR_OUTPUTS, 640)
    status, result = convert(h, "uploads/det.onnx", model_type="object_detection", detection_arch="rf_detr",
                             image_width=640, image_height=640, class_names=["x"], num_classes=1,
                             iou_threshold=0.45, output_mode="semantic", mask_threshold=0.9)
    assert status == 200, result
    # DD15: the detection path never inspects the graph and ignores the Seg
    # fields exactly as it ignores iou_threshold for RF-DETR today.
    assert result["model_info"] == STUB_ONNX_INFO
    assert "output_mode" not in result and "deprecation" not in result
    manifest = package_members(h, result)[0]
    assert manifest["detection"] == {"layout": "rf_detr", "num_classes": 1, "score_threshold": 0.25,
                                     "network_input": 640, "preserve_aspect": False, "top_k": 300,
                                     "class_names": ["x"]}
    [payload] = h.lam.payloads
    assert set(payload) == LEGACY_IMPORT_KEYS


def test_seg_pth_asked_for_as_onnx_dispatches_to_convert_checkpoint_after_the_gates(h, monkeypatch):
    """Task 7.5's note: the MODEL_TYPES gate now admits instance_segmentation,
    so a Seg .pth with export_format onnx reaches convert_checkpoint (the
    Conversion_Job path, tested in test_model_converter_checkpoint_conversion)
    once the Seg request gates pass; its own vocabulary is
    validate_conversion_request's."""
    seen = []

    def fake_convert_checkpoint(body, user, usecase, credentials, s3_client, bucket, key, uri):
        seen.append((body, key))
        return mc.create_response(200, {"training_id": "tid", "status": "InProgress"})

    monkeypatch.setattr(mc, "convert_checkpoint", fake_convert_checkpoint)
    h.s3.put_object(Bucket=h.bucket, Key="uploads/seg.pth", Body=b"not read here")
    status, result = convert(h, "uploads/seg.pth", image_width=312, image_height=312, class_names=None)
    assert status == 200 and result["status"] == "InProgress"
    assert seen[0][1] == "uploads/seg.pth" and "detection_arch" not in seen[0][0]
    status, body = convert(h, "uploads/seg.pth", iou_threshold=0.5)
    assert status == 400 and body["error"].startswith("'iou_threshold' does not apply")
    monkeypatch.setenv("PORTAL_RFDETR_SEG_ENABLED", "false")
    status, body = convert(h, "uploads/seg.pth")
    assert status == 403
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# model_import.import_model: the record (Req 8.3, 8.5; task 8.3)
# ---------------------------------------------------------------------------

class _Table:
    def __init__(self):
        self.items = []

    def put_item(self, Item):
        self.items.append(Item)


@pytest.fixture
def import_harness(monkeypatch):
    table = _Table()
    state = {"stage_type": "rf_detr_instance_segmentation"}

    def validation_result(uri, usecase, *, purpose):
        return {"valid": True, "metadata": {
            "image_width": NATIVE, "image_height": NATIVE, "input_shape": [1, 3, NATIVE, NATIVE],
            "model_type": state["stage_type"], "pt_file": "model.onnx", "model_file": "model.onnx",
            "framework": "ONNX", "framework_version": "onnx"}}

    monkeypatch.setattr(mi, "check_user_access", lambda *a, **k: True)
    monkeypatch.setattr(mi, "get_usecase_details", lambda _id: {
        "usecase_id": "uc-seg", "s3_bucket": "uc-bucket-seg",
        "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT}:role/uc", "external_id": "ext"})
    monkeypatch.setattr(mi, "assert_bucket_allowed", lambda *a, **k: None)
    monkeypatch.setattr(mi, "validate_model_artifact", validation_result)
    monkeypatch.setattr(mi, "log_audit_event", lambda **k: None)
    monkeypatch.setattr(mi, "dynamodb", SimpleNamespace(Table=lambda _name: table))
    monkeypatch.setattr(mi, "TRAINING_JOBS_TABLE", "test-training-jobs")
    return SimpleNamespace(table=table, state=state)


def import_model(extra=None):
    body = {"usecase_id": "uc-seg", "model_name": "Disc Seg", "model_version": "1.0.0",
            "model_s3_uri": "s3://uc-bucket-seg/converted-models/disc_seg-0badcafe.tar.gz",
            "description": "auto-converted", "fine_tunable": None}
    body.update(extra or {})
    resp = mi.import_model(event(body), None)
    return resp["statusCode"], json.loads(resp["body"])


@pytest.mark.parametrize("output_mode,stage_type", [("instance", "rf_detr_instance_segmentation"),
                                                    ("semantic", "rf_detr_semantic_segmentation")])
def test_import_persists_the_seg_record_block_and_output_mode(import_harness, output_mode, stage_type):
    import_harness.state["stage_type"] = stage_type
    block = record_block(output_mode)
    status, body = import_model({"model_type": "instance_segmentation", "detection": block})
    assert status == 201, body
    [item] = import_harness.table.items
    assert item["model_type"] == "instance_segmentation" and item["source"] == "imported"
    assert item["detection"] == mi._to_dynamo_compatible(block)
    assert item["detection"]["output_mode"] == output_mode
    assert item["detection"]["detection_arch"] == "rf_detr_seg" and item["detection"]["top_k"] == 100
    assert item["metadata"]["fine_tunable"] is None
    assert item["metadata"]["model_type"] == stage_type    # the validated artifact's own type
    assert "runtime" not in item                            # routed by is_onnx_import, as today
    assert "detection" not in body["validation_result"]["metadata"]


@pytest.mark.parametrize("stage_type", ["rf_detr_instance_segmentation", "rf_detr_semantic_segmentation",
                                        "instance_segmentation"])
def test_import_maps_the_seg_stage_types_without_an_override(import_harness, stage_type):
    import_harness.state["stage_type"] = stage_type
    status, body = import_model()
    assert status == 201, body
    [item] = import_harness.table.items
    assert item["model_type"] == "instance_segmentation"
    assert "detection" not in item


@pytest.mark.parametrize("stage_type,expected", [("segmentation", "segmentation"),
                                                 ("anomaly_detection", "classification"),
                                                 ("classification", "classification"),
                                                 ("yolo_object_detection", "yolo_object_detection")])
def test_import_mapping_of_the_existing_types_is_unchanged(import_harness, stage_type, expected):
    import_harness.state["stage_type"] = stage_type
    status, body = import_model()
    assert status == 201, body
    [item] = import_harness.table.items
    assert item["model_type"] == expected and "detection" not in item


@pytest.mark.parametrize("extra,needle", [
    ({"detection": "rf_detr_seg"}, "detection"),
    ({"detection": ["rf_detr_seg"]}, "detection"),
    ({"model_type": 7}, "model_type"),
    ({"model_type": ""}, "model_type"),
], ids=["detection-str", "detection-list", "model_type-int", "model_type-empty"])
def test_import_rejects_a_malformed_block_or_override(import_harness, extra, needle):
    status, body = import_model(extra)
    assert status == 400, body
    assert needle in body["error"]
    assert import_harness.table.items == []


# ---------------------------------------------------------------------------
# package_onnx_component nests model.onnx under the Seg_Stage_Type (Req 8.6)
# ---------------------------------------------------------------------------

def _load_packaging():
    spec = importlib.util.spec_from_file_location(
        "portal_packaging_seg_import", os.path.join(_FUNCTIONS_DIR, "packaging.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_packaging_seg_import"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("output_mode,stage_dir", [("instance", "rf_detr_instance_segmentation"),
                                                   ("semantic", "rf_detr_semantic_segmentation")])
def test_package_onnx_component_nests_the_import_under_the_seg_stage_type(h, output_mode, stage_dir):
    put_graph(h, "uploads/seg.onnx")
    status, result = convert(h, "uploads/seg.onnx", output_mode=output_mode)
    assert status == 200, result
    packaging = _load_packaging()
    s3_uri = packaging.package_onnx_component(result["converted_model_s3_uri"], h.s3, h.usecase)
    key = s3_uri.split(f"s3://{h.bucket}/", 1)[1]
    assert key.startswith("model_artifacts/model-") and key.endswith("_greengrass_model_component.zip")
    data = h.s3.get_object(Bucket=h.bucket, Key=key)["Body"].read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = sorted(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
    assert names == ["manifest.json", f"{stage_dir}/model.onnx"]
    assert manifest["model_graph"]["stages"][0]["type"] == stage_dir
    assert manifest["dataset"] == {"image_width": NATIVE, "image_height": NATIVE}   # merged from config.yaml
    assert manifest["detection"]["output_mode"] == output_mode
    expected = builder_manifest(output_mode)
    expected["dataset"] = {"image_width": NATIVE, "image_height": NATIVE}
    assert manifest == expected
