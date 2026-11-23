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
 * rfdetr-seg-support Requirement 10.6: the detected-objects table shows the
 * instance mask's pixel count as a `Mask area (px)` column, after the box and
 * before the association column, only when some row carries `maskArea`; a
 * detection run's table (no row with a mask) is today's four columns.
 */
import { render, screen } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import DetectedObjectsTable, { MASK_AREA_HEADER, VIOLATION_BADGE } from "./DetectedObjectsTable";
import type { RunDetection } from "../detections";

const FOUR_HEADERS = ["Index", "Object", "Confidence", "Bounding box (px)"];

const WITH_MASK: RunDetection[] = [
  { id: "a1", label: "scratch", confidence: 0.93, box: [412, 97, 500, 138], maskArea: 4821 },
  { id: "b2", label: "dent", confidence: 0.61, box: [10, 20, 40, 60] },
];

const WITHOUT_MASK: RunDetection[] = [
  { id: "a1", label: "scratch", confidence: 0.93, box: [412, 97, 500, 138] },
  { id: "b2", label: "dent", confidence: 0.61, box: [10, 20, 40, 60] },
];

function table(): ReturnType<ReturnType<typeof createWrapper>["findTable"]> {
  return createWrapper(document.body).findTable();
}

function headers(): string[] {
  return (table()?.findColumnHeaders() ?? []).map((header) => header.getElement().textContent ?? "");
}

function rows(): string[][] {
  return (table()?.findRows() ?? []).map((row) =>
    Array.from(row.getElement().querySelectorAll("td")).map((cell) => (cell.textContent ?? "").trim()),
  );
}

describe("the Mask area (px) column", () => {
  it("appears after the bounding box when some row carries maskArea, as an integer", () => {
    render(<DetectedObjectsTable detections={WITH_MASK} />);
    expect(headers()).toEqual([...FOUR_HEADERS, MASK_AREA_HEADER]);
    expect(rows().map((row) => row[4])).toEqual(["4821", "-"]);
    expect(rows()[0]).toEqual(["0", "scratch", "93.0%", "x 412–500, y 97–138", "4821"]);
  });

  it("rounds a fractional mask area to an integer pixel count", () => {
    render(
      <DetectedObjectsTable
        detections={[{ label: "scratch", confidence: 0.5, box: [0, 0, 1, 1], maskArea: 1234.6 }]}
      />,
    );
    expect(rows().map((row) => row[4])).toEqual(["1235"]);
  });

  it("is absent when no row carries maskArea (today's four columns)", () => {
    render(<DetectedObjectsTable detections={WITHOUT_MASK} />);
    expect(headers()).toEqual(FOUR_HEADERS);
    expect(screen.queryByText(MASK_AREA_HEADER)).toBeNull();
  });

  it("sits before the association column", () => {
    render(<DetectedObjectsTable detections={WITH_MASK} violatingIds={new Set(["b2"])} />);
    expect(headers()).toEqual([...FOUR_HEADERS, MASK_AREA_HEADER, "Association"]);
    expect(rows().map((row) => row[4])).toEqual(["4821", "-"]);
    expect(rows().map((row) => row[5])).toEqual(["-", VIOLATION_BADGE]);
  });

  it("sorts by mask area, keeping rows without one", () => {
    render(
      <DetectedObjectsTable
        detections={[
          { label: "a", confidence: 0.5, box: [0, 0, 1, 1], maskArea: 300 },
          { label: "b", confidence: 0.5, box: [0, 0, 1, 1], maskArea: 100 },
          { label: "c", confidence: 0.5, box: [0, 0, 1, 1] },
        ]}
      />,
    );
    const header = table()?.findColumnSortingArea(5);
    expect(header).not.toBeNull();
    header?.click();
    const sorted = rows().map((row) => row[1]);
    // Rows without a mask area sort as undefined; the two with one are ordered.
    expect(sorted.indexOf("b")).toBeLessThan(sorted.indexOf("a"));
    expect(sorted).toHaveLength(3);
  });
});
