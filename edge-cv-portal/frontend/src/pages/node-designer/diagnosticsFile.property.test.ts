/**
 * **Feature: custom-node-source-lifecycle, Property 18: Failed-arch file picking is deterministic and in-tree**
 *
 * For any log text and Source_Tree path list containing the scaffold files,
 * `pickFileForDiagnostics` returns a path from the list; it returns the
 * first listed path mentioned in the log when any is, else the failing
 * architecture's meson file for meson/ninja markers, else the C source for
 * compiler markers, else the hook file.
 *
 * **Validates: Requirements 5.1**
 */
import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { HOOK_FILE, pickFileForDiagnostics } from './diagnosticsFile';

const ARCHES = ['x86_64', 'x86_64_nvidia', 'arm64_jp4', 'arm64_jp5', 'arm64_jp6', 'arm64_jp7'];
const C_SOURCE = 'plugin/gstcustomblur.c';

function scaffoldPaths(arches: string[]): string[] {
  return [HOOK_FILE, C_SOURCE, 'README.md', ...arches.map((a) => `builds/${a}/meson.build`)];
}

const fillerArb = fc.stringMatching(/^[A-Za-z0-9 ,;:()\n-]{0,40}$/);
const mesonMarkerArb = fc.constantFrom('meson.build:12:0: ERROR', 'ninja: build stopped', 'dependency(gstreamer) not found');
const compilerMarkerArb = fc.constantFrom('gcc: error: unrecognized', 'undefined reference to gst_pad', 'error: expected ; before');

describe('Property 18: Failed-arch file picking', () => {
  it('always returns an in-tree path with the documented precedence', () => {
    fc.assert(
      fc.property(
        fc.subarray(ARCHES, { minLength: 1 }),
        fc.constantFrom(...ARCHES),
        fc.constantFrom('mention', 'meson', 'compiler', 'none'),
        fillerArb,
        fillerArb,
        (arches, failing, kind, before, after) => {
          const paths = scaffoldPaths(arches);
          let log: string;
          let expected: string;
          const mesonForFailing = `builds/${failing}/meson.build`;
          if (kind === 'mention') {
            const mentioned = paths[(before.length + after.length) % paths.length];
            log = `${before} ${mentioned} ${after}`;
            // The earliest mention wins; the filler is marker-free by
            // construction so only `mentioned` can match — unless a shorter
            // path is a substring of the mentioned one (README.md in a
            // longer path cannot be; hook/C/meson share no substrings).
            expected = mentioned;
          } else if (kind === 'meson') {
            log = `${before} ${fc.sample(mesonMarkerArb, 1)[0]} ${after}`;
            expected = paths.includes(mesonForFailing)
              ? mesonForFailing
              : paths.find((p) => p.endsWith('/meson.build'))!;
          } else if (kind === 'compiler') {
            log = `${before} ${fc.sample(compilerMarkerArb, 1)[0]} ${after}`;
            expected = C_SOURCE;
          } else {
            log = `${before} ${after}`;
            expected = HOOK_FILE;
          }
          const picked = pickFileForDiagnostics(log, paths, failing);
          expect(paths).toContain(picked);
          expect(picked).toBe(expected);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('returns null only for an empty tree', () => {
    expect(pickFileForDiagnostics('anything', [], 'x86_64')).toBeNull();
    fc.assert(
      fc.property(fc.string({ maxLength: 50 }), fc.array(fc.stringMatching(/^[a-z]{1,6}\.txt$/), { minLength: 1, maxLength: 4 }),
        (log, paths) => {
          const picked = pickFileForDiagnostics(log, paths, null);
          expect(picked).not.toBeNull();
          expect(paths).toContain(picked!);
        }),
      { numRuns: 100 }
    );
  });
});
