#!/usr/bin/env python3
"""Classify a weights file by inspecting its envelope — never by loading it.

Shared-layer home of ``classify_checkpoint`` (spec
``rfdetr-training-and-transfer-learning``, Requirement 7 — design §Shared
layer; contract and measured behaviour in ``docs/transfer-learning-spike.md``
§2). Prototyped by the spike as ``datasets/detection_training/_checkpoint_probe.py``
(task 1.2) and promoted here unchanged in behaviour by task 7.1.
``detection_training.py`` re-exports ``classify_checkpoint`` and
``FINE_TUNABLE_KINDS`` so callers can import either module. Smart Import
(``model_converter.py``, Requirement 7.1) runs it on every non-ONNX source to
decide whether to keep the checkpoint as a fine-tunable sidecar.

Design constraints
------------------
* **stdlib only** (``zipfile``, ``tarfile``, ``pickletools``, ``struct``,
  ``json``); no ``torch`` import, so it runs in the portal Lambda and on the
  build host.
* **Nothing is unpickled.** The pickle envelope is walked with
  ``pickletools.genops`` through a literal-only stack machine: ints, floats,
  strings, ``None``/bools, tuples, lists, dicts, sets and memo get/put are
  interpreted; ``GLOBAL``/``REDUCE``/``NEWOBJ``/``BUILD``/``BINPERSID`` become
  opaque nodes that *record* what they would have called, and nothing is ever
  imported or invoked.  Safe on untrusted files.
* **Never raises on bad bytes.** Any parse failure degrades to
  ``kind='unknown'`` with the error recorded under ``evidence``.

Kinds and how they are told apart
---------------------------------
``onnx``
    Protobuf ``ModelProto`` header: byte 0 is ``0x08`` (field 1 varint
    ``ir_version``) followed by ``producer_name`` / ``opset_import`` / the
    ``graph`` field.  The whole file is then scanned field-by-field (seeking
    over length-delimited blobs, so initializers are never read) to collect
    the graph outputs and ``metadata_props`` — ultralytics exports write
    ``names`` there, which is the only place an ONNX carries class names.
``torchscript``
    Torch zip with ``constants.pkl`` **and** ``code/``; every ``data.pkl``
    GLOBAL is a mangled ``__torch__.*`` class.  Frozen graph — not
    fine-tunable by either trainer.
``ultralytics_checkpoint``
    Torch zip whose ``data.pkl`` references ``ultralytics.nn.tasks.*``.  The
    pickled model object's ``names`` / ``nc`` / ``yaml`` attributes are plain
    literals in the pickle and are recovered from the BUILD state.
``rfdetr_checkpoint``
    Torch zip whose top level is ``{model: <raw state_dict>, args: ...}``.
    **No** ``rfdetr``/``lwdetr`` GLOBAL appears in real files (only
    ``argparse.Namespace`` / ``collections.OrderedDict`` / ``torch.*Storage`` /
    ``torch._utils._rebuild_tensor_v2``), so the signal is the state_dict
    member names (``class_embed.*``, ``transformer.enc_out_class_embed.*``,
    ``backbone.0.encoder.*``) plus the ``args`` field set.  ``num_classes`` is
    ``len(class_embed.bias) - 1`` read from the ``_rebuild_tensor_v2`` size
    argument — exactly what ``rfdetr.models.weights.load_pretrain_weights``
    does; ``args.num_classes`` is a stale CLI default and is ignored.  Both
    layouts are accepted: published ``{model, optimizer, lr_scheduler, epoch,
    args(Namespace)}`` and the 1.10.1 ``BestModelCallback`` layout ``{model,
    args(dict), epoch, callbacks, model_name, model_config[, ema_model]}``
    (which carries ``args.class_names``), as is the PTL ``.ckpt`` layout the
    loader normalises (``state_dict`` with ``model.`` prefix +
    ``hyper_parameters``).
``state_dict``
    Torch zip (or bare pickle) holding only tensors — a raw ``OrderedDict`` of
    tensors, or a dict whose ``state_dict``/``model`` member is one — with no
    framework GLOBAL and no RF-DETR head.  No architecture definition, so not
    fine-tunable here.
``legacy_torch``
    Pre-zip ``torch.save``: either the tar form (``sys_info`` / ``pickle`` /
    ``tensors`` / ``storages`` members, e.g. torchvision's
    ``resnet18-5c106cde.pth``) or the pickle-stream form that opens with the
    ``0x1950a86a20f9469cfc6c`` magic.
``unknown``
    Anything else (gzip tarballs, ELF, empty/corrupt files, pickles that are
    not tensors).

``fine_tunable`` is ``True`` only for ``ultralytics_checkpoint`` (loadable by
``YOLO(path)``) and ``rfdetr_checkpoint`` (loadable by
``RFDETR*(pretrain_weights=path)``).

CLI: ``python3 checkpoint_probe.py <file> [<file> ...]`` prints one JSON
object per path.
"""
from __future__ import annotations

import io
import json
import os
import pickletools
import sys
import tarfile
import zipfile
from typing import Any, Iterable

__all__ = ["classify_checkpoint", "KINDS", "ARCHES", "FINE_TUNABLE_KINDS"]

