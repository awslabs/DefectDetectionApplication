/*
 * Property test for the output-node preview card
 * (output-node-preview-popover spec, task 4.3).
 *
 * Covers design Property 4: the "View full results" link is present in every
 * preview state, pointing at the Run_Results_Page for the current
 * registration and execution ids.
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import fc from "fast-check";

import NodePreviewCard, { NO_DETECTIONS_MESSAGE } from "./NodePreviewCard";
import type { PreviewViewModel } from "./previewModel";
import type { RunDetection } from "../detections";

const NUM_RUNS = 100;

/** URL-safe id segments so the asserted href is the literal route string. */
const idArb = fc
  .stringOf(
    fc.constantFrom(
      ..."abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-".split(
        "",
      ),
    ),
    { minLength: 1, maxLength: 16 },
  )
  .filter((s) => s !== "-");

/** Every non-"none" preview view-model kind an Output_Node can produce. */
const viewModelArb: fc.Arbitrary<PreviewViewModel> = fc.oneof(
  fc.constant<PreviewViewModel>({ kind: "pending" }),
  fc.constant<PreviewViewModel>({ kind: "loading" }),
  fc.constant<PreviewViewModel>({ kind: "unavailable" }),
  fc
    .option(fc.string(), { nil: undefined })
    .map(
      (detail): PreviewViewModel =>
        detail !== undefined ? { kind: "failure", detail } : { kind: "failure" },
    ),
  fc
    .string({ minLength: 1 })
    .map((src): PreviewViewModel => ({ kind: "image", src })),
  fc.string().map((text): PreviewViewModel => ({ kind: "text", text })),
  fc
    .array(fc.tuple(fc.string({ minLength: 1 }), fc.string()), { maxLength: 4 })
    .map((fields): PreviewViewModel => ({ kind: "fields", fields })),
  fc
    .tuple(
      fc.constantFrom("success", "warning"),
      fc.option(fc.string(), { nil: undefined }),
    )
    .map(
      ([status, detail]): PreviewViewModel =>
        detail !== undefined
          ? { kind: "status", status, detail }
          : { kind: "status", status },
    ),
  // run-detection-visibility: the model_inference detections preview.
  fc
    .record({
      detections: fc.array(
        fc.record({
          label: fc.string({ minLength: 1 }),
          confidence: fc.double({ min: 0, max: 1, noNaN: true }),
        }),
        { maxLength: 15 },
      ),
      imageSrc: fc.option(fc.string({ minLength: 1 }), { nil: undefined }),
    })
    .map(
      ({ detections, imageSrc }): PreviewViewModel =>
        imageSrc !== undefined
          ? { kind: "detections", detections, imageSrc }
          : { kind: "detections", detections },
    ),
);

describe("Property 4: Results link is present in every preview state", () => {
  // **Validates: Requirements 1.3, 3.5**
  it("renders the 'View full results' link with the Run_Results_Page href for every view-model kind", () => {
    fc.assert(
      fc.property(
        viewModelArb,
        idArb,
        idArb,
        idArb,
        (viewModel, registrationId, executionId, nodeId) => {
          render(
            <MemoryRouter>
              <NodePreviewCard
                nodeId={nodeId}
                registrationId={registrationId}
                executionId={executionId}
                viewModel={viewModel}
              />
            </MemoryRouter>,
          );
          try {
            const link = screen.getByRole("link", {
              name: "View full results",
            });
            expect(link).toHaveAttribute(
              "href",
              `/deployed-workflows/${registrationId}/executions/${executionId}/results`,
            );
          } finally {
            cleanup();
          }
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });
});

// --------------------------------------------------------------------------
// run-detection-visibility: rendering of the "detections" kind (R3.2, R3.3)
// --------------------------------------------------------------------------

function renderCard(viewModel: PreviewViewModel): void {
  render(
    <MemoryRouter>
      <NodePreviewCard
        nodeId="model_1"
        registrationId="reg-1"
        executionId="exec-1"
        viewModel={viewModel}
      />
    </MemoryRouter>,
  );
}

function detections(count: number): RunDetection[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `id-${index}`,
    label: `object-${index}`,
    confidence: 0.5 + index / 100,
  }));
}

describe("NodePreviewCard detections preview", () => {
  it("lists every detection as label and confidence when there are at most 10", () => {
    renderCard({
      kind: "detections",
      detections: [
        { label: "helmet", confidence: 0.9354003071784973 },
        { label: "no-helmet", confidence: 0.6878951787948608 },
      ],
    });

    const list = screen.getByTestId("preview-detections");
    expect(list).toHaveTextContent("Objects detected in this run (2)");
    const items = screen.getAllByRole("listitem").map((item) => item.textContent);
    expect(items).toEqual(["helmet — 93.5%", "no-helmet — 68.8%"]);
    expect(screen.queryByTestId("preview-detections-more")).toBeNull();
    // No overlay image in the view-model -> no thumbnail.
    expect(screen.queryByTestId("preview-overlay-thumbnail")).toBeNull();
  });

  it("lists the first 10 and states how many more the run found", () => {
    renderCard({ kind: "detections", detections: detections(13) });

    expect(screen.getByTestId("preview-detections")).toHaveTextContent(
      "Objects detected in this run (13)",
    );
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(10);
    expect(items[0]).toHaveTextContent("object-0 — 50.0%");
    expect(items[9]).toHaveTextContent("object-9 — 59.0%");
    expect(screen.getByTestId("preview-detections-more")).toHaveTextContent(
      "and 3 more",
    );
  });

  it("states that nothing was detected for an empty list", () => {
    renderCard({ kind: "detections", detections: [] });

    expect(screen.getByTestId("preview-detections")).toHaveTextContent(
      NO_DETECTIONS_MESSAGE,
    );
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });

  it("shows the overlay thumbnail, and hides only the thumbnail when it fails to load", () => {
    renderCard({
      kind: "detections",
      detections: detections(1),
      imageSrc: "/overlay-image/exec-1",
    });

    const thumbnail = screen.getByTestId("preview-overlay-thumbnail");
    expect(thumbnail).toHaveAttribute("src", "/overlay-image/exec-1");

    fireEvent.error(thumbnail);

    expect(screen.queryByTestId("preview-overlay-thumbnail")).toBeNull();
    expect(screen.getByTestId("preview-detections")).toHaveTextContent(
      "object-0 — 50.0%",
    );
    // The results link stays (R3.6).
    expect(
      screen.getByRole("link", { name: "View full results" }),
    ).toBeInTheDocument();
  });
});
