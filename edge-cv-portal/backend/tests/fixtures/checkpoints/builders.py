"""Builders for tiny, faithful checkpoint *envelopes* (never loadable weights).

``checkpoint_probe.classify_checkpoint`` decides a file's kind from its
container (zip member set / tar member names / protobuf header) and from the
*literal* structure of the torch ``data.pkl`` — it never unpickles and never
reads storage bytes. These builders therefore reproduce exactly what the probe
looks at, as observed on real files in ``docs/transfer-learning-spike.md``
§1.4 / §2.2:

* torch zip layout ``<root>/data.pkl`` + ``<root>/byteorder`` +
  ``<root>/data/<n>`` + ``<root>/version`` (``_use_new_zipfile_serialization``);
* ``data.pkl`` as a **protocol-2 pickle assembled opcode by opcode** — the
  GLOBAL names torch writes (``torch._utils._rebuild_tensor_v2``,
  ``torch.FloatStorage`` behind a ``BINPERSID`` ``('storage', …)`` tuple,
  ``collections.OrderedDict`` via ``REDUCE`` + ``SETITEMS``,
  ``argparse.Namespace`` / ``ultralytics.nn.tasks.DetectionModel`` via
  ``NEWOBJ`` + ``BUILD``), single-item ``APPEND`` / ``SETITEM`` where the
  stdlib pickler would emit them (the §2.4 regression), memo ``BINPUT`` /
  ``BINGET`` for repeated globals;
* an ONNX ``ModelProto`` with header fields, one node, one initializer, graph
  inputs/outputs and optional ``metadata_props``;
* the legacy pre-zip tar (``sys_info`` / ``pickle`` / ``tensors`` /
  ``storages``) behind a PAX header.

Storage members are written DEFLATED so the declared tensor sizes are real
while every fixture stays well under 50 KB on disk. We cannot use
``pickle.dumps`` on stand-in objects: the stdlib pickler verifies every GLOBAL
by importing it, and ``torch`` / ``ultralytics`` are not (and must not be)
installed where these tests run.

Every builder signature is ``build_x(path, ...) -> bytes``.
"""
from __future__ import annotations

import gzip
import io
import random
import struct
import tarfile
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Pickle opcode emitter (protocol 2, the protocol torch.save uses)             #
# --------------------------------------------------------------------------- #

_PROTO2 = b"\x80\x02"
_STOP = b"."
_NONE = b"N"
_NEWTRUE = b"\x88"
_NEWFALSE = b"\x89"
_BININT1 = b"K"
_BININT2 = b"M"
_BININT = b"J"
_LONG1 = b"\x8a"
_BINFLOAT = b"G"
_BINUNICODE = b"X"
_EMPTY_TUPLE = b")"
_TUPLE1 = b"\x85"
_TUPLE2 = b"\x86"
_TUPLE3 = b"\x87"
_MARK = b"("
_TUPLE = b"t"
_EMPTY_LIST = b"]"
_APPEND = b"a"
_APPENDS = b"e"
_EMPTY_DICT = b"}"
_SETITEM = b"s"
_SETITEMS = b"u"
_GLOBAL = b"c"
_REDUCE = b"R"
_NEWOBJ = b"\x81"
_BUILD = b"b"
_BINPERSID = b"Q"
_BINPUT = b"q"
_LONG_BINPUT = b"r"
_BINGET = b"h"
_LONG_BINGET = b"j"


class G:
    """A ``GLOBAL module\\nname\\n`` reference (never imported by anyone)."""

    __slots__ = ("module", "name")

    def __init__(self, module: str, name: str):
        self.module = module
        self.name = name

    @property
    def dotted(self) -> str:
        return f"{self.module}.{self.name}"


class Reduce:
    """``callable(*args)`` → REDUCE."""

    __slots__ = ("fn", "args")

    def __init__(self, fn: Any, args: tuple):
        self.fn = fn
        self.args = args


class NewObj:
    """``cls.__new__(cls, *args)`` → NEWOBJ (protocol-2 object construction)."""

    __slots__ = ("cls", "args")

    def __init__(self, cls: G, args: tuple = ()):
        self.cls = cls
        self.args = args


class Build:
    """``obj.__setstate__(state)`` → BUILD."""

    __slots__ = ("obj", "state")

    def __init__(self, obj: Any, state: Any):
        self.obj = obj
        self.state = state


class PersId:
    """A persistent id → payload then BINPERSID."""

    __slots__ = ("payload",)

    def __init__(self, payload: Any):
        self.payload = payload


class ODict:
    """``collections.OrderedDict`` as torch pickles it: REDUCE + SETITEMS."""

    __slots__ = ("items",)

    def __init__(self, items: Optional[Dict[Any, Any]] = None):
        self.items = dict(items or {})


