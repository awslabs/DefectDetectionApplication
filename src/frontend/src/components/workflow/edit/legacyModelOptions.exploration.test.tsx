/*
 * Bug 1 condition exploration test (edge-vlm-workflow-fixes, task 1).
 *
 * **Feature: edge-vlm-workflow-fixes, Property 1: Bug Condition — VLM excluded
 * from legacy model options**
 * **Validates: Requirements 2.1, 2.2**
 *
 * These tests encode the EXPECTED behavior of the legacy workflow editor's
 * model selection: the model options it builds must NOT contain any
 * `VllmModel`-backed entry, while every `LFVModel` / `TritonModel` entry must
 * remain selectable.
 *
 * On UNFIXED code they are EXPECTED TO FAIL — each failure is a counterexample
 * confirming the bug (isBugCondition1 from design.md):
 *
 *   isBugCondition1(config) :=
 *       config.type == "VllmModel"
 *       AND config appears in the legacy-workflow selectable model options
 *
 * `EditWorkflow.tsx` builds `modelOptions` from the RAW feature-config list
 * (`listFeatureConfigurations()`) with no type filter, so a `VllmModel` entry
 * such as `opt125m-smoke` leaks in as a selectable legacy model.
 *
 * To observe exactly the options EditWorkflow computes, `ImageSourceAndModel`
 * is mocked to render the `modelOptions` prop it receives. This exercises the
 * REAL (buggy) builder in EditWorkflow rather than a reproduction of it, so the
 * same test flips from FAIL (unfixed) to PASS once the VLM filter is added.
 */

import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import fc from "fast-check";

import EditWorkflow from "./EditWorkflow";
import {
  FeatureConfiguration,
  FeatureConfigurationType,
} from "components/workflow/types";

// The exact backend `type` value emitted for vLLM models (design: VLLM_FEATURE_TYPE).
const VLLM_FEATURE_TYPE = "VllmModel";

/* ------------------------------------------------------------------ */
/* Mocks                                                               */
/* ------------------------------------------------------------------ */

// Capture the modelOptions EditWorkflow builds by rendering them as a list.
// (Same directory specifier EditWorkflow imports, so this replaces the real
// child component.)
jest.mock("./ImageSourceAndModel", () => ({
  __esModule: true,
  default: ({
    modelOptions,
  }: {
    modelOptions: Array<{ label?: string; value?: string }>;
  }) => (
    <ul data-testid="model-options">
      {modelOptions.map((option, index) => (
        <li key={index} data-value={option.value}>
          {option.label}
        </li>
      ))}
    </ul>
  ),
}));

jest.mock("api/FeatureConfigurationAPI");
jest.mock("api/WorkflowAPI");

// eslint-disable-next-line @typescript-eslint/no-var-requires
const featureConfigAPI = require("api/FeatureConfigurationAPI");
// eslint-disable-next-line @typescript-eslint/no-var-requires
const workflowAPI = require("api/WorkflowAPI");

// Minimal workflow so EditWorkflow's getWorkflow query resolves and the
// (mocked) ImageSourceAndModel renders.
const EMPTY_WORKFLOW = {
  workflowId: "wf-1",
  name: "Legacy Workflow",
  description: "",
  inputConfigurations: [],
  imageSources: [],
  featureConfigurations: [],
  outputConfigurations: [],
};

function setFeatureConfigs(configs: FeatureConfiguration[]): void {
  (featureConfigAPI.listFeatureConfigurations as jest.Mock).mockResolvedValue(
    configs,
  );
}

beforeEach(() => {
  // CRA's jest config resets mocks before each test; (re)install implementations.
  (workflowAPI.getWorkflow as jest.Mock).mockResolvedValue(EMPTY_WORKFLOW);
  (workflowAPI.editWorkflow as jest.Mock).mockResolvedValue(EMPTY_WORKFLOW);
});

afterEach(() => {
  cleanup();
});

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

async function renderLegacyModelOptions(): Promise<string[]> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
    logger: {
      log: () => undefined,
      warn: () => undefined,
      error: () => undefined,
    },
  });

  const { container } = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/workflows/wf-1/edit"]}>
        <Routes>
          <Route path="/workflows/:workflowId/edit" element={<EditWorkflow />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );

  await screen.findByTestId("model-options", {}, { timeout: 4000 });
  // The option values are the model names EditWorkflow made selectable.
  await waitFor(() => {
    // ImageSourceAndModel only renders after getWorkflow resolves; give the
    // model-options list a chance to be populated.
    expect(container.querySelector('[data-testid="model-options"]')).not.toBeNull();
  });

  const items = Array.from(
    container.querySelectorAll('[data-testid="model-options"] li'),
  );
  return items.map((li) => li.getAttribute("data-value") || "");
}

// Reported concrete feature-config listing (design "Examples" for Bug 1).
const REPORTED_CONFIGS: FeatureConfiguration[] = [
  {
    // Triton model / READY → must remain selectable (not a bug).
    type: FeatureConfigurationType.TritonModel,
    status: "READY",
    modelName: "cookies-binary",
    defaultConfiguration: {},
  },
  {
    // LFV model / LOADING → must remain selectable (not a bug).
    type: FeatureConfigurationType.LFVModel,
    status: "LOADING",
    modelName: "model-cookies-binary",
    defaultConfiguration: {},
  },
  {
    // vLLM entry / READY: currently selectable (BUG); must be excluded.
    type: VLLM_FEATURE_TYPE as unknown as FeatureConfigurationType,
    status: "READY",
    modelName: "opt125m-smoke",
    defaultConfiguration: {},
  },
];

