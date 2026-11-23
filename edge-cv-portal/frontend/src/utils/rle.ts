/**
 * Canonical_RLE codec (COCO-style column-major counts) shared by the
 * labeling AnnotationCanvas and the Portal results viewer
 * (rfdetr-seg-support DD20). The three functions moved here verbatim from
 * `components/labeling/AnnotationCanvas.tsx`, which re-exports them so its
 * callers and tests are unchanged; importing the canvas module into the
 * viewer would pull the whole labeling UI. `rleToImageData` (task 11.3) is
 * the viewer's canvas decoder for a device `mask_rle`.
 *
 * Wire format: the backend's canonical form is the space-separated counts
 * string ("4 1 4") over the mask flattened column-major (down each column,
 * left to right), counts alternating starting with the zero-run (a leading
 * "0" when the first pixel is foreground); `sum(counts) == width * height`.
 */

/** An RGBA colour, each channel 0..255. */
export type Rgba = [number, number, number, number];

/**
 * Encode the pixels of `bitmap` equal to `value` as COCO-style RLE counts:
 * pixels are visited in column-major order (down each column, left to
 * right) and counts alternate starting with the count of zeros. Joining
 * the returned counts with spaces yields the backend's canonical
 * space-separated counts string.
 */
export function encodeRleColumnMajor(
  bitmap: Uint8Array,
  width: number,
  height: number,
  value: number
): number[] {
  const counts: number[] = [];
  let current = 0;
  let run = 0;
  for (let x = 0; x < width; x++) {
    const columnBase = x;
    for (let y = 0; y < height; y++) {
      const v = bitmap[y * width + columnBase] === value ? 1 : 0;
      if (v === current) {
        run++;
      } else {
        counts.push(run);
        current = v;
        run = 1;
      }
    }
  }
  counts.push(run);
  return counts;
}

/**
 * Parse an RLE value from the API into numeric counts. The backend's
 * canonical form is the space-separated counts string ("4 1 4"); a
 * numeric array is tolerated for robustness. Iterating the string
 * directly must never happen — that walks characters, not counts.
 */
export function parseRleCounts(rle: string | number[]): number[] {
  if (Array.isArray(rle)) {
    return rle;
  }
  return rle
    .split(/\s+/)
    .filter((token) => token.length > 0)
    .map(Number);
}

/**
 * Decode COCO-style column-major RLE counts into a binary mask stored in
 * row-major order (1 where the region is present). Inverse of
 * {@link encodeRleColumnMajor}. Accepts either a counts array or the
 * backend's canonical space-separated counts string (the shape
 * `DdaMaskRegion.rle` actually carries at runtime).
 */
export function decodeRleColumnMajor(
  rle: number[] | string,
  width: number,
  height: number
): Uint8Array {
  const counts =
    typeof rle === 'string'
      ? rle
          .split(/\s+/)
          .filter((token) => token.length > 0)
          .map(Number)
      : rle;
  const mask = new Uint8Array(width * height);
  let value = 0;
  let index = 0; // column-major pixel index
  const total = width * height;
  for (const count of counts) {
    if (value === 1) {
      for (let k = 0; k < count && index + k < total; k++) {
        const p = index + k;
        const x = Math.floor(p / height);
        const y = p % height;
        mask[y * width + x] = 1;
      }
    }
    index += count;
    value = 1 - value;
  }
  return mask;
}

/**
 * An `ImageData` over `data` (`width * height * 4` RGBA bytes). Uses the
 * platform constructor where it exists; jsdom (vitest) has none, so the
 * structurally identical plain object stands in there.
 */
export function pixelsToImageData(
  data: Uint8ClampedArray<ArrayBuffer>,
  width: number,
  height: number
): ImageData {
  if (typeof ImageData !== 'undefined') {
    return new ImageData(data, width, height);
  }
  return { data, width, height, colorSpace: 'srgb' } as ImageData;
}

/**
 * Decode a device `mask_rle` (Canonical_RLE at source-frame resolution,
 * rfdetr-seg-support Requirement 10.2) into canvas pixels: `rgba` where the
 * mask is set, fully transparent elsewhere. The runs must be non-negative
 * integers summing to `width * height` (the dimensions of the loaded source
 * image); anything else throws a `RangeError`, as the device's decoder
 * rejects it, so the caller skips that instance and keeps its box.
 */
export function rleToImageData(
  rle: string | number[],
  width: number,
  height: number,
  rgba: Rgba
): ImageData {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 0 || height < 0) {
    throw new RangeError(`mask size must be non-negative integers, got ${width}x${height}`);
  }
  const counts = parseRleCounts(rle);
  if (counts.some((count) => !Number.isInteger(count) || count < 0)) {
    throw new RangeError('mask_rle run lengths must be non-negative integers');
  }
  const total = width * height;
  const sum = counts.reduce((acc, count) => acc + count, 0);
  if (sum !== total) {
    throw new RangeError(`mask_rle runs sum to ${sum}, expected ${width}x${height} = ${total}`);
  }
  const bits = decodeRleColumnMajor(counts, width, height);
  const data = new Uint8ClampedArray(total * 4);
  const [r, g, b, a] = rgba;
  for (let i = 0; i < total; i++) {
    if (bits[i]) {
      const offset = i * 4;
      data[offset] = r;
      data[offset + 1] = g;
      data[offset + 2] = b;
      data[offset + 3] = a;
    }
  }
  return pixelsToImageData(data, width, height);
}
