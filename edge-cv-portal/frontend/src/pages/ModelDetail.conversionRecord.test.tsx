/**
 * Model Detail for a detector Conversion_Record (detector-checkpoint-import
 * task 8.4, Requirement 11.4): ModelDetail.tsx is unchanged, and its
 * fine-tunable badge still renders, because a Conversion_Record carries the
 * same `metadata.fine_tunable` descriptor a PyTorch-path Smart Import writes
 * (the sidecar checkpoint), even though its runtime is ONNX.
 *
 * Harness as in ModelDetail.fineTunable.test.tsx: a real `MemoryRouter`,
 * with `apiService`, the auth context and the CompilationTab mocked.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import ModelDetail from './ModelDetail';
import { NOT_FINE_TUNABLE_EXPLANATIONS } from '../utils/importedFineTunable';
import { isDetectorConversionRecord } from '../utils/detectorConversion';

const { apiMocks } = vi.hoisted(() => ({
  apiMocks: {
    getModel: vi.fn(),
    getTrainingJob: vi.fn(),
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

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 'u-1', username: 'user', role: 'DataScientist' },
  }),
}));

vi.mock('../components/CompilationTab', () => ({
  default: () => <div data-testid="compilation-tab-stub" />,
}));

const TRAINING_ID = 'cnv-1';
const CHECKPOINT_S3 = 's3://ryvan-cookies/converted-models/ppe_detection-1a2b3c4d/checkpoint.pt';

/** A Completed Conversion_Record, as the finalize leaves it (design "Data model"). */
const CONVERSION_RECORD = {
  training_id: TRAINING_ID,
  usecase_id: 'uc-1',
  model_name: 'ppe-detection',
  model_version: '1.0.0',
  model_type: 'object_detection',
  source: 'imported',
  runtime: 'onnx',
  status: 'Completed',
  progress: 100,
  instance_type: 'ml.m5.xlarge',
  training_job_arn: 'arn:aws:sagemaker:us-east-1:164152369890:training-job/ppe_detection-cnv-20261001120000',
  artifact_s3: 's3://ryvan-cookies/models/conversion/ppe_detection-cnv-20261001120000/output/model.tar.gz',
  dataset_manifest_s3: '',
  algorithm_uri: '164152369890.dkr.ecr.us-east-1.amazonaws.com/dda-detector-export@sha256:0814',
  hyperparameters: {},
  metrics: {},
  created_by: 'scientist@example.com',
  created_at: 1,
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
  },
  metadata: {
    framework: 'PYTORCH',
    framework_version: 'ultralytics 8.4.2',
    model_file: 'checkpoint.pt',
    pt_file: 'checkpoint.pt',
    fine_tunable: {
      arch: 'yolo',
      kind: 'ultralytics_checkpoint',
      checkpoint_s3: CHECKPOINT_S3,
      class_names: ['helmet', 'human', 'no-helmet', 'vest'],
      num_classes: 4,
    },
  },
  conversion: { status: 'Completed', job_name: 'ppe_detection-cnv-20261001120000' },
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('ModelDetail — Conversion_Record', () => {
  it('still shows "Fine-tunable (YOLO)" with the checkpoint classes', async () => {
    expect(isDetectorConversionRecord(CONVERSION_RECORD)).toBe(true);
    apiMocks.getModel.mockResolvedValue({
      model: {
        model_id: `${TRAINING_ID}-1.0.0`,
        usecase_id: 'uc-1',
        name: 'model-ppe-detection',
        version: '1.0.0',
        stage: 'candidate',
        source: 'imported',
        training_job_id: TRAINING_ID,
        model_type: 'object_detection',
        metrics: {},
        component_arns: {},
        deployed_devices: [],
        created_by: 'me',
        created_at: 1,
        updated_at: 1,
      },
    });
    apiMocks.getTrainingJob.mockResolvedValue(CONVERSION_RECORD);

    render(
      <MemoryRouter initialEntries={[`/models/${TRAINING_ID}-1.0.0`]}>
        <Routes>
          <Route path="/models/:modelId" element={<ModelDetail />} />
        </Routes>
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.queryByText('Loading model...')).not.toBeInTheDocument());

    expect(await screen.findByText('Fine-tunable (YOLO)')).toBeInTheDocument();
    expect(apiMocks.getTrainingJob).toHaveBeenCalledWith(TRAINING_ID);
    expect(screen.getByText('Classes: helmet, human, no-helmet, vest')).toBeInTheDocument();
    expect(screen.getByText(CHECKPOINT_S3)).toBeInTheDocument();
    // An ONNX runtime does not turn it into the "ONNX graphs cannot be fine-tuned" case.
    for (const text of Object.values(NOT_FINE_TUNABLE_EXPLANATIONS)) {
      expect(screen.queryByText(text)).not.toBeInTheDocument();
    }
  });
});
