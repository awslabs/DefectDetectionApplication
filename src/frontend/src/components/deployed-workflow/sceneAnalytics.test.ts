/*
 * The readers of a run's Scene_Analytics_Node outputs
 * (rtsp-rtmp-stream-cameras Requirement 16.3). The run metadata is
 * arbitrary JSON, so they must skip what does not fit and never throw.
 */
import {
  hasSceneAnalytics,
  runAssociations,
  runCounters,
  runEventGates,
  runSceneAnalytics,
  violatingDetectionIds,
} from "./sceneAnalytics";

/** The design's run metadata example. */
const METADATA = {
  counter: {
    count_1: {
      counts: { person: 3, hardhat: 2 },
      total: 5,
      labels: { person: "Person", hardhat: "Hardhat" },
    },
  },
  association: {
    ppe: {
      subjects: 3,
      compliant: 2,
      violations: 1,
      missing: { hardhat: 1, vest: 0 },
      violating_ids: ["9f2c1a7b"],
    },
  },
  event: {
    gate_1: {
      state: "active",
      transition: "activated",
      active_since: 1790000000150,
      consecutive_true: 3,
      consecutive_false: 0,
    },
  },
};

describe("runSceneAnalytics", () => {
  it("reads the design's example", () => {
    expect(runSceneAnalytics(METADATA)).toEqual({
      counters: [
        {
          nodeId: "count_1",
          total: 5,
          rows: [
            { key: "person", label: "Person", count: 3 },
            { key: "hardhat", label: "Hardhat", count: 2 },
          ],
        },
      ],
      associations: [
        {
          nodeId: "ppe",
          subjects: 3,
          compliant: 2,
          violations: 1,
          missing: [
            { key: "hardhat", label: "hardhat", count: 1 },
            { key: "vest", label: "vest", count: 0 },
          ],
          violatingIds: ["9f2c1a7b"],
        },
      ],
      gates: [
        {
          nodeId: "gate_1",
          state: "active",
          transition: "activated",
          activeSince: 1790000000150,
          consecutiveTrue: 3,
          consecutiveFalse: 0,
        },
      ],
    });
    expect(hasSceneAnalytics(runSceneAnalytics(METADATA))).toBe(true);
  });

  it.each([undefined, null, {}, { counter: [] }, { counter: "x", association: 7, event: null }])(
    "finds nothing in %p",
    (metadata) => {
      const analytics = runSceneAnalytics(metadata as never);
      expect(analytics).toEqual({ counters: [], associations: [], gates: [] });
      expect(hasSceneAnalytics(analytics)).toBe(false);
    },
  );

  it("keeps the executor's node order", () => {
    const counters = runCounters({
      counter: { zone_b: { total: 1 }, zone_a: { total: 2 } },
    });
    expect(counters.map((counter) => counter.nodeId)).toEqual(["zone_b", "zone_a"]);
  });
});

describe("defensive reads", () => {
  it("skips node entries that are not objects", () => {
    expect(runCounters({ counter: { a: 3, b: null, c: [1], d: { total: 1 } } })).toEqual([
      { nodeId: "d", total: 1, rows: [] },
    ]);
  });

  it("reads bad counts as zero and falls back to the key for a label", () => {
    expect(
      runCounters({
        counter: {
          c: {
            counts: { person: -1, car: "3", "": 2, bus: Number.NaN },
            total: "5",
            labels: { car: "", bus: 9 },
          },
        },
      }),
    ).toEqual([
      {
        nodeId: "c",
        total: 0,
        rows: [
          { key: "person", label: "person", count: 0 },
          { key: "car", label: "car", count: 0 },
          { key: "", label: "(unlabeled)", count: 2 },
          { key: "bus", label: "bus", count: 0 },
        ],
      },
    ]);
  });

  it("keeps only non-empty string violating ids", () => {
    expect(
      runAssociations({ association: { a: { violating_ids: ["d1", "", 7, null, "d2"] } } })[0]
        .violatingIds,
    ).toEqual(["d1", "d2"]);
    expect(runAssociations({ association: { a: { violating_ids: "d1" } } })[0].violatingIds).toEqual([]);
  });

  it("reads an unknown gate state as inactive and an unknown transition as none", () => {
    expect(
      runEventGates({ event: { g: { state: "armed", transition: "flipped", active_since: "now" } } }),
    ).toEqual([
      {
        nodeId: "g",
        state: "inactive",
        transition: "none",
        activeSince: null,
        consecutiveTrue: 0,
        consecutiveFalse: 0,
      },
    ]);
  });
});

describe("violatingDetectionIds", () => {
  it("collects the violating ids of every association", () => {
    const analytics = runSceneAnalytics({
      association: {
        hardhats: { violating_ids: ["d1", "d2"] },
        vests: { violating_ids: ["d2", "d5"] },
      },
    });
    expect(Array.from(violatingDetectionIds(analytics)).sort()).toEqual(["d1", "d2", "d5"]);
  });

  it("is empty without associations", () => {
    expect(violatingDetectionIds(runSceneAnalytics(METADATA.counter)).size).toBe(0);
  });
});
