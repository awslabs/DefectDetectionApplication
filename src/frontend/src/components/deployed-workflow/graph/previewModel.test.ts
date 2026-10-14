/*
 * Property tests for the output-node preview view-model
 * (output-node-preview-popover spec, tasks 3.3–3.5).
 *
 * Covers design Properties 1–3: output-node classification gating,
 * preview state precedence, and snippet truncation.
 */

import fc from "fast-check";
import type { NodeRunStatus } from "api/WorkflowRegistrationAPI";
import { runDetections } from "../detections";
import {
  OUTPUT_NODE_TYPES,
  SNIPPET_MAX_LENGTH,
  isOutputNode,
  previewViewModel,
  snippet,
} from "./previewModel";

const NUM_RUNS = 100;

const OUTPUT_TYPES = Array.from(OUTPUT_NODE_TYPES);
const PUBLISH_TYPES = ["mqtt_publish", "opcua_write", "digital_output"];
const METADATA_TYPES = ["llm_inference", "bedrock_inference"];

/** Baseline args for previewViewModel; individual tests override fields. */
function baseArgs(nodeType: string) {
  return {
    nodeType,
    nodeId: "node-1",
    hasImageResults: true,
    imageSrc: "http://device/executions/e1/output-image",
    metadataLoading: false,
    metadataError: false,
  };
}

/** Arbitrary node-status entry with any recognized-or-not status string. */
const statusEntryArb: fc.Arbitrary<NodeRunStatus> = fc.record(
  {
    status: fc.oneof(
      fc.constantFrom("pending", "running", "success", "warning", "failure"),
      fc.string(),
    ),
    detail: fc.string(),
  },
  { requiredKeys: ["status"] },
);

/** Arbitrary metadata object (may or may not contain relevant entries). */
const metadataArb = fc.option(
  fc.dictionary(fc.string(), fc.jsonValue()) as fc.Arbitrary<
    Record<string, unknown>
  >,
  { nil: undefined },
);

