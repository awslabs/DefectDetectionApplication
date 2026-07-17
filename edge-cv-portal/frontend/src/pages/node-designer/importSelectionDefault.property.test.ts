/**
 * **Feature: workflow-designer-bugfixes, Property 5: Bug Condition —
 * Import selection defaults to none with an explicit gate**
 *
 * _For any_ non-empty module plugin list loading in the import view
 * (including after switching modules), the view SHALL seed the
 * selection empty (0 of N selected) and SHALL keep the import blocked
 * (`formComplete` false, gate message shown) until the user explicitly
 * selects at least one plugin individually or via "Select all".
 *
 * **Validates: Requirements 1.6, 2.7, 2.8**
 *
 * BUG CONDITION EXPLORATION (workflow-designer-bugfixes task 7): this
 * test encodes the EXPECTED behavior and is expected to FAIL on the
 * unfixed code — the module plugin load effect in `ImportView.tsx`
 * seeds `setSelectedModulePlugins(allPluginNames(plugins))`, so every
 * plugin is checked on load and the import proceeds with zero explicit
 * opt-in (isBugCondition3 in design.md). The failing plugin lists are
 * the behavioral counterexamples confirming the bug; the same test
 * validates the fix once the selection seeds empty and the existing
 * gate (`moduleSelectionIncomplete` → `formComplete`) blocks the
 * review until an explicit selection is made.
 *
 * The property drives the real component (RTL over `ImportView`, same
 * mock wiring as ImportView.test.tsx), so a failure is an observable
 * import-view behavior: choose an official module, let its plugin list
 * load, and read the selection count, gate message, and Review-import
 * button state back out of the DOM.
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
import ImportView from './ImportView';
import type { ModulePluginEntry } from './types';

// ------------------------------------------------------------------ mocks
//
// Same wiring as ImportView.test.tsx: the view's collaborators are
// mocked so the module plugin list loads with no network access.

const {
  navigateMock,
  listUseCases,
  listPluginModules,
  listModulePlugins,
  importPlugin,
  selectImportPlugins,
  getVersion,
} = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  listUseCases: vi.fn(),
  listPluginModules: vi.fn(),
  listModulePlugins: vi.fn(),
  importPlugin: vi.fn(),
  selectImportPlugins: vi.fn(),
  getVersion: vi.fn(),
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
    listPluginModules,
    listModulePlugins,
    importPlugin,
    selectImportPlugins,
    getVersion,
  },
}));

const MODULE_A = 'gst-plugins-good';
const MODULE_B = 'gst-plugins-bad';

const MODULES = [
  {
    name: MODULE_A,
    description: 'Well-maintained plugins',
    repoUrl: 'https://gitlab.freedesktop.org/gstreamer/gst-plugins-good.git',
    classification: 'good',
  },
  {
    name: MODULE_B,
    description: 'Plugins lacking upstream review',
    repoUrl: 'https://gitlab.freedesktop.org/gstreamer/gst-plugins-bad.git',
    classification: 'bad',
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'Line A' }],
  });
  listPluginModules.mockResolvedValue({
    modules: MODULES,
    fetchedAt: 1,
    cached: false,
  });
});

// ------------------------------------------------------------ generators

/** Plugin-like names: lowercase alphanumerics, as the listing returns. */
const pluginNameArb = fc.string({
  unit: fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789'.split('')),
  minLength: 1,
  maxLength: 12,
});

/** A NON-EMPTY module plugin list with unique names (isBugCondition3). */
const pluginListArb: fc.Arbitrary<ModulePluginEntry[]> = fc
  .uniqueArray(pluginNameArb, { minLength: 1, maxLength: 8 })
  .map((names) => names.map((name) => ({ name })));

// ---------------------------------------------------------------- helpers

const GATE_MESSAGE = 'Select at least one plugin to import';

/**
 * Render the import view with the given per-module plugin lists,
 * choose `moduleName` from the Module_Listing select, pick one target
 * architecture (so only the selection gate can block the form), and
 * wait for the module's plugin list to render.
 */
async function openModulePluginList(
  pluginsByModule: Record<string, ModulePluginEntry[]>,
  moduleName: string
): Promise<HTMLElement> {
  listModulePlugins.mockImplementation(async (name: string) => ({
    module: name,
    plugins: pluginsByModule[name] ?? [],
    fetchedAt: 1,
    cached: false,
  }));

  const { container } = render(createElement(ImportView));
  await waitFor(() => expect(listPluginModules).toHaveBeenCalled());
  const wrapper = createWrapper(container);

  // Selects: [0] use case, [1] module.
  const moduleSelect = wrapper.findAllSelects()[1];
  moduleSelect.openDropdown();
  moduleSelect.selectOptionByValue(moduleName);

  const archSelect = wrapper.findMultiselect()!;
  archSelect.openDropdown();
  archSelect.selectOptionByValue('x86_64');

  // The chosen module's plugin list has rendered, and the use case
  // restored from context is selected — every formComplete condition
  // other than the plugin selection is satisfied.
  await screen.findByText('Plugins to import');
  await waitFor(() => expect(screen.getByText('Line A')).toBeInTheDocument());
  return container;
}

