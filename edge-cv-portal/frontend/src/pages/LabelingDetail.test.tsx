/**
 * Unit tests for the DDA-job display helpers of LabelingDetail
 * (dda-data-labeling task 16.3): progress description including the
 * skip-verification substitution note (Requirements 11.1, 11.10) and
 * Stop-button visibility (Requirements 11.4, 11.9).
 */

import { describe, it, expect } from 'vitest';
import { getDdaProgress, canStopDdaJob } from './LabelingDetail';

describe('getDdaProgress', () => {
  it('reports submitted/total and the backend percentage for team jobs', () => {
    const progress = getDdaProgress({
      submitted_count: 3,
      image_count: 10,
      progress_percent: 30,
    });
    expect(progress.percent).toBe(30);
    expect(progress.description).toBe('3 of 10 tasks submitted');
    expect(progress.note).toBeUndefined();
  });

  it('computes the percentage when the backend omits it', () => {
    const progress = getDdaProgress({ submitted_count: 1, image_count: 3 });
    expect(progress.percent).toBe(33);
  });

  it('handles a zero image count without dividing by zero', () => {
    const progress = getDdaProgress({ submitted_count: 0, image_count: 0 });
    expect(progress.percent).toBe(0);
    expect(progress.description).toBe('0 of 0 tasks submitted');
  });

  it('substitutes auto-label completion wording for skip-verification jobs (Req 11.10)', () => {
    const progress = getDdaProgress({
      submitted_count: 4,
      image_count: 8,
      progress_percent: 50,
      skip_verification: true,
    });
    expect(progress.description).toBe(
      '4 of 8 auto-label attempts completed'
    );
    expect(progress.note).toContain('auto-label completion');
  });
});

describe('canStopDdaJob', () => {
  it('allows stopping only InProgress DDA jobs', () => {
    expect(
      canStopDdaJob({ labeling_backend: 'DDA', status: 'InProgress' })
    ).toBe(true);
  });

  it.each(['Completed', 'Failed', 'Stopped'])(
    'disallows stopping a DDA job in %s status',
    (status) => {
      expect(canStopDdaJob({ labeling_backend: 'DDA', status })).toBe(false);
    }
  );

  it('disallows stopping Ground Truth jobs', () => {
    expect(
      canStopDdaJob({ labeling_backend: 'GroundTruth', status: 'InProgress' })
    ).toBe(false);
    expect(canStopDdaJob({ status: 'InProgress' })).toBe(false);
  });
});
