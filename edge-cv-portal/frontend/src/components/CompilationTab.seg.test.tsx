/**
 * CompilationTab's Output_Mode override for RF-DETR-Seg records
 * (rfdetr-seg-support task 12.3; Requirements 6.4, 12.3, 15.2; C31, C34).
 *
 * - A trained Seg record shows an "Output" Select above Package Models,
 *   defaulting to the record's `detection.output_mode`; Package sends
 *   `startPackaging(trainingId, undefined, undefined, { outputMode })`, with
 *   the chosen mode after a change; "Last packaged" shows
 *   `packaged_output_mode` when present.
 * - A detection record's call carries no fourth argument (byte-identical
 *   body); the select is absent.
 * - With `config.json` `rfDetrSegEnabled: false` the select is absent and
 *   the call is the plain one (the backend packages the stored mode).
 * - An imported ONNX Seg record (not a Conversion_Record) shows the
 *   fixed-at-import note and calls the plain `startPackaging(trainingId)`;
 *   a Seg Conversion_Record gets the select.
 *
 * `apiService` and `getConfig` are mocked (everything else in `config` is
 * real).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import CompilationTab from './CompilationTab';
import type { TrainingJob } from '../types';
import type { AppConfig } from '../config';

const { apiMocks, configState } = vi.hoisted(() => ({
  apiMocks: {
    getCompilationStatus: vi.fn(),
    startPackaging: vi.fn(),
  },
  configState: { current: {} as Partial<AppConfig> },
}));

vi.mock('../services/api', () => {
  class ApiError extends Error {
    constructor(
      message: string,
      public readonly status?: number,
      public readonly code?: string,
      public readonly details?: Record<string, unknown>
    ) {
      super(message);
      this.name = 'ApiError';
    }
  }
  const apiService = new Proxy(apiMocks as Record<string, unknown>, {
    get(target, prop: string) {
      if (prop in target) return target[prop];
      return (..._args: unknown[]) => Promise.resolve({});
    },
  });
  return { ApiError, apiService };
});

vi.mock('../config', async importOriginal => {
  const actual = await importOriginal<typeof import('../config')>();
  return { ...actual, getConfig: () => configState.current as AppConfig };
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const TRAINING_ID = 'training-seg-1';

const baseJob = {
  training_id: TRAINING_ID,
  usecase_id: 'usecase-1',
  model_name: 'crack-masks',
  model_version: '1.0.0',
  dataset_manifest_s3: 's3://bucket/labeled/output.manifest',
  algorithm_uri: '763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-training:2.5.1-gpu-py311-cu124-ubuntu22.04-sagemaker',
  hyperparameters: {},
  instance_type: 'ml.g4dn.xlarge',
  status: 'Completed',
  metrics: { 'test:mAP50': 0.9 },
  artifact_s3: 's3://bucket/models/training/crack-masks-x/output/model.tar.gz',
  created_by: 'tester',
  created_at: 1700000000000,
  compilation_jobs: [],
  runtime: 'onnx',
} as unknown as TrainingJob;

const SEG_DETECTION = {
  detection_arch: 'rf_detr_seg', network_input_width: 384, network_input_height: 384, class_names: ['crack'],
  num_classes: 1, score_threshold: 0.5, mask_threshold: 0.5, top_k: 100, preserve_aspect: false, output_mode: 'semantic',
};

/** A portal-trained Seg job whose record says semantic output. */
const SEG_TRAINED = { ...baseJob, model_type: 'instance_segmentation', detection: SEG_DETECTION } as unknown as TrainingJob;

/** A portal-trained RF-DETR detection job. */
const RF_TRAINED = {
  ...baseJob, model_type: 'object_detection',
  detection: { detection_arch: 'rf_detr', network_input_width: 512, network_input_height: 512, class_names: ['plate'], num_classes: 1, score_threshold: 0.5, top_k: 300, preserve_aspect: false },
} as unknown as TrainingJob;

/** An imported ONNX Seg record: the mode is fixed at import (no conversion block). */
const SEG_IMPORTED_ONNX = {
  ...SEG_TRAINED, source: 'imported',
  detection: { ...SEG_DETECTION, output_mode: 'instance' },
  metadata: { framework: 'ONNX', model_file: 'model.onnx', fine_tunable: null },
} as unknown as TrainingJob;

/** A Seg Conversion_Record (imported .pth converted to ONNX): the packager builds it, so the override applies. */
const SEG_CONVERSION = {
  ...SEG_TRAINED, source: 'imported',
  metadata: { framework: 'PYTORCH', pt_file: 'checkpoint.pth', fine_tunable: { arch: 'rf_detr_seg', checkpoint_s3: 's3://b/ckpt.pth' } },
  conversion: { status: 'Completed', job_name: 'crack-masks-cnv-20261123' },
} as unknown as TrainingJob;

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.clearAllMocks();
  configState.current = {};
  apiMocks.getCompilationStatus.mockResolvedValue({ compilation_jobs: [] });
  apiMocks.startPackaging.mockResolvedValue({ training_id: TRAINING_ID, packaged_components: [], message: 'ok' });
});

