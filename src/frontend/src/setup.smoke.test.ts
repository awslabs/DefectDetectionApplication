// Smoke test proving the Jest runner and fast-check work end to end.
// This is test-infrastructure scaffolding; it may be removed once real tests exist.
import fc from "fast-check";

describe("frontend test setup", () => {
  it("runs a trivial jest assertion", () => {
    expect(1 + 1).toBe(2);
  });

  it("runs a fast-check property (string concat round-trip)", () => {
    fc.assert(
      fc.property(fc.string(), fc.string(), (a, b) => {
        const joined = a + b;
        expect(joined.startsWith(a)).toBe(true);
        expect(joined.endsWith(b)).toBe(true);
        expect(joined.length).toBe(a.length + b.length);
      })
    );
  });
});