describe("Property 1: Output-node classification gates the preview", () => {
  // **Validates: Requirements 1.1, 1.4**
  it("returns kind 'none' exactly when the type is not in OUTPUT_NODE_TYPES", () => {
    fc.assert(
      fc.property(
        fc.oneof(fc.string(), fc.constantFrom(...OUTPUT_TYPES)),
        fc.option(statusEntryArb, { nil: undefined }),
        (nodeType, statusEntry) => {
          const vm = previewViewModel({ ...baseArgs(nodeType), statusEntry });
          if (OUTPUT_NODE_TYPES.has(nodeType)) {
            expect(vm.kind).not.toBe("none");
            expect(isOutputNode(nodeType)).toBe(true);
          } else {
            expect(vm.kind).toBe("none");
            expect(isOutputNode(nodeType)).toBe(false);
          }
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  it("returns a non-'none' view-model for every output type", () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...OUTPUT_TYPES),
        fc.option(statusEntryArb, { nil: undefined }),
        (nodeType, statusEntry) => {
          const vm = previewViewModel({ ...baseArgs(nodeType), statusEntry });
          expect(vm.kind).not.toBe("none");
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });
});

describe("Property 2: Preview state precedence", () => {
  // **Validates: Requirements 2.4, 3.1, 3.2, 3.3**
  it("missing or non-terminal status yields 'pending' regardless of data availability", () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...OUTPUT_TYPES),
        fc.option(
          fc.record(
            {
              status: fc.oneof(
                fc.constantFrom("pending", "running"),
                // Unrecognized statuses are also non-terminal.
                fc
                  .string()
                  .filter(
                    (s) =>
                      s !== "success" && s !== "warning" && s !== "failure",
                  ),
              ),
              detail: fc.string(),
            },
            { requiredKeys: ["status"] },
          ),
          { nil: undefined },
        ),
        fc.boolean(),
        metadataArb,
        fc.boolean(),
        fc.boolean(),
        (
          nodeType,
          statusEntry,
          hasImageResults,
          metadata,
          metadataLoading,
          metadataError,
        ) => {
          const vm = previewViewModel({
            ...baseArgs(nodeType),
            statusEntry,
            hasImageResults,
            metadata,
            metadataLoading,
            metadataError,
          });
          expect(vm.kind).toBe("pending");
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  it("a failure status yields 'failure' carrying the entry's detail", () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...OUTPUT_TYPES),
        fc.option(fc.string(), { nil: undefined }),
        metadataArb,
        fc.boolean(),
        (nodeType, detail, metadata, hasImageResults) => {
          const statusEntry: NodeRunStatus =
            detail !== undefined
              ? { status: "failure", detail }
              : { status: "failure" };
          const vm = previewViewModel({
            ...baseArgs(nodeType),
            statusEntry,
            hasImageResults,
            metadata,
          });
          expect(vm.kind).toBe("failure");
          if (vm.kind === "failure") {
            expect(vm.detail).toBe(detail);
          }
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  it("terminal success/warning with the data source unavailable yields 'unavailable'", () => {
    fc.assert(
      fc.property(
        fc.constantFrom("capture", ...METADATA_TYPES),
        fc.constantFrom("success", "warning"),
        fc.boolean(),
        (nodeType, status, metadataError) => {
          // Make the type's data source unavailable:
          // - capture: no image results
          // - llm/bedrock: metadata without the node's entry, or an errored
          //   request (metadataError)
          const vm = previewViewModel({
            ...baseArgs(nodeType),
            statusEntry: { status },
            hasImageResults: false,
            metadata: metadataError ? undefined : {},
            metadataError: nodeType === "capture" ? false : metadataError,
          });
          expect(vm.kind).toBe("unavailable");
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  it("publish types with a terminal status always yield 'status' carrying status and detail", () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...PUBLISH_TYPES),
        fc.constantFrom("success", "warning"),
        fc.option(fc.string(), { nil: undefined }),
        metadataArb,
        fc.boolean(),
        (nodeType, status, detail, metadata, hasImageResults) => {
          const statusEntry: NodeRunStatus =
            detail !== undefined ? { status, detail } : { status };
          const vm = previewViewModel({
            ...baseArgs(nodeType),
            statusEntry,
            hasImageResults,
            metadata,
          });
          expect(vm.kind).toBe("status");
          if (vm.kind === "status") {
            expect(vm.status).toBe(status);
            expect(vm.detail).toBe(detail);
          }
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });
});

describe("Property 3: Snippet truncation", () => {
  // **Validates: Requirements 2.2, 2.5**
  it("returns the string unchanged when at most 280 characters", () => {
    fc.assert(
      fc.property(
        fc.string({ maxLength: SNIPPET_MAX_LENGTH }),
        (text) => {
          expect(snippet(text)).toBe(text);
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  it("returns exactly the first 280 characters plus an ellipsis when longer", () => {
    fc.assert(
      fc.property(
        fc.string({
          minLength: SNIPPET_MAX_LENGTH + 1,
          maxLength: SNIPPET_MAX_LENGTH * 3,
        }),
        (text) => {
          const out = snippet(text);
          expect(out).toBe(`${text.slice(0, SNIPPET_MAX_LENGTH)}…`);
          expect(out.length).toBe(SNIPPET_MAX_LENGTH + 1);
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  it("the returned text (minus any ellipsis) is always a prefix of the input", () => {
    fc.assert(
      fc.property(fc.string({ maxLength: SNIPPET_MAX_LENGTH * 3 }), (text) => {
        const out = snippet(text);
        const body = out.endsWith("…") ? out.slice(0, -1) : out;
        expect(text.startsWith(body)).toBe(true);
      }),
      { numRuns: NUM_RUNS },
    );
  });
});

// --------------------------------------------------------------------------
// run-detection-visibility: the model_inference preview
// --------------------------------------------------------------------------

/** A metadata object carrying a Detection_List of arbitrary entries. */
const detectionMetadataArb: fc.Arbitrary<Record<string, unknown>> = fc.record(
  {
    detections: fc.array(
      fc.oneof(
        fc.record({
          id: fc.hexaString({ minLength: 8, maxLength: 8 }),
          label: fc.string(),
          confidence: fc.double({ min: 0, max: 1, noNaN: true }),
          x_min: fc.double({ noNaN: true, noDefaultInfinity: true }),
          y_min: fc.double({ noNaN: true, noDefaultInfinity: true }),
          x_max: fc.double({ noNaN: true, noDefaultInfinity: true }),
          y_max: fc.double({ noNaN: true, noDefaultInfinity: true }),
        }),
        fc.jsonValue(),
      ),
      { maxLength: 12 },
    ),
    is_anomalous: fc.constantFrom(0, 1),
    confidence: fc.double({ min: 0, max: 1, noNaN: true }),
  },
  { requiredKeys: ["detections"] },
);

describe("Property 4 (run-detection-visibility): model_inference preview precedence", () => {
  // **Validates: Requirements 3.2, 3.4, 3.5**
  it("model_inference is a previewable node type", () => {
    expect(isOutputNode("model_inference")).toBe(true);
  });

  it("a missing or non-terminal status yields 'pending'; failure yields 'failure'", () => {
    fc.assert(
      fc.property(
        fc.option(fc.constantFrom("pending", "running"), { nil: undefined }),
        fc.option(fc.string(), { nil: undefined }),
        metadataArb,
        (status, detail, metadata) => {
          const pending = previewViewModel({
            ...baseArgs("model_inference"),
            statusEntry: status === undefined ? undefined : { status },
            metadata,
            overlayImageSrc: "/overlay-image/e1",
          });
          expect(pending.kind).toBe("pending");

          const failed = previewViewModel({
            ...baseArgs("model_inference"),
            statusEntry:
              detail === undefined
                ? { status: "failure" }
                : { status: "failure", detail },
            metadata,
          });
          expect(failed).toEqual(
            detail === undefined
              ? { kind: "failure" }
              : { kind: "failure", detail },
          );
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  it("a terminal status with metadata in flight or errored yields 'loading' / 'unavailable'", () => {
    fc.assert(
      fc.property(
        fc.constantFrom("success", "warning"),
        detectionMetadataArb,
        (status, metadata) => {
          expect(
            previewViewModel({
              ...baseArgs("model_inference"),
              statusEntry: { status },
              metadata,
              metadataLoading: true,
            }).kind,
          ).toBe("loading");
          expect(
            previewViewModel({
              ...baseArgs("model_inference"),
              statusEntry: { status },
              metadata,
              metadataError: true,
            }).kind,
          ).toBe("unavailable");
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  it("a Detection_List yields 'detections' carrying exactly runDetections(metadata) and the overlay thumbnail", () => {
    fc.assert(
      fc.property(
        fc.constantFrom("success", "warning"),
        detectionMetadataArb,
        fc.option(fc.string({ minLength: 1 }), { nil: undefined }),
        (status, metadata, overlayImageSrc) => {
          const vm = previewViewModel({
            ...baseArgs("model_inference"),
            statusEntry: { status },
            metadata,
            overlayImageSrc,
          });
          const expected = runDetections(metadata);
          expect(expected).not.toBeNull();
          expect(vm).toEqual(
            overlayImageSrc === undefined
              ? { kind: "detections", detections: expected }
              : { kind: "detections", detections: expected, imageSrc: overlayImageSrc },
          );
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  it("without a Detection_List, the verdict fields or 'unavailable'", () => {
    fc.assert(
      fc.property(
        fc.constantFrom("success", "warning"),
        fc.option(fc.constantFrom(0, 1, true, false), { nil: undefined }),
        fc.option(fc.double({ min: 0, max: 1, noNaN: true }), { nil: undefined }),
        (status, isAnomalous, confidence) => {
          const metadata: Record<string, unknown> = { trigger: {} };
          if (isAnomalous !== undefined) {
            metadata.is_anomalous = isAnomalous;
          }
          if (confidence !== undefined) {
            metadata.confidence = confidence;
          }
          const vm = previewViewModel({
            ...baseArgs("model_inference"),
            statusEntry: { status },
            metadata,
            overlayImageSrc: "/overlay-image/e1",
          });
          const fields: [string, string][] = [];
          if (isAnomalous !== undefined) {
            fields.push(["is_anomalous", String(isAnomalous)]);
          }
          if (confidence !== undefined) {
            fields.push(["confidence", String(confidence)]);
          }
          expect(vm).toEqual(
            fields.length > 0 ? { kind: "fields", fields } : { kind: "unavailable" },
          );
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  it("leaves the bedrock_inference fields preview unchanged by the overlay argument", () => {
    const vm = previewViewModel({
      ...baseArgs("bedrock_inference"),
      statusEntry: { status: "success" },
      metadata: {
        is_anomalous: true,
        confidence: 0.93,
        detections: [{ label: "plate", confidence: 0.9 }],
      },
      overlayImageSrc: "/overlay-image/e1",
    });
    expect(vm).toEqual({
      kind: "fields",
      fields: [
        ["is_anomalous", "true"],
        ["confidence", "0.93"],
      ],
    });
  });
});
