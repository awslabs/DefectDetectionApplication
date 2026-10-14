/**
 * Helpers behind Smart Import's detector-checkpoint conversion and the
 * Conversion_Record views (detector-checkpoint-import Requirements 3, 10, 11).
 *
 * Bounds, defaults and wording mirror the backend's shared layer
 * `detector_conversion.py` (CHECKPOINT_SIZE_CAP, UPLOAD_EXTENSIONS,
 * INPUT_BOUNDS, INPUT_STEP, DEFAULT_SCORE_THRESHOLD, DEFAULT_IOU_THRESHOLD,
 * RFDETR_SIZES, validate_conversion_request, is_detector_conversion_record),
 * so the form rejects what the API would reject. The server still re-checks
 * everything: convert re-classifies the source and never trusts what this
 * page was shown (Requirement 4.1).
 *
 * Everything here is pure except `putFileWithProgress`, the browser upload of
 * a model file to its presigned URL. That one uses XMLHttpRequest because
 * fetch has no upload-progress events.
 */
import type {
  CheckpointAssessment,
  ConversionStatus,
  DetectionArch,
  RfDetrSize,
} from '../services/api';
import { RFDETR_SIZES, RFDETR_SIZE_ORDER, isRfDetrSize } from './detectionBaseModels';

// ---------------------------------------------------------------------------
// Limits (backend detector_conversion.py)
// ---------------------------------------------------------------------------

/** Checkpoint_Size_Cap: the largest file that upload, inspect and convert accept (512 MiB). */
export const CHECKPOINT_SIZE_CAP_BYTES = 512 * 1024 * 1024;

/** File types `POST /models/upload-url` issues a URL for. */
export const UPLOAD_EXTENSIONS = ['.pt', '.pth', '.onnx'] as const;

/** Sources the inspect route classifies as checkpoints. */
export const CHECKPOINT_EXTENSIONS = ['.pt', '.pth'] as const;

export const INPUT_STEP = 32;

/** The square network inputs each decoder family accepts. */
export const INPUT_BOUNDS: Record<DetectionArch, { min: number; max: number }> = {
  yolo: { min: 320, max: 2048 },
  rf_detr: { min: 224, max: 1120 },
};

export const DEFAULT_SCORE_THRESHOLD: Record<DetectionArch, number> = { yolo: 0.25, rf_detr: 0.5 };
export const DEFAULT_IOU_THRESHOLD = 0.45;

/** Offered when a YOLO checkpoint records no usable `imgsz` (the ultralytics default). */
export const YOLO_FALLBACK_INPUT = 640;

/** Requirement 10.2 verdict for a Convertible_Checkpoint. */
export const CONVERTIBLE_VERDICT = 'Can be converted to ONNX';
export const NOT_CONVERTIBLE_VERDICT = 'Cannot be converted to ONNX';

const has = (obj: object, key: string): boolean => Object.prototype.hasOwnProperty.call(obj, key);

function lowerName(name: string | null | undefined): string {
  return String(name ?? '').toLowerCase();
}

/** A `.pt` / `.pth` file name (case-insensitive). */
export function isCheckpointFileName(name: string | null | undefined): boolean {
  const lower = lowerName(name);
  return CHECKPOINT_EXTENSIONS.some(ext => lower.endsWith(ext));
}