KINDS = (
    "onnx",
    "torchscript",
    "ultralytics_checkpoint",
    "rfdetr_checkpoint",
    "state_dict",
    "legacy_torch",
    "unknown",
)
ARCHES = ("yolo", "rf_detr", None)
# The only kinds a portal trainer can continue training from (Requirement 7.5):
# ``YOLO(path)`` for ultralytics, ``RFDETR*(pretrain_weights=path)`` for RF-DETR.
FINE_TUNABLE_KINDS = ("ultralytics_checkpoint", "rfdetr_checkpoint")
# Kinds for which ``arch`` may be non-None (ONNX keeps a best-effort arch from
# ultralytics ``metadata_props``; it is still never fine-tunable).
_ARCH_BEARING_KINDS = FINE_TUNABLE_KINDS + ("onnx",)

# Hard caps so a hostile file cannot make the probe run away.
_MAX_PICKLE_OPS = 3_000_000
_MAX_BARE_PICKLE_BYTES = 256 * 1024 * 1024
_MAX_ONNX_FIELDS = 5_000_000

# Legacy (non-zip, non-tar) torch.save opens with this LONG as its first pickle.
_TORCH_LEGACY_MAGIC = 0x1950A86A20F9469CFC6C

_RFDETR_HEAD_KEYS = ("class_embed.weight", "class_embed.bias")
_RFDETR_ARGS_FIELDS = ("num_queries", "group_detr", "encoder", "resolution")
_STORAGE_GLOBAL_PREFIXES = (
    "torch._utils._rebuild",
    "torch.storage._load_from_bytes",
    "collections.OrderedDict",
    "argparse.Namespace",
    "types.SimpleNamespace",
    "numpy.core.multiarray",
    "numpy.dtype",
    "torch.serialization._get_layout",
)


# --------------------------------------------------------------------------- #
# Literal-only pickle stack machine (lifted from the 1.1 safe_pickle_tree.py)  #
# --------------------------------------------------------------------------- #


class _Mark:
    """Stack sentinel for MARK."""


class _Opaque:
    """Anything the machine refuses to execute.

    ``kind`` is one of ``global`` / ``reduce`` / ``newobj`` / ``persid`` /
    ``unsupported``.  ``items`` receives SETITEM(S)/APPEND(S) applied to the
    object (this is how a REDUCE'd ``collections.OrderedDict`` gets its
    entries), ``state`` receives the BUILD argument.
    """

    __slots__ = ("kind", "callable", "args", "state", "items")

    def __init__(self, kind: str, callable_: str | None = None, args: Any = None):
        self.kind = kind
        self.callable = callable_
        self.args = args
        self.state: Any = None
        self.items: dict = {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.kind} {self.callable}>"


class _PickleTruncated(Exception):
    pass


_LITERAL_OPS = frozenset({
    "BININT", "BININT1", "BININT2", "LONG", "LONG1", "LONG4", "INT",
    "BINFLOAT", "FLOAT", "SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8",
    "UNICODE", "STRING", "SHORT_BINSTRING", "BINSTRING",
})
_BYTES_OPS = frozenset({"SHORT_BINBYTES", "BINBYTES", "BINBYTES8", "BYTEARRAY8"})


def _pop_mark(stack: list) -> list:
    items = []
    while stack and stack[-1] is not _Mark:
        items.append(stack.pop())
    if stack:
        stack.pop()
    items.reverse()
    return items


def _key(k: Any) -> Any:
    if isinstance(k, (str, int, float, bool)) or k is None:
        return k
    if isinstance(k, tuple):
        return tuple(_key(i) for i in k)
    return repr(k)


def _set_item(container: Any, k: Any, v: Any) -> None:
    if isinstance(container, _Opaque):
        container.items[_key(k)] = v
    elif isinstance(container, dict):
        container[_key(k)] = v
    else:
        raise TypeError(f"SETITEM on {type(container).__name__}")


def _append(container: Any, v: Any) -> None:
    if isinstance(container, list):
        container.append(v)
    elif isinstance(container, _Opaque):
        container.items[len(container.items)] = v
    else:
        raise TypeError(f"APPEND on {type(container).__name__}")


