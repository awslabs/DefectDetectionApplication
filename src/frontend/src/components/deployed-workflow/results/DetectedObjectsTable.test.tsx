/*
 * Component tests for the detected-objects table
 * (run-detection-visibility spec, Requirement 2).
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import createWrapper from "@cloudscape-design/components/test-utils/dom";

import DetectedObjectsTable, { NO_OBJECTS_MESSAGE } from "./DetectedObjectsTable";
import type { RunDetection } from "../detections";

/** The Detection_List recorded on jetson-thor1 for the PPE run (list order). */
const PPE_DETECTIONS: RunDetection[] = [
  { id: "7b693d25", label: "vest", confidence: 0.8004, box: [53.69, 279.11, 206.84, 520.49] },
  { id: "de604231", label: "helmet", confidence: 0.9354, box: [92.18, 201.91, 169.87, 255.16] },
  { id: "2e94ab70", label: "human", confidence: 0.8781, box: [34.77, 198.92, 229.54, 859.51] },
  { id: "0a583a31", label: "no-helmet", confidence: 0.6879, box: [297.77, 246.49, 364.27, 348.32] },
  { id: "3a27480e", label: "human", confidence: 0.9636 },
];

function tableRows(): string[][] {
  const table = createWrapper().findTable();
  if (!table) {
    throw new Error("table not rendered");
  }
  return table
    .findRows()
    .map((row) =>
      row.findAll("td").map((cell) => (cell.getElement().textContent ?? "").trim()),
    );
}

describe("DetectedObjectsTable", () => {
  it("lists every detection in Detection_List order with index, label, confidence and box (2.1, 2.3, 2.4)", () => {
    render(<DetectedObjectsTable detections={PPE_DETECTIONS} />);

    expect(tableRows()).toEqual([
      ["0", "vest", "80.0%", "x 54–207, y 279–520"],
      ["1", "helmet", "93.5%", "x 92–170, y 202–255"],
      ["2", "human", "87.8%", "x 35–230, y 199–860"],
      ["3", "no-helmet", "68.8%", "x 298–364, y 246–348"],
      // An entry without a complete box shows "-" (2.8).
      ["4", "human", "96.4%", "-"],
    ]);
  });

  it("shows the count and a per-label summary in the header (2.2)", () => {
    render(<DetectedObjectsTable detections={PPE_DETECTIONS} />);

    const header = screen.getByRole("heading", { name: /Objects detected/ });
    expect(header).toHaveTextContent("Objects detected");
    expect(header).toHaveTextContent("(5)");
    expect(
      screen.getByText("helmet 1 · human 2 · no-helmet 1 · vest 1"),
    ).toBeInTheDocument();
  });

  it("sorts by confidence, keeping each row's list index (2.3)", () => {
    render(<DetectedObjectsTable detections={PPE_DETECTIONS} />);

    const table = createWrapper().findTable();
    const confidenceHeader = table?.findColumnSortingArea(3);
    expect(confidenceHeader).toBeTruthy();
    userEvent.click(confidenceHeader!.getElement());

    const rows = tableRows();
    expect(rows.map((row) => row[2])).toEqual([
      "68.8%",
      "80.0%",
      "87.8%",
      "93.5%",
      "96.4%",
    ]);
    // The index stays attached to its detection.
    expect(rows.map((row) => row[0])).toEqual(["3", "0", "2", "1", "4"]);
  });

  it("states that nothing was detected for an empty list (2.5)", () => {
    render(<DetectedObjectsTable detections={[]} />);

    expect(screen.getByTestId("no-detected-objects")).toHaveTextContent(
      NO_OBJECTS_MESSAGE,
    );
    expect(
      screen.getByRole("heading", { name: /Objects detected/ }),
    ).toHaveTextContent("(0)");
  });
});
