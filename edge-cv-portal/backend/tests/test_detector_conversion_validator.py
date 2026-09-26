"""
``detector_conversion.validate_conversion_artifact`` -- the untrusted
Conversion_Artifact gate (detector-checkpoint-import task 2.5).

Every artifact is built here: a gzip tarball assembled member by member
(regular files, links, devices, hostile names) and ONNX ModelProtos encoded by
hand with the protobuf helpers of ``fixtures.checkpoints.builders`` -- no
``onnx`` and no ``torch``. The reader was also run against the ten real
SageMaker artifacts of the spike (docs/detector-checkpoint-import-spike.md).
# Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7
"""
import hashlib
import io
import json
import os
import tarfile

import pytest

import detector_conversion as dc
from fixtures.checkpoints.builders import _pb_ld, _pb_str, _pb_vi, _value_info

FLOAT, FLOAT16, INT64 = 1, 10, 7


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def tensor(name, *, external=False, external_entry=False, raw=b"\x00" * 64):
    t = _pb_vi(1, 16) + _pb_vi(2, FLOAT) + _pb_str(8, name)
    if external:
        t += _pb_vi(14, 1)  # data_location = EXTERNAL
    if external_entry:
        t += _pb_ld(13, _pb_str(1, "location") + _pb_str(2, "weights.bin"))
    return t + _pb_ld(9, raw)


def node(op="Conv", domain="", inputs=("images", "w0"), output="y", attributes=()):
    n = b"".join(_pb_str(1, i) for i in inputs) + _pb_str(2, output) + _pb_str(4, op)
    for attr in attributes:
        n += _pb_ld(5, attr)
    if domain:
        n += _pb_str(7, domain)
    return n


def graph_bytes(*, nodes=None, initializers=None, inputs=None, outputs=None, sparse=()):
    nodes = [node()] if nodes is None else nodes
    initializers = [tensor("w0")] if initializers is None else initializers
    inputs = [("images", [1, 3, 640, 640], FLOAT)] if inputs is None else inputs
    outputs = [("output0", [1, 8, 8400], FLOAT)] if outputs is None else outputs
    g = b"".join(_pb_ld(1, n) for n in nodes) + _pb_str(2, "main_graph")
    g += b"".join(_pb_ld(5, t) for t in initializers)
    for values, indices in sparse:
        g += _pb_ld(15, _pb_ld(1, values) + _pb_ld(2, indices))
    for name, shape, dtype in inputs:
        g += _pb_ld(11, _value_info(name, shape, dtype))
    for name, shape, dtype in outputs:
        g += _pb_ld(12, _value_info(name, shape, dtype))
    return g


def model_bytes(*, ir=8, opsets=(("", 17),), functions=0, graph=None, **graph_kwargs):
    m = _pb_vi(1, ir) + _pb_str(2, "pytorch") + _pb_str(3, "2.5.1")
    m += _pb_ld(7, graph if graph is not None else graph_bytes(**graph_kwargs))
    for domain, version in opsets:
        m += _pb_ld(8, (_pb_str(1, domain) if domain else b"") + _pb_vi(2, version))
    for i in range(functions):
        m += _pb_ld(25, _pb_str(1, f"fn{i}") + _pb_str(2, "custom.domain"))
    return m


YOLO_RECORD = {"detection": {"detection_arch": "yolo", "network_input_width": 640,
                             "network_input_height": 640, "num_classes": 4}}
RFDETR_RECORD = {"detection": {"detection_arch": "rf_detr", "network_input_width": 512,
                               "network_input_height": 512, "num_classes": 4}}
RFDETR_OUTPUTS = [("dets", [1, 300, 4], FLOAT), ("labels", [1, 300, 5], FLOAT)]