class PySet:
    """A ``set`` under protocol 2: ``REDUCE(builtins.set, ([...],))``."""

    __slots__ = ("items",)

    def __init__(self, items: Iterable[Any] = ()):
        self.items = list(items)


class Tensor:
    """``torch._utils._rebuild_tensor_v2(storage, 0, size, stride, False, OrderedDict())``.

    ``storage`` is the ``BINPERSID`` of ``('storage', torch.FloatStorage,
    '<key>', 'cpu', numel)``. The key is assigned on first emission so a node
    referenced twice (PTL ``state_dict`` mirroring ``model``) shares a storage
    exactly like torch does.
    """

    __slots__ = ("shape", "key", "storage_type")

    def __init__(self, shape: Sequence[int], storage_type: str = "FloatStorage"):
        self.shape = tuple(int(d) for d in shape)
        self.key: Optional[str] = None
        self.storage_type = storage_type

    @property
    def numel(self) -> int:
        n = 1
        for d in self.shape:
            n *= d
        return n

    @property
    def stride(self) -> tuple:
        out: List[int] = []
        acc = 1
        for d in reversed(self.shape):
            out.append(acc)
            acc *= max(d, 1)
        return tuple(reversed(out))


class Param:
    """``torch._utils._rebuild_parameter(tensor, requires_grad, OrderedDict())``."""

    __slots__ = ("tensor",)

    def __init__(self, tensor: Tensor):
        self.tensor = tensor


def _uni(s: str) -> bytes:
    b = s.encode("utf-8")
    return _BINUNICODE + struct.pack("<I", len(b)) + b


def _int(i: int) -> bytes:
    if 0 <= i <= 0xFF:
        return _BININT1 + bytes([i])
    if 0 <= i <= 0xFFFF:
        return _BININT2 + struct.pack("<H", i)
    if -0x80000000 <= i <= 0x7FFFFFFF:
        return _BININT + struct.pack("<i", i)
    nbytes = (i.bit_length() + 8) // 8
    return _LONG1 + bytes([nbytes]) + i.to_bytes(nbytes, "little", signed=True)


class TorchPickler:
    """Emit a protocol-2 ``data.pkl`` for a literal/node tree.

    ``storages`` collects ``{key: nbytes}`` for every tensor emitted so the
    caller can write the matching ``data/<key>`` zip members.
    """

    def __init__(self):
        self.out = io.BytesIO()
        self.storages: Dict[str, int] = {}
        self._memo: Dict[str, int] = {}
        self._next_memo = 0
        self._next_storage = 0

    # -- memo (only globals are memoised; enough to exercise BINGET) --------
    def _put(self) -> bytes:
        idx = self._next_memo
        self._next_memo += 1
        if idx < 256:
            return _BINPUT + bytes([idx])
        return _LONG_BINPUT + struct.pack("<I", idx)

    def _get(self, idx: int) -> bytes:
        if idx < 256:
            return _BINGET + bytes([idx])
        return _LONG_BINGET + struct.pack("<I", idx)

    def _global(self, g: G) -> None:
        if g.dotted in self._memo:
            self.out.write(self._get(self._memo[g.dotted]))
            return
        self.out.write(_GLOBAL + f"{g.module}\n{g.name}\n".encode("utf-8"))
        self._memo[g.dotted] = self._next_memo
        self.out.write(self._put())

    # -- public -------------------------------------------------------------
    def dumps(self, tree: Any) -> bytes:
        self.out.write(_PROTO2)
        self.emit(tree)
        self.out.write(_STOP)
        return self.out.getvalue()

    def emit(self, v: Any) -> None:  # noqa: C901 - flat dispatch on purpose
        w = self.out.write
        if v is None:
            w(_NONE)
        elif v is True:
            w(_NEWTRUE)
        elif v is False:
            w(_NEWFALSE)
        elif isinstance(v, int):
            w(_int(v))
        elif isinstance(v, float):
            w(_BINFLOAT + struct.pack(">d", v))
        elif isinstance(v, str):
            w(_uni(v))
        elif isinstance(v, tuple):
            if not v:
                w(_EMPTY_TUPLE)
            elif len(v) <= 3:
                for item in v:
                    self.emit(item)
                w({1: _TUPLE1, 2: _TUPLE2, 3: _TUPLE3}[len(v)])
            else:
                w(_MARK)
                for item in v:
                    self.emit(item)
                w(_TUPLE)
        elif isinstance(v, list):
            w(_EMPTY_LIST)
            if len(v) == 1:  # the stdlib pickler's single-element APPEND
                self.emit(v[0])
                w(_APPEND)
            elif v:
                w(_MARK)
                for item in v:
                    self.emit(item)
                w(_APPENDS)
        elif isinstance(v, dict):
            w(_EMPTY_DICT)
            self._setitems(v)
        elif isinstance(v, G):
            self._global(v)
        elif isinstance(v, Reduce):
            self.emit(v.fn)
            self.emit(tuple(v.args))
            w(_REDUCE)
        elif isinstance(v, NewObj):
            self.emit(v.cls)
            self.emit(tuple(v.args))
            w(_NEWOBJ)
        elif isinstance(v, Build):
            self.emit(v.obj)
            self.emit(v.state)
            w(_BUILD)
        elif isinstance(v, PersId):
            self.emit(v.payload)
            w(_BINPERSID)
        elif isinstance(v, ODict):
            self.emit(Reduce(G("collections", "OrderedDict"), ()))
            self._setitems(v.items)
        elif isinstance(v, PySet):
            self.emit(Reduce(G("builtins", "set"), (list(v.items),)))
        elif isinstance(v, Tensor):
            self._tensor(v)
        elif isinstance(v, Param):
            self.emit(Reduce(G("torch._utils", "_rebuild_parameter"), (v.tensor, True, ODict())))
        else:
            raise TypeError(f"cannot emit {type(v).__name__}")

    def _setitems(self, items: Dict[Any, Any]) -> None:
        w = self.out.write
        if len(items) == 1:  # the stdlib pickler's single-element SETITEM
            (k, val), = items.items()
            self.emit(k)
            self.emit(val)
            w(_SETITEM)
        elif items:
            w(_MARK)
            for k, val in items.items():
                self.emit(k)
                self.emit(val)
            w(_SETITEMS)

    def _tensor(self, t: Tensor) -> None:
        if t.key is None:
            t.key = str(self._next_storage)
            self._next_storage += 1
        self.storages[t.key] = t.numel * 4
        storage = PersId(("storage", G("torch", t.storage_type), t.key, "cpu", t.numel))
        self.emit(Reduce(
            G("torch._utils", "_rebuild_tensor_v2"),
            (storage, 0, t.shape, t.stride, False, ODict()),
        ))


