/**
 * Unit tests for the import-flow helpers (custom-node-designer task
 * 12.3, Requirements 5.1, 6.3, 15.1, 15.2, 15.3, 15.7).
 */
import { describe, expect, it } from 'vitest';
import { ApiError } from '../../services/api';
import {
  addAllToSelection,
  adjustRevisionError,
  archRevisionEntries,
  archRevisionLabel,
  archRevisionsParam,
  canAdjustRevision,
  CLASSIFICATION_EXPLANATIONS,
  classifyPluginSet,
  filterPluginEntries,
  GSTREAMER_DOCS_URL,
  GSTREAMER_PLUGIN_SETS_DOCS_URL,
  IMPORT_POLL_TIMEOUT_MS,
  importedPluginsLabel,
  importedPluginsSummary,
  importPollDecision,
  incompatiblePlatformWarnings,
  isModuleListingUnavailable,
  platformWarningMessage,
  pluginDocsUrl,
  pluginEntryDescription,
  pluginSelectionError,
  requiresAcknowledgment,
  restrictArchitectureSelection,
  selectableArchitectures,
  togglePluginSelection,
} from './importFlow';
import type {
  EnumeratedPlugin,
  PlatformCompatibilityEntry,
  PluginVersionDetail,
} from './types';

describe('CLASSIFICATION_EXPLANATIONS', () => {
  it('presents the fixed plain-language explanation per value (15.3)', () => {
    expect(CLASSIFICATION_EXPLANATIONS.good).toBe(
      'good indicates a well-maintained, well-tested, properly licensed plugin set'
    );
    expect(CLASSIFICATION_EXPLANATIONS.bad).toBe(
      'bad indicates a plugin set lacking upstream review, testing, or active maintenance'
    );
    expect(CLASSIFICATION_EXPLANATIONS.ugly).toBe(
      'ugly indicates a plugin set of good quality that carries licensing or distribution concerns'
    );
    expect(CLASSIFICATION_EXPLANATIONS.unclassified).toBe(
      'unclassified indicates a plugin outside the official GStreamer plugin sets that warrants the highest caution'
    );
  });
});

describe('requiresAcknowledgment', () => {
  it('requires acknowledgment for bad, ugly, and unclassified (15.7)', () => {
    expect(requiresAcknowledgment('bad')).toBe(true);
    expect(requiresAcknowledgment('ugly')).toBe(true);
    expect(requiresAcknowledgment('unclassified')).toBe(true);
  });

  it('lets good imports proceed without acknowledgment (15.7)', () => {
    expect(requiresAcknowledgment('good')).toBe(false);
  });

  it('treats a missing classification as requiring acknowledgment', () => {
    expect(requiresAcknowledgment(null)).toBe(true);
    expect(requiresAcknowledgment(undefined)).toBe(true);
  });
});

describe('selectableArchitectures', () => {
  it('restricts DeepStream imports to arm64 JetPack 4/5/6 (5.1)', () => {
    expect(selectableArchitectures(true)).toEqual([
      'arm64_jp4',
      'arm64_jp5',
      'arm64_jp6',
    ]);
  });

  it('offers all six Target_Architectures otherwise', () => {
    expect(selectableArchitectures(false)).toEqual([
      'x86_64',
      'x86_64_nvidia',
      'arm64_jp4',
      'arm64_jp5',
      'arm64_jp6',
      'arm64_jp7',
    ]);
  });
});

describe('restrictArchitectureSelection', () => {
  it('drops non-Jetson selections when the DeepStream toggle turns on (5.1)', () => {
    expect(
      restrictArchitectureSelection(['x86_64', 'arm64_jp5', 'x86_64_nvidia'], true)
    ).toEqual(['arm64_jp5']);
  });

  it('keeps the selection unchanged when DeepStream is off', () => {
    expect(restrictArchitectureSelection(['x86_64', 'arm64_jp4'], false)).toEqual([
      'x86_64',
      'arm64_jp4',
    ]);
  });
});

