import fc from "fast-check";
import { describe, expect, it } from "vitest";

import { formatDateTime, formatTime } from "./format";

/**
 * Unit tests for timestamp formatting (Requirement 4.6): epoch-seconds
 * rendered in the local time zone with at least seconds precision.
 *
 * The property coverage below is shared by the single-inspection HMI
 * (quality-station-hmi Property 10) and the Triple_HMI header
 * (imts-triple-inspection-hmi Property 17) — one property, one test.
 */

// A fixed instant: 2025-01-15T14:32:07 UTC.
const EPOCH = 1736951527;

describe("formatTime", () => {
  it("renders HH:MM:SS with zero-padded fields", () => {
    expect(formatTime(EPOCH)).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });

  it("matches the local-time components of the instant", () => {
    const d = new Date(EPOCH * 1000);
    const expected = [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map((v) => String(v).padStart(2, "0"))
      .join(":");
    expect(formatTime(EPOCH)).toBe(expected);
  });

  it("includes seconds precision", () => {
    // Two instants one second apart must render differently.
    expect(formatTime(EPOCH)).not.toBe(formatTime(EPOCH + 1));
  });

  it("zero-pads single-digit components", () => {
    // Choose an instant whose local seconds are 05 regardless of time zone
    // (time-zone offsets are whole minutes).
    const withSeconds5 = EPOCH - (EPOCH % 60) + 5;
    expect(formatTime(withSeconds5).endsWith(":05")).toBe(true);
  });
});

describe("formatDateTime", () => {
  it("renders YYYY-MM-DD HH:MM:SS", () => {
    expect(formatDateTime(EPOCH)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });

  it("matches the local-date components of the instant", () => {
    const d = new Date(EPOCH * 1000);
    const expectedDate = [
      String(d.getFullYear()),
      String(d.getMonth() + 1).padStart(2, "0"),
      String(d.getDate()).padStart(2, "0"),
    ].join("-");
    expect(formatDateTime(EPOCH)).toBe(`${expectedDate} ${formatTime(EPOCH)}`);
  });

  it("handles the epoch origin", () => {
    const d = new Date(0);
    expect(formatDateTime(0).startsWith(String(d.getFullYear()))).toBe(true);
  });
});

/**
 * Property test for run-timing rendering, shared by both HMI entries.
 *
 * **Feature: imts-triple-inspection-hmi, Property 17: Timestamp formatting**
 *
 * **Validates: Requirements 6.3, 6.9**
 *
 * *For any* epoch-seconds `startedAt` and any `finishedAt` (present or
 * absent), the run-timing display renders each present timestamp as its
 * local-time representation with at least seconds precision, and omits the
 * finish time exactly when `finishedAt` is absent, with no error or
 * placeholder text.
 */

/**
 * The run-timing composition both headers use: the started time always
 * rendered, the finish time rendered only when present. Mirrors the header
 * rendering in `ui/render.ts` (and, for the Triple_HMI, `triple/render.ts`),
 * which build these two strings from `formatDateTime` / `formatTime`.
 */
function renderRunTiming(
  startedAt: number,
  finishedAt: number | null | undefined,
): { started: string; finished: string } {
  return {
    started: `Run started ${formatDateTime(startedAt)}`,
    finished: finishedAt != null ? `Finished ${formatTime(finishedAt)}` : "",
  };
}

/**
 * Epoch-seconds timestamps: realistic run times, the epoch origin, and
 * far-past / far-future instants that exercise date rollover and negative
 * offsets.
 */
const epochSeconds = fc.oneof(
  { arbitrary: fc.integer({ min: 1_700_000_000, max: 2_000_000_000 }), weight: 4 },
  { arbitrary: fc.integer({ min: 0, max: 4_000_000_000 }), weight: 2 },
  { arbitrary: fc.integer({ min: -2_000_000_000, max: 2_000_000_000 }), weight: 1 },
  { arbitrary: fc.constantFrom(0, 1, -1, 86_399, 86_400, 951_782_400), weight: 1 },
);

/** An absent finish time is either `null` or `undefined` in API payloads. */
const optionalEpochSeconds = fc.oneof(
  { arbitrary: epochSeconds as fc.Arbitrary<number | null | undefined>, weight: 3 },
  {
    arbitrary: fc.constantFrom<number | null | undefined>(null, undefined),
    weight: 2,
  },
);

/**
 * Independent oracle for local-time-of-day rendering: the platform's own
 * locale formatter on a 24-hour clock, which knows the host time zone
 * without reusing the implementation's `Date` getters.
 */
function localTimeOracle(epoch: number): string {
  return new Date(epoch * 1000).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });
}

/** Text that would signal an error or a placeholder in the timing display. */
const PLACEHOLDER_MARKERS = [
  "NaN",
  "Invalid",
  "undefined",
  "null",
  "unknown",
  "n/a",
  "—",
  "--:--",
];

describe("Property 17: Timestamp formatting", () => {
  it("renders every present timestamp in local time with seconds precision", () => {
    fc.assert(
      fc.property(epochSeconds, optionalEpochSeconds, (startedAt, finishedAt) => {
        const timing = renderRunTiming(startedAt, finishedAt);

        // The started time is always rendered, in local time, to the second.
        expect(timing.started).toContain(localTimeOracle(startedAt));
        expect(timing.started).toMatch(/\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/);

        if (finishedAt != null) {
          expect(timing.finished).toContain(localTimeOracle(finishedAt));
          expect(timing.finished).toMatch(/\d{2}:\d{2}:\d{2}/);
        }

        // Neither field ever carries error or placeholder text.
        for (const marker of PLACEHOLDER_MARKERS) {
          expect(timing.started).not.toContain(marker);
          expect(timing.finished).not.toContain(marker);
        }
      }),
    );
  });

  it("omits the finish time exactly when finishedAt is absent", () => {
    fc.assert(
      fc.property(epochSeconds, optionalEpochSeconds, (startedAt, finishedAt) => {
        const timing = renderRunTiming(startedAt, finishedAt);

        expect(timing.finished === "").toBe(finishedAt == null);
        // Omission never costs the started time.
        expect(timing.started).not.toBe("");
      }),
    );
  });

  it("distinguishes instants one second apart", () => {
    fc.assert(
      fc.property(epochSeconds, (epoch) => {
        // Seconds precision: adjacent instants never collapse to one string.
        expect(formatTime(epoch)).not.toBe(formatTime(epoch + 1));
        expect(formatDateTime(epoch)).not.toBe(formatDateTime(epoch + 1));
      }),
    );
  });

  it("is deterministic and depends on nothing but its arguments", () => {
    fc.assert(
      fc.property(epochSeconds, optionalEpochSeconds, (startedAt, finishedAt) => {
        expect(renderRunTiming(startedAt, finishedAt)).toEqual(
          renderRunTiming(startedAt, finishedAt),
        );
      }),
    );
  });
});
