import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { scaleBox, type Box, type Dimensions } from './scaleBox';

/**
 * Property-based tests for the pure `scaleBox` helper.
 *
 * Feature: object-detection-visualization, Property 8: Box coordinates scale
 * proportionally to the displayed image
 * Validates: Requirements 4.6
 */

// Approximate float equality tolerant to accumulated multiplication error.
function approxEqual(actual: number, expected: number, epsilon = 1e-6): boolean {
  const scale = Math.max(1, Math.abs(actual), Math.abs(expected));
  return Math.abs(actual - expected) <= epsilon * scale;
}

// Finite pixel coordinate generator (bounded to keep float error controlled).
const coord = fc.double({ min: 0, max: 10000, noNaN: true });

// Valid (finite, > 0) image dimensions.
const validDim = (): fc.Arbitrary<Dimensions> =>
  fc.record({
    w: fc.double({ min: 1, max: 10000, noNaN: true }),
    h: fc.double({ min: 1, max: 10000, noNaN: true }),
  });

// An arbitrary box [x_min, y_min, x_max, y_max].
const anyBox = (): fc.Arbitrary<Box> =>
  fc.tuple(coord, coord, coord, coord) as fc.Arbitrary<Box>;

describe('scaleBox (Property 8: Box coordinates scale proportionally to the displayed image)', () => {
  // Feature: object-detection-visualization, Property 8: Box coordinates scale proportionally to the displayed image
  // Validates: Requirements 4.6
  it('scales coordinates by the width/height ratios for any box and valid dims', () => {
    fc.assert(
      fc.property(anyBox(), validDim(), validDim(), (box, src, disp) => {
        const [xMin, yMin, xMax, yMax] = box;
        const rw = disp.w / src.w;
        const rh = disp.h / src.h;
        const result = scaleBox(box, src, disp);

        expect(approxEqual(result.x, xMin * rw)).toBe(true);
        expect(approxEqual(result.y, yMin * rh)).toBe(true);
        expect(approxEqual(result.w, (xMax - xMin) * rw)).toBe(true);
        expect(approxEqual(result.h, (yMax - yMin) * rh)).toBe(true);
      }),
      { numRuns: 200 }
    );
  });

  // Feature: object-detection-visualization, Property 8: Box coordinates scale proportionally to the displayed image
  // Validates: Requirements 4.6
  it('is the identity transform when display dimensions equal source dimensions', () => {
    fc.assert(
      fc.property(anyBox(), validDim(), (box, src) => {
        const [xMin, yMin, xMax, yMax] = box;
        const result = scaleBox(box, src, { w: src.w, h: src.h });

        expect(approxEqual(result.x, xMin)).toBe(true);
        expect(approxEqual(result.y, yMin)).toBe(true);
        expect(approxEqual(result.w, xMax - xMin)).toBe(true);
        expect(approxEqual(result.h, yMax - yMin)).toBe(true);
      }),
      { numRuns: 200 }
    );
  });

  // Feature: object-detection-visualization, Property 8: Box coordinates scale proportionally to the displayed image
  // Validates: Requirements 4.6
  it('keeps an in-bounds source box within the display bounds', () => {
    // Generate a box guaranteed to sit within source bounds:
    // 0 <= x_min <= x_max <= src.w and 0 <= y_min <= y_max <= src.h.
    const inBoundsCase = validDim().chain((src) =>
      fc
        .tuple(
          fc.double({ min: 0, max: src.w, noNaN: true }),
          fc.double({ min: 0, max: src.w, noNaN: true }),
          fc.double({ min: 0, max: src.h, noNaN: true }),
          fc.double({ min: 0, max: src.h, noNaN: true })
        )
        .map(([a, b, c, d]) => {
          const xMin = Math.min(a, b);
          const xMax = Math.max(a, b);
          const yMin = Math.min(c, d);
          const yMax = Math.max(c, d);
          const box: Box = [xMin, yMin, xMax, yMax];
          return { src, box };
        })
    );

    fc.assert(
      fc.property(inBoundsCase, validDim(), ({ src, box }, disp) => {
        const result = scaleBox(box, src, disp);
        const epsilon = 1e-6 * Math.max(disp.w, disp.h, 1);

        expect(result.x).toBeGreaterThanOrEqual(-epsilon);
        expect(result.y).toBeGreaterThanOrEqual(-epsilon);
        expect(result.x + result.w).toBeLessThanOrEqual(disp.w + epsilon);
        expect(result.y + result.h).toBeLessThanOrEqual(disp.h + epsilon);
      }),
      { numRuns: 200 }
    );
  });

  // Feature: object-detection-visualization, Property 8: Box coordinates scale proportionally to the displayed image
  // Validates: Requirements 4.6
  it('returns a zeroed no-op box when source dimensions are non-finite or <= 0', () => {
    const badDim = fc.oneof(
      fc.constant(0),
      fc.double({ min: -10000, max: 0, noNaN: true }),
      fc.constant(Number.NaN),
      fc.constant(Number.POSITIVE_INFINITY),
      fc.constant(Number.NEGATIVE_INFINITY)
    );

    fc.assert(
      fc.property(
        anyBox(),
        fc.record({ w: fc.double({ noNaN: false }), h: fc.double({ noNaN: false }) }),
        validDim(),
        badDim,
        (box, maybeBadSrc, disp, forcedBad) => {
          // Force at least one invalid source dimension.
          const src: Dimensions = fc.sample(fc.boolean(), 1)[0]
            ? { w: forcedBad, h: maybeBadSrc.h }
            : { w: maybeBadSrc.w, h: forcedBad };

          const invalid =
            !Number.isFinite(src.w) ||
            !Number.isFinite(src.h) ||
            src.w <= 0 ||
            src.h <= 0;

          if (!invalid) return; // only assert the guard branch

          const result = scaleBox(box, src, disp);
          expect(result).toEqual({ x: 0, y: 0, w: 0, h: 0 });
        }
      ),
      { numRuns: 200 }
    );
  });
});
