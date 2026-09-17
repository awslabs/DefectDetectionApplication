import { describe, expect, it } from 'vitest';
import {
  ALL_MODEL_TYPE_OPTIONS,
  LFV_MODEL_TYPE_OPTIONS,
  MODEL_SOURCE_MARKETPLACE,
  MODEL_SOURCE_OPTIONS,
  MODEL_SOURCE_RF_DETR,
  MODEL_SOURCE_YOLO,
  RF_DETR_MODEL_TYPE_OPTIONS,
  YOLO_MODEL_TYPE_OPTIONS,
  detectionArchForSource,
  labelingJobCoversFolder,
  modelSourceForDetectionArch,
  modelSourceForLabelingTask,
  modelSourceForType,
  modelTypeOptionsForSource,
  normalizeS3Folder,
} from './trainingSources';

describe('Model Source options', () => {
  it('offers marketplace, YOLO and RF-DETR in that order — and no BYOM source (Req 6.7)', () => {
    expect(MODEL_SOURCE_OPTIONS.map(o => o.value)).toEqual([
      MODEL_SOURCE_MARKETPLACE,
      MODEL_SOURCE_YOLO,
      MODEL_SOURCE_RF_DETR,
    ]);
    expect(MODEL_SOURCE_OPTIONS.some(o => o.value === 'byom')).toBe(false);
    expect(MODEL_SOURCE_OPTIONS.some(o => /BYOM|Imported Model/i.test(o.label ?? ''))).toBe(false);
  });

  it('enables every source: all three have a training path (Req 3.1)', () => {
    expect(MODEL_SOURCE_OPTIONS.filter(o => o.disabled)).toEqual([]);
    for (const o of MODEL_SOURCE_OPTIONS) {
      expect(o.description).not.toMatch(/Coming soon/i);
    }
  });

  it('labels RF-DETR as Roboflow object detection with an ONNX tag', () => {
    const rf = MODEL_SOURCE_OPTIONS.find(o => o.value === MODEL_SOURCE_RF_DETR)!;
    expect(rf.label).toMatch(/RF-DETR/);
    expect(rf.label).toMatch(/Roboflow/);
    expect(rf.label).toMatch(/Object Detection/);
    expect(rf.tags).toEqual(['ONNX']);
  });
});

describe('modelTypeOptionsForSource', () => {
  it('filters model types by source', () => {
    expect(modelTypeOptionsForSource(MODEL_SOURCE_MARKETPLACE)).toBe(LFV_MODEL_TYPE_OPTIONS);
    expect(modelTypeOptionsForSource(MODEL_SOURCE_YOLO)).toBe(YOLO_MODEL_TYPE_OPTIONS);
    expect(modelTypeOptionsForSource(MODEL_SOURCE_RF_DETR)).toBe(RF_DETR_MODEL_TYPE_OPTIONS);
    expect(modelTypeOptionsForSource(undefined)).toBe(LFV_MODEL_TYPE_OPTIONS);
    expect(modelTypeOptionsForSource('byom')).toBe(LFV_MODEL_TYPE_OPTIONS);
  });

  it('YOLO and RF-DETR train object_detection only; marketplace never does', () => {
    expect(YOLO_MODEL_TYPE_OPTIONS.map(o => o.value)).toEqual(['object_detection']);
    expect(RF_DETR_MODEL_TYPE_OPTIONS.map(o => o.value)).toEqual(['object_detection']);
    expect(LFV_MODEL_TYPE_OPTIONS.map(o => o.value)).toEqual([
      'classification', 'classification-robust', 'segmentation', 'segmentation-robust',
    ]);
    // One entry per model_type value (the two detection sources share one).
    expect(ALL_MODEL_TYPE_OPTIONS.length).toBe(5);
    expect(new Set(ALL_MODEL_TYPE_OPTIONS.map(o => o.value)).size).toBe(5);
  });

  it('RF-DETR describes its own geometry contract (square resize, no NMS), not letterboxing', () => {
    expect(RF_DETR_MODEL_TYPE_OPTIONS[0].description).toMatch(/square resize/i);
    expect(RF_DETR_MODEL_TYPE_OPTIONS[0].description).not.toMatch(/letterbox/i);
    expect(YOLO_MODEL_TYPE_OPTIONS[0].description).toMatch(/letterbox/i);
  });
});