/** Switch the already-rendered view to another module and wait for its
 *  plugin list to load (the selection must reseed for the new list). */
async function switchModule(
  container: HTMLElement,
  moduleName: string,
  expectedCount: number
): Promise<void> {
  const moduleSelect = createWrapper(container).findAllSelects()[1];
  moduleSelect.openDropdown();
  moduleSelect.selectOptionByValue(moduleName);
  await waitFor(() =>
    expect(screen.getByText(new RegExp(`of ${expectedCount} selected`))).toBeInTheDocument()
  );
}

/** The "X of N selected" counter must read 0 selected, the gate
 *  message must show, and the Review import button must be blocked. */
function expectSeededEmptyAndBlocked(pluginCount: number): void {
  // Seeded empty: 0 of N selected immediately after the list load (2.7).
  expect(
    screen.getByText(`0 of ${pluginCount} selected`),
    'the module plugin selection must seed empty (0 of N selected) on load'
  ).toBeInTheDocument();

  // The explicit-selection gate shows and blocks the review (2.8):
  // formComplete stays false until the user opts in.
  expect(
    screen.getByText(GATE_MESSAGE),
    'the selection gate message must show while nothing is selected'
  ).toBeInTheDocument();
  expect(
    screen.getByRole('button', { name: 'Review import' }),
    'the import must stay blocked until an explicit selection is made'
  ).toBeDisabled();
}

// --------------------------------------------------------------- property

describe('Property 5: Import selection defaults to none with an explicit gate (bug condition exploration)', () => {
  it(
    'seeds the selection empty and blocks the import until at least one ' +
      'plugin is explicitly selected, individually or via Select all ' +
      '(1.6, 2.7, 2.8)',
    async () => {
      await fc.assert(
        fc.asyncProperty(pluginListArb, async (plugins) => {
          try {
            await openModulePluginList({ [MODULE_A]: plugins }, MODULE_A);

            // Immediately after the list load: 0 of N selected,
            // import blocked, gate message shown.
            expectSeededEmptyAndBlocked(plugins.length);

            // Explicit individual opt-in unblocks the import: the
            // first plugin's checkbox (the plugin checkboxes precede
            // the DeepStream toggle in DOM order).
            fireEvent.click(screen.getAllByRole('checkbox')[0]);
            expect(
              screen.getByText(`1 of ${plugins.length} selected`)
            ).toBeInTheDocument();
            expect(screen.queryByText(GATE_MESSAGE)).not.toBeInTheDocument();
            expect(
              screen.getByRole('button', { name: 'Review import' })
            ).toBeEnabled();

            // Clearing re-blocks; "Select all" is the other explicit
            // opt-in path and unblocks with the full selection.
            fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
            expectSeededEmptyAndBlocked(plugins.length);
            fireEvent.click(screen.getByRole('button', { name: 'Select all' }));
            expect(
              screen.getByText(`${plugins.length} of ${plugins.length} selected`)
            ).toBeInTheDocument();
            expect(
              screen.getByRole('button', { name: 'Review import' })
            ).toBeEnabled();
          } finally {
            cleanup();
          }
        }),
        { numRuns: 8 }
      );
    },
    120000
  );

  it(
    'seeds the selection empty again for the new list after switching ' +
      'modules (2.7)',
    async () => {
      await fc.assert(
        fc.asyncProperty(
          pluginListArb,
          pluginListArb,
          async (pluginsA, pluginsB) => {
            try {
              const container = await openModulePluginList(
                { [MODULE_A]: pluginsA, [MODULE_B]: pluginsB },
                MODULE_A
              );
              expectSeededEmptyAndBlocked(pluginsA.length);

              // A selection on the first module must not leak into the
              // next module's freshly loaded list.
              fireEvent.click(screen.getAllByRole('checkbox')[0]);
              await switchModule(container, MODULE_B, pluginsB.length);
              expectSeededEmptyAndBlocked(pluginsB.length);
            } finally {
              cleanup();
            }
          }
        ),
        { numRuns: 6 }
      );
    },
    120000
  );
});
