/**
 * Shared fast-check arbitraries for the scan.ts property tests
 * (gst-parameter-prepopulation, Properties 8-12).
 *
 * Test-only helper module: generates `ParameterForm` rows (the wizard's
 * raw-text form state, declaration.ts) and `ParameterDeclaration`
 * suggestions (the gst-properties wire shape, types.ts).
 *
 * Design notes:
 * - Names draw mostly from a small shared pool so form/suggestion name
 *   collisions actually happen, and carry optional surrounding
 *   whitespace to exercise the trimmed-name matching rule.
 * - All five paramTypes are generated (string/int/float/bool/enum).
 * - Suggestions appear with and without `default`, `examples`, and
 *   `constraints` (numeric min/max, enum values) to cover every
 *   formFromSuggestion branch.
 */
import fc from 'fast-check';
import type { ParameterForm } from './declaration';
import type { ScanElement } from './scan';
import type { ParameterDeclaration } from './types';

export const PARAM_TYPES = ['string', 'int', 'float', 'bool', 'enum'] as const;

/** Small pool shared by forms and suggestions so collisions occur often. */
const NAME_POOL = [
  'radius',
  'mode',
  'name',
  'qos',
  'threshold',
  'device',
  'silent',
  'blur-size',
];

/** Leading/trailing whitespace paddings (trimmed-name matching, 6.2). */
const PADDING = ['', ' ', '  ', '\t'];

/**
 * A parameter name: a launch-safe-ish identifier core (mostly from the
 * shared pool, sometimes random) with optional surrounding whitespace.
 */
export const parameterNameArb: fc.Arbitrary<string> = fc
  .tuple(
    fc.oneof(
      { arbitrary: fc.constantFrom(...NAME_POOL), weight: 3 },
      { arbitrary: fc.stringMatching(/^[a-z][a-z0-9-]{0,11}$/), weight: 1 }
    ),
    fc.constantFrom(...PADDING),
    fc.constantFrom(...PADDING)
  )
  .map(([core, lead, trail]) => `${lead}${core}${trail}`);

const intArb = fc.integer({ min: -100000, max: 100000 });
const floatArb = fc
  .double({ noNaN: true, noDefaultInfinity: true })
  .map((value) => (Object.is(value, -0) ? 0 : value));

/** A wire value appropriate for the paramType (default / example slots). */
function valueArbFor(
  paramType: string
): fc.Arbitrary<string | number | boolean> {
  switch (paramType) {
    case 'int':
      return intArb;
    case 'float':
      return floatArb;
    case 'bool':
      return fc.boolean();
    default:
      // string and enum carry strings on the wire
      return fc.string({ maxLength: 12 });
  }
}

/** Numeric min/max ride-along constraints (both keys optional). */
const numericConstraintsArb: fc.Arbitrary<{ min?: number; max?: number }> =
  fc.record({ min: intArb, max: intArb }, { requiredKeys: [] });

/** Enum constraints: the allowed value nicks (occasionally empty). */
const enumConstraintsArb: fc.Arbitrary<Record<string, unknown>> = fc.record({
  values: fc.array(fc.string({ minLength: 1, maxLength: 8 }), {
    maxLength: 5,
  }),
});

/** One ParameterForm raw-text row, with and without ride-along constraints. */
export const parameterFormArb: fc.Arbitrary<ParameterForm> = fc.record(
  {
    name: parameterNameArb,
    paramType: fc.constantFrom(...PARAM_TYPES),
    required: fc.boolean(),
    defaultValue: fc.string({ maxLength: 12 }),
    description: fc.string({ maxLength: 40 }),
    example: fc.string({ maxLength: 12 }),
    enumValues: fc.string({ maxLength: 24 }),
    constraints: numericConstraintsArb,
  },
  {
    requiredKeys: [
      'name',
      'paramType',
      'required',
      'defaultValue',
      'description',
      'example',
      'enumValues',
    ],
  }
);

/**
 * One ParameterDeclaration suggestion in the gst-properties wire shape:
 * `default` absent, null, or a typed value; `examples` possibly empty;
 * `constraints` absent, numeric {min,max}, or enum {values} per type.
 */
export const suggestionArb: fc.Arbitrary<ParameterDeclaration> = fc
  .constantFrom(...PARAM_TYPES)
  .chain((paramType) => {
    const valueArb = valueArbFor(paramType);
    const constraintsArb: fc.Arbitrary<Record<string, unknown>> =
      paramType === 'enum'
        ? enumConstraintsArb
        : (numericConstraintsArb as fc.Arbitrary<Record<string, unknown>>);
    return fc.record(
      {
        name: parameterNameArb,
        paramType: fc.constant<string>(paramType),
        required: fc.boolean(),
        description: fc.string({ maxLength: 40 }),
        examples: fc.array(valueArb, { maxLength: 3 }),
        default: fc.option(valueArb, { nil: null }),
        constraints: constraintsArb,
      },
      {
        requiredKeys: ['name', 'paramType', 'required', 'description', 'examples'],
      }
    );
  });

