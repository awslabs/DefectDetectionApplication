import { describe, expect, it } from 'vitest';
import {
  ALL_MODEL_TYPE_OPTIONS,
  LFV_MODEL_TYPE_OPTIONS,
  MODEL_SOURCE_BYOM,
  MODEL_SOURCE_MARKETPLACE,
  MODEL_SOURCE_OPTIONS,
  MODEL_SOURCE_RF_DETR,
  MODEL_SOURCE_YOLO,
  YOLO_MODEL_TYPE_OPTIONS,
  labelingJobCoversFolder,
  modelSourceForLabelingTask,
  modelSourceForType,
  modelTypeOptionsForSource,
  normalizeS3Folder,
} from './trainingSources';

describe('Model Source options', () => {
  it('offers marketplace, YOLO, RF-DETR and BYOM in that order', () => {
    expect(MODEL_SOURCE_OPTIONS.map(o => o.value)).toEqual([
      MODEL_SOURCE_MARKETPLACE,
      MODEL_SOURCE_YOLO,
      MODEL_SOURCE_RF_DETR,
      MODEL_SOURCE_BYOM,
    ]);
  });

  it('enables exactly the two sources that have a training path today', () => {
    const enabled = MODEL_SOURCE_OPTIONS.filter(o => !o.disabled).map(o => o.value);
    expect(enabled).toEqual([MODEL_SOURCE_MARKETPLACE, MODEL_SOURCE_YOLO]);
    // The disabled ones say so and point at Smart Import instead of dead-ending.
    for (const o of MODEL_SOURCE_OPTIONS.filter(o => o.disabled)) {
      expect(o.description).toMatch(/Coming soon/i);
      expect(o.description).toMatch(/Models → Import/);
    }
  });
});

describe('modelTypeOptionsForSource', () => {
  it('filters model types by source', () => {
    expect(modelTypeOptionsForSource(MODEL_SOURCE_MARKETPLACE)).toBe(LFV_MODEL_TYPE_OPTIONS);
    expect(modelTypeOptionsForSource(MODEL_SOURCE_YOLO)).toBe(YOLO_MODEL_TYPE_OPTIONS);
    expect(modelTypeOptionsForSource(undefined)).toBe(LFV_MODEL_TYPE_OPTIONS);
  });

  it('YOLO trains object_detection only; marketplace never does', () => {
    expect(YOLO_MODEL_TYPE_OPTIONS.map(o => o.value)).toEqual(['object_detection']);
    expect(LFV_MODEL_TYPE_OPTIONS.map(o => o.value)).toEqual([
      'classification', 'classification-robust', 'segmentation', 'segmentation-robust',
    ]);
    expect(ALL_MODEL_TYPE_OPTIONS.length).toBe(5);
  });
});

describe('modelSourceForType / modelSourceForLabelingTask', () => {
  it('maps a cloned model type back to its source', () => {
    expect(modelSourceForType('object_detection')).toBe(MODEL_SOURCE_YOLO);
    expect(modelSourceForType('classification')).toBe(MODEL_SOURCE_MARKETPLACE);
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
