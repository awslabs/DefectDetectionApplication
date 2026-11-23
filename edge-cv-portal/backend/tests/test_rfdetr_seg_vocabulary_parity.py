"""
Vocabulary parity for RF-DETR-Seg (spec rfdetr-seg-support, Requirement 1.6;
task 1.5): the Seg size table, class map, query counts and the tables derived
from them are identical wherever they are duplicated -- `detection_training.py`
and `detector_conversion.py` (shared layer, on sys.path through conftest) and
`datasets/detection_training/export_checkpoint.py` (the Conversion_Job,
imported from `datasets/` via importlib with the heavy libraries blocked, as
test_export_checkpoint_static.py does) and `train_rfdetr_seg.py` (the Seg
trainer, task 4.1: SEG_SIZE_CLASSES carries class, native, queries and block
per size, plus STAGE_TYPE and the thresholds) -- extending the role
test_detection_manifest_parity.py plays for the device manifest.

Grows with the plan, each assertion in the commit that creates the constant it
pins: the arch-vocabulary mirrors join in 7.1 (`checkpoint_probe.ARCHES`),
7.3 (`export_checkpoint.ARCHES` and `INPUT_BOUNDS['rf_detr_seg']`) and 8.3
(`model_import.FINE_TUNABLE_ARCHES`, the handler module imported from
`functions/` as the other handler suites do).

# Validates: Requirements 1.4, 1.6
"""
import importlib
import importlib.util
import os
import sys

import checkpoint_probe as cp
import detection_training as dt
import detector_conversion as dc
import model_import as mi

_HERE = os.path.dirname(os.path.abspath(__file__))
_DETECTION_TRAINING = os.path.abspath(
    os.path.join(_HERE, "..", "..", "..", "datasets", "detection_training"))
_HEAVY = ("ultralytics", "torch", "onnx", "onnxruntime", "rfdetr", "scipy", "cv2")
if _DETECTION_TRAINING not in sys.path:
    # Appended, not prepended (as test_train_rfdetr_static.py does): the Seg
    # trainer imports train_rfdetr and _common as plain siblings.
    sys.path.append(_DETECTION_TRAINING)


class _Blocked:
    """Meta-path finder that makes `import <name>` raise ImportError."""

    def __init__(self, names):
        self.names = set(names)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.names:
            raise ImportError(f"{fullname} is blocked in this test")
        return None


