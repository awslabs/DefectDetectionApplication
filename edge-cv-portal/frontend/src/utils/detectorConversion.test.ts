/**
 * Pure helpers behind the detector-checkpoint conversion UI
 * (detector-checkpoint-import task 8.1; Requirements 3.3, 4.3-4.5, 10.2,
 * 10.3, 11.1, 11.2), plus the presigned-PUT upload with progress.
 *
 * The assessment fixtures are the backend's `assess_checkpoint` output for
 * the spike's real checkpoints (docs/detector-checkpoint-import-spike.md).
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { CheckpointAssessment } from '../services/api';
import {
  CHECKPOINT_SIZE_CAP_BYTES,
  CONVERSION_GEOMETRY,
  checkpointFamilyLabel,
  checkpointLibraryLabel,
  conversionLocksFor,
  conversionStatusIndicatorType,
  conversionStatusLabel,
  conversionStatusOf,
  formatBytes,
  isCheckpointFileName,
  isConversionActive,
  isDetectorConversionRecord,
  networkInputProblem,
  putFileWithProgress,
  thresholdProblem,
  uploadFileProblem,
  validateClassNames,
} from './detectorConversion';

/** The PPE checkpoint (ultralytics 8.4.2, 4 classes, trained at 640). */
const PPE: CheckpointAssessment = {
  kind: 'ultralytics_checkpoint',
  arch: 'yolo',
  task: 'detect',
  model_class: 'ultralytics.nn.tasks.DetectionModel',
  head_classes: ['ultralytics.nn.modules.head.Detect'],
  num_classes: 4,
  class_names: ['helmet', 'human', 'no-helmet', 'vest'],
  train_input_size: 640,
  framework: 'ultralytics',
  framework_version: '8.4.2',
  rfdetr_size: null,
  convertible: true,
  reasons: [],
};

/** An RF-DETR small checkpoint trained in the portal (native 512). */
const RF_SMALL: CheckpointAssessment = {
  kind: 'rfdetr_checkpoint',
  arch: 'rf_detr',
  task: 'detect',
  model_class: 'RFDETRSmall',
  head_classes: [],
  num_classes: 1,
  class_names: ['blue_plate'],
  train_input_size: 512,
  framework: 'rfdetr',
  framework_version: null,
  rfdetr_size: 'small',
  convertible: true,
  reasons: [],
};

/** A YOLO segmentation checkpoint: recognised, not convertible. */
const YOLO_SEG: CheckpointAssessment = {
  ...PPE,
  model_class: 'ultralytics.nn.tasks.SegmentationModel',
  head_classes: ['ultralytics.nn.modules.head.Segment'],
  task: 'segment',
  num_classes: 80,
  class_names: null,
  convertible: false,
  reasons: [
    'this ultralytics checkpoint is a segmentation model (SegmentationModel); only object detection converts',
    "the checkpoint was trained for the 'segment' task; only detect converts",
  ],
};

const conversionRecord = (status: string) => ({
  training_id: 'cnv-1',
  source: 'imported',
  model_type: 'object_detection',
  runtime: 'onnx',
  status: status === 'Failed' ? 'Failed' : status === 'Completed' ? 'Completed' : 'InProgress',
  conversion: { status, job_name: 'ppe_detection-cnv-20261001120000' },
});

describe('isDetectorConversionRecord (mirrors the backend predicate)', () => {
  it('is true only for an imported ONNX detector carrying a conversion block', () => {
    expect(isDetectorConversionRecord(conversionRecord('InProgress'))).toBe(true);
    expect(isDetectorConversionRecord({ ...conversionRecord('Completed'), runtime: 'ONNX' })).toBe(true);
  });

  it('is false for a portal-trained detector, a plain ONNX import, and junk', () => {
    const { conversion: _c, ...plainImport } = conversionRecord('Completed');
    expect(isDetectorConversionRecord(plainImport)).toBe(false);
    expect(isDetectorConversionRecord({ ...conversionRecord('Completed'), source: undefined })).toBe(false);
    expect(isDetectorConversionRecord({ ...conversionRecord('Completed'), model_type: 'classification' })).toBe(false);
    expect(isDetectorConversionRecord({ ...conversionRecord('Completed'), runtime: undefined })).toBe(false);
    expect(isDetectorConversionRecord({ ...conversionRecord('Completed'), conversion: [] })).toBe(false);
    expect(isDetectorConversionRecord({ ...conversionRecord('Completed'), conversion: null })).toBe(false);
    expect(isDetectorConversionRecord(null)).toBe(false);
    expect(isDetectorConversionRecord(undefined)).toBe(false);
  });
});

