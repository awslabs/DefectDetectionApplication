import { describe, expect, it } from "vitest";
import fc from "fast-check";

// Scaffold smoke test: verifies the Vitest + fast-check toolchain runs and
// that the global fast-check configuration enforces the minimum of 100
// iterations per property required by the design.
describe("scaffold", () => {
  it("configures fast-check with at least 100 runs per property", () => {
    const { numRuns } = fc.readConfigureGlobal();
    expect(numRuns).toBeGreaterThanOrEqual(100);
  });

  it("executes properties for every generated input", () => {
    let executions = 0;
    fc.assert(
      fc.property(fc.integer(), () => {
        executions += 1;
        return true;
      }),
    );
    expect(executions).toBeGreaterThanOrEqual(100);
  });
});
