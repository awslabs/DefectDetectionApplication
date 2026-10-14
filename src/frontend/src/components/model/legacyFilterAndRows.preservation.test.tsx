/*
 * Defect 2 preservation tests (mqtt-authz-model-visibility, task 2.2).
 *
 * **Feature: mqtt-authz-model-visibility**
 * **Property 4: Preservation — Legacy model filtering and existing model rows**
 * **Validates: Requirements 3.5, 3.6, 3.7**
 *
 * Observation-first baseline captured on the UNFIXED tree — these tests MUST
 * PASS on unfixed code and must KEEP passing after the task 3.2 fix switches
 * the Deployed models page data source from `listModels()` to
 * `listFeatureConfigurations()`:
 *
 *   3.6 — `listModels()` (api/FeatureConfigurationAPI) returns EXACTLY the
 *         `isAssignableModel` subset of the `/feature-configurations`
 *         response: every `LFVModel`/`TritonModel` entry kept in order, every
 *         `VllmModel` entry dropped. The fix must not touch this fetcher.
 *   3.7 — For any VllmModel-FREE response, `DeployedModels` renders one row
 *         per returned entry with the existing rendering: friendly name
 *         (modelAlias fallback to modelName), status, type label
 *         ("LFV (Neo/DLR)" / "Triton" via modelTypeLabel), and input shape
 *         (modelShapeString over modelMetaData). This is the baseline that
 *         must survive the data-source switch: on vLLM-free inputs the
 *         filtered and unfiltered fetchers return identical lists, so the
 *         rendered rows must be identical before and after the fix.
 *
 *   3.5 (EditWorkflow model options exclude VllmModel) is already covered by
 *   the edge-vlm-workflow-fixes tests
 *   (`components/workflow/edit/legacyModelOptions.exploration.test.tsx` and
 *   `legacyModelOptions.preservation.test.tsx`); it is deliberately NOT
 *   duplicated here — this file complements those tests.
 *
 * Like the task 1.2 exploration test, the RAW axios layer is mocked
 * (jest.spyOn(axios, "get")) rather than the API module, so the real
 * `listModels()` filter and the real page query run in every case.
 */

import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axios from "axios";
import fc from "fast-check";

import DeployedModels from "./DeployedModels";
import { listModels } from "api/FeatureConfigurationAPI";
import {
  FeatureConfiguration,
  FeatureConfigurationType,
  isAssignableModel,
} from "components/workflow/types";

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

let getSpy: jest.SpyInstance | undefined;

function setFeatureConfigResponse(configs: FeatureConfiguration[]): void {
  // Mock the raw /feature-configurations axios response so the REAL
  // listModels() (including its isAssignableModel filter) executes.
  getSpy = jest.spyOn(axios, "get").mockResolvedValue({ data: configs });
}

afterEach(() => {
  cleanup();
  if (getSpy) {
    getSpy.mockRestore();
    getSpy = undefined;
  }
});

async function renderDeployedModels(): Promise<void> {
  // Fresh client per render so the query cache never leaks between cases.
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
    logger: {
      log: () => undefined,
      warn: () => undefined,
      error: () => undefined,
    },
  });

  render(
    <QueryClientProvider client={queryClient}>
      <DeployedModels />
    </QueryClientProvider>,
  );

  await waitFor(
    () => {
      expect(getSpy).toHaveBeenCalled();
      expect(
        screen.queryByText("Loading deployed models"),
      ).not.toBeInTheDocument();
    },
    { timeout: 4000 },
  );
}

/** Arbitrary for a single feature-config entry of the given types. */
function configArb(
  types: FeatureConfigurationType[],
): fc.Arbitrary<FeatureConfiguration> {
  return fc
    .tuple(
      fc.constantFrom(...types),
      fc.constantFrom("READY", "LOADING", "STOPPED"),
      fc.hexaString({ minLength: 4, maxLength: 10 }),
    )
    .map(([type, status, suffix]) => ({
      type,
      status,
      modelName: `${
        type === FeatureConfigurationType.VllmModel ? "vllm" : "model"
      }-${suffix}`,
      defaultConfiguration: {},
    }));
}

const ALL_TYPES = [
  FeatureConfigurationType.LFVModel,
  FeatureConfigurationType.TritonModel,
  FeatureConfigurationType.VllmModel,
];

const ASSIGNABLE_TYPES = [
  FeatureConfigurationType.LFVModel,
  FeatureConfigurationType.TritonModel,
];

/** Observed baseline: the type labels the unfixed page renders per type. */
const TYPE_LABEL: Record<string, string> = {
  [FeatureConfigurationType.LFVModel]: "LFV (Neo/DLR)",
  [FeatureConfigurationType.TritonModel]: "Triton",
};

/* ------------------------------------------------------------------ */
/* 3.6 — listModels() returns exactly the isAssignableModel subset     */
/* ------------------------------------------------------------------ */

