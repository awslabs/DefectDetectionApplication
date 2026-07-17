/**
 * **Feature: workflow-designer-bugfixes, Property 6: Preservation —
 * Import serialization and fallbacks unchanged**
 *
 * _For any_ selection state, the fixed view SHALL serialize exactly as
 * the original: a proper subset serializes to that subset as
 * `selected_plugins`; a full selection (or, when no list is available,
 * no selection) serializes to no `selected_plugins` parameter
 * (whole-module import); an unavailable or empty plugin list SHALL
 * never block the import; and the post-fetch `pending_selection`
 * dialog SHALL keep its default-none, at-least-one-required behavior.
 *
 * **Validates: Requirements 3.11, 3.12, 3.13, 3.14**
 *
 * PRESERVATION (workflow-designer-bugfixes task 8): observation-first —
 * these properties encode the UNFIXED behavior observed in
 * `importFlow.ts` (`selectedPluginsParam`, `moduleSelectionSummary`,
 * `pluginSelectionError`) and `ImportView.tsx` (non-blocking plugin-list
 * fallback, `pending_selection` dialog). They MUST pass on the unfixed
 * code, and MUST STILL pass unchanged after the Bug 3 fix (task 9.3):
 * the fix only reseeds the module plugin selection default; none of the
 * behavior asserted here may move.
 *
 * Pure serialization rules are property-tested directly; the
 * component-level fallback and dialog behavior drive the real
 * `ImportView` via RTL with the same mock wiring as ImportView.test.tsx.
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
import { ApiError } from '../../services/api';
import {
  moduleSelectionSummary,
  pluginSelectionError,
  selectedPluginsParam,
} from './importFlow';
import type { EnumeratedPlugin } from './types';

// ------------------------------------------------------------------ mocks
//
// Same wiring as ImportView.test.tsx: the view's collaborators are
// mocked so the flows run with no network access.

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

const MODULE_GOOD = 'gst-plugins-good';

const MODULES = [
  {
    name: MODULE_GOOD,
    description: 'Well-maintained plugins',
    repoUrl: 'https://gitlab.freedesktop.org/gstreamer/gst-plugins-good.git',
    classification: 'good',
  },
];

const IMPORTED_RESPONSE = {
  plugin: { plugin_id: 'plugin-p6', version: 1, import_status: 'imported' },
  import: { status: 'imported' },
};

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

/** An available module plugin listing (unique names, listing order). */
const availableArb = fc.uniqueArray(pluginNameArb, {
  minLength: 1,
  maxLength: 20,
});

/**
 * A listing of >= 2 plugins together with a PROPER non-empty subset of
 * it selected in arbitrary (user click) order — the Req 3.11 case.
 */
const properSubsetCaseArb = fc
  .uniqueArray(pluginNameArb, { minLength: 2, maxLength: 20 })
  .chain((available) =>
    fc.record({
      available: fc.constant(available),
      selected: fc.shuffledSubarray(available, {
        minLength: 1,
        maxLength: available.length - 1,
      }),
    })
  );

/** A listing plus extra names guaranteed NOT to be in the listing. */
const withUnknownNamesArb = fc
  .uniqueArray(pluginNameArb, { minLength: 2, maxLength: 24 })
  .chain((pool) =>
    fc
      .integer({ min: 1, max: pool.length - 1 })
      .chain((cut) =>
        fc.record({
          available: fc.constant(pool.slice(0, cut)),
          unknown: fc.constant(pool.slice(cut)),
          selected: fc.shuffledSubarray(pool.slice(0, cut)),
        })
      )
  );

/** How the unfixed summary spells a partial selection (observed:
 *  names in listing order, capped at 8 with a "+K more" suffix). */
const SUMMARY_MAX_NAMES = 8;

// ---------------------------------------------------------------------
// Pure serialization rules (Req 3.11, 3.12) — observed on unfixed code:
// selectedPluginsParam returns the normalized (listing-ordered) proper
// subset, and undefined for a full selection, an empty selection, or
// an unavailable listing; moduleSelectionSummary mirrors that split.
// ---------------------------------------------------------------------

