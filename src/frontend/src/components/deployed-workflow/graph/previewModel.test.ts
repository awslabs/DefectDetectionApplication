/*
 * Property tests for the output-node preview view-model
 * (output-node-preview-popover spec, tasks 3.3–3.5).
 *
 * Covers design Properties 1–3: output-node classification gating,
 * preview state precedence, and snippet truncation.
 */

import fc from "fast-check";
import type { NodeRunStatus } from "api/WorkflowRegistrationAPI";
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