describe("listModels() legacy filter preservation (Property 4, 3.6)", () => {
  it("returns EXACTLY the isAssignableModel subset for any mixed LFV/Triton/Vllm list", async () => {
    const listArb = fc.uniqueArray(configArb(ALL_TYPES), {
      selector: (c) => c.modelName,
      minLength: 0,
      maxLength: 8,
    });

    await fc.assert(
      fc.asyncProperty(listArb, async (configs) => {
        setFeatureConfigResponse(configs);
        try {
          const result = await listModels();

          // Exact subset, order preserved: no VllmModel entry survives, and
          // every assignable entry comes through unchanged.
          expect(result).toEqual(configs.filter(isAssignableModel));
          expect(
            result.some(
              (c) => c.type === FeatureConfigurationType.VllmModel,
            ),
          ).toBe(false);
        } finally {
          if (getSpy) {
            getSpy.mockRestore();
            getSpy = undefined;
          }
        }
      }),
      { numRuns: 50 },
    );
  }, 30000);
});

/* ------------------------------------------------------------------ */
/* 3.7 — DeployedModels renders LFV/Triton rows exactly as before      */
/* ------------------------------------------------------------------ */

describe("DeployedModels LFV/Triton row rendering preservation (Property 4, 3.7)", () => {
  it("renders the baseline concrete rows: alias name, status, type label, input shape", async () => {
    // Concrete vLLM-free baseline mirroring the observed on-device rendering:
    // friendly name from modelAlias, status text, per-type label, and the
    // input shape extracted from the Triton model-metadata JSON.
    setFeatureConfigResponse([
      {
        type: FeatureConfigurationType.TritonModel,
        status: "READY",
        modelName: "cookies-binary",
        defaultConfiguration: {
          modelAlias: "Cookies binary classifier",
          modelMetaData: JSON.stringify({
            inputs: [
              { name: "input", datatype: "FP32", shape: [1, 3, 224, 224] },
            ],
          }),
        },
      },
      {
        type: FeatureConfigurationType.LFVModel,
        status: "LOADING",
        modelName: "widget-anomaly",
        defaultConfiguration: {},
      },
    ]);

    await renderDeployedModels();

    // Friendly name column: alias when present, modelName otherwise.
    expect(
      screen.queryAllByText("Cookies binary classifier").length,
    ).toBeGreaterThan(0);
    expect(screen.queryAllByText("widget-anomaly").length).toBeGreaterThan(0);
    // ID column keeps the raw model name even when an alias is shown.
    expect(screen.queryAllByText("cookies-binary").length).toBeGreaterThan(0);

    // Status column.
    expect(screen.queryAllByText("READY").length).toBeGreaterThan(0);
    expect(screen.queryAllByText("LOADING").length).toBeGreaterThan(0);

    // Type label column (modelTypeLabel baseline).
    expect(screen.queryAllByText("Triton").length).toBeGreaterThan(0);
    expect(screen.queryAllByText("LFV (Neo/DLR)").length).toBeGreaterThan(0);

    // Input shape column (modelShapeString baseline) with "-" fallback.
    expect(
      screen.queryAllByText("input: [1,3,224,224]").length,
    ).toBeGreaterThan(0);

    // Header counter reflects one row per returned entry.
    expect(screen.queryAllByText("(2)").length).toBeGreaterThan(0);
  }, 15000);

  it("renders one row per entry (name, status, type label) for ANY vLLM-free list", async () => {
    // VllmModel-FREE lists only: the bug condition does not hold, so the
    // page must render identically before and after the fix. ≤ 6 entries
    // keeps every row on the first table page (page size 10).
    const listArb = fc.uniqueArray(configArb(ASSIGNABLE_TYPES), {
      selector: (c) => c.modelName,
      minLength: 1,
      maxLength: 6,
    });

    await fc.assert(
      fc.asyncProperty(listArb, async (configs) => {
        cleanup();
        setFeatureConfigResponse(configs);

        await renderDeployedModels();

        // One row per returned entry (header counter is the item count).
        expect(
          screen.queryAllByText(`(${configs.length})`).length,
        ).toBeGreaterThan(0);

        for (const config of configs) {
          // Every entry's name renders (friendlyName falls back to the
          // model name, and the ID column always shows it).
          expect(
            screen.queryAllByText(config.modelName).length,
          ).toBeGreaterThan(0);
        }

        // Type label cells: exactly one label per entry of that type.
        for (const type of ASSIGNABLE_TYPES) {
          const expected = configs.filter((c) => c.type === type).length;
          expect(screen.queryAllByText(TYPE_LABEL[type]).length).toBe(
            expected,
          );
        }

        // Status cells: exactly one per entry carrying that status.
        for (const status of ["READY", "LOADING", "STOPPED"]) {
          const expected = configs.filter((c) => c.status === status).length;
          expect(screen.queryAllByText(status).length).toBe(expected);
        }
      }),
      { numRuns: 10 },
    );
  }, 120000);
});