# --------------------------------------------------------------------------- #
# Containers                                                                   #
# --------------------------------------------------------------------------- #


def write_bytes(path, data: bytes) -> bytes:
    with open(path, "wb") as f:
        f.write(data)
    return data


def _torch_zip_bytes(root: str, tree: Any, extra: Optional[Dict[str, bytes]] = None,
                     byteorder: bool = True) -> bytes:
    """``torch.save`` zip: data.pkl, byteorder, data/<n> storages, version."""
    pk = TorchPickler()
    data_pkl = pk.dumps(tree)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{root}/data.pkl", data_pkl)
        if byteorder:
            z.writestr(f"{root}/byteorder", b"little")
        for key, nbytes in pk.storages.items():
            z.writestr(f"{root}/data/{key}", b"\x00" * nbytes)
        for name, blob in (extra or {}).items():
            z.writestr(f"{root}/{name}", blob)
        z.writestr(f"{root}/version", b"3\n")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# ultralytics best.pt (spike §1.4, R1 / R1')                                   #
# --------------------------------------------------------------------------- #


def _nn_module(cls: G, state: Dict[str, Any]) -> Build:
    """An ``nn.Module`` as pickled: NEWOBJ + BUILD(__dict__) with the standard
    hook/buffer OrderedDicts in front of the class-specific attributes."""
    base: Dict[str, Any] = {
        "training": False,
        "_parameters": ODict(),
        "_buffers": ODict(),
        "_non_persistent_buffers_set": PySet(),
        "_backward_pre_hooks": ODict(),
        "_backward_hooks": ODict(),
        "_is_full_backward_hook": None,
        "_forward_hooks": ODict(),
        "_forward_hooks_with_kwargs": ODict(),
        "_forward_hooks_always_called": ODict(),
        "_forward_pre_hooks": ODict(),
        "_forward_pre_hooks_with_kwargs": ODict(),
        "_state_dict_hooks": ODict(),
        "_state_dict_pre_hooks": ODict(),
        "_load_state_dict_pre_hooks": ODict(),
        "_load_state_dict_post_hooks": ODict(),
        "_modules": ODict(),
    }
    base.update(state)
    return Build(NewObj(cls), base)


def build_ultralytics_ckpt(path, names: Optional[Dict[int, str]] = None,
                           nc: Optional[int] = None, version: str = "8.3.40") -> bytes:
    """A stripped ``best.pt``: ``{date, version, …, model: DetectionModel, ema: None,
    …, train_args}`` with ``names`` / ``nc`` / ``yaml`` as literal attributes of the
    pickled model (spike §1.4). ``names`` keeps the caller's dict order so tests can
    check the probe sorts by class id."""
    return write_bytes(path, _torch_zip_bytes("best", _ultralytics_tree(names, nc, version)))


