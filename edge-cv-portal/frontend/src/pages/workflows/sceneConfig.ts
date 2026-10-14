/**
 * Scene_Analytics_Node configuration parsers for the Workflow_Builder
 * (rtsp-rtmp-stream-cameras Requirements 13.3, 13.4, 13.7, 14.6).
 *
 * TypeScript port of `label_key`, `parse_label_list` and `parse_zone` in
 * `workflow_core/analytics/scene.py`, the single source of truth the
 * validator (V13), the device, and the test sandbox share. The inline
 * V13 mirror in `inlineChecks.ts` uses these, so a value the backend
 * rejects is marked on the canvas with the same problems.
 *
 * Parity is exact for string, number, boolean and null values and for
 * lists of them, which is every value the configuration panel produces.
 * A nested container inside a label parameter (possible only through an
 * imported definition) is read through `JSON.stringify` where Python uses
 * `str()`; both agree on whether it yields a usable label. Zone JSON is
 * read the way Python's `json.loads` reads it (`pythonJsonLoads`).
 */
import { pyStrip } from './streamUrl';

/** A JSON string, or one of the three non-standard constants. */
const PY_JSON_CONSTANTS = /"(?:[^"\\]|\\[\s\S])*"|(-Infinity|Infinity|NaN)/g;

/**
 * `JSON.parse` with Python `json.loads`'s one extension: the `NaN`,
 * `Infinity` and `-Infinity` literals. They parse to non-finite floats in
 * Python, which every caller here treats as "not a number"; each is read
 * here as a string, which gets the same treatment and the same messages.
 */
export function pythonJsonLoads(text: string): unknown {
  return JSON.parse(
    text.replace(PY_JSON_CONSTANTS, (match, constant: string | undefined) =>
      constant === undefined ? match : JSON.stringify(`\u0000${constant}`)
    )
  );
}

/** A Label_Key: lowercase ASCII words joined by single underscores. */
export const LABEL_KEY_PATTERN = '^[a-z0-9]+(_[a-z0-9]+)*$';
export const MIN_ZONE_POINTS = 3;
export const MAX_ZONE_POINTS = 32;
/** Most entries a `classes` list may have. */
export const MAX_CLASS_LIST_ITEMS = 32;
/** Most entries `object_association.required_classes` may have. */
export const MAX_REQUIRED_CLASSES = 10;

export type ZonePoint = [number, number];

/** A non-string value as Python's `str()` would spell it, for label use. */
function labelText(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  if (value === null || value === undefined) {
    return 'None';
  }
  if (typeof value === 'boolean') {
    return value ? 'True' : 'False';
  }
  if (typeof value === 'number') {
    return String(value);
  }
  return JSON.stringify(value) ?? '';
}

/**
 * The Label_Key of `label`: lowercased, every run of characters other
 * than ASCII letters and digits replaced by one underscore, leading and
 * trailing underscores removed. Empty for a label with no ASCII letters
 * or digits; idempotent.
 */
export function labelKey(label: unknown): string {
  if (label === null || label === undefined) {
    return '';
  }
  return labelText(label)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

/** The raw, stripped entries of a label parameter. */
function labelItems(text: unknown): string[] {
  if (text === null || text === undefined) {
    return [];
  }
  if (typeof text === 'string') {
    if (pyStrip(text) === '') {
      return [];
    }
    return text.split(',').map(pyStrip);
  }
  if (Array.isArray(text)) {
    return text.map((item) =>
      pyStrip(item === null || item === undefined ? '' : labelText(item))
    );
  }
  return [pyStrip(labelText(text))];
}

/**
 * Parse a comma-separated label parameter into Label_Keys.
 *
 * Returns `[keys, problems]`: the keys de-duplicated in first-occurrence
 * order, and the problems (an empty entry, an entry with no letters or
 * digits, more than `maxItems` entries), empty exactly when the value is
 * well formed. A blank or missing value is `[[], []]`.
 */
export function parseLabelList(text: unknown, maxItems: number): [string[], string[]] {
  const items = labelItems(text);
  const problems: string[] = [];
  const keys: string[] = [];
  const seen = new Set<string>();
  items.forEach((item, index) => {
    const position = index + 1;
    if (item === '') {
      problems.push(`Label list entry ${position} is empty; remove the extra comma.`);
      return;
    }
    const key = labelKey(item);
    if (key === '') {
      problems.push(
        `Label '${item}' has no letters or digits, so it cannot be used as a metadata key.`
      );
      return;
    }
    if (seen.has(key)) {
      return;
    }
    seen.add(key);
    keys.push(key);
  });
  const limit = Number.isInteger(maxItems) ? maxItems : 0;
  if (limit > 0 && items.length > limit) {
    problems.push(`Label list has ${items.length} entries; at most ${limit} are allowed.`);
  }
  return [keys, problems];
}

function isNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function zonePoint(entry: unknown): ZonePoint | null {
  let x: unknown;
  let y: unknown;
  if (isObject(entry)) {
    x = entry.x;
    y = entry.y;
  } else if (Array.isArray(entry) && entry.length === 2) {
    [x, y] = entry;
  } else {
    return null;
  }
  if (!isNumber(x) || !isNumber(y)) {
    return null;
  }
  return [x, y];
}

/**
 * Parse a Zone parameter into normalized polygon points.
 *
 * Accepts `[[x, y], ...]`, `[{"x": x, "y": y}, ...]`, or either wrapped
 * as `{"points": [...]}`. Returns `[points, []]` for a well-formed Zone,
 * `[null, problems]` for a malformed one (invalid JSON, a point count
 * outside 3 to 32, a coordinate that is not a number in 0 to 1), and
 * `[null, []]` for a blank or missing value.
 */
export function parseZone(text: unknown): [ZonePoint[] | null, string[]] {
  if (text === null || text === undefined) {
    return [null, []];
  }
  let raw: unknown = text;
  if (typeof text === 'string') {
    if (pyStrip(text) === '') {
      return [null, []];
    }
    try {
      raw = pythonJsonLoads(text);
    } catch {
      return [null, ['Zone is not valid JSON.']];
    }
  }

  if (isObject(raw)) {
    if (!Object.prototype.hasOwnProperty.call(raw, 'points')) {
      return [null, ["Zone object must have a 'points' list of [x, y] pairs."]];
    }
    raw = raw.points;
  }

  if (!Array.isArray(raw)) {
    return [null, ["Zone must be a list of [x, y] points, or an object with a 'points' list."]];
  }

  const problems: string[] = [];
  const points: ZonePoint[] = [];
  raw.forEach((entry, index) => {
    const position = index + 1;
    const point = zonePoint(entry);
    if (point === null) {
      problems.push(`Zone point ${position} must be a pair of numbers [x, y].`);
      return;
    }
    const [x, y] = point;
    if (!(x >= 0 && x <= 1)) {
      problems.push(`Zone point ${position} x coordinate must be between 0 and 1.`);
      return;
    }
    if (!(y >= 0 && y <= 1)) {
      problems.push(`Zone point ${position} y coordinate must be between 0 and 1.`);
      return;
    }
    points.push([x, y]);
  });

  const count = raw.length;
  if (count < MIN_ZONE_POINTS || count > MAX_ZONE_POINTS) {
    problems.push(
      `Zone must have between ${MIN_ZONE_POINTS} and ${MAX_ZONE_POINTS} points; ` +
        `this one has ${count}.`
    );
  }

  if (problems.length > 0) {
    return [null, problems];
  }
  return [points, []];
}
