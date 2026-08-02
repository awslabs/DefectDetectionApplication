/**
 * **Feature: workflow-designer-bugfixes, Property 4: Preservation —
 * Edited rows, advisory guidance, and Port_Scan unchanged**
 *
 * _For any_ port rows that are NOT Untouched_Defaults (any rename,
 * retype, addition, or removal), the wizards SHALL leave the rows
 * exactly unchanged across any sequence of category changes;
 * `guidanceDivergence` and `portsStepErrors` SHALL answer identically
 * to today for all inputs; guidance SHALL remain advisory and
 * non-blocking, with the dismissable divergence advisory still firing;
 * and `applySuggestions` SHALL preserve its
 * replace-over-untouched-defaults / merge-over-edited semantics,
 * including over today's in/out default pair. Non-input categories
 * keep presenting their typical arrangements.
 *
 * **Validates: Requirements 3.6, 3.7, 3.8, 3.9, 3.10**
 *
 * PRESERVATION (workflow-designer-bugfixes task 5): observation-first —
 * every property below encodes behavior observed on the UNFIXED code,
 * so the whole file passes before the Bug 2 fix and must keep passing
 * after it. The generated "user-edited" rows are constrained to rows
 * that deep-equal NO palette category's default arrangement (the fix
 * generalizes Untouched_Defaults to the per-category default shapes,
 * so rows that coincidentally equal another category's defaults are
 * intentionally excluded — their handling is allowed to change).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import * as fc from 'fast-check';
import { createElement } from 'react';
import CreateWizard from './CreateWizard';
import PortGuidancePanel from './PortGuidancePanel';
import { CATEGORY_ARRANGEMENTS, guidanceDivergence } from './portGuidance';
import { applySuggestions, isUntouchedDefaults } from './portScan';
import { portsStepErrors } from './declaration';
import type { PortForm, WizardForm } from './declaration';
import type { PortSuggestion } from './portScan';
import {
  nonEmptyPortSuggestionsArb,
  portListArb,
  portNameArb,
  portSuggestionsArb,
  untouchedDefaultInputs,
  untouchedDefaultOutputs,
} from './portScanArbitraries';
import { CATEGORIES, PORT_TYPES } from './types';
import type { NodeCategory } from './types';

// ------------------------------------------------------------------ mocks
//
// Same wiring as CreateWizard.test.tsx / categoryDefaultPorts: the
// wizard's collaborators are mocked so the steps render with no
// network access.

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

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: {
      user_id: 'u-1',
      email: 'user@example.com',
      username: 'user',
      role: 'UseCaseAdmin',
      is_super_user: false,
    },
  }),
}));

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

// ------------------------------------------- category default shapes

/**
 * The per-category default port arrangements the fix seeds (design
 * "defaultPortsForCategory"): one 'in'/'out' row per arrangement side.
 * Generated "user-edited" rows must deep-equal NONE of these, so the
 * generalized Untouched_Defaults detection keeps answering false for
 * them before AND after the fix.
 */
const CATEGORY_DEFAULT_SHAPES: { inputs: PortForm[]; outputs: PortForm[] }[] =
  CATEGORIES.map((category) => {
    const arrangement = CATEGORY_ARRANGEMENTS[category];
    return {
      inputs:
        arrangement.inputs === 'at-least-one'
          ? [{ name: 'in', portType: 'VideoFrames' }]
          : arrangement.inputs.map((portType) => ({ name: 'in', portType })),
      outputs: arrangement.outputs.map((portType) => ({
        name: 'out',
        portType,
      })),
    };
  });

const sameRows = (a: readonly PortForm[], b: readonly PortForm[]) =>
  a.length === b.length &&
  a.every(
    (port, i) => port.name === b[i].name && port.portType === b[i].portType
  );

const matchesAnyCategoryDefaults = (
  inputs: readonly PortForm[],
  outputs: readonly PortForm[]
) =>
  CATEGORY_DEFAULT_SHAPES.some(
    (shape) => sameRows(shape.inputs, inputs) && sameRows(shape.outputs, outputs)
  );

/** Edited row pairs: any lists that equal no category's defaults. */
const editedRowsArb: fc.Arbitrary<{
  inputs: PortForm[];
  outputs: PortForm[];
}> = fc
  .record({ inputs: portListArb, outputs: portListArb })
  .filter(
    ({ inputs, outputs }) => !matchesAnyCategoryDefaults(inputs, outputs)
  );

// --------------------------------------------------------- RTL helpers

/**
 * Render the create wizard, fill the details step, and walk to the
 * Ports step (initial category 'preprocessing', rows still defaults).
 */