def _ultralytics_tree(names: Optional[Dict[int, str]], nc: Optional[int], version: str) -> dict:
    names = {0: "blue_plate"} if names is None else names
    nc = len(names) if nc is None else nc
    conv = _nn_module(G("torch.nn.modules.conv", "Conv2d"), {
        "_parameters": ODict({"weight": Param(Tensor((16, 3, 3, 3))), "bias": None}),
        "in_channels": 3, "out_channels": 16, "kernel_size": (3, 3), "stride": (2, 2),
        "padding": (1, 1), "dilation": (1, 1), "transposed": False, "output_padding": (0, 0),
        "groups": 1, "padding_mode": "zeros",
    })
    conv_block = _nn_module(G("ultralytics.nn.modules.conv", "Conv"), {
        "_modules": ODict({"conv": conv}), "i": 0, "f": -1, "type": "ultralytics.nn.modules.conv.Conv",
        "np": 432,
    })
    detect = _nn_module(G("ultralytics.nn.modules.head", "Detect"), {
        "_buffers": ODict({"stride": Tensor((3,))}),
        "_modules": ODict({
            "cv3": _nn_module(G("torch.nn.modules.container", "ModuleList"), {}),
            "dfl": _nn_module(G("ultralytics.nn.modules.block", "DFL"), {"c1": 16}),
        }),
        "nc": nc, "nl": 3, "reg_max": 16, "no": nc + 64, "stride": Tensor((3,)),
        "i": 23, "f": [16, 19, 22], "type": "ultralytics.nn.modules.head.Detect",
        "end2end": False, "export": False, "format": None, "shape": None,
    })
    seq = _nn_module(G("torch.nn.modules.container", "Sequential"), {
        "_modules": ODict({"0": conv_block, "23": detect}),
    })
    model = _nn_module(G("ultralytics.nn.tasks", "DetectionModel"), {
        "_modules": ODict({"model": seq}),
        "yaml": {
            "nc": nc, "scale": "s", "yaml_file": "yolo11s.yaml", "ch": 3,
            "backbone": [[-1, 1, "Conv", [64, 3, 2]], [-1, 1, "Conv", [128, 3, 2]]],
            "head": [[[16, 19, 22], 1, "Detect", ["nc"]]],
        },
        "save": [4, 6, 10, 13, 16, 19, 22],
        "names": dict(names),
        "inplace": True,
        "end2end": False,
        "stride": Tensor((3,)),
        "nc": nc,
        "args": {"task": "detect", "mode": "train", "model": "yolo11s.pt", "imgsz": 1280,
                 "epochs": 100, "batch": 4, "patience": 30, "box": 7.5, "cls": 0.5, "dfl": 1.5},
        "criterion": None,
    })
    top = {
        "date": "2026-09-13T14:42:26.000000",
        "version": version,
        "license": "AGPL-3.0 License (https://ultralytics.com/license)",
        "docs": "https://docs.ultralytics.com",
        "epoch": -1,
        "best_fitness": None,
        "model": model,
        "ema": None,
        "updates": None,
        "optimizer": None,
        "train_args": {
            "task": "detect", "mode": "train", "model": "yolo11s.pt",
            "data": "/opt/ml/input/work/dataset/data.yaml", "epochs": 100, "patience": 30,
            "batch": 4, "imgsz": 1280, "save": True, "device": "0", "workers": 4,
            "project": "/opt/ml/output/runs", "name": "train", "exist_ok": True,
        },
        "train_metrics": {"metrics/precision(B)": 0.98, "metrics/recall(B)": 0.97,
                          "metrics/mAP50(B)": 0.99, "metrics/mAP50-95(B)": 0.81, "fitness": 0.83},
        "train_results": {"epoch": [1, 2], "train/box_loss": [1.2, 0.9]},
    }
    return top


# --------------------------------------------------------------------------- #
# RF-DETR .pth — the three layouts of spike §1.4 / §2.2                         #
# --------------------------------------------------------------------------- #

_RFDETR_ARGS_COMMON: Dict[str, Any] = {
    "lr": 1e-4, "lr_encoder": 1.5e-4, "batch_size": 4, "weight_decay": 1e-4,
    "encoder": "dinov2_windowed_small", "num_queries": 300, "group_detr": 13,
    "dec_layers": 2, "hidden_dim": 256, "sa_nheads": 8, "ca_nheads": 16, "dec_n_points": 2,
    "two_stage": True, "bbox_reparam": True, "lite_refpoint_refine": True,
    "layer_norm": True, "amp": True, "num_windows": 4, "device": "cuda",
}