/* ------------------------------------------------------------------ */
/* Scoped concrete case                                                */
/* ------------------------------------------------------------------ */

describe("legacy workflow model options — VLM exclusion (bug condition)", () => {
  it("excludes the opt125m-smoke VllmModel entry while keeping the LFV/Triton models", async () => {
    setFeatureConfigs(REPORTED_CONFIGS);

    const optionValues = await renderLegacyModelOptions();

    // Non-VLM models (Triton READY + LFV LOADING) remain selectable (3.1).
    expect(optionValues).toContain("cookies-binary");
    expect(optionValues).toContain("model-cookies-binary");

    // The VLM model must NOT be offered as a legacy model option (2.1, 2.2).
    // On UNFIXED code this FAILS: "opt125m-smoke" (VllmModel) is present.
    expect(optionValues).not.toContain("opt125m-smoke");
  }, 15000);

  /* ---------------------------------------------------------------- */
  /* Companion root-cause diagnostic: the no-op listModels() filter    */
  /* ---------------------------------------------------------------- */

  it("listModels() excludes VllmModel entries (repaired filter)", async () => {
    // Exercise the REAL listModels() (bypass the auto-mock) against the
    // reported listing. The filter previously bound as
    // `(config.type === LFVModel) || "TritonModel"` (a truthy constant), so it
    // was always true and excluded nothing — every VllmModel entry survived.
    //
    // After the fix, listModels() delegates to the shared `isAssignableModel`
    // helper, which drops `VllmModel` entries while retaining
    // `LFVModel` / `TritonModel`. This diagnostic now asserts the CORRECTED
    // behavior described in design.md.
    const realAPI = jest.requireActual("api/FeatureConfigurationAPI");
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const axios = require("axios").default;
    const getSpy = jest
      .spyOn(axios, "get")
      .mockResolvedValue({ data: REPORTED_CONFIGS });

    try {
      const models = await realAPI.listModels();
      const returnedTypes = models.map(
        (config: FeatureConfiguration) => config.type,
      );
      // The repaired filter drops the VllmModel entry.
      expect(returnedTypes).not.toContain(VLLM_FEATURE_TYPE);
      expect(
        models.some(
          (config: FeatureConfiguration) => config.modelName === "opt125m-smoke",
        ),
      ).toBe(false);
      // Non-VLM models are retained.
      expect(
        models.some(
          (config: FeatureConfiguration) => config.modelName === "cookies-binary",
        ),
      ).toBe(true);
      expect(
        models.some(
          (config: FeatureConfiguration) =>
            config.modelName === "model-cookies-binary",
        ),
      ).toBe(true);
    } finally {
      getSpy.mockRestore();
    }
  });

  it("produces empty options when the list contains only VllmModel entries", async () => {
    setFeatureConfigs([
      {
        type: VLLM_FEATURE_TYPE as unknown as FeatureConfigurationType,
        status: "READY",
        modelName: "opt125m-smoke",
        defaultConfiguration: {},
      },
      {
        type: VLLM_FEATURE_TYPE as unknown as FeatureConfigurationType,
        status: "READY",
        modelName: "vlm-only-2",
        defaultConfiguration: {},
      },
    ]);

    const optionValues = await renderLegacyModelOptions();

    // A list of only VLM entries yields no legacy model options (design edge case).
    expect(optionValues).toHaveLength(0);
  }, 15000);

  /* ---------------------------------------------------------------- */
  /* Generalized property                                             */
  /* ---------------------------------------------------------------- */

  it("never offers any VllmModel entry as a legacy option, for any mixed list (2.1)", async () => {
    const configArb: fc.Arbitrary<FeatureConfiguration> = fc.record({
      type: fc.constantFrom(
        FeatureConfigurationType.LFVModel,
        FeatureConfigurationType.TritonModel,
        VLLM_FEATURE_TYPE as unknown as FeatureConfigurationType,
      ),
      status: fc.constantFrom("READY", "LOADING"),
      modelName: fc.string({ minLength: 1, maxLength: 12 }),
      defaultConfiguration: fc.constant({}),
    });

    // Unique model names, and at least one VllmModel entry (bug condition holds).
    const listArb = fc
      .uniqueArray(configArb, {
        selector: (c) => c.modelName,
        minLength: 1,
        maxLength: 6,
      })
      .filter((list) => list.some((c) => c.type === VLLM_FEATURE_TYPE))
      .map((list) => list.filter((c) => c.modelName.trim().length > 0));

    await fc.assert(
      fc.asyncProperty(listArb, async (configs) => {
        cleanup();
        setFeatureConfigs(configs);

        const optionValues = await renderLegacyModelOptions();
        const typeByName = new Map(configs.map((c) => [c.modelName, c.type]));

        // No selectable option maps back to a VllmModel config (2.1).
        for (const value of optionValues) {
          expect(typeByName.get(value)).not.toBe(VLLM_FEATURE_TYPE);
        }

        // Every non-VLM config remains selectable (regression prevention 3.1).
        for (const config of configs) {
          if (config.type !== (VLLM_FEATURE_TYPE as unknown)) {
            expect(optionValues).toContain(config.modelName);
          }
        }
      }),
      { numRuns: 12 },
    );
  }, 120000);
});
