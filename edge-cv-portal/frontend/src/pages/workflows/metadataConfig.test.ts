/**
 * Example-based unit tests for the TypeScript mirror of the shared
 * Metadata_Node config rules (workflow-manager-gaps Requirements 6.3,
 * 6.7). Cross-language parity with `metadata_config.py` is covered by
 * the fast-check property suite (task 14.3, Property 16); these tests
 * pin the concrete semantics per error class.
 */

import { describe, expect, it } from 'vitest';
import {
  ERROR_DUPLICATE_KEY,
  ERROR_EMPTY_FIELD_PATH,
  ERROR_EMPTY_KEY,
  ERROR_MAPPINGS_INVALID,
  ERROR_STATIC_JSON_INVALID,
  ERROR_TOO_MANY_MAPPINGS,
  MAX_MAPPINGS,
  MAX_STATIC_JSON_CHARS,
  parseMappings,
  parseStaticJson,
  validateMetadataConfig,
} from './metadataConfig';

function codes(errors: { code: string }[]): string[] {
  return errors.map((e) => e.code);
}

describe('parseMappings', () => {
  it('treats null/undefined and empty/whitespace-only strings as no mappings', () => {
    for (const raw of [null, undefined, '', '   ', '\t\n']) {
      const [mappings, errors] = parseMappings(raw);
      expect(mappings).toEqual([]);
      expect(errors).toEqual([]);
    }
  });

  it('parses a valid array and trims paths and keys', () => {
    const [mappings, errors] = parseMappings(
      '[{"path": " job_id ", "key": "jobId"}, {"path": "meta.file", "key": " file "}]'
    );
    expect(errors).toEqual([]);
    expect(mappings).toEqual([
      { path: 'job_id', key: 'jobId' },
      { path: 'meta.file', key: 'file' },
    ]);
  });

  it('flags non-string raw values as mappings_invalid', () => {
    const [mappings, errors] = parseMappings(42);
    expect(mappings).toEqual([]);
    expect(codes(errors)).toEqual([ERROR_MAPPINGS_INVALID]);
  });

  it('flags unparseable JSON as mappings_invalid', () => {
    const [mappings, errors] = parseMappings('[{"path": "a"');
    expect(mappings).toEqual([]);
    expect(codes(errors)).toEqual([ERROR_MAPPINGS_INVALID]);
  });

  it('flags non-array JSON as mappings_invalid', () => {
    const [, errors] = parseMappings('{"path": "a", "key": "b"}');
    expect(codes(errors)).toEqual([ERROR_MAPPINGS_INVALID]);
  });

  it('flags entries that are not {path, key} string objects as mappings_invalid', () => {
    for (const raw of [
      '[1]',
      '[["path", "key"]]',
      '[null]',
      '[{"path": "a"}]',
      '[{"path": "a", "key": 3}]',
      '[{"path": null, "key": "b"}]',
    ]) {
      const [mappings, errors] = parseMappings(raw);
      expect(mappings).toEqual([]);
      expect(codes(errors)).toEqual([ERROR_MAPPINGS_INVALID]);
    }
  });

  it('reports empty trimmed paths and keys per mapping', () => {
    const [, errors] = parseMappings(
      '[{"path": "  ", "key": "k"}, {"path": "p", "key": ""}, {"path": "", "key": " "}]'
    );
    expect(codes(errors)).toEqual([
      ERROR_EMPTY_FIELD_PATH,
      ERROR_EMPTY_KEY,
      ERROR_EMPTY_FIELD_PATH,
      ERROR_EMPTY_KEY,
    ]);
  });

  it('reports one duplicate_key error per duplicated key', () => {
    const [, errors] = parseMappings(
      JSON.stringify([
        { path: 'a', key: 'x' },
        { path: 'b', key: 'x' },
        { path: 'c', key: 'x' },
        { path: 'd', key: 'y' },
        { path: 'e', key: ' y ' },
      ])
    );
    expect(codes(errors)).toEqual([ERROR_DUPLICATE_KEY, ERROR_DUPLICATE_KEY]);
  });

  it('does not count empty keys toward duplicates', () => {
    const [, errors] = parseMappings(
      '[{"path": "a", "key": ""}, {"path": "b", "key": " "}]'
    );
    expect(codes(errors)).toEqual([ERROR_EMPTY_KEY, ERROR_EMPTY_KEY]);
  });

  it('accepts exactly MAX_MAPPINGS entries and rejects one more', () => {
    const entry = (i: number) => ({ path: `p${i}`, key: `k${i}` });
    const atLimit = JSON.stringify(
      Array.from({ length: MAX_MAPPINGS }, (_, i) => entry(i))
    );
    expect(parseMappings(atLimit)[1]).toEqual([]);

    const overLimit = JSON.stringify(
      Array.from({ length: MAX_MAPPINGS + 1 }, (_, i) => entry(i))
    );
    const [mappings, errors] = parseMappings(overLimit);
    expect(mappings).toHaveLength(MAX_MAPPINGS + 1);
    expect(codes(errors)).toEqual([ERROR_TOO_MANY_MAPPINGS]);
  });
});