def _walk_pickle(fobj, globals_out: set[str]) -> tuple[Any, int]:
    """Run one pickle from ``fobj`` through the literal machine.

    Returns ``(top_of_stack, n_ops)``.  ``globals_out`` collects every dotted
    name a GLOBAL/STACK_GLOBAL referenced.  Raises on malformed input (the
    caller converts that to ``kind='unknown'``).
    """
    stack: list = []
    memo: dict = {}
    n_ops = 0
    for op, arg, _pos in pickletools.genops(fobj):
        n_ops += 1
        if n_ops > _MAX_PICKLE_OPS:
            raise _PickleTruncated(f"more than {_MAX_PICKLE_OPS} opcodes")
        n = op.name
        if n in _LITERAL_OPS:
            stack.append(arg)
        elif n in _BYTES_OPS:
            stack.append(f"<bytes:{len(arg)}>")
        elif n == "NONE":
            stack.append(None)
        elif n == "NEWTRUE":
            stack.append(True)
        elif n == "NEWFALSE":
            stack.append(False)
        elif n == "MARK":
            stack.append(_Mark)
        elif n == "EMPTY_DICT":
            stack.append({})
        elif n == "EMPTY_LIST":
            stack.append([])
        elif n == "EMPTY_TUPLE":
            stack.append(())
        elif n == "EMPTY_SET":
            stack.append(_Opaque("set"))
        elif n == "TUPLE1":
            stack.append((stack.pop(),))
        elif n == "TUPLE2":
            b = stack.pop()
            a = stack.pop()
            stack.append((a, b))
        elif n == "TUPLE3":
            c = stack.pop()
            b = stack.pop()
            a = stack.pop()
            stack.append((a, b, c))
        elif n == "TUPLE":
            stack.append(tuple(_pop_mark(stack)))
        elif n == "LIST":
            stack.append(list(_pop_mark(stack)))
        elif n == "FROZENSET":
            o = _Opaque("frozenset")
            for i, v in enumerate(_pop_mark(stack)):
                o.items[i] = v
            stack.append(o)
        elif n == "DICT":
            items = _pop_mark(stack)
            d: dict = {}
            for i in range(0, len(items) - 1, 2):
                d[_key(items[i])] = items[i + 1]
            stack.append(d)
        elif n == "SETITEM":
            v = stack.pop()
            k = stack.pop()
            _set_item(stack[-1], k, v)
        elif n == "SETITEMS":
            items = _pop_mark(stack)
            for i in range(0, len(items) - 1, 2):
                _set_item(stack[-1], items[i], items[i + 1])
        elif n == "APPEND":
            # Pop the value *before* taking the container: Python evaluates
            # call arguments left to right, so ``_append(stack[-1],
            # stack.pop())`` would hand the value in as the container.
            v = stack.pop()
            _append(stack[-1], v)
        elif n == "APPENDS":
            for v in _pop_mark(stack):
                _append(stack[-1], v)
        elif n == "ADDITEMS":
            for v in _pop_mark(stack):
                _append(stack[-1], v)
        elif n in ("BINPUT", "LONG_BINPUT", "PUT"):
            memo[arg] = stack[-1]
        elif n == "MEMOIZE":
            memo[len(memo)] = stack[-1]
        elif n in ("BINGET", "LONG_BINGET", "GET"):
            stack.append(memo[arg])
        elif n == "GLOBAL":
            name = arg.replace(" ", ".")
            globals_out.add(name)
            stack.append(_Opaque("global", name))
        elif n == "STACK_GLOBAL":
            attr = stack.pop()
            mod = stack.pop()
            name = f"{mod}.{attr}"
            globals_out.add(name)
            stack.append(_Opaque("global", name))
        elif n == "REDUCE":
            args = stack.pop()
            fn = stack.pop()
            fn_name = fn.callable if isinstance(fn, _Opaque) else repr(fn)
            stack.append(_Opaque("reduce", fn_name, args))
        elif n in ("NEWOBJ", "NEWOBJ_EX"):
            if n == "NEWOBJ_EX":
                stack.pop()  # kwargs
            args = stack.pop()
            cls = stack.pop()
            cls_name = cls.callable if isinstance(cls, _Opaque) else repr(cls)
            stack.append(_Opaque("newobj", cls_name, args))
        elif n in ("OBJ", "INST"):
            # Protocol-0/1 object construction; keep the class name only.
            if n == "INST":
                _pop_mark(stack)
                name = arg.replace(" ", ".")
            else:
                items = _pop_mark(stack)
                cls = items[0] if items else None
                name = cls.callable if isinstance(cls, _Opaque) else repr(cls)
            globals_out.add(name)
            stack.append(_Opaque("newobj", name, ()))
        elif n == "BUILD":
            state = stack.pop()
            obj = stack[-1]
            if isinstance(obj, _Opaque):
                obj.state = state
            else:
                o = _Opaque("built", type(obj).__name__)
                o.state = state
                stack[-1] = o
        elif n in ("BINPERSID", "PERSID"):
            pid = stack.pop() if n == "BINPERSID" else arg
            stack.append(_Opaque("persid", None, pid))
        elif n == "POP":
            stack.pop()
        elif n == "POP_MARK":
            _pop_mark(stack)
        elif n == "DUP":
            stack.append(stack[-1])
        elif n in ("PROTO", "FRAME"):
            pass
        elif n == "STOP":
            break
        else:
            # EXT1/2/4, NEXT_BUFFER, READONLY_BUFFER, ... — record and go on.
            stack.append(_Opaque("unsupported", n))
    if not stack:
        raise ValueError("empty pickle")
    return stack[-1], n_ops


# --------------------------------------------------------------------------- #
# Tree helpers                                                                 #
# --------------------------------------------------------------------------- #


def _is_reduce_of(node: Any, prefix: str) -> bool:
    return (
        isinstance(node, _Opaque)
        and node.kind in ("reduce", "newobj")
        and isinstance(node.callable, str)
        and node.callable.startswith(prefix)
    )


def _tensor_shape(node: Any) -> list | None:
    """Shape of a ``torch._utils._rebuild_tensor_v2`` / ``_rebuild_parameter`` node.

    ``_rebuild_tensor_v2(storage, storage_offset, size, stride, requires_grad,
    backward_hooks[, metadata])``; ``size`` is a tuple (or a REDUCE of
    ``torch.Size``).  ``_rebuild_parameter(data, requires_grad, backward_hooks)``
    wraps a tensor node.
    """
    if not isinstance(node, _Opaque) or node.kind != "reduce":
        return None
    fn = node.callable or ""
    if fn == "torch._utils._rebuild_parameter":
        args = node.args if isinstance(node.args, tuple) else ()
        return _tensor_shape(args[0]) if args else None
    if not fn.startswith("torch._utils._rebuild_tensor"):
        return None
    args = node.args if isinstance(node.args, tuple) else ()
    if len(args) < 3:
        return None
    size = args[2]
    if isinstance(size, _Opaque) and size.kind == "reduce":
        inner = size.args if isinstance(size.args, tuple) else ()
        size = inner[0] if inner else None
    if isinstance(size, (list, tuple)) and all(isinstance(d, int) for d in size):
        return list(size)
    return None