def metadata_for(onnx, record, **overrides):
    det = record["detection"]
    arch = det["detection_arch"]
    meta = {"detection_arch": arch, "num_classes": det["num_classes"],
            "onnx_sha256": hashlib.sha256(onnx).hexdigest(),
            "exporter": "ultralytics 8.4.162" if arch == "yolo" else "rfdetr 1.10.1",
            "fleet_floor": {"onnxruntime": "1.16.3", "loaded": True, "finite": True},
            "parity": {"tolerance": {}, "max_abs": {"box_max_abs": 0.0018, "score_max_abs": 2.5e-6},
                       "runtimes": {"onnxruntime 1.30.0": {}, "onnxruntime 1.16.3": {}}}}
    if arch == "yolo":
        meta.update(imgsz=det["network_input_width"], onnx_output_shape=[1, 8, 8400])
    else:
        meta.update(resolution=det["network_input_width"], top_k=300,
                    onnx_output_shapes=[[1, 300, 4], [1, 300, 5]])
    meta.update(overrides)
    return meta


def regular(name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def write_tar(path, members, mode="w:gz"):
    with tarfile.open(path, mode) as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def artifact(tmp_path, onnx=None, record=YOLO_RECORD, meta=None, extra=(), names=None):
    if onnx is None:
        onnx = model_bytes(**({"outputs": RFDETR_OUTPUTS, "inputs": [("input", [1, 3, 512, 512], FLOAT)]}
                              if record is RFDETR_RECORD else {}))
    meta = metadata_for(onnx, record) if meta is None else meta
    meta_bytes = meta if isinstance(meta, bytes) else json.dumps(meta).encode()
    onnx_name, meta_name = names or ("model.onnx", "training_metadata.json")
    members = [regular(onnx_name, onnx), regular(meta_name, meta_bytes), *extra]
    return write_tar(str(tmp_path / "model.tar.gz"), members)


def validate(tmp_path, tar_path, record=YOLO_RECORD):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return dc.validate_conversion_artifact(tar_path, record, str(work))


def rejected(tmp_path, tar_path, record=YOLO_RECORD):
    with pytest.raises(dc.ConversionValidationError) as exc:
        validate(tmp_path, tar_path, record)
    return exc.value


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------

def test_accepts_the_yolo_contract(tmp_path):
    onnx = model_bytes()
    out = validate(tmp_path, artifact(tmp_path, onnx))
    assert out.onnx_sha256 == hashlib.sha256(onnx).hexdigest()
    assert open(out.onnx_path, "rb").read() == onnx
    assert out.summary["ir_version"] == 8 and out.summary["opset"] == 17
    assert out.summary["input"] == [1, 3, 640, 640]
    assert out.summary["outputs"] == [[1, 8, 8400]] and out.summary["anchors"] == 8400
    assert out.summary["parity_max_abs"] == {"box_max_abs": 0.0018, "score_max_abs": 2.5e-6}
    assert out.summary["fleet_floor_onnxruntime"] == "1.16.3"
    assert out.summary["parity_runtimes"] == ["onnxruntime 1.16.3", "onnxruntime 1.30.0"]
    assert out.summary["exporter"] == "ultralytics 8.4.162"
    assert out.summary["onnx_bytes"] == len(onnx)


def test_accepts_channels_last_yolo(tmp_path):
    onnx = model_bytes(outputs=[("output0", [1, 8400, 8], FLOAT)])
    meta = metadata_for(onnx, YOLO_RECORD, onnx_output_shape=[1, 8400, 8])
    assert validate(tmp_path, artifact(tmp_path, onnx, meta=meta)).summary["anchors"] == 8400


def test_accepts_the_rfdetr_contract(tmp_path):
    out = validate(tmp_path, artifact(tmp_path, record=RFDETR_RECORD), RFDETR_RECORD)
    assert out.summary["outputs"] == [[1, 300, 4], [1, 300, 5]]
    assert out.summary["top_k"] == 300
    assert out.summary["output_names"] == ["dets", "labels"]


def test_accepts_dot_slash_names_and_a_root_directory_entry(tmp_path):
    onnx = model_bytes()
    root = tarfile.TarInfo(".")
    root.type = tarfile.DIRTYPE
    tar_path = write_tar(str(tmp_path / "a.tar.gz"), [
        (root, None), regular("./model.onnx", onnx),
        regular("./training_metadata.json", json.dumps(metadata_for(onnx, YOLO_RECORD)).encode())])
    assert validate(tmp_path, tar_path).summary["outputs"] == [[1, 8, 8400]]


def test_initializers_listed_as_graph_inputs_are_not_feeds(tmp_path):
    onnx = model_bytes(inputs=[("images", [1, 3, 640, 640], FLOAT), ("w0", [16], FLOAT)])
    assert validate(tmp_path, artifact(tmp_path, onnx)).summary["input"] == [1, 3, 640, 640]


def test_record_decimals_are_accepted(tmp_path):
    from decimal import Decimal
    record = {"detection": {"detection_arch": "yolo", "network_input_width": Decimal(640),
                            "num_classes": Decimal(4)}}
    assert validate(tmp_path, artifact(tmp_path), record).summary["opset"] == 17


def test_untrusted_informational_claims_are_bounded(tmp_path):
    onnx = model_bytes()
    meta = metadata_for(onnx, YOLO_RECORD, exporter="x" * 5000,
                        fleet_floor={"onnxruntime": {"nested": "object"}},
                        parity={"max_abs": {"box_max_abs": float("nan"), "score_max_abs": "1",
                                            "extra": 1}, "runtimes": {f"r{i}": 0 for i in range(50)}})
    s = validate(tmp_path, artifact(tmp_path, onnx, meta=meta)).summary
    assert len(s["exporter"]) == 120
    assert s["fleet_floor_onnxruntime"] is None
    assert s["parity_max_abs"] == {"box_max_abs": None, "score_max_abs": None}
    assert len(s["parity_runtimes"]) == 4


# ---------------------------------------------------------------------------
# Hostile tarballs (Req 8.2)
# ---------------------------------------------------------------------------

def test_symlink_member_is_rejected(tmp_path):
    link = tarfile.TarInfo("model.onnx")
    link.type, link.linkname = tarfile.SYMTYPE, "/proc/self/environ"
    tar_path = write_tar(str(tmp_path / "a.tar.gz"), [
        (link, None), regular("training_metadata.json", b"{}")])
    err = rejected(tmp_path, tar_path)
    assert err.rule == "tar-member" and "/proc/self/environ" in str(err)


def test_hard_link_member_is_rejected(tmp_path):
    onnx = model_bytes()
    link = tarfile.TarInfo("training_metadata.json")
    link.type, link.linkname = tarfile.LNKTYPE, "model.onnx"
    tar_path = write_tar(str(tmp_path / "a.tar.gz"), [regular("model.onnx", onnx), (link, None)])
    assert rejected(tmp_path, tar_path).rule == "tar-member"


@pytest.mark.parametrize("name,fragment", [
    ("../model.onnx", "'..'"), ("sub/../../model.onnx", "'..'"), ("/model.onnx", "absolute"),
    ("/etc/passwd", "absolute"),
])
def test_traversal_and_absolute_names_are_rejected(tmp_path, name, fragment):
    onnx = model_bytes()
    tar_path = artifact(tmp_path, onnx, extra=[regular(name, b"x")])
    err = rejected(tmp_path, tar_path)
    assert err.rule == "tar-member" and fragment in str(err)
    assert not os.path.exists(tmp_path / "passwd")


def test_extra_member_is_rejected(tmp_path):
    err = rejected(tmp_path, artifact(tmp_path, extra=[regular("best.pt", b"pickle")]))
    assert err.rule == "tar-member" and "unexpected member 'best.pt'" in str(err)


def test_nested_member_name_is_rejected(tmp_path):
    err = rejected(tmp_path, artifact(tmp_path, names=("export/model.onnx", "training_metadata.json")))
    assert err.rule == "tar-member"


def test_duplicate_member_is_rejected(tmp_path):
    onnx = model_bytes()
    err = rejected(tmp_path, artifact(tmp_path, onnx, extra=[regular("model.onnx", onnx)]))
    assert "duplicate member" in str(err)


def test_device_and_fifo_members_are_rejected(tmp_path):
    for kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
        dev = tarfile.TarInfo("model.onnx")
        dev.type = kind
        tar_path = write_tar(str(tmp_path / f"d{kind!r}.tar.gz"),
                             [(dev, None), regular("training_metadata.json", b"{}")])
        assert rejected(tmp_path, tar_path).rule == "tar-member"


def test_oversize_member_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "METADATA_MEMBER_CAP", 16)
    # the cap is read at call time through the default argument's module constant
    tar_path = artifact(tmp_path)
    with pytest.raises(dc.ConversionValidationError) as exc:
        dc.extract_artifact_members(tar_path, str(tmp_path), metadata_cap=16)
    assert exc.value.rule == "size" and "training_metadata.json" in str(exc.value)