def _rfdetr_state_dict(num_classes: int, hidden_dim: int = 8, group_detr: int = 13,
                       num_queries: int = 300) -> ODict:
    """Raw-keyed RF-DETR ``model`` state_dict with the head / encoder /
    backbone key families the probe keys on. ``class_embed.bias`` has
    ``num_classes + 1`` rows (background slot), as ``load_pretrain_weights`` reads."""
    c1 = num_classes + 1
    sd: Dict[str, Any] = {
        "class_embed.weight": Tensor((c1, hidden_dim)),
        "class_embed.bias": Tensor((c1,)),
        "bbox_embed.layers.0.weight": Tensor((hidden_dim, hidden_dim)),
        "bbox_embed.layers.0.bias": Tensor((hidden_dim,)),
        "bbox_embed.layers.2.weight": Tensor((4, hidden_dim)),
        "bbox_embed.layers.2.bias": Tensor((4,)),
        "refpoint_embed.weight": Tensor((num_queries, 4)),
        "query_feat.weight": Tensor((num_queries, hidden_dim)),
    }
    for g in range(group_detr):
        sd[f"transformer.enc_out_class_embed.{g}.weight"] = Tensor((c1, hidden_dim))
        sd[f"transformer.enc_out_class_embed.{g}.bias"] = Tensor((c1,))
    sd["transformer.enc_out_bbox_embed.layers.0.weight"] = Tensor((hidden_dim, hidden_dim))
    sd["transformer.enc_out_bbox_embed.layers.0.bias"] = Tensor((hidden_dim,))
    sd["transformer.decoder.layers.0.self_attn.in_proj_weight"] = Tensor((3 * hidden_dim, hidden_dim))
    sd["transformer.decoder.layers.0.self_attn.in_proj_bias"] = Tensor((3 * hidden_dim,))
    sd["backbone.0.encoder.embeddings.cls_token"] = Tensor((1, 1, hidden_dim))
    sd["backbone.0.encoder.embeddings.position_embeddings"] = Tensor((1, 5, hidden_dim))
    sd["backbone.0.encoder.embeddings.patch_embeddings.projection.weight"] = Tensor((hidden_dim, 3, 2, 2))
    sd["backbone.0.encoder.embeddings.patch_embeddings.projection.bias"] = Tensor((hidden_dim,))
    sd["backbone.0.encoder.encoder.layer.0.attention.attention.query.weight"] = Tensor((hidden_dim, hidden_dim))
    sd["backbone.0.encoder.encoder.layer.0.attention.attention.query.bias"] = Tensor((hidden_dim,))
    sd["backbone.0.encoder.layernorm.weight"] = Tensor((hidden_dim,))
    sd["backbone.0.projector.0.weight"] = Tensor((hidden_dim, hidden_dim, 1, 1))
    return ODict(sd)


def build_rfdetr_published(path, num_classes: int = 90, args_num_classes: int = 2,
                           hidden_dim: int = 8, group_detr: int = 13) -> bytes:
    """Published ``rf-detr-*.pth`` (R2): ``{model, optimizer, lr_scheduler, epoch,
    args}`` with ``args`` an ``argparse.Namespace`` that carries **no**
    ``class_names`` and a stale ``num_classes`` (2 in the real file, against a
    91-wide COCO head)."""
    ns_fields = dict(_RFDETR_ARGS_COMMON)
    ns_fields.update({
        "resolution": 384, "hidden_dim": hidden_dim, "group_detr": group_detr,
        "dataset_file": "coco", "coco_path": "/data/coco", "num_classes": args_num_classes,
        "resume": "rf-detr-nano-experimental.pth", "epochs": 100, "output_dir": "output",
        "seed": 42, "eval": False, "distributed": True, "world_size": 8,
    })
    top = {
        "model": _rfdetr_state_dict(num_classes, hidden_dim, group_detr),
        "optimizer": {
            "state": {},
            "param_groups": [{"lr": 1e-4, "weight_decay": 1e-4, "initial_lr": 1e-4,
                              "params": [0, 1, 2, 3]}],
        },
        "lr_scheduler": {"step_size": 40, "gamma": 0.1, "base_lrs": [1e-4], "last_epoch": 48,
                         "_step_count": 49, "verbose": False, "_last_lr": [1e-5]},
        "epoch": 48,
        "args": Build(NewObj(G("argparse", "Namespace")), ns_fields),
    }
    return write_bytes(path, _torch_zip_bytes("rf-detr-nano", top))


def _rfdetr_train_args(class_names: Sequence[str], args_num_classes: Optional[int],
                       hidden_dim: int, group_detr: int) -> Dict[str, Any]:
    """1.10.1 ``train_config.model_dump()`` — a plain dict with ``class_names``."""
    a = dict(_RFDETR_ARGS_COMMON)
    a.update({
        "resolution": 512, "hidden_dim": hidden_dim, "group_detr": group_detr,
        "dataset_dir": "/opt/ml/input/work/dataset", "output_dir": "/opt/ml/output/rfdetr",
        "epochs": 30, "grad_accum_steps": 4, "early_stopping": True, "early_stopping_patience": 10,
        "class_names": list(class_names),
        "num_classes": len(class_names) if args_num_classes is None else args_num_classes,
        "pretrain_weights": "rf-detr-small.pth", "run_test": True,
    })
    return a


