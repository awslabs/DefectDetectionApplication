/**
 * Training Detail for a detector Conversion_Record
 * (detector-checkpoint-import task 8.3; Requirements 11.1-11.3).
 *
 * - The record is labelled by its conversion state: "Converting to ONNX",
 *   "Validating and packaging", "Completed" with its packaged components,
 *   and "Conversion failed" under an alert headed "Conversion failed" (not
 *   "Training Job Failed").
 * - The polling fix, proven with fake timers: the page refetches every 15 s
 *   while the record is InProgress (the interval used to read the mount-time
 *   `job`, null, and never refetched), and stops once it is terminal.
 * - Import Metadata shows the source checkpoint, the exporter, and after
 *   completion the ONNX sha256, opset, IR version, shapes and parity maxima.
 *
 * `apiService` and the Compilation tab (its own suites) are mocked.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import TrainingDetail, { RECORD_POLL_INTERVAL_MS, parityMaximaText, shouldPollRecord } from './TrainingDetail';

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
// Fixtures: a Conversion_Record through its lifecycle (design "Data model")
// ---------------------------------------------------------------------------

const TRAINING_ID = 'cnv-1';
const JOB_NAME = 'ppe_detection-cnv-20261001120000';
const EXPORT_IMAGE =
  '164152369890.dkr.ecr.us-east-1.amazonaws.com/dda-detector-export@sha256:0814885ea1ee171e06f1231f4ed9fba1aa740557b1bf0d0946df6d8c1a3bcdab';
const SOURCE_SHA = 'a00b6fce' + '0'.repeat(52) + '2119';
const ONNX_SHA = '5f2c' + '1'.repeat(60);
const TARGETS = ['jetson-xavier-jp5', 'jetson-xavier-jp6', 'jetson-xavier-jp7', 'x86_64-cpu'];

const IN_PROGRESS = {
  training_id: TRAINING_ID,
  usecase_id: 'uc-1',
  model_name: 'ppe-detection',
  model_version: '1.0.0',
  model_type: 'object_detection',
  source: 'imported',
  runtime: 'onnx',
  status: 'InProgress',
  progress: 10,
  training_job_name: JOB_NAME,
  training_job_arn: `arn:aws:sagemaker:us-east-1:164152369890:training-job/${JOB_NAME}`,
  instance_type: 'ml.m5.xlarge',
  algorithm_uri: EXPORT_IMAGE,
  created_by: 'scientist@example.com',
  created_at: 1_790_000_000_000,
  detection: {
    detection_arch: 'yolo',
    network_input_width: 640,
    network_input_height: 640,
    class_names: ['helmet', 'human', 'no-helmet', 'vest'],
    num_classes: 4,
    score_threshold: 0.25,
    iou_threshold: 0.45,
    preserve_aspect: true,
    onnx_opset: 17,
    imgsz: 640,
  },
  metadata: {
    framework: 'PYTORCH',
    framework_version: 'ultralytics 8.4.2',
    model_file: 'checkpoint.pt',
    pt_file: 'checkpoint.pt',
    model_type: 'object_detection',
    image_width: 640,
    image_height: 640,
    input_shape: [1, 3, 640, 640],
    fine_tunable: {
      arch: 'yolo',
      kind: 'ultralytics_checkpoint',
      checkpoint_s3: 's3://ryvan-cookies/converted-models/ppe_detection-1a2b3c4d/checkpoint.pt',
      class_names: ['helmet', 'human', 'no-helmet', 'vest'],
      num_classes: 4,
    },
  },
  conversion: {
    status: 'InProgress',
    job_name: JOB_NAME,
    export_image: EXPORT_IMAGE,
    source_s3: 's3://ryvan-cookies/model-uploads/5b0c7a51/best.pt',
    source_sha256: SOURCE_SHA,
    source_bytes: 5_475_290,
    source_framework: 'ultralytics',
    source_framework_version: '8.4.2',
    started_at: 1_790_000_000_000,
  },
};

const FINALIZING = {
  ...IN_PROGRESS,
  progress: 80,
  artifact_s3: `s3://ryvan-cookies/models/conversion/${JOB_NAME}/${JOB_NAME}/output/model.tar.gz`,
  conversion: { ...IN_PROGRESS.conversion, status: 'Finalizing', finalizing_at: 1_790_000_200_000 },
};

const COMPLETED = {
  ...FINALIZING,
  status: 'Completed',
  progress: 100,
  packaged_components: TARGETS.map(target => ({
    target,
    component_package_s3: 's3://dda-component-bucket/ppe-detection/component.zip',
    status: 'packaged',
  })),
  published_components: [
    {
      target: 'jetson-xavier-jp7',
      component_name: 'model-ppe-detection-jetson-xavier-jp7',
      component_version: '1.0.0',
      status: 'published',
    },
  ],
  conversion: {
    ...FINALIZING.conversion,
    status: 'Completed',
    completed_at: 1_790_000_300_000,
    onnx_sha256: ONNX_SHA,
    onnx_summary: {
      ir_version: 8,
      opset: 17,
      input: [1, 3, 640, 640],
      outputs: [[1, 8, 8400]],
      output_names: ['output0'],
      anchors: 8400,
      exporter: 'ultralytics 8.4.162',
      fleet_floor_onnxruntime: '1.16.3',
      parity_max_abs: { box_max_abs: 0.0063, score_max_abs: 3.2e-6 },
      parity_runtimes: ['onnxruntime 1.16.3', 'onnxruntime 1.23.2'],
      onnx_bytes: 12_345_678,
    },
  },
};

const FAILED_REASON = 'AlgorithmError: FATAL: segmentation heads are not converted, exit code: 1';
const FAILED = {
  ...IN_PROGRESS,
  status: 'Failed',
  progress: 0,
  failure_reason: FAILED_REASON,
  conversion: { ...IN_PROGRESS.conversion, status: 'Failed', failed_at: 1_790_000_100_000 },
};

// ---------------------------------------------------------------------------
// Harness (fake timers: flush with act + advanceTimersByTimeAsync, not waitFor)
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.useFakeTimers();
  // Reset (not just clear): queued mockResolvedValueOnce values must not
  // leak from one test into the next.
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

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function renderRecord() {
  const view = render(
    <MemoryRouter initialEntries={[`/training/${TRAINING_ID}`]}>
      <Routes>
        <Route path="/training/:trainingId" element={<TrainingDetail />} />
      </Routes>
    </MemoryRouter>
  );
  await flush();
  return view;
}

const statusText = () => screen.getByTestId('record-status').textContent;

/** The value of the KeyValuePairs item labelled exactly `label`. */
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
// Labels + polling (Req 11.1, 11.2)
// ---------------------------------------------------------------------------

