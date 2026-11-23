import { describe, expect, it } from 'vitest';
import type { TrainingJobRecord } from '../services/api';
import {
  BASE_MODEL_GROUP_LABELS,
  PUBLISHED_CHECKPOINTS,
  RFDETR_PARAM_DEFAULTS,
  RFDETR_RESOLUTION_ERROR,
  RFDETR_SEG_INPUT_BLOCK,
  RFDETR_SEG_PARAM_DEFAULTS,
  RFDETR_SEG_QUERIES,
  RFDETR_SEG_RESOLUTION_BOUNDS,
  RFDETR_SEG_SIZES,
  RFDETR_SEG_SIZE_ORDER,
  RFDETR_SIZES,
  YOLO_PARAM_DEFAULTS,
  applyRfDetrSegSize,
  applyRfDetrSize,
  baseModelClassNames,
  baseModelOptionValue,
  baseModelSelectOptions,
  buildDetectionHyperparameters,
  buildDetectionSubmitFields,
  classListsDiffer,
  classNamesFromManifestEntry,
  defaultDetectionInstanceType,
  defaultInstanceTypeForSegSize,
  detectionParamDefaults,
  findBaseModelRecord,
  groupDetectionBaseModels,
  isRfDetrSegSize,
  isTrainedDetectionRecord,
  paramsFromBaseRecord,
  parseBaseModelOptionValue,
  parseClassNamesInput,
  publishedBaseModelRef,
  recordDetectionArch,
  rfdetrSegNativeResolution,
  rfdetrSegResolutionError,
  validateDetectionParams,
  validateRfDetrSegResolution,
} from './detectionBaseModels';

const job = (over: Partial<TrainingJobRecord>): TrainingJobRecord => ({
  training_id: 'tj-x',
  usecase_id: 'uc-1',
  model_name: 'det',
  model_version: '1.0.0',
  model_type: 'object_detection',
  runtime: 'onnx',
  status: 'Completed',
  created_at: 1,
  ...over,
});

const yoloJob = job({
  training_id: 'tj-yolo',
  model_name: 'blue-plate',
  model_version: '2.0.0',
  detection: {
    detection_arch: 'yolo', network_input_width: 1280, network_input_height: 1280,
    class_names: ['plate'], num_classes: 1, score_threshold: 0.25, preserve_aspect: true, imgsz: 1280,
  },
  metrics: { 'test:mAP50': 0.931 },
});
// Written before RF-DETR existed: no detection_arch => YOLO.
const legacyYoloJob = job({
  training_id: 'tj-legacy',
  detection: {
    network_input_width: 640, network_input_height: 640, class_names: ['a', 'b'],
    num_classes: 2, score_threshold: 0.25, preserve_aspect: true,
  },
});
const rfJob = job({
  training_id: 'tj-rf',
  model_name: 'rf-plate',
  detection: {
    detection_arch: 'rf_detr', network_input_width: 576, network_input_height: 576, resolution: 576,
    rfdetr_size: 'medium', class_names: ['plate', 'luggage'], num_classes: 2, score_threshold: 0.5,
    preserve_aspect: false, top_k: 300,
  },
});
const inProgressRf = job({ training_id: 'tj-rf-running', status: 'InProgress', detection: rfJob.detection });
const lfvJob = job({ training_id: 'tj-lfv', model_type: 'classification', runtime: undefined });
const importedYolo = job({
  training_id: 'imp-yolo', source: 'imported', model_name: 'brought-in',
  metadata: { fine_tunable: { arch: 'yolo', kind: 'ultralytics', checkpoint_s3: 's3://b/ckpt.pt', class_names: ['x'] } },
});
const importedOnnxOnly = job({ training_id: 'imp-onnx', source: 'imported', metadata: { fine_tunable: null } });
const importedRf = job({
  training_id: 'imp-rf', source: 'imported',
  metadata: { fine_tunable: { arch: 'rf_detr', checkpoint_s3: 's3://b/ckpt.pth' } },
});
// rfdetr-seg-support: a Completed RF-DETR-Seg training and a Seg import. With
// these in the fixture, the 'yolo' / 'rf_detr' exact-list cases below also
// prove a Seg record never lands in either detection group.
const segJob = job({
  training_id: 'tj-seg',
  model_name: 'crack-masks',
  model_type: 'instance_segmentation',
  detection: {
    detection_arch: 'rf_detr_seg', network_input_width: 432, network_input_height: 432, resolution: 432,
    rfdetr_seg_size: 'medium', class_names: ['crack'], num_classes: 1, score_threshold: 0.5, mask_threshold: 0.5,
    preserve_aspect: false, top_k: 200, output_mode: 'instance',
  },
  metrics: { 'test:mAP50': 0.9, 'test:mask-mAP50': 0.85 },
});
const inProgressSeg = job({ training_id: 'tj-seg-running', model_type: 'instance_segmentation', status: 'InProgress', detection: segJob.detection });
const importedSeg = job({
  training_id: 'imp-seg', source: 'imported', model_type: 'instance_segmentation',
  metadata: { fine_tunable: { arch: 'rf_detr_seg', checkpoint_s3: 's3://b/seg.pth', num_classes: 1 } },
});
const all = [yoloJob, legacyYoloJob, rfJob, inProgressRf, lfvJob, importedYolo, importedOnnxOnly, importedRf];
// The Seg records join the shared fixture so the exact-list cases below prove exclusion.
all.push(segJob, inProgressSeg, importedSeg);

