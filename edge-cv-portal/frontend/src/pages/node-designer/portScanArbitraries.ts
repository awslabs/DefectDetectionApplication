/**
 * Shared fast-check arbitraries for the portScan.ts / portGuidance.ts
 * property tests (port-guidance-and-pad-prepopulation, Properties 10-12).
 *
 * Test-only helper module mirroring `scanArbitraries.ts`: generates
 * `PortForm` rows (the wizard's Ports-step form state, declaration.ts),
 * `PortSuggestion` values (the pad-derived gst-properties wire shape,
 * portScan.ts), and category/port-list pairs (the guidance divergence
 * inputs, portGuidance.ts).
 *
 * Design notes:
 * - Names draw mostly from a small shared pool so form/suggestion name
 *   collisions actually happen, and carry optional surrounding
 *   whitespace to exercise the trimmed-name matching rule (6.2).
 * - Suggestions are generated in both directions and both confidence
 *   states, with caps that actually satisfy the confidence contract:
 *   `confident` iff the caps string begins with the exact
 *   case-sensitive `video/x-raw` prefix (5.2, 5.3).
 * - Suggestion names always have a non-empty trim: the backend never
 *   derives a Port_Suggestion from a whitespace-only name template
 *   (those become Unmapped_Pads, 5.6).
 */
import fc from 'fast-check';
import type { PortForm } from './declaration';
import type { PortSuggestion } from './portScan';
import { CATEGORIES, PORT_TYPES } from './types';

/** Small pool shared by forms and suggestions so collisions occur often. */
const NAME_POOL = [
  'in',
  'out',
  'sink',
  'src',
  'video',
  'result',
  'sink_0',
  'src_%u',
];

/** Leading/trailing whitespace paddings (trimmed-name matching, 6.2). */
const PADDING = ['', ' ', '  ', '\t'];

/**
 * A port/pad name: an identifier core (mostly from the shared pool,
 * sometimes random) with optional surrounding whitespace. The trimmed
 * name is always non-empty.
 */
export const portNameArb: fc.Arbitrary<string> = fc
  .tuple(
    fc.oneof(
      { arbitrary: fc.constantFrom(...NAME_POOL), weight: 3 },
      { arbitrary: fc.stringMatching(/^[a-z][a-z0-9_%]{0,11}$/), weight: 1 }
    ),
    fc.constantFrom(...PADDING),
    fc.constantFrom(...PADDING)
  )
  .map(([core, lead, trail]) => `${lead}${core}${trail}`);

/** One PortForm row: any catalog port type, name from the shared pool. */
export const portFormArb: fc.Arbitrary<PortForm> = fc.record({
  name: portNameArb,
  portType: fc.constantFrom<string>(...PORT_TYPES),
});

/** A user-edited port list side. */
export const portListArb: fc.Arbitrary<PortForm[]> = fc.array(portFormArb, {
  maxLength: 6,
});

// ------------------------------------------------------- port suggestions

/** Caps that begin with the exact case-sensitive prefix (5.2). */
const confidentCapsArb: fc.Arbitrary<string> = fc
  .constantFrom(
    '',
    ', format=(string){ RGB, BGR }',
    ', width=(int)[ 1, 2147483647 ]',
    '(memory:NVMM), format=(string)NV12'
  )
  .map((suffix) => `video/x-raw${suffix}`);

/** Caps that do NOT begin with `video/x-raw` (case/prefix variants). */
const unconfirmedCapsArb: fc.Arbitrary<string> = fc.oneof(
  fc.constantFrom(
    'ANY',
    'audio/x-raw',
    'application/x-rtp',
    'video/x-h264',
    'Video/X-RAW',
    'VIDEO/X-RAW, format=RGB',
    ' video/x-raw'
  ),
  fc
    .string({ maxLength: 24 })
    .filter((caps) => !caps.startsWith('video/x-raw'))
);

/** The backend's per-confidence reason texts (5.2, 5.3). */
const CONFIDENT_REASON = "the pad's caps begin with video/x-raw";
const UNCONFIRMED_REASON =
  'InferenceMeta and EventSignal are DDA semantic concepts GStreamer caps ' +
  'cannot express; confirm the Port_Type';

/** One PortSuggestion with the given confidence and matching caps. */
function suggestionArbFor(confident: boolean): fc.Arbitrary<PortSuggestion> {
  return fc.record({
    name: portNameArb,
    direction: fc.constantFrom<'input' | 'output'>('input', 'output'),
    portType: fc.constant('VideoFrames'),
    confident: fc.constant(confident),
    caps: confident ? confidentCapsArb : unconfirmedCapsArb,
    capsTruncated: fc.boolean(),
    reason: fc.constant(confident ? CONFIDENT_REASON : UNCONFIRMED_REASON),
  });
}

/** A Confident_Suggestion (caps begin with video/x-raw). */
export const confidentSuggestionArb: fc.Arbitrary<PortSuggestion> =
  suggestionArbFor(true);

/** An Unconfirmed_Suggestion (caps outside the confident prefix). */
export const unconfirmedSuggestionArb: fc.Arbitrary<PortSuggestion> =
  suggestionArbFor(false);

/** One PortSuggestion: either confidence, either direction. */
export const portSuggestionArb: fc.Arbitrary<PortSuggestion> = fc.oneof(
  confidentSuggestionArb,
  unconfirmedSuggestionArb
);

/** A scanned suggestion list (duplicate names within the list included). */
export const portSuggestionsArb: fc.Arbitrary<PortSuggestion[]> = fc.array(
  portSuggestionArb,
  { maxLength: 8 }
);

/** A non-empty scanned suggestion list (Property 10's precondition). */
export const nonEmptyPortSuggestionsArb: fc.Arbitrary<PortSuggestion[]> =
  fc.array(portSuggestionArb, { minLength: 1, maxLength: 8 });

// ------------------------------------------------------ untouched defaults

/**
 * Fresh copies of the wizard-supplied Untouched_Defaults: one input
 * named "in" and one output named "out", both VideoFrames (6.1).
 */
export function untouchedDefaultInputs(): PortForm[] {
  return [{ name: 'in', portType: 'VideoFrames' }];
}

export function untouchedDefaultOutputs(): PortForm[] {
  return [{ name: 'out', portType: 'VideoFrames' }];
}

// ------------------------------------------------- category/port pairs

/** A palette category (all five, for the divergence rule, 2.4, 2.5). */
export const categoryArb: fc.Arbitrary<string> = fc.constantFrom<string>(
  ...CATEGORIES
);

/** A category with declared port lists (guidanceDivergence inputs). */
export const categoryPortsArb: fc.Arbitrary<{
  category: string;
  inputs: PortForm[];
  outputs: PortForm[];
}> = fc.record({
  category: categoryArb,
  inputs: portListArb,
  outputs: portListArb,
});
