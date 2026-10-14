/**
 * Shared validity rules for the Metadata_Node configuration parameters
 * (workflow-manager-gaps Requirements 6.3, 6.7).
 *
 * TypeScript mirror of the backend source of truth
 * `workflow_core/catalog/metadata_config.py`: parse/validate the two
 * string parameters of the `metadata` node type —
 *
 * - `mappings`: a JSON array of `{"path": ..., "key": ...}` string
 *   pairs (0..MAX_MAPPINGS entries) mapping a trigger-payload field
 *   path to an output metadata key.
 * - `static_json`: an optional static JSON object of at most
 *   MAX_STATIC_JSON_CHARS characters attached alongside the mappings.
 *
 * Both helpers are total: they never throw on any input and instead
 * report problems through the returned error list. Each error is
 * `{code, message}` where the code is one of the `ERROR_*` constants
 * shared with the Python validator. The semantics (including error
 * ordering and one-error-per-duplicated-key) mirror the Python module
 * exactly so the designer, the Workflow_Validator, and the
 * Workflow_Compiler agree on validity.
 *
 * Known Python/TypeScript JSON divergence: Python's `json.loads`
 * accepts the non-standard literals `NaN`/`Infinity`/`-Infinity`,
 * which strict `JSON.parse` rejects. Configurations containing them
 * are invalid JSON as far as this mirror is concerned.
 */

import type { JsonValue } from './types';

/** Maximum number of Metadata_Mappings a single node may declare. */
export const MAX_MAPPINGS = 50;

/** Maximum length (in code points) of the raw `static_json` parameter. */
export const MAX_STATIC_JSON_CHARS = 10240;

// Error classes shared with the Python validator (one finding code per
// class), the compiler, and this TypeScript mirror.
export const ERROR_MAPPINGS_INVALID = 'mappings_invalid';
export const ERROR_EMPTY_FIELD_PATH = 'empty_field_path';
export const ERROR_EMPTY_KEY = 'empty_key';
export const ERROR_DUPLICATE_KEY = 'duplicate_key';
export const ERROR_TOO_MANY_MAPPINGS = 'too_many_mappings';
export const ERROR_STATIC_JSON_INVALID = 'static_json_invalid';

/** A single violated rule, mirroring the Python error dicts. */
export interface MetadataConfigError {
  code: string;
  message: string;
}

/** One parsed Metadata_Mapping with trimmed path and key. */
export interface MetadataMapping {
  path: string;
  key: string;
}

function error(code: string, message: string): MetadataConfigError {
  return { code, message };
}

/**
 * Python's `str.strip()` whitespace set (characters for which
 * `str.isspace()` is true). This differs from JavaScript's
 * `String.prototype.trim()` in both directions: Python additionally
 * strips U+001C–U+001F and U+0085, while JS strips U+FEFF (BOM) which
 * Python does not. Matching Python exactly keeps the two validators'
 * accept/reject partitions identical.
 */
const PY_WHITESPACE =
  '\t\n\u000b\f\r\u001c\u001d\u001e\u001f \u0085\u00a0\u1680' +
  '\u2000-\u200a\u2028\u2029\u202f\u205f\u3000';
const PY_TRIM_RE = new RegExp(
  `^[${PY_WHITESPACE}]+|[${PY_WHITESPACE}]+$`,
  'g'
);

/** Trim exactly the characters Python's `str.strip()` trims. */
function pythonTrim(value: string): string {
  return value.replace(PY_TRIM_RE, '');
}

/**
 * Parse and validate the `mappings` parameter value.
 *
 * Returns `[mappings, errors]` where `mappings` is the list of
 * `{path, key}` entries with paths and keys trimmed, and `errors`
 * lists every violated rule:
 *
 * - ERROR_MAPPINGS_INVALID: `raw` is not a string, is not parseable
 *   as JSON, or does not parse to an array whose every entry is a
 *   JSON object carrying string `path` and `key` values (the returned
 *   list is empty in this case);
 * - ERROR_EMPTY_FIELD_PATH: a mapping whose `path` is empty or
 *   whitespace-only after trimming (one error per such mapping);
 * - ERROR_EMPTY_KEY: a mapping whose `key` is empty or
 *   whitespace-only after trimming (one error per such mapping);
 * - ERROR_DUPLICATE_KEY: the same trimmed output key appears in more
 *   than one mapping (one error per duplicated key);
 * - ERROR_TOO_MANY_MAPPINGS: more than MAX_MAPPINGS entries.
 *
 * `null`/`undefined` and empty/whitespace-only strings are treated as
 * the descriptor default `"[]"` (no mappings, no errors). A valid
 * configuration is exactly one for which `errors` is empty.
 */
