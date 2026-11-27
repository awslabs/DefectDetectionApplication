/*
 *  Copyright 2025 Amazon Web Services, Inc.
 *
 *  Licensed under the Apache License, Version 2.0 (the "License");
 *  you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *  See the License for the specific language governing permissions and
 *  limitations under the License.
 */
/**
 * car-assembly-check Requirement 7.1: the defensive reader of
 * `layout.<nodeId>` and the instance key that matches a layout detection to
 * a mask-layer entry.
 */
import {
  failedPieceOutlines,
  flaggedInstanceKeys,
  ignoredInstanceKeys,
  instanceKey,
  readLayoutSections,
} from "./layoutVerdicts";

/** The Data-contracts example of the design. */
const FAILING_SECTION = {
  pass: false,
  pieces: {
    front_fender: {
      status: "ok",
      iou: 0.9123,
      detections: [
        { id: "3f9a1c2e", class_label: "front_fender", confidence: 0.97, bounding_box: [2290.0, 1205.0, 3980.0, 2870.0], iou: 0.9123, outline_overlap: 0.9871 },
      ],
    },
    rear_fender: {
      status: "misplaced",
      iou: 0.1201,
      detections: [
        { id: "b07d44aa", class_label: "rear_fender", confidence: 0.95, bounding_box: [1180.0, 1690.0, 2690.0, 3250.0], iou: 0.1201, outline_overlap: 0.6402 },
      ],
    },
    roof: { status: "missing", iou: null, detections: [] },
  },
  flagged: ["b07d44aa"],
  ignored: [
    { id: "5c21e0f7", class_label: "rear_fender", confidence: 0.81, bounding_box: [0.0, 1410.0, 205.0, 2240.0], outline_overlap: 0.0 },
  ],
  unexpected_classes: [],
  expected_regions: {
    front_fender: [[0.5, 0.4], [0.9, 0.4], [0.9, 0.8], [0.5, 0.8]],
    rear_fender: [[0.1, 0.4], [0.5, 0.4], [0.5, 0.8], [0.1, 0.8]],
    roof: [[0.3, 0.1], [0.7, 0.1], [0.7, 0.35]],
  },
  frame: { width: 4608, height: 3288 },
  min_iou: 0.7,
  min_confidence: 0.5,
  min_outline_overlap: 0.1,
};