describe('Property 6: selected_plugins serialization unchanged (pure)', () => {
  it('serializes a proper subset to exactly that subset in listing order (3.11)', () => {
    fc.assert(
      fc.property(properSubsetCaseArb, ({ available, selected }) => {
        const chosen = new Set(selected);
        const expected = available.filter((name) => chosen.has(name));

        expect(selectedPluginsParam(selected, available)).toEqual(expected);

        // The confirm-step summary spells the same subset out.
        const shown = expected.slice(0, SUMMARY_MAX_NAMES).join(', ');
        const more =
          expected.length > SUMMARY_MAX_NAMES
            ? `, +${expected.length - SUMMARY_MAX_NAMES} more`
            : '';
        expect(moduleSelectionSummary(selected, available)).toBe(
          `${expected.length} of ${available.length} plugins: ${shown}${more}`
        );
      })
    );
  });

  it('serializes a full selection to no selected_plugins parameter — whole-module import (3.12)', () => {
    fc.assert(
      fc.property(
        availableArb.chain((available) =>
          fc.record({
            available: fc.constant(available),
            // Every plugin selected, in arbitrary click order.
            selected: fc.shuffledSubarray(available, {
              minLength: available.length,
            }),
          })
        ),
        ({ available, selected }) => {
          expect(selectedPluginsParam(selected, available)).toBeUndefined();
          expect(moduleSelectionSummary(selected, available)).toBe(
            'All plugins'
          );
        }
      )
    );
  });

  it('serializes an empty selection or an unavailable listing to no selected_plugins parameter (3.12)', () => {
    fc.assert(
      fc.property(availableArb, (available) => {
        // Empty selection over an available listing.
        expect(selectedPluginsParam([], available)).toBeUndefined();
        expect(moduleSelectionSummary([], available)).toBe('All plugins');
      })
    );
    fc.assert(
      fc.property(fc.uniqueArray(pluginNameArb, { maxLength: 8 }), (selected) => {
        // No listing available at all: whatever is "selected" is moot.
        expect(selectedPluginsParam(selected, [])).toBeUndefined();
        expect(moduleSelectionSummary(selected, [])).toBe('All plugins');
      })
    );
  });

  it('ignores names outside the listing: serialization is decided by the known subset alone (3.11, 3.12)', () => {
    fc.assert(
      fc.property(withUnknownNamesArb, ({ available, unknown, selected }) => {
        const mixed = [...selected, ...unknown];
        expect(selectedPluginsParam(mixed, available)).toEqual(
          selectedPluginsParam(selected, available)
        );
        expect(moduleSelectionSummary(mixed, available)).toBe(
          moduleSelectionSummary(selected, available)
        );
      })
    );
  });
});

// ---------------------------------------------------------------------
// pending_selection dialog validation rule (Req 3.14, pure part) —
// observed on unfixed code: an empty selection is rejected with the
// at-least-one gate message, names outside the enumeration are
// rejected by name, and any non-empty enumerated subset is accepted.
// ---------------------------------------------------------------------

describe('Property 6: pending_selection at-least-one-required rule unchanged (pure)', () => {
  const foundArb: fc.Arbitrary<EnumeratedPlugin[]> = fc
    .uniqueArray(pluginNameArb, { minLength: 1, maxLength: 12 })
    .map((names) => names.map((name) => ({ name, path: `gst/${name}` })));

  it('rejects an empty selection with the at-least-one gate message (3.14)', () => {
    fc.assert(
      fc.property(foundArb, (found) => {
        expect(pluginSelectionError([], found)).toBe(
          'Select at least one plugin to import'
        );
      })
    );
  });

  it('accepts any non-empty subset of the enumerated plugins (3.14)', () => {
    fc.assert(
      fc.property(
        foundArb.chain((found) =>
          fc.record({
            found: fc.constant(found),
            selected: fc.shuffledSubarray(
              found.map((entry) => entry.name),
              { minLength: 1 }
            ),
          })
        ),
        ({ found, selected }) => {
          expect(pluginSelectionError(selected, found)).toBeNull();
        }
      )
    );
  });

  it('rejects selections naming plugins outside the enumeration (3.14)', () => {
    fc.assert(
      fc.property(withUnknownNamesArb, ({ available, unknown, selected }) => {
        const found = available.map((name) => ({
          name,
          path: `gst/${name}`,
        }));
        const mixed = [...selected, ...unknown];
        expect(pluginSelectionError(mixed, found)).toBe(
          `Unknown plugins: ${unknown.join(', ')}`
        );
      })
    );
  });
});

// ---------------------------------------------------------------------
// Component-level fallback (Req 3.13) — observed on unfixed code: a
// module plugin list that fails to load (distinct listing-unavailable
// code or any other error) or comes back empty never blocks the
// import; the whole-module import proceeds with no selected_plugins.
// These flows never render a non-empty selection list, so they are
// independent of the Bug 3 default-seeding change and must behave
// identically before and after the fix.
// ---------------------------------------------------------------------

type FallbackScenario = 'listing-unavailable' | 'generic-error' | 'empty-list';

const fallbackScenarioArb = fc.constantFrom<FallbackScenario>(
  'listing-unavailable',
  'generic-error',
  'empty-list'
);

/** Select the module and one Target_Architecture on the form view. */
async function fillForm(container: HTMLElement): Promise<void> {
  await waitFor(() => expect(listPluginModules).toHaveBeenCalled());
  const wrapper = createWrapper(container);

  // Selects: [0] use case, [1] module.
  const moduleSelect = wrapper.findAllSelects()[1];
  moduleSelect.openDropdown();
  moduleSelect.selectOptionByValue(MODULE_GOOD);

  const archSelect = wrapper.findMultiselect()!;
  archSelect.openDropdown();
  archSelect.selectOptionByValue('x86_64');

  await waitFor(() => expect(screen.getByText('Line A')).toBeInTheDocument());
}

