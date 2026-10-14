/**
 * CompilationTab gating for portal-trained Object Detection jobs
 * (portal-detection-training task 6.3, Requirement 7.1).
 *
 * A completed `object_detection` job has no compilation jobs and never will
 * (the backend bypasses Neo). The tab must therefore treat it like an imported
 * ONNX model: no "Start Compilation" empty state, Component Actions visible
 * with the ONNX info alert. A completed LFV job with no compilation jobs must
 * still get the empty state (unchanged behaviour).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';

import CompilationTab from './CompilationTab';
import { TrainingJob } from '../types';

const { getCompilationStatus, otherApiCalls } = vi.hoisted(() => ({
  getCompilationStatus: vi.fn(),
  otherApiCalls: [] as string[],
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
  const apiService = new Proxy(
    { getCompilationStatus },
    {
      get(target, prop) {
        if (prop in target) {
          return target[prop as keyof typeof target];
        }
        return (..._args: unknown[]) => {
          otherApiCalls.push(String(prop));
          return Promise.resolve({});
        };
      },
    }
  );
  return { ApiError, apiService };
});

const baseJob = {
  training_id: 'training-det-1',
  usecase_id: 'usecase-1',
  model_name: 'blue-plate',
  model_version: '2.0.0',
  dataset_manifest_s3: 's3://bucket/labeled/output.manifest',
  algorithm_uri: '763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-training:2.5.1-gpu-py311-cu124-ubuntu22.04-sagemaker',
  hyperparameters: {},
  instance_type: 'ml.g4dn.xlarge',
  training_job_arn: 'arn:aws:sagemaker:us-east-1:123456789012:training-job/blue-plate-x',
  status: 'Completed',
  metrics: { 'test:mAP50': 0.995 },
  artifact_s3: 's3://bucket/models/training/blue-plate-x/output/model.tar.gz',
  created_by: 'tester',
  created_at: 1700000000000,
  compilation_jobs: [],
} as unknown as TrainingJob;

async function renderTab(job: TrainingJob) {
  getCompilationStatus.mockResolvedValue({ compilation_jobs: [] });
  const rendered = render(<CompilationTab trainingId={job.training_id} trainingJob={job} />);
  await act(async () => {});
  return rendered;
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  otherApiCalls.length = 0;
});

describe('CompilationTab — portal-trained detection gating (Req 7.1)', () => {
  it('offers Package/Publish (no compile) for a completed object_detection job', async () => {
    await renderTab({ ...baseJob, model_type: 'object_detection', runtime: 'onnx' } as TrainingJob);

    expect(screen.queryByText('Start Compilation')).toBeNull();
    expect(screen.queryByText('No compilation jobs')).toBeNull();
    expect(screen.getByText('Component Actions')).not.toBeNull();
    expect(screen.getByText(/don't require\s+SageMaker Neo compilation/)).not.toBeNull();
    expect(screen.getByText('Package Models')).not.toBeNull();
    // The publish modal (rendered hidden) also carries this label.
    expect(screen.getAllByText('Publish Component').length).toBeGreaterThan(0);
  });

  it('keeps the Neo empty state for an object_detection record WITHOUT runtime onnx (TorchScript detector)', async () => {
    // Matches the backend predicate: earlier specs seed object_detection
    // records whose artifact is a .pt and which still compile through Neo /
    // the ONNX export job. Only training.py's `runtime: 'onnx'` bypasses.
    await renderTab({ ...baseJob, model_type: 'object_detection' } as TrainingJob);
    expect(screen.getByText('Start Compilation')).not.toBeNull();
    expect(screen.queryByText('Component Actions')).toBeNull();
  });

  it('recognizes the job by runtime alone', async () => {
    await renderTab({ ...baseJob, runtime: 'onnx' } as TrainingJob);
    expect(screen.queryByText('Start Compilation')).toBeNull();
    expect(screen.getByText('Component Actions')).not.toBeNull();
  });

  it('still shows the Start Compilation empty state for a completed LFV job', async () => {
    await renderTab({ ...baseJob, model_type: 'classification' } as TrainingJob);
    expect(screen.getByText('No compilation jobs')).not.toBeNull();
    expect(screen.getByText('Start Compilation')).not.toBeNull();
    expect(screen.queryByText('Component Actions')).toBeNull();
  });
});

/**
 * detector-checkpoint-import task 8.4 (Requirements 11.4, 9.5-9.7, 7.9): a
 * Conversion_Record is a detection ONNX package. Its metadata names the
 * PyTorch checkpoint it was converted from (framework PYTORCH,
 * checkpoint.pt), yet it never goes through Neo. Packaging is the server's
 * job: Package is disabled while the conversion runs or after it failed, and
 * stays available while Finalizing so a lost finalize can be retried.
 */