describe("readLayoutSections", () => {
  it("reads the Data-contracts shape", () => {
    const [section] = readLayoutSections({ layout: { check: FAILING_SECTION } });
    expect(section.nodeId).toBe("check");
    expect(section.pass).toBe(false);
    expect(section.pieces.map((piece) => [piece.label, piece.status, piece.iou])).toEqual([
      ["front_fender", "ok", 0.9123],
      ["rear_fender", "misplaced", 0.1201],
      ["roof", "missing", null],
    ]);
    expect(section.pieces[1].detections).toEqual([
      {
        id: "b07d44aa",
        classLabel: "rear_fender",
        confidence: 0.95,
        box: [1180, 1690, 2690, 3250],
        iou: 0.1201,
        outlineOverlap: 0.6402,
      },
    ]);
    expect(section.flagged).toEqual(["b07d44aa"]);
    expect(section.ignored).toEqual([
      { id: "5c21e0f7", classLabel: "rear_fender", confidence: 0.81, box: [0, 1410, 205, 2240], outlineOverlap: 0 },
    ]);
    expect(section.unexpectedClasses).toEqual([]);
    expect(Object.keys(section.expectedRegions)).toEqual(["front_fender", "rear_fender", "roof"]);
    expect(section.frame).toEqual({ width: 4608, height: 3288 });
  });

  it("keeps the metadata's key order", () => {
    const sections = readLayoutSections({
      layout: { zeta: { ...FAILING_SECTION, pass: true }, alpha: FAILING_SECTION },
    });
    expect(sections.map((section) => section.nodeId)).toEqual(["zeta", "alpha"]);
  });

  it("skips malformed values and never throws", () => {
    expect(readLayoutSections(undefined)).toEqual([]);
    expect(readLayoutSections(null)).toEqual([]);
    expect(readLayoutSections({ layout: [] })).toEqual([]);
    expect(readLayoutSections({ layout: "x" })).toEqual([]);
    const sections = readLayoutSections({
      layout: {
        noPass: { ...FAILING_SECTION, pass: "false" },
        notObject: 3,
        partial: {
          pass: false,
          pieces: {
            good: { status: "missing", iou: null, detections: [] },
            badStatus: { status: "unknown", iou: 1, detections: [] },
            notObject: [],
            mixed: {
              status: "duplicate",
              iou: "x",
              detections: [
                { id: null, class_label: "mixed", confidence: 0.6, bounding_box: [1, 2, 3, 4], iou: null, outline_overlap: null },
                { id: "a", class_label: "mixed", confidence: 0.6, bounding_box: [1, 2, 3] },
                "nope",
              ],
            },
          },
          flagged: ["a", 4, ""],
          ignored: [{ id: "i", class_label: "x" }, null],
          unexpected_classes: "wheel",
          expected_regions: { short: [[0, 0], [1, 1]], bad: [[0, 0], [1, "1"], [1, 0]], ok: [[0, 0], [1, 0], [1, 1]] },
          frame: { width: 0, height: 10 },
        },
      },
    });
    expect(sections).toHaveLength(1);
    const [section] = sections;
    expect(section.nodeId).toBe("partial");
    expect(section.pieces.map((piece) => piece.label)).toEqual(["good", "mixed"]);
    expect(section.pieces[1].iou).toBeNull();
    expect(section.pieces[1].detections).toEqual([
      { id: null, classLabel: "mixed", confidence: 0.6, box: [1, 2, 3, 4], iou: null, outlineOverlap: null },
    ]);
    expect(section.flagged).toEqual(["a"]);
    expect(section.ignored).toEqual([]);
    expect(section.unexpectedClasses).toEqual([]);
    expect(section.expectedRegions).toEqual({ ok: [[0, 0], [1, 0], [1, 1]] });
    expect(section.frame).toBeNull();
  });
});

describe("instanceKey", () => {
  it("joins the class and the box with one decimal", () => {
    expect(instanceKey({ classLabel: "roof", box: [1, 2.25, 3.04, 4.96] })).toBe("roof|1.0|2.3|3.0|5.0");
  });
});

describe("mask-layer inputs", () => {
  const sections = readLayoutSections({ layout: { check: FAILING_SECTION } });

  it("keys the flagged detections of a failing section", () => {
    expect(Array.from(flaggedInstanceKeys(sections))).toEqual([
      "rear_fender|1180.0|1690.0|2690.0|3250.0",
    ]);
    const passing = readLayoutSections({ layout: { check: { ...FAILING_SECTION, pass: true } } });
    expect(flaggedInstanceKeys(passing).size).toBe(0);
  });

  it("keys the ignored instances whatever the verdict", () => {
    const passing = readLayoutSections({ layout: { check: { ...FAILING_SECTION, pass: true } } });
    expect(Array.from(ignoredInstanceKeys(passing))).toEqual(["rear_fender|0.0|1410.0|205.0|2240.0"]);
  });

  it("outlines every piece that is not ok, in frame pixels", () => {
    const outlines = failedPieceOutlines(sections);
    expect(outlines.map((outline) => outline.label)).toEqual(["rear_fender", "roof"]);
    expect(outlines[1].points[0][0]).toBeCloseTo(0.3 * 4608);
    expect(outlines[1].points[0][1]).toBeCloseTo(0.1 * 3288);
    const noFrame = readLayoutSections({ layout: { check: { ...FAILING_SECTION, frame: null } } });
    expect(failedPieceOutlines(noFrame)).toEqual([]);
  });
});
