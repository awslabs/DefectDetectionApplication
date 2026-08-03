/*
 * Defect 2 condition exploration test (mqtt-authz-model-visibility, task 1.2).
 *
 * **Feature: mqtt-authz-model-visibility**
 * **Property 2: Bug Condition — vLLM models visible on the Deployed models page**
 * **Validates: Requirements 1.3, 2.3**
 *
 * These tests encode the EXPECTED behavior of the Deployed models page: every
 * model entry returned by the backend `/feature-configurations` endpoint —
 * including every `VllmModel` entry — must appear as a table row.
 *
 * On UNFIXED code they are EXPECTED TO FAIL — each failure is a counterexample
 * confirming the bug (isBugCondition_2 from design.md):
 *
 *   isBugCondition_2(response) :=
 *       EXISTS m IN response WHERE m.type == "VllmModel"
 *       // listModels() drops m via isAssignableModel, so the Deployed models
 *       // page never shows it
 *
 * Root cause under test: `DeployedModels.tsx` fetches through `listModels()`
 * (`api/FeatureConfigurationAPI`), which applies the legacy-workflow filter
 * `isAssignableModel` — a filter meant only for legacy workflow model
 * assignment. It drops every `VllmModel` entry, so vLLM models deployed and
 * reported by the device never render.
 *
 * Concrete on-device counterexample (JP6 device, component
 * `model-vllm-opt125m-smoke` 2.0.0 RUNNING; `/feature-configurations` verified
 * to return the entry):
 *
 *   {"type":"VllmModel","modelName":"opt125m-smoke","status":"LOADING",...}
 *
 * The axios response is mocked (jest.spyOn(axios, "get")) rather than the API
 * module, so the REAL (buggy) `listModels()` filter inside
 * `FeatureConfigurationAPI` is exercised — the same tests flip from FAIL
 * (unfixed) to PASS once the page switches to the unfiltered
 * `listFeatureConfigurations()` data source.
 *
 * COUNTEREXAMPLES OBSERVED ON UNFIXED CODE (documented per task 1.2):
 *  - Concrete: response [TritonModel "cookies-binary" READY, VllmModel
 *    "opt125m-smoke" LOADING] renders only the Triton row; "opt125m-smoke"
 *    is absent from the table.
 *  - Property (fast-check, seed 734630949, shrunk 13 times):
 *      [{"type":"VllmModel","status":"READY","modelName":"vllm-0000",
 *        "defaultConfiguration":{}},
 *       {"type":"LFVModel","status":"READY","modelName":"model-0000",
 *        "defaultConfiguration":{}}]
 *    — the table renders "model-0000" but "vllm-0000" is missing: rendered
 *    rows ["model-0000"] ≠ returned models ["model-0000","vllm-0000"].
 */

import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axios from "axios";
import fc from "fast-check";

import DeployedModels from "./DeployedModels";
import {
  FeatureConfiguration,
  FeatureConfigurationType,
} from "components/workflow/types";

// The exact backend `type` value emitted for vLLM models.
const VLLM_FEATURE_TYPE = FeatureConfigurationType.VllmModel;

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

let getSpy: jest.SpyInstance;

function setFeatureConfigResponse(configs: FeatureConfiguration[]): void {
  // Mock the raw /feature-configurations axios response so the REAL
  // listModels() (and its isAssignableModel filter — the suspected root
  // cause) runs inside the page's query.
  getSpy = jest
    .spyOn(axios, "get")
    .mockResolvedValue({ data: configs });
}

afterEach(() => {
  cleanup();
  if (getSpy) getSpy.mockRestore();
});

async function renderDeployedModels(): Promise<void> {
  // Fresh client per render so the ["listModels"] queryKey is never served
  // from a previous test's cache.
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

  // Wait for the query to settle: the table's loading state clears once the
  // mocked response has been fetched and mapped to rows.
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

/** All model names visible in the rendered table body. */
function renderedNames(candidates: string[]): string[] {
  // Each row shows the model name in both the "Name" (friendlyName fallback)
  // and "ID" columns; presence of the text anywhere in the rendered table is
  // sufficient evidence the model has a row.
  return candidates.filter((name) => screen.queryAllByText(name).length > 0);
}

/* ------------------------------------------------------------------ */
/* Concrete on-device counterexample                                   */
/* ------------------------------------------------------------------ */

// The verified device listing: a Triton model alongside the vLLM entry
// (design "Examples": /feature-configurations returns both, the page lists
// only the Triton model).
const ON_DEVICE_RESPONSE: FeatureConfiguration[] = [
  {
    type: FeatureConfigurationType.TritonModel,
    status: "READY",
    modelName: "cookies-binary",
    defaultConfiguration: {},
  },
  {
    // Concrete on-device counterexample:
    // {"type":"VllmModel","modelName":"opt125m-smoke","status":"LOADING",...}
    type: VLLM_FEATURE_TYPE,
    status: "LOADING",
    modelName: "opt125m-smoke",
    defaultConfiguration: {},
  },
];

describe("DeployedModels — vLLM row visibility (Property 2: Bug Condition)", () => {
  it("lists the on-device VllmModel entry opt125m-smoke alongside the Triton model (2.3)", async () => {
    setFeatureConfigResponse(ON_DEVICE_RESPONSE);

    await renderDeployedModels();

    // The Triton model renders (works today — regression guard, 3.7).
    expect(screen.queryAllByText("cookies-binary").length).toBeGreaterThan(0);

    // The VllmModel entry must ALSO render as a row.
    // On UNFIXED code this FAILS: listModels() drops it via isAssignableModel,
    // so "opt125m-smoke" never appears in the table.
    expect(screen.queryAllByText("opt125m-smoke").length).toBeGreaterThan(0);
  }, 15000);

  /* ---------------------------------------------------------------- */
  /* Generalized property                                              */
  /* ---------------------------------------------------------------- */

  it("renders a row for EVERY returned model, for any mixed Triton/vLLM list (2.3)", async () => {
    // Names constrained to a safe, realistic shape (kebab-ish, unique) so
    // testing-library text matching is exact and collision-free.
    const configArb: fc.Arbitrary<FeatureConfiguration> = fc
      .tuple(
        fc.constantFrom(
          FeatureConfigurationType.LFVModel,
          FeatureConfigurationType.TritonModel,
          VLLM_FEATURE_TYPE,
        ),
        fc.constantFrom("READY", "LOADING"),
        fc.hexaString({ minLength: 4, maxLength: 10 }),
      )
      .map(([type, status, suffix]) => ({
        type,
        status,
        modelName: `${type === VLLM_FEATURE_TYPE ? "vllm" : "model"}-${suffix}`,
        defaultConfiguration: {},
      }));

    // Unique model names; at least one VllmModel entry so isBugCondition_2
    // holds; ≤ 6 entries keeps every row on the first table page.
    const listArb = fc
      .uniqueArray(configArb, {
        selector: (c) => c.modelName,
        minLength: 1,
        maxLength: 6,
      })
      .filter((list) => list.some((c) => c.type === VLLM_FEATURE_TYPE));

    await fc.assert(
      fc.asyncProperty(listArb, async (configs) => {
        cleanup();
        setFeatureConfigResponse(configs);

        await renderDeployedModels();

        const names = configs.map((c) => c.modelName);
        const visible = renderedNames(names);

        // Every returned model — including each VllmModel — appears as a row.
        // On UNFIXED code this FAILS for every list containing a VllmModel:
        // those names are filtered out before the table ever sees them.
        expect(visible.sort()).toEqual([...names].sort());
      }),
      { numRuns: 12 },
    );
  }, 120000);
});
