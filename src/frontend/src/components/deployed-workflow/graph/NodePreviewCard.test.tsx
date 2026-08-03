/*
 * Property test for the output-node preview card
 * (output-node-preview-popover spec, task 4.3).
 *
 * Covers design Property 4: the "View full results" link is present in every
 * preview state, pointing at the Run_Results_Page for the current
 * registration and execution ids.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import fc from "fast-check";

import NodePreviewCard from "./NodePreviewCard";
import type { PreviewViewModel } from "./previewModel";

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