/** "300 B" / "5.2 MiB" / "512 MiB"; an em dash for anything that is not a size. */
export function formatBytes(bytes: number | null | undefined): string {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  if (unit === 0) return `${bytes} B`;
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[unit]}`;
}

/**
 * Why a chosen file cannot be uploaded, or null. These are the same checks
 * the upload-url route answers with HTTP 400 (Requirement 3.3), made before
 * anything is requested.
 */
export function uploadFileProblem(file: { name: string; size: number } | null | undefined): string | null {
  if (!file) return 'Choose a model file to upload';
  const lower = lowerName(file.name);
  if (!UPLOAD_EXTENSIONS.some(ext => lower.endsWith(ext))) {
    return `The file must end in ${UPLOAD_EXTENSIONS.join(', ')}; got ${file.name}`;
  }
  if (!(file.size > 0)) return `${file.name} is empty`;
  if (file.size > CHECKPOINT_SIZE_CAP_BYTES) {
    return `${file.name} is ${formatBytes(file.size)}; the checkpoint size cap is ${formatBytes(CHECKPOINT_SIZE_CAP_BYTES)}`;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Conversion_Record
// ---------------------------------------------------------------------------

/** The record fields the predicates read: a TrainingJob, a TrainingJobRecord, or any subset. */
export interface ConversionRecordLike {
  source?: string | null;
  model_type?: string | null;
  runtime?: string | null;
  conversion?: unknown;
}

/** Mirrors the backend's `detector_conversion.is_detector_conversion_record` (Requirement 9.1). */
export function isDetectorConversionRecord(record: ConversionRecordLike | null | undefined): boolean {
  if (!record || typeof record !== 'object') return false;
  const conversion = record.conversion;
  return (
    record.source === 'imported' &&
    record.model_type === 'object_detection' &&
    String(record.runtime ?? '').toLowerCase() === 'onnx' &&
    !!conversion &&
    typeof conversion === 'object' &&
    !Array.isArray(conversion)
  );
}

const CONVERSION_STATUSES: readonly ConversionStatus[] = ['InProgress', 'Finalizing', 'Completed', 'Failed'];

function asConversionStatus(value: unknown): ConversionStatus | null {
  return CONVERSION_STATUSES.includes(value as ConversionStatus) ? (value as ConversionStatus) : null;
}

/** A Conversion_Record's `conversion.status`; null for any other record or an unknown value. */
export function conversionStatusOf(record: ConversionRecordLike | null | undefined): ConversionStatus | null {
  if (!record || !isDetectorConversionRecord(record)) return null;
  return asConversionStatus((record.conversion as { status?: unknown }).status);
}

/** Requirement 11.1 labels. */
export const CONVERSION_STATUS_LABELS: Record<ConversionStatus, string> = {
  InProgress: 'Converting to ONNX',
  Finalizing: 'Validating and packaging',
  Completed: 'Completed',
  Failed: 'Conversion failed',
};

/** The Requirement 11.1 label for a conversion state; anything unrecognised is shown as-is. */
export function conversionStatusLabel(status: string | null | undefined): string {
  const known = asConversionStatus(status);
  if (known) return CONVERSION_STATUS_LABELS[known];
  return status ? String(status) : 'Unknown';
}

export type ConversionIndicatorType = 'in-progress' | 'loading' | 'success' | 'error' | 'info';

/** The StatusIndicator type that goes with a conversion state. */
export function conversionStatusIndicatorType(status: string | null | undefined): ConversionIndicatorType {
  switch (asConversionStatus(status)) {
    case 'InProgress':
      return 'in-progress';
    case 'Finalizing':
      return 'loading';
    case 'Completed':
      return 'success';
    case 'Failed':
      return 'error';
    default:
      return 'info';
  }
}

/** Still moving: the record page keeps polling (Requirement 11.2). */
export function isConversionActive(status: string | null | undefined): boolean {
  const known = asConversionStatus(status);
  return known === 'InProgress' || known === 'Finalizing';
}

// ---------------------------------------------------------------------------
// Checkpoint panel (Requirement 10.2)
// ---------------------------------------------------------------------------

const KIND_LABELS: Record<string, string> = {
  ultralytics_checkpoint: 'Ultralytics YOLO',
  rfdetr_checkpoint: 'RF-DETR',
  torchscript: 'TorchScript graph',
  state_dict: 'PyTorch state_dict (weights only)',
  legacy_torch: 'Legacy torch file',
  onnx: 'ONNX graph (saved with a .pt name)',
  unknown: 'Unrecognised file',
};

/** "Ultralytics YOLO", "RF-DETR (small)", "TorchScript graph", and so on. */
export function checkpointFamilyLabel(assessment: CheckpointAssessment | null | undefined): string {
  const kind = String(assessment?.kind ?? 'unknown');
  const label = has(KIND_LABELS, kind) ? KIND_LABELS[kind] : KIND_LABELS.unknown;
  const size = assessment?.rfdetr_size;
  if (kind === 'rfdetr_checkpoint' && isRfDetrSize(size)) return `${label} (${size})`;
  return label;
}

/**
 * The library that saved the checkpoint and its version ("ultralytics
 * 8.4.2"). The name alone, marked, when the file records no version; null
 * when the file is not a training checkpoint at all.
 */
export function checkpointLibraryLabel(assessment: CheckpointAssessment | null | undefined): string | null {
  const framework = assessment?.framework;
  if (!framework) return null;
  const version = assessment?.framework_version;
  return version ? `${framework} ${version}` : `${framework} (version not recorded)`;
}

// ---------------------------------------------------------------------------
// Locks and pre-fill for a Convertible_Checkpoint (Requirement 10.3)
// ---------------------------------------------------------------------------

/** What the Smart Import form locks, and the values it pre-fills, for a Convertible_Checkpoint. */
export interface ConversionLocks {
  arch: DetectionArch;
  modelType: 'object_detection';
  exportFormat: 'onnx';
  /** Locked: the checkpoint's head. Names may be renamed but not added or removed. */
  numClasses: number;
  /** Pre-fill in class-index order; blank entries when the checkpoint stores no names. */
  classNames: string[];
  /** The training size, else the arch default; null when the user must choose (RF-DETR of unknown size). */
  networkInput: number | null;
  inputBounds: { min: number; max: number; step: number };
  /** RF-DETR: the native resolution(s) it converts at. Null for YOLO (any size within the bounds). */
  allowedInputs: number[] | null;
  rfdetrSize: RfDetrSize | null;
  scoreThreshold: number;
  /** YOLO only; RF-DETR decodes set-based top-k with no NMS. */
  iouThreshold: number | null;
  /** Derived from the arch, never chosen (Requirement 4.5). */
  preserveAspect: boolean;
  geometry: string;
  geometryReason: string;
}

export const CONVERSION_GEOMETRY: Record<DetectionArch, { label: string; reason: string }> = {
  yolo: {
    label: 'Letterbox (aspect ratio preserved)',
    reason:
      'Ultralytics trains YOLO letterboxed: the frame is scaled by a single ratio and centre-padded ' +
      'to the square network input.',
  },
  rf_detr: {
    label: 'Square resize with ImageNet normalisation',
    reason:
      'RF-DETR is trained on a square resize of the frame, normalised with the ImageNet mean and ' +
      'standard deviation.',
  },
};

function isValidYoloInput(value: number): boolean {
  const { min, max } = INPUT_BOUNDS.yolo;
  return Number.isInteger(value) && value % INPUT_STEP === 0 && value >= min && value <= max;
}

/**
 * The locks and pre-fill for a Convertible_Checkpoint, or null when the
 * assessment is absent or not convertible (the form then behaves as before,
 * with ONNX output disabled).
 */
export function conversionLocksFor(assessment: CheckpointAssessment | null | undefined): ConversionLocks | null {
  if (!assessment || assessment.convertible !== true) return null;
  const arch: DetectionArch | null =
    assessment.arch === 'yolo' || assessment.arch === 'rf_detr' ? assessment.arch : null;
  const numClasses = assessment.num_classes;
  if (!arch || typeof numClasses !== 'number' || !Number.isInteger(numClasses) || numClasses < 1) {
    return null;
  }
  const classNames =
    Array.isArray(assessment.class_names) && assessment.class_names.length === numClasses
      ? assessment.class_names.map(name => String(name))
      : Array.from({ length: numClasses }, () => '');
  const trained = assessment.train_input_size;
  const size = isRfDetrSize(assessment.rfdetr_size) ? assessment.rfdetr_size : null;

  let allowedInputs: number[] | null = null;
  let networkInput: number | null;
  if (arch === 'rf_detr') {
    allowedInputs = size ? [RFDETR_SIZES[size]] : RFDETR_SIZE_ORDER.map(s => RFDETR_SIZES[s]);
    if (typeof trained === 'number' && allowedInputs.includes(trained)) networkInput = trained;
    else networkInput = allowedInputs.length === 1 ? allowedInputs[0] : null;
  } else {
    networkInput = typeof trained === 'number' && isValidYoloInput(trained) ? trained : YOLO_FALLBACK_INPUT;
  }

  return {
    arch,
    modelType: 'object_detection',
    exportFormat: 'onnx',
    numClasses,
    classNames,
    networkInput,
    inputBounds: { ...INPUT_BOUNDS[arch], step: INPUT_STEP },
    allowedInputs,
    rfdetrSize: arch === 'rf_detr' ? size : null,
    scoreThreshold: DEFAULT_SCORE_THRESHOLD[arch],
    iouThreshold: arch === 'yolo' ? DEFAULT_IOU_THRESHOLD : null,
    preserveAspect: arch === 'yolo',
    geometry: CONVERSION_GEOMETRY[arch].label,
    geometryReason: CONVERSION_GEOMETRY[arch].reason,
  };
}

// ---------------------------------------------------------------------------
// Validation (the convert route's Requirement 4.3 rules, same wording)
// ---------------------------------------------------------------------------

/**
 * The class names must be exactly `count` non-empty strings: a supplied list
 * may rename classes but never change their count (Requirement 4.4). Returns
 * the problem, or null.
 */
export function validateClassNames(names: ReadonlyArray<string> | null | undefined, count: number): string | null {
  const list = Array.isArray(names) ? names : [];
  if (list.length !== count) {
    return (
      `Class names must list exactly ${count} ${count === 1 ? 'class' : 'classes'} (the checkpoint's head); ` +
      `got ${list.length}. Names may be renamed but not added or removed`
    );
  }
  const blank = list.findIndex(name => !String(name ?? '').trim());
  if (blank >= 0) return `Class ${blank} needs a name`;
  return null;
}