async function openPortsStep(): Promise<HTMLElement> {
  const { container } = render(createElement(CreateWizard));
  await waitFor(() => expect(listUseCases).toHaveBeenCalled());

  const nameInput = container.querySelector(
    'input[placeholder="Blur Regions"]'
  )!;
  fireEvent.change(nameInput, { target: { value: 'Node Under Test' } });

  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  await screen.findByText('Input ports');
  return container;
}

/**
 * One user edit of the port rows, applied on the Ports step. Every
 * edit lands on rows that deep-equal NO category's default
 * arrangement, so the rows are genuinely user-edited under both the
 * current and the generalized Untouched_Defaults notion.
 */
const EDITS = [
  'rename-input',
  'rename-output',
  'add-input',
  'add-output',
  'retype-input',
] as const;
type Edit = (typeof EDITS)[number];

function applyEdit(container: HTMLElement, edit: Edit): void {
  const inputNames = () =>
    container.querySelectorAll('input[placeholder="in"]');
  const outputNames = () =>
    container.querySelectorAll('input[placeholder="out"]');
  switch (edit) {
    case 'rename-input':
      fireEvent.change(inputNames()[0], { target: { value: 'video' } });
      break;
    case 'rename-output':
      fireEvent.change(outputNames()[0], { target: { value: 'result' } });
      break;
    case 'add-input':
      fireEvent.click(screen.getByRole('button', { name: 'Add input port' }));
      fireEvent.change(inputNames()[1], { target: { value: 'aux' } });
      break;
    case 'add-output':
      fireEvent.click(screen.getByRole('button', { name: 'Add output port' }));
      fireEvent.change(outputNames()[1], { target: { value: 'extra' } });
      break;
    case 'retype-input': {
      // Ports-step selects are the port-type selects in DOM order
      // (input rows first); retype input row 0.
      const typeSelect = createWrapper(container).findAllSelects()[0];
      typeSelect.openDropdown();
      typeSelect.selectOptionByValue('EventSignal');
      break;
    }
  }
}

/** Read the presented port rows back out of the Ports step DOM. */
function readRows(container: HTMLElement): {
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
      return (
        PORT_TYPES.find((portType) => text.includes(portType)) ?? text.trim()
      );
    });

  return {
    inputs: inputNames.map((name, i) => ({ name, portType: types[i] })),
    outputs: outputNames.map((name, i) => ({
      name,
      portType: types[inputNames.length + i],
    })),
  };
}

// ----------------------------------------------------- pure-fn oracles

/** Order-insensitive multiset equality of port-type lists. */
const multisetEquals = (
  expected: readonly string[],
  actual: readonly string[]
) =>
  expected.length === actual.length &&
  [...expected].sort().every((value, i) => value === [...actual].sort()[i]);

/**
 * Independent re-statement of today's divergence rule: each side
 * diverges unless its port-type multiset matches the arrangement
 * ('at-least-one' diverges only when empty); both matching → null.
 */
function oracleDivergence(
  category: string,
  inputs: readonly PortForm[],
  outputs: readonly PortForm[]
): { inputs: boolean; outputs: boolean } | null {
  if (!(CATEGORIES as readonly string[]).includes(category)) {
    return null;
  }
  const arrangement = CATEGORY_ARRANGEMENTS[category as NodeCategory];
  const inputsDiverge =
    arrangement.inputs === 'at-least-one'
      ? inputs.length === 0
      : !multisetEquals(
          arrangement.inputs,
          inputs.map((port) => port.portType)
        );
  const outputsDiverge = !multisetEquals(
    arrangement.outputs,
    outputs.map((port) => port.portType)
  );
  return inputsDiverge || outputsDiverge
    ? { inputs: inputsDiverge, outputs: outputsDiverge }
    : null;
}

/** Today's portsStepErrors: one message per blank-named port, in order. */
const oraclePortErrors = (
  inputs: readonly PortForm[],
  outputs: readonly PortForm[]
) =>
  [...inputs, ...outputs].flatMap((port, index) =>
    port.name.trim() ? [] : [`Port ${index + 1} needs a name.`]
  );

const formWith = (
  category: string,
  inputs: PortForm[],
  outputs: PortForm[]
): WizardForm => ({
  name: 'Node Under Test',
  description: '',
  category,
  inputs,
  outputs,
  parameters: [],
  architectures: ['x86_64'],
});

/** Port rows whose names may be blank (portsStepErrors inputs). */
const loosePortListArb: fc.Arbitrary<PortForm[]> = fc.array(
  fc.record({
    name: fc.oneof(fc.constantFrom('', ' ', '\t'), portNameArb),
    portType: fc.constantFrom<string>(...PORT_TYPES),
  }),
  { maxLength: 5 }
);

