"""
``onnx_fleet_ir.normalize_fleet_ir_version``, the fleet-floor IR header fix
for trained detection graphs.

onnxruntime 1.16.3 runs on the JP5 GPU build and the CPU / x86 images, and it
loads IR <= 9 only. The portal YOLO trainer's onnxslim re-serialization
stamps IR 10 on an opset-17 graph. The fix lowers only the header varint, and
only when nothing in the graph needs the newer IR. Every ModelProto here is
hand-encoded with the protobuf helpers of ``fixtures.checkpoints.builders``.
The real trainer artifact (blue-plate-yolo-ft-v3, IR 10) was also checked
against onnxruntime 1.16.3 (docs/detector-checkpoint-import-spike.md).
"""
import pytest
from hypothesis import given, settings, strategies as st

import detector_conversion as dc
import onnx_fleet_ir as fir
from fixtures.checkpoints.builders import _pb_ld, _pb_str, _pb_vi, _value_info

FLOAT, FLOAT8E4M3FN, UINT4, INT4, FLOAT4E2M1 = 1, 17, 21, 22, 23


def tensor(name, dtype=FLOAT):
    return _pb_vi(1, 4) + _pb_vi(2, dtype) + _pb_str(8, name) + _pb_ld(9, b"\x00" * 16)


def graph(initializers=(tensor("w0"),), outputs=(("output0", [1, 8, 8400], FLOAT),),
          value_infos=(), nodes=None):
    nodes = [_pb_str(1, "images") + _pb_str(1, "w0") + _pb_str(2, "output0") + _pb_str(4, "Conv")] \
        if nodes is None else nodes
    g = b"".join(_pb_ld(1, n) for n in nodes) + _pb_str(2, "main_graph")
    g += b"".join(_pb_ld(5, t) for t in initializers)
    g += _pb_ld(11, _value_info("images", [1, 3, 640, 640]))
    for name, shape, dtype in outputs:
        g += _pb_ld(12, _value_info(name, shape, dtype))
    for name, shape, dtype in value_infos:
        g += _pb_ld(13, _value_info(name, shape, dtype))
    return g


def model(ir=10, opset=17, functions=0, header_first=True, **graph_kwargs):
    ir_field = _pb_vi(1, ir)
    body = _pb_str(2, "pytorch") + _pb_str(3, "2.5.1") + _pb_ld(7, graph(**graph_kwargs))
    body += _pb_ld(8, _pb_vi(2, opset)) if opset is not None else b""
    body += b"".join(_pb_ld(25, _pb_str(1, f"fn{i}")) for i in range(functions))
    return ir_field + body if header_first else body + ir_field