describe('conversion status', () => {
  it('labels each state per Requirement 11.1', () => {
    expect(conversionStatusLabel('InProgress')).toBe('Converting to ONNX');
    expect(conversionStatusLabel('Finalizing')).toBe('Validating and packaging');
    expect(conversionStatusLabel('Completed')).toBe('Completed');
    expect(conversionStatusLabel('Failed')).toBe('Conversion failed');
  });

  it('shows unknown values as-is and never reads the object prototype', () => {
    expect(conversionStatusLabel('Weird')).toBe('Weird');
    expect(conversionStatusLabel('toString')).toBe('toString');
    expect(conversionStatusLabel(null)).toBe('Unknown');
    expect(conversionStatusIndicatorType('constructor')).toBe('info');
  });

  it('maps states to indicators and to "keep polling"', () => {
    expect(conversionStatusIndicatorType('InProgress')).toBe('in-progress');
    expect(conversionStatusIndicatorType('Finalizing')).toBe('loading');
    expect(conversionStatusIndicatorType('Completed')).toBe('success');
    expect(conversionStatusIndicatorType('Failed')).toBe('error');
    expect(isConversionActive('InProgress')).toBe(true);
    expect(isConversionActive('Finalizing')).toBe(true);
    expect(isConversionActive('Completed')).toBe(false);
    expect(isConversionActive('Failed')).toBe(false);
    expect(isConversionActive(undefined)).toBe(false);
  });

  it('reads conversion.status only from a Conversion_Record', () => {
    expect(conversionStatusOf(conversionRecord('Finalizing'))).toBe('Finalizing');
    expect(conversionStatusOf(conversionRecord('Bogus'))).toBeNull();
    expect(conversionStatusOf({ ...conversionRecord('Completed'), source: 'trained' })).toBeNull();
  });
});

describe('Checkpoint panel labels', () => {
  it('names the family, with the RF-DETR size when known', () => {
    expect(checkpointFamilyLabel(PPE)).toBe('Ultralytics YOLO');
    expect(checkpointFamilyLabel(RF_SMALL)).toBe('RF-DETR (small)');
    expect(checkpointFamilyLabel({ ...RF_SMALL, rfdetr_size: null })).toBe('RF-DETR');
    expect(checkpointFamilyLabel({ ...PPE, kind: 'torchscript' })).toBe('TorchScript graph');
    expect(checkpointFamilyLabel({ ...PPE, kind: 'hasOwnProperty' })).toBe('Unrecognised file');
    expect(checkpointFamilyLabel(null)).toBe('Unrecognised file');
  });

  it('names the saving library and version', () => {
    expect(checkpointLibraryLabel(PPE)).toBe('ultralytics 8.4.2');
    expect(checkpointLibraryLabel(RF_SMALL)).toBe('rfdetr (version not recorded)');
    expect(checkpointLibraryLabel({ ...PPE, framework: null })).toBeNull();
  });
});

