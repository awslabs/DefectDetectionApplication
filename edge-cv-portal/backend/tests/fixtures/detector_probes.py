"""``checkpoint_probe.classify_checkpoint`` outputs for the detector-checkpoint-
import tests (spec .kiro/specs/detector-checkpoint-import).

The ultralytics / RF-DETR dicts are the REAL evidence the probe returned for
the spike fixtures (docs/detector-checkpoint-import-spike.md), trimmed to the
fields ``detector_conversion.assess_checkpoint`` reads:

* PPE ``best.pt`` (huggingface.co/melihuzunoglu/ppe-detection, sha256
  a00b6fce...2119, saved by ultralytics 8.4.2);
* the published yolo26n / yolov10n / yolo11n-seg checkpoints;
* R2': the portal's own RF-DETR small ``checkpoint_best_total.pth``;
* R2: the published RF-DETR nano ``.pth`` (argparse.Namespace args, no class
  names, stale ``args.num_classes`` of 2 against a 91-slot head).
"""
import copy

BLOCKS = [
    "ultralytics.nn.modules.block.Attention", "ultralytics.nn.modules.block.Bottleneck",
    "ultralytics.nn.modules.block.C2PSA", "ultralytics.nn.modules.block.C3k",
    "ultralytics.nn.modules.block.C3k2", "ultralytics.nn.modules.block.DFL",
    "ultralytics.nn.modules.block.PSABlock", "ultralytics.nn.modules.block.SPPF",
    "ultralytics.nn.modules.conv.Concat", "ultralytics.nn.modules.conv.Conv",
    "ultralytics.nn.modules.conv.DWConv",
]
PPE_NAMES = ["helmet", "human", "no-helmet", "vest"]
PPE_SHA256 = "a00b6fce124e63c5d23f44792593983b70e646008b58bc54cf5b0a1c87ba2119"
PPE_BYTES = 5475290

PPE_PROBE = {
    "kind": "ultralytics_checkpoint", "arch": "yolo", "fine_tunable": True,
    "num_classes": 4, "class_names": list(PPE_NAMES),
    "evidence": {
        "model_class": "ultralytics.nn.tasks.DetectionModel",
        "pickle_globals": BLOCKS + ["ultralytics.nn.modules.head.Detect",
                                    "ultralytics.nn.tasks.DetectionModel"],
        "train_args": {"task": "detect", "model": "yolo11n.pt", "imgsz": 640, "epochs": 50,
                       "batch": 16},
        "version": "8.4.2", "model_member": "model",
    },
}


def ultralytics_probe(model_class, heads, task="detect", imgsz=640, names=None, nc=None,
                      version="8.4.2"):
    """A probe dict shaped like PPE_PROBE with the given model class / heads."""
    names = ["a", "b"] if names is None else names
    probe = copy.deepcopy(PPE_PROBE)
    probe["num_classes"] = len(names) if nc is None else nc
    probe["class_names"] = names
    ev = probe["evidence"]
    ev["model_class"] = f"ultralytics.nn.tasks.{model_class}"
    ev["pickle_globals"] = sorted(BLOCKS + [f"ultralytics.nn.modules.head.{h}" for h in heads]
                                  + [f"ultralytics.nn.tasks.{model_class}"])
    ev["train_args"] = {"task": task, "imgsz": imgsz} if task is not None else {"imgsz": imgsz}
    ev["version"] = version
    return probe


COCO_LIKE = [f"c{i}" for i in range(80)]
YOLO26N_PROBE = ultralytics_probe("DetectionModel", ["Detect"], names=COCO_LIKE, version="8.3.222")
YOLOV10N_PROBE = ultralytics_probe("DetectionModel", ["v10Detect"], names=COCO_LIKE,
                                   version="8.2.30")
YOLO11N_SEG_PROBE = ultralytics_probe("SegmentationModel", ["Detect", "Segment"], task="segment",
                                      names=COCO_LIKE, version="8.2.100")

RFDETR_OWN_PROBE = {
    "kind": "rfdetr_checkpoint", "arch": "rf_detr", "fine_tunable": True,
    "num_classes": 1, "class_names": ["blue_plate"],
    "evidence": {
        "pickle_globals": [], "model_name": "RFDETRSmall",
        "rfdetr_signals": {"class_embed": True, "enc_out_class_embed_groups": 13,
                           "backbone.0.encoder.*": 223, "args_fields": [], "args_type": "dict",
                           "state_dict_prefix": "", "n_tensors": 488},
    },
}
RFDETR_PUBLISHED_PROBE = {
    "kind": "rfdetr_checkpoint", "arch": "rf_detr", "fine_tunable": True,
    "num_classes": 90, "class_names": None,
    "evidence": {
        "pickle_globals": [],
        "args_picks": {"encoder": "dinov2_windowed_small", "resolution": 384, "num_queries": 300,
                       "group_detr": 13, "num_classes": 2},
        "rfdetr_signals": {"class_embed": True, "enc_out_class_embed_groups": 13,
                           "backbone.0.encoder.*": 223,
                           "args_fields": ["num_queries", "group_detr", "encoder", "resolution"],
                           "args_type": "argparse.Namespace", "state_dict_prefix": "",
                           "n_tensors": 465},
    },
}


def other_kind(kind):
    """A probe result for a file that is not an ultralytics / RF-DETR checkpoint."""
    return {"kind": kind, "arch": None, "fine_tunable": False, "num_classes": None,
            "class_names": None, "evidence": {}}