def _is_tensor(node: Any) -> bool:
    return _tensor_shape(node) is not None


def _mapping_items(node: Any) -> dict | None:
    """View a plain dict or a REDUCE'd OrderedDict as ``{key: value}``."""
    if isinstance(node, dict):
        return node
    if isinstance(node, _Opaque) and _is_reduce_of(node, "collections.OrderedDict"):
        return node.items
    return None


def _tensor_mapping(node: Any) -> dict | None:
    """Return ``{name: shape}`` if *node* is a mapping of (mostly) tensors."""
    items = _mapping_items(node)
    if not items:
        return None
    shapes = {}
    n_other = 0
    for k, v in items.items():
        s = _tensor_shape(v)
        if s is None:
            n_other += 1
        else:
            shapes[str(k)] = s
    # A state_dict is overwhelmingly tensors; tolerate a few scalars
    # (LFV's mochi.pth carries `anomaly_score_threshold`).
    if not shapes or n_other > max(2, len(shapes) // 10):
        return None
    return shapes


def _state_of(node: Any) -> dict | None:
    if isinstance(node, _Opaque) and isinstance(node.state, dict):
        return node.state
    return None


def _namespace_fields(node: Any) -> dict | None:
    """Fields of an ``args`` value stored as a dict or an ``argparse.Namespace``."""
    if isinstance(node, dict):
        return node
    if isinstance(node, _Opaque) and node.kind in ("reduce", "newobj"):
        st = node.state
        if isinstance(st, dict):
            return st
        # Namespace pickled with protocol 2 REDUCE + BUILD(state) — handled
        # above; some pickles use a (dict, slotstate) tuple.
        if isinstance(st, tuple) and st and isinstance(st[0], dict):
            return st[0]
    return None


def _clean_class_names(raw: Any) -> list[str] | None:
    """Normalise ultralytics ``names`` ({int: str}) or RF-DETR ``class_names`` (list/str)."""
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, dict):
        try:
            ordered = sorted(raw.items(), key=lambda kv: int(kv[0]))
        except (TypeError, ValueError):
            ordered = list(raw.items())
        names = [str(v) for _k, v in ordered]
        return names or None
    if isinstance(raw, (list, tuple)):
        names = [n for n in raw if isinstance(n, str)]
        return names or None
    return None


# --------------------------------------------------------------------------- #
# Kind-specific extraction                                                     #
# --------------------------------------------------------------------------- #