describe('classifyPluginSet', () => {
  it('classifies the official plugin-set module names', () => {
    expect(classifyPluginSet('gst-plugins-good', null)).toBe('good');
    expect(classifyPluginSet('gst-plugins-bad', null)).toBe('bad');
    expect(classifyPluginSet('gst-plugins-ugly', null)).toBe('ugly');
  });

  it('classifies known freedesktop.org repository locations', () => {
    expect(
      classifyPluginSet(null, 'https://gitlab.freedesktop.org/gstreamer/gst-plugins-good.git')
    ).toBe('good');
    expect(
      classifyPluginSet(
        null,
        'https://gitlab.freedesktop.org/gstreamer/gstreamer/-/tree/main/subprojects/gst-plugins-bad'
      )
    ).toBe('bad');
    expect(
      classifyPluginSet(null, 'https://gstreamer.freedesktop.org/src/gst-plugins-ugly/')
    ).toBe('ugly');
  });

  it('never guesses arbitrary public repositories into an official set (15.4)', () => {
    expect(classifyPluginSet(null, 'https://github.com/someone/gst-plugins-good')).toBe(
      'unclassified'
    );
    expect(classifyPluginSet('my-plugin', 'https://example.com/repo.git')).toBe(
      'unclassified'
    );
    expect(classifyPluginSet(null, null)).toBe('unclassified');
    expect(classifyPluginSet(null, 'not a url')).toBe('unclassified');
  });
});

describe('isModuleListingUnavailable', () => {
  it('recognizes the distinct MODULE_LISTING_UNAVAILABLE code (6.3)', () => {
    const err = new ApiError('listing down', 502, 'MODULE_LISTING_UNAVAILABLE');
    expect(isModuleListingUnavailable(err)).toBe(true);
  });

  it('rejects other errors', () => {
    expect(isModuleListingUnavailable(new Error('network'))).toBe(false);
    expect(isModuleListingUnavailable(new ApiError('forbidden', 403, 'FORBIDDEN'))).toBe(
      false
    );
  });
});

// -------------------------------------------- plugin-set selection

const FOUND: EnumeratedPlugin[] = [
  { name: 'jpeg', path: 'ext/jpeg' },
  { name: 'rtp', path: 'gst/rtp' },
  { name: 'udp', path: 'gst/udp' },
];

// ------------------------------------------- asynchronous fetch flow

describe('importPollDecision', () => {
  it('keeps waiting while the fetch runs', () => {
    expect(importPollDecision({ import_status: 'fetching' }, 0)).toEqual({
      kind: 'wait',
    });
    expect(
      importPollDecision({ import_status: 'fetching' }, IMPORT_POLL_TIMEOUT_MS - 1)
    ).toEqual({ kind: 'wait' });
  });

  it('gives up at the ~12 minute poll bound (fetch project times out at 10)', () => {
    expect(
      importPollDecision({ import_status: 'fetching' }, IMPORT_POLL_TIMEOUT_MS)
    ).toEqual({ kind: 'timeout' });
  });

  it('surfaces the recorded finding for failed imports', () => {
    expect(
      importPollDecision(
        { import_status: 'failed', import_finding: 'No plugin target found' },
        1000
      )
    ).toEqual({ kind: 'failed', finding: 'No plugin target found' });
  });

  it('falls back to a generic message when the finding is missing', () => {
    const decision = importPollDecision({ import_status: 'failed' }, 0);
    expect(decision.kind).toBe('failed');
    expect((decision as { finding: string }).finding).toBeTruthy();
  });

  it('opens the selection dialog for pending_selection plugin sets', () => {
    expect(
      importPollDecision(
        { import_status: 'pending_selection', plugins_found: FOUND },
        0
      )
    ).toEqual({ kind: 'select', found: FOUND });
    expect(
      importPollDecision({ import_status: 'pending_selection' }, 0)
    ).toEqual({ kind: 'select', found: [] });
  });

  it('completes for imported records', () => {
    expect(importPollDecision({ import_status: 'imported' }, 0)).toEqual({
      kind: 'done',
    });
    // Records without an import status (defensive) also complete.
    expect(importPollDecision({}, 0)).toEqual({ kind: 'done' });
  });
});

// ------------------------------------------ external documentation

describe('documentation links', () => {
  it('points at the official GStreamer documentation index', () => {
    expect(GSTREAMER_DOCS_URL).toBe(
      'https://gstreamer.freedesktop.org/documentation/'
    );
  });

  it('points at the plugin-set split-up explanation', () => {
    expect(GSTREAMER_PLUGIN_SETS_DOCS_URL).toBe(
      'https://gstreamer.freedesktop.org/documentation/additional/splitup.html'
    );
  });

  it('builds the official per-plugin docs URL', () => {
    expect(pluginDocsUrl('rtp')).toBe(
      'https://gstreamer.freedesktop.org/documentation/rtp/index.html'
    );
    expect(pluginDocsUrl(' v4l2 ')).toBe(
      'https://gstreamer.freedesktop.org/documentation/v4l2/index.html'
    );
  });

  it('URL-encodes unusual plugin names', () => {
    expect(pluginDocsUrl('a/b c')).toBe(
      'https://gstreamer.freedesktop.org/documentation/a%2Fb%20c/index.html'
    );
  });
});

