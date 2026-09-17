/**
 * Pure helpers behind the Create Training "Detection Settings" panel and its
 * Base model control (rfdetr-training-and-transfer-learning Req 3.5, 3.6,
 * 6.1, 6.2). No React, no network: `CreateTraining.tsx` wires these to state
 * and `apiService.listDetectionBaseModels` feeds `groupDetectionBaseModels`
 * with the `GET /training` list.
 *
 * Bounds and defaults mirror the backend's `detection_training.py`
 * (`DETECTION_DEFAULTS`, `RFDETR_DEFAULTS`, `RFDETR_SIZES`,
 * `parse_detection_hyperparameters`) so the form rejects exactly what the
 * API would reject, with the same wording.
 */
import type { SelectProps } from '@cloudscape-design/components';
import type {
  BaseModelKind,
  BaseModelRef,
  DetectionArch,
  DetectionBaseModels,
  DetectionHyperparameters,
  PublishedCheckpoint,
  RfDetrHyperparameters,
  RfDetrSize,
  TrainingJobRecord,
  YoloHyperparameters,
} from '../services/api';
import { OBJECT_DETECTION_MODEL_TYPE } from './manifestFormat';

// ---------------------------------------------------------------------------
// Published checkpoints
// ---------------------------------------------------------------------------

/** RF-DETR size -> native square resolution (the default `resolution`). */
export const RFDETR_SIZES: Record<RfDetrSize, number> = {
  nano: 384,
  small: 512,
  medium: 576,
  large: 704,
};

export const RFDETR_SIZE_ORDER: RfDetrSize[] = ['nano', 'small', 'medium', 'large'];

export function isRfDetrSize(value: unknown): value is RfDetrSize {
  return typeof value === 'string' && value in RFDETR_SIZES;
}

export function rfdetrNativeResolution(size: unknown): number {
  return isRfDetrSize(size) ? RFDETR_SIZES[size] : RFDETR_SIZES.small;
}

/** The COCO checkpoints each arch's entry point can start from (Req 6.1 "published"). */
export const PUBLISHED_CHECKPOINTS: Record<DetectionArch, PublishedCheckpoint[]> = {
  yolo: [
    { ref: 'yolo11n.pt', label: 'yolo11n.pt (nano — fastest on device)' },
    { ref: 'yolo11s.pt', label: 'yolo11s.pt (small — recommended)' },
    { ref: 'yolo11m.pt', label: 'yolo11m.pt (medium — more accurate, slower)' },
  ],
  rf_detr: RFDETR_SIZE_ORDER.map(size => ({
    ref: size,
    label: `RF-DETR ${size} (COCO, native ${RFDETR_SIZES[size]}px)${size === 'small' ? ' — recommended' : ''}`,
    native_resolution: RFDETR_SIZES[size],
  })),
};

// ---------------------------------------------------------------------------
// Base model grouping (feeds the grouped Select)
// ---------------------------------------------------------------------------

/** Mirrors `detection_training.is_trained_detection_record`. */
export function isTrainedDetectionRecord(job: TrainingJobRecord | null | undefined): boolean {
  if (!job || typeof job !== 'object') return false;
  if (job.model_type !== OBJECT_DETECTION_MODEL_TYPE) return false;
  if (String(job.runtime ?? '').toLowerCase() !== 'onnx') return false;
  return job.source !== 'imported';
}

/** A record's detector family; records written before RF-DETR existed are YOLO. */
export function recordDetectionArch(job: TrainingJobRecord | null | undefined): DetectionArch {
  return job?.detection?.detection_arch === 'rf_detr' ? 'rf_detr' : 'yolo';
}

/**
 * Split the use case's `GET /training` list into what the Base model control
 * offers for `arch` (Req 6.1): the arch's published checkpoints, Completed
 * portal-trained detectors of the same arch (newest first, as the API
 * returns them), and imported records whose `metadata.fine_tunable.arch`
 * matches (Req 7). Anything else — LFV jobs, in-progress or failed
 * detectors, other-arch detectors, ONNX/TorchScript-only imports — is left
 * out rather than offered and rejected with a 400 later.
 */
