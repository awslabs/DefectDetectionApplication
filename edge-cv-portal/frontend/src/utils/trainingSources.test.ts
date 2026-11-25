import { describe, expect, it } from 'vitest';
import {
  ALL_MODEL_TYPE_OPTIONS,
  LFV_MODEL_TYPE_OPTIONS,
  MODEL_SOURCE_MARKETPLACE,
  MODEL_SOURCE_OPTIONS,
  MODEL_SOURCE_RF_DETR,
  MODEL_SOURCE_YOLO,
  RF_DETR_MODEL_TYPE_OPTIONS,
  RF_DETR_SEG_MODEL_TYPE_OPTION,
  YOLO_MODEL_TYPE_OPTIONS,
  detectionArchFor,
  detectionArchForSource,
  isInstanceSegmentationTaskType,
  labelingJobCoversFolder,
  modelSourceForDetectionArch,
  modelSourceForLabelingTask,
  modelSourceForType,
  modelTypeOptionsForSource,
  normalizeS3Folder,
  visibleModelTypeOptionsForSource,
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

  it('YOLO trains object_detection only; RF-DETR trains object_detection and instance_segmentation; marketplace never does', () => {
    expect(YOLO_MODEL_TYPE_OPTIONS.map(o => o.value)).toEqual(['object_detection']);
    expect(RF_DETR_MODEL_TYPE_OPTIONS.map(o => o.value)).toEqual(['object_detection', 'instance_segmentation']);
    expect(LFV_MODEL_TYPE_OPTIONS.map(o => o.value)).toEqual([
      'classification', 'classification-robust', 'segmentation', 'segmentation-robust',
    ]);
    // One entry per model_type value (the two detection sources share
    // object_detection; instance_segmentation is RF-DETR's alone).
    expect(ALL_MODEL_TYPE_OPTIONS.length).toBe(6);
    expect(new Set(ALL_MODEL_TYPE_OPTIONS.map(o => o.value)).size).toBe(6);
    expect(ALL_MODEL_TYPE_OPTIONS).toContain(RF_DETR_SEG_MODEL_TYPE_OPTION);
  });

  // rfdetr-seg-support Requirement 12.2 (O13 a): the second RF-DETR entry.
  it('labels the RF-DETR-Seg entry "Instance Segmentation (masks + boxes)" with an ONNX tag', () => {
    expect(RF_DETR_SEG_MODEL_TYPE_OPTION).toMatchObject({
      label: 'Instance Segmentation (masks + boxes)',
      value: 'instance_segmentation',
      tags: ['ONNX'],
    });
    expect(RF_DETR_SEG_MODEL_TYPE_OPTION.description).toMatch(/mask/i);
    expect(RF_DETR_SEG_MODEL_TYPE_OPTION.description).not.toMatch(/letterbox/i);
  });

  it('RF-DETR describes its own geometry contract (square resize, no NMS), not letterboxing', () => {
    expect(RF_DETR_MODEL_TYPE_OPTIONS[0].description).toMatch(/square resize/i);
    expect(RF_DETR_MODEL_TYPE_OPTIONS[0].description).not.toMatch(/letterbox/i);
    expect(YOLO_MODEL_TYPE_OPTIONS[0].description).toMatch(/letterbox/i);
  });
});

// rfdetr-seg-support Requirement 15.2: the Seg_Switch filter lives in the
// sibling helper, never inside modelTypeOptionsForSource (the identity pin
// above holds).
describe('visibleModelTypeOptionsForSource', () => {
  it('offers both RF-DETR types when the config field is absent or true', () => {
    expect(visibleModelTypeOptionsForSource(MODEL_SOURCE_RF_DETR, {})).toBe(RF_DETR_MODEL_TYPE_OPTIONS);
    expect(visibleModelTypeOptionsForSource(MODEL_SOURCE_RF_DETR, { rfDetrSegEnabled: true })).toBe(RF_DETR_MODEL_TYPE_OPTIONS);
    expect(visibleModelTypeOptionsForSource(MODEL_SOURCE_RF_DETR, undefined).map(o => o.value)).toEqual([
      'object_detection', 'instance_segmentation',
    ]);
  });

  it('hides the Instance Segmentation entry when the switch is off; the other sources are unchanged', () => {
    const off = { rfDetrSegEnabled: false };
    expect(visibleModelTypeOptionsForSource(MODEL_SOURCE_RF_DETR, off).map(o => o.value)).toEqual(['object_detection']);
    expect(visibleModelTypeOptionsForSource(MODEL_SOURCE_YOLO, off)).toBe(YOLO_MODEL_TYPE_OPTIONS);
    expect(visibleModelTypeOptionsForSource(MODEL_SOURCE_MARKETPLACE, off)).toBe(LFV_MODEL_TYPE_OPTIONS);
    // The constant itself is untouched by the filter.
    expect(RF_DETR_MODEL_TYPE_OPTIONS.map(o => o.value)).toEqual(['object_detection', 'instance_segmentation']);
  });
});