def test_oversize_artifact_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "ARTIFACT_SIZE_CAP", 10)
    assert rejected(tmp_path, artifact(tmp_path)).rule == "size"


def test_missing_member_is_rejected(tmp_path):
    tar_path = write_tar(str(tmp_path / "a.tar.gz"), [regular("model.onnx", model_bytes())])
    err = rejected(tmp_path, tar_path)
    assert "missing member(s) ['training_metadata.json']" in str(err)


def test_not_a_tarball_is_rejected(tmp_path):
    path = tmp_path / "junk.tar.gz"
    path.write_bytes(b"\x1f\x8b not really gzip")
    assert rejected(tmp_path, str(path)).rule == "tar"


# ---------------------------------------------------------------------------
# Hostile / non-conforming ONNX (Req 8.3)
# ---------------------------------------------------------------------------

def test_external_data_initializer_is_rejected(tmp_path):
    onnx = model_bytes(initializers=[tensor("w0", external=True)])
    err = rejected(tmp_path, artifact(tmp_path, onnx))
    assert err.rule == "external-data" and "w0" in str(err)


def test_external_data_entry_is_rejected(tmp_path):
    onnx = model_bytes(initializers=[tensor("w0", external_entry=True)])
    assert rejected(tmp_path, artifact(tmp_path, onnx)).rule == "external-data"


