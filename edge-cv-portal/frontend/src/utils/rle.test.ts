/**
 * rfdetr-seg-support task 11.4 (Requirement 11.2, DD5, DD20): the shared
 * Canonical_RLE codec in `utils/rle.ts`, pinned to the repo-wide vector file
 * `test/fixtures/canonical_rle_vectors.json` that every encoder and decoder of
 * the format reproduces (the Portal layer's `dda_manifest.rle_encode` is the
 * oracle), so the Portal viewer paints exactly the mask the device encoded.
 */
import { describe, expect, it } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';
import {
  decodeRleColumnMajor,
  encodeRleColumnMajor,
  parseRleCounts,
  pixelsToImageData,
  rleToImageData,
  type Rgba,
} from './rle';

interface Vector {
  name: string;
  width: number;
  height: number;
  mask_rows: string[];
  rle: string;
}

const VECTORS: Vector[] = JSON.parse(
  fs.readFileSync(
    path.resolve(__dirname, '../../../../test/fixtures/canonical_rle_vectors.json'),
    'utf8'
  )
).vectors;

const RGBA: Rgba = [230, 25, 75, 102];

function rowsToBits(rows: string[]): Uint8Array {
  return Uint8Array.from(rows.join('').split('').map((char) => (char === '1' ? 1 : 0)));
}

function pixel(data: Uint8ClampedArray, width: number, x: number, y: number): number[] {
  const offset = (y * width + x) * 4;
  return Array.from(data.slice(offset, offset + 4));
}

describe('utils/rle against the shared Canonical_RLE vectors', () => {
  it('ships the seven vectors', () => {
    expect(VECTORS.length).toBeGreaterThanOrEqual(7);
    expect(VECTORS.map((v) => v.name)).toContain('border_touching');
  });

  it.each(VECTORS.map((v) => [v.name, v] as const))(
    'encodes %s byte-identically to dda_manifest.rle_encode',
    (_name, vector) => {
      const bits = rowsToBits(vector.mask_rows);
      expect(encodeRleColumnMajor(bits, vector.width, vector.height, 1).join(' ')).toBe(
        vector.rle
      );
    }
  );

  it.each(VECTORS.map((v) => [v.name, v] as const))(
    'decodes %s back to the row-major mask',
    (_name, vector) => {
      const bits = rowsToBits(vector.mask_rows);
      expect(Array.from(decodeRleColumnMajor(vector.rle, vector.width, vector.height))).toEqual(
        Array.from(bits)
      );
      expect(
        Array.from(
          decodeRleColumnMajor(parseRleCounts(vector.rle), vector.width, vector.height)
        )
      ).toEqual(Array.from(bits));
    }
  );

  it('parses the canonical counts string and tolerates a numeric array', () => {
    expect(parseRleCounts('0 5 2 2 2 5')).toEqual([0, 5, 2, 2, 2, 5]);
    expect(parseRleCounts('  6 ')).toEqual([6]);
    expect(parseRleCounts([6])).toEqual([6]);
    expect(parseRleCounts('')).toEqual([]);
  });
});

describe('rleToImageData', () => {
  it.each(VECTORS.map((v) => [v.name, v] as const))(
    'paints %s in the given colour where the mask is set and transparent elsewhere',
    (_name, vector) => {
      const image = rleToImageData(vector.rle, vector.width, vector.height, RGBA);
      expect(image.width).toBe(vector.width);
      expect(image.height).toBe(vector.height);
      expect(image.data.length).toBe(vector.width * vector.height * 4);
      const bits = rowsToBits(vector.mask_rows);
      for (let y = 0; y < vector.height; y++) {
        for (let x = 0; x < vector.width; x++) {
          const expected = bits[y * vector.width + x] ? RGBA : [0, 0, 0, 0];
          expect(pixel(image.data, vector.width, x, y)).toEqual(expected);
        }
      }
    }
  );

  it('rejects runs that do not sum to width x height (a mask at another resolution)', () => {
    expect(() => rleToImageData('0 5 2 2 2 5', 5, 4, RGBA)).toThrow(RangeError);
    expect(() => rleToImageData('0 5 2 2 2 5', 4, 4, RGBA)).not.toThrow();
  });

  it('rejects non-integer or negative run lengths and a bad size', () => {
    expect(() => rleToImageData('1 x 14', 4, 4, RGBA)).toThrow(RangeError);
    expect(() => rleToImageData('1.5 14.5', 4, 4, RGBA)).toThrow(RangeError);
    expect(() => rleToImageData('16', 4.5, 4, RGBA)).toThrow(RangeError);
    expect(() => rleToImageData('16', -4, -4, RGBA)).toThrow(RangeError);
  });

  it('accepts a counts array and an empty mask', () => {
    const image = rleToImageData([6], 3, 2, RGBA);
    expect(Array.from(image.data).every((v) => v === 0)).toBe(true);
    const full = rleToImageData([0, 6], 3, 2, RGBA);
    expect(pixel(full.data, 3, 2, 1)).toEqual(RGBA);
  });

  it('pixelsToImageData stands in for the platform ImageData where jsdom has none', () => {
    const data = new Uint8ClampedArray(2 * 1 * 4);
    const image = pixelsToImageData(data, 2, 1);
    expect(image.width).toBe(2);
    expect(image.height).toBe(1);
    expect(image.data).toBe(data);
    if (typeof ImageData !== 'undefined') {
      expect(image).toBeInstanceOf(ImageData);
    }
  });
});