describe('detectionArchFor (source + model type -> the request detection_arch)', () => {
  it.each([
    [MODEL_SOURCE_RF_DETR, 'instance_segmentation', 'rf_detr_seg'],
    [MODEL_SOURCE_RF_DETR, 'object_detection', 'rf_detr'],
    [MODEL_SOURCE_RF_DETR, undefined, 'rf_detr'],
    [MODEL_SOURCE_YOLO, 'object_detection', 'yolo'],
    [MODEL_SOURCE_YOLO, 'instance_segmentation', 'yolo'],
    [MODEL_SOURCE_MARKETPLACE, 'segmentation', null],
    [MODEL_SOURCE_MARKETPLACE, 'instance_segmentation', null],
    [undefined, 'instance_segmentation', null],
  ] as const)('%s + %s → %s', (source, modelType, arch) => {
    expect(detectionArchFor(source, modelType)).toBe(arch);
  });

  it('agrees with detectionArchForSource for every object_detection pair', () => {
    for (const source of [MODEL_SOURCE_YOLO, MODEL_SOURCE_RF_DETR, MODEL_SOURCE_MARKETPLACE]) {
      expect(detectionArchFor(source, 'object_detection')).toBe(detectionArchForSource(source));
    }
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

  it('maps rf_detr_seg to the RF-DETR source (cloning a Seg job lands there)', () => {
    expect(modelSourceForDetectionArch('rf_detr_seg')).toBe(MODEL_SOURCE_RF_DETR);
    expect(modelSourceForDetectionArch(detectionArchFor(MODEL_SOURCE_RF_DETR, 'instance_segmentation'))).toBe(MODEL_SOURCE_RF_DETR);
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

  // rfdetr-seg-support Requirement 12.2: the Seg job's source and type.
  it('maps a cloned instance_segmentation job to the RF-DETR source', () => {
    expect(modelSourceForType('instance_segmentation', 'rf_detr_seg')).toBe(MODEL_SOURCE_RF_DETR);
    // Defensive: a Seg record without its arch still lands on a detection source, never the marketplace.
    expect(modelSourceForType('instance_segmentation')).toBe(MODEL_SOURCE_YOLO);
  });

  it('maps an InstanceSegmentation labeling job to the RF-DETR source (case-insensitive), Segmentation stays marketplace', () => {
    expect(modelSourceForLabelingTask('InstanceSegmentation')).toBe(MODEL_SOURCE_RF_DETR);
    expect(modelSourceForLabelingTask('instancesegmentation')).toBe(MODEL_SOURCE_RF_DETR);
    expect(isInstanceSegmentationTaskType('InstanceSegmentation')).toBe(true);
    expect(isInstanceSegmentationTaskType('Segmentation')).toBe(false);
    expect(isInstanceSegmentationTaskType('instance-segmentation')).toBe(false);
    expect(isInstanceSegmentationTaskType(undefined)).toBe(false);
    expect(modelSourceForLabelingTask('Segmentation')).toBe(MODEL_SOURCE_MARKETPLACE);
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
  const folder = 's3://example-bucket/imts-plates-luggage/';

  it('matches a job whose bucket + prefix is the folder, with or without trailing slash', () => {
    expect(labelingJobCoversFolder(
      { dataset_bucket: 'example-bucket', dataset_prefix: 'imts-plates-luggage/' }, folder)).toBe(true);
    expect(labelingJobCoversFolder(
      { dataset_bucket: 'example-bucket', dataset_prefix: 'imts-plates-luggage' }, folder)).toBe(true);
    expect(labelingJobCoversFolder(
      { dataset_bucket: 'example-bucket', dataset_prefix: '/imts-plates-luggage/' }, folder)).toBe(true);
  });

  it('falls back to the folder bucket when the job recorded only a prefix', () => {
    expect(labelingJobCoversFolder({ dataset_prefix: 'imts-plates-luggage/' }, folder)).toBe(true);
  });

  it('does NOT match the sibling-prefix collision or a different bucket', () => {
    expect(labelingJobCoversFolder(
      { dataset_bucket: 'example-bucket', dataset_prefix: 'imts-plates-luggage-other-resolutions/' }, folder)).toBe(false);
    expect(labelingJobCoversFolder(
      { dataset_bucket: 'other-bucket', dataset_prefix: 'imts-plates-luggage/' }, folder)).toBe(false);
    expect(labelingJobCoversFolder(
      { dataset_bucket: 'example-bucket', dataset_prefix: 'imts-plates-luggage/sub/' }, folder)).toBe(false);
  });

  it('is false for missing inputs', () => {
    expect(labelingJobCoversFolder(null, folder)).toBe(false);
    expect(labelingJobCoversFolder({}, folder)).toBe(false);
    expect(labelingJobCoversFolder({ dataset_prefix: 'x/' }, '')).toBe(false);
    expect(labelingJobCoversFolder({ dataset_prefix: 'x/' }, 'not-an-s3-uri/')).toBe(false);
  });
});