def test_external_data_in_a_node_attribute_tensor_is_rejected(tmp_path):
    attr = _pb_str(1, "value") + _pb_ld(5, tensor("const0", external=True)) + _pb_vi(20, 4)
    onnx = model_bytes(nodes=[node(op="Constant", inputs=(), output="c", attributes=[attr]), node()])
    assert rejected(tmp_path, artifact(tmp_path, onnx)).rule == "external-data"


def test_external_data_in_a_sparse_initializer_is_rejected(tmp_path):
    onnx = model_bytes(sparse=[(tensor("sv", external=True), tensor("si"))])
    assert rejected(tmp_path, artifact(tmp_path, onnx)).rule == "external-data"


def test_custom_domain_op_is_rejected(tmp_path):
    onnx = model_bytes(nodes=[node(), node(op="EfficientNMS_TRT", domain="trt.plugins")],
                       opsets=(("", 17), ("trt.plugins", 1)))
    err = rejected(tmp_path, artifact(tmp_path, onnx))
    assert err.rule == "op-domain" and "trt.plugins" in str(err)


def test_custom_domain_op_inside_a_subgraph_is_rejected(tmp_path):
    inner = graph_bytes(nodes=[node(op="Evil", domain="com.attacker")], initializers=[],
                        inputs=[], outputs=[("z", [1], FLOAT)])
    then_attr = _pb_str(1, "then_branch") + _pb_ld(6, inner) + _pb_vi(20, 5)
    onnx = model_bytes(nodes=[node(op="If", inputs=("cond",), output="o", attributes=[then_attr]),
                              node()])
    err = rejected(tmp_path, artifact(tmp_path, onnx))
    assert err.rule == "op-domain" and "com.attacker" in str(err)


def test_declared_custom_opset_alone_is_rejected(tmp_path):
    onnx = model_bytes(opsets=(("", 17), ("com.microsoft", 1)))
    assert rejected(tmp_path, artifact(tmp_path, onnx)).rule == "op-domain"


def test_local_functions_are_rejected(tmp_path):
    assert rejected(tmp_path, artifact(tmp_path, model_bytes(functions=1))).rule == "op-domain"


@pytest.mark.parametrize("opset", [20, 21])
def test_opset_above_the_fleet_floor_is_rejected(tmp_path, opset):
    err = rejected(tmp_path, artifact(tmp_path, model_bytes(opsets=(("", opset),))))
    assert err.rule == "opset" and f"opset {opset} > 19" in str(err)