describe('filterPluginEntries', () => {
  it('returns everything for an empty or whitespace-only filter', () => {
    expect(filterPluginEntries(FOUND, '')).toEqual(FOUND);
    expect(filterPluginEntries(FOUND, '   ')).toEqual(FOUND);
  });

  it('matches the plugin name case-insensitively', () => {
    expect(filterPluginEntries(FOUND, 'RTP')).toEqual([
      { name: 'rtp', path: 'gst/rtp' },
    ]);
  });

  it('matches the source path too', () => {
    expect(filterPluginEntries(FOUND, 'ext/')).toEqual([
      { name: 'jpeg', path: 'ext/jpeg' },
    ]);
  });

  it('returns nothing when no entry matches', () => {
    expect(filterPluginEntries(FOUND, 'nope')).toEqual([]);
  });

  it('matches the description case-insensitively', () => {
    const described: EnumeratedPlugin[] = [
      { name: 'jpeg', path: 'ext/jpeg', description: 'JPeg plugin library' },
      { name: 'rtp', path: 'gst/rtp', description: 'Real-time Transport Protocol' },
      { name: 'udp', path: 'gst/udp' },
    ];
    expect(filterPluginEntries(described, 'transport')).toEqual([described[1]]);
    expect(filterPluginEntries(described, 'LIBRARY')).toEqual([described[0]]);
    // Entries without a description still match on name/path only.
    expect(filterPluginEntries(described, 'udp')).toEqual([described[2]]);
  });
});

describe('pluginEntryDescription', () => {
  it('combines the description with the source path as secondary detail', () => {
    expect(
      pluginEntryDescription({
        name: 'rtp',
        path: 'gst/rtp',
        description: 'Real-time Transport Protocol',
      })
    ).toBe('Real-time Transport Protocol — gst/rtp');
  });

  it('falls back to whichever of description or path is known', () => {
    expect(
      pluginEntryDescription({ name: 'rtp', path: 'gst/rtp' })
    ).toBe('gst/rtp');
    expect(
      pluginEntryDescription({ name: 'one', path: '', description: 'A plugin' })
    ).toBe('A plugin');
  });

  it('is undefined when neither is known', () => {
    expect(pluginEntryDescription({ name: 'one', path: '' })).toBeUndefined();
    expect(
      pluginEntryDescription({ name: 'one', path: '', description: '   ' })
    ).toBeUndefined();
  });
});

describe('togglePluginSelection', () => {
  it('adds an unselected plugin and removes a selected one', () => {
    expect(togglePluginSelection([], 'rtp')).toEqual(['rtp']);
    expect(togglePluginSelection(['rtp', 'udp'], 'rtp')).toEqual(['udp']);
  });
});

describe('addAllToSelection', () => {
  it('selects every visible plugin without duplicating', () => {
    expect(addAllToSelection(['rtp'], FOUND).sort()).toEqual([
      'jpeg',
      'rtp',
      'udp',
    ]);
  });

  it('preserves selected plugins currently filtered out of view', () => {
    const visible = filterPluginEntries(FOUND, 'udp');
    expect(addAllToSelection(['jpeg'], visible).sort()).toEqual(['jpeg', 'udp']);
  });
});

describe('pluginSelectionError', () => {
  it('rejects an empty selection', () => {
    expect(pluginSelectionError([], FOUND)).toBe(
      'Select at least one plugin to import'
    );
  });

  it('rejects plugins outside the enumeration', () => {
    expect(pluginSelectionError(['rtp', 'nope'], FOUND)).toBe(
      'Unknown plugins: nope'
    );
  });

  it('accepts a non-empty subset of the enumerated plugins', () => {
    expect(pluginSelectionError(['rtp'], FOUND)).toBeNull();
    expect(pluginSelectionError(['jpeg', 'udp', 'rtp'], FOUND)).toBeNull();
  });
});

// ---------------------------------------- import-time module selection

import {
  allPluginNames,
  moduleSelectionIncomplete,
  moduleSelectionSummary,
  normalizeModuleSelection,
  selectedPluginsParam,
} from './importFlow';
import type { ModulePluginEntry } from './types';

