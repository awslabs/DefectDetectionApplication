/**
 * Unit tests for the pure annotation helpers of AnnotationCanvas:
 * COCO-style column-major RLE encode/decode and box clamping
 * (dda-data-labeling Requirements 7.4, 7.5).
 */
import { describe, it, expect } from 'vitest';
import {
  encodeRleColumnMajor,
  decodeRleColumnMajor,
  parseRleCounts,
  clampBoxToImage,
} from './AnnotationCanvas';

describe('parseRleCounts', () => {
  it('parses the backend space-separated counts string', () => {
    expect(parseRleCounts('4 1 4')).toEqual([4, 1, 4]);
  });

  it('parses a real SAM-style counts string with large runs', () => {
    expect(parseRleCounts('8238 8 754 25 3 4')).toEqual([
      8238, 8, 754, 25, 3, 4,
    ]);
  });

  it('tolerates extra whitespace', () => {
    expect(parseRleCounts('  4  1 4 ')).toEqual([4, 1, 4]);
  });

  it('passes numeric arrays through unchanged', () => {
    expect(parseRleCounts([4, 1, 4])).toEqual([4, 1, 4]);
  });

  it('round-trips a string prelabel through decode without character iteration', () => {
    // 3x3 with only the centre pixel set: "4 1 4". Iterating the string
    // itself (the old bug) would walk 5 characters instead of 3 counts
    // and corrupt the mask.
    const mask = decodeRleColumnMajor(parseRleCounts('4 1 4'), 3, 3);
    expect(Array.from(mask)).toEqual([0, 0, 0, 0, 1, 0, 0, 0, 0]);
  });
});

describe('encodeRleColumnMajor / decodeRleColumnMajor', () => {
  it('encodes an empty bitmap as a single zero-run', () => {
    const bitmap = new Uint8Array(4 * 3);
    expect(encodeRleColumnMajor(bitmap, 4, 3, 1)).toEqual([12]);
  });

  it('encodes a full bitmap as zero-count then full run', () => {
    const bitmap = new Uint8Array(4 * 3).fill(1);
    expect(encodeRleColumnMajor(bitmap, 4, 3, 1)).toEqual([0, 12]);
  });

  it('visits pixels in column-major order', () => {
    // 2x2, only pixel (x=0, y=1) set: column-major order is
    // (0,0) (0,1) (1,0) (1,1) -> 0,1,0,0 -> counts [1,1,2]
    const bitmap = new Uint8Array(2 * 2);
    bitmap[1 * 2 + 0] = 1;
    expect(encodeRleColumnMajor(bitmap, 2, 2, 1)).toEqual([1, 1, 2]);
  });

  it('only encodes pixels matching the requested class value', () => {
    const bitmap = new Uint8Array(2 * 2);
    bitmap[0] = 1; // class 1 at (0,0)
    bitmap[3] = 2; // class 2 at (1,1)
    expect(encodeRleColumnMajor(bitmap, 2, 2, 1)).toEqual([0, 1, 3]);
    expect(encodeRleColumnMajor(bitmap, 2, 2, 2)).toEqual([3, 1]);
  });

  it('round-trips arbitrary bitmaps', () => {
    const width = 7;
    const height = 5;
    const bitmap = new Uint8Array(width * height);
    // Deterministic pseudo-random pattern.
    let seed = 42;
    for (let p = 0; p < bitmap.length; p++) {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      bitmap[p] = seed % 3 === 0 ? 1 : 0;
    }
    const counts = encodeRleColumnMajor(bitmap, width, height, 1);
    const decoded = decodeRleColumnMajor(counts, width, height);
    expect(Array.from(decoded)).toEqual(Array.from(bitmap));
  });

  it('produces counts convertible to the backend space-separated string', () => {
    const bitmap = new Uint8Array(3 * 3);
    bitmap[4] = 1; // center pixel
    const counts = encodeRleColumnMajor(bitmap, 3, 3, 1);
    expect(counts.join(' ')).toBe('4 1 4');
  });
});

describe('clampBoxToImage', () => {
  it('leaves in-bounds boxes unchanged (rounded)', () => {
    expect(
      clampBoxToImage({ left: 10.4, top: 5.6, width: 20, height: 30 }, 100, 100)
    ).toEqual({ left: 10, top: 6, width: 20, height: 30 });
  });

  it('clamps boxes extending past the image edges', () => {
    expect(
      clampBoxToImage({ left: -5, top: 90, width: 30, height: 30 }, 100, 100)
    ).toEqual({ left: 0, top: 90, width: 25, height: 10 });
  });

  it('collapses fully out-of-bounds boxes to zero area on the edge', () => {
    const clamped = clampBoxToImage(
      { left: 150, top: 150, width: 10, height: 10 },
      100,
      100
    );
    expect(clamped.left).toBe(100);
    expect(clamped.top).toBe(100);
    expect(clamped.width).toBe(0);
    expect(clamped.height).toBe(0);
  });
});