def build_rfdetr_v1101(path, class_names: Sequence[str] = ("blue_plate",),
                       args_num_classes: Optional[int] = None,
                       hidden_dim: int = 8, group_detr: int = 13) -> bytes:
    """rfdetr 1.10.1 ``BestModelCallback`` layout as documented in the package
    source (spike §1.4): ``{model, args(dict), epoch, model_name, model_config,
    callbacks}`` — optimizer / scheduler stripped, ``args.class_names`` present."""
    nc = len(class_names)
    top = {
        "model": _rfdetr_state_dict(nc, hidden_dim, group_detr),
        "args": _rfdetr_train_args(class_names, args_num_classes, hidden_dim, group_detr),
        "epoch": 3,
        "model_name": "RFDETRSmall",
        "model_config": {"num_classes": nc, "encoder": "dinov2_windowed_small", "resolution": 512,
                         "hidden_dim": hidden_dim, "group_detr": group_detr,
                         "pretrain_weights": "rf-detr-small.pth", "device": "cuda"},
        "callbacks": {
            "BestModelCallback": {"best_map": 0.87, "best_epoch": 3, "monitor": "val/map50"},
            "EMACallback": {"decay": 0.993, "updates": 96},
        },
    }
    return write_bytes(path, _torch_zip_bytes("checkpoint_best_total", top))


def build_rfdetr_ptl(path, class_names: Sequence[str] = ("blue_plate", "blue_plate_b"),
                     args_num_classes: Optional[int] = None,
                     hidden_dim: int = 8, group_detr: int = 13) -> bytes:
    """The layout a real 1.10.1 ``checkpoint_best_total.pth`` actually has
    (spike §2.2 row R2', §2.5): the rfdetr core (``model`` + ``args`` +
    ``model_name``) **plus** the PyTorch-Lightning ``.ckpt`` payload
    (``state_dict`` with ``model.`` prefix sharing the same storages,
    ``optimizer_states``, ``lr_schedulers``, ``loops``, …) and **no**
    ``model_config``. Zip root is PTL's atomic-save ``tmp…`` directory."""
    nc = len(class_names)
    model = _rfdetr_state_dict(nc, hidden_dim, group_detr)
    top = {
        "model": model,
        "args": _rfdetr_train_args(class_names, args_num_classes, hidden_dim, group_detr),
        "model_name": "RFDETRSmall",
        "rfdetr_version": "1.10.1",
        "state_dict": ODict({f"model.{k}": v for k, v in model.items.items()}),
        "global_step": 96,
        "epoch": 3,
        "pytorch-lightning_version": "2.5.1",
        "loops": {"fit_loop": {"state_dict": {}, "epoch_loop.state_dict": {"_batches_that_stepped": 96},
                               "epoch_progress": {"total": {"ready": 4, "completed": 3}}}},
        "callbacks": {"BestModelCallback": {"best_map": 0.87, "best_epoch": 3},
                      "EMACallback": {"decay": 0.993, "updates": 96}},
        "optimizer_states": [{"state": {}, "param_groups": [{"lr": 1e-4, "params": [0, 1, 2]}]}],
        "lr_schedulers": [{"base_lrs": [1e-4], "last_epoch": 96, "_step_count": 97}],
        "best_total_source": "ema",
    }
    return write_bytes(path, _torch_zip_bytes("tmp8f3k2v1q", top))


# --------------------------------------------------------------------------- #
# Negatives: TorchScript, state_dict, legacy tar                              #
# --------------------------------------------------------------------------- #


def build_torchscript(path) -> bytes:
    """``torch.jit.save`` archive (R3): ``constants.pkl`` + ``code/`` beside
    ``data.pkl``, whose object GLOBALs are all mangled ``__torch__.*`` classes."""
    conv = Build(NewObj(G("__torch__.torch.nn.modules.conv.___torch_mangle_1", "Conv2d")), {
        "training": False, "weight": Tensor((8, 3, 3, 3)), "bias": Tensor((8,)),
        "in_channels": 3, "out_channels": 8,
    })
    model = Build(NewObj(G("__torch__.___torch_mangle_0", "Model")), {
        "training": False, "encoder": conv, "anomaly_score_threshold": 0.5,
    })
    constants = TorchPickler().dumps(())
    code = {
        "code/__torch__.py": (
            b"class Model(Module):\n  __parameters__ = []\n  __buffers__ = []\n"
            b"  training : bool\n  encoder : __torch__.torch.nn.modules.conv.___torch_mangle_1.Conv2d\n"
            b"  def forward(self: __torch__.___torch_mangle_0.Model, x: Tensor) -> Tensor:\n"
            b"    return (self.encoder).forward(x, )\n"
        ),
        "code/__torch__.py.debug_pkl": TorchPickler().dumps(((), (), ())),
        "code/__torch__/torch/nn/modules/conv/___torch_mangle_1.py": (
            b"class Conv2d(Module):\n  __parameters__ = [\"weight\", \"bias\", ]\n"
            b"  def forward(self, x: Tensor) -> Tensor:\n    return torch.conv2d(x, self.weight, self.bias)\n"
        ),
        "constants.pkl": constants,
    }
    return write_bytes(path, _torch_zip_bytes("mochi", model, extra=code, byteorder=False))