export function groupDetectionBaseModels(
  jobs: ReadonlyArray<TrainingJobRecord> | null | undefined,
  arch: DetectionArch
): DetectionBaseModels {
  const list = Array.isArray(jobs) ? jobs : [];
  const trainedDetectors = list.filter(
    job => isTrainedDetectionRecord(job) && job.status === 'Completed' && recordDetectionArch(job) === arch
  );
  const fineTunableImports = list.filter(job => {
    if (!job || job.source !== 'imported') return false;
    const fineTunable = job.metadata?.fine_tunable;
    return !!fineTunable && typeof fineTunable === 'object'
      && fineTunable.arch === arch && !!fineTunable.checkpoint_s3;
  });
  return { arch, published: PUBLISHED_CHECKPOINTS[arch], trainedDetectors, fineTunableImports };
}

const OPTION_VALUE_SEPARATOR = ':';

/** Encode a `{kind, ref}` as a Select option value (`kind:ref`). */
export function baseModelOptionValue(ref: BaseModelRef): string {
  return `${ref.kind}${OPTION_VALUE_SEPARATOR}${ref.ref}`;
}

/** Inverse of `baseModelOptionValue`; null for anything malformed. */
export function parseBaseModelOptionValue(value: unknown): BaseModelRef | null {
  if (typeof value !== 'string') return null;
  const idx = value.indexOf(OPTION_VALUE_SEPARATOR);
  if (idx <= 0) return null;
  const kind = value.slice(0, idx);
  const ref = value.slice(idx + 1);
  if (!ref) return null;
  if (kind !== 'published' && kind !== 'training_job' && kind !== 'imported') return null;
  return { kind: kind as BaseModelKind, ref };
}

export const BASE_MODEL_GROUP_LABELS = {
  published: 'Published checkpoints',
  training_job: 'My trained detectors (same arch)',
  imported: 'Imported checkpoints',
} as const;

function recordLabel(job: TrainingJobRecord): string {
  const version = job.model_version ? ` v${job.model_version}` : '';
  return `${job.model_name || job.training_id}${version}`;
}

function trainedDetectorOption(job: TrainingJobRecord): SelectProps.Option {
  const det = job.detection;
  const classes = det?.class_names?.length ? `Classes: ${det.class_names.join(', ')}` : 'Classes: (unknown)';
  const input = det?.network_input_width ? ` • ${det.network_input_width}px input` : '';
  const map50 = job.metrics?.['test:mAP50'];
  return {
    label: recordLabel(job),
    value: baseModelOptionValue({ kind: 'training_job', ref: job.training_id }),
    description: `${classes}${input}`,
    tags: typeof map50 === 'number' ? [`mAP@50 ${(map50 * 100).toFixed(1)}%`] : undefined,
  };
}

function importedOption(job: TrainingJobRecord): SelectProps.Option {
  const fineTunable = job.metadata?.fine_tunable;
  const classes = fineTunable?.class_names?.length
    ? `Classes: ${fineTunable.class_names.join(', ')}`
    : 'Imported fine-tunable checkpoint';
  return {
    label: recordLabel(job),
    value: baseModelOptionValue({ kind: 'imported', ref: job.training_id }),
    description: classes,
    tags: fineTunable?.kind ? [String(fineTunable.kind)] : undefined,
  };
}

/**
 * Grouped options for the Base model `Select`: "Published checkpoints" /
 * "My trained detectors (same arch)" / "Imported checkpoints". Empty groups
 * are omitted so the dropdown never shows a header with nothing under it.
 */
export function baseModelSelectOptions(groups: DetectionBaseModels): SelectProps.OptionGroup[] {
  const out: SelectProps.OptionGroup[] = [
    {
      label: BASE_MODEL_GROUP_LABELS.published,
      options: groups.published.map(p => ({
        label: p.label,
        value: baseModelOptionValue({ kind: 'published', ref: p.ref }),
      })),
    },
  ];
  if (groups.trainedDetectors.length > 0) {
    out.push({
      label: BASE_MODEL_GROUP_LABELS.training_job,
      options: groups.trainedDetectors.map(trainedDetectorOption),
    });
  }
  if (groups.fineTunableImports.length > 0) {
    out.push({
      label: BASE_MODEL_GROUP_LABELS.imported,
      options: groups.fineTunableImports.map(importedOption),
    });
  }
  return out;
}

/** The option matching `ref` across all groups (for the Select's selectedOption). */
export function findBaseModelOption(
  options: ReadonlyArray<SelectProps.OptionGroup>,
  ref: BaseModelRef | null | undefined
): SelectProps.Option | null {
  if (!ref) return null;
  const wanted = baseModelOptionValue(ref);
  for (const group of options) {
    const hit = group.options.find(o => o.value === wanted);
    if (hit) return hit;
  }
  return null;
}