describe('conversionLocksFor (Requirement 10.3)', () => {
  it('YOLO: locks type/output/arch, pre-fills names in index order, 640, letterbox, 0.25/0.45', () => {
    const locks = conversionLocksFor(PPE)!;
    expect(locks).toMatchObject({
      arch: 'yolo',
      modelType: 'object_detection',
      exportFormat: 'onnx',
      numClasses: 4,
      classNames: ['helmet', 'human', 'no-helmet', 'vest'],
      networkInput: 640,
      inputBounds: { min: 320, max: 2048, step: 32 },
      allowedInputs: null,
      rfdetrSize: null,
      scoreThreshold: 0.25,
      iouThreshold: 0.45,
      preserveAspect: true,
      geometry: CONVERSION_GEOMETRY.yolo.label,
    });
  });

  it('YOLO without a usable imgsz falls back to 640', () => {
    expect(conversionLocksFor({ ...PPE, train_input_size: null })!.networkInput).toBe(640);
    expect(conversionLocksFor({ ...PPE, train_input_size: 650 })!.networkInput).toBe(640);
    expect(conversionLocksFor({ ...PPE, train_input_size: 1280 })!.networkInput).toBe(1280);
  });

  it('RF-DETR: native resolution only, square resize, score 0.5 and no IoU', () => {
    const locks = conversionLocksFor(RF_SMALL)!;
    expect(locks).toMatchObject({
      arch: 'rf_detr',
      numClasses: 1,
      classNames: ['blue_plate'],
      networkInput: 512,
      allowedInputs: [512],
      rfdetrSize: 'small',
      scoreThreshold: 0.5,
      iouThreshold: null,
      preserveAspect: false,
      geometry: CONVERSION_GEOMETRY.rf_detr.label,
    });
  });

  it('RF-DETR of unknown size offers the four natives and leaves the choice to the user', () => {
    const locks = conversionLocksFor({ ...RF_SMALL, rfdetr_size: null, train_input_size: null })!;
    expect(locks.allowedInputs).toEqual([384, 512, 576, 704]);
    expect(locks.networkInput).toBeNull();
    expect(conversionLocksFor({ ...RF_SMALL, rfdetr_size: null, train_input_size: 576 })!.networkInput).toBe(576);
  });

  it('pre-fills blank names when the checkpoint stores none (the user must supply them)', () => {
    const locks = conversionLocksFor({ ...RF_SMALL, num_classes: 3, class_names: null })!;
    expect(locks.classNames).toEqual(['', '', '']);
  });

  it('is null for non-convertible or unusable assessments', () => {
    expect(conversionLocksFor(YOLO_SEG)).toBeNull();
    expect(conversionLocksFor({ ...PPE, arch: null })).toBeNull();
    expect(conversionLocksFor({ ...PPE, num_classes: null })).toBeNull();
    expect(conversionLocksFor({ ...PPE, num_classes: 0 })).toBeNull();
    expect(conversionLocksFor(null)).toBeNull();
  });
});

describe('validateClassNames (Requirement 4.4)', () => {
  it('accepts renamed classes of the same count', () => {
    expect(validateClassNames(['hard-hat', 'person', 'bare-head', 'hi-vis'], 4)).toBeNull();
  });

  it('rejects a changed count with the backend wording', () => {
    expect(validateClassNames(['a', 'b', 'c'], 4)).toBe(
      "Class names must list exactly 4 classes (the checkpoint's head); got 3. Names may be renamed but not added or removed"
    );
    expect(validateClassNames(null, 1)).toBe(
      "Class names must list exactly 1 class (the checkpoint's head); got 0. Names may be renamed but not added or removed"
    );
  });

  it('rejects a blank name, naming its index', () => {
    expect(validateClassNames(['helmet', '  ', 'no-helmet', 'vest'], 4)).toBe('Class 1 needs a name');
  });
});

describe('networkInputProblem and thresholdProblem', () => {
  const yolo = conversionLocksFor(PPE)!;
  const rf = conversionLocksFor(RF_SMALL)!;
  const rfUnknown = conversionLocksFor({ ...RF_SMALL, rfdetr_size: null, train_input_size: null })!;

  it('YOLO: a multiple of 32 in [320, 2048]', () => {
    expect(networkInputProblem(yolo, '640')).toBeNull();
    expect(networkInputProblem(yolo, 320)).toBeNull();
    expect(networkInputProblem(yolo, '2048')).toBeNull();
    expect(networkInputProblem(yolo, '650')).toBe(
      'The network input must be a multiple of 32 between 320 and 2048 for YOLO; got 650'
    );
    expect(networkInputProblem(yolo, '288')).toMatch(/between 320 and 2048/);
    expect(networkInputProblem(yolo, '')).toBe('Enter the network input size in pixels');
    expect(networkInputProblem(yolo, '64.5')).toBe('Enter the network input size in pixels');
  });

  it('RF-DETR: its native resolution only', () => {
    expect(networkInputProblem(rf, '512')).toBeNull();
    expect(networkInputProblem(rf, '640')).toBe('RF-DETR small converts only at its native 512px; got 640');
    expect(networkInputProblem(rfUnknown, '704')).toBeNull();
    expect(networkInputProblem(rfUnknown, '640')).toBe(
      'RF-DETR converts only at a native resolution (384, 512, 576, 704); got 640'
    );
  });

  it('thresholds are strictly between 0 and 1', () => {
    expect(thresholdProblem('Score threshold', '0.25')).toBeNull();
    expect(thresholdProblem('Score threshold', '0')).toBe('Score threshold must be strictly between 0 and 1');
    expect(thresholdProblem('IoU threshold', '1')).toBe('IoU threshold must be strictly between 0 and 1');
    expect(thresholdProblem('IoU threshold', '')).toBe('IoU threshold must be strictly between 0 and 1');
    expect(thresholdProblem('IoU threshold', 'abc')).toBe('IoU threshold must be strictly between 0 and 1');
  });
});