def _plain_state_dict() -> ODict:
    return ODict({
        "encoder.conv1.weight": Tensor((8, 3, 3, 3)),
        "encoder.conv1.bias": Tensor((8,)),
        "encoder.bn1.weight": Tensor((8,)),
        "encoder.bn1.bias": Tensor((8,)),
        "encoder.bn1.running_mean": Tensor((8,)),
        "encoder.bn1.running_var": Tensor((8,)),
        "encoder.bn1.num_batches_tracked": Tensor((), "LongStorage"),
        "decoder.fc.weight": Tensor((2, 8)),
        "decoder.fc.bias": Tensor((2,)),
        "anomaly_score_threshold": Tensor(()),
    })


def build_state_dict(path, wrapped: bool = True) -> bytes:
    """Plain tensors, no framework GLOBAL, no RF-DETR head (R4). ``wrapped``
    reproduces LFV's ``{state_dict, args, kwargs, optimizer_state_dict,
    lr_scheduler_state_dict}``; otherwise the root *is* the OrderedDict."""
    sd = _plain_state_dict()
    tree: Any = sd
    if wrapped:
        tree = {
            "state_dict": sd,
            "args": [],
            "kwargs": {"in_channels": 3, "latent_dim": 8},
            "optimizer_state_dict": {"state": {}, "param_groups": [{"lr": 0.001, "params": [0, 1, 2, 3]}]},
            "lr_scheduler_state_dict": {"last_epoch": 12, "_step_count": 13},
        }
    return write_bytes(path, _torch_zip_bytes("mochi", tree))


def build_legacy_torch_tar(path) -> bytes:
    """Pre-zip ``torch.save`` (R5, e.g. ``resnet18-5c106cde.pth``): an
    uncompressed PAX tar with ``sys_info`` / ``pickle`` / ``tensors`` /
    ``storages``; tensors in ``pickle`` are integer persistent ids into the
    ``tensors`` member and parameters reference ``torch.nn.parameter.Parameter``."""
    sys_info = TorchPickler().dumps({
        "protocol_version": 1000, "little_endian": True,
        "type_sizes": {"short": 2, "int": 4, "long": 4},
    })
    pk = TorchPickler().dumps(ODict({
        "conv1.weight": Reduce(G("torch.nn.parameter", "Parameter"), (PersId(0), True)),
        "bn1.weight": Reduce(G("torch.nn.parameter", "Parameter"), (PersId(1), True)),
        "bn1.running_mean": PersId(2),
        "fc.weight": Reduce(G("torch.nn.parameter", "Parameter"), (PersId(3), True)),
    }))
    tensors = struct.pack("<qq", 4, 0) + struct.pack("<qqq", 8, 3, 3) + b"\x00" * 32
    storages = struct.pack("<q", 4) + b"\x00" * 128
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        for name, blob in (("sys_info", sys_info), ("pickle", pk), ("tensors", tensors), ("storages", storages)):
            ti = tarfile.TarInfo(name)
            ti.size = len(blob)
            ti.mtime = 1509037320.5  # float mtime → PAX header, like the real files
            t.addfile(ti, io.BytesIO(blob))
    return write_bytes(path, buf.getvalue())


# --------------------------------------------------------------------------- #
# ONNX ModelProto (hand-encoded protobuf)                                      #
# --------------------------------------------------------------------------- #


def _pb_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_tag(field: int, wt: int) -> bytes:
    return _pb_varint((field << 3) | wt)


def _pb_vi(field: int, n: int) -> bytes:
    return _pb_tag(field, 0) + _pb_varint(n)


def _pb_ld(field: int, payload: bytes) -> bytes:
    return _pb_tag(field, 2) + _pb_varint(len(payload)) + payload


def _pb_str(field: int, s: str) -> bytes:
    return _pb_ld(field, s.encode("utf-8"))


def _value_info(name: str, shape: Sequence[Any], elem_type: int = 1) -> bytes:
    dims = b""
    for d in shape:
        if isinstance(d, int):
            dims += _pb_ld(1, _pb_vi(1, d))          # Dimension.dim_value
        else:
            dims += _pb_ld(1, _pb_str(2, str(d)))   # Dimension.dim_param
    tensor_type = _pb_vi(1, elem_type) + _pb_ld(2, dims)   # TypeProto.Tensor
    type_proto = _pb_ld(1, tensor_type)                     # TypeProto.tensor_type
    return _pb_str(1, name) + _pb_ld(2, type_proto)