export function parseMappings(
  raw: unknown
): [MetadataMapping[], MetadataConfigError[]] {
  if (raw === null || raw === undefined) {
    return [[], []];
  }
  if (typeof raw !== 'string') {
    return [
      [],
      [error(ERROR_MAPPINGS_INVALID, 'mappings must be a JSON array string')],
    ];
  }
  const text = pythonTrim(raw);
  if (!text) {
    return [[], []];
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return [
      [],
      [error(ERROR_MAPPINGS_INVALID, 'mappings is not parseable as JSON')],
    ];
  }
  if (!Array.isArray(parsed)) {
    return [
      [],
      [
        error(
          ERROR_MAPPINGS_INVALID,
          'mappings must be a JSON array of {"path", "key"} objects'
        ),
      ],
    ];
  }

  const mappings: MetadataMapping[] = [];
  for (let index = 0; index < parsed.length; index += 1) {
    const entry: unknown = parsed[index];
    const isObject =
      typeof entry === 'object' && entry !== null && !Array.isArray(entry);
    const path = isObject
      ? (entry as Record<string, unknown>).path
      : undefined;
    const key = isObject ? (entry as Record<string, unknown>).key : undefined;
    if (!isObject || typeof path !== 'string' || typeof key !== 'string') {
      return [
        [],
        [
          error(
            ERROR_MAPPINGS_INVALID,
            `mappings entry ${index} must be an object with string ` +
              '"path" and "key" values'
          ),
        ],
      ];
    }
    mappings.push({ path: pythonTrim(path), key: pythonTrim(key) });
  }

  const errors: MetadataConfigError[] = [];
  mappings.forEach((mapping, index) => {
    if (!mapping.path) {
      errors.push(
        error(
          ERROR_EMPTY_FIELD_PATH,
          `mapping ${index} has an empty trigger-payload field path`
        )
      );
    }
    if (!mapping.key) {
      errors.push(
        error(ERROR_EMPTY_KEY, `mapping ${index} has an empty output metadata key`)
      );
    }
  });

  const seen = new Map<string, number>();
  const reported = new Set<string>();
  for (const mapping of mappings) {
    const key = mapping.key;
    if (!key) {
      continue; // already reported as ERROR_EMPTY_KEY
    }
    const count = (seen.get(key) ?? 0) + 1;
    seen.set(key, count);
    if (count > 1 && !reported.has(key)) {
      reported.add(key);
      errors.push(
        error(
          ERROR_DUPLICATE_KEY,
          `output metadata key '${key}' is used by more than one mapping`
        )
      );
    }
  }

  if (mappings.length > MAX_MAPPINGS) {
    errors.push(
      error(
        ERROR_TOO_MANY_MAPPINGS,
        `at most ${MAX_MAPPINGS} mappings are allowed (got ${mappings.length})`
      )
    );
  }

  return [mappings, errors];
}

/**
 * Parse and validate the `static_json` parameter value.
 *
 * Returns `[staticJson, errors]` where `staticJson` is the parsed
 * JSON object, or `null` when no static JSON is configured or the
 * value is invalid. Errors carry ERROR_STATIC_JSON_INVALID when the
 * value is not a string, is longer than MAX_STATIC_JSON_CHARS
 * characters, is not parseable as JSON, or parses to a value that is
 * not a JSON object.
 *
 * `null`/`undefined` and empty/whitespace-only strings mean "no
 * static JSON" (the descriptor default `""`) and produce no errors.
 */
export function parseStaticJson(
  raw: unknown
): [Record<string, JsonValue> | null, MetadataConfigError[]] {
  if (raw === null || raw === undefined) {
    return [null, []];
  }
  if (typeof raw !== 'string') {
    return [
      null,
      [
        error(
          ERROR_STATIC_JSON_INVALID,
          'static_json must be a JSON object string'
        ),
      ],
    ];
  }
  if (!pythonTrim(raw)) {
    return [null, []];
  }
  // Python's len() counts code points, not UTF-16 code units; count
  // code points here so both sides apply the limit identically.
  const length = [...raw].length;
  if (length > MAX_STATIC_JSON_CHARS) {
    return [
      null,
      [
        error(
          ERROR_STATIC_JSON_INVALID,
          `static_json exceeds ${MAX_STATIC_JSON_CHARS} characters ` +
            `(got ${length})`
        ),
      ],
    ];
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [
      null,
      [
        error(
          ERROR_STATIC_JSON_INVALID,
          'static_json is not parseable as JSON'
        ),
      ],
    ];
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return [
      null,
      [
        error(
          ERROR_STATIC_JSON_INVALID,
          'static_json must parse to a JSON object'
        ),
      ],
    ];
  }
  return [parsed as Record<string, JsonValue>, []];
}

/**
 * Field-level validation errors for a Metadata_Node configuration,
 * keyed by the parameter each error belongs to. A configuration is
 * saveable exactly when both lists are empty (Requirements 6.3, 6.7).
 */
export interface MetadataConfigFieldErrors {
  mappings: MetadataConfigError[];
  staticJson: MetadataConfigError[];
}

/**
 * Validate both Metadata_Node parameters at once, returning the
 * errors grouped by field for the node configuration UI.
 */
export function validateMetadataConfig(
  mappingsRaw: unknown,
  staticJsonRaw: unknown
): MetadataConfigFieldErrors {
  const [, mappingErrors] = parseMappings(mappingsRaw);
  const [, staticErrors] = parseStaticJson(staticJsonRaw);
  return { mappings: mappingErrors, staticJson: staticErrors };
}