describe('parseStaticJson', () => {
  it('treats null/undefined and empty/whitespace-only strings as no static JSON', () => {
    for (const raw of [null, undefined, '', '  ', '\n']) {
      const [value, errors] = parseStaticJson(raw);
      expect(value).toBeNull();
      expect(errors).toEqual([]);
    }
  });

  it('parses a valid JSON object', () => {
    const [value, errors] = parseStaticJson('{"line": "A", "shift": 2}');
    expect(errors).toEqual([]);
    expect(value).toEqual({ line: 'A', shift: 2 });
  });

  it('flags non-string raw values as static_json_invalid', () => {
    const [value, errors] = parseStaticJson({ line: 'A' });
    expect(value).toBeNull();
    expect(codes(errors)).toEqual([ERROR_STATIC_JSON_INVALID]);
  });

  it('flags unparseable JSON as static_json_invalid', () => {
    const [value, errors] = parseStaticJson('{"line": ');
    expect(value).toBeNull();
    expect(codes(errors)).toEqual([ERROR_STATIC_JSON_INVALID]);
  });

  it('flags non-object JSON values as static_json_invalid', () => {
    for (const raw of ['[]', '"text"', '3', 'true', 'null']) {
      const [value, errors] = parseStaticJson(raw);
      expect(value).toBeNull();
      expect(codes(errors)).toEqual([ERROR_STATIC_JSON_INVALID]);
    }
  });

  it('accepts exactly MAX_STATIC_JSON_CHARS characters and rejects one more', () => {
    const prefix = '{"k": "';
    const suffix = '"}';
    const pad = MAX_STATIC_JSON_CHARS - prefix.length - suffix.length;
    const atLimit = prefix + 'x'.repeat(pad) + suffix;
    expect(atLimit).toHaveLength(MAX_STATIC_JSON_CHARS);
    expect(parseStaticJson(atLimit)[1]).toEqual([]);

    const overLimit = prefix + 'x'.repeat(pad + 1) + suffix;
    const [value, errors] = parseStaticJson(overLimit);
    expect(value).toBeNull();
    expect(codes(errors)).toEqual([ERROR_STATIC_JSON_INVALID]);
  });
});

describe('validateMetadataConfig', () => {
  it('returns empty field errors for a valid configuration', () => {
    const result = validateMetadataConfig(
      '[{"path": "job_id", "key": "jobId"}]',
      '{"line": "A"}'
    );
    expect(result).toEqual({ mappings: [], staticJson: [] });
  });

  it('groups errors by field', () => {
    const result = validateMetadataConfig('not json', '[]');
    expect(codes(result.mappings)).toEqual([ERROR_MAPPINGS_INVALID]);
    expect(codes(result.staticJson)).toEqual([ERROR_STATIC_JSON_INVALID]);
  });
});
