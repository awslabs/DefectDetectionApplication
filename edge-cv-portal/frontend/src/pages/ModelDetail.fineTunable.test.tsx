/**
 * Model Detail "Fine-tuning" section for imported records
 * (rfdetr-training-and-transfer-learning task 7.4c; Req 7.2, 7.6, 8.2).
 *
 * The page loads the model (`getModel`) and, for imported records, the
 * underlying training-job record (`getTrainingJob`), whose `metadata`
 * carries what Smart Import decided:
 * - `metadata.fine_tunable` set -> a "Fine-tunable (YOLO|RF-DETR)" badge with
 *   the checkpoint's class names, or the class count when the file stores no
 *   names (published RF-DETR COCO checkpoints);
 * - `metadata.fine_tunable` null/absent -> the per-kind explanation from
 *   `docs/transfer-learning-spike.md` §4.5 (ONNX / TorchScript / state_dict),
 *   keyed by the record's `framework` / artifact file name / `runtime`.
 * - trained (non-imported) records never render the section.
 *
 * The page is rendered inside a real `MemoryRouter`; `apiService`, the auth
 * context and the CompilationTab (unrelated to this section, network-heavy)
 * are mocked.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import ModelDetail from './ModelDetail';
import type { TrainingJob } from '../types';
import {
  NOT_FINE_TUNABLE_EXPLANATIONS,
  fineTunableClassSummary,
  notFineTunableKind,
} from '../utils/importedFineTunable';

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

// The Compilation / Component Actions tab is exercised by its own suites.
vi.mock('../components/CompilationTab', () => ({
  default: () => <div data-testid="compilation-tab-stub" />,
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const MODEL_ID = 'imp-1';

const modelResponse = (over: Record<string, unknown> = {}) => ({
  model: {
    model_id: MODEL_ID,
    usecase_id: 'uc-1',
    name: 'brought-in',
    version: '1.0.0',
    stage: 'candidate',
    source: 'imported',
    training_job_id: MODEL_ID,
    model_type: 'object_detection',
    metrics: {},
    component_arns: {},
    deployed_devices: [],
    created_by: 'me',
    created_at: 1,
    updated_at: 1,
    ...over,
  },
});

const importedJob = (over: Partial<TrainingJob>): TrainingJob =>
  ({
    training_id: MODEL_ID,
    usecase_id: 'uc-1',
    model_name: 'brought-in',
    model_version: '1.0.0',
    dataset_manifest_s3: '',
    algorithm_uri: '',
    hyperparameters: {},
    instance_type: '',
    training_job_arn: '',
    status: 'Completed',
    metrics: {},
    artifact_s3: 's3://models-bucket/converted-models/brought-in-abc123/model.tar.gz',
    created_by: 'me',
    created_at: 1,
    model_type: 'object_detection',
    source: 'imported',
    ...over,
  }) as TrainingJob;

function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/models/${MODEL_ID}`]}>
      <Routes>
        <Route path="/models/:modelId" element={<ModelDetail />} />
        <Route path="/models" element={<div>models-list-page</div>} />
      </Routes>
    </MemoryRouter>
  );
}

async function renderWith(job: TrainingJob | null, model = modelResponse()) {
  apiMocks.getModel.mockResolvedValue(model);
  if (job) apiMocks.getTrainingJob.mockResolvedValue(job);
  else apiMocks.getTrainingJob.mockRejectedValue(new Error('no job'));
  const utils = renderPage();
  await waitFor(() => expect(screen.queryByText('Loading model...')).not.toBeInTheDocument());
  return utils;
}

beforeEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Badge (Req 7.6)
// ---------------------------------------------------------------------------

describe('ModelDetail fine-tunable badge', () => {
  it('shows "Fine-tunable (YOLO)" with the checkpoint class names', async () => {
    await renderWith(importedJob({
      runtime: 'onnx',
      metadata: {
        framework: 'PYTORCH',
        pt_file: 'model.pt',
        fine_tunable: {
          arch: 'yolo',
          kind: 'ultralytics_checkpoint',
          checkpoint_s3: 's3://models-bucket/converted-models/brought-in-abc123/checkpoint.pt',
          class_names: ['blue_plate', 'scratch'],
          num_classes: 2,
        },
      },
    }));

    expect(await screen.findByText('Fine-tunable (YOLO)')).toBeInTheDocument();
    expect(screen.getByText('Classes: blue_plate, scratch')).toBeInTheDocument();
    expect(
      screen.getByText('s3://models-bucket/converted-models/brought-in-abc123/checkpoint.pt')
    ).toBeInTheDocument();
    // No explanation alongside a badge.
    for (const text of Object.values(NOT_FINE_TUNABLE_EXPLANATIONS)) {
      expect(screen.queryByText(text)).not.toBeInTheDocument();
    }
  });

  it('shows "Fine-tunable (RF-DETR)" with only the class count when names are absent', async () => {
    // Published RF-DETR COCO checkpoints store no class names (spike §2.5).
    await renderWith(importedJob({
      metadata: {
        framework: 'PYTORCH',
        pt_file: 'model.pt',
        fine_tunable: {
          arch: 'rf_detr',
          kind: 'rfdetr_checkpoint',
          checkpoint_s3: 's3://models-bucket/converted-models/brought-in-abc123/checkpoint.pth',
          class_names: null,
          num_classes: 90,
        },
      },
    }));

    expect(await screen.findByText('Fine-tunable (RF-DETR)')).toBeInTheDocument();
    expect(screen.getByText('Classes: 90 classes')).toBeInTheDocument();
    expect(screen.queryByText(/Classes: .*,/)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Per-kind explanations (Req 7.2)
// ---------------------------------------------------------------------------

describe('ModelDetail not-fine-tunable explanations', () => {
  it('ONNX import: Smart-Import its training checkpoint', async () => {
    await renderWith(importedJob({
      runtime: 'onnx',
      metadata: { framework: 'ONNX', pt_file: 'model.onnx', model_file: 'model.onnx', fine_tunable: null },
    }));

    expect(await screen.findByText(NOT_FINE_TUNABLE_EXPLANATIONS.onnx)).toBeInTheDocument();
    expect(screen.getByText(NOT_FINE_TUNABLE_EXPLANATIONS.onnx).textContent).toContain(
      'Smart-Import its training checkpoint (ultralytics .pt or RF-DETR .pth)'
    );
    expect(screen.queryByText(/Fine-tunable \(/)).not.toBeInTheDocument();
  });

  it('TorchScript import: frozen graph', async () => {
    await renderWith(importedJob({
      metadata: { framework: 'PYTORCH', pt_file: 'mochi.pt', model_file: 'mochi.pt', fine_tunable: null },
    }));

    expect(await screen.findByText(NOT_FINE_TUNABLE_EXPLANATIONS.torchscript)).toBeInTheDocument();
    expect(screen.queryByText(/Fine-tunable \(/)).not.toBeInTheDocument();
  });

  it('state_dict import: no model definition', async () => {
    await renderWith(importedJob({
      metadata: { framework: 'PYTORCH', pt_file: 'mochi.pth', model_file: 'mochi.pth', fine_tunable: null },
    }));

    expect(await screen.findByText(NOT_FINE_TUNABLE_EXPLANATIONS.state_dict)).toBeInTheDocument();
  });

  it('a record imported without any fine_tunable field is explained too (direct POST /models/import)', async () => {
    await renderWith(importedJob({
      metadata: { framework: 'ONNX', pt_file: 'model.onnx' },
    }));

    expect(await screen.findByText(NOT_FINE_TUNABLE_EXPLANATIONS.onnx)).toBeInTheDocument();
  });
});

describe('ModelDetail fine-tuning section scope', () => {
  it('is not rendered for trained (non-imported) models', async () => {
    await renderWith(
      importedJob({ source: undefined, metadata: undefined }),
      modelResponse({ source: 'trained' })
    );

    await waitFor(() => expect(screen.getByTestId('compilation-tab-stub')).toBeInTheDocument());
    expect(screen.queryByText('Fine-tuning')).not.toBeInTheDocument();
    expect(screen.queryByText(/Fine-tunable \(/)).not.toBeInTheDocument();
    for (const text of Object.values(NOT_FINE_TUNABLE_EXPLANATIONS)) {
      expect(screen.queryByText(text)).not.toBeInTheDocument();
    }
  });
});

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe('importedFineTunable helpers', () => {
  it('kind detection prefers the artifact extension, then framework, then runtime', () => {
    expect(notFineTunableKind({ metadata: { pt_file: 'model.onnx' } })).toBe('onnx');
    expect(notFineTunableKind({ metadata: { framework: 'ONNX', pt_file: 'model.pt' } })).toBe('onnx');
    expect(notFineTunableKind({ runtime: 'onnx', metadata: {} })).toBe('onnx');
    expect(notFineTunableKind({ metadata: { framework: 'PYTORCH', pt_file: 'mochi.pth' } })).toBe('state_dict');
    expect(notFineTunableKind({ metadata: { framework: 'PYTORCH', pt_file: 'mochi.pt' } })).toBe('torchscript');
    expect(notFineTunableKind({ metadata: {} })).toBe('torchscript');
    expect(notFineTunableKind(null)).toBe('torchscript');
  });

  it('class summary: names joined, else a count, else null', () => {
    const base = { arch: 'yolo', checkpoint_s3: 's3://b/k.pt' };
    expect(fineTunableClassSummary({ ...base, class_names: ['a', 'b'], num_classes: 2 })).toBe('a, b');
    expect(fineTunableClassSummary({ ...base, class_names: null, num_classes: 90 })).toBe('90 classes');
    expect(fineTunableClassSummary({ ...base, class_names: [], num_classes: 1 })).toBe('1 class');
    expect(fineTunableClassSummary({ ...base, class_names: null, num_classes: null })).toBeNull();
  });
});
