/*
 * Preservation property tests — Bug 1 non-bug-condition inputs
 * (edge-vlm-workflow-fixes, task 2).
 *
 * **Feature: edge-vlm-workflow-fixes, Property 4: Preservation — Non-VLM
 * models, other consumers, and UUID fallback unchanged**
 * **Validates: Requirements 3.1, 3.2, 3.6**
 *
 * Observation-first methodology: these assertions encode behavior OBSERVED on
 * the UNFIXED code and MUST PASS on it — they establish the baseline the Bug 1
 * fix must preserve. They cover only inputs where isBugCondition1 does NOT hold
 * (lists with no `VllmModel` entry), so the VLM filter added by the fix cannot
 * change them: the same options must keep coming out.
 *
 * Covered preservation requirements:
 *   3.1 — `LFVModel` / `TritonModel` entries stay selectable, in the same
 *         `sortWorkflowModelOptions` order, with the same
 *         `getWorkflowModelOptionLabelWithoutVersion` labels.
 *   3.2 — `listFeatureConfigurations()` (the raw fetch other consumers use)
 *         returns `VllmModel` entries unchanged (VLM exclusion is scoped to
 *         the legacy model options only).
 *   3.6 — a legacy workflow that already has a non-VLM model assigned still
 *         loads and displays that model.
 *
 * As in the task-1 exploration test, `ImageSourceAndModel` is mocked to render
 * the `modelOptions` EditWorkflow builds (and the currently-selected model) so
 * the REAL EditWorkflow builder is exercised.
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
import {
  getWorkflowModelOptionLabelWithoutVersion,
  sortWorkflowModelOptions,
} from "components/utils";

// The exact backend `type` value emitted for vLLM models (design: VLLM_FEATURE_TYPE).
const VLLM_FEATURE_TYPE = "VllmModel";

/* ------------------------------------------------------------------ */
/* Mocks                                                               */
/* ------------------------------------------------------------------ */

// Render both the modelOptions EditWorkflow built and the selected model it
// seeded into the form, so we can observe order/labels (3.1) and the
// already-assigned model (3.6).
jest.mock("./ImageSourceAndModel", () => ({
  __esModule: true,
  default: ({
    modelOptions,
    form,
  }: {
    modelOptions: Array<{ label?: string; value?: string }>;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    form: any;
  }) => {
    const selected = form.watch("model");
    return (
      <div>
        <ul data-testid="model-options">
          {modelOptions.map((option, index) => (
            <li key={index} data-value={option.value} data-label={option.label}>
              {option.label}
            </li>
          ))}
        </ul>
        <div
          data-testid="selected-model"
          data-value={selected?.value ?? ""}
          data-label={selected?.label ?? ""}
        />
      </div>
    );
  },
}));

jest.mock("api/FeatureConfigurationAPI");
jest.mock("api/WorkflowAPI");

// eslint-disable-next-line @typescript-eslint/no-var-requires
const featureConfigAPI = require("api/FeatureConfigurationAPI");
// eslint-disable-next-line @typescript-eslint/no-var-requires
const workflowAPI = require("api/WorkflowAPI");

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

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function setWorkflow(workflow: any): void {
  (workflowAPI.getWorkflow as jest.Mock).mockResolvedValue(workflow);
}

beforeEach(() => {
  // CRA's jest config resets mocks before each test; (re)install implementations.
  setWorkflow(EMPTY_WORKFLOW);
  (workflowAPI.editWorkflow as jest.Mock).mockResolvedValue(EMPTY_WORKFLOW);
});

afterEach(() => {
  cleanup();
});

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
    logger: {
      log: () => undefined,
      warn: () => undefined,
      error: () => undefined,
    },
  });
}

async function renderEditor(): Promise<HTMLElement> {
  const { container } = render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter initialEntries={["/workflows/wf-1/edit"]}>
        <Routes>
          <Route path="/workflows/:workflowId/edit" element={<EditWorkflow />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );

  await screen.findByTestId("model-options", {}, { timeout: 4000 });
  await waitFor(() => {
    expect(
      container.querySelector('[data-testid="model-options"]'),
    ).not.toBeNull();
  });
  return container;
}

async function renderModelOptions(): Promise<
  Array<{ value: string; label: string }>
> {
  const container = await renderEditor();
  const items = Array.from(
    container.querySelectorAll('[data-testid="model-options"] li'),
  );
  return items.map((li) => ({
    value: li.getAttribute("data-value") || "",
    label: li.getAttribute("data-label") || "",
  }));
}

/* ------------------------------------------------------------------ */
/* 3.1 — non-VLM options: selectable, ordered, labelled unchanged      */
/* ------------------------------------------------------------------ */

