/**
 * Unit tests for the source image size-limit helpers
 * (fix-synthetic-image-size-limits): the descriptions and violation
 * messages match the backend's wording for the three catalog models.
 */
import { describe, expect, it } from 'vitest';
import {
  DEFAULT_OVERSIZE_HANDLING,
  OVERSIZE_HANDLING_ITEMS,
  describeImageLimits,
  imageLimitViolation,
} from './imageLimits';
import type { SyntheticImageLimits } from './types';

const STABILITY: SyntheticImageLimits = {
  min_side: 64,
  min_pixels: 4096,
  max_pixels: 9437184,
};
const NOVA: SyntheticImageLimits = {
  min_side: 320,
  max_side: 4096,
  max_pixels: 4194303,
};
const TITAN: SyntheticImageLimits = { max_side: 1408 };

describe('describeImageLimits', () => {
  it('describes the three catalog models like the backend', () => {
    expect(describeImageLimits(STABILITY)).toBe(
      'images between 4,096 and 9,437,184 pixels in total, with each side at least 64 px'
    );
    expect(describeImageLimits(NOVA)).toBe(
      'images up to 4,194,303 pixels in total, with each side between 320 and 4,096 px'
    );
    expect(describeImageLimits(TITAN)).toBe(
      'images with the longest side at most 1,408 px'
    );
    expect(describeImageLimits({})).toBe('images of any size');
  });
});

describe('imageLimitViolation', () => {
  it('names the pixel count and the maximum for the 4608x3288 sample', () => {
    const violation = imageLimitViolation(4608, 3288, STABILITY);
    expect(violation).toContain('15,151,104');
    expect(violation).toContain('9,437,184');
  });

  it('accepts the fitted 3636x2594 size', () => {
    expect(imageLimitViolation(3636, 2594, STABILITY)).toBeNull();
  });

  it('names the shortest side when only it is too small', () => {
    expect(imageLimitViolation(200, 40, STABILITY)).toContain(
      'the shortest side (40 px)'
    );
    // 64x48 is 3,072 px: the pixel minimum is reported first.
    expect(imageLimitViolation(64, 48, STABILITY)).toContain(
      '3,072 pixels is below the minimum of 4,096 pixels'
    );
  });

  it('names the longest side for Titan', () => {
    expect(imageLimitViolation(4608, 3288, TITAN)).toBe(
      'the longest side (4608 px) exceeds the maximum of 1,408 px'
    );
    expect(imageLimitViolation(1408, 1000, TITAN)).toBeNull();
  });
});

describe('handling choices', () => {
  it('defaults to resize-and-restore and offers exactly the two backend choices', () => {
    expect(DEFAULT_OVERSIZE_HANDLING).toBe('resize_restore');
    expect(OVERSIZE_HANDLING_ITEMS.map((item) => item.value)).toEqual([
      'resize_restore',
      'fail',
    ]);
  });
});