describe('groupDetectionBaseModels', () => {
  it('YOLO: published yolo11 weights, Completed YOLO detectors (legacy = yolo), fine-tunable YOLO imports', () => {
    const g = groupDetectionBaseModels(all, 'yolo');
    expect(g.published.map(p => p.ref)).toEqual(['yolo11n.pt', 'yolo11s.pt', 'yolo11m.pt']);
    expect(g.trainedDetectors.map(j => j.training_id)).toEqual(['tj-yolo', 'tj-legacy']);
    expect(g.fineTunableImports.map(j => j.training_id)).toEqual(['imp-yolo']);
  });

  it('RF-DETR: the four sizes, Completed RF-DETR detectors only, RF-DETR imports only', () => {
    const g = groupDetectionBaseModels(all, 'rf_detr');
    expect(g.published.map(p => p.ref)).toEqual(['nano', 'small', 'medium', 'large']);
    expect(g.published.map(p => p.native_resolution)).toEqual([384, 512, 576, 704]);
    expect(g.trainedDetectors.map(j => j.training_id)).toEqual(['tj-rf']);
    expect(g.fineTunableImports.map(j => j.training_id)).toEqual(['imp-rf']);
  });

  it('tolerates an empty / missing list', () => {
    expect(groupDetectionBaseModels([], 'yolo').trainedDetectors).toEqual([]);
    expect(groupDetectionBaseModels(undefined, 'rf_detr').published).toBe(PUBLISHED_CHECKPOINTS.rf_detr);
  });

  // rfdetr-seg-support Req 5.4 / 12.4 (C19): the Seg group is fed by the three
  // widened inputs — PUBLISHED_CHECKPOINTS.rf_detr_seg, isTrainedDetectionRecord
  // through isDetectionModelType, recordDetectionArch returning 'rf_detr_seg'.
  it('RF-DETR-Seg: the six sizes with their natives, Completed Seg jobs only, Seg imports only', () => {
    const g = groupDetectionBaseModels(all, 'rf_detr_seg');
    expect(g.published.map(p => p.ref)).toEqual(['nano', 'small', 'medium', 'large', 'xlarge', '2xlarge']);
    expect(g.published.map(p => p.native_resolution)).toEqual([312, 384, 432, 504, 624, 768]);
    expect(g.published[1].label).toBe('RF-DETR-Seg small (COCO, native 384px) — recommended');
    expect(g.published[5].label).toBe('RF-DETR-Seg 2xlarge (COCO, native 768px)');
    expect(g.trainedDetectors.map(j => j.training_id)).toEqual(['tj-seg']);
    expect(g.fineTunableImports.map(j => j.training_id)).toEqual(['imp-seg']);
    expect(groupDetectionBaseModels(undefined, 'rf_detr_seg').published).toBe(PUBLISHED_CHECKPOINTS.rf_detr_seg);
  });

  it('isTrainedDetectionRecord admits a Completed instance_segmentation ONNX training and recordDetectionArch names its family', () => {
    expect(isTrainedDetectionRecord(segJob)).toBe(true);
    expect(isTrainedDetectionRecord(importedSeg)).toBe(false);
    expect(isTrainedDetectionRecord(job({ model_type: 'instance_segmentation', runtime: undefined }))).toBe(false);
    expect(isTrainedDetectionRecord(job({ model_type: 'segmentation' }))).toBe(false);
    expect(recordDetectionArch(segJob)).toBe('rf_detr_seg');
    expect(recordDetectionArch(rfJob)).toBe('rf_detr');
    expect(recordDetectionArch(legacyYoloJob)).toBe('yolo');
    expect(recordDetectionArch(undefined)).toBe('yolo');
  });
});

