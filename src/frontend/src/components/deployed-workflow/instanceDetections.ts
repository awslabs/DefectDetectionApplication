/*
 *
 * Copyright 2025 Amazon Web Services, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */
/**
 * Pure helpers for an instance-segmentation Detection_List (rfdetr-seg-support
 * Requirement 10.6, DD5, DD14).
 *
 * The device carries the list in two shapes: the live result's map
 * (`inferenceResult.detections = {"0": {...}, "1": {...}}`) and the stored /
 * run-preview list (`detections: [...]`, `/overlay` `instances`). An instance
 * entry adds `mask_rle` (a Canonical_RLE string: space-separated run lengths
 * over the column-major flattening of the source-resolution binary mask, the
 * first run counting background pixels), `mask_area` and, when the device's
 * payload ceiling dropped the mask, `mask_truncated`. Everything here reads
 * those values defensively; only `decodeMaskRle` throws, on a string that is
 * not a mask of the given size.
 */

/** One Detection_List entry as the device stores and serves it. */
export interface InstanceDetectionEntry {
  class_index: string;
  class_label: string;
  /** `[x_min, y_min, x_max, y_max]` in source-frame pixels. */
  bounding_box: number[];
  confidence: number;
  /** Canonical_RLE of the instance mask at source resolution, when kept. */
  mask_rle?: string;
  /** The mask's foreground pixel count. */
  mask_area?: number;
  /** True when the device shed this entry's `mask_rle` at its size ceiling. */
  mask_truncated?: boolean;
}

/** An RGBA colour, each channel 0..255. */
export type Rgba = [number, number, number, number];

/** The pixels of an `ImageData` (`new ImageData(data, width, height)`). */
export interface MaskPixels {
  data: Uint8ClampedArray;
  width: number;
  height: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** The order of the live map's keys: numeric ascending, then the rest. */
function compareMapKeys(a: string, b: string): number {
  const numericA = /^\d+$/.test(a);
  const numericB = /^\d+$/.test(b);
  if (numericA && numericB) {
    return Number(a) - Number(b);
  }
  if (numericA !== numericB) {
    return numericA ? -1 : 1;
  }
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * The Detection_List entries of either shape: a map's values in ascending
 * key order (`"0"`, `"1"`, ... as the device writes them), a list as it is,
 * and `[]` for anything else. Entries that are not objects are skipped.
 */
export function normalizeDetectionEntries(
  source: Record<string, unknown> | unknown[] | null | undefined,
): InstanceDetectionEntry[] {
  let values: unknown[];
  if (Array.isArray(source)) {
    values = source;
  } else if (isRecord(source)) {
    values = Object.keys(source)
      .sort(compareMapKeys)
      .map((key) => source[key]);
  } else {
    return [];
  }
  return values.filter(isRecord) as unknown as InstanceDetectionEntry[];
}

/** Whether the entry still carries a decodable mask string. */
export function hasMask(entry: InstanceDetectionEntry): boolean {
  return typeof entry.mask_rle === "string" && entry.mask_rle.length > 0;
}

/**
 * Decode a Canonical_RLE string into a row-major `width × height` binary
 * mask (1 = foreground). The runs alternate background / foreground over the
 * column-major flattening, background first (a leading `0` when the first
 * pixel is foreground), and must sum to `width × height`; anything else
 * throws a `RangeError`, as the device's decoder rejects it.
 */
export function decodeMaskRle(rle: string, width: number, height: number): Uint8Array {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 0 || height < 0) {
    throw new RangeError(`mask size must be non-negative integers, got ${width}x${height}`);
  }
  const total = width * height;
  const tokens = rle.trim().length > 0 ? rle.trim().split(/\s+/) : [];
  const counts = tokens.map((token) => {
    if (!/^\d+$/.test(token)) {
      throw new RangeError(`mask_rle run length is not a non-negative integer: ${token}`);
    }
    return Number(token);
  });
  const sum = counts.reduce((acc, count) => acc + count, 0);
  if (sum !== total) {
    throw new RangeError(`mask_rle runs sum to ${sum}, expected ${width}x${height} = ${total}`);
  }
  const mask = new Uint8Array(total);
  let index = 0; // column-major pixel index
  let value = 0;
  for (const count of counts) {
    if (value === 1) {
      for (let k = 0; k < count; k++) {
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
 * RGBA pixels for a decoded mask: `rgba` where the mask is set, fully
 * transparent elsewhere. `bits` is row-major, `width × height` long.
 */
export function maskToImageData(
  bits: Uint8Array,
  width: number,
  height: number,
  rgba: Rgba,
): MaskPixels {
  const data = new Uint8ClampedArray(width * height * 4);
  const [r, g, b, a] = rgba;
  for (let i = 0, n = Math.min(bits.length, width * height); i < n; i++) {
    if (bits[i]) {
      const offset = i * 4;
      data[offset] = r;
      data[offset + 1] = g;
      data[offset + 2] = b;
      data[offset + 3] = a;
    }
  }
  return { data, width, height };
}

/**
 * A fixed palette for the instance layer, one colour per class index
 * (wrapping), so instances of the same class share a colour.
 */
export const INSTANCE_PALETTE: ReadonlyArray<[number, number, number]> = [
  [230, 25, 75],
  [60, 180, 75],
  [255, 225, 25],
  [0, 130, 200],
  [245, 130, 48],
  [145, 30, 180],
  [70, 240, 240],
  [240, 50, 230],
  [210, 245, 60],
  [250, 190, 212],
  [0, 128, 128],
  [220, 190, 255],
];

/** The layer colour of an entry's class, from its `class_index`. */
export function instanceColor(entry: InstanceDetectionEntry): [number, number, number] {
  const index = Number.parseInt(String(entry.class_index), 10);
  const safe = Number.isFinite(index) && index >= 0 ? index : 0;
  return INSTANCE_PALETTE[safe % INSTANCE_PALETTE.length];
}