/** An existing wizard parameter list. */
export const existingParametersArb: fc.Arbitrary<ParameterForm[]> = fc.array(
  parameterFormArb,
  { maxLength: 8 }
);

/** A scanned suggestion list (duplicate names within the list included). */
export const suggestionsArb: fc.Arbitrary<ParameterDeclaration[]> = fc.array(
  suggestionArb,
  { maxLength: 8 }
);

// ---------------------------------------------------------------------------
// Realistic Parameter_Suggestions (Property 11)
//
// The round-trip property (formFromSuggestion -> parameterFromForm via
// buildDeclaration) holds for the suggestions the backend actually serves,
// not for arbitrary ParameterDeclaration-shaped values: the raw-text form
// trims names/descriptions/values and joins/splits enum values on commas,
// so the generator below mirrors the wire contract of
// `gst_properties.py`'s `_suggestion`/`map_property` exactly:
//
// - `name` is a GObject property name (launch-safe identifier, no
//   whitespace or commas);
// - `description` is the pspec blurb (realistic blurbs are trim-stable
//   text) or the backend's synthesized
//   `"<name> (<gtype>) property of the plugin element"` fallback;
// - `required` is true iff there is no usable default, and `default` is
//   present exactly when optional (never `null` on the wire);
// - `examples` always carries exactly one value: the default when usable,
//   else the synthesized example (range min, else max, else 0 for
//   numerics; `false` for bool; `"value"` for string; the first nick for
//   enum) — Requirement 2.6;
// - numeric `constraints` carry min/max only when the property is ranged,
//   with min <= max and any default inside the range; int values stay in
//   the JS safe-integer range (the JSON API boundary guarantees that);
// - string defaults are non-empty, trim-stable text (3.1 already filtered
//   NULL/empty/whitespace-only defaults server-side);
// - enum `constraints.values` are GEnum nicks: lowercase identifiers
//   without commas or whitespace, and any default is one of the nicks.
// ---------------------------------------------------------------------------

/** GObject property name: launch-safe identifier, no whitespace/commas. */
const gobjectPropertyNameArb: fc.Arbitrary<string> =
  fc.stringMatching(/^[a-z][a-z0-9-]{0,14}$/);

/** Trim-stable, non-empty human text (pspec blurbs, string defaults). */
const trimStableTextArb: fc.Arbitrary<string> = fc
  .array(fc.stringMatching(/^[A-Za-z][A-Za-z0-9]{0,7}$/), {
    minLength: 1,
    maxLength: 6,
  })
  .map((words) => words.join(' '));

/** GEnum value nick: lowercase identifier, no commas/whitespace. */
const enumNickArb: fc.Arbitrary<string> =
  fc.stringMatching(/^[a-z][a-z0-9-]{0,11}$/);

/** Finite double, -0 normalized (String(-0) === '0' would not round-trip). */
const finiteFloatArb: fc.Arbitrary<number> = fc
  .double({ noNaN: true, noDefaultInfinity: true })
  .map((value) => (Object.is(value, -0) ? 0 : value));

/** The backend's blurb-or-synthesized description rule (2.4). */
function describeProperty(
  name: string,
  gtype: string,
  blurb: string | undefined
): string {
  return blurb ?? `${name} (${gtype}) property of the plugin element`;
}

/** Mirror of `gst_properties._suggestion`: wire-shape assembly (3.1, 3.2, 2.6). */
function wireSuggestion(
  name: string,
  paramType: string,
  description: string,
  defaultValue: string | number | boolean | undefined,
  constraints: Record<string, unknown> | undefined,
  synthesizedExample: string | number | boolean
): ParameterDeclaration {
  const suggestion: ParameterDeclaration = {
    name,
    paramType,
    required: defaultValue === undefined,
    description,
    examples: [defaultValue === undefined ? synthesizedExample : defaultValue],
  };
  if (defaultValue !== undefined) {
    suggestion.default = defaultValue;
  }
  if (constraints !== undefined) {
    suggestion.constraints = constraints;
  }
  return suggestion;
}