const MODULE_PLUGINS: ModulePluginEntry[] = [
  { name: 'jpeg' },
  { name: 'rtp' },
  { name: 'udp' },
  { name: 'v4l2' },
];
const AVAILABLE = ['jpeg', 'rtp', 'udp', 'v4l2'];

describe('allPluginNames', () => {
  it('lists the plugin names in listing order', () => {
    expect(allPluginNames(MODULE_PLUGINS)).toEqual(AVAILABLE);
    expect(allPluginNames([])).toEqual([]);
  });
});

describe('moduleSelectionIncomplete', () => {
  it('gates a loaded plugin list with nothing selected (2.8)', () => {
    expect(moduleSelectionIncomplete('module', AVAILABLE, [])).toBe(true);
  });

  it('unblocks once at least one plugin is selected', () => {
    expect(moduleSelectionIncomplete('module', AVAILABLE, ['rtp'])).toBe(false);
    expect(moduleSelectionIncomplete('module', AVAILABLE, AVAILABLE)).toBe(false);
  });

  it('never blocks without an available plugin list (3.13)', () => {
    expect(moduleSelectionIncomplete('module', [], [])).toBe(false);
  });

  it('never gates manual repository URL imports', () => {
    expect(moduleSelectionIncomplete('manual', AVAILABLE, [])).toBe(false);
    expect(moduleSelectionIncomplete('manual', [], [])).toBe(false);
  });
});

describe('normalizeModuleSelection', () => {
  it('drops unknown names and follows the listing order', () => {
    expect(normalizeModuleSelection(['udp', 'nope', 'jpeg'], AVAILABLE)).toEqual([
      'jpeg',
      'udp',
    ]);
  });

  it('handles empty selections and empty listings', () => {
    expect(normalizeModuleSelection([], AVAILABLE)).toEqual([]);
    expect(normalizeModuleSelection(['rtp'], [])).toEqual([]);
  });
});

describe('selectedPluginsParam', () => {
  it('serializes a partial selection in listing order', () => {
    expect(selectedPluginsParam(['udp', 'rtp'], AVAILABLE)).toEqual(['rtp', 'udp']);
    expect(selectedPluginsParam(['jpeg'], AVAILABLE)).toEqual(['jpeg']);
  });

  it('serializes a full selection to nothing (absent = whole module)', () => {
    expect(selectedPluginsParam(AVAILABLE, AVAILABLE)).toBeUndefined();
    expect(selectedPluginsParam(['v4l2', 'udp', 'rtp', 'jpeg'], AVAILABLE)).toBeUndefined();
  });

  it('serializes empty and unavailable listings to nothing', () => {
    expect(selectedPluginsParam([], AVAILABLE)).toBeUndefined();
    expect(selectedPluginsParam(['rtp'], [])).toBeUndefined();
  });

  it('ignores unknown names when deciding fullness', () => {
    expect(selectedPluginsParam(['rtp', 'ghost'], AVAILABLE)).toEqual(['rtp']);
  });
});

describe('moduleSelectionSummary', () => {
  it('summarizes a full (or unavailable) selection as All plugins', () => {
    expect(moduleSelectionSummary(AVAILABLE, AVAILABLE)).toBe('All plugins');
    expect(moduleSelectionSummary([], AVAILABLE)).toBe('All plugins');
    expect(moduleSelectionSummary([], [])).toBe('All plugins');
  });

  it('summarizes a partial selection with count and names', () => {
    expect(moduleSelectionSummary(['udp', 'rtp'], AVAILABLE)).toBe(
      '2 of 4 plugins: rtp, udp'
    );
  });

  it('truncates long name lists', () => {
    const available = Array.from({ length: 12 }, (_, i) => `plugin${i}`);
    const selected = available.slice(0, 10);
    expect(moduleSelectionSummary(selected, available)).toBe(
      '10 of 12 plugins: plugin0, plugin1, plugin2, plugin3, plugin4, ' +
        'plugin5, plugin6, plugin7, +2 more'
    );
  });
});

