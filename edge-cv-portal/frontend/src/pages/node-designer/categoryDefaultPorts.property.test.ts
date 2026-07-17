/**
 * **Feature: workflow-designer-bugfixes, Property 3: Bug Condition —
 * Default ports follow the selected category**
 *
 * _For any_ palette category in `CATEGORY_ARRANGEMENTS` selected in the
 * create wizard while the port rows are Untouched_Defaults, the wizard
 * SHALL present default port rows whose port-type multisets exactly
 * match that category's typical arrangement (in particular: category
 * `input` → zero input rows and one VideoFrames output; `output` → at
 * least one input row and zero outputs), every seeded row SHALL carry a
 * non-empty name (so `portsStepErrors` stays clean),
 * `guidanceDivergence(category, inputs, outputs)` SHALL be null for the
 * seeded rows, and the ports step SHALL state that category's
 * input/output requirements.
 *
 * **Validates: Requirements 1.4, 1.5, 2.4, 2.5, 2.6**
 *
 * BUG CONDITION EXPLORATION (workflow-designer-bugfixes task 4): this
 * test encodes the EXPECTED behavior and is expected to FAIL on the
 * unfixed code — the wizards seed one "in" input and one "out" output
 * regardless of the selected palette category and never rewrite the
 * untouched default rows when the category changes (isBugCondition2 in
 * design.md). The failing categories are the behavioral
 * counterexamples confirming the bug; the same test validates the fix
 * once the wizards derive their default rows from the category.
 *
 * The property drives the real component (RTL over `CreateWizard`), so
 * a failure is an observable wizard behavior, not a missing import:
 * select the category on the details step, walk to the Ports step, and
 * read the presented port rows back out of the DOM.
 *
 * Requirement 2.6 is asserted against the interface the design defines
 * for the fix: a `data-testid="port-guidance-requirements"` element on
 * the ports step (rendered by `PortGuidancePanel` from the new
 * `arrangementRequirements(category)` helper) stating the category's
 * input and output requirements.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import * as fc from 'fast-check';
import { createElement } from 'react';
import CreateWizard from './CreateWizard';
import { CATEGORY_ARRANGEMENTS, guidanceDivergence } from './portGuidance';
import { CATEGORIES, PORT_TYPES } from './types';
import type { PortForm } from './declaration';

// ------------------------------------------------------------------ mocks
//
// Same wiring as CreateWizard.test.tsx: the wizard's collaborators are
// mocked so the Ports step renders with no network access.

const { navigateMock, listUseCases, createScaffoldPlugin } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  listUseCases: vi.fn(),
  createScaffoldPlugin: vi.fn(),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}));

vi.mock('../../services/api', () => {
  class ApiError extends Error {
    constructor(
      message: string,
      public readonly status?: number,
      public readonly code?: string,
      public readonly details?: Record<string, unknown>
    ) {
      super(message);
      this.name = 'ApiError';
    }
  }
  return { ApiError, apiService: { listUseCases } };
});

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: () => ({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  }),
}));

vi.mock('./api', () => ({
  nodeDesignerApi: {
    createScaffoldPlugin,
    getGstProperties: vi.fn(),
    putVersionSource: vi.fn(),
    startBuilds: vi.fn(),
  },
}));

vi.mock('./zip', () => ({
  downloadZip: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'Line A' }],
  });
});

// ---------------------------------------------------------------- helpers

/**
 * Render the create wizard, select the palette category on the details
 * step (the port rows stay Untouched_Defaults — the user never edits
 * them), and walk to the Ports step.
 */
async function openPortsStepWithCategory(
  category: string
): Promise<HTMLElement> {
  const { container } = render(createElement(CreateWizard));
  await waitFor(() => expect(listUseCases).toHaveBeenCalled());

  // Name is required to leave the details step.
  const nameInput = container.querySelector(
    'input[placeholder="Blur Regions"]'
  )!;
  fireEvent.change(nameInput, { target: { value: 'Node Under Test' } });

  // Details-step selects: [0] use case, [1] palette category.
  const categorySelect = createWrapper(container).findAllSelects()[1];
  categorySelect.openDropdown();
  categorySelect.selectOptionByValue(category);

  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  await screen.findByText('Input ports');
  return container;
}