const categoryArb = fc.constantFrom<string>(...CATEGORIES);

// ------------------------------------------------------------ properties

describe('Property 4: Preservation — edited rows, advisory guidance, and Port_Scan unchanged', () => {
  it(
    'leaves user-edited port rows exactly unchanged across any sequence ' +
      'of category changes (3.6)',
    async () => {
      await fc.assert(
        fc.asyncProperty(
          fc.constantFrom(...EDITS),
          fc.array(fc.constantFrom(...CATEGORIES), {
            minLength: 1,
            maxLength: 3,
          }),
          async (edit, categorySequence) => {
            try {
              const container = await openPortsStep();
              applyEdit(container, edit);
              const edited = readRows(container);

              // The edit produced genuinely user-edited rows: they
              // deep-equal no category's default arrangement.
              expect(
                matchesAnyCategoryDefaults(edited.inputs, edited.outputs)
              ).toBe(false);

              // Walk back to the details step and change the palette
              // category through the generated sequence.
              fireEvent.click(
                screen.getByRole('button', { name: 'Previous' })
              );
              await screen.findByText('Palette category');
              for (const category of categorySequence) {
                const categorySelect =
                  createWrapper(container).findAllSelects()[1];
                categorySelect.openDropdown();
                categorySelect.selectOptionByValue(category);
              }

              // Return to the Ports step: the edited rows survive the
              // category changes byte-for-byte (3.6).
              fireEvent.click(screen.getByRole('button', { name: 'Next' }));
              await screen.findByText('Input ports');
              expect(readRows(container)).toEqual(edited);
            } finally {
              cleanup();
            }
          }
        ),
        { numRuns: 8 }
      );
    },
    180000
  );

  it('guidanceDivergence answers identically to today for any category and rows (3.8, 3.9)', () => {
    fc.assert(
      fc.property(
        categoryArb,
        portListArb,
        portListArb,
        (category, inputs, outputs) => {
          // The divergence rule is exactly today's multiset comparison:
          // the advisory keeps firing for diverging declarations and
          // stays silent for matching ones.
          expect(guidanceDivergence(category, inputs, outputs)).toEqual(
            oracleDivergence(category, inputs, outputs)
          );
        }
      ),
      { numRuns: 100 }
    );
  });

  it('guidanceDivergence stays null for unknown categories', () => {
    fc.assert(
      fc.property(
        fc.constantFrom('', 'unknown', 'Input', 'sink'),
        portListArb,
        portListArb,
        (category, inputs, outputs) => {
          expect(guidanceDivergence(category, inputs, outputs)).toBeNull();
        }
      ),
      { numRuns: 50 }
    );
  });

  it(
    'portsStepErrors answers identically to today and never gates on ' +
      'guidance divergence (3.8)',
    () => {
      fc.assert(
        fc.property(
          categoryArb,
          loosePortListArb,
          loosePortListArb,
          (category, inputs, outputs) => {
            const form = formWith(category, inputs, outputs);
            const errors = portsStepErrors(form);

            // Exactly today's blank-name messages, in row order.
            expect(errors).toEqual(oraclePortErrors(inputs, outputs));

            // Guidance is advisory and non-blocking: when every row is
            // named, the step is clean even for declarations diverging
            // from the category's arrangement (any valid arrangement is
            // accepted).
            const allNamed = [...inputs, ...outputs].every((port) =>
              port.name.trim()
            );
            if (allNamed) {
              expect(errors).toEqual([]);
            }
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'the dismissable divergence advisory keeps firing exactly when the ' +
      'declaration diverges, and dismissing never blocks (3.8, 3.9)',
    () => {
      fc.assert(
        fc.property(
          categoryArb,
          portListArb,
          portListArb,
          (category, inputs, outputs) => {
            try {
              render(
                createElement(PortGuidancePanel, {
                  category,
                  inputs,
                  outputs,
                })
              );
              const alert = screen.queryByTestId(
                'port-guidance-divergence-alert'
              );
              const divergence = guidanceDivergence(
                category,
                inputs,
                outputs
              );

              if (divergence === null) {
                expect(alert).toBeNull();
              } else {
                // The advisory fires, states it is only guidance, and
                // dismisses away without removing the panel content.
                expect(alert).not.toBeNull();
                expect(alert!.textContent).toContain('This is only guidance');
                fireEvent.click(within(alert!).getByRole('button'));
                expect(
                  screen.queryByTestId('port-guidance-divergence-alert')
                ).toBeNull();
                expect(
                  screen.queryByTestId('port-guidance-arrangement')
                ).not.toBeNull();
              }
            } finally {
              cleanup();
            }
          }
        ),
        { numRuns: 25 }
      );
    }
  );

  it(
    "non-input categories keep presenting their typical arrangements' " +
      'expected input and output ports (3.7)',
    () => {
      fc.assert(
        fc.property(
          fc.constantFrom<NodeCategory>(
            'preprocessing',
            'inference',
            'post_processing',
            'output'
          ),
          (category) => {
            try {
              render(
                createElement(PortGuidancePanel, {
                  category,
                  inputs: [],
                  outputs: [],
                })
              );
              // The arrangement box presents the category's expected
              // input and output ports per its typical arrangement.
              expect(
                screen.getByTestId('port-guidance-arrangement').textContent
              ).toBe(CATEGORY_ARRANGEMENTS[category].summary);
            } finally {
              cleanup();
            }
          }
        ),
        { numRuns: 8 }
      );
    }
  );

  it(
    "applySuggestions keeps replacing today's untouched in/out default " +
      'pair wholesale with the suggestions (3.10)',
    () => {
      fc.assert(
        fc.property(nonEmptyPortSuggestionsArb, (suggestions) => {
          const inputs = untouchedDefaultInputs();
          const outputs = untouchedDefaultOutputs();

          // Today's in/out default pair keeps counting as untouched.
          expect(isUntouchedDefaults(inputs, outputs)).toBe(true);

          const result = applySuggestions(
            inputs,
            outputs,
            suggestions,
            isUntouchedDefaults(inputs, outputs)
          );

          // Replacement: each side is exactly the suggestions of that
          // direction, in order; nothing lands in alreadyDeclared.
          expect(result.inputs).toEqual(
            suggestions
              .filter((s) => s.direction === 'input')
              .map((s) => ({ name: s.name, portType: s.portType }))
          );
          expect(result.outputs).toEqual(
            suggestions
              .filter((s) => s.direction === 'output')
              .map((s) => ({ name: s.name, portType: s.portType }))
          );
          expect(result.applied).toEqual(suggestions.map((s) => s.name));
          expect(result.alreadyDeclared).toEqual([]);
          expect(result.unconfirmed).toEqual(
            suggestions.filter((s) => !s.confident).map((s) => s.name)
          );
        }),
        { numRuns: 100 }
      );
    }
  );

  it(
    'applySuggestions keeps merging additively over user-edited rows: ' +
      'edits win, only the genuinely new names are appended (3.10)',
    () => {
      fc.assert(
        fc.property(
          editedRowsArb,
          portSuggestionsArb,
          ({ inputs, outputs }, suggestions) => {
            const inputsBefore = inputs.map((port) => ({ ...port }));
            const outputsBefore = outputs.map((port) => ({ ...port }));

            // Genuinely user-edited rows keep counting as touched.
            expect(isUntouchedDefaults(inputs, outputs)).toBe(false);

            const result = applySuggestions(
              inputs,
              outputs,
              suggestions,
              isUntouchedDefaults(inputs, outputs)
            );

            // Every existing port stays unchanged and in place: the
            // originals form the exact prefix of each merged side.
            expect(result.inputs.slice(0, inputs.length)).toEqual(
              inputsBefore
            );
            expect(result.outputs.slice(0, outputs.length)).toEqual(
              outputsBefore
            );

            // Trimmed-name collisions are reported already declared;
            // everything else is appended to its side in suggestion
            // order and reported applied.
            const declared = new Set(
              [...inputsBefore, ...outputsBefore].map((port) =>
                port.name.trim()
              )
            );
            const colliding: PortSuggestion[] = [];
            const fresh: PortSuggestion[] = [];
            for (const suggestion of suggestions) {
              const trimmed = suggestion.name.trim();
              if (declared.has(trimmed)) {
                colliding.push(suggestion);
              } else {
                declared.add(trimmed);
                fresh.push(suggestion);
              }
            }
            expect(result.alreadyDeclared).toEqual(
              colliding.map((s) => s.name.trim())
            );
            expect(result.inputs.slice(inputs.length)).toEqual(
              fresh
                .filter((s) => s.direction === 'input')
                .map((s) => ({ name: s.name, portType: s.portType }))
            );
            expect(result.outputs.slice(outputs.length)).toEqual(
              fresh
                .filter((s) => s.direction === 'output')
                .map((s) => ({ name: s.name, portType: s.portType }))
            );
            expect(result.applied).toEqual(fresh.map((s) => s.name));

            // An empty suggestion list leaves the lists identical.
            if (suggestions.length === 0) {
              expect(result.inputs).toEqual(inputsBefore);
              expect(result.outputs).toEqual(outputsBefore);
            }
          }
        ),
        { numRuns: 100 }
      );
    }
  );
});
