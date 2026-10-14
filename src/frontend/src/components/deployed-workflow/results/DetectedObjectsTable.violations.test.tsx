/*
 * Violating detections in the detected-objects table
 * (rtsp-rtmp-stream-cameras Requirement 16.3): the Detection_IDs an object
 * association found in violation are badged, and without any the table is
 * unchanged (DetectedObjectsTable.test.tsx pins that layout).
 */
import { render, screen } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import type { RunDetection } from "../detections";
import DetectedObjectsTable, { VIOLATION_BADGE } from "./DetectedObjectsTable";

const DETECTIONS: RunDetection[] = [
  { id: "p1", label: "person", confidence: 0.91, box: [10, 10, 110, 310] },
  { id: "p2", label: "person", confidence: 0.88, box: [200, 12, 300, 320] },
  { id: "h1", label: "hardhat", confidence: 0.8, box: [20, 5, 90, 40] },
  { label: "person", confidence: 0.6 },
];

function table(): ReturnType<ReturnType<typeof createWrapper>["findTable"]> {
  return createWrapper(document.body).findTable();
}

function headers(): string[] {
  return (table()?.findColumnHeaders() ?? []).map((header) => header.getElement().textContent ?? "");
}

function rows(): string[][] {
  return (table()?.findRows() ?? []).map((row) =>
    Array.from(row.getElement().querySelectorAll("td")).map((cell) => cell.textContent ?? ""),
  );
}

describe("violating detections", () => {
  it("badges the rows an association found in violation", () => {
    render(<DetectedObjectsTable detections={DETECTIONS} violatingIds={new Set(["p2"])} />);
    expect(headers()).toEqual(["Index", "Object", "Confidence", "Bounding box (px)", "Association"]);
    expect(rows().map((row) => row[4])).toEqual(["-", VIOLATION_BADGE, "-", "-"]);
    expect(screen.getByText(/1 in violation/)).toBeInTheDocument();
  });

  it("never badges a detection without an id", () => {
    render(<DetectedObjectsTable detections={DETECTIONS} violatingIds={new Set(["p1", "h1"])} />);
    expect(rows().map((row) => row[4])).toEqual([VIOLATION_BADGE, "-", VIOLATION_BADGE, "-"]);
    expect(screen.getByText(/2 in violation/)).toBeInTheDocument();
  });

  it.each([undefined, new Set<string>()])("leaves the table unchanged without violations (%p)", (violatingIds) => {
    render(<DetectedObjectsTable detections={DETECTIONS} violatingIds={violatingIds} />);
    expect(headers()).toEqual(["Index", "Object", "Confidence", "Bounding box (px)"]);
    expect(screen.queryByText(/in violation/)).toBeNull();
  });
});