/** The base record a non-published `ref` points at, if it is on offer. */
export function findBaseModelRecord(
  groups: DetectionBaseModels | null | undefined,
  ref: BaseModelRef | null | undefined
): TrainingJobRecord | undefined {
  if (!groups || !ref || ref.kind === 'published') return undefined;
  const pool = ref.kind === 'training_job' ? groups.trainedDetectors : groups.fineTunableImports;
  return pool.find(job => job.training_id === ref.ref);
}

/**
 * The class list a base model was trained with (what the class head knows),
 * or null for a published COCO checkpoint / an unknown ref.
 */
export function baseModelClassNames(record: TrainingJobRecord | null | undefined): string[] | null {
  if (!record) return null;
  const fromImport = record.metadata?.fine_tunable?.class_names;
  const fromMeta = record.metadata?.class_names;
  const fromDetection = record.detection?.class_names;
  const names = (fromImport?.length ? fromImport : undefined)
    ?? (Array.isArray(fromMeta) && fromMeta.length ? (fromMeta as unknown[]) : undefined)
    ?? (fromDetection?.length ? fromDetection : undefined);
  return names ? names.map(n => String(n)) : null;
}

// ---------------------------------------------------------------------------
// Class names
// ---------------------------------------------------------------------------

/**
 * Ordered class names from a manifest sample entry's `<attr>-metadata.class-map`
 * (`{"0": "plate", "1": "luggage"}` -> `['plate', 'luggage']`), exactly as the
 * backend's `class_names_from_class_map` orders them. Empty when the entry is
 * not a bounding-box entry.
 */
export function classNamesFromManifestEntry(sampleEntry: unknown): string[] {
  if (!sampleEntry || typeof sampleEntry !== 'object' || Array.isArray(sampleEntry)) return [];
  const entry = sampleEntry as Record<string, unknown>;
  for (const [key, value] of Object.entries(entry)) {
    if (!key.endsWith('-metadata') || !value || typeof value !== 'object') continue;
    const classMap = (value as { 'class-map'?: unknown })['class-map'];
    if (!classMap || typeof classMap !== 'object' || Array.isArray(classMap)) continue;
    const ids = Object.keys(classMap as Record<string, unknown>);
    if (ids.length === 0 || ids.some(id => !/^\d+$/.test(id))) continue;
    return ids
      .sort((a, b) => Number(a) - Number(b))
      .map(id => String((classMap as Record<string, unknown>)[id]));
  }
  return [];
}

/** "a, b,c\nd" -> ['a', 'b', 'c', 'd']; blanks dropped. */
export function parseClassNamesInput(text: string | null | undefined): string[] {
  return String(text ?? '')
    .split(/[,\n]/)
    .map(s => s.trim())
    .filter(Boolean);
}

/**
 * Do two class lists name a different head? Order-sensitive on purpose: the
 * class id IS the output index, so `[a, b]` vs `[b, a]` needs a re-init too.
 */
export function classListsDiffer(a: ReadonlyArray<string>, b: ReadonlyArray<string>): boolean {
  if (a.length !== b.length) return true;
  return a.some((name, i) => name.trim() !== b[i].trim());
}

/** The inline warning shown when the run's classes differ from the base's (Req 6.2). */
export function classMismatchWarning(baseClasses: ReadonlyArray<string>): string {
  return `Class list differs from the base model (${baseClasses.join(', ')}); the detection head will be re-initialised — expect more epochs.`;
}

// ---------------------------------------------------------------------------
// Per-arch form defaults, validation, submit payload
// ---------------------------------------------------------------------------

/**
 * Detection Settings form state (strings: they are bound to `Input`s). One
 * shape for both arches; each arch reads its own subset.
 */
export interface DetectionFormParams {
  // shared
  epochs: string;
  batch: string;
  patience: string;
  scoreThreshold: string;
  // YOLO (train.py)
  imgsz: string;
  baseWeights: string;
  iouThreshold: string;
  // RF-DETR (train_rfdetr.py)
  rfdetrSize: RfDetrSize;
  resolution: string;
  gradAccum: string;
  lr: string;
}

/** Backend `DETECTION_DEFAULTS` (YOLO). */
export const YOLO_PARAM_DEFAULTS: DetectionFormParams = {
  epochs: '100',
  batch: '4',
  patience: '30',
  scoreThreshold: '0.25',
  imgsz: '1280',
  baseWeights: 'yolo11s.pt',
  iouThreshold: '0.45',
  rfdetrSize: 'small',
  resolution: String(RFDETR_SIZES.small),
  gradAccum: '4',
  lr: '0.0001',
};