def _classify_torch_tree(tree: Any, globals_: set[str], result: dict) -> None:
    """Fill ``result`` from the literal tree of a torch ``data.pkl``."""
    ev = result["evidence"]
    ev["pickle_globals"] = sorted(globals_)
    top = _mapping_items(tree)
    if top is not None:
        ev["top_level_keys"] = [str(k) for k in top.keys()][:64]

    # ---- ultralytics: full pickled DetectionModel under `model` (or `ema`) ----
    if any(g.startswith("ultralytics.") for g in globals_):
        result["kind"] = "ultralytics_checkpoint"
        result["arch"] = "yolo"
        result["fine_tunable"] = True
        model = None
        if top is not None:
            for k in ("model", "ema"):
                cand = top.get(k)
                if _is_reduce_of(cand, "ultralytics."):
                    model = cand
                    ev["model_member"] = k
                    break
            if model is None:
                for k in ("model", "ema"):
                    if isinstance(top.get(k), _Opaque):
                        model = top[k]
                        ev["model_member"] = k
                        break
        elif _is_reduce_of(tree, "ultralytics."):
            model = tree
            ev["model_member"] = "<root>"
        if model is not None:
            ev["model_class"] = model.callable
            st = _state_of(model) or {}
            names = _clean_class_names(st.get("names"))
            nc = st.get("nc")
            yaml = st.get("yaml")
            if not isinstance(nc, int) and isinstance(yaml, dict) and isinstance(yaml.get("nc"), int):
                nc = yaml["nc"]
            if not isinstance(nc, int) and names:
                nc = len(names)
            result["class_names"] = names
            result["num_classes"] = nc if isinstance(nc, int) else None
            if isinstance(yaml, dict):
                ev["yaml_file"] = yaml.get("yaml_file")
            ev["names_source"] = "model.<state>.names" if names else None
        if top is not None:
            ta = top.get("train_args")
            if isinstance(ta, dict):
                ev["train_args"] = {k: ta.get(k) for k in ("task", "model", "imgsz", "epochs", "batch") if k in ta}
            for k in ("version", "date", "epoch"):
                if k in top and isinstance(top[k], (str, int, float)):
                    ev[k] = top[k]
        return

    # ---- RF-DETR: {model: state_dict, args: ...} (+ PTL .ckpt normalisation) ----
    if top is not None:
        sd_node = top.get("model")
        sd = _tensor_mapping(sd_node)
        prefix = ""
        if sd is None and "state_dict" in top:
            cand = _tensor_mapping(top.get("state_dict"))
            if cand and any(k.startswith("model.") for k in cand):
                sd = {k[len("model."):]: v for k, v in cand.items() if k.startswith("model.")}
                prefix = "model."
        args_node = top.get("args", top.get("hyper_parameters"))
        args = _namespace_fields(args_node)
        if sd is not None and all(k in sd for k in _RFDETR_HEAD_KEYS):
            enc_out = [k for k in sd if k.startswith("transformer.enc_out_class_embed.")]
            backbone = [k for k in sd if k.startswith("backbone.0.encoder.")]
            args_hits = [f for f in _RFDETR_ARGS_FIELDS if isinstance(args, dict) and f in args]
            ev["rfdetr_signals"] = {
                "class_embed": True,
                "enc_out_class_embed_groups": len({k.split(".")[2] for k in enc_out}) if enc_out else 0,
                "backbone.0.encoder.*": len(backbone),
                "args_fields": args_hits,
                "args_type": (args_node.callable if isinstance(args_node, _Opaque) else type(args_node).__name__),
                "state_dict_prefix": prefix,
                "n_tensors": len(sd),
            }
            if enc_out or backbone or args_hits:
                result["kind"] = "rfdetr_checkpoint"
                result["arch"] = "rf_detr"
                result["fine_tunable"] = True
                bias = sd.get("class_embed.bias")
                if bias and len(bias) == 1 and bias[0] > 0:
                    result["num_classes"] = bias[0] - 1
                    ev["num_classes_source"] = "class_embed.bias.shape[0] - 1"
                names = None
                if isinstance(args, dict):
                    names = _clean_class_names(args.get("class_names"))
                    if names:
                        ev["names_source"] = "args.class_names"
                    for k in ("encoder", "resolution", "num_queries", "group_detr", "num_classes"):
                        if k in args and isinstance(args[k], (str, int, float, bool, type(None))):
                            ev.setdefault("args_picks", {})[k] = args[k]
                result["class_names"] = names
                for k in ("model_name", "epoch"):
                    if k in top and isinstance(top[k], (str, int)):
                        ev[k] = top[k]
                mc = top.get("model_config")
                if isinstance(mc, dict) and isinstance(mc.get("num_classes"), int):
                    ev["model_config.num_classes"] = mc["num_classes"]
                return

    # ---- raw state_dict / plain dict of tensors ----
    framework = sorted(g for g in globals_ if not g.startswith(_STORAGE_GLOBAL_PREFIXES) and not g.startswith("torch.") and not g.startswith("__torch__."))
    ev["non_torch_globals"] = framework
    if not framework:
        sd = _tensor_mapping(tree)
        member = "<root>"
        if sd is None and top is not None:
            for k in ("state_dict", "model", "model_state_dict", "weights", "ema"):
                sd = _tensor_mapping(top.get(k))
                if sd is not None:
                    member = k
                    break
        if sd is not None:
            result["kind"] = "state_dict"
            ev["state_dict_member"] = member
            keys = list(sd.keys())
            ev["n_tensors"] = len(keys)
            ev["first_keys"] = keys[:5]
            return
    # A pickled object of some other framework, or a dict of non-tensor
    # literals: not a checkpoint we know how to name.
    result["kind"] = "unknown"


# --------------------------------------------------------------------------- #
# Containers                                                                   #
# --------------------------------------------------------------------------- #


def _zip_base(name: str) -> str:
    """Member name with torch's single top-level folder (``best/``) removed."""
    return name.split("/", 1)[1] if "/" in name else name


def _classify_zip(path: str, result: dict) -> None:
    ev = result["evidence"]
    ev["container"] = "zip"
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        base = {_zip_base(n): n for n in names}
        markers = {
            "data.pkl": "data.pkl" in base,
            "constants.pkl": "constants.pkl" in base,
            "code/": any(b.startswith("code/") for b in base),
            "version": "version" in base,
            "byteorder": "byteorder" in base,
            "n_storages": sum(1 for b in base if b.startswith("data/")),
        }
        ev["zip_markers"] = markers
        ev["zip_root"] = sorted({n.split("/")[0] for n in names if "/" in n})[:3]
        if not markers["data.pkl"]:
            result["kind"] = "unknown"
            ev["note"] = "zip without data.pkl (not a torch archive)"
            return
        globals_: set[str] = set()
        tree = None
        try:
            with z.open(base["data.pkl"]) as fobj:
                tree, n_ops = _walk_pickle(fobj, globals_)
            ev["pickle_ops"] = n_ops
        except Exception as exc:  # noqa: BLE001 - untrusted bytes; degrade
            ev["pickle_error"] = f"{type(exc).__name__}: {exc}"
            ev["pickle_globals"] = sorted(globals_)
        if markers["constants.pkl"] and markers["code/"]:
            result["kind"] = "torchscript"
            ev["pickle_globals"] = sorted(globals_)[:12]
            ev["torchscript_globals_all_mangled"] = bool(globals_) and all(
                g.startswith("__torch__.") for g in globals_
            )
            ev["code_files"] = [b for b in base if b.startswith("code/") and b.endswith(".py")][:5]
            return
        if tree is None:
            result["kind"] = "unknown"
            return
        _classify_torch_tree(tree, globals_, result)