describe('upload checks (Requirement 3.3)', () => {
  it('accepts .pt, .pth and .onnx up to the cap', () => {
    expect(uploadFileProblem({ name: 'best.pt', size: 5_475_290 })).toBeNull();
    expect(uploadFileProblem({ name: 'CHECKPOINT.PTH', size: 1 })).toBeNull();
    expect(uploadFileProblem({ name: 'model.onnx', size: CHECKPOINT_SIZE_CAP_BYTES })).toBeNull();
  });

  it('rejects other extensions, empty files and files over the cap', () => {
    expect(uploadFileProblem({ name: 'weights.bin', size: 10 })).toBe(
      'The file must end in .pt, .pth, .onnx; got weights.bin'
    );
    expect(uploadFileProblem({ name: 'best.pt', size: 0 })).toBe('best.pt is empty');
    expect(uploadFileProblem({ name: 'huge.pt', size: CHECKPOINT_SIZE_CAP_BYTES + 1 })).toBe(
      'huge.pt is 512 MiB; the checkpoint size cap is 512 MiB'
    );
    expect(uploadFileProblem(null)).toBe('Choose a model file to upload');
  });

  it('recognises checkpoint names and formats sizes', () => {
    expect(isCheckpointFileName('best.PT')).toBe(true);
    expect(isCheckpointFileName('ckpt.pth')).toBe(true);
    expect(isCheckpointFileName('model.onnx')).toBe(false);
    expect(formatBytes(300)).toBe('300 B');
    expect(formatBytes(5_475_290)).toBe('5.2 MiB');
    expect(formatBytes(CHECKPOINT_SIZE_CAP_BYTES)).toBe('512 MiB');
    expect(formatBytes(undefined)).toBe('—');
  });
});

// ---------------------------------------------------------------------------
// putFileWithProgress, against a scripted XMLHttpRequest
// ---------------------------------------------------------------------------

class FakeXhr {
  static last: FakeXhr | null = null;
  method = '';
  url = '';
  body: unknown = null;
  headers: Record<string, string> = {};
  status = 0;
  responseText = '';
  upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = {
    onprogress: null,
  };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  constructor() {
    FakeXhr.last = this;
  }
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  send(body: unknown) {
    this.body = body;
  }
}

describe('putFileWithProgress', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    FakeXhr.last = null;
  });

  it('PUTs the file to the URL with no Content-Type and reports progress to 100', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
    const file = new File(['weights'], 'best.pt');
    const progress: number[] = [];
    const done = putFileWithProgress('https://bucket.example/put?sig', file, p => progress.push(p));
    const xhr = FakeXhr.last!;
    expect(xhr.method).toBe('PUT');
    expect(xhr.url).toBe('https://bucket.example/put?sig');
    expect(xhr.body).toBe(file);
    expect(xhr.headers).toEqual({});
    xhr.upload.onprogress!({ lengthComputable: true, loaded: 25, total: 100 });
    xhr.upload.onprogress!({ lengthComputable: false, loaded: 50, total: 0 });
    xhr.upload.onprogress!({ lengthComputable: true, loaded: 999, total: 1000 });
    xhr.status = 200;
    xhr.onload!();
    await expect(done).resolves.toBeUndefined();
    expect(progress).toEqual([25, 99, 100]);
  });

  it('rejects with the HTTP status and the S3 error code', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
    const done = putFileWithProgress('https://bucket.example/put', new Blob(['x']));
    const xhr = FakeXhr.last!;
    xhr.status = 403;
    xhr.responseText = '<?xml version="1.0"?><Error><Code>AccessDenied</Code><Message>no</Message></Error>';
    xhr.onload!();
    await expect(done).rejects.toThrow('Upload failed (HTTP 403 AccessDenied)');
  });

  it('rejects on a network or CORS failure', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
    const done = putFileWithProgress('https://bucket.example/put', new Blob(['x']));
    FakeXhr.last!.onerror!();
    await expect(done).rejects.toThrow(/could not reach the bucket/);
  });
});
