/**
 * Training Detail for an RF-DETR-Seg record (rfdetr-seg-support task 12.3;
 * Requirements 12.3, 12.6; O15 a).
 *
 * - `modelTypeLabel` reads "Instance Segmentation · RF-DETR-Seg (ONNX) ·
 *   instance output" or "… · semantic output" from `detection.output_mode`
 *   (absent = instance); the detection labels are unchanged.
 * - The metrics container (Test mAP@50) and the Detection container render
 *   for an `instance_segmentation` job — with size, resolution, top-k, both
 *   thresholds, the Output and the mask mAP rows, and "Fine-tuned from" —
 *   and still render for an `object_detection` job (the RF-DETR rows, no
 *   mask rows, no YOLO rows).
 *
 * `apiService` and the Compilation tab (its own suites) are mocked; the
 * harness is `TrainingDetail.conversion.test.tsx`'s (fake timers, `act`).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import TrainingDetail, { detectionArchLabel, modelTypeLabel, outputModeLabel } from './TrainingDetail';

const { apiMocks } = vi.hoisted(() => ({
  apiMocks: {
    getTrainingJob: vi.fn(),
    getTrainingLogs: vi.fn(),
  },
}));

vi.mock('../services/api', () => {
  class ApiError extends Error {
    status: number;
    constructor(message: string, status = 0) {
      super(message);
      this.status = status;
    }
  }
  const apiService = new Proxy(apiMocks as Record<string, unknown>, {
    get(target, prop: string) {
      if (prop in target) return target[prop];
      return (..._args: unknown[]) => Promise.resolve({});
    },
  });
  return { apiService, ApiError };
});

vi.mock('../components/CompilationTab', () => ({
  default: () => <div data-testid="compilation-tab-stub" />,
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const TRAINING_ID = 'tj-seg';
const BASE_ID = 'tj-seg-base';

const SEG_DETECTION = {
  detection_arch: 'rf_detr_seg',
  rfdetr_seg_size: 'medium',
  resolution: 432,
  network_input_width: 432,
  network_input_height: 432,
  class_names: ['crack', 'chip'],
  num_classes: 2,
  score_threshold: 0.5,
  mask_threshold: 0.4,
  top_k: 200,
  preserve_aspect: false,
  epochs: 100,
  batch: 4,
  grad_accum: 4,
  lr: 0.0001,
  patience: 10,
  onnx_opset: 17,
  output_mode: 'instance',
  base_model: {
    kind: 'training_job',
    ref: BASE_ID,
    weights_s3: 's3://example-bucket/models/training/tj-seg-base/output/model.tar.gz',
    member: 'checkpoint_best_total.pth',
    detection_arch: 'rf_detr_seg',
    class_names: ['crack', 'chip'],
  },
};

const SEG_JOB = {
  training_id: TRAINING_ID,
  usecase_id: 'uc-1',
  model_name: 'crack-masks',
  model_version: '1.0.0',
  model_type: 'instance_segmentation',
  runtime: 'onnx',
  status: 'Completed',
  progress: 100,
  training_job_name: 'crack-masks-20261123',
  instance_type: 'ml.g4dn.xlarge',
  algorithm_uri: '763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-training:2.5.1-gpu-py311-cu124-ubuntu22.04-sagemaker',
  dataset_manifest_s3: 's3://example-bucket/labels/crack/output.manifest',
  created_by: 'scientist@example.com',
  created_at: 1_790_000_000_000,
  completed_at: 1_790_003_600_000,
  artifact_s3: 's3://example-bucket/models/training/crack-masks-20261123/output/model.tar.gz',
  hyperparameters: { rfdetr_seg_size: 'medium', resolution: 432, epochs: 100, mask_threshold: 0.4 },
  metrics: { 'test:mAP50': 0.912, 'test:mAP50-95': 0.71, 'test:mask-mAP50': 0.874, 'test:mask-mAP50-95': 0.655 },
  detection: SEG_DETECTION,
  packaged_output_mode: 'instance',
};

const BASE_JOB = { ...SEG_JOB, training_id: BASE_ID, model_name: 'crack-masks-base', model_version: '0.9.0', detection: { ...SEG_DETECTION, base_model: undefined } };

const RF_JOB = {
  ...SEG_JOB,
  training_id: 'tj-rf',
  model_name: 'blue-plate',
  model_type: 'object_detection',
  metrics: { 'test:mAP50': 0.931 },
  detection: {
    detection_arch: 'rf_detr', rfdetr_size: 'small', resolution: 512, network_input_width: 512, network_input_height: 512,
    class_names: ['blue_plate'], num_classes: 1, score_threshold: 0.5, top_k: 300, preserve_aspect: false,
  },
  packaged_output_mode: undefined,
};

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.useFakeTimers();
  apiMocks.getTrainingJob.mockReset();
  apiMocks.getTrainingLogs.mockReset();
  apiMocks.getTrainingLogs.mockResolvedValue({ logs: [] });
});

afterEach(() => {
  vi.useRealTimers();
});

async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

async function renderRecord(job: Record<string, unknown>, base?: Record<string, unknown>) {
  apiMocks.getTrainingJob.mockImplementation(async (id: string) => {
    if (base && id === base.training_id) return base;
    return job;
  });
  const view = render(
    <MemoryRouter initialEntries={[`/training/${job.training_id}`]}>
      <Routes>
        <Route path="/training/:trainingId" element={<TrainingDetail />} />
      </Routes>
    </MemoryRouter>
  );
  await flush();
  await flush();
  return view;
}

/** The value of the KeyValuePairs item labelled exactly `label`, or null. */
function kvValue(container: HTMLElement, label: string): string | null {
  for (const pairs of createWrapper(container).findAllKeyValuePairs()) {
    for (const item of pairs.findItems()) {
      if (item.findLabel()?.getElement().textContent?.trim() === label) {
        return item.findValue()?.getElement().textContent ?? null;
      }
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Labels (pure)
// ---------------------------------------------------------------------------

describe('Seg labels', () => {
  it('names the family and the output mode', () => {
    expect(detectionArchLabel({ ...SEG_DETECTION } as never)).toBe('RF-DETR-Seg');
    expect(detectionArchLabel({ detection_arch: 'rf_detr' } as never)).toBe('RF-DETR');
    expect(detectionArchLabel({ detection_arch: 'yolo' } as never)).toBe('YOLO');
    expect(detectionArchLabel(undefined)).toBe('YOLO');
    expect(outputModeLabel({ ...SEG_DETECTION, output_mode: 'semantic' } as never)).toBe('semantic output');
    expect(outputModeLabel({ ...SEG_DETECTION } as never)).toBe('instance output');
    expect(outputModeLabel({ ...SEG_DETECTION, output_mode: undefined } as never)).toBe('instance output');
  });

  it('labels a Seg record by type, family, runtime and output mode; the detection labels are unchanged', () => {
    expect(modelTypeLabel('instance_segmentation', SEG_DETECTION as never)).toBe('Instance Segmentation · RF-DETR-Seg (ONNX) · instance output');
    expect(modelTypeLabel('instance_segmentation', { ...SEG_DETECTION, output_mode: 'semantic' } as never)).toBe('Instance Segmentation · RF-DETR-Seg (ONNX) · semantic output');
    expect(modelTypeLabel('object_detection', RF_JOB.detection as never)).toBe('Object Detection · RF-DETR (ONNX)');
    expect(modelTypeLabel('object_detection', { detection_arch: 'yolo' } as never)).toBe('Object Detection · YOLO (ONNX)');
    expect(modelTypeLabel('classification', undefined)).toBe('classification');
  });
});

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

describe('TrainingDetail — RF-DETR-Seg record', () => {
  it('renders the metrics and Detection containers with the Seg rows and the instance-output label', async () => {
    const { container } = await renderRecord(SEG_JOB, BASE_JOB);
    expect(screen.getByText('Test mAP@50')).toBeInTheDocument();
    expect(screen.getByText('91.2%')).toBeInTheDocument();
    expect(screen.queryByText('Validation Accuracy')).toBeNull();
    expect(kvValue(container, 'Model Type')).toBe('Instance Segmentation · RF-DETR-Seg (ONNX) · instance output');
    expect(screen.getByText('Detection')).toBeInTheDocument();
    expect(kvValue(container, 'Architecture')).toBe('RF-DETR-Seg');
    expect(kvValue(container, 'Size')).toBe('medium');
    expect(kvValue(container, 'Resolution')).toBe('432');
    expect(kvValue(container, 'Top-k')).toBe('200');
    expect(kvValue(container, 'Mask threshold')).toBe('0.4');
    expect(kvValue(container, 'Output')).toBe('instance output');
    expect(kvValue(container, 'Score threshold')).toBe('0.5');
    expect(kvValue(container, 'Test mask mAP@50')).toBe('87.4%');
    expect(kvValue(container, 'Test mask mAP@50-95')).toBe('65.5%');
    expect(kvValue(container, 'Preserve aspect (letterbox)')).toBe('No (square resize)');
    expect(kvValue(container, 'Classes')).toBe('crack, chip');
    // No YOLO rows for a Seg record.
    expect(kvValue(container, 'Network input (imgsz)')).toBeNull();
    expect(kvValue(container, 'Base weights')).toBeNull();
    expect(kvValue(container, 'IoU threshold')).toBeNull();
    // Fine-tuned from the base record, resolved by name and version.
    expect(kvValue(container, 'Fine-tuned from')).toContain('crack-masks-base v0.9.0');
  });

  it('labels a semantic-output record "… · semantic output"', async () => {
    const { container } = await renderRecord({
      ...SEG_JOB,
      detection: { ...SEG_DETECTION, output_mode: 'semantic', base_model: undefined },
    });
    expect(kvValue(container, 'Model Type')).toBe('Instance Segmentation · RF-DETR-Seg (ONNX) · semantic output');
    expect(kvValue(container, 'Output')).toBe('semantic output');
    expect(kvValue(container, 'Fine-tuned from')).toBeNull();
  });

  it('still renders both containers for an object_detection RF-DETR job, without the Seg rows', async () => {
    const { container } = await renderRecord(RF_JOB);
    expect(screen.getByText('Test mAP@50')).toBeInTheDocument();
    expect(screen.getByText('93.1%')).toBeInTheDocument();
    expect(kvValue(container, 'Model Type')).toBe('Object Detection · RF-DETR (ONNX)');
    expect(screen.getByText('Detection')).toBeInTheDocument();
    expect(kvValue(container, 'Architecture')).toBe('RF-DETR');
    expect(kvValue(container, 'Size')).toBe('small');
    expect(kvValue(container, 'Resolution')).toBe('512');
    expect(kvValue(container, 'Top-k')).toBe('300');
    expect(kvValue(container, 'Mask threshold')).toBeNull();
    expect(kvValue(container, 'Output')).toBeNull();
    expect(kvValue(container, 'Test mask mAP@50')).toBeNull();
    expect(kvValue(container, 'IoU threshold')).toBeNull();
  });

  it('keeps the LFV rendering for a classification job (no Detection container)', async () => {
    await renderRecord({ ...RF_JOB, training_id: 'tj-lfv', model_type: 'classification', runtime: undefined, detection: undefined, metrics: { 'validation:accuracy': 0.98 } });
    expect(screen.getByText('Validation Accuracy')).toBeInTheDocument();
    expect(screen.queryByText('Test mAP@50')).toBeNull();
    expect(screen.queryByText('Detection')).toBeNull();
  });
});
