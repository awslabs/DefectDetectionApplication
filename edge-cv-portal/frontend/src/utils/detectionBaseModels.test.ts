import { describe, expect, it } from 'vitest';
import type { TrainingJobRecord } from '../services/api';
import {
  BASE_MODEL_GROUP_LABELS,
  PUBLISHED_CHECKPOINTS,
  RFDETR_PARAM_DEFAULTS,
  RFDETR_RESOLUTION_ERROR,
  RFDETR_SIZES,
  YOLO_PARAM_DEFAULTS,
  applyRfDetrSize,
  baseModelClassNames,
  baseModelOptionValue,
  baseModelSelectOptions,
  buildDetectionHyperparameters,
  buildDetectionSubmitFields,
  classListsDiffer,
  classNamesFromManifestEntry,
  defaultDetectionInstanceType,
  findBaseModelRecord,
  groupDetectionBaseModels,
  paramsFromBaseRecord,
  parseBaseModelOptionValue,
  parseClassNamesInput,
  publishedBaseModelRef,
  validateDetectionParams,
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
const all = [yoloJob, legacyYoloJob, rfJob, inProgressRf, lfvJob, importedYolo, importedOnnxOnly, importedRf];

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