describe('importedPluginsSummary', () => {
  it('spells out a partial selection with counts', () => {
    expect(importedPluginsSummary(['rtsp'], 74)).toBe('rtsp (1 of 74 found)');
    expect(importedPluginsSummary(['rtp', 'udp'], 4)).toBe(
      'rtp, udp (2 of 4 found)'
    );
  });

  it('truncates long partial selections', () => {
    const selected = Array.from({ length: 10 }, (_, i) => `p${i}`);
    expect(importedPluginsSummary(selected, 74)).toBe(
      'p0, p1, p2, p3, p4, p5, p6, p7, +2 more (10 of 74 found)'
    );
  });

  it('omits counts when the enumeration size is unknown', () => {
    expect(importedPluginsSummary(['rtsp'], undefined)).toBe('rtsp');
    expect(importedPluginsSummary(['rtsp'], 0)).toBe('rtsp');
  });

  it('reports the whole enumeration when no selection is recorded', () => {
    expect(importedPluginsSummary(undefined, 74)).toBe('All 74 plugins');
    expect(importedPluginsSummary([], 74)).toBe('All 74 plugins');
  });

  it('treats a full selection as the whole enumeration', () => {
    expect(importedPluginsSummary(['a', 'b'], 2)).toBe('All 2 plugins');
  });

  it('shows nothing for non-imports and single-plugin repositories', () => {
    expect(importedPluginsSummary(undefined, undefined)).toBeNull();
    expect(importedPluginsSummary([], 0)).toBeNull();
    // One plugin enumerated, none selected: nothing to disambiguate.
    expect(importedPluginsSummary(undefined, 1)).toBeNull();
  });
});

describe('importedPluginsLabel', () => {
  it('labels a single-plugin selection with the plugin itself', () => {
    expect(importedPluginsLabel(['rtsp'], 74)).toBe('rtsp');
    expect(importedPluginsLabel(['rtsp'], undefined)).toBe('rtsp');
  });

  it('labels larger partial selections with a count', () => {
    expect(importedPluginsLabel(['rtp', 'udp', 'jpeg'], 74)).toBe('3 plugins');
  });

  it('stays quiet for full selections and records without one', () => {
    expect(importedPluginsLabel(['a', 'b'], 2)).toBeNull();
    expect(importedPluginsLabel([], 74)).toBeNull();
    expect(importedPluginsLabel(undefined, undefined)).toBeNull();
  });
});

describe('platform compatibility display helpers', () => {
  const incompatibleJp5: PlatformCompatibilityEntry = {
    compatible: false,
    platformVersion: '1.16',
    requiredVersion: '1.24.0',
    reason:
      'The source requires GStreamer >= 1.24.0; arm64 JetPack 5 provides 1.16',
    suggestedRevision: '1.16',
  };
  const compatibleX86: PlatformCompatibilityEntry = {
    compatible: true,
    platformVersion: '1.20',
    requiredVersion: '1.24.0',
    reason: null,
    suggestedRevision: null,
  };

  describe('platformWarningMessage', () => {
    it('combines the recorded reason with the revision suggestion', () => {
      expect(platformWarningMessage('arm64_jp5', incompatibleJp5)).toBe(
        'The source requires GStreamer >= 1.24.0; arm64 JetPack 5 ' +
          'provides 1.16. Import revision 1.16 for this platform instead.'
      );
    });

    it('omits the suggestion for non-official repositories', () => {
      expect(
        platformWarningMessage('arm64_jp4', {
          compatible: false,
          platformVersion: '1.14',
          requiredVersion: '1.24.0',
          reason:
            'The source requires GStreamer >= 1.24.0; arm64 JetPack 4 provides 1.14',
          suggestedRevision: null,
        })
      ).toBe(
        'The source requires GStreamer >= 1.24.0; arm64 JetPack 4 ' +
          'provides 1.14.'
      );
    });

    it('builds a reason locally when the record carries none', () => {
      expect(
        platformWarningMessage('arm64_jp5', {
          compatible: false,
          platformVersion: '1.16',
          requiredVersion: '1.24.0',
        })
      ).toBe(
        'The source requires GStreamer >= 1.24.0; arm64 JetPack 5 ' +
          'provides 1.16.'
      );
      expect(platformWarningMessage('arm64_jp4', { compatible: false })).toBe(
        'This source may not be compatible with arm64 JetPack 4.'
      );
    });
  });

  describe('incompatiblePlatformWarnings', () => {
    it('lists only the incompatible platforms, in architecture order', () => {
      const warnings = incompatiblePlatformWarnings({
        platform_compatibility: {
          x86_64: compatibleX86,
          arm64_jp5: incompatibleJp5,
          arm64_jp4: {
            compatible: false,
            platformVersion: '1.14',
            requiredVersion: '1.24.0',
            reason:
              'The source requires GStreamer >= 1.24.0; arm64 JetPack 4 provides 1.14',
            suggestedRevision: '1.14',
          },
        },
      });
      expect(warnings.map((w) => w.arch)).toEqual(['arm64_jp4', 'arm64_jp5']);
      expect(warnings[1].message).toContain('Import revision 1.16');
    });

    it('is empty when every platform is compatible', () => {
      expect(
        incompatiblePlatformWarnings({
          platform_compatibility: { x86_64: compatibleX86 },
        })
      ).toEqual([]);
    });

    it('is empty when no map was recorded (older records, non-imports)', () => {
      expect(incompatiblePlatformWarnings({})).toEqual([]);
      expect(
        incompatiblePlatformWarnings({ platform_compatibility: undefined })
      ).toEqual([]);
    });
  });
});

