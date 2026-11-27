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
 * car-assembly-check Requirement 7.1: one section per Layout Check node with
 * a PASS/FAIL badge, a row per detection of each class, the node status
 * detail, and the instances ignored outside the car outline.
 */
import { render, screen, within } from "@testing-library/react";
import LayoutSections, { IGNORED_HEADER } from "./LayoutSections";
import { readLayoutSections } from "../layoutVerdicts";

function detection(id: string | null, label: string, confidence: number, iou: number | null) {
  return { id, class_label: label, confidence, bounding_box: [1, 2, 3, 4], iou, outline_overlap: 0.9 };
}

const REGIONS = {
  rear_fender: [[0.1, 0.4], [0.5, 0.4], [0.5, 0.8]],
  roof: [[0.3, 0.1], [0.7, 0.1], [0.7, 0.35]],
};

function section(pieces: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  return {
    pass: false,
    pieces,
    flagged: [],
    ignored: [],
    unexpected_classes: [],
    expected_regions: REGIONS,
    frame: { width: 100, height: 80 },
    ...extra,
  };
}

const OK_REAR = { status: "ok", iou: 0.91, detections: [detection("r1", "rear_fender", 0.95, 0.91)] };
const OK_ROOF = { status: "ok", iou: 0.88, detections: [detection("t1", "roof", 0.93, 0.88)] };

function renderSections(layout: Record<string, unknown>, nodeStatus = {}): void {
  render(<LayoutSections sections={readLayoutSections({ layout })} nodeStatus={nodeStatus} />);
}

function rowsOf(nodeId: string): string[][] {
  const table = screen.getByTestId(`layout-pieces-${nodeId}`);
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent ?? ""));
}

describe("LayoutSections", () => {
  it("shows a passing check", () => {
    renderSections(
      { lay: section({ rear_fender: OK_REAR, roof: OK_ROOF }, { pass: true }) },
      { lay: { status: "success", detail: "PASS" } },
    );
    const container = screen.getByTestId("layout-section-lay");
    expect(within(container).getByText("Layout check: lay")).toBeInTheDocument();
    expect(within(container).getByText("PASS", { selector: "span" })).toBeInTheDocument();
    expect(rowsOf("lay")).toEqual([
      ["rear_fender", "OK", "0.91", "0.95", "r1"],
      ["roof", "OK", "0.88", "0.93", "t1"],
    ]);
    expect(screen.getByTestId("layout-detail-lay")).toHaveTextContent("PASS");
    expect(screen.queryByText(IGNORED_HEADER)).toBeNull();
  });

  it("shows a failing check with one misplaced piece", () => {
    renderSections(
      {
        lay: section(
          { rear_fender: { status: "misplaced", iou: 0.1201, detections: [detection("b07", "rear_fender", 0.95, 0.1201)] }, roof: OK_ROOF },
          { flagged: ["b07"] },
        ),
      },
      { lay: { status: "success", detail: "FAIL: rear_fender misplaced (IoU 0.12)" } },
    );
    expect(screen.getByText("FAIL")).toBeInTheDocument();
    expect(rowsOf("lay")[0]).toEqual(["rear_fender", "Misplaced", "0.12", "0.95", "b07"]);
    expect(screen.getByTestId("layout-detail-lay")).toHaveTextContent("FAIL: rear_fender misplaced (IoU 0.12)");
  });

  it("shows a missing piece as one row without a detection", () => {
    renderSections({ lay: section({ rear_fender: OK_REAR, roof: { status: "missing", iou: null, detections: [] } }) });
    expect(rowsOf("lay")[1]).toEqual(["roof", "Missing", "—", "—", "—"]);
  });

  it("shows each detection of a duplicate piece", () => {
    renderSections({
      lay: section({
        rear_fender: OK_REAR,
        roof: {
          status: "duplicate",
          iou: 0.9,
          detections: [detection("t1", "roof", 0.93, 0.9), detection("t2", "roof", 0.81, 0.4)],
        },
      }),
    });
    expect(rowsOf("lay").slice(1)).toEqual([
      ["roof", "Duplicate", "0.90", "0.93", "t1"],
      ["roof", "Duplicate", "0.40", "0.81", "t2"],
    ]);
  });

  it("shows a warning detail as a warning", () => {
    renderSections(
      { lay: section({ rear_fender: OK_REAR, roof: OK_ROOF }) },
      { lay: { status: "warning", detail: "unexpected classes: wheel" } },
    );
    expect(screen.getByText("unexpected classes: wheel")).toBeInTheDocument();
    expect(screen.queryByTestId("layout-detail-lay")).toBeNull();
  });

  it("renders nothing without a section", () => {
    const { container } = render(<LayoutSections sections={readLayoutSections({})} />);
    expect(container.querySelector("[data-testid^='layout-section-']")).toBeNull();
  });

  it("lists an ignored instance with the verdict unchanged", () => {
    renderSections(
      {
        lay: section(
          { rear_fender: OK_REAR, roof: OK_ROOF },
          {
            pass: true,
            ignored: [{ id: "5c21", class_label: "rear_fender", confidence: 0.81, bounding_box: [0, 10, 5, 20], outline_overlap: 0.0312 }],
          },
        ),
      },
      { lay: { status: "success", detail: "PASS; ignored outside the car outline: rear_fender (1)" } },
    );
    expect(screen.getByText("PASS", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText(IGNORED_HEADER)).toBeInTheDocument();
    const table = screen.getByTestId("layout-ignored-lay");
    const [row] = within(table).getAllByRole("row").slice(1);
    expect(within(row).getAllByRole("cell").map((cell) => cell.textContent)).toEqual([
      "rear_fender",
      "0.81",
      "5c21",
      "0.03",
    ]);
  });
});