def test_missing_default_opset_is_rejected(tmp_path):
    assert rejected(tmp_path, artifact(tmp_path, model_bytes(opsets=()))).rule == "opset"


@pytest.mark.parametrize("ir", [10, 11])
def test_ir_version_above_the_fleet_floor_is_rejected(tmp_path, ir):
    err = rejected(tmp_path, artifact(tmp_path, model_bytes(ir=ir)))
    assert err.rule == "ir-version" and f"IR version {ir} > 9" in str(err)


@pytest.mark.parametrize("inputs,fragment", [
    ([("images", [1, 3, 320, 320], FLOAT)], "[1, 3, 320, 320]"),
    ([("images", [1, 3, 640, 640], FLOAT16)], "FLOAT16"),
    ([("images", ["batch", 3, 640, 640], FLOAT)], "'batch'"),
    ([("images", [1, 3, 640, 640], FLOAT), ("mask", [1, 1], FLOAT)], "exactly 1 graph input"),
    ([], "exactly 1 graph input"),
])
def test_wrong_input_is_rejected(tmp_path, inputs, fragment):
    err = rejected(tmp_path, artifact(tmp_path, model_bytes(inputs=inputs)))
    assert err.rule == "input" and fragment in str(err)


@pytest.mark.parametrize("outputs,fragment", [
    ([("output0", [1, 300, 6], FLOAT)], "[1, 300, 6]"),                  # embedded NMS / one-to-one
    ([("output0", [1, 100, 7], FLOAT)], "[1, 100, 7]"),                  # NMS with batch index
    ([("output0", [1, 40, 8400], FLOAT), ("output1", [1, 32, 160, 160], FLOAT)], "exactly 1 output"),
    ([("output0", [1, 40, 8400], FLOAT)], "[1, 40, 8400]"),              # seg: 4 + C + 32
    ([("output0", [1, 8, 8400], FLOAT16)], "FLOAT16"),
    ([("output0", [1, 8, "anchors"], FLOAT)], "static"),
    ([("output0", [2, 8, 8400], FLOAT)], "batch-1"),
    ([("output0", [8, 8400], FLOAT)], "rank-3"),
    ([("output0", [1, 8, 8], FLOAT)], "N > 8"),
])
def test_yolo_output_contract_is_enforced(tmp_path, outputs, fragment):
    err = rejected(tmp_path, artifact(tmp_path, model_bytes(outputs=outputs)))
    assert err.rule == "output" and fragment in str(err)


@pytest.mark.parametrize("outputs", [
    [("dets", [1, 300, 4], FLOAT), ("labels", [1, 300, 4], FLOAT)],      # head not widened (C slots)
    [("dets", [1, 300, 4], FLOAT), ("labels", [1, 200, 5], FLOAT)],      # Q disagrees
    [("dets", [1, 300, 4], FLOAT)],                                      # one output
    [("dets", [1, 300, 4], FLOAT), ("labels", [1, 300, 5], FLOAT), ("masks", [1, 300, 5], FLOAT)],
    [("dets", [1, 300, 6], FLOAT), ("labels", [1, 300, 5], FLOAT)],      # no box tensor
])
def test_rfdetr_output_contract_is_enforced(tmp_path, outputs):
    onnx = model_bytes(inputs=[("input", [1, 3, 512, 512], FLOAT)], outputs=outputs)
    assert rejected(tmp_path, artifact(tmp_path, onnx, RFDETR_RECORD), RFDETR_RECORD).rule == "output"


@pytest.mark.parametrize("blob,fragment", [
    (b"", "empty"), (b"\x08", "not a readable ONNX ModelProto"), (b"\xff" * 64, "not a readable"),
    (_pb_vi(1, 8), "no IR version / graph"),
    (_pb_ld(7, graph_bytes()), "no IR version / graph"),
    (b"PK\x03\x04" + b"\x00" * 60, "not a readable"),
])
def test_garbage_onnx_is_rejected(tmp_path, blob, fragment):
    err = rejected(tmp_path, artifact(tmp_path, blob, meta=metadata_for(blob, YOLO_RECORD)))
    assert err.rule == "onnx" and fragment in str(err)