// ------------------------------------- per-architecture revisions

describe('archRevisionsParam', () => {
  it('keeps only non-empty trimmed overrides of selected architectures', () => {
    expect(
      archRevisionsParam(
        { arm64_jp5: ' 1.16 ', arm64_jp4: '1.14', x86_64: '   ' },
        ['x86_64', 'arm64_jp4', 'arm64_jp5']
      )
    ).toEqual({ arm64_jp4: '1.14', arm64_jp5: '1.16' });
  });

  it('drops overrides of architectures no longer selected', () => {
    expect(
      archRevisionsParam({ arm64_jp5: '1.16', arm64_jp4: '1.14' }, ['arm64_jp4'])
    ).toEqual({ arm64_jp4: '1.14' });
  });

  it('is undefined when no override remains (single revision everywhere)', () => {
    expect(archRevisionsParam({}, ['x86_64'])).toBeUndefined();
    expect(archRevisionsParam({ x86_64: '  ' }, ['x86_64'])).toBeUndefined();
    expect(archRevisionsParam({ arm64_jp5: '1.16' }, [])).toBeUndefined();
  });
});

describe('archRevisionEntries', () => {
  it('resolves each selected architecture to its effective revision', () => {
    expect(
      archRevisionEntries({ arm64_jp5: '1.16' }, ['x86_64', 'arm64_jp5'], 'main')
    ).toEqual([
      { arch: 'x86_64', revision: 'main' },
      { arch: 'arm64_jp5', revision: '1.16' },
    ]);
  });

  it('falls back to the default branch when no top-level revision is set', () => {
    expect(archRevisionEntries({}, ['x86_64'], '  ')).toEqual([
      { arch: 'x86_64', revision: 'default branch' },
    ]);
  });
});

describe('archRevisionLabel', () => {
  const detail = {
    arch_revisions: {
      arm64_jp5: '1.16',
      x86_64: 'default',
    },
    fetches: {
      '1.16': {
        revision: '1.16',
        source_prefix: 'plugin-sources/uc/p/1/rev-1.16/',
        status: 'succeeded' as const,
      },
      default: {
        revision: 'default',
        source_prefix: 'plugin-sources/uc/p/1/rev-default/',
        status: 'succeeded' as const,
      },
    },
  };

  it('resolves the arch through its slug to the fetched revision', () => {
    expect(archRevisionLabel(detail, 'arm64_jp5')).toBe('1.16');
  });

  it("renders the default-branch marker as 'default branch'", () => {
    expect(archRevisionLabel(detail, 'x86_64')).toBe('default branch');
  });

  it('is null for unmapped architectures and single-revision records', () => {
    expect(archRevisionLabel(detail, 'arm64_jp6')).toBeNull();
    expect(archRevisionLabel({}, 'x86_64')).toBeNull();
  });
});

// ---------------------------------------------------------------------
// Preservation property tests: compatible-platform display is unchanged
// (imported-plugin-revision-adjustment-fix, Property 2)
// ---------------------------------------------------------------------
//
// **Validates: Requirements 3.2**
//
// Observation-first: these properties capture the display behavior
// OBSERVED on the UNFIXED code for platform entries where the bug
// condition does NOT hold — compatible entries and incompatible
// entries WITHOUT a suggested revision. `platformWarningMessage`,
// `incompatiblePlatformWarnings`, and `archRevisionLabel` must render
// them exactly as today. These tests MUST PASS on the unfixed code
// (the baseline) and MUST STILL PASS after the fix lands (task 3.7
// re-runs them unchanged).

import * as fc from 'fast-check';
import { ARCHITECTURE_LABELS, DeviceArchitecture } from './types';
import type { ImportFetchEntry, ImportFetchStatus } from './types';

const ALL_ARCHS = [
  'x86_64',
  'x86_64_nvidia',
  'arm64_jp4',
  'arm64_jp5',
  'arm64_jp6',
  'arm64_jp7',
] as const;

const versionArb = fc.constantFrom('1.14', '1.16', '1.18', '1.20', '1.24.0');

