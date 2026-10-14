/**
 * Unit tests for the simulator-view helpers (custom-node-designer
 * task 12.4, Requirements 7.1, 7.3, 7.4, 7.5 plus the failure/timeout
 * presentation of 7.6, 7.7).
 */
import { describe, expect, it } from 'vitest';
import { ApiError } from '../../services/api';
import type {
  SimulationResultsDocument,
  SimulationRunSummary,
} from './types';
import {
  MISSING_X86_64_CODE,
  MISSING_X86_64_MESSAGE,
  coerceParameterValue,
  dataUrlToBase64,
  describeRunFailure,
  describeStartError,
  frameLabel,
  hasSuccessfulX86Build,
  isRenderableUrl,
  isSupportedFrameName,
  isTerminalStatus,
  orderedFrames,
  parametersFromRows,
  rowsFromParameters,
} from './simulation';

function runWith(overrides: Partial<SimulationRunSummary>): SimulationRunSummary {
  return {
    run_id: 'run-1',
    plugin_id: 'plugin-1',
    version: 1,
    usecase_id: 'uc-1',
    dataset: { kind: 'dataset', dataset_id: 'ds-1' },
    parameters: {},
    element_factory: 'myelement',
    status: 'completed',
    results_s3_key: null,
    failure: null,
    started_at: null,
    finished_at: null,
    created_by: 'user',
    ...overrides,
  };
}

describe('hasSuccessfulX86Build (7.5)', () => {
  it('passes exactly when a succeeded x86_64 artifact with an s3Key exists', () => {
    expect(
      hasSuccessfulX86Build({
        x86_64: { buildStatus: 'succeeded', s3Key: 'plugins/custom/uc/x86_64/p.so' },
      })
    ).toBe(true);
  });

  it('fails for missing, unbuilt, failed, or key-less x86_64 entries', () => {
    expect(hasSuccessfulX86Build(undefined)).toBe(false);
    expect(hasSuccessfulX86Build(null)).toBe(false);
    expect(hasSuccessfulX86Build({})).toBe(false);
    expect(hasSuccessfulX86Build({ x86_64: { buildStatus: 'failed', s3Key: 'k' } })).toBe(false);
    expect(hasSuccessfulX86Build({ x86_64: { buildStatus: 'building', s3Key: 'k' } })).toBe(false);
    expect(hasSuccessfulX86Build({ x86_64: { buildStatus: 'succeeded' } })).toBe(false);
    expect(
      hasSuccessfulX86Build({
        arm64_jp5: { buildStatus: 'succeeded', s3Key: 'k' },
      })
    ).toBe(false);
  });
});

describe('parameter editor helpers (7.4)', () => {
  it('coerces booleans and numbers and leaves other text as strings', () => {
    expect(coerceParameterValue('true')).toBe(true);
    expect(coerceParameterValue('false')).toBe(false);
    expect(coerceParameterValue('42')).toBe(42);
    expect(coerceParameterValue('-3.5')).toBe(-3.5);
    expect(coerceParameterValue('  7 ')).toBe(7);
    expect(coerceParameterValue('fast')).toBe('fast');
    expect(coerceParameterValue('')).toBe('');
    expect(coerceParameterValue('1px')).toBe('1px');
  });

  it('assembles parameters from rows, skipping unnamed rows', () => {
    expect(
      parametersFromRows([
        { name: 'radius', value: '3' },
        { name: '', value: 'ignored' },
        { name: '  ', value: 'ignored too' },
        { name: 'mode', value: 'fast' },
        { name: 'enabled', value: 'true' },
      ])
    ).toEqual({ radius: 3, mode: 'fast', enabled: true });
  });

  it('lets later duplicate names win', () => {
    expect(
      parametersFromRows([
        { name: 'radius', value: '1' },
        { name: 'radius', value: '2' },
      ])
    ).toEqual({ radius: 2 });
  });

  it('round-trips a previous run\'s parameters into editable rows', () => {
    const rows = rowsFromParameters({ radius: 3, mode: 'fast', flag: true, empty: null });
    expect(rows).toEqual([
      { name: 'radius', value: '3' },
      { name: 'mode', value: 'fast' },
      { name: 'flag', value: 'true' },
      { name: 'empty', value: '' },
    ]);
    expect(rowsFromParameters(undefined)).toEqual([]);
  });
});

describe('sample upload helpers (7.1)', () => {
  it('accepts only JPEG/PNG file names', () => {
    expect(isSupportedFrameName('frame.jpg')).toBe(true);
    expect(isSupportedFrameName('frame.JPEG')).toBe(true);
    expect(isSupportedFrameName('frame.png')).toBe(true);
    expect(isSupportedFrameName('frame.gif')).toBe(false);
    expect(isSupportedFrameName('frame.mp4')).toBe(false);
    expect(isSupportedFrameName('noextension')).toBe(false);
  });

  it('strips the data-URL prefix from FileReader results', () => {
    expect(dataUrlToBase64('data:image/png;base64,AAAA')).toBe('AAAA');
    expect(dataUrlToBase64('AAAA')).toBe('AAAA');
  });
});

