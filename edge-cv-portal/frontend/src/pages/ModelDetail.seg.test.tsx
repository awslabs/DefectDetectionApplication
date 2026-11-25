/**
 * Model Detail "Fine-tuning" badge for an imported RF-DETR-Seg checkpoint
 * (rfdetr-seg-support task 12.3; Requirement 12.3).
 *
 * `FINE_TUNABLE_ARCH_LABELS.rf_detr_seg` is what `fineTunableBadgeLabel`
 * reads for the badge the page renders; without it the badge would read
 * "Fine-tunable (rf_detr_seg)". The YOLO / RF-DETR badges are pinned by
 * `ModelDetail.fineTunable.test.tsx` and are unchanged.
 *
 * Harness: `ModelDetail.fineTunable.test.tsx`'s (apiService, the auth context
 * and the CompilationTab mocked).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import ModelDetail from './ModelDetail';
import type { TrainingJob } from '../types';
import { FINE_TUNABLE_ARCH_LABELS, fineTunableBadgeLabel } from '../utils/importedFineTunable';

const { apiMocks } = vi.hoisted(() => ({
  apiMocks: {
    getModel: vi.fn(),
    getTrainingJob: vi.fn(),
  },
}));

vi.mock('../services/api', () => {
  class ApiError extends Error {
    status: number;
    details?: Record<string, unknown>;
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

const MODEL_ID = 'imp-seg';

const MODEL = {
  model: {
    model_id: MODEL_ID,
    usecase_id: 'uc-1',
    name: 'crack-masks-import',
    version: '1.0.0',
    stage: 'candidate',
    source: 'imported',
    training_job_id: MODEL_ID,
    model_type: 'instance_segmentation',
    metrics: {},
    component_arns: {},
    deployed_devices: [],
    created_by: 'me',
    created_at: 1,
    updated_at: 1,
  },
};

const SEG_IMPORT = {
  training_id: MODEL_ID,
  usecase_id: 'uc-1',
  model_name: 'crack-masks-import',
  model_version: '1.0.0',
  dataset_manifest_s3: '',
  algorithm_uri: '',
  hyperparameters: {},
  instance_type: '',
  training_job_arn: '',
  status: 'Completed',
  metrics: {},
  artifact_s3: 's3://models-bucket/converted-models/crack-masks-import-abc123/model.tar.gz',
  created_by: 'me',
  created_at: 1,
  model_type: 'instance_segmentation',
  source: 'imported',
  runtime: 'onnx',
  detection: {
    detection_arch: 'rf_detr_seg', network_input_width: 384, network_input_height: 384, class_names: ['crack'],
    num_classes: 1, score_threshold: 0.5, mask_threshold: 0.5, preserve_aspect: false, output_mode: 'instance',
  },
  metadata: {
    framework: 'PYTORCH',
    pt_file: 'checkpoint.pth',
    fine_tunable: {
      arch: 'rf_detr_seg',
      kind: 'rfdetr_checkpoint',
      checkpoint_s3: 's3://models-bucket/converted-models/crack-masks-import-abc123/checkpoint.pth',
      class_names: ['crack'],
      num_classes: 1,
    },
  },
} as unknown as TrainingJob;

beforeEach(() => {
  vi.clearAllMocks();
});

async function renderWith(job: TrainingJob) {
  apiMocks.getModel.mockResolvedValue(MODEL);
  apiMocks.getTrainingJob.mockResolvedValue(job);
  const utils = render(
    <MemoryRouter initialEntries={[`/models/${MODEL_ID}`]}>
      <Routes>
        <Route path="/models/:modelId" element={<ModelDetail />} />
        <Route path="/models" element={<div>models-list-page</div>} />
      </Routes>
    </MemoryRouter>
  );
  await waitFor(() => expect(screen.queryByText('Loading model...')).not.toBeInTheDocument());
  return utils;
}

describe('ModelDetail — RF-DETR-Seg fine-tunable badge', () => {
  it('maps rf_detr_seg to "RF-DETR-Seg" beside the two existing labels', () => {
    expect(FINE_TUNABLE_ARCH_LABELS).toEqual({ yolo: 'YOLO', rf_detr: 'RF-DETR', rf_detr_seg: 'RF-DETR-Seg' });
    expect(fineTunableBadgeLabel({ arch: 'rf_detr_seg', checkpoint_s3: 's3://b/k.pth' })).toBe('Fine-tunable (RF-DETR-Seg)');
    expect(fineTunableBadgeLabel({ arch: 'rf_detr', checkpoint_s3: 's3://b/k.pth' })).toBe('Fine-tunable (RF-DETR)');
  });

  it('shows "Fine-tunable (RF-DETR-Seg)" with the checkpoint class names for an imported Seg checkpoint', async () => {
    await renderWith(SEG_IMPORT);
    expect(await screen.findByText('Fine-tunable (RF-DETR-Seg)')).toBeInTheDocument();
    expect(screen.getByText('Classes: crack')).toBeInTheDocument();
    expect(screen.getByText('s3://models-bucket/converted-models/crack-masks-import-abc123/checkpoint.pth')).toBeInTheDocument();
    expect(screen.queryByText('Fine-tunable (rf_detr_seg)')).toBeNull();
  });
});