/** Backend `RFDETR_DEFAULTS` (the documented T4 configuration, Req 3.5). */
export const RFDETR_PARAM_DEFAULTS: DetectionFormParams = {
  epochs: '100',
  batch: '4',
  patience: '10',
  scoreThreshold: '0.5',
  imgsz: '1280',
  baseWeights: 'yolo11s.pt',
  iouThreshold: '0.45',
  rfdetrSize: 'small',
  resolution: String(RFDETR_SIZES.small),
  gradAccum: '4',
  lr: '0.0001',
};

export function detectionParamDefaults(arch: DetectionArch): DetectionFormParams {
  return arch === 'rf_detr' ? { ...RFDETR_PARAM_DEFAULTS } : { ...YOLO_PARAM_DEFAULTS };
}

export const RFDETR_RESOLUTION_STEP = 32;
export const RFDETR_RESOLUTION_MIN = 224;
export const RFDETR_RESOLUTION_MAX = 1120;

/** Same rule (and wording) as the backend's `parse_detection_hyperparameters`. */
export const RFDETR_RESOLUTION_ERROR =
  `Resolution must be a multiple of ${RFDETR_RESOLUTION_STEP} between ${RFDETR_RESOLUTION_MIN} and ${RFDETR_RESOLUTION_MAX}`;

export function isValidRfDetrResolution(resolution: number): boolean {
  return Number.isInteger(resolution)
    && resolution >= RFDETR_RESOLUTION_MIN
    && resolution <= RFDETR_RESOLUTION_MAX
    && resolution % RFDETR_RESOLUTION_STEP === 0;
}

/**
 * Client-side mirror of the backend's hyperparameter bounds, one message per
 * violated field. The YOLO messages are the ones CreateTraining has always
 * shown; RF-DETR's follow the same style.
 */
export function validateDetectionParams(arch: DetectionArch, p: DetectionFormParams): string[] {
  const errors: string[] = [];
  const epochs = parseInt(p.epochs, 10);
  const batch = parseInt(p.batch, 10);
  const patience = parseInt(p.patience, 10);
  const score = parseFloat(p.scoreThreshold);
  if (arch === 'rf_detr') {
    if (!isRfDetrSize(p.rfdetrSize)) errors.push(`Size must be one of ${RFDETR_SIZE_ORDER.join(', ')}`);
    const resolution = Number(p.resolution);
    if (!isValidRfDetrResolution(resolution)) errors.push(RFDETR_RESOLUTION_ERROR);
  } else {
    const imgsz = parseInt(p.imgsz, 10);
    if (!(imgsz >= 320 && imgsz <= 2048 && imgsz % 32 === 0)) errors.push('Image size must be a multiple of 32 between 320 and 2048');
  }
  if (!(epochs >= 1 && epochs <= 1000)) errors.push('Epochs must be between 1 and 1000');
  if (!(batch >= 1 && batch <= 64)) errors.push('Batch size must be between 1 and 64');
  if (arch === 'rf_detr') {
    const gradAccum = parseInt(p.gradAccum, 10);
    const lr = parseFloat(p.lr);
    if (!(gradAccum >= 1 && gradAccum <= 64)) errors.push('Gradient accumulation must be between 1 and 64');
    if (!(lr > 0 && lr < 1)) errors.push('Learning rate must be strictly between 0 and 1');
  }
  if (!(patience >= 0 && patience <= 1000)) errors.push('Patience must be between 0 and 1000');
  if (!(score > 0 && score < 1)) errors.push('Score threshold must be strictly between 0 and 1');
  if (arch === 'yolo') {
    const iou = parseFloat(p.iouThreshold);
    if (!(iou > 0 && iou < 1)) errors.push('IoU threshold must be strictly between 0 and 1');
  }
  return errors;
}

/**
 * The `hyperparameters` body for the arch — YOLO exactly as before; RF-DETR
 * without `imgsz` or `iou_threshold` (Req 3.6: no IoU, NMS-free).
 */