/** Recorded reasons: realistic sentences plus arbitrary text (never
 * containing the adjustment suggestion marker, so its absence in the
 * output is attributable to the helper, not the input). */
const reasonArb = fc
  .oneof(
    fc.constantFrom(
      'The source requires GStreamer >= 1.24.0; arm64 JetPack 4 provides 1.14',
      'This source may not be compatible with x86_64'
    ),
    fc.string({ minLength: 1, maxLength: 40 })
  )
  .filter((s) => !s.includes('Import revision'));

/** A compatible platform entry (bug condition does not hold). */
const compatibleEntryArb: fc.Arbitrary<PlatformCompatibilityEntry> = fc.record({
  compatible: fc.constant(true),
  platformVersion: fc.option(versionArb, { nil: null }),
  requiredVersion: fc.option(versionArb, { nil: null }),
  reason: fc.constant(null),
  suggestedRevision: fc.constant(null),
});

/** An incompatible entry WITHOUT a suggested revision (non-official
 * repositories — the bug condition does not hold either). */
const noSuggestionEntryArb: fc.Arbitrary<PlatformCompatibilityEntry> =
  fc.record({
    compatible: fc.constant(false),
    platformVersion: fc.option(versionArb, { nil: null }),
    requiredVersion: fc.option(versionArb, { nil: null }),
    reason: fc.option(reasonArb, { nil: null }),
    suggestedRevision: fc.constant(null),
  });

/** An incompatible entry WITH a suggested revision (the bug-condition
 * anchor — included in map generation so the warnings list is checked
 * against mixed maps, its own message rendering unchanged). */
const suggestionEntryArb: fc.Arbitrary<PlatformCompatibilityEntry> = fc.record({
  compatible: fc.constant(false),
  platformVersion: fc.option(versionArb, { nil: null }),
  requiredVersion: fc.option(versionArb, { nil: null }),
  reason: fc.option(reasonArb, { nil: null }),
  suggestedRevision: versionArb,
});

const archArb = fc.constantFrom<string>(...ALL_ARCHS);

describe('Property 2 (preservation): compatible-platform display is unchanged (3.2)', () => {
  it('renders entries WITHOUT a suggested revision as the plain reason sentence, never an adjustment suggestion', () => {
    fc.assert(
      fc.property(
        archArb,
        fc.oneof(compatibleEntryArb, noSuggestionEntryArb),
        (arch, entry) => {
          const message = platformWarningMessage(arch, entry);

          // Exactly today's rendering: the recorded reason (or the
          // locally built fallback) as one sentence...
          const label =
            ARCHITECTURE_LABELS[arch as DeviceArchitecture] || arch;
          const reason =
            entry.reason ||
            (entry.requiredVersion && entry.platformVersion
              ? `The source requires GStreamer >= ${entry.requiredVersion}; ` +
                `${label} provides ${entry.platformVersion}`
              : `This source may not be compatible with ${label}`);
          expect(message).toBe(`${reason}.`);

          // ...and never the revision suggestion tail.
          expect(message).not.toContain('Import revision');
        }
      ),
      { numRuns: 100 }
    );
  });

  it('lists exactly the incompatible entries, sorted, each rendered by platformWarningMessage', () => {
    fc.assert(
      fc.property(
        fc.dictionary(
          archArb,
          fc.oneof(compatibleEntryArb, noSuggestionEntryArb, suggestionEntryArb),
          { maxKeys: 5 }
        ),
        (map) => {
          const warnings = incompatiblePlatformWarnings({
            platform_compatibility: map,
          });

          // Compatible entries never produce a warning; incompatible
          // ones (with or without a suggestion) all do, in arch order.
          const expected = Object.keys(map)
            .filter((arch) => map[arch].compatible === false)
            .sort();
          expect(warnings.map((w) => w.arch)).toEqual(expected);
          for (const warning of warnings) {
            expect(warning.message).toBe(
              platformWarningMessage(warning.arch, map[warning.arch])
            );
          }
        }
      ),
      { numRuns: 100 }
    );
  });

  it('keeps archRevisionLabel resolution unchanged for any record shape', () => {
    const slugArb = fc.constantFrom(
      'default',
      '1.14',
      '1.16',
      'a-b',
      'feature-x'
    );
    const fetchEntryArb: fc.Arbitrary<ImportFetchEntry> = fc.record({
      revision: fc.constantFrom('default', '1.14', '1.16', 'main'),
      source_prefix: fc.constant('plugin-sources/uc/p/1/rev-x/'),
      status: fc.constantFrom<ImportFetchStatus>(
        'succeeded',
        'fetching',
        'failed'
      ),
    });
    fc.assert(
      fc.property(
        fc.dictionary(archArb, slugArb, { maxKeys: 5 }),
        fc.dictionary(slugArb, fetchEntryArb, { maxKeys: 5 }),
        archArb,
        (archRevisions, fetches, arch) => {
          const detail = { arch_revisions: archRevisions, fetches };
          const label = archRevisionLabel(detail, arch);

          const slug = archRevisions[arch];
          if (!slug) {
            // Unmapped architectures and single-revision records show
            // no per-arch revision label — exactly today's behavior.
            expect(label).toBeNull();
            expect(archRevisionLabel({}, arch)).toBeNull();
            return;
          }
          // Mapped architectures resolve slug -> fetches revision
          // (slug itself when the entry is missing), 'default'
          // rendering as 'default branch'.
          const revision = fetches[slug]?.revision || slug;
          expect(label).toBe(
            revision === 'default' ? 'default branch' : revision
          );
        }
      ),
      { numRuns: 100 }
    );
  });
});