def _import_export_checkpoint():
    blocked = _Blocked(_HEAVY)
    sys.meta_path.insert(0, blocked)
    saved = {n: sys.modules.pop(n) for n in list(sys.modules) if n.split(".")[0] in _HEAVY}
    try:
        spec = importlib.util.spec_from_file_location(
            "dda_export_checkpoint_parity", os.path.join(_DETECTION_TRAINING, "export_checkpoint.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.meta_path.remove(blocked)
        sys.modules.update(saved)


def _import_train_rfdetr_seg():
    blocked = _Blocked(_HEAVY)
    sys.meta_path.insert(0, blocked)
    saved = {n: sys.modules.pop(n) for n in list(sys.modules) if n.split(".")[0] in _HEAVY}
    try:
        sys.modules.pop("train_rfdetr_seg", None)
        return importlib.import_module("train_rfdetr_seg")
    finally:
        sys.meta_path.remove(blocked)
        sys.modules.update(saved)


ec = _import_export_checkpoint()
ts = _import_train_rfdetr_seg()

SEG_SIZE_ORDER = ['nano', 'small', 'medium', 'large', 'xlarge', '2xlarge']


# ---------------------------------------------------------------------------
# The duplicated Seg tables are identical (Req 1.6)
# ---------------------------------------------------------------------------

def test_seg_size_table_is_identical_across_the_three_modules():
    assert dt.RFDETR_SEG_SIZES == dc.RFDETR_SEG_SIZES == ec.RFDETR_SEG_NATIVE_INPUTS
    assert list(dt.RFDETR_SEG_SIZES) == list(dc.RFDETR_SEG_SIZES) == \
        list(ec.RFDETR_SEG_NATIVE_INPUTS) == SEG_SIZE_ORDER
    assert dt.RFDETR_SEG_SIZES == {'nano': 312, 'small': 384, 'medium': 432,
                                   'large': 504, 'xlarge': 624, '2xlarge': 768}
    # The Seg trainer's own table (task 4.1): (class, native, queries, block).
    assert list(ts.SEG_SIZE_CLASSES) == SEG_SIZE_ORDER
    assert {k: v[1] for k, v in ts.SEG_SIZE_CLASSES.items()} == dt.RFDETR_SEG_SIZES


def test_seg_class_map_is_identical_across_the_three_modules():
    assert dt.RFDETR_SEG_CLASS_FOR_SIZE == dc.RFDETR_SEG_CLASS_FOR_SIZE == ec.RFDETR_SEG_SIZE_CLASSES
    assert list(ec.RFDETR_SEG_SIZE_CLASSES) == SEG_SIZE_ORDER
    assert [dt.RFDETR_SEG_CLASS_FOR_SIZE[s] for s in SEG_SIZE_ORDER] == [
        'RFDETRSegNano', 'RFDETRSegSmall', 'RFDETRSegMedium',
        'RFDETRSegLarge', 'RFDETRSegXLarge', 'RFDETRSeg2XLarge']
    assert {k: v[0] for k, v in ts.SEG_SIZE_CLASSES.items()} == dt.RFDETR_SEG_CLASS_FOR_SIZE


def test_seg_query_counts_are_identical():
    assert dt.RFDETR_SEG_QUERIES_FOR_SIZE == dc.RFDETR_SEG_QUERIES_FOR_SIZE
    assert [dt.RFDETR_SEG_QUERIES_FOR_SIZE[s] for s in SEG_SIZE_ORDER] == [100, 100, 200, 200, 300, 300]
    assert {k: v[2] for k, v in ts.SEG_SIZE_CLASSES.items()} == dt.RFDETR_SEG_QUERIES_FOR_SIZE


def test_seg_input_blocks_and_bounds_match_the_trainer():
    assert {k: v[3] for k, v in ts.SEG_SIZE_CLASSES.items()} == dt.RFDETR_SEG_INPUT_BLOCK_FOR_SIZE
    assert (ts.RESOLUTION_MIN, ts.RESOLUTION_MAX) == \
        (dt.RFDETR_SEG_RESOLUTION_MIN, dt.RFDETR_SEG_RESOLUTION_MAX) == (240, 1152)
    assert ts.MASK_DOWNSAMPLE == dt.RFDETR_SEG_MASK_DOWNSAMPLE == 4
    assert ts.DEFAULT_SIZE == dt.RFDETR_SEG_DEFAULTS['rfdetr_seg_size'] == 'small'
    assert ts.CHECKPOINT_MEMBER == dt.CHECKPOINT_MEMBER_FOR_ARCH['rf_detr_seg']


def test_seg_tables_share_one_key_set_and_the_natives_are_pairwise_distinct():
    sizes = set(SEG_SIZE_ORDER)
    for table in (dt.RFDETR_SEG_SIZES, dt.RFDETR_SEG_CLASS_FOR_SIZE, dt.RFDETR_SEG_QUERIES_FOR_SIZE,
                  dt.RFDETR_SEG_INPUT_BLOCK_FOR_SIZE, dc.RFDETR_SEG_SIZES, dc.RFDETR_SEG_CLASS_FOR_SIZE,
                  dc.RFDETR_SEG_QUERIES_FOR_SIZE, ec.RFDETR_SEG_SIZE_CLASSES, ec.RFDETR_SEG_NATIVE_INPUTS):
        assert set(table) == sizes
    natives = list(dt.RFDETR_SEG_SIZES.values())
    assert len(set(natives)) == len(natives) == 6
    # Every native is a multiple of its size's input block and inside the Seg bounds.
    for size, native in dt.RFDETR_SEG_SIZES.items():
        assert native % dt.RFDETR_SEG_INPUT_BLOCK_FOR_SIZE[size] == 0, size
        assert dt.RFDETR_SEG_RESOLUTION_MIN <= native <= dt.RFDETR_SEG_RESOLUTION_MAX, size
    assert [dt.RFDETR_SEG_INPUT_BLOCK_FOR_SIZE[s] for s in SEG_SIZE_ORDER] == [12, 24, 24, 24, 24, 24]


def test_derived_native_input_tables_follow_the_size_table():
    assert dc.RFDETR_SEG_NATIVE_INPUTS == tuple(sorted(dt.RFDETR_SEG_SIZES.values())) == \
        (312, 384, 432, 504, 624, 768)
    assert dc.RFDETR_SEG_SIZE_FOR_NATIVE == {native: size for size, native in dt.RFDETR_SEG_SIZES.items()}
    for size, native in ec.RFDETR_SEG_NATIVE_INPUTS.items():
        assert dc.RFDETR_SEG_SIZE_FOR_NATIVE[native] == size


# ---------------------------------------------------------------------------
# Bounds, thresholds, model types and stage types agree
# ---------------------------------------------------------------------------

def test_seg_bounds_and_default_thresholds_agree():
    assert dc.INPUT_BOUNDS['rf_detr_seg'] == \
        (dt.RFDETR_SEG_RESOLUTION_MIN, dt.RFDETR_SEG_RESOLUTION_MAX) == (240, 1152)
    assert dc.DEFAULT_SCORE_THRESHOLD['rf_detr_seg'] == dt.RFDETR_SEG_DEFAULTS['score_threshold'] == 0.5
    assert dc.DEFAULT_MASK_THRESHOLD == dt.RFDETR_SEG_DEFAULTS['mask_threshold'] == 0.5
    assert ts.DEVICE_SCORE_THRESHOLD == dt.RFDETR_SEG_DEFAULTS['score_threshold']
    assert ts.DEVICE_MASK_THRESHOLD == dt.RFDETR_SEG_DEFAULTS['mask_threshold']
    # The detection bounds are untouched (export_checkpoint.INPUT_BOUNDS gains
    # its rf_detr_seg entry in 7.3, where the parity assertion joins).
    assert dc.INPUT_BOUNDS['rf_detr'] == ec.INPUT_BOUNDS['rf_detr'] == \
        (dt.RFDETR_RESOLUTION_MIN, dt.RFDETR_RESOLUTION_MAX) == (224, 1120)
    assert dc.INPUT_BOUNDS['yolo'] == ec.INPUT_BOUNDS['yolo'] == (320, 2048)


def test_model_type_vocabulary_agrees():
    assert dc.MODEL_TYPE_INSTANCE_SEGMENTATION == dt.MODEL_TYPE_INSTANCE_SEGMENTATION == \
        'instance_segmentation'
    assert dc.MODEL_TYPE_OBJECT_DETECTION == dt.MODEL_TYPE_OBJECT_DETECTION == 'object_detection'
    assert dc.DETECTION_MODEL_TYPES == dt.DETECTION_MODEL_TYPES
    assert set(dc.MODEL_TYPE_FOR_ARCH) == set(dt.DETECTION_ARCHES)
    assert dc.MODEL_TYPE_FOR_ARCH == {'yolo': 'object_detection', 'rf_detr': 'object_detection',
                                      'rf_detr_seg': 'instance_segmentation'}


def test_arch_vocabulary_mirrors_agree():
    # checkpoint_probe.ARCHES (task 7.1, C22): the probe's vocabulary, with its
    # trailing None for kinds that carry no arch, names exactly the detection archs.
    assert cp.ARCHES == ('yolo', 'rf_detr', 'rf_detr_seg', None)
    assert set(cp.ARCHES) - {None} == set(dt.DETECTION_ARCHES)
    assert dt.DETECTION_ARCHES == ('yolo', 'rf_detr', 'rf_detr_seg')
    # model_import.FINE_TUNABLE_ARCHES (task 8.3, C25): the import boundary's
    # mirror of the probe's archs, so the sidecar descriptor of every
    # fine-tunable checkpoint kind -- a Seg .pth included -- is admitted.
    assert mi.FINE_TUNABLE_ARCHES == ('yolo', 'rf_detr', 'rf_detr_seg')
    assert mi.FINE_TUNABLE_ARCHES == dt.DETECTION_ARCHES == tuple(a for a in cp.ARCHES if a is not None)
    assert mi.FINE_TUNABLE_KINDS == cp.FINE_TUNABLE_KINDS == ('ultralytics_checkpoint', 'rfdetr_checkpoint')


def test_output_mode_and_detr_family_mirrors_agree():
    # detector_conversion's stdlib-only mirrors of the Output_Mode vocabulary,
    # the mask grid and the DETR family (task 7.2).
    assert dc.OUTPUT_MODES == dt.OUTPUT_MODES == ('instance', 'semantic')
    assert dc.DEFAULT_OUTPUT_MODE == dt.DEFAULT_OUTPUT_MODE == 'instance'
    assert dc.RFDETR_SEG_MASK_DOWNSAMPLE == dt.RFDETR_SEG_MASK_DOWNSAMPLE == ts.MASK_DOWNSAMPLE == 4
    assert dc.DETR_FAMILY_ARCHES == dt.DETR_FAMILY_ARCHES == ('rf_detr', 'rf_detr_seg')
    for arch in cp.ARCHES:
        assert dc.is_detr_family(arch) is dt.is_detr_family(arch)
    assert dc.RFDETR_REJECTED_SEG_CLASSES == ('RFDETRSegPreview',)
    assert not set(dc.RFDETR_REJECTED_SEG_CLASSES) & set(dc.RFDETR_SEG_CLASS_FOR_SIZE.values())


def test_conversion_job_mirrors_agree():
    # export_checkpoint.py (task 7.3): the job's arch vocabulary, Seg bounds,
    # name gates and mask grid agree with the shared layer and the Seg trainer.
    assert ec.ARCHES == dt.DETECTION_ARCHES == ('yolo', 'rf_detr', 'rf_detr_seg')
    assert ec.TASKS == ('detection', 'segmentation')
    assert ec.INPUT_BOUNDS['rf_detr_seg'] == dc.INPUT_BOUNDS['rf_detr_seg'] == (240, 1152)
    assert set(ec.INPUT_BOUNDS) == set(dc.INPUT_BOUNDS) == set(dt.DETECTION_ARCHES)
    assert ec.RFDETR_REJECTED_SEG_NAMES == dc.RFDETR_REJECTED_SEG_CLASSES == ('RFDETRSegPreview',)
    assert ec.RFDETR_NON_DETECTION_PREFIXES == ('RFDETRKeypoint',)
    assert ec.RFDETR_SEG_MASK_DOWNSAMPLE == ts.MASK_DOWNSAMPLE == dc.RFDETR_SEG_MASK_DOWNSAMPLE == 4
    assert ec.RFDETR_SEG_HEAD_PREFIXES == ('segmentation_head.',)


def test_stage_type_constants_agree():
    assert dt.STAGE_TYPE_FOR_ARCH['rf_detr_seg'] == dt.RFDETR_SEG_INSTANCE_STAGE_TYPE == \
        'rf_detr_instance_segmentation'
    assert dt.RFDETR_SEG_SEMANTIC_STAGE_TYPE == 'rf_detr_semantic_segmentation'
    assert dt.stage_type_for('rf_detr_seg', dt.DEFAULT_OUTPUT_MODE) == dt.STAGE_TYPE_FOR_ARCH['rf_detr_seg']
    assert dt.stage_type_for('rf_detr_seg', 'semantic') == dt.RFDETR_SEG_SEMANTIC_STAGE_TYPE
    # The trainer's hints name the default mode's stage type (task 4.1).
    assert ts.STAGE_TYPE == dt.RFDETR_SEG_INSTANCE_STAGE_TYPE
    assert ts.OUTPUT_MODE == dt.DEFAULT_OUTPUT_MODE == 'instance'


# ---------------------------------------------------------------------------
# Licence lists and the detection tables (Req 1.4, D4)
# ---------------------------------------------------------------------------

def test_pml_lists_name_only_the_two_detection_sizes():
    assert dc.RFDETR_PML_CLASSES == ec.RFDETR_PML_MODEL_NAMES == ('RFDETRXLarge', 'RFDETR2XLarge')
    assert not set(dc.RFDETR_PML_CLASSES) & set(dt.RFDETR_SEG_CLASS_FOR_SIZE.values())
    assert not set(ec.RFDETR_PML_MODEL_NAMES) & set(ec.RFDETR_SEG_SIZE_CLASSES.values())


def test_detection_tables_unchanged_and_the_class_sets_disjoint():
    assert dt.RFDETR_SIZES == dc.RFDETR_SIZES == {'nano': 384, 'small': 512, 'medium': 576, 'large': 704}
    assert dc.RFDETR_CLASS_FOR_SIZE == ec.RFDETR_SIZE_CLASSES
    assert not set(dc.RFDETR_CLASS_FOR_SIZE.values()) & set(dc.RFDETR_SEG_CLASS_FOR_SIZE.values())
    # 384 is detection nano AND Seg small: a reader must settle the task before
    # the size table (Seg spec task 7.2).
    assert set(dt.RFDETR_SIZES.values()) & set(dt.RFDETR_SEG_SIZES.values()) == {384}