export function buildDetectionHyperparameters(arch: DetectionArch, p: DetectionFormParams): DetectionHyperparameters {
  if (arch === 'rf_detr') {
    const rf: RfDetrHyperparameters = {
      rfdetr_size: p.rfdetrSize,
      resolution: parseInt(p.resolution, 10),
      epochs: parseInt(p.epochs, 10),
      batch: parseInt(p.batch, 10),
      grad_accum: parseInt(p.gradAccum, 10),
      lr: parseFloat(p.lr),
      patience: parseInt(p.patience, 10),
      score_threshold: parseFloat(p.scoreThreshold),
    };
    return rf;
  }
  const yolo: YoloHyperparameters = {
    imgsz: parseInt(p.imgsz, 10),
    epochs: parseInt(p.epochs, 10),
    batch: parseInt(p.batch, 10),
    base_weights: p.baseWeights,
    patience: parseInt(p.patience, 10),
    score_threshold: parseFloat(p.scoreThreshold),
    iou_threshold: parseFloat(p.iouThreshold),
  };
  return yolo;
}

/** The `published` base model the current form implies (YOLO: base weights; RF-DETR: size). */
export function publishedBaseModelRef(arch: DetectionArch, p: DetectionFormParams): BaseModelRef {
  return { kind: 'published', ref: arch === 'rf_detr' ? p.rfdetrSize : p.baseWeights };
}

/** The `POST /training` fields Detection Settings contributes (Req 3.2, 6.3). */
export interface DetectionSubmitFields {
  detection_arch: DetectionArch;
  base_model: BaseModelRef;
  hyperparameters: DetectionHyperparameters;
  class_names?: string[];
}

/**
 * Everything the submit adds for a detection run. A null/absent `baseModel`
 * means "the published checkpoint the form names". `classNames` is sent only
 * when the user (or a base-model prefill) supplied one; otherwise the backend
 * reads the manifest's class-map.
 */
export function buildDetectionSubmitFields(
  arch: DetectionArch,
  p: DetectionFormParams,
  baseModel: BaseModelRef | null | undefined,
  classNames?: ReadonlyArray<string> | null
): DetectionSubmitFields {
  const fields: DetectionSubmitFields = {
    detection_arch: arch,
    base_model: baseModel ?? publishedBaseModelRef(arch, p),
    hyperparameters: buildDetectionHyperparameters(arch, p),
  };
  if (classNames && classNames.length > 0) fields.class_names = [...classNames];
  return fields;
}

// ---------------------------------------------------------------------------
// Instance defaults (Req 3.5)
// ---------------------------------------------------------------------------

export const DETECTION_DEFAULT_INSTANCE_TYPE = 'ml.g4dn.xlarge';
export const RFDETR_LARGE_INSTANCE_TYPE = 'ml.g5.xlarge';

/**
 * `ml.g4dn.xlarge` (T4) for YOLO and RF-DETR nano/small; RF-DETR medium and
 * large are nudged to `ml.g5.xlarge` (A10G, 24 GB). The page applies this
 * only while the user has not picked an instance type themselves.
 */
export function defaultDetectionInstanceType(arch: DetectionArch, rfdetrSize?: RfDetrSize | string): string {
  if (arch === 'rf_detr' && (rfdetrSize === 'medium' || rfdetrSize === 'large')) {
    return RFDETR_LARGE_INSTANCE_TYPE;
  }
  return DETECTION_DEFAULT_INSTANCE_TYPE;
}

/**
 * Change the RF-DETR size. The resolution follows to the new size's native
 * value unless the user had moved it off the old size's native value.
 */
export function applyRfDetrSize(p: DetectionFormParams, size: RfDetrSize): DetectionFormParams {
  const wasNative = p.resolution === String(rfdetrNativeResolution(p.rfdetrSize));
  return {
    ...p,
    rfdetrSize: size,
    resolution: wasNative ? String(RFDETR_SIZES[size]) : p.resolution,
  };
}

/**
 * Form values a selected base record implies (Req 6.2): the network input it
 * was trained at and, for RF-DETR, its size (the checkpoint IS that size).
 */
export function paramsFromBaseRecord(
  arch: DetectionArch,
  p: DetectionFormParams,
  record: TrainingJobRecord | null | undefined
): DetectionFormParams {
  const det = record?.detection;
  if (!det) return p;
  const next = { ...p };
  const input = det.network_input_width;
  if (arch === 'rf_detr') {
    if (isRfDetrSize(det.rfdetr_size)) next.rfdetrSize = det.rfdetr_size;
    const resolution = det.resolution ?? input;
    if (typeof resolution === 'number' && isValidRfDetrResolution(resolution)) next.resolution = String(resolution);
  } else if (typeof input === 'number' && input > 0) {
    next.imgsz = String(input);
  }
  return next;
}
