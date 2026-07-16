/**
 * Parameter scan module (gst-parameter-prepopulation).
 *
 * Wire shapes of the `GET /plugins/{id}/versions/{v}/gst-properties`
 * route (plugin_records.py / gst_properties.py): the stored
 * Introspection_Report served as per-element Parameter_Suggestions in
 * the ParameterDeclaration wire shape (Requirement 1.5), or a
 * machine-readable unavailability reason (Requirement 1.6, 7.4, 8.3).
 *
 * The pure merge/convert/pick functions used by ParameterScanPanel
 * live here too (task 6.2).
 */
import type { ParameterForm } from './declaration';
import type { PadsReason, PortSuggestion, UnmappedPad } from './portScan';
import type { ParameterDeclaration } from './types';

/** Machine-readable reasons of an `available: false` response (1.6, 7.4). */
export type GstUnavailableReason =
  | 'no_x86_64_build'
  | 'not_captured'
  | 'introspection_failed';

/**
 * One element of the report: the factory name plus its derived
 * suggestions (base-class-filtered, type-mapped, required-classified
 * server-side) and the properties skipped with reasons (2.5).
 */
export interface ScanElement {
  factory: string;
  suggestions: ParameterDeclaration[];
  skipped: { name: string; reason: string }[];
  /**
   * Pad-derived port scan extension
   * (port-guidance-and-pad-prepopulation, 4.5): optional fields an
   * old backend simply omits. `padsReason` is non-null exactly when
   * no pad data could be derived (4.7, 4.8); `padsMessage` carries
   * the pads_read_failed diagnostic (3.2).
   */
  portSuggestions?: PortSuggestion[];
  unmappedPads?: UnmappedPad[];
  padsReason?: PadsReason | null;
  padsMessage?: string | null;
}

/**
 * Response of GET /plugins/{id}/versions/{v}/gst-properties.
 *
 * `available: false` carries `reason` (and optionally `message`, e.g.
 * the introspection diagnostic); `available: true` carries the
 * elements plus the capture provenance (gstVersion, capturedAt).
 */
export interface GstPropertiesResponse {
  available: boolean;
  reason?: GstUnavailableReason;
  message?: string;
  gstVersion?: string | null;
  capturedAt?: string | null;
  elements?: ScanElement[];
}

// -------------------------------------------------------- pure functions

/**
 * ParameterDeclaration wire shape -> ParameterForm raw-text row
 * (design "ParameterForm mapping" table).
 *
 * Numeric min/max constraints are retained on the row's ride-along
 * `constraints` field so declaration assembly (parameterFromForm via
 * buildRegistrationDeclaration) re-emits them at submit; they have no
 * editable UI (3.3).
 */
export function formFromSuggestion(s: ParameterDeclaration): ParameterForm {
  const form: ParameterForm = {
    name: s.name,
    paramType: s.paramType,
    required: s.required,
    defaultValue:
      s.default === undefined || s.default === null ? '' : String(s.default),
    description: s.description,
    example:
      Array.isArray(s.examples) && s.examples.length > 0
        ? String(s.examples[0])
        : '',
    enumValues: Array.isArray(s.constraints?.values)
      ? (s.constraints.values as unknown[]).map(String).join(', ')
      : '',
  };
  const min = s.constraints?.min;
  const max = s.constraints?.max;
  if (typeof min === 'number' || typeof max === 'number') {
    form.constraints = {};
    if (typeof min === 'number') {
      form.constraints.min = min;
    }
    if (typeof max === 'number') {
      form.constraints.max = max;
    }
  }
  return form;
}

/**
 * Requirement 6: merge Parameter_Suggestions into the wizard's
 * parameter list without silent overwrite. Existing rows stay
 * unchanged in place (6.1); suggestions whose trimmed name matches no
 * declared trimmed name are appended in suggestion order (6.2) and
 * reported in `added`; colliding names are kept as declared and
 * reported in `alreadyDeclared` (6.3). Name matching is exact on the
 * trimmed parameter name.
 */
export function mergeSuggestions(
  existing: ParameterForm[],
  suggestions: ParameterDeclaration[]
): { parameters: ParameterForm[]; added: string[]; alreadyDeclared: string[] } {
  const declared = new Set(existing.map((row) => row.name.trim()));
  const parameters = [...existing];
  const added: string[] = [];
  const alreadyDeclared: string[] = [];
  for (const suggestion of suggestions) {
    const name = suggestion.name.trim();
    if (declared.has(name)) {
      alreadyDeclared.push(name);
      continue;
    }
    declared.add(name);
    parameters.push(formFromSuggestion(suggestion));
    added.push(name);
  }
  return { parameters, added, alreadyDeclared };
}

/**
 * 5.4: pick the element whose factory equals the preferred factory
 * when one exists; else the sole element when the list has exactly
 * one; else null (the user chooses via the factory selector).
 */
export function pickElement(
  elements: ScanElement[],
  preferredFactory?: string
): ScanElement | null {
  if (preferredFactory) {
    const match = elements.find(
      (element) => element.factory === preferredFactory
    );
    if (match) {
      return match;
    }
  }
  return elements.length === 1 ? elements[0] : null;
}
