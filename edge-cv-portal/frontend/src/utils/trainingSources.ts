/**
 * Model Source / Model Type vocabulary for the Create Training page.
 *
 * Model Source = which training algorithm / model family runs the job.
 * Model Type  = the task that family supports (filtered by source).
 *
 * The backend only reads `model_source` to decide whether to run the
 * marketplace manifest validator ('marketplace'); everything else keys off
 * `model_type`, so the extra source values are informational to the API.
 */
import type { SelectProps } from '@cloudscape-design/components';
import { isDetectionModelType, OBJECT_DETECTION_MODEL_TYPE } from './manifestFormat';

export const MODEL_SOURCE_MARKETPLACE = 'marketplace';
export const MODEL_SOURCE_YOLO = 'yolo';
export const MODEL_SOURCE_RF_DETR = 'rf_detr';
export const MODEL_SOURCE_BYOM = 'byom';

export const MODEL_SOURCE_OPTIONS: SelectProps.Option[] = [
  {
    label: 'AWS Marketplace - Computer Vision Defect Detection',
    value: MODEL_SOURCE_MARKETPLACE,
    description: 'Anomaly classification and segmentation from anomaly-label manifests (requires subscription).',
  },
  {
    label: 'YOLO (Ultralytics) - Object Detection',
    value: MODEL_SOURCE_YOLO,
    description: 'Fine-tune a YOLO detector on a bounding-box manifest. Exports ONNX for the DDA edge runtime; no compilation step.',
    tags: ['ONNX'],
  },
  {
    label: 'RF-DETR - Object Detection',
    value: MODEL_SOURCE_RF_DETR,
    description: 'Coming soon. To deploy an already-trained RF-DETR ONNX today, use Models → Import (Smart Import).',
    disabled: true,
  },
  {
    label: 'Imported Model (BYOM)',
    value: MODEL_SOURCE_BYOM,
    description: 'Fine-tuning from an imported model is coming soon. To deploy an existing model without training, use Models → Import.',
    disabled: true,
  },
];

export const LFV_MODEL_TYPE_OPTIONS: SelectProps.Option[] = [
  {
    label: 'Classification',
    value: 'classification',
    description: 'Binary normal/anomaly detection. Faster training, smaller model. Best for pass/fail inspection.',
  },
  {
    label: 'Classification (Robust)',
    value: 'classification-robust',
    description: 'Simulates angle and lighting variations. Use when camera position or lighting varies. Training time: 6-24+ hours.',
    tags: ['Long training time'],
  },
  {
    label: 'Segmentation',
    value: 'segmentation',
    description: 'Pixel-level defect localization with mask output. Shows exactly where defects are. Requires mask-annotated training data.',
  },
  {
    label: 'Segmentation (Robust)',
    value: 'segmentation-robust',
    description: 'Segmentation with angle/lighting simulation. Use for variable environments. Training time: 6-24+ hours.',
    tags: ['Long training time'],
  },
];

export const YOLO_MODEL_TYPE_OPTIONS: SelectProps.Option[] = [
  {
    label: 'Object Detection (bounding boxes)',
    value: OBJECT_DETECTION_MODEL_TYPE,
    description: 'Localizes each object with a box and class. Trains letterboxed and is served letterboxed on device.',
    tags: ['ONNX'],
  },
];

export const ALL_MODEL_TYPE_OPTIONS: SelectProps.Option[] = [
  ...LFV_MODEL_TYPE_OPTIONS,
  ...YOLO_MODEL_TYPE_OPTIONS,
];

/** Model types a given model source can train. */
export function modelTypeOptionsForSource(source: unknown): SelectProps.Option[] {
  return source === MODEL_SOURCE_YOLO ? YOLO_MODEL_TYPE_OPTIONS : LFV_MODEL_TYPE_OPTIONS;
}

/** The model source that trains a given model type (used when cloning). */
export function modelSourceForType(modelType: unknown): string {
  return isDetectionModelType(modelType) ? MODEL_SOURCE_YOLO : MODEL_SOURCE_MARKETPLACE;
}

/** The model source that can train from a labeling job's task type. */
export function modelSourceForLabelingTask(taskType: unknown): string {
  return String(taskType ?? '').toLowerCase().includes('detection')
    ? MODEL_SOURCE_YOLO
    : MODEL_SOURCE_MARKETPLACE;
}

/** s3://bucket/prefix with exactly one trailing slash, for folder comparisons. */
export function normalizeS3Folder(uri: string | null | undefined): string {
  if (!uri) return '';
  const trimmed = uri.trim().replace(/\/+$/, '');
  return trimmed ? `${trimmed}/` : '';
}

/**
 * Does a labeling job's dataset location cover the folder the user arrived
 * from (Data Management → "Use for Training")? Jobs record
 * `dataset_bucket` + `dataset_prefix`; older Ground Truth jobs may carry only
 * the prefix, in which case the bucket is taken from the folder URI.
 *
 * Exact-folder match only: `imts-plates-luggage/` must NOT match a job on
 * `imts-plates-luggage-other-resolutions/` (a real prefix-collision trap,
 * docs/detection-training-gap.md §10).
 */
export function labelingJobCoversFolder(
  job: { dataset_bucket?: string; dataset_prefix?: string } | null | undefined,
  folderUri: string
): boolean {
  const folder = normalizeS3Folder(folderUri);
  if (!folder || !folder.startsWith('s3://') || !job?.dataset_prefix) return false;
  const bucket = job.dataset_bucket || folder.slice('s3://'.length).split('/')[0];
  const jobFolder = normalizeS3Folder(`s3://${bucket}/${String(job.dataset_prefix).replace(/^\/+/, '')}`);
  return folder === jobFolder;
}
