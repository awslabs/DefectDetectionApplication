import fc from "fast-check";
import { describe, expect, it } from "vitest";

import {
  DEFAULT_TRIPLE_WORKFLOW_NAME,
  resolveWorkflowName,
  type ConfigValue,
} from "./config";

/**
 * Property test for the Triple_HMI's Target_Workflow name resolution.
 *
 * **Feature: imts-triple-inspection-hmi, Property 5: Workflow-name configuration resolution**
 *
 * **Validates: Requirements 2.5**
 */

/** Whitespace-only values that must fall through to the next source. */
const blankish = fc.constantFrom("", " ", "   ", "\t", "\n", " \t\n ");

/** A configuration source: absent, blank/whitespace-only, or arbitrary. */
const configValue: fc.Arbitrary<ConfigValue> = fc.oneof(
  { arbitrary: fc.constantFrom<ConfigValue>(null, undefined), weight: 1 },
  { arbitrary: blankish as fc.Arbitrary<ConfigValue>, weight: 1 },
  { arbitrary: fc.string() as fc.Arbitrary<ConfigValue>, weight: 2 },
  // Realistic workflow names, plus names padded with whitespace.
  {
    arbitrary: fc
      .stringMatching(/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,40}$/)
      .chain((name) =>
        fc.constantFrom(name, ` ${name}`, `${name} `, `\t${name}\n`),
      ) as fc.Arbitrary<ConfigValue>,
    weight: 3,
  },
);

/** Independent oracle: a source counts only when it has non-whitespace text. */
function isBlank(value: ConfigValue): boolean {
  return value === null || value === undefined || /^\s*$/.test(value);
}

describe("Property 5: Workflow-name configuration resolution", () => {
  it("resolves query > build-time > default, blank sources falling through", () => {
    fc.assert(
      fc.property(configValue, configValue, (queryValue, buildTimeValue) => {
        const resolved = resolveWorkflowName(queryValue, buildTimeValue);

        if (!isBlank(queryValue)) {
          // The query parameter wins whenever it carries a name, even when a
          // build-time value is also present (Requirement 2.5).
          expect(resolved).toBe((queryValue as string).trim());
        } else if (!isBlank(buildTimeValue)) {
          expect(resolved).toBe((buildTimeValue as string).trim());
        } else {
          expect(resolved).toBe(DEFAULT_TRIPLE_WORKFLOW_NAME);
        }

        // The resolved name is always usable as a case-sensitive exact match
        // key: non-empty and free of surrounding whitespace.
        expect(resolved).not.toBe("");
        expect(resolved).toBe(resolved.trim());
      }),
    );
  });

  it("is deterministic and depends on nothing but its two arguments", () => {
    fc.assert(
      fc.property(configValue, configValue, (queryValue, buildTimeValue) => {
        expect(resolveWorkflowName(queryValue, buildTimeValue)).toBe(
          resolveWorkflowName(queryValue, buildTimeValue),
        );
      }),
    );
  });
});

describe("resolveWorkflowName examples", () => {
  it("defaults when neither source is configured", () => {
    expect(resolveWorkflowName(null, null)).toBe(
      "blue-plate-detection-guided-inspection",
    );
  });

  it("prefers the query parameter over the build-time value", () => {
    expect(resolveWorkflowName("from-query", "from-build")).toBe("from-query");
  });

  it("falls through a whitespace-only query parameter", () => {
    expect(resolveWorkflowName("   ", "from-build")).toBe("from-build");
  });

  it("falls through both whitespace-only sources to the default", () => {
    expect(resolveWorkflowName(" ", "\t")).toBe(DEFAULT_TRIPLE_WORKFLOW_NAME);
  });
});
