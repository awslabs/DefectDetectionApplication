import { describe, expect, it } from 'vitest';
import { classifyManifestFormat, isDetectionModelType } from './manifestFormat';

// Validates: portal-detection-training Requirement 6.2

const ddaDetectionEntry = {
  'source-ref': 's3://bucket/imts-plates-luggage/frame_0001.jpg',
  'bounding-box': {
    image_size: [{ width: 2001, height: 2352, depth: 3 }],
    annotations: [{ class_id: 0, left: 10, top: 20, width: 300, height: 280 }],
  },
  'bounding-box-metadata': {
    objects: [{ confidence: 1 }],
    'class-map': { '0': 'blue_plate' },
    type: 'groundtruth/object-detection',
    'human-annotated': 'yes',
    'creation-date': '2026-09-12T00:00:00',
    'job-name': 'labeling-9cbcdb4c',
  },
};

const gtDetectionEntry = {
  'source-ref': 's3://bucket/frames/frame_0007.jpg',
  'plates-bbox': {
    image_size: [{ width: 1920, height: 1080, depth: 3 }],
    annotations: [{ class_id: 1, left: 5, top: 5, width: 50, height: 50 }],
  },
  'plates-bbox-metadata': {
    objects: [{ confidence: 0.9 }],
    'class-map': { '0': 'plate', '1': 'luggage' },
    type: 'groundtruth/object-detection',
    'human-annotated': 'yes',
    'creation-date': '2026-09-12T00:00:00',
    'job-name': 'plates-bbox',
  },
};

const gtClassificationEntry = {
  'source-ref': 's3://bucket/img.jpg',
  'cookie-classification': 1,
  'cookie-classification-metadata': {
    'class-name': 'anomaly',
    confidence: 0.95,
    type: 'groundtruth/image-classification',
    'job-name': 'cookie-classification',
    'human-annotated': 'yes',
    'creation-date': '2026-01-01T00:00:00',
  },
};

const ddaClassificationEntry = {
  'source-ref': 's3://bucket/img.jpg',
  'anomaly-label': 1,
  'anomaly-label-metadata': {
    'class-name': 'anomaly',
    confidence: 1,
    type: 'groundtruth/image-classification',
    'job-name': 'j',
    'human-annotated': 'yes',
    'creation-date': 'x',
  },
};

const ddaSegmentationEntry = {
  ...ddaClassificationEntry,
  'anomaly-mask-ref': 's3://bucket/masks/img.png',
  'anomaly-mask-ref-metadata': {
    'internal-color-map': { '0': { 'class-name': 'BACKGROUND', 'hex-color': '#ffffff' } },
    type: 'groundtruth/semantic-segmentation',
  },
};

/** The Instance_Manifest line the DDA labeling worker writes (rfdetr-seg-support Req 3.1). */
const instanceEntry = {
  'source-ref': 's3://bucket/cookies/frame_0001.jpg',
  'instance-segmentation': {
    image_size: [{ width: 1920, height: 1080, depth: 3 }],
    annotations: [{ class_id: 0, left: 10, top: 20, width: 300, height: 280, rle: '2073600' }],
  },
  'instance-segmentation-metadata': {
    objects: [{ confidence: 1 }],
    'class-map': { '0': 'crack' },
    type: 'groundtruth/instance-segmentation',
    'human-annotated': 'yes',
    'creation-date': '2026-11-20T00:00:00',
    'job-name': 'labeling-instances',
  },
};

describe('classifyManifestFormat', () => {
  it('recognizes the DDA literal bounding-box attribute as detection', () => {
    expect(classifyManifestFormat(ddaDetectionEntry)).toBe('detection');
  });

  it('recognizes a Ground Truth job-named object-detection attribute as detection, not ground-truth', () => {
    expect(classifyManifestFormat(gtDetectionEntry)).toBe('detection');
  });

  it('still routes a Ground Truth classification manifest to the transformer', () => {
    expect(classifyManifestFormat(gtClassificationEntry)).toBe('ground-truth');
  });

  it('recognizes DDA classification and segmentation manifests as dda', () => {
    expect(classifyManifestFormat(ddaClassificationEntry)).toBe('dda');
    expect(classifyManifestFormat(ddaSegmentationEntry)).toBe('dda');
  });

  it('returns unknown for missing, non-object, or unlabeled entries', () => {
    expect(classifyManifestFormat(undefined)).toBe('unknown');
    expect(classifyManifestFormat(null)).toBe('unknown');
    expect(classifyManifestFormat('s3://x')).toBe('unknown');
    expect(classifyManifestFormat([])).toBe('unknown');
    expect(classifyManifestFormat({ 'source-ref': 's3://bucket/img.jpg' })).toBe('unknown');
  });

  it('does not treat a metadata key with a detection type but no sibling attribute as detection', () => {
    const orphan = {
      'source-ref': 's3://bucket/img.jpg',
      'plates-bbox-metadata': { type: 'groundtruth/object-detection' },
    };
    // No `plates-bbox` object → falls through to the Ground Truth heuristic.
    expect(classifyManifestFormat(orphan)).toBe('ground-truth');
  });

  // rfdetr-seg-support Requirement 3.5: an Instance_Manifest is recognised
  // before the Ground Truth rule, so it is never sent to the transformer.
  it('recognizes an Instance_Manifest entry as instance, not ground-truth', () => {
    expect(classifyManifestFormat(instanceEntry)).toBe('instance');
    // The job-named attribute of a Ground Truth / DDA instance labeling job.
    expect(classifyManifestFormat({
      'source-ref': 's3://bucket/img.jpg',
      'cookies-instances': { image_size: [{ width: 640, height: 480, depth: 3 }], annotations: [] },
      'cookies-instances-metadata': { type: 'GroundTruth/Instance-Segmentation', 'class-map': { '0': 'crack' } },
    })).toBe('instance');
  });

  it('does not treat an instance-segmentation metadata key without a sibling attribute as instance', () => {
    const orphan = {
      'source-ref': 's3://bucket/img.jpg',
      'instance-segmentation-metadata': { type: 'groundtruth/instance-segmentation' },
    };
    expect(classifyManifestFormat(orphan)).toBe('ground-truth');
    // A semantic-segmentation type is not the instance type (exact match).
    expect(classifyManifestFormat({
      'source-ref': 's3://bucket/img.jpg',
      'masks': { x: 1 },
      'masks-metadata': { type: 'groundtruth/semantic-segmentation' },
    })).toBe('ground-truth');
  });
});

describe('isDetectionModelType', () => {
  it('matches only the backend detection-family values: object_detection and instance_segmentation', () => {
    expect(isDetectionModelType('object_detection')).toBe(true);
    expect(isDetectionModelType('instance_segmentation')).toBe(true);
    expect(isDetectionModelType('object-detection')).toBe(false);
    expect(isDetectionModelType('segmentation')).toBe(false);
    expect(isDetectionModelType('segmentation-robust')).toBe(false);
    expect(isDetectionModelType(undefined)).toBe(false);
  });
});