/**
 * Read the presented port rows back out of the Ports step DOM: input
 * rows are the name inputs with placeholder "in", output rows those
 * with placeholder "out"; the port-type selects follow in DOM order
 * (all input types, then all output types).
 */
function readSeededRows(container: HTMLElement): {
  inputs: PortForm[];
  outputs: PortForm[];
} {
  const inputNames = Array.from(
    container.querySelectorAll('input[placeholder="in"]')
  ).map((el) => (el as HTMLInputElement).value);
  const outputNames = Array.from(
    container.querySelectorAll('input[placeholder="out"]')
  ).map((el) => (el as HTMLInputElement).value);

  const types = createWrapper(container)
    .findAllSelects()
    .map((select) => {
      const text = select.findTrigger().getElement().textContent ?? '';
      return PORT_TYPES.find((portType) => text.includes(portType)) ?? text.trim();
    });

  return {
    inputs: inputNames.map((name, i) => ({ name, portType: types[i] })),
    outputs: outputNames.map((name, i) => ({
      name,
      portType: types[inputNames.length + i],
    })),
  };
}

const multiset = (ports: readonly PortForm[]) =>
  ports.map((port) => port.portType).sort();

// --------------------------------------------------------------- property

describe('Property 3: Default ports follow the selected category (bug condition exploration)', () => {
  it(
    'presents default port rows whose port-type multisets match the ' +
      "category's typical arrangement, with non-empty names and no " +
      'guidance divergence (1.4, 1.5, 2.4, 2.5)',
    async () => {
      await fc.assert(
        fc.asyncProperty(fc.constantFrom(...CATEGORIES), async (category) => {
          try {
            const container = await openPortsStepWithCategory(category);
            const { inputs, outputs } = readSeededRows(container);
            const arrangement = CATEGORY_ARRANGEMENTS[category];

            // The presented default rows' port-type multisets match the
            // category's typical arrangement (2.4, 2.5). The output
            // category's 'at-least-one' input side seeds at least one
            // input row; its output side must be empty.
            if (arrangement.inputs === 'at-least-one') {
              expect(
                inputs.length,
                `category ${category}: expected at least one seeded input row`
              ).toBeGreaterThanOrEqual(1);
            } else {
              expect(
                multiset(inputs),
                `category ${category}: seeded input port types`
              ).toEqual([...arrangement.inputs].sort());
            }
            expect(
              multiset(outputs),
              `category ${category}: seeded output port types`
            ).toEqual([...arrangement.outputs].sort());

            // Every seeded row carries a non-empty name, so
            // portsStepErrors stays clean on the untouched defaults.
            for (const port of [...inputs, ...outputs]) {
              expect(
                port.name.trim(),
                `category ${category}: seeded rows need non-empty names`
              ).not.toBe('');
            }

            // The seeded rows match the arrangement, so the divergence
            // advisory has nothing to flag.
            expect(
              guidanceDivergence(category, inputs, outputs),
              `category ${category}: seeded defaults must not diverge`
            ).toBeNull();
          } finally {
            cleanup();
          }
        }),
        { numRuns: 10 }
      );
    },
    120000
  );

  it(
    "states the selected category's input/output requirements on the " +
      'ports step (2.6)',
    async () => {
      await fc.assert(
        fc.asyncProperty(fc.constantFrom(...CATEGORIES), async (category) => {
          try {
            await openPortsStepWithCategory(category);

            // The design's fix interface: PortGuidancePanel renders the
            // per-kind requirements under
            // data-testid="port-guidance-requirements".
            const requirements = screen.queryByTestId(
              'port-guidance-requirements'
            );
            expect(
              requirements,
              `category ${category}: the ports step must state the ` +
                "category's input/output requirements"
            ).not.toBeNull();
            expect(requirements!.textContent).toMatch(/inputs/i);
            expect(requirements!.textContent).toMatch(/outputs/i);
          } finally {
            cleanup();
          }
        }),
        { numRuns: 6 }
      );
    },
    120000
  );
});
