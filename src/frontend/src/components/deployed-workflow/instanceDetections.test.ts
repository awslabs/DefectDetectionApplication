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
 * rfdetr-seg-support Requirement 10.6, DD5: the HMI's Detection_List helpers.
 * `decodeMaskRle` is pinned to the shared Canonical_RLE vector file
 * (`test/fixtures/canonical_rle_vectors.json`) every encoder and decoder of
 * the format reproduces, so the HMI paints exactly the mask the device
 * encoded.
 */
import * as fs from "fs";
import * as path from "path";
import fc from "fast-check";
import {
  INSTANCE_PALETTE,
  decodeMaskRle,
  hasMask,
  instanceColor,
  maskToImageData,
  normalizeDetectionEntries,
} from "./instanceDetections";
import type { InstanceDetectionEntry } from "./instanceDetections";

interface Vector {
  name: string;
  width: number;
  height: number;
  mask_rows: string[];
  rle: string;
}

const VECTORS: Vector[] = JSON.parse(
  fs.readFileSync(
    path.resolve(__dirname, "../../../../../test/fixtures/canonical_rle_vectors.json"),
    "utf8",
  ),
).vectors;

function rowsToBits(rows: string[]): Uint8Array {
  return Uint8Array.from(rows.join("").split("").map((char) => (char === "1" ? 1 : 0)));
}

/** The reference encoder: column-major runs, background first. */
function encodeColumnMajor(bits: Uint8Array, width: number, height: number): string {
  const counts: number[] = [];
  let current = 0;
  let run = 0;
  for (let x = 0; x < width; x++) {
    for (let y = 0; y < height; y++) {
      const value = bits[y * width + x] ? 1 : 0;
      if (value === current) {
        run++;
      } else {
        counts.push(run);
        current = value;
        run = 1;
      }
    }
  }
  counts.push(run);
  return counts.join(" ");
}

const ENTRY: InstanceDetectionEntry = {
  class_index: "0",
  class_label: "scratch",
  bounding_box: [3, 2, 8, 5],
  confidence: 0.9,
  mask_rle: "27 4 8 4 8 4 8 4 125",
  mask_area: 16,
};

describe("decodeMaskRle", () => {
  it("decodes every shared Canonical_RLE vector to its mask", () => {
    expect(VECTORS.length).toBeGreaterThan(0);
    for (const vector of VECTORS) {
      const expected = rowsToBits(vector.mask_rows);
      expect(Array.from(decodeMaskRle(vector.rle, vector.width, vector.height))).toEqual(
        Array.from(expected),
      );
    }
  });

  it("round-trips any mask through the column-major encoding", () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 12 }),
        fc.integer({ min: 1, max: 12 }),
        fc.array(fc.boolean(), { minLength: 144, maxLength: 144 }),
        (width, height, cells) => {
          const bits = Uint8Array.from(cells.slice(0, width * height).map((cell) => (cell ? 1 : 0)));
          const rle = encodeColumnMajor(bits, width, height);
          expect(Array.from(decodeMaskRle(rle, width, height))).toEqual(Array.from(bits));
        },
      ),
      { numRuns: 100 },
    );
  });

  it("accepts an empty mask and a leading zero for a foreground first pixel", () => {
    expect(Array.from(decodeMaskRle("6", 3, 2))).toEqual([0, 0, 0, 0, 0, 0]);
    expect(Array.from(decodeMaskRle("0 6", 3, 2))).toEqual([1, 1, 1, 1, 1, 1]);
    expect(Array.from(decodeMaskRle("", 0, 0))).toEqual([]);
  });

  it("rejects runs that do not sum to the mask size or are not integers", () => {
    expect(() => decodeMaskRle("1 2 3", 4, 4)).toThrow(RangeError);
    expect(() => decodeMaskRle("1 2 x", 3, 1)).toThrow(RangeError);
    expect(() => decodeMaskRle("-1 7", 3, 2)).toThrow(RangeError);
    expect(() => decodeMaskRle("6", 2.5, 2)).toThrow(RangeError);
  });
});

describe("normalizeDetectionEntries", () => {
  it("orders a live map's values by ascending integer key", () => {
    const entries = normalizeDetectionEntries({
      "10": { ...ENTRY, class_label: "k10" },
      "2": { ...ENTRY, class_label: "k2" },
      "0": { ...ENTRY, class_label: "k0" },
    });
    expect(entries.map((entry) => entry.class_label)).toEqual(["k0", "k2", "k10"]);
  });

  it("keeps a list as it is and skips entries that are not objects", () => {
    const entries = normalizeDetectionEntries([ENTRY, null, "x", 3, { ...ENTRY, class_label: "b" }]);
    expect(entries.map((entry) => entry.class_label)).toEqual(["scratch", "b"]);
  });

  it("returns [] for null, undefined and scalars", () => {
    expect(normalizeDetectionEntries(null)).toEqual([]);
    expect(normalizeDetectionEntries(undefined)).toEqual([]);
    expect(normalizeDetectionEntries("text" as unknown as unknown[])).toEqual([]);
    expect(normalizeDetectionEntries({})).toEqual([]);
  });
});

describe("hasMask / instanceColor / maskToImageData", () => {
  it("hasMask is true only for a non-empty mask_rle string", () => {
    expect(hasMask(ENTRY)).toBe(true);
    expect(hasMask({ ...ENTRY, mask_rle: undefined })).toBe(false);
    expect(hasMask({ ...ENTRY, mask_rle: "" })).toBe(false);
    expect(hasMask({ ...ENTRY, mask_rle: 7 as unknown as string })).toBe(false);
  });

  it("instanceColor picks the palette row of the class index, wrapping", () => {
    expect(instanceColor({ ...ENTRY, class_index: "0" })).toEqual(INSTANCE_PALETTE[0]);
    expect(instanceColor({ ...ENTRY, class_index: "3" })).toEqual(INSTANCE_PALETTE[3]);
    expect(instanceColor({ ...ENTRY, class_index: String(INSTANCE_PALETTE.length + 1) })).toEqual(
      INSTANCE_PALETTE[1],
    );
    expect(instanceColor({ ...ENTRY, class_index: "nope" })).toEqual(INSTANCE_PALETTE[0]);
  });

  it("maskToImageData paints the colour where the mask is set and leaves the rest transparent", () => {
    const bits = Uint8Array.from([1, 0, 0, 1]);
    const pixels = maskToImageData(bits, 2, 2, [10, 20, 30, 102]);
    expect(pixels.width).toBe(2);
    expect(pixels.height).toBe(2);
    expect(Array.from(pixels.data)).toEqual([
      10, 20, 30, 102, 0, 0, 0, 0, 0, 0, 0, 0, 10, 20, 30, 102,
    ]);
  });
});