// ---------------------------------------------------------------------
// Post-import revision adjustment helpers
// (imported-plugin-revision-adjustment-fix, task 4 unit tests)
// ---------------------------------------------------------------------
//
// **Validates: Requirements 2.1, 2.5**

describe('canAdjustRevision', () => {
  type AdjustableDetail = Pick<
    PluginVersionDetail,
    'kind' | 'import_status' | 'platform_compatibility'
  >;

  const incompatibleWithSuggestion: PlatformCompatibilityEntry = {
    compatible: false,
    platformVersion: '1.14',
    requiredVersion: '1.24.0',
    reason: null,
    suggestedRevision: '1.14',
  };

  const settledImport: AdjustableDetail = {
    kind: 'imported',
    import_status: 'imported',
    platform_compatibility: { arm64_jp4: incompatibleWithSuggestion },
  };

  it('is true exactly for a settled import with an incompatible entry carrying a suggestion (2.1)', () => {
    expect(canAdjustRevision(settledImport, 'arm64_jp4')).toBe(true);
  });

  it('is false for non-imports (mirrors the backend 409 gate, 2.5)', () => {
    expect(
      canAdjustRevision({ ...settledImport, kind: 'scaffold' }, 'arm64_jp4')
    ).toBe(false);
    expect(
      canAdjustRevision({ ...settledImport, kind: 'generated' }, 'arm64_jp4')
    ).toBe(false);
  });

  it('is false while the import has not settled (2.5)', () => {
    for (const status of [
      'fetching',
      'pending_selection',
      'failed',
    ] as const) {
      expect(
        canAdjustRevision({ ...settledImport, import_status: status }, 'arm64_jp4')
      ).toBe(false);
    }
    expect(
      canAdjustRevision(
        { ...settledImport, import_status: undefined },
        'arm64_jp4'
      )
    ).toBe(false);
  });

  it('is false for compatible entries and entries without a suggested revision', () => {
    expect(
      canAdjustRevision(
        {
          ...settledImport,
          platform_compatibility: {
            arm64_jp4: { ...incompatibleWithSuggestion, compatible: true },
          },
        },
        'arm64_jp4'
      )
    ).toBe(false);
    expect(
      canAdjustRevision(
        {
          ...settledImport,
          platform_compatibility: {
            arm64_jp4: {
              ...incompatibleWithSuggestion,
              suggestedRevision: null,
            },
          },
        },
        'arm64_jp4'
      )
    ).toBe(false);
  });

  it('is false for architectures without an entry and records without a map', () => {
    expect(canAdjustRevision(settledImport, 'arm64_jp5')).toBe(false);
    expect(
      canAdjustRevision(
        { kind: 'imported', import_status: 'imported' },
        'arm64_jp4'
      )
    ).toBe(false);
  });
});

describe('adjustRevisionError', () => {
  it('accepts any non-empty trimmed revision', () => {
    expect(adjustRevisionError('1.14')).toBeNull();
    expect(adjustRevisionError('  1.16  ')).toBeNull();
    expect(adjustRevisionError('feature/x')).toBeNull();
  });

  it('rejects empty and whitespace-only input with the display message', () => {
    expect(adjustRevisionError('')).toBe(
      'Enter a revision to import for this platform'
    );
    expect(adjustRevisionError('   ')).toBe(
      'Enter a revision to import for this platform'
    );
  });
});
