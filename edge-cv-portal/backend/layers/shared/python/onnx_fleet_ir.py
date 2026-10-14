"""Keep packaged ONNX graphs loadable on the fleet's oldest onnxruntime.

onnxruntime 1.16.3 runs on the JP5 GPU build and on the CPU / x86 images. It
loads ONNX IR version 9 at most and rejects anything newer at load time
("Unsupported model IR version: 10, max supported IR version: 9").

The portal's YOLO trainer re-serializes its export through onnx 1.17, which
stamps `ir_version` 10 on a graph that uses nothing newer than IR 8 (opset 17).
So a portal-trained YOLO component never loaded on JP5 or x86 CPU targets,
while JP6 (1.20.1) and JP7 (1.23.2) loaded it fine. Found by the
detector-checkpoint-import spike (docs/detector-checkpoint-import-spike.md).

`normalize_fleet_ir_version` lowers the header to the smallest IR the graph
actually needs, and only when nothing in the graph needs the newer IR:
  * the default-domain opset: <= 18 needs IR 8, 19-20 need IR 9, >= 21 needs
    IR 10 (onnx/docs/Versioning.md);
  * element types: FLOAT8* (17-20) need IR 9; UINT4 / INT4 (21, 22) need
    IR 10; FLOAT4E2M1 (23) and later need IR 11+;
  * model-local functions (they can carry IR-10 overloads) are never touched.
Only the varint of ModelProto field 1 changes; every other byte stays as it
was. Pure stdlib over the protobuf wire format, reusing the torch-free walker
of detector_conversion. It never raises: unreadable input is left untouched
and the reason is returned.
"""
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Tuple

from detector_conversion import _fields, _varint

#: onnxruntime 1.16.3 (JP5 GPU build, CPU / x86 images).
FLEET_FLOOR_ORT = '1.16.3'
FLEET_MAX_IR = 9
MAX_GRAPH_DEPTH = 16

#: TensorProto.DataType -> the first IR version that defines it.
_DTYPE_MIN_IR = {17: 9, 18: 9, 19: 9, 20: 9,   # FLOAT8E4M3FN / FNUZ, FLOAT8E5M2 / FNUZ
                 21: 10, 22: 10,                # UINT4, INT4
                 23: 11}                        # FLOAT4E2M1


@dataclass(frozen=True)
class IrNormalization:
    """before / after: the model's ir_version (None when unreadable);
    changed: the file was rewritten; reason: why, or why not."""

    before: Optional[int]
    after: Optional[int]
    changed: bool
    reason: str


def required_ir_for_opset(opset: int) -> int:
    """The smallest IR version that defines a default-domain opset."""
    if opset <= 18:
        return 8
    if opset <= 20:
        return 9
    return 10


def _dtype_ir(dtype: int) -> int:
    return _DTYPE_MIN_IR.get(dtype, 11 if dtype > 23 else 3)


def _type_proto_dtypes(buf, span: Tuple[int, int], out: Set[int], depth: int) -> None:
    """Element types in a TypeProto: tensor_type (1) / sparse_tensor_type (8)
    elem_type, recursing through sequence (4), map (5) and optional (9)."""
    if depth > MAX_GRAPH_DEPTH:
        raise ValueError('TypeProto nested too deeply')
    off, length = span
    for fnum, wt, value in _fields(buf, off, off + length):
        if wt != 2:
            continue
        if fnum in (1, 8):
            v_off, v_len = value
            for f2, w2, v2 in _fields(buf, v_off, v_off + v_len):
                if f2 == 1 and w2 == 0:
                    out.add(v2)
        elif fnum in (4, 9):  # Sequence.elem_type / Optional.elem_type (TypeProto)
            s_off, s_len = value
            for f2, w2, v2 in _fields(buf, s_off, s_off + s_len):
                if f2 == 1 and w2 == 2:
                    _type_proto_dtypes(buf, v2, out, depth + 1)
        elif fnum == 5:  # Map: key_type (1, varint), value_type (2, TypeProto)
            m_off, m_len = value
            for f2, w2, v2 in _fields(buf, m_off, m_off + m_len):
                if f2 == 1 and w2 == 0:
                    out.add(v2)
                elif f2 == 2 and w2 == 2:
                    _type_proto_dtypes(buf, v2, out, depth + 1)


def _value_info_dtypes(buf, span: Tuple[int, int], out: Set[int], depth: int) -> None:
    off, length = span
    for fnum, wt, value in _fields(buf, off, off + length):
        if fnum == 2 and wt == 2:  # ValueInfoProto.type
            _type_proto_dtypes(buf, value, out, depth + 1)


def _tensor_dtype(buf, span: Tuple[int, int], out: Set[int]) -> None:
    off, length = span
    for fnum, wt, value in _fields(buf, off, off + length):
        if fnum == 2 and wt == 0:  # TensorProto.data_type
            out.add(value)


def _sparse_tensor_dtypes(buf, span: Tuple[int, int], out: Set[int]) -> None:
    off, length = span
    for fnum, wt, value in _fields(buf, off, off + length):
        if fnum in (1, 2) and wt == 2:  # values / indices (TensorProto)
            _tensor_dtype(buf, value, out)