describe('baseModelSelectOptions / option values', () => {
  it('groups as Published / My trained detectors / Imported and omits empty groups', () => {
    const g = groupDetectionBaseModels(all, 'yolo');
    const opts = baseModelSelectOptions(g);
    expect(opts.map(o => o.label)).toEqual([
      BASE_MODEL_GROUP_LABELS.published,
      BASE_MODEL_GROUP_LABELS.training_job,
      BASE_MODEL_GROUP_LABELS.imported,
    ]);
    expect(opts[1].options[0]).toMatchObject({
      label: 'blue-plate v2.0.0',
      value: 'training_job:tj-yolo',
      tags: ['mAP@50 93.1%'],
    });
    expect(opts[2].options[0].value).toBe('imported:imp-yolo');

    const onlyPublished = baseModelSelectOptions(groupDetectionBaseModels([], 'rf_detr'));
    expect(onlyPublished.map(o => o.label)).toEqual([BASE_MODEL_GROUP_LABELS.published]);
    expect(onlyPublished[0].options.map(o => o.value)).toEqual([
      'published:nano', 'published:small', 'published:medium', 'published:large',
    ]);
  });

  it('offers the six published RF-DETR-Seg sizes when no Seg record exists (rfdetr-seg-support)', () => {
    const onlyPublished = baseModelSelectOptions(groupDetectionBaseModels([], 'rf_detr_seg'));
    expect(onlyPublished.map(o => o.label)).toEqual([BASE_MODEL_GROUP_LABELS.published]);
    expect(onlyPublished[0].options.map(o => o.value)).toEqual([
      'published:nano', 'published:small', 'published:medium', 'published:large', 'published:xlarge', 'published:2xlarge',
    ]);
    const withRecords = baseModelSelectOptions(groupDetectionBaseModels(all, 'rf_detr_seg'));
    expect(withRecords.map(o => o.label)).toEqual([
      BASE_MODEL_GROUP_LABELS.published,
      BASE_MODEL_GROUP_LABELS.training_job,
      BASE_MODEL_GROUP_LABELS.imported,
    ]);
    expect(withRecords[1].options.map(o => o.value)).toEqual(['training_job:tj-seg']);
    expect(withRecords[2].options.map(o => o.value)).toEqual(['imported:imp-seg']);
  });

  it('round-trips {kind, ref} through the option value and rejects junk', () => {
    const ref = { kind: 'training_job' as const, ref: 'tj:with:colons' };
    expect(parseBaseModelOptionValue(baseModelOptionValue(ref))).toEqual(ref);
    expect(parseBaseModelOptionValue('published:yolo11s.pt')).toEqual({ kind: 'published', ref: 'yolo11s.pt' });
    expect(parseBaseModelOptionValue('bogus:x')).toBeNull();
    expect(parseBaseModelOptionValue('published:')).toBeNull();
    expect(parseBaseModelOptionValue(undefined)).toBeNull();
  });

  it('finds the base record and its class names; published has none', () => {
    const g = groupDetectionBaseModels(all, 'yolo');
    expect(findBaseModelRecord(g, { kind: 'training_job', ref: 'tj-yolo' })).toBe(yoloJob);
    expect(findBaseModelRecord(g, { kind: 'imported', ref: 'imp-yolo' })).toBe(importedYolo);
    expect(findBaseModelRecord(g, { kind: 'training_job', ref: 'tj-rf' })).toBeUndefined();
    expect(findBaseModelRecord(g, { kind: 'published', ref: 'yolo11s.pt' })).toBeUndefined();
    expect(baseModelClassNames(yoloJob)).toEqual(['plate']);
    expect(baseModelClassNames(importedYolo)).toEqual(['x']);
    expect(baseModelClassNames(undefined)).toBeNull();
  });
});