describe('CompilationTab — detector Conversion_Record', () => {
  const conversionJob = (conversionStatus: string) =>
    ({
      ...baseJob,
      training_id: 'cnv-1',
      model_name: 'ppe-detection',
      model_version: '1.0.0',
      dataset_manifest_s3: undefined,
      source: 'imported',
      model_type: 'object_detection',
      runtime: 'onnx',
      instance_type: 'ml.m5.xlarge',
      status:
        conversionStatus === 'Completed' ? 'Completed' : conversionStatus === 'Failed' ? 'Failed' : 'InProgress',
      metadata: {
        framework: 'PYTORCH',
        framework_version: 'ultralytics 8.4.2',
        model_file: 'checkpoint.pt',
        pt_file: 'checkpoint.pt',
        fine_tunable: {
          arch: 'yolo',
          kind: 'ultralytics_checkpoint',
          checkpoint_s3: 's3://bucket/converted-models/ppe_detection-1a2b3c4d/checkpoint.pt',
          class_names: ['helmet', 'human', 'no-helmet', 'vest'],
          num_classes: 4,
        },
      },
      conversion: { status: conversionStatus, job_name: 'ppe_detection-cnv-20261001120000' },
      ...(conversionStatus === 'Completed'
        ? {
            packaged_components: [
              { target: 'jetson-xavier-jp7', component_package_s3: 's3://bucket/pkg.zip', status: 'packaged' },
            ],
          }
        : {}),
    }) as unknown as TrainingJob;

  it('treats a Completed conversion like a detection ONNX package: no Neo, Package and Publish offered', async () => {
    await renderTab(conversionJob('Completed'));

    expect(screen.queryByText('Start Compilation')).toBeNull();
    expect(screen.queryByText('No compilation jobs')).toBeNull();
    expect(screen.getByText('Component Actions')).not.toBeNull();
    expect(screen.getByText(/don't require\s+SageMaker Neo compilation/)).not.toBeNull();
    expect(screen.getByRole('button', { name: 'Re-package Models' })).not.toBeDisabled();
    expect(screen.getAllByRole('button', { name: 'Publish Component' })[0]).not.toBeDisabled();
    expect(screen.getByTestId('conversion-packaging-note').textContent).toContain('packaged automatically');
    expect(otherApiCalls).not.toContain('startCompilation');
  });

  it('disables Package while the conversion runs and says the server finalizes', async () => {
    await renderTab(conversionJob('InProgress'));

    expect(screen.queryByText('Start Compilation')).toBeNull();
    expect(screen.getByRole('button', { name: 'Package Models' })).toBeDisabled();
    expect(screen.getByTestId('conversion-packaging-note').textContent).toContain(
      'validates, packages and publishes the component automatically'
    );
    expect(otherApiCalls).not.toContain('startPackaging');
  });

  it('keeps Package available while Finalizing, to retry a lost finalize', async () => {
    await renderTab(conversionJob('Finalizing'));

    expect(screen.getByRole('button', { name: 'Package Models' })).not.toBeDisabled();
    expect(screen.getByTestId('conversion-packaging-note').textContent).toContain('Package Models retries it');
  });

  it('disables Package for a failed conversion', async () => {
    await renderTab(conversionJob('Failed'));

    expect(screen.queryByText('Start Compilation')).toBeNull();
    expect(screen.getByRole('button', { name: 'Package Models' })).toBeDisabled();
    expect(screen.getByTestId('conversion-packaging-note').textContent).toContain('conversion failed');
  });

  it('leaves a plain ONNX import (no conversion block) packageable as before', async () => {
    const { conversion: _c, ...plainImport } = conversionJob('Completed') as unknown as Record<string, unknown>;
    await renderTab({
      ...plainImport,
      metadata: { framework: 'ONNX', model_file: 'model.onnx', pt_file: 'model.onnx', fine_tunable: null },
    } as unknown as TrainingJob);

    expect(screen.getByText('Component Actions')).not.toBeNull();
    expect(screen.queryByTestId('conversion-packaging-note')).toBeNull();
    expect(screen.getByRole('button', { name: 'Re-package Models' })).not.toBeDisabled();
  });
});