def _graph_dtypes(buf, span: Tuple[int, int], out: Set[int], depth: int = 0) -> None:
    """Every element type a GraphProto declares: initializers, sparse
    initializers, graph inputs / outputs / value_info, and node attribute
    tensors, types and subgraphs (recursively)."""
    if depth > MAX_GRAPH_DEPTH:
        raise ValueError('subgraphs nested too deeply')
    off, length = span
    for fnum, wt, value in _fields(buf, off, off + length):
        if wt != 2:
            continue
        if fnum == 5:
            _tensor_dtype(buf, value, out)
        elif fnum == 15:
            _sparse_tensor_dtypes(buf, value, out)
        elif fnum in (11, 12, 13):
            _value_info_dtypes(buf, value, out, depth)
        elif fnum == 1:  # NodeProto -> AttributeProto (5)
            n_off, n_len = value
            for f2, w2, v2 in _fields(buf, n_off, n_off + n_len):
                if f2 != 5 or w2 != 2:
                    continue
                a_off, a_len = v2
                for f3, w3, v3 in _fields(buf, a_off, a_off + a_len):
                    if w3 != 2:
                        continue
                    if f3 in (5, 10):      # t / tensors
                        _tensor_dtype(buf, v3, out)
                    elif f3 in (22, 23):   # sparse_tensor / sparse_tensors
                        _sparse_tensor_dtypes(buf, v3, out)
                    elif f3 in (14, 15):   # tp / type_protos
                        _type_proto_dtypes(buf, v3, out, depth + 1)
                    elif f3 in (6, 11):    # g / graphs
                        _graph_dtypes(buf, v3, out, depth + 1)


def _scan(buf) -> Dict[str, Any]:
    """The top-level facts the decision needs."""
    facts: Dict[str, Any] = {'ir': None, 'ir_span': None, 'opset': None, 'functions': 0,
                             'dtypes': set(), 'graphs': 0}
    i = 0
    end = len(buf)
    while i < end:
        tag_start = i
        tag, i = _varint(buf, i)
        fnum, wt = tag >> 3, tag & 7
        if wt == 0:
            value_start = i
            value, i = _varint(buf, i)
            if fnum == 1:
                facts['ir'] = value
                facts['ir_span'] = (value_start, i)
        elif wt == 2:
            length, i = _varint(buf, i)
            if i + length > end:
                raise ValueError('truncated field')
            if fnum == 7:
                facts['graphs'] += 1
                _graph_dtypes(buf, (i, length), facts['dtypes'])
            elif fnum == 8:
                domain, version = '', None
                for f2, w2, v2 in _fields(buf, i, i + length):
                    if f2 == 1 and w2 == 2:
                        d_off, d_len = v2
                        domain = bytes(buf[d_off:d_off + d_len]).decode('utf-8', 'replace')
                    elif f2 == 2 and w2 == 0:
                        version = v2
                if domain in ('', 'ai.onnx') and version is not None:
                    facts['opset'] = version
            elif fnum == 25:
                facts['functions'] += 1
            i += length
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        else:
            raise ValueError(f'unsupported wire type {wt} at offset {tag_start}')
    return facts


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def normalize_fleet_ir_version(path: str) -> IrNormalization:
    """Lower an ONNX file's ir_version to what its content needs when that
    brings it within the fleet floor (IR <= 9); see the module docstring.
    Rewrites the file in place when it changes it. Never raises."""
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError as e:
        return IrNormalization(None, None, False, f'unreadable: {e}')
    try:
        facts = _scan(memoryview(data))
    except (ValueError, IndexError) as e:
        return IrNormalization(None, None, False, f'not a readable ONNX ModelProto: {e}')
    before = facts['ir']
    if before is None or facts['graphs'] != 1:
        return IrNormalization(before, before, False, 'not an ONNX ModelProto (no IR version / graph)')
    if before <= FLEET_MAX_IR:
        return IrNormalization(before, before, False,
                               f'IR {before} already loads on onnxruntime {FLEET_FLOOR_ORT}')
    opset = facts['opset']
    if opset is None:
        return IrNormalization(before, before, False, 'no default-domain opset; left as is')
    if facts['functions']:
        return IrNormalization(before, before, False,
                               f"{facts['functions']} model-local function(s); left as is")
    needed = max([required_ir_for_opset(opset)] + [_dtype_ir(t) for t in facts['dtypes']])
    if needed > FLEET_MAX_IR:
        return IrNormalization(before, before, False,
                               f'the graph needs IR {needed} (opset {opset}, element types '
                               f'{sorted(facts["dtypes"])}); onnxruntime {FLEET_FLOOR_ORT} cannot load it')
    start, stop = facts['ir_span']
    new_value = _encode_varint(needed)
    try:
        if len(new_value) == stop - start:
            with open(path, 'r+b') as fh:
                fh.seek(start)
                fh.write(new_value)
        else:
            tmp = f'{path}.ir-tmp'
            with open(tmp, 'wb') as fh:
                fh.write(data[:start])
                fh.write(new_value)
                fh.write(data[stop:])
            os.replace(tmp, path)
    except OSError as e:
        return IrNormalization(before, before, False, f'could not rewrite: {e}')
    return IrNormalization(before, needed, True,
                           f'ir_version {before} -> {needed} (default-domain opset {opset}); '
                           f'loads on onnxruntime {FLEET_FLOOR_ORT}')