afterEach(() => {
  vi.clearAllMocks();
});

async function renderTab(job: TrainingJob) {
  const rendered = render(<CompilationTab trainingId={job.training_id} trainingJob={job} />);
  await act(async () => {});
  await screen.findByText('Component Actions');
  return rendered;
}

type FormFieldWrapper = ReturnType<ReturnType<typeof createWrapper>['findAllFormFields']>[number];

function findField(container: HTMLElement, label: string): FormFieldWrapper | undefined {
  return createWrapper(container)
    .findAllFormFields()
    .find(field => field.findLabel()?.getElement().textContent?.trim() === label);
}

function outputSelect(container: HTMLElement) {
  const select = findField(container, 'Output')?.findControl()?.findSelect();
  if (!select) throw new Error('No "Output" Select is rendered');
  return select;
}

async function clickPackage() {
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: 'Package Models' }));
  await waitFor(() => {
    expect(apiMocks.startPackaging).toHaveBeenCalledTimes(1);
  });
  return apiMocks.startPackaging.mock.calls[0];
}

// ---------------------------------------------------------------------------
// Cases
// ---------------------------------------------------------------------------

describe('CompilationTab — RF-DETR-Seg Output override (Req 6.4)', () => {
  it('offers the Output select defaulting to the stored mode and sends it as { outputMode }', async () => {
    const { container } = await renderTab(SEG_TRAINED);
    const select = outputSelect(container);
    expect(select.findTrigger().getElement().textContent).toContain('semantic overlay');
    expect(findField(container, 'Output')?.findDescription()?.getElement().textContent).not.toContain('Last packaged');
    const call = await clickPackage();
    expect(call).toEqual([TRAINING_ID, undefined, undefined, { outputMode: 'semantic' }]);
    expect(await screen.findByText(/Packaging completed/)).toBeInTheDocument();
  });

  it('sends the chosen mode after the select changes', async () => {
    const { container } = await renderTab(SEG_TRAINED);
    const select = outputSelect(container);
    select.openDropdown();
    select.selectOptionByValue('instance');
    await waitFor(() => {
      expect(outputSelect(container).findTrigger().getElement().textContent).toContain('instance');
    });
    const call = await clickPackage();
    expect(call).toEqual([TRAINING_ID, undefined, undefined, { outputMode: 'instance' }]);
  });

  it('shows "Last packaged" from packaged_output_mode', async () => {
    const { container } = await renderTab({ ...SEG_TRAINED, packaged_output_mode: 'instance' } as unknown as TrainingJob);
    expect(findField(container, 'Output')?.findDescription()?.getElement().textContent).toContain('Last packaged: instance');
    // The select still defaults to the record's trained mode, not the last packaged one.
    expect(outputSelect(container).findTrigger().getElement().textContent).toContain('semantic overlay');
  });

  it('a detection record gets no select and its call carries no fourth argument', async () => {
    const { container } = await renderTab(RF_TRAINED);
    expect(findField(container, 'Output')).toBeUndefined();
    expect(screen.queryByTestId('output-mode-fixed-note')).toBeNull();
    const call = await clickPackage();
    expect(call).toEqual([TRAINING_ID]);
    expect(call.length).toBe(1);
  });

  it('is absent when the config field is false (the backend packages the stored mode)', async () => {
    configState.current = { rfDetrSegEnabled: false };
    const { container } = await renderTab(SEG_TRAINED);
    expect(findField(container, 'Output')).toBeUndefined();
    expect(screen.queryByTestId('output-mode-fixed-note')).toBeNull();
    const call = await clickPackage();
    expect(call).toEqual([TRAINING_ID]);
  });

  it('an imported ONNX Seg record shows the fixed-at-import note and posts the plain body (C34)', async () => {
    const { container } = await renderTab(SEG_IMPORTED_ONNX);
    expect(findField(container, 'Output')).toBeUndefined();
    expect(screen.getByTestId('output-mode-fixed-note').textContent).toBe(
      'Output mode is fixed at import (instance); re-import to change it'
    );
    const call = await clickPackage();
    expect(call).toEqual([TRAINING_ID]);
  });

  it('a Seg Conversion_Record gets the select (the trained-detection packager builds it)', async () => {
    const { container } = await renderTab(SEG_CONVERSION);
    expect(outputSelect(container).findTrigger().getElement().textContent).toContain('semantic overlay');
    expect(screen.queryByTestId('output-mode-fixed-note')).toBeNull();
    const call = await clickPackage();
    expect(call).toEqual([TRAINING_ID, undefined, undefined, { outputMode: 'semantic' }]);
  });
});