describe('detectionArchForSource', () => {
  it.each([
    [MODEL_SOURCE_YOLO, 'yolo'],
    [MODEL_SOURCE_RF_DETR, 'rf_detr'],
    [MODEL_SOURCE_MARKETPLACE, null],
    ['byom', null],
    [undefined, null],
    [null, null],
    ['', null],
    ['YOLO', null],
  ] as const)('%s → %s', (source, arch) => {
    expect(detectionArchForSource(source)).toBe(arch);
  });

  it('round-trips through modelSourceForDetectionArch', () => {
    for (const source of [MODEL_SOURCE_YOLO, MODEL_SOURCE_RF_DETR]) {
      expect(modelSourceForDetectionArch(detectionArchForSource(source))).toBe(source);
    }
    // Records written before RF-DETR existed carry no arch and are YOLO.
    expect(modelSourceForDetectionArch(undefined)).toBe(MODEL_SOURCE_YOLO);
    expect(modelSourceForDetectionArch(null)).toBe(MODEL_SOURCE_YOLO);
  });
});

describe('modelSourceForType / modelSourceForLabelingTask', () => {
  it('maps a cloned model type back to its source', () => {
    expect(modelSourceForType('object_detection')).toBe(MODEL_SOURCE_YOLO);
    expect(modelSourceForType('object_detection', 'yolo')).toBe(MODEL_SOURCE_YOLO);
    expect(modelSourceForType('object_detection', 'rf_detr')).toBe(MODEL_SOURCE_RF_DETR);
    expect(modelSourceForType('classification')).toBe(MODEL_SOURCE_MARKETPLACE);
    expect(modelSourceForType('classification', 'rf_detr')).toBe(MODEL_SOURCE_MARKETPLACE);
    expect(modelSourceForType('segmentation-robust')).toBe(MODEL_SOURCE_MARKETPLACE);
  });

  it('maps a labeling job task type to the source that can train from it', () => {
    expect(modelSourceForLabelingTask('ObjectDetection')).toBe(MODEL_SOURCE_YOLO);
    expect(modelSourceForLabelingTask('object-detection')).toBe(MODEL_SOURCE_YOLO);
    expect(modelSourceForLabelingTask('Classification')).toBe(MODEL_SOURCE_MARKETPLACE);
    expect(modelSourceForLabelingTask('Segmentation')).toBe(MODEL_SOURCE_MARKETPLACE);
    expect(modelSourceForLabelingTask(undefined)).toBe(MODEL_SOURCE_MARKETPLACE);
  });
});

describe('normalizeS3Folder', () => {
  it('yields exactly one trailing slash', () => {
    expect(normalizeS3Folder('s3://b/p')).toBe('s3://b/p/');
    expect(normalizeS3Folder('s3://b/p/')).toBe('s3://b/p/');
    expect(normalizeS3Folder('s3://b/p///')).toBe('s3://b/p/');
    expect(normalizeS3Folder('  s3://b/p ')).toBe('s3://b/p/');
    expect(normalizeS3Folder('')).toBe('');
    expect(normalizeS3Folder(null)).toBe('');
  });
});

describe('labelingJobCoversFolder', () => {
  const folder = 's3://ryvan-cookies/imts-plates-luggage/';

  it('matches a job whose bucket + prefix is the folder, with or without trailing slash', () => {
    expect(labelingJobCoversFolder(
      { dataset_bucket: 'ryvan-cookies', dataset_prefix: 'imts-plates-luggage/' }, folder)).toBe(true);
    expect(labelingJobCoversFolder(
      { dataset_bucket: 'ryvan-cookies', dataset_prefix: 'imts-plates-luggage' }, folder)).toBe(true);
    expect(labelingJobCoversFolder(
      { dataset_bucket: 'ryvan-cookies', dataset_prefix: '/imts-plates-luggage/' }, folder)).toBe(true);
  });

  it('falls back to the folder bucket when the job recorded only a prefix', () => {
    expect(labelingJobCoversFolder({ dataset_prefix: 'imts-plates-luggage/' }, folder)).toBe(true);
  });

  it('does NOT match the sibling-prefix collision or a different bucket', () => {
    expect(labelingJobCoversFolder(
      { dataset_bucket: 'ryvan-cookies', dataset_prefix: 'imts-plates-luggage-other-resolutions/' }, folder)).toBe(false);
    expect(labelingJobCoversFolder(
      { dataset_bucket: 'other-bucket', dataset_prefix: 'imts-plates-luggage/' }, folder)).toBe(false);
    expect(labelingJobCoversFolder(
      { dataset_bucket: 'ryvan-cookies', dataset_prefix: 'imts-plates-luggage/sub/' }, folder)).toBe(false);
  });

  it('is false for missing inputs', () => {
    expect(labelingJobCoversFolder(null, folder)).toBe(false);
    expect(labelingJobCoversFolder({}, folder)).toBe(false);
    expect(labelingJobCoversFolder({ dataset_prefix: 'x/' }, '')).toBe(false);
    expect(labelingJobCoversFolder({ dataset_prefix: 'x/' }, 'not-an-s3-uri/')).toBe(false);
  });
});