describe('Property 6: unavailable or empty plugin list never blocks the import (component)', () => {
  it(
    'keeps the whole-module import available and sends no selected_plugins (3.13)',
    async () => {
      await fc.assert(
        fc.asyncProperty(fallbackScenarioArb, async (scenario) => {
          try {
            if (scenario === 'listing-unavailable') {
              listModulePlugins.mockRejectedValue(
                new ApiError(
                  "The plugin list for module 'gst-plugins-good' could not be retrieved",
                  502,
                  'MODULE_LISTING_UNAVAILABLE'
                )
              );
            } else if (scenario === 'generic-error') {
              listModulePlugins.mockRejectedValue(new Error('network down'));
            } else {
              listModulePlugins.mockResolvedValue({
                module: MODULE_GOOD,
                plugins: [],
                fetchedAt: 1,
                cached: false,
              });
            }
            importPlugin.mockResolvedValue(IMPORTED_RESPONSE);

            const { container } = render(createElement(ImportView));
            await fillForm(container);
            await waitFor(() =>
              expect(listModulePlugins).toHaveBeenCalledWith(MODULE_GOOD)
            );

            if (scenario !== 'empty-list') {
              // The failure surfaces as a non-blocking warning.
              await screen.findByText('Plugin list unavailable');
            }

            // No selection gate: no plugin list rendered, no gate
            // message, and the review stays available.
            expect(
              screen.queryByText('Select at least one plugin to import')
            ).not.toBeInTheDocument();
            await waitFor(() =>
              expect(
                screen.getByRole('button', { name: 'Review import' })
              ).toBeEnabled()
            );

            // The whole-module import proceeds: 'All plugins' on the
            // confirm step and no selected_plugins on the wire.
            fireEvent.click(
              screen.getByRole('button', { name: 'Review import' })
            );
            await screen.findByText('Upstream classification');
            expect(screen.getByText('All plugins')).toBeInTheDocument();
            fireEvent.click(
              screen.getByRole('button', { name: 'Import plugin' })
            );
            await waitFor(() => expect(importPlugin).toHaveBeenCalled());
            expect(
              importPlugin.mock.calls[0][0].selected_plugins
            ).toBeUndefined();
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

// ---------------------------------------------------------------------
// pending_selection dialog (Req 3.14, component) — observed on unfixed
// code: the post-fetch selection dialog seeds with NO plugins selected
// (setSelectedPlugins([]) in settleImport) and keeps "Import selected
// plugins" blocked until at least one plugin is checked. The module
// plugin list resolves empty here so the flow is identical before and
// after the Bug 3 fix (the fix touches only the module-list seeding).
// ---------------------------------------------------------------------

const enumeratedListArb: fc.Arbitrary<EnumeratedPlugin[]> = fc
  .uniqueArray(pluginNameArb, { minLength: 1, maxLength: 6 })
  .map((names) => names.map((name) => ({ name, path: `gst/${name}` })));

describe('Property 6: pending_selection dialog default-none behavior unchanged (component)', () => {
  it(
    'opens with no plugins selected and requires at least one before submission (3.14)',
    async () => {
      await fc.assert(
        fc.asyncProperty(enumeratedListArb, async (found) => {
          try {
            listModulePlugins.mockResolvedValue({
              module: MODULE_GOOD,
              plugins: [],
              fetchedAt: 1,
              cached: false,
            });
            importPlugin.mockResolvedValue({
              plugin: {
                plugin_id: 'plugin-p6',
                version: 1,
                import_status: 'pending_selection',
                plugins_found: found,
              },
              import: { status: 'pending_selection' },
            });
            selectImportPlugins.mockResolvedValue({
              plugin: {
                plugin_id: 'plugin-p6',
                version: 1,
                import_status: 'imported',
              },
              import: { status: 'imported' },
            });

            const { container } = render(createElement(ImportView));
            await fillForm(container);
            fireEvent.click(
              screen.getByRole('button', { name: 'Review import' })
            );
            await screen.findByText('Upstream classification');
            fireEvent.click(
              screen.getByRole('button', { name: 'Import plugin' })
            );

            // The dialog opens defaulting to NO selection, with the
            // confirmation blocked (at-least-one-required).
            await screen.findByText('Select plugins to import');
            expect(
              screen.getByText(`0 of ${found.length} selected`),
              'the pending_selection dialog must default to no plugins selected'
            ).toBeInTheDocument();
            expect(
              screen.getByRole('button', { name: 'Import selected plugins' }),
              'submission must stay blocked while nothing is selected'
            ).toBeDisabled();

            // Checking one plugin satisfies the at-least-one rule.
            fireEvent.click(screen.getAllByRole('checkbox')[0]);
            expect(
              screen.getByText(`1 of ${found.length} selected`)
            ).toBeInTheDocument();
            expect(
              screen.getByRole('button', { name: 'Import selected plugins' })
            ).toBeEnabled();

            // "Select none" re-blocks — the rule keeps holding.
            fireEvent.click(
              screen.getByRole('button', { name: 'Select none' })
            );
            expect(
              screen.getByText(`0 of ${found.length} selected`)
            ).toBeInTheDocument();
            expect(
              screen.getByRole('button', { name: 'Import selected plugins' })
            ).toBeDisabled();
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
