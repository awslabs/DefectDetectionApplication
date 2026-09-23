/**
 * **Feature: custom-node-source-lifecycle, Property 14: Bounded file context**
 *
 * For any Source_Tree map and active file, `otherTextFiles(files, active,
 * limit)` excludes the active file, never exceeds `limit` bytes in total,
 * drops the largest files first when over the limit, and `allPaths` lists
 * every path of the tree exactly once.
 *
 * **Validates: Requirements 5.6**
 */
import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { allPaths, otherTextFiles, utf8Length } from './fileContext';

const pathArb = fc.stringMatching(/^[a-z]{1,5}\/[a-z]{1,5}\.(c|py|md|build)$/);
const filesArb = fc.dictionary(pathArb, fc.string({ maxLength: 40 }), { maxKeys: 8 });

describe('Property 14: Bounded file context', () => {
  it('excludes the active file, respects the byte budget, drops largest first, lists every path once', () => {
    fc.assert(
      fc.property(
        filesArb,
        fc.integer({ min: 0, max: 120 }),
        fc.array(fc.stringMatching(/^assets\/[a-z]{1,4}\.bin$/), { maxLength: 3 }),
        (files, limit, binary) => {
          const paths = Object.keys(files);
          const active = paths.length > 0 ? paths[0] : null;
          const selected = otherTextFiles(files, active, limit);

          // Excludes the active file.
          if (active) expect(active in selected).toBe(false);
          // Every selected file is a real other file with unchanged content.
          for (const [p, c] of Object.entries(selected)) {
            expect(p).not.toBe(active);
            expect(files[p]).toBe(c);
          }
          // Total within the limit.
          const total = Object.values(selected).reduce((s, c) => s + utf8Length(c), 0);
          expect(total).toBeLessThanOrEqual(limit);
          // Largest dropped first: every omitted other file is at least as
          // large as every selected file, or would not have fit.
          const omitted = paths.filter((p) => p !== active && !(p in selected));
          const maxSelected = Math.max(0, ...Object.values(selected).map(utf8Length));
          for (const p of omitted) {
            const size = utf8Length(files[p]);
            expect(size >= maxSelected || total + size > limit).toBe(true);
          }
          // allPaths lists each path exactly once, text and binary.
          const listed = allPaths(files, binary);
          const expected = [...new Set([...paths, ...binary])].sort();
          expect(listed).toEqual(expected);
        }
      ),
      { numRuns: 100 }
    );
  });
});