describe('class names', () => {
  it('reads the class-map in id order from a DDA or Ground Truth entry', () => {
    expect(classNamesFromManifestEntry({
      'source-ref': 's3://x', 'bounding-box': {}, 'bounding-box-metadata': { 'class-map': { '1': 'b', '0': 'a', '10': 'k' } },
    })).toEqual(['a', 'b', 'k']);
    expect(classNamesFromManifestEntry({ 'anomaly-label': 1, 'anomaly-label-metadata': { type: 'x' } })).toEqual([]);
    expect(classNamesFromManifestEntry(null)).toEqual([]);
  });

  it('parses a comma list and compares order-sensitively', () => {
    expect(parseClassNamesInput(' plate, luggage ,,\nbag ')).toEqual(['plate', 'luggage', 'bag']);
    expect(classListsDiffer(['a', 'b'], ['a', 'b'])).toBe(false);
    expect(classListsDiffer(['a', 'b'], ['b', 'a'])).toBe(true);
    expect(classListsDiffer(['a'], ['a', 'b'])).toBe(true);
  });
});

describe('per-arch defaults, validation and payload', () => {
  it('RF-DETR defaults are the documented T4 configuration; resolution follows the size', () => {
    expect(RFDETR_PARAM_DEFAULTS).toMatchObject({
      rfdetrSize: 'small', resolution: '512', epochs: '100', batch: '4', gradAccum: '4', lr: '0.0001', patience: '10', scoreThreshold: '0.5',
    });
    expect(applyRfDetrSize(RFDETR_PARAM_DEFAULTS, 'large').resolution).toBe(String(RFDETR_SIZES.large));
    // A user-set resolution is kept when the size changes.
    expect(applyRfDetrSize({ ...RFDETR_PARAM_DEFAULTS, resolution: '640' }, 'large').resolution).toBe('640');
  });

  it('validates the RF-DETR resolution rule with the backend wording and has no IoU rule', () => {
    expect(validateDetectionParams('rf_detr', RFDETR_PARAM_DEFAULTS)).toEqual([]);
    expect(validateDetectionParams('rf_detr', { ...RFDETR_PARAM_DEFAULTS, resolution: '500' })).toEqual([RFDETR_RESOLUTION_ERROR]);
    expect(RFDETR_RESOLUTION_ERROR).toBe('Resolution must be a multiple of 32 between 224 and 1120');
    expect(validateDetectionParams('rf_detr', { ...RFDETR_PARAM_DEFAULTS, resolution: '1152' })).toEqual([RFDETR_RESOLUTION_ERROR]);
    expect(validateDetectionParams('rf_detr', { ...RFDETR_PARAM_DEFAULTS, iouThreshold: '5' })).toEqual([]);
    expect(validateDetectionParams('rf_detr', { ...RFDETR_PARAM_DEFAULTS, gradAccum: '0', lr: '1' })).toEqual([
      'Gradient accumulation must be between 1 and 64',
      'Learning rate must be strictly between 0 and 1',
    ]);
  });

  it('YOLO validation is unchanged', () => {
    expect(validateDetectionParams('yolo', YOLO_PARAM_DEFAULTS)).toEqual([]);
    expect(validateDetectionParams('yolo', { ...YOLO_PARAM_DEFAULTS, imgsz: '1000', iouThreshold: '1' })).toEqual([
      'Image size must be a multiple of 32 between 320 and 2048',
      'IoU threshold must be strictly between 0 and 1',
    ]);
  });

  it('builds the per-arch hyperparameters: YOLO as before, RF-DETR without imgsz / iou_threshold', () => {
    expect(buildDetectionHyperparameters('yolo', YOLO_PARAM_DEFAULTS)).toEqual({
      imgsz: 1280, epochs: 100, batch: 4, base_weights: 'yolo11s.pt', patience: 30, score_threshold: 0.25, iou_threshold: 0.45,
    });
    const rf = buildDetectionHyperparameters('rf_detr', RFDETR_PARAM_DEFAULTS);
    expect(rf).toEqual({
      rfdetr_size: 'small', resolution: 512, epochs: 100, batch: 4, grad_accum: 4, lr: 0.0001, patience: 10, score_threshold: 0.5,
    });
    expect(rf).not.toHaveProperty('iou_threshold');
    expect(rf).not.toHaveProperty('imgsz');
  });

  it('submit fields carry detection_arch + base_model; published ref is the weights / size', () => {
    expect(publishedBaseModelRef('yolo', YOLO_PARAM_DEFAULTS)).toEqual({ kind: 'published', ref: 'yolo11s.pt' });
    expect(publishedBaseModelRef('rf_detr', { ...RFDETR_PARAM_DEFAULTS, rfdetrSize: 'medium' })).toEqual({ kind: 'published', ref: 'medium' });

    const published = buildDetectionSubmitFields('rf_detr', RFDETR_PARAM_DEFAULTS, null, []);
    expect(published).toEqual({
      detection_arch: 'rf_detr',
      base_model: { kind: 'published', ref: 'small' },
      hyperparameters: buildDetectionHyperparameters('rf_detr', RFDETR_PARAM_DEFAULTS),
    });
    expect(published).not.toHaveProperty('class_names');

    const fromJob = buildDetectionSubmitFields('yolo', YOLO_PARAM_DEFAULTS, { kind: 'training_job', ref: 'tj-yolo' }, ['plate', 'luggage']);
    expect(fromJob.detection_arch).toBe('yolo');
    expect(fromJob.base_model).toEqual({ kind: 'training_job', ref: 'tj-yolo' });
    expect(fromJob.class_names).toEqual(['plate', 'luggage']);
  });

  it('a base record defaults the network input (and RF-DETR size) to what it was trained at', () => {
    expect(paramsFromBaseRecord('yolo', YOLO_PARAM_DEFAULTS, legacyYoloJob).imgsz).toBe('640');
    const rf = paramsFromBaseRecord('rf_detr', RFDETR_PARAM_DEFAULTS, rfJob);
    expect(rf.rfdetrSize).toBe('medium');
    expect(rf.resolution).toBe('576');
    expect(paramsFromBaseRecord('rf_detr', RFDETR_PARAM_DEFAULTS, undefined)).toBe(RFDETR_PARAM_DEFAULTS);
  });

  it('instance default: g4dn.xlarge everywhere except RF-DETR medium/large -> g5.xlarge', () => {
    expect(defaultDetectionInstanceType('yolo')).toBe('ml.g4dn.xlarge');
    expect(defaultDetectionInstanceType('rf_detr', 'nano')).toBe('ml.g4dn.xlarge');
    expect(defaultDetectionInstanceType('rf_detr', 'small')).toBe('ml.g4dn.xlarge');
    expect(defaultDetectionInstanceType('rf_detr', 'medium')).toBe('ml.g5.xlarge');
    expect(defaultDetectionInstanceType('rf_detr', 'large')).toBe('ml.g5.xlarge');
    expect(defaultDetectionInstanceType('yolo', 'large')).toBe('ml.g4dn.xlarge');
  });
});