describe('frame rendering helpers (7.3)', () => {
  it('labels a frame reference with its file name', () => {
    expect(frameLabel('simulations/uc/run/frames/out_000003.png')).toBe('out_000003.png');
    expect(frameLabel('plain.png')).toBe('plain.png');
    expect(frameLabel(null)).toBe('');
  });

  it('treats only http(s) references as renderable image URLs', () => {
    expect(isRenderableUrl('https://bucket.s3.amazonaws.com/f.png?sig=x')).toBe(true);
    expect(isRenderableUrl('http://localhost/f.png')).toBe(true);
    expect(isRenderableUrl('simulations/uc/run/frames/f.png')).toBe(false);
    expect(isRenderableUrl(null)).toBe(false);
  });

  it('orders frames by frameIndex even in partial documents (7.6, 7.7)', () => {
    const results: SimulationResultsDocument = {
      frames: [
        { frameIndex: 2, inputRef: 'in2', outputRef: 'out2', metadata: {} },
        { frameIndex: 0, inputRef: 'in0', outputRef: 'out0', metadata: {} },
        { frameIndex: 1, inputRef: 'in1', outputRef: null, metadata: {} },
      ],
    };
    expect(orderedFrames(results).map((f) => f.frameIndex)).toEqual([0, 1, 2]);
    // the input document is not mutated
    expect(results.frames?.map((f) => f.frameIndex)).toEqual([2, 0, 1]);
  });

  it('returns an empty strip for missing or malformed results', () => {
    expect(orderedFrames(null)).toEqual([]);
    expect(orderedFrames({})).toEqual([]);
    expect(orderedFrames({ frames: undefined })).toEqual([]);
  });
});

describe('run-state derivation', () => {
  it('treats completed and failed as terminal, pending/running as live', () => {
    expect(isTerminalStatus('completed')).toBe(true);
    expect(isTerminalStatus('failed')).toBe(true);
    expect(isTerminalStatus('running')).toBe(false);
    expect(isTerminalStatus('pending')).toBe(false);
    expect(isTerminalStatus(undefined)).toBe(false);
  });
});

describe('describeRunFailure (7.6, 7.7)', () => {
  it('returns null for completed and running runs', () => {
    expect(describeRunFailure(runWith({ status: 'completed' }), null)).toBeNull();
    expect(describeRunFailure(runWith({ status: 'running' }), null)).toBeNull();
  });

  it('labels a timeout failure distinctly (7.7)', () => {
    const view = describeRunFailure(
      runWith({
        status: 'failed',
        failure: { message: 'Simulation exceeded the 5-minute limit', timeout: true },
      }),
      null
    );
    expect(view).not.toBeNull();
    expect(view!.timeout).toBe(true);
    expect(view!.header).toBe('Simulation timed out');
    expect(view!.message).toContain('5-minute');
  });

  it('carries the captured plugin error output for plugin failures (7.6)', () => {
    const view = describeRunFailure(
      runWith({ status: 'failed', failure: { message: 'plugin crashed' } }),
      { error: { errorOutput: 'gst-launch error: segfault in myelement' } }
    );
    expect(view!.timeout).toBe(false);
    expect(view!.header).toBe('Simulation failed');
    expect(view!.errorOutput).toContain('segfault');
  });

  it('tolerates a missing failure record and blank error output', () => {
    const bare = describeRunFailure(runWith({ status: 'failed', failure: null }), null);
    expect(bare!.message).toBe('Simulation run failed');
    expect(bare!.errorOutput).toBeNull();
    const blank = describeRunFailure(
      runWith({ status: 'failed', failure: { message: 'x' } }),
      { error: { errorOutput: '   ' } }
    );
    expect(blank!.errorOutput).toBeNull();
  });
});

describe('describeStartError (7.5)', () => {
  it('surfaces the backend missing-x86_64 refusal message', () => {
    const err = new ApiError(
      'Simulation requires a successful x86_64 build.',
      409,
      MISSING_X86_64_CODE
    );
    expect(describeStartError(err)).toBe('Simulation requires a successful x86_64 build.');
  });

  it('falls back to the fixed refusal text when the 409 carries no message', () => {
    const err = new ApiError('', 409, MISSING_X86_64_CODE);
    expect(describeStartError(err)).toBe(MISSING_X86_64_MESSAGE);
  });

  it('uses the API message for other errors and a default otherwise', () => {
    expect(describeStartError(new ApiError('dataset not found', 404, 'DATASET_NOT_FOUND'))).toBe(
      'dataset not found'
    );
    expect(describeStartError(new Error('network down'))).toBe('network down');
    expect(describeStartError('boom')).toBe('The simulation run could not be started.');
  });
});