describe('TrainingDetail — Conversion_Record lifecycle', () => {
  it('refetches every 15 s while InProgress, relabels each state, and stops once Completed', async () => {
    apiMocks.getTrainingJob
      .mockResolvedValueOnce(IN_PROGRESS)
      .mockResolvedValueOnce(FINALIZING)
      .mockResolvedValue(COMPLETED);
    const { container } = await renderRecord();

    expect(apiMocks.getTrainingJob).toHaveBeenCalledTimes(1);
    expect(apiMocks.getTrainingJob).toHaveBeenCalledWith(TRAINING_ID);
    expect(statusText()).toBe('Converting to ONNX');
    expect(screen.getByText(/converted in a network-isolated SageMaker job/)).toBeInTheDocument();
    expect(kvValue(container, 'Source')).toBe('Imported checkpoint (converted to ONNX)');

    // Not yet: the poll interval is 15 s.
    await advance(RECORD_POLL_INTERVAL_MS - 1);
    expect(apiMocks.getTrainingJob).toHaveBeenCalledTimes(1);

    await advance(1);
    expect(apiMocks.getTrainingJob).toHaveBeenCalledTimes(2);
    expect(statusText()).toBe('Validating and packaging');
    // A background refresh never swaps the page for the loading state.
    expect(screen.queryByText('Loading training job details...')).not.toBeInTheDocument();

    await advance(RECORD_POLL_INTERVAL_MS);
    expect(apiMocks.getTrainingJob).toHaveBeenCalledTimes(3);
    expect(statusText()).toBe('Completed');
    expect(screen.getByTestId('conversion-packaged-components').textContent).toBe(
      `Packaged for: ${TARGETS.join(', ')}`
    );
    expect(screen.getByTestId('conversion-published-components').textContent).toBe(
      'Published: model-ppe-detection-jetson-xavier-jp7 v1.0.0'
    );

    // Terminal: no more refetches.
    await advance(RECORD_POLL_INTERVAL_MS * 3);
    expect(apiMocks.getTrainingJob).toHaveBeenCalledTimes(3);
  });

  it('heads a failure "Conversion failed" with the reason, and stops polling', async () => {
    apiMocks.getTrainingJob.mockResolvedValueOnce(IN_PROGRESS).mockResolvedValue(FAILED);
    const { container } = await renderRecord();

    await advance(RECORD_POLL_INTERVAL_MS);
    expect(statusText()).toBe('Conversion failed');
    const alert = createWrapper(container)
      .findAllAlerts()
      .find(a => a.findHeader()?.getElement().textContent === 'Conversion failed');
    expect(alert).toBeDefined();
    expect(alert!.findContent().getElement().textContent).toBe(FAILED_REASON);
    expect(screen.queryByText('Training Job Failed')).not.toBeInTheDocument();

    await advance(RECORD_POLL_INTERVAL_MS * 2);
    expect(apiMocks.getTrainingJob).toHaveBeenCalledTimes(2);
  });

  it('keeps the last good record on screen when a refresh fails, and keeps polling', async () => {
    apiMocks.getTrainingJob
      .mockResolvedValueOnce(IN_PROGRESS)
      .mockRejectedValueOnce(new Error('Network error'))
      .mockResolvedValue(FINALIZING);
    await renderRecord();

    await advance(RECORD_POLL_INTERVAL_MS);
    expect(apiMocks.getTrainingJob).toHaveBeenCalledTimes(2);
    expect(statusText()).toBe('Converting to ONNX');
    expect(screen.queryByText('Error loading training job')).not.toBeInTheDocument();

    await advance(RECORD_POLL_INTERVAL_MS);
    expect(apiMocks.getTrainingJob).toHaveBeenCalledTimes(3);
    expect(statusText()).toBe('Validating and packaging');
  });

  it('also refetches an ordinary InProgress training job (the stale-closure fix is general)', async () => {
    const training = {
      training_id: 'tj-1',
      usecase_id: 'uc-1',
      model_name: 'blue-plate',
      model_version: '1.0.0',
      status: 'InProgress',
      progress: 50,
      instance_type: 'ml.g4dn.xlarge',
      created_at: 1,
    };
    apiMocks.getTrainingJob.mockResolvedValueOnce(training).mockResolvedValue({ ...training, status: 'Completed' });
    await renderRecord();

    expect(statusText()).toBe('In Progress');
    await advance(RECORD_POLL_INTERVAL_MS);
    expect(apiMocks.getTrainingJob).toHaveBeenCalledTimes(2);
    expect(statusText()).toBe('Completed');
    expect(screen.queryByText('Converting to ONNX')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Import Metadata (Req 11.3)
// ---------------------------------------------------------------------------

describe('TrainingDetail — conversion Import Metadata', () => {
  it('shows the source checkpoint and the exporter while converting', async () => {
    apiMocks.getTrainingJob.mockResolvedValue(IN_PROGRESS);
    const { container } = await renderRecord();

    expect(kvValue(container, 'Checkpoint')).toBe('s3://ryvan-cookies/model-uploads/5b0c7a51/best.pt');
    expect(kvValue(container, 'Checkpoint SHA-256')).toBe(SOURCE_SHA);
    expect(kvValue(container, 'Checkpoint size')).toBe('5.2 MiB');
    expect(kvValue(container, 'Saved by')).toBe('ultralytics 8.4.2');
    expect(kvValue(container, 'Export image')).toBe(EXPORT_IMAGE);
    expect(kvValue(container, 'Conversion job')).toBe(JOB_NAME);
    expect(kvValue(container, 'Exporter')).toBe('Reported when the conversion completes');
    // The ONNX facts exist only after completion.
    expect(kvValue(container, 'ONNX SHA-256')).toBeNull();
  });

  it('adds the validated ONNX after completion', async () => {
    apiMocks.getTrainingJob.mockResolvedValue(COMPLETED);
    const { container } = await renderRecord();

    expect(kvValue(container, 'Exporter')).toBe('ultralytics 8.4.162');
    expect(kvValue(container, 'ONNX SHA-256')).toBe(ONNX_SHA);
    expect(kvValue(container, 'Opset')).toBe('17');
    expect(kvValue(container, 'IR version')).toBe('8');
    expect(kvValue(container, 'Input shape')).toBe('[1, 3, 640, 640]');
    expect(kvValue(container, 'Output shapes')).toBe('[1, 8, 8400]');
    expect(kvValue(container, 'Parity check (max abs difference)')).toBe('box 0.0063, score 3.2e-6');
    expect(kvValue(container, 'Parity checked on')).toBe('onnxruntime 1.16.3, onnxruntime 1.23.2');
    // The existing import fields are still there.
    expect(kvValue(container, 'Framework')).toBe('PYTORCH ultralytics 8.4.2');
    expect(kvValue(container, 'Model File')).toBe('checkpoint.pt');
  });

  it('shows no conversion fields for a plain imported model', async () => {
    const { conversion: _c, ...plain } = COMPLETED;
    apiMocks.getTrainingJob.mockResolvedValue({
      ...plain,
      metadata: { ...plain.metadata, framework: 'ONNX', framework_version: '1.17', pt_file: 'model.onnx' },
    });
    const { container } = await renderRecord();

    expect(statusText()).toBe('Completed');
    expect(kvValue(container, 'Source')).toBe('Imported Model (BYOM)');
    expect(kvValue(container, 'Checkpoint SHA-256')).toBeNull();
    expect(screen.queryByTestId('conversion-packaged-components')).not.toBeInTheDocument();
  });
});

describe('TrainingDetail helpers', () => {
  it('polls only records that are still moving', () => {
    expect(shouldPollRecord({ status: 'InProgress' })).toBe(true);
    expect(shouldPollRecord({ status: 'Pending' })).toBe(true);
    expect(shouldPollRecord({ status: 'Completed' })).toBe(false);
    expect(shouldPollRecord({ status: 'Failed' })).toBe(false);
    expect(shouldPollRecord(null)).toBe(false);
    expect(RECORD_POLL_INTERVAL_MS).toBeLessThanOrEqual(15000);
  });

  it('formats the parity maxima', () => {
    expect(parityMaximaText({ parity_max_abs: { box_max_abs: 0.0063, score_max_abs: 3.2e-6 } })).toBe(
      'box 0.0063, score 3.2e-6'
    );
    expect(parityMaximaText({ parity_max_abs: { box_max_abs: 0, score_max_abs: null } })).toBe('box 0, score —');
    expect(parityMaximaText({})).toBe('—');
    expect(parityMaximaText(undefined)).toBe('—');
  });
});