// ---------------------------------------------------------------------------
// RF-DETR-Seg (rfdetr-seg-support Req 1.2, 1.5, 12.1, 12.2; each case mirrors
// a backend constant of detection_training.py or a rf_detr case above).
// ---------------------------------------------------------------------------

describe('RF-DETR-Seg constant mirrors', () => {
  it('six sizes with the backend natives, in order', () => {
    expect(RFDETR_SEG_SIZE_ORDER).toEqual(['nano', 'small', 'medium', 'large', 'xlarge', '2xlarge']);
    expect(RFDETR_SEG_SIZES).toEqual({ nano: 312, small: 384, medium: 432, large: 504, xlarge: 624, '2xlarge': 768 });
    expect(RFDETR_SEG_SIZE_ORDER.map(s => RFDETR_SEG_SIZES[s])).toEqual([312, 384, 432, 504, 624, 768]);
  });

  it('Seg_Input_Block 12 for nano and 24 otherwise; bounds [240, 1152]; queries 100/200/300', () => {
    expect(RFDETR_SEG_INPUT_BLOCK).toEqual({ nano: 12, small: 24, medium: 24, large: 24, xlarge: 24, '2xlarge': 24 });
    expect(RFDETR_SEG_RESOLUTION_BOUNDS).toEqual({ min: 240, max: 1152 });
    expect(RFDETR_SEG_QUERIES).toEqual({ nano: 100, small: 100, medium: 200, large: 200, xlarge: 300, '2xlarge': 300 });
    // Every native is a multiple of its own block and inside the bounds.
    for (const size of RFDETR_SEG_SIZE_ORDER) {
      expect(RFDETR_SEG_SIZES[size] % RFDETR_SEG_INPUT_BLOCK[size]).toBe(0);
      expect(validateRfDetrSegResolution(size, RFDETR_SEG_SIZES[size])).toBeNull();
    }
  });

  it('isRfDetrSegSize / rfdetrSegNativeResolution', () => {
    expect(isRfDetrSegSize('2xlarge')).toBe(true);
    expect(isRfDetrSegSize('xlarge')).toBe(true);
    expect(isRfDetrSegSize('huge')).toBe(false);
    expect(isRfDetrSegSize(undefined)).toBe(false);
    expect(rfdetrSegNativeResolution('nano')).toBe(312);
    expect(rfdetrSegNativeResolution('bogus')).toBe(384);
  });

  it('published Seg checkpoints: the six refs the backend accepts, natives in the label', () => {
    expect(PUBLISHED_CHECKPOINTS.rf_detr_seg.map(p => p.ref)).toEqual(RFDETR_SEG_SIZE_ORDER);
    for (const p of PUBLISHED_CHECKPOINTS.rf_detr_seg) {
      expect(p.label).toContain(`RF-DETR-Seg ${p.ref} (COCO, native ${p.native_resolution}px)`);
    }
  });
});