describe("legacy model options preservation — non-VLM entries (3.1)", () => {
  // A generator of NON-VLM feature configs (bug condition does NOT hold).
  // Unique modelAlias + modelName keep sortWorkflowModelOptions a total order,
  // so the observed ordering is deterministic and idempotent across re-renders.
  const listArb = fc
    .uniqueArray(
      fc.record({
        type: fc.constantFrom(
          FeatureConfigurationType.LFVModel,
          FeatureConfigurationType.TritonModel,
        ),
        status: fc.constantFrom("READY", "LOADING"),
        alias: fc.string({ minLength: 1, maxLength: 8 }),
        version: fc.constantFrom("1", "2", "3"),
      }),
      { selector: (x) => x.alias, minLength: 1, maxLength: 6 },
    )
    .map((items) =>
      items.map(
        (it, i): FeatureConfiguration => ({
          type: it.type,
          status: it.status,
          modelName: `model-${i}-${it.alias.trim() || "x"}`,
          defaultConfiguration: {
            modelAlias: `${i}-${it.alias.trim() || "a"}`,
            modelVersion: it.version,
          },
        }),
      ),
    );

  it("keeps every non-VLM entry selectable, in sort order, with the same labels", async () => {
    await fc.assert(
      fc.asyncProperty(listArb, async (configs) => {
        cleanup();
        setFeatureConfigs(configs.map((c) => ({ ...c })));

        const options = await renderModelOptions();

        // The observed baseline is exactly what EditWorkflow computes:
        //   featureConfigurations.sort(sortWorkflowModelOptions).map(...)
        const expected = [...configs]
          .sort(sortWorkflowModelOptions)
          .map((c) => ({
            value: c.modelName,
            label: getWorkflowModelOptionLabelWithoutVersion(c),
          }));

        expect(options).toEqual(expected);

        // Every non-VLM entry is present (nothing dropped) — the property the
        // fix must preserve for lists that do not satisfy the bug condition.
        for (const config of configs) {
          expect(options.some((o) => o.value === config.modelName)).toBe(true);
        }
      }),
      { numRuns: 12 },
    );
  }, 120000);
});

/* ------------------------------------------------------------------ */
/* 3.2 — listFeatureConfigurations() still returns VllmModel entries   */
/* ------------------------------------------------------------------ */

describe("feature-config endpoint preservation — other consumers (3.2)", () => {
  it("listFeatureConfigurations() returns the raw list (VllmModel entries kept)", async () => {
    const realAPI = jest.requireActual("api/FeatureConfigurationAPI");
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const axios = require("axios").default;

    const payloadArb = fc
      .uniqueArray(
        fc.record({
          type: fc.constantFrom(
            FeatureConfigurationType.LFVModel,
            FeatureConfigurationType.TritonModel,
            VLLM_FEATURE_TYPE as unknown as FeatureConfigurationType,
          ),
          modelName: fc.string({ minLength: 1, maxLength: 12 }),
        }),
        { selector: (c) => c.modelName, minLength: 1, maxLength: 6 },
      )
      // Bug-condition-adjacent guard: at least one VllmModel entry, so we prove
      // the raw fetch keeps them for non-legacy consumers.
      .filter((list) => list.some((c) => c.type === VLLM_FEATURE_TYPE))
      .map((list) =>
        list.map(
          (c): FeatureConfiguration => ({
            type: c.type,
            status: "READY",
            modelName: c.modelName,
            defaultConfiguration: {},
          }),
        ),
      );

    await fc.assert(
      fc.asyncProperty(payloadArb, async (payload) => {
        const getSpy = jest
          .spyOn(axios, "get")
          .mockResolvedValue({ data: payload });
        try {
          const result = await realAPI.listFeatureConfigurations();
          // The endpoint payload is returned unchanged for other consumers —
          // VllmModel entries survive (exclusion is scoped to legacy options).
          expect(result).toEqual(payload);
          expect(
            result.some(
              (c: FeatureConfiguration) => c.type === VLLM_FEATURE_TYPE,
            ),
          ).toBe(true);
        } finally {
          getSpy.mockRestore();
        }
      }),
      { numRuns: 15 },
    );
  });
});

/* ------------------------------------------------------------------ */
/* 3.6 — an already-assigned non-VLM model still loads and displays    */
/* ------------------------------------------------------------------ */

describe("legacy workflow preservation — assigned non-VLM model (3.6)", () => {
  it("loads and displays a non-VLM model already assigned to the workflow", async () => {
    const assigned: FeatureConfiguration = {
      type: FeatureConfigurationType.LFVModel,
      status: "READY",
      modelName: "assigned-cookie-model",
      defaultConfiguration: { modelAlias: "Cookie", modelVersion: "2" },
    };
    setWorkflow({
      ...EMPTY_WORKFLOW,
      featureConfigurations: [assigned],
    });
    // The available options need not include the assigned model; the assigned
    // model is seeded from the workflow itself.
    setFeatureConfigs([
      {
        type: FeatureConfigurationType.TritonModel,
        status: "READY",
        modelName: "some-other-model",
        defaultConfiguration: { modelAlias: "Other", modelVersion: "1" },
      },
    ]);

    const container = await renderEditor();

    // The assigned model is loaded into the form and displayed with its label.
    // react-hook-form syncs the `values` prop asynchronously, so wait for the
    // selected model to populate.
    await waitFor(() => {
      const selected = container.querySelector(
        '[data-testid="selected-model"]',
      )!;
      expect(selected.getAttribute("data-value")).toBe("assigned-cookie-model");
    });
    const selected = container.querySelector('[data-testid="selected-model"]')!;
    expect(selected.getAttribute("data-label")).toBe(
      getWorkflowModelOptionLabelWithoutVersion(assigned),
    );
  }, 15000);
});
