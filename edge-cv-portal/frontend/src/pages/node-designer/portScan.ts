/**
 * Port scan module (port-guidance-and-pad-prepopulation).
 *
 * Wire shapes of the pad-derived extension of the
 * `GET /plugins/{id}/versions/{v}/gst-properties` route
 * (plugin_records.py / gst_properties.ports_for_element): per-element
 * Port_Suggestions, Unmapped_Pads, and the machine-readable
 * pads-unavailability reason (Requirements 4.5, 4.7, 4.8).
 *
 * The pure detection/merge/protection functions used by PortScanPanel
 * live here too, mirroring `scan.ts` for the parameter scan. Element
 * selection reuses `pickElement` from `scan.ts` unchanged so the
 * Port_Scan and Parameter_Scan always agree on the factory (6.6).
 */
import type { PortForm } from './declaration';

/**
 * Machine-readable reason the element carries no pad-derived
 * suggestions (4.7, 4.8, 3.2). `null`/absent means pads were derived.
 */
export type PadsReason =
  | 'pads_not_captured'
  | 'no_pad_templates'
  | 'pads_read_failed';

/**
 * One pre-populated Port declaration derived from an always-present
 * Pad_Template (Requirement 5): `confident` distinguishes a
 * Confident_Suggestion (caps begin with `video/x-raw`, 5.2) from an
 * Unconfirmed_Suggestion needing user confirmation (5.3); `caps` and
 * `capsTruncated` ride along for display (6.4).
 */
export interface PortSuggestion {
  name: string;
  direction: 'input' | 'output';
  portType: string; // always 'VideoFrames' today (5.5)
  confident: boolean;
  caps: string;
  capsTruncated: boolean;
  reason: string;
}

/**
 * A Pad_Template that does not map to a declared Port — presence
 * sometimes/request (5.4) or an invalid name template (5.6) —
 * surfaced as an advisory note with its caveat.
 */
export interface UnmappedPad {
  name: string;
  direction: 'sink' | 'src';
  presence: 'sometimes' | 'request' | 'always';
  caveat: string;
}

// -------------------------------------------------------- pure functions

/**
 * Untouched_Defaults detection (6.1): exactly the wizard-supplied
 * initial lists — one input named "in" and one output named "out",
 * both VideoFrames. Any rename, retype, addition, or removal makes
 * the lists user-edited.
 */
export function isUntouchedDefaults(
  inputs: PortForm[],
  outputs: PortForm[]
): boolean {
  return (
    inputs.length === 1 &&
    outputs.length === 1 &&
    inputs[0].name === 'in' &&
    inputs[0].portType === 'VideoFrames' &&
    outputs[0].name === 'out' &&
    outputs[0].portType === 'VideoFrames'
  );
}

/** The result of applying Port_Suggestions to the port lists. */
export interface ApplySuggestionsResult {
  inputs: PortForm[];
  outputs: PortForm[];
  /** Names newly added/applied (6.1, 6.11). */
  applied: string[];
  /** Names kept as declared, without modification (6.2). */
  alreadyDeclared: string[];
  /** Applied names with `confident === false`, needing confirmation (6.5). */
  unconfirmed: string[];
}

/**
 * Apply Port_Suggestions to the port lists (6.1, 6.2, 6.10, 6.11).
 *
 * `untouched && suggestions.length > 0`: both sides are replaced by
 * the suggestions partitioned by direction, in suggestion order (6.1).
 *
 * Otherwise additive merge: every existing port stays unchanged and
 * in place; each suggestion whose trimmed name exactly
 * (case-sensitively) matches an existing trimmed port name on either
 * side is reported in `alreadyDeclared` (6.2); the rest are appended
 * to their side in suggestion order and reported in `applied` (6.11).
 * Empty suggestions always leave the lists unchanged (6.10).
 */
export function applySuggestions(
  inputs: PortForm[],
  outputs: PortForm[],
  suggestions: PortSuggestion[],
  untouched: boolean
): ApplySuggestionsResult {
  if (untouched && suggestions.length > 0) {
    const applied: string[] = [];
    const unconfirmed: string[] = [];
    const nextInputs: PortForm[] = [];
    const nextOutputs: PortForm[] = [];
    for (const suggestion of suggestions) {
      const side = suggestion.direction === 'input' ? nextInputs : nextOutputs;
      side.push({ name: suggestion.name, portType: suggestion.portType });
      applied.push(suggestion.name);
      if (!suggestion.confident) {
        unconfirmed.push(suggestion.name);
      }
    }
    return {
      inputs: nextInputs,
      outputs: nextOutputs,
      applied,
      alreadyDeclared: [],
      unconfirmed,
    };
  }

  // Additive merge (6.2, 6.10, 6.11): name collisions are checked
  // against the trimmed names of both sides, like the parameter
  // merge's trimmed-name matching in scan.ts.
  const declared = new Set(
    [...inputs, ...outputs].map((port) => port.name.trim())
  );
  const nextInputs = [...inputs];
  const nextOutputs = [...outputs];
  const applied: string[] = [];
  const alreadyDeclared: string[] = [];
  const unconfirmed: string[] = [];
  for (const suggestion of suggestions) {
    const name = suggestion.name.trim();
    if (declared.has(name)) {
      alreadyDeclared.push(name);
      continue;
    }
    declared.add(name);
    const side = suggestion.direction === 'input' ? nextInputs : nextOutputs;
    side.push({ name: suggestion.name, portType: suggestion.portType });
    applied.push(suggestion.name);
    if (!suggestion.confident) {
      unconfirmed.push(suggestion.name);
    }
  }
  return {
    inputs: nextInputs,
    outputs: nextOutputs,
    applied,
    alreadyDeclared,
    unconfirmed,
  };
}

/**
 * Update-mode removal protection (6.9): the reason a port cannot be
 * removed, or null when removal is allowed. A port is protected when
 * its trimmed name appears on the same side of the existing
 * registered declaration — the registered Custom_Node_Type depends on
 * it. A null declaration (initial registration) never blocks.
 */
export function removalBlockReason(
  side: 'inputs' | 'outputs',
  portName: string,
  existingDeclaration: Record<string, unknown> | null
): string | null {
  if (!existingDeclaration) {
    return null;
  }
  const name = portName.trim();
  if (!name) {
    return null;
  }
  const declaredSide = existingDeclaration[side];
  if (!Array.isArray(declaredSide)) {
    return null;
  }
  const depended = declaredSide.some(
    (port: any) => String(port?.name ?? '').trim() === name
  );
  if (!depended) {
    return null;
  }
  const sideLabel = side === 'inputs' ? 'input' : 'output';
  return `The registered node type declares the ${sideLabel} port "${name}". Removing it would break the existing registration, so it cannot be removed here.`;
}
