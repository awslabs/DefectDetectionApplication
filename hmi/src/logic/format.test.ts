import { describe, expect, it } from "vitest";

import { formatDateTime, formatTime } from "./format";

/**
 * Unit tests for timestamp formatting (Requirement 4.6): epoch-seconds
 * rendered in the local time zone with at least seconds precision.
 * Property 10 coverage lives in the separate property-test task.
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