def _classify_tar(path: str, result: dict) -> None:
    ev = result["evidence"]
    ev["container"] = "tar"
    with tarfile.open(path) as t:
        members = t.getmembers()
        leaf = {m.name.split("/")[-1]: m for m in members}
        ev["tar_members"] = [m.name for m in members][:8]
        if {"sys_info", "pickle", "tensors", "storages"} <= set(leaf):
            result["kind"] = "legacy_torch"
            pk = leaf["pickle"]
            fobj = t.extractfile(pk)
            if fobj is not None and pk.size <= _MAX_BARE_PICKLE_BYTES:
                globals_: set[str] = set()
                try:
                    tree, n_ops = _walk_pickle(fobj, globals_)
                    ev["pickle_ops"] = n_ops
                    ev["pickle_globals"] = sorted(globals_)
                    sd = _tensor_mapping(tree)
                    if sd is not None:
                        ev["n_tensors"] = len(sd)
                        ev["first_keys"] = list(sd)[:5]
                except Exception as exc:  # noqa: BLE001
                    ev["pickle_error"] = f"{type(exc).__name__}: {exc}"
            return
    result["kind"] = "unknown"
    ev["note"] = "tar without legacy torch members"


def _classify_bare_pickle(path: str, result: dict) -> None:
    """A file that starts with a pickle: legacy torch stream or plain pickle."""
    ev = result["evidence"]
    ev["container"] = "pickle"
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        globals_: set[str] = set()
        first, n_ops = _walk_pickle(f, globals_)
        if isinstance(first, int) and first == _TORCH_LEGACY_MAGIC:
            # torch legacy stream: MAGIC, PROTOCOL_VERSION, sys_info, obj, keys, storages...
            result["kind"] = "legacy_torch"
            ev["torch_legacy_magic"] = True
            try:
                proto, _ = _walk_pickle(f, globals_)
                ev["torch_legacy_protocol"] = proto
                _walk_pickle(f, globals_)  # sys_info
                tree, n2 = _walk_pickle(f, globals_)
                ev["pickle_ops"] = n_ops + n2
                ev["pickle_globals"] = sorted(globals_)
                sd = _tensor_mapping(tree)
                if sd is not None:
                    ev["n_tensors"] = len(sd)
                    ev["first_keys"] = list(sd)[:5]
                elif any(g.startswith("ultralytics.") for g in globals_):
                    ev["note"] = "legacy stream pickles an ultralytics model; not accepted (pre-zip format)"
            except Exception as exc:  # noqa: BLE001
                ev["pickle_error"] = f"{type(exc).__name__}: {exc}"
            return
        ev["pickle_ops"] = n_ops
        ev["file_size"] = size
        _classify_torch_tree(first, globals_, result)


# --------------------------------------------------------------------------- #
# ONNX (protobuf) — header sniff lifted from 1.1 inspect_weights.py, plus a    #
# seek-over-blobs scan for graph outputs and metadata_props.                   #
# --------------------------------------------------------------------------- #


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _read_varint(f) -> int | None:
    shift = 0
    val = 0
    while True:
        b = f.read(1)
        if not b:
            return None
        c = b[0]
        val |= (c & 0x7F) << shift
        if not (c & 0x80):
            return val
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _skip_wire(f, wt: int) -> None:
    if wt == 0:
        _read_varint(f)
    elif wt == 1:
        f.seek(8, io.SEEK_CUR)
    elif wt == 2:
        ln = _read_varint(f)
        if ln is None:
            raise EOFError
        f.seek(ln, io.SEEK_CUR)
    elif wt == 5:
        f.seek(4, io.SEEK_CUR)
    else:
        raise ValueError(f"unsupported wire type {wt}")


def _sniff_onnx_header(head: bytes) -> dict:
    """Top-level ModelProto fields that precede the graph (cheap, in-memory)."""
    out: dict = {}
    i = 0
    try:
        while i < len(head):
            tag, i = _varint(head, i)
            fnum, wt = tag >> 3, tag & 7
            if wt == 0:
                v, i = _varint(head, i)
                if fnum == 1:
                    out["ir_version"] = v
                elif fnum == 5:
                    out["model_version"] = v
            elif wt == 2:
                ln, i = _varint(head, i)
                if fnum in (2, 3, 4, 6) and i + ln <= len(head):
                    out[{2: "producer_name", 3: "producer_version", 4: "domain", 6: "doc_string"}[fnum]] = head[i:i + ln].decode("utf-8", "replace")
                elif fnum == 7:
                    out["graph_at"] = i
                    break
                i += ln
            elif wt == 1:
                i += 8
            elif wt == 5:
                i += 4
            else:
                break
    except (IndexError, ValueError):
        out["header_truncated"] = True  # ran off the 4 KiB head; keep what we have
    return out


def _looks_like_onnx(head: bytes) -> bool:
    if len(head) < 4 or head[0] != 0x08:
        return False
    info = _sniff_onnx_header(head)
    return "ir_version" in info and 1 <= info["ir_version"] <= 64 and (
        "producer_name" in info or "graph_at" in info or "opset_import" in info
    )