/** The network input's problem under `locks`, or null. */
export function networkInputProblem(locks: ConversionLocks, value: string | number | null | undefined): string | null {
  const text = String(value ?? '').trim();
  const n = Number(text);
  if (!text || !Number.isInteger(n)) return 'Enter the network input size in pixels';
  if (locks.allowedInputs) {
    if (locks.allowedInputs.includes(n)) return null;
    return locks.rfdetrSize
      ? `RF-DETR ${locks.rfdetrSize} converts only at its native ${RFDETR_SIZES[locks.rfdetrSize]}px; got ${n}`
      : `RF-DETR converts only at a native resolution (${locks.allowedInputs.join(', ')}); got ${n}`;
  }
  const { min, max, step } = locks.inputBounds;
  if (n % step !== 0 || n < min || n > max) {
    return `The network input must be a multiple of ${step} between ${min} and ${max} for YOLO; got ${n}`;
  }
  return null;
}

/** A threshold must be strictly between 0 and 1. Returns the problem, or null. */
export function thresholdProblem(label: string, value: string | number | null | undefined): string | null {
  const text = String(value ?? '').trim();
  const n = Number(text);
  if (!text || !Number.isFinite(n) || !(n > 0 && n < 1)) return `${label} must be strictly between 0 and 1`;
  return null;
}

// ---------------------------------------------------------------------------
// Browser upload (Requirement 3)
// ---------------------------------------------------------------------------

/**
 * PUT `file` to a presigned URL, reporting whole-number percent progress.
 * No Content-Type header is set: the URL is SigV4-signed without it
 * (Requirement 3.4), so whatever the browser sends is accepted.
 */
export function putFileWithProgress(
  url: string,
  file: Blob,
  onProgress?: (percent: number) => void
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    xhr.upload.onprogress = event => {
      if (onProgress && event.lengthComputable && event.total > 0) {
        onProgress(Math.min(100, Math.floor((event.loaded / event.total) * 100)));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100);
        resolve();
        return;
      }
      // S3 errors are XML; its <Code> (AccessDenied, RequestTimeTooSkewed, ...) says why.
      const code = /<Code>([^<]+)<\/Code>/.exec(String(xhr.responseText ?? ''))?.[1];
      reject(new Error(`Upload failed (HTTP ${xhr.status}${code ? ` ${code}` : ''})`));
    };
    xhr.onerror = () =>
      reject(new Error('Upload failed: the browser could not reach the bucket (network error or bucket CORS)'));
    xhr.onabort = () => reject(new Error('Upload cancelled'));
    xhr.send(file);
  });
}
