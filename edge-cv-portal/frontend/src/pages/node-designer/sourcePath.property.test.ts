/**
 * **Feature: custom-node-source-lifecycle, Property 2: Source path validity predicate**
 *
 * For any string, `isValidSourcePath` accepts it iff the POSIX-normalized
 * trimmed path is non-empty, not `.`, does not start with `/` or `..`, and
 * contains no `..` segment (the frontend twin of the backend's
 * normalize_source_path).
 *
 * **Validates: Requirements 1.3, 3.1**
 */
import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { isValidSourcePath, normalizeSourcePath } from './sourcePath';

function referenceNormalize(path: string): string {
  const out: string[] = [];
  for (const seg of path.split('/')) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') {
      if (out.length && out[out.length - 1] !== '..') out.pop();
      else out.push('..');
      continue;
    }
    out.push(seg);
  }
  return out.length ? out.join('/') : '.';
}

function referenceValid(path: string): boolean {
  const stripped = path.trim();
  if (!stripped) return false;
  if (stripped.startsWith('/') || stripped.startsWith('\\')) return false;
  const norm = referenceNormalize(stripped);
  if (norm === '.' || norm.startsWith('..')) return false;
  const segments = norm.split('/');
  if (segments.some((s) => s === '..')) return false;
  // Fixed-point rule: no segment with leading/trailing whitespace, no
  // control characters (shared with the backend oracle).
  if (segments.some((s) => s !== s.trim())) return false;
  for (const ch of norm) {
    const code = ch.charCodeAt(0);
    if (code < 0x20 || code === 0x7f) return false;
  }
  return norm.length <= 200;
}

const segmentArb = fc.oneof(
  fc.constantFrom('..', '.', '', ' ', 'src', 'plugin', 'builds', 'x86_64', 'meson.build'),
  fc.stringMatching(/^[a-z0-9_.-]{1,8}$/)
);
const pathArb = fc.oneof(
  fc.array(segmentArb, { maxLength: 5 }).map((s) => s.join('/')),
  fc.array(segmentArb, { maxLength: 5 }).map((s) => '/' + s.join('/')),
  fc.array(segmentArb, { maxLength: 5 }).map((s) => s.join('/') + '/'),
  fc.string({ maxLength: 20 })
);

/** Shared accepted/rejected corpus with the backend test (Property 2 twins). */
const CORPUS: Array<[string, boolean]> = [
  ['plugin/frame_processing_hook.py', true],
  ['builds/x86_64/meson.build', true],
  ['README.md', true],
  ['docs/notes.md', true],
  ['./a/b.c', true],
  ['a//b.c', true],
  ['../x.py', false],
  ['/abs.py', false],
  ['a/../../y', false],
  ['.', false],
  ['', false],
  ['  ', false],
  ['a/..', false],
  // Fixed-point rule (Hypothesis-found on the backend twin: "0\r/" used to
  // normalize to "0\r", which then normalized to "0").
  ['0\r/', false],
  ['a/ b', false],
  ['a /b', false],
  ['a/b\t', true], // trimmed whole-string whitespace is fine
  ['a\u0000b', false],
  [' src/plugin.c ', true],
];

describe('Property 2: Source path validity predicate', () => {
  it('matches the normalized-path rule for arbitrary strings', () => {
    fc.assert(
      fc.property(pathArb, (path) => {
        expect(isValidSourcePath(path)).toBe(referenceValid(path));
        const normalized = normalizeSourcePath(path);
        if (normalized !== null) {
          expect(normalizeSourcePath(normalized)).toBe(normalized);
          expect(normalized.startsWith('/')).toBe(false);
          expect(normalized.split('/')).not.toContain('..');
        }
      }),
      { numRuns: 100 }
    );
  });

  it('agrees with the shared corpus', () => {
    for (const [path, expected] of CORPUS) {
      expect(isValidSourcePath(path)).toBe(expected);
    }
  });
});