def _parse_value_info(buf: bytes) -> dict:
    """ValueInfoProto → {name, shape}; dims are ints or dim_param strings."""
    out: dict = {"name": None, "shape": None}
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        fnum, wt = tag >> 3, tag & 7
        if wt != 2:
            if wt == 0:
                _v, i = _varint(buf, i)
            elif wt == 1:
                i += 8
            elif wt == 5:
                i += 4
            else:
                break
            continue
        ln, i = _varint(buf, i)
        sub = buf[i:i + ln]
        i += ln
        if fnum == 1:
            out["name"] = sub.decode("utf-8", "replace")
        elif fnum == 2:  # TypeProto
            j = 0
            while j < len(sub):
                t2, j = _varint(sub, j)
                f2, w2 = t2 >> 3, t2 & 7
                if w2 != 2:
                    if w2 == 0:
                        _v, j = _varint(sub, j)
                    else:
                        break
                    continue
                l2, j = _varint(sub, j)
                tt = sub[j:j + l2]
                j += l2
                if f2 != 1:  # tensor_type only
                    continue
                k = 0
                while k < len(tt):
                    t3, k = _varint(tt, k)
                    f3, w3 = t3 >> 3, t3 & 7
                    if w3 == 0:
                        _v, k = _varint(tt, k)
                        continue
                    if w3 != 2:
                        break
                    l3, k = _varint(tt, k)
                    if f3 == 2:  # TensorShapeProto
                        shape_buf = tt[k:k + l3]
                        dims: list = []
                        m = 0
                        while m < len(shape_buf):
                            t4, m = _varint(shape_buf, m)
                            f4, w4 = t4 >> 3, t4 & 7
                            if w4 != 2:
                                break
                            l4, m = _varint(shape_buf, m)
                            dim = shape_buf[m:m + l4]
                            m += l4
                            if f4 != 1:
                                continue
                            dv: Any = None
                            p = 0
                            while p < len(dim):
                                t5, p = _varint(dim, p)
                                f5, w5 = t5 >> 3, t5 & 7
                                if w5 == 0:
                                    v5, p = _varint(dim, p)
                                    if f5 == 1:
                                        dv = v5
                                elif w5 == 2:
                                    l5, p = _varint(dim, p)
                                    if f5 == 2:
                                        dv = dim[p:p + l5].decode("utf-8", "replace")
                                    p += l5
                                else:
                                    break
                            dims.append(dv)
                        out["shape"] = dims
                    k += l3
    return out


def _scan_onnx(path: str, head: bytes, result: dict) -> None:
    """Fill ``result`` for an ONNX file: header, opsets, graph outputs, metadata_props."""
    ev = result["evidence"]
    result["kind"] = "onnx"
    ev["container"] = "protobuf"
    ev["onnx"] = _sniff_onnx_header(head)
    ev["onnx"].pop("graph_at", None)
    outputs: list[dict] = []
    metadata: dict[str, str] = {}
    opsets: list[dict] = []
    n_fields = 0
    try:
        with open(path, "rb") as f:
            end = os.path.getsize(path)
            while f.tell() < end:
                n_fields += 1
                if n_fields > _MAX_ONNX_FIELDS:
                    raise _PickleTruncated("too many protobuf fields")
                tag = _read_varint(f)
                if tag is None:
                    break
                fnum, wt = tag >> 3, tag & 7
                if wt != 2:
                    _skip_wire(f, wt)
                    continue
                ln = _read_varint(f)
                if ln is None:
                    break
                if fnum == 7:  # graph: walk its fields, only read outputs (12)
                    g_end = f.tell() + ln
                    while f.tell() < g_end:
                        n_fields += 1
                        if n_fields > _MAX_ONNX_FIELDS:
                            raise _PickleTruncated("too many protobuf fields")
                        t2 = _read_varint(f)
                        if t2 is None:
                            break
                        f2, w2 = t2 >> 3, t2 & 7
                        if w2 != 2:
                            _skip_wire(f, w2)
                            continue
                        l2 = _read_varint(f)
                        if l2 is None:
                            break
                        if f2 == 12 and l2 <= 65536:
                            outputs.append(_parse_value_info(f.read(l2)))
                        else:
                            f.seek(l2, io.SEEK_CUR)
                    f.seek(g_end)
                elif fnum == 8 and ln <= 4096:  # opset_import
                    sub = f.read(ln)
                    j = 0
                    dom, ver = "", None
                    while j < len(sub):
                        t2, j = _varint(sub, j)
                        f2, w2 = t2 >> 3, t2 & 7
                        if w2 == 2:
                            l2, j = _varint(sub, j)
                            if f2 == 1:
                                dom = sub[j:j + l2].decode("utf-8", "replace")
                            j += l2
                        elif w2 == 0:
                            v2, j = _varint(sub, j)
                            if f2 == 2:
                                ver = v2
                        else:
                            break
                    opsets.append({"domain": dom, "version": ver})
                elif fnum == 14 and ln <= 1 << 20:  # metadata_props: StringStringEntryProto
                    sub = f.read(ln)
                    j = 0
                    key = val = None
                    while j < len(sub):
                        t2, j = _varint(sub, j)
                        f2, w2 = t2 >> 3, t2 & 7
                        if w2 != 2:
                            break
                        l2, j = _varint(sub, j)
                        s = sub[j:j + l2].decode("utf-8", "replace")
                        j += l2
                        if f2 == 1:
                            key = s
                        elif f2 == 2:
                            val = s
                    if key is not None:
                        metadata[key] = val if val is not None else ""
                else:
                    f.seek(ln, io.SEEK_CUR)
    except Exception as exc:  # noqa: BLE001
        ev["onnx_scan_error"] = f"{type(exc).__name__}: {exc}"
    if opsets:
        ev["onnx"]["opset_import"] = opsets
    if outputs:
        ev["onnx"]["outputs"] = outputs
    if metadata:
        ev["onnx"]["metadata_props"] = {k: (v if len(v) <= 400 else v[:400] + "…") for k, v in metadata.items()}

    # ---- what is recoverable: names only from ultralytics metadata_props ----
    names = None
    raw_names = metadata.get("names")
    if raw_names:
        parsed = _parse_python_literal_dict(raw_names)
        names = _clean_class_names(parsed) if parsed is not None else None
        if names:
            ev["names_source"] = "metadata_props.names"
            result["arch"] = "yolo" if {"stride", "task"} & set(metadata) else None
    result["class_names"] = names
    if names:
        result["num_classes"] = len(names)
        ev["num_classes_source"] = "len(metadata_props.names)"
    else:
        # Single [1, 4+nc, anchors] output → YOLOv8-family head (heuristic).
        det = [o for o in outputs if isinstance(o.get("shape"), list) and len(o["shape"]) == 3]
        if len(outputs) == 1 and det:
            s = det[0]["shape"]
            if all(isinstance(d, int) for d in s) and s[0] == 1 and 4 < s[1] < s[2]:
                result["num_classes"] = s[1] - 4
                ev["num_classes_source"] = "output shape [1, 4+nc, anchors] (heuristic)"


