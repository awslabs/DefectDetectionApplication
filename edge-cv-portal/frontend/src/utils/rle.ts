/**
 * Canonical_RLE codec (COCO-style column-major counts) shared by the
 * labeling AnnotationCanvas and the Portal results viewer
 * (rfdetr-seg-support DD20). The three functions moved here verbatim from
 * `components/labeling/AnnotationCanvas.tsx`, which re-exports them so its
 * callers and tests are unchanged; importing the canvas module into the
 * viewer would pull the whole labeling UI.
 *
 * Wire format: the backend's canonical form is the space-separated counts
 * string ("4 1 4") over the mask flattened column-major (down each column,
 * left to right), counts alternating starting with the zero-run (a leading
 * "0" when the first pixel is foreground); `sum(counts) == width * height`.
 */

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