/** Ranged numeric suggestion (int/float share the min<=default<=max shape). */
function rangedNumericSuggestionArb(
  paramType: 'int' | 'float',
  gtypeArb: fc.Arbitrary<string>,
  valueArb: fc.Arbitrary<number>,
  unrangedExample: number
): fc.Arbitrary<ParameterDeclaration> {
  return fc
    .tuple(
      gobjectPropertyNameArb,
      gtypeArb,
      fc.option(trimStableTextArb, { nil: undefined }),
      fc.tuple(valueArb, valueArb, valueArb).map((values) =>
        [...values].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
      ),
      fc.boolean(),
      fc.boolean(),
      fc.boolean()
    )
    .map(([name, gtype, blurb, [lo, mid, hi], hasMin, hasMax, hasDefault]) => {
      const constraints =
        hasMin || hasMax
          ? {
              ...(hasMin ? { min: lo } : {}),
              ...(hasMax ? { max: hi } : {}),
            }
          : undefined;
      // Backend synthesized example: range min, else max, else 0 (2.6).
      const synthesized = hasMin ? lo : hasMax ? hi : unrangedExample;
      return wireSuggestion(
        name,
        paramType,
        describeProperty(name, gtype, blurb),
        hasDefault ? mid : undefined,
        constraints,
        synthesized
      );
    });
}

const realisticIntSuggestionArb = rangedNumericSuggestionArb(
  'int',
  fc.constantFrom('gint', 'guint', 'gint64', 'guint64', 'glong', 'gulong', 'guchar'),
  fc.integer(),
  0
);

const realisticFloatSuggestionArb = rangedNumericSuggestionArb(
  'float',
  fc.constantFrom('gfloat', 'gdouble'),
  finiteFloatArb,
  0
);

const realisticBoolSuggestionArb: fc.Arbitrary<ParameterDeclaration> = fc
  .tuple(
    gobjectPropertyNameArb,
    fc.option(trimStableTextArb, { nil: undefined }),
    fc.option(fc.boolean(), { nil: undefined })
  )
  .map(([name, blurb, defaultValue]) =>
    wireSuggestion(
      name,
      'bool',
      describeProperty(name, 'gboolean', blurb),
      defaultValue,
      undefined,
      false
    )
  );

const realisticStringSuggestionArb: fc.Arbitrary<ParameterDeclaration> = fc
  .tuple(
    gobjectPropertyNameArb,
    fc.option(trimStableTextArb, { nil: undefined }),
    fc.option(trimStableTextArb, { nil: undefined })
  )
  .map(([name, blurb, defaultValue]) =>
    wireSuggestion(
      name,
      'string',
      describeProperty(name, 'gchararray', blurb),
      defaultValue,
      undefined,
      'value'
    )
  );

const realisticEnumSuggestionArb: fc.Arbitrary<ParameterDeclaration> = fc
  .tuple(
    gobjectPropertyNameArb,
    fc.stringMatching(/^Gst[A-Z][A-Za-z0-9]{0,11}$/),
    fc.option(trimStableTextArb, { nil: undefined }),
    fc.uniqueArray(enumNickArb, { minLength: 1, maxLength: 6 }),
    fc.option(fc.nat(), { nil: undefined })
  )
  .map(([name, gtype, blurb, nicks, defaultIndex]) =>
    wireSuggestion(
      name,
      'enum',
      describeProperty(name, gtype, blurb),
      defaultIndex === undefined ? undefined : nicks[defaultIndex % nicks.length],
      { values: nicks },
      nicks[0]
    )
  );

/**
 * A realistic Parameter_Suggestion exactly as the gst-properties route
 * serves it (`suggestions_for_element` -> `map_property` output), across
 * all five mapped paramTypes.
 */
export const realisticSuggestionArb: fc.Arbitrary<ParameterDeclaration> =
  fc.oneof(
    realisticIntSuggestionArb,
    realisticFloatSuggestionArb,
    realisticBoolSuggestionArb,
    realisticStringSuggestionArb,
    realisticEnumSuggestionArb
  );

// ---------------------------------------------------------------------------
// Scan elements (Property 12: element picking)
//
// Factory names draw mostly from a small pool so a preferred factory
// actually matches generated elements often, with a random-identifier
// tail so misses occur too (pickElement's fallback branches).
// ---------------------------------------------------------------------------

/** Small factory-name pool shared with preferred-factory picks. */
const FACTORY_POOL = ['videoflip', 'myblur', 'dda-crop', 'identity'];

/** An element factory name: mostly pooled, sometimes random. */
export const factoryNameArb: fc.Arbitrary<string> = fc.oneof(
  { arbitrary: fc.constantFrom(...FACTORY_POOL), weight: 3 },
  { arbitrary: fc.stringMatching(/^[a-z][a-z0-9-]{0,11}$/), weight: 1 }
);

/** One ScanElement of a gst-properties response (duplicate factories allowed). */
export const scanElementArb: fc.Arbitrary<ScanElement> = fc.record({
  factory: factoryNameArb,
  suggestions: suggestionsArb,
  skipped: fc.array(
    fc.record({
      name: gobjectPropertyNameArb,
      reason: fc.string({ minLength: 1, maxLength: 24 }),
    }),
    { maxLength: 3 }
  ),
});