def _parse_python_literal_dict(text: str) -> Any:
    """ultralytics writes ``names`` as ``str(dict)``: ``{0: 'a', 1: 'b'}``.

    Try JSON first, then a tiny safe parser for the ``{int: 'str'}`` repr.
    Never uses ``eval``.
    """
    try:
        return json.loads(text)
    except ValueError:
        pass
    s = text.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    out: dict = {}
    body = s[1:-1].strip()
    if not body:
        return out
    i = 0
    n = len(body)
    try:
        while i < n:
            while i < n and body[i] in " ,":
                i += 1
            j = i
            while j < n and body[j] not in ":":
                j += 1
            key = int(body[i:j].strip())
            i = j + 1
            while i < n and body[i] == " ":
                i += 1
            q = body[i]
            if q not in "'\"":
                return None
            j = i + 1
            val_chars = []
            while j < n and body[j] != q:
                if body[j] == "\\" and j + 1 < n:
                    j += 1
                val_chars.append(body[j])
                j += 1
            out[key] = "".join(val_chars)
            i = j + 1
    except (ValueError, IndexError):
        return None
    return out


# --------------------------------------------------------------------------- #
# Public entry point                                                           #
# --------------------------------------------------------------------------- #


def _empty_result() -> dict:
    return {
        "kind": "unknown",
        "arch": None,
        "fine_tunable": False,
        "num_classes": None,
        "class_names": None,
        "evidence": {},
    }


def classify_checkpoint(path: str) -> dict:
    """Classify the weights file at *path* by envelope inspection only.

    Returns ``{kind, arch, fine_tunable, num_classes, class_names, evidence}``
    (see module docstring for the vocabulary).  Never raises for unreadable or
    corrupt content: those come back as ``kind='unknown'`` with the failure
    under ``evidence['error']``.
    """
    result = _empty_result()
    ev = result["evidence"]
    try:
        size = os.path.getsize(path)
        ev["size"] = size
        with open(path, "rb") as f:
            head = f.read(4096)
        ev["magic"] = head[:8].hex()
        if not head:
            ev["error"] = "empty file"
            return result
        if head[:2] == b"PK":
            _classify_zip(path, result)
        elif head[:8] == b"././@Pax" or (head[257:262] == b"ustar"):
            _classify_tar(path, result)
        elif head[:1] == b"\x80" and 2 <= head[1] <= 5:
            _classify_bare_pickle(path, result)
        elif head[:2] == b"\x1f\x8b":
            ev["container"] = "gzip"
            ev["note"] = "gzip (tarball?) — extract first"
        elif head[:4] == b"\x7fELF":
            ev["container"] = "elf"
            ev["note"] = "ELF shared object (Neo/TensorRT output), not a checkpoint"
        elif _looks_like_onnx(head):
            _scan_onnx(path, head, result)
        elif head[:1] in (b"(", b"c", b"]", b"}", b"I", b"S", b"U", b"N") and _maybe_proto0_pickle(path):
            _classify_bare_pickle(path, result)
        else:
            ev["container"] = "unknown"
    except Exception as exc:  # noqa: BLE001 - contract: never raise
        result["kind"] = "unknown"
        result["arch"] = None
        result["fine_tunable"] = False
        ev["error"] = f"{type(exc).__name__}: {exc}"
    if result["kind"] not in FINE_TUNABLE_KINDS:
        result["fine_tunable"] = False
    if result["kind"] not in _ARCH_BEARING_KINDS:
        result["arch"] = None
    return result


def _maybe_proto0_pickle(path: str) -> bool:
    """Cheap check for a protocol-0/1 pickle (text opcodes)."""
    try:
        with open(path, "rb") as f:
            ops = pickletools.genops(f)
            for _ in range(8):
                next(ops)
        return True
    except Exception:  # noqa: BLE001
        return False


def _main(argv: Iterable[str]) -> int:
    paths = list(argv)
    if not paths:
        print(__doc__.split("\n\n", 1)[0])
        print("usage: checkpoint_probe.py <file> [<file> ...]")
        return 2
    for p in paths:
        res = classify_checkpoint(p)
        res = {"path": p, **res}
        print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