describe('RF-DETR-Seg form defaults, validation and payload', () => {
  it('defaults mirror the backend RFDETR_SEG_DEFAULTS and the size\'s native resolution', () => {
    expect(RFDETR_SEG_PARAM_DEFAULTS).toMatchObject({
      rfdetrSegSize: 'small', resolution: '384', epochs: '100', batch: '4', gradAccum: '4', lr: '0.0001',
      patience: '10', scoreThreshold: '0.5', maskThreshold: '0.5',
    });
    expect(detectionParamDefaults('rf_detr_seg')).toEqual(RFDETR_SEG_PARAM_DEFAULTS);
    expect(detectionParamDefaults('rf_detr_seg')).not.toBe(RFDETR_SEG_PARAM_DEFAULTS);
    // The rf_detr / yolo defaults keep their own values and gain the Seg keys.
    expect(detectionParamDefaults('rf_detr')).toEqual(RFDETR_PARAM_DEFAULTS);
    expect(RFDETR_PARAM_DEFAULTS).toMatchObject({ resolution: '512', rfdetrSegSize: 'small', maskThreshold: '0.5' });
    expect(YOLO_PARAM_DEFAULTS).toMatchObject({ imgsz: '1280', rfdetrSegSize: 'small', maskThreshold: '0.5' });
  });

  it('applyRfDetrSegSize follows the native resolution unless the user moved it', () => {
    expect(applyRfDetrSegSize(RFDETR_SEG_PARAM_DEFAULTS, '2xlarge')).toMatchObject({ rfdetrSegSize: '2xlarge', resolution: '768' });
    expect(applyRfDetrSegSize(RFDETR_SEG_PARAM_DEFAULTS, 'nano').resolution).toBe('312');
    expect(applyRfDetrSegSize({ ...RFDETR_SEG_PARAM_DEFAULTS, resolution: '480' }, 'large').resolution).toBe('480');
    // The detection size helper is untouched by the Seg fields.
    expect(applyRfDetrSize(RFDETR_SEG_PARAM_DEFAULTS, 'large').rfdetrSegSize).toBe('small');
  });

  it('validates the Seg resolution per block and bounds with the backend wording', () => {
    // Every native is accepted for its size.
    for (const size of RFDETR_SEG_SIZE_ORDER) {
      expect(validateDetectionParams('rf_detr_seg', { ...RFDETR_SEG_PARAM_DEFAULTS, rfdetrSegSize: size, resolution: String(RFDETR_SEG_SIZES[size]) })).toEqual([]);
    }
    // nano (block 12): 324 = 27 * 12 accepted; 320 is a multiple of 32 but not of 12 -> rejected.
    expect(validateRfDetrSegResolution('nano', 324)).toBeNull();
    expect(validateRfDetrSegResolution('nano', '320')).toBe('Resolution must be a multiple of 12 between 240 and 1152 for RF-DETR-Seg nano');
    // small (block 24): 480 accepted; 500 rejected; 324 (a multiple of nano's 12, not of 24) -> rejected.
    expect(validateRfDetrSegResolution('small', 480)).toBeNull();
    expect(validateRfDetrSegResolution('small', 500)).toBe('Resolution must be a multiple of 24 between 240 and 1152 for RF-DETR-Seg small');
    expect(validateRfDetrSegResolution('small', 324)).toMatch(/multiple of 24/);
    expect(validateRfDetrSegResolution('small', 312)).toBeNull();
    // Bounds: 216 (below 240) and 1176 (above 1152) are multiples of 24 and still rejected; the bounds themselves pass.
    expect(validateRfDetrSegResolution('medium', 216)).toMatch(/between 240 and 1152/);
    expect(validateRfDetrSegResolution('medium', 1176)).toMatch(/between 240 and 1152/);
    expect(validateRfDetrSegResolution('medium', 240)).toBeNull();
    expect(validateRfDetrSegResolution('medium', 1152)).toBeNull();
    expect(validateRfDetrSegResolution('large', '')).not.toBeNull();
    expect(validateRfDetrSegResolution('large', 'abc')).not.toBeNull();
    expect(validateRfDetrSegResolution('large', 504.5)).not.toBeNull();
    expect(rfdetrSegResolutionError('nano')).toBe('Resolution must be a multiple of 12 between 240 and 1152 for RF-DETR-Seg nano');
  });

  it('Seg validation: size table, grad-accum / lr as rf_detr, mask threshold in (0, 1), no imgsz, no IoU', () => {
    expect(validateDetectionParams('rf_detr_seg', RFDETR_SEG_PARAM_DEFAULTS)).toEqual([]);
    expect(validateDetectionParams('rf_detr_seg', { ...RFDETR_SEG_PARAM_DEFAULTS, resolution: '500' })).toEqual([
      'Resolution must be a multiple of 24 between 240 and 1152 for RF-DETR-Seg small',
    ]);
    expect(validateDetectionParams('rf_detr_seg', { ...RFDETR_SEG_PARAM_DEFAULTS, rfdetrSegSize: 'huge' as never })).toEqual([
      'Size must be one of nano, small, medium, large, xlarge, 2xlarge',
    ]);
    // The YOLO / rf_detr-only fields are ignored: an invalid imgsz / IoU / rf_detr size raises nothing.
    expect(validateDetectionParams('rf_detr_seg', { ...RFDETR_SEG_PARAM_DEFAULTS, imgsz: '1000', iouThreshold: '5', rfdetrSize: 'bogus' as never })).toEqual([]);
    expect(validateDetectionParams('rf_detr_seg', { ...RFDETR_SEG_PARAM_DEFAULTS, gradAccum: '0', lr: '1' })).toEqual([
      'Gradient accumulation must be between 1 and 64',
      'Learning rate must be strictly between 0 and 1',
    ]);
    expect(validateDetectionParams('rf_detr_seg', { ...RFDETR_SEG_PARAM_DEFAULTS, maskThreshold: '1' })).toEqual([
      'Mask threshold must be strictly between 0 and 1',
    ]);
    expect(validateDetectionParams('rf_detr_seg', { ...RFDETR_SEG_PARAM_DEFAULTS, maskThreshold: '0' })).toEqual([
      'Mask threshold must be strictly between 0 and 1',
    ]);
    // The mask threshold is a Seg-only rule: rf_detr / yolo ignore it.
    expect(validateDetectionParams('rf_detr', { ...RFDETR_PARAM_DEFAULTS, maskThreshold: '7' })).toEqual([]);
    expect(validateDetectionParams('yolo', { ...YOLO_PARAM_DEFAULTS, maskThreshold: '7' })).toEqual([]);
  });

  it('builds the exact Seg hyperparameters body: no imgsz, iou_threshold or base_weights', () => {
    const seg = buildDetectionHyperparameters('rf_detr_seg', RFDETR_SEG_PARAM_DEFAULTS);
    expect(seg).toEqual({
      rfdetr_seg_size: 'small', resolution: 384, epochs: 100, batch: 4, grad_accum: 4, lr: 0.0001, patience: 10,
      score_threshold: 0.5, mask_threshold: 0.5,
    });
    expect(seg).not.toHaveProperty('imgsz');
    expect(seg).not.toHaveProperty('iou_threshold');
    expect(seg).not.toHaveProperty('base_weights');
    expect(seg).not.toHaveProperty('rfdetr_size');
    // The Seg keys never enter the rf_detr / yolo bodies.
    expect(buildDetectionHyperparameters('rf_detr', RFDETR_SEG_PARAM_DEFAULTS)).not.toHaveProperty('mask_threshold');
    expect(buildDetectionHyperparameters('yolo', RFDETR_SEG_PARAM_DEFAULTS)).not.toHaveProperty('rfdetr_seg_size');
  });

  it('published ref is the Seg size; submit fields carry detection_arch rf_detr_seg', () => {
    expect(publishedBaseModelRef('rf_detr_seg', { ...RFDETR_SEG_PARAM_DEFAULTS, rfdetrSegSize: 'xlarge' })).toEqual({ kind: 'published', ref: 'xlarge' });
    const published = buildDetectionSubmitFields('rf_detr_seg', RFDETR_SEG_PARAM_DEFAULTS, null, ['crack']);
    expect(published).toEqual({
      detection_arch: 'rf_detr_seg',
      base_model: { kind: 'published', ref: 'small' },
      hyperparameters: buildDetectionHyperparameters('rf_detr_seg', RFDETR_SEG_PARAM_DEFAULTS),
      class_names: ['crack'],
    });
  });

  it('a Seg base record defaults the size and the resolution it was trained at', () => {
    const fromSeg = paramsFromBaseRecord('rf_detr_seg', RFDETR_SEG_PARAM_DEFAULTS, segJob);
    expect(fromSeg.rfdetrSegSize).toBe('medium');
    expect(fromSeg.resolution).toBe('432');
    expect(paramsFromBaseRecord('rf_detr_seg', RFDETR_SEG_PARAM_DEFAULTS, undefined)).toBe(RFDETR_SEG_PARAM_DEFAULTS);
    // A resolution the size's block rejects is left alone (network_input_width fallback likewise).
    const odd = job({ detection: { ...segJob.detection!, resolution: undefined, network_input_width: 500 } });
    expect(paramsFromBaseRecord('rf_detr_seg', RFDETR_SEG_PARAM_DEFAULTS, odd).resolution).toBe('384');
    // The detection branches ignore the Seg record's Seg fields.
    expect(paramsFromBaseRecord('rf_detr', RFDETR_PARAM_DEFAULTS, segJob).rfdetrSize).toBe('small');
  });

  it('instance default per Seg size: g4dn.xlarge for nano/small/medium, g5.xlarge for large/xlarge/2xlarge (Req 5.3)', () => {
    expect(defaultInstanceTypeForSegSize('nano')).toBe('ml.g4dn.xlarge');
    expect(defaultInstanceTypeForSegSize('small')).toBe('ml.g4dn.xlarge');
    expect(defaultInstanceTypeForSegSize('medium')).toBe('ml.g4dn.xlarge');
    expect(defaultInstanceTypeForSegSize('large')).toBe('ml.g5.xlarge');
    expect(defaultInstanceTypeForSegSize('xlarge')).toBe('ml.g5.xlarge');
    expect(defaultInstanceTypeForSegSize('2xlarge')).toBe('ml.g5.xlarge');
    expect(defaultInstanceTypeForSegSize(undefined)).toBe('ml.g4dn.xlarge');
    for (const size of RFDETR_SEG_SIZE_ORDER) {
      expect(defaultDetectionInstanceType('rf_detr_seg', size)).toBe(defaultInstanceTypeForSegSize(size));
    }
    // rf_detr medium stays g5.xlarge; the Seg table does not leak into it.
    expect(defaultDetectionInstanceType('rf_detr', 'medium')).toBe('ml.g5.xlarge');
    expect(defaultDetectionInstanceType('rf_detr', 'xlarge')).toBe('ml.g4dn.xlarge');
  });
});