def build_onnx(path, names: Optional[Dict[int, str]] = None,
               outputs: Optional[Sequence[Tuple[str, Sequence[Any]]]] = None,
               producer: str = "pytorch", producer_version: str = "2.4.1",
               ir_version: int = 8, opset: int = 17,
               extra_metadata: Optional[Dict[str, str]] = None) -> bytes:
    """A ``ModelProto`` with header, one node, one initializer (raw bytes the
    probe must seek over), an input, the given outputs and, when ``names`` is
    given, ultralytics-style ``metadata_props`` (``names`` as ``str(dict)``,
    plus ``stride`` / ``task`` which mark the export as YOLO). Without
    ``names`` the default outputs are the RF-DETR two-tensor export (R7b)."""
    if outputs is None:
        if names is not None:
            outputs = [("output0", [1, 4 + len(names), 8400])]
        else:
            outputs = [("pred_boxes", [1, 300, 4]), ("pred_logits", [1, 300, 91])]
    node = _pb_str(1, "images") + _pb_str(1, "w0") + _pb_str(2, outputs[0][0]) + _pb_str(4, "Conv")
    init = (_pb_vi(1, 16) + _pb_vi(1, 3) + _pb_vi(1, 3) + _pb_vi(1, 3)   # dims
            + _pb_vi(2, 1)                                             # FLOAT
            + _pb_str(8, "w0")
            + _pb_ld(9, bytes(range(256)) * 2))                        # raw_data (opaque blob)
    graph = (_pb_ld(1, node) + _pb_str(2, "main_graph") + _pb_ld(5, init)
             + _pb_ld(11, _value_info("images", [1, 3, 640, 640])))
    for oname, oshape in outputs:
        graph += _pb_ld(12, _value_info(oname, oshape))
    model = (_pb_vi(1, ir_version) + _pb_str(2, producer) + _pb_str(3, producer_version)
             + _pb_str(4, "") + _pb_vi(5, 0) + _pb_str(6, "")
             + _pb_ld(7, graph)
             + _pb_ld(8, _pb_str(1, "") + _pb_vi(2, opset)))
    meta: Dict[str, str] = {}
    if names is not None:
        meta.update({
            "description": "Ultralytics YOLO11s model trained on data.yaml",
            "author": "Ultralytics", "version": "8.3.40", "license": "AGPL-3.0",
            "stride": "32", "task": "detect", "batch": "1", "imgsz": "[640, 640]",
            "names": "{" + ", ".join(f"{k}: {v!r}" for k, v in names.items()) + "}",
        })
    meta.update(extra_metadata or {})
    for k, v in meta.items():
        model += _pb_ld(14, _pb_str(1, k) + _pb_str(2, v))
    return write_bytes(path, model)


# --------------------------------------------------------------------------- #
# Garbage / negatives that are not checkpoints at all                          #
# --------------------------------------------------------------------------- #


def build_elf(path) -> bytes:
    """A Neo / TensorRT ``.so`` header (ELF64 aarch64 shared object)."""
    data = (b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
            + struct.pack("<HHI", 3, 0xB7, 1) + b"\x00" * 240)
    return write_bytes(path, data)


def build_garbage(path, n: int = 1024, seed: int = 0xC0FFEE) -> bytes:
    """Deterministic pseudo-random bytes behind a lead that matches no magic."""
    return write_bytes(path, b"\xff\xfe" + random.Random(seed).randbytes(n))


def build_empty(path) -> bytes:
    return write_bytes(path, b"")


def build_truncated_zip(path, frac: float = 0.6) -> bytes:
    """A real ultralytics envelope cut mid-archive (no central directory)."""
    blob = _torch_zip_bytes("best", _ultralytics_tree(None, None, "8.3.40"))
    return write_bytes(path, blob[: max(4, int(len(blob) * frac))])


def build_truncated_pickle(path) -> bytes:
    """A protocol-2 pickle that stops mid-BINUNICODE (bare pickle path)."""
    return write_bytes(path, b"\x80\x02}q\x00(X\x04\x00\x00\x00da")


def build_zip_without_data_pkl(path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("README.txt", b"not a torch archive\n")
        z.writestr("weights.bin", b"\x00" * 64)
    return write_bytes(path, buf.getvalue())


def build_gzip_tarball(path) -> bytes:
    """``model.tar.gz`` handed to the probe un-extracted."""
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w") as t:
        ti = tarfile.TarInfo("model.onnx")
        ti.size = 4
        t.addfile(ti, io.BytesIO(b"\x08\x08\x12\x00"))
    return write_bytes(path, gzip.compress(inner.getvalue()))


def build_plain_pickle_dict(path) -> bytes:
    """A bare pickle of literals — no tensors, no framework — is not a checkpoint."""
    return write_bytes(path, TorchPickler().dumps({"a": 1, "names": ["x", "y"], "nested": {"k": None}}))