def write(tmp_path, data, name="model.onnx"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def ir_of(path):
    return dc.read_onnx_structure(str(path))["ir_version"]


def test_the_trainer_case_ir10_opset17_becomes_ir8(tmp_path):
    data = model(ir=10, opset=17)
    path = write(tmp_path, data)
    result = fir.normalize_fleet_ir_version(str(path))
    assert (result.before, result.after, result.changed) == (10, 8, True)
    assert "10 -> 8" in result.reason and "1.16.3" in result.reason
    patched = path.read_bytes()
    # Only the ir_version varint changed: same length, one byte different.
    assert len(patched) == len(data)
    assert [i for i, (a, b) in enumerate(zip(data, patched)) if a != b] == [1]
    before, after = dc.read_onnx_structure(str(write(tmp_path, data, "orig.onnx"))), \
        dc.read_onnx_structure(str(path))
    assert after["ir_version"] == 8
    assert {k: v for k, v in after.items() if k != "ir_version"} == \
        {k: v for k, v in before.items() if k != "ir_version"}
    # ...and the portal's own validator now accepts the header.
    dc.check_onnx_against_record(after, "yolo", 640, 4)


def test_idempotent(tmp_path):
    path = write(tmp_path, model(ir=10))
    fir.normalize_fleet_ir_version(str(path))
    once = path.read_bytes()
    again = fir.normalize_fleet_ir_version(str(path))
    assert again.changed is False and again.before == 8
    assert path.read_bytes() == once


@pytest.mark.parametrize("opset,expected", [(11, 8), (17, 8), (18, 8), (19, 9), (20, 9)])
def test_target_is_the_smallest_ir_the_opset_needs(tmp_path, opset, expected):
    path = write(tmp_path, model(ir=10, opset=opset))
    result = fir.normalize_fleet_ir_version(str(path))
    assert result.changed and result.after == expected == ir_of(path)


@pytest.mark.parametrize("ir", [3, 7, 8, 9])
def test_ir_within_the_floor_is_untouched(tmp_path, ir):
    data = model(ir=ir)
    path = write(tmp_path, data)
    result = fir.normalize_fleet_ir_version(str(path))
    assert (result.before, result.after, result.changed) == (ir, ir, False)
    assert path.read_bytes() == data


@pytest.mark.parametrize("kwargs,fragment", [
    (dict(opset=21), "needs IR 10"),
    (dict(functions=1), "model-local function"),
    (dict(initializers=(tensor("w0", INT4),)), "needs IR 10"),
    (dict(initializers=(tensor("w0", UINT4),)), "needs IR 10"),
    (dict(value_infos=(("q", [1, 4], FLOAT4E2M1),)), "needs IR 11"),
    (dict(opset=None), "no default-domain opset"),
])
def test_graphs_that_need_the_newer_ir_are_left_alone(tmp_path, kwargs, fragment):
    data = model(ir=10, **kwargs)
    path = write(tmp_path, data)
    result = fir.normalize_fleet_ir_version(str(path))
    assert result.changed is False and result.after == 10
    assert fragment in result.reason
    assert path.read_bytes() == data


def test_float8_raises_the_target_to_ir9(tmp_path):
    path = write(tmp_path, model(ir=10, opset=17, initializers=(tensor("w0", FLOAT8E4M3FN),)))
    result = fir.normalize_fleet_ir_version(str(path))
    assert result.changed and result.after == 9


def test_types_inside_subgraphs_count(tmp_path):
    inner = graph(initializers=(tensor("wi", INT4),), outputs=(), nodes=[])
    attr = _pb_str(1, "then_branch") + _pb_ld(6, inner) + _pb_vi(20, 5)
    if_node = _pb_str(1, "cond") + _pb_str(2, "o") + _pb_str(4, "If") + _pb_ld(5, attr)
    plain = _pb_str(1, "images") + _pb_str(1, "w0") + _pb_str(2, "output0") + _pb_str(4, "Conv")
    data = model(ir=10, nodes=[if_node, plain])
    path = write(tmp_path, data)
    assert fir.normalize_fleet_ir_version(str(path)).changed is False
    assert path.read_bytes() == data


def test_attribute_tensors_count(tmp_path):
    attr = _pb_str(1, "value") + _pb_ld(5, tensor("c", UINT4)) + _pb_vi(20, 4)
    const = _pb_str(2, "c") + _pb_str(4, "Constant") + _pb_ld(5, attr)
    plain = _pb_str(1, "images") + _pb_str(1, "w0") + _pb_str(2, "output0") + _pb_str(4, "Conv")
    path = write(tmp_path, model(ir=10, nodes=[const, plain]))
    assert fir.normalize_fleet_ir_version(str(path)).changed is False


def test_the_ir_field_need_not_come_first(tmp_path):
    path = write(tmp_path, model(ir=10, header_first=False))
    result = fir.normalize_fleet_ir_version(str(path))
    assert result.changed and ir_of(path) == 8


def test_a_multi_byte_varint_is_rewritten(tmp_path):
    data = model(ir=300)
    path = write(tmp_path, data)
    result = fir.normalize_fleet_ir_version(str(path))
    assert result.changed and result.after == 8
    assert len(path.read_bytes()) == len(data) - 1  # 2-byte varint -> 1 byte
    assert ir_of(path) == 8
    assert not (tmp_path / "model.onnx.ir-tmp").exists()


@pytest.mark.parametrize("blob", [b"", b"\x08", b"\xff" * 32, b"PK\x03\x04" + b"\x00" * 40,
                                  _pb_vi(1, 10), _pb_ld(7, graph())])
def test_garbage_is_left_untouched_and_never_raises(tmp_path, blob):
    path = write(tmp_path, blob)
    result = fir.normalize_fleet_ir_version(str(path))
    assert result.changed is False
    assert path.read_bytes() == blob


def test_a_missing_file_is_reported_not_raised(tmp_path):
    result = fir.normalize_fleet_ir_version(str(tmp_path / "missing.onnx"))
    assert result.changed is False and result.before is None and "unreadable" in result.reason


@settings(max_examples=150, deadline=None)
@given(ir=st.integers(1, 400), opset=st.integers(7, 25),
       dtypes=st.lists(st.sampled_from([1, 7, 10, 16, 17, 20, 21, 22, 23, 30]), max_size=3))
def test_property_never_breaks_the_floor_rules(tmp_path_factory, ir, opset, dtypes):
    tmp = tmp_path_factory.mktemp("prop")
    data = model(ir=ir, opset=opset, initializers=tuple(tensor(f"w{i}", t) for i, t in enumerate(dtypes)))
    path = write(tmp, data)
    result = fir.normalize_fleet_ir_version(str(path))
    needed = max([fir.required_ir_for_opset(opset)] +
                 [{17: 9, 18: 9, 19: 9, 20: 9, 21: 10, 22: 10, 23: 11}.get(t, 11 if t > 23 else 3)
                  for t in dtypes])
    if ir <= 9 or needed > 9:
        assert result.changed is False and path.read_bytes() == data
    else:
        assert result.changed and result.after == needed == ir_of(path)
        assert result.after < ir