def test_deeply_nested_subgraphs_are_rejected(tmp_path):
    inner = graph_bytes(nodes=[node()], initializers=[], inputs=[], outputs=[])
    for _ in range(20):
        attr = _pb_str(1, "body") + _pb_ld(6, inner) + _pb_vi(20, 5)
        inner = graph_bytes(nodes=[node(op="Loop", attributes=[attr])], initializers=[], inputs=[],
                            outputs=[])
    attr = _pb_str(1, "body") + _pb_ld(6, inner) + _pb_vi(20, 5)
    onnx = model_bytes(nodes=[node(op="Loop", attributes=[attr]), node()])
    assert rejected(tmp_path, artifact(tmp_path, onnx)).rule == "onnx"


# ---------------------------------------------------------------------------
# Metadata against the record and the graph (Req 8.4, 8.5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides,fragment", [
    ({"detection_arch": "rf_detr"}, "detection_arch"),
    ({"imgsz": 1280}, "imgsz 1280"),
    ({"imgsz": None}, "imgsz None"),
    ({"num_classes": 80}, "num_classes 80"),
    ({"onnx_output_shape": [1, 84, 8400]}, "onnx_output_shape"),
])
def test_metadata_must_agree_with_the_record_and_graph(tmp_path, overrides, fragment):
    onnx = model_bytes()
    err = rejected(tmp_path, artifact(tmp_path, onnx, meta=metadata_for(onnx, YOLO_RECORD, **overrides)))
    assert err.rule == "metadata" and fragment in str(err)


def test_rfdetr_metadata_must_agree(tmp_path):
    onnx = model_bytes(inputs=[("input", [1, 3, 512, 512], FLOAT)], outputs=RFDETR_OUTPUTS)
    for overrides, fragment in (({"resolution": 640}, "resolution 640"),
                                ({"top_k": 100}, "top_k 100"),
                                ({"onnx_output_shapes": [[1, 300, 4], [1, 300, 91]]}, "onnx_output_shapes")):
        meta = metadata_for(onnx, RFDETR_RECORD, **overrides)
        err = rejected(tmp_path, artifact(tmp_path, onnx, RFDETR_RECORD, meta=meta), RFDETR_RECORD)
        assert err.rule == "metadata" and fragment in str(err), overrides


def test_metadata_sha256_must_match_the_graph(tmp_path):
    onnx = model_bytes()
    meta = metadata_for(onnx, YOLO_RECORD, onnx_sha256="0" * 64)
    err = rejected(tmp_path, artifact(tmp_path, onnx, meta=meta))
    assert err.rule == "sha256" and hashlib.sha256(onnx).hexdigest() in str(err)


@pytest.mark.parametrize("meta", [b"not json", b"[1, 2]", b"\xff\xfe"])
def test_metadata_must_be_a_json_object(tmp_path, meta):
    assert rejected(tmp_path, artifact(tmp_path, meta=meta)).rule == "metadata"


def test_record_without_detection_fields_is_rejected(tmp_path):
    for record in ({}, {"detection": {"detection_arch": "yolo"}},
                   {"detection": {"detection_arch": "ssd", "network_input_width": 640,
                                  "num_classes": 4}}):
        assert rejected(tmp_path, artifact(tmp_path), record).rule == "record"


def test_class_names_come_from_the_record_not_the_metadata(tmp_path):
    onnx = model_bytes()
    meta = metadata_for(onnx, YOLO_RECORD, class_names=["x", "y", "z", "evil"])
    out = validate(tmp_path, artifact(tmp_path, onnx, meta=meta))
    assert "class_names" not in out.summary  # never promoted from the untrusted file


def test_error_message_names_rule_and_value():
    err = dc.ConversionValidationError("opset", "default-domain opset 20 > 19")
    assert str(err) == "Conversion output rejected (opset): default-domain opset 20 > 19"
    assert err.rule == "opset" and err.detail == "default-domain opset 20 > 19"
